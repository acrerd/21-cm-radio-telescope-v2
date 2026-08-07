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

On first run the required HI4PI files are downloaded automatically from
CDS (see **Data** below — the all-sky cube is **~33 GiB**, so the first
launch takes a while; interrupted downloads resume). Then a window opens
with the all-sky N_HI map: **click anywhere** to point the dish and get
its spectrum.

## Scripts

| Script | Purpose |
|---|---|
| `hi4pi_interactive.py` | Interactive GUI: click the all-sky map, get the dish spectrum. Editable pointing, beam, T_sys, integration time, bandwidth, band centre and velocity frame (LSR/SSB/topocentric); target list, site horizon/visibility overlays, continuum sources (Sun, Cyg A, Cas A). Press `s` to save the current spectrum (PNG + txt). |
| `hi4pi_3m_dish.py` | Command-line, single-pointing version working from one ~20°×20° HI4PI tile (~250 MiB, chosen automatically from the pointing and downloaded if missing) instead of the 33 GiB cube. Writes a PNG + txt spectrum. |
| `hi4pi_data.py` | Data download helper (see below). Run directly to pre-fetch the all-sky files. |

Examples:

```
python hi4pi_interactive.py --bw 2 --tsys 100 --tint 60 --nchan 1024
python hi4pi_3m_dish.py --glon 132 --glat -1 --bw 2
```

## Data

All files come from CDS catalogue
[J/A+A/594/A116](https://cdsarc.cds.unistra.fr/ftp/J/A+A/594/A116/) and are
fetched automatically the first time a script needs them (`hi4pi_data.py`;
downloads resume if interrupted):

| Local file | CDS source | Size |
|---|---|---|
| `hi4pi_allsky_gal_CAR.fits` | `ALLSKY/GAL/CAR.fits` — all-sky galactic plate-carrée spectral cube | ~33 GiB |
| `hi4pi.fits` | `NHI_HPX.fits.gz` — N_HI HEALPix table (display map) | 318 MiB (~600 MiB unpacked) |
| `CAR_A01.fits` … `CAR_I18.fits` | `CUBES/GAL/CAR/` — individual tiles, on demand for `hi4pi_3m_dish.py` (chosen from l, b) | ~250 MiB each |

`nhi_grid_cache.npy` is a regenerable cache of the gridded N_HI display
map, written on the first GUI launch.

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
