"""Fitting the bandpass templates from a recording on disk (RF tab, 2026-09-25).

The live job records two minutes of whatever sky is up; a template wants a
long run on an empty field with the TX off, chosen afterwards. The route must
refuse what a template cannot be made from - a Sun run, a recording at another
tuning or gain - and, when it fits, say that the gain now needs refitting.
"""
import os

import h5py
import numpy as np
import pytest

import h1_web_scheduler as sched


@pytest.fixture
def client():
    sched.app.config["TESTING"] = True
    with sched.app.test_client() as c:
        yield c


def _recording(folder, name, obs_name, gain_db=None, n=40):
    inst = sched.instrument_in_force()
    path = os.path.join(folder, name)
    with h5py.File(path, "w") as hf:
        hf.attrs["obs_name"] = obs_name
        hf.attrs["observation_mode"] = "track"
        hf.attrs["coord_system"] = "galactic"
        hf.attrs["center_freq_hz"] = float(inst["lo_hz"])
        hf.attrs["sample_rate_hz"] = float(inst["sample_rate_hz"])
        hf.attrs["gain_db"] = float(inst["gain_db"] if gain_db is None else gain_db)
        hf.create_dataset("timestamps", data=np.arange(n, dtype=float))
    return path


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "observations_folder", lambda: os.path.realpath(str(tmp_path)))
    return str(tmp_path)


def test_it_refuses_nothing_and_the_unknown(client, folder):
    assert client.post("/api/rf/bandpass/from-recording", json={}).status_code == 400
    assert client.post("/api/rf/bandpass/from-recording", json={"file": "nope.h5"}).status_code == 404


def test_it_refuses_the_sun_and_another_gain(client, folder):
    _recording(folder, "sun.h5", "Sun monitor")
    r = client.post("/api/rf/bandpass/from-recording", json={"file": "sun.h5"})
    assert r.status_code == 400 and "Sun" in r.get_json()["error"]
    _recording(folder, "tendb.h5", "Spectrum l=71.7 b=+44.0", gain_db=sched.instrument_in_force()["gain_db"] - 10)
    r = client.post("/api/rf/bandpass/from-recording", json={"file": "tendb.h5"})
    assert r.status_code == 400 and "gain" in r.get_json()["error"]


def test_a_good_recording_replaces_both_templates_and_says_the_gain_is_stale(client, folder, monkeypatch):
    import bandpass
    _recording(folder, "good.h5", "Spectrum l=71.7 b=+44.0", n=601)
    fake = {"degree": 9, "u_scale_hz": 1.65e6, "fit_residual_rms": 0.001, "n_channels_fitted": 716}
    calls = []
    monkeypatch.setattr(bandpass, "fit_both_from_observation",
                        lambda path, name="", degree=9: calls.append((os.path.basename(path), name)) or
                        {"h1": (fake, "/x/bandpass_template.json"),
                         "wide": (dict(fake, fit_residual_rms=0.0008, n_channels_fitted=774), "/x/bandpass_template_wide.json")})
    saved = dict(sched.rf_state)
    try:
        sched.rf_state["running"] = False
        r = client.post("/api/rf/bandpass/from-recording", json={"file": "good.h5"})
        d = r.get_json()
        assert r.status_code == 200 and d["success"], d
        assert calls == [("good.h5", "Spectrum l=71.7 b=+44.0")]
        assert d["result"]["records"] == 601 and d["result"]["from_recording"]
        assert d["result"]["wide"]["residual_pct"] == pytest.approx(0.08)
        assert "gain" in d["note"] and "refit" in d["note"]
        assert sched.rf_state["result"]["file"] == "good.h5"
        # and not while the live job holds the same files
        sched.rf_state["running"] = True
        assert client.post("/api/rf/bandpass/from-recording", json={"file": "good.h5"}).status_code == 409
    finally:
        sched.rf_state.clear(); sched.rf_state.update(saved)
