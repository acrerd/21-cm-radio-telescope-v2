"""H I pointing: the pointing model from the H I sky instead of the Sun.

Near the winter solstice the Sun barely clears the trees, and even in summer it
never rises above 58 deg, so the terms that grow with elevation have always
been poorly constrained. The Galactic plane is up somewhere at every hour, at
every elevation, and its H I changes fast enough with position - in brightness
and in velocity - that a spectrum says where the beam was. With the Thunderbolt
locked the frequency axis is exact, so velocity structure is a measure of
position rather than a shift to be fitted (clock-ppm-is-pointing-on-plane-fields).

A session is one ordinary tracked observation (`hi_pointing: True` on the
entry): one receiver run, so no start-up drift between fields, while the
scheduler's sequencer steps the dish through a planned list of fields. Each
field is reduced on its own: its mean spectrum against the HI4PI model through
the beam, over a grid of pointing offsets, gain and T_sys free. The result is a
pointing record of the kind a Sun scan makes - the true direction the H I says
the beam was at (T) and the drive position the controller reported (D) - so
`sun_scan.fit_pointing_model` takes them as they are.

What was learned before building it (2026-10-27):
- On 16 archived spectra taken while the Sun model was known good, single
  pointings on strong plane fields scattered by 0.3-0.5 deg in altitude and
  0.2-0.3 in cross-elevation; fields with weak or one-directional structure
  (the anticentre, high latitudes) failed, the fit trading T_sys for position.
  So fields are chosen by the information the model predicts in BOTH
  directions (`information`), not by brightness.
- A cross on l=72 b=0 (2 deg arms, one recording) agreed with its own single
  pointings to ~0.1 deg, T_sys a steady 197 K in every fit: on a strong field
  the single pointing is enough, so a session is single pointings.
- Holding T_sys to an expected value with a prior made things worse: the
  residuals are dominated by smooth model mismatch, not noise, so a per-bin
  likelihood is far too confident. T_sys is free; information keeps it honest.
- Archived spectra were reduced with the bandpass template in force weeks
  later, which may be part of their scatter; a session is reduced with the
  template it was recorded under.

Every number here is a starting point; judge the method by leave-one-out
prediction across a session's fields (`leave_one_out`), as the Sun model is.
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import observatory  # noqa: E402  (puts astro_simulator/ on the path)

OUT_DIR = os.path.join(HERE, "data", "hi_pointing")

# A field: three minutes on it, the first SETTLE_S after arrival dropped (the
# records straddle the slew and the first tracking steps).
FIELD_DWELL_S = 180.0
SETTLE_S = 12.0
# The first field is the recording's first minutes, and a fresh receiver
# start reads 0.3-1.7% high and settles over minutes (receiver-start-settle).
# It gets this much more, dropped, before its own dwell: the 2026-10-27
# session measured its first field from 12 s in and read +0.25 deg there
# against +0.70 on the same field at the end, where the cross of the same
# afternoon, which waited 5 min, agreed with itself to 0.04 deg.
FIRST_FIELD_SETTLE_S = 300.0
INTEGRATION_S = 3.0
# Where fields are taken. Not below 25 deg (atmosphere and ground spillover
# change fastest there, and the trees), not above 80 (azimuth runs fast near
# the zenith and the cross-elevation axis turns with it).
MIN_ALT_DEG = 25.0
MAX_ALT_DEG = 80.0
# Clearance asked of the measured horizon: the full beam plus a degree, at
# both ends of the dwell - trees are a 290 K source, not a screen.
HORIZON_MARGIN_EXTRA_DEG = 1.0
# Candidates: the plane and 4 deg either side, every 5 deg of longitude.
CANDIDATE_L_STEP = 5.0
CANDIDATE_B = (-4.0, 0.0, 4.0)
# A field is usable when the model predicts both offsets to this or better
# for 1 K of residual per channel (l=72 b=0: 0.03; anticentre cross-elevation
# 0.26; the Lockman Hole 3-4). Measured residuals run 0.7-2 K, mostly model.
MAX_PREDICTED_SIGMA_DEG = 0.08
# Fields at least this far apart in galactic coordinates.
MIN_FIELD_SEPARATION_DEG = 10.0
# Slew estimate for planning: the axes' speeds (2026-08-21) plus a settle.
SLEW_ALT_DEG_S = 3.9
SLEW_AZ_DEG_S = 2.9
SLEW_SETTLE_S = 15.0
# The first field again at the end: the same direction an hour apart is the
# session's own check on drift and repeatability.
REPEAT_FIRST = True

# The offset search: coarse, then fine about the best.
COARSE_SPAN_DEG = 3.0
COARSE_STEP_DEG = 0.5
FINE_SPAN_DEG = 0.5
FINE_STEP_DEG = 0.1
ERROR_FLOOR_DEG = 0.05
# A field's result is used only if the fit is clean.
MIN_CORRELATION = 0.99
T_SYS_RANGE_K = (120.0, 350.0)
MAX_ERROR_DEG = 0.3
MIN_RECORDS = 20


def _site():
    from astropy.coordinates import EarthLocation
    import astropy.units as u
    return EarthLocation(lat=observatory.SITE_LAT_DEG * u.deg, lon=observatory.SITE_LON_DEG * u.deg,
                         height=observatory.SITE_HEIGHT_M * u.m)


def altaz_of(l_deg, b_deg, when):
    """(alt, az) arrays, geometric (the controller's true frame), of galactic
    positions at a time (datetime, UTC)."""
    from astropy.coordinates import SkyCoord, AltAz
    from astropy.time import Time
    import astropy.units as u
    c = SkyCoord(l=np.atleast_1d(l_deg) * u.deg, b=np.atleast_1d(b_deg) * u.deg,
                 frame="galactic").transform_to(AltAz(obstime=Time(when), location=_site()))
    return c.alt.deg, c.az.deg


def galactic_of(alt_deg, az_deg, when):
    """(l, b) arrays of geometric alt/az positions at a time."""
    from astropy.coordinates import SkyCoord, AltAz
    from astropy.time import Time
    import astropy.units as u
    frame = AltAz(obstime=Time(when), location=_site())
    g = SkyCoord(alt=np.atleast_1d(alt_deg) * u.deg, az=np.atleast_1d(az_deg) * u.deg, frame=frame).galactic
    return g.l.deg, g.b.deg


def offset_direction(alt0, az0, dalt, dxel):
    """The direction dalt up and dxel across (cross-elevation, degrees on the
    sky) from (alt0, az0)."""
    alt = alt0 + dalt
    return alt, az0 + dxel / math.cos(math.radians(alt))


# --------------------------------------------------------------------------
# planning


def information(l_deg, b_deg, when, sim, h=0.5):
    """Predicted 1-sigma (dalt, dxel) for 1 K of residual per model channel.

    The model's derivative with respect to each offset, with the two nuisance
    terms (gain times the spectrum, gain times T_sys) projected out: what is
    left is the change a pointing error makes that a gain or T_sys change
    cannot imitate. Small in both directions is a field worth taking.
    """
    import rf_calibration as R
    alt0, az0 = (float(v[0]) for v in altaz_of(l_deg, b_deg, when))
    offs = ((0.0, 0.0), (h, 0.0), (-h, 0.0), (0.0, h), (0.0, -h))
    dirs = [offset_direction(alt0, az0, a, x) for a, x in offs]
    gl, gb = galactic_of([d[0] for d in dirs], [d[1] for d in dirs], when)
    grid = None
    spectra = []
    for l1, b1 in zip(gl, gb):
        f, ta = R.simulated_spectrum(float(l1), float(b1), when, sim)
        if grid is None:
            grid = f
        spectra.append(np.interp(grid, f, ta))
    s = np.array(spectra)
    ja = (s[1] - s[2]) / (2 * h)
    jx = (s[3] - s[4]) / (2 * h)
    nuis = np.vstack([s[0], np.ones_like(s[0])]).T
    proj = np.eye(len(s[0])) - nuis @ np.linalg.pinv(nuis)
    j = np.vstack([proj @ ja, proj @ jx]).T
    try:
        cov = np.linalg.inv(j.T @ j)
    except np.linalg.LinAlgError:
        return float("inf"), float("inf"), float(s[0].max())
    return float(math.sqrt(max(cov[0, 0], 0))), float(math.sqrt(max(cov[1, 1], 0))), float(s[0].max())


def _slew_s(a, b):
    dalt = abs(a[0] - b[0])
    daz = abs((a[1] - b[1] + 180.0) % 360.0 - 180.0)
    return max(dalt / SLEW_ALT_DEG_S, daz / SLEW_AZ_DEG_S) + SLEW_SETTLE_S


def _clear(alt, az, profile, margin):
    import horizon_store
    if not (MIN_ALT_DEG <= alt <= MAX_ALT_DEG):
        return False
    if profile and horizon_store.profile_floors(profile):
        return alt >= horizon_store.horizon_floor(profile, az) + margin
    return True


def _sep_deg(l1, b1, l2, b2):
    l1, b1, l2, b2 = (math.radians(v) for v in (l1, b1, l2, b2))
    c = math.sin(b1) * math.sin(b2) + math.cos(b1) * math.cos(b2) * math.cos(l1 - l2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def plan_session(start, minutes, profile=None, sim=None, dwell_s=FIELD_DWELL_S, max_fields=None):
    """The fields for a session starting at `start` (datetime, UTC) and
    lasting `minutes`, in the order they are taken: a list of dicts with l,
    b, the planned start offset in seconds, alt/az there and the predicted
    sigmas. Greedy: the field with most information first, then at each slot
    the usable, visible field farthest across the sky from those already
    chosen, so a session spreads over altitude and azimuth. The first field
    is repeated at the end when there is time."""
    import horizon_store
    import rf_calibration as R
    sim = sim or R.load_simulator()
    margin = horizon_store.beam_margin_deg() + HORIZON_MARGIN_EXTRA_DEG
    ls = np.arange(0.0, 360.0, CANDIDATE_L_STEP)
    cand = [(float(l), float(b)) for l in ls for b in CANDIDATE_B]
    alt, az = altaz_of([c[0] for c in cand], [c[1] for c in cand], start)
    # information once, at the start, for the fields that are up at all
    usable = []
    for (l, b), a, z in zip(cand, alt, az):
        if not (MIN_ALT_DEG - 15.0 <= a <= MAX_ALT_DEG + 10.0):
            continue
        sa, sx, peak = information(l, b, start, sim)
        if max(sa, sx) <= MAX_PREDICTED_SIGMA_DEG:
            usable.append({"l": l, "b": b, "sigma_alt": sa, "sigma_xel": sx, "peak_k": peak})
    total_s = float(minutes) * 60.0
    plan = []
    t = 0.0
    last_dir = None
    while True:
        if max_fields is not None and len(plan) >= max_fields:
            break
        span = dwell_s + (SETTLE_S if plan else FIRST_FIELD_SETTLE_S)
        when0 = start + timedelta(seconds=t)
        when1 = start + timedelta(seconds=t + span)
        pool = [u for u in usable if all(_sep_deg(u["l"], u["b"], p["l"], p["b"]) >= MIN_FIELD_SEPARATION_DEG
                                         for p in plan)]
        if not pool:
            break
        a0, z0 = altaz_of([u["l"] for u in pool], [u["b"] for u in pool], when0)
        a1, z1 = altaz_of([u["l"] for u in pool], [u["b"] for u in pool], when1)
        ok = [i for i in range(len(pool))
              if _clear(a0[i], z0[i], profile, margin) and _clear(a1[i], z1[i], profile, margin)]
        if not ok:
            break
        if not plan:
            # the best-determined field that is still up at the end, since it
            # is taken again then; any best field if none stays up
            end = start + timedelta(seconds=total_s)
            ae, ze = altaz_of([pool[i]["l"] for i in ok], [pool[i]["b"] for i in ok], end)
            stays = {i for i, a, z in zip(ok, ae, ze) if _clear(a, z, profile, margin)}
            pick = min(ok, key=lambda i: (i not in stays, max(pool[i]["sigma_alt"], pool[i]["sigma_xel"]),
                                          -a0[i]))
        else:
            def spread(i):
                return min(_coverage_sep(a0[i], z0[i], p["alt"], p["az"]) for p in plan)
            pick = max(ok, key=spread)
        here = (float(a0[pick]), float(z0[pick]))
        slew = _slew_s(last_dir, here) if last_dir else SLEW_SETTLE_S
        # room kept for the first field again at the end, once there is one
        reserve = (dwell_s + _slew_s((0.0, 0.0), (MAX_ALT_DEG - MIN_ALT_DEG, 180.0))) if (REPEAT_FIRST and plan) else 0.0
        settle = FIRST_FIELD_SETTLE_S if not plan else SETTLE_S
        if t + slew + settle + dwell_s + reserve > total_s:
            break
        t += slew
        f = dict(pool[pick], alt=here[0], az=here[1], start_s=round(t, 1),
                 dwell_s=dwell_s + settle, settle_s=settle)
        plan.append(f)
        t += f["dwell_s"]
        last_dir = here
    if REPEAT_FIRST and len(plan) >= 2:
        first = plan[0]
        when = start + timedelta(seconds=t)
        a, z = (float(v[0]) for v in altaz_of(first["l"], first["b"], when))
        slew = _slew_s(last_dir, (a, z))
        if (t + slew + dwell_s + SETTLE_S <= total_s and _clear(a, z, profile, margin)
                and _clear(*(float(v[0]) for v in altaz_of(first["l"], first["b"],
                                                           when + timedelta(seconds=dwell_s + SETTLE_S))),
                           profile, margin)):
            plan.append(dict(first, alt=a, az=z, start_s=round(t + slew, 1), dwell_s=dwell_s + SETTLE_S,
                             settle_s=SETTLE_S, repeat=True))
    return plan


# How far apart two fields are for the model's purposes: altitude for the
# terms that go with tan/sec(alt), azimuth for the tilt, which moves the
# altitude as cos/sin(az) at every elevation. Distance on the sky undervalues
# azimuth near the zenith, where every azimuth is close, and a plan spread by
# it left the west-south-west empty (2026-10-27). Azimuth is scaled down by
# this so the two ranges (360 and ~55 deg) weigh comparably.
AZ_SPREAD_SCALE = 3.0


def _coverage_sep(alt1, az1, alt2, az2):
    daz = abs((az1 - az2 + 180.0) % 360.0 - 180.0)
    return math.hypot(daz / AZ_SPREAD_SCALE, alt1 - alt2)


def _sky_sep(alt1, az1, alt2, az2):
    a1, z1, a2, z2 = (math.radians(v) for v in (alt1, az1, alt2, az2))
    c = math.sin(a1) * math.sin(a2) + math.cos(a1) * math.cos(a2) * math.cos(z1 - z2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


# --------------------------------------------------------------------------
# one field


def _binned(sim_f, freq_hz, mean_counts, art):
    import rf_calibration as R
    mid = 0.5 * (sim_f[1:] + sim_f[:-1])
    edges = np.concatenate([[sim_f[0] - (mid[0] - sim_f[0])], mid, [sim_f[-1] + (sim_f[-1] - mid[-1])]])
    binned, count = R._bin_to(edges, freq_hz, mean_counts)
    use = (count > 0) & np.isfinite(binned)
    if art is not None:
        use &= np.abs(sim_f - float(art)) > R.DC_EXCLUSION_HZ
    return binned, use


def fit_at(freq_hz, mean_counts, when, l, b, sim, bandwidth_hz, art):
    """(residual rms K, correlation, T_sys K, gain, bins) for the model at
    (l, b) against one mean spectrum, gain and T_sys free."""
    import rf_calibration as R
    sim_f, sim_ta = R.simulated_spectrum(l, b, when, sim, bandwidth_hz)
    binned, use = _binned(sim_f, freq_hz, mean_counts, art)
    use &= np.isfinite(sim_ta)
    c, ta = binned[use], sim_ta[use]
    if len(c) < 20:
        raise ValueError("only %d usable bins" % len(c))
    a = np.vstack([ta, np.ones_like(ta)]).T              # c = G T_A + G T_sys
    (g, gt), *_ = np.linalg.lstsq(a, c, rcond=None)
    resid_k = (c - (g * ta + gt)) / g
    return (float(np.sqrt(np.mean(resid_k ** 2))), float(np.corrcoef(ta, c)[0, 1]),
            float(gt / g), float(g), int(len(c)))


def fit_offset(freq_hz, mean_counts, when, l0, b0, sim, bandwidth_hz, art=None,
               coarse_span=COARSE_SPAN_DEG):
    """Where the beam was, relative to (l0, b0), from one mean spectrum: dict
    with dalt, dxel (degrees on the sky), their curvature errors, and the
    fit at the best and at zero offset. Coarse grid, fine grid about the
    best, paraboloid through the fine grid's chi-squared. The curvature error
    treats the residual as noise and so is a floor; analyse_field adds the
    disagreement between the halves of the dwell."""
    alt0, az0 = (float(v[0]) for v in altaz_of(l0, b0, when))

    def at(da, dx):
        a, z = offset_direction(alt0, az0, da, dx)
        gl, gb = galactic_of(a, z, when)
        return fit_at(freq_hz, mean_counts, when, float(gl[0]), float(gb[0]), sim, bandwidth_hz, art)

    steps = np.arange(-coarse_span, coarse_span + 1e-6, COARSE_STEP_DEG)
    coarse = {(round(a, 3), round(x, 3)): at(a, x) for a in steps for x in steps}
    (ca, cx), _ = min(coarse.items(), key=lambda kv: kv[1][0])
    fsteps = np.arange(-FINE_SPAN_DEG, FINE_SPAN_DEG + 1e-6, FINE_STEP_DEG)
    fine = {(round(ca + a, 3), round(cx + x, 3)): at(ca + a, cx + x) for a in fsteps for x in fsteps}
    (ba, bx), best = min(fine.items(), key=lambda kv: kv[1][0])
    rmin, n = best[0], best[4]
    rows, y = [], []
    for (a, x), v in fine.items():
        rows.append([1, a, x, a * a, x * x, a * x])
        y.append(n * v[0] ** 2 / rmin ** 2)
    c, *_ = np.linalg.lstsq(np.array(rows), np.array(y), rcond=None)
    hess = np.array([[2 * c[3], c[5]], [c[5], 2 * c[4]]])
    pa, px, ea, ex = ba, bx, float("nan"), float("nan")
    try:
        if np.all(np.linalg.eigvalsh(hess) > 0):
            cov = 2 * np.linalg.inv(hess)
            sol = np.linalg.solve(hess, -np.array([c[1], c[2]]))
            # the paraboloid's minimum only if it lies inside the fine grid
            if abs(sol[0] - ca) <= FINE_SPAN_DEG and abs(sol[1] - cx) <= FINE_SPAN_DEG:
                pa, px = float(sol[0]), float(sol[1])
            ea, ex = float(math.sqrt(cov[0, 0])), float(math.sqrt(cov[1, 1]))
    except np.linalg.LinAlgError:
        pass
    zero = coarse.get((0.0, 0.0)) or at(0.0, 0.0)
    return {"dalt": pa, "dxel": px, "err_alt_curv": ea, "err_xel_curv": ex,
            "at_edge": max(abs(ca), abs(cx)) >= coarse_span - 1e-6,
            "rms_k": rmin, "correlation": best[1], "t_sys_k": best[2], "gain": best[3], "bins": n,
            "rms_zero_k": zero[0], "t_sys_zero_k": zero[2], "alt0": alt0, "az0": az0}


# --------------------------------------------------------------------------
# a session


def steps_path(recording):
    return os.path.join(OUT_DIR, os.path.splitext(os.path.basename(recording))[0] + ".steps.json")


def result_path(recording):
    return os.path.join(OUT_DIR, os.path.splitext(os.path.basename(recording))[0] + ".json")


def _mean_spectrum(freq_hz, corrected, sel):
    import rf_calibration as R
    with np.errstate(invalid="ignore"):
        keep = np.isfinite(corrected[sel]).any(axis=0)
    mean = np.full(freq_hz.shape, np.nan)
    if keep.any():
        mean[keep] = np.nanmean(corrected[sel][:, keep], axis=0)
    rfi, _ = R.flag_narrow_rfi(freq_hz, mean)
    return np.where(rfi, np.nan, mean)


def _wrap(d):
    return (d + 180.0) % 360.0 - 180.0


def analyse_field(step, freq_hz, corrected, starts, ends, sim, bandwidth_hz, art):
    """One field of a session: the offset from the whole dwell and from each
    half, and the pointing record (T where the H I says the beam was, D the
    mean drive position the controller reported over the same records)."""
    t0 = step["arrived_unix"] + float(step.get("settle_s", SETTLE_S))
    t1 = step["left_unix"]
    sel = (starts >= t0) & (ends <= t1)
    n = int(sel.sum())
    out = {"l": step["l"], "b": step["b"], "repeat": bool(step.get("repeat")), "records": n}
    if n < MIN_RECORDS:
        out.update(ok=False, why="only %d records" % n)
        return out
    mid = float(np.mean((starts[sel] + ends[sel]) / 2))
    when = datetime.fromtimestamp(mid, tz=timezone.utc)
    whole = fit_offset(freq_hz, _mean_spectrum(freq_hz, corrected, sel), when, step["l"], step["b"],
                       sim, bandwidth_hz, art)
    idx = np.where(sel)[0]
    halves = []
    for part in (idx[: len(idx) // 2], idx[len(idx) // 2:]):
        s = np.zeros_like(sel)
        s[part] = True
        hw = datetime.fromtimestamp(float(np.mean((starts[s] + ends[s]) / 2)), tz=timezone.utc)
        halves.append(fit_offset(freq_hz, _mean_spectrum(freq_hz, corrected, s), hw, step["l"], step["b"],
                                 sim, bandwidth_hz, art))
    da_h = halves[0]["dalt"] - halves[1]["dalt"]
    dx_h = halves[0]["dxel"] - halves[1]["dxel"]
    err_alt = math.sqrt(np.nan_to_num(whole["err_alt_curv"]) ** 2 + (da_h / 2) ** 2)
    err_xel = math.sqrt(np.nan_to_num(whole["err_xel_curv"]) ** 2 + (dx_h / 2) ** 2)
    err_alt, err_xel = max(err_alt, ERROR_FLOOR_DEG), max(err_xel, ERROR_FLOOR_DEG)
    # D: the drive position the controller reported during the same records;
    # T: the commanded true direction at those instants, moved by the offset.
    samples = [s for s in step.get("samples", []) if t0 <= s["unix"] <= t1]
    if not samples:
        out.update(ok=False, why="no drive positions logged during the dwell", fit=whole)
        return out
    d_alt = float(np.mean([s["alt"] for s in samples]))
    d_az = float(np.degrees(np.angle(np.mean(np.exp(1j * np.radians([s["az"] for s in samples]))))) % 360.0)
    t_alt, t_az = altaz_of([step["l"]] * len(samples), [step["b"]] * len(samples),
                           [datetime.fromtimestamp(s["unix"], tz=timezone.utc) for s in samples])
    c_alt = float(np.mean(t_alt))
    c_az = float(np.degrees(np.angle(np.mean(np.exp(1j * np.radians(t_az))))) % 360.0)
    T_alt, T_az = offset_direction(c_alt, c_az, whole["dalt"], whole["dxel"])
    T_az %= 360.0
    why = []
    if whole["correlation"] < MIN_CORRELATION:
        why.append("correlation %.4f" % whole["correlation"])
    if not (T_SYS_RANGE_K[0] <= whole["t_sys_k"] <= T_SYS_RANGE_K[1]):
        why.append("T_sys %.0f K" % whole["t_sys_k"])
    if whole["at_edge"]:
        why.append("best offset at the edge of the search")
    if max(err_alt, err_xel) > MAX_ERROR_DEG:
        why.append("error %.2f deg" % max(err_alt, err_xel))
    record = {
        "timestamp": when.isoformat(),
        "sun_alt_deg": T_alt, "sun_az_deg": T_az,          # the field names a Sun scan uses
        "alt_error_deg": d_alt - T_alt,
        "az_error_deg": _wrap(d_az - T_az),
        "az_error_sky_deg": _wrap(d_az - T_az) * math.cos(math.radians(T_alt)),
        "alt_error_uncertainty_deg": err_alt,
        "az_error_uncertainty_deg": err_xel / math.cos(math.radians(T_alt)),
        "beam_fwhm_deg": None,
        "fit_success": not why,
        "record_version": 2,
        "source": "hi",
        "field": "l=%.0f b=%+.0f" % (step["l"], step["b"]),
    }
    out.update(ok=not why, why="; ".join(why), dalt=whole["dalt"], dxel=whole["dxel"],
               err_alt=err_alt, err_xel=err_xel, halves=[[h["dalt"], h["dxel"]] for h in halves],
               fit=whole, drive_alt=d_alt, drive_az=d_az, true_alt=T_alt, true_az=T_az,
               utc=when.isoformat(timespec="seconds"), record=record)
    return out


def residual_against(record, terms):
    """(alt, cross-elevation) of D minus the drive position `terms` would
    have commanded for T: how far the record says that model is out."""
    import drift_park
    p_alt, p_az = drift_park.true_to_drive(record["sun_alt_deg"], record["sun_az_deg"], terms)
    d_alt = record["sun_alt_deg"] + record["alt_error_deg"]
    d_az = record["sun_az_deg"] + record["az_error_deg"]
    return d_alt - p_alt, _wrap(d_az - p_az) * math.cos(math.radians(record["sun_alt_deg"]))


def leave_one_out(records, hold=None):
    """Each record predicted by a model fitted to the others: list of (alt,
    cross-el) residuals, None where the others did not support a fit. The
    honest measure of a model: what it predicts where it was not fitted."""
    import sun_scan
    out = []
    for i, r in enumerate(records):
        rest = records[:i] + records[i + 1:]
        fit = sun_scan.fit_pointing_model(rest, observatory.SITE_LAT_DEG, observatory.SITE_LON_DEG, hold=hold)
        out.append(residual_against(r, fit["terms"]) if fit.get("success") else None)
    return out


def analyse_session(recording, steps=None):
    """The whole session: every field reduced, the good ones as pointing
    records, each judged against the model in force when it was recorded,
    and the leave-one-out prediction of a model fitted to the good fields
    alone. Writes result_path(recording) and returns the dict."""
    import bandpass
    import rf_calibration as R
    from observation_plot import read_observation
    if steps is None:
        with open(steps_path(recording)) as fh:
            steps = json.load(fh)
    freq_hz, spectra, stamps, taus, header = read_observation(recording)
    corrected, note = bandpass.apply_bandpass(freq_hz, spectra, header)
    if "not bandpass corrected" in note:
        raise ValueError("cannot reduce an uncorrected recording: " + note)
    ends = np.asarray(stamps, float)
    taus = (np.asarray(taus, float) if taus is not None and np.size(taus)
            else np.full(len(ends), float(header.get("nominal_integration_time", INTEGRATION_S))))
    starts = ends - taus
    bw = float(header.get("sample_rate_requested_hz") or header.get("sample_rate_hz") or 2.0e6)
    art = header.get("dc_artefact_freq_hz") or header.get("center_freq_hz")
    sim = R.load_simulator()
    fields = [analyse_field(s, freq_hz, corrected, starts, ends, sim, bw, art) for s in steps.get("fields", [])]
    terms = header.get("pointing_terms")
    if isinstance(terms, (bytes, str)):
        try:
            terms = json.loads(terms)
        except ValueError:
            terms = None
    good = [f for f in fields if f.get("ok")]
    records = [f["record"] for f in good]
    for f in fields:
        if f.get("record") and terms:
            f["residual_in_force"] = list(residual_against(f["record"], terms))
    summary = {"recording": os.path.basename(recording), "analysed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "fields": fields, "n_fields": len(fields), "n_good": len(good), "terms_in_force": terms,
               "bandpass_note": note}
    if terms and good:
        res = np.array([f["residual_in_force"] for f in good])
        summary["rms_in_force"] = [float(np.sqrt(np.mean(res[:, 0] ** 2))), float(np.sqrt(np.mean(res[:, 1] ** 2)))]
        summary["mean_in_force"] = [float(res[:, 0].mean()), float(res[:, 1].mean())]
    reps = [f for f in fields if f.get("repeat") and f.get("ok")]
    if reps:
        first = next((f for f in fields if f.get("ok") and not f.get("repeat")
                      and f["l"] == reps[0]["l"] and f["b"] == reps[0]["b"]), None)
        if first:
            summary["repeat_difference"] = [reps[0]["dalt"] - first["dalt"], reps[0]["dxel"] - first["dxel"]]
    if len(records) >= 5:
        loo = [x for x in leave_one_out(records) if x is not None]
        if loo:
            a = np.array(loo)
            summary["loo_rms"] = [float(np.sqrt(np.mean(a[:, 0] ** 2))), float(np.sqrt(np.mean(a[:, 1] ** 2)))]
            summary["loo_n"] = len(loo)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(result_path(recording), "w") as fh:
        json.dump(summary, fh, indent=1, default=float)
    return summary


def describe(summary):
    """Lines for the log."""
    lines = ["H I pointing %s: %d of %d fields usable" % (summary["recording"], summary["n_good"], summary["n_fields"])]
    for f in summary["fields"]:
        if "dalt" in f:
            lines.append("  l=%5.1f b=%+4.1f%s alt %4.1f az %5.1f: offset %+.2f / %+.2f (+- %.2f / %.2f), T_sys %.0f K, corr %.4f%s"
                         % (f["l"], f["b"], " (repeat)" if f.get("repeat") else "", f["true_alt"], f["true_az"],
                            f["dalt"], f["dxel"], f["err_alt"], f["err_xel"], f["fit"]["t_sys_k"],
                            f["fit"]["correlation"], "" if f.get("ok") else "  - not used: " + f.get("why", "")))
        else:
            lines.append("  l=%5.1f b=%+4.1f: not reduced (%s)" % (f["l"], f["b"], f.get("why", "")))
    if "rms_in_force" in summary:
        lines.append("  against the model in force: mean %+.2f / %+.2f, rms %.2f / %.2f deg (alt / cross-el)"
                     % tuple(summary["mean_in_force"] + summary["rms_in_force"]))
    if "repeat_difference" in summary:
        lines.append("  first field again at the end: changed by %+.2f / %+.2f deg" % tuple(summary["repeat_difference"]))
    if "loo_rms" in summary:
        lines.append("  a model fitted to these fields predicts each left-out one to %.2f / %.2f deg rms (%d fields)"
                     % (summary["loo_rms"][0], summary["loo_rms"][1], summary["loo_n"]))
    return lines


def main():
    import argparse
    ap = argparse.ArgumentParser(description="H I pointing: reduce a session, or plan one")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyse")
    a.add_argument("recording")
    p = sub.add_parser("plan")
    p.add_argument("--minutes", type=float, default=60.0)
    args = ap.parse_args()
    if args.cmd == "analyse":
        for line in describe(analyse_session(args.recording)):
            print(line)
    else:
        import horizon_store
        plan = plan_session(datetime.now(timezone.utc), args.minutes, horizon_store.load_active())
        for f in plan:
            print("%6.0f s  l=%5.1f b=%+4.1f  alt %4.1f az %5.1f  sigma %.3f / %.3f%s"
                  % (f["start_s"], f["l"], f["b"], f["alt"], f["az"], f["sigma_alt"], f["sigma_xel"],
                     "  (repeat)" if f.get("repeat") else ""))


if __name__ == "__main__":
    main()
