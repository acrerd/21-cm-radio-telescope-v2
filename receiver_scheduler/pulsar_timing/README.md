# PSR B0329+54 timing products

Kept in git because they are hard to regenerate. Each folded profile is a night of telescope time, and the TOAs are a history no single recording can rebuild. Raw recordings stay in `data/observations/`, outside git.

- `b0329.tim`: one TOA per night. This is the file to fit.
  - A booked run is named `<recording>`. A pulsar-monitor window is named `night_<session>`, from all its pieces added at the absolute phase; the session is when its window opened, to the minute (e.g. `2026-09-28T1942`).
  - tempo2 format, topocentric UTC at the observing frequency, site `ar` (our surveyed position, registered with PINT by `pint_tools.py`).
  - `-pps 0` marks a host-clock TOA, `-pps 1` one timed by the PPS.
- `b0329_segments.tim`: the same nights cut into 4 h segments, `<recording>_sN`, and the pieces of a pulsar-monitor night, `<recording>_p`, same format.
  - A within-night check. Never fit it together with `b0329.tim`: it is the same data again, and a timing program has no way to know that, so a night would count twice.
  - Our plot shows the segments' residuals under the model fitted to the nights.
- `B0329+54.par`: the ephemeris the fold uses, rewritten by `pulsar_toa.write_par`.
- `<name>.json`: the full record behind each TOA line (shift, SNR, template, clock, phase offset).
- `profiles/<recording>.npz`: each recording's whole-run fold at the absolute phase.
  - It holds 1024-bin weighted sums `S` and weights `C`, plus the phase convention and what the recording was.
  - The stacked profile on the pulsar plot is built from these, so a night counts even after its recording is gone.

Plot Result writes here. Committing is done by hand.
