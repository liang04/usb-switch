"""程序入口。

启动方式：

    python -m usbswitch
    python -m usbswitch --minimized     # 开机自启用：静默进托盘，不弹窗口
    python -m usbswitch --selftest      # 自检（打包产物验证也用它）
    usbswitch                           （安装后，由 pyproject 的 gui-scripts 提供）
"""

from __future__ import annotations

import logging
import sys

from . import __version__
from .core import autostart
from .core import build_info
from .core import config as config_module
from .core import logging_setup

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    # 自检必须**最先**处理，而且要早于 QApplication 与单实例锁：
    # - 已有实例在跑时，自检不该被挡在门外（它只是想检查环境）；
    # - 自检不该抢焦点、也不该留下任何副作用。
    if "--selftest" in argv:
        from . import selftest

        logging_setup.setup()
        return selftest.main(argv)

    wants_minimized = "--minimized" in argv

    logging_setup.setup()
    log.info(
        "USB Switch Console v%s 启动（minimized=%s）",
        __version__,
        wants_minimized,
    )
    # 构建身份单独记一行：排查「改了没生效」时先看这里，比对比文件时间戳可靠
    log.info("构建身份：%s", build_info.current().describe())
    problem = build_info.freshness_problem()
    if problem:
        log.warning(problem)

    # PySide6 是重依赖，放在自检分支之后导入 —— 那条路径不需要界面
    from PySide6.QtWidgets import QApplication

    from .ui.icons import app_icon
    from .ui.main_window import MainWindow
    from .ui.single_instance import SingleInstance
    from .ui.theme import STYLESHEET, apply as theme_apply, theme_mode_from_config

    app = QApplication(argv)
    app.setApplicationName("USB Switch Console")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())
    # 先套一份默认（明色）样式，保证「读配置 → 套主题」这段本身看着是正常的
    app.setStyleSheet(STYLESHEET)

    # 注意：setQuitOnLastWindowClosed(False) 由 MainWindow 设置 ——
    # 它才是拥有托盘、需要「关窗不退进程」这个不变量的地方。

    guard = SingleInstance()
    if not guard.try_acquire():
        # 已有实例在运行，它已被唤起窗口。自己直接退出，
        # 否则会出现两个 BleakClient —— 必然复现 WinRT 并发缺陷。
        return 0

    config = config_module.load()

    # 外观必须在**建窗口之前**生效。否则窗口第一帧是明色、读完配置才刷成暗色，
    # 启动瞬间会闪一下白 —— 暗色用户每次开程序都被闪一次。
    _mode, palette = theme_apply(app, theme_mode_from_config(config))
    log.info("界面外观：%s（生效 %s）", _mode, palette)

    # 开机自启会带 --minimized：只在「注册表里确实登记了自启」时才真的隐藏，
    # 避免手工敲 --minimized 时把窗口藏起来找不到。
    start_hidden = wants_minimized and autostart.is_enabled()
    if wants_minimized and not start_hidden:
        log.info("收到 --minimized，但未启用开机自启，仍显示主窗口")

    window = MainWindow(config, history=logging_setup.get_ring_buffer().snapshot())
    guard.activated.connect(window.show_and_activate)

    if start_hidden:
        window.hide()
    else:
        window.show()

    try:
        return app.exec()
    finally:
        guard.release()


def _run() -> int:
    """顶层兜底：任何未捕获异常都要落日志并给用户一个可读的提示。"""
    try:
        return main()
    except Exception:  # noqa: BLE001
        log.exception("启动失败")
        try:
            # 此时 PySide6 可能还没导入成功，所以要在这里才 import
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(None, "USB Switch Console", "程序启动失败，详见日志。")
        except Exception:  # noqa: BLE001 —— 连弹窗都失败就只剩日志了
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(_run())
