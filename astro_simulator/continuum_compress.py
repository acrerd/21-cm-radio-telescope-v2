#!/usr/bin/env python3
"""
Build the compact 1420 MHz continuum map for the dish simulator.

Source: the Stockert/Villa-Elisa 1420 MHz all-sky continuum survey
(Reich 1982; Reich & Reich 1986; Reich, Testori & Reich 2001) - the
only absolutely calibrated full-sky continuum map at the H I frequency.
Input is the CADE HEALPix regridding (Nside 256, NESTED, galactic,
mK full-beam T_B, 35.4' resolution) mirrored at LAMBDA and fetched
automatically by hi4pi_data.py (3.2 MiB).

Processing mirrors hi4pi_compress.py: bin to the same 0.5 deg CAR grid,
smooth to a 1.0 deg total resolution (recorded in the file, so the
simulator convolves only the residual beam), subtract the uniform zero
level (CMB + isotropic background, stored as t_zero) and LZMA-compress.
Output is a few hundred kB; astro_simulator.py uses it, when present,
as the continuum sky under the H I line - which also makes the noise
estimate pointing-dependent, since the galactic background heats the
system just like a source does.

    python continuum_compress.py
"""

import argparse
import io
import lzma
import os
import types

import numpy as np
from astropy.io import fits

from hi4pi_compress import smooth_grid
from hi4pi_data import ensure_file

CONTINUUM_DEFAULT = "continuum_1420_compact.npz.xz"
HPX_FILE = "stockert_villaelisa_1420MHz_healpix.fits"
NATIVE_FWHM = 35.4 / 60.0            # deg, survey beam

# the survey's full-beam calibration and limited dynamic range suppress
# the strongest compact sources by factors of 3-5 (Cas A is blanked
# outright, -32768 sentinels).  The simulator carries these analytically
# at their true fluxes, so their residual imprint is removed from the
# map here: (name, l, b); Cas A's blanked hole is inpainted first.
STRONG = [("Cas A", 111.73, -2.13),
          ("Cyg A", 76.19, 5.75),
          ("Tau A", 184.55, -5.79)]

# The imprint is wider than the formal 35.4' beam: Tau A's radial profile
# shows a skirt reaching ~1.2 deg that a single 0.65 deg FWHM Gaussian
# misses by a factor 2 at 0.4-0.7 deg, which is what left a halo around
# all three sources and ~0.5 K of double counting at a Cas A beam
# crossing (issue #23).  The shape is therefore *fitted* from Tau A -
# isolated, off the plane - as a core + skirt Gaussian pair, and that
# two-component shape is scaled to each source.  The background annulus
# sits outside the fitted skirt, and the per-source amplitude is a
# least-squares fit of the shape to the ring profile rather than the
# single pixel peak.
BG_ANNULUS = (3.5, 4.5)              # deg, outside the imprint skirt
FIT_RMAX = 1.75                      # deg, rings the amplitude is fit on
RING_DR = 0.25                       # deg, radial bin width

# Cas A sits on the Galactic ridge, and its blanked hole used to be
# filled with a flat annulus median taken from inside its own halo -
# 2.8-2.9 K across the hole against 1.2-2.2 K on the neighbouring
# longitudes.  The ridge's latitude structure is what a flat fill
# destroys, so the hole is inpainted by continuing the ridge instead:
# each hole pixel interpolates in longitude between the same latitude
# row in these two windows, both clear of the imprint.  Keyed by source
# because the windows are specific to Cas A's stretch of the ridge; a
# hole appearing at another source would need its own pair chosen, not
# these applied blindly.
RIDGE_WINDOWS = {"Cas A": ((106.0, 108.0), (115.0, 117.0))}


def _compress_even_bits(v):
    """Keep the even-position bits of v and pack them contiguously."""
    v = v & 0x55555555
    v = (v | (v >> 1)) & 0x33333333
    v = (v | (v >> 2)) & 0x0F0F0F0F
    v = (v | (v >> 4)) & 0x00FF00FF
    v = (v | (v >> 8)) & 0x0000FFFF
    return v


def nest_pix2lonlat(nside, ipix):
    """Galactic (lon, lat) in deg of NESTED HEALPix pixel centres.
    Vectorized transcription of the standard HEALPix pix2ang_nest
    (Gorski et al. 2005); avoids a healpy dependency for one call."""
    ipix = np.asarray(ipix, dtype=np.int64)
    face = ipix // (nside * nside)
    p = (ipix % (nside * nside)).astype(np.uint32)
    x = _compress_even_bits(p).astype(np.int64)
    y = _compress_even_bits(p >> 1).astype(np.int64)

    jrll = np.array([2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4])[face]
    jpll = np.array([1, 3, 5, 7, 0, 2, 4, 6, 1, 3, 5, 7])[face]
    jr = jrll * nside - x - y - 1          # ring index, 1..4*nside-1

    nr = np.full(jr.shape, nside, dtype=np.int64)
    z = np.empty(jr.shape, dtype=np.float64)
    kshift = np.zeros(jr.shape, dtype=np.int64)

    north = jr < nside
    south = jr > 3 * nside
    eq = ~(north | south)
    nr[north] = jr[north]
    z[north] = 1.0 - nr[north] ** 2 / (3.0 * nside ** 2)
    nr[south] = 4 * nside - jr[south]
    z[south] = -1.0 + nr[south] ** 2 / (3.0 * nside ** 2)
    z[eq] = (2 * nside - jr[eq]) * 2.0 / (3.0 * nside)
    kshift[eq] = (jr[eq] - nside) & 1

    jp = (jpll * nr + x - y + 1 + kshift) // 2
    jp = np.where(jp > 4 * nr, jp - 4 * nr, jp)
    jp = np.where(jp < 1, jp + 4 * nr, jp)
    phi = (jp - (kshift + 1) * 0.5) * (np.pi / (2 * nr))
    return np.degrees(phi) % 360.0, 90.0 - np.degrees(np.arccos(z))


def _ring_profile(t_k, mask, sep, rmax, dr=RING_DR):
    """Median map value in `dr`-wide rings of separation out to `rmax`.
    Returns (ring centres, medians); a ring with no valid pixel is NaN."""
    edges = np.arange(0.0, rmax + dr / 2, dr)
    med = np.full(edges.size - 1, np.nan)
    for i in range(edges.size - 1):
        ring = mask & (sep >= edges[i]) & (sep < edges[i + 1])
        if ring.any():
            med[i] = np.median(t_k[ring])
    return edges[:-1] + dr / 2, med


def _fit_imprint_shape(r, excess):
    """Core + skirt Gaussian pair fitted to a radial excess profile.

    Widths on a grid, amplitudes by linear least squares at each pair -
    dependency-free and exhaustive at the scales that matter.  Returns
    (sigma_core, sigma_skirt, amp_core, amp_skirt) with the amplitudes
    normalised so the shape is 1 at r = 0."""
    ok = np.isfinite(excess)
    r, y = r[ok], excess[ok]
    best = None
    for s1 in np.arange(0.15, 0.45, 0.01):
        for s2 in np.arange(s1 + 0.1, 1.5, 0.02):
            m = np.column_stack([np.exp(-0.5 * (r / s1) ** 2),
                                 np.exp(-0.5 * (r / s2) ** 2)])
            a, *_ = np.linalg.lstsq(m, y, rcond=None)
            if a.min() < 0:
                continue
            resid = float(((m @ a - y) ** 2).sum())
            if best is None or resid < best[0]:
                best = (resid, s1, s2, a)
    _, s1, s2, a = best
    total = a.sum()
    return s1, s2, a[0] / total, a[1] / total


def _imprint(sep, s1, s2, a1, a2):
    """The fitted imprint shape (1 at the centre) at separations `sep`."""
    return (a1 * np.exp(-0.5 * (sep / s1) ** 2)
            + a2 * np.exp(-0.5 * (sep / s2) ** 2))


def compress(out_path, res=0.5, fwhm=1.0, preset=6):
    path = ensure_file(HPX_FILE)
    with fits.open(path) as hdul:
        hdu = next(h for h in hdul[1:] if h.data is not None)
        if hdu.header.get("ORDERING", "NESTED").upper() != "NESTED":
            raise ValueError("expected a NESTED HEALPix map")
        nside = int(hdu.header["NSIDE"])
        t_mk = np.ravel(np.asarray(
            hdu.data[hdu.data.dtype.names[0]], dtype=np.float64))
    good = np.isfinite(t_mk) & (t_mk > -3e4)      # mask blank sentinels
    lon, lat = nest_pix2lonlat(nside, np.arange(t_mk.size))
    t_k = t_mk / 1e3                              # mK -> K
    print(f"{HPX_FILE}: Nside {nside}, {good.sum()} of {t_mk.size} "
          f"pixels valid, T = {t_k[good].min():.2f}.."
          f"{t_k[good].max():.0f} K")

    def sep_to(tl, tb):
        dlon = (lon - tl + 180.0) % 360.0 - 180.0
        return np.sqrt((dlon * np.cos(np.radians(tb))) ** 2
                       + (lat - tb) ** 2)

    # The imprint shape, fitted from Tau A: isolated and off the plane,
    # so its ring profile above the distant background is the survey's
    # response to a point source, skirt included.
    tau_l, tau_b = next((tl, tb) for n, tl, tb in STRONG if n == "Tau A")
    sep = sep_to(tau_l, tau_b)
    bg_ann = good & (sep > BG_ANNULUS[0]) & (sep < BG_ANNULUS[1])
    r, prof = _ring_profile(t_k, good, sep, FIT_RMAX)
    s1, s2, a1, a2 = _fit_imprint_shape(r, prof - np.median(t_k[bg_ann]))
    print(f"  imprint shape from Tau A: core sigma {s1:.2f} deg "
          f"({a1:.0%}), skirt sigma {s2:.2f} deg ({a2:.0%})")

    # Inpaint the blanked Cas A hole by continuing the Galactic ridge:
    # each hole pixel interpolates in longitude between the same
    # latitude row in the two RIDGE_WINDOWS.  The inpainted pixels carry
    # no imprint, so the subtraction below skips them (orig_good).
    orig_good = good.copy()
    for name, tl, tb in STRONG:
        sep = sep_to(tl, tb)
        hole = np.flatnonzero(~good & (sep < 3.0))
        if not hole.size:
            continue
        if name not in RIDGE_WINDOWS:
            raise ValueError(f"{name} has {hole.size} blanked pixels but "
                             "no ridge windows defined - choose a pair "
                             "clear of its imprint rather than reusing "
                             "another source's")
        (l0a, l0b), (l1a, l1b) = RIDGE_WINDOWS[name]
        c0, c1 = (l0a + l0b) / 2, (l1a + l1b) / 2
        for i in hole:
            row = np.abs(lat - lat[i]) <= RING_DR
            v0 = np.median(t_k[good & row & (lon >= l0a) & (lon <= l0b)])
            v1 = np.median(t_k[good & row & (lon >= l1a) & (lon <= l1b)])
            f = (lon[i] - c0) / (c1 - c0)
            t_k[i] = v0 + (v1 - v0) * f
        good[hole] = True
        print(f"  {name}: inpainted {hole.size} blanked pixels by "
              f"continuing the ridge from l={l0a:g}-{l0b:g} and "
              f"{l1a:g}-{l1b:g}")

    # Remove each source's imprint: the fitted shape, scaled to the
    # source's own ring profile by least squares rather than to the
    # single-pixel peak (Cas A's peak is blanked; a ring profile is
    # what survives).  Only original survey pixels are touched - the
    # inpainted ridge has no imprint in it.
    for name, tl, tb in STRONG:
        sep = sep_to(tl, tb)
        bg = np.median(t_k[orig_good & (sep > BG_ANNULUS[0])
                           & (sep < BG_ANNULUS[1])])
        r, prof = _ring_profile(t_k, orig_good, sep, FIT_RMAX)
        y = prof - bg
        ok = np.isfinite(y)
        shape_r = _imprint(r[ok], s1, s2, a1, a2)
        amp = float((shape_r * y[ok]).sum() / (shape_r ** 2).sum())
        if amp > 0:
            near = orig_good & (sep < 6.0)
            t_k[near] -= amp * _imprint(sep[near], s1, s2, a1, a2)
            print(f"  {name}: removed {amp:.1f} K imprint "
                  f"(map imprint; analytic source used instead)")
    lon, lat, t_k = lon[good], lat[good], t_k[good]

    # bin onto the same 0.5 deg CAR grid the compact H I cube uses
    nx, ny = int(round(360 / res)), int(round(180 / res))
    s, _, _ = np.histogram2d(lat, lon, bins=[
        np.linspace(-90, 90, ny + 1), np.linspace(0, 360, nx + 1)],
        weights=t_k)
    n, _, _ = np.histogram2d(lat, lon, bins=[
        np.linspace(-90, 90, ny + 1), np.linspace(0, 360, nx + 1)])
    with np.errstate(invalid="ignore"):
        grid = s / n
    lat_c = -90.0 + (np.arange(ny) + 0.5) * res
    lon_c = (np.arange(nx) + 0.5) * res

    # smooth to `fwhm` total: the survey beam and the binning boxcar are
    # already in the data (boxcar of width res ~ Gaussian 0.68*res FWHM)
    have2 = NATIVE_FWHM ** 2 + (0.68 * res) ** 2
    resid = np.sqrt(fwhm ** 2 - have2)
    sm = smooth_grid(grid, lat_c, res, resid).astype(np.float32)
    if not np.isfinite(sm).all():
        raise ValueError(f"{np.sum(~np.isfinite(sm))} empty cells "
                         "after smoothing")

    # uniform zero level (CMB + isotropic background + survey zero):
    # a total-power scan only measures contrast, and the user's Tsys
    # estimate already contains the uniform sky, so store it separately
    t_zero = float(np.percentile(sm, 0.1))
    sm -= t_zero

    buf = io.BytesIO()
    np.savez(buf, t=sm, lon=lon_c, lat=lat_c, fwhm=np.float64(fwhm),
             t_zero=np.float64(t_zero))
    tmp = out_path + ".tmp"
    with lzma.open(tmp, "wb", preset=preset) as f:
        f.write(buf.getvalue())
    os.replace(tmp, out_path)
    print(f"zero level {t_zero:.2f} K; wrote {out_path}: "
          f"{os.path.getsize(out_path) / 2**20:.2f} MB "
          f"({fwhm:g} deg resolution, {res:g} deg pixels)")


def load_continuum(path):
    """Load a compact continuum map; returns a namespace with .t (K of
    galactic emission above the uniform zero level, lat x lon), .lon /
    .lat (deg), .fwhm (deg, resolution already in the data) and .t_zero
    (the subtracted uniform level in K)."""
    with lzma.open(path, "rb") as f:
        npz = np.load(io.BytesIO(f.read()))
        return types.SimpleNamespace(
            t=npz["t"], lon=npz["lon"], lat=npz["lat"],
            fwhm=float(npz["fwhm"]), t_zero=float(npz["t_zero"]))


def main():
    p = argparse.ArgumentParser(
        description="Compact the 1420 MHz continuum survey for the "
                    "dish simulator.")
    p.add_argument("-o", "--out", default=CONTINUUM_DEFAULT)
    p.add_argument("--res", type=float, default=0.5,
                   help="output pixel size, deg (default 0.5)")
    p.add_argument("--fwhm", type=float, default=1.0,
                   help="total resolution, deg (default 1.0, matching "
                        "the compact H I cube)")
    p.add_argument("--preset", type=int, default=6, help="LZMA effort")
    a = p.parse_args()
    compress(a.out, a.res, a.fwhm, a.preset)


if __name__ == "__main__":
    main()
