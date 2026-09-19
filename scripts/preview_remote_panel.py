# -*- coding: utf-8 -*-
"""把「远程桥接服务」分区渲染成 PNG，供人眼验收版面。

与 `preview_remote_config.py` 同一套路：**真实平台 + `WA_DontShowOnScreen`**。
`QT_QPA_PLATFORM=offscreen` 在本机没有字体库，截图里中文全是方块，等于没看。

    python scripts/preview_remote_panel.py [输出目录]

输出三张：启用态（分区）/ 禁用态（分区）/ 禁用态（整窗，看分区头摘要与芯片）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 必须在 import usbswitch 之前设：这条路径决定配置落点。
# MainWindow.shutdown() 会把内存里的配置落盘 —— 不隔离就会覆盖用户已配好的远端主机。
os.environ.setdefault("USBSWITCH_DATA_DIR", tempfile.mkdtemp(prefix="usbswitch-preview-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from usbswitch.core.models import AppConfig  # noqa: E402


def _configured() -> AppConfig:
    """照用户真实场景造一份：Host B 是那台 Linux 边缘网关。"""
    config = AppConfig()
    remote = config.remote_bridge()
    # 用示例 IP 而不是真机地址 —— 本仓库 6935fdb 起的约定：不往仓库里写本地环境
    remote.host = "192.168.1.100"
    remote.username = "admin"
    remote.password = "secret"
    remote.ssh_port = 22
    remote.bridge_port = 8738
    remote.display_name = "Linux 设备 (Edge-Gateway)"
    # 关掉隧道开关：`MainWindow.__init__` 会立刻 sync 一次隧道，开着就会真去连
    # 192.168.1.100 —— 预览脚本不该发起真实网络连接。这只影响摘要里那截
    # 「SSH 隧道 / 直连 :8738」文案，版面完全一致。
    remote.use_tunnel = False
    return config


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="usbswitch-ui-"))
    out.mkdir(parents=True, exist_ok=True)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from usbswitch.ui.main_window import MainWindow
    from usbswitch.ui.theme import STYLESHEET

    app = QApplication([])
    app.setStyleSheet(STYLESHEET)

    window = MainWindow(_configured())
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.show()
    app.processEvents()

    # 分区折叠着就只能看到一行摘要 —— 得展开才能看清里面
    window.section("remote").set_expanded(True)
    app.processEvents()

    written: list[Path] = []

    def shot(name: str, widget) -> None:
        path = out / f"{name}.png"
        widget.grab().save(str(path))
        written.append(path)

    shot("01-启用态-分区", window.section("remote"))

    # 走真实路径切换，而不是直接改配置 —— 顺便验证接线本身
    window._on_remote_enabled_changed(False)
    app.processEvents()
    shot("02-禁用态-分区", window.section("remote"))
    shot("03-禁用态-整窗", window)

    window.shutdown()

    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
