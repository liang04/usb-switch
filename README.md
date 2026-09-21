# USB-A 双主机 U 盘切换器

一个 USB-A 接口的 U 盘双主机切换器：一枚 U 盘可以在两台主机（Host A / Host B）之间切换，由 ESP32-C3 通过 BLE 蓝牙控制，并在切换前自动在操作系统层面安全弹出 U 盘。

```text
Host A USB-B ── ESD 保护 ──┐
                           ├── CH442E (数据 2:1 开关) ── USB-A 母座 (U盘)
Host B USB-B ── ESD 保护 ──┘

Host A VBUS ── SY6280AAC (U2) ──┐
                                ├── UDISK_VBUS
Host B VBUS ── SY6280AAC (U3) ──┘

ESP32-C3 ── J3 排针 (SEL_IN / NEN / VBUS_x_EN / HOSTx_DET)
```

## 硬件要点

- **数据通道**：CH442E 双路 2:1 模拟开关切换 D+/D-，`NEN` 低有效使能，`SEL_IN` 选择 A/B
- **电源通道**：两片 SY6280AAC 分别控制两路 Host VBUS，限流约 0.68 A，**严禁两路同时导通**
- **主机检测**：Host VBUS 经 47 kΩ / 68 kΩ 分压送 `HOSTA_DET` / `HOSTB_DET`（5 V 时约 2.96 V）
- **安全时序**：先断数据 → 断两路 VBUS → 等待 U 盘放电 → 设置 SEL → 开目标 VBUS → 稳定后接通数据

完整的电路连接、引脚分配、切换时序与调试检查清单见
[docs/ESP32-C3_实际电路连接与控制设计.md](docs/ESP32-C3_实际电路连接与控制设计.md)。

## 目录结构

```text
├── src/usbswitch/                           # Windows 桌面客户端（Python + PySide6）
│   ├── core/                                #   纯逻辑层，零 Qt 依赖，可单测
│   ├── workers/                             #   Qt 适配层（asyncio ↔ Signal/Slot）
│   ├── ui/                                  #   表现层
│   ├── selftest.py                          #   自检（打包产物的验证入口）
│   └── resources/                           #   app.ico（由 scripts/make_icon.py 生成）
├── packaging/launcher.py                    # 打包入口脚本
├── usbswitch.spec                           # PyInstaller 配置
├── tests/                                   # pytest 单元测试
├── docs/
│   ├── 用户手册.md                          # 面向使用者
│   ├── Windows客户端_实施方案.md            # 设计与实施记录（含变更记录）
│   ├── ESP32-C3_实际电路连接与控制设计.md   # 电路设计文档（接线和固件的依据）
│   └── datasheets/                          # CH442E / SY6280 / USBLC6 数据手册
├── firmware/
│   └── USBSwitch/USBSwitch.ino              # ESP32-C3 固件（Arduino 框架 + NimBLE）
├── scripts/
│   ├── usb_switch.py / .bat                 # Windows 蓝牙控制脚本（a/b/x/s 命令）
│   ├── usb_switch_safe.py / .bat            # 安全切换：先弹出 U 盘，成功才切换
│   ├── bridge.py + start_bridge.bat         # 本机桥接服务的 CLI 启动器
│   ├── linux_bridge.py                      # Linux 端桥接服务（自包含单文件，会被上传）
│   └── make_icon.py                         # 生成 exe 图标
└── web/
    └── usb_switch_panel.html                # Web Bluetooth 控制面板（Chrome/Edge）
```

## 工作原理

```text
网页/脚本发命令 ──BLE GATT──> ESP32-C3 ──GPIO──> CH442E / SY6280 ×2
      │
      └── 切换前: 先请求「当前主机」的桥接服务弹出 U 盘
          (Host A → http://127.0.0.1:8737, Host B → http://<Host B IP>:8738)
          弹出失败则中止切换，绝不动 VBUS
          （该主机的「安全弹出锁」未启用时跳过弹出，仅告警）
```

角色是固定的：**Host A 恒为本机（Windows）**，**Host B 恒为远程（Linux）**。
安全弹出锁因此也是派生的——A 跟随本机桥接服务启停，B 跟随远程桥接的启用与配置。

ESP32-C3 只控制电气连接，无法代替操作系统弹出 U 盘，因此「安全弹出」互锁放在电脑端：
每个 Host 上运行一个桥接服务，网页/脚本在发切换命令前先调用它。

## 快速开始

### 1. 固件编译与烧写（Windows）

需要 Arduino CLI 与 ESP32 核心（本仓库 `tools/` 目录被 gitignore，需自行安装）：

```bat
REM 下载 arduino-cli: https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Windows_64bit.zip
arduino-cli config init
arduino-cli config set board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32
arduino-cli lib install "NimBLE-Arduino"

REM 编译（CDCOnBoot=cdc 用于通过原生 USB 输出串口日志）
arduino-cli compile --fqbn "esp32:esp32:esp32c3:CDCOnBoot=cdc" firmware/USBSwitch

REM 烧写（按实际串口号修改）
arduino-cli upload -p COM3 --fqbn "esp32:esp32:esp32c3:CDCOnBoot=cdc" firmware/USBSwitch
```

### 2. 启动桥接服务

**Host A（Windows）**：双击 `scripts/start_bridge.bat`。桥接服务本体是
`src/usbswitch/core/bridge_server.py`，只用标准库，**不需要安装任何第三方包**。

**Host B（Linux）**：推荐直接在 GUI 的「远程桥接服务」里填好 IP / SSH 端口 /
用户名 / 密码，然后点「安装」——它会自动完成上传、注册 systemd 服务、启动与验证。

不想用 GUI 时也可以手工部署：

```bash
scp scripts/linux_bridge.py <user>@<Host B IP>:~/usb_switch_bridge.py
ssh <user>@<Host B IP> "nohup python3 ~/usb_switch_bridge.py > ~/usb_switch_bridge.log 2>&1 &"
```

如 U 盘在 Linux 上被挂载，`umount` 需要 root，建议配置免密 sudo（`<user>` 替换为实际用户名）。
程序**不会**替你改 `/etc/sudoers` —— 这是特权改动，写坏会导致连 `sudo` 都用不了，
所以只会在日志里给出下面这条命令，由你自行执行：

```text
echo '<user> ALL=(ALL) NOPASSWD: /bin/umount' | sudo tee /etc/sudoers.d/usb-switch && \
sudo chmod 0440 /etc/sudoers.d/usb-switch && sudo visudo -c
```

### 3. 使用

- **网页面板（推荐）**：用 Chrome/Edge 打开 `web/usb_switch_panel.html`，
  点「连接设备」选择 `USB-Switch`。面板会自动检测两个桥接服务状态，
  切换前自动在当前主机上弹出 U 盘。桥接地址可在面板上修改并自动保存。
- **命令行**：

  ```bat
  scripts\usb_switch_safe.bat a   REM 弹出 U 盘后切到 Host A
  scripts\usb_switch_safe.bat b   REM 弹出 U 盘后切到 Host B
  scripts\usb_switch.bat x        REM 直接断开（不弹出，仅调试固件时用）
  scripts\usb_switch.bat s        REM 查询状态
  ```

## Windows 桌面客户端

把「连接 ESP32 / 安全弹出 / 切换 / 查看状态」收敛进一个 GUI。
**使用说明见 [docs/用户手册.md](docs/用户手册.md)**；
实施方案见 [docs/Windows客户端_实施方案.md](docs/Windows客户端_实施方案.md)，
界面改版见 [docs/界面优化方案.md](docs/界面优化方案.md)。

版面按**使用频率**分四层（顶栏 / U 盘连接三卡 / 折叠配置区 / 状态栏）。
折叠态下内容高约 326px，可视区约 687px —— 最常用的切换操作不滚动即可见。

### 打包为 exe

> ⚠️ **改了 `src/usbswitch/` 下的任何代码，都必须重新打包才能生效。**
> 开发态（`python -m usbswitch`）跑的是源码，随手改随手生效；打包产物是一份
> **快照**。两者不符时程序**不会报错**，只会让你觉得「界面没变」——
> 这条真的踩过：界面改版后双击的仍是两小时前的包。自检里有一项专门盯它。

```bat
.venv\Scripts\python.exe scripts\make_icon.py     REM 生成 exe 图标（改了图标绘制代码后重跑）
.venv\Scripts\python.exe -m PyInstaller usbswitch.spec --noconfirm
```

产物在 `dist\USB Switch Console\`（onedir，约 130 MB）。
**必须整个目录一起分发**，不能只拷 exe。

打包前请先退出正在运行的实例，否则 exe 被占用、替换会失败。
若是原地重建，先把 `dist` **改名**（不是删除）腾位置，再用全新 `--workpath`，
这样 PyInstaller 不需要清理任何旧文件。

### 构建身份：怎么知道「我跑的是哪一版」

程序在三个地方报出构建身份：

| 位置 | 形式 |
|---|---|
| 状态栏最右侧（常驻） | `v0.1.0 · 构建 09-18 12:39`（打包态）/ `v0.1.0 · dev 09-18 12:36`（开发态） |
| 启动日志 | `构建身份：v0.1.0，打包态，构建于 …，源码 …` |
| `--selftest` 的 `build 身份` 一项 | 完整描述；**产物落后于源码时直接判失败** |

机制：`usbswitch.spec` 在构建时把 `{version, built_at, source_stamp}` 写进
`packaging/_build.json` 并随包分发（`core/build_info.py` 读它）。自检拿
`source_stamp` 与当前源码树比对，落后就报 `请重新打包后再验证`。
在没有源码树的机器上该项如实跳过，不误报。

### 自检（打包产物的验证手段）

窗口态 exe 没有控制台，所以自检把结果写进文件：

```bat
"dist\USB Switch Console\USB Switch Console.exe" --selftest --gui --json selftest.json
```

开发态等价：`.venv\Scripts\python.exe -m usbswitch --selftest --gui --json selftest.json`

退出码 0 = 关键项全过。它检查**构建身份/是否落后于源码**、路径解析、配置、DPAPI、
依赖导入（含 bleak 的 WinRT 后端）、UI 与 workers 全树导入、桥接起停、磁盘、
自启命令、图标绘制、**新版版面部件**、主窗口离屏构建、真实蓝牙扫描，
以及（配置了远程时）真实建一次 SSH 隧道并探活。

加 `--autostart` 会额外真写一次注册表并**还原**（验收冻结态自启路径用）。

> **改了打包配置或依赖后请务必跑一遍** —— 开发态全绿、打包后崩掉是这类项目
> 最典型的失败方式。

> `USBSWITCH_SOURCE=<仓库路径>` 可以显式指定源码树位置，
> 于是**在仓库外**跑产物自检也能做落后检测。

### 环境准备

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .[dev]
```

`[dev]` 装上 `pytest`、`ruff` 与 `pyinstaller`。不开 `[dev]` 也能跑，
只是静态检查那条用例会跳过。

### 启动

```bat
.venv\Scripts\python.exe -m usbswitch
```

### 运行测试

```bat
.venv\Scripts\python.exe -m pytest tests
```

无显示器环境（CI、远程会话）需要先设置离屏渲染，否则 Qt 平台插件会初始化失败：

```bat
set QT_QPA_PLATFORM=offscreen
```

如果沙箱/安全软件拦下了 pytest 的临时目录回收，给一个**每次唯一**的 basetemp
即可绕开（它就不需要删任何东西了）：

```bat
.venv\Scripts\python.exe -m pytest tests --basetemp=%TEMP%\usbswitch-pt-%RANDOM%
```

### 架构约定（改代码前请先读）

1. **`core/` 层禁止 import 任何 Qt 符号。** `ui → workers → core` 是单向依赖。
   违反会被 `tests/unit/test_architecture.py` 直接拦下。
2. **全进程只允许一个 `BleakClient`。** Windows 的 WinRT 后端在并发客户端下会出现
   GATT 服务发现不完整。因此 GUI 运行时**不要**再去调用 `scripts/usb_switch.py`。
3. **要分发到别的机器上的文件必须自包含。** `scripts/linux_bridge.py` 会被上传到
   Linux 由 systemd 执行，那台机器上没有本项目，因此它不能 import `usbswitch.*`。
4. **打开卷只用 `GENERIC_READ`。** `GENERIC_READ | GENERIC_WRITE` 会被 Windows
   恒定拒绝（`ERROR_ACCESS_DENIED`），看起来却像「被进程占用」——曾据此误判并白白
   加重试窗口。实测只读句柄足以完成 `FSCTL_LOCK_VOLUME` + `FSCTL_DISMOUNT_VOLUME`，
   且不开写句柄反而更安全。`tests/unit/test_disk.py` 已锁死这条。
5. **从 `QComboBox` 取枚举值必须转回来。** 本项目的数据类枚举都是 `str` 混合枚举，
   `currentData()` 会把它们**退化成普通字符串**，于是 `is BridgeKind.REMOTE` 恒为假、
   相关控件永远置灰且不报错。一律写 `BridgeKind(combo.currentData())`。
6. **跨线程信号是队列投递的。** 测试里要验证 worker 发出的信号，必须泵 Qt 事件循环
   （`QApplication.processEvents()`），只 `sleep` 是收不到的。
7. **中继 socket 收尾用半关闭，不要直接 `close()`。** 接收缓冲里还有未读数据时
   `close()` 会发 RST 而不是 FIN，对端尚未取走的数据会被丢掉
   （表现为 `http.client.IncompleteRead`）。见 `core/ssh_tunnel.py` 的 `_pump`。
8. **测试不要假设 8737 / 8738 空闲。** 开发机上这类通用端口常被代理 / VPN /
   安全软件占着（实测本机 Clash Verge 绑了 `0.0.0.0:8737`）。用
   `conftest.pick_free_port()` 取一个当前空闲端口。
9. **改完代码先跑静态检查。** 有一类 bug 单元测试极难覆盖：引用了已被删除的名字。
   它可能躲在 `or` 短路的右侧，只在特定运行路径（比如「第二次启动」）才炸。
   `ruff` 的 `F` 规则能提前拦住：

   ```bat
   .venv\Scripts\python.exe -m ruff check src tests --select F,E9
   ```

### 配置文件

`%APPDATA%\USBSwitchConsole\config.json`，日志在同目录的 `logs\` 下。
SSH 密码等凭据使用 Windows DPAPI 加密存储，文件里不含明文。

每次保存前会把上一版留成 `config.json.bak` —— 配置里有一整台远端主机的
连接参数与密码密文，被改坏一次就得全部重填。

### 当前进度（Phase 1–4 + 界面改版已完成）

已实现：

| 能力 | 说明 |
|---|---|
| 设备扫描 / 连接 / 断线自动重连 | BLE Worker 跑独立线程，指数退避 |
| Host A / B / 断开 切换 | 三张大卡片直接标出 VBUS 与检测位；断开前经桥接服务安全弹出，失败即硬中止 |
| **按频率分层的版面** | 顶栏 + U 盘连接卡片 + 折叠配置区 + 常驻状态栏；折叠状态可持久化 |
| **危险操作二次确认** | 「断开 U 盘」默认按钮为「取消」；远程「卸载」同样要确认 |
| **键盘快捷键** | `Ctrl+1/2` 切主机、`Ctrl+R` 刷新、`Ctrl+L` 展开日志（断开刻意没有快捷键） |
| 状态实时显示 | 连接后自动回填，断开时卡片与状态栏一并清空 |
| 日志面板 | 实时追加、自动滚底、分级着色、导出到文件 |
| 本地桥接服务托管 | GUI 内一键启停，库形态内嵌，退出无残留 |
| 真实安全弹出互锁 | 走 `POST /eject`，本地与远程同一套 HTTP 契约 |
| 安全弹出锁按主机派生 | **Host A** 跟随本机桥接服务启停、**Host B** 跟随远程桥接的启用与配置；锁未启用则跳过弹出并告警（不中止切换），不再有全局开关 |
| 远程桥接总开关 | 面板上一键启用/禁用：禁用后 Host B 视为不使用远程桥接，弹出锁一并关闭，隧道停、远程操作置灰，参数完整保留 |
| 系统托盘 | 显示/隐藏主窗口、关闭转托盘、状态图标 |
| 开机自启 | 注册表 HKCU，免管理员，路径加引号并可回读校验 |
| 单实例锁 | 重复启动时唤出已有窗口，不产生第二个进程 |
| 远程（Linux）桥接管理 | GUI 内完成安装 / 启动 / 停止 / 卸载，幂等 |
| 远程连接配置 | **独立配置窗口**（不在主窗口占位）：IP / SSH 端口 / 用户名 / 密码（DPAPI 加密，掩码回显）/ 私钥可选 / 桥接端口 / 别名 |
| SSH 隧道 | 经 SSH 把远端桥接映射到本机端口，绕开远端防火墙（默认开启） |
| 三层连通性呈现 | 「SSH 可达」「隧道已打通」「桥接服务在线」是三个独立指示灯 |
| 打包为 exe | PyInstaller onedir，约 130 MB，无需预装 Python |
| 自检入口 | `--selftest`，打包产物的可自动化验证手段 |

用户使用说明见 **[docs/用户手册.md](docs/用户手册.md)**。

### 远程桥接总开关

「远程桥接服务」面板左上角有「**启用远程桥接**」勾选框。取消勾选后，Host B 整体
视为「不使用远程桥接」：切换前不再执行安全弹出、SSH 隧道立即拆掉、远程管理操作
置灰、三盏灯统一显示「已禁用」。**已填的连接参数完整保留**，重新勾选即恢复。

用途：远端长期没有 U 盘、或其桥接服务常常不可达时，弹出锁会卡住「从 Host B 切走」。
关掉总开关即可放行，不必把填好的 IP / 凭据删掉重填。**切换被拦下时提示文本里就写着
这条出路**，不必去翻文档。

注意「已禁用」与「未配置」在界面上是两种说法（黄色芯片 vs 空芯片）—— 看到
「已禁用」时不要去重填地址。两者在代码里都收敛到 `AppConfig.remote_host()` 返回
`None`，但配置本身完好无损。

### 关于 SSH 隧道（默认开启）

很多设备（尤其边缘网关 / 路由器）的防火墙只放通 22/80/443，桥接端口进不来。
实测一台 Robustel 边缘网关就是如此：设备上服务运行正常、设备本机
`curl 127.0.0.1:8738/status` 也正常，但从 Windows 直连 8738 被挡。

所以远端桥接**默认经 SSH 隧道访问**：隧道把远端 `127.0.0.1:<桥接端口>` 映射到
本机一个自动分配的端口，再从本机访问。好处是**不需要改设备配置**，
而且无鉴权的桥接 HTTP 服务不必暴露在网络上（建议同时把远端桥接改为只监听回环）。

在「远程桥接服务」里可以关掉隧道改用直连——那种情况下远端防火墙必须放通
桥接端口。面板会明确告诉你当前会连到哪一个地址。

**勾着隧道时不退回直连。** 隧道没建起来的那一刻，桥接灯显示「隧道未建立」
（而不是「无响应」），切换会被拦下并说明原因。理由是退回直连只会把真正的故障盖掉：
直连的失败形态是「连不上那台主机」，很容易被读成「远端服务没启动」，而实际原因是
隧道没通 —— 2026-09-19 的现场就是这么被带偏的（真实原因是 SSH 主机密钥与
`known_hosts` 不符）。同理，探活也走同一个判据，不会出现「灯显示在线、切换却报
隧道未建立」这种自相矛盾。

隧道本地端口填 `0` 表示**自动分配**（推荐）；写死端口时若被占用会当场报错并提示改回自动。

### 排查：U 盘弹不出来

安全弹出走的是「锁定 + 卸载卷」的原生序列（与「安全删除硬件」内核路径一致），
成败是确定性的，不依赖轮询。若报「无法卸载卷」，按顺序排查：

1. **先看卷健康状态**（最常见的误诊来源）：

   ```bat
   .venv\Scripts\python.exe -m usbswitch.core.disk
   ```

   若显示「脏位已置位」，用管理员命令行执行（`F` 换成实际盘符）：

   ```bat
   chkdsk F: /f /x
   ```

   `/x` 表示强制卸载，去掉它会卡在交互提示上。脏位通常来自上一次未正常卸载
   （例如挂载状态下直接断电切走 VBUS）——正是本项目的安全互锁要防的事故。

2. **卷健康正常但仍卸载失败**：说明确实有进程占用。关闭资源管理器窗口、
   退出可能正在读写 U 盘的程序后重试。

程序在卸载失败时会自动做第 1 项检查，并给出对应提示。

## BLE 接口

设备名 `USB-Switch`，Nordic UART 风格 GATT 服务：

| 角色 | UUID |
|---|---|
| Service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` |
| 写入命令 (RX) | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` |
| 状态通知 (TX) | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` |

命令：`a` / `b` / `x`（断开）/ `s`（查询状态）。手机 nRF Connect 也可直接控制。

固件支持**多客户端并发**（最多 3 路，连接期间继续广播），网页面板和控制脚本可以同时在线。
已知边界：Windows 上两个基于 WinRT 的客户端（如两个 Python 脚本）并发时，
第二个会话的 GATT 服务发现可能不完整；Chrome 面板走独立 GATT 实现，不受影响。
若脚本报「服务发现失败」，关闭再打开蓝牙开关即可恢复。

## 注意事项

1. 固件中的 `switchTo()` 是唯一允许操作 VBUS 的入口，**不要绕过它直接写 VBUS_EN 引脚**
2. J3-1 是 3.3 V 输入，**不要接 5 V**；ESP32-C3 与切换器板必须共地
3. 切换前应在当前主机操作系统中安全弹出 U 盘（桥接服务已自动化此步骤）
4. 烧写/调试固件时先不要接 J3 排针，验证逻辑后再联调
