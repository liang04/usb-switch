"""端到端验证：真实配置下装配 MainWindow，检查安全弹出锁的派生状态。

铁律：USBSWITCH_DATA_DIR 必须指向临时目录 —— 否则 shutdown() 会把内存配置
落盘，覆盖 %APPDATA% 下用户已配好的远端主机（2026-09-18 真实发生过一次）。
所以这里先**读出**真实配置，再把数据目录重定向到临时目录。
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

tmp = tempfile.mkdtemp(prefix="usbswitch-e2e-")

from usbswitch.core import config as config_module  # noqa: E402
from usbswitch.core.models import Host  # noqa: E402

real_path = config_module.config_file()
print("真实配置:", real_path, "存在" if real_path.exists() else "（不存在，用默认配置）")
config = config_module.load(real_path)

# 加载完真实配置之后再重定向 —— 落盘写临时目录，绝不碰用户的配置
os.environ["USBSWITCH_DATA_DIR"] = tmp

from PySide6.QtWidgets import QApplication  # noqa: E402

from usbswitch.ui.main_window import MainWindow  # noqa: E402

app = QApplication.instance() or QApplication([])
window = MainWindow(config)

print("\n--- 角色 ---")
print("Host A kind:", config.bridges[Host.A].kind.value)
print("Host B kind:", config.bridges[Host.B].kind.value)
print("remote_host():", config.remote_host())
print("local_bridge_port:", config.local_bridge_port)

print("\n--- 安全弹出锁 ---")
print("Host A 锁（本机桥接未启动）:", window._eject_lock_for(Host.A))
print("Host B 锁（远程已配置）:", window._eject_lock_for(Host.B))

print("\n--- 分区摘要 ---")
for key in ("bridge", "remote", "options"):
    print(f"{key}: {window.section(key).summary_text()}  [{window.section(key).chip_text()}]")

window.resize(760, 760)
window.show()
app.processEvents()
shot = os.path.join(tmp, "options.png")
window.section("options").set_expanded(True)
app.processEvents()
window.grab().save(shot)
print("\n截图:", shot)

window.shutdown()
print("已退出，临时目录:", tmp)
sys.exit(0)
