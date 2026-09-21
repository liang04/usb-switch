"""主窗口的分区面板。

每个面板只负责「摆放控件 + 转发信号」，不持有任何业务逻辑 ——
真正的逻辑在 `usbswitch.core`，线程胶水在 `usbswitch.workers`。
"""
