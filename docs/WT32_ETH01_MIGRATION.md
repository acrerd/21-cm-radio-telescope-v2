# WT32-ETH01 Setup Guide

**Status:** Done. The WT32-ETH01 is the deployed controller and the default
build (`default_envs = wt32-eth01`). The `esp32s3` environment is legacy; that
board is no longer used.
**Purpose:** Record of the move from ESP32-S3 to WT32-ETH01 for native Ethernet, and the flashing and wiring procedure that still applies.

**Current state:** the controller is on a private point-to-point link to the
observatory computer, on **DHCP** with its address pinned by MAC on the host
(`192.168.50.120`, host `192.168.50.1`); see CLAUDE.md "Controller network" and
`docs/OBSERVATORY_HOST_SETUP.md`. The static-IP defaults name the same link.
WiFi AP fallback `192.168.4.1`. Firmware updates go over Ethernet OTA. The
controller's HTTP API and internals are in `docs/ESP32_CONTROLLER.md`.

`pio` below means `~/.platformio/penv/bin/pio` on the observatory Linux host
(`~/.platformio/penv/Scripts/pio.exe` on Windows); it is not on PATH.

---

## 1. Overview

The WT32-ETH01 is a compact ESP32 module with built-in LAN8720 Ethernet PHY. Unlike the earlier ESP32-S3 + W5500 SPI arrangement, it uses the ESP32's native RMII Ethernet MAC.

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

The OTA target defaults to `192.168.50.120` and port `3232`. The address comes
from the host's DHCP reservation; if the controller is ever moved to another
network, update `upload_port` in `esp32_controller_arduino/platformio.ini`. The
OTA password is set in `src/config.h` (`OTA_PASSWORD`) and repeated in the
`--auth` upload flag. The loop watchdog (30 s) is fed from the OTA progress
callback, so an upload does not trip it.

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

The ESP32 firmware supports both boards from the same codebase using
conditional compilation. WT32-ETH01 is the default and canonical target; the
ESP32-S3 environment is legacy.

### 4.1 platformio.ini

As in `esp32_controller_arduino/platformio.ini` (the `esp32s3` and `native`
environments omitted):

```ini
[platformio]
default_envs = wt32-eth01

[env:wt32-eth01]
platform = espressif32
board = wt32-eth01
framework = arduino
monitor_speed = 115200
board_build.partitions = partitions_wt32_ota.csv

; No USB CDC - uses standard UART
build_flags =
    -DBOARD_WT32_ETH01
    -DCORE_DEBUG_LEVEL=0

upload_speed = 460800
upload_protocol = esptool

lib_deps =
    https://github.com/me-no-dev/AsyncTCP.git
    https://github.com/me-no-dev/ESPAsyncWebServer.git

[env:wt32-eth01-ota]
extends = env:wt32-eth01
upload_protocol = espota
upload_port = 192.168.50.120
upload_flags =
    --auth=srt-ota-1420
    --port=3232
```

`-DBOARD_WT32_ETH01` selects the WT32 pins and `ETHERNET_ENABLED` in
`config.h`. `partitions_wt32_ota.csv` gives two 1.6 MB app slots (`ota_0`,
`ota_1`) so an OTA image is written beside the running one:

```
# Name,   Type, SubType, Offset,  Size, Flags
nvs,      data, nvs,     0x9000,  0x5000,
otadata,  data, ota,     0xe000,  0x2000,
app0,     app,  ota_0,   0x10000, 0x190000,
app1,     app,  ota_1,   0x1A0000,0x190000,
spiffs,   data, spiffs,  0x330000,0xD0000,
```

### 4.2 config.h Changes

```cpp
#ifdef BOARD_WT32_ETH01
    // WT32-ETH01: Serial to Due via GPIO4/14
    // Note: GPIO32/33 labelled CFG/485_EN on RS-485 variants - avoid those
    // TX0/RX0 (GPIO1/3) reserved for programming - no need to disconnect Due
    #define DUE_UART_TX 4    // WT32 IO4 -> Due RX (pin 19)
    #define DUE_UART_RX 14   // WT32 IO14 <- Due TX (pin 18)

    // Ethernet PHY configuration (LAN8720) - pin numbers only
    // The actual PHY type constants are defined by ETH.h
    #define ETH_PHY_ADDR_CFG    1
    #define ETH_PHY_MDC_PIN     23
    #define ETH_PHY_MDIO_PIN    18
    #define ETH_PHY_POWER_PIN   16

    // Enable Ethernet support
    #define ETHERNET_ENABLED 1
#endif

// Ethernet static-IP fallback, used only if DHCP is turned off in the web UI
#define DEFAULT_ETH_STATIC_IP "192.168.50.120"
#define DEFAULT_ETH_GATEWAY   "192.168.50.1"
#define DEFAULT_ETH_SUBNET    "255.255.255.0"
#define DEFAULT_ETH_DNS       "192.168.50.1"
```

The names carry `_CFG`/`_PIN` suffixes to stay clear of the `ETH_PHY_*`
names the core's `ETH.h` uses. The PHY type (`ETH_PHY_LAN8720`) and clock
mode (`ETH_CLOCK_GPIO0_IN`) are passed directly in `ETH.begin()`.

### 4.3 New Ethernet Initialization (main.cpp)

Add Ethernet support alongside WiFi:

Abridged from `esp32_controller_arduino/src/main.cpp`:

```cpp
#include <ETH.h>

bool ethConnected = false;
bool ethNeedNtpSync = false;  // serviced from loop()
String ethIP = "";

void onEthEvent(arduino_event_id_t event) {
    switch (event) {
        case ARDUINO_EVENT_ETH_START:
            ETH.setHostname(CONTROLLER_HOSTNAME);
            break;
        case ARDUINO_EVENT_ETH_GOT_IP:
            ethConnected = true;
            ethIP = ETH.localIP().toString();
            ethNeedNtpSync = true;     // re-armed on every lease, so a reconnect re-syncs
            startDiscoveryServices();  // mDNS
            startOTAService();         // ArduinoOTA on port 3232
            break;
        case ARDUINO_EVENT_ETH_DISCONNECTED:
        case ARDUINO_EVENT_ETH_STOP:
            ethConnected = false;
            ethIP = "";
            break;
        default:
            break;
    }
}

void setup() {
    // ...
    WiFi.onEvent(onEthEvent);
    ETH.begin(ETH_PHY_ADDR_CFG, ETH_PHY_POWER_PIN, ETH_PHY_MDC_PIN,
              ETH_PHY_MDIO_PIN, ETH_PHY_LAN8720, ETH_CLOCK_GPIO0_IN);
    if (!settings.ethUseDHCP) {
        ETH.config(ip, gateway, subnet, dns);   // from settings, if all four parse
    }
    // wait up to 5 s for a lease, then start SNTP

    // WiFi AP always starts, alongside Ethernet
    wifiManager.startup();
    // ...
}
```

### 4.4 Network Status Updates (web_server.cpp)

Add Ethernet status to `/wifi/status` endpoint:

```cpp
json += "\"eth_available\":true,";
json += "\"eth_connected\":" + String(ethConnected ? "true" : "false") + ",";
json += "\"eth_ip\":\"" + ethIP + "\",";
json += "\"eth_mac\":\"" + ETH.macAddress() + "\",";
json += "\"eth_dhcp\":" + String(settings.ethUseDHCP ? "true" : "false") + ",";
// ... eth_static_ip, eth_gateway, eth_subnet, eth_dns
```

### 4.5 Remove USB CDC Workarounds

The WT32-ETH01 uses standard UART, not USB CDC. The `DBG()` macro was kept;
on this board its guard is always true. The ESP32-S3-only `setTxTimeoutMs(0)`
and 3 s enumeration delay are under `#ifdef BOARD_ESP32S3`.

```cpp
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
| `/calibrator` | GET | `on=1\|true` or `on=0`; sends `CAL ON`/`CAL OFF` to the Due. Nothing is connected to it since the noise diode was removed (issue #39) |

The full endpoint list is in `docs/ESP32_CONTROLLER.md` section 5.

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
PC USB #2 ─────────►│  Native USB     │◄──── mirror of ESP32→Due commands
                    │  (SerialUSB)    │
                    │       ↑         │
                    │    Serial1      │────► WT32-ETH01 (IO4/IO14)
                    └─────────────────┘

                   Temporary FT232 ─────► WT32-ETH01 TX0/RX0 (first flash/recovery)
```

**Programming USB (Serial):** Due programming, commands (HOME, STOP, etc.), status output

**Native USB (SerialUSB):** Mirror of the ESP32-to-Due command stream, plus a path for typing to the ESP32

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

The bridge code in `src/main.cpp` is in two places. `handleESPBridge()`, called
at the top of `loop()`, forwards only one way:

```cpp
#define ESP_BRIDGE_ENABLED  1       // Set to 0 to disable bridge functionality
#define ESP_BRIDGE_BAUD     115200  // Serial1 baud rate (ESP32 link)

#if ESP_BRIDGE_ENABLED

void setupESPBridge() {
    // SerialUSB on Due is native USB CDC - baud rate parameter is ignored
    // but begin() is required to initialize the USB stack
    SerialUSB.begin(0);
}

// Note: Serial1->SerialUSB forwarding is now done in processSerialInput()
// where we also process commands from ESP32
void handleESPBridge() {
    // Forward Native USB -> Serial1 (PC to ESP32)
    while (SerialUSB.available()) {
        Serial1.write(SerialUSB.read());
    }
}

#endif // ESP_BRIDGE_ENABLED
```

The other direction is inside `processSerialInput()`, which is the Due's reader
of the controller's commands. Each byte read from Serial1 is copied to the
Native USB port and then parsed:

```cpp
while (Serial1.available() > 0) {
    char c = Serial1.read();
    #if ESP_BRIDGE_ENABLED
    SerialUSB.write(c);          // mirror for monitoring
    #endif
    // ... line assembly, then processCommand() with cmdFromSerial1 = true
}
```

Do not restore the earlier version of `handleESPBridge()` that also drained
`Serial1` into `SerialUSB`. Serial1 has one reader. A second loop that empties
it first takes the controller's `STATUS` polls and drive targets away from
`processSerialInput()`, so the Due never executes them.

### How the Bridge Works

- **Always active** in the main loop - no mode switching or special commands
- **ESP32 to Due only**: bytes the ESP32 sends (drive targets, `STATUS`, `HOME`,
  `STOP`, `CAL ON/OFF`) are mirrored to Native USB. The Due's replies on
  Serial1 (status lines, `ACK DRIVE`, `ERR ...`) are **not** mirrored; watch
  those on the Programming USB port or in the controller's `/serial/log`
- **Native USB input goes to the ESP32**, which reads it as if it came from the
  Due
- Not serviced during a homing: `performHoming()` reads Serial1 itself
  (`homingServiceSerial()`) and does not mirror it
- The ESP32's own console (UART0, TX0/RX0) is not on this bridge

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

**Monitor ESP32-to-Due commands:**
- Connect any serial terminal to Due Native USB port (CDC; the baud setting is ignored)
- The ESP32's commands appear: a `STATUS` poll a second and each drive target.
  The Due's replies do not
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
- Check Serial1 baud rate matches on both ends (115200)
- Nothing is mirrored while the Due is homing

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

Once programmed, the Due Native USB port shows the ESP32-to-Due half of the
Serial1 traffic:

- Connect terminal to Due Native USB port
- ESP32-to-Due commands appear; the Due's replies do not (see section 6)
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

Both ESP32-S3 and WT32-ETH01 build from the same codebase. WT32-ETH01 is the
deployed board, the default environment, and must always build clean; the
ESP32-S3 is no longer used and is kept compiling when convenient.

```bash
# Build for WT32-ETH01 (Ethernet, default)
pio run -e wt32-eth01

# Build for ESP32-S3 (legacy, USB CDC)
pio run -e esp32s3

# Host unit tests (no board)
pio test -e native
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
| Due Native USB       | Mirror of ESP32→Due commands (not replies)  |
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

- **DHCP** (default, and the normal mode): the observatory computer's
  reservation for the controller's MAC gives `192.168.50.120`
- **Static IP**: Manually configure IP, gateway, subnet, and DNS; pre-filled
  with `192.168.50.120`, gateway/DNS `192.168.50.1`, `255.255.255.0`

Settings are stored in non-volatile memory and persist across reboots.
Changes require a reboot to take effect.

---

## References

- [WT32-ETH01 Datasheet](http://www.wireless-tag.com/portfolio/wt32-eth01/)
- [ESP32 Ethernet Examples](https://github.com/espressif/arduino-esp32/tree/master/libraries/Ethernet/examples)
- [LAN8720 PHY Datasheet](https://www.microchip.com/en-us/product/LAN8720A)
