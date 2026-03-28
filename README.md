# SRT Drive Controller

A complete control system for a Small Radio Telescope (SRT) for 21cm hydrogen line observations at Acre Road Observatory, Glasgow.

## System Overview

The system consists of four integrated components:

1. **Arduino Due** - Low-level motor control, position tracking, limit switches, current sensing
2. **WT32-ETH01** - Network interface (Ethernet + WiFi), web UI, Stellarium integration, RA/Dec to Alt/Az coordinate transforms
3. **H1 Receiver** - GNU Radio SDR application for 21cm hydrogen line data acquisition
4. **Observation Scheduler** - Web-based scheduler that coordinates telescope pointing and data recording

```
+------------------+     +----------------------+
|   Stellarium     |     |  Observation         |
|   (TCP:10001)    |     |  Scheduler           |
+--------+---------+     |  (HTTP:5000)         |
         |               |                      |
         |               |  - Schedule obs      |
         |               |  - Point telescope   |
         |               |  - Start/stop SDR    |
         |               +----------+-----------+
         |                          |
         |    HTTP API              | Subprocess
         |    (point telescope)     | (start receiver)
         |                          |
+--------+----------+      +--------+-----------+
|                   |      |                    |
|   WT32-ETH01      |      |   H1 Receiver      |
|    Controller     |      |   (GNU Radio)      |
|                   |      |                    |
|  - Ethernet/WiFi  |      |  - Ettus B210 SDR  |
|  - Web UI (:80)   |      |  - FFT processing  |
|  - Coord convert  |      |  - HDF5 output     |
|  - NTP time sync  |      |  - Live display    |
+--------+----------+      +--------------------+
         |
    UART (Serial)
         |
+--------+----------+
|                   |
|   Arduino Due     |
|                   |
|  - Motor PWM      |
|  - Encoders       |
|  - Limit switches |
|  - Current sense  |
+--------+----------+
         |
    Motors/Mount
         |
+--------+----------+
|                   |
|   3.7m Dish       |
|   1420 MHz Feed   |
|                   |
+-------------------+
```

### Data Flow

1. **Scheduling**: User creates observation schedule via web UI (target coordinates, time, duration, SDR settings)
2. **Telescope Control**: At scheduled time, scheduler sends HTTP request to ESP32 to point telescope
3. **Coordinate Conversion**: ESP32 converts RA/Dec or Galactic coordinates to Alt/Az
4. **Motor Control**: ESP32 sends target to Arduino Due, which drives motors to position
5. **Data Acquisition**: Scheduler launches GNU Radio receiver to capture 21cm spectrum data
6. **Storage**: Integrated spectra saved to HDF5 files with timestamps and metadata

## Hardware

### Arduino Due
- **Microcontroller:** Arduino Due (ARM Cortex-M3)
- **Motors:** DC motors with H-bridge drivers
- **Position sensing:** Reed switch encoders (0.5° resolution)
- **Current sensing:** ACS712 hall-effect sensors
- **Limit switches:** Altitude ~0°, Azimuth ~0° and ~355°

### WT32-ETH01

- ESP32 module with built-in LAN8720 Ethernet PHY
- Native 100 Mbps Ethernet (RJ45 connector)
- WiFi 802.11 b/g/n (simultaneous with Ethernet)
- Connects to Due via UART serial (IO4/IO14)

### Wiring: WT32-ETH01 to Arduino Due

| WT32-ETH01 | Arduino Due | Function |
|------------|-------------|----------|
| IO4        | Pin 19 (RX1)| ESP32 TX -> Due RX |
| IO14       | Pin 18 (TX1)| ESP32 RX <- Due TX |
| GND        | GND         | Common ground |
| 5V         | 5V          | Power |

**Note:** IO4/IO14 are used for Due communication. Avoid IO32/IO33 (labelled CFG/485_EN on RS-485 variants). See [WT32-ETH01 Migration Guide](docs/WT32_ETH01_MIGRATION.md) for details.

---

## Installation

### Prerequisites

- [PlatformIO](https://platformio.org/) (VS Code extension or CLI)

### 1. Arduino Due Firmware

```bash
cd new_SRT_drive

# Build and upload
pio run -e due -t upload

# Monitor serial output
pio device monitor -b 115200
```

### 2. WT32-ETH01 Controller

```bash
cd new_SRT_drive/esp32_controller_arduino

# Build and upload (use USB-TTL adapter)
pio run -e wt32-eth01 --target upload

# Monitor serial output
pio device monitor -b 115200
```

The ESP32 controller uses Arduino/PlatformIO (not MicroPython) for better performance and reliability.

---

## Configuration

### WT32-ETH01 Settings

Most settings can be changed at runtime via the web interface **Settings** tab:

- Observer location (latitude/longitude)
- Mount software limits (azimuth/altitude ranges)
- Home position
- Update tolerance (position deadband)
- WiFi AP name and password
- Ethernet IP (DHCP or static)

Settings are saved to ESP32 flash (NVS) and persist across reboots.

Compile-time defaults are in `esp32_controller_arduino/src/config.h`:

```cpp
// WiFi Access Point
#define WIFI_AP_SSID "SRT_Controller"
#define WIFI_AP_PASSWORD "radio1420"

// Serial pins to Arduino Due (WT32-ETH01)
#define DUE_UART_TX 4
#define DUE_UART_RX 14

// Observer location (for coordinate conversion)
#define OBSERVER_LAT 55.902426
#define OBSERVER_LON -4.307865
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
- **RA/Dec**: Right Ascension (0-24 hours), Declination (-90 to +90 degrees), **J2000 epoch**
- **Galactic**: Galactic longitude l (0-360°), latitude b (-90 to +90°), **J2000 epoch**
- **Alt/Az**: Altitude (0-90°), Azimuth (0-355°)

All equatorial (RA/Dec) coordinates use the **J2000 reference frame**, which is the standard epoch for modern star catalogs and planetarium software like Stellarium. The controller automatically handles precession when converting to Alt/Az for telescope pointing.

**Tracking Modes:**
- **Go To**: Slew to position once (no tracking)
- **Track**: Continuously update position as Earth rotates
- **Sun/Moon**: Automatically updates coordinates as they move across the sky

#### Network Tab

- **Ethernet**: Connection status, IP address, MAC address, DHCP/static configuration
- **WiFi Power**: Enable/disable WiFi to save ~100mA (only available when Ethernet connected)
- **WiFi**: Access Point status, station connection status
- **WiFi Config**: Scan and connect to WiFi networks, forget saved credentials

**Network Priority:** Ethernet provides a stable wired connection for Stellarium. WiFi can be disabled to save power when using Ethernet only.

**Power Consumption:** System draws ~400mA idle (300-320mA with WiFi disabled).

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

#### Calibrator
| Command | Description |
|---------|-------------|
| `CAL ON` | Turn calibrator noise source on |
| `CAL OFF` | Turn calibrator noise source off |
| `CAL` | Toggle calibrator state |

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
Alt:45.0 Az:180.0 Ialt:0.15A Iaz:0.20A Status:Ready Cal:OFF
Alt:45.0 Az:180.0 Ialt:0.25A Iaz:0.30A Status:Slewing -> Alt:60.0 Az:200.0 Cal:OFF
Alt:45.0 Az:180.0 Ialt:0.00A Iaz:0.00A Status:FAULT [Motor stalled] Cal:OFF
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

## H1 Receiver & Observation Scheduler

The `receiver_scheduler/` folder contains the data acquisition system for 21 cm hydrogen line observations.

### Receiver Prerequisites

The receiver requires **radioconda** (or a GNU Radio installation with UHD support):

```bash
# 1. Install radioconda
# Download from: https://github.com/ryanvolz/radioconda

# 2. Activate the environment
conda activate radioconda

# 3. Install additional Python dependencies
cd receiver_scheduler
pip install -r requirements.txt
```

This installs Flask (web scheduler), ephem (satellite tracking), and pytest (testing). GNU Radio, PyQt5, NumPy, and h5py are provided by radioconda.

### Receiver Components

#### H1 Receiver (`b210_h1_receiver.py`)

GNU Radio-based spectrum analyzer for 21 cm observations:

- **SDR Support:** Ettus B210, RTL-SDR, or demo mode (simulated data)
- **Processing:** Real-time FFT with configurable integration time
- **Display:** Live spectrum plot and waterfall display (PyQtGraph)
- **Output:** HDF5 files with integrated spectra, timestamps, and metadata

```bash
# Run standalone receiver
cd receiver_scheduler
python b210_h1_receiver.py --sdr b210 --gain 40

# Or use demo mode without hardware
python b210_h1_receiver.py --sdr demo
```

#### Observation Scheduler (`h1_web_scheduler.py`)

Tabbed web interface that coordinates telescope pointing and data recording:

- **Scheduler Tab:** Add/edit/clone/delete observations with clash prevention, late-start recovery, preemption, and audio notifications
- **Configuration Tab:** Persistent settings (controller URL, observer location, data folder, Python path, sound)
- **Log Tab:** Live view of rotating scheduler log
- **Coordinate Systems:** Alt/Az, RA/Dec (J2000), Galactic, Solar System objects (Sun/Moon), and Satellite (TLE)
- **Satellite Tracking:** Fetch TLEs from CelesTrak, compute next pass, track via 1 Hz position updates
- **Calibrator Control:** Per-observation noise source on/off, with `_cal` filename suffix
- **End Actions:** Stay, Go Home, or Stow telescope after observation

```bash
# Start the scheduler
cd receiver_scheduler
python h1_web_scheduler.py --host 0.0.0.0 --port 5000

# Open browser to http://localhost:5000
```

### Scheduler Configuration

Settings are managed via the Configuration tab in the web interface and persisted in `scheduler_config.json`. Key settings include:

- **Controller URL** — ESP32 address (empty to disable telescope control)
- **Observer Location** — Latitude, longitude, elevation (used for satellite pass prediction)
- **Min Elevation** — Minimum elevation for satellite passes (default 10°)
- **Data Output Folder** — Where HDF5 files are saved
- **Python Path** — Path to Python executable (e.g. radioconda)

### Observation Workflow

1. **Open Scheduler:** Browse to `http://localhost:5000`
2. **Add Observation:** Click "+ Add Observation"
   - Select coordinate system and enter target (or fetch satellite TLE from CelesTrak)
   - Set start date/time and duration (end time calculated automatically)
   - Configure SDR settings, calibrator, and end action (stay/home/stow)
3. **Save Schedule:** Auto-saved with clash prevention; running items are locked
4. **Automatic Execution:** At scheduled time:
   - Scheduler sends pointing command to ESP32
   - Waits for slew to complete (polls `is_slewing` status)
   - Sets calibrator state, starts satellite tracking thread if applicable
   - Launches GNU Radio receiver
   - Data saved to HDF5 in linear power with observation metadata
   - On completion: calibrator off, telescope home/stow if configured
5. **Monitor:** Status bar shows running observation with countdown, or time to next observation when idle

### Output Data Format

HDF5 files contain:

```
h1_sun_20260328_151303.h5
├── frequency_hz          # Frequency axis (Hz)
├── spectra_linear        # Integrated spectra (linear power), shape: [n_spectra, n_channels]
├── timestamps            # Unix timestamps for each spectrum
├── integration_times     # Actual integration time per spectrum
└── attrs:
    ├── sdr_type          # "b210", "rtlsdr", or "demo"
    ├── center_freq_hz    # Center frequency
    ├── sample_rate_hz    # Sample rate / bandwidth
    ├── fft_size          # Number of FFT channels
    ├── gain_db           # RF gain setting
    ├── created           # ISO timestamp
    ├── obs_name          # Observation name (from scheduler)
    ├── coord_system      # altaz, radec, galactic, object, or satellite
    ├── calibrator        # 1 = noise source on, 0 = off
    └── ...               # Target coordinates, TLE, schedule times
```

See `receiver_scheduler/read_h1_data.ipynb` for a complete analysis example.

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
├── platformio.ini          # PlatformIO build config (Arduino Due)
├── README.md               # This file
│
├── src/                    # Arduino Due firmware
│   └── main.cpp            # Motor control, encoders, limits
│
├── include/
│   └── config.h            # Due pin assignments, defaults
│
├── esp32_controller_arduino/   # WT32-ETH01 Arduino/PlatformIO
│   ├── platformio.ini          # ESP32 build config
│   └── src/
│       ├── main.cpp            # Main application, tracking loop
│       ├── config.h            # Default settings
│       ├── settings.cpp/h      # Runtime settings with NVS persistence
│       ├── wifi_manager.cpp/h  # WiFi AP/STA management
│       ├── web_server.cpp/h    # HTTP server & web UI
│       ├── srt_serial.cpp/h    # Serial protocol to Due
│       ├── coordinates.cpp/h   # RA/Dec/Galactic <-> Alt/Az
│       ├── stellarium.cpp/h    # Stellarium telescope protocol
│       ├── state.h             # Global state structure
│       └── index_html.h        # Embedded web interface
│
├── esp32_controller_micropython/  # Legacy MicroPython version (deprecated)
│
├── receiver_scheduler/     # Observation scheduling & data acquisition
│   ├── h1_web_scheduler.py # Flask web scheduler with ESP32 integration
│   ├── b210_h1_receiver.py # GNU Radio 21cm receiver (B210/RTL-SDR)
│   ├── read_h1_data.ipynb  # Jupyter notebook for reading/plotting HDF5 data
│   ├── scheduler_config.json # Persistent configuration (auto-generated)
│   ├── scheduler.log       # Rotating log file (auto-generated)
│   ├── h1_schedule.json    # Saved observation schedule
│   └── README.md           # Receiver/scheduler documentation
│
└── docs/
    ├── SRT_DRIVE_MANUAL.md         # Arduino Due firmware manual
    ├── ESP32_CONTROLLER.md         # ESP32 controller manual
    └── WT32_ETH01_MIGRATION.md     # WT32-ETH01 setup guide
```

---

## Documentation

- [SRT Drive Manual](docs/SRT_DRIVE_MANUAL.md) - Arduino Due firmware reference
- [ESP32 Controller Manual](docs/ESP32_CONTROLLER.md) - WT32-ETH01 controller and API reference
- [WT32-ETH01 Setup Guide](docs/WT32_ETH01_MIGRATION.md) - Hardware setup and wiring
- [Receiver & Scheduler](receiver_scheduler/README.md) - H1 receiver and observation scheduler

## License

MIT License - Acre Road Observatory, University of Glasgow
