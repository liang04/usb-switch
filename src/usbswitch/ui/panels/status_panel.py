"""状态显示面板。

约定：**永远不显示假值**。没有数据就显示 `—`，绝不沿用上一次的结果 ——
否则用户会以为设备还连在 Host A，实际早就断了。
"""

from __future__ import annotations

from PySide6.QtWidgets import QGroupBox, QVBoxLayout, QWidget

from ...core.models import DeviceStatus, Host
from ..widgets import KeyValueGrid

_HOST_ROW = "当前主机"
_DET_A_ROW = "Host A 检测"
_DET_B_ROW = "Host B 检测"
_REPLY_ROW = "固件报文"


class StatusPanel(QGroupBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("状态", parent)
        self._grid = KeyValueGrid([_HOST_ROW, _DET_A_ROW, _DET_B_ROW, _REPLY_ROW])

        layout = QVBoxLayout(self)
        layout.addWidget(self._grid)

    def apply(self, status: DeviceStatus) -> None:
        # 传语义键而非色值：切主题后这一格会跟着变（色值会被固化在控件上）
        self._grid.set_value(
            _HOST_ROW,
            status.host.label,
            "warn" if status.host is Host.NONE else "ok",
        )
        self._set_detection(_DET_A_ROW, status.det_a)
        self._set_detection(_DET_B_ROW, status.det_b)
        self._grid.set_value(_REPLY_ROW, status.raw)

    def apply_raw(self, text: str) -> None:
        """报文无法解析时，至少把原文展示出来，便于排查。"""
        self._grid.set_value(_REPLY_ROW, text)

    def reset(self) -> None:
        self._grid.reset()

    def _set_detection(self, row: str, present: bool) -> None:
        self._grid.set_value(row, "已插入" if present else "无", "ok" if present else None)
