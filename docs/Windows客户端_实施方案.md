# USB Switch Console — Windows 桌面客户端实施方案

> 目标：把当前散落在三处的手工操作（BLE 切换脚本 / 本地桥接服务 / 远程桥接服务）
> 收敛进一个 Windows 桌面应用，并提供状态可视化与安全互锁。

---

## 1. 范围界定

### 1.1 需求清单

| # | 需求 | 归属模块 |
|---|---|---|
| 1 | 扫描、连接 ESP32 | BLE Worker |
| 2 | 启动 / 停止 **本地** 桥接服务 | Bridge Manager · Local |
| 3 | 配置 **远程（Linux）** 连接：IP / SSH 端口 / 用户名 / 密码 | Config + Remote |
| 4 | 安装 / 启动 / 停止 / 卸载 **远程** 桥接服务 | Bridge Manager · Remote |
| 5 | 切换 U 盘（Host A / Host B / 断开） | Switch Orchestrator |
| 6 | 查看 / 刷新状态 | Status Poller |
| 7 | 启用 / 禁用安全弹出互锁（单个 Trigger 开关） | Safety Policy |
| 8 | **系统托盘常驻**，显示 / 隐藏主窗口 | Tray |
| 9 | 启用 / 关闭 **开机自启** | Autostart |
| 10 | （隐含）日志与错误反馈 | Log Bus |

### 1.2 明确不做

- **不改动硬件设计与固件协议** — 沿用现有 Nordic UART 风格 GATT、命令字 `a/b/x/s` 与状态文本格式。
- **不废弃现有 CLI 脚本** — `usb_switch*.bat` 保持可用；GUI 与 CLI 共用同一套 `core/`，不复制逻辑。
- **不引入服务端** — 全部本地运行，无云端依赖。

---

## 2. 技术选型

| 候选方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **Python + PySide6 (Qt)** | 直接复用已跑通的 bleak / 桥接 / 弹出逻辑；Signal-Slot 天然适配异步；控件齐全；LGPL 可商用 | PyInstaller 打包后约 60–80 MB | **推荐** |
| Python + Tkinter | 零第三方依赖、体积最小 | 界面陈旧、样式定制能力差、asyncio 集成别扭 | 备选（若强制要求零依赖） |
| Python + CustomTkinter | 观感现代、依赖小 | 生态小，复杂列表/表格能力弱 | 备选 |
| C# WPF / WinUI | 原生体验、产物体积小 | 必须用 `Windows.Devices.Bluetooth` + 重写桥接逻辑，**放弃全部已验证的 Python 资产** | 不推荐 |
| Electron / Tauri | Web 技术栈熟悉 | 体积大 / 需 Rust 工具链，与现有 Python 生态割裂 | 不推荐 |

**已确认选型：Python 3.12 + PySide6。**（远程管理通道：paramiko）

理由一句话：核心逻辑（BLE 连接、磁盘弹出、桥接托管）已经在 Python 里跑通并踩过坑，换栈等于把已验证的部分推倒重写，而风险最高、最难调的恰好就是那些部分。

补充依赖：

```text
PySide6      GUI 框架
bleak        BLE（已在使用）
paramiko     SSH / SCP（远程管理）
pywin32      仅取 win32crypt 做 DPAPI 加密（可选）
pyinstaller  打包
```

---

## 3. 总体架构

```text
                ┌───────────────────────────────────────────────┐
                │   USB Switch Console  (单一进程)               │
                │                                               │
                │   ┌───────────────────────────────────────┐   │
                │   │  UI 线程 (Qt Event Loop)               │   │
                │   │  MainWindow / 各功能面板 / 日志面板     │   │
                │   │  TrayIcon · 托盘菜单 · 显示/隐藏窗口    │   │
                │   └──────┬───────────────────────┬────────┘   │
                │          │ Signals               │ Signals     │
                │   ┌──────▼────────┐   ┌──────────▼─────────┐  │
                │   │ BleWorker      │   │ BridgeManager      │  │
                │   │ QThread        │   │ QThread            │  │
                │   │  asyncio loop  │   │  subprocess 守护   │  │
                │   │  BleakClient   │   │  paramiko SSH      │  │
                │   └──────┬─────────┘   └──────────┬─────────┘  │
                └──────────┼────────────────────────┼────────────┘
                           │ BLE GATT               │ HTTP / SSH
                    ┌──────▼──────┐        ┌────────▼─────────────┐
                    │  ESP32-C3   │        │ 本机 :8737           │
                    │  USB-Switch │        │ 远端 Linux :8738     │
                    └─────────────┘        └──────────────────────┘
```

### 三条架构铁律

1. **UI 线程零阻塞。** 所有 IO（BLE、HTTP、SSH、子进程读取）都在工作线程；UI 只收 Signal 改控件。
2. **全进程只允许一个 BleakClient。** Windows 的 WinRT 后端在多个客户端并发时会出现 GATT 服务发现不完整（README 已记录该缺陷）。因此 **GUI 运行时不得再调用 `usb_switch.py` 脚本**，否则立刻复现该问题。
3. **`core/` 与 UI 解耦。** `core/` 不 import 任何 Qt 符号，可被 CLI 脚本与单元测试直接调用。

---

## 4. 模块设计

### 4.1 `core/disk.py` — 磁盘检测与安全弹出 ⚠️ 必须先修

**现状问题**：`scripts/usb_switch_safe.py` 的 `list_removable_drives()` 每次调用 spawn 一个 PowerShell 进程，实测**单次约 6.6 秒**。GUI 若按 1 s 轮询会直接卡死界面；`eject_drive()` 的 20 s 超时（0.5 s 间隔）实际只能轮询约 3 次，会在 U 盘刚弹出时误判失败。

**改法**：用 `ctypes` 直调 Win32 API，毫秒级、零子进程。

```python
import ctypes
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetLogicalDrives.restype = wintypes.DWORD
_k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_k32.GetDriveTypeW.restype = wintypes.UINT

DRIVE_REMOVABLE = 2

def removable_drives() -> list[str]:
    """返回可移动磁盘盘符，如 ['E']。毫秒级。"""
    mask = _k32.GetLogicalDrives()
    out = []
    for i in range(26):
        if mask & (1 << i):
            letter = chr(ord("A") + i)
            if _k32.GetDriveTypeW(f"{letter}:\\") == DRIVE_REMOVABLE:
                out.append(letter)
    return out
```

**弹出**也改为 ctypes，走「锁定 + 卸载」的原生序列（与「安全删除硬件」内核路径一致）：

```text
1. CreateFile("\\\\.\\F:", GENERIC_READ, share=RW)   ← 只读，不要加 GENERIC_WRITE
2. FSCTL_LOCK_VOLUME      (0x00090018)   ← 无进程占用时才能成功
3. FSCTL_DISMOUNT_VOLUME  (0x00090020)   ← 即使第 2 步失败也可卸载
```

> **为什么用只读打开（`GENERIC_READ`）**
>
> 踩过最贵的一个坑。加 `GENERIC_WRITE` 后 `CreateFile` 对 removable 卷**恒定**返回
> `ERROR_ACCESS_DENIED`，现象与「被进程占用」完全一样，我据此误判方向、
> 把重试窗口从 2.4 s 一路加到 8 s——**等多久都不会成功**。
>
> 真机时间轴实验的对照结果：`GENERIC_READ|GENERIC_WRITE` 恒定 err=5，
> `GENERIC_READ` 恒定成功，而 `FSCTL_DISMOUNT_VOLUME` 只需读权限。
> 改成只读打开后，弹盘耗时从「永远失败」变成 **0.01 – 0.87 秒**。
>
> 另外也不需要额外 `FlushFileBuffers` —— `FSCTL_DISMOUNT_VOLUME` 自身会刷写并
> 作废卷缓存。实测多次强制卸载后 `fsutil dirty query` 仍显示未置脏，
> 说明文件系统是被干净关闭的。

> **为什么不走 Shell 的 `InvokeVerb('Eject')`（初版方案）**
>
> 真机验证证明这条路有两个硬伤：
>
> 1. **动词名是本地化的**。中文系统上叫「弹出(&J)」，写死 `'Eject'` 不会报错，
>    只是什么都不做 —— 且在英文系统上无法发现这个问题（原方案只能在英文 Windows 上工作）。
> 2. **成败判据不成立**。该 U 盘的桥片不支持 `IOCTL_STORAGE_EJECT_MEDIA`
>    （返回 `ERROR_NOT_SUPPORTED`），盘符根本不会消失，「轮询盘符是否消失」
>    这个判据从一开始就是错的。
>
> 改为原生序列后，成败由 `DeviceIoControl` 的返回值**确定性**给出，
> 不再依赖轮询与计时；也不再需要 PowerShell，少一个进程开销。

**ioctl 常量必须按公式算**：`CTL_CODE(FILE_DEVICE_FILE_SYSTEM=9, Function, METHOD_BUFFERED, FILE_ANY_ACCESS)`
= `(9 << 16) | (Function << 2)`。算错**不会报错**，只返回 `ERROR_INVALID_FUNCTION`，
极难排查（`FSCTL_IS_VOLUME_DIRTY` 就写错过一次）。已加测试锁死这两个常量。

**重试保留但只作兜底**：`6 次 × 0.4 s`（预算 2.4 s）。真正的原因修掉之后，
第一次尝试通常就成功，重试只用于覆盖「卷刚上电仍在稳定」的瞬时窗口。

接口约定：

```python
def removable_drives() -> list[str]
def eject(letter: str | None = None) -> str    # 失败抛 EjectError
def resolve_target(configured: str | None = None) -> str
def volume_health(letter: str) -> str | None   # Healthy / Warning / …
def is_volume_dirty(letter: str) -> bool | None
```

> `volume_health()` 走 PowerShell 的 `Get-Volume`（返回 .NET 枚举成员名，
> **不随系统语言变化**，可作稳定判据）。`FSCTL_IS_VOLUME_DIRTY` 的常量修正后
> 在本机仍返回 `ERROR_INVALID_FUNCTION`，**没有继续盲扫候选控制码**——
> 在用户机器上调用未知 ioctl 风险不可接受。它只在弹盘失败时调用，开销可接受。

### 4.2 `core/ble.py` — BLE 客户端

`BleWorker` 是 `QObject`，moveToThread 到专用 `QThread`，内部跑独立 asyncio 事件循环。

```python
class BleWorker(QObject):
    connectedChanged = Signal(bool)
    statusReceived   = Signal(str)          # 原始状态行 host=..,detA=..,detB=..
    devicesFound     = Signal(list)
    logMessage       = Signal(str, str)     # level, text

    def start(self) -> None:
        """起一个标准 threading.Thread（**不是** QThread，见下）。"""
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, daemon=False)
        self._thread.start()
        self._ready.wait(timeout=5.0)       # 等事件循环就绪，消除启动竞态

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_until_complete(self._supervise())   # 阻塞直到 stop()

    def scan(self, timeout: float = 6.0):
        asyncio.run_coroutine_threadsafe(self._scan(timeout), self._loop)

    def send(self, cmd: str):
        asyncio.run_coroutine_threadsafe(self._q.put(cmd), self._loop)

    def shutdown(self) -> bool:                 # 幂等
        """请求停止 → join，返回是否在超时内退出。"""
```

要点：

- **不要用 `QThread` 承载这个循环**（初版方案写的是 `moveToThread(QThread)`，是错的）。
  `QThread.quit()` 只作用于 **Qt 事件循环**，而这里跑的是阻塞的
  `asyncio.run_forever()`——`quitNow` 要等 `_thread_main()` 返回、Qt `exec()` 启动后
  才生效，而 `run_forever()` 在此之前永不返回。表现为 `wait()` 超时、
  `QThread: Destroyed while thread is still running`，**进程退出时直接崩溃**
  （exit code `0xC0000409`）；还有一层竞态：`shutdown()` 早于 `_thread_main` 读
  `self._loop`，stop 请求根本发不出去。
  现改为标准 `threading.Thread` + `threading.Event` 显式握手（等循环就绪 → 请求停止 → join）。
  信号跨线程照常走 QueuedConnection，不受影响。
- **退出清理不能只挂 `closeEvent`**：`QApplication.quit()`（托盘退出、系统注销）
  **不触发** `closeEvent`，后台线程会被直接销毁。已补挂 `QApplication.aboutToQuit`，
  并把 `shutdown()` 做成幂等的。
- **`_supervise()` 是常驻重连循环**：连接 → 服务注册 → 消费命令队列；异常时 `connectedChanged(False)` 并指数退避重连（1 s → 30 s 封顶）。ESP32 在连接期间持续广播（最多 3 路），所以重连只需按 Service UUID 过滤，无需重新扫描。
- **`send()` 强制串行**：命令走 `asyncio.Queue`，UI 连点按钮不会产生并发 GATT 写。
- **等待应答**：写入后等 TX 通知，**超时 2 s**（固件 `switchTo()` 最坏约 1.5 s），超时上报"结果未知"而不是静默成功。
- **断线自动恢复**：监听 `gattserverdisconnected`，置位后走退避重连。
- **GATT 缓存兜底**：连接失败且异常类型表明服务发现失败时，提示用户"关闭再打开蓝牙开关"，与现有脚本文案一致。
- **连接成功后必须自动查一次状态**。缺这一步，界面会停在「已连接但状态全是 `—`」，
  看起来像程序坏了（真机验证时发现，原方案漏了这步，网页面板本来是有 `await send("s")` 的）。

### 4.3 `core/bridge_server.py` + `core/bridge_local.py` — 本地桥接服务

**关键修正：桥接服务在 GUI 里以「库 + 后台线程」形态运行，不是子进程。**

原方案让 GUI 用 `subprocess.Popen([sys.executable, "bridge.py"])` 拉起本地桥接。这在开发态成立，**打包后必然崩**：PyInstaller 冻结后 `sys.executable` 指向的就是 exe 自己，`exe bridge.py` 不会去执行什么脚本，只会再开一个 GUI 实例。

```python
# core/bridge_server.py —— 库形态
class BridgeServer:
    def __init__(self, port: int, eject_provider: Callable[[], dict]): ...
    def start(self) -> None      # 后台线程 serve_forever
    def stop(self) -> None
    @property
    def state(self) -> BridgeState
```

`bridge_local.py` 是它的薄封装：按配置装配 `eject_provider`（指向 `core.disk`）、做端口预检、探活与状态上报。

要处理的四件事：

1. **端口占用预检** — `socket.connect(("127.0.0.1", port))` 探测；冲突则提示并允许改端口。
2. **存活检测** — HTTP `/status` 探活（线程活着 ≠ 服务可用）。
3. **生命周期** — 线程随进程结束而结束，socket 自动释放；**不再需要** `terminate/kill` 与 `atexit` 兜底，顺带消灭了「退出残留进程」这个风险。
4. **向后兼容** — `scripts/bridge.py` 退化为十几行的 CLI 启动器，`start_bridge.bat` 与 README 里现有的手工用法不受影响。

### 4.4 `core/bridge_remote.py` — 远程（Linux）桥接服务

通道：`paramiko.SSHClient` + `SFTPClient`，密码与私钥双认证（`allow_agent=False`、
`look_for_keys=False`，避免「密码错」被 agent 里的密钥掩盖成更难懂的错误）。

**安装（幂等，可重复执行）**：

```text
1. SSH 连通性测试（超时 8 s，失败按类型给可操作提示）
2. 远端环境探测：$HOME / python3 路径与版本 / systemd 可用性 / umount 路径 / 免密 sudo
   —— 用**一条**命令取回全部字段，省往返
3. SFTP 上传 linux_bridge.py → ~/.local/bin/usb_switch_bridge.py，chmod 0755
4. 生成 systemd user unit ~/.config/systemd/user/usb-switch-bridge.service
       [Unit]    Description=USB-Switch Bridge
       [Service] ExecStart=<python3 绝对路径> <脚本绝对路径> <bridge_port>
                 Restart=always  RestartSec=3
       [Install] WantedBy=default.target
5. systemctl --user daemon-reload && systemctl --user enable --now usb-switch-bridge
6. loginctl enable-linger <user>      ← 保证用户登出后服务不被杀
7. 验证 GET http://<ip>:<bridge_port>/status 返回 ok=true
```

**启动 / 停止 / 卸载** 全部落到同一组 systemctl 原语上；**卸载**顺序为
`disable --now` → `reset-failed` → 删 unit → 删脚本，**保留日志文件**，
且对"本来就不存在"的情况静默成功（幂等）。

#### ⚠️ `systemctl --user` 在 SSH 非登录会话里必须带 `XDG_RUNTIME_DIR`

不带会得到 `Failed to connect to bus: No such file or directory` —— 这个报错很有
迷惑性，**看起来像「这台机器没有 systemd」**，实际只是少了个环境变量。
所有 `systemctl --user` 调用统一走同一个前缀常量，并有测试守着。

#### ⚠️ 不代替用户改 `/etc/sudoers`（与初版方案的偏离）

初版方案把「配置 umount 免密 sudo」列为安装步骤 7。实现时改成**只检测、不代劳**：

- 写 `sudoers.d` 是特权且高风险的改动（写坏会连被写机器上的 `sudo` 都用不了）；
- SSH 非交互会话无法安全地输入 sudo 密码，用 `echo pass | sudo -S` 把密码塞进
  命令行是不可接受的。

所以实现只检测 `sudo -n -l /bin/umount` 是否可用，不可用时在日志里给出**可直接
复制粘贴**的配置命令（含 `visudo -c` 校验），由用户自己决定和执行。

**回退方案**：远端无 systemd（老发行版、容器）时自动降级为 nohup + PID 文件
（复用 README 里已有的方式），启动前先 `kill -0` 检查旧 PID 避免重复拉起。

**连通性呈现要分层**：UI 上「SSH 可达」与「桥接服务在线」是**两个独立指示灯**——
SSH 通不代表 HTTP 能通（防火墙、服务没起、端口被占）。

#### 探测分两档（开销差三个数量级）

| 方法 | 开销 | 用途 |
|---|---|---|
| `probe()` | 一个 HTTP 请求，毫秒级 | 跟着 5 秒轮询跑，**不置 busy** |
| `inspect()` | 建 SSH 连接 + 环境探测，秒级 | 只在用户点「刷新 / 测试连接」或操作结束后跑 |

`RemoteState` 因此有三个状态而不是两个：`ssh_reachable` / `http_online`，
再加上 **`ssh_checked`** —— 只做 HTTP 探活时根本没碰 SSH，既不能说「可达」也
不能说「不可用」，只能如实显示「未检测」。少了这个字段，周期轮询会把 SSH
一直画成红色，用户会以为连不上。

#### SSH 隧道（`core/ssh_tunnel.py`）—— 真机验证后补上的通路

真机验证暴露了一个初版方案的致命前提错误。实测目标是一台 Robustel 边缘网关：

```
设备侧：服务正常，监听 0.0.0.0:8738，设备本机 curl 127.0.0.1:8738/status 返回正常
本机侧：ping 通、22/80/443 开放、**8738 被挡**
```

即**网关 LAN 口的入站白名单只放通 22/80/443**，桥接端口不在其中。
「远端 HTTP 直连」这条路在真实环境里根本走不通，而这不是代码问题。

于是补上 SSH 隧道：复用同一套 SSH 凭据与主机密钥策略，把远端回环地址上的
桥接服务映射到本机一个端口。三个好处：

1. **不需要改设备配置** —— SSH 本来就是通的；
2. **顺带消灭了 §7 风险 #3**（远端无鉴权 HTTP 暴露在网络上）—— 服务可以只绑回环；
3. 隧道只在需要时存在，关掉即无暴露面。

实现要点：

| 决策 | 理由 |
|---|---|
| 本地端口默认**自动分配**（配置 0） | 写死端口平白多一类「本地端口被占用」的失败；实测开发机上 Clash Verge 就占着 8737 |
| 隧道与「管理连接」是**两条独立 SSH 连接** | 管理操作是短命串行的，隧道是长命持续的；共用会让「装个东西失败」顺带打断隧道 |
| 就绪后写回 `config.runtime_http_base` | 让**桥接地址只有一个来源**，见下 |
| 断线退避重连（3 s → 30 s） | 与 BLE 一致；网络抖动不该让切换功能永久失效 |

**为什么"桥接地址只有一个来源"很重要**：`ejector`、`probe()`、安装后的 HTTP
验证三处都要知道「远端桥接在哪个地址」。如果分别在各自内部判断「走隧道还是
直连」，漏掉任何一处就会留下「状态显示在线、切换却连到被防火墙挡住的直连地址」
这种自相矛盾的表现。所以把运行期覆盖放在 `RemoteBridgeConfig` 上，
`http_base` 属性统一读取，三处都无需知情。

> `runtime_http_base` 是**运行期**字段，**不落盘**（`_remote_to_dict` 逐字段列出，
> 天然把它挡在外面；并有测试断言配置文件里搜不到它的值）。隧道地址只在本次
> 进程里存在，写进配置文件会让下次启动拿着一个不存在的端口去连。

### 4.5 `core/config.py` — 配置与凭据

- 路径：`%APPDATA%\USBSwitchConsole\config.json`
- 写入用「临时文件 + `os.replace`」原子替换，避免断电写坏配置。

#### 配置模型：以「Host 角色」为中心，而不是「远程主机列表」

系统物理上只有两路 Host（Host A / Host B），**每个角色需要一个桥接端点**。所以配置按角色组织，而不是维护一个扁平的远程主机列表——后者会让「当前这一步该调哪台」变得模糊。

```jsonc
{
  "ble":       { "device_name": "USB-Switch", "auto_connect": false },
  "safety":    { "force_eject": true },
  "window":    { "close_to_tray": true, "geometry": "..." },
  "autostart": { "enabled": false, "start_minimized": true },
  "bridges": {
    "A": {
      "kind": "local",
      "local": { "port": 8737 }
    },
    "B": {
      "kind": "remote",
      "remote": {
        "host":           "192.168.1.100",   // IP 或域名
        "ssh_port":       22,                // SSH 管理端口
        "username":       "ubu",
        "auth":           "password",        // password | key
        "password_enc":   "<DPAPI base64>",  // 仅 auth=password
        "key_path":       "",                // 仅 auth=key
        "passphrase_enc": "",                // 仅 auth=key 且私钥有口令
        "bridge_port":    8738,              // 远端桥接 HTTP 监听端口
        "display_name":   "Linux 测试机"      // 可选别名，仅用于显示
      }
    }
  }
}
```

#### ⚠️ 必须区分两个端口（实现时最易踩的坑）

| 字段 | 含义 | 默认值 | 用途 |
|---|---|---|---|
| `ssh_port` | SSH **管理**通道 | 22 | 安装 / 启动 / 停止 / 卸载远端服务 |
| `bridge_port` | 远端桥接服务 **HTTP 监听**端口 | 8738 | `POST /eject` 安全弹出、`GET /status` 探活 |

两者完全独立：SSH 走 22，桥接 HTTP 走 8738，UI 上**必须拆成两个独立输入框**，绝不能合并成一个「端口」。

#### 凭据存储

- `password` 与 `passphrase` 用 Windows DPAPI（`win32crypt.CryptProtectData`，用户域，`CRYPTPROTECT_LOCAL_MACHINE = 0`）加密后以 base64 存入 `*_enc` 字段。
- **任何情况下不落明文密码**；`config.json` 里 grep 不到明文。
- 私钥认证只存路径；路径本身不加密（不敏感），口令仍走 DPAPI。
- UI 回填时密码框显示掩码 `••••••••`，不做明文回显。

#### 表单字段 → 配置字段映射

| UI 输入框 | 配置字段 | 校验 |
|---|---|---|
| IP / 主机名 | `host` | 非空；域名或 IPv4/IPv6 |
| SSH 端口 | `ssh_port` | 1–65535，默认 22 |
| 用户名 | `username` | 非空 |
| 密码 | `password_enc` | 与私钥认证二选一 |
| （私钥路径） | `key_path` | 选 key 认证时必填且文件存在 |
| 桥接端口 | `bridge_port` | 1–65535，默认 8738 |
| 别名 | `display_name` | 可空 |

### 4.6 `core/orchestrator.py` — 切换编排（业务核心）

这是把所有模块串起来、也是唯一允许改变设备状态的地方。它按「当前主机」把弹出请求路由到 `bridge_local` 或 `bridge_remote`，调用方无需关心对方是本机还是远程。

### 4.7 `core/autostart.py` — 开机自启

**注册表方式（推荐，不需要管理员权限）**：

```text
HKCU\Software\Microsoft\Windows\CurrentVersion\Run
    值名   USBSwitchConsole
    值数据 "<可执行路径>" --minimized
```

实现要点：

1. **可执行路径在开发态与打包态不同**，必须分支处理，统一封装成 `_launch_command()`：
   - 打包后（`getattr(sys, "frozen", False)` 为真）：`sys.executable` 就是 exe 本身
   - 开发态：`f'"{sys.executable}" "{项目根 / "app" / "main.py"}"'`
2. **路径含空格必须加引号**。不加引号时注册表值会被 Windows 在第一个空格处截断——`C:\Program Files\...` 必踩。
3. **`--minimized` 参数**：自启拉起时直接进托盘，不弹窗口打扰用户。
4. **幂等**：写入前先比对现有值，相同则跳过；删除时值不存在也视为成功。
5. 用标准库 `winreg` 读写，不额外引入依赖。

```python
def is_enabled() -> bool
def enable() -> None
def disable() -> None
```

**一致性处理**：应用每次启动时读注册表**实际**状态回填 UI（以注册表为准，而不是拿配置去覆写注册表）——因为该项可能被安全软件或用户手工清理，配置与实际脱节会导致 UI 显示错误。

---

## 5. 界面设计

单窗口、分区块（不做多页跳转，所有状态一屏可见）。

```text
┌─ USB Switch Console ──────────────────────────────── [?] ─┐
│                                                           │
│  设备                                                     │
│  ●  未连接  USB-Switch        [ 扫描 ]  [ 连接 ]           │
│                                                           │
│  快速切换                                                 │
│  ┌────────────────┐ ┌────────────────┐ ┌──────────────┐  │
│  │  → Host A      │ │  → Host B      │ │   断开 U 盘  │  │
│  └────────────────┘ └────────────────┘ └──────────────┘  │
│                            [ 刷新状态 ]                    │
│                                                           │
│  状态                                                     │
│  当前主机      Host A           Host A 检测   已插入       │
│  固件回报      12:04:31         Host B 检测   无           │
│                                                           │
│  桥接服务                                                 │
│  本地   ● 运行中  :8737        [启动] [停止] [弹出U盘]     │
│  远程   ○ 未连接               [安装] [启动] [停止] [卸载] │
│  ┌──────────────────────────────────────────────────┐    │
│  │ ubu@192.168.1.100:22          [测试连通] [保存]   │    │
│  └──────────────────────────────────────────────────┘    │
│                                                           │
│  选项                                                     │
│  安全弹出互锁  [ ● 启用 ]  [ ○ 禁用 ]                       │
│  ☐ 开机自启（静默启动到托盘）                                │
│  ☑ 关闭主窗口时最小化到托盘，不退出程序                      │
│  ☐ 启动时自动连接设备                                      │
│                                                           │
│  日志                                     [清空] [导出]    │
│  ┌──────────────────────────────────────────────────┐    │
│  │ [12:04:28] 正在扫描 ...                           │    │
│  │ [12:04:30] 已找到 USB-Switch，正在连接 ...        │    │
│  │ [12:04:31] ✓ 已连接，host=A                       │    │
│  └──────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────────┘
```

### 5.1 系统托盘

托盘菜单（右键）：

```text
┌────────────────────────────┐
│  显示主窗口                 │
├────────────────────────────┤
│  当前主机：Host B           │  ← 只读
│  本地桥接：运行中            │  ← 只读
│  远程桥接：未运行            │  ← 只读
├────────────────────────────┤
│  → 切换到 Host A            │
│  ✓ → 切换到 Host B          │  ← 当前项打勾
│    断开 U 盘                │
├────────────────────────────┤
│  开机自启              ✓    │  ← 可勾选，与主窗口选项联动
├────────────────────────────┤
│  退出                       │
└────────────────────────────┘
```

托盘行为约定：

- **左键单击 = 切换主窗口显示/隐藏**；双击同左键（保持一致，避免两种点击语义冲突）。
- **右键 = 弹出菜单**；菜单内只读项（当前主机、桥接状态）置灰不可点。
- **托盘图标随状态变化**：已连接 / 未连接 / 切换进行中，三种图标或叠加角标。
- **首次收起时弹一次气泡通知**「程序仍在后台运行，U 盘切换服务保持可用」，并写入配置不再重复提示。
- 托盘菜单的 **「退出」是唯一真正退出程序的入口**，会连带停止所托管的本地桥接子进程。
- 前提：`QApplication.setQuitOnLastWindowClosed(False)`，否则关闭窗口会直接结束进程，托盘根本没机会存活。

### 5.2 单实例约束（硬要求）

引入托盘后用户极易重复双击图标启动。**两个实例 = 两个 `BleakClient` = 必然复现 WinRT 并发 GATT 缺陷**，同时还会争抢本地桥接端口。

因此必须加单实例锁（`QLocalServer` / `QSharedMemory`，或 Windows 命名互斥体）：第二个实例检测到已有实例后，**唤出并激活已有窗口，然后自身退出**。

交互约定：

- **按钮按可用性联动**：未连接 BLE 时切换按钮置灰；本地桥接未运行时禁用「安全弹出」相关提示。
- **安全弹出开关**：默认「启用」。切到「禁用」时开关转为警示色，并常驻一行提示"切换将不再弹出 U 盘"，避免被误长期关闭。
- **开机自启开关**：主窗口与托盘菜单两处入口**必须双向同步**，改一处另一处立即跟随。
- **状态区永不显示假值**：无数据一律显示 `—`，不沿用上一次结果。
- **日志区实时追加 + 自动滚底**，错误行红色、成功行绿色。

---

## 6. 核心流程

### 6.1 切换 U 盘（主流程）

```text
用户点击「→ Host B」
  │
  ├─ 1. 前置校验
  │      BLE 已连接？          否 → 提示并中止
  │      目标主机 == 当前主机？ 是 → 提示"已是 Host B"，不动设备
  │
  ├─ 2. 安全弹出互锁（开关启用 且 当前主机 != none）
  │      调「当前主机」对应桥接的 POST /eject
  │        ├─ 成功        → 继续
  │        ├─ 桥接不可达  → 中止，提示"无法安全弹出，已阻止切换"
  │        └─ 弹出失败    → 中止，展示具体占用原因
  │      ── 开关禁用时整步跳过，日志打 WARN 并标注"未经安全弹出"
  │
  ├─ 3. BLE 写入 'b'
  │
  ├─ 4. 等待 TX 通知（超时 2 s）
  │        ├─ OK: host=B → 更新 UI
  │        └─ FAIL / 超时 → 报错并自动发 's' 校准真实状态
  │
  └─ 5. 记录日志
```

**关键设计**：第 2 步的失败是**硬中止**，绝不「先切了再说」——因为此时 U 盘还挂载着，断电切换会损坏文件系统。这与现有 `usb_switch_safe.py` 的语义保持一致。

### 6.2 远程安装流程

```text
[安装] → 读取该 Host 角色的远程配置（host / ssh_port / username / 凭据 / bridge_port）
       → 后台线程执行 4.4 的 8 步，其中 systemd unit 里的端口用 bridge_port
       → 每一步的 stdout/stderr 实时推送到日志面板
       → 完成/失败后刷新远程状态灯
       → 失败时保留已完成步骤，支持重试（幂等）
```

配置不完整时（缺 IP / 用户名 / 凭据）**在点击瞬间就拦下**并标红对应输入框，不要等到 SSH 超时后才报一个笼统错误。

### 6.3 窗口 / 托盘生命周期

```text
主窗口 [X]
   ├─ close_to_tray = true  → 隐藏窗口；进程与本地桥接继续运行
   │                          （首次弹气泡提示）
   └─ close_to_tray = false → 走「退出」流程

托盘图标 [左键]  → 主窗口可见？隐藏 : 显示并激活
托盘菜单 [退出]  → 真正退出流程：
       1. 停止本地桥接子进程（terminate → wait(3) → kill）
       2. 断开 BLE 连接
       3. 保存窗口几何与配置
       4. app.quit()

第二实例启动 → 检测到单实例锁 → 唤出已有窗口 → 自身退出
```

### 6.4 开机自启

```text
用户勾选「开机自启」（主窗口选项区 或 托盘菜单）
  → config.autostart.enabled = true
  → autostart.enable() 写 HKCU\...\Run
  → 回读校验；失败（被组策略 / 安全软件拦截）→ 回滚配置并向用户明示原因

系统重启，注册表拉起：  "<exe 路径>" --minimized
  → main.py 解析到 --minimized → 不显示主窗口，直接进托盘
  → 按配置决定是否自动连接 BLE、自动启动本地桥接

应用启动时 → 读注册表实际状态回填 UI（以注册表为准）
```

---

## 7. 风险与对策

| # | 风险 | 影响 | 对策 |
|---|---|---|---|
| 1 | WinRT 并发 GATT 缺陷 | 服务发现不完整、设备无响应 | GUI 单进程单客户端；明确禁止 GUI 再调 CLI 脚本 |
| 2 | `list_removable_drives` 单次 6.6 s | UI 卡顿、误判弹出失败 | 4.1 改 ctypes，必须先于 UI 开发完成 |
| 3 | 远程桥接无鉴权且监听 `0.0.0.0` | 同网段任何人可触发弹盘 | **已由 SSH 隧道解决**：默认经隧道访问，远端桥接不必暴露在网络上（建议同时把监听地址改回环，见 §4.4）。若要直连使用，仍需把主机放在可信内网并限制端口入站来源 |
| 4 | 弹出成功但切换失败 | U 盘停在"已弹出"态，用户以为丢了盘 | 失败提示中明确说明 U 盘状态，并提供「重新挂载 / 重试切换」指引 |
| 5 | 两路 VBUS 互斥依赖软件 | 极端情况硬件风险 | GUI 加一道**状态层校验**：仅当 `当前主机 != 目标` 才下发命令，减少无谓切换次数 |
| 6 | 端口 8737 被占用 | 本地桥接起不来 | 启动前**真去 bind**一次做探测；区分 `WSAEACCES` 与 `WSAEADDRINUSE` 给出不同排查方向；改端口后重新点「启动」即生效，不必重启程序 |
| 15 | 远端防火墙不放通桥接端口 | 远端 HTTP 直连不通，功能整个不可用 | **默认走 SSH 隧道**（`use_tunnel`，见 §4.4）—— 实测边缘网关只放 22/80/443 |
| 16 | 隧道本地端口被占用 | 隧道起不来 | 默认自动分配空闲端口；写死端口时建立失败会当场报错并提示改为「自动」 |
| 7 | 打包态用子进程拉起桥接 | exe 只会再启一个 GUI 实例，桥接永远起不来 | **8.4 ①**：桥接固定以「库 + 后台线程」形态运行，不 spawn 进程 |
| 8 | SSH 凭据泄露 | 安全风险 | DPAPI 加密；优先私钥认证 |
| 9 | 托盘 + 自启诱发多实例 | 两个 `BleakClient` **必现** WinRT 并发缺陷，并争抢本地桥接端口 | **单实例锁**（`QLocalServer`）；第二实例唤出已有窗口后自行退出 |
| 10 | 注册表自启项被安全软件 / 组策略拦截 | 自启静默失效，用户以为已生效 | `enable()` 后**回读校验**；失败回滚配置并向用户明示 |
| 11 | 开发态 / 打包态可执行路径不同 | 自启指向错误目标，重启后什么都不发生 | `autostart.py` 统一封装 `_launch_command()`，两种形态各验证一次 |
| 12 | 注册表值中路径含空格未加引号 | 值被 Windows 在第一个空格处截断，自启失效 | 写入时强制加引号（`C:\Program Files\...` 必踩） |
| 13 | 托盘模式下退出路径分散 | 桥接子进程残留、端口占死 | 退出逻辑统一收敛到单个 `shutdown()`；托盘「退出」与 `aboutToQuit` 都只调它 |
| 14 | SSH 端口与桥接端口填反 | 服务装到了错误端口，HTTP 永远探不通 | UI 拆成两个独立输入框并各自带默认值与提示文案 |

---

## 8. 代码目录规划

### 8.1 三条布局原则

1. **分三层，依赖单向。** `ui → workers → core`，反向禁止。`core/` 里出现任何 `PySide6` 字样都算 bug。
2. **路径差异只在一个文件里处理。** 开发态与打包态（`sys._MEIPASS`）的资源路径不同，这个分支只允许出现在 `core/paths.py`，其他模块一律从它取。这是 PyInstaller 项目最容易到处漏 `if frozen` 的地方。
3. **要分发到别的机器上的文件必须自包含。** `linux_bridge.py` 会被 SFTP 到 Linux 主机由 systemd 拉起，那台机器上没有本项目、没有 PySide6、也不该为了它装一整套包——它必须是一个能独立 `python3 xxx.py` 跑起来的单文件。

### 8.2 完整目录树

```text
usb-switch/
├── pyproject.toml                        # 新增：包元数据 + 依赖 + 入口点
├── usbswitch.spec                        # PyInstaller 打包配置（Phase 4）
├── packaging/
│   └── launcher.py                       # 打包入口：PyInstaller 需要顶层脚本
├── src/
│   └── usbswitch/                        # 桌面客户端主包
│       ├── __init__.py
│       ├── __main__.py                   # 支持 python -m usbswitch
│       ├── main.py                       # 入口：--selftest → 单实例 → 解析 --minimized → 装配
│       ├── selftest.py                   # 自检（打包产物的可自动化验证手段）
│       ├── version.py                    # 唯一版本号来源
│       │
│       ├── core/                         # ── 纯逻辑层：零 Qt，可单测，可被 CLI 复用
│       │   ├── paths.py                  # 集中路径解析（开发态 / 打包态）
│       │   ├── models.py                 # 数据类与枚举：Host / BridgeKind / BridgeRunState / RemoteBridgeConfig
│       │   ├── errors.py                 # 领域异常：EjectError / BleError / RemoteAuthError …
│       │   ├── logging_setup.py          # 文件轮转 + 内存环形缓冲（供 UI 读日志）
│       │   ├── crypto.py                 # DPAPI 加解密（ctypes 直调 crypt32，不依赖 pywin32）
│       │   ├── config.py                 # 配置读写（原子替换）+ 凭据字段编解码
│       │   ├── disk.py                   # 磁盘检测与安全弹出（ctypes：锁定 + 卸载卷）
│       │   ├── http_json.py              # stdlib 的 JSON-over-HTTP 客户端
│       │   ├── bridge_server.py          # 桥接 HTTP 服务（库形态 —— 见 8.4 ①）
│       │   ├── bridge_local.py           # 本地桥接启停/探活（bridge_server 的薄封装）
│       │   ├── bridge_remote.py          # 远程桥接：paramiko 连接 + 安装/启停/卸载
│       │   ├── ssh_tunnel.py             # SSH 隧道：远端回环桥接 ↔ 本机端口
│       │   ├── ejector.py                # 按 Host 角色路由到桥接端点的 POST /eject
│       │   ├── ble.py                    # BLE 协议层：扫描/连接/重连/串行命令（纯 asyncio）
│       │   ├── autostart.py              # 开机自启（winreg）
│       │   └── orchestrator.py           # 切换编排 + 安全弹出互锁（业务核心）
│       │
│       ├── workers/                      # ── Qt 适配层：core 与 UI 之间的桥
│       │   ├── ble_worker.py             # threading.Thread + asyncio loop，包装 core.ble
│       │   ├── bridge_worker.py          # 本地桥接启停 / 探活
│       │   ├── remote_worker.py          # 远程操作（单线程 + 命令队列，串行）
│       │   └── tunnel_worker.py          # 按配置决定隧道该不该存在
│       │
│       ├── ui/                           # ── 表现层：只展示与转发，不写业务逻辑
│       │   ├── main_window.py
│       │   ├── tray.py                   # 托盘图标 + 右键菜单 + 显示/隐藏
│       │   ├── single_instance.py        # QLocalServer 单实例锁 + 唤出已有窗口
│       │   ├── icons.py                  # 运行时 QPainter 画图标（零二进制资源）
│       │   ├── theme.py                  # QSS 与颜色令牌
│       │   ├── widgets.py                # StatusLight / KeyValueGrid / LogView / ToggleSwitch
│       │   └── panels/
│       │       ├── device_panel.py
│       │       ├── switch_panel.py
│       │       ├── status_panel.py
│       │       ├── bridge_panel.py       # 本地桥接
│       │       ├── remote_panel.py       # 远程连接配置 + 两层状态（SSH / HTTP）
│       │       ├── options_panel.py
│       │       └── log_panel.py
│       │
│       └── resources/                    # 随包分发
│           ├── app.ico                   # exe 图标（由 scripts/make_icon.py 生成）
│           └── remote/                   # 打包时复制进来的远端脚本
│
├── tests/
│   ├── conftest.py
│   ├── unit/                             # 纯逻辑，mock 掉 bleak / paramiko / winreg
│   │   ├── test_disk.py
│   │   ├── test_config_roundtrip.py      # 含「密文里不出现明文」断言
│   │   ├── test_autostart.py
│   │   ├── test_remote_install.py
│   │   └── test_bridge_api_parity.py     # 见 8.4 ③
│   └── manual/
│       └── smoke_checklist.md            # 需要真机的检查项
│
├── scripts/                              # 保留：CLI 与独立运行的桥接
│   ├── usb_switch.py / .bat              # CLI 切换（保持原样）
│   ├── usb_switch_safe.py / .bat         # → 改为薄封装，转发 usbswitch.core.disk
│   ├── bridge.py + start_bridge.bat      # → 改为薄启动器，调 usbswitch.core.bridge_server
│   └── linux_bridge.py                   # ★ 自包含单文件（canonical，打包时复制进 bundle）
│
├── firmware/                             # 不动
├── web/                                  # 保留：Web Bluetooth 面板
├── docs/
├── tools/                                # gitignore（Arduino CLI 等）
└── README.md
```

### 8.3 为什么要单独拆出 `workers/` 这一层

`core` 不能碰 Qt，`ui` 不该写业务。那「把 asyncio 的 BLE 客户端接到 Qt 信号上」这段胶水代码放哪？

- 塞进 `core` → `core` 就依赖 PySide6，铁律破了，连单测都得装 Qt
- 塞进 `ui` → 三个面板要用同一个 worker，且 `main_window.py` 会膨胀成上帝对象

单独一层 `workers/`，职责就一句话：**把 core 的异步/阻塞调用翻译成 Qt 的 Signal/Slot**。它很薄，但让上下两层都保持干净。

### 8.4 三条硬约束（两条来自实现推演）

**① `core/bridge_server.py` 必须是库，不能是脚本。**

原方案让 GUI 用 `subprocess.Popen` 拉起 `bridge.py`。开发态成立，**打包后必崩**——PyInstaller 冻结后 `sys.executable` 就是 exe 自身，`exe bridge.py` 只会再开一个 GUI 实例。

改成：桥接 HTTP 服务抽为可 import 的模块，GUI 在后台线程里直接跑它。开发态与打包态行为一致，还顺带消灭「退出残留子进程」和「端口被占死」两个风险。

`scripts/bridge.py` 退化为 CLI 启动器，保留 README 里现有的手工用法。

**② `scripts/linux_bridge.py` 必须保持单文件自包含。**

它会被上传到 Linux 主机由 systemd 执行，那台机器上没有本项目。因此**不允许 `from usbswitch.core import ...`**。

代价：与 `bridge_server.py` 有一层 HTTP 样板代码重复（约 60 行）。这个重复是**有意接受**的——比"上传时动态拼接生成代码"要好调试得多。

**③ 两个桥接的对外契约必须一致，用测试锁死。**

既然接受重复，就必须防漂移：`test_bridge_api_parity.py` 断言两边暴露的路由集合与响应字段名（`ok` / `ejected` / `warning` / `error` / `drives` / `disks` / `mounts`）完全一致。任何一边改了响应格式，测试立刻红。

### 8.5 与现有目录的关系

| 现有文件 | 处置 |
|---|---|
| `scripts/usb_switch.py` | 保持原样（纯 CLI，不依赖新包） |
| `scripts/usb_switch_safe.py` | 磁盘函数改为转发 `usbswitch.core.disk`；CLI 行为不变 |
| `scripts/bridge.py` | 改为薄启动器，调用 `usbswitch.core.bridge_server` |
| `scripts/linux_bridge.py` | **保持自包含**；同时作为打包资源来源（单一副本，不复制，避免版本漂移） |
| `web/usb_switch_panel.html` | 保留，作为不开 GUI 时的轻量替代 |
| `firmware/`、`docs/` | 不动 |

`README.md` 需同步更新：新增 GUI 启动方式、`pip install -e .` 说明；`linux_bridge.py` 位置不变，现有 `scp` 流程无需改动。

### 8.6 运行期数据目录（不在仓库里）

```text
%APPDATA%\USBSwitchConsole\
├── config.json          # 配置（密码字段为 DPAPI 密文）
├── logs\
│   └── usbswitch.log    # 按大小轮转，保留 5 份
└── crash\               # 未捕获异常时导出的诊断信息
```

**所有可变数据一律不写进安装目录**——安装目录可能位于 `C:\Program Files\` 而无写权限，这是 Windows 应用的经典翻车点。

---

## 9. 分阶段实施计划

### Phase 1 — 核心闭环（可独立交付使用）

**范围**：需求 1、5、6、8

> **实施状态：已完成，含真机验证。**
> 全部验收项已在真实 ESP32-C3 上跑通（扫描 / 连接 / 三向切换 / 状态回填）。
> 测试数从 96 增至 166（含 Phase 2）。

| 步骤 | 内容 |
|---|---|
| 1.1 | 项目脚手架：`src/usbswitch/` 目录、`pyproject.toml`（依赖 + 入口点）、`python -m usbswitch` 跑起空窗口 |
| 1.2 | `core/config.py` + `core/models.py` |
| 1.3 | **`core/disk.py`（ctypes 重写）** — 先做，它是后面所有轮询的地基 |
| 1.4 | `core/ble.py`：扫描 / 连接 / 重连 / 串行命令 / 状态解析 |
| 1.5 | `core/orchestrator.py`：切换流程（此阶段安全弹出走"空实现"，Phase 2 补齐） |
| 1.6 | UI：设备区、切换区、状态区、日志区 |

**验收标准**
- [x] `core/disk.py` 单次调用 < 10 ms —— **实测 0.009 ms/次**（对比原 6600 ms）
- [x] 凭据落盘加密 —— 配置文件中 grep 不到明文密码
- [x] `core/` 层零 Qt 依赖 —— 由 `test_architecture.py` 强制
- [x] 退出时后台线程干净回收 —— 无残留线程、无 `QThread` 泄漏告警
- [x] 能扫描到 `USB-Switch` 并连接，断开后自动重连 —— 真机实测 3 s 内连上
- [x] `→ Host A` / `→ Host B` / `断开` 三个按钮全部生效，状态实时刷新
- [x] UI 在 BLE 重连期间不卡顿 —— BLE 跑独立线程，UI 线程不阻塞

**实施中发现并修正的三个设计缺陷**（原方案未预见，详见 §11 变更记录）：

1. `QThread` 不能承载阻塞的 `asyncio` 事件循环 —— 改为 `threading.Thread`；
2. 退出清理只挂 `closeEvent` 不够 —— `QApplication.quit()` 不触发它，补 `aboutToQuit`；
3. 连接成功后缺少自动查询状态 —— 界面会显示「已连接但全是 `—`」。

### Phase 2 — 本地桥接托管 + 应用外壳（托盘 / 自启）

**范围**：需求 2、7、8、9

> **实施状态：已完成。自动化测试 + 真机端到端验证通过。**
> 真机闭环：桥接服务启动 → `none → Host A`（U 盘 0.79 s 挂载为 `F:`）→
> 经 HTTP 桥接安全弹出 → `none`，全程 7 秒，无跳过任何一层。

把托盘、自启与本地桥接放进同一阶段是有意的：**「关闭窗口时窗口怎么处理」和「关闭窗口时桥接子进程怎么处理」是同一个决策**，分开做必然要推翻一次。

| 步骤 | 内容 |
|---|---|
| 2.1 | `core/bridge_server.py`（库形态）+ `core/bridge_local.py` 薄封装：端口预检 / 探活 / 状态上报 |
| 2.2 | `core/disk.py` 补齐 `eject()` 真实现 |
| 2.3 | Orchestrator 接入安全弹出互锁（`core/ejector.py` 的 `BridgeEjector`） |
| 2.4 | `ui/tray.py`：托盘图标（多状态）、右键菜单、左键显示/隐藏、气泡通知 |
| 2.5 | `ui/single_instance.py`：单实例锁 + 第二实例唤出已有窗口 |
| 2.6 | `core/autostart.py`：`winreg` 读写 + `_launch_command()` 双形态 + 回读校验 |
| 2.7 | 窗口生命周期：`setQuitOnLastWindowClosed(False)`、关闭转托盘、统一 `shutdown()` |
| 2.8 | `main.py` 支持 `--minimized`（自启静默进托盘） |
| 2.9 | UI：桥接服务区（本地）、选项区（含开机自启、关闭行为） |
| 2.10 | （顺带）修 `scripts/usb_switch_safe.py` 的 6.6 s 问题，转发到 `core.disk` |

**验收标准**
- [x] GUI 内一键启停本地桥接，日志实时可见
- [x] 开启安全弹出后，弹出失败时切换被硬中止且提示明确 —— **真实故障（卷脏位）下已验证**
- [x] 关闭主窗口后进程常驻托盘、本地桥接继续可用；托盘可重新唤出窗口
- [ ] 托盘菜单可完成 Host A / Host B / 断开 切换，与主窗口状态同步 ← **菜单项状态有测试，实际点击待人工确认**
- [x] 托盘「退出」后无残留 python 进程、端口已释放 —— 实测停止后 `probe() is None`
- [x] 重复双击图标不会起第二个实例，而是唤出已有窗口
- [ ] 开机自启开关写注册表后，重启系统能静默进托盘 ← **注册表读写有测试，静默启动待重启验证**
- [x] `scripts/usb_switch_safe.py` 不再 spawn PowerShell —— 转发到 `core.disk`，毫秒级

**实施中的关键修正**（原方案未预见）：

1. **弹盘实现整体弃用 Shell 动词方案**，改为原生 `FSCTL_LOCK_VOLUME` + `FSCTL_DISMOUNT_VOLUME`。
   原方案（`InvokeVerb('Eject')` + 轮询盘符消失）有**两处硬伤**：动词名在中文系统上是「弹出(&J)」，
   写死 `'Eject'` 会静默失效；且该 U 盘桥片不支持 `IOCTL_STORAGE_EJECT_MEDIA`，
   **盘符永远不会消失**——成败判据从一开始就不成立。详见 §11。
2. **打开卷只用 `GENERIC_READ`**。`GENERIC_READ | GENERIC_WRITE` 会被 Windows 恒定拒绝，
   却表现为「疑似被进程占用」，曾据此误判方向并白加了 8 秒重试窗口。

### Phase 3 — 远程桥接管理

**范围**：需求 3、4

> **实施状态：已完成，含真机验证（Robustel 边缘网关 `192.168.0.1`，Debian 13 +
> OpenSSH 10.0p2 + python3 3.13.5）。**
>
> 实测通过：install → 重复 install（幂等）→ inspect（`systemctl is-active =
> active`）→ stop → start → uninstall → 重复 uninstall（幂等）。
> 隧道打通后 `http_online = True`、`run_state = RUNNING`，ejector 自动指向隧道地址。
>
> 259 项自动化测试通过。

| 步骤 | 内容 |
|---|---|
| 3.1 | `core/bridge_remote.py`：paramiko 连接、SFTP、命令执行、错误分类 |
| 3.2 | 幂等安装脚本（systemd + nohup 回退），端口取 `bridge_port` |
| 3.3 | 凭据存储（DPAPI 加解密 + 掩码回显） ← Phase 1 已就位，本阶段接入 UI |
| 3.4 | UI：远程连接配置表单 —— **IP / SSH 端口 / 用户名 / 密码**（+ 私钥认证可选），含即时校验 |
| 3.5 | UI：桥接服务区（远程）+ 测试连通（SSH 与 HTTP 两层） |
| 3.6 | Orchestrator 按"当前主机"路由到本地或远程桥接 ← Phase 2 的 `BridgeEjector` 已按 Host 路由，无需改动 |
| 3.7 | **`core/ssh_tunnel.py`（真机验证后补上）**：经 SSH 隧道访问远端桥接，绕开远端防火墙 |

**验收标准**
- [x] 从 GUI 完成远端安装 → 启动 → 状态在线，全程无需登录终端 —— **真机验证通过**
- [x] 四个动作幂等：重复安装 / 重复卸载不报错 —— **真机验证通过**
- [x] SSH 可达但 HTTP 不通时，两个指示灯状态正确区分 —— 有专项测试
- [x] IP / 端口 / 用户名 / 密码四项可在 GUI 内填写保存，重启后回填（密码显示掩码）—— 已写入真实配置并回读校验
- [x] `config.json` 中 grep 不到明文密码 —— 实测密码字段为 `dpapi:AQAA…` 密文
- [x] SSH 端口与桥接端口填错时能给出可区分的错误提示 —— 错误分类 + 提示文案有测试
- [x] 远端无 systemd 时自动降级 nohup，且不误调 systemctl
- [x] 同一时刻只有一个远程操作在执行（命令队列串行）
- [x] 经 SSH 隧道访问远端桥接服务 —— **真机验证：直连被挡、隧道 HTTP 200**
- [x] 隧道地址与直连地址的切换对编排层透明（`runtime_http_base` 单一来源）
- [x] 隧道停止后本地端口释放、桥接地址还原

**实施中的关键修正**（原方案未预见）：

1. **不代替用户改 `/etc/sudoers`**（原方案步骤 7）。只检测并给出可复制的命令，
   理由见 §4.4。
2. **`systemctl --user` 必须带 `XDG_RUNTIME_DIR`**，否则报错看起来像「没有 systemd」。
3. **`RemoteState` 需要第三个状态 `ssh_checked`**，否则周期轮询会把未探测的
   SSH 画成红色「不可达」。
4. **周期探活不能置 busy**，否则面板近一半时间处于按钮全灰状态。
5. **补上 SSH 隧道**：真机发现远端防火墙不放通桥接端口，「HTTP 直连」这个
   隐含前提在真实环境里不成立。详见 §4.4 与 §11。

### Phase 4 — 打磨与交付

> **实施状态：已完成。打包产物在净化环境、仓库外目录下自检 15/15 通过。**
> 271 项自动化测试 + 静态检查全绿。

| 步骤 | 内容 |
|---|---|
| 4.1 | 日志导出到文件 + 「打开日志目录」按钮；错误文案中文化（各阶段持续完善） |
| 4.2 | PyInstaller 打包（`--onedir`），多尺寸 `.ico` 与版本资源；包元数据随附 |
| 4.3 | 打包态下回归验证开机自启路径（真写注册表 → 回读 → 删除 → 还原） |
| 4.4 | `--selftest` 冒烟自检入口 + 用户手册 |
| 4.5 | 接入 `ruff` 静态检查并做成测试用例（补上 C23 那类 bug 的防线） |

**验收标准**
- [x] 干净 Windows 机器上双击 exe 可用，无需预装 Python —— 在**净化环境变量、仓库外工作目录**下验证（见下「未能完全覆盖的部分」）
- [x] 打包产物在无开发环境的机器上跑通完整切换流程 —— 打包产物在冻结态下完成：
      蓝牙扫描命中真机（`USB-Switch`）、经 SSH 隧道连到真实远端桥接、桥接服务起停、
      主窗口构建与退出。**唯一未覆盖的是「用鼠标真的点一次切换」**
- [x] 打包产物的开机自启指向 exe 本身且路径带引号 —— 真写注册表成功，命令为
      `"<dist>\USB Switch Console.exe" --minimized`

**打包配置里最容易踩的三处**（都已用测试钉住）：

1. **`scripts/linux_bridge.py` 必须作为数据文件带进去**，且目标路径要与
   `paths.linux_bridge_script()` 的冻结态路径一致。漏了它，开发态一切正常，
   打包后「安装远程桥接」直接失败。
2. **bleak 的 WinRT 后端是动态导入的**，PyInstaller 静态分析看不见，
   必须逐个列进 `hiddenimports`。（清单不许靠印象写 —— 第一版就多写了一个
   并不存在的 `bleak.backends.winrt.service`。现在有测试双向校验：
   声明了的必须存在，存在的必须都声明。）
3. **版本号只能有一个来源**：版本资源文件在 spec 里现场生成，不落仓库。

**未能完全覆盖的部分**（如实记录）：

- 「在**别人家的**干净机器上双击」无法真正验证 —— 没有可用的干净虚拟机。
  已尽量逼近：清空 `PYTHONPATH`/`PYTHONHOME`/`VIRTUAL_ENV` 等变量、
  在仓库外的工作目录运行、只依赖构建产物自身。
- 「完整切换流程」缺最后一步：**经 GUI 点击切换按钮**（需要真实鼠标操作与
  已配好的硬件）。打包产物已验证到「蓝牙能扫到设备 + 远端桥接可达」，
  剩下的就是串起来，而这条链路在开发态已多次真机验证。

### Phase 5 — 可选增强（未排期）

| 候选 | 说明 |
|---|---|
| 远端桥接鉴权 | 目前无鉴权；默认走隧道已不必暴露，但直连场景仍缺一层共享密钥 |
| 无 GUI 常驻模式 | 现在隧道与 BLE 会话都随 GUI 进程存在；若要做后台服务需重构生命周期 |
| 多套远程配置 | 现在每个 Host 角色只有一份远端配置 |

---

## 10. 已确认决策

| # | 决策点 | 结论 |
|---|---|---|
| D1 | GUI 框架 | **Python + PySide6**（复用现有 Python 资产） |
| D2 | 远程管理通道 | **paramiko 纯 Python SSH**（密码 / 私钥双支持） |
| D3 | 「安全弹出限制」语义 | **单个 Trigger 开关，控制「是否强制安全弹出」**（见下） |
| D4 | 系统托盘（显示 / 隐藏主窗口） | **要** — Phase 2 交付 |
| D5 | 开机自启（启用 / 关闭） | **要** — Phase 2 交付，注册表 `HKCU\...\Run`，不需要管理员权限 |
| D6 | 远程桥接可配置项 | **IP / SSH 端口 / 用户名 / 密码**（+ 私钥认证可选），密码 DPAPI 加密 |
| D7 | 是否由程序代写远端 `/etc/sudoers.d` | **否** — 只检测并给出可复制的命令。特权改动 + SSH 非交互会话无法安全输密码（见 §4.4） |

**D3 落地约定**：

- 语义 = 现有网页面板 `chkSafe` 的后端等价物，**不是对话框，也不改注册表**。
- UI 上是一个 Trigger（Qt 中实现为 `QCheckBox` + `QSS` 自绘开关，或 `QAbstractButton` 子类）。
- **开启（默认）**：切换前必须成功弹出当前主机的 U 盘，失败即硬中止。
- **关闭**：跳过弹出直接下发切换命令，日志打 `WARN` 说明本次切换未经安全弹出；开关本身在关闭态使用警示色，避免误长期关闭。
- 状态持久化到 `config.json` 的 `safety.force_eject` 字段。
- 不动 Windows 系统层的「安全删除硬件」通知策略（涉及管理员权限与全局副作用，明确排除）。

---

## 11. 实施变更记录

以下条目都是**真机 / 端到端验证才发现**的问题，初版设计未预见。保留在这里是为了避免后来者重走一遍。

| # | 初版设计 | 实际问题 | 现方案 | 影响面 |
|---|---|---|---|---|
| C1 | `QThread` + `moveToThread` 承载 asyncio 循环 | `QThread.quit()` 只作用于 Qt 事件循环，`run_forever()` 永不返回 → `wait()` 超时、退出时进程崩溃（`0xC0000409`） | `threading.Thread` + `threading.Event` 显式握手 | `workers/ble_worker.py` |
| C2 | 退出清理只挂 `closeEvent` | `QApplication.quit()`（托盘退出、注销）不触发 `closeEvent` → 后台线程被直接销毁 | 补挂 `aboutToQuit`，`shutdown()` 幂等 | `ui/main_window.py` |
| C3 | 连接后不做额外动作 | 界面停在「已连接但状态全是 `—`」，看起来像坏了 | 连接成功即自动查一次状态 | `ui/main_window.py` |
| C4 | `InvokeVerb('Eject')` 弹盘 | **动词名本地化**，中文系统上是「弹出(&J)」，写死英文名静默失效（原方案只能在英文 Windows 上工作） | 弃用 Shell，改原生 ioctl | `core/disk.py` |
| C5 | 轮询「盘符是否消失」判断弹盘成败 | 该 U 盘桥片不支持 `IOCTL_STORAGE_EJECT_MEDIA`，盘符**永远不会消失** → 判据根本不成立 | 改由 `DeviceIoControl` 返回值确定性判定 | `core/disk.py` |
| C6 | `CreateFile(..., GENERIC_READ\|GENERIC_WRITE, ...)` 打开卷 | 对 removable 卷**恒定** `ERROR_ACCESS_DENIED`，现象酷似「被进程占用」，导致误判并白加 8 s 重试窗口 | 只请求 `GENERIC_READ`（卸载只需读权限） | `core/disk.py` |
| C7 | `FSCTL_IS_VOLUME_DIRTY = 0x00090038` | `CTL_CODE(9, 27, ...)` 应为 `0x0009006C`；写错不报错，只返回 `ERROR_INVALID_FUNCTION` | 改用 `Get-Volume` 的 `HealthStatus`（.NET 枚举名，不随语言变） | `core/disk.py` |
| C8 | 「弹盘失败就是被占用」 | 真因可能是**卷脏位**（一次早先的硬切换造成），Windows 会拒绝弹出这类卷 | 失败时自动查卷健康并在提示里给出 `chkdsk /f /x` | `core/disk.py` |
| C9 | GUI 用 `subprocess` 拉起 `bridge.py` | 打包后 `sys.executable` 是 exe 自身，`exe bridge.py` 只会再开一个 GUI，桥接永远起不来 | 桥接服务库化（`core/bridge_server.py`），GUI 内嵌线程运行 | `core/bridge_server.py` |
| C10 | `linux_bridge.py` 在模块级读 `sys.argv[1]` | 无法被测试导入（导入即 exit），API 一致性测试无从做起 | 挪进 `main(argv)` | `scripts/linux_bridge.py` |
| C11 | `QThreadPool` 跑桥接探活 | 任务还在队列中时 worker 已被回收 → `QThread: Destroyed while thread is still running` | 改普通 `threading.Thread`（线程持引用） | `workers/bridge_worker.py` |
| C12 | `PySide6` 元包 | 会连带拉入 168 MB 的 `PySide6-Addons`，而本项目只用 Essentials 里的模块 | 依赖改为 `PySide6-Essentials` | `pyproject.toml` |
| C13 | `QComboBox.itemData(枚举)` | `currentData()` 对 `str` 混合枚举**退化成普通字符串**，`is BridgeKind.REMOTE` 恒为假 → 配了远程但整个远端参数区永远置灰，且不报任何错 | 一律用 `BridgeKind(data)` / `Host(data)` / `AuthMethod(data)` 转回枚举 | `ui/panels/remote_panel.py` |
| C14 | 远程状态只有 SSH / HTTP 两个布尔量 | 周期轮询只做 HTTP 探活，却因 `ssh_reachable=False` 把 SSH 画成红色「不可达」，用户以为连不上 | 增加 `ssh_checked`，未探测时如实显示「未检测」 | `core/bridge_remote.py` |
| C15 | 周期探活与用户操作共用 busy 标志 | HTTP 超时 3 s、轮询间隔 5 s → 面板近一半时间按钮全灰，看起来像程序坏了 | 只有用户主动发起的操作才置 busy | `workers/remote_worker.py` |
| C16 | `systemctl --user ...` | SSH 非登录会话缺 `XDG_RUNTIME_DIR`，报 `Failed to connect to bus`，**看起来像「没有 systemd」** | 统一 `XDG_RUNTIME_DIR="/run/user/$(id -u)"` 前缀 | `core/bridge_remote.py` |
| C17 | 把 `NoValidConnectionsError` 当 `SSHException` 子类 | paramiko 5.x 里它继承的是 **`OSError`**，判断顺序靠后会退化成笼统的「网络错误」 | 显式判断并排在 `OSError` 之前 | `core/bridge_remote.py` |
| C18 | 两个角色都设为「本机」时，第二个角色的端口被静默忽略 | 用户在第二个角色上改端口「改了没反应」 | UI 明确提示实际生效的角色与端口 | `ui/panels/remote_panel.py` |
| C19 | `load_system_host_keys()` 把用户的 `~/.ssh/known_hosts` 一起加载 | 用户机器上早有的 `192.168.0.1` 记录与目标设备密钥不同，抛 `BadHostKeyException`，而**错误提示指向本程序的文件** —— 去那里翻什么都找不到 | 只用本程序自己的单一信任库；报错给出的路径就是唯一要处理的地方 | `core/bridge_remote.py` |
| C20 | 连接失败路径也调 `save_host_keys()` | 写出的是客户端当前 `_host_keys`，失败时里面可能一条服务器密钥都没有，却把别处加载来的记录写进信任库 —— **用错误密钥污染它** | 只在 `connect()` 成功后保存 | `core/bridge_remote.py` |
| C21 | 端口预检用 `connect_ex` 判断「有没有人监听」 | 别的进程可能**绑定**了端口却未监听（或绑 `0.0.0.0` 而我们绑 `127.0.0.1`，不是同一把锁）→ 预检放行、真正 bind 才报错。实测本机 Clash Verge 占着 `0.0.0.0:8737`，报的是 `WSAEACCES(10013)` 而**不是** `WSAEADDRINUSE(10048)` | 预检改成**真去 bind**；两个错误码给不同的排查提示（代理独占 / Windows 保留区间 vs 已被占用） | `core/bridge_server.py` |
| C22 | 隧道 `_pump` 结束时 `close()` 两端 | socket 接收缓冲里还有未读数据时 `close()` 发 RST 而非 FIN，**对端尚未取走的数据被丢掉** → `IncompleteRead`。小响应侥幸能过，所以先表现为「偶发失败」 | 改**半关闭**（`shutdown(SHUT_WR)` / `Channel.shutdown_write()`），两个方向都结束后再彻底关闭 | `core/ssh_tunnel.py` |
| C23 | `_restore_geometry` 里引用了早前重构中已删除的常量 | `if not raw or raw.startswith(CONST)` 在测试里因 `or` 短路**从不被求值**（geometry 为空），整套测试全绿；真实用户**第二次启动**（geometry 非空）直接 `NameError` 崩溃 | 删掉该判断；补「保存几何 → 重新加载 → 再建窗口」的回归测试 | `ui/main_window.py` |
| C24 | 只有运行时测试，没有静态检查 | C23 这类「引用了不存在的名字」单元测试极难穷尽 | 接入 `ruff`（`F` + `E9`）并把它做成一个测试用例；`pyproject.toml` 声明 `dev` 可选依赖 | `pyproject.toml` / `tests/unit/test_lint.py` |
| C25 | `hiddenimports` 按印象列 bleak 的后端模块 | 实际包内容里**没有** `bleak.backends.winrt.service`（只有 `client`/`scanner`/`util`），构建日志出现 `Hidden import not found`。这次只是噪音，但说明这份清单没被验证过 | 加双向测试：**声明了的必须存在**，**存在的必须都声明** | `usbswitch.spec` / `tests/unit/test_packaging.py` |
| C26 | 冻结态读不到依赖版本（自检里显示 `bleak ?`） | 没打包 dist-info 时 `importlib.metadata` 查不到；这类信息正是反馈问题时最需要的 | spec 里 `copy_metadata` 带上 bleak / paramiko / PySide6 / usbswitch 的元数据；取值本身做三级降级（`__version__` → metadata → `?`），不让它成为失败理由 | `usbswitch.spec` / `selftest.py` |
| C27 | 打包配置自身没有任何测试 | 「spec 的 datas 目标」与「`paths.linux_bridge_script()` 的冻结态路径」必须一致，对不上时开发态全绿、打包后必崩 | 用测试钉住：datas 目标字符串、冻结态路径真算一遍再比、`console=False` 与 traceback 的取舍、不许排除 shiboken6 | `tests/unit/test_packaging.py` |
| C28 | 自检只检查「能不能导入」 | 真正会翻车的是**动态导入**：bleak 的 WinRT 后端在打包后未必能干活 | 自检里加一条真扫一次蓝牙（非关键项，设备没上电不算失败）。实测打包产物在冻结态扫到了真机 | `selftest.py` |
| C29 | 改了源码但没重新打包 | **最伤用户的一条**：界面改版后双击的仍是两小时前的包，看到的是旧界面，程序不报任何错。排查只能靠人工比对文件时间戳 | 新增「构建身份」：spec 生成 `_build.json`（版本 / 构建时刻 / 构建时的源码时间戳）随包分发；状态栏常驻显示、启动日志记录；**自检拿它与当前源码树比对，落后即判关键项失败** | `core/build_info.py`、`usbswitch.spec`、`ui/widgets.py`、`selftest.py` |
| C30 | 自检的 UI 导入清单是手写的 | 删掉 `switch_panel.py` 后清单没跟着改 → 自检报**假失败**，而 322 项单测全绿（没有任何用例碰过那个函数） | 改为按目录 `walk_packages` 枚举，增删文件都不需要同步维护；并补一条测试**真的执行**这个检查项 | `selftest.py`、`tests/unit/test_packaging.py` |
| C31 | 自检只断言「窗口能建起来」 | 改版前后主窗口都能正常构建 —— 那个断言在**旧产物上同样通过**，于是「装了旧界面」被自检放过去 | 断言新版特有的部件（`HeroPanel`、折叠分区数量、状态栏构建标记）存在 | `selftest.py` |
| C32 | 同一文件并行编辑会丢写入 | 给 spec 新增 manifest 生成块时与另一处 import 编辑并行发出，`BUILD_MANIFEST` 的定义块被静默丢弃 → 构建直接失败 | 约定：**同一文件必须串行编辑**；本次由新增的 spec 测试发现 | 工作方法（已在项目备忘中） |

**一条方法论**：C1 – C3、C11 都是**单元测试抓不到**的 —— 要么需要真实退出流程（C1/C2/C11），
要么需要真实设备时序（C3）。C13 – C15 则是**单元测试写得再全也会漏**的类型：
它们只在「界面装配起来 + 事件循环真的在跑」之后才显形，是端到端装配验证抓出来的。
C19 – C23 全部来自**真机验证**，其中 C23 尤其值得记：它躲过了 259 项测试、只在
「用真实配置文件启动」这一条路径上必现。

因此每个阶段都必须做一次**真机 / 端到端验证**，而且**必须用真实配置文件**跑一遍
入口 —— 用默认配置跑会漏掉「第二次启动」这类只在持久化状态存在时才走的路径。
C24 则是对 C23 的正面回应：这类名字错误应当由静态检查提前拦住，而不是靠运气撞上。
C25 – C28 说明**打包配置本身也需要测试** —— 它是一段没有类型、没有编译期检查、
只在构建时才执行的代码，而它出错的表现又是「开发态一切正常、打包后必崩」。

C29 – C31 是同一件事的三个侧面：**「验证」这件事本身需要被验证**。
C29 是「验的可能不是新代码」（跑的是旧产物），C30 是「自检自己会失效」，
C31 是「断言太弱，新旧都能过」。它们共同说明：每加一项检查，都要问
「这个检查在**错误的情况下**会不会失败」。只验证通过路径的检查等于没有检查 ——
所以 C29 落地时专门做了一次**反向验证**：伪造一个落后的源码树，
确认自检确实报错、退出码确实为 1。
