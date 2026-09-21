"""图标 —— **运行时绘制**，不引入任何二进制资源。

这样做的理由：托盘图标需要按连接状态变色（已连接 / 未连接 / 切换中），
准备三份 .ico 就要维护三个文件、还要处理打包时的资源路径；
而 `QPainter` 画一个圆点是十几行代码，且天然支持任意尺寸与颜色。
顺带也让 `resources/icons/` 保持为空，少一处打包配置。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from .theme import THEME

#: 托盘图标各平台会缩到 16~24 px，主形状必须足够粗才看得清
_TRAY_SIZE = 64
_APP_SIZE = 256


def _blank(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    return pixmap


def dot_icon(color: str, *, size: int = _TRAY_SIZE, ring: bool = True) -> QIcon:
    """实心圆点 + 可选内环。用于托盘状态。"""
    pixmap = _blank(size)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    margin = size * 0.14
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(QRectF(margin, margin, size - 2 * margin, size - 2 * margin))

    if ring:
        inset = size * 0.32
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 170), max(1.0, size * 0.05)))
        painter.drawEllipse(QRectF(inset, inset, size - 2 * inset, size - 2 * inset))

    painter.end()
    return QIcon(pixmap)


def app_icon(*, size: int = _APP_SIZE) -> QIcon:
    """程序图标：圆角方块 + 中心圆点 + 外环。"""
    pixmap = _blank(size)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    radius = size * 0.22
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(THEME["accent"]))
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    dot = size * 0.22
    painter.setBrush(QColor("#ffffff"))
    painter.drawEllipse(QRectF((size - dot) / 2, (size - dot) / 2, dot, dot))

    ring = size * 0.42
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QColor(255, 255, 255, 90), max(2.0, size * 0.03)))
    painter.drawEllipse(QRectF((size - ring) / 2, (size - ring) / 2, ring, ring))

    painter.end()
    return QIcon(pixmap)
