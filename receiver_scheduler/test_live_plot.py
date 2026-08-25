"""The live plot on the Observe tab, and the route table it is served from.

Two things are pinned here.

**The routes.** Adding `live_plot_kind` above `api_observe_live` on 2026-08-25
put it *between* the `@app.route` decorator and the function it was meant to
decorate, so Flask registered the helper as the view for `/api/observe/live`.
Every request became a 500 - `live_plot_kind() missing 1 required positional
argument` - and the whole suite still passed, because no test called that
endpoint. It was found by opening the page.

The general form is worth guarding rather than the instance: a Flask view takes
only the variables in its own URL rule, so any view wanting an argument that
the rule does not supply is a decorator that has come adrift from its function.
That is one assertion over the whole route table and it covers all 57 of them.

**The plot itself.** Which observations get one, and that a drift scan's time
axis is the observation's own window rather than the extent of the data so far
- which is the entire difference between "the source peaked where it should"
and "the source peaked in the middle of the plot", the second being true of
every autoscaled plot ever drawn.
"""

import inspect
import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import h1_web_scheduler as sched


@pytest.fixture
def client():
    sched.app.config["TESTING"] = True
    with sched.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# The route table
# ---------------------------------------------------------------------------

def test_every_view_matches_the_rule_it_is_registered_for():
    """No @app.route may have come adrift from its function.

    A view is called with the converter variables of its own rule and nothing
    else, so a required parameter that the rule does not name can never be
    supplied - the route is a guaranteed 500 the first time anybody visits it.
    """
    for rule in sched.app.url_map.iter_rules():
        view = sched.app.view_functions[rule.endpoint]
        sig = inspect.signature(view)
        required = {
            name for name, p in sig.parameters.items()
            if p.default is inspect.Parameter.empty
            and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        }
        missing = required - set(rule.arguments)
        assert not missing, (
            "%s serves %s but requires %s, which the rule does not supply - "
            "most likely an @app.route that has become separated from the "
            "function it was meant to decorate"
            % (view.__name__, rule.rule, sorted(missing)))


# ---------------------------------------------------------------------------
# Which observations get a live plot
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("obs,expected", [
    ({"coord_system": "object", "object_name": "sun"}, "solar"),
    ({"coord_system": "object", "object_name": "Sun"}, "solar"),
    ({"coord_system": "drift"}, "drift"),
    # An alt/az entry parks the dish and leaves tracking off, so the sky moves
    # through the beam: a drift scan whatever the form called it. Identified
    # the same way its filename is, which is the point of sharing the rule.
    ({"coord_system": "altaz"}, "drift"),
    # The Moon is tracked, so its band power is meant to be flat.
    ({"coord_system": "object", "object_name": "moon"}, None),
    ({"coord_system": "galactic"}, None),
    ({"coord_system": "radec"}, None),
])
def test_which_observations_get_a_live_plot(obs, expected):
    assert sched.live_plot_kind(obs) == expected


def test_a_tracked_spectrum_gets_no_plot_rather_than_an_empty_one():
    """Deliberate, not an oversight.

    A tracked observation's band power is meant to be constant. Plotted on an
    axis that autoscales to it, the noise fills the frame and reads as
    structure - and there is nothing to compare it against, since the whole
    point of tracking is that the pointing does not change.
    """
    assert sched.live_plot_kind({"coord_system": "galactic"}) is None


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

def _live_file(tmp_path, n=20, t0=None, dt=2.0):
    """A recording's summary sidecar, as the receiver writes it."""
    out = tmp_path / "20260825_120000_drift.h5"
    side = tmp_path / "20260825_120000_drift.live.jsonl"
    t0 = t0 if t0 is not None else datetime.now().timestamp()
    with open(side, "w") as fh:
        for i in range(n):
            fh.write(json.dumps({"t": t0 + i * dt, "tau": dt, "n": i + 1,
                                 "median": 1.0e-5 + 1e-7 * i}) + "\n")
    return str(out)


@pytest.fixture
def drift_running(tmp_path):
    """A drift scan in progress, with records on disk."""
    started = datetime.now() - timedelta(minutes=5)
    ends = started + timedelta(minutes=30)
    out = _live_file(tmp_path, n=20, t0=started.timestamp() + sched.LIVE_WARMUP_S + 1)
    obs = {"name": "Drift test", "coord_system": "drift", "output_file": out,
           "center_freq_mhz": 1420.405752, "bandwidth_mhz": 2.4,
           "channels": 1024, "gain_db": 40,
           "started_at": started.isoformat(), "ends_at": ends.isoformat()}
    saved = sched.current_observation
    sched.current_observation = obs
    yield obs, started, ends
    sched.current_observation = saved


def test_the_time_axis_is_the_observation_window_not_the_data(client, drift_running):
    """The whole point of the request.

    The records span five minutes of a thirty-minute scan. An axis scaled to
    the data would show a full-width trace that says nothing about how far
    through the scan is, and would rescale under the reader on every poll. The
    window is fixed from the moment the observation starts.
    """
    obs, started, ends = drift_running
    d = client.get("/api/observe/live").get_json()

    assert d["kind"] == "drift"
    assert d["t_start"] == pytest.approx(started.timestamp(), abs=1)
    assert d["t_end"] == pytest.approx(ends.timestamp(), abs=1)

    # The data must sit inside the window with room to spare, or this test is
    # not distinguishing a fixed axis from a fitted one.
    last = d["points"][-1]["t"]
    assert last < d["t_end"] - 600, "the scan should be nowhere near finished"


def test_the_beam_crossing_is_marked_at_the_middle_of_the_window(client, drift_running):
    """Where the pointing was laid out to put the source.

    compute_drift_pointing parks the dish where the target will be at the
    slot's mid-point, so that is the moment a source should peak. Marking it
    is what turns the plot from a trace into a check.
    """
    obs, started, ends = drift_running
    d = client.get("/api/observe/live").get_json()
    middle = (started.timestamp() + ends.timestamp()) / 2.0
    assert d["t_transit"] == pytest.approx(middle, abs=1)


def test_a_drift_scan_reports_antenna_temperature_not_flux(client, drift_running):
    """SFU is a solar convention; a drift scan is plotted in kelvin."""
    d = client.get("/api/observe/live").get_json()
    if not d["calibrated"]:
        pytest.skip("no gain calibration on this host for that tuning")
    assert "t_a_k" in d["points"][0]


def test_the_axes_are_known_before_the_first_record(client, tmp_path):
    """A drift plot can be drawn empty, and should be.

    The window is known from the moment the observation starts, so the box can
    show its axes and its crossing time while waiting - which says "nothing has
    arrived yet" far better than an empty panel does.
    """
    started = datetime.now()
    ends = started + timedelta(minutes=30)
    obs = {"name": "Drift test", "coord_system": "drift",
           "output_file": str(tmp_path / "nothing_yet.h5"),
           "center_freq_mhz": 1420.405752, "bandwidth_mhz": 2.4,
           "channels": 1024, "gain_db": 40,
           "started_at": started.isoformat(), "ends_at": ends.isoformat()}
    saved = sched.current_observation
    sched.current_observation = obs
    try:
        d = client.get("/api/observe/live").get_json()
        assert d["success"] is True
        assert d["points"] == []
        assert d["kind"] == "drift"
        assert d["t_start"] and d["t_end"] and d["t_transit"], \
            "the window must be reported even with no data, or the plot cannot " \
            "draw its axes until the first record arrives"
    finally:
        sched.current_observation = saved


def test_a_finished_scan_keeps_the_window_it_was_given(client, tmp_path):
    """A scan stopped early must not redraw itself as a complete one.

    last_observation carries the *planned* end alongside the actual one. Fall
    back to the data and a run abandoned at a third of its length looks, on
    screen, exactly like one that ran to the end.
    """
    started = datetime.now() - timedelta(minutes=30)
    planned_end = started + timedelta(minutes=30)
    out = _live_file(tmp_path, n=10,
                     t0=started.timestamp() + sched.LIVE_WARMUP_S + 1)
    saved_cur, saved_last = sched.current_observation, sched.last_observation
    sched.current_observation = None
    sched.last_observation = {
        "name": "Stopped early", "coord_system": "drift", "output_file": out,
        "center_freq_mhz": 1420.405752, "bandwidth_mhz": 2.4,
        "channels": 1024, "gain_db": 40,
        "started_at": started.isoformat(),
        "ends_at": planned_end.isoformat(),
        "ended_at": (started + timedelta(minutes=10)).isoformat()}
    try:
        d = client.get("/api/observe/live").get_json()
        assert d["finished"] is True
        assert d["t_end"] == pytest.approx(planned_end.timestamp(), abs=1), \
            "the axis collapsed onto the data when the run stopped early"
    finally:
        sched.current_observation, sched.last_observation = saved_cur, saved_last
