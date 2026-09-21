# -*- coding: utf-8 -*-
"""把「远程桥接服务」分区渲染成 PNG，供人眼验收版面。

与 `preview_remote_config.py` 同一套路：**真实平台 + `WA_DontShowOnScreen`**。
`QT_QPA_PLATFORM=offscreen` 在本机没有字体库，截图里中文全是方块，等于没看。

    python scripts/preview_remote_panel.py [输出目录]

输出五张：启用态（分区）/ **真实点击取消勾选后**（分区，验点击链路）/ 禁用态（分区）/
禁用态（整窗，看分区头摘要与芯片）/「隧道没通」态（分区，看桥接灯是否如实说
「隧道未建立」而不是「无响应」）。
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

from usbswitch.core.models import AppConfig, Host  # noqa: E402


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

    # 05：**真实点击**复选框。这条链路曾经是断的 ——
    # `clicked.connect(enabledChanged.emit)` 撞上 PySide6 里 `clicked()` 无参
    # 重载，TypeError 被咽掉，界面上勾掉了、配置却没动。截图在这里是**自检**：
    # 断言先跑，不生效就当场失败，不会默默产出一张好看的假图。
    window._remote._enable.click()
    app.processEvents()
    assert window._config.remote_bridge().enabled is False, "点击总开关没生效（信号链路断了）"
    assert "已禁用" in window.section("remote").chip_text(), "分区头没跟着改口"
    shot("05-点击取消勾选后-分区", window.section("remote"))

    # 回到启用态，再走程序化路径产出 02/03
    window._remote._enable.click()
    app.processEvents()
    window._on_remote_enabled_changed(False)
    app.processEvents()
    shot("02-禁用态-分区", window.section("remote"))
    shot("03-禁用态-整窗", window)

    # 04：「勾了隧道、但隧道没通」。这是最容易被误读的状态 —— 桥接灯必须说
    # 「隧道未建立」（去修隧道）而不是「无响应」（去启动远端服务）。
    #
    # 这里直接改配置 + 直接喂状态，**不走 MainWindow 的隧道同步**：
    # 把 use_tunnel 打开后若触发同步，会真去连 192.168.1.100。
    from usbswitch.core.bridge_remote import RemoteBridge
    from usbswitch.core.models import BridgeRunState
    from usbswitch.workers.remote_worker import RemoteWorker

    remote = window._config.bridges[Host.B].remote
    remote.enabled = True
    remote.use_tunnel = True
    remote.runtime_http_base = ""
    # 三处都要跟着改口，否则分区头还停在上一态的「已禁用」上、
    # 而桥接灯已经在说「隧道未建立」—— 截图里自相矛盾，就不是有效验收了
    window._remote.refresh_target()
    window._remote.apply_tunnel(BridgeRunState.STOPPED)
    window._remote.apply_state(RemoteWorker._http_only_state(RemoteBridge(remote)))
    window._update_remote_summary()
    app.processEvents()
    shot("04-隧道未建立-分区", window.section("remote"))

    window.shutdown()

    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
