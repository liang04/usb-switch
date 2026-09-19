#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 exe 图标（``src/usbswitch/resources/app.ico``）。

为什么需要这一步
----------------

界面里的图标全是**运行时用 QPainter 画的**（见 ``ui/icons.py``），项目因此
不带任何二进制资源、也没有打包资源路径的麻烦。但有一个例外绕不过去：
**exe 自身的图标必须在构建期嵌入**，操作系统读的是 PE 资源段，
运行时画不出来。

所以这里把 ``ui/icons.py`` 的同一份绘制代码渲染成多尺寸 ICO —— 保持
「图标只有一个来源」，改绘制代码后重跑本脚本即可。

手工拼 ICO 容器而不是依赖某个图像库：ICO 允许直接内嵌 PNG（Vista 以后都支持），
容器格式只有 6 + 16×N 字节的头部，比多引一个依赖划算得多。

用法::

    python scripts/make_icon.py            # 写入默认位置
    python scripts/make_icon.py --out x.ico
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

# 必须在导入 Qt 之前设置：无显示器环境（CI）也要能跑
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_OUT = REPO_ROOT / "src" / "usbswitch" / "resources" / "app.ico"

#: Windows 会按 DPI 与场景挑最合适的一档，多给几档避免缩放模糊
SIZES = (16, 24, 32, 48, 64, 128, 256)

ICO_HEADER = struct.Struct("<HHH")          # reserved, type, count
ICO_ENTRY = struct.Struct("<BBBBHHII")      # w, h, colors, reserved, planes, bpp, size, offset


def render_png(size: int) -> bytes:
    """用界面的绘制代码渲染一张 PNG。"""
    from PySide6.QtCore import QBuffer, QByteArray
    from PySide6.QtWidgets import QApplication

    from usbswitch.ui.icons import app_icon

    QApplication.instance() or QApplication([])

    pixmap = app_icon(size=size).pixmap(size, size)
    if pixmap.isNull():
        raise RuntimeError(f"{size}px 图标渲染失败")

    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QBuffer.WriteOnly)
    if not pixmap.save(buffer, "PNG"):
        raise RuntimeError(f"{size}px 图标无法编码为 PNG")
    buffer.close()
    return bytes(payload)


def build_ico(images: dict[int, bytes]) -> bytes:
    """把若干 PNG 拼成 ICO 容器。"""
    count = len(images)
    header = ICO_HEADER.pack(0, 1, count)  # type=1 表示图标

    offset = ICO_HEADER.size + ICO_ENTRY.size * count
    entries = bytearray()
    payloads = bytearray()

    for size, data in sorted(images.items()):
        if not 0 < size <= 256:
            raise ValueError(f"ICO 单边尺寸必须在 1-256 之间：{size}")
        entries += ICO_ENTRY.pack(
            size % 256,      # 256 要写成 0
            size % 256,
            0,               # 调色板颜色数：真彩色填 0
            0,               # 保留位
            1,               # 色彩平面
            32,              # 位深
            len(data),
            offset,
        )
        payloads += data
        offset += len(data)

    return bytes(header + entries + payloads)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 exe 图标")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出路径")
    args = parser.parse_args(argv)

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)

    images = {size: render_png(size) for size in SIZES}
    for size, data in sorted(images.items()):
        print(f"  渲染 {size:>3}px → {len(data):>6} 字节")

    blob = build_ico(images)
    target.write_bytes(blob)
    print(f"已写入 {target}（{len(blob)} 字节，{len(images)} 档尺寸）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
