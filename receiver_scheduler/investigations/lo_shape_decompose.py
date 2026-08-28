#!/usr/bin/env python3
"""Split the residual bandpass structure into LO-fixed and sky-fixed parts.

Each run's shape is detrended (a cubic removed, as any baseline fit would) and
then stacked two ways: aligned by baseband frequency and aligned by sky
frequency. Structure fixed to the LO survives the first stack and is smeared by
the second; structure fixed to the sky does the opposite. Comparing the surviving
amplitude against the scatter of the stack says which is real.

The catch this is built to expose: real H I emission is also fixed in sky
frequency, so it forges a sky-fixed instrumental term exactly. The test is run
at several H I blanking widths - a genuine instrumental feature is indifferent to
how much of the line is cut out, residual emission is not.

SUPERSEDED - kept for the record, do not trust its numbers. This reported a
7-sigma "sky-fixed" component in the bandpass. It was contaminated twice over:
by real H I emission in the line wings beyond the blanking width, and by the
band edges, where the detrending cubic is least constrained and each run's edge
falls at a different sky frequency. Restricting to a window wholly above the
line and inside 0.20 Fs of every LO removes both and the effect vanishes. See
lo_shape_pairs.py, which is the analysis that stands.
"""

import glob
import os

import h5py
import numpy as np

# receiver_scheduler/, where the recording data and the receiver live;
# this script sits one level down in investigations/.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H1 = 1420.405752e6
HALF_BAND = 0.25


def load(path, h1_blank_hz):
    with h5py.File(path, "r") as hf:
        f = np.array(hf["frequency_hz"][:], float)
        sp = np.array(hf["spectra_kelvin" if "spectra_kelvin" in hf else "spectra_linear"][:], float)
        a = dict(hf.attrs)
    lo, sr = a["center_freq_hz"], a["sample_rate_hz"]
    mean = sp.mean(axis=0)
    each = sp / np.median(sp, axis=1, keepdims=True)
    noise = each.std(axis=0, ddof=1).mean() / np.sqrt(sp.shape[0])
    bad = (np.abs(f - lo) < 25e3) | (np.abs(f - H1) < h1_blank_hz)
    mean = np.where(bad, np.nan, mean / np.nanmedian(np.where(bad, np.nan, mean)))
    return dict(f=f, bb=f - lo, mean=mean, lo=lo, sr=sr, noise=noise)


def detrended(run, grid, frame):
    x = run["bb"] if frame == "baseband" else run["f"]
    ok = np.isfinite(run["mean"])
    inside = (grid >= x[ok].min()) & (grid <= x[ok].max())
    y = np.full(grid.size, np.nan)
    y[inside] = np.interp(grid[inside], x[ok], run["mean"][ok])
    u = np.linspace(-1, 1, grid.size)
    g = np.isfinite(y)
    y[g] -= np.polyval(np.polyfit(u[g], y[g], 3), u[g])
    return y


def stack(runs, grid, frame):
    Y = np.vstack([detrended(r, grid, frame) for r in runs])
    with np.errstate(invalid="ignore"):
        avg = np.nanmean(Y, axis=0)
        scat = np.nanstd(Y, axis=0, ddof=1)
        cnt = np.sum(np.isfinite(Y), axis=0)
    ok = cnt >= len(runs) - 1
    # Amplitude that survives the stack, and what the scatter alone would give.
    surviving = np.nanstd(avg[ok])
    from_scatter = np.nanmean(scat[ok]) / np.sqrt(np.nanmean(cnt[ok]))
    return 100 * surviving, 100 * from_scatter


def main():
    paths = sorted(glob.glob(os.path.join(HERE, "data", "bandpass_lo_*.h5")),
                   key=lambda p: int(p.rsplit("_", 1)[1].split(".")[0]))
    print("Stacking %d runs. 'survives' is the structure left after averaging;" % len(paths))
    print("'from scatter' is what noise alone would leave. Real structure is the")
    print("excess of the first over the second.\n")
    print("  H I blank    frame       survives   from scatter   excess")
    for blank in (300e3, 500e3, 800e3, 1200e3):
        runs = [load(p, blank) for p in paths]
        sr = runs[0]["sr"]
        half = HALF_BAND * sr
        lo_lo = min(r["lo"] for r in runs)
        lo_hi = max(r["lo"] for r in runs)
        grids = {"baseband": np.linspace(-half, half, 700),
                 "sky": np.linspace(lo_hi - half, lo_lo + half, 700)}
        for frame in ("baseband", "sky"):
            s, n = stack(runs, grids[frame], frame)
            exc = np.sqrt(max(s * s - n * n, 0.0))
            print("  %5.0f kHz    %-10s  %6.3f%%     %6.3f%%     %6.3f%%"
                  % (blank / 1e3, frame, s, n, exc))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
