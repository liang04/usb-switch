"""纯逻辑层。

**本包及其所有子模块严禁 import 任何 Qt 符号**（PySide6 / PyQt）。
这是项目的架构铁律：core 必须可被单元测试直接调用，也必须能被 CLI 脚本复用。

依赖方向固定为单向：

    ui  →  workers  →  core
"""
