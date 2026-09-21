"""``core.ssh_tunnel`` 的测试。

**不起真的 SSH 连接**，但也不做「假到底」的 mock：这里用一个假的 SSH 客户端
返回**真的 socketpair**，对端则接一个**真的桥接服务**。于是 accept 循环、
双向中继、连接关闭这些真正容易写错的部分都被真实地跑了一遍。

（唯一被替换掉的是「字节怎么穿过 SSH 通道」这一跳 —— 那不是我们的代码。）
"""

from __future__ import annotations

import socket
import threading
import time

import paramiko
import pytest

from usbswitch.core import http_json
from usbswitch.core.bridge_server import BridgeServer
from usbswitch.core.errors import TunnelError, UsbSwitchError
from usbswitch.core.models import AuthMethod, BridgeRunState, RemoteBridgeConfig
from usbswitch.core.ssh_tunnel import SshTunnel


# --------------------------------------------------------------------------- #
# 夹具与替身
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    """测试用的单向搬运。与产品实现一样做**半关闭**。

    直接 close 会在接收缓冲还有数据时发 RST，对端数据被丢掉 ——
    测试脚手架要是犯了同样的错，就会把产品的 bug 掩盖成「偶发失败」。
    """
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _close_quietly(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


def _relay_pair(a: socket.socket, b: socket.socket) -> None:
    """把两个 socket 双向对接（测试里用来模拟「SSH 通道那一跳」）。"""
    threading.Thread(target=_pipe, args=(a, b), daemon=True).start()
    _pipe(b, a)


class _FakeTransport:
    def __init__(self, channel_factory) -> None:
        self._channel_factory = channel_factory

    def is_active(self) -> bool:
        return True

    def open_channel(self, kind, dest, src):  # noqa: ANN001
        return self._channel_factory(dest, src)


class _FakeClient:
    """够用的 SSHClient 替身；``open_channel`` 通过 socketpair 接到真实服务。"""

    def __init__(self, channel_factory, *, connect_error: BaseException | None = None) -> None:
        self.channel_factory = channel_factory
        self.connect_error = connect_error
        self.closed = False
        self.connected = False
        self.policy = None

    # -- SSHClient 接口 ---------------------------------------------------- #

    def load_host_keys(self, filename: str) -> None:
        pass

    def set_missing_host_key_policy(self, policy) -> None:
        self.policy = policy

    def connect(self, host: str, **kwargs) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    def save_host_keys(self, filename: str) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def get_transport(self) -> _FakeTransport:
        return _FakeTransport(self.channel_factory)


def _channel_to(dest_port: int):
    """返回一个 channel 工厂：每条通道都真实连到 ``127.0.0.1:dest_port``。"""

    def factory(dest, src):  # noqa: ANN001
        near, far = socket.socketpair()

        def bridge() -> None:
            try:
                upstream = socket.create_connection(dest, timeout=3)
            except OSError:
                _close_quietly(far)
                _close_quietly(near)
                return
            _relay_pair(far, upstream)
            _close_quietly(far)
            _close_quietly(upstream)

        threading.Thread(target=bridge, daemon=True).start()
        return near

    return factory


@pytest.fixture()
def remote_service():
    """充当「远端」的真实桥接服务。"""
    server = BridgeServer(
        _free_port(),
        status_provider=lambda: {"ok": True, "drives": ["/dev/sda"]},
        eject_provider=lambda: {"ok": True, "ejected": "/dev/sda"},
    )
    server.start()
    yield server
    server.stop()


def _config(bridge_port: int, **kwargs) -> RemoteBridgeConfig:
    return RemoteBridgeConfig(
        host="10.0.0.9",
        ssh_port=22,
        username="ubu",
        auth=AuthMethod.PASSWORD,
        password="secret",
        bridge_port=bridge_port,
        **kwargs,
    )


def _make_tunnel(config: RemoteBridgeConfig, client: _FakeClient, **kwargs) -> SshTunnel:
    return SshTunnel(
        config,
        client_factory=lambda: client,
        on_log=kwargs.pop("on_log", None),
        on_state=kwargs.pop("on_state", None),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 中继：真的把字节送过去
# --------------------------------------------------------------------------- #


def test_tunnel_relays_http_to_remote_service(remote_service, data_dir):
    """核心用例：经隧道访问，必须能拿到「远端」服务真实的响应。

    覆盖 accept 循环、双向中继、连接收尾 —— 这几处是最容易写错的地方。
    """
    config = _config(remote_service.port)
    client = _FakeClient(_channel_to(remote_service.port))
    tunnel = _make_tunnel(config, client)

    tunnel.start()
    try:
        assert tunnel.wait_ready(5.0), "隧道没有就绪"
        assert tunnel.local_port > 0

        payload = http_json.get_json(f"{tunnel.base_url}/status", timeout=5.0)

        assert payload == {"ok": True, "drives": ["/dev/sda"]}
    finally:
        tunnel.stop()


def test_tunnel_relays_post_body(remote_service, data_dir):
    """POST 也要能过 —— 安全弹出走的是 /eject，不能只测 GET。"""
    config = _config(remote_service.port)
    tunnel = _make_tunnel(config, _FakeClient(_channel_to(remote_service.port)))

    tunnel.start()
    try:
        assert tunnel.wait_ready(5.0)
        payload = http_json.post_json(f"{tunnel.base_url}/eject", timeout=5.0)
        assert payload == {"ok": True, "ejected": "/dev/sda"}
    finally:
        tunnel.stop()


def test_tunnel_does_not_truncate_large_response(data_dir):
    """回归测试：大响应体不能被截断。

    原实现里 `_pump` 收尾时直接 `close()` 两端。socket 接收缓冲里还有未读数据时
    `close()` 会发 RST 而不是 FIN，**对端尚未取走的数据会被丢掉**，
    表现为 ``http.client.IncompleteRead``。小响应在快机器上侥幸能过
    （所以它一开始是「偶发失败」），响应一大就稳定复现。

    200 KB 足以把内核缓冲撑满、逼出这个竞态。
    """
    blob = "x" * 200_000
    server = BridgeServer(_free_port(), status_provider=lambda: {"ok": True, "blob": blob})
    server.start()
    try:
        config = _config(server.port)
        tunnel = _make_tunnel(config, _FakeClient(_channel_to(server.port)))
        tunnel.start()
        try:
            assert tunnel.wait_ready(5.0)
            payload = http_json.get_json(f"{tunnel.base_url}/status", timeout=15.0)
            assert len(payload["blob"]) == len(blob), "响应体被截断了"
        finally:
            tunnel.stop()
    finally:
        server.stop()


def test_tunnel_serves_repeated_requests(remote_service, data_dir):
    """连续多次请求都要成功 —— 中继不能只活一条连接。"""
    config = _config(remote_service.port)
    tunnel = _make_tunnel(config, _FakeClient(_channel_to(remote_service.port)))

    tunnel.start()
    try:
        assert tunnel.wait_ready(5.0)
        for _ in range(5):
            assert http_json.get_json(f"{tunnel.base_url}/status", timeout=5.0)["ok"]
    finally:
        tunnel.stop()


# --------------------------------------------------------------------------- #
# 桥接地址：唯一来源
# --------------------------------------------------------------------------- #


def test_runtime_http_base_tracks_tunnel_lifecycle(remote_service, data_dir):
    """隧道的意义就在于「桥接地址只有一处来源」。

    ``http_base`` 必须跟着隧道起落切换 —— ejector、probe、安装验证三处都只读它，
    少了这个联动就会出现「状态显示在线、切换却连到被防火墙挡住的直连地址」。
    """
    config = _config(remote_service.port)
    assert config.http_base == f"http://10.0.0.9:{remote_service.port}"

    tunnel = _make_tunnel(config, _FakeClient(_channel_to(remote_service.port)))
    tunnel.start()
    try:
        assert tunnel.wait_ready(5.0)
        assert config.http_base == tunnel.base_url
        assert config.http_base.startswith("http://127.0.0.1:")
        assert config.direct_http_base == f"http://10.0.0.9:{remote_service.port}"
    finally:
        tunnel.stop()

    assert config.http_base == f"http://10.0.0.9:{remote_service.port}", "停止后没有还原"
    assert config.runtime_http_base == ""


def test_ejector_uses_tunnel_address(remote_service, data_dir):
    """编排层无需知情：它读 http_base，隧道一开就自动走隧道。"""
    from usbswitch.core.ejector import BridgeEjector
    from usbswitch.core.models import AppConfig, BridgeKind, Host

    app_config = AppConfig()
    app_config.bridges[Host.B].kind = BridgeKind.REMOTE
    app_config.bridges[Host.B].remote = _config(remote_service.port)

    tunnel = _make_tunnel(
        app_config.bridges[Host.B].remote, _FakeClient(_channel_to(remote_service.port))
    )
    tunnel.start()
    try:
        assert tunnel.wait_ready(5.0)

        endpoint = BridgeEjector(app_config).endpoint_for(Host.B)

        assert endpoint == tunnel.base_url
        # 而且真的能弹出（走的是「远端」服务的 /eject）
        BridgeEjector(app_config).eject_for(Host.B)
    finally:
        tunnel.stop()


# --------------------------------------------------------------------------- #
# 状态与失败
# --------------------------------------------------------------------------- #


def test_tunnel_reports_running_state(remote_service, data_dir):
    config = _config(remote_service.port)
    states: list[BridgeRunState] = []
    tunnel = _make_tunnel(
        config,
        _FakeClient(_channel_to(remote_service.port)),
        on_state=states.append,
    )

    tunnel.start()
    try:
        assert tunnel.wait_ready(5.0)
        assert tunnel.state is BridgeRunState.RUNNING
        assert BridgeRunState.RUNNING in states
    finally:
        tunnel.stop()

    assert tunnel.state is BridgeRunState.STOPPED


def test_tunnel_reports_error_on_auth_failure(data_dir):
    """认证失败要落到 ERROR，而且**不能**把桥接地址指向一个不存在的隧道。"""
    config = _config(8738)
    client = _FakeClient(
        _channel_to(8738),
        connect_error=paramiko.AuthenticationException("bad password"),
    )
    tunnel = _make_tunnel(config, client)

    tunnel.start()
    try:
        time.sleep(0.3)  # 让监督线程跑一轮
        assert tunnel.state is BridgeRunState.ERROR
        assert "认证失败" in tunnel.last_error
        assert config.http_base == config.direct_http_base, "隧道没通却改了桥接地址"
    finally:
        tunnel.stop()


def test_tunnel_retries_after_connection_failure(remote_service, data_dir):
    """第一次连不上要自动重试，而不是一直停在失败态。"""
    attempts: list[int] = []

    class FlakyClient(_FakeClient):
        def connect(self, host: str, **kwargs) -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise paramiko.AuthenticationException("第一次失败")

    config = _config(remote_service.port)
    flaky = FlakyClient(_channel_to(remote_service.port))
    tunnel = _make_tunnel(config, flaky)

    # 把退避压到几乎没有，免得测试等 3 秒
    import usbswitch.core.ssh_tunnel as st

    original = st.RECONNECT_DELAY
    st.RECONNECT_DELAY = 0.05
    try:
        tunnel.start()
        assert tunnel.wait_ready(5.0), "失败后没有重连"
        assert len(attempts) >= 2
    finally:
        st.RECONNECT_DELAY = original
        tunnel.stop()


# --------------------------------------------------------------------------- #
# 本地端口
# --------------------------------------------------------------------------- #


def test_local_port_is_auto_allocated_by_default(remote_service, data_dir):
    config = _config(remote_service.port, tunnel_local_port=0)
    tunnel = _make_tunnel(config, _FakeClient(_channel_to(remote_service.port)))

    tunnel.start()
    try:
        assert tunnel.local_port > 0
    finally:
        tunnel.stop()


def test_local_port_can_be_pinned(remote_service, data_dir):
    wanted = _free_port()
    config = _config(remote_service.port, tunnel_local_port=wanted)
    tunnel = _make_tunnel(config, _FakeClient(_channel_to(remote_service.port)))

    tunnel.start()
    try:
        assert tunnel.local_port == wanted
    finally:
        tunnel.stop()


def test_local_port_conflict_raises_tunnel_error(data_dir):
    """本地端口被占时要**当场**报错，而不是留个坏隧道在后面。"""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    taken = int(holder.getsockname()[1])

    config = _config(8738, tunnel_local_port=taken)
    tunnel = _make_tunnel(config, _FakeClient(_channel_to(8738)))
    try:
        with pytest.raises(TunnelError) as excinfo:
            tunnel.start()
        assert "0（自动）" in excinfo.value.hint
        assert tunnel.is_running is False
    finally:
        holder.close()
        tunnel.stop()


# --------------------------------------------------------------------------- #
# 收尾
# --------------------------------------------------------------------------- #


def test_stop_is_idempotent_and_releases_port(remote_service, data_dir):
    config = _config(remote_service.port, tunnel_local_port=0)
    tunnel = _make_tunnel(config, _FakeClient(_channel_to(remote_service.port)))

    tunnel.start()
    assert tunnel.wait_ready(5.0)
    port = tunnel.local_port

    tunnel.stop()
    tunnel.stop()

    assert tunnel.is_running is False
    assert tunnel.local_port == 0

    # 端口真的释放了：能重新绑上
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


def test_restart_reuses_tunnel_after_stop(remote_service, data_dir):
    config = _config(remote_service.port, tunnel_local_port=0)
    tunnel = _make_tunnel(config, _FakeClient(_channel_to(remote_service.port)))

    for _ in range(2):
        tunnel.start()
        try:
            assert tunnel.wait_ready(5.0)
            assert http_json.get_json(f"{tunnel.base_url}/status", timeout=5.0)["ok"]
        finally:
            tunnel.stop()


def test_start_twice_is_noop(remote_service, data_dir):
    config = _config(remote_service.port, tunnel_local_port=0)
    tunnel = _make_tunnel(config, _FakeClient(_channel_to(remote_service.port)))

    tunnel.start()
    try:
        assert tunnel.wait_ready(5.0)
        port = tunnel.local_port
        tunnel.start()  # 再来一次不该换端口，也不该起第二个监听
        assert tunnel.local_port == port
        assert http_json.get_json(f"{tunnel.base_url}/status", timeout=5.0)["ok"]
    finally:
        tunnel.stop()


def test_tunnel_failures_are_usbswitch_errors(data_dir):
    """隧道建不起来时抛的必须是领域异常，UI 才能给出中文提示。"""
    config = _config(8738)
    client = _FakeClient(_channel_to(8738), connect_error=OSError("boom"))

    tunnel = _make_tunnel(config, client)
    tunnel.start()
    try:
        time.sleep(0.3)
        assert tunnel.state is BridgeRunState.ERROR
        assert tunnel.last_error
    finally:
        tunnel.stop()

    assert issubclass(TunnelError, UsbSwitchError)
