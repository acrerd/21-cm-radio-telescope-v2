# WT32-ETH01 Migration Plan

**Status:** Planned
**Purpose:** Replace ESP32-S3 with WT32-ETH01 for native Ethernet support

---

## 1. Overview

The WT32-ETH01 is a compact ESP32 module with built-in LAN8720 Ethernet PHY. Unlike the current ESP32-S3 + W5500 SPI approach, this uses the ESP32's native RMII Ethernet MAC for reliable, high-performance networking.

### Why WT32-ETH01?

| Feature | ESP32-S3 + W5500 | WT32-ETH01 |
|---------|------------------|------------|
| Ethernet | SPI (software) | Native RMII (hardware) |
| Speed | Limited by SPI | Full 100 Mbps |
| Reliability | Had stability issues | Proven stable |
| WiFi | Yes | Yes (simultaneous) |
| Cost | ~$8 + $5 module | ~$8 total |
| Size | Larger (2 boards) | Compact single board |

### WT32-ETH01 Specifications

- **MCU:** ESP32-WROOM-32 (dual-core 240MHz)
- **Ethernet:** LAN8720A PHY, 10/100 Mbps, RJ45 connector
- **WiFi:** 802.11 b/g/n, PCB antenna
- **Flash:** 4MB
- **RAM:** 520KB SRAM
- **Power:** 5V via pin or 3.3V direct
- **Size:** 55mm x 26mm

---

## 2. Hardware

### WT32-ETH01 Pinout

```
                    WT32-ETH01
              ┌─────────────────────┐
              │  [RJ45 Ethernet]    │
              │                     │
        3V3 ──┤ 3V3           EN   ├── EN (reset)
        GND ──┤ GND           IO0  ├── GPIO0 (boot)
      GPIO2 ──┤ IO2           IO4  ├── GPIO4
      GPIO4 ──┤ IO4           RXD  ├── GPIO3 (RX)
     GPIO12 ──┤ IO12          TXD  ├── GPIO1 (TX)
     GPIO14 ──┤ IO14          IO15 ├── GPIO15
     GPIO15 ──┤ IO15          IO33 ├── GPIO33
     GPIO32 ──┤ IO32          IO35 ├── GPIO35 (input only)
     GPIO33 ──┤ IO33          IO36 ├── GPIO36 (input only)
     GPIO39 ──┤ IO39          IO39 ├── GPIO39 (input only)
         5V ──┤ 5V            GND  ├── GND
              └─────────────────────┘
```

### Reserved Pins (Ethernet RMII - Do Not Use)

| GPIO | Function |
|------|----------|
| 17 | EMAC_CLK_OUT_180 |
| 18 | EMAC_MDIO |
| 19 | EMAC_TXD0 |
| 21 | EMAC_TX_EN |
| 22 | EMAC_TXD1 |
| 23 | EMAC_MDC |
| 25 | EMAC_RXD0 |
| 26 | EMAC_RXD1 |
| 27 | EMAC_RX_DV |

### Available GPIO for User

| GPIO | Notes |
|------|-------|
| 2 | Onboard LED, boot mode (avoid pull-up) |
| 4 | General purpose |
| 5 | General purpose (strapping pin) |
| 12 | MTDI, boot mode (must be LOW at boot) |
| 14 | General purpose |
| 15 | MTDO (must be HIGH at boot for normal) |
| 32 | General purpose |
| 33 | General purpose |
| 35 | Input only |
| 36 | Input only |
| 39 | Input only |

---

## 3. Wiring: WT32-ETH01 to Arduino Due

### Recommended UART Pins

Use GPIO14 and GPIO15 for serial to Due (avoiding boot-sensitive pins):

| WT32-ETH01 | Arduino Due | Function |
|------------|-------------|----------|
| GPIO14 | Pin 19 (RX1) | WT32 TX -> Due RX |
| GPIO15 | Pin 18 (TX1) | WT32 RX <- Due TX |
| GND | GND | Common ground |
| 5V | 5V | Power (or separate supply) |

**Alternative:** GPIO32/GPIO33 are also safe choices if GPIO14/15 cause issues.

### Programming Connection

The WT32-ETH01 requires a USB-TTL adapter for programming:

| USB-TTL | WT32-ETH01 |
|---------|------------|
| TX | RXD (GPIO3) |
| RX | TXD (GPIO1) |
| GND | GND |
| 3.3V | 3V3 |

**Boot Mode:** Hold IO0 LOW while pressing EN to enter flash mode.

---

## 4. Code Changes

### 4.1 platformio.ini

Replace the ESP32-S3 configuration:

```ini
; PlatformIO Configuration for WT32-ETH01

[env:wt32-eth01]
platform = espressif32
board = wt32-eth01
framework = arduino
monitor_speed = 115200

; No USB CDC - uses standard UART
build_flags =
    -DCORE_DEBUG_LEVEL=0

upload_speed = 460800
upload_protocol = esptool

lib_deps =
    https://github.com/me-no-dev/AsyncTCP.git
    https://github.com/me-no-dev/ESPAsyncWebServer.git
```

### 4.2 config.h Changes

```cpp
// Serial connection to Arduino Due
// WT32-ETH01 pinout (GPIO14/15 recommended)
#define DUE_UART_TX 14   // WT32 GPIO -> Due RX (pin 19)
#define DUE_UART_RX 15   // WT32 GPIO <- Due TX (pin 18)

// Ethernet PHY configuration (LAN8720)
#define ETH_PHY_TYPE  ETH_PHY_LAN8720
#define ETH_PHY_ADDR  1
#define ETH_PHY_MDC   23
#define ETH_PHY_MDIO  18
#define ETH_PHY_POWER 16
#define ETH_CLK_MODE  ETH_CLOCK_GPIO0_IN
```

### 4.3 New Ethernet Initialization (main.cpp)

Add Ethernet support alongside WiFi:

```cpp
#include <ETH.h>

// Ethernet state
bool ethConnected = false;

void onEthEvent(WiFiEvent_t event) {
    switch (event) {
        case ARDUINO_EVENT_ETH_START:
            Serial.println("ETH Started");
            ETH.setHostname("srt-controller");
            break;
        case ARDUINO_EVENT_ETH_CONNECTED:
            Serial.println("ETH Connected");
            break;
        case ARDUINO_EVENT_ETH_GOT_IP:
            Serial.printf("ETH IP: %s\n", ETH.localIP().toString().c_str());
            ethConnected = true;
            break;
        case ARDUINO_EVENT_ETH_DISCONNECTED:
            Serial.println("ETH Disconnected");
            ethConnected = false;
            break;
        default:
            break;
    }
}

void setup() {
    Serial.begin(115200);

    // Register Ethernet event handler
    WiFi.onEvent(onEthEvent);

    // Initialize Ethernet
    ETH.begin(ETH_PHY_ADDR, ETH_PHY_POWER, ETH_PHY_MDC,
              ETH_PHY_MDIO, ETH_PHY_TYPE, ETH_CLK_MODE);

    // Start WiFi AP (runs alongside Ethernet)
    WiFi.softAP(settings.apSSID.c_str(), settings.apPassword.c_str());

    // ... rest of setup
}
```

### 4.4 Network Status Updates (web_server.cpp)

Add Ethernet status to `/wifi/status` endpoint:

```cpp
json += "\"eth_connected\":" + String(ethConnected ? "true" : "false") + ",";
json += "\"eth_ip\":\"" + (ethConnected ? ETH.localIP().toString() : String("")) + "\",";
```

### 4.5 Remove USB CDC Workarounds

The WT32-ETH01 uses standard UART, not USB CDC. The `DBG()` macro can be simplified but keeping it doesn't hurt:

```cpp
// Standard serial - always works, DBG macro optional
#define DBG(x) if (Serial) { x; }
```

---

## 5. Web Interface Updates

### Network Tab Additions

Add Ethernet status display:

```html
<div class="section">
    <h3>Ethernet</h3>
    <div class="status-row">
        <span>Status:</span>
        <span id="eth-status">--</span>
    </div>
    <div class="status-row">
        <span>IP Address:</span>
        <span id="eth-ip">--</span>
    </div>
</div>
```

### JavaScript Updates

```javascript
function updateNetworkStatus() {
    fetch('/wifi/status')
        .then(r => r.json())
        .then(data => {
            // Ethernet
            document.getElementById('eth-status').textContent =
                data.eth_connected ? 'Connected' : 'Disconnected';
            document.getElementById('eth-ip').textContent =
                data.eth_ip || '--';
            // ... existing WiFi status code
        });
}
```

---

## 6. Dual-USB Architecture

### Overview

The Arduino Due has two USB ports. We use both for a clean, always-on design:

```
                        Arduino Due
                    ┌─────────────────┐
PC USB #1 ─────────►│ Programming USB │◄──── Due debug, commands, status
                    │    (Serial)     │
                    │                 │
PC USB #2 ─────────►│  Native USB     │◄──── ESP32 console & programming
                    │  (SerialUSB)    │
                    │       ↕         │
                    │    Serial1      │────► WT32-ETH01 (GPIO1/GPIO3)
                    │                 │
                    │    GPIO 2,3     │────► WT32-ETH01 (GPIO0, EN)
                    └─────────────────┘
```

**Programming USB (Serial):** Due programming, commands (HOME, STOP, etc.), status output

**Native USB (SerialUSB):** Permanent bridge to ESP32 - always active, no mode switching

### Hardware Connections

| Due Pin | WT32-ETH01 | Function |
|---------|------------|----------|
| Pin 18 (TX1) | GPIO3 (RXD) | Serial data to ESP32 |
| Pin 19 (RX1) | GPIO1 (TXD) | Serial data from ESP32 |
| Pin 2 | GPIO0 (IO0) | Boot mode control |
| Pin 3 | EN | Reset control |
| GND | GND | Common ground |
| 5V | 5V | Power |

### Due Firmware Changes

Add to the Due's `main.cpp`:

```cpp
// ===== Add near top, after includes =====
#define ESP_GPIO0_PIN 2
#define ESP_EN_PIN 3

// ===== Add this function =====
void setupESPBridge() {
    // Initialize Native USB for ESP32 bridge
    SerialUSB.begin(115200);

    // Setup boot mode control pins
    pinMode(ESP_GPIO0_PIN, OUTPUT);
    pinMode(ESP_EN_PIN, OUTPUT);
    digitalWrite(ESP_GPIO0_PIN, HIGH);  // Normal boot (not download mode)
    digitalWrite(ESP_EN_PIN, HIGH);     // Not in reset
}

void espEnterBootMode() {
    Serial.println("ESP32: Entering download mode...");
    digitalWrite(ESP_GPIO0_PIN, LOW);   // GPIO0 LOW = download mode
    delay(10);
    digitalWrite(ESP_EN_PIN, LOW);      // Reset
    delay(100);
    digitalWrite(ESP_EN_PIN, HIGH);     // Release reset (GPIO0 still LOW)
    Serial.println("ESP32: Ready for upload on Native USB port");
}

void espReset() {
    Serial.println("ESP32: Resetting...");
    digitalWrite(ESP_GPIO0_PIN, HIGH);  // Ensure normal boot mode
    digitalWrite(ESP_EN_PIN, LOW);
    delay(100);
    digitalWrite(ESP_EN_PIN, HIGH);
    Serial.println("ESP32: Reset complete");
}

// ===== Add to setup(), after Serial1.begin() =====
void setup() {
    // ... existing Serial and Serial1 initialization ...

    // Add: Initialize ESP32 bridge
    setupESPBridge();

    // ... rest of existing setup ...
}

// ===== Add at START of loop() =====
void loop() {
    // Permanent bridge: Native USB ↔ Serial1 (ESP32)
    // This runs every loop iteration, always active
    while (SerialUSB.available()) {
        Serial1.write(SerialUSB.read());
    }
    while (Serial1.available()) {
        SerialUSB.write(Serial1.read());
    }

    // ... rest of existing loop code ...
}

// ===== Add to command processing =====
// In processLine() or wherever commands are handled, add:

if (line == "ESPBOOT" || line == "BOOTMODE") {
    espEnterBootMode();
    return;
}
if (line == "ESPRESET" || line == "RESETESP") {
    espReset();
    return;
}
```

### How the Bridge Works

The Due's Native USB port acts as a **transparent USB-to-serial adapter**. It's functionally identical to a CH340 or CP2102 module - just byte-for-byte passthrough between the PC and the ESP32's serial port.

- **Always active** - no mode switching or special commands for normal use
- **Bidirectional** - ESP32 output appears on PC, PC input goes to ESP32
- **Independent** - works alongside normal Due operation

The Due pins 2 and 3 (GPIO0/EN control) are optional extras that let you trigger download mode via software command - something a plain USB-to-serial adapter can't do.

### Programming the ESP32

The ESP32 must be in **download mode** to accept firmware uploads. There are two ways to enter download mode:

#### Method 1: Physical Buttons (Simplest)

If the WT32-ETH01 has GPIO0 and EN buttons (or you've added them):

1. **Hold** GPIO0 button
2. **Press and release** EN button
3. **Release** GPIO0 button
4. **Upload** via PlatformIO:
   ```bash
   cd esp32_controller_arduino
   pio run -e wt32-eth01 -t upload --upload-port COMx
   ```

The ESP32 auto-resets to normal operation after upload completes.

#### Method 2: Due Software Command

If you've wired Due pins 2/3 to GPIO0/EN:

1. Send `ESPBOOT` to Due (via Programming USB port)
2. **Upload** via Native USB port:
   ```bash
   pio run -e wt32-eth01 -t upload --upload-port COMx
   ```
3. Send `ESPRESET` to Due (or ESP32 auto-resets after upload)

### Due Commands for ESP32

| Command | Action |
|---------|--------|
| `ESPBOOT` | Put ESP32 in download mode for programming |
| `ESPRESET` | Reset ESP32 to normal operation |

### Why No Auto-Reset?

Many ESP32 dev boards program without pressing any buttons. They use **auto-reset circuitry** - the USB-to-serial chip's DTR/RTS lines are wired through transistors to toggle GPIO0 and EN automatically when esptool starts.

The Due bridge doesn't have this because SerialUSB doesn't expose DTR/RTS control lines the same way. Using physical buttons or the `ESPBOOT` command is simple and reliable.

### Daily Usage

**Monitor ESP32:**
- Connect any serial terminal to Native USB port at 115200 baud
- ESP32 debug output appears continuously
- No special commands needed

**Control Due:**
- Connect to Programming USB port
- Send HOME, STOP, STATUS, etc.
- Completely independent of ESP32

### Windows Upload Script

Create `upload_esp32.bat`:

```batch
@echo off
setlocal

:: Configure for your system
set DUE_PROG_PORT=COM3
set DUE_NATIVE_PORT=COM10
set PROJECT_DIR=esp32_controller_arduino

echo Putting ESP32 in download mode...
echo ESPBOOT > \\.\%DUE_PROG_PORT%
timeout /t 2 /nobreak > nul

echo Uploading firmware...
cd %PROJECT_DIR%
pio run -e wt32-eth01 -t upload --upload-port %DUE_NATIVE_PORT%

if %ERRORLEVEL% EQU 0 (
    echo.
    echo Upload successful! Resetting ESP32...
    echo ESPRESET > \\.\%DUE_PROG_PORT%
) else (
    echo.
    echo Upload FAILED
)

pause
```

### Troubleshooting

**No output on Native USB:**
- Check Serial1 wiring (TX1→RX, RX1→TX - they cross!)
- Verify ESP32 is powered and running
- Check baud rate matches (115200)

**Upload fails:**
- Try physical button method: hold GPIO0, press EN, release GPIO0
- Check GPIO0 and EN wiring to Due pins 2 and 3
- Verify correct COM port for Native USB

**ESP32 not running after upload:**
- Send `ESPRESET` command to Due
- Or press EN button on WT32-ETH01
- Or power cycle the system

## 7. Build & Flash Procedure

### First-Time Setup (USB-TTL adapter)

1. **Install USB-TTL adapter drivers** (CH340 or CP2102)

2. **Connect USB-TTL to WT32-ETH01:**
   ```
   USB-TTL TX  -> WT32 RXD
   USB-TTL RX  -> WT32 TXD
   USB-TTL GND -> WT32 GND
   USB-TTL 3V3 -> WT32 3V3
   ```

3. **Enter boot mode:**
   - Hold IO0 button (or jumper to GND)
   - Press EN button (reset)
   - Release IO0

4. **Flash:**
   ```bash
   cd esp32_controller_arduino
   pio run -e wt32-eth01 --target upload
   ```

5. **Monitor:**
   ```bash
   pio device monitor
   ```

### Subsequent Updates

Same process - enter boot mode, flash.

---

## 7. Testing Checklist

### Hardware Verification

- [ ] WT32-ETH01 powers up (LED activity)
- [ ] USB-TTL serial connection works
- [ ] Can enter boot mode and flash firmware
- [ ] Serial monitor shows boot messages

### Ethernet

- [ ] Ethernet link LED lights when cable connected
- [ ] DHCP assigns IP address
- [ ] Can ping WT32-ETH01 from PC
- [ ] Web interface accessible via Ethernet IP
- [ ] Stellarium connects via Ethernet

### WiFi

- [ ] AP mode starts (SSID visible)
- [ ] Can connect to AP
- [ ] Web interface accessible at 192.168.4.1
- [ ] WiFi station mode connects to saved network

### Due Communication

- [ ] Serial connection to Due works
- [ ] Status updates received from Due
- [ ] Commands sent to Due execute correctly
- [ ] Tracking loop functions properly

### Full System

- [ ] NTP time sync works (via Ethernet or WiFi)
- [ ] Coordinate conversion accurate
- [ ] Sun/Moon tracking works
- [ ] Stellarium slew commands work
- [ ] Settings save/load works

---

## 8. Fallback Plan

If WT32-ETH01 migration has issues:

1. **Keep ESP32-S3 version working** - don't delete the current code
2. **Use build environments** - platformio.ini can have both `[env:esp32s3]` and `[env:wt32-eth01]`
3. **Conditional compilation** - use `#ifdef` for board-specific code

```ini
[env:esp32s3]
; Current working configuration
board = esp32-s3-devkitc-1
build_flags = -DBOARD_ESP32S3

[env:wt32-eth01]
; New Ethernet configuration
board = wt32-eth01
build_flags = -DBOARD_WT32_ETH01
```

---

## 9. Parts List

| Item | Quantity | Notes |
|------|----------|-------|
| WT32-ETH01 | 1 | Main controller |
| USB-TTL adapter | 1 | CH340 or CP2102, for programming |
| Ethernet cable | 1 | Cat5e or better |
| Dupont wires | 4 | For Due connection |
| 5V power supply | 1 | If not powering from Due |

**Suppliers:**
- AliExpress: ~$8-10 for WT32-ETH01
- Amazon: ~$15-20 (faster shipping)

---

## 10. Timeline

| Phase | Tasks | Duration |
|-------|-------|----------|
| 1. Procurement | Order WT32-ETH01, USB-TTL adapter | 1-2 weeks |
| 2. Code changes | Update platformio.ini, config.h, add ETH code | 1 day |
| 3. Bench test | Flash, test Ethernet/WiFi/Serial independently | 1 day |
| 4. Integration | Connect to Due, test full system | 1 day |
| 5. Deployment | Install in telescope, verify operation | 1 day |

---

## References

- [WT32-ETH01 Datasheet](http://www.wireless-tag.com/portfolio/wt32-eth01/)
- [ESP32 Ethernet Examples](https://github.com/espressif/arduino-esp32/tree/master/libraries/Ethernet/examples)
- [LAN8720 PHY Datasheet](https://www.microchip.com/en-us/product/LAN8720A)
