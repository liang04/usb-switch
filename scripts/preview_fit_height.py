# -*- coding: utf-8 -*-
"""渲染「窗口高度自适应」的三种状态，供人眼验收。

    python scripts/preview_fit_height.py [输出目录]

输出：
- ``1-首次启动-全折叠``：用户双击后看到的第一屏 —— 窗口应贴着内容，
  下方不留空白（改动前是写死的 760，空一大片）
- ``2-展开全部``：内容超上限，窗口封顶 + 滚动区出现滚动条
- ``3-用户手动调过``：手动 resize 后再开合，高度不被动

用真实平台 + ``WA_DontShowOnScreen`` —— offscreen 没有字体库，中文全是方块。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 必须在 import usbswitch 之前设：这条路径决定配置落点。
# MainWindow.shutdown() 会把内存里的配置落盘 —— 不隔离就会覆盖用户配好的远端主机。
os.environ.setdefault("USBSWITCH_DATA_DIR", tempfile.mkdtemp(prefix="usbswitch-fit-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from usbswitch.core.models import AppConfig, SECTION_IDS  # noqa: E402


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
    remote.use_tunnel = False
    return config


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="usbswitch-fit-"))
    out.mkdir(parents=True, exist_ok=True)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from usbswitch.ui.main_window import MainWindow

    app = QApplication([])
    written: list[Path] = []

    def settle(times: int = 10) -> None:
        for _ in range(times):
            app.processEvents()

    def shot(name: str, window) -> None:
        path = out / f"{name}.png"
        window.grab().save(str(path))
        written.append(path)
        print(f"{name}: {window.width()}x{window.height()}  → {path}")

    # ---- 1. 首次启动，全折叠 ------------------------------------------------
    config = _configured()
    config.window.expanded_sections = {key: False for key in SECTION_IDS}
    window = MainWindow(config)
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.show()
    settle()
    assert not window._size_is_user_owned, "首次显示不该被判定成用户定过尺寸"
    shot("1-首次启动-全折叠", window)

    # ---- 2. 展开全部 --------------------------------------------------------
    for key in SECTION_IDS:
        window.section(key).toggle()
    settle()
    cap = window._max_window_height()
    assert window.height() == cap, f"全部展开应封顶于 {cap}，实际 {window.height()}"
    shot("2-展开全部-封顶", window)

    # ---- 3. 用户手动调过尺寸之后 --------------------------------------------
    for key in SECTION_IDS:
        window.section(key).toggle()
    window.resize(760, 620)
    settle()
    for key in SECTION_IDS:
        window.section(key).toggle()
    settle()
    assert window.height() == 620, "用户定过高度后不该被自动调整"
    shot("3-用户手动调过-不跟随", window)

    window.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
