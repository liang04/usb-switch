"""静态检查（ruff）——把「引用了不存在的名字」这类 bug 挡在运行之前。

为什么值得单开一个测试文件
--------------------------

踩过一次代价不小的坑：某次重构删掉了 ``ui/main_window.py`` 里的
``FIRST_MINIMIZE_HINT`` 常量，却漏了一处引用：

```python
if not raw or raw.startswith(FIRST_MINIMIZE_HINT):
```

``_restore_geometry()`` 里这句因为 `or` 短路，在测试里（geometry 为空）
**永远不会被求值**，所以整套测试一路绿灯；而真实用户的第二次启动
（此时 geometry 已保存、非空）会直接 ``NameError`` 崩掉。

这类「只在特定运行路径才炸」的名字错误，单元测试很难穷尽 ——
静态检查才是对的工具。这个文件把 ruff 接进测试流程，
让 `pytest` 一次就顺带做完这件事。

没装 ruff 时自动跳过，不强制所有环境都装（CI 里装上即可）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 只跑「可能真的是 bug」的规则，不做代码风格审查。
#: F  = pyflakes（未定义名字 F821、未使用导入 F401、未使用变量 F841 …）
#: E9 = 语法层面能解析但明显有错（如 E999）
SELECT = "F,E9"


def _run_ruff(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_ruff_is_available_or_skipped():
    """ruff 是可选依赖；这里只是把「跳过」这件事显式化。"""
    probe = _run_ruff("--version")
    if probe.returncode != 0:
        pytest.skip("未安装 ruff：pip install -e .[dev]")


def test_source_has_no_undefined_names_or_dead_imports():
    probe = _run_ruff("--version")
    if probe.returncode != 0:
        pytest.skip("未安装 ruff：pip install -e .[dev]")

    result = _run_ruff("check", "src", "tests", "--select", SELECT, "--output-format", "concise")

    assert result.returncode == 0, (
        "静态检查未通过 —— 多半是引用了不存在的名字，或留下了无用导入：\n"
        f"{result.stdout or result.stderr}\n"
        f"（可先看：{sys.executable} -m ruff check src tests --select {SELECT}）"
    )
