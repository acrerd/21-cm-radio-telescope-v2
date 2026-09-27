#!/usr/bin/env python3
"""Pulsars on a 3 m dish: a folded detection from a fast total-power recording.

A pulsar's mean flux is far below what one pulse can show here - B0329+54 at
203 mJy is 0.48 mK mean against 0.4 K of noise per pulse - so it is only ever
a **folded** detection: the band power sampled every millisecond for an hour,
added up at the pulsar's period, so the pulse stands at its peak of ~50 mK
while the noise averages down as the square root of the number of periods.
At T_sys 190 K that is 5 sigma in ~75 minutes; at 100 K, ~20.

The recording (`b210_h1_receiver.py --headless` with H1_MODE=pulsar) is a
small filterbank rather than a single number: `NCHAN` channels across the
8 MHz at `DT_S`, ~230 MB an hour. The channels are for **interference**: a
channel hit by a carrier or a burst can be dropped, or masked in time and
frequency by PRESTO's rfifind, where a single band sum could not be cleaned.
They also let each channel's gain drift and the SAW tilt divide out on their
own before the sum, though a detrended band sum would do nearly as well.
They are **not** for dispersion: the sweep across our band is 0.6 ms at DM
26.76, under one 1 ms sample, so dedispersing gains 0.1% of S/N and the
sweep cannot be seen at this S/N (the per-channel panel's DM line is
effectively vertical; it is an interference view, not a proof of the pulsar).

The period used is the catalogue's barycentric period brought to the
telescope: the observer's velocity toward the pulsar - Earth's rotation and
orbit, astropy's barycentric correction - compresses or stretches the pulse
train by up to a part in 10^4, which over an hour of 5000 periods is most of
a period. Integrated across the run rather than held fixed, since the
rotation term changes through the hour. The fold also searches a small
window of fractional period around the prediction and reports where the
peak signal-to-noise fell, in parts per million: near zero says the
conversion is right, and a sign error would show as twice the velocity term.

Absolute time is not needed for any of this. The fold wants the sample
cadence stable (the B210's clock, on the external reference) and the start
time to a millisecond (the host clock); pulsar *timing* would want a PPS, and
this is not that.

Built for B0329+54 alone: at 203 mJy and a 1% duty cycle it needs an hour
here, and the next brightest (B0950+08) twenty times as long. The par block
is hard-wired; there is no catalogue.
"""
import json
import math
import os

import numpy as np

C_M_S = 299792458.0

NCHAN = 1               # the whole band summed (2026-09-26; 16 bought nothing on the sky, at 16x the storage)
DT_S = 1.0e-3           # one row per millisecond
NBINS = 64              # phase bins in the folded profile
SEARCH_PPM = 150.0      # +-fractional period searched around the prediction, ppm
SUBINT_S = 600.0        # sub-integration length for the time-phase panel (10 min, 2026-09-27)
DETREND_S = 10.0        # running-median window for each channel's gain drift

# The one pulsar this dish can fold in an evening, as a par block. Everything
# else is a factor of 20 or more in time at any T_sys we will reach (B0950+08
# at 8 h is the nearest), so the mode is built for B0329+54 and nothing
# else: no catalogue, no picker, no lookup. Values as published in the ATNF
# catalogue (PSR J0332+5434) as remembered here; the fold's period search
# absorbs a part in 10^4, which covers their precision, and the number that
# matters most - the barycentric period - is good to nine digits.
#
#   PSRJ   J0332+5434      PSRB  B0329+54
#   RAJ    03:32:59.4096   DECJ  +54:34:43.329      (J2000)
#   F0     1.399538 Hz     P0    0.714519699726 s
#   P1     2.0496e-15      PEPOCH 46473 (MJD)
#   DM     26.7641         S1400 203 mJy   W50 ~6.6 ms
PULSAR_NAME = "B0329+54"
B0329 = {
    "name": PULSAR_NAME, "psrj": "J0332+5434",
    "ra_deg": 15.0 * (3 + 32 / 60.0 + 59.4096 / 3600.0),
    "dec_deg": 54 + 34 / 60.0 + 43.329 / 3600.0,
    "period_s": 0.714519699726, "pdot": 2.04826e-15, "pepoch_mjd": 46473.0,   # ATNF F0/F1 (hlk+04), checked 2026-09-26
    "dm": 26.7641, "s1400_mjy": 203.0, "w50_ms": 6.6,
    # The spin ephemeris as ATNF gives it (PEPOCH in TDB), for absolute phase,
    # and the position's epoch and proper motion (0.3 arcsec since POSEPOCH is
    # 0.7 ms of annual Roemer error - small, but free to remove).
    "f0": 1.399541538720, "f1": -4.011970e-15, "f2": 5.3e-28,
    "posepoch_mjd": 56000.0, "pmra_mas_yr": 16.97, "pmdec_mas_yr": -10.37,
    # Phase zero, chosen once (2026-09-27) so the pulse of the first
    # detection, 20260926_203116_pulsar.h5, folds to 0.5: with no offset its
    # matched peak fell at 0.1895. Every other run lands at 0.5 by
    # prediction, so where one actually lands is a check on the ephemeris,
    # the barycentring and the clock together. Change it only with a reason
    # that applies to every run, never to centre one night.
    "phase_offset": 0.3105,
    "note": "circumpolar from Glasgow; scintillates by factors of two or three over an hour",
}


def lookup(name=None):
    """B0329+54 under any of its names (or with no name at all), else None."""
    key = str(name or PULSAR_NAME).strip().upper().replace(" ", "")
    if key in ("B0329+54", "0329+54", "J0332+5434", "0332+5434", "PSRB0329+54", "PSRJ0332+5434"):
        return dict(B0329)
    return None


def period_at(unix_time):
    """The barycentric period at a date, P0 + P1 x (t - PEPOCH): 2.6 us
    beyond the epoch by 2026, four parts in 10^6, a fifth of a period across
    an hour of 5000 - small, but free to include."""
    mjd = float(unix_time) / 86400.0 + 40587.0
    return B0329["period_s"] + B0329["pdot"] * (mjd - B0329["pepoch_mjd"]) * 86400.0


# ---------------------------------------------------------------------------
# the period at the telescope


def _site_location():
    import observatory
    from astropy import units as u
    from astropy.coordinates import EarthLocation
    return EarthLocation(lat=observatory.SITE_LAT_DEG * u.deg, lon=observatory.SITE_LON_DEG * u.deg,
                         height=observatory.SITE_HEIGHT_M * u.m)


def observer_velocity_toward(ra_deg, dec_deg, t_unix):
    """The observer's velocity toward the pulsar, m/s, at each `t_unix`
    (scalar or array): astropy's barycentric radial-velocity correction,
    which is the number to add to an observed radial velocity to make it
    barycentric - positive when the site is moving toward the source."""
    from astropy import units as u
    from astropy.coordinates import SkyCoord
    from astropy.time import Time
    tgt = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
    times = Time(np.atleast_1d(np.asarray(t_unix, float)), format="unix")
    rv = tgt.radial_velocity_correction("barycentric", obstime=times, location=_site_location())
    return np.asarray(rv.to_value(u.m / u.s), float)


def topocentric_period(period_bary_s, ra_deg, dec_deg, t_unix):
    """The apparent period at the site: an observer moving toward the pulsar
    at v sees the pulses arrive faster, P_topo = P_bary (1 - v/c)."""
    v = observer_velocity_toward(ra_deg, dec_deg, t_unix)
    return period_bary_s * (1.0 - v / C_M_S)


K_DM_S = 4.148808e3      # dispersion constant, s MHz^2 pc^-1 cm^3


def absolute_phase(t_unix, pulsar=None, freq_hz=None, knot_s=60.0):
    """Pulse phase from the pulsar's own ephemeris, the same zero every night.

    Each time goes to the solar-system barycentre - TDB, plus astropy's
    light-travel time from the site (the Roemer delay, up to 500 s over the
    year) toward the pulsar's position at that date (proper motion applied)
    - less the dispersion delay at `freq_hz`, and the phase is the ATNF spin
    ephemeris there: F0 dt + F1 dt^2/2 + F2 dt^3/6 from PEPOCH, plus the
    fixed `phase_offset` that puts the pulse at 0.5. `phase_track` counted
    from each file's first sample, so each run had its own zero; this is
    TEMPO's TZR idea. Evaluated on `knot_s` knots and interpolated (the
    curvature between knots is 1e-7 of a period). Returns (phase, mean
    topocentric period) like `phase_track`. Precision: dt is 1.3e9 s, so
    float64 phase is good to ~3e-7 of a period.
    """
    from astropy import units as u
    from astropy.coordinates import SkyCoord
    from astropy.time import Time
    p = pulsar or B0329
    t = np.asarray(t_unix, float)
    lo, hi = float(np.min(t)), float(np.max(t))
    knots = np.arange(lo, hi + knot_s, knot_s) if hi > lo else np.array([lo, lo + 1.0])
    tk = Time(knots, format="unix", scale="utc", location=_site_location())
    yrs = (tk.mjd - p.get("posepoch_mjd", tk.mjd[0])) / 365.25
    dec = p["dec_deg"] + p.get("pmdec_mas_yr", 0.0) * yrs / 3.6e6
    ra = p["ra_deg"] + p.get("pmra_mas_yr", 0.0) * yrs / 3.6e6 / np.cos(np.radians(p["dec_deg"]))
    coord = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs")
    tb = tk.tdb + tk.light_travel_time(coord, "barycentric")
    dt = (tb - Time(p["pepoch_mjd"], format="mjd", scale="tdb")).to_value(u.s)
    if freq_hz:
        dt = dt - K_DM_S * p.get("dm", 0.0) / (float(freq_hz) / 1e6) ** 2
    f0, f1, f2 = p["f0"], p.get("f1", 0.0), p.get("f2", 0.0)
    whole = np.floor(f0 * dt)                     # keep the numbers small before adding the offset
    phk = (f0 * dt - whole) + 0.5 * f1 * dt ** 2 + f2 * dt ** 3 / 6.0 + p.get("phase_offset", 0.0)
    phk = phk + (whole - whole[0])                # continuous across the run
    # Whole periods only, so the fraction - the phase that matters - is kept:
    # counted from the run's first period, as the period search's phase/(1+d)
    # scaling and n_periods assume (the F1 term alone is -3300 periods by now).
    phk = phk - np.floor(phk[0])
    freq_topo = np.gradient(phk, knots) if len(knots) > 2 else np.array([f0, f0])
    return np.interp(t, knots, phk), float(1.0 / np.mean(freq_topo))


def phase_track(t_unix, period_bary_s, ra_deg, dec_deg, knot_s=60.0):
    """Pulse phase (in periods, from the first sample) at each time, with
    the topocentric period integrated across the run rather than held at
    its value at the start: the rotation term alone changes the period by
    a few parts in 10^7 over an hour, a fifth of a period by the end."""
    t = np.asarray(t_unix, float)
    knots = np.arange(t[0], t[-1] + knot_s, knot_s)
    if len(knots) < 2:
        knots = np.array([t[0], t[-1] + 1.0])
    p_knots = topocentric_period(period_bary_s, ra_deg, dec_deg, knots)
    f_knots = 1.0 / p_knots
    # cumulative phase at the knots, then interpolated (f is smooth)
    dphi = np.diff(knots) * 0.5 * (f_knots[:-1] + f_knots[1:])
    phi_knots = np.concatenate([[0.0], np.cumsum(dphi)])
    return np.interp(t, knots, phi_knots), float(np.mean(p_knots))


# ---------------------------------------------------------------------------
# the recording


ROW_CENTRE = 0.5        # a row's time is its middle: mark + (i + 0.5) dt (2026-09-27, #48)


def row_times(n_rows, dt_s, t0_unix, time_marks=None):
    """The time of each row's MIDDLE. With the radio's `time_marks` - (row,
    device time of that row's first sample) at the start and after every
    overflow - each stretch between marks is timed from its own mark and the
    row count, so a dropped block moves the rows after it by exactly what was
    dropped rather than not at all. Without marks (a demo recording, an old
    file) the rows start at t0 + i dt. Mid-row, not start: a row is the sum
    of dt of samples, so its centroid is half a row after its first sample,
    and a TOA stamped at the start would be 0.5 ms early (constant, but
    wrong)."""
    i = np.arange(n_rows, dtype=float) + ROW_CENTRE
    marks = np.asarray(time_marks, float).reshape(-1, 2) if time_marks is not None else np.empty((0, 2))
    marks = marks[np.argsort(marks[:, 0])] if len(marks) else marks
    if not len(marks):
        return t0_unix + dt_s * i
    t = np.empty(n_rows)
    # A mark's row may be fractional (the exact sample of an overflow's first
    # sample over the samples per row, _TagTap): the arithmetic keeps the
    # fraction, and a stretch starts at the first row wholly after its mark -
    # the row the gap falls in began before it, on the previous mark.
    r = marks[:, 0]
    starts = np.ceil(r - 1e-9).astype(int)
    for k, (rk, tm) in enumerate(zip(r, marks[:, 1])):
        lo = 0 if k == 0 else min(max(starts[k], 0), n_rows)
        hi = min(starts[k + 1], n_rows) if k + 1 < len(r) else n_rows
        t[lo:hi] = tm + dt_s * (i[lo:hi] - rk)
    return t


def read_recording(path):
    """(t_unix, freq_hz, power[N, nchan], attrs) from a pulsar-mode file."""
    import observation_plot
    with observation_plot.open_readonly(path) as hf:
        attrs = {k: (v.item() if hasattr(v, "item") else v) for k, v in hf.attrs.items()}
        power = np.asarray(hf["power"][:], dtype=np.float32)
        freq = np.asarray(hf["frequency_hz"][:], float)
        marks = np.asarray(hf["time_marks"][:], float) if "time_marks" in hf else None
        if "overflow_marks" in hf and hf["overflow_marks"].shape[0]:
            attrs["overflows_total"] = int(np.asarray(hf["overflow_marks"][:])[:, 1].sum())
    t0 = float(attrs.get("t0_unix", 0.0))
    dt = float(attrs.get("dt_s", DT_S))
    attrs["n_time_marks"] = int(len(marks)) if marks is not None else 0
    t = row_times(power.shape[0], dt, t0, marks if marks is not None and len(marks) else None)
    return t, freq, power, attrs


def detrend_channels(power, dt_s, window_s=DETREND_S):
    """Each channel divided by its own running median, minus one: the SAW
    tilt, the gain drift and the channel's bandpass level all go, and what
    is left is a fractional excess that can be summed across channels."""
    from scipy.ndimage import median_filter
    n = max(3, int(round(window_s / dt_s)) | 1)
    out = np.empty_like(power, dtype=float)
    for c in range(power.shape[1]):
        base = median_filter(power[:, c].astype(float), size=n, mode="nearest")
        with np.errstate(divide="ignore", invalid="ignore"):
            out[:, c] = power[:, c] / base - 1.0
    out[~np.isfinite(out)] = 0.0
    return out


def clip_rfi(series, sigma=6.0):
    """Zero samples beyond `sigma` robust deviations - interference or a
    dropped block - so they do not land in one phase bin."""
    x = np.asarray(series, float)
    mad = np.median(np.abs(x - np.median(x))) * 1.4826
    if mad <= 0:
        return x, 0
    bad = np.abs(x - np.median(x)) > sigma * mad
    y = x.copy()
    y[bad] = 0.0
    return y, int(bad.sum())


# ---------------------------------------------------------------------------
# folding


def fold(series, phase, nbins=NBINS):
    """Mean of `series` in `nbins` bins of pulse phase."""
    b = np.floor((np.asarray(phase) % 1.0) * nbins).astype(int)
    b = np.clip(b, 0, nbins - 1)
    sums = np.bincount(b, weights=series, minlength=nbins)
    counts = np.bincount(b, minlength=nbins)
    with np.errstate(invalid="ignore", divide="ignore"):
        prof = sums / counts
    prof[counts == 0] = 0.0
    return prof, counts


def profile_snr(profile):
    """(snr, peak_bin, baseline, sigma): the peak over the baseline in units
    of the off-pulse scatter. Baseline and scatter are the median and the
    median absolute deviation (x1.4826) of the whole profile: a pulse a few
    percent wide moves neither, and unlike the standard deviation of the
    lowest bins - which is a truncated distribution's, 0.6 sigma - the MAD
    does not flatter pure noise (that estimator called noise 4.7 sigma)."""
    p = np.asarray(profile, float)
    base = float(np.median(p))
    sig = float(1.4826 * np.median(np.abs(p - base)))
    if sig <= 0:
        return 0.0, int(np.argmax(p)), base, sig
    return float((p.max() - base) / sig), int(np.argmax(p)), base, sig


FINE_BINS = 256         # the fold the matched filter runs on


def matched_snr(series, phase, width_s, period_s, nbins=FINE_BINS):
    """Signal to noise with a boxcar of the pulse's width run over a fine fold:
    the pulse's whole energy against the noise in one pulse width, which is
    what the pulsar radiometer equation counts. The coarse profile's peak bin
    loses a third of it - a 6.6 ms pulse sits inside an 11 ms bin and is split
    across two on average. Returns (snr, peak_phase, profile_fine)."""
    prof, counts = fold(series, phase, nbins)
    w = max(1, int(round(width_s / period_s * nbins)))
    kernel = np.ones(w) / w
    # circular boxcar
    ext = np.concatenate([prof[-w:], prof, prof[:w]])
    sm = np.convolve(ext, kernel, mode="same")[w:-w]
    base = float(np.median(sm))
    sig = float(1.4826 * np.median(np.abs(sm - base)))
    if sig <= 0:
        return 0.0, 0.0, prof
    k = int(np.argmax(sm))
    return float((sm[k] - base) / sig), (k + 0.5) / nbins, prof


def search_period(series, phase0, ppm=SEARCH_PPM, nbins=NBINS, steps=None):
    """Fold at fractional period offsets within +-ppm of the prediction and
    return (best_ppm, best_snr, trial_ppm, trial_snr). `phase0` is the
    predicted phase; a period longer by a fraction d has phase phase0/(1+d).
    The step is set by the phase resolution across the run: one bin at the
    last sample."""
    n_periods = float(phase0[-1] - phase0[0]) if len(phase0) > 1 else 1.0
    step_ppm = max(0.25, 1e6 / (nbins * max(n_periods, 1.0)))
    trials = np.arange(-ppm, ppm + step_ppm, step_ppm) if steps is None else np.linspace(-ppm, ppm, steps)
    snrs = np.empty(len(trials))
    for i, d in enumerate(trials):
        prof, _ = fold(series, phase0 / (1.0 + d * 1e-6), nbins)
        snrs[i] = profile_snr(prof)[0]
    k = int(np.argmax(snrs))
    return float(trials[k]), float(snrs[k]), trials, snrs


def analyse(t, freq_hz, power, pulsar, nbins=NBINS, search_ppm=SEARCH_PPM):
    """The whole reduction for one recording against one catalogue pulsar.

    Returns a dict with the summed profile at the predicted period and at the
    best searched period, the per-channel profiles, the time-phase
    sub-integrations, the DM-predicted delay per channel, and the numbers.
    """
    dt = float(np.median(np.diff(t))) if len(t) > 1 else DT_S
    det = detrend_channels(power, dt)
    per_chan = det.copy()
    total = per_chan.sum(axis=1) / per_chan.shape[1]
    total, n_clipped = clip_rfi(total)
    p_bary = period_at(t[0]) if pulsar.get("pdot") is not None else pulsar["period_s"]
    if pulsar.get("f0"):
        phase0, p_mean = absolute_phase(t, pulsar, float(np.mean(freq_hz)))
    else:
        phase0, p_mean = phase_track(t, p_bary, pulsar["ra_deg"], pulsar["dec_deg"])
    prof0, counts0 = fold(total, phase0, nbins)
    snr0 = profile_snr(prof0)
    width_s = float(pulsar.get("w50_ms", 6.6)) * 1e-3
    m_snr, m_phase, _ = matched_snr(total, phase0, width_s, p_mean)
    best_ppm, best_snr, trial_ppm, trial_snr = search_period(total, phase0, search_ppm, nbins)
    phase_best = phase0 / (1.0 + best_ppm * 1e-6)
    prof_best, _ = fold(total, phase_best, nbins)
    snr_best = profile_snr(prof_best)
    # per-channel profiles at the best period, and where their peaks fall
    chan_prof = np.array([fold(clip_rfi(per_chan[:, c])[0], phase_best, nbins)[0]
                          for c in range(per_chan.shape[1])])
    # the dispersion delay of each channel relative to the highest frequency
    f_ghz = np.asarray(freq_hz, float) / 1e9
    delay_s = 4.148808e-3 * pulsar["dm"] * (1.0 / f_ghz ** 2 - 1.0 / f_ghz.max() ** 2)
    # sub-integrations: the profile in slices of SUBINT_S, to see it build
    n_sub = max(1, int((t[-1] - t[0]) // SUBINT_S))
    edges = np.linspace(t[0], t[-1] + 1e-6, n_sub + 1)
    subints = np.array([fold(total[(t >= a) & (t < b)], phase_best[(t >= a) & (t < b)], nbins)[0]
                        for a, b in zip(edges[:-1], edges[1:])])
    v = observer_velocity_toward(pulsar["ra_deg"], pulsar["dec_deg"], np.array([t[0], t[-1]]))
    return {
        "pulsar": pulsar["name"], "period_bary_s": p_bary, "period_topo_mean_s": p_mean,
        "observer_velocity_m_s": [float(v[0]), float(v[-1])],
        "duration_s": float(t[-1] - t[0] + dt), "dt_s": dt, "n_periods": float(phase0[-1] - phase0[0]),
        "nbins": nbins, "n_clipped": n_clipped,
        "profile_predicted": prof0.tolist(), "snr_predicted": snr0[0], "peak_bin_predicted": snr0[1],
        "snr_matched": m_snr, "matched_phase": m_phase, "matched_width_s": width_s,
        "profile_best": prof_best.tolist(), "snr_best": snr_best[0], "peak_bin_best": snr_best[1],
        "best_ppm": best_ppm, "search_ppm": [float(trial_ppm.min()), float(trial_ppm.max())],
        "search_snr": trial_snr.tolist(), "search_trial_ppm": trial_ppm.tolist(),
        "channel_profiles": chan_prof.tolist(), "channel_freq_hz": np.asarray(freq_hz, float).tolist(),
        "dm_delay_s": delay_s.tolist(), "subints": subints.tolist(), "subint_s": SUBINT_S,
        "duty_expected": None,
    }


BLOCK_ROWS = 600_000     # 10 minutes of rows per read: 38 MB of float32


STREAM_DETREND_S = 1.0  # analyse_file's baseline block (the 408 MHz pipeline's 1 s median)
WEIGHT_S = 1.0          # per-second inverse-variance weighting, as the 408 MHz fold did
SUB_S = 60.0            # sub-profile length the period search shifts


def _block_times(lo, hi, dt, t0, marks):
    """row_times for rows lo..hi alone, so no whole-run array is built."""
    j = np.arange(lo, hi, dtype=float)
    i = j + ROW_CENTRE                                  # row middles, as row_times
    if not len(marks):
        return t0 + dt * i
    # which mark: by the row's first sample, as row_times' ceil rule
    k = np.clip(np.searchsorted(marks[:, 0], j + 1e-9, side="right") - 1, 0, len(marks) - 1)
    return marks[k, 1] + dt * (i - marks[k, 0])


def _matched_from_profile(prof, width_s, period_s):
    """matched_snr's boxcar statistic on an already-folded fine profile."""
    nb = len(prof)
    w = max(1, int(round(width_s / period_s * nb)))
    ext = np.concatenate([prof[-w:], prof, prof[:w]])
    sm = np.convolve(ext, np.ones(w) / w, mode="same")[w:-w]
    base = float(np.median(sm))
    sig = float(1.4826 * np.median(np.abs(sm - base)))
    if sig <= 0:
        return 0.0, 0.0
    k = int(np.argmax(sm))
    return float((sm[k] - base) / sig), (k + 0.5) / nb


def analyse_file(path, pulsar=None, nbins=NBINS, search_ppm=SEARCH_PPM, block_rows=BLOCK_ROWS, toa_bins=None):
    """`analyse` for a recording on disk, in one pass and bounded memory.

    Nothing the length of the run is kept. The first version held the row
    times, the phase and the summed series whole, which on the 16 h run of
    2026-09-27 (57.5 M rows) took the scheduler to 2.8 GB, into swap and ten
    minutes. The one before that held the float64 array and the kernel
    killed the scheduler (09-26). Now each block of rows is timed from the
    radio's marks, phased by interpolating `phase_track` evaluated on 60 s
    knots, divided by its own 1 s block medians, clipped at 6 robust sigma
    (clipped samples get no weight rather than a zero) and weighted by the
    inverse variance of its second, as the 408 MHz pipeline did. On that run
    this gives matched 9.2 / peak bin 9.4 (the first version 9.4 / 9.3), in
    24 s and 260 MB. A 1 s baseline cuts the red noise at 0.1-0.5 Hz from
    7x to 2x white, but the pulse's harmonics lie above it. Everything is
    accumulated into 60 s sub-profiles of the 256-bin fold. The 64-bin
    profile, the matched filter, the period search (sub-profiles shifted per
    trial, as prepfold does) and the time-phase panel all come from those.
    """
    import observation_plot
    with observation_plot.open_readonly(path) as hf:
        attrs = {k: (v.item() if hasattr(v, "item") else v) for k, v in hf.attrs.items()}
        ds = hf["power"]
        n, nchan = ds.shape
        freq = np.asarray(hf["frequency_hz"][:], float)
        marks = np.asarray(hf["time_marks"][:], float).reshape(-1, 2) if "time_marks" in hf else np.empty((0, 2))
        marks = marks[np.argsort(marks[:, 0])] if len(marks) else marks
        if "overflow_marks" in hf and hf["overflow_marks"].shape[0]:
            attrs["overflows_total"] = int(np.asarray(hf["overflow_marks"][:])[:, 1].sum())
        attrs["n_time_marks"] = int(len(marks))
        injected = attrs.get("inject_period_s") is not None
        if pulsar is None and injected:
            # An artificial pulsar from our own transmitter: folded at its
            # period in the receiver's own time - no Doppler, no ephemeris.
            pulsar = dict(B0329, name="artificial pulsar", period_s=float(attrs["inject_period_s"]),
                          pdot=None, dm=0.0, w50_ms=1e3 * float(attrs["inject_period_s"]) * float(attrs.get("inject_duty", 0.01)),
                          fixed_period=True)
        if pulsar is None:
            pulsar = lookup(attrs.get("pulsar_name") or attrs.get("object_name") or PULSAR_NAME)
        if pulsar is None:
            raise ValueError("this mode folds B0329+54 only; the recording names %r"
                             % (attrs.get("pulsar_name") or attrs.get("object_name")))
        if n < 1000:
            raise ValueError("too few samples to fold (%d rows)" % n)
        dt = float(attrs.get("dt_s", DT_S))
        t0_attr = float(attrs.get("t0_unix", 0.0))
        t_first = float(_block_times(0, 1, dt, t0_attr, marks)[0])
        t_last = float(_block_times(n - 1, n, dt, t0_attr, marks)[0])
        p_bary = period_at(t_first) if pulsar.get("pdot") is not None else pulsar["period_s"]
        if pulsar.get("fixed_period"):
            knots = np.array([t_first, t_last + 1.0])
            phi_k, p_mean = (knots - t_first) / p_bary, p_bary
        else:
            knots = np.arange(t_first, t_last + 120.0, 60.0)
            if pulsar.get("f0"):
                # the same phase zero every night (absolute_phase)
                phi_k, p_mean = absolute_phase(knots, pulsar, float(np.mean(freq)))
            else:
                phi_k, p_mean = phase_track(knots, p_bary, pulsar["ra_deg"], pulsar["dec_deg"])
        fb = FINE_BINS
        nsub = int((t_last - t_first) // SUB_S) + 1
        S = np.zeros((nsub, fb)); C = np.zeros((nsub, fb))
        phi_sum = np.zeros(nsub); phi_n = np.zeros(nsub)
        chan_sum = np.zeros((nchan, nbins)); chan_cnt = np.zeros((nchan, nbins))
        per = max(1, int(round(STREAM_DETREND_S / dt)))
        wper = max(1, int(round(WEIGHT_S / dt)))
        n_clipped = 0
        if toa_bins:
            # a finer fold for timing (pulsar_toa), in the same 60 s pieces
            S1 = np.zeros((nsub, int(toa_bins))); C1 = np.zeros((nsub, int(toa_bins)))
        for i in range(0, n, block_rows):
            x = np.asarray(ds[i:min(n, i + block_rows)], dtype=np.float64)
            m = len(x)
            # 1 s block medians per channel, the tail folded into the last block
            nb_ = max(1, m // per)
            k = np.minimum(np.arange(m) // per, nb_ - 1)
            base = (np.median(x[:nb_ * per].reshape(nb_, per, nchan), axis=1) if m >= per
                    else np.median(x, axis=0)[None, :])
            base[base <= 0] = np.nan
            frac = x / base[k] - 1.0
            frac[~np.isfinite(frac)] = 0.0
            t = _block_times(i, i + m, dt, t0_attr, marks)
            phase = np.interp(t, knots, phi_k)
            b64 = np.clip(np.floor((phase % 1.0) * nbins).astype(np.int64), 0, nbins - 1)
            for c in range(nchan):
                col = frac[:, c]
                mad = 1.4826 * np.median(np.abs(col - np.median(col)))
                good = np.abs(col) <= 6.0 * mad if mad > 0 else np.ones(m, bool)
                chan_sum[c] += np.bincount(b64[good], weights=col[good], minlength=nbins)
                chan_cnt[c] += np.bincount(b64[good], minlength=nbins)
            y = frac.mean(axis=1)
            mad = 1.4826 * np.median(np.abs(y - np.median(y)))
            good = np.abs(y - np.median(y)) <= 6.0 * mad if mad > 0 else np.ones(m, bool)
            n_clipped += int((~good).sum())
            # inverse variance of each second, over its unclipped samples
            sec = np.arange(m) // wper
            g = good.astype(float)
            cnt = np.bincount(sec, weights=g)
            s1 = np.bincount(sec, weights=g * y)
            s2 = np.bincount(sec, weights=g * y * y)
            with np.errstate(invalid="ignore", divide="ignore"):
                var = s2 / cnt - (s1 / cnt) ** 2
            inv = np.where((cnt > 1) & (var > 0), 1.0 / np.where(var > 0, var, 1.0), 0.0)
            w = np.where(good, inv[sec], 0.0)
            sub = np.clip(((t - t_first) // SUB_S).astype(np.int64), 0, nsub - 1)
            fbin = np.clip(np.floor((phase % 1.0) * fb).astype(np.int64), 0, fb - 1)
            S += np.bincount(sub * fb + fbin, weights=w * y, minlength=nsub * fb).reshape(nsub, fb)
            C += np.bincount(sub * fb + fbin, weights=w, minlength=nsub * fb).reshape(nsub, fb)
            phi_sum += np.bincount(sub, weights=phase, minlength=nsub)
            phi_n += np.bincount(sub, minlength=nsub)
            if toa_bins:
                tb = int(toa_bins)
                tbin = np.clip(np.floor((phase % 1.0) * tb).astype(np.int64), 0, tb - 1)
                S1 += np.bincount(sub * tb + tbin, weights=w * y, minlength=nsub * tb).reshape(nsub, tb)
                C1 += np.bincount(sub * tb + tbin, weights=w, minlength=nsub * tb).reshape(nsub, tb)
    group = fb // nbins
    s_all = S.sum(axis=0); c_all = C.sum(axis=0)
    fine = np.where(c_all > 0, s_all / np.maximum(c_all, 1e-300), 0.0)
    s64 = s_all.reshape(nbins, group).sum(axis=1); c64 = c_all.reshape(nbins, group).sum(axis=1)
    prof0 = np.where(c64 > 0, s64 / np.maximum(c64, 1e-300), 0.0)
    snr0 = profile_snr(prof0)
    width_s = float(pulsar.get("w50_ms", 6.6)) * 1e-3
    m_snr, m_phase = _matched_from_profile(fine, width_s, p_mean)
    phi_mid = phi_sum / np.maximum(phi_n, 1)
    n_periods = float(np.interp(t_last, knots, phi_k) - np.interp(t_first, knots, phi_k))
    step_ppm = max(0.25, 1e6 / (nbins * max(n_periods, 1.0)))
    trials = np.arange(-search_ppm, search_ppm + step_ppm, step_ppm)
    cols = np.arange(fb)

    def shifted(d_ppm):
        # a period longer by d has phase phase/(1+d): each sub-profile moves
        # back by phi_mid * d (cycles), applied as a whole number of fine bins
        shifts = np.round(-phi_mid * d_ppm * 1e-6 * fb).astype(np.int64) % fb
        idx = (cols[None, :] - shifts[:, None]) % fb
        return np.take_along_axis(S, idx, axis=1), np.take_along_axis(C, idx, axis=1)

    snrs = np.empty(len(trials))
    for ii, d in enumerate(trials):
        rs, rc = shifted(d)
        ss = rs.sum(axis=0).reshape(nbins, group).sum(axis=1)
        cc = rc.sum(axis=0).reshape(nbins, group).sum(axis=1)
        snrs[ii] = profile_snr(np.where(cc > 0, ss / np.maximum(cc, 1e-300), 0.0))[0]
    kbest = int(np.argmax(snrs)); best_ppm = float(trials[kbest])
    rs, rc = shifted(best_ppm)
    ss = rs.sum(axis=0).reshape(nbins, group).sum(axis=1)
    cc = rc.sum(axis=0).reshape(nbins, group).sum(axis=1)
    prof_best = np.where(cc > 0, ss / np.maximum(cc, 1e-300), 0.0)
    snr_best = profile_snr(prof_best)
    per_sub = max(1, int(round(SUBINT_S / SUB_S)))
    ns2 = nsub // per_sub

    def _subints(SS, CC, fallback):
        if not ns2:
            return fallback[None, :]
        s2 = SS[:ns2 * per_sub].reshape(ns2, per_sub, nbins, group).sum(axis=(1, 3))
        c2 = CC[:ns2 * per_sub].reshape(ns2, per_sub, nbins, group).sum(axis=(1, 3))
        return np.where(c2 > 0, s2 / np.maximum(c2, 1e-300), 0.0)

    subints = _subints(rs, rc, prof_best)
    # at the predicted period and the absolute phase - what the plot draws
    subints_predicted = _subints(S, C, prof0)
    chan_prof = np.where(chan_cnt > 0, chan_sum / np.maximum(chan_cnt, 1), 0.0)
    f_ghz = freq / 1e9
    delay_s = 4.148808e-3 * pulsar["dm"] * (1.0 / f_ghz ** 2 - 1.0 / f_ghz.max() ** 2)
    v = (np.zeros(2) if pulsar.get("fixed_period")
         else observer_velocity_toward(pulsar["ra_deg"], pulsar["dec_deg"], np.array([t_first, t_last])))
    r = {
        "pulsar": pulsar["name"], "period_bary_s": p_bary, "period_topo_mean_s": p_mean,
        "observer_velocity_m_s": [float(v[0]), float(v[-1])],
        "duration_s": float(t_last - t_first + dt), "dt_s": dt, "n_periods": n_periods,
        "nbins": nbins, "n_clipped": n_clipped, "nchan": int(nchan),
        "profile_predicted": prof0.tolist(), "snr_predicted": snr0[0], "peak_bin_predicted": snr0[1],
        "snr_matched": m_snr, "matched_phase": m_phase, "matched_width_s": width_s,
        "profile_best": prof_best.tolist(), "snr_best": snr_best[0], "peak_bin_best": snr_best[1],
        "best_ppm": best_ppm, "search_ppm": [float(trials.min()), float(trials.max())],
        "search_snr": snrs.tolist(), "search_trial_ppm": trials.tolist(),
        "channel_profiles": chan_prof.tolist(), "channel_freq_hz": freq.tolist(),
        "dm_delay_s": delay_s.tolist(), "subints": subints.tolist(), "subint_s": per_sub * SUB_S,
        "subints_predicted": subints_predicted.tolist(),
        "duty_expected": None,
    }
    if toa_bins:
        # numpy, not lists: for pulsar_toa only, never serialised to the page
        r["toa"] = {"S": S1, "C": C1, "sub_t0": t_first + SUB_S * np.arange(nsub), "sub_s": SUB_S,
                    "knots": knots, "phi_k": phi_k, "p_mean": p_mean, "freq_hz": float(np.mean(freq)),
                    "bw_hz": float(attrs.get("sample_rate_hz") or attrs.get("channel_width_hz", 0.0) * nchan),
                    "dt_s": dt, "t_first": t_first, "t_last": t_last}
    return r, attrs


PRESTO_BIN = "/home/astro/radioconda/envs/presto/bin"   # conda-forge presto-pulsar, own env
# prepfold rebuilt from source with tools/presto_acre_road.patch, so a .fil with
# telescope_id 82 is named "Acre Road SRT" and PRESTO knows the site's ITRF
# position. Its patched libpresto.so sits beside it ($ORIGIN comes first in its
# rpath); everything else it loads from the presto environment. Falls back to
# the stock prepfold, which calls the telescope "Unknown", if it is missing.
PREPFOLD_ACRE = "/home/astro/opt/presto-acre/bin/prepfold"


def prepfold_path():
    return PREPFOLD_ACRE if os.path.exists(PREPFOLD_ACRE) else os.path.join(PRESTO_BIN, "prepfold")


def run_topocentric_period(path):
    """(mean topocentric period, first row time, duration, (f, fd, fdd)) for a
    recording, from its attributes and time marks only - four hours of rows
    are 900 MB and none of them is needed for this.

    (f, fd, fdd) is a cubic fit to the topocentric phase over the run, about
    its first sample: what prepfold's -f/-fd/-fdd take, since prepfold folds
    at a period (and derivatives) held from the start rather than integrating
    the site's changing velocity as our own fold does. A single period leaves
    up to 0.0015 cycles (1 ms) of curvature over four hours - Earth's
    rotation is largest near transit - which is a tenth of a profile bin for
    the fold but the whole TOA error for timing (issue #48); f+fd+fdd leaves
    under 1e-5 cycles."""
    import observation_plot
    with observation_plot.open_readonly(path) as hf:
        a = dict(hf.attrs)
        n = int(hf["power"].shape[0])
        marks = np.asarray(hf["time_marks"][:], float) if "time_marks" in hf else np.empty((0, 2))
    dt = float(a.get("dt_s", DT_S))
    t0 = float(marks[0, 1] - dt * marks[0, 0]) if len(marks) else float(a.get("t0_unix", 0.0))
    t = np.linspace(t0, t0 + n * dt, 400)
    # the same phase model as our own fold (absolute_phase, at the recording's
    # frequency), so the two folds cannot disagree about the pulsar
    phase, p_topo = absolute_phase(t, B0329, float(a.get("center_freq_hz") or 1413e6))
    c = np.polyfit(t - t0, phase - phase[0], 3)  # phase = c0 x^3 + c1 x^2 + c2 x + c3
    f, fd, fdd = c[2], 2 * c[1], 6 * c[0]
    return p_topo, t0, n * dt, (float(f), float(fd), float(fdd))


def presto_phase_offset(path):
    """The absolute phase at the .fil's first sample: prepfold counts phase
    from there, so -phs with this puts its pulse where ours is, at 0.5."""
    import observation_plot
    with observation_plot.open_readonly(path) as hf:
        a = dict(hf.attrs)
        marks = np.asarray(hf["time_marks"][:], float) if "time_marks" in hf else np.empty((0, 2))
    dt = float(a.get("dt_s", DT_S))
    t0 = float(marks[0, 1] - dt * marks[0, 0]) if len(marks) else float(a.get("t0_unix", 0.0))
    ph, _ = absolute_phase(np.array([t0, t0 + 60.0]), B0329, float(a.get("center_freq_hz") or 1413e6))
    return float(ph[0] % 1.0)


def _nchan(path):
    import observation_plot
    with observation_plot.open_readonly(path) as hf:
        return int(hf["power"].shape[1])


def presto_fold(path, out_dir, timeout_s=1800):
    """Export `path` to a .fil and fold it with PRESTO's prepfold at the
    topocentric period and the catalogue DM, no search (DM is not constrained
    by 8 MHz at 1.4 GHz; left to search, prepfold wanders to DM thousands).
    Run at the lowest CPU priority. Returns (png_path, summary dict)."""
    import glob
    import subprocess
    import sigproc_export
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    fil = os.path.join(out_dir, stem + ".fil")
    if not os.path.exists(fil) or os.path.getmtime(fil) < os.path.getmtime(path):
        sigproc_export.export(path, fil)
    p_topo, t0, dur, (f, fd, fdd) = run_topocentric_period(path)
    for old in glob.glob(os.path.join(out_dir, stem + "_prepfold*")):
        os.remove(old)
    # -f/-fd/-fdd at the first sample, not a single period: see run_topocentric_period.
    phs = presto_phase_offset(path)
    cmd = ["nice", "-n", "19", prepfold_path(), "-topo",
           "-f", "%.15f" % f, "-fd", "%.6e" % fd, "-fdd", "%.6e" % fdd, "-phs", "%.6f" % phs,
           "-dm", "%.4f" % B0329["dm"], "-n", "64", "-nsub", str(min(16, _nchan(path))),
           "-nosearch", "-scaleparts", "-noxwin", "-o", stem + "_prepfold", os.path.basename(fil)]
    env = dict(os.environ, PATH=PRESTO_BIN + os.pathsep + os.environ.get("PATH", ""))  # ghostscript for the .png
    res = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True, timeout=timeout_s, env=env)
    pngs = glob.glob(os.path.join(out_dir, stem + "_prepfold*.pfd.png"))
    if res.returncode != 0 or not pngs:
        raise RuntimeError("prepfold failed (%d): %s" % (res.returncode, (res.stderr or res.stdout)[-400:]))
    summary = {"p_topo_s": p_topo, "f_hz": f, "fd_hz_s": fd, "fdd_hz_s2": fdd, "duration_s": dur,
               "png": pngs[0], "prepfold": prepfold_path()}
    bp = glob.glob(os.path.join(out_dir, stem + "_prepfold*.pfd.bestprof"))
    if bp:
        for line in open(bp[0]):
            if "Reduced chi-sqr" in line:
                summary["reduced_chi2"] = float(line.split("=")[1])
            if "Prob(Noise)" in line and "(" in line:
                summary["prob_noise"] = line.split("<")[-1].strip()
    return pngs[0], summary


# ---------------------------------------------------------------------------
# plot


def plot_recording(path, out_path, pulsar=None):
    """Reduce a pulsar-mode recording and draw it.

    Left, this run: its profile at the predicted period and the absolute
    phase (two periods, the pulse at 0.5), and below it the sub-integrations
    on exactly the same phase axis, bin for bin, so a drifting or jumping
    pulse shows as a slanted or broken line under the profile. Right, every
    run so far: the profile of all pulsar recordings added at the absolute
    phase with the EPN template fitted over it, and the timing residuals of
    the whole-run TOAs with the fitted (or held) P and Pdot.

    One fold serves all of it (analyse_file with the 1024-bin timing fold);
    for a recording in the observations folder it also writes this run's
    TOAs to the .tim file and caches its profile for the stack. A file
    anywhere else - a test's - gets the left panels only.
    """
    import plot_backend
    plot_backend.use_headless()
    import matplotlib.pyplot as plt
    import pulsar_toa as T
    r, attrs = analyse_file(path, pulsar, toa_bins=T.TOA_BINS)
    timing = T.in_observations(path)
    acc = fitres = None
    if timing:
        try:
            toas = [t for t in T.toas_for_recording(path, segments=4, analysis=(r, attrs))
                    if t["snr"] >= T.MIN_TOA_SNR]
            if toas:
                T.write_tim(toas)
            T.cache_profile(path, r["toa"], attrs)
            acc = T.accumulated_profile(fold_missing=True)
            fitres = T.timing_fit()
        except Exception as exc:                          # noqa: BLE001 - the plot still draws
            fitres = {"error": str(exc)}
    toa = r.pop("toa")
    nb = r["nbins"]
    edges = np.linspace(0.0, 2.0, 2 * nb + 1)

    fig = plt.figure(figsize=(16, 10))
    gs = fig.add_gridspec(2, 2, height_ratios=[1, 1.15], hspace=0.28, wspace=0.18)
    a0 = fig.add_subplot(gs[0, 0])
    a1 = fig.add_subplot(gs[1, 0], sharex=a0)

    # this run's profile: predicted (the number that counts) in blue
    # Baseline at zero - the off-pulse median, |phase - 0.5| > 0.1 - so the
    # axis is the pulse's own excess; baseline=None below so matplotlib does
    # not pin the axis at zero and cut off the noise that dips below it.
    ph64 = (np.arange(nb) + 0.5) / nb
    off64 = np.abs(ph64 - 0.5) > 0.1
    p0 = 100 * np.array(r["profile_predicted"]); pb = 100 * np.array(r["profile_best"])
    p0 = p0 - np.median(p0[off64]); pb = pb - np.median(pb[off64])
    # One SNR, the matched one at the predicted period (in the title): the
    # peak-bin figure differs by the bin's share of the pulse and only confused.
    a0.stairs(np.concatenate([pb, pb]), edges, color="C1", lw=0.8, alpha=0.8, baseline=None,
              label="best searched period (%+.1f ppm)" % r["best_ppm"])
    a0.stairs(np.concatenate([p0, p0]), edges, color="C0", lw=1.6, baseline=None, label="predicted period")
    a0.axhline(0, color="k", lw=0.5)
    a0.set_ylabel("excess over the off-pulse level (%)")
    a0.set_title("this run: matched SNR %.1f at the predicted period, %.0f min, %d periods"
                 % (r["snr_matched"], r["duration_s"] / 60, r["n_periods"]), fontsize=10)
    a0.legend(fontsize=8, loc="upper center"); a0.grid(alpha=0.3)
    plt.setp(a0.get_xticklabels(), visible=False)

    # the sub-integrations on the same axis, two periods, pixel edges on the bin edges
    sub = 100 * np.array(r["subints_predicted"])
    if sub.size:
        img = np.concatenate([sub, sub], axis=1)
        lo, hi = np.percentile(img, [2, 98])
        a1.imshow(img, aspect="auto", origin="lower", interpolation="nearest", cmap="viridis",
                  extent=[0.0, 2.0, 0.0, sub.shape[0] * r["subint_s"] / 60.0], vmin=lo, vmax=hi)
    for a in (a0, a1):
        for x in (0.5, 1.5):
            a.axvline(x, color="w" if a is a1 else "0.5", lw=0.6, ls=":")
        a.set_xlim(0.0, 2.0)
    a1.set_xticks(np.arange(0.0, 2.01, 0.25))
    a1.set_xlabel("pulse phase (absolute; two periods, the pulse at 0.5 and 1.5)")
    a1.set_ylabel("time from start (min)")
    a1.set_title("sub-integrations of %.0f s at the predicted period" % r["subint_s"], fontsize=10)

    # every run so far, added at the absolute phase
    a2 = fig.add_subplot(gs[0, 1])
    if acc is not None and acc[2]:
        prof, hours, used = acc
        tm = T.template(T.TOA_BINS, toa["dt_s"], toa["bw_hz"] or 8e6, toa["freq_hz"])
        f = T.fit_shift(prof, tm)
        ph = (np.arange(T.TOA_BINS) + 0.5) / T.TOA_BINS
        off = np.abs(ph - 0.5) > 0.1
        prof = prof - np.median(prof[off])
        k = np.arange(1, T.TOA_BINS // 2 + 1)
        Tk = np.fft.rfft(tm)
        ts = np.fft.irfft(np.concatenate([[Tk[0]], Tk[1:] * np.exp(-2j * np.pi * k * f["tau"])]), n=T.TOA_BINS)
        g = 4                                             # shown at 256 bins, fitted at 1024
        # in units of the fitted template's peak: the template reads 1 there
        model = f["b"] * (ts - np.median(ts[off]))
        unit = float(np.max(model)) if np.max(model) > 0 else 1.0
        shown = prof.reshape(-1, g).mean(axis=1) / unit
        a2.stairs(shown, np.linspace(0, 1, T.TOA_BINS // g + 1),
                  color="C0", lw=1.2, baseline=None, label="all runs (%d, %.1f h)" % (len(used), hours))
        a2.axhline(0, color="k", lw=0.5)
        a2.plot(ph, model / unit, color="C3", lw=1.0, alpha=0.4, label="EPN 1410 MHz")
        a2.axvline(0.5, color="0.5", lw=0.6, ls=":")
        a2.set_xlim(0.25, 0.75)                          # the pulse and its outriders, stretched
        a2.set_title("accumulated profile: template SNR %.1f" % f["snr"], fontsize=10)
        # unity near the top, the noise below zero in view, room for the legend
        mid = (np.arange(len(shown)) + 0.5) / len(shown)
        vis = shown[(mid > 0.25) & (mid < 0.75)]             # the limits from what is on screen
        a2.set_ylim(min(float(np.min(vis)) * 1.15, -0.1), max(1.2, float(np.max(vis)) * 1.08))
        a2.legend(fontsize=8, loc="upper left")
    else:
        a2.text(0.5, 0.5, "the accumulated profile covers the observatory's own\nrecordings only",
                ha="center", va="center", transform=a2.transAxes, color="0.4")
    a2.set_xlabel("pulse phase (absolute)"); a2.set_ylabel("relative to the template peak"); a2.grid(alpha=0.3)

    # timing residuals and the fitted period
    a3 = fig.add_subplot(gs[1, 1])
    if fitres and not fitres.get("error"):
        mjd = np.array(fitres["mjd"]); res = np.array(fitres["resid_us"]) / 1e3
        err = np.array(fitres["err_us"]) / 1e3; whole = np.array(fitres["whole"], bool)
        pps = np.array([p == "1" for p in fitres["pps"]])
        m0 = np.floor(mjd.min()) if len(mjd) else 0.0
        raw = np.array(fitres.get("err_raw_us", fitres["err_us"])) / 1e3
        # Statistical errors throughout. The fit itself still weights a
        # host-clock TOA with its clock term (EQUAD); drawn, that bar hid the
        # measurement, and a run's segments share one clock offset anyway.
        # One TOA per night (red, fitted) - over weeks these show the pulsar and
        # the model, timing noise included - and that night's 4 h segments
        # (grey), a check on how its arrival times behave within the night.
        if (~whole).any():
            a3.errorbar(mjd[~whole] - m0, res[~whole], raw[~whole], fmt=".", color="0.6", ms=5, lw=0.8,
                        label="4 h segments (a check, not fitted)")
        for sel, mk, lab in ((whole & pps, "o", "night TOA (fitted), PPS time"),
                             (whole & ~pps, "s", "night TOA (fitted), host clock")):
            if sel.any():
                a3.errorbar(mjd[sel] - m0, res[sel], raw[sel], fmt=mk, color="C3", mfc="C3" if mk == "o" else "none",
                            ms=7, lw=1.4, capsize=3, label=lab)
        a3.legend(fontsize=8, loc="lower left")
        a3.axhline(0, color="k", lw=0.5)
        lo_y = float(np.min(res - raw)); hi_y = float(np.max(res + raw))
        span_y = max(hi_y - lo_y, 1.0)
        a3.set_ylim(lo_y - 0.12 * span_y, hi_y + 0.75 * span_y)      # headroom for the numbers
        a3.set_xlabel("MJD - %d" % m0); a3.set_ylabel("residual (ms)")
        free = fitres["free"]
        pl = ("P    = %.12f s" % fitres["P"]) + (" +- %.2e (fitted)" % fitres["P_err"] if "F0" in free else "  (catalogue, held)")
        pdl = ("Pdot = %.4e" % fitres["Pdot"]) + (" +- %.1e (fitted)" % fitres["Pdot_err"] if "F1" in free else "  (catalogue, held)")
        need = ("" if "F1" in free else
                "\nP fitted once 3 runs span a day; Pdot once 4 span 3 weeks" if not free else
                "\nPdot fitted once 4 runs span 3 weeks")
        chi = ("chi2 %.1f / %d dof" % (fitres["chi2"], fitres["dof"])) if fitres.get("chi2") is not None else ""
        a3.text(0.99, 0.98, "%s\n%s\nat MJD %.3f (barycentric)\n%d night TOAs over %.1f d  %s%s"
                % (pl, pdl, fitres["pepoch"], fitres["n_whole"], fitres["span_days"], chi, need),
                transform=a3.transAxes, ha="right", va="top", fontsize=8, family="monospace",
                bbox=dict(boxstyle="round", fc="white", ec="0.7", alpha=0.9))
        a3.set_title("timing residuals (PINT): one TOA per night's run, fitted", fontsize=10)
    else:
        a3.text(0.5, 0.5, (fitres or {}).get("error") or "timing covers the observatory's own recordings only",
                ha="center", va="center", transform=a3.transAxes, color="0.4")
    a3.grid(alpha=0.3)

    fig.suptitle("%s  %s   %d overflows, %d time marks%s%s" % (
        os.path.basename(path), attrs.get("obs_name", ""),
        int(attrs.get("overflows_total", 0)), int(attrs.get("n_time_marks", 0)),
        "  (exact)" if int(attrs.get("time_marks_exact", 0)) else "",
        ";  clock: %s" % attrs.get("time_source", "host")), fontsize=10)
    fig.savefig(out_path, dpi=90, bbox_inches="tight"); plt.close(fig)
    return r


def main():
    import argparse
    p = argparse.ArgumentParser(description="fold a pulsar-mode recording")
    p.add_argument("recording"); p.add_argument("--out", default=None)
    args = p.parse_args()
    out = args.out or os.path.splitext(args.recording)[0] + "_fold.png"
    r = plot_recording(args.recording, out)
    print(json.dumps({k: v for k, v in r.items() if not isinstance(v, list)}, indent=1))
    print("plot:", out)


if __name__ == "__main__":
    main()
