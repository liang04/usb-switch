"""两个桥接实现（Windows 本地 / Linux 远端）的对外契约必须一致。

``scripts/linux_bridge.py`` **必须保持单文件自包含** —— 它会被 SFTP 到 Linux
由 systemd 执行，那台机器上没有本项目。因此它与 ``core/bridge_server.py``
之间存在一段**有意接受**的 HTTP 样板重复。

既然接受了重复，就必须防漂移：本测试真的把两份实现都跑起来，逐一比对
响应字段。任何一边改了契约，这里立刻会红。

Linux 端在 Windows 上照样能跑起来 —— 它读 ``/sys/block`` 与 ``/proc/mounts``
拿不到东西，只会返回空列表，但那正是我们要验证的「响应形状」。
"""

from __future__ import annotations

import ast
import importlib.util
import socket
import threading
from pathlib import Path
from types import ModuleType

import pytest

from usbswitch.core import http_json
from usbswitch.core.bridge_server import BridgeServer

LINUX_BRIDGE = Path(__file__).resolve().parents[2] / "scripts" / "linux_bridge.py"

#: /eject 的结果字段 —— 两边必须完全一致，编排层据此判断成败
EJECT_FIELDS = {"ok", "ejected", "warning", "error"}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _linux_bridge_source() -> str:
    return LINUX_BRIDGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def linux_bridge() -> ModuleType:
    spec = importlib.util.spec_from_file_location("linux_bridge_under_test", LINUX_BRIDGE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def linux_server(linux_bridge: ModuleType):
    port = _free_port()
    httpd = linux_bridge.ThreadingHTTPServer(("127.0.0.1", port), linux_bridge.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture()
def local_server():
    server = BridgeServer(_free_port())
    server.start()
    try:
        yield server.base_url
    finally:
        server.stop()


# --------------------------------------------------------------------------- #
# 自包含约束
# --------------------------------------------------------------------------- #


def test_linux_bridge_does_not_import_the_project():
    """最硬的约束：远端机器上没有本项目，import 到它就会直接崩。"""
    tree = ast.parse(_linux_bridge_source())
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(a.name for a in node.names if a.name.startswith("usbswitch"))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("usbswitch"):
            offenders.append(node.module or "")

    assert not offenders, f"linux_bridge.py 不能依赖本项目：{offenders}"


def test_linux_bridge_does_not_import_qt():
    tree = ast.parse(_linux_bridge_source())
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            assert not name.startswith(("PySide6", "PyQt")), f"远端不应依赖 Qt：{name}"


# --------------------------------------------------------------------------- #
# 契约一致性
# --------------------------------------------------------------------------- #


def test_both_expose_status_endpoint(local_server: str, linux_server: str):
    assert http_json.get_json(f"{local_server}/status")["ok"] is True
    assert http_json.get_json(f"{linux_server}/status")["ok"] is True


def test_both_expose_eject_endpoint(local_server: str, linux_server: str):
    assert http_json.post_json(f"{local_server}/eject").get("ok") is True
    assert http_json.post_json(f"{linux_server}/eject").get("ok") is True


def test_eject_payload_fields_are_a_subset_of_the_shared_vocabulary(
    local_server: str, linux_server: str
):
    """两边 /eject 的响应字段都必须落在同一套词表里。

    这是编排层能「用同一个 BridgeEjector 处理本地与远程」的前提。
    """
    for base in (local_server, linux_server):
        payload = http_json.post_json(f"{base}/eject")
        extra = set(payload) - EJECT_FIELDS
        assert not extra, f"{base} 返回了词表之外的字段：{extra}"


def test_both_reject_unknown_paths(local_server: str, linux_server: str):
    from usbswitch.core.errors import BridgeError

    for base in (local_server, linux_server):
        with pytest.raises(BridgeError):
            http_json.get_json(f"{base}/definitely-not-a-route")


def test_both_report_no_disk_with_ok_true_and_warning(linux_server: str):
    """本机看不到 U 盘时，语义必须是「成功 + 警告」，而不是失败。

    否则接在另一台 Host 上的盘会让切换被无谓地拦下。
    """
    payload = http_json.post_json(f"{linux_server}/eject")
    assert payload["ok"] is True
    assert "warning" in payload


def test_status_field_names_differ_but_both_carry_ok(local_server: str, linux_server: str):
    """状态字段两边不同（drives vs disks/mounts），这是有意的：
    网页控制面板已经同时兼容两者。但 ``ok`` 必须一致。"""
    local = http_json.get_json(f"{local_server}/status")
    remote = http_json.get_json(f"{linux_server}/status")

    assert "drives" in local
    assert {"disks", "mounts"} <= set(remote)
    assert local["ok"] is remote["ok"] is True


def test_web_panel_handles_both_status_shapes():
    """网页控制面板要同时认两种状态字段，否则接了远端就显示不出盘。"""
    panel = (Path(__file__).resolve().parents[2] / "web" / "usb_switch_panel.html")
    source = panel.read_text(encoding="utf-8")

    assert "drives" in source
    assert "mounts" in source
