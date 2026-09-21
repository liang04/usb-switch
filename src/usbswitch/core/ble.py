"""BLE 协议层 —— 纯 asyncio，不含任何 Qt 依赖。

与固件的 GATT 契约（沿用现有实现，不改协议）：

===========  ========================================  ==================
角色         UUID                                      方向
===========  ========================================  ==================
Service      6e400001-b5a3-f393-e0a9-e50e24dcca9e      —
RX（写）     6e400002-b5a3-f393-e0a9-e50e24dcca9e      电脑 → 设备
TX（通知）   6e400003-b5a3-f393-e0a9-e50e24dcca9e      设备 → 电脑
===========  ========================================  ==================

命令：``a`` / ``b`` / ``x``（断开） / ``s``（查询状态）。

**本模块不 import 任何 Qt 符号**，UI 侧的适配见 ``usbswitch.workers.ble_worker``。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError as BleakRuntimeError

from .errors import (
    BleDeviceNotFoundError,
    BleError,
    BleNoResponseError,
    BleServiceDiscoveryError,
)
from .models import DeviceStatus

log = logging.getLogger(__name__)

SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

DEFAULT_DEVICE_NAME = "USB-Switch"

SCAN_TIMEOUT = 15.0
CONNECT_TIMEOUT = 20.0
#: 固件 switchTo() 最坏约 1.5 s，留出余量
REPLY_TIMEOUT = 3.0

_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 30.0


class BleState(str, Enum):
    IDLE = "idle"
    SCANNING = "scanning"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ScanResult:
    name: str
    address: str
    rssi: int | None = None


@dataclass
class BleEvents:
    """对外通知回调。全部可选，默认丢弃。"""

    on_state: Callable[[BleState], None] | None = None
    on_status: Callable[[DeviceStatus], None] | None = None
    on_reply: Callable[[str], None] | None = None
    on_log: Callable[[str, str], None] | None = None


def _client_kwargs() -> dict:
    """Windows 平台禁用 GATT 服务缓存。

    缓存损坏是本项目的已知故障模式（README 有记录）。禁用后每次连接都重新
    做服务发现，代价是连接略慢，换来的是不会再莫名其妙地「服务发现失败」。
    """
    if sys.platform == "win32":
        return {"winrt": {"use_cached_services": False}}
    return {}


class BleController:
    """USB-Switch 的 BLE 客户端。

    ``run()`` 是一个常驻协程：连接 → 守连接 → 断开 → 指数退避重连，直到
    ``stop()``。命令下发走 ``send()``，由内部锁保证严格串行。
    """

    def __init__(
        self,
        *,
        device_name: str = DEFAULT_DEVICE_NAME,
        events: BleEvents | None = None,
    ) -> None:
        self._device_name = device_name
        self._events = events or BleEvents()
        self._client: BleakClient | None = None
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._state = BleState.IDLE
        self._stop_event = asyncio.Event()
        self._disconnected = asyncio.Event()

    # -- 只读状态 ----------------------------------------------------------- #

    @property
    def state(self) -> BleState:
        return self._state

    @property
    def is_connected(self) -> bool:
        client = self._client
        return client is not None and client.is_connected

    @property
    def device_name(self) -> str:
        return self._device_name

    # -- 生命周期 ----------------------------------------------------------- #

    async def run(self) -> None:
        """常驻协程。``stop()`` 被调用或任务被取消时返回。"""
        backoff = _BACKOFF_MIN
        while not self._stop_event.is_set():
            try:
                await self._connect_once()
                backoff = _BACKOFF_MIN
                self._set_state(BleState.CONNECTED)
                self._emit_log("info", f"已连接 {self._device_name}")
                await self._hold_connection()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 任何异常都不该让重连循环退出
                log.warning("BLE 连接中断: %s", exc, exc_info=True)
                self._emit_log("error", f"蓝牙连接中断: {exc}")
            finally:
                await self._teardown()

            if self._stop_event.is_set():
                break

            self._set_state(BleState.RECONNECTING)
            self._emit_log("info", f"{backoff:.0f} 秒后自动重连 …")
            if await self._sleep_or_stop(backoff):
                break
            backoff = min(backoff * 2, _BACKOFF_MAX)

        self._set_state(BleState.STOPPED)

    async def stop(self) -> None:
        self._stop_event.set()
        await self._teardown()

    async def _sleep_or_stop(self, seconds: float) -> bool:
        """等待指定秒数；若期间收到停止信号则返回 True。"""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _teardown(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            if client.is_connected:
                await client.disconnect()
        except Exception as exc:  # noqa: BLE001
            log.debug("断开连接时出现异常（已忽略）: %s", exc)

    # -- 连接 --------------------------------------------------------------- #

    def _matches(self, device, adv) -> bool:
        uuids = {str(u).lower() for u in (getattr(adv, "service_uuids", None) or ())}
        if SERVICE_UUID in uuids:
            return True
        name = device.name or getattr(adv, "local_name", None)
        return name == self._device_name

    async def _connect_once(self) -> None:
        self._set_state(BleState.SCANNING)
        self._emit_log("info", f"正在扫描 {self._device_name} …")

        try:
            device = await BleakScanner.find_device_by_filter(self._matches, timeout=SCAN_TIMEOUT)
        except BleakRuntimeError as exc:
            raise BleError(
                f"蓝牙扫描失败: {exc}",
                hint="请确认电脑蓝牙已打开，且未被其他程序独占",
            ) from exc

        if device is None:
            raise BleDeviceNotFoundError(
                f"未找到设备 {self._device_name}",
                hint="请确认 ESP32-C3 已上电，且电脑蓝牙已打开",
            )

        self._set_state(BleState.CONNECTING)
        self._emit_log("info", f"已找到 {device.address}，正在连接 …")

        self._disconnected.clear()
        client = BleakClient(
            device,
            disconnected_callback=self._on_disconnected,
            timeout=CONNECT_TIMEOUT,
            **_client_kwargs(),
        )
        try:
            await client.connect()
        except BleakRuntimeError as exc:
            raise BleError(f"连接失败: {exc}") from exc

        self._client = client

        try:
            await client.start_notify(TX_UUID, self._on_notify)
        except Exception as exc:
            raise BleServiceDiscoveryError() from exc

    async def _hold_connection(self) -> None:
        """守着连接，直到对端断开或收到停止信号。纯事件驱动，不做轮询。"""
        stop_wait = asyncio.create_task(self._stop_event.wait())
        disconnect_wait = asyncio.create_task(self._disconnected.wait())
        try:
            await asyncio.wait(
                {stop_wait, disconnect_wait}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (stop_wait, disconnect_wait):
                task.cancel()

    # -- 通知与下发 --------------------------------------------------------- #

    def _on_notify(self, _characteristic, data: bytearray) -> None:
        text = data.decode("utf-8", errors="replace")
        log.debug("BLE <- %r", text)

        self._inbox.put_nowait(text)
        self._emit_log("info", f"设备: {text}")

        status = DeviceStatus.parse(text)
        if status is not None and self._events.on_status:
            self._events.on_status(status)
        if self._events.on_reply:
            self._events.on_reply(text)

    def _on_disconnected(self, _client) -> None:
        self._emit_log("warning", "蓝牙连接已断开")
        self._client = None
        self._disconnected.set()

    async def _drain(self) -> None:
        """清空积压的通知。

        否则上一次命令的回复会被当成本次的结果 —— 表现为「切换成功」但实际
        根本没生效，是这类请求-应答协议最容易出的错。
        """
        while True:
            try:
                self._inbox.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def send(self, command: str, *, timeout: float = REPLY_TIMEOUT) -> str:
        """下发命令并等待设备回传。命令之间严格串行。"""
        async with self._send_lock:
            client = self._client
            if client is None or not client.is_connected:
                raise BleError("蓝牙未连接", hint="请先点击「连接设备」")

            await self._drain()
            try:
                await client.write_gatt_char(RX_UUID, command.encode("utf-8"), response=False)
            except Exception as exc:
                raise BleError(f"命令下发失败: {exc}") from exc

            try:
                return await asyncio.wait_for(self._inbox.get(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise BleNoResponseError(
                    f"{timeout:g} 秒内未收到设备回复",
                    hint="命令可能未生效，建议点击「刷新状态」确认实际状态",
                ) from exc

    # -- 扫描 --------------------------------------------------------------- #

    async def scan(self, timeout: float = 6.0) -> list[ScanResult]:
        """一次性扫描，返回所有匹配的设备（供 UI 的「扫描」按钮使用）。"""
        found: dict[str, ScanResult] = {}

        def on_detect(device, adv) -> None:
            if not self._matches(device, adv):
                return
            found[device.address] = ScanResult(
                name=device.name or getattr(adv, "local_name", None) or "(未命名)",
                address=device.address,
                rssi=getattr(adv, "rssi", None),
            )

        scanner = BleakScanner(detection_callback=on_detect)
        await scanner.start()
        try:
            await asyncio.sleep(timeout)
        finally:
            await scanner.stop()

        return sorted(found.values(), key=lambda item: item.name)

    # -- 事件派发 ----------------------------------------------------------- #

    def _set_state(self, state: BleState) -> None:
        if state is self._state:
            return
        self._state = state
        log.debug("BLE 状态 -> %s", state.value)
        if self._events.on_state:
            self._events.on_state(state)

    def _emit_log(self, level: str, message: str) -> None:
        if self._events.on_log:
            self._events.on_log(level, message)
