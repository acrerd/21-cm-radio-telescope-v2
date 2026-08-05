#!/usr/bin/env python3
"""
Sun Scan — Pointing Calibration for SRT

Performs an nxn raster scan around the sun to determine telescope pointing errors.
Measures broadband power at each grid point, fits a 2D Gaussian to locate the
true sun position, and returns the pointing correction (true minus assumed).

The antenna beam is approximately 3 degrees wide, so the default grid spacing
of 1.5 degrees (half-beam) provides good Nyquist sampling of the beam pattern.

Usage as a function:
    from sun_scan import sun_scan
    result = sun_scan(n=5, integration_time_s=3.0)
    print(f"Pointing error: dAlt={result['alt_error_deg']:.2f}  dAz={result['az_error_deg']:.2f}")

Usage from command line:
    python sun_scan.py --n 5 --integration 3.0 --output sun_scan.png
"""

import json
import logging
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    import ephem
except ImportError:
    ephem = None

try:
    from scipy.optimize import curve_fit
except ImportError:
    curve_fit = None

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

log = logging.getLogger("sun_scan")

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_SCRIPT_DIR, "scheduler_config.json")


def _load_scheduler_config() -> dict:
    """Load observer location and SRT URL from the scheduler config file."""
    defaults = {
        "srt_controller_url": "http://192.168.106.120",
        "observer_lat": 55.902444,
        "observer_lon": -4.307861,
        "observer_elevation": 50,
        "slew_timeout": 300,
    }
    try:
        with open(_CONFIG_FILE) as f:
            cfg = json.load(f)
        for k, v in defaults.items():
            cfg.setdefault(k, v)
        return cfg
    except FileNotFoundError:
        return defaults


# ---------------------------------------------------------------------------
# SRT telescope control (reuses same HTTP API as the scheduler)
# ---------------------------------------------------------------------------

def _srt_api(base_url: str, endpoint: str, params: dict | None = None,
             timeout: int = 10) -> dict:
    """Call an SRT controller endpoint or raise a useful control error."""
    url = f"{base_url}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = resp.read().decode(errors="replace")
            return json.loads(payload, strict=False)
    except Exception as exc:
        raise RuntimeError(
            f"SRT controller request failed at {base_url}{endpoint}: {exc}") from exc


def _slew_to(base_url: str, alt: float, az: float,
             slew_timeout: int = 120, position_tolerance: float = 0.5,
             start_grace_s: float = 5.0) -> bool:
    """Command telescope to alt/az and verify that it reaches the target."""
    result = _srt_api(base_url, "/direct", {"alt": f"{alt:.3f}", "az": f"{az:.3f}"})
    if not result.get("ok"):
        detail = result.get("error") or result.get("message") or repr(result)
        raise RuntimeError(
            f"SRT controller rejected slew to Alt={alt:.2f} deg Az={az:.2f} deg: {detail}")

    t0 = time.time()
    while time.time() - t0 < slew_timeout:
        status = _srt_api(base_url, "/status")
        current_alt = status.get("alt")
        current_az = status.get("az")
        if status.get("fault_active"):
            detail = status.get("fault") or status.get("status") or "unknown fault"
            raise RuntimeError(
                f"Telescope fault while slewing to Alt={alt:.2f} deg Az={az:.2f} deg: {detail}")

        if current_alt is not None and current_az is not None:
            alt_error = abs(float(current_alt) - alt)
            az_error = abs(float(current_az) - az)
            at_target = max(alt_error, az_error) <= position_tolerance
            if at_target and not status.get("is_slewing", False):
                return True

        elapsed = time.time() - t0
        if (not status.get("is_slewing", False) and elapsed >= start_grace_s
                and current_alt is not None and current_az is not None):
            raise RuntimeError(
                "Telescope stopped before reaching the Sun-scan target: "
                f"current Alt={float(current_alt):.2f} deg Az={float(current_az):.2f} deg, "
                f"target Alt={alt:.2f} deg Az={az:.2f} deg, "
                f"status={status.get('status', 'unknown')}")
        time.sleep(1)

    raise RuntimeError(
        f"Telescope slew timed out after {slew_timeout}s while targeting "
        f"Alt={alt:.2f} deg Az={az:.2f} deg")


# ---------------------------------------------------------------------------
# Sun position
# ---------------------------------------------------------------------------

def _ephem_date(when: datetime | None = None):
    """Return a PyEphem date, treating aware datetimes as UTC."""
    if when is None:
        return ephem.now()
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return ephem.Date(when)


def get_sun_altaz(lat: float, lon: float, elevation: float = 0,
                  when: datetime | None = None) -> tuple[float, float]:
    """Return the sun (altitude, azimuth) in degrees for now or a UTC time."""
    if ephem is None:
        raise ImportError("PyEphem is required: pip install ephem")
    observer = ephem.Observer()
    observer.lat = str(lat)
    observer.lon = str(lon)
    observer.elevation = elevation
    observer.date = _ephem_date(when)
    sun = ephem.Sun(observer)
    return math.degrees(float(sun.alt)), math.degrees(float(sun.az))


def _clamp_azimuth_deg(az: float) -> float:
    """Clamp command azimuth to the SRT mount's usable 0..353 degree range."""
    return max(0.0, min(353.0, az))


def _sun_offset_to_command(sun_alt: float, sun_az: float,
                           dalt: float, daz_sky: float) -> tuple[float, float, float, bool]:
    """Convert sky offsets around the current Sun position into mount commands.

    ``daz_sky`` is a cross-elevation offset.  The mount azimuth command must be
    expanded by cos(alt), then clamped to the hardware-safe scan range.
    """
    cos_alt = math.cos(math.radians(sun_alt))
    if cos_alt < 0.01:
        raise ValueError("Sun is too close to zenith for an azimuth scan")

    cmd_alt = max(0.0, min(90.0, sun_alt + dalt))
    raw_az = sun_az + daz_sky / cos_alt
    cmd_az = _clamp_azimuth_deg(raw_az)
    return cmd_alt, cmd_az, cos_alt, (cmd_az != raw_az)


# ---------------------------------------------------------------------------
# Broadband power measurement
# ---------------------------------------------------------------------------

def _measure_power_uhd(center_freq: float, sample_rate: float,
                       gain: float, integration_time: float) -> float:
    """Measure total broadband power using UHD (Ettus B210)."""
    import uhd
    num_samps = int(sample_rate * integration_time)
    # recv_num_samps is a convenience method on MultiUSRP
    usrp = uhd.usrp.MultiUSRP()
    samples = usrp.recv_num_samps(num_samps, center_freq, sample_rate, [0], gain)
    return float(np.mean(np.abs(samples[0]) ** 2))


class _B210PowerMeter:
    """Keep one explicitly configured B210 session for a complete raster."""

    def __init__(self, center_freq: float, sample_rate: float, gain: float):
        import uhd

        self.center_freq = center_freq
        self.sample_rate = sample_rate
        self.gain = gain
        self.usrp = uhd.usrp.MultiUSRP()
        self.usrp.set_rx_antenna("RX2", 0)
        stream_args = uhd.usrp.StreamArgs("fc32", "sc16")
        stream_args.channels = [0]
        self.streamer = self.usrp.get_rx_stream(stream_args)

        # Tune and discard a short capture so LO/gain transients are not used as
        # the first grid measurement.
        warmup_samples = max(1, int(sample_rate * 0.25))
        self.usrp.recv_num_samps(
            warmup_samples, center_freq, sample_rate, [0], gain,
            streamer=self.streamer)
        log.info("B210 ready on RX2: %.6f MHz, %.3f Msps, %.1f dB",
                 center_freq / 1e6, sample_rate / 1e6, gain)

    def measure(self, integration_time: float) -> float:
        num_samps = int(self.sample_rate * integration_time)
        samples = self.usrp.recv_num_samps(
            num_samps, self.center_freq, self.sample_rate, [0], self.gain,
            streamer=self.streamer)
        return float(np.mean(np.abs(samples[0]) ** 2))

    def close(self):
        self.streamer = None
        self.usrp = None

    def __del__(self):
        self.close()


def _measure_power_rtlsdr(center_freq: float, sample_rate: float,
                          gain: float, integration_time: float) -> float:
    """Measure total broadband power using RTL-SDR."""
    from rtlsdr import RtlSdr
    sdr = RtlSdr()
    try:
        sdr.sample_rate = sample_rate
        sdr.center_freq = center_freq
        sdr.gain = gain
        num_samps = int(sample_rate * integration_time)
        # read_samples returns complex IQ
        samples = sdr.read_samples(num_samps)
        return float(np.mean(np.abs(samples) ** 2))
    finally:
        sdr.close()


def _measure_power_demo(center_freq: float, sample_rate: float,
                        gain: float, integration_time: float,
                        _sim_state: dict | None = None) -> float:
    """Simulated power measurement for testing without hardware.

    If _sim_state is provided it must contain 'sun_alt', 'sun_az', 'point_alt',
    'point_az', and optionally 'beam_fwhm' and 'peak_power'.
    """
    if _sim_state is None:
        return float(np.random.normal(1.0, 0.05))

    sun_alt = _sim_state["sun_alt"]
    sun_az = _sim_state["sun_az"]
    pt_alt = _sim_state["point_alt"]
    pt_az = _sim_state["point_az"]
    beam_fwhm = _sim_state.get("beam_fwhm", 3.0)
    peak = _sim_state.get("peak_power", 10.0)
    background = _sim_state.get("background", 1.0)
    noise_rms = _sim_state.get("noise_rms", 0.15)

    # Angular distance on the sky (cross-elevation corrected)
    cos_alt = math.cos(math.radians(sun_alt))
    dalt = pt_alt - sun_alt
    daz_sky = (pt_az - sun_az) * cos_alt

    sigma = beam_fwhm / (2 * math.sqrt(2 * math.log(2)))  # FWHM -> sigma
    r2 = dalt ** 2 + daz_sky ** 2
    power = peak * math.exp(-r2 / (2 * sigma ** 2)) + background
    power += np.random.normal(0, noise_rms)
    return float(max(power, 0.0))


def measure_power(sdr_type: str = "b210",
                  center_freq: float = 1420.405752e6,
                  sample_rate: float = 2.4e6,
                  gain: float = 40.0,
                  integration_time: float = 1.0,
                  _sim_state: dict | None = None) -> float:
    """Measure total broadband power.

    Tries the appropriate SDR backend.  Use sdr_type='demo' for testing.
    """
    if sdr_type == "demo":
        return _measure_power_demo(center_freq, sample_rate, gain,
                                   integration_time, _sim_state)
    if sdr_type == "b210":
        try:
            return _measure_power_uhd(center_freq, sample_rate, gain, integration_time)
        except Exception as exc:
            log.warning("UHD measurement failed (%s), trying GNU Radio...", exc)
            # fall through to GNU Radio attempt below

    if sdr_type == "rtlsdr":
        return _measure_power_rtlsdr(center_freq, sample_rate, gain, integration_time)

    raise RuntimeError(f"No working SDR backend for sdr_type='{sdr_type}'")


# ---------------------------------------------------------------------------
# 2-D Gaussian fitting
# ---------------------------------------------------------------------------

def _gaussian_2d(coords, amplitude, x0, y0, sigma, offset):
    """Circular 2-D Gaussian.  coords = (x_array, y_array)."""
    x, y = coords
    return amplitude * np.exp(-((x - x0) ** 2 + (y - y0) ** 2) / (2 * sigma ** 2)) + offset


def fit_pointing_error(alt_offsets: np.ndarray, az_offsets: np.ndarray,
                       power: np.ndarray, beam_fwhm_hint: float = 3.0
                       ) -> dict:
    """Fit a 2-D Gaussian to the measured power grid.

    Parameters
    ----------
    alt_offsets : 1-D array of altitude offsets (degrees) for each measurement
    az_offsets  : 1-D array of cross-elevation azimuth offsets (degrees)
    power       : 1-D array of measured power at each point
    beam_fwhm_hint : approximate beam width for initial guess

    Returns
    -------
    dict with keys:
        alt_error_deg               : altitude pointing correction
        az_error_deg/az_error_sky_deg : cross-elevation pointing correction
        amplitude, sigma, offset    : fitted Gaussian parameters
        beam_fwhm_deg               : fitted FWHM
        success                     : bool
    """
    if curve_fit is None:
        raise ImportError("scipy is required for Gaussian fitting: pip install scipy")

    alt_offsets = np.asarray(alt_offsets, dtype=float).ravel()
    az_offsets = np.asarray(az_offsets, dtype=float).ravel()
    power = np.asarray(power, dtype=float).ravel()
    if not (len(alt_offsets) == len(az_offsets) == len(power)):
        raise ValueError("Altitude, azimuth, and power arrays must have equal lengths")
    finite = np.isfinite(alt_offsets) & np.isfinite(az_offsets) & np.isfinite(power)
    alt_offsets = alt_offsets[finite]
    az_offsets = az_offsets[finite]
    power = power[finite]
    if len(power) < 5:
        raise ValueError(f"Need at least 5 finite measurements for a fit, have {len(power)}")
    if np.ptp(power) <= max(abs(float(np.mean(power))) * 1e-9, 1e-12):
        raise ValueError("Measured power has no usable variation; check the SDR and Sun visibility")

    alt_min, alt_max = float(np.min(alt_offsets)), float(np.max(alt_offsets))
    az_min, az_max = float(np.min(az_offsets)), float(np.max(az_offsets))
    alt_span = alt_max - alt_min
    az_span = az_max - az_min
    if alt_span <= 0 or az_span <= 0:
        raise ValueError("Scan must cover more than one altitude and azimuth offset")

    sigma_guess = beam_fwhm_hint / (2 * math.sqrt(2 * math.log(2)))
    peak_idx = int(np.argmax(power))
    sigma_max = max(beam_fwhm_hint * 4.0, alt_span, az_span)
    p0 = [np.max(power) - np.min(power), alt_offsets[peak_idx],
          az_offsets[peak_idx], sigma_guess, np.min(power)]
    # Constrain the peak to the area actually measured.  An unconstrained fit
    # can otherwise report a precise-looking correction far outside the grid.
    bounds_lo = [0, alt_min, az_min, 0.05, -np.inf]
    bounds_hi = [np.inf, alt_max, az_max, sigma_max, np.inf]

    try:
        popt, pcov = curve_fit(_gaussian_2d, (alt_offsets, az_offsets), power,
                               p0=p0, bounds=(bounds_lo, bounds_hi), maxfev=5000)
        amplitude, alt0, az0, sigma, offset = popt
        fwhm = sigma * 2 * math.sqrt(2 * math.log(2))
        perr = np.sqrt(np.diag(pcov))
        fitted = _gaussian_2d((alt_offsets, az_offsets), *popt)
        residual_rms = float(np.sqrt(np.mean((power - fitted) ** 2)))
        ss_res = float(np.sum((power - fitted) ** 2))
        ss_tot = float(np.sum((power - np.mean(power)) ** 2))
        r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("-inf")

        quality_errors = []
        if not np.all(np.isfinite(popt)):
            quality_errors.append("fit returned non-finite parameters")
        if not np.all(np.isfinite(perr)):
            quality_errors.append("fit uncertainty could not be estimated")
        if r_squared < 0.50:
            quality_errors.append(f"poor Gaussian agreement (R squared {r_squared:.2f})")
        # A peak pinned to an edge means the raster did not enclose the Sun and
        # the inferred correction is not safe to use in a pointing model.
        edge_tol = max(min(alt_span, az_span) * 1e-3, 1e-6)
        if (alt0 <= alt_min + edge_tol or alt0 >= alt_max - edge_tol or
                az0 <= az_min + edge_tol or az0 >= az_max - edge_tol):
            quality_errors.append("fitted peak lies on the scan boundary")
        min_fwhm = max(0.1, beam_fwhm_hint * 0.25)
        max_fwhm = min(beam_fwhm_hint * 4.0, 2.0 * max(alt_span, az_span))
        if not min_fwhm <= fwhm <= max_fwhm:
            quality_errors.append(
                f"fitted FWHM {fwhm:.2f} deg is outside {min_fwhm:.2f}..{max_fwhm:.2f} deg")

        return {
            "alt_error_deg": float(alt0),
            "az_error_deg": float(az0),
            "az_error_sky_deg": float(az0),
            "amplitude": float(amplitude),
            "sigma_deg": float(sigma),
            "offset": float(offset),
            "beam_fwhm_deg": float(fwhm),
            "fit_errors": {
                "alt_err": float(perr[1]),
                "az_err": float(perr[2]),
                "sigma_err": float(perr[3]),
            },
            "residual_rms": residual_rms,
            "r_squared": float(r_squared),
            "success": not quality_errors,
            "error": "; ".join(quality_errors) if quality_errors else None,
        }
    except Exception as exc:
        log.error("Gaussian fit failed: %s", exc)
        # Fall back: peak pixel
        idx = int(np.argmax(power))
        return {
            "alt_error_deg": float(alt_offsets[idx]),
            "az_error_deg": float(az_offsets[idx]),
            "az_error_sky_deg": float(az_offsets[idx]),
            "amplitude": float(power[idx]),
            "sigma_deg": float(sigma_guess),
            "offset": float(np.min(power)),
            "beam_fwhm_deg": float(beam_fwhm_hint),
            "fit_errors": None,
            "residual_rms": None,
            "r_squared": None,
            "error": f"Gaussian fit failed: {exc}",
            "success": False,
        }


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

def generate_image(alt_offsets_grid: np.ndarray, az_offsets_grid: np.ndarray,
                   power_grid: np.ndarray, fit_result: dict,
                   output_path: str = "sun_scan.png",
                   sun_alt: float | None = None, sun_az: float | None = None) -> str:
    """Create a publication-quality image of the sun scan.

    Parameters
    ----------
    alt_offsets_grid : 2-D array (n x n) of altitude offsets
    az_offsets_grid  : 2-D array (n x n) of cross-el azimuth offsets
    power_grid       : 2-D array (n x n) of measured power
    fit_result       : dict from fit_pointing_error()
    output_path      : where to save the image
    sun_alt, sun_az  : assumed sun position (for annotation)

    Returns the path to the saved image.
    """
    if not MATPLOTLIB_AVAILABLE:
        raise ImportError("matplotlib is required: pip install matplotlib")

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    # --- Left panel: measured data ---
    ax = axes[0]
    im = ax.pcolormesh(az_offsets_grid, alt_offsets_grid, power_grid,
                       shading="auto", cmap="inferno")
    ax.set_xlabel("Azimuth offset (°, cross-elevation)")
    ax.set_ylabel("Altitude offset (°)")
    ax.set_title("Measured broadband power")
    ax.set_aspect("equal")
    fig.colorbar(im, ax=ax, label="Power (linear)")
    # Mark assumed centre
    ax.plot(0, 0, "w+", markersize=12, markeredgewidth=2, label="Assumed position")
    # Mark fitted peak
    fit_az_sky = fit_result.get("az_error_sky_deg", fit_result["az_error_deg"])
    if fit_result["success"]:
        ax.plot(fit_az_sky, fit_result["alt_error_deg"],
                "cx", markersize=12, markeredgewidth=2, label="Fitted peak")
    ax.legend(loc="upper right", fontsize=8)

    # --- Right panel: fitted model ---
    ax2 = axes[1]
    n_fine = 80
    alt_fine = np.linspace(alt_offsets_grid.min(), alt_offsets_grid.max(), n_fine)
    az_fine = np.linspace(az_offsets_grid.min(), az_offsets_grid.max(), n_fine)
    AZ_F, ALT_F = np.meshgrid(az_fine, alt_fine)
    model = _gaussian_2d((ALT_F.ravel(), AZ_F.ravel()),
                         fit_result["amplitude"],
                         fit_result["alt_error_deg"],
                         fit_az_sky,
                         fit_result["sigma_deg"],
                         fit_result["offset"]).reshape(n_fine, n_fine)

    im2 = ax2.pcolormesh(AZ_F, ALT_F, model, shading="auto", cmap="inferno",
                         norm=Normalize(vmin=power_grid.min(), vmax=power_grid.max()))
    ax2.set_xlabel("Azimuth offset (°, cross-elevation)")
    ax2.set_ylabel("Altitude offset (°)")
    ax2.set_title("Gaussian fit")
    ax2.set_aspect("equal")
    fig.colorbar(im2, ax=ax2, label="Power (linear)")
    ax2.plot(0, 0, "w+", markersize=12, markeredgewidth=2)
    if fit_result["success"]:
        ax2.plot(fit_az_sky, fit_result["alt_error_deg"],
                 "cx", markersize=12, markeredgewidth=2)

    # Title with results
    title_parts = [f"Sun Scan — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"]
    if sun_alt is not None and sun_az is not None:
        title_parts.append(f"Sun at Alt={sun_alt:.1f}° Az={sun_az:.1f}°")
    title_parts.append(
        f"Pointing error: dAlt={fit_result['alt_error_deg']:+.2f}°  "
        f"dAz={fit_result['az_error_deg']:+.2f}°  "
        f"(FWHM={fit_result['beam_fwhm_deg']:.1f}°)"
    )
    if "az_error_sky_deg" in fit_result:
        title_parts[-1] += f"  sky-dAz={fit_result['az_error_sky_deg']:+.2f}°"
    fig.suptitle("\n".join(title_parts), fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.90])

    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info("Image saved to %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# Main scan function
# ---------------------------------------------------------------------------

def sun_scan(
    n: int = 5,
    grid_spacing_deg: float = 1.5,
    integration_time_s: float = 3.0,
    srt_url: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    elevation: float | None = None,
    sdr_type: str = "b210",
    center_freq: float = 1420.405752e6,
    sample_rate: float = 2.4e6,
    gain: float = 40.0,
    output_image: str | None = "sun_scan.png",
    slew_timeout: int = 120,
    position_tolerance: float = 0.5,
    beam_fwhm_deg: float = 3.0,
    backlash_deg: float = 2.0,
    progress_callback=None,
    cancel_event=None,
) -> dict:
    """Perform an nxn raster scan centred on the sun.

    Drives the telescope to each grid point, measures broadband power, fits
    a 2-D Gaussian, and returns the pointing error.

    Parameters
    ----------
    n                : grid size (n x n)
    grid_spacing_deg : spacing between grid points in degrees on the sky
    integration_time_s : broadband integration time per grid point
    srt_url          : ESP32 controller URL (default from scheduler config)
    lat, lon, elevation : observer location (default from scheduler config)
    sdr_type         : 'b210', 'rtlsdr', or 'demo'
    center_freq      : centre frequency in Hz
    sample_rate      : SDR sample rate in Hz
    gain             : SDR gain in dB
    output_image     : path for the output image (None to skip)
    slew_timeout     : max seconds to wait for each slew
    position_tolerance : maximum final Alt/Az error accepted after a slew
    beam_fwhm_deg    : approximate beam FWHM for fit initial guess
    progress_callback: optional callable(point_index, total_points, info_dict)

    Returns
    -------
    dict with keys:
        alt_error_deg  : pointing error in altitude (degrees)
        az_error_deg   : pointing error in mount azimuth degrees
        az_error_sky_deg : fitted cross-elevation azimuth error
        sun_alt_deg    : mid-scan sun altitude
        sun_az_deg     : mid-scan sun azimuth
        beam_fwhm_deg  : fitted beam FWHM
        power_grid     : n x n array of measured powers
        fit            : full fit result dict
        image_path     : path to output image (or None)
        timestamp      : ISO-format UTC timestamp
    """
    if n < 3 or n % 2 == 0:
        raise ValueError("Grid size must be an odd integer of at least 3")
    if grid_spacing_deg <= 0:
        raise ValueError("Grid spacing must be greater than zero")
    if integration_time_s <= 0 and sdr_type != "demo":
        raise ValueError("Integration time must be greater than zero")
    if beam_fwhm_deg <= 0:
        raise ValueError("Beam FWHM hint must be greater than zero")
    if sdr_type not in {"b210", "rtlsdr", "demo"}:
        raise ValueError(f"Unsupported SDR type: {sdr_type}")
    # --- Load defaults from scheduler config ---
    cfg = _load_scheduler_config()
    if srt_url is None:
        srt_url = cfg["srt_controller_url"]
    if lat is None:
        lat = cfg["observer_lat"]
    if lon is None:
        lon = cfg["observer_lon"]
    if elevation is None:
        elevation = cfg["observer_elevation"]
    if sdr_type != "demo" and not srt_url:
        raise ValueError("SRT controller URL is required for a hardware Sun scan")

    # --- Get sun position ---
    sun_alt, sun_az = get_sun_altaz(lat, lon, elevation)
    log.info("Sun position: Alt=%.2f° Az=%.2f°", sun_alt, sun_az)

    if sun_alt < 5.0:
        log.warning("Sun is very low (Alt=%.1f°). Results may be poor.", sun_alt)
    if sun_alt < 0:
        raise ValueError(f"Sun is below the horizon (Alt={sun_alt:.1f}°)")

    # --- Build grid of offsets (in sky degrees) ---
    # Offsets are centred on zero; the actual alt/az commands add the sun position.
    half = (n - 1) / 2.0
    offsets_1d = np.array([(i - half) * grid_spacing_deg for i in range(n)])
    # alt_offsets: rows, az_offsets: columns (cross-elevation)
    AZ_OFF, ALT_OFF = np.meshgrid(offsets_1d, offsets_1d)

    # All rows scan east to west (decreasing azimuth) for consistent backlash.
    # At the start of each row, an overshoot point east of the first
    # measurement removes backlash from both axes before data is taken.
    scan_order = []
    row_starts = []  # indices in scan_order where each new row begins
    for row in range(n):
        row_starts.append(len(scan_order))
        for col in range(n - 1, -1, -1):  # always east to west
            scan_order.append((row, col))

    total_points = n * n
    power_grid = np.full((n, n), np.nan)
    alt_off_flat = []
    az_off_flat = []
    power_flat = []

    b210_meter = None
    if sdr_type == "b210":
        b210_meter = _B210PowerMeter(center_freq, sample_rate, gain)

    log.info("Starting %dx%d sun scan (spacing=%.1f°, integration=%.1fs)",
             n, n, grid_spacing_deg, integration_time_s)

    scan_start_utc = datetime.now(timezone.utc)
    cancelled = False

    def command_for_current_sun(dalt_now: float, daz_sky_now: float,
                                allow_clamped: bool = False):
        current_sun_alt, current_sun_az = get_sun_altaz(lat, lon, elevation)
        if current_sun_alt < 0:
            raise RuntimeError(f"Sun set during scan (Alt={current_sun_alt:.1f}°)")

        cmd_alt_now, cmd_az_now, cos_alt_now, az_clamped = _sun_offset_to_command(
            current_sun_alt, current_sun_az, dalt_now, daz_sky_now)
        raw_alt = current_sun_alt + dalt_now
        alt_clamped = not math.isclose(cmd_alt_now, raw_alt, abs_tol=1e-9)
        if (az_clamped or alt_clamped) and not allow_clamped:
            raise RuntimeError(
                "Sun scan grid exceeds the safe mount range at "
                f"Sun Alt={current_sun_alt:.2f} deg Az={current_sun_az:.2f} deg "
                f"with offset dAlt={dalt_now:+.2f} deg dAz(sky)={daz_sky_now:+.2f} deg; "
                "reduce grid size/spacing or wait for the Sun to move")
        return current_sun_alt, current_sun_az, cmd_alt_now, cmd_az_now, cos_alt_now

    def slew_and_refine(dalt_now: float, daz_sky_now: float):
        """Slew to a moving-Sun offset, correcting ephemeris drift after slews."""
        last = None
        for attempt in range(3):
            last = command_for_current_sun(dalt_now, daz_sky_now)
            point_alt, point_az, cmd_alt_now, cmd_az_now, _ = last
            _slew_to(srt_url, cmd_alt_now, cmd_az_now, slew_timeout,
                     position_tolerance)

            updated = command_for_current_sun(dalt_now, daz_sky_now)
            drift_alt = abs(updated[2] - cmd_alt_now)
            drift_az = abs(updated[3] - cmd_az_now)
            last = updated
            if max(drift_alt, drift_az) <= 0.03:
                return last
            log.info("Sun moved during slew; refining target (dAlt=%.3f deg, dAz=%.3f deg)",
                     drift_alt, drift_az)
        raise RuntimeError(
            "Sun target kept moving beyond the refinement tolerance after 3 slews")

    # --- Scan loop ---
    # The finally guarantees the B210 session is released on every exit
    # path (slew fault, Sun set, clamp error, SDR error, cancellation) —
    # a claimed USRP would otherwise block all later scans and observations.
    try:
        for idx, (row, col) in enumerate(scan_order):
            if cancel_event is not None and cancel_event.is_set():
                log.warning("Sun scan cancelled by user at point %d/%d", idx, total_points)
                cancelled = True
                break

            dalt = ALT_OFF[row, col]
            daz_sky = AZ_OFF[row, col]  # cross-elevation offset

            # Backlash compensation: at the start of each row, slew to an
            # overshoot point east of the first measurement, then approach
            # westward so both axes settle with backlash taken up.
            if sdr_type != "demo" and backlash_deg > 0 and idx in row_starts:
                _, _, overshoot_alt, overshoot_az, _ = (
                    command_for_current_sun(dalt, daz_sky + backlash_deg,
                                            allow_clamped=True))
                log.info("Row %d: backlash overshoot to Az=%.2f° then approaching west",
                         row, overshoot_az)
                _slew_to(srt_url, overshoot_alt, overshoot_az, slew_timeout,
                         position_tolerance)

            # Recompute the Sun immediately before the measurement slew.  A row-start
            # overshoot can take long enough for the Sun to move appreciably.
            point_sun_alt, point_sun_az, cmd_alt, cmd_az, _ = (
                command_for_current_sun(dalt, daz_sky))

            point_info = {
                "point": idx + 1, "total": total_points,
                "row": row, "col": col,
                "dalt": dalt, "daz_sky": daz_sky,
                "cmd_alt": cmd_alt, "cmd_az": cmd_az,
                "sun_alt": point_sun_alt, "sun_az": point_sun_az,
            }
            log.info("Point %d/%d: offset (%.1f, %.1f)° -> Alt=%.2f° Az=%.2f°",
                     idx + 1, total_points, dalt, daz_sky, cmd_alt, cmd_az)

            # Slew to measurement point
            if sdr_type != "demo":
                refined = slew_and_refine(dalt, daz_sky)
                point_sun_alt, point_sun_az, cmd_alt, cmd_az, _ = refined
                point_info.update(cmd_alt=cmd_alt, cmd_az=cmd_az,
                                  sun_alt=point_sun_alt, sun_az=point_sun_az)
                # Short settle time after slew
                time.sleep(0.5)

            # Measure power
            sim_state = None
            if sdr_type == "demo":
                sim_state = {
                    "sun_alt": point_sun_alt, "sun_az": point_sun_az,
                    "point_alt": cmd_alt, "point_az": cmd_az,
                    "beam_fwhm": beam_fwhm_deg,
                    "peak_power": 10.0, "background": 1.0, "noise_rms": 0.15,
                }

            if b210_meter is not None:
                pwr = b210_meter.measure(integration_time_s)
            else:
                pwr = measure_power(sdr_type, center_freq, sample_rate, gain,
                                    integration_time_s, _sim_state=sim_state)
            power_grid[row, col] = pwr
            alt_off_flat.append(dalt)
            az_off_flat.append(daz_sky)
            power_flat.append(pwr)

            log.info("  Power = %.4f", pwr)
            if progress_callback:
                progress_callback(idx, total_points, point_info)
    finally:
        if b210_meter is not None:
            b210_meter.close()
    scan_end_utc = datetime.now(timezone.utc)
    if cancelled:
        raise RuntimeError("Sun scan cancelled")

    mid_scan_utc = scan_start_utc + (scan_end_utc - scan_start_utc) / 2
    mid_sun_alt, mid_sun_az = get_sun_altaz(lat, lon, elevation, when=mid_scan_utc)
    mid_cos_alt = math.cos(math.radians(mid_sun_alt))
    if mid_cos_alt < 0.01:
        raise ValueError("Sun is too close to zenith for azimuth error conversion")

    # --- Fit Gaussian ---
    alt_off_arr = np.array(alt_off_flat)
    az_off_arr = np.array(az_off_flat)
    power_arr = np.array(power_flat)

    # Remove NaN measurements
    valid = ~np.isnan(power_arr)
    if valid.sum() < 5:
        raise RuntimeError("Too few valid measurements for a fit (%d)" % valid.sum())

    fit = fit_pointing_error(alt_off_arr[valid], az_off_arr[valid],
                             power_arr[valid], beam_fwhm_hint=beam_fwhm_deg)
    az_error_sky = fit.get("az_error_sky_deg", fit["az_error_deg"])
    fit["az_error_sky_deg"] = float(az_error_sky)
    fit["az_error_deg"] = float(az_error_sky / mid_cos_alt)
    if fit.get("fit_errors"):
        az_err_sky = fit["fit_errors"].get("az_err")
        if az_err_sky is not None:
            fit["fit_errors"]["az_err_sky"] = float(az_err_sky)
            fit["fit_errors"]["az_err"] = float(az_err_sky / mid_cos_alt)

    log.info("Fit result: dAlt=%+.3f°  dAz=%+.3f° mount (%+.3f° sky)  FWHM=%.2f°  (success=%s)",
             fit["alt_error_deg"], fit["az_error_deg"], fit["az_error_sky_deg"],
             fit["beam_fwhm_deg"], fit["success"])

    # --- Generate image ---
    image_path = None
    if output_image and MATPLOTLIB_AVAILABLE:
        image_path = generate_image(ALT_OFF, AZ_OFF, power_grid, fit,
                                    output_path=output_image,
                                    sun_alt=mid_sun_alt, sun_az=mid_sun_az)

    return {
        "alt_error_deg": fit["alt_error_deg"],
        "az_error_deg": fit["az_error_deg"],
        "az_error_sky_deg": fit["az_error_sky_deg"],
        "sun_alt_deg": mid_sun_alt,
        "sun_az_deg": mid_sun_az,
        "beam_fwhm_deg": fit["beam_fwhm_deg"],
        "power_grid": power_grid,
        "fit": fit,
        "image_path": image_path,
        "timestamp": mid_scan_utc.isoformat(),
        "scan_start_timestamp": scan_start_utc.isoformat(),
        "scan_end_timestamp": scan_end_utc.isoformat(),
        "grid_spacing_deg": grid_spacing_deg,
        "n": n,
        "integration_time_s": integration_time_s,
    }


# ---------------------------------------------------------------------------
# Pointing model — 4-parameter tilt fit from multiple sun scans
# ---------------------------------------------------------------------------
#
# Model:
#   ΔAlt = ΔAlt₀ + AN·cos(az) + AE·sin(az)
#   ΔAz  = ΔAz₀  + (AN·sin(az) − AE·cos(az))·tan(alt)
#
# where AN = north-south tilt ≈ latitude error
#       AE = east-west tilt  ≈ longitude error × cos(lat)
#       ΔAlt₀, ΔAz₀ = constant zero-point offsets
#
# The effective observer position is:
#   effective_lat = true_lat + AN
#   effective_lon = true_lon + AE / cos(true_lat)
# ---------------------------------------------------------------------------

_POINTING_DATA_FILE = os.path.join(_SCRIPT_DIR, "pointing_data.json")
_POINTING_MODEL_FILE = os.path.join(_SCRIPT_DIR, "pointing_model.json")


def save_scan_to_pointing_data(scan_result: dict):
    """Append a sun scan result to the pointing data file."""
    entry = {
        "timestamp": scan_result["timestamp"],
        "sun_alt_deg": scan_result["sun_alt_deg"],
        "sun_az_deg": scan_result["sun_az_deg"],
        "alt_error_deg": scan_result["alt_error_deg"],
        "az_error_deg": scan_result["az_error_deg"],
        "beam_fwhm_deg": scan_result["beam_fwhm_deg"],
        "fit_success": scan_result["fit"]["success"],
    }
    fit_errors = scan_result.get("fit", {}).get("fit_errors") or {}
    if fit_errors.get("alt_err") is not None:
        entry["alt_error_uncertainty_deg"] = fit_errors["alt_err"]
    if fit_errors.get("az_err") is not None:
        entry["az_error_uncertainty_deg"] = fit_errors["az_err"]
    if "az_error_sky_deg" in scan_result:
        entry["az_error_sky_deg"] = scan_result["az_error_sky_deg"]
    if "scan_start_timestamp" in scan_result:
        entry["scan_start_timestamp"] = scan_result["scan_start_timestamp"]
    if "scan_end_timestamp" in scan_result:
        entry["scan_end_timestamp"] = scan_result["scan_end_timestamp"]
    data = load_pointing_data()
    data.append(entry)
    with open(_POINTING_DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)
    log.info("Saved scan to pointing data (%d entries total)", len(data))


def load_pointing_data() -> list:
    """Load accumulated pointing data."""
    try:
        with open(_POINTING_DATA_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def clear_pointing_data():
    """Clear all accumulated pointing data."""
    with open(_POINTING_DATA_FILE, "w") as f:
        json.dump([], f)
    log.info("Pointing data cleared")


# The drive firmware counts position in encoder pulses at PULSES_PER_DEGREE = 2,
# so no commanded position is meaningful below half a degree.  The Gaussian
# centroid uncertainties from a single scan are an order of magnitude finer than
# that, so they are combined in quadrature with the quantisation error below.
_MOUNT_QUANTISATION_DEG = 0.5
_MOUNT_QUANTISATION_SIGMA_DEG = _MOUNT_QUANTISATION_DEG / math.sqrt(12.0)


def _pointing_model_matrix(alt_deg: np.ndarray, az_deg: np.ndarray) -> np.ndarray:
    """Build the design matrix for the 4-parameter pointing model.

    For N observations, returns a (2N x 4) matrix where the parameters are
    [ΔAlt₀, ΔAz₀, AN, AE].

    Rows 0..N-1 are the altitude equations, rows N..2N-1 are the azimuth equations.

    AN and AE are the north and east tilt of the mount's azimuth axis.  The
    coefficients are the derivatives of a source's alt/az with respect to that
    tilt, obtained by rotating the horizon frame; see
    test_pointing_model_matrix_matches_frame_rotation for the check against
    numerically rotated coordinates.  The azimuth signs are opposite to the
    altitude ones: tipping the axis north raises a source in the north and
    swings a source in the east clockwise.
    """
    az_rad = np.radians(az_deg)
    alt_rad = np.radians(alt_deg)
    n = len(alt_deg)
    A = np.zeros((2 * n, 4))

    # Altitude equations: ΔAlt = ΔAlt₀ + AN·cos(az) + AE·sin(az)
    A[:n, 0] = 1.0          # ΔAlt₀
    A[:n, 2] = np.cos(az_rad)  # AN
    A[:n, 3] = np.sin(az_rad)  # AE

    # Azimuth equations: ΔAz = ΔAz₀ + (AN·sin(az) − AE·cos(az))·tan(alt)
    tan_alt = np.tan(alt_rad)
    A[n:, 1] = 1.0                       # ΔAz₀
    A[n:, 2] = np.sin(az_rad) * tan_alt   # AN
    A[n:, 3] = -np.cos(az_rad) * tan_alt  # AE

    return A


def fit_pointing_model(data: list | None = None,
                       true_lat: float | None = None,
                       true_lon: float | None = None) -> dict:
    """Fit the 4-parameter pointing/tilt model to accumulated sun scan data.

    Parameters
    ----------
    data      : list of scan entries (default: load from file)
    true_lat  : observer latitude for effective lat/lon calculation
    true_lon  : observer longitude for effective lat/lon calculation

    Returns
    -------
    dict with keys:
        alt_offset_deg, az_offset_deg : constant zero-point offsets
        tilt_north_deg (AN)           : north-south tilt
        tilt_east_deg (AE)            : east-west tilt
        effective_lat, effective_lon  : corrected observer position (if true given)
        az_offset_command_deg         : azimuth offset to push to the controller
                                        alongside the effective position; differs
                                        from az_offset_deg by az_site_rotation_deg
        az_site_rotation_deg          : constant azimuth rotation introduced by
                                        the longitude shift, which is cancelled
                                        in az_offset_command_deg
        parameter_significance        : |value|/sigma for each parameter
        min_tilt_significance         : the weaker of the two tilt significances
        reduced_chi_squared           : residuals against the per-scan sigmas
        residuals_alt, residuals_az   : residual errors (degrees)
        rms_alt, rms_az               : RMS residuals
        n_scans                       : number of scans used
        success                       : the fit converged; it does NOT mean the
                                        model is good enough to apply - check
                                        the significance and chi-squared too
    """
    if data is None:
        data = load_pointing_data()

    # Filter to successful, finite scan fits.  Four parameters technically need
    # only two scans, but at least four well-spread scans are required to detect
    # poor geometry and produce meaningful uncertainty estimates.
    required = ("sun_alt_deg", "sun_az_deg", "alt_error_deg", "az_error_deg")
    good = []
    rejected = 0
    for entry in data:
        try:
            values = [float(entry[key]) for key in required]
        except (KeyError, TypeError, ValueError):
            rejected += 1
            continue
        if not entry.get("fit_success", True) or not np.all(np.isfinite(values)):
            rejected += 1
            continue
        if not 0.0 <= values[0] < 89.5:
            rejected += 1
            continue
        good.append(entry)

    if len(good) < 4:
        return {
            "success": False,
            "error": f"Need at least 4 valid successful scans, have {len(good)}",
            "n_scans": len(good),
            "n_rejected": rejected,
        }

    alt_sun = np.array([d["sun_alt_deg"] for d in good], dtype=float)
    az_sun = np.array([d["sun_az_deg"] for d in good], dtype=float)
    d_alt = np.array([d["alt_error_deg"] for d in good], dtype=float)
    d_az = np.array([d["az_error_deg"] for d in good], dtype=float)
    n = len(good)

    az_sorted = np.sort(np.mod(az_sun, 360.0))
    gaps = np.diff(np.concatenate([az_sorted, [az_sorted[0] + 360.0]]))
    az_coverage = float(360.0 - np.max(gaps))
    if az_coverage < 30.0:
        return {
            "success": False,
            "error": (f"Sun azimuth coverage is only {az_coverage:.1f} deg; "
                      "collect scans spanning at least 30 deg"),
            "n_scans": n,
            "n_rejected": rejected,
            "az_coverage_deg": az_coverage,
        }

    # Build design matrix and observation vector
    A = _pointing_model_matrix(alt_sun, az_sun)
    b = np.concatenate([d_alt, d_az])

    def scan_uncertainty(entry: dict, key: str) -> float:
        """Per-scan sigma, floored by what the mount can actually command.

        A tight Gaussian centroid does not mean the telescope was pointed that
        precisely, so the fit uncertainty is combined in quadrature with the
        encoder quantisation.  Without this the weights claim a precision the
        hardware cannot deliver and a single scan can dominate the solution.
        """
        try:
            value = float(entry.get(key, 1.0))
        except (TypeError, ValueError):
            value = 1.0
        if not math.isfinite(value) or value <= 0.0:
            value = 1.0
        return math.hypot(value, _MOUNT_QUANTISATION_SIGMA_DEG)

    alt_unc = np.array([
        scan_uncertainty(d, "alt_error_uncertainty_deg") for d in good
    ])
    az_unc = np.array([
        scan_uncertainty(d, "az_error_uncertainty_deg") for d in good
    ])
    uncertainties = np.concatenate([alt_unc, az_unc])
    if not np.all(np.isfinite(uncertainties)):
        uncertainties = np.ones(2 * n)
    weights = 1.0 / uncertainties
    A_weighted = A * weights[:, None]
    b_weighted = b * weights

    rank = int(np.linalg.matrix_rank(A_weighted))
    condition_number = float(np.linalg.cond(A_weighted))
    if rank < 4 or not math.isfinite(condition_number) or condition_number > 1e4:
        return {
            "success": False,
            "error": ("Calibration geometry cannot constrain all four parameters "
                      f"(rank={rank}, condition={condition_number:.1f}); "
                      "collect scans over a wider part of the day"),
            "n_scans": n,
            "n_rejected": rejected,
            "az_coverage_deg": az_coverage,
            "condition_number": condition_number,
        }

    # Weighted least-squares fit: higher-quality individual Gaussian fits carry
    # more information without allowing tiny uncertainties to become infinite.
    result = np.linalg.lstsq(A_weighted, b_weighted, rcond=None)
    x = result[0]  # [ΔAlt₀, ΔAz₀, AN, AE]

    alt_offset = float(x[0])
    az_offset = float(x[1])
    tilt_north = float(x[2])  # AN ≈ δlat
    tilt_east = float(x[3])   # AE ≈ δlon·cos(lat)

    # Residuals
    predicted = A @ x
    res_alt = d_alt - predicted[:n]
    res_az = d_az - predicted[n:]
    rms_alt = float(np.sqrt(np.mean(res_alt ** 2)))
    rms_az = float(np.sqrt(np.mean(res_az ** 2)))
    weighted_residual = (b - predicted) / uncertainties
    dof = max(2 * n - 4, 1)
    reduced_chi_squared = float(np.sum(weighted_residual ** 2) / dof)
    # Floor the chi-squared scale at 1: mount-quantisation error is partly
    # systematic, so mutually consistent scans give chi2_red << 1, and
    # scaling by it would shrink the parameter errors below the deliberate
    # per-scan quantisation floor — overstating the tilt significance that
    # gates weak calibrations.
    covariance = (np.linalg.pinv(A_weighted.T @ A_weighted) *
                  max(reduced_chi_squared, 1.0))
    parameter_errors = np.sqrt(np.maximum(np.diag(covariance), 0.0))

    def significance(value: float, error: float) -> float:
        """How many sigma a fitted parameter is from zero."""
        if not math.isfinite(error) or error <= 0.0:
            return float("inf") if value else 0.0
        return abs(value) / error

    significances = {
        "alt_offset": significance(alt_offset, parameter_errors[0]),
        "az_offset": significance(az_offset, parameter_errors[1]),
        "tilt_north": significance(tilt_north, parameter_errors[2]),
        "tilt_east": significance(tilt_east, parameter_errors[3]),
    }

    model = {
        "alt_offset_deg": alt_offset,
        "az_offset_deg": az_offset,
        "tilt_north_deg": tilt_north,
        "tilt_east_deg": tilt_east,
        "rms_alt_deg": rms_alt,
        "rms_az_deg": rms_az,
        "n_scans": n,
        "n_rejected": rejected,
        "az_coverage_deg": az_coverage,
        "condition_number": condition_number,
        "reduced_chi_squared": reduced_chi_squared,
        "parameter_errors_deg": {
            "alt_offset": float(parameter_errors[0]),
            "az_offset": float(parameter_errors[1]),
            "tilt_north": float(parameter_errors[2]),
            "tilt_east": float(parameter_errors[3]),
        },
        "parameter_significance": {k: float(v) for k, v in significances.items()},
        "min_tilt_significance": float(min(significances["tilt_north"],
                                           significances["tilt_east"])),
        "residuals_alt": res_alt.tolist(),
        "residuals_az": res_az.tolist(),
        "scan_azimuths": az_sun.tolist(),
        "scan_altitudes": alt_sun.tolist(),
        "measured_alt_errors": d_alt.tolist(),
        "measured_az_errors": d_az.tolist(),
        "success": True,
    }

    # Effective lat/lon.  Moving the observer north is a rotation about a purely
    # horizontal axis, so a latitude shift reproduces a north tilt exactly.
    if true_lat is not None:
        model["effective_lat"] = true_lat + tilt_north
    if true_lon is not None and true_lat is not None:
        cos_lat = math.cos(math.radians(true_lat))
        if abs(cos_lat) <= 0.01:
            return {"success": False,
                    "error": "Effective longitude is undefined near the geographic pole",
                    "n_scans": n}
        delta_lon = tilt_east / cos_lat
        model["effective_lon"] = true_lon + delta_lon

        # A longitude shift is a rotation about the Earth's polar axis, which
        # is only partly horizontal.  Its vertical component rotates azimuth by
        # delta_lon*sin(lat) at every altitude - a constant no axis tilt can
        # produce.  Cancel it in the constant azimuth offset that is pushed to
        # the controller, so the applied correction matches the fitted model.
        site_az_rotation = delta_lon * math.sin(math.radians(true_lat))
        model["az_site_rotation_deg"] = float(site_az_rotation)
        model["az_offset_command_deg"] = float(az_offset - site_az_rotation)

    return model


def save_pointing_model(model: dict):
    """Save fitted pointing model to file."""
    with open(_POINTING_MODEL_FILE, "w") as f:
        json.dump(model, f, indent=2)
    log.info("Pointing model saved")


def load_pointing_model() -> dict | None:
    """Load the last fitted pointing model."""
    try:
        with open(_POINTING_MODEL_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def generate_calibration_plot(model: dict, output_path: str = "calibration_day.png") -> str:
    """Generate a plot showing the calibration day results.

    Four panels:
    1. Measured vs modelled altitude errors over azimuth
    2. Measured vs modelled azimuth errors over azimuth
    3. Residuals
    4. Summary text
    """
    if not MATPLOTLIB_AVAILABLE:
        raise ImportError("matplotlib is required")

    az = np.array(model["scan_azimuths"])
    alt = np.array(model["scan_altitudes"])
    d_alt_meas = np.array(model["measured_alt_errors"])
    d_az_meas = np.array(model["measured_az_errors"])
    res_alt = np.array(model["residuals_alt"])
    res_az = np.array(model["residuals_az"])

    # Compute model predictions
    d_alt_model = d_alt_meas - res_alt
    d_az_model = d_az_meas - res_az

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    # Panel 1: altitude errors vs azimuth
    ax = axes[0, 0]
    ax.plot(az, d_alt_meas, "o", color="#00d4ff", label="Measured", markersize=6)
    az_fine = np.linspace(az.min(), az.max(), 200)
    alt_mean = np.mean(alt)
    A_fine = _pointing_model_matrix(
        np.full_like(az_fine, alt_mean), az_fine)
    x = np.array([model["alt_offset_deg"], model["az_offset_deg"],
                   model["tilt_north_deg"], model["tilt_east_deg"]])
    pred_fine = A_fine @ x
    ax.plot(az_fine, pred_fine[:len(az_fine)], "-", color="#ff6b6b",
            label="Model", linewidth=2)
    ax.set_xlabel("Sun azimuth (°)")
    ax.set_ylabel("Altitude error (°)")
    ax.set_title("Altitude pointing error")
    ax.legend()
    ax.grid(alpha=0.3)

    # Panel 2: azimuth errors vs azimuth
    ax = axes[0, 1]
    ax.plot(az, d_az_meas, "o", color="#00d4ff", label="Measured", markersize=6)
    # For azimuth model curve, need varying altitude too — use scatter style
    ax.plot(az, d_az_model, "s", color="#ff6b6b", label="Model", markersize=5,
            markerfacecolor="none", markeredgewidth=1.5)
    ax.set_xlabel("Sun azimuth (°)")
    ax.set_ylabel("Azimuth error (°)")
    ax.set_title("Azimuth pointing error")
    ax.legend()
    ax.grid(alpha=0.3)

    # Panel 3: residuals
    ax = axes[1, 0]
    ax.plot(az, res_alt, "o", color="#00ff88", label="Alt residual", markersize=5)
    ax.plot(az, res_az, "s", color="#ffaa00", label="Az residual", markersize=5)
    ax.axhline(0, color="#666", linewidth=0.5)
    ax.set_xlabel("Sun azimuth (°)")
    ax.set_ylabel("Residual (°)")
    ax.set_title("Residuals after model fit")
    ax.legend()
    ax.grid(alpha=0.3)

    # Panel 4: summary text
    ax = axes[1, 1]
    ax.axis("off")
    lines = [
        "Pointing Model Results",
        "",
        f"Scans used: {model['n_scans']}",
        f"Az coverage: {az.min():.0f}° — {az.max():.0f}°",
        "",
        f"Alt zero offset:  {model['alt_offset_deg']:+.3f}°",
        f"Az zero offset:   {model['az_offset_deg']:+.3f}°",
        f"N-S tilt (AN):    {model['tilt_north_deg']:+.3f}°",
        f"E-W tilt (AE):    {model['tilt_east_deg']:+.3f}°",
        "",
        f"RMS residual alt: {model['rms_alt_deg']:.3f}°",
        f"RMS residual az:  {model['rms_az_deg']:.3f}°",
    ]
    if "effective_lat" in model:
        lines.append("")
        lines.append(f"Effective lat: {model['effective_lat']:.6f}°")
        lines.append(f"Effective lon: {model['effective_lon']:.6f}°")
    ax.text(0.1, 0.95, "\n".join(lines), transform=ax.transAxes,
            fontsize=12, verticalalignment="top", fontfamily="monospace",
            color="#ccc", bbox=dict(facecolor="#0f0f23", edgecolor="#333",
                                    boxstyle="round,pad=0.5"))

    fig.suptitle("Pointing Calibration Day", fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(output_path, dpi=150, bbox_inches="tight",
                facecolor="#1a1a2e", edgecolor="none")
    plt.close(fig)
    log.info("Calibration plot saved to %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Sun raster scan for SRT pointing calibration")
    parser.add_argument("--n", type=int, default=5,
                        help="Grid size (n x n, default 5)")
    parser.add_argument("--spacing", type=float, default=1.5,
                        help="Grid spacing in degrees (default 1.5)")
    parser.add_argument("--integration", type=float, default=3.0,
                        help="Integration time per point in seconds (default 3.0)")
    parser.add_argument("--sdr", default="b210", choices=["b210", "rtlsdr", "demo"],
                        help="SDR type (default b210)")
    parser.add_argument("--freq", type=float, default=1420.405752e6,
                        help="Centre frequency in Hz")
    parser.add_argument("--gain", type=float, default=40.0,
                        help="SDR gain in dB")
    parser.add_argument("--output", default="sun_scan.png",
                        help="Output image path")
    parser.add_argument("--srt-url", default=None,
                        help="SRT controller URL (default from config)")
    parser.add_argument("--lat", type=float, default=None)
    parser.add_argument("--lon", type=float, default=None)
    parser.add_argument("--elevation", type=float, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(message)s")

    def progress(idx, total, info):
        print(f"  [{idx+1}/{total}] offset=({info['dalt']:+.1f}, {info['daz_sky']:+.1f})°",
              flush=True)

    result = sun_scan(
        n=args.n,
        grid_spacing_deg=args.spacing,
        integration_time_s=args.integration,
        srt_url=args.srt_url,
        lat=args.lat, lon=args.lon, elevation=args.elevation,
        sdr_type=args.sdr,
        center_freq=args.freq,
        gain=args.gain,
        output_image=args.output,
        progress_callback=progress,
    )

    print()
    print("=" * 60)
    print("SUN SCAN COMPLETE")
    print("=" * 60)
    print(f"  Sun position:   Alt={result['sun_alt_deg']:.2f}°  Az={result['sun_az_deg']:.2f}°")
    print(f"  Pointing error: dAlt={result['alt_error_deg']:+.3f}°  dAz={result['az_error_deg']:+.3f}°")
    print(f"  Beam FWHM:      {result['beam_fwhm_deg']:.2f}°")
    print(f"  Fit success:    {result['fit']['success']}")
    if result["image_path"]:
        print(f"  Image:          {result['image_path']}")
    print()
    print("To apply this correction, subtract these errors from your")
    print("drive offsets (or add them to your target coordinates).")


if __name__ == "__main__":
    main()
