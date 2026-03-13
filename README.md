# SRT Drive Controller

A complete control system for a Small Radio Telescope (SRT) alt-azimuth mount at Acre Road Observatory, Glasgow.

## System Overview

The system consists of two controllers:

1. **Arduino Due** - Low-level motor control, position tracking, limit switches, current sensing
2. **ESP32-S3** - WiFi interface, web UI, Stellarium integration, RA/Dec to Alt/Az coordinate transforms

```
                                    +------------------+
                                    |   Stellarium     |
                                    |   (TCP:10001)    |
                                    +--------+---------+
                                             |
+----------------+                  +--------+---------+
|  Web Browser   +----------------->|                  |
|  (HTTP:80)     |                  |    ESP32-S3      |
+----------------+                  |                  |
                                    |  - WiFi AP/STA   |
                                    |  - Web server    |
                                    |  - RA/Dec->Alt/Az|
                                    |  - NTP time sync |
                                    +--------+---------+
                                             |
                                        UART (Serial)
                                             |
                                    +--------+---------+
                                    |                  |
                                    |   Arduino Due    |
                                    |                  |
                                    |  - Motor PWM     |
                                    |  - Encoders      |
                                    |  - Limit switches|
                                    |  - Current sense |
                                    +--------+---------+
                                             |
                                        Motors/Mount
```

## Hardware

### Arduino Due
- **Microcontroller:** Arduino Due (ARM Cortex-M3)
- **Motors:** DC motors with H-bridge drivers
- **Position sensing:** Reed switch encoders (0.5° resolution)
- **Current sensing:** ACS712 hall-effect sensors
- **Limit switches:** Altitude ~0°, Azimuth ~0° and ~355°

### ESP32-S3
- ESP32-S3 development board
- W5500 Ethernet module (optional, recommended for fixed installations)
- Connects to Due via UART serial

### Wiring: ESP32-S3 to Arduino Due

| ESP32-S3 | Arduino Due | Function |
|----------|-------------|----------|
| GPIO17   | Pin 19 (RX1)| ESP32 TX -> Due RX |
| GPIO18   | Pin 18 (TX1)| ESP32 RX <- Due TX |
| GND      | GND         | Common ground |

### Wiring: ESP32-S3 to W5500 Ethernet

| W5500 | ESP32-S3 | Function |
|-------|----------|----------|
| MOSI  | GPIO11   | SPI data out |
| MISO  | GPIO13   | SPI data in |
| SCK   | GPIO12   | SPI clock |
| CS    | GPIO10   | Chip select |
| RST   | GPIO9    | Reset (or tie to 3.3V) |
| 3.3V  | 3.3V     | Power |
| GND   | GND      | Ground |

### Ethernet Options

The W5500 is the default Ethernet option, but alternatives exist depending on your requirements.

#### SPI-Based Options (ESP32-S3 compatible)

| Chip | Speed | MicroPython | Notes |
|------|-------|-------------|-------|
| **W5500** | 10/100 Mbps | `network.WIZNET5K` | Recommended. Hardware TCP/IP stack, 8 sockets |
| W5100S | 10/100 Mbps | `network.WIZNET5K` | Older variant, 4 sockets |
| ENC28J60 | 10 Mbps | Limited | Cheaper but slower, software TCP/IP stack |

The W5500 is preferred for its hardware TCP/IP offloading and reliable MicroPython support.

#### Native Ethernet (Original ESP32 only)

The **original ESP32** (not S3) has a built-in RMII Ethernet MAC, enabling use of PHY chips like:

| Option | Notes |
|--------|-------|
| **LAN8720 PHY module** | ~$3, requires specific RMII GPIO pins |
| **Olimex ESP32-POE** | All-in-one board with Ethernet + Power over Ethernet |
| **Olimex ESP32-Gateway** | ESP32 + Ethernet + WiFi gateway board |

Native Ethernet advantages:
- Faster and more reliable than SPI
- Fewer GPIO pins used (dedicated RMII interface)
- Native `network.LAN()` support in MicroPython

**Trade-off:** The ESP32-S3 has more RAM and faster CPU than the original ESP32, but lacks native Ethernet. For fixed observatory installations where Ethernet reliability is critical (e.g., Stellarium connection), consider using an Olimex ESP32-POE instead. The controller code requires only minor changes to `ethernet.py` to use `network.LAN()`.

---

## Installation

### Prerequisites

- [PlatformIO](https://platformio.org/) (VS Code extension or CLI)
- Python 3.x with `esptool` and `mpremote`:
  ```bash
  pip install platformio esptool mpremote
  ```

### 1. Arduino Due Firmware

```bash
cd new_SRT_drive

# Build and upload
pio run -e due -t upload

# Monitor serial output
pio device monitor -b 115200
```

### 2. ESP32-S3 MicroPython

#### Flash MicroPython (one-time)

Download firmware from [micropython.org/download/ESP32_GENERIC_S3](https://micropython.org/download/ESP32_GENERIC_S3/)

```bash
# Erase flash (hold BOOT button while connecting if needed)
esptool.py --chip esp32s3 --port COM5 erase_flash

# Flash MicroPython
esptool.py --chip esp32s3 --port COM5 write_flash -z 0 ESP32_GENERIC_S3-*.bin
```

Replace `COM5` with your port (`/dev/ttyUSB0` on Linux, `/dev/tty.usbserial-*` on Mac).

#### Upload Controller Code

```bash
cd new_SRT_drive/esp32_controller

# Upload all files
mpremote connect COM5 cp *.py :

# Reset to run
mpremote connect COM5 reset
```

Or use **MicroPico** VS Code extension for easier development.

---

## Configuration

### ESP32-S3 Settings

Edit `esp32_controller/config.py` before uploading:

```python
# WiFi Access Point
WIFI_AP_SSID = "SRT_Controller"
WIFI_AP_PASSWORD = "radio1420"

# Serial pins to Arduino Due
DUE_UART_TX = 17
DUE_UART_RX = 18

# Observer location (for coordinate conversion)
OBSERVER_LAT = 55.9    # Latitude (degrees)
OBSERVER_LON = -4.3    # Longitude (west negative)
```

### Arduino Due Pin Assignments

| Function | Pin |
|----------|-----|
| Az PWM | 8 |
| Az Direction | 9 |
| Alt PWM | 10 |
| Alt Direction | 11 |
| Az Encoder | 12 |
| Alt Encoder | 13 |
| Az Current | A1 |
| Alt Current | A0 |
| Serial1 TX | 18 |
| Serial1 RX | 19 |

---

## Usage

### Connecting to the Web Interface

#### Option 1: Direct WiFi (always available)
1. Connect to WiFi: `SRT_Controller` (password: `radio1420`)
2. Browse to: `http://192.168.4.1`

#### Option 2: Your Network
1. Connect to the AP first
2. Go to **WiFi** tab, click **Scan**
3. Select your network and enter password
4. Note the new IP address
5. Connect your computer to same network
6. Browse to the new IP

Credentials are saved and the ESP32 auto-reconnects on boot. The AP stays active as fallback.

### Web Interface

#### Control Tab

| Section | Controls |
|---------|----------|
| **Current Position** | Shows Alt, Az, motor currents, status, current tracking target |
| **Quick Targets** | Track Sun, Track Moon, Stop Tracking |
| **Equatorial (RA/Dec)** | Enter RA (hours) and Dec (degrees), Go To or Track |
| **Galactic (l/b)** | Enter galactic longitude/latitude, Go To or Track |
| **Direct Control** | Enter Alt/Az directly, Go Direct / Home |

**Coordinate Systems:**
- **RA/Dec**: Right Ascension (0-24 hours), Declination (-90 to +90 degrees)
- **Galactic**: Galactic longitude l (0-360°), latitude b (-90 to +90°)
- **Alt/Az**: Altitude (0-90°), Azimuth (0-355°)

**Tracking Modes:**
- **Go To**: Slew to position once (no tracking)
- **Track**: Continuously update position as Earth rotates
- **Sun/Moon**: Automatically updates coordinates as they move across the sky

#### Network Tab

- **Ethernet**: Connection status, IP address, MAC address
- **WiFi**: Access Point status, station connection status
- **WiFi Config**: Scan and connect to WiFi networks, forget saved credentials

**Network Priority:** Ethernet provides a stable wired connection for Stellarium. WiFi AP remains active for mobile configuration access.

### Stellarium Integration

1. In Stellarium: **Configuration > Plugins > Telescope Control**
2. Enable and restart Stellarium
3. **Add** telescope:
   - Type: External software or remote computer
   - Host: `192.168.4.1` (or network IP)
   - Port: `10001`
4. Click **Connect**
5. Select any object and press `Ctrl+1` to slew

### Serial Commands (Arduino Due)

Connect via USB at 115200 baud.

#### Motion
| Command | Description |
|---------|-------------|
| `45 180` | Slew to Alt=45°, Az=180° |
| `HOME` | Run homing sequence |
| `STOP` | Emergency stop |
| `RESET` | Clear fault and re-home |

#### Status
| Command | Description |
|---------|-------------|
| `STATUS` | Show current position |
| `CONFIG` | Show configuration |
| `HELP` | List all commands |

#### Configuration
| Command | Description |
|---------|-------------|
| `SET ALTMIN 0` | Minimum altitude (degrees) |
| `SET ALTMAX 90` | Maximum altitude (degrees) |
| `SET CURRENT 4.5` | Current limit (Amps) |
| `SET RAMPUP 1000` | Acceleration time (ms) |
| `SAVE` | Save to flash |
| `DEFAULTS` | Reset to defaults |

#### Status Output Format
```
Alt:45.0 Az:180.0 Ialt:0.15A Iaz:0.20A Status:Ready
Alt:45.0 Az:180.0 Ialt:0.25A Iaz:0.30A Status:Slewing -> Alt:60.0 Az:200.0
Alt:45.0 Az:180.0 Ialt:0.00A Iaz:0.00A Status:FAULT [Motor stalled]
```

---

## Operation

### Startup Sequence
1. Power on both controllers
2. Arduino Due homes automatically (drives to limit switches)
3. ESP32 starts WiFi AP and connects to saved network (if any)
4. ESP32 syncs time via NTP (if internet available)
5. System ready when Due status shows "Ready"

### Time Synchronization
Accurate time is required for coordinate calculations. The ESP32 syncs time automatically:

1. **NTP (primary):** If connected to the internet, time syncs from NTP servers
2. **Browser fallback:** If NTP fails, the web interface automatically sends your browser's time to the ESP32 when you open the page

Time status is shown on the Control tab (e.g., "2024-03-13 14:30:00 UTC (NTP)").

### Tracking Mode
When **Track** is enabled:
- ESP32 continuously converts RA/Dec to Alt/Az using current time
- Updates sent to Due every second
- Mount follows object as Earth rotates

### Safety Features
- **Position limits:** Alt 0-90°, Az 0-355°
- **Limit switches:** Physical stops at extremes
- **Current limiting:** Motors stop on overcurrent
- **Stall detection:** Motors stop if position doesn't change

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Can't connect to web UI | Check WiFi connection, try AP mode at 192.168.4.1 |
| Motors don't move | Check Due serial for FAULT status, verify homing completed |
| Position incorrect | Run `HOME` command, check limit switches |
| Stellarium won't connect | Verify IP and port 10001, check ESP32 is running |
| Coordinates don't match sky | Check observer lat/lon, verify NTP time sync |

---

## File Structure

```
new_SRT_drive/
├── platformio.ini
├── src/
│   ├── main.cpp            # Arduino Due firmware
│   └── config.h
├── docs/
│   └── SRT_DRIVE_MANUAL.md
└── esp32_controller/       # ESP32-S3 MicroPython
    ├── boot.py             # WiFi startup
    ├── main.py             # Main application
    ├── config.py           # Settings
    ├── wifi_manager.py     # WiFi management
    ├── web_server.py       # HTTP server & UI
    ├── srt_serial.py       # Due serial protocol
    ├── coordinates.py      # RA/Dec <-> Alt/Az
    └── stellarium.py       # Stellarium protocol
```

---

## Documentation

- [SRT Drive Manual](docs/SRT_DRIVE_MANUAL.md) - Complete operations manual

## License

MIT License - Acre Road Observatory, University of Glasgow
