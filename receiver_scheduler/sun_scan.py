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
        "srt_controller_url": "http://192.168.0.149",
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
             timeout: int = 10) -> dict | None:
    """Call an SRT controller HTTP endpoint, return JSON or None."""
    url = f"{base_url}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        log.warning("SRT API error (%s): %s", endpoint, exc)
        return None


def _slew_to(base_url: str, alt: float, az: float,
             slew_timeout: int = 120) -> bool:
    """Command telescope to alt/az and wait until the slew is complete."""
    result = _srt_api(base_url, "/direct", {"alt": f"{alt:.3f}", "az": f"{az:.3f}"})
    if not (result and result.get("ok")):
        log.error("Slew command rejected: %s", result)
        return False

    time.sleep(2)  # let slew start
    t0 = time.time()
    while time.time() - t0 < slew_timeout:
        status = _srt_api(base_url, "/status")
        if status and not status.get("is_slewing", True):
            return True
        time.sleep(1)

    log.error("Slew timeout after %ds", slew_timeout)
    return False


# ---------------------------------------------------------------------------
# Sun position
# ---------------------------------------------------------------------------

def get_sun_altaz(lat: float, lon: float, elevation: float = 0) -> tuple[float, float]:
    """Return current (altitude, azimuth) of the sun in degrees."""
    if ephem is None:
        raise ImportError("PyEphem is required: pip install ephem")
    observer = ephem.Observer()
    observer.lat = str(lat)
    observer.lon = str(lon)
    observer.elevation = elevation
    observer.date = ephem.now()
    sun = ephem.Sun(observer)
    return math.degrees(float(sun.alt)), math.degrees(float(sun.az))


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
                  center_freq: float = 1420.405e6,
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
        alt_error_deg, az_error_deg : pointing correction
        amplitude, sigma, offset    : fitted Gaussian parameters
        beam_fwhm_deg               : fitted FWHM
        success                     : bool
    """
    if curve_fit is None:
        raise ImportError("scipy is required for Gaussian fitting: pip install scipy")

    sigma_guess = beam_fwhm_hint / (2 * math.sqrt(2 * math.log(2)))
    p0 = [np.max(power) - np.min(power), 0.0, 0.0, sigma_guess, np.min(power)]
    bounds_lo = [0, -90, -360, 0.1, -np.inf]
    bounds_hi = [np.inf, 90, 360, 30, np.inf]

    try:
        popt, pcov = curve_fit(_gaussian_2d, (alt_offsets, az_offsets), power,
                               p0=p0, bounds=(bounds_lo, bounds_hi), maxfev=5000)
        amplitude, alt0, az0, sigma, offset = popt
        fwhm = sigma * 2 * math.sqrt(2 * math.log(2))
        perr = np.sqrt(np.diag(pcov))
        return {
            "alt_error_deg": float(alt0),
            "az_error_deg": float(az0),
            "amplitude": float(amplitude),
            "sigma_deg": float(sigma),
            "offset": float(offset),
            "beam_fwhm_deg": float(fwhm),
            "fit_errors": {
                "alt_err": float(perr[1]),
                "az_err": float(perr[2]),
                "sigma_err": float(perr[3]),
            },
            "success": True,
        }
    except Exception as exc:
        log.error("Gaussian fit failed: %s", exc)
        # Fall back: peak pixel
        idx = int(np.argmax(power))
        return {
            "alt_error_deg": float(alt_offsets[idx]),
            "az_error_deg": float(az_offsets[idx]),
            "amplitude": float(power[idx]),
            "sigma_deg": float(sigma_guess),
            "offset": float(np.min(power)),
            "beam_fwhm_deg": float(beam_fwhm_hint),
            "fit_errors": None,
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
    if fit_result["success"]:
        ax.plot(fit_result["az_error_deg"], fit_result["alt_error_deg"],
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
                         fit_result["az_error_deg"],
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
        ax2.plot(fit_result["az_error_deg"], fit_result["alt_error_deg"],
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
    center_freq: float = 1420.405e6,
    sample_rate: float = 2.4e6,
    gain: float = 40.0,
    output_image: str | None = "sun_scan.png",
    slew_timeout: int = 120,
    beam_fwhm_deg: float = 3.0,
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
    beam_fwhm_deg    : approximate beam FWHM for fit initial guess
    progress_callback: optional callable(point_index, total_points, info_dict)

    Returns
    -------
    dict with keys:
        alt_error_deg  : pointing error in altitude (degrees)
        az_error_deg   : pointing error in azimuth (degrees)
        sun_alt_deg    : assumed sun altitude
        sun_az_deg     : assumed sun azimuth
        beam_fwhm_deg  : fitted beam FWHM
        power_grid     : n x n array of measured powers
        fit            : full fit result dict
        image_path     : path to output image (or None)
        timestamp      : ISO-format UTC timestamp
    """
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

    # Serpentine (snake) scan order to minimise slew distance
    scan_order = []
    for row in range(n):
        cols = range(n) if row % 2 == 0 else range(n - 1, -1, -1)
        for col in cols:
            scan_order.append((row, col))

    total_points = n * n
    power_grid = np.full((n, n), np.nan)
    alt_off_flat = []
    az_off_flat = []
    power_flat = []

    cos_alt = math.cos(math.radians(sun_alt))
    if cos_alt < 0.01:
        raise ValueError("Sun is too close to zenith for an azimuth scan")

    log.info("Starting %dx%d sun scan (spacing=%.1f°, integration=%.1fs)",
             n, n, grid_spacing_deg, integration_time_s)

    # --- Scan loop ---
    for idx, (row, col) in enumerate(scan_order):
        if cancel_event is not None and cancel_event.is_set():
            log.warning("Sun scan cancelled by user at point %d/%d", idx, total_points)
            break

        dalt = ALT_OFF[row, col]
        daz_sky = AZ_OFF[row, col]  # cross-elevation offset

        # Convert sky offset to actual az command (account for cos(alt))
        cmd_alt = sun_alt + dalt
        cmd_az = sun_az + daz_sky / cos_alt

        # Enforce altitude limits
        cmd_alt = max(0.0, min(90.0, cmd_alt))

        point_info = {
            "point": idx + 1, "total": total_points,
            "row": row, "col": col,
            "dalt": dalt, "daz_sky": daz_sky,
            "cmd_alt": cmd_alt, "cmd_az": cmd_az,
        }
        log.info("Point %d/%d: offset (%.1f, %.1f)° -> Alt=%.2f° Az=%.2f°",
                 idx + 1, total_points, dalt, daz_sky, cmd_alt, cmd_az)

        if progress_callback:
            progress_callback(idx, total_points, point_info)

        # Slew
        if sdr_type != "demo":
            ok = _slew_to(srt_url, cmd_alt, cmd_az, slew_timeout)
            if not ok:
                log.warning("Slew failed for point %d — recording NaN", idx + 1)
                power_grid[row, col] = np.nan
                continue
            # Short settle time after slew
            time.sleep(0.5)

        # Measure power
        sim_state = None
        if sdr_type == "demo":
            sim_state = {
                "sun_alt": sun_alt, "sun_az": sun_az,
                "point_alt": cmd_alt, "point_az": cmd_az,
                "beam_fwhm": beam_fwhm_deg,
                "peak_power": 10.0, "background": 1.0, "noise_rms": 0.15,
            }

        pwr = measure_power(sdr_type, center_freq, sample_rate, gain,
                            integration_time_s, _sim_state=sim_state)
        power_grid[row, col] = pwr
        alt_off_flat.append(dalt)
        az_off_flat.append(daz_sky)
        power_flat.append(pwr)

        log.info("  Power = %.4f", pwr)

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

    log.info("Fit result: dAlt=%+.3f°  dAz=%+.3f°  FWHM=%.2f°  (success=%s)",
             fit["alt_error_deg"], fit["az_error_deg"],
             fit["beam_fwhm_deg"], fit["success"])

    # --- Generate image ---
    image_path = None
    if output_image and MATPLOTLIB_AVAILABLE:
        image_path = generate_image(ALT_OFF, AZ_OFF, power_grid, fit,
                                    output_path=output_image,
                                    sun_alt=sun_alt, sun_az=sun_az)

    return {
        "alt_error_deg": fit["alt_error_deg"],
        "az_error_deg": fit["az_error_deg"],
        "sun_alt_deg": sun_alt,
        "sun_az_deg": sun_az,
        "beam_fwhm_deg": fit["beam_fwhm_deg"],
        "power_grid": power_grid,
        "fit": fit,
        "image_path": image_path,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "grid_spacing_deg": grid_spacing_deg,
        "n": n,
        "integration_time_s": integration_time_s,
    }


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
    parser.add_argument("--freq", type=float, default=1420.405e6,
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
