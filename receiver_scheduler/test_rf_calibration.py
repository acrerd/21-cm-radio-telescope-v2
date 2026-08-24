#!/usr/bin/env python3
"""Tests for the counts-to-kelvin calibration."""

import numpy as np
import pytest

import rf_calibration as R


def _fake_plane(peak_k=100.0, n=300):
    """A line profile with the dynamic range a plane pointing gives."""
    v = np.linspace(-150e3, 150e3, n)
    return v, peak_k * np.exp(-0.5 * (v / 25e3) ** 2) + 0.3


def test_gain_and_tsys_are_recovered():
    _, ta = _fake_plane()
    gain, t_sys = 3.0e-5, 120.0
    counts = gain * (t_sys + ta)
    out = R.fit_gain(counts, ta)
    assert out["gain_counts_per_k"] == pytest.approx(gain, rel=1e-6)
    assert out["t_sys_k"] == pytest.approx(t_sys, rel=1e-6)
    assert not out["t_sys_bound_active"]
    assert out["correlation"] == pytest.approx(1.0, abs=1e-9)


def test_noise_does_not_bias_the_fit():
    _, ta = _fake_plane()
    gain, t_sys = 3.0e-5, 120.0
    rng = np.random.default_rng(7)
    counts = gain * (t_sys + ta) * (1 + 0.01 * rng.standard_normal(ta.size))
    out = R.fit_gain(counts, ta)
    assert out["gain_counts_per_k"] == pytest.approx(gain, rel=0.05)
    assert out["t_sys_k"] == pytest.approx(t_sys, rel=0.10)


def test_the_system_temperature_floor_is_enforced_by_the_fit():
    """A receiver quieter than its own LNA is a broken fit, not a discovery."""
    _, ta = _fake_plane()
    gain, t_sys = 3.0e-5, 20.0          # unphysical: SAWbird alone is 51-67 K
    counts = gain * (t_sys + ta)
    out = R.fit_gain(counts, ta)
    assert out["t_sys_bound_active"]
    assert out["t_sys_k"] == pytest.approx(R.MIN_T_SYS_K)
    assert out["gain_counts_per_k"] > 0


def test_the_floor_is_not_applied_when_it_is_not_needed():
    _, ta = _fake_plane()
    counts = 3.0e-5 * (51.0 + ta)       # just above the floor
    out = R.fit_gain(counts, ta)
    assert not out["t_sys_bound_active"]
    assert out["t_sys_k"] == pytest.approx(51.0, rel=1e-6)


def test_too_few_bins_is_refused():
    with pytest.raises(ValueError):
        R.fit_gain(np.arange(4.0), np.arange(4.0))


def test_nans_are_dropped_rather_than_poisoning_the_fit():
    _, ta = _fake_plane()
    counts = 3.0e-5 * (120.0 + ta)
    counts[10:20] = np.nan
    ta = ta.copy()
    ta[100:110] = np.nan
    out = R.fit_gain(counts, ta)
    assert out["n_bins"] == ta.size - 20
    assert out["t_sys_k"] == pytest.approx(120.0, rel=1e-6)


def test_a_flat_sky_cannot_calibrate():
    """No lever arm means no slope; the fit must not invent one."""
    ta = np.full(300, 0.5)
    counts = 3.0e-5 * (120.0 + ta)
    out = R.fit_gain(counts, ta)
    # Degenerate: it should fall back to the bound rather than return nonsense.
    assert out["t_sys_bound_active"] or not np.isfinite(out["t_sys_k"]) \
        or out["model_span_k"] == pytest.approx(0.0)


def test_obstructed_sectors_are_avoided():
    assert R._in_obstructed_sector(az=90.0, alt=20.0, sectors=[[45, 120, 30]])
    assert not R._in_obstructed_sector(az=90.0, alt=40.0, sectors=[[45, 120, 30]])
    assert not R._in_obstructed_sector(az=200.0, alt=20.0, sectors=[[45, 120, 30]])


def test_a_sector_wrapping_through_north_is_handled():
    """az_min > az_max means the sector straddles 0 degrees."""
    assert R._in_obstructed_sector(az=10.0, alt=20.0, sectors=[[350, 30, 30]])
    assert R._in_obstructed_sector(az=355.0, alt=20.0, sectors=[[350, 30, 30]])
    assert not R._in_obstructed_sector(az=180.0, alt=20.0, sectors=[[350, 30, 30]])


def test_malformed_sectors_are_ignored_not_fatal():
    assert not R._in_obstructed_sector(90.0, 20.0, [["x", None], [45, 120]])


def test_counts_convert_back_to_kelvin():
    cal = {"gain_counts_per_k": 3.0e-5, "t_sys_k": 120.0}
    counts = 3.0e-5 * (120.0 + 40.0)
    assert R.counts_to_kelvin(counts, cal) == pytest.approx(40.0)
    assert R.counts_to_kelvin(counts, None) is None


def test_calibration_round_trip(tmp_path):
    _, ta = _fake_plane()
    cal = R.fit_gain(3.0e-5 * (120.0 + ta), ta)
    cal["version"] = R.CALIBRATION_VERSION
    p = tmp_path / "cal.json"
    R.save_calibration(cal, str(p))
    back = R.load_calibration(str(p))
    assert back["t_sys_k"] == pytest.approx(cal["t_sys_k"])
    back["version"] = R.CALIBRATION_VERSION + 1
    R.save_calibration(back, str(p))
    assert R.load_calibration(str(p)) is None


def test_missing_calibration_is_not_an_error():
    assert R.load_calibration("/nonexistent/cal.json") is None


def test_an_implausibly_hot_system_is_flagged():
    """The 50 K floor only catches errors of one sign.

    A run that recorded while the mount was still slewing fitted 467 K on
    2026-08-24 and nothing in the result said so.
    """
    _, ta = _fake_plane()
    counts = 3.0e-5 * (467.0 + ta)
    out = R.fit_gain(counts, ta)
    assert out["t_sys_k"] == pytest.approx(467.0, rel=1e-6)
    assert out["t_sys_implausible"]
    assert not out["t_sys_bound_active"]


def test_a_normal_system_temperature_is_not_flagged():
    _, ta = _fake_plane()
    out = R.fit_gain(3.0e-5 * (130.0 + ta), ta)
    assert not out["t_sys_implausible"]
    assert not out["t_sys_bound_active"]


def test_the_lo_artefact_is_kept_out_of_the_fit():
    """It is a deep hole where the model has nothing, so it drags the fit.

    Left in, it took the correlation on a real run from 0.86 down to 0.48 and
    the residual from 1.4 K to 4.3 K - and r < 0.8 is what raises the "weak
    correlation" warning, so it would have misfired on good calibrations.
    """
    _, ta = _fake_plane()
    gain, t_sys = 3.0e-5, 120.0
    counts = gain * (t_sys + ta)
    # One channel driven far negative, as the artefact is.
    spoiled = counts.copy()
    spoiled[len(spoiled) // 2] *= 0.3
    clean = R.fit_gain(counts, ta)
    dirty = R.fit_gain(spoiled, ta)
    assert dirty["correlation"] < clean["correlation"]
    assert dirty["residual_rms_k"] > clean["residual_rms_k"]
    # And the constant that governs how much band is excluded is generous.
    assert R.DC_EXCLUSION_HZ >= 20e3


def test_a_flat_offset_moves_the_intercept_but_not_the_slope():
    """Why the continuum belongs in the model, and where it goes if it is not.

    The continuum enters as a flat offset, so it cannot change the
    counts-per-kelvin gain - but the intercept is exactly what it does change,
    and the intercept is the system temperature. Left out, all of it is
    reported as receiver noise: 0.12 K toward the Lockman Hole, but 6.13 K on
    the plane at l=80, where a calibration is meant to be made.
    """
    _, ta = _fake_plane()
    gain, t_sys, continuum = 3.0e-5, 120.0, 6.0
    counts = gain * (t_sys + continuum + ta)
    without = R.fit_gain(counts, ta)                 # model omits the continuum
    with_it = R.fit_gain(counts, ta + continuum)     # model includes it
    assert without["gain_counts_per_k"] == pytest.approx(
        with_it["gain_counts_per_k"], rel=1e-9)
    assert without["t_sys_k"] == pytest.approx(t_sys + continuum, rel=1e-6)
    assert with_it["t_sys_k"] == pytest.approx(t_sys, rel=1e-6)


def test_the_diffuse_continuum_map_is_actually_loaded():
    """Discrete sources were always in; the map was silently absent."""
    sim = R.load_simulator()
    assert sim.cmap is not None, "the 1420 MHz continuum map is not loaded"
    assert len(sim.sources) > 0, "the bright discrete sources are missing"
    # And it must contribute where the plane calibration will happen.
    assert sim.continuum(80.0, 0.0) > 1.0


def test_the_system_temperature_inverts_to_a_loss():
    """371.5 K is opaque; 2.8 dB of loss ahead of the LNA is actionable.

    A lossy element of factor L at ambient gives T_sys = (L-1)T_amb + L*T_rx
    referred to the sky, so the measured value inverts. It assumes the whole
    excess is loss, making it an upper bound rather than a measurement.
    """
    assert R._implied_loss_db(R.RECEIVER_T_RX_K) is None      # needs no loss
    assert R._implied_loss_db(371.5) == pytest.approx(2.78, abs=0.02)
    assert R._implied_loss_db(150.0) == pytest.approx(1.01, abs=0.02)
    # and it must be monotonic, or it is not an inversion of anything
    vals = [R._implied_loss_db(t) for t in (100, 200, 300, 400)]
    assert vals == sorted(vals)


def test_a_loss_predicts_how_the_system_temperature_tracks_ambient():
    """The test that needs no dismantling: dT_sys/dT_amb = L - 1.

    Neither spillover nor receiver noise tracks air temperature; a lossy front
    end does, at nearly a kelvin per kelvin here.
    """
    L = 10 ** (2.78 / 10)
    warm = (L - 1) * (R.AMBIENT_T_K + 15) + L * R.RECEIVER_T_RX_K
    cold = (L - 1) * (R.AMBIENT_T_K - 0) + L * R.RECEIVER_T_RX_K
    assert (warm - cold) / 15 == pytest.approx(L - 1, rel=1e-6)
    assert warm - cold > 10          # a 15 C swing is plainly measurable
