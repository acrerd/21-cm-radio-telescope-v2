#!/usr/bin/env python3
"""The pilot: the B210's own transmitter as the receiver's gain and passband reference (issue #30).

The front end drifts by percent over tens of minutes, and it drifts with a
*tilt* across the band - the SAW filter's skirt sliding under us with its
temperature (2026-09-16: a solar track rose 4.2% at one end of the band and
1.8% at the other in 25 minutes while GOES was quiet) - and its multi-transit
ripple, 0.1-0.15% of the passband with a 160 kHz period, changes from day to
day, which is why a stored bandpass template leaves 0.5 K of structure in
every line fit. No sky reference can follow either on a solar track, so the
receiver carries its own: a comb with a tone on every bin of the band,
generated digitally, sent from TX/RX through fixed pads and the old SRT
calibration dipole at the dish's vertex, and received through the feed, the
SAWbird and everything after it.

**It is sent in bursts.** Every N-th record (default one in twenty) is a
burst: the comb is on for that record, at a level near the system noise, and
the record is flagged and dropped from the science. Every other record
carries no pilot at all - nothing to exclude, subtract or explain. The
operator's choice (2026-09-16): a continuous weak pilot added only cadence,
and the drifts are minutes long. What the burst measures is the whole
passband at once, so the H I band's own SAW ripple is measured rather than
modelled - the thing a stored template can never follow.

Three facts make the recovery cheap and exact.

- The frame is one FFT length long (1024 samples) and repeats, so any receive
  frame holds exactly one period, cyclically shifted, and **under a
  rectangular window a periodic signal has no leakage at all**: the reference
  spectrum is flat across the band, every bin measures its own response, and
  the cross-spectrum accumulates coherently. The pilot therefore has its own
  unwindowed FFT (`_PilotGate` in the receiver) rather than borrowing the
  wide product's Blackman-Harris one - that window multiplies the frame in
  time, and since a Schroeder chirp sweeps frequency linearly with time, it
  drives the reference to zero at both band edges, which is exactly where the
  filter's tilt has to be measured.
- The framing offset between transmit and receive is unknown but *fixed* (one
  clock, one rate), so it appears as a phase ramp `exp(-2 pi i j d / N)` and
  nothing else. It is estimated per burst from the peak of the inverse
  transform of the cross-spectrum and removed, leaving a proper complex
  response whose phase is the chain's own - which is what a later look at the
  SAW's echo structure would need. Everything used for calibration is a
  magnitude, so a failure to find `d` costs nothing.
- The pilot's presence is decided *per frame* from the frame's total power,
  which a burst at the system-noise level roughly doubles against a per-frame
  scatter of 3%. So the frames a burst actually occupied are counted rather
  than assumed: the transmit buffers' latency at switch-on and switch-off
  costs nothing, and a science record that caught the tail of a burst is
  known and dropped. A power gate rather than a coherent one because the
  phase ramp above would cancel a whole-band coherent statistic.

What comes out of a burst: the complex response per bin, `h`; its **level**
relative to a reference and its **slope** across the band (fraction per MHz);
and, averaged over the bursts of the last `shape_window_s`, a normalised
passband **correction vector** on the H I and wide axes - the SAW ripple and
tilt as they are now. The science records up to the next burst are divided by
`factor(level, slope, f)` and by the correction vector on the way to kelvin;
the numbers and vectors applied are stored, so the division is exactly
reversible (observation_plot.read_observation), which also drops the burst
records for every consumer.

The reference is the calibration's `pilot_reference` when the gain job has
stored one at its fit, else the run's first detected burst (then the file
says `pilot_anchored = 0` and the correction removes drift within the run).
With the transmitter unconnected - the state until the dipole is wired -
every burst reads "not detected", nothing is applied, and the file reduces
identically to one made with the pilot off, less the burst records.

This module is deliberately free of GNU Radio: the receiver builds the frame
and the reference from it, and the scheduler reduces files with it, and
neither should need the other's dependencies to do so.
"""
import json
import math
from collections import deque

import numpy as np

H1_REST_FREQ_HZ = 1420.405752e6

# Defaults. `burst_amplitude` is the frame's peak in DAC full scale; what
# reaches the feed is set by the TX gain and the pads, which the bench run
# decides (#30). Nothing here radiates a level by itself.
PILOT_DEFAULTS = {
    "enabled": True,
    # How often a burst is sent. Expressed in TIME, because a record is not a
    # fixed length: "every 20th record" is a burst a minute at 3 s records and
    # one every twenty minutes at 60 s. A burst costs exactly one record
    # whatever its length, so the interval and the duty cycle cannot both be
    # held at long integrations - the duty cap wins, and an observation that
    # needs a tighter calibration cadence should use shorter records (the
    # plots bin them back down anyway) rather than pay a tenth of its samples.
    # `burst_every_records` overrides the derived number when set; 0 disables.
    "burst_interval_s": 60.0,
    "max_duty_cycle": 0.05,
    "burst_every_records": None,
    "burst_amplitude": 0.5,               # comb's frame peak during a burst, DAC full scale
    # The continuous carrier (issue #30's original single tone), for the fast
    # common-mode gain wobble the bursts cannot reach: they measure the gain
    # at one instant a minute and the wobble is white on 60 s scales. It runs
    # in every record, burst ones included, and is measured incoherently - at
    # ~1000x the noise in its own bin it needs no recovery branch, only the
    # channels of the wide product it already lands in. Placed in the 800 kHz
    # between the low edge of the sampled band and the continuum band, where
    # nothing is measured, so no exclusion machinery comes back; the analogue
    # bandwidth is twice the sample rate, so there is no roll-off there. It
    # does sit in the SAW skirt - which is why it carries only the fast part,
    # the bursts tracking the skirt's own slow motion.
    "tone_enabled": True,
    "tone_hz": 1415.3e6,
    "tone_amplitude": 0.25,               # carrier frame peak; what matters is
                                          # its ratio to a comb tone (~250 in
                                          # power), the pads setting the absolute
    "tone_guard_bins": 6,                 # comb tones left out around the carrier
    "tone_sum_bins": 2,                   # bins either side summed for its power
    "tone_detect_ratio": 10.0,            # excess over the local noise to believe it
    # Whether the carrier's level is *applied* on the fly. Off until the bench
    # correlation test (issue #30, runs 1-5) says the wobble is downstream of
    # the vertex dipole: a carrier corrects a wobble in the SAWbird or the
    # B210, does nothing for an atmospheric one, and would substitute the
    # transmit chain's own wobble for the receive chain's. The series is
    # recorded either way, so the decision can be made on real data and
    # applied at reduction.
    "tone_apply": False,
    "tx_gain_db": 0.0,                    # the minimum
    "dc_guard_bins": 4,                   # no tone this close to the LO
    # How far above the baseline a block has to sit to count as holding the
    # burst. The gate only judges inside an armed window (see _PilotGate), so
    # this has to beat the per-block scatter - 0.25% over 156 frames of 1024
    # bins - and nothing else. It must stay well *below* the smallest burst
    # step, which is not the quiet-sky one: on the Sun the same comb raises
    # the total power by only 30%, which a 30% margin would have missed
    # exactly. 10% is 40 sigma on the scatter and a third of that step.
    "gate_margin": 0.1,
    # A pilot that is never detected costs one record an interval for
    # nothing. After this many undetected bursts with none ever seen, the
    # run stops sending them and says so; the carrier keeps running.
    "give_up_after_bursts": 5,
    "detect_snr": 8.0,                    # a burst counts as seen at this median per-bin SNR
    # Bursts averaged for the passband correction. The budget: a burst whose
    # power per bin is `ratio` times the noise gives SNR sqrt(n_frames x ratio)
    # per bin on the amplitude, so 2/that on the power - at 8 Msps, a 3 s
    # burst and ratio 5, 0.29% per bin. The SAW ripple is 0.1-0.15% of the
    # passband, so a single burst cannot see it: what makes it measurable is
    # averaging bursts (the ripple is stable to r=0.8 over 1.7 h) and the
    # delay filter below. 30 minutes of one burst a minute, with the filter,
    # reaches ~0.04% - the thermal floor of a gain fit. Note the correction
    # divides every record alike, so its own noise is a *systematic*: too
    # short a window trades the ripple for something no better.
    "shape_window_s": 1800.0,
    # The passband's structure is a few SAW multi-transit echoes at 1.5-6.2 us
    # (measured 2026-09-14), so the response is sparse in delay: keeping only
    # |delay| below this discards most of the per-bin noise and none of the
    # ripple. 1024 bins over 8 MHz resolve delay to 0.125 us out to 64 us.
    "max_delay_us": 10.0,
    # The shape correction divides every record alike, so applying one built
    # from too little pilot trades the ripple for noise of its own: measured,
    # a single 3 s burst at 5x the noise leaves 0.18% where the ripple was
    # 0.11% - worse than not correcting. The threshold is in **accumulated
    # pilot seconds**, not bursts, because a burst is one record and records
    # are not a fixed length: eight 3 s bursts and a third of one 60 s burst
    # carry the same information, and a burst count would have refused every
    # correction on a 60 s drift scan while accepting a worse one at 3 s.
    # 24 s is where it starts paying (0.05% per channel); 76 s reaches the
    # 0.04% that puts it under a gain fit's thermal floor. Below the
    # threshold the level and tilt are applied alone; they are good to 0.02%
    # from a single burst.
    "min_shape_pilot_s": 24.0,
    "hold_bursts": 3,                     # apply a burst's level this many intervals, then stop
    "burst_off_margin_s": 0.3,            # switch the comb off this long before the record ends
    # Tests: add the frame digitally into the demo source so the recovery
    # can be exercised with no radio.
    "demo_inject": False,
    "demo_inject_scale": 1.0,
}

_CONFIG_KEYS = tuple(PILOT_DEFAULTS)


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
    cfg["enabled"] = _truthy(cfg["enabled"])
    for k in ("burst_amplitude", "tx_gain_db", "gate_margin", "detect_snr",
              "shape_window_s", "max_delay_us", "burst_off_margin_s", "demo_inject_scale",
              "tone_hz", "tone_amplitude", "tone_detect_ratio", "burst_interval_s",
              "max_duty_cycle", "min_shape_pilot_s"):
        cfg[k] = float(cfg[k])
    for k in ("dc_guard_bins", "hold_bursts", "tone_guard_bins", "tone_sum_bins",
              "give_up_after_bursts"):
        cfg[k] = int(cfg[k])
    if cfg["burst_every_records"] not in (None, ""):
        cfg["burst_every_records"] = int(cfg["burst_every_records"])
    else:
        cfg["burst_every_records"] = None
    cfg["tone_enabled"] = _truthy(cfg["tone_enabled"])
    cfg["tone_apply"] = _truthy(cfg["tone_apply"])
    if not (0.0 < cfg["tone_amplitude"] < 1.0):
        raise ValueError("pilot tone amplitude must be in (0, 1) of full scale")
    carrier_amp = cfg["tone_amplitude"] if cfg["tone_enabled"] else 0.0
    if cfg["burst_amplitude"] + carrier_amp > 0.95:
        raise ValueError("burst and tone amplitudes together would run the DAC into its rails")
    cfg["demo_inject"] = _truthy(cfg["demo_inject"])
    if not (0.0 < cfg["burst_amplitude"] <= 1.0):
        raise ValueError("pilot burst amplitude must be in (0, 1] of full scale")
    if cfg["burst_every_records"] is not None:
        if cfg["burst_every_records"] < 0:
            raise ValueError("burst_every_records cannot be negative")
        if cfg["burst_every_records"] == 1:
            raise ValueError("every record a burst would leave no science")
        if cfg["burst_every_records"] == 0:
            cfg["enabled"] = False
    if not (0.0 < cfg["max_duty_cycle"] <= 0.5):
        raise ValueError("pilot max duty cycle must be in (0, 0.5]")
    if cfg["burst_interval_s"] <= 0:
        raise ValueError("pilot burst interval must be positive")
    return cfg


def burst_off_margin_s(cfg, integration_time_s):
    """How long before a burst record ends to switch the comb off.

    Capped at a quarter of the record: at 0.1 s records a flat 0.3 s margin
    would switch the comb off before it was ever on, and the record would be
    dropped for a burst that never happened.
    """
    return min(float(cfg["burst_off_margin_s"]), 0.25 * float(integration_time_s or 0.0))


def burst_every(cfg, integration_time_s):
    """How many records apart the bursts go, for records of this length.

    The target interval in records, floored by the duty cycle - a burst costs
    one whole record whatever its length, so at 60 s records a 60 s interval
    would be every record. At 3 s records and the defaults this is 20, a burst
    a minute at 5%; at 0.5 s records it is 120, still a burst a minute but at
    0.8%, where a fixed record count would have sent one every ten seconds
    for nothing.
    """
    if not cfg.get("enabled"):
        return 0
    if cfg.get("burst_every_records"):
        return int(cfg["burst_every_records"])
    tau = float(integration_time_s or 0.0)
    if tau <= 0:
        return 0
    wanted = int(round(cfg["burst_interval_s"] / tau))
    floor = int(math.ceil(1.0 / cfg["max_duty_cycle"]))
    return max(2, wanted, floor)


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def wide_axis(lo_hz, sample_rate_hz, nbins):
    """The wide product's frequency axis: fftshifted, bin j at lo + (j - N/2) fs/N."""
    return float(lo_hz) + np.fft.fftshift(np.fft.fftfreq(int(nbins), 1.0 / float(sample_rate_hz)))


def plan(cfg, lo_hz, sample_rate_hz, nbins):
    """The frames the transmitter sends and what the receiver correlates against.

    Two frames, both periodic at the FFT length so neither leaks:

    - `idle_frame`, sent in every record - the continuous carrier alone, or
      zeros with the carrier disabled.
    - `frame`, sent during a burst - the comb on every bin but the LO guard
      and the carrier's own neighbourhood, *plus* the same carrier at the
      same level. The carrier is never interrupted, which is what a fast-gain
      monitor needs, and its power therefore reads cleanly out of every
      record including the bursts - which is what makes it the right
      reference for the records between them.

    Also `bins` (the comb's), `reference` (the fftshifted unwindowed FFT of
    the comb part alone - flat in magnitude, which is why the recovery has
    its own rectangular FFT), `tone_bin`, `freq_hz`, `centre_hz`, `nbins`.
    """
    nbins = int(nbins)
    fs = float(sample_rate_hz)
    lo = float(lo_hz)
    freq = wide_axis(lo, fs, nbins)
    half = nbins // 2

    tone_bin = None
    if cfg["enabled"] and cfg["tone_enabled"]:
        j = int(round((float(cfg["tone_hz"]) - lo) * nbins / fs)) + half
        if 0 <= j < nbins and abs(j - half) > cfg["dc_guard_bins"] + cfg["tone_sum_bins"]:
            tone_bin = j

    bins = []
    if cfg["enabled"]:
        for j in range(nbins):
            if abs(j - half) <= cfg["dc_guard_bins"]:
                continue
            if tone_bin is not None and abs(j - tone_bin) <= cfg["tone_guard_bins"]:
                continue
            bins.append(j)
    bins = np.array(bins, dtype=int)

    # Schroeder phases: a swept chirp, crest factor ~sqrt(2), so the burst
    # carries its power without running the DAC into its rails.
    spec = np.zeros(nbins, dtype=complex)
    for i, j in enumerate(bins):
        spec[(j - half) % nbins] = np.exp(1j * math.pi * i * i / max(len(bins), 1))
    comb = np.fft.ifft(spec) * nbins
    peak = np.abs(comb).max()
    if peak > 0:
        comb = comb * (cfg["burst_amplitude"] / peak)

    carrier = np.zeros(nbins, dtype=complex)
    if tone_bin is not None:
        cspec = np.zeros(nbins, dtype=complex)
        cspec[(tone_bin - half) % nbins] = 1.0
        carrier = np.fft.ifft(cspec) * nbins
        carrier = carrier * (cfg["tone_amplitude"] / np.abs(carrier).max())

    reference = np.fft.fftshift(np.fft.fft(comb))
    return {"bins": bins, "frame": (comb + carrier).astype(np.complex64),
            "comb_frame": comb.astype(np.complex64),
            "idle_frame": carrier.astype(np.complex64),
            "zeros": np.zeros(nbins, dtype=np.complex64),
            "reference": reference, "freq_hz": freq, "centre_hz": lo, "nbins": nbins,
            "tone_bin": tone_bin,
            "tone_hz": None if tone_bin is None else float(freq[tone_bin])}


def tone_power(wide_mean, planned, cfg):
    """The continuous carrier's power in this record, from the wide product.

    At ~1000x the noise in its own bin the carrier needs no coherent
    recovery: it is simply the excess over the local noise in the channels it
    occupies, which the wide product already records. The window spreads it
    over its main lobe, so `tone_sum_bins` either side are summed, and the
    noise is the median of the bins just outside that.

    Returns {'power', 'noise', 'ratio', 'detected'}; power is the excess in
    the receiver's normalised counts.
    """
    out = {"power": 0.0, "noise": 0.0, "ratio": 0.0, "detected": False}
    j = planned.get("tone_bin")
    if j is None or wide_mean is None:
        return out
    w = np.asarray(wide_mean, dtype=float)
    n = len(w)
    k = int(cfg["tone_sum_bins"])
    lo_i, hi_i = max(0, j - k), min(n, j + k + 1)
    near = np.concatenate([w[max(0, j - 20):max(0, j - k - 2)], w[min(n, j + k + 3):min(n, j + 21)]])
    if near.size < 4:
        return out
    noise = float(np.median(near))
    excess = float(w[lo_i:hi_i].sum() - noise * (hi_i - lo_i))
    out.update(power=excess, noise=noise,
               ratio=(excess / noise if noise > 0 else 0.0))
    out["detected"] = bool(out["ratio"] > cfg["tone_detect_ratio"])
    return out


def estimate(xspec, n_frames, planned, noise_per_bin):
    """One burst's response from the accumulated cross-spectrum.

    `xspec` is the sum over the pilot-on frames of F_j conj(R_j) for every
    bin of the receiver's unwindowed pilot FFT, `n_frames` how many frames it
    covers, `noise_per_bin` the mean power per bin of that same FFT measured
    on the frames the pilot was *off* - the noise the burst sat in.

    The fixed framing offset between transmit and receive appears as a phase
    ramp across the band; it is found from the peak of the inverse transform
    and removed, so `h` is the chain's own complex response. Magnitudes are
    unaffected either way.

    Returns `h` (per pilot bin), `snr` (per bin), `snr_median`, `n_frames`,
    `delay_samples`.
    """
    bins = planned["bins"]
    out = {"h": np.zeros(len(bins), dtype=complex), "snr": np.zeros(len(bins)),
           "snr_median": 0.0, "n_frames": int(n_frames), "delay_samples": 0}
    if len(bins) == 0 or n_frames <= 0 or xspec is None:
        return out
    x = np.asarray(xspec, dtype=complex)
    r = np.asarray(planned["reference"], dtype=complex)
    noise = np.asarray(noise_per_bin, dtype=float)
    if noise.ndim == 0:
        noise = np.full(planned["nbins"], float(noise))
    # The framing offset: X carries exp(-2 pi i j d / N) across the band, so
    # its inverse transform peaks at d.
    try:
        d = int(np.argmax(np.abs(np.fft.ifft(np.fft.ifftshift(x)))))
        x = x * np.exp(2j * np.pi * np.arange(planned["nbins"]) * d / planned["nbins"])
        out["delay_samples"] = d
    except (ValueError, FloatingPointError):              # pragma: no cover
        pass
    x, r, noise = x[bins], r[bins], noise[bins]
    rp = np.abs(r) ** 2
    good = rp > 0
    h = np.zeros(len(bins), dtype=complex)
    h[good] = x[good] / (n_frames * rp[good])
    # Noise on X: the receive noise in the bin times the reference, summed
    # over the frames -> sigma^2 = n * P_noise * |R|^2.
    sigma = np.sqrt(np.maximum(n_frames * noise * rp, 1e-300))
    out["h"], out["snr"] = h, np.abs(x) / sigma
    out["snr_median"] = float(np.median(out["snr"][good])) if good.any() else 0.0
    return out


def power_ratio(h, reference_h):
    """The chain's *power* gain relative to the reference, per pilot bin.

    `h` is a field response - X = n H |R|^2, so h = H - and the recorded
    counts are powers, so everything applied to counts is |h|^2. Fitting the
    amplitude ratio and applying it to counts would be wrong by a square, and
    invisible at the percent level where the two differ by a factor of two.
    """
    ref = np.abs(np.asarray(reference_h, dtype=complex))
    out = np.full(len(ref), np.nan)
    good = ref > 0
    out[good] = (np.abs(np.asarray(h, dtype=complex)[good]) / ref[good]) ** 2
    return out


def level_and_slope(h, snr, planned, reference_h, cfg):
    """(level, slope, level_err) of `h` against `reference_h`, or None if the
    burst was not seen or the numbers are unusable.

    Both are *power* quantities, applicable to counts as they stand: the
    level is the power gain at the band centre and the slope its fractional
    change per MHz.
    """
    if reference_h is None or float(np.median(snr)) < cfg["detect_snr"]:
        return None
    ref = np.asarray(reference_h, dtype=complex)
    ok = (np.abs(ref) > 0) & (snr > 1.0)
    if ok.sum() < 3:
        return None
    g = power_ratio(h, ref)[ok]
    w = snr[ok] ** 2
    x = (planned["freq_hz"][planned["bins"]][ok] - planned["centre_hz"]) / 1e6
    W = np.sqrt(w)
    A = np.vstack([W, W * x]).T
    coef, *_ = np.linalg.lstsq(A, W * g, rcond=None)
    a, b = float(coef[0]), float(coef[1])
    if not (0.25 < a < 4.0):
        return None
    return a, float(np.clip(b / a, -0.2, 0.2)), float(1.0 / math.sqrt(np.sum(w)))


def factor(level, slope, freq_hz, centre_hz):
    """The per-channel factor the counts are divided by: level x (1 + slope (f - fc)/MHz),
    clamped so a bad estimate cannot do more than double or halve anything."""
    f = np.asarray(freq_hz, dtype=float)
    fac = float(level) * (1.0 + float(slope) * (f - float(centre_hz)) / 1e6)
    return np.clip(fac, 0.5, 2.0)


def delay_filter(p_full, sample_rate_hz, max_delay_s):
    """Keep only the delays a real passband has (issue #30).

    The response is a few SAW multi-transit echoes plus a smooth shape, so it
    is sparse in delay: zeroing everything beyond `max_delay_s` throws away
    most of the per-bin noise and none of the structure. A straight line is
    taken out first and put back, so the band edges do not have to be
    periodic for the transform to behave.
    """
    p = np.asarray(p_full, dtype=float)
    n = len(p)
    x = np.arange(n) - 0.5 * (n - 1)
    a, b = np.polyfit(x, p, 1)
    trend = a * x + b
    resid = p - trend
    k_max = int(round(float(max_delay_s) * float(sample_rate_hz)))
    if k_max < 1 or 2 * k_max + 1 >= n:
        return p
    spec = np.fft.fft(resid)
    spec[k_max + 1:n - k_max] = 0.0
    return trend + np.fft.ifft(spec).real


def correction_vector(h_mean, planned, freq_hz, norm_band_hz, reference_h=None,
                      max_delay_s=10e-6):
    """The normalised passband correction on an arbitrary axis from a mean response.

    The power ratio to the reference (or the shape itself with no reference),
    with its straight line removed - `factor` already carries the level and
    the tilt, and applying either twice would be a bug - then delay-filtered,
    interpolated onto `freq_hz` and normalised to unit median over
    `norm_band_hz`. What is left is the ripple and any curvature: the part a
    stored bandpass template cannot follow from day to day. Clamped like the
    factor; ones where the axis reaches outside the pilot's bins.
    """
    bins = planned["bins"]
    nb = planned["nbins"]
    f_wide = planned["freq_hz"]
    p = np.abs(np.asarray(h_mean, dtype=complex)) ** 2
    if reference_h is not None:
        p = power_ratio(h_mean, reference_h)
    good = np.isfinite(p) & (p > 0)
    f = np.asarray(freq_hz, dtype=float)
    vec = np.ones(f.shape, dtype=float)
    if good.sum() < 16:
        return vec
    # Onto the uniform wide grid (the LO guard interpolated across) so the
    # delay filter has something to transform.
    p_full = np.interp(np.arange(nb), bins[good], p[good])
    spacing = float(np.median(np.diff(f_wide)))
    p_full = delay_filter(p_full, nb * spacing, max_delay_s)
    # ... and the straight line out: the level and tilt are `factor`'s.
    x = f_wide - planned["centre_hz"]
    a, b = np.polyfit(x, p_full, 1)
    shape = p_full / np.maximum(a * x + b, 1e-12)
    lo_b, hi_b = f_wide[bins[good]].min(), f_wide[bins[good]].max()
    inside = (f >= lo_b) & (f <= hi_b)
    vec[inside] = np.interp(f[inside], f_wide, shape)
    lo, hi = float(norm_band_hz[0]), float(norm_band_hz[1])
    band = inside & (f >= lo) & (f <= hi)
    med = float(np.median(vec[band])) if band.any() else float(np.median(vec[inside]))
    if med > 0:
        vec = vec / med
    vec[~inside] = 1.0
    return np.clip(vec, 0.5, 2.0)


class PilotTracker:
    """Per-run bookkeeping in the receiver: the reference, the bursts, what to apply.

    `burst(xspec, n_on, noise_per_bin, now)` takes one burst record's
    accumulation and returns its estimate; `correction(now)` says what a
    science record now should be divided by, and `shape(now)` hands over the
    mean response of the bursts inside the shape window.
    """

    def __init__(self, cfg, planned, reference_h=None, expected_interval_s=None):
        self.cfg = cfg
        self.planned = planned
        # What the recorder intends the burst spacing to be. Used until two
        # bursts have been seen and the real spacing is known: without it the
        # first burst's level would be held for an arbitrary default rather
        # than for the interval it belongs to.
        self._expected_interval_s = (float(expected_interval_s)
                                     if expected_interval_s else None)
        self.reference_h = None if reference_h is None else np.asarray(reference_h, dtype=complex)
        self.anchored = reference_h is not None
        self.bursts = 0
        self.detected = 0
        self._last = None                   # (t, level, slope)
        self._shape = deque()               # (t, h, seconds) of detected bursts
        self._frame_s = None                # seconds per FFT frame, from the plan
        self._interval_s = None             # measured burst spacing
        self._shape_version = 0
        self._shape_mean = None
        self._tone_ref = None               # the carrier's power at the last burst
        self._tone_ref_t = None
        self.tone_records = 0

    def burst(self, xspec, n_on, noise_per_bin, now):
        self.bursts += 1
        est = estimate(xspec, n_on, self.planned, noise_per_bin)
        out = {"burst": 1, "ok": 0, "level": 1.0, "slope": 0.0, "snr": float(est["snr_median"]),
               "n_on": int(n_on), "h": est["h"], "level_err": float("nan"),
               "delay_samples": est["delay_samples"]}
        if est["snr_median"] < self.cfg["detect_snr"] or n_on <= 0:
            return out
        if self.reference_h is None:
            # First sight of the pilot with nothing to anchor to: this run
            # becomes its own reference, and the file says so.
            self.reference_h = est["h"].copy()
        ls = level_and_slope(est["h"], est["snr"], self.planned, self.reference_h, self.cfg)
        if ls is None:
            return out
        level, slope, err = ls
        if self._last is not None:
            gap = now - self._last[0]
            self._interval_s = gap if self._interval_s is None else 0.5 * (self._interval_s + gap)
        self._last = (now, level, slope)
        self.detected += 1
        self._shape.append((now, est["h"].copy(), n_on * self._frame_seconds()))
        self._prune(now)
        self._shape_mean = None
        self._shape_version += 1
        out.update(ok=1, level=level, slope=slope, level_err=err)
        return out

    def give_up(self):
        """True when the pilot has never been seen after enough bursts.

        A pilot that is not there costs one record an interval for nothing -
        the state until the vertex dipole is wired - so the run stops asking.
        Never once it has been seen: an intermittent pilot is a fault to
        record, not a reason to stop measuring.
        """
        limit = int(self.cfg["give_up_after_bursts"])
        return bool(limit and self.detected == 0 and self.bursts >= limit)

    def _interval(self):
        """The burst spacing: measured once two have been seen, else intended."""
        return (self._interval_s or self._expected_interval_s
                or self.cfg["shape_window_s"] / 10.0)

    def set_tone_reference(self, power, now=None):
        """The carrier's power at a burst: the level the records after it are
        measured against, so the burst's own level and the carrier's never
        count the same change twice - at a burst the carrier reads unity by
        construction, and between bursts it carries the departure since."""
        if power and power > 0:
            self._tone_ref = float(power)
            self._tone_ref_t = now

    def tone_level(self, tone, now=None):
        """(level, ok) for a science record's carrier: its power relative to
        the last burst's, which is the fast common-mode gain change since the
        burst that set `level` and `slope`.

        Bounded by the same rule as the burst's own level: a reference older
        than `hold_bursts` intervals is stale, and the carrier would then be
        carrying the slow drift the burst is supposed to carry - so it stops
        being applied rather than silently changing meaning.
        """
        if not tone.get("detected") or not self._tone_ref:
            return 1.0, 0
        if (now is not None and self._tone_ref_t is not None
                and now - self._tone_ref_t > self.cfg["hold_bursts"] * max(self._interval(), 1.0) + 1.0):
            return 1.0, 0
        lvl = float(tone["power"]) / self._tone_ref
        if not (0.5 < lvl < 2.0):
            return 1.0, 0
        self.tone_records += 1
        return lvl, 1

    @property
    def pilot_seconds(self):
        """Accumulated burst time inside the shape window."""
        return float(sum(e[2] for e in self._shape))

    def _frame_seconds(self):
        """How long one FFT frame lasts, from the plan's own frequency axis."""
        if self._frame_s is None:
            spacing = float(np.median(np.diff(self.planned["freq_hz"])))
            fs = self.planned["nbins"] * spacing
            self._frame_s = self.planned["nbins"] / fs if fs > 0 else 0.0
        return self._frame_s

    def _prune(self, now):
        while self._shape and now - self._shape[0][0] > self.cfg["shape_window_s"]:
            self._shape.popleft()

    def correction(self, now):
        """What a science record at `now` is divided by.

        (level, slope, ok): the latest detected burst's level and slope while
        it is recent - within hold_bursts burst intervals - else unit and not
        ok, so a pilot that vanished stops being applied rather than freezing
        the last thing it saw.
        """
        if self._last is None:
            return 1.0, 0.0, 0
        t, level, slope = self._last
        if now - t > self.cfg["hold_bursts"] * max(self._interval(), 1.0) + 1.0:
            return 1.0, 0.0, 0
        return level, slope, 1

    def shape(self, now):
        """(version, mean response over the shape window), or (version, None).

        None until `min_shape_pilot_s` of pilot has accumulated inside the
        window: one short burst's per-bin noise is larger than the ripple it
        would correct, so applying it early would make the spectra worse, not
        better. Counted in seconds rather than bursts because a burst is one
        record and records are not a fixed length.
        """
        self._prune(now)
        if self.pilot_seconds < self.cfg["min_shape_pilot_s"]:
            return self._shape_version, None
        if self._shape_mean is None:
            # Weighted by each burst's own length: a 60 s burst carries
            # twenty times the information of a 3 s one.
            w = np.array([e[2] for e in self._shape], dtype=float)
            w = w / w.sum() if w.sum() > 0 else np.full(len(w), 1.0 / len(w))
            self._shape_mean = np.tensordot(w, np.array([e[1] for e in self._shape]), axes=1)
        return self._shape_version, self._shape_mean


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


def describe(cfg, integration_time_s=None):
    if not cfg or not cfg.get("enabled"):
        return "pilot off"
    every = burst_every(cfg, integration_time_s) if integration_time_s else None
    if every:
        cadence = ("every %d records (%.0f s, %.0f%% of them)"
                   % (every, every * integration_time_s, 100.0 / every))
    elif cfg.get("burst_every_records"):
        cadence = "every %d records" % cfg["burst_every_records"]
    else:
        cadence = ("every %.0f s, at most %.0f%% of records"
                   % (cfg["burst_interval_s"], 100 * cfg["max_duty_cycle"]))
    text = ("pilot: full-band comb burst %s at %.2f of full scale, TX gain %.0f dB"
            % (cadence, cfg["burst_amplitude"], cfg["tx_gain_db"]))
    if cfg.get("tone_enabled"):
        text += ("; continuous carrier at %.3f MHz, %.2f of full scale, %s"
                 % (cfg["tone_hz"] / 1e6, cfg["tone_amplitude"],
                    "applied" if cfg.get("tone_apply") else "recorded only"))
    return text
