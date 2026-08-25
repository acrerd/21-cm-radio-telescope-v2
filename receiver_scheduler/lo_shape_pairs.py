#!/usr/bin/env python3
"""Disentangle LO dependence from time drift over all 28 pairs of runs.

Every pair of the eight runs is characterised by two separations: how many
steps apart in time they were taken, and how far apart their local oscillators
were. The palindrome sequence makes those two nearly independent - at a time
separation of one step there are six pairs whose LOs differ and one whose LO is
identical, which is the comparison that matters and cannot be made from a
monotonic sweep.

If the bandpass shape is a property of the LO, the residual grows with LO
separation at fixed time separation. If it is drift, it grows with time
separation at fixed LO separation. Fitting both at once says which.
"""

import glob
import itertools
import os

import h5py
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
H1 = 1420.405752e6
HALF_BAND = 0.25


def load(path, order):
    with h5py.File(path, "r") as hf:
        f = np.array(hf["frequency_hz"][:], float)
        sp = np.array(hf["spectra_kelvin" if "spectra_kelvin" in hf else "spectra_linear"][:], float)
        a = dict(hf.attrs)
    lo, sr = a["center_freq_hz"], a["sample_rate_hz"]
    mean = sp.mean(axis=0)
    each = sp / np.median(sp, axis=1, keepdims=True)
    noise = each.std(axis=0, ddof=1).mean() / np.sqrt(sp.shape[0])
    bad = (np.abs(f - lo) < 25e3) | (np.abs(f - H1) < 400e3)
    m = np.where(bad, np.nan, mean)
    return dict(f=f, bb=f - lo, mean=m / np.nanmedian(m), lo=lo, sr=sr,
                noise=noise, order=order)


def residual(a, b, frame):
    sr = a["sr"]
    half = HALF_BAND * sr
    if frame == "baseband":
        grid = np.linspace(-half, half, 600)
        xa, xb = a["bb"], b["bb"]
    else:
        lo = max(a["lo"], b["lo"]) - half
        hi = min(a["lo"], b["lo"]) + half
        grid = np.linspace(lo, hi, 600)
        xa, xb = a["f"], b["f"]
    ya = np.interp(grid, xa[np.isfinite(a["mean"])], a["mean"][np.isfinite(a["mean"])])
    yb = np.interp(grid, xb[np.isfinite(b["mean"])], b["mean"][np.isfinite(b["mean"])])
    r = (ya / np.median(ya)) / (yb / np.median(yb))
    u = np.linspace(-1, 1, r.size)
    return 100 * (r - np.polyval(np.polyfit(u, r, 3), u)).std()


def main():
    paths = sorted(glob.glob(os.path.join(HERE, "data", "bandpass_lo_*.h5")),
                   key=lambda p: int(p.rsplit("_", 1)[1].split(".")[0]))
    runs = [load(p, i) for i, p in enumerate(paths)]

    rows = []
    for a, b in itertools.combinations(runs, 2):
        rows.append(dict(dt=abs(a["order"] - b["order"]),
                         dlo=abs(a["lo"] - b["lo"]) / 1e6,
                         bb=residual(a, b, "baseband"),
                         sky=residual(a, b, "sky")))

    print("The clean comparison: pairs one step apart in time\n")
    print("   dLO (MHz)   n    baseband rms   sky rms")
    one = [r for r in rows if r["dt"] == 1]
    for dlo in sorted({r["dlo"] for r in one}):
        g = [r for r in one if abs(r["dlo"] - dlo) < 1e-9]
        print("   %6.2f     %2d      %6.3f%%     %6.3f%%"
              % (dlo, len(g), np.mean([r["bb"] for r in g]),
                 np.mean([r["sky"] for r in g])))

    print("\nAll 28 pairs, grouped by time separation (baseband frame)\n")
    print("   steps apart   n    mean rms    mean dLO")
    for dt in sorted({r["dt"] for r in rows}):
        g = [r for r in rows if r["dt"] == dt]
        print("   %6d        %2d     %6.3f%%     %.2f MHz"
              % (dt, len(g), np.mean([r["bb"] for r in g]),
                 np.mean([r["dlo"] for r in g])))

    print("\nAll 28 pairs, grouped by LO separation (baseband frame)\n")
    print("   dLO (MHz)     n    mean rms    mean steps apart")
    for dlo in sorted({r["dlo"] for r in rows}):
        g = [r for r in rows if abs(r["dlo"] - dlo) < 1e-9]
        print("   %6.2f       %2d     %6.3f%%     %.1f"
              % (dlo, len(g), np.mean([r["bb"] for r in g]),
                 np.mean([r["dt"] for r in g])))

    # Joint least squares: rms^2 = c0 + c_t*dt + c_lo*dlo
    A = np.column_stack([np.ones(len(rows)),
                         [r["dt"] for r in rows],
                         [r["dlo"] for r in rows]])
    for frame in ("bb", "sky"):
        y = np.array([r[frame] ** 2 for r in rows])
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        resid = y - A @ coef
        se = np.sqrt(np.diag(np.linalg.pinv(A.T @ A)) * resid.var(ddof=3))
        name = {"bb": "baseband", "sky": "sky"}[frame]
        print("\njoint fit, %s frame:  rms^2 = c0 + c_t*(steps) + c_lo*(dLO/MHz)" % name)
        print("   c0    %8.4f +- %.4f   ->  floor        %.3f%%"
              % (coef[0], se[0], np.sqrt(max(coef[0], 0))))
        print("   c_t   %8.4f +- %.4f   ->  %.1f sigma" % (coef[1], se[1], abs(coef[1]) / se[1]))
        print("   c_lo  %8.4f +- %.4f   ->  %.1f sigma" % (coef[2], se[2], abs(coef[2]) / se[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
