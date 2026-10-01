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
- **Coordinate transforms** from RA/Dec (J2000) to true Alt/Az
- **Pointing model**: true Alt/Az to drive coordinates and back (`pointing.cpp`), stored in its own NVS namespace
- **Ephemeris calculations** for Sun and Moon (topocentric) positions
- **Time synchronization** via SNTP (hourly) or browser fallback
- **Runtime configurable settings** saved to flash (NVS)
- **Networking** via native Ethernet + WiFi (AP + station mode)
- **mDNS and Ethernet OTA** for `srt-controller.local` discovery and network firmware updates
- **Diagnostics** that survive a reset (`/diag`) and a 30 s loop watchdog
- **Ethernet address**: `http://192.168.50.120/` — a private point-to-point link
  to the observatory computer (`192.168.50.1`), which serves its DHCP lease and
  NATs it out for NTP. Not the observatory LAN.

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
                         |  pointing.cpp    |
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
| `src/pointing.cpp/h` | Pointing model, refraction, `trueToDrive()` / `driveToTrue()`, mount-limit helpers |
| `src/web_server.cpp/h` | HTTP server and web interface |
| `src/stellarium.cpp/h` | Stellarium telescope protocol |
| `src/srt_serial.cpp/h` | Serial communication with Due, drive-target acknowledgement, homing report |
| `src/wifi_manager.cpp/h` | WiFi AP/station management |
| `src/diag.cpp/h` | RTC-memory record of the last boot, reset reason, loop watchdog |
| `src/sync.cpp/h` | Recursive mutex (`SRTLock`) shared by `loopTask` and `async_tcp` |
| `src/state.h` | Global state structure, clock-state helpers |
| `src/index_html.h` | Embedded web interface HTML |
| `test/test_coordinates`, `test/test_pointing` | Host unit tests (`native` environment) |
| `test/shim/` | Minimal `Arduino.h` / `Preferences.h` so `pointing.cpp` builds on the host |

---

## 2. Installation

### Prerequisites

- WT32-ETH01 module (ESP32 with built-in LAN8720 Ethernet)
- Temporary FT232 USB-TTL programmer for first flash or recovery
- [PlatformIO](https://platformio.org/) (VS Code extension or CLI)

### Build and Upload

`pio` is not on PATH. On the observatory Linux host it is
`~/.platformio/penv/bin/pio`; on Windows `~/.platformio/penv/Scripts/pio.exe`.

```bash
cd 21-cm-radio-telescope-v2/esp32_controller_arduino
PIO=~/.platformio/penv/bin/pio

# Build for WT32-ETH01 (the default environment)
$PIO run -e wt32-eth01

# First serial flash or recovery (temporary FT232 on TX0/RX0)
$PIO run -e wt32-eth01 --target upload

# Routine Ethernet OTA update after first flash
$PIO run -e wt32-eth01-ota --target upload

# Host unit tests: test/test_coordinates and test/test_pointing, no board
$PIO test -e native

# Monitor serial output (needs the FT232 on TX0/RX0; the WT32 has no USB)
$PIO device monitor
```

`esp32s3` is a legacy environment for a board no longer used. `wt32-eth01`
is the canonical target and must always build clean.

### Verify Installation

After upload, the serial monitor should show (abridged):

```
SRT Controller starting...
Settings loaded from NVS
Due serial initialized
Initializing Ethernet...
  Mode: DHCP
ETH IP: 192.168.50.120, Speed: 100Mbps, Full Duplex
Ethernet connected (DNS 192.168.50.1) - syncing time
SNTP started (pool.ntp.org), resync every 3600 s
AP started: SRT_Controller at 192.168.4.1
...
Async web server listening on port 80
Stellarium async server listening on port 10001
Free memory: <n> bytes
Ethernet IP: 192.168.50.120
AP IP: 192.168.4.1
```

The repository OTA target is configured for `192.168.50.120:3232`.

---

## 3. Configuration Reference

### Compile-Time Defaults (config.h)

These values are used as defaults when no saved settings exist:

```cpp
// WiFi Access Point
#define WIFI_AP_SSID "SRT_Controller"
#define WIFI_AP_PASSWORD "radio1420"

// Serial connection to Arduino Due (WT32-ETH01 pins)
#define DUE_UART_TX 4    // WT32 IO4 -> Due RX (pin 19)
#define DUE_UART_RX 14   // WT32 IO14 <- Due TX (pin 18)
#define DUE_BAUD_RATE 115200

// Observer location (Acre Road Observatory, Glasgow)
#define OBSERVER_LAT 55.902426
#define OBSERVER_LON -4.307865

// NTP: name first, numeric fallback (time.cloudflare.com) if DNS fails
#define NTP_SERVER "pool.ntp.org"
#define NTP_SERVER_FALLBACK "162.159.200.123"
#define NTP_SYNC_INTERVAL_MS 3600000UL  // SNTP resync every hour
#define CLOCK_STALE_WARN_S   18000UL    // warn after 5 h with no sync
#define NTP_RETRY_INTERVAL_MS 300000UL  // restart SNTP every 5 min until first sync

// Ethernet static-IP fallback (only if DHCP is switched off)
#define DEFAULT_ETH_STATIC_IP "192.168.50.120"
#define DEFAULT_ETH_GATEWAY   "192.168.50.1"
#define DEFAULT_ETH_SUBNET    "255.255.255.0"
#define DEFAULT_ETH_DNS       "192.168.50.1"

// Mount software limits (degrees, DRIVE frame)
#define MOUNT_AZ_MIN 2.0
#define MOUNT_AZ_MAX 353.0
#define MOUNT_ALT_MIN 0.0
#define MOUNT_ALT_MAX 90.0

// Observing horizon (degrees, TRUE frame)
#define TRACKING_HORIZON_ALT 10.0

// Acquisition floor for the galactic-plane target (degrees)
#define GALACTIC_PLANE_MIN_ALT 45.0

// Stow position (degrees, DRIVE frame; pointing model bypassed)
#define STOW_ALT 90.0
#define STOW_AZ 180.0

// Position deadband (degrees)
#define POSITION_DEADBAND 0.25
```

### Runtime Settings (Web Interface)

Settings can be changed via the web interface Settings tab:

| Setting | Description | Default |
|---------|-------------|---------|
| Observer Lat/Lon | True site position (never a pointing correction) | 55.902426, -4.307865 |
| Software Limits | Mount limits in the drive frame, applied after the pointing model | 2-353, 0-90 |
| Observing Horizon | Minimum true altitude for sky targets; below it, tracking parks the dish | 10 deg |
| Galactic plane acquire above | Lowest altitude at which the galactic-plane target may be *started* | 45 deg |
| Stow Position | Where the dish parks when its target sets and on `/go-home`; drive coordinates | Alt=90, Az=180 |
| Update Tolerance | Minimum position change to trigger update | 0.25 deg |
| Page Name | Web interface title | "SRT Controller" |
| AP SSID/Password | WiFi access point credentials | SRT_Controller/radio1420 |

Settings are saved to ESP32 flash (NVS) and persist across reboots.

The 10 degree observing horizon is enforced separately from the mechanical altitude lower limit. This keeps automatic tracking above local obstructions even if the saved mount minimum remains 0 degrees. The horizon is a true-frame test, made on the target's sky altitude before the pointing model; the mount limits are drive-frame and clamp after it.

The stow is held in **drive** coordinates and the pointing model is bypassed on the stow path: parking is mechanical, not an observation. At the zenith azimuth is degenerate and the model's `tan(alt)` azimuth term would ask for a ~50 degree correction that moves the beam by nothing. It is still clamped to the mount limits. The stow is unrelated to the Due's `HOMEALT`/`HOMEAZ` (`cfg.homeAlt/homeAz`), which are the encoder origin that the limit-switch stall corresponds to.

Galactic plane tracking uses a second, higher threshold. The controller walks outwards along b = 0 from the galactic centre and takes the first longitude at or above the acquisition altitude, preferring the higher of the two equidistant candidates because it stays observable longer. That is an acquisition floor, not a horizon: once tracking, the target is followed down until the 10 degree horizon parks the dish. At 45 degrees a suitable point exists about 73 percent of the time and never comes closer than roughly 45 degrees of longitude to the centre; lowering it to 30 degrees raises availability to about 94 percent.

---

## 4. Source Code Structure

### main.cpp

Main application entry point and tracking loop.

**Key Functions:**

- `setup()` - Initialize hardware, WiFi, web server; start the loop watchdog last
- `loop()` - Handle servers, update tracking, clock status
- `updateTracking()` - Convert RA/Dec to true Alt/Az, then to drive, send to Due
- `syncTimeNTP()` - Start SNTP, or restart it after a link change (non-blocking)

**Tracking Loop Behavior:**

1. Reads whatever the Due has sent on every loop pass (~100 Hz); polls `STATUS` and updates the target once a second
2. A `/stop/movement` hold suspends sends until it expires
3. For Sun, Moon and Galactic Plane targets, refreshes the RA/Dec every 30 seconds
4. Converts RA/Dec to **true** Alt/Az with the true site position
5. Horizon test in the true frame: below `horizonAlt` it sends the stow once (drive coordinates, model bypassed, clamped) and sets `waiting_for_rise`; tracking resumes when the target rises
6. Adds the operator's `/offset` in the true frame, applies az-only / alt-only axis holds
7. `trueToDrive()`: refraction, then the model terms
8. Drive azimuth outside the mount limits: holds and sets `waiting_for_wrap` until it comes back
9. Drive altitude above `mountAltMax`: holds in place (not driven to the stop) and sets `waiting_for_descend`
10. Clamps to the mount limits (the lower altitude stop) and sends with `sendDriveTarget()` if either axis moved by at least the deadband, drive against drive

Every state change (parking, wrap, descend, resume, and every clearing of a live target with the endpoint and client address) goes to the serial log through `logESP()`.

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
srtSerial.sendDriveTarget(driveAlt, driveAz); // Drive coordinates only, "%.1f %.1f"
srtSerial.sendHome(); sendStop(); sendReset(); sendCalibrator(on);
srtSerial.requestStatus();        // "STATUS"
srtSerial.readStatus();           // Read and parse every waiting line
srtSerial.getCurrentAlt();        // Reported drive position
srtSerial.getStatusStr();         // Get status string
srtSerial.getHomingReportJSON();  // last_homing in /status
srtSerial.getDriveAckJSON();      // drive_ack in /status
srtSerial.logESP(msg);            // Serial log + diag RTC ring
```

The Due answers every drive target with `ACK DRIVE <alt> <az>` (the target
rounded to the 0.5 degree pulse grid) or `ERR DRIVE fault|homing|limits`, and an
unparseable line with `ERR UNKNOWN <line>`. A target unanswered after 2 s is
sent once more; a second silence counts as lost. Re-sending starts only once
an ACK has been seen, so a Due flashed before 2026-09-30 is left alone. A
mid-homing refusal arrives as `Homing: busy - ignored` and counts as refused.

The serial log is a 30-entry RAM ring (`TX`, `RX`, `ESP` entries), served by
`/serial/log`.

### pointing.cpp

The boundary between the two frames. **True** Alt/Az is where the dish looks on
the sky; **drive** is what the mount mechanically is (encoder pulses, 0.5
degree grid). The Due works only in drive coordinates.

```cpp
trueToDrive(trueAlt, trueAz, driveAlt, driveAz); // refraction, then model terms
driveToTrue(driveAlt, driveAz, trueAlt, trueAz); // three-pass fixed-point inverse of trueToDrive
driveAltWithinLimits(a); driveAzWithinLimits(z); clampToMountLimits(a, z);
refractionDeg(trueAlt);           // Bennett x 1.15 (radio), applied with or without a model
```

Model terms: `IE`, `IA`, `AN`, `AE`, `CA`, `NPAE`, `TF` (degrees) and
`AZSCALE` (degrees per degree). All default to zero; no model loaded is the
identity transform plus refraction. Every goto, track and manual move goes
through `trueToDrive()` before `sendDriveTarget()`; every measured position
(`/status` RA/Dec and l/b, Stellarium) goes through `driveToTrue()` first. The
model is stored in its own NVS namespace, so `/settings/reset` does not discard
it.

### diag.cpp

A record in `RTC_NOINIT` memory that survives every reset except a power
cycle: the loop stage last entered, uptime, time since the last target sent,
longest loop pass, heap low-water marks, and a ring of the last 24 `logESP`
events. At boot the previous record becomes `previous_boot` in `/diag`, with
the reset reason (panic, task watchdog, brownout, power-on, software).

The loop task is on the task watchdog at **30 s** (not the core's 5 s:
`readStringUntil` timeouts and WiFi power changes legitimately take seconds).
It is fed from the OTA `onProgress` callback, since an upload runs inside one
loop pass.

### sync.h

One recursive mutex (`SRTLock`) guards the state shared between `loopTask`
(tracking, Due serial, clock) and `async_tcp` (web handlers, Stellarium). The
wait is bounded at 2 s; on timeout the caller proceeds unlocked and
`lock_timeouts` in `/status` counts it.

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
| `/ping` | GET | Minimal plain-text liveness check (`ok`) |
| `/network` | GET | Minimal diagnostics for heap, active IPs and the lwIP resolver (issue #11) when the full UI is hard to load |
| `/status` | GET | Mount position and status |
| `/diag` | GET | This boot's and the previous boot's diagnostic record, reset reason, watchdog state. Lock-free: answers even with the loop stuck holding the lock |
| `/serial/log` | GET | Last 30 serial log entries (`time`, `dir` TX/RX/ESP, `msg`) |
| `/tracking` | GET | Current tracking state, including `waiting_for_rise`, `waiting_for_wrap`, `waiting_for_descend`, offsets |
| `/ephemeris` | GET | Sun, Moon and galactic-plane target (`sun`, `moon`, `plane`) |
| `/pointing` | GET | Pointing model in force |
| `/time/status` | GET | Time sync status |
| `/settings` | GET | Current settings (AP password not served) |

**Example: `/status` response**

```json
{
  "alt": 45.00,
  "az": 180.00,
  "true_alt": 44.87,
  "true_az": 181.24,
  "pointing_loaded": true,
  "ra": 12.3456,
  "dec": 45.67,
  "gal_l": 120.00,
  "gal_b": 30.00,
  "target_alt": 45.00,
  "target_az": 180.00,
  "alt_current_a": 0.15,
  "az_current_a": 0.20,
  "status": "Ready",
  "fault": "",
  "fault_active": false,
  "is_slewing": false,
  "calibrator": false,
  "clock_state": "ok",
  "clock_age_s": 1200,
  "lock_timeouts": 0,
  "serial_splices": 0,
  "malformed_status": 0,
  "last_homing": {
    "alt_error_first_deg": -0.5,
    "az_error_first_deg": -1.0,
    "alt_error_second_deg": null,
    "az_error_second_deg": null,
    "reapproach_skipped": true,
    "utc": 1787825741
  },
  "drive_ack": {
    "ack_seen": true,
    "acks": 412,
    "refused": 0,
    "resends": 0,
    "lost": 0,
    "unknown_commands": 0,
    "pending": false,
    "last_refusal": null
  },
  "uptime_s": 86400,
  "boot": 7,
  "reset_reason": "software (restart or OTA)",
  "loop_age_ms": 4,
  "free_heap": 180000,
  "raw": "Alt:45.0 Az:180.0 Ialt:0.1A Iaz:0.2A Status:Ready -> Alt:45.0 Az:180.0 Cal:OFF"
}
```

`alt`/`az` are the Due's reported **drive** position — the frame the
scheduler's slew check and `sun_scan.py` need. `true_alt`/`true_az` are the sky
position it corresponds to (`driveToTrue()`), and `ra`/`dec` and
`gal_l`/`gal_b` are computed from those. `target_alt`/`target_az` are the
Due's drive target. `pointing_loaded` is false when no model is stored (the
transform is then refraction only).

`clock_state` is `ok`, `stale` (no SNTP sync for 5 h), `unverified` (set from a
browser, never by SNTP) or `never`; `clock_age_s` is -1 before the first sync.
`lock_timeouts` counts cross-task lock acquisitions that timed out; it should
stay at zero. `serial_splices` counts lines from the Due that were neither a
status line nor a homing line (issue #34); `malformed_status` currently reports
the same counter. `drive_ack` is described under `srt_serial.cpp`; `lost`
should stay at zero. `uptime_s`, `boot`, `reset_reason`, `loop_age_ms` and
`free_heap` are enough to notice a reboot or a hung loop from a poll; `/diag`
has the rest. `raw` is the last status line as received.

`last_homing` is the encoder error the Due reports at the last homing (issue
#24), latched from its `Homing: <axis> limit reached at N pulses (D deg)`
lines. The **first approach** is the count error accumulated since the
previous homing — the stop is the true zero, so a healthy axis reads within
a pulse or two. The **re-approach** after the 5° back-off is the stop's
repeatability, when there is one: the Due skips it when both axes met their
switches at creep and the azimuth cut edge was captured (issue #33), prints
`Re-approach skipped`, and the second-approach values are then `null` with
`reapproach_skipped: true`. `last_homing` is `null` until a homing has run. The
value is latched here because the status flood during a homing scrolls the
line out of the 30-line serial log before a reader can poll it.

**Example: `/diag` response** (events abridged)

```json
{
  "reset_reason": "task watchdog",
  "loop_watchdog": true,
  "free_heap": 180000,
  "max_alloc": 110000,
  "loop_age_ms": 3,
  "this_boot": { "boot": 8, "uptime_s": 60, "last_stage": "loop delay", "last_target_age_s": null,
                 "max_loop_gap_ms": 40, "min_free_heap": 170000, "min_max_alloc": 100000,
                 "events": ["+5s NTP synced (first since boot)"] },
  "previous_boot": { "boot": 7, "uptime_s": 86400, "last_stage": "tracking: reading the Due",
                     "last_target_age_s": 31, "max_loop_gap_ms": 450, "min_free_heap": 120000,
                     "min_max_alloc": 60000,
                     "events": ["14:09:11 Tracking Sun cleared by /stop/all from 192.168.50.1"] }
}
```

`last_stage` is the loop stage last entered, so after a watchdog reset it names
where the loop hung. `last_target_age_s` is the time since tracking last sent a
target (`null` if none this boot). Events carry UTC once the clock is set,
`+Ns` since boot before that.

`previous_boot` is `null` after a power cycle, which clears RTC memory.

### Control Endpoints

| Endpoint | Parameters | Description |
|----------|------------|-------------|
| `/goto` | `ra`, `dec`, optional `track=0` | Go to RA/Dec (J2000). Default tracks (historical goto-implies-tracking); `track=0` slews once and stops, and returns 400 if the drive position is outside the mount limits. The UI's Go To sends `track=0` |
| `/goto/galactic` | `l`, `b`, optional `track=0` | As `/goto`, for galactic coords; returns the RA/Dec |
| `/track/radec` | `ra`, `dec` | Track RA/Dec (J2000) |
| `/track/galactic` | `l`, `b` | Track galactic coords |
| `/track/galactic-plane` | (none) | Track the galactic-plane point nearest the centre above `galacticMinAlt`; 409 if none exists now. Target name `Galactic Plane`, re-chosen every 30 s |
| `/track/sun` | (none) | Track Sun |
| `/track/moon` | (none) | Track Moon (topocentric) |
| `/tracking/enable` | `enable=0/1` | Enable tracking of the current target, or stop tracking and clear it |
| `/tracking/axis` | `mode=both/az/alt`, optional `alt` or `az` | Track both axes or hold one axis fixed |
| `/direct` | `alt`, `az` | Go to a **true** Alt/Az: clears tracking, applies `trueToDrive()`, returns `drive_alt`/`drive_az`, or 400 if outside the mount limits. With az-only or alt-only tracking active it sets the held axis instead (clamped) |
| `/go-home` | (none) | Clear tracking and drive to the stow, in drive coordinates with the pointing model bypassed, clamped to the mount limits. Returns `drive_alt`/`drive_az` |
| `/stop/movement` | (none) | Stop current motion and hold automatic tracking sends for 10 seconds |
| `/stop/slewing` | (none) | Same as `/stop/movement` |
| `/stop/tracking` | (none) | Clear the tracking target without sending a motion stop |
| `/stop/all` | (none) | Stop motion and clear the current tracking target |
| `/reset` | (none) | Clear an active Due fault; 409 if none is active |
| `/home` | (none) | Clear tracking and run the Due homing sequence |
| `/offset` | `alt`, `az` | Set the operator's pointing offset (degrees, true frame, on top of the model) |
| `/offset/clear` | (none) | Clear pointing offset |
| `/calibrator` | `on=1\|true\|0` | Send `CAL ON`/`CAL OFF` to the Due. The noise diode it once switched is gone (issue #39); the Due's pin still switches and `calibrator` in `/status` follows it |

All goto and track endpoints except `/track/sun`, `/track/moon` and
`/track/galactic-plane` refuse a target below `horizonAlt` with 400 before
changing any state. Every clearing of a live tracking target is logged with the
endpoint and the client address.

### Pointing Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/pointing` | GET | Model in force: `loaded`, `version`, `n_scans`, `fitted_utc`, `terms` |
| `/pointing/apply` | POST | Replace the model from a JSON document in the body, save to NVS |
| `/pointing/clear` | GET | Zero the model and erase it from NVS |

`/pointing/apply` takes the document the scheduler builds
(`sun_scan.pointing_model_document()`):

```json
{"version": 1, "fitted_utc": "2026-09-23T12:00:00Z", "n_scans": 14,
 "frame": "cross_elevation",
 "terms": {"IE": -1.2, "IA": 0.5, "AN": 0.1, "AE": -0.1, "CA": -1.4, "AZSCALE": 0.0036}}
```

The controller reads `version`, `n_scans`, `fitted_utc` and `terms`; other
keys (`frame`, `site`, `residual_rms_deg`) are carried for the record and
ignored. Missing or malformed `version`, a version below 1, no `terms` object, no recognised term, a
malformed number or a term outside its plausible range rejects the whole
document with 400 and leaves the current model in force. Unknown term names are
ignored. Bodies over 1536 bytes are refused with 413. The response carries the
model as stored.

### Settings Endpoints

| Endpoint | Description |
|----------|-------------|
| `/settings` | GET current settings |
| `/settings/save` | Save settings (query params: `observer_lat`, `observer_lon`, `mount_az_min/max`, `mount_alt_min/max`, `horizon_alt`, `galactic_min_alt`, `stow_alt`, `stow_az`, `position_deadband`, `ap_ssid`, `ap_password`, `page_name`) |
| `/settings/reset` | Reset to defaults (the pointing model is kept) |

### Time Endpoints

| Endpoint | Parameters | Description |
|----------|------------|-------------|
| `/time/status` | (none) | Clock state, see section 8 |
| `/time/sync` | (none) | Restart the SNTP client now; returns at once, the result appears in `/time/status` |
| `/time/set` | `timestamp` (Unix s) | Set the clock from a browser. Source becomes `browser`; `lastSyncEpoch` is not touched, so a clock that SNTP has never synced this boot reads `unverified` |

### Network Endpoints

| Endpoint | Description |
|----------|-------------|
| `/wifi/status` | Network status (includes WiFi, Ethernet, MAC addresses) |
| `/wifi/scan` | Scan for WiFi networks |
| `/wifi/connect` | Connect to WiFi network |
| `/wifi/forget` | Forget saved WiFi credentials |
| `/wifi/power` | Enable/disable WiFi (`enable=0/1`) - requires Ethernet connection |
| `/eth/save` | Save Ethernet settings (dhcp, ip, gateway, subnet, dns) |

The `/wifi/status` response includes hostname, mDNS URL, OTA status, `wifi_enabled`, `eth_mac` and `wifi_mac` fields.

---

## 6. Stellarium Protocol

The controller implements the Stellarium telescope protocol on TCP port 10001.

### Stellarium Setup

1. **Configuration > Plugins > Telescope Control**
2. Enable plugin and restart Stellarium
3. Add telescope:
   - Type: "External software or remote computer"
   - Host: ESP32 IP address (normally `192.168.50.120`; AP fallback `192.168.4.1`)
   - Port: `10001`
4. Connect
5. Select any object and press `Ctrl+1` to slew

### Coordinate Handling

- Stellarium sends **J2000** coordinates
- A goto becomes an ordinary tracking target (unnamed RA/Dec); there is no horizon check at the goto, so a target below `horizonAlt` parks at the stow through the tracking loop
- Position reports, every 100 ms, are where the dish **is**: the Due's measured drive position, brought to true Alt/Az with `driveToTrue()`, then to J2000 RA/Dec. Not the commanded target

---

## 7. Coordinate System

### Reference Frame

All equatorial coordinates use **J2000** (epoch J2000.0, equinox J2000.0):

- Standard for modern star catalogs
- Used by Stellarium, SIMBAD, and most planetarium software

### Precession

The controller applies IAU 1976 precession to convert J2000 coordinates to the equinox of the current date before computing Alt/Az.

### True and Drive Frames

Everything astronomical is computed in the **true** frame with the true site
latitude and longitude. `trueToDrive()` converts a true position to the
**drive** frame the Due works in: radio refraction first (applied whether or
not a model is loaded; `sun_scan.refraction_deg()` must stay identical to
`refractionDeg()`), then the model terms. `driveToTrue()` inverts it. The
observing horizon is tested before the transform, the mount limits after it,
and arrival checks stay wholly in the drive frame. See `pointing.h`.

### Sun and Moon

Sun and Moon positions are calculated using simplified algorithms:

- **Sun accuracy:** ~1 arcmin
- **Moon:** topocentric since 2026-09-07 (Meeus ch. 40 parallax, issue #3 C14); agrees with pyephem to 0.025 deg. Before that it was geocentric and up to ~1 deg out

---

## 8. Time Synchronization

Accurate UTC time is required for coordinate transforms.

### NTP (Primary)

The lwIP SNTP client runs in the background; nothing blocks on it. It is
started once, on the first link up:

```cpp
esp_sntp_set_time_sync_notification_cb(onTimeSync);
esp_sntp_set_sync_interval(NTP_SYNC_INTERVAL_MS);   // hourly
configTime(0, 0, NTP_SERVER, NTP_SERVER_FALLBACK);   // pool.ntp.org, 162.159.200.123
```

The numeric fallback keeps the clock syncing when DNS fails, so `"source":"NTP"`
proves the clock synced, not that name resolution works. An Ethernet link-up
restarts the client (`esp_sntp_restart()`); a clock that has never synced this
boot is restarted every 5 min. Success is declared only by `onTimeSync()`, when
SNTP has actually set the clock: a soft reset keeps the RTC running, so a
plausible-looking time after boot proves nothing. Each resync records the
correction it applied (`last_offset_ms`) as a drift diagnostic. After 5 h with
no sync the controller logs a warning; tracking is never blocked.

Proof that the link's NAT works is a fresh sync after a reboot: `sync_count` 1
with a small `last_sync_age_s`.

### Browser Fallback

If `/time/status` reports `synced: false`, the web interface sends browser
time on page load (`/time/set`). A browser-set clock reports `unverified`.

### Time Status

Check via `/time/status`:

```json
{
  "synced": true,
  "source": "NTP",
  "sync_state": "ok",
  "stale": false,
  "last_sync_age_s": 1200,
  "last_offset_ms": -12,
  "sync_count": 25,
  "utc": "2026-09-30 14:30:00",
  "timestamp": 1790778600
}
```

`sync_state` is `ok`, `stale`, `unverified` or `never`. `last_sync_age_s` is -1
before the first SNTP sync; `stale` is true then as well. `last_offset_ms` is 0
for the first sync of a boot.

---

## 9. Networking

### Network Modes

The ESP32 operates in **AP+STA** mode:

1. **WiFi Access Point** - Active at 192.168.4.1 (can be disabled); the fallback when Ethernet is unreachable
2. **WiFi Station** - Connects to saved network if available
3. **Ethernet** - Native WT32-ETH01 LAN with DHCP or static address

### The Private Link

The controller is not on the observatory LAN. Its Ethernet goes to a TP-Link
TG-3468 (`enp5s0`) in the observatory computer, NetworkManager connection
`srt-link`, `ipv4.method shared`, host at `192.168.50.1/24`. Shared mode
supplies the address, DHCP/DNS and the NAT the controller needs to reach
`pool.ntp.org`. The controller stays on **DHCP**; its address is pinned by MAC
on the host in `/etc/NetworkManager/dnsmasq-shared.d/srt.conf`
(`dhcp-host=70:4B:CA:58:59:8B,192.168.50.120`). Nothing on the controller is
configured for the link, so moving the cable back to a campus switch reverses
it. Other machines on the observatory LAN cannot reach it. Build procedure:
`docs/OBSERVATORY_HOST_SETUP.md`.

### Startup Sequence

1. Load settings and the pointing model from NVS; open the Due UART
2. Initialize Ethernet, wait up to 5 s for a lease; on link up start SNTP
3. Always start the WiFi Access Point, then try saved station credentials (15 s timeout)
4. Restore the DNS resolver if WiFi startup cleared it (issue #11; checked every loop pass thereafter)
5. Start the web server, mDNS, OTA and the Stellarium server; arm the loop watchdog

### IP Addresses

| Interface | IP Address |
|-----------|------------|
| Private link Ethernet | `http://192.168.50.120/` |
| Hostname | `http://srt-controller.local/` |
| WiFi AP | 192.168.4.1 (fixed) |
| WiFi Station | DHCP assigned |
| Ethernet (WT32-ETH01) | DHCP (reserved by MAC on the host) or static |

### Ethernet Configuration (WT32-ETH01)

Ethernet IP can be configured via the web interface Network tab:

- **DHCP** (default, and the normal mode): address from the host's reservation
- **Static IP**: Manually configure IP, gateway, subnet, and DNS. The form is pre-filled with `192.168.50.120` / gateway and DNS `192.168.50.1` / `255.255.255.0`, the link the controller is on

Settings are stored in non-volatile memory (NVS) and persist across reboots.
Changes require a reboot to take effect.

### WiFi Power Control

When Ethernet is connected, WiFi can be disabled to save power (~80-120mA):

- **Web UI**: Network tab > WiFi Power > Disable WiFi
- **API**: `GET /wifi/power?enable=0`

The radio change is queued for the loop and the request answers at once
(`pending: true`). The setting is **not** persisted: WiFi and the AP come back
on every boot, which keeps `192.168.4.1` as the fallback path. WiFi cannot be
disabled unless Ethernet is connected.

---

## 10. Development

### Serial Monitor

The WT32-ETH01 has no USB port; its console is UART0 on TX0/RX0, reached with
the temporary FT232 programmer.

```bash
~/.platformio/penv/bin/pio device monitor -b 115200
```

In normal running the serial log (`/serial/log`) and `/diag` are the
controller's record, and the Due's Native USB port mirrors the ESP32-to-Due
command traffic (see `WT32_ETH01_MIGRATION.md` section 6).

### Debug Output

```cpp
#define DBG(x) if (Serial) { x; }
DBG(Serial.println("Debug message"));
```

The guard was for the ESP32-S3's USB CDC, which blocked without a host
attached. On the WT32-ETH01 `Serial` is a plain UART and the guard is always
true; output goes to TX0 whether or not anything is listening.

### Memory Usage

Typical usage: ~42% flash, ~14% RAM on WT32-ETH01 (ESP32 with 4MB flash).

### Building

```bash
PIO=~/.platformio/penv/bin/pio   # Windows: ~/.platformio/penv/Scripts/pio.exe

# Build only (wt32-eth01 is the default environment)
$PIO run

# Build and upload over Ethernet
$PIO run -e wt32-eth01-ota --target upload

# Host unit tests
$PIO test -e native

# Clean build
$PIO run --target clean
```

The `native` environment builds only `coordinates.cpp` and `pointing.cpp`
(`build_src_filter`) with `test/shim/` standing in for the Arduino headers, so
the firmware source is tested exactly as it is flashed.

---

## 11. Troubleshooting

### Web Interface Issues

| Problem | Solution |
|---------|----------|
| Can't connect to 192.168.50.120 | Reachable only from the observatory computer. Check the `srt-link` connection on `enp5s0`, the cable and controller power; try `/ping` or `/network`, or the AP at 192.168.4.1 |
| Can't connect to 192.168.4.1 | Verify connected to SRT_Controller WiFi |
| Page loads but no data | Check Due serial connection |
| Settings won't save | Check NVS, try reset to defaults |

### Coordinate Issues

| Problem | Solution |
|---------|----------|
| Position doesn't match sky | Check observer lat/lon in settings (the true site, never a correction) and `/pointing` |
| Large errors (>1 deg) | Check time sync status |
| `pointing_loaded: false` | No model stored (cleared, or flash erased); the transform is refraction only. Re-apply the model from the scheduler |

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
| `serial_splices` rising | Due replies arriving faster than read, or bytes lost on the link (issue #34) |
| `drive_ack.lost` non-zero | A drive target went unanswered twice; check `/serial/log` |
| Controller restarted unexpectedly | `/diag`: `reset_reason` and `previous_boot.last_stage` |

---

## Appendix: Quick Reference

### Default Credentials

| Setting | Value |
|---------|-------|
| WiFi AP SSID | SRT_Controller |
| WiFi AP Password | radio1420 |
| Private link Ethernet IP | 192.168.50.120 |
| AP IP | 192.168.4.1 |
| Web Port | 80 |
| Stellarium Port | 10001 |

### Pin Assignments (WT32-ETH01)

| Function | GPIO |
|----------|------|
| Due TX | 4 |
| Due RX | 14 |

### Coordinate Ranges

| Coordinate | Range |
|------------|-------|
| RA | 0 - 24 hours |
| Dec | -90 to +90 degrees |
| Galactic l | 0 - 360 degrees |
| Galactic b | -90 to +90 degrees |
| Altitude | 0 - 90 degrees (mount limits, drive frame) |
| Azimuth | 2 - 353 degrees (mount limits, drive frame) |

---

**License:** MIT License - Acre Road Observatory, University of Glasgow
