"""远程（Linux）桥接管理的 Qt 适配层。

为什么是「单线程 + 命令队列」而不是「每次操作起一个线程」
--------------------------------------------------------

远程操作天然是**串行**的：安装、启动、卸载都在动同一台机器上的同一份服务，
并发执行只会互相踩（例如「卸载」和「启动」交错，最后留下一个装了一半的状态）。

用一个常驻线程消费队列，串行就由结构保证了，不需要靠锁去碰运气；
按钮则由 ``busyChanged`` 统一置灰，用户也不会误连点。

另外**探活分两档**，代价差三个数量级：

- :meth:`refreshHttp` —— 只发一个 HTTP 请求（毫秒级），可以跟着界面周期跑
- :meth:`inspect` —— 要建 SSH 连接并跑环境探测（秒级），只在用户明确要求
  或操作结束后跑，绝不放进周期轮询里
"""

from __future__ import annotations

import logging
import queue
import threading

from PySide6.QtCore import QObject, Signal, Slot

from ..core.bridge_remote import RemoteBridge, RemoteState
from ..core.errors import UsbSwitchError
from ..core.models import AppConfig, Host

log = logging.getLogger(__name__)

SHUTDOWN_JOIN_TIMEOUT = 5.0

#: 队列里的哨兵：收到它就退出消费循环
_STOP = None


class RemoteWorker(QObject):
    stateUpdated = Signal(object)      # RemoteState
    envDetected = Signal(object)       # RemoteEnv
    logMessage = Signal(str, str)      # level, message
    operationFailed = Signal(str, str)  # message, hint
    busyChanged = Signal(bool)

    def __init__(self, config: AppConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._commands: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._shutdown = False

    # -- 生命周期 ----------------------------------------------------------- #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._consume, name="usbswitch-remote", daemon=True
        )
        self._thread.start()

    def shutdown(self) -> bool:
        """请求退出并等待线程收尾。可重复调用。"""
        if self._shutdown:
            return True
        self._shutdown = True
        self._commands.put(_STOP)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(SHUTDOWN_JOIN_TIMEOUT)
        ok = thread is None or not thread.is_alive()
        self._thread = None
        return ok

    # -- 供 UI 查询 --------------------------------------------------------- #

    @property
    def target_host(self) -> Host | None:
        """远程（Linux）桥接角色 —— 固定 Host B；未启用或未填地址时为 None。"""
        return self._config.remote_host()

    def endpoint_title(self) -> str:
        host = self.target_host
        if host is None:
            return "未配置远程主机"
        return self._config.endpoint_for(host).title

    def base_url(self) -> str:
        """实际连接地址；空串表示当前没有可用地址（配了隧道但隧道没通）。"""
        host = self.target_host
        if host is None:
            return ""
        return self._config.endpoint_for(host).connect_base() or ""

    # -- 槽：提交任务 ------------------------------------------------------- #

    @Slot()
    def testConnection(self) -> None:
        self._submit("test_connection")

    @Slot()
    def install(self) -> None:
        self._submit("install")

    @Slot()
    def startService(self) -> None:
        self._submit("start")

    @Slot()
    def stopService(self) -> None:
        self._submit("stop")

    @Slot()
    def uninstall(self) -> None:
        self._submit("uninstall")

    @Slot()
    def refreshHttp(self) -> None:
        """廉价探活：只打一个 HTTP 请求，不发 SSH。

        **刻意不置 busy** —— 它每 5 秒跑一次，而 HTTP 超时是 3 秒；若跟着置忙，
        界面会有近一半时间处于「按钮全灰」的状态，看起来就像程序坏了。
        按钮置灰只服务于用户主动发起的操作。
        """
        self._submit("refresh_http", mark_busy=False)

    @Slot()
    def inspect(self) -> None:
        """完整探测：SSH + HTTP 两层。开销大，仅按需调用。"""
        self._submit("inspect")

    # -- 内部 --------------------------------------------------------------- #

    def _submit(self, action: str, *, mark_busy: bool = True) -> None:
        if self._shutdown:
            return
        if mark_busy:
            self.busyChanged.emit(True)
        self._commands.put((action, mark_busy))

    def _bridge(self) -> RemoteBridge | None:
        """按当前配置构造远程桥接对象；远程桥接未配置（Host B 没填地址）时返回 None。

        每次都重新构造：用户可能在界面上改了 IP / 端口 / 密码，
        缓存住旧参数会导致「明明改了却还是连不上」。
        """
        host = self.target_host
        if host is None:
            return None
        return RemoteBridge(
            self._config.endpoint_for(host).remote,
            on_log=lambda level, message: self.logMessage.emit(level, message),
        )

    def _consume(self) -> None:
        while True:
            item = self._commands.get()
            if item is _STOP:
                break
            action, mark_busy = item
            try:
                self._dispatch(action)
            except UsbSwitchError as exc:
                self.operationFailed.emit(exc.message, exc.hint)
            except Exception as exc:  # noqa: BLE001 —— 后台线程绝不能因异常退出
                log.exception("远程操作异常: %s", action)
                self.operationFailed.emit(
                    f"远程操作失败：{type(exc).__name__}", str(exc)[:300]
                )
            finally:
                # 只有置过忙的才负责解除，否则会误把用户操作的「忙」提前清掉
                if mark_busy:
                    self.busyChanged.emit(False)

    def _dispatch(self, action: str) -> None:
        # 廉价探活要先于「已配置」检查：它是后台周期任务，未配置时应当安静地
        # 什么都不做，而不是每 5 秒往日志里灌一条错误。
        if action == "refresh_http":
            bridge = self._bridge()
            if bridge is not None:
                self.stateUpdated.emit(self._http_only_state(bridge))
            return

        bridge = self._bridge()
        if bridge is None:
            # 面板在禁用时已把按钮置灰，正常走不到这里；这条兜底是给「禁用后
            # 残留一次已排队的旧命令」用的 —— 那时若仍报「尚未配置远程主机」，
            # 用户会去重填一个本来填对的地址。
            if not self._config.remote_bridge().enabled:
                self.operationFailed.emit(
                    "远程桥接服务已禁用",
                    "请在「远程桥接服务」区勾选「启用远程桥接」后重试",
                )
                return
            self.operationFailed.emit(
                "尚未配置远程主机",
                "请先在「远程桥接服务 → 配置…」中填写 IP / SSH 端口 / 用户名 / 密码",
            )
            return

        if action == "test_connection":
            env = bridge.test_connection()
            self.envDetected.emit(env)
        elif action == "install":
            self.stateUpdated.emit(bridge.install())
        elif action == "start":
            self.stateUpdated.emit(bridge.start())
        elif action == "stop":
            self.stateUpdated.emit(bridge.stop())
        elif action == "uninstall":
            self.stateUpdated.emit(bridge.uninstall())
        elif action == "inspect":
            self.stateUpdated.emit(bridge.inspect())
        else:
            raise UsbSwitchError(f"未知的远程操作：{action}")

    @staticmethod
    def _http_only_state(bridge: RemoteBridge) -> RemoteState:
        """只探 HTTP 时，SSH 那一栏保留「未检测」，不冒充已探测。

        「隧道没通」必须与「服务没响应」分开说：前者是本地这一侧的问题（去修
        隧道），后者是远端服务的问题（去启动服务），用户要做的事完全不同。
        """
        if bridge.connect_base() is None:
            return RemoteState(
                ssh_reachable=False,
                ssh_detail="未检测",
                http_online=False,
                http_detail="隧道未建立",
            )

        payload = bridge.probe()
        if payload and payload.get("ok"):
            return RemoteState(
                ssh_reachable=False,
                ssh_detail="未检测",
                http_online=True,
                http_detail="在线",
                drives=tuple(payload.get("disks") or []),
            )
        return RemoteState(
            ssh_reachable=False,
            ssh_detail="未检测",
            http_online=False,
            http_detail="无响应",
        )
