# B0329+54 timing products

Kept in git because they are hard to regenerate. Each folded profile is a night of telescope time, and the TOAs are a history no single recording can rebuild. Raw recordings stay in `data/observations/`, outside git.

- `b0329.tim`: tempo2-format TOAs, topocentric UTC at the observing frequency, site `ar` (our surveyed position, registered with PINT by `pint_tools.py`).
  - A line named `<recording>` is that night's TOA, and is fitted.
  - `<recording>_sN` are 4 h segments of the night, a check that is never fitted.
  - `-pps 0` marks a host-clock TOA.
- `B0329+54.par`: the ephemeris the fold uses, rewritten by `pulsar_toa.write_par`.
- `<name>.json`: the full record behind each TOA line (shift, SNR, template, clock, phase offset).
- `profiles/<recording>.npz`: each recording's whole-run fold at the absolute phase.
  - It holds 1024-bin weighted sums `S` and weights `C`, plus the phase convention and what the recording was.
  - The stacked profile on the pulsar plot is built from these, so a night counts even after its recording is gone.

Plot Result writes here. Committing is done by hand.
