# -*- coding: utf-8 -*-
"""USB-Switch 蓝牙控制脚本（Windows）

用法:
    python usb_switch.py a        切换到 Host A
    python usb_switch.py b        切换到 Host B
    python usb_switch.py x        断开 U 盘（全部关闭）
    python usb_switch.py s        查询当前状态

依赖: pip install bleak
"""

import asyncio
import sys

from bleak import BleakClient, BleakScanner

DEVICE_NAME = "USB-Switch"
SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # 写入命令
TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # 接收状态


async def main(cmd: str) -> int:
    print(f"正在扫描设备 {DEVICE_NAME} ...")
    # 按服务 UUID 匹配（比名称可靠，Windows 被动扫描可能拿不到设备名）
    device = await BleakScanner.find_device_by_filter(
        lambda d, adv: SERVICE_UUID in [u.lower() for u in adv.service_uuids]
        or (d.name is not None and d.name == DEVICE_NAME),
        timeout=15,
    )
    if device is None:
        print("错误: 未找到设备。请确认 ESP32-C3 已上电，且电脑蓝牙已打开。")
        return 1

    print(f"已找到: {device.address}，正在连接...")

    replies = []

    def on_notify(_, data: bytearray):
        text = data.decode("utf-8", errors="replace")
        replies.append(text)
        print(f"设备回复: {text}")

    async with BleakClient(device, winrt={"use_cached_services": False}) as client:
        print("已连接。")
        try:
            await client.start_notify(TX_UUID, on_notify)
        except Exception as e:
            print(f"错误: 服务发现失败（{type(e).__name__}）。")
            print("这是 Windows GATT 缓存损坏的典型症状，请关闭再打开蓝牙开关后重试。")
            return 4
        await client.write_gatt_char(RX_UUID, cmd.encode("utf-8"), response=False)
        # 等待固件执行切换时序（最长约 1.5 秒）并回传结果
        for _ in range(50):
            if replies:
                break
            await asyncio.sleep(0.1)
        await client.stop_notify(TX_UUID)

    if not replies:
        print("警告: 未收到设备回复，命令可能未生效。")
        return 2
    return 0 if any(r.startswith("OK") or r.startswith("STATUS") for r in replies) else 3


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1].lower() not in ("a", "b", "x", "s"):
        print(__doc__)
        sys.exit(64)
    sys.exit(asyncio.run(main(sys.argv[1].lower())))
