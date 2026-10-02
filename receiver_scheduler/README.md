# Hydrogen Line (21cm) Receiver

A real-time spectrum analyzer for observing the hydrogen line at 1420.405 MHz using software-defined radio. Features a PyQtGraph GUI with live spectrum and waterfall displays, continuous data logging to HDF5, and a web-based observation scheduler.

## Overview

This receiver is designed for radio astronomy observations of neutral hydrogen (HI) emissions at 1420.405 MHz. It provides:

- Real-time spectrum display with integrated averaging
- Waterfall/spectrogram visualization
- Continuous data recording to HDF5 format
- Support for multiple SDR platforms (Ettus B200/B210, RTL-SDR)
- Demo mode for testing without hardware
- Web-based scheduler for automated observations

## Files

| File | Description |
|------|-------------|
| `h1_web_scheduler.py` | The Flask scheduler: routes, the schedule, and the observing state machine |
| `web/` | The operator page as static files — `index.html`, `app.css`, and fifteen scripts under `js/`: one per tab (ten), plus `state.js` (first), `shared.js`, `help.js`, `clock.js` and `boot.js` (last). Read per request, so editing them needs a browser refresh and not a scheduler restart |
| `b210_h1_receiver.py` | The receiver. `--headless` for observing, a Qt window for warm-up at the console; `H1_MODE=pulsar` records the pulsar band instead of spectra |
| `sun_scan.py` | Sun raster, pointing-model fit, calibration day |
| `horizon_scan.py` | Radiometric horizon measurement, and the Stellarium landscape export |
| `rf_calibration.py` | Counts to kelvin: gain, system temperature, and the velocity shift (held at zero on the locked Thunderbolt reference) |
| `beam_scan.py` | The beam from a two-hour Sun drift: main-lobe solid angle integrated directly, adopted into `beam_calibration.json` only if the scan passes |
| `scallop.py` | The tracking scallop, fitted from a tracked Sun/Moon/Jupiter run (or carried from the last quiet one) and taken out of the source term |
| `pulsar_fold.py` | B0329+54: the streaming fold at the absolute phase, the matched S/N, the flux scale, and the PRESTO fold |
| `pulsar_toa.py` | Times of arrival from the fold against the EPN template; writes `pulsar_timing/b0329.tim` (nights) and `b0329_segments.tim` (4 h checks) |
| `pint_tools.py` | The PINT side of timing, run by subprocess in the `pint` environment, never radioconda |
| `sigproc_export.py` | A pulsar recording as a SIGPROC `.fil` for PRESTO, overflow gaps filled so every row sits on the time grid |
| `clocks.py` | The three clocks (Thunderbolt, this computer's NTP, the controller's) judged in one place; the modified Allan deviation and its transition |
| `thunderbolt.py` | Reads the Thunderbolt's TSIP status in a thread; the one reader of its serial port, and the queue for commands to it |
| `plot_backend.py` | `use_headless()`: Agg for scripts and the scheduler, left alone in a Jupyter kernel |
| `bandpass.py` | The measured instrument response: one template per product, and whether it applies to a given tuning |
| `tuning.py` | The **fixed instrument** — LO, sample rate, gain and the two product bands — shared by the receiver and the scheduler; the older LO-offset planner is kept for the console window |
| `drift_park.py` | Parks a drift scan on the drive grid: the controller's transform in Python, validated against its own reports |
| `drift_fit.py` | The total-power fit of a drift scan against the predicted drift curve (continuum band only) |
| `observation_files.py` | Where recordings go and what they are called |
| `observation_plot.py` | Reads a recording (live or finished, either product, always as counts) and renders it, in kelvin and on an LSR velocity axis |
| `observatory.py` | Where the telescope is and how big its beam is — plumbing only; the numbers live in `astro_simulator/instrument.py` |
| `solar_reference.py` | The professional solar flux quoted beside ours: reads NOAA SWPC's hourly RSTN local-noon file, keeps a dated history in `data/`, never waits on the network |
| `pilot.py` | The **pilot** (issue #30): the B200's own TX as the gain and passband reference — a full-band comb burst every N-th record plus a continuous carrier for the fast wobble; the frames, the flat rectangular reference, the per-burst response, the power level and tilt, the delay-filtered passband correction and its reversal. GNU-Radio-free so the scheduler can reduce with it. See `docs/CALIBRATION.md` |
| `ad9361_filters.py` | **Not imported at runtime.** A standalone account of the B200's decimation-filter chain and the passband shape it implies, run by hand; `bandpass.py`'s docstring cites its result (about 4% of the measured response). Kept as the reasoning behind measuring the bandpass rather than modelling it |
| `investigations/lo_shape_*.py`, `investigations/freq_switch_demo.py` | One-off investigations from the LO-placement work, not imported by anything, kept for their reasoning |
| `tests/page_sources.py`, `tests/conftest.py` | Test plumbing: collects the operator page and every script it loads; keeps the suite out of the observatory's records |
| `../tools/` | Hardware-side scripts, none of them part of the observing path: `due_emulator.py` (bench emulator of the Due's serial protocol for ESP32 work), `az_switch_probe_capture.py` (drives the Due's `PROBE` command and records the azimuth limit switch's reed/current trace, issue #32), `homing_scan_experiment.py` (home from the Sun's position then Sun scan, repeated, for homing-repeatability tests) |

The JSON files beside the code are the scheduler's state, not code; `data/` holds recordings, plots and diagnostic archives; `horizon_profiles/`, `pointing_models/` and `beam_calibrations/` are the dated archives of measurements. The calibrations (`gain_calibration.json`, `bandpass_template*.json`, `beam_calibration.json`), `pointing_data.json`, the horizon and pointing archives and `pulsar_timing/` are tracked by git; `h1_schedule.json`, `scheduler_config.json`, `pointing_model.json`, `last_observation.json`, `scallop_reference.json` and `data/` are not (`docs/HOST_REBUILD.md` lists what to save).

### State the scheduler keeps

| File | Description |
|------|-------------|
| `h1_schedule.json` | The observation schedule |
| `scheduler_config.json` | Local configuration (auto-generated, gitignored) |
| `scheduler.log` | Rotating log — the operational record of what the telescope did |
| `bandpass_template.json`, `bandpass_template_wide.json` | The measured bandpass in force, one per product |
| `gain_calibration.json` | The gain and system temperature in force, and the velocity shift (zero, and never carried, when fitted on the locked reference) |
| `beam_calibration.json` | The measured beam in force (21.0 sq deg, 4.30° FWHM-equivalent since 2026-09-25) |
| `scallop_reference.json` | The scallop parameters carried from the last quiet solar track |
| `pulsar_timing/` | `b0329.tim`, `b0329_segments.tim` and the stored fold of every pulsar recording (`profiles/`) |
| `pointing_data.json` | Every Sun scan on file, which the pointing model is fitted from |
| `horizon_profiles/` | Every horizon scan, kept by date, with `active.json` naming the one in force |
| `horizon_profile.json` | A mirror of whichever horizon profile is active, for older readers |
| `last_observation.json` | Points at the last run, so the Observe tab survives a restart |
| `requirements.txt` | Python package dependencies |
| `start_srt_software.sh` | The desktop launcher: starts the scheduler and opens the controller in Firefox |
| `start_platformio_monitor.sh` | Single-instance serial monitor with device wait and lock retry handling |
| `SRT Software.code-workspace` | VS Code workspace with automatic scheduler and serial-monitor tasks |

### Tests

`cd receiver_scheduler && python -m pytest` — about 540 tests (2026-09-14). `conftest.py` keeps the suite out of the observatory's records: it detaches the loggers from `scheduler.log` and redirects the last-observation pointer, because several tests run the real `stop_observation`. No test may open the B200; stub `sun_scan._B210PowerMeter` if you need the non-demo path. One test, `TestFlaskAPI::test_post_config`, reaches the live controller and fails on the observatory host for that reason alone.

| File | Covers |
|------|--------|
| `test_scheduler.py` | Scheduling, the observation lifecycle, preemption, the API, homing judgements, expired bookings, the per-entry gain override |
| `test_scheduler_state.py` | That the names describing a running observation agree with each other |
| `test_exclusion.py` | That only one thing at a time owns the SDR and the mount |
| `test_sun_scan.py` | Raster geometry, the pointing fit, obstruction handling |
| `test_horizon_scan.py` | Strip scanning, partial saves, deriving clearance afterwards |
| `test_horizon_store.py` | The dated archive, choosing which profile is in force, window trimming |
| `test_rf_calibration.py` | Gain and T_sys fitting, RFI rejection, the LO artefact |
| `test_rf_goto.py` | Sending the dish to the bandpass field, and refusing to while something else holds it |
| `test_bandpass.py`, `test_tuning.py` | The instrument response and the tuning plan |
| `test_fixed_instrument.py` | The fixed instrument: one tuning, two products per recording, and the legacy dataset names |
| `test_self_contained.py` | That a recording carries everything needed to re-reduce it, and that kelvin written at record time reverse exactly |
| `test_velocity_frame.py` | The LSR correction and the clock offset |
| `test_drift_park.py` | Parking a drift scan on the drive grid, against the controller's own reported positions |
| `test_drift_plot.py` | The drift-scan plot: total power per record, the recorded crossing marked |
| `test_live_plot.py` | The live plot on the Observe tab and the route table it is served from |
| `test_solar_recording_plot.py` | A solar track's recording drawn as flux against the clock, the way its live view was |
| `test_solar_reference.py` | The RSTN reference flux: parsing NOAA's file, the history, never waiting on the network |
| `test_pilot.py` | The pilot: the flat rectangular reference (and why the windowed one fails at the band edges), detection and non-detection (the TX unplugged), the framing offset, level and tilt as power quantities, the ripple recovered by averaging bursts and refused from too few, the kelvin write and its exact reversal with the burst records dropped, the per-entry switch and the endpoints; a demo-source flowgraph run that gates a switched burst |
| `test_sun_monitor.py`, `test_pulsar_monitor.py` | The two standing orders: when they run, what they yield to, and every manual start path that must stop them |
| `test_pulsar.py`, `test_pulsar_toa.py`, `test_pulsar_timing_update.py` | The fold, the TOAs and the `.tim` files, and the automatic timing update after a pulsar run |
| `test_beam_scan.py`, `test_scallop.py` | The beam from a Sun drift (including the drift angle's sign), and the scallop fit and its carried reference |
| `test_thunderbolt.py`, `test_clock_stability.py` | TSIP decoding, and the stability plot's deviation and transition |
| `test_page_structure.py`, `test_page_javascript.py` | That the operator page has not lost an element, a handler, or a route |

## Hardware Requirements

### Supported SDRs

| SDR | Frequency Range | Sample Rate | Notes |
|-----|-----------------|-------------|-------|
| Ettus B200/B210 | 70 MHz - 6 GHz | Up to 56 MHz | Recommended for best performance |
| RTL-SDR (R820T/R820T2) | 24 - 1766 MHz | Up to 2.4 MHz | Budget option, adequate for H1 |

**Note:** RTL-SDR dongles with the older E4000 tuner cannot reach 1420 MHz. Ensure you have an R820T or R820T2 tuner.

### Antenna

A suitable antenna for 1420 MHz is required. Common options include:
- Horn antenna
- Parabolic dish with appropriate feed
- Helical antenna
- Yagi-Uda antenna

### Optional: Low Noise Amplifier (LNA)

An LNA at the antenna improves sensitivity significantly. Look for:
- Frequency coverage including 1420 MHz
- Low noise figure (< 1 dB ideal)
- Adequate gain (20-30 dB typical)

## Software Requirements

### Radioconda

This project uses [Radioconda](https://github.com/ryanvolz/radioconda), a conda distribution that includes GNU Radio and SDR tools.

#### Installation

1. Download Radioconda from: https://github.com/ryanvolz/radioconda/releases

2. Install Radioconda:
   - **Windows:** Run the installer executable
   - **Linux/macOS:** Run the shell script installer

3. Activate the environment:
   ```bash
   conda activate radioconda
   ```

4. Install additional dependencies:
   ```bash
   pip install -r requirements.txt
   ```

   This installs Flask (web scheduler), ephem (satellite tracking), and pytest (testing).

### Included with Radioconda

- GNU Radio (gr-uhd, gr-osmosdr)
- PyQt5 / PyQtGraph
- NumPy
- h5py

These cannot be pip-installed and must come from Radioconda.

### SDR Drivers

#### Ettus B200/B210
- Install UHD drivers from [Ettus Research](https://files.ettus.com/binaries/uhd/latest_release/)
- Download FPGA images:
  ```bash
  uhd_images_downloader
  ```

#### RTL-SDR
- Drivers are typically included with Radioconda
- On Windows, you may need [Zadig](https://zadig.akeo.ie/) to install WinUSB driver

## Usage

### Running the Receiver Directly

```bash
# Activate Radioconda environment
conda activate radioconda

# Run with Ettus B200 (default)
python b210_h1_receiver.py

# Run with RTL-SDR
python b210_h1_receiver.py --sdr rtlsdr

# Run in demo mode (no hardware required)
python b210_h1_receiver.py --sdr demo
```

### Command Line Options

```
usage: b210_h1_receiver.py [-h] [--sdr {b210,rtlsdr,demo}] [--gain GAIN]
                           [--sample-rate SAMPLE_RATE] [--headless]

Hydrogen Line (21cm) Receiver

optional arguments:
  -h, --help            show this help message and exit
  --sdr, -s {b210,rtlsdr,demo}
                        SDR type (default: b210, which is the B200)
  --gain, -g GAIN       RF gain in dB (default: 30 for both SDRs)
  --sample-rate, -r SAMPLE_RATE
                        Sample rate in Hz (default: 2.4e6 for B200, 2.048e6 for RTL-SDR)
  --headless            Record without any GUI: no Qt, no display needed. This is
                        how scheduled and Observe-tab observations run.
```

`--gain` and `--sample-rate` set the console window only. A headless run records with
the fixed instrument (from `H1_INSTRUMENT` when the scheduler starts it) and
ignores both.

### Examples

```bash
# B200 with higher sample rate for wider bandwidth
python b210_h1_receiver.py --sdr b210 --sample-rate 5e6

# RTL-SDR with adjusted gain
python b210_h1_receiver.py --sdr rtlsdr --gain 45

# Demo mode for testing
python b210_h1_receiver.py --sdr demo
```

## Web Scheduler

The web scheduler provides a browser-based interface for managing and automating observations.

### Starting the Scheduler

On the observatory Linux host, double-click **Start SRT Sofware**. The launcher:

1. Starts `h1_web_scheduler.py` under radioconda on port 5000 through `start_scheduler.sh`, detached, unless one is already serving. Its console output goes to `/tmp/srt-scheduler-console.log`; the operational record is `scheduler.log` as always.
2. Opens the live controller at `http://192.168.50.120/` in Firefox.

VS Code and Stellarium were taken out of the launcher on 2026-10-01. Opening the VS Code workspace by hand still runs the scheduler and PlatformIO Serial Monitor tasks (the monitor waits for `/dev/ttyACM0`, prevents duplicate monitors, and retries temporary exclusive-lock failures); Stellarium is started by hand and its telescope is configured at `192.168.50.120:10001`. Launcher diagnostics are written to `/tmp/srt-software-launcher.log`.

The scheduler start runs:

```bash
/home/astro/21-cm-radio-telescope-v2/receiver_scheduler/start_scheduler.sh
# -> h1_web_scheduler.py --host 127.0.0.1 --port 5000, only if nothing is already serving
```

For a manual terminal start:

```bash
conda activate radioconda
python h1_web_scheduler.py
```

Then open http://localhost:5000 in your browser.

The scheduler points at the current controller web UI by default: `http://192.168.50.120`. The AP fallback remains `http://192.168.4.1`.

### Web Interface Tabs

Ten tabs. Everything that produces data you keep is headless; the one
deliberately graphical path is the **Start receiver GUI (console only)** button
in the status bar, which opens the receiver's Qt window on the observatory
machine for warm-up and checking the band. It will fail over ssh, and that is
correct rather than broken.

| Tab | For |
|-----|-----|
| **Scheduler** | Booking observations, and what is running now |
| **Sun Scan** | Pointing calibration: a raster on the Sun, the model fit, and the calibration day that repeats it; *Start beam drift* runs the two-hour Sun drift that measures the beam |
| **Horizon** | Measuring the obstructed horizon, choosing which measured profile is in force, and exporting it to Stellarium |
| **RF calibration** | A **Progress** panel at the top for the job running; the two bandpass templates (measured live, with a *Go to Lockman Hole* button, or fitted from a recording on disk); the counts-to-kelvin gain, with suggested targets screened against the measured horizon; the pilot's status; and the **Clocks** card — the Thunderbolt (lock, disciplining, satellites and their signal levels, antenna position and cable delay), this computer's NTP, the controller's clock and its offset from this one, active warnings only, and an on-demand stability plot of the Thunderbolt's PPS record with the transition read off it |
| **Camera** | The safety camera watching the dish: snapshots, and live video at a chosen frame rate |
| **Simulator** | The sky simulator, served from the scheduler so the two share an origin; its *Schedule* button books (or starts) an observation |
| **Observe** | Running an observation now, and looking at any recording (see below) |
| **Configuration** | Site, controller, horizon, camera and receiver settings; the fixed instrument and the pilot, editable here only, with a warning; the Sun monitor and the pulsar monitor |
| **Log** | The operational record |
| **Guide** | How to operate the telescope, and the astronomy behind it |

Only one of the Sun scan, calibration day, horizon scan, RF calibration, a
scheduled observation, or a hand-started receiver may hold the B200 and the
mount at once; whichever is asked for second is refused with the reason.

#### Observe Tab

Runs an observation immediately (*Start Now*), or hands one to the schedule
form (*Send to Scheduler*). Four types:

- **Spectrum (tracked)** — the dish follows a galactic direction
- **Drift scan** — the dish is parked and the source crosses the beam, transiting at the mid-point
- **Solar track (flux monitor)** — the dish follows the Sun, plotting flux in
  solar flux units live while the spectra record as usual. The plot bins the
  whole run rather than its tail and updates at the rate records appear. When
  the run ends the live trace comes down and the finished recording is plotted
  in its place.
- **Solar drift** — the dish parks where the Sun will be at the mid-point and
  the Sun drifts through the beam (a drift entry in the `object` frame)

Drift scans get the same live plot, in antenna temperature: the time axis is
the observation's own start-to-stop window, fixed from the moment it begins,
with the beam-crossing time marked — so the trace fills in from the left and a
source should peak on the dashed line. The plotted number is the band median,
i.e. the continuum level: it follows a broadband source through the beam but is
nearly blind to a narrow line, so an H I drift scan can show little here while
recording the line perfectly well. The first point lands roughly one
integration after the slew finishes — a 60 s average cannot exist sooner — so
a shorter integration per record gives faster feedback (the plot bins records
down anyway). Expect the trace to wander a few tenths of a kelvin beyond the
radiometer floor: band-integrated total power is limited by receiver gain and
atmosphere stability (measured 2.3×10⁻⁴ of T_sys per minute), not by thermal
noise, and an absolute offset of some kelvin means the gain calibration is
stale — it drifts ~0.3%/h with temperature through the evening, so calibrate
near the observation if absolute temperatures matter. A tracked spectrum gets
no live plot: its band power is meant to be flat, and an autoscaled trace of
it would only magnify noise into the appearance of structure.

Finished observations are plotted in kelvin when a gain calibration applies to
the tuning, and on an **LSR** velocity axis when the direction and epoch can be
worked out — H I is quoted in LSR everywhere, and the correction reaches
~30 km/s. Where the direction is unknown the axis stays topocentric and says so.
A solar track's recording is drawn as flux against UTC with the RSTN reference
in the subtitle, a drift scan as band power against time, and a pulsar
recording as its fold (profile at the predicted period, sub-integrations, the
stack of every night, PINT residuals).

Below the plot sit the recordings and the buttons that act on one:

- **Filter** — by category: spectra, drift scans, booked Sun, Sun monitor,
  pulsar, console. The default is everything but the Sun monitor; the choice is
  remembered in the browser. The newest 25 are listed, with *show all*; a live
  run is always listed.
- **Plot Result**, **Download file**, **View live recording** (re-plots the file
  being written every 30 s until the run ends).
- **Fit model** — the per-channel gain fit for a tracked spectrum, the
  total-power fit for a drift scan; not for a pulsar. *Apply as calibration*
  appears only for a fit that can be applied.
- **PRESTO fold** — pulsar recordings only: exports a `.fil` and folds it with
  prepfold at the topocentric period, as an independent check.
- **dB scale** — solar and drift plots only: 10 log10 of value over peak,
  floored at −30 dB, for sidelobes a percent of the peak.

Buttons that do not apply to the selected recording are greyed out.

#### Scheduler Tab
The main view for managing observations.

- **Add/Edit/Clone/Delete observations** with full parameter control
- **Automatic triggering** - observations start automatically at scheduled times
- **Late start recovery** - if the scheduler starts after a scheduled time, observations still within their window are started with remaining duration
- **Preemption** - if a new observation is due while another is running, the current one is stopped and the new one starts
- **Clash prevention** - overlapping observations cannot be saved; end times are calculated from start time + duration
- **Real-time status** - see running observation with countdown timer
- **Running item protection** - running observations cannot be edited, deleted, or disabled
- **Local and UTC time display** - schedule in local time, see both clocks
- **Auto-save** - changes are saved automatically
- **Manual start** - click play button to start any observation immediately
- **Receiver start/status** - manually start the B200 receiver for warm-up/testing and see whether the active receiver process is manual or observation-owned
- **Clone** - duplicate an observation's settings into a new item
- **Clear Past** - remove observations whose end time has passed
- **Import/Export** - save and load schedules as JSON
- **Audio notifications** - rising/falling tones when observations start/stop

#### Configuration Tab
Persistent settings saved to `scheduler_config.json`:

| Setting | Description |
|---------|-------------|
| Banner Name / Subtitle | Customise the page title and heading; a blank subtitle shows the beam and T_sys in force. *Help tips* (hover explanations) is per browser, not saved here |
| Controller URL | SRT telescope controller address, normally `http://192.168.50.120` (empty to disable) |
| Slew Timeout | Max seconds to wait for telescope to reach target (default: 300) |
| Position Tolerance | Degrees within which the telescope is considered on-target (default: 0.5) |
| Observer Latitude | Observer latitude in degrees (+N), synced from controller on startup |
| Observer Longitude | Observer longitude in degrees (+E), synced from controller on startup |
| Observer Elevation | Observer elevation in metres |
| Min Elevation | Minimum elevation for satellite pass filtering (default: 10°) |
| Safety Camera | Video device and capture resolution |
| Receiver Python Executable | Path to radioconda Python used for the scheduler-managed receiver |
| SDR | The radio an Observe-tab run uses (a schedule entry names its own) |
| Instrument | The fixed instrument — LO, sample rate, gain, H I band edges, H I and continuum channel counts — and the pilot (on/off, burst interval, duty cap, burst amplitude, TX gain). Empty means the default in `tuning.py` / `pilot.py`; the pilot is off by default. A warning above the boxes: a change leaves every later recording uncalibrated until the bandpass and gain are re-measured |
| Data Output Folder | Where observation HDF5 files are saved |
| Log Lines to Display | Number of log lines shown in the Log tab |
| Sun monitor | `sun_monitor`: track the Sun with 3 s records whenever nothing holds the hardware, no booking is near and the Sun clears the measured horizon (off by default) |
| Pulsar monitor | `pulsar_monitor`: record B0329+54 in pulsar mode daily, ahead of the Sun monitor (off by default). `pulsar_monitor_window` is `follow` (open when the pulsar comes out from behind the measured horizon; the default) or `clock` (open at `pulsar_monitor_start`, default 20:00 local); `pulsar_monitor_hours` is the window length (default 16) |
| Sound on Start/Stop | Enable/disable audio notifications |

Both monitors rank below bookings and hand starts, the pulsar monitor above
the Sun monitor (no Sun run is started into the pulsar window, and one still
going when the window opens is stopped). Bookings win: a run ends short of the
next one, and the pulsar monitor resumes afterwards if 30 min of its window are
left; the pieces of one window share a `pulsar_session` and are timed as one
TOA. Anything started by hand, or *Stop*, stops a monitor run and holds both
monitors off for 30 min. A goto from the controller's own page is not seen by
the scheduler. `/api/status` reports both.

Some settings are not on the tab and are set in `scheduler_config.json` (every
key must be one of `_DEFAULT_CONFIG`'s), among them
`srt_controller_fallback_urls` (mDNS and the WiFi AP, tried after the primary
URL), `thunderbolt_device` / `thunderbolt_baud`, and
the **pulsar band** — `pulsar_sample_rate_hz` (default 32 Msps) and
`pulsar_lo_hz` (default 1413 MHz), passed to the receiver as `H1_PULSAR_RATE` /
`H1_PULSAR_LO`. Pulsar mode needs no H I line and is not held to the fixed
instrument.

If the scheduler is launched under a different Python, it re-execs itself under the configured receiver Python when that interpreter exists. This keeps scheduled observations, manual receiver starts, Sun scans, and SDR imports on the same radioconda environment. A manually started receiver is stopped before a scheduled observation starts so the SDR is not shared by two processes.

Configuration changes take effect immediately without restarting.

#### Log Tab
Displays the last N lines of `scheduler.log` with auto-refresh (5 second interval, toggleable). The log file uses rotating storage (5 MB max, 3 backups).

### Observation Parameters

| Parameter | Description |
|-----------|-------------|
| Name | Descriptive name for the observation |
| Start Date/Time | When to start (local time, leave date empty for "today") |
| Duration | How long to observe (minutes); end time is calculated automatically |
| Coordinates | Target position — see Coordinate Systems below |
| Comment | Free text, stored as the recording's `comment` attribute |
| Instrument | **Shown, not set** — the fixed instrument (see below) |
| Integration Time | Seconds per record (does not apply to a pulsar entry) |
| SDR Type | B200, RTL-SDR, or Demo |
| Respect local horizon | Advisory check against the measured horizon; trims a scheduled entry |
| Home the mount first | Run the physical homing before pointing, recording the counters at the stops and any count drift |
| Pilot off for this entry | Leave the pilot transmitter off for this run (the pilot is off by default in any case) |
| When Done | Action after observation ends: Stay, Go Home (Alt 0°, Az 0°), or Stow (Alt 90°, Az 180°) |
| Filename | Output file (auto-generated if empty) |

**The tuning is not an observation parameter.** Since issue #27 the B200 records
with a **fixed instrument** — LO 1418.905752 MHz, 8 Msps, gain 20 dB — set once
in `tuning.py`, overridable only on the Configuration tab (with a warning). The
centre-frequency, bandwidth, gain and channels boxes are gone from the form.
Every recording carries **two products**: an H I sub-band (1419.006–1422.306 MHz,
845 channels of 3.91 kHz) under the usual dataset names, and a continuum product
(the whole 8 MHz, 1024 channels) beside it. Continuum measurements always exclude
the H I band.

### Coordinate Systems

The form's *Observation* dropdown groups them by what the dish does:

| System | Description | Tracking |
|--------|-------------|----------|
| RA/Dec (Equatorial J2000) | Right Ascension / Declination | Tracks as Earth rotates |
| Galactic (l, b) | Galactic longitude / latitude | Tracks as Earth rotates |
| Famous targets | The simulator's target list (galactic fields, M31, M33, HVCs, Lockman Hole, Cas A, Cyg A, Tau A, Sun, Moon), in track or drift mode. Saved as an ordinary galactic, drift or object entry | As saved |
| Sun or Moon (`object`) | Select Sun or Moon by name | Automatic ephemeris tracking |
| Satellite (TLE) | Two-Line Element set | SGP4 propagation at 1 Hz |
| Fixed alt/az (`altaz`) | Direct altitude/azimuth pointing — a drift scan | Fixed position |
| Drift Scan | RA/Dec, Galactic or Sun/Moon source + beam-crossing time | Fixed position |
| PSR B0329+54 (`pulsar`) | The one pulsar this dish can fold; records the pulsar band, not spectra. Plan on 6 h or more, homing first | Tracks the catalogue J2000 position |
| Calibration day (`calibration`) | Repeated Sun rasters from sunrise to sunset (grid, spacing, interval; optionally archive old scans and clear the controller's model first) | Per raster |
| Horizon scan (`horizon`) | The radiometric horizon strip scan (azimuth and altitude steps, azimuth range); about two hours, for a dark dry night | Per strip |

The ESP32 controller treats sky targets below 10° altitude as below the local observing horizon. The Galactic Plane shortcut uses a separate, higher acquisition floor (45° by default): it picks the point on the plane nearest the galactic centre that is currently that high, then follows it down to the 10° horizon.

### Drift Scans

A drift scan parks the dish at a fixed alt/az and lets Earth's rotation carry the source through the beam. Select "Drift Scan" in the coordinate system dropdown and enter:

1. The source, in RA/Dec (J2000), Galactic (l, b), or a solar-system object (Sun or Moon)
2. The **beam-crossing time T** (local) — when the source should be at beam centre
3. A **symmetric window ±W minutes** — recording runs from T−W to T+W

The start time and duration are derived automatically (start = T−W, duration = 2W). At start time the scheduler computes the source's track around T, parks on the drive-grid point (0.5° encoder steps) the track passes closest to (`drift_park.py`, which reproduces the controller's pointing transform), sends that point to `/direct`, and starts the receiver with tracking off. The crossing time and the cross-drift miss are then known exactly and recorded. With `/pointing` unreadable it falls back to the source's alt/az at T and says so. The form previews the computed pointing live and warns if the source is below the horizon or in the azimuth dead zone (355–360°) at T; "Use Next Transit" fills T with the source's next meridian transit, the classical drift-scan geometry.

Because the pointing is recomputed for each day's beam-crossing time, an entry that repeats daily stays centred on the source with no sidereal bookkeeping. On a late start the geometry is preserved (T comes from the scheduled slot, not the actual start); only the front of the window is lost, along with the initial slew time as usual. The computed pointing, beam time, frame, and window are recorded in the HDF5 observation metadata (`drift_alt`, `drift_az`, `drift_beam_time`, `drift_frame`, `drift_window_min`).

### Satellite Tracking

The scheduler supports satellite tracking using Two-Line Element (TLE) sets:

1. Select "Satellite (TLE)" in the coordinate system dropdown
2. Enter a TLE by one of three methods:
   - **Search CelesTrak**: Type a satellite name or NORAD catalogue number and click "Fetch TLE"
   - **Paste**: Paste 2 or 3 TLE lines directly into the text area
   - **Load file**: Load a `.tle` or `.txt` file
3. Click "Compute Next Pass" to find the next pass above the minimum elevation
4. The start time, duration, and satellite name are auto-filled

During a satellite observation, a background thread uses PyEphem to propagate the TLE and sends `/direct?alt=X&az=Y` to the controller every second. When the satellite is below the horizon, no command is sent.

Observer location (latitude, longitude, elevation) and minimum pass elevation are set in the Configuration tab.

### Telescope Integration

When an SRT controller is configured, the scheduler:
1. Runs the physical homing first, if the entry asks for it, recording the counters at the stops
2. Sends the pointing/tracking command to the telescope
3. Waits for slewing to complete (polls `is_slewing` status)
4. For satellite observations, starts a background thread sending position updates at 1 Hz
5. Starts the SDR receiver
6. On completion: stops satellite tracking, sends home/stow command (if configured); a pulsar run is handed to `pulsar_toa.py` for its TOAs

There is no calibrator step: the old noise diode is gone (issue #39), and the
receiver's in-band reference is now the pilot (`pilot.py`, off by default).

The Configuration tab also exposes firmware update settings. The controller UI's **Update firmware** button asks the local scheduler to run PlatformIO in the configured environment (`wt32-eth01-ota` by default), which uploads to the controller over Ethernet OTA.

### Logging

All scheduler activity is logged to both the console (INFO level) and `scheduler.log` (DEBUG level):
- Observation start/stop events
- Telescope commands and slew status
- Preemption events
- Schedule loading and clash detection
- Errors with full tracebacks
- Firmware update progress from PlatformIO OTA

### Sun Scan Calibration

The Sun Scan tab runs `sun_scan.py` as a scheduler-owned pointing calibration workflow. It uses the configured observer location, controller URL, receiver backend, and cancellation state.

- Each measurement recomputes the Sun position immediately before slewing. Hardware scans recheck it after each slew and refine the command when motion took long enough for the Sun to move.
- Before a hardware scan, the scheduler resolves the live controller using the configured primary and fallback URLs. Every slew must be accepted and finish within the configured position tolerance; a controller error, telescope fault, stationary mount, wrong final position, or timeout stops immediately and is shown on the website with current and target coordinates.
- Calibration Day runs the Due's physical `HOME` sequence before every hardware raster, rather than trusting an ordinary commanded `Alt=0, Az=0` position. This re-establishes the mount reference at its physical limits. A rejected raster is re-homed and retried once before it counts as a failed scan.
- A B200 raster holds one SDR session for all points, explicitly selects `RX2`, and discards a short receiver warm-up capture. Starting the standalone receiver is blocked while Sun Scan or Calibration Day owns the B200.
- The website point counter advances only after the telescope has reached the requested grid point and a power measurement has completed; failed movement is never counted as scan data.
- Azimuth grid offsets are cross-elevation sky offsets; mount azimuth commands are expanded by `cos(altitude)`. A grid point outside the safe mount range stops the scan with a website error instead of recording a clipped, inaccurate point.
- Results include both `az_error_deg` (mount azimuth correction) and `az_error_sky_deg` (fitted sky/cross-elevation correction), plus mid-scan Sun position and scan start/end timestamps.
- Gaussian peaks must lie inside the measured grid and pass beam-width, uncertainty, and goodness-of-fit checks before a scan can enter calibration-day data.
- Cancelling a scan stops before fitting partial data.

Calibration Day repeatedly performs a complete N×N Sun scan on a start-to-start interval, saves each successful fit, and continues until sunset, cancellation, or three consecutive failures. The pointing model fits four terms — IE and IA (elevation and azimuth index), AN and AE (the two tilts) — plus CA (collimation) when the scans span enough altitude and AZSCALE when they span 90° of azimuth; any term can be held at a stored value, which is how a feed change is refitted (IE and CA only). It needs at least four scans; applying it needs 30° of Sun azimuth coverage, a condition number under 10⁴ and significant tilt terms. It uses only successful finite scans, weights them by their fit uncertainties, leaves out scans from behind the measured horizon, and reports parameter uncertainty, RMS residuals, coverage, and matrix conditioning in the web interface. A large difference between the displayed and physical mount position indicates lost encoder reference, mechanical slip, or a drive/power fault; run physical homing and correct the hardware problem rather than accepting a poor fit. **Apply to Telescope** POSTs the fitted terms as a document to the controller's `/pointing/apply` (`sun_scan.pointing_model_document`), which keeps it in its own NVS namespace; the observer position stays the true site and the operator's offset boxes are not touched. A controller rejection is reported.

## Configuration

Default parameters can be modified at the top of `b210_h1_receiver.py`:

```python
CENTER_FREQ = 1420.405e6    # Hydrogen line frequency (Hz)
FFT_SIZE = 4096             # FFT bins (frequency resolution)
INTEGRATION_TIME = 3.0      # Seconds between HDF5 saves
```

### Where recordings go, and what they are called

Every recording lands in `data/observations/`, named for when it was made and
what the mount was doing:

```
20260825_192937_drift.h5        the dish was parked; the sky drifted through the beam
20260825_201455_track.h5        the mount followed the source
20260825_143012_manual.h5       started at the console, nobody commanded the mount
20260926_203116_pulsar.h5       a pulsar entry: the fast total-power record, not spectra
```

and nothing else. The target name, the coordinates, the
tuning and the calibration in force are all attributes *inside* the file, so
repeating any of them in the name would only make a second copy free to
disagree with the first after a rename or an edit to the schedule entry.

`track` and `drift` describe the mount, not the box the entry was typed into:
an `altaz` entry is a **drift** scan, because the scheduler sends it to
`/direct` and leaves tracking off. The same word is stored as the
`observation_mode` attribute, so a renamed file still says what it was.

The convention lives in `observation_files.py`, shared by the scheduler and the
receiver. `H1_OUTPUT_FILE` still overrides it outright.

The scheduler passes the fixed instrument and the run's settings through the
environment:
- `H1_INSTRUMENT` — the fixed-instrument document (JSON); the receiver ignores
  `--sample-rate` and `--gain` in headless mode so a stale launcher cannot retune
  a scheduled run
- `H1_INTEGRATION_TIME`
- `H1_OUTPUT_FILE`
- `H1_OBS_METADATA` — the observation metadata above
- `H1_MODE=pulsar`, with `H1_PULSAR_RATE` / `H1_PULSAR_LO` from the pulsar
  band settings, for a pulsar entry

### Frequency Resolution

The frequency resolution is determined by:

```
Resolution = Sample Rate / FFT Size
```

| Sample Rate | FFT Size | Resolution |
|-------------|----------|------------|
| 2.048 MHz   | 4096     | 500 Hz     |
| 2.4 MHz     | 4096     | 586 Hz     |
| 5.0 MHz     | 4096     | 1.22 kHz   |
| 2.4 MHz     | 8192     | 293 Hz     |

For hydrogen line observations, ~500 Hz resolution is typically sufficient.

## Output Data Format

Data is saved to an HDF5 file with the following structure:

A recording carries **two products** (issue #27) and can be **read while it is
being written** (HDF5 SWMR): open it read-only, or in h5py with
`h5py.File(path, 'r', swmr=True)`. A change of spectral geometry — which can now
only happen on the console window, not a scheduled run — rolls to a new
timestamped file.

### Datasets

| Dataset | Shape | Type | Description |
|---------|-------|------|-------------|
| `frequency_hz` | (N_ch,) | float64 | H I sub-band axis in Hz (845 channels) |
| `spectra_kelvin` **or** `spectra_linear` | (N_rec, N_ch) | float32 | H I product; kelvin when calibrated, else counts |
| `frequency_hz_wide` | (N_wide,) | float64 | Continuum product axis (1024 channels over 8 MHz) |
| `spectra_wide_kelvin` **or** `spectra_wide_linear` | (N_rec, N_wide) | float32 | Continuum product |
| `bandpass_correction`, `bandpass_valid` (and `_wide`) | (N,) | float32 / bool | Per-channel correction applied at write time, and where the template speaks |
| `timestamps` | (N_rec,) | float64 | Unix time at the centre of each record (since 2026-08-27; the end before that) |
| `integration_times` | (N_rec,) | float32 | Actual integration per record |
| `overflows`, `underflows` | (N_rec,) | int32 | UHD overflows, and pilot TX underflows, during the record |

**The units are in the dataset name.** `spectra_kelvin` means the bandpass template
and gain in force applied to this tuning; `spectra_linear` means they did not and
the data are raw counts. Asking for the wrong one raises `KeyError`. Writing kelvin
is exactly reversible — the correction, gain and T_sys travel in the file — and
`observation_plot.read_observation` reverses it so the fitting code always gets counts.

### Attributes

| Attribute | Description |
|-----------|-------------|
| `sdr_type` | SDR used ('b210', 'rtlsdr', or 'demo') |
| `center_freq_hz`, `sample_rate_hz`, `gain_db` | The fixed instrument's LO, rate and gain |
| `instrument` | The whole fixed-instrument document (JSON) |
| `h1_band_hz`, `continuum_band_hz` | The two bands, `[low, high]` in Hz |
| `spectra_units`, `spectra_wide_units` | `K` or `counts`, per product |
| `applied_gain_counts_per_k`, `applied_t_sys_k` | The calibration applied when in kelvin |
| `bandpass_template`, `bandpass_template_wide`, `gain_calibration` | The calibration in force, as JSON, so a file is reducible off-machine |
| `beam_fwhm_deg`, `effective_area_m2`, `site_lat_deg/lon_deg/height_m` | Measured beam and surveyed site |
| `clock_source`, `clock_ref_locked` | Which 10 MHz the radio ran from, and whether it reported lock |
| `nominal_integration_time`, `created` | Target integration; ISO 8601 creation time |

When launched from the scheduler, additional observation metadata is included:

| Attribute | Description |
|-----------|-------------|
| `obs_name`, `comment` | Name, and the free text from the schedule form |
| `observation_mode` | `track`, `drift`, `pulsar` or `manual` — the word in the filename |
| `coord_system` | altaz, radec, galactic, object, drift, satellite, pulsar |
| `object_name` | Solar system object (sun, moon, jupiter) if applicable |
| `coord1_deg/min/sec`, `coord2_deg/min/sec` | Target coordinates |
| `sun_monitor`, `pulsar_monitor`, `pulsar_session` | Whether a monitor started the run, and the pulsar window it belongs to |
| `pointing_terms` | The pointing model in force (JSON), for reconstructing the commanded drive position |
| `reference_*` | The Thunderbolt's state at the start (`reference_state` `absent` when none is read) |
| `duration_minutes`, `start_date`, `start_time` | The scheduled slot |
| `drift_frame`, `drift_window_min`, `drift_beam_time` | Drift scans: frame, half-window W, planned crossing T |
| `drift_alt` / `drift_az` | The true alt/az commanded |
| `drift_drive_alt` / `drift_drive_az` | The drive-grid point parked on |
| `drift_crossing_time`, `drift_crossing_offset_deg` | When the source crosses the parked beam, and how far off centre |
| `homed_first`, `homing_count_error_alt_deg`, `homing_count_error_az_deg`, `homing_drift_alt_deg`, `homing_drift_az_deg` | Homing before the run: the raw first-approach counters (a fixed switch-detection offset, normally alt −1.0 / az −0.5, plus any drift; the name is historical) and the drift alone (0 when inside the normal range) |

### Pulsar recordings

A pulsar entry (`H1_MODE=pulsar`) records no spectra: the band power summed per
millisecond, by default one channel over 32 MHz at 1413 MHz (~14 MB an hour),
SWMR from creation. `H1_PULSAR_NCHAN=16` gives a 16-channel filterbank instead.

| Dataset | Shape | Description |
|---------|-------|-------------|
| `power` | (N, nchan) float32 | Band power per row, `dt_s` apart from `t0_unix` |
| `frequency_hz` | (nchan,) | Channel centres |
| `time_marks` | (M, 2) float64 | (row, radio time) at the first sample and after every overflow, from the `rx_time` tags; rows are fractional (`time_marks_exact = 1`) |
| `overflow_marks`, `underflow_marks` | (K, 2) int64 | (row, count) for each batch of overflows or TX underflows |

Attributes beside the usual radio, instrument, site and scheduler metadata:
`mode` = `pulsar`, `dt_s`, `nchan`, `channel_width_hz`, `t0_unix`,
`created_utc`; the catalogue numbers (`pulsar_name`, `pulsar_period_s`,
`pulsar_pdot`, `pulsar_pepoch_mjd`, `pulsar_dm`, ...); the time source
(`time_source` `pps` or `host`, `time_pps_verified`,
`time_device_minus_host_s`, `time_last_pps`); and the calibration in force at
the start (`cal_t_sys_k`, `cal_effective_area_m2`, `cal_t_sys_utc`,
`cal_beam_utc`, from 2026-09-30), so the fold's mK and mJy scales stay the
recording's own. The fold, the TOAs and the PRESTO export are in
`pulsar_fold.py`, `pulsar_toa.py` and `sigproc_export.py`; see
`../docs/PULSAR_PROCESSING.tex`.

### Reading Data in Python

Recordings are written in HDF5's single-writer / multiple-reader mode, so a
file **can be opened for reading while the receiver is still writing it** —
from the Observe tab's *View live recording*, or in h5py with
`h5py.File(path, 'r', swmr=True)` (a plain open still hits the lock while the
run is in progress; that is the protection, and the keyword is how a reader
says it knows the file is live). The on-disk format is HDF5 1.10
(`libver='latest'`); h5py, current MATLAB and the notebook read it.

A recording holds its spectra under **one of two names**, and the name is the
units. `spectra_kelvin` means the bandpass and gain in force were applied at
write time and the numbers are antenna temperature in K; `spectra_linear` means
no calibration applied to that tuning and the numbers are raw counts. Ask for
the wrong one and you get a `KeyError` — which is the point, and is why the
units are not merely an attribute you could forget to read.

```python
import h5py
import numpy as np
import matplotlib.pyplot as plt

with h5py.File('data/observations/20260825_192937_drift.h5', 'r') as hf:
    freq_mhz = hf['frequency_hz'][:] / 1e6
    timestamps = hf['timestamps'][:]

    # The dataset name is the units. Never guess.
    calibrated = 'spectra_kelvin' in hf
    spectra = hf['spectra_kelvin' if calibrated else 'spectra_linear'][:]
    units = 'K' if calibrated else 'counts'

    print(f"SDR: {hf.attrs['sdr_type']}")
    print(f"Mode: {hf.attrs.get('observation_mode', '?')}"
          f"  target: {hf.attrs.get('obs_name', '?')}")
    print(f"Center freq: {hf.attrs['center_freq_hz']/1e6:.3f} MHz")
    print(f"Sample rate: {hf.attrs['sample_rate_hz']/1e6:.3f} MHz")
    print(f"{len(timestamps)} spectra in {units}")

    # To go back to raw counts (the calibration travels with the file):
    if calibrated:
        raw = ((spectra + hf.attrs['applied_t_sys_k'])
               * hf.attrs['applied_gain_counts_per_k']
               * hf['bandpass_correction'][:])

plt.figure(figsize=(12, 6))
plt.plot(freq_mhz, np.mean(spectra, axis=0))
plt.axvline(x=1420.405, color='r', linestyle='--', label='H I line')
plt.xlabel('Frequency (MHz)')
plt.ylabel(f'Antenna temperature ({units})')
plt.legend(); plt.grid(True); plt.show()

plt.figure(figsize=(12, 8))
plt.imshow(spectra, aspect='auto',
           extent=[freq_mhz[0], freq_mhz[-1], len(spectra), 0], cmap='viridis')
plt.colorbar(label=f'Antenna temperature ({units})')
plt.xlabel('Frequency (MHz)'); plt.ylabel('Time (integration #)')
plt.show()
```

Uncalibrated counts still want `10*np.log10(...)` for display; kelvin does not —
a temperature is already linear in the thing you care about, and taking its
logarithm throws away the calibration you just gained.

See `../notebooks/read_h1_data.ipynb` for a more complete example with metadata display and zoomed H I views, and `../notebooks/solar_flux_scallop.ipynb` for a solar track reduced end to end.

## Troubleshooting

### SDR Not Found

**B200/B210:**
```bash
# Check if device is detected
uhd_find_devices
```
If not found:
- Ensure USB 3.0 connection (B200 and B210 require USB 3.0)
- Install/reinstall UHD drivers
- On Windows, check Device Manager for driver issues

**RTL-SDR:**
```bash
# Check if device is detected
rtl_test
```
If not found:
- On Windows, use Zadig to install WinUSB driver
- Ensure no other application is using the device

**Demo Mode:**
If no SDR is available, the receiver automatically falls back to demo mode with simulated data.

### Import Errors

If you get `ModuleNotFoundError`:
```bash
# Ensure Radioconda is activated
conda activate radioconda

# Verify packages are installed
python -c "from gnuradio import gr; print('GNU Radio OK')"
python -c "import osmosdr; print('osmosdr OK')"
```

### Scheduler Not Starting Observations

- Ensure you run the scheduler from the radioconda environment
- Check that observations are enabled (checkbox checked)
- Verify the scheduled time is in local time
- Watch the console output for status messages

### Overflow Errors

If you see "O" printed or overflow warnings:
- Reduce sample rate
- Close other CPU-intensive applications
- On laptops, ensure power is plugged in (performance mode)

### Poor Signal / No Hydrogen Line Visible

- Verify antenna is connected and pointed at a hydrogen-rich region (Milky Way)
- Increase gain
- Use an LNA at the antenna
- Increase integration time for more averaging
- Check for local RFI interference

### Qt/GUI Issues

If the GUI doesn't appear or crashes:
```bash
# Try setting Qt platform explicitly
export QT_QPA_PLATFORM=xcb  # Linux
set QT_QPA_PLATFORM=windows  # Windows
```

## Observation Tips

### Best Targets

The hydrogen line is strongest when observing:
- The galactic plane (Milky Way)
- Specific coordinates with known HI emissions

### Doppler Shift

The hydrogen line can be Doppler-shifted due to:
- Galactic rotation
- Motion of hydrogen clouds
- Earth's motion

The rest frequency is 1420.405751 MHz. Observed shifts indicate radial velocity:
```
v = c * (f_rest - f_observed) / f_rest
```

### Integration Time

Longer integration times improve signal-to-noise ratio:
- SNR improvement = sqrt(integration_time)
- 1 minute integration: ~8x SNR improvement over 1 second
- 1 hour integration: ~60x SNR improvement over 1 second

## License

This project is provided as-is for educational and amateur radio astronomy purposes.

## References

- [NRAO Introduction to Radio Astronomy](https://www.cv.nrao.edu/~sransom/web/xxx.html)
- [GNU Radio Wiki](https://wiki.gnuradio.org/)
- [Ettus Research UHD Documentation](https://files.ettus.com/manual/)
- [RTL-SDR Blog](https://www.rtl-sdr.com/)
