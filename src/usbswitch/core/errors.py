"""领域异常。

UI 层按异常类型给出精准的中文提示，而不是把 bleak / paramiko / ctypes
的原始异常直接甩给用户。每个异常都带一个可选的 `hint`，用于补充「该怎么办」。
"""

from __future__ import annotations


class UsbSwitchError(Exception):
    """所有业务异常的基类。"""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message}（{self.hint}）" if self.hint else self.message


# --------------------------------------------------------------------------- #
# 磁盘 / 安全弹出
# --------------------------------------------------------------------------- #
class DiskError(UsbSwitchError):
    """磁盘相关操作的基类。"""


class NoRemovableDriveError(DiskError):
    """本机当前没有可移动磁盘。"""


class MultipleRemovableDrivesError(DiskError):
    """检测到多个可移动磁盘，无法自动判定目标。"""


class EjectError(DiskError):
    """安全弹出（锁定 + 卸载卷）失败。"""


# --------------------------------------------------------------------------- #
# BLE
# --------------------------------------------------------------------------- #
class BleError(UsbSwitchError):
    """BLE 相关操作的基类。"""


class BleDeviceNotFoundError(BleError):
    """扫描未发现目标设备。"""


class BleServiceDiscoveryError(BleError):
    """GATT 服务发现失败 —— Windows 上典型的蓝牙缓存损坏症状。"""

    def __init__(self, message: str = "服务发现失败", *, hint: str = "") -> None:
        super().__init__(
            message,
            hint=hint or "这是 Windows GATT 缓存损坏的典型症状，请关闭再打开蓝牙开关后重试",
        )


class BleNoResponseError(BleError):
    """命令已发出但设备无回复，切换结果未知。"""


class BleCommandRejectedError(BleError):
    """固件明确回复 FAIL（例如目标主机未接入 VBUS）。"""


# --------------------------------------------------------------------------- #
# 远程（Linux）桥接
# --------------------------------------------------------------------------- #
class RemoteError(UsbSwitchError):
    """远程桥接管理相关操作的基类。"""


class RemoteUnreachableError(RemoteError):
    """网络不可达 / 连接被拒绝 / 超时。"""


class RemoteAuthError(RemoteError):
    """认证失败（密码或密钥错误）。"""


class RemoteHostKeyError(RemoteError):
    """主机密钥未被信任。"""


class RemoteCommandError(RemoteError):
    """远端命令执行返回非零退出码。"""


# --------------------------------------------------------------------------- #
# 本地桥接服务
# --------------------------------------------------------------------------- #
class BridgeError(UsbSwitchError):
    """本地桥接服务相关操作的基类。"""


class BridgePortInUseError(BridgeError):
    """配置的端口已被占用。"""


class BridgeUnreachableError(BridgeError):
    """桥接服务探测不到（进程活着 ≠ 服务可用）。"""


# --------------------------------------------------------------------------- #
# SSH 隧道
# --------------------------------------------------------------------------- #
class TunnelError(BridgeError):
    """SSH 隧道建立或维持失败。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
class ConfigError(UsbSwitchError):
    """配置文件读写或校验失败。"""
