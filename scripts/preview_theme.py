# -*- coding: utf-8 -*-
"""把界面渲染成 PNG，供人眼验收版面与配色。

与 `preview_remote_panel.py` 同一套路：**真实平台 + `WA_DontShowOnScreen`**。
`QT_QPA_PLATFORM=offscreen` 在本机没有字体库，截图里中文全是方块，等于没看。

    python scripts/preview_theme.py [输出目录]

输出：明色 / 暗色 两套 × （整窗折叠态、远程分区展开、选项分区展开）。

「选项」分区里有外观下拉框 —— 它是这一版新增的控件，单独出一张看图。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 必须在 import usbswitch 之前设：这条路径决定配置落点。
# MainWindow.shutdown() 会把内存里的配置落盘 —— 不隔离就会覆盖用户已配好的远端主机。
os.environ.setdefault("USBSWITCH_DATA_DIR", tempfile.mkdtemp(prefix="usbswitch-theme-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from usbswitch.core.models import AppConfig  # noqa: E402


def _configured() -> AppConfig:
    """造一份「远程已配好、隧道关掉」的配置 —— 版面与真实场景一致，
    但不发起任何网络连接。"""
    config = AppConfig()
    remote = config.remote_bridge()
    remote.host = "192.168.1.100"
    remote.username = "admin"
    remote.password = "secret"
    remote.ssh_port = 22
    remote.bridge_port = 8738
    remote.display_name = "Linux 设备 (Edge-Gateway)"
    remote.use_tunnel = False  # 避免 MainWindow.__init__ 立刻去连它
    return config


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="usbswitch-ui-"))
    out.mkdir(parents=True, exist_ok=True)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from usbswitch.ui import theme
    from usbswitch.ui.main_window import MainWindow

    app = QApplication([])

    written: list[Path] = []
    for mode in ("light", "dark"):
        assert theme.apply(app, mode)[1] == mode, f"{mode} 没有生效"

        window = MainWindow(_configured())
        window.setAttribute(Qt.WA_DontShowOnScreen, True)
        window.resize(860, 980)
        window.show()
        app.processEvents()

        def shot(name: str, widget) -> None:
            path = out / f"{name}.png"
            widget.grab().save(str(path))
            written.append(path)

        # 折叠态整窗：这是用户打开程序看到的第一屏
        shot(f"{mode}-1-整窗-折叠态", window)

        # 展开「远程」：三盏灯 + 提示行的语义色都在这儿，是配色的试金石
        window.section("remote").set_expanded(True)
        app.processEvents()
        shot(f"{mode}-2-远程分区", window.section("remote"))

        # 展开「选项」：新增的外观下拉框在这里
        window.section("options").set_expanded(True)
        app.processEvents()
        shot(f"{mode}-3-选项分区", window.section("options"))

        window.shutdown()

    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
