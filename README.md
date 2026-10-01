# SRT Motor Driver

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
|  - Ethernet/WiFi  |      |  - Ettus B200 SDR  |
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
|   3.0m Dish       |
|   1420 MHz Feed   |
|                   |
+-------------------+
```

### Data Flow

1. **Scheduling**: User creates observation schedule via web UI (target, time, duration, integration time); the receiver's tuning is fixed, not set per observation
2. **Telescope Control**: At scheduled time, scheduler sends HTTP request to ESP32 to point telescope
3. **Coordinate Conversion**: ESP32 converts RA/Dec or Galactic coordinates to Alt/Az
4. **Motor Control**: ESP32 sends target to Arduino Due, which drives motors to position
5. **Data Acquisition**: Scheduler launches GNU Radio receiver to capture 21cm spectrum data
6. **Storage**: Integrated spectra (or, for the pulsar, millisecond band power) saved to HDF5 files with timestamps and metadata

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
- mDNS hostname `srt-controller.local`
- Wired controller web UI at `http://192.168.50.120/`, on a private point-to-point
  Ethernet link to the observatory computer (not the observatory LAN)
- Ethernet OTA firmware updates after the first serial flash

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
cd 21-cm-radio-telescope-v2

# Build and upload
pio run -e due -t upload

# Monitor serial output
pio device monitor -b 115200
```

### 2. WT32-ETH01 Controller

```bash
cd 21-cm-radio-telescope-v2/esp32_controller_arduino

# First flash or recovery with temporary FT232 programmer
pio run -e wt32-eth01 --target upload

# Routine Ethernet OTA update after the first serial flash
pio run -e wt32-eth01-ota --target upload
```

The ESP32 controller uses Arduino/PlatformIO.
The OTA target in `esp32_controller_arduino/platformio.ini` is set to the current controller address, `192.168.50.120`.

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

#### Option 2: Wired Controller, Hostname, or Your Network
1. From the observatory computer, browse directly to: `http://192.168.50.120/`
   (reachable over the private link only — see below for remote access)
2. If mDNS is supported, `http://srt-controller.local/` should also resolve
3. If the controller joins a different network, connect to the AP first
4. Go to **WiFi** tab, click **Scan**
5. Select your network and enter password
6. Browse to the hostname or the new IP

Credentials are saved and the ESP32 auto-reconnects on boot. The AP stays active as fallback.

#### Option 4: Remotely, over an ssh tunnel

The controller is on a private link and the scheduler binds loopback, so neither
is reachable from the network. Reach them *through* the observatory host:

```bash
ssh -L 8080:192.168.50.120:80 -L 5000:127.0.0.1:5000 astro@ettus3.astro.gla.ac.uk
```

Then `http://localhost:8080` for the controller and `http://localhost:5000` for
the scheduler. Forward port 5000 even if you only want the controller: its page
fetches the scheduler at `127.0.0.1:5000` for the Sun Scan, Calibration Day and
firmware-update buttons, and through a tunnel that resolves to your own machine.

For applications rather than web pages — Stellarium, VS Code, the receiver GUI —
use `waypipe ssh astro@ettus3.astro.gla.ac.uk <command>`. Full details, including
the WSL2 client requirements, are in
[Observatory Host Setup](docs/OBSERVATORY_HOST_SETUP.md#10-remote-access).

#### Option 3: Direct WiFi Setup for a New Network
1. Connect to the AP first
2. Go to **WiFi** tab, click **Scan**
3. Select your network and enter password
4. Browse to `http://srt-controller.local/` if mDNS is supported, or note the new IP address
5. Connect your computer to same network
6. Browse to the hostname or the new IP

### Web Interface

#### Control Tab

| Section | Controls |
|---------|----------|
| **Current Status** | Shows Alt, Az, RA/Dec, Galactic coordinates, motor currents, fault state, and tracking target |
| **Quick Targets** | Track Sun or Moon |
| **Actions** | Stop all, pause slewing for 10 seconds, home, homing, reset active faults, Cal On/Off (drives nothing: the noise diode is gone, issue #39) |
| **Axis Mode** | Track both axes, azimuth only at fixed altitude, or altitude only at fixed azimuth |
| **Coordinates** | Direct Alt/Az, RA/Dec (J2000), and Galactic Go To / Track commands |

**Coordinate Systems:**
- **RA/Dec**: Right Ascension (0-24 hours), Declination (-90 to +90 degrees), **J2000 epoch**
- **Galactic**: Galactic longitude l (0-360°), latitude b (-90 to +90°), **J2000 epoch**
- **Alt/Az**: Altitude (0-90°), Azimuth (0-355°)

All equatorial (RA/Dec) coordinates use the **J2000 reference frame**, which is the standard epoch for modern star catalogs and planetarium software like Stellarium. The controller automatically handles precession when converting to Alt/Az for telescope pointing.

**Tracking Modes:**
- **Go To**: Slew to position once (no tracking)
- **Track**: Continuously update position as Earth rotates
- **Axis-only Track**: Follow target azimuth or altitude while holding the other axis fixed
- **Sun/Moon**: Automatically updates coordinates as they move across the sky
- **Galactic Plane**: Tracks the point on the plane (b = 0) nearest the galactic centre that is currently at or above the acquisition altitude (45° by default), then follows it down until the 10° observing horizon parks the dish. The centre itself is never an option from Glasgow — at Dec −28.9° it culminates at 5.2°, below the trees.

#### Network Tab

- **Ethernet**: Hostname, connection status, IP address, MAC address, DHCP/static configuration
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
   - Host: `192.168.50.120` (or AP/network IP)
   - Port: `10001`
4. Click **Connect**
5. Select any object and press `Ctrl+1` to slew

### Serial Commands (Arduino Due)

Connect via USB at 115200 baud.

#### Motion
| Command | Description |
|---------|-------------|
| `45 180` or `DRIVE 45 180` | Slew to Alt=45°, Az=180° (rounded to the 0.5° pulse grid) |
| `HOME` | Run homing sequence (also run at power-on) |
| `STOP` | Emergency stop. During a homing it aborts the homing into `FAULT_HOMING_ABORTED`: the position is unknown until `RESET` then `HOME` |
| `RESET` | Clear fault; `HOME` afterwards to re-home |

During a homing the Due answers `STATUS` with the live line and refuses every
other command except `STOP` with `Homing: busy - ignored`.

#### Status
| Command | Description |
|---------|-------------|
| `STATUS` | Show current position |
| `CONFIG` | Show configuration |
| `HELP` | List all commands |

#### Calibrator
| Command | Description |
|---------|-------------|
| `CAL ON` / `CAL OFF` / `CAL` | Set or toggle the calibrator pin and the `Cal:` field. Nothing is driven: the noise diode is gone (issue #39) |

#### Configuration
| Command | Description |
|---------|-------------|
| `SET ALTMIN 0` | Minimum altitude (degrees) |
| `SET ALTMAX 90` | Maximum altitude (degrees) |
| `SET CURRENT 4.5` | Current limit (Amps) |
| `SET RAMPUP 1000` | Acceleration time (ms) |
| `SAVE` | Save to flash |
| `LOAD` | Load from flash |
| `DEFAULTS` | Reset to defaults |

`HELP` lists the rest of the `SET` parameters (hardware limits, home position,
ramps, stall timeout, encoder debounce per axis, azimuth backlash).

#### Replies to the controller

Commands arriving on Serial1 (from the ESP32) are answered there. A drive
target gets `ACK DRIVE <alt> <az>` (the target as rounded to the pulse grid) or
`ERR DRIVE fault|homing|limits`; an unparseable line gets `ERR UNKNOWN <line>`.
The ESP32 re-sends a target left unanswered for 2 s once and reports the counts
as `drive_ack` in `/status`.

#### Status Output Format

The ESP32 parses this positionally; currents are printed to one decimal place.
```
Alt:45.0 Az:180.0 Ialt:0.2A Iaz:0.2A Status:Ready Cal:OFF
Alt:45.0 Az:180.0 Ialt:1.0A Iaz:1.1A Status:Slewing -> Alt:60.0 Az:200.0 Cal:OFF
Alt:45.0 Az:180.0 Ialt:0.0A Iaz:0.0A Status:FAULT [Azimuth motor stalled] Cal:OFF
```

Encoder pulses are counted on the reed switches' rising edge.

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

The `receiver_scheduler/` folder contains the data acquisition system for 21 cm hydrogen line observations: the receiver itself, a Flask scheduler with a ten-tab operator page, pointing calibration against the Sun, the beam measured from a Sun drift, a radiometric measurement of the obstructed horizon, the bandpass and gain calibration that turns counts into kelvin, the clocks (Thunderbolt 10 MHz/PPS, NTP), and pulsar mode for PSR B0329+54 with its fold and timing. See [its README](receiver_scheduler/README.md) for the tabs and the files.

Everything in the observing path is **headless** — the observatory is worked over ssh — with one deliberate exception, the console-only receiver GUI button.

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

- **SDR Support:** Ettus B200/B210, RTL-SDR, or demo mode (simulated data)
- **Processing:** Real-time FFT with configurable integration time
- **Display:** Live spectrum plot and waterfall display (PyQtGraph)
- **Output:** HDF5 files with integrated spectra, timestamps, and metadata

```bash
# Run standalone receiver
cd receiver_scheduler
python b210_h1_receiver.py --sdr b210 --gain 30

# Or use demo mode without hardware
python b210_h1_receiver.py --sdr demo
```

#### Observation Scheduler (`h1_web_scheduler.py`)

Tabbed web interface that coordinates telescope pointing and data recording:

- **Scheduler Tab:** Add/edit/clone/delete observations with clash prevention, late-start recovery, preemption, and audio notifications
- **Sun Scan Tab:** Pointing calibration via raster scan of the sun (see below)
- **Horizon, RF calibration, Camera, Simulator, Observe, Guide Tabs:** the horizon archive, bandpass/gain and the Clocks card, the safety camera, the sky simulator, running and plotting observations (including pulsar folds and the PRESTO fold)
- **Configuration Tab:** Persistent settings (controller URL, observer location, the fixed instrument, data folder, receiver Python path, the Sun and pulsar monitors, sound)
- **Log Tab:** Live view of rotating scheduler log
- **Start Receiver:** Starts the B200 receiver manually for warm-up/testing and reports whether the receiver is idle, manually started, or owned by a scheduled observation
- **Coordinate Systems:** Alt/Az, RA/Dec (J2000), Galactic, famous targets, Drift Scan (fixed pointing computed from a source and beam-crossing time), Solar System objects (Sun/Moon), Satellite (TLE), PSR B0329+54 (`pulsar`), Calibration day (`calibration`) and Horizon scan (`horizon`)
- **Standing orders:** the Sun monitor tracks the Sun whenever the telescope is idle, and the pulsar monitor records B0329+54 daily; both off by default, both give way to bookings and to anything started by hand
- **Satellite Tracking:** Fetch TLEs from CelesTrak, compute next pass, track via 1 Hz position updates
- **End Actions:** Stay, Go Home, or Stow telescope after observation
- **Firmware Update:** Requests the local scheduler service to build and upload WT32 firmware over Ethernet OTA

```bash
# Start the scheduler
cd receiver_scheduler
python h1_web_scheduler.py --port 5000    # binds 127.0.0.1 by default

# Open browser to http://localhost:5000
```

On the observatory Linux host, use the desktop launcher **Start SRT Sofware**. It starts the scheduler, detached, and opens `http://192.168.50.120/` in Firefox. VS Code and Stellarium were taken out of it on 2026-10-01; both are started by hand when wanted, and opening the VS Code workspace still runs its scheduler and PlatformIO Serial Monitor tasks. `start_scheduler.sh` reuses an already-serving scheduler rather than starting a second one that would die on the bound port. The launcher runs:

```bash
/home/astro/21-cm-radio-telescope-v2/receiver_scheduler/start_scheduler.sh
```

which starts `h1_web_scheduler.py --host 127.0.0.1 --port 5000` only when nothing is already serving on port 5000. There is deliberately no systemd unit: the scheduler starts from the launcher and should not come up unattended.

The **loopback bind is deliberate**. The scheduler has no authentication, and its
endpoints start observations, take the SDR, rewrite the schedule and flash
controller firmware over OTA — bound to `0.0.0.0` that is an unauthenticated
telescope-control API offered to every network the host is attached to. Nothing
needs the wildcard: the controller's own page fetches the scheduler at
`127.0.0.1:5000` from a browser running on this host, and remote use is over
waypipe, which forwards the display rather than the connection.

The scheduler re-execs itself under the configured receiver Python when needed so GNU Radio, PyEphem, and SDR dependencies come from radioconda. On the observatory machine the receiver Python default is `/home/astro/radioconda/bin/python`.

### Scheduler Configuration

Settings are managed via the Configuration tab in the web interface and persisted in `scheduler_config.json`. Key settings include:

- **Controller URL** — ESP32 address, currently `http://192.168.50.120` (empty to disable telescope control)
- **Controller Fallback URLs** — Additional controller addresses such as `http://srt-controller.local` and `http://192.168.4.1`
- **Observer Location** — Latitude, longitude, elevation (used for satellite pass prediction)
- **Min Elevation** — Minimum elevation for satellite passes (default 10°)
- **Instrument** — The fixed tuning and the pilot, editable here only, with a warning that a change leaves later recordings uncalibrated
- **Data Output Folder** — Where HDF5 files are saved
- **Receiver Python Path** — Path to the radioconda Python executable used by the scheduler and receiver
- **Sun monitor / Pulsar monitor** — `sun_monitor`; `pulsar_monitor`, `pulsar_monitor_window` (`follow` or `clock`), `pulsar_monitor_start`, `pulsar_monitor_hours`
- **Pulsar band** — `pulsar_sample_rate_hz` (32 Msps) and `pulsar_lo_hz` (1413 MHz), in `scheduler_config.json` only
- **Firmware Update Environment** — PlatformIO environment used for WT32 Ethernet OTA uploads

### Observation Workflow

1. **Open Scheduler:** Browse to `http://localhost:5000`
2. **Add Observation:** Click "+ Add Observation"
   - Select coordinate system and enter target (or fetch satellite TLE from CelesTrak)
   - Set start date/time and duration (end time calculated automatically)
   - Set integration time, SDR type, homing first, the horizon check, and end action (stay/home/stow); the instrument is shown, not set
3. **Save Schedule:** Auto-saved with clash prevention; running items are locked
4. **Automatic Execution:** At scheduled time:
   - Homes the mount first if the entry asks
   - Scheduler sends pointing command to ESP32
   - Waits for slew to complete (polls `is_slewing` status)
   - Starts satellite tracking thread if applicable
   - Launches GNU Radio receiver
   - Data saved to HDF5 with observation metadata: `spectra_kelvin` when the bandpass template and gain in force apply, `spectra_linear` (raw counts) when they do not
   - On completion: telescope home/stow if configured
5. **Monitor:** Status bar shows running observation with countdown, or time to next observation when idle

### Sun Scan — Pointing Calibration (`sun_scan.py`)

Determines telescope pointing errors by performing an n×n raster scan centred on the sun. The antenna beam (4.30° FWHM-equivalent, from the main-lobe solid angle of 21.0 sq deg in `beam_calibration.json`) is sampled at each grid point by measuring broadband power, then a 2-D Gaussian is fitted to locate the true peak. The difference between the fitted peak and the assumed sun position gives the pointing correction.

- **Integrated via the Sun Scan tab** in the web scheduler — uses the same observer location, SRT controller URL, and SDR settings as the scheduler
- **Grid:** configurable n×n (default 5×5) with adjustable spacing (default 1.5°, about a third of the beam)
- **Scan pattern:** all rows scan east-to-west with backlash overshoot at each row start
- **Azimuth correction:** grid offsets are treated as cross-elevation sky offsets and mount azimuth commands are expanded by cos(altitude); scans stop with a clear error if any requested grid point would be clipped by mount limits
- **Moving Sun:** Sun position is recomputed before each measurement slew, checked again after hardware motion, and refined when necessary; the saved comparison point is the mid-scan ephemeris
- **Verified movement:** before scanning, the scheduler resolves the reachable controller from the primary/fallback URLs. Each command must reach its requested position within tolerance; rejected commands, telescope faults, a stationary mount, wrong final coordinates, and timeouts stop immediately with a detailed website error. The point counter advances only after a completed measurement.
- **Physical reference and retry:** Calibration Day runs the Due `HOME` sequence before each hardware raster so displayed coordinates are re-established at the physical limits. It automatically re-homes and retries one rejected raster before counting a scan failure. A continuing mismatch between displayed and physical position is a motor, encoder/coupling, or power problem and must not be treated as valid calibration data.
- **Stable B200 acquisition:** one B200 session is held across the complete raster, explicitly on `RX2`, with a short discarded warm-up capture. The standalone receiver cannot start while calibration owns the SDR.
- **Fit quality:** Gaussian peaks outside the measured raster, implausible beam widths, non-finite uncertainty, and poor fits are rejected and displayed as website errors rather than entering the day model
- **Output:** pointing error (ΔAlt, mount ΔAz, sky ΔAz), fitted beam FWHM, scan start/end timestamps, and a two-panel image (measured data + Gaussian fit)
- **SDR backends:** B200/B210, RTL-SDR, or demo mode (simulated Gaussian beam)
- **Standalone usage:**

  ```bash
  python sun_scan.py --n 5 --spacing 1.5 --integration 3.0 --sdr demo
  ```

#### Calibration Day — Pointing Model

Running repeated sun scans over a day fits the mount's pointing model. The base model has four terms:

- **IE, IA** — elevation and azimuth index (constant zero-point offsets)
- **AN** — north-south tilt of the azimuth axis
- **AE** — east-west tilt of the azimuth axis

and two more when the scans can constrain them: **CA** (collimation, the beam not perpendicular to the elevation axis; needs a spread of altitude) and **AZSCALE** (an azimuth scale error; needs 90° of azimuth). Any term can be held at a stored value: after a feed change only IE and CA are refitted.

**Apply to Telescope** POSTs the fitted terms as a document to the controller's `/pointing/apply`. The controller keeps it in its own NVS namespace and applies it in `pointing.cpp` (`trueToDrive`, which also takes NPAE and TF) to every goto, track and manual move, and inverts it for every reported position. The observer position stays the true site; the model is never folded into an effective latitude/longitude, and the operator's offset boxes are not touched.

Calibration Day performs one complete N×N Sun scan per start-to-start interval and saves each successful scan before waiting for the next. The fit needs at least four successful scans; applying it needs 30 degrees of Sun azimuth coverage, a condition number under 10⁴ and significant tilt terms. It weights scans by their fitted uncertainties, leaves out scans taken with the Sun behind the measured horizon, and reports parameter uncertainty and residual RMS. Hardware, scan, fit, and apply errors appear in the receiver website; three consecutive scan failures stop a calibration day.

To run a calibration day:
- **From the scheduler:** add a "Calibration Day (Sun Scan)" observation with a start time before sunrise and a long duration (e.g. 12 hours). The scheduler waits for sunrise, runs scans at the configured interval, and stops at sunset.
- **From the Sun Scan tab:** use the Calibration Day controls to start/stop manually, fit the model, and apply corrections.
- **Recommended:** 6–8 scans spread over several hours for good azimuth coverage. Spring–autumn gives best results.

### Output Data Format

Every recording goes in `receiver_scheduler/data/observations/`, named for when it was made and what the mount was doing — and nothing else, because everything else is inside the file:

```
20260825_192937_drift.h5     the dish was parked; the sky drifted through the beam
20260825_201455_track.h5     the mount followed the source
20260825_143012_manual.h5    started at the console; nobody commanded the mount
20260926_203116_pulsar.h5    a PSR B0329+54 entry: millisecond band power, not spectra
```

`track` and `drift` describe the mount rather than the box the entry was typed into: an alt/az observation is a **drift** scan, because the scheduler parks the dish and leaves tracking off. Calibration days and horizon scans write their own products (`pointing_data.json`, `horizon_profiles/`), not recordings.

A pulsar recording holds `power` (N × 1 float32: by default one 32 MHz channel at 1413 MHz, summed per millisecond), `frequency_hz`, `time_marks` and `overflow_marks`, with the time source (`time_source` pps/host) and the calibration in force (`cal_*`) as attributes; see [the receiver README](receiver_scheduler/README.md#pulsar-recordings) and `docs/PULSAR_PROCESSING.tex`.

Since issue #27 the B200 records with a **fixed instrument** (LO 1418.905752 MHz,
8 Msps, gain 20 dB, set in `tuning.py`) and every file carries **two products** —
an H I sub-band and a whole-band continuum product — readable while it is still
being written (HDF5 SWMR):

```
20260826_184209_drift.h5
├── frequency_hz            # H I sub-band axis (Hz), 845 channels
├── spectra_kelvin          # H I product, antenna temperature (K)  -- when calibrated
│   or spectra_linear       #             raw power (counts)         -- when not
├── frequency_hz_wide       # Continuum product axis, 1024 ch over 8 MHz
├── spectra_wide_kelvin     # Continuum product (K or counts)
│   or spectra_wide_linear
├── bandpass_correction(_wide), bandpass_valid(_wide)  # per-channel correction applied
├── timestamps              # Unix time at the centre of each record
├── integration_times       # Actual integration per record
├── overflows               # UHD overflows during the record
└── attrs:
    ├── spectra_units, spectra_wide_units            # "K" or "counts", per product
    ├── instrument, h1_band_hz, continuum_band_hz    # the fixed instrument and its bands
    ├── applied_gain_counts_per_k, applied_t_sys_k   # to reverse the calibration
    ├── bandpass_template(_wide), gain_calibration   # the full calibration, as JSON
    ├── beam_fwhm_deg, effective_area_m2, site_*     # measured beam and surveyed site
    ├── sdr_type, center_freq_hz, sample_rate_hz, gain_db, created
    ├── obs_name, comment, observation_mode          # "track", "drift" or "manual"
    ├── coord_system        # altaz, radec, galactic, object, drift, or satellite
    ├── drift_crossing_time, drift_crossing_offset_deg   # drift scans: parked-beam crossing
    ├── homed_first, homing_count_error_*_deg        # if homed first, the count error
    ├── clock_source, clock_ref_locked, reference_*  # the 10 MHz in use; the Thunderbolt's state
    ├── pointing_terms                               # the pointing model in force
    └── ...                 # target coordinates, TLE, schedule times
```

**The dataset name is the units.** If the bandpass template and gain in force applied to the tuning, the spectra were converted at write time and live under `spectra_kelvin`; otherwise they are raw counts under `spectra_linear`. Asking for the wrong name raises `KeyError` instead of quietly handing back the other scale. Nothing is lost by calibrating on write — the correction, the gain and the system temperature all travel in the file, so the raw counts are one line away:

```python
raw = (spectra + attrs['applied_t_sys_k']) * attrs['applied_gain_counts_per_k'] * bandpass_correction
```

If the receiver frequency, sample rate, or FFT size changes during a run, the receiver closes the current file and opens a new one so every file stays internally consistent. The new file is named like any other; the reason for the split is recorded in its `segment` and `segment_reason` attributes.

See `notebooks/read_h1_data.ipynb` for a complete analysis example, and
`notebooks/solar_flux_scallop.ipynb` for the whole reduction of a solar track.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Can't connect to controller web UI | Try `http://192.168.50.120/`, then `http://srt-controller.local/`, then AP mode at `192.168.4.1` |
| Can't connect to scheduler web UI | Start `h1_web_scheduler.py` and open `http://localhost:5000` on that host |
| Motors don't move | Check Due serial for FAULT status, verify homing completed |
| Position incorrect | Run `HOME` command, check limit switches |
| Stellarium won't connect | Verify IP and port 10001, check ESP32 is running |
| Coordinates don't match sky | Check observer lat/lon, verify NTP time sync |

---

## File Structure

```
21-cm-radio-telescope-v2/
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
│   ├── test/                   # Host unit tests: coordinates, pointing (pio test -e native)
│   └── src/
│       ├── main.cpp            # Main application, tracking loop
│       ├── config.h            # Default settings
│       ├── settings.cpp/h      # Runtime settings with NVS persistence
│       ├── wifi_manager.cpp/h  # WiFi AP/STA management
│       ├── web_server.cpp/h    # HTTP server & web UI
│       ├── srt_serial.cpp/h    # Serial protocol to Due
│       ├── coordinates.cpp/h   # RA/Dec/Galactic <-> Alt/Az
│       ├── pointing.cpp/h      # True <-> drive frame: refraction and the pointing model
│       ├── diag.cpp/h          # Reset-surviving diagnostics record, loop watchdog, /diag
│       ├── sync.cpp/h          # Cross-task locking between loopTask and async_tcp
│       ├── stellarium.cpp/h    # Stellarium telescope protocol
│       ├── state.h             # Global state structure
│       └── index_html.h        # Embedded web interface
│
├── receiver_scheduler/     # Observation scheduling & data acquisition
│   ├── h1_web_scheduler.py # Flask scheduler: routes, schedule, observing state
│   ├── web/                # The operator page as static files (index.html,
│   │                       #   app.css, js/ - fifteen scripts, one per tab plus five shared)
│   ├── b210_h1_receiver.py # GNU Radio receiver (B200/RTL-SDR); spectra or pulsar mode
│   ├── sun_scan.py         # Sun raster, pointing model, calibration day
│   ├── beam_scan.py        # The beam's solid angle from a two-hour Sun drift
│   ├── horizon_scan.py     # Radiometric horizon measurement
│   ├── rf_calibration.py   # Counts to kelvin: gain, T_sys, velocity shift
│   ├── bandpass.py         # The measured instrument response
│   ├── pilot.py            # The B200's TX as gain/passband reference (off by default)
│   ├── tuning.py           # The fixed instrument: LO, rate, gain, the two bands
│   ├── drift_park.py       # Park a drift scan on the drive grid
│   ├── drift_fit.py        # Total-power fit of a drift scan vs the model curve
│   ├── scallop.py          # Tracking scallop removed from a tracked compact source
│   ├── observation_plot.py # Finished observations, in kelvin and LSR velocity
│   ├── observatory.py      # Site and beam - plumbing; numbers in instrument.py
│   ├── observation_files.py # Where a recording goes and what it is called
│   ├── solar_reference.py  # RSTN/F10.7 reference fluxes from NOAA SWPC
│   ├── pulsar_fold.py      # B0329+54: fold at the absolute phase, S/N, flux, PRESTO
│   ├── pulsar_toa.py       # TOAs against the EPN template; the .tim files
│   ├── pint_tools.py       # PINT residuals and fits (run in the pint env)
│   ├── sigproc_export.py   # Pulsar recording -> SIGPROC .fil for PRESTO
│   ├── clocks.py           # Thunderbolt, host NTP and controller clock; stability
│   ├── thunderbolt.py      # The Thunderbolt's TSIP status monitor
│   ├── plot_backend.py     # Agg unless in a Jupyter kernel
│   ├── data/observations/  # Every recording: <date>_<time>_<track|drift|pulsar|manual>.h5
│   ├── horizon_profiles/   # Every horizon scan, by date, one chosen
│   ├── pulsar_timing/      # b0329.tim, b0329_segments.tim, stored profiles
│   └── README.md           # Receiver/scheduler documentation
│
├── notebooks/              # Worked data-reduction examples, run from anywhere
│   ├── read_h1_data.ipynb  # Open a recording, see what is in it, plot it
│   └── solar_flux_scallop.ipynb # A solar track: band power -> SFU, scallop removed
│
├── astro_simulator/        # Sky simulator (HI4PI + continuum)
│   ├── instrument.py       # Surveyed site, measured beam - the one copy
│   ├── horizon_store.py    # The dated horizon archive, and the rule for using it
│   └── web/                # Browser build, served by the scheduler at /simulator/
│
└── docs/
    ├── SRT_DRIVE_MANUAL.md         # Arduino Due firmware manual
    ├── ESP32_CONTROLLER.md         # ESP32 controller manual
    ├── WT32_ETH01_MIGRATION.md     # WT32-ETH01 setup guide
    ├── OBSERVATORY_HOST_SETUP.md   # building the host and its private controller link
    ├── HOST_REBUILD.md             # rebuilding the observatory computer; host/ snapshots
    ├── CALIBRATION.md              # how the telescope is calibrated
    └── PULSAR_PROCESSING.tex       # pulsar mode: recording, fold, TOAs, timing
```

---

## Documentation

- [SRT Drive Manual](docs/SRT_DRIVE_MANUAL.md) - Arduino Due firmware reference
- [ESP32 Controller Manual](docs/ESP32_CONTROLLER.md) - WT32-ETH01 controller and API reference
- [WT32-ETH01 Setup Guide](docs/WT32_ETH01_MIGRATION.md) - Hardware setup and wiring
- [Observatory Host Setup](docs/OBSERVATORY_HOST_SETUP.md) - Building a new observatory computer: second Ethernet card, private link to the controller, firewall and scheduler
- [Host Rebuild](docs/HOST_REBUILD.md) - Everything needed to replace the observatory computer, including the files git does not hold; environment snapshots in `docs/host/`
- [Calibration](docs/CALIBRATION.md) - What the bandpass template, gain fit, pilot and beam each measure, the order they are applied in, and what is still uncalibrated
- [Pulsar Processing](docs/PULSAR_PROCESSING.tex) - Pulsar mode for B0329+54: recording, fold, TOAs and timing
- [Receiver & Scheduler](receiver_scheduler/README.md) - H1 receiver and observation scheduler

## License

MIT License - Acre Road Observatory, University of Glasgow
