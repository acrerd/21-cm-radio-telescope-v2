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

HERE = os.path.dirname(os.path.abspath(__file__))
CALIBRATION_FILE = os.path.join(HERE, "gain_calibration.json")
SIMULATOR_DIR = os.path.join(os.path.dirname(HERE), "astro_simulator")

H1_REST_FREQ_HZ = 1420.405752e6
C_M_S = 299792458.0

# The SAWbird+ H1's own noise temperature is 51/59/67 K (min/typ/max), so
# nothing below this is physical once spillover, sky and ground are added.
MIN_T_SYS_K = 50.0

# Don't calibrate against a patch of plane low down: ground spillover and airmass
# both grow there, and both land in T_sys rather than in the line.
MIN_TARGET_ALT_DEG = 30.0

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


def plane_target_now(when=None, lat=55.902426, lon=-4.307865, elevation_m=50.0,
                     min_alt_deg=MIN_TARGET_ALT_DEG, obstruction_sectors=None,
                     step_deg=3.0, sim=None, glat=0.0):
    """The best bit of galactic plane available at this moment.

    Scored as the simulated line peak weighted by sin(altitude). The brightness
    sets the lever arm the fit gets; the altitude term stands in for everything
    that gets worse towards the horizon - airmass, ground in the sidelobes, and
    the spillover that a small dish has plenty of. It is a heuristic and is meant
    to be: the point is to avoid calibrating on a bright patch lying in the
    trees, not to optimise anything to the last percent.

    Returns None if the plane is entirely unavailable, which happens.
    """
    when = when or datetime.now(timezone.utc)
    sim = sim or load_simulator()
    lons = np.arange(0.0, 360.0, step_deg)
    alt, az = _sky_position(lons, np.full_like(lons, glat), when,
                            lat, lon, elevation_m)

    best = None
    for l, a, z in zip(lons, alt, az):
        if a < min_alt_deg:
            continue
        if _in_obstructed_sector(z, a, obstruction_sectors):
            continue
        peak = float(np.nanmax(sim.spectrum(float(l), glat)[1]))
        score = peak * math.sin(math.radians(a))
        if best is None or score > best["score"]:
            best = {"glon": float(l), "glat": float(glat),
                    "alt_deg": float(a), "az_deg": float(z),
                    "expected_peak_k": peak, "score": float(score)}
    return best


# --------------------------------------------------------------------------
# what the sky should look like


def load_simulator(bandwidth_hz=2.0e6, dish_m=3.0, nchan=None, compact=None):
    """A DishSimulator on the shipped compact HI4PI cube."""
    import sys
    if SIMULATOR_DIR not in sys.path:
        sys.path.insert(0, SIMULATOR_DIR)
    import astro_simulator as A

    return A.DishSimulator(
        cube_path=None, bw_hz=bandwidth_hz, dish_m=dish_m, eta=1.0,
        nchan=nchan, tsys=None, tint=60.0,
        compact_path=compact or os.path.join(SIMULATOR_DIR,
                                             "hi4pi_compact.npz.xz"))


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
        "min_t_sys_k": float(min_t_sys_k),
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


def calibrate_observation(path, glon, glat, sim=None, min_t_sys_k=MIN_T_SYS_K,
                          bandwidth_hz=None):
    """Fit gain and T_sys from a recorded observation of the plane.

    The spectrum is bandpass corrected first and then binned onto the
    simulator's own channels. Binning down rather than interpolating up is
    deliberate: HI4PI's native resolution is 1.29 km/s against 0.10 km/s per
    recorded channel, so the model simply cannot say anything at our resolution,
    and regressing fine channels against an interpolated coarse curve would be
    fitting noise against a smooth line and calling the correlation good.
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
    # Bin edges from the simulator's own channels, so the model is never
    # asked for detail it does not have.
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
    result = fit_gain(binned[usable], sim_ta[usable], min_t_sys_k)
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
