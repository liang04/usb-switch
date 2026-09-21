"""切换编排 —— 唯一允许改变设备状态的入口。

除了本模块，其他任何地方都不得直接向设备下发 ``a`` / ``b`` / ``x`` 命令。
这条约束对应设计文档的两条安全性要求：

1. **安全弹出失败必须硬中止** —— 此时 U 盘还挂载着，断电切换会损坏文件系统；
2. **避免无谓切换** —— 目标主机等于当前主机时不发命令，减少 VBUS 通断次数。

本模块只依赖抽象，不关心对端是本机桥接还是远程桥接，也不依赖 Qt。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .errors import BleCommandRejectedError, BleError, BleNoResponseError
from .models import AppConfig, DeviceStatus, Host, ReplyKind, classify_reply

log = logging.getLogger(__name__)

#: 固件 switchTo() 最坏约 1.5 s（50 + 300 + 5 + 300 ms 加 hostPresent 去抖），留出余量
REPLY_TIMEOUT = 3.0

_COMMANDS: dict[Host, str] = {Host.A: "a", Host.B: "b", Host.NONE: "x"}

#: 按主机查询「安全弹出锁」是否启用的回调。
#: 返回 False 时切换**跳过**该主机上的安全弹出（仅告警，不中止）。
#: 由上层注入：Host A 跟随本机桥接服务运行态，Host B 跟随远程桥接配置。
EjectLock = Callable[[Host], bool]


# --------------------------------------------------------------------------- #
# 抽象
# --------------------------------------------------------------------------- #


class BleLike(Protocol):
    """编排层需要的 BLE 能力（真实实现见 :mod:`usbswitch.core.ble`）。"""

    @property
    def is_connected(self) -> bool: ...

    async def send(self, command: str, *, timeout: float = ...) -> str: ...


class Ejector(Protocol):
    """对指定 Host 执行安全弹出。失败必须抛 :class:`~usbswitch.core.errors.EjectError`。

    真实实现见 :class:`usbswitch.core.ejector.BridgeEjector`（走桥接服务的 HTTP 接口）。
    """

    def eject_for(self, host: Host) -> None: ...


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SwitchResult:
    host: Host
    message: str
    #: 成功执行了安全弹出的主机
    ejected: tuple[Host, ...] = field(default_factory=tuple)
    #: True 表示本次未下发任何命令（幂等短路）
    skipped: bool = False


# --------------------------------------------------------------------------- #
# 编排器
# --------------------------------------------------------------------------- #


class SwitchOrchestrator:
    def __init__(
        self,
        *,
        ble: BleLike,
        ejector: Ejector,
        config: AppConfig,
        on_log=None,
        eject_lock: EjectLock | None = None,
    ) -> None:
        self._ble = ble
        self._ejector = ejector
        self._config = config
        self._on_log = on_log
        #: 按主机的安全弹出锁；None 表示不设锁（所有主机的弹出照常执行），
        #: 供脱离 GUI 的脚本 / 测试使用。
        self._eject_lock = eject_lock
        #: None 表示「尚未获知」——不能用 Host.NONE 代替，两者含义完全不同：
        #: NONE 是「确认两路都断开」，None 是「还不知道」。
        self._current_host: Host | None = None
        self._lock = asyncio.Lock()

    # -- 状态 --------------------------------------------------------------- #

    @property
    def current_host(self) -> Host | None:
        return self._current_host

    def apply_status(self, status: DeviceStatus) -> None:
        """用一次状态查询的结果校准本地认知。"""
        self._current_host = status.host

    async def refresh_status(self) -> DeviceStatus:
        """主动查询设备状态（固件命令 ``s``）。"""
        reply = await self._ble.send("s", timeout=REPLY_TIMEOUT)
        status = DeviceStatus.parse(reply)
        if status is None:
            raise BleNoResponseError(
                f"无法解析状态报文: {reply!r}",
                hint="请确认固件版本与客户端匹配",
            )
        self.apply_status(status)
        return status

    # -- 主流程 ------------------------------------------------------------- #

    async def switch_to(self, target: Host) -> SwitchResult:
        """切换到目标主机。

        Raises:
            BleError: 蓝牙未连接。
            EjectError: 安全弹出失败（**切换被中止，未触碰 VBUS**）。
            BleCommandRejectedError: 固件回复 FAIL（目标主机未接入）。
            BleNoResponseError: 超时未收到回复，切换结果未知。
        """
        async with self._lock:
            if not self._ble.is_connected:
                raise BleError("蓝牙未连接", hint="请先点击「连接设备」")

            if target is not Host.NONE and target is self._current_host:
                return SwitchResult(
                    host=target,
                    message=f"当前已经是 {target.label}，未下发命令",
                    skipped=True,
                )

            ejected = await self._safe_eject()
            reply = await self._ble.send(_COMMANDS[target], timeout=REPLY_TIMEOUT)
            return self._interpret(target, reply, ejected)

    # -- 内部 --------------------------------------------------------------- #

    async def _safe_eject(self) -> tuple[Host, ...]:
        """在当前（或所有可能）主机上执行安全弹出。任何失败都会向上抛出。

        为什么要有「对所有可能主机」这一支：刚启动时 ``_current_host`` 还是
        None（尚未查询）。此时若只跳过弹出，就有可能在 U 盘仍挂载的情况下切换。
        对两端都发一次请求是安全的 —— 没有 U 盘的那端会返回 warning 而非错误。

        每个主机先过**安全弹出锁**（Host A 跟随本机桥接服务、Host B 跟随远程
        桥接配置）：锁未启用的主机跳过弹出并告警，但**不中止**切换 ——
        「桥接服务未启动」本身就是用户明确选择的运行状态。
        """
        if self._current_host is None:
            candidates = [Host.A, Host.B]
        elif self._current_host is Host.NONE:
            # 两路 VBUS 均已断开，U 盘不可能被任何主机挂载
            candidates = []
        else:
            candidates = [self._current_host]

        done: list[Host] = []
        for host in candidates:
            if not self._eject_enabled(host):
                message = f"{host.label} 安全弹出锁未启用，跳过该主机上的安全弹出"
                log.warning(message)
                self._emit("warning", message)
                continue

            self._emit("info", f"正在 {host.label} 上安全弹出 U 盘 …")
            # ejector 是同步的 HTTP 调用，最坏可到 20 s，必须挪出事件循环，
            # 否则整个 BLE 重连与界面刷新都会被它卡住。
            await asyncio.to_thread(self._ejector.eject_for, host)
            done.append(host)
        return tuple(done)

    def _eject_enabled(self, host: Host) -> bool:
        if self._eject_lock is None:
            return True
        return bool(self._eject_lock(host))

    def _interpret(self, target: Host, reply: str, ejected: tuple[Host, ...]) -> SwitchResult:
        kind = classify_reply(reply)

        if kind is ReplyKind.FAIL:
            raise BleCommandRejectedError(
                reply,
                hint=f"{target.label} 可能未接入或未上电，设备已回到断开状态",
            )
        if kind not in (ReplyKind.OK, ReplyKind.STATUS):
            raise BleNoResponseError(
                f"设备返回了无法识别的报文: {reply!r}",
                hint="切换结果未知，建议点击「刷新状态」确认",
            )

        status = DeviceStatus.parse(reply)
        if status is not None:
            self._current_host = status.host
        else:
            self._current_host = target

        return SwitchResult(host=self._current_host or target, message=reply, ejected=ejected)

    def _emit(self, level: str, message: str) -> None:
        if self._on_log:
            self._on_log(level, message)
