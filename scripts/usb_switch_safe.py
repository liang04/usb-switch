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
import os
import subprocess
import sys
import time

from bleak import BleakClient, BleakScanner

POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
if not os.path.exists(POWERSHELL):
    POWERSHELL = "powershell"  # 回退到 PATH 查找

# 如果电脑上会同时插多个可移动磁盘，在这里固定盘符，例如 "E"
# 保持 None 表示自动检测（仅当检测到恰好一个可移动磁盘时才继续）
TARGET_DRIVE = None

DEVICE_NAME = "USB-Switch"
SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

EJECT_TIMEOUT = 20  # 等待弹出完成的秒数


def run_ps(script: str) -> str:
    r = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=30,
    )
    return (r.stdout or "").strip()


def list_removable_drives() -> list:
    """返回所有可移动磁盘的盘符列表，如 ['E']"""
    out = run_ps(
        "(Get-Volume | Where-Object {$_.DriveType -eq 'Removable' "
        "-and $_.DriveLetter} | Select-Object -ExpandProperty DriveLetter) -join ','"
    )
    return [d for d in out.split(",") if d] if out else []


def resolve_target_drive() -> str:
    drives = list_removable_drives()
    if TARGET_DRIVE:
        letter = TARGET_DRIVE.upper()
        if letter not in drives:
            raise RuntimeError(f"配置的盘符 {letter}: 当前不存在或不是可移动磁盘")
        return letter
    if len(drives) == 0:
        raise RuntimeError("未检测到可移动 U 盘（可能已被弹出）")
    if len(drives) > 1:
        raise RuntimeError(
            f"检测到多个可移动磁盘 {drives}，无法确定目标。"
            "请修改脚本顶部的 TARGET_DRIVE 固定盘符"
        )
    return drives[0]


def eject_drive(letter: str) -> None:
    print(f"正在弹出 {letter}: 盘 ...")
    run_ps(
        f"$s = New-Object -ComObject Shell.Application;"
        f"$s.Namespace(17).ParseName('{letter}:').InvokeVerb('Eject')"
    )
    # 轮询确认盘符消失
    deadline = time.time() + EJECT_TIMEOUT
    while time.time() < deadline:
        if letter not in list_removable_drives():
            print(f"{letter}: 盘已安全弹出。")
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"等待 {EJECT_TIMEOUT} 秒后 {letter}: 盘仍未弹出。"
        "可能有程序正在占用 U 盘（资源管理器窗口、杀软扫描等）。"
        "已中止切换，VBUS 保持不变。"
    )


async def ble_switch(cmd: str) -> bool:
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

    async with BleakClient(device) as client:
        await client.start_notify(TX_UUID, on_notify)
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
    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
