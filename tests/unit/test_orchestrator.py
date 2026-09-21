"""core.orchestrator 单元测试 —— 切换编排与安全弹出锁。

这一层承载了两条最关键的安全约束，必须有测试兜住：
1. 安全弹出失败必须**硬中止**，绝不能「先切了再说」；
2. 目标等于当前主机时不发命令（减少 VBUS 通断次数）。

第三条是后来的改动：安全弹出不再是一个全局开关，而是**按主机**的派生锁
（Host A 跟随本机桥接服务、Host B 跟随远程桥接配置）。锁未启用的主机跳过
弹出并告警，但**不中止**切换 —— 判据由上层注入，编排层只负责执行。
"""

from __future__ import annotations

import asyncio

import pytest

from usbswitch.core.errors import BleCommandRejectedError, BleError, EjectError
from usbswitch.core.models import AppConfig, DeviceStatus, Host
from usbswitch.core.orchestrator import SwitchOrchestrator


class FakeBle:
    def __init__(self, replies: dict[str, str] | None = None) -> None:
        self.sent: list[str] = []
        self.connected = True
        self.replies = replies or {}
        self.default = "OK: host=A"

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def send(self, command: str, *, timeout: float = 3.0) -> str:
        self.sent.append(command)
        return self.replies.get(command, self.default)


class RecordingEjector:
    def __init__(self, fail_on: Host | None = None) -> None:
        self.calls: list[Host] = []
        self.fail_on = fail_on

    def eject_for(self, host: Host) -> None:
        self.calls.append(host)
        if host is self.fail_on:
            raise EjectError(f"无法弹出 {host.label}")


def build(*, replies=None, fail_on=None, eject_lock=None, log=None):
    config = AppConfig()
    ble = FakeBle(replies)
    ejector = RecordingEjector(fail_on)
    orch = SwitchOrchestrator(
        ble=ble, ejector=ejector, config=config, on_log=log, eject_lock=eject_lock
    )
    return orch, ble, ejector


def set_host(orch: SwitchOrchestrator, host: Host) -> None:
    orch.apply_status(DeviceStatus(host=host, det_a=True, det_b=True))


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


def test_first_switch_ejects_both_hosts_when_state_unknown():
    """刚启动时状态未知，必须对两端都尝试弹出，不能想当然跳过。"""
    orch, ble, ejector = build()

    result = run(orch.switch_to(Host.A))

    assert ejector.calls == [Host.A, Host.B]
    assert ble.sent == ["a"]
    assert result.host is Host.A
    assert result.skipped is False


def test_switch_ejects_only_current_host():
    orch, ble, ejector = build()
    set_host(orch, Host.A)

    run(orch.switch_to(Host.B))

    assert ejector.calls == [Host.A]
    assert ble.sent == ["b"]


def test_switch_tracks_host_from_device_reply():
    orch, ble, _ = build(replies={"b": "OK: host=B"})

    result = run(orch.switch_to(Host.B))

    assert orch.current_host is Host.B
    assert result.host is Host.B


def test_disconnect_does_not_eject_when_already_none():
    """host=none 表示两路 VBUS 都断开，U 盘不可能被挂载，无需弹出。"""
    orch, ble, ejector = build(replies={"a": "OK: host=A"})
    set_host(orch, Host.NONE)

    run(orch.switch_to(Host.A))

    assert ejector.calls == []
    assert ble.sent == ["a"]


# --------------------------------------------------------------------------- #
# 安全弹出锁（按主机）
# --------------------------------------------------------------------------- #


def test_eject_failure_aborts_switch_without_touching_vbus():
    """本模块最重要的一条断言：弹出失败不得下发任何切换命令。"""
    orch, ble, ejector = build(fail_on=Host.A)
    set_host(orch, Host.A)

    with pytest.raises(EjectError):
        run(orch.switch_to(Host.B))

    assert ble.sent == [], "弹出失败后仍然下发了 BLE 命令，VBUS 会被错误切换"
    assert orch.current_host is Host.A


def test_unlocked_host_is_skipped_but_switch_proceeds():
    """锁未启用的主机跳过弹出，切换照常执行 —— 那是用户明确选择的运行状态。"""
    warnings: list[tuple[str, str]] = []
    orch, ble, ejector = build(
        eject_lock=lambda host: host is not Host.A,
        log=lambda level, message: warnings.append((level, message)),
    )
    set_host(orch, Host.A)

    run(orch.switch_to(Host.B))

    assert ejector.calls == []
    assert ble.sent == ["b"]
    assert any("Host A" in message for _level, message in warnings), "跳过弹出必须告警"


def test_lock_is_evaluated_per_host_when_state_unknown():
    """状态未知时两端都会尝试弹出，但只弹锁启用的那一台。"""
    orch, ble, ejector = build(eject_lock=lambda host: host is Host.B)

    run(orch.switch_to(Host.A))

    assert ejector.calls == [Host.B]


def test_unlocked_host_does_not_raise_when_eject_would_fail():
    """锁未启用时，即使那台主机的弹出注定失败也不该拦下切换。

    这正是「锁跟随桥接服务启停」的目的：服务没起，弹出必然失败，
    若仍硬中止，用户就再也切不动了。
    """
    orch, ble, _ = build(fail_on=Host.A, eject_lock=lambda host: host is not Host.A)
    set_host(orch, Host.A)

    run(orch.switch_to(Host.B))

    assert ble.sent == ["b"]


def test_unknown_state_eject_failure_also_aborts():
    orch, ble, ejector = build(fail_on=Host.B)

    with pytest.raises(EjectError):
        run(orch.switch_to(Host.A))

    assert ble.sent == []


# --------------------------------------------------------------------------- #
# 真实弹出器接进编排层：勾了隧道但隧道没通
# --------------------------------------------------------------------------- #


def test_real_ejector_aborts_when_tunnel_is_down(monkeypatch: pytest.MonkeyPatch):
    """现场复现（2026-09-19）：U 盘在 Host B 上，隧道没建起来 → 切走被硬中止。

    与上面那些用例的区别是这里用**真的** ``BridgeEjector``：假弹出器不会暴露
    「地址悄悄回落到直连、于是报错指向远端主机」这个信息层面的坑 —— 而那正是
    当时把排查带偏的原因。
    """
    from usbswitch.core import http_json
    from usbswitch.core.ejector import BridgeEjector

    config = AppConfig()
    remote = config.bridges[Host.B].remote
    remote.host = "192.168.0.1"
    remote.username = "admin"
    remote.password = "p"
    # use_tunnel 默认 True；runtime_http_base 为空 = 隧道没建起来
    posted: list[str] = []
    monkeypatch.setattr(
        http_json, "post_json",
        lambda url, timeout: (posted.append(url), {"ok": True})[1],
    )

    ble = FakeBle()
    orch = SwitchOrchestrator(
        ble=ble, ejector=BridgeEjector(config), config=config
    )
    set_host(orch, Host.B)

    with pytest.raises(EjectError) as excinfo:
        run(orch.switch_to(Host.A))

    assert ble.sent == [], "弹出失败必须硬中止，不能下发切换命令"
    assert posted == [], "隧道没通却发了直连请求"
    assert "SSH 隧道尚未建立" in excinfo.value.message


def test_real_ejector_lets_the_switch_through_when_the_lock_is_off(
    monkeypatch: pytest.MonkeyPatch
):
    """同一场景，只是主机 B 的弹出锁未启用（用户关掉了远程桥接）→ 照常切走。

    两条用例成对，锁住的核心差异就是「锁开不开」，从而保证「卡死」只发生在
    用户确实要求弹盘的那一侧。
    """
    from usbswitch.core.ejector import BridgeEjector

    config = AppConfig()
    remote = config.bridges[Host.B].remote
    remote.host = "192.168.0.1"
    remote.enabled = False

    ble = FakeBle()
    orch = SwitchOrchestrator(
        ble=ble,
        ejector=BridgeEjector(config),
        config=config,
        eject_lock=lambda host: host is not Host.B,
    )
    set_host(orch, Host.B)

    run(orch.switch_to(Host.A))

    assert ble.sent == ["a"]


# --------------------------------------------------------------------------- #
# 幂等与前置校验
# --------------------------------------------------------------------------- #


def test_switching_to_current_host_is_a_noop():
    orch, ble, ejector = build()
    set_host(orch, Host.B)

    result = run(orch.switch_to(Host.B))

    assert result.skipped is True
    assert ble.sent == []
    assert ejector.calls == [], "幂等短路时不应触发任何弹出"


def test_requires_ble_connection():
    orch, ble, ejector = build()
    ble.connected = False

    with pytest.raises(BleError):
        run(orch.switch_to(Host.A))

    assert ejector.calls == [], "蓝牙都没连上，不应该先去弹盘"
    assert ble.sent == []


def test_fail_reply_raises_rejected_error():
    orch, ble, _ = build(replies={"b": "FAIL: Host B unavailable"})
    set_host(orch, Host.A)

    with pytest.raises(BleCommandRejectedError):
        run(orch.switch_to(Host.B))


def test_refresh_status_parses_reply():
    orch, ble, _ = build(replies={"s": "STATUS: host=B, detA=1, detB=0"})

    status = run(orch.refresh_status())

    assert status.host is Host.B
    assert status.det_a is True
    assert status.det_b is False
    assert orch.current_host is Host.B
