"""/api/rf/goto: send the dish to the Lockman Hole for a bandpass measurement.

The measurement itself never slews; this is the separate act of getting
there, refused while anything owns the mount and below the minimum
elevation, and warning rather than refusing behind the measured horizon."""

from unittest.mock import patch

import pytest

import h1_web_scheduler as sched


@pytest.fixture
def client():
    sched.app.config["TESTING"] = True
    with sched.app.test_client() as c:
        yield c


def _sky(alt, az):
    """A _sky_position stand-in returning arrays like the real one."""
    return lambda *a, **k: ([alt], [az])


def test_tracks_the_lockman_hole_by_default(client):
    import rf_calibration
    with patch.object(sched, "hardware_in_use", return_value=None), \
         patch.object(sched, "SRT_CONTROLLER_URL", "http://controller"), \
         patch.object(rf_calibration, "_sky_position", _sky(40.0, 300.0)), \
         patch.object(sched, "srt_api_call", return_value={"ok": True}) as api:
        r = client.post("/api/rf/goto", json={})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["glon"] == pytest.approx(sched.LOCKMAN_HOLE_GLON)
    assert d["glat"] == pytest.approx(sched.LOCKMAN_HOLE_GLAT)
    assert (d["alt_deg"], d["az_deg"]) == (40.0, 300.0)
    endpoint, params = api.call_args[0][:2]
    assert endpoint == "/track/galactic"
    assert params == {"l": pytest.approx(149.77), "b": pytest.approx(52.03)}


def test_refused_while_the_mount_is_owned(client):
    with patch.object(sched, "hardware_in_use", return_value="a Sun scan is running"), \
         patch.object(sched, "srt_api_call") as api:
        r = client.post("/api/rf/goto", json={})
    assert r.status_code == 409
    assert "Sun scan" in r.get_json()["error"]
    api.assert_not_called()


def test_refused_below_the_minimum_elevation(client):
    import rf_calibration
    with patch.object(sched, "hardware_in_use", return_value=None), \
         patch.object(sched, "SRT_CONTROLLER_URL", "http://controller"), \
         patch.object(rf_calibration, "_sky_position", _sky(4.0, 10.0)), \
         patch.object(sched, "get_config_value", side_effect=lambda k: 10.0 if k == "min_elevation" else None), \
         patch.object(sched, "srt_api_call") as api:
        r = client.post("/api/rf/goto", json={"glon": 30.0, "glat": -40.0})
    assert r.status_code == 409
    assert "below the 10" in r.get_json()["error"]
    api.assert_not_called()


def test_the_bandpass_job_waits_for_the_slew_before_reading_the_pointing():
    """Pressed 23 s after 'Go to Lockman Hole' on 2026-08-26, the job read
    the direction mid-slew, 12 deg short, and refused the field."""
    order = []

    def wait(timeout=None, cancel_event=None):
        order.append("wait")
        return True

    def status():
        order.append("status")
        return {"gal_l": 149.8, "gal_b": 52.0, "alt": 71.0, "az": 291.0}

    with patch.object(sched, "SRT_CONTROLLER_URL", "http://controller"), \
         patch.object(sched, "stop_booted_receiver"), \
         patch.object(sched, "srt_wait_for_slew", side_effect=wait), \
         patch.object(sched, "srt_get_status", side_effect=status), \
         patch.object(sched, "_rf_emission_outside_mask",
                      return_value={"peak_k": 99.0, "outside_mask_k": 99.0}):
        # The emission stub makes the job refuse the field, which ends the
        # worker before it touches the radio; what matters is the order of
        # the two calls before that.
        sched._run_rf_calibration("bandpass", {"duration_s": 1})
    assert order[:2] == ["wait", "status"]
    assert sched.rf_state["running"] is False
    assert "99.0 K of H I" in (sched.rf_state.get("error") or "")


def test_a_controller_refusal_is_relayed(client):
    import rf_calibration
    with patch.object(sched, "hardware_in_use", return_value=None), \
         patch.object(sched, "SRT_CONTROLLER_URL", "http://controller"), \
         patch.object(rf_calibration, "_sky_position", _sky(40.0, 300.0)), \
         patch.object(sched, "srt_api_call", return_value={"ok": False, "error": "below horizon"}):
        r = client.post("/api/rf/goto", json={})
    assert r.status_code == 502
    assert "below horizon" in r.get_json()["error"]
