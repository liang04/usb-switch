"""core.models 单元测试 —— 固件报文解析与配置校验。"""

from __future__ import annotations

import pytest

from usbswitch.core.models import (
    AppConfig,
    AuthMethod,
    DeviceStatus,
    Host,
    RemoteBridgeConfig,
    ReplyKind,
    classify_reply,
)


# --------------------------------------------------------------------------- #
# 固件报文解析
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "host", "det_a", "det_b"),
    [
        ("STATUS: host=A, detA=1, detB=0", Host.A, True, False),
        ("READY: host=B, detA=1, detB=1", Host.B, True, True),
        ("host=none, detA=0, detB=0", Host.NONE, False, False),
        ("OK: host=none (USB disconnected)", Host.NONE, False, False),
    ],
)
def test_parse_status(text: str, host: Host, det_a: bool, det_b: bool):
    status = DeviceStatus.parse(text)
    assert status is not None
    assert status.host is host
    assert status.det_a is det_a
    assert status.det_b is det_b
    assert status.raw == text


@pytest.mark.parametrize("text", ["FAIL: Host A unavailable", "", "garbage"])
def test_parse_returns_none_without_host_field(text: str):
    assert DeviceStatus.parse(text) is None


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("OK: host=A", ReplyKind.OK),
        ("FAIL: Host B unavailable", ReplyKind.FAIL),
        ("STATUS: host=A, detA=1, detB=0", ReplyKind.STATUS),
        ("READY: host=A, detA=1, detB=0", ReplyKind.READY),
        ("???", ReplyKind.UNKNOWN),
    ],
)
def test_classify_reply(text: str, kind: ReplyKind):
    assert classify_reply(text) is kind


# --------------------------------------------------------------------------- #
# 远程配置校验
# --------------------------------------------------------------------------- #


def _valid_remote() -> RemoteBridgeConfig:
    return RemoteBridgeConfig(
        host="192.168.1.100",
        ssh_port=22,
        username="ubu",
        auth=AuthMethod.PASSWORD,
        password="secret",
        bridge_port=8738,
    )


def test_valid_remote_config_has_no_errors():
    assert _valid_remote().validate() == []


def test_missing_host_and_username_are_reported():
    cfg = RemoteBridgeConfig(auth=AuthMethod.PASSWORD, password="x")
    errors = cfg.validate()
    assert any("IP" in e for e in errors)
    assert any("用户名" in e for e in errors)


def test_password_auth_requires_password():
    cfg = _valid_remote()
    cfg.password = ""
    assert any("密码" in e for e in cfg.validate())


def test_key_auth_requires_key_path():
    cfg = _valid_remote()
    cfg.auth = AuthMethod.KEY
    cfg.password = ""
    assert any("私钥" in e for e in cfg.validate())

    cfg.key_path = "C:/keys/id_ed25519"
    assert cfg.validate() == []


@pytest.mark.parametrize("port", [0, -1, 70000])
def test_out_of_range_ports_are_reported(port: int):
    cfg = _valid_remote()
    cfg.ssh_port = port
    assert any("SSH 端口" in e for e in cfg.validate())

    cfg = _valid_remote()
    cfg.bridge_port = port
    assert any("桥接端口" in e for e in cfg.validate())


def test_ssh_and_bridge_ports_are_independent():
    """两个端口必须各管各的，绝不能互相干扰。"""
    cfg = _valid_remote()
    cfg.ssh_port = 2222
    cfg.bridge_port = 9000
    assert cfg.validate() == []
    assert cfg.ssh_target == "ubu@192.168.1.100:2222"
    assert cfg.http_base == "http://192.168.1.100:9000"


# --------------------------------------------------------------------------- #
# 连接地址：勾了隧道就不退直连（2026-09-19）
# --------------------------------------------------------------------------- #


def test_connect_base_is_none_while_the_tunnel_is_not_ready():
    """勾了隧道、隧道还没通 → **没有**可用地址，而不是回落到直连。

    回落会把「隧道没建起来」这个真实原因换成「连不上远端主机」，用户于是去
    排查远端服务 —— 现场 2026-09-19 就是这么被带偏的。
    """
    cfg = _valid_remote()
    assert cfg.use_tunnel is True
    assert cfg.connect_base() is None
    # 展示用地址仍然给直连（界面上要说明「关掉隧道会连这里」）
    assert cfg.http_base == "http://192.168.1.100:8738"


def test_connect_base_returns_the_tunnel_address_once_ready():
    cfg = _valid_remote()
    cfg.runtime_http_base = "http://127.0.0.1:54321"
    assert cfg.connect_base() == "http://127.0.0.1:54321"


def test_connect_base_is_direct_when_tunnel_is_off():
    """没勾隧道时直连就是唯一的正路，行为与从前一致。"""
    cfg = _valid_remote()
    cfg.use_tunnel = False
    assert cfg.connect_base() == "http://192.168.1.100:8738"


def test_endpoint_connect_base_follows_the_role():
    """Host A 恒为本机回环，与隧道无关；Host B 才会出现「暂时没有地址」。"""
    config = AppConfig()
    config.bridges[Host.B].remote.host = "192.168.1.100"

    assert config.endpoint_for(Host.A).connect_base() == "http://127.0.0.1:8737"
    assert config.endpoint_for(Host.B).connect_base() is None

    config.bridges[Host.B].remote.use_tunnel = False
    assert config.endpoint_for(Host.B).connect_base() == "http://192.168.1.100:8738"
