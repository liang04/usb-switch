"""SSH 隧道：把远端回环地址上的桥接服务映射到本机一个端口。

为什么需要它
------------

实测（Robustel 边缘网关 192.168.0.1）：设备上服务明明正常 —— 监听
``0.0.0.0:8738``、设备本机 ``curl 127.0.0.1:8738/status`` 返回
``{"ok": true, ...}`` —— 但从 Windows 直连 8738 **被网关 LAN 口的入站白名单挡住**
（实测 22/80/443 放通，其余不放）。

走 SSH 隧道有三个好处：

1. **不需要改设备配置** —— SSH（22）本来就是通的。
2. **顺带解决「无鉴权 HTTP 服务暴露在网络里」这个遗留风险**。远端桥接可以不
   监听在网络上，外部只能经认证过的 SSH 访问。
3. **语义清晰** —— 隧道只在需要时存在，关掉即无暴露面。

设计要点
--------

**本地端口默认自动选。** 请求 0 让内核分配，再回报实际端口。写死一个端口会
平白多出一类「本地端口被占用」的失败 —— 开发机上 8737/8738 这类通用端口
常被代理 / VPN / 安全软件占着（实测本机 Clash Verge 就绑了 ``0.0.0.0:8737``）。

**隧道与「管理连接」是两条独立的 SSH 连接。** 安装 / 探活是短命的、串行的；
隧道是长命的、要一直转发。共用一条连接会让「装个东西」的失败顺带把隧道也
打断，反过来隧道上的异常也会污染管理操作的错误分类。

**就绪后写回 ``config.runtime_http_base``。** 这样 ``ejector``、``probe()``、
安装后的 HTTP 验证三处都自动走隧道，不需要各自判断 —— 少判断一处就会留下
「状态显示在线、切换却连不上」这种自相矛盾。
"""

from __future__ import annotations

import logging
import socket
import threading

import paramiko

from .bridge_remote import open_ssh_client, translate_ssh_error
from .errors import BridgeError, TunnelError, UsbSwitchError
from .models import BridgeRunState, RemoteBridgeConfig

log = logging.getLogger(__name__)

#: accept 的超时，用于让循环有机会检查停止标志
ACCEPT_TIMEOUT = 0.5

#: 转发缓冲区
BUFFER_SIZE = 65536

#: 断线后的重连退避
RECONNECT_DELAY = 3.0
MAX_RECONNECT_DELAY = 30.0

#: 单个转发连接的空闲上限（秒）。没有它，一条半死不活的连接会永远占着线程。
RELAY_TIMEOUT = 300.0


def _half_close_write(sock) -> None:
    """只关闭**写**方向，保留读方向。

    参数是 socket 或 paramiko 的 Channel，两者的 API 名字不同。
    """
    shutdown_write = getattr(sock, "shutdown_write", None)
    if callable(shutdown_write):
        try:
            shutdown_write()
        except (OSError, paramiko.SSHException):
            pass
        return

    shutdown = getattr(sock, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _close_quietly(sock) -> None:
    try:
        sock.close()
    except (OSError, paramiko.SSHException):
        pass


def _pump(src, dst) -> None:
    """单向搬运：把 ``src`` 的数据写到 ``dst``。

    结束时对 ``dst`` 做**半关闭**，而不是直接 close
    ---------------------------------------------------

    踩过（由测试抓出来的）：原来两个方向都直接 ``close()``。可是**当 socket 的
    接收缓冲里还有未读数据时，``close()`` 会让 TCP 发 RST 而不是 FIN**，
    对端还没取走的数据会被内核直接丢掉 —— ``http.client`` 报
    ``IncompleteRead(n bytes read, m more expected)`` 就是这么来的。

    小响应在快机器上通常侥幸不会命中（这正是它难查的原因：14 个用例过了、
    第 15 个偶发失败），但只要响应大一点、或者链路上慢一点，就会稳定地截断
    响应体。

    正确做法是半关闭：告诉对端「我没有更多数据了」，但仍然能收；
    等两个方向都结束，再由 :meth:`SshTunnel._relay` 彻底关闭。
    """
    try:
        while True:
            chunk = src.recv(BUFFER_SIZE)
            if not chunk:
                break
            dst.sendall(chunk)
    except (OSError, paramiko.SSHException, EOFError):
        pass
    finally:
        _half_close_write(dst)


class SshTunnel:
    """远端 ``127.0.0.1:<bridge_port>`` ←→ 本机 ``127.0.0.1:<自动端口>``。

    典型用法::

        tunnel = SshTunnel(config, on_state=..., on_log=...)
        tunnel.start()               # 立刻返回；本地监听已就绪
        tunnel.wait_ready(10)        # 等 SSH 通道建立
        ...
        tunnel.stop()
    """

    def __init__(
        self,
        config: RemoteBridgeConfig,
        *,
        local_port: int = 0,
        on_log=None,
        on_state=None,
        client_factory=None,
    ) -> None:
        self._config = config
        self._requested_port = local_port or config.tunnel_local_port
        self._on_log = on_log
        self._on_state = on_state
        self._client_factory = client_factory

        self._listener: socket.socket | None = None
        self._client: paramiko.SSHClient | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._relay_threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._local_port = 0
        self._state = BridgeRunState.STOPPED
        self._error = ""

    # -- 只读属性 ----------------------------------------------------------- #

    @property
    def local_port(self) -> int:
        return self._local_port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._local_port}" if self._local_port else ""

    @property
    def remote_target(self) -> str:
        return f"127.0.0.1:{self._config.bridge_port}"

    @property
    def state(self) -> BridgeRunState:
        with self._lock:
            return self._state

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._error

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- 生命周期 ----------------------------------------------------------- #

    def start(self) -> None:
        """绑定本地监听并启动转发线程。

        Raises:
            TunnelError: 本地端口无法绑定。
        """
        if self._thread is not None:
            return

        self._stop.clear()
        self._ready.clear()
        self._set_state(BridgeRunState.STARTING)

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", self._requested_port))
            listener.listen(16)
            listener.settimeout(ACCEPT_TIMEOUT)
        except OSError as exc:
            listener.close()
            self._fail(f"无法在 127.0.0.1:{self._requested_port or '自动'} 上建立隧道监听：{exc}")
            raise TunnelError(
                "无法建立隧道本地监听",
                hint=f"{exc}。可把「隧道本地端口」改为 0（自动）后重试。",
            ) from exc

        self._listener = listener
        self._local_port = int(listener.getsockname()[1])
        self._thread = threading.Thread(
            target=self._supervise, name="usbswitch-tunnel", daemon=True
        )
        self._thread.start()
        self._log("info", f"隧道本地监听已就绪：{self.base_url} → 远端 {self.remote_target}")

    def stop(self) -> None:
        """停止转发并断开。可重复调用。"""
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

        self._close_client()
        self._clear_runtime_base()
        self._ready.clear()
        self._set_state(BridgeRunState.STOPPED)
        self._local_port = 0

    def wait_ready(self, timeout: float = 15.0) -> bool:
        return self._ready.wait(timeout)

    # -- 内部 --------------------------------------------------------------- #

    def _log(self, level: str, message: str) -> None:
        log.log({"debug": 10, "info": 20, "warning": 30, "error": 40}.get(level, 20), message)
        if self._on_log is not None:
            self._on_log(level, message)

    def _set_state(self, state: BridgeRunState, error: str = "") -> None:
        with self._lock:
            self._state = state
            self._error = error
        if self._on_state is not None:
            self._on_state(state)

    def _fail(self, message: str) -> None:
        with self._lock:
            self._state = BridgeRunState.ERROR
            self._error = message
        if self._on_state is not None:
            self._on_state(BridgeRunState.ERROR)

    def _publish_runtime_base(self) -> None:
        """把隧道地址写回配置 —— 桥接地址的唯一来源。"""
        self._config.runtime_http_base = self.base_url

    def _clear_runtime_base(self) -> None:
        if self._config.runtime_http_base:
            self._config.runtime_http_base = ""

    def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 —— 关闭失败不该影响收尾
                log.debug("关闭隧道 SSH 连接时出错", exc_info=True)

    def _supervise(self) -> None:
        """常驻监督：连不上/断了就退避重连，直到被要求停止。"""
        delay = RECONNECT_DELAY
        while not self._stop.is_set():
            try:
                self._client = open_ssh_client(
                    self._config,
                    client_factory=self._client_factory,
                    on_new_host_key=lambda host, kind, fp: self._log(
                        "warning",
                        f"隧道首次连接 {host}，已记录主机密钥（{kind} {fp[:16]}…）。",
                    ),
                )
            except UsbSwitchError as exc:
                self._fail(exc.message)
                self._log("error", f"隧道建立失败：{exc}（{delay:.0f} 秒后重试）")
                self._sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)
                continue

            delay = RECONNECT_DELAY
            self._ready.set()
            self._publish_runtime_base()
            self._set_state(BridgeRunState.RUNNING)
            self._log("success", f"隧道已打通：{self.base_url} → {self.remote_target}")

            try:
                self._accept_loop()
            finally:
                self._ready.clear()
                self._clear_runtime_base()
                self._close_client()

            if not self._stop.is_set():
                self._fail("SSH 连接已断开")
                self._log("warning", f"隧道断开，{delay:.0f} 秒后重连")
                self._sleep(delay)

    def _sleep(self, seconds: float) -> None:
        """可被打断的等待。"""
        self._stop.wait(seconds)

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                conn, peer = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                # 监听套接字已关闭（停止流程）或出错
                return
            except Exception:  # noqa: BLE001
                log.debug("隧道 accept 异常", exc_info=True)
                return

            thread = threading.Thread(
                target=self._relay, args=(conn, peer), name="usbswitch-tunnel-relay", daemon=True
            )
            thread.start()
            self._reap_relays()
            self._relay_threads.append(thread)

    def _reap_relays(self) -> None:
        self._relay_threads = [t for t in self._relay_threads if t.is_alive()]

    def _relay(self, conn: socket.socket, peer) -> None:
        client = self._client
        if client is None:
            conn.close()
            return
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            conn.close()
            return

        try:
            conn.settimeout(RELAY_TIMEOUT)
            channel = transport.open_channel(
                "direct-tcpip", ("127.0.0.1", self._config.bridge_port), peer
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("隧道打开通道失败: %s", exc)
            try:
                conn.close()
            except OSError:
                pass
            return

        log.debug("隧道路由 %s → %s", peer, self.remote_target)
        # 双向搬运。两条 _pump 各自负责在半程结束时做**半关闭**（不能直接
        # close，见 _pump 的说明），全部结束之后再彻底关闭两端。
        worker = threading.Thread(
            target=_pump, args=(conn, channel), name="usbswitch-tunnel-pump", daemon=True
        )
        worker.start()
        _pump(channel, conn)
        # 等另一侧把客户端剩下的数据搬完；客户端若是 keep-alive 长连接，
        # 最多等 2 秒就强制收尾，免得一条空闲连接永远占着线程
        worker.join(timeout=2.0)
        _close_quietly(channel)
        _close_quietly(conn)


def make_tunnel_error(config: RemoteBridgeConfig, exc: BaseException) -> UsbSwitchError:
    """隧道建不起来时，把底层异常翻成可操作提示。"""
    return translate_ssh_error(config, exc)


def tunnel_hint(config: RemoteBridgeConfig) -> str:
    """隧道关闭时，给用户一句直白的话说明会连到哪里。"""
    return f"（隧道已关闭，将直连 {config.direct_http_base}）"


def is_local_port_free(port: int) -> bool:
    """端口是否可绑。给 UI 做即时校验用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


__all__ = [
    "SshTunnel",
    "TunnelError",
    "BridgeError",
    "is_local_port_free",
    "make_tunnel_error",
    "tunnel_hint",
]
