#!/usr/bin/env python3
"""Is the beam round? Refit the Sun rasters with separate elevation and
cross-elevation widths.

Every main-lobe solid angle so far (`beam_scan.py`) is integrated along one
cut, the Sun's path through a parked beam, as if the beam were round - and the
beam in force came from a drift that crossed it horizontally, so its width in
elevation was never sampled. The scan records keep only a single fitted FWHM,
but `scheduler.log` keeps every raster point: the commanded offset and true
position, and the band power. This reconstructs where each point really
pointed and fits

    P = B + A exp(-4 ln2 [ (del - e0)^2 / W_el^2 + (dxel - x0)^2 / W_xel^2 ])

against a round fit (W_el = W_xel) on the same points.

Where each point pointed: the controller sends trueToDrive(commanded) and the
Due rounds it to the 0.5 deg pulse grid, so the reached true position is
driveToTrue(round(trueToDrive(commanded))) under the model loaded at the time
(`drift_park`, the validated copy of the firmware transform). On 2026-09-23
the model was cleared at 09:00 and re-applied at 20:08, so that day's rasters
are refraction only and exact. The real telescope's own pointing error is a
constant over a two-minute raster and goes into the fitted centre.

The raster reaches +-3 deg, the core of a flat-topped lobe, so the widths are
those of a Gaussian fitted to the core - window-dependent as numbers, but the
two axes share one window, so their ratio is the measurement.

    cd receiver_scheduler
    python ../tools/beam_ellipticity_from_rasters.py --from "2026-09-23 09:00" --to "2026-09-23 20:08"
    python ../tools/beam_ellipticity_from_rasters.py --from "2026-09-09" --to "2026-09-13" --terms old.json
"""
import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
from scipy.optimize import curve_fit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "receiver_scheduler"))
import drift_park   # noqa: E402

LOG = os.path.join(HERE, "..", "receiver_scheduler", "scheduler.log")
PULSE_DEG = 0.5
FWHM = 2.0 * math.sqrt(2.0 * math.log(2.0))

_START = re.compile(r"^(\S+ \S+) INFO  Starting (\d+)x\d+ sun scan \(spacing=([\d.]+)")
_POINT = re.compile(r"^(\S+ \S+) INFO  Point (\d+)/(\d+): offset \(([-\d.]+), ([-\d.]+)\)\S* -> Alt=([-\d.]+)\S* Az=([-\d.]+)")
_POWER = re.compile(r"^(\S+ \S+) INFO    Power = ([-\d.eE]+)")


def local_to_utc(text):
    """scheduler.log stamps are local time."""
    return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").astimezone(timezone.utc)


def read_rasters(path, start, end):
    """[{'start', 'points': [(utc, d_alt_cmd, d_xel_cmd, alt_cmd, az_cmd, power)]}]."""
    rasters, cur, pending = [], None, None
    with open(path, errors="replace") as fh:
        for line in fh:
            if line[:19] < start or line[:19] >= end:
                continue
            m = _START.match(line)
            if m:
                cur = {"start": m.group(1), "n": int(m.group(2)), "spacing": float(m.group(3)), "points": []}
                rasters.append(cur)
                continue
            m = _POINT.match(line)
            if m and cur is not None:
                pending = [float(m.group(4)), float(m.group(5)), float(m.group(6)), float(m.group(7))]
                continue
            m = _POWER.match(line)
            if m and cur is not None and pending is not None:
                cur["points"].append([local_to_utc(m.group(1))] + pending + [float(m.group(2))])
                pending = None
    return [r for r in rasters if len(r["points"]) == r["n"] ** 2]


def sun_position(utc_list):
    """(alt, az, parallactic angle) of the Sun per time, degrees. The
    parallactic angle is how far the sky's north has turned from the zenith
    direction, positive west of the meridian - what a sky-fixed shape would
    turn by in the alt-az frame."""
    import ephem
    site = ephem.Observer()
    site.lat, site.lon, site.elevation = "55.902426", "-4.307865", 50.0
    site.pressure = 0
    lat = math.radians(55.902426)
    out = []
    for t in utc_list:
        site.date = t.replace(tzinfo=None)
        s = ephem.Sun(site)
        ha = float(site.sidereal_time()) - float(s.ra)
        dec = float(s.dec)
        q = math.atan2(math.sin(ha), math.tan(lat) * math.cos(dec) - math.sin(dec) * math.cos(ha))
        out.append((math.degrees(float(s.alt)), math.degrees(float(s.az)), math.degrees(q)))
    return np.array(out)


def reached(alt_cmd, az_cmd, terms):
    """True position the beam axis reached for a commanded true position."""
    d_alt, d_az = drift_park.true_to_drive(alt_cmd, az_cmd, terms)
    d_alt = round(d_alt / PULSE_DEG) * PULSE_DEG
    d_az = round(d_az / PULSE_DEG) * PULSE_DEG
    return drift_park.drive_to_true(d_alt, d_az, terms)


def _ellip(xy, b, a, e0, x0, w_el, w_xel):
    de, dx = xy
    return b + a * np.exp(-4 * math.log(2) * ((de - e0) ** 2 / w_el ** 2 + (dx - x0) ** 2 / w_xel ** 2))


def _rotated(xy, b, a, e0, x0, w_major, w_minor, pa):
    """Elliptical Gaussian whose major axis lies at `pa` degrees from the
    elevation axis, towards +cross-elevation."""
    de, dx = xy
    c, s = math.cos(math.radians(pa)), math.sin(math.radians(pa))
    u = (de - e0) * c + (dx - x0) * s
    v = -(de - e0) * s + (dx - x0) * c
    return b + a * np.exp(-4 * math.log(2) * (u ** 2 / w_major ** 2 + v ** 2 / w_minor ** 2))


def _round(xy, b, a, e0, x0, w):
    return _ellip(xy, b, a, e0, x0, w, w)


def fit_raster(r, terms):
    pts = r["points"]
    # Mid-integration: the Power line is stamped at the end of a 3 s point.
    times = [p[0] - timedelta(seconds=1.5) for p in pts]
    sun = sun_position(times)
    de, dx, pw = [], [], []
    for p, (s_alt, s_az, _) in zip(pts, sun):
        t_alt, t_az = reached(p[3], p[4], terms)
        de.append(t_alt - s_alt)
        dx.append(((t_az - s_az + 180.0) % 360.0 - 180.0) * math.cos(math.radians(s_alt)))
        pw.append(p[5])
    de, dx, pw = np.array(de), np.array(dx), np.array(pw)
    peak = pw.max() - pw.min()
    i = int(np.argmax(pw))
    p0 = [pw.min(), peak, de[i], dx[i], 4.5, 4.5]
    lo = [-np.inf, 0, -5, -5, 1.0, 1.0]
    hi = [np.inf, np.inf, 5, 5, 15.0, 15.0]
    pe, ce = curve_fit(_ellip, (de, dx), pw, p0=p0, bounds=(lo, hi), maxfev=20000)
    pr, _ = curve_fit(_round, (de, dx), pw, p0=p0[:5], bounds=(lo[:5], hi[:5]), maxfev=20000)
    # Free position angle, started from the axis-aligned answer either way
    # round; the better of the two starts is kept.
    best = None
    for start in (0.0, 90.0, 45.0, -45.0):
        try:
            pp, cp = curve_fit(_rotated, (de, dx), pw, p0=list(pe[:4]) + [max(pe[4], pe[5]), min(pe[4], pe[5]), start],
                               bounds=(lo + [-180.0], hi + [180.0]), maxfev=20000)
        except RuntimeError:
            continue
        rms = float((pw - _rotated((de, dx), *pp)).std())
        if best is None or rms < best[2]:
            best = (pp, cp, rms)
    pp, cp, rms_rot = best
    w_maj, w_min, pa = pp[4], pp[5], pp[6]
    if w_min > w_maj:
        w_maj, w_min, pa = w_min, w_maj, pa + 90.0
    pa = (pa + 90.0) % 180.0 - 90.0                       # -90..90 from elevation
    pa_err = math.sqrt(cp[6, 6]) if np.isfinite(cp[6, 6]) else float("nan")
    res_e = pw - _ellip((de, dx), *pe)
    res_r = pw - _round((de, dx), *pr)
    err = np.sqrt(np.diag(ce))
    return {"start": r["start"], "sun_alt": float(sun[:, 0].mean()), "sun_az": float(sun[:, 1].mean()),
            "w_el": pe[4], "w_xel": pe[5], "w_el_err": err[4], "w_xel_err": err[5],
            "w_round": pr[4], "centre": (pe[2], pe[3]),
            "rms_ellip": float(res_e.std()), "rms_round": float(res_r.std()),
            "peak_over_base": float(pe[1] / pe[0]) if pe[0] else float("nan"),
            "min_offset_el": float(np.abs(de - pe[2]).max()), "min_offset_xel": float(np.abs(dx - pe[3]).max()),
            "w_major": float(w_maj), "w_minor": float(w_min), "pa": float(pa), "pa_err": float(pa_err),
            "rms_rot": rms_rot, "parallactic": float(sun[:, 2].mean())}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="start", required=True, help="local time, as the log stamps it")
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--terms", help="JSON of the pointing terms loaded at the time (default: none - refraction only)")
    ap.add_argument("--log", default=LOG)
    a = ap.parse_args(argv)
    terms = json.load(open(a.terms)) if a.terms else {}
    rasters = read_rasters(a.log, a.start, a.end)
    print("%d complete rasters, terms: %s" % (len(rasters), terms or "none (refraction only)"))
    print("%-19s %5s %6s   %-12s %-12s %6s  %6s  %-9s %-11s %6s" %
          ("start (local)", "alt", "az", "W_el", "W_xel", "ratio", "round", "maj/min", "PA (el->xel)", "parall"))
    ratios, wts, pas = [], [], []
    for r in rasters:
        try:
            f = fit_raster(r, terms)
        except (RuntimeError, ValueError) as exc:
            print("%s  fit failed: %s" % (r["start"], exc))
            continue
        q = f["w_el"] / f["w_xel"]
        qe = q * math.hypot(f["w_el_err"] / f["w_el"], f["w_xel_err"] / f["w_xel"])
        ratios.append(q)
        wts.append(1.0 / qe ** 2)
        print("%s %5.1f %6.1f   %5.2f±%.2f   %5.2f±%.2f   %5.3f±%.3f %5.2f   %5.3f   %+6.1f±%4.1f %+6.1f" % (
            f["start"][:19], f["sun_alt"], f["sun_az"], f["w_el"], f["w_el_err"], f["w_xel"],
            f["w_xel_err"], q, qe, f["w_round"], f["w_major"] / f["w_minor"], f["pa"], f["pa_err"],
            f["parallactic"]))
        pas.append((f["pa"], f["pa_err"], f["parallactic"]))
    if ratios:
        ratios, wts = np.array(ratios), np.array(wts)
        mean = float(np.sum(wts * ratios) / wts.sum())
        print("\nW_el / W_xel: weighted mean %.3f ± %.3f (formal), median %.3f, scatter %.3f over %d rasters"
              % (mean, 1 / math.sqrt(wts.sum()), np.median(ratios), ratios.std(ddof=1) if len(ratios) > 1 else 0,
                 len(ratios)))
        pa, pe_, par = (np.array(x) for x in zip(*pas))
        w = 1.0 / np.maximum(pe_, 0.5) ** 2
        print("major-axis PA from elevation: weighted mean %+.1f deg, scatter %.1f, range %+.1f..%+.1f"
              % (np.sum(w * pa) / w.sum(), pa.std(ddof=1) if len(pa) > 1 else 0, pa.min(), pa.max()))
        if len(pa) > 2 and np.ptp(par) > 5:
            slope = np.polyfit(par, pa, 1, w=np.sqrt(w))[0]
            print("PA against parallactic angle (%+.0f..%+.0f): slope %.2f (0 = fixed to the mount, 1 = fixed to the sky)"
                  % (par.min(), par.max(), slope))


if __name__ == "__main__":
    main()
