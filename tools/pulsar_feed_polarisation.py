#!/usr/bin/env python3
"""The feed's polarisation, from PSR B0329+54's profile against parallactic angle.

Each recording is folded in its 60 s sub-integrations (pulsar_fold.analyse_file,
the same fold the timing uses), every minute is given the pulsar's parallactic
angle q, and all of them are fitted at once to

    profile_j = c_j + a_n [ I + v V + x (Q cos 2q_j + U sin 2q_j)
                                   + y (-Q sin 2q_j + U cos 2q_j) ]

with I, Q, U, V the Effelsberg 1410 MHz Stokes profiles (EPN hx97b) smoothed
exactly as the timing template is. c_j is a baseline per minute, a_n an
amplitude per recording (scintillation moves it by 2x), v the feed's circular
response, and (x, y) its linear response: p_L = hypot(x, y), at an angle that
also carries the Faraday rotation and Effelsberg's own reference, so the angle
is not the feed's orientation on the dish. The mean profile alone cannot see
x and y - a night's spread of q averages them away - which is why this exists.

Two things are not known in advance and are reported both ways: the sense in
which the parallactic angle rotates the feed against the sky's Q/U (q or -q),
and the sign convention of the EPN V. A shuffle test - the same fit with the
minutes' parallactic angles permuted within each recording - gives the
distribution of p_L with no signal, since hypot() of noise is biased up.

    cd receiver_scheduler
    python ../tools/pulsar_feed_polarisation.py            # all pulsar recordings

Heavy: about 25 s of folding per 16 h recording, cached afterwards in
data/pulsar_polarisation/. Do not run it while anything is recording.
Written 2026-10-08.
"""
import glob
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RS = os.path.join(HERE, "..", "receiver_scheduler")
sys.path.insert(0, RS)
import observatory  # noqa: E402
import pulsar_fold as PF  # noqa: E402
import pulsar_toa as PT  # noqa: E402

OBS = os.path.join(RS, "data", "observations")
CACHE = os.path.join(RS, "data", "pulsar_polarisation")
SHUFFLES = 200


def pieces(path):
    """(S, C, t_mid) per 60 s sub-integration, cached by the recording's size and mtime."""
    os.makedirs(CACHE, exist_ok=True)
    st = os.stat(path)
    cache = os.path.join(CACHE, os.path.basename(path)[:-3] + ".npz")
    if os.path.exists(cache):
        d = np.load(cache)
        if int(d["size"]) == st.st_size and float(d["mtime"]) == st.st_mtime \
                and float(d["phase_offset"]) == PF.B0329["phase_offset"]:
            return d["S"], d["C"], d["t_mid"]
    r, _attrs = PF.analyse_file(path, toa_bins=PT.TOA_BINS)
    toa = r["toa"]
    t_mid = toa["sub_t0"] + 0.5 * toa["sub_s"]
    np.savez(cache, S=toa["S"], C=toa["C"], t_mid=t_mid, size=st.st_size, mtime=st.st_mtime,
             phase_offset=PF.B0329["phase_offset"])
    return toa["S"], toa["C"], t_mid


def parallactic_deg(t_unix):
    """The pulsar's parallactic angle at the site, degrees, east of north."""
    from astropy.time import Time
    import astropy.units as u
    lat = math.radians(observatory.SITE_LAT_DEG)
    dec = math.radians(PF.B0329["dec_deg"])
    lst = Time(t_unix, format="unix").sidereal_time(
        "apparent", longitude=observatory.SITE_LON_DEG * u.deg).deg
    h = np.radians(lst - PF.B0329["ra_deg"])
    return np.degrees(np.arctan2(np.sin(h) * math.cos(lat),
                                 math.sin(lat) * math.cos(dec) - math.cos(lat) * math.sin(dec) * np.cos(h)))


def templates(n):
    """I, Q, U, V from the EPN file, through the timing template's smoothing,
    normalised to the I peak."""
    P = PF.period_at(1790000000.0)
    d = np.load(PT.TEMPLATE_FILE)
    t_ms = d["t_ms"]
    out = {}
    for k in "IQUV":
        m = n * 8
        ph = (np.arange(m) + 0.5) / m
        src = 0.5 + t_ms / (1e3 * P)
        o = np.argsort(src % 1.0)
        f = np.interp(ph, (src % 1.0)[o], d[k][o], period=1.0)
        for w in (1e-3 / P, PT.dispersion_smear_s(1413e6, 32e6) / P, 1.0 / n):
            f = PT._circular_boxcar(f, w)
        out[k] = f.reshape(n, 8).mean(axis=1)
    peak = out["I"].max()
    return {k: v / peak for k, v in out.items()}


def rotate(x, tau):
    n = len(x)
    k = np.arange(1, n // 2 + 1)
    X = np.fft.rfft(x)
    X[1:] *= np.exp(-2j * np.pi * k * tau)
    return np.fft.irfft(X, n=n)


def fit(R, w, rec, c2, s2, G, RI, RQ, RU, RV, rr, nrec):
    """Linear weighted least squares on inner products.

    Model per minute: r_j = alpha_n I + gV V + gx A_j + gy B_j, an I
    coefficient per recording and the polarisation terms global, then
    (v, x, y) = (gV, gx, gy) / alpha_bar, alpha_bar being the alphas averaged
    with the weight each recording carries in the g's. Returns ((v, x, y),
    their covariance, alphas).

    Linear on purpose. The bilinear a_n (I + v V + x A + y B), fitted jointly,
    is unbiased at high S/N but at this data's ~0.25 per minute it read the
    circular fraction 0.2-0.4 high in three of three injections: V is almost
    I's shape in this pulsar (correlation -0.965 after smoothing), so v and
    the poorly measured a_n trade off. Here the g's are plain linear
    estimates and alpha_bar is measured from all the data at once.
    """
    GVV = G["VV"]
    GAA = G["QQ"] * c2**2 + 2 * G["QU"] * c2 * s2 + G["UU"] * s2**2
    GBB = G["QQ"] * s2**2 - 2 * G["QU"] * c2 * s2 + G["UU"] * c2**2
    GAB = (G["UU"] - G["QQ"]) * c2 * s2 + G["QU"] * (c2**2 - s2**2)
    GVA = G["QV"] * c2 + G["UV"] * s2
    GVB = -G["QV"] * s2 + G["UV"] * c2
    GIA = G["IQ"] * c2 + G["IU"] * s2
    GIB = -G["IQ"] * s2 + G["IU"] * c2
    RA = RQ * c2 + RU * s2
    RB = -RQ * s2 + RU * c2
    k = 3 + nrec
    N = np.zeros((k, k))
    b = np.zeros(k)
    N[:3, :3] = [[np.sum(w * GVV), np.sum(w * GVA), np.sum(w * GVB)],
                 [np.sum(w * GVA), np.sum(w * GAA), np.sum(w * GAB)],
                 [np.sum(w * GVB), np.sum(w * GAB), np.sum(w * GBB)]]
    b[:3] = [np.sum(w * RV), np.sum(w * RA), np.sum(w * RB)]
    W = np.zeros(nrec)
    for n in range(nrec):
        m = rec == n
        W[n] = np.sum(w[m])
        N[3 + n, 3 + n] = G["II"] * W[n]
        N[0, 3 + n] = N[3 + n, 0] = G["IV"] * W[n]
        N[1, 3 + n] = N[3 + n, 1] = np.sum(w[m] * GIA[m])
        N[2, 3 + n] = N[3 + n, 2] = np.sum(w[m] * GIB[m])
        b[3 + n] = np.sum(w[m] * RI[m])
    cov = np.linalg.inv(N)
    sol = cov @ b
    alpha = sol[3:]
    abar = float(np.sum(W * alpha) / np.sum(W))
    return sol[:3] / abar, cov[:3, :3] / abar**2, alpha


def main():
    paths = sorted(glob.glob(os.path.join(OBS, "*_pulsar.h5")))
    n = PT.TOA_BINS
    T = templates(n)
    Ss, Cs, ts, recs = [], [], [], []
    for i, p in enumerate(paths):
        try:
            S, C, t = pieces(p)
        except Exception as exc:                          # noqa: BLE001
            print("skip %s: %s" % (os.path.basename(p), exc))
            continue
        keep = C.sum(axis=1) > 0
        Ss.append(S[keep]); Cs.append(C[keep]); ts.append(t[keep]); recs.append(np.full(keep.sum(), len(recs)))
        print("%s: %d minutes" % (os.path.basename(p), keep.sum()))
    S = np.vstack(Ss); C = np.vstack(Cs); t = np.concatenate(ts); rec = np.concatenate(recs)
    nrec = len(Ss)
    prof = np.where(C > 0, S / np.maximum(C, 1e-300), 0.0)
    total = S.sum(axis=0) / np.maximum(C.sum(axis=0), 1e-300)
    tau = PT.fit_shift(total, T["I"] - np.median(T["I"]))["tau"]
    T = {k: rotate(v, tau) for k, v in T.items()}
    Tt = {k: v - v.mean() for k, v in T.items()}          # the baseline per minute is projected out
    G = {a + b: float(Tt[a] @ Tt[b]) for a in "IQUV" for b in "IQUV"}
    R = prof - prof.mean(axis=1, keepdims=True)
    w = C.mean(axis=1)                                     # weight per minute (1/sigma^2 per bin)
    RI, RQ, RU, RV = (R @ Tt[k] for k in "IQUV")
    rr = np.sum(R * R, axis=1)
    off = T["I"] < 0.01
    # noise scale from off-pulse bins: w * var should be 1 if the weights are exact
    scale = float(np.sqrt(np.median(w * np.var(R[:, off], axis=1))))
    q = np.radians(parallactic_deg(t))
    print("\n%d minutes (%.1f h) in %d recordings; parallactic angle %.0f to %.0f deg; "
          "noise scale %.2f" % (len(t), len(t) / 60, nrec, np.degrees(q.min()), np.degrees(q.max()), scale))
    rng = np.random.default_rng(1)
    for sense in (+1, -1):
        c2, s2 = np.cos(2 * sense * q), np.sin(2 * sense * q)
        par, cov, a = fit(R, w, rec, c2, s2, G, RI, RQ, RU, RV, rr, nrec)
        err = np.sqrt(np.diag(cov)) * scale
        v, x, y = par
        pl = math.hypot(x, y)
        null = []
        for _ in range(SHUFFLES):
            perm = np.empty_like(q)
            for nn in range(nrec):
                idx = np.where(rec == nn)[0]
                perm[idx] = q[rng.permutation(idx)]
            pp, _, _ = fit(R, w, rec, np.cos(2 * sense * perm), np.sin(2 * sense * perm), G,
                           RI, RQ, RU, RV, rr, nrec)
            null.append(math.hypot(pp[1], pp[2]))
        null = np.array(null)
        print("sense %+d: v = %+.3f +- %.3f   x = %+.3f +- %.3f   y = %+.3f +- %.3f   "
              "p_L = %.3f at %.0f deg; shuffled p_L median %.3f, %d of %d at or above"
              % (sense, v, err[0], x, err[1], y, err[2], pl, 0.5 * math.degrees(math.atan2(y, x)),
                 np.median(null), int(np.sum(null >= pl)), SHUFFLES))


if __name__ == "__main__":
    main()
