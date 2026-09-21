"""选项面板。

三个设置都是「配置意图」类，改一次存一次。

安全弹出不再有全局互锁开关：每个主机的「安全弹出锁」是**派生状态**——
Host A 跟随本机桥接服务的启停，Host B 跟随远程桥接配置的有无，
两者分别在「桥接服务」「远程桥接服务」分区里可见可控。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ...core.models import AppConfig
from ..theme import mode_labels
from ..widgets import ToggleSwitch

_HINT_AUTOSTART = "开机后静默启动到托盘，不弹出窗口。"
_HINT_CLOSE = "关闭主窗口时留在托盘继续运行；取消勾选则关闭即退出程序。"
_HINT_THEME = "「跟随系统」需要在 Windows 10/11 的「个性化 → 颜色」里设置。"


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
        assert layout is not None
        layout.addWidget(self.switch)
        layout.addWidget(self.hint)


class _ThemeRow:
    """一行「外观」选择：下拉框。

    用下拉框而不是开关，是因为有三个取值（跟随系统 / 浅色 / 深色）——
    「跟随系统」与「深色」不是「开 / 关」的关系。
    """

    def __init__(self, panel: QGroupBox, current: str, hint: str, slot) -> None:
        self.combo = QComboBox()
        for value, label in mode_labels().items():
            self.combo.addItem(label, value)
        index = self.combo.findData(current)
        self.combo.setCurrentIndex(index if index >= 0 else 0)
        # 接无参槽、从控件现读值 —— 直接 `.emit` 会挑中带默认参数的无参重载，
        # 参数被吞掉、槽静默不执行（09-20 的开关事故就是这个）。
        self.combo.currentIndexChanged.connect(slot)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(10)
        title = QLabel("界面外观")
        title.setFixedWidth(ToggleSwitch.TRACK_WIDTH + 10)
        row_layout.addWidget(title)
        # 短下拉框不能被拉满整行 —— QFormLayout/QHBoxLayout 默认会拉伸它
        row_layout.addWidget(self.combo)
        row_layout.addStretch(1)

        self.hint = QLabel(hint)
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)

        layout = panel.layout()
        assert layout is not None
        layout.addWidget(row)
        layout.addWidget(self.hint)

    def value(self) -> str:
        return self.combo.currentData()


class OptionsPanel(QGroupBox):
    autostartChanged = Signal(bool)
    closeToTrayChanged = Signal(bool)
    themeChanged = Signal(str)

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
        self._theme = _ThemeRow(
            self, config.window.theme,
            _HINT_THEME, self._on_theme_picked,
        )

    # -- 外部同步 ----------------------------------------------------------- #

    def _on_theme_picked(self) -> None:
        """值从下拉框现读，不依赖信号参数。"""
        self.themeChanged.emit(self._theme.value())

    def set_theme(self, value: str) -> None:
        """程序化回填外观下拉框（屏蔽信号，避免回环）。"""
        index = self._theme.combo.findData(value)
        if index < 0:
            return
        self._theme.combo.blockSignals(True)
        self._theme.combo.setCurrentIndex(index)
        self._theme.combo.blockSignals(False)

    def theme_value(self) -> str:
        return self._theme.value()

    def set_autostart(self, enabled: bool) -> None:
        """以注册表**实际**状态回填。

        必须屏蔽信号：`setChecked` 会发出 `toggled`，不屏蔽就会再写一遍注册表，
        形成「设置 → 触发 → 再设置」的回环。
        """
        switch = self._autostart.switch
        switch.blockSignals(True)
        switch.setChecked(enabled)
        switch.blockSignals(False)
