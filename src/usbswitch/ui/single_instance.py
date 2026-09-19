"""单实例锁 + 「唤出已有窗口」。

**为什么这是硬要求**：引入托盘与开机自启之后，用户重复双击启动就成了常态。
两个实例 = 两个 ``BleakClient`` = **必然复现** Windows WinRT 并发 GATT 缺陷
（README 已记录），同时还会争抢本地桥接端口。

用 ``QLocalServer`` 而不是裸互斥体：它一次性解决两件事 ——
既是锁，又是唤醒通道。第二个实例只需往这个 socket 发一条消息，
已有实例收到后把窗口唤到前台，然后自己退出。

放在 ``ui/`` 而不是 ``core/``：`QLocalServer` 是 Qt 组件，而 core 层
禁止依赖任何 Qt 符号（由 `tests/unit/test_architecture.py` 强制）。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

DEFAULT_KEY = "usbswitch-console"
_ACTIVATE_MESSAGE = b"activate"
_CONNECT_TIMEOUT_MS = 500


class SingleInstance(QObject):
    """单实例守卫。

    用法::

        guard = SingleInstance()
        if not guard.try_acquire():
            return 0            # 已有实例，消息已发出，自己退出
        guard.activated.connect(window.raise_)
    """

    activated = Signal()

    def __init__(self, key: str = DEFAULT_KEY, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._key = key
        self._server: QLocalServer | None = None
        self._connections: list[QLocalSocket] = []

    # -- 获取 --------------------------------------------------------------- #

    def try_acquire(self) -> bool:
        """返回 True 表示本进程是唯一实例；False 表示已有实例在跑。"""
        if self._notify_existing():
            return False

        # 上一次异常退出可能留下残骸，先清掉再监听
        QLocalServer.removeServer(self._key)

        server = QLocalServer(self)
        server.newConnection.connect(self._on_new_connection)
        if not server.listen(self._key):
            log.error("无法监听单实例通道 %s: %s", self._key, server.errorString())
            # 监听失败时选择「继续运行」而不是拒绝启动 ——
            # 让用户用不上程序，比偶尔多开一个实例更糟。
            return True

        self._server = server
        return True

    def release(self) -> None:
        for socket in self._connections:
            socket.disconnectFromServer()
        self._connections.clear()
        if self._server is not None:
            self._server.close()
            QLocalServer.removeServer(self._key)
            self._server = None

    # -- 内部 --------------------------------------------------------------- #

    def _notify_existing(self) -> bool:
        """尝试唤醒已有实例。连得上说明它存在。"""
        socket = QLocalSocket()
        socket.connectToServer(self._key)
        if not socket.waitForConnected(_CONNECT_TIMEOUT_MS):
            return False
        socket.write(_ACTIVATE_MESSAGE)
        socket.flush()
        socket.waitForBytesWritten(_CONNECT_TIMEOUT_MS)
        socket.disconnectFromServer()
        log.info("检测到已有实例，已请求其唤出窗口，本进程退出")
        return True

    def _on_new_connection(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                break
            self._connections.append(socket)
            socket.disconnected.connect(lambda s=socket: self._forget(s))
            self.activated.emit()

    def _forget(self, socket: QLocalSocket) -> None:
        if socket in self._connections:
            self._connections.remove(socket)
        socket.deleteLater()
