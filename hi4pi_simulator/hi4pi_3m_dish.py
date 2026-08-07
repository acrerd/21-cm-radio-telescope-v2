#!/usr/bin/env python3
"""
hi4pi_3m_dish.py
================
Predict the HI spectrum that a small dish (default 3 m) + spectrometer
would observe, using the HI4PI 21-cm survey as the "true sky".

The HI4PI survey (HI4PI Collaboration 2016, A&A 594, A116) provides
brightness temperature T_B(l, b, v_LSR) at 16.2' angular resolution and
1.29 km/s spectral resolution, |v_LSR| < ~600 km/s.  A 3-m dish at
1420 MHz has a beam of FWHM ~ 1.22*lambda/D ~ 4.9 deg, i.e. much larger
than the survey beam, so the survey cube can be treated as the true sky
brightness distribution.

The predicted antenna temperature in each spectrometer channel is the
beam-weighted average of the survey brightness temperature, scaled by
the main-beam efficiency:

    T_A(v) = eta_mb * [ sum_i P(theta_i) T_B,i dOmega_i ]
                      / [ sum_i P(theta_i) dOmega_i ]

with P a normalised Gaussian power pattern of the dish main beam.
(The spectrum is the *line* contribution only, i.e. what remains after
baseline/continuum subtraction; system noise can be added optionally
via the radiometer equation.)

Input file: one of the standard HI4PI FITS image cubes from CDS
(the 21.5 deg x 21.5 deg "CAR" cubes, e.g. CAR_E04.fits, or the HPX
projection tiles).  Any FITS image cube with a celestial WCS + spectral
axis works.  If no cube is given, the CAR tile covering the pointing is
chosen automatically and downloaded from CDS (~250 MiB) if missing.

Usage examples
--------------
    python hi4pi_3m_dish.py --glon 132.0 --glat -1.0 --bw 2.0
    python hi4pi_3m_dish.py                         # will prompt for l, b, bw
    python hi4pi_3m_dish.py CAR_E04.fits --glon 132.0 --glat -1.0 --bw 2.0

Options (see --help): dish diameter, main-beam efficiency, band centre,
number of spectrometer channels, Tsys/integration time for noise.
"""

import argparse
import sys

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS

from hi4pi_data import ensure_file

C_LIGHT = 299792458.0          # m/s
F_HI = 1420405751.768          # Hz, HI rest frequency


def beam_fwhm_rad(diameter_m, freq_hz=F_HI):
    """Diffraction-limited FWHM of the main beam, theta = 1.22 lambda/D."""
    lam = C_LIGHT / freq_hz
    return 1.22 * lam / diameter_m


def cube_name(glon, glat):
    """The galactic CAR tile covering a pointing.  The CDS grid of tile
    centres is regular: GLAT -80..+80 in steps of 20 (row letter A..I),
    GLON 10..350 in steps of 20 (column number 01..18)."""
    row = min(8, max(0, round((glat + 80.0) / 20.0)))
    col = round((glon % 360.0 - 10.0) / 20.0) % 18
    return f"CAR_{chr(ord('A') + row)}{col + 1:02d}.fits"


def open_cube(path):
    """Open a HI4PI image cube; return (hdu, wcs, header)."""
    hdul = fits.open(path, memmap=True)
    hdu = next((h for h in hdul if h.data is not None and h.header["NAXIS"] >= 3),
               None)
    if hdu is None:
        sys.exit("No 3-D image cube found in this FITS file. "
                 "(If you downloaded the HEALPix binary-table version of HI4PI, "
                 "please use the CAR or HPX image cubes from CDS instead.)")
    return hdu, WCS(hdu.header), hdu.header


def channel_velocities(wcs, header, nspec):
    """LSR radial velocity (m/s, radio convention) of each spectral channel."""
    spec_wcs = wcs.spectral
    pix = np.arange(nspec)
    world = spec_wcs.pixel_to_world(pix)
    ctype = header.get(f"CTYPE{wcs.wcs.spec + 1}", "").upper()
    if ctype.startswith(("VRAD", "VELO", "VLSR")):
        return world.to_value(u.m / u.s)
    if ctype.startswith("FREQ"):
        f = world.to_value(u.Hz)
        return C_LIGHT * (F_HI - f) / F_HI
    sys.exit(f"Unrecognised spectral axis type '{ctype}'.")


def simulate(path, glon, glat, bw_hz, dish_m=3.0, eta_mb=0.7,
             f_center=F_HI, nchan=None, tsys=None, tint=60.0, npol=2,
             seed=None):
    """Return dict with the simulated spectrum and bookkeeping info."""
    hdu, wcs, hdr = open_cube(path)

    naxis = hdr["NAXIS"]
    spec_np_axis = naxis - 1 - wcs.wcs.spec     # numpy axis of spectral coord
    shape = hdu.data.shape if naxis == 3 else hdu.data.shape[-3:]
    # HI4PI cubes are (spec, lat, lon) in numpy order; enforce that layout
    if spec_np_axis != naxis - 3:
        sys.exit("Unexpected axis ordering in cube (expected spectral axis "
                 "to be FITS axis 3).")
    nspec, ny, nx = shape[0], shape[1], shape[2]

    # ---------- spectral selection ----------------------------------------
    v_chan = channel_velocities(wcs, hdr, nspec)          # m/s, LSR
    f_chan = F_HI * (1.0 - v_chan / C_LIGHT)              # Hz
    in_band = np.abs(f_chan - f_center) <= bw_hz / 2.0
    if not in_band.any():
        sys.exit("Requested band does not overlap the HI4PI spectral coverage "
                 "(rest frame 1420.4058 MHz +/- ~2.8 MHz, i.e. |v_LSR| < 600 km/s).")
    band_edges = f_center + np.array([-0.5, 0.5]) * bw_hz
    v_edges = C_LIGHT * (F_HI - band_edges) / F_HI
    clipped = (v_chan.min() > min(v_edges) + 2e3) or (v_chan.max() < max(v_edges) - 2e3)
    ksel = np.where(in_band)[0]
    k0, k1 = ksel.min(), ksel.max() + 1

    # ---------- spatial selection ------------------------------------------
    fwhm = np.degrees(beam_fwhm_rad(dish_m, f_center))    # deg
    r_max = 1.5 * fwhm                                    # keeps 99.8% of beam
    target = SkyCoord(l=glon * u.deg, b=glat * u.deg, frame="galactic")

    xx, yy = np.meshgrid(np.arange(nx), np.arange(ny))
    sky = wcs.celestial.pixel_to_world(xx, yy)
    sep = target.separation(sky).deg                      # (ny, nx)
    mask = sep <= r_max
    if not mask.any():
        sys.exit(f"Pointing (l={glon}, b={glat}) is not inside this cube. "
                 "Download the HI4PI cube that covers those coordinates.")

    ys, xs = np.where(mask)
    y0, y1 = ys.min(), ys.max() + 1
    x0, x1 = xs.min(), xs.max() + 1

    # Gaussian beam weight * pixel solid angle (CAR pixels shrink as cos b)
    sigma = fwhm / (2.0 * np.sqrt(2.0 * np.log(2.0)))
    lat_native = sky.spherical.lat.deg                    # frame of the WCS grid
    pix_area = abs(np.prod(wcs.celestial.wcs.cdelt))      # deg^2 at the equator
    w_full = np.where(mask,
                      np.exp(-0.5 * (sep / sigma) ** 2)
                      * np.cos(np.radians(lat_native)) * pix_area,
                      0.0)
    w = w_full[y0:y1, x0:x1]

    # Fraction of the analytic beam integral actually covered by the cube
    omega_beam = 2.0 * np.pi * sigma ** 2                 # deg^2
    coverage = w_full.sum() / omega_beam
    if coverage < 0.95:
        print(f"WARNING: only {100*coverage:.0f}% of the {fwhm:.1f} deg beam "
              "falls on this cube - the pointing is near the cube edge, so the "
              "spectrum will be biased. Pass the neighbouring HI4PI tile "
              "explicitly, or use the all-sky cube (hi4pi_interactive.py).")

    # ---------- read data and form the beam-weighted spectrum --------------
    if naxis == 3:
        sub = np.asarray(hdu.section[k0:k1, y0:y1, x0:x1], dtype=np.float64)
    else:                                                 # degenerate 4th axis
        sub = np.asarray(hdu.section[..., k0:k1, y0:y1, x0:x1],
                         dtype=np.float64).reshape(k1 - k0, y1 - y0, x1 - x0)
    bad = ~np.isfinite(sub)
    sub[bad] = 0.0
    wsum = np.where(bad, 0.0, w[None, :, :]).sum(axis=(1, 2))
    t_mb = (sub * w[None, :, :]).sum(axis=(1, 2)) / np.where(wsum > 0, wsum, np.nan)
    t_a = eta_mb * t_mb                                   # antenna temperature

    v_sel = v_chan[k0:k1]
    f_sel = f_chan[k0:k1]

    # ---------- rebin onto the spectrometer channel grid --------------------
    dv_survey = abs(np.median(np.diff(v_sel)))
    if nchan:
        f_edges = np.linspace(f_center - bw_hz / 2, f_center + bw_hz / 2, nchan + 1)
        f_out = 0.5 * (f_edges[:-1] + f_edges[1:])
        df_out = bw_hz / nchan
        if df_out < abs(np.median(np.diff(f_sel))):
            order = np.argsort(f_sel)                     # finer than survey:
            t_out = np.interp(f_out, f_sel[order], t_a[order],
                              left=np.nan, right=np.nan)  # interpolate
        else:
            idx = np.digitize(f_sel, f_edges) - 1
            t_out = np.full(nchan, np.nan)
            for i in range(nchan):
                sel = (idx == i) & np.isfinite(t_a)
                if sel.any():
                    t_out[i] = t_a[sel].mean()
        v_out = C_LIGHT * (F_HI - f_out) / F_HI
    else:
        f_out, v_out, t_out = f_sel, v_sel, t_a
        df_out = abs(np.median(np.diff(f_sel)))

    # ---------- optional radiometer noise -----------------------------------
    sigma_noise = None
    if tsys is not None:
        sigma_noise = tsys / np.sqrt(npol * df_out * tint)
        rng = np.random.default_rng(seed)
        t_out = t_out + rng.normal(0.0, sigma_noise, size=t_out.shape)

    return dict(freq=f_out, vel=v_out, t_a=t_out, fwhm_deg=fwhm,
                coverage=coverage, clipped=clipped, dv_survey=dv_survey,
                df_chan=df_out, sigma_noise=sigma_noise, eta_mb=eta_mb,
                glon=glon, glat=glat, bw_hz=bw_hz, dish_m=dish_m,
                tsys=tsys, tint=tint)


def make_plot(res, png_path):
    ink = "#333639"
    line = "#3b7bbf"
    plt.rcParams.update({"font.size": 11, "text.color": ink,
                         "axes.edgecolor": "#c7cacd", "axes.labelcolor": ink,
                         "xtick.color": ink, "ytick.color": ink})
    fig, ax = plt.subplots(figsize=(8, 4.8))
    f_mhz = res["freq"] / 1e6
    ax.plot(f_mhz, res["t_a"], color=line, lw=1.6)
    ax.axhline(0, color="#c7cacd", lw=0.8, zorder=0)
    ax.set_xlabel("Frequency  (MHz)")
    ax.set_ylabel("Antenna temperature  $T_A$  (K)")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.2f}"))
    noise_txt = ("no noise" if res["sigma_noise"] is None else
                 f"$T_{{sys}}$={res['tsys']:.0f} K, "
                 f"$\\tau$={res['tint']:.0f} s, "
                 f"$\\sigma$={res['sigma_noise']*1e3:.1f} mK")
    ax.set_title(f"Simulated {res['dish_m']:.0f}-m dish HI spectrum   "
                 f"l={res['glon']:.2f}$^\\circ$, b={res['glat']:.2f}$^\\circ$\n"
                 f"beam FWHM {res['fwhm_deg']:.1f}$^\\circ$, "
                 f"$\\eta_{{mb}}$={res['eta_mb']:.2f}, "
                 f"BW {res['bw_hz']/1e6:.2f} MHz, {noise_txt}",
                 fontsize=10)
    ax.grid(color="#eceeef", lw=0.7)
    ax.set_axisbelow(True)

    sec = ax.secondary_xaxis(
        "top",
        functions=(lambda f: C_LIGHT * (F_HI - f * 1e6) / F_HI / 1e3,
                   lambda v: (F_HI * (1 - v * 1e3 / C_LIGHT)) / 1e6))
    sec.set_xlabel("LSR radial velocity (km s$^{-1}$)", fontsize=10)
    fig.tight_layout()
    fig.savefig(png_path, dpi=150)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(
        description="Simulate a small-dish HI observation from an HI4PI cube.")
    p.add_argument("cube", nargs="?",
                   help="HI4PI FITS image cube (e.g. CAR_E04.fits); "
                        "default: the tile covering the pointing, "
                        "downloaded from CDS (~250 MiB) if missing")
    p.add_argument("--glon", type=float, help="Galactic longitude l (deg)")
    p.add_argument("--glat", type=float, help="Galactic latitude b (deg)")
    p.add_argument("--bw", type=float,
                   help="Spectrometer bandwidth in MHz (centred on --fc)")
    p.add_argument("--fc", type=float, default=F_HI / 1e6,
                   help="Band centre frequency in MHz (default: HI rest freq)")
    p.add_argument("--dish", type=float, default=3.0, help="Dish diameter (m)")
    p.add_argument("--eta", type=float, default=0.7,
                   help="Main-beam efficiency (default 0.7)")
    p.add_argument("--nchan", type=int,
                   help="Number of spectrometer channels (default: native "
                        "HI4PI channels, ~6.1 kHz / 1.29 km/s)")
    p.add_argument("--tsys", type=float,
                   help="System temperature (K); if given, radiometer noise "
                        "is added")
    p.add_argument("--tint", type=float, default=60.0,
                   help="Integration time in s for the noise (default 60)")
    p.add_argument("--npol", type=int, default=2,
                   help="Polarisations averaged in the noise calc (default 2)")
    p.add_argument("--seed", type=int, help="Random seed for the noise")
    p.add_argument("--out", default=None,
                   help="Basename for outputs (default derived from l, b)")
    a = p.parse_args()

    if a.glon is None:
        a.glon = float(input("Galactic longitude l (deg): "))
    if a.glat is None:
        a.glat = float(input("Galactic latitude b (deg): "))
    if a.bw is None:
        a.bw = float(input("Spectrometer bandwidth (MHz): "))

    if a.cube is None:
        a.cube = cube_name(a.glon, a.glat)
        print(f"Tile for l={a.glon:.2f}, b={a.glat:.2f}:  {a.cube}")
    a.cube = ensure_file(a.cube)
    res = simulate(a.cube, a.glon, a.glat, a.bw * 1e6, dish_m=a.dish,
                   eta_mb=a.eta, f_center=a.fc * 1e6, nchan=a.nchan,
                   tsys=a.tsys, tint=a.tint, npol=a.npol, seed=a.seed)

    base = a.out or f"spectrum_l{a.glon:+07.2f}_b{a.glat:+06.2f}"
    good = np.isfinite(res["t_a"])
    dv = res["dv_survey"] / 1e3
    print(f"\nDish {a.dish:.1f} m  ->  beam FWHM = {res['fwhm_deg']:.2f} deg "
          f"(HI4PI resolution 16.2', fully beam-filling)")
    print(f"Band: {a.bw:.3f} MHz about {a.fc:.4f} MHz  ->  "
          f"v_LSR {res['vel'].min()/1e3:+.1f} to {res['vel'].max()/1e3:+.1f} km/s")
    if res["clipped"]:
        print("NOTE: band wider than HI4PI velocity coverage; spectrum is "
              "clipped to the survey range (no HI signal expected outside it).")
    print(f"Channel width: {res['df_chan']/1e3:.2f} kHz "
          f"({res['df_chan']/F_HI*C_LIGHT/1e3:.2f} km/s); survey native {dv:.2f} km/s")
    print(f"Beam coverage on this cube: {100*res['coverage']:.1f}%")
    if res["sigma_noise"] is not None:
        print(f"Radiometer noise per channel: {res['sigma_noise']*1e3:.1f} mK "
              f"(Tsys={a.tsys} K, {a.tint} s, {a.npol} pol)")
    print(f"Peak T_A = {np.nanmax(res['t_a']):.2f} K;  "
          f"integrated line = {np.nansum(res['t_a'][good] ) * dv:.1f} K km/s")

    np.savetxt(base + ".txt",
               np.column_stack([res["freq"], res["vel"] / 1e3, res["t_a"]]),
               header="freq_Hz   v_LSR_km/s   T_A_K", fmt="%.6e")
    make_plot(res, base + ".png")
    print(f"\nSaved: {base}.png and {base}.txt")


if __name__ == "__main__":
    main()
