# How this telescope is calibrated

Written for whoever has to reduce, extend or distrust the numbers this
telescope produces. It says what each calibration measures, what it cannot
measure, where it is applied, and how to undo it.

**The governing rule: a recording is always exactly reversible.** Every
correction applied on the way to disk is stored beside the data it was applied
to, and `observation_plot.read_observation` multiplies all of them back out, so
the pipeline always works in raw counts. Spectra are written in kelvin for
whoever opens the file in a notebook; they are never *only* in kelvin. A
calibration that could not be undone would make every later improvement a
re-observation.

Read §1 and §2 first. §3–§6 are the detail behind each step, §7–§9 are where
the numbers end up, and the appendix records designs that were tried and are
worse, so nobody rebuilds them.

---

## 1. What has to be measured

A recorded spectrum is

```
counts(f, t)  =  G(t) · B(f, t) · [ T_sys(t) + g(t) · T_A(f, t) ]
```

| term | is | measured |
|---|---|---|
| `T_A` | the sky — the thing we want | — |
| `T_sys` | everything the instrument and its surroundings add: the SAWbird's 59 K, spillover onto the ground, the atmosphere, the cable | 187.7 K (l=72 b=0, 2026-09-30); 340–372 K on the old feed, before 2026-09-22 |
| `B(f, t)` | the **shape** of the response across the band, normalised to unit median: the SAW filter's passband, and that filter's multi-transit echo ripple | — |
| `G(t)` | the **level**, counts per kelvin | 1.460×10⁻⁷ (same fit); ~8–9×10⁻⁷ on 2026-09-15/16, not comparable: those fits were at 30 dB receiver gain (20 dB from 2026-09-17) and through the old feed (replaced 2026-09-22) |
| `g(t)` | the **beam's gain on the source**, where the source is small against the beam: 1 when the beam is centred, less when the mount is pointed off | 0.98–1.00 |

Note where `g` sits in that line. It multiplies `T_A` and **not** `T_sys`,
because the system temperature does not care where the dish is looking. Every
other term multiplies the whole bracket. That one difference decides the order
everything is applied in, and getting it wrong costs the ratio of the two —
22% on the Sun.

Five calibrations measure these, on five different timescales, and a sixth
converts kelvin to flux.

| what | measures | good for | where |
|---|---|---|---|
| bandpass template | `B(f)` | days, and it goes stale | `bandpass.py` |
| gain and system temperature | `G`, `T_sys` | hours | `rf_calibration.py` |
| pilot bursts | changes in `G` and `B(f)` | one burst per 60 s of run, duty permitting | `pilot.py` |
| pilot carrier | fast common-mode `G` | every record | `pilot.py` |
| tracking scallop | `g(t)` on a tracked compact source | one observation, fitted from its own records | `scallop.py` |
| beam solid angle | kelvin → flux | once, per feed | `astro_simulator/instrument.py` |

The first two are **measured beforehand, on separate jobs, and stored on
disk**. The two pilot calibrations are **measured inside the observation
itself**, at write time — when the pilot is enabled, which it has not been by
default since 2026-09-22 (§3). The scallop is measured inside the observation too,
but **afterwards, in the reduction** — everything it needs is derivable from
the file, so it is never written and an old recording improves the day the
pointing model or the measured beam does. The beam solid angle is measured
once and rarely changes.

---

## 2. The whole chain in one pass

What happens to one observation, in order. Each step names the section with
the detail.

**Before the run**

1. A **bandpass job** tracks the Lockman Hole, records a few minutes, fits a
   polynomial to the mean spectrum with the line masked, and stores
   `bandpass_template.json` and `bandpass_template_wide.json`. Since
   2026-09-25 the same fit can be made from a recording already on disk
   (`/api/rf/bandpass/from-recording`). A new template stales the gain. (§5)
2. A **gain job** tracks a plane field, records, and fits `counts = G·(T_sys +
   T_model)` against the simulator's sky. Stores `gain_calibration.json`. (§5)

**At start-up of the observation**

Steps 3, 4 and 6–9 run only with the pilot enabled. It is off by default
since 2026-09-22 (§3); with it off, steps 5 and 10 are the whole write-time
chain.

3. The receiver builds the **pilot plan** from the tuning: which wide bins
   carry comb tones, the two transmit frames, and the reference spectrum the
   recovery correlates against. (§3)
4. It converts `burst_interval_s` into a **burst every N records** using this
   observation's integration time, floored by the duty cap. (§3)
5. It copies the stored template and gain into the file as
   `bandpass_correction`, `applied_gain_counts_per_k`, `applied_t_sys_k`, and
   writes every attribute, so the file is complete before the first record and
   can be opened SWMR while it is still being written. (§7)

**Per record, during the run**

6. The transmitter sends the **carrier** continuously. On a burst record it
   also sends the **comb**, switched off `burst_off_margin_s` before the
   record ends. (§3)
7. A gate decides, per 20 ms block, whether the comb is present, **by the
   comb's own coherent signature**. On-blocks accumulate a cross-spectrum;
   off-blocks accumulate the noise. (§4)
8. At the end of a burst record the accumulation is turned into a **complex
   response per comb bin**, `h`. (§4)
9. From `h` against the reference: a **level** and **slope** applied to the
   records up to the next burst, and a **passband correction vector** averaged
   over the bursts of the last half hour. The carrier's power gives a flat
   per-record factor, recorded but not applied. (§6)
10. The science record is divided by bandpass × gain × pilot factors, `T_sys`
    subtracted, and written in kelvin — with every factor stored beside it.
    Burst records are written uncorrected and flagged. (§7)

**Afterwards, in the reduction**

11. `read_observation` multiplies all of it back, drops the burst records, and
    hands back counts — **except the pilot when asked for `keep_pilot=True`**,
    which a plot does and a fit does not, because the pilot is the only
    correction that cannot be rebuilt from the file. (§8, §10)
12. The bandpass and the gain are re-applied from the stored template, `T_sys`
    is subtracted, and the channels outside the continuum window are dropped.
    What is left is **antenna temperature**. (§5)
13. On a tracked compact source, the **tracking scallop** — the beam walking
    ±0.25° across the source as the drive crosses encoder pulses — is fitted
    from the run's own records and divided out. This is `g(t)` from §1, so it
    happens **here**, after `T_sys` is subtracted, and never on the counts.
    (§5)
14. Kelvin becomes flux through the measured beam solid angle, and solar work
    is corrected to above the atmosphere. (§5)
15. Plots and fits say on their face what was and was not applied — which
    pointing model the scallop used, how many records the pilot reached, and
    the reason whenever either was refused. (§9)

Steps 11–15 are all reversible-by-omission: none of them writes to the
recording, so a file re-reduces from scratch every time and improves when the
template, the pointing model or the measured beam does.

---

## 3. What is injected, and when

The pilot is the receiver's own reference: the B200's transmitter, through
fixed pads and the old SRT calibration dipole at the dish's vertex, radiating
into the feed so that **everything downstream of the feed is inside the
measurement** — the probe, the horn, the SAWbird, the cable down the mount,
the converter. Detail in `receiver_scheduler/pilot.py`; the design record is
issue #30.

**Status: off by default since the evening of 2026-09-22**
(`PILOT_DEFAULTS["enabled"] = False`; `receiver_pilot_enabled: false` in the
live config). The dipole was wired that afternoon — a 30 dB pad, an axial
stub, `receiver_pilot_tx_gain_db` 70 in the live config against the
`tx_gain_db` default of 10 — and the first sky runs showed the carrier's
received level stepping by up to 1.6% at every host stall (a TX underflow with
an RX overflow, counted per record in `underflows`) and staying there, and
drifting three times as far as the receiver between stalls (r = 0.97 in
shape). Dividing by it makes a series worse. The bursts are unproven. What
follows is the design as built; it is parked in issue #46.

**Enabling the TX chain costs 8.4% of receiver sensitivity**, instantly, in
both products, whatever the TX gain: an A-B-A on l=84 b=4 on 2026-09-22 read
8.41 ± 0.01% (continuum) and 8.53 ± 0.02% (H I) higher with the TX off than
on, the two on-legs agreeing to 0.3%, and a leg
with the TX enabled at 0 dB sat at the 70 dB level to 0.02%. It is the
AD9364's state with its transmitter on, not the radiated carrier. A further
~1.5% start-up transient with a few-minute time constant rides on top when the
TX is on. So a gain belongs to one TX state, and **the pilot is never toggled
per observation**: a recording made in the other state reads 8.4% off the gain
in force, and its kelvin are wrong by that much though nothing flags it.

Two things are transmitted, and they are transmitted from **separate sources**
that are added together, so switching one never disturbs the other.

### The carrier — continuous

One tone at **1415.3 MHz**, in every record including the burst records, at
`tone_amplitude` (0.0791 of DAC full scale).

**Its amplitude is not independent of the comb's.** Both come off one transmit
chain and one sink, so `tx_gain_db` moves them together — and the carrier runs
in *every* record, not just the bursts. A CW tone has a crest factor of 1.0
while the comb is peak-limited by its Schroeder crest of 1.35, so at equal
amplitudes the carrier is far from negligible: at the old 0.25 it was 46% of
the comb's mean power, putting 2.24× the whole band's noise power in the band
continuously. When the transmit gain went up 10 dB on 2026-09-17 the amplitude
was divided by √10 to hold the carrier exactly where it was. Left alone it
would have reached 23× of band noise in every science record — most of the
headroom the receiver-gain change had just bought, and a strong in-band CW
tone is the classic source of intermodulation products, which land back across
the measured band rather than staying in their own unmeasured bin.

It sits in the 800 kHz between the low edge of the sampled band and the
continuum band, where nothing is measured, so no exclusion machinery is
needed; the analogue bandwidth is twice the sample rate, so nothing rolls off
there. It does sit in the SAW skirt, which is why it carries only the *fast*
part of the gain — the bursts track the skirt's own slow motion.

It exists because a burst measures the gain at one instant a minute, while the
common-mode wobble (`GAIN_INSTABILITY`, 2.3×10⁻⁴ per 60 s record) is white on
that timescale. That is the one regime where neither per-channel thermal
averaging nor a switched reference saves an unswitched band-integrated drift
scan.

### The comb — in bursts

A **full-band comb**, one tone on every wide bin except the LO guard
(`dc_guard_bins`, 4) and the carrier's neighbourhood (`tone_guard_bins`, 6),
at `burst_amplitude` (0.5 of full scale), with Schroeder phases so the crest
factor is ~√2 and the burst carries its power without hitting the rails.

It runs for the length of **one whole record**, switched off
`burst_off_margin_s` (0.3 s, capped at a quarter of the record) before the
record ends. That record is flagged `pilot_burst = 1` and **dropped from the
science** by `read_observation`. Every other record carries no comb at all —
nothing to exclude, subtract or explain.

### How often a burst is sent

**The cadence is a time, not a record count.** A record is not a fixed
length — 3 s on a solar track, 10 s on a calibration field, 60 s on a drift
scan — so "every twentieth record" would mean a burst a minute in one case and
one every twenty minutes in another. The configuration asks for an interval in
seconds (`burst_interval_s`, 60 s) and `pilot.burst_every()` converts it at
start-up. But **a burst costs one whole record whatever its length**, so the
interval and the cost cannot both be held: `max_duty_cycle` (5%) takes over at
long integrations.

| record length | burst every | cost |
|---|---|---|
| 0.5 s | 60 s (120 records) | 0.8% |
| 3 s | 60 s (20 records) | 5% |
| 10 s | 200 s (20 records) | 5% |
| 60 s | 20 min (20 records) | 5% |

So an observation that needs a tighter calibration cadence should use
**shorter records** — the plots bin them back down anyway — rather than pay a
larger fraction of its samples. That matters most for the tilt, which is only
measured at bursts: on a drift scan the continuum band sits entirely on one
side of the anchor, so a tilt changing by 0.1%/MHz between bursts moves the
band mean by 0.17%.

**A pilot that is never detected is given up on** after `give_up_after_bursts`
(5), since it otherwise costs one record an interval for nothing — the state
of every run before the vertex dipole was wired. The carrier keeps running, costing
no records. A pilot seen even once is never given up on: an intermittent one
is a fault to record, not a reason to stop measuring.

---

## 4. How it is recovered

### Deciding a block holds the comb

Per 20 ms block, the statistic is the **height of the cross-spectrum's delay
peak against that block's own noise**. The fixed framing offset between
transmit and receive puts a phase ramp across the band, so the inverse
transform of the cross-spectrum peaks at that offset; the peak against the
block's own noise is an absolute measure.

| | statistic |
|---|---|
| no comb | 2.6 σ (the expected maximum of 1024 Rayleigh draws) |
| a twentieth of the block covered | 32 σ |
| the whole block | 286 σ |
| the whole block, Sun in the beam | 286 σ |

Threshold `gate_sigma` = 8. **Nothing is compared with a running baseline and
nothing depends on how bright the sky is**, which is what makes it safe — see
the appendix for the two designs that did depend on one.

A block is committed only when it and both its neighbours agree, which drops
the partial block at each burst edge: two blocks in 150, where counting a
partial one as whole would bias the response by up to 1.3%. On-blocks
accumulate `Σ F conj(R)`; off-blocks accumulate the noise power per bin.

**Do not put the gate's arithmetic in the Python block.** The first version did
it per frame and cost 55–70% of a core, because GNU Radio hands a 1024-point
vector sink 3.2 frames a call whatever `set_min_noutput_items` asks. The
multiply, the magnitude and both integrations are C++ blocks; the Python sink
runs at 50 Hz. Measured with the pilot exactly as a scheduled observation runs
it, 90 s with 9 bursts: zero overflows, zero underruns, 2.65 cores of 8.

### Turning the accumulation into a response

`pilot.estimate()` takes the accumulated cross-spectrum, the frame count and
the off-block noise, finds the framing offset from the peak of the inverse
transform and removes it, and returns

- `h`, the chain's complex **field** response per comb bin,
- `snr` per bin and its median.

Three details that each cost a rewrite:

- **The recovery has its own unwindowed FFT.** The frame is periodic at
  exactly the transform length, so under a rectangular window it has no
  leakage and the reference is flat across the band. Through the wide
  product's Blackman-Harris window the reference instead follows the window in
  *time*, and since a swept chirp maps time to frequency, it falls to zero at
  both band edges — exactly where the filter's tilt must be measured.
- **Everything applied to counts is a power.** `h` is a field response and
  counts are powers, so the ratio is `|h|²`. Fitting the amplitude ratio and
  applying it to counts would be wrong by a square: a factor of two in the
  departure from unity, invisible at the percent level.
- **The reference matters.** `h` is compared against `pilot_reference` — the
  calibration's stored reference where the gain job has written one (not yet),
  otherwise the run's own first detected burst, with `pilot_anchored = 0` to
  say so. Self-referenced, the pilot removes drift *within* a run but not the
  offset from the calibration.

---

## 5. The measurements that do not come from the pilot

### The bandpass template

A polynomial in frequency, fitted to the mean spectrum of the emptiest
hydrogen field in the northern sky (the Lockman Hole, l=150 b=+52, 1.3 K of
H I) with the line masked. Normalised to unit median over the band it was
fitted on, so dividing by it flattens the band without moving its level: the
counts-to-kelvin scale is a separate matter, deliberately not folded in.

**Two templates, one scale.** `bandpass_template.json` for the H I product,
`bandpass_template_wide.json` for the continuum product. The wide one is
normalised over the *H I* band, so the single gain — fitted on the H I
product — is the right scale for both.

**It refuses rather than warns.** A template's polynomial is a function of
frequency *relative to the local oscillator it was fitted at*, and the
hardware's passband moves with the oscillator. Evaluated at a different tuning
it still returns finite numbers over whatever frequencies overlap: retuning by
1.4 MHz once left 60% of channels "covered" by a shape displaced from the real
passband and marked valid. So `bandpass.applies_to` checks the tuning, and a
mismatch leaves the file in counts.

**What it checks is the LO and the sample rate, not the receiver gain** —
unlike the gain calibration, which refuses on all three. That is defensible:
the template is normalised to unit median, so it carries shape alone, and an
amplifier's compression responds to the *total* power through it and takes the
whole band down together, which divides out of a normalised shape. It is not
proven, though, and the second-order term has never been measured. Re-measure
the template after a receiver-gain change anyway: nothing refuses a template
fitted at another gain, and it would be applied silently.

**In force:** order 9, fitted 2026-09-30 by a live job on l=150 b=+52
(38 records, 20 dB, TX off), residual 0.18% (H I) and 0.14% (wide).

**From a recording on disk.** Since 2026-09-25 the RF tab can fit both
templates from a finished recording (`/api/rf/bandpass/from-recording`): a
tracked spectrum at the instrument in force (LO, rate and receiver gain all
checked), Sun runs refused. A template wants a long, high-latitude, TX-off
run chosen after the fact, which a few minutes of live job on whatever sky is
up cannot always give. The template in force from 09-22 to 09-25 was a
37-record live job with the TX on; its 0.5 K error was the S-shaped residual
on every spectrum in between, correlated 0.84–0.97 across plane fields, and
gone when a 601-record TX-off run at l=71.7 b=+44 replaced it.

`bandpass.fit_from_observation` **writes the template in force** unless called
with `save=False`; an experiment overwrote it on 09-25. And **the template and
the gain are a pair**: the gain was fitted through the template, so replacing
the template stales the gain, and the gain must be refitted after it.

**What it cannot do, and the reason the pilot exists:** the SAW filter is
outdoors on the dish, its substrate drifts tens of parts per million per
degree, and its echo ripple — 0.1–0.15% of the passband at a 160 kHz period —
correlates 0.83 within a run, 0.80 within an evening and only 0.55 from one
day to the next. A stored template cannot follow it, and it is the dominant
residual of a gain fit: 0.48 K against a 0.15 K thermal floor.

### Gain and system temperature

A straight-line fit of measured counts against the simulator's sky model along
one line of sight: `counts = G·(T_sys + T_model)`. Two parameters, least
squares, on a field where the H I line gives the fit its lever arm.

It is an ordinary regression with an intercept, and that has a consequence
worth knowing: **the slope is immune to anything flat in frequency, and the
intercept absorbs it.** So ground spillover — broadband — lands wholly in
`G·T_sys`, and only something spectral can move `G`. Reported `T_sys` is
derived as intercept/slope and is *not* an independent quantity; do not read a
trend in it without checking the intercept itself.

**In force:** 1.460×10⁻⁷ counts/K, `T_sys` 187.7 K, fitted on l=72 b=0 on
2026-09-30, correlation 0.9992, 20 dB, TX off. The feed was replaced on
2026-09-22 (`T_sys` 349 → ~187–209 K); every gain and `T_sys` from before that
date belongs to the old feed.

**Anchored on the H I line.** The line is the only part of the sky whose
brightness is known independently, from HI4PI through the measured beam.

**No velocity shift is fitted or carried on the locked reference**
(2026-09-30). A recording is on it when its `clock_source` is `external`,
`clock_ref_locked` is 1, and the Thunderbolt's `reference_state` is not `bad`
or `stale` (`rf_calibration.reference_locked`). Then the frequency axis is
good to well under 10⁻⁹: a calibration fitted on such a recording holds the
shift at zero (`fit_shift=False`, marked `shift_fixed` and `reference_locked`),
and `trustworthy_velocity_shift` returns nothing for a locked recording or a
locked calibration, so no clock correction is applied either way. The
calibration in force carries `velocity_shift_km_s` 0, `shift_fixed` and
`reference_locked` true. Until 2026-09-30 the 09-25 calibration's −0.225 km/s
(−0.75 ppm, fitted on the plane field l=138 b=−1) was applied to every
spectrum as a "receiver clock" correction.

On the TCXO (before the Thunderbolt, 2026-09-22) the shift was fitted
alongside the gain and carried only from a fit whose correlation said a line
held it (≥0.99), since a shift with no line to hold it slides onto whatever is
nearby. It is a clock term only on a field where pointing cannot move it:
l=148 b=8 read −0.60 to −0.63 km/s on the TCXO over four weeks (−2 ppm, in
spec) and −0.075 km/s (−0.25 ppm) on the Thunderbolt. A plane field moves
0.5–0.7 km/s per 0.5° of longitude pointing, so its fitted shift reads the
pointing and the model, not the clock. Never quote a ppm from a plane-field
fit.

**It goes stale with temperature.** The fitted gain fell 2.1% and `T_sys`
4.3 K between midday and evening on 2026-08-25, about 0.3% an hour through the
evening transition. Calibrate next to the observation that needs absolute
temperatures; for a transit the offset is constant and harmless.

**It varies by 10–15% between fits, and the cause is not settled.** Three fits
in fifteen hours on 2026-09-15/16 gave 8.49e-7 (l=78 b=+2), 9.38e-7 (l=108
b=+12) and 9.23e-7 counts/K (l=184 b=0) with the total power constant to 5%:
what differs is the measured line against the model, and the fit moves `G` and
`T_sys` against each other to absorb it. This was read as a *field* effect
until 2026-09-17, when the A/B test below showed the same field, l=108 b=+12,
spanning the same 8.14–9.38e-7 across time on its own. All of these are on
the old feed and at 30 dB receiver gain, so their level is not comparable with today's; the spread has not been re-measured on the new feed. Treat the kelvin scale
as good to about ±8% depending on where it was anchored, and **anchor
consistently**.

Two candidate causes are open, and they are separable by the rule above:

- **Stray H I in sky-directed sidelobes** would move the *slope*. Measured
  2026-09-16/17 by observing a smooth field and two plane ridges at two hour
  angles: sorted by the measured horizon floor at the pointing azimuth, the
  two open-horizon pointings give 8.51e-7 against 8.14e-7 for the four blocked
  ones, a 4.4% split with 1.15% scatter inside the blocked group. Suggestive,
  not settled — n=2 in the open group, and azimuth is confounded with altitude.
- **Spillover off an uneven horizon** moves the *intercept*. Same data: the
  one pair that swung to a more blocked azimuth gained ~10 K of total power
  with its slope unchanged, the pair that swung open lost ~5 K. About 0.3 K
  per degree of horizon floor, 10–15 K across this site's 5–45° range.

A third caveat, now settled: total power stepped **−7.9%** between
2026-09-16 10:39 and 21:00, the window the pilot's transmitter went live in.
The A-B-A of 2026-09-22 (§3) explained it: enabling the TX chain costs 8.4% of
the receiver's sensitivity whatever the TX gain. A gain belongs to the TX
state it was fitted in, and a gain fitted with the TX on does not apply to a
TX-off recording, or the reverse.

### The tracking scallop

`receiver_scheduler/scallop.py`. Everything above calibrates the *receiver*.
This one calibrates the **mount**, and on a tracked compact source it is the
largest single systematic left in the photometry.

The Due rounds every commanded position to one encoder pulse, 0.5°. `round()`
is the firmware's own arithmetic, so the pointing error swings ±0.25° rather
than running 0 → 0.5 (issue #7), and the beam's gain follows the square of it:
a **scallop**, not a sawtooth. Both axes do it at once, at their own rates —
altitude steps every ~10.7 minutes on the Sun, azimuth every ~1.6 — and
neither is slow enough to be absorbed by a baseline. Measured on the
2026-09-17 solar track, folded on the drive's own quantisation phase:

| axis | period | peak-to-peak | a sawtooth would give |
|---|---|---|---|
| altitude | 10.7 min | 0.82% | 3.32% |
| azimuth | 1.64 min | 0.58% | 2.27% |

The correction is fitted from the observation's own records, not taken from
the beam, and two measured facts are why. On the old feed the amplitude came
out 10–40% above what the 4.57° Gaussian of the time predicted, because the
beam is flat-topped and the scallop only ever probes the middle quarter-degree
of it. And the phase of the dip sits 0.04–0.07° from where the pointing model
puts it, which is the model's own residual at that part of the sky — **a
correction applied at the wrong phase adds modulation rather than removing
it**, so the fit carries a phase offset per axis and searches it. Each axis is
judged separately against `MIN_SIGMA` (4) and against the beam's predicted
curvature, within `AMPLITUDE_RANGE` (0.4–2× the beam); an axis that fails
either is left in rather than carried, because a negative fitted amplitude
applied would amplify that axis instead of flattening it. The beam judged
against is the one in force (`beam_calibration.json`), which outranks the
width in the recording's header. Records beyond `OUTLIER_SIGMA` (5) of the
run's robust scatter — the stow at the end of a run, a record off the source —
are dropped and the fit repeated, judged each time against the original set.

**Two solutions, judged by what is left** (2026-09-24). A least-squares fit
over a whole run has no defence against records that are not the quiet Sun:
ten stow records at −85% among 1200 pulled the fit to 3.5× the beam at a phase
of 0.2°, and a simulated ×6 radio burst 3 min wide fitted 3.75×. So
`scallop.correct` tries both this run's fit and the parameters **carried from
the last quiet run** in `scallop_reference.json` (amplitudes stored relative
to the beam's curvature, so a re-measured beam rescales them), and applies
whichever leaves less modulation folded at the pulse phase on the run's
**quiet** records — those within 5% of a 20-minute running median. Never by
chi-squared, which the free fit wins on its own residual whether or not it
fitted the burst. If neither beats no correction, nothing is applied. A fresh
fit that wins replaces the carried parameters. The carrying is justified by
the archive: twenty Sun tracks (2026-09-08 to 09-24) put the phases at alt
+0.05 ± 0.02° and az −0.04 ± 0.02° under one model, and the amplitudes at
0.6–1.2× the beam. The parameters carried now are from 2026-09-30: alt 0.67×,
az 1.07× the 4.30° beam, phases +0.060/−0.060°. A fit well outside the range —
3.85× on the count-error run of 09-24 — is refused, and is a pointing problem,
not a beam.

Reconstructing where the mount was *commanded* to point needs the pointing
model that was in force. The scheduler now writes it into every recording as
`pointing_terms`; a file made before that (or when the controller could not be
read) falls back to the model this installation last fitted, from
`pointing_model.json`, and the plot says which was used. **It never runs with
no model at all** — without one the reconstructed demand is over a degree out
and varies across the sky, so the quantisation phase is wrong and the fit
returns a *negative* amplitude, which is exactly how the fallback came to be
written: the same track the code fitted at 37σ with the terms in hand reported
"not detected" through the plot path without them.

Two limits on where it is applied. It multiplies the **source**, not the
system temperature — `counts = G·B·(T_sys + g·T_A)` — so it belongs after
T_sys is subtracted and never on the counts, which is also why it is not part
of the receiver's write-time chain. And it is meaningful only for a source
small against the beam: diffuse emission fills the beam however far it is
offset, so `applies_to` says yes for a tracked Sun, Moon or Jupiter and asks
for anything else. A drift scan has no scallop at all, the mount being parked.

On the finished 2026-09-17 solar track — 3261 records over 165 minutes, band
mean over the 397 continuum channels, antenna temperature — it takes the
folded modulation from **0.87% to 0.24%** in altitude and **0.58% to 0.10%**
in azimuth, and the residual rms about an 8th-order trend from **0.562% to
0.458%** of a 1631 K Sun, 9.2 K to 7.5 K. The correction applied spans 1.60%
peak to peak and its mean is 0.57%, so every point also comes up by that much:
the run reads 74.7 SFU where uncorrected it would read 74.3.

Quote those on **antenna temperature**, never on counts. The scallop multiplies
the source alone, so on total power its fractional depth is diluted by
`T_A/(T_sys+T_A)` — 0.82 here — and a residual computed on counts flatters the
correction by that factor. The pipeline gets this right (`plot_observation`
converts to kelvin before `_plot_solar`); it is the diagnostic that is easy to
run on the wrong series, and the first figures quoted for this work were.

It is applied in the reduction, for display only, so recordings stay raw and
re-reduce with a better beam or a better pointing model.
`notebooks/solar_flux_scallop.ipynb` walks the whole reduction from the
recorded file, step by step, and reproduces the plot.

### Kelvin to flux

`A_e = λ²/Ω`, the antenna theorem, with the **measured** main-lobe solid
angle. Since 2026-09-24 the beam is a calibration product like the gain and
the bandpass: `beam_scan.py` measures it from a **two-hour Sun drift** (the
Sun Scan tab's *Start beam drift*, or a drift entry with `beam_scan: true`),
integrating the main lobe directly on each side of the crossing —
2π∫P(θ)θdθ out to the first null, baseline from beyond 6.5°, the two sides a
check on each other — and writes `beam_calibration.json`, which
`instrument.measured_beam()` serves to every consumer: fluxes, the
simulators, the horizon margin, the raster's default width, the receiver's
file attributes. A scan is adopted only if it reaches ≥6° of drift either
side, the Sun passes within 0.6° of the beam centre and stands ≥3× the
baseline, and the two sides agree within 12%. The FWHM kept beside the solid
angle is its Gaussian equivalent, √(Ω/1.133), for the simulators' Gaussian
convolution; a Gaussian *fitted* to the crossing is reported for comparison
only, because on a flat-topped lobe it depends on the window.

In force since 2026-09-25: **20.96 sq deg, FWHM-equivalent 4.30°**, from the
first two-hour drift on the new feed (`20260925_120824_drift.h5`, ±14–15° of
drift, adopted on its own merits; the two sides 22.05 and 19.87 sq deg). First
sidelobes 0.44% of peak leading, 0.84% trailing (−23.6 / −20.8 dB, baseline
subtracted), nulls at 5.27° and 4.89°. The 09-24 hour-long drift before it
gave 20.8 sq deg / 4.29°, adopted by force with its trailing side 0.2° short
of the baseline rule. The drift angle's sign was inverted until 2026-09-25,
so every beam report before that date has `before` and `after` swapped,
the archived 09-15 and 09-24 analyses included. By this method the old feed's
09-15 drift gives 21.6 sq deg, not the 23.7 recorded then, so the earlier
number carried ~9% of method that cannot be reconstructed. `λ²/Ω_main` is an
**upper bound** on the effective area — on the new feed it is the physical
7.07 m² to within 1%, which says only that the main lobe holds most of the
power; the sidelobes and spillover lie outside it, and the true effective
area is smaller by the main-beam efficiency.

Solar work is corrected to above the atmosphere with the same zenith opacity
the drift fits use, and the professional measurement for the same day — the
RSTN 1415 MHz local-noon flux from NOAA SWPC — is quoted beside ours on the
plot. The stations disagree by 10–20 SFU with each other, so agreement to
within that is agreement.

---

## 6. What the recovery is used for

Three products come out of one burst, and a fourth out of the carrier.

### Level and slope — applied to the next records

`pilot.level_and_slope()` fits `|h|²/|h_ref|²` against frequency, weighted by
SNR², and returns the **power gain at band centre** and its **fractional
change per MHz**. Both are power quantities, applicable to counts as they
stand. Rejected if the median SNR is below `detect_snr` (8), if fewer than
three bins are usable, or if the level is outside 0.25–4.

`pilot.factor(level, slope, f, fc)` turns them into the per-channel divisor,
clamped to 0.5–2 so a bad estimate cannot do more than double or halve
anything. It is applied to every science record until the next burst, for at
most `hold_bursts` (3) intervals — after that the estimate is stale and
nothing is applied rather than something wrong.

### The passband correction vector — the ripple

`pilot.correction_vector()` averages the bursts inside `shape_window_s`
(30 min), **weighted by their own lengths**, takes the power ratio to the
reference, removes its straight line (the level and tilt are `factor`'s, and
applying either twice would be a bug), denoises it in the delay domain, then
interpolates onto the product's axis and normalises to unit median.

The **delay filter** is the part that makes it work: the response is a few SAW
multi-transit echoes at 1.5–6.2 µs, so it is sparse in delay, and keeping
only |τ| < `max_delay_us` (10) discards most of the per-bin noise and none of
the ripple. Worth a factor 2.2.

**A correction from too little pilot is worse than none.** One 3 s burst gives
0.18% per channel against a 0.11% ripple, and because the same vector divides
every record, that noise is a *systematic*, not something that averages away.
The threshold is `min_shape_pilot_s` (24 s) in **accumulated pilot seconds**,
not bursts: a burst is one record, records are not a fixed length, and eight
3 s bursts carry what a third of one 60 s burst does. A burst count would have
refused every correction on a 60 s drift scan while accepting a worse one at
3 s. Below the threshold the level and tilt are applied alone; they are good
to 0.02% from a single burst.

Simulated, 30 bursts (90 s, half an hour at 5% duty), against the comb's
strength per bin — which is what the receiver-gain change of 2026-09-17 bought
and why it was worth buying:

| comb per bin | burst, × system noise | residual per channel | on a 360 K system |
|---|---|---|---|
| the ripple itself, uncorrected | — | 0.106% | 0.38 K |
| 5× (the old design point, 30 dB) | ×5.9 | 0.032% | 0.117 K |
| 20× | ×20.6 | 0.016% | 0.058 K |
| **50× (the design point at 20 dB)** | **×52** | **0.0104%** | **0.037 K** |
| 100× | ×99 | 0.0074% | 0.027 K |

against the 0.15 K thermal floor of a gain fit — so at the current design
point the ripple is a quarter of the floor and has stopped being a term. It is
not pushed further because the return is already bending away from 1/√ratio
(4.35× at 100 against 4.47× predicted) for something the science can no longer
see. The recovery itself needs no change to run there: every burst is still
detected and neither the factor nor the correction vector reaches its clip.

Shortening the accumulation instead costs as 1/√N: 24 s, the threshold, leaves
about twice the residual of 90 s. Accumulated pilot in a
half-hour window is 90 s whatever the record length, since the duty cycle sets
it — a long record gives fewer but proportionately more precise bursts.

### The carrier's level — recorded, not applied

At ~1000× the noise in its own bin the carrier needs no coherent recovery:
`pilot.tone_power()` reads it as the excess over the local noise in the
channels of the wide product it already lands in, `tone_sum_bins` either side
summed, noise from the median just outside. Per 60 s record that is good to
6.5×10⁻⁵ — the thermal floor.

**It is measured against the last burst**, so the two never count the same
change twice: at a burst the carrier reads unity by construction, and between
bursts it carries the departure since. A reference older than the burst
level's hold window is stale and stops being used, rather than quietly
becoming a measurement of the slow drift the burst is supposed to carry.

**It is recorded but not applied** (`tone_apply`, off). Applying it corrects a
wobble in the SAWbird or the converter, does nothing for an atmospheric one,
and would substitute the transmit chain's own wobble for the receive chain's.
The bench correlation test of issue #30 decides.
**To apply it retrospectively:** each science record's `pilot_tone_power`
divided by that of the burst record before it is the factor, and
`pilot_tone_ok` says whether the carrier was detected. Nothing else is needed,
which is the point of recording the series whether or not it is used.

### With the transmitter unconnected

The state of every run before 2026-09-22, recorded here because it is also
the proof of no leakage. Nothing is detected,
nothing is applied, unit factors are written, and the file reduces identically
to one made with the pilot off — less the burst records, which are still
flagged and dropped. Verified on the hardware: bursts on exactly every N-th
record, zero detected, spectra in kelvin as before, and no measurable leakage
from the transmit port into the receive path (burst and science records agree
in band power to 0.9%, the sky's own drift). The front end's 42 dB of gain is
what makes internal leakage harmless: it amplifies the wanted path through the
dipole and not the leak.

---

## 7. How it is applied on the way to disk

Per record, in `b210_h1_receiver.append_spectrum`:

```
kelvin = counts / (bandpass_correction
                   · applied_gain_counts_per_k
                   · pilot_factor(level, slope, f)
                   · pilot_correction[index]
                   · pilot_tone_level)            −  applied_t_sys_k
```

The same divisor is built separately for each product, from that product's own
`bandpass_correction` / `bandpass_correction_wide` and correction vector.

Which factors are present depends on the record:

| record | bandpass + gain | pilot level/slope/shape | carrier |
|---|---|---|---|
| science, recent burst detected | yes | yes | only if `tone_apply` |
| science, no burst yet or stale | yes | no (unit factors written) | no |
| burst record, or caught a tail | yes | **no** — written as measured, `pilot_burst = 1` | no |
| tuning the calibration does not cover | **no** — written as `spectra_linear`, raw counts | no | no |

---

## 8. How the calibration appears in a recorded file

Everything in the divisor travels in the file. Nothing is left implicit.

**Fixed vectors, written once before the first record**

| dataset | what |
|---|---|
| `frequency_hz`, `frequency_hz_wide` | the two products' axes |
| `bandpass_correction`, `bandpass_correction_wide` | the stored template evaluated on those axes |
| `bandpass_valid`, `bandpass_valid_wide` | which channels the template covers |
| `pilot_reference` | the reference complex response, per comb bin |
| `pilot_bins_wide` | which wide bins carry comb tones |
| `pilot_anchored` | 1 = referenced to the calibration's stored reference, 0 = to this run's first burst |

**Per record** (all the same length as `timestamps`)

| dataset | what |
|---|---|
| `spectra_kelvin` / `spectra_linear` | the H I product; the **name is the guard** — asking for the wrong one raises rather than silently returning the other scale |
| `spectra_wide_kelvin` / `spectra_wide_linear` | the continuum product |
| `timestamps` | the record's **midpoint**, not its end |
| `integration_times`, `overflows` | length, and UHD's dropped-sample count |
| `underflows` | the pilot transmitter's underflow count this record, beside `overflows` in a file with a wide product; a host stall shows in both, and the carrier level steps at it |
| `pilot_burst` | 1 = this record held a burst or its tail → dropped by `read_observation` |
| `pilot_ok` | 1 = a pilot correction was applied to this record |
| `pilot_level`, `pilot_slope` | what was applied; 1.0 and 0.0 when nothing was |
| `pilot_snr` | median per-bin SNR of the burst behind this record |
| `pilot_correction_index` | which row of `pilot_correction_*` was applied; −1 for none |
| `pilot_tone_power` | the carrier's excess power this record, always recorded |
| `pilot_tone_level` | the carrier factor **applied**; 1.0 when it was not |
| `pilot_tone_ok` | 1 = carrier detected |

The `pilot_*` datasets and the `pilot_*` attributes other than `pilot` itself
exist only in a file recorded with the pilot enabled. With it off (the
default since 2026-09-22) the file carries the `pilot` configuration and
nothing else of it.

**Per burst, and per change of correction**

| dataset | what |
|---|---|
| `pilot_shape`, `pilot_shape_time` | one row per *detected* burst: the recovered complex response and its midpoint |
| `pilot_correction_h1`, `pilot_correction_wide` | one row each time the applied vector changes, indexed by `pilot_correction_index` |

**Attributes**

| attribute | what |
|---|---|
| `applied_gain_counts_per_k`, `applied_t_sys_k` | the gain and `T_sys` used at write time |
| `gain_calibration` | the whole fit as JSON, including its correlation and when it was observed |
| `bandpass_template`, `bandpass_template_wide` | the templates as JSON, so the file is reducible even if the stored one has moved on |
| `spectra_units`, `spectra_wide_units` | `K` or counts — says the same thing the dataset name does |
| `pilot` | the pilot configuration in force, as JSON |
| `pilot_applied`, `pilot_tone_applied` | whether each was applied at write time |
| `pilot_centre_hz`, `pilot_tone_hz`, `pilot_burst_every_records` | what `factor()` needs to be reversed, and the cadence actually used |
| `instrument`, `h1_band_hz`, `continuum_band_hz` | the fixed instrument (issue #27) |
| `clock_source`, `clock_ref_locked` | which reference the sample clock ran from (`internal` or `external`) and whether the B200 reported the external one locked (1, 0, or −1 unknown) |
| `reference_state`, `reference_status`, `reference_locked`, `reference_*` | the Thunderbolt's own account at the start: state (`absent` when no unit is read), disciplining mode, holdover, alarms, oscillator and PPS offsets, DAC volts, temperature, satellites. With the two above, these decide whether the recording is on the locked reference and so whether any clock shift applies (§5) |
| `beam_fwhm_deg`, `effective_area_m2` | the beam in force, for flux |
| `pointing_terms` | the pointing model in force, as JSON — what the scallop needs to reconstruct the drive demand. Written from 2026-09-17; absent on an earlier file, and empty if the controller could not be read |
| `site_lat_deg`, `site_lon_deg`, `site_height_m`, `object_name`, `observation_mode` | enough to recompute where the source was, and whether the mount was tracking it |

**The scallop is not in this list, and that is deliberate.** Everything it
needs — the ephemeris, the pointing model, the beam, the record times — is
either in the file or derivable from it, so the correction is recomputed on
every reduction rather than baked in. A recording therefore carries no trace
of it and needs no undoing: fit a better pointing model, or measure the beam
again, and every old solar track improves. The two pilot corrections are the
opposite case — they are measured from the run's own bursts and can never be
recovered later — which is why those *are* written, and why `keep_pilot`
exists (§10).

A file is opened SWMR once every dataset and attribute exists, which is why
all of the above is written **before** the first record — including the
segment number of a rolled file. Nothing may be created afterwards.

---

## 9. How it appears on the web server

### Configuration tab

**The pilot is off by default since 2026-09-22.** Tested on the sky that
evening, the carrier's received level flips by up to 1.6 % at every host
stall (a TX underflow with an RX overflow) and stays at the new level, and
between stalls it drifts three times as far as the receiver does, so
dividing by it makes a time series worse, not better. The bursts have not
been proven either. Enabling it is a deliberate act on the Configuration
tab, and it changes the receiver's sensitivity by 8.4 % (the TX chain being
on, not the radiated carrier), so the gain in force belongs to one state and
must be re-fitted after a change.

The pilot's switch and five knobs — `receiver_pilot_enabled`,
`_burst_interval_s`, `_max_duty_cycle`, `_burst_amplitude`, `_tx_gain_db`,
`_tone_amplitude`. The live config holds `enabled` false and `tx_gain_db` 70
(set when the dipole was wired). The
last two belong together: raising the transmit gain raises the carrier as well
as the comb, so a change to one is almost always a change to both. Blank means
the default
in `pilot.PILOT_DEFAULTS`. The tab warns before saving a tuning change,
because every recording after one is uncalibrated until the bandpass and gain
are re-measured.

### RF tab — the calibration in force

Three cards, from `/api/rf/status` and `/api/pilot/status`:

- **Bandpass**: polynomial order, band, residual %, how long ago it was
  measured, on which field, and at which LO and sample rate.
- **Gain**: `T_sys` and gain in counts/K, with its age. Turns red if `T_sys`
  hit its 50 K floor — the fit is then against the bound, not a measurement —
  and carries a plain-language judgement of whether `T_sys` is high, and what
  raises it. Plots via `/api/rf/bandpass/plot` and `/api/rf/gain/plot`.
- **Pilot**: the configuration in one sentence, then for the current or last
  recording, *"N of M bursts detected, K of R records corrected"* and either
  *"applied now: level x%, tilt y%/MHz"* or an amber *"nothing applied (last
  burst SNR …) — the transmitter is not connected, or the dipole is not
  radiating into the feed"*. This is the one place the pilot's health is
  visible; check it before claiming the pilot corrected anything.

### Observe tab — the calibration of one recording

- The file list marks each recording **"calibrated (kelvin)"** or
  **"uncalibrated (counts)"**.
- The details table's `units` row gives the applied gain and `T_sys`.
- **The recording plot's subtitle always states what was done**, including
  when nothing was: the bandpass note names the template's order, date and
  source field, or says the spectrum was not corrected and why; and when a
  gain applied, *"calibrated to kelvin: T_sys … K, gain measured … UTC"*,
  marked if it came from the file rather than the current calibration. A
  flat-looking spectrum that has silently been through a template is
  indistinguishable from one that has not, which is why this line is never
  omitted.
- **Fit model** (`/api/observe/fit`) refits gain and `T_sys` on the chosen
  recording and reports it **against the calibration in force as a
  percentage**, with the correlation, the residual and whether the clock shift
  is trustworthy; on a recording on the locked reference the shift is held
  at zero, not fitted (§5). It is a *proposal* until "Apply as calibration"
  is pressed.
  For a drift scan it is a different fit — total power against the simulator's
  predicted drift curve — and comes back `applicable: false`, drawn and
  reported but never applied as the per-channel calibration.
- **The tracking scallop gets a line of its own** on a solar track's plot,
  from `scallop.plot_caption`. Either *"scallop removed (fitted): alt 0.67x;
  az 1.07x the 4.30 deg beam, phase +0.060/-0.060, x->y% [from the
  file]"* — `(carried 2026-09-30)` in place of `(fitted)` when the carried
  parameters won, and the modulation folded at the pulse phase before and
  after — or *"tracking scallop left in: …"* with the reason. It
  names the **pointing model** it used, because a recording made before
  `pointing_terms` was stored is reduced against a later model and that is an
  assumption, not a measurement. The line is never omitted on a run where the
  correction could apply — a de-scalloped trace and an uncorrected one look
  alike, which is exactly why the caption exists.
- **The live view** (`/api/observe/live`) captions the running trace with
  *"pilot: gain x%, tilt y%/MHz applied; N of M bursts seen, K records
  corrected"*, and drops burst records from the trace. It does **not** apply
  the scallop. The 30 s re-plot of the file beside it does, so the two
  disagree by up to 1.6% mid-run. The carried parameters (§5) would let the
  live trace be corrected from its first record; that is not done.

**Known gap:** the static recording plot's pilot line reports only how many
records the pilot reached, not the size of what it applied — the level and
tilt are on the live caption and the RF card. A file's `pilot_*` datasets are
the full record of what happened.

---

## 10. How to undo it

`observation_plot.read_observation(path, product="h1"|"wide")` does all of it:
multiplies back `applied_t_sys_k`, `applied_gain_counts_per_k`, the bandpass
correction, each record's `pilot_level`/`pilot_slope` factor, its correction
vector by index, and its carrier factor where one was applied; then drops the
burst records and reports how many in the header. What comes back is counts.

**Why the round trip is not wasted.** Fitting a gain from spectra a gain has
already been applied to is circular: it would return unity and a system
temperature of zero, while looking like a perfect calibration.

**The pilot is the exception, and `keep_pilot` is why.** Everything else
reversed here can be put back from the stored template — the bandpass and the
gain belong to the instrument, not to the run. The pilot does not: it is
measured from that run's own bursts, and nothing outside the file knows the
answer, so reversing it and not re-applying it discards it for good. A *fit*
must still see raw counts, so the default reverses it and `bandpass.py`,
`drift_fit.py` and `rf_calibration.py` take that default. A *plot* asks for
`keep_pilot=True`, and `plot_observation` does. The header carries
`pilot_kept` so a caption cannot claim a correction that was thrown away, and
the subtitle names the number of records it reached. With the transmitter
unwired, as it was when this was found (2026-09-17), every factor is unity and
the two paths agree exactly, which is why it had to be caught by reading
rather than by looking at a plot. With the pilot off, as it is by default
now, there is nothing to keep.

The **tracking scallop** needs no undoing: it is applied in the reduction and
never written, so it is absent from the file by construction. The same is true
of the LSR velocity axis and the atmospheric-opacity correction on solar flux
— all three are properties of where the dish was pointed rather than of the
receiver, and all three improve when the ephemeris, the pointing model or the
beam does.

---

## 11. What is not calibrated, and what it would take

- **Absolute scale independent of HI4PI.** Everything rests on a model line. A
  noise diode with a known excess noise ratio, or a hot/cold load, would break
  that circle. The pilot does not: it is a transfer standard, not an absolute
  one.
- **Whether the gain's 10–15% spread is stray radiation, spillover or the
  receiver.** §5 has the state of it. The next step is several fields at
  *matched altitude* across open and blocked azimuths for the slope, and a
  horizon strip scan at alt 45–60 for the intercept.
- **Whether the pilot can be used at all.** The TX chain's 8.4% sensitivity
  cost is measured (§3); the carrier's steps at host stalls and its drift
  between them are not understood, and the bursts are unproven. Issue #46.
- **Main-beam efficiency**, which turns the effective-area upper bound into a
  number. A clean Moon drift is the best route: a known disc temperature at a
  known size.
- **The fast wobble's origin** — receiver, transmitter or atmosphere — which
  decides whether the carrier is applied.
- **What is left under the tracking scallop.** Taking it out leaves 0.27% rms
  on the Sun where the radiometer equation and `GAIN_INSTABILITY` together
  predict about 0.08% per record. So something of the same order as the
  scallop is still there, and the obvious candidate is the rest of the
  pointing residual: the correction assumes the only error is the
  quantisation, while the model itself is good to a few hundredths of a degree
  and drifts with the structure's temperature. A second scallop fit on a
  *second* tracked source the same day would separate a mount effect from a
  receiver one.
- **The pilot's own absolute scale.** The gain job does not yet store a
  `pilot_reference`, so every run is referenced to its own first burst
  (`pilot_anchored = 0`) and the pilot removes drift *within* a run, not the
  offset from the calibration. When that reference is stored it must carry the
  tuning it was taken at — the receiver refuses one whose gain, LO or rate
  differ, because the level is a power ratio and a 10 dB gain override would
  read as a factor of ten and have every burst refused.

---

## 12. Operating rules

- Anchor the gain on the same field when comparing across days; the
  fit-to-fit spread is larger than any real change you are likely to be
  looking for.
- Re-measure the bandpass and the gain after any change to the tuning. The
  Configuration tab warns before saving one, and every recording after it is
  in counts until both are refitted.
- The receiver gain is **20 dB** since 2026-09-17 (40 until 09-15, 30 until
  09-17). At 40 dB the receiver compresses the Sun's peak by about 5.7%; 30
  and 20 agree to 0.66%. The compression is downstream of this setting, so
  every 10 dB off multiplies the power the chain will take by ten — which is
  what pays for the comb. It costs ~4 K of `T_sys` and nothing in
  quantisation.
- Fluxes are always computed on the beam in force (`beam_calibration.json`),
  whatever a recording's header says. A recording made before 2026-09-15
  re-reduced 22% lower on the solid angle adopted that day.
- A gain belongs to one TX state: enabling the TX chain costs 8.4% of receiver
  sensitivity (§3). **The pilot is never toggled per observation**; the gain in
  force was fitted with it off, and turning it on means refitting the gain.
- A gain or beam measured before 2026-09-22 belongs to the old feed.
- Replacing the bandpass template stales the gain; refit the gain after it.
  Experiments with `bandpass.fit_from_observation` pass `save=False`.
- On the locked Thunderbolt reference no velocity shift is fitted or applied.
  A recording on the TCXO gets a carried shift only from an unlocked
  calibration whose correlation is ≥0.99.
- **The scallop correction is only as good as the pointing model.** It removes
  the drive's 0.5° quantisation and nothing else, so a stale model leaves its
  own residual behind — and if the model is wrong enough the fit loses the
  phase and refuses, which the caption says. Recordings made from 2026-09-17
  carry `pointing_terms`; anything earlier is reduced against whatever
  `pointing_model.json` holds now, so re-check the caption before quoting a
  number off an old file.
- **Photometry of a tracked compact source needs short records.** The scallop
  fit needs at least 50 records (2.5 min at 3 s); a run with fewer, or one
  whose fit is refused, can still take the carried parameters if they leave
  less modulation than none. It also does not apply to a drift scan, where the mount is
  parked and never crosses a pulse boundary, nor to a field observed for its
  diffuse emission, where the beam stays full however far it is offset.

---

## Appendix: designs that were tried and are worse

Recorded so nobody rebuilds them.

**Gating a block by its power** against a median of recent blocks works, but it
is second-hand. It **deadlocks** the first time the received power steps up and
stays up — a slew onto the Sun, or the carrier starting a moment after the
baseline formed — because every block then reads "on", the baseline never
updates again, and every science record is flagged as contaminated and
dropped. Invisible with the transmitter unwired; found by review 2026-09-16.
Its margin also had to beat the per-block scatter while still catching a comb
that raises total power by only 30% with the Sun in the beam — true of the 5×
comb of the time, and the reason the margin had nowhere to sit.

**Trusting the command** is simpler still, and silent: the transmit buffers
empty tens of milliseconds after the comb is switched off, and a record that
caught that tail would be reduced as sky — at the 5× comb of the time that was
a 7 K error over 1% of a 3 s record, and the comb is ten times stronger now.

**A whole-band coherent statistic** cannot work at all: the fixed
transmit/receive framing offset puts a phase ramp across the band that cancels
it. The ramp is instead found per burst and removed.

**A continuous weak pilot** instead of bursts bought only cadence, and could
never enter the H I band — a comb strong enough to measure the 0.1–0.15% SAW
ripple is a 7 K picket fence on the fine channels.

**Rewriting a running vector source's samples** to switch the comb, rather than
a multiplier on its own source, interrupts the carrier — whose whole value is
that it is uninterrupted, so its power during a burst is the right reference
for the records after it.

**`moving_average_ff`** for the products' smoothing profiled at a full core per
branch and overflowed at 8 Msps where the old single graph did not.
`integrate_ff` plus an accumulating sink sums every spectrum exactly and left
the hottest thread at 30%.

**A burst count** rather than accumulated pilot seconds as the shape threshold
refused every correction on a 60 s drift scan while accepting a worse one at
3 s.

**A flat `burst_off_margin_s`** of 0.3 s switched the comb off before it was
ever on at 0.1 s records, and dropped the record for a burst that never
happened. It is capped at a quarter of the record.
