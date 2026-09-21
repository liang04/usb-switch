"""core.ble 单元测试 —— 匹配规则、状态派发、命令串行与陈旧回复。

不需要真实硬件：用一个假 client 顶替 BleakClient，直接验证协议层的语义。
"""

from __future__ import annotations

import asyncio

import pytest

from usbswitch.core.ble import (
    RX_UUID,
    SERVICE_UUID,
    BleController,
    BleEvents,
    BleState,
    ScanResult,
)
from usbswitch.core.errors import BleError, BleNoResponseError
from usbswitch.core.models import Host

NAME = "USB-Switch"


class FakeDevice:
    def __init__(self, name: str | None = None, address: str = "AA:BB:CC:DD:EE:FF") -> None:
        self.name = name
        self.address = address


class FakeAdv:
    def __init__(self, uuids=(), local_name=None, rssi=-60) -> None:
        self.service_uuids = list(uuids)
        self.local_name = local_name
        self.rssi = rssi


class FakeClient:
    """替身 client。写命令时可按脚本推送一条通知，模拟固件回传。"""

    def __init__(self, controller: BleController, replies: dict[str, str] | None = None) -> None:
        self._controller = controller
        self.is_connected = True
        self.writes: list[tuple[str, bytes]] = []
        self.replies = replies or {}
        self.reply_with: str | None = None

    async def write_gatt_char(self, char, data, response=None) -> None:
        self.writes.append((str(char), bytes(data)))
        if self.reply_with is not None:
            self._controller._inbox.put_nowait(self.reply_with)


def make_controller(**kwargs) -> BleController:
    return BleController(device_name=NAME, **kwargs)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 设备匹配
# --------------------------------------------------------------------------- #


def test_matches_by_service_uuid():
    controller = make_controller()
    assert controller._matches(FakeDevice(None), FakeAdv(uuids=[SERVICE_UUID])) is True


def test_matches_by_device_name():
    controller = make_controller()
    assert controller._matches(FakeDevice(NAME), FakeAdv()) is True


def test_matches_by_local_name_in_advertisement():
    controller = make_controller()
    assert controller._matches(FakeDevice(None), FakeAdv(local_name=NAME)) is True


@pytest.mark.parametrize("name", ["Other-Device", None, ""])
def test_does_not_match_unrelated_device(name):
    controller = make_controller()
    assert controller._matches(FakeDevice(name), FakeAdv(uuids=["1234"])) is False


def test_custom_device_name_is_respected():
    controller = BleController(device_name="My-Switch")
    assert controller._matches(FakeDevice("My-Switch"), FakeAdv()) is True
    assert controller._matches(FakeDevice(NAME), FakeAdv()) is False


# --------------------------------------------------------------------------- #
# 通知派发
# --------------------------------------------------------------------------- #


def test_notify_dispatches_status_and_raw_reply():
    seen = {"states": [], "status": [], "replies": [], "logs": []}
    events = BleEvents(
        on_status=seen["status"].append,
        on_reply=seen["replies"].append,
        on_log=lambda level, msg: seen["logs"].append((level, msg)),
    )
    controller = make_controller(events=events)

    controller._on_notify(None, b"STATUS: host=B, detA=1, detB=0")

    assert seen["status"][0].host is Host.B
    assert seen["replies"] == ["STATUS: host=B, detA=1, detB=0"]
    assert any("设备: " in msg for _, msg in seen["logs"])


def test_notify_without_status_still_forwards_raw_reply():
    seen = {"replies": []}
    controller = make_controller(events=BleEvents(on_reply=seen["replies"].append))

    controller._on_notify(None, b"FAIL: Host A unavailable")

    assert seen["replies"] == ["FAIL: Host A unavailable"]


def test_notify_tolerates_invalid_utf8():
    controller = make_controller()
    controller._on_notify(None, bytearray(b"OK: host=A \xff\xfe"))
    assert controller._inbox.qsize() == 1


# --------------------------------------------------------------------------- #
# 命令下发
# --------------------------------------------------------------------------- #


def test_send_requires_connection():
    controller = make_controller()
    with pytest.raises(BleError):
        run(controller.send("s"))


def test_send_writes_and_returns_reply():
    controller = make_controller()
    client = FakeClient(controller, replies={"s": "STATUS: host=A, detA=1, detB=0"})
    client.reply_with = "STATUS: host=A, detA=1, detB=0"
    controller._client = client

    reply = run(controller.send("s"))

    assert reply == "STATUS: host=A, detA=1, detB=0"
    assert client.writes == [(RX_UUID, b"s")]


def test_send_raises_on_timeout():
    controller = make_controller()
    client = FakeClient(controller)  # 永不回传
    controller._client = client

    with pytest.raises(BleNoResponseError):
        run(controller.send("a", timeout=0.2))


def test_stale_reply_is_not_mistaken_for_current_result():
    """回归测试：上一次的积压回复不得被当成本次结果。

    这是请求-应答协议最容易出的错 —— 表现为「界面显示切换成功，实际没生效」。
    """
    controller = make_controller()
    client = FakeClient(controller)  # 本次不回传
    controller._client = client
    controller._inbox.put_nowait("OK: host=A, detA=1, detB=0")  # 上一次的残留

    with pytest.raises(BleNoResponseError):
        run(controller.send("b", timeout=0.2))


def test_send_is_serialized_across_tasks():
    """并发下发必须排队 —— 否则会交叉写同一个 GATT 特征，回复与命令对不上。"""

    class ConcurrencyProbe(FakeClient):
        def __init__(self, controller: BleController) -> None:
            super().__init__(controller)
            self.active = 0
            self.max_active = 0

        async def write_gatt_char(self, char, data, response=None) -> None:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)  # 放大竞态窗口
                self.writes.append((str(char), bytes(data)))
                self._controller._inbox.put_nowait("OK: host=A")
            finally:
                self.active -= 1

    async def scenario() -> tuple[int, int]:
        controller = make_controller()
        probe = ConcurrencyProbe(controller)
        controller._client = probe

        await asyncio.gather(*(controller.send("a") for _ in range(5)))
        return probe.max_active, len(probe.writes)

    max_concurrent, write_count = asyncio.run(scenario())
    assert max_concurrent == 1, f"检测到 {max_concurrent} 个并发 GATT 写"
    assert write_count == 5


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #


def test_state_change_emits_event():
    seen: list = []
    controller = make_controller(events=BleEvents(on_state=seen.append))

    controller._set_state(BleState.CONNECTING)

    assert seen == [BleState.CONNECTING]


def test_repeated_same_state_emits_once():
    seen: list = []
    controller = make_controller(events=BleEvents(on_state=seen.append))

    controller._set_state(BleState.CONNECTING)
    controller._set_state(BleState.CONNECTING)

    assert seen == [BleState.CONNECTING]


def test_disconnect_clears_client_and_signals():
    controller = make_controller()
    controller._client = FakeClient(controller)

    controller._on_disconnected(None)

    assert controller._client is None
    assert controller.is_connected is False
    assert controller._disconnected.is_set()


def test_scan_result_ordering():
    items = [ScanResult("b", "2"), ScanResult("a", "1")]
    assert sorted(items, key=lambda r: r.name) == [ScanResult("a", "1"), ScanResult("b", "2")]
