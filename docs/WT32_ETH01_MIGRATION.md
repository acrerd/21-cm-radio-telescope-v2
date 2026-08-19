# WT32-ETH01 Setup Guide

**Status:** Implemented
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

### Pin Strategy: Separate Programming and Runtime Communication

The WT32-ETH01 uses **different pins** for programming vs runtime communication:

| Function | Pins | Notes |
|----------|------|-------|
| **Programming** | TX0/RX0 (GPIO1/3) | 6-pin header, temporary FT232 programmer for first flash/recovery |
| **Due Communication** | IO4/IO14 | General purpose, no boot restrictions |

**Note:** Some WT32-ETH01 variants (RS-485 versions) have IO32/IO33 labelled as CFG/485_EN and connected to onboard RS-485 circuitry. Use IO4/IO14 instead to avoid conflicts.

This separation means you can program the ESP32 **without disconnecting the Due**.

### Important: TX0/RX0 vs TXD/RXD

The board has confusingly-named serial pins:

| Label | GPIO | UART | Location | Notes |
|-------|------|------|----------|-------|
| **TX0** | GPIO1 | UART0 | 6-pin programming header | Programming only |
| **RX0** | GPIO3 | UART0 | 6-pin programming header | Programming only |
| TXD | GPIO17 | UART2 | Main board edge | **Conflicts with Ethernet!** |
| RXD | GPIO5 | UART2 | Main board edge | UART2 TX unusable with Ethernet |

### Due to WT32-ETH01 Connections (Runtime)

| Arduino Due | WT32-ETH01 | Function | Wire Color |
|-------------|------------|----------|------------|
| Pin 18 (TX1) | **IO14** | Data to ESP32 (Due TX -> ESP RX) | Blue |
| Pin 19 (RX1) | **IO4** | Data from ESP32 (ESP TX -> Due RX) | Green |
| GND | GND | Common ground | Black |
| 5V | 5V | Power | Red |

**Benefits of IO4/IO14:**
- No boot mode restrictions (unlike GPIO12/15)
- Not used by Ethernet (unlike GPIO17-27)
- Not connected to RS-485 circuitry (unlike IO32/IO33 on some variants)
- Programming header (TX0/RX0) stays free for the temporary FT232 programmer

### One-Time Serial Flash via Temporary FT232 Programmer

This migration uses a temporary FT232 USB-TTL programmer with manual boot/reset
buttons to install the first OTA-capable WT32 firmware. The programmer is not
part of the permanent telescope wiring. After the first successful serial flash,
routine firmware updates should be done over Ethernet OTA.

Connect the temporary programmer to the WT32-ETH01 programming header only while
flashing or recovering the controller:

| Temporary FT232 programmer | WT32-ETH01 |
|----------------------------|------------|
| TXD | RX0 (GPIO3) |
| RXD | TX0 (GPIO1) |
| GND | GND |

Do not connect the programmer's 3.3V, 5V, or VCC pin while the WT32 is powered
from the telescope/Due supply. The WT32 and programmer must share ground, but
the programmer should not be a second power source.

Do not leave IO0/GPIO0 jumpered, held low, connected to a boot button harness,
or connected to an auto-reset line after flashing. On the WT32-ETH01, GPIO0 is
also the Ethernet RMII clock input; loading or holding that pin can prevent
Ethernet link and can make the WiFi AP appear only intermittently.

**No need to disconnect the Due serial wires** - it uses IO4/IO14, not TX0/RX0.

### Recommended Update Strategy

Migrate the controller to network firmware updates. Use the temporary FT232
programmer only for the first WT32 flash and for recovery if Ethernet OTA is not
available. The firmware includes Ethernet OTA support, so normal updates should
be uploaded over the network after the OTA-capable firmware is installed once.

**Best installed hardware setup:**
- Leave a keyed 3-pin service connector wired to WT32 `TX0`, `RX0`, and `GND`
- Do not permanently install the temporary FT232 programmer
- Do not leave programmer `3.3V/5V/VCC`, `DTR`, `RTS`, `IO0`, or `EN` wiring
  connected during operation
- Do not leave anything loading or holding `IO0/GPIO0`; it is also the Ethernet
  RMII clock input on this board

**Routine firmware update over Ethernet:**
```bash
cd esp32_controller_arduino
pio run -e wt32-eth01-ota -t upload
```

The OTA target defaults to `192.168.50.120` and port `3232`. If DHCP gives the
controller a different address, update `upload_port` in
`esp32_controller_arduino/platformio.ini`. The OTA password is configured in
`src/config.h`.

**First flash or recovery via temporary FT232 programmer:**
```bash
cd esp32_controller_arduino
pio run -e wt32-eth01 -t upload --upload-port /dev/ttyUSB0
```

Use recovery when the controller is not reachable on Ethernet, an OTA update is
interrupted, or a broken firmware image boots but does not start the network.
Disconnect the programmer after the flash succeeds.

**Manual button sequence for the temporary programmer:**
1. Start the PlatformIO upload command.
2. Hold the programmer's `BOOT`, `IO0`, or `FLASH` button.
3. Tap the programmer's `EN`, `RST`, or reset button once.
4. Keep holding `BOOT` for about two seconds while esptool connects.
5. Release `BOOT` after esptool connects or starts writing.
6. After upload, unplug the temporary programmer before normal operation.

---

## 4. ESP32 Code (Implemented)

The ESP32 firmware supports both ESP32-S3 and WT32-ETH01 from the same codebase using conditional compilation.

### 4.1 platformio.ini

Both board environments are configured (use `pio run -e wt32-eth01` or `pio run -e esp32s3`):

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
// WT32-ETH01: IO4/IO14 for runtime (TX0/RX0 reserved for programming)
// Note: Avoid IO32/IO33 - labelled CFG/485_EN on RS-485 variants
#define DUE_UART_TX 4    // WT32 IO4 -> Due RX (pin 19)
#define DUE_UART_RX 14   // WT32 IO14 <- Due TX (pin 18)

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

## 5. Web Interface (Implemented)

### Network Tab Features

The Network tab includes:

- **Ethernet status**: Connection state and current IP address
- **Ethernet configuration**: DHCP/Static IP selection with IP, gateway, subnet, DNS fields
- **WiFi AP status**: Access point SSID and IP
- **WiFi Station**: Connection to external networks with scan/connect

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/wifi/status` | GET | Network status including Ethernet settings and MAC addresses |
| `/eth/save` | GET | Save Ethernet settings (dhcp, ip, gateway, subnet, dns) |
| `/wifi/scan` | GET | Scan for WiFi networks |
| `/wifi/connect` | GET | Connect to WiFi (ssid, password) |
| `/wifi/forget` | GET | Clear saved WiFi credentials |
| `/offset` | GET | Set pointing offset (alt, az in degrees) |
| `/offset/clear` | GET | Clear pointing offset |
| `/calibrator` | GET | Control calibrator (state=on/off/toggle) |

---

## 6. Dual-USB Architecture (Implemented)

### Overview

The Arduino Due has two USB ports. The Native USB provides serial monitoring for the ESP32:

```
                        Arduino Due
                    ┌─────────────────┐
PC USB #1 ─────────►│ Programming USB │◄──── Due debug, commands, status
                    │    (Serial)     │
                    │                 │
PC USB #2 ─────────►│  Native USB     │◄──── ESP32 serial monitoring
                    │  (SerialUSB)    │
                    │       ↕         │
                    │    Serial1      │────► WT32-ETH01 (IO4/IO14)
                    └─────────────────┘

                   Temporary FT232 ─────► WT32-ETH01 TX0/RX0 (first flash/recovery)
```

**Programming USB (Serial):** Due programming, commands (HOME, STOP, etc.), status output

**Native USB (SerialUSB):** Bidirectional serial bridge to ESP32 for monitoring

**Temporary FT232:** ESP32 first flash/recovery via TX0/RX0 (no need to disconnect Due)

### Hardware Connections

| Due Pin | WT32-ETH01 | Function | Wire Color |
|---------|------------|----------|------------|
| Pin 18 (TX1) | IO14 | Serial data to ESP32 | Blue |
| Pin 19 (RX1) | IO4 | Serial data from ESP32 | Green |
| GND | GND | Common ground | Black |
| 5V | 5V | Power | Red |

**Note:** Due uses IO4/IO14, leaving TX0/RX0 free for programming without disconnection.

### Due Firmware (Already Implemented)

The bridge code in `src/main.cpp`:

```cpp
#define ESP_BRIDGE_ENABLED  1       // Set to 0 to disable bridge functionality
#define ESP_BRIDGE_BAUD     115200  // Baud rate for ESP32 serial monitoring

#if ESP_BRIDGE_ENABLED

void setupESPBridge() {
    // Initialize Native USB for ESP32 serial monitoring
    SerialUSB.begin(ESP_BRIDGE_BAUD);
}

// Handle ESP32 serial bridge - bidirectional passthrough for monitoring
void handleESPBridge() {
    // Forward Native USB -> Serial1 (PC to ESP32)
    while (SerialUSB.available()) {
        Serial1.write(SerialUSB.read());
    }

    // Forward Serial1 -> Native USB (ESP32 to PC)
    while (Serial1.available()) {
        SerialUSB.write(Serial1.read());
    }
}

#endif // ESP_BRIDGE_ENABLED
```

### How the Bridge Works

The Due's Native USB port acts as a **transparent USB-to-serial adapter** for runtime monitoring:

- **Always active** - no mode switching or special commands
- **Bidirectional** - ESP32 output appears on PC, PC input goes to ESP32
- **Independent** - works alongside normal Due operation

### Programming the ESP32

Programming uses the TX0/RX0 pins (6-pin header), separate from Due communication
(IO4/IO14). Use the temporary FT232 manual-button programmer only for the first
OTA-capable flash or for recovery:

1. **Connect** temporary programmer to WT32-ETH01 programming header:
   - TXD -> RX0 (GPIO3)
   - RXD -> TX0 (GPIO1)
   - GND -> GND
   - Leave programmer 3.3V/5V/VCC disconnected when the WT32 is already powered
2. **Start upload** via PlatformIO:
   ```bash
   cd esp32_controller_arduino
   pio run -e wt32-eth01 -t upload --upload-port /dev/ttyUSB0
   ```
3. **Enter boot mode with the programmer buttons:**
   - Hold BOOT/IO0/FLASH
   - Tap EN/RST once
   - Keep holding BOOT for about two seconds while esptool connects
   - Release BOOT after esptool connects or starts writing

**No need to disconnect the Due serial wires** - it uses IO4/IO14, not TX0/RX0.
Do remove the temporary programmer and any IO0/EN/DTR/RTS wiring before normal
Ethernet operation. Routine updates after the first serial flash should use the
`wt32-eth01-ota` environment over Ethernet.

### Daily Usage

**Monitor Due/ESP32 control traffic:**
- Connect any serial terminal to Due Native USB port at 115200 baud
- ESP32-to-Due commands and Due status traffic appear continuously
- Use the temporary FT232 programmer on TX0/RX0 only when you need WT32
  boot/Ethernet logs
- No special commands needed

**Control Due:**
- Connect to Programming USB port
- Send HOME, STOP, STATUS, etc.
- Completely independent of ESP32

### Troubleshooting

**No output on Native USB:**
- Check Serial1 wiring (TX1→IO14, RX1→IO4 - they cross!)
- Verify ESP32 is powered and running
- Check baud rate matches (115200)

**Upload fails:**
- Start upload first, then hold BOOT/IO0/FLASH, tap EN/RST, and release BOOT
  after esptool connects
- Verify correct serial port for the temporary FT232 programmer
- Ensure the programmer is connected to TX0/RX0 (programming header)
- Ensure IO0 is released after upload and not connected during normal Ethernet use
- Ensure programmer 5V/3V3/VCC is not tied to the WT32 while it is powered from
  the telescope

## 7. Build & Flash Procedure

### First Serial Flash via Temporary FT232 Programmer

Use the temporary FT232 programmer with manual boot/reset buttons for the first
OTA-capable flash, or later recovery. It is not permanently installed. **No need
to disconnect the Due** - programming uses TX0/RX0, while Due communication uses
IO4/IO14.

1. **Connect the programmer to WT32-ETH01 programming header:**

   ```text
   Programmer TXD -> WT32 RX0 (GPIO3)
   Programmer RXD -> WT32 TX0 (GPIO1)
   Programmer GND -> WT32 GND
   ```

   Leave programmer 3.3V/5V/VCC disconnected while the WT32 is powered from the
   telescope/Due supply.

2. **Flash:**
   ```bash
   cd esp32_controller_arduino
   pio run -e wt32-eth01 -t upload --upload-port /dev/ttyUSB0
   ```

3. **Use the manual button timing while esptool connects:**
   - Hold BOOT/IO0/FLASH
   - Tap EN/RST once
   - Keep holding BOOT for about two seconds
   - Release BOOT after esptool connects or starts writing

The ESP32 resets after programming. Due communication resumes automatically.
Disconnect the temporary programmer before normal operation.

### Routine Network OTA Update

After the OTA-capable firmware has been installed once, routine ESP32 updates
should use Ethernet instead of serial:

```bash
cd esp32_controller_arduino
pio run -e wt32-eth01-ota -t upload
```

The repository OTA upload target is the current controller address,
`192.168.50.120`.

### Monitoring via Due Bridge

Once programmed, the Due Native USB port can monitor Serial1 control traffic
between the Due and ESP32:

- Connect terminal to Due Native USB port at 115200 baud
- ESP32-to-Due commands and Due status traffic appear continuously
- WT32 boot/Ethernet logs on TX0/RX0 require the temporary programmer or another
  serial adapter
- Disconnect the temporary programmer again after debugging normal operation

---

## 8. Testing Checklist

### Hardware Verification

- [ ] WT32-ETH01 powers up (LED activity)
- [ ] Temporary FT232 serial connection works
- [ ] Can enter boot mode and flash the OTA-capable firmware once
- [ ] Serial monitor shows boot messages

### Ethernet

- [ ] Ethernet link LED lights when cable connected
- [ ] Controller is reachable at `192.168.50.120` or the configured static/DHCP address
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

## 9. Dual-Board Support (Implemented)

Both ESP32-S3 and WT32-ETH01 are supported from the same codebase:

```bash
# Build for WT32-ETH01 (Ethernet)
pio run -e wt32-eth01

# Build for ESP32-S3 (USB CDC)
pio run -e esp32s3
```

The code uses `#ifdef BOARD_WT32_ETH01` / `#ifdef BOARD_ESP32S3` for board-specific features.

---

## 10. Parts List

| Item | Quantity | Notes |
|------|----------|-------|
| WT32-ETH01 | 1 | Main controller |
| Temporary FT232 programmer | 1 | Manual-button serial flashing, not permanently installed |
| Ethernet cable | 1 | Cat5e or better |
| Dupont wires | 4 | For Due connection |
| 5V power supply | 1 | If not powering from Due |

**Suppliers:**
- AliExpress: ~$8-10 for WT32-ETH01
- Amazon: ~$15-20 (faster shipping)

---

## 11. Quick Reference

### USB Ports

| Port                 | Purpose                                     |
|----------------------|---------------------------------------------|
| Due Programming USB  | Due commands (HOME, STOP, STATUS, etc.)     |
| Due Native USB       | ESP32 serial monitoring (115200 baud)       |
| Temporary FT232      | First ESP32 flash or recovery only          |

### Wiring Summary

| Connection | Pins |
|------------|------|
| Due TX1 (pin 18) → ESP32 | IO14 |
| Due RX1 (pin 19) ← ESP32 | IO4 |
| Temporary programmer TXD → ESP32 | RX0 (GPIO3) |
| Temporary programmer RXD ← ESP32 | TX0 (GPIO1) |

### Network Interfaces

| Interface  | Address                              |
|------------|--------------------------------------|
| Ethernet   | 192.168.50.120 (private link to the observatory computer) |
| Hostname   | http://srt-controller.local/         |
| WiFi AP    | 192.168.4.1 (SSID: SRT_Controller)   |
| Stellarium | Port 10001 on any interface          |

### Ethernet IP Configuration

Ethernet IP can be configured via the web interface (Network tab):

- **DHCP** (default): Automatically obtain IP from network
- **Static IP**: Manually configure IP, gateway, subnet, and DNS

Settings are stored in non-volatile memory and persist across reboots.
Changes require a reboot to take effect.

---

## References

- [WT32-ETH01 Datasheet](http://www.wireless-tag.com/portfolio/wt32-eth01/)
- [ESP32 Ethernet Examples](https://github.com/espressif/arduino-esp32/tree/master/libraries/Ethernet/examples)
- [LAN8720 PHY Datasheet](https://www.microchip.com/en-us/product/LAN8720A)
