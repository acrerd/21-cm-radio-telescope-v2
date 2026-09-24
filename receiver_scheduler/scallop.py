#!/usr/bin/env python3
"""The tracking scallop, and taking it out of a tracked compact source.

The drive quantises every commanded position to one encoder pulse, 0.5
degrees, so while the mount tracks, the beam sits up to a quarter of a degree
off the source and walks back and forth across it at the rate the demanded
position crosses pulse boundaries. `round()` is what the firmware does, so the
error swings +-0.25 deg rather than running 0 to 0.5 (issue #7), and the gain
follows the square of it: a **scallop**, not a sawtooth.

Measured on the 2026-09-17 solar track, folding two hours of records on the
drive's own quantisation phase:

| axis | period | peak-to-peak | a sawtooth would give |
|---|---|---|---|
| altitude | 10.7 min | 0.82% | 3.32% |
| azimuth | 1.64 min | 0.58% | 2.27% |

which is 13 K and 10 K on a 1636 K Sun, and about 0.30% rms in quadrature
against that run's total residual of 0.41%. So on a tracked compact source it
is the largest single systematic in the photometry, and it is entirely
predictable: the demanded position comes from the ephemeris and the pointing
model, and the quantisation is arithmetic.

**It applies to a compact source and to nothing else.** The loss is the beam
moving off a source smaller than itself. Diffuse emission fills the beam, so
offsetting it changes nothing, and "correcting" an H I field for the scallop
would inject a spurious modulation. `applies_to` therefore says yes only for a
tracked observation of a named object; anything else has to ask.

**It multiplies the source, not the system temperature.** counts = G B (T_sys
+ g T_A), so the correction belongs after T_sys is subtracted and never on the
counts themselves - which is also why this is not part of the receiver's
write-time chain. Everything it needs is derivable after the fact, so it stays
in the reduction, where a better beam or a better pointing model can be
applied to an old recording.

**The amplitude and phase are fitted from the observation's own data**, not
taken from the beam. Two reasons, both measured on 2026-09-17: the fold's
amplitude is 10-20% above what a 4.57 deg Gaussian predicts, because the beam
is flat-topped and the scallop only probes the middle quarter-degree of it;
and the phase of the dip sits 0.067 deg (altitude) and 0.044 deg (azimuth)
away from where the pointing model puts it, which is the model's own residual
at that part of the sky. A correction applied at the wrong phase *adds*
modulation. So the fit carries a phase offset per axis, and if it does not
find the scallop at better than `MIN_SIGMA` nothing is applied and the report
says so.
"""
import json
import math
import os

import numpy as np

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Only ever applied automatically to a tracked source small against the beam.
COMPACT_OBJECTS = ("sun", "moon", "jupiter")

# The last quiet fit, carried between runs the way the gain and T_sys are.
# Twenty Sun tracks (2026-09-08..24) put the phases at alt +0.05 +-0.02 deg
# and az -0.04 +-0.02 deg under one pointing model - the model's residual at
# that part of the sky, stable to the fit's own grid step - and the amplitude
# at 0.6-1.2x the beam's curvature. So a carried correction takes out the
# scallop on a quiet day about as well as a fresh fit, and it is the only one
# available on a day with a radio burst, when the fit is either refused or
# wrecked (a x6 burst 3 min wide fitted 3.75x the beam at a phase of 0.2 deg).
# `correct` solves both ways and keeps whichever leaves less modulation.
REFERENCE_FILE = os.path.join(_SCRIPT_DIR, "scallop_reference.json")
# The burst mask: a record this far above (or below) a running median of the
# series is not the quiet Sun and does not judge the candidates. The window is
# about twice the longest scallop period, so the median follows a burst's
# envelope but not the scallop.
QUIET_WINDOW_S = 1200.0
ACTIVE_FRACTION = 0.05
FOLD_BINS = 8

# The fit has to see the scallop this strongly before anything is divided out.
MIN_SIGMA = 4.0
# ... and its amplitude has to be within this factor of what the measured beam
# predicts, or something other than the scallop has been fitted.
AMPLITUDE_RANGE = (0.4, 2.0)   # the archive spans 0.6-1.2; a 0.3 deg count error fitted 3.85x
# Records further than this from the fit, in units of the run's own robust
# scatter, are dropped and the fit repeated once: the stow at the end of a
# run, a record with the beam off the source. See `fit`.
OUTLIER_SIGMA = 5.0
# Phase offsets searched, in degrees of drive position: a whole pulse, since
# the phase is periodic in one.
PHASE_STEPS = 21
PULSE_DEG = 0.5


def applies_to(header):
    """(ok, reason) - whether a scallop correction is meaningful for this file.

    Yes for a tracked named object; no for a drift scan (the mount is parked,
    so there is no quantisation walk at all) and no for a field observed for
    its diffuse emission (the beam is full whatever the offset).
    """
    if not header:
        return False, "no header"
    if str(header.get("observation_mode", "")).lower() != "track":
        return False, "not a tracked observation: a parked mount has no scallop"
    name = str(header.get("object_name", "")).strip().lower()
    if str(header.get("coord_system", "")).lower() == "object" and name in COMPACT_OBJECTS:
        return True, ""
    if name:
        return False, "%r is not a source known to be compact against the beam" % name
    return False, ("a tracked field, which may be diffuse: the beam stays full "
                   "however far it is offset, so there is no scallop to remove")


def stored_model_terms():
    """The terms from the pointing model this installation last fitted.

    `pointing_model.json` beside the code, which is what the scheduler fitted
    and pushed to the controller. Used as the fallback for a recording that
    does not carry its own, since the reduction must not depend on the
    controller being reachable.
    """
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(here, "pointing_model.json")) as fh:
            return dict(json.load(fh).get("terms") or {})
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def pointing_terms(header, fallback=None):
    """(terms, source) - the pointing model to reconstruct the drive demand with.

    The recording's own `pointing_terms` first; then an explicit `fallback`;
    then the model this installation last fitted. **Never nothing**: without
    the model the demand is wrong by more than a degree and varies across the
    sky, so the quantisation phase is wrong and the fit finds a negative
    amplitude rather than the scallop - which is exactly what happened the
    first time this ran through the plot path, on 2026-09-17.
    """
    raw = header.get("pointing_terms") if header else None
    if raw:
        try:
            terms = json.loads(raw) if isinstance(raw, str) else dict(raw)
            if terms:
                return terms, "the recording's own"
        except (ValueError, TypeError):
            pass
    if fallback:
        return dict(fallback), "the controller's current"
    stored = stored_model_terms()
    if stored:
        return stored, "the last model this installation fitted"
    return {}, "none - refraction only"


def target_altaz(header, stamps, site):
    """True alt/az of the tracked source at each record time."""
    import ephem
    from datetime import datetime, timezone

    name = str(header.get("object_name", "")).strip().lower()
    bodies = {"sun": ephem.Sun, "moon": ephem.Moon, "jupiter": ephem.Jupiter}
    if name not in bodies:
        raise ValueError("scallop: no ephemeris for %r" % name)
    body = bodies[name]()
    alt = np.empty(len(stamps))
    az = np.empty(len(stamps))
    for i, ts in enumerate(np.asarray(stamps, float)):
        site.date = datetime.fromtimestamp(float(ts), tz=timezone.utc).replace(tzinfo=None)
        body.compute(site)
        alt[i] = math.degrees(body.alt)
        az[i] = math.degrees(body.az)
    return alt, az


def observer(header):
    """An ephem observer at the site the recording names, refraction off - the
    pointing transform applies refraction itself, and applying it twice is the
    mistake `sun_scan.refraction_deg` exists to avoid."""
    import ephem

    site = ephem.Observer()
    site.lat = str(float(header.get("site_lat_deg", 0.0)))
    site.lon = str(float(header.get("site_lon_deg", 0.0)))
    site.elevation = float(header.get("site_height_m", 0.0))
    site.pressure = 0
    return site


def drive_demand(header, stamps, terms):
    """(demanded drive alt, demanded drive az, true alt) per record.

    The demand is what the controller asks the Due for before the Due rounds
    it: `trueToDrive` of the source's true position, reproduced by
    `drift_park.true_to_drive` - the same copy of the firmware transform that
    the drift-scan parking is validated against.
    """
    import drift_park

    site = observer(header)
    t_alt, t_az = target_altaz(header, stamps, site)
    d_alt = np.empty(len(stamps))
    d_az = np.empty(len(stamps))
    for i in range(len(stamps)):
        d_alt[i], d_az[i] = drift_park.true_to_drive(float(t_alt[i]), float(t_az[i]), terms)
    return d_alt, d_az, t_alt


def sky_offsets(d_alt, d_az, true_alt, phase_alt=0.0, phase_az=0.0):
    """Where the beam sits relative to the source, per record, in degrees on the sky.

    `round()` leaves `commanded - demanded`, zero at a pulse boundary and
    +-a quarter pulse midway between. The phase offsets absorb any constant
    difference between the demand computed here and the controller's own - the
    model's residual at this part of the sky, mostly - and are fitted rather
    than assumed.

    Azimuth is converted to the sky by cos(alt): half a degree of drive
    azimuth is less than half a degree of arc anywhere but the horizon.
    """
    def residual(d, phase):
        x = (np.asarray(d, float) + phase) / PULSE_DEG
        return (np.round(x) * PULSE_DEG) - (np.asarray(d, float) + phase)

    e_alt = residual(d_alt, phase_alt)
    e_az = residual(d_az, phase_az) * np.cos(np.radians(np.asarray(true_alt, float)))
    return e_alt, e_az


def beam_fwhm(header=None, override=None):
    """The beam the scallop is judged against: an explicit width, else the
    beam **in force** (`instrument.measured_beam`, the calibration product),
    else what the recording's header says, else the reference figure. The
    measured beam outranks the header because the header holds what the
    receiver was told at write time, and a reduction should improve with a
    better beam - the recordings of 2026-09-24 before 13:36 UTC say 4.57 deg
    for a feed measured that afternoon at 4.29."""
    if override:
        return float(override)
    try:
        import observatory  # noqa: F401  (puts astro_simulator on the path)
        import instrument
        measured = instrument.measured_beam()
        if measured and measured.get("fwhm_deg"):
            return float(measured["fwhm_deg"])
    except Exception:                                     # noqa: BLE001
        pass
    if header and header.get("beam_fwhm_deg"):
        return float(header["beam_fwhm_deg"])
    return 4.57


def _trend_basis(t, degree):
    """Legendre-ish polynomial columns for the slow trend, on a scaled axis so
    the normal equations stay conditioned over a five-hour run."""
    x = np.asarray(t, float)
    x = 2 * (x - x.min()) / max(x.max() - x.min(), 1e-9) - 1.0
    return np.vstack([x ** k for k in range(degree + 1)]).T


def fit(values, stamps, header, terms, beam_fwhm_deg=None, trend_degree=None):
    """Fit the scallop in a per-record series, and say what it found.

    `values` is antenna temperature (or anything proportional to the source's
    own brightness - flux, band power above T_sys); the slow trend is fitted
    alongside, so airmass and real source variation do not have to be removed
    first.

    Model: value = trend(t) x (1 - a_alt e_alt^2 - a_az e_az^2), linear in the
    amplitudes once the phases are fixed, so the phases are found by a grid
    search over one pulse in each axis and the rest by least squares.
    """
    values = np.asarray(values, float)
    stamps = np.asarray(stamps, float)
    good = np.isfinite(values)
    if good.sum() < 50:
        return {"ok": False, "why": "too few records to fit a scallop"}
    d_alt, d_az, t_alt = drive_demand(header, stamps, terms)
    span_h = (stamps.max() - stamps.min()) / 3600.0
    if trend_degree is None:
        # About one free term per half hour, so the trend cannot reach the
        # scallop's periods (10.7 min in altitude at the worst).
        trend_degree = int(np.clip(round(2 * span_h), 2, 10))
    T = _trend_basis(stamps, trend_degree)
    fwhm = beam_fwhm(header, beam_fwhm_deg)
    k_beam = 4 * math.log(2) / fwhm ** 2               # loss per deg^2, the measured beam

    phases = np.linspace(-PULSE_DEG / 2, PULSE_DEG / 2, PHASE_STEPS, endpoint=False)

    def search(mask):
        best = None
        for p_alt in phases:
            for p_az in phases:
                e_alt, e_az = sky_offsets(d_alt, d_az, t_alt, p_alt, p_az)
                A = np.column_stack([T, -T[:, 0] * e_alt ** 2, -T[:, 0] * e_az ** 2])
                coef, *_ = np.linalg.lstsq(A[mask], values[mask], rcond=None)
                resid = values[mask] - A[mask] @ coef
                chi = float(resid @ resid)
                if best is None or chi < best[0]:
                    best = (chi, p_alt, p_az, coef, A, resid.std())
        return best

    best = search(good)
    # One robust pass. A run's last records are often not the Sun at all -
    # the stow slewing the beam off it before the receiver stopped - and on
    # 2026-09-24 ten such records at -85% among 1200 pulled the whole fit to
    # an azimuth amplitude 3.5x the beam's at a phase of 0.2 deg, reported as
    # "scallop removed" over a plot that plainly still had it. Least squares
    # has no defence against that; a residual cut against the run's own
    # scatter does, at no cost to a clean run (nothing is beyond 5 sigma of
    # a 0.5% scatter but a fault).
    # Iterated, and always judged against the *original* set: the first fit
    # bends its trend towards the outliers and takes their neighbours down
    # with them, and those come back once the fit no longer has to reach.
    base = good.copy()
    rejected = 0
    for _ in range(3):
        chi, p_alt, p_az, coef, A, rms = best
        resid = np.full(len(values), np.nan)
        resid[base] = values[base] - A[base] @ coef
        mad = np.nanmedian(np.abs(resid[base] - np.nanmedian(resid[base])))
        sigma = 1.4826 * mad if mad > 0 else rms
        keep = base & (np.abs(resid) <= OUTLIER_SIGMA * sigma)
        if np.array_equal(keep, good) or keep.sum() < 50:
            break
        good = keep
        rejected = int(base.sum() - good.sum())
        best = search(good)
    chi, p_alt, p_az, coef, A, rms = best
    scale = coef[0] if abs(coef[0]) > 1e-12 else 1.0
    a_alt, a_az = coef[-2] / scale, coef[-1] / scale     # fractional loss per deg^2
    cov = (rms ** 2) * np.linalg.inv(A[good].T @ A[good])
    s_alt, s_az = math.sqrt(abs(cov[-2, -2])) / abs(scale), math.sqrt(abs(cov[-1, -1])) / abs(scale)
    out = {"ok": False, "phase_alt_deg": float(p_alt), "phase_az_deg": float(p_az),
           "k_alt": float(a_alt), "k_az": float(a_az),
           "k_alt_sigma": float(s_alt), "k_az_sigma": float(s_az),
           "k_beam": float(k_beam), "beam_fwhm_deg": fwhm,
           "sigma_alt": float(a_alt / s_alt) if s_alt else 0.0,
           "sigma_az": float(a_az / s_az) if s_az else 0.0,
           "residual_rms": float(rms), "trend_degree": int(trend_degree),
           "records": int(good.sum()), "records_rejected": rejected}
    # Per axis, because one can be measured well and the other not, and a
    # negative amplitude applied would *amplify* that axis's modulation
    # rather than remove it. An axis that fails is set to zero, not dropped.
    lo, hi = AMPLITUDE_RANGE
    used, refused = [], []
    for axis, a, sig in (("alt", a_alt, out["sigma_alt"]), ("az", a_az, out["sigma_az"])):
        if sig < MIN_SIGMA:
            refused.append("%s not detected (%.1f sigma)" % (axis, sig))
            out["k_" + axis] = 0.0
        elif not (lo * k_beam <= a <= hi * k_beam):
            refused.append("%s amplitude %.2fx the beam's, outside %.2f-%.2f"
                           % (axis, a / k_beam, lo, hi))
            out["k_" + axis] = 0.0
        else:
            used.append("%s %.2fx the %.2f deg beam at phase %+.3f deg"
                        % (axis, a / k_beam, fwhm, p_alt if axis == "alt" else p_az))
    out["axes_used"] = len(used)
    if used:
        out["ok"] = True
        out["why"] = "fitted from %d records: %s" % (out["records"], "; ".join(used))
        if rejected:
            out["why"] += " (%d records off the source left out)" % rejected
        if refused:
            out["why"] += " (left in: %s)" % "; ".join(refused)
    else:
        out["why"] = "nothing applied: %s" % "; ".join(refused)
    return out


def gain(fit_result, stamps, header, terms):
    """The per-record beam gain on the source, from a successful fit.

    Divide an antenna temperature (or a flux) by this to get what the source
    would have given with the beam centred. Ones where the fit was refused, so
    a caller that does not check `ok` still gets the uncorrected answer rather
    than a wrong one.
    """
    stamps = np.asarray(stamps, float)
    if not fit_result.get("ok"):
        return np.ones(len(stamps))
    d_alt, d_az, t_alt = drive_demand(header, stamps, terms)
    e_alt, e_az = sky_offsets(d_alt, d_az, t_alt,
                              fit_result["phase_alt_deg"], fit_result["phase_az_deg"])
    g = 1.0 - fit_result["k_alt"] * e_alt ** 2 - fit_result["k_az"] * e_az ** 2
    # A quarter-pulse offset costs under a percent; anything outside this is a
    # runaway fit, not a beam.
    return np.clip(g, 0.9, 1.0)


def load_reference(path=None):
    """The carried scallop parameters, or None. Amplitudes are stored relative
    to the beam's curvature so a re-measured beam rescales them."""
    path = path or REFERENCE_FILE
    try:
        with open(path) as fh:
            ref = json.load(fh)
        for key in ("phase_alt_deg", "phase_az_deg", "k_alt_over_beam", "k_az_over_beam"):
            ref[key] = float(ref[key])
        return ref
    except (OSError, ValueError, TypeError, KeyError):
        return None


def save_reference(fit_result, source_file="", path=None):
    """Keep a successful fit as the carried parameters for the next run."""
    from datetime import datetime, timezone

    k_beam = float(fit_result.get("k_beam") or 1.0)
    ref = {"phase_alt_deg": float(fit_result["phase_alt_deg"]),
           "phase_az_deg": float(fit_result["phase_az_deg"]),
           "k_alt_over_beam": float(fit_result["k_alt"]) / k_beam,
           "k_az_over_beam": float(fit_result["k_az"]) / k_beam,
           "beam_fwhm_deg": float(fit_result.get("beam_fwhm_deg", 0.0)),
           "records": int(fit_result.get("records", 0)),
           "fitted_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "source_file": os.path.basename(str(source_file or ""))}
    path = path or REFERENCE_FILE
    with open(path + ".tmp", "w") as fh:
        json.dump(ref, fh, indent=2)
    os.replace(path + ".tmp", path)
    return ref


def reference_candidate(ref, fwhm):
    """The carried parameters as a fit result at the beam in force. An axis the
    stored fit had zeroed (refused) stays zero, as it was applied then."""
    k_beam = 4 * math.log(2) / float(fwhm) ** 2
    return {"ok": True, "phase_alt_deg": ref["phase_alt_deg"], "phase_az_deg": ref["phase_az_deg"],
            "k_alt": ref["k_alt_over_beam"] * k_beam, "k_az": ref["k_az_over_beam"] * k_beam,
            "k_beam": k_beam, "beam_fwhm_deg": float(fwhm),
            "reference_utc": ref.get("fitted_utc", ""), "reference_file": ref.get("source_file", "")}


def _running_median(values, stamps, window_s=QUIET_WINDOW_S):
    from scipy.ndimage import median_filter

    stamps = np.asarray(stamps, float)
    step = float(np.median(np.diff(stamps))) if len(stamps) > 1 else 1.0
    n = int(max(3, round(window_s / max(step, 1e-6))))
    n = min(n | 1, len(values) | 1)
    return median_filter(np.asarray(values, float), size=n, mode="nearest")


def quiet_mask(values, stamps):
    """Records that are the quiet source: within ACTIVE_FRACTION of the running
    median. A burst, a stow, a record with the beam off the source all fail."""
    values = np.asarray(values, float)
    base = _running_median(values, stamps)
    with np.errstate(divide="ignore", invalid="ignore"):
        dev = np.abs(values / base - 1.0)
    return np.isfinite(dev) & (dev <= ACTIVE_FRACTION)


def residual_modulation(values, stamps, header, terms, candidate=None, quiet=None):
    """How much scallop is left, in percent, after dividing by a candidate's
    gain (none for the uncorrected series): the series is detrended by its
    running median and folded on each axis's pulse phase over the quiet
    records, and the two folds' peak-to-peak spans are added in quadrature.
    This is the yardstick the candidates are judged by - not chi-squared,
    which the free fit always wins on its own residual, wrecked or not."""
    values = np.asarray(values, float)
    stamps = np.asarray(stamps, float)
    if candidate is not None:
        values = values / gain(candidate, stamps, header, terms)
    if quiet is None:
        quiet = quiet_mask(values, stamps)
    base = _running_median(values, stamps)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = values / base
    d_alt, d_az, t_alt = drive_demand(header, stamps, terms)
    e_alt, e_az = sky_offsets(d_alt, d_az, t_alt)
    total = 0.0
    for e in (e_alt, e_az):
        edges = np.linspace(-PULSE_DEG / 2, PULSE_DEG / 2, FOLD_BINS + 1)
        idx = np.clip(np.digitize(e, edges) - 1, 0, FOLD_BINS - 1)
        means = [np.nanmean(ratio[quiet & (idx == i)]) if np.any(quiet & (idx == i)) else np.nan
                 for i in range(FOLD_BINS)]
        means = np.asarray(means, float)
        if np.isfinite(means).sum() >= 2:
            total += float(np.nanmax(means) - np.nanmin(means)) ** 2
    return 100.0 * math.sqrt(total)


def correct(values, stamps, header, terms=None, fallback_terms=None, beam_fwhm_deg=None,
            reference_path=None, source_file="", update_reference=True):
    """(corrected values, report). The whole thing, guarded.

    Refuses on anything but a tracked compact source. Otherwise two solutions
    are tried - a fit to this run, and the parameters carried from the last
    quiet run - and the one that leaves less modulation at the pulse phase on
    the run's quiet records is applied (`chosen`: "fitted", "carried" or
    "none"). A fresh fit that wins replaces the carried parameters. The judge
    is `residual_modulation`, not the fit's own chi-squared, so a fit bent by
    a radio burst or a pointing error loses to the carried values rather than
    being applied because it fitted the burst.
    """
    values = np.asarray(values, float)
    stamps = np.asarray(stamps, float)
    ok, why = applies_to(header)
    if not ok:
        return values, {"ok": False, "why": why, "applies": False}
    if terms is None:
        terms, source = pointing_terms(header, fallback_terms)
    else:
        source = "given by the caller"
    try:
        res = fit(values, stamps, header, terms, beam_fwhm_deg=beam_fwhm_deg)
    except Exception as exc:                              # noqa: BLE001
        return values, {"ok": False, "applies": True,
                        "why": "scallop fit failed: %s" % exc}
    res["applies"] = True
    res["terms_source"] = source
    fwhm = float(res.get("beam_fwhm_deg") or beam_fwhm(header, beam_fwhm_deg))

    candidates = {}
    if res.get("ok"):
        candidates["fitted"] = res
    ref = load_reference(reference_path)
    if ref is not None:
        candidates["carried"] = reference_candidate(ref, fwhm)

    try:
        quiet = quiet_mask(values, stamps)
        modulation = {"none": residual_modulation(values, stamps, header, terms, None, quiet)}
        for name, cand in candidates.items():
            modulation[name] = residual_modulation(values, stamps, header, terms, cand, quiet)
    except Exception as exc:                              # noqa: BLE001
        res["ok"] = False
        res["why"] = "scallop judgement failed: %s" % exc
        return values, res
    res["modulation_pct"] = modulation
    res["quiet_records"] = int(quiet.sum())
    fit_why = res.get("why", "")

    best = min(candidates, key=lambda k: modulation[k]) if candidates else None
    if best is None or modulation[best] >= modulation["none"]:
        res["ok"] = False
        res["chosen"] = "none"
        parts = []
        if "fitted" in candidates:
            parts.append("this run's fit leaves %.2f%%" % modulation["fitted"])
        else:
            parts.append(fit_why)
        if "carried" in candidates:
            parts.append("the parameters carried from %s leave %.2f%%"
                         % (ref.get("fitted_utc", "?"), modulation["carried"]))
        else:
            parts.append("no carried parameters")
        res["why"] = "nothing applied (%.2f%% modulation uncorrected): %s" % (
            modulation["none"], "; ".join(parts))
        return values, res

    chosen = candidates[best]
    if best == "carried":
        for key in ("phase_alt_deg", "phase_az_deg", "k_alt", "k_az", "k_beam", "beam_fwhm_deg"):
            res[key] = chosen[key]
        res["ok"] = True
        res["reference_utc"] = chosen["reference_utc"]
        res["reference_file"] = chosen["reference_file"]
        res["why"] = ("carried parameters from %s applied (%.2f%% modulation left, against "
                      "%.2f%% uncorrected%s): %s"
                      % (chosen["reference_utc"], modulation["carried"], modulation["none"],
                         ", %.2f%% from this run's fit" % modulation["fitted"] if "fitted" in candidates else "",
                         fit_why if "fitted" not in candidates else "this run's fit was worse"))
    else:
        res["why"] = "%s (%.2f%% modulation left, against %.2f%% uncorrected%s)" % (
            fit_why, modulation["fitted"], modulation["none"],
            ", %.2f%% with the carried parameters" % modulation["carried"] if "carried" in candidates else "")
        if update_reference:
            try:
                save_reference(res, source_file=source_file, path=reference_path)
            except OSError:
                pass
    res["chosen"] = best
    g = gain(res, stamps, header, terms)
    res["mean_gain"] = float(np.mean(g))
    res["peak_to_peak_pct"] = float(100 * (g.max() - g.min()))
    return values / g, res


# Where the terms came from, in the few words a plot has room for. The full
# sentence stays in `terms_source` for the log and the API.
_TERMS_SHORT = {"the recording's own": "from the file",
                "the controller's current": "controller's current",
                "the last model this installation fitted": "last fitted model",
                "none - refraction only": "NONE - refraction only",
                "given by the caller": "caller's"}


def plot_caption(report):
    """One line for a plot subtitle, or '' if the scallop does not arise.

    Short enough to fit the figure - `why` is written for the log, and at two
    axes with amplitudes, phases and an explanation it runs off the page.
    """
    if not report or not report.get("applies"):
        return ""
    terms = _TERMS_SHORT.get(report.get("terms_source", ""), report.get("terms_source", "?"))
    if not report.get("ok"):
        return "tracking scallop left in: %s [terms %s]" % (report.get("why", ""), terms)
    fwhm = report.get("beam_fwhm_deg", 0.0)
    k_beam = report.get("k_beam") or 1.0
    axes = "; ".join("%s %s" % (ax, "%.2fx" % (report["k_" + ax] / k_beam)
                                if report.get("k_" + ax) else "left in")
                     for ax in ("alt", "az"))
    if report.get("chosen") == "carried":
        origin = "carried %s" % (report.get("reference_utc", "?") or "?")[:10]
    else:
        origin = "fitted"
    mod = report.get("modulation_pct") or {}
    left = ""
    if "none" in mod and report.get("chosen") in mod:
        # the modulation folded at the pulse phase, before -> after
        left = ", %.2f->%.2f%%" % (mod["none"], mod[report["chosen"]])
    return ("scallop removed (%s): %s the %.2f deg beam, phase %+.3f/%+.3f%s [%s]"
            % (origin, axes, fwhm,
               report.get("phase_alt_deg", 0.0), report.get("phase_az_deg", 0.0), left, terms))
