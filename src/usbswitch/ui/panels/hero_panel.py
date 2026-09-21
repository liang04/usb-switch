"""U 盘连接面板 —— 界面的主视觉。

**为什么把切换从按钮改成卡片。**
原实现的切换是三个并排按钮（`→ Host A` / `→ Host B` / `断开 U 盘`），
而「这一路 VBUS 通没通」「检测位插没插」要跑到下方「状态」区去对照 ——
判断一次切换要做两次视线跳转。卡片把两件事画在同一个视觉单元里。

**为什么自带刷新与时间戳。**
状态是异步刷新的，用户必须能分辨「看到的是最新值」还是「三分钟前的残留」。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ...core.models import DeviceStatus, Host
from ..widgets import HeroCard

FALLBACK_CARD_LINE = "两路 VBUS 均断电"


class HeroPanel(QWidget):
    switchRequested = Signal(str)  # Host.value
    refreshRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._enabled = False
        self._busy = False

        title = QLabel("U 盘连接")
        title.setObjectName("HeroTitle")
        self._subtitle = QLabel("尚未获取状态")
        self._subtitle.setObjectName("HeroSubtitle")

        self._btn_refresh = QPushButton("刷新状态")
        self._btn_refresh.setObjectName("Ghost")
        self._btn_refresh.clicked.connect(self.refreshRequested.emit)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(10)
        head.addWidget(title)
        head.addWidget(self._subtitle)
        head.addStretch(1)
        head.addWidget(self._btn_refresh)

        self._cards: dict[Host, HeroCard] = {
            Host.A: HeroCard("Host A"),
            Host.B: HeroCard("Host B"),
        }
        for host, card in self._cards.items():
            card.clicked.connect(lambda h=host: self.switchRequested.emit(h.value))

        self._off = HeroCard("断开 U 盘", variant="off")
        self._off.set_line(0, FALLBACK_CARD_LINE, "idle")
        self._off.hide_line(1)
        # 子控件被设为鼠标透明，卡片可点；但「断开」是危险操作，
        # 点击后由主窗口再弹一次确认（见 main_window._on_switch_requested）
        self._off.clicked.connect(lambda: self.switchRequested.emit(Host.NONE.value))

        cards = QHBoxLayout()
        cards.setContentsMargins(0, 0, 0, 0)
        cards.setSpacing(12)
        for card in (*self._cards.values(), self._off):
            cards.addWidget(card, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addLayout(head)
        layout.addLayout(cards)

        self.reset()

    # -- 只读 --------------------------------------------------------------- #

    def card(self, host: Host) -> HeroCard:
        """按角色取卡片（测试与主窗口用；不暴露内部容器）。"""
        return self._cards[host]

    @property
    def off_card(self) -> HeroCard:
        return self._off

    # -- 外部驱动 ----------------------------------------------------------- #

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self._subtitle.setText("设备未连接")
        self._apply_interactivity()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._apply_interactivity()

    def set_current(self, host: Host | None) -> None:
        for candidate, card in self._cards.items():
            active = candidate is host
            card.set_card_state("current" if active else "")
            card.set_badge("当前" if active else "", "info")

    def set_switching(self, host: Host | None, step: str = "") -> None:
        """切换进行中：在目标卡片上打标，并把步骤写在标题右侧。

        弹盘最坏要等 8 秒，没有进度反馈的话界面看起来就是卡死了。
        """
        if host is None:
            return
        for candidate, card in self._cards.items():
            if candidate is host:
                card.set_badge("切换中…", "info")
        if step:
            self._subtitle.setText(step)

    def mark_failed(self, host: Host | None) -> None:
        """把失败标在**那一张**卡片上，用户不必去日志里找是切到哪儿失败的。

        该标记会在下一次状态刷新时被 ``apply()`` 清掉。
        """
        card = self._cards.get(host) if host is not None else None
        if card is None:
            return
        card.set_card_state("failed")
        card.set_badge("失败", "error")

    def apply(self, status: DeviceStatus) -> None:
        self._subtitle.setText(f"状态更新于 {datetime.now():%H:%M:%S}")
        for host, card in self._cards.items():
            detected = status.det_a if host is Host.A else status.det_b
            active = status.host is host
            card.set_card_state("current" if active else "")
            card.set_badge("当前" if active else "", "info")
            card.set_line(
                0,
                "VBUS 已接通" if active else "VBUS 断开",
                "ok" if active else "idle",
            )
            card.set_line(
                1,
                "检测位 已插入" if detected else "检测位 无",
                "ok" if detected else "idle",
            )

    def reset(self) -> None:
        """清空为「无数据」。

        绝不沿用上一次的结果 —— 否则用户会以为 U 盘还挂在 Host A，实际早就断了。
        """
        self._subtitle.setText("尚未获取状态")
        for card in self._cards.values():
            card.set_card_state("")
            card.set_badge("")
            card.set_line(0, "VBUS —", "idle")
            card.set_line(1, "检测位 —", "idle")
        self._off.set_card_state("")

    # -- 内部 --------------------------------------------------------------- #

    def _apply_interactivity(self) -> None:
        live = self._enabled and not self._busy
        for card in (*self._cards.values(), self._off):
            card.setEnabled(live)
            card.set_clickable(live)
        self._btn_refresh.setEnabled(self._enabled and not self._busy)
        self._btn_refresh.setText("处理中…" if self._busy else "刷新状态")
