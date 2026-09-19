"""pytest 全局夹具。"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把运行期数据目录重定向到临时目录，避免污染真实的 %APPDATA%。"""
    monkeypatch.setenv("USBSWITCH_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(scope="session")
def qapp():
    """全局唯一的 QApplication。

    session 作用域：Qt 不允许一个进程里有两个 QApplication，多个测试模块
    各自 ``QApplication([])`` 会直接崩。
    """
    import os

    # 必须在创建 QApplication **之前**设置，否则平台插件已选定
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def pick_free_port() -> int:
    """要一个当前空闲的本地端口。

    测试**不能假设 8737/8738 是空闲的**：开发机上这些通用端口常被代理 / VPN /
    安全软件占着（实测本机 Clash Verge 就绑了 ``0.0.0.0:8737``），
    于是「启动本地桥接」这类用例会在别人的机器配置上假失败。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture()
def free_port() -> int:
    return pick_free_port()
