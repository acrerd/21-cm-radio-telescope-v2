#!/usr/bin/env python3
"""Times of arrival for B0329+54 from pulsar-mode recordings, and the .tim file.

The recording is folded at the absolute phase (`pulsar_fold.absolute_phase`,
the same zero every night) into 1024 bins per 60 s; the profile of a span is
matched against a template by a least-squares fit of a shift, amplitude and
offset (Taylor 1992, done in the Fourier domain so the shift is continuous,
not a bin), and the shift becomes a topocentric UTC arrival time at the
observing frequency. Barycentring and dispersion are left to the timing
program (PINT or tempo2), which does them from the par file; ours are used
only to fold.

Template: the EPN 1410 MHz profile (von Hoensbroech & Xilouris 1997, hx97b),
smoothed exactly as our system smooths - one row, the dispersion sweep
across our band, one fold bin - and placed with its main peak at phase 0.5,
which is where `absolute_phase`'s offset puts the pulse. It is noise-free and
independent of our data, so it cannot pull a TOA toward our own noise. Our
single feed sees I plus some mixture of Q, U and V (V/I -0.34 at the peak),
so our profile's shape differs slightly: a constant difference is absorbed by
the timing fit's phase; one that changes with parallactic angle would show
as TOAs that depend on hour angle - test for it.

Precision: for a resolved pulse the fold's bin width drops out, and the error
is about 0.6 x FWHM / (matched S/N), ~0.45 ms for the first 16 h detection.

    python pulsar_toa.py data/observations/20260926_203116_pulsar.h5 [--segments 4] [--no-write]
"""
import json
import os
import re

import numpy as np

import pulsar_fold as PF

HERE = os.path.dirname(os.path.abspath(__file__))
TOA_BINS = 1024
# tracked in git (data/ is not): the timing depends on it. Source and licence
# in pulsar_templates/README.md.
TEMPLATE_FILE = os.path.join(HERE, "pulsar_templates", "b0329_1410_epn_hx97b.npz")
TEMPLATE_NAME = "epn_hx97b_1410"
# In git, unlike data/: the TOAs and the folded profiles are the hard-won
# product - 16 h of telescope time each - where a raw recording is, in the
# end, re-observable. Plotting writes here; committing stays a person's act.
TOA_DIR = os.path.join(HERE, "pulsar_timing")
# One TOA per night, and nothing else: what any timing program should fit.
# The 4 h segments are the same photons again, so they live in a file of
# their own - in one file PINT, tempo2 or pintk would count a night twice,
# and nothing in the format can tell them not to (2026-09-28).
TIM_FILE = os.path.join(TOA_DIR, "b0329.tim")
# A pulsar-monitor night can come in pieces - a booking or a hand-started job
# interrupts it and the monitor resumes afterwards - and every piece carries
# `pulsar_session`, when its window opened. The night's TOA is fitted on
# the pieces' folds added at the absolute phase (`night_<session>`), one per
# night as for an unbroken run; each piece's own TOA (`<recording>_p`) goes
# with the segments, a check on the same data rather than more of it.
SEGMENT_NAME = re.compile(r"_(s\d+|p)$")
PIECE_SUFFIX = "_p"
NIGHT_PREFIX = "night_"
PAR_FILE = os.path.join(TOA_DIR, "B0329+54.par")
# Our site as a PINT observatory (pint_tools.register_site), at the surveyed
# position rather than PINT's own "acre", which is 49 m away.
SITE_CODE = "ar"
# Below this a template fit can lock onto a noise feature and its error stops
# being Gaussian, so the TOA is not written: a faint night adds nothing but a
# wrong point to the timing fit (whole runs and segments alike).
MIN_TOA_SNR = 6.0
HOST_CLOCK_EQUAD_US = 2000.0      # NTP root distance ~1-4 ms (timesyncd), until the PPS
PROFILE_CACHE_DIR = os.path.join(TOA_DIR, "profiles")    # one folded profile per recording
OBS_DIR = os.path.join(HERE, "data", "observations")
PINT_PY = "/home/astro/radioconda/envs/pint/bin/python"
SEGMENT_SUFFIX = "_s"             # tim names: <recording> for the run, <recording>_sN per segment


# ---------------------------------------------------------------------------
# template


def _circular_boxcar(x, width_frac):
    """Convolve a periodic profile with a boxcar `width_frac` of a period
    wide, exactly, via its transform (the sinc of the width)."""
    if width_frac <= 0:
        return x
    k = np.fft.rfftfreq(len(x), 1.0 / len(x))
    return np.fft.irfft(np.fft.rfft(x) * np.sinc(k * width_frac), n=len(x))


def dispersion_smear_s(freq_hz, bw_hz, dm=None):
    """The dispersion sweep across a band, seconds."""
    dm = PF.B0329["dm"] if dm is None else dm
    f_lo, f_hi = (freq_hz - bw_hz / 2) / 1e6, (freq_hz + bw_hz / 2) / 1e6
    return PF.K_DM_S * dm * (1.0 / f_lo ** 2 - 1.0 / f_hi ** 2)


def template(nbins=TOA_BINS, dt_s=1e-3, bw_hz=32e6, freq_hz=1413e6, period_s=None):
    """The EPN profile as our fold would record it: main peak at phase 0.5,
    smoothed by one row, the band's dispersion sweep and one fold bin (each a
    boxcar), baseline zero, peak one."""
    d = np.load(TEMPLATE_FILE)
    t_ms, I = d["t_ms"], d["I"]
    P = period_s or PF.period_at(1790000000.0)
    # the EPN profile on a fine grid of phase, main peak (t = 0) at 0.5
    m = nbins * 8
    ph = (np.arange(m) + 0.5) / m
    src_ph = 0.5 + t_ms / (1e3 * P)
    order = np.argsort(src_ph % 1.0)
    fine = np.interp(ph, (src_ph % 1.0)[order], I[order], period=1.0)
    for w in (dt_s / P, dispersion_smear_s(freq_hz, bw_hz) / P, 1.0 / nbins):
        fine = _circular_boxcar(fine, w)
    prof = fine.reshape(nbins, 8).mean(axis=1)
    prof -= np.median(prof)
    return prof / prof.max()


# ---------------------------------------------------------------------------
# the fit


def fit_shift(profile, tmpl):
    """Fit profile = a + b * tmpl(phase - tau). Returns tau (cycles, in
    [-0.5, 0.5)), its error from the Fisher information, b, the matched S/N
    and the reduced chi-squared. Fourier domain: every harmonic but the mean,
    so tau is continuous rather than a bin; a 16x oversampled
    cross-correlation finds the peak, a bounded 1-d maximisation refines it."""
    from scipy.optimize import minimize_scalar
    p = np.asarray(profile, float)
    t = np.asarray(tmpl, float)
    n = len(p)
    Pk = np.fft.rfft(p)[1:]
    Tk = np.fft.rfft(t)[1:]
    k = np.arange(1, len(Pk) + 1)
    cross = Pk * np.conj(Tk)
    over = 16
    cc = np.fft.irfft(np.concatenate([[0.0], cross]), n=n * over)
    tau0 = np.argmax(cc) / (n * over)

    def neg(tau):
        return -np.sum((cross * np.exp(2j * np.pi * k * tau)).real)

    res = minimize_scalar(neg, bounds=(tau0 - 1.0 / (n * over), tau0 + 1.0 / (n * over)), method="bounded",
                          options={"xatol": 1e-9})
    tau = float(res.x)
    shift = np.exp(-2j * np.pi * k * tau)
    Ts = np.fft.irfft(np.concatenate([[np.fft.rfft(t)[0]], Tk * shift]), n=n)
    Ts0 = Ts - Ts.mean()
    b = float(np.sum((p - p.mean()) * Ts0) / np.sum(Ts0 ** 2))
    resid = (p - p.mean()) - b * Ts0
    sigma = float(np.sqrt(np.sum(resid ** 2) / (n - 3)))
    dT = np.fft.irfft(np.concatenate([[0.0], 2j * np.pi * k * Tk * shift]), n=n)   # d tmpl / d phase
    sigma_tau = float(sigma / (abs(b) * np.sqrt(np.sum(dT ** 2)))) if b != 0 else np.inf
    snr = float(b * np.sqrt(np.sum(Ts0 ** 2)) / sigma) if sigma > 0 else 0.0
    tau = (tau + 0.5) % 1.0 - 0.5
    return {"tau": tau, "sigma_tau": sigma_tau, "b": b, "snr": snr, "sigma_bin": sigma,
            "chi2_red": float(np.sum(resid ** 2) / (n - 3) / sigma ** 2) if sigma > 0 else np.nan}


# ---------------------------------------------------------------------------
# from a recording to TOAs


def span_profile(toa, t_a=None, t_b=None):
    """The weighted fine profile of the 60 s pieces whose start lies in
    [t_a, t_b), and the time span they cover."""
    t0 = toa["sub_t0"]
    sel = np.ones(len(t0), bool)
    if t_a is not None:
        sel &= t0 >= t_a
    if t_b is not None:
        sel &= t0 < t_b
    S = toa["S"][sel].sum(axis=0)
    C = toa["C"][sel].sum(axis=0)
    prof = np.where(C > 0, S / np.maximum(C, 1e-300), 0.0)
    if not sel.any():
        return prof, None, None
    lo = max(float(t0[sel][0]), toa["t_first"])
    hi = min(float(t0[sel][-1]) + toa["sub_s"], toa["t_last"])
    return prof, lo, hi


def arrival_time(toa, tau, t_mid):
    """The topocentric UTC time (unix) of the pulse nearest `t_mid`: where the
    folding phase model reads n + 0.5 + tau - the template's zero at 0.5 plus
    the measured shift. Linear between the 60 s phase knots (1e-7 period)."""
    knots, phi = toa["knots"], toa["phi_k"]
    ph_mid = float(np.interp(t_mid, knots, phi))
    n = np.round(ph_mid - 0.5 - tau)
    return float(np.interp(n + 0.5 + tau, phi, knots))


def mjd_string(t_unix, decimals=13):
    """UTC MJD as text to `decimals` places, from astropy's two-part time -
    never via one float64 (whose MJD resolution is ~0.6 us)."""
    from astropy.time import Time
    t = Time(t_unix, format="unix", scale="utc")
    day = int(np.floor(t.mjd))
    frac = (t - Time(day, format="mjd", scale="utc")).to_value("day")
    if frac >= 1.0:
        day, frac = day + 1, frac - 1.0
    return "%d.%s" % (day, ("%.*f" % (decimals, frac)).split(".")[1])


def toas_for_recording(path, segments=1, tmpl=None, pulsar=None, analysis=None):
    """One TOA for the whole run and, with `segments` > 1, one per equal part.
    `analysis` is an (r, attrs) from analyse_file(..., toa_bins=TOA_BINS)
    already made - the plot's own fold - so the file is read once."""
    r, attrs = analysis if analysis is not None else PF.analyse_file(path, pulsar=pulsar, toa_bins=TOA_BINS)
    toa = r["toa"]
    tm = tmpl if tmpl is not None else template(TOA_BINS, toa["dt_s"], toa["bw_hz"] or 8e6, toa["freq_hz"])
    base = os.path.basename(path)
    pps = int(str(attrs.get("time_source", "host")) == "pps" and int(attrs.get("time_pps_verified", 0)) == 1)
    spans = [(None, None, base)]
    if segments and segments > 1:
        edges = np.linspace(toa["t_first"], toa["t_last"], segments + 1)
        spans += [(edges[i], edges[i + 1], "%s_s%d" % (base, i)) for i in range(segments)]
    out = []
    for t_a, t_b, name in spans:
        prof, lo, hi = span_profile(toa, t_a, t_b)
        if lo is None:
            continue
        fit = fit_shift(prof, tm)
        t_toa = arrival_time(toa, fit["tau"], 0.5 * (lo + hi))
        out.append({
            "name": name, "recording": base, "t_unix": t_toa, "mjd": mjd_string(t_toa),
            "err_us": fit["sigma_tau"] * toa["p_mean"] * 1e6, "freq_mhz": toa["freq_hz"] / 1e6,
            "snr": fit["snr"], "tau": fit["tau"], "sigma_tau": fit["sigma_tau"], "chi2_red": fit["chi2_red"],
            "tobs_s": hi - lo, "bw_mhz": (toa["bw_hz"] or 0.0) / 1e6, "pps": pps,
            "time_source": str(attrs.get("time_source", "host")),
            "time_marks_exact": int(attrs.get("time_marks_exact", 0)),
            "template": TEMPLATE_NAME, "toa_bins": TOA_BINS, "phase_offset": PF.B0329["phase_offset"],
        })
    return out


def _stored_session(d):
    return str(d["session"]) if "session" in d.files else ""


def session_toa(session, tmpl=None):
    """One TOA for a pulsar-monitor night from every stored profile of that
    session, added at the absolute phase - so it needs the pieces' profiles
    cached first (cache_profile). None if none are stored. The TOA sits at
    the pieces' time-weighted middle; tobs is the recorded time, not the span."""
    import glob
    S = C = None
    pieces = []
    for store in sorted(glob.glob(os.path.join(PROFILE_CACHE_DIR, "*.npz"))):
        d = np.load(store)
        if _stored_session(d) != session or int(d["nbins"]) != TOA_BINS \
                or float(d["row_centre"]) != PF.ROW_CENTRE:
            continue
        s_, c_ = d["S"], d["C"]
        shift = PF.B0329["phase_offset"] - float(d["phase_offset"])
        if shift:
            s_, c_ = _rotated(s_, shift), _rotated(c_, shift)
        S = s_ if S is None else S + s_
        C = c_ if C is None else C + c_
        pps = int(d["pps"]) if "pps" in d.files else int(str(d["time_source"]) == "pps")
        pieces.append({"t0": float(d["t_first_unix"]), "t1": float(d["t_last_unix"]),
                       "freq_hz": float(d["freq_hz"]), "bw_hz": float(d["bw_hz"]),
                       "dt_s": float(d["dt_s"]), "pps": pps,
                       "exact": int(d["time_marks_exact"]) if "time_marks_exact" in d.files else 0})
    if not pieces:
        return None
    prof = np.where(C > 0, S / np.maximum(C, 1e-300), 0.0)
    first = pieces[0]
    tm = tmpl if tmpl is not None else template(TOA_BINS, first["dt_s"], first["bw_hz"] or 8e6, first["freq_hz"])
    fit = fit_shift(prof, tm)
    dur = np.array([p["t1"] - p["t0"] for p in pieces])
    mids = np.array([0.5 * (p["t0"] + p["t1"]) for p in pieces])
    t_mid = float(np.sum(dur * mids) / np.sum(dur)) if dur.sum() > 0 else float(mids[0])
    knots = t_mid + np.arange(-600.0, 601.0, 60.0)
    phi_k, p_mean = PF.absolute_phase(knots, PF.B0329, first["freq_hz"])
    t_toa = arrival_time({"knots": knots, "phi_k": phi_k}, fit["tau"], t_mid)
    name = NIGHT_PREFIX + session
    return {
        "name": name, "recording": name, "session": session, "pieces": len(pieces),
        "t_unix": t_toa, "mjd": mjd_string(t_toa),
        "err_us": fit["sigma_tau"] * p_mean * 1e6, "freq_mhz": first["freq_hz"] / 1e6,
        "snr": fit["snr"], "tau": fit["tau"], "sigma_tau": fit["sigma_tau"], "chi2_red": fit["chi2_red"],
        "tobs_s": float(dur.sum()), "bw_mhz": first["bw_hz"] / 1e6,
        "pps": int(all(p["pps"] for p in pieces)),
        "time_source": "pps" if all(p["pps"] for p in pieces) else "host",
        "time_marks_exact": int(all(p["exact"] for p in pieces)),
        "template": TEMPLATE_NAME, "toa_bins": TOA_BINS, "phase_offset": PF.B0329["phase_offset"],
    }


def timing_toas(path, analysis, segments=4):
    """The TOAs a recording contributes: its own run and segments, and - for a
    piece of a pulsar-monitor night - the run renamed as a piece (a check)
    and the whole night's TOA recomputed with this piece in. Cache the
    recording's profile (cache_profile) before calling."""
    r, attrs = analysis
    toas = toas_for_recording(path, segments=segments, analysis=analysis)
    session = str(attrs.get("pulsar_session", "") or "")
    if not session:
        return toas
    base = os.path.basename(path)
    for t in toas:
        if t["name"] == base:
            t["name"] = base + PIECE_SUFFIX
    night = session_toa(session)
    if night is not None:
        toas.append(night)
    return toas


# ---------------------------------------------------------------------------
# the .tim file


def tim_line(t):
    """One tempo2-format TOA line. The flags say what a fit needs to select
    or weight by: -pps 0 is a host-clock TOA (few ms of clock error)."""
    return ("%s %.6f %s %.3f %s -pps %d -snr %.1f -tobs %.0f -bw %.1f -be b200 -fe sawbird -tmpl %s"
            % (t["name"], t["freq_mhz"], t["mjd"], t["err_us"], SITE_CODE, t["pps"], t["snr"],
               t["tobs_s"], t["bw_mhz"], t["template"]))


def segment_file(tim_file=TIM_FILE):
    """Where a .tim file's segments go: b0329.tim -> b0329_segments.tim."""
    root, ext = os.path.splitext(tim_file)
    return root + "_segments" + ext


SEGMENT_HEADER = ("C 4 h segments of nights whose whole-run TOA is in %s. The same data again:\n"
                  "C a within-night check, never to be fitted together with that file.\n")


def _read_tim(path):
    lines = {}
    if os.path.exists(path):
        for ln in open(path):
            ln = ln.strip()
            if ln and not ln.startswith(("FORMAT", "C ", "#", "MODE")):
                lines[ln.split()[0]] = ln
    return lines


def _write_lines(path, lines, header=""):
    body = sorted(lines.values(), key=lambda ln: float(ln.split()[2]))
    with open(path, "w") as fh:
        fh.write("FORMAT 1\n" + header)
        fh.write("".join(ln + "\n" for ln in body))


def write_tim(toas, tim_file=TIM_FILE):
    """Add or replace these TOAs (by name), sorted by time: whole-run TOAs in
    `tim_file`, segments (`<recording>_sN`) in its segment_file(). Rerunning
    a recording replaces its lines rather than duplicating them, and a
    segment line found in the main file - one written before the split - is
    moved across."""
    os.makedirs(os.path.dirname(tim_file), exist_ok=True)
    seg_file = segment_file(tim_file)
    lines = {**_read_tim(tim_file), **_read_tim(seg_file)}
    for t in toas:
        lines[t["name"]] = tim_line(t)
    whole = {k: v for k, v in lines.items() if not SEGMENT_NAME.search(k)}
    segs = {k: v for k, v in lines.items() if SEGMENT_NAME.search(k)}
    _write_lines(tim_file, whole)
    if segs or os.path.exists(seg_file):
        _write_lines(seg_file, segs, SEGMENT_HEADER % os.path.basename(tim_file))
    for t in toas:
        with open(os.path.join(os.path.dirname(tim_file), t["name"] + ".json"), "w") as fh:
            json.dump(t, fh, indent=1)
    return tim_file


def write_par(par_file=PAR_FILE, pulsar=None):
    """The par file the timing program starts from: the same ephemeris
    `absolute_phase` folds with."""
    p = pulsar or PF.B0329

    def sexa(v, hours):
        v = v / 15.0 if hours else v
        sign = "-" if v < 0 else ("+" if not hours else "")
        v = abs(v)
        d = int(v); m = int((v - d) * 60); s = (v - d - m / 60.0) * 3600
        return "%s%02d:%02d:%09.6f" % (sign, d, m, s)

    os.makedirs(os.path.dirname(par_file), exist_ok=True)
    with open(par_file, "w") as fh:
        fh.write("PSRJ           J0332+5434\n")
        fh.write("RAJ            %s\n" % sexa(p["ra_deg"], True))
        fh.write("DECJ           %s\n" % sexa(p["dec_deg"], False))
        fh.write("PMRA           %.2f\nPMDEC          %.2f\nPOSEPOCH       %.1f\n"
                 % (p["pmra_mas_yr"], p["pmdec_mas_yr"], p["posepoch_mjd"]))
        fh.write("F0             %.12f 1\nF1             %.6e\nF2             %.2e\n" % (p["f0"], p["f1"], p["f2"]))
        fh.write("PEPOCH         %.1f\nDM             %.4f\n" % (p["pepoch_mjd"], p["dm"]))
        fh.write("UNITS          TDB\nEPHEM          DE421\nCLK            TT(TAI)\n")
        # A TOA timed by the host's NTP clock (-pps 0) carries its few ms of
        # clock error besides the fit's statistical one: added in quadrature.
        fh.write("EQUAD -pps 0 %.1f\n" % HOST_CLOCK_EQUAD_US)
    return par_file


# ---------------------------------------------------------------------------
# every night's fold, added up; the timing fit


def in_observations(path):
    """Only the observatory's own recordings feed the .tim and the stacked
    profile - never a test's temporary file."""
    return os.path.dirname(os.path.realpath(path)) == os.path.realpath(OBS_DIR)


def cache_profile(path, toa, attrs=None):
    """Store a recording's whole-run fine fold - weighted sums at the absolute
    phase - with what it is, so the profile stands on its own when the
    recording is gone: the stack is built from these, not from the files."""
    os.makedirs(PROFILE_CACHE_DIR, exist_ok=True)
    out = os.path.join(PROFILE_CACHE_DIR, os.path.splitext(os.path.basename(path))[0] + ".npz")
    a = attrs or {}
    np.savez(out, S=toa["S"].sum(axis=0), C=toa["C"].sum(axis=0), hours=(toa["t_last"] - toa["t_first"]) / 3600.0,
             phase_offset=PF.B0329["phase_offset"], row_centre=PF.ROW_CENTRE, nbins=TOA_BINS,
             recording=os.path.basename(path), t_first_unix=toa["t_first"], t_last_unix=toa["t_last"],
             freq_hz=toa["freq_hz"], bw_hz=toa["bw_hz"] or 0.0, dt_s=toa["dt_s"],
             time_source=str(a.get("time_source", "host")), obs_name=str(a.get("obs_name", "")),
             session=str(a.get("pulsar_session", "") or ""),
             pps=int(str(a.get("time_source", "host")) == "pps" and int(a.get("time_pps_verified", 0)) == 1),
             time_marks_exact=int(a.get("time_marks_exact", 0)))
    return out


def _store_path(path):
    return os.path.join(PROFILE_CACHE_DIR, os.path.splitext(os.path.basename(path))[0] + ".npz")


def _cache_valid(cache, path):
    """A stored profile is current if it exists, was made after the recording
    last changed, and on today's phase convention and binning."""
    if not os.path.exists(cache) or os.path.getmtime(cache) < os.path.getmtime(path):
        return False
    d = np.load(cache)
    return (float(d["phase_offset"]) == PF.B0329["phase_offset"] and float(d["row_centre"]) == PF.ROW_CENTRE
            and int(d["nbins"]) == TOA_BINS)


def _rotated(x, dphase):
    """A periodic profile moved later by `dphase` of a period, continuously."""
    k = np.arange(len(x) // 2 + 1)
    return np.fft.irfft(np.fft.rfft(x) * np.exp(-2j * np.pi * k * dphase), n=len(x))


def accumulated_profile(fold_missing=True):
    """Every stored profile, summed with its own noise weights (the
    per-second 1/sigma^2 the fold applied, so a noisier 8 MHz night counts
    for less). Recordings on disk whose profile is missing or stale are
    folded first if `fold_missing`; a profile whose recording is gone still
    counts. One made under another phase_offset is rotated to today's."""
    import glob
    for path in sorted(glob.glob(os.path.join(OBS_DIR, "*_pulsar.h5"))):
        if not _cache_valid(_store_path(path), path) and fold_missing:
            try:
                r, attrs = PF.analyse_file(path, toa_bins=TOA_BINS)
            except Exception:                            # noqa: BLE001 - an unreadable file is skipped
                continue
            cache_profile(path, r["toa"], attrs)
    S = np.zeros(TOA_BINS); C = np.zeros(TOA_BINS); hours = 0.0; used = []
    for store in sorted(glob.glob(os.path.join(PROFILE_CACHE_DIR, "*.npz"))):
        d = np.load(store)
        if int(d["nbins"]) != TOA_BINS or float(d["row_centre"]) != PF.ROW_CENTRE:
            continue
        s_, c_ = d["S"], d["C"]
        shift = PF.B0329["phase_offset"] - float(d["phase_offset"])
        if shift:
            s_, c_ = _rotated(s_, shift), _rotated(c_, shift)
        S += s_; C += c_; hours += float(d["hours"]); used.append(os.path.basename(store)[:-4])
    prof = np.where(C > 0, S / np.maximum(C, 1e-300), 0.0)
    return prof, hours, used


def timing_fit(tim_file=TIM_FILE, par_file=PAR_FILE, timeout_s=600):
    """PINT on the whole-run TOAs in `tim_file`, with the segments from its
    segment_file() given residuals under the same model but never fitted:
    F0 free from three nights, F1 free once they span three weeks;
    otherwise residuals only. Returns pint_tools' dict, or {'error': ...}."""
    import subprocess
    import observatory
    if not os.path.exists(PINT_PY):
        return {"error": "no PINT environment"}
    if not os.path.exists(tim_file):
        return {"error": "no TOAs yet"}
    write_par(par_file)
    cmd = [PINT_PY, os.path.join(HERE, "pint_tools.py"), "fit", par_file, tim_file,
           str(observatory.SITE_LAT_DEG), str(observatory.SITE_LON_DEG), str(observatory.SITE_HEIGHT_M),
           segment_file(tim_file)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as exc:                             # noqa: BLE001
        return {"error": "PINT: %s" % exc}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="TOAs for B0329+54 from a pulsar-mode recording")
    ap.add_argument("recording")
    ap.add_argument("--segments", type=int, default=1)
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()
    analysis = PF.analyse_file(a.recording, toa_bins=TOA_BINS)
    if in_observations(a.recording) and not a.no_write:
        cache_profile(a.recording, analysis[0]["toa"], analysis[1])
    toas = timing_toas(a.recording, analysis, segments=a.segments)
    for t in toas:
        print("%-40s MJD %s  %8.1f us  S/N %5.1f  tau %+.5f  chi2r %.2f  pps %d"
              % (t["name"], t["mjd"], t["err_us"], t["snr"], t["tau"], t["chi2_red"], t["pps"]))
    keep = [t for t in toas if t["snr"] >= MIN_TOA_SNR]
    if len(keep) < len(toas):
        print("not written (S/N < %.0f):" % MIN_TOA_SNR, ", ".join(t["name"] for t in toas if t not in keep))
    if not a.no_write and keep:
        print("written:", write_tim(keep), write_par())


if __name__ == "__main__":
    main()
