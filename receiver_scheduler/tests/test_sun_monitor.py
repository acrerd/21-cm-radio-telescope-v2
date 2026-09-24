"""The Sun monitor (issue #44): track the Sun whenever nothing else wants the telescope.

It is the lowest claimant of all, and everything here is about keeping it
that way: it never runs into a booking, never takes the hardware from a
scan or a receiver started by hand, and gives way - and stays away for a
while - the moment anyone starts something themselves. None of these tests
may start a thread, a process or a slew; the starts are stubbed throughout.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import h1_web_scheduler as sched


@pytest.fixture
def client():
    sched.app.config["TESTING"] = True
    with sched.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def clean_state():
    saved = {name: dict(getattr(sched, name)) for name in
             ("sun_scan_state", "cal_day_state", "horizon_state", "rf_state",
              "sun_monitor_state")}
    saved_proc, saved_obs = sched.current_process, sched.current_observation
    saved_starting = (sched.observation_starting, sched.starting_observation)
    for name in ("sun_scan_state", "cal_day_state", "horizon_state", "rf_state"):
        getattr(sched, name)["running"] = False
    sched.sun_monitor_state.update(holdoff_until=None, holdoff_reason="", waiting="")
    sched.current_process = None
    sched.current_observation = None
    sched.observation_starting, sched.starting_observation = False, None
    yield
    for name, value in saved.items():
        getattr(sched, name).clear()
        getattr(sched, name).update(value)
    sched.current_process, sched.current_observation = saved_proc, saved_obs
    sched.observation_starting, sched.starting_observation = saved_starting


def _running_process():
    proc = MagicMock()
    proc.poll.return_value = None
    return proc


NOON = datetime(2026, 6, 21, 13, 0)        # local; the Sun near 58 deg
MIDNIGHT = datetime(2026, 6, 21, 1, 0)


# --- when, and for how long ---------------------------------------------------

def test_the_sun_is_not_clear_at_night():
    assert sched._sun_clear_minutes(MIDNIGHT, None, 60, 10.0) == 0


def test_the_sun_is_clear_at_noon_in_june_until_the_evening():
    minutes = sched._sun_clear_minutes(NOON, None, 12 * 60, 10.0)
    # Sets through 10 deg around 20:40 BST at midsummer: 7-8 h from 13:00.
    assert 6 * 60 < minutes < 9 * 60


def test_a_measured_floor_ends_the_run_sooner():
    with patch("horizon_store.profile_floors", return_value=[(0, 30.0)]), \
         patch("horizon_store.horizon_floor", return_value=30.0):
        with_floor = sched._sun_clear_minutes(NOON, {"x": 1}, 12 * 60, 10.0)
    assert 0 < with_floor < sched._sun_clear_minutes(NOON, None, 12 * 60, 10.0)


def test_a_run_ends_before_the_next_booking():
    booking = NOON + timedelta(minutes=90)
    schedule = [{"enabled": True, "start_date": booking.strftime("%Y-%m-%d"),
                 "start_time": booking.strftime("%H:%M"), "duration_minutes": 30}]
    minutes, why = sched.sun_monitor_plan(NOON, schedule, None, 10.0)
    assert minutes == 90 - sched.SUN_MONITOR_GUARD_MINUTES
    assert why == ""


def test_no_run_when_a_booking_is_about_to_start():
    booking = NOON + timedelta(minutes=10)
    schedule = [{"enabled": True, "start_date": booking.strftime("%Y-%m-%d"),
                 "start_time": booking.strftime("%H:%M")}]
    minutes, why = sched.sun_monitor_plan(NOON, schedule, None, 10.0)
    assert minutes == 0 and "booking" in why


def test_disabled_and_blocked_bookings_do_not_cut_a_run_short():
    booking = NOON + timedelta(minutes=10)
    base = {"start_date": booking.strftime("%Y-%m-%d"), "start_time": booking.strftime("%H:%M")}
    schedule = [dict(base, enabled=False),
                dict(base, enabled=True, horizon_blocked=True)]
    minutes, _ = sched.sun_monitor_plan(NOON, schedule, None, 10.0)
    assert minutes > 60


def test_no_run_at_night_or_for_a_sliver():
    assert sched.sun_monitor_plan(MIDNIGHT, [], None, 10.0)[0] == 0
    with patch.object(sched, "_sun_clear_minutes", return_value=10):
        minutes, why = sched.sun_monitor_plan(NOON, [], None, 10.0)
    assert minutes == 0 and "only" in why


def test_a_run_ends_before_the_sun_does():
    with patch.object(sched, "_sun_clear_minutes", return_value=120):
        minutes, _ = sched.sun_monitor_plan(NOON, [], None, 10.0)
    assert minutes == 120 - sched.SUN_MONITOR_GUARD_MINUTES


def test_the_entry_is_an_ordinary_solar_track():
    entry = sched.sun_monitor_entry(NOON, 100)
    assert entry["coord_system"] == "object" and entry["object_name"] == "sun"
    assert entry["name"] == sched.SUN_MONITOR_NAME and entry["sun_monitor"] is True
    assert entry["duration_minutes"] == 100 and entry["integration_time_s"] == 3.0
    assert sched.live_plot_kind(entry) == "solar"


def test_a_run_ends_at_the_stow():
    """The dish waits at the stow between runs, not following the Sun into the trees."""
    assert sched.sun_monitor_entry(NOON, 100)["end_action"] == "stow"


# --- the tick -------------------------------------------------------------------

def _tick(enabled=True, plan=(120, ""), busy=None, started=True):
    with patch.object(sched, "load_config", return_value={"sun_monitor": enabled}), \
         patch.object(sched, "hardware_in_use", return_value=busy), \
         patch.object(sched, "sun_monitor_plan", return_value=plan), \
         patch("horizon_store.load_active", return_value=None), \
         patch.object(sched, "start_observation", return_value=started) as start:
        sched._sun_monitor_tick(datetime.now(), [])
    return start


def test_off_by_default():
    assert sched._DEFAULT_CONFIG["sun_monitor"] is False
    assert not _tick(enabled=False).called


def test_starts_when_idle_and_the_sun_is_clear():
    start = _tick()
    assert start.call_count == 1
    entry = start.call_args[0][0]
    assert entry["sun_monitor"] and entry["duration_minutes"] == 120


def test_does_not_start_while_anything_holds_the_hardware():
    assert not _tick(busy="a Sun scan is running").called


def test_does_not_start_when_the_plan_says_no():
    assert not _tick(plan=(0, "the Sun is behind the horizon")).called
    assert sched.sun_monitor_state["waiting"] == "the Sun is behind the horizon"


def test_does_not_start_while_held_off():
    sched.sun_monitor_hold("test")
    assert not _tick().called


def test_a_failed_start_is_not_retried_every_tick():
    _tick(started=False)
    assert sched.sun_monitor_state["holdoff_until"] > datetime.now()
    assert not _tick().called


# --- giving way -------------------------------------------------------------------

def test_yield_stops_a_monitor_run_and_holds_it_off():
    sched.current_process = _running_process()
    sched.current_observation = sched.sun_monitor_entry(NOON, 60)
    with patch.object(sched, "stop_observation") as stop:
        assert sched.sun_monitor_yield("test") is True
    stop.assert_called_once()
    assert sched.sun_monitor_state["holdoff_until"] > datetime.now() + timedelta(minutes=25)
    # Whoever it gave way to commands the mount next: no detour to the zenith.
    assert sched.current_observation["end_action"] == "none"


def test_yield_stops_a_monitor_run_that_is_still_slewing():
    sched.observation_starting = True
    sched.starting_observation = sched.sun_monitor_entry(NOON, 60)
    with patch.object(sched, "stop_observation") as stop:
        assert sched.sun_monitor_yield("test") is True
    stop.assert_called_once()


def test_yield_leaves_anything_else_running():
    sched.current_process = _running_process()
    sched.current_observation = {"name": "Cas A drift", "coord_system": "drift"}
    with patch.object(sched, "stop_observation") as stop:
        assert sched.sun_monitor_yield("test") is False
    assert not stop.called


def test_the_monitor_never_preempts_a_scan_or_a_hand_started_receiver():
    """start_observation cancels scans for a booking; never for the monitor."""
    entry = sched.sun_monitor_entry(NOON, 60)
    for flag in ("sun_scan_state", "cal_day_state", "horizon_state", "rf_state"):
        getattr(sched, flag)["running"] = True
        sched.sun_scan_cancel.clear()
        with patch.object(sched, "stop_booted_receiver") as stop_rx:
            assert sched.start_observation(entry) is False
        assert not sched.sun_scan_cancel.is_set()
        assert not stop_rx.called
        getattr(sched, flag)["running"] = False
    with patch.object(sched, "receiver_boot_process", _running_process()), \
         patch.object(sched, "stop_booted_receiver") as stop_rx:
        assert sched.start_observation(entry) is False
    assert not stop_rx.called


# Every manual path, and what it takes to make it refuse straight after the
# yield so nothing real runs. The yield must come *before* the refusal: a
# monitor run is exactly what would otherwise be refusing it.
MANUAL_PATHS = {
    "Observe start":      ("/api/start", {"name": "x"}, "process"),
    "beam scan":          ("/api/beam/start", {}, "process"),
    "simulator start":    ("/api/simulator/schedule",
                           {"l": 120, "b": 0, "mode": "hi"}, "process"),
    "receiver start":     ("/api/receiver/start", {}, "sun_scan_state"),
    "RF goto":            ("/api/rf/goto", {"target": "zenith"}, "rf_state"),
    "RF calibration":     ("/api/rf/run", {"job": "gain"}, "sun_scan_state"),
    # Horizon checks off, as in test_exclusion.py: a raster refused by the
    # treeline is refused before the yield, and should be - a scan that is
    # not going to run has no business stopping the monitor.
    "Sun scan":           ("/api/sunscan/start",
                           {"n": 5, "respect_local_horizon": False}, "rf_state"),
    "calibration day":    ("/api/calday/start",
                           {"n": 5, "interval_minutes": 30,
                            "respect_local_horizon": False}, "rf_state"),
    "horizon scan":       ("/api/horizon/start", {}, "rf_state"),
}


@pytest.mark.parametrize("path_name", sorted(MANUAL_PATHS))
def test_every_manual_start_makes_the_monitor_give_way(client, path_name):
    path, payload, busy = MANUAL_PATHS[path_name]
    if busy == "process":
        sched.current_process = _running_process()
        sched.current_observation = {"name": "something else"}
    else:
        getattr(sched, busy)["running"] = True
    with patch.object(sched, "sun_monitor_yield") as yield_, \
         patch.object(sched, "start_observation", return_value=False), \
         patch.object(sched.subprocess, "Popen") as popen:
        client.post(path, json=payload)
    assert yield_.called, "%s did not make the Sun monitor give way" % path_name
    assert not popen.called


def test_stop_by_hand_holds_the_monitor_off(client):
    with patch.object(sched, "stop_observation", return_value=True):
        client.post("/api/stop")
    assert sched.sun_monitor_state["holdoff_until"] > datetime.now()


# --- what the operator sees ---------------------------------------------------------

def test_status_reports_the_monitor(client):
    sched.sun_monitor_hold("test")
    body = client.get("/api/status").get_json()
    assert body["sun_monitor"]["holdoff_reason"] == "test"
    assert "enabled" in body["sun_monitor"]


def test_the_switch_is_a_config_key(client):
    """Accepted by /api/config rather than refused as unknown - without writing it."""
    with patch.object(sched, "save_config") as save:
        resp = client.post("/api/config", json={"sun_monitor": True})
    assert resp.status_code == 200
    assert save.call_args[0][0]["sun_monitor"] is True
