"""本地桥接服务的 Qt 适配层。

起停是同步的（绑端口/关 socket，微秒级），但 **HTTP 探活放到独立线程里做** ——
即使本机调用通常只要几毫秒，UI 线程也不该赌这个时间。

为什么不用 ``QThreadPool`` + ``QRunnable``
------------------------------------------

试过，会炸：``QRunnable`` 不是 ``QObject``，它的信号载体只被 Python 侧引用
持有。任务还在线程池里排队、而 worker 已被回收时，``run()`` 里的 emit 会直接
报 ``RuntimeError: Signal source has been deleted``。

换成普通线程后这个问题自然消失：线程函数是绑定方法，**持有 worker 的强引用**，
QObject 不可能在探活完成前被回收。信号跨线程投递照常走 QueuedConnection。
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Signal, Slot

from ..core.bridge_local import LocalBridge
from ..core.errors import UsbSwitchError
from ..core.models import AppConfig, BridgeRunState, LocalBridgeConfig

log = logging.getLogger(__name__)

PROBE_JOIN_TIMEOUT = 3.0


class BridgeWorker(QObject):
    stateChanged = Signal(object)     # BridgeRunState
    statusUpdated = Signal(object)    # dict | None（/status 的响应）
    logMessage = Signal(str, str)     # level, message
    operationFailed = Signal(str, str)  # message, hint

    def __init__(self, config: AppConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._local = LocalBridge(LocalBridgeConfig(port=config.local_bridge_port))
        self._probe_thread: threading.Thread | None = None
        self._stopped = False

    @property
    def base_url(self) -> str:
        return self._local.base_url

    @property
    def port(self) -> int:
        """当前**实际**监听的本机端口。

        端口是构造时绑定的，配置改了不会热生效 —— 主窗口拿它和配置比对，
        不一致就提示用户需要重启。
        """
        return self._local.port

    @property
    def is_started(self) -> bool:
        return self._local.is_started

    @property
    def state(self) -> BridgeRunState:
        return self._local.state

    # -- 槽 --------------------------------------------------------------- #

    @Slot()
    def startBridge(self) -> None:
        self._sync_port()
        try:
            self._local.start()
        except UsbSwitchError as exc:
            self.stateChanged.emit(BridgeRunState.ERROR)
            self.operationFailed.emit(exc.message, exc.hint)
            return

        self.stateChanged.emit(BridgeRunState.RUNNING)
        self.logMessage.emit("success", f"本地桥接服务已启动：{self.base_url}")
        self.refresh()

    def _sync_port(self) -> None:
        """配置里的本机端口变了就换一个 ``LocalBridge``。

        端口是在构造 ``LocalBridge`` 时写死的。不同步的话，用户在设置里改了
        端口、再点「启动」，仍然会撞在**旧端口**上，只能重启程序才能生效。

        正在跑的时候不动它：先把服务停掉再启动，免得把旧端口上的服务
        悄悄替换掉。
        """
        if self._local.is_started:
            return
        wanted = self._config.local_bridge_port
        if self._local.port != wanted:
            log.info("本机桥接端口由 %d 改为 %d", self._local.port, wanted)
            self._local = LocalBridge(LocalBridgeConfig(port=wanted))

    @Slot()
    def stopBridge(self) -> None:
        if not self._local.is_started:
            self.stateChanged.emit(BridgeRunState.STOPPED)
            return
        self._local.stop()
        self.stateChanged.emit(BridgeRunState.STOPPED)
        self.statusUpdated.emit(None)
        self.logMessage.emit("info", "本地桥接服务已停止")

    @Slot()
    def refresh(self) -> None:
        """异步探活。上一次还没回来时直接跳过，避免堆积线程。"""
        if self._stopped or (self._probe_thread is not None and self._probe_thread.is_alive()):
            return
        self._probe_thread = threading.Thread(
            target=self._probe, name="usbswitch-bridge-probe", daemon=True
        )
        self._probe_thread.start()

    @Slot()
    def shutdown(self) -> None:
        """退出前释放端口。可重复调用。"""
        self._stopped = True
        self._local.stop()
        thread = self._probe_thread
        if thread is not None and thread.is_alive():
            thread.join(PROBE_JOIN_TIMEOUT)

    # -- 后台 --------------------------------------------------------------- #

    def _probe(self) -> None:
        try:
            payload = self._local.probe()
        except Exception as exc:  # noqa: BLE001 —— 探活绝不能把线程带崩
            log.debug("桥接探活异常: %s", exc)
            payload = None
        self.statusUpdated.emit(payload)
