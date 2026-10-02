"""The interference survey (interference_scan.py), on the demo SDR only:
no test may open the radio."""
import numpy as np
import pytest

import interference_scan as I


def test_the_tunings_abut_on_one_grid_across_the_passband():
    f = I.survey_frequencies()
    assert len(f) == 7 * 3200
    assert np.allclose(np.diff(f), I.SAMPLE_RATE_HZ / I.NFFT)
    assert f[0] == pytest.approx(1332.5e6) and f[-1] < 1507.5e6


def test_a_demo_survey_finds_the_mast(tmp_path):
    path = I.interference_scan(sdr_type="demo", az_start=40, az_end=130, dwell_s=7, out_dir=str(tmp_path))
    s = I.load_scan(path)
    assert s["meta"]["complete"] and s["mean"].shape == (19, 22400)
    az, f, db = I.relative_db(s, "median")
    downlink = (f > 1475e6) & (f < 1489e6)
    assert az[np.argmax(db[:, downlink].mean(axis=1))] == 85.0
    az, f, db = I.relative_db(s, "floor")
    out_of_band = f < 1355e6
    assert abs(np.median(db[:, out_of_band])) < 0.5          # nothing passes there: the floor
    png = I.plot_scan(s, mode="median", stat="peak", horizon_floor=lambda a: 12.0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_stopped_survey_keeps_what_it_measured(tmp_path):
    import threading
    stop = threading.Event()

    def progress(done, total, info):
        if done == 3 and info.get("stage") == "done":
            stop.set()

    path = I.interference_scan(sdr_type="demo", az_start=40, az_end=130, dwell_s=7, out_dir=str(tmp_path),
                               progress_callback=progress, cancel_event=stop)
    s = I.load_scan(path)
    assert not s["meta"]["complete"] and len(s["az_deg"]) == 3 and s["meta"]["n_planned"] == 19
    assert I.list_scans(str(tmp_path))[0]["complete"] is False


def test_only_survey_names_are_served(tmp_path):
    with pytest.raises(ValueError):
        I.scan_path(str(tmp_path), "../scheduler_config")
    with pytest.raises(ValueError):
        I.scan_path(str(tmp_path), "horizon_20260907")


def test_the_endpoints_run_a_demo_survey_and_plot_it(tmp_path, monkeypatch):
    import time
    import h1_web_scheduler as sched
    monkeypatch.setattr(sched, "interference_dir", lambda: str(tmp_path))
    monkeypatch.setattr(sched, "hardware_in_use", lambda: None)
    monkeypatch.setattr(sched, "sun_monitor_yield", lambda why: False)
    sched.app.config["TESTING"] = True
    c = sched.app.test_client()
    assert c.post("/api/interference/start", json={"sdr_type": "demo", "alt": 0}).status_code == 400
    assert c.post("/api/interference/start", json={"sdr_type": "demo", "az_start": 80, "az_end": 90,
                                                   "dwell_s": 7}).get_json()["success"]
    for _ in range(100):
        if not sched.interference_state["running"]:
            break
        time.sleep(0.05)
    st = c.get("/api/interference/status").get_json()
    assert not st["running"] and st["error"] is None and st["last_name"].startswith("interference_")
    scans = c.get("/api/interference/scans").get_json()["scans"]
    assert scans[0]["n_azimuths"] == 3
    r = c.get("/api/interference/plot?mode=median&stat=peak")
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert c.get("/api/interference/plot?name=../x").status_code == 400
