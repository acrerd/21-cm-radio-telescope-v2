#!/usr/bin/env python3
"""PINT side of the TOA chain. Runs ONLY under the PINT environment
(/home/astro/radioconda/envs/pint), never radioconda, and is driven by
pulsar_toa / tests through a subprocess, exchanging JSON.

    pint_tools.py residuals PAR TIM LAT LON HEIGHT   -> residuals (us) per TOA
    pint_tools.py fake PAR MJDS.json FREQ LAT LON HEIGHT -> PINT's own arrival times

The site is registered as our own observatory, "acre_srt" (alias "ar"), at the
surveyed position passed in, rather than PINT's "acre", which sits 49 m away.
Times are UTC from NTP or the PPS, so no GPS or site clock correction applies
(apply_gps2utc False, no clock file); the par's CLK TT(TAI) keeps PINT off the
network for BIPM files - a microsecond-level choice, far below our errors.
"""
import json
import sys
import warnings

warnings.filterwarnings("ignore")


def register_site(lat, lon, height):
    import astropy.units as u
    from astropy.coordinates import EarthLocation
    from pint.observatory.topo_obs import TopoObs
    loc = EarthLocation(lat=float(lat) * u.deg, lon=float(lon) * u.deg, height=float(height) * u.m)
    TopoObs("acre_srt", aliases=["ar"], itrf_xyz=[loc.x.to_value(u.m), loc.y.to_value(u.m), loc.z.to_value(u.m)],
            apply_gps2utc=False, overwrite=True, origin="Acre Road SRT, surveyed 55.902426 -4.307865")


def residuals(par, tim, lat, lon, height):
    from pint.models import get_model
    from pint.toa import get_TOAs
    from pint.residuals import Residuals
    register_site(lat, lon, height)
    m = get_model(par)
    t = get_TOAs(tim, model=m, planets=False, include_bipm=False)
    r = Residuals(t, m)
    names = [f.get("name", "") for f in t.table["flags"]] if "flags" in t.table.colnames else []
    return {"mjd": [float(x) for x in t.get_mjds().value],
            "resid_us": [float(x) for x in r.time_resids.to_value("us")],
            "err_us": [float(x) for x in t.get_errors().to_value("us")],
            "names": names, "chi2": float(r.chi2), "dof": int(r.dof)}


def fake(par, mjds_json, freq_mhz, lat, lon, height):
    """PINT's own arrival times at our site near the given MJDs: TOAs with
    zero residual under the par - pulses arriving exactly on its phase."""
    import astropy.units as u
    import numpy as np
    from astropy.time import Time
    from pint.models import get_model
    from pint.simulation import make_fake_toas_fromMJDs
    register_site(lat, lon, height)
    m = get_model(par)
    mjds = Time(np.array(json.load(open(mjds_json)), float), format="mjd", scale="utc")
    t = make_fake_toas_fromMJDs(mjds, m, freq=float(freq_mhz) * u.MHz, obs="acre_srt", error=1 * u.us,
                                add_noise=False, include_bipm=False)
    # the arrivals as unix times. Not (t - 1970-01-01) in UTC: that counts the
    # 27 leap seconds unix time leaves out, and the 27 s error, through the
    # Doppler rate, showed as a 4 us/h drift against absolute_phase.
    tt = Time(list(t.get_mjds(high_precision=True)))      # PINT's Time objects, joined; two-part inside
    out = [float(x) for x in tt.utc.unix]
    return {"t_unix": out}


def fit(par, tim, lat, lon, height):
    """Fit the whole-run TOAs (names without a _sN suffix): F0 free from
    three runs spanning a day, F1 free from four spanning three weeks,
    otherwise nothing free and the residuals are against the catalogue par.
    Residuals are returned for every TOA - segments too, under the same
    model - with the weighted mean of the whole-run ones removed."""
    import re
    import numpy as np
    from pint.models import get_model
    from pint.toa import get_TOAs
    from pint.residuals import Residuals
    from pint.fitter import WLSFitter
    register_site(lat, lon, height)
    m = get_model(par)
    t = get_TOAs(tim, model=m, planets=False, include_bipm=False)
    flags = list(t.table["flags"])
    names = [f.get("name", "") for f in flags]
    whole = np.array([re.search(r"_s\d+$", n) is None for n in names])
    tw = t[whole]
    mjd_w = tw.get_mjds().value
    span = float(np.ptp(mjd_w)) if len(mjd_w) else 0.0
    free = []
    if len(mjd_w) >= 3 and span >= 1.0:
        free.append("F0")
    if len(mjd_w) >= 4 and span >= 21.0:
        free.append("F1")
    # Quote P and Pdot at the data's own epoch, not the catalogue's 1986 one:
    # a fitted F0 there is a 40-year extrapolation and its error means little.
    if len(mjd_w):
        m.change_pepoch(float(np.round(np.mean(mjd_w), 3)))
    m.free_params = free
    chi2 = dof = None
    if free:
        f = WLSFitter(tw, m)
        f.fit_toas()
        m = f.model
        chi2, dof = float(f.resids.chi2), int(f.resids.dof)
    r = Residuals(t, m, subtract_mean=False)
    res = r.time_resids.to_value("us")
    err = m.scaled_toa_uncertainty(t).to_value("us")
    if whole.any():
        w = 1.0 / err[whole] ** 2
        res = res - np.sum(w * res[whole]) / np.sum(w)
    if chi2 is None and whole.sum() > 1:
        chi2 = float(np.sum((res[whole] / err[whole]) ** 2)); dof = int(whole.sum() - 1)
    f0, f1 = m.F0.value, m.F1.value
    ef0 = m.F0.uncertainty_value if "F0" in free else None
    ef1 = m.F1.uncertainty_value if "F1" in free else None
    P = 1.0 / f0
    Pdot = -f1 / f0 ** 2
    return {"mjd": [float(x) for x in t.get_mjds().value], "resid_us": [float(x) for x in res],
            "err_us": [float(x) for x in err], "names": names, "whole": [bool(x) for x in whole],
            # statistical only: a run's segments share its clock offset, so
            # among themselves they carry no clock term
            "err_raw_us": [float(x) for x in t.get_errors().to_value("us")],
            "pps": [str(fl.get("pps", "")) for fl in flags],
            "free": free, "n_whole": int(whole.sum()), "span_days": span,
            "pepoch": float(m.PEPOCH.value),
            "F0": float(f0), "F0_err": ef0, "F1": float(f1), "F1_err": ef1,
            "P": float(P), "P_err": (float(ef0 / f0 ** 2) if ef0 else None),
            "Pdot": float(Pdot), "Pdot_err": (float(ef1 / f0 ** 2) if ef1 else None),
            "chi2": chi2, "dof": dof,
            "rms_us": float(np.sqrt(np.mean(res[whole] ** 2))) if whole.any() else None}


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "residuals":
        print(json.dumps(residuals(*args)))
    elif cmd == "fit":
        print(json.dumps(fit(*args)))
    elif cmd == "fake":
        print(json.dumps(fake(*args)))
    else:
        raise SystemExit("unknown command %r" % cmd)


if __name__ == "__main__":
    main()
