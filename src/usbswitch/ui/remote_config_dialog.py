"""桥接服务配置对话框。

**为什么把表单从主窗口搬出来。**
这张表单有 10 个输入框，是一次性配置，却和「切换 U 盘」那种高频操作占同样
的主窗口面积。搬进独立窗口后，主窗口只留一行目标摘要 + 操作按钮。

沿用与主窗口一致的语义：**改一次存一次**（`configEdited` → 主窗口持久化）。
所以底部按钮是「完成」而不是「取消」—— 它不承诺回滚，也就不会骗人。

角色固定后这里只有两块内容
--------------------------

- **Host A · 本机桥接服务** —— 本机（Windows）桥接服务监听的端口；
- **Host B · 远程连接** —— 远程（Linux）桥接的地址、凭据、端口、隧道。

物理拓扑只有这一种：本机必然占一路 Host，另一路是 Linux 设备。曾经允许
「任意角色选本机 / 远程」，代价是多出两个下拉框，以及「两个角色都设成本机时
只有第一个生效」这类静默失效的配置。

版面约束（都是踩过才这么写的）
------------------------------

1. **两块内容各自成一个分组框，本机端口有自己的一组。** 它曾经是一行裸控件挂在
   根布局上、紧挨着 Host B 的卡片 —— 读起来像那张卡的说明文字。
   它**不能**并进 Host B 那一组：分组会整体 `setEnabled(False)`，子控件继承禁用
   状态，于是这个端口在两种模式下都改不了。
2. **行内第二个字段的标签定宽**（:func:`_inline_label`）。否则「用户名」与「别名」
   宽度不同，两行的第二个输入框左边缘差几像素 —— 单看没事，整体就是「没对齐」。
3. **「SSH 端口」与「桥接端口」是两个独立输入框，且分处两段。** 这是最容易填反的
   地方（SSH 走 22，桥接 HTTP 走 8738），填反了症状很像「网络不通」，排查代价极高。
4. **密码框回填时只给掩码，不回显明文。** 状态用**徽标**表达而不是 placeholder ——
   placeholder 在用户敲下第一个字符时就消失了，那正是他最需要看到「原来存过密码」
   的时刻。
5. **两个按钮都 `setAutoDefault(False)`。** 否则「完成」是默认按钮，用户在 IP 框里
   按回车想确认这一格，窗口直接关了。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.models import (
    DEFAULT_LOCAL_BRIDGE_PORT,
    DEFAULT_REMOTE_BRIDGE_PORT,
    AppConfig,
    AuthMethod,
    Host,
    RemoteBridgeConfig,
)
from .theme import SPACE
from .widgets import Chip, ToggleSwitch, repolish

PASSWORD_KEEP_HINT = "留空则不修改"
PASSWORD_EMPTY_HINT = "SSH 登录密码"
KEY_HINT = "私钥文件（PEM / OpenSSH）"
KEY_TOOLTIP = "支持 PEM 与 OpenSSH 两种格式；只有「认证方式」选私钥时才生效。"

TUNNEL_LABEL = "经 SSH 隧道访问"
TUNNEL_TOOLTIP = (
    "把远端 127.0.0.1:<桥接端口> 经 SSH 映射到本机一个端口，再从本机访问。\n"
    "适用于远端防火墙不允许直连桥接端口的情况，也让桥接服务不必暴露在网络上。"
)

#: 分组框的 objectName —— 让 QSS 用更紧凑的内边距（全局那套是给分区卡片调的）
FORM_GROUP = "FormGroup"

#: 行内第二个字段的标签宽度。取「用户名」（最宽的一个）的实测宽，多留一点余量。
_INLINE_LABEL_WIDTH = 56


def _inline_label(text: str) -> QLabel:
    """行内第二个字段的标签：定宽，好让相邻两行的第二个输入框左边缘对齐。"""
    label = QLabel(text)
    label.setFixedWidth(_INLINE_LABEL_WIDTH)
    return label


class RemoteConfigDialog(QDialog):
    #: 用户改动了配置（连接参数）→ 主窗口负责持久化
    configEdited = Signal()
    #: 请求测试连接 → 主窗口路由到 RemoteWorker
    testRequested = Signal()

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._current = Host.B
        self._loading = False
        self._busy = False

        self.setWindowTitle("桥接服务配置")
        self.setMinimumWidth(620)

        self._build()
        self.reload()

    # ------------------------------------------------------------------ 构建 #

    def _build(self) -> None:
        self._local_box = self._build_local_group()
        self._creds = self._build_remote_group()

        self._hint_bar = self._build_hint_bar()

        self._btn_test = QPushButton("测试连接")
        self._btn_test.clicked.connect(self.testRequested.emit)

        self._btn_done = QPushButton("完成")
        self._btn_done.setObjectName("Primary")
        self._btn_done.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addWidget(self._btn_test)
        buttons.addStretch(1)
        buttons.addWidget(self._btn_done)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE["lg"], SPACE["lg"], SPACE["lg"], SPACE["lg"])
        layout.setSpacing(SPACE["md"])
        layout.addWidget(self._local_box)
        layout.addWidget(self._creds)
        layout.addWidget(self._hint_bar)
        # 留白放在提示条与按钮之间：窗口被拉高时松的是这里，而不是把表单行撑开
        layout.addStretch(1)
        layout.addLayout(buttons)

        self._wire()

        # 统一关掉 autoDefault，必须放在**所有控件都进了窗口之后**（分组框要等
        # `layout.addWidget` 才被 reparent 到对话框，在那之前 findChildren 找不到
        # 里面的按钮）。逐个设置只是「记得改」的运气：漏掉的那个会安静地抢走回车
        # —— 「完成」抢走是窗口直接关掉，「浏览…」抢走是弹出文件对话框，一样难用。
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)

    def _build_local_group(self) -> QGroupBox:
        self._local_port = QSpinBox()
        self._local_port.setRange(1, 65535)
        self._local_port.setValue(DEFAULT_LOCAL_BRIDGE_PORT)
        self._local_port.setFixedWidth(96)
        self._local_port.setToolTip("本机桥接服务监听的端口，通常是 8737")

        self._local_port_label = QLabel("监听端口")
        self._local_hint = QLabel("切换前由它执行安全弹出")
        self._local_hint.setObjectName("Muted")

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE["md"])
        row.addWidget(self._local_port_label)
        row.addWidget(self._local_port)
        row.addWidget(self._local_hint, 1)

        box = QGroupBox("Host A · 本机桥接服务（Windows）")
        box.setObjectName(FORM_GROUP)
        box.setLayout(row)
        return box

    def _build_remote_group(self) -> QGroupBox:
        self._host = QLineEdit()
        self._host.setPlaceholderText("192.168.1.100 或主机名")

        self._ssh_port = QSpinBox()
        self._ssh_port.setRange(1, 65535)
        self._ssh_port.setValue(22)
        self._ssh_port.setToolTip("SSH 管理通道端口，通常是 22")
        self._ssh_port.setFixedWidth(96)

        self._username = QLineEdit()
        self._username.setPlaceholderText("登录用户名")

        self._auth = QComboBox()
        self._auth.addItem("密码", AuthMethod.PASSWORD)
        self._auth.addItem("私钥", AuthMethod.KEY)
        # 不跟着其它字段一起拉伸：只有两个短选项，撑满一行既难看、又容易被当成
        # 可以填字的输入框
        self._auth.setMinimumWidth(140)
        auth_row = QHBoxLayout()
        auth_row.setContentsMargins(0, 0, 0, 0)
        auth_row.addWidget(self._auth)
        auth_row.addStretch(1)

        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.Password)
        self._password.setPlaceholderText(PASSWORD_EMPTY_HINT)

        #: 密码存储状态。**不能**只靠 placeholder 表达 —— 见模块 docstring 第 4 条。
        self._password_state = Chip("", "idle")

        password_row = QHBoxLayout()
        password_row.setContentsMargins(0, 0, 0, 0)
        password_row.setSpacing(SPACE["sm"])
        password_row.addWidget(self._password, 1)
        password_row.addWidget(self._password_state)

        self._key_path = QLineEdit()
        self._key_path.setPlaceholderText(KEY_HINT)
        self._key_path.setToolTip(KEY_TOOLTIP)

        self._btn_browse = QPushButton("浏览…")
        self._btn_browse.setFixedWidth(76)
        self._btn_browse.clicked.connect(self._pick_key)

        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.setSpacing(SPACE["sm"])
        key_row.addWidget(self._key_path, 1)
        key_row.addWidget(self._btn_browse)

        self._bridge_port = QSpinBox()
        self._bridge_port.setRange(1, 65535)
        self._bridge_port.setValue(DEFAULT_REMOTE_BRIDGE_PORT)
        self._bridge_port.setToolTip("远端桥接服务监听的 HTTP 端口，通常是 8738")
        self._bridge_port.setFixedWidth(96)

        self._display_name = QLineEdit()
        self._display_name.setPlaceholderText("仅用于显示")

        bridge_row = QHBoxLayout()
        bridge_row.setContentsMargins(0, 0, 0, 0)
        bridge_row.setSpacing(SPACE["sm"])
        bridge_row.addWidget(self._bridge_port)
        bridge_row.addWidget(_inline_label("别名"))
        bridge_row.addWidget(self._display_name, 1)

        self._tunnel = ToggleSwitch(TUNNEL_LABEL)
        self._tunnel.setToolTip(TUNNEL_TOOLTIP)

        self._tunnel_port = QSpinBox()
        self._tunnel_port.setRange(0, 65535)
        self._tunnel_port.setValue(0)
        self._tunnel_port.setFixedWidth(96)
        self._tunnel_port.setSpecialValueText("自动")
        self._tunnel_port.setToolTip(
            "隧道在本机监听的端口。0 = 每次自动挑一个空闲端口（推荐）——\n"
            "写死端口会平白多出一类「本地端口被占用」的失败。"
        )
        # 单独持有标签对象，好在隧道关闭时把它一起置灰（用字符串加行就没法控制）
        self._tunnel_port_label = QLabel("隧道端口")

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(SPACE["md"])
        form.setVerticalSpacing(SPACE["sm"])
        form.addRow("地址", self._host)

        port_row = QHBoxLayout()
        port_row.setContentsMargins(0, 0, 0, 0)
        port_row.setSpacing(SPACE["sm"])
        port_row.addWidget(self._ssh_port)
        port_row.addWidget(_inline_label("用户名"))
        port_row.addWidget(self._username, 1)
        form.addRow("SSH 端口", port_row)

        form.addRow("认证方式", auth_row)
        form.addRow("密码", password_row)
        form.addRow("私钥", key_row)

        # 凭据与「桥接服务」不是一回事，但都属于 Host B。拆成两个分组框会把对话框
        # 撑得更高，一条分隔线足够表达分段。
        form.addRow(self._build_section_rule("桥接服务"))

        form.addRow("桥接端口", bridge_row)
        form.addRow("", self._tunnel)
        form.addRow(self._tunnel_port_label, self._tunnel_port)

        box = QGroupBox("Host B · 远程连接（Linux）")
        box.setObjectName(FORM_GROUP)
        box.setLayout(form)
        return box

    def _build_section_rule(self, text: str) -> QWidget:
        """分组内的小节分隔：一条细线 + 右侧小标题。"""
        line = QWidget()
        line.setObjectName("Rule")
        line.setFixedHeight(1)

        label = QLabel(text)
        label.setObjectName("SubTitle")

        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, SPACE["sm"], 0, 0)
        row.setSpacing(SPACE["sm"])
        # 标签在**左**、线向右延伸：反过来（线在左、标签贴右）会让标题飘到离内容
        # 最远的一角，读起来像个孤儿
        row.addWidget(label)
        row.addWidget(line, 1)
        return holder

    def _build_hint_bar(self) -> QFrame:
        """底部提示条。

        颜色由 :meth:`_set_hint_state` 设属性 + repolish 决定，**不写死样式** ——
        否则换主题时要在这里再改一遍，也会盖掉控件级 QSS。
        """
        self._validation = QLabel("")
        self._validation.setObjectName("Muted")
        self._validation.setWordWrap(True)

        bar = QFrame()
        bar.setObjectName("HintBar")
        bar.setProperty("state", "info")
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(SPACE["md"], SPACE["sm"], SPACE["md"], SPACE["sm"])
        layout.setSpacing(0)
        layout.addWidget(self._validation)
        return bar

    def _wire(self) -> None:
        self._auth.currentIndexChanged.connect(self._on_auth_changed)
        for field in (
            self._host,
            self._username,
            self._key_path,
            self._display_name,
            self._password,
        ):
            field.textEdited.connect(self._on_field_edited)
        for spin in (self._ssh_port, self._bridge_port, self._local_port, self._tunnel_port):
            # valueChanged 会带一个 int，用 lambda 吞掉，免得和「无参槽」签名打架
            spin.valueChanged.connect(lambda _value: self._on_field_edited())
        self._tunnel.toggled.connect(lambda _checked: self._on_tunnel_toggled())

    # ------------------------------------------------------- 配置读取与回填 #

    def reload(self) -> None:
        """把配置回填到控件。角色固定，因此读的位置也是固定的。"""
        self._loading = True
        try:
            remote = self._config.endpoint_for(Host.B).remote
            self._local_port.setValue(self._config.local_bridge_port)

            self._host.setText(remote.host)
            self._ssh_port.setValue(remote.ssh_port)
            self._username.setText(remote.username)
            self._auth.setCurrentIndex(0 if remote.auth is AuthMethod.PASSWORD else 1)
            self._bridge_port.setValue(remote.bridge_port)
            self._display_name.setText(remote.display_name)
            self._key_path.setText(remote.key_path)
            self._tunnel.setChecked(remote.use_tunnel)
            self._tunnel_port.setValue(remote.tunnel_local_port)

            # 密码永不回显明文：有则只给提示，输入框留空
            self._password.clear()
            self._password.setPlaceholderText(
                PASSWORD_KEEP_HINT if remote.password else PASSWORD_EMPTY_HINT
            )

            self._apply_mode()
        finally:
            self._loading = False
        self._refresh_password_chip()
        self._validate()

    def _refresh_password_chip(self) -> None:
        """密码徽标跟随「配置里到底有没有密码」—— 与认证方式无关。"""
        saved = bool(self.current_remote().password)
        self._password_state.set_state("已保存" if saved else "未设置", "ok" if saved else "idle")

    def _apply_mode(self) -> None:
        """各控件该不该可编辑。角色固定后只剩认证方式与隧道两处联动。"""
        is_password = self.current_auth() is AuthMethod.PASSWORD
        self._password.setEnabled(is_password)
        self._password_state.setEnabled(is_password)
        self._key_path.setEnabled(not is_password)
        self._btn_browse.setEnabled(not is_password)

        # 只有开了隧道才需要「隧道端口」
        tunnel_on = self._tunnel.isChecked()
        self._tunnel_port.setEnabled(tunnel_on)
        self._tunnel_port_label.setEnabled(tunnel_on)

    # ------------------------------------------------------------ 槽 #

    def _on_tunnel_toggled(self) -> None:
        if self._loading:
            return
        self._commit()
        self._apply_mode()
        self._validate()
        self.configEdited.emit()

    def _on_auth_changed(self, _index: int = 0) -> None:
        if self._loading:
            return
        self._commit()
        self._apply_mode()

    def _on_field_edited(self) -> None:
        if self._loading:
            return
        self._commit()
        self._refresh_password_chip()
        self._validate()
        self.configEdited.emit()

    # ---------------------------------------------------------- 值读取 #

    # 注意：`QComboBox.currentData()` 对 `str` 混合枚举会**退化成普通字符串**，
    # `is BridgeKind.REMOTE` 一律为假 —— 结果是远程控件永远置灰。
    # 所以这里一律用枚举构造器转回来，不依赖 Qt 的类型保真。

    def current_auth(self) -> AuthMethod:
        return AuthMethod(self._auth.currentData())

    def current_remote(self) -> RemoteBridgeConfig:
        return self._config.endpoint_for(self._current).remote

    def _commit(self) -> None:
        """把控件值写回配置对象（内存中）。密码按「空 = 不修改」处理。"""
        # 本机端口属于 Host A —— 角色固定，不会再有「哪个角色是本机的」的歧义
        self._config.endpoint_for(Host.A).local.port = self._local_port.value()

        remote = self.current_remote()
        remote.host = self._host.text().strip()
        remote.ssh_port = self._ssh_port.value()
        remote.username = self._username.text().strip()
        remote.auth = self.current_auth()
        remote.bridge_port = self._bridge_port.value()
        remote.display_name = self._display_name.text().strip()
        remote.key_path = self._key_path.text().strip()
        remote.use_tunnel = self._tunnel.isChecked()
        remote.tunnel_local_port = self._tunnel_port.value()

        typed = self._password.text()
        if typed:
            remote.password = typed

    def commit(self) -> None:
        """供外部在保存配置前显式调用一次。"""
        self._commit()

    # ------------------------------------------------------------ 校验 #

    def _validate(self) -> None:
        errors = self.current_remote().validate()
        if errors:
            self._validation.setText("· " + "\n· ".join(errors))
            self._set_hint_state("error")
            return

        remote = self.current_remote()
        # 两行：一句说「连到哪」，一句说「怎么到桥接服务」。合成一行读起来太赶，
        # 而这两件事是用户核对配置时唯一真正要看的东西。
        if remote.use_tunnel:
            local = (
                f"127.0.0.1:{remote.tunnel_local_port}"
                if remote.tunnel_local_port
                else "本机自动分配端口"
            )
            self._validation.setText(
                f"将连接 {remote.ssh_target}\n"
                f"桥接服务经 SSH 隧道访问：{local} → 远端 127.0.0.1:{remote.bridge_port}"
            )
        else:
            self._validation.setText(
                f"将连接 {remote.ssh_target}\n"
                f"直连桥接服务 {remote.direct_http_base}（远端防火墙需放通该端口）"
            )
        self._set_hint_state("info")

    def _set_hint_state(self, state: str) -> None:
        self._hint_bar.setProperty("state", state)
        repolish(self._hint_bar)

    def validation_errors(self) -> list[str]:
        return self.current_remote().validate()

    # ------------------------------------------------------------ 其它 #

    def set_busy(self, busy: bool) -> None:
        """测试连接期间整体置灰，避免边改边测。

        提示条**不**跟着置灰 —— 它正是「测到哪一步」的反馈来源。
        """
        self._busy = busy
        for widget in (self._local_box, self._creds, self._btn_test, self._btn_done):
            widget.setEnabled(not busy)
        if not busy:
            self._apply_mode()
            self._validate()

    def _pick_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择私钥文件", "", "所有文件 (*)")
        if path:
            self._key_path.setText(path)
            self._on_field_edited()
