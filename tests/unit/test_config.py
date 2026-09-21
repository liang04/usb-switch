"""core.config 单元测试 —— 配置往返、凭据加密、损坏恢复。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from usbswitch.core import config
from usbswitch.core.errors import ConfigError
from usbswitch.core.models import (
    AppConfig,
    AuthMethod,
    BridgeKind,
    Host,
)


def _remote_config() -> AppConfig:
    cfg = AppConfig()
    endpoint = cfg.bridges[Host.B]
    endpoint.kind = BridgeKind.REMOTE
    endpoint.remote.host = "192.168.1.100"
    endpoint.remote.ssh_port = 2222
    endpoint.remote.username = "ubu"
    endpoint.remote.password = "SuperSecret123"
    endpoint.remote.bridge_port = 8738
    endpoint.remote.display_name = "Linux 测试机"
    return cfg


# --------------------------------------------------------------------------- #
# 往返
# --------------------------------------------------------------------------- #


def test_roundtrip_preserves_all_fields(data_dir: Path):
    path = data_dir / "config.json"
    original = _remote_config()
    original.autostart.enabled = True
    original.ble.auto_connect = True

    config.save(original, path)
    loaded = config.load(path)

    assert loaded.autostart.enabled is True
    assert loaded.ble.auto_connect is True

    assert loaded.bridges[Host.A].kind is BridgeKind.LOCAL
    assert loaded.bridges[Host.A].local.port == 8737

    remote = loaded.bridges[Host.B].remote
    assert loaded.bridges[Host.B].kind is BridgeKind.REMOTE
    assert remote.host == "192.168.1.100"
    assert remote.ssh_port == 2222
    assert remote.username == "ubu"
    assert remote.password == "SuperSecret123"
    assert remote.bridge_port == 8738
    assert remote.display_name == "Linux 测试机"


def test_password_never_appears_in_plaintext_on_disk(data_dir: Path):
    """这是本模块最重要的一条断言：落盘文件里搜不到明文密码。"""
    path = data_dir / "config.json"
    config.save(_remote_config(), path)

    text = path.read_text(encoding="utf-8")
    assert "SuperSecret123" not in text
    assert '"password_enc"' in text


def test_missing_file_yields_defaults(data_dir: Path):
    loaded = config.load(data_dir / "does-not-exist.json")
    assert loaded.bridges[Host.A].kind is BridgeKind.LOCAL
    assert loaded.bridges[Host.B].kind is BridgeKind.REMOTE
    # Host B 是固定的远程角色，但没填地址 —— 即「未配置」
    assert loaded.remote_host() is None


# --------------------------------------------------------------------------- #
# 健壮性
# --------------------------------------------------------------------------- #


def test_corrupt_json_falls_back_instead_of_crashing(data_dir: Path):
    path = data_dir / "config.json"
    path.write_text("{ this is not json", encoding="utf-8")

    loaded = config.load(path)
    assert loaded.bridges[Host.A].kind is BridgeKind.LOCAL
    assert loaded.bridges[Host.B].kind is BridgeKind.REMOTE


def test_host_roles_are_pinned_on_load(data_dir: Path):
    """旧配置允许「任意角色选本机 / 远程」，加载时必须收敛到固定拓扑。

    真实事故：用户把 Host A 配成「远程但地址为空」，于是隧道指向空主机、
    桥接状态全乱。A 恒为本机、B 恒为远程 —— 收敛时不能丢掉 B 的连接参数。
    """
    path = data_dir / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "bridges": {
                    "A": {"kind": "remote", "remote": {"host": ""}},
                    "B": {
                        "kind": "local",
                        "local": {"port": 9001},
                        "remote": {"host": "192.168.0.1", "username": "admin"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = config.load(path)

    assert loaded.bridges[Host.A].kind is BridgeKind.LOCAL
    assert loaded.bridges[Host.B].kind is BridgeKind.REMOTE
    assert loaded.bridges[Host.B].remote.host == "192.168.0.1"
    assert loaded.remote_host() is Host.B
    # 本机端口恒取自 Host A，B 上那个 9001 不再有意义
    assert loaded.local_bridge_port == 8737


def test_remote_host_is_none_until_address_is_filled(data_dir: Path):
    """远程「已配置」的唯一判据是填了地址 —— Host B 的安全弹出锁据此开关。"""
    cfg = AppConfig()
    assert cfg.bridges[Host.B].kind is BridgeKind.REMOTE
    assert cfg.remote_host() is None

    cfg.bridges[Host.B].remote.host = "192.168.0.1"
    assert cfg.remote_host() is Host.B

    cfg.bridges[Host.B].remote.host = "   "
    assert cfg.remote_host() is None, "空白地址不能算配置过"


def test_disabled_remote_bridge_counts_as_not_in_use(data_dir: Path):
    """总开关关掉后「远程桥接」整体不参与工作 —— 即使地址填得完整。

    这是本功能的核心语义：禁用**不是**清除配置，而是让远程桥接停止参与
    弹出锁 / 隧道 / 远程操作的判断。所以两个断言必须同时成立 ——
    `remote_host()` 变 None（停用），而配置字段原封不动（没被清）。
    """
    cfg = AppConfig()
    cfg.bridges[Host.B].remote.host = "192.168.0.1"
    assert cfg.remote_host() is Host.B

    cfg.bridges[Host.B].remote.enabled = False

    assert cfg.remote_host() is None, "禁用后 Host B 的安全弹出锁必须一并关闭"
    assert cfg.remote_bridge().host == "192.168.0.1", "禁用不该把配置一起吃掉"
    assert cfg.remote_bridge().is_configured is True


def test_enabled_flag_missing_in_old_config_defaults_to_true(data_dir: Path):
    """老配置里没有 enabled 键 —— 缺省必须是 True。

    若缺省成 False，升级到本版本后所有用户的远程桥接会被**静默停用**：
    不弹盘、隧道不起，而界面上没有任何「我刚关了它」的操作痕迹。
    """
    path = data_dir / "config.json"
    path.write_text(
        json.dumps({"bridges": {"B": {"remote": {"host": "192.168.1.100"}}}}),
        encoding="utf-8",
    )

    loaded = config.load(path)

    assert loaded.remote_bridge().enabled is True
    assert loaded.remote_host() is Host.B


def test_enabled_flag_roundtrips(data_dir: Path):
    path = data_dir / "config.json"
    cfg = _remote_config()
    cfg.bridges[Host.B].remote.enabled = False

    config.save(cfg, path)

    assert config.load(path).remote_bridge().enabled is False
    assert '"enabled": false' in path.read_text(encoding="utf-8")


def test_newer_schema_version_is_rejected(data_dir: Path):
    path = data_dir / "config.json"
    path.write_text(json.dumps({"version": 999}), encoding="utf-8")

    with pytest.raises(ConfigError):
        config.load(path)


def test_unknown_and_missing_fields_are_tolerated(data_dir: Path):
    """向前兼容：老配置缺字段、新配置多字段都不应炸。"""
    path = data_dir / "config.json"
    path.write_text(
        json.dumps({"version": 1, "ble": {"device_name": "Custom"}, "future_flag": 42}),
        encoding="utf-8",
    )

    loaded = config.load(path)
    assert loaded.ble.device_name == "Custom"
    assert loaded.bridges[Host.A].local.port == 8737


def test_save_is_atomic_and_leaves_no_temp_files(data_dir: Path):
    path = data_dir / "config.json"
    config.save(AppConfig(), path)
    config.save(AppConfig(), path)

    leftovers = [p.name for p in data_dir.iterdir() if p.name.startswith(".config-")]
    assert leftovers == []


def test_decrypt_failure_degrades_to_empty_password(data_dir: Path, monkeypatch):
    """凭据损坏时不应阻断启动，而是把密码置空等用户重填。"""
    path = data_dir / "config.json"
    config.save(_remote_config(), path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["bridges"]["B"]["remote"]["password_enc"] = "dpapi:bm90LWEtcmVhbC1ibG9i"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = config.load(path)
    assert loaded.bridges[Host.B].remote.password == ""
    assert loaded.bridges[Host.B].remote.host == "192.168.1.100"


def test_remote_auth_method_roundtrip(data_dir: Path):
    path = data_dir / "config.json"
    cfg = _remote_config()
    cfg.bridges[Host.B].remote.auth = AuthMethod.KEY
    cfg.bridges[Host.B].remote.password = ""
    cfg.bridges[Host.B].remote.key_path = "C:/keys/id_ed25519"

    config.save(cfg, path)
    loaded = config.load(path)

    remote = loaded.bridges[Host.B].remote
    assert remote.auth is AuthMethod.KEY
    assert remote.key_path == "C:/keys/id_ed25519"
# --------------------------------------------------------------------------- #
# SSH 隧道配置
# --------------------------------------------------------------------------- #


def test_tunnel_fields_roundtrip(data_dir: Path):
    """隧道开关与隧道端口必须落盘 —— 否则每次启动都要重新勾一遍。"""
    path = data_dir / "config.json"
    cfg = _remote_config()
    cfg.bridges[Host.B].remote.use_tunnel = False
    cfg.bridges[Host.B].remote.tunnel_local_port = 18738

    config.save(cfg, path)
    loaded = config.load(path)

    remote = loaded.bridges[Host.B].remote
    assert remote.use_tunnel is False
    assert remote.tunnel_local_port == 18738


def test_tunnel_defaults_to_enabled(data_dir: Path):
    """旧配置文件里没有这两个字段时，隧道应当默认开启。

    默认开是有意的：远端防火墙不放通桥接端口是**常态**（实测那台边缘网关
    只放 22/80/443），默认关闭会让第一次使用必然失败一次。
    """
    path = data_dir / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "bridges": {
                    "B": {
                        "kind": "remote",
                        "remote": {
                            "host": "192.168.1.100",
                            "username": "ubu",
                            "bridge_port": 8738,
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = config.load(path)

    remote = loaded.bridges[Host.B].remote
    assert remote.use_tunnel is True
    assert remote.tunnel_local_port == 0


def test_runtime_http_base_is_never_persisted(data_dir: Path):
    """``runtime_http_base`` 是运行期状态，绝不能落盘。

    它是一条**隧道**的地址，隧道只在本次进程里存在。写进配置文件的话，
    下次启动会拿着一个已经不存在的端口去连，症状是「一切看起来正常但连不上」。
    """
    path = data_dir / "config.json"
    cfg = _remote_config()
    cfg.bridges[Host.B].remote.runtime_http_base = "http://127.0.0.1:54321"

    config.save(cfg, path)

    raw = path.read_text(encoding="utf-8")
    assert "54321" not in raw, "运行期隧道地址被写进了配置文件"
    assert "runtime_http_base" not in raw

    loaded = config.load(path)
    assert loaded.bridges[Host.B].remote.runtime_http_base == ""
    # 于是 http_base 回到直连地址
    assert loaded.bridges[Host.B].remote.http_base == "http://192.168.1.100:8738"


def test_http_base_prefers_runtime_override(data_dir: Path):
    """桥接地址只有一个来源：有隧道就用隧道。"""
    cfg = _remote_config()
    remote = cfg.bridges[Host.B].remote

    assert remote.http_base == "http://192.168.1.100:8738"

    remote.runtime_http_base = "http://127.0.0.1:40000"
    assert remote.http_base == "http://127.0.0.1:40000"
    assert remote.direct_http_base == "http://192.168.1.100:8738"


def test_tunnel_local_port_validation(data_dir: Path):
    cfg = _remote_config()
    remote = cfg.bridges[Host.B].remote

    remote.tunnel_local_port = 0  # 自动，合法
    assert not [e for e in remote.validate() if "隧道" in e]

    remote.tunnel_local_port = 70000  # 越界
    assert [e for e in remote.validate() if "隧道" in e]


# --------------------------------------------------------------------------- #
# 落盘安全
# --------------------------------------------------------------------------- #


def test_save_keeps_previous_version_as_backup(data_dir: Path):
    """覆盖前要留一份 .bak —— 配置里有远端主机参数与 DPAPI 密文，写坏一次代价很大。"""
    path = data_dir / "config.json"
    backup = data_dir / "config.json.bak"

    first = AppConfig()
    first.ble.device_name = "第一版"
    config.save(first, path)
    assert not backup.exists(), "首次保存没有旧文件，不该产生备份"

    second = AppConfig()
    second.ble.device_name = "第二版"
    config.save(second, path)

    assert backup.exists(), "覆盖前没有留备份"
    assert json.loads(backup.read_text(encoding="utf-8"))["ble"]["device_name"] == "第一版"
    assert config.load(path).ble.device_name == "第二版"


def test_backup_failure_does_not_block_save(data_dir: Path, monkeypatch):
    """备份失败（磁盘满、权限）不能连累主流程 —— 能存上比有备份重要。"""
    import shutil as _shutil

    path = data_dir / "config.json"
    config.save(AppConfig(), path)

    def boom(*_args, **_kwargs):
        raise OSError("磁盘已满")

    monkeypatch.setattr(_shutil, "copy2", boom)

    config.save(AppConfig(), path)  # 不该抛
    assert config.load(path) is not None
