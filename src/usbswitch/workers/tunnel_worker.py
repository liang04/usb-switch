"""SSH 隧道的 Qt 适配层。

``SshTunnel.start()`` 只做本地 bind，**立刻就返回**；真正建 SSH 通道是后台
监督线程的事。所以这里不需要再套一层线程 —— 直接在 UI 线程调用即可，
状态通过回调转成信号送回。

这一层唯一的职责是「按配置决定隧道该不该存在」：
Host B 没填远程地址、用户关掉了隧道开关、或换了目标主机，隧道都要跟着起停。
把这个判断放在 `ui/` 里会让 `main_window` 再胖一圈，放在 `core/` 又会引入
「配置变了要通知谁」这种纯 UI 关注点。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal, Slot

from ..core.errors import UsbSwitchError
from ..core.models import AppConfig, BridgeRunState, Host, RemoteBridgeConfig
from ..core.ssh_tunnel import SshTunnel

log = logging.getLogger(__name__)


class TunnelWorker(QObject):
    stateChanged = Signal(object)   # BridgeRunState
    logMessage = Signal(str, str)   # level, message

    def __init__(self, config: AppConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._tunnel: SshTunnel | None = None
        self._started_port = -1
        self._started_target = ""

    # -- 只读属性 ----------------------------------------------------------- #

    @property
    def is_configured(self) -> bool:
        """当前配置是否要求一条隧道。"""
        host = self._remote_host()
        if host is None:
            return False
        remote = self._remote(host)
        return bool(remote.use_tunnel and remote.host.strip())

    @property
    def is_running(self) -> bool:
        return self._tunnel is not None and self._tunnel.is_running

    @property
    def is_ready(self) -> bool:
        return self._tunnel is not None and self._tunnel.is_ready

    @property
    def base_url(self) -> str:
        return self._tunnel.base_url if self._tunnel is not None else ""

    @property
    def local_port(self) -> int:
        return self._tunnel.local_port if self._tunnel is not None else 0

    @property
    def remote_target(self) -> str:
        return self._tunnel.remote_target if self._tunnel is not None else ""

    @property
    def state(self) -> BridgeRunState:
        if self._tunnel is None:
            return BridgeRunState.STOPPED
        return self._tunnel.state

    @property
    def last_error(self) -> str:
        return self._tunnel.last_error if self._tunnel is not None else ""

    # -- 槽 --------------------------------------------------------------- #

    @Slot()
    def sync(self) -> None:
        """按当前配置让隧道的存在状态与之对齐。配置改动后调用。"""
        if not self.is_configured:
            self._teardown()
            return

        host = self._remote_host()
        assert host is not None  # is_configured 已保证
        remote = self._remote(host)

        # 目标或端口变了就得重建 —— 隧道是绑在具体目标上的
        target = remote.ssh_target
        if self.is_running and (self._started_port != remote.tunnel_local_port
                                or self._started_target != target):
            self.logMessage.emit("info", "远程连接参数已变更，正在重建 SSH 隧道 …")
            self._teardown()

        if self.is_running:
            return
        self._bring_up(remote)

    @Slot()
    def restart(self) -> None:
        """手动重连（比如用户改了远端的网关配置）。"""
        self._teardown()
        self.sync()

    @Slot()
    def shutdown(self) -> None:
        self._teardown()

    # -- 内部 --------------------------------------------------------------- #

    def _remote_host(self) -> Host | None:
        return self._config.remote_host()

    def _remote(self, host: Host) -> RemoteBridgeConfig:
        return self._config.endpoint_for(host).remote

    def _bring_up(self, remote: RemoteBridgeConfig) -> None:
        tunnel = SshTunnel(
            remote,
            on_log=lambda level, message: self.logMessage.emit(level, message),
            on_state=self._on_state,
        )
        try:
            tunnel.start()
        except UsbSwitchError as exc:
            self.logMessage.emit("error", f"SSH 隧道启动失败：{exc.message}")
            if exc.hint:
                self.logMessage.emit("info", exc.hint)
            self.stateChanged.emit(BridgeRunState.ERROR)
            return

        self._tunnel = tunnel
        self._started_port = remote.tunnel_local_port
        self._started_target = remote.ssh_target
        self.stateChanged.emit(BridgeRunState.STARTING)

    def _teardown(self) -> None:
        tunnel, self._tunnel = self._tunnel, None
        self._started_port = -1
        self._started_target = ""
        if tunnel is None:
            # 就算没起过，也要把可能残留的运行期地址清掉，免得桥接一直指向
            # 一个已经不存在的隧道端口
            self._clear_runtime_base()
            self.stateChanged.emit(BridgeRunState.STOPPED)
            return
        tunnel.stop()
        self.stateChanged.emit(BridgeRunState.STOPPED)

    def _clear_runtime_base(self) -> None:
        """清掉可能残留的隧道地址。

        **刻意不走 `remote_host()`**：远程桥接被禁用时它返回 None，而禁用恰恰是
        最需要抹掉隧道地址的时刻 —— 否则用户重新启用后，`http_base` 会短暂指向
        一个已经关掉的本地端口，表现为「刚打开就说桥接不可达」。
        这里问的是「远程桥接被配成了什么样」，不是「现在要不要用」。
        """
        remote = self._remote(Host.B)
        if remote.runtime_http_base:
            remote.runtime_http_base = ""

    def _on_state(self, state: BridgeRunState) -> None:
        self.stateChanged.emit(state)
