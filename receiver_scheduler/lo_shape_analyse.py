#!/usr/bin/env python3
"""Decide whether the bandpass shape is fixed to the LO or fixed to the sky.

Each run is reduced to a mean spectrum and compared against the others twice:
once against baseband frequency (offset from that run's own LO) and once
against sky frequency. A shape that is a property of the receiver's filters
overlays in the first frame; a shape imposed by something ahead of the mixer -
the RF filter, a reflection - overlays in the second. Whichever frame the runs
agree in is the frame the shape lives in, and only the first one cancels under
frequency switching.

The eight runs are four LO settings visited up and then back down, so for every
setting the two visits straddle the midpoint of the sequence symmetrically.
Averaging a setting's two visits therefore cancels any drift that is linear in
time, at every setting equally, which is what makes the cross-LO comparison
below a statement about the LO rather than about the clock.

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

HERE = os.path.dirname(os.path.abspath(__file__))
H1 = 1420.405752e6
DC_BLANK_HZ = 25e3        # the LO artefact, at baseband zero in every run
H1_BLANK_HZ = 300e3       # generous: real emission, wherever it lands
HALF_BAND = 0.25          # fraction of the sample rate used, either side


def load(path):
    with h5py.File(path, "r") as hf:
        f = np.array(hf["frequency_hz"][:], float)
        sp = np.array(hf["spectra_linear"][:], float)
        a = dict(hf.attrs)
    lo, sr = a["center_freq_hz"], a["sample_rate_hz"]
    mean = sp.mean(axis=0)
    # Per-channel noise on the mean, from record-to-record scatter.
    each = sp / np.median(sp, axis=1, keepdims=True)
    noise = each.std(axis=0, ddof=1).mean() / np.sqrt(sp.shape[0])
    bad = np.zeros(f.size, bool)
    bad |= np.abs(f - lo) < DC_BLANK_HZ
    bad |= np.abs(f - H1) < H1_BLANK_HZ
    mean = np.where(bad, np.nan, mean)
    return dict(f=f, bb=f - lo, mean=mean / np.nanmedian(mean),
                lo=lo, sr=sr, n=sp.shape[0], noise=noise,
                name=os.path.basename(path))


def on_grid(run, grid, frame):
    x = run["bb"] if frame == "baseband" else run["f"]
    ok = np.isfinite(run["mean"])
    y = np.interp(grid, x[ok], run["mean"][ok])
    return y / np.median(y)


def compare(a, b, grid, frame):
    ya, yb = on_grid(a, grid, frame), on_grid(b, grid, frame)
    r = ya / yb
    u = np.linspace(-1, 1, r.size)
    res = r - np.polyval(np.polyfit(u, r, 3), u)
    return 100 * res.std()


def main():
    paths = sorted(glob.glob(os.path.join(HERE, "data", "bandpass_lo_*.h5")),
                   key=lambda p: int(p.rsplit("_", 1)[1].split(".")[0]))
    runs = [load(p) for p in paths]
    sr = runs[0]["sr"]
    print("%d runs, %.2f Msps, %.1f Hz per channel\n"
          % (len(runs), sr / 1e6, sr / runs[0]["f"].size))
    for r in runs:
        print("   %-24s LO %.6f MHz  %2d records  noise %.4f%%"
              % (r["name"], r["lo"] / 1e6, r["n"], 100 * r["noise"]))

    by_lo = {}
    for r in runs:
        by_lo.setdefault(round(r["lo"] - H1), []).append(r)

    # Baseband grid common to every run; sky grid over the overlap of all bands.
    half = HALF_BAND * sr
    bb_grid = np.linspace(-half, half, 600)
    lo_lo, lo_hi = min(r["lo"] for r in runs), max(r["lo"] for r in runs)
    sky_grid = np.linspace(lo_hi - half, lo_lo + half, 600)
    print("\nbaseband grid +-%.2f MHz;  sky grid %.3f - %.3f MHz (%d points)"
          % (half / 1e6, sky_grid[0] / 1e6, sky_grid[-1] / 1e6, sky_grid.size))

    print("\n=== repeatability: same LO, the two visits ~10 min apart ===")
    print("    (this is the floor - noise plus whatever drifts in time)\n")
    floor = []
    for off in sorted(by_lo):
        pair = by_lo[off]
        if len(pair) != 2:
            continue
        bbv = compare(pair[0], pair[1], bb_grid, "baseband")
        exp = 100 * np.hypot(pair[0]["noise"], pair[1]["noise"]) \
            / np.sqrt(pair[0]["f"].size / bb_grid.size)
        floor.append(bbv)
        print("   offset %.2f MHz   rms %.3f%%   (noise alone %.3f%%)"
              % (off / 1e6, bbv, exp))
    print("\n   mean repeatability floor: %.3f%%" % np.mean(floor))

    # Drift-cancelled shape per LO setting: average the two symmetric visits.
    merged = []
    for off in sorted(by_lo):
        pair = by_lo[off]
        m = dict(pair[0])
        m["mean"] = np.nanmean(np.vstack([p["mean"] for p in pair]), axis=0)
        m["noise"] = np.mean([p["noise"] for p in pair]) / np.sqrt(2)
        m["label"] = "%.2f MHz" % (off / 1e6)
        merged.append(m)

    for frame, grid in (("baseband", bb_grid), ("sky", sky_grid)):
        print("\n=== cross-LO agreement, compared in %s frequency ===" % frame)
        vals = []
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                v = compare(merged[i], merged[j], grid, frame)
                d = abs(merged[i]["lo"] - merged[j]["lo"]) / 1e6
                vals.append(v)
                print("   %s vs %s  (LO differs %.2f MHz)   rms %.3f%%"
                      % (merged[i]["label"], merged[j]["label"], d, v))
        print("   mean: %.3f%%" % np.mean(vals))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
