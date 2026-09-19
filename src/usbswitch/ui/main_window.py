"""主窗口 —— 把各面板、托盘子系统和各后台 worker 接起来。

本模块只做四件事：摆放面板、转发信号、同步托盘、收敛退出路径。
任何业务判断都应该出现在 `usbswitch.core` 里，而不是这里。

版面结构（自 2026-09-18 改版）
------------------------------

原实现是 7 个等权重的 `QGroupBox` 竖排，内容高约 950px 而窗口只有 900px ——
最常用的「切换 U 盘」要滚动才看得见，而配好就不再动的远程表单长期占据首屏。
现在改为四层：

```
顶栏（设备连接）
U 盘连接（三张大卡片，承载状态）
折叠区 ×5（各带一行摘要）
状态栏（常驻四个指示灯）
```

首屏约 480px，三件关键信息不滚动即可见。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core import autostart
from ..core import config as config_module
from ..core.ble import BleState
from ..core.errors import ConfigError
from ..core.models import SECTION_IDS, AppConfig, BridgeRunState, Host
from ..core.orchestrator import SwitchResult
from ..workers import BleWorker, BridgeWorker, RemoteWorker, TunnelWorker
from .icons import app_icon
from .panels.bridge_panel import BridgePanel
from .panels.device_panel import DevicePanel
from .panels.hero_panel import HeroPanel
from .panels.log_panel import LogPanel
from .panels.options_panel import OptionsPanel
from .panels.remote_panel import RemotePanel
from .panels.status_panel import StatusPanel
from .remote_config_dialog import RemoteConfigDialog
from .theme import THEME
from .tray import TrayIcon
from .widgets import CollapsibleSection, StatusBar

log = logging.getLogger(__name__)

BRIDGE_POLL_INTERVAL_MS = 5000

#: 远程配置改动后，等这么久再把变更同步到隧道。
#: 隧道绑定在具体目标上，改目标必须重建；但**每个按键**都重建一次是荒唐的 ——
#: 输入一个 IP 地址会触发十几次 SSH 连接建立与拆除。合并成一次。
TUNNEL_SYNC_DELAY_MS = 800

_STATE_PRESENTATION: dict[BleState, tuple[str, str]] = {
    BleState.IDLE: ("未连接", "idle"),
    BleState.SCANNING: ("正在扫描…", "warning"),
    BleState.CONNECTING: ("正在连接…", "warning"),
    BleState.CONNECTED: ("已连接", "success"),
    BleState.RECONNECTING: ("连接中断，重连中…", "warning"),
    BleState.STOPPED: ("未连接", "idle"),
}


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, *, history: list[str] | None = None) -> None:
        super().__init__()
        self._config = config
        self._connected = False
        self._switching = False
        self._shutdown_done = False
        self._last_bridge_payload: dict | None = None
        #: 「本机桥接端口需要重启才生效」的提醒去重
        self._warned_local_port: int | None = None
        #: 状态栏与 Hero 都需要的最近一次已知状态
        self._ble_state: BleState = BleState.IDLE
        self._current_host: Host | None = None
        self._remote_http_online = False
        #: 桥接异常只自动展开一次，之后尊重用户的开合选择
        self._bridge_alerted = False
        #: 当前打开的远程配置对话框（同一时刻只允许一个）
        self._remote_dialog: RemoteConfigDialog | None = None

        self.setWindowTitle("USB Switch Console")
        self.setWindowIcon(app_icon())
        self.resize(760, 760)
        self._restore_geometry()

        self._bridge_worker = BridgeWorker(config)
        # 安全弹出锁是**派生状态**：Host A 跟随本机桥接服务的启停，
        # Host B 跟随远程桥接配置的有无。由主窗口注入编排器 —— 这两样都不在
        # core 层（一个是 worker 的运行态，一个是 UI 维持的配置对象）。
        self._worker = BleWorker(config, eject_lock=self._eject_lock_for)
        self._remote_worker = RemoteWorker(config)
        self._tunnel_worker = TunnelWorker(config)
        self._tray = TrayIcon(self)

        # 托盘常驻时，「关闭窗口」不能等同于「退出程序」—— 不设这一句，
        # 一旦有代码路径真的关掉窗口，进程就会结束、托盘也随之下线。
        # 这条不变量属于拥有托盘的 MainWindow，放在入口 main.py 里容易被漏掉。
        app = QApplication.instance()
        if app is not None:
            app.setQuitOnLastWindowClosed(False)

        self._build_ui()
        self._wire_signals()
        self._wire_shortcuts()
        self._wire_quit_hook()

        self._worker.start()
        self._remote_worker.start()
        # 隧道要尽早拉起：桥接地址由它决定，晚起会让前几次探活打到直连地址上
        self._tunnel_worker.sync()
        self._tray.show()
        self._sync_autostart_from_registry()

        if history:
            # 回填启动前的日志（此刻事件循环还没跑，不会有信号插队）
            self._log_panel.load_lines(history)
            last = history[-1].strip()
            if last:
                self._sections["log"].set_summary(last)
        self._append("info", "就绪。点击「连接设备」开始。")

        self._bridge_poll = QTimer(self)
        self._bridge_poll.setInterval(BRIDGE_POLL_INTERVAL_MS)
        self._bridge_poll.timeout.connect(self._poll_bridges)
        self._bridge_poll.start()

        self._tunnel_sync = QTimer(self)
        self._tunnel_sync.setSingleShot(True)
        self._tunnel_sync.setInterval(TUNNEL_SYNC_DELAY_MS)
        self._tunnel_sync.timeout.connect(self._refresh_tunnel)

        self._poll_bridges()

    # -- 构建 --------------------------------------------------------------- #

    def _build_ui(self) -> None:
        self._device = DevicePanel(self._config.ble.device_name)
        self._hero = HeroPanel()
        self._status = StatusPanel()
        self._bridge = BridgePanel(self._bridge_worker.base_url)
        self._remote = RemotePanel(self._config)
        self._options = OptionsPanel(
            self._config, autostart_enabled=autostart.is_enabled()
        )
        self._log_panel = LogPanel()

        titled = {
            "detail": ("设备状态详情", self._status),
            "bridge": ("桥接服务", self._bridge),
            "remote": ("远程桥接服务", self._remote),
            "options": ("选项", self._options),
            "log": ("日志", self._log_panel),
        }
        self._sections: dict[str, CollapsibleSection] = {}
        for key in SECTION_IDS:
            title, content = titled[key]
            section = CollapsibleSection(
                key,
                title,
                content,
                expanded=self._config.window.section_expanded(key),
            )
            section.toggled.connect(self._on_section_toggled)
            self._sections[key] = section

        body = QWidget()
        body.setObjectName("Root")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self._hero)
        for key in SECTION_IDS:
            body_layout.addWidget(self._sections[key])
        body_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(body)

        self._status_bar = StatusBar()

        central = QWidget()
        central.setObjectName("Root")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._device)
        outer.addWidget(scroll, 1)
        outer.addWidget(self._status_bar)
        self.setCentralWidget(central)

        self._hero.set_enabled(False)
        self._refresh_summaries()
        self._update_status_bar()

    def _wire_signals(self) -> None:
        self._device.connectRequested.connect(self._worker.requestConnection)
        self._device.scanRequested.connect(self._on_scan_requested)
        self._hero.switchRequested.connect(self._on_switch_requested)
        self._hero.refreshRequested.connect(self._worker.requestStatus)

        self._bridge.startRequested.connect(self._bridge_worker.startBridge)
        self._bridge.stopRequested.connect(self._bridge_worker.stopBridge)
        self._bridge.refreshRequested.connect(self._bridge_worker.refresh)

        # 远程面板：安装/启动/停止/卸载前先做一次本地校验，避免白等一次 SSH 超时
        self._remote.testRequested.connect(self._remote_worker.testConnection)
        self._remote.installRequested.connect(self._on_remote_install)
        self._remote.startRequested.connect(self._remote_worker.startService)
        self._remote.stopRequested.connect(self._remote_worker.stopService)
        self._remote.uninstallRequested.connect(self._remote_worker.uninstall)
        self._remote.refreshRequested.connect(self._remote_worker.inspect)
        self._remote.configureRequested.connect(self.open_remote_config)
        self._remote.enabledChanged.connect(self._on_remote_enabled_changed)

        self._remote_worker.stateUpdated.connect(self._on_remote_state)
        self._remote_worker.envDetected.connect(self._on_remote_env)
        self._remote_worker.logMessage.connect(self._append)
        self._remote_worker.operationFailed.connect(self._on_operation_failed)
        self._remote_worker.busyChanged.connect(self._on_remote_busy)

        self._tunnel_worker.stateChanged.connect(self._on_tunnel_state)
        self._tunnel_worker.logMessage.connect(self._append)

        self._options.autostartChanged.connect(self._on_autostart_changed)
        self._options.closeToTrayChanged.connect(self._on_close_to_tray_changed)

        self._worker.stateChanged.connect(self._on_state_changed)
        self._worker.statusReceived.connect(self._on_status_received)
        self._worker.devicesFound.connect(self._on_devices_found)
        self._worker.logMessage.connect(self._append)
        self._worker.switchSucceeded.connect(self._on_switch_succeeded)
        self._worker.operationFailed.connect(self._on_operation_failed)

        self._bridge_worker.stateChanged.connect(self._on_bridge_state)
        self._bridge_worker.statusUpdated.connect(self._on_bridge_status)
        self._bridge_worker.logMessage.connect(self._append)
        self._bridge_worker.operationFailed.connect(self._on_operation_failed)

        self._tray.toggleWindowRequested.connect(self.toggle_window)
        self._tray.switchRequested.connect(self._on_switch_requested)
        self._tray.autostartToggled.connect(self._on_autostart_changed)
        self._tray.quitRequested.connect(self.quit_application)

    def _wire_shortcuts(self) -> None:
        """高频操作的键盘入口。

        **「断开 U 盘」刻意不给快捷键** —— 它会让两路 VBUS 同时断电，是不可逆的
        危险操作，误触一次的成本远大于省下的那一下。它仍然只能通过点击卡片
        并二次确认触达。
        """
        for sequence, slot in (
            ("Ctrl+1", lambda: self._switch_via_shortcut(Host.A)),
            ("Ctrl+2", lambda: self._switch_via_shortcut(Host.B)),
            ("Ctrl+R", self._refresh_via_shortcut),
            ("Ctrl+L", lambda: self._sections["log"].set_expanded(True, emit=True)),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(slot)

    def _switch_via_shortcut(self, host: Host) -> None:
        """快捷键绕过了卡片的禁用状态，这里必须自己再判一次可用性。"""
        if not self._hero.card(host).isEnabled():
            self._append("warning", "设备未连接或正在处理中，快捷键已忽略")
            return
        self._on_switch_requested(host.value)

    def _refresh_via_shortcut(self) -> None:
        if not self._connected:
            self._append("warning", "设备未连接，无法刷新状态")
            return
        self._worker.requestStatus()

    def _wire_quit_hook(self) -> None:
        """把退出清理同时挂到 aboutToQuit 上。

        只挂 closeEvent 是不够的：``QApplication.quit()``（托盘退出、系统注销等
        路径）不触发 closeEvent，后台线程会被直接销毁。``shutdown()`` 幂等，
        两条路径都调用是安全的。
        """
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    # -- 折叠分区 ----------------------------------------------------------- #

    def _on_section_toggled(self, key: str, expanded: bool) -> None:
        self._config.window.expanded_sections[key] = expanded
        self._save_config()

    def section(self, key: str) -> CollapsibleSection:
        """按 id 取分区（测试与主窗口内部使用）。"""
        return self._sections[key]

    def _refresh_summaries(self) -> None:
        """把各分区的一行摘要填上 —— 折叠态下这是用户唯一能看到的信息。"""
        self._update_bridge_summary()
        self._update_remote_summary()
        self._update_options_summary()
        self._sections["detail"].set_summary("固件报文 —")
        self._sections["log"].set_summary("尚无日志")

    def _update_bridge_summary(self, state: BridgeRunState | None = None) -> None:
        """state 省略时取 worker 的当前状态。

        显式传入是必要的：``_on_bridge_state`` 的**参数**才是刚发生的事情，
        而 ``self._bridge_worker.state`` 可能还没更新到 —— 用它会让摘要行
        与徽标落后一拍（测试就是这么发现的）。
        """
        if state is None:
            state = self._bridge_worker.state

        section = self._sections["bridge"]
        section.set_summary(self._bridge_worker.base_url)

        if state is BridgeRunState.RUNNING:
            # 线程活着但 HTTP 不通 —— 与「运行中」必须区分开
            if self._last_bridge_payload:
                section.set_chip("运行中", "ok")
            else:
                section.set_chip("未响应", "warn")
            section.set_alert(False)
        elif state is BridgeRunState.ERROR:
            section.set_chip("异常", "error")
            section.set_alert(True)
        else:
            section.set_chip("已停止", "idle")
            section.set_alert(False)

    def _alert_bridge_section(self) -> None:
        """桥接首次进入异常时自动展开一次。

        只做一次：之后由用户控制开合 —— 每 5 秒的状态轮询都把用户正在看的
        内容顶走，比不展开更烦人。
        """
        if self._bridge_alerted:
            return
        self._bridge_alerted = True
        self._sections["bridge"].set_expanded(True)

    def _update_remote_summary(self) -> None:
        section = self._sections["remote"]
        remote = self._config.remote_bridge()

        # 「已禁用」必须与「未配置」分开说：前者是用户自己关的，后者是压根没填。
        # 混成同一句话，用户会以为禁用把配置吃掉了。
        if not remote.enabled:
            section.set_summary("远程桥接服务已禁用 —— Host B 安全弹出锁未启用")
            section.set_chip("已禁用", "warn")
            return

        host = self._config.remote_host()
        if host is None:
            section.set_summary("Host B 未配置远程（Linux）桥接 —— 安全弹出锁未启用")
            section.set_chip("", "idle")
            return

        name = remote.display_name or remote.host or "未填写地址"
        transport = "SSH 隧道" if remote.use_tunnel else f"{remote.host}:{remote.bridge_port}"
        section.set_summary(f"{host.label} · {name} · {remote.username or '未填用户名'} · {transport}")

        if self._remote_http_online:
            section.set_chip("在线", "ok")
        elif self._tunnel_worker.is_running:
            section.set_chip("隧道已通", "warn")
        elif self._tunnel_worker.state is BridgeRunState.ERROR:
            section.set_chip("隧道异常", "error")
        else:
            section.set_chip("未连接", "idle")

    def _update_options_summary(self) -> None:
        section = self._sections["options"]
        # 两把锁都是派生状态：A 跟随本机桥接服务，B 跟随远程桥接配置。
        # 折叠态也要看得见 —— 「为什么这次切换没弹盘」是最常被追问的问题。
        lock = "开" if self._eject_lock_for(Host.A) else "关"
        lock_b = "开" if self._eject_lock_for(Host.B) else "关"
        section.set_summary(
            f"安全弹出锁 Host A {lock} · Host B {lock_b}"
            f" · 开机自启 {'开' if autostart.is_enabled() else '关'}"
            f" · 关闭到托盘 {'开' if self._config.window.close_to_tray else '关'}"
        )
        if lock == "开" and lock_b == "开":
            section.set_chip("", "idle")
        else:
            section.set_chip("弹出锁未全开", "warn")

    def _update_status_bar(self) -> None:
        """状态栏每处都从「最近一次已知状态」推导，不重复请求。"""
        bar = self._status_bar
        connected = self._ble_state is BleState.CONNECTED
        bar.set_segment(
            "device",
            f"设备 {'已连接' if connected else '未连接'}",
            "ok" if connected else "idle",
        )

        host = self._current_host
        if host is None:
            bar.set_segment("host", "当前主机 —", "idle")
        elif host is Host.NONE:
            bar.set_segment("host", "当前主机 已断开", "warn")
        else:
            bar.set_segment("host", f"当前主机 {host.label}", "ok")

        state = self._bridge_worker.state
        if state is BridgeRunState.RUNNING:
            bar.set_segment(
                "bridge",
                "本地桥接 运行中" if self._last_bridge_payload else "本地桥接 未响应",
                "ok" if self._last_bridge_payload else "warn",
            )
        elif state is BridgeRunState.ERROR:
            bar.set_segment("bridge", "本地桥接 异常", "error")
        else:
            bar.set_segment("bridge", "本地桥接 已停止", "idle")

        if not self._tunnel_worker.is_configured:
            bar.set_segment("tunnel", "远程隧道 未启用", "idle")
        elif self._tunnel_worker.is_running:
            bar.set_segment("tunnel", "远程隧道 已打通", "ok")
        elif self._tunnel_worker.state is BridgeRunState.ERROR:
            bar.set_segment("tunnel", "远程隧道 异常", "error")
        else:
            bar.set_segment("tunnel", "远程隧道 连接中", "warn")

    # -- 设备 --------------------------------------------------------------- #

    def _on_state_changed(self, state: BleState) -> None:
        text, color_key = _STATE_PRESENTATION.get(state, ("未知", "idle"))
        self._ble_state = state
        self._connected = state is BleState.CONNECTED

        if self._connected:
            self._device.set_connected(True)
            # 连接成功后立刻查一次状态。缺了这一步，状态区会一直停在「—」，
            # 用户看到的就是一个「连上了但什么都不显示」的界面。
            self._worker.requestStatus()
        else:
            self._device.set_connected(False)
            self._device.set_busy(f"{text}  ·  {self._config.ble.device_name}", THEME[color_key])
            # 断线后状态区的数据已经不可信，必须清空而不是留着旧值
            self._status.reset()
            self._hero.reset()
            self._current_host = None
            self._sections["detail"].set_summary("固件报文 —")

        self._hero.set_enabled(self._connected)
        self._tray.apply_connection(self._connected, busy=self._switching)
        self._update_status_bar()

    def _on_status_received(self, status) -> None:
        self._status.apply(status)
        self._hero.apply(status)
        self._current_host = status.host
        self._sections["detail"].set_summary(status.raw or "—")
        self._tray.apply_host(status.host)
        self._update_status_bar()

    def _on_scan_requested(self) -> None:
        self._device.set_scanning(True)
        self._append("info", "开始扫描附近的 USB-Switch 设备 …")
        self._worker.requestScan()

    def _on_devices_found(self, devices: list) -> None:
        self._device.set_scanning(False)
        if not devices:
            self._append("warning", "未发现设备。请确认 ESP32-C3 已上电、电脑蓝牙已打开。")
            QMessageBox.warning(
                self,
                "未发现设备",
                "没有扫描到 USB-Switch。\n\n请确认：\n"
                "· ESP32-C3 已上电\n"
                "· 电脑蓝牙已打开\n"
                "· 设备未被其他程序独占",
            )
            return

        for item in devices:
            rssi = f"，信号 {item.rssi} dBm" if item.rssi is not None else ""
            self._append("success", f"发现 {item.name}（{item.address}{rssi}）")

        self._worker.requestConnection(True)

    # -- 切换 --------------------------------------------------------------- #

    def _eject_lock_for(self, host: Host) -> bool:
        """安全弹出锁的启停判据 —— 由 BLE 后台线程经编排器回调。

        - **Host A（本机 Windows）**：跟随本机桥接服务。服务停着，桥接地址
          根本不通，弹出必然失败；此时锁一并解除，切换照常进行但跳过本机弹出。
        - **Host B（远程 Linux）**：跟随远程桥接。既没填地址、也没被启用时就没有
          可弹出的对端，锁不启用。两者在 `remote_host()` 里已经收敛成同一个
          None，这里不必分开判断。

        只做纯读取（`BridgeWorker.is_started` 是普通属性），没有 Qt 调用，
        因此跨线程直接读是安全的。
        """
        if host is Host.A:
            return self._bridge_worker.is_started
        return self._config.remote_host() is not None

    def _on_switch_requested(self, host_value: str) -> None:
        host = Host(host_value)
        if host is Host.NONE and not self._confirm_disconnect():
            self._append("info", "已取消断开操作")
            return

        self._switching = True
        self._switch_target = host
        self._hero.set_busy(True)
        self._hero.set_switching(
            host,
            "正在断开 U 盘 …" if host is Host.NONE else f"正在切换到 {host.label} …",
        )
        self._tray.apply_connection(self._connected, busy=True)
        self._worker.requestSwitch(host_value)

    def _confirm_disconnect(self) -> bool:
        """断开是危险操作，必须二次确认。

        默认按钮设为「取消」—— 回车即取消，这是安全方向上的默认值。
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("断开 U 盘")
        box.setText("确定要断开 U 盘吗？")
        box.setInformativeText(
            "两路 VBUS 都会断电，U 盘将从两台主机上消失。\n"
            "若 U 盘当前仍被系统挂载且未经安全弹出，可能导致文件系统损坏。"
        )
        cancel = box.addButton("取消", QMessageBox.RejectRole)
        confirm = box.addButton("断开", QMessageBox.DestructiveRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is confirm

    def _on_switch_succeeded(self, result: SwitchResult) -> None:
        self._finish_switch()
        if result.skipped:
            self._append("info", result.message)
        else:
            self._append("success", f"切换完成：{result.message}")
            # 「OK: host=B」不带检测位，必须再查一次状态才能拿到完整信息
            self._worker.requestStatus()
            self._tray.notify("USB Switch Console", f"已切换到 {result.host.label}")

    def _on_operation_failed(self, message: str, hint: str) -> None:
        failed_target = self._switch_target if self._switching else None
        self._finish_switch()
        self._device.set_scanning(False)
        if failed_target is not None:
            # 把失败标在**那一张**卡片上，用户不必去日志里找是切到哪儿失败了
            self._hero.mark_failed(failed_target)
        self._append("error", f"{message}（{hint}）" if hint else message)

    def _finish_switch(self) -> None:
        self._switching = False
        self._switch_target = None
        self._hero.set_busy(False)
        self._tray.apply_connection(self._connected)

    # -- 桥接 --------------------------------------------------------------- #

    def _on_bridge_state(self, state: BridgeRunState) -> None:
        self._bridge.apply(state, self._last_bridge_payload)
        self._tray.apply_bridge(state, healthy=bool(self._last_bridge_payload))
        self._update_bridge_summary(state)
        if state is BridgeRunState.ERROR:
            self._alert_bridge_section()
        # Host A 的安全弹出锁跟随本服务启停，选项区摘要必须跟着变
        self._update_options_summary()
        self._update_status_bar()

    def _on_bridge_status(self, payload: object) -> None:
        self._last_bridge_payload = payload if isinstance(payload, dict) else None
        state = self._bridge_worker.state
        self._bridge.apply(state, self._last_bridge_payload)
        self._tray.apply_bridge(state, healthy=bool(self._last_bridge_payload))
        self._update_bridge_summary()
        self._update_status_bar()

    def _poll_bridges(self) -> None:
        """周期探活：本地走 HTTP（毫秒级），远程也只发一个 HTTP 请求。

        远端的 **SSH 探测不放进轮询** —— 那要建连接并执行环境探测，是秒级开销，
        对用户机器和被连的主机都是负担。SSH 状态只在用户点「测试连接 / 刷新」
        或执行完一次操作后更新。
        """
        self._bridge_worker.refresh()
        if self._remote_worker.target_host is not None:
            self._remote_worker.refreshHttp()

    # -- 远程桥接 ----------------------------------------------------------- #

    def _on_remote_enabled_changed(self, enabled: bool) -> None:
        """远程桥接服务总开关被切换。

        关掉它等于宣告「Host B 不使用远程桥接」，四件事一并收敛，全部由
        ``AppConfig.remote_host()`` 这一条判据派生，这里不重新判断：

        1. Host B 的安全弹出锁 —— 切换前不再要求弹出（``_eject_lock_for``）
        2. SSH 隧道 —— 立即拆掉，不是等下一次配置变更
        3. 远程管理操作与状态灯 —— 面板置灰（``refresh_target``）
        4. 周期 HTTP 探活 —— ``_poll_bridges`` 里的 target_host 判据自动跳过
        """
        self._config.remote_bridge().enabled = enabled
        self._save_config()

        if not enabled:
            # 不清掉的话，重新启用后 chip 会先显示上一次的「在线」，
            # 直到下一轮探活（最多 5 秒）才纠正过来 —— 一段凭空的假状态
            self._remote_http_online = False

        # 隧道是绑在目标上的：取消防抖立即同步，禁用要立刻拆、启用要立刻建
        self._tunnel_sync.stop()
        # 先重画灯再同步隧道：apply_tunnel 会用真实运行态覆盖隧道那盏，
        # 另两盏（SSH / 桥接服务）没有新探测结果，必须由 refresh_lights 立刻改口
        self._remote.refresh_lights()
        self._refresh_tunnel()
        self._remote.refresh_target()
        self._update_remote_summary()
        self._update_options_summary()
        self._update_status_bar()
        self._append(
            "info",
            f"远程桥接服务已{'启用' if enabled else '禁用'}"
            + ("" if enabled else "，Host B 切换前将不再执行安全弹出"),
        )

    def _on_remote_install(self) -> None:
        """安装前先做本地校验。

        参数没填全时立刻给出「哪个字段不对」，而不是让用户等一次 SSH 超时
        再收到一句笼统的失败。
        """
        errors = self._remote.validation_errors()
        if errors:
            for message in errors:
                self._append("error", f"远程配置不完整：{message}")
            self._sections["remote"].set_expanded(True)
            return
        self._remote_worker.install()

    def _on_remote_state(self, state: object) -> None:
        self._remote.apply_state(state)
        self._remote_http_online = bool(getattr(state, "http_online", False))
        self._update_remote_summary()
        self._update_status_bar()

    def _on_remote_env(self, env: object) -> None:
        self._remote.apply_env(env)

    def _on_remote_busy(self, busy: bool) -> None:
        self._remote.set_busy(busy)
        # 配置窗口开着时一并置灰 —— 边测边改会让「测的是哪一版」变得含糊
        if self._remote_dialog is not None:
            self._remote_dialog.set_busy(busy)

    def _on_tunnel_state(self, state: object) -> None:
        self._remote.apply_tunnel(state)
        self._update_remote_summary()
        self._update_status_bar()

    def _refresh_tunnel(self) -> None:
        """把配置变更同步到隧道，并把当前状态回填到面板。"""
        if self._shutdown_done:
            # 退出过程中配置窗口可能刚被关掉，它的 finally 会走到这里 ——
            # 那时 tunnel worker 已经停了，再同步只会炸出一串噪声
            return
        self._tunnel_worker.sync()
        self._remote.apply_tunnel(self._tunnel_worker.state)
        self._update_remote_summary()
        self._update_status_bar()

    def open_remote_config(self) -> None:
        """打开远程连接配置窗口（模态）。

        同一时刻只允许一个：配置窗口里的控件直接读写同一份 `AppConfig`，
        开两个会让「哪一份是当前的」变得不确定。

        **用 ``setModal(True) + show()`` 而不是 ``exec()``。** ``exec()`` 会起一个
        嵌套事件循环，于是「配置窗口开着时从托盘退出」这条路径上，
        ``QApplication.quit()`` 只能退出嵌套的那个循环，主循环还在跑 ——
        进程不结束；而试图同步关掉它又会踩到重入。不用嵌套循环，整类问题都不存在。
        """
        if self._remote_dialog is not None:
            self._remote_dialog.raise_()
            self._remote_dialog.activateWindow()
            return

        dialog = RemoteConfigDialog(self._config, self)
        dialog.setModal(True)
        dialog.configEdited.connect(self._on_remote_config_edited)
        dialog.testRequested.connect(self._remote_worker.testConnection)
        dialog.finished.connect(self._on_remote_config_closed)
        self._remote_dialog = dialog
        dialog.show()

    def _on_remote_config_closed(self, _result: int) -> None:
        dialog = self._remote_dialog
        self._remote_dialog = None
        if dialog is not None:
            dialog.deleteLater()

        # 用户已经改完了，不必再等防抖
        self._tunnel_sync.stop()
        self._refresh_tunnel()
        self._remote.refresh_target()
        self._update_remote_summary()
        self._update_options_summary()

    def _on_remote_config_edited(self) -> None:
        """远程配置改动后立即持久化，并把隧道同步延后合并成一次。

        本机端口取 ``config.local_bridge_port``（本机恒为 Host A），不写死角色。
        """
        self._save_config()
        self._update_remote_summary()
        # Host B 的安全弹出锁跟随远程配置的有无
        self._update_options_summary()
        self._remote.refresh_target()

        # 目标主机 / 隧道开关一变，隧道就要跟着重建 —— 它是绑在具体目标上的。
        # 但配置窗口是**逐键**提交的，直接重建意味着输入一个 IP 要建拆十几次
        # SSH 连接。这里合并成一次；窗口关闭时立刻补做。
        self._tunnel_sync.start()

        port = self._config.local_bridge_port
        if self._bridge_worker.port == port or self._warned_local_port == port:
            return

        # 端口变化会被 BridgeWorker 在下一次「启动」时自动跟进，不必重启程序；
        # 但**正在运行**的服务仍留在旧端口上，所以要提醒一句。同一个端口值
        # 只提醒一次，免得每次敲键盘都往日志里灌一遍。
        self._warned_local_port = port
        if self._bridge_worker.is_started:
            self._append(
                "warning",
                f"本机桥接端口已改为 {port}。当前服务仍在 {self._bridge_worker.port} 上运行，"
                "请先「停止」再「启动」以切换端口。",
            )
        else:
            self._append(
                "info",
                f"本机桥接端口已改为 {port}，点击「启动」即按新端口监听。",
            )

    # -- 选项 --------------------------------------------------------------- #

    def _on_close_to_tray_changed(self, enabled: bool) -> None:
        self._config.window.close_to_tray = enabled
        self._save_config()
        self._update_options_summary()
        self._append(
            "info",
            "关闭窗口后将留在托盘继续运行" if enabled else "关闭窗口将直接退出程序",
        )

    def _on_autostart_changed(self, enabled: bool) -> None:
        """启用/关闭开机自启。

        写入后一律以**注册表的实际状态**回填两处入口 —— 写成功不代表生效，
        可能被组策略或安全软件拦下。
        """
        try:
            actual = autostart.sync_to(enabled)
        except ConfigError as exc:
            self._append("error", f"开机自启设置失败：{exc}")
            self._sync_autostart_from_registry()
            return

        self._config.autostart.enabled = actual
        self._save_config()
        self._options.set_autostart(actual)
        self._tray.set_autostart(actual)
        self._update_options_summary()
        self._append("info", f"开机自启已{'启用' if actual else '关闭'}")

    def _sync_autostart_from_registry(self) -> None:
        """启动时以注册表为准回填 UI —— 该项可能被安全软件或用户手工清理。"""
        actual = autostart.is_enabled()
        self._options.set_autostart(actual)
        self._tray.set_autostart(actual)
        if self._config.autostart.enabled != actual:
            self._config.autostart.enabled = actual
            self._save_config()

    # -- 窗口与托盘 --------------------------------------------------------- #

    def toggle_window(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.show_and_activate()

    def show_and_activate(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_application(self) -> None:
        """托盘「退出」—— 唯一真正结束进程的入口。"""
        self.shutdown()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event) -> None:  # noqa: N802 —— Qt 命名
        self._save_geometry()
        if self._config.window.close_to_tray and not self._shutdown_done:
            event.ignore()
            self.hide()
            self._notify_first_minimize()
            return

        self.shutdown()
        super().closeEvent(event)

    def _notify_first_minimize(self) -> None:
        """首次收起时提示一次，之后不再打扰（否则每次关窗都弹很烦）。"""
        if self._config.window.tray_hint_shown:
            return
        self._config.window.tray_hint_shown = True
        self._save_config()
        self._tray.notify(
            "USB Switch Console 仍在后台运行",
            "程序已最小化到托盘，U 盘切换与会话保持可用。双击托盘图标可重新打开窗口。",
        )

    # -- 生命周期 ----------------------------------------------------------- #

    def shutdown(self) -> None:
        """统一退出路径，幂等。

        托盘「退出」、关闭窗口（未开托盘常驻）、``aboutToQuit`` 都只调它，
        避免出现「某条路径漏了清理」导致端口或 BLE 会话残留。
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True

        # 配置窗口若开着要关掉：它关窗时要做一次隧道同步与摘要刷新，
        # 拖到 worker 全停之后再跑就只会留下一串噪声
        if self._remote_dialog is not None:
            self._remote_dialog.close()

        self._bridge_poll.stop()
        self._tunnel_sync.stop()
        self._save_geometry()
        self._save_config()

        # 先停桥接：它会释放端口，且不依赖 BLE
        self._bridge_worker.shutdown()
        # 隧道必须早于 BLE worker 停：ejector 依赖它定位远端桥接地址
        self._tunnel_worker.shutdown()
        # 远程 worker 只是自己的命令队列，停掉即可（远端服务不受影响）
        self._remote_worker.shutdown()
        # 再停 BLE：必须阻塞到线程真正退出，否则会残留 BLE 会话
        self._worker.shutdown()

        self._tray.hide()
        log.info("已退出")

    def _append(self, level: str, message: str) -> None:
        self._log_panel.append(level, message)
        section = self._sections.get("log")
        if section is not None:
            # 折叠态下摘要行就是用户唯一看得到的东西，所以拿最后一条日志填它
            section.set_summary(message)
            section.set_chip("错误" if level == "error" else "", "error")

        log.log(
            {"debug": 10, "info": 20, "success": 20, "warning": 30, "error": 40}.get(level, 20),
            message,
        )

    def _restore_geometry(self) -> None:
        """恢复窗口几何。

        这里曾经多写了一个 ``raw.startswith(FIRST_MINIMIZE_HINT)`` 判断，而那个常量
        早已在重构中被删掉 —— 于是**第二次启动必崩**（首次启动 geometry 还是空的，
        `or` 短路让那句永远不被求值，所以测试一路绿灯）。已改为只判空。
        """
        raw = self._config.window.geometry
        if not raw:
            return
        try:
            self.restoreGeometry(QByteArray.fromBase64(raw.encode("ascii")))
        except (ValueError, UnicodeEncodeError):
            log.debug("窗口几何信息无法解析，已忽略")

    def _save_geometry(self) -> None:
        self._config.window.geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")

    def _save_config(self) -> None:
        try:
            config_module.save(self._config)
        except OSError as exc:
            log.warning("保存配置失败: %s", exc)
