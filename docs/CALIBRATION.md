# How this telescope is calibrated

Written for whoever has to reduce, extend or distrust the numbers this
telescope produces. It says what each calibration measures, what it cannot
measure, where it is applied, and how to undo it.

The governing rule, and the reason the rest hangs together: **a recording is
always exactly reversible**. Every correction applied on the way to disk is
stored beside the data it was applied to, and `observation_plot.read_observation`
multiplies all of them back out, so the pipeline always works in raw counts.
Spectra are written in kelvin for whoever opens the file in a notebook; they
are never *only* in kelvin. A calibration that could not be undone would make
every later improvement a re-observation.

---

## 1. What has to be measured

A recorded spectrum is

```
counts(f, t)  =  G(t) · B(f, t) · [ T_sys(t) + T_A(f, t) ]
```

- `T_A` is the sky, the thing we want.
- `T_sys` is everything the instrument and its surroundings add: the
  SAWbird's 59 K, spillover onto the ground, the atmosphere, the cable.
  Measured 340–372 K.
- `B(f, t)` is the **shape** of the response across the band, normalised to
  unit median. Its slow part is the SAW filter's passband; its fine part is
  that filter's multi-transit echo ripple.
- `G(t)` is the **level**, counts per kelvin.

Four calibrations measure these, on four different timescales, and a fifth
converts kelvin to flux.

| what | measures | timescale it is good for | where |
|---|---|---|---|
| bandpass template | `B(f)` | days, and it goes stale | `bandpass.py` |
| gain and system temperature | `G`, `T_sys` | hours | `rf_calibration.py` |
| pilot bursts | changes in `G` and `B(f)` | a burst every 60 s of run, duty permitting | `pilot.py` |
| pilot carrier | fast common-mode `G` | every record | `pilot.py` |
| beam solid angle | kelvin → flux | measured once, per feed | `astro_simulator/instrument.py` |

---

## 2. The bandpass template

**What it is.** A polynomial in frequency, fitted to the mean spectrum of the
emptiest hydrogen field in the northern sky (the Lockman Hole, l=150 b=+52,
1.3 K of H I), with the line masked. Normalised to unit median over the band
it was fitted on, so dividing by it flattens the band without moving its
level: the counts-to-kelvin scale is a separate matter, deliberately not
folded in.

**Two templates, one scale.** `bandpass_template.json` for the H I product and
`bandpass_template_wide.json` for the continuum product. The wide one is
normalised over the *H I* band, so the single gain — fitted on the H I
product — is the right scale for both.

**It refuses rather than warns.** A template's polynomial is a function of
frequency *relative to the local oscillator it was fitted at*, and the
hardware's passband moves with the oscillator. Evaluated at a different
tuning it still returns finite numbers over whatever frequencies overlap:
retuning by 1.4 MHz once left 60% of channels "covered" by a shape displaced
from the real passband and marked valid. So `bandpass.applies_to` checks the
tuning and a mismatch leaves the file in counts.

**What it cannot do**, and this is the reason the pilot exists: the SAW
filter is outdoors on the dish, its substrate drifts tens of parts per
million per degree, and its echo ripple — 0.1–0.15% of the passband at a
160 kHz period — correlates 0.83 within a run, 0.80 within an evening and
only 0.55 from one day to the next. A stored template cannot follow it, and
it is the dominant residual of a gain fit: 0.48 K against a 0.15 K thermal
floor.

## 3. Gain and system temperature

**What it is.** A straight-line fit of measured counts against the simulator's
sky model along one line of sight:
`counts = G · (T_sys + T_model)`. Two parameters, least squares, on a plane
field where the H I line gives the fit its lever arm.

**Anchored on the H I line.** The line is the only part of the sky whose
brightness is known independently, from HI4PI through the measured beam. The
velocity shift of the B210's clock is fitted alongside, and **carried between
observations only from a fit whose correlation says a line held it** (≥0.99):
a shift with no line to hold it slides onto whatever is nearby and reports a
confident number. Constrained fits give −2.4 to −3.0 ppm over three weeks,
inside the part's ±2 ppm specification.

**It goes stale with temperature.** The fitted gain fell 2.1% and T_sys 4.3 K
between midday and evening on 2026-08-25, about 0.3% an hour through the
evening transition. Calibrate next to the observation that needs absolute
temperatures; for a transit the offset is constant and harmless.

**It is field-dependent at the 15% level.** Three fits in fifteen hours gave
8.49e-7 (l=78 b=+2), 1.00e-6 (l=108 b=+12) and 9.25e-7 counts/K (l=184 b=0)
with the total power constant to 5%: what differs is the measured line
against the model, and the fit moves `G` and `T_sys` against each other to
absorb it. Both plane fields anchor low and the smooth field high, which is
the sign expected from stray radiation in sky-directed sidelobes. Until that
is measured, treat the kelvin scale as good to about ±8% depending on where
it was anchored, and anchor consistently.

## 4. The pilot

The receiver's own reference: the B210's transmitter, through fixed pads and
the old SRT calibration dipole at the dish's vertex, radiating into the feed
so that **everything downstream of the feed is inside the measurement** — the
probe, the horn, the SAWbird, the cable down the mount, the converter. Full
detail in `receiver_scheduler/pilot.py`; the design record is issue #30.

### 4a. Bursts — the passband

Every so often the transmitter sends a **full-band comb**, one tone on every
bin, at a level near the system noise, for the length of one record. That
record is flagged (`pilot_burst = 1`) and **dropped from the science** by
`read_observation`, so every consumer is safe without knowing about it. Every
other record carries no comb at all: nothing to exclude, subtract or explain.

**How often is a time, not a record count.** A record is not a fixed length —
3 s on a solar track, 10 s on a calibration field, 60 s on a drift scan — so
"every twentieth record" would mean a burst a minute in one case and one every
twenty minutes in another. The configuration asks for an interval in seconds
(`burst_interval_s`, 60 s) and the receiver converts it at start-up using that
observation's integration time. But **a burst costs one whole record whatever
its length**, so the interval and the cost cannot both be held: a duty cap
(`max_duty_cycle`, 5%) takes over at long integrations.

| record length | burst every | cost |
|---|---|---|
| 0.5 s | 60 s (120 records) | 0.8% |
| 3 s | 60 s (20 records) | 5% |
| 10 s | 200 s (20 records) | 5% |
| 60 s | 20 min (20 records) | 5% |

So an observation that needs a tight calibration cadence should use **shorter
records** — the plots bin them back down anyway — rather than pay a larger
fraction of its samples. That matters most for the tilt, which is only
measured at bursts: on a drift scan the continuum band sits entirely on one
side of the anchor, so a tilt that changes by 0.1% per MHz between bursts
moves the band mean by 0.17%.

A burst yields the complex response per bin, and from it:

- **level** and **slope** — the power gain at band centre and its fractional
  change per MHz — applied to the records up to the next burst;
- a **passband correction vector**, averaged over the bursts of the last half
  hour, applied to the same records.

**A block is judged to hold the comb by the comb's own signature**, not by
the command and not by the power. The cross-spectrum carries a phase ramp
across the band — the fixed framing offset between transmit and receive — so
its inverse transform peaks at that offset, and the height of that peak
against the block's *own* noise is the statistic. Per 20 ms block:

| | statistic |
|---|---|
| no comb | 2.6 σ (the expected maximum of 1024 Rayleigh draws) |
| a twentieth of the block covered | 32 σ |
| the whole block | 286 σ |
| the whole block, with the Sun in the beam | 286 σ |

The threshold is 8. Nothing is compared with a running baseline and nothing
depends on how bright the sky is, which is what makes it safe.

Two earlier designs are recorded here because both were worse, and both were
built before this one. Judging a block by its **power** against a median of
recent blocks works, but it is second-hand: it deadlocks the first time the
received power steps up and stays up — a slew onto the Sun, or the carrier
starting a moment after the baseline formed — because every block then reads
"on", the baseline never updates again, and every science record is flagged
as contaminated and dropped. Its margin also has to beat the per-block
scatter while still catching a comb that raises the total power by only 30%
when the Sun is in the beam. Trusting the **command** is simpler still, but
silent: the transmit buffers empty tens of milliseconds after the comb is
switched off, and a record that caught that tail would be reduced as sky — a
comb at five times the noise over 1% of a 3 s record is a 7 K error. The
coherent test needs neither, and catches a tail a twentieth of a block long.

**A pilot that is never detected is given up on.** It would otherwise cost one
record an interval for nothing, which is the state until the vertex dipole is
wired. After five undetected bursts with none ever seen the run stops sending
them and says so once; the carrier keeps running, since it costs no records.
A pilot seen once is never given up on — an intermittent one is a fault to
record, not a reason to stop measuring.

Three more details that are easy to get wrong, each of which cost a rewrite:

- **The recovery has its own unwindowed FFT.** The frame is periodic at
  exactly the transform length, so under a rectangular window it has no
  leakage at all and the reference is flat across the band. Through the wide
  product's Blackman-Harris window the reference instead follows the window
  in *time*, and since a swept chirp maps time to frequency, it falls to zero
  at both band edges — exactly where the filter's tilt must be measured.
- **Everything applied to counts is a power.** The recovered response is a
  field response and the counts are powers, so the ratio is squared. Fitting
  the amplitude ratio and applying it to counts would be wrong by a factor of
  two in the departure from unity, and invisible at the percent level.
- **A correction from too little pilot is worse than none.** One 3 s burst
  gives 0.18% per channel against a 0.11% ripple, and because the same vector
  divides every record, that noise is a *systematic*, not something that
  averages away. The threshold is in **accumulated pilot seconds**
  (`min_shape_pilot_s`, 24 s), not in bursts: a burst is one record, records
  are not a fixed length, and eight 3 s bursts carry the same information as
  a third of one 60 s burst. Counting bursts would have refused every
  correction on a 60 s drift scan while accepting a worse one at 3 s. For the
  same reason the bursts in the window are averaged **weighted by their own
  lengths**.

The correction is denoised in the **delay domain**: the response is a few SAW
echoes at 1.5–6.2 µs, so it is sparse in delay, and keeping only |τ| < 10 µs
discards most of the per-bin noise and none of the ripple. Simulated, with the
burst at five times the system noise:

| pilot accumulated | residual per channel | on a 360 K system |
|---|---|---|
| the ripple itself, uncorrected | 0.106% | 0.38 K |
| 24 s (the threshold) | 0.053% | 0.19 K |
| 90 s (half an hour at 5% duty) | 0.028% | 0.10 K |

against the 0.15 K thermal floor of a gain fit. The delay filter is worth a
factor of 2.2 of that. Note that the accumulated pilot in a half-hour window
is 90 s whatever the record length, since the duty cycle is what sets it — a
long record gives fewer but proportionately more precise bursts.

### 4b. The carrier — the fast wobble, every record

A single carrier runs **continuously**, in every record including the bursts,
at 1415.3 MHz: in the 800 kHz between the low edge of the sampled band and
the continuum band, where nothing is measured, so no exclusion machinery is
needed. The analogue bandwidth is twice the sample rate, so nothing rolls off
there.

It exists because a burst measures the gain at one instant a minute, while the
common-mode wobble (`GAIN_INSTABILITY`, 2.3×10⁻⁴ per 60 s record) is white on
that timescale — the one regime where neither per-channel thermal averaging
nor a switched reference saves an unswitched band-integrated drift scan. At
about a thousand times the noise in its own bin the carrier needs no coherent
recovery: its power is simply an excess in the channels of the wide product it
already lands in, and per 60 s record that is good to 6.5×10⁻⁵ — the thermal
floor.

**It is measured against the last burst**, so the two never count the same
change twice: at a burst the carrier reads unity by construction, and between
bursts it carries the departure since. A reference older than the burst
level's own hold window is stale and stops being used, rather than quietly
becoming a measurement of the slow drift the burst is supposed to carry.

**To apply it retrospectively** from a file where `tone_apply` was off: each
science record's `pilot_tone_power` divided by that of the burst record
before it is the factor, and `pilot_tone_ok` says whether the carrier was
detected. Nothing else is needed, which is the point of recording the series
whether or not it is used.

**It is recorded but not applied by default** (`tone_apply`). Applying it
corrects a wobble in the SAWbird or the converter, does nothing for an
atmospheric one, and would substitute the transmit chain's own wobble for the
receive chain's. The bench correlation test of issue #30 — tone-bin power
against off-tone band power over a settled hour — decides, and the series is
recorded meanwhile so the decision can be made on real data.

### 4c. With the transmitter unconnected

Which is the state until the vertex dipole is wired. Nothing is detected,
nothing is applied, unit factors are written, and the file reduces identically
to one made with the pilot off — less the burst records, which are still
flagged and dropped. Verified on the hardware: bursts on exactly every N-th
record, zero detected, spectra in kelvin as before, and no measurable leakage
from the transmit port into the receive path (burst and science records agree
in band power to 0.9%, which is the sky's own drift). The front end's 42 dB of
gain is what makes internal leakage harmless: it amplifies the wanted path
through the dipole and not the leak.

## 5. Kelvin to flux

`A_e = λ²/Ω`, the antenna theorem, with the **measured** main-lobe solid angle
— 23.7 square degrees, a Gaussian-equivalent 4.57°, integrated directly from
three Sun drifts at 40, 30 and 20 dB on 2026-09-15. That gives 6.18 m²
against a physical 7.07, and it is an **upper bound**: the sidelobes and the
~10% spillover lie outside the integrated lobe, so the true effective area is
smaller by the main-beam efficiency. On this scale the Sun reads 79 SFU
against the reference network's 75, Cas A 1.15 times its model and the Moon
1.20 times a 225 K disc.

Solar work is corrected to above the atmosphere with the same zenith opacity
the drift fits use, and the professional measurement for the same day — the
RSTN 1415 MHz local-noon flux from NOAA SWPC — is quoted beside ours on the
plot. The stations disagree by 10–20 SFU with each other, so agreement to
within that is agreement.

---

## 6. The order things are applied, and how to undo them

On the way to disk, per record:

```
kelvin = counts / (bandpass · G · pilot_factor(level, slope, f)
                   · pilot_shape(f) · pilot_tone_level)  −  T_sys
```

Everything in that denominator travels in the file: `bandpass_correction`,
`applied_gain_counts_per_k`, `applied_t_sys_k`, `pilot_level`, `pilot_slope`,
`pilot_correction_index` into `pilot_correction_h1`/`_wide`, and
`pilot_tone_level`. `read_observation` multiplies them all back and returns
counts, and drops the burst records on the way.

**The units are in the dataset name**, not in an attribute: `spectra_kelvin`
means the corrections were applied, `spectra_linear` means the calibration did
not apply to that tuning and the data are raw. Asking for the wrong one raises
an error rather than silently returning the other scale.

**Why the round trip is not wasted.** Fitting a gain from spectra a gain has
already been applied to is circular: it would return unity and a system
temperature of zero, while looking like a perfect calibration.

## 7. What is not calibrated, and what it would take

- **Absolute scale independent of HI4PI.** Everything rests on a model line.
  A noise diode with a known excess noise ratio, or a hot/cold load, would
  break that circle. The pilot does not: it is a transfer standard, not an
  absolute one.
- **Stray radiation in sky-directed sidelobes**, which is the most likely
  cause of the 15% field-to-field spread in the gain. Measured by observing a
  smooth field and a plane ridge at two hour angles and comparing the ratio.
- **Main-beam efficiency**, which turns the effective-area upper bound into a
  number. A clean Moon drift is the best route: a known disc temperature at a
  known size.
- **The fast wobble's origin** — receiver, transmitter or atmosphere — which
  decides whether the carrier is applied.
- **The pilot's own absolute scale.** The gain job does not yet store a
  `pilot_reference`, so every run is referenced to its own first burst and
  the pilot removes drift *within* a run, not the offset from the
  calibration. When that reference is stored it must carry the tuning it was
  taken at — the receiver refuses one whose gain, local oscillator or rate
  differ, because the level is a power ratio and a 10 dB gain override would
  read as a factor of ten and have every burst refused.

## 8. Operating rules

- Anchor the gain on the same field when comparing across days; the
  field-to-field spread is larger than any real change you are likely to be
  looking for.
- Re-measure the bandpass and the gain after any change to the tuning. The
  Configuration tab warns before saving one, and every recording after it is
  in counts until both are refitted.
- Record solar work at 30 dB. At 40 dB the receiver compresses the Sun's peak
  by about 5.7%; 30 and 20 dB agree to half a percent.
- A recording made before 2026-09-15 carries the old beam, and its fluxes
  re-reduce 22% lower on the corrected solid angle.
