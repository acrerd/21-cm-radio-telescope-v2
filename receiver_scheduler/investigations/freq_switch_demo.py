#!/usr/bin/env python3
"""Reduce this morning's LO-offset runs as a frequency-switched pair.

The eight runs were taken to test whether the bandpass moves with the LO. They
are also, without any further observing, a frequency-switching experiment: the
0.6 MHz and 1.2 MHz offset runs differ by a 0.6 MHz throw on the same field.

The reduction is done in channel space, which is where the method works. A
spectrometer channel is fixed relative to the LO, so the instrumental bandpass
B sits in the same channel in both phases while the line moves by the throw:

    A(i) = B(i) * (Tsys + Tline(LO_A + nu_i))
    B(i) = B(i) * (Tsys + Tline(LO_B + nu_i))

so (A - B) / ((A + B)/2) leaves Tline/Tsys with B divided out and Tsys removed,
and the line appears twice - positive where phase A put it, negative where
phase B did. Folding the negative copy back onto the positive one recovers the
root-two the two-phase split costs.

Compared here against the alternative: the same data as plain total power with a
polynomial baseline fitted to it, which is what you must do if you cannot switch.
"""

import glob
import os

import h5py
import numpy as np

# receiver_scheduler/, where the recording data and the receiver live;
# this script sits one level down in investigations/.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H1 = 1420.405752e6
C_KMS = 299792.458


def load_group(offset_mhz):
    """Mean spectrum of every run at one LO offset, in channel space."""
    paths = sorted(glob.glob(os.path.join(
        HERE, "data", "bandpass_lo_%03d_*.h5" % round(offset_mhz * 100))))
    spectra, taus, lo, sr = [], 0.0, None, None
    for p in paths:
        with h5py.File(p, "r") as hf:
            spectra.append(np.array(hf["spectra_kelvin" if "spectra_kelvin" in hf else "spectra_linear"][:], float))
            taus += float(np.nansum(hf["integration_times"][:]))
            a = dict(hf.attrs)
            lo, sr = a["center_freq_hz"], a["sample_rate_hz"]
    stack = np.vstack(spectra)
    return dict(mean=stack.mean(axis=0), n=stack.shape[0], tau=taus,
                lo=lo, sr=sr, paths=[os.path.basename(p) for p in paths])


def main():
    A = load_group(0.6)
    B = load_group(1.2)
    assert A["sr"] == B["sr"] and A["mean"].size == B["mean"].size
    nch, sr = A["mean"].size, A["sr"]
    throw = B["lo"] - A["lo"]
    # Baseband frequency of each channel: identical for both phases.
    nu = (np.arange(nch) - nch // 2) * (sr / nch)

    print("Phase A: LO %.6f MHz, %d records, %.0f s  (%s)"
          % (A["lo"] / 1e6, A["n"], A["tau"], ", ".join(A["paths"])))
    print("Phase B: LO %.6f MHz, %d records, %.0f s  (%s)"
          % (B["lo"] / 1e6, B["n"], B["tau"], ", ".join(B["paths"])))
    print("Throw %.3f MHz = %.1f km/s at 21 cm\n" % (throw / 1e6, throw / H1 * C_KMS))

    # ---- frequency switching ------------------------------------------------
    total = (A["mean"] + B["mean"]) / 2.0
    diff = (A["mean"] - B["mean"]) / total          # Tline/Tsys, bandpass gone

    # Where each phase puts the line, in baseband
    nu_A = H1 - A["lo"]
    nu_B = H1 - B["lo"]
    shift = int(round(throw / (sr / nch)))
    print("Line sits at baseband %+.3f MHz in phase A (positive) and %+.3f MHz "
          "in phase B (negative)" % (nu_A / 1e6, nu_B / 1e6))

    # Fold: the negative copy is 'shift' channels below the positive one.
    folded = (diff - np.roll(diff, -shift)) / 2.0

    # ---- the alternative: total power with a polynomial baseline ------------
    use = np.abs(nu) < 0.25 * sr
    line_win = np.abs(nu - nu_A) < 400e3
    fit_ch = use & ~line_win & (np.abs(nu) > 30e3)
    u = nu / (0.25 * sr)
    poly = np.polyfit(u[fit_ch], A["mean"][fit_ch], 5)
    tp = A["mean"] / np.polyval(poly, u) - 1.0

    # ---- compare ------------------------------------------------------------
    def band_stats(y, label):
        base = use & ~line_win & ~(np.abs(nu - nu_B) < 400e3) & (np.abs(nu) > 30e3)
        peak = np.nanmax(y[line_win])
        rms = np.nanstd(y[base])
        print("   %-34s peak %+.4f   baseline rms %.4f   S/N %5.1f"
              % (label, peak, rms, peak / rms))
        return peak, rms

    print("\nOver the usable band, line window excluded from the baseline:")
    band_stats(tp, "total power, order-5 baseline")
    band_stats(diff, "frequency switched, unfolded")
    band_stats(folded, "frequency switched, folded")

    # Baseline curvature left behind: how much a further polynomial would remove
    print("\nHow much structure a further cubic would still take out of each:")
    for y, label in ((tp, "total power, order-5 baseline"),
                     (folded, "frequency switched, folded")):
        base = use & ~line_win & ~(np.abs(nu - nu_B) < 400e3) & (np.abs(nu) > 30e3)
        uu = np.linspace(-1, 1, base.sum())
        before = np.nanstd(y[base])
        after = np.nanstd(y[base] - np.polyval(np.polyfit(uu, y[base], 3), uu))
        print("   %-34s %.4f -> %.4f   (removable %.4f)"
              % (label, before, after, np.sqrt(max(before**2 - after**2, 0))))

    np.savez(os.path.join(HERE, "data", "freq_switch_demo.npz"),
             nu=nu, diff=diff, folded=folded, tp=tp, total=total,
             nu_A=nu_A, nu_B=nu_B, throw=throw, sr=sr, use=use)
    print("\nsaved data/freq_switch_demo.npz")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
