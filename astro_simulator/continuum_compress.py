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
PSF_FWHM = 0.65                      # deg, measured from Tau A profile


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

    # inpaint the blanked Cas A hole with the local background, then
    # remove each strong source's PSF-shaped imprint (the simulator
    # adds them back analytically at their true, current-epoch fluxes)
    sig = PSF_FWHM / (2 * np.sqrt(2 * np.log(2)))
    for name, tl, tb in STRONG:
        sep = sep_to(tl, tb)
        bg = np.median(t_k[good & (sep > 1.8) & (sep < 2.6)])
        hole = ~good & (sep < 3.0)
        if hole.any():
            t_k[hole] = bg
            good |= hole
            print(f"  {name}: inpainted {hole.sum()} blanked pixels "
                  f"at {bg:.2f} K")
        amp = t_k[good & (sep < 0.25)].max() - bg
        if amp > 0:
            t_k -= amp * np.exp(-0.5 * (sep / sig) ** 2)
            print(f"  {name}: removed {amp:.1f} K peak "
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
