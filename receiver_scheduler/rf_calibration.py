#!/usr/bin/env python3
"""Turn counts into kelvin, against the galactic plane, now.

After the bandpass template has divided out the instrument's shape (bandpass.py)
what is left is a flat spectrum in arbitrary counts:

    P(v) = G * (T_sys + T_A(v))

with two unknowns - the gain G in counts per kelvin, and the system temperature.
The simulator supplies T_A(v) in kelvin for any direction, beam-weighted from
HI4PI through the beam that was actually measured off the Sun, so a single
observation fixes both: within one plane spectrum T_A runs from a fraction of a
kelvin in the line-free channels to around 100 K at the peak, and a straight line
through counts against kelvin gives G as its slope and G*T_sys as its intercept.
Measured spread available for the fit, through a 5.16 degree beam: 0.4 K median
against a 96.9 K peak toward l=80, and 0.1 K against 1.3 K toward the Lockman
Hole, which is why the plane is the calibrator and the Lockman Hole is the check.

The pointing is chosen *now* rather than named. Gain drifts, so a calibration is
only worth what it was worth at the time, and a fixed source is unavailable for
most of the day. `plane_target_now` walks the plane and picks the best patch of
it currently up.

T_sys is bounded below at 50 K because the SAWbird+ H1 alone is 51-67 K by its
datasheet, before any spillover, sky or ground. A fit returning less than that
has not discovered a quiet receiver, it has gone wrong - usually a bandpass
template applied to the wrong tuning, or a slew that did not arrive. The bound is
a genuine constraint on the least squares rather than a check afterwards, and
whether it ended up active is reported, because a calibration sitting exactly on
its own floor is not a measurement.
"""

import json
import math
import os
from datetime import datetime, timezone

import numpy as np

import bandpass
from observatory import (SITE_HEIGHT_M, SITE_LAT_DEG, SITE_LON_DEG,
                         SITE_NAME)

HERE = os.path.dirname(os.path.abspath(__file__))
CALIBRATION_FILE = os.path.join(HERE, "gain_calibration.json")
SIMULATOR_DIR = os.path.join(os.path.dirname(HERE), "astro_simulator")

H1_REST_FREQ_HZ = 1420.405752e6
C_M_S = 299792458.0

# The SAWbird+ H1's own noise temperature is 51/59/67 K (min/typ/max), so
# nothing below this is physical once spillover, sky and ground are added.
MIN_T_SYS_K = 50.0

# Preferred floor on target altitude. Lower is worse but not fatal, and the
# reason matters: ground spillover is *additive*, so it lands in T_sys and leaves
# the slope - the counts-per-kelvin gain, which is what most of this is for -
# largely alone. What a low target really costs is atmospheric attenuation
# multiplying the sky, and at 1420 MHz that is about 3% at airmass 3. So a low
# calibration is a good G with an elevation-specific T_sys, not a bad
# calibration, and refusing to produce one is worse than producing one and
# saying what it is.
MIN_TARGET_ALT_DEG = 25.0

# Above this, a fit is reporting something no working system does. The SAWbird
# is 59 K and spillover, sky and ground on a dish this size bring the total to
# order 100-150 K; 250 K would be a badly illuminated dish and 400 K is not a
# system temperature at all. Measured 2026-08-24: a run that recorded while the
# mount was still slewing across fifty degrees of sky fitted 467 K, and nothing
# in the result said so - the floor at 50 K only catches errors of the opposite
# sign. This is a flag rather than a bound: a genuinely hot system is worth
# seeing, not clamping.
IMPLAUSIBLE_T_SYS_K = 300.0

# Floors to fall back through when nothing clears the preferred one. Each step
# down is reported, so a compromised calibration announces itself.
FALLBACK_ALT_FLOORS_DEG = (20.0, 15.0, 12.0)

# How far off the plane to look. H I does not stop at b=0 - measured on
# 2026-08-24, when the whole plane had sunk below 30 degrees, l=108 b=+8 was 5
# degrees higher than the best plane longitude and still showed a 70.8 K peak
# against the Lockman Hole's 1.3 K. Insisting on the plane is what made the
# first version refuse to calibrate at all.
MAX_ABS_GLAT_DEG = 40.0

# A calibration wants dynamic range. Below this the fit is too poorly determined
# to be worth the mount wear, and the caller is told so rather than handed a
# number with an invisible error bar.
MIN_USEFUL_PEAK_K = 15.0

# How much of the band either side of the LO artefact to keep out of the fit.
# Generous: the artefact's wings are shallow, and the model has no structure
# here to lose, so there is nothing to trade against being careful.
DC_EXCLUSION_HZ = 40e3

# Main-beam efficiency applied to the model. One, deliberately.
#
# The simulator multiplies its antenna temperature by this, and it is the
# fraction of the total pattern power inside the main beam - so applying it
# asserts that the sidelobes see *nothing*. For galactic H I that is plainly
# false: the sidelobes see sky of comparable brightness a few degrees away, so
# the dilution it models largely does not happen, and the line is not weakened
# in proportion to it. What the sidelobes do see that the main beam does not is
# the ground, and that is a constant - it belongs in T_sys, additively, which is
# exactly where the fit already puts it.
#
# The other half of the argument is that the beam here is measured rather than
# assumed: 18 solar scans on 2026-08-22 gave 5.173 +/- 0.020 deg, 5.164 after
# deconvolving the solar disk (astro_simulator/instrument.py). The
# beam-weighting is therefore the real instrument's, not a model of it, and
# putting an efficiency in front of it discounts a measurement twice.
#
# This was briefly set to the simulator's CLI default of 0.7, which lowered a
# fitted system temperature from 372 K to 260 K - a comfortable-looking number
# obtained by scaling the data until it looked right. The 372 K is the honest
# figure and it should be explained rather than absorbed: see the note in
# calibrate_observation about separating spillover from receiver noise.
MAIN_BEAM_EFFICIENCY = 1.0

# Bumped whenever the reduction changes in a way that alters the fit, so a
# calibration stored by older code is not silently redrawn against a newer
# reduction. Version 2 excludes the LO artefact and takes the main-beam
# efficiency as one; a version 1 fit plotted against it disagreed by tenths of
# a kelvin everywhere, which looks exactly like a calibration error. Version 3
# adds the diffuse continuum map, which shifts the intercept.
REDUCTION_VERSION = 3

CALIBRATION_VERSION = 1


# --------------------------------------------------------------------------
# choosing where to point


def _sky_position(glon, glat, when, lat, lon, elevation_m):
    """Alt/az of a galactic direction, at a time and place."""
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time

    site = EarthLocation(lat=lat * u.deg, lon=lon * u.deg,
                         height=elevation_m * u.m)
    frame = AltAz(obstime=Time(when), location=site)
    c = SkyCoord(l=np.atleast_1d(glon) * u.deg, b=np.atleast_1d(glat) * u.deg,
                 frame="galactic").transform_to(frame)
    return np.asarray(c.alt.deg, float), np.asarray(c.az.deg, float)


def _in_obstructed_sector(az, alt, sectors):
    """True if this azimuth is blocked at this altitude.

    Sectors are [az_min, az_max, min_alt] and are the treeline: the same list
    the calibration day and the pointing fit already use.
    """
    for entry in sectors or []:
        try:
            az_min, az_max, floor = (float(entry[0]), float(entry[1]),
                                     float(entry[2]))
        except (TypeError, ValueError, IndexError):
            continue
        inside = (az_min <= az <= az_max if az_min <= az_max
                  else az >= az_min or az <= az_max)
        if inside and alt < floor:
            return True
    return False


def calibration_target_now(when=None, lat=SITE_LAT_DEG, lon=SITE_LON_DEG,
                           elevation_m=SITE_HEIGHT_M, min_alt_deg=MIN_TARGET_ALT_DEG,
                           obstruction_sectors=None, lon_step_deg=6.0,
                           lat_step_deg=6.0, max_abs_glat=MAX_ABS_GLAT_DEG,
                           sim=None, fallback_floors=FALLBACK_ALT_FLOORS_DEG):
    """The best direction available for a gain calibration at this moment.

    Searches galactic longitude and latitude rather than the plane alone, because
    the plane spends much of the day too low and the emission either side of it is
    ample: a 70 K peak at b=+8 is a fine calibrator when b=0 has set.

    Scored as the simulated line peak weighted by sin(altitude). Brightness sets
    the lever arm the fit gets; the altitude term stands in for airmass and for
    the ground the sidelobes of a small dish always see.

    Falls through progressively lower altitude floors rather than refusing, and
    says in the result which floor it had to use and whether the peak is weak.
    One needs a calibration even when conditions are poor - what one must not
    have is a poor calibration that looks like a good one.
    """
    when = when or datetime.now(timezone.utc)
    sim = sim or load_simulator()

    lons = np.arange(0.0, 360.0, lon_step_deg)
    lats = np.arange(-max_abs_glat, max_abs_glat + 1e-9, lat_step_deg)
    grid_l, grid_b = np.meshgrid(lons, lats)
    grid_l, grid_b = grid_l.ravel(), grid_b.ravel()
    # One vectorised transform for the whole grid; the simulator is only asked
    # about directions that survive the altitude and obstruction cuts.
    alt, az = _sky_position(grid_l, grid_b, when, lat, lon, elevation_m)

    floors = [float(min_alt_deg)] + [float(f) for f in (fallback_floors or ())
                                     if float(f) < float(min_alt_deg)]
    for attempt, floor in enumerate(floors):
        best = None
        for l, b, a, z in zip(grid_l, grid_b, alt, az):
            if a < floor:
                continue
            if _in_obstructed_sector(z, a, obstruction_sectors):
                continue
            peak = float(np.nanmax(sim.spectrum(float(l), float(b))[1]))
            score = peak * math.sin(math.radians(a))
            if best is None or score > best["score"]:
                best = {"glon": float(l), "glat": float(b),
                        "alt_deg": float(a), "az_deg": float(z),
                        "expected_peak_k": peak, "score": float(score)}
        if best is None:
            continue
        notes = []
        if attempt:
            notes.append("nothing was above %.0f deg, so the floor was lowered "
                         "to %.0f deg" % (min_alt_deg, floor))
        if best["expected_peak_k"] < MIN_USEFUL_PEAK_K:
            notes.append("the brightest direction available peaks at only "
                         "%.0f K, so the gain will be poorly determined"
                         % best["expected_peak_k"])
        best["alt_floor_used_deg"] = floor
        best["compromised"] = bool(notes)
        best["notes"] = notes
        return best
    return None


def plane_target_now(*args, **kwargs):
    """Deprecated: the plane alone is too often below the horizon."""
    kwargs.setdefault("max_abs_glat", 0.0)
    kwargs.setdefault("lat_step_deg", 1.0)
    return calibration_target_now(*args, **kwargs)


def _sky_separation_deg(l1, b1, l2, b2):
    """Great-circle angle between two galactic directions."""
    p1, p2 = math.radians(b1), math.radians(b2)
    dl = math.radians(l1 - l2)
    return math.degrees(math.acos(max(-1.0, min(1.0,
        math.sin(p1) * math.sin(p2)
        + math.cos(p1) * math.cos(p2) * math.cos(dl)))))


COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass_point(az_deg):
    """'NNE' and so on, because an operator recognises a skyline by direction."""
    return COMPASS[int((float(az_deg) % 360.0) / 22.5 + 0.5) % 16]


def calibration_candidates_now(when=None, lat=SITE_LAT_DEG, lon=SITE_LON_DEG,
                               elevation_m=SITE_HEIGHT_M,
                               min_alt_deg=MIN_TARGET_ALT_DEG,
                               obstruction_sectors=None, lon_step_deg=4.0,
                               lat_step_deg=4.0,
                               max_abs_glat=MAX_ABS_GLAT_DEG, sim=None,
                               n=8, min_separation_deg=25.0):
    """A ranked, spread-out list of directions worth calibrating against.

    A single automatic choice is brittle here, because the software does not
    know the skyline: the obstruction sectors only describe the eastern
    treeline, and the observatory's dome towers are not in them at all. On
    2026-08-24 the best-scoring direction came out at azimuth 15 degrees, which
    is straight into a tower. So this proposes and a person disposes.

    Suggestions are forced apart by `min_separation_deg`, without which the list
    is five neighbouring points on the same bright patch pointing the same way -
    a menu with one item on it. Each carries its compass point, since that is
    what an operator recognises a skyline by.
    """
    when = when or datetime.now(timezone.utc)
    sim = sim or load_simulator()

    lons = np.arange(0.0, 360.0, lon_step_deg)
    lats = np.arange(-max_abs_glat, max_abs_glat + 1e-9, lat_step_deg)
    gl, gb = np.meshgrid(lons, lats)
    gl, gb = gl.ravel(), gb.ravel()
    alt, az = _sky_position(gl, gb, when, lat, lon, elevation_m)

    scored = []
    for l, b, a, z in zip(gl, gb, alt, az):
        if a < min_alt_deg:
            continue
        if _in_obstructed_sector(z, a, obstruction_sectors):
            continue
        peak = float(np.nanmax(sim.spectrum(float(l), float(b))[1]))
        scored.append({"glon": float(l), "glat": float(b), "alt_deg": float(a),
                       "az_deg": float(z), "expected_peak_k": peak,
                       "compass": compass_point(z),
                       "score": peak * math.sin(math.radians(a))})
    scored.sort(key=lambda c: -c["score"])

    picked = []
    for cand in scored:
        if any(_sky_separation_deg(cand["glon"], cand["glat"],
                                   p["glon"], p["glat"]) < min_separation_deg
               for p in picked):
            continue
        picked.append(cand)
        if len(picked) >= n:
            break
    return picked


# --------------------------------------------------------------------------
# what the sky should look like


_SIM_CACHE = {}


def load_simulator(bandwidth_hz=2.0e6, dish_m=3.0, nchan=None, compact=None,
                   eta=MAIN_BEAM_EFFICIENCY):
    """A DishSimulator on the shipped compact HI4PI cube.

    Cached by configuration: building one unpacks a 23 MB compressed cube and
    costs 2.7 seconds, against 2.9 ms for a spectrum out of it. A sky search asks
    for several hundred spectra, so paying the load once rather than per request
    is the difference between a usable page and one that appears to hang.
    """
    key = (bandwidth_hz, dish_m, nchan, compact, eta)
    if key in _SIM_CACHE:
        return _SIM_CACHE[key]
    import sys
    if SIMULATOR_DIR not in sys.path:
        sys.path.insert(0, SIMULATOR_DIR)
    import astro_simulator as A

    # Same surveyed position the rest of the scheduler uses. The simulator's
    # own default now comes from the same constant, so this is belt and braces
    # rather than a correction - but it keeps the frame explicit at the point
    # where a velocity correction is about to be computed.
    A.set_site(SITE_NAME, SITE_LAT_DEG, SITE_LON_DEG, SITE_HEIGHT_M)

    # The diffuse 1420 MHz continuum, not just the handful of bright sources.
    # It enters the model as a flat offset, so it never touches the slope and
    # the counts-per-kelvin gain is the same with or without it - but the offset
    # is exactly what the intercept measures, so leaving it out puts the whole
    # of it into the fitted system temperature. Measured on 2026-08-24: 0.12 K
    # toward the Lockman Hole, 0.66 K at l=36 b=40, and 6.13 K on the plane at
    # l=80 - largest precisely where a gain calibration is meant to be made.
    # The bright discrete sources were always included; only the map was missing.
    sim = A.DishSimulator(
        cube_path=None, bw_hz=bandwidth_hz, dish_m=dish_m, eta=eta,
        nchan=nchan, tsys=None, tint=60.0,
        compact_path=compact or os.path.join(SIMULATOR_DIR,
                                             "hi4pi_compact.npz.xz"),
        continuum_path=os.path.join(SIMULATOR_DIR,
                                    "continuum_1420_compact.npz.xz"))
    _SIM_CACHE[key] = sim
    return sim


def simulated_spectrum(glon, glat, obstime, sim=None, bandwidth_hz=2.0e6):
    """(frequency_hz, T_A) for a direction, on the topocentric sky.

    The simulator works in LSR; the recorded spectra are topocentric, in true
    sky frequency. Both are shifted here rather than there so the observation is
    never rewritten - and at the observation's own epoch, because the barycentric
    term moves by a couple of km/s in a week.
    """
    import sys
    if SIMULATOR_DIR not in sys.path:
        sys.path.insert(0, SIMULATOR_DIR)
    import astro_simulator as A

    sim = sim or load_simulator(bandwidth_hz=bandwidth_hz)
    out = sim.spectrum(float(glon), float(glat))
    v_lsr, ta = np.asarray(out[0], float), np.asarray(out[1], float)
    dv = A.frame_offset(float(glon), float(glat), "topo", obstime)
    v_topo = v_lsr + dv
    freq = H1_REST_FREQ_HZ * (1.0 - v_topo / C_M_S)     # radio convention
    order = np.argsort(freq)
    return freq[order], ta[order]


def _bin_to(freq_edges, freq_hz, values):
    """Average `values` into bins, ignoring empty ones."""
    idx = np.digitize(freq_hz, freq_edges) - 1
    n = freq_edges.size - 1
    total = np.zeros(n)
    count = np.zeros(n)
    ok = (idx >= 0) & (idx < n) & np.isfinite(values)
    np.add.at(total, idx[ok], values[ok])
    np.add.at(count, idx[ok], 1.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(count > 0, total / np.maximum(count, 1), np.nan)
    return out, count


# --------------------------------------------------------------------------
# the fit


def fit_gain(observed_counts, model_k, min_t_sys_k=MIN_T_SYS_K):
    """Least squares for (G, T_sys) in counts = G * (T_sys + T_A).

    The bound on T_sys is imposed on the fit, not checked after it. With a single
    inequality on a two-parameter linear problem the solution is the textbook
    active-set one: take the unconstrained fit if it satisfies the bound,
    otherwise pin T_sys to the bound and fit the one remaining parameter.
    """
    counts = np.asarray(observed_counts, float)
    ta = np.asarray(model_k, float)
    ok = np.isfinite(counts) & np.isfinite(ta)
    if ok.sum() < 8:
        raise ValueError("only %d usable bins - too few to calibrate" % ok.sum())
    counts, ta = counts[ok], ta[ok]

    A = np.column_stack([ta, np.ones_like(ta)])
    (slope, intercept), *_ = np.linalg.lstsq(A, counts, rcond=None)
    constrained = False
    if slope <= 0 or intercept / slope < min_t_sys_k:
        constrained = True
        x = min_t_sys_k + ta
        slope = float(np.dot(counts, x) / np.dot(x, x))
        intercept = slope * min_t_sys_k

    t_sys = intercept / slope if slope else float("nan")
    predicted = slope * (t_sys + ta)
    resid_k = (counts - predicted) / slope if slope else np.full_like(counts, np.nan)
    return {
        "gain_counts_per_k": float(slope),
        "t_sys_k": float(t_sys),
        "t_sys_bound_active": bool(constrained),
        "t_sys_implausible": bool(np.isfinite(t_sys)
                                  and t_sys > IMPLAUSIBLE_T_SYS_K),
        "implausible_above_k": float(IMPLAUSIBLE_T_SYS_K),
        "min_t_sys_k": float(min_t_sys_k),
        "assumed_main_beam_efficiency": float(MAIN_BEAM_EFFICIENCY),
        "reduction_version": REDUCTION_VERSION,
        "n_bins": int(ok.sum()),
        "model_peak_k": float(np.nanmax(ta)),
        "model_span_k": float(np.nanmax(ta) - np.nanmin(ta)),
        "residual_rms_k": float(np.std(resid_k)),
        # A flat model has no variance, so the correlation is undefined rather
        # than zero. Report it as nan instead of letting numpy warn about it:
        # this is the degenerate case the caller most needs to notice.
        "correlation": (float(np.corrcoef(ta, counts)[0, 1])
                        if np.std(ta) > 0 and np.std(counts) > 0
                        else float("nan")),
    }


def reduce_for_fit(path, glon, glat, sim=None, bandwidth_hz=None):
    """Everything the fit sees, so a plot of the fit can see exactly the same.

    Returns the simulator's frequency grid, its antenna temperature, the
    observation binned onto that grid, and which bins carry data. Kept apart
    from the fit itself so the picture and the number can never drift: a plot
    drawn from a second, slightly different reduction is worse than no plot,
    because it looks like corroboration.

    The spectrum is bandpass corrected first and then binned *down* onto the
    simulator's channels. HI4PI's native resolution is 1.29 km/s against 0.10
    km/s per recorded channel, so the model cannot say anything at our
    resolution, and regressing fine channels against an interpolated coarse
    curve would be fitting noise against a smooth line and calling the
    correlation good.
    """
    from observation_plot import read_observation

    freq_hz, spectra, stamps, _taus, header = read_observation(path)
    corrected, note = bandpass.apply_bandpass(freq_hz, spectra, header)
    if "not bandpass corrected" in note:
        raise ValueError("cannot calibrate an uncorrected spectrum: " + note)

    obstime = (datetime.fromtimestamp(float(stamps[0]), tz=timezone.utc)
               if np.size(stamps) else datetime.now(timezone.utc))
    if bandwidth_hz is None:
        bandwidth_hz = float(header.get("sample_rate_requested_hz")
                             or header.get("sample_rate_hz") or 2.0e6)

    sim_f, sim_ta = simulated_spectrum(glon, glat, obstime, sim, bandwidth_hz)
    mid = 0.5 * (sim_f[1:] + sim_f[:-1])
    edges = np.concatenate([[sim_f[0] - (mid[0] - sim_f[0])], mid,
                            [sim_f[-1] + (sim_f[-1] - mid[-1])]])

    # Channels outside the bandpass template are NaN in every record; averaging
    # them warns and yields NaN, which _bin_to would then have to filter anyway.
    with np.errstate(invalid="ignore"):
        keep_ch = np.isfinite(corrected).any(axis=0)
    mean_counts = np.full(freq_hz.shape, np.nan)
    mean_counts[keep_ch] = np.nanmean(corrected[:, keep_ch], axis=0)
    binned, count = _bin_to(edges, freq_hz, mean_counts)
    usable = count > 0

    # Drop the LO artefact. It is a hole tens of percent deep at a known
    # frequency, and the model puts nothing there, so in the regression it is a
    # lone point far below the line - which is exactly where a least squares is
    # most easily led. The bandpass template already keeps it out of its own
    # fit; it has to be kept out of this one, for the same reason.
    artefact = header.get("dc_artefact_freq_hz") or header.get("center_freq_hz")
    if artefact is not None:
        usable = usable & (np.abs(sim_f - float(artefact)) > DC_EXCLUSION_HZ)

    return {
        "sim_freq_hz": sim_f, "sim_ta_k": sim_ta,
        "binned_counts": binned, "usable": usable,
        "dc_artefact_freq_hz": (float(artefact) if artefact is not None else None),
        "obstime": obstime, "header": header, "bandpass_note": note,
        "bandwidth_hz": bandwidth_hz,
    }


def calibrate_observation(path, glon, glat, sim=None, min_t_sys_k=MIN_T_SYS_K,
                          bandwidth_hz=None):
    """Fit gain and T_sys from a recorded observation of the plane."""
    red = reduce_for_fit(path, glon, glat, sim, bandwidth_hz)
    usable = red["usable"]
    obstime, header, note = red["obstime"], red["header"], red["bandpass_note"]
    result = fit_gain(red["binned_counts"][usable], red["sim_ta_k"][usable],
                      min_t_sys_k)
    result.update({
        "version": CALIBRATION_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "observed_utc": obstime.isoformat(timespec="seconds"),
        "source_file": os.path.basename(path),
        "glon": float(glon), "glat": float(glat),
        "bandpass_note": note,
        "config": {
            "lo_hz": float(header.get("center_freq_hz", 0.0)),
            "sample_rate_hz": float(header.get("sample_rate_hz", 0.0)),
            "gain_db": (float(header["gain_db"])
                        if header.get("gain_db") is not None else None),
        },
    })
    return result


def save_calibration(cal, path=CALIBRATION_FILE):
    with open(path, "w") as fh:
        json.dump(cal, fh, indent=2)
    return path


def load_calibration(path=CALIBRATION_FILE):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            cal = json.load(fh)
    except (OSError, ValueError):
        return None
    if cal.get("version") != CALIBRATION_VERSION:
        return None
    return cal


def counts_to_kelvin(counts, cal):
    """Antenna temperature from corrected counts, minus the system term."""
    if not cal or not cal.get("gain_counts_per_k"):
        return None
    return np.asarray(counts, float) / cal["gain_counts_per_k"] - cal["t_sys_k"]
