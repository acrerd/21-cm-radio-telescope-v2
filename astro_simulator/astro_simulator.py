#!/usr/bin/env python3
"""
Interactive HI4PI sky: click the all-sky map, get the 3-m dish spectrum.

Opens a window with the HI4PI column-density map (Mollweide, galactic).
Left-click anywhere on the map: the beam-weighted antenna-temperature
spectrum for that pointing is computed from the all-sky spectral cube
and drawn in the lower panel, with the beam footprint shown on the map.

Usage:
    python astro_simulator.py
    python astro_simulator.py --bw 2 --tsys 100 --tint 60 --nchan 1024

Sky data: the compact pre-smoothed cube from hi4pi_compress.py
(hi4pi_compact.npz.xz, ~23 MB, shipped in the repo) is used when
present; the full ~33 GiB all-sky cube is loaded automatically instead
when a request needs it (beam finer than ~1.5 deg, or a band beyond
+/-470 km/s) and it is on disk, or with --full (downloading from CDS if
missing, see hi4pi_data.py).  The N_HI display map is gridded once and
cached in nhi_grid_cache.npy.
Press "s" to save the current spectrum to PNG + txt.
"""

import argparse
import json
import os
import urllib.parse
import urllib.request
import warnings

import numpy as np
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
from matplotlib.widgets import Button, TextBox
from astropy import units as u
from astropy.coordinates import (AltAz, EarthLocation, FK4, SkyCoord,
                                 get_body, get_sun)
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS

from continuum_compress import CONTINUUM_DEFAULT, load_continuum
from hi4pi_compress import COMPACT_DEFAULT, load_compact
from hi4pi_data import ensure_file
from instrument import (MAIN_BEAM_EFFICIENCY, SITE_HEIGHT_M, SITE_LAT_DEG,
                        SITE_LON_DEG, SITE_NAME as SITE_NAME_DEFAULT,
                        beam_fwhm_deg)

# the cursor moving outside the Mollweide ellipse makes matplotlib's
# inverse projection hit arcsin(|x| > 1); harmless, so keep it quiet
# (filtered by module so our own arcsin in haversine_deg still warns)
warnings.filterwarnings("ignore",
                        message="invalid value encountered in arcsin",
                        module=r"matplotlib\.projections\.geo")

# ---- observer site: edit these defaults, or override at runtime with
# ---- --site/--lat/--lon/--height (visibility loops, live horizon and
# ---- the topocentric frame all follow automatically)
SITE_NAME = SITE_NAME_DEFAULT
SITE_LAT = SITE_LAT_DEG      # deg, +N
SITE_LON = SITE_LON_DEG      # deg, +E
SITE_HEIGHT = SITE_HEIGHT_M  # m
SITE_LOC = EarthLocation(lat=SITE_LAT * u.deg, lon=SITE_LON * u.deg,
                         height=SITE_HEIGHT * u.m)


def set_site(name, lat, lon, height):
    """Point every site-dependent feature at a new observer location."""
    global SITE_NAME, SITE_LAT, SITE_LON, SITE_HEIGHT, SITE_LOC
    SITE_NAME, SITE_LAT, SITE_LON, SITE_HEIGHT = name, lat, lon, height
    SITE_LOC = EarthLocation(lat=lat * u.deg, lon=lon * u.deg,
                             height=height * u.m)


def never_rises(dec_deg):
    """True if a declination never clears the site's horizon."""
    if SITE_LAT >= 0:
        return dec_deg < SITE_LAT - 90.0
    return dec_deg > SITE_LAT + 90.0


K_B = 1.380649e-23
LANDMARKS = [("M31", 121.17, -21.57)]   # map markers, no continuum flux

# notable pointings: (label, l, b, min bandwidth MHz to cover the line)
# notable pointings: (label, l, b, min bandwidth MHz, short description)
TARGETS = [
    ("Galactic centre wings", 0.0, 0.0, 3.0, "broad velocity wings"),
    ("Inner Galaxy (l=30)", 30.0, 0.0, 2.0, "terminal velocities"),
    ("Vulpecula rift (l=60)", 60.0, 0.0, 2.0, "local + Sagittarius arm"),
    ("Cygnus X (l=80)", 80.0, 0.0, 2.0, "star-forming complex"),
    ("Outer Arm (l=110)", 110.0, 0.0, 2.0, "distant spiral arm"),
    ("Perseus arm (l=134)", 134.0, -1.0, 2.0, "double-peaked line"),
    ("Anticentre (l=180)", 180.0, 0.0, 2.0, "zero-velocity direction"),
    ("Rosette (l=206)", 206.0, -2.0, 2.0, "3rd quadrant plane"),
    ("Third quadrant (l=220)", 220.0, 0.0, 2.0, "negative velocities"),
    ("M31 (Andromeda)", 121.17, -21.57, 5.0, "H I at -300 km/s"),
    ("M33 (Triangulum)", 133.6, -31.3, 3.0, "H I at -180 km/s"),
    ("LMC", 280.5, -32.9, 5.0, "H I at +280 km/s"),
    ("SMC", 302.8, -44.3, 3.0, "H I at +160 km/s"),
    ("HVC Complex A", 150.0, 35.0, 3.0, "infalling, -180 km/s"),
    ("HVC Complex C", 100.0, 45.0, 3.0, "infalling, -120 km/s"),
    ("Smith Cloud", 39.0, -13.0, 2.0, "infalling, +100 km/s"),
    ("Lockman Hole", 150.0, 53.0, 2.0, "minimum H I, off-position"),
    ("Celestial pole", 122.9, 27.1, 2.0, "zero drift rate"),
]


def continuum_sources():
    """The bright continuum sources: (name, l, b, flux at 1420 MHz in Jy).
    Sun and Moon positions are for launch time; quiet-Sun flux (active
    Sun is 10-100x).  These stay analytic even when the 1420 MHz
    continuum map is loaded: the survey saturates/blanks the strong
    sources (their imprint is removed from the compact map and the true
    fluxes added back here), and the moving ones can't be in a map."""
    now = Time.now()
    cyg = SkyCoord(ra=299.868 * u.deg, dec=40.734 * u.deg).galactic
    cas = SkyCoord(ra=350.850 * u.deg, dec=58.815 * u.deg).galactic
    tau = SkyCoord(ra=83.633 * u.deg, dec=22.015 * u.deg).galactic
    # direction-only: get_sun carries the Earth-Sun distance, and a 3-D
    # transform to Galactic would re-centre on the solar-system
    # barycentre, giving a meaningless direction (the Sun IS the
    # barycentre, near enough)
    sun_gcrs = get_sun(now)
    sun = SkyCoord(ra=sun_gcrs.ra, dec=sun_gcrs.dec).galactic
    # topocentric Moon (parallax ~1 deg matters); a ~225 K disc of
    # ~0.52 deg diameter is ~890 Jy at 21 cm - Cas A class in this beam
    moon_gcrs = get_body("moon", now, location=SITE_LOC)
    moon = SkyCoord(ra=moon_gcrs.ra, dec=moon_gcrs.dec).galactic
    # Cas A fades secularly: anchor the Baars et al. (1977) epoch-1965
    # spectrum (2500 Jy at 1420 MHz) and apply the 0.670 +/- 0.019 %/yr
    # long-term L-band rate of Trotter et al. (2017, MNRAS 469, 1299);
    # ~1650 Jy in 2026, good to a few % (the rate wanders by decade)
    cas_jy = 2500.0 * np.exp(-0.00670 * (now.decimalyear - 1965.0))
    return [("Cyg A", cyg.l.deg, cyg.b.deg, 1590.0),
            ("Cas A", cas.l.deg, cas.b.deg, cas_jy),
            ("Tau A", tau.l.deg, tau.b.deg, 875.0),
            ("Sun", sun.l.deg, sun.b.deg, 5.0e5),
            ("Moon", moon.l.deg, moon.b.deg, 890.0)]


C_LIGHT = 299792458.0
F_HI = 1420405751.768


def haversine_deg(l1, b1, l2, b2):
    """Angular separation in deg; inputs in deg, broadcastable."""
    l1, b1, l2, b2 = map(np.radians, (l1, b1, l2, b2))
    s = np.sin((b1 - b2) / 2) ** 2 \
        + np.cos(b1) * np.cos(b2) * np.sin((l1 - l2) / 2) ** 2
    return np.degrees(2 * np.arcsin(np.sqrt(s)))


def frame_offset(glon, glat, frame, obstime=None):
    """Velocity (m/s) to ADD to a v_LSRK axis to express the spectrum in
    `frame`: 'lsr' (native), 'ssb' (solar-system barycentre) or 'topo'
    (the observer site, at `obstime`, or now if that is not given).

    `obstime` matters more than it looks. The barycentric term moves with the
    Earth's orbit and rotation, so simulating a recorded observation at "now"
    rather than at the time it was taken misplaces the velocity axis: measured
    toward l=80 b=0 from Acre Road, 0.06 km/s after an hour but 1.95 km/s after
    a week and 8.06 km/s after a month - 19 and 78 channels at 0.49 kHz. Live
    use can leave it None; reducing anything from the archive must not.

    LSRK is defined by a 20 km/s solar motion toward RA 18h, Dec +30
    (B1900), so v_SSB = v_LSR - U with U the apex motion projected on
    the line of sight; the topocentric axis subtracts in addition the
    barycentric correction (Earth orbit + rotation) from astropy."""
    if frame == "lsr":
        return 0.0
    tgt = SkyCoord(l=glon * u.deg, b=glat * u.deg, frame="galactic").icrs
    apex = SkyCoord(ra="18h", dec="30d",
                    frame=FK4(equinox="B1900")).icrs
    dv = -20.0e3 * np.cos(apex.separation(tgt).rad)
    if frame == "topo":
        rv = tgt.radial_velocity_correction(
            "barycentric", obstime=Time(obstime) if obstime is not None else Time.now(),
            location=SITE_LOC)
        dv -= rv.to_value(u.m / u.s)
    return dv


class DishSimulator:
    """Holds the sky data + precomputed axes; computes spectra quickly.

    Two interchangeable backends: the compact pre-smoothed cube from
    hi4pi_compress.py (a few hundred MB in RAM, instant spectra) or the
    full 33 GiB survey cube (memory-mapped).  The compact sky is already
    smoothed to `data_fwhm`, so only the residual beam is convolved; it
    cannot honour beams below `min_fwhm` or bands beyond its trimmed
    velocity range - use_full_cube() upgrades when that happens."""

    FULL_V_MS = 6.07e5      # the full cube covers |v_LSR| < ~607 km/s

    def __init__(self, cube_path, bw_hz, dish_m, eta, nchan=None,
                 tsys=None, tint=60.0, npol=1, compact_path=None,
                 continuum_path=None):
        self.bw_hz, self.eta = bw_hz, eta
        self.nchan, self.tsys, self.tint, self.npol = nchan, tsys, tint, npol
        self.dish_m = dish_m
        self.full_path = cube_path
        self.compact_path = compact_path
        self.compact = None
        self._compact_cache = None      # survives a switch to the full cube
        self.hdul = None
        self.sources = continuum_sources()
        self.cmap = None                # diffuse 1420 MHz continuum sky
        if continuum_path and os.path.exists(continuum_path):
            self.cmap = load_continuum(continuum_path)
            print(f"Continuum sky: {continuum_path} (Stockert/"
                  f"Villa-Elisa 1420 MHz; strong sources analytic).")
        natural = beam_fwhm_deg(dish_m)
        if compact_path:
            self._load_compact(compact_path)
            if natural < self.min_fwhm and os.path.exists(self.full_path):
                print(f"Dish beam {natural:.2f} deg is finer than the "
                      f"compact dataset supports; using the full cube.")
                self._load_full()
        else:
            self._load_full()
        self.set_beam(natural)
        self.rng = np.random.default_rng()
        self.set_band(bw_hz, F_HI)

    def _load_compact(self, path):
        """Point the simulator at a compact cube (all in RAM)."""
        if self._compact_cache is None:
            self._compact_cache = load_compact(path)
        c = self._compact_cache
        self.compact = c
        if self.hdul is not None:
            self.hdul.close()
            self.hdul = None
        self.lon, self.lat = c.lon % 360.0, c.lat
        self.v_all = c.v
        self.f_all = F_HI * (1.0 - c.v / C_LIGHT)
        # the data are already smoothed to c.fwhm; a requested beam must
        # leave a residual of ~2 pixels or the convolution is untrue
        pix = abs(np.median(np.diff(c.lat)))
        self.data_fwhm = c.fwhm
        self.min_fwhm = round(np.sqrt(c.fwhm ** 2 + (2.355 * pix) ** 2), 2)

    def _load_full(self):
        """Point the simulator at the full survey cube (memory-mapped)."""
        path = ensure_file(self.full_path)
        self.compact = None
        self.hdul = fits.open(path, memmap=True)
        self.hdu = next(h for h in self.hdul
                        if h.data is not None and h.header["NAXIS"] == 3)
        hdr = self.hdu.header
        wcs = WCS(hdr)
        nv, ny, nx = self.hdu.data.shape

        # 1-D world axes (exact for CAR: lon depends on x only, lat on y)
        x = np.arange(nx)
        y = np.arange(ny)
        lon, _, _ = wcs.wcs_pix2world(x, np.full(nx, hdr["CRPIX2"] - 1),
                                      np.zeros(nx), 0)
        _, lat, _ = wcs.wcs_pix2world(np.full(ny, hdr["CRPIX1"] - 1), y,
                                      np.zeros(ny), 0)
        self.lon, self.lat = lon % 360.0, lat

        # spectral axis (VRAD, m/s)
        k = np.arange(nv)
        _, _, v = wcs.wcs_pix2world(np.full(nv, hdr["CRPIX1"] - 1),
                                    np.full(nv, hdr["CRPIX2"] - 1), k, 0)
        self.v_all = v
        self.f_all = F_HI * (1.0 - v / C_LIGHT)
        self.data_fwhm = 0.0
        self.min_fwhm = 0.2

    def use_full_cube(self):
        """Switch from the compact dataset to the full cube, keeping the
        current beam and band.  False if the full cube is not on disk
        (never triggers the 33 GiB download uninvited)."""
        if self.compact is None or not os.path.exists(self.full_path):
            return False
        print("Switching to the full HI4PI cube "
              "(the compact dataset cannot honour this request)...")
        self._load_full()
        self.set_beam(self.fwhm)
        self.set_band(self.bw_hz, self.fc)
        return True

    def use_compact_cube(self, fwhm, bw_hz, fc_hz):
        """The reverse of use_full_cube: drop back to the (cached)
        compact dataset once the requested beam and band fit it again.
        The requested band is applied here so the trimmed velocity axis
        is never indexed with the full cube's channel range."""
        c = self._compact_cache
        if self.compact is not None or c is None:
            return False
        pix = abs(np.median(np.diff(c.lat)))
        min_fwhm = round(np.sqrt(c.fwhm ** 2 + (2.355 * pix) ** 2), 2)
        # stay on the full cube only while it offers line data that the
        # compact one cannot; a band outside both is continuum-only
        # everywhere, so the compact dataset serves it just as well
        f = F_HI * (1.0 - c.v / C_LIGHT)
        in_compact = (fc_hz + bw_hz / 2 >= f.min()
                      and fc_hz - bw_hz / 2 <= f.max())
        df = F_HI * self.FULL_V_MS / C_LIGHT
        in_full = (fc_hz + bw_hz / 2 >= F_HI - df
                   and fc_hz - bw_hz / 2 <= F_HI + df)
        if fwhm < min_fwhm or (in_full and not in_compact):
            return False
        print("Returning to the compact dataset "
              "(the requested beam and band fit it again)...")
        self._load_compact(self.compact_path)
        self.set_beam(fwhm)
        self.set_band(bw_hz, fc_hz)
        return True

    def set_band(self, bw_hz, fc_hz):
        """Select the cube channels inside fc +/- bw/2.  A band with no
        overlap is accepted too (empty channel range: the spectrum is
        then continuum + noise only, for planning away from the line);
        the False return just reports that there is no line coverage."""
        self.bw_hz, self.fc = bw_hz, fc_hz
        band = np.abs(self.f_all - fc_hz) <= bw_hz / 2
        if not band.any():
            self.k0 = self.k1 = 0
            self.v = self.v_all[:0]
            self.f = self.f_all[:0]
            return False
        self.k0, self.k1 = np.where(band)[0][[0, -1]] + np.array([0, 1])
        self.v = self.v_all[self.k0:self.k1]
        self.f = self.f_all[self.k0:self.k1]
        return True

    def set_beam(self, fwhm_deg):
        if fwhm_deg < self.min_fwhm:
            print(f"Beam {fwhm_deg:.2f} deg is finer than this dataset "
                  f"supports; using {self.min_fwhm:.2f} deg.")
            fwhm_deg = self.min_fwhm
        self.fwhm = fwhm_deg
        # the compact sky is pre-smoothed to data_fwhm: weight with the
        # residual Gaussian so the total beam comes out as requested
        eff = np.sqrt(max(fwhm_deg ** 2 - self.data_fwhm ** 2, 1e-12))
        self.sigma = eff / (2 * np.sqrt(2 * np.log(2)))
        self.rmax = 1.5 * eff

    def continuum(self, glon, glat):
        """Beam-weighted continuum T_A: the bright point sources
        analytically (A_e follows from the beam via the antenna theorem:
        A_e * Omega_A = lambda^2, with Omega_A = 1.133 FWHM^2 / eta_mb),
        plus the diffuse 1420 MHz survey sky when the map is loaded."""
        lam2 = (C_LIGHT / F_HI) ** 2
        a_e = lam2 * self.eta / (1.133 * np.radians(self.fwhm) ** 2)
        sigma_b = self.fwhm / (2 * np.sqrt(2 * np.log(2)))
        total = 0.0
        for name, sl, sb, s_jy in self.sources:
            theta = haversine_deg(sl, sb, glon, glat)
            total += (s_jy * 1e-26 * a_e / (2 * K_B)
                      * np.exp(-0.5 * (theta / sigma_b) ** 2))
        if self.cmap is not None:
            total += self._map_continuum(glon, glat)
        return total

    def _map_continuum(self, glon, glat):
        """Beam-weighted diffuse continuum (K of T_A) from the compact
        1420 MHz map.  The map is pre-smoothed to cmap.fwhm, so only the
        residual beam is convolved; requested beams below that get the
        map's own resolution (a documented floor, irrelevant above
        ~1.5 deg)."""
        c = self.cmap
        pix = abs(c.lat[1] - c.lat[0])
        sig = max(np.sqrt(max(self.fwhm ** 2 - c.fwhm ** 2, 0.0)) / 2.355,
                  0.3 * pix)
        rmax = max(3 * sig, 1.5 * pix)
        rows = np.abs(c.lat - glat) <= rmax
        cosb = max(0.05, np.cos(np.radians(min(89.0, abs(glat) + rmax))))
        dl = (c.lon - glon + 180.0) % 360.0 - 180.0
        cols = np.abs(dl) * cosb <= rmax
        sub = c.t[np.ix_(rows, cols)]
        sep = haversine_deg(c.lon[cols][None, :], c.lat[rows][:, None],
                            glon, glat)
        w = np.exp(-0.5 * (sep / sig) ** 2) \
            * np.cos(np.radians(c.lat[rows]))[:, None]
        return self.eta * float((sub * w).sum() / w.sum())

    def spectrum(self, glon, glat):
        """Return (v_out [m/s], T_A [K], per-channel sigma or None)."""
        if self.k1 <= self.k0:
            # band entirely outside the H I coverage: the line signal is
            # zero everywhere, but continuum + noise are still right -
            # that is all a continuum SNR estimate needs
            df_nat = abs(np.median(np.diff(self.f_all))) \
                if self.f_all.size > 1 else 6.1e3
            n = self.nchan or max(2, int(round(self.bw_hz / df_nat)))
            f_edges = np.linspace(self.fc - self.bw_hz / 2,
                                  self.fc + self.bw_hz / 2, n + 1)
            f_out = 0.5 * (f_edges[:-1] + f_edges[1:])
            return self._finish(C_LIGHT * (F_HI - f_out) / F_HI,
                                np.zeros(n), self.bw_hz / n, glon, glat)
        rows = np.where(np.abs(self.lat - glat) <= self.rmax)[0]
        cosb = max(0.05, np.cos(np.radians(
            min(89.0, abs(glat) + self.rmax))))
        dl = (self.lon - glon + 180.0) % 360.0 - 180.0
        colmask = np.abs(dl) * cosb <= self.rmax
        if rows.size == 0 or not colmask.any():
            raise ValueError(
                f"beam footprint ({self.fwhm:g} deg FWHM) contains no "
                f"map pixels at l={glon:.2f}, b={glat:.2f}")
        y0, y1 = rows.min(), rows.max() + 1

        # subsample to match the beam: a Gaussian needs only ~FWHM/15
        # sampling, so wide beams read far less of the cube (a 40 deg
        # beam would otherwise pull in nearly the whole 33 GB file)
        pix = np.nanmedian(np.abs(np.diff(self.lat)))
        step = max(1, int(self.fwhm / (15.0 * pix)))

        # contiguous column runs (two if the box wraps l = 0)
        ci = np.where(colmask)[0]
        splits = np.where(np.diff(ci) > 1)[0]
        runs = np.split(ci, splits + 1)
        subs, lons = [], []
        for r in runs:
            if self.compact is not None:      # int16 cube already in RAM
                subs.append(self.compact.t[self.k0:self.k1, y0:y1:step,
                                           r[0]:r[-1] + 1:step]
                            .astype(np.float64) * self.compact.scale)
            else:
                # memmap slicing, not hdu.section: astropy's Section is
                # very slow with stepped slices, numpy strides the mmap
                subs.append(np.asarray(
                    self.hdu.data[self.k0:self.k1, y0:y1:step,
                                  r[0]:r[-1] + 1:step],
                    dtype=np.float64))
            lons.append(self.lon[r[0]:r[-1] + 1:step])
        sub = np.concatenate(subs, axis=2)
        lonb = np.concatenate(lons)
        latb = self.lat[y0:y1:step]

        sep = haversine_deg(lonb[None, :], latb[:, None], glon, glat)
        w = np.where(sep <= self.rmax,
                     np.exp(-0.5 * (sep / self.sigma) ** 2)
                     * np.cos(np.radians(latb))[:, None], 0.0)
        bad = ~np.isfinite(sub)
        sub[bad] = 0.0
        wsum = np.where(bad, 0.0, w[None, :, :]).sum(axis=(1, 2))
        t_a = self.eta * (sub * w[None, :, :]).sum(axis=(1, 2)) \
            / np.where(wsum > 0, wsum, np.nan)

        v_out, t_out = self.v, t_a
        df = abs(np.median(np.diff(self.f)))
        if self.nchan:
            f_edges = np.linspace(self.fc - self.bw_hz / 2,
                                  self.fc + self.bw_hz / 2, self.nchan + 1)
            f_out = 0.5 * (f_edges[:-1] + f_edges[1:])
            df = self.bw_hz / self.nchan
            order = np.argsort(self.f)
            # zero line (not NaN) beyond the survey coverage, so a band
            # hanging over the edge still gives a full noise spectrum
            t_out = np.interp(f_out, self.f[order], t_a[order],
                              left=0.0, right=0.0)
            v_out = C_LIGHT * (F_HI - f_out) / F_HI
        return self._finish(v_out, t_out, df, glon, glat)

    def _finish(self, v_out, t_out, df, glon, glat):
        """Add the continuum offset and radiometer noise to a line
        spectrum and return the (v, T_A, sigma, T_cont) tuple; sigma is
        per channel (or None with no radiometer noise)."""
        t_cont = self.continuum(glon, glat)
        t_out = t_out + t_cont                # flat continuum offset
        sigma_n = None
        if self.tsys is not None:
            # the signal is itself noise-like: line and continuum heat
            # the system, so channels on a bright line fluctuate more
            sigma_n = (self.tsys + np.maximum(t_out, 0.0)) \
                / np.sqrt(self.npol * df * self.tint)
            t_out = t_out + self.rng.normal(0.0, sigma_n)
        return v_out, t_out, sigma_n, t_cont


class FastTextBox(TextBox):
    """TextBox that repaints only its own axes while typing.

    Stock TextBox calls canvas.draw() — a full synchronous re-render of
    every artist on the figure — for every keystroke, which makes typing
    crawl on a canvas this busy.  Instead, cache the pixels behind the
    box once per focus session and blit just text + cursor over them."""

    def __init__(self, *args, **kwargs):
        self._bg = None
        super().__init__(*args, **kwargs)
        self.ax.figure.canvas.mpl_connect(
            "resize_event", lambda _e: setattr(self, "_bg", None))

    def begin_typing(self, *args, **kwargs):
        self._bg = None                    # recapture per focus session
        super().begin_typing(*args, **kwargs)

    def stop_typing(self):
        # stock TextBox stop_typing ends with a full synchronous
        # canvas.draw() and is called on EVERY canvas click for EVERY
        # box; with seven boxes that was ~0.5 s per map click.  Only a
        # box that was actually in a typing session needs any of it.
        if self.capturekeystrokes:
            super().stop_typing()

    def _blit(self):
        canvas = self.ax.figure.canvas
        if not canvas.supports_blit:
            type(canvas).draw(canvas)
            return
        if self._bg is None:
            vis = self.text_disp.get_visible(), self.cursor.get_visible()
            self.text_disp.set_visible(False)
            self.cursor.set_visible(False)
            type(canvas).draw(canvas)      # one real full draw to cache
            self._bg = canvas.copy_from_bbox(self.ax.bbox)
            self.text_disp.set_visible(vis[0])
            self.cursor.set_visible(vis[1])
        canvas.restore_region(self._bg)
        self.ax.draw_artist(self.text_disp)
        if self.cursor.get_visible():
            self.ax.draw_artist(self.cursor)
        canvas.blit(self.ax.bbox)

    def _rendercursor(self):
        # a programmatic set_val (map click, target pick) needs no
        # cursor and no synchronous draw: without this, every click paid
        # for two full canvas redraws before the spectrum one
        if not self.capturekeystrokes:
            self.ax.figure.canvas.draw_idle()
            return
        # the parent ends with canvas.draw(); intercept that one call so
        # its cursor-placement logic runs unchanged but the repaint is
        # only this axes (type(canvas).draw in _blit skips the override)
        canvas = self.ax.figure.canvas
        canvas.draw = self._blit
        try:
            super()._rendercursor()
        finally:
            del canvas.draw


def gauss_kernel(sigma_pix, half=None):
    half = half or max(1, int(np.ceil(4 * sigma_pix)))
    x = np.arange(-half, half + 1)
    k = np.exp(-0.5 * (x / sigma_pix) ** 2)
    return k / k.sum()


def smooth_to_beam(grid, lat_c, step, fwhm_deg):
    """Separable Gaussian smoothing on the CAR grid; the longitude kernel
    widens as 1/cos(b) so the smoothing is ~isotropic on the sky
    (approximate within a few % for |b| < 80 deg)."""
    sigma_deg = fwhm_deg / (2 * np.sqrt(2 * np.log(2)))
    filled = np.nan_to_num(grid, nan=0.0)
    mask = np.isfinite(grid).astype(float)

    out = np.empty_like(filled)
    outm = np.empty_like(filled)
    for j, b in enumerate(lat_c):                # along longitude, wrap
        s = sigma_deg / step / max(0.05, np.cos(np.radians(b)))
        k = gauss_kernel(min(s, filled.shape[1] / 6))
        h = len(k) // 2
        rowp = np.concatenate([filled[j, -h:], filled[j], filled[j, :h]])
        mrow = np.concatenate([mask[j, -h:], mask[j], mask[j, :h]])
        out[j] = np.convolve(rowp, k, mode="valid")
        outm[j] = np.convolve(mrow, k, mode="valid")

    k = gauss_kernel(sigma_deg / step)                   # along latitude
    h = len(k) // 2
    outp = np.pad(out, ((h, h), (0, 0)), mode="edge")
    mp = np.pad(outm, ((h, h), (0, 0)), mode="edge")
    for i in range(out.shape[1]):
        out[:, i] = np.convolve(outp[:, i], k, mode="valid")
        outm[:, i] = np.convolve(mp[:, i], k, mode="valid")
    with np.errstate(invalid="ignore"):
        return out / outm


def load_nhi_grid(path, step=0.5, cache="nhi_grid_cache.npy"):
    if os.path.exists(cache):
        return np.load(cache)         # the map file itself is not needed
    path = ensure_file(path)
    print("Gridding the N_HI map (first launch only)...")
    with fits.open(path, memmap=True) as hdul:
        t = hdul["HI4PI-HPX"].data
        glon = np.asarray(t["GLON"], dtype=np.float64) % 360.0
        glat = np.asarray(t["GLAT"], dtype=np.float64)
        nhi = np.asarray(t["NHI"], dtype=np.float64)
    nx, ny = int(360 / step), int(180 / step)
    good = np.isfinite(nhi)
    s, _, _ = np.histogram2d(glat[good], glon[good],
                             bins=[np.linspace(-90, 90, ny + 1),
                                   np.linspace(0, 360, nx + 1)],
                             weights=nhi[good])
    n, _, _ = np.histogram2d(glat[good], glon[good],
                             bins=[np.linspace(-90, 90, ny + 1),
                                   np.linspace(0, 360, nx + 1)])
    with np.errstate(invalid="ignore"):
        grid = s / n
    np.save(cache, grid)
    return grid


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cube", nargs="?", default="hi4pi_allsky_gal_CAR.fits",
                   help="all-sky HI4PI spectral cube (galactic CAR); "
                        "downloaded from CDS (~33 GiB) if missing")
    p.add_argument("--nhi", default="hi4pi.fits",
                   help="N_HI HEALPix file for the display map")
    p.add_argument("--compact", default=COMPACT_DEFAULT,
                   help="compact cube from hi4pi_compress.py, used "
                        "instead of the full cube when present")
    p.add_argument("--full", action="store_true",
                   help="use the full cube even if the compact file "
                        "exists (downloads ~33 GiB if missing)")
    p.add_argument("--continuum", default=CONTINUUM_DEFAULT,
                   help="compact 1420 MHz continuum map from "
                        "continuum_compress.py (diffuse sky under the "
                        "point sources; blank to disable)")
    p.add_argument("--bw", type=float, default=2.0, help="Bandwidth (MHz)")
    p.add_argument("--dish", type=float, default=3.0, help="Dish (m)")
    p.add_argument("--eta", type=float, default=MAIN_BEAM_EFFICIENCY,
                   help="Main-beam eff. (1.0: the measured beam is taken as the "
                        "whole pattern, so point sources are upper limits)")
    p.add_argument("--nchan", type=int, help="Spectrometer channels")
    p.add_argument("--tsys", type=float, default=200.0,
                   help="Tsys (K) for the noise (default 200; 0 = ideal "
                        "receiver, source self-noise only; clear the "
                        "box in the GUI to disable noise)")
    p.add_argument("--tint", type=float, default=60.0, help="Integration (s)")
    p.add_argument("--npol", type=int, default=1, help="Polarisations")
    p.add_argument("--controller", default="http://192.168.50.120",
                   help="SRT controller base URL for the Realise button")
    p.add_argument("--site", default=SITE_NAME, help="Observer site name")
    p.add_argument("--lat", type=float, default=SITE_LAT,
                   help="Site latitude (deg, +N)")
    p.add_argument("--lon", type=float, default=SITE_LON,
                   help="Site longitude (deg, +E)")
    p.add_argument("--height", type=float, default=SITE_HEIGHT,
                   help="Site height (m)")
    a = p.parse_args()
    set_site(a.site, a.lat, a.lon, a.height)

    compact = None
    if not a.full and os.path.exists(a.compact):
        compact = a.compact
        print(f"Using the compact HI4PI dataset ({a.compact}); "
              f"run with --full for the native-resolution cube.")
    else:
        a.cube = ensure_file(a.cube)
    sim = DishSimulator(a.cube, a.bw * 1e6, a.dish, a.eta,
                        a.nchan, a.tsys, a.tint, a.npol,
                        compact_path=compact,
                        continuum_path=a.continuum)
    grid = load_nhi_grid(a.nhi)
    ny, nx = grid.shape
    lon_c = (np.arange(nx) + 0.5) * 360.0 / nx
    lat_c = -90.0 + (np.arange(ny) + 0.5) * 180.0 / ny

    ink, accent = "#333639", "#3b7bbf"
    fig = plt.figure(figsize=(11, 10))
    fig.canvas.manager.set_window_title("HI4PI - click for a spectrum")
    # explicit placement: the map keeps clear of the control column on
    # the left, the spectrum floor clears the parameter-box labels, and
    # the band between spectrum top and map bottom must hold the top
    # velocity axis, its label and the title even in a shrunken window
    axm = fig.add_axes([0.165, 0.545, 0.735, 0.41],
                       projection="mollweide")
    axs = fig.add_axes([0.125, 0.16, 0.775, 0.285])

    lon_plot = -np.radians((lon_c + 180.0) % 360.0 - 180.0)
    o = np.argsort(lon_plot)
    LON, LAT = np.meshgrid(lon_plot[o], np.radians(lat_c))
    map_norm = LogNorm(vmin=4e19, vmax=2e22)
    cont_norm = LogNorm(vmin=0.05, vmax=60.0)
    map_step = 360.0 / nx
    map_state = {"fwhm": None, "mesh": None, "mode": "hi"}

    def update_map(fwhm, force=False):
        """Show the selected all-sky map (N_HI or 1420 MHz continuum)
        smoothed to the current beam, so the display matches what the
        dish can actually resolve."""
        if not force and map_state["fwhm"] is not None \
                and abs(fwhm - map_state["fwhm"]) < 0.01:
            return
        if map_state["mode"] == "cont":
            # the continuum map is stored already smoothed to its own
            # resolution; only the residual to the dish beam remains
            resid = np.sqrt(max(fwhm ** 2 - sim.cmap.fwhm ** 2, 0.0))
            sm = (smooth_to_beam(np.asarray(sim.cmap.t, dtype=np.float64),
                                 lat_c, map_step, resid)
                  if resid > 0.3 else np.asarray(sim.cmap.t))
            sm = np.maximum(sm, 0.01)
            norm = cont_norm
            title = ("1420 MHz continuum (Stockert/Villa-Elisa) - click "
                     f"to point the dish (beam {fwhm:.1f}°)")
        else:
            sm = smooth_to_beam(grid, lat_c, map_step, fwhm)
            norm = map_norm
            title = (f"HI4PI N$_{{HI}}$ - click to point the dish "
                     f"(beam {fwhm:.1f}°)")
        # display stride: once smoothed to the beam there is no detail
        # finer than ~fwhm/4, so bigger beams need far fewer quads and
        # every full canvas redraw (each widget keystroke!) gets cheaper
        ds = max(1, min(4, int(round(fwhm / 4.0 / map_step))))
        if map_state["mesh"] is not None:
            map_state["mesh"].remove()
        map_state["mesh"] = axm.pcolormesh(
            LON[::ds, ::ds], LAT[::ds, ::ds], sm[:, o][::ds, ::ds],
            cmap="inferno", norm=norm, rasterized=True, zorder=0)
        map_state["fwhm"] = fwhm
        axm.set_title(title, fontsize=11, color=ink)

    update_map(sim.fwhm)
    # the map is a fixed all-sky view; geographic axes already refuse
    # toolbar zoom/pan gestures (can_zoom/can_pan are False), but the
    # Home/Back/Forward restore path calls set_xlim, which geo axes
    # reject with TypeError, breaking those buttons for the whole figure
    # once anything had been zoomed — make the map's view save/restore a
    # no-op instead.  Don't set_navigate(False) here: that would also
    # stop the toolbar showing the l/b cursor readout via format_coord.
    axm._get_view = dict
    axm._set_view = lambda view: None
    axm.set_xticks(np.radians([-120, -60, 0, 60, 120]))
    axm.set_xticklabels(["120°", "60°", "0°", "300°", "240°"],
                        color="white", fontsize=8)
    # subtle dark halo behind every white map label: readable on the
    # bright galactic plane and where labels overhang onto the page
    text_halo = [pe.withStroke(linewidth=2, foreground="#2d2f32",
                               alpha=0.85)]
    for lbl in axm.get_xticklabels():
        lbl.set_path_effects(text_halo)
    axm.set_yticks(np.radians([-60, -30, 0, 30, 60]))
    axm.tick_params(axis="y", labelsize=8, colors="#555859")
    axm.grid(color="white", alpha=0.6, lw=0.8)
    beam_artist = [None]

    def map_coord(x, y):
        """Cursor readout in proper galactic convention (x is plotted
        as -l, so undo that and wrap to 0..360)."""
        if not (np.isfinite(x) and np.isfinite(y)):
            return ""
        return (f"l = {(-np.degrees(x)) % 360.0:.2f}°,  "
                f"b = {np.degrees(y):+.2f}°")

    axm.format_coord = map_coord

    # site visibility loops: constant-declination curves for the sky that
    # never rises (alt 0 at culmination) and that stays below alt 20; the
    # invisible side flips for a southern-hemisphere site
    def dec_loop(alt_deg, color, label):
        if SITE_LAT >= 0:
            dec, side = SITE_LAT - 90.0 + alt_deg, "<"
        else:
            dec, side = SITE_LAT + 90.0 - alt_deg, ">"
        circ = SkyCoord(ra=np.linspace(0, 360, 721) * u.deg,
                        dec=dec * u.deg, frame="icrs").galactic
        hx = -np.radians((circ.l.deg + 180.0) % 360.0 - 180.0)
        hy = np.radians(circ.b.deg)
        first = True
        for part in np.split(np.arange(len(hx)),
                             np.where(np.abs(np.diff(hx)) > 1.0)[0] + 1):
            axm.plot(hx[part], hy[part], color=color, lw=1.4, ls="--",
                     label=(f"{label} (dec {side} {dec:.0f}°)"
                            if first else None))
            first = False

    dec_loop(0.0, "#7fe07f", "never rises")
    dec_loop(20.0, "#ffd166", "alt always < 20°")

    # the horizon right now: the great circle 90 deg from the site's
    # zenith, redrawn once a minute as the sky turns
    horizon_art = []

    def draw_horizon(_=None):
        t = Time.now()
        aa = AltAz(obstime=t, location=SITE_LOC)
        az = np.linspace(0.0, 360.0, 721)
        hor = SkyCoord(az=az * u.deg, alt=np.zeros_like(az) * u.deg,
                       frame=aa).galactic
        hx = -np.radians((hor.l.deg + 180.0) % 360.0 - 180.0)
        hy = np.radians(hor.b.deg)
        for art in horizon_art:
            art.remove()
        horizon_art.clear()
        first = True
        outline = [pe.Stroke(linewidth=3.2, foreground="#55585b"),
                   pe.Normal()]
        for part in np.split(np.arange(len(hx)),
                             np.where(np.abs(np.diff(hx)) > 1.0)[0] + 1):
            horizon_art.append(
                axm.plot(hx[part], hy[part], color="white", lw=1.8,
                         ls="-.", path_effects=outline,
                         label=(f"horizon at {t.strftime('%H:%M')} UT"
                                if first else None))[0])
            first = False
        zen = SkyCoord(az=0 * u.deg, alt=90 * u.deg, frame=aa).galactic
        zx = -np.radians((zen.l.deg + 180.0) % 360.0 - 180.0)
        zy = np.radians(zen.b.deg)
        horizon_art.append(axm.plot(zx, zy, "+", ms=9, mew=1.8,
                                    color="white",
                                    path_effects=outline)[0])
        horizon_art.append(axm.annotate(
            "zenith", (zx, zy), xytext=(5, 4),
            textcoords="offset points", fontsize=7.5, color="white",
            path_effects=text_halo))
        axm.legend(loc="lower right", bbox_to_anchor=(1.12, -0.02),
                   fontsize=7.5, frameon=True, framealpha=0.9,
                   edgecolor="#c7cacd", labelcolor=ink,
                   title=f"from {SITE_NAME} (inside loop)",
                   title_fontsize=7.5)
        fig.canvas.draw_idle()

    draw_horizon()
    timer = fig.canvas.new_timer(interval=60000)
    timer.add_callback(draw_horizon)
    timer.start()
    fig._hi4pi_timer = timer

    # bright continuum sources
    for name, sl, sb, _flux in sim.sources:
        mx = -np.radians((sl + 180.0) % 360.0 - 180.0)
        my = np.radians(sb)
        colr = {"Sun": "#ffe14d", "Moon": "#d5d8dc"}.get(name, "white")
        axm.plot(mx, my, "o", ms=6, mfc=colr, mec="#333639", mew=0.8)
        axm.annotate(name, (mx, my), xytext=(6, 5),
                     textcoords="offset points", fontsize=8,
                     color="white", path_effects=text_halo)
    for name, sl, sb in LANDMARKS:
        mx = -np.radians((sl + 180.0) % 360.0 - 180.0)
        my = np.radians(sb)
        axm.plot(mx, my, "D", ms=5, mfc="#a8e6ff", mec="#333639", mew=0.8)
        axm.annotate(name, (mx, my), xytext=(6, 5),
                     textcoords="offset points", fontsize=8, color="white",
                     path_effects=text_halo)

    axs.set_xlabel("LSR radial velocity  (km s$^{-1}$)")
    axs.set_ylabel("$T_A$  (K)")
    axs.text(0.5, 0.5, "click the map", transform=axs.transAxes,
             ha="center", va="center", color="#9a9da0", fontsize=14)
    axs.grid(color="#eceeef", lw=0.7)
    axs.set_axisbelow(True)
    state = {"last": None, "frame": "lsr", "params": None}
    FRAME_NAMES = {"lsr": "LSR", "ssb": "SSB",
                   "topo": f"Topo ({SITE_NAME})"}
    VEL_LABELS = {"lsr": "LSR radial velocity",
                  "ssb": "SSB (barycentric) radial velocity",
                  "topo": f"topocentric radial velocity ({SITE_NAME}, now)"}

    # ------- parameter bar: one row of boxes, labels above ----------------
    def add_box(x, y, w, label, initial):
        tb = FastTextBox(fig.add_axes([x, y, w, 0.038]), label,
                         initial=initial, textalignment="center")
        tb.label.set_fontsize(8)
        tb.label.set_position((0.5, 1.25))
        tb.label.set_ha("center")
        tb.label.set_va("bottom")
        return tb

    # grouped: pointing | band & beam | radiometer
    ROW = 0.025
    tb_l = add_box(0.035, ROW, 0.095, "l (°)", "132.0")
    tb_b = add_box(0.145, ROW, 0.095, "b (°)", "-1.0")
    tb_fw = add_box(0.28, ROW, 0.095, "beam (°)", f"{sim.fwhm:.2f}")
    tb_bw = add_box(0.39, ROW, 0.095, "BW (MHz)", f"{a.bw:g}")
    tb_fc = add_box(0.50, ROW, 0.095, "$f_c$ (MHz)", f"{F_HI/1e6:.2f}")
    fc_shown = [tb_fc.text]     # what the box displays (2 dp); the
    #                             exact value in use lives in sim.fc
    # shows the effective startup count: --nchan if given, else the
    # band's native channel count; an emptied box = native resolution
    nc_default = a.nchan or (len(sim.f) if len(sim.f) > 1
                             else max(2, int(round(sim.bw_hz / 6.1e3))))
    tb_nc = add_box(0.61, ROW, 0.095, "channels", f"{nc_default:d}")
    tb_ts = add_box(0.755, ROW, 0.095, "$T_{sys}$ (K)",
                    "" if a.tsys is None else f"{a.tsys:g}")
    tb_ti = add_box(0.865, ROW, 0.095, "$\\tau$ (s)", f"{a.tint:g}")

    # ------- control stack in the free column left of the map -------------
    btn_fr = Button(fig.add_axes([0.015, 0.770, 0.13, 0.045]),
                    "Frame: LSR", color="#f0ede4", hovercolor="#e4dfd0")
    btn_fr.label.set_fontsize(9)

    # ------- targets dropdown ---------------------------------------------
    # H I targets plus the analytic continuum point sources (the Sun's
    # entry uses its position at launch time)
    # the [continuum] tag marks what the signal is: H I targets
    # (galactic or extragalactic) are untagged even when they sit on a
    # bright continuum background; the point sources are continuum
    src_desc = {"Cyg A": "radio galaxy", "Cas A": "supernova remnant",
                "Tau A": "Crab nebula", "Sun": "launch position",
                "Moon": "launch position"}
    targets = [(f"{nm} ({ds})", tl, tb_deg, bw)
               for nm, tl, tb_deg, bw, ds in TARGETS]
    targets += [(f"{nm} ({src_desc[nm]})  [continuum]", sl, sb, 2.0)
                for nm, sl, sb, _flux in sim.sources]
    dd_ax = fig.add_axes([0.155, 0.545, 0.34, 0.40], zorder=10)
    # never let the toolbar pan/zoom touch this axes: it overlaps the
    # spectrum panel, and zooming there would silently rescale the
    # (invisible) menu, blanking its labels and breaking row hit-testing
    dd_ax.set_navigate(False)
    dd_ax.set_xlim(0, 1)
    dd_ax.set_ylim(0, 1)
    dd_ax.set_xticks([])
    dd_ax.set_yticks([])
    dd_ax.set_facecolor("#fbfcfd")
    for sp in dd_ax.spines.values():
        sp.set_color("#c7cacd")
    for i, (nm, tl, tb_deg, _bw) in enumerate(targets):
        yy = 1 - (i + 0.5) / len(targets)
        if i:
            dd_ax.axhline(1 - i / len(targets), color="#eceeef", lw=0.6)
        tdec = SkyCoord(l=tl * u.deg, b=tb_deg * u.deg,
                        frame="galactic").icrs.dec.deg
        gone = never_rises(tdec)
        dd_ax.text(0.04, yy, nm + ("   [never rises]" if gone else ""),
                   fontsize=8, va="center",
                   color="#b0b3b6" if gone else ink)
        dd_ax.text(0.97, yy, f"({tl:.1f}°, {tb_deg:+.1f}°)", fontsize=7,
                   va="center", ha="right", color="#8b8e91")
    dd_ax.set_visible(False)

    btn_tg = Button(fig.add_axes([0.015, 0.900, 0.13, 0.045]),
                    "Targets ▾", color="#e8f4e8", hovercolor="#d4ead4")
    btn_tg.label.set_fontsize(9)

    def toggle_targets(_event=None):
        dd_ax.set_visible(not dd_ax.get_visible())
        fig.canvas.draw_idle()

    btn_tg.on_clicked(toggle_targets)

    # ------- map display toggle (N_HI <-> 1420 MHz continuum) --------
    btn_map = Button(fig.add_axes([0.015, 0.835, 0.13, 0.045]),
                     "Map: H I", color="#f4ede8", hovercolor="#eaddd4")
    btn_map.label.set_fontsize(9)

    def toggle_map(_event=None):
        if sim.cmap is None:
            print("No continuum map loaded (continuum_1420_compact"
                  ".npz.xz missing) - only the N_HI display available.")
            return
        map_state["mode"] = "cont" if map_state["mode"] == "hi" else "hi"
        cont = map_state["mode"] == "cont"
        btn_map.label.set_text("Map: 1420" if cont else "Map: H I")
        # grey out what the mode makes irrelevant: the frame only
        # relabels the spectrum, the scan length only shapes drift scans
        btn_fr.label.set_color("#b0b3b6" if cont else "black")
        tb_sd.label.set_color("#333639" if cont else "#b0b3b6")
        update_map(sim.fwhm, force=True)
        # the lower panel is modal: spectrum with the H I map, drift
        # scan with the continuum map
        if state["last"]:
            if map_state["mode"] == "cont":
                render_drift()
            else:
                clear_scan_track()
                render()
        else:
            if map_state["mode"] != "cont":
                clear_scan_track()
            fig.canvas.draw_idle()

    btn_map.on_clicked(toggle_map)

    # ------- drift-scan duration (continuum-map mode) ----------------
    tb_sd = add_box(0.015, 0.675, 0.13, "scan (min)", "240")
    tb_sd.label.set_color("#b0b3b6")      # greyed until drift mode
    scan_artist = [None]

    def clear_scan_track():
        if scan_artist[0]:
            for art in scan_artist[0]:
                art.remove()
            scan_artist[0] = None

    def draw_scan_track(sl, sb):
        """Dashed constant-declination line on the map showing the
        stretch of sky the drift scan sweeps through."""
        clear_scan_track()
        sx = -np.radians((sl + 180.0) % 360.0 - 180.0)
        sy = np.radians(sb)
        seg = np.where(np.abs(np.diff(sx)) > 1.0)[0]      # split at wrap
        arts = []
        for part in np.split(np.arange(len(sx)), seg + 1):
            arts.append(axm.plot(sx[part], sy[part], color="#4dd2ff",
                                 lw=1.2, ls="--", zorder=3)[0])
        arts.append(axm.plot(sx[[0, -1]], sy[[0, -1]], ls="none",
                             marker=".", ms=5, color="#4dd2ff",
                             zorder=3)[0])
        scan_artist[0] = arts

    def render_drift():
        """Draw a drift scan through the current pointing in the lower
        panel (continuum-map mode): band-averaged T_A along the
        constant-declination track of a parked beam, centred on
        beam-centre transit, spanning the duration in the scan box -
        valid for any target, above the horizon or not, with the H I
        line included whenever the band covers it."""
        glon, glat = state["last"][0], state["last"][1]
        try:
            dur = float(tb_sd.text)
        except ValueError:
            dur = 240.0
        dur = min(1435.0, max(2.0, dur))
        if abs(float(tb_sd.text or "0") - dur) > 1e-9:
            tb_sd.eventson = False
            tb_sd.set_val(f"{dur:g}")
            tb_sd.eventson = True
            print(f"Clamped scan duration to {dur:g} min")
        c = SkyCoord(l=glon * u.deg, b=glat * u.deg, frame="galactic")
        ra0, dec = c.icrs.ra.deg, c.icrs.dec.deg
        cosd = max(0.02, np.cos(np.radians(dec)))
        n = 151
        dt_h = np.linspace(-dur / 120.0, dur / 120.0, n)
        # the beam centre moves east through the sky at the sidereal
        # rate, so at time t it sits at RA = ra0 + 15.041 t
        sky = SkyCoord(ra=(ra0 + 15.041 * dt_h) * u.deg,
                       dec=np.full(n, dec) * u.deg).galactic
        draw_scan_track(sky.l.deg, sky.b.deg)
        keep_tsys, sim.tsys = sim.tsys, None      # noiseless band means
        try:
            tbar = np.array([np.nanmean(sim.spectrum(li, bi)[1])
                             for li, bi in zip(sky.l.deg, sky.b.deg)])
        finally:
            sim.tsys = keep_tsys
        v_ax = state["last"][2]
        f_ax = F_HI * (1.0 - v_ax / C_LIGHT)
        bw_use = (len(f_ax) * abs(np.median(np.diff(f_ax)))
                  if f_ax.size > 1 else sim.bw_hz)
        # the tau box sets the integration time per sample; the scan
        # duration only sets how many samples there are, so the noise
        # per sample must not depend on it
        tau_s = sim.tint
        mins = dt_h * 60.0
        axs.clear()
        noise = ""
        if keep_tsys is not None and dur * 60.0 >= tau_s:
            n_smp = int(dur * 60.0 / tau_s)
            if n_smp > 20000:
                print(f"Drift scan: showing 20000 of {n_smp} samples "
                      f"(tau {tau_s:g} s over {dur:g} min)")
                n_smp = 20000
            t_smp = np.linspace(-dur / 120.0, dur / 120.0, n_smp)
            tb_smp = np.interp(t_smp, dt_h, tbar)
            sig = (keep_tsys + tb_smp) / np.sqrt(
                sim.npol * bw_use * tau_s)
            axs.plot(t_smp * 60.0, tb_smp + sim.rng.normal(0.0, sig),
                     ".", ms=2.5 if n_smp > 500 else 3.5,
                     color="#9aa3ac", rasterized=n_smp > 2000)
            noise = (f",  $\\tau$/sample {tau_s:g} s, "
                     f"$\\sigma\\approx${np.median(sig) * 1e3:.0f} mK")
        axs.plot(mins, tbar, color=accent, lw=1.5)
        half = (sim.fwhm / 2) / (15.041 * cosd) * 60.0
        for xx in (-half, half):
            axs.axvline(xx, color="#c7cacd", lw=1.0, ls="--")
        axs.set_xlabel("minutes from beam-centre transit")
        axs.set_ylabel("band-averaged $T_A$  (K)")
        axs.set_title(f"drift scan  l={glon:.1f}°, b={glat:.1f}° "
                      f"(dec {dec:+.1f}°)   BW {bw_use / 1e6:.2g} MHz "
                      f"at {sim.fc / 1e6:.1f} MHz{noise}",
                      fontsize=10, color=ink)
        axs.grid(color="#eceeef", lw=0.7)
        axs.set_axisbelow(True)
        draw_beam(glon, glat)
        fig.canvas.draw_idle()

    def on_scan_len(_event=None):
        if map_state["mode"] == "cont" and state["last"]:
            render_drift()

    tb_sd.on_submit(on_scan_len)

    # ------- realise: hand the simulated observation to the SRT -------
    btn_rl = Button(fig.add_axes([0.015, 0.600, 0.13, 0.045]), "Realise",
                    color="#e2ecf8", hovercolor="#cfe0f2")
    btn_rl.label.set_fontsize(9)

    def controller_call(endpoint, params=None):
        url = a.controller.rstrip("/") + endpoint
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                return json.loads(r.read().decode(errors="replace"))
        except Exception as err:
            print(f"SRT controller not reachable ({url}): {err}")
            return None

    def realise(_event=None):
        """Send the current simulation to the real telescope: with the
        H I map, ask the controller to track the pointing (galactic
        coordinates); with the continuum map, cancel tracking and park
        the dish at the drift-scan start, so the target transits the
        beam centre half a scan from now."""
        p = apply_params()
        if not p:
            return
        glon, glat = p
        if map_state["mode"] != "cont":
            print(f"Realise: asking the SRT to track "
                  f"l={glon:.2f}°, b={glat:.2f}°...")
            r = controller_call("/track/galactic",
                                {"l": round(glon, 3),
                                 "b": round(glat, 3)})
        else:
            try:
                dur = min(1435.0, max(2.0, float(tb_sd.text)))
            except ValueError:
                dur = 240.0
            c = SkyCoord(l=glon * u.deg, b=glat * u.deg,
                         frame="galactic")
            # the beam centre sweeps east at the sidereal rate, so park
            # half a scan west of the target (in RA) and let it drift in
            start = SkyCoord(ra=(c.icrs.ra.deg
                                 - 15.041 * dur / 120.0) * u.deg,
                             dec=c.icrs.dec.deg * u.deg)
            aa = start.transform_to(AltAz(obstime=Time.now(),
                                          location=SITE_LOC))
            if aa.alt.deg <= 0:
                print(f"Realise: the drift-scan start is below the "
                      f"horizon (alt {aa.alt.deg:.1f}°) - not sent.")
                return
            print(f"Realise: parking the SRT at alt {aa.alt.deg:.1f}°, "
                  f"az {aa.az.deg:.1f}°; the target transits the beam "
                  f"centre in {dur / 2:.0f} min...")
            controller_call("/tracking/enable", {"enable": 0})
            r = controller_call("/direct", {"alt": round(aa.alt.deg, 2),
                                            "az": round(aa.az.deg, 2)})
        if r is not None:
            print(f"  controller: {r}")

    btn_rl.on_clicked(realise)

    # ------- pointing readout at the figure's top-right edge ----------
    info_txt = axm.text(1.12, 1.02, "", transform=axm.transAxes,
                        va="top", ha="right", fontsize=8, color=ink,
                        linespacing=1.5)

    def update_info(_=None):
        """Refresh the pointing readout: the current l/b in equatorial
        coordinates, and where it sits in the sky over the site now."""
        try:
            glon = float(tb_l.text) % 360.0
            glat = float(tb_b.text)
        except ValueError:
            return
        c = SkyCoord(l=glon * u.deg, b=glat * u.deg, frame="galactic")
        icrs = c.icrs
        aa = c.transform_to(AltAz(obstime=Time.now(), location=SITE_LOC))
        ra_h = int(icrs.ra.hour)
        ra_m = (icrs.ra.hour - ra_h) * 60
        dec = icrs.dec.deg
        d_d, d_m = int(abs(dec)), (abs(dec) - int(abs(dec))) * 60
        info_txt.set_text(
            f"RA {ra_h:02d}h {ra_m:04.1f}m   "
            f"Dec {'-' if dec < 0 else '+'}{d_d:02d}° {d_m:04.1f}′\n"
            f"Alt {aa.alt.deg:+.1f}°   Az {aa.az.deg:.1f}°"
            + ("" if aa.alt.deg > 0 else "   (below horizon)"))
        fig.canvas.draw_idle()

    timer.add_callback(update_info)       # alt/az drift with the sky

    def apply_params():
        """Read the parameter boxes into the simulator; returns (l, b).
        Out-of-range values are clamped to physical limits and written
        back into the boxes so what you see is what runs."""
        try:
            glon = float(tb_l.text) % 360.0
            glat = min(90.0, max(-90.0, float(tb_b.text)))
            fwhm = abs(float(tb_fw.text))
            tint = abs(float(tb_ti.text))
            # empty box = no noise model; 0 is a valid (ideal) receiver
            # whose spectra and drift scans still carry source self-noise
            tsys = float(tb_ts.text) if tb_ts.text.strip() else None
            bw_hz = abs(float(tb_bw.text)) * 1e6
            fc_text = tb_fc.text.strip()
            fc_hz = float(fc_text) * 1e6
            nc_text = tb_nc.text.strip()
            nchan = int(float(nc_text)) if nc_text else None
        except ValueError:
            print("Could not parse the parameter boxes.")
            return None
        # the box displays fc to 2 dp but the value in use keeps every
        # supplied digit: an unchanged display means "keep the exact
        # current fc", and a typed value that ROUNDS to the rest
        # frequency at its own precision means the exact rest frequency
        if fc_text == fc_shown[0]:
            fc_hz = sim.fc
        else:
            dec = len(fc_text.split(".")[1]) if "." in fc_text else 0
            if abs(fc_hz - F_HI) < 0.5 * 10.0 ** (6 - min(dec, 6)):
                fc_hz = F_HI
        # clamp to what the loaded dataset supports (the compact cube is
        # pre-smoothed; a finer beam needs the full cube, if it is here)
        # and drop back to the compact dataset when the request fits it
        went_compact = sim.compact is None and sim.use_compact_cube(
            fwhm if fwhm > 0 else sim.fwhm, bw_hz, fc_hz)
        if 0 < fwhm < sim.min_fwhm and sim.compact is not None \
                and not went_compact and not sim.use_full_cube():
            print(f"Beams below {sim.min_fwhm:.1f} deg need the full "
                  f"cube ({sim.full_path}): rerun with --full to "
                  f"download and use it.")
        fwhm = min(90.0, max(sim.min_fwhm, fwhm)) if fwhm > 0 else 0.0
        tint = min(1e7, max(1e-3, tint))
        if tsys is not None:
            tsys = min(1e6, max(0.0, tsys))
        bw_hz = min(8e6, max(2e4, bw_hz))
        if nchan is not None:
            nchan = min(65536, max(2, nchan))
        fixups = ((tb_b, f"{glat:g}"),
                  (tb_fw, f"{fwhm:g}" if fwhm > 0 else f"{sim.fwhm:g}"),
                  (tb_ti, f"{tint:g}"), (tb_bw, f"{bw_hz/1e6:g}"),
                  (tb_nc, "" if nchan is None else f"{nchan:d}"))
        for tb, val in fixups:
            if tb.text.strip() and abs(float(tb.text) - float(val)) > 1e-9:
                tb.eventson = False
                tb.set_val(val)
                tb.eventson = True
                print(f"Clamped {tb.label.get_text().strip()} to {val}")
        if fwhm > 0 and (went_compact or abs(fwhm - sim.fwhm) > 1e-6):
            sim.set_beam(fwhm)
            update_map(sim.fwhm)
        sim.tsys = tsys
        sim.tint = tint if tint > 0 else 1.0
        sim.nchan = nchan
        if bw_hz > 0 and (abs(bw_hz - sim.bw_hz) > 1
                          or abs(fc_hz - sim.fc) > 1):
            ok = sim.set_band(bw_hz, fc_hz)
            # the compact cube trims the velocity range; the full cube
            # may still hold line data for the requested band
            df = F_HI * sim.FULL_V_MS / C_LIGHT
            in_full = (fc_hz + bw_hz / 2 >= F_HI - df
                       and fc_hz - bw_hz / 2 <= F_HI + df)
            if not ok and sim.compact is not None and in_full \
                    and sim.use_full_cube():
                ok = sim.set_band(bw_hz, fc_hz)
            if not ok:
                # band accepted anyway: zero line, continuum + noise
                note = (" (the full cube would cover it: rerun with "
                        "--full)" if in_full and sim.compact is not None
                        else "")
                print(f"Band has no H I coverage: the spectrum is "
                      f"continuum + noise only{note}.")
        # show the applied fc back at display precision
        fc_shown[0] = f"{sim.fc / 1e6:.2f}"
        if tb_fc.text.strip() != fc_shown[0]:
            tb_fc.eventson = False
            tb_fc.set_val(fc_shown[0])
            tb_fc.eventson = True
        return glon, glat

    def to_lb(event):
        if event.inaxes is not axm or event.xdata is None \
                or not (np.isfinite(event.xdata)
                        and np.isfinite(event.ydata)):
            return None      # outside the projection ellipse
        lon = (-np.degrees(event.xdata)) % 360.0
        lat = np.degrees(event.ydata)
        return lon, lat

    def draw_beam(glon, glat):
        t = np.linspace(0, 2 * np.pi, 100)
        r = sim.fwhm / 2
        b = np.clip(glat + r * np.cos(t), -89.9, 89.9)
        l_ = glon + r * np.sin(t) / np.cos(np.radians(b))
        x = -np.radians((l_ + 180.0) % 360.0 - 180.0)
        y = np.radians(b)
        seg = np.where(np.abs(np.diff(x)) > 1.0)[0]     # split at wrap
        if beam_artist[0]:
            for art in beam_artist[0]:
                art.remove()
        arts = []
        for part in np.split(np.arange(len(x)), seg + 1):
            arts.append(axm.plot(x[part], y[part], color="#4dd2ff",
                                 lw=1.5)[0])
        beam_artist[0] = arts

    def render():
        """Draw the stored spectrum in the currently selected frame.
        The line is the same; only the frequency/velocity axes shift by
        the line-of-sight frame velocity (constant across the band)."""
        glon, glat, v, t_a, sig, t_cont = state["last"]
        frame = state["frame"]
        dv = frame_offset(glon, glat, frame)
        vf = v + dv
        axs.clear()
        f_mhz = F_HI * (1.0 - vf / C_LIGHT) / 1e6
        axs.plot(f_mhz, t_a, color=accent, lw=1.3, drawstyle="steps-mid")
        axs.axhline(0, color="#c7cacd", lw=0.8, zorder=0)
        axs.set_xlabel(f"Frequency  (MHz, {FRAME_NAMES[frame]})")
        axs.set_ylabel("$T_A$  (K)")
        axs.xaxis.set_major_formatter(
            plt.FuncFormatter(lambda xx, _: f"{xx:.2f}"))
        noise = ("" if sig is None
                 else f",  $\\sigma_0$={np.nanmin(sig)*1e3:.0f} mK")
        cont = f",  continuum offset {t_cont:.2f} K" if t_cont > 0.005 else ""
        shift = ("" if frame == "lsr"
                 else f",  frame shift {dv/1e3:+.1f} km/s")
        axs.set_title(f"l={glon:.1f}°, b={glat:.1f}°   "
                      f"peak $T_A$={np.nanmax(t_a):.1f} K{noise}{cont}"
                      f"{shift}", fontsize=10, color=ink)
        axs.grid(color="#eceeef", lw=0.7)
        axs.set_axisbelow(True)
        sec = axs.secondary_xaxis(
            "top",
            functions=(lambda ff: C_LIGHT * (F_HI - ff * 1e6) / F_HI / 1e3,
                       lambda vv: (F_HI * (1 - vv * 1e3 / C_LIGHT)) / 1e6))
        sec.set_xlabel(VEL_LABELS[frame] + "  (km s$^{-1}$)", fontsize=9)
        sec.tick_params(labelsize=8)
        draw_beam(glon, glat)
        fig.canvas.draw_idle()

    def point(glon, glat):
        try:
            v, t_a, sig, t_cont = sim.spectrum(glon, glat)
        except ValueError as err:
            print(f"No spectrum: {err}")
            return
        state["last"] = (glon, glat, v, t_a, sig, t_cont)
        state["params"] = (glon, glat, sim.fwhm, sim.tsys, sim.tint,
                           sim.bw_hz, sim.fc, sim.nchan)
        update_info()
        if map_state["mode"] == "cont":
            render_drift()
        else:
            render()

    def select_target(i):
        name, tl, tb_deg, req_bw = targets[i]
        for tb, val in ((tb_l, f"{tl:.2f}"), (tb_b, f"{tb_deg:.2f}")):
            tb.eventson = False
            tb.set_val(val)
            tb.eventson = True
        if sim.bw_hz < req_bw * 1e6 - 1:      # widen band only if needed
            tb_bw.eventson = False
            tb_bw.set_val(f"{req_bw:g}")
            tb_bw.eventson = True
        dd_ax.set_visible(False)
        print(f"Target: {name}")
        p = apply_params()
        if p:
            point(*p)

    def on_click(event):
        if dd_ax.get_visible():
            if event.inaxes is btn_tg.ax:
                return                # let the button's own toggle run
            if event.inaxes is dd_ax and event.ydata is not None:
                i = min(len(targets) - 1,
                        max(0, int((1 - event.ydata) * len(targets))))
                select_target(i)
            else:
                dd_ax.set_visible(False)      # click-away closes it
                fig.canvas.draw_idle()
            return
        # no toolbar-mode guard: geo axes refuse pan/zoom gestures, so a
        # click on the map is always a pointing request, even while the
        # zoom/pan tool is still selected
        lb = to_lb(event)
        if lb is None:
            return
        for tb, val in ((tb_l, f"{lb[0]:.2f}"), (tb_b, f"{lb[1]:.2f}")):
            tb.eventson = False
            tb.set_val(val)
            tb.eventson = True
        p = apply_params()
        if p:
            point(*p)

    def on_go(_event=None):
        """Recompute only if a parameter really changed: TextBox fires
        submit whenever a box loses focus, and recomputing then made
        every click feel sluggish."""
        p = apply_params()
        if not p:
            return
        params = (p[0], p[1], sim.fwhm, sim.tsys, sim.tint,
                  sim.bw_hz, sim.fc, sim.nchan)
        if params != state["params"]:
            point(*p)

    def on_frame(_event=None):
        order = ["lsr", "ssb", "topo"]
        state["frame"] = order[(order.index(state["frame"]) + 1) % 3]
        btn_fr.label.set_text("Frame: " + FRAME_NAMES[state["frame"]])
        # the frame only relabels the spectrum axes; the drift view
        # (continuum-map mode) is frame-independent
        if state["last"] and map_state["mode"] != "cont":
            render()
        else:
            fig.canvas.draw_idle()

    btn_fr.on_clicked(on_frame)
    for tb in (tb_l, tb_b, tb_fw, tb_ts, tb_ti, tb_bw, tb_fc, tb_nc):
        tb.on_submit(on_go)

    def on_key(event):
        if event.key == "s" and state["last"]:
            glon, glat, v, t_a, _sig, _tc = state["last"]
            frame = state["frame"]
            vf = v + frame_offset(glon, glat, frame)
            base = f"spectrum_l{glon:+07.2f}_b{glat:+06.2f}"
            np.savetxt(base + ".txt", np.column_stack([vf / 1e3, t_a]),
                       header=f"v_{frame}_km/s   T_A_K", fmt="%.6e")
            fig.savefig(base + ".png", dpi=150)
            print(f"Saved {base}.png and {base}.txt ({frame} frame)")

    def on_scroll(event):
        """Wheel-zoom the spectrum about the cursor (up = in); the
        velocity axis on top follows automatically."""
        if event.inaxes is not axs or event.xdata is None:
            return
        f = 1 / 1.3 if event.button == "up" else 1.3
        x0, x1 = axs.get_xlim()
        y0, y1 = axs.get_ylim()
        axs.set_xlim(event.xdata - (event.xdata - x0) * f,
                     event.xdata + (x1 - event.xdata) * f)
        axs.set_ylim(event.ydata - (event.ydata - y0) * f,
                     event.ydata + (y1 - event.ydata) * f)
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect("button_press_event", on_click)
    fig.canvas.mpl_connect("key_press_event", on_key)
    fig.canvas.mpl_connect("scroll_event", on_scroll)

    # ------- toolbar Home also resets every parameter ----------------
    initials = {tb: tb.text for tb in (tb_l, tb_b, tb_fw, tb_ts, tb_ti,
                                       tb_bw, tb_fc, tb_nc, tb_sd)}

    def reset_params(_event=None):
        """Return every parameter box and the velocity frame to their
        startup values, then recompute the current display."""
        for tb, val in initials.items():
            tb.eventson = False
            tb.set_val(val)
            tb.eventson = True
        state["frame"] = "lsr"
        btn_fr.label.set_text("Frame: LSR")
        print("Parameters reset to startup values.")
        p = apply_params()
        if p and state["last"]:
            point(*p)
        else:
            update_info()
            fig.canvas.draw_idle()

    tbar = getattr(fig.canvas, "toolbar", None)
    if tbar is not None:
        _orig_home = tbar.home

        def _home_and_reset(*args, **kwargs):
            _orig_home(*args, **kwargs)
            reset_params()

        # covers the keyboard shortcut ('h'/'home'), which looks the
        # method up at call time...
        tbar.home = _home_and_reset
        # ...but the Tk toolbar button captured the original bound
        # method when it was created, so rebind its command too
        home_btn = getattr(tbar, "_buttons", {}).get("Home")
        if home_btn is not None:
            home_btn.config(command=_home_and_reset)

    fig._hi4pi_widgets = (tb_l, tb_b, tb_fw, tb_ts, tb_ti, tb_bw, tb_fc,
                          tb_nc, btn_fr, btn_tg, tb_sd, btn_map, btn_rl)
    fig._hi4pi_reset = reset_params
    update_info()
    plt.show()


if __name__ == "__main__":
    main()
