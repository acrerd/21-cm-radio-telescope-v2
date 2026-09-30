"""The scheduler's watch on the controller: a restart is logged with the
controller's own record of the previous boot, and a tracked observation whose
target the controller has lost is re-pointed (2026-09-30: tracking stopped at
14:09, the controller rebooted at 14:35, and a 'Sun track' recorded blank sky)."""
import logging

import pytest

import h1_web_scheduler as S


class _Alive:
    def poll(self):
        return None


@pytest.fixture
def watch(monkeypatch):
    calls = []
    state = {"status": {"uptime_s": 500, "boot": 3, "reset_reason": "power-on", "loop_age_ms": 5},
             "tracking": {"enabled": True},
             "diag": {"previous_boot": {"uptime_s": 1560, "last_stage": "tracking: sending the target",
                                        "events": ["13:09:01 Tracking enabled"]}}}

    def fake_api(endpoint, params=None, **kw):
        calls.append(endpoint)
        return {"/status": state["status"], "/tracking": state["tracking"], "/diag": state["diag"]}.get(endpoint)

    pointed = []
    monkeypatch.setattr(S, "srt_api_call", fake_api)
    monkeypatch.setattr(S, "srt_point_telescope", lambda obs: pointed.append(obs["name"]) or True)
    monkeypatch.setattr(S, "SRT_CONTROLLER_URL", "http://controller.test")
    monkeypatch.setattr(S, "_controller_watch", {"last": 0.0, "boot": None, "uptime": None, "stall_logged": False})
    monkeypatch.setattr(S, "current_process", _Alive())
    monkeypatch.setattr(S, "current_observation", {"name": "Sun monitor", "coord_system": "object"})

    def tick():
        S._controller_watch["last"] = 0.0
        S.controller_watchdog()
    return state, calls, pointed, tick


def test_a_restart_is_logged_with_the_previous_boot(watch, caplog):
    state, calls, pointed, tick = watch
    tick()
    state["status"] = dict(state["status"], uptime_s=20, boot=4, reset_reason="task watchdog")
    with caplog.at_level(logging.ERROR, logger="scheduler"):
        tick()
    text = caplog.text
    assert "restarted (reset by task watchdog" in text
    assert "last loop stage 'tracking: sending the target'" in text
    assert "Tracking enabled" in text
    assert "/diag" in calls


def test_a_lost_track_is_re_pointed_and_a_kept_one_is_not(watch):
    state, calls, pointed, tick = watch
    tick()
    assert pointed == []
    state["tracking"] = {"enabled": False}
    tick()
    assert pointed == ["Sun monitor"]


def test_a_drift_scan_is_never_re_pointed(watch, monkeypatch):
    state, calls, pointed, tick = watch
    monkeypatch.setattr(S, "current_observation", {"name": "Cas A drift", "coord_system": "altaz"})
    state["tracking"] = {"enabled": False}
    tick()
    assert pointed == [] and "/tracking" not in calls


def test_pulsar_mode_counts_as_tracked():
    assert S._observation_is_tracked({"coord_system": "pulsar"})
    assert S._observation_is_tracked({"coord_system": "radec"})
    assert not S._observation_is_tracked({"coord_system": "altaz"})
    assert not S._observation_is_tracked({"coord_system": "satellite"})
    assert not S._observation_is_tracked(None)


def test_a_stuck_loop_is_logged_once(watch, caplog):
    state, calls, pointed, tick = watch
    state["status"] = dict(state["status"], loop_age_ms=25000)
    with caplog.at_level(logging.ERROR, logger="scheduler"):
        tick()
        tick()
    assert caplog.text.count("loop has not run") == 1
