"""本地桥接服务面板。"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.models import BridgeRunState
from ..theme import THEME
from ..widgets import StatusLight


class BridgePanel(QGroupBox):
    startRequested = Signal()
    stopRequested = Signal()
    refreshRequested = Signal()

    def __init__(self, base_url: str, parent: QWidget | None = None) -> None:
        super().__init__("桥接服务", parent)
        self._base_url = base_url

        self._light = StatusLight(f"本地 · {base_url}")
        self._btn_start = QPushButton("启动")
        self._btn_stop = QPushButton("停止")
        self._btn_refresh = QPushButton("刷新")

        self._btn_start.clicked.connect(self.startRequested.emit)
        self._btn_stop.clicked.connect(self.stopRequested.emit)
        self._btn_refresh.clicked.connect(self.refreshRequested.emit)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self._light, 1)
        toolbar.addWidget(self._btn_start)
        toolbar.addWidget(self._btn_stop)
        toolbar.addWidget(self._btn_refresh)

        self._detail = QLabel("—")
        self._detail.setObjectName("Muted")
        self._detail.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addLayout(toolbar)
        layout.addWidget(self._detail)

        self.apply(BridgeRunState.STOPPED, None)

    # -- 外部驱动 ----------------------------------------------------------- #

    def apply(self, state: BridgeRunState, payload: dict | None) -> None:
        """payload 为 /status 的响应；None 表示探活未拿到响应。"""
        if state is BridgeRunState.RUNNING:
            self._btn_start.setEnabled(False)
            self._btn_stop.setEnabled(True)
            if payload and payload.get("ok"):
                drives = payload.get("drives") or []
                self._light.set_state(f"本地 · {self._base_url}  运行中", THEME["success"])
                shown = "、".join(f"{letter}:" for letter in drives) if drives else "（无）"
                self._detail.setText(f"本机可移动磁盘：{shown}")
            else:
                # 线程活着但 HTTP 不通 —— 必须与「运行中」区分开
                self._light.set_state(f"本地 · {self._base_url}  已启动但未响应", THEME["warning"])
                self._detail.setText("服务线程在跑，但 /status 无响应，请检查端口是否被占用")
        elif state is BridgeRunState.ERROR:
            self._btn_start.setEnabled(True)
            self._btn_stop.setEnabled(False)
            self._light.set_state(f"本地 · {self._base_url}  异常", THEME["danger"])
            self._detail.setText("启动失败，详见日志")
        else:
            self._btn_start.setEnabled(True)
            self._btn_stop.setEnabled(False)
            self._light.set_state(f"本地 · {self._base_url}  已停止", THEME["idle"])
            self._detail.setText(
                "Host A 安全弹出锁未启用：切换前不会在本机弹出 U 盘。启动本服务即启用该锁"
            )
