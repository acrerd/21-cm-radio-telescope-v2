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
