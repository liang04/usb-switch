"""日志初始化：文件轮转 + 内存环形缓冲。

UI 的日志面板需要一次性读到最近 N 条，而不是自己去 tail 文件。
因此除了文件 handler 之外，再挂一个环形缓冲 handler 供 UI 直接读取。
"""

from __future__ import annotations

import logging
from collections import deque
from logging.handlers import RotatingFileHandler
from threading import Lock

from .paths import log_dir

LOGGER_NAME = "usbswitch"
LOGGER = logging.getLogger(LOGGER_NAME)

_FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_CONSOLE_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 5
_RING_CAPACITY = 2000


class RingBufferHandler(logging.Handler):
    """把最近若干条日志留在内存里，供 UI 读取与导出。"""

    def __init__(self, capacity: int = _RING_CAPACITY) -> None:
        super().__init__()
        self._records: deque[logging.LogRecord] = deque(maxlen=capacity)
        self._lock = Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            self._records.append(record)

    def snapshot(self) -> list[str]:
        """返回已格式化的日志行（副本）。"""
        with self._lock:
            records = list(self._records)
        return [self.format(record) for record in records]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


_ring = RingBufferHandler()
_configured = False


def get_ring_buffer() -> RingBufferHandler:
    return _ring


def setup(*, level: int = logging.INFO, console: bool = True) -> RingBufferHandler:
    """初始化日志。重复调用是幂等的。"""
    global _configured
    if _configured:
        return _ring

    LOGGER.setLevel(level)
    # 不要让日志冒泡到 root，否则会被调用方的 basicConfig 重复输出一次
    LOGGER.propagate = False

    file_handler = RotatingFileHandler(
        log_dir() / "usbswitch.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT))
    LOGGER.addHandler(file_handler)

    _ring.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt="%H:%M:%S"))
    LOGGER.addHandler(_ring)

    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt="%H:%M:%S"))
        LOGGER.addHandler(stream_handler)

    _configured = True
    return _ring
