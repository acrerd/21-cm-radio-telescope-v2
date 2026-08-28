#!/usr/bin/env python3
"""Tests for the measured bandpass correction."""

import numpy as np
import pytest

import bandpass


LO = 1421.205752e6
SKY = 1420.405752e6
RATE = 4.5e6
NCH = 4096


def _header(**over):
    h = {"center_freq_hz": LO, "sample_rate_hz": RATE,
         "sky_center_freq_hz": SKY, "lo_offset_hz": 0.8e6,
         "sample_rate_requested_hz": 2.0e6, "gain_db": 40.0}
    h.update(over)
    return h


def _band(nch=NCH, rate=RATE, lo=LO):
    return lo + (np.arange(nch) - nch // 2) * (rate / nch)


def _shape(freq_hz, lo=LO, rate=RATE):
    """A plausible instrument response: a tilt on top of a symmetric droop.

    Kept close to the real thing, and above all kept positive. The tilt
    coefficient gives 3.8%/MHz, which is what the front end was measured at on
    2026-08-24, and the even terms bring the band edge to about 0.69 of centre,
    near the measured roll-off. An earlier version of this used a tilt three
    times too steep and went *negative* near the band edge - which is not a
    bandpass, and which apply_bandpass rightly declined to divide by.
    """
    u = (freq_hz - lo) / rate
    return (1.0 + 0.17 * u) * (1.0 - 1.6 * u ** 2 - 3.0 * u ** 4)


def _observation(line_amplitude=0.0, noise=0.0, seed=1, nch=NCH):
    f = _band(nch)
    sky = np.ones_like(f)
    if line_amplitude:
        sky = sky + line_amplitude * np.exp(-0.5 * ((f - SKY) / 60e3) ** 2)
    mean = _shape(f) * sky
    rng = np.random.default_rng(seed)
    spectra = mean[None, :] * (1.0 + noise * rng.standard_normal((8, f.size)))
    return f, spectra


def test_fit_then_apply_flattens_the_band():
    f, spectra = _observation()
    t = bandpass.fit_bandpass(f, spectra, _header())
    corrected, note = bandpass.apply_bandpass(f, spectra, _header(), t)
    inside = np.isfinite(corrected[0])
    assert inside.sum() > NCH // 3
    assert np.nanstd(corrected[:, inside]) / np.nanmean(corrected[:, inside]) < 1e-3
    assert "bandpass corrected" in note


def test_the_band_fitted_covers_what_the_tuning_will_ask_for():
    """The reduced spectrum reaches offset + bandwidth/2 from the LO."""
    f, spectra = _observation()
    t = bandpass.fit_bandpass(f, spectra, _header())
    reach = abs(0.8e6) + 2.0e6 / 2          # 1.8 MHz below the LO
    assert t["u_scale_hz"] >= reach, "template narrower than the plotted band"


def test_a_line_is_not_absorbed_into_the_template():
    """The Lockman Hole is not 0 K; fitting through its line would subtract it."""
    f, clean = _observation(line_amplitude=0.0)
    _, with_line = _observation(line_amplitude=0.05)
    a = bandpass.fit_bandpass(f, clean, _header())
    b = bandpass.fit_bandpass(f, with_line, _header())
    ma = bandpass.evaluate(a, f)
    mb = bandpass.evaluate(b, f)
    ok = np.isfinite(ma) & np.isfinite(mb)
    # A 5% line must not move the template by more than a small fraction of it.
    assert np.nanmax(np.abs(mb[ok] / ma[ok] - 1.0)) < 0.005


def test_a_masked_line_still_shows_up_after_correction():
    f, clean = _observation(line_amplitude=0.0)
    t = bandpass.fit_bandpass(f, clean, _header())
    _, with_line = _observation(line_amplitude=0.05)
    corrected, _ = bandpass.apply_bandpass(f, with_line, _header(), t)
    k = int(np.argmin(np.abs(f - SKY)))
    peak = corrected[:, k].mean() / np.nanmedian(corrected)
    assert peak == pytest.approx(1.05, abs=0.01)


def test_it_refuses_a_different_local_oscillator():
    f, spectra = _observation()
    t = bandpass.fit_bandpass(f, spectra, _header())
    other = _header(center_freq_hz=LO + 200e3)
    ok, why = bandpass.applies_to(t, other)
    assert not ok and "LO" in why
    out, note = bandpass.apply_bandpass(f, spectra, other, t)
    assert np.shares_memory(out, spectra) or np.array_equal(out, spectra)
    assert "not bandpass corrected" in note


def test_it_refuses_a_different_sample_rate():
    f, spectra = _observation()
    t = bandpass.fit_bandpass(f, spectra, _header())
    ok, why = bandpass.applies_to(t, _header(sample_rate_hz=7.0e6))
    assert not ok and "Msps" in why


def test_no_template_is_not_an_error():
    f, spectra = _observation()
    out, note = bandpass.apply_bandpass(f, spectra, _header(), None,
                                        path="/nonexistent/template.json")
    assert np.array_equal(out, spectra)
    assert "no bandpass template" in note


def test_outside_the_fitted_band_is_dropped_not_extrapolated():
    """A polynomial leaves its data without saying so; NaN says so."""
    f, spectra = _observation()
    t = bandpass.fit_bandpass(f, spectra, _header())
    wide = _band(nch=NCH) + 0.0
    far = np.array([LO - 0.9 * RATE, LO + 0.9 * RATE])
    assert np.all(np.isnan(bandpass.evaluate(t, far)))
    assert np.isfinite(bandpass.evaluate(t, wide)).any()


def test_the_correction_preserves_the_overall_level():
    """Flattening the shape must not silently rescale the counts."""
    f, spectra = _observation()
    t = bandpass.fit_bandpass(f, spectra, _header())
    corrected, _ = bandpass.apply_bandpass(f, spectra, _header(), t)
    inside = np.isfinite(corrected[0])
    before = np.median(spectra[:, inside])
    after = np.median(corrected[:, inside])
    assert after == pytest.approx(before, rel=0.02)


def test_round_trip_through_the_stored_file(tmp_path):
    f, spectra = _observation()
    t = bandpass.fit_bandpass(f, spectra, _header(), source_name="Lockman Hole")
    p = tmp_path / "template.json"
    bandpass.save_bandpass(t, str(p))
    back = bandpass.load_bandpass(str(p))
    assert back["degree"] == t["degree"]
    assert back["coefficients"] == pytest.approx(t["coefficients"])
    _, note = bandpass.apply_bandpass(f, spectra, _header(), back)
    assert "Lockman Hole" in note


def test_a_template_from_the_wrong_version_is_ignored(tmp_path):
    f, spectra = _observation()
    t = bandpass.fit_bandpass(f, spectra, _header())
    t["version"] = bandpass.TEMPLATE_VERSION + 1
    p = tmp_path / "template.json"
    bandpass.save_bandpass(t, str(p))
    assert bandpass.load_bandpass(str(p)) is None


def test_an_observation_with_no_tuning_recorded_cannot_be_fitted():
    f, spectra = _observation()
    with pytest.raises(ValueError):
        bandpass.fit_bandpass(f, spectra, {"sample_rate_hz": RATE})
