"""``core.bridge_remote`` 的测试。

用**假 SSH 客户端**驱动，不碰真实主机 —— 但走的是与生产完全相同的代码路径
（``_open`` → ``_exec`` → ``install``），所以命令构造、幂等性、错误分类
都能被真实地验证。

重点覆盖三件事：

1. **端口不能混用** —— ``ssh_port`` 只管 SSH，``bridge_port`` 只管桥接 HTTP。
   这是设计文档里点名的「最容易填反」的坑。
2. **幂等** —— 重复安装 / 重复卸载 / 卸载一个本来就不存在的服务都必须成功。
3. **错误分类** —— 认证失败、网络不可达、主机密钥不符要给出**可区分**的提示。
"""

from __future__ import annotations

import functools
import socket
from pathlib import Path

import paramiko
import pytest
from paramiko.ssh_exception import NoValidConnectionsError

from usbswitch.core import bridge_remote as br
from usbswitch.core.errors import (
    BridgeUnreachableError,
    RemoteAuthError,
    RemoteCommandError,
    RemoteError,
    RemoteHostKeyError,
    RemoteUnreachableError,
)
from usbswitch.core.models import AuthMethod, BridgeRunState, RemoteBridgeConfig


@pytest.fixture(autouse=True)
def _offline_and_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """两道保险，对所有用例生效。

    1. **禁止真实网络**：``install()`` 结尾会验证远端 HTTP。若不拦截，测试会真的
       去连配置里的 IP（曾经就发生过），既慢又不稳定。
    2. **缩短启动等待**：默认要轮询 12 秒，测试里必须压到几乎没有。
       需要验证「等不到响应」的用例可以自己覆盖 ``probe``。
    """

    def blocked(*args, **kwargs):
        raise BridgeUnreachableError("测试环境禁止真实网络请求")

    monkeypatch.setattr(br.http_json, "get_json", blocked)
    monkeypatch.setattr(br, "START_WAIT", 0.05)
    monkeypatch.setattr(br, "START_POLL", 0.01)


# --------------------------------------------------------------------------- #
# 假 SSH 客户端
# --------------------------------------------------------------------------- #


class _Stream:
    def __init__(self, data: bytes, code: int) -> None:
        self._data = data
        self.channel = self

    def read(self) -> bytes:
        return self._data

    def recv_exit_status(self) -> int:
        return 0

    def close(self) -> None:
        pass


class _ChannelStream(_Stream):
    """stdout：``channel`` 需要带真实的退出码。"""

    def __init__(self, data: bytes, code: int) -> None:
        super().__init__(data, code)
        self._code = code
        self.channel = self

    def recv_exit_status(self) -> int:
        return self._code


class _FakeSFTP:
    def __init__(self) -> None:
        self.puts: list[tuple[str, str]] = []
        self.chmods: list[tuple[str, int]] = []
        self.files: dict[str, str] = {}

    def put(self, localpath: str, remotepath: str) -> None:
        self.puts.append((localpath, remotepath))

    def chmod(self, path: str, mode: int) -> None:
        self.chmods.append((path, mode))

    def open(self, filename: str, mode: str = "r"):
        store = self.files

        class _Handle:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def write(self_inner, text: str) -> None:
                store[filename] = store.get(filename, "") + text

        return _Handle()

    def close(self) -> None:
        pass


class FakeShell:
    """按规则匹配远端命令并返回预设结果。"""

    #: 环境探测那条命令里含有 ``systemctl`` / ``umount`` 等关键字，
    #: 做关键字断言时必须先把它排除，否则会误判。
    ENV_PROBE_MARK = 'printf "HOME=%s'

    def __init__(self, *, env: dict | None = None, rules: list | None = None) -> None:
        self.commands: list[str] = []
        self.sftp = _FakeSFTP()
        self.connect_kwargs: dict = {}
        self.closed = False
        self.system_keys_loaded = 0
        self.saved_host_keys = 0
        self.env = {
            "HOME": "/home/ubu",
            "PY": "/usr/bin/python3",
            "PYVER": "3.10.12",
            "SYSTEMD": "1",
            "UMOUNT": "/usr/bin/umount",
            "SUDO": "1",
        }
        if env:
            self.env.update(env)
        #: [(子串, 退出码, stdout, stderr)]
        self.rules: list[tuple[str, int, str, str]] = rules or []

    # -- 断言辅助 ----------------------------------------------------------- #

    @property
    def real_commands(self) -> list[str]:
        """去掉环境探测那一条后的命令列表。"""
        return [c for c in self.commands if self.ENV_PROBE_MARK not in c]

    @property
    def joined(self) -> str:
        return "\n".join(self.real_commands)

    # -- 客户端接口 --------------------------------------------------------- #

    def load_host_keys(self, filename: str) -> None:
        pass

    def load_system_host_keys(self, filename=None) -> None:
        self.system_keys_loaded += 1

    def set_missing_host_key_policy(self, policy) -> None:
        self.policy = policy

    def save_host_keys(self, filename: str) -> None:
        self.saved_host_keys += 1

    def connect(self, host: str, **kwargs) -> None:
        self.connect_kwargs = {"host": host, **kwargs}

    def close(self) -> None:
        self.closed = True

    def open_sftp(self) -> _FakeSFTP:
        return self.sftp

    def exec_command(self, command: str, timeout=None):
        self.commands.append(command)

        if 'printf "HOME=%s' in command:
            body = "".join(f"{k}={v}\n" for k, v in self.env.items())
            return None, _ChannelStream(body.encode(), 0), _Stream(b"", 0)

        for needle, code, out, err in self.rules:
            if needle in command:
                return (
                    None,
                    _ChannelStream(out.encode(), code),
                    _Stream(err.encode(), code),
                )

        return None, _ChannelStream(b"", 0), _Stream(b"", 0)


def make_bridge(
    shell: FakeShell,
    *,
    tmp_path: Path,
    ssh_port: int = 22,
    bridge_port: int = 8738,
    auth: AuthMethod = AuthMethod.PASSWORD,
    **cfg_kwargs,
) -> br.RemoteBridge:
    config = RemoteBridgeConfig(
        host="192.168.1.50",
        ssh_port=ssh_port,
        username="ubu",
        auth=auth,
        password="secret",
        key_path="/home/me/.ssh/id_ed25519",
        bridge_port=bridge_port,
        **cfg_kwargs,
    )
    script = tmp_path / "linux_bridge.py"
    script.write_text("# fake payload\n", encoding="utf-8")
    return br.RemoteBridge(config, script=script, client_factory=lambda: shell)


@pytest.fixture()
def shell() -> FakeShell:
    return FakeShell()


@functools.lru_cache(maxsize=1)
def _fake_keys() -> tuple:
    """一对假密钥，仅用于构造 ``BadHostKeyException``。

    1024 位是 ``cryptography`` 允许的下限；生成一次缓存起来，
    避免每个用例都花几十毫秒造密钥。
    """
    return paramiko.RSAKey.generate(1024), paramiko.RSAKey.generate(1024)


def _bad_host_key(exc_message: str = "host key mismatch") -> paramiko.BadHostKeyException:
    got, expected = _fake_keys()
    return paramiko.BadHostKeyException(exc_message, got, expected)


# --------------------------------------------------------------------------- #
# 环境探测
# --------------------------------------------------------------------------- #


def test_env_is_parsed_from_single_command(shell: FakeShell, tmp_path: Path, data_dir):
    bridge = make_bridge(shell, tmp_path=tmp_path)

    env = bridge.test_connection()

    assert env.home == "/home/ubu"
    assert env.python == "/usr/bin/python3"
    assert env.python_version == "3.10.12"
    assert env.systemd is True
    assert env.sudo_umount is True
    assert env.mode == "systemd"
    # 只发了一条命令：环境探测不该为了省事拆成六次往返
    assert len(shell.commands) == 1
    assert shell.closed is True


def test_env_without_systemd_reports_nohup_mode(tmp_path: Path, data_dir):
    shell = FakeShell(env={"SYSTEMD": "0", "SUDO": "0"})
    bridge = make_bridge(shell, tmp_path=tmp_path)

    env = bridge.test_connection()

    assert env.systemd is False
    assert env.mode == "nohup"


def test_missing_python_is_reported(tmp_path: Path, data_dir):
    shell = FakeShell(env={"PY": ""})
    bridge = make_bridge(shell, tmp_path=tmp_path)

    with pytest.raises(RemoteError) as excinfo:
        bridge.test_connection()

    assert "python3" in str(excinfo.value)


def test_sudo_hint_when_no_passwordless_umount(tmp_path: Path, data_dir):
    """没有免密 sudo 不能静默放过：必须给出可直接复制的命令。"""
    shell = FakeShell(env={"SUDO": "0"})
    seen: list[tuple[str, str]] = []
    config = RemoteBridgeConfig(
        host="10.0.0.5", username="ubu", password="x", bridge_port=8738
    )
    script = tmp_path / "linux_bridge.py"
    script.write_text("x", encoding="utf-8")
    bridge = br.RemoteBridge(
        config, script=script, on_log=lambda lvl, msg: seen.append((lvl, msg)),
        client_factory=lambda: shell,
    )

    bridge.test_connection()

    warnings = [msg for lvl, msg in seen if lvl == "warning"]
    assert any("sudoers.d/usb-switch" in msg for msg in warnings)


# --------------------------------------------------------------------------- #
# 端口必须独立
# --------------------------------------------------------------------------- #


def test_ssh_port_used_for_connection_and_bridge_port_for_service(
    tmp_path: Path, data_dir
):
    """回归测试：两个端口绝不能混用。

    ``ssh_port=2222`` 是管理通道，``bridge_port=9999`` 是服务监听。
    混用会让服务绑到一个用户以为「只是 SSH 用的」端口上，排查起来极痛苦。
    """
    shell = FakeShell()
    bridge = make_bridge(shell, tmp_path=tmp_path, ssh_port=2222, bridge_port=9999)

    bridge.install()

    assert shell.connect_kwargs["port"] == 2222, "SSH 连接没有用 ssh_port"

    unit = _written_unit(shell)
    assert "9999" in unit, "systemd unit 没有使用 bridge_port"
    assert "2222" not in unit, "systemd unit 里混进了 ssh_port"

    assert bridge.base_url == "http://192.168.1.50:9999"


def _written_unit(shell: FakeShell) -> str:
    units = [text for path, text in shell.sftp.files.items() if path.endswith(".service")]
    assert units, "没有写入任何 systemd unit"
    return units[0]


# --------------------------------------------------------------------------- #
# 安装
# --------------------------------------------------------------------------- #


def test_install_uploads_script_with_exec_bit(shell: FakeShell, tmp_path: Path, data_dir):
    bridge = make_bridge(shell, tmp_path=tmp_path)
    bridge.probe = lambda: {"ok": True, "disks": []}  # type: ignore[method-assign]

    state = bridge.install()

    assert shell.sftp.puts, "没有上传脚本"
    local, remote = shell.sftp.puts[0]
    assert local.endswith("linux_bridge.py")
    assert remote == "/home/ubu/.local/bin/usb_switch_bridge.py"
    assert ("/home/ubu/.local/bin/usb_switch_bridge.py", 0o755) in shell.sftp.chmods
    assert state.http_online is True


def test_install_unit_has_restart_and_linger(shell: FakeShell, tmp_path: Path, data_dir):
    bridge = make_bridge(shell, tmp_path=tmp_path)
    bridge.probe = lambda: {"ok": True}  # type: ignore[method-assign]

    bridge.install()

    unit = _written_unit(shell)
    assert "ExecStart=/usr/bin/python3 /home/ubu/.local/bin/usb_switch_bridge.py 8738" in unit
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit

    joined = shell.joined
    # 没有 linger，用户登出后服务就被 systemd 回收了
    assert "enable-linger" in joined
    assert "daemon-reload" in joined


def test_systemctl_commands_carry_xdg_runtime_dir(shell: FakeShell, tmp_path: Path, data_dir):
    """回归测试：SSH 非登录会话里 ``systemctl --user`` 必须带 XDG_RUNTIME_DIR。

    不带会报 ``Failed to connect to bus: No such file or directory`` ——
    这个错误看起来像「没有 systemd」，实际只是少了个环境变量。
    """
    bridge = make_bridge(shell, tmp_path=tmp_path)
    bridge.probe = lambda: {"ok": True}  # type: ignore[method-assign]

    bridge.install()

    systemctl_cmds = [c for c in shell.real_commands if "systemctl --user" in c]
    # install 至少要有 daemon-reload 与 enable --now 两次
    assert len(systemctl_cmds) >= 2, f"没有调用 systemctl: {shell.real_commands}"
    for command in systemctl_cmds:
        assert "XDG_RUNTIME_DIR" in command, f"缺少 XDG_RUNTIME_DIR: {command}"


def test_install_is_idempotent(shell: FakeShell, tmp_path: Path, data_dir):
    """重复安装必须成功：脚本与 unit 都是覆盖写，服务用 enable --now。"""
    bridge = make_bridge(shell, tmp_path=tmp_path)
    bridge.probe = lambda: {"ok": True}  # type: ignore[method-assign]

    first = bridge.install()
    second = bridge.install()

    assert first is not None and second is not None
    assert len(shell.sftp.puts) == 2
    # 两次的 unit 内容必须完全一致（没有把端口写重、没有追加残留）
    units = [t for p, t in shell.sftp.files.items() if p.endswith(".service")]
    assert len(units) == 1, "unit 被重复写入成了多个文件"


def test_install_falls_back_to_nohup_without_systemd(tmp_path: Path, data_dir):
    shell = FakeShell(env={"SYSTEMD": "0"})
    bridge = make_bridge(shell, tmp_path=tmp_path)
    bridge.probe = lambda: {"ok": True}  # type: ignore[method-assign]

    bridge.install()

    joined = shell.joined
    assert "nohup" in joined
    assert "systemctl" not in joined, "无 systemd 时不该调用 systemctl"
    assert "usb-switch-bridge.pid" in joined


def test_nohup_start_is_idempotent(tmp_path: Path, data_dir):
    """已经是「先探测再启动」：PID 还活着就什么都不做，避免打断线上服务。"""
    shell = FakeShell(
        env={"SYSTEMD": "0"},
        rules=[("echo ALREADY", 0, "ALREADY", "")],
    )
    bridge = make_bridge(shell, tmp_path=tmp_path)
    seen: list[tuple[str, str]] = []
    bridge._on_log = lambda lvl, msg: seen.append((lvl, msg))

    bridge.start()

    joined = shell.joined
    assert "kill -0" in joined, "启动前必须先检查 PID 是否活着"
    # 命令里带了 if/else 分支，命中 ALREADY 时走的就是「不重复拉起」那一支
    assert any("已在运行" in msg for _, msg in seen)


def test_install_reports_missing_script(tmp_path: Path, data_dir):
    shell = FakeShell()
    bridge = br.RemoteBridge(
        RemoteBridgeConfig(host="1.2.3.4", username="u", password="p"),
        script=tmp_path / "nope.py",
        client_factory=lambda: shell,
    )

    with pytest.raises(RemoteError) as excinfo:
        bridge.install()

    assert "找不到远端桥接脚本" in str(excinfo.value)


def test_install_warns_when_http_never_comes_up(shell: FakeShell, tmp_path: Path, data_dir):
    """装好了但端口不通是常见情况，必须如实告警而不是谎报成功。"""
    bridge = make_bridge(shell, tmp_path=tmp_path)
    bridge.probe = lambda: None  # type: ignore[method-assign]
    seen: list[tuple[str, str]] = []
    bridge._on_log = lambda lvl, msg: seen.append((lvl, msg))

    state = bridge.install()

    assert state.http_online is False
    assert any(lvl == "warning" and "无响应" in msg for lvl, msg in seen)


# --------------------------------------------------------------------------- #
# 卸载
# --------------------------------------------------------------------------- #


def test_uninstall_is_idempotent_when_nothing_installed(tmp_path: Path, data_dir):
    """卸载一个本来就没装的服务不该报错 —— 全部走 ``|| true`` 与存在性判断。"""
    shell = FakeShell(
        rules=[
            ("-e \"$HOME/.config/systemd/user", 0, "ABSENT", ""),
            ("-e \"$HOME/.local/bin", 0, "ABSENT", ""),
        ]
    )
    bridge = make_bridge(shell, tmp_path=tmp_path)
    bridge.probe = lambda: None  # type: ignore[method-assign]

    state = bridge.uninstall()

    assert state.http_online is False
    joined = shell.joined
    assert "disable --now" in joined
    assert "|| true" in joined, "卸载必须对「本来就不存在」的情况静默成功"


def test_uninstall_removes_unit_and_script_but_keeps_log(shell: FakeShell, tmp_path: Path, data_dir):
    shell = FakeShell(
        rules=[
            ("-e \"$HOME/.config/systemd/user", 0, "REMOVED", ""),
            ("-e \"$HOME/.local/bin", 0, "REMOVED", ""),
        ]
    )
    bridge = make_bridge(shell, tmp_path=tmp_path)
    bridge.probe = lambda: None  # type: ignore[method-assign]
    seen: list[tuple[str, str]] = []
    bridge._on_log = lambda lvl, msg: seen.append((lvl, msg))

    bridge.uninstall()

    removes = [c for c in shell.real_commands if c.startswith("if [ -e")]
    assert len(removes) == 2, "unit 与脚本都应走存在性判断后删除"
    for command in removes:
        assert "rm -f" in command
    # 日志要保留，便于卸载后仍能排查历史问题
    assert not any("usb-switch-bridge.log" in c and "rm " in c for c in shell.real_commands)
    assert any("日志文件已保留" in msg for _, msg in seen)


def test_uninstall_clears_nohup_pid_even_with_systemd(shell: FakeShell, tmp_path: Path, data_dir):
    """两种形态可能先后被用过，卸载要把 PID 残留也清掉。"""
    bridge = make_bridge(shell, tmp_path=tmp_path)
    bridge.probe = lambda: None  # type: ignore[method-assign]

    bridge.uninstall()

    joined = shell.joined
    assert "usb-switch-bridge.pid" in joined
    assert "kill" in joined


# --------------------------------------------------------------------------- #
# 两层状态
# --------------------------------------------------------------------------- #


def test_inspect_separates_ssh_from_http(shell: FakeShell, tmp_path: Path, data_dir):
    """SSH 可达但桥接没起来 —— 必须呈现为两个不同的指示灯，而不是笼统的「离线」。"""
    shell.rules.append(("is-active", 0, "inactive", ""))
    bridge = make_bridge(shell, tmp_path=tmp_path)
    bridge.probe = lambda: None  # type: ignore[method-assign]

    state = bridge.inspect()

    assert state.ssh_reachable is True
    assert state.http_online is False
    assert state.run_state is BridgeRunState.ERROR
    assert "inactive" in state.describe() or "无响应" in state.describe()


def test_inspect_http_failure_does_not_hide_ssh_failure(shell: FakeShell, tmp_path: Path, data_dir):
    def boom(*args, **kwargs):
        raise RemoteAuthError("SSH 认证失败")

    shell.connect = boom  # type: ignore[method-assign]
    bridge = make_bridge(shell, tmp_path=tmp_path)
    bridge.probe = lambda: {"ok": True, "disks": ["/dev/sda"]}  # type: ignore[method-assign]

    state = bridge.inspect()

    # 认证失败但服务其实在跑：两层信息都要给出，不能因为 SSH 挂了就说「离线」
    assert state.ssh_reachable is False
    assert state.http_online is True
    assert state.run_state is BridgeRunState.RUNNING


def test_probe_uses_bridge_port_not_ssh_port(
    shell: FakeShell, tmp_path: Path, data_dir, monkeypatch: pytest.MonkeyPatch
):
    # 走直连才谈得上「地址怎么拼」；勾了隧道时 probe 会等隧道就绪，
    # 那条路径由 test_probe_waits_for_the_tunnel_when_configured 覆盖。
    bridge = make_bridge(
        shell, tmp_path=tmp_path, ssh_port=22, bridge_port=9999, use_tunnel=False
    )
    calls: list[str] = []

    def fake_get(url, *, timeout=2.0):
        calls.append(url)
        return {"ok": True}

    monkeypatch.setattr(br.http_json, "get_json", fake_get)
    bridge.probe()

    assert calls == ["http://192.168.1.50:9999/status"]


def test_probe_waits_for_the_tunnel_when_configured(
    shell: FakeShell, tmp_path: Path, data_dir, monkeypatch: pytest.MonkeyPatch
):
    """勾了隧道就不能用直连探测冒充「在线」。

    否则面板会亮着「桥接服务 在线」，用户一按切换却报「隧道未建立」——
    同一个分区里自相矛盾，而且两次结论来自两条不同的路。
    """
    bridge = make_bridge(shell, tmp_path=tmp_path, ssh_port=22, bridge_port=9999,
                         use_tunnel=True)
    calls: list[str] = []

    def fake_get(url, *, timeout=2.0):
        calls.append(url)
        return {"ok": True}

    monkeypatch.setattr(br.http_json, "get_json", fake_get)

    assert bridge.probe() is None, "隧道没通却拿到了探测结果"
    assert calls == [], "隧道没通却发了直连请求"


def test_probe_follows_the_tunnel_once_ready(
    shell: FakeShell, tmp_path: Path, data_dir, monkeypatch: pytest.MonkeyPatch
):
    bridge = make_bridge(shell, tmp_path=tmp_path, bridge_port=9999, use_tunnel=True)
    bridge.config.runtime_http_base = "http://127.0.0.1:54321"
    calls: list[str] = []

    monkeypatch.setattr(
        br.http_json, "get_json",
        lambda url, timeout: (calls.append(url), {"ok": True})[1],
    )

    assert bridge.probe() == {"ok": True}
    assert calls == ["http://127.0.0.1:54321/status"]


# --------------------------------------------------------------------------- #
# 错误分类
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (paramiko.AuthenticationException("bad password"), RemoteAuthError),
        (_bad_host_key(), RemoteHostKeyError),
        (socket.gaierror("name resolution"), RemoteUnreachableError),
        (socket.timeout("timed out"), RemoteUnreachableError),
        (ConnectionRefusedError("refused"), RemoteUnreachableError),
        (paramiko.SSHException("protocol"), RemoteError),
    ],
)
def test_exceptions_are_translated(tmp_path: Path, data_dir, exc, expected):
    bridge = br.RemoteBridge(
        RemoteBridgeConfig(host="10.0.0.1", username="u", password="p")
    )

    translated = bridge._translate(exc)

    assert isinstance(translated, expected)
    assert translated.message


def test_no_valid_connections_error_maps_to_unreachable(tmp_path: Path, data_dir):
    """回归测试：paramiko 5.x 里 NoValidConnectionsError 继承的是 OSError。

    所以判断顺序必须把它排在笼统的 OSError 之前，否则用户看到的会是
    「网络错误」而不是「端口可能被防火墙拦截 / SSH 服务未运行」。
    """
    bridge = br.RemoteBridge(
        RemoteBridgeConfig(host="10.0.0.1", username="u", password="p")
    )
    exc = NoValidConnectionsError({("10.0.0.1", 22): ConnectionRefusedError()})

    translated = bridge._translate(exc)

    assert isinstance(translated, RemoteUnreachableError)
    assert "SSH 端口" in translated.hint


def test_unreachable_hint_mentions_ssh_port_confusion(tmp_path: Path, data_dir):
    """「SSH 端口填成桥接端口」是最高频的填错，提示必须直说。"""
    bridge = br.RemoteBridge(
        RemoteBridgeConfig(host="10.0.0.1", username="u", password="p")
    )

    translated = bridge._translate(ConnectionRefusedError("nope"))

    assert "桥接端口" in translated.hint


def test_command_failure_raises_with_stderr(tmp_path: Path, data_dir):
    shell = FakeShell(rules=[("mkdir -p", 1, "", "Permission denied")])
    bridge = make_bridge(shell, tmp_path=tmp_path)

    with pytest.raises(RemoteCommandError) as excinfo:
        bridge.install()

    assert "Permission denied" in excinfo.value.hint


def test_bad_host_key_hint_points_at_known_hosts(tmp_path: Path, data_dir):
    bridge = br.RemoteBridge(
        RemoteBridgeConfig(host="10.0.0.1", username="u", password="p")
    )

    translated = bridge._translate(_bad_host_key())

    assert "known_hosts" in translated.hint


# --------------------------------------------------------------------------- #
# 认证方式
# --------------------------------------------------------------------------- #


def test_key_auth_passes_keyfile_not_password(tmp_path: Path, data_dir):
    shell = FakeShell()
    bridge = make_bridge(shell, tmp_path=tmp_path, auth=AuthMethod.KEY)

    bridge.test_connection()

    assert shell.connect_kwargs["key_filename"] == "/home/me/.ssh/id_ed25519"
    assert shell.connect_kwargs["password"] is None, "私钥认证不该把口令当密码传"
    assert shell.connect_kwargs["look_for_keys"] is False
    assert shell.connect_kwargs["allow_agent"] is False


def test_password_auth_uses_password(tmp_path: Path, data_dir):
    shell = FakeShell()
    bridge = make_bridge(shell, tmp_path=tmp_path)

    bridge.test_connection()

    assert shell.connect_kwargs["password"] == "secret"
    assert shell.connect_kwargs["look_for_keys"] is False


# --------------------------------------------------------------------------- #
# 信任库
# --------------------------------------------------------------------------- #


def test_system_known_hosts_is_never_loaded(shell: FakeShell, tmp_path: Path, data_dir):
    """回归测试：不许把用户自己的 ``~/.ssh/known_hosts`` 拉进来。

    真机上踩过：用户机器的 ``~/.ssh/known_hosts`` 里早就有 ``192.168.0.1``
    （路由器常规地址），密钥与目标设备不同。两份记录合并进同一个
    ``_host_keys`` 后抛 ``BadHostKeyException``，而错误提示指向的是**本程序的
    文件** —— 去那里翻什么也找不到，完全无从下手。

    本程序只用自己的单一信任库，报错给出的路径就是唯一要处理的地方。
    """
    bridge = make_bridge(shell, tmp_path=tmp_path)

    bridge.test_connection()

    assert shell.system_keys_loaded == 0, "不该读取用户的 ssh 信任库"


def test_host_keys_saved_only_after_successful_connect(tmp_path: Path, data_dir):
    """回归测试：连接失败时**不能**保存 known_hosts。

    ``save_host_keys()`` 写出的是客户端当前 ``_host_keys`` 的内容。连接失败时
    里面可能一条服务器密钥都没有，却会把从别处加载来的记录原样写进我们的
    信任库 —— 等于用错误的密钥污染它，之后每次连接都撞上这个假冲突。
    """
    shell = FakeShell()

    def refuse(host, **kwargs):
        raise paramiko.AuthenticationException("bad password")

    shell.connect = refuse  # type: ignore[method-assign]
    bridge = make_bridge(shell, tmp_path=tmp_path)

    with pytest.raises(RemoteAuthError):
        bridge.test_connection()

    assert shell.saved_host_keys == 0, "连接失败却保存了信任库"
    assert shell.closed is True, "失败路径没有关闭连接"


def test_host_keys_saved_after_successful_connect(shell: FakeShell, tmp_path: Path, data_dir):
    bridge = make_bridge(shell, tmp_path=tmp_path)

    bridge.test_connection()

    assert shell.saved_host_keys == 1


# --------------------------------------------------------------------------- #
# 命令构造安全
# --------------------------------------------------------------------------- #


def test_remote_paths_with_spaces_are_quoted(tmp_path: Path, data_dir):
    """远端路径进 shell 前必须被引用。

    正常路径不含元字符时 ``shlex.quote`` 会原样返回（这是对的，不该硬加引号）。
    所以这里用一个**含空格的 HOME** 来验证引用确实生效 —— 如果哪天有人把
    ``shlex.quote`` 去掉换成裸拼接，这条就会红。
    """
    shell = FakeShell(env={"HOME": "/home/ub u"})
    bridge = make_bridge(shell, tmp_path=tmp_path)
    bridge.probe = lambda: {"ok": True}  # type: ignore[method-assign]

    bridge.install()

    mkdirs = [c for c in shell.real_commands if c.startswith("mkdir -p")]
    assert mkdirs
    for command in mkdirs:
        assert "'/home/ub u/" in command, f"含空格的路径没有被引用: {command}"


def test_username_with_shell_metacharacters_is_rejected(tmp_path: Path, data_dir):
    """用户名会进 sudoers 规则，必须挡在配置校验这一层。"""
    config = RemoteBridgeConfig(host="10.0.0.1", username="u; rm -rf /", password="p")
    errors = config.validate()

    assert any("用户名格式" in e for e in errors)


def test_host_with_shell_metacharacters_is_rejected(tmp_path: Path, data_dir):
    config = RemoteBridgeConfig(host="10.0.0.1; reboot", username="u", password="p")

    assert any("非法字符" in e for e in config.validate())


def test_sudo_hint_contains_exact_command(tmp_path: Path, data_dir):
    bridge = br.RemoteBridge(
        RemoteBridgeConfig(host="10.0.0.1", username="ubu", password="p")
    )

    hint = bridge.sudo_hint()

    assert "ubu ALL=(ALL) NOPASSWD: /bin/umount" in hint
    assert "visudo -c" in hint, "改 sudoers 必须先校验，否则可能连 sudo 都用不了"
