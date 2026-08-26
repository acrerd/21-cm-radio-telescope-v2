"""The predicted drift scan against the recorded one: a two-parameter fit.

A drift scan is a *total power* measurement. The dish is parked, the sky
drifts through the beam, and what is recorded is

    counts(t) = G . (T_sys + T_A(t))

with T_A(t) the beam-averaged sky at whatever the fixed pointing is looking at
as the Earth turns. The simulator can predict T_A(t) - the H I cube, the
diffuse continuum and the bright sources, through the measured beam - so G and
T_sys follow from a straight line through the data against the prediction.

That is all it takes. In particular it needs **no bandpass template**: the
band-integrated power is the spectrum summed over channels, and the bandpass
shape is the same constant factor on every record, so it lives inside G. This
is what lets a drift scan at any tuning be compared with the simulator, where
the per-channel gain fit (rf_calibration.calibrate_observation) rightly refuses
a tuning it has no template for. The two gains are not the same number - this
one carries the band-mean of the bandpass shape and belongs to the band it was
fitted on - which is why a total-power fit is reported and drawn, never applied
as the per-channel calibration.

What is approximate about it, learned on the first real run (Cas A, 2026-08-26):
the "constant factor" is only constant for a flat sky. The band mean sums
channels with different gains, and the H I line - which dominated the model
curve on the plane - sits on channels 12% hotter than the continuum ones. So
the fitted G is a band-weighted mixture, and a point source read on that scale
came out 1.5-2x, depending on which channels anchored the gain. Anchoring the
gain on the H I line alone (its slope against the model, 1.43e-5 counts/K)
agreed with the per-channel calibration to 5%, which is what the 1.4 MHz LO
shift predicts - so the H I cube through the beam is right, the point-source
arithmetic is right by hand, and what is left is Cas A itself: 1.46x the model
on that scale and a 29-minute crossing against 40 predicted. Both point at the
beam, not the code. See CLAUDE.md.
"""

import math
import os
from datetime import datetime, timezone

import numpy as np

# Fraction of the recorded band, centred, that goes into the band power. The
# outer tenth either side is the filter skirt: measured on the 2026-08-26 Cas A
# scan the response is flat to +-1 MHz, 89% at +-1.8 MHz (this edge, at 4.5
# Msps), 78% at +-2.0 and 41% at the band edge. The skirt is where a small LO
# or filter drift moves the mean most, and where band-edge interference lives;
# the channels given up cost nothing measurable, since total power is
# instability-limited rather than thermal. Was 60% until the operator chose to
# keep more of the flat band.
BAND_FRACTION = 0.8
# Channels within this of the tuned centre are the LO artefact.
DC_MASK_HZ = 30_000.0
# Model samples along the track; the sky through a 5-degree beam changes
# slowly, so ~200 points over a scan of any length is plenty, and it bounds
# the simulator cost for a run with thousands of records.
MODEL_SAMPLES = 200


def _fixed_pointing(header):
    """(alt, az) the dish was parked at, from the recording's own header."""
    system = str(header.get("coord_system", "") or "").lower()
    if system == "drift" and header.get("drift_alt") is not None:
        return float(header["drift_alt"]), float(header["drift_az"])
    if system == "altaz":
        return float(header.get("coord1_deg", 0.0)), float(header.get("coord2_deg", 0.0))
    raise ValueError("the recording does not say where the dish was parked "
                     "(coord_system %r)" % system)


def track_galactic(alt_deg, az_deg, stamps):
    """Galactic (l, b) of a fixed alt/az pointing at each unix time."""
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time
    import astropy.units as u
    from observatory import SITE_HEIGHT_M, SITE_LAT_DEG, SITE_LON_DEG

    site = EarthLocation(lat=SITE_LAT_DEG * u.deg, lon=SITE_LON_DEG * u.deg,
                         height=SITE_HEIGHT_M * u.m)
    times = Time(np.asarray(stamps, float), format="unix")
    sky = SkyCoord(alt=np.full(len(times), alt_deg) * u.deg,
                   az=np.full(len(times), az_deg) * u.deg,
                   frame=AltAz(obstime=times, location=site)).galactic
    return np.asarray(sky.l.deg, float), np.asarray(sky.b.deg, float)


H1_REST_HZ = 1420.405752e6


def _band_window(header, freq_hz):
    """The frequency span the continuum is taken over, and the channels in it.

    The recording's continuum band (fixed instrument, issue #27), which by
    construction holds no hydrogen, less the LO spur and anything inside the
    H I band. A recording that does not name its bands cannot be reduced as
    continuum.
    """
    freq_hz = np.asarray(freq_hz, float)
    cont = header.get("continuum_band_hz")
    if cont is None:
        raise ValueError("the recording names no continuum band: it was made "
                         "before the fixed instrument")
    fc = float(header.get("center_freq_hz", np.median(freq_hz)))
    dc = float(header.get("dc_artefact_freq_hz", fc))
    lo, hi = float(cont[0]), float(cont[1])
    keep = (freq_hz >= lo) & (freq_hz <= hi) & (np.abs(freq_hz - dc) > DC_MASK_HZ)
    keep &= ~h1_channels(header, freq_hz)
    return lo, hi, keep


def h1_channels(header, freq_hz):
    """Which channels of this axis carry the H I band the file names."""
    freq_hz = np.asarray(freq_hz, float)
    band = header.get("h1_band_hz")
    if band is None:
        raise ValueError("the recording names no H I band")
    return (freq_hz >= float(band[0])) & (freq_hz <= float(band[1]))


def band_power(freq_hz, spectra, header):
    """Mean counts per record over the continuum window - the total-power series."""
    _, _, keep = _band_window(header, np.asarray(freq_hz, float))
    if not keep.any():
        raise ValueError("no continuum channels: the band is all hydrogen or spur")
    return np.nanmean(np.asarray(spectra, float)[:, keep], axis=1)


def predicted_track(header, stamps, freq_hz, sim=None):
    """The simulator's band-mean T_A (K) at each record time.

    Sampled at MODEL_SAMPLES points along the track and interpolated: the
    beam-averaged sky changes slowly against a 5-degree beam.
    """
    import rf_calibration

    stamps = np.asarray(stamps, float)
    alt, az = _fixed_pointing(header)
    lo, hi, _ = _band_window(header, np.asarray(freq_hz, float))
    idx = np.unique(np.linspace(0, len(stamps) - 1,
                                min(MODEL_SAMPLES, len(stamps))).astype(int))
    glon, glat = track_galactic(alt, az, stamps[idx])
    # The simulator's spectrum has to reach the continuum window, which on a
    # fixed-instrument file lies below the line: ask for a band wide enough
    # to cover it from the rest frequency.
    reach = 2.0 * max(abs(lo - H1_REST_HZ), abs(hi - H1_REST_HZ)) + 0.5e6
    bandwidth = max(float(header.get("sample_rate_hz", 4.5e6)), reach)
    sim = sim or rf_calibration.load_simulator(bandwidth_hz=bandwidth)
    model = np.empty(len(idx))
    for k, (l, b, t) in enumerate(zip(glon, glat, stamps[idx])):
        when = datetime.fromtimestamp(float(t), tz=timezone.utc)
        f, ta = rf_calibration.simulated_spectrum(
            float(l), float(b), when, sim, bandwidth_hz=bandwidth)
        f, ta = np.asarray(f, float), np.asarray(ta, float)
        # The same channels the data are measured over: the continuum
        # window with the hydrogen cut out.
        inside = (f >= lo) & (f <= hi) & ~h1_channels(header, f)
        model[k] = float(np.nanmean(ta[inside])) if inside.any() else float(np.nanmean(ta))
    return np.interp(stamps, stamps[idx], model), (stamps[idx], glon, glat)


def fit_total_power(path, sim=None):
    """G and T_sys from counts(t) = G (T_sys + T_model(t)), by least squares.

    Returns everything needed to draw the comparison: the record times, the
    measured band power converted to kelvin with the fitted G and T_sys, the
    model, and the fit statistics.
    """
    from observation_plot import read_observation

    # The continuum product, with the H I band cut out; a recording without
    # one raises here and the caller reports it.
    freq_hz, spectra, stamps, taus, header = read_observation(path, product="wide")
    stamps = np.asarray(stamps, float)
    if stamps.size < 3:
        raise ValueError("too few records to fit (%d)" % stamps.size)
    counts = band_power(freq_hz, spectra, header)
    model, samples = predicted_track(header, stamps, freq_hz, sim)

    ok = np.isfinite(counts) & np.isfinite(model)
    if ok.sum() < 3:
        raise ValueError("too few usable records to fit")
    # counts = G*model + G*T_sys: slope and intercept.
    slope, intercept = np.polyfit(model[ok], counts[ok], 1)
    if slope <= 0:
        raise ValueError("the fitted gain is not positive; the data do not "
                         "follow the predicted drift curve")
    gain = float(slope)
    t_sys = float(intercept / slope)
    measured_k = counts / gain - t_sys
    resid = measured_k - model
    corr = float(np.corrcoef(model[ok], counts[ok])[0, 1]) if ok.sum() > 2 else float("nan")
    return {
        "kind": "total_power",
        "gain_counts_per_k": gain,
        "t_sys_k": t_sys,
        "correlation": corr,
        "residual_rms_k": float(np.std(resid[ok])),
        "model_span_k": float(np.nanmax(model) - np.nanmin(model)),
        "n_records": int(stamps.size),
        "records_used": int(ok.sum()),
        "stamps": stamps.tolist(),
        "measured_k": measured_k.tolist(),
        "model_k": model.tolist(),
        "track": {"stamps": samples[0].tolist(), "glon": samples[1].tolist(),
                  "glat": samples[2].tolist()},
        "band_window_hz": list(_band_window(header, np.asarray(freq_hz, float))[:2]),
        "pointing": dict(zip(("alt_deg", "az_deg"), _fixed_pointing(header))),
        "source_file": os.path.basename(path),
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {
            "lo_hz": float(header.get("center_freq_hz", 0.0)),
            "sample_rate_hz": float(header.get("sample_rate_hz", 0.0)),
            "gain_db": (float(header["gain_db"]) if header.get("gain_db") is not None
                        else None),
        },
        "approximate": ("total-power fit: two parameters (gain, T_sys) against the "
                        "simulator's predicted drift curve through the measured "
                        "beam. The bandpass shape is absorbed into the gain - which "
                        "is why no template is needed - but only as a band average: "
                        "the H I line and the continuum sit on channels with "
                        "different gain (12% apart on the 2026-08-26 Cas A scan), "
                        "so the kelvin scale is a mixture where the model has line "
                        "structure. Not the per-channel calibration."),
    }
