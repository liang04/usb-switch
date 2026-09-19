"""架构铁律的自动化守卫。

`core` 层必须完全与 GUI 框架解耦 —— 否则单元测试就得装 PySide6，
CLI 脚本也没法复用这些逻辑。把它写成测试，比写在文档里靠人自觉可靠。
"""

from __future__ import annotations

import ast
from pathlib import Path

import usbswitch.core

FORBIDDEN_ROOTS = {"PySide6", "PyQt5", "PyQt6", "shiboken6", "shiboken2"}


def _imported_roots(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
    return found


def test_core_never_imports_qt():
    core_dir = Path(usbswitch.core.__file__).parent
    offenders: list[str] = []

    for path in sorted(core_dir.rglob("*.py")):
        for lineno, name in _imported_roots(path):
            if name.split(".")[0] in FORBIDDEN_ROOTS:
                offenders.append(f"{path.name}:{lineno} -> {name}")

    assert not offenders, "core 层禁止依赖 Qt，违规：" + "; ".join(offenders)


def test_core_has_no_top_level_qt_attribute():
    """兜底：即使通过 importlib 动态导入，模块命名空间里也不该出现 Qt 符号。"""
    core_dir = Path(usbswitch.core.__file__).parent
    modules = sorted(p.stem for p in core_dir.glob("*.py") if p.stem != "__init__")
    assert modules, "core 包为空，测试本身可能失效了"

    import importlib

    for name in modules:
        module = importlib.import_module(f"usbswitch.core.{name}")
        leaked = [attr for attr in dir(module) if attr.startswith(("PySide", "PyQt"))]
        assert not leaked, f"usbswitch.core.{name} 泄漏了 Qt 符号：{leaked}"
