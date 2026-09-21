# -*- coding: utf-8 -*-
"""把「桥接服务配置」对话框渲染成 PNG，供人眼验收版面。

**为什么需要它。** 界面改动光靠单元测试验不了 —— 测试断言得了「控件在不在、可不可
编辑」，断言不了「看起来对不对」。而 `QT_QPA_PLATFORM=offscreen` 在本机没有字体库，
截图里中文全是方块，等于没看。

所以这里**用真实平台渲染**（Windows 上就是 `windows`），配合
`Qt.WA_DontShowOnScreen` —— 窗口不会真的弹到桌面上，但仍按真实平台+真实字体完成
布局与绘制，`grab()` 拿到的就是屏幕上的样子。

    python scripts/preview_remote_config.py [输出目录]

输出三张：正常态 / 校验错误态 / 私钥认证态。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 必须在 import usbswitch 之前设：这条路径决定配置落点，别让预览脚本碰到真配置
os.environ.setdefault("USBSWITCH_DATA_DIR", tempfile.mkdtemp(prefix="usbswitch-preview-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from usbswitch.core.models import AppConfig, AuthMethod, Host  # noqa: E402


def _configured() -> AppConfig:
    """造一份「Host B 已填满」的配置，字段长短贴近真实值，才能看出对齐问题。"""
    config = AppConfig()
    remote = config.bridges[Host.B].remote
    # 用示例 IP 而不是真机地址 —— 本仓库 6935fdb 起的约定：不往仓库里写本地环境
    remote.host = "192.168.1.100"
    remote.username = "admin"
    remote.password = "secret"
    remote.ssh_port = 22
    remote.bridge_port = 8738
    remote.display_name = "Linux 设备 (Edge-Gateway)"
    remote.use_tunnel = True
    return config


def _shot(dialog, path: Path) -> Path:
    dialog.grab().save(str(path))
    return path


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="usbswitch-ui-"))
    out.mkdir(parents=True, exist_ok=True)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from usbswitch.ui.remote_config_dialog import RemoteConfigDialog
    from usbswitch.ui.theme import STYLESHEET

    app = QApplication([])
    app.setStyleSheet(STYLESHEET)

    scenes: list[tuple[str, AppConfig]] = [
        ("01-正常态", _configured()),
        ("02-校验错误态", AppConfig()),  # Host B 什么都没填
    ]

    key_config = _configured()
    key_config.bridges[Host.B].remote.auth = AuthMethod.KEY
    key_config.bridges[Host.B].remote.key_path = r"C:\Users\me\.ssh\id_ed25519"
    scenes.append(("03-私钥认证态", key_config))

    written: list[Path] = []
    for name, config in scenes:
        dialog = RemoteConfigDialog(config)
        # 不真的弹窗，但仍按真实平台完成布局与绘制
        dialog.setAttribute(Qt.WA_DontShowOnScreen, True)
        dialog.show()
        app.processEvents()
        written.append(_shot(dialog, out / f"{name}.png"))
        dialog.close()

    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
