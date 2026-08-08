# USB-A 双主机 U 盘切换器

## ESP32-C3 实际电路连接与控制设计

> 本文依据当前 EasyEDA 工程中的实际原理图整理。实际电路与 `USB-A双主机U盘切换器_RevA-Lite方案文档.md` 存在差异，接线和程序应以本文及实际板卡丝印为准。

## 1. 实际电路概述

实际电路由以下部分组成：

```text
Host A USB-B  ── USB 数据保护 ──┐
                                ├── CH442E ── USB U 盘接口
Host B USB-B  ── USB 数据保护 ──┘

Host A VBUS ── SY6280AAC U2 ──┐
                              ├── UDISK_VBUS
Host B VBUS ── SY6280AAC U3 ──┘

ESP32-C3 ── J3 控制排针
```

主要器件：

| 器件 | 实际型号 | 作用 |
|---|---|---|
| U1 | CH442E | USB D+/D- 双路 2:1 模拟开关 |
| U2 | SY6280AAC | Host A VBUS 电源开关 |
| U3 | SY6280AAC | Host B VBUS 电源开关 |
| D1 | USBLC6-2SC6 | Host A USB ESD 保护 |
| D2 | USBLC6-2SC6 | Host B USB ESD 保护 |
| D3 | USBLC6-2SC6 | U 盘 USB ESD 保护 |
| J1 | USB-B 母座 | Host A 接口 |
| J2 | USB-B 母座 | Host B 接口 |
| USB1 | USB-A 母座 | U 盘接口 |
| J3 | 1×8 2.54 mm 排针 | ESP32-C3 控制接口 |

## 2. USB 数据通道

实际原理图中的数据网络如下：

| CH442E 引脚 | 网络 | 说明 |
|---:|---|---|
| 1 `IN` | `SEL_IN` | Host 选择控制 |
| 2 `S1B` | `HOSTA_DP` | Host A D+ |
| 3 `S2B` | `HOSTB_DP` | Host B D+ |
| 4 `DB` | `UDISK_DP` | U 盘 D+ |
| 5 `GND` | `GND` | 地 |
| 6 `DC` | `UDISK_DM` | U 盘 D- |
| 7 `S2C` | `HOSTB_DM` | Host B D- |
| 8 `S1C` | `HOSTA_DM` | Host A D- |
| 9 `EN#` | `NEN` | 数据通道使能，低有效 |
| 10 `VCC` | `3V3` | CH442E 逻辑电源 |

CH442E 的控制逻辑：

| `NEN` | `SEL_IN` | 数据通道 |
|---:|---:|---|
| 1 | 任意 | 全部断开 |
| 0 | 0 | 连接 Host A |
| 0 | 1 | 连接 Host B |

CH442E 数据手册：<https://www.bitsavers.org/components/wch/_dataSheets/CH440DS1.PDF>

## 3. USB VBUS 电源通道

### 3.1 U2：Host A 电源开关

| U2 引脚 | 网络 |
|---:|---|
| 1 `OUT` | `UDISK_VBUS` |
| 2 `GND` | `GND` |
| 3 `ISET` | `ISET_A` |
| 4 `EN` | `VBUS_A_EN` |
| 5 `IN` | `HOSTA_VBUS` |

### 3.2 U3：Host B 电源开关

| U3 引脚 | 网络 |
|---:|---|
| 1 `OUT` | `UDISK_VBUS` |
| 2 `GND` | `GND` |
| 3 `ISET` | `ISET_B` |
| 4 `EN` | `VBUS_B_EN` |
| 5 `IN` | `HOSTB_VBUS` |

`VBUS_A_EN` 和 `VBUS_B_EN` 均为高电平有效：

| `VBUS_A_EN` | `VBUS_B_EN` | VBUS 状态 |
|---:|---:|---|
| 0 | 0 | U 盘断电 |
| 1 | 0 | U 盘由 Host A 供电 |
| 0 | 1 | U 盘由 Host B 供电 |
| 1 | 1 | 禁止，程序不得进入此状态 |

实际 R11、R12 均为 10 kΩ。根据 SY6280 数据手册的计算公式：

```text
I_LIMIT = 6800 / RSET
         = 6800 / 10000
         ≈ 0.68 A
```

SY6280 数据手册：<https://www.silergy.com/download/downloadFile?ftype=note&id=4369&type=product>

## 4. USB 接口网络

### Host A：J1

```text
J1 pin 1 VCC  -> HOSTA_VBUS
J1 pin 2 D-   -> HOSTA_DM
J1 pin 3 D+   -> HOSTA_DP
J1 pin 4 GND  -> GND
J1 SHIELD     -> GND
```

### Host B：J2

```text
J2 pin 1 VCC  -> HOSTB_VBUS
J2 pin 2 D-   -> HOSTB_DM
J2 pin 3 D+   -> HOSTB_DP
J2 pin 4 GND  -> GND
J2 SHIELD     -> GND
```

### U 盘接口：USB1

```text
USB1 pin 1 VCC -> UDISK_VBUS
USB1 pin 2 D-  -> UDISK_DM
USB1 pin 3 D+  -> UDISK_DP
USB1 pin 4 GND -> GND
```

ESP32-C3 只连接 J3 控制信号，不要把 ESP32-C3 GPIO 连接到 `HOSTA_DP`、`HOSTA_DM`、`HOSTB_DP`、`HOSTB_DM`、`UDISK_DP` 或 `UDISK_DM`。

## 5. 实际 J3 排针定义

J3 从上到下为 1～8 脚：

| J3 引脚 | 网络 | 方向 | 作用 |
|---:|---|---|---|
| 1 | `3V3` | 电源输入 | 给 U1 和控制上拉电阻供电 |
| 2 | `GND` | 电源 | 与 ESP32-C3 共地 |
| 3 | `SEL_IN` | 输入 | 选择 Host A/B |
| 4 | `NEN` | 输入 | CH442E 数据通道使能 |
| 5 | `VBUS_A_EN` | 输入 | 打开/关闭 Host A VBUS |
| 6 | `VBUS_B_EN` | 输入 | 打开/关闭 Host B VBUS |
| 7 | `HOSTA_DET` | 输出 | 检测 Host A 是否有 VBUS |
| 8 | `HOSTB_DET` | 输出 | 检测 Host B 是否有 VBUS |

实际电路没有 `FAULT_A`、`FAULT_B`，也没有文档中的 1×10 排针。不要额外连接不存在的第 9、10 脚。

## 6. ESP32-C3 推荐接线

以下 GPIO 分配以常见 ESP32-C3 开发板为例，可根据实际开发板调整：

| ESP32-C3 | J3 | 方向 |
|---|---:|---|
| `3V3` | 1 | 给实际电路板供电 |
| `GND` | 2 | 共地 |
| GPIO4 | 3 | `SEL_IN` 输出 |
| GPIO5 | 4 | `NEN` 输出 |
| GPIO6 | 5 | `VBUS_A_EN` 输出 |
| GPIO7 | 6 | `VBUS_B_EN` 输出 |
| GPIO0 | 7 | `HOSTA_DET` 输入 |
| GPIO1 | 8 | `HOSTB_DET` 输入 |

注意：

1. ESP32-C3 和实际电路板必须共地。
2. J3-1 是 3.3 V 电源输入，不要接 5 V。
3. `HOSTA_DET`、`HOSTB_DET` 已经由板上 47 kΩ / 68 kΩ 电阻分压，不需要额外分压。
4. `HOSTA_DET`、`HOSTB_DET` 使用 `INPUT`，不要使用 `INPUT_PULLUP`。
5. ESP32-C3 复位时，板上 R2 将 `NEN` 拉高，R3/R4 将两路 VBUS_EN 拉低，默认是安全断开状态。

Host 检测电压为：

```text
V_DET = VBUS × 68k / (47k + 68k)
```

当 Host VBUS 为 5 V 时，检测脚约为 2.96 V。

## 7. 推荐切换时序

切换到任意主机时必须遵循以下顺序：

```text
1. NEN = 1，断开 USB D+/D-
2. 等待约 50 ms
3. VBUS_A_EN = 0
4. VBUS_B_EN = 0
5. 等待约 300 ms，确保 U 盘断电
6. 设置 SEL_IN
7. 只打开目标主机的 VBUS_EN
8. 等待约 300 ms，等待 VBUS 稳定
9. NEN = 0，重新连接 USB D+/D-
```

切换前应先在当前主机操作系统中安全弹出 U 盘。ESP32-C3 只能控制电气连接，不能代替操作系统执行安全弹出。

## 8. Arduino 示例程序

```cpp
#include <Arduino.h>

constexpr uint8_t PIN_SEL    = 4;
constexpr uint8_t PIN_NEN    = 5;
constexpr uint8_t PIN_VBUS_A = 6;
constexpr uint8_t PIN_VBUS_B = 7;
constexpr uint8_t PIN_DET_A  = 0;
constexpr uint8_t PIN_DET_B  = 1;

constexpr uint32_t DATA_OFF_DELAY = 50;
constexpr uint32_t VBUS_OFF_DELAY = 300;
constexpr uint32_t VBUS_ON_DELAY  = 300;

enum class Host : uint8_t {
  None,
  A,
  B
};

Host currentHost = Host::None;

bool hostPresent(Host host)
{
  const uint8_t pin = (host == Host::A) ? PIN_DET_A : PIN_DET_B;
  uint8_t highCount = 0;

  // 进行简单去抖
  for (uint8_t i = 0; i < 5; i++) {
    if (digitalRead(pin) == HIGH) {
      highCount++;
    }
    delay(10);
  }

  return highCount >= 4;
}

void allOff()
{
  // CH442E 的 EN# 为低有效；NEN=1 表示数据断开
  digitalWrite(PIN_NEN, HIGH);
  delay(DATA_OFF_DELAY);

  // 两路 VBUS 必须先全部关闭
  digitalWrite(PIN_VBUS_A, LOW);
  digitalWrite(PIN_VBUS_B, LOW);
}

bool switchTo(Host target)
{
  if (target == Host::None) {
    allOff();
    currentHost = Host::None;
    return true;
  }

  // 目标主机没有 VBUS 时不打开电源开关
  if (!hostPresent(target)) {
    allOff();
    currentHost = Host::None;
    return false;
  }

  // 1. 断开数据，2. 关闭两路 VBUS
  allOff();
  delay(VBUS_OFF_DELAY);

  // 3. 设置数据通道选择
  digitalWrite(PIN_SEL, target == Host::A ? LOW : HIGH);
  delay(5);

  // 4. 只打开目标主机的 VBUS
  if (target == Host::A) {
    digitalWrite(PIN_VBUS_A, HIGH);
  } else {
    digitalWrite(PIN_VBUS_B, HIGH);
  }

  // 5. 等待 U 盘供电稳定
  delay(VBUS_ON_DELAY);

  if (!hostPresent(target)) {
    allOff();
    currentHost = Host::None;
    return false;
  }

  // 6. 重新接通数据
  digitalWrite(PIN_NEN, LOW);
  currentHost = target;
  return true;
}

void setup()
{
  Serial.begin(115200);

  pinMode(PIN_SEL, OUTPUT);
  pinMode(PIN_NEN, OUTPUT);
  pinMode(PIN_VBUS_A, OUTPUT);
  pinMode(PIN_VBUS_B, OUTPUT);

  pinMode(PIN_DET_A, INPUT);
  pinMode(PIN_DET_B, INPUT);

  // 上电安全状态：默认选择 A，但数据和 VBUS 均关闭
  digitalWrite(PIN_SEL, LOW);
  digitalWrite(PIN_NEN, HIGH);
  digitalWrite(PIN_VBUS_A, LOW);
  digitalWrite(PIN_VBUS_B, LOW);

  delay(100);

  // 上电时优先选择 Host A
  if (hostPresent(Host::A)) {
    switchTo(Host::A);
  } else if (hostPresent(Host::B)) {
    switchTo(Host::B);
  }
}

void loop()
{
  // 串口命令：a=Host A，b=Host B，x=全部关闭
  if (Serial.available()) {
    const char cmd = Serial.read();

    if (cmd == 'a' || cmd == 'A') {
      Serial.println(switchTo(Host::A)
                       ? "Host A selected"
                       : "Host A unavailable");
    } else if (cmd == 'b' || cmd == 'B') {
      Serial.println(switchTo(Host::B)
                       ? "Host B selected"
                       : "Host B unavailable");
    } else if (cmd == 'x' || cmd == 'X') {
      switchTo(Host::None);
      Serial.println("USB disconnected");
    }
  }
}
```

后续如果使用按键、网页、BLE 或 MQTT 控制，只需要在事件处理函数中调用：

```cpp
switchTo(Host::A);
switchTo(Host::B);
switchTo(Host::None);
```

不要绕过 `switchTo()` 直接打开 VBUS，否则可能造成两路 Host VBUS 同时连接到 UDISK_VBUS。

## 9. 实际板上的外围网络

| 器件 | 实际连接 |
|---|---|
| R1 10 kΩ | `SEL_IN` → `GND`，默认选择 Host A |
| R2 10 kΩ | `3V3` → `NEN`，默认关闭数据 |
| R3 10 kΩ | `VBUS_A_EN` → `GND`，默认关闭 A 路 VBUS |
| R4 10 kΩ | `VBUS_B_EN` → `GND`，默认关闭 B 路 VBUS |
| R5 47 kΩ、R6 68 kΩ | `HOSTA_VBUS` → `HOSTA_DET` → `GND` |
| R7 47 kΩ、R8 68 kΩ | `HOSTB_VBUS` → `HOSTB_DET` → `GND` |
| R9 1 kΩ、LED1 | A 路 VBUS_EN 指示灯 |
| R10 1 kΩ、LED2 | B 路 VBUS_EN 指示灯 |
| R11 10 kΩ | `ISET_A` → `GND` |
| R12 10 kΩ | `ISET_B` → `GND` |
| C1 0.1 µF、C2 1 µF | `3V3` 去耦 |
| C3 0.1 µF | `HOSTA_VBUS` 去耦 |
| C4 0.1 µF | `HOSTB_VBUS` 去耦 |
| C5 10 µF、C6 0.1 µF | `UDISK_VBUS` 去耦 |

SY6280 数据手册建议在 VIN 附近放置约 10 µF 输入电容。当前实板 C3/C4 为 0.1 µF；如果热插拔时出现 VBUS 掉压、复位或 U 盘枚举不稳定，应考虑在 U2/U3 的 `IN-GND` 附近补充 10 µF 电容。

## 10. 上电及调试检查

1. 不插 Host 时，测量 J3-1 对 J3-2，应为约 3.3 V。
2. ESP32-C3 复位时，`NEN` 应为高电平，`VBUS_A_EN/B_EN` 应为低电平。
3. 插入 Host A 后，`HOSTA_DET` 应约为 2.8～3.1 V。
4. 插入 Host B 后，`HOSTB_DET` 应约为 2.8～3.1 V。
5. 选择 Host A 时，只允许 `VBUS_A_EN=1`。
6. 选择 Host B 时，只允许 `VBUS_B_EN=1`。
7. 在 `NEN=1` 时切换 VBUS 和 `SEL_IN`，最后再将 `NEN` 拉低。

