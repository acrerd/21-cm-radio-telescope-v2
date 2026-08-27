#!/usr/bin/env python3
"""
Unit tests for h1_web_scheduler.py

Run with: python -m pytest test_scheduler.py -v
"""

import json
import math
import os
import re
import sys
import threading
import time
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

# Ensure the scheduler module can be imported
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Mock flask before importing the scheduler (in case flask is not installed in test env)
# The actual Flask app is not needed for unit tests of pure functions
try:
    import h1_web_scheduler as sched
except ImportError:
    pytest.skip("Could not import h1_web_scheduler", allow_module_level=True)

import observation_files


# =============================================================================
# dms_to_decimal
# =============================================================================

class TestDmsToDecimal:
    """Tests for coordinate conversion from degrees/minutes/seconds to decimal."""

    def test_simple_degrees(self):
        assert sched.dms_to_decimal(45, 0, 0.0) == 45.0

    def test_degrees_minutes(self):
        result = sched.dms_to_decimal(45, 30, 0.0)
        assert result == pytest.approx(45.5)

    def test_degrees_minutes_seconds(self):
        result = sched.dms_to_decimal(45, 30, 36.0)
        assert result == pytest.approx(45.51)

    def test_negative_degrees(self):
        result = sched.dms_to_decimal(-30, 15, 0.0)
        assert result == pytest.approx(-30.25)

    def test_zero(self):
        assert sched.dms_to_decimal(0, 0, 0.0) == 0.0

    def test_ra_mode(self):
        """RA mode: input is hours/minutes/seconds, output is decimal hours."""
        result = sched.dms_to_decimal(12, 30, 0.0, is_ra=True)
        assert result == pytest.approx(12.5)

    def test_ra_mode_same_as_regular(self):
        """is_ra doesn't change the math, just the interpretation."""
        assert sched.dms_to_decimal(6, 45, 0.0, is_ra=True) == \
               sched.dms_to_decimal(6, 45, 0.0, is_ra=False)

    def test_full_circle(self):
        result = sched.dms_to_decimal(359, 59, 59.0)
        assert result == pytest.approx(360.0, abs=0.01)

    def test_south_pole(self):
        result = sched.dms_to_decimal(-90, 0, 0.0)
        assert result == -90.0


# =============================================================================
# Calibration request validation
# =============================================================================

class TestSunScanParameterValidation:
    def test_valid_calibration_parameters_are_normalised(self):
        params = sched._validate_sun_scan_params({
            "n": "5",
            "sdr_type": "DEMO",
            "interval_minutes": "30",
        }, include_interval=True)

        assert params["n"] == 5
        assert params["sdr_type"] == "demo"
        assert params["interval_minutes"] == 30

    def test_even_grid_is_rejected(self):
        with pytest.raises(ValueError, match="must be odd"):
            sched._validate_sun_scan_params({"n": 4})

    def test_non_finite_number_is_rejected(self):
        with pytest.raises(ValueError, match="grid_spacing_deg"):
            sched._validate_sun_scan_params({"grid_spacing_deg": float("nan")})


class TestControllerUrlResolution:
    def test_candidates_include_runtime_and_fresh_config_primary(self):
        cfg = {
            "srt_controller_url": "http://192.0.2.120/",
            "srt_controller_fallback_urls": ["http://srt-controller.local"],
        }
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(sched, "SRT_CONTROLLER_URL", "http://192.0.2.136"), \
             patch.object(sched, "load_config", return_value=cfg):
            assert sched._controller_url_candidates() == [
                "http://192.0.2.136",
                "http://192.0.2.120",
                "http://srt-controller.local",
            ]

    def test_api_tries_next_controller_after_bad_json(self):
        bad_response = MagicMock()
        bad_response.__enter__.return_value.read.return_value = b"not json"
        good_response = MagicMock()
        good_response.__enter__.return_value.read.return_value = b'{"alt": 12.5}'

        with patch.object(sched, "_controller_url_candidates", return_value=[
                 "http://stale", "http://working"]), \
             patch.object(sched.urllib.request, "urlopen",
                          side_effect=[bad_response, good_response]), \
             patch.object(sched, "SRT_CONTROLLER_URL", "http://stale"):
            result = sched.srt_api_call("/status")

        assert result == {"alt": 12.5}


class TestCalibrationDayRetry:
    def test_rejected_scan_is_rehomed_and_retried_before_counting_failure(self):
        original_scan_state = dict(sched.sun_scan_state)
        original_cal_state = dict(sched.cal_day_state)
        calls = []

        def fake_run_scan(params):
            calls.append(dict(params))
            if len(calls) == 1:
                sched.sun_scan_state.update(
                    running=False,
                    result={"fit": {"success": False}},
                    error="Sun scan fit rejected: no beam",
                )
            else:
                sched.sun_scan_state.update(
                    running=False,
                    result={"fit": {"success": True}},
                    error=None,
                )

        def fake_save(_result):
            sched.cal_day_cancel.set()

        try:
            with patch('sun_scan.get_sun_altaz', return_value=(35.0, 150.0)), \
                 patch('sun_scan.save_scan_to_pointing_data', side_effect=fake_save) as save, \
                 patch.object(sched, '_run_sun_scan', side_effect=fake_run_scan), \
                 patch.object(sched.time, 'sleep'):
                sched._run_calibration_day({
                    "sdr_type": "b210",
                    "interval_minutes": 30,
                })

            assert len(calls) == 2
            assert all(call["home_before_scan"] is True for call in calls)
            save.assert_called_once()
            assert sched.cal_day_state["scans_completed"] == 1
            assert sched.cal_day_state["consecutive_failures"] == 0
        finally:
            sched.sun_scan_state.clear()
            sched.sun_scan_state.update(original_scan_state)
            sched.cal_day_state.clear()
            sched.cal_day_state.update(original_cal_state)
            sched.cal_day_cancel.clear()

    def test_no_scan_is_taken_while_the_sun_is_behind_an_obstruction(self):
        """A scan through the treeline is wrong, not weak, so it is not taken.

        Foliage is a bright extended source at 1420 MHz. Caught in the lower
        rows of the raster it drags the fitted beam centre down into itself, and
        the resulting scan is saved looking as respectable as any other.

        Since 2026-08-25 the sectors come from the measured horizon profile
        rather than a hand-entered config list, so the profile is what this
        patches. The Sun sits at alt 25 az 100, where the synthetic profile
        floor is 20 - clear of the floor itself, but not once the beam and the
        raster's reach below centre are allowed for, which is the whole point.
        """
        profile = {
            "entries": [
                {"az_deg": az, "fit": {"success": True, "alt_clear": alt}}
                for az, alt in [(0.0, 5.0), (90.0, 20.0), (110.0, 20.0),
                                (180.0, 5.0), (270.0, 5.0)]
            ]
        }
        original_cal_state = dict(sched.cal_day_state)
        phases = []

        def stop_waiting(_timeout):
            phases.append(sched.cal_day_state["phase"])
            sched.cal_day_cancel.set()
            return True

        try:
            with patch('sun_scan.get_sun_altaz', return_value=(25.0, 100.0)), \
                 patch('horizon_store.load_active', return_value=profile), \
                 patch.object(sched, 'load_config', return_value={
                     "observer_lat": 55.9, "observer_lon": -4.3,
                     "observer_elevation": 50,
                     "respect_local_horizon": True,
                 }), \
                 patch.object(sched.cal_day_cancel, 'wait', side_effect=stop_waiting), \
                 patch.object(sched, '_run_sun_scan') as run_scan, \
                 patch.object(sched.time, 'sleep'):
                sched._run_calibration_day({
                    "sdr_type": "b210",
                    "interval_minutes": 30,
                })

            run_scan.assert_not_called()
            assert phases == ["waiting_for_clear_horizon"]
        finally:
            sched.cal_day_state.clear()
            sched.cal_day_state.update(original_cal_state)
            sched.cal_day_cancel.clear()


# =============================================================================
# parse_tle
# =============================================================================


def _page_markup():
    """The operator page as served.

    It stopped being sched.HTML_TEMPLATE on 2026-08-25 and became a static file
    under web/, so these read it back through Flask rather than reaching into
    the module for a string that is no longer there.
    """
    import page_sources as ps

    sched.app.config["TESTING"] = True
    with sched.app.test_client() as client:
        html, _ = ps.fetch_page(client)
    return html


class TestParseTle:
    """Tests for TLE text parsing."""

    def test_three_line_tle(self):
        tle = "ISS (ZARYA)\n1 25544U 98067A...\n2 25544  51.6400..."
        name, l1, l2 = sched.parse_tle(tle)
        assert name == "ISS (ZARYA)"
        assert l1.startswith("1 ")
        assert l2.startswith("2 ")

    def test_two_line_tle(self):
        tle = "1 25544U 98067A...\n2 25544  51.6400..."
        name, l1, l2 = sched.parse_tle(tle)
        assert name == "SATELLITE"
        assert l1.startswith("1 ")
        assert l2.startswith("2 ")

    def test_strips_whitespace(self):
        tle = "  ISS  \n  1 25544U...  \n  2 25544...  \n"
        name, l1, l2 = sched.parse_tle(tle)
        assert name == "ISS"
        assert not l1.startswith(" ")

    def test_blank_lines_ignored(self):
        tle = "\nISS\n\n1 25544U...\n\n2 25544...\n"
        name, l1, l2 = sched.parse_tle(tle)
        assert name == "ISS"

    def test_single_line_raises(self):
        with pytest.raises(ValueError, match="Expected 2 or 3"):
            sched.parse_tle("just one line")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Expected 2 or 3"):
            sched.parse_tle("")

    def test_four_lines_raises(self):
        tle = "NAME\n1 line1\n2 line2\nextra line"
        with pytest.raises(ValueError, match="Expected 2 or 3"):
            sched.parse_tle(tle)


# =============================================================================
# find_clashes
# =============================================================================

def _make_obs(name, start_time, duration_minutes, enabled=True, start_date="2026-03-28"):
    return {
        "name": name,
        "start_date": start_date,
        "start_time": start_time,
        "duration_minutes": duration_minutes,
        "enabled": enabled,
    }


class TestFindClashes:
    """Tests for schedule clash detection."""

    def test_no_clashes_sequential(self):
        schedule = [
            _make_obs("A", "10:00", 30),
            _make_obs("B", "10:30", 30),
        ]
        assert sched.find_clashes(schedule) == []

    def test_overlap_detected(self):
        schedule = [
            _make_obs("A", "10:00", 60),
            _make_obs("B", "10:30", 30),
        ]
        clashes = sched.find_clashes(schedule)
        assert len(clashes) == 1
        assert "'A'" in clashes[0]
        assert "'B'" in clashes[0]

    def test_exact_adjacency_no_clash(self):
        """A ends at 10:30, B starts at 10:30 — no overlap."""
        schedule = [
            _make_obs("A", "10:00", 30),
            _make_obs("B", "10:30", 30),
        ]
        assert sched.find_clashes(schedule) == []

    def test_dateless_entries_do_not_clash_with_today(self):
        """A dateless leftover is not "today", and must not block a save.

        find_clashes used to substitute today's date for a missing start_date,
        so any old entry without a date collided with whatever was genuinely
        scheduled for today. POST /api/schedule then rejected the save with a
        400 for a clash that did not exist (S8).
        """
        today = datetime.now().strftime('%Y-%m-%d')
        schedule = [
            _make_obs("real", "10:00", 60, start_date=today),
            _make_obs("leftover", "10:30", 30, start_date=""),
        ]
        assert sched.find_clashes(schedule) == []

    def test_dateless_entries_do_not_clash_with_each_other(self):
        schedule = [
            _make_obs("A", "10:00", 60, start_date=""),
            _make_obs("B", "10:30", 30, start_date=""),
        ]
        assert sched.find_clashes(schedule) == []

    def test_same_day_overlap_still_detected(self):
        """The dateless exclusion must not weaken real clash detection."""
        schedule = [
            _make_obs("A", "10:00", 60, start_date="2026-03-28"),
            _make_obs("B", "10:30", 30, start_date="2026-03-28"),
        ]
        assert len(sched.find_clashes(schedule)) == 1

    def test_disabled_obs_ignored(self):
        schedule = [
            _make_obs("A", "10:00", 60),
            _make_obs("B", "10:30", 30, enabled=False),
        ]
        assert sched.find_clashes(schedule) == []

    def test_no_start_time_ignored(self):
        schedule = [
            _make_obs("A", "10:00", 60),
            {"name": "B", "start_date": "2026-03-28", "start_time": "", "duration_minutes": 30, "enabled": True},
        ]
        assert sched.find_clashes(schedule) == []

    def test_multiple_clashes(self):
        schedule = [
            _make_obs("A", "10:00", 120),
            _make_obs("B", "10:30", 30),
            _make_obs("C", "11:00", 30),
        ]
        clashes = sched.find_clashes(schedule)
        assert len(clashes) == 2

    def test_different_dates_no_clash(self):
        schedule = [
            _make_obs("A", "10:00", 60, start_date="2026-03-28"),
            _make_obs("B", "10:00", 60, start_date="2026-03-29"),
        ]
        assert sched.find_clashes(schedule) == []

    def test_empty_schedule(self):
        assert sched.find_clashes([]) == []

    def test_single_obs(self):
        assert sched.find_clashes([_make_obs("A", "10:00", 30)]) == []

    def test_contained_overlap(self):
        """B is entirely within A."""
        schedule = [
            _make_obs("A", "10:00", 120),
            _make_obs("B", "10:30", 30),
        ]
        clashes = sched.find_clashes(schedule)
        assert len(clashes) == 1

    def test_invalid_time_format_skipped(self):
        schedule = [
            _make_obs("A", "10:00", 60),
            {"name": "B", "start_date": "2026-03-28", "start_time": "invalid", "duration_minutes": 30, "enabled": True},
        ]
        assert sched.find_clashes(schedule) == []


# =============================================================================
# generate_filename
# =============================================================================

class TestGenerateFilename:
    """The name carries the time and the mode, and nothing else.

    Rewritten 2026-08-25. The old name held the target and the calibrator flag,
    which are attributes of the file - so the name was a second copy of them,
    free to disagree with the first after any rename or any edit of the
    schedule entry. These tests now assert the *absence* of that information as
    firmly as they assert what is present, because putting it back would look
    like a helpful improvement.
    """

    @patch.object(sched, 'get_config_value', return_value="/tmp/test_data")
    def test_the_name_is_the_time_and_the_mode(self, mock_config):
        obs = {"name": "Sun Survey", "coord_system": "radec", "calibrator": False}
        result = sched.generate_filename(obs)
        assert os.path.basename(result).endswith("_track.h5")
        # YYYYMMDD_HHMMSS_track.h5 and no more.
        stem = os.path.basename(result)[:-len(".h5")]
        assert re.fullmatch(r"\d{8}_\d{6}_track", stem), stem

    @patch.object(sched, 'get_config_value', return_value="/tmp/test_data")
    def test_recordings_go_in_their_own_folder(self, mock_config):
        """Not loose in the data folder, which is where the plots live."""
        obs = {"name": "Sun Survey", "coord_system": "radec"}
        result = sched.generate_filename(obs)
        expected = os.path.realpath(
            os.path.join("/tmp/test_data",
                         observation_files.OBSERVATION_SUBFOLDER))
        assert os.path.dirname(result) == expected
        assert os.path.isdir(expected), "the folder should have been created"

    @patch.object(sched, 'get_config_value', return_value="/tmp/test_data")
    def test_the_name_says_nothing_the_file_already_says(self, mock_config):
        """The regression this rewrite exists to prevent."""
        obs = {"name": "Cas A", "coord_system": "radec", "calibrator": True,
               "object_name": "sun", "center_freq_mhz": 1420.405}
        name = os.path.basename(sched.generate_filename(obs))
        for leaked in ("cas", "_cal", "sun", "1420"):
            assert leaked not in name.lower(), (
                "%r is an attribute of the file; putting it in the name makes "
                "a second copy that a rename can falsify" % leaked)

    @pytest.mark.parametrize("system,mode", [
        ("radec", "track"), ("galactic", "track"), ("object", "track"),
        ("satellite", "track"),
        ("drift", "drift"),
        # The one worth spelling out: an alt/az entry is sent to /direct with
        # tracking off, so the dish is parked and the sky moves through the
        # beam. That is a drift scan however the entry was typed.
        ("altaz", "drift"),
    ])
    @patch.object(sched, 'get_config_value', return_value="/tmp/test_data")
    def test_the_mode_follows_what_the_mount_does(self, mock_config, system, mode):
        result = sched.generate_filename({"name": "x", "coord_system": system})
        assert os.path.basename(result).endswith("_%s.h5" % mode)

    @patch.object(sched, 'get_config_value', return_value="/tmp/test_data")
    def test_two_in_the_same_second_do_not_collide(self, mock_config):
        """Or the second open truncates the first file."""
        folder = sched.observations_folder()
        when = datetime(2026, 8, 25, 12, 0, 0)
        first = observation_files.observation_filename(folder, "track", when)
        open(first, "w").close()
        try:
            second = observation_files.observation_filename(folder, "track", when)
            assert second != first
        finally:
            os.remove(first)

    @patch.object(sched, 'get_config_value', return_value="/tmp/test_data")
    def test_an_explicit_name_without_an_extension_gets_one(self, mock_config):
        """A typed name is a stem. "Cas A drift scan" produced a recording
        with no extension on 2026-08-26 - present in the folder and invisible
        to every *.h5 listing the notebook and the Observe tab use."""
        result = sched.generate_filename({"name": "x", "filename": "Cas A drift scan"})
        assert result.endswith("Cas A drift scan.h5")
        assert sched.generate_filename({"name": "x", "filename": "keep.HDF5"}).endswith("keep.HDF5")

    def test_the_comment_travels_to_the_recording(self, tmp_path, monkeypatch):
        """Free text from the schedule form must reach H1_OBS_METADATA.

        The receiver writes every non-empty metadata value as an HDF5
        attribute, so this is the whole path: form -> entry -> environment
        -> file. A comment that stops at the schedule is a comment the file
        does not remember.
        """
        captured = {}

        proc = MagicMock()
        proc.poll.return_value = None

        def fake_popen(cmd, env=None, **kw):
            captured.update(json.loads(env["H1_OBS_METADATA"]))
            return proc

        monkeypatch.setattr(sched, "LAST_OBSERVATION_FILE",
                            str(tmp_path / "last_observation.json"))
        obs = {"name": "Commented", "coord_system": "altaz",
               "coord1_deg": 45, "coord1_min": 0, "coord1_sec": 0,
               "coord2_deg": 180, "coord2_min": 0, "coord2_sec": 0,
               "center_freq_mhz": 1420.405, "channels": 1024,
               "integration_time_s": 3.0, "sdr_type": "demo", "gain_db": 40,
               "bandwidth_mhz": 2.4, "duration_minutes": 5,
               "comment": "Cas A drift, checking A_e off the plane"}
        with patch.object(sched, "SRT_CONTROLLER_URL", None), \
             patch("subprocess.Popen", side_effect=fake_popen), \
             patch.object(sched, "generate_filename",
                          return_value=str(tmp_path / "c.h5")):
            assert sched.start_observation(dict(obs)) is True
        try:
            assert captured.get("comment") == obs["comment"]
        finally:
            proc.poll.return_value = 0
            with patch.object(sched, "SRT_CONTROLLER_URL", None):
                sched.stop_observation()

    @patch.object(sched, 'get_config_value', return_value="/tmp/test_data")
    def test_explicit_filename(self, mock_config):
        obs = {"name": "Test", "filename": "my_file.h5"}
        result = sched.generate_filename(obs)
        assert result.endswith("my_file.h5")

    @patch.object(sched, 'get_config_value', return_value="/tmp/test_data")
    def test_an_explicit_filename_cannot_escape_the_folder(self, mock_config):
        """Still contained, now against the observations folder."""
        folder = os.path.realpath(
            os.path.join("/tmp/test_data", observation_files.OBSERVATION_SUBFOLDER))
        for escape in ("../../etc/passwd.h5", "/etc/passwd.h5"):
            result = sched.generate_filename({"name": "T", "filename": escape})
            assert result.startswith(folder + os.sep), escape


# =============================================================================
# Config loading/saving
# =============================================================================

class TestConfig:
    """Tests for configuration persistence."""

    def test_load_config_defaults(self, tmp_path):
        """When no config file exists, defaults are returned."""
        with patch.object(sched, 'CONFIG_FILE', str(tmp_path / "nonexistent.json")):
            cfg = sched.load_config()
            assert cfg["srt_controller_url"] == "http://192.168.50.120"
            # The true site, same value as the firmware's OBSERVER_LAT.
            # Written out rather than imported, deliberately. Everything else
            # takes the position from instrument.py; this is the one
            # independent statement of what it should be, so an accidental
            # edit there fails here instead of propagating silently. If you are
            # changing the site on purpose, change it in both.
            assert cfg["observer_lat"] == pytest.approx(55.902426)
            assert cfg["observer_lon"] == pytest.approx(-4.307865)
            assert cfg["sound_enabled"] is True
            assert cfg["receiver_python_path"] == "/home/astro/radioconda/bin/python"

    def test_save_and_load_config(self, tmp_path):
        config_file = str(tmp_path / "test_config.json")
        with patch.object(sched, 'CONFIG_FILE', config_file):
            sched.save_config({"srt_controller_url": "http://10.0.0.1", "custom_key": 42})
            cfg = sched.load_config()
            assert cfg["srt_controller_url"] == "http://10.0.0.1"
            assert cfg["custom_key"] == 42
            # Defaults are still present
            assert "observer_lat" in cfg

    def test_load_config_corrupt_file(self, tmp_path):
        config_file = tmp_path / "bad.json"
        config_file.write_text("not valid json{{{")
        with patch.object(sched, 'CONFIG_FILE', str(config_file)):
            cfg = sched.load_config()
            # Should return defaults
            assert cfg["srt_controller_url"] == "http://192.168.50.120"


# =============================================================================
# Schedule loading/saving
# =============================================================================

class TestSchedule:
    """Tests for schedule persistence."""

    def test_load_empty(self, tmp_path):
        with patch.object(sched, 'SCHEDULE_FILE', str(tmp_path / "nonexistent.json")):
            assert sched.load_schedule() == []

    def test_save_and_load(self, tmp_path):
        sched_file = str(tmp_path / "schedule.json")
        with patch.object(sched, 'SCHEDULE_FILE', sched_file):
            obs_list = [_make_obs("Test", "12:00", 30)]
            sched.save_schedule(obs_list)
            loaded = sched.load_schedule()
            assert len(loaded) == 1
            assert loaded[0]["name"] == "Test"


# =============================================================================
# Flask API endpoints
# =============================================================================

class TestFlaskAPI:
    """Tests for the Flask HTTP API using the test client."""

    @pytest.fixture
    def client(self, tmp_path):
        """Create a Flask test client with isolated files."""
        sched.app.config['TESTING'] = True
        # Isolate schedule and config files
        self._sched_file = str(tmp_path / "schedule.json")
        self._config_file = str(tmp_path / "config.json")
        self._patches = [
            patch.object(sched, 'SCHEDULE_FILE', self._sched_file),
            patch.object(sched, 'CONFIG_FILE', self._config_file),
        ]
        for p in self._patches:
            p.start()
        with sched.app.test_client() as client:
            yield client
        for p in self._patches:
            p.stop()

    def test_get_schedule_empty(self, client):
        resp = client.get('/api/schedule')
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_post_and_get_schedule(self, client):
        obs_list = [_make_obs("Test Obs", "14:00", 30)]
        resp = client.post('/api/schedule',
                           data=json.dumps(obs_list),
                           content_type='application/json')
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        resp = client.get('/api/schedule')
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["name"] == "Test Obs"

    def test_post_schedule_returns_what_it_stored(self, client):
        """The response carries the stored schedule, not an acknowledgement.

        The server may trim what was posted - the horizon trim rewrites start
        times - so a client that keeps displaying what it *sent* shows a
        schedule that will not run. That happened on 2026-08-25: the alert
        announced a trim to 09:42 while the list and the edit window went on
        showing 06:00. The page adopts response['schedule'] wholesale, so this
        pins that the field exists and matches what a GET would return.
        """
        obs_list = [_make_obs("Echo", "14:00", 30)]
        resp = client.post('/api/schedule',
                           data=json.dumps(obs_list),
                           content_type='application/json')
        body = resp.get_json()
        assert body["success"] is True
        assert body["schedule"] == client.get('/api/schedule').get_json(), (
            "the POST response must be the stored schedule, or the browser "
            "keeps showing times the server has rewritten")

    def test_the_simulator_books_a_tracked_spectrum(self, client):
        """The Schedule button: a tracked spectrum starts at the page's clock.

        The clock is sent as UTC; the schedule keeps local time, so 12:00Z on
        2026-08-26 must land as the local equivalent. tau is the simulated
        integration and becomes the duration, as the Observe hand-off did.
        """
        # A pinned clock a day ahead, on a whole minute: booked as it is.
        epoch = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0)
        resp = client.post('/api/simulator/schedule', json={
            'l': 132.0, 'b': -1.0, 'mode': 'hi',
            'epoch_utc': epoch.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'center_freq_mhz': 1420.405752, 'bandwidth_mhz': 2.0,
            'channels': 327, 'integration_time_s': 600.0})
        d = resp.get_json()
        assert resp.status_code == 200 and d['success'], d
        e = d['entry']
        assert e['coord_system'] == 'galactic'
        assert e['coord1_deg'] == pytest.approx(132.0)
        assert e['coord2_deg'] == pytest.approx(-1.0)
        assert e['comment'] == sched.SIMULATOR_COMMENT
        local = epoch.astimezone()
        assert e['start_date'] == local.strftime('%Y-%m-%d')
        assert e['start_time'] == local.strftime('%H:%M')
        assert e['duration_minutes'] == 10
        # The tuning is the fixed instrument's (issue #27): whatever the page
        # sent is not carried into the entry.
        assert 'channels' not in e and 'bandwidth_mhz' not in e and 'center_freq_mhz' not in e
        stored = client.get('/api/schedule').get_json()
        assert [o['name'] for o in stored] == [e['name']]

    def test_the_simulator_books_a_drift_scan_starting_at_the_clock(self, client):
        """A drift scan starts at the page's clock and crosses beam centre
        half a scan in - what the drift panel draws. It is not centred on the
        next meridian transit: that booked tomorrow morning for a scan asked
        for now (2026-08-26), and the drift machinery parks for any T."""
        epoch = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=22, minute=0, second=0, microsecond=0)
        resp = client.post('/api/simulator/schedule', json={
            'l': 111.735, 'b': -2.130, 'mode': 'cont',
            'epoch_utc': epoch.strftime('%Y-%m-%dT%H:%M:%SZ'), 'scan_minutes': 300,
            'center_freq_mhz': 1420.405752, 'bandwidth_mhz': 2.4,
            'channels': 4096, 'integration_time_s': 240.0})
        d = resp.get_json()
        assert resp.status_code == 200 and d['success'], d
        e = d['entry']
        assert e['coord_system'] == 'drift' and e['drift_frame'] == 'galactic'
        assert e['drift_window_min'] == 150 and e['duration_minutes'] == 300
        assert e['integration_time_s'] == 240.0, "tau is the time per sample"
        assert e['comment'] == sched.SIMULATOR_COMMENT
        start = epoch.astimezone()
        assert e['start_date'] == start.strftime('%Y-%m-%d')
        assert e['start_time'] == start.strftime('%H:%M')
        assert e['drift_time'] == (start + timedelta(minutes=150)).strftime('%H:%M')

    def test_a_live_clock_starts_now_rather_than_booking(self, client):
        """Pressed at 21:25:57 with a one-minute spectrum, Schedule used to
        book 21:25 - a minute already gone - and the entry arrived expired;
        booked for the next minute instead, a run waited for the minute, then
        for the thread's poll, then had its duration trimmed to the slot. A
        live clock now starts the observation at once, with the full
        duration, and writes nothing to the schedule."""
        started = []
        with patch.object(sched, 'start_observation', side_effect=lambda obs: started.append(obs) or True):
            resp = client.post('/api/simulator/schedule', json={
                'l': 42.7, 'b': 1.4, 'mode': 'hi',
                'epoch_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'integration_time_s': 60.0})
        d = resp.get_json()
        assert resp.status_code == 200 and d['success'] and d['started'], d
        assert len(started) == 1 and started[0]['coord_system'] == 'galactic'
        assert started[0]['duration_minutes'] == sched.SIMULATOR_MIN_SPECTRUM_MIN
        # Listed as well, so the page shows it running in its place.
        listed = client.get('/api/schedule').get_json()
        assert [o['name'] for o in listed] == [started[0]['name']]

    def test_a_finished_slot_is_not_started_again(self):
        """Stopped by hand or run to the end, a booking's slot is done with:
        the thread must not start it again while the slot is still due
        (issue #25 - twice on 2026-08-26 a stopped Sun drift restarted within
        ten seconds)."""
        obs = {'name': 'Once only', 'start_date': '2026-08-26', 'start_time': '14:00',
               'duration_minutes': 30, 'coord_system': 'altaz'}
        key = sched._slot_key(obs)
        assert key == ('2026-08-26', '14:00', 'Once only')
        assert sched._slot_key({'name': 'Run Now, no slot'}) is None
        sched.finished_slots.clear()
        try:
            sched.finished_slots.add(key)
            assert sched._slot_key(dict(obs)) in sched.finished_slots
            # An edited booking is a different slot.
            assert sched._slot_key(dict(obs, start_time='14:05')) not in sched.finished_slots
        finally:
            sched.finished_slots.clear()

    def test_a_live_clock_is_refused_while_something_records(self, client):
        proc = MagicMock(); proc.poll.return_value = None
        with patch.object(sched, 'current_process', proc), \
             patch.object(sched, 'current_observation', {'name': 'Busy one'}), \
             patch.object(sched, 'start_observation') as start:
            resp = client.post('/api/simulator/schedule', json={
                'l': 42.7, 'b': 1.4, 'mode': 'hi',
                'epoch_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'integration_time_s': 60.0})
        assert resp.status_code == 409 and 'Busy one' in resp.get_json()['error']
        start.assert_not_called()

    def test_a_pinned_clock_books_the_next_whole_minute_and_at_least_two(self, client):
        """A pinned clock a few minutes ahead is a booking: the next whole
        minute with 45 s in hand, and a spectrum of at least two minutes."""
        now = datetime.now()
        epoch = datetime.now(timezone.utc) + timedelta(minutes=5)
        resp = client.post('/api/simulator/schedule', json={
            'l': 42.7, 'b': 1.4, 'mode': 'hi',
            'epoch_utc': epoch.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'integration_time_s': 60.0})
        d = resp.get_json()
        assert resp.status_code == 200 and d['success'] and not d['started'], d
        e = d['entry']
        start = datetime.strptime(e['start_date'] + ' ' + e['start_time'], '%Y-%m-%d %H:%M')
        assert start.second == 0 and (start - now).total_seconds() >= sched.SIMULATOR_LEAD_S
        assert e['duration_minutes'] == sched.SIMULATOR_MIN_SPECTRUM_MIN

    def test_a_clock_in_the_past_is_refused_not_booked_dead(self, client):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        resp = client.post('/api/simulator/schedule', json={
            'l': 42.7, 'b': 1.4, 'mode': 'hi',
            'epoch_utc': past.strftime('%Y-%m-%dT%H:%M:%SZ'), 'integration_time_s': 600.0})
        assert resp.status_code == 409
        assert 'in the past' in resp.get_json()['error']
        assert client.get('/api/schedule').get_json() == []

    def test_the_simulator_is_refused_a_clash(self, client):
        """Same refusal as any other save: the entry must not be stored."""
        first = client.post('/api/simulator/schedule', json={
            'l': 10.0, 'b': 0.0, 'mode': 'hi',
            'epoch_utc': (datetime.now(timezone.utc) + timedelta(days=6)).replace(
                hour=12, minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'integration_time_s': 3600.0})
        assert first.get_json()['success']
        second = client.post('/api/simulator/schedule', json={
            'l': 20.0, 'b': 0.0, 'mode': 'hi',
            'epoch_utc': (datetime.now(timezone.utc) + timedelta(days=6)).replace(
                hour=12, minute=30, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'integration_time_s': 600.0})
        assert second.status_code == 409
        assert len(client.get('/api/schedule').get_json()) == 1

    def test_the_schedule_is_stored_in_chronological_order(self, client):
        """Whatever order entries arrive in, they are kept and returned by
        start time; entries without one go last."""
        late = _make_obs("Late", "15:00", 30); late["start_date"] = "2026-09-02"
        early = _make_obs("Early", "09:00", 30); early["start_date"] = "2026-09-01"
        undated = _make_obs("Undated", "", 30); undated["start_date"] = ""
        resp = client.post('/api/schedule', data=json.dumps([late, undated, early]),
                           content_type='application/json')
        assert resp.get_json()["success"] is True
        names = [o["name"] for o in client.get('/api/schedule').get_json()]
        assert names == ["Early", "Late", "Undated"]

    def test_post_schedule_with_clash(self, client):
        obs_list = [
            _make_obs("A", "10:00", 60),
            _make_obs("B", "10:30", 30),
        ]
        resp = client.post('/api/schedule',
                           data=json.dumps(obs_list),
                           content_type='application/json')
        assert resp.status_code == 400

    def test_get_status_idle(self, client):
        resp = client.get('/api/status')
        data = resp.get_json()
        assert data["running"] is False
        assert data["observation"] is None

    def test_get_config(self, client):
        resp = client.get('/api/config')
        data = resp.get_json()
        assert "srt_controller_url" in data
        assert "observer_lat" in data

    def test_post_config(self, client):
        cfg = {"srt_controller_url": "http://10.0.0.99", "observer_lat": 51.5}
        resp = client.post('/api/config',
                           data=json.dumps(cfg),
                           content_type='application/json')
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        # Verify it persisted
        resp = client.get('/api/config')
        data = resp.get_json()
        assert data["srt_controller_url"] == "http://10.0.0.99"
        assert data["observer_lat"] == 51.5

    def test_camera_snapshot_returns_the_frame_with_its_capture_time(self, client):
        def fake_pipewire(workdir, width, height, target, frames):
            frame = os.path.join(workdir, "frame001.jpg")
            with open(frame, "wb") as f:
                f.write(b"\xff\xd8jpeg\xff\xd9")
            return frame, ""

        with patch.object(sched, '_capture_via_pipewire', side_effect=fake_pipewire):
            resp = client.get('/api/camera/snapshot')

        assert resp.status_code == 200
        assert resp.mimetype == 'image/jpeg'
        assert resp.data == b"\xff\xd8jpeg\xff\xd9"
        assert resp.headers['X-Capture-Source'] == 'pipewire'
        assert resp.headers['X-Capture-Time'].endswith('Z')
        # A safety camera must never be served from cache.
        assert 'no-store' in resp.headers['Cache-Control']

    def test_camera_snapshot_falls_back_to_the_v4l2_device(self, client):
        """The two capture paths fail in opposite circumstances.

        PipeWire is unavailable when no desktop session is running; the V4L2
        device is busy exactly when one is, because wireplumber holds it open.
        """
        def fake_v4l2(workdir, device, width, height, frames):
            frame = os.path.join(workdir, "frame.jpg")
            with open(frame, "wb") as f:
                f.write(b"v4l2-frame")
            return frame, ""

        with patch.object(sched, '_capture_via_pipewire',
                          return_value=(None, "no PipeWire session")), \
             patch.object(sched, '_capture_via_v4l2', side_effect=fake_v4l2):
            resp = client.get('/api/camera/snapshot')

        assert resp.status_code == 200
        assert resp.data == b"v4l2-frame"
        assert resp.headers['X-Capture-Source'] == 'v4l2'

    def test_camera_warm_up_shrinks_when_the_camera_just_ran(self, client):
        """The sensor keeps its exposure across a quick close and reopen.

        Measured against a settled 15-frame reference on this camera, a single
        frame came back 0.2% darker after 1s idle and 3.0% after 5s, but 11.8%
        after 15s. Paying the full warm-up every second would make auto-refresh
        cost four times what it needs to; skipping it after a long idle would
        serve a visibly dark frame.
        """
        requested = []

        def fake_pipewire(workdir, width, height, target, frames):
            requested.append(frames)
            frame = os.path.join(workdir, "frame001.jpg")
            with open(frame, "wb") as f:
                f.write(b"frame")
            return frame, ""

        with patch.object(sched, '_capture_via_pipewire', side_effect=fake_pipewire), \
             patch.object(sched, '_camera_last_capture', 0.0):
            cold = client.get('/api/camera/snapshot')      # nothing recent
            warm = client.get('/api/camera/snapshot')      # straight after it

        assert requested == [sched.CAMERA_COLD_FRAMES, sched.CAMERA_WARM_FRAMES]
        assert cold.headers['X-Capture-Frames'] == str(sched.CAMERA_COLD_FRAMES)
        assert warm.headers['X-Capture-Frames'] == str(sched.CAMERA_WARM_FRAMES)

    def test_camera_warm_up_is_paid_again_after_a_failed_capture(self, client):
        """A capture that failed says nothing about the state of the sensor."""
        requested = []

        def failing(workdir, width, height, target, frames):
            requested.append(frames)
            return None, "no frame"

        def succeeding(workdir, width, height, target, frames):
            requested.append(frames)
            frame = os.path.join(workdir, "frame001.jpg")
            with open(frame, "wb") as f:
                f.write(b"frame")
            return frame, ""

        with patch.object(sched, '_capture_via_v4l2', return_value=(None, "busy")), \
             patch.object(sched, '_camera_last_capture', 0.0):
            with patch.object(sched, '_capture_via_pipewire', side_effect=succeeding):
                client.get('/api/camera/snapshot')
            with patch.object(sched, '_capture_via_pipewire', side_effect=failing):
                client.get('/api/camera/snapshot')
            with patch.object(sched, '_capture_via_pipewire', side_effect=succeeding):
                client.get('/api/camera/snapshot')

        assert requested[0] == sched.CAMERA_COLD_FRAMES
        assert requested[1] == sched.CAMERA_WARM_FRAMES
        assert requested[-1] == sched.CAMERA_COLD_FRAMES

    def test_camera_snapshot_reports_why_both_paths_failed(self, client):
        with patch.object(sched, '_capture_via_pipewire',
                          return_value=(None, "no PipeWire session")), \
             patch.object(sched, '_capture_via_v4l2',
                          return_value=(None, "/dev/video0 is held by another application")), \
             patch.object(sched, '_camera_last_capture', 0.0):
            resp = client.get('/api/camera/snapshot')

        assert resp.status_code == 503
        error = resp.get_json()["error"]
        assert "no PipeWire session" in error
        assert "held by another application" in error

    def test_get_log(self, client):
        resp = client.get('/api/log')
        data = resp.get_json()
        assert "lines" in data

    def test_predict_pass_no_tle(self, client):
        resp = client.post('/api/predict_pass',
                           data=json.dumps({"tle_text": ""}),
                           content_type='application/json')
        assert resp.status_code == 400

    def test_predict_pass_invalid_tle(self, client):
        resp = client.post('/api/predict_pass',
                           data=json.dumps({"tle_text": "just one line"}),
                           content_type='application/json')
        assert resp.status_code == 400

    def test_fetch_tle_no_query(self, client):
        resp = client.post('/api/fetch_tle',
                           data=json.dumps({"query": ""}),
                           content_type='application/json')
        assert resp.status_code == 400

    def test_stop_when_nothing_running(self, client):
        resp = client.post('/api/stop')
        data = resp.get_json()
        assert data["success"] is False

    @staticmethod
    def _applicable_model(**overrides):
        model = {
            "success": True,
            "n_scans": 6,
            "az_coverage_deg": 140.0,
            "condition_number": 8.0,
            "min_tilt_significance": 6.0,
            "reduced_chi_squared": 1.2,
            "fitted_utc": "2026-08-19T10:00:00Z",
            "alt_offset_deg": 0.35,
            "az_offset_deg": -0.20,
            "tilt_north_deg": 0.12,
            "tilt_east_deg": -0.08,
            "terms": {"IE": 0.35, "IA": -0.20, "AN": 0.12, "AE": -0.08},
            "site_lat": 55.9,
            "site_lon": -4.3,
        }
        model.update(overrides)
        return model

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    def test_apply_posts_the_model_to_the_controller(self, client):
        """The model goes over as itself, in one POST, and nothing else moves.

        It used to arrive as a fictitious observer position plus a write to the
        operator's pointing-offset boxes, which left the web UI reporting a
        location the telescope is not at and put half the calibration in RAM
        that a reboot cleared.
        """
        model = self._applicable_model()
        with patch('sun_scan.load_pointing_model', return_value=model), \
             patch.object(sched, 'srt_api_call',
                          return_value={"ok": True, "model": {"loaded": True}}) as api, \
             patch.object(sched, 'save_config') as save:
            resp = client.post('/api/calday/apply')

        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert [call.args[0] for call in api.call_args_list] == ["/pointing/apply"]
        document = api.call_args_list[0].kwargs["json_body"]
        assert document["version"] == 1
        assert document["terms"] == {"IE": 0.35, "IA": -0.20, "AN": 0.12, "AE": -0.08}
        assert document["fitted_utc"] == "2026-08-19T10:00:00Z"
        assert document["site"] == {"lat": 55.9, "lon": -4.3}
        # The observer position is the true site position and is not rewritten.
        save.assert_not_called()

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    def test_apply_reports_a_controller_rejection(self, client):
        model = self._applicable_model()
        with patch('sun_scan.load_pointing_model', return_value=model), \
             patch.object(sched, 'srt_api_call',
                          return_value={"ok": False, "error": "Malformed value for term IE"}):
            resp = client.post('/api/calday/apply')

        assert resp.status_code == 502
        assert "Malformed value for term IE" in resp.get_json()["error"]

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    def test_clear_erases_the_model_on_the_controller_too(self, client):
        """Deleting the scan history alone leaves the telescope still corrected.

        The controller holds the fitted model in its own flash, so a clear that
        stops at the scheduler leaves the two halves disagreeing about whether a
        calibration is in force.
        """
        with patch('sun_scan.clear_pointing_data') as clear, \
             patch.object(sched, 'srt_api_call', return_value={"ok": True}) as api:
            resp = client.post('/api/calday/clear')

        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert resp.get_json()["controller_cleared"] is True
        clear.assert_called_once()
        assert [call.args[0] for call in api.call_args_list] == ["/pointing/clear"]

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    def test_clear_reports_a_controller_that_kept_its_model(self, client):
        with patch('sun_scan.clear_pointing_data'), \
             patch.object(sched, 'srt_api_call', return_value=None):
            resp = client.post('/api/calday/clear')

        assert resp.status_code == 502
        assert resp.get_json()["partial"] is True
        assert "still" in resp.get_json()["error"]

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    def test_apply_rejects_insignificant_tilt(self, client):
        """Half a day of Sun leaves the tilts degenerate with the offsets."""
        model = self._applicable_model(min_tilt_significance=1.7)
        with patch('sun_scan.load_pointing_model', return_value=model), \
             patch.object(sched, 'srt_api_call') as api:
            resp = client.post('/api/calday/apply')

        assert resp.get_json()["success"] is False
        assert "sigma" in resp.get_json()["error"]
        api.assert_not_called()

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    def test_apply_rejects_model_that_does_not_fit_the_scans(self, client):
        model = self._applicable_model(reduced_chi_squared=24.0)
        with patch('sun_scan.load_pointing_model', return_value=model), \
             patch.object(sched, 'srt_api_call') as api:
            resp = client.post('/api/calday/apply')

        assert resp.get_json()["success"] is False
        assert "chi-squared" in resp.get_json()["error"]
        api.assert_not_called()

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    def test_apply_rejects_a_model_saved_before_the_resident_terms(self, client):
        """A model fitted under the old scheme has no terms to send."""
        model = self._applicable_model()
        del model["terms"]
        with patch('sun_scan.load_pointing_model', return_value=model), \
             patch.object(sched, 'srt_api_call') as api:
            resp = client.post('/api/calday/apply')

        assert resp.get_json()["success"] is False
        assert "fit the model again" in resp.get_json()["error"]
        api.assert_not_called()

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    def test_apply_rejects_legacy_unchecked_model(self, client):
        model = {
            "success": True,
            "n_scans": 3,
            "alt_offset_deg": 0.35,
            "az_offset_deg": -0.20,
            "terms": {"IE": 0.35, "IA": -0.20, "AN": 0.12, "AE": -0.08},
        }
        with patch('sun_scan.load_pointing_model', return_value=model), \
             patch.object(sched, 'srt_api_call') as api:
            resp = client.post('/api/calday/apply')

        assert resp.get_json()["success"] is False
        assert "quality checks" in resp.get_json()["error"]
        api.assert_not_called()


# =============================================================================
# srt_point_telescope (with mocked network)
# =============================================================================

class TestSrtPointTelescope:
    """Tests for telescope pointing command dispatch."""

    @patch.object(sched, 'SRT_CONTROLLER_URL', None)
    def test_no_controller_configured(self):
        obs = {"coord_system": "altaz"}
        assert sched.srt_point_telescope(obs) is True

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    @patch.object(sched, 'srt_api_call', return_value={"ok": True})
    def test_altaz(self, mock_api):
        obs = {"coord_system": "altaz", "coord1_deg": 45, "coord1_min": 0, "coord1_sec": 0,
               "coord2_deg": 180, "coord2_min": 0, "coord2_sec": 0}
        assert sched.srt_point_telescope(obs) is True
        mock_api.assert_called_once()
        endpoint, params = mock_api.call_args[0]
        assert endpoint == "/direct"
        assert params["alt"] == 45.0

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    @patch.object(sched, 'srt_api_call', return_value={"ok": True})
    def test_radec(self, mock_api):
        obs = {"coord_system": "radec", "coord1_deg": 12, "coord1_min": 30, "coord1_sec": 0,
               "coord2_deg": -30, "coord2_min": 0, "coord2_sec": 0}
        assert sched.srt_point_telescope(obs) is True
        args = mock_api.call_args
        assert args[0][0] == "/track/radec"

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    @patch.object(sched, 'srt_api_call', return_value={"ok": True})
    def test_galactic(self, mock_api):
        obs = {"coord_system": "galactic", "coord1_deg": 0, "coord1_min": 0, "coord1_sec": 0,
               "coord2_deg": 0, "coord2_min": 0, "coord2_sec": 0}
        assert sched.srt_point_telescope(obs) is True
        args = mock_api.call_args
        assert args[0][0] == "/track/galactic"

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    @patch.object(sched, 'srt_api_call', return_value={"ok": True})
    def test_sun(self, mock_api):
        obs = {"coord_system": "object", "object_name": "sun"}
        assert sched.srt_point_telescope(obs) is True
        args = mock_api.call_args
        assert args[0][0] == "/track/sun"

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    @patch.object(sched, 'srt_api_call', return_value={"ok": True})
    def test_moon(self, mock_api):
        obs = {"coord_system": "object", "object_name": "moon"}
        assert sched.srt_point_telescope(obs) is True
        args = mock_api.call_args
        assert args[0][0] == "/track/moon"

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    def test_satellite_skips_api(self):
        obs = {"coord_system": "satellite"}
        assert sched.srt_point_telescope(obs) is True

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    def test_unknown_coord_system(self):
        obs = {"coord_system": "wibble", "coord1_deg": 0, "coord1_min": 0, "coord1_sec": 0,
               "coord2_deg": 0, "coord2_min": 0, "coord2_sec": 0}
        assert sched.srt_point_telescope(obs) is False

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    def test_unknown_object(self):
        obs = {"coord_system": "object", "object_name": "mars"}
        assert sched.srt_point_telescope(obs) is False

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    @patch.object(sched, 'srt_api_call', return_value=None)
    def test_api_failure(self, mock_api):
        obs = {"coord_system": "altaz", "coord1_deg": 45, "coord1_min": 0, "coord1_sec": 0,
               "coord2_deg": 180, "coord2_min": 0, "coord2_sec": 0}
        assert sched.srt_point_telescope(obs) is False


# =============================================================================
# srt_set_calibrator (with mocked network)
# =============================================================================

class TestSrtSetCalibrator:
    """Tests for calibrator control."""

    @patch.object(sched, 'SRT_CONTROLLER_URL', None)
    def test_no_controller(self):
        assert sched.srt_set_calibrator(True) is True

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    @patch.object(sched, 'srt_api_call', return_value={"ok": True})
    def test_turn_on(self, mock_api):
        assert sched.srt_set_calibrator(True) is True
        mock_api.assert_called_once_with("/calibrator", {"on": "1"})

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    @patch.object(sched, 'srt_api_call', return_value={"ok": True})
    def test_turn_off(self, mock_api):
        assert sched.srt_set_calibrator(False) is True
        mock_api.assert_called_once_with("/calibrator", {"on": "0"})


# =============================================================================
# srt_go_position (with mocked network)
# =============================================================================

class TestSrtGoPosition:
    """Tests for telescope positioning commands."""

    @patch.object(sched, 'SRT_CONTROLLER_URL', None)
    def test_no_controller(self):
        assert sched.srt_go_position("home", 0, 0) is True

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    @patch.object(sched, 'srt_api_call', return_value={"ok": True})
    def test_go_home(self, mock_api):
        assert sched.srt_go_position("home", 0, 0) is True
        mock_api.assert_called_once_with("/home")

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    @patch.object(sched, 'srt_api_call', return_value={"ok": True})
    def test_go_stow_uses_go_home_not_direct(self, mock_api):
        """Stow is a DRIVE position and must not go through the pointing model.

        /direct takes a TRUE sky position and applies the model. At the default
        zenith stow that is not academic: azimuth is degenerate at the pole and
        the model's tan(alt) term asks for a ~10 deg azimuth correction that
        moves the beam by nothing, so the dish parked at drive azimuth 170
        instead of 180 and swung 10 deg to get there. /go-home sends the
        controller's own stowAlt/stowAz, in drive coordinates, model bypassed.
        """
        assert sched.srt_go_position("stow", 90, 180) is True
        mock_api.assert_called_once_with("/go-home")

    @patch.object(sched, 'srt_api_call', return_value={"ok": True})
    @patch.object(sched, 'srt_get_status', side_effect=[
        {"status": "Homing", "fault_active": False},
        {"status": "Ready", "fault_active": False, "is_slewing": False,
         "alt": 0.0, "az": 0.0},
    ])
    @patch.object(sched.time, 'sleep')
    def test_physical_home_waits_for_ready(self, _sleep, status, api):
        result = sched.srt_home_and_wait(timeout=10)

        assert result["status"] == "Ready"
        api.assert_called_once_with("/home")
        assert status.call_count == 2

    @patch.object(sched, 'srt_api_call', return_value={"ok": True})
    @patch.object(sched, 'srt_get_status', return_value={
        "status": "FAULT", "fault_active": True, "fault": "Altitude motor stalled",
    })
    def test_physical_home_reports_fault(self, _status, _api):
        with pytest.raises(RuntimeError, match="Altitude motor stalled"):
            sched.srt_home_and_wait(timeout=10)


# =============================================================================
# predict_next_pass (requires ephem)
# =============================================================================

@pytest.mark.skipif(not sched.EPHEM_AVAILABLE, reason="PyEphem not installed")
class TestPredictNextPass:
    """Tests for satellite pass prediction."""

    # Use a known TLE epoch that matches a date we can test with
    ISS_TLE = (
        "ISS\n"
        "1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927\n"
        "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537"
    )

    @patch.object(sched, 'get_config_value')
    def test_returns_dict_with_required_fields(self, mock_config):
        """Use a mock observer near the epoch so we get a pass."""
        import ephem

        def config_side_effect(key):
            return {"observer_lat": 55.9, "observer_lon": -4.3,
                    "observer_elevation": 50, "min_elevation": 0}.get(key)
        mock_config.side_effect = config_side_effect

        # Patch ephem.now to return a date near the TLE epoch
        with patch.object(ephem, 'now', return_value=ephem.Date("2008/9/20 12:00:00")):
            result = sched.predict_next_pass(self.ISS_TLE)

        assert result is not None
        assert "name" in result
        assert "rise_time_utc" in result
        assert "max_el" in result
        assert "duration_minutes" in result
        assert result["max_el"] > 0

    def test_invalid_tle_raises(self):
        with pytest.raises(ValueError):
            sched.predict_next_pass("not a tle")


# =============================================================================
# Drift scans
# =============================================================================

class TestDriftBeamTime:
    """The beam-crossing time T is the mid-point of the scheduled slot."""

    def test_midpoint_of_slot(self):
        obs = {"start_date": "2026-08-10", "start_time": "22:00",
               "duration_minutes": 60}
        assert sched.drift_beam_time(obs) == datetime(2026, 8, 10, 22, 30)

    def test_no_date_uses_reference_day(self):
        obs = {"start_time": "22:00", "duration_minutes": 40}
        now = datetime(2026, 8, 11, 12, 0)
        assert sched.drift_beam_time(obs, now=now) == datetime(2026, 8, 11, 22, 20)

    def test_bad_time_falls_back_to_now(self):
        now = datetime(2026, 8, 11, 12, 0)
        obs = {"start_time": "nonsense", "duration_minutes": 30}
        assert sched.drift_beam_time(obs, now=now) == now + timedelta(minutes=15)


class TestDriftPointTelescope:
    """srt_point_telescope for drift entries, with the ephem math mocked out."""

    def _obs(self, **extra):
        obs = {
            "name": "Drift Test", "coord_system": "drift", "drift_frame": "radec",
            "coord1_deg": 23, "coord1_min": 23, "coord1_sec": 24.0,
            "coord2_deg": 58, "coord2_min": 48, "coord2_sec": 0.0,
            "start_date": "2026-08-10", "start_time": "22:00",
            "duration_minutes": 60, "drift_window_min": 30,
        }
        obs.update(extra)
        return obs

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://test")
    @patch.object(sched, 'srt_api_call', return_value={"ok": True})
    @patch.object(sched, 'compute_drift_pointing', return_value=(45.0, 180.0))
    def test_sends_direct_with_computed_pointing(self, mock_compute, mock_api):
        obs = self._obs()
        assert sched.srt_point_telescope(obs) is True
        # /pointing is asked for the model first; with none readable (the
        # stub answers {"ok": True} to everything) the dish is parked at the
        # source's position at T, as before.
        mock_api.assert_called_with("/direct", {"alt": 45.0, "az": 180.0})
        assert "drift_crossing_time" not in obs
        # RA converted as hours (23h 23m 24s = 23.39h), beam time = slot midpoint
        frame, coord1, coord2, beam_time = mock_compute.call_args[0][:4]
        assert frame == "radec"
        assert coord1 == pytest.approx(23.39)
        assert coord2 == pytest.approx(58.8)
        assert beam_time == datetime(2026, 8, 10, 22, 30)
        # Computed pointing is stashed for the observation metadata
        assert obs["drift_alt"] == 45.0
        assert obs["drift_az"] == 180.0
        assert obs["drift_beam_time"] == "2026-08-10 22:30"

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://test")
    @patch.object(sched, 'srt_api_call')
    @patch.object(sched, 'compute_drift_pointing', return_value=(-5.0, 180.0))
    def test_below_horizon_rejected(self, _compute, mock_api):
        assert sched.srt_point_telescope(self._obs()) is False
        mock_api.assert_not_called()

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://test")
    @patch.object(sched, 'srt_api_call')
    @patch.object(sched, 'compute_drift_pointing', return_value=(45.0, 357.0))
    def test_azimuth_dead_zone_rejected(self, _compute, mock_api):
        assert sched.srt_point_telescope(self._obs()) is False
        mock_api.assert_not_called()

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://test")
    @patch.object(sched, 'srt_api_call')
    @patch.object(sched, 'compute_drift_pointing', return_value=None)
    def test_no_ephem_rejected(self, _compute, mock_api):
        assert sched.srt_point_telescope(self._obs()) is False
        mock_api.assert_not_called()

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://test")
    @patch.object(sched, 'srt_api_call', return_value={"ok": True})
    @patch.object(sched, 'compute_drift_pointing', return_value=(45.0, 180.0))
    def test_galactic_frame_passes_degrees(self, mock_compute, _api):
        obs = self._obs(drift_frame="galactic",
                        coord1_deg=111, coord1_min=42, coord1_sec=0.0,
                        coord2_deg=-2, coord2_min=6, coord2_sec=0.0)
        assert sched.srt_point_telescope(obs) is True
        frame, coord1, coord2, _ = mock_compute.call_args[0][:4]
        assert frame == "galactic"
        assert coord1 == pytest.approx(111.7)
        assert coord2 == pytest.approx(-2.1)


# The controller's model on 2026-08-26, and a source drifting west across it.
_TERMS = {"IE": -0.50832, "IA": 1.68546, "AN": -0.46401, "AE": -0.90160,
          "CA": -1.04381, "NPAE": 0.0, "TF": 0.0, "AZSCALE": 0.000673}
_T = datetime(2026, 8, 10, 22, 30)


def _moving_source(frame, c1, c2, when, object_name=''):
    """True alt/az of something crossing alt 43.9 at 0.25 deg of azimuth a minute."""
    return 43.9, 192.2 + 0.25 * (when - _T).total_seconds() / 60.0


class TestDriftParksOnTheGrid:
    """The mount rounds to 0.5 deg; park on the grid point the track passes
    closest to, sent as that point's true position, and record the crossing."""

    def _obs(self):
        return {"coord_system": "drift", "drift_frame": "radec",
                "coord1_deg": 23, "coord1_min": 23, "coord1_sec": 24.0,
                "coord2_deg": 58, "coord2_min": 48, "coord2_sec": 0.0,
                "start_date": "2026-08-10", "start_time": "22:00",
                "duration_minutes": 60}

    @staticmethod
    def _controller(endpoint, params=None, **kw):
        if endpoint == "/pointing":
            return {"loaded": True, "terms": dict(_TERMS)}
        return {"ok": True}

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://test")
    @patch.object(sched, 'srt_api_call', side_effect=_controller.__func__)
    @patch.object(sched, 'compute_drift_pointing', side_effect=_moving_source)
    def test_parks_on_a_grid_point_and_records_the_crossing(self, _compute, mock_api):
        import drift_park
        obs = self._obs()
        assert sched.srt_point_telescope(obs) is True
        assert obs["drift_drive_alt"] % 0.5 == 0 and obs["drift_drive_az"] % 0.5 == 0
        endpoint, params = mock_api.call_args[0][:2]
        assert endpoint == "/direct"
        # What was sent is the true position of that grid point, so the
        # controller's own rounding lands on it exactly.
        d_alt, d_az = drift_park.true_to_drive(params["alt"], params["az"], _TERMS)
        assert (d_alt, d_az) == pytest.approx((obs["drift_drive_alt"], obs["drift_drive_az"]), abs=1e-4)
        assert obs["drift_alt"] == pytest.approx(params["alt"], abs=1e-3)
        crossing = datetime.strptime(obs["drift_crossing_time"], "%Y-%m-%d %H:%M:%S")
        assert abs((crossing - _T).total_seconds()) <= 240
        assert 0.0 <= obs["drift_crossing_offset_deg"] <= 0.36
        # The slot's mid-point is still recorded as what was asked for.
        assert obs["drift_beam_time"] == "2026-08-10 22:30"

    @patch.object(sched, 'srt_get_status', return_value={
        "alt": 44.0, "az": 192.0, "true_alt": 43.84, "true_az": 192.38})
    @patch.object(sched, 'compute_drift_pointing', side_effect=_moving_source)
    def test_confirm_uses_the_position_the_controller_reports(self, _compute, _status):
        obs = dict(self._obs(), drift_drive_alt=44.0, drift_drive_az=192.0)
        sched.confirm_drift_parking(obs)
        # 192.38 is 0.18 deg of azimuth past 192.2: 43 s after T.
        crossing = datetime.strptime(obs["drift_crossing_time"], "%Y-%m-%d %H:%M:%S")
        assert (crossing - _T).total_seconds() == pytest.approx(43, abs=1.5)
        # The miss is the 0.06 deg of altitude the track never reaches.
        assert obs["drift_crossing_offset_deg"] == pytest.approx(0.06, abs=0.005)

    @patch.object(sched, 'srt_get_status', return_value={
        "alt": 44.5, "az": 192.0, "true_alt": 44.34, "true_az": 192.38})
    @patch.object(sched, 'compute_drift_pointing', side_effect=_moving_source)
    def test_confirm_warns_when_the_mount_parked_elsewhere(self, _compute, _status, caplog):
        obs = dict(self._obs(), drift_drive_alt=44.0, drift_drive_az=192.0)
        with caplog.at_level(logging.WARNING, logger="scheduler"):
            sched.confirm_drift_parking(obs)
        assert any("disagrees with the controller" in r.message for r in caplog.records)
        assert obs["drift_drive_alt"] == 44.5


@pytest.mark.skipif(not sched.EPHEM_AVAILABLE, reason="PyEphem not installed")
class TestDriftPointingMath:
    """compute_drift_pointing against independent PyEphem computations."""

    OBSERVER = {"observer_lat": 55.9, "observer_lon": -4.3,
                "observer_elevation": 50, "min_elevation": 10.0}

    def _patch_config(self):
        return patch.object(sched, 'get_config_value',
                            side_effect=lambda key: self.OBSERVER.get(key))

    def test_radec_matches_independent_ephem(self):
        import ephem
        when = datetime(2026, 8, 10, 22, 30)
        with self._patch_config():
            alt, az = sched.compute_drift_pointing('radec', 23.39, 58.8, when)

            observer = sched._get_observer()
            observer.date = sched._local_to_ephem_utc(when)
        body = ephem.FixedBody()
        body._ra = math.radians(23.39 * 15.0)
        body._dec = math.radians(58.8)
        body._epoch = ephem.J2000
        body.compute(observer)

        assert alt == pytest.approx(math.degrees(body.alt), abs=1e-6)
        assert az == pytest.approx(math.degrees(body.az), abs=1e-6)

    def test_galactic_centre_matches_its_radec(self):
        # Galactic (0, 0) is RA 17.7603h, Dec -28.9362 deg (J2000)
        when = datetime(2026, 8, 10, 22, 30)
        with self._patch_config():
            alt_gal, az_gal = sched.compute_drift_pointing('galactic', 0.0, 0.0, when)
            alt_eq, az_eq = sched.compute_drift_pointing('radec', 17.7603, -28.9362, when)
        assert alt_gal == pytest.approx(alt_eq, abs=0.05)
        assert az_gal == pytest.approx(az_eq, abs=0.05)

    def test_source_crosses_pointing_at_beam_time(self):
        # The pointing computed for T must coincide with the source's actual
        # position at T, and differ from its position half an hour earlier.
        when = datetime(2026, 8, 10, 22, 30)
        with self._patch_config():
            alt_t, az_t = sched.compute_drift_pointing('radec', 12.0, 40.0, when)
            alt_early, az_early = sched.compute_drift_pointing(
                'radec', 12.0, 40.0, when - timedelta(minutes=30))
        assert (abs(alt_t - alt_early) > 0.5) or (abs(az_t - az_early) > 0.5)


class TestDriftPreviewAPI:
    """The /api/drift_preview endpoint."""

    @pytest.fixture
    def client(self, tmp_path):
        sched.app.config['TESTING'] = True
        patches = [
            patch.object(sched, 'SCHEDULE_FILE', str(tmp_path / "schedule.json")),
            patch.object(sched, 'CONFIG_FILE', str(tmp_path / "config.json")),
        ]
        for p in patches:
            p.start()
        with sched.app.test_client() as client:
            yield client
        for p in patches:
            p.stop()

    def test_missing_params_rejected(self, client):
        if not sched.EPHEM_AVAILABLE:
            pytest.skip("PyEphem not installed")
        resp = client.get('/api/drift_preview?frame=radec')
        assert resp.status_code == 400

    def test_unknown_frame_rejected(self, client):
        if not sched.EPHEM_AVAILABLE:
            pytest.skip("PyEphem not installed")
        resp = client.get('/api/drift_preview?frame=ecliptic&coord1=1&coord2=2&time=12:00')
        assert resp.status_code == 400

    def test_preview_returns_pointing_and_transit(self, client):
        if not sched.EPHEM_AVAILABLE:
            pytest.skip("PyEphem not installed")
        resp = client.get('/api/drift_preview?frame=radec&coord1=23.39&coord2=58.8'
                          '&date=2026-08-10&time=22:30')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        for key in ('alt', 'az', 'reachable', 'warnings',
                    'next_transit_date', 'next_transit_time'):
            assert key in data
        # Dec 58.8 from Glasgow is circumpolar, so always above the horizon
        assert data['alt'] > 0


# =============================================================================
# Observation lifecycle (with mocked subprocess/network)
# =============================================================================

class TestObservationLifecycle:
    """Tests for start/stop observation with mocked dependencies."""

    def setup_method(self):
        """Reset global state before each test."""
        sched.current_process = None
        sched.current_observation = None
        sched.observation_end_time = None
        sched.receiver_boot_process = None

    @patch.object(sched, 'SRT_CONTROLLER_URL', None)
    @patch('subprocess.Popen')
    def test_start_observation(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        obs = {
            "name": "Test",
            "coord_system": "altaz",
            "coord1_deg": 45, "coord1_min": 0, "coord1_sec": 0,
            "coord2_deg": 180, "coord2_min": 0, "coord2_sec": 0,
            "center_freq_mhz": 1420.405,
            "channels": 4096,
            "integration_time_s": 3.0,
            "sdr_type": "demo",
            "gain_db": 40,
            "bandwidth_mhz": 2.4,
            "calibrator": False,
            "duration_minutes": 10,
        }

        with patch.object(sched, 'generate_filename', return_value="/tmp/test.h5"):
            result = sched.start_observation(obs)

        assert result is True
        assert sched.current_process is not None
        assert sched.current_observation is not None
        assert sched.current_observation["name"] == "Test"
        assert sched.observation_end_time is not None

    @patch.object(sched, 'SRT_CONTROLLER_URL', None)
    @patch('subprocess.Popen')
    def test_start_when_already_running(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        obs = {"name": "A", "coord_system": "altaz",
               "coord1_deg": 0, "coord1_min": 0, "coord1_sec": 0,
               "coord2_deg": 0, "coord2_min": 0, "coord2_sec": 0,
               "center_freq_mhz": 1420.405, "channels": 4096,
               "integration_time_s": 3.0, "sdr_type": "demo",
               "gain_db": 40, "bandwidth_mhz": 2.4,
               "calibrator": False, "duration_minutes": 10}

        with patch.object(sched, 'generate_filename', return_value="/tmp/a.h5"):
            sched.start_observation(obs)
        with patch.object(sched, 'generate_filename', return_value="/tmp/b.h5"):
            result = sched.start_observation(obs)
        assert result is False

    def test_stop_when_nothing_running(self):
        assert sched.stop_observation() is False

    @patch.object(sched, 'SRT_CONTROLLER_URL', None)
    @patch('subprocess.Popen')
    def test_stop_running_observation(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        obs = {"name": "StopMe", "coord_system": "altaz",
               "coord1_deg": 0, "coord1_min": 0, "coord1_sec": 0,
               "coord2_deg": 0, "coord2_min": 0, "coord2_sec": 0,
               "center_freq_mhz": 1420.405, "channels": 4096,
               "integration_time_s": 3.0, "sdr_type": "demo",
               "gain_db": 40, "bandwidth_mhz": 2.4,
               "calibrator": False, "duration_minutes": 10}

        with patch.object(sched, 'generate_filename', return_value="/tmp/stop.h5"):
            sched.start_observation(obs)

        result = sched.stop_observation()
        assert result is True
        assert sched.current_process is None
        assert sched.current_observation is None

    @patch.object(sched, 'SRT_CONTROLLER_URL', None)
    @patch('subprocess.Popen')
    def test_duration_override(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        obs = {"name": "Override", "coord_system": "altaz",
               "coord1_deg": 0, "coord1_min": 0, "coord1_sec": 0,
               "coord2_deg": 0, "coord2_min": 0, "coord2_sec": 0,
               "center_freq_mhz": 1420.405, "channels": 4096,
               "integration_time_s": 3.0, "sdr_type": "demo",
               "gain_db": 40, "bandwidth_mhz": 2.4,
               "calibrator": False, "duration_minutes": 60}

        with patch.object(sched, 'generate_filename', return_value="/tmp/ov.h5"):
            sched.start_observation(obs, duration_override=5)

        # End time should be ~5 minutes from now, not 60
        expected = datetime.now() + timedelta(minutes=5)
        assert abs((sched.observation_end_time - expected).total_seconds()) < 5

    @patch.object(sched, 'SRT_CONTROLLER_URL', None)
    @patch.object(sched, 'receiver_python_path', return_value='/tmp/radioconda/bin/python')
    @patch.object(os.path, 'exists', return_value=True)
    @patch('subprocess.Popen')
    def test_start_observation_uses_receiver_python(self, mock_popen, mock_exists, mock_receiver_python):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        obs = {"name": "ReceiverPy", "coord_system": "altaz",
               "coord1_deg": 0, "coord1_min": 0, "coord1_sec": 0,
               "coord2_deg": 0, "coord2_min": 0, "coord2_sec": 0,
               "center_freq_mhz": 1420.405, "channels": 4096,
               "integration_time_s": 3.0, "sdr_type": "demo",
               "gain_db": 40, "bandwidth_mhz": 2.4,
               "calibrator": False, "duration_minutes": 10}

        with patch.object(sched, 'PYTHON_PATH', 'python'):
            with patch.object(sched, 'generate_filename', return_value="/tmp/receiver_py.h5"):
                assert sched.start_observation(obs) is True

        cmd = mock_popen.call_args.args[0]
        assert cmd[0] == '/tmp/radioconda/bin/python'

    @patch.object(sched, 'SRT_CONTROLLER_URL', None)
    @patch.object(sched, 'receiver_python_path', return_value='/tmp/radioconda/bin/python')
    @patch.object(os.path, 'exists', return_value=True)
    @patch('subprocess.Popen')
    def test_start_observation_stops_manual_receiver_first(self, mock_popen, mock_exists, mock_receiver_python):
        manual_proc = MagicMock()
        manual_proc.poll.return_value = None
        manual_proc.wait.return_value = 0
        sched.receiver_boot_process = manual_proc

        obs_proc = MagicMock()
        obs_proc.poll.return_value = None
        mock_popen.return_value = obs_proc

        obs = {"name": "TakesOver", "coord_system": "altaz",
               "coord1_deg": 0, "coord1_min": 0, "coord1_sec": 0,
               "coord2_deg": 0, "coord2_min": 0, "coord2_sec": 0,
               "center_freq_mhz": 1420.405, "channels": 4096,
               "integration_time_s": 3.0, "sdr_type": "demo",
               "gain_db": 40, "bandwidth_mhz": 2.4,
               "calibrator": False, "duration_minutes": 10}

        with patch.object(sched, 'generate_filename', return_value="/tmp/takeover.h5"):
            assert sched.start_observation(obs) is True

        manual_proc.terminate.assert_called_once()
        assert sched.current_process is obs_proc

    @patch.object(sched, 'receiver_python_path', return_value='/tmp/radioconda/bin/python')
    @patch.object(os.path, 'exists', return_value=True)
    @patch('subprocess.Popen')
    def test_receiver_start_does_not_preempt_scheduled_observation(self, mock_popen, mock_exists, mock_receiver_python):
        sched.current_observation = {"name": "Scheduled"}
        sched.current_process = MagicMock()
        sched.current_process.poll.return_value = None
        sched.current_process.pid = 1234

        sched.app.config['TESTING'] = True
        with sched.app.test_client() as client:
            resp = client.post('/api/receiver/start')

        data = resp.get_json()
        assert data["running"] is True
        assert data["source"] == "observation"
        assert data["pid"] == 1234
        mock_popen.assert_not_called()

    @patch.object(sched, 'receiver_python_path', return_value='/tmp/radioconda/bin/python')
    @patch.object(sched, '_same_executable', return_value=False)
    @patch.object(os.path, 'exists', return_value=True)
    @patch.object(os, 'execvpe')
    def test_scheduler_reexecs_under_receiver_python(self, mock_execvpe, mock_exists, mock_same, mock_receiver_python):
        with patch.dict(os.environ, {}, clear=True):
            sched.maybe_reexec_scheduler_under_receiver_python()

        assert mock_execvpe.call_args.args[0] == '/tmp/radioconda/bin/python'


# =============================================================================
# Home the mount first
# =============================================================================

class TestHomingCounters:
    """The Due keeps reporting its position while driving into the stops, so
    the last position line before each 'limit reached' is the counter at the
    stall: first approach = count error since the last homing, re-approach =
    repeatability."""

    LINES = [
        "Homing: Drive to limits...",
        "Alt:12.0 Az:40.5 Ialt:1.2A Iaz:1.3A Status:HOMING -> Alt:0.0 Az:0.0 Cal:OFF",
        "Alt:0.5 Az:20.0 Ialt:1.2A Iaz:1.3A Status:HOMING -> Alt:0.0 Az:0.0 Cal:OFF",
        "Homing: Altitude limit reached",
        "Alt:0.5 Az:-1.5 Ialt:0.0A Iaz:1.3A Status:HOMING -> Alt:0.0 Az:0.0 Cal:OFF",
        "Homing: Azimuth limit reached",
        "Homing: Backing off limits...",
        "Alt:5.0 Az:5.0 Ialt:1.2A Iaz:1.3A Status:HOMING -> Alt:0.0 Az:0.0 Cal:OFF",
        "Homing: Re-approach limits...",
        "Alt:0.0 Az:0.5 Ialt:1.0A Iaz:1.0A Status:HOMING -> Alt:0.0 Az:0.0 Cal:OFF",
        "Homing: Altitude limit reached",
        "Alt:0.0 Az:0.0 Ialt:0.0A Iaz:1.0A Status:HOMING -> Alt:0.0 Az:0.0 Cal:OFF",
        "Homing: Azimuth limit reached",
        "Homing: Moving to home position...",
    ]

    # The firmware carrying issue #24 prints the counter on the limit line
    # itself; reading it there does not depend on catching a status line
    # before the 30-line buffer rolls.
    LINES_PRINTED = [
        "Homing: Drive to limits...",
        "Homing: Altitude limit reached at 1 pulses (0.50 deg)",
        "Homing: Azimuth limit reached at -3 pulses (-1.50 deg)",
        "Homing: Backing off limits...",
        "Homing: Re-approach limits...",
        "Homing: Altitude limit reached at 0 pulses (0.00 deg)",
        "Homing: Azimuth limit reached at 0 pulses (0.00 deg)",
        "Homing: Moving to home position...",
    ]

    def test_reads_the_counter_at_each_stop(self):
        c = sched._homing_counters(self.LINES)
        assert c["first"] == {"alt": 0.5, "az": -1.5}
        assert c["second"] == {"alt": 0.0, "az": 0.0}

    def test_reads_the_counter_printed_on_the_limit_line(self):
        c = sched._homing_counters(self.LINES_PRINTED)
        assert c["first"] == {"alt": 0.5, "az": -1.5}
        assert c["second"] == {"alt": 0.0, "az": 0.0}

    def test_missing_lines_leave_the_axis_out_rather_than_inventing_it(self):
        assert sched._homing_counters([]) == {"first": {}, "second": {}}
        # The bare prefix, no number and no preceding status line: unknown.
        assert sched._homing_counters(["Homing: Azimuth limit reached"]) == {"first": {}, "second": {}}

    def test_reads_the_error_the_controller_latched_on_status(self):
        """The controller latches the homing error on /status (issue #24),
        which survives the status flood that scrolls it out of the log."""
        status = {"last_homing": {"alt_error_first_deg": -0.5, "az_error_first_deg": -1.0,
                                  "alt_error_second_deg": 0.0, "az_error_second_deg": 0.5,
                                  "utc": 1787825741}}
        assert sched._homing_counters_from_status(status) == {
            "first": {"alt": -0.5, "az": -1.0}, "second": {"alt": 0.0, "az": 0.5}}
        # A controller too old to carry it, or before any homing.
        assert sched._homing_counters_from_status({}) is None
        assert sched._homing_counters_from_status({"last_homing": None}) is None


class TestHomeFirst:
    """An entry with home_first runs the physical homing before pointing, and
    a homing that fails is a reason not to observe."""

    OBS = {"name": "HomeFirst", "coord_system": "altaz",
           "coord1_deg": 45, "coord1_min": 0, "coord1_sec": 0,
           "coord2_deg": 180, "coord2_min": 0, "coord2_sec": 0,
           "center_freq_mhz": 1420.405, "channels": 4096,
           "integration_time_s": 3.0, "sdr_type": "demo",
           "gain_db": 40, "bandwidth_mhz": 2.4,
           "calibrator": False, "duration_minutes": 10, "home_first": True}

    def setup_method(self):
        sched.current_process = None
        sched.current_observation = None
        sched.observation_end_time = None
        sched.receiver_boot_process = None
        sched.observation_starting = False
        sched.start_abort.clear()

    teardown_method = setup_method

    @patch.object(sched, 'SRT_CONTROLLER_URL', 'http://controller')
    @patch('subprocess.Popen')
    def test_homes_before_pointing_and_records_the_count_error(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        order = []
        report = {"alt": 0.0, "az": 0.0, "status": "Ready",
                  "counters": {"first": {"alt": 0.5, "az": -1.5}, "second": {"alt": 0.0, "az": 0.0}}}
        with patch.object(sched, 'srt_home_with_report',
                          side_effect=lambda **kw: (order.append('home'), report)[1]), \
             patch.object(sched, 'srt_point_telescope',
                          side_effect=lambda obs: (order.append('point'), True)[1]), \
             patch.object(sched, 'srt_wait_for_slew', return_value=True), \
             patch.object(sched, 'srt_set_calibrator', return_value=True), \
             patch.object(sched, 'generate_filename', return_value='/tmp/h.h5'):
            assert sched.start_observation(dict(self.OBS)) is True
        assert order == ['home', 'point']
        meta = json.loads(mock_popen.call_args.kwargs['env']['H1_OBS_METADATA'])
        assert meta['homed_first'] is True
        assert meta['homing_count_error_alt_deg'] == 0.5
        assert meta['homing_count_error_az_deg'] == -1.5

    @patch.object(sched, 'SRT_CONTROLLER_URL', 'http://controller')
    @patch('subprocess.Popen')
    def test_a_failed_homing_aborts_the_observation(self, mock_popen):
        with patch.object(sched, 'srt_home_with_report',
                          side_effect=RuntimeError("Telescope homing failed: Az stall")), \
             patch.object(sched, 'srt_point_telescope', return_value=True) as point:
            assert sched.start_observation(dict(self.OBS)) is False
        point.assert_not_called()
        mock_popen.assert_not_called()

    @patch.object(sched, 'SRT_CONTROLLER_URL', 'http://controller')
    @patch('subprocess.Popen')
    def test_without_the_flag_nothing_is_homed(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        obs = dict(self.OBS, home_first=False)
        with patch.object(sched, 'srt_home_with_report') as home, \
             patch.object(sched, 'srt_point_telescope', return_value=True), \
             patch.object(sched, 'srt_wait_for_slew', return_value=True), \
             patch.object(sched, 'srt_set_calibrator', return_value=True), \
             patch.object(sched, 'generate_filename', return_value='/tmp/h.h5'):
            assert sched.start_observation(obs) is True
        home.assert_not_called()
        meta = json.loads(mock_popen.call_args.kwargs['env']['H1_OBS_METADATA'])
        assert meta['homed_first'] is False


# =============================================================================
# Start/stop responsiveness during slews
# =============================================================================

class TestNonBlockingStart:
    """The slew wait must not hold process_lock, and stop must abort it."""

    OBS = {"name": "SlewTest", "coord_system": "altaz",
           "coord1_deg": 45, "coord1_min": 0, "coord1_sec": 0,
           "coord2_deg": 180, "coord2_min": 0, "coord2_sec": 0,
           "center_freq_mhz": 1420.405, "channels": 4096,
           "integration_time_s": 3.0, "sdr_type": "demo",
           "gain_db": 40, "bandwidth_mhz": 2.4,
           "calibrator": False, "duration_minutes": 10}

    def setup_method(self):
        sched.current_process = None
        sched.current_observation = None
        sched.observation_end_time = None
        sched.receiver_boot_process = None
        sched.observation_starting = False
        sched.start_abort.clear()

    teardown_method = setup_method

    @patch.object(sched, 'SRT_CONTROLLER_URL', 'http://controller')
    @patch('subprocess.Popen')
    def test_process_lock_free_during_slew_wait(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        lock_free = {}

        def fake_wait(timeout=None, cancel_event=None):
            # /api/status and /api/stop must be able to take the lock while
            # the telescope is still slewing.
            acquired = sched.process_lock.acquire(timeout=1)
            lock_free['value'] = acquired
            if acquired:
                sched.process_lock.release()
            return True

        with patch.object(sched, 'srt_point_telescope', return_value=True), \
             patch.object(sched, 'srt_wait_for_slew', side_effect=fake_wait), \
             patch.object(sched, 'srt_set_calibrator', return_value=True), \
             patch.object(sched, 'generate_filename', return_value='/tmp/s.h5'):
            assert sched.start_observation(dict(self.OBS)) is True

        assert lock_free['value'] is True
        assert sched.observation_starting is False

    @patch.object(sched, 'SRT_CONTROLLER_URL', 'http://controller')
    @patch('subprocess.Popen')
    def test_stop_aborts_inflight_start(self, mock_popen):
        def fake_wait(timeout=None, cancel_event=None):
            # Operator hits stop while the dish is still slewing
            assert sched.stop_observation() is True
            return not cancel_event.is_set()

        with patch.object(sched, 'srt_point_telescope', return_value=True), \
             patch.object(sched, 'srt_wait_for_slew', side_effect=fake_wait), \
             patch.object(sched, 'srt_set_calibrator', return_value=True), \
             patch.object(sched, 'generate_filename', return_value='/tmp/s.h5'):
            assert sched.start_observation(dict(self.OBS)) is False

        mock_popen.assert_not_called()
        assert sched.current_process is None
        assert sched.observation_starting is False

    def test_second_start_rejected_while_starting(self):
        sched.observation_starting = True
        try:
            assert sched.start_observation(dict(self.OBS)) is False
        finally:
            sched.observation_starting = False

    def test_wait_for_slew_honours_cancel_event(self):
        cancel = sched.threading.Event()
        cancel.set()
        with patch.object(sched, 'SRT_CONTROLLER_URL', 'http://controller'), \
             patch.object(sched.time, 'sleep'), \
             patch.object(sched, 'srt_get_status',
                          return_value={'is_slewing': True,
                                        'alt': 0, 'az': 0}):
            assert sched.srt_wait_for_slew(timeout=30,
                                           cancel_event=cancel) is False


# =============================================================================
# Sun scan preemption by scheduled observations
# =============================================================================

class TestScanPreemption:
    """A new scheduled observation always wins over a running Sun scan."""

    OBS = {"name": "Preempt", "coord_system": "altaz",
           "coord1_deg": 45, "coord1_min": 0, "coord1_sec": 0,
           "coord2_deg": 180, "coord2_min": 0, "coord2_sec": 0,
           "center_freq_mhz": 1420.405, "channels": 4096,
           "integration_time_s": 3.0, "sdr_type": "demo",
           "gain_db": 40, "bandwidth_mhz": 2.4,
           "calibrator": False, "duration_minutes": 10}

    def setup_method(self):
        sched.current_process = None
        sched.current_observation = None
        sched.observation_end_time = None
        sched.receiver_boot_process = None
        sched.observation_starting = False
        sched.start_abort.clear()
        sched.sun_scan_cancel.clear()
        sched.cal_day_cancel.clear()
        sched.sun_scan_state["running"] = False
        sched.cal_day_state["running"] = False

    teardown_method = setup_method

    @patch.object(sched, 'SRT_CONTROLLER_URL', None)
    @patch('subprocess.Popen')
    def test_scheduled_start_cancels_running_scan(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        sched.sun_scan_state["running"] = True

        def scan_notices_cancel(seconds):
            assert sched.sun_scan_cancel.is_set()
            sched.sun_scan_state["running"] = False

        with patch.object(sched.time, 'sleep', side_effect=scan_notices_cancel), \
             patch.object(sched, 'generate_filename', return_value='/tmp/p.h5'):
            assert sched.start_observation(dict(self.OBS)) is True

        assert sched.sun_scan_cancel.is_set()
        assert sched.current_process is not None

    @patch.object(sched, 'SRT_CONTROLLER_URL', None)
    @patch('subprocess.Popen')
    def test_preempt_gives_up_if_scan_never_stops(self, mock_popen):
        sched.sun_scan_state["running"] = True

        with patch.object(sched, 'SUN_SCAN_PREEMPT_TIMEOUT', 0), \
             patch.object(sched.time, 'sleep'), \
             patch.object(sched, 'generate_filename', return_value='/tmp/p.h5'):
            assert sched.start_observation(dict(self.OBS)) is False

        mock_popen.assert_not_called()


# =============================================================================
# Config/CORS/filename hardening
# =============================================================================

class TestApiHardening:
    def test_config_rejects_unknown_keys(self):
        client = sched.app.test_client()
        resp = client.post('/api/config', json={'platformio_path': '',
                                                'not_a_real_key': 'x'})
        assert resp.status_code == 400
        assert 'not_a_real_key' in resp.get_json()['error']

    def test_config_accepts_known_keys(self):
        saved = {}
        prev = (sched.SRT_CONTROLLER_URL, sched.SRT_SLEW_TIMEOUT,
                sched.SRT_POSITION_TOLERANCE, sched.PYTHON_PATH)
        try:
            with patch.object(sched, 'load_config',
                              return_value=dict(sched._DEFAULT_CONFIG)), \
                 patch.object(sched, 'save_config',
                              side_effect=lambda c: saved.update(c)), \
                 patch.object(sched, 'sync_observer_from_controller'):
                client = sched.app.test_client()
                resp = client.post('/api/config', json={'slew_timeout': 123})
            assert resp.status_code == 200
            assert saved['slew_timeout'] == 123
        finally:
            (sched.SRT_CONTROLLER_URL, sched.SRT_SLEW_TIMEOUT,
             sched.SRT_POSITION_TOLERANCE, sched.PYTHON_PATH) = prev

    def test_cors_denied_for_unknown_origin(self):
        client = sched.app.test_client()
        resp = client.get('/api/config',
                          headers={'Origin': 'http://evil.example'})
        assert 'Access-Control-Allow-Origin' not in resp.headers

    def test_cors_allowed_for_controller_origin(self):
        with patch.object(sched, '_controller_url_candidates',
                          return_value=['http://192.0.2.120']):
            client = sched.app.test_client()
            resp = client.get('/api/config',
                              headers={'Origin': 'http://192.0.2.120'})
        assert (resp.headers.get('Access-Control-Allow-Origin')
                == 'http://192.0.2.120')

    def test_filename_escape_is_contained(self, tmp_path):
        with patch.object(sched, 'get_config_value',
                          return_value=str(tmp_path)):
            path = sched.generate_filename(
                {'filename': '../../evil.h5', 'name': 'x'})
        real = os.path.realpath(path)
        assert real.startswith(os.path.realpath(str(tmp_path)) + os.sep)
        assert 'evil' not in os.path.basename(real)

    def test_filename_plain_name_kept(self, tmp_path):
        with patch.object(sched, 'get_config_value',
                          return_value=str(tmp_path)):
            path = sched.generate_filename({'filename': 'mine.h5'})
        assert os.path.basename(path) == 'mine.h5'
        assert os.path.realpath(path).startswith(
            os.path.realpath(str(tmp_path)) + os.sep)


# =============================================================================
# SIGTERM handling
# =============================================================================

def test_sigterm_handler_raises_system_exit():
    """SIGTERM must unwind main() so the receiver subprocess is stopped
    rather than orphaned holding the B210."""
    import signal as _signal
    with pytest.raises(SystemExit):
        sched._handle_sigterm(_signal.SIGTERM, None)


# =============================================================================
# Scheduler start-failure backoff
# =============================================================================

class TestStartFailureBackoff:
    """A crash-looping receiver must not be respawned every 5 s all slot."""

    def setup_method(self):
        sched._failed_starts.clear()
        sched.current_process = None
        sched.current_observation = None
        sched.observation_end_time = None

    def teardown_method(self):
        sched.scheduler_running = False
        sched._failed_starts.clear()
        sched.current_process = None
        sched.current_observation = None
        sched.observation_end_time = None

    def _due_obs(self):
        start = datetime.now() - timedelta(minutes=5)
        return {
            "name": "crashy",
            "start_date": start.strftime("%Y-%m-%d"),
            "start_time": start.strftime("%H:%M"),
            "duration_minutes": 60,
            "enabled": True,
        }

    def test_gives_up_after_max_failed_starts(self):
        obs = self._due_obs()
        ticks = 8  # well past the failure limit

        with patch.object(sched, "load_schedule", return_value=[obs]), \
             patch.object(sched, "start_observation", return_value=False) as start, \
             patch.object(sched, "stop_observation"), \
             patch.object(sched.time, "sleep",
                          side_effect=[None] * (ticks - 1) + [StopIteration()]):
            sched.scheduler_running = True
            with pytest.raises(StopIteration):
                sched.scheduler_thread()

        assert start.call_count == sched.MAX_START_FAILURES

    def test_receiver_early_exit_counts_and_cleans_up(self):
        obs = self._due_obs()
        dead = MagicMock()
        dead.poll.return_value = 1  # process has exited

        with patch.object(sched, "load_schedule", return_value=[obs]), \
             patch.object(sched, "start_observation", return_value=True) as start, \
             patch.object(sched, "stop_observation") as stop, \
             patch.object(sched.time, "sleep", side_effect=[StopIteration()]):
            sched.scheduler_running = True
            sched.current_process = dead
            sched.current_observation = dict(obs)
            with pytest.raises(StopIteration):
                sched.scheduler_thread()

        stop.assert_called_once()
        assert sched._failed_starts[sched._slot_key(obs)] == 1
        start.assert_called_once()

    def test_backoff_is_per_slot(self):
        obs = self._due_obs()
        for _ in range(sched.MAX_START_FAILURES):
            sched._record_start_failure(obs, "test")
        assert sched._too_many_start_failures(obs)

        tomorrow = dict(obs)
        tomorrow["start_date"] = (
            datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        assert not sched._too_many_start_failures(tomorrow)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


class TestScheduledHorizonScan:
    """A horizon scan is schedulable on the same terms as a calibration day."""

    def _restore(self, obs, end_time):
        sched.current_observation = obs
        sched.observation_end_time = end_time
        sched.horizon_state.clear()
        sched.horizon_state.update({
            "running": False, "progress": 0, "total": 0, "point_info": None,
            "profile": None, "error": None, "started_utc": None,
        })
        sched.horizon_cancel.clear()

    def test_a_scheduled_horizon_observation_starts_the_scan(self):
        obs = dict(sched.DEFAULT_OBSERVATION,
                   name="Horizon", coord_system="horizon",
                   horizon_az_step=10.0, horizon_alt_step=1.5,
                   horizon_integration_s=0.4, duration_minutes=180)
        saved, saved_end = sched.current_observation, sched.observation_end_time
        started = []
        try:
            with patch.object(sched, '_run_horizon_scan',
                              side_effect=lambda p: started.append(p)), \
                 patch.object(sched, 'stop_booted_receiver'), \
                 patch.object(sched, 'current_process', None):
                assert sched.start_observation(obs) is True
                # The thread runs the scan; give it a moment to be scheduled.
                for _ in range(50):
                    if started:
                        break
                    time.sleep(0.02)

            assert started, "the scan thread never ran"
            params = started[0]
            assert params["az_step"] == 10.0
            assert params["alt_step"] == 1.5
            assert params["integration_time_s"] == 0.4
            assert sched.current_observation["name"] == "Horizon"
        finally:
            self._restore(saved, saved_end)

    def test_stopping_it_cancels_the_scan_and_stows(self):
        saved, saved_end = sched.current_observation, sched.observation_end_time
        try:
            sched.current_observation = {"name": "Horizon",
                                         "coord_system": "horizon",
                                         "end_action": "stow"}
            with patch.object(sched, 'SRT_CONTROLLER_URL', 'http://ctrl'), \
                 patch.object(sched, 'srt_go_position') as go:
                assert sched.stop_observation() is True

            assert sched.horizon_cancel.is_set()
            go.assert_called_once_with("stow", 90, 180)
            assert sched.current_observation is None
        finally:
            self._restore(saved, saved_end)

    def test_a_finished_scan_is_not_restarted_in_its_own_slot(self):
        """It finishes well inside its slot; the scheduler must let it lie.

        Same reasoning as the calibration day: the slot owns the run, so a scan
        that completed at 02:30 inside an 01:00-04:00 booking must not be
        started again from the top at 02:31.
        """
        saved = sched.current_observation
        try:
            sched.current_observation = {"name": "Horizon", "coord_system": "horizon"}
            sched.horizon_state["running"] = False
            sched.horizon_state["profile"] = {"n_azimuths": 70}
            # The scheduler-loop test: a completed profile still counts as the
            # slot being occupied.
            occupied = (sched.horizon_state["running"]
                        or bool(sched.horizon_state["profile"]))
            assert occupied is True
        finally:
            self._restore(saved, sched.observation_end_time)

    def test_the_horizon_scan_is_preempted_by_a_real_observation(self):
        """It holds the SDR and the mount, so an observation outranks it.

        The preempt deadline is real wall-clock time, so it is patched to zero
        here: without that this test sits for the full ten-minute timeout,
        because a scan that never stops is exactly what it is simulating.
        """
        assert sched.SUN_SCAN_PREEMPT_TIMEOUT > 0
        saved = dict(sched.horizon_state)
        try:
            sched.horizon_state["running"] = True
            with patch.object(sched, 'stop_booted_receiver'), \
                 patch.object(sched, 'current_process', None), \
                 patch.object(sched, 'SUN_SCAN_PREEMPT_TIMEOUT', 0), \
                 patch.object(sched.time, 'sleep'), \
                 patch.object(sched, 'start_abort', threading.Event()):
                # start_observation gives up once the preempt deadline passes,
                # but it must have asked the scan to stop on the way.
                sched.start_observation(dict(sched.DEFAULT_OBSERVATION,
                                             name="Real", coord_system="altaz"))
            assert sched.horizon_cancel.is_set()
        finally:
            sched.horizon_state.clear()
            sched.horizon_state.update(saved)
            sched.horizon_cancel.clear()
            sched.observation_starting = False


class TestHorizonBandwidth:
    def test_a_scheduled_horizon_scan_carries_its_bandwidth(self):
        """Bandwidth is a free choice for this measurement, so it is a setting.

        At 2.4 MHz and 0.5 s the radiometric precision is 9e-4 and at 1 MHz it
        is 1.4e-3, against a step of order 60% - so narrower costs nothing and
        samples less spectrum either side of the protected band.
        """
        obs = dict(sched.DEFAULT_OBSERVATION, name="Horizon",
                   coord_system="horizon", bandwidth_mhz=1.0)
        started = []
        saved, saved_end = sched.current_observation, sched.observation_end_time
        try:
            with patch.object(sched, '_run_horizon_scan',
                              side_effect=lambda p: started.append(p)), \
                 patch.object(sched, 'stop_booted_receiver'), \
                 patch.object(sched, 'current_process', None):
                assert sched.start_observation(obs) is True
                for _ in range(50):
                    if started:
                        break
                    time.sleep(0.02)
            assert started[0]["bandwidth_mhz"] == 1.0
        finally:
            sched.current_observation = saved
            sched.observation_end_time = saved_end
            sched.horizon_state["running"] = False
            sched.horizon_cancel.clear()


class TestHorizonDefaultsMatchTheStripScan:
    """The scan pattern and the numbers it is given must not drift apart.

    When the cut scan was retired its 1 degree altitude step and 0.5 s
    integration survived in the observation template, the scheduled-scan
    starter and both forms. A scheduled horizon scan would have run the strip
    pattern with the old numbers - eleven times more altitude steps than
    intended, at a quarter of the integration - and nothing would have said so.
    """

    def test_the_observation_template_matches_the_module_defaults(self):
        import horizon_scan as hs
        template = sched.DEFAULT_OBSERVATION
        assert template["horizon_az_step"] == hs.DEFAULT_STRIP_AZ_STEP
        assert template["horizon_alt_step"] == hs.DEFAULT_STRIP_ALT_STEP
        assert template["horizon_alt_start"] == hs.DEFAULT_STRIP_ALT_START
        assert template["horizon_alt_max"] == hs.DEFAULT_STRIP_ALT_MAX
        assert template["horizon_settle_s"] == hs.DEFAULT_SETTLE_S
        assert template["horizon_integration_s"] == hs.DEFAULT_STRIP_INTEGRATION_S
        # not the sun scan's, which shares the observation record
        assert template["integration_time_s"] != template["horizon_integration_s"]

    def test_a_scheduled_scan_is_started_with_the_strip_parameters(self):
        import horizon_scan as hs
        obs = dict(sched.DEFAULT_OBSERVATION, name="H", coord_system="horizon")
        started = []
        saved, saved_end = sched.current_observation, sched.observation_end_time
        try:
            with patch.object(sched, '_run_horizon_scan',
                              side_effect=lambda p: started.append(p)), \
                 patch.object(sched, 'stop_booted_receiver'), \
                 patch.object(sched, 'current_process', None):
                assert sched.start_observation(obs) is True
                for _ in range(50):
                    if started:
                        break
                    time.sleep(0.02)
            params = started[0]
            assert params["alt_step"] == hs.DEFAULT_STRIP_ALT_STEP
            assert params["alt_start"] == hs.DEFAULT_STRIP_ALT_START
            assert params["alt_max"] == hs.DEFAULT_STRIP_ALT_MAX
            assert params["settle_s"] == hs.DEFAULT_SETTLE_S
            assert params["integration_time_s"] == hs.DEFAULT_STRIP_INTEGRATION_S
        finally:
            sched.current_observation = saved
            sched.observation_end_time = saved_end
            sched.horizon_state["running"] = False
            sched.horizon_cancel.clear()

    def test_the_page_offers_the_strip_parameters(self):
        """A field the scan needs but the form omits is a silent default."""
        page = _page_markup()
        for field in ("hzAzStep", "hzAltStep", "hzAltStart", "hzAltMax",
                      "hzSettle", "hzIntegration", "hzHomeEvery"):
            assert 'id="%s"' % field in page, field
        # and none of the cut scan's fields linger
        for gone in ("hzWindow", "hzClearanceFraction"):
            assert 'id="%s"' % gone not in page


def test_form_fields_are_not_restorable_by_the_browser():
    """Browsers restore nameless inputs by position, not by identity.

    Adding six fields to the horizon form shifted every restored value into the
    wrong box: the altitude ceiling showed 300 (the slew timeout), the settle
    time 300 (the homing timeout) and the re-home interval 55.9 (the observer
    latitude). On a page that commands a telescope, a stale value silently
    occupying the wrong field is not a cosmetic problem.
    """
    page = _page_markup()
    for tag in ("<input ", "<select ", "<textarea "):
        for match in re.finditer(re.escape(tag) + r'[^>]*>', page):
            assert 'autocomplete="off"' in match.group(0), match.group(0)[:90]


class TestTuningEndpoint:
    """/api/tuning has no state, so it needs no isolated files."""

    @pytest.fixture
    def client(self):
        sched.app.config['TESTING'] = True
        with sched.app.test_client() as c:
            yield c

    def test_the_page_is_told_what_the_receiver_will_do(self, client):
        resp = client.get('/api/tuning?center_freq_mhz=1420.405752'
                          '&bandwidth_mhz=2.0&channels=327')
        assert resp.status_code == 200
        p = resp.get_json()
        assert p['success'] is True
        assert p['tuned_center_freq_hz'] == pytest.approx(1421.205752e6)
        assert p['sample_rate_raised'] is True
        assert 'raised' in p['description']

    def test_a_malformed_request_is_refused_not_guessed(self, client):
        resp = client.get('/api/tuning?bandwidth_mhz=wide')
        assert resp.status_code == 400


class TestSlewArrival:
    """The mount must be where it was sent before anything records.

    The bug these pin: srt_wait_for_slew returned as soon as `is_slewing` was
    false, which it is for the first seconds after any command because the mount
    has not started moving. On 2026-08-24 a calibration commanded to alt 33
    azimuth 108 was told the slew was complete with the dish still at alt 75
    azimuth 286, and recorded while it swept across the sky.
    """

    def _run(self, statuses, monkeypatch, timeout=30):
        import h1_web_scheduler as sched
        seq = list(statuses)
        seen = []

        def fake_status():
            seen.append(len(seen))
            return seq[min(len(seen) - 1, len(seq) - 1)]

        monkeypatch.setattr(sched, "srt_get_status", fake_status)
        monkeypatch.setattr(sched, "SRT_CONTROLLER_URL", "http://x")
        monkeypatch.setattr(sched.time, "sleep", lambda *_: None)
        # Advance the clock fast enough that the start grace passes.
        clock = {"t": 0.0}

        def fake_time():
            clock["t"] += 1.0
            return clock["t"]

        monkeypatch.setattr(sched.time, "time", fake_time)
        return sched.srt_wait_for_slew(timeout=timeout)

    def test_not_slewing_but_far_from_target_is_not_arrival(self, monkeypatch):
        """The exact 2026-08-24 failure: parked on the previous target."""
        stuck = {"is_slewing": False, "alt": 75.0, "az": 286.5,
                 "target_alt": 33.0, "target_az": 107.0}
        assert self._run([stuck], monkeypatch, timeout=20) is False

    def test_arrival_is_accepted_once_the_position_matches(self, monkeypatch):
        moving = {"is_slewing": True, "alt": 75.0, "az": 286.5,
                  "target_alt": 33.0, "target_az": 107.0}
        there = {"is_slewing": False, "alt": 33.0, "az": 107.2,
                 "target_alt": 33.0, "target_az": 107.0}
        assert self._run([moving, moving, moving, there, there, there],
                         monkeypatch) is True

    def test_half_a_degree_of_quantisation_still_counts_as_arrived(self, monkeypatch):
        """The drive rounds to 0.5 deg, so arrival cannot be tighter than that."""
        there = {"is_slewing": False, "alt": 33.5, "az": 107.5,
                 "target_alt": 33.0, "target_az": 107.0}
        assert self._run([there] * 8, monkeypatch) is True

    def test_azimuth_wrap_is_not_a_huge_error(self, monkeypatch):
        """359.8 and 0.1 are adjacent, not 359 degrees apart."""
        there = {"is_slewing": False, "alt": 40.0, "az": 359.8,
                 "target_alt": 40.0, "target_az": 0.1}
        assert self._run([there] * 8, monkeypatch) is True

    def test_a_controller_with_no_target_falls_back_rather_than_hanging(self, monkeypatch):
        old = {"is_slewing": False, "alt": 40.0, "az": 100.0}
        assert self._run([old] * 8, monkeypatch) is True

    def test_it_gives_up_rather_than_waiting_for_ever(self, monkeypatch):
        never = {"is_slewing": True, "alt": 75.0, "az": 286.5,
                 "target_alt": 33.0, "target_az": 107.0}
        assert self._run([never], monkeypatch, timeout=15) is False

    def test_cancel_is_honoured_while_waiting(self, monkeypatch):
        import threading
        import h1_web_scheduler as sched
        monkeypatch.setattr(sched, "SRT_CONTROLLER_URL", "http://x")
        monkeypatch.setattr(sched, "srt_get_status",
                            lambda: {"is_slewing": True, "alt": 1.0, "az": 1.0,
                                     "target_alt": 50.0, "target_az": 50.0})
        ev = threading.Event()
        ev.set()
        assert sched.srt_wait_for_slew(timeout=30, cancel_event=ev) is False


def test_azimuth_difference_wraps():
    import h1_web_scheduler as sched
    assert sched._az_difference(359.8, 0.1) == pytest.approx(0.3, abs=1e-6)
    assert sched._az_difference(10.0, 350.0) == pytest.approx(20.0, abs=1e-6)
    assert sched._az_difference(90.0, 270.0) == pytest.approx(180.0, abs=1e-6)


class TestSkyFrameArrival:
    """While tracking, arrival means the dish points at what it is tracking.

    The drive-frame target is scraped from the Due's status line, so while the
    mount was still following the previous target it kept reporting that one and
    a drive-frame comparison agreed with itself. /tracking does not lag: the
    controller sets it synchronously inside the request handler.
    """

    def _wait(self, status, tracking, monkeypatch, timeout=20):
        import h1_web_scheduler as sched
        monkeypatch.setattr(sched, "SRT_CONTROLLER_URL", "http://x")
        monkeypatch.setattr(sched, "srt_get_status", lambda: status)
        monkeypatch.setattr(sched, "srt_get_tracking", lambda: tracking)
        monkeypatch.setattr(sched.time, "sleep", lambda *_: None)
        clock = {"t": 0.0}
        monkeypatch.setattr(sched.time, "time",
                            lambda: clock.__setitem__("t", clock["t"] + 1.0) or clock["t"])
        return sched.srt_wait_for_slew(timeout=timeout)

    def test_pointing_at_the_tracked_target_is_arrival(self, monkeypatch):
        assert self._wait(
            {"is_slewing": False, "ra": 16.4702, "dec": 19.53, "alt": 35.0, "az": 110.0,
             "target_alt": 35.0, "target_az": 110.0},
            {"enabled": True, "ra": 16.4651, "dec": 19.24}, monkeypatch) is True

    def test_pointing_somewhere_else_is_not_arrival(self, monkeypatch):
        """The 2026-08-24 failure, in the frame that can actually see it."""
        assert self._wait(
            {"is_slewing": False, "ra": 16.47, "dec": 19.5, "alt": 75.0, "az": 286.5,
             # the drive target agrees with the drive position, as it did then
             "target_alt": 75.0, "target_az": 286.5},
            {"enabled": True, "ra": 10.8, "dec": 57.2}, monkeypatch, timeout=10) is False

    def test_with_tracking_off_it_falls_back_to_the_drive_frame(self, monkeypatch):
        assert self._wait(
            {"is_slewing": False, "alt": 40.0, "az": 100.0,
             "target_alt": 40.0, "target_az": 100.0},
            {"enabled": False}, monkeypatch) is True


def test_sky_separation():
    import h1_web_scheduler as sched
    assert sched._sky_separation_deg(0.0, 0.0, 0.0, 0.0) == pytest.approx(0.0, abs=1e-9)
    # one hour of RA at the celestial equator is fifteen degrees
    assert sched._sky_separation_deg(0.0, 0.0, 1.0, 0.0) == pytest.approx(15.0, abs=1e-6)
    # and shrinks with the cosine of declination
    assert sched._sky_separation_deg(0.0, 60.0, 1.0, 60.0) == pytest.approx(7.5, abs=0.05)
    assert sched._sky_separation_deg(0.0, 10.0, 0.0, -10.0) == pytest.approx(20.0, abs=1e-6)


class TestOneSitePosition:
    """The telescope's position is written down once and read everywhere.

    It had reached nine literals across six files, one of them - the
    simulator's own default - 3.6 km wrong and used for the barycentric
    velocity correction.
    """

    def test_every_consumer_agrees_with_instrument_py(self):
        import observatory
        import rf_calibration
        import sun_scan
        import h1_web_scheduler as sched
        import astro_simulator as A

        lat, lon = observatory.SITE_LAT_DEG, observatory.SITE_LON_DEG
        assert A.SITE_LAT == pytest.approx(lat)
        assert A.SITE_LON == pytest.approx(lon)
        assert sched._DEFAULT_CONFIG["observer_lat"] == pytest.approx(lat)
        assert sched._DEFAULT_CONFIG["observer_lon"] == pytest.approx(lon)
        assert rf_calibration.SITE_LAT_DEG == pytest.approx(lat)
        assert sun_scan.SITE_LAT_DEG == pytest.approx(lat)
        assert sun_scan.SITE_LON_DEG == pytest.approx(lon)

    def test_the_controller_firmware_agrees(self):
        """The one copy that cannot import this: config.h, in C++.

        Two ecosystems means the position genuinely has to be written twice, so
        the protection is a test that reads the other one rather than a comment
        asking people to remember. sun_scan already carried that comment, and a
        comment is not a check.
        """
        import os
        import re
        import observatory
        header = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "esp32_controller_arduino", "src",
            "config.h")
        if not os.path.exists(header):
            pytest.skip("controller firmware not present")
        with open(header) as fh:
            text = fh.read()
        lat = re.search(r"#define\s+OBSERVER_LAT\s+(-?[0-9.]+)", text)
        lon = re.search(r"#define\s+OBSERVER_LON\s+(-?[0-9.]+)", text)
        assert lat and lon, "config.h no longer defines OBSERVER_LAT/LON"
        assert float(lat.group(1)) == pytest.approx(observatory.SITE_LAT_DEG, abs=1e-6)
        assert float(lon.group(1)) == pytest.approx(observatory.SITE_LON_DEG, abs=1e-6)

    def test_one_main_beam_efficiency_too(self):
        """The tabs disagreed by 1/0.7 about the same patch of sky.

        A measured beam already fixes both answers - the beam-weighted
        brightness temperature for extended emission, and the effective area
        through A_e*Omega_A = lambda^2 for a point source - so an efficiency in
        front of either adds back a quantity the beam has determined.
        """
        import observatory
        import rf_calibration
        import instrument
        import astro_simulator as A
        assert instrument.MAIN_BEAM_EFFICIENCY == pytest.approx(1.0)
        assert rf_calibration.MAIN_BEAM_EFFICIENCY == pytest.approx(
            instrument.MAIN_BEAM_EFFICIENCY)
        assert observatory.MAIN_BEAM_EFFICIENCY == pytest.approx(
            instrument.MAIN_BEAM_EFFICIENCY)
        # And the two tools must predict the same sky.
        sim = rf_calibration.load_simulator()
        direct = A.DishSimulator(
            cube_path=None, bw_hz=2.0e6, dish_m=3.0,
            eta=instrument.MAIN_BEAM_EFFICIENCY, nchan=None, tsys=None,
            tint=60.0,
            compact_path=os.path.join(observatory.SIMULATOR_DIR,
                                      "hi4pi_compact.npz.xz"))
        import numpy as np
        a = float(np.nanmax(sim.spectrum(124.0, 36.0)[1]))
        b = float(np.nanmax(direct.spectrum(124.0, 36.0)[1]))
        assert a == pytest.approx(b, rel=0.05)

    def test_the_web_simulator_ships_the_same_constants_as_the_python_one(self):
        """data/meta.json is generated, and generated files go stale silently.

        The web simulator gets its beam, efficiency and site from a JSON bundle
        built by make_web_data.py. Nothing at runtime checks that bundle against
        instrument.py, so when MAIN_BEAM_EFFICIENCY went back to 1.0 the bundle
        kept shipping 0.7 and the Simulator tab reported every antenna
        temperature 30% low - 74.3 K where the scheduler said 106.1 K at
        l=184 b=0. The same file was still carrying the pre-survey site
        position, the last copy of the one that was 3.6 km wrong.

        Regenerate with: python make_web_data.py --meta
        """
        import json
        import os

        import instrument
        import observatory

        meta_path = os.path.join(observatory.SIMULATOR_DIR, "web", "data",
                                 "meta.json")
        with open(meta_path) as fh:
            meta = json.load(fh)

        assert meta["defaults"]["eta"] == instrument.MAIN_BEAM_EFFICIENCY
        # The measured receiver gain instability - without it the web drift
        # scan draws the ideal radiometer floor, 13x quieter than the receiver.
        assert meta["defaults"]["gain_sigma"] == pytest.approx(
            instrument.GAIN_INSTABILITY)
        assert meta["defaults"]["fwhm"] == pytest.approx(
            instrument.beam_fwhm_deg(instrument.DISH_M), abs=5e-4)
        assert meta["defaults"]["dish_m"] == instrument.DISH_M
        assert meta["site"]["name"] == instrument.SITE_NAME
        # The fixed instrument (issue #27): the simulator draws the band a
        # scheduled observation will record, so the bundle must carry the
        # same one tuning.py defines.
        import tuning
        inst = tuning.fixed_instrument()
        assert meta["instrument"]["lo_hz"] == pytest.approx(inst["lo_hz"])
        assert meta["instrument"]["sample_rate_hz"] == pytest.approx(inst["sample_rate_hz"])
        assert meta["instrument"]["h1_band_hz"] == pytest.approx(inst["h1_band_hz"])
        assert meta["instrument"]["continuum_band_hz"] == pytest.approx(inst["continuum_band_hz"])
        assert meta["site"]["lat"] == pytest.approx(instrument.SITE_LAT_DEG)
        assert meta["site"]["lon"] == pytest.approx(instrument.SITE_LON_DEG)
        assert meta["site"]["height"] == pytest.approx(instrument.SITE_HEIGHT_M)

    def test_observatory_module_holds_no_numbers_of_its_own(self):
        """It is plumbing. If numbers appear in it, there are two places again."""
        import os
        import re
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "observatory.py")
        with open(path) as fh:
            body = "".join(l for l in fh if not l.strip().startswith("#"))
        body = re.sub(r'"""[\s\S]*?"""', "", body)
        assert not re.search(r"\b5[0-9]\.[0-9]{4}", body), \
            "observatory.py must import the position, not restate it"


class TestSolarSystemDrift:
    """A drift scan can park for the Sun or Moon by name (2026-08-26)."""

    def test_the_sun_is_parked_for_where_it_will_be(self):
        if not sched.EPHEM_AVAILABLE:
            pytest.skip("no ephem")
        from astropy.coordinates import get_sun, AltAz, EarthLocation
        from astropy.time import Time
        import astropy.units as u
        from observatory import SITE_LAT_DEG, SITE_LON_DEG, SITE_HEIGHT_M
        when = datetime(2026, 8, 26, 13, 55)                      # local
        alt, az = sched.compute_drift_pointing('object', 0, 0, when, 'sun')
        site = EarthLocation(lat=SITE_LAT_DEG*u.deg, lon=SITE_LON_DEG*u.deg, height=SITE_HEIGHT_M*u.m)
        t = Time(sched._local_to_ephem_utc(when))
        aa = get_sun(t).transform_to(AltAz(obstime=t, location=site))
        assert alt == pytest.approx(aa.alt.deg, abs=0.05)
        assert az == pytest.approx(aa.az.deg, abs=0.05)

    def test_an_unknown_object_is_refused(self):
        if not sched.EPHEM_AVAILABLE:
            pytest.skip("no ephem")
        with pytest.raises(ValueError):
            sched.compute_drift_pointing('object', 0, 0, datetime(2026, 8, 26, 13, 55), 'venus')

    def test_the_horizon_trim_can_place_a_solar_drift(self):
        if not sched.EPHEM_AVAILABLE:
            pytest.skip("no ephem")
        obs = {"coord_system": "drift", "drift_frame": "object", "object_name": "sun",
               "coord1_deg": 0, "coord2_deg": 0, "start_date": "2026-08-26",
               "start_time": "13:40", "duration_minutes": 30}
        alt, az = sched.observation_altaz_at(obs, datetime(2026, 8, 26, 13, 55))
        assert 40 < alt < 48 and 185 < az < 200, (alt, az)
