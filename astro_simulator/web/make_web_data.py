#!/usr/bin/env python3
"""Convert the compact datasets into the web simulator's data bundles.

Reads ../hi4pi_compact.npz.xz and ../continuum_1420_compact.npz.xz and
writes data/hi4pi_web.bin.gz, data/continuum_web.bin.gz and
data/meta.json.  The web page depends on nothing else: the N_HI display
map is derived here from the compact cube itself (not from the desktop
app's nhi_grid_cache.npy).

Bundle layout (little-endian throughout):
    magic (4 bytes) | uint32 header length | header JSON | sections...
Sections follow in the order named by the header's "sections" list;
every array is tightly packed with the dtype the header declares.

Cube bundle (magic HI4W), pixel-major sparse:
    pix_off  uint32[nlat*nlon + 1]   cumulative run count per pixel
    runs     uint16[2 * nruns]       (k0, len) pairs
    vals     int16[nnz]              nonzero T_B quantisation steps
    v        float64[nv]             LSR velocity axis (m/s)
    lon      float64[nlon]           grid longitudes (deg, cube order)
    lat      float64[nlat]           grid latitudes (deg)
Pixel index p = ilat * nlon + ilon; a pixel's spectrum is the union of
its runs: t[k0 .. k0+len) = vals * scale (K), zero elsewhere.

Continuum bundle (magic CONW):
    t        float32[nlat*nlon]      diffuse T_B above t_zero (K)
    nhi      float32[nlat*nlon]      N_HI display map (cm^-2, from cube)
    lon      float64[nlon]
    lat      float64[nlat]

Run from this folder:  python make_web_data.py
"""

import gzip
import io
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
from hi4pi_compress import load_compact          # noqa: E402
from continuum_compress import load_continuum    # noqa: E402
from instrument import DISH_M, beam_fwhm_deg     # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CUBE_SRC = os.path.join(HERE, "..", "hi4pi_compact.npz.xz")
CONT_SRC = os.path.join(HERE, "..", "continuum_1420_compact.npz.xz")


def write_bundle(path, magic, header, sections):
    """Assemble magic|len|JSON|sections and gzip the lot."""
    header = dict(header)
    header["sections"] = [
        {"name": n, "dtype": str(a.dtype), "count": int(a.size)}
        for n, a in sections]
    hjson = json.dumps(header).encode()
    buf = io.BytesIO()
    buf.write(magic)
    buf.write(np.uint32(len(hjson)).tobytes())
    buf.write(hjson)
    for _, a in sections:
        buf.write(np.ascontiguousarray(a).tobytes())
    raw = buf.getvalue()
    with gzip.open(path, "wb", compresslevel=9) as f:
        f.write(raw)
    print(f"{os.path.basename(path)}: {len(raw)/1e6:.1f} MB raw, "
          f"{os.path.getsize(path)/1e6:.1f} MB gzipped")


def sparse_cube(c):
    """Multi-run sparse encoding of the (v, lat, lon) int16 cube."""
    nv, nlat, nlon = c.t.shape
    # pixel-major view: (lat*lon, v)
    t = np.ascontiguousarray(c.t.transpose(1, 2, 0)).reshape(-1, nv)
    nz = t != 0
    # run starts/ends along v for every pixel at once
    prev = np.zeros_like(nz)
    prev[:, 1:] = nz[:, :-1]
    starts = nz & ~prev
    nxt = np.zeros_like(nz)
    nxt[:, :-1] = nz[:, 1:]
    ends = nz & ~nxt
    runs_per_pix = starts.sum(axis=1).astype(np.uint32)
    pix_off = np.zeros(t.shape[0] + 1, dtype=np.uint32)
    np.cumsum(runs_per_pix, out=pix_off[1:])

    sp, sk = np.nonzero(starts)          # pixel, k0 of each run
    ep, ek = np.nonzero(ends)            # same pixels, same order
    assert np.array_equal(sp, ep)
    lens = (ek - sk + 1)
    assert lens.max() < 65536 and sk.max() < 65536
    runs = np.empty(2 * len(sk), dtype=np.uint16)
    runs[0::2] = sk
    runs[1::2] = lens
    vals = t[nz]                          # row-major: pixel, then k order
    return pix_off, runs, vals.astype(np.int16)


def main():
    os.makedirs(DATA, exist_ok=True)
    c = load_compact(CUBE_SRC)
    nv, nlat, nlon = c.t.shape
    dv_ms = float(np.median(np.diff(c.v)))
    print(f"cube: {nv} x {nlat} x {nlon}, scale {c.scale} K, "
          f"fwhm {c.fwhm} deg")

    pix_off, runs, vals = sparse_cube(c)
    print(f"sparse: {len(vals)/1e6:.1f}M values, {len(runs)//2/1e6:.2f}M runs")
    write_bundle(
        os.path.join(DATA, "hi4pi_web.bin.gz"), b"HI4W",
        {"shape": [nv, nlat, nlon], "scale": c.scale, "fwhm": c.fwhm,
         "dv_ms": dv_ms},
        [("pix_off", pix_off), ("runs", runs), ("vals", vals),
         ("v", c.v.astype(np.float64)),
         ("lon", (c.lon % 360.0).astype(np.float64)),
         ("lat", c.lat.astype(np.float64))])

    # N_HI display map straight from the cube: 1.823e18 * sum(T) dv[km/s]
    tsum = c.t.astype(np.float64).sum(axis=0) * c.scale
    nhi = 1.823e18 * tsum * (abs(dv_ms) / 1e3)

    m = load_continuum(CONT_SRC)
    assert m.t.shape == (nlat, nlon)

    # The cube's lon axis is wrapped/descending while the continuum
    # grid (whose axes the map renderer samples every display grid
    # with) ascends from 0.25 deg: resample N_HI onto the continuum
    # grid by nearest neighbour, or the H I image lands at the wrong
    # longitudes even though every spectrum is unaffected.
    clon = c.lon % 360.0
    ix = np.argmin(np.abs((clon[None, :] - m.lon[:, None] + 180.0)
                          % 360.0 - 180.0), axis=1)
    iy = np.argmin(np.abs(c.lat[None, :] - m.lat[:, None]), axis=1)
    nhi = nhi[np.ix_(iy, ix)]
    write_bundle(
        os.path.join(DATA, "continuum_web.bin.gz"), b"CONW",
        {"shape": [nlat, nlon], "fwhm": m.fwhm, "t_zero": m.t_zero},
        [("t", m.t.astype(np.float32)),
         ("nhi", nhi.astype(np.float32)),
         ("lon", (m.lon % 360.0).astype(np.float64)),
         ("lat", m.lat.astype(np.float64))])

    meta = {
        "f_hi": 1420405751.768,
        "c_light": 299792458.0,
        "site": {"name": "Glasgow", "lat": 55.87, "lon": -4.29,
                 "height": 50.0},
        # fwhm is the measured beam, shipped so the browser never has to fall
        # back on a formula for it; see instrument.py for why it is not
        # 1.22 lambda/D.
        "defaults": {"bw_mhz": 2.0, "dish_m": DISH_M, "eta": 0.7,
                     "tsys": 200.0, "tint": 60.0, "npol": 1,
                     "fwhm": round(beam_fwhm_deg(DISH_M), 3)},
        # No controller address: the web build cannot command the telescope
        # (see README - mixed content, no CORS, and the controller is on a
        # private link). astro_simulator.py --controller does that instead.
    }
    with open(os.path.join(DATA, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print("meta.json written")


if __name__ == "__main__":
    main()
