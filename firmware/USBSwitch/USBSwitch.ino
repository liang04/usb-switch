// USB-A 双主机 U 盘切换器控制固件（ESP32-C3）
// 依据 docs/ESP32-C3_实际电路连接与控制设计.md 编写
// 控制方式：串口命令（a/b/x）或 BLE GATT 写入（A/B/X）

#include <Arduino.h>
#include <NimBLEDevice.h>

// ---------------- 引脚定义（与文档第 6 节一致） ----------------
constexpr uint8_t PIN_SEL    = 4;
constexpr uint8_t PIN_NEN    = 5;
constexpr uint8_t PIN_VBUS_A = 6;
constexpr uint8_t PIN_VBUS_B = 7;
constexpr uint8_t PIN_DET_A  = 0;
constexpr uint8_t PIN_DET_B  = 1;

constexpr uint32_t DATA_OFF_DELAY = 50;
constexpr uint32_t VBUS_OFF_DELAY = 300;
constexpr uint32_t VBUS_ON_DELAY  = 300;

// ---------------- 主机切换逻辑（与文档第 8 节一致） ----------------
enum class Host : uint8_t { None, A, B };

Host currentHost = Host::None;

bool hostPresent(Host host)
{
  const uint8_t pin = (host == Host::A) ? PIN_DET_A : PIN_DET_B;
  uint8_t highCount = 0;

  for (uint8_t i = 0; i < 5; i++) {
    if (digitalRead(pin) == HIGH) highCount++;
    delay(10);
  }
  return highCount >= 4;
}

void allOff()
{
  digitalWrite(PIN_NEN, HIGH);
  delay(DATA_OFF_DELAY);
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

  if (!hostPresent(target)) {
    allOff();
    currentHost = Host::None;
    return false;
  }

  allOff();
  delay(VBUS_OFF_DELAY);

  digitalWrite(PIN_SEL, target == Host::A ? LOW : HIGH);
  delay(5);

  if (target == Host::A) digitalWrite(PIN_VBUS_A, HIGH);
  else                   digitalWrite(PIN_VBUS_B, HIGH);

  delay(VBUS_ON_DELAY);

  if (!hostPresent(target)) {
    allOff();
    currentHost = Host::None;
    return false;
  }

  digitalWrite(PIN_NEN, LOW);
  currentHost = target;
  return true;
}

// ---------------- BLE（Nordic UART 风格 GATT 服务） ----------------
// 服务 UUID:        6E400001-B5A3-F393-E0A9-E50E24DCCA9E
// 写入特征 (RX):    6E400002-B5A3-F393-E0A9-E50E24DCCA9E  <- 电脑写入命令
// 通知特征 (TX):    6E400003-B5A3-F393-E0A9-E50E24DCCA9E  <- 固件回传状态

#define BLE_DEVICE_NAME "USB-Switch"
static NimBLECharacteristic* txChar = nullptr;
static bool bleConnected = false;

String statusString()
{
  String s = "host=";
  if (currentHost == Host::A)      s += "A";
  else if (currentHost == Host::B) s += "B";
  else                             s += "none";
  s += ", detA=" + String(digitalRead(PIN_DET_A));
  s += ", detB=" + String(digitalRead(PIN_DET_B));
  return s;
}

void notifyStatus(const String& msg)
{
  Serial.println(msg);
  if (txChar != nullptr && bleConnected) {
    txChar->setValue(msg.c_str());
    txChar->notify();
  }
}

void handleCommand(char cmd)
{
  switch (cmd) {
    case 'a': case 'A':
      notifyStatus(switchTo(Host::A) ? "OK: host=A" : "FAIL: Host A unavailable");
      break;
    case 'b': case 'B':
      notifyStatus(switchTo(Host::B) ? "OK: host=B" : "FAIL: Host B unavailable");
      break;
    case 'x': case 'X':
      switchTo(Host::None);
      notifyStatus("OK: host=none (USB disconnected)");
      break;
    case 's': case 'S':
      notifyStatus("STATUS: " + statusString());
      break;
    default:
      break;
  }
}

class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* pServer, NimBLEConnInfo& connInfo) override {
    bleConnected = true;
    Serial.println("BLE connected");
  }
  void onDisconnect(NimBLEServer* pServer, NimBLEConnInfo& connInfo, int reason) override {
    bleConnected = false;
    Serial.println("BLE disconnected");
    // 不在回调里直接重启广播（NimBLE 2.x 回调上下文下可能失败），
    // 由 loop() 里的看门狗负责重启
  }
};

class RxCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* pChar, NimBLEConnInfo& connInfo) override {
    std::string v = pChar->getValue();
    if (!v.empty()) handleCommand(v[0]);
  }
};

void setupBLE()
{
  NimBLEDevice::init(BLE_DEVICE_NAME);
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);

  NimBLEServer* server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());

  NimBLEService* service = server->createService("6E400001-B5A3-F393-E0A9-E50E24DCCA9E");

  txChar = service->createCharacteristic(
      "6E400003-B5A3-F393-E0A9-E50E24DCCA9E",
      NIMBLE_PROPERTY::NOTIFY);

  NimBLECharacteristic* rxChar = service->createCharacteristic(
      "6E400002-B5A3-F393-E0A9-E50E24DCCA9E",
      NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  rxChar->setCallbacks(new RxCallbacks());

  bool svcOk = service->start();
  Serial.printf("BLE service start: %s\n", svcOk ? "OK" : "FAILED");

  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  // 主广播包空间：flags(3B) + 名字(12B) = 15B，放得下；
  // 128 位服务 UUID 移到扫描响应包，这样 Chrome 首次扫描即可显示名称
  adv->setName(BLE_DEVICE_NAME);
  NimBLEAdvertisementData srData;
  srData.addServiceUUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E");
  adv->setScanResponseData(srData);
  adv->enableScanResponse(true);
  bool advOk = adv->start();
  Serial.printf("BLE advertising start: %s, name: %s\n", advOk ? "OK" : "FAILED", BLE_DEVICE_NAME);
}

void setup()
{
  Serial.begin(115200);
  delay(3000);  // 留出时间给上位机打开串口，便于捕获启动日志
  Serial.println("=== USBSwitch boot ===");

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

  setupBLE();

  // 上电时优先选择 Host A
  if (hostPresent(Host::A))      switchTo(Host::A);
  else if (hostPresent(Host::B)) switchTo(Host::B);

  notifyStatus("READY: " + statusString());
}

void loop()
{
  if (Serial.available()) {
    handleCommand((char)Serial.read());
  }

  // 广播看门狗：未连接且广播停止时自动重启广播
  static uint32_t lastAdvCheck = 0;
  if (!bleConnected && millis() - lastAdvCheck > 2000) {
    lastAdvCheck = millis();
    NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
    if (adv != nullptr && !adv->isAdvertising()) {
      Serial.println("Advertising watchdog: restarting");
      adv->start();
    }
  }
}
