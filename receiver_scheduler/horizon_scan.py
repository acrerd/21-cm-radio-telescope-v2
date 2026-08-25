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

from observatory import SITE_LAT_DEG, SITE_LON_DEG

# `observatory` puts astro_simulator on the path; the store lives there because
# the simulator draws the measured horizon and must not import the scheduler.
import horizon_store  # noqa: E402
from horizon_store import (  # noqa: E402
    ARCHIVE_DIR,
    archive_profile,
    horizon_castellation,
    horizon_floor,
    is_obstructed,
    list_profiles,
    load_active,
    load_profile,
    profile_date,
    profile_floors,
    profile_name,
    set_active,
    summarise,
)

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
    """Save a scan. With no path, file it in the dated archive.

    An explicit path is a partial save during a scan and goes exactly where it
    is told. A save with no path is a finished scan, and that is *archived*
    under its own date rather than overwriting the horizon in force.

    It does not become the horizon in force by itself. The trees are cut back
    every so often, which genuinely opens the sky, and a scan that lowered the
    horizon the moment it finished would do so with nobody having agreed that
    the new one is right - and would destroy the old measurement in the same
    stroke. Choosing is a separate act (`set_active`). The one exception is the
    first scan on a fresh installation, where there is nothing to displace.
    """
    if path is not None:
        with open(path, "w") as f:
            json.dump(profile, f, indent=2)
        return path

    archived = archive_profile(profile)
    if horizon_store.active_name() is None:
        set_active(profile_name(profile), note="first scan on this installation")
        log.info("Horizon profile %s archived and made active (%d azimuths)",
                 profile_name(profile), profile.get("n_azimuths", 0))
    else:
        log.info("Horizon profile %s archived (%d azimuths). The horizon in "
                 "force is still %s - choose the new one on the Horizon tab if "
                 "it is the better record.",
                 profile_name(profile), profile.get("n_azimuths", 0),
                 horizon_store.active_name())
    return archived


def load_horizon_profile(path: str | None = None) -> dict | None:
    """The horizon in force, or the one at an explicit path."""
    if path is None:
        return load_active()
    try:
        with open(path) as f:
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
latitude = {profile.get('site_lat', SITE_LAT_DEG):+.6f}
longitude = {profile.get('site_lon', SITE_LON_DEG):+.6f}
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


# profile_floors / horizon_floor / is_obstructed now live in horizon_store, so
# that the simulator can apply the identical rule when it draws the horizon
# without importing this module and everything it depends on. They are imported
# above and re-exported here, because every existing caller reads them from
# `horizon_scan` and there is no reason to make them all move.


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

def _equal_area_radius(alt_deg):
    """Lambert azimuthal equal-area radius, zenith at 0, horizon at 1.

    r = sqrt(2) * sin(z/2) for zenith distance z. The point of choosing this
    over the obvious r = z/90 is that area on the page is then *exactly*
    proportional to solid angle on the sky: r dr dtheta = sin(z) dz dtheta.
    A linear-in-altitude polar plot exaggerates the zenith enormously and makes
    a tall obstruction near the horizon look like a thin sliver, when in solid
    angle it is the expensive one - which is the whole thing this plot exists
    to show. Half the sky lies inside r = 1/sqrt(2), at altitude 30 deg.
    """
    z = np.radians(90.0 - np.asarray(alt_deg, dtype=float))
    return np.sqrt(2.0) * np.sin(z / 2.0)


def _sky_xy(alt_deg, az_deg):
    """Projected x, y with north up and east left, looking up at the sky.

    Matches the convention of a planetarium all-sky chart (and Stellarium's):
    azimuth runs from north through east, which is anticlockwise on the page
    because the observer is underneath looking out, not above looking down.
    """
    r = _equal_area_radius(alt_deg)
    a = np.radians(np.asarray(az_deg, dtype=float))
    return -r * np.sin(a), r * np.cos(a)


def generate_sky_plot(profile: dict,
                      output_path: str = "horizon_sky.png") -> str:
    """The available sky as an equal-area polar chart.

    Shows what is left rather than what is blocked, in a projection where the
    area of the open region on the page is proportional to the solid angle it
    represents - so the picture can be read directly as "this much sky", and
    two scans can be compared by eye.

    The boundary is drawn by sampling `horizon_floor`, so it is a castellation
    and it is the same rule that decides whether a target is blocked, not a
    prettier curve alongside it.
    """
    if not MATPLOTLIB_AVAILABLE:
        raise ImportError("matplotlib is required")

    floors = profile_floors(profile)
    if not floors:
        raise ValueError("horizon profile has no usable entries")

    ink = "#e8eaed"
    sky = "#16233f"
    edge = "#ff9f43"
    grid = "#2f6f5e"
    compass = "#ff6b6b"
    back = "#0a0a18"
    blocked = "#3a1f2b"

    fig = plt.figure(figsize=(9.0, 9.0), facecolor=back)
    ax = fig.add_axes([0.04, 0.04, 0.92, 0.88])
    ax.set_facecolor(back)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_xlim(-1.30, 1.30)
    ax.set_ylim(-1.30, 1.30)

    # Everything below the horizon line is obstructed, not absent: tint the
    # whole disc first and let the open sky cover what it covers, so the
    # difference between the two is what the eye is actually reading.
    ax.add_patch(plt.Circle((0, 0), 1.0, facecolor=blocked, edgecolor="none",
                            zorder=0))

    # The open sky, as one filled polygon: the region is star-shaped about the
    # zenith, so the boundary alone bounds it.
    az_fine = np.arange(0.0, 360.0 + 0.25, 0.25)
    alt_edge = np.array([horizon_floor(profile, a) for a in az_fine])
    bx, by = _sky_xy(alt_edge, az_fine)
    ax.fill(bx, by, facecolor=sky, edgecolor="none", zorder=1)

    # Altitude rings, labelled up the most open spoke on this particular
    # profile - wherever the sky reaches lowest, the numbers have room and are
    # never overwritten by the horizon crossing them.
    label_az = min(floors, key=lambda f: f[1])[0]
    for alt in (0, 15, 30, 45, 60, 75):
        r = float(_equal_area_radius(alt))
        ring = plt.Circle((0, 0), r, fill=False, edgecolor=grid,
                          lw=1.4 if alt == 0 else 0.8,
                          alpha=0.9 if alt == 0 else 0.55, zorder=3)
        ax.add_patch(ring)
        if alt:
            lx, ly = _sky_xy(alt, label_az)
            ax.text(float(lx), float(ly), "%d°" % alt, color=ink,
                    fontsize=9, ha="center", va="center", zorder=4, alpha=0.8,
                    bbox=dict(boxstyle="round,pad=0.15", fc=back, ec="none",
                              alpha=0.7))

    # Azimuth spokes every 30 degrees.
    for a in range(0, 360, 30):
        sx, sy = _sky_xy([90.0, 0.0], [a, a])
        ax.plot(sx, sy, color=grid, lw=0.7, alpha=0.5, zorder=3)

    # The measured horizon itself.
    ax.plot(bx, by, color=edge, lw=2.0, zorder=5)

    names = {0: "N", 45: "NE", 90: "E", 135: "SE",
             180: "S", 225: "SW", 270: "W", 315: "NW"}
    for a, name in names.items():
        lx, ly = _sky_xy(0.0, a)
        ax.text(float(lx) * 1.13, float(ly) * 1.13, name, color=compass,
                fontsize=13 if len(name) == 1 else 11, ha="center",
                va="center", zorder=6)

    visible = horizon_store.visible_sky_sq_deg(profile)
    fraction = visible / horizon_store.HEMISPHERE_SQ_DEG
    date = horizon_store.profile_date(profile)
    demo = " — SIMULATED, not the observatory" if \
        profile.get("sdr_type") == "demo" else ""
    fig.text(0.5, 0.965, "Available sky — measured %s%s" % (date, demo),
             color=ink, fontsize=14, ha="center", va="center")
    fig.text(0.5, 0.932,
             "%s of %s deg²  (%.0f%% of the hemisphere)   ·   "
             "equal-area projection: area on the page = solid angle on the sky"
             % (format(int(round(visible)), ","),
                format(int(round(horizon_store.HEMISPHERE_SQ_DEG)), ","),
                100.0 * fraction),
             color=grid, fontsize=9.5, ha="center", va="center")

    fig.savefig(output_path, dpi=110, facecolor=back)
    plt.close(fig)
    log.info("Sky plot written to %s (%.0f deg2 visible)", output_path, visible)
    return output_path


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

    fig = plt.figure(figsize=(14, 9))
    ax = fig.add_subplot(2, 2, (1, 2))
    ax.fill_between(az, 0, clear, color="#ff6b6b", alpha=0.18,
                    label="Obstructed")
    ax.step(az, clear, where="mid", color="#00d4ff", linewidth=1.8,
            label="Lowest clear altitude")
    if blocked.any():
        ax.plot(az[blocked], clear[blocked], "v", color="#ffaa00", markersize=8,
                label="Still blocked at the ceiling")
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
        print("  strip %d/%d  alt %4.1f  az %5.1f  %.6g  (%d/%d)" % (
            idx + 1, total, info["alt"], info["az"], info["power"],
            info.get("measured", 0), info.get("of", 0)), flush=True)

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

# Bumped when the record changed shape: version 2 peeled azimuths away as they
# cleared and thresholded while observing; version 3 measures every azimuth at
# every altitude, records the power, and decides afterwards.

_STRIP_RECORD_VERSION = 3


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


def _partial_path(started):
    """Where a scan in progress writes itself.

    Under data/, with the observations, rather than beside the source. A partial
    is written after every strip and again on any failure, so a demo run or a
    test leaves one behind too - sixteen of them accumulated in the source tree
    within an hour of the saving being added.
    """
    folder = os.path.join(_SCRIPT_DIR, "data")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        folder = _SCRIPT_DIR
    return os.path.join(folder, "horizon_partial_%s.json"
                        % started.strftime("%Y%m%dT%H%M%SZ"))


def derive_clearance(profile, quantile: float = 0.25,
                     tolerance: float = 0.03) -> dict:
    """Decide where the horizon is, from powers already measured.

    Kept apart from the observing and re-runnable on a stored profile, because a
    threshold applied while the dish is moving has to be right first time and
    there is no way to know whether it is until the data exist. On 2026-08-24 one
    was wrong and cost a night: it allowed 0.26% above the clear-sky level, being
    scaled by the *repeat noise* of the sky reference rather than by how much
    real sky varies between azimuths, and nothing cleared at all.

    Every azimuth is now measured at every altitude, and that changes what is
    possible here. The scan used to drop azimuths once they cleared, so by the
    top strip everything remaining was blocked and the strip's own distribution
    was useless as a reference - which is why it needed control azimuths carried
    up from below. With nothing dropped, the clear azimuths are present in every
    strip, so the strip's own lower quartile *is* the clear-sky level, measured
    at the right airmass, with no reference to carry and nothing to go stale.

    The tolerance is fractional and generous, because what it has to span is the
    genuine variation of clear sky across azimuth - spillover into different
    surroundings - and not radiometric noise, which is thirty times smaller.

    Returns the profile, modified in place: each entry gains a `fit`, and each
    strip the level and threshold used, so a later reader can see what was
    decided and re-decide it.
    """
    entries = profile.get("entries") or []
    alt_max = float(profile.get("alt_max_deg") or 0.0)
    strips = profile.get("strips") or []
    if strips and not all(s.get("powers") for s in strips):
        raise ValueError("this profile has strips without per-strip powers, so "
                         "the horizon cannot be re-derived from it; it predates "
                         "record version 3")
    if not strips:
        # A scan interrupted inside its first strip. There is nothing to decide
        # from, but the measurements it did take are worth keeping, so say the
        # horizon is undetermined rather than raising - otherwise the emergency
        # save fails precisely when it is needed, which is the failure it was
        # written to prevent.
        for entry in entries:
            entry["fit"] = {
                "success": False,
                "estimator": "not_enough_data",
                "alt_clear": alt_max,
                "edge_reported_deg": alt_max,
                "quality": "no complete strip, so the horizon is undetermined",
                "limited_by_ceiling": True,
            }
        profile["clearance_rule"] = {"quantile": quantile,
                                     "tolerance": tolerance}
        return profile

    # Group by the strip's *commanded* altitude, not by the true altitude each
    # measurement landed at. Those differ per azimuth through the pointing
    # model - 4.8 to 5.2 degrees across one strip - so grouping by true
    # altitude puts one measurement in each group, and a quantile of a single
    # value is that value, so every point clears itself. It did exactly that
    # the first time this ran.
    levels = {}
    for strip in strips:
        arr = np.asarray([float(v) for v in strip["powers"].values()], float)
        arr = arr[np.isfinite(arr)]
        if not arr.size:
            continue
        clear_level = float(np.quantile(arr, quantile))
        levels[float(strip["alt_deg"])] = {
            "clear_level": clear_level,
            "threshold": clear_level * (1.0 + tolerance),
            "n": int(arr.size),
        }

    cleared = {}
    for alt in sorted(levels):
        threshold = levels[alt]["threshold"]
        powers = next(s["powers"] for s in strips
                      if float(s["alt_deg"]) == alt)
        for az_key, power in powers.items():
            az = round(float(az_key), 1)
            if az not in cleared and float(power) <= threshold:
                cleared[az] = alt

    for entry in entries:
        cleared_at = cleared.get(round(float(entry["az_deg"]), 1))
        entry["fit"] = {
            "success": True,
            "estimator": "strip_quantile" if cleared_at is not None
                         else "blocked_above_ceiling",
            "alt_clear": cleared_at if cleared_at is not None else alt_max,
            "edge_reported_deg": cleared_at if cleared_at is not None else alt_max,
            "quality": ("clear from %.0f deg" % cleared_at)
                       if cleared_at is not None
                       else "still blocked at the ceiling of %.0f deg" % alt_max,
            "limited_by_ceiling": cleared_at is None,
        }

    for strip in strips:
        level = levels.get(float(strip["alt_deg"]))
        if level:
            strip.update({
                "clear_level": level["clear_level"],
                "threshold": level["threshold"],
                "n_cleared": sum(1 for a in cleared.values()
                                 if a == float(strip["alt_deg"])),
            })
    profile["clearance_rule"] = {"quantile": quantile, "tolerance": tolerance}
    return profile


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

    def build_profile(complete):
        """The profile as it stands. Called after every strip and at the end."""
        finished_at = datetime.now(timezone.utc)
        entries = []
        for az in azimuths:
            column = sorted(columns[az])
            entries.append({
                "az_deg": az,
                "cut_alt_deg": [a for a, _ in column],
                "cut_power": [pw for _, pw in column],
            })
        profile = {
            "record_version": _STRIP_RECORD_VERSION,
            "pattern": "strips",
            "started_utc": started.isoformat(),
            "finished_utc": finished_at.isoformat(),
            "duration_s": (finished_at - started).total_seconds(),
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
            "sky_references": references,
            "entries": entries,
            "success": bool(entries),
            "complete": complete,
        }
        return derive_clearance(profile)

    try:
        for index, alt in enumerate(altitudes):
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
            log.info("Strip at alt %.0f deg: %d azimuths, sky reference "
                     "%.6g +- %.2g", alt, len(azimuths), reference["level"],
                     reference["sigma"])

            # Serpentine in azimuth: no long return slew between strips.
            order = sorted(azimuths, reverse=(index % 2 == 1))
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
                        "measured": len(measured), "of": len(order),
                        "sky_reference": reference["level"],
                    })

            # Nothing is decided here. Every azimuth is measured at every
            # altitude and its power recorded; where the horizon lies is worked
            # out afterwards by derive_clearance(), from the numbers.
            #
            # The scan used to threshold as it went, dropping azimuths that had
            # cleared so later strips had less to do. It cost a night on
            # 2026-08-24. The threshold was the clear-sky level plus five times
            # the *repeat noise* of the sky reference - a radiometric scale, not
            # the scale on which clear sky varies from one azimuth to the next.
            # At 4.6e-06 that allowed 0.26%, real sky varies by more than that
            # through spillover alone, and by altitude 20 nothing had cleared at
            # all while every reading sat within 0.5% of the controls. The
            # perverse part: measuring the reference better made the test
            # stricter, so more care produced a worse answer.
            #
            # A threshold applied while observing must be right first time or
            # the observing is wasted, and there is no way to know it is right
            # until the data exist. Applied afterwards it costs a re-analysis.
            strips.append({
                "alt_deg": alt,
                "n_measured": len(measured),
                "sky_reference": reference["level"],
                "sky_reference_sigma": reference["sigma"],
                "powers": {("%.1f" % az): float(pw) for az, pw in measured},
            })
            log.info("Strip at alt %.0f deg: %d azimuths measured, median "
                     "%.6g, sky reference %.6g",
                     alt, len(measured),
                     float(np.median([pw for _, pw in measured])) if measured
                     else float("nan"),
                     reference["level"])

            # Saved after every strip. Tonight's run lost forty-five minutes of
            # good measurements because nothing was written until the end, and
            # a power reading exists nowhere else - not in the log, not in the
            # progress callback's history.
            try:
                save_horizon_profile(build_profile(complete=False),
                                     _partial_path(started))
            except OSError as exc:
                log.warning("Could not save the partial horizon scan: %s", exc)

            if (home_every_strips and index + 1 < len(altitudes)
                    and (index + 1) % home_every_strips == 0
                    and sdr_type != "demo"):
                _home_and_wait(base_url, cancel_event=cancel_event)
    except BaseException:
        # Save before unwinding, whatever went wrong - a rejected slew, a
        # cancellation, a receiver that died. Writing only after each strip was
        # not enough: on 2026-08-24 a scan was refused a slew on the last
        # azimuth of the first strip and lost the whole strip, having saved
        # nothing yet. Measurements already taken are not the failure's to keep.
        try:
            path = save_horizon_profile(build_profile(complete=False),
                                        _partial_path(started))
            log.warning("Horizon scan interrupted; %d azimuths saved to %s",
                        sum(1 for az in azimuths if columns[az]), path)
        except Exception as exc:                      # noqa: BLE001
            log.error("Could not save the interrupted horizon scan: %s", exc)
        raise
    finally:
        if power_meter is not None:
            power_meter.close()

    return build_profile(complete=True)


if __name__ == "__main__":
    main()
