"""日志面板 —— 实时追加、复制、导出。

导出与「打开日志目录」都是有意的：这个程序出问题时，用户能提供的最有用
东西就是日志；而日志同时存在两处（界面里的实时缓冲、磁盘上的轮转文件），
让用户自己去 `%APPDATA%` 里翻是不现实的。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.paths import log_dir
from ..widgets import LogView


class LogPanel(QGroupBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("日志", parent)

        self._view = LogView()

        self._btn_clear = QPushButton("清空")
        self._btn_clear.clicked.connect(self._view.clear)
        self._btn_copy = QPushButton("复制全部")
        self._btn_copy.clicked.connect(self._copy_all)
        self._btn_export = QPushButton("导出…")
        self._btn_export.clicked.connect(self._export)
        self._btn_open = QPushButton("日志目录")
        self._btn_open.setToolTip("打开磁盘上的日志目录（含历史日志与崩溃转储）")
        self._btn_open.clicked.connect(self._open_log_dir)

        toolbar = QHBoxLayout()
        toolbar.addStretch(1)
        for button in (self._btn_clear, self._btn_copy, self._btn_export, self._btn_open):
            toolbar.addWidget(button)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addWidget(self._view, 1)
        layout.addLayout(toolbar)

    # -- 外部驱动 ----------------------------------------------------------- #

    def append(self, level: str, message: str) -> None:
        self._view.append_line(level, message)

    def load_lines(self, lines: list[str]) -> None:
        """回填启动前的历史日志（会先清空当前内容）。"""
        self._view.load_lines(list(lines))

    def text(self) -> str:
        return self._view.toPlainText()

    # -- 导出 --------------------------------------------------------------- #

    def default_export_path(self) -> Path:
        """默认导出位置：日志目录 + 带时间戳的文件名。

        放在日志目录而不是「文档」：用户已经知道去哪找，也方便附在问题反馈里。
        """
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return log_dir() / f"usbswitch-{stamp}.log"

    def export_to(self, path: Path) -> int:
        """把当前界面里的日志写入文件，返回写入行数。

        单独抽出来是为了能被测试直接调用 —— 弹文件对话框的那条路径测不了。
        """
        text = self._view.toPlainText()
        target = Path(path)
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return len(text.splitlines())

    def _export(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            self, "导出日志", str(self.default_export_path()), "日志文件 (*.log);;所有文件 (*)"
        )
        if not chosen:
            return
        try:
            lines = self.export_to(Path(chosen))
        except OSError as exc:
            self.append("error", f"日志导出失败：{exc}")
            return
        self.append("success", f"已导出 {lines} 行日志到 {chosen}")

    def _open_log_dir(self) -> None:
        directory = log_dir()
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
            self.append("warning", f"无法打开目录，请手工访问：{directory}")

    def _copy_all(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self._view.toPlainText())
        self.append("info", "日志已复制到剪贴板")
