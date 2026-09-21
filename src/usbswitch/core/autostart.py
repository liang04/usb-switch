"""开机自启 —— 注册表 ``HKCU``，**不需要管理员权限**。

用 ``HKCU\\...\\Run`` 而不是「启动」文件夹快捷方式：注册表是纯文本值，
读写、校验、清理都更简单，也不会在用户目录里留下一个可能失效的 .lnk。
"""

from __future__ import annotations

import logging
import sys

from .errors import ConfigError

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "USBSwitchConsole"
#: 自启拉起时直接进托盘，不弹窗口打扰用户
STARTUP_FLAG = "--minimized"

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import winreg
else:  # pragma: no cover —— 非 Windows 走不到这条分支以外的逻辑
    winreg = None  # type: ignore[assignment]


def launch_command() -> str:
    """构造自启命令行。

    开发态与打包态的可执行路径不同，必须分支：

    - **打包态**：``sys.executable`` 就是 exe 本身
    - **开发态**：``sys.executable`` 是 python.exe，需要补 ``-m usbswitch``
      （依赖 ``pip install -e .`` 已把包装进环境）

    **路径必须加引号**：不加引号时 Windows 会在第一个空格处截断注册表值，
    ``C:\\Program Files\\...`` 必踩。
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {STARTUP_FLAG}'
    return f'"{sys.executable}" -m usbswitch {STARTUP_FLAG}'


def current_command() -> str | None:
    """返回注册表中已登记的命令；未启用返回 None。"""
    if not _IS_WINDOWS:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("读取开机自启项失败: %s", exc)
        return None
    return str(value)


def is_enabled() -> bool:
    """以**注册表的实际状态**为准，而不是配置文件里的意图。

    用户可能手工删掉它，或被安全软件清理 —— 配置与实际脱节会让 UI 显示错误。
    """
    return current_command() is not None


def enable(command: str | None = None) -> str:
    """写入自启项，返回写入的命令。

    Raises:
        ConfigError: 写不进去，或写完回读不一致（被组策略 / 安全软件拦截）。
    """
    if not _IS_WINDOWS:
        raise ConfigError("开机自启仅支持 Windows")

    target = command or launch_command()
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, target)
    except OSError as exc:
        raise ConfigError(
            f"写入开机自启项失败（{exc}）",
            hint="可能被组策略或安全软件阻止",
        ) from exc

    # 回读校验：写成功不代表生效
    actual = current_command()
    if actual != target:
        raise ConfigError(
            "开机自启项写入后未能生效",
            hint=f"注册表实际值为 {actual!r}，可能被安全软件拦截",
        )

    log.info("已启用开机自启: %s", target)
    return target


def disable() -> None:
    """删除自启项。本来就不存在时静默成功（幂等）。"""
    if not _IS_WINDOWS:
        return
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        # 键或值本来就不存在 —— 目标状态已经达成
        return
    except OSError as exc:
        raise ConfigError(
            f"删除开机自启项失败（{exc}）",
            hint="可能被组策略或安全软件阻止",
        ) from exc
    log.info("已关闭开机自启")


def sync_to(desired: bool) -> bool:
    """把注册表对齐到期望状态，返回最终是否启用。"""
    if desired and not is_enabled():
        enable()
    elif not desired and is_enabled():
        disable()
    return is_enabled()
