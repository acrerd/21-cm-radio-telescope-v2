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
| `h1_schedule.json` | Saved observation schedule (auto-generated) |

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

4. Install Flask for the web scheduler:
   ```bash
   pip install flask
   ```

### Included with Radioconda

- GNU Radio (gr-uhd, gr-osmosdr)
- PyQt5 / PyQtGraph
- NumPy
- h5py

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
                        Sample rate in Hz (default: 2.4e6)
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

```bash
conda activate radioconda
python h1_web_scheduler.py
```

Then open http://localhost:5000 in your browser.

### Scheduler Features

- **Add/Edit/Delete observations** with full parameter control
- **Automatic triggering** - observations start automatically at scheduled times
- **Real-time status** - see running observation with time remaining
- **Local and UTC time display** - schedule in local time, see both clocks
- **Auto-save** - changes are saved automatically
- **Manual start** - click play button to start any observation immediately
- **Import/Export** - save and load schedules as JSON

### Observation Parameters

| Parameter | Description |
|-----------|-------------|
| Name | Descriptive name for the observation |
| Start Date/Time | When to start (local time, leave date empty for "today") |
| Duration | How long to observe (minutes) |
| Coordinates | Target position (Alt/Az, RA/Dec, or Galactic) |
| Center Frequency | Observation frequency in MHz (default: 1420.405) |
| Bandwidth | Sample rate / observation bandwidth in MHz |
| Gain | RF gain in dB |
| Channels | FFT size (frequency resolution) |
| Integration Time | Seconds per averaged spectrum |
| SDR Type | B210, RTL-SDR, or Demo |
| Filename | Output file (auto-generated if empty) |

### Scheduler Console Output

The scheduler prints status to the console:
```
[Scheduler] 12:00:00 - 2 observations loaded
  - Morning Survey: 2026-03-14 06:00 (enabled=True)
  - Evening Deep: 2026-03-14 22:00 (enabled=True)
[12:00:05] Scheduled start: Morning Survey (diff=5.2s)
[12:00:05] Started: Morning Survey (ends at 13:00:05)
```

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

### Datasets

| Dataset | Shape | Type | Description |
|---------|-------|------|-------------|
| `frequency_hz` | (N_fft,) | float64 | Frequency axis in Hz |
| `spectra_db` | (N_spectra, N_fft) | float32 | Power spectra in dB |
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

### Reading Data in Python

```python
import h5py
import numpy as np
import matplotlib.pyplot as plt

# Open the data file
with h5py.File('h1_data.h5', 'r') as hf:
    # Read datasets
    freq_hz = hf['frequency_hz'][:]
    freq_mhz = freq_hz / 1e6
    spectra = hf['spectra_db'][:]
    timestamps = hf['timestamps'][:]

    # Print metadata
    print(f"SDR: {hf.attrs['sdr_type']}")
    print(f"Center freq: {hf.attrs['center_freq_hz']/1e6:.3f} MHz")
    print(f"Sample rate: {hf.attrs['sample_rate_hz']/1e6:.3f} MHz")
    print(f"Number of spectra: {len(timestamps)}")

# Plot average spectrum
avg_spectrum = np.mean(spectra, axis=0)
plt.figure(figsize=(12, 6))
plt.plot(freq_mhz, avg_spectrum)
plt.axvline(x=1420.405, color='r', linestyle='--', label='H1 Line')
plt.xlabel('Frequency (MHz)')
plt.ylabel('Power (dB)')
plt.title('Average Hydrogen Line Spectrum')
plt.legend()
plt.grid(True)
plt.show()

# Plot waterfall
plt.figure(figsize=(12, 8))
plt.imshow(spectra, aspect='auto',
           extent=[freq_mhz[0], freq_mhz[-1], len(spectra), 0],
           cmap='viridis', vmin=-70, vmax=-30)
plt.colorbar(label='Power (dB)')
plt.xlabel('Frequency (MHz)')
plt.ylabel('Time (integration #)')
plt.title('Hydrogen Line Waterfall')
plt.show()
```

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
