"""数据类与枚举。纯 stdlib，不依赖 Qt。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #


class Host(str, Enum):
    """U 盘当前连接的主机。"""

    A = "A"
    B = "B"
    NONE = "none"

    @property
    def label(self) -> str:
        return {"A": "Host A", "B": "Host B", "none": "已断开"}[self.value]

    @classmethod
    def from_firmware(cls, value: str) -> "Host":
        v = value.strip().lower()
        if v == "a":
            return cls.A
        if v == "b":
            return cls.B
        return cls.NONE


class BridgeKind(str, Enum):
    """某个 Host 角色的桥接端点类型。"""

    LOCAL = "local"
    REMOTE = "remote"


class BridgeRunState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"
    UNKNOWN = "unknown"


class AuthMethod(str, Enum):
    PASSWORD = "password"
    KEY = "key"


class ReplyKind(str, Enum):
    """固件回传报文的类别。"""

    OK = "ok"
    FAIL = "fail"
    STATUS = "status"
    READY = "ready"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# 设备状态
# --------------------------------------------------------------------------- #

_HOST_RE = re.compile(r"host=([A-Za-z]+)")
_DET_RE = re.compile(r"det([AB])=([01])")


@dataclass(frozen=True)
class DeviceStatus:
    """固件状态报文，形如 ``host=A, detA=1, detB=0``。

    报文可能带前缀（`STATUS: ` / `READY: ` / `OK: `），解析时忽略前缀。
    """

    host: Host
    det_a: bool
    det_b: bool
    raw: str = ""

    @classmethod
    def parse(cls, text: str) -> "DeviceStatus | None":
        """无法解析出 host= 字段时返回 None。"""
        m_host = _HOST_RE.search(text)
        if not m_host:
            return None
        dets = {k.upper(): v == "1" for k, v in _DET_RE.findall(text)}
        return cls(
            host=Host.from_firmware(m_host.group(1)),
            det_a=dets.get("A", False),
            det_b=dets.get("B", False),
            raw=text,
        )


def classify_reply(text: str) -> ReplyKind:
    """判断固件回传报文属于哪一类。"""
    head = text.strip().upper()
    if head.startswith("OK"):
        return ReplyKind.OK
    if head.startswith("FAIL"):
        return ReplyKind.FAIL
    if head.startswith("STATUS"):
        return ReplyKind.STATUS
    if head.startswith("READY"):
        return ReplyKind.READY
    return ReplyKind.UNKNOWN


# --------------------------------------------------------------------------- #
# 桥接配置
# --------------------------------------------------------------------------- #

DEFAULT_LOCAL_BRIDGE_PORT = 8737
DEFAULT_REMOTE_BRIDGE_PORT = 8738
DEFAULT_SSH_PORT = 22

#: 主机名 / IP 允许的字符。刻意收窄：这些值会出现在 SSH 目标串与 known_hosts
#: 里，放行 shell 元字符没有任何好处，只有风险。
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]]+$")
#: Unix 用户名（含 Debian 系常见的 `user.name` 形式）
_USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,31}$")


@dataclass
class LocalBridgeConfig:
    port: int = DEFAULT_LOCAL_BRIDGE_PORT

    @property
    def http_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass
class RemoteBridgeConfig:
    """远程（Linux）桥接连接参数。

    ⚠️ ``ssh_port`` 与 ``bridge_port`` 是两个完全独立的端口，切勿混用：

    - ``ssh_port``    管理通道，用于安装 / 启停 / 卸载远端服务
    - ``bridge_port`` 远端桥接服务的 HTTP 监听端口，用于安全弹出与探活
    """

    host: str = ""
    ssh_port: int = DEFAULT_SSH_PORT
    username: str = ""
    auth: AuthMethod = AuthMethod.PASSWORD
    password: str = ""  # 内存中的明文；落盘时由 crypto 加密
    key_path: str = ""
    passphrase: str = ""  # 同上
    bridge_port: int = DEFAULT_REMOTE_BRIDGE_PORT
    display_name: str = ""
    #: 经 SSH 隧道访问远端桥接服务（绕开远端防火墙，且不必暴露无鉴权的 HTTP）
    use_tunnel: bool = True
    #: 隧道在本机监听的端口；0 = 每次自动挑一个空闲端口
    tunnel_local_port: int = 0

    #: **运行期**字段，**不落盘**：隧道就绪后由 ``SshTunnel`` 填入，
    #: 形如 ``http://127.0.0.1:54321``。
    #:
    #: 把它放在这里，是为了让「桥接服务到底在哪个地址」只有**一个**来源。
    #: 否则 ``ejector``、``probe()``、安装后的 HTTP 验证三处都要各自判断
    #: 「走隧道还是直连」—— 漏掉任何一处就会出现「状态显示在线、切换却连不上」
    #: 这类自相矛盾的表现。
    runtime_http_base: str = field(default="", compare=False, repr=False)

    @property
    def direct_http_base(self) -> str:
        """不走隧道的直连地址（用于 UI 说明「关掉隧道时会连这里」）。"""
        return f"http://{self.host}:{self.bridge_port}"

    @property
    def is_configured(self) -> bool:
        """是否已填写远端地址 —— 「远程桥接配置了」的唯一判据。

        Host B 固定为远程角色后，「配置了 / 没配」不再看角色类型，
        只看这里有没有一个可连接的目标。
        """
        return bool(self.host.strip())

    @property
    def http_base(self) -> str:
        return self.runtime_http_base or self.direct_http_base

    @property
    def ssh_target(self) -> str:
        return f"{self.username}@{self.host}:{self.ssh_port}"

    @property
    def title(self) -> str:
        return self.display_name or self.host or "（未配置）"

    def validate(self) -> list[str]:
        """返回人类可读的校验错误列表；空列表表示通过。

        UI 在点击「安装」的瞬间调用它，避免等到 SSH 超时后才报一个笼统错误。
        """
        errors: list[str] = []
        host = self.host.strip()
        if not host:
            errors.append("IP / 主机名不能为空")
        elif not _SSH_HOST_RE.match(host):
            errors.append("IP / 主机名含非法字符（只允许字母、数字、`.` `-` `:` `[` `]`）")

        username = self.username.strip()
        if not username:
            errors.append("用户名不能为空")
        elif not _USERNAME_RE.match(username):
            errors.append("用户名格式不正确（只允许字母、数字、`.` `_` `-`，且不能以数字开头）")

        if not 1 <= self.ssh_port <= 65535:
            errors.append("SSH 端口必须在 1-65535 之间")
        if not 1 <= self.bridge_port <= 65535:
            errors.append("桥接端口必须在 1-65535 之间")
        if self.tunnel_local_port and not 1 <= self.tunnel_local_port <= 65535:
            errors.append("隧道本地端口必须为 0（自动）或 1-65535 之间")
        if self.auth is AuthMethod.PASSWORD:
            if not self.password:
                errors.append("请填写密码")
        elif not self.key_path.strip():
            errors.append("选择密钥认证时必须指定私钥路径")
        return errors


@dataclass
class BridgeEndpoint:
    """某个 Host 角色的桥接端点。角色已固定：Host A 用本机端、Host B 用远程端。"""

    kind: BridgeKind = BridgeKind.LOCAL
    local: LocalBridgeConfig = field(default_factory=LocalBridgeConfig)
    remote: RemoteBridgeConfig = field(default_factory=RemoteBridgeConfig)

    @property
    def http_base(self) -> str:
        if self.kind is BridgeKind.LOCAL:
            return self.local.http_base
        return self.remote.http_base

    @property
    def title(self) -> str:
        if self.kind is BridgeKind.LOCAL:
            return f"本机 :{self.local.port}"
        return f"远程 {self.remote.title}:{self.remote.bridge_port}"


# --------------------------------------------------------------------------- #
# 应用配置
# --------------------------------------------------------------------------- #


@dataclass
class BleConfig:
    device_name: str = "USB-Switch"
    auto_connect: bool = False


#: 可折叠分区。顺序即界面里的排列顺序。
SECTION_IDS: tuple[str, ...] = ("detail", "bridge", "remote", "options", "log")

#: 各分区的默认开合状态。日志默认展开 —— 出问题时用户最需要看到的正是它，
#: 而其余分区都是「配好就不动」的。
DEFAULT_SECTION_EXPANDED: dict[str, bool] = {
    "detail": False,
    "bridge": False,
    "remote": False,
    "options": False,
    "log": True,
}


@dataclass
class WindowConfig:
    close_to_tray: bool = True
    geometry: str = ""
    #: 首次收起时提示过一次即置位，避免每次关窗都弹气泡
    tray_hint_shown: bool = False
    #: 折叠区开合状态 ``{分区 id: 是否展开}``。缺键表示用该分区的默认值 ——
    #: 这样新增分区不必迁移旧配置。
    expanded_sections: dict[str, bool] = field(default_factory=dict)

    def section_expanded(self, section: str) -> bool:
        saved = self.expanded_sections.get(section)
        if saved is None:
            return DEFAULT_SECTION_EXPANDED.get(section, False)
        return bool(saved)


@dataclass
class AutostartConfig:
    enabled: bool = False
    start_minimized: bool = True


@dataclass
class AppConfig:
    ble: BleConfig = field(default_factory=BleConfig)
    window: WindowConfig = field(default_factory=WindowConfig)
    autostart: AutostartConfig = field(default_factory=AutostartConfig)
    #: Host 角色固定：**Host A 恒为本机（Windows），Host B 恒为远程（Linux）**。
    #: 物理拓扑如此 —— 本机必然是其中一路 Host，另一路接 Linux 设备。
    #: 「哪个角色接哪种桥接」不再可配置；配置文件里的旧角色值在加载时收敛。
    bridges: dict[Host, BridgeEndpoint] = field(
        default_factory=lambda: {
            Host.A: BridgeEndpoint(kind=BridgeKind.LOCAL),
            Host.B: BridgeEndpoint(kind=BridgeKind.REMOTE),
        }
    )

    @property
    def local_bridge_port(self) -> int:
        """本机桥接服务应监听的端口（本机恒为 Host A）。"""
        return self.bridges[Host.A].local.port

    def endpoint_for(self, host: Host) -> BridgeEndpoint:
        """取某个 Host 角色的桥接端点，缺失时给一个安全的默认值。"""
        return self.bridges.get(host) or BridgeEndpoint()

    def remote_host(self) -> "Host | None":
        """远程（Linux）桥接角色 —— 固定为 Host B；未填写地址时返回 None。

        「哪个角色是远程」已不可配置，这个方法保留的职责是回答
        **「远程桥接是否已配置」**—— UI 与 worker 的判据都收敛在这里，
        避免各自检查字段出现口径不一。
        """
        remote = self.bridges.get(Host.B)
        if remote is None or not remote.remote.is_configured:
            return None
        return Host.B
