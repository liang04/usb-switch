"""远程（Linux）桥接服务的管理：安装 / 启动 / 停止 / 卸载。

通道是 ``paramiko``（纯 Python，密码与私钥双认证，打包无外部依赖）。
部署目标是 ``scripts/linux_bridge.py`` —— 那是个**自包含单文件**，
不依赖本项目任何模块，所以可以原样上传。

四件事必须说清楚
----------------

**1. 两个端口是独立的。** ``ssh_port`` 是管理通道，``bridge_port`` 是远端桥接
服务的 HTTP 监听端口。两者混用是最容易踩的坑，所以 :class:`RemoteBridge`
从来不从 ``ssh_port`` 推导 ``bridge_port``。

**2. ``systemctl --user`` 在 SSH 非登录会话里必须带 ``XDG_RUNTIME_DIR``。**
否则会得到 ``Failed to connect to bus: No such file or directory`` —— 这个报错
很有迷惑性，看起来像「没有 systemd」，实际只是环境变量缺失。所有 ``systemctl
--user`` 调用统一走 :data:`_SYSTEMCTL` 前缀。

**3. 全部动作幂等。** 安装两次、卸载两次都必须成功返回。远端是别人的机器，
「重复点一下按钮就报错」是不可接受的。

**4. 不代替用户改 ``/etc/sudoers``。** 免密 umount 是可选能力，写 sudoers 属于
特权且高风险的改动（写坏会连 sudo 都用不了），而 SSH 非交互会话无法安全地
输入 sudo 密码。所以这里只**检测** ``sudo -n umount`` 是否可用，不可用时通过
:meth:`RemoteBridge.sudo_hint` 给出用户可自行执行的确切命令。
"""

from __future__ import annotations

import logging
import shlex
import socket
import time
from dataclasses import dataclass
from pathlib import Path

import paramiko
from paramiko.ssh_exception import NoValidConnectionsError

from . import http_json
from .errors import (
    RemoteAuthError,
    RemoteCommandError,
    RemoteError,
    RemoteHostKeyError,
    RemoteUnreachableError,
    UsbSwitchError,
)
from .models import AuthMethod, BridgeRunState, RemoteBridgeConfig
from .paths import data_dir, linux_bridge_script

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

CONNECT_TIMEOUT = 8.0
COMMAND_TIMEOUT = 30.0
HTTP_PROBE_TIMEOUT = 3.0

#: 启动后等待 HTTP 就绪的总时长与轮询间隔
START_WAIT = 12.0
START_POLL = 0.75

UNIT_NAME = "usb-switch-bridge.service"
REMOTE_BIN_DIR = ".local/bin"
REMOTE_STATE_DIR = ".local/state"
REMOTE_SCRIPT_NAME = "usb_switch_bridge.py"
REMOTE_PID_NAME = "usb-switch-bridge.pid"
REMOTE_LOG_NAME = "usb-switch-bridge.log"

#: ``systemctl --user`` 必须带 XDG_RUNTIME_DIR，否则连不上 bus。
_SYSTEMCTL = 'XDG_RUNTIME_DIR="/run/user/$(id -u)" systemctl --user'

#: 本进程独立保存的 known_hosts，不去动用户自己的 ~/.ssh/known_hosts
_KNOWN_HOSTS_NAME = "known_hosts"

# 远端路径（相对 $HOME），供命令拼接与 SFTP 使用
_UNIT_REL = f".config/systemd/user/{UNIT_NAME}"
_SCRIPT_REL = f"{REMOTE_BIN_DIR}/{REMOTE_SCRIPT_NAME}"
_PID_REL = f"{REMOTE_STATE_DIR}/{REMOTE_PID_NAME}"
_LOG_REL = f"{REMOTE_STATE_DIR}/{REMOTE_LOG_NAME}"


# --------------------------------------------------------------------------- #
# 数据类
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RemoteEnv:
    """远端环境探测结果。"""

    home: str = ""
    python: str = ""
    python_version: str = ""
    systemd: bool = False
    umount: str = ""
    sudo_umount: bool = False

    @property
    def ready(self) -> bool:
        """是否具备安装条件。"""
        return bool(self.python)

    @property
    def mode(self) -> str:
        return "systemd" if self.systemd else "nohup"

    def describe(self) -> str:
        if not self.python:
            return "未找到 python3，无法安装"
        parts = [
            f"python3 {self.python_version or '?'} ({self.python})",
            "systemd" if self.systemd else "无 systemd（将用 nohup）",
        ]
        if self.umount:
            parts.append("免密 umount" if self.sudo_umount else "umount 需密码")
        return "，".join(parts)


@dataclass(frozen=True)
class RemoteState:
    """远程桥接的**两层**状态。

    SSH 可达与 HTTP 在线必须分开呈现 —— SSH 通不代表桥接服务在跑
    （可能没装、没启动、端口被占，或防火墙挡了 8738）。

    ``ssh_checked`` 是必需的第三个状态：只做廉价 HTTP 探活时根本没碰 SSH，
    此时既不能说「可达」也不能说「不可达」，只能如实说「未检测」。
    少了这个字段，周期轮询会一直把 SSH 画成红色，用户会以为连不上。
    """

    ssh_reachable: bool = False
    ssh_detail: str = "未检测"
    http_online: bool = False
    http_detail: str = "未检测"
    service: str = "unknown"
    drives: tuple[str, ...] = ()
    ssh_checked: bool = False

    @property
    def run_state(self) -> BridgeRunState:
        if self.http_online:
            return BridgeRunState.RUNNING
        if self.ssh_checked and not self.ssh_reachable:
            return BridgeRunState.UNKNOWN
        if self.ssh_reachable:
            # SSH 能通但 HTTP 不通：服务没跑起来或端口不通，属于异常而非「已停止」
            return BridgeRunState.ERROR
        return BridgeRunState.UNKNOWN

    def describe(self) -> str:
        return f"SSH：{self.ssh_detail}；桥接服务：{self.http_detail}"


@dataclass(frozen=True)
class CommandResult:
    code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def output(self) -> str:
        return self.stdout or self.stderr


# --------------------------------------------------------------------------- #
# 主机密钥策略
# --------------------------------------------------------------------------- #


class _AcceptNewHostKey(paramiko.AutoAddPolicy):
    """首次连接记录指纹并告警；之后按 known_hosts 校验。

    等价于 ssh 的 ``StrictHostKeyChecking=accept-new``：既不静默信任，
    也不因为第一次连接就失败。密钥变化时 paramiko 会抛
    :class:`paramiko.BadHostKeyException`，由 :meth:`RemoteBridge._translate`
    翻成 :class:`RemoteHostKeyError`。
    """

    def __init__(self, notify) -> None:
        super().__init__()
        self._notify = notify

    def missing_host_key(self, client, hostname, key) -> None:  # noqa: ANN001
        fingerprint = key.get_fingerprint().hex()
        self._notify(hostname, key.get_name(), fingerprint)
        super().missing_host_key(client, hostname, key)


# --------------------------------------------------------------------------- #
# SSH 连接（管理通道与隧道共用同一套安全策略）
# --------------------------------------------------------------------------- #
#
# 这几个函数刻意做成模块级的：SSH **隧道**（core/ssh_tunnel.py）也需要一条
# 同样安全策略的连接。如果只在 RemoteBridge 里实现，隧道那边就得复制一遍
# 主机密钥策略与错误翻译 —— 两份实现迟早会漂移，而漂移的地方恰好是安全策略。


def known_hosts_path() -> Path:
    """本程序自己的 known_hosts（唯一信任库）。"""
    return data_dir() / _KNOWN_HOSTS_NAME


def translate_ssh_error(config: RemoteBridgeConfig, exc: BaseException) -> UsbSwitchError:
    """把 paramiko / socket 的异常翻成带可操作提示的领域异常。"""
    target = f"{config.host}:{config.ssh_port}"

    if isinstance(exc, paramiko.BadHostKeyException):
        return RemoteHostKeyError(
            f"主机密钥与记录不符：{target}",
            hint=(
                "可能是对方重装系统或更换了密钥。核实无误后，删除 "
                f"{known_hosts_path()} 中对应条目再重试。"
            ),
        )
    if isinstance(exc, paramiko.AuthenticationException):
        return RemoteAuthError(
            f"SSH 认证失败：{config.username}@{target}",
            hint="请检查用户名、密码（或私钥及其口令）是否正确",
        )
    # 注意 paramiko 5.x 里 NoValidConnectionsError 继承的是 OSError 而**不是**
    # SSHException，所以必须排在 OSError 之前显式判断，否则会被下面那条
    # 笼统的「网络错误」吞掉，用户就看不到「端口可能被防火墙拦截」这个提示。
    if isinstance(exc, NoValidConnectionsError):
        return RemoteUnreachableError(
            f"无法连接 {target}",
            hint=(
                "端口可能被防火墙拦截，或 SSH 服务未运行"
                "（请确认「SSH 端口」填的是 22，而不是桥接端口）"
            ),
        )
    if isinstance(exc, socket.gaierror):
        return RemoteUnreachableError(
            f"无法解析主机名：{config.host}",
            hint="请检查 IP / 主机名是否输入正确",
        )
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return RemoteUnreachableError(
            f"连接 {target} 超时",
            hint="请检查对方是否开机、网络是否可达、SSH 端口是否正确",
        )
    if isinstance(exc, ConnectionRefusedError):
        return RemoteUnreachableError(
            f"{target} 拒绝连接",
            hint="SSH 服务可能未启动，或端口填错了（管理通道是 SSH 端口，不是桥接端口）",
        )
    if isinstance(exc, paramiko.SSHException):
        return RemoteError(f"SSH 协议错误：{exc}")
    if isinstance(exc, OSError):
        return RemoteUnreachableError(f"网络错误：{exc}")
    return RemoteError(f"未预期的错误：{type(exc).__name__}: {exc}")


def open_ssh_client(
    config: RemoteBridgeConfig,
    *,
    client_factory=None,
    on_new_host_key=None,
) -> paramiko.SSHClient:
    """建立 SSH 连接。失败一律翻成领域异常。

    信任库只用**本程序自己的** ``known_hosts``
    --------------------------------------------------

    刻意**不调用** ``load_system_host_keys()``。踩过一次：

    本机用户自己的 ``~/.ssh/known_hosts`` 里早就有 ``192.168.0.1``（那是台路由器
    的常规地址），密钥与目标设备不同。两份记录被合并进同一个 ``_host_keys``
    字典后，冲突以 ``BadHostKeyException`` 的形式抛出，而错误提示指向的是
    **本程序的文件** —— 用户去那里翻，什么都找不到，只会一头雾水。

    用自己的单一信任库就好得多：

    - 语义与 ssh 的 ``StrictHostKeyChecking=accept-new`` 一致：首次记录并告警，
      之后校验；密钥变化时报错。
    - 报错里给出的路径就是**唯一**需要处理的地方，不会牵扯用户的 ssh 配置。
    - 不去读用户 ``~/.ssh`` 下的私人文件。

    Args:
        on_new_host_key: ``(hostname, key_type, fingerprint)``，首次记录密钥时回调。
    """
    client = (client_factory or paramiko.SSHClient)()
    notify = on_new_host_key or (lambda *args: None)

    known = known_hosts_path()
    if known.exists():
        client.load_host_keys(str(known))

    client.set_missing_host_key_policy(_AcceptNewHostKey(notify))

    kwargs: dict = {
        "port": config.ssh_port,
        "username": config.username,
        "timeout": CONNECT_TIMEOUT,
        "banner_timeout": CONNECT_TIMEOUT,
        "auth_timeout": CONNECT_TIMEOUT,
        # 明确关掉 SSH agent：否则「密码认证失败」会被 agent 里的密钥
        # 掩盖成另一个更难懂的错误
        "allow_agent": False,
        "look_for_keys": False,
    }
    if config.auth is AuthMethod.KEY:
        kwargs["key_filename"] = config.key_path
        kwargs["password"] = config.passphrase or None
    else:
        kwargs["password"] = config.password

    try:
        client.connect(config.host, **kwargs)
    except Exception as exc:
        client.close()
        raise translate_ssh_error(config, exc) from exc

    # 仅在**连通之后**才落盘。
    #
    # 曾经在失败路径上也保存 —— 那是个 bug：``save_host_keys()`` 写出的是
    # 客户端当前 ``_host_keys`` 的内容。连接失败时里面可能一条服务器密钥都
    # 没有（策略还没触发），却把从别处加载来的记录原样写进了我们的信任库，
    # 等于**用错误的密钥污染了它**，之后每次连接都会撞上这个假冲突。
    try:
        client.save_host_keys(str(known))
    except OSError as exc:
        log.debug("保存 known_hosts 失败: %s", exc)
    return client


# --------------------------------------------------------------------------- #
# 主体
# --------------------------------------------------------------------------- #


class RemoteBridge:
    """某个 Host 角色上的远程（Linux）桥接服务。

    所有方法都是**同步阻塞**的（SSH 天然如此）。调用方负责挪到后台线程 ——
    见 :class:`usbswitch.workers.remote_worker.RemoteWorker`。
    """

    def __init__(
        self,
        config: RemoteBridgeConfig,
        *,
        script: Path | None = None,
        on_log=None,
        client_factory=None,
    ) -> None:
        self._config = config
        self._script = script or linux_bridge_script()
        #: 形如 ``on_log(level, message)``，与 Qt 的 logMessage 信号同签名
        self._on_log = on_log
        #: 测试接缝：注入假的 ``SSHClient``，从而无需真实主机即可验证安装流程
        self._client_factory = client_factory or paramiko.SSHClient

    # -- 只读属性 ----------------------------------------------------------- #

    @property
    def config(self) -> RemoteBridgeConfig:
        return self._config

    @property
    def base_url(self) -> str:
        return self._config.http_base

    @property
    def ssh_target(self) -> str:
        return self._config.ssh_target

    @property
    def script_path(self) -> Path:
        return self._script

    # -- 工具 --------------------------------------------------------------- #

    def _log(self, level: str, message: str) -> None:
        log.log({"debug": 10, "info": 20, "warning": 30, "error": 40}.get(level, 20), message)
        if self._on_log is not None:
            self._on_log(level, message)

    def _known_hosts_path(self) -> Path:
        return known_hosts_path()

    def _open(self) -> paramiko.SSHClient:
        return open_ssh_client(
            self._config,
            client_factory=self._client_factory,
            on_new_host_key=lambda host, kind, fp: self._log(
                "warning",
                f"首次连接 {host}，已记录主机密钥（{kind} {fp[:16]}…）。"
                "若与你预期不符请先核实。",
            ),
        )

    def _translate(self, exc: BaseException) -> UsbSwitchError:
        return translate_ssh_error(self._config, exc)

    def sudo_hint(self) -> str:
        """返回用户可自行执行的免密 umount 配置命令。

        **不代为执行**：写 ``/etc/sudoers.d`` 是特权且高风险的改动（写坏会连
        ``sudo`` 都用不了），而 SSH 非交互会话没法安全地输入 sudo 密码。
        宁可在日志里给出一条能直接复制粘贴的命令。
        """
        rule = f"{self._config.username} ALL=(ALL) NOPASSWD: /bin/umount"
        return (
            f"echo '{rule}' | sudo tee /etc/sudoers.d/usb-switch && "
            f"sudo chmod 0440 /etc/sudoers.d/usb-switch && sudo visudo -c"
        )

    def _exec(
        self,
        client: paramiko.SSHClient,
        command: str,
        *,
        timeout: float = COMMAND_TIMEOUT,
        check: bool = False,
    ) -> CommandResult:
        """执行远端命令。``check=True`` 时非零退出码抛 RemoteCommandError。"""
        log.debug("远程命令: %s", command)
        try:
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            code = stdout.channel.recv_exit_status()
        except (socket.timeout, TimeoutError) as exc:
            raise RemoteCommandError(
                f"远端命令执行超时（{timeout:g} 秒）",
                hint=f"命令：{command}",
            ) from exc
        except paramiko.SSHException as exc:
            raise self._translate(exc) from exc

        result = CommandResult(code=code, stdout=out.strip(), stderr=err.strip())
        if check and code != 0:
            raise RemoteCommandError(
                f"远端命令失败（退出码 {code}）",
                hint=(result.stderr or result.stdout or command)[:300],
            )
        return result

    # -- 探活（两层） ------------------------------------------------------- #

    def probe(self) -> dict | None:
        """HTTP 层探活：请求远端 ``/status``。可达返回响应 dict，否则 None。"""
        try:
            return http_json.get_json(f"{self.base_url}/status", timeout=HTTP_PROBE_TIMEOUT)
        except UsbSwitchError as exc:
            log.debug("远程桥接探活失败: %s", exc)
            return None

    def inspect(self) -> RemoteState:
        """同时探测 SSH 与 HTTP 两层，返回完整状态。

        SSH 层失败不会阻断 HTTP 层 —— 服务可能已经装好在跑，只是当前
        SSH 连不上（例如改了密码）。两层信息都要给用户。
        """
        ssh_ok = False
        ssh_detail = ""
        service = "unknown"

        try:
            with self._connect() as client:
                ssh_ok = True
                ssh_detail = f"可达（{self.ssh_target}）"
                env = self._detect_env(client)
                if env.systemd:
                    res = self._exec(
                        client,
                        f'{_SYSTEMCTL} is-active {UNIT_NAME} || true',
                        timeout=10.0,
                    )
                    service = res.stdout.strip() or "unknown"
                else:
                    service = "nohup" if self._nohup_alive(client) else "stopped"
        except UsbSwitchError as exc:
            ssh_detail = exc.message

        payload = self.probe()
        if payload and payload.get("ok"):
            drives = payload.get("disks") or []
            http_detail = "在线"
            if drives:
                http_detail += f"（远端磁盘 {'、'.join(drives)}）"
            else:
                http_detail += "（远端无 U 盘）"
        else:
            http_detail = "无响应"

        return RemoteState(
            ssh_reachable=ssh_ok,
            ssh_detail=ssh_detail,
            http_online=bool(payload and payload.get("ok")),
            http_detail=http_detail,
            service=service,
            drives=tuple(payload.get("disks") or []) if payload else (),
            ssh_checked=True,
        )

    def _connect(self) -> "_Session":
        return _Session(self)

    # -- 环境探测 ----------------------------------------------------------- #

    def _detect_env(self, client: paramiko.SSHClient) -> RemoteEnv:
        """一次性取回所需的环境信息（用一条命令，省往返）。

        刻意避免嵌套引号：``python3 -c '...'`` 这类写法要穿过 Python 字符串、
        shell 和 sudo 三层，很容易在某个发行版的 shell 上炸掉。改用
        ``python3 --version`` 再切一刀，稳得多。
        """
        script = (
            'printf "HOME=%s\\n" "$HOME"; '
            'printf "PY=%s\\n" "$(command -v python3 || true)"; '
            'printf "PYVER=%s\\n" "$(python3 --version 2>/dev/null | cut -d\' \' -f2)"; '
            'printf "SYSTEMD=%s\\n" "$(command -v systemctl >/dev/null 2>&1 && '
            '[ -d "/run/user/$(id -u)" ] && echo 1 || echo 0)"; '
            'printf "UMOUNT=%s\\n" "$(command -v umount || true)"; '
            'printf "SUDO=%s\\n" "$(sudo -n -l /bin/umount >/dev/null 2>&1 && echo 1 || echo 0)"'
        )
        res = self._exec(client, script, timeout=CONNECT_TIMEOUT + 10.0)
        fields = {}
        for line in res.stdout.splitlines():
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()

        return RemoteEnv(
            home=fields.get("HOME", ""),
            python=fields.get("PY", ""),
            python_version=fields.get("PYVER", ""),
            systemd=fields.get("SYSTEMD") == "1",
            umount=fields.get("UMOUNT", ""),
            sudo_umount=fields.get("SUDO") == "1",
        )

    # -- 安装 --------------------------------------------------------------- #

    def test_connection(self) -> RemoteEnv:
        """只测 SSH 与环境，不改动远端任何东西。"""
        self._log("info", f"正在连接 {self.ssh_target} …")
        with self._connect() as client:
            env = self._detect_env(client)
        self._log("success", f"SSH 连接成功。远端环境：{env.describe()}")
        if not env.ready:
            raise RemoteError(
                "远端缺少 python3",
                hint="请先在远端安装 python3 后重试",
            )
        if not env.sudo_umount:
            self._log(
                "warning",
                "远端普通用户无法免密 umount，U 盘挂载时将无法安全弹出。"
                "需要时请在该主机上执行：" + self.sudo_hint(),
            )
        return env

    def install(self) -> RemoteState:
        """幂等安装：上传脚本 → 注册服务 → 启动 → 验证 HTTP。

        重复执行完全安全：脚本与 unit 都是覆盖写，服务用 ``enable --now``。
        """
        self._log("info", f"开始安装远程桥接服务到 {self.ssh_target} …")
        self._log("info", f"待上传脚本：{self._script}")

        if not self._script.exists():
            raise RemoteError(
                f"找不到远端桥接脚本：{self._script}",
                hint="开发态应位于 scripts/linux_bridge.py；打包态应位于 resources/remote/",
            )

        port = self._config.bridge_port

        with self._connect() as client:
            env = self._detect_env(client)
            self._log("info", f"远端环境：{env.describe()}")
            if not env.ready:
                raise RemoteError("远端缺少 python3", hint="请先在远端安装 python3 后重试")

            self._upload(client, env)
            if env.systemd:
                self._install_systemd(client, env, port)
            else:
                self._install_nohup(client, env, port)

        state = self._wait_online()
        if state.http_online:
            self._log("success", f"远程桥接服务已就绪：{self.base_url}")
        else:
            self._log(
                "warning",
                f"服务已安装，但 {self.base_url} 无响应。"
                "请检查远端防火墙是否放通该端口，或查看远端日志。",
            )
        return state

    def _upload(self, client: paramiko.SSHClient, env: RemoteEnv) -> None:
        """SFTP 上传脚本。先建目录再 put，覆盖写保证幂等。"""
        home = env.home or "/tmp"
        script_abs = f"{home}/{_SCRIPT_REL}"
        bin_dir = f"{home}/{REMOTE_BIN_DIR}"

        self._exec(client, f"mkdir -p {shlex.quote(bin_dir)}", check=True)

        sftp = client.open_sftp()
        try:
            sftp.put(str(self._script), script_abs)
            sftp.chmod(script_abs, 0o755)
        except OSError as exc:
            raise RemoteError(
                f"上传脚本失败：{exc}",
                hint=f"目标路径 {script_abs}，请确认该用户对主目录有写权限",
            ) from exc
        finally:
            sftp.close()

        self._log("success", f"已上传脚本 → {script_abs}")

    def _install_systemd(self, client: paramiko.SSHClient, env: RemoteEnv, port: int) -> None:
        home = env.home or "/tmp"
        unit_abs = f"{home}/{_UNIT_REL}"
        script_abs = f"{home}/{_SCRIPT_REL}"
        unit_dir = f"{home}/.config/systemd/user"

        unit = (
            "[Unit]\n"
            "Description=USB-Switch Bridge\n"
            "After=network.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={env.python} {script_abs} {port}\n"
            "Restart=always\n"
            "RestartSec=3\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

        self._exec(client, f"mkdir -p {shlex.quote(unit_dir)}", check=True)

        sftp = client.open_sftp()
        try:
            with sftp.open(unit_abs, "w") as handle:
                handle.write(unit)
        except OSError as exc:
            raise RemoteError(f"写入 systemd unit 失败：{exc}") from exc
        finally:
            sftp.close()
        self._log("success", f"已写入 systemd unit → {unit_abs}")

        # enable --now 同时完成「开机自启」与「立即启动」，且可重复执行
        self._exec(client, f"{_SYSTEMCTL} daemon-reload", check=True, timeout=COMMAND_TIMEOUT)
        res = self._exec(
            client,
            f"{_SYSTEMCTL} enable --now {UNIT_NAME} || "
            f"({_SYSTEMCTL} start {UNIT_NAME})",
            timeout=COMMAND_TIMEOUT,
        )
        if res.code != 0:
            raise RemoteCommandError(
                "启动 systemd 服务失败",
                hint=(res.stderr or res.stdout)[:300],
            )
        self._log("success", f"服务已启用并启动（监听端口 {port}）")

        # 用户登出后服务会被 systemd 回收，必须开 linger 才能常驻
        linger = self._exec(
            client,
            "loginctl enable-linger \"$(id -un)\" 2>&1 || true",
            timeout=COMMAND_TIMEOUT,
        )
        if "Failed" in linger.output or "failed" in linger.output:
            self._log(
                "warning",
                "开启 linger 失败，远端用户登出后服务可能被停止。"
                "可在该主机执行：sudo loginctl enable-linger " + self._config.username,
            )
        else:
            self._log("info", "已开启 linger，用户登出后服务仍保持运行")

    def _install_nohup(self, client: paramiko.SSHClient, env: RemoteEnv, port: int) -> None:
        """无 systemd 时的回退：nohup + PID 文件（与 README 的手工方式一致）。"""
        home = env.home or "/tmp"
        state_dir = f"{home}/{REMOTE_STATE_DIR}"
        self._exec(client, f"mkdir -p {shlex.quote(state_dir)}", check=True)

        self._log("info", "远端无 systemd，改用 nohup + PID 文件方式")
        self._start_nohup(client, env, port)

    # -- nohup 形态 --------------------------------------------------------- #

    def _nohup_alive(self, client: paramiko.SSHClient) -> bool:
        res = self._exec(
            client,
            f'p=$(cat "$HOME/{_PID_REL}" 2>/dev/null || true); '
            '[ -n "$p" ] && kill -0 "$p" 2>/dev/null && echo 1 || echo 0',
        )
        return res.stdout.strip() == "1"

    def _start_nohup(self, client: paramiko.SSHClient, env: RemoteEnv, port: int) -> None:
        home = env.home or "/tmp"
        script_abs = f"{home}/{_SCRIPT_REL}"
        pid_abs = f"{home}/{_PID_REL}"
        log_abs = f"{home}/{_LOG_REL}"

        command = (
            f'mkdir -p "$(dirname {shlex.quote(pid_abs)})"; '
            f'if [ -f {shlex.quote(pid_abs)} ] && kill -0 "$(cat {shlex.quote(pid_abs)})" 2>/dev/null; '
            'then echo ALREADY; else '
            f'nohup {shlex.quote(env.python)} {shlex.quote(script_abs)} {port} '
            f'>> {shlex.quote(log_abs)} 2>&1 & echo $! > {shlex.quote(pid_abs)}; echo STARTED; fi'
        )
        res = self._exec(client, command, check=True, timeout=COMMAND_TIMEOUT)
        if res.stdout.strip() == "ALREADY":
            self._log("info", "服务已在运行，无需重复启动")
        else:
            self._log("success", f"服务已通过 nohup 启动（监听端口 {port}）")

    # -- 启动 / 停止 / 卸载 ------------------------------------------------- #

    def start(self) -> RemoteState:
        self._log("info", f"正在启动远程桥接服务（{self.ssh_target}）…")
        with self._connect() as client:
            env = self._detect_env(client)
            if env.systemd:
                self._exec(client, f"{_SYSTEMCTL} start {UNIT_NAME}", check=True)
                self._log("success", "服务已启动")
            else:
                self._start_nohup(client, env, self._config.bridge_port)
        return self._wait_online()

    def stop(self) -> RemoteState:
        self._log("info", "正在停止远程桥接服务 …")
        with self._connect() as client:
            env = self._detect_env(client)
            if env.systemd:
                # 停一个本来就没跑的服务不算错误
                self._exec(client, f"{_SYSTEMCTL} stop {UNIT_NAME} || true")
                self._log("info", "服务已停止")
            else:
                res = self._exec(
                    client,
                    f'if [ -f "$HOME/{_PID_REL}" ]; then '
                    f'kill "$(cat "$HOME/{_PID_REL}")" 2>/dev/null || true; '
                    f'rm -f "$HOME/{_PID_REL}"; echo KILLED; else echo NONE; fi',
                )
                self._log(
                    "info",
                    "服务已停止" if res.stdout.strip() == "KILLED" else "服务本来就未运行",
                )

        state = self.inspect()
        if state.http_online:
            self._log("warning", "停止后远端 HTTP 仍在响应，可能有另一份实例在跑")
        return state

    def uninstall(self) -> RemoteState:
        """幂等卸载：停服务 → disable → 删 unit → 删脚本。**保留日志**。

        对「本来就没装」的情况静默成功 —— 卸载按钮不该因为目标不存在而报错。
        """
        self._log("info", "正在卸载远程桥接服务 …")
        with self._connect() as client:
            env = self._detect_env(client)

            if env.systemd:
                # 先 disable --now（停止 + 取消自启），不存在时静默
                self._exec(client, f"{_SYSTEMCTL} disable --now {UNIT_NAME} >/dev/null 2>&1 || true")
                self._exec(client, f"{_SYSTEMCTL} reset-failed {UNIT_NAME} >/dev/null 2>&1 || true")

            # nohup 残留的 PID 也要清掉，两种形态可能先后被用过
            self._exec(
                client,
                f'if [ -f "$HOME/{_PID_REL}" ]; then '
                f'kill "$(cat "$HOME/{_PID_REL}")" 2>/dev/null || true; fi; '
                f'rm -f "$HOME/{_PID_REL}"',
            )

            removed = []
            for rel in (_UNIT_REL, _SCRIPT_REL):
                res = self._exec(
                    client,
                    f'if [ -e "$HOME/{rel}" ]; then rm -f "$HOME/{rel}" && echo REMOVED; else echo ABSENT; fi',
                )
                removed.append((rel, res.stdout.strip() == "REMOVED"))

            if env.systemd:
                self._exec(client, f"{_SYSTEMCTL} daemon-reload || true")

            for rel, was_removed in removed:
                self._log("info", f"{'已删除' if was_removed else '本就不存在'}：~/{rel}")
            self._log(
                "info",
                f"日志文件已保留：~/{_LOG_REL}（如需清理请手工删除）",
            )

        state = self.inspect()
        if state.http_online:
            self._log(
                "warning",
                "卸载后远端 HTTP 仍在响应，可能有手工启动的实例仍在运行",
            )
        else:
            self._log("success", "远程桥接服务已卸载")
        return state

    # -- 启动后验证 --------------------------------------------------------- #

    def _wait_online(self) -> RemoteState:
        """轮询 HTTP 直到就绪或超时。返回最终状态（不抛异常 —— 装好了但端口
        被防火墙挡住是常见情况，应当如实呈现而不是当成安装失败）。"""
        deadline = time.monotonic() + START_WAIT
        payload = None
        while time.monotonic() < deadline:
            payload = self.probe()
            if payload and payload.get("ok"):
                break
            time.sleep(START_POLL)

        if payload and payload.get("ok"):
            return RemoteState(
                ssh_reachable=True,
                ssh_detail="可达",
                http_online=True,
                http_detail="在线",
                drives=tuple(payload.get("disks") or []),
                ssh_checked=True,
            )
        return RemoteState(
            ssh_reachable=True,
            ssh_detail="可达",
            http_online=False,
            http_detail=f"无响应（等待 {START_WAIT:g} 秒）",
            ssh_checked=True,
        )


class _Session:
    """SSH 连接的上下文管理器，保证异常路径也关闭连接。"""

    def __init__(self, bridge: RemoteBridge) -> None:
        self._bridge = bridge
        self._client: paramiko.SSHClient | None = None

    def __enter__(self) -> paramiko.SSHClient:
        self._client = self._bridge._open()
        return self._client

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._client is not None:
            self._client.close()
            self._client = None
        return False
