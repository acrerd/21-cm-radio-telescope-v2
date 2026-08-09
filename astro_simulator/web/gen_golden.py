#!/usr/bin/env python3
"""Generate golden validation vectors for the web simulator.

Runs the desktop simulator (compact data only) at a FIXED epoch and
writes test/golden.json.  The JS test harness (test/run_golden.mjs)
must reproduce every number here within the stated tolerances.

Everything time-dependent is evaluated at T0 so the vectors are stable;
the continuum point sources are computed once here and *injected* into
the engine on both sides, which separates engine validation from
ephemeris validation (both are still covered, just independently).

Run from this folder:  python gen_golden.py
"""

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
import astro_simulator as A                    # noqa: E402
from astropy import units as u                 # noqa: E402
from astropy.coordinates import (SkyCoord, AltAz, FK4, get_sun,
                                 get_body)     # noqa: E402
from astropy.time import Time                  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
T0 = Time("2026-08-09T12:00:00", scale="utc")

SITE = {"name": "Glasgow", "lat": 55.87, "lon": -4.29, "height": 50.0}


def fixed_sources():
    """continuum_sources() evaluated at T0 instead of Time.now()."""
    cyg = SkyCoord(ra=299.868 * u.deg, dec=40.734 * u.deg).galactic
    cas = SkyCoord(ra=350.850 * u.deg, dec=58.815 * u.deg).galactic
    tau = SkyCoord(ra=83.633 * u.deg, dec=22.015 * u.deg).galactic
    sun_g = get_sun(T0)
    sun = SkyCoord(ra=sun_g.ra, dec=sun_g.dec).galactic
    moon_g = get_body("moon", T0, location=A.SITE_LOC)
    moon = SkyCoord(ra=moon_g.ra, dec=moon_g.dec).galactic
    cas_jy = 2500.0 * np.exp(-0.00670 * (T0.decimalyear - 1965.0))
    return [("Cyg A", cyg.l.deg, cyg.b.deg, 1590.0),
            ("Cas A", cas.l.deg, cas.b.deg, float(cas_jy)),
            ("Tau A", tau.l.deg, tau.b.deg, 875.0),
            ("Sun", sun.l.deg, sun.b.deg, 5.0e5),
            ("Moon", moon.l.deg, moon.b.deg, 890.0)]


def coordinate_vectors():
    """Frame conversions and alt/az the JS port must reproduce."""
    pts = [(0.0, 0.0), (30.0, 0.0), (121.17, -21.57), (280.5, -32.9),
           (150.0, 53.0), (359.9, 89.0), (180.0, -89.0)]
    gal2icrs = []
    for l, b in pts:
        c = SkyCoord(l=l * u.deg, b=b * u.deg, frame="galactic").icrs
        gal2icrs.append({"l": l, "b": b,
                         "ra": float(c.ra.deg), "dec": float(c.dec.deg)})
    aa_frame = AltAz(obstime=T0, location=A.SITE_LOC, pressure=0)
    radec = [(18.62 * 15, 38.78), (83.633, 22.015), (350.85, 58.815),
             (266.4, -29.0)]
    altaz = []
    for ra, dec in radec:
        c = SkyCoord(ra=ra * u.deg, dec=dec * u.deg).transform_to(aa_frame)
        altaz.append({"ra": ra, "dec": dec,
                      "alt": float(c.alt.deg), "az": float(c.az.deg)})
    sun_g = get_sun(T0)
    sun = SkyCoord(ra=sun_g.ra, dec=sun_g.dec).galactic
    moon_g = get_body("moon", T0, location=A.SITE_LOC)
    moon = SkyCoord(ra=moon_g.ra, dec=moon_g.dec).galactic
    return {"gal2icrs": gal2icrs, "altaz": altaz,
            "sun_gal": {"l": float(sun.l.deg), "b": float(sun.b.deg)},
            "moon_gal": {"l": float(moon.l.deg), "b": float(moon.b.deg)}}


def frame_vectors():
    """LSR->SSB (time-free) and LSR->topo offsets at T0, m/s."""
    out = []
    apex = SkyCoord(ra="18h", dec="30d", frame=FK4(equinox="B1900")).icrs
    for l, b in [(0.0, 0.0), (30.0, 0.0), (134.0, -1.0), (150.0, 53.0),
                 (280.5, -32.9)]:
        tgt = SkyCoord(l=l * u.deg, b=b * u.deg, frame="galactic").icrs
        dv_ssb = -20.0e3 * np.cos(apex.separation(tgt).rad)
        rv = tgt.radial_velocity_correction(
            "barycentric", obstime=T0, location=A.SITE_LOC)
        out.append({"l": l, "b": b, "ssb": float(dv_ssb),
                    "topo": float(dv_ssb - rv.to_value(u.m / u.s))})
    return out


def build_sim(sources):
    sim = A.DishSimulator(
        os.path.join(HERE, "..", "hi4pi_allsky_gal_CAR.fits"),
        2.0e6, 3.0, 0.7, None, None, 60.0, 1,
        compact_path=os.path.join(HERE, "..", "hi4pi_compact.npz.xz"),
        continuum_path=os.path.join(HERE, "..",
                                    "continuum_1420_compact.npz.xz"))
    sim.sources = sources
    return sim


def spectrum_vectors(sim):
    F = A.F_HI
    combos = []
    pointings = [(30.0, 0.0), (0.0, 0.0), (134.0, -1.0), (180.0, 0.0),
                 (121.17, -21.57), (280.5, -32.9), (150.0, 53.0)]
    for l, b in pointings[:4]:
        combos.append((l, b, 4.93, 2.0e6, F, None))
    combos += [(121.17, -21.57, 4.93, 5.0e6, F, 1024),
               (280.5, -32.9, 10.0, 5.0e6, F, 1024),
               (150.0, 53.0, 1.6, 2.0e6, F, None),
               (30.0, 0.0, 1.6, 2.0e6, F, 512),
               (134.0, -1.0, 10.0, 3.0e6, F, None),
               (30.0, 0.0, 4.93, 2.0e6, 1425.0e6, None),   # off-line
               (150.0, 53.0, 4.93, 2.0e6, 1424.0e6, 256),  # off-line
               (0.0, 0.0, 4.93, 8.0e6, F + 2.0e6, 2048)]   # hangs over edge
    out = []
    for l, b, fwhm, bw, fc, nchan in combos:
        sim.set_beam(fwhm)
        sim.set_band(bw, fc)
        sim.nchan = nchan
        v, t, sig, tcont = sim.spectrum(l, b)
        assert sig is None
        out.append({"l": l, "b": b, "fwhm": fwhm, "bw_hz": bw,
                    "fc_hz": fc, "nchan": nchan,
                    "v": np.asarray(v).tolist(),
                    "t": np.asarray(t).tolist(),
                    "tcont": float(tcont)})
    return out


def continuum_vectors(sim, sources):
    sim.set_beam(4.93)
    pts = [(s[1], s[2]) for s in sources[:4]]           # on-source
    pts += [(30.0, 0.0), (150.0, 53.0), (121.17, -21.57), (0.0, -90.0)]
    return [{"l": l, "b": b, "t": float(sim.continuum(l, b))}
            for l, b in pts]


def drift_vectors(sim):
    out = []
    for l, b, dur, fwhm in [(83.63, 22.01, 240.0, 4.93),
                            (0.0, 0.0, 120.0, 4.93)]:
        sim.set_beam(fwhm)
        sim.set_band(2.0e6, A.F_HI)
        sim.nchan = None
        c = SkyCoord(l=l * u.deg, b=b * u.deg, frame="galactic")
        ra0, dec = float(c.icrs.ra.deg), float(c.icrs.dec.deg)
        n = 151
        dt_h = np.linspace(-dur / 120.0, dur / 120.0, n)
        sky = SkyCoord(ra=(ra0 + 15.041 * dt_h) * u.deg,
                       dec=np.full(n, dec) * u.deg).galactic
        tbar = [float(np.nanmean(sim.spectrum(li, bi)[1]))
                for li, bi in zip(sky.l.deg, sky.b.deg)]
        out.append({"l": l, "b": b, "dur_min": dur, "fwhm": fwhm,
                    "ra0": ra0, "dec": dec, "tbar": tbar})
    return out


def main():
    sources = fixed_sources()
    sim = build_sim(sources)
    golden = {
        "t0_utc": T0.isot,
        "t0_jd": float(T0.jd),
        "t0_decimalyear": float(T0.decimalyear),
        "site": SITE,
        "eta": 0.7, "npol": 1,
        "sources": [{"name": n, "l": l, "b": b, "jy": f}
                    for n, l, b, f in sources],
        "coords": coordinate_vectors(),
        "frames": frame_vectors(),
        "spectra": spectrum_vectors(sim),
        "continuum": continuum_vectors(sim, sources),
        "drift": drift_vectors(sim),
    }
    os.makedirs(os.path.join(HERE, "test"), exist_ok=True)
    path = os.path.join(HERE, "test", "golden.json")
    with open(path, "w") as f:
        json.dump(golden, f)
    print(f"wrote {path} ({os.path.getsize(path)/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
