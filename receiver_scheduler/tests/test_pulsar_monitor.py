"""The pulsar monitor: B0329+54 in pulsar mode every night, above the Sun monitor.

One window a night, named by the day it opens; a run stops short of any
booking and resumes after it, every piece tagged with the same session; it
wins over the Sun monitor and gives way to anything by hand. None of these
tests may start a thread, a process or a slew; the starts are stubbed.
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
              "sun_monitor_state", "pulsar_monitor_state")}
    saved_proc, saved_obs = sched.current_process, sched.current_observation
    saved_starting = (sched.observation_starting, sched.starting_observation)
    for name in ("sun_scan_state", "cal_day_state", "horizon_state", "rf_state"):
        getattr(sched, name)["running"] = False
    for name in ("sun_monitor_state", "pulsar_monitor_state"):
        getattr(sched, name).update(holdoff_until=None, holdoff_reason="", waiting="")
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


EVENING = datetime(2026, 9, 28, 21, 0)
AFTER_MIDNIGHT = datetime(2026, 9, 29, 3, 0)
AFTERNOON = datetime(2026, 9, 29, 15, 0)


def _booking(at, minutes=60):
    return {"enabled": True, "start_date": at.strftime("%Y-%m-%d"),
            "start_time": at.strftime("%H:%M"), "duration_minutes": minutes}


# --- the window ---------------------------------------------------------------

def test_a_night_is_one_window_across_midnight():
    for now in (EVENING, AFTER_MIDNIGHT):
        opens, closes, open_now = sched.pulsar_clock_window(now, "20:00", 16)
        assert open_now
        assert opens == datetime(2026, 9, 28, 20, 0)
        assert closes == datetime(2026, 9, 29, 12, 0)


def test_between_windows_it_names_the_next_one():
    opens, _, open_now = sched.pulsar_clock_window(AFTERNOON, "20:00", 16)
    assert not open_now and opens == datetime(2026, 9, 29, 20, 0)


def test_the_session_is_when_the_window_opened():
    _, _, session = sched.pulsar_monitor_plan(AFTER_MIDNIGHT, [], None, 10.0, "20:00", 16)
    assert session == "2026-09-28T2000"


# --- following the pulsar ---------------------------------------------------------

def _north_wall(az):
    """Our northern obstruction, roughly: 50 deg from az 340 through north to 20."""
    return 50.0 if (az >= 340 or az < 20) else 5.0


@pytest.fixture
def north_wall():
    with patch("horizon_store.profile_floors", return_value=[(0, 50.0)]), \
         patch("horizon_store.horizon_floor", side_effect=lambda p, az: _north_wall(az)), \
         patch("horizon_store.beam_margin_deg", return_value=4.3):
        sched._follow_cache.clear()
        yield {"synthetic": True}
        sched._follow_cache.clear()


def test_following_opens_when_the_pulsar_comes_out_from_behind(north_wall):
    """Late September it comes out of the northern obstruction in the evening;
    the window opens there and runs 16 h."""
    opens, closes, open_now = sched.pulsar_follow_window(EVENING, 16, north_wall, 10.0)
    assert open_now
    assert datetime(2026, 9, 28, 18, 30) < opens < datetime(2026, 9, 28, 20, 45)
    assert closes == opens + timedelta(hours=16)
    before, _, open_before = sched.pulsar_follow_window(opens - timedelta(minutes=5), 16,
                                                        north_wall, 10.0)
    assert not open_before and before == opens               # names the coming one


def test_the_window_moves_two_hours_a_month_into_the_day(north_wall):
    sept, _, _ = sched.pulsar_follow_window(datetime(2026, 9, 28, 12, 0), 16, north_wall, 10.0)
    dec, _, _ = sched.pulsar_follow_window(datetime(2026, 12, 28, 6, 0), 16, north_wall, 10.0)
    # 91 days at 3.93 min a day is ~6 h earlier in sidereal terms, and the
    # clocks going back (BST -> GMT, the scheduler works in local time) adds
    # the seventh.
    shift_h = ((sept.hour * 60 + sept.minute) - (dec.hour * 60 + dec.minute)) / 60.0 % 24
    assert shift_h == pytest.approx(91 * 3.93 / 60 + 1.0, abs=0.2)
    assert 11 <= dec.hour <= 14                                # a daytime run by winter


def test_a_window_never_outlasts_the_clear_stretch(north_wall):
    """Asked for longer than the pulsar stays clear, a window closes where it
    goes behind again (4.4 h a day behind this wall; ~6 h behind the real one)."""
    opens, closes, _ = sched.pulsar_follow_window(EVENING, 23, north_wall, 10.0)
    assert closes - opens < timedelta(hours=23)
    edge = sched._pulsar_blocked_minutes(closes - timedelta(minutes=1), 2, north_wall, 10.0)
    assert edge == [False, True]


def test_with_nothing_hiding_it_the_clock_window_is_used():
    cfg = {"pulsar_monitor_window": "follow", "pulsar_monitor_start": "20:00",
           "pulsar_monitor_hours": 16}
    sched._follow_cache.clear()
    opens, closes, open_now = sched.pulsar_window_for(EVENING, cfg, None, 10.0)
    assert opens == datetime(2026, 9, 28, 20, 0) and open_now


def test_a_following_plan_names_its_session_by_the_minute(north_wall):
    minutes, why, session = sched.pulsar_monitor_plan(EVENING + timedelta(hours=1), [],
                                                      north_wall, 10.0, "20:00", 16, "follow")
    assert why == "" and session.startswith("2026-09-28T") and len(session) == 15
    _, closes, _ = sched.pulsar_follow_window(EVENING + timedelta(hours=1), 16, north_wall, 10.0)
    assert minutes == int((closes - (EVENING + timedelta(hours=1))).total_seconds() // 60)


# --- how long -----------------------------------------------------------------

def test_a_run_lasts_to_the_end_of_the_window():
    minutes, why, _ = sched.pulsar_monitor_plan(EVENING, [], None, 10.0, "20:00", 16)
    assert why == "" and minutes == 15 * 60


def test_a_run_ends_before_the_next_booking_and_resumes_after_it():
    booking = EVENING + timedelta(hours=3)
    minutes, _, session = sched.pulsar_monitor_plan(EVENING, [_booking(booking)], None, 10.0,
                                                    "20:00", 16)
    assert minutes == 180 - sched.PULSAR_MONITOR_GUARD_MINUTES
    after = booking + timedelta(minutes=61)
    later, why, later_session = sched.pulsar_monitor_plan(after, [_booking(booking)], None,
                                                          10.0, "20:00", 16)
    assert why == "" and later > 8 * 60 and later_session == session


def test_no_run_outside_the_window_or_for_a_sliver():
    assert sched.pulsar_monitor_plan(AFTERNOON, [], None, 10.0, "20:00", 16)[0] == 0
    near_close = datetime(2026, 9, 29, 11, 45)
    minutes, why, _ = sched.pulsar_monitor_plan(near_close, [], None, 10.0, "20:00", 16)
    assert minutes == 0 and "closes" in why
    minutes, why, _ = sched.pulsar_monitor_plan(
        EVENING, [_booking(EVENING + timedelta(minutes=20))], None, 10.0, "20:00", 16)
    assert minutes == 0 and "booking" in why


def test_b0329_is_circumpolar_here():
    """Dec +54.6 at lat 55.9: never below ~19 deg, so a 10 deg floor never cuts it."""
    minutes = sched._body_clear_minutes(EVENING, sched._pulsar_body(), None, 24 * 60, 10.0)
    assert minutes == 24 * 60


def test_the_entry_is_a_pulsar_run_that_homes_and_stows():
    entry = sched.pulsar_monitor_entry(EVENING, 900, "2026-09-28")
    assert entry["coord_system"] == "pulsar" and entry["object_name"] == "B0329+54"
    assert entry["pulsar_monitor"] is True and entry["pulsar_session"] == "2026-09-28"
    assert entry["home_first"] is True and entry["end_action"] == "stow"
    assert entry["duration_minutes"] == 900


# --- the tick -------------------------------------------------------------------

def _tick(enabled=True, plan=(600, "", "2026-09-28"), busy=None, started=True):
    with patch.object(sched, "load_config", return_value={"pulsar_monitor": enabled}), \
         patch.object(sched, "hardware_in_use", return_value=busy), \
         patch.object(sched, "pulsar_monitor_plan", return_value=plan), \
         patch("horizon_store.load_active", return_value=None), \
         patch.object(sched, "start_observation", return_value=started) as start:
        took = sched._pulsar_monitor_tick(datetime.now(), [])
    return start, took


def test_off_by_default():
    assert sched._DEFAULT_CONFIG["pulsar_monitor"] is False
    start, took = _tick(enabled=False)
    assert not start.called and not took


def test_starts_in_its_window_when_idle():
    start, took = _tick()
    assert took and start.call_count == 1
    entry = start.call_args[0][0]
    assert entry["pulsar_monitor"] and entry["pulsar_session"] == "2026-09-28"


def test_does_not_start_while_anything_holds_the_hardware():
    start, took = _tick(busy="a Sun scan is running")
    assert not start.called and not took


def test_does_not_start_while_held_off():
    sched.pulsar_monitor_hold("test")
    assert not _tick()[0].called


def test_a_failed_start_is_not_retried_every_tick():
    _tick(started=False)
    assert sched.pulsar_monitor_state["holdoff_until"] > datetime.now()
    assert not _tick()[0].called


# --- over the Sun monitor -------------------------------------------------------

def test_a_sun_run_does_not_start_into_the_window():
    noon = datetime(2026, 6, 21, 13, 0)
    opens = datetime(2026, 6, 21, 14, 0)
    minutes, _ = sched.sun_monitor_plan(noon, [], None, 10.0, pulsar_from=opens)
    assert minutes == 60 - sched.SUN_MONITOR_GUARD_MINUTES
    minutes, why = sched.sun_monitor_plan(noon, [], None, 10.0, pulsar_from=noon)
    assert minutes == 0 and "pulsar" in why


def test_a_sun_run_gives_way_when_the_window_opens():
    sched.current_process = _running_process()
    sched.current_observation = sched.sun_monitor_entry(EVENING, 60)
    cfg = {"pulsar_monitor": True, "pulsar_monitor_window": "clock", "pulsar_monitor_start": "20:00", "pulsar_monitor_hours": 16}
    with patch.object(sched, "load_config", return_value=cfg), \
         patch.object(sched, "stop_observation") as stop:
        assert sched._pulsar_takes_over_from_sun(EVENING) is True
    stop.assert_called_once()
    assert sched.current_observation["end_action"] == "none"


def test_a_sun_run_is_left_alone_outside_the_window_or_when_off():
    sched.current_process = _running_process()
    sched.current_observation = sched.sun_monitor_entry(AFTERNOON, 60)
    for cfg in ({"pulsar_monitor": True, "pulsar_monitor_window": "clock", "pulsar_monitor_start": "20:00",
                 "pulsar_monitor_hours": 16}, {"pulsar_monitor": False}):
        with patch.object(sched, "load_config", return_value=cfg), \
             patch.object(sched, "stop_observation") as stop:
            assert sched._pulsar_takes_over_from_sun(AFTERNOON) is False
        assert not stop.called


def test_a_booking_is_never_stopped_for_the_pulsar():
    sched.current_process = _running_process()
    sched.current_observation = {"name": "Cas A drift", "coord_system": "drift"}
    cfg = {"pulsar_monitor": True, "pulsar_monitor_window": "clock", "pulsar_monitor_start": "20:00", "pulsar_monitor_hours": 16}
    with patch.object(sched, "load_config", return_value=cfg), \
         patch.object(sched, "stop_observation") as stop:
        assert sched._pulsar_takes_over_from_sun(EVENING) is False
    assert not stop.called


# --- giving way -------------------------------------------------------------------

def test_yield_stops_a_pulsar_monitor_run_and_holds_it_off():
    sched.current_process = _running_process()
    sched.current_observation = sched.pulsar_monitor_entry(EVENING, 600, "2026-09-28")
    with patch.object(sched, "stop_observation") as stop:
        assert sched.sun_monitor_yield("test") is True
    stop.assert_called_once()
    assert sched.pulsar_monitor_state["holdoff_until"] > datetime.now() + timedelta(minutes=25)
    assert sched.current_observation["end_action"] == "none"


def test_the_pulsar_monitor_never_preempts_a_scan():
    entry = sched.pulsar_monitor_entry(EVENING, 600, "2026-09-28")
    sched.horizon_state["running"] = True
    sched.horizon_cancel.clear()
    with patch.object(sched, "stop_booted_receiver") as stop_rx:
        assert sched.start_observation(entry) is False
    assert not sched.horizon_cancel.is_set() and not stop_rx.called


def test_stop_by_hand_holds_both_monitors_off(client):
    with patch.object(sched, "stop_observation", return_value=True):
        client.post("/api/stop")
    assert sched.pulsar_monitor_state["holdoff_until"] > datetime.now()
    assert sched.sun_monitor_state["holdoff_until"] > datetime.now()


# --- configuration and status -------------------------------------------------------

def test_the_window_is_configured_and_checked(client):
    with patch.object(sched, "save_config") as save:
        ok = client.post("/api/config", json={"pulsar_monitor": True,
                                              "pulsar_monitor_start": "19:30",
                                              "pulsar_monitor_hours": "15.5"})
        assert ok.status_code == 200
        stored = save.call_args[0][0]
        assert stored["pulsar_monitor"] is True
        assert stored["pulsar_monitor_start"] == "19:30" and stored["pulsar_monitor_hours"] == 15.5
        assert client.post("/api/config", json={"pulsar_monitor_start": "25:00"}).status_code == 400
        assert client.post("/api/config", json={"pulsar_monitor_hours": 24}).status_code == 400


def test_status_reports_the_pulsar_monitor(client):
    sched.pulsar_monitor_hold("test")
    body = client.get("/api/status").get_json()
    assert body["pulsar_monitor"]["holdoff_reason"] == "test"
    assert body["pulsar_monitor"]["start"]


def test_the_session_reaches_the_recording():
    """The night tag travels in H1_OBS_METADATA, so the file carries it."""
    import inspect
    src = inspect.getsource(sched.start_observation)
    assert "'pulsar_session': obs.get('pulsar_session'" in src
