"""可复用的小控件。

界面从「七张等权重卡片」改为「顶栏 + Hero 卡片 + 折叠分区 + 状态栏」后，
新增了四类控件：:class:`HeroCard`（承载状态的切换卡片）、
:class:`CollapsibleSection`（可折叠分区）、:class:`StatusBar`（底部常驻状态）、
以及若干零件（:class:`Chip` / :class:`Chevron` / :class:`ElidedLabel`）。
"""

from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPolygonF
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import build_info
from .theme import LOG_COLORS, STATE_FG, THEME


def repolish(widget: QWidget) -> None:
    """改过动态属性后必须重新应用样式表，否则 QSS 选择器不会重新匹配。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


# --------------------------------------------------------------------------- #
# 零件
# --------------------------------------------------------------------------- #


class _Dot(QWidget):
    """实心圆点。"""

    def __init__(self, size: int = 12, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self._color = THEME["idle"]
        self.setFixedSize(size, size)

    def set_color(self, color: str) -> None:
        if color != self._color:
            self._color = color
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self._color))
        inset = max(1.0, self._size * 0.08)
        diameter = self._size - 2 * inset
        painter.drawEllipse(QRectF(inset, inset, diameter, diameter))


class Chevron(QWidget):
    """折叠指示三角。用绘制而非 `▸` 字符 —— 后者在不同字体下大小与基线都不一致。"""

    SIZE = 16

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._open = False
        self.setFixedSize(self.SIZE, self.SIZE)

    def set_open(self, is_open: bool) -> None:
        if is_open != self._open:
            self._open = is_open
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(THEME["muted"]))
        if self._open:
            points = [QPointF(4, 6.5), QPointF(12, 6.5), QPointF(8, 11)]
        else:
            points = [QPointF(6.5, 4), QPointF(11, 8), QPointF(6.5, 12)]
        painter.drawPolygon(QPolygonF(points))


class Chip(QLabel):
    """状态徽标。只负责设属性，颜色由 QSS 的属性选择器决定。"""

    def __init__(self, text: str = "", state: str = "idle", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Chip")
        self.setAlignment(Qt.AlignCenter)
        self.setProperty("state", state)
        self.setVisible(bool(text))

    def set_state(self, text: str, state: str = "idle") -> None:
        self.setText(text)
        self.setVisible(bool(text))
        self.setProperty("state", state)
        repolish(self)


class ElidedLabel(QLabel):
    """会自动省略的 QLabel。

    QLabel 不省略长文本，长串会把布局撑开 —— 在折叠区的摘要行里那是灾难性的
    （窗口被撑到屏幕外）。这里保留完整文本，只在尺寸变化时把**显示**文本换成
    省略版；颜色仍由 QSS 控制，所以不影响主题。

    **注意 `text()` 返回的是省略后的显示文本**（QLabel 的语义），要拿原串用
    :meth:`fullText` —— 否则在窗口还没显示过（宽度是默认值）的测试里会读到
    被截断的字符串。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        super().setText(text)
        self.setToolTip(text)

    def setText(self, text: str) -> None:  # noqa: N802 —— Qt 命名
        self._full = text
        self.setToolTip(text)
        self._apply_elide()

    def fullText(self) -> str:  # noqa: N802
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = self.width() - 2
        if width <= 0:
            super().setText(self._full)
            return
        super().setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, width))


class StatusLight(QWidget):
    """一个彩色圆点 + 一行说明文字。"""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dot = _Dot()
        self._label = QLabel(text)
        self._label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._dot)
        layout.addWidget(self._label, 1)

    def set_state(self, text: str, color: str) -> None:
        self._dot.set_color(color)
        self._label.setText(text)

    def set_state_key(self, text: str, state: str = "idle") -> None:
        """按语义状态着色，避免调用方自己查 THEME。"""
        self.set_state(text, STATE_FG.get(state, THEME["idle"]))


# --------------------------------------------------------------------------- #
# 卡片与分区
# --------------------------------------------------------------------------- #


class _CardRow(QWidget):
    """卡片里的一行：小圆点 + 说明文字。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dot = _Dot(9)
        self._label = QLabel("")
        self._label.setObjectName("CardLine")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        layout.addWidget(self._dot)
        layout.addWidget(self._label, 1)

    def set_text(self, text: str, state: str = "idle") -> None:
        self._label.setText(text)
        self._dot.set_color(STATE_FG.get(state, THEME["idle"]))


class HeroCard(QFrame):
    """一张可点击的切换卡片，自身承载状态。

    为什么不用按钮：切换目标需要同时表达「是什么」「当前是不是它」「这一路通不通」——
    按钮只能表达前两者。卡片能把 VBUS 与检测位直接画上去，用户不必再去状态区对照。
    """

    clicked = Signal()

    def __init__(
        self,
        title: str,
        *,
        variant: str = "host",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("HeroCard")
        self.setProperty("variant", variant)
        self.setProperty("state", "")
        self.setCursor(Qt.PointingHandCursor)
        self._clickable = True

        self._title = QLabel(title)
        self._title.setObjectName("CardTitle")
        self._badge = Chip("")

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(self._title)
        head.addWidget(self._badge)
        head.addStretch(1)

        self._rows = (_CardRow(), _CardRow())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        layout.addLayout(head)
        for row in self._rows:
            layout.addWidget(row)
        layout.addStretch(1)

        # 让整张卡片成为一个点击目标：子控件不吞鼠标事件，否则只有「缝隙」能点中
        for child in self.findChildren(QWidget):
            child.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    # -- 外部驱动 ----------------------------------------------------------- #

    def set_line(self, index: int, text: str, state: str = "idle") -> None:
        if 0 <= index < len(self._rows):
            row = self._rows[index]
            row.setVisible(True)
            row.set_text(text, state)

    def hide_line(self, index: int) -> None:
        if 0 <= index < len(self._rows):
            self._rows[index].setVisible(False)

    def set_badge(self, text: str, state: str = "idle") -> None:
        self._badge.set_state(text, state)

    def set_card_state(self, state: str) -> None:
        """``""`` 普通 / ``current`` 当前挂载 / ``failed`` 切换失败 / ``busy`` 切换中。"""
        if state == self.property("state"):
            return
        self.setProperty("state", state)
        repolish(self)

    def set_clickable(self, clickable: bool) -> None:
        self._clickable = clickable
        self.setCursor(Qt.PointingHandCursor if clickable else Qt.ArrowCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        inside = self.rect().contains(event.position().toPoint())
        if self._clickable and event.button() == Qt.LeftButton and inside:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _ClickableRow(QWidget):
    """整行可点。子控件被设为鼠标透明，所以点哪儿都算点在行上。"""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class CollapsibleSection(QWidget):
    """折叠分区：一行「标题 + 摘要 + 徽标」的表头，下面挂内容。

    表头常驻，内容按需展开 —— 这是把「一次性配置」从首屏收走的关键。
    """

    toggled = Signal(str, bool)  # (分区 id, 是否展开)

    TITLE_WIDTH = 76

    def __init__(
        self,
        key: str,
        title: str,
        content: QWidget,
        *,
        expanded: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Section")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setProperty("alert", "false")

        self._key = key
        self._expanded = expanded

        # 内容区自身的 QGroupBox 标题会和分区头重复，且卡片边框会造成「框中框」
        if isinstance(content, QGroupBox):
            content.setTitle("")
            content.setProperty("bare", "true")

        self._chevron = Chevron()
        self._title = QLabel(title)
        self._title.setObjectName("SectionTitle")
        self._title.setFixedWidth(self.TITLE_WIDTH)
        self._summary = ElidedLabel("")
        self._summary.setObjectName("SectionSummary")
        self._chip = Chip("")

        header = _ClickableRow()
        header.setObjectName("SectionHeader")
        header.clicked.connect(self.toggle)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 10, 16, 10)
        header_layout.setSpacing(10)
        header_layout.addWidget(self._chevron)
        header_layout.addWidget(self._title)
        header_layout.addWidget(self._summary, 1)
        header_layout.addWidget(self._chip)
        for child in header.findChildren(QWidget):
            child.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(16, 2, 16, 14)
        body_layout.setSpacing(0)
        body_layout.addWidget(content)
        self._body = body

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(body)

        self._body.setVisible(expanded)
        self._chevron.set_open(expanded)

    # -- 外部驱动 ----------------------------------------------------------- #

    @property
    def key(self) -> str:
        return self._key

    @property
    def body(self) -> QWidget:
        """展开时显示的内容容器（测试里用来检查内容有没有被挤出边界）。"""
        return self._body

    def is_expanded(self) -> bool:
        return self._expanded

    def toggle(self) -> None:
        self.set_expanded(not self._expanded, emit=True)

    def set_expanded(self, expanded: bool, *, emit: bool = False) -> None:
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self._body.setVisible(expanded)
        self._chevron.set_open(expanded)
        if emit:
            self.toggled.emit(self._key, expanded)

    def set_summary(self, text: str) -> None:
        self._summary.setText(text)

    def summary_text(self) -> str:
        return self._summary.fullText()

    def set_chip(self, text: str, state: str = "idle") -> None:
        self._chip.set_state(text, state)

    def chip_text(self) -> str:
        return self._chip.text()

    def set_alert(self, alert: bool) -> None:
        value = "true" if alert else "false"
        if value == self.property("alert"):
            return
        self.setProperty("alert", value)
        repolish(self)


# --------------------------------------------------------------------------- #
# 状态栏
# --------------------------------------------------------------------------- #


def _build_tooltip() -> str:
    """鼠标悬停时给出完整构建信息，必要时把「产物落后于源码」直接说出来。"""
    info = build_info.current()
    lines = [info.describe()]
    problem = build_info.freshness_problem()
    if problem:
        lines += ["", "⚠ " + problem]
    return "\n".join(lines)


class StatusBar(QWidget):
    """底部常驻状态栏。

    存在的理由：分区可以滚动、可以折叠，但「设备连没连」「U 盘在哪」这两件事
    必须任何时刻都看得到。
    """

    SEGMENTS: tuple[tuple[str, str], ...] = (
        ("device", "设备"),
        ("host", "当前主机"),
        ("bridge", "本地桥接"),
        ("tunnel", "远程隧道"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatusBar")
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 7, 16, 7)
        layout.setSpacing(0)

        self._items: dict[str, tuple[_Dot, QLabel]] = {}
        for index, (key, label) in enumerate(self.SEGMENTS):
            if index:
                divider = QWidget()
                divider.setObjectName("StatusDivider")
                divider.setAttribute(Qt.WA_StyledBackground, True)
                divider.setFixedSize(1, 12)
                layout.addSpacing(16)
                layout.addWidget(divider)
                layout.addSpacing(16)

            dot = _Dot(8)
            text = QLabel(f"{label} —")
            text.setObjectName("StatusText")

            item = QWidget()
            item_layout = QHBoxLayout(item)
            item_layout.setContentsMargins(0, 0, 0, 0)
            item_layout.setSpacing(6)
            item_layout.addWidget(dot)
            item_layout.addWidget(text)

            self._items[key] = (dot, text)
            layout.addWidget(item)

        layout.addStretch(1)

        # 构建身份常驻在最右侧。它存在的唯一目的是回答「我现在跑的是哪一版」——
        # 「改了源码但双击的是旧 exe」这种情况程序不会报错，只会让人觉得
        # 「界面没变」。有这一行就能一眼看出来。
        self._build = QLabel(build_info.current().short())
        self._build.setObjectName("StatusBuild")
        self._build.setToolTip(_build_tooltip())
        layout.addWidget(self._build)

    def set_segment(self, key: str, text: str, state: str = "idle") -> None:
        item = self._items.get(key)
        if item is None:
            return
        dot, label = item
        dot.set_color(STATE_FG.get(state, THEME["idle"]))
        label.setText(text)

    def text(self, key: str) -> str:
        """读取某一段当前显示的文字（测试用，避免触碰内部控件）。"""
        item = self._items.get(key)
        return item[1].text() if item else ""

    def build_text(self) -> str:
        """最右侧的构建身份标记。"""
        return self._build.text()


# --------------------------------------------------------------------------- #
# 表单与日志
# --------------------------------------------------------------------------- #


class KeyValueGrid(QWidget):
    """两列的「标签 / 值」网格，值那一列永远不显示假数据。"""

    PLACEHOLDER = "—"

    def __init__(self, rows: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QGridLayout

        self._values: dict[str, QLabel] = {}
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(7)

        for index, name in enumerate(rows):
            key = QLabel(name)
            key.setObjectName("Muted")
            value = QLabel(self.PLACEHOLDER)
            value.setObjectName("Value")
            layout.addWidget(key, index, 0)
            layout.addWidget(value, index, 1)
            self._values[name] = value

        layout.setColumnStretch(1, 1)

    def set_value(self, name: str, text: str, color: str | None = None) -> None:
        label = self._values.get(name)
        if label is None:
            return
        label.setText(text)
        label.setStyleSheet(f"color: {color};" if color else "")

    def reset(self) -> None:
        for label in self._values.values():
            label.setText(self.PLACEHOLDER)
            label.setStyleSheet("")


class LogView(QPlainTextEdit):
    """只读日志视图，按级别着色并自动滚到底部。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(3000)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setMinimumHeight(160)

    def append_line(self, level: str, message: str) -> None:
        color = LOG_COLORS.get(level, LOG_COLORS["info"])
        stamp = datetime.now().strftime("%H:%M:%S")
        text = html.escape(f"[{stamp}] {message}")
        self.appendHtml(f'<span style="color:{color}">{text}</span>')
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def load_lines(self, lines: list[str]) -> None:
        self.clear()
        for line in lines:
            self.appendPlainText(line)
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())


class ToggleSwitch(QAbstractButton):
    """细长开关控件。文字在右，开关在左。"""

    TRACK_WIDTH = 38
    TRACK_HEIGHT = 20

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._text = text
        self.setFixedHeight(24)

    def setText(self, text: str) -> None:  # noqa: N802 —— Qt 命名
        self._text = text
        self.updateGeometry()
        self.update()

    def text(self) -> str:  # noqa: N802
        return self._text

    def sizeHint(self) -> QSize:  # noqa: N802
        advance = self.fontMetrics().horizontalAdvance(self._text)
        return QSize(self.TRACK_WIDTH + 10 + advance, 24)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        height = self.TRACK_HEIGHT
        top = (self.height() - height) / 2
        track = QRectF(0, top, self.TRACK_WIDTH, height)

        on = self.isChecked()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(THEME["success"] if on else THEME["border_strong"]))
        painter.drawRoundedRect(track, height / 2, height / 2)

        knob_size = height - 4
        knob_x = self.TRACK_WIDTH - knob_size - 2 if on else 2
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QRectF(knob_x, top + 2, knob_size, knob_size))

        painter.setPen(QColor(THEME["text"]))
        text_rect = QRectF(self.TRACK_WIDTH + 10, 0, self.width() - self.TRACK_WIDTH - 10, self.height())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self._text)
