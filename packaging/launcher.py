"""PyInstaller 的入口脚本。

为什么不直接把 ``src/usbswitch/main.py`` 当入口
-----------------------------------------------

PyInstaller 把入口脚本当作**顶层模块**执行。``main.py`` 里全是
``from . import ...`` 这样的相对导入，脱离包上下文后会直接 ImportError。

所以需要一个极薄的启动器：先把 ``src`` 加进 ``sys.path``，
再按正常方式导入包 —— 打包态与开发态走的完全是同一份代码。
"""

from __future__ import annotations

import sys
from pathlib import Path

# PyInstaller 打包后 __file__ 位于 _MEIPASS 之内，这个分支只为「直接
# python packaging/launcher.py」调试时兜底。
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from usbswitch.main import _run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(_run())
