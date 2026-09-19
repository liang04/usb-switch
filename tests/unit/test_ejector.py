"""core.ejector 单元测试 —— 按 Host 角色路由与失败处理。

这是安全互锁的执行端：它抛 EjectError 时编排层必须硬中止切换，
所以失败路径的每一条都要覆盖。
"""

from __future__ import annotations

import pytest

from usbswitch.core import http_json
from usbswitch.core.ejector import BridgeEjector
from usbswitch.core.errors import BridgeUnreachableError, EjectError
from usbswitch.core.models import AppConfig, Host


def _config_with_remote(host: Host = Host.B) -> AppConfig:
    """Host B 恒为远程角色，填地址即可（不再需要切换 kind）。"""
    config = AppConfig()
    endpoint = config.bridges[host]
    endpoint.remote.host = "192.168.1.100"
    endpoint.remote.bridge_port = 8738
    return config


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #


def test_local_endpoint_uses_loopback():
    """本机角色恒为 Host A，地址是回环。"""
    ejector = BridgeEjector(AppConfig())
    assert ejector.endpoint_for(Host.A) == "http://127.0.0.1:8737"


def test_remote_endpoint_uses_configured_host_and_bridge_port():
    """注意用的是 bridge_port 而不是 ssh_port —— 两者完全不同。"""
    config = _config_with_remote()
    config.bridges[Host.B].remote.ssh_port = 2222

    ejector = BridgeEjector(config)
    assert ejector.endpoint_for(Host.B) == "http://192.168.1.100:8738"


def test_unknown_host_is_rejected():
    config = AppConfig()
    config.bridges.pop(Host.B)

    with pytest.raises(EjectError):
        BridgeEjector(config).endpoint_for(Host.B)


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #


def test_success_with_ejected_field(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(http_json, "post_json", lambda url, timeout: {"ok": True, "ejected": "F"})
    BridgeEjector(AppConfig()).eject_for(Host.A)  # 不抛异常即通过


def test_warning_response_counts_as_success(monkeypatch: pytest.MonkeyPatch):
    """本机看不到 U 盘 → ok + warning。绝不能当成失败，否则接在另一台
    Host 上的盘会让切换被无谓拦下。"""
    monkeypatch.setattr(
        http_json, "post_json",
        lambda url, timeout: {"ok": True, "warning": "本机未检测到 U 盘"},
    )
    BridgeEjector(AppConfig()).eject_for(Host.A)


def test_request_goes_to_the_right_endpoint(monkeypatch: pytest.MonkeyPatch):
    seen: list[str] = []

    def fake_post(url: str, timeout: float) -> dict:
        seen.append(url)
        return {"ok": True, "ejected": "F"}

    monkeypatch.setattr(http_json, "post_json", fake_post)

    ejector = BridgeEjector(_config_with_remote(Host.B))
    ejector.eject_for(Host.A)
    ejector.eject_for(Host.B)

    assert seen == ["http://127.0.0.1:8737/eject", "http://192.168.1.100:8738/eject"]


# --------------------------------------------------------------------------- #
# 失败路径
# --------------------------------------------------------------------------- #


def test_ok_false_raises_with_service_message(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        http_json, "post_json",
        lambda url, timeout: {"ok": False, "error": "无法卸载卷 F:（Win32 错误 5）"},
    )

    with pytest.raises(EjectError) as excinfo:
        BridgeEjector(AppConfig()).eject_for(Host.A)

    assert "Win32 错误 5" in excinfo.value.message
    assert "VBUS 保持不变" in excinfo.value.hint


def test_unreachable_bridge_raises_eject_error(monkeypatch: pytest.MonkeyPatch):
    def unreachable(url: str, timeout: float) -> dict:
        raise BridgeUnreachableError("无法连接桥接服务")

    monkeypatch.setattr(http_json, "post_json", unreachable)

    with pytest.raises(EjectError) as excinfo:
        BridgeEjector(AppConfig()).eject_for(Host.A)

    assert "未启动" in excinfo.value.hint or "桥接服务" in excinfo.value.hint


def test_missing_ok_field_is_treated_as_failure(monkeypatch: pytest.MonkeyPatch):
    """响应里没有 ok 字段时不能算成功 —— 宁可拦下也不要赌。"""
    monkeypatch.setattr(http_json, "post_json", lambda url, timeout: {"ejected": "F"})

    with pytest.raises(EjectError):
        BridgeEjector(AppConfig()).eject_for(Host.A)
