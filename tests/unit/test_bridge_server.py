"""core.bridge_server 单元测试 —— HTTP 契约与生命周期。

这个服务是安全弹出互锁的执行端：它返回 ``ok=false`` 时编排层必须硬中止切换。
因此契约本身要当成 API 来测。
"""

from __future__ import annotations

import socket

import pytest

from usbswitch.core import http_json
from usbswitch.core.bridge_server import BridgeServer
from usbswitch.core.errors import BridgeError, BridgePortInUseError, BridgeUnreachableError


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _make_server(**kwargs) -> BridgeServer:
    return BridgeServer(
        _free_port(),
        status_provider=kwargs.pop("status_provider", lambda: {"ok": True, "drives": ["F"]}),
        eject_provider=kwargs.pop("eject_provider", lambda: {"ok": True, "ejected": "F"}),
        **kwargs,
    )


@pytest.fixture()
def server():
    instance = _make_server()
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


# --------------------------------------------------------------------------- #
# 契约
# --------------------------------------------------------------------------- #


def test_status_returns_provider_payload(server: BridgeServer):
    payload = http_json.get_json(f"{server.base_url}/status")
    assert payload == {"ok": True, "drives": ["F"]}


def test_eject_returns_provider_payload(server: BridgeServer):
    payload = http_json.post_json(f"{server.base_url}/eject")
    assert payload == {"ok": True, "ejected": "F"}


def test_unknown_path_returns_404(server: BridgeServer):
    with pytest.raises(BridgeError):
        http_json.get_json(f"{server.base_url}/nope")


def test_port_probe_uses_bind_not_connect():
    """回归测试：端口预检必须**真的 bind 一次**。

    原实现用 connect 判断「有没有人在监听」，而别的进程可能绑定了端口却没在监听
    （或者绑在 0.0.0.0 而我们想绑 127.0.0.1，那不是同一把锁）。这种情形下预检
    放行、真正的 bind 才报错 —— 用户看到的失败来自起完服务之后。

    这里用「偷偷占住端口」来模拟：先 bind 住，再构造服务，预检必须当场拦下。
    """
    port = _free_port()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", port))
    # 刻意**不**调用 listen()：绑定但未监听，正是 connect 探不出来的情形
    try:
        with pytest.raises(BridgePortInUseError) as excinfo:
            BridgeServer(port).start()
    finally:
        holder.close()

    assert "无法绑定" in excinfo.value.message


def test_wsaaccess_hint_points_at_exclusive_holders():
    """回归测试：10013 与 10048 的排查方向不同，提示不能混为一谈。

    实测本机 Clash Verge 绑了 ``0.0.0.0:8737`` 且带 SO_EXCLUSIVEADDRUSE，
    于是绑 ``127.0.0.1:8737`` 得到的是 WSAEACCES(10013)「访问被拒」，
    不是 WSAEADDRINUSE(10048)「已被占用」。
    """
    from usbswitch.core.bridge_server import _bind_failure_hint

    exc = OSError(10013, "拒绝访问")
    exc.winerror = 10013

    hint = _bind_failure_hint(8737, exc)

    assert "10013" in hint
    assert "netsh" in hint, "保留区间是 10013 的常见成因之一，应给出排查命令"
    assert "findstr" in hint, "要给出查持有者的命令"


def test_wsaaddrinuse_hint_points_at_occupancy():
    from usbswitch.core.bridge_server import _bind_failure_hint

    exc = OSError(10048, "已被占用")
    exc.winerror = 10048

    assert "已被占用" in _bind_failure_hint(8737, exc)


def test_eject_failure_is_reported_as_ok_false():
    """弹出失败必须以 ok=false 返回，调用方据此中止切换。"""
    def failing() -> dict:
        raise RuntimeError("E: 被占用")

    instance = _make_server(eject_provider=failing)
    instance.start()
    try:
        payload = http_json.post_json(f"{instance.base_url}/eject")
        assert payload["ok"] is False
        assert "占用" in payload["error"]
    finally:
        instance.stop()


def test_status_failure_is_reported_as_ok_false():
    def failing() -> dict:
        raise RuntimeError("枚举失败")

    instance = _make_server(status_provider=failing)
    instance.start()
    try:
        with pytest.raises(BridgeError):
            http_json.get_json(f"{instance.base_url}/status")
    finally:
        instance.stop()


def test_cors_headers_present():
    """网页控制面板来自不同源，缺了 CORS 头它就用不了。"""
    instance = _make_server()
    instance.start()
    try:
        import urllib.request

        with urllib.request.urlopen(f"{instance.base_url}/status", timeout=5) as response:
            assert response.headers.get("Access-Control-Allow-Origin") == "*"
    finally:
        instance.stop()


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #


def test_start_is_idempotent():
    instance = _make_server()
    instance.start()
    try:
        instance.start()  # 不应抛异常、也不应起第二个监听
        assert http_json.get_json(f"{instance.base_url}/status")["ok"] is True
    finally:
        instance.stop()


def test_stop_is_idempotent():
    instance = _make_server()
    instance.start()
    instance.stop()
    instance.stop()


def test_port_is_released_after_stop():
    instance = _make_server()
    port = instance.port
    instance.start()
    instance.stop()

    with socket.socket() as probe:
        probe.settimeout(1.0)
        assert probe.connect_ex(("127.0.0.1", port)) != 0, "端口没有释放"


def test_occupied_port_raises_before_starting():
    first = _make_server()
    first.start()
    try:
        second = BridgeServer(first.port)
        with pytest.raises(BridgePortInUseError) as excinfo:
            second.start()
        assert str(first.port) in excinfo.value.message
    finally:
        first.stop()


def test_stopped_server_is_unreachable(server: BridgeServer):
    base = server.base_url
    server.stop()
    with pytest.raises(BridgeUnreachableError):
        http_json.get_json(f"{base}/status")


def test_is_reachable_helper(server: BridgeServer):
    assert http_json.is_reachable(server.base_url) is True
    assert http_json.is_reachable("http://127.0.0.1:1") is False
