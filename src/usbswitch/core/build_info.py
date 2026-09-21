"""构建身份 —— 回答「我现在跑的这一版，是新的还是旧的」。

存在的理由很具体：**改了源码却仍在跑旧的 exe，程序不会报任何错。**
症状是「界面没变」「改了没反应」，而唯一的排查方式是对比文件时间戳 ——
用户看不到构建时间，我自己也会忘。

这一条真的发生过：界面改版落在 10:55–12:24，而用户双击的是 10:26 打的包，
看到的就是旧界面。

两条信息来源：

- **打包态**：读随包分发的 ``_build.json``（``usbswitch.spec`` 在构建时生成）。
  它记录构建时刻，以及**构建当时源码树的最新修改时间**。
- **开发态**：直接扫描 ``src/usbswitch``，取最新修改时间。

:func:`freshness_problem` 把两者相减：产物里记的源码时间戳落后于当前源码树，
就说明「改了但没重新打包」。这条判断只在本机能定位源码树时生效 ——
分发到别人机器上源码树不存在，它如实返回「无法判定」，不误报。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..version import __version__

log = logging.getLogger(__name__)

#: 由 usbswitch.spec 在构建时生成，落在 ``usbswitch/_build.json``
MANIFEST_NAME = "_build.json"

#: 源码树里的标记文件，用来判定「这里是仓库根」
REPO_MARKERS = ("pyproject.toml", "src/usbswitch")

#: 允许的时间偏差。构建紧跟编辑时两边可能只差几毫秒，别让浮点误差变成误报。
_SLACK_SECONDS = 1.0

#: 环境变量：显式指定源码树位置（打包产物在外地跑自检时用得上）
SOURCE_ENV = "USBSWITCH_SOURCE"


@dataclass(frozen=True)
class BuildInfo:
    """一份构建的样子。时间戳都是 epoch 秒，便于相减。"""

    version: str
    frozen: bool
    #: 构建时刻；开发态为 None
    built_at: float | None
    #: 打包态：构建时源码树的最新修改时间；开发态：实时扫描结果
    source_stamp: float | None

    def describe(self) -> str:
        """完整描述，用于日志与自检报告。"""
        where = "打包态" if self.frozen else "开发态"
        parts = [f"v{self.version}", where]
        if self.built_at is not None:
            parts.append(f"构建于 {_format(self.built_at)}")
        if self.source_stamp is not None:
            parts.append(f"源码 {_format(self.source_stamp)}")
        if self.frozen:
            parts.append(str(sys.executable))
        return "，".join(parts)

    def short(self) -> str:
        """一行短标记，用于状态栏常驻显示。"""
        if not self.frozen:
            suffix = f"dev {_format(self.source_stamp)}" if self.source_stamp else "dev"
            return f"v{self.version} · {suffix}"
        if self.built_at is None:
            return f"v{self.version} · 打包"
        return f"v{self.version} · 构建 {_format(self.built_at)}"


def _format(stamp: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(stamp))


# --------------------------------------------------------------------------- #
# 来源
# --------------------------------------------------------------------------- #


def _is_frozen() -> bool:
    """单独抽出来，测试可以直接替换它而不必去动全局的 ``sys.frozen``。"""
    return bool(getattr(sys, "frozen", False))


def manifest_path() -> Path:
    """随包分发的构建清单位置：``usbswitch/_build.json``。

    刻意不放在 ``resources/`` —— 它不是资源，而是这一份构建的元数据。
    位置必须与 ``usbswitch.spec`` 里 datas 的目标目录一致
    （``tests/unit/test_packaging.py`` 会盯着这条）。
    """
    return Path(__file__).resolve().parent.parent / MANIFEST_NAME


def _read_manifest() -> dict:
    path = manifest_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.debug("构建清单无法解析（%s）：%s", path, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def scan_source_stamp(root: Path) -> float | None:
    """源码树里最新的 ``.py`` 修改时间。"""
    if not root.is_dir():
        return None
    newest: float | None = None
    for path in root.rglob("*.py"):
        try:
            stamp = path.stat().st_mtime
        except OSError:  # 文件正好被删/换掉，跳过即可
            continue
        if newest is None or stamp > newest:
            newest = stamp
    return newest


def repo_source_root(start: Path | None = None) -> Path | None:
    """从若干起点向上找仓库里的 ``src/usbswitch``；找不到返回 None。

    起点依次是：``USBSWITCH_SOURCE`` 环境变量、``start``、当前工作目录。
    在别人的机器上这些都指不到仓库，于是返回 None —— 这是**正常**的，
    调用方据此跳过新鲜度判定。
    """
    candidates: list[Path] = []
    explicit = os.environ.get(SOURCE_ENV)
    if explicit:
        candidates.append(Path(explicit))
    if start is not None:
        candidates.append(Path(start))
    candidates.append(Path.cwd())

    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        for base in (candidate, *candidate.parents):
            if all((base / marker).exists() for marker in REPO_MARKERS):
                return base / "src" / "usbswitch"
    return None


def current() -> BuildInfo:
    """当前这一份构建的身份。"""
    frozen = _is_frozen()

    if frozen:
        manifest = _read_manifest()
        return BuildInfo(
            version=str(manifest.get("version") or __version__),
            frozen=True,
            built_at=_as_float(manifest.get("built_at")),
            source_stamp=_as_float(manifest.get("source_stamp")),
        )

    return BuildInfo(
        version=__version__,
        frozen=False,
        built_at=None,
        source_stamp=scan_source_stamp(Path(__file__).resolve().parent.parent),
    )


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 新鲜度
# --------------------------------------------------------------------------- #


def freshness_problem(start: Path | None = None) -> str | None:
    """产物是否落后于源码。无法判定时返回 None（不是「没问题」，是「测不了」）。

    只有打包态 + 能定位到源码树两者同时成立才会给出结论。
    """
    info = current()
    if not info.frozen or info.source_stamp is None:
        return None

    root = repo_source_root(start)
    if root is None:
        return None

    live = scan_source_stamp(root)
    if live is None:
        return None
    if live <= info.source_stamp + _SLACK_SECONDS:
        return None

    newer = _count_newer(root, info.source_stamp)
    return (
        f"这个产物落后于源码：产物打包于 {_format(info.built_at or 0)}"
        f"（当时源码 {_format(info.source_stamp)}），"
        f"而当前源码 {_format(live)}，其中 {newer} 个文件比产物新。"
        "请重新打包后再验证 —— 否则你看到的是旧界面、旧行为。"
    )


def _count_newer(root: Path, than: float) -> int:
    count = 0
    for path in root.rglob("*.py"):
        try:
            if path.stat().st_mtime > than:
                count += 1
        except OSError:
            continue
    return count
