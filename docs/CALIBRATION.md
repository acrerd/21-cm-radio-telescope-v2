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
counts(f, t)  =  G(t) · B(f, t) · [ T_sys(t) + T_A(f, t) ]
```

| term | is | measured |
|---|---|---|
| `T_A` | the sky — the thing we want | — |
| `T_sys` | everything the instrument and its surroundings add: the SAWbird's 59 K, spillover onto the ground, the atmosphere, the cable | 340–372 K |
| `B(f, t)` | the **shape** of the response across the band, normalised to unit median: the SAW filter's passband, and that filter's multi-transit echo ripple | — |
| `G(t)` | the **level**, counts per kelvin | ~8–9×10⁻⁷ |

Four calibrations measure these, on four different timescales, and a fifth
converts kelvin to flux.

| what | measures | good for | where |
|---|---|---|---|
| bandpass template | `B(f)` | days, and it goes stale | `bandpass.py` |
| gain and system temperature | `G`, `T_sys` | hours | `rf_calibration.py` |
| pilot bursts | changes in `G` and `B(f)` | one burst per 60 s of run, duty permitting | `pilot.py` |
| pilot carrier | fast common-mode `G` | every record | `pilot.py` |
| beam solid angle | kelvin → flux | once, per feed | `astro_simulator/instrument.py` |

The first two are **measured beforehand, on separate jobs, and stored on
disk**. The two pilot calibrations are **measured inside the observation
itself**. The beam is measured once and rarely changes.

---

## 2. The whole chain in one pass

What happens to one observation, in order. Each step names the section with
the detail.

**Before the run**

1. A **bandpass job** tracks the Lockman Hole, records a few minutes, fits a
   polynomial to the mean spectrum with the line masked, and stores
   `bandpass_template.json` and `bandpass_template_wide.json`. (§5)
2. A **gain job** tracks a plane field, records, and fits `counts = G·(T_sys +
   T_model)` against the simulator's sky. Stores `gain_calibration.json`. (§5)

**At start-up of the observation**

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

**Afterwards**

11. `read_observation` multiplies all of it back, drops the burst records, and
    hands back counts. (§8)
12. Plots and fits work from those counts, and say on their face what was and
    was not applied. (§9)

---

## 3. What is injected, and when

The pilot is the receiver's own reference: the B210's transmitter, through
fixed pads and the old SRT calibration dipole at the dish's vertex, radiating
into the feed so that **everything downstream of the feed is inside the
measurement** — the probe, the horn, the SAWbird, the cable down the mount,
the converter. Detail in `receiver_scheduler/pilot.py`; the design record is
issue #30.

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
(5), since it otherwise costs one record an interval for nothing — which is
the state until the vertex dipole is wired. The carrier keeps running, costing
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
the template after a receiver-gain change anyway; nothing stops you, and the
09-16 30 dB template will otherwise be applied silently to 20 dB recordings.

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

**Anchored on the H I line.** The line is the only part of the sky whose
brightness is known independently, from HI4PI through the measured beam. The
velocity shift of the B210's clock is fitted alongside, and **carried between
observations only from a fit whose correlation says a line held it** (≥0.99):
a shift with no line to hold it slides onto whatever is nearby and reports a
confident number. Constrained fits give −2.4 to −3.0 ppm over three weeks,
inside the part's ±2 ppm specification.

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
spanning the same 8.14–9.38e-7 across time on its own. Treat the kelvin scale
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

A third caveat sits under both: total power stepped **−7.9%** between
2026-09-16 10:39 and 21:00, the window the pilot's transmitter went live in,
and has been stable since. The bandpass template (<1%) and the master clock
rate are ruled out. Until the pilot-on/pilot-off test settles it, do not
compare a gain fitted before that date with one fitted after.

### Kelvin to flux

`A_e = λ²/Ω`, the antenna theorem, with the **measured** main-lobe solid angle
— 23.7 square degrees, a Gaussian-equivalent 4.57°, integrated directly from
three Sun drifts at 40, 30 and 20 dB on 2026-09-15. That gives 6.18 m² against
a physical 7.07, and it is an **upper bound**: the sidelobes and the ~10%
spillover lie outside the integrated lobe, so the true effective area is
smaller by the main-beam efficiency. On this scale the Sun reads 79 SFU
against the reference network's 75, Cas A 1.15 times its model and the Moon
1.20 times a 225 K disc.

Solar work is corrected to above the atmosphere with the same zenith opacity
the drift fits use, and the professional measurement for the same day — the
RSTN 1415 MHz local-noon flux from NOAA SWPC — is quoted beside ours on the
plot. The stations disagree by 10–20 SFU with each other, so agreement to
within that is agreement.

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
the beam, and two measured facts are why. The amplitude comes out 10–40% above
what a 4.57° Gaussian predicts, because the beam is flat-topped and the
scallop only ever probes the middle quarter-degree of it. And the phase of the
dip sits 0.04–0.07° from where the pointing model puts it, which is the
model's own residual at that part of the sky — **a correction applied at the
wrong phase adds modulation rather than removing it**, so the fit carries a
phase offset per axis and searches it. Each axis is judged separately against
`MIN_SIGMA` (4) and against the beam's predicted curvature; an axis that fails
either is left in rather than carried, because a negative fitted amplitude
applied would amplify that axis instead of flattening it.

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
`receiver_scheduler/solar_flux_scallop.ipynb` walks the whole reduction from
the recorded file, step by step, and reproduces the plot.

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

Which is the state until the vertex dipole is wired. Nothing is detected,
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
| `pilot_burst` | 1 = this record held a burst or its tail → dropped by `read_observation` |
| `pilot_ok` | 1 = a pilot correction was applied to this record |
| `pilot_level`, `pilot_slope` | what was applied; 1.0 and 0.0 when nothing was |
| `pilot_snr` | median per-bin SNR of the burst behind this record |
| `pilot_correction_index` | which row of `pilot_correction_*` was applied; −1 for none |
| `pilot_tone_power` | the carrier's excess power this record, always recorded |
| `pilot_tone_level` | the carrier factor **applied**; 1.0 when it was not |
| `pilot_tone_ok` | 1 = carrier detected |

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
| `beam_fwhm_deg`, `effective_area_m2` | the beam in force, for flux |

A file is opened SWMR once every dataset and attribute exists, which is why
all of the above is written **before** the first record — including the
segment number of a rolled file. Nothing may be created afterwards.

---

## 9. How it appears on the web server

### Configuration tab

The pilot's five knobs — `receiver_pilot_enabled`, `_burst_interval_s`,
`_max_duty_cycle`, `_burst_amplitude`, `_tx_gain_db`, `_tone_amplitude`. The
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
  is trustworthy. It is a *proposal* until "Apply as calibration" is pressed.
  For a drift scan it is a different fit — total power against the simulator's
  predicted drift curve — and comes back `applicable: false`, drawn and
  reported but never applied as the per-channel calibration.
- **The live view** (`/api/observe/live`) captions the running trace with
  *"pilot: gain x%, tilt y%/MHz applied; N of M bursts seen, K records
  corrected"*, and drops burst records from the trace.

**Known gap:** the static recording plot says nothing about the pilot — only
the live caption and the RF card do. A file's `pilot_*` datasets are the
record of what happened; the plot is not.

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
the subtitle names the number of records it reached. Until the vertex dipole
is wired every factor is unity and the two paths agree exactly, which is
precisely why this had to be caught by reading rather than by looking at a
plot.

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
- **Whether the pilot's transmitter changed the receiver's gain.** One field,
  pilot on then pilot off, back to back; an hour-long fixed-field run would
  also say whether the drift that appeared with it settles.
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
- A recording made before 2026-09-15 carries the old beam, and its fluxes
  re-reduce 22% lower on the corrected solid angle.
- A gain fitted before 2026-09-16 evening is not comparable with one fitted
  after, until the pilot's total-power step is explained.

---

## Appendix: designs that were tried and are worse

Recorded so nobody rebuilds them.

**Gating a block by its power** against a median of recent blocks works, but it
is second-hand. It **deadlocks** the first time the received power steps up and
stays up — a slew onto the Sun, or the carrier starting a moment after the
baseline formed — because every block then reads "on", the baseline never
updates again, and every science record is flagged as contaminated and
dropped. Invisible until the transmitter is wired; found by review 2026-09-16.
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
