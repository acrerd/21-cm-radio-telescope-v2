# Hydrogen Line (21cm) Receiver

A real-time spectrum analyzer for observing the hydrogen line at 1420.405 MHz using software-defined radio. Features a PyQtGraph GUI with live spectrum and waterfall displays, continuous data logging to HDF5, and a web-based observation scheduler.

## Overview

This receiver is designed for radio astronomy observations of neutral hydrogen (HI) emissions at 1420.405 MHz. It provides:

- Real-time spectrum display with integrated averaging
- Waterfall/spectrogram visualization
- Continuous data recording to HDF5 format
- Support for multiple SDR platforms (Ettus B210, RTL-SDR)
- Demo mode for testing without hardware
- Web-based scheduler for automated observations

## Files

| File | Description |
|------|-------------|
| `b210_h1_receiver.py` | Main receiver application with GUI |
| `h1_web_scheduler.py` | Web-based observation scheduler |
| `h1_schedule.json` | Saved observation schedule |
| `scheduler_config.json` | Local scheduler configuration (auto-generated, ignored) |
| `scheduler.log` | Rotating log file (auto-generated, ignored) |
| `read_h1_data.ipynb` | Jupyter notebook for reading/plotting HDF5 data |
| `requirements.txt` | Python package dependencies |
| `test_scheduler.py` | Unit tests for scheduler behavior |
| `test_sun_scan.py` | Unit tests for Sun scan geometry and cancellation |
| `pointing_data.json` | Saved Sun scan pointing corrections |
| `start_srt_software.sh` | Starts VS Code, Firefox, Stellarium, and optional window layout |
| `start_platformio_monitor.sh` | Single-instance serial monitor with device wait and lock retry handling |
| `SRT Software.code-workspace` | VS Code workspace with automatic scheduler and serial-monitor tasks |

## Hardware Requirements

### Supported SDRs

| SDR | Frequency Range | Sample Rate | Notes |
|-----|-----------------|-------------|-------|
| Ettus B210 | 70 MHz - 6 GHz | Up to 56 MHz | Recommended for best performance |
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

#### Ettus B210
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

# Run with Ettus B210 (default)
python b210_h1_receiver.py

# Run with RTL-SDR
python b210_h1_receiver.py --sdr rtlsdr

# Run in demo mode (no hardware required)
python b210_h1_receiver.py --sdr demo
```

### Command Line Options

```
usage: b210_h1_receiver.py [-h] [--sdr {b210,rtlsdr,demo}] [--gain GAIN]
                           [--sample-rate SAMPLE_RATE]

Hydrogen Line (21cm) Receiver

optional arguments:
  -h, --help            show this help message and exit
  --sdr, -s {b210,rtlsdr,demo}
                        SDR type (default: b210)
  --gain, -g GAIN       RF gain in dB (default: 40)
  --sample-rate, -r SAMPLE_RATE
                        Sample rate in Hz (default: 2.4e6 for B210, 2.048e6 for RTL-SDR)
```

### Examples

```bash
# B210 with higher sample rate for wider bandwidth
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

1. Opens the repository in Visual Studio Code.
2. Starts `h1_web_scheduler.py` under radioconda in a hidden VS Code task terminal on port 5000.
3. Starts and reveals the PlatformIO Serial Monitor for the `due` environment. The monitor waits for `/dev/ttyACM0`, prevents duplicate launcher-owned monitors, and retries temporary exclusive-lock failures.
4. Opens the live controller at `http://192.168.106.120/` in Firefox.
5. Starts Stellarium.

If `wmctrl` is installed, it waits for the application windows and then places VS Code across the bottom half, Stellarium at top left, and Firefox at top right when the window manager exposes them. Stellarium is started through XWayland for reliable placement. The confined Ubuntu Firefox snap uses its native session backend so it opens reliably; GNOME Wayland may prevent `wmctrl` from positioning that one window. Without `wmctrl`, all programs still start and the desktop chooses their positions. Launcher diagnostics are written to `/tmp/srt-software-launcher.log`.

The VS Code scheduler task runs:

```bash
/home/astro/radioconda/bin/python /home/astro/21-cm-radio-telescope-v2/receiver_scheduler/h1_web_scheduler.py --host 0.0.0.0 --port 5000
```

For a manual terminal start:

```bash
conda activate radioconda
python h1_web_scheduler.py
```

Then open http://localhost:5000 in your browser.

The scheduler points at the current controller web UI by default: `http://192.168.106.120`. The AP fallback remains `http://192.168.4.1`.

### Web Interface Tabs

The scheduler web interface has Scheduler, Sun Scan, Configuration, and Log tabs:

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
- **Receiver start/status** - manually start the B210 receiver for warm-up/testing and see whether the active receiver process is manual or observation-owned
- **Clone** - duplicate an observation's settings into a new item
- **Clear Past** - remove observations whose end time has passed
- **Import/Export** - save and load schedules as JSON
- **Audio notifications** - rising/falling tones when observations start/stop

#### Configuration Tab
Persistent settings saved to `scheduler_config.json`:

| Setting | Description |
|---------|-------------|
| Banner Name / Subtitle | Customise the page title and heading |
| Controller URL | SRT telescope controller address, normally `http://192.168.106.120` (empty to disable) |
| Controller Fallback URLs | Additional controller addresses tried after the primary URL |
| Slew Timeout | Max seconds to wait for telescope to reach target (default: 300) |
| Position Tolerance | Degrees within which the telescope is considered on-target (default: 0.5) |
| Observer Latitude | Observer latitude in degrees (+N), synced from controller on startup |
| Observer Longitude | Observer longitude in degrees (+E), synced from controller on startup |
| Observer Elevation | Observer elevation in metres |
| Min Elevation | Minimum elevation for satellite pass filtering (default: 10°) |
| Receiver Python Executable | Path to radioconda Python used for the scheduler-managed receiver |
| Data Output Folder | Where observation HDF5 files are saved |
| Log Lines to Display | Number of log lines shown in the Log tab |
| Sound on Start/Stop | Enable/disable audio notifications |

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
| Center Frequency | Observation frequency in MHz (default: 1420.405) |
| Bandwidth | Sample rate / observation bandwidth in MHz |
| Gain | RF gain in dB |
| Channels | FFT size (frequency resolution) |
| Integration Time | Seconds per averaged spectrum |
| SDR Type | B210, RTL-SDR, or Demo |
| Calibrator | Turn noise source on/off for this observation |
| When Done | Action after observation ends: Stay, Go Home (Alt 0°, Az 0°), or Stow (Alt 90°, Az 180°) |
| Filename | Output file (auto-generated if empty; `_cal` suffix added when calibrator is on) |

### Coordinate Systems

| System | Description | Tracking |
|--------|-------------|----------|
| Alt/Az (Horizontal) | Direct altitude/azimuth pointing | Fixed position |
| RA/Dec (Equatorial J2000) | Right Ascension / Declination | Tracks as Earth rotates |
| Galactic (l, b) | Galactic longitude / latitude | Tracks as Earth rotates |
| Drift Scan | RA/Dec or Galactic source + beam-crossing time | Fixed position |
| Solar System Object | Select Sun or Moon by name | Automatic ephemeris tracking |
| Satellite (TLE) | Two-Line Element set | SGP4 propagation at 1 Hz |

The ESP32 controller treats sky targets below 10° altitude as below the local observing horizon. The Galactic Bulge shortcut uses that same 10° clearance: if the bulge is lower, the controller chooses the nearest galactic-plane point at or above 10° instead.

### Drift Scans

A drift scan parks the dish at a fixed alt/az and lets Earth's rotation carry the source through the beam. Select "Drift Scan" in the coordinate system dropdown and enter:

1. The source coordinates, in RA/Dec (J2000) or Galactic (l, b)
2. The **beam-crossing time T** (local) — when the source should be at beam centre
3. A **symmetric window ±W minutes** — recording runs from T−W to T+W

The start time and duration are derived automatically (start = T−W, duration = 2W). At start time the scheduler computes the source's alt/az *at T* with PyEphem, commands `/direct` to that fixed pointing, and starts the receiver with tracking off. The form previews the computed pointing live and warns if the source is below the horizon or in the azimuth dead zone (355–360°) at T; "Use Next Transit" fills T with the source's next meridian transit, the classical drift-scan geometry.

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
1. Sends the pointing/tracking command to the telescope
2. Waits for slewing to complete (polls `is_slewing` status)
3. Sets the calibrator state (on/off)
4. For satellite observations, starts a background thread sending position updates at 1 Hz
5. Starts the SDR receiver
6. On completion: stops satellite tracking, turns off calibrator (if it was on), sends home/stow command (if configured)

The Configuration tab also exposes firmware update settings. The controller UI's **Update firmware** button asks the local scheduler to run PlatformIO in the configured environment (`wt32-eth01-ota` by default), which uploads to the controller over Ethernet OTA.

### Logging

All scheduler activity is logged to both the console (INFO level) and `scheduler.log` (DEBUG level):
- Observation start/stop events
- Telescope commands and slew status
- Calibrator state changes
- Preemption events
- Schedule loading and clash detection
- Errors with full tracebacks
- Firmware update progress from PlatformIO OTA

### Sun Scan Calibration

The Sun Scan tab runs `sun_scan.py` as a scheduler-owned pointing calibration workflow. It uses the configured observer location, controller URL, receiver backend, and cancellation state.

- Each measurement recomputes the Sun position immediately before slewing. Hardware scans recheck it after each slew and refine the command when motion took long enough for the Sun to move.
- Before a hardware scan, the scheduler resolves the live controller using the configured primary and fallback URLs. Every slew must be accepted and finish within the configured position tolerance; a controller error, telescope fault, stationary mount, wrong final position, or timeout stops immediately and is shown on the website with current and target coordinates.
- Calibration Day runs the Due's physical `HOME` sequence before every hardware raster, rather than trusting an ordinary commanded `Alt=0, Az=0` position. This re-establishes the mount reference at its physical limits. A rejected raster is re-homed and retried once before it counts as a failed scan.
- A B210 raster holds one SDR session for all points, explicitly selects `RX2`, and discards a short receiver warm-up capture. Starting the standalone receiver is blocked while Sun Scan or Calibration Day owns the B210.
- The website point counter advances only after the telescope has reached the requested grid point and a power measurement has completed; failed movement is never counted as scan data.
- Azimuth grid offsets are cross-elevation sky offsets; mount azimuth commands are expanded by `cos(altitude)`. A grid point outside the safe mount range stops the scan with a website error instead of recording a clipped, inaccurate point.
- Results include both `az_error_deg` (mount azimuth correction) and `az_error_sky_deg` (fitted sky/cross-elevation correction), plus mid-scan Sun position and scan start/end timestamps.
- Gaussian peaks must lie inside the measured grid and pass beam-width, uncertainty, and goodness-of-fit checks before a scan can enter calibration-day data.
- Cancelling a scan stops before fitting partial data.

Calibration Day repeatedly performs a complete N×N Sun scan on a start-to-start interval, saves each successful fit, and continues until sunset, cancellation, or three consecutive failures. Its four-parameter offset/tilt model uses only successful finite scans. It requires at least four scans and at least 30 degrees of Sun azimuth coverage, uses individual scan-fit uncertainties as weights, and reports parameter uncertainty, RMS residuals, coverage, and matrix conditioning in the web interface. A large difference between the displayed and physical mount position indicates lost encoder reference, mechanical slip, or a drive/power fault; run physical homing and correct the hardware problem rather than accepting a poor fit. **Apply to Telescope** sends the effective latitude/longitude and the constant altitude/azimuth offsets to the controller; partial controller/configuration failures are reported explicitly.

## Configuration

Default parameters can be modified at the top of `b210_h1_receiver.py`:

```python
CENTER_FREQ = 1420.405e6    # Hydrogen line frequency (Hz)
FFT_SIZE = 4096             # FFT bins (frequency resolution)
INTEGRATION_TIME = 3.0      # Seconds between HDF5 saves
OUTPUT_FILE = "h1_data.h5"  # Output filename
```

These can also be overridden via environment variables (used by the scheduler):
- `H1_CENTER_FREQ`
- `H1_FFT_SIZE`
- `H1_INTEGRATION_TIME`
- `H1_OUTPUT_FILE`

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

Each HDF5 file has one fixed spectral geometry. If the center frequency, sample rate, or FFT size changes during a receiver session, the active file is closed and a new timestamped segment is created.

### Datasets

| Dataset | Shape | Type | Description |
|---------|-------|------|-------------|
| `frequency_hz` | (N_fft,) | float64 | Frequency axis in Hz |
| `spectra_linear` | (N_spectra, N_fft) | float32 | Power spectra in linear units |
| `timestamps` | (N_spectra,) | float64 | Unix timestamps |
| `integration_times` | (N_spectra,) | float32 | Actual integration time per spectrum |

### Attributes

| Attribute | Description |
|-----------|-------------|
| `sdr_type` | SDR used ('b210', 'rtlsdr', or 'demo') |
| `center_freq_hz` | Center frequency in Hz |
| `sample_rate_hz` | Sample rate in Hz |
| `fft_size` | Number of FFT bins |
| `gain_db` | RF gain setting |
| `nominal_integration_time` | Target integration time |
| `created` | ISO 8601 timestamp of file creation |

When launched from the scheduler, additional observation metadata is included:

| Attribute | Description |
|-----------|-------------|
| `obs_name` | Observation name from the schedule |
| `coord_system` | Coordinate system used (altaz, radec, galactic, drift, object) |
| `object_name` | Solar system object name (sun, moon) if applicable |
| `coord1_deg/min/sec` | Target coordinate 1 (Alt, RA, or Galactic longitude) |
| `coord2_deg/min/sec` | Target coordinate 2 (Az, Dec, or Galactic latitude) |
| `calibrator` | 1 if calibrator noise source was on, 0 otherwise |
| `duration_minutes` | Scheduled observation duration |
| `start_date` | Scheduled start date (YYYY-MM-DD) |
| `start_time` | Scheduled start time (HH:MM) |
| `drift_frame` | Drift scans: source frame (radec or galactic) |
| `drift_window_min` | Drift scans: half-window W in minutes |
| `drift_beam_time` | Drift scans: beam-crossing time T (local) |
| `drift_alt` / `drift_az` | Drift scans: the fixed pointing commanded (degrees) |

### Reading Data in Python

```python
import h5py
import numpy as np
import matplotlib.pyplot as plt

# Open the data file
with h5py.File('h1_data.h5', 'r') as hf:
    # Read datasets (linear power, not dB)
    freq_hz = hf['frequency_hz'][:]
    freq_mhz = freq_hz / 1e6
    spectra = hf['spectra_linear'][:]
    timestamps = hf['timestamps'][:]

    # Print metadata
    print(f"SDR: {hf.attrs['sdr_type']}")
    print(f"Center freq: {hf.attrs['center_freq_hz']/1e6:.3f} MHz")
    print(f"Sample rate: {hf.attrs['sample_rate_hz']/1e6:.3f} MHz")
    print(f"Number of spectra: {len(timestamps)}")

# Average in linear power domain, then convert to dB for display
avg_spectrum_db = 10 * np.log10(np.mean(spectra, axis=0))
plt.figure(figsize=(12, 6))
plt.plot(freq_mhz, avg_spectrum_db)
plt.axvline(x=1420.405, color='r', linestyle='--', label='H1 Line')
plt.xlabel('Frequency (MHz)')
plt.ylabel('Power (dB)')
plt.title('Average Hydrogen Line Spectrum')
plt.legend()
plt.grid(True)
plt.show()

# Plot waterfall (convert to dB for display)
spectra_db = 10 * np.log10(np.maximum(spectra, 1e-30))
plt.figure(figsize=(12, 8))
plt.imshow(spectra_db, aspect='auto',
           extent=[freq_mhz[0], freq_mhz[-1], len(spectra_db), 0],
           cmap='viridis')
plt.colorbar(label='Power (dB)')
plt.xlabel('Frequency (MHz)')
plt.ylabel('Time (integration #)')
plt.title('Hydrogen Line Waterfall')
plt.show()
```

See `read_h1_data.ipynb` for a more complete example with metadata display and zoomed H I views.

## Troubleshooting

### SDR Not Found

**B210:**
```bash
# Check if device is detected
uhd_find_devices
```
If not found:
- Ensure USB 3.0 connection (B210 requires USB 3.0)
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
