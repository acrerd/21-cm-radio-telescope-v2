#!/usr/bin/env python3
"""The pilot: the B210's own transmitter as the receiver's gain and passband reference (issue #30).

The front end drifts by percent over tens of minutes, and it drifts with a
*tilt* across the band - the SAW filter's skirt sliding under us with its
temperature (2026-09-16: a solar track rose 4.2% at one end of the band and
1.8% at the other in 25 minutes while GOES was quiet). No sky reference can
follow that on a solar track, so the receiver carries its own: one frame of
tones, generated digitally, sent from TX/RX through fixed pads and the old
SRT calibration dipole at the dish's vertex, and received through the feed,
the SAWbird and everything after it.

Three facts make the recovery cheap and exact.

- The frame is one wide-FFT length long (1024 samples) and repeats, so any
  receive frame holds exactly one period, cyclically shifted. TX and RX run
  from one clock at one rate, so the shift - and every tone's phase - is the
  same in every frame, and the cross-spectrum of the receive FFT against the
  known frame's spectrum accumulates coherently: one complex multiply per
  bin per frame, on the wide FFT the receiver already computes.
- Every tone sits on a bin centre and the comb tones are spaced by whole
  bins, more than the Blackman-Harris window's main lobe, so each pilot bin
  holds one tone and nothing of its neighbours; the recovered magnitude is
  then independent of the unknown shift.
- The noise on each cross-spectrum bin is known analytically from the
  receive power in that bin, so the pilot's presence is a signal-to-noise
  test with no threshold to tune by eye: the TX unplugged reads as "not
  detected", and the receiver then applies nothing and says so.

What is applied on the fly, per record, is two numbers: the pilot's level
relative to a reference, and its slope across the band (fractional change
per MHz), both smoothed over a few records. The kelvin spectra are divided by
`factor(level, slope, f)`, and both numbers are stored per record so the
division is exactly reversible (observation_plot.read_observation). The full
per-bin response is accumulated and written at a slower cadence
(`shape_interval_s`) for the passband-ripple work, which needs minutes of
averaging anyway.

The reference is the calibration's `pilot_reference` when the gain job has
stored one at its fit (then the level carries the gain change since the
calibration), else the run's first detected record (then it removes drift
within the run only, and the file says `pilot_anchored = 0`).

This module is deliberately free of GNU Radio: the receiver builds the
transmit frame and the reference from it, and the scheduler reduces files
with it, and neither should need the other's dependencies to do so.
"""
import json
import math
from collections import deque

import numpy as np

H1_REST_FREQ_HZ = 1420.405752e6

# Defaults. Levels are digital: `amplitude` is the frame's peak in units of
# DAC full scale; what reaches the feed is set by the TX gain and the pads,
# which the bench run decides (#30). Nothing here radiates a level: with the
# TX unconnected these settings produce a file identical to one recorded
# with the pilot off, apart from the per-record "not detected" flags.
PILOT_DEFAULTS = {
    "enabled": True,
    # "tones": the three tones only; "continuum": tones plus a comb over
    # every bin outside the H I band; "full": comb over the whole band,
    # H I included (continuum-only science); "off": no TX, no recovery.
    "mode": "continuum",
    "tones_hz": [1415.3e6, 1418.9e6, 1422.6e6],
    # The tone that will eventually be the strong one for the fast wobble
    # (#30); until the bench sets levels it is as weak as the others.
    "strong_tone_hz": 1418.9e6,
    "strong_tone_relative_db": 0.0,       # relative to a comb tone
    "comb_spacing_bins": 8,               # wide bins between comb tones
    "tx_gain_db": 0.0,                    # the minimum
    "amplitude": 0.05,                    # frame peak, DAC full scale
    "shape_interval_s": 60.0,
    "detect_snr": 4.0,                    # median SNR over pilot bins
    "level_smooth_records": 3,
    "slope_smooth_records": 10,
    "guard_bins": 1,                      # excluded either side of a comb tone
    "strong_guard_bins": 4,               # either side of the strong tone
    "dc_guard_bins": 4,                   # no comb tone this close to the LO
    "h1_guard_hz": 100e3,                 # comb keeps this far from the H I band
    # Tests: add the frame digitally into the demo source so the recovery
    # can be exercised with no radio.
    "demo_inject": False,
    "demo_inject_scale": 0.5,
}

_CONFIG_KEYS = ("enabled", "mode", "tones_hz", "strong_tone_hz", "strong_tone_relative_db",
                "comb_spacing_bins", "tx_gain_db", "amplitude", "shape_interval_s",
                "detect_snr", "level_smooth_records", "slope_smooth_records",
                "guard_bins", "strong_guard_bins", "dc_guard_bins", "h1_guard_hz",
                "demo_inject", "demo_inject_scale")


def config_from(overrides=None):
    """The pilot configuration: defaults, then `pilot_*` / `receiver_pilot_*`
    overrides (the scheduler's config keys), then a nested `pilot` dict."""
    cfg = json.loads(json.dumps(PILOT_DEFAULTS))
    overrides = overrides or {}
    nested = overrides.get("pilot") if isinstance(overrides.get("pilot"), dict) else {}
    for key in _CONFIG_KEYS:
        for name in ("receiver_pilot_" + key, "pilot_" + key):
            if overrides.get(name) not in (None, ""):
                cfg[key] = overrides[name]
        if nested.get(key) not in (None, ""):
            cfg[key] = nested[key]
    cfg["enabled"] = _truthy(cfg["enabled"]) and str(cfg["mode"]).lower() != "off"
    cfg["mode"] = str(cfg["mode"]).lower()
    if cfg["mode"] not in ("tones", "continuum", "full", "off"):
        raise ValueError("pilot mode must be tones, continuum, full or off, not %r" % cfg["mode"])
    cfg["tones_hz"] = [float(x) for x in cfg["tones_hz"]]
    for k in ("strong_tone_hz", "strong_tone_relative_db", "tx_gain_db", "amplitude",
              "shape_interval_s", "detect_snr", "h1_guard_hz", "demo_inject_scale"):
        cfg[k] = float(cfg[k])
    for k in ("comb_spacing_bins", "level_smooth_records", "slope_smooth_records",
              "guard_bins", "strong_guard_bins", "dc_guard_bins"):
        cfg[k] = int(cfg[k])
    cfg["demo_inject"] = _truthy(cfg["demo_inject"])
    if not (0.0 < cfg["amplitude"] <= 1.0):
        raise ValueError("pilot amplitude must be in (0, 1] of full scale")
    if cfg["comb_spacing_bins"] < 4:
        raise ValueError("comb tones closer than 4 bins leak into each other "
                         "through the Blackman-Harris window")
    return cfg


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def blackman_harris(n):
    """The 4-term Blackman-Harris window as GNU Radio's fft.window.blackmanharris
    computes it (symmetric, N-1 in the denominator). The reference spectrum
    has to use the window the receive FFT uses, or the tones' recovered
    amplitudes are wrong by the window's gain."""
    m = np.arange(n)
    d = max(n - 1, 1)
    return (0.35875 - 0.48829 * np.cos(2 * np.pi * m / d)
            + 0.14128 * np.cos(4 * np.pi * m / d)
            - 0.01168 * np.cos(6 * np.pi * m / d))


def wide_axis(lo_hz, sample_rate_hz, nbins):
    """The wide product's frequency axis: fftshifted, bin j at lo + (j - N/2) fs/N."""
    return float(lo_hz) + np.fft.fftshift(np.fft.fftfreq(int(nbins), 1.0 / float(sample_rate_hz)))


def plan(cfg, lo_hz, sample_rate_hz, nbins, h1_band_hz):
    """Where the pilot goes and what the receiver correlates against.

    Returns a dict: `bins` (wide bin indices carrying a tone), `kind` per bin
    ("tone", "strong", "comb"), `frame` (complex64, one period, peak at
    cfg amplitude), `reference` (complex, the fftshifted FFT of the windowed
    frame, i.e. what a receive frame of the pilot alone would give), `excluded`
    (bool mask over the wide bins that band means must leave out when the
    pilot is present), `freq_hz` (the wide axis), `centre_hz` (where the
    slope is anchored).
    """
    nbins = int(nbins)
    fs = float(sample_rate_hz)
    lo = float(lo_hz)
    freq = wide_axis(lo, fs, nbins)
    half = nbins // 2

    def bin_of(f_hz):
        return int(round((float(f_hz) - lo) * nbins / fs)) + half

    amps = np.zeros(nbins, dtype=float)
    kind = {}
    if cfg["enabled"] and cfg["mode"] != "off":
        strong = bin_of(cfg["strong_tone_hz"])
        for f in cfg["tones_hz"]:
            j = bin_of(f)
            if 0 <= j < nbins:
                amps[j] = 1.0
                kind[j] = "tone"
        if 0 <= strong < nbins:
            amps[strong] = 10 ** (cfg["strong_tone_relative_db"] / 20.0)
            kind[strong] = "strong"
        if cfg["mode"] in ("continuum", "full"):
            h1_lo, h1_hi = float(h1_band_hz[0]), float(h1_band_hz[1])
            g = cfg["h1_guard_hz"]
            step = cfg["comb_spacing_bins"]
            for j in range(step, nbins - step, step):
                if abs(j - half) <= cfg["dc_guard_bins"]:
                    continue
                if any(abs(j - t) <= cfg["strong_guard_bins"] + 1 for t in kind):
                    continue
                if cfg["mode"] == "continuum" and (h1_lo - g) <= freq[j] <= (h1_hi + g):
                    continue
                amps[j] = 1.0
                kind[j] = "comb"
    bins = np.array(sorted(kind), dtype=int)

    # Schroeder phases over the comb keep the frame's crest factor low; the
    # tones take phase zero. The frame is the inverse FFT of the (unshifted)
    # spectrum, scaled to the configured peak.
    spec = np.zeros(nbins, dtype=complex)
    for i, j in enumerate(bins):
        phase = math.pi * i * i / max(len(bins), 1) if kind[j] == "comb" else 0.0
        spec[(j - half) % nbins] = amps[j] * np.exp(1j * phase)
    frame = np.fft.ifft(spec) * nbins
    peak = np.abs(frame).max()
    if peak > 0:
        frame = frame * (cfg["amplitude"] / peak)
    frame = frame.astype(np.complex64)

    # What the receive FFT makes of one frame of pilot alone: window, FFT,
    # shift - exactly the receiver's wide branch.
    reference = np.fft.fftshift(np.fft.fft(blackman_harris(nbins) * frame.astype(complex)))

    excluded = np.zeros(nbins, dtype=bool)
    for j in bins:
        w = cfg["strong_guard_bins"] if kind[j] == "strong" else cfg["guard_bins"]
        excluded[max(0, j - w):min(nbins, j + w + 1)] = True

    return {"bins": bins, "kind": [kind[j] for j in bins], "frame": frame,
            "reference": reference, "excluded": excluded, "freq_hz": freq,
            "centre_hz": lo, "nbins": nbins}


def estimate(xspec, n_frames, planned, wide_power_mean, reference_h=None):
    """One record's pilot estimate from the accumulated cross-spectrum.

    `xspec` is sum over frames of F_j * conj(R_j) for every wide bin, `n_frames`
    how many frames went into it, `wide_power_mean` the wide product's mean
    per-bin power over the same frames (the receiver's normalised counts,
    |F|^2 / N). `reference_h` is the per-pilot-bin response the level is
    measured against, or None for an absolute reading.

    Returns a dict with `h` (complex response per pilot bin), `snr` (per bin),
    `detected`, `level`, `slope` (fraction per MHz about the plan's centre),
    and `level_err`. With nothing detected, level 1 and slope 0.
    """
    bins = planned["bins"]
    nb = planned["nbins"]
    out = {"detected": False, "level": 1.0, "slope": 0.0, "level_err": float("nan"),
           "snr_median": 0.0, "h": np.zeros(len(bins), dtype=complex),
           "snr": np.zeros(len(bins)), "n_frames": int(n_frames)}
    if len(bins) == 0 or n_frames <= 0 or xspec is None:
        return out
    x = np.asarray(xspec, dtype=complex)[bins]
    r = np.asarray(planned["reference"], dtype=complex)[bins]
    rp = np.abs(r) ** 2
    good = rp > 0
    # Recovered response: X = N * H * |R|^2 when the pilot is there.
    h = np.zeros(len(bins), dtype=complex)
    h[good] = x[good] / (n_frames * rp[good])
    # Noise on X: the receive noise in the bin times the reference, summed
    # over N frames -> sigma^2 = N * P_raw * |R|^2, with P_raw = P_mean * N_bins
    # undoing the receiver's 1/N normalisation.
    p_raw = np.asarray(wide_power_mean, dtype=float)[bins] * nb
    sigma = np.sqrt(np.maximum(n_frames * p_raw * rp, 1e-300))
    snr = np.abs(x) / sigma
    out["h"], out["snr"] = h, snr
    out["snr_median"] = float(np.median(snr[good])) if good.any() else 0.0
    return out


def relative(est, planned, reference_h, cfg):
    """Fill in level and slope from a detected estimate against `reference_h`."""
    est = dict(est)
    if est["snr_median"] < cfg["detect_snr"] or reference_h is None:
        return est
    bins = planned["bins"]
    ref = np.asarray(reference_h, dtype=complex)
    ok = (np.abs(ref) > 0) & (est["snr"] > 1.0)
    if ok.sum() < 1:
        return est
    g = np.abs(est["h"][ok]) / np.abs(ref[ok])
    w = est["snr"][ok] ** 2
    x = (planned["freq_hz"][bins][ok] - planned["centre_hz"]) / 1e6
    if ok.sum() >= 3 and np.ptp(x) > 0.5:
        # Weighted straight line g = a + b x; level is the line at the centre.
        W = np.sqrt(w)
        A = np.vstack([W, W * x]).T
        coef, *_ = np.linalg.lstsq(A, W * g, rcond=None)
        a, b = float(coef[0]), float(coef[1])
        slope = b / a if a > 0 else 0.0
        level = a
    else:
        level = float(np.sum(w * g) / np.sum(w))
        slope = 0.0
    if not (0.25 < level < 4.0):          # something is badly wrong; do not apply it
        return est
    est.update(detected=True, level=level, slope=float(np.clip(slope, -0.2, 0.2)),
               level_err=float(1.0 / math.sqrt(np.sum(w))) if np.sum(w) > 0 else float("nan"))
    return est


def factor(level, slope, freq_hz, centre_hz):
    """The per-channel factor the counts are divided by: level x (1 + slope (f - fc)/MHz),
    clamped so a bad estimate cannot do more than double or halve anything."""
    f = np.asarray(freq_hz, dtype=float)
    fac = float(level) * (1.0 + float(slope) * (f - float(centre_hz)) / 1e6)
    return np.clip(fac, 0.5, 2.0)


class PilotTracker:
    """Per-record bookkeeping in the receiver: reference, smoothing, shape.

    `update(xspec, n_frames, wide_mean, now)` returns the record's pilot dict
    with the *applied* (smoothed) level and slope, and `shape_ready(now)`
    hands over the accumulated per-bin response when the shape interval is up.
    """

    def __init__(self, cfg, planned, reference_h=None):
        self.cfg = cfg
        self.planned = planned
        self.reference_h = None if reference_h is None else np.asarray(reference_h, dtype=complex)
        self.anchored = reference_h is not None
        self._levels = deque(maxlen=max(1, cfg["level_smooth_records"]))
        self._slopes = deque(maxlen=max(1, cfg["slope_smooth_records"]))
        self._shape_sum = np.zeros(len(planned["bins"]), dtype=complex)
        self._shape_n = 0
        self._shape_t0 = None
        self.detected_records = 0
        self.records = 0

    def update(self, xspec, n_frames, wide_mean, now):
        self.records += 1
        est = estimate(xspec, n_frames, self.planned, wide_mean)
        seen = est["snr_median"] >= self.cfg["detect_snr"]
        if seen and self.reference_h is None:
            # First sight of the pilot with nothing to anchor to: this run
            # becomes its own reference, and the file says so.
            self.reference_h = est["h"].copy()
        est = relative(est, self.planned, self.reference_h, self.cfg)
        if est["detected"]:
            self.detected_records += 1
            self._levels.append(est["level"])
            self._slopes.append(est["slope"])
            if self._shape_t0 is None:
                self._shape_t0 = now
            self._shape_sum += est["h"]
            self._shape_n += 1
            level = float(np.mean(self._levels))
            slope = float(np.mean(self._slopes))
        else:
            # Not there this record: apply nothing, and forget the smoothing
            # so a return does not drag stale values in.
            self._levels.clear()
            self._slopes.clear()
            level, slope = 1.0, 0.0
        return {"ok": bool(est["detected"]), "level": level, "slope": slope,
                "snr": float(est["snr_median"]), "raw_level": float(est["level"]),
                "raw_slope": float(est["slope"]), "level_err": est["level_err"],
                "h": est["h"]}

    def shape_ready(self, now):
        """(t_mid, mean response per pilot bin) once the interval is up, else None."""
        if self._shape_n == 0 or self._shape_t0 is None:
            return None
        if now - self._shape_t0 < self.cfg["shape_interval_s"]:
            return None
        mean = self._shape_sum / self._shape_n
        t_mid = 0.5 * (self._shape_t0 + now)
        self._shape_sum[:] = 0
        self._shape_n = 0
        self._shape_t0 = None
        return t_mid, mean


# ---------------------------------------------------------------------------
# Reduction side: what a file says about its pilot.

def config_of(header):
    """The pilot config a recording carries, or None for a file from before."""
    raw = header.get("pilot") if header else None
    if raw is None:
        return None
    try:
        return config_from({"pilot": json.loads(raw) if isinstance(raw, str) else dict(raw)})
    except (ValueError, TypeError):
        return None


def excluded_channels(header, freq_hz, only_if_detected=True):
    """Which channels of `freq_hz` band means should leave out because a pilot tone sits there.

    Derived from the pilot configuration in the header and the file's own
    geometry, for any axis (wide or H I). With `only_if_detected` (the
    default) a file in which the pilot was never seen - the TX unplugged -
    excludes nothing, so such a file reduces exactly as one recorded with
    the pilot off. `header["pilot_detected_records"]` is what
    observation_plot.read_observation fills in.
    """
    freq_hz = np.asarray(freq_hz, dtype=float)
    none = np.zeros(freq_hz.shape, dtype=bool)
    cfg = config_of(header)
    if cfg is None or not cfg["enabled"]:
        return none
    if only_if_detected and not int(header.get("pilot_detected_records", 0) or 0):
        return none
    try:
        inst = header.get("instrument")
        inst = json.loads(inst) if isinstance(inst, str) else dict(inst or {})
        lo = float(header.get("center_freq_hz", inst.get("lo_hz")))
        fs = float(header.get("sample_rate_hz", inst.get("sample_rate_hz")))
        nb = int(inst.get("wide_channels", 1024))
        h1 = header.get("h1_band_hz", inst.get("h1_band_hz"))
    except (TypeError, ValueError, KeyError):
        return none
    p = plan(cfg, lo, fs, nb, h1)
    # A pilot tone at wide bin j occupies the wide bins the plan excludes;
    # on any other axis, exclude the channels within those bins' span.
    width = fs / nb
    mask = none.copy()
    for j in np.flatnonzero(p["excluded"]):
        f0 = p["freq_hz"][j]
        mask |= np.abs(freq_hz - f0) <= 0.5 * width + 1e-3
    return mask


def describe(cfg):
    if not cfg or not cfg.get("enabled"):
        return "pilot off"
    return ("pilot %s: tones %s MHz, comb every %d bins, TX gain %.0f dB, amplitude %.2f"
            % (cfg["mode"], "/".join("%.1f" % (f / 1e6) for f in cfg["tones_hz"]),
               cfg["comb_spacing_bins"], cfg["tx_gain_db"], cfg["amplitude"]))
