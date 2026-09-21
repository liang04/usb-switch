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
        """该主机上桥接服务的**实际连接地址**。

        没有可用地址时抛 :class:`EjectError`（而不是给一个注定失败的地址）——
        调用方要把这条信息原样呈现给用户，因此它必须说清是哪一种「连不上」。

        注意取的是 :meth:`BridgeEndpoint.connect_base` 而不是 ``http_base``：
        远程端勾了「经 SSH 隧道访问」却没建起隧道时，前者返回 None，后者会
        悄悄换成直连地址，于是报错变成「连不上主机」，把真正的原因盖掉。
        """
        endpoint = self._config.bridges.get(host)
        if endpoint is None:
            raise EjectError(f"未配置 {host.label} 的桥接端点")

        base = endpoint.connect_base()
        if base is None:
            raise EjectError(
                f"{host.label} 的 SSH 隧道尚未建立，无法在该主机上安全弹出",
                hint=self._tunnel_down_hint(),
            )
        return base

    def _tunnel_down_hint(self) -> str:
        """隧道没通时的可操作提示：既说清原因，也给出一条能立刻脱困的路。

        两种情形都让 Host B 的安全弹出锁处于「要弹却又弹不了」的状态，
        用户会卡在「切不走」上，所以提示里必须带上出口。
        """
        return (
            "配置勾选了「经 SSH 隧道访问」，所以不会退回直连 —— 直连报出来的"
            "「连不上主机」会把真正的原因盖掉。请先照日志里的「隧道建立失败」"
            "修隧道（隧道自己的错误信息里写了处置办法）；若这轮不需要在 Host B 上"
            "弹盘，可取消勾选「远程桥接服务」里的「启用远程桥接」后重试，"
            "切换将不再要求安全弹出。"
        )

    def eject_for(self, host: Host) -> None:
        """在指定 Host 上安全弹出 U 盘。

        Raises:
            EjectError: 桥接不可达、隧道未建立、或弹出失败。调用方据此**硬中止**
                切换 —— 此时 U 盘仍挂载着，断电切换会损坏文件系统。
        """
        base = self.endpoint_for(host)

        try:
            payload = http_json.post_json(f"{base}/eject", timeout=self._timeout)
        except UsbSwitchError as exc:
            raise EjectError(
                f"无法连接 {host.label} 的桥接服务（{base}）",
                hint=(
                    f"{exc.message}。请确认该主机上的桥接服务已启动；"
                    "若这轮不需要在该主机上弹盘，可取消勾选「远程桥接服务」里的"
                    "「启用远程桥接」后重试"
                    if host is Host.B
                    else f"{exc.message}。请确认该主机上的桥接服务已启动。"
                ),
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
