#!/usr/bin/env python3
"""Where to put the local oscillator, and how wide to sample.

The B210 is a direct-conversion receiver, so the tuned frequency lands on the
FFT's DC bin, where LO leakage and ADC offset live. UHD corrects for that
automatically, and the correction subtracts whatever is there - including
signal. Tuned to the H I rest frequency, as every observation was until
2026-08-24, the receiver was quietly removing the line it was measuring: a
three-channel notch 7.6% deep centred exactly on 1420.405752 MHz, measured
toward the Lockman Hole.

So the LO is offset and the line is observed away from DC. Nothing else about
the observation changes: the spectra are recorded against true sky frequency
either way, because the frequency axis is built from the tuned centre. The
offset is recorded in the file header so a spectrum can always say where its
DC artefact was.

The offset cannot be arbitrary. It has to be large enough to put the artefact
clear of the line, and small enough that the line stays in the flat part of the
band - the B210's decimation filter rolls off hard near the edges. Measured
from a 2 MHz Lockman Hole spectrum on 2026-08-24: response is above 90% of peak
within +-0.46 MHz of centre, which is +-23% of the sample rate, and has fallen
to 29% at the band edge. That measurement is what USABLE_HALF_WIDTH below
encodes, and it is why asking for a large offset raises the sample rate rather
than pushing the line into the roll-off.

This module is deliberately free of GNU Radio and Flask so that the receiver
and the scheduler can both import it and cannot disagree about the answer.
"""

H1_REST_FREQ_HZ = 1420.405752e6

# ---------------------------------------------------------------------------
# The fixed instrument (issue #27, decided 2026-08-26)
#
# Scheduled observations no longer choose a tuning. The B210 is always at the
# same LO, sample rate and gain, and every recording carries two products
# from the one stream: a coarse spectrum across the whole band (continuum,
# RFI, the calibration comb of #26) and a fine H I sub-band. What kind of
# observation it is decides only what is plotted and fitted.
#
# The numbers, and why:
#   sample rate 8 Msps  - clean in the 2026-08-26 ladder with margin (one
#                         overflow in ten minutes; 12 Msps overflowed steadily,
#                         the host's FFT thread being the limit, not USB), and
#                         the rate at which the LO fits outside the sub-band.
#   LO = line - 1.5 MHz - on the positive-velocity edge, where there is no
#                         emission beyond +300 km/s, so the DC spur can never
#                         sit on a line and is outside the H I product.
#   H I sub-band LO+0.1..+3.4 MHz = topocentric -400..+300 km/s: the whole
#                         disc and every major HVC complex, with the Earth's
#                         +-50 km/s absorbed. M31 (to -600) is not reachable
#                         from this tuning; it is the manual GUI's case.
#   continuum LO-3.2..-0.1 MHz - 3.1 MHz with no hydrogen (v > +330 km/s),
#                         on the good side of the SAW filter.
#   gain 40 dB          - has held the Sun at +2000 K.
#
# These are config values (scheduler_config.json, receiver_* keys) so they can
# be changed deliberately, and they are the defaults so that a receiver run by
# hand with --headless records exactly what a scheduled one does.
# ---------------------------------------------------------------------------

FIXED_LO_HZ = H1_REST_FREQ_HZ - 1.5e6            # 1418.905752 MHz
FIXED_SAMPLE_RATE_HZ = 8.0e6
FIXED_GAIN_DB = 40.0
WIDE_CHANNELS = 1024                              # 7.8 kHz per channel
H1_BAND_HZ = (FIXED_LO_HZ + 0.1e6, FIXED_LO_HZ + 3.4e6)
# Over the decimated 4 Msps: 3.9 kHz = 0.82 km/s, 845 channels kept across
# the sub-band. Chosen 2026-08-26 over 2048: the narrowest galactic H I is
# 2-3 km/s wide and the HI4PI model is 1.29 km/s, so 0.8 km/s still
# Nyquist-samples anything the 5 deg beam can show, at half the file size.
H1_CHANNELS = 1024
H1_DECIMATION = 2                                 # 8 -> 4 Msps holds a 3.3 MHz band
CONTINUUM_BAND_HZ = (FIXED_LO_HZ - 3.2e6, FIXED_LO_HZ - 0.1e6)

INSTRUMENT_KEYS = ("lo_hz", "sample_rate_hz", "gain_db", "wide_channels",
                   "h1_band_hz", "h1_channels", "h1_decimation", "continuum_band_hz")


def fixed_instrument(overrides: dict | None = None) -> dict:
    """The instrument a scheduled observation records with.

    `overrides` are the scheduler's config values (keys as in INSTRUMENT_KEYS,
    or prefixed `receiver_`); anything missing takes the fixed default. The
    result is what the scheduler hands the receiver (H1_INSTRUMENT) and what
    the receiver writes into the file.
    """
    inst = {
        "lo_hz": FIXED_LO_HZ,
        "sample_rate_hz": FIXED_SAMPLE_RATE_HZ,
        "gain_db": FIXED_GAIN_DB,
        "wide_channels": WIDE_CHANNELS,
        "h1_band_hz": list(H1_BAND_HZ),
        "h1_channels": H1_CHANNELS,
        "h1_decimation": H1_DECIMATION,
        "continuum_band_hz": list(CONTINUUM_BAND_HZ),
    }
    for key in INSTRUMENT_KEYS:
        for name in (key, "receiver_" + key):
            if overrides and overrides.get(name) not in (None, ""):
                inst[key] = overrides[name]
    inst["lo_hz"] = float(inst["lo_hz"])
    inst["sample_rate_hz"] = float(inst["sample_rate_hz"])
    inst["gain_db"] = float(inst["gain_db"])
    inst["wide_channels"] = int(inst["wide_channels"])
    inst["h1_channels"] = int(inst["h1_channels"])
    inst["h1_decimation"] = int(inst["h1_decimation"])
    inst["h1_band_hz"] = [float(inst["h1_band_hz"][0]), float(inst["h1_band_hz"][1])]
    inst["continuum_band_hz"] = [float(inst["continuum_band_hz"][0]),
                                 float(inst["continuum_band_hz"][1])]
    return inst


def h1_subband_plan(inst: dict) -> dict:
    """How the H I product is cut from the wide stream.

    A frequency-translating decimator centred on the sub-band, so the LO's DC
    spur (at the band's low edge) is outside it. The decimated stream is
    wider than the sub-band, so only the channels inside it are kept.
    """
    lo, hi = inst["h1_band_hz"]
    centre = 0.5 * (lo + hi)
    out_rate = inst["sample_rate_hz"] / inst["h1_decimation"]
    if (hi - lo) > 0.9 * out_rate:
        raise ValueError("the H I sub-band (%.2f MHz) does not fit the decimated "
                         "rate (%.2f Msps)" % ((hi - lo) / 1e6, out_rate / 1e6))
    return {
        "centre_hz": centre,
        "offset_from_lo_hz": centre - inst["lo_hz"],
        "out_rate_hz": out_rate,
        "channels": inst["h1_channels"],
        "channel_width_hz": out_rate / inst["h1_channels"],
        "band_hz": [lo, hi],
        # The anti-alias low-pass on the translated stream: flat across the
        # whole sub-band with 0.2 MHz to spare (the first cut, 0.1 MHz and a
        # wide transition, rolled the outer 100 kHz of the H I band off by
        # 20%), and a transition that ends past the decimated Nyquist - what
        # folds back lands outside the kept channels.
        "cutoff_hz": 0.5 * (hi - lo) + 0.2e6,
        "transition_hz": 0.2e6,
    }


def describe_instrument(inst: dict) -> str:
    """One line for the log: what the fixed instrument is."""
    plan = h1_subband_plan(inst)
    return ("LO %.6f MHz, %.1f Msps, gain %.0f dB; H I %.3f-%.3f MHz in %d channels "
            "(%.2f kHz); continuum %.3f-%.3f MHz in %d channels"
            % (inst["lo_hz"] / 1e6, inst["sample_rate_hz"] / 1e6, inst["gain_db"],
               plan["band_hz"][0] / 1e6, plan["band_hz"][1] / 1e6, plan["channels"],
               plan["channel_width_hz"] / 1e3,
               inst["continuum_band_hz"][0] / 1e6, inst["continuum_band_hz"][1] / 1e6,
               inst["wide_channels"]))

# How far the LO sits from the line. 0.8 MHz is 169 km/s at 21 cm, which clears
# the H I of any ordinary galactic sightline, so the DC artefact never lands in
# the signal or in the baseline either side of it.
DEFAULT_LO_OFFSET_HZ = 0.8e6

# Fraction of the sample rate, either side of centre, within which the response
# stays above 90% of peak.
#
# Measured on sky toward the Lockman Hole on 2026-08-24, and the history is
# worth keeping because the first value was measured under one configuration
# and then used as though it were a property of the receiver. With the analog
# filter set equal to the sample rate it is 23%; widening that filter to twice
# the sample rate moves it to 27%. The number below is the second one.
#
# It is deliberately not used as the target. Putting the line at exactly this
# fraction lands it on the 90% contour - the shoulder - which is where the
# first version of this put it. LINE_PLACEMENT below leaves margin.
USABLE_HALF_WIDTH = 0.27

# Where the line should actually sit, as a fraction of the sample rate from
# centre. Measured response at the line against this fraction, analog filter at
# 2x: 23% -> 91%, 18% -> 92%, 15% -> 94%. It improves slowly, because the
# decimation filter starts drooping well before the 90% contour, so chasing the
# last few percent costs bandwidth for very little. 18% is the knee of that
# curve: most of the available gain, at a sample rate the B210 and the disk do
# not notice.
LINE_PLACEMENT = 0.18

# The analog baseband filter, as a multiple of the sample rate. It is a lowpass
# on I and Q, so in RF terms a bandpass centred on the LO - it cannot be centred
# anywhere else, which is why the artefact is moved instead of filtered out.
# Setting it equal to the sample rate, as the receiver did until 2026-08-24,
# puts its corner at the band edge so the roll-off eats the whole recorded band:
# band edges at 28% of peak rather than 36%. Anti-aliasing is the AD9361's
# digital decimation filters' job regardless.
ANALOG_BW_FACTOR = 2.0

# Sample rates are rounded up to a multiple of this, so the number that appears
# on the page and in the file header is one a person would recognise.
SAMPLE_RATE_GRANULARITY_HZ = 0.5e6


def next_fast_size(n: int) -> int:
    """Smallest 5-smooth integer >= n.

    Scaling a channel count by a ratio lands on arbitrary integers, and an FFT
    length with a large prime factor is slow - 5973 is 3 x 11 x 181, and would
    be transformed by the general-purpose path rather than the fast one. Sizes
    whose only prime factors are 2, 3 and 5 all have fast transforms, and they
    are dense enough that rounding up to one barely moves the channel width.
    """
    if n <= 1:
        return 1
    best = None
    power_of_two = 1
    while power_of_two < n * 2:
        for three in _powers(3, n * 2 // power_of_two):
            for five in _powers(5, max(1, n * 2 // (power_of_two * three))):
                value = power_of_two * three * five
                if value >= n and (best is None or value < best):
                    best = value
        power_of_two *= 2
    return best or n


def _powers(base: int, limit: int):
    value = 1
    while value <= max(limit, 1):
        yield value
        value *= base


def minimum_sample_rate(lo_offset_hz: float = DEFAULT_LO_OFFSET_HZ) -> float:
    """Narrowest sample rate that still leaves the line in the flat band."""
    if lo_offset_hz <= 0:
        return 0.0
    exact = abs(lo_offset_hz) / LINE_PLACEMENT
    steps = int(exact / SAMPLE_RATE_GRANULARITY_HZ)
    if steps * SAMPLE_RATE_GRANULARITY_HZ < exact:
        steps += 1
    return steps * SAMPLE_RATE_GRANULARITY_HZ


def plan_tuning(sky_center_freq_hz: float,
                requested_sample_rate_hz: float,
                requested_channels: int | None = None,
                lo_offset_hz: float = DEFAULT_LO_OFFSET_HZ) -> dict:
    """Decide the LO frequency, the sample rate and the channel count.

    `sky_center_freq_hz` is the frequency the observer cares about - the line.
    The returned `tuned_center_freq_hz` is where the hardware is actually put,
    and the spectra are still recorded against true sky frequency, so nothing
    downstream has to know about any of this.

    Raising the sample rate widens every channel, which would quietly coarsen
    the velocity resolution the observer asked for. So the channel count is
    scaled with it and the original channel width is preserved.
    """
    requested_sample_rate_hz = float(requested_sample_rate_hz)
    needed = minimum_sample_rate(lo_offset_hz)
    sample_rate = max(requested_sample_rate_hz, needed)
    raised = sample_rate > requested_sample_rate_hz + 1.0

    channels = requested_channels
    if channels and raised and requested_sample_rate_hz > 0:
        # Keep the channel width, so the resolution is what was asked for.
        scale = sample_rate / requested_sample_rate_hz
        channels = next_fast_size(int(round(requested_channels * scale)))

    tuned = float(sky_center_freq_hz) + float(lo_offset_hz)
    plan = {
        "sky_center_freq_hz": float(sky_center_freq_hz),
        "tuned_center_freq_hz": tuned,
        "lo_offset_hz": float(lo_offset_hz),
        "sample_rate_hz": sample_rate,
        "requested_sample_rate_hz": requested_sample_rate_hz,
        "sample_rate_raised": raised,
        "channels": channels,
        "requested_channels": requested_channels,
        "line_offset_fraction": (abs(lo_offset_hz) / sample_rate
                                 if sample_rate else 0.0),
    }
    if channels and sample_rate:
        plan["channel_width_hz"] = sample_rate / channels
    return plan


def describe_tuning(plan: dict) -> str:
    """One line for the page and the log, saying what was actually done."""
    mhz = 1e6
    parts = [
        "LO at %.6f MHz, %.2f MHz above the line, so the DC artefact "
        "falls clear of it" % (plan["tuned_center_freq_hz"] / mhz,
                               plan["lo_offset_hz"] / mhz)
    ]
    if plan["sample_rate_raised"]:
        parts.append("sample rate raised %.2f to %.2f MHz to keep the line in "
                     "the flat part of the band"
                     % (plan["requested_sample_rate_hz"] / mhz,
                        plan["sample_rate_hz"] / mhz))
        if plan.get("channels") and plan.get("requested_channels"):
            parts.append("channels %d to %d, holding %.2f kHz resolution"
                         % (plan["requested_channels"], plan["channels"],
                            plan["channel_width_hz"] / 1e3))
    return "; ".join(parts)
