# ESP32-S3 Controller - Technical Manual

**Version 1.0**
**Acre Road Observatory, Glasgow**

---

## Table of Contents

1. [Overview](#1-overview)
2. [Installation](#2-installation)
3. [Configuration Reference](#3-configuration-reference)
4. [Module Reference](#4-module-reference)
5. [HTTP API](#5-http-api)
6. [Stellarium Protocol](#6-stellarium-protocol)
7. [Coordinate System](#7-coordinate-system)
8. [Time Synchronization](#8-time-synchronization)
9. [Networking](#9-networking)
10. [Development](#10-development)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Overview

The ESP32-S3 controller provides the high-level interface for the SRT drive system:

- **Web interface** for manual control and monitoring
- **Stellarium integration** via TCP telescope protocol
- **Coordinate transforms** from RA/Dec (J2000) to Alt/Az
- **Ephemeris calculations** for Sun and Moon positions
- **Time synchronization** via NTP or browser fallback
- **Networking** via WiFi (AP + station) and optional Ethernet

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
                         |    ESP32-S3      |
                         |                  |
                         |  main.py         |
                         |  coordinates.py  |
                         |  web_server.py   |
                         |  stellarium.py   |
                         +--------+---------+
                                  |
                             UART Serial
                                  |
                         +--------+---------+
                         |   Arduino Due    |
                         +------------------+
```

### File Structure

| File | Purpose | Size |
|------|---------|------|
| `boot.py` | WiFi/Ethernet initialization on startup | ~0.5 KB |
| `main.py` | Main application, tracking loop, time sync | ~4.5 KB |
| `config.py` | All configuration settings | ~1.2 KB |
| `coordinates.py` | Astronomical coordinate transforms | ~17 KB |
| `web_server.py` | HTTP server and web interface | ~34 KB |
| `stellarium.py` | Stellarium telescope protocol | ~3.5 KB |
| `srt_serial.py` | Serial communication with Due | ~5 KB |
| `wifi_manager.py` | WiFi AP/station management | ~4 KB |
| `ethernet.py` | W5500 Ethernet management | ~5 KB |
| `help.html` | Full documentation (served at /docs) | ~16 KB |

---

## 2. Installation

### Prerequisites

- ESP32-S3 development board
- MicroPython firmware (ESP32_GENERIC_S3)
- `mpremote` tool: `pip install mpremote`

### Flash MicroPython

```bash
# Download firmware from:
# https://micropython.org/download/ESP32_GENERIC_S3/

# Erase flash (hold BOOT button if needed)
esptool.py --chip esp32s3 --port COM5 erase_flash

# Flash firmware
esptool.py --chip esp32s3 --port COM5 write_flash -z 0 ESP32_GENERIC_S3-*.bin
```

### Upload Controller Code

```bash
cd esp32_controller

# Upload all Python files
mpremote connect COM5 cp *.py :

# Upload help page
mpremote connect COM5 cp help.html :

# Reset to run
mpremote connect COM5 reset
```

### Verify Installation

```bash
# Connect to REPL
mpremote connect COM5

# Check files
>>> import os
>>> os.listdir()
['boot.py', 'main.py', 'config.py', ...]
```

---

## 3. Configuration Reference

All settings are in `config.py`. Edit before uploading.

### WiFi Access Point

```python
WIFI_AP_SSID = "SRT_Controller"    # AP network name
WIFI_AP_PASSWORD = "radio1420"      # AP password (min 8 chars)
```

The AP is always active for configuration access at `192.168.4.1`.

### Serial Connection to Arduino Due

```python
DUE_UART_TX = 17        # ESP32 GPIO -> Due RX (Pin 19)
DUE_UART_RX = 18        # ESP32 GPIO <- Due TX (Pin 18)
DUE_BAUD_RATE = 115200  # Must match Due firmware
```

### W5500 Ethernet (Optional)

```python
ETH_ENABLED = True      # Set False to disable Ethernet
ETH_SPI_ID = 1          # SPI bus number
ETH_SCK = 12            # SPI clock
ETH_MOSI = 11           # SPI data out
ETH_MISO = 13           # SPI data in
ETH_CS = 10             # Chip select
ETH_RST = 9             # Reset (or None if tied to 3.3V)

# IP Configuration
ETH_USE_DHCP = True     # True for DHCP, False for static
ETH_STATIC_IP = "192.168.1.100"
ETH_STATIC_MASK = "255.255.255.0"
ETH_STATIC_GW = "192.168.1.1"
ETH_STATIC_DNS = "8.8.8.8"
```

### Observer Location

```python
OBSERVER_LAT = 55.9     # Latitude in degrees (north positive)
OBSERVER_LON = -4.3     # Longitude in degrees (east positive, west negative)
```

**Critical:** Set these to your observatory location for accurate coordinate transforms.

### Server Ports

```python
STELLARIUM_PORT = 10001  # Stellarium telescope protocol
WEB_PORT = 80            # HTTP web interface
NTP_SERVER = "pool.ntp.org"
```

---

## 4. Module Reference

### main.py

Main application entry point and tracking loop.

**Global State Variables:**

| Variable | Type | Description |
|----------|------|-------------|
| `current_ra` | float | Current target RA (hours, J2000) |
| `current_dec` | float | Current target Dec (degrees, J2000) |
| `target_alt` | float | Computed target altitude (degrees) |
| `target_az` | float | Computed target azimuth (degrees) |
| `tracking_enabled` | bool | Whether tracking is active |
| `target_name` | str | "Sun", "Moon", "Gal l=x b=y", or None |
| `time_synced` | bool | True if time has been set |
| `time_source` | str | "NTP", "browser", or None |

**Key Functions:**

- `sync_time_ntp()` - Sync time from NTP server
- `set_time_from_timestamp(unix_ts)` - Set time from browser
- `get_time_status()` - Return time sync status dict
- `tracking_loop(srt)` - Background thread: RA/Dec to Alt/Az conversion

**Tracking Loop Behavior:**

1. Runs every 1 second when `tracking_enabled` is True
2. For Sun/Moon targets, refreshes ephemeris every 30 seconds
3. Converts current RA/Dec to Alt/Az using current time
4. Sends position to Due if altitude > 0 (above horizon)

### coordinates.py

Astronomical coordinate transformations. All functions are pure (no side effects).

**Coordinate Functions:**

| Function | Input | Output | Description |
|----------|-------|--------|-------------|
| `ra_dec_to_alt_az(ra, dec, lat, lon)` | RA (h), Dec (deg) | Alt, Az (deg) | J2000 RA/Dec to horizon coords |
| `alt_az_to_ra_dec(alt, az, lat, lon)` | Alt, Az (deg) | RA (h), Dec (deg) | Horizon to J2000 RA/Dec |
| `galactic_to_equatorial(l, b)` | l, b (deg) | RA (h), Dec (deg) | Galactic to J2000 equatorial |
| `equatorial_to_galactic(ra, dec)` | RA (h), Dec (deg) | l, b (deg) | J2000 equatorial to galactic |
| `get_sun_position()` | (none) | RA (h), Dec (deg) | Current Sun position (J2000) |
| `get_moon_position()` | (none) | RA (h), Dec (deg) | Current Moon position (J2000) |

**Low-Level Functions:**

| Function | Description |
|----------|-------------|
| `julian_date(y, m, d, h, m, s)` | Calendar to Julian Date |
| `gmst(jd)` | Greenwich Mean Sidereal Time (hours) |
| `precess_j2000_to_date(ra, dec, jd)` | J2000 to equinox of date |
| `precess_date_to_j2000(ra, dec, jd)` | Equinox of date to J2000 |

### srt_serial.py

Serial communication with Arduino Due.

**SRTSerial Class:**

```python
srt = SRTSerial(tx_pin, rx_pin, baud_rate)

# Commands
srt.send_target(alt, az)   # Send position command
srt.send_home()            # HOME command
srt.send_stop()            # STOP command
srt.send_reset()           # RESET command (clear fault)

# Status
srt.read_status()          # Read and parse status line
srt.get_status_dict()      # Get parsed status as dict
srt.is_ready()             # True if status is "Ready"
srt.is_fault()             # True if status is "FAULT"
```

**Status Dict Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `alt` | float | Current altitude (degrees) |
| `az` | float | Current azimuth (degrees) |
| `target_alt` | float | Target altitude if slewing |
| `target_az` | float | Target azimuth if slewing |
| `alt_current_a` | float | Altitude motor current (Amps) |
| `az_current_a` | float | Azimuth motor current (Amps) |
| `status` | str | "Ready", "Slewing", "Homing", "FAULT" |
| `fault` | str | Fault description if in fault state |
| `is_slewing` | bool | True if currently moving |

### web_server.py

HTTP server and web interface. Contains embedded HTML/CSS/JavaScript.

**Key Functions:**

- `start_web_server(srt)` - Start HTTP server (blocking)
- `handle_http_request(client, srt)` - Route and handle requests
- `send_response(client, body, content_type, status)` - Send HTTP response

### wifi_manager.py

WiFi connection management with credential storage.

**WiFiManager Class:**

```python
from wifi_manager import wifi

wifi.startup()              # Initialize (AP + try saved network)
wifi.connect_sta(ssid, pw)  # Connect to network
wifi.scan_networks()        # Scan for networks
wifi.save_credentials(ssid, pw)  # Save credentials
wifi.clear_credentials()    # Forget saved network
wifi.get_status()           # Get connection status dict
```

Credentials are stored in `wifi_creds.json` on the ESP32 filesystem.

### ethernet.py

W5500 Ethernet management.

**EthernetManager Class:**

```python
from ethernet import ethernet

ethernet.init()             # Initialize W5500 hardware
ethernet.connect()          # Connect (DHCP or static)
ethernet.is_connected()     # Check connection status
ethernet.get_ip()           # Get IP address
ethernet.get_status()       # Get status dict
```

---

## 5. HTTP API

All endpoints return JSON unless noted.

### Status Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web interface (HTML) |
| `/docs` | GET | Full documentation (HTML) |
| `/status` | GET | Mount position and status |
| `/tracking` | GET | Current tracking state |
| `/ephemeris` | GET | Sun and Moon positions |
| `/time/status` | GET | Time sync status |

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
  "is_slewing": false
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
| `/track` | `enable=0/1` | Enable/disable tracking |
| `/direct` | `alt`, `az` | Go to Alt/Az directly |

**Example: Track a source at RA=12h, Dec=+30deg**

```
GET /track/radec?ra=12&dec=30
```

### Time Endpoints

| Endpoint | Parameters | Description |
|----------|------------|-------------|
| `/time/status` | (none) | Get time sync status |
| `/time/set` | `timestamp` | Set time from Unix timestamp |

### Network Endpoints

| Endpoint | Parameters | Description |
|----------|------------|-------------|
| `/eth/status` | (none) | Ethernet status |
| `/wifi/status` | (none) | WiFi status |
| `/wifi/scan` | (none) | Scan for networks |
| `/wifi/connect` | `ssid`, `password` | Connect to network |
| `/wifi/forget` | (none) | Forget saved network |

---

## 6. Stellarium Protocol

The controller implements the Stellarium telescope protocol on TCP port 10001.

### Protocol Format

**Goto Command (from Stellarium):**
- Length: 20 bytes
- Message type: 0
- RA/Dec as unsigned 32-bit integers

**Position Report (to Stellarium):**
- Length: 24 bytes
- Current position in same format

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

- Standard for modern star catalogs (Hipparcos, Tycho, etc.)
- Used by Stellarium, SIMBAD, and most planetarium software
- Matches ICRS to within milliarcseconds

### Coordinate Flow

```
Input (J2000)              Internal                    Output
+-------------+    +-------------------+    +------------------+
| RA/Dec      | -> | Precess to date   | -> | Hour Angle       |
| (J2000)     |    | Calculate HA      |    | Alt/Az           |
+-------------+    | Spherical trig    |    +------------------+
                   +-------------------+              |
                                                      v
                                              +------------------+
                                              | Arduino Due      |
                                              | (motor control)  |
                                              +------------------+
```

### Precession

The controller applies IAU 1976 precession to convert J2000 coordinates to the equinox of the current date before computing Alt/Az. This is necessary because:

1. The celestial pole moves ~50 arcsec/year due to precession
2. Hour angle calculation requires coordinates at current equinox
3. Error would be ~0.7 deg without precession correction (for 2025)

### Sun and Moon

Sun and Moon positions are calculated using simplified algorithms that produce **apparent** coordinates (equinox of date). These are automatically converted to J2000 before being returned by `get_sun_position()` and `get_moon_position()`.

**Accuracy:**
- Sun: ~1 arcmin
- Moon: ~5-10 arcmin (due to simplified orbital model)

### Pointing Accuracy

The coordinate transforms achieve <0.5 arcmin RMS error compared to astropy/ERFA for typical observing scenarios. Larger errors may occur:

- Near the zenith (azimuth becomes ill-defined)
- At very low altitudes (atmospheric refraction, not modeled)

---

## 8. Time Synchronization

Accurate UTC time is required for coordinate transforms. The ESP32 obtains time via:

### NTP (Primary)

On startup, the controller attempts NTP sync 3 times:

```python
ntptime.host = "pool.ntp.org"
ntptime.settime()
```

### Browser Fallback

If NTP fails (no internet), the web interface automatically sends browser time:

```javascript
// On page load
fetch('/time/set?timestamp=' + Math.floor(Date.now() / 1000))
```

### Time Status

Check time status via `/time/status`:

```json
{
  "synced": true,
  "source": "NTP",
  "utc": "2024-03-13 14:30:00",
  "timestamp": 1710339000
}
```

---

## 9. Networking

### Network Priority

1. **Ethernet** (if enabled and connected) - Best for Stellarium
2. **WiFi Station** (if connected to saved network)
3. **WiFi AP** (always active at 192.168.4.1)

All interfaces can be active simultaneously. The AP always remains available for configuration.

### Startup Sequence

1. Start WiFi Access Point
2. Initialize Ethernet (if enabled)
3. Connect Ethernet (DHCP or static)
4. Try saved WiFi network (if configured)
5. Continue - AP always available as fallback

### IP Addresses

| Interface | IP Address |
|-----------|------------|
| WiFi AP | 192.168.4.1 (fixed) |
| WiFi Station | DHCP assigned |
| Ethernet | DHCP or static (configurable) |

---

## 10. Development

### REPL Access

```bash
mpremote connect COM5
```

### Testing Coordinates

```python
>>> from coordinates import ra_dec_to_alt_az, get_sun_position
>>>
>>> # Get Sun position
>>> ra, dec = get_sun_position()
>>> print(f"Sun: RA={ra:.4f}h, Dec={dec:.2f}deg")
>>>
>>> # Convert to Alt/Az
>>> from config import OBSERVER_LAT, OBSERVER_LON
>>> alt, az = ra_dec_to_alt_az(ra, dec, OBSERVER_LAT, OBSERVER_LON)
>>> print(f"Alt={alt:.1f}, Az={az:.1f}")
```

### Memory Usage

```python
>>> import gc
>>> gc.collect()
>>> gc.mem_free()
>>> gc.mem_alloc()
```

Typical usage: ~60-80 KB code, leaving >2 MB free on 4 MB ESP32-S3.

### Debug Output

All modules print status messages to the serial console:

```
SRT Controller starting...
NTP time synced
W5500 initialized, MAC: de:ad:be:ef:00:01
Ethernet connected: 192.168.1.100
AP active: SRT_Controller
AP IP: 192.168.4.1
Web server listening on port 80
Stellarium server listening on port 10001
```

---

## 11. Troubleshooting

### Web Interface Issues

| Problem | Solution |
|---------|----------|
| Can't connect to 192.168.4.1 | Verify connected to SRT_Controller WiFi |
| Page loads but no data | Check Due serial connection, verify baud rate |
| Buttons don't respond | Check browser console for JavaScript errors |

### Coordinate Issues

| Problem | Solution |
|---------|----------|
| Position doesn't match sky | Verify OBSERVER_LAT/LON in config.py |
| Large errors (>1 deg) | Check time sync status on web interface |
| Sun/Moon wrong by ~20 arcmin | Normal - simplified ephemeris accuracy |

### Stellarium Issues

| Problem | Solution |
|---------|----------|
| Can't connect | Check IP and port 10001, verify ESP32 running |
| Connects but no slew | Click object then Ctrl+1 (not just click) |
| Wrong position shown | Verify time is synced |

### Ethernet Issues

| Problem | Solution |
|---------|----------|
| "WIZNET5K not available" | Rebuild MicroPython with W5500 support |
| "No chip detected" | Check SPI wiring, verify 3.3V power |
| "Connection timeout" | Check cable, try static IP |

### Serial Issues

| Problem | Solution |
|---------|----------|
| No status updates | Check TX/RX wiring (cross-connect) |
| Garbled data | Verify baud rate matches Due (115200) |
| "FAULT" status | Check Due USB serial for details |

### Memory Issues

| Problem | Solution |
|---------|----------|
| MemoryError on startup | Reduce HTML_PAGE size, use help.html |
| Crashes during operation | Check for memory leaks, add gc.collect() |

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

### Pin Assignments (Default)

| Function | ESP32-S3 GPIO |
|----------|---------------|
| Due TX | 17 |
| Due RX | 18 |
| ETH SCK | 12 |
| ETH MOSI | 11 |
| ETH MISO | 13 |
| ETH CS | 10 |
| ETH RST | 9 |

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
