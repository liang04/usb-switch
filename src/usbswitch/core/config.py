"""配置读写与凭据编解码。

配置模型以 **Host 角色** 为中心（物理上只有 Host A / Host B 两路），
而不是维护一个扁平的远程主机列表 —— 后者会让「这一步该调哪台桥接」变得模糊。

凭据（SSH 密码 / 私钥口令）在内存里是明文，落盘前一律经 :mod:`crypto`
用 Windows DPAPI 加密，`config.json` 中搜不到明文密码。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from . import crypto
from .errors import ConfigError
from .models import (
    DEFAULT_LOCAL_BRIDGE_PORT,
    DEFAULT_REMOTE_BRIDGE_PORT,
    DEFAULT_SSH_PORT,
    AppConfig,
    AuthMethod,
    AutostartConfig,
    BleConfig,
    BridgeEndpoint,
    BridgeKind,
    Host,
    LocalBridgeConfig,
    RemoteBridgeConfig,
    WindowConfig,
)
from .paths import config_file

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #


def _remote_to_dict(rc: RemoteBridgeConfig) -> dict[str, Any]:
    # 刻意逐字段列出，而不是 asdict()：`runtime_http_base` 是运行期状态，
    # 必须**不落盘**，逐字段列出天然把它挡在外面。
    return {
        "enabled": rc.enabled,
        "host": rc.host,
        "ssh_port": rc.ssh_port,
        "username": rc.username,
        "auth": rc.auth.value,
        "password_enc": crypto.protect(rc.password),
        "key_path": rc.key_path,
        "passphrase_enc": crypto.protect(rc.passphrase),
        "bridge_port": rc.bridge_port,
        "display_name": rc.display_name,
        "use_tunnel": rc.use_tunnel,
        "tunnel_local_port": rc.tunnel_local_port,
    }


def _remote_from_dict(data: dict[str, Any]) -> RemoteBridgeConfig:
    def _decrypt(field: str) -> str:
        token = data.get(field) or ""
        if not token:
            return ""
        try:
            return crypto.unprotect(token)
        except (OSError, RuntimeError, ValueError) as exc:
            # 降级为空并告警，不阻断启动 —— 用户重新输一次密码即可。
            log.warning("凭据解密失败（%s），已置空等待重新输入: %s", field, exc)
            return ""

    return RemoteBridgeConfig(
        # 缺省 True：老配置里没有这个键，语义是「配了就用」，与新增开关前一致
        enabled=bool(data.get("enabled", True)),
        host=str(data.get("host", "")),
        ssh_port=int(data.get("ssh_port", DEFAULT_SSH_PORT)),
        username=str(data.get("username", "")),
        auth=AuthMethod(data.get("auth", AuthMethod.PASSWORD.value)),
        password=_decrypt("password_enc"),
        key_path=str(data.get("key_path", "")),
        passphrase=_decrypt("passphrase_enc"),
        bridge_port=int(data.get("bridge_port", DEFAULT_REMOTE_BRIDGE_PORT)),
        display_name=str(data.get("display_name", "")),
        use_tunnel=bool(data.get("use_tunnel", True)),
        tunnel_local_port=int(data.get("tunnel_local_port", 0)),
    )


def _expanded_sections(raw: Any) -> dict[str, bool]:
    """折叠区开合状态。非 dict 或值不合法时一律忽略，绝不因此拒绝整个配置。"""
    if not isinstance(raw, dict):
        return {}
    return {str(key): bool(value) for key, value in raw.items()}


#: 界面外观的合法取值。**必须与 ``ui.theme.MODES`` 一致** ——
#: 但 core 不许 import Qt，所以这里独立声明一份并靠测试钉住两者相等
#: （``test_theme.py::test_modes_agree_with_config``）。
THEME_MODES: tuple[str, ...] = ("auto", "light", "dark")


def _theme_mode(raw: Any) -> str:
    """界面外观。非法值（改坏的配置文件、旧版本写的）一律回落 ``light``。

    这里**不抛异常**：一个拼错的字符串不该让用户连界面都打不开。
    """
    text = str(raw or "").strip().lower()
    return text if text in THEME_MODES else "light"


def _endpoint_to_dict(ep: BridgeEndpoint) -> dict[str, Any]:
    return {
        "kind": ep.kind.value,
        "local": {"port": ep.local.port},
        "remote": _remote_to_dict(ep.remote),
    }


def _endpoint_from_dict(data: dict[str, Any]) -> BridgeEndpoint:
    return BridgeEndpoint(
        kind=BridgeKind(data.get("kind", BridgeKind.LOCAL.value)),
        local=LocalBridgeConfig(
            port=int(data.get("local", {}).get("port", DEFAULT_LOCAL_BRIDGE_PORT))
        ),
        remote=_remote_from_dict(data.get("remote", {})),
    )


def to_dict(config: AppConfig) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "ble": {
            "device_name": config.ble.device_name,
            "auto_connect": config.ble.auto_connect,
        },
        "window": {
            "close_to_tray": config.window.close_to_tray,
            "geometry": config.window.geometry,
            "tray_hint_shown": config.window.tray_hint_shown,
            "theme": config.window.theme,
            "expanded_sections": {
                str(key): bool(value)
                for key, value in config.window.expanded_sections.items()
            },
        },
        "autostart": {
            "enabled": config.autostart.enabled,
            "start_minimized": config.autostart.start_minimized,
        },
        "bridges": {host.value: _endpoint_to_dict(ep) for host, ep in config.bridges.items()},
    }


def from_dict(data: dict[str, Any]) -> AppConfig:
    """从 dict 构造配置。任何字段缺失都回落到默认值，保证向前/向后兼容。"""
    version = int(data.get("version", SCHEMA_VERSION))
    if version > SCHEMA_VERSION:
        raise ConfigError(
            f"配置文件版本 {version} 高于本程序支持的 {SCHEMA_VERSION}",
            hint="请升级 USB Switch Console，或删除配置文件重新生成",
        )

    ble = data.get("ble", {})
    window = data.get("window", {})
    autostart = data.get("autostart", {})
    bridges_raw = data.get("bridges", {})

    bridges = {
        host: _endpoint_from_dict(bridges_raw.get(host.value, {})) for host in (Host.A, Host.B)
    }
    # 角色钉死：Host A 恒为本机、Host B 恒为远程（Linux）。
    # 旧版本允许自选角色，曾把 Host A 配成「远程但地址为空」—— 那会拖垮
    # 隧道与桥接状态。加载时收敛，兼容旧配置文件。
    bridges[Host.A].kind = BridgeKind.LOCAL
    bridges[Host.B].kind = BridgeKind.REMOTE

    return AppConfig(
        ble=BleConfig(
            device_name=str(ble.get("device_name", "USB-Switch")),
            auto_connect=bool(ble.get("auto_connect", False)),
        ),
        window=WindowConfig(
            close_to_tray=bool(window.get("close_to_tray", True)),
            geometry=str(window.get("geometry", "")),
            tray_hint_shown=bool(window.get("tray_hint_shown", False)),
            expanded_sections=_expanded_sections(window.get("expanded_sections")),
            theme=_theme_mode(window.get("theme")),
        ),
        autostart=AutostartConfig(
            enabled=bool(autostart.get("enabled", False)),
            start_minimized=bool(autostart.get("start_minimized", True)),
        ),
        bridges=bridges,
    )


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #


def load(path: Path | None = None) -> AppConfig:
    """读取配置。文件不存在或损坏时返回默认配置，绝不因此崩溃。"""
    target = path or config_file()
    if not target.exists():
        return AppConfig()

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("配置文件无法解析（%s），已回退到默认配置: %s", target, exc)
        return AppConfig()

    try:
        return from_dict(raw)
    except ConfigError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("配置文件字段异常（%s），已回退到默认配置: %s", target, exc)
        return AppConfig()


def save(config: AppConfig, path: Path | None = None) -> None:
    """原子写入配置。

    临时文件 + ``os.replace`` —— 直接覆写的话，断电或崩溃会留下一个
    半截的 JSON，下次启动配置全丢。

    覆盖前还会留一份 ``config.json.bak``。这份配置里存着一整台远端主机的
    连接参数与 DPAPI 密文，被无声写坏一次就得全部重填；一份备份的成本
    可以忽略，而它换来的是「任何时候都找得回来」。
    """
    target = path or config_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(to_dict(config), ensure_ascii=False, indent=2) + "\n"

    if target.exists():
        try:
            shutil.copy2(target, target.with_name(target.name + ".bak"))
        except OSError as exc:
            # 备份失败不阻断保存 —— 主流程能用比留备份重要
            log.warning("配置备份失败（不影响本次保存）: %s", exc)

    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
