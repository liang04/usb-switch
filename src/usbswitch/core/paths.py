"""路径解析 —— 全项目唯一允许出现 `sys._MEIPASS` / `sys.frozen` 的地方。

开发态与打包态（PyInstaller）的资源路径不同。把分支限制在本模块，
可以避免 `if frozen:` 散落到十几个文件里 —— 那是 PyInstaller 项目最常见的翻车点。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "USBSwitchConsole"

#: 测试用：覆盖运行期数据目录，避免污染真实的 %APPDATA%
ENV_DATA_DIR = "USBSWITCH_DATA_DIR"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物中。"""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """随包分发的只读资源根目录（icons / remote 模板）。

    注意冻结态的层级：build.spec 把 `src/usbswitch/resources` 映射到
    `_MEIPASS/usbswitch/resources`，所以这里要补上包名。
    """
    if is_frozen():
        return Path(sys._MEIPASS) / "usbswitch" / "resources"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent / "resources"


def repo_root() -> Path:
    """仓库根目录。仅开发态存在。

    路径层级：<repo>/src/usbswitch/core/paths.py
    """
    return Path(__file__).resolve().parents[3]


def linux_bridge_script() -> Path:
    """远端（Linux）桥接脚本的 canonical 位置。

    只保留一份副本，避免「GUI 是新的、脚本是旧的」这类版本漂移：

    - 开发态：仓库里的 `scripts/linux_bridge.py`（README 的 scp 流程直接可用）
    - 打包态：build.spec 把它复制进 `resources/remote/`
    """
    if is_frozen():
        return resource_root() / "remote" / "linux_bridge.py"
    return repo_root() / "scripts" / "linux_bridge.py"


def data_dir() -> Path:
    """运行期可变数据目录（配置 / 日志 / 崩溃转储）。

    **绝不写安装目录** —— 安装目录可能位于 ``C:\\Program Files\\`` 而无写权限。
    """
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        root = Path(override)
    else:
        appdata = os.environ.get("APPDATA")
        root = (Path(appdata) / APP_NAME) if appdata else (Path.home() / f".{APP_NAME.lower()}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_file() -> Path:
    return data_dir() / "config.json"


def log_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def crash_dir() -> Path:
    d = data_dir() / "crash"
    d.mkdir(parents=True, exist_ok=True)
    return d
