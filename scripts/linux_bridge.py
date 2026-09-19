#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USB-Switch Linux 端桥接服务（部署在 Host B）

与 Windows 端 bridge.py 对等：监听 HTTP，为网页控制面板提供
"安全卸载 U 盘"能力。只处理通过 USB 接入的可移动磁盘（/sys/block/sd*/removable=1）。

    GET  /status -> {"ok":true, "disks":["/dev/sda"], "mounts":[["/dev/sda1","/media/u"]]}
    POST /eject  -> {"ok":true, "ejected":"/dev/sda"}
                    {"ok":true, "warning":"未检测到 U 盘"}
                    {"ok":false, "error":"..."}

安全语义：只要 USB 可移动磁盘仍有挂载点，就必须全部卸载成功才返回 ok=true。

启动:  python3 usb_switch_bridge.py [端口，默认 8738]
建议:  用 systemd 或 nohup 常驻
"""

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8738
HOST = "0.0.0.0"

#: 实际监听端口。默认值可被 __main__ 里的命令行参数覆盖。
#:
#: 注意：**不要**在模块级解析 ``sys.argv`` —— 那样这个文件就无法被 import
#: （测试要把它和 Windows 端实现跑起来比对契约），而且 import 一个模块
#: 不应该有「读命令行」这种副作用。
PORT = DEFAULT_PORT


def usb_disks() -> list:
    """返回所有 USB 可移动磁盘，如 ['/dev/sda']"""
    disks = []
    try:
        names = os.listdir("/sys/block")
    except FileNotFoundError:
        return disks
    for name in names:
        if not name.startswith("sd"):
            continue
        try:
            with open(f"/sys/block/{name}/removable") as f:
                if f.read().strip() == "1":
                    disks.append("/dev/" + name)
        except (FileNotFoundError, PermissionError):
            continue
    return sorted(disks)


def mounts_of(disks: list) -> list:
    """返回这些磁盘的挂载点列表，如 [('/dev/sda1', '/media/u')]"""
    mounts = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and any(parts[0].startswith(d) for d in disks):
                    mounts.append((parts[0], parts[1]))
    except FileNotFoundError:
        pass
    return mounts


def umount(mnt: str) -> str:
    """卸载挂载点，失败返回错误信息，成功返回空字符串"""
    r = subprocess.run(["umount", mnt], capture_output=True, text=True)
    if r.returncode == 0:
        return ""
    # 普通用户无权卸载时尝试免密 sudo（需在 sudoers 中配置）
    r2 = subprocess.run(["sudo", "-n", "umount", mnt],
                        capture_output=True, text=True)
    if r2.returncode == 0:
        return ""
    return (r.stderr or r2.stderr or "unknown error").strip()


def do_eject() -> dict:
    disks = usb_disks()
    if not disks:
        # 本机看不到 U 盘：可能已被弹出，或当前连接在另一台 Host 上
        return {"ok": True, "warning": "本机未检测到 U 盘（已弹出或连接在另一台 Host 上）"}

    subprocess.run(["sync"])
    errors = []
    for dev, mnt in mounts_of(disks):
        err = umount(mnt)
        if err:
            errors.append(f"{mnt}: {err}")
    if errors:
        return {"ok": False, "error": "卸载失败: " + "; ".join(errors)}

    remaining = mounts_of(disks)
    if remaining:
        return {"ok": False, "error": f"卸载后仍存在挂载点: {remaining}"}
    return {"ok": True, "ejected": ",".join(disks)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        if self.path == "/status":
            try:
                disks = usb_disks()
                self._send(200, {"ok": True, "disks": disks,
                                 "mounts": mounts_of(disks)})
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)})
        else:
            self._send(404, {"ok": False, "error": "unknown path"})

    def do_POST(self):
        if self.path == "/eject":
            try:
                self._send(200, do_eject())
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
        else:
            self._send(404, {"ok": False, "error": "unknown path"})

    def log_message(self, fmt, *args):
        print(f"[bridge] {fmt % args}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        try:
            PORT = int(sys.argv[1])
        except ValueError:
            print(f"端口必须是数字: {sys.argv[1]!r}")
            raise SystemExit(64)

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"USB-Switch Linux 桥接服务已启动: http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
