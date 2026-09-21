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


def _config_with_remote(host: Host = Host.B, *, use_tunnel: bool = False) -> AppConfig:
    """Host B 恒为远程角色，填地址即可（不再需要切换 kind）。

    默认 ``use_tunnel=False``：本文件多数用例关心的是「地址怎么拼」，
    走直连才拿得到那个地址（勾了隧道就要等隧道就绪，见文件末尾的隧道用例）。
    """
    config = AppConfig()
    endpoint = config.bridges[host]
    endpoint.remote.host = "192.168.1.100"
    endpoint.remote.bridge_port = 8738
    endpoint.remote.use_tunnel = use_tunnel
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


# --------------------------------------------------------------------------- #
# 勾了隧道就不退直连（2026-09-19）
# --------------------------------------------------------------------------- #


def test_tunnel_configured_but_not_ready_refuses_to_eject(monkeypatch: pytest.MonkeyPatch):
    """隧道没通时必须**如实报错**，不能偷偷去连远端主机自己的 8738。

    退回直连的代价是错误信息变成「连不上主机」，用户会去排查「桥接服务有没有
    启动」，而真正的原因是隧道没建起来 —— 2026-09-19 的现场就是这样被带偏的。
    """
    calls: list[str] = []

    def fake_post(url: str, timeout: float) -> dict:
        calls.append(url)
        return {"ok": True, "ejected": "F"}

    monkeypatch.setattr(http_json, "post_json", fake_post)

    ejector = BridgeEjector(_config_with_remote(use_tunnel=True))
    with pytest.raises(EjectError) as excinfo:
        ejector.eject_for(Host.B)

    assert calls == [], "隧道没通却发了直连请求"
    assert "SSH 隧道尚未建立" in excinfo.value.message
    assert "192.168.1.100" not in excinfo.value.message, "报错里不该出现会被误读的直连地址"


def test_tunnel_ready_uses_the_tunnel_address(monkeypatch: pytest.MonkeyPatch):
    """隧道就绪后，弹出请求走隧道地址而不是远端地址。"""
    calls: list[str] = []
    monkeypatch.setattr(
        http_json, "post_json",
        lambda url, timeout: (calls.append(url), {"ok": True, "ejected": "F"})[1],
    )

    config = _config_with_remote(use_tunnel=True)
    config.bridges[Host.B].remote.runtime_http_base = "http://127.0.0.1:54321"

    BridgeEjector(config).eject_for(Host.B)

    assert calls == ["http://127.0.0.1:54321/eject"]


def test_tunnel_down_hint_names_the_way_out():
    """「弹出锁要弹却弹不了」会让用户卡在切不走上，提示必须给出出口。

    这条断言守的是可用性：报错再准确，如果没说「怎么才能切走」，用户就只能
    卡在那里。
    """
    ejector = BridgeEjector(_config_with_remote(use_tunnel=True))
    with pytest.raises(EjectError) as excinfo:
        ejector.endpoint_for(Host.B)

    hint = excinfo.value.hint
    assert "隧道建立失败" in hint
    assert "启用远程桥接" in hint and "取消勾选" in hint


def test_no_tunnel_configured_still_uses_direct_address():
    """没勾隧道就是直连，行为与从前一致。"""
    ejector = BridgeEjector(_config_with_remote(use_tunnel=False))
    assert ejector.endpoint_for(Host.B) == "http://192.168.1.100:8738"


def test_unreachable_remote_hint_mentions_the_switch(monkeypatch: pytest.MonkeyPatch):
    """直连也没连上时，提示同样要给出口（否则用户还是只能卡着）。"""

    def unreachable(url: str, timeout: float) -> dict:
        raise BridgeUnreachableError("无法连接桥接服务")

    monkeypatch.setattr(http_json, "post_json", unreachable)

    with pytest.raises(EjectError) as excinfo:
        BridgeEjector(_config_with_remote(use_tunnel=False)).eject_for(Host.B)

    assert "启用远程桥接" in excinfo.value.hint
