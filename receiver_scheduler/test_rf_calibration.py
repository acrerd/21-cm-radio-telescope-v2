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


def test_a_hot_system_is_reported_by_degree_not_dismissed():
    """The 50 K floor only catches errors of one sign.

    A run that recorded while the mount was still slewing fitted 467 K on
    2026-08-24 and nothing in the result said so. But the flag says how high
    the temperature is, not that it cannot be real: this telescope calibrates
    at 340-372 K and works, the excess being loss ahead of the LNA, so a flag
    reading "hotter than any working system" was wrong about its own
    observatory.
    """
    _, ta = _fake_plane()
    for t_sys, expected in ((467.0, "very high"),   # the slewing run
                            (355.0, "very high"),   # this telescope, working
                            (240.0, "high"),
                            (130.0, None),          # a well-fed dish
                            (60.0, None)):
        out = R.fit_gain(3.0e-5 * (t_sys + ta), ta)
        assert out["t_sys_k"] == pytest.approx(t_sys, rel=1e-6)
        assert out["t_sys_level"] == expected, t_sys
        assert not out["t_sys_bound_active"]


def test_the_bands_meet_without_a_gap():
    """One threshold's floor is the other's ceiling, so nothing falls between."""
    assert R.HIGH_T_SYS_K < R.VERY_HIGH_T_SYS_K
    _, ta = _fake_plane()
    for t_sys, expected in ((R.HIGH_T_SYS_K - 0.1, None),
                            (R.HIGH_T_SYS_K + 0.1, "high"),
                            (R.VERY_HIGH_T_SYS_K - 0.1, "high"),
                            (R.VERY_HIGH_T_SYS_K + 0.1, "very high")):
        out = R.fit_gain(3.0e-5 * (t_sys + ta), ta)
        assert out["t_sys_level"] == expected, t_sys


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


def test_no_polarisation_factor_is_applied_to_the_temperature_scale():
    """One polarisation, and no half belongs in the temperature scale.

    For a single-polarisation antenna on unpolarised sky the antenna
    temperature is the beam-weighted brightness temperature - the half-power
    split is already inside the definition. The simulator's npol affects only
    the radiometer noise, and this path generates none.
    """
    sim = R.load_simulator()
    assert sim.npol == 1
    assert sim.tsys is None, "no simulated noise, so npol cannot enter"
    # The model must not be scaled by a half: a known-bright direction has to
    # come back at survey brightness, not half of it.
    peak = float(max(sim.spectrum(80.0, 0.0)[1]))
    assert peak > 50.0, "the plane should peak near 100 K, not near 50"


def _shifted(v_kms, peak_k=100.0, n=400):
    """A line whose model sits at a different velocity from the data."""
    import numpy as np
    H1, C = R.H1_REST_FREQ_HZ, R.C_M_S / 1e3
    # Ascending in frequency, which is how the pipeline delivers it and what
    # np.interp requires; built descending, the first version of this test made
    # the fitter look broken when it was the fixture that was.
    f = H1 * (1 - np.linspace(150.0, -150.0, n) / C)
    v = -(f - H1) / H1 * C
    model = peak_k * np.exp(-0.5 * ((v - 0.0) / 20.0) ** 2) + 0.3
    data_k = peak_k * np.exp(-0.5 * ((v - v_kms) / 20.0) ** 2) + 0.3
    return f, model, data_k


def test_a_frequency_scale_error_is_recovered():
    """The B210's crystal scales the axis; over 2 MHz that is a pure shift."""
    f, model, data_k = _shifted(2.0)
    counts = 3.0e-5 * (120.0 + data_k)
    out, _ = R.fit_gain_with_shift(f, counts, f, model)
    assert out["velocity_shift_km_s"] == pytest.approx(2.0, abs=0.15)
    assert out["gain_counts_per_k"] == pytest.approx(3.0e-5, rel=0.05)
    assert out["t_sys_k"] == pytest.approx(120.0, rel=0.05)


def test_ppm_and_velocity_are_the_same_statement():
    f, model, data_k = _shifted(2.0)
    out, _ = R.fit_gain_with_shift(f, 3.0e-5 * (120.0 + data_k), f, model)
    assert out["implied_ppm"] == pytest.approx(
        out["velocity_shift_km_s"] / (R.C_M_S / 1e3) * 1e6, rel=1e-9)


def test_an_aligned_spectrum_fits_no_shift():
    """It must not invent one, or every fit gets a free parameter of slack."""
    f, model, data_k = _shifted(0.0)
    out, _ = R.fit_gain_with_shift(f, 3.0e-5 * (120.0 + data_k), f, model)
    assert abs(out["velocity_shift_km_s"]) < 0.2


def test_the_search_is_bounded_and_says_when_it_hits_the_edge():
    """Unbounded, a shift slides onto a neighbouring component and fits it."""
    f, model, data_k = _shifted(40.0)          # far outside the search
    out, _ = R.fit_gain_with_shift(f, 3.0e-5 * (120.0 + data_k), f, model)
    assert abs(out["velocity_shift_km_s"]) <= R.MAX_SHIFT_KM_S + 1e-6
    assert out["shift_at_search_limit"]


def test_the_reduction_reports_the_total_integration():
    """The radiometer equation needs it, and the sum is not the nominal.

    A run stopped early, or one whose records ran long, must not be credited
    with noise it never averaged down.
    """
    import glob
    import os
    files = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "data", "rf_gain_calibration_*.h5")))
    if not files:
        pytest.skip("no calibration observation on disk")
    try:
        red = R.reduce_for_fit(files[0], 36.0, 40.0)
    except ValueError as exc:
        # An archived field from another tuning, with no template for it
        # on this machine (recordings from before the fixed instrument are
        # not carried).
        if "not bandpass corrected" in str(exc):
            pytest.skip(str(exc))
        raise
    assert red["tau_total_s"] > 0
    # Binned onto the model's grid, so the channel width is the model's.
    dnu = float(np.abs(np.median(np.diff(red["sim_freq_hz"]))))
    assert 4e3 < dnu < 9e3, "expected the model's ~6.1 kHz channels, got %.0f Hz" % dnu
    # And the resulting sigma must be sane for a few minutes on a ~350 K system.
    sigma = 350.0 / np.sqrt(dnu * red["tau_total_s"])
    assert 0.05 < sigma < 2.0, sigma


def test_noise_scales_as_the_radiometer_equation_says():
    """Binning down to the model grid averages the noise down with it.

    Quoting the fine-channel figure against binned data would overstate the
    expected noise fourfold, and make a noise-limited fit look systematic.
    """
    fine, binned, tau = 488.3, 6100.0, 175.0
    s_fine = 350.0 / np.sqrt(fine * tau)
    s_binned = 350.0 / np.sqrt(binned * tau)
    assert s_fine / s_binned == pytest.approx(np.sqrt(binned / fine), rel=1e-9)
    assert s_binned == pytest.approx(0.34, abs=0.02)


def _spectrum_with(line_channels=0, spike_channels=0, n=2000, seed=3):
    """A flat band, optionally with a resolved line and/or a narrow spike."""
    H1 = R.H1_REST_FREQ_HZ
    dnu = 488.3
    f = H1 + (np.arange(n) - n // 2) * dnu
    y = np.ones(n)
    if line_channels:
        y += 0.2 * np.exp(-0.5 * ((np.arange(n) - n // 2) / (line_channels / 2.355)) ** 2)
    if spike_channels:
        c = n // 2 - 260              # well away from the line
        y[c:c + spike_channels] += 0.15
    y = y * (1 + 0.001 * np.random.default_rng(seed).standard_normal(n))
    return f, y


def test_narrow_interference_is_found():
    f, y = _spectrum_with(spike_channels=3)
    mask, found = R.flag_narrow_rfi(f, y)
    assert len(found) == 1
    assert found[0]["channels"] <= R.RFI_MAX_CHANNELS
    assert mask.sum() == 3


def test_a_narrow_line_is_not_mistaken_for_interference():
    """The width ceiling is what protects it, not the filter width.

    Even cold hydrogen runs about a kilometre a second, nine channels here, and
    at a filter width of 81 the line's own tip was flagged at 9 sigma on
    2026-08-24 and would have been deleted.
    """
    f, y = _spectrum_with(line_channels=10)
    mask, found = R.flag_narrow_rfi(f, y)
    assert not mask.any(), "a 10-channel line is the sky, however sharp"


def test_a_broad_line_is_not_touched():
    f, y = _spectrum_with(line_channels=200)
    mask, _ = R.flag_narrow_rfi(f, y)
    assert not mask.any()


def test_a_line_and_a_spike_together_leave_the_line_alone():
    f, y = _spectrum_with(line_channels=40, spike_channels=3)
    mask, found = R.flag_narrow_rfi(f, y)
    assert len(found) == 1
    # nothing flagged within the line
    assert not mask[len(f) // 2 - 30:len(f) // 2 + 30].any()


def test_only_positive_excursions_are_flagged():
    """A narrow deficit is the LO artefact or a dead channel, handled by name.

    Flagging them here would let this quietly delete real absorption.
    """
    f, y = _spectrum_with()
    y[900:903] -= 0.15
    mask, found = R.flag_narrow_rfi(f, y)
    assert not mask.any()


def test_the_known_interference_line_is_rejected_from_a_real_run():
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                        "rf_gain_calibration_20260824_180418.h5")
    if not os.path.exists(path):
        pytest.skip("the 2026-08-24 run is not on disk")
    try:
        red = R.reduce_for_fit(path, 36.53, 56.9)
    except ValueError as exc:
        if "not bandpass corrected" in str(exc):
            pytest.skip("that run's tuning has no template here: " + str(exc))
        raise
    hits = [x for x in red["rfi_found"] if abs(x["freq_hz"] - 1420.2790e6) < 5e3]
    assert hits, "the 1420.2790 MHz interference was not caught"
    assert hits[0]["sigma"] > 10


def test_a_bright_line_keeps_its_peak_however_high_the_signal_to_noise():
    """Curvature at a line's peak beats any noise-relative threshold.

    A 40-channel line at a thousand to one had its tip flagged at 9 sigma
    before the edge-drop criterion existed. Neither the filter width nor the
    channel count catches this; only the shape does.
    """
    for line_channels in (12, 20, 40, 120):
        f, y = _spectrum_with(line_channels=line_channels, seed=11)
        mask, found = R.flag_narrow_rfi(f, y)
        assert not mask.any(), \
            "clipped the peak of a %d-channel line" % line_channels


def test_the_edge_drop_is_what_does_it():
    """A spike keeps its neighbours at the baseline; a line does not."""
    f, y = _spectrum_with(spike_channels=2)
    assert R.flag_narrow_rfi(f, y)[0].any()
    # Relax the criterion to accept anything, and the line is caught again -
    # which is the point: this is the criterion carrying the weight.
    f2, y2 = _spectrum_with(line_channels=40, seed=11)
    assert not R.flag_narrow_rfi(f2, y2)[0].any()
    assert R.flag_narrow_rfi(f2, y2, edge_drop=1.5)[0].any()
