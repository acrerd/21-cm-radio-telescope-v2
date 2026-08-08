# HI4PI small-dish simulator

Simulate what a small radio telescope (default: a 3-m dish) observes when
pointed at the 21-cm sky, using the [HI4PI survey](https://ui.adsabs.harvard.edu/abs/2016A%26A...594A.116H)
(HI4PI Collaboration 2016, A&A 594, A116) as the "true" sky. HI4PI has a
16.2′ beam and 1.29 km/s channels — far finer than a small dish's ~5°
beam — so the simulated antenna-temperature spectrum is the beam-weighted
average of the survey brightness temperature, with optional continuum
sources, radiometer noise and velocity-frame conversion.

## Quick start

```
pip install -r requirements.txt
python hi4pi_interactive.py
```

Or use the launchers: `run.bat` (Windows) / `./run.sh` (Linux; make it
executable once with `chmod +x run.sh`). Both pass extra arguments
through to `hi4pi_interactive.py`.

The repo ships `hi4pi_compact.npz.xz` (~23 MB), a pre-smoothed compact
version of the survey tuned to small-dish beams, so **no download is
needed** to start: a window opens with the all-sky N_HI map — **click
anywhere** to point the dish and get its spectrum. The full ~33 GiB
survey cube is only fetched from CDS if you ask for something the
compact data cannot honour (a beam below ~1.5°, a band beyond
±470 km/s) or run with `--full` (interrupted downloads resume).

## Scripts

| Script | Purpose |
|---|---|
| `hi4pi_interactive.py` | Interactive GUI: click the all-sky map, get the dish spectrum. Editable pointing, beam, T_sys, integration time, bandwidth, band centre and velocity frame (LSR/SSB/topocentric); target list (H I targets plus the continuum sources Cyg A, Cas A, Tau A and the Sun), a map toggle between the N_HI and 1420 MHz continuum skies, site horizon/visibility overlays, and a live RA/Dec + Alt/Az readout for the current pointing beside the map. The lower panel is modal: with the H I map it shows the spectrum; with the continuum map it shows a **drift scan** — band-averaged T_A (H I included when in-band) for the sky drifting through a beam parked on the current target, any target, above the horizon or not, centred on beam-centre transit over the duration set in the **scan (min)** box, with per-sample noise. Press `s` to save the current spectrum (PNG + txt). |
| `hi4pi_compress.py` | Regenerates `hi4pi_compact.npz.xz` from the full cube: block-averages to 0.5° pixels, smooths to 1° total resolution, trims to \|v\| ≤ 470 km/s, zeroes below 3σ, quantizes to int16 (0.01 K) and LZMA-compresses — 33 GiB → ~23 MB, exact to <0.5% for beams ≥ 1.6°. |
| `continuum_compress.py` | Regenerates `continuum_1420_compact.npz.xz` from the Stockert/Villa-Elisa 1420 MHz continuum survey (CADE HEALPix, fetched automatically, 3.2 MiB): same 0.5° grid and 1° resolution as the H I cube, zero level (CMB + isotropic, ~3.2 K) stored separately, strong sources (Cyg A, Cas A, Tau A — saturated/blanked in the survey) removed for analytic re-insertion. ~0.7 MB. |
| `hi4pi_data.py` | Data download helper (see below). Run directly to pre-fetch the all-sky files. |

Example:

```
python hi4pi_interactive.py --bw 2 --tsys 100 --tint 60 --nchan 1024
```

## The two sky datasets

The GUI can draw its spectra from either of two forms of the survey:

| | `hi4pi_compact.npz.xz` (in the repo) | `hi4pi_allsky_gal_CAR.fits` (CDS) |
|---|---|---|
| Size | ~23 MB | ~33 GiB |
| Resolution | pre-smoothed to 1.0°, 0.5° pixels | native 16.2′, 5′ pixels |
| Velocity range | \|v_LSR\| ≤ 470 km/s | \|v_LSR\| ≤ 607 km/s |
| Beams supported | ≥ ~1.5° (any dish ≲ 7 m) | ≥ 0.2° |
| Spectrum speed | ~10 ms (held in RAM) | 0.3–2.5 s (memory-mapped) |

The compact file is quantized to 0.01 K with everything below 3σ of its
8.7 mK noise floor zeroed. Because it stores its own resolution, the
simulator convolves only the *residual* beam, so the delivered beam is
exactly what you asked for; for the default 3-m dish (4.9° beam) the
two datasets agree to <0.2% of peak per channel — far below any
simulated radiometer noise, i.e. indistinguishable in use.

Selection is automatic: the compact file is used whenever it is present.
If you request something it cannot honour — a beam finer than ~1.5° or a
band centre beyond its velocity range — the GUI switches to the full
cube on the fly if that file is on disk, and otherwise clamps the
request and tells you (it never starts the 33 GiB download uninvited).
When the beam and band later return to values the compact data can
serve (say the beam goes from 0.2° back to 5°), it switches back just
as automatically — the compact cube stays cached in RAM, so the return
is immediate.

## Continuum sky

`continuum_1420_compact.npz.xz` (in the repo, ~0.7 MB) adds the diffuse
1420 MHz continuum from the Stockert/Villa-Elisa all-sky survey (Reich
1982; Reich & Reich 1986; Reich, Testori & Reich 2001) under the
analytic point sources. Every spectrum then rides on the real continuum
background — Cygnus X, the galactic ridge, the North Polar Spur — which
both shapes drift-scan baselines and feeds the noise estimate: the
radiometer rms uses T_sys + T_A(pointing), so pointing at the plane
degrades the SNR just as it does at the telescope. Notes: the survey's
uniform zero level (CMB + isotropic, ~3.2 K) is subtracted and assumed
to live inside your T_sys; the survey saturates or blanks the strongest
compact sources, so Cyg A, Cas A and Tau A are removed from the map and
carried analytically at their true fluxes; the map's resolution floor
is 1°, irrelevant for beams ≥ 1.5°. Delete or rename the file (or pass
`--continuum ""`) to fall back to point sources on an empty sky.
Force the full cube with `--full` (this *will* download it if missing);
point at a different compact file with `--compact PATH`.

## Data

The raw survey files are fetched automatically the first time a script
needs them (`hi4pi_data.py`; downloads resume if interrupted). The
HI4PI files come from CDS catalogue
[J/A+A/594/A116](https://cdsarc.cds.unistra.fr/ftp/J/A+A/594/A116/):

| Local file | Source | Size |
|---|---|---|
| `hi4pi_allsky_gal_CAR.fits` | `ALLSKY/GAL/CAR.fits` — all-sky galactic plate-carrée spectral cube | ~33 GiB |
| `hi4pi.fits` | `NHI_HPX.fits.gz` — N_HI HEALPix table (display map) | 318 MiB (~600 MiB unpacked) |
| `stockert_villaelisa_1420MHz_healpix.fits` | [LAMBDA mirror](https://lambda.gsfc.nasa.gov/product/foreground/fg_stockert_villa_info.html) of the CADE HEALPix Stockert/Villa-Elisa 1420 MHz continuum survey — only needed to regenerate the compact continuum map | 3.2 MiB |

`hi4pi_compact.npz.xz`, `continuum_1420_compact.npz.xz` and
`nhi_grid_cache.npy` (the gridded N_HI display map) are all committed
to the repo and derived from these files, so a fresh clone runs the GUI
with no downloads at all; regenerate them with `hi4pi_compress.py`,
`continuum_compress.py`, or (for the cache) by deleting the file before
a launch with `hi4pi.fits` present.

## Observer site

The horizon, visibility loops and topocentric velocity frame default to
Glasgow; change the defaults at the top of `hi4pi_interactive.py` or per
run with `--site/--lat/--lon/--height`.

## Requirements

Python 3.9+, `numpy`, `matplotlib`, `astropy` (`pip install -r requirements.txt`).
The GUI needs an interactive matplotlib backend (TkAgg or QtAgg).

## Acknowledgement

HI4PI: a full-sky HI survey based on EBHIS and GASS, HI4PI Collaboration,
2016, A&A 594, A116. Data via CDS/VizieR, catalogue J/A+A/594/A116.
