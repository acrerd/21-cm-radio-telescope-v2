#!/usr/bin/env python3
"""Tests for horizon_scan.py — edge fitting, estimators and derived floors."""

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import horizon_scan as hs
except ImportError:                                  # pragma: no cover
    pytest.skip("Could not import horizon_scan", allow_module_level=True)


def _cut(edge, beam_fwhm=5.8, lo=2.0, hi=40.0, step=1.0, contrast=1.6,
         p_sky=1.0, noise=0.0, seed=1):
    alts = np.arange(lo, hi + step / 2, step)
    sigma = beam_fwhm * hs._FWHM_TO_SIGMA
    p = hs.horizon_step(alts, p_sky, contrast, edge, sigma)
    if noise:
        p = p + np.random.default_rng(seed).normal(0, noise, len(p))
    return alts, p


def test_edge_is_the_half_power_point_regardless_of_beam_width():
    """The 50% crossing is the geometric edge by the symmetry of the convolution.

    This is the whole reason for fitting a step rather than thresholding: the
    answer must not depend on how wide the beam is, and the fit measures the
    width independently as a check.
    """
    for beam in (3.0, 5.8, 9.0):
        alts, p = _cut(edge=15.0, beam_fwhm=beam)
        fit = hs.fit_horizon_edge(alts, p, beam_fwhm_deg=beam)
        assert fit["success"] is True
        assert fit["estimator"] == "edge_fit"
        assert fit["edge_deg"] == pytest.approx(15.0, abs=0.05)
        assert fit["width_fwhm_deg"] == pytest.approx(beam, rel=0.05)


def test_edge_survives_realistic_noise():
    alts, p = _cut(edge=12.0, noise=0.01)
    fit = hs.fit_horizon_edge(alts, p)
    assert fit["edge_deg"] == pytest.approx(12.0, abs=0.2)


def test_clearance_sits_above_the_edge_by_the_measured_skirt():
    """The margin is read off the fitted curve, not assumed as a beam multiple."""
    alts, p = _cut(edge=10.0)
    fit = hs.fit_horizon_edge(alts, p, clearance_fraction=0.01)
    # erfcinv(0.02) * sqrt(2) * sigma for a 5.8 deg beam
    sigma = 5.8 * hs._FWHM_TO_SIGMA
    expected = 10.0 + sigma * math.sqrt(2) * float(hs.erfcinv(0.02))
    assert fit["alt_clear_fit"] == pytest.approx(expected, abs=0.1)
    assert fit["alt_clear"] > fit["edge_deg"]
    # A tighter requirement must push the clearance higher.
    strict = hs.fit_horizon_edge(alts, p, clearance_fraction=0.001)
    assert strict["alt_clear_fit"] > fit["alt_clear_fit"]


def test_a_cut_with_no_horizon_is_reported_as_such():
    """Open sky all the way down is not an edge at any altitude."""
    alts = np.arange(2.0, 40.0, 1.0)
    p = np.full_like(alts, 1.0) + np.random.default_rng(0).normal(0, 0.002, len(alts))
    fit = hs.fit_horizon_edge(alts, p)
    assert fit["success"] is False
    assert fit["estimator"] == "none"
    assert "no horizon" in fit["quality"]


def test_two_obstructions_at_once_fall_back_to_the_envelope():
    """One edge is a step; two are not.

    A roofline with the dome tower standing behind it gives a double step, and
    a single erfc fitted to that lands somewhere between the two with
    respectable-looking parameters. The model has to be able to reject itself,
    and the envelope - simply the highest altitude still showing ground - makes
    no assumption about the shape at all.
    """
    alts = np.arange(2.0, 40.0, 1.0)
    p = np.where(alts < 8.0, 2.6, np.where(alts < 22.0, 1.8, 1.0))
    fit = hs.fit_horizon_edge(alts, p)
    assert fit["estimator"] == "envelope"
    assert fit["success"] is True
    assert fit["alt_clear"] == fit["alt_clear_measured"]
    # The envelope must sit at the top of the taller obstruction, not between
    # the two, which is exactly where the single-step fit would put it.
    assert fit["alt_clear"] >= 21.0
    assert fit["edge_deg"] < fit["alt_clear"]


def test_sky_below_an_obstruction_is_not_treated_as_a_horizon():
    """No real horizon has sky underneath it.

    A cut whose lowest points are cold is a transient - an aircraft, a
    satellite, a receiver glitch - not the skyline, and must not be recorded as
    an edge at whatever altitude the anomaly happened to sit.
    """
    alts = np.arange(2.0, 40.0, 1.0)
    p = 1.0 + 1.6 * ((alts > 10) & (alts < 20))
    fit = hs.fit_horizon_edge(alts, p)
    assert fit["success"] is False
    assert fit["estimator"] == "none"


def test_floor_takes_the_worse_of_two_bracketing_azimuths():
    """Between samples, assume the worse neighbour.

    An obstruction narrower than the sampling is far more likely to be missed
    than to be double-counted, so interpolating would systematically
    under-report exactly the structures hardest to see.
    """
    profile = {"az_step_deg": 5.0, "entries": [
        {"az_deg": 100.0, "fit": {"success": True, "alt_clear": 12.0,
                                  "estimator": "edge_fit"}},
        {"az_deg": 105.0, "fit": {"success": True, "alt_clear": 30.0,
                                  "estimator": "edge_fit"}},
    ]}
    assert hs.horizon_floor(profile, 102.0) == 30.0
    assert hs.horizon_floor(profile, 100.0) == 12.0
    assert hs.horizon_floor(profile, 105.0) == 30.0
    assert hs.horizon_floor(profile, 102.0, margin_deg=2.0) == 32.0
    assert hs.is_obstructed(profile, 20.0, 102.0) is True
    assert hs.is_obstructed(profile, 20.0, 100.0) is False


def test_failed_cuts_are_left_out_of_the_floor():
    profile = {"az_step_deg": 5.0, "entries": [
        {"az_deg": 100.0, "fit": {"success": True, "alt_clear": 12.0}},
        {"az_deg": 105.0, "fit": {"success": False, "alt_clear": None}},
    ]}
    assert hs.profile_floors(profile) == [(100.0, 12.0)]


def test_direction_bias_matches_neighbours_by_proximity():
    """Real azimuths are never exactly one step apart.

    The recorded azimuth is where the mount actually pointed, so an equality
    test finds nothing on real data while passing in simulation.
    """
    entries = []
    for i, az in enumerate(np.arange(100.0, 141.0, 5.0)):
        up = i % 2 == 1
        entries.append({
            "az_deg": az + 0.13 * (i % 3 - 1),      # jitter, as a real mount has
            "direction": "up" if up else "down",
            "fit": {"success": True, "estimator": "edge_fit",
                    "edge_reported_deg": 10.0 + (0.4 if up else 0.0)},
        })
    bias = hs.direction_bias({"az_step_deg": 5.0, "entries": entries})
    assert bias["available"] is True
    assert bias["up_minus_down_deg"] == pytest.approx(0.4, abs=0.05)


def test_serpentine_cuts_alternate_direction():
    down = hs._cut_altitudes(5.0, 15.0, 1.0, descending=True)
    up = hs._cut_altitudes(5.0, 15.0, 1.0, descending=False)
    assert down[0] > down[-1]
    assert up[0] < up[-1]
    assert sorted(down) == pytest.approx(sorted(up))


def test_demo_scan_recovers_the_synthetic_horizon():
    """End to end with no hardware: the scan must find the horizon it was given."""
    profile = hs.horizon_scan(az_start=50.0, az_end=150.0, sdr_type="demo")

    assert profile["success"] is True
    assert profile["record_version"] == hs._HORIZON_RECORD_VERSION
    errors = []
    for entry in profile["entries"]:
        fit = entry["fit"]
        assert fit["success"] is True
        errors.append(fit["edge_reported_deg"] - hs._demo_edge(entry["az_deg"]))
    assert np.max(np.abs(errors)) < 0.5
    # The raw cut is stored, not just the conclusion.
    assert len(profile["entries"][0]["cut_power"]) >= 5
    assert len(profile["entries"][0]["cut_alt_deg"]) == \
        len(profile["entries"][0]["cut_power"])


def test_scan_widens_the_window_when_the_horizon_jumps():
    """The tracking window is an optimisation, and must not become the answer."""
    profile = hs.horizon_scan(az_start=55.0, az_end=70.0, sdr_type="demo",
                              initial_edge_guess=5.0, window_deg=3.0)
    # az 60 is where the demo treeline steps from 5 to 16 degrees, well outside
    # a 3 degree window around the previous answer.
    jumped = [e for e in profile["entries"] if abs(e["az_deg"] - 60.0) < 0.1][0]
    assert jumped["attempts"] > 1
    assert jumped["fit"]["edge_reported_deg"] == pytest.approx(16.0, abs=0.5)


def test_a_tall_obstruction_is_given_headroom_for_its_clearance():
    """Finding the edge is not the same as measuring the skirt above it.

    The demo tower stands at 34 degrees against a default ceiling of 40, so the
    edge fits comfortably but the clearance - which is read off the curve above
    the edge - would otherwise be extrapolated past the last measured point.
    """
    profile = hs.horizon_scan(az_start=5.0, az_end=15.0, sdr_type="demo",
                              initial_edge_guess=34.0)
    for entry in profile["entries"]:
        fit = entry["fit"]
        assert fit["edge_reported_deg"] == pytest.approx(34.0, abs=0.5)
        # The cut must reach above the clearance, not stop at it.
        assert max(entry["cut_alt_deg"]) > fit["alt_clear"]


def test_a_cut_cannot_retry_for_ever():
    """One pathological azimuth must not eat the whole night."""
    assert hs._MAX_CUT_ATTEMPTS >= 2
    profile = hs.horizon_scan(az_start=100.0, az_end=110.0, sdr_type="demo")
    assert all(e["attempts"] <= hs._MAX_CUT_ATTEMPTS for e in profile["entries"])


def test_a_retry_does_not_re_measure_what_it_already_has(monkeypatch):
    """Widening a cut must cost only the altitudes it adds.

    The sky does not change between attempts. Chasing the dome tower's
    clearance up to 48 degrees re-measured nearly forty points of solid tower
    that had already been measured, turning an eighty-second azimuth into four
    and a half minutes.
    """
    calls = []
    real = hs._measure_at

    def counting(base_url, alt, az, *args, **kwargs):
        calls.append(round(float(alt), 1))
        return real(base_url, alt, az, *args, **kwargs)

    monkeypatch.setattr(hs, "_measure_at", counting)
    # az 5 is the demo tower at 34 degrees, so a window around 10 must widen.
    profile = hs.horizon_scan(az_start=5.0, az_end=5.0, sdr_type="demo",
                              initial_edge_guess=10.0, window_deg=6.0)

    entry = profile["entries"][0]
    assert entry["attempts"] > 1, "this azimuth should have needed a retry"
    assert entry["points_reused"] > 0
    # Every altitude was visited at most once, however many attempts it took.
    assert len(calls) == len(set(calls))
    assert len(calls) == entry["points_measured"]


def test_one_hot_sample_cannot_pin_the_envelope_to_the_ceiling():
    """A lone sample above threshold is noise, not an obstruction.

    Seen on 2026-08-21: azimuths 180-195 all reported a clearance of ~40 deg,
    which was the top of the sampled range, from cuts whose real horizon was
    around 8. A real obstruction fills the beam and shows in its neighbours.
    """
    alts, p = _cut(edge=8.0, lo=2.0, hi=40.0, step=1.0)
    p = p.copy()
    p[-1] += 0.5 * (p[0] - p[-1])          # one hot sample at the very top
    fit = hs.fit_horizon_edge(alts, p)
    assert fit["alt_clear_measured"] < 30.0
    assert fit["edge_deg"] == pytest.approx(8.0, abs=0.3)


def test_clearance_is_not_claimed_below_the_noise_of_the_cut():
    """p_sky is a median, so half the sky samples sit above it by construction.

    With a threshold only 1% of the step above that median, any sky scatter
    larger than radiometric - gain drift over a three-minute cut, or RFI -
    puts sky samples over the line and the envelope runs away upwards.
    """
    alts, p = _cut(edge=8.0, lo=2.0, hi=40.0, step=1.0, contrast=1.6, noise=0.0)
    rng = np.random.default_rng(3)
    drifty = p + rng.normal(0, 0.05, len(p))   # scatter >> 1% of the step
    fit = hs.fit_horizon_edge(alts, drifty)
    assert fit["threshold"] > fit["p_sky"] + 0.01 * fit["contrast_fraction"]
    assert fit["sky_sigma"] > 0
    assert fit["alt_clear_measured"] < 30.0


def test_an_envelope_does_not_move_the_tracking_window():
    """The envelope is an upper bound, not an estimate of where the edge is.

    Feeding it back as the next window centre ratchets: the window opens above
    the horizon, sees only sky, fails, widens, envelopes near the ceiling, and
    re-arms. Three consecutive azimuths were lost to this on 2026-08-21.
    """
    import inspect
    source = inspect.getsource(hs.horizon_scan)
    assert 'fit.get("estimator") == "edge_fit"' in source, \
        "the tracking guess must only follow a fitted edge"


def test_a_profile_can_be_re_derived_from_its_stored_cuts():
    """The point of storing raw cuts: fix the estimator, not the observation."""
    profile = hs.horizon_scan(az_start=60.0, az_end=80.0, sdr_type="demo")
    # Corrupt the derived answers, leaving the measurements untouched.
    for entry in profile["entries"]:
        entry["fit"] = {"success": True, "estimator": "envelope",
                        "edge_reported_deg": 40.0, "alt_clear": 40.0}

    recovered = hs.reprocess_profile(profile)

    for entry in recovered["entries"]:
        fit = entry["fit"]
        assert fit["estimator"] == "edge_fit"
        assert fit["edge_reported_deg"] == pytest.approx(
            hs._demo_edge(entry["az_deg"]), abs=0.5)
        # The wrong answer is kept alongside, not silently discarded.
        assert entry["fit_as_measured"]["alt_clear"] == 40.0
    assert "reprocessed_utc" in recovered


def test_reprocessing_can_change_the_clearance_fraction():
    profile = hs.horizon_scan(az_start=100.0, az_end=110.0, sdr_type="demo")
    strict = hs.reprocess_profile(profile, clearance_fraction=0.001)
    for before, after in zip(profile["entries"], strict["entries"]):
        assert after["fit"]["alt_clear_fit"] > before["fit"]["alt_clear_fit"]


def test_stellarium_horizon_starts_at_north():
    """Stellarium's polygonal format requires the list to begin at azimuth 0.

    The scan runs 5 to 350 degrees - the mount cannot reach past its azimuth
    limits - so the value at due north is interpolated across that gap rather
    than left missing, which would make Stellarium close the polygon wrongly.
    """
    profile = {"clearance_fraction": 0.01, "entries": [
        {"az_deg": 350.0, "fit": {"success": True, "alt_clear": 20.0,
                                  "edge_reported_deg": 14.0}},
        {"az_deg": 5.0, "fit": {"success": True, "alt_clear": 30.0,
                                "edge_reported_deg": 24.0}},
        {"az_deg": 180.0, "fit": {"success": True, "alt_clear": 8.0,
                                  "edge_reported_deg": 4.0}},
    ]}
    points = hs.stellarium_horizon_points(profile)

    assert points[0][0] == 0.0
    assert [az for az, _ in points] == sorted(az for az, _ in points)
    # North sits between its two neighbours, 10/15 of the way from 350 to 5.
    assert points[0][1] == pytest.approx(20.0 + (10.0 / 15.0) * 10.0, abs=0.01)


def test_stellarium_landscape_can_draw_either_quantity(tmp_path):
    profile = {"clearance_fraction": 0.01, "site_lat": 55.9, "site_lon": -4.3,
               "entries": [
                   {"az_deg": float(a), "fit": {"success": True,
                                                "alt_clear": 20.0,
                                                "edge_reported_deg": 12.0}}
                   for a in (5, 90, 180, 270, 350)]}

    clean = hs.write_stellarium_landscape(profile, str(tmp_path / "clean"),
                                          use="clearance")
    sky = hs.write_stellarium_landscape(profile, str(tmp_path / "sky"), use="edge")

    ini = open(os.path.join(clean, "landscape.ini")).read()
    assert "type = polygonal" in ini
    assert "polygonal_horizon_list = horizon_acreroad_clearance.txt" in ini
    # The site position travels with the landscape.
    assert "+55.900000" in ini
    horizon = open(os.path.join(clean, "horizon_acreroad_clearance.txt")).read()
    assert "20.00" in horizon and "12.00" not in horizon
    skyline = open(os.path.join(sky, "horizon_acreroad_skyline.txt")).read()
    assert "12.00" in skyline


def test_stellarium_landscape_zips_with_its_folder(tmp_path):
    """Stellarium installs a landscape from a zip containing one folder."""
    import zipfile
    profile = {"clearance_fraction": 0.01, "entries": [
        {"az_deg": float(a), "fit": {"success": True, "alt_clear": 10.0,
                                     "edge_reported_deg": 5.0}}
        for a in (5, 120, 240, 350)]}

    path = hs.zip_stellarium_landscape(profile, str(tmp_path / "l.zip"))
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    assert any(n.endswith("acreroad_clearance/landscape.ini") for n in names)
    assert any(n.endswith(".txt") for n in names)


def test_a_profile_with_nothing_usable_will_not_make_a_landscape(tmp_path):
    with pytest.raises(ValueError, match="no usable azimuths"):
        hs.write_stellarium_landscape({"entries": [
            {"az_deg": 10.0, "fit": {"success": False}}]}, str(tmp_path))


def test_an_open_azimuth_is_a_result_not_a_failure():
    """The observatory is on a hill; some azimuths have no horizon in reach.

    A cut that bottoms out at the mount limit with no step means the skyline is
    below anything the telescope can point at. Reporting that as a failed
    measurement is worse than useless: a dropped azimuth has its floor
    interpolated from its neighbours, lending their trees to the one direction
    that is actually clear.
    """
    alts = np.arange(2.0, 40.0, 1.0)
    p = 1.0 + np.random.default_rng(0).normal(0, 0.002, len(alts))   # all sky

    open_sky = hs.fit_horizon_edge(alts, p, reached_mount_limit=True)
    assert open_sky["success"] is True
    assert open_sky["estimator"] == "unobstructed"
    assert open_sky["alt_clear"] == pytest.approx(2.0)
    assert open_sky["limited_by_mount"] is True

    # Without having reached the limit, the same cut proves nothing: the
    # horizon may simply be below where we looked.
    inconclusive = hs.fit_horizon_edge(alts, p, reached_mount_limit=False)
    assert inconclusive["success"] is False
    assert inconclusive["estimator"] == "none"


def test_an_open_azimuth_reaches_the_derived_floor():
    """It must appear in the profile, or its neighbours will speak for it."""
    profile = {"az_step_deg": 5.0, "entries": [
        {"az_deg": 175.0, "fit": {"success": True, "alt_clear": 25.0,
                                  "estimator": "edge_fit"}},
        {"az_deg": 180.0, "fit": {"success": True, "alt_clear": 2.0,
                                  "estimator": "unobstructed"}},
        {"az_deg": 185.0, "fit": {"success": True, "alt_clear": 24.0,
                                  "estimator": "edge_fit"}},
    ]}
    assert (180.0, 2.0) in hs.profile_floors(profile)
    # Exactly on the open azimuth the floor is the mount limit ...
    assert hs.horizon_floor(profile, 180.0) == 2.0
    # ... while between samples the conservative rule still applies.
    assert hs.horizon_floor(profile, 177.5) == 25.0


def test_reprocessing_knows_where_the_cut_floor_was():
    """The floor used at scan time is recorded, so reprocessing can judge it."""
    profile = hs.horizon_scan(az_start=100.0, az_end=105.0, sdr_type="demo")
    assert profile["alt_min_deg"] == hs.DEFAULT_ALT_MIN
    recovered = hs.reprocess_profile(profile)
    assert len(recovered["entries"]) == len(profile["entries"])


def test_an_open_azimuth_stops_retrying_immediately():
    """An open azimuth is a conclusion, not a reason to keep looking higher.

    Treating "no step" as "must be all ground" and chasing the ceiling turned
    open azimuths into twenty-five minute cuts on 2026-08-21. The observatory
    is on a hill, so there are a lot of open azimuths, and a full sweep would
    never have finished.
    """
    calls = []
    real = hs._measure_at

    def counting(base_url, alt, az, *args, **kwargs):
        calls.append(float(alt))
        return real(base_url, alt, az, *args, **kwargs)

    # A synthetic horizon well below the cut floor: nothing to find anywhere.
    original = hs._DEMO_HORIZON, hs._DEMO_BASE_EDGE
    try:
        hs._DEMO_HORIZON = []
        hs._DEMO_BASE_EDGE = -5.0        # below anything the mount can reach
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
                hs, "_measure_at", counting):
            profile = hs.horizon_scan(az_start=100.0, az_end=105.0,
                                      sdr_type="demo", initial_edge_guess=10.0)
    finally:
        hs._DEMO_HORIZON, hs._DEMO_BASE_EDGE = original

    for entry in profile["entries"]:
        assert entry["attempts"] <= 2, "an open azimuth must not be retried"
    # Two azimuths, each a window plus one widened cut - not four ceiling chases.
    assert len(calls) < 120


def test_all_ground_is_not_mistaken_for_open_sky():
    """Both are stepless cuts; only the sky level tells them apart.

    Without the reference, a building filling the whole cut reads exactly like
    a clear horizon - and would be recorded as the most open azimuth on site.
    """
    alts = np.arange(2.0, 40.0, 1.0)
    ground = np.full_like(alts, 2.6)          # solid ground, no step
    sky_level = 1.0

    verdict = hs.fit_horizon_edge(alts, ground, reached_mount_limit=True,
                                  sky_reference=sky_level)
    assert verdict["estimator"] == "all_ground"
    assert verdict["success"] is False

    open_sky = hs.fit_horizon_edge(alts, np.full_like(alts, 1.02),
                                   reached_mount_limit=True,
                                   sky_reference=sky_level)
    assert open_sky["estimator"] == "unobstructed"


def test_an_abandoned_scan_keeps_what_it_measured(monkeypatch):
    """Ninety minutes of good cuts must survive a cancelled run."""
    import threading
    cancel = threading.Event()
    seen = {"n": 0}
    real = hs._measure_at

    def cancel_partway(*args, **kwargs):
        seen["n"] += 1
        if seen["n"] > 40:
            cancel.set()
        return real(*args, **kwargs)

    monkeypatch.setattr(hs, "_measure_at", cancel_partway)
    with pytest.raises(Exception) as caught:
        hs.horizon_scan(az_start=100.0, az_end=140.0, sdr_type="demo",
                        cancel_event=cancel)

    partial = getattr(caught.value, "partial_profile", None)
    assert partial is not None
    assert partial["complete"] is False
    assert len(partial["entries"]) >= 1
    assert partial["entries"][0]["cut_power"]


def test_a_run_of_open_azimuths_does_not_retry_each_one():
    """After an open azimuth, the next cut goes straight to the full range.

    The tracking window is centred on the last measured *edge*, which is
    meaningless once the horizon has dropped out of reach - so it fails and
    widens, wasting a whole cut at every open azimuth in the run.
    """
    original = hs._DEMO_HORIZON
    calls = []
    real = hs._measure_at

    def counting(base_url, alt, az, *args, **kwargs):
        calls.append((round(float(az), 1), round(float(alt), 1)))
        return real(base_url, alt, az, *args, **kwargs)

    saved_base = hs._DEMO_BASE_EDGE
    try:
        hs._DEMO_HORIZON = []                     # nothing anywhere: all open
        hs._DEMO_BASE_EDGE = -5.0                 # and below the mount's reach
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
                hs, "_measure_at", counting):
            profile = hs.horizon_scan(az_start=100.0, az_end=115.0,
                                      sdr_type="demo", initial_edge_guess=25.0)
    finally:
        hs._DEMO_HORIZON = original
        hs._DEMO_BASE_EDGE = saved_base

    estimators = [e["fit"]["estimator"] for e in profile["entries"]]
    assert estimators[0] == "unobstructed"
    # Only the first azimuth pays for discovering the horizon is out of reach.
    assert all(e["attempts"] == 1 for e in profile["entries"][1:])
