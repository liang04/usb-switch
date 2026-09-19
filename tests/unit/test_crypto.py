"""core.crypto 单元测试 —— DPAPI 加解密往返。"""

from __future__ import annotations

import sys

import pytest

from usbswitch.core import crypto

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅 Windows 可用")


def test_roundtrip_returns_original():
    token = crypto.protect("hunter2")
    assert crypto.is_encrypted(token)
    assert "hunter2" not in token
    assert crypto.unprotect(token) == "hunter2"


def test_empty_input_stays_empty():
    """空串不加密，避免往配置里塞无意义的密文。"""
    assert crypto.protect("") == ""
    assert crypto.unprotect("") == ""


def test_non_ascii_roundtrip():
    secret = "密码=测试🔒\n带换行"
    assert crypto.unprotect(crypto.protect(secret)) == secret


def test_ciphertext_differs_each_time():
    """DPAPI 每次加密结果不同（含随机盐），但都能解回同一明文。"""
    a = crypto.protect("same-input")
    b = crypto.protect("same-input")
    assert a != b
    assert crypto.unprotect(a) == crypto.unprotect(b) == "same-input"


def test_corrupt_blob_raises_oserror():
    with pytest.raises(OSError):
        crypto.unprotect("dpapi:bm90LWEtcmVhbC1kcGFwaS1ibG9i")
