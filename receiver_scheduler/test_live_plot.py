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

        # And so is the calibration - it is a property of the tuning, not of
        # the data. This was hardcoded False in the empty response, invisible
        # while an empty response drew nothing; once the drift plot drew its
        # axes while waiting, a 30 s integration meant a solid minute of a
        # calibrated instrument labelled "uncalibrated".
        import rf_calibration
        expected, _ = rf_calibration.calibration_applies_to(
            rf_calibration.load_calibration(), sched.obs_header(obs))
        assert d["calibrated"] == bool(expected), (
            "the empty response must report the calibration that will apply, "
            "not a placeholder")
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


# ---------------------------------------------------------------------------
# The scale of the number the plot is drawn from
# ---------------------------------------------------------------------------

def _summary_file(tmp_path, correction, valid):
    """A file with just what _append_live_summary reads, plus its sidecar."""
    import h5py
    import numpy as np

    path = str(tmp_path / "scale.h5")
    hf = h5py.File(path, "w")
    hf.create_dataset("bandpass_correction", data=np.asarray(correction, float))
    hf.create_dataset("bandpass_valid", data=np.asarray(valid, bool))
    return hf, path


def test_the_live_median_is_on_the_scale_the_gain_was_fitted_on(tmp_path):
    """The bug the first blank-sky drift scan surfaced, pinned.

    The sidecar median used to be taken over the raw spectrum, full band, on
    the reasoning that the template normalises to a median of one so the
    scales are "very nearly" the same. They differ by about 8% - the band
    edges roll off - and 8% of a 353 K system temperature is 28 K. Watching
    the Sun at a thousand kelvin nobody could see it; the first drift scan of
    blank sky read a steady -24 K, an axis that was confidently wrong.

    So the median must be of the corrected spectrum over the channels the
    template covers - the scale the gain was fitted on - and this asserts it
    is *not* the raw full-band value, with numbers far enough apart that a
    regression cannot pass by luck.
    """
    import json
    import numpy as np

    import b210_h1_receiver as rx

    n = 64
    # Band edges reading low, the way a real anti-alias rolloff does, with the
    # template refusing to speak for the outer quarter.
    correction = np.full(n, 1.0)
    correction[: n // 4] = 0.5
    correction[-n // 4:] = 0.5
    valid = np.ones(n, bool)
    valid[: n // 8] = False
    valid[-n // 8:] = False
    raw = 0.004 * correction          # a flat sky, seen through that bandpass

    hf, path = _summary_file(tmp_path, correction, valid)
    try:
        rx._append_live_summary(hf, raw, 123.0, 30.0, 1)
    finally:
        hf.close()

    rec = json.loads(open(path.replace(".h5", ".live.jsonl")).read())
    expected = float(np.median((raw / correction)[valid]))
    assert rec["median"] == pytest.approx(expected, rel=1e-12)
    assert rec["median"] != pytest.approx(float(np.median(raw)), rel=0.01), (
        "corrected and raw medians must differ in this setup, or the test "
        "cannot tell which one was written")


def test_without_a_template_the_raw_median_goes_out_unchanged(tmp_path):
    """No template means counts, honestly - not a half-applied correction."""
    import json
    import numpy as np

    import b210_h1_receiver as rx

    raw = np.linspace(0.003, 0.005, 32)
    hf, path = _summary_file(tmp_path, np.ones(32), np.zeros(32, bool))
    try:
        rx._append_live_summary(hf, raw, 123.0, 30.0, 1)
    finally:
        hf.close()

    rec = json.loads(open(path.replace(".h5", ".live.jsonl")).read())
    assert rec["median"] == pytest.approx(float(np.median(raw)), rel=1e-12)


# ---------------------------------------------------------------------------
# The warm-up rule scales with the record length
# ---------------------------------------------------------------------------

def _run_with_tau(sched_mod, tmp_path, tau, n=4):
    """A running drift scan whose records are tau seconds long."""
    started = datetime.now() - timedelta(minutes=10)
    out = tmp_path / "20260825_130000_drift.h5"
    side = tmp_path / "20260825_130000_drift.live.jsonl"
    t0 = started.timestamp()
    with open(side, "w") as fh:
        for i in range(n):
            # Timestamps are the *centre* of each integration: record i spans
            # [t0 + i*tau, t0 + (i+1)*tau], so its midpoint is t0 + (i+0.5)*tau.
            fh.write(json.dumps({"t": t0 + (i + 0.5) * tau, "tau": tau,
                                 "n": i + 1, "median": 0.004}) + "\n")
    return {"name": "Warmup", "coord_system": "drift", "output_file": str(out),
            "center_freq_mhz": 1420.405752, "bandwidth_mhz": 2.4,
            "channels": 1024, "gain_db": 40,
            "started_at": started.isoformat(),
            "ends_at": (started + timedelta(hours=1)).isoformat()}


def test_a_long_first_record_is_not_discarded_as_warm_up(client, tmp_path):
    """The rule that cost a minute, pinned in proportion.

    The flowgraph settle is ~5 s. Purging it from a 60 s record means
    throwing away a minute of good data to remove five contaminated seconds,
    and it doubled the wait for the first point - measured at 2 min 18 s from
    slew start on 2026-08-25, of which the second minute was only this rule.
    A record is dropped only when the settle covers more than a tenth of it.
    """
    saved = sched.current_observation
    sched.current_observation = _run_with_tau(sched, tmp_path, tau=60.0)
    try:
        d = client.get("/api/observe/live").get_json()
        assert d["warmup_dropped"] == 0
        assert len(d["points"]) == 4, "all four 60 s records should be shown"
    finally:
        sched.current_observation = saved


def test_a_short_first_record_is_still_dropped(client, tmp_path):
    """The solar behaviour, preserved.

    At a 10 s record the settle is half the integration and the first record
    was measured 8% low - that is why the rule exists, and the proportionate
    version must keep dropping it.
    """
    saved = sched.current_observation
    sched.current_observation = _run_with_tau(sched, tmp_path, tau=10.0)
    try:
        d = client.get("/api/observe/live").get_json()
        assert d["warmup_dropped"] == 1, "the half-contaminated record goes"
        assert len(d["points"]) == 3
    finally:
        sched.current_observation = saved


# ---------------------------------------------------------------------------
# Fit model: the simulator's sky against the last recording
# ---------------------------------------------------------------------------

@pytest.fixture
def no_run():
    """Nothing recording, and the last-observation slot under our control."""
    saved_cur, saved_last, saved_fit = (sched.current_observation,
                                        sched.last_observation, sched.last_observe_fit)
    sched.current_observation = None
    sched.last_observation = None
    sched.last_observe_fit = None
    yield
    sched.current_observation, sched.last_observation, sched.last_observe_fit = (
        saved_cur, saved_last, saved_fit)


def test_fit_refuses_when_there_is_nothing_to_fit(client, no_run):
    assert client.post("/api/observe/fit").status_code == 404
    assert client.get("/api/observe/fit/plot").status_code == 404
    assert client.post("/api/observe/fit/apply").status_code == 404, \
        "applying a fit that was never made must be impossible"


def test_the_live_recording_can_be_plotted_and_listed(client, no_run, tmp_path, monkeypatch):
    """View live recording: the file the receiver is writing is readable.

    A writer holds the file open in SWMR mode; the catalogue lists it as
    recording and readable, and Plot Result draws what has arrived so far.
    """
    import numpy as np
    import b210_h1_receiver as rx

    monkeypatch.setattr(sched, "get_config_value",
                        lambda k, *a, **kw: str(tmp_path) if k == "data_output_folder" else None)
    folder = tmp_path / "observations"; folder.mkdir()
    live = str(folder / "20260826_120000_track.h5")
    freq = np.linspace(1419.5e6, 1421.5e6, 64)
    # The scheduler always hands the receiver its metadata; without it a
    # file has no coordinate system and is taken for a drift scan, which a
    # single-product file cannot be plotted as.
    monkeypatch.setenv("H1_OBS_METADATA",
                       '{"coord_system": "galactic", "observation_mode": "track"}')
    hf = rx.init_hdf5(live, freq, 64, "demo", 1420.4e6, 2.0e6, 0.0)
    try:
        for i in range(3):
            rx.append_spectrum(hf, np.full(64, 0.003 + 1e-5 * i), 1.0e9 + i, 1.0, 64)
        sched.current_observation = {"name": "live", "coord_system": "galactic",
                                     "output_file": live}
        rows = client.get("/api/observations").get_json()["observations"]
        row = next(r for r in rows if r["filename"] == "20260826_120000_track.h5")
        assert row["recording"] is True and not row.get("locked"), row
        assert row["name"] == "" or isinstance(row["name"], str)
        r = client.get("/api/observe/plot?file=20260826_120000_track.h5")
        assert r.status_code == 200, r.get_json()
        assert r.data[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        hf.close()


@pytest.mark.parametrize("info,why", [
    ({"coord_system": "object", "object_name": "sun"}, "no H I model"),
    ({"coord_system": "object", "object_name": "moon"}, "no H I model"),
])
def test_fit_refuses_what_has_no_single_direction_or_model(client, no_run, tmp_path,
                                                          info, why):
    path = tmp_path / "f.h5"
    path.write_bytes(b"")
    sched.last_observation = dict(info, name="x", output_file=str(path))
    r = client.post("/api/observe/fit")
    assert r.status_code == 400, r.get_json()
    assert why in r.get_json()["error"]


def test_fit_runs_end_to_end_on_a_real_calibration_field(client, no_run):
    """A tracked galactic recording at the standard tuning, fitted for real.

    Uses one of the archived RF-calibration fields, which is exactly the kind
    of file the button is for. The plot is drawn from the same reduction the
    fit used, and the fit is held as a proposal until applied - this checks
    the proposal, and deliberately never applies it.
    """
    import glob
    here = os.path.dirname(os.path.abspath(__file__))
    files = sorted(glob.glob(os.path.join(here, "data", "rf_gain_calibration_*.h5")))
    if not files:
        pytest.skip("no archived calibration field on this machine")
    sched.last_observation = {"name": "field", "coord_system": "galactic",
                              "output_file": files[-1]}
    r = client.post("/api/observe/fit")
    d = r.get_json()
    if r.status_code == 409 and "bandpass" in d.get("error", ""):
        pytest.skip("no bandpass template applies to that file here")
    assert r.status_code == 200, d
    f = d["fit"]
    # The counts scale is the flowgraph's - the fixed instrument's sums
    # linear power, the old graph went through dB - so only its sign is
    # a property of the fit.
    assert f["gain_counts_per_k"] > 0
    assert 100 < f["t_sys_k"] < 1000
    assert d["compare"] is None or "gain_ratio" in d["compare"]
    assert client.get("/api/observe/fit/plot").status_code == 200
    assert sched.last_observe_fit["source_file"] == os.path.basename(files[-1])


# ---------------------------------------------------------------------------
# The recordings catalogue
# ---------------------------------------------------------------------------

def test_the_catalogue_lists_recordings_from_their_own_attributes(client, tmp_path, monkeypatch):
    """Date, filename and comment, read from the files, newest first."""
    import h5py
    monkeypatch.setattr(sched, "get_config_value",
                        lambda k, *a, **kw: str(tmp_path) if k == "data_output_folder" else None)
    folder = tmp_path / "observations"; folder.mkdir()
    for name, comment, created in (("20260826_100000_track.h5", "first", "2026-08-26T09:00:00"),
                                   ("20260826_110000_drift.h5", "second", "2026-08-26T10:00:00")):
        with h5py.File(folder / name, "w") as hf:
            hf.attrs["obs_name"] = "T"; hf.attrs["comment"] = comment
            hf.attrs["created"] = created; hf.attrs["coord_system"] = "galactic"
            hf.attrs["observation_mode"] = "track" if "track" in name else "drift"
    (folder / "notes.txt").write_text("not a recording")
    d = client.get("/api/observations").get_json()
    rows = d["observations"]
    assert [r["filename"] for r in rows] == ["20260826_110000_drift.h5", "20260826_100000_track.h5"], \
        "newest first, and only HDF5 files"
    assert rows[0]["comment"] == "second" and rows[0]["mode"] == "drift"
    assert rows[1]["created"] == "2026-08-26T09:00:00"


def test_a_file_parameter_cannot_leave_the_observations_folder(client, tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "get_config_value",
                        lambda k, *a, **kw: str(tmp_path) if k == "data_output_folder" else None)
    (tmp_path / "observations").mkdir()
    (tmp_path / "secret.h5").write_bytes(b"")
    for bad in ("../secret.h5", "/etc/passwd", "nope.h5"):
        r = client.get("/api/observe/plot?file=" + bad)
        assert r.status_code == 404, bad


def test_a_drift_scan_gets_the_total_power_fit(client, no_run):
    """Last night's Cas A drift scan, if it is on this machine.

    Recorded at 1419 MHz, where no bandpass template applies - and fitted
    anyway, because a drift scan is total power against the simulator's
    predicted curve: two parameters, the bandpass shape inside the gain. The
    result is drawn and labelled approximate, and cannot be applied as the
    per-channel calibration.

    Since the fixed instrument (issue #27) the continuum is the wide
    product, and this file - recorded before it, with one product - is not
    carried: the fit refuses it and says why, rather than improvising a
    continuum out of a band that holds the line. (It fitted at correlation
    0.86 with the line in, 0.69 with the line cut out, for the record.)
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "data", "observations", "Cas A drift scan.h5")
    if not os.path.exists(path):
        pytest.skip("no Cas A drift scan on this machine")
    sched.last_observation = {"name": "Cas A", "coord_system": "drift",
                              "output_file": path}
    r = client.post("/api/observe/fit")
    d = r.get_json()
    assert r.status_code == 400, d
    assert "no continuum product" in d["error"]





def test_recording_details_say_whether_the_line_was_in_band(client):
    """The Cas A scan at 1419 MHz: LO 1419.8, the line +0.6 MHz above it, in band."""
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(here, "data", "observations", "Cas A drift scan.h5")):
        pytest.skip("no Cas A drift scan on this machine")
    d = client.get("/api/observe/info?file=Cas%20A%20drift%20scan.h5").get_json()
    assert d["success"], d
    x = d["details"]
    assert x["lo_mhz"] == pytest.approx(1419.8)
    assert x["h1_in_band"]
    # One product, from before the fixed instrument: no continuum window.
    assert x["products"] == ["h1"] and x["fit_window_mhz"] is None
    assert not x["h1_in_fit_window"]
    assert x["h1_offset_from_lo_mhz"] == pytest.approx(0.606, abs=0.01)
    assert x["units"] == "counts" and x["mode"] == "drift"
    assert x["records"] == 73 and x["integration_s"] == pytest.approx(240, abs=1)
    assert client.get("/api/observe/info?file=../secret.h5").status_code == 404


def test_the_total_power_band_window_is_the_continuum_band_less_the_spur_and_the_line():
    """The continuum is measured over the recording's own continuum band
    (fixed instrument, issue #27), never over the H I band, and never on the
    LO spur."""
    import numpy as np
    import drift_fit
    import tuning
    inst = tuning.fixed_instrument()
    freq = inst["lo_hz"] + np.linspace(-4e6, 4e6, 1024, endpoint=False)
    header = {"center_freq_hz": inst["lo_hz"], "sample_rate_hz": inst["sample_rate_hz"],
              "dc_artefact_freq_hz": inst["lo_hz"],
              "continuum_band_hz": inst["continuum_band_hz"],
              "h1_band_hz": inst["h1_band_hz"]}
    lo, hi, keep = drift_fit._band_window(header, freq)
    assert (lo, hi) == pytest.approx(tuple(inst["continuum_band_hz"]))
    assert not keep[np.abs(freq - inst["lo_hz"]) < 30e3].any(), "the LO artefact is masked"
    assert not keep[freq < lo].any() and not keep[freq > hi].any()
    assert not keep[(freq >= inst["h1_band_hz"][0]) & (freq <= inst["h1_band_hz"][1])].any()
    assert keep.sum() == pytest.approx(3.1e6 / (8e6 / 1024), abs=3)


def test_the_selected_recording_can_be_downloaded(client):
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(here, "data", "observations", "Cas A drift scan.h5")):
        pytest.skip("no Cas A drift scan on this machine")
    r = client.get("/api/observe/download?file=Cas%20A%20drift%20scan.h5")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("Content-Disposition", "")
    assert r.data[:8] == b"\x89HDF\r\n\x1a\n", "an HDF5 file, byte for byte"
    assert client.get("/api/observe/download?file=../h1_schedule.json").status_code == 404
