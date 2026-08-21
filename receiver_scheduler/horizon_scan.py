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
    from horizon_scan import horizon_scan
    profile = horizon_scan(sdr_type="demo")

    python horizon_scan.py --sdr demo --az-step 5 --output horizon.png
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
    from scipy.optimize import curve_fit
    from scipy.special import erfc, erfcinv
except ImportError:                                  # pragma: no cover
    curve_fit = None
    erfc = erfcinv = None

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
DEFAULT_ALT_STEP = 1.0
DEFAULT_WINDOW_DEG = 6.0          # half-width of the tracking window
DEFAULT_ALT_MIN = 2.0             # stay clear of the mechanical limit
DEFAULT_ALT_CEILING = 40.0        # normal top of a cut
DEFAULT_ALT_CEILING_MAX = 80.0    # how far up a tall obstruction may be chased
DEFAULT_INTEGRATION_S = 0.5
DEFAULT_BEAM_FWHM_DEG = 5.8       # as measured by the Sun scans

# Excess power over sky, as a fraction of the sky-to-ground step, that counts as
# a clean sky position. 1% of a ~200 K step is ~2 K, comfortably below the other
# systematics in an H1 observation.
DEFAULT_CLEARANCE_FRACTION = 0.01

# Is there a step in this cut at all? That is a question about significance
# against the noise, not about an arbitrary percentage - and treating it as the
# latter made the scan blind to exactly the obstructions hardest to see.
#
# On 2026-08-21 the northeastern azimuths returned a contrast of 2.3% of sky
# where the eastern treeline gives 59%. A metal-clad building stands there:
# metal has very low emissivity, so it reflects cold sky rather than radiating
# at ambient, and shows only a faint emissive residue. But radiometric noise at
# 2.4 MHz and 0.5 s is 0.09%, so 2.3% is a 25 sigma detection - and a flat 5%
# threshold discarded it and reported the building as open sky, which is the
# unsafe direction to be wrong in.
#
# So the test is significance against the measured scatter of the sky samples,
# with a small absolute floor only to catch a pathological cut whose sky
# scatter has come out at zero.
_MIN_CONTRAST_SIGMA = 5.0
_MIN_CONTRAST_FRACTION = 0.003

# How far the fitted transition width may stray from the known beam before the
# straight-edge model is declared inapplicable. A tall narrow structure is not
# an edge, and fitting one to it produces a confident wrong answer.
_WIDTH_TOLERANCE = (0.3, 3.0)

# Residual RMS above this fraction of the contrast means the step model does not
# describe the cut, whatever the fitted parameters say.
_MAX_RESIDUAL_FRACTION = 0.15

# Retries per azimuth: widen the window, chase a tall obstruction upwards, make
# headroom for the clearance. Each one raises the ceiling so the loop cannot run
# away, but a cap keeps one pathological azimuth from eating the whole night.
_MAX_CUT_ATTEMPTS = 5

# Consecutive samples that must exceed the threshold before the empirical
# envelope will believe them, and the floor on that threshold in units of the
# scatter of the sky samples themselves.
_ENVELOPE_RUN = 3
_SKY_SIGMA_FLOOR = 3.0

# How far above the run's sky level a stepless cut may sit and still count as
# sky. Sky and ground differ by of order the system temperature, so this only
# has to beat receiver gain drift across the run.
_SKY_MATCH_TOLERANCE = 0.25

_SQRT2 = math.sqrt(2.0)
_FWHM_TO_SIGMA = 1.0 / (2.0 * math.sqrt(2.0 * math.log(2.0)))


# ---------------------------------------------------------------------------
# The step model
# ---------------------------------------------------------------------------

def horizon_step(alt, p_sky, contrast, edge, sigma):
    """Power against altitude for a straight edge seen through a Gaussian beam."""
    return p_sky + 0.5 * contrast * erfc((np.asarray(alt) - edge) / (sigma * _SQRT2))


def clearance_altitude(edge: float, sigma: float,
                       fraction: float = DEFAULT_CLEARANCE_FRACTION) -> float:
    """Altitude at which ground pickup falls to `fraction` of the full step.

    Inverting the same fitted curve rather than adding a rule-of-thumb multiple
    of the beam width: this is a statement about the measured skirt of this
    dish in this direction, and it stays right if the beam is not what we think.
    """
    return float(edge + sigma * _SQRT2 * erfcinv(2.0 * fraction))


def fit_horizon_edge(altitudes, powers,
                     beam_fwhm_deg: float = DEFAULT_BEAM_FWHM_DEG,
                     clearance_fraction: float = DEFAULT_CLEARANCE_FRACTION,
                     reached_mount_limit: bool = False,
                     sky_reference: float | None = None) -> dict:
    """Fit one altitude cut and describe what it found.

    Always reports an empirical clearance as well as the fitted one. Where the
    step model does not apply - a tower rather than a treeline - the fit is
    marked unusable and the empirical envelope is the answer, because it makes
    no assumption about the shape of the obstruction.
    """
    alt = np.asarray(altitudes, dtype=float)
    p = np.asarray(powers, dtype=float)
    order = np.argsort(alt)
    alt, p = alt[order], p[order]

    result = {
        "n_points": int(len(alt)),
        "alt_min": float(alt.min()) if len(alt) else None,
        "alt_max": float(alt.max()) if len(alt) else None,
        "success": False,
        "estimator": None,
        "quality": None,
    }
    if curve_fit is None:
        result["quality"] = "scipy is not available"
        return result
    if len(alt) < 5:
        result["quality"] = f"only {len(alt)} points in the cut"
        return result

    p_sky_guess = float(np.median(p[-max(2, len(p) // 5):]))
    p_ground_guess = float(np.median(p[:max(2, len(p) // 5)]))
    contrast_guess = p_ground_guess - p_sky_guess
    scale = abs(p_sky_guess) if p_sky_guess else 1.0

    # An empirical clearance first: it needs no model, so it is available even
    # when the fit is rejected, and it is what a complex obstruction gets.
    #
    # Two things this has to survive, both seen on 2026-08-21 pinning the
    # clearance to the top of the cut at azimuths 180 to 195:
    #
    # p_sky is the median of the sky end of the cut, so half of those samples
    # lie above it by construction, and a threshold 1% of the step above the
    # median is below the scatter of the sky itself whenever that scatter is
    # larger than radiometric - gain drift across a three-minute cut, or RFI.
    # So the threshold is floored at three sigma of the sky samples: a
    # clearance cannot be claimed from a signal the cut cannot measure.
    sky_samples = p[-max(3, len(p) // 5):]
    sky_sigma = float(np.std(sky_samples)) if len(sky_samples) > 2 else 0.0
    threshold = p_sky_guess + max(clearance_fraction * contrast_guess,
                                  _SKY_SIGMA_FLOOR * sky_sigma)
    result["contrast_fraction"] = float(contrast_guess / scale)
    result["sky_sigma"] = sky_sigma
    result["threshold"] = float(threshold)
    result["p_sky"] = p_sky_guess
    result["p_ground"] = p_ground_guess
    significant = (contrast_guess > _MIN_CONTRAST_SIGMA * sky_sigma
                   and contrast_guess / scale >= _MIN_CONTRAST_FRACTION)
    result["contrast_sigma"] = (float(contrast_guess / sky_sigma)
                                if sky_sigma > 0 else float("inf"))
    if not significant:
        # No step anywhere in the cut. Which of two very different things that
        # means depends on whether the mount had run out of altitude: the
        # observatory stands on a hill, so at some azimuths the skyline is
        # below anything the telescope can point at, and the cut is then all
        # sky. That is the best possible result for an azimuth, not a failed
        # measurement - and reporting it as a failure was actively harmful,
        # because a dropped azimuth gets its floor interpolated from its
        # neighbours, lending their trees to the one clear direction.
        #
        # The reverse case, a cut that is all ground, is handled before this by
        # the caller raising the ceiling until sky appears.
        # A stepless cut is all sky or all ground, and those are opposite
        # conclusions. Nothing inside one cut can tell them apart - both are
        # flat - so it takes the sky level measured at the other azimuths of
        # the run. Gain drifts by far less across an hour than the factor of
        # ~1.6 between sky and ground, so the comparison is safe.
        level = float(np.median(p))
        if sky_reference is not None and sky_reference > 0:
            looks_like_sky = level <= sky_reference * (1.0 + _SKY_MATCH_TOLERANCE)
        else:
            looks_like_sky = True          # no reference yet; the caller's
            #                                ceiling chase is the backstop
        if reached_mount_limit and looks_like_sky:
            result["estimator"] = "unobstructed"
            result["success"] = True
            result["quality"] = (
                "no obstruction above the mount limit: contrast is "
                f"{100 * contrast_guess / scale:.2f}% of sky, "
                f"{result['contrast_sigma']:.1f} sigma")
            result["edge_reported_deg"] = float(alt.min())
            result["alt_clear"] = float(alt.min())
            result["alt_clear_measured"] = float(alt.min())
            result["limited_by_mount"] = True
            result["level"] = level
            return result
        result["level"] = level
        if not looks_like_sky:
            # Ground from the bottom of the cut to the top: the obstruction is
            # taller than we have looked, and the caller should raise the
            # ceiling rather than call this an absence of horizon.
            result["quality"] = (f"no sky in this cut: level {level:.4g} against a "
                                 f"sky reference of {sky_reference:.4g}")
            result["estimator"] = "all_ground"
            return result
        result["quality"] = (
            f"no horizon in this cut: contrast is "
            f"{100 * contrast_guess / scale:.2f}% of sky, "
            f"{result['contrast_sigma']:.1f} sigma")
        result["estimator"] = "none"
        return result
    # And it must take a run of consecutive samples, not one. A single sample
    # above threshold at the top of a cut is noise; a real obstruction fills
    # the beam and shows up in its neighbours too. One sample was all it took
    # to pin the envelope to the ceiling.
    above = np.asarray(p > threshold, dtype=int)
    if len(above) >= _ENVELOPE_RUN:
        runs = np.convolve(above, np.ones(_ENVELOPE_RUN, dtype=int), mode="valid")
        complete = np.where(runs == _ENVELOPE_RUN)[0]
        result["alt_clear_measured"] = (
            float(alt[complete[-1] + _ENVELOPE_RUN - 1]) if len(complete)
            else float(alt.min()))
    else:
        result["alt_clear_measured"] = float(alt.min())

    sigma_guess = beam_fwhm_deg * _FWHM_TO_SIGMA
    midpoint = p_sky_guess + 0.5 * contrast_guess
    crossings = np.where(p >= midpoint)[0]
    edge_guess = float(alt[crossings[-1]]) if len(crossings) else float(np.median(alt))

    try:
        popt, _ = curve_fit(
            horizon_step, alt, p,
            p0=[p_sky_guess, contrast_guess, edge_guess, sigma_guess],
            bounds=([-np.inf, 0.0, alt.min() - 10.0, 0.2],
                    [np.inf, np.inf, alt.max() + 10.0, 20.0]),
            maxfev=20000)
    except Exception as exc:                          # noqa: BLE001
        result["quality"] = f"step fit did not converge: {exc}"
        result["estimator"] = "envelope"
        result["success"] = "alt_clear_measured" in result
        return result

    p_sky, contrast, edge, sigma = (float(v) for v in popt)
    residual = p - horizon_step(alt, *popt)
    rms = float(np.sqrt(np.mean(residual ** 2)))

    result.update({
        "p_sky": p_sky,
        "contrast": contrast,
        "edge_deg": edge,
        "width_sigma_deg": sigma,
        "width_fwhm_deg": sigma / _FWHM_TO_SIGMA,
        "residual_rms": rms,
        "residual_fraction": float(rms / contrast) if contrast else None,
        "alt_clear_fit": clearance_altitude(edge, sigma, clearance_fraction),
    })

    expected_sigma = beam_fwhm_deg * _FWHM_TO_SIGMA
    problems = []
    if not alt.min() - 1.0 <= edge <= alt.max() + 1.0:
        problems.append("edge lies outside the sampled window")
    if not (_WIDTH_TOLERANCE[0] * expected_sigma <= sigma
            <= _WIDTH_TOLERANCE[1] * expected_sigma):
        problems.append(f"transition width {sigma / _FWHM_TO_SIGMA:.1f} deg is "
                        f"not the {beam_fwhm_deg:.1f} deg beam")
    if contrast and rms / contrast > _MAX_RESIDUAL_FRACTION:
        problems.append(f"residuals are {100 * rms / contrast:.0f}% of the step")

    if problems:
        # Not a straight edge. Keep the fit for the record but do not trust it:
        # the conservative envelope makes no shape assumption.
        result["estimator"] = "envelope"
        result["quality"] = "; ".join(problems)
        result["success"] = True
        result["alt_clear"] = result["alt_clear_measured"]
        result["edge_reported_deg"] = result["alt_clear_measured"]
    else:
        result["estimator"] = "edge_fit"
        result["quality"] = "ok"
        result["success"] = True
        # The conservative of the two: the fitted skirt is an extrapolation into
        # the part of the curve the cut constrains least.
        result["alt_clear"] = max(result["alt_clear_fit"],
                                  result["alt_clear_measured"])
        result["edge_reported_deg"] = edge
    return result


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


def _cut_altitudes(low: float, high: float, step: float, descending: bool):
    """Altitudes for one cut, ordered so the mount sweeps one way only.

    Cuts alternate direction from azimuth to azimuth (a serpentine), which
    removes the long return slew at the end of every cut - 71 of them at up to
    40 degrees each. The cost is that any backlash appears as a difference
    between the two directions, which is why even and odd azimuths are worth
    comparing before the profile is believed.
    """
    n = max(2, int(round((high - low) / step)) + 1)
    alts = np.linspace(low, high, n)
    return alts[::-1] if descending else alts


def horizon_scan(az_start: float = 5.0,
                 az_end: float = 350.0,
                 az_step: float = DEFAULT_AZ_STEP,
                 alt_step: float = DEFAULT_ALT_STEP,
                 window_deg: float = DEFAULT_WINDOW_DEG,
                 alt_min: float = DEFAULT_ALT_MIN,
                 alt_ceiling: float = DEFAULT_ALT_CEILING,
                 alt_ceiling_max: float = DEFAULT_ALT_CEILING_MAX,
                 integration_time_s: float = DEFAULT_INTEGRATION_S,
                 beam_fwhm_deg: float = DEFAULT_BEAM_FWHM_DEG,
                 clearance_fraction: float = DEFAULT_CLEARANCE_FRACTION,
                 initial_edge_guess: float = 10.0,
                 sdr_type: str = "b210",
                 center_freq: float = 1420.405752e6,
                 sample_rate: float = 2.4e6,
                 gain: float = 40.0,
                 srt_url: str | None = None,
                 slew_timeout: int = 120,
                 position_tolerance: float = 0.5,
                 progress_callback=None,
                 cancel_event=None) -> dict:
    """Sweep azimuth, cutting down through the horizon at each step.

    The altitude window tracks the previous azimuth's answer, because the
    horizon is continuous almost everywhere; where it is not - a roof edge, the
    tower - the cut fails to find a step in its window and is retried over the
    full range, extending upwards if the obstruction turns out to be taller
    than the ceiling. That way the common case is cheap and the rare case is
    still correct.
    """
    cfg = _load_scheduler_config()
    base_url = (srt_url or cfg.get("srt_controller_url", "")).rstrip("/")
    if sdr_type != "demo" and not base_url:
        raise RuntimeError("No SRT controller URL configured")

    azimuths = list(np.arange(az_start, az_end + 0.001, az_step))
    started = datetime.now(timezone.utc)
    power_meter = None
    if sdr_type == "b210":
        from sun_scan import _B210PowerMeter
        power_meter = _B210PowerMeter(center_freq, sample_rate, gain)

    entries = []
    guess = float(initial_edge_guess)
    sky_levels: list = []
    sky_reference = None
    last_estimator = None
    total = len(azimuths)

    def partial_profile():
        """What has been measured so far, in the same shape as a full profile."""
        return _assemble_profile(entries, started, datetime.now(timezone.utc),
                                 az_step, alt_step, alt_min, beam_fwhm_deg,
                                 clearance_fraction, integration_time_s,
                                 sdr_type, center_freq, sample_rate, gain, cfg,
                                 complete=False)

    try:
        for index, az in enumerate(azimuths):
            _check_cancelled(cancel_event, "horizon scan")
            descending = (index % 2 == 0)
            if last_estimator == "unobstructed":
                # The neighbour had no horizon within reach, so this one very
                # likely has none either. A tracking window would have to fail
                # first and then widen, which costs a whole wasted cut - 37 s
                # measured - at every open azimuth, and on a hill there are
                # runs of them.
                low, high = alt_min, alt_ceiling
            else:
                low = max(alt_min, guess - window_deg)
                high = min(alt_ceiling, guess + window_deg)
            ceiling = alt_ceiling
            attempt = 0
            # Points already measured at this azimuth, keyed by commanded
            # altitude. A retry widens or raises the cut; it does not change the
            # sky, so re-measuring the altitudes already visited is pure cost.
            # It is a large cost where it happens: chasing the dome tower's
            # clearance to 48 deg re-measured nearly forty points of solid
            # tower that had not changed since the first attempt, and took four
            # and a half minutes per azimuth instead of eighty seconds.
            measured: dict = {}
            reused = 0
            while True:
                attempt += 1
                alts = _cut_altitudes(low, high, alt_step, descending)
                points = []
                for alt in alts:
                    _check_cancelled(cancel_event, "horizon scan")
                    key = round(float(alt), 1)
                    point = measured.get(key)
                    if point is None:
                        point = _measure_at(
                            base_url, float(alt), float(az), sdr_type,
                            integration_time_s, center_freq, sample_rate, gain,
                            beam_fwhm_deg, power_meter, cancel_event,
                            position_tolerance, slew_timeout)
                        measured[key] = point
                    else:
                        reused += 1
                    points.append(point)
                fit = fit_horizon_edge(
                    [p["true_alt"] for p in points],
                    [p["power"] for p in points],
                    beam_fwhm_deg, clearance_fraction,
                    reached_mount_limit=(low <= alt_min + 0.01),
                    sky_reference=sky_reference)
                if fit.get("success") and fit.get("estimator") == "edge_fit":
                    # Finding the edge is not enough: the clearance is read off
                    # the skirt *above* it, so the cut has to reach that high or
                    # the number is an extrapolation past the last measured
                    # point. A tall obstruction hits this even though its edge
                    # sits comfortably inside the ceiling.
                    if (fit["alt_clear_fit"] > max(p["true_alt"] for p in points) - 0.5
                            and ceiling < alt_ceiling_max
                            and attempt < _MAX_CUT_ATTEMPTS):
                        ceiling = min(alt_ceiling_max,
                                      fit["alt_clear_fit"] + 2.0 * alt_step + 2.0)
                        low, high = max(alt_min, fit["edge_deg"] - window_deg), ceiling
                        descending = True
                        log.info("Az %.0f deg: edge at %.1f deg leaves no headroom "
                                 "for the clearance, extending to %.0f deg",
                                 az, fit["edge_deg"], ceiling)
                        continue
                    break
                if fit.get("estimator") == "unobstructed":
                    # Conclusive: the cut reached the mount limit and never left
                    # the sky. Chasing the ceiling from here is what turned open
                    # azimuths into twenty-five minute cuts on 2026-08-21, and on
                    # a hill there are a lot of open azimuths.
                    break
                # Widen once to the full range, then chase a tall obstruction
                # upwards rather than reporting the ceiling as the answer.
                if attempt == 1 and (high - low) < (ceiling - alt_min) - 0.5:
                    low, high = alt_min, ceiling
                    descending = True
                    continue
                all_ground = (fit.get("estimator") == "all_ground"
                              or (sky_reference is None
                                  and fit.get("contrast_fraction", 1.0)
                                  < _MIN_CONTRAST_FRACTION))
                if (all_ground and ceiling < alt_ceiling_max
                        and attempt < _MAX_CUT_ATTEMPTS):
                    ceiling = min(alt_ceiling_max, ceiling + 15.0)
                    low, high = alt_min, ceiling
                    descending = True
                    log.info("Az %.0f deg: no sky below %.0f deg, extending the "
                             "cut upwards", az, high)
                    continue
                break

            entry = {
                "az_deg": float(np.mean([p["true_az"] for p in points])),
                "az_commanded_deg": float(az),
                "direction": "down" if descending else "up",
                "attempts": attempt,
                "points_measured": len(measured),
                "points_reused": reused,
                "cut_alt_deg": [p["true_alt"] for p in points],
                "cut_power": [p["power"] for p in points],
                "cut_drive_alt_deg": [p["drive_alt"] for p in points],
                "fit": fit,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            entries.append(entry)
            last_estimator = fit.get("estimator")
            # Only a fitted edge may move the tracking window. The envelope is
            # an upper bound on contamination, not an estimate of where the
            # skyline is, and feeding it back as the next window centre
            # ratchets: the window opens above the horizon, sees only sky,
            # fails, widens, produces another envelope near the ceiling, and
            # re-arms itself. Observed on 2026-08-21 from azimuth 180 onwards,
            # where three consecutive azimuths reported ~40 deg - the ceiling -
            # having started from one bad cut due south.
            if fit.get("estimator") == "edge_fit" and fit.get("edge_deg") is not None:
                guess = float(fit["edge_deg"])
                # Every cut that found a real horizon also measured the sky, and
                # the median of those is what lets a later stepless cut be told
                # apart from a cut with no sky in it at all.
                sky_levels.append(float(fit["p_sky"]))
                sky_reference = float(np.median(sky_levels[-15:]))
            if progress_callback:
                progress_callback(index, total, {
                    "az": float(az),
                    "edge": fit.get("edge_reported_deg"),
                    "clear": fit.get("alt_clear"),
                    "estimator": fit.get("estimator"),
                    "quality": fit.get("quality"),
                })
            log.info("Az %5.1f deg: edge %s, clear above %s (%s)", az,
                     "%.2f deg" % fit["edge_reported_deg"]
                     if fit.get("edge_reported_deg") is not None else "not found",
                     "%.2f deg" % fit["alt_clear"] if fit.get("alt_clear") is not None
                     else "unknown", fit.get("estimator"))
    except BaseException as exc:
        # Abandoned - cancelled, or a fault. Attach what was measured so the
        # caller can keep it rather than discard an hour of good cuts.
        exc.partial_profile = partial_profile()
        raise
    finally:
        if power_meter is not None:
            power_meter.close()

    return _assemble_profile(entries, started, datetime.now(timezone.utc),
                             az_step, alt_step, alt_min, beam_fwhm_deg,
                             clearance_fraction, integration_time_s, sdr_type,
                             center_freq, sample_rate, gain, cfg, complete=True)


def _assemble_profile(entries, started, finished, az_step, alt_step, alt_min,
                      beam_fwhm_deg, clearance_fraction, integration_time_s,
                      sdr_type, center_freq, sample_rate, gain, cfg,
                      complete: bool) -> dict:
    return {
        "record_version": _HORIZON_RECORD_VERSION,
        "started_utc": started.isoformat(),
        "finished_utc": finished.isoformat(),
        "duration_s": (finished - started).total_seconds(),
        "az_step_deg": az_step,
        "alt_step_deg": alt_step,
        "alt_min_deg": alt_min,
        "beam_fwhm_deg": beam_fwhm_deg,
        "clearance_fraction": clearance_fraction,
        "integration_time_s": integration_time_s,
        "sdr_type": sdr_type,
        "center_freq_hz": center_freq,
        "sample_rate_hz": sample_rate,
        "gain_db": gain,
        "site_lat": cfg.get("observer_lat"),
        "site_lon": cfg.get("observer_lon"),
        "n_azimuths": len(entries),
        "entries": entries,
        "success": bool(entries),
        "complete": complete,
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


def reprocess_profile(profile: dict,
                      beam_fwhm_deg: float | None = None,
                      clearance_fraction: float | None = None) -> dict:
    """Re-derive every azimuth's edge and clearance from the stored raw cuts.

    This is what storing the cuts buys. The 2026-08-21 evening run pinned the
    clearance to the ceiling from azimuth 180 onwards through two estimator
    faults; the measurements themselves were sound, so the profile was
    recovered by running the corrected estimator over the same data rather than
    by re-observing. It also allows the clearance fraction to be changed after
    the fact, which is the whole reason it is not baked into the record.

    Returns a new profile; the original is not modified.
    """
    updated = dict(profile)
    updated["entries"] = []
    beam = beam_fwhm_deg or profile.get("beam_fwhm_deg", DEFAULT_BEAM_FWHM_DEG)
    fraction = (clearance_fraction if clearance_fraction is not None
                else profile.get("clearance_fraction", DEFAULT_CLEARANCE_FRACTION))
    alt_floor = float(profile.get("alt_min_deg", DEFAULT_ALT_MIN))
    for entry in profile.get("entries", []):
        alts = entry.get("cut_alt_deg")
        powers = entry.get("cut_power")
        if not alts or not powers or len(alts) != len(powers):
            updated["entries"].append(dict(entry))
            continue
        new_entry = dict(entry)
        new_entry["fit_as_measured"] = entry.get("fit")
        new_entry["fit"] = fit_horizon_edge(
            alts, powers, beam, fraction,
            reached_mount_limit=(min(alts) <= alt_floor + 0.01))
        updated["entries"].append(new_entry)
    updated["beam_fwhm_deg"] = beam
    updated["clearance_fraction"] = fraction
    updated["reprocessed_utc"] = datetime.now(timezone.utc).isoformat()
    return updated


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
    what = ("the altitude above which ground pickup falls below "
            f"{100 * profile.get('clearance_fraction', DEFAULT_CLEARANCE_FRACTION):g}% "
            "of the sky-to-ground step"
            if use == "clearance" else "the geometric skyline")

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


def direction_bias(profile: dict) -> dict:
    """Compare cuts taken downwards against those taken upwards.

    The serpentine saves a long slew per azimuth but alternates the direction
    the altitude axis is approached from, so any backlash lands as a systematic
    difference between neighbouring azimuths. A real horizon has no reason to
    zigzag with the parity of the azimuth index, so this is a clean test.
    """
    down, up = [], []
    for entry in profile.get("entries", []):
        fit = entry.get("fit") or {}
        if fit.get("estimator") != "edge_fit":
            continue
        (down if entry.get("direction") == "down" else up).append(
            (float(entry["az_deg"]), float(fit["edge_reported_deg"])))
    if len(down) < 3 or len(up) < 3:
        return {"available": False}
    # Compare each up-cut against the mean of its two down-cut neighbours, so a
    # genuine slope in the horizon cancels and only the parity term survives.
    #
    # Neighbours are matched by proximity, not by arithmetic on the commanded
    # azimuth: the azimuth recorded here is the true-frame position the mount
    # actually reached, so it is never exactly five degrees from its neighbour
    # and an equality test would silently find nothing on real data while
    # working perfectly in simulation.
    spacing = float(profile.get("az_step_deg", DEFAULT_AZ_STEP))
    tolerance = 0.4 * spacing
    residuals = []
    for az, edge in up:
        neighbours = [e for a, e in down
                      if abs(abs(a - az) - spacing) <= tolerance]
        if len(neighbours) == 2:
            residuals.append(edge - 0.5 * (neighbours[0] + neighbours[1]))
    if len(residuals) < 3:
        return {"available": False}
    residuals = np.array(residuals)
    mean = float(residuals.mean())
    err = float(residuals.std(ddof=1) / math.sqrt(len(residuals)))
    return {
        "available": True,
        "n": len(residuals),
        "up_minus_down_deg": mean,
        "uncertainty_deg": err,
        "significance": abs(mean) / err if err else float("inf"),
    }


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

def generate_horizon_plot(profile: dict,
                          output_path: str = "horizon_profile.png") -> str:
    """Three panels: the profile, a sample cut, and what the run found."""
    if not MATPLOTLIB_AVAILABLE:
        raise ImportError("matplotlib is required")

    entries = profile.get("entries", [])
    if not entries:
        raise ValueError("horizon profile has no entries")

    az = np.array([e["az_deg"] for e in entries])
    edge = np.array([(e["fit"] or {}).get("edge_reported_deg", np.nan)
                     for e in entries], dtype=float)
    clear = np.array([(e["fit"] or {}).get("alt_clear", np.nan)
                      for e in entries], dtype=float)
    envelope = np.array([(e["fit"] or {}).get("estimator") == "envelope"
                         for e in entries])

    fig = plt.figure(figsize=(14, 9))
    ax = fig.add_subplot(2, 2, (1, 2))
    ax.fill_between(az, 0, clear, color="#ff6b6b", alpha=0.18,
                    label="Obstructed (below clearance)")
    ax.plot(az, edge, "o-", color="#00d4ff", markersize=4, linewidth=1.5,
            label="Horizon edge")
    ax.plot(az, clear, "-", color="#ff6b6b", linewidth=1.5,
            label="Clearance altitude")
    if envelope.any():
        ax.plot(az[envelope], edge[envelope], "s", color="#ffaa00", markersize=7,
                markerfacecolor="none", markeredgewidth=1.5,
                label="Envelope (step model did not apply)")
    ax.set_xlabel("True azimuth (deg)")
    ax.set_ylabel("Altitude (deg)")
    ax.set_title("Obstructed horizon")
    ax.set_xlim(0, 360)
    ax.set_xticks(np.arange(0, 361, 30))
    ax.legend(loc="upper right", fontsize=9)

    # A representative cut: the one whose edge is highest, since that is the
    # one worth eyeballing.
    ax = fig.add_subplot(2, 2, 3)
    worst = int(np.nanargmax(np.where(np.isnan(edge), -np.inf, edge)))
    entry = entries[worst]
    fit = entry["fit"] or {}
    cut_alt = np.array(entry["cut_alt_deg"])
    cut_p = np.array(entry["cut_power"])
    ax.plot(cut_alt, cut_p, "o", color="#00d4ff", markersize=5, label="Measured")
    if fit.get("estimator") == "edge_fit":
        fine = np.linspace(cut_alt.min(), cut_alt.max(), 200)
        ax.plot(fine, horizon_step(fine, fit["p_sky"], fit["contrast"],
                                   fit["edge_deg"], fit["width_sigma_deg"]),
                "-", color="#ff6b6b", linewidth=2, label="Step fit")
        ax.axvline(fit["edge_deg"], color="#00ff88", linestyle="--", linewidth=1,
                   label="Edge")
    if fit.get("alt_clear") is not None:
        ax.axvline(fit["alt_clear"], color="#ffaa00", linestyle=":", linewidth=1.5,
                   label="Clearance")
    ax.set_xlabel("True altitude (deg)")
    ax.set_ylabel("Total power")
    ax.set_title("Cut at azimuth %.0f deg" % entry["az_deg"])
    ax.legend(fontsize=9)

    ax = fig.add_subplot(2, 2, 4)
    ax.axis("off")
    usable = int(np.sum(~np.isnan(edge)))
    bias = direction_bias(profile)
    lines = [
        "Horizon Scan",
        "",
        f"Azimuths: {len(entries)} at {profile.get('az_step_deg')}° spacing",
        f"Usable:   {usable}"
        + (f"  ({int(envelope.sum())} by envelope)" if envelope.any() else ""),
        f"Duration: {profile.get('duration_s', 0) / 60:.0f} min",
        "",
        f"Highest obstruction: {np.nanmax(edge):.1f}° at az {az[worst]:.0f}°",
        f"Median edge:         {np.nanmedian(edge):.1f}°",
        f"Median clearance:    {np.nanmedian(clear):.1f}°",
        "",
        f"Clearance fraction: {profile.get('clearance_fraction')}",
        f"Beam FWHM assumed:  {profile.get('beam_fwhm_deg')}°",
    ]
    if bias.get("available"):
        lines += ["",
                  "Up-cuts minus down-cuts:",
                  f"  {bias['up_minus_down_deg']:+.3f} ± "
                  f"{bias['uncertainty_deg']:.3f}° "
                  f"({bias['significance']:.1f} sigma)"]
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

    parser = argparse.ArgumentParser(description="Map the local horizon")
    parser.add_argument("--az-start", type=float, default=5.0)
    parser.add_argument("--az-end", type=float, default=350.0)
    parser.add_argument("--az-step", type=float, default=DEFAULT_AZ_STEP)
    parser.add_argument("--alt-step", type=float, default=DEFAULT_ALT_STEP)
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW_DEG)
    parser.add_argument("--integration", type=float, default=DEFAULT_INTEGRATION_S)
    parser.add_argument("--sdr", default="b210", choices=["b210", "rtlsdr", "demo"])
    parser.add_argument("--srt-url", default=None)
    parser.add_argument("--output", default="horizon_profile.png")
    parser.add_argument("--save", action="store_true",
                        help="write the profile to horizon_profile.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(message)s")

    def progress(idx, total, info):
        print("  [%3d/%3d] az=%5.1f  edge=%s  clear=%s  %s" % (
            idx + 1, total, info["az"],
            "%6.2f" % info["edge"] if info["edge"] is not None else "  none",
            "%6.2f" % info["clear"] if info["clear"] is not None else "  none",
            info["estimator"]), flush=True)

    profile = horizon_scan(
        az_start=args.az_start, az_end=args.az_end, az_step=args.az_step,
        alt_step=args.alt_step, window_deg=args.window,
        integration_time_s=args.integration, sdr_type=args.sdr,
        srt_url=args.srt_url, progress_callback=progress)

    print()
    print("=" * 60)
    print("HORIZON SCAN COMPLETE")
    print("=" * 60)
    print("  Azimuths: %d in %.1f min" % (profile["n_azimuths"],
                                          profile["duration_s"] / 60))
    bias = direction_bias(profile)
    if bias.get("available"):
        print("  Up minus down: %+.3f +- %.3f deg (%.1f sigma)"
              % (bias["up_minus_down_deg"], bias["uncertainty_deg"],
                 bias["significance"]))
    if args.save:
        print("  Saved: %s" % save_horizon_profile(profile))
    if MATPLOTLIB_AVAILABLE:
        print("  Plot:  %s" % generate_horizon_plot(profile, args.output))


if __name__ == "__main__":
    main()


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
