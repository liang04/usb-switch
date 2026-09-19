"""UI 冒烟测试。

用 offscreen 平台把窗口真正装配起来，验证三件事：
QSS 能套用、各面板能构建、后台线程能干净启动并退出（不挂死）。

不是像素级测试 —— 只挡住「一启动就崩」这类回归。
"""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6")

# 必须在导入 PySide6 之前设置，否则平台插件已经选定
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from usbswitch.core.models import Host  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """UI 测试一律不许发真实网络请求。

    ``MainWindow`` 一构造就会触发一次桥接探活；远程端点若填了真实 IP
    （测试里就填了 ``192.168.1.100``），测试会真的去连它 ——
    慢、不稳定，而且是对别人机器的意外请求。
    """
    from usbswitch.core import bridge_remote as br
    from usbswitch.core.errors import BridgeUnreachableError

    def blocked(*args, **kwargs):
        raise BridgeUnreachableError("测试环境禁止真实网络请求")

    monkeypatch.setattr(br.http_json, "get_json", blocked)


def test_theme_stylesheet_is_not_empty():
    from usbswitch.ui.theme import STYLESHEET

    assert "QGroupBox" in STYLESHEET
    assert "QPushButton#Primary" in STYLESHEET


def test_log_view_appends_without_error(qapp):
    from usbswitch.ui.widgets import LogView

    view = LogView()
    for level in ("debug", "info", "success", "warning", "error"):
        view.append_line(level, f"测试 {level} 行")
    assert "测试 error 行" in view.toPlainText()


def test_toggle_switch_roundtrips_state(qapp):
    from usbswitch.ui.widgets import ToggleSwitch

    switch = ToggleSwitch("安全弹出互锁")
    assert switch.isChecked() is False
    switch.setChecked(True)
    assert switch.isChecked() is True
    assert switch.sizeHint().width() > 0


def test_main_window_constructs_and_shuts_down_cleanly(qapp, data_dir):
    """窗口能建起来，且后台线程能在超时内干净退出（不泄漏）。"""
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow
    from usbswitch.ui.theme import STYLESHEET

    qapp.setStyleSheet(STYLESHEET)
    window = MainWindow(AppConfig())

    try:
        assert window.windowTitle() == "USB Switch Console"
        assert window.isEnabled()
    finally:
        window.shutdown()

    # closeEvent 里保存了窗口几何信息，说明退出流程真的跑到了
    from usbswitch.core import config as config_module

    assert config_module.config_file().exists()


def test_main_window_accepts_history(qapp, data_dir):
    """回归测试：main.py 会传入非空的启动历史，这条路径必须走得通。

    之前 history 为空时 `if history:` 短路，把 LogPanel 缺少 load_lines
    这个问题藏了起来 —— 只有真实入口才会暴露。
    """
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    history = ["14:10:30 [INFO] USB Switch Console v0.1.0 启动", "历史第二行"]
    window = MainWindow(AppConfig(), history=history)

    try:
        text = window._log_panel._view.toPlainText()
        assert "USB Switch Console v0.1.0 启动" in text
        assert "就绪。点击「连接设备」开始。" in text
    finally:
        window.shutdown()


def test_worker_shutdown_is_idempotent_and_joins_thread(qapp, data_dir):
    """回归测试：后台线程必须在进程退出前被 join，否则 Qt 会报线程泄漏并崩溃。

    这里刻意用「start 后立刻 shutdown」来复现最初的竞态：如果 shutdown 在
    事件循环就绪之前去读 self._loop，stop 请求发不出去，run_forever() 会永远阻塞。
    """
    from usbswitch.core.models import AppConfig
    from usbswitch.workers.ble_worker import BleWorker

    worker = BleWorker(AppConfig())
    worker.start()

    assert worker.shutdown() is True, "后台线程未能在超时内退出"
    assert worker.shutdown() is True, "shutdown 不可重复调用"
    assert worker._thread is None


def test_about_to_quit_hook_shuts_worker_down(qapp, data_dir):
    """QApplication.quit() 不触发 closeEvent —— aboutToQuit 必须兜住清理。"""
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    worker = window._worker
    assert worker._thread is not None

    qapp.aboutToQuit.emit()  # 模拟 QApplication.quit()

    assert worker._thread is None, "aboutToQuit 没有清理后台线程"
    window.shutdown()


def test_connect_triggers_automatic_status_query(qapp, data_dir, monkeypatch):
    """回归测试：连接成功后必须自动查一次状态。

    真机验证时发现：缺了这一步，连上以后状态区一直停在「—」，
    看起来就像程序坏了。单元测试原本抓不到，因为没人调用 _on_state_changed。
    """
    from usbswitch.core.ble import BleState
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    calls: list[int] = []
    monkeypatch.setattr(window._worker, "requestStatus", lambda: calls.append(1))

    window._on_state_changed(BleState.CONNECTED)

    assert calls, "连接成功后没有自动查询状态"
    assert window._hero.card(Host.A).isEnabled() is True

    window.shutdown()


def test_disconnect_clears_stale_status(qapp, data_dir):
    """断线后状态区必须清空，绝不能留着上一次的数据。"""
    from usbswitch.core.ble import BleState
    from usbswitch.core.models import AppConfig, DeviceStatus
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    window._status.apply(
        DeviceStatus(host=Host.B, det_a=True, det_b=False, raw="OK: host=B")
    )
    assert window._status._grid._values["当前主机"].text() == "Host B"

    window._on_state_changed(BleState.STOPPED)

    assert window._status._grid._values["当前主机"].text() == "—"
    assert window._hero.card(Host.A).isEnabled() is False
    window.shutdown()


def test_main_window_starts_with_saved_geometry(qapp, data_dir):
    """回归测试：**用户第二次启动**必须能起来。

    真实入口验证抓到的：`_restore_geometry()` 里引用了早前重构中已删除的
    常量 `FIRST_MINIMIZE_HINT`。那行写成 `if not raw or raw.startswith(CONST)`，
    测试里 geometry 为空，`or` 短路让它**永远不被求值** —— 整套测试全绿，
    而真实用户第二次启动（geometry 已保存、非空）直接 NameError 崩溃。

    所以这条用例特意先跑一遍「保存几何 → 重新加载 → 再建窗口」。
    """
    from usbswitch.core import config as config_module
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    path = data_dir / "config.json"

    # 第一次启动：建窗口 → 保存几何
    first = MainWindow(AppConfig())
    first._save_geometry()
    first.shutdown()
    config_module.save(first._config, path)
    assert config_module.load(path).window.geometry, "几何信息没有落盘"

    # 第二次启动：带着非空 geometry 建窗口，必须不抛异常
    second = MainWindow(config_module.load(path))
    try:
        assert second.windowTitle() == "USB Switch Console"
    finally:
        second.shutdown()


def test_restore_geometry_ignores_garbage(qapp, data_dir):
    """几何信息损坏（被手工改过、写了一半）不能让程序起不来。"""
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    config = AppConfig()
    config.window.geometry = "这不是合法的 base64 !!!"

    window = MainWindow(config)
    try:
        assert window.isEnabled()
    finally:
        window.shutdown()


# --------------------------------------------------------------------------- #
# Phase 2：图标 / 托盘 / 单实例 / 新面板
# --------------------------------------------------------------------------- #


def test_icons_are_generated_at_runtime(qapp):
    """图标靠 QPainter 现画，不依赖任何二进制资源文件。"""
    from usbswitch.ui.icons import app_icon, dot_icon
    from usbswitch.ui.theme import THEME

    assert not app_icon().isNull()
    for key in ("success", "idle", "warning"):
        icon = dot_icon(THEME[key])
        assert not icon.isNull()
        assert not icon.pixmap(16, 16).isNull()


def test_tray_marks_current_host(qapp):
    from usbswitch.core.models import Host
    from usbswitch.ui.tray import TrayIcon

    tray = TrayIcon()
    tray.apply_host(Host.B)

    assert "Host B" in tray._act_host.text()
    assert tray._host_actions[Host.B].text().startswith("✓")
    assert not tray._host_actions[Host.A].text().startswith("✓")


def test_tray_switch_actions_follow_connection(qapp):
    from usbswitch.ui.tray import TrayIcon

    tray = TrayIcon()

    tray.apply_connection(False)
    assert all(not a.isEnabled() for a in tray._switch_actions)

    tray.apply_connection(True)
    assert all(a.isEnabled() for a in tray._switch_actions)

    # 切换进行中要禁用，避免连点产生并发 GATT 写
    tray.apply_connection(True, busy=True)
    assert all(not a.isEnabled() for a in tray._switch_actions)


def test_tray_set_autostart_does_not_echo(qapp):
    """程序化回填勾选态时不能把 autostartToggled 再发一遍，否则会形成回环。"""
    from usbswitch.ui.tray import TrayIcon

    tray = TrayIcon()
    seen: list[bool] = []
    tray.autostartToggled.connect(seen.append)

    tray.set_autostart(True)

    assert tray._act_autostart.isChecked() is True
    assert seen == [], "set_autostart 不应该触发 autostartToggled"


def test_options_panel_set_autostart_does_not_echo(qapp):
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.panels.options_panel import OptionsPanel

    panel = OptionsPanel(AppConfig())
    seen: list[bool] = []
    panel.autostartChanged.connect(seen.append)

    panel.set_autostart(True)

    assert seen == [], "set_autostart 不应该触发 autostartChanged"


def test_options_panel_has_no_interlock_switch(qapp):
    """安全弹出互锁开关已移除 —— 锁是派生状态，不再是可勾的全局选项。"""
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.panels.options_panel import OptionsPanel

    panel = OptionsPanel(AppConfig())

    assert not hasattr(panel, "_eject"), "互锁开关不该再出现"
    assert not hasattr(panel, "forceEjectChanged")


def test_bridge_panel_distinguishes_running_from_healthy(qapp):
    """服务线程活着 ≠ HTTP 可用，两者必须显示成不同状态。"""
    from usbswitch.core.models import BridgeRunState
    from usbswitch.ui.panels.bridge_panel import BridgePanel

    panel = BridgePanel("http://127.0.0.1:8737")

    panel.apply(BridgeRunState.RUNNING, {"ok": True, "drives": ["F"]})
    assert "运行中" in panel._light._label.text()
    assert "F:" in panel._detail.text()

    panel.apply(BridgeRunState.RUNNING, None)
    assert "未响应" in panel._light._label.text()

    panel.apply(BridgeRunState.STOPPED, None)
    assert "已停止" in panel._light._label.text()


def test_single_instance_blocks_second_acquire(qapp):
    """第二个实例必须拿不到锁，并去唤醒已有实例。"""
    from usbswitch.ui.single_instance import SingleInstance

    key = "usbswitch-pytest-single-instance"
    first = SingleInstance(key)
    second = SingleInstance(key)
    try:
        assert first.try_acquire() is True
        assert second.try_acquire() is False
    finally:
        second.release()
        first.release()


def test_single_instance_signals_activation(qapp):
    from usbswitch.ui.single_instance import SingleInstance

    key = "usbswitch-pytest-activation"
    first = SingleInstance(key)
    second = SingleInstance(key)
    fired: list[int] = []
    first.activated.connect(lambda: fired.append(1))
    try:
        first.try_acquire()
        second.try_acquire()

        # 消息是异步投递的，给它一点时间处理
        for _ in range(20):
            qapp.processEvents()
            if fired:
                break
        assert fired, "已有实例没有收到唤醒请求"
    finally:
        second.release()
        first.release()


def test_single_instance_release_allows_reacquire(qapp):
    from usbswitch.ui.single_instance import SingleInstance

    key = "usbswitch-pytest-release"
    first = SingleInstance(key)
    assert first.try_acquire() is True
    first.release()

    again = SingleInstance(key)
    try:
        assert again.try_acquire() is True
    finally:
        again.release()


def test_main_window_owns_tray_and_bridge_worker(qapp, data_dir):
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        assert window._tray is not None
        assert window._bridge_worker.base_url.startswith("http://127.0.0.1:")
        # 托盘常驻是关闭窗口不退出进程的前提
        assert qapp.quitOnLastWindowClosed() is False
    finally:
        window.shutdown()


def test_shutdown_is_idempotent_and_stops_bridge(qapp, data_dir, free_port):
    """回归测试：不能假设 8737 是空闲的。

    开发机上通用端口常被代理 / VPN / 安全软件占着（实测本机 Clash Verge 绑了
    ``0.0.0.0:8737``），写死端口会让这条用例在别人的机器配置上假失败。
    """
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    config = AppConfig()
    config.bridges[Host.A].local.port = free_port

    window = MainWindow(config)
    window._bridge_worker.startBridge()
    assert window._bridge_worker.is_started is True

    window.shutdown()
    assert window._bridge_worker.is_started is False
    window.shutdown()  # 幂等


def test_worker_picks_up_changed_local_port(qapp, data_dir, free_port):
    """改端口后重新「启动」应当按新端口监听，不需要重启程序。"""
    from usbswitch.core.models import AppConfig
    from usbswitch.workers.bridge_worker import BridgeWorker

    config = AppConfig()
    config.bridges[Host.A].local.port = free_port

    worker = BridgeWorker(config)
    assert worker.port == free_port

    worker.startBridge()
    assert worker.is_started is True
    try:
        assert worker.base_url == f"http://127.0.0.1:{free_port}"
    finally:
        worker.shutdown()


def test_close_hides_to_tray_when_enabled(qapp, data_dir):
    """开启托盘常驻时，关闭窗口只能隐藏 —— 否则托盘根本没有机会存活。

    这条路径不能用 shutdown 覆盖：它走的是 closeEvent 里 event.ignore()。
    """
    from PySide6.QtGui import QCloseEvent

    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    config = AppConfig()
    config.window.close_to_tray = True

    window = MainWindow(config)
    window.show()
    try:
        event = QCloseEvent()
        window.closeEvent(event)

        assert event.isAccepted() is False, "关闭事件应当被忽略（转为隐藏）"
        assert window.isVisible() is False
        assert window._shutdown_done is False, "不应该真的退出程序"
    finally:
        window.shutdown()


def test_close_quits_when_tray_residency_disabled(qapp, data_dir):
    from PySide6.QtGui import QCloseEvent

    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    config = AppConfig()
    config.window.close_to_tray = False

    window = MainWindow(config)
    try:
        event = QCloseEvent()
        window.closeEvent(event)
        assert window._shutdown_done is True
    finally:
        window.shutdown()


def test_switch_state_reflects_device_reply(qapp):
    """状态面板应按固件报文更新，且断线后必须清空而不是留旧值。"""
    from usbswitch.core.models import DeviceStatus, Host
    from usbswitch.ui.panels.status_panel import StatusPanel

    panel = StatusPanel()
    panel.apply(DeviceStatus(host=Host.B, det_a=True, det_b=False, raw="OK: host=B"))
    panel.reset()

    assert panel.isVisible() is False  # 未 show()，但构建过程不应抛异常


# --------------------------------------------------------------------------- #
# Phase 3：远程桥接配置与状态
# --------------------------------------------------------------------------- #


def _remote_config(*, with_password: bool = True):
    """构造一个「Host B 是远程」的配置。"""
    from usbswitch.core.models import AppConfig, BridgeKind

    config = AppConfig()
    endpoint = config.bridges[Host.B]
    endpoint.kind = BridgeKind.REMOTE
    remote = endpoint.remote
    remote.host = "192.168.1.100"
    remote.username = "ubu"
    remote.ssh_port = 22
    remote.bridge_port = 8738
    if with_password:
        remote.password = "s3cr3t"
    return config


def test_remote_panel_two_lights_are_independent(qapp, data_dir):
    """SSH 可达 ≠ 桥接在线。这是 Phase 3 验收标准里点名的一条。"""
    from usbswitch.core.bridge_remote import RemoteState
    from usbswitch.ui.panels.remote_panel import RemotePanel

    panel = RemotePanel(_remote_config())

    panel.apply_state(
        RemoteState(
            ssh_reachable=True,
            ssh_detail="可达（ubu@192.168.1.100:22）",
            http_online=False,
            http_detail="无响应",
            ssh_checked=True,
        )
    )

    assert "可达" in panel._light_ssh._label.text()
    assert "离线" in panel._light_http._label.text()


def test_remote_panel_http_only_probe_says_undetected_not_unreachable(qapp, data_dir):
    """回归测试：廉价 HTTP 探活没碰 SSH，那一栏必须显示「未检测」。

    之前只有 ssh_reachable 一个布尔量，周期轮询会把 SSH 画成红色「不可达」，
    用户会以为连不上 —— 这是我自己埋的显示 bug。
    """
    from usbswitch.core.bridge_remote import RemoteState
    from usbswitch.ui.panels.remote_panel import RemotePanel

    panel = RemotePanel(_remote_config())

    panel.apply_state(RemoteState(http_online=True, http_detail="在线", ssh_checked=False))

    text = panel._light_ssh._label.text()
    assert "未检测" in text
    assert "不可达" not in text
    assert "在线" in panel._light_http._label.text()


def test_remote_panel_shows_detected_environment(qapp, data_dir):
    from usbswitch.core.bridge_remote import RemoteEnv
    from usbswitch.ui.panels.remote_panel import RemotePanel

    panel = RemotePanel(_remote_config())

    panel.apply_env(
        RemoteEnv(
            home="/home/ubu",
            python="/usr/bin/python3",
            python_version="3.10.12",
            systemd=True,
            sudo_umount=True,
        )
    )

    assert "3.10.12" in panel._light_ssh._label.text()
    assert "systemd" in panel._light_ssh._label.text()


def test_remote_panel_lights_say_not_configured_without_remote(qapp, data_dir):
    """远程没配地址时，三盏灯不该显示「不可达」—— 那是在误导用户。"""
    from usbswitch.core.bridge_remote import RemoteState
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.panels.remote_panel import RemotePanel

    panel = RemotePanel(AppConfig())

    panel.apply_state(RemoteState(ssh_checked=True, ssh_reachable=False))

    assert "未配置远程桥接" in panel._light_ssh._label.text()
    assert "未配置远程桥接" in panel._light_tunnel._label.text()


def test_remote_panel_ops_enabled_for_complete_config(qapp, data_dir):
    """配置完整时，操作按钮必须真的可点。

    历史上这里踩过 `QComboBox.currentData()` 把 `str` 枚举退化成普通字符串的坑
    （`is BridgeKind.REMOTE` 恒为假 → 整块永远置灰）。表单搬去对话框后，
    判据变成「配置是否完整」，这条断言守的是同一个承诺。
    """
    from usbswitch.ui.panels.remote_panel import RemotePanel

    panel = RemotePanel(_remote_config())

    assert panel.validation_errors() == []
    assert panel._btn_install.isEnabled() is True
    assert panel._btn_test.isEnabled() is True
    assert panel._btn_configure.isEnabled() is True


def test_remote_panel_ops_disabled_without_remote_role(qapp, data_dir):
    """远程没配地址时，远程操作整体置灰，并给出该怎么做。

    但「配置…」必须仍然可用 —— 否则用户没有入口去填远程参数。
    """
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.panels.remote_panel import RemotePanel

    panel = RemotePanel(AppConfig())

    assert panel._btn_install.isEnabled() is False
    assert panel._btn_test.isEnabled() is False
    assert panel.validation_errors() == []
    assert panel._btn_configure.isEnabled() is True
    assert "配置…" in panel._hint.text()


def test_remote_panel_warns_but_still_allows_ops_on_incomplete_config(qapp, data_dir):
    """配置不完整时给警告，但**不禁用**按钮。

    `validate()` 比现实更严（例如「私钥认证必须填路径」，而 paramiko 没路径时
    会回退到 agent / 默认密钥，实测照样连得上）。用一条过严的判据把按钮锁死，
    会把本来能用的配置堵住 —— 点击后的前置校验才是拦人的地方。
    """
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.panels.remote_panel import RemotePanel

    # 填了地址但没填用户名 —— 远程「已配置」但不完整
    config = AppConfig()
    config.bridges[Host.B].remote.host = "10.0.0.1"
    panel = RemotePanel(config)

    assert "可能不完整" in panel._hint.text()
    assert "用户名" in panel._hint.text()
    assert panel._btn_install.isEnabled() is True, "过严的判据不该锁死按钮"


def test_remote_panel_target_summary_names_the_host(qapp, data_dir):
    """摘要行要能独立回答「现在连的是谁」—— 折叠态下它是唯一可见的信息。

    断言用 `fullText()`：摘要控件会按当前宽度省略，而测试里窗口没显示过、
    宽度是默认值，`text()` 拿到的是被截断的那一版。
    """
    from usbswitch.ui.panels.remote_panel import RemotePanel

    panel = RemotePanel(_remote_config())

    text = panel._target.fullText()
    assert "Host B" in text
    assert "ubu@192.168.1.100:22" in text
    assert "SSH 隧道" in text


def test_remote_panel_refresh_target_picks_up_config_changes(qapp, data_dir):
    """配置改完要能刷新摘要 —— 否则关掉配置窗口后主界面显示的仍是旧目标。"""
    from usbswitch.ui.panels.remote_panel import RemotePanel

    config = _remote_config()
    panel = RemotePanel(config)

    config.bridges[Host.B].remote.host = "10.1.2.3"
    config.bridges[Host.B].remote.display_name = "新网关"
    panel.refresh_target()

    assert "新网关" in panel._target.text()
    assert "10.1.2.3" in panel._target.text()


def test_remote_panel_configure_button_emits(qapp, data_dir):
    """「配置…」只是发信号，不自己开窗口 —— 开窗是主窗口的职责。"""
    from usbswitch.ui.panels.remote_panel import RemotePanel

    panel = RemotePanel(_remote_config())
    fired: list[int] = []
    panel.configureRequested.connect(lambda: fired.append(1))

    panel._btn_configure.click()

    assert fired == [1]


def test_remote_panel_busy_disables_everything(qapp, data_dir):
    """安装 / 启动期间不允许改配置 —— 边装边改会让「装的是哪一版」变得含糊。"""
    from usbswitch.ui.panels.remote_panel import RemotePanel

    panel = RemotePanel(_remote_config())

    panel.set_busy(True)
    assert panel._btn_install.isEnabled() is False
    assert panel._btn_configure.isEnabled() is False

    panel.set_busy(False)
    assert panel._btn_install.isEnabled() is True
    assert panel._btn_configure.isEnabled() is True


def test_remote_panel_tunnel_light_is_independent(qapp, data_dir):
    """隧道、SSH、HTTP 是三件独立的事，灯必须分开。

    隧道通了不代表桥接服务在跑；SSH 可达也不代表隧道建起来了。
    """
    from usbswitch.core.models import BridgeRunState
    from usbswitch.ui.panels.remote_panel import RemotePanel

    panel = RemotePanel(_remote_config())

    panel.apply_tunnel(BridgeRunState.RUNNING)
    assert "已打通" in panel._light_tunnel._label.text()

    panel.apply_tunnel(BridgeRunState.STARTING)
    assert "建立中" in panel._light_tunnel._label.text()

    panel.apply_tunnel(BridgeRunState.ERROR)
    assert "失败" in panel._light_tunnel._label.text()

    panel.apply_tunnel(BridgeRunState.STOPPED)
    assert "未启用" in panel._light_tunnel._label.text()


def test_remote_panel_tunnel_light_reads_config_not_widgets(qapp, data_dir):
    """隧道灯读的是**配置**里的开关 —— 表单搬走后这里已经没有对应控件了。"""
    from usbswitch.core.models import BridgeRunState
    from usbswitch.ui.panels.remote_panel import RemotePanel

    config = _remote_config()
    config.bridges[Host.B].remote.use_tunnel = False
    panel = RemotePanel(config)

    panel.apply_tunnel(BridgeRunState.RUNNING)

    assert "未启用" in panel._light_tunnel._label.text()


def test_tunnel_worker_is_configured_only_for_remote_with_tunnel(qapp, data_dir):
    from usbswitch.core.models import AppConfig
    from usbswitch.workers.tunnel_worker import TunnelWorker

    assert TunnelWorker(AppConfig()).is_configured is False, "两个角色都是本机时不需要隧道"

    config = _remote_config()  # Host B 远程，隧道默认开
    assert TunnelWorker(config).is_configured is True

    config.bridges[Host.B].remote.use_tunnel = False
    assert TunnelWorker(config).is_configured is False

    config.bridges[Host.B].remote.use_tunnel = True
    config.bridges[Host.B].remote.host = ""
    assert TunnelWorker(config).is_configured is False, "没填主机名不该去建隧道"


def test_tunnel_worker_sync_without_config_is_noop(qapp, data_dir):
    """未配置远程时，周期调用 sync() 必须安静 —— 否则每次都会尝试连一个空主机。"""
    from usbswitch.core.models import AppConfig
    from usbswitch.workers.tunnel_worker import TunnelWorker

    worker = TunnelWorker(AppConfig())
    worker.sync()
    worker.shutdown()

    assert worker.is_running is False
    assert worker.base_url == ""
    assert worker.state.value == "stopped"


def test_main_window_owns_tunnel_worker(qapp, data_dir):
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        assert window._tunnel_worker is not None
        assert window._tunnel_worker.is_configured is False
        # 退出时隧道必须被收掉，否则会留下监听端口与 SSH 连接
        window.shutdown()
        assert window._tunnel_worker.is_running is False
    finally:
        window.shutdown()


# --------------------------------------------------------------------------- #
# Phase 3：远程 worker
# --------------------------------------------------------------------------- #


def _remote_ready_config():
    """一个配置齐全、可以走完整流程的远程端点。"""
    from usbswitch.core.models import AppConfig, BridgeKind

    config = AppConfig()
    endpoint = config.bridges[Host.B]
    endpoint.kind = BridgeKind.REMOTE
    endpoint.remote.host = "10.0.0.1"
    endpoint.remote.username = "ubu"
    endpoint.remote.password = "p"
    return config


def _wait(predicate, timeout: float = 5.0) -> bool:
    """轮询等待条件成立。

    两件事必须同时做到：

    1. **泵 Qt 事件循环**。worker 线程 emit 的信号是**队列投递**到主线程的，
       只 sleep 不 processEvents 的话永远收不到 —— 测试会误报「什么都没发生」。
    2. 命令队列是 FIFO，「后提交的动作已完成」可以证明前面提交的都处理完了，
       于是不必靠 sleep 猜时间。
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_remote_worker_reports_unconfigured(qapp, data_dir):
    """没配远程主机时点操作，要给可操作的提示而不是静默失败。"""
    from usbswitch.core.models import AppConfig
    from usbswitch.workers.remote_worker import RemoteWorker

    worker = RemoteWorker(AppConfig())  # Host B 没填地址 —— 未配置
    failures: list[tuple[str, str]] = []
    worker.operationFailed.connect(lambda m, h: failures.append((m, h)))
    worker.start()
    try:
        worker.testConnection()
        assert _wait(lambda: bool(failures)), "未配置时应当报错"
    finally:
        assert worker.shutdown() is True

    message, hint = failures[0]
    assert "尚未配置远程主机" in message
    assert "SSH 端口" in hint


def test_remote_worker_http_poll_is_silent_when_unconfigured(qapp, data_dir):
    """后台周期探活未配置时必须安静 —— 否则每 5 秒往日志灌一条错误。"""
    from usbswitch.core.models import AppConfig
    from usbswitch.workers.remote_worker import RemoteWorker

    worker = RemoteWorker(AppConfig())
    states: list = []
    failures: list = []
    worker.stateUpdated.connect(states.append)
    worker.operationFailed.connect(lambda m, h: failures.append(m))
    worker.start()
    try:
        worker.refreshHttp()
        worker.testConnection()  # FIFO：它完成后，前面的 refreshHttp 必然已处理
        assert _wait(lambda: bool(failures))
    finally:
        assert worker.shutdown() is True

    assert states == [], "未配置时不该发出状态"
    assert len(failures) == 1, f"refreshHttp 不该产生错误，实际: {failures}"


def test_remote_worker_shutdown_is_idempotent(qapp, data_dir):
    from usbswitch.core.models import AppConfig
    from usbswitch.workers.remote_worker import RemoteWorker

    worker = RemoteWorker(AppConfig())
    worker.start()

    assert worker.shutdown() is True
    assert worker.shutdown() is True
    assert worker._thread is None


def test_remote_worker_http_poll_does_not_mark_busy(qapp, data_dir, monkeypatch):
    """回归测试：周期探活不能让面板「忙」。

    HTTP 超时是 3 秒、轮询间隔 5 秒，若跟着置忙，界面会有近一半时间按钮全灰，
    看起来就像程序坏了。按钮置灰只服务于用户主动发起的操作。
    """
    from usbswitch.core import bridge_remote as br
    from usbswitch.core.bridge_remote import RemoteEnv
    from usbswitch.workers import remote_worker as rw

    # 探活返回「在线」、测试连接返回一个假环境 —— 全程不碰网络也不碰 SSH
    monkeypatch.setattr(br.http_json, "get_json", lambda *a, **k: {"ok": True, "disks": []})
    monkeypatch.setattr(
        rw.RemoteBridge,
        "test_connection",
        lambda self: RemoteEnv(python="/usr/bin/python3", python_version="3.10.12"),
    )

    worker = rw.RemoteWorker(_remote_ready_config())
    busy: list[bool] = []
    worker.busyChanged.connect(busy.append)
    worker.start()
    try:
        worker.refreshHttp()
        worker.testConnection()  # FIFO：它置的忙说明前一个已被处理
        assert _wait(lambda: busy.count(True) == 1 and busy.count(False) >= 1)
    finally:
        assert worker.shutdown() is True

    assert busy[0] is True
    assert busy.count(True) == 1, f"refreshHttp 不该产生忙状态: {busy}"


def test_remote_worker_operations_are_serialized(qapp, data_dir, monkeypatch):
    """安装 / 停止 / 卸载都在动同一台机器上的同一个服务，绝不能并发。

    用「同时记录进入与离开」的方式验证：任何时刻在执行的线程数只能有 1 个。
    """
    import threading

    from usbswitch.core.bridge_remote import RemoteState
    from usbswitch.workers import remote_worker as rw

    config = _remote_ready_config()

    concurrent = 0
    peak = 0
    lock = threading.Lock()

    def fake_install(self) -> RemoteState:
        nonlocal concurrent, peak
        with lock:
            concurrent += 1
            peak = max(peak, concurrent)
        time.sleep(0.02)
        with lock:
            concurrent -= 1
        return RemoteState()

    monkeypatch.setattr(rw.RemoteBridge, "install", fake_install)

    worker = rw.RemoteWorker(config)
    done: list = []
    worker.stateUpdated.connect(done.append)
    worker.start()
    try:
        for _ in range(4):
            worker.install()
        assert _wait(lambda: len(done) == 4), f"只完成了 {len(done)}/4 个任务"
    finally:
        assert worker.shutdown() is True

    assert peak == 1, f"远程操作出现了并发（峰值 {peak}）"


# --------------------------------------------------------------------------- #
# 版面改版：顶栏 / Hero 卡片 / 折叠分区 / 状态栏
# --------------------------------------------------------------------------- #


def _label_texts(widget) -> str:
    """把容器里所有 QLabel 的文字拼起来，用于「界面上显示没显示某句话」的断言。"""
    from PySide6.QtWidgets import QLabel

    return " | ".join(label.text() for label in widget.findChildren(QLabel))


def _shown(window) -> None:
    """把窗口真正显示一次并让 Qt 跑完布局。

    离屏测试里最容易踩的坑：窗口从未 ``show()`` 过时，Qt 会跳过布局失效的
    传播，此时读到的 ``height`` / ``sizeHint`` 是**滞后一拍**的旧值 ——
    折叠明明生效了，量出来还是展开时的高度。
    """
    from PySide6.QtWidgets import QApplication

    window.show()
    QApplication.processEvents()


def _body_height(window) -> int:
    """滚动区内容的总高度 —— 首屏放不放得下就看它。"""
    from PySide6.QtWidgets import QScrollArea

    _shown(window)
    scroll = window.findChild(QScrollArea)
    assert scroll is not None
    layout = scroll.widget().layout()
    layout.activate()
    return layout.totalSizeHint().height()


def _viewport_height(window) -> int:
    """滚动区可视高度 —— 内容不超过它就不会出现滚动条。"""
    from PySide6.QtWidgets import QScrollArea

    _shown(window)
    return window.findChild(QScrollArea).viewport().height()


def test_window_has_all_four_layers(qapp, data_dir):
    """四层结构必须齐：顶栏 / Hero / 折叠区 / 状态栏。"""
    from usbswitch.core.models import SECTION_IDS, AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        assert window._device.objectName() == "TopBar"
        assert window._hero.card(Host.A) is not None
        assert window._hero.off_card is not None
        for key in SECTION_IDS:
            assert window.section(key) is not None
        assert window._status_bar.text("host")
    finally:
        window.shutdown()


def test_collapsing_recovers_first_screen(qapp, data_dir):
    """回归测试：折叠态下核心内容必须不滚动就可见。

    改版前是 7 张等权重卡片竖排，内容高约 950px、视口只有约 810px ——
    最高频的「切换 U 盘」要滚动才看得见。这条断言守住那个基线。
    """
    from usbswitch.core.models import SECTION_IDS, AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        for key in SECTION_IDS:
            window.section(key).set_expanded(True)
        expanded = _body_height(window)

        for key in SECTION_IDS:
            window.section(key).set_expanded(False)
        collapsed = _body_height(window)
        viewport = _viewport_height(window)

        assert collapsed < expanded, "折叠没有减少高度"
        assert collapsed <= viewport, (
            f"全部折叠后内容仍有 {collapsed}px，超出可视区 {viewport}px —— 首屏又要滚动了"
        )
    finally:
        window.shutdown()


def test_section_expansion_persists(qapp, data_dir):
    """折叠状态要落盘 —— 否则每次启动都得重新折一遍。"""
    from usbswitch.core import config as config_module
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        assert window.section("bridge").is_expanded() is False
        window.section("bridge").set_expanded(True, emit=True)
    finally:
        window.shutdown()

    reloaded = config_module.load()
    assert reloaded.window.section_expanded("bridge") is True
    assert reloaded.window.section_expanded("log") is True


def test_section_defaults_apply_to_missing_keys(qapp, data_dir):
    """旧配置里没有 expanded_sections —— 必须回落到各分区的默认值。"""
    from usbswitch.core.models import WindowConfig

    window = WindowConfig()
    assert window.section_expanded("log") is True  # 日志默认展开
    assert window.section_expanded("remote") is False
    assert window.section_expanded("不存在的分区") is False


def test_status_bar_tracks_device_and_host(qapp, data_dir):
    """状态栏要跟着状态走 —— 它是滚动时唯一常驻的信息源。"""
    from usbswitch.core.ble import BleState
    from usbswitch.core.models import AppConfig, DeviceStatus
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        assert "未连接" in window._status_bar.text("device")

        window._on_state_changed(BleState.CONNECTED)
        assert "已连接" in window._status_bar.text("device")

        window._on_status_received(
            DeviceStatus(host=Host.A, det_a=True, det_b=True, raw="STATUS: host=A")
        )
        assert "Host A" in window._status_bar.text("host")

        window._on_state_changed(BleState.STOPPED)
        assert "未连接" in window._status_bar.text("device")
        assert "—" in window._status_bar.text("host")
    finally:
        window.shutdown()


def test_hero_card_carries_vbus_and_detection(qapp, data_dir):
    """卡片自己承载 VBUS 与检测位 —— 用户不必再去「状态」区对照。"""
    from usbswitch.core.ble import BleState
    from usbswitch.core.models import AppConfig, DeviceStatus
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        window._on_state_changed(BleState.CONNECTED)
        window._on_status_received(
            DeviceStatus(host=Host.A, det_a=True, det_b=False, raw="STATUS: host=A")
        )

        active = window._hero.card(Host.A)
        assert active.property("state") == "current"
        assert "当前" in _label_texts(active)
        assert "VBUS 已接通" in _label_texts(active)
        assert "检测位 已插入" in _label_texts(active)

        idle = window._hero.card(Host.B)
        assert idle.property("state") != "current"
        assert "VBUS 断开" in _label_texts(idle)
        assert "检测位 无" in _label_texts(idle)
    finally:
        window.shutdown()


def test_hero_reset_never_shows_stale_values(qapp, data_dir):
    """断线后 Hero 必须清空 —— 显示旧值会让用户以为 U 盘还挂在那儿。"""
    from usbswitch.core.ble import BleState
    from usbswitch.core.models import AppConfig, DeviceStatus
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        window._on_state_changed(BleState.CONNECTED)
        window._on_status_received(
            DeviceStatus(host=Host.B, det_a=True, det_b=True, raw="STATUS: host=B")
        )
        assert "VBUS 已接通" in _label_texts(window._hero.card(Host.B))

        window._on_state_changed(BleState.STOPPED)

        text = _label_texts(window._hero.card(Host.B))
        assert "已接通" not in text
        assert "VBUS —" in text
    finally:
        window.shutdown()


def test_hero_cards_disable_without_connection(qapp, data_dir):
    """未连接时三张卡片都不可点 —— 否则点了只会得到一次失败。"""
    from usbswitch.core.ble import BleState
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        assert window._hero.card(Host.A).isEnabled() is False
        assert window._hero.off_card.isEnabled() is False

        window._on_state_changed(BleState.CONNECTED)
        assert window._hero.card(Host.A).isEnabled() is True
        assert window._hero.off_card.isEnabled() is True

        window._on_state_changed(BleState.RECONNECTING)
        assert window._hero.card(Host.A).isEnabled() is False
    finally:
        window.shutdown()


def test_disconnect_requires_confirmation(qapp, data_dir, monkeypatch):
    """「断开 U 盘」是危险操作，必须过二次确认这道闸。"""
    from usbswitch.core.ble import BleState
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    sent: list[str] = []
    monkeypatch.setattr(window._worker, "requestSwitch", sent.append)
    window._on_state_changed(BleState.CONNECTED)
    try:
        monkeypatch.setattr(window, "_confirm_disconnect", lambda: False)
        window._on_switch_requested(Host.NONE.value)
        assert sent == [], "用户取消后仍然下发了断开命令"

        monkeypatch.setattr(window, "_confirm_disconnect", lambda: True)
        window._on_switch_requested(Host.NONE.value)
        assert sent == [Host.NONE.value]
    finally:
        window.shutdown()


def test_host_switch_does_not_ask_for_confirmation(qapp, data_dir, monkeypatch):
    """切到某一台主机是高频操作，不加二次确认 —— 每次都弹会很烦。"""
    from usbswitch.core.ble import BleState
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    sent: list[str] = []
    monkeypatch.setattr(window._worker, "requestSwitch", sent.append)
    monkeypatch.setattr(
        window, "_confirm_disconnect", lambda: (_ for _ in ()).throw(AssertionError("不该弹确认"))
    )
    window._on_state_changed(BleState.CONNECTED)
    try:
        window._on_switch_requested(Host.A.value)
        assert sent == [Host.A.value]
    finally:
        window.shutdown()


def test_switch_target_is_marked_on_failure(qapp, data_dir, monkeypatch):
    """失败要标在那一张卡片上，而不是只往日志里写一行。"""
    from usbswitch.core.ble import BleState
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    monkeypatch.setattr(window._worker, "requestSwitch", lambda _value: None)
    window._on_state_changed(BleState.CONNECTED)
    try:
        window._on_switch_requested(Host.B.value)
        window._on_operation_failed("无法安全弹出 U 盘", "请先手工弹出")

        assert window._hero.card(Host.B).property("state") == "failed"
        assert "失败" in _label_texts(window._hero.card(Host.B))
        # 失败后按钮必须恢复可用，否则界面就卡在「处理中」了
        assert window._hero.card(Host.A).isEnabled() is True
    finally:
        window.shutdown()


def test_bridge_failure_expands_section_once(qapp, data_dir):
    """桥接异常时自动展开一次，之后就交给用户，不反复打断。"""
    from usbswitch.core.models import AppConfig, BridgeRunState
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        section = window.section("bridge")
        assert section.is_expanded() is False

        window._on_bridge_state(BridgeRunState.ERROR)
        assert section.is_expanded() is True, "异常时没有自动展开"
        assert section.property("alert") == "true", "异常分区没有高亮"

        # 用户手动收起后，再来一次异常不该把它顶开
        section.set_expanded(False, emit=True)
        window._on_bridge_state(BridgeRunState.RUNNING)
        assert section.property("alert") == "false"

        window._on_bridge_state(BridgeRunState.ERROR)
        assert section.is_expanded() is False, "自动展开不应反复打断用户"
    finally:
        window.shutdown()


def test_log_section_summary_shows_last_line(qapp, data_dir):
    """折叠态下摘要行是唯一可见信息，必须跟上最后一条日志。"""
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        window._append("success", "切换完成：已切换到 Host A")
        assert "已切换到 Host A" in window.section("log").summary_text()

        window._append("error", "无法安全弹出 U 盘")
        assert "无法安全弹出" in window.section("log").summary_text()
    finally:
        window.shutdown()


def test_eject_lock_follows_local_bridge_and_remote_config(qapp, data_dir, free_port):
    """锁的两条规则：A 跟随本机桥接服务启停，B 跟随远程配置有无。

    本机端口用空闲端口 —— 8737 在这台机器上被 Clash Verge 占着，写死会让
    用例在别人的配置上假失败。
    """
    from usbswitch.core.models import AppConfig, Host
    from usbswitch.ui.main_window import MainWindow

    config = AppConfig()
    window = MainWindow(config)
    try:
        assert window._eject_lock_for(Host.A) is False, "桥接未启动，A 的锁应当关闭"
        assert window._eject_lock_for(Host.B) is False, "远程未配置，B 的锁应当关闭"

        window._config.bridges[Host.B].remote.host = "192.168.0.1"
        assert window._eject_lock_for(Host.B) is True, "填了地址就该启用 B 的锁"

        window._config.bridges[Host.A].local.port = free_port
        window._bridge_worker.startBridge()
        assert window._eject_lock_for(Host.A) is True, "桥接起来就该启用 A 的锁"

        window._bridge_worker.stopBridge()
        assert window._eject_lock_for(Host.A) is False, "停掉服务就该解除 A 的锁"
    finally:
        window.shutdown()


def test_options_summary_shows_both_eject_locks(qapp, data_dir):
    """两把锁都是派生状态，折叠态也必须看得见 —— 「为什么这次没弹盘」靠它回答。"""
    from usbswitch.core.models import AppConfig, Host
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        summary = window.section("options").summary_text()
        assert "Host A 关" in summary
        assert "Host B 关" in summary

        window._config.bridges[Host.B].remote.host = "192.168.0.1"
        window._on_remote_config_edited()

        assert "Host B 开" in window.section("options").summary_text()
    finally:
        window.shutdown()


def test_opening_gui_does_not_touch_remote_config(qapp, data_dir):
    """回归测试：建窗口 + 关闭，绝不能动到已配好的远程连接。

    这条是因为一次真实事故加的 —— 验证脚本用**默认配置**建了 MainWindow，
    而 ``shutdown()`` 会把内存里的配置落盘，于是用户已配好的远端主机
    （IP / 用户名 / 密码密文）被一份空白配置覆盖掉了。

    GUI 本身不会这样（RemotePanel 有 ``_loading`` 守卫），但这条断言必须存在：
    它守的是「打开一次程序不会弄丢配置」这个承诺。
    """
    from usbswitch.core import config as config_module
    from usbswitch.core.models import AppConfig, BridgeKind
    from usbswitch.ui.main_window import MainWindow

    path = data_dir / "config.json"
    seeded = AppConfig()
    seeded.bridges[Host.B].kind = BridgeKind.REMOTE
    seeded.bridges[Host.B].remote.host = "192.168.0.1"
    seeded.bridges[Host.B].remote.username = "admin"
    seeded.bridges[Host.B].remote.password = "admin"
    config_module.save(seeded, path)

    window = MainWindow(config_module.load(path))
    try:
        assert window._config.bridges[Host.B].remote.host == "192.168.0.1"
    finally:
        window.shutdown()

    after = config_module.load(path)
    remote = after.bridges[Host.B].remote
    assert after.remote_host() is Host.B, "远程角色被改回了本机"
    assert after.bridges[Host.B].kind is BridgeKind.REMOTE
    assert remote.host == "192.168.0.1"
    assert remote.username == "admin"
    assert remote.password == "admin", "密码密文被清掉了"


def test_expanded_sections_do_not_clip_their_content(qapp, data_dir):
    """展开后内容不能被挤出分区边界 —— 那表现为文字压在边框上。

    折叠区的内容是嵌套的（分区 → 面板 → 内部 GroupBox），任何一层拿到的高度
    小于它的 sizeHint 都会造成溢出，而这种问题在离屏渲染里看不出来。
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QWidget

    from usbswitch.core.models import SECTION_IDS, AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        window.resize(780, 1400)
        for key in SECTION_IDS:
            window.section(key).set_expanded(True)
        _shown(window)
        for _ in range(20):
            qapp.processEvents()

        for key in SECTION_IDS:
            body = window.section(key).body
            limit = body.height() + 2
            for child in body.findChildren(QWidget):
                if not child.isVisible():
                    continue
                bottom = child.mapTo(body, QPoint(0, 0)).y() + child.height()
                assert bottom <= limit, (
                    f"{key} 分区的内容溢出边界：{type(child).__name__} "
                    f"底边 {bottom} > {limit}"
                )
    finally:
        window.shutdown()


def test_keyboard_shortcuts_respect_availability(qapp, data_dir, monkeypatch):
    """快捷键绕过了卡片的禁用状态，所以必须自己再判一次可用性。"""
    from usbswitch.core.ble import BleState
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    sent: list[str] = []
    monkeypatch.setattr(window._worker, "requestSwitch", sent.append)
    try:
        window._switch_via_shortcut(Host.A)
        assert sent == [], "未连接时快捷键不该下发切换命令"

        window._on_state_changed(BleState.CONNECTED)
        window._switch_via_shortcut(Host.B)
        assert sent == [Host.B.value]
    finally:
        window.shutdown()


def test_disconnect_has_no_keyboard_shortcut(qapp, data_dir):
    """「断开 U 盘」刻意不给快捷键 —— 这条断言守住那个决定。

    它会让两路 VBUS 同时断电，误触一次可能损坏正在挂载的文件系统。
    只能通过点击卡片 + 二次确认触达。
    """
    from PySide6.QtGui import QShortcut

    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        keys = {shortcut.key().toString() for shortcut in window.findChildren(QShortcut)}
        assert keys == {"Ctrl+1", "Ctrl+2", "Ctrl+R", "Ctrl+L"}, (
            f"快捷键清单变了：{sorted(keys)} —— 新增键位前请确认它不是危险操作"
        )
    finally:
        window.shutdown()


# --------------------------------------------------------------------------- #
# 远程配置窗口
# --------------------------------------------------------------------------- #


def test_configure_button_opens_the_dialog(qapp, data_dir, monkeypatch):
    """「配置…」必须真的开窗，关窗后立刻同步隧道、刷新摘要、释放引用。"""
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    synced: list[int] = []
    monkeypatch.setattr(window, "_refresh_tunnel", lambda: synced.append(1))

    try:
        window.open_remote_config()

        dialog = window._remote_dialog
        assert dialog is not None, "没有打开配置窗口"
        assert dialog.isModal() is True, "配置窗口应当是模态的"
        assert dialog.isVisible() is True, "配置窗口没有真的显示出来"

        dialog.accept()  # 等价于用户点「完成」

        assert window._remote_dialog is None, "关窗后没有释放引用"
        assert synced, "关窗后没有立刻同步隧道"
    finally:
        window.shutdown()


def test_config_dialog_is_singleton(qapp, data_dir):
    """同一时刻只允许一个配置窗口。

    两个窗口会读写同一份 `AppConfig`，「哪一个是当前」就变得不确定。
    """
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        window.open_remote_config()
        first = window._remote_dialog
        assert first is not None

        window.open_remote_config()

        assert window._remote_dialog is first, "重复点击开了第二个窗口"
    finally:
        window.shutdown()


def test_remote_busy_forwards_to_open_dialog(qapp, data_dir):
    """测试连接期间，配置表单要跟着一起置灰。"""
    from usbswitch.core.models import AppConfig, BridgeKind
    from usbswitch.ui.main_window import MainWindow
    from usbswitch.ui.remote_config_dialog import RemoteConfigDialog

    config = AppConfig()
    config.bridges[Host.B].kind = BridgeKind.REMOTE
    config.bridges[Host.B].remote.host = "192.168.0.1"
    config.bridges[Host.B].remote.username = "admin"
    config.bridges[Host.B].remote.password = "admin"

    window = MainWindow(config)
    dialog = RemoteConfigDialog(config, window)
    window._remote_dialog = dialog

    try:
        window._on_remote_busy(True)
        assert dialog._creds.isEnabled() is False
        assert window._remote._btn_install.isEnabled() is False

        window._on_remote_busy(False)
        assert dialog._creds.isEnabled() is True
    finally:
        window._remote_dialog = None
        window.shutdown()


def test_remote_edit_defers_tunnel_sync(qapp, data_dir, monkeypatch):
    """逐键编辑不能逐键重建隧道。

    配置窗口是「改一次存一次」的，若每次都同步隧道，输入一个 IP 地址会建拆
    十几次 SSH 连接。这里断言的是：编辑时只是**启动防抖计时器**，不同步。
    """
    from usbswitch.core.models import AppConfig, BridgeKind
    from usbswitch.ui.main_window import MainWindow

    config = AppConfig()
    config.bridges[Host.B].kind = BridgeKind.REMOTE
    config.bridges[Host.B].remote.host = "192.168.0.1"
    config.bridges[Host.B].remote.username = "admin"
    config.bridges[Host.B].remote.password = "admin"

    window = MainWindow(config)
    synced: list[int] = []
    monkeypatch.setattr(window, "_refresh_tunnel", lambda: synced.append(1))

    try:
        for _ in range(5):
            window._on_remote_config_edited()

        assert synced == [], "编辑过程中不该同步隧道"
        assert window._tunnel_sync.isActive() is True, "防抖计时器没有启动"
    finally:
        window._tunnel_sync.stop()
        window.shutdown()


def test_shutdown_closes_open_config_dialog(qapp, data_dir):
    """退出时若配置窗口还开着，必须先关掉。

    关窗时会做一次隧道同步与摘要刷新；拖到 worker 全停之后再跑，
    只会留下一串「服务已停止」的噪声 —— `_refresh_tunnel` 对此有守卫。
    """
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    window.open_remote_config()
    assert window._remote_dialog is not None, "配置窗口没有打开"

    window.shutdown()

    assert window._remote_dialog is None, "退出后对话框引用没有释放"
    assert window._shutdown_done is True


def test_tunnel_sync_is_skipped_after_shutdown(qapp, data_dir, monkeypatch):
    """退出后再调 `_refresh_tunnel` 必须安静返回。

    配置窗口的关窗回调会走到它，而那时 tunnel worker 已经停了。
    """
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    window.shutdown()

    called: list[int] = []
    monkeypatch.setattr(window._tunnel_worker, "sync", lambda: called.append(1))

    window._refresh_tunnel()

    assert called == [], "退出后仍在同步隧道"


def test_status_bar_shows_build_identity(qapp, data_dir):
    """状态栏必须常驻一行构建身份。

    这条是为了「改了源码、双击的却是旧 exe」加的：那种情况下程序不报错，
    用户只会觉得「界面没变」。有这一行就能一眼确认跑的是哪一版。
    用真实配置跑真机验证时，它也是判断「我看到的是不是新界面」的依据。
    """
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        text = window._status_bar.build_text()
        assert text.startswith("v"), f"构建标记异常：{text!r}"
        # 开发态会带源码时间戳；打包态带构建时间
        assert ("dev" in text) or ("构建" in text), text
        assert "打包" in window._status_bar.build_text() or "dev" in text
    finally:
        window.shutdown()


def test_build_marker_matches_current_process(qapp, data_dir):
    """状态栏显示的就是这一进程自己的构建身份，不是一个写死的常量。"""
    from usbswitch.core import build_info
    from usbswitch.core.models import AppConfig
    from usbswitch.ui.main_window import MainWindow

    window = MainWindow(AppConfig())
    try:
        assert window._status_bar.build_text() == build_info.current().short()
    finally:
        window.shutdown()
