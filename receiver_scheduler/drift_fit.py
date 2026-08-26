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

What is approximate about it is the model, not the fit: the beam is 5 degrees
and the prediction is the compact cube through a Gaussian beam at ~200 sample
points along the track, interpolated to every record. Good to the level of the
continuum sources and the plane; not a substitute for a calibration field.
"""

import math
import os
from datetime import datetime, timezone

import numpy as np

# Fraction of the recorded band, centred, that goes into the band power. The
# outer 20% either side is the filter skirt, where the receiver's response
# is falling and any narrow interference at the band edge is loudest.
BAND_FRACTION = 0.6
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


def _band_window(header, freq_hz):
    """The frequency span the band power is taken over, and the channels in it."""
    fc = float(header.get("center_freq_hz", np.median(freq_hz)))
    sr = float(header.get("sample_rate_hz", abs(freq_hz[-1] - freq_hz[0])))
    lo, hi = fc - 0.5 * BAND_FRACTION * sr, fc + 0.5 * BAND_FRACTION * sr
    dc = float(header.get("dc_artefact_freq_hz", fc))
    keep = (freq_hz >= lo) & (freq_hz <= hi) & (np.abs(freq_hz - dc) > DC_MASK_HZ)
    return lo, hi, keep


def band_power(freq_hz, spectra, header):
    """Mean counts per record over the band window - the total-power series."""
    _, _, keep = _band_window(header, np.asarray(freq_hz, float))
    if not keep.any():
        raise ValueError("no channels inside the band window")
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
    sim = sim or rf_calibration.load_simulator(
        bandwidth_hz=float(header.get("sample_rate_hz", 4.5e6)))
    model = np.empty(len(idx))
    for k, (l, b, t) in enumerate(zip(glon, glat, stamps[idx])):
        when = datetime.fromtimestamp(float(t), tz=timezone.utc)
        f, ta = rf_calibration.simulated_spectrum(
            float(l), float(b), when, sim,
            bandwidth_hz=float(header.get("sample_rate_hz", 4.5e6)))
        f, ta = np.asarray(f, float), np.asarray(ta, float)
        inside = (f >= lo) & (f <= hi)
        model[k] = float(np.nanmean(ta[inside])) if inside.any() else float(np.nanmean(ta))
    return np.interp(stamps, stamps[idx], model), (stamps[idx], glon, glat)


def fit_total_power(path, sim=None):
    """G and T_sys from counts(t) = G (T_sys + T_model(t)), by least squares.

    Returns everything needed to draw the comparison: the record times, the
    measured band power converted to kelvin with the fitted G and T_sys, the
    model, and the fit statistics.
    """
    from observation_plot import read_observation

    freq_hz, spectra, stamps, taus, header = read_observation(path)
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
                        "beam; the bandpass shape is absorbed into the gain, which "
                        "is why no template is needed and why this gain is not the "
                        "per-channel calibration"),
    }
