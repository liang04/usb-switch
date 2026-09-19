"""选项面板。

两个开关都是「配置意图」类设置，改一次存一次。

安全弹出不再有全局互锁开关：每个主机的「安全弹出锁」是**派生状态**——
Host A 跟随本机桥接服务的启停，Host B 跟随远程桥接配置的有无，
两者分别在「桥接服务」「远程桥接服务」分区里可见可控。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGroupBox, QLabel, QVBoxLayout, QWidget

from ...core.models import AppConfig
from ..widgets import ToggleSwitch

_HINT_AUTOSTART = "开机后静默启动到托盘，不弹出窗口。"
_HINT_CLOSE = "关闭主窗口时留在托盘继续运行；取消勾选则关闭即退出程序。"


class _OptionRow:
    """一行开关 + 说明文字。"""

    def __init__(self, panel: QGroupBox, title: str, checked: bool, hint: str, slot) -> None:
        self.switch = ToggleSwitch(title)
        self.switch.setChecked(checked)
        self.switch.toggled.connect(slot)

        self.hint = QLabel(hint)
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)

        layout = panel.layout()
        assert isinstance(layout, QVBoxLayout)
        layout.addWidget(self.switch)
        layout.addWidget(self.hint)


class OptionsPanel(QGroupBox):
    autostartChanged = Signal(bool)
    closeToTrayChanged = Signal(bool)

    def __init__(
        self,
        config: AppConfig,
        *,
        autostart_enabled: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("选项", parent)
        QVBoxLayout(self).setSpacing(6)

        self._autostart = _OptionRow(
            self, "开机自启", autostart_enabled,
            _HINT_AUTOSTART, self.autostartChanged.emit,
        )
        self._close_tray = _OptionRow(
            self, "关闭主窗口时最小化到托盘", config.window.close_to_tray,
            _HINT_CLOSE, self.closeToTrayChanged.emit,
        )

    # -- 外部同步 ----------------------------------------------------------- #

    def set_autostart(self, enabled: bool) -> None:
        """以注册表**实际**状态回填。

        必须屏蔽信号：`setChecked` 会发出 `toggled`，不屏蔽就会再写一遍注册表，
        形成「设置 → 触发 → 再设置」的回环。
        """
        switch = self._autostart.switch
        switch.blockSignals(True)
        switch.setChecked(enabled)
        switch.blockSignals(False)
