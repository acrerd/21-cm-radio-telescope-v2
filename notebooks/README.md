# Worked data reductions

Notebooks that take a recording apart by hand. Each one does what the pipeline
does, step by step, so the assumptions at each stage are visible instead of
buried in one call — and each ends by showing the one-line equivalent.

They are for reading and for learning the instrument, not for doing the
observatory's work. `observation_plot.plot_observation` is what you want for
that.

| notebook | what it does |
|---|---|
| `read_h1_data.ipynb` | Opens a recording, prints what is in it, and plots it — a tracked spectrum as a spectrum, a drift scan as its band-power curve. The place to start. |
| `solar_flux_scallop.ipynb` | A solar track end to end: continuum window, band mean, the tracking scallop fitted and removed, the antenna theorem, the atmosphere, and the comparison against the RSTN reference network. |
| `solar_flux_rstn.ipynb` | The Sun at 1415 MHz over one UTC day: the RSTN network's one-second archive from NOAA beside our own solar tracks, raw or binned onto common UTC edges, with a test of whether the two move together. RSTN runs about a month behind, and the last cell says which of our days can be compared yet. Downloads are cached in `~/.cache/srt_rstn/`. |

## Running them

They find the repository from their own location, so they run from here, from
the repository root, or from a copy elsewhere with `SRT_ROOT` set:

```
cd notebooks && jupyter lab
SRT_ROOT=/path/to/21-cm-radio-telescope-v2 jupyter lab   # from anywhere else
```

Recordings are read from `receiver_scheduler/data/observations/`. Nothing here
writes to the observatory's data — any output lands beside the notebook.

Either interpreter works: the observatory's radioconda
(`/home/astro/radioconda/bin/python`) or the project venv (`.venv`). Both were
checked against the same recording and give the same numbers.

`read_h1_data.ipynb` needs only `h5py`, `numpy` and `matplotlib`.
`solar_flux_scallop.ipynb` also imports `scallop.py` and `observation_plot.py`
from `receiver_scheduler/` — and `ephem`, `scipy` and `astropy` through them —
because reconstructing where the mount was *commanded* to point means
reproducing the firmware's own transform, and a hand copy in a notebook would
drift away from the firmware silently.

For a fresh venv, `receiver_scheduler/requirements.txt` covers all of it.
Install one package at a time on the observatory host; resolving the whole list
in one pip run was killed by the OOM killer there.

## A recording still being written

Both open a live file read-only and show what has arrived so far, so you can
watch an observation reduce while it runs. `docs/CALIBRATION.md` is the
reference for what every correction means and the order they are applied in.
