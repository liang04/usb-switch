"""远程（Linux）桥接服务面板。

**只保留状态与操作，配置表单搬到了** :mod:`usbswitch.ui.remote_config_dialog`。
理由是这张表单有 10 个输入框、属于一次性配置，却和「切换 U 盘」那种高频操作
争主窗口面积。现在这里是一行目标摘要 + 一个「配置…」按钮。

上一层（主窗口）负责把 `configureRequested` 接到对话框、把 `testRequested`
等接到 worker —— 本面板不持有任何业务逻辑。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.bridge_remote import RemoteEnv, RemoteState
from ...core.models import AppConfig, BridgeRunState
from ..theme import THEME
from ..widgets import ElidedLabel, StatusLight

NOT_REMOTE_HINT = "点击「配置…」设置 Host B（Linux）的远程桥接 —— 配置后 Host B 安全弹出锁即启用"
DISABLED_HINT = (
    "远程桥接服务已禁用：Host B 切换前不再执行安全弹出，SSH 隧道与远程管理操作一并停用。"
    "远端参数会保留，重新启用即恢复。"
)


class RemotePanel(QGroupBox):
    testRequested = Signal()
    installRequested = Signal()
    startRequested = Signal()
    stopRequested = Signal()
    uninstallRequested = Signal()
    refreshRequested = Signal()
    #: 用户点了「配置…」→ 主窗口打开配置对话框
    configureRequested = Signal()
    #: 总开关被用户切换 → 主窗口落盘并同步隧道 / 弹出锁
    enabledChanged = Signal(bool)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__("远程桥接服务", parent)
        self._config = config
        self._busy = False
        self._ready = False

        self._enable = QCheckBox("启用远程桥接")
        self._enable.setToolTip(
            "关闭后 Host B 视为「不使用远程桥接」：切换前不再要求安全弹出，\n"
            "SSH 隧道自动停止，安装/启动/卸载等远程操作置灰。"
        )
        # 用 clicked 而不是 toggled：toggled 会被 refresh_target() 里的回填触发，
        # 于是「刷新界面」变成「又写一次配置」，日志里会多出一串假变更记录。
        #
        # 但**不能**写成 `clicked.connect(self.enabledChanged.emit)`：PySide6 给
        # `QCheckBox.clicked` 注册了 `clicked()` 与 `clicked(bool)` 两个重载，接到
        # 「需要一个参数」的 callable 上会命中**无参**那个 —— 参数丢空、抛
        # TypeError、槽静默不执行（只在 stderr 留一行，打包后完全不可见）。
        # 于是「取消勾选」看着生效，配置却没落盘，重启又变回勾选。接一个自己读
        # 控件状态的槽，与 `clicked` 传几个参数彻底解耦。
        self._enable.clicked.connect(self._on_enable_clicked)

        self._target = ElidedLabel("")
        self._target.setObjectName("SectionSummary")

        self._btn_configure = QPushButton("配置…")
        self._btn_configure.setToolTip("打开配置窗口：远端地址、SSH 凭据、端口、隧道")
        self._btn_configure.clicked.connect(self.configureRequested.emit)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        toolbar.addWidget(self._enable)
        toolbar.addWidget(self._target, 1)
        toolbar.addWidget(self._btn_configure)

        self._light_ssh = StatusLight("SSH  未检测")
        self._light_tunnel = StatusLight("SSH 隧道  未启用")
        self._light_http = StatusLight("桥接服务  未检测")

        lights = QGridLayout()
        lights.setContentsMargins(0, 0, 0, 0)
        lights.setVerticalSpacing(4)
        lights.addWidget(self._light_ssh, 0, 0)
        lights.addWidget(self._light_tunnel, 1, 0)
        lights.addWidget(self._light_http, 2, 0)

        self._btn_test = QPushButton("测试连接")
        self._btn_install = QPushButton("安装")
        self._btn_install.setObjectName("Primary")
        self._btn_start = QPushButton("启动")
        self._btn_stop = QPushButton("停止")
        self._btn_uninstall = QPushButton("卸载")
        self._btn_refresh = QPushButton("刷新")

        self._btn_test.clicked.connect(self.testRequested.emit)
        self._btn_install.clicked.connect(self.installRequested.emit)
        self._btn_start.clicked.connect(self.startRequested.emit)
        self._btn_stop.clicked.connect(self.stopRequested.emit)
        self._btn_uninstall.clicked.connect(self.uninstallRequested.emit)
        self._btn_refresh.clicked.connect(self.refreshRequested.emit)

        self._ops = [
            self._btn_test,
            self._btn_install,
            self._btn_start,
            self._btn_stop,
            self._btn_uninstall,
            self._btn_refresh,
        ]

        ops_row = QHBoxLayout()
        ops_row.setSpacing(8)
        for button in self._ops:
            ops_row.addWidget(button)
        ops_row.addStretch(1)

        self._hint = QLabel("")
        self._hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(9)
        layout.addLayout(toolbar)
        layout.addLayout(lights)
        layout.addLayout(ops_row)
        layout.addWidget(self._hint)

        self.refresh_target()
        self.apply_tunnel(BridgeRunState.STOPPED)

    # ---------------------------------------------------------- 配置校验 #

    def validation_errors(self) -> list[str]:
        """安装 / 启动前的前置校验。

        从**配置**读而不是从控件读 —— 表单已搬到独立窗口，而且校验本来就该针对
        「将要执行的配置」，而不是「某个界面上还没提交的内容」。
        """
        host = self._config.remote_host()
        if host is None:
            return []
        return self._config.endpoint_for(host).remote.validate()

    def is_enabled(self) -> bool:
        """总开关的配置值（不是控件的勾选态）。"""
        return self._config.remote_bridge().enabled

    def refresh_target(self) -> None:
        """按配置刷新开关、目标摘要与按钮可用性。配置改完必须调一次。"""
        self._refresh_enable()
        remote = self._config.remote_bridge()

        if not remote.enabled:
            where = remote.host.strip() or "未填地址"
            self._target.setText(f"已禁用 · Host B · {where}")
            self._set_hint(DISABLED_HINT, THEME["warning"])
            self._ready = False
            self._apply_interactivity()
            return

        host = self._config.remote_host()
        if host is None:
            self._target.setText("Host B 未配置远程桥接")
            self._set_hint(NOT_REMOTE_HINT, THEME["muted"])
            self._ready = False
            self._apply_interactivity()
            return

        name = remote.display_name or remote.host or "未填写地址"
        user = remote.username or "未填用户名"
        where = remote.host or "未填地址"
        transport = "SSH 隧道" if remote.use_tunnel else f"直连 :{remote.bridge_port}"
        self._target.setText(
            f"{host.label} · {name} · {user}@{where}:{remote.ssh_port} · {transport}"
        )

        errors = remote.validate()
        if errors:
            # 只**警告**，不禁用 —— `validate()` 比现实更严（例如「私钥认证必须
            # 填路径」，而 paramiko 在没有路径时会回退到 agent / 默认密钥，实测
            # 照样连得上）。用一条过严的判据把按钮锁死，会把本来能用的配置堵住。
            self._set_hint("配置可能不完整：" + "；".join(errors), THEME["warning"])
        else:
            self._set_hint("", THEME["muted"])

        # 门控只有一条：配了远程角色就允许操作。剩下的交给点击后的前置校验 ——
        # 它会给出同样具体、但不会挡住人的错误。
        self._ready = True
        self._apply_interactivity()

    # ------------------------------------------------------------ 状态 #

    def apply_state(self, state: RemoteState) -> None:
        if not self._config.remote_bridge().enabled:
            self._set_lights_idle("—（远程桥接服务已禁用）")
            return
        if self._config.remote_host() is None:
            self._set_lights_idle("—（未配置远程桥接）")
            return

        if not state.ssh_checked:
            # 只做了 HTTP 探活，SSH 那栏如实显示「未检测」——
            # 画成红色会让用户以为连不上，画成绿色则是在撒谎
            self._light_ssh.set_state("SSH  未检测（点击「刷新」探测）", THEME["idle"])
        else:
            self._light_ssh.set_state(
                f"SSH  {'可达' if state.ssh_reachable else '不可达'} · {state.ssh_detail}",
                THEME["success"] if state.ssh_reachable else THEME["danger"],
            )

        if state.http_online:
            self._light_http.set_state(f"桥接服务  在线 · {state.http_detail}", THEME["success"])
        elif state.ssh_checked and state.ssh_reachable:
            # SSH 通但 HTTP 不通：这是**要查的故障**，不是「本来就没开」
            self._light_http.set_state(
                f"桥接服务  离线 · {state.http_detail}", THEME["warning"]
            )
        else:
            self._light_http.set_state(f"桥接服务  {state.http_detail}", THEME["idle"])

    def apply_env(self, env: RemoteEnv) -> None:
        """测试连接成功后展示远端环境，让用户确认装到哪里、以什么方式常驻。"""
        self._light_ssh.set_state(
            f"SSH  可达 · {env.describe()}",
            THEME["success"] if env.ready else THEME["warning"],
        )

    def apply_tunnel(self, state: BridgeRunState) -> None:
        """隧道状态灯。隧道与 SSH / HTTP 是三件独立的事，必须分开显示。

        - SSH 可达不代表隧道建起来了（可能刚被关掉，或目标刚改过）；
        - 隧道通了也不代表桥接服务在跑（服务可能没装、没启动）。

        读的是**配置里**的隧道开关，不是某个控件 —— 否则关掉配置窗口后
        这盏灯就失去了判断依据。
        """
        if not self._config.remote_bridge().enabled:
            self._light_tunnel.set_state("SSH 隧道  —（远程桥接服务已禁用）", THEME["idle"])
            return
        host = self._config.remote_host()
        if host is None:
            self._light_tunnel.set_state("SSH 隧道  —（未配置远程桥接）", THEME["idle"])
            return
        if not self._config.endpoint_for(host).remote.use_tunnel:
            self._light_tunnel.set_state("SSH 隧道  未启用", THEME["idle"])
            return

        if state is BridgeRunState.RUNNING:
            self._light_tunnel.set_state("SSH 隧道  已打通", THEME["success"])
        elif state is BridgeRunState.STARTING:
            self._light_tunnel.set_state("SSH 隧道  建立中…", THEME["warning"])
        elif state is BridgeRunState.ERROR:
            self._light_tunnel.set_state("SSH 隧道  失败（详见日志）", THEME["danger"])
        else:
            self._light_tunnel.set_state("SSH 隧道  未启用", THEME["idle"])

    # ------------------------------------------------------------ 可用性 #

    def refresh_lights(self) -> None:
        """按配置把三盏灯重画到「本轮尚未探测」的起始态。

        总开关刚切换时必须调它。新一轮探测结果要等最多一个轮询周期才回来，
        不重画的话灯会停在切换前的旧内容上 —— 分区头写着「已禁用」，灯却显示
        「在线」，同一个分区里自相矛盾；反过来刚启用时灯还挂着「已禁用」。

        注意「还没探测」与「探测到不可达」是两件事：前者是 idle 灰灯，后者才是
        红灯。所以这里给的是 idle 文案，不是失败文案。
        """
        remote = self._config.remote_bridge()
        if not remote.enabled:
            self._set_lights_idle("—（远程桥接服务已禁用）")
            return
        if self._config.remote_host() is None:
            self._set_lights_idle("—（未配置远程桥接）")
            return

        # 配置变了，旧的探测结论一律作废：SSH 与 HTTP 回到「未检测」，
        # 隧道则由 apply_tunnel 按最新的配置与运行态重新表述
        self._light_ssh.set_state("SSH  未检测（点击「刷新」探测）", THEME["idle"])
        self._light_http.set_state("桥接服务  未检测", THEME["idle"])
        self.apply_tunnel(BridgeRunState.STOPPED)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._apply_interactivity()

    def _apply_interactivity(self) -> None:
        # 安装 / 启动期间不允许改配置 —— 边装边改会让「装的是哪一版」变得含糊
        self._btn_configure.setEnabled(not self._busy)
        # 总开关**不随 `_ready` 置灰**：禁用之后它就是唯一能把服务重新打开的
        # 入口，跟着一起灰掉用户就再也回不来了
        self._enable.setEnabled(not self._busy)
        for button in self._ops:
            button.setEnabled(self._ready and not self._busy)

    # ------------------------------------------------------------ 内部 #

    def _on_enable_clicked(self) -> None:
        """用户点了总开关 —— 值**从控件现读**，不看 `clicked` 带了什么参数。

        这是唯一发出 :attr:`enabledChanged` 的地方，所以「点击生效」只依赖
        控件状态本身，不依赖信号重载的解析结果（见 ``__init__`` 里的注释）。
        """
        self.enabledChanged.emit(self._enable.isChecked())

    def _refresh_enable(self) -> None:
        """把配置里的启用状态回填到勾选框。

        回填时**不触发** `enabledChanged`（信号接的是 `clicked`，而这里是
        `setChecked`）—— 否则每次刷新目标摘要都会再写一次配置，日志里凭空多出
        一串「远程桥接服务已启用」，用户会以为自己在反复误触。
        """
        self._enable.setChecked(self._config.remote_bridge().enabled)

    def _set_hint(self, text: str, color: str) -> None:
        self._hint.setText(text)
        self._hint.setVisible(bool(text))
        self._hint.setStyleSheet(f"color: {color};" if text else "")

    def _set_lights_idle(self, suffix: str) -> None:
        for light, name in (
            (self._light_ssh, "SSH"),
            (self._light_tunnel, "SSH 隧道"),
            (self._light_http, "桥接服务"),
        ):
            light.set_state(f"{name}  {suffix}", THEME["idle"])
