"""本地桥接服务的生命周期与状态。

``bridge_server`` 是纯 HTTP 实现，本模块补上 UI 需要的那一层：
按配置起停、端口探活、把结果映射成 :class:`BridgeRunState`。
"""

from __future__ import annotations

import logging

from . import http_json
from .bridge_server import BridgeServer
from .errors import BridgeError
from .models import BridgeRunState, LocalBridgeConfig

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 2.0


class LocalBridge:
    """本机桥接服务。"""

    def __init__(self, config: LocalBridgeConfig | None = None) -> None:
        self._config = config or LocalBridgeConfig()
        self._server = BridgeServer(self._config.port)

    # -- 只读属性 ----------------------------------------------------------- #

    @property
    def port(self) -> int:
        return self._config.port

    @property
    def base_url(self) -> str:
        return self._server.base_url

    @property
    def is_started(self) -> bool:
        """服务线程是否在跑（不代表 HTTP 可用）。"""
        return self._server.is_running

    @property
    def state(self) -> BridgeRunState:
        """服务线程状态。

        线程活着 ≠ 服务可用，所以这里的 RUNNING 只表示「已启动」；
        真正可用性由 :meth:`probe` 判断。
        """
        return BridgeRunState.RUNNING if self._server.is_running else BridgeRunState.STOPPED

    # -- 生命周期 ----------------------------------------------------------- #

    def start(self) -> None:
        """启动。端口被占用时抛 :class:`BridgePortInUseError`。"""
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    # -- 探活 --------------------------------------------------------------- #

    def probe(self) -> dict | None:
        """请求 ``/status``。可达返回响应 dict，不可达返回 None。"""
        try:
            return http_json.get_json(f"{self.base_url}/status", timeout=PROBE_TIMEOUT)
        except BridgeError as exc:
            log.debug("桥接探活失败: %s", exc)
            return None

    def is_healthy(self) -> bool:
        """服务线程在跑 **且** HTTP 可响应 —— 这才是 UI 该显示的「运行中」。"""
        return self._server.is_running and bool((self.probe() or {}).get("ok"))
