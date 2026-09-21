"""极简 JSON over HTTP 客户端。

桥接服务的对外契约就是两个 JSON 端点，为此引入 ``requests`` 不值当 ——
而且少一个依赖就少一份打包负担。用 stdlib 的 ``urllib`` 足够。

注意区分两类失败：

- **不可达**（连不上 / 超时）→ :class:`BridgeUnreachableError`
- **可达但出错**（HTTP 错误码 / 非法 JSON）→ :class:`BridgeError`

调用方据此给出不同的提示：前者让用户检查服务是否在跑，后者是服务自身的问题。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .errors import BridgeError, BridgeUnreachableError


def _request(url: str, *, method: str, timeout: float) -> dict:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise BridgeError(
            f"桥接服务返回 HTTP {exc.code}",
            hint=f"请求 {url} 被拒绝",
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BridgeUnreachableError(
            f"无法连接桥接服务 {url}",
            hint=f"{exc}",
        ) from exc

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError(f"桥接服务返回了非法 JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise BridgeError("桥接服务返回的 JSON 不是对象")
    return data


def get_json(url: str, *, timeout: float = 2.0) -> dict:
    return _request(url, method="GET", timeout=timeout)


def post_json(url: str, *, timeout: float = 30.0) -> dict:
    return _request(url, method="POST", timeout=timeout)


def is_reachable(base_url: str, *, timeout: float = 2.0) -> bool:
    """探活：能取到 ``/status`` 且返回 ``ok``。"""
    try:
        return bool(get_json(f"{base_url}/status", timeout=timeout).get("ok"))
    except BridgeError:
        return False
