#!/usr/bin/env python3
"""The receiver's own account of its decimation chain, and the response it implies.

UHD exposes the AD9361's filter chain - the fixed half-band and DEC3 coefficients
and, importantly, the 128 programmable FIR taps that UHD itself designs for the
requested bandwidth. So this part of the bandpass need not be measured or
modelled: the hardware can be asked what it is set to, and the response computed
exactly from the coefficients in use.

What UHD does *not* expose is the FPGA's own digital down-converter, which takes
the AD9361's output rate down to the requested sample rate. On this B210 at
7 Msps the AD9361 runs at a 56 MHz master clock, so the FPGA decimates by a
further 8, and that stage's anti-alias filter is the one whose corner sits at the
output Nyquist frequency. Anything this module cannot account for belongs to
that stage or to the analogue front end, which is the point of computing it: the
residual is what step two has to measure.

Run directly to capture the chain from the attached B210 into a JSON file.
"""

import json
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CHAIN_FILE = os.path.join(HERE, "ad9361_chain.json")


def capture(sample_rate, bandwidth, center_freq=1421.205752e6, gain=40.0,
            path=CHAIN_FILE):
    """Ask the attached B210 for its filter chain and store it."""
    import uhd

    u = uhd.usrp.MultiUSRP("type=b200")
    u.set_rx_rate(sample_rate)
    u.set_rx_bandwidth(bandwidth, 0)
    u.set_rx_freq(uhd.types.TuneRequest(center_freq), 0)
    u.set_rx_gain(gain, 0)

    chain = {"sample_rate_hz": u.get_rx_rate(),
             "bandwidth_hz": u.get_rx_bandwidth(0),
             "requested_sample_rate_hz": sample_rate,
             "requested_bandwidth_hz": bandwidth,
             "filters": {}}
    for name in u.get_rx_filter_names(0):
        short = name.rsplit("/", 1)[1]
        f = u.get_rx_filter(name, 0)
        entry = {"kind": type(f).__name__}
        for attr in ("get_input_rate", "get_output_rate", "get_decimation",
                     "get_interpolation", "get_full_scale", "get_cutoff",
                     "get_rolloff"):
            if hasattr(f, attr):
                try:
                    entry[attr[4:]] = float(getattr(f, attr)())
                except Exception:                     # noqa: BLE001
                    pass
        if hasattr(f, "get_taps"):
            entry["taps"] = [float(t) for t in f.get_taps()]
        chain["filters"][short] = entry

    with open(path, "w") as fh:
        json.dump(chain, fh, indent=2)
    return chain


def load(path=CHAIN_FILE):
    with open(path) as fh:
        return json.load(fh)


def _fir_response(taps, rate_hz, freq_hz):
    """|H(f)| of a FIR with these taps clocked at rate_hz, normalised at DC."""
    taps = np.asarray(taps, float)
    n = np.arange(taps.size)
    phase = np.exp(-2j * np.pi * np.outer(freq_hz / rate_hz, n))
    h = phase @ taps
    return np.abs(h) / abs(taps.sum())


def _analog_response(cutoff_hz, order, freq_hz):
    """Butterworth magnitude, normalised at DC. Power response is this squared."""
    return 1.0 / np.sqrt(1.0 + (np.abs(freq_hz) / cutoff_hz) ** (2 * order))


def digital_response(chain, freq_hz):
    """Power response of every AD9361 stage that is actually enabled.

    A stage whose reported input and output rates are equal is bypassed - UHD
    still lists it, and still reports a decimation factor, so the rates are the
    only reliable indication of whether it is in circuit.
    """
    out = np.ones_like(np.asarray(freq_hz, float))
    used = []
    for name, f in chain["filters"].items():
        if "taps" not in f:
            continue
        rate = f.get("input_rate")
        if rate is None or f.get("output_rate") == rate:
            continue                                   # bypassed
        out = out * _fir_response(f["taps"], rate, freq_hz) ** 2
        used.append((name, rate, f.get("decimation")))
    return out, used


def analog_response(chain, freq_hz):
    """Power response of the two analogue low-pass stages.

    LPF_BB is the third-order baseband Butterworth and LPF_TIA the first-order
    pole at the transimpedance amplifier; UHD reports each one's corner, which
    is what it actually calibrated the part to rather than what was requested.
    """
    out = np.ones_like(np.asarray(freq_hz, float))
    for name, order in (("LPF_BB", 3), ("LPF_TIA", 1)):
        f = chain["filters"].get(name)
        if f and f.get("cutoff"):
            out = out * _analog_response(f["cutoff"], order, freq_hz) ** 2
    return out


def known_response(chain, freq_hz):
    """Everything the hardware can tell us about its own response, as power."""
    dig, used = digital_response(chain, freq_hz)
    return dig * analog_response(chain, freq_hz), used


def main():
    import sys
    rate = float(sys.argv[1]) if len(sys.argv) > 1 else 7.0e6
    bw = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0 * rate
    chain = capture(rate, bw)
    print("captured %s" % CHAIN_FILE)
    print("  sample rate %.3f MHz, analogue bandwidth %.3f MHz"
          % (chain["sample_rate_hz"] / 1e6, chain["bandwidth_hz"] / 1e6))
    for n, f in chain["filters"].items():
        bypassed = ("taps" in f and f.get("input_rate") == f.get("output_rate"))
        print("  %-8s %-24s %s%s"
              % (n, f["kind"],
                 ("%d taps" % len(f["taps"])) if "taps" in f
                 else "cutoff %.2f MHz" % (f.get("cutoff", 0) / 1e6),
                 "  (bypassed)" if bypassed else ""))

    nyq = chain["sample_rate_hz"] / 2
    f = np.linspace(-nyq, nyq, 401)
    resp, used = known_response(chain, f)
    print("\n  stages in circuit: %s" % ", ".join("%s @%.0f MHz /%d"
          % (n, r / 1e6, d) for n, r, d in used))
    print("\n  |f|/Nyquist   response predicted by the AD9361 chain")
    for x in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.98):
        i = int(np.argmin(np.abs(f - x * nyq)))
        print("     %.2f          %6.2f%%" % (x, 100 * resp[i] / resp.max()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
