"""core.disk 单元测试。

重点断言三件事：
1. 性能 —— removable_drives() 必须远快于旧实现（单次约 6600 ms）；
2. 安全 —— 盘符会被拼进设备路径，非法输入必须被拒绝；
3. 弹盘判据 —— 成败来自 DeviceIoControl 的返回值，不再依赖轮询计时。
"""

from __future__ import annotations

import sys
import time

import pytest

from usbswitch.core import disk
from usbswitch.core.errors import DiskError, EjectError, MultipleRemovableDrivesError, NoRemovableDriveError

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="磁盘弹出仅支持 Windows")


# --------------------------------------------------------------------------- #
# 性能
# --------------------------------------------------------------------------- #


def test_removable_drives_within_budget():
    """核心性能断言：单次调用必须远低于旧的 PowerShell 实现。"""
    disk.removable_drives()  # 预热

    rounds = 20
    start = time.perf_counter()
    for _ in range(rounds):
        disk.removable_drives()
    elapsed_ms = (time.perf_counter() - start) * 1000 / rounds

    assert elapsed_ms < 50, f"removable_drives() 平均 {elapsed_ms:.1f} ms/次，超出 50 ms 预算"


# --------------------------------------------------------------------------- #
# 盘符校验
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("e", "E"), ("E", "E"), ("E:", "E"), (" E:\\ ", "E"), ("g:", "G")],
)
def test_normalize_letter_accepts_common_forms(raw: str, expected: str):
    """规范化只做语法处理，不判断该盘符是否真是可移动磁盘。

    `C:` 会被规范化为 `C` —— 这是正确的：它是不是合法的**目标**属于语义校验，
    由 resolve_target() / eject() 通过对比 removable_drives() 来完成。
    """
    assert disk._normalize_letter(raw) == expected


@pytest.mark.parametrize(
    "bad",
    ["", "  ", "AB", "1", "E;", "E'", "$(calc)", "E:\\Windows", "*", "-"],
)
def test_normalize_letter_rejects_bad_input(bad: str):
    """盘符会被拼进设备路径，这里必须挡死注入。"""
    with pytest.raises(DiskError):
        disk._normalize_letter(bad)


# --------------------------------------------------------------------------- #
# 目标判定
# --------------------------------------------------------------------------- #


def test_resolve_target_uses_configured_letter(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(disk, "removable_drives", lambda: ["E", "F"])
    assert disk.resolve_target("f") == "F"


def test_resolve_target_rejects_missing_configured_letter(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(disk, "removable_drives", lambda: ["E"])
    with pytest.raises(NoRemovableDriveError):
        disk.resolve_target("F")


def test_resolve_target_requires_exactly_one_when_auto(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(disk, "removable_drives", lambda: [])
    with pytest.raises(NoRemovableDriveError):
        disk.resolve_target()

    monkeypatch.setattr(disk, "removable_drives", lambda: ["E", "F"])
    with pytest.raises(MultipleRemovableDrivesError):
        disk.resolve_target()

    monkeypatch.setattr(disk, "removable_drives", lambda: ["E"])
    assert disk.resolve_target() == "E"


# --------------------------------------------------------------------------- #
# 弹出
# --------------------------------------------------------------------------- #


def test_eject_returns_letter_and_uses_dismount(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(disk, "removable_drives", lambda: ["E"])
    calls: list[str] = []

    def fake_dismount(letter: str) -> bool:
        calls.append(letter)
        return True

    monkeypatch.setattr(disk, "_dismount_volume", fake_dismount)

    assert disk.eject("E") == "E"
    assert calls == ["E"], "eject() 必须走卸载路径"


def test_eject_accepts_auto_detection(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(disk, "removable_drives", lambda: ["E"])
    monkeypatch.setattr(disk, "_dismount_volume", lambda letter: True)
    assert disk.eject() == "E"


def test_eject_propagates_dismount_failure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(disk, "removable_drives", lambda: ["E"])

    def failing(letter: str) -> bool:
        raise EjectError("无法卸载卷 E:")

    monkeypatch.setattr(disk, "_dismount_volume", failing)
    with pytest.raises(EjectError):
        disk.eject("E")


def test_eject_rejects_letter_that_is_not_removable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(disk, "removable_drives", lambda: ["F"])
    with pytest.raises(NoRemovableDriveError):
        disk.eject("E")


def test_eject_forces_dismount_when_lock_would_fail(monkeypatch: pytest.MonkeyPatch):
    """回归测试：锁定失败**不应**导致提前放弃卸载。

    最初实现里 LOCK 失败就 return，结果永远弹不出盘 —— 而 MSDN 明确
    「卷即使无法锁定也可以被卸载」，chkdsk /x 走的正是这条。
    """
    monkeypatch.setattr(disk, "removable_drives", lambda: ["E"])

    def force_dismount(letter: str) -> bool:
        return False  # False = 不是干净锁定，但卸载照样成功

    monkeypatch.setattr(disk, "_dismount_volume", force_dismount)
    assert disk.eject("E") == "E"


# --------------------------------------------------------------------------- #
# 卸载重试
# --------------------------------------------------------------------------- #


def test_dismount_retries_transient_open_failure(monkeypatch: pytest.MonkeyPatch):
    """回归测试：卷刚上电时打开句柄会瞬时失败，必须重试。

    真机上踩到：切到 Host A 后 1 秒内直接开卷必然拿到 err=5，
    单次失败并不代表这张盘弹不出来。
    """
    attempts: list[str] = []

    def flaky(letter: str) -> bool:
        attempts.append(letter)
        if len(attempts) < 3:
            raise EjectError("无法打开卷 E:（Win32 错误 5）")
        return True

    monkeypatch.setattr(disk, "_dismount_once", flaky)

    assert disk._dismount_volume("E", attempts=5, delay=0.0) is True
    assert len(attempts) == 3, "应当重试到成功为止"


def test_dismount_gives_up_after_all_attempts(monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []

    def always_fail(letter: str) -> bool:
        calls.append(1)
        raise EjectError("无法卸载卷 E:")

    monkeypatch.setattr(disk, "_dismount_once", always_fail)

    with pytest.raises(EjectError, match="无法卸载卷"):
        disk._dismount_volume("E", attempts=3, delay=0.0)

    assert len(calls) == 3


def test_dismount_returns_immediately_on_success(monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []

    def ok(letter: str) -> bool:
        calls.append(1)
        return True

    monkeypatch.setattr(disk, "_dismount_once", ok)

    assert disk._dismount_volume("E", attempts=5, delay=0.0) is True
    assert len(calls) == 1, "成功后不应继续重试"


# --------------------------------------------------------------------------- #
# 卸载失败的提示
# --------------------------------------------------------------------------- #


def test_dismount_failure_hint_points_to_chkdsk_when_unhealthy(monkeypatch: pytest.MonkeyPatch):
    """卷健康异常是最容易被误诊成「被占用」的情况 —— 提示必须指向 chkdsk。

    真机上就踩到了：Get-Volume 报 health=Warning、FSCTL_LOCK_VOLUME 报 err=5，
    看起来完全像被进程占用，实际是脏位被置位。
    """
    monkeypatch.setattr(disk, "volume_health", lambda letter: "Warning")

    hint = disk._dismount_failure_hint("F")
    assert "chkdsk F: /f /x" in hint
    assert "Warning" in hint


def test_dismount_failure_hint_points_to_occupancy_when_healthy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(disk, "volume_health", lambda letter: "Healthy")

    hint = disk._dismount_failure_hint("F")
    assert "占用" in hint
    assert "VBUS 保持不变" in hint


def test_dismount_failure_hint_handles_unknown_health(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(disk, "volume_health", lambda letter: None)
    assert "占用" in disk._dismount_failure_hint("F")


# --------------------------------------------------------------------------- #
# 卷健康状态
# --------------------------------------------------------------------------- #


def test_volume_health_of_system_drive_is_healthy():
    """真实调用：系统盘应报告 Healthy（顺带验证 PowerShell 通道可用）。"""
    assert disk.volume_health("C") == "Healthy"


def test_volume_health_returns_none_for_missing_drive():
    assert disk.volume_health("Q") is None


def test_volume_health_rejects_bad_letter():
    with pytest.raises(DiskError):
        disk.volume_health("$(calc)")


def test_is_volume_dirty_derives_from_health(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(disk, "volume_health", lambda letter: "Healthy")
    assert disk.is_volume_dirty("E") is False

    monkeypatch.setattr(disk, "volume_health", lambda letter: "Warning")
    assert disk.is_volume_dirty("E") is True

    monkeypatch.setattr(disk, "volume_health", lambda letter: None)
    assert disk.is_volume_dirty("E") is None


# --------------------------------------------------------------------------- #
# ioctl 常量
# --------------------------------------------------------------------------- #


def test_fsctl_constants_match_ctl_code_formula():
    """CTL_CODE(9, Function, METHOD_BUFFERED, FILE_ANY_ACCESS) = (9 << 16) | (Function << 2)。

    算错不会报错，只会返回 ERROR_INVALID_FUNCTION —— 必踩且难查。
    """
    assert disk.FSCTL_LOCK_VOLUME == (9 << 16) | (6 << 2)
    assert disk.FSCTL_DISMOUNT_VOLUME == (9 << 16) | (8 << 2)


def test_volume_is_opened_read_only():
    """回归测试：开卷**只请求读权限**。

    这是踩过最贵的一个坑 —— 早期用 GENERIC_READ|GENERIC_WRITE 开 removable 卷会
    恒定拿到 ERROR_ACCESS_DENIED，而我误判成「卷刚挂载被扫描占用」，把重试预算
    从 6 次加到 20 次（8 秒），方向完全错了：等多久都不会成功。
    """
    import inspect

    source = inspect.getsource(disk._dismount_once)
    assert "GENERIC_READ," in source or "GENERIC_READ\n" in source
    assert "GENERIC_READ | GENERIC_WRITE" not in source
    assert not hasattr(disk, "GENERIC_WRITE"), "不该再保留写权限常量"
