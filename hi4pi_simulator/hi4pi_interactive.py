#!/usr/bin/env python3
"""
Interactive HI4PI sky: click the all-sky map, get the 3-m dish spectrum.

Opens a window with the HI4PI column-density map (Mollweide, galactic).
Left-click anywhere on the map: the beam-weighted antenna-temperature
spectrum for that pointing is computed from the all-sky spectral cube
and drawn in the lower panel, with the beam footprint shown on the map.

Usage:
    python hi4pi_interactive.py
    python hi4pi_interactive.py --bw 2 --tsys 100 --tint 60 --nchan 1024

The all-sky cube (~33 GiB) and the N_HI display map are downloaded from
CDS on first run if not already present (see hi4pi_data.py); the N_HI
map is gridded once and cached in nhi_grid_cache.npy.
Press "s" to save the current spectrum to PNG + txt.
"""

import argparse
import os
import sys
import warnings

import numpy as np
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
from matplotlib.widgets import Button, TextBox
from astropy import units as u
from astropy.coordinates import (AltAz, EarthLocation, FK4, SkyCoord,
                                 get_sun)
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS

from hi4pi_data import ensure_file

# the cursor moving outside the Mollweide ellipse makes matplotlib's
# inverse projection hit arcsin(|x| > 1); harmless, so keep it quiet
# (filtered by module so our own arcsin in haversine_deg still warns)
warnings.filterwarnings("ignore",
                        message="invalid value encountered in arcsin",
                        module=r"matplotlib\.projections\.geo")

# ---- observer site: edit these defaults, or override at runtime with
# ---- --site/--lat/--lon/--height (visibility loops, live horizon and
# ---- the topocentric frame all follow automatically)
SITE_NAME = "Glasgow"
SITE_LAT = 55.87        # deg, +N
SITE_LON = -4.29        # deg, +E
SITE_HEIGHT = 50.0      # m
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
TARGETS = [
    ("Galactic centre wings", 0.0, 0.0, 3.0),
    ("Inner Galaxy terminal vel. (l=30)", 30.0, 0.0, 2.0),
    ("Cygnus X (l=80)", 80.0, 0.0, 2.0),
    ("Outer Arm (l=110)", 110.0, 0.0, 2.0),
    ("Perseus arm (l=134)", 134.0, -1.0, 2.0),
    ("Anticentre (l=180)", 180.0, 0.0, 2.0),
    ("M31 (Andromeda)", 121.17, -21.57, 5.0),
    ("M33 (Triangulum)", 133.6, -31.3, 3.0),
    ("LMC", 280.5, -32.9, 5.0),
    ("SMC", 302.8, -44.3, 3.0),
    ("HVC Complex A", 150.0, 35.0, 3.0),
    ("HVC Complex C", 100.0, 45.0, 3.0),
    ("Smith Cloud", 39.0, -13.0, 2.0),
]


def continuum_sources():
    """The bright continuum sources: (name, l, b, flux at 1420 MHz in Jy).
    Sun position is for launch time; quiet-Sun flux (active Sun is 10-100x)."""
    cyg = SkyCoord(ra=299.868 * u.deg, dec=40.734 * u.deg).galactic
    cas = SkyCoord(ra=350.850 * u.deg, dec=58.815 * u.deg).galactic
    # direction-only: get_sun carries the Earth-Sun distance, and a 3-D
    # transform to Galactic would re-centre on the solar-system
    # barycentre, giving a meaningless direction (the Sun IS the
    # barycentre, near enough)
    sun_gcrs = get_sun(Time.now())
    sun = SkyCoord(ra=sun_gcrs.ra, dec=sun_gcrs.dec).galactic
    return [("Cyg A", cyg.l.deg, cyg.b.deg, 1590.0),
            ("Cas A", cas.l.deg, cas.b.deg, 1500.0),
            ("Sun", sun.l.deg, sun.b.deg, 5.0e5)]


C_LIGHT = 299792458.0
F_HI = 1420405751.768


def haversine_deg(l1, b1, l2, b2):
    """Angular separation in deg; inputs in deg, broadcastable."""
    l1, b1, l2, b2 = map(np.radians, (l1, b1, l2, b2))
    s = np.sin((b1 - b2) / 2) ** 2 \
        + np.cos(b1) * np.cos(b2) * np.sin((l1 - l2) / 2) ** 2
    return np.degrees(2 * np.arcsin(np.sqrt(s)))


def frame_offset(glon, glat, frame):
    """Velocity (m/s) to ADD to a v_LSRK axis to express the spectrum in
    `frame`: 'lsr' (native), 'ssb' (solar-system barycentre) or 'topo'
    (the observer site, evaluated at the current time).

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
            "barycentric", obstime=Time.now(), location=SITE_LOC)
        dv -= rv.to_value(u.m / u.s)
    return dv


class DishSimulator:
    """Holds the open cube + precomputed axes; computes spectra quickly."""

    def __init__(self, cube_path, bw_hz, dish_m, eta, nchan=None,
                 tsys=None, tint=60.0, npol=2):
        self.bw_hz, self.eta = bw_hz, eta
        self.nchan, self.tsys, self.tint, self.npol = nchan, tsys, tint, npol
        self.dish_m = dish_m
        self.set_beam(np.degrees(1.22 * (C_LIGHT / F_HI) / dish_m))
        self.sources = continuum_sources()

        self.hdul = fits.open(cube_path, memmap=True)
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

        # spectral axis (VRAD, m/s) and the in-band channel range
        k = np.arange(nv)
        _, _, v = wcs.wcs_pix2world(np.full(nv, hdr["CRPIX1"] - 1),
                                    np.full(nv, hdr["CRPIX2"] - 1), k, 0)
        self.v_all = v
        self.f_all = F_HI * (1.0 - v / C_LIGHT)
        self.rng = np.random.default_rng()
        if not self.set_band(bw_hz, F_HI):
            sys.exit("Bandwidth does not overlap the cube's velocity range.")

    def set_band(self, bw_hz, fc_hz):
        """Select the cube channels inside fc +/- bw/2; False if no overlap."""
        band = np.abs(self.f_all - fc_hz) <= bw_hz / 2
        if not band.any():
            return False
        self.bw_hz, self.fc = bw_hz, fc_hz
        self.k0, self.k1 = np.where(band)[0][[0, -1]] + np.array([0, 1])
        self.v = self.v_all[self.k0:self.k1]
        self.f = self.f_all[self.k0:self.k1]
        return True

    def set_beam(self, fwhm_deg):
        self.fwhm = fwhm_deg
        self.sigma = fwhm_deg / (2 * np.sqrt(2 * np.log(2)))
        self.rmax = 1.5 * fwhm_deg

    def continuum(self, glon, glat):
        """Beam-weighted continuum T_A from the bright point sources.
        A_e follows from the beam via the antenna theorem:
        A_e * Omega_A = lambda^2, with Omega_A = 1.133 FWHM^2 / eta_mb."""
        lam2 = (C_LIGHT / F_HI) ** 2
        a_e = lam2 * self.eta / (1.133 * np.radians(self.fwhm) ** 2)
        total = 0.0
        for name, sl, sb, s_jy in self.sources:
            theta = haversine_deg(sl, sb, glon, glat)
            total += (s_jy * 1e-26 * a_e / (2 * K_B)
                      * np.exp(-0.5 * (theta / self.sigma) ** 2))
        return total

    def spectrum(self, glon, glat):
        """Return (v_out [m/s], T_A [K], sigma_noise or None)."""
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
            # memmap slicing, not hdu.section: astropy's Section is very
            # slow with stepped slices, numpy strides the mmap directly
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
            t_out = np.interp(f_out, self.f[order], t_a[order],
                              left=np.nan, right=np.nan)
            v_out = C_LIGHT * (F_HI - f_out) / F_HI
        t_cont = self.continuum(glon, glat)
        t_out = t_out + t_cont                # flat continuum offset
        sigma_n = None
        if self.tsys is not None:             # sources also heat the system
            sigma_n = (self.tsys + t_cont) \
                / np.sqrt(self.npol * df * self.tint)
            t_out = t_out + self.rng.normal(0.0, sigma_n, t_out.shape)
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
    p.add_argument("--bw", type=float, default=2.0, help="Bandwidth (MHz)")
    p.add_argument("--dish", type=float, default=3.0, help="Dish (m)")
    p.add_argument("--eta", type=float, default=0.7, help="Main-beam eff.")
    p.add_argument("--nchan", type=int, help="Spectrometer channels")
    p.add_argument("--tsys", type=float, help="Tsys (K) -> add noise")
    p.add_argument("--tint", type=float, default=60.0, help="Integration (s)")
    p.add_argument("--npol", type=int, default=2, help="Polarisations")
    p.add_argument("--site", default=SITE_NAME, help="Observer site name")
    p.add_argument("--lat", type=float, default=SITE_LAT,
                   help="Site latitude (deg, +N)")
    p.add_argument("--lon", type=float, default=SITE_LON,
                   help="Site longitude (deg, +E)")
    p.add_argument("--height", type=float, default=SITE_HEIGHT,
                   help="Site height (m)")
    a = p.parse_args()
    set_site(a.site, a.lat, a.lon, a.height)

    a.cube = ensure_file(a.cube)
    sim = DishSimulator(a.cube, a.bw * 1e6, a.dish, a.eta,
                        a.nchan, a.tsys, a.tint, a.npol)
    grid = load_nhi_grid(a.nhi)
    ny, nx = grid.shape
    lon_c = (np.arange(nx) + 0.5) * 360.0 / nx
    lat_c = -90.0 + (np.arange(ny) + 0.5) * 180.0 / ny

    ink, accent = "#333639", "#3b7bbf"
    fig = plt.figure(figsize=(11, 10))
    fig.canvas.manager.set_window_title("HI4PI - click for a spectrum")
    gs = fig.add_gridspec(2, 1, height_ratios=[1.4, 1.0], hspace=0.3,
                          top=0.96, bottom=0.21)
    axm = fig.add_subplot(gs[0], projection="mollweide")
    axs = fig.add_subplot(gs[1])

    lon_plot = -np.radians((lon_c + 180.0) % 360.0 - 180.0)
    o = np.argsort(lon_plot)
    LON, LAT = np.meshgrid(lon_plot[o], np.radians(lat_c))
    map_norm = LogNorm(vmin=4e19, vmax=2e22)
    map_step = 360.0 / nx
    map_state = {"fwhm": None, "mesh": None}

    def update_map(fwhm):
        """Show the N_HI map smoothed to the current beam, so the display
        matches what the dish can actually resolve."""
        if map_state["fwhm"] is not None \
                and abs(fwhm - map_state["fwhm"]) < 0.01:
            return
        sm = smooth_to_beam(grid, lat_c, map_step, fwhm)
        # display stride: once smoothed to the beam there is no detail
        # finer than ~fwhm/4, so bigger beams need far fewer quads and
        # every full canvas redraw (each widget keystroke!) gets cheaper
        ds = max(1, min(4, int(round(fwhm / 4.0 / map_step))))
        if map_state["mesh"] is not None:
            map_state["mesh"].remove()
        map_state["mesh"] = axm.pcolormesh(
            LON[::ds, ::ds], LAT[::ds, ::ds], sm[:, o][::ds, ::ds],
            cmap="inferno", norm=map_norm, rasterized=True, zorder=0)
        map_state["fwhm"] = fwhm

    update_map(sim.fwhm)
    axm.set_xticks(np.radians([-120, -60, 0, 60, 120]))
    axm.set_xticklabels(["120°", "60°", "0°", "300°", "240°"],
                        color="white", fontsize=8)
    axm.set_yticks(np.radians([-60, -30, 0, 30, 60]))
    axm.tick_params(axis="y", labelsize=8, colors="#555859")
    axm.grid(color="white", alpha=0.6, lw=0.8)
    axm.set_title(f"HI4PI N$_{{HI}}$ - click to point the "
                  f"{a.dish:.0f}-m dish (beam {sim.fwhm:.1f}°)",
                  fontsize=11, color=ink)
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
            textcoords="offset points", fontsize=7.5, color="white"))
        axm.legend(loc="lower right", bbox_to_anchor=(1.19, -0.02),
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
        colr = "#ffe14d" if name == "Sun" else "white"
        axm.plot(mx, my, "o", ms=6, mfc=colr, mec="#333639", mew=0.8)
        axm.annotate(name, (mx, my), xytext=(6, 5),
                     textcoords="offset points", fontsize=8,
                     color="white")
    for name, sl, sb in LANDMARKS:
        mx = -np.radians((sl + 180.0) % 360.0 - 180.0)
        my = np.radians(sb)
        axm.plot(mx, my, "D", ms=5, mfc="#a8e6ff", mec="#333639", mew=0.8)
        axm.annotate(name, (mx, my), xytext=(6, 5),
                     textcoords="offset points", fontsize=8, color="white")

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

    # ------- parameter bar (two rows) -------------------------------------
    def add_box(x, y, w, label, initial):
        tb = FastTextBox(fig.add_axes([x, y, w, 0.04]), label,
                         initial=initial, textalignment="center")
        tb.label.set_fontsize(9)
        return tb

    ROW1, ROW2 = 0.095, 0.03
    tb_l = add_box(0.10, ROW1, 0.09, "l (°) ", "132.0")
    tb_b = add_box(0.27, ROW1, 0.09, "b (°) ", "-1.0")
    tb_fw = add_box(0.46, ROW1, 0.08, "beam (°) ", f"{sim.fwhm:.2f}")
    tb_ts = add_box(0.62, ROW1, 0.08, "$T_{sys}$ (K) ",
                    "" if a.tsys is None else f"{a.tsys:g}")
    tb_ti = add_box(0.76, ROW1, 0.07, "$\\tau$ (s) ", f"{a.tint:g}")
    tb_bw = add_box(0.10, ROW2, 0.09, "BW (MHz) ", f"{a.bw:g}")
    tb_fc = add_box(0.32, ROW2, 0.13, "$f_c$ (MHz) ", f"{F_HI/1e6:.4f}")
    btn_fr = Button(fig.add_axes([0.50, ROW2, 0.20, 0.04]), "Frame: LSR",
                    color="#f0ede4", hovercolor="#e4dfd0")
    btn_fr.label.set_fontsize(9)

    # ------- targets dropdown ---------------------------------------------
    dd_ax = fig.add_axes([0.51, 0.145, 0.34, 0.40], zorder=10)
    dd_ax.set_xlim(0, 1)
    dd_ax.set_ylim(0, 1)
    dd_ax.set_xticks([])
    dd_ax.set_yticks([])
    dd_ax.set_facecolor("#fbfcfd")
    for sp in dd_ax.spines.values():
        sp.set_color("#c7cacd")
    for i, (nm, tl, tb_deg, _bw) in enumerate(TARGETS):
        yy = 1 - (i + 0.5) / len(TARGETS)
        if i:
            dd_ax.axhline(1 - i / len(TARGETS), color="#eceeef", lw=0.6)
        tdec = SkyCoord(l=tl * u.deg, b=tb_deg * u.deg,
                        frame="galactic").icrs.dec.deg
        gone = never_rises(tdec)
        dd_ax.text(0.04, yy, nm + ("   [never rises]" if gone else ""),
                   fontsize=8, va="center",
                   color="#b0b3b6" if gone else ink)
        dd_ax.text(0.97, yy, f"({tl:.1f}°, {tb_deg:+.1f}°)", fontsize=7,
                   va="center", ha="right", color="#8b8e91")
    dd_ax.set_visible(False)

    btn_tg = Button(fig.add_axes([0.74, ROW2, 0.11, 0.04]), "Targets ▾",
                    color="#e8f4e8", hovercolor="#d4ead4")
    btn_tg.label.set_fontsize(9)

    def toggle_targets(_event=None):
        dd_ax.set_visible(not dd_ax.get_visible())
        fig.canvas.draw_idle()

    btn_tg.on_clicked(toggle_targets)

    def apply_params():
        """Read the parameter boxes into the simulator; returns (l, b).
        Out-of-range values are clamped to physical limits and written
        back into the boxes so what you see is what runs."""
        try:
            glon = float(tb_l.text) % 360.0
            glat = min(90.0, max(-90.0, float(tb_b.text)))
            fwhm = abs(float(tb_fw.text))
            tint = abs(float(tb_ti.text))
            tsys = float(tb_ts.text) if tb_ts.text.strip() else 0.0
            bw_hz = abs(float(tb_bw.text)) * 1e6
            fc_hz = float(tb_fc.text) * 1e6
        except ValueError:
            print("Could not parse the parameter boxes.")
            return None
        # clamp to sane ranges (beam >= 2 cube pixels, else the beam
        # footprint can contain no map rows at all and the sum is empty)
        fwhm = min(90.0, max(0.2, fwhm)) if fwhm > 0 else 0.0
        tint = min(1e7, max(1e-3, tint))
        tsys = min(1e6, max(0.0, tsys))
        bw_hz = min(8e6, max(2e4, bw_hz))
        fixups = ((tb_b, f"{glat:g}"),
                  (tb_fw, f"{fwhm:g}" if fwhm > 0 else f"{sim.fwhm:g}"),
                  (tb_ti, f"{tint:g}"), (tb_bw, f"{bw_hz/1e6:g}"))
        for tb, val in fixups:
            if tb.text.strip() and abs(float(tb.text) - float(val)) > 1e-9:
                tb.eventson = False
                tb.set_val(val)
                tb.eventson = True
                print(f"Clamped {tb.label.get_text().strip()} to {val}")
        if fwhm > 0 and abs(fwhm - sim.fwhm) > 1e-6:
            sim.set_beam(fwhm)
            axm.set_title(f"HI4PI N$_{{HI}}$ - click to point the dish "
                          f"(beam {sim.fwhm:.1f}°)", fontsize=11, color=ink)
            update_map(sim.fwhm)
        sim.tsys = tsys if tsys > 0 else None
        sim.tint = tint if tint > 0 else 1.0
        if bw_hz > 0 and (abs(bw_hz - sim.bw_hz) > 1
                          or abs(fc_hz - sim.fc) > 1):
            if not sim.set_band(bw_hz, fc_hz):
                print("Requested band lies entirely outside the HI4PI "
                      "coverage (1420.4 MHz +/- ~2.9 MHz); keeping the "
                      "previous band.")
                tb_bw.eventson = tb_fc.eventson = False
                tb_bw.set_val(f"{sim.bw_hz/1e6:g}")
                tb_fc.set_val(f"{sim.fc/1e6:.4f}")
                tb_bw.eventson = tb_fc.eventson = True
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
        axs.plot(f_mhz, t_a, color=accent, lw=1.3)
        axs.axhline(0, color="#c7cacd", lw=0.8, zorder=0)
        axs.set_xlabel(f"Frequency  (MHz, {FRAME_NAMES[frame]})")
        axs.set_ylabel("$T_A$  (K)")
        axs.xaxis.set_major_formatter(
            plt.FuncFormatter(lambda xx, _: f"{xx:.2f}"))
        noise = "" if sig is None else f",  $\\sigma$={sig*1e3:.0f} mK"
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
                           sim.bw_hz, sim.fc)
        render()

    def select_target(i):
        name, tl, tb_deg, req_bw = TARGETS[i]
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
                i = min(len(TARGETS) - 1,
                        max(0, int((1 - event.ydata) * len(TARGETS))))
                select_target(i)
            else:
                dd_ax.set_visible(False)      # click-away closes it
                fig.canvas.draw_idle()
            return
        tbar = getattr(fig.canvas, "toolbar", None)
        if tbar is not None and getattr(tbar, "mode", ""):
            return        # toolbar pan/zoom active: don't repoint the dish
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
                  sim.bw_hz, sim.fc)
        if params != state["params"]:
            point(*p)

    def on_frame(_event=None):
        order = ["lsr", "ssb", "topo"]
        state["frame"] = order[(order.index(state["frame"]) + 1) % 3]
        btn_fr.label.set_text("Frame: " + FRAME_NAMES[state["frame"]])
        if state["last"]:
            render()
        else:
            fig.canvas.draw_idle()

    btn_fr.on_clicked(on_frame)
    for tb in (tb_l, tb_b, tb_fw, tb_ts, tb_ti, tb_bw, tb_fc):
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
    fig._hi4pi_widgets = (tb_l, tb_b, tb_fw, tb_ts, tb_ti, tb_bw, tb_fc,
                          btn_fr, btn_tg)
    plt.show()


if __name__ == "__main__":
    main()
