# -*- coding: utf-8 -*-
"""USB-Switch 本地桥接服务

常驻在本机 127.0.0.1:8737，给网页控制面板提供"安全弹出 U 盘"能力。
网页本身无权弹出 U 盘，因此在发送蓝牙切换命令前先请求本服务：

    GET  http://127.0.0.1:8737/status   -> {"ok":true, "drives":["G"]}
    POST http://127.0.0.1:8737/eject    -> {"ok":true, "ejected":"G"}
                                           {"ok":true, "warning":"未检测到 U 盘"}
                                           {"ok":false, "error":"..."}

安全语义：只要本机上还存在可移动 U 盘，就必须弹出成功才返回 ok=true；
弹出失败一律返回 ok=false，网页据此中止切换，不会动 VBUS。

启动:  python bridge.py        (或双击 start_bridge.bat)
停止:  关闭窗口或 Ctrl+C
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usb_switch_safe import eject_drive, list_removable_drives, resolve_target_drive

HOST = "127.0.0.1"
PORT = 8737


def do_eject() -> dict:
    drives = list_removable_drives()
    if not drives:
        # 本机看不到 U 盘：可能已被弹出，也可能 U 盘当前接在另一台 Host 上
        # （另一台 Host 上的弹出只能在那台电脑上执行，这里无法代办）
        return {"ok": True, "warning": "本机未检测到 U 盘（已弹出或连接在另一台 Host 上）"}
    letter = resolve_target_drive()
    eject_drive(letter)
    return {"ok": True, "ejected": letter}


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
                self._send(200, {"ok": True, "drives": list_removable_drives()})
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
        print(f"[bridge] {fmt % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"USB-Switch 桥接服务已启动: http://{HOST}:{PORT}")
    print("网页控制面板的安全切换功能依赖本服务，使用期间请勿关闭本窗口。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
