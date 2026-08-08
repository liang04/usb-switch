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
├── docs/
│   ├── ESP32-C3_实际电路连接与控制设计.md   # 电路设计文档（接线和固件的依据）
│   └── datasheets/                          # CH442E / SY6280 / USBLC6 数据手册
├── firmware/
│   └── USBSwitch/USBSwitch.ino              # ESP32-C3 固件（Arduino 框架 + NimBLE）
├── scripts/
│   ├── usb_switch.py / .bat                 # Windows 蓝牙控制脚本（a/b/x/s 命令）
│   ├── usb_switch_safe.py / .bat            # 安全切换：先弹出 U 盘，成功才切换
│   ├── bridge.py + start_bridge.bat         # Host A (Windows) 本地桥接服务 :8737
│   └── linux_bridge.py                      # Host B (Linux) 桥接服务 :8738
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
```

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

**Host A（Windows）**：双击 `scripts/start_bridge.bat`（依赖：`pip install bleak`）

**Host B（Linux）**：

```bash
scp scripts/linux_bridge.py <user>@<Host B IP>:~/usb_switch_bridge.py
ssh <user>@<Host B IP> "nohup python3 ~/usb_switch_bridge.py > ~/usb_switch_bridge.log 2>&1 &"
```

如 U 盘在 Linux 上被挂载，`umount` 需要 root，建议配置免密 sudo（`<user>` 替换为实际用户名）：

```text
<user> ALL=(ALL) NOPASSWD: /bin/umount
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

## BLE 接口

设备名 `USB-Switch`，Nordic UART 风格 GATT 服务：

| 角色 | UUID |
|---|---|
| Service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` |
| 写入命令 (RX) | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` |
| 状态通知 (TX) | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` |

命令：`a` / `b` / `x`（断开）/ `s`（查询状态）。手机 nRF Connect 也可直接控制。

## 注意事项

1. 固件中的 `switchTo()` 是唯一允许操作 VBUS 的入口，**不要绕过它直接写 VBUS_EN 引脚**
2. J3-1 是 3.3 V 输入，**不要接 5 V**；ESP32-C3 与切换器板必须共地
3. 切换前应在当前主机操作系统中安全弹出 U 盘（桥接服务已自动化此步骤）
4. 烧写/调试固件时先不要接 J3 排针，验证逻辑后再联调
