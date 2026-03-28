# WT32-ETH01 Controller - Technical Manual

**Version 2.1 (Arduino/PlatformIO)**
**Acre Road Observatory, Glasgow**

---

## Table of Contents

1. [Overview](#1-overview)
2. [Installation](#2-installation)
3. [Configuration Reference](#3-configuration-reference)
4. [Source Code Structure](#4-source-code-structure)
5. [HTTP API](#5-http-api)
6. [Stellarium Protocol](#6-stellarium-protocol)
7. [Coordinate System](#7-coordinate-system)
8. [Time Synchronization](#8-time-synchronization)
9. [Networking](#9-networking)
10. [Development](#10-development)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Overview

The WT32-ETH01 controller provides the high-level interface for the SRT drive system:

- **Web interface** for manual control and monitoring
- **Stellarium integration** via TCP telescope protocol
- **Coordinate transforms** from RA/Dec (J2000) to Alt/Az
- **Ephemeris calculations** for Sun and Moon positions
- **Time synchronization** via NTP or browser fallback
- **Runtime configurable settings** saved to flash (NVS)
- **Networking** via native Ethernet + WiFi (AP + station mode)

### Architecture

```
+------------------+     +------------------+     +------------------+
|   Stellarium     |     |   Web Browser    |     |   NTP Server     |
|   (TCP:10001)    |     |   (HTTP:80)      |     |                  |
+--------+---------+     +--------+---------+     +--------+---------+
         |                        |                        |
         +------------------------+------------------------+
                                  |
                         +--------+---------+
                         |                  |
                         |   WT32-ETH01     |
                         |   (Arduino C++)  |
                         |                  |
                         |  main.cpp        |
                         |  coordinates.cpp |
                         |  web_server.cpp  |
                         |  stellarium.cpp  |
                         +--------+---------+
                                  |
                             UART Serial
                                  |
                         +--------+---------+
                         |   Arduino Due    |
                         +------------------+
```

### Source File Structure

| File | Purpose |
|------|---------|
| `src/main.cpp` | Main application, tracking loop, time sync |
| `src/config.h` | Default configuration values |
| `src/settings.cpp/h` | Runtime settings with NVS persistence |
| `src/coordinates.cpp/h` | Astronomical coordinate transforms |
| `src/web_server.cpp/h` | HTTP server and web interface |
| `src/stellarium.cpp/h` | Stellarium telescope protocol |
| `src/srt_serial.cpp/h` | Serial communication with Due |
| `src/wifi_manager.cpp/h` | WiFi AP/station management |
| `src/state.h` | Global state structure |
| `src/index_html.h` | Embedded web interface HTML |

---

## 2. Installation

### Prerequisites

- WT32-ETH01 module (ESP32 with built-in LAN8720 Ethernet)
- USB-TTL adapter (CH340 or CP2102) for programming
- [PlatformIO](https://platformio.org/) (VS Code extension or CLI)

### Build and Upload

```bash
cd new_SRT_drive/esp32_controller_arduino

# Build for WT32-ETH01
pio run -e wt32-eth01

# Upload (requires boot mode: hold IO0, press EN, release IO0)
pio run -e wt32-eth01 --target upload

# Monitor serial output
pio device monitor
```

### Verify Installation

After upload, the serial monitor should show:

```
SRT Controller starting...
Settings loaded from NVS
Due serial initialized
Connecting to WiFi: <saved_network>
...
Async web server listening on port 80
Stellarium async server listening on port 10001
Free memory: 255000 bytes
WiFi IP: 192.168.x.x
AP IP: 192.168.4.1
```

---

## 3. Configuration Reference

### Compile-Time Defaults (config.h)

These values are used as defaults when no saved settings exist:

```cpp
// WiFi Access Point
#define WIFI_AP_SSID "SRT_Controller"
#define WIFI_AP_PASSWORD "radio1420"

// Serial connection to Arduino Due (WT32-ETH01 pins)
#define DUE_UART_TX 32   // WT32 GPIO32 -> Due RX (pin 19)
#define DUE_UART_RX 33   // WT32 GPIO33 <- Due TX (pin 18)
#define DUE_BAUD_RATE 115200

// Observer location (Acre Road Observatory, Glasgow)
#define OBSERVER_LAT 55.902426
#define OBSERVER_LON -4.307865

// Mount software limits (degrees)
#define MOUNT_AZ_MIN 2.0
#define MOUNT_AZ_MAX 353.0
#define MOUNT_ALT_MIN 0.0
#define MOUNT_ALT_MAX 90.0

// Home position (degrees)
#define HOME_ALT 0.0
#define HOME_AZ 180.0

// Position deadband (degrees)
#define POSITION_DEADBAND 0.25
```

### Runtime Settings (Web Interface)

Settings can be changed via the web interface Settings tab:

| Setting | Description | Default |
|---------|-------------|---------|
| Observer Lat/Lon | Observatory coordinates | Glasgow |
| Software Limits | Az/Alt min/max for tracking | 2-353, 0-90 |
| Home Position | Park position when target below horizon | Alt=0, Az=180 |
| Update Tolerance | Minimum position change to trigger update | 0.25 deg |
| Page Name | Web interface title | "SRT Controller" |
| AP SSID/Password | WiFi access point credentials | SRT_Controller/radio1420 |

Settings are saved to ESP32 flash (NVS) and persist across reboots.

---

## 4. Source Code Structure

### main.cpp

Main application entry point and tracking loop.

**Key Functions:**

- `setup()` - Initialize hardware, WiFi, web server
- `loop()` - Handle servers, update tracking
- `updateTracking()` - Convert RA/Dec to Alt/Az, send to Due
- `syncTimeNTP()` - Sync time from NTP server

**Tracking Loop Behavior:**

1. Runs every 1 second when `trackingEnabled` is true
2. For Sun/Moon targets, refreshes ephemeris every 30 seconds
3. Converts current RA/Dec to Alt/Az using current time
4. Checks mount software limits before sending commands
5. Sends position to Due if within limits and changed beyond deadband

### coordinates.cpp

Astronomical coordinate transformations. All functions are pure.

| Function | Input | Output |
|----------|-------|--------|
| `raDecToAltAz()` | RA (h), Dec (deg) | Alt, Az (deg) |
| `altAzToRaDec()` | Alt, Az (deg) | RA (h), Dec (deg) |
| `galacticToEquatorial()` | l, b (deg) | RA (h), Dec (deg) |
| `getSunPosition()` | (none) | RA (h), Dec (deg) |
| `getMoonPosition()` | (none) | RA (h), Dec (deg) |

### srt_serial.cpp

Serial communication with Arduino Due.

```cpp
srtSerial.sendTarget(alt, az);   // Send position command
srtSerial.requestStatus();        // Request status update
srtSerial.readStatus();           // Read and parse status
srtSerial.getCurrentAlt();        // Get current position
srtSerial.getStatusStr();         // Get status string
```

### settings.cpp

Runtime settings with NVS persistence.

```cpp
settings.load();              // Load from NVS (called in setup)
settings.save();              // Save to NVS
settings.resetToDefaults();   // Reset to compile-time defaults
```

---

## 5. HTTP API

All endpoints return JSON unless noted.

### Status Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web interface (HTML) |
| `/status` | GET | Mount position and status |
| `/tracking` | GET | Current tracking state |
| `/ephemeris` | GET | Sun and Moon positions |
| `/time/status` | GET | Time sync status |
| `/settings` | GET | Current settings |

**Example: `/status` response**

```json
{
  "alt": 45.0,
  "az": 180.0,
  "target_alt": 45.0,
  "target_az": 180.0,
  "alt_current_a": 0.15,
  "az_current_a": 0.20,
  "status": "Ready",
  "fault": "",
  "is_slewing": false,
  "calibrator_on": false
}
```

### Control Endpoints

| Endpoint | Parameters | Description |
|----------|------------|-------------|
| `/goto` | `ra`, `dec` | Slew to RA/Dec (J2000) |
| `/goto/galactic` | `l`, `b` | Slew to galactic coords |
| `/track/radec` | `ra`, `dec` | Track RA/Dec (J2000) |
| `/track/galactic` | `l`, `b` | Track galactic coords |
| `/track/sun` | (none) | Track Sun |
| `/track/moon` | (none) | Track Moon |
| `/tracking/enable` | `enable=0/1` | Enable/disable tracking |
| `/direct` | `alt`, `az` | Go to Alt/Az directly |
| `/offset` | `alt`, `az` | Set pointing offset (degrees) |
| `/offset/clear` | (none) | Clear pointing offset |
| `/calibrator` | `state=on/off/toggle` | Control calibrator noise source |

### Settings Endpoints

| Endpoint | Description |
|----------|-------------|
| `/settings` | GET current settings |
| `/settings/save` | Save settings (with query params) |
| `/settings/reset` | Reset to defaults |

### Network Endpoints

| Endpoint | Description |
|----------|-------------|
| `/wifi/status` | Network status (includes WiFi, Ethernet, MAC addresses) |
| `/wifi/scan` | Scan for WiFi networks |
| `/wifi/connect` | Connect to WiFi network |
| `/wifi/forget` | Forget saved WiFi credentials |
| `/eth/save` | Save Ethernet settings (dhcp, ip, gateway, subnet, dns) |

The `/wifi/status` response includes `eth_mac` and `wifi_mac` fields for device identification.

---

## 6. Stellarium Protocol

The controller implements the Stellarium telescope protocol on TCP port 10001.

### Stellarium Setup

1. **Configuration > Plugins > Telescope Control**
2. Enable plugin and restart Stellarium
3. Add telescope:
   - Type: "External software or remote computer"
   - Host: ESP32 IP address (e.g., `192.168.4.1`)
   - Port: `10001`
4. Connect
5. Select any object and press `Ctrl+1` to slew

### Coordinate Handling

- Stellarium sends **J2000** coordinates
- Controller converts to Alt/Az using current time
- Position reports return current target as J2000

---

## 7. Coordinate System

### Reference Frame

All equatorial coordinates use **J2000** (epoch J2000.0, equinox J2000.0):

- Standard for modern star catalogs
- Used by Stellarium, SIMBAD, and most planetarium software

### Precession

The controller applies IAU 1976 precession to convert J2000 coordinates to the equinox of the current date before computing Alt/Az.

### Sun and Moon

Sun and Moon positions are calculated using simplified algorithms:

- **Sun accuracy:** ~1 arcmin
- **Moon accuracy:** ~5-10 arcmin

---

## 8. Time Synchronization

Accurate UTC time is required for coordinate transforms.

### NTP (Primary)

On startup with network connection:

```cpp
configTime(0, 0, "pool.ntp.org");
```

### Browser Fallback

If NTP fails, the web interface automatically sends browser time on page load.

### Time Status

Check via `/time/status`:

```json
{
  "synced": true,
  "source": "NTP",
  "utc": "2026-03-15 14:30:00",
  "timestamp": 1773766200
}
```

---

## 9. Networking

### Network Modes

The ESP32 operates in **AP+STA** mode:

1. **WiFi Access Point** - Always active at 192.168.4.1
2. **WiFi Station** - Connects to saved network if available

### Startup Sequence

1. Start WiFi Access Point
2. Load saved WiFi credentials
3. Attempt connection to saved network (15s timeout)
4. If connected: disable AP (single network mode)
5. If failed: keep AP active for configuration

### IP Addresses

| Interface | IP Address |
|-----------|------------|
| WiFi AP | 192.168.4.1 (fixed) |
| WiFi Station | DHCP assigned |
| Ethernet (WT32-ETH01) | DHCP or static (configurable) |

### Ethernet Configuration (WT32-ETH01)

Ethernet IP can be configured via the web interface Network tab:

- **DHCP** (default): Automatically obtain IP from network
- **Static IP**: Manually configure IP, gateway, subnet, and DNS

Settings are stored in non-volatile memory (NVS) and persist across reboots.
Changes require a reboot to take effect.

---

## 10. Development

### Serial Monitor

```bash
pio device monitor -b 115200
```

### Debug Output

Debug messages are only printed when USB Serial is connected:

```cpp
#define DBG(x) if (Serial) { x; }
DBG(Serial.println("Debug message"));
```

This allows the controller to run without a serial terminal attached.

### Memory Usage

Typical usage: ~42% flash, ~14% RAM on WT32-ETH01 (ESP32 with 4MB flash).

### Building

```bash
# Build only
pio run

# Build and upload
pio run --target upload

# Clean build
pio run --target clean
```

---

## 11. Troubleshooting

### Web Interface Issues

| Problem | Solution |
|---------|----------|
| Can't connect to 192.168.4.1 | Verify connected to SRT_Controller WiFi |
| Page loads but no data | Check Due serial connection |
| Settings won't save | Check NVS, try reset to defaults |

### Coordinate Issues

| Problem | Solution |
|---------|----------|
| Position doesn't match sky | Check observer lat/lon in settings |
| Large errors (>1 deg) | Check time sync status |
| Sun/Moon wrong by ~20 arcmin | Normal - simplified ephemeris |

### Stellarium Issues

| Problem | Solution |
|---------|----------|
| Can't connect | Check IP and port 10001 |
| Connects but no slew | Click object then Ctrl+1 |
| Wrong position shown | Verify time is synced |

### Serial Issues

| Problem | Solution |
|---------|----------|
| No status updates | Check TX/RX wiring (cross-connect) |
| Garbled data | Verify baud rate (115200) |
| Works only with terminal | Normal behavior before USB CDC fix |

---

## Appendix: Quick Reference

### Default Credentials

| Setting | Value |
|---------|-------|
| WiFi AP SSID | SRT_Controller |
| WiFi AP Password | radio1420 |
| AP IP | 192.168.4.1 |
| Web Port | 80 |
| Stellarium Port | 10001 |

### Pin Assignments (WT32-ETH01)

| Function | GPIO |
|----------|------|
| Due TX | 32 |
| Due RX | 33 |

### Coordinate Ranges

| Coordinate | Range |
|------------|-------|
| RA | 0 - 24 hours |
| Dec | -90 to +90 degrees |
| Galactic l | 0 - 360 degrees |
| Galactic b | -90 to +90 degrees |
| Altitude | 0 - 90 degrees |
| Azimuth | 0 - 355 degrees |

---

**License:** MIT License - Acre Road Observatory, University of Glasgow
