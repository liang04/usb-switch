"""安全弹出：按 Host 角色路由到对应的桥接端点。

编排层只认识 ``Ejector`` 协议（见 :mod:`usbswitch.core.orchestrator`），
不关心对端是本机还是远程 —— 两者暴露的是**同一套 HTTP 契约**，
所以这里是同一个实现。远程与本地唯一的差别只在于「谁来把这个服务跑起来」，
那是 `bridge_local` 与 `bridge_remote` 各自的职责。
"""

from __future__ import annotations

import logging

from . import http_json
from .errors import EjectError, UsbSwitchError
from .models import AppConfig, Host

log = logging.getLogger(__name__)

EJECT_TIMEOUT = 30.0


class BridgeEjector:
    """通过桥接服务的 ``POST /eject`` 执行安全弹出。"""

    def __init__(self, config: AppConfig, *, timeout: float = EJECT_TIMEOUT) -> None:
        self._config = config
        self._timeout = timeout

    def endpoint_for(self, host: Host) -> str:
        endpoint = self._config.bridges.get(host)
        if endpoint is None:
            raise EjectError(f"未配置 {host.label} 的桥接端点")
        return endpoint.http_base

    def eject_for(self, host: Host) -> None:
        """在指定 Host 上安全弹出 U 盘。

        Raises:
            EjectError: 桥接不可达、或弹出失败。调用方据此**硬中止**切换 ——
                此时 U 盘仍挂载着，断电切换会损坏文件系统。
        """
        base = self.endpoint_for(host)

        try:
            payload = http_json.post_json(f"{base}/eject", timeout=self._timeout)
        except UsbSwitchError as exc:
            raise EjectError(
                f"无法连接 {host.label} 的桥接服务（{base}）",
                hint=f"{exc.message}。请确认该主机上的桥接服务已启动。",
            ) from exc

        if payload.get("ok"):
            warning = payload.get("warning")
            if warning:
                # 本机没看到 U 盘：可能已弹出，也可能接在另一台 Host 上
                log.info("%s: %s", host.label, warning)
            else:
                log.info("%s: 已安全弹出 %s", host.label, payload.get("ejected"))
            return

        raise EjectError(
            f"{host.label} 弹出失败：{payload.get('error', '未知错误')}",
            hint="已中止切换，VBUS 保持不变",
        )
