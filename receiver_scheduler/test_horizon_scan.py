#!/usr/bin/env python3
"""Tests for horizon_scan.py — edge fitting, estimators and derived floors."""

import math
import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import horizon_scan as hs
except ImportError:                                  # pragma: no cover
    pytest.skip("Could not import horizon_scan", allow_module_level=True)


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


# ---------------------------------------------------------------------------
# Strip scan
# ---------------------------------------------------------------------------

def test_every_azimuth_is_measured_at_every_altitude():
    """Nothing is dropped, so nothing has to be right while the dish is moving.

    The scan used to peel: an azimuth that cleared was not visited again, which
    made the work shrink but meant the clearing decision had to be correct at
    the time it was taken. On 2026-08-24 it was not - the threshold was scaled
    by the repeat noise of the sky reference rather than by how much clear sky
    varies between azimuths - and a night's observing produced a profile saying
    everything was blocked, with no way to re-decide because the powers were
    never kept.
    """
    profile = hs.horizon_strip_scan(sdr_type="demo", settle_s=0.0, alt_max=60.0)

    counts = [s["n_measured"] for s in profile["strips"]]
    assert len(set(counts)) == 1, "every strip must measure the same azimuths"
    assert counts[0] == profile["n_azimuths"]
    for entry in profile["entries"]:
        assert len(entry["cut_power"]) == len(profile["strips"]), \
            "azimuth %s has a hole in its column" % entry["az_deg"]


def test_every_power_is_recorded():
    """A reading exists nowhere else - not in the log, not in the callback."""
    profile = hs.horizon_strip_scan(sdr_type="demo", settle_s=0.0, alt_max=60.0)

    for entry in profile["entries"]:
        assert entry["cut_power"], entry["az_deg"]
        assert len(entry["cut_alt_deg"]) == len(entry["cut_power"])
        assert all(p == p for p in entry["cut_power"])     # no NaN
    # And per strip, so a strip can be inspected without transposing.
    for strip in profile["strips"]:
        assert strip["powers"], strip["alt_deg"]
        assert len(strip["powers"]) == strip["n_measured"]


def test_the_clear_sky_level_comes_from_the_strip_itself():
    """With nothing dropped, the strip's own distribution is a valid reference.

    This is what peeling made impossible: once cleared azimuths stopped being
    revisited, everything left in the top strips was blocked, so a percentile
    of the strip sat at a blocked level and would have cleared the tower along
    with the sky. That is why the old scan had to carry control azimuths up
    from below. Measuring everything removes the need - the clear azimuths are
    present in every strip, at the right airmass, with nothing to go stale.
    """
    profile = hs.horizon_strip_scan(sdr_type="demo", settle_s=0.0, alt_max=60.0)

    for strip in profile["strips"]:
        assert "clear_level" in strip and "threshold" in strip
        assert strip["threshold"] > strip["clear_level"]
    assert profile["clearance_rule"]["tolerance"] > 0


def test_the_decision_can_be_taken_again_without_reobserving():
    """The whole point: a wrong threshold costs a re-analysis, not a night."""
    profile = hs.horizon_strip_scan(sdr_type="demo", settle_s=0.0, alt_max=60.0)
    powers_before = [list(e["cut_power"]) for e in profile["entries"]]

    strict = hs.derive_clearance(json.loads(json.dumps(profile)), tolerance=0.0001)
    loose = hs.derive_clearance(json.loads(json.dumps(profile)), tolerance=0.5)

    strict_edges = [e["fit"]["edge_reported_deg"] for e in strict["entries"]]
    loose_edges = [e["fit"]["edge_reported_deg"] for e in loose["entries"]]
    assert strict_edges != loose_edges, "the rule must actually bite"
    assert all(a >= b for a, b in zip(strict_edges, loose_edges)), \
        "a stricter tolerance can only push the horizon up"
    # and re-deciding must never touch the measurements
    assert [list(e["cut_power"]) for e in profile["entries"]] == powers_before


def test_an_azimuth_blocked_to_the_ceiling_says_so():
    """Never silently report the ceiling as if it were a measured clearance."""
    profile = hs.horizon_strip_scan(sdr_type="demo", settle_s=0.0,
                                    alt_start=5.0, alt_max=10.0)
    blocked = [e for e in profile["entries"]
               if e["fit"]["estimator"] == "blocked_above_ceiling"]
    assert blocked, "the demo tower stands well above a 10 degree ceiling"
    for entry in blocked:
        assert entry["fit"]["limited_by_ceiling"] is True
        assert "ceiling" in entry["fit"]["quality"]
    # `complete` now means the strips were all measured, which they were. It
    # used to mean "nothing left pending", which conflated finishing the
    # observing with finding the horizon.
    assert profile["complete"] is True


def test_a_strip_profile_still_drives_the_floor_and_the_landscape(tmp_path):
    """The traversal changed; the things that consume a profile did not."""
    profile = hs.horizon_strip_scan(sdr_type="demo", settle_s=0.0, alt_max=60.0)

    floors = hs.profile_floors(profile)
    assert len(floors) == profile["n_azimuths"]
    assert hs.horizon_floor(profile, 180.0) > 0
    folder = hs.write_stellarium_landscape(profile, str(tmp_path / "l"))
    assert os.path.isfile(os.path.join(folder, "landscape.ini"))


def test_the_mount_is_homed_between_strips():
    """Bounds any lost counts to a couple of strips instead of a whole sweep."""
    homed = []
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            hs, "_home_and_wait", side_effect=lambda *a, **k: homed.append(1)):
        # demo mode deliberately skips homing, so drive the real path
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
                hs, "_measure_at",
                side_effect=lambda base, alt, az, *a, **k: {
                    "true_alt": alt, "true_az": az, "drive_alt": alt,
                    "drive_az": az, "power": hs._demo_power(alt, az, 5.8)}):
            hs.horizon_strip_scan(sdr_type="b210", srt_url="http://ctrl",
                                  settle_s=0.0, alt_max=30.0,
                                  home_every_strips=2)
    assert homed, "the mount should have been homed at least once"
