# PSR B0329+54 timing products

Kept in git because they are hard to regenerate. Each folded profile is up to 16 h of telescope time, and the TOAs are a history no single recording can rebuild. Raw recordings stay in `data/observations/`, outside git.

- `b0329.tim`: one TOA per run or pulsar-monitor session. This is the file to fit.
  - A booked run is named `<recording>`. A pulsar-monitor window is named `session_<session>`, from all its pieces added at the absolute phase; the session is when its window opened, to the minute (e.g. `session_2026-09-28T1942`). "Session", not "night": B0329+54 is circumpolar here and the window follows it round the clock, so from December it is a daytime run. These were named `night_<session>` until 2026-10-09, and `pulsar_toa` renames any such line as it reads the file.
  - tempo2 format, topocentric UTC at the observing frequency, site `ar` (our surveyed position, registered with PINT by `pint_tools.py`).
  - `-pps 0` marks a host-clock TOA, `-pps 1` one timed by the PPS.
- `b0329_segments.tim`: the same runs cut into segments of about 4 h (a run of h hours into round(h/4) equal parts), `<recording>_sN`, and the pieces of a pulsar-monitor session, `<recording>_p`, same format.
  - A within-run check. Never fit it together with `b0329.tim`: it is the same data again, and a timing program has no way to know that, so a run would count twice.
  - Our plot shows the segments' residuals under the model fitted to the runs and sessions.
- `B0329+54.par`: the ephemeris the fold uses, rewritten by `pulsar_toa.write_par`.
- `<name>.json`: the full record behind each TOA line (shift, SNR, template, clock, phase offset).
- `profiles/<recording>.npz`: each recording's whole-run fold at the absolute phase.
  - It holds 1024-bin weighted sums `S` and weights `C`, plus the phase convention and what the recording was.
  - It carries the recording's calibration: `cal_t_sys_k` and `cal_effective_area_m2`, from the file's `cal_*` attributes (written from 2026-09-30), else from the calibration in force when the profile was stored; `cal_recorded` says which (1 from the file).
  - The stacked profile on the pulsar plot is built from these, so a run counts even after its recording is gone. Each run is converted to kelvin and janskys on its own T_sys and A_e before the runs are added (`pulsar_toa.accumulated_profile(calibrated=True)`), so a re-calibration never rescales an old run.

The scheduler writes here when every pulsar run ends (`update_pulsar_timing`), and Plot Result rewrites a run's lines. Committing is done by hand.
