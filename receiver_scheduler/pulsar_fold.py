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
SUBINT_S = 120.0        # sub-integration length for the time-phase panel
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
    "period_s": 0.714519699726, "pdot": 2.0496e-15, "pepoch_mjd": 46473.0,
    "dm": 26.7641, "s1400_mjy": 203.0, "w50_ms": 6.6,
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


def row_times(n_rows, dt_s, t0_unix, time_marks=None):
    """The time of each row. With the radio's `time_marks` - (row, device
    time) at the start and after every overflow - each stretch between marks
    is timed from its own mark and the row count, so a dropped block moves
    the rows after it by exactly what was dropped rather than not at all.
    Without marks (a demo recording, an old file) the rows are t0 + i dt."""
    i = np.arange(n_rows, dtype=float)
    marks = np.asarray(time_marks, float).reshape(-1, 2) if time_marks is not None else np.empty((0, 2))
    marks = marks[np.argsort(marks[:, 0])] if len(marks) else marks
    if not len(marks):
        return t0_unix + dt_s * i
    t = np.empty(n_rows)
    rows = marks[:, 0].astype(int)
    for k, (r, tm) in enumerate(zip(rows, marks[:, 1])):
        lo = 0 if k == 0 else r
        hi = rows[k + 1] if k + 1 < len(rows) else n_rows
        t[lo:hi] = tm + dt_s * (i[lo:hi] - r)
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
        "duration_s": float(t[-1] - t[0] + dt), "dt_s": dt, "n_periods": float(phase0[-1]),
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


def analyse_file(path, pulsar=None, nbins=NBINS, search_ppm=SEARCH_PPM, block_rows=BLOCK_ROWS):
    """`analyse` for a recording on disk, without ever holding it in memory.

    A four-hour run is 14 million rows by 16 channels, 900 MB as float32;
    `analyse` works on the whole array in float64 and needed 5.3 GB for it,
    which on this 7.7 GB host had the kernel kill the scheduler on
    2026-09-26 (the Observe tab plots a recording as soon as it is chosen).
    Here the rows are read ten minutes at a time. Each channel is divided by
    its own median over 10 s blocks (the gain-drift baseline, piecewise
    rather than running - the drift is far slower than 10 s), clipped at 6
    robust sigma within the block, folded into the per-channel profiles and
    summed into one float32 series; only that series, the row times and the
    phase are kept whole. The period search then works on 60 s sub-profiles
    of the fine (256-bin) fold, shifted per trial period, as prepfold does,
    instead of refolding 14 million samples per trial.
    """
    import observation_plot
    with observation_plot.open_readonly(path) as hf:
        attrs = {k: (v.item() if hasattr(v, "item") else v) for k, v in hf.attrs.items()}
        ds = hf["power"]
        n, nchan = ds.shape
        freq = np.asarray(hf["frequency_hz"][:], float)
        marks = np.asarray(hf["time_marks"][:], float) if "time_marks" in hf else np.empty((0, 2))
        if "overflow_marks" in hf and hf["overflow_marks"].shape[0]:
            attrs["overflows_total"] = int(np.asarray(hf["overflow_marks"][:])[:, 1].sum())
        attrs["n_time_marks"] = int(len(marks))
        if pulsar is None:
            pulsar = lookup(attrs.get("pulsar_name") or attrs.get("object_name") or PULSAR_NAME)
        if pulsar is None:
            raise ValueError("this mode folds B0329+54 only; the recording names %r"
                             % (attrs.get("pulsar_name") or attrs.get("object_name")))
        if n < 1000:
            raise ValueError("too few samples to fold (%d rows)" % n)
        dt = float(attrs.get("dt_s", DT_S))
        t0 = float(marks[0, 1] - dt * marks[0, 0]) if len(marks) else float(attrs.get("t0_unix", 0.0))
        t = row_times(n, dt, t0, marks if len(marks) else None)
        p_bary = period_at(t[0]) if pulsar.get("pdot") is not None else pulsar["period_s"]
        phase0, p_mean = phase_track(t, p_bary, pulsar["ra_deg"], pulsar["dec_deg"])
        bins0 = np.clip(np.floor((phase0 % 1.0) * nbins).astype(np.int32), 0, nbins - 1)
        total = np.empty(n, dtype=np.float32)
        chan_sum = np.zeros((nchan, nbins))
        chan_cnt = np.zeros((nchan, nbins))
        per = max(1, int(round(DETREND_S / dt)))
        for i in range(0, n, block_rows):
            x = np.asarray(ds[i:min(n, i + block_rows)], dtype=np.float64)
            m = len(x)
            k = np.arange(m) // per
            frac = np.empty_like(x)
            for j in np.unique(k):
                sl = k == j
                base = np.median(x[sl], axis=0)
                base[base <= 0] = np.nan
                frac[sl] = x[sl] / base - 1.0
            frac[~np.isfinite(frac)] = 0.0
            b = bins0[i:i + m]
            for c in range(nchan):
                col = frac[:, c]
                mad = 1.4826 * np.median(np.abs(col - np.median(col)))
                good = np.abs(col) <= 6.0 * mad if mad > 0 else np.ones(m, bool)
                chan_sum[c] += np.bincount(b[good], weights=col[good], minlength=nbins)
                chan_cnt[c] += np.bincount(b[good], minlength=nbins)
            total[i:i + m] = frac.mean(axis=1)
    total, n_clipped = clip_rfi(total.astype(np.float64))
    prof0, _ = fold(total, phase0, nbins)
    snr0 = profile_snr(prof0)
    width_s = float(pulsar.get("w50_ms", 6.6)) * 1e-3
    m_snr, m_phase, _ = matched_snr(total, phase0, width_s, p_mean)
    # 60 s sub-profiles of the fine fold, for the period search and the panel
    fb = FINE_BINS
    sub = np.floor((t - t[0]) / 60.0).astype(np.int64)
    nsub = int(sub.max()) + 1
    fbin = np.clip(np.floor((phase0 % 1.0) * fb).astype(np.int64), 0, fb - 1)
    S = np.bincount(sub * fb + fbin, weights=total, minlength=nsub * fb).reshape(nsub, fb)
    C = np.bincount(sub * fb + fbin, minlength=nsub * fb).reshape(nsub, fb).astype(float)
    phi_mid = np.bincount(sub, weights=phase0, minlength=nsub) / np.maximum(np.bincount(sub, minlength=nsub), 1)
    n_periods = float(phase0[-1] - phase0[0])
    step_ppm = max(0.25, 1e6 / (nbins * max(n_periods, 1.0)))
    trials = np.arange(-search_ppm, search_ppm + step_ppm, step_ppm)
    group = fb // nbins

    def shifted(d_ppm):
        # a period longer by d has phase phase0/(1+d): each sub-profile moves
        # back by phi_mid * d (cycles), applied as a whole number of fine bins
        shifts = np.round(-phi_mid * d_ppm * 1e-6 * fb).astype(int) % fb
        ss = np.zeros(fb); cc = np.zeros(fb)
        rows_s = np.empty_like(S); rows_c = np.empty_like(C)
        for kk in range(nsub):
            rows_s[kk] = np.roll(S[kk], shifts[kk]); rows_c[kk] = np.roll(C[kk], shifts[kk])
        return rows_s, rows_c

    snrs = np.empty(len(trials))
    for ii, d in enumerate(trials):
        rs, rc = shifted(d)
        s64 = rs.sum(axis=0).reshape(nbins, group).sum(axis=1)
        c64 = rc.sum(axis=0).reshape(nbins, group).sum(axis=1)
        snrs[ii] = profile_snr(np.where(c64 > 0, s64 / np.maximum(c64, 1), 0.0))[0]
    kbest = int(np.argmax(snrs)); best_ppm = float(trials[kbest])
    rs, rc = shifted(best_ppm)
    s64 = rs.sum(axis=0).reshape(nbins, group).sum(axis=1)
    c64 = rc.sum(axis=0).reshape(nbins, group).sum(axis=1)
    prof_best = np.where(c64 > 0, s64 / np.maximum(c64, 1), 0.0)
    snr_best = profile_snr(prof_best)
    per_sub = max(1, int(round(SUBINT_S / 60.0)))
    ns2 = nsub // per_sub
    if ns2:
        s2 = rs[:ns2 * per_sub].reshape(ns2, per_sub, nbins, group).sum(axis=(1, 3))
        c2 = rc[:ns2 * per_sub].reshape(ns2, per_sub, nbins, group).sum(axis=(1, 3))
        subints = np.where(c2 > 0, s2 / np.maximum(c2, 1), 0.0)
    else:
        subints = prof_best[None, :]
    chan_prof = np.where(chan_cnt > 0, chan_sum / np.maximum(chan_cnt, 1), 0.0)
    f_ghz = freq / 1e9
    delay_s = 4.148808e-3 * pulsar["dm"] * (1.0 / f_ghz ** 2 - 1.0 / f_ghz.max() ** 2)
    v = observer_velocity_toward(pulsar["ra_deg"], pulsar["dec_deg"], np.array([t[0], t[-1]]))
    r = {
        "pulsar": pulsar["name"], "period_bary_s": p_bary, "period_topo_mean_s": p_mean,
        "observer_velocity_m_s": [float(v[0]), float(v[-1])],
        "duration_s": float(t[-1] - t[0] + dt), "dt_s": dt, "n_periods": n_periods,
        "nbins": nbins, "n_clipped": n_clipped, "nchan": int(nchan),
        "profile_predicted": prof0.tolist(), "snr_predicted": snr0[0], "peak_bin_predicted": snr0[1],
        "snr_matched": m_snr, "matched_phase": m_phase, "matched_width_s": width_s,
        "profile_best": prof_best.tolist(), "snr_best": snr_best[0], "peak_bin_best": snr_best[1],
        "best_ppm": best_ppm, "search_ppm": [float(trials.min()), float(trials.max())],
        "search_snr": snrs.tolist(), "search_trial_ppm": trials.tolist(),
        "channel_profiles": chan_prof.tolist(), "channel_freq_hz": freq.tolist(),
        "dm_delay_s": delay_s.tolist(), "subints": subints.tolist(), "subint_s": per_sub * 60.0,
        "duty_expected": None,
    }
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
    phase, p_topo = phase_track(t, period_at(t0), B0329["ra_deg"], B0329["dec_deg"])
    c = np.polyfit(t - t0, phase, 3)            # phase = c0 x^3 + c1 x^2 + c2 x + c3
    f, fd, fdd = c[2], 2 * c[1], 6 * c[0]
    return p_topo, t0, n * dt, (float(f), float(fd), float(fdd))


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
    cmd = ["nice", "-n", "19", prepfold_path(), "-topo",
           "-f", "%.15f" % f, "-fd", "%.6e" % fd, "-fdd", "%.6e" % fdd,
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
    """Reduce a pulsar-mode recording and draw it: the folded profile, the
    period search, the sub-integrations and the per-channel profiles with the
    dispersion delay the DM predicts. Returns the analysis dict."""
    import plot_backend
    plot_backend.use_headless()
    import matplotlib.pyplot as plt
    # Streamed, never the whole file in memory: see analyse_file.
    r, attrs = analyse_file(path, pulsar)
    pulsar = lookup(r["pulsar"])
    nb = r["nbins"]
    ph = (np.arange(2 * nb) + 0.5) / nb
    prof = np.array(r["profile_best"]); prof2 = np.concatenate([prof, prof])
    fig, ax = plt.subplots(2, 2, figsize=(16, 9))
    a = ax[0][0]
    a.step(ph, 100 * prof2, where="mid", color="C0", label="folded at the best period")
    p0 = np.array(r["profile_predicted"]); a.step(ph, 100 * np.concatenate([p0, p0]), where="mid",
                                                  color="C1", lw=0.8, alpha=0.7, label="at the predicted period")
    a.set_xlabel("pulse phase (two periods)"); a.set_ylabel("excess over the running median (%)")
    a.set_title("%s: matched S/N %.1f at the predicted period (peak bin %.1f; best searched %.1f), %.0f min, %d periods, %d channels" % (
        r["pulsar"], r["snr_matched"], r["snr_predicted"], r["snr_best"], r["duration_s"] / 60, r["n_periods"], r["nchan"]), fontsize=10)
    a.legend(fontsize=8); a.grid(alpha=0.3)
    a = ax[0][1]
    a.plot(r["search_trial_ppm"], r["search_snr"], "-", lw=1)
    a.axvline(r["best_ppm"], color="C3", lw=0.8, ls="--", label="best %+.1f ppm" % r["best_ppm"])
    a.axvline(0, color="k", lw=0.5)
    a.set_xlabel("fractional period offset from the topocentric prediction (ppm)"); a.set_ylabel("S/N")
    a.set_title("period search: barycentric %.9f s, topocentric mean %.9f s (v toward %.2f..%.2f km/s)" % (
        r["period_bary_s"], r["period_topo_mean_s"], r["observer_velocity_m_s"][0] / 1e3, r["observer_velocity_m_s"][1] / 1e3),
        fontsize=9)
    a.legend(fontsize=8); a.grid(alpha=0.3)
    a = ax[1][0]
    sub = np.array(r["subints"])
    if sub.size:
        a.imshow(100 * sub, aspect="auto", origin="lower", extent=[0, 1, 0, sub.shape[0] * r["subint_s"] / 60],
                 cmap="viridis", interpolation="nearest")
    a.set_xlabel("pulse phase"); a.set_ylabel("time (min)"); a.set_title("sub-integrations of %.0f s" % r["subint_s"])
    a = ax[1][1]
    cp = np.array(r["channel_profiles"]); f = np.array(r["channel_freq_hz"]) / 1e6
    if cp.size and cp.shape[0] == 1:
        a.step(np.arange(nb) / nb, 100 * cp[0], where="post")
        a.set_xlabel("pulse phase"); a.set_ylabel("excess (%)")
        a.set_title("single band-summed channel", fontsize=9)
    elif cp.size:
        a.imshow(100 * cp, aspect="auto", origin="lower", extent=[0, 1, f.min(), f.max()],
                 cmap="viridis", interpolation="nearest")
        # where the DM says each channel's peak should sit, relative to the summed peak
        pk = (r["peak_bin_best"] + 0.5) / nb
        d = np.array(r["dm_delay_s"]) / r["period_topo_mean_s"]
        a.plot((pk + d - np.mean(d)) % 1.0, f, "w--", lw=0.8, label="DM %.1f delay" % pulsar["dm"])
        a.legend(fontsize=8, loc="upper right")
        a.set_xlabel("pulse phase"); a.set_ylabel("frequency (MHz)"); a.set_title("per-channel profiles (interference view; the 0.6 ms DM sweep is below one sample)", fontsize=9)
    fig.suptitle("%s  %s  %s  %d overflows, %d time marks%s" % (
        os.path.basename(path), attrs.get("obs_name", ""),
        "%d samples clipped;" % r["n_clipped"] if r["n_clipped"] else "",
        int(attrs.get("overflows_total", 0)), int(attrs.get("n_time_marks", 0)),
        "" if attrs.get("n_time_marks", 0) else " (no radio time marks: rows timed by count alone)"), fontsize=10)
    fig.tight_layout()
    fig.savefig(out_path, dpi=90); plt.close(fig)
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
