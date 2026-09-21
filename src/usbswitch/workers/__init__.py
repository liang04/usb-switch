"""Qt 适配层。

职责只有一句话：**把 core 的异步/阻塞调用翻译成 Qt 的 Signal/Slot**。

这一层不塞进 core 是因为 core 必须零 Qt 依赖；不塞进 ui 是因为多个面板
需要共用同一个 worker，塞进去会让 main_window 膨胀成上帝对象。
"""

from .ble_worker import BleWorker
from .bridge_worker import BridgeWorker
from .remote_worker import RemoteWorker
from .tunnel_worker import TunnelWorker

__all__ = ["BleWorker", "BridgeWorker", "RemoteWorker", "TunnelWorker"]
