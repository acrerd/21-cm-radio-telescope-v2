"""The horizon archive: many scans kept by date, one of them chosen.

What these pin down is mostly *restraint*. The easy implementation has each
finished scan become the horizon, and it is wrong in a way that only shows up
a season later: the trees get cut back, a scan runs, the horizon silently drops
by 30 degrees, and the profile that recorded the old one is gone. So the tests
below care less about what a scan does than about what it does not do.
"""

import json
import os

import pytest

import observatory  # noqa: F401  - puts astro_simulator on the path
import horizon_store as store


def make_profile(started="2026-08-24T20:40:22.000000+00:00", floors=None,
                 complete=True, sdr_type="b210", n_az=None):
    """A profile with just the fields the store reads."""
    floors = floors if floors is not None else {0.0: 10.0, 90.0: 20.0,
                                                180.0: 5.0, 270.0: 15.0}
    entries = [
        {"az_deg": az,
         "cut_alt_deg": [5.0, 10.0], "cut_power": [1.0, 0.5],
         "fit": {"success": True, "alt_clear": alt,
                 "edge_reported_deg": alt, "estimator": "strip_quantile",
                 "limited_by_ceiling": False}}
        for az, alt in sorted(floors.items())
    ]
    return {"record_version": 3, "pattern": "strips",
            "started_utc": started, "finished_utc": started,
            "duration_s": 4800.0, "az_step_deg": 10, "alt_step_deg": 5,
            "alt_min_deg": 5, "alt_max_deg": 60,
            "n_azimuths": n_az if n_az is not None else len(entries),
            "sdr_type": sdr_type, "complete": complete,
            "strips": [], "sky_references": [], "entries": entries}


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """Point the store at a throwaway archive."""
    monkeypatch.setattr(store, "ARCHIVE_DIR", str(tmp_path / "horizon_profiles"))
    monkeypatch.setattr(store, "ACTIVE_FILE",
                        str(tmp_path / "horizon_profiles" / "active.json"))
    monkeypatch.setattr(store, "LEGACY_FILE", str(tmp_path / "horizon_profile.json"))
    return tmp_path


# ---------------------------------------------------------------------------
# Keeping scans
# ---------------------------------------------------------------------------

def test_a_scan_is_filed_under_the_date_it_started(archive):
    path = store.archive_profile(make_profile())
    assert os.path.basename(path) == "horizon_20260824T204022Z.json"
    assert store.profile_date(make_profile()) == "2026-08-24"


def test_saving_the_same_scan_twice_does_not_make_two_copies(archive):
    """The name comes from the scan, not from the moment of writing.

    A scan saves a partial after every strip and once more at the end. If the
    stamp came from the clock, one night would leave a dozen near-identical
    profiles in the list and the operator would have to guess which was final.
    """
    profile = make_profile()
    store.archive_profile(profile)
    store.archive_profile(dict(profile, complete=True))
    assert len(store.list_profiles()) == 1


def test_two_scans_on_different_nights_are_both_kept(archive):
    store.archive_profile(make_profile("2026-02-01T20:00:00+00:00"))
    store.archive_profile(make_profile("2026-08-24T20:40:22+00:00"))
    names = [p["name"] for p in store.list_profiles()]
    assert len(names) == 2
    assert names[0].startswith("horizon_20260824"), "newest first"


def test_a_profile_name_cannot_escape_the_archive(archive):
    """The name arrives from an HTTP request."""
    store.archive_profile(make_profile())
    for bad in ("../../etc/passwd", "/etc/passwd", "active", ""):
        with pytest.raises((ValueError, FileNotFoundError)):
            store.set_active(bad)


# ---------------------------------------------------------------------------
# Choosing, and not choosing
# ---------------------------------------------------------------------------

def test_archiving_a_scan_does_not_put_it_in_force(archive):
    """The point of the whole exercise.

    Trimming the trees genuinely opens the sky, so a fresh scan will read lower
    than the one before it. If finishing a scan were enough to adopt it, the
    horizon would drop without anyone having agreed that the new measurement is
    the better record - and, before this, the old one was overwritten in the
    same stroke, so there was nothing to compare against afterwards.
    """
    store.archive_profile(make_profile("2026-02-01T20:00:00+00:00",
                                       floors={0.0: 40.0}))
    store.set_active("horizon_20260201T200000Z")

    store.archive_profile(make_profile("2026-08-24T20:40:22+00:00",
                                       floors={0.0: 5.0}))

    assert store.active_name() == "horizon_20260201T200000Z"
    assert store.horizon_floor(store.load_active(), 0.0) == 40.0


def test_choosing_moves_the_horizon_and_the_legacy_mirror(archive):
    store.archive_profile(make_profile("2026-02-01T20:00:00+00:00", floors={0.0: 40.0}))
    store.archive_profile(make_profile("2026-08-24T20:40:22+00:00", floors={0.0: 5.0}))
    store.set_active("horizon_20260201T200000Z")

    store.set_active("horizon_20260824T204022Z", note="trees cut back in June")

    assert store.horizon_floor(store.load_active(), 0.0) == 5.0
    assert store.active_record()["note"] == "trees cut back in June"
    # Everything that still opens the old single-file path follows the choice
    # without knowing the archive exists.
    with open(store.LEGACY_FILE) as f:
        assert store.horizon_floor(json.load(f), 0.0) == 5.0


def test_with_nothing_chosen_the_newest_complete_real_scan_is_used(archive):
    store.archive_profile(make_profile("2026-02-01T20:00:00+00:00", floors={0.0: 40.0}))
    store.archive_profile(make_profile("2026-08-24T20:00:00+00:00", floors={0.0: 5.0},
                                       complete=False))
    store.archive_profile(make_profile("2026-08-25T20:00:00+00:00", floors={0.0: 1.0},
                                       sdr_type="demo"))

    # Not the partial, and not the simulated one.
    assert store.horizon_floor(store.load_active(), 0.0) == 40.0
    assert store.active_name() is None, "a fallback is not a choice"


def test_a_simulated_scan_is_labelled_wherever_it_can_be_chosen(archive):
    store.archive_profile(make_profile(sdr_type="demo"))
    assert store.list_profiles()[0]["is_demo"] is True


def test_the_legacy_file_is_brought_into_the_archive_once(archive):
    """Last night's scan predates the archive and must not be stranded."""
    profile = make_profile()
    os.makedirs(os.path.dirname(store.LEGACY_FILE), exist_ok=True)
    with open(store.LEGACY_FILE, "w") as f:
        json.dump(profile, f)

    listed = store.list_profiles()
    assert [p["name"] for p in listed] == ["horizon_20260824T204022Z"]
    assert store.list_profiles() == listed, "migrating twice must not duplicate"


# ---------------------------------------------------------------------------
# Comparing scans
# ---------------------------------------------------------------------------

def test_the_summary_says_how_open_each_horizon_is(archive):
    """How an operator tells a pruning from a bad scan."""
    summer = store.summarise(make_profile(floors={0.0: 40.0, 90.0: 30.0,
                                                  180.0: 20.0, 270.0: 30.0}))
    winter = store.summarise(make_profile(floors={0.0: 10.0, 90.0: 10.0,
                                                  180.0: 5.0, 270.0: 10.0}))
    assert summer["floors"]["median_deg"] == 30.0
    assert winter["floors"]["median_deg"] == 10.0
    assert summer["floors"]["max_deg"] == 40.0
    assert winter["floors"]["visible_sq_deg"] > summer["floors"]["visible_sq_deg"]


@pytest.mark.parametrize("floor_deg", [0.0, 10.0, 30.0, 45.0, 60.0, 90.0])
def test_visible_sky_matches_the_analytic_solid_angle(archive, floor_deg):
    """A horizon flat at h leaves (1 - sin h) of the hemisphere.

    Worth pinning against the closed form rather than against a previous run,
    because the cos(alt) weighting is the whole content of the number and it is
    easy to write an integral that quietly reports the fraction of *azimuths*
    clear instead - which would be the median floor wearing a different unit.
    """
    import math

    profile = make_profile(floors={az: floor_deg for az in (0.0, 90.0, 180.0, 270.0)})
    expected = (1.0 - math.sin(math.radians(floor_deg))) * store.HEMISPHERE_SQ_DEG
    assert store.visible_sky_sq_deg(profile) == pytest.approx(expected, abs=1.0)


def test_a_tall_obstruction_costs_more_sky_than_a_wide_low_one(archive):
    """Why square degrees replaced the median floor in the chooser.

    Both horizons below block the same number of azimuths, and the one with the
    *lower* median blocks them at a lower altitude - yet it leaves less sky,
    because solid angle goes as cos(alt) and the cells are widest at the
    horizon. A per-azimuth statistic cannot express that; this is the number
    that answers "how much does this horizon cost me".
    """
    tall = make_profile(floors={0.0: 60.0, 90.0: 60.0, 180.0: 5.0, 270.0: 5.0})
    wide = make_profile(floors={0.0: 25.0, 90.0: 25.0, 180.0: 25.0, 270.0: 25.0})

    assert store.floors_summary(tall)["median_deg"] > \
           store.floors_summary(wide)["median_deg"]
    assert store.visible_sky_sq_deg(tall) < store.visible_sky_sq_deg(wide)


def test_an_unmeasured_horizon_is_the_whole_hemisphere(archive):
    """No profile must not read as "no sky"."""
    assert store.visible_sky_sq_deg({}) == pytest.approx(store.HEMISPHERE_SQ_DEG)


# ---------------------------------------------------------------------------
# Drawing it
# ---------------------------------------------------------------------------

def test_the_drawn_horizon_is_the_rule_that_is_applied(archive):
    """The line on the simulator must be the line that blocks observations.

    Drawn by sampling horizon_floor rather than by joining the measured points,
    so a reader looking at the plot is looking at the test that will actually
    be applied - not a prettier curve alongside it.
    """
    profile = make_profile(floors={0.0: 10.0, 90.0: 45.0, 180.0: 5.0, 270.0: 15.0})
    az, alt = store.horizon_castellation(profile, step_deg=1.0)
    assert az and len(az) == len(alt)
    for a, h in zip(az, alt):
        assert h == store.horizon_floor(profile, a)


def test_the_drawn_horizon_is_stepped_not_ramped(archive):
    """Flat across each sample's cell, abrupt at the boundary.

    A smooth curve between two samples 10 degrees apart would claim a precision
    the scan does not have, and would claim it on the optimistic side - drawing
    sky where the measurement only says "somewhere between these two".
    """
    profile = make_profile(floors={0.0: 5.0, 90.0: 45.0, 180.0: 5.0, 270.0: 5.0})
    _, alt = store.horizon_castellation(profile, step_deg=1.0)
    assert set(alt) == {5.0, 45.0}, "only measured levels, nothing in between"


def test_between_two_samples_the_worse_one_wins(archive):
    profile = make_profile(floors={0.0: 5.0, 90.0: 45.0, 180.0: 5.0, 270.0: 5.0})
    assert store.horizon_floor(profile, 45.0) == 45.0
    assert store.horizon_floor(profile, 89.0) == 45.0


def test_the_horizon_wraps_through_north(archive):
    """Azimuth 359 is next to azimuth 1, not off the end of a list."""
    profile = make_profile(floors={10.0: 5.0, 90.0: 5.0, 180.0: 5.0, 350.0: 40.0})
    assert store.horizon_floor(profile, 355.0) == 40.0
    assert store.horizon_floor(profile, 0.0) == 40.0
    assert store.horizon_floor(profile, 5.0) == 40.0


def test_the_api_serves_floors_in_the_shape_the_web_simulator_draws():
    """The contract between the scheduler and the simulator in the iframe.

    The web simulator re-implements horizon_floor in JavaScript over exactly
    this list, because it has to apply the rule at draw time. If `floors` ever
    stopped being [[az, alt], ...] the simulator would quietly draw no horizon
    at all - it guards on length - and a missing line reads as "nothing is
    blocked", which is the unsafe direction to fail in.
    """
    import h1_web_scheduler as scheduler

    scheduler.app.config['TESTING'] = True
    data = scheduler.app.test_client().get('/api/horizon/profile').get_json()
    if not data.get('success'):
        pytest.skip("no horizon profile on this machine")
    floors = data['floors']
    assert floors and isinstance(floors, list)
    for pair in floors:
        assert len(pair) == 2
        az, alt = pair
        assert 0.0 <= float(az) < 360.0
        assert 0.0 <= float(alt) <= 90.0


# ---------------------------------------------------------------------------
# Respecting it
# ---------------------------------------------------------------------------

def test_clearance_allows_for_the_whole_beam(archive):
    """A source centred on the floor still has half its beam in the trees.

    Foliage at 1420 MHz is a ~290 K source, not a screen, so a beam clipping it
    gains signal rather than losing it - which is why this asks for the full
    width and not the half width.
    """
    profile = make_profile(floors={0.0: 20.0, 90.0: 20.0, 180.0: 20.0, 270.0: 20.0})
    beam = store.beam_margin_deg()
    assert beam == pytest.approx(5.16, abs=0.2)

    assert store.horizon_clearance(profile, 20.0 + beam + 0.1, 0.0)["clear"]
    assert not store.horizon_clearance(profile, 20.0 + beam - 0.1, 0.0)["clear"]
    # Above the floor but inside the beam allowance: not "below the horizon",
    # but not clear either, and the wording has to distinguish them.
    mid = store.horizon_clearance(profile, 22.0, 0.0)
    assert not mid["clear"] and not mid["below_floor"]
    assert "within a beamwidth" in store.horizon_warning(profile, 22.0, 0.0)
    assert "below the measured" in store.horizon_warning(profile, 10.0, 0.0)


def test_an_unmeasured_horizon_warns_about_nothing(archive):
    """No profile must never read as "everything is blocked"."""
    assert store.horizon_warning({}, 1.0, 90.0) is None
    assert store.horizon_clearance({}, 1.0, 90.0)["known"] is False


def test_the_sector_form_is_never_looser_than_the_floor(archive):
    """horizon_sectors stands in for the hand-entered sectors, so it must not
    quietly admit anything horizon_floor would exclude.

    They are not bit-identical: sector bounds are inclusive at both ends, so
    exactly on a measured azimuth both adjacent intervals claim it and the
    stricter wins. That is the direction to err in, and the test pins the
    direction rather than the equality.
    """
    from rf_calibration import _in_obstructed_sector

    profile = make_profile(floors={0.0: 45.0, 90.0: 5.0, 180.0: 20.0, 270.0: 5.0})
    sectors = store.horizon_sectors(profile)
    margin = store.beam_margin_deg()
    for i in range(720):
        az = i * 0.5
        required = store.horizon_floor(profile, az) + margin
        assert _in_obstructed_sector(az, required - 0.01, sectors), \
            "sector form is looser than horizon_floor at az %.1f" % az


def test_the_sector_form_wraps_through_north(archive):
    from rf_calibration import _in_obstructed_sector

    profile = make_profile(floors={10.0: 5.0, 90.0: 5.0, 180.0: 5.0, 350.0: 40.0})
    sectors = store.horizon_sectors(profile)
    # The interval from 350 round to 10 is blocked to 40 + a beam.
    assert _in_obstructed_sector(0.0, 40.0, sectors)
    assert _in_obstructed_sector(355.0, 40.0, sectors)
    assert not _in_obstructed_sector(0.0, 40.0 + store.beam_margin_deg() + 1, sectors)


# ---------------------------------------------------------------------------
# Trimming a scheduled window to the visible part
# ---------------------------------------------------------------------------

def _scheduled(**over):
    obs = {"name": "test", "coord_system": "radec",
           "coord1_deg": 23.39, "coord1_min": 0, "coord1_sec": 0,
           "coord2_deg": 58.8, "coord2_min": 0, "coord2_sec": 0,
           "start_date": "2026-08-25", "start_time": "18:00",
           "duration_minutes": 480, "respect_local_horizon": True}
    obs.update(over)
    return obs


def test_a_trimmed_window_stays_put_when_saved_again():
    """The trim rewrites the stored entry, so it must not creep.

    Saving a schedule trims each window to the part where the target is clear.
    If that were not idempotent, every save would shave more off the same
    observation and an entry would walk itself down to nothing over a few
    edits. It holds because the trim takes the longest *clear* run, and a
    window that is already clear throughout is returned untouched.
    """
    import copy

    import h1_web_scheduler as scheduler

    obs = _scheduled()
    scheduler.apply_horizon_trim(obs)
    once = (obs["start_time"], obs["duration_minutes"])
    for _ in range(4):
        scheduler.apply_horizon_trim(obs)
        assert (obs["start_time"], obs["duration_minutes"]) == once

    # And an untrimmed observation is genuinely untouched, not trimmed by zero.
    high = copy.deepcopy(_scheduled(coord1_deg=19.99, coord2_deg=40.7,
                                    start_time="20:00", duration_minutes=60))
    before = copy.deepcopy(high)
    scheduler.apply_horizon_trim(high)
    assert high["start_time"] == before["start_time"]
    assert high["duration_minutes"] == before["duration_minutes"]


def test_an_unticked_observation_is_never_trimmed():
    import h1_web_scheduler as scheduler

    obs = _scheduled(respect_local_horizon=False)
    scheduler.apply_horizon_trim(obs)
    assert (obs["start_time"], obs["duration_minutes"]) == ("18:00", 480)
    assert "horizon_note" not in obs
    assert "horizon_blocked" not in obs


def test_a_window_with_no_visible_part_is_marked_not_trimmed():
    """Marked rather than zero-length, so the reason survives to the log."""
    import h1_web_scheduler as scheduler

    # Deep south from Glasgow: never clears the treeline.
    obs = _scheduled(coord1_deg=18.0, coord2_deg=-45.0,
                     start_time="20:00", duration_minutes=300)
    scheduler.apply_horizon_trim(obs)
    assert obs["horizon_blocked"] is True
    assert obs["duration_minutes"] == 300, "the times are left for the log to quote"
    assert "behind the measured horizon" in obs["horizon_note"]


def test_the_longest_clear_run_wins_not_merely_the_ends():
    """A target can be clear at both ends and behind a tower in between.

    The northern towers reach 45 deg, so this is not hypothetical: a
    circumpolar source clears them, passes behind, and clears again. Trimming
    on the endpoints alone would keep the whole window and observe the tower
    for the middle third of it.
    """
    import h1_web_scheduler as scheduler

    obs = _scheduled(coord1_deg=2.0, coord2_deg=70.0,
                     start_time="20:00", duration_minutes=360)
    start, duration, note = scheduler.horizon_visible_window(obs)
    assert start is not None and note is not None
    assert duration < 360, "the passage behind the tower must be cut out"

    # Every minute of what survives really is clear.
    import horizon_store
    profile = horizon_store.load_active()
    margin = horizon_store.beam_margin_deg()
    from datetime import timedelta
    for minute in range(int(duration) + 1):
        alt, az = scheduler.observation_altaz_at(obs, start + timedelta(minutes=minute))
        assert alt >= horizon_store.horizon_floor(profile, az) + margin - 1e-6, \
            "minute %d of the trimmed window is behind the horizon" % minute


@pytest.mark.parametrize("system,why", [
    ("satellite", "its window comes from its own pass prediction"),
    ("calibration", "it follows the Sun all day"),
    ("horizon", "it is the thing that measures the horizon"),
])
def test_some_observations_are_never_trimmed(system, why):
    """The horizon scan is the one that matters.

    Trimming it against the last measurement would stop it re-measuring
    wherever the sky was previously found blocked - which is precisely where a
    pruning needs re-measuring. A horizon that had closed in once could then
    never be shown to have opened again.
    """
    import h1_web_scheduler as scheduler

    obs = _scheduled(coord_system=system)
    start, duration, note = scheduler.horizon_visible_window(obs)
    assert note is None and duration == 480, why

    scheduler.apply_horizon_trim(obs)
    assert obs["start_time"] == "18:00" and obs["duration_minutes"] == 480
    assert not obs.get("horizon_blocked")


def test_a_sun_raster_is_allowed_more_room_than_a_single_pointing():
    """The retired 30 deg sector folded the raster in; this recomputes it.

    A raster's lowest row sits (n-1)/2 * spacing below the Sun, so the Sun
    clearing the trees is not enough - the bottom of the raster has to clear
    them too. Retiring the hand-entered sectors and dropping to a beam-only
    margin would have quietly readmitted the 2026-08-20 scans that fitted
    0.5-1.2 deg low, which is exactly what those sectors existed to exclude.
    """
    from unittest.mock import patch

    import h1_web_scheduler as scheduler

    profile = {"entries": [
        {"az_deg": az, "fit": {"success": True, "alt_clear": 20.0}}
        for az in (0.0, 90.0, 180.0, 270.0)]}
    cfg = {"respect_local_horizon": True}

    with patch("horizon_store.load_active", return_value=profile):
        plain = scheduler.horizon_obstruction_sectors(cfg)
        raster = scheduler.sun_raster_obstruction_sectors(cfg, n=5, spacing_deg=1.5)
        wider = scheduler.sun_raster_obstruction_sectors(cfg, n=9, spacing_deg=2.0)

    beam = store.beam_margin_deg()
    assert plain[0][2] == pytest.approx(20.0 + beam)
    assert raster[0][2] == pytest.approx(20.0 + beam + 3.0)      # (5-1)/2 * 1.5
    assert wider[0][2] == pytest.approx(20.0 + beam + 8.0)       # (9-1)/2 * 2.0
    assert wider[0][2] > raster[0][2] > plain[0][2], \
        "a raster that reaches further down needs more room"


def test_with_no_measured_horizon_nothing_is_excluded():
    """Honest rather than safe, and deliberately so.

    With the hand-entered sectors retired there is no fallback list, so a site
    that has never run a horizon scan excludes nothing. Inventing a default
    would be claiming knowledge of a skyline nobody has measured.
    """
    from unittest.mock import patch

    import h1_web_scheduler as scheduler

    with patch("horizon_store.load_active", return_value=None):
        assert scheduler.horizon_obstruction_sectors({"respect_local_horizon": True}) == []
        assert scheduler.sun_raster_obstruction_sectors({"respect_local_horizon": True}) == []


def test_no_profile_blocks_nothing(archive):
    assert store.horizon_floor({}, 123.0) == 0.0
    assert store.is_obstructed({}, 1.0, 123.0) is False
    assert store.horizon_castellation({}) == ([], [])
