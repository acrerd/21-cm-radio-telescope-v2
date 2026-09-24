"""The beam, measured from a Sun drift scan and kept as a calibration product.

A drift scan parks the dish where the Sun will be and lets the Sun cross the
beam; the record of band power against time is the beam's cross-section,
the Sun's 0.5 deg disc being small against it. The number every consumer
needs is the main lobe's SOLID ANGLE - that is what the antenna theorem turns
into a collecting area, and so into every flux - with the FWHM as its
Gaussian equivalent for the simulators, which convolve with a Gaussian.

Measured, not fitted: the crossing is not Gaussian (a focused aperture has a
flattened top), and a Gaussian fitted to it depends on the window it is
fitted over (5.13 deg over +-4.7, 4.79 over +-7.4 on the same 2026-09 data).
So the lobe is integrated directly, 2 pi int P(theta) theta dtheta, on each
side of the crossing out to its first null, with the baseline taken from the
far ends of the scan. The two sides are a check on each other.

The result lives in beam_calibration.json beside the gain and the bandpass,
read by `instrument.measured_beam()` - the same path-based boundary as
`measured_t_sys_k` - so the scheduler, the simulators and the receiver all
take the beam from one place, and a feed change is followed by one drift scan
rather than by editing a constant in five files.
"""
import datetime
import glob
import json
import math
import os

import numpy as np

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BEAM_FILE = os.path.join(_SCRIPT_DIR, "beam_calibration.json")
BEAM_ARCHIVE_DIR = os.path.join(_SCRIPT_DIR, "beam_calibrations")
BEAM_VERSION = 1

# The Sun crosses the beam at ~0.23 deg/min in September (15 deg/h cos dec),
# so a two-hour scan sees +-14 deg of drift: the whole main lobe, the first
# sidelobe on both sides, and a clean baseline beyond them. An hour saw
# +-7 and a hint of a sidelobe at its end (2026-09-24).
DRIFT_MINUTES = 120
DRIFT_INTEGRATION_S = 10.0

# Quality: what a scan must show before its numbers are adopted.
MIN_HALF_SPAN_DEG = 6.0          # each side of the crossing, to reach the baseline
MAX_CLOSEST_APPROACH_DEG = 0.6   # the Sun must pass through the beam's middle
MIN_PEAK_TO_TSYS = 3.0           # the Sun must stand well clear of the noise
MAX_SIDE_DISAGREEMENT = 0.12     # the two sides' lobes, as a fraction
BASELINE_BEYOND_DEG = 6.5        # where the baseline is taken from
NULL_LEVEL = 0.005               # the lobe ends where the profile falls to this of peak
SIDELOBE_SPAN_DEG = 9.0          # a side reaching this far has measured its first sidelobe

SITE_LAT, SITE_LON, SITE_HEIGHT = "55.902426", "-4.307865", 50


def profile_from_file(path):
    """(theta_signed_deg, band_power, stamps, header) for a Sun drift recording.

    theta is the Sun's great-circle separation from the parked beam centre
    (the recording's `drift_alt`/`drift_az`, the true position of the grid
    point the mount was parked on), signed negative before the crossing and
    positive after. The pilot correction is reversed so the profile is raw
    counts: a total-power shape must not depend on a gain correction.
    """
    import ephem
    import observation_plot as op
    import drift_fit
    freq, spectra, stamps, taus, header = op.read_observation(path, product="wide",
                                                              keep_pilot=False)
    power = drift_fit.band_power(freq, spectra, header)
    t = np.asarray(stamps, float)
    if header.get("drift_alt") is None or header.get("drift_az") is None:
        raise ValueError("not a drift recording: no parked position in the file")
    alt0, az0 = math.radians(float(header["drift_alt"])), math.radians(float(header["drift_az"]))
    observer = ephem.Observer()
    observer.lat, observer.lon, observer.elevation = SITE_LAT, SITE_LON, SITE_HEIGHT
    sun = ephem.Sun()
    theta, side = np.empty(len(t)), np.empty(len(t))
    for i, ti in enumerate(t):
        observer.date = ephem.Date(datetime.datetime.utcfromtimestamp(float(ti)))
        sun.compute(observer)
        a, z = float(sun.alt), float(sun.az)
        cosd = math.sin(a) * math.sin(alt0) + math.cos(a) * math.cos(alt0) * math.cos(z - az0)
        theta[i] = math.degrees(math.acos(max(-1.0, min(1.0, cosd))))
        side[i] = 1.0 if math.sin(z - az0) >= 0 else -1.0
    return -side * theta, power, t, header


def analyse_profile(theta_signed, power, t_sys_k=None):
    """The main lobe from a drift profile: solid angle, FWHM equivalent,
    per-side integrals, sidelobe level, and the quality checks.

    `theta_signed` in degrees (negative before the crossing), `power` in any
    linear unit. Returns a dict; `ok` says whether the scan meets the bar and
    `why` lists what it failed.
    """
    theta = np.asarray(theta_signed, float)
    p = np.asarray(power, float)
    absth = np.abs(theta)
    out = {"n_records": int(len(p)),
           "span_before_deg": float(-theta.min()), "span_after_deg": float(theta.max()),
           "closest_approach_deg": float(absth.min())}
    why = []
    base_sel = absth > BASELINE_BEYOND_DEG
    if base_sel.sum() < 6 or (theta[base_sel] < 0).sum() < 3 or (theta[base_sel] > 0).sum() < 3:
        why.append("too little baseline beyond %.1f deg on both sides" % BASELINE_BEYOND_DEG)
        base = np.full_like(p, np.median(p[absth > absth.max() - 2.0]) if len(p) else 0.0)
    else:
        base = np.polyval(np.polyfit(theta[base_sel], p[base_sel], 1), theta)
    excess = p - base
    core = absth < 0.6
    if not core.any():
        why.append("the Sun never came within 0.6 deg of the beam centre")
        peak = float(excess.max())
    else:
        peak = float(excess[core].max())
    if peak <= 0:
        why.append("no source above the baseline")
        out.update(ok=False, why=why)
        return out
    pn = excess / peak
    out["peak_over_baseline"] = float((peak + base[np.argmax(excess)]) / base[np.argmax(excess)])
    if t_sys_k:
        out["t_a_peak_k"] = float(t_sys_k) * (out["peak_over_baseline"] - 1.0)

    sides = {}
    for name, sel in (("before", theta < 0), ("after", theta > 0)):
        x, y = absth[sel], pn[sel]
        order = np.argsort(x)
        x, y = x[order], y[order]
        if len(x) < 5:
            sides[name] = None
            continue
        smooth = np.convolve(y, np.ones(5) / 5.0, mode="same")
        # The lobe ends at its first null: the first minimum past the core
        # where the profile is small, or where it falls to NULL_LEVEL, whichever
        # comes first. A sidelobe close in keeps the profile above NULL_LEVEL,
        # so the minimum is what separates the two.
        end = len(x)
        for i in range(2, len(x) - 2):
            if x[i] < 1.0:
                continue
            if smooth[i] <= NULL_LEVEL:
                end = i
                break
            if smooth[i] < 0.05 and smooth[i] <= smooth[i - 1] and smooth[i] < smooth[i + 1] \
                    and smooth[i + 2] > smooth[i]:
                end = i
                break
        xx, yy = x[:end], np.clip(y[:end], 0.0, None)
        omega = 2.0 * math.pi * float(np.trapz(yy * xx, xx)) if len(xx) > 1 else float("nan")
        # First sidelobe: the largest excess beyond the null, as a fraction of peak.
        tail = smooth[end:] if end < len(x) else np.array([])
        sidelobe = float(np.max(tail)) if tail.size else None
        sides[name] = {"solid_angle_sq_deg": omega, "null_deg": float(xx[-1]) if len(xx) else None,
                       "sidelobe_peak_fraction": sidelobe,
                       "sidelobe_measured": bool(x.max() >= SIDELOBE_SPAN_DEG),
                       "span_deg": float(x.max())}
    out["sides"] = sides
    good = [s for s in sides.values() if s and math.isfinite(s["solid_angle_sq_deg"])]
    if len(good) < 2:
        why.append("the lobe could not be integrated on both sides")
        out.update(ok=False, why=why)
        return out
    omegas = [s["solid_angle_sq_deg"] for s in good]
    omega = float(np.mean(omegas))
    disagreement = abs(omegas[0] - omegas[1]) / omega
    out.update({
        "solid_angle_sq_deg": omega,
        "solid_angle_sr": omega * math.radians(1.0) ** 2,
        "fwhm_deg": math.sqrt(omega / 1.133),
        "side_disagreement": float(disagreement),
        "sidelobe_peak_fraction": max((s["sidelobe_peak_fraction"] or 0.0) for s in good),
        "sidelobe_measured": all(s["sidelobe_measured"] for s in good),
    })
    # A Gaussian over the whole window, for comparison only: it is what the
    # rasters report and what people expect to see, and it is window-dependent.
    try:
        from scipy.optimize import curve_fit
        g = lambda x, a, w, x0, c: a * np.exp(-4 * np.log(2) * ((x - x0) / w) ** 2) + c
        popt, _ = curve_fit(g, theta, p, p0=[peak, out["fwhm_deg"], 0.0, float(np.median(base))])
        out["gaussian_fwhm_deg"] = float(abs(popt[1]))
        out["gaussian_centre_deg"] = float(popt[2])
    except Exception:                                  # noqa: BLE001
        pass
    if out["span_before_deg"] < MIN_HALF_SPAN_DEG or out["span_after_deg"] < MIN_HALF_SPAN_DEG:
        why.append("scan too short: %.1f / %.1f deg either side, %.0f needed"
                   % (out["span_before_deg"], out["span_after_deg"], MIN_HALF_SPAN_DEG))
    if out["closest_approach_deg"] > MAX_CLOSEST_APPROACH_DEG:
        why.append("the Sun passed %.2f deg from the beam centre" % out["closest_approach_deg"])
    if out["peak_over_baseline"] < MIN_PEAK_TO_TSYS:
        why.append("the Sun is only %.1fx the baseline" % out["peak_over_baseline"])
    if disagreement > MAX_SIDE_DISAGREEMENT:
        why.append("the two sides disagree by %.0f%%" % (100 * disagreement))
    out.update(ok=not why, why=why)
    return out


def analyse_drift(path):
    """Analyse a Sun drift recording; the result carries what it was made from."""
    theta, power, stamps, header = profile_from_file(path)
    t_sys = header.get("applied_t_sys_k")
    result = analyse_profile(theta, power, float(t_sys) if t_sys is not None else None)
    result.update({
        "version": BEAM_VERSION,
        "source_file": os.path.basename(path),
        # Stamped at the crossing (the scan's mid-point), which is when the
        # beam was measured, not when somebody pressed Analyse.
        "measured_utc": datetime.datetime.fromtimestamp(
            float(np.mean(stamps)), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gain_db": header.get("gain_db"),
        "t_sys_k": t_sys,
        "created_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "profile": {"theta_deg": [round(float(x), 4) for x in theta],
                    "power": [float(x) for x in power]},
    })
    return result


def _stamp(doc):
    keep = [c for c in str(doc.get("measured_utc") or "") if c.isdigit()]
    return "%sT%sZ" % ("".join(keep[:8]), "".join(keep[8:14])) if len(keep) >= 14 else "unknown"


def save_beam_calibration(result, path=None):
    """Adopt a result as the beam in force, archiving a dated copy first."""
    path = path or BEAM_FILE
    os.makedirs(BEAM_ARCHIVE_DIR, exist_ok=True)
    doc = {k: v for k, v in result.items() if k != "profile"}
    archive = os.path.join(BEAM_ARCHIVE_DIR, "beam_%s.json" % _stamp(doc))
    with open(archive, "w") as fh:
        json.dump(result, fh, indent=2)
    with open(path, "w") as fh:
        json.dump(doc, fh, indent=2)
    return archive


def load_beam_calibration(path=None):
    try:
        path = path or BEAM_FILE
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def list_beam_calibrations():
    out = []
    for fn in sorted(glob.glob(os.path.join(BEAM_ARCHIVE_DIR, "beam_*.json")), reverse=True):
        try:
            with open(fn) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        out.append({"name": os.path.basename(fn)[:-5], "measured_utc": d.get("measured_utc"),
                    "fwhm_deg": d.get("fwhm_deg"), "solid_angle_sq_deg": d.get("solid_angle_sq_deg"),
                    "source_file": d.get("source_file"), "ok": d.get("ok")})
    return out


def plot_beam(result, out_path):
    """The profile, the baseline, the integration limits and the Gaussian."""
    import plot_backend
    plot_backend.use_headless()
    import matplotlib.pyplot as plt
    prof = result.get("profile") or {}
    th = np.asarray(prof.get("theta_deg", []), float)
    p = np.asarray(prof.get("power", []), float)
    if not len(th):
        raise ValueError("no profile to plot")
    fig, ax = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    ax[0].plot(th, p, ".", ms=3, label="band power")
    for name, sign in (("before", -1), ("after", 1)):
        s = (result.get("sides") or {}).get(name)
        if s and s.get("null_deg"):
            ax[0].axvline(sign * s["null_deg"], color="orange", lw=0.8, ls="--")
    ax[0].set_ylabel("counts")
    ax[0].set_title("Sun drift %s: main lobe %.1f sq deg, FWHM-equivalent %.2f deg%s" % (
        result.get("source_file", ""), result.get("solid_angle_sq_deg", float("nan")),
        result.get("fwhm_deg", float("nan")), "" if result.get("ok") else "  [NOT ADOPTED: %s]" % "; ".join(result.get("why", []))))
    ax[0].legend(); ax[0].grid(alpha=0.3)
    base = np.polyval(np.polyfit(th[np.abs(th) > BASELINE_BEYOND_DEG], p[np.abs(th) > BASELINE_BEYOND_DEG], 1), th) \
        if (np.abs(th) > BASELINE_BEYOND_DEG).sum() > 5 else np.full_like(p, np.median(p))
    ex = (p - base); ex /= ex[np.abs(th) < 0.6].max() if (np.abs(th) < 0.6).any() else ex.max()
    ax[1].semilogy(th, np.clip(ex, 1e-4, None), ".", ms=3)
    if result.get("gaussian_fwhm_deg"):
        w, x0 = result["gaussian_fwhm_deg"], result.get("gaussian_centre_deg", 0.0)
        ax[1].semilogy(th, np.clip(np.exp(-4 * np.log(2) * ((th - x0) / w) ** 2), 1e-4, None), "-", lw=0.8,
                       label="Gaussian %.2f deg (whole window)" % w)
    ax[1].axhline(NULL_LEVEL, color="orange", lw=0.8, ls="--", label="null level")
    ax[1].set_ylim(1e-4, 2); ax[1].set_ylabel("fraction of peak"); ax[1].set_xlabel("offset from beam centre (deg, along the drift)")
    ax[1].legend(); ax[1].grid(alpha=0.3)
    fig.tight_layout(); fig.savefig(out_path, dpi=90); plt.close(fig)
    return out_path
