"""H I pointing (hi_pointing.py): field choice, the offset fit, the records it
makes, and the scheduler's session - sequencer, reduction hook and the
pointing-data hand-over. Nothing here opens the radio or moves the mount, and
no test reads the HI4PI cube: the sky is an analytic one where it is needed."""

import json
import math
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import h1_web_scheduler as sched
import hi_pointing as H
import rf_calibration as R

T0 = datetime(2026, 10, 27, 20, 0, tzinfo=timezone.utc)


# --- planning ---------------------------------------------------------------

def test_a_plan_takes_strong_fields_spread_over_the_sky_and_repeats_the_first(monkeypatch):
    # every plane field strong, everything off the plane useless
    monkeypatch.setattr(H, "information", lambda l, b, when, sim, h=0.5:
                        (0.03, 0.03, 100.0) if b == 0.0 else (1.0, 1.0, 1.0))
    plan = H.plan_session(T0, 60, profile=None, sim=object())
    assert len(plan) >= 6
    assert all(f["b"] == 0.0 for f in plan)
    assert plan[-1].get("repeat") and (plan[-1]["l"], plan[-1]["b"]) == (plan[0]["l"], plan[0]["b"])
    # the first field is the recording's first minutes: the receiver's
    # warm-up is dropped before it is measured, and only there
    assert plan[0]["settle_s"] == H.FIRST_FIELD_SETTLE_S
    assert plan[0]["dwell_s"] == H.FIELD_DWELL_S + H.FIRST_FIELD_SETTLE_S
    assert all(f["settle_s"] == H.SETTLE_S for f in plan[1:])
    starts = [f["start_s"] for f in plan]
    assert starts == sorted(starts) and starts[-1] + plan[-1]["dwell_s"] <= 60 * 60
    singles = [f for f in plan if not f.get("repeat")]
    for i, a in enumerate(singles):
        assert H.MIN_ALT_DEG <= a["alt"] <= H.MAX_ALT_DEG
        for b in singles[i + 1:]:
            assert H._sep_deg(a["l"], a["b"], b["l"], b["b"]) >= H.MIN_FIELD_SEPARATION_DEG


def test_a_field_behind_the_measured_horizon_is_never_planned(monkeypatch):
    import horizon_store
    monkeypatch.setattr(H, "information", lambda l, b, when, sim, h=0.5: (0.03, 0.03, 100.0))
    # a wall higher than any field is taken, everywhere but the north-east,
    # where the outer plane is rising at 20:00 UTC in late October
    monkeypatch.setattr(horizon_store, "profile_floors", lambda p: [1])
    monkeypatch.setattr(horizon_store, "horizon_floor", lambda p, az: 0.0 if 20 <= az <= 100 else 85.0)
    plan = H.plan_session(T0, 60, profile={"x": 1}, sim=object())
    assert plan and all(20 <= f["az"] <= 100 for f in plan)


def test_nothing_usable_up_means_no_plan(monkeypatch):
    monkeypatch.setattr(H, "information", lambda l, b, when, sim, h=0.5: (1.0, 1.0, 1.0))
    assert H.plan_session(T0, 60, profile=None, sim=object()) == []


# --- the offset fit, on an analytic sky ---------------------------------------

F0 = 1420.405751768e6


def _sky(l, b):
    """A line whose brightness and velocity both change with position: the
    kind of structure the method needs, in two directions."""
    amp = 60.0 + 6.0 * (l - 72.0) + 9.0 * b
    v0 = -20.0 + 4.0 * (l - 72.0) - 3.0 * b          # km/s
    width = 12.0 + 1.5 * b
    return amp, v0, width


def _model(l, b, when, sim=None, bandwidth_hz=2.0e6):
    v = np.linspace(-150, 150, 241)
    amp, v0, w = _sky(l, b)
    ta = amp * np.exp(-0.5 * ((v - v0) / w) ** 2) + 3.0
    f = F0 * (1 - v / 299792.458)
    order = np.argsort(f)
    return f[order], ta[order]


def test_the_fit_finds_a_known_offset(monkeypatch):
    monkeypatch.setattr(R, "simulated_spectrum", _model)
    when = T0
    alt0, az0 = (float(v[0]) for v in H.altaz_of(72.0, 0.0, when))
    true_off = (0.4, -0.3)
    a, z = H.offset_direction(alt0, az0, *true_off)
    gl, gb = H.galactic_of(a, z, when)
    ff, ta = _model(float(gl[0]), float(gb[0]), when)
    freq = np.linspace(ff.min(), ff.max(), 2000)
    rng = np.random.default_rng(1)
    counts = 2.0e-7 * (190.0 + np.interp(freq, ff, ta)) * (1 + 1e-4 * rng.standard_normal(freq.size))
    fit = H.fit_offset(freq, counts, when, 72.0, 0.0, sim=None, bandwidth_hz=2.0e6)
    assert fit["dalt"] == pytest.approx(true_off[0], abs=0.05)
    assert fit["dxel"] == pytest.approx(true_off[1], abs=0.05)
    assert fit["t_sys_k"] == pytest.approx(190.0, abs=3.0)
    assert not fit["at_edge"]


# --- the record --------------------------------------------------------------

def test_a_field_record_pairs_the_drive_reported_with_where_the_h_i_puts_the_beam(monkeypatch):
    monkeypatch.setattr(H, "fit_offset", lambda *a, **k: {
        "dalt": 0.30, "dxel": -0.20, "err_alt_curv": 0.02, "err_xel_curv": 0.02, "at_edge": False,
        "rms_k": 0.8, "correlation": 0.999, "t_sys_k": 190.0, "gain": 1e-7, "bins": 300,
        "rms_zero_k": 1.0, "t_sys_zero_k": 190.0, "alt0": 40.0, "az0": 100.0})
    monkeypatch.setattr(H, "_mean_spectrum", lambda f, c, sel: np.ones(3))
    t0 = T0.timestamp()
    n = 60
    starts = t0 + 3.0 * np.arange(n)
    ends = starts + 3.0
    a, z = H.altaz_of(72.0, 0.0, datetime.fromtimestamp(t0 + 90, tz=timezone.utc))
    samples = [{"unix": t0 + 15 + 5 * i, "alt": float(a[0]) + 4.5, "az": float(z[0]) - 1.0} for i in range(30)]
    step = {"l": 72.0, "b": 0.0, "arrived_unix": t0, "left_unix": t0 + 180, "samples": samples}
    out = H.analyse_field(step, np.ones(3), np.ones((n, 3)), starts, ends, None, 2e6, None)
    r = out["record"]
    assert out["ok"] and r["record_version"] == 2 and r["source"] == "hi" and r["fit_success"]
    # D is the logged drive position, T the commanded direction moved by the offset
    assert r["sun_alt_deg"] + r["alt_error_deg"] == pytest.approx(float(np.mean([s["alt"] for s in samples])), abs=1e-6)
    assert out["true_alt"] - out["fit"]["alt0"] != 0     # computed from the samples' own instants
    assert r["alt_error_deg"] == pytest.approx(4.5 - 0.30, abs=0.05)
    assert r["az_error_sky_deg"] == pytest.approx(r["az_error_deg"] * math.cos(math.radians(r["sun_alt_deg"])))


def test_a_field_with_a_runaway_t_sys_is_kept_out(monkeypatch):
    monkeypatch.setattr(H, "fit_offset", lambda *a, **k: {
        "dalt": 2.0, "dxel": 1.0, "err_alt_curv": 0.02, "err_xel_curv": 0.02, "at_edge": False,
        "rms_k": 0.8, "correlation": 0.999, "t_sys_k": 60.0, "gain": 1e-7, "bins": 300,
        "rms_zero_k": 1.0, "t_sys_zero_k": 190.0, "alt0": 40.0, "az0": 100.0})
    monkeypatch.setattr(H, "_mean_spectrum", lambda f, c, sel: np.ones(3))
    t0 = T0.timestamp()
    starts = t0 + 3.0 * np.arange(60)
    step = {"l": 72.0, "b": 0.0, "arrived_unix": t0, "left_unix": t0 + 180,
            "samples": [{"unix": t0 + 20, "alt": 40.0, "az": 100.0}]}
    out = H.analyse_field(step, np.ones(3), np.ones((60, 3)), starts, starts + 3.0, None, 2e6, None)
    assert not out["ok"] and "T_sys" in out["why"] and not out["record"]["fit_success"]


# --- the scheduler's session -----------------------------------------------

def test_a_session_entry_homes_stows_and_is_never_trimmed():
    e = sched.hi_pointing_entry(datetime(2026, 10, 27, 20, 0), 45)
    assert e["hi_pointing"] and e["home_first"] and e["end_action"] == "stow" and e["duration_minutes"] == 45
    start, duration, note = sched.horizon_visible_window(dict(e, coord1_deg=72.0))
    assert note is None


def test_prepare_points_the_entry_at_the_first_planned_field(monkeypatch):
    plan = [{"l": 65.0, "b": 0.0, "alt": 34.0, "az": 88.0, "start_s": 15.0, "dwell_s": 180.0},
            {"l": 110.0, "b": 4.0, "alt": 40.0, "az": 32.0, "start_s": 230.0, "dwell_s": 180.0}]
    monkeypatch.setattr(H, "plan_session", lambda *a, **k: plan)
    out = sched.prepare_hi_pointing(sched.hi_pointing_entry(datetime.now(), 30))
    assert (out["coord1_deg"], out["coord2_deg"]) == (65.0, 0.0) and out["hi_pointing_plan"] == plan
    # the minutes asked for are an upper limit: the session ends with its fields
    assert out["duration_minutes"] == sched.hi_pointing_minutes_needed(plan) == 8
    long = sched.prepare_hi_pointing(sched.hi_pointing_entry(datetime.now(), 5))
    assert long["duration_minutes"] == 5
    monkeypatch.setattr(H, "plan_session", lambda *a, **k: [])
    assert sched.prepare_hi_pointing(sched.hi_pointing_entry(datetime.now(), 30)) is None


def test_the_sequencer_steps_through_the_fields_and_logs_the_drive(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(sched, "HI_POINTING_SAMPLE_S", 0.01)
    monkeypatch.setattr(sched, "_hi_session_alive", lambda f: True)
    pointed = []
    monkeypatch.setattr(sched, "srt_point_telescope", lambda obs: pointed.append((obs["coord1_deg"], obs["coord2_deg"])) or True)
    monkeypatch.setattr(sched, "srt_wait_for_slew", lambda **k: True)
    monkeypatch.setattr(sched, "srt_get_status", lambda: {"alt": 40.0, "az": 100.0, "true_alt": 35.0, "true_az": 101.0})
    plan = [{"l": 65.0, "b": 0.0, "dwell_s": 0.05}, {"l": 110.0, "b": 4.0, "dwell_s": 0.05},
            {"l": 65.0, "b": 0.0, "dwell_s": 0.05, "repeat": True}]
    sched.run_hi_sequencer("/x/20261027_200000_track.h5", plan)
    steps = json.load(open(H.steps_path("/x/20261027_200000_track.h5")))
    assert [(f["l"], f["b"]) for f in steps["fields"]] == [(65.0, 0.0), (110.0, 4.0), (65.0, 0.0)]
    assert pointed == [(110.0, 4.0), (65.0, 0.0)]          # the first was pointed by start_observation
    assert steps["fields"][2]["repeat"] and all(f["samples"] for f in steps["fields"])
    assert all(f["left_unix"] >= f["arrived_unix"] for f in steps["fields"])
    assert not sched.hi_pointing_state["running"]


class _Thread:
    started = []

    def __init__(self, target=None, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        _Thread.started.append(self.target)


def test_a_session_reduces_itself_when_it_ends():
    saved = (sched.current_process, sched.current_observation, sched.observation_end_time)
    try:
        sched.current_process = MagicMock()
        sched.current_observation = {"name": "H I pointing", "hi_pointing": True, "coord_system": "galactic",
                                     "output_file": "/x/20261027_200000_track.h5", "end_action": "none"}
        _Thread.started = []
        with patch.object(sched.threading, "Thread", _Thread), patch.object(sched, "SRT_CONTROLLER_URL", ""):
            sched.stop_observation()
        assert sched.update_hi_pointing in _Thread.started
    finally:
        sched.current_process, sched.current_observation, sched.observation_end_time = saved


def test_a_reduced_session_is_added_to_the_pointing_data_once(tmp_path, monkeypatch):
    import sun_scan
    monkeypatch.setattr(H, "OUT_DIR", str(tmp_path))
    store = tmp_path / "pointing_data.json"
    store.write_text("[]")
    monkeypatch.setattr(sun_scan, "_POINTING_DATA_FILE", str(store))
    rec = {"timestamp": "2026-10-27T20:01:00+00:00", "sun_alt_deg": 40.0, "sun_az_deg": 100.0,
           "alt_error_deg": 4.2, "az_error_deg": -1.0, "record_version": 2, "fit_success": True, "source": "hi"}
    summary = {"recording": "20261027_200000_track.h5", "fields": [
        {"ok": True, "record": rec}, {"ok": False, "record": dict(rec, timestamp="2026-10-27T20:05:00+00:00")}]}
    with open(H.result_path("20261027_200000_track.h5"), "w") as fh:
        json.dump(summary, fh)
    client = sched.app.test_client()
    for expect in (1, 0):
        r = client.post("/api/hipointing/save", json={"recording": "20261027_200000_track.h5"}).get_json()
        assert r["success"] and r["added"] == expect and r["total"] == 1
    assert json.load(open(store))[0]["recording"] == "20261027_200000_track.h5"
    assert client.post("/api/hipointing/save", json={"recording": "nothing.h5"}).status_code == 404


def test_the_suggested_times_are_at_the_sidereal_times_named():
    from astropy.time import Time
    import astropy.units as u
    after = datetime(2026, 10, 27, 14, 30)
    for lst in (18.0, 7.0, 0.0):
        t = sched.next_lst_time(lst, after)
        assert after <= t <= after + timedelta(hours=24)
        got = Time(t.astimezone(timezone.utc)).sidereal_time("mean", longitude=sched.SITE_LON_DEG * u.deg).hour
        assert abs(((got - lst + 12) % 24) - 12) < 0.02


def test_a_session_is_booked_into_the_schedule(monkeypatch):
    stored = []
    monkeypatch.setattr(sched, "load_schedule", lambda: [])
    monkeypatch.setattr(sched, "_store_schedule", lambda s: (stored.append(s) or ([], [])))
    client = sched.app.test_client()
    when = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    r = client.post("/api/hipointing/book", json={"start": when, "minutes": 50}).get_json()
    assert r["success"]
    entry = stored[0][-1]
    assert entry["hi_pointing"] and entry["duration_minutes"] == 50
    assert "%sT%s" % (entry["start_date"], entry["start_time"]) == when
    past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    assert client.post("/api/hipointing/book", json={"start": past}).status_code == 400
    assert client.post("/api/hipointing/book", json={"start": "soon"}).status_code == 400
