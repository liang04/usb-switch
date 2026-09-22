"""顶栏 —— 设备连接状态与扫描 / 连接按钮。

从「设备」分组框变成一条常驻顶栏：设备连接是所有功能的前提，
把它留在滚动区里意味着用户滚动到下方时看不到这个前提是否成立。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from ..theme import THEME
from ..widgets import _Dot


class DevicePanel(QFrame):
    connectRequested = Signal(bool)
    scanRequested = Signal()

    def __init__(self, device_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TopBar")
        self._device_name = device_name
        self._want_connected = False

        self._dot = _Dot(10)
        self._label = QLabel(f"未连接  ·  {device_name}")

        self._btn_scan = QPushButton("扫描")
        self._btn_scan.setToolTip("只查看附近有哪些 USB-Switch，不会建立连接")
        self._btn_scan.clicked.connect(self.scanRequested.emit)
        self._btn_connect = QPushButton("连接设备")
        self._btn_connect.setObjectName("Primary")
        self._btn_connect.setToolTip("扫描并连接设备，断线后自动重连")
        self._btn_connect.clicked.connect(self._on_connect_clicked)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(10)
        layout.addWidget(self._dot)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._btn_scan)
        layout.addWidget(self._btn_connect)

    # -- 外部驱动 ----------------------------------------------------------- #

    def set_connected(self, connected: bool) -> None:
        self._want_connected = connected
        if connected:
            self.set_busy(f"已连接  ·  {self._device_name}", THEME["success"])
            self._btn_connect.setText("断开连接")
            self._btn_connect.setObjectName("")
        else:
            self.set_busy(f"未连接  ·  {self._device_name}", THEME["idle"])
            self._btn_connect.setText("连接设备")
            self._btn_connect.setObjectName("Primary")
        self._refresh_style()

    def set_busy(self, text: str, color: str) -> None:
        self._label.setText(text)
        self._dot.set_color(color)

    def set_scanning(self, scanning: bool) -> None:
        self._btn_scan.setEnabled(not scanning)
        self._btn_scan.setText("扫描中…" if scanning else "扫描")

    # -- 内部 --------------------------------------------------------------- #

    def _on_connect_clicked(self) -> None:
        self.connectRequested.emit(not self._want_connected)

    def _refresh_style(self) -> None:
        # 改 objectName 后必须重新应用样式表，否则 QSS 选择器不会重新匹配
        self._btn_connect.style().unpolish(self._btn_connect)
        self._btn_connect.style().polish(self._btn_connect)
