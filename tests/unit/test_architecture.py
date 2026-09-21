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


# --------------------------------------------------------------------------- #
# 信号连接：原生信号不得直连自定义信号的 .emit
# --------------------------------------------------------------------------- #


def _ui_dir() -> Path:
    return Path(usbswitch.core.__file__).parent.parent / "ui"


def _parameterized_signals(tree: ast.Module) -> set[str]:
    """本模块里声明为「带参数」的自定义信号名（``xxx = Signal(bool)``）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if isinstance(func, ast.Name) and func.id == "Signal" and node.value.args:
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _direct_emit_connections(tree: ast.Module) -> list[tuple[int, str]]:
    """找出 ``<obj>.<signal>.connect(self.<name>.emit)`` 形式的连接。"""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "connect"):
            continue
        arg = node.args[0]
        if (
            isinstance(arg, ast.Attribute)
            and arg.attr == "emit"
            and isinstance(arg.value, ast.Attribute)
        ):
            found.append((node.lineno, arg.value.attr))
    return found


def test_parameterized_signals_are_not_wired_straight_to_emit():
    """带参数的自定义信号，不许直接接原生信号的 ``.emit``。

    事故现场：``self._enable.clicked.connect(self.enabledChanged.emit)``。Qt 原生
    信号常常带默认参数 —— ``QAbstractButton.clicked(bool checked = false)`` 因此在
    PySide6 里注册了 ``clicked()`` 与 ``clicked(bool)`` 两个重载；把一个「需要一个
    参数」的 callable 接上去会命中**无参**那个，参数丢空、抛 TypeError、槽静默不
    执行。表现是「界面上取消了勾选、配置却没落盘，重启又自己勾回来」，而单元测试
    因为直接调处理函数而全绿。

    规矩：中间接一个自己读控件状态的槽（``xxx.clicked.connect(self._on_xxx)``）。
    接 ``Signal()``（无参）的 ``.emit`` 不受影响，仍然允许。
    """
    offenders: list[str] = []
    for path in sorted(_ui_dir().rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parameterized = _parameterized_signals(tree)
        for lineno, name in _direct_emit_connections(tree):
            if name in parameterized:
                offenders.append(f"{path.name}:{lineno} -> self.{name}.emit")

    assert not offenders, (
        "原生信号直连了带参数信号的 .emit（参数会丢空、槽静默不执行），"
        "请改接一个从控件读值的槽：" + "; ".join(offenders)
    )
