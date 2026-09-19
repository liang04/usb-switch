# -*- coding: utf-8 -*-
"""USB-Switch 安全切换脚本（先弹出 U 盘，成功后才切换）

用法:
    python usb_switch_safe.py a      弹出 U 盘后切换到 Host A
    python usb_switch_safe.py b      弹出 U 盘后切换到 Host B
    python usb_switch_safe.py x      弹出 U 盘后断开全部连接
    python usb_switch_safe.py list   只列出检测到的可移动磁盘（不执行任何操作）
    python usb_switch_safe.py a --no-eject   跳过弹出步骤直接切换（不推荐）

安全逻辑:
    1. 找到可移动 U 盘的盘符
    2. 调用 Windows "弹出" 命令
    3. 轮询确认盘符已经消失
    4. 以上全部成功才发送 BLE 切换命令；任何一步失败都直接中止

依赖: pip install bleak
"""

import asyncio
import sys
from pathlib import Path

# 磁盘检测与安全弹出统一转发到 usbswitch.core.disk。
#
# 原实现每次调用都要 spawn 一个 PowerShell 进程（实测单次约 6.6 秒），
# 既拖慢轮询，又会让弹出误判失败。core.disk 用 ctypes 直调 Win32（毫秒级），
# 弹出则走「锁定 + 卸载卷」的原生序列。这里保留同名函数，调用方无感。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from usbswitch.core import disk as _disk  # noqa: E402
from usbswitch.core.errors import (  # noqa: E402
    EjectError,
    MultipleRemovableDrivesError,
    NoRemovableDriveError,
    UsbSwitchError,
)

# bleak 只在真正需要 BLE 切换时才导入，避免被无关依赖拖累。
try:
    from bleak import BleakClient, BleakScanner
    _BLEAK_IMPORT_ERROR = None
except ImportError as e:
    BleakClient = BleakScanner = None
    _BLEAK_IMPORT_ERROR = e

# 如果电脑上会同时插多个可移动磁盘，在这里固定盘符，例如 "E"
# 保持 None 表示自动检测（仅当检测到恰好一个可移动磁盘时才继续）
TARGET_DRIVE = None

DEVICE_NAME = "USB-Switch"
SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


def list_removable_drives() -> list:
    """返回所有可移动磁盘的盘符列表，如 ['E']。毫秒级。"""
    return _disk.removable_drives()


def resolve_target_drive() -> str:
    try:
        return _disk.resolve_target(TARGET_DRIVE)
    except MultipleRemovableDrivesError as exc:
        raise RuntimeError(f"{exc.message}。请修改脚本顶部的 TARGET_DRIVE 固定盘符") from exc
    except NoRemovableDriveError as exc:
        raise RuntimeError(exc.message) from exc


def eject_drive(letter: str) -> None:
    print(f"正在弹出 {letter}: 盘 ...")
    try:
        _disk.eject(letter)
    except EjectError as exc:
        raise RuntimeError(str(exc)) from exc
    print(f"{letter}: 盘已安全弹出。")


async def ble_switch(cmd: str) -> bool:
    if _BLEAK_IMPORT_ERROR is not None:
        raise RuntimeError(
            f"缺少 BLE 依赖 bleak（{_BLEAK_IMPORT_ERROR}）。"
            "切换蓝牙需要它，请先执行: pip install bleak"
        )
    print(f"正在连接 {DEVICE_NAME} ...")
    device = await BleakScanner.find_device_by_filter(
        lambda d, adv: SERVICE_UUID in [u.lower() for u in adv.service_uuids]
        or (d.name is not None and d.name == DEVICE_NAME),
        timeout=15,
    )
    if device is None:
        raise RuntimeError("未找到 USB-Switch 蓝牙设备")

    replies = []

    def on_notify(_, data: bytearray):
        text = data.decode("utf-8", errors="replace")
        replies.append(text)
        print(f"设备回复: {text}")

    async with BleakClient(device, winrt={"use_cached_services": False}) as client:
        try:
            await client.start_notify(TX_UUID, on_notify)
        except Exception as e:
            raise RuntimeError(
                f"服务发现失败（{type(e).__name__}）。"
                "这是 Windows GATT 缓存损坏的典型症状，请关闭再打开蓝牙开关后重试。"
            )
        await client.write_gatt_char(RX_UUID, cmd.encode(), response=False)
        for _ in range(50):
            if replies:
                break
            await asyncio.sleep(0.1)
        await client.stop_notify(TX_UUID)

    if not replies:
        raise RuntimeError("设备无回复，切换结果未知")
    return any(r.startswith("OK") for r in replies)


async def main(cmd: str, do_eject: bool) -> int:
    if do_eject:
        letter = resolve_target_drive()
        eject_drive(letter)          # 失败会抛异常，不会执行到下面
    else:
        print("警告: 已跳过安全弹出步骤！")

    ok = await ble_switch(cmd)
    if ok:
        print("切换完成。")
        return 0
    print("切换失败: 目标主机不可用或设备拒绝。", file=sys.stderr)
    return 3


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_eject = "--no-eject" in sys.argv

    if not args or args[0].lower() not in ("a", "b", "x", "list"):
        print(__doc__)
        sys.exit(64)

    if args[0].lower() == "list":
        drives = list_removable_drives()
        print("当前可移动磁盘:", drives if drives else "（无）")
        sys.exit(0)

    try:
        sys.exit(asyncio.run(main(args[0].lower(), do_eject=not no_eject)))
    except (RuntimeError, UsbSwitchError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
