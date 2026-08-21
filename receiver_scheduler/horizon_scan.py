#!/usr/bin/env python3
"""
Horizon Scan — map the obstructed horizon by radiometry.

The telescope is its own surveying instrument. Trees, roofline and the solar
dome tower are all near 290 K at 1420 MHz while cold sky is not, so lowering the
beam through the skyline produces a large step in total power - of order the
system temperature, not some marginal excess. Signal to noise is a non-issue:
at 2.4 MHz bandwidth a tenth of a second gives 0.2% precision on a step of
order 100%. Slewing is the entire cost, and the whole design follows from that.

Three different "horizons" get confused with each other, and this module
deliberately measures only the first and derives the rest:

  1. the geometric horizon - where the obstruction edge actually is;
  2. the clearance altitude - where ground pickup through the beam skirt has
     fallen below a stated fraction of the step, which is higher;
  3. whatever floor a particular activity needs, which is higher again (a Sun
     raster must clear the edge by its own half-extent as well as the beam's).

Storing (3) is what makes a number unreusable - it silently encodes the raster
size of the day it was measured. This module stores (1) and (2) per azimuth,
plus the raw cut behind them, and lets callers derive their own floors.

Method, per azimuth: step the beam down (or up) through the skyline recording
total power, then fit a step convolved with the beam,

    P(alt) = P_sky + C/2 * erfc((alt - edge) / (sigma * sqrt2))

The 50% crossing is the geometric edge, and by the symmetry of the convolution
that estimate does not depend on the beam width - which the same fit measures
independently, as a check. Each cut carries its own sky and ground levels, so a
receiver gain drift across a half-hour run cannot bias the edges; that is the
main reason for cutting azimuth by azimuth rather than rastering the sky and
normalising globally.

Usage:
    from horizon_scan import horizon_strip_scan
    profile = horizon_strip_scan(sdr_type="demo")

    python horizon_scan.py --sdr demo --output horizon.png
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timezone

import numpy as np

from sun_scan import (MATPLOTLIB_AVAILABLE, ScanCancelled, _cancellable_sleep,
                      _check_cancelled, _load_scheduler_config, _slew_to,
                      _srt_api, _style_dark, measure_power)

try:
    from scipy.special import erfc
except ImportError:                                  # pragma: no cover
    erfc = None

if MATPLOTLIB_AVAILABLE:                             # pragma: no cover
    import matplotlib.pyplot as plt

log = logging.getLogger("horizon_scan")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_HORIZON_PROFILE_FILE = os.path.join(_SCRIPT_DIR, "horizon_profile.json")

# Records carry the raw cut, not just the fitted edge. This project has already
# paid once for storing conclusions instead of measurements: the 23 archived
# version-1 sun scans are residuals against a model that is no longer knowable
# and cannot be refitted. If the clearance fraction changes, or the fit
# improves, raw cuts can be reprocessed without another night on the dish.
_HORIZON_RECORD_VERSION = 1

DEFAULT_AZ_STEP = 5.0
DEFAULT_BEAM_FWHM_DEG = 5.8       # as measured by the Sun scans








_SQRT2 = math.sqrt(2.0)
_FWHM_TO_SIGMA = 1.0 / (2.0 * math.sqrt(2.0 * math.log(2.0)))


# ---------------------------------------------------------------------------
# The step model
# ---------------------------------------------------------------------------

def horizon_step(alt, p_sky, contrast, edge, sigma):
    """Power against altitude for a straight edge seen through a Gaussian beam."""
    return p_sky + 0.5 * contrast * erfc((np.asarray(alt) - edge) / (sigma * _SQRT2))


# ---------------------------------------------------------------------------
# Synthetic horizon, for running the whole scan without hardware
# ---------------------------------------------------------------------------

_DEMO_HORIZON = [
    # (az_from, az_to, edge_alt) - a treeline in the east and a tall tower
    # in the north, so the demo exercises both estimators.
    (60.0, 140.0, 16.0),
    (340.0, 360.0, 34.0),
    (0.0, 20.0, 34.0),
]
_DEMO_BASE_EDGE = 2.0


def _demo_edge(az: float) -> float:
    for lo, hi, edge in _DEMO_HORIZON:
        if lo <= az % 360.0 <= hi:
            return edge
    return _DEMO_BASE_EDGE


# Seeded, so a demo scan is reproducible. An unseeded draw made the recovery
# test flaky, and a test that fails one run in three is worse than no test:
# it teaches you to re-run rather than to look.
_demo_rng = np.random.default_rng(20260821)


def _demo_power(alt: float, az: float, beam_fwhm_deg: float,
                rng=None) -> float:
    """Total power for the synthetic horizon, with the beam already convolved."""
    sigma = beam_fwhm_deg * _FWHM_TO_SIGMA
    p = horizon_step(alt, 1.0, 1.6, _demo_edge(az), sigma)
    return float(p + (rng or _demo_rng).normal(0.0, 0.004))


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

def _measure_at(base_url, alt, az, sdr_type, integration_time_s, center_freq,
                sample_rate, gain, beam_fwhm_deg, power_meter, cancel_event,
                position_tolerance, slew_timeout, settle_s: float = 0.0):
    """Point at one true alt/az and measure the power there.

    Returns the position the mount actually reached, in both frames. The true
    altitude is the controller's own driveToTrue of the encoder reading, which
    is the only frame in which a horizon means anything: store drive
    coordinates and the profile quietly absorbs the pointing model, then
    becomes wrong the next time the model is refitted.
    """
    if sdr_type == "demo":
        power = _demo_power(alt, az, beam_fwhm_deg)
        return {"true_alt": alt, "true_az": az, "drive_alt": alt,
                "drive_az": az, "power": power}

    drive_alt, drive_az = _slew_to(base_url, alt, az,
                                   slew_timeout=slew_timeout,
                                   position_tolerance=position_tolerance,
                                   cancel_event=cancel_event)
    # Let the structure stop moving before integrating. The wobble measured on
    # 2026-08-21 was small - under a third of a degree, against a 5.8 degree
    # beam - but the integration is now seconds rather than a fraction of one,
    # so there is nothing to be gained by starting it early.
    if settle_s > 0 and _cancellable_sleep(settle_s, cancel_event):
        raise ScanCancelled("Horizon scan cancelled while settling")
    status = _srt_api(base_url, "/status")
    if power_meter is not None:
        power = power_meter.measure(integration_time_s)
    else:
        power = measure_power(sdr_type=sdr_type, center_freq=center_freq,
                              sample_rate=sample_rate, gain=gain,
                              integration_time=integration_time_s)
    return {
        "true_alt": float(status.get("true_alt", drive_alt)),
        "true_az": float(status.get("true_az", drive_az)),
        "drive_alt": float(drive_alt),
        "drive_az": float(drive_az),
        "power": float(power),
    }


# ---------------------------------------------------------------------------
# Storage and use
# ---------------------------------------------------------------------------

def save_horizon_profile(profile: dict, path: str | None = None) -> str:
    path = path or _HORIZON_PROFILE_FILE
    with open(path, "w") as f:
        json.dump(profile, f, indent=2)
    log.info("Horizon profile saved to %s (%d azimuths)", path,
             profile.get("n_azimuths", 0))
    return path


def load_horizon_profile(path: str | None = None) -> dict | None:
    try:
        with open(path or _HORIZON_PROFILE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def stellarium_horizon_points(profile: dict, use: str = "clearance") -> list:
    """(azimuth, altitude) pairs for a Stellarium polygonal horizon.

    `use` selects which of the two measured quantities to draw:
      "clearance" - the altitude above which the sky is radiometrically clean,
                    which is what an observer planning a run needs to see;
      "edge"      - the geometric skyline, which is what the eye would see.

    Stellarium wants the list to begin at due north, so a point at azimuth 0 is
    interpolated across the gap between the last and first measured azimuths.
    """
    points = []
    for entry in profile.get("entries", []):
        fit = entry.get("fit") or {}
        if not fit.get("success"):
            continue
        value = (fit.get("alt_clear") if use == "clearance"
                 else fit.get("edge_reported_deg"))
        if value is None:
            continue
        points.append((float(entry["az_deg"]) % 360.0, float(value)))
    points.sort()
    if not points:
        return points
    last_az, last_alt = points[-1]
    first_az, first_alt = points[0]
    gap = (first_az + 360.0) - last_az
    fraction = ((360.0 - last_az) / gap) if gap else 0.0
    north = last_alt + fraction * (first_alt - last_alt)
    return [(0.0, north)] + points


def write_stellarium_landscape(profile: dict, output_dir: str,
                               use: str = "clearance",
                               name: str | None = None) -> str:
    """Write a Stellarium polygonal landscape describing the measured horizon.

    Polygonal rather than a panorama photograph: Stellarium fills the ground
    below a list of azimuth/altitude pairs, which is exactly the shape of this
    measurement. Nothing has to be rendered, interpolated into an image, or
    aligned by eye - the horizon Stellarium draws is the horizon the telescope
    measured, to the degree.

    Both frames agree: the profile is stored in true azimuth measured from
    north through east, which is what Stellarium's horizon list expects.
    """
    points = stellarium_horizon_points(profile, use)
    if not points:
        raise ValueError("profile has no usable azimuths")

    label = name or ("Acre Road (clean sky)" if use == "clearance"
                     else "Acre Road (skyline)")
    slug = "acreroad_" + ("clearance" if use == "clearance" else "skyline")
    os.makedirs(output_dir, exist_ok=True)
    horizon_file = f"horizon_{slug}.txt"

    measured = profile.get("finished_utc", "unknown date")
    what = ("the lowest altitude at which the sky reads clear"
            if use == "clearance" else "the lowest measured clear altitude")

    with open(os.path.join(output_dir, horizon_file), "w") as f:
        f.write("# Acre Road Observatory measured horizon\n")
        f.write(f"# Measured radiometrically at 1420 MHz by the telescope itself, {measured}\n")
        f.write(f"# Value plotted: {what}\n")
        f.write("# Azimuth (deg, from north through east)   Altitude (deg)\n")
        for az, alt in points:
            f.write(f"{az:8.2f} {alt:7.2f}\n")

    with open(os.path.join(output_dir, "landscape.ini"), "w") as f:
        f.write(f"""[landscape]
name = {label}
author = Measured by the SRT at Acre Road Observatory
description = Horizon of the Acre Road 21cm telescope, measured radiometrically \
by the telescope itself at 1420 MHz ({measured}). Trees, roofline and the two \
dome towers are all near 290 K at this frequency, so the skyline is found as a \
step in total power rather than by eye. Plotted here: {what}.
type = polygonal
polygonal_horizon_list = {horizon_file}
; A horizon with a vertex at exactly 0 or 180 degrees azimuth renders badly in
; Stellarium; the tiny rotation is the workaround its own bundled landscapes use.
polygonal_angle_rotatez = 0.00001
ground_color = .13,.17,.10
horizon_line_color = .45,.35,.15
minimal_brightness = 0.12

[location]
planet = Earth
latitude = {profile.get('site_lat', 55.902426):+.6f}
longitude = {profile.get('site_lon', -4.307865):+.6f}
altitude = 50
timezone = Europe/London
""")
    log.info("Stellarium landscape written to %s (%d azimuths, %s)",
             output_dir, len(points), use)
    return output_dir


def zip_stellarium_landscape(profile: dict, zip_path: str,
                             use: str = "clearance") -> str:
    """Package the landscape as the zip Stellarium installs from."""
    import tempfile
    import zipfile

    slug = "acreroad_" + ("clearance" if use == "clearance" else "skyline")
    with tempfile.TemporaryDirectory(prefix="srt-landscape-") as work:
        folder = os.path.join(work, slug)
        write_stellarium_landscape(profile, folder, use=use)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry in sorted(os.listdir(folder)):
                archive.write(os.path.join(folder, entry),
                              arcname=os.path.join(slug, entry))
    return zip_path


def profile_floors(profile: dict) -> list:
    """(azimuth, clearance altitude) pairs, sorted, for the usable entries."""
    floors = []
    for entry in profile.get("entries", []):
        fit = entry.get("fit") or {}
        if fit.get("success") and fit.get("alt_clear") is not None:
            floors.append((float(entry["az_deg"]) % 360.0,
                           float(fit["alt_clear"])))
    floors.sort()
    return floors


def horizon_floor(profile: dict, az_deg: float, margin_deg: float = 0.0) -> float:
    """Lowest clean altitude at this azimuth.

    Takes the higher of the two bracketing samples rather than interpolating.
    An obstruction narrower than the 5 degree sampling is more likely to be
    missed than double-counted, so between two measured azimuths the safe
    assumption is the worse of them.
    """
    floors = profile_floors(profile)
    if not floors:
        return 0.0
    az = float(az_deg) % 360.0
    azs = [a for a, _ in floors]
    before = max((i for i, a in enumerate(azs) if a <= az), default=len(azs) - 1)
    after = min((i for i, a in enumerate(azs) if a >= az), default=0)
    return max(floors[before][1], floors[after][1]) + margin_deg


def is_obstructed(profile: dict, alt_deg: float, az_deg: float,
                  margin_deg: float = 0.0) -> bool:
    """Is this true-frame sky position inside the obstructed horizon?"""
    return float(alt_deg) < horizon_floor(profile, az_deg, margin_deg)


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

def generate_horizon_plot(profile: dict,
                          output_path: str = "horizon_profile.png") -> str:
    """Three panels: the horizon, how the strips peeled away, and a summary."""
    if not MATPLOTLIB_AVAILABLE:
        raise ImportError("matplotlib is required")

    entries = profile.get("entries", [])
    if not entries:
        raise ValueError("horizon profile has no entries")

    az = np.array([e["az_deg"] for e in entries])
    clear = np.array([(e["fit"] or {}).get("alt_clear", np.nan)
                      for e in entries], dtype=float)
    blocked = np.array([(e["fit"] or {}).get("estimator") == "blocked_above_ceiling"
                        for e in entries])
    controls = set(profile.get("control_azimuths", []))

    fig = plt.figure(figsize=(14, 9))
    ax = fig.add_subplot(2, 2, (1, 2))
    ax.fill_between(az, 0, clear, color="#ff6b6b", alpha=0.18,
                    label="Obstructed")
    ax.step(az, clear, where="mid", color="#00d4ff", linewidth=1.8,
            label="Lowest clear altitude")
    if blocked.any():
        ax.plot(az[blocked], clear[blocked], "v", color="#ffaa00", markersize=8,
                label="Still blocked at the ceiling")
    if controls:
        mask = np.array([a in controls for a in az])
        if mask.any():
            ax.plot(az[mask], clear[mask], "o", color="#00ff88", markersize=7,
                    markerfacecolor="none", markeredgewidth=1.5,
                    label="Clear-sky reference azimuths")
    ax.set_xlabel("True azimuth (deg)")
    ax.set_ylabel("Altitude (deg)")
    ax.set_title("Obstructed horizon")
    ax.set_xlim(0, 360)
    ax.set_xticks(np.arange(0, 361, 30))
    ax.legend(loc="upper right", fontsize=9)

    # How the work peeled away, strip by strip. A scan in good health clears
    # most of the sky in the first strip or two and converges; one that clears
    # nothing for several strips is either looking at a tall horizon or has a
    # threshold set from a bad reference.
    ax = fig.add_subplot(2, 2, 3)
    strips = profile.get("strips", [])
    if strips:
        alts = [s["alt_deg"] for s in strips]
        ax.bar(alts, [s["n_measured"] for s in strips], width=3.0,
               color="#00d4ff", alpha=0.5, label="Measured")
        ax.bar(alts, [s["n_cleared"] for s in strips], width=3.0,
               color="#00ff88", alpha=0.9, label="Cleared")
        ax.set_xlabel("Strip altitude (deg)")
        ax.set_ylabel("Azimuths")
        ax.set_title("Azimuths remaining, strip by strip")
        ax.legend(fontsize=9)

    ax = fig.add_subplot(2, 2, 4)
    ax.axis("off")
    finite = clear[~np.isnan(clear)]
    references = profile.get("sky_references", [])
    lines = [
        "Horizon Scan (strips)",
        "",
        f"Azimuths: {len(entries)} at {profile.get('az_step_deg')}\u00b0 spacing",
        f"Altitude: {profile.get('alt_min_deg')}\u00b0 upwards "
        f"in {profile.get('alt_step_deg')}\u00b0 steps",
        f"Strips:   {len(strips)}",
        f"Duration: {profile.get('duration_s', 0) / 60:.0f} min",
        f"Complete: {profile.get('complete')}",
        "",
        f"Highest obstruction: {np.nanmax(clear):.0f}\u00b0 at az "
        f"{az[int(np.nanargmax(clear))]:.0f}\u00b0",
        f"Median clearance:    {np.median(finite):.0f}\u00b0",
        f"Blocked at ceiling:  {int(blocked.sum())}",
    ]
    if references:
        levels = [r["level"] for r in references]
        lines += ["",
                  "Sky reference (health check):",
                  f"  {len(references)} taken, {min(levels):.5g} to {max(levels):.5g}",
                  f"  drift {100 * (max(levels) - min(levels)) / min(levels):.1f}%"]
    if profile.get("sdr_type") == "demo":
        lines += ["", "SIMULATED - not the observatory horizon"]
    ax.text(0.05, 0.95, "\n".join(lines), transform=ax.transAxes, fontsize=11,
            verticalalignment="top", fontfamily="monospace", color="#ccc",
            bbox=dict(facecolor="#0f0f23", edgecolor="#333",
                      boxstyle="round,pad=0.5"))

    fig.suptitle("Local Horizon", fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    _style_dark(fig)
    fig.savefig(output_path, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    log.info("Horizon plot saved to %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Map the local horizon in strips")
    parser.add_argument("--az-start", type=float, default=5.0)
    parser.add_argument("--az-end", type=float, default=350.0)
    parser.add_argument("--az-step", type=float, default=DEFAULT_STRIP_AZ_STEP)
    parser.add_argument("--alt-start", type=float, default=DEFAULT_STRIP_ALT_START)
    parser.add_argument("--alt-step", type=float, default=DEFAULT_STRIP_ALT_STEP)
    parser.add_argument("--alt-max", type=float, default=DEFAULT_STRIP_ALT_MAX)
    parser.add_argument("--settle", type=float, default=DEFAULT_SETTLE_S)
    parser.add_argument("--integration", type=float,
                        default=DEFAULT_STRIP_INTEGRATION_S)
    parser.add_argument("--home-every", type=int, default=DEFAULT_HOME_EVERY_STRIPS)
    parser.add_argument("--sdr", default="b210", choices=["b210", "rtlsdr", "demo"])
    parser.add_argument("--srt-url", default=None)
    parser.add_argument("--output", default="horizon_profile.png")
    parser.add_argument("--save", action="store_true",
                        help="write the profile to horizon_profile.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(message)s")

    def progress(idx, total, info):
        print("  strip %d/%d  alt %4.1f  az %5.1f  %.6g  (%d pending)" % (
            idx + 1, total, info["alt"], info["az"], info["power"],
            info["pending"]), flush=True)

    profile = horizon_strip_scan(
        az_start=args.az_start, az_end=args.az_end, az_step=args.az_step,
        alt_start=args.alt_start, alt_step=args.alt_step, alt_max=args.alt_max,
        settle_s=args.settle, integration_time_s=args.integration,
        home_every_strips=args.home_every, sdr_type=args.sdr,
        srt_url=args.srt_url, progress_callback=progress)

    print()
    print("=" * 60)
    print("HORIZON SCAN COMPLETE" if profile["complete"]
          else "HORIZON SCAN INCOMPLETE - azimuths still blocked at the ceiling")
    print("=" * 60)
    print("  Azimuths: %d in %.1f min over %d strips"
          % (profile["n_azimuths"], profile["duration_s"] / 60,
             len(profile["strips"])))
    references = [r["level"] for r in profile["sky_references"]]
    if len(references) > 1:
        print("  Sky reference drift: %.1f%% across the run"
              % (100 * (max(references) - min(references)) / min(references)))
    if args.save:
        print("  Saved: %s" % save_horizon_profile(profile))
    if MATPLOTLIB_AVAILABLE:
        print("  Plot:  %s" % generate_horizon_plot(profile, args.output))




# ---------------------------------------------------------------------------
# Strip scan: constant altitude, sweeping azimuth
# ---------------------------------------------------------------------------
#
# The cut scan above moves the altitude axis thousands of times and repeatedly
# into its lower limit. On 2026-08-21 that axis lost encoder counts partway
# through a sweep: the reported position drifted from the real one, the tower
# faded from the beam, and every azimuth after it recorded sky while believing
# it was measuring the horizon. Nothing in the scan noticed.
#
# Strips invert the traversal. Altitude moves once per strip and azimuth does
# the rest, so the axis that failed is barely exercised and never driven to its
# limit. Three further defences follow from the shape:
#
#   - the mount is re-homed every couple of strips, which bounds any count loss
#     to one strip rather than letting it accumulate over a whole sweep;
#   - a sky reference is measured at high altitude at the start of every strip,
#     which is a running check on the instrument - had it been there on the
#     21st it would have shown the fault within twenty minutes;
#   - an azimuth found clear is dropped from every higher strip, because the
#     horizon is monotonic: sky at 10 degrees means sky at 15. Most of the sky
#     is resolved by the first two or three strips and the work peels away.
#
# What it costs is resolution. At 5 degree altitude steps there are about two
# samples across a 5.8 degree beam, far too few to fit an edge, so this reports
# the lowest altitude at which an azimuth reads clear rather than a fitted
# geometric edge. That is the operationally useful number, quantised to the
# altitude step, and it is what the exclusion logic actually consumes.

DEFAULT_STRIP_AZ_STEP = 5.0
DEFAULT_STRIP_ALT_STEP = 5.0
DEFAULT_STRIP_ALT_START = 5.0
DEFAULT_STRIP_ALT_MAX = 60.0
DEFAULT_SETTLE_S = 2.0
DEFAULT_STRIP_INTEGRATION_S = 2.0
DEFAULT_HOME_EVERY_STRIPS = 2

# Where the per-strip sky reference is taken. High enough to be clear of
# everything, and the same place every time so the series is comparable.
SKY_REFERENCE_ALT = 85.0
SKY_REFERENCE_AZ = 180.0

# How far above the strip's own clear-sky level a sample may sit and still count
# as clear, in units of the reference scatter. The metal-clad building showed
# 2.3% of sky, about ten sigma, so this keeps it flagged as blocked.
_CLEAR_SIGMA = 5.0

_STRIP_RECORD_VERSION = 2


def _home_and_wait(base_url: str, timeout: int = 300, cancel_event=None) -> dict:
    """Run the Due homing sequence and wait for Ready."""
    result = _srt_api(base_url, "/home")
    if not (result and result.get("ok")):
        raise RuntimeError(f"Controller rejected the homing command: {result}")
    log.info("Homing the mount to re-establish the encoder zero")
    started, started_at, last = False, time.time(), None
    while time.time() - started_at < timeout:
        _check_cancelled(cancel_event, "homing")
        status = _srt_api(base_url, "/status")
        last = status
        state = str(status.get("status", "")).strip().lower()
        if status.get("fault_active") or state == "fault":
            raise RuntimeError("Telescope fault during homing: %s"
                               % (status.get("fault") or state))
        if state == "homing":
            started = True
        elif started and state == "ready" and not status.get("is_slewing", False):
            log.info("Homing complete at drive alt %.2f az %.2f",
                     float(status.get("alt", 0.0)), float(status.get("az", 0.0)))
            return status
        if not started and time.time() - started_at >= 10:
            raise RuntimeError("Telescope did not begin homing; last status %s" % last)
        if _cancellable_sleep(0.5, cancel_event):
            raise ScanCancelled("Horizon scan cancelled during homing")
    raise RuntimeError(f"Homing timed out after {timeout}s; last status {last}")


def _sky_reference(base_url, sdr_type, integration_time_s, center_freq,
                   sample_rate, gain, beam_fwhm_deg, power_meter, cancel_event,
                   position_tolerance, slew_timeout, settle_s, samples=4):
    """Measure the clear sky at a fixed high position: level and scatter.

    Serves two purposes. Its scatter sets the threshold for deciding an azimuth
    is clear, measured rather than assumed. And its level, recorded once per
    strip, is a running health check on the whole chain - a collapse in it, or
    in the contrast it implies, is what a drifting mount or a failing signal
    path looks like from the outside.
    """
    values = []
    for _ in range(samples):
        point = _measure_at(base_url, SKY_REFERENCE_ALT, SKY_REFERENCE_AZ,
                            sdr_type, integration_time_s, center_freq,
                            sample_rate, gain, beam_fwhm_deg, power_meter,
                            cancel_event, position_tolerance, slew_timeout,
                            settle_s=settle_s)
        values.append(point["power"])
    array = np.asarray(values, dtype=float)
    return {
        "alt_deg": SKY_REFERENCE_ALT,
        "az_deg": SKY_REFERENCE_AZ,
        "level": float(array.mean()),
        "sigma": float(array.std(ddof=1)) if len(array) > 1 else 0.0,
        "samples": values,
        "utc": datetime.now(timezone.utc).isoformat(),
    }


def horizon_strip_scan(az_start: float = 5.0,
                       az_end: float = 350.0,
                       az_step: float = DEFAULT_STRIP_AZ_STEP,
                       alt_start: float = DEFAULT_STRIP_ALT_START,
                       alt_step: float = DEFAULT_STRIP_ALT_STEP,
                       alt_max: float = DEFAULT_STRIP_ALT_MAX,
                       settle_s: float = DEFAULT_SETTLE_S,
                       integration_time_s: float = DEFAULT_STRIP_INTEGRATION_S,
                       home_every_strips: int = DEFAULT_HOME_EVERY_STRIPS,
                       beam_fwhm_deg: float = DEFAULT_BEAM_FWHM_DEG,
                       sdr_type: str = "b210",
                       center_freq: float = 1420.405752e6,
                       sample_rate: float = 2.4e6,
                       gain: float = 40.0,
                       srt_url: str | None = None,
                       slew_timeout: int = 150,
                       position_tolerance: float = 0.5,
                       progress_callback=None,
                       cancel_event=None) -> dict:
    """Sweep azimuth at each of a series of altitudes, climbing until clear."""
    cfg = _load_scheduler_config()
    base_url = (srt_url or cfg.get("srt_controller_url", "")).rstrip("/")
    if sdr_type != "demo" and not base_url:
        raise RuntimeError("No SRT controller URL configured")

    azimuths = [float(a) for a in np.arange(az_start, az_end + 0.001, az_step)]
    altitudes = [float(a) for a in np.arange(alt_start, alt_max + 0.001, alt_step)]
    started = datetime.now(timezone.utc)

    power_meter = None
    if sdr_type == "b210":
        from sun_scan import _B210PowerMeter
        power_meter = _B210PowerMeter(center_freq, sample_rate, gain)

    pending = list(azimuths)
    control_azimuths: list = []
    clearance: dict = {}
    columns: dict = {az: [] for az in azimuths}
    references = []
    reference = None
    strips = []

    def measure(alt, az):
        return _measure_at(base_url, float(alt), float(az), sdr_type,
                           integration_time_s, center_freq, sample_rate, gain,
                           beam_fwhm_deg, power_meter, cancel_event,
                           position_tolerance, slew_timeout, settle_s=settle_s)

    try:
        for index, alt in enumerate(altitudes):
            if not pending:
                break
            _check_cancelled(cancel_event, "horizon strip scan")

            # The sky reference sits high, so taking one costs a large
            # altitude move each way - on the very axis this pattern exists to
            # spare. So it is taken at the start and then only when the mount
            # has just homed, which runs the axis over its full range anyway.
            if reference is None or index % max(1, home_every_strips) == 0:
                reference = _sky_reference(
                    base_url, sdr_type, integration_time_s, center_freq,
                    sample_rate, gain, beam_fwhm_deg, power_meter, cancel_event,
                    position_tolerance, slew_timeout, settle_s)
                reference["strip_alt_deg"] = alt
                references.append(reference)
            log.info("Strip at alt %.0f deg: %d azimuths pending, sky reference "
                     "%.6g +- %.2g", alt, len(pending), reference["level"],
                     reference["sigma"])

            # Re-measure a few azimuths already known to be clear, at this
            # altitude. They are what the threshold is judged against: the
            # strip's own distribution cannot serve once most of what remains
            # is blocked, and by the top strip everything remaining is blocked,
            # so a percentile of it would clear the tower along with the sky.
            controls = []
            for az in control_azimuths:
                _check_cancelled(cancel_event, "horizon strip scan")
                controls.append(measure(alt, az)["power"])

            # Serpentine in azimuth: no long return slew between strips.
            order = sorted(pending, reverse=(index % 2 == 1))
            measured = []
            for az in order:
                _check_cancelled(cancel_event, "horizon strip scan")
                point = measure(alt, az)
                point["strip_alt_deg"] = alt
                columns[az].append((point["true_alt"], point["power"]))
                measured.append((az, point["power"]))
                if progress_callback:
                    progress_callback(index, len(altitudes), {
                        "alt": alt, "az": az, "power": point["power"],
                        "pending": len(pending),
                        "sky_reference": reference["level"],
                    })

            # The clear-sky level at *this* altitude, from the strip itself.
            # It cannot come from the zenith reference: even an unobstructed
            # horizon is warmer low down through airmass and spillover - 0.0114
            # against 0.0089 at the zenith, measured on 2026-08-21 - so a zenith
            # threshold would call every low azimuth blocked.
            powers = np.array([p for _, p in measured], dtype=float)
            if controls:
                clear_level = float(np.median(controls))
                clear_from = 'controls'
            else:
                # Bootstrap only: the most open azimuths in the strip stand in
                # for a clear-sky level until some azimuth has actually been
                # cleared. It assumes the first strip contains open sky, which
                # is why the first strip should start below the bulk of the
                # horizon; if it does not, nothing clears and the next strip
                # tries again one step higher.
                clear_level = float(np.percentile(powers, 5))
                clear_from = 'strip'

            threshold = clear_level + _CLEAR_SIGMA * max(reference["sigma"], 1e-12)
            newly_clear = [az for az, p in measured if p <= threshold]
            for az in newly_clear:
                clearance[az] = alt
                pending.remove(az)
            # Keep a few of the first azimuths to clear as controls for every
            # strip above, spread across the sweep so one local oddity cannot
            # set the reference by itself.
            if not control_azimuths and len(newly_clear) >= 3:
                step = max(1, len(newly_clear) // 3)
                control_azimuths = newly_clear[::step][:3]
                log.info("Control azimuths for the clear-sky reference: %s",
                         ", ".join("%.0f" % a for a in control_azimuths))
            strips.append({
                "alt_deg": alt,
                "n_measured": len(measured),
                "n_controls": len(controls),
                "clear_level_from": clear_from,
                "clear_level": clear_level,
                "threshold": threshold,
                "n_cleared": len(newly_clear),
                "n_pending_after": len(pending),
                "sky_reference": reference["level"],
            })
            log.info("Strip at alt %.0f deg: %d of %d cleared, %d still blocked",
                     alt, len(newly_clear), len(measured), len(pending))

            if (pending and home_every_strips
                    and (index + 1) % home_every_strips == 0
                    and sdr_type != "demo"):
                _home_and_wait(base_url, cancel_event=cancel_event)
    finally:
        if power_meter is not None:
            power_meter.close()

    finished = datetime.now(timezone.utc)
    entries = []
    for az in azimuths:
        column = sorted(columns[az])
        cleared_at = clearance.get(az)
        entries.append({
            "az_deg": az,
            "cut_alt_deg": [a for a, _ in column],
            "cut_power": [p for _, p in column],
            "fit": {
                "success": True,
                "estimator": "strip_threshold" if cleared_at is not None
                             else "blocked_above_ceiling",
                "alt_clear": float(cleared_at) if cleared_at is not None
                             else float(alt_max),
                "edge_reported_deg": float(cleared_at) if cleared_at is not None
                                     else float(alt_max),
                "quality": ("clear from %.0f deg" % cleared_at) if cleared_at is not None
                           else "still blocked at the ceiling of %.0f deg" % alt_max,
                "limited_by_ceiling": cleared_at is None,
            },
        })

    return {
        "record_version": _STRIP_RECORD_VERSION,
        "pattern": "strips",
        "started_utc": started.isoformat(),
        "finished_utc": finished.isoformat(),
        "duration_s": (finished - started).total_seconds(),
        "az_step_deg": az_step,
        "alt_step_deg": alt_step,
        "alt_min_deg": alt_start,
        "alt_max_deg": alt_max,
        "settle_s": settle_s,
        "integration_time_s": integration_time_s,
        "home_every_strips": home_every_strips,
        "beam_fwhm_deg": beam_fwhm_deg,
        "sdr_type": sdr_type,
        "center_freq_hz": center_freq,
        "sample_rate_hz": sample_rate,
        "gain_db": gain,
        "site_lat": cfg.get("observer_lat"),
        "site_lon": cfg.get("observer_lon"),
        "n_azimuths": len(entries),
        "strips": strips,
        # Which azimuths were re-measured at every altitude to provide the
        # clear-sky reference. Recorded because the derived clearances depend
        # on them: if one turns out to have been obstructed after all, every
        # threshold above the first strip was set too high.
        "control_azimuths": control_azimuths,
        "sky_references": references,
        "entries": entries,
        "success": bool(entries),
        "complete": not pending,
    }

if __name__ == "__main__":
    main()
