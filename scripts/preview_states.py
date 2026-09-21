# -*- coding: utf-8 -*-
"""语义状态的配色验收：把「在线 / 警告 / 失败 / 未检测」四种状态同时画出来。

`preview_theme.py` 拍的是静止态（一切「未检测」，全是中性灰）；
配色最容易出错的地方恰恰是**有语义色的时候** ——
chip 的浅底、灯的前景、卡片当前态的正文，这些在暗色下极易对比度不足。
这个脚本专门把它们一次全摆出来。

    python scripts/preview_states.py [输出目录]
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("USBSWITCH_DATA_DIR", tempfile.mkdtemp(prefix="usbswitch-states-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="usbswitch-states-"))
    out.mkdir(parents=True, exist_ok=True)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QApplication,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QVBoxLayout,
        QWidget,
    )

    from usbswitch.core.bridge_remote import RemoteState
    from usbswitch.core.models import AppConfig, BridgeKind, BridgeRunState, DeviceStatus, Host
    from usbswitch.ui import theme
    from usbswitch.ui.main_window import MainWindow
    from usbswitch.ui.widgets import Chip, HeroCard, StatusLight

    app = QApplication([])
    written: list[Path] = []

    for mode in ("light", "dark"):
        theme.apply(app, mode)

        # 一份「配好了、一切在线」的配置，用来点亮所有语义色
        config = AppConfig()
        config.bridges[Host.B].kind = BridgeKind.REMOTE
        remote = config.bridges[Host.B].remote
        remote.host = "192.168.1.100"
        remote.username = "admin"
        remote.password = "secret"
        remote.use_tunnel = False

        window = MainWindow(config)
        window.setAttribute(Qt.WA_DontShowOnScreen, True)
        window.resize(860, 1020)
        window.show()
        app.processEvents()

        # --- 1. 卡片：当前挂载 / 空闲 / 失败 三种态并排 ---
        board = QWidget()
        board.setObjectName("Root")
        grid = QGridLayout(board)
        grid.setContentsMargins(20, 20, 20, 20)
        grid.setSpacing(16)

        grid.addWidget(QLabel("Hero 卡片三态：当前（信息）/ 空闲 / 失败（错误）"), 0, 0, 1, 3)

        cur = HeroCard("Host A", variant="host")
        cur.setProperty("variant", "host")
        cur.set_badge("当前", "info")
        cur.set_line(0, "VBUS 已接通", "ok")
        cur.set_line(1, "检测位 已插入", "ok")
        cur.set_card_state("current")
        grid.addWidget(cur, 1, 0)

        idle = HeroCard("Host B", variant="host")
        idle.set_badge("空闲", "idle")
        idle.set_line(0, "VBUS 断开", "idle")
        idle.set_line(1, "检测位 无", "idle")
        grid.addWidget(idle, 1, 1)

        bad = HeroCard("Host B", variant="host")
        bad.set_badge("切换失败", "error")
        bad.set_line(0, "无法安全弹出 U 盘", "error")
        bad.set_line(1, "请先手工弹出", "error")
        bad.set_card_state("failed")
        grid.addWidget(bad, 1, 2)

        # --- 2. 徽标 + 状态灯：四种语义色 ---
        row = QWidget()
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(10)
        row_layout.addWidget(QLabel("徽标四态与状态灯四态"))

        chips = QWidget()
        chip_layout = QHBoxLayout(chips)
        chip_layout.setContentsMargins(0, 0, 0, 0)
        chip_layout.setSpacing(10)
        for text, state in (("运行中", "ok"), ("未响应", "warn"), ("异常", "error"), ("已禁用", "idle")):
            chip_layout.addWidget(Chip(text, state))
        chip_layout.addStretch(1)
        row_layout.addWidget(chips)

        for name, state in (
            ("SSH  可达（admin@192.168.1.100:22）", "ok"),
            ("桥接服务  离线 · 无响应", "warn"),
            ("隧道  失败（详见日志）", "error"),
            ("SSH 隧道  未启用", "idle"),
        ):
            light = StatusLight()
            light.set_state_key(name, state)
            row_layout.addWidget(light)

        grid.addWidget(row, 2, 0, 1, 3)
        grid.setRowStretch(3, 1)

        board.setAttribute(Qt.WA_DontShowOnScreen, True)
        board.show()
        app.processEvents()

        path = out / f"{mode}-语义状态.png"
        board.grab().save(str(path))
        written.append(path)

        # --- 3. 真实窗口：喂在线状态，看分区头 / 状态栏 / 三盏灯 ---
        from usbswitch.core.ble import BleState
        from usbswitch.core.bridge_remote import RemoteEnv

        window.section("remote").set_expanded(True)
        window._on_state_changed(BleState.CONNECTED)
        window._on_status_received(
            DeviceStatus(host=Host.B, det_a=True, det_b=True, raw="STATUS: host=B")
        )
        window._remote.apply_env(
            RemoteEnv(
                home="/home/admin", python="/usr/bin/python3",
                python_version="3.10.12", systemd=True, sudo_umount=True,
            )
        )
        window._remote.apply_state(
            RemoteState(ssh_reachable=True, ssh_detail="可达（admin@192.168.1.100:22）",
                        http_online=True, http_detail="在线", ssh_checked=True)
        )
        window._remote.apply_tunnel(BridgeRunState.RUNNING)
        window._update_remote_summary()
        app.processEvents()

        path = out / f"{mode}-在线整窗.png"
        window.grab().save(str(path))
        written.append(path)

        window.shutdown()

    for p in written:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
