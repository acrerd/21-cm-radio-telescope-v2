#!/usr/bin/env python3
"""
Compress the 33 GiB HI4PI all-sky cube into a compact form for the repo.

The interactive simulator convolves the sky with a dish beam of a few
degrees (4.9 deg FWHM for the default 3-m dish), so the survey's native
16.2' resolution and 5' sampling are far finer than anything the dish
can see.  This script produces a small self-contained file that is
indistinguishable from the full cube for beams >~ 1.5 deg:

  1. block-average each channel map from 5' to `--res` (0.5 deg) pixels;
  2. smooth to a total resolution of `--fwhm` (1.0 deg), recorded in the
     file so the simulator convolves only the residual beam;
  3. keep channels with |v_LSR| <= `--vmax` (470 km/s: all Milky Way and
     Magellanic emission; beyond that the cube is pure noise);
  4. zero everything below `--clip` sigma of the smoothed noise, so the
     array is mostly zeros;
  5. quantize to int16 steps of `--quant` (0.01 K, at the smoothed noise
     floor) and LZMA-compress a .npz of the result.

Output is ~25 MB instead of 33 GiB.  Usage (needs the full cube once):

    python hi4pi_compress.py

The result (default hi4pi_compact.npz.xz) is loaded with
load_compact() below; astro_simulator.py picks it up automatically.
"""

import argparse
import io
import lzma
import os
import sys
import time
import types

import numpy as np
from astropy.io import fits

from hi4pi_data import ensure_file

COMPACT_DEFAULT = "hi4pi_compact.npz.xz"


def gauss_kernel(sigma_pix, half=None):
    half = half or max(1, int(np.ceil(4 * sigma_pix)))
    x = np.arange(-half, half + 1)
    k = np.exp(-0.5 * (x / sigma_pix) ** 2)
    return k / k.sum()


def smooth_grid(grid, lat_c, step, fwhm_deg):
    """NaN-aware separable Gaussian smoothing on a CAR grid, wrapping in
    longitude; the longitude kernel widens as 1/cos(b) so the smoothing
    is ~isotropic on the sky (same scheme as the display map in
    astro_simulator.smooth_to_beam)."""
    sigma_deg = fwhm_deg / (2 * np.sqrt(2 * np.log(2)))
    filled = np.nan_to_num(grid, nan=0.0)
    mask = np.isfinite(grid).astype(np.float64)

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


def compress(cube_path, out_path, res=0.5, fwhm=1.0, vmax_kms=470.0,
             quant_k=0.01, clip_sigma=3.0, preset=6):
    cube_path = ensure_file(cube_path)
    t0 = time.time()
    with fits.open(cube_path, memmap=True) as hdul:
        hdu = next(h for h in hdul
                   if h.data is not None and h.header["NAXIS"] == 3)
        hdr = hdu.header
        nv, ny, nx = hdu.data.shape

        # 1-D world axes straight from the CAR header
        d1, d2, d3 = hdr["CDELT1"], hdr["CDELT2"], hdr["CDELT3"]
        lon_f = hdr["CRVAL1"] + (np.arange(nx) + 1 - hdr["CRPIX1"]) * d1
        lat_f = hdr["CRVAL2"] + (np.arange(ny) + 1 - hdr["CRPIX2"]) * d2
        v_f = hdr["CRVAL3"] + (np.arange(nv) + 1 - hdr["CRPIX3"]) * d3

        # trim to an exact 360 x 180 deg region divisible into res blocks
        blk = int(round(res / abs(d2)))
        ncol, nrow = int(round(360.0 / res)) * blk, \
            int(round(180.0 / res)) * blk
        j0 = int(np.argmin(np.abs(lat_f + 90.0)))    # row centred on -90
        j0 = min(max(j0, 0), ny - nrow)
        i0 = (nx - ncol) // 2
        lat_t, lon_t = lat_f[j0:j0 + nrow], lon_f[i0:i0 + ncol]

        # coarse axes: mean of each block (lon unwrapped, so a block
        # straddling l = 0 averages correctly; wrapped at the end)
        lat_c = lat_t.reshape(-1, blk).mean(axis=1)
        lon_c = lon_t.reshape(-1, blk).mean(axis=1) % 360.0

        keep = np.abs(v_f) <= vmax_kms * 1e3
        kk = np.where(keep)[0]
        v_c = v_f[kk]
        nk = kk.size
        nyc, nxc = nrow // blk, ncol // blk
        print(f"cube {nv}x{ny}x{nx} -> compact {nk}x{nyc}x{nxc} "
              f"({res:g} deg pixels, |v| <= {vmax_kms:g} km/s)")

        # residual smoothing to reach `fwhm` total: the data start at the
        # survey's 16.2' resolution and the block average adds a res-wide
        # boxcar (equivalent Gaussian FWHM 0.68*res)
        native = 16.2 / 60.0
        have2 = native ** 2 + (0.68 * res) ** 2
        if fwhm ** 2 <= have2:
            sys.exit(f"--fwhm {fwhm:g} is below the {np.sqrt(have2):.2f} "
                     f"deg resolution implied by --res {res:g}")
        resid = np.sqrt(fwhm ** 2 - have2)

        coarse = np.empty((nk, nyc, nxc), dtype=np.float32)
        chunk = 16
        for a in range(0, nk, chunk):
            b = min(a + chunk, nk)
            slab = np.asarray(hdu.data[kk[a]:kk[b - 1] + 1,
                                       j0:j0 + nrow, i0:i0 + ncol],
                              dtype=np.float64)
            for c in range(b - a):
                ch = slab[c].reshape(nyc, blk, nxc, blk)
                cnt = np.isfinite(ch).sum(axis=(1, 3))
                s = np.nansum(ch, axis=(1, 3))
                with np.errstate(invalid="ignore"):
                    g = np.where(cnt > 0, s / cnt, np.nan)
                coarse[a + c] = smooth_grid(g, lat_c, res, resid)
            done = b / nk
            el = time.time() - t0
            sys.stderr.write(f"\r  {done * 100:5.1f}%  "
                             f"({el:.0f} s, ~{el / done:.0f} s total)   ")
        sys.stderr.write("\n")

    # noise from the outermost kept channels (essentially emission-free),
    # via a robust MAD so the LMC / residual HVCs there do not bias it
    edge = coarse[np.abs(v_c) >= 0.85 * vmax_kms * 1e3][:, ::2, ::2]
    edge = edge[np.isfinite(edge)]
    sigma = 1.4826 * np.median(np.abs(edge - np.median(edge)))
    thresh = clip_sigma * sigma
    nan_n = int(np.sum(~np.isfinite(coarse)))
    np.nan_to_num(coarse, copy=False, nan=0.0)
    coarse[np.abs(coarse) < thresh] = 0.0
    occ = np.count_nonzero(coarse) / coarse.size
    print(f"smoothed noise {sigma * 1e3:.1f} mK, zeroed |T| < "
          f"{thresh * 1e3:.0f} mK -> {occ * 100:.1f}% of voxels kept "
          f"({nan_n} NaNs blanked)")

    q = np.clip(np.rint(coarse / quant_k), -32767, 32767).astype(np.int16)
    buf = io.BytesIO()
    np.savez(buf, t=q, scale=np.float64(quant_k), v=v_c,
             lon=lon_c, lat=lat_c, fwhm=np.float64(fwhm),
             sigma=np.float64(sigma), thresh=np.float64(thresh))
    print(f"compressing {buf.getbuffer().nbytes / 2**20:.0f} MB with "
          f"LZMA (preset {preset})...")
    tmp = out_path + ".tmp"
    with lzma.open(tmp, "wb", preset=preset) as f:
        f.write(buf.getvalue())
    os.replace(tmp, out_path)
    mb = os.path.getsize(out_path) / 2**20
    print(f"wrote {out_path}: {mb:.1f} MB "
          f"({os.path.getsize(cube_path) / os.path.getsize(out_path):.0f}x "
          f"smaller, {time.time() - t0:.0f} s)")


def load_compact(path):
    """Load a compact cube; returns a namespace with .t (int16 cube,
    v x lat x lon), .scale (K per step), .v (m/s), .lon/.lat (deg,
    matching the cube axes) and .fwhm (deg, the resolution already in
    the data)."""
    with lzma.open(path, "rb") as f:
        npz = np.load(io.BytesIO(f.read()))
        return types.SimpleNamespace(
            t=npz["t"], scale=float(npz["scale"]), v=npz["v"],
            lon=npz["lon"], lat=npz["lat"], fwhm=float(npz["fwhm"]))


def main():
    p = argparse.ArgumentParser(
        description="Compress the HI4PI all-sky cube for small-dish use.")
    p.add_argument("cube", nargs="?", default="hi4pi_allsky_gal_CAR.fits",
                   help="full HI4PI cube (downloaded if missing, ~33 GiB)")
    p.add_argument("-o", "--out", default=COMPACT_DEFAULT,
                   help=f"output file (default {COMPACT_DEFAULT})")
    p.add_argument("--res", type=float, default=0.5,
                   help="output pixel size, deg (default 0.5)")
    p.add_argument("--fwhm", type=float, default=1.0,
                   help="total resolution of the stored sky, deg "
                        "(default 1.0; beams below ~1.5x this need the "
                        "full cube)")
    p.add_argument("--vmax", type=float, default=470.0,
                   help="keep |v_LSR| below this, km/s (default 470)")
    p.add_argument("--quant", type=float, default=0.01,
                   help="quantization step, K (default 0.01)")
    p.add_argument("--clip", type=float, default=3.0,
                   help="zero below this many sigma (default 3)")
    p.add_argument("--preset", type=int, default=6,
                   help="LZMA effort 0-9 (default 6)")
    a = p.parse_args()
    compress(a.cube, a.out, a.res, a.fwhm, a.vmax, a.quant, a.clip,
             a.preset)


if __name__ == "__main__":
    main()
