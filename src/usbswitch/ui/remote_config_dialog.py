"""桥接服务配置对话框。

**为什么把表单从主窗口搬出来。**
这张表单有 10 个输入框，是一次性配置，却和「切换 U 盘」那种高频操作占同样
的主窗口面积。搬进独立窗口后，主窗口只留一行目标摘要 + 操作按钮。

沿用与主窗口一致的语义：**改一次存一次**（`configEdited` → 主窗口持久化）。
所以底部按钮是「完成」而不是「取消」—— 它不承诺回滚，也就不会骗人。

角色固定后这里只有两块内容
--------------------------

- **本机端口** —— 本机桥接服务（Host A，Windows）监听的端口；
- **Host B 连接参数** —— 远程（Linux）桥接的地址、凭据、端口、隧道。

物理拓扑只有这一种：本机必然占一路 Host，另一路是 Linux 设备。曾经允许
「任意角色选本机 / 远程」，代价是多出两个下拉框，以及「两个角色都设成本机时
只有第一个生效」这类静默失效的配置。

里面有两处刻意的设计，都来自真机踩坑：

1. **「SSH 端口」与「桥接端口」是两个独立输入框。** 这是最容易填反的地方
   （SSH 走 22，桥接 HTTP 走 8738），填反了症状很像「网络不通」，排查代价极高。
2. **密码框回填时只给掩码，不回显明文。** 已存密码时输入框留空 + 占位提示
   「已保存」；用户不改就不动它，一旦输入新值才覆盖。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
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
    AppConfig,
    AuthMethod,
    Host,
    RemoteBridgeConfig,
)
from .theme import THEME
from .widgets import ToggleSwitch

PASSWORD_SAVED_HINT = "已保存（留空则不修改）"
PASSWORD_EMPTY_HINT = "SSH 登录密码"
KEY_HINT = "私钥文件路径（PEM / OpenSSH 格式）"

TUNNEL_LABEL = "经 SSH 隧道访问"
TUNNEL_TOOLTIP = (
    "把远端 127.0.0.1:<桥接端口> 经 SSH 映射到本机一个端口，再从本机访问。\n"
    "适用于远端防火墙不允许直连桥接端口的情况，也让桥接服务不必暴露在网络上。"
)


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
        self.setMinimumWidth(560)

        self._build()
        self.reload()

    # ------------------------------------------------------------------ 构建 #

    def _build(self) -> None:
        self._host = QLineEdit()
        self._host.setPlaceholderText("192.168.1.100 或主机名")

        self._ssh_port = QSpinBox()
        self._ssh_port.setRange(1, 65535)
        self._ssh_port.setValue(22)
        self._ssh_port.setToolTip("SSH 管理通道端口，通常是 22")
        self._ssh_port.setFixedWidth(84)

        self._username = QLineEdit()
        self._username.setPlaceholderText("登录用户名")

        self._auth = QComboBox()
        self._auth.addItem("密码", AuthMethod.PASSWORD)
        self._auth.addItem("私钥", AuthMethod.KEY)

        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.Password)
        self._password.setPlaceholderText(PASSWORD_EMPTY_HINT)

        self._key_path = QLineEdit()
        self._key_path.setPlaceholderText(KEY_HINT)
        self._btn_browse = QPushButton("浏览…")
        self._btn_browse.setFixedWidth(72)
        self._btn_browse.clicked.connect(self._pick_key)

        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.addWidget(self._key_path, 1)
        key_row.addWidget(self._btn_browse)

        self._bridge_port = QSpinBox()
        self._bridge_port.setRange(1, 65535)
        self._bridge_port.setValue(8738)
        self._bridge_port.setToolTip("远端桥接服务监听的 HTTP 端口，通常是 8738")
        self._bridge_port.setFixedWidth(84)

        self._display_name = QLineEdit()
        self._display_name.setPlaceholderText("可选，仅用于显示")

        self._local_port = QSpinBox()
        self._local_port.setRange(1, 65535)
        self._local_port.setValue(8737)
        self._local_port.setFixedWidth(84)

        self._local_port_label = QLabel("本机端口")
        self._local_port.setToolTip("本机桥接服务监听的端口，通常是 8737")

        # 「本机端口」刻意**不放进「连接参数」分组**：那一组会在「远程」模式下
        # 整体 setEnabled(False)，而本机端口恰恰只在「本机」模式下才有意义 ——
        # 放进去的结果是它永远无法编辑（子控件继承父控件的禁用状态）。
        self._local_hint = QLabel("本机桥接服务（Host A）监听的端口")
        self._local_hint.setObjectName("Muted")

        local_row = QHBoxLayout()
        local_row.setSpacing(10)
        local_row.addWidget(self._local_port_label)
        local_row.addWidget(self._local_port)
        local_row.addWidget(self._local_hint, 1)

        self._tunnel = ToggleSwitch(TUNNEL_LABEL)
        self._tunnel.setToolTip(TUNNEL_TOOLTIP)

        self._tunnel_port = QSpinBox()
        self._tunnel_port.setRange(0, 65535)
        self._tunnel_port.setValue(0)
        self._tunnel_port.setFixedWidth(84)
        self._tunnel_port.setSpecialValueText("自动")
        self._tunnel_port.setToolTip(
            "隧道在本机监听的端口。0 = 每次自动挑一个空闲端口（推荐）——\n"
            "写死端口会平白多出一类「本地端口被占用」的失败。"
        )

        self._tunnel_port_label = QLabel("隧道端口")

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.addRow("IP / 主机名", self._host)

        port_row = QHBoxLayout()
        port_row.setContentsMargins(0, 0, 0, 0)
        port_row.addWidget(self._ssh_port)
        port_row.addSpacing(8)
        port_row.addWidget(QLabel("用户名"))
        port_row.addWidget(self._username, 1)
        form.addRow("SSH 端口", port_row)

        form.addRow("认证方式", self._auth)
        form.addRow("密码", self._password)
        form.addRow("私钥", key_row)

        bridge_row = QHBoxLayout()
        bridge_row.setContentsMargins(0, 0, 0, 0)
        bridge_row.addWidget(self._bridge_port)
        bridge_row.addSpacing(8)
        bridge_row.addWidget(QLabel("别名"))
        bridge_row.addWidget(self._display_name, 1)
        form.addRow("桥接端口", bridge_row)

        form.addRow("", self._tunnel)
        form.addRow(self._tunnel_port_label, self._tunnel_port)

        self._creds = QGroupBox("Host B（Linux）连接参数")
        self._creds.setLayout(form)

        self._validation = QLabel("")
        self._validation.setObjectName("Muted")
        self._validation.setWordWrap(True)

        # -- 底部操作 ------------------------------------------------------ #

        self._btn_test = QPushButton("测试连接")
        self._btn_test.clicked.connect(self.testRequested.emit)

        self._btn_done = QPushButton("完成")
        self._btn_done.setObjectName("Primary")
        self._btn_done.setDefault(True)
        self._btn_done.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addWidget(self._btn_test)
        buttons.addStretch(1)
        buttons.addWidget(self._btn_done)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addLayout(local_row)
        layout.addWidget(self._creds)
        layout.addWidget(self._validation)
        layout.addLayout(buttons)

        self._wire()

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
                PASSWORD_SAVED_HINT if remote.password else PASSWORD_EMPTY_HINT
            )

            self._apply_mode()
        finally:
            self._loading = False
        self._validate()

    def _apply_mode(self) -> None:
        """各控件该不该可编辑。角色固定后只剩认证方式与隧道两处联动。"""
        is_password = self.current_auth() is AuthMethod.PASSWORD
        self._password.setEnabled(is_password)
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
            self._validation.setStyleSheet(f"color: {THEME['danger']};")
            return

        remote = self.current_remote()
        if remote.use_tunnel:
            local = (
                f"127.0.0.1:{remote.tunnel_local_port}"
                if remote.tunnel_local_port
                else "本机自动分配端口"
            )
            self._validation.setText(
                f"将连接 {remote.ssh_target}；桥接服务经 SSH 隧道访问"
                f"（{local} → 远端 127.0.0.1:{remote.bridge_port}）"
            )
        else:
            self._validation.setText(
                f"将连接 {remote.ssh_target}；直连桥接服务 {remote.direct_http_base}"
                "（远端防火墙需放通该端口）"
            )
        self._validation.setStyleSheet(f"color: {THEME['muted']};")

    def validation_errors(self) -> list[str]:
        return self.current_remote().validate()

    # ------------------------------------------------------------ 其它 #

    def set_busy(self, busy: bool) -> None:
        """测试连接期间整体置灰，避免边改边测。"""
        self._busy = busy
        for widget in (
            self._creds,
            self._local_port,
            self._local_port_label,
            self._local_hint,
            self._btn_test,
            self._btn_done,
        ):
            widget.setEnabled(not busy)
        if not busy:
            self._apply_mode()
            self._validate()

    def _pick_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择私钥文件", "", "所有文件 (*)")
        if path:
            self._key_path.setText(path)
            self._on_field_edited()
