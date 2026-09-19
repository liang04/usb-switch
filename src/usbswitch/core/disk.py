"""磁盘检测与安全弹出。

**本模块存在的首要理由是性能。** 原实现（``scripts/usb_switch_safe.py``）的
``list_removable_drives()`` 每次调用都要 spawn 一个 PowerShell 进程，实测单次
约 **6.6 秒**。后果有两个：

1. GUI 只要按秒级轮询就会卡死界面；
2. ``eject_drive()`` 的 20 s 超时（0.5 s 间隔）实际只轮询得了约 3 次，
   会在 U 盘刚弹出时误判失败。

因此全部改为 ``ctypes`` 直调 Win32，零子进程。

关于弹出方式的历史
------------------

最初这里走的是 Windows Shell 的 ``InvokeVerb('Eject')``，判断成败靠「轮询盘符
是否消失」。这条路在真机上被证明有两个硬伤：

1. **动词名是本地化的**。中文系统上叫「弹出(&J)」，写死 ``'Eject'`` 不会报错，
   只是什么都不做，最终表现为莫名其妙的超时；
2. **成败判据不成立**。这个 U 盘的桥片不支持 ``IOCTL_STORAGE_EJECT_MEDIA``
   （``ERROR_NOT_SUPPORTED``），盘符根本不会消失 —— 判据从一开始就是错的。

现在改为「锁定 + 卸载」的原生序列，与「安全删除硬件」内核路径一致：

1. 打开卷 —— **只请求 ``GENERIC_READ``**（见下）；
2. ``FSCTL_LOCK_VOLUME`` —— 没有任何进程占用时才能成功；
3. ``FSCTL_DISMOUNT_VOLUME`` —— **即使锁定失败也可以卸载**（MSDN 明确如此，
   ``chkdsk /x`` 用的就是这条），它自身负责刷写并作废卷缓存。

成败由 ``DeviceIoControl`` 的返回值**确定性**给出，不再依赖轮询与计时。

关于访问权限（踩过的最贵的一个坑）
----------------------------------

一开始用的是 ``GENERIC_READ | GENERIC_WRITE``。真机时间轴实验显示，对
removable 卷这个组合**几乎恒定**返回 ``ERROR_ACCESS_DENIED``，而
``GENERIC_READ`` 单独使用恒定成功 —— 且 ``FSCTL_DISMOUNT_VOLUME``
本来就只需要读权限。

当时我把这误判成「卷刚挂载、被系统扫描占用」，于是把重试预算从 6 次一路
加到 20 次（8 秒）。**方向完全错了**：无论等多久都不会成功。
堆重试之前，先确认失败原因到底是什么。
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes

from .errors import (
    DiskError,
    EjectError,
    MultipleRemovableDrivesError,
    NoRemovableDriveError,
)

log = logging.getLogger(__name__)

DRIVE_REMOVABLE = 2
_DRIVE_COUNT = 26
_LETTER_RE = re.compile(r"^[A-Za-z]$")

_POWERSHELL_FALLBACK = "powershell"
_POWERSHELL_SYSTEM = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

_IS_WINDOWS = sys.platform == "win32"

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value

#: CTL_CODE(FILE_DEVICE_FILE_SYSTEM=9, Function, METHOD_BUFFERED, FILE_ANY_ACCESS)
#: = (9 << 16) | (Function << 2)。写错不会报错，只返回 ERROR_INVALID_FUNCTION。
FSCTL_LOCK_VOLUME = 0x00090018      # Function 6
FSCTL_DISMOUNT_VOLUME = 0x00090020  # Function 8

ERROR_ACCESS_DENIED = 5

if _IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.GetLogicalDrives.argtypes = []
    _k32.GetLogicalDrives.restype = wintypes.DWORD
    _k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _k32.GetDriveTypeW.restype = wintypes.UINT

    # 必须显式声明 argtypes，否则 64 位下 HANDLE 会被当成 int 截断
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    _k32.DeviceIoControl.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL


# --------------------------------------------------------------------------- #
# 检测
# --------------------------------------------------------------------------- #


def removable_drives() -> list[str]:
    """返回本机可移动磁盘的盘符列表，如 ``['E']``。

    毫秒级 —— 这是整个 GUI 里被调用最频繁的底层函数。
    """
    if not _IS_WINDOWS:
        raise DiskError("磁盘弹出仅支持 Windows")

    mask = _k32.GetLogicalDrives()
    if mask == 0:
        raise DiskError(
            "GetLogicalDrives 调用失败",
            hint=f"Win32 错误码 {ctypes.get_last_error()}",
        )

    drives: list[str] = []
    for index in range(_DRIVE_COUNT):
        if not mask & (1 << index):
            continue
        letter = chr(ord("A") + index)
        if _k32.GetDriveTypeW(f"{letter}:\\") == DRIVE_REMOVABLE:
            drives.append(letter)
    return drives


def _normalize_letter(value: str) -> str:
    """校验并规范化盘符。

    必须严格校验：盘符会被拼进设备路径，放任任意字符串等于开了注入的口子。
    """
    letter = value.strip().rstrip(":\\").strip()
    if not _LETTER_RE.match(letter):
        raise DiskError(f"非法盘符: {value!r}", hint="盘符必须是单个字母 A-Z")
    return letter.upper()


def resolve_target(configured: str | None = None) -> str:
    """确定要操作的目标盘符。

    Args:
        configured: 配置中固定的盘符。为 None 时自动判定，
            并且**只在本机恰好有一个可移动磁盘时才继续** —— 宁可让用户
            去配置里指定，也不要赌错盘。
    """
    drives = removable_drives()

    if configured:
        letter = _normalize_letter(configured)
        if letter not in drives:
            raise NoRemovableDriveError(
                f"配置的盘符 {letter}: 当前不存在或不是可移动磁盘",
                hint=f"当前可移动磁盘: {drives or '（无）'}",
            )
        return letter

    if not drives:
        raise NoRemovableDriveError("未检测到可移动 U 盘（可能已被弹出）")
    if len(drives) > 1:
        raise MultipleRemovableDrivesError(
            f"检测到多个可移动磁盘 {drives}，无法确定目标",
            hint="请在设置中固定目标盘符",
        )
    return drives[0]


# --------------------------------------------------------------------------- #
# 卷健康状态
# --------------------------------------------------------------------------- #

HEALTHY = "Healthy"

_HEALTH_SCRIPT = (
    "$v = Get-Volume -DriveLetter __LETTER__ -ErrorAction SilentlyContinue;"
    "if ($v) { $v.HealthStatus.ToString() } else { '' }"
)


def volume_health(letter: str) -> str | None:
    """返回卷的健康状态：``'Healthy'`` / ``'Warning'`` / ``'Unhealthy'`` …；查询失败返回 None。

    用 PowerShell 的 ``Get-Volume`` 而不是 ``FSCTL_IS_VOLUME_DIRTY``：前者返回的是
    **.NET 枚举成员名**，不随系统语言变化；后者的 ioctl 码试了多个候选都返回
    ``ERROR_INVALID_FUNCTION``，而盲扫未知控制码在用户机器上不可接受。

    只在弹盘失败时调用，因此多一次进程开销可以接受。
    """
    if not _IS_WINDOWS:
        return None

    target = _normalize_letter(letter)
    try:
        result = subprocess.run(
            [
                _POWERSHELL_SYSTEM if _powershell_available() else _POWERSHELL_FALLBACK,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _HEALTH_SCRIPT.replace("__LETTER__", target),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def _powershell_available() -> bool:
    return os.path.exists(_POWERSHELL_SYSTEM)


def is_volume_dirty(letter: str) -> bool | None:
    """卷是否处于「Windows 会拒绝正常弹出」的状态。无法判断时返回 None。

    典型来源是脏位（dirty bit）：上一次没有正常卸载，例如在挂载状态下直接被
    切断了供电。这种卷 ``FSCTL_LOCK_VOLUME`` 会返回 ERROR_ACCESS_DENIED，
    看起来很像「被程序占用」，实际只要 ``chkdsk /f`` 就能解决。
    """
    health = volume_health(letter)
    if health is None:
        return None
    return health != HEALTHY


# --------------------------------------------------------------------------- #
# 弹出
# --------------------------------------------------------------------------- #


#: 卷刚挂载（或刚从强制卸载中恢复）时，卸载可能瞬时失败，因此带重试。
#:
#: 注意重试**不是**用来掩盖打开方式错误的 —— 早期版本用
#: ``GENERIC_READ|GENERIC_WRITE`` 开卷会恒定拿到 err=5，当时我把重试预算
#: 一路加到 8 秒也没用。真正修好之后（改用只读打开），重试只剩兜底意义。
_DISMOUNT_ATTEMPTS = 6
_DISMOUNT_RETRY_DELAY = 0.4


def _device_io_control(handle, code: int) -> tuple[bool, int]:
    """返回 (是否成功, Win32 错误码)。"""
    returned = wintypes.DWORD(0)
    ok = _k32.DeviceIoControl(handle, code, None, 0, None, 0, ctypes.byref(returned), None)
    return bool(ok), ctypes.get_last_error()


def _dismount_once(letter: str) -> bool:
    """单次尝试：打开卷 → 锁定 → 卸载。

    返回是否是「干净锁定」（False 表示走了强制卸载）。

    Raises:
        EjectError: 打不开卷，或卸载失败。
    """
    path = f"\\\\.\\{letter}:"
    # 只请求 GENERIC_READ，**不要**加 GENERIC_WRITE。
    #
    # 实测（真机时间轴实验）：对 removable 卷开 ``GENERIC_READ|GENERIC_WRITE``
    # 基本恒定返回 ERROR_ACCESS_DENIED，而 ``GENERIC_READ`` 恒定成功；
    # 而 FSCTL_DISMOUNT_VOLUME 只需要读权限就足够。
    # 多加 WRITE 不但没用，还会让整个弹盘操作在绝大多数情况下直接失败。
    handle = _k32.CreateFileW(
        path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == _INVALID_HANDLE or not handle:
        error = ctypes.get_last_error()
        raise EjectError(
            f"无法打开卷 {letter}:（Win32 错误 {error}）",
            hint="卷可能尚未完成挂载，或正被系统独占",
        )

    try:
        locked, lock_error = _device_io_control(handle, FSCTL_LOCK_VOLUME)
        if not locked:
            # 锁失败只说明有进程占用（真机上 explorer 常年持有句柄，所以这是常态）。
            # 这不代表不能卸载 —— MSDN 明确「卷即使无法锁定也可以被卸载」，
            # chkdsk /x 走的也是这条。
            detail = "被其他进程占用" if lock_error == ERROR_ACCESS_DENIED else f"错误 {lock_error}"
            log.info("%s: 卷%s，改用强制卸载（原有打开的句柄会失效）", letter, detail)

        # 不需要额外的 FlushFileBuffers：FSCTL_DISMOUNT_VOLUME 自身会刷写并作废
        # 卷缓存。真机验证过：多次强制卸载后 fsutil dirty 仍显示未置脏，
        # 说明文件系统是被干净关闭的。
        dismounted, dismount_error = _device_io_control(handle, FSCTL_DISMOUNT_VOLUME)
        if not dismounted:
            raise EjectError(
                f"无法卸载卷 {letter}:（Win32 错误 {dismount_error}）",
                hint=_dismount_failure_hint(letter),
            )
        return locked
    finally:
        _k32.CloseHandle(handle)


def _dismount_volume(
    letter: str,
    *,
    attempts: int = _DISMOUNT_ATTEMPTS,
    delay: float = _DISMOUNT_RETRY_DELAY,
) -> bool:
    """带重试的卸载。返回是否是干净锁定。

    重试是必要的：U 盘刚上电时卷还在稳定过程中，第一次打开句柄就可能拿到
    ``ERROR_ACCESS_DENIED``（实测切换主机后 1 秒内必然如此）。单次失败不代表
    这张盘弹不出来。
    """
    last: EjectError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _dismount_once(letter)
        except EjectError as exc:
            last = exc
            if attempt < attempts:
                if attempt == 1:
                    # 只提示一次，避免刷屏；用户看到这条就知道在等什么
                    log.info(
                        "%s: 卷暂时不可用，正在等待（最多 %.1f 秒）…",
                        letter, attempts * delay,
                    )
                time.sleep(delay)

    assert last is not None
    log.error("卸载 %s: 重试 %d 次仍失败（%s）", letter, attempts, last.message)
    raise last


def _dismount_failure_hint(letter: str) -> str:
    """卸载失败时给出**可操作**的原因，而不是一句笼统的「可能被占用」。"""
    health = volume_health(letter)
    if health is not None and health != HEALTHY:
        return (
            f"{letter}: 卷健康状态为 {health}，通常意味着上一次没有正常卸载"
            "（脏位被置位）。请用管理员命令行执行 "
            f"chkdsk {letter}: /f /x 修复后重试。"
        )
    return (
        "可能有程序正在占用该卷。请关闭资源管理器窗口与正在读写 U 盘的程序后重试；"
        "已中止本次操作，VBUS 保持不变。"
    )


def eject(letter: str | None = None) -> str:
    """安全弹出指定的可移动磁盘，返回被弹出的盘符。

    实现为「锁定 + 卸载」，成败是确定性的，不依赖轮询。

    Args:
        letter: 目标盘符。为 None 时自动判定（见 :func:`resolve_target`）。

    Raises:
        NoRemovableDriveError: 目标盘不存在。
        EjectError: 打不开卷或卸载失败。
    """
    target = resolve_target() if letter is None else _normalize_letter(letter)

    if target not in removable_drives():
        raise NoRemovableDriveError(
            f"{target}: 当前不是可移动磁盘",
            hint="可能已被弹出，或该盘符已被其他设备占用",
        )

    # 走的是干净锁定还是强制卸载，由 _dismount_once 内部按情况记录 ——
    # 真机上 explorer 常年持有句柄，强制卸载是常态而非异常，不该每次都告警。
    _dismount_volume(target)
    log.info("已安全弹出 %s:", target)
    return target


# --------------------------------------------------------------------------- #
# 手工验证入口： python -m usbswitch.core.disk [--eject X]
# --------------------------------------------------------------------------- #


def _main(argv: list[str]) -> int:
    from time import perf_counter

    if "--eject" in argv:
        index = argv.index("--eject")
        target = argv[index + 1] if index + 1 < len(argv) else None
        began = perf_counter()
        letter = eject(target)
        print(f"已弹出 {letter}:，耗时 {perf_counter() - began:.2f}s")
        return 0

    drives = removable_drives()
    print(f"可移动磁盘: {drives or '（无）'}")
    for letter in drives:
        health = volume_health(letter)
        state = {None: "无法查询"}.get(health, health)
        suffix = "" if health in (None, HEALTHY) else "  ← 需 chkdsk /f /x"
        print(f"  {letter}:  卷健康 = {state}{suffix}")

    rounds = 20
    began = perf_counter()
    for _ in range(rounds):
        removable_drives()
    elapsed_ms = (perf_counter() - began) * 1000 / rounds

    print(f"removable_drives() {rounds} 次平均 {elapsed_ms:.2f} ms/次（旧实现约 6600 ms）")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
