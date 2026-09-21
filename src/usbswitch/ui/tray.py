"""系统托盘。

托盘是「关掉窗口后程序还在」的唯一逃生口 —— 因此菜单里的**「退出」是唯一
真正结束进程的入口**，它会连同本地桥接服务一起收干净。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..core.models import BridgeRunState, Host
from .icons import dot_icon
from .theme import THEME

log = logging.getLogger(__name__)


class TrayIcon(QSystemTrayIcon):
    toggleWindowRequested = Signal()
    switchRequested = Signal(str)      # Host.value
    refreshRequested = Signal()
    autostartToggled = Signal(bool)
    quitRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._busy = False
        self._connected = False
        self._icon_key = "idle"

        menu = QMenu()

        self._act_toggle = menu.addAction("显示/隐藏主窗口")
        self._act_toggle.triggered.connect(self.toggleWindowRequested.emit)

        menu.addSeparator()
        self._act_host = menu.addAction("当前主机：—")
        self._act_host.setEnabled(False)
        self._act_bridge = menu.addAction("本地桥接：—")
        self._act_bridge.setEnabled(False)

        menu.addSeparator()
        self._host_actions: dict[Host, object] = {}
        for host in (Host.A, Host.B):
            action = menu.addAction(f"   切换到 {host.label}")
            action.triggered.connect(lambda _=False, h=host: self.switchRequested.emit(h.value))
            self._host_actions[host] = action

        self._act_off = menu.addAction("   断开 U 盘")
        self._act_off.triggered.connect(lambda: self.switchRequested.emit(Host.NONE.value))

        self._switch_actions = [*self._host_actions.values(), self._act_off]

        menu.addSeparator()
        self._act_autostart = menu.addAction("开机自启")
        self._act_autostart.setCheckable(True)
        # 接槽而不是 `toggled.connect(self.autostartToggled.emit)`：Qt 的原生信号
        # 常带默认参数（`clicked(bool = false)` 就因此多出一个无参重载），直连
        # `.emit` 会挑中无参那个、把参数吞掉。值一律从控件现读。
        self._act_autostart.toggled.connect(self._on_autostart_toggled)

        menu.addSeparator()
        menu.addAction("退出").triggered.connect(self.quitRequested.emit)

        self.setContextMenu(menu)
        self._menu = menu
        self.setIcon(dot_icon(THEME["idle"]))
        self.setToolTip("USB Switch Console")
        self.activated.connect(self._on_activated)

    # -- 交互 --------------------------------------------------------------- #

    def _on_autostart_toggled(self) -> None:
        """用户点了「开机自启」—— 值从 QAction 现读，不依赖 `toggled` 的参数。"""
        self.autostartToggled.emit(self._act_autostart.isChecked())

    def _on_activated(self, reason) -> None:
        # 左键单击与双击都做同一件事：切换窗口显示/隐藏。
        # 不区分二者，避免用户在不同平台上形成互相矛盾的肌肉记忆。
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.toggleWindowRequested.emit()

    # -- 状态同步 ----------------------------------------------------------- #

    def apply_connection(self, connected: bool, *, busy: bool = False) -> None:
        self._busy = busy
        self._connected = connected
        self._apply_icon()
        self._sync_switch_enabled(connected)

    def refresh_icon(self) -> None:
        """按**当前**主题重画托盘图标。

        托盘图标是 ``QPainter`` 现画的位图，不吃 QSS —— 换主题后不显式重画，
        托盘会一直停在旧色上（暗色桌面里挂着一颗明色主题的深色圆点）。
        """
        self._apply_icon()

    def _apply_icon(self) -> None:
        if self._busy:
            key, tip = "warning", "正在处理…"
        elif self._connected:
            key, tip = "success", "已连接 USB-Switch"
        else:
            key, tip = "idle", "未连接"
        self._icon_key = key
        self.setIcon(dot_icon(THEME[key]))
        self.setToolTip(f"USB Switch Console — {tip}")

    def apply_host(self, host: Host | None) -> None:
        self._act_host.setText(f"当前主机：{host.label if host else '—'}")
        for candidate, action in self._host_actions.items():
            mark = "✓" if candidate is host else "　"
            action.setText(f"{mark} 切换到 {candidate.label}")

    def apply_bridge(self, state: BridgeRunState, *, healthy: bool = False) -> None:
        if state is BridgeRunState.RUNNING:
            text = "运行中" if healthy else "已启动（未响应）"
        elif state is BridgeRunState.ERROR:
            text = "异常"
        elif state is BridgeRunState.STARTING:
            text = "启动中…"
        else:
            text = "已停止"
        self._act_bridge.setText(f"本地桥接：{text}")

    def set_autostart(self, enabled: bool) -> None:
        """程序化同步勾选状态。

        必须屏蔽信号：否则会立刻把我们自己的 autostartToggled 再发一遍，
        形成「设置 → 触发 → 再设置」的回环。
        """
        self._act_autostart.blockSignals(True)
        self._act_autostart.setChecked(enabled)
        self._act_autostart.blockSignals(False)

    def notify(self, title: str, message: str) -> None:
        self.showMessage(title, message, dot_icon(THEME["accent"]), 4000)

    # -- 内部 --------------------------------------------------------------- #

    def _sync_switch_enabled(self, connected: bool) -> None:
        enabled = connected and not self._busy
        for action in self._switch_actions:
            action.setEnabled(enabled)
