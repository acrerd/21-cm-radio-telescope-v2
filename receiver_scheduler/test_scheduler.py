#!/usr/bin/env python3
"""
Unit tests for h1_web_scheduler.py

Run with: python -m pytest test_scheduler.py -v
"""

import json
import os
import sys
from datetime import datetime, timedelta
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


# =============================================================================
# parse_tle
# =============================================================================

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
    """Tests for observation filename generation."""

    @patch.object(sched, 'get_config_value', return_value="/tmp/test_data")
    @patch('os.makedirs')
    def test_auto_generated_name(self, mock_makedirs, mock_config):
        obs = {"name": "Sun Survey", "calibrator": False}
        result = sched.generate_filename(obs)
        assert result.startswith("/tmp/test_data")
        assert "sun_survey" in result
        assert result.endswith(".h5")

    @patch.object(sched, 'get_config_value', return_value="/tmp/test_data")
    @patch('os.makedirs')
    def test_calibrator_suffix(self, mock_makedirs, mock_config):
        obs = {"name": "Cal Test", "calibrator": True}
        result = sched.generate_filename(obs)
        assert "_cal_" in result

    @patch.object(sched, 'get_config_value', return_value="/tmp/test_data")
    @patch('os.makedirs')
    def test_no_calibrator_suffix(self, mock_makedirs, mock_config):
        obs = {"name": "Normal", "calibrator": False}
        result = sched.generate_filename(obs)
        assert "_cal_" not in result

    @patch.object(sched, 'get_config_value', return_value="/tmp/test_data")
    @patch('os.makedirs')
    def test_explicit_filename(self, mock_makedirs, mock_config):
        obs = {"name": "Test", "filename": "my_file.h5"}
        result = sched.generate_filename(obs)
        assert result.endswith("my_file.h5")

    @patch.object(sched, 'get_config_value', return_value="/tmp/test_data")
    @patch('os.makedirs')
    def test_spaces_replaced(self, mock_makedirs, mock_config):
        obs = {"name": "My Long Name", "calibrator": False}
        result = sched.generate_filename(obs)
        assert "my_long_name" in result
        assert " " not in os.path.basename(result)


# =============================================================================
# Config loading/saving
# =============================================================================

class TestConfig:
    """Tests for configuration persistence."""

    def test_load_config_defaults(self, tmp_path):
        """When no config file exists, defaults are returned."""
        with patch.object(sched, 'CONFIG_FILE', str(tmp_path / "nonexistent.json")):
            cfg = sched.load_config()
            assert cfg["srt_controller_url"] == "http://192.168.106.120"
            assert cfg["observer_lat"] == pytest.approx(55.902444)
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
            assert cfg["srt_controller_url"] == "http://192.168.106.120"


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

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    def test_apply_calibration_updates_location_and_offsets(self, client):
        model = {
            "success": True,
            "n_scans": 6,
            "az_coverage_deg": 140.0,
            "condition_number": 8.0,
            "effective_lat": 55.91,
            "effective_lon": -4.29,
            "alt_offset_deg": 0.35,
            "az_offset_deg": -0.20,
        }
        with patch('sun_scan.load_pointing_model', return_value=model), \
             patch.object(sched, 'srt_api_call', return_value={"ok": True}) as api, \
             patch.object(sched, 'save_config') as save:
            resp = client.post('/api/calday/apply')

        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert [call.args[0] for call in api.call_args_list] == ["/settings/save", "/offset"]
        assert api.call_args_list[1].args[1] == {
            "alt": "0.350000", "az": "-0.200000"}
        save.assert_called_once()

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    def test_apply_rejects_legacy_unchecked_model(self, client):
        model = {
            "success": True,
            "n_scans": 3,
            "effective_lat": 55.91,
            "effective_lon": -4.29,
            "alt_offset_deg": 0.35,
            "az_offset_deg": -0.20,
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
        mock_api.assert_called_once_with("/direct", {"alt": 0, "az": 0})

    @patch.object(sched, 'SRT_CONTROLLER_URL', "http://fake")
    @patch.object(sched, 'srt_api_call', return_value={"ok": True})
    def test_go_stow(self, mock_api):
        assert sched.srt_go_position("stow", 90, 180) is True
        mock_api.assert_called_once_with("/direct", {"alt": 90, "az": 180})


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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
