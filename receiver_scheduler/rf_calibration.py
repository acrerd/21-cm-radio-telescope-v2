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

# Two bands for reporting a hot system, not a claim that it is broken. The
# SAWbird is 59 K and spillover, sky and ground on a dish this size bring the
# total to order 100-150 K, so anything much above that is worth noticing -
# but this telescope measures 340-372 K and works, most likely because of loss
# ahead of the LNA (see _implied_loss_db), so "hotter than any working system"
# was simply wrong about its own observatory.
#
# What the flag is for is catching the errors the 50 K floor cannot, since that
# only catches errors of one sign. Measured 2026-08-24: a run that recorded
# while the mount was still slewing across fifty degrees of sky fitted 467 K,
# and nothing in the result said so.
#
# Flags rather than bounds: a genuinely hot system is worth seeing, not
# clamping.
HIGH_T_SYS_K = 200.0
VERY_HIGH_T_SYS_K = 300.0

# For turning a system temperature into something that can be acted on. A lossy
# element of factor L at physical temperature T_amb ahead of the LNA gives,
# referred to the sky:
#
#     T_sys = (L - 1) * T_amb + L * T_rx
#
# so a measured T_sys inverts to a loss. It is the honest reading of an excess
# that has survived the alternatives: ground spillover was excluded by
# measurement (baseline power at altitude 34 and 78.5 agreed to 3.6%, and
# spillover grows towards the horizon), the beam efficiency by argument (the
# beam is measured and the sidelobes see sky), and the missing continuum by
# arithmetic (0.7 K here). A corroded probe or connector is a constant loss,
# which is exactly the elevation-independent excess that was left.
#
# The inversion assumes the whole excess is loss at ambient, so it is an upper
# bound on the loss rather than a measurement of it. What makes it testable
# without dismantling anything is the ambient term: dT_sys/dT_amb = L - 1, so a
# genuinely lossy front end makes the system temperature track the air
# temperature, at nearly a kelvin per kelvin here. Neither spillover nor
# receiver noise does that.
# One polarisation - the B210 is opened with channels=[0] - and no factor of a
# half belongs anywhere in the temperature scale because of it. For a
# single-polarisation antenna looking at unpolarised sky the antenna temperature
# *is* the beam-weighted brightness temperature: the half-power split is already
# inside the definition, and inside the antenna theorem the simulator uses, where
# a source of flux density S gives T_A = S*A_e/(2k). The factor to insert is
# already there. Adding another would halve every calibrated spectrum.
#
# Where the single polarisation does belong is the radiometer equation, and the
# simulator's npol defaults to 1 and enters only the noise expression, never the
# antenna temperature. This calibration passes tsys=None, so no simulated noise
# is generated at all and npol is unused here.
#
# Confirmed from the data rather than the wiring, on the 2026-08-24 run: measured
# per-record channel noise 2.394%, against 2.574% predicted for one polarisation
# and 1.820% for two.
RECEIVER_T_RX_K = 59.0        # SAWbird+ H1 datasheet, typical
AMBIENT_T_K = 290.0

# Zenith opacity of the atmosphere at 1.4 GHz, in nepers. Almost all of it is
# molecular oxygen; water vapour contributes little this far from the 22 GHz
# line, so it barely moves with humidity - which is what makes a single number
# defensible here at all.
#
# This is the *physical expectation*, not a site measurement. Fitting ln(flux)
# against airmass on the 2026-08-25 solar track (Sun 30.2 deg down to 14.2 deg,
# airmass 1.99 to 4.08) gave 0.0149, but that number absorbs everything else
# that worsens towards the horizon - pointing-model error at low altitude,
# ground spillover, the beam starting to clip the treeline - so it is an upper
# bound on the opacity rather than a measurement of it. Replace this with a
# real tipping curve if the 2-3% ever matters.
ZENITH_OPACITY_NEPERS = 0.010


def airmass(alt_deg):
    """Airmass at this elevation, by Kasten and Young (1989).

    Not 1/sin(alt): that diverges at the horizon and is already 12% high at
    5 degrees, which is exactly where a setting Sun spends its most interesting
    minutes. Agrees with the simple form to better than 0.2% above 20 degrees.
    """
    h = float(alt_deg)
    if h < -1.0:
        return float("inf")
    return 1.0 / (math.sin(math.radians(h))
                  + 0.50572 * (h + 6.07995) ** -1.6364)


def atmospheric_transmission(alt_deg, tau=ZENITH_OPACITY_NEPERS):
    """Fraction of a source's flux that survives the atmosphere at this elevation.

    Divide a measured flux by this to get the flux above the atmosphere, which
    is what a published index like the RSTN 1415 MHz value quotes. The effect
    is small - 1% at the zenith, 3% at airmass 4 - and is applied for display
    only. The recorded spectra stay as they were measured.
    """
    a = airmass(alt_deg)
    if not math.isfinite(a):
        return float("nan")
    return math.exp(-float(tau) * a)


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

# Main-beam efficiency: taken from instrument.py, where the reasoning lives
# beside the measured beam that makes it one. Not restated here - it was in two
# places with two values once already, and the two tabs duly disagreed by 1/0.7
# about the same patch of sky.
from observatory import MAIN_BEAM_EFFICIENCY

# Bumped whenever the reduction changes in a way that alters the fit, so a
# calibration stored by older code is not silently redrawn against a newer
# reduction. Version 2 excludes the LO artefact and takes the main-beam
# efficiency as one; a version 1 fit plotted against it disagreed by tenths of
# a kelvin everywhere, which looks exactly like a calibration error. Version 3
# adds the diffuse continuum map, which shifts the intercept.
REDUCTION_VERSION = 5

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


# Width of the running median that narrow interference is judged against, and
# the cut applied to it. Both are set by the two scales involved: the filter has
# to be much *narrower* than an H I line so it follows the line rather than
# treating its peak as an outlier, and much *wider* than interference so the
# interference stands clear of it. At 0.49 kHz channels a line spans some 250
# channels and the interference two or three, so 81 channels sits comfortably
# between them - and this is done before binning for exactly that reason, since
# on the model's 6.1 kHz grid no such gap exists.
RFI_MEDIAN_CHANNELS = 21
RFI_SIGMA = 8.0

# And a hard ceiling on how wide a feature may be and still be called
# interference. This is the criterion that actually keeps the line safe, because
# a filter width alone cannot: interference is *unresolved*, one to three
# channels, while the narrowest hydrogen there is runs a kilometre a second -
# thermal broadening of even 20 K gas - which is nine channels at 0.49 kHz.
# Anything broader than this is the sky, however sharp it looks.
#
# Measured on 2026-08-24: at a filter width of 81 channels the line's own tip
# was flagged at 9 sigma and would have been deleted; at 21 it is followed
# exactly and only the interference at 1420.2790 MHz stands out, at 31 sigma.
RFI_MAX_CHANNELS = 4

# How far the excursion must fall in the channel either side of the group. This
# is what finally separates interference from a bright line, because neither the
# filter width nor the channel count can: with a high enough signal to noise the
# curvature at a line's own peak exceeds any threshold measured against the
# noise, and a 40-channel line duly had its tip flagged at 9 sigma in testing.
#
# The physical difference is that interference is discontinuous at its edges -
# the channel beside it is at the baseline - while a spectral line is smooth,
# and the channel beside its peak is within a percent of the peak. Requiring the
# excursion to collapse by this factor immediately outside the group asks for
# exactly that, and asks it in a way that does not care how bright the line is.
RFI_EDGE_DROP = 0.3


def flag_narrow_rfi(freq_hz, values, width=RFI_MEDIAN_CHANNELS, sigma=RFI_SIGMA,
                    max_channels=RFI_MAX_CHANNELS, edge_drop=RFI_EDGE_DROP):
    """(mask, found) - channels that stand out as narrow interference.

    Only positive excursions: interference adds power. A narrow *deficit* is
    the LO artefact or a dead channel, which are handled by name elsewhere, and
    treating them here would let this quietly delete real absorption.

    Known example, and the one this was written for: 1420.2790 MHz, 126.8 kHz
    below the line at +26.8 km/s, seen at 36 and 20 sigma in two of six runs on
    2026-08-24 and absent from the rest - fixed in sky frequency and
    intermittent, which is interference and not the sky.
    """
    from scipy.ndimage import median_filter

    values = np.asarray(values, float)
    finite = np.isfinite(values)
    if finite.sum() < width * 2:
        return np.zeros(values.shape, bool), []

    filled = np.where(finite, values, np.nanmedian(values[finite]))
    base = median_filter(filled, size=width, mode="nearest")
    resid = filled - base
    scatter = float(np.median(np.abs(resid - np.median(resid))) * 1.4826)
    if scatter <= 0:
        return np.zeros(values.shape, bool), []

    above = finite & (resid > sigma * scatter)
    mask = np.zeros(values.shape, bool)
    found = []
    idx = np.flatnonzero(above)
    if idx.size:
        for group in np.split(idx, np.flatnonzero(np.diff(idx) > 3) + 1):
            if not group.size or group.size > max_channels:
                continue          # resolved, so it is the sky
            peak = group[int(np.argmax(resid[group]))]
            # Must fall away sharply either side, or it is the top of something
            # real. Guard the array edges, where there is no outside to check.
            lo, hi = group[0] - 1, group[-1] + 1
            if lo < 0 or hi >= resid.size:
                continue
            shoulder = max(resid[lo], resid[hi])
            if shoulder > edge_drop * resid[peak]:
                continue
            mask[group] = True
            found.append({"freq_hz": float(freq_hz[peak]),
                          "sigma": float(resid[peak] / scatter),
                          "channels": int(group.size)})
    return mask, found


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


def _implied_loss_db(t_sys_k):
    """Loss ahead of the LNA that would account for this system temperature.

    None when the fit is quieter than the receiver alone, which needs no loss to
    explain and usually means the fit has gone wrong in the other direction.
    """
    if not np.isfinite(t_sys_k) or t_sys_k <= RECEIVER_T_RX_K:
        return None
    loss = (t_sys_k + AMBIENT_T_K) / (RECEIVER_T_RX_K + AMBIENT_T_K)
    return float(10.0 * np.log10(loss))


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
        # "high" / "very high" / None. Kept as a level rather than a boolean so
        # the callers phrase it the same way and there is one place to change
        # where the bands sit.
        "t_sys_level": t_sys_level(t_sys),
        "high_above_k": float(HIGH_T_SYS_K),
        "very_high_above_k": float(VERY_HIGH_T_SYS_K),
        "min_t_sys_k": float(min_t_sys_k),
        "assumed_main_beam_efficiency": float(MAIN_BEAM_EFFICIENCY),
        "implied_loss_db": _implied_loss_db(t_sys),
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

    freq_hz, spectra, stamps, taus, header = read_observation(path)
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
    # Narrow interference, removed on the fine grid before binning - a single
    # 36-sigma channel is a lever on a least squares out of all proportion to
    # the one bin it occupies.
    rfi_mask, rfi_found = flag_narrow_rfi(freq_hz, mean_counts)
    if rfi_mask.any():
        mean_counts = np.where(rfi_mask, np.nan, mean_counts)

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
        "rfi_channels": int(rfi_mask.sum()), "rfi_found": rfi_found,
        "bandwidth_hz": bandwidth_hz,
        # Total integration, for the radiometer equation. The sum of the
        # per-record times, not the record count times the nominal: a run that
        # was stopped early or whose records ran long must not be credited with
        # noise it never averaged down.
        "tau_total_s": (float(np.nansum(taus)) if taus is not None and taus.size
                        else float(spectra.shape[0])
                        * float(header.get("nominal_integration_time", 0.0))),
    }


# How far to search for a frequency-scale error, as a velocity. Wide enough to
# find a badly-out crystal, narrow enough that it cannot slide onto a different
# velocity component of the line and call that a fit.
MAX_SHIFT_KM_S = 12.0


def fit_gain_with_shift(freq_hz, counts, model_freq_hz, model_k,
                        min_t_sys_k=MIN_T_SYS_K, max_shift_km_s=MAX_SHIFT_KM_S):
    """Fit gain, system temperature and a frequency-scale error together.

    The B210 runs from its own TCXO, and an error in it scales the whole
    frequency axis. Across the 2 MHz that matters here that scaling is a pure
    shift - the differential from one end of the band to the other is 13 Hz -
    so it appears as a constant velocity offset, which is exactly what was
    measured: +1.8 to +2.2 km/s against HI4PI, consistent across three fields at
    three times. That is 5.9 to 7.2 ppm, outside the +-2 ppm the part is
    specified at but ordinary for one untrimmed or warm.

    Fitted per observation rather than assumed - but the reason recorded here
    until 2026-08-25 was wrong, and the correction matters because it inverts
    the conclusion.

    The claim was that three fields between 15:21 and 16:31 on 2026-08-24
    agreeing at -5.92, -6.55 and -6.88 ppm, against -2.67 at 17:04, showed the
    clock moving 3.7 ppm in an hour and a half. Re-fitting all eight archived
    calibrations against the settled bandpass template shows what those numbers
    actually track, and it is not time:

        model peak 93-106 K (the plane)  r = 0.999   ppm -2.63, -2.09
        model peak 21-46 K  (b ~ 40)     r = 0.51-0.93   ppm -6.6 to -0.8
        model peak 1.3 K    (Lockman)    r = 0.41    ppm +18.4

    The scatter is entirely explained by line strength. Those three "agreeing"
    fields were all weak high-latitude pointings with 20 K peaks and
    correlations of 0.51, 0.78 and 0.93; they agree because they share a bias,
    not because they measured a common oscillator. With no line to hold it, the
    shift slides - +18.4 ppm came from a 1.3 K peak, where there is nothing to
    fit at all.

    Judged on the fits that are actually constrained, the clock is stable:
    -2.36 +- 0.27 ppm across 18 hours, which is +-0.08 km/s, under one channel,
    and well inside the +-2 ppm the part is specified at. It is an ordinary
    TCXO behaving like one.

    So the shift *is* stable enough to carry from one observation to another,
    and observation_plot applies the stored value to the velocity axis - but
    only from a calibration whose correlation says the fit was constrained. A
    shift fitted against a weak line is worse than no shift, because it is
    confidently wrong by several km/s.

    The wider lesson is the one that produced the error: a quantity fitted
    alongside others takes up their slack. These numbers were read as a
    property of the clock when they were a property of the fit.

    The search is bounded: given room, a shift will happily slide onto a
    neighbouring velocity component and report a superb fit to the wrong line.
    """
    freq_hz = np.asarray(freq_hz, float)
    counts = np.asarray(counts, float)
    model_freq_hz = np.asarray(model_freq_hz, float)
    model_k = np.asarray(model_k, float)

    # np.interp needs an increasing x and gives no warning when it does not get
    # one - it simply returns nonsense, which here looked like a fit that always
    # ran to its search limit. Sort once rather than trusting the caller.
    order = np.argsort(model_freq_hz)
    model_freq_hz, model_k = model_freq_hz[order], model_k[order]

    def at(shift_km_s):
        # Radio convention: a positive velocity shift moves the model down in
        # frequency. Interpolated onto the data's own grid, never the reverse.
        scaled = model_freq_hz * (1.0 - shift_km_s / (C_M_S / 1e3))
        return np.interp(freq_hz, scaled, model_k)

    def score(shift):
        """Residual in counts, which is the only frame that can be compared.

        Not the kelvin residual. That is (counts - predicted)/slope, so a shift
        that flattens the model inflates the slope and shrinks the kelvin
        residual while making the fit worse - and the search duly ran to its
        limit on data with no shift in it at all. Counts are what was measured
        and do not move when the fitted parameters do.
        """
        model = at(shift)
        try:
            out = fit_gain(counts, model, min_t_sys_k)
        except ValueError:
            return None, np.inf
        predicted = out["gain_counts_per_k"] * (out["t_sys_k"] + model)
        ok = np.isfinite(predicted) & np.isfinite(counts)
        if ok.sum() < 8:
            return None, np.inf
        return out, float(np.std(counts[ok] - predicted[ok]))

    best, best_cost = None, np.inf
    coarse = np.linspace(-max_shift_km_s, max_shift_km_s, 97)
    for shift in coarse:
        out, cost = score(shift)
        if out is not None and cost < best_cost:
            best, best_cost = (shift, out), cost
    if best is None:
        raise ValueError("no usable fit at any frequency shift")

    # Refine around the coarse minimum; the grid step is 0.25 km/s and a line
    # centroid is determined far better than that.
    step = coarse[1] - coarse[0]
    lo = max(best[0] - step, -max_shift_km_s)
    hi = min(best[0] + step, max_shift_km_s)
    for shift in np.linspace(lo, hi, 41):
        out, cost = score(shift)
        if out is not None and cost < best_cost:
            best, best_cost = (shift, out), cost

    shift, out = best
    out["velocity_shift_km_s"] = float(shift)
    out["implied_ppm"] = float(shift / (C_M_S / 1e3) * 1e6)
    out["shift_search_limit_km_s"] = float(max_shift_km_s)
    out["shift_at_search_limit"] = bool(abs(shift) > 0.95 * max_shift_km_s)
    return out, at(shift)


def calibrate_observation(path, glon, glat, sim=None, min_t_sys_k=MIN_T_SYS_K,
                          bandwidth_hz=None):
    """Fit gain and T_sys from a recorded observation of the plane."""
    red = reduce_for_fit(path, glon, glat, sim, bandwidth_hz)
    usable = red["usable"]
    obstime, header, note = red["obstime"], red["header"], red["bandpass_note"]
    result, _model_used = fit_gain_with_shift(
        red["sim_freq_hz"][usable], red["binned_counts"][usable],
        red["sim_freq_hz"], red["sim_ta_k"], min_t_sys_k)
    result.update({
        "rfi_channels_flagged": red.get("rfi_channels", 0),
        "rfi_found": red.get("rfi_found", []),
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


def t_sys_level(t_sys):
    """"very high", "high", or None. One place decides where the bands sit."""
    try:
        value = float(t_sys)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(value):
        return None
    if value > VERY_HIGH_T_SYS_K:
        return "very high"
    if value > HIGH_T_SYS_K:
        return "high"
    return None


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
    # Derived on read for calibrations saved before the field existed - and
    # recomputed for those that have it, so moving a band takes effect on
    # everything already on disk rather than only on the next calibration.
    cal["t_sys_level"] = t_sys_level(cal.get("t_sys_k"))
    cal.pop("t_sys_implausible", None)
    cal.pop("implausible_above_k", None)
    return cal


# How well the model has to match before the fitted velocity shift is worth
# carrying to another observation. Re-fitting the eight archived calibrations
# on 2026-08-25 split cleanly: correlations of 0.999 on the plane gave
# -2.63 and -2.09 ppm, while everything from 0.41 to 0.93 gave -6.6 to +18.4.
# The shift needs a strong line to hold it; without one it slides onto
# whatever is nearby and reports a confident number.
MIN_SHIFT_CORRELATION = 0.99


def trustworthy_velocity_shift(cal):
    """The fitted velocity shift in km/s, or None if the fit could not hold it.

    Returned separately from the gain because the two do not fail together: a
    fit against a weak line can give a defensible slope while its shift is
    several km/s out, having slid to wherever the residual happened to fall.
    """
    if not cal:
        return None
    shift = cal.get("velocity_shift_km_s")
    corr = cal.get("correlation")
    if shift is None or corr is None:
        return None
    if not np.isfinite(shift) or not np.isfinite(corr):
        return None
    if corr < MIN_SHIFT_CORRELATION:
        return None
    if cal.get("shift_at_search_limit"):
        return None          # ran to the bound, so it is a limit not a measurement
    return float(shift)


def embedded_calibration(header):
    """The gain calibration carried in an observation's own header, or None.

    Counterpart to bandpass.embedded_template. Recordings written from
    2026-08-25 carry the gain and system temperature they were taken under, so
    the file is reducible on its own rather than only on the machine that
    happens to hold the current gain_calibration.json.
    """
    raw = (header or {}).get("gain_calibration")
    if not raw:
        return None
    try:
        cal = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
    except (ValueError, AttributeError):
        return None
    if not isinstance(cal, dict):
        return None
    cal["t_sys_level"] = t_sys_level(cal.get("t_sys_k"))
    return cal


def calibration_for(header, path=CALIBRATION_FILE):
    """(calibration, why, source) - the best gain available for this observation.

    Prefers the stored one while it applies, because re-reducing an old
    recording against a better calibration is the reason the recording is kept
    raw. Falls back to the one the file carries, so an observation from a
    tuning nobody uses any more is still reducible rather than stranded in
    counts.
    """
    cal = load_calibration(path)
    ok, why = calibration_applies_to(cal, header)
    if ok:
        return cal, "", "current"
    embedded = embedded_calibration(header)
    if embedded is not None:
        ok_e, why_e = calibration_applies_to(embedded, header)
        if ok_e:
            return embedded, "", "recorded with the observation"
        # Prefer the embedded reason: "no gain calibration has been made" is
        # true of this machine but misleading about this file, which carries
        # one - it simply does not fit the tuning, and that is what the reader
        # needs to be told.
        return None, why_e, ""
    return None, why, ""


def calibration_applies_to(cal, header, tolerance_hz=1e3):
    """(ok, why) - whether a stored gain applies to this observation.

    Same reasoning as the bandpass template: the counts-per-kelvin scale belongs
    to a tuning and a receiver gain setting, and using it on another is worse
    than leaving the spectrum in counts, because counts are honestly unlabelled
    while wrong kelvin are not.
    """
    if not cal or not cal.get("gain_counts_per_k"):
        return False, "no gain calibration has been made"
    cfg = cal.get("config") or {}
    lo, rate = header.get("center_freq_hz"), header.get("sample_rate_hz")
    if lo is None or rate is None:
        return False, "observation does not record its tuning"
    if abs(float(cfg.get("lo_hz", 0)) - float(lo)) > tolerance_hz:
        return False, ("calibration is for LO %.6f MHz, this is %.6f MHz"
                       % (float(cfg.get("lo_hz", 0)) / 1e6, float(lo) / 1e6))
    if abs(float(cfg.get("sample_rate_hz", 0)) - float(rate)) > 1.0:
        return False, ("calibration is for %.3f Msps, this is %.3f Msps"
                       % (float(cfg.get("sample_rate_hz", 0)) / 1e6,
                          float(rate) / 1e6))
    want, have = cfg.get("gain_db"), header.get("gain_db")
    if want is not None and have is not None and abs(float(want) - float(have)) > 0.01:
        return False, ("calibration is for %.1f dB of receiver gain, this is %.1f"
                       % (float(want), float(have)))
    return True, ""


def counts_to_kelvin(counts, cal):
    """Antenna temperature from corrected counts, minus the system term."""
    if not cal or not cal.get("gain_counts_per_k"):
        return None
    return np.asarray(counts, float) / cal["gain_counts_per_k"] - cal["t_sys_k"]
