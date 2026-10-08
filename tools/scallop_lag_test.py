#!/usr/bin/env python3
"""Test an analytic tracking-scallop model against scallop.correct.

Model, per record:
    v = trend(t) * (1 - k_alt (e_alt + d_alt(t))^2 - k_az (e_az + d_az(t))^2)
where e is commanded minus demanded drive position (sky degrees), with the
Due's steps taken `lag` degrees late in the direction of motion, k is the dip
depth per axis (free, reported against the beam's), and d is a pointing
offset per axis interpolated between hourly knots. Linear once the lags are
fixed, so the lags are a grid search.

Written 2026-10-07 (issue: analytic scallop). On nine Sun tracks before the
controller sent on every pulse change the azimuth lag fitted 0.02-0.05 deg
and the model beat the existing correction on 5 of 9 runs. Run it on tracks
made after 2026-10-07 16:00 BST, when the lag should be ~0.

    cd receiver_scheduler
    python ../tools/scallop_lag_test.py 20261010_120000_track.h5 ...

Reads the values exactly as the solar plot makes them, and never writes the
carried scallop parameters (update_reference=False). Do not run it while a
recording is in progress (CLAUDE.md: no heavy work during recordings).
"""
import math
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "receiver_scheduler"))
import observation_plot as op  # noqa: E402
import scallop  # noqa: E402

OBS = os.path.join(HERE, "..", "receiver_scheduler", "data", "observations")
KNOT_S = 3600.0          # pointing offsets every hour, linearly interpolated
MIN_E_STD = 0.08         # a knot needs this much step error spread to measure an offset
LAGS = np.arange(0.0, 0.105, 0.01)


def capture(path):
    """(values, stamps, header): the per-record source values the solar plot
    hands to scallop.correct, captured without running it."""
    cap = {}
    real = scallop.correct

    def grab(values, stamps, header, *a, **k):
        cap.update(v=np.array(values, float), t=np.array(stamps, float), h=dict(header))
        return np.array(values, float), {}
    scallop.correct = grab
    try:
        with tempfile.TemporaryDirectory() as tmp:
            op.plot_observation(path, os.path.join(tmp, "x.png"), mode="solar")
    finally:
        scallop.correct = real
    return cap["v"], cap["t"], cap["h"]


def hats(t, knots):
    H = np.zeros((len(t), len(knots)))
    for j in range(len(knots)):
        y = np.zeros(len(knots))
        y[j] = 1.0
        H[:, j] = np.interp(t, knots, y) if len(knots) > 1 else 1.0
    return H


def measurable(t, e, knots):
    H = hats(t, knots)
    keep = []
    for j, kt in enumerate(knots):
        w = H[:, j]
        if w.sum() < 50:
            continue
        m = np.average(e, weights=w)
        if math.sqrt(np.average((e - m) ** 2, weights=w)) >= MIN_E_STD:
            keep.append(kt)
    return np.array(keep)


def lagged_error(d, t, lag):
    """Commanded minus demanded, with each step `lag` deg late in the direction of motion."""
    s = np.sign(np.gradient(d, t))
    return np.round((d - lag * s) / scallop.PULSE_DEG) * scallop.PULSE_DEG - d


def model(v, t, h, terms):
    d_alt, d_az, t_alt = scallop.drive_demand(h, t, terms)
    good = np.isfinite(v) & scallop.quiet_mask(v, t)
    y = v / np.nanmedian(v[good])
    T = scallop._trend_basis(t, int(np.clip(round(2 * (t.max() - t.min()) / 3600), 2, 10)))
    knots = np.linspace(t.min(), t.max(), max(1, int(round((t.max() - t.min()) / KNOT_S))) + 1)
    best = None
    for la in LAGS:
        ea = lagged_error(d_alt, t, la)
        for lz in LAGS:
            ez = lagged_error(d_az, t, lz) * np.cos(np.radians(t_alt))
            ka, kz = measurable(t[good], ea[good], knots), measurable(t[good], ez[good], knots)
            Ha = hats(t, ka) if len(ka) else np.zeros((len(t), 0))
            Hz = hats(t, kz) if len(kz) else np.zeros((len(t), 0))
            A = np.column_stack([T, -ea ** 2, -ez ** 2, -ea[:, None] * Ha, -ez[:, None] * Hz])
            c, *_ = np.linalg.lstsq(A[good], y[good], rcond=None)
            chi = float(np.sum((y[good] - A[good] @ c) ** 2))
            if best is None or chi < best["chi"]:
                best = dict(chi=chi, lag_alt=la, lag_az=lz, c=c, ka=ka, kz=kz, ea=ea, ez=ez, nt=T.shape[1])
    b = best
    nt, c = b["nt"], b["c"]
    k_a, k_z = c[nt], c[nt + 1]
    off_a = c[nt + 2: nt + 2 + len(b["ka"])] / (2 * k_a)
    off_z = c[nt + 2 + len(b["ka"]):] / (2 * k_z)
    da = np.interp(t, b["ka"], off_a) if len(b["ka"]) else 0 * t
    dz = np.interp(t, b["kz"], off_z) if len(b["kz"]) else 0 * t
    g = np.clip(1 - k_a * (b["ea"] + da) ** 2 - k_z * (b["ez"] + dz) ** 2, 0.9, 1.0)
    return dict(b, k_alt=k_a, k_az=k_z, off_alt=off_a, off_az=off_z, gain=g)


def main(names):
    for name in names:
        path = name if os.path.isabs(name) else os.path.join(OBS, name)
        v, t, h = capture(path)
        terms, _ = scallop.pointing_terms(h)
        k_beam = 4 * math.log(2) / scallop.beam_fwhm(h) ** 2
        r = model(v, t, h, terms)
        quiet = scallop.quiet_mask(v, t)
        old, rep = scallop.correct(v, t, h, terms=terms, update_reference=False)
        m = {"none": scallop.residual_modulation(v, t, h, terms, None, quiet),
             "current": scallop.residual_modulation(old, t, h, terms, None, quiet),
             "model": scallop.residual_modulation(v / r["gain"], t, h, terms, None, quiet)}
        print("%s  %d records  lag alt %.2f az %.2f deg | dip alt %.2fx az %.2fx the beam | "
              "offsets alt %s, az %s | modulation left: none %.2f%%, current (%s) %.2f%%, model %.2f%%"
              % (os.path.basename(path), len(v), r["lag_alt"], r["lag_az"], r["k_alt"] / k_beam,
                 r["k_az"] / k_beam, " ".join("%+.2f" % x for x in r["off_alt"]) or "-",
                 " ".join("%+.2f" % x for x in r["off_az"]) or "-", m["none"], rep.get("chosen"),
                 m["current"], m["model"]))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
