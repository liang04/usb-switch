"""桥接服务配置对话框的测试。

这些用例原先放在 `test_ui_smoke.py` 里测 `RemotePanel` —— 表单搬进独立窗口后
一并搬过来：**测试应当跟着拥有该行为的控件走**。

对话框沿用主窗口的「改一次存一次」语义（`configEdited` → 主窗口持久化），
所以底部按钮是「完成」而不是「取消」，它不承诺回滚。
"""

from __future__ import annotations

from usbswitch.core.models import AppConfig, AuthMethod, Host


def _remote_config(*, with_password: bool = True) -> AppConfig:
    """构造一个已配好 Host B 远程参数的配置（B 恒为远程角色）。"""
    config = AppConfig()
    remote = config.bridges[Host.B].remote
    remote.host = "192.168.1.100"
    remote.username = "ubu"
    remote.ssh_port = 22
    remote.bridge_port = 8738
    if with_password:
        remote.password = "s3cr3t"
    return config


def _dialog(config: AppConfig):
    from usbswitch.ui.remote_config_dialog import RemoteConfigDialog

    return RemoteConfigDialog(config)


# --------------------------------------------------------------------------- #
# 密码：既不回显，也不误清
# --------------------------------------------------------------------------- #


def test_password_is_never_echoed(qapp, data_dir):
    """回填时只能给掩码提示，绝不能把明文画到屏幕上。"""
    dialog = _dialog(_remote_config())

    assert dialog._password.text() == "", "密码被回显到了输入框"
    assert "不修改" in dialog._password.placeholderText()


def test_password_state_is_a_chip_not_a_placeholder(qapp, data_dir):
    """「已保存」必须是常驻徽标，不能只靠 placeholder。

    踩过的点：状态原先只写在 placeholder 上，用户往框里敲下第一个字符，那句
    「已保存」当场消失 —— 恰恰是他最需要确认「原来存过密码」的时刻。
    """
    dialog = _dialog(_remote_config())

    assert dialog._password_state.text() == "已保存"

    dialog._password.setText("newpass")
    dialog._password.textEdited.emit("newpass")

    assert dialog._password_state.text() == "已保存", "开始输入后状态徽标不该消失"


def test_password_chip_says_unset_without_password(qapp, data_dir):
    """没存过密码时，徽标要明说「未设置」—— 别让用户以为框里的空白是已配置。"""
    dialog = _dialog(_remote_config(with_password=False))

    assert dialog._password_state.text() == "未设置"
    assert dialog._password.placeholderText() == "SSH 登录密码", "空密码时不该提示「留空不修改」"


def test_password_survives_editing_other_fields(qapp, data_dir):
    """用户不动密码框时，保存不能把已存的密码清掉。"""
    config = _remote_config()
    dialog = _dialog(config)

    dialog._host.setText("10.0.0.9")
    dialog.commit()

    assert config.bridges[Host.B].remote.password == "s3cr3t"
    assert config.bridges[Host.B].remote.host == "10.0.0.9"


def test_typing_a_password_overwrites(qapp, data_dir):
    config = _remote_config()
    dialog = _dialog(config)

    dialog._password.setText("newpass")
    dialog.commit()

    assert config.bridges[Host.B].remote.password == "newpass"


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def test_validation_lists_missing_fields(qapp, data_dir):
    """必须指出**具体**缺什么，而不是等 SSH 超时后给一句笼统的失败。"""
    dialog = _dialog(AppConfig())  # Host B 未填任何参数

    errors = dialog.validation_errors()

    assert any("IP" in e for e in errors)
    assert any("用户名" in e for e in errors)
    assert any("密码" in e for e in errors)
    assert "·" in dialog._validation.text()


def test_validation_passes_for_complete_config(qapp, data_dir):
    dialog = _dialog(_remote_config())

    assert dialog.validation_errors() == []
    assert "SSH 隧道" in dialog._validation.text()


def test_hint_bar_flags_errors_by_property_not_stylesheet(qapp, data_dir):
    """提示条的颜色由 QSS 属性选择器决定，不在代码里写死样式。

    写死 `setStyleSheet` 会盖掉控件级 QSS，换主题时还得回来改这一处。
    """
    broken = _dialog(AppConfig())  # Host B 未填任何参数
    assert broken._hint_bar.property("state") == "error"
    assert "·" in broken._validation.text()

    ok = _dialog(_remote_config())
    assert ok._hint_bar.property("state") == "info"


def test_local_port_lives_in_its_own_group(qapp, data_dir):
    """本机端口有自己的分组框，且**不在** `_creds` 里。

    它曾经只是根布局上的一行裸控件、紧贴在 Host B 的卡片之前，读起来像那张卡
    的说明文字。反过来也不能并进 `_creds` —— 分组会整体 `setEnabled(False)`，
    子控件继承禁用状态，这个端口就再也改不了了。
    """
    dialog = _dialog(_remote_config())

    assert dialog._local_port.isEnabled() is True
    assert dialog._creds.isAncestorOf(dialog._local_port) is False
    assert dialog._creds.isAncestorOf(dialog._host) is True


def test_no_button_swallows_enter(qapp, data_dir):
    """填表时回车不该关窗。

    「完成」曾经是 `setDefault(True)`：用户在 IP 框里敲完想确认这一格，一按回车
    整个窗口就没了。判据**枚举**对话框里所有按钮逐个断言，以后新增按钮也绕不开。
    """
    from PySide6.QtWidgets import QPushButton

    dialog = _dialog(_remote_config())
    buttons = dialog.findChildren(QPushButton)

    assert buttons, "一个按钮都没找到，说明选择器写错了"
    assert [b.text() for b in buttons if b.autoDefault()] == []


def test_dialog_has_no_role_or_kind_selectors(qapp, data_dir):
    """角色固定后不再有「角色 / 桥接类型」下拉框 —— 它们只制造歧义。"""
    dialog = _dialog(_remote_config())

    assert not hasattr(dialog, "_role")
    assert not hasattr(dialog, "_kind")


def test_local_port_is_always_editable(qapp, data_dir):
    """本机端口始终可编辑。

    它属于 Host A，与远端参数区不再互斥 —— 曾经被放进「连接参数」分组，
    而那一组在远程模式下整体置灰（子控件继承父控件禁用状态），两头都改不了。
    """
    local = _dialog(AppConfig())
    assert local._local_port.isEnabled() is True

    remote = _dialog(_remote_config())
    assert remote._local_port.isEnabled() is True


def test_local_port_edits_host_a(qapp, data_dir):
    """本机端口写回 Host A，且不动 Host B 的远程参数。"""
    config = _remote_config()
    dialog = _dialog(config)

    dialog._local_port.setValue(9001)
    dialog.commit()

    assert config.bridges[Host.A].local.port == 9001
    assert config.local_bridge_port == 9001
    assert config.bridges[Host.B].remote.host == "192.168.1.100"


# --------------------------------------------------------------------------- #
# 模式切换与可用性
# --------------------------------------------------------------------------- #


def test_remote_fields_are_editable(qapp, data_dir):
    """远端参数区必须真的可编辑。

    历史上这里踩过 `QComboBox.currentData()` 把 `str` 枚举退化成普通字符串的坑
    （`is BridgeKind.REMOTE` 恒为假 → 整块永远置灰）。角色固定后下拉框没了，
    这条断言守的是同一个承诺。
    """
    dialog = _dialog(_remote_config())

    assert dialog._creds.isEnabled() is True
    assert dialog._btn_test.isEnabled() is True


def test_key_auth_swaps_field_availability(qapp, data_dir):
    dialog = _dialog(_remote_config())

    assert dialog._password.isEnabled() is True
    assert dialog._key_path.isEnabled() is False

    dialog._auth.setCurrentIndex(1)  # 私钥

    assert dialog.current_auth() is AuthMethod.KEY
    assert dialog._password.isEnabled() is False
    assert dialog._key_path.isEnabled() is True


def test_busy_disables_the_whole_form(qapp, data_dir):
    """测试连接期间不能边改边测。"""
    dialog = _dialog(_remote_config())

    dialog.set_busy(True)
    assert dialog._creds.isEnabled() is False
    assert dialog._btn_test.isEnabled() is False
    assert dialog._btn_done.isEnabled() is False

    dialog.set_busy(False)
    assert dialog._creds.isEnabled() is True
    assert dialog._btn_test.isEnabled() is True
    assert dialog._btn_done.isEnabled() is True


# --------------------------------------------------------------------------- #
# 写入与信号
# --------------------------------------------------------------------------- #


def test_editing_host_b_notifies_the_main_window(qapp, data_dir):
    """每处编辑都要发 configEdited —— 主窗口据此持久化并重建隧道。

    `setText()` 是程序化赋值，**不会**触发 `textEdited`（那只在用户敲键盘时发），
    所以这里显式发出信号，模拟一次真实输入。
    """
    dialog = _dialog(_remote_config())
    fired: list[int] = []
    dialog.configEdited.connect(lambda: fired.append(1))

    dialog._host.setText("10.0.0.9")
    dialog._host.textEdited.emit("10.0.0.9")

    assert fired, "编辑主机地址没有通知主窗口"


def test_test_button_emits_request(qapp, data_dir):
    """按钮只发信号 —— 真正建 SSH 连接是 worker 的职责。"""
    dialog = _dialog(_remote_config())
    fired: list[int] = []
    dialog.testRequested.connect(lambda: fired.append(1))

    dialog._btn_test.click()

    assert fired == [1]


# --------------------------------------------------------------------------- #
# SSH 隧道
# --------------------------------------------------------------------------- #


def test_tunnel_toggle_roundtrips(qapp, data_dir):
    config = _remote_config()
    dialog = _dialog(config)
    edited: list[int] = []
    dialog.configEdited.connect(lambda: edited.append(1))

    assert dialog._tunnel.isChecked() is True, "隧道默认应当是开着的"

    dialog._tunnel.setChecked(False)

    assert config.bridges[Host.B].remote.use_tunnel is False
    assert edited, "切换隧道开关必须通知主窗口（要重建隧道）"


def test_tunnel_port_only_enabled_with_tunnel(qapp, data_dir):
    dialog = _dialog(_remote_config())

    assert dialog._tunnel_port.isEnabled() is True
    assert dialog._tunnel_port.value() == 0, "默认应当是 0（自动）"

    dialog._tunnel.setChecked(False)

    assert dialog._tunnel_port.isEnabled() is False


def test_tunnel_hint_mentions_tunnel_target(qapp, data_dir):
    """开了隧道时，提示行要说清「本地哪 → 远端哪」，否则用户无从核对。"""
    config = _remote_config()
    dialog = _dialog(config)

    text = dialog._validation.text()
    assert "SSH 隧道" in text
    assert "127.0.0.1:8738" in text, "远端回环地址应当出现"

    config.bridges[Host.B].remote.use_tunnel = False
    dialog.reload()

    assert "直连" in dialog._validation.text()
