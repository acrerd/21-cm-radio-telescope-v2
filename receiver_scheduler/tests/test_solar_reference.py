"""The professional solar flux quoted beside ours (solar_reference, 2026-09-14).

NOAA SWPC's hourly RSTN file is the source; these tests run on a saved copy
of it and never touch the network. Two things they hold to: a reader is
never made to wait on a fetch, and a day with no reading falls back to the
latest earlier day and says so rather than quoting a stale number as today's.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import h1_web_scheduler as sched          # noqa: E402
import solar_reference as sr              # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "test_data", "solar_radio_flux_sample.txt")


@pytest.fixture
def client():
    sched.app.config["TESTING"] = True
    with sched.app.test_client() as c:
        yield c


@pytest.fixture
def parsed():
    with open(SAMPLE) as f:
        return sr.parse(f.read())


@pytest.fixture
def offline_history(tmp_path, monkeypatch, parsed):
    """A history built from the sample, on a private path, with the network
    stubbed out so a stale cache cannot reach for it."""
    path = str(tmp_path / "history.json")
    monkeypatch.setattr(sr, "HISTORY_PATH", path)
    monkeypatch.setattr(sr, "fetch", lambda *a, **k: None)
    sr.reset_cache()
    hist = sr.merge_into_history(parsed, path)
    yield hist
    sr.reset_cache()


def test_the_file_parses_to_days_frequencies_and_stations(parsed):
    assert parsed["issued"] == "1442 UTC 14 Sep 2026"
    assert sorted(parsed["days"]) == ["2026-09-%02d" % d for d in range(8, 15)]
    day = parsed["days"]["2026-09-13"]
    assert day[1415] == {"San Vito": 85, "Palehua": 73}
    # Penticton appears three times, told apart by the UTC of its noon.
    assert day[2800] == {"Penticton 1700": 107, "Penticton 2000": 114, "Penticton 2300": 109}
    # -1 is missing, not a value.
    assert "Learmonth" not in day[1415]
    assert parsed["days"]["2026-09-11"][1415]["Learmonth"] == 77


def test_the_summary_quotes_1415_and_f107_for_the_day(offline_history):
    text = sr.summary_for("2026-09-13", offline_history)
    assert text == ("RSTN 1415 MHz local-noon flux, 13 Sep: San Vito 85, Palehua 73 SFU"
                    " · F10.7 114 (NOAA SWPC)")
    # Epoch seconds and datetimes pick the same day.
    when = datetime(2026, 9, 13, 16, 17).timestamp()
    assert sr.summary_for(when, offline_history) == text


def test_a_day_without_a_reading_falls_back_and_says_so(offline_history):
    text = sr.summary_for("2026-09-20", offline_history)
    assert text.startswith("RSTN 1415 MHz local-noon flux, 14 Sep (latest available): ")
    # Before the history starts there is nothing honest to say.
    assert sr.summary_for("2026-08-25", offline_history) is None


def test_the_history_accumulates_and_a_refetch_replaces_the_day(tmp_path, parsed):
    path = str(tmp_path / "h.json")
    sr.merge_into_history(parsed, path)
    later = {"issued": "later", "days": {"2026-09-14": {1415: {"Learmonth": 73, "San Vito": 75, "Sag Hill": 80}},
                                        "2026-09-15": {1415: {"San Vito": 70}}}}
    hist = sr.merge_into_history(later, path)
    assert "2026-09-08" in hist                                   # kept from the first fetch
    assert hist["2026-09-14"]["flux"]["1415"]["Sag Hill"] == 80    # the late station arrived
    assert hist["2026-09-15"]["issued"] == "later"
    with open(path) as f:
        assert json.load(f) == hist


def test_reading_the_cache_never_waits_on_the_network(tmp_path, monkeypatch):
    """A stale cache starts a background fetch and returns at once."""
    monkeypatch.setattr(sr, "HISTORY_PATH", str(tmp_path / "h.json"))
    def slow_fetch(*a, **k):
        time.sleep(2.0)
        return None
    monkeypatch.setattr(sr, "fetch", slow_fetch)
    sr.reset_cache()
    t0 = time.time()
    hist = sr.history()
    assert time.time() - t0 < 0.5
    assert hist == {}
    sr.reset_cache()


def test_a_failed_fetch_keeps_what_was_known(tmp_path, monkeypatch, parsed):
    path = str(tmp_path / "h.json")
    monkeypatch.setattr(sr, "HISTORY_PATH", path)
    sr.merge_into_history(parsed, path)
    monkeypatch.setattr(sr, "fetch", lambda *a, **k: None)
    sr.reset_cache()
    hist = sr.history(blocking=True)
    assert sr.summary_for("2026-09-13", hist) is not None
    sr.reset_cache()


# ---------------------------------------------------------------------------
# On the live plot
# ---------------------------------------------------------------------------

def _solar_live_file(tmp_path, n=20, t0=None, dt=2.0):
    out = tmp_path / "20260913_161706_track.h5"
    side = tmp_path / "20260913_161706_track.live.jsonl"
    with open(side, "w") as fh:
        for i in range(n):
            fh.write(json.dumps({"t": t0 + i * dt, "tau": dt, "n": i + 1,
                                 "median": 1.0e-5 + 1e-7 * i, "continuum": 1.0e-5}) + "\n")
    return str(out)


@pytest.fixture
def solar_running(tmp_path):
    started = datetime.now() - timedelta(minutes=5)
    out = _solar_live_file(tmp_path, t0=started.timestamp() + sched.LIVE_WARMUP_S + 1)
    obs = {"name": "Solar track", "coord_system": "object", "object_name": "sun",
           "output_file": out, "center_freq_mhz": 1420.405752, "bandwidth_mhz": 2.4,
           "channels": 1024, "gain_db": 40, "started_at": started.isoformat()}
    saved = sched.current_observation
    sched.current_observation = obs
    yield obs
    sched.current_observation = saved


def test_the_live_plot_carries_the_reference_for_a_solar_track(client, solar_running, monkeypatch):
    monkeypatch.setattr(sched.solar_reference, "summary_for",
                        lambda when, hist=None: "RSTN 1415 MHz local-noon flux, 13 Sep: San Vito 85 SFU (NOAA SWPC)")
    d = client.get("/api/observe/live").get_json()
    assert d["success"] and d["points"]
    assert d["reference"].startswith("RSTN 1415 MHz")


def test_the_live_plot_says_nothing_when_nothing_is_known(client, solar_running, monkeypatch):
    monkeypatch.setattr(sched.solar_reference, "summary_for", lambda when, hist=None: None)
    d = client.get("/api/observe/live").get_json()
    assert d["success"] and d["reference"] is None
