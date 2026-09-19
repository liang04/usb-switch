"""Windows DPAPI 加解密 —— ctypes 直调 crypt32，不依赖 pywin32。

只用于加密需要落盘的凭据（SSH 密码、私钥口令）。用 DPAPI 而不是自己写
AES 的理由：密钥由 Windows 按当前用户托管，我们不需要管理任何主密码。

非 Windows 平台降级为「base64 + 明确前缀」，仅供开发与 CI 使用，
并在测试中显式断言，避免误以为有加密。
"""

from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes

_IS_WINDOWS = sys.platform == "win32"

#: 密文前缀，用于区分加密方式（未来若要换实现，靠它做版本兼容）
DPAPI_PREFIX = "dpapi:"
PLAIN_PREFIX = "plain:"

#: DPAPI 的描述串，便于在凭据管理器中识别来源
_DESCRIPTION = "usbswitch"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


if _IS_WINDOWS:
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),  # pDataIn
        wintypes.LPCWSTR,           # szDataDescr
        ctypes.POINTER(_DataBlob),  # pOptionalEntropy
        ctypes.c_void_p,            # pvReserved
        ctypes.c_void_p,            # pPromptStruct
        wintypes.DWORD,             # dwFlags
        ctypes.POINTER(_DataBlob),  # pDataOut
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL

    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),  # ppszDataDescr，传 None 则不解码描述
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL


def _to_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    """构造输入 blob。

    返回的 buffer 必须由调用方持有到 API 调用结束 —— 否则会被 GC 回收，
    导致 ctypes 读到已释放内存（表现为随机崩溃或乱码）。
    """
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


def _from_blob(blob: _DataBlob) -> bytes:
    """读取输出 blob 并释放其内存（DPAPI 用 LocalAlloc 分配）。"""
    try:
        if not blob.pbData or blob.cbData == 0:
            return b""
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        if blob.pbData:
            _kernel32.LocalFree(ctypes.cast(blob.pbData, wintypes.HLOCAL))


def is_available() -> bool:
    """当前平台是否具备真正的加密能力。"""
    return _IS_WINDOWS


def is_encrypted(token: str) -> bool:
    """该字符串是否为密文（而非空串或开发态明文）。"""
    return token.startswith(DPAPI_PREFIX)


def protect(plaintext: str) -> str:
    """加密明文，返回可直接写入 JSON 的字符串。空串原样返回空串。"""
    if not plaintext:
        return ""
    if not _IS_WINDOWS:
        return PLAIN_PREFIX + base64.b64encode(plaintext.encode("utf-8")).decode("ascii")

    blob_in, _keepalive = _to_blob(plaintext.encode("utf-8"))
    blob_out = _DataBlob()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in), _DESCRIPTION, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData 失败")
    return DPAPI_PREFIX + base64.b64encode(_from_blob(blob_out)).decode("ascii")


def unprotect(token: str) -> str:
    """解密。空串返回空串；密文损坏或属于其他 Windows 用户时抛 OSError。"""
    if not token:
        return ""
    if token.startswith(PLAIN_PREFIX):
        return base64.b64decode(token[len(PLAIN_PREFIX):]).decode("utf-8")

    payload = token[len(DPAPI_PREFIX):] if token.startswith(DPAPI_PREFIX) else token
    if not _IS_WINDOWS:
        raise RuntimeError("该凭据由 Windows DPAPI 加密，无法在当前平台解密")

    blob_in, _keepalive = _to_blob(base64.b64decode(payload))
    blob_out = _DataBlob()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError(
            ctypes.get_last_error(),
            "CryptUnprotectData 失败（凭据可能由其他 Windows 用户加密，或已损坏）",
        )
    return _from_blob(blob_out).decode("utf-8")
