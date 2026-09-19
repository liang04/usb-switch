# -*- coding: utf-8 -*-
"""USB-Switch 本地桥接服务 —— CLI 启动器。

服务本体在 `usbswitch.core.bridge_server`（**库形态**）。GUI 也是直接 import
它在后台线程里跑 —— 那边不能 spawn 子进程，因为 PyInstaller 打包后
``sys.executable`` 是 exe 自身，``exe bridge.py`` 只会再开一个 GUI 实例。

本文件只保留「独立命令行启动」这一种用法，供 README 里的手工流程与调试使用：

    python bridge.py [端口]        默认 8737

对外接口（与 GUI 内嵌版本、以及 Linux 端的 linux_bridge.py 完全一致）：

    GET  /status   -> {"ok":true, "drives":["F"]}
    POST /eject    -> {"ok":true, "ejected":"F"}
                      {"ok":true, "warning":"未检测到 U 盘"}
                      {"ok":false, "error":"..."}

安全语义：只要本机上还存在可移动 U 盘，就必须弹出成功才返回 ok=true；
弹出失败一律返回 ok=false，调用方据此中止切换，不会动 VBUS。

停止: 关闭窗口或 Ctrl+C
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

# 允许在源码树里直接运行（未执行 pip install -e . 时）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from usbswitch.core.bridge_server import DEFAULT_PORT, BridgeServer  # noqa: E402


def main(argv: list[str]) -> int:
    try:
        port = int(argv[0]) if argv else DEFAULT_PORT
    except ValueError:
        print(f"端口必须是数字: {argv[0]!r}")
        return 64

    server = BridgeServer(port)
    try:
        server.start()
    except Exception as exc:  # noqa: BLE001 —— CLI 直接把原因打出来就够了
        print(f"启动失败: {exc}")
        return 1

    print(f"USB-Switch 桥接服务已启动: {server.base_url}")
    print("GUI 与网页控制面板的安全切换依赖本服务，使用期间请勿关闭本窗口。")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\n正在停止 …")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
