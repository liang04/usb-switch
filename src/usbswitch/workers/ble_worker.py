"""BLE + 切换编排的 Qt 适配层。

设计要点：

1. **独立后台线程 + 独立 asyncio 事件循环。** Qt 事件循环与 asyncio 事件循环
   不共处一个线程，因此不存在互相阻塞的问题，调试也简单。
2. **用 ``threading.Thread`` 而不是 ``QThread``。** 见下方说明。
3. **只允许一个 BleakClient。** 整个进程只有本模块持有 BLE 客户端 —— Windows 的
   WinRT 后端在并发客户端下会出现 GATT 服务发现不完整（README 已记录）。
   这也是为什么 GUI 运行时**不能**再去调用 `scripts/usb_switch.py`。

为什么不用 QThread
------------------

``QThread.quit()`` 只对**Qt 事件循环**有效，而本模块的线程跑的是阻塞的
``asyncio.run_forever()``。两者机制不相通：``quit()`` 设置的 ``quitNow`` 标志
要等到 ``_thread_main()`` 返回、Qt 的 ``exec()`` 启动时才会生效，而 ``run_forever()``
在此之前永远不会返回 —— 结果是 ``wait()`` 超时、Qt 报
「QThread: Destroyed while thread is still running」，进程退出时直接崩溃。

换成普通线程后，收尾逻辑变成显式的「等循环就绪 → 请求停止 → join」，
没有隐式状态机，也不会有竞态。信号跨线程投递不受影响：接收者在主线程，
Qt 会自动走 QueuedConnection。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal, Slot

from ..core.ble import BleController, BleEvents, BleState
from ..core.ejector import BridgeEjector
from ..core.errors import UsbSwitchError
from ..core.models import AppConfig, Host
from ..core.orchestrator import SwitchOrchestrator

log = logging.getLogger(__name__)

JOIN_TIMEOUT = 5.0


class BleWorker(QObject):
    """在后台线程里托管 BLE 连接与切换编排。"""

    stateChanged = Signal(object)      # BleState
    statusReceived = Signal(object)    # DeviceStatus
    devicesFound = Signal(list)        # list[ScanResult]
    logMessage = Signal(str, str)      # level, message
    switchSucceeded = Signal(object)   # SwitchResult
    operationFailed = Signal(str, str)  # message, hint
    threadStopped = Signal()

    def __init__(
        self,
        config: AppConfig,
        *,
        eject_lock: Callable[[Host], bool] | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._run_task: asyncio.Task | None = None
        self._pending_connection: bool = bool(config.ble.auto_connect)

        self._controller = BleController(
            device_name=config.ble.device_name,
            events=BleEvents(
                on_state=self.stateChanged.emit,
                on_status=self.statusReceived.emit,
                on_log=self.logMessage.emit,
            ),
        )
        self._orchestrator = SwitchOrchestrator(
            ble=self._controller,
            ejector=BridgeEjector(config),
            config=config,
            on_log=self.logMessage.emit,
            eject_lock=eject_lock,
        )

    # -- 生命周期 ----------------------------------------------------------- #

    def start(self) -> None:
        """由 UI 线程调用。重复调用无副作用。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._thread_main, name="usbswitch-ble", daemon=True
        )
        self._thread.start()

    def shutdown(self, timeout: float = JOIN_TIMEOUT) -> bool:
        """阻塞直到后台线程退出，返回是否干净退出。可重复调用。

        必须同时挂在 ``closeEvent`` 和 ``QApplication.aboutToQuit`` 上：
        ``QApplication.quit()`` 不触发 ``closeEvent``，只挂后者会让线程
        在解释器退出时才被动销毁。
        """
        with self._shutdown_lock:
            thread = self._thread
            if thread is None:
                return True

            # 必须先等事件循环就绪：否则 _loop 还是 None，stop 请求发不出去，
            # run_forever() 会永远阻塞（这正是最初用 QThread 踩到的坑）。
            if not self._loop_ready.wait(timeout):
                log.error("后台事件循环在 %.1f 秒内未就绪", timeout)
                return False

            loop = self._loop
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except RuntimeError:
                    log.debug("事件循环已被关闭，跳过 stop 请求")

            thread.join(timeout)
            if thread.is_alive():
                log.error("后台线程未能在 %.1f 秒内退出，可能存在泄漏", timeout)
                return False

            self._thread = None
            return True

    def _thread_main(self) -> None:
        """后台线程入口：建立事件循环并常驻，直到收到 stop 请求。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()   # 置位后才能安全接收 stop 请求

        try:
            loop.call_soon(self._apply_connection, self._pending_connection)
            loop.run_forever()
        finally:
            self._cancel_run_task()
            try:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                loop.close()
                self._loop = None
                self.threadStopped.emit()

    # -- 对外槽（均在 UI 线程被调用） --------------------------------------- #

    @Slot(bool)
    def requestConnection(self, wanted: bool) -> None:
        self._pending_connection = wanted
        self._call_soon(self._apply_connection, wanted)

    @Slot(str)
    def requestSwitch(self, host_value: str) -> None:
        self._submit(
            lambda: self._orchestrator.switch_to(Host(host_value)),
            on_success=self.switchSucceeded.emit,
        )

    @Slot()
    def requestStatus(self) -> None:
        self._submit(self._orchestrator.refresh_status)

    @Slot()
    def requestScan(self) -> None:
        self._submit(self._controller.scan, on_success=self.devicesFound.emit)

    # -- 内部 --------------------------------------------------------------- #

    def _call_soon(self, func, *args) -> bool:
        """把同步回调投递到事件循环线程。循环未就绪时返回 False。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        try:
            loop.call_soon_threadsafe(func, *args)
        except RuntimeError:
            return False
        return True

    def _apply_connection(self, wanted: bool) -> None:
        """在事件循环线程里执行。"""
        if wanted:
            self._start_run_task()
        else:
            self._cancel_run_task()
            self.stateChanged.emit(BleState.IDLE)

    def _start_run_task(self) -> None:
        if self._loop is None or self._run_task is not None:
            return
        self._run_task = self._loop.create_task(self._controller.run())
        self._run_task.add_done_callback(self._on_run_task_done)

    def _cancel_run_task(self) -> None:
        task, self._run_task = self._run_task, None
        if task is not None and not task.done():
            task.cancel()

    def _on_run_task_done(self, task: asyncio.Task) -> None:
        self._run_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning("BLE 常驻任务异常退出: %s", exc, exc_info=exc)

    def _submit(self, factory, *, on_success=None) -> None:
        """在事件循环线程里创建协程并投递，把结果翻译成信号。

        ``factory`` 是「无参可调用对象 → 协程」，这样在循环不可用时可以安全地
        放弃创建（直接传协程对象的话，不 await 就会一直挂着报警告）。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            self.operationFailed.emit("蓝牙服务尚未就绪", "请稍候重试")
            return

        def schedule() -> None:
            future = asyncio.ensure_future(factory(), loop=loop)
            future.add_done_callback(deliver)

        def deliver(task: asyncio.Future) -> None:
            if task.cancelled():
                return
            try:
                result = task.result()
            except UsbSwitchError as exc:
                self.operationFailed.emit(exc.message, exc.hint)
            except Exception as exc:  # noqa: BLE001 —— 兜底，绝不让异常被吞
                log.exception("BLE 操作失败")
                self.operationFailed.emit(str(exc), "")
            else:
                if on_success is not None:
                    on_success(result)

        try:
            loop.call_soon_threadsafe(schedule)
        except RuntimeError:
            self.operationFailed.emit("蓝牙服务已停止", "请重新连接设备")
