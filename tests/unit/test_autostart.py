"""core.autostart 单元测试。

会**真的读写注册表** —— 但把值名换成测试专用的，并在夹具里清理干净，
不碰用户真实的自启项。
"""

from __future__ import annotations

import sys

import pytest

from usbswitch.core import autostart

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="注册表自启仅 Windows")

TEST_VALUE_NAME = "USBSwitchConsolePytest"


@pytest.fixture()
def clean_value(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(autostart, "VALUE_NAME", TEST_VALUE_NAME)
    autostart.disable()  # 保险：先把可能残留的测试项清掉
    yield TEST_VALUE_NAME
    autostart.disable()


# --------------------------------------------------------------------------- #
# 命令行构造（纯函数，不碰注册表）
# --------------------------------------------------------------------------- #


def test_launch_command_quotes_executable():
    """路径必须加引号：不加时 Windows 会在第一个空格处截断注册表值，
    ``C:\\Program Files\\...`` 必踩。"""
    assert autostart.launch_command().startswith('"')


def test_launch_command_always_requests_minimized():
    assert autostart.STARTUP_FLAG in autostart.launch_command()


def test_launch_command_in_dev_mode_uses_module_entry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    command = autostart.launch_command()
    assert "-m usbswitch" in command
    assert sys.executable in command


def test_launch_command_when_frozen_points_at_exe(monkeypatch: pytest.MonkeyPatch):
    """打包后 sys.executable 就是 exe 自身，不能再拼 -m usbswitch。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    command = autostart.launch_command()
    assert "-m usbswitch" not in command
    assert sys.executable in command


# --------------------------------------------------------------------------- #
# 注册表读写
# --------------------------------------------------------------------------- #


def test_disabled_by_default(clean_value: str):
    assert autostart.is_enabled() is False
    assert autostart.current_command() is None


def test_enable_then_disable_roundtrip(clean_value: str):
    written = autostart.enable()
    assert written == autostart.launch_command()
    assert autostart.is_enabled() is True
    assert autostart.current_command() == written

    autostart.disable()
    assert autostart.is_enabled() is False
    assert autostart.current_command() is None


def test_disable_is_idempotent(clean_value: str):
    autostart.disable()
    autostart.disable()  # 本来就不存在也不该抛


def test_enable_twice_keeps_single_value(clean_value: str):
    autostart.enable()
    autostart.enable()
    assert autostart.current_command() == autostart.launch_command()


def test_enable_accepts_explicit_command(clean_value: str):
    autostart.enable('"C:\\path with space\\app.exe" --minimized')
    assert autostart.current_command() == '"C:\\path with space\\app.exe" --minimized'


def test_sync_to_aligns_registry_with_desired_state(clean_value: str):
    assert autostart.sync_to(True) is True
    assert autostart.is_enabled() is True

    assert autostart.sync_to(False) is False
    assert autostart.is_enabled() is False

    # 已经是目标状态时不做多余写入
    assert autostart.sync_to(False) is False
