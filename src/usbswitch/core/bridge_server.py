"""桥接 HTTP 服务（**库形态**）。

原方案让 GUI 用 ``subprocess.Popen([sys.executable, "bridge.py"])`` 拉起本地桥接。
开发态成立，**打包后必然崩**：PyInstaller 冻结后 ``sys.executable`` 指向的就是
exe 自己，``exe bridge.py`` 不会去执行什么脚本，只会再开一个 GUI 实例。

因此把服务做成可 import 的库，GUI 在后台线程里直接跑它。开发态与打包态行为
完全一致，还顺带消灭了「退出残留子进程」与「端口占死」两个风险 ——
线程随进程结束，socket 自动释放。

脚本形态（``scripts/bridge.py``）保留为十几行的 CLI 启动器，
README 里现有的手工用法不受影响。

对外契约
--------

**必须与 Linux 端的 ``scripts/linux_bridge.py`` 保持一致**，由
``tests/unit/test_bridge_api_parity.py`` 锁死：

============================  ==========================================================
``GET  /status``              ``{"ok": true, "drives": ["F"]}``
``POST /eject``               ``{"ok": true, "ejected": "F"}``
                              ``{"ok": true, "warning": "本机未检测到 U 盘"}``
                              ``{"ok": false, "error": "..."}``
============================  ==========================================================

安全语义（与旧实现一致）：**只要本机上还存在可移动 U 盘，就必须弹出成功才返回
``ok=true``**；弹出失败一律 ``ok=false``，调用方据此中止切换，不会动 VBUS。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import disk
from .errors import BridgePortInUseError
from .models import BridgeRunState

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8737

EjectProvider = Callable[[], dict]
StatusProvider = Callable[[], dict]

#: Windows 的两个套接字错误码，需要给出**不同**的提示
WSAEADDRINUSE = 10048  # 端口已被占用
WSAEACCES = 10013      # 访问被拒 —— 见 _bind_failure_hint()


def _errno_of(exc: BaseException) -> int | None:
    return getattr(exc, "winerror", None) or getattr(exc, "errno", None)


def _bind_failure_hint(port: int, exc: BaseException) -> str:
    """把绑定失败翻译成用户能照着做的提示。

    踩过：本机装着 Clash Verge，它在 ``0.0.0.0:8737`` 上绑了套接字（且带
    ``SO_EXCLUSIVEADDRUSE``），于是我们绑 ``127.0.0.1:8737`` 得到的是
    ``WSAEACCES(10013) 访问被拒`` 而不是 ``WSAEADDRINUSE(10048) 已被占用``。

    这两个码的排查方向完全不同，一句笼统的「端口可能被占用」会把人带偏。
    """
    code = _errno_of(exc)
    who = f"netstat -ano | findstr :{port}"
    if code == WSAEACCES:
        return (
            f"端口 {port} 被拒绝访问（WSAEACCES/10013）。常见原因有两种："
            f"① 代理 / VPN / 安全软件独占绑定了这个端口（用 {who} 查持有者）；"
            "② 该端口落在 Windows 的保留区间内"
            "（用 netsh int ipv4 show excludedportrange protocol=tcp 查看）。"
            "换个端口重新点「启动」即可，不必重启程序。"
        )
    if code == WSAEADDRINUSE:
        return (
            f"端口 {port} 已被占用。可能已有一个桥接服务在运行，"
            f"或其他程序正在使用它（用 {who} 查持有者）"
        )
    return f"端口 {port} 无法绑定（{exc}），可在设置中改用其他端口"


# --------------------------------------------------------------------------- #
# 默认的本地实现（平台相关部分集中在这里，方便测试替换）
# --------------------------------------------------------------------------- #


def default_status_provider() -> dict:
    return {"ok": True, "drives": disk.removable_drives()}


def default_eject_provider() -> dict:
    """本机弹出。

    本机看不到 U 盘时返回 ``ok`` + ``warning`` —— 可能是已经被弹出，
    也可能 U 盘当前接在另一台 Host 上（那台机器上的弹出只能在那台机器执行）。
    """
    drives = disk.removable_drives()
    if not drives:
        return {"ok": True, "warning": "本机未检测到 U 盘（已弹出或连接在另一台 Host 上）"}

    letter = disk.resolve_target()
    disk.eject(letter)
    return {"ok": True, "ejected": letter}


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #


class BridgeServer:
    """可嵌入的桥接 HTTP 服务。

    典型用法::

        server = BridgeServer(port=8737)
        server.start()
        ...
        server.stop()
    """

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        *,
        host: str = DEFAULT_HOST,
        eject_provider: EjectProvider | None = None,
        status_provider: StatusProvider | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._eject_provider = eject_provider or default_eject_provider
        self._status_provider = status_provider or default_status_provider

        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_error = ""

    # -- 只读属性 ----------------------------------------------------------- #

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    @property
    def is_running(self) -> bool:
        return self._httpd is not None

    @property
    def last_error(self) -> str:
        return self._last_error

    # -- 生命周期 ----------------------------------------------------------- #

    def start(self) -> None:
        """启动服务。

        Raises:
            BridgePortInUseError: 端口已被占用（或无法绑定）。
        """
        with self._lock:
            if self._httpd is not None:
                return

            self._assert_port_available()

            try:
                httpd = ThreadingHTTPServer(
                    (self._host, self._port), self._make_handler()
                )
            except OSError as exc:
                raise BridgePortInUseError(
                    f"无法监听 {self._host}:{self._port}（{exc}）",
                    hint=_bind_failure_hint(self._port, exc),
                ) from exc

            # 守护线程：即使主线程异常退出也不会挂住进程
            httpd.daemon_threads = True
            self._httpd = httpd
            self._thread = threading.Thread(
                target=lambda: httpd.serve_forever(poll_interval=0.1),
                name="usbswitch-bridge",
                daemon=True,
            )
            self._thread.start()
            self._last_error = ""
            log.info("桥接服务已启动: %s", self.base_url)

    def stop(self) -> None:
        """停止服务并释放端口。可重复调用。"""
        with self._lock:
            httpd, self._httpd = self._httpd, None
            thread, self._thread = self._thread, None

        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=3.0)
        log.info("桥接服务已停止")

    # -- 内部 --------------------------------------------------------------- #

    def _assert_port_available(self) -> None:
        """提前探测端口，避免起完服务才发现绑不上。

        **必须用 bind 而不是 connect 来探测。**

        原实现用 ``connect_ex`` 判断「有没有人在监听」，语义是错的：
        别的进程可能**绑定**了该端口却并未进入监听状态（或者它监听在
        ``0.0.0.0`` 而我们想绑 ``127.0.0.1``，两者并非同一把锁）。
        这时 connect 会失败、预检放行，紧接着真正的 bind 却报
        ``WSAEACCES`` —— 用户看到的错误来自「起完服务之后」，而不是预检。

        能不能绑，只有真的 bind 一次才知道。绑完立刻关掉即可。
        """
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # 不设 SO_REUSEADDR：要的就是「现在到底能不能独占这个地址」
            probe.bind((self._host, self._port))
        except OSError as exc:
            raise BridgePortInUseError(
                f"无法绑定 {self._host}:{self._port}",
                hint=_bind_failure_hint(self._port, exc),
            ) from exc
        finally:
            probe.close()

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class BridgeRequestHandler(BaseHTTPRequestHandler):
            server_version = "USBSwitchBridge/0.1"

            def _send(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # 网页控制面板与 GUI 都可能来自不同源
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:  # noqa: N802 —— HTTP 动词
                self._send(204, {})

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/status":
                    self._send(404, {"ok": False, "error": "unknown path"})
                    return
                try:
                    self._send(200, server._status_provider())
                except Exception as exc:  # noqa: BLE001 —— 绝不能因单个请求打断服务
                    log.exception("处理 /status 失败")
                    self._send(500, {"ok": False, "error": str(exc)})

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/eject":
                    self._send(404, {"ok": False, "error": "unknown path"})
                    return
                try:
                    self._send(200, server._eject_provider())
                except Exception as exc:  # noqa: BLE001
                    # 弹出失败必须以 ok=false 返回，调用方据此中止切换
                    log.warning("处理 /eject 失败: %s", exc)
                    self._send(200, {"ok": False, "error": str(exc)})

            def log_message(self, fmt: str, *args) -> None:
                log.debug("[bridge] " + fmt, *args)

        return BridgeRequestHandler


def state_of(server: BridgeServer) -> BridgeRunState:
    """把服务状态映射为 UI 用的枚举。"""
    return BridgeRunState.RUNNING if server.is_running else BridgeRunState.STOPPED
