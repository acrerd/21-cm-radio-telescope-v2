"""Every pulsar run adds its TOAs to the .tim when it ends.

stop_observation hands a finished pulsar recording to update_pulsar_timing,
which runs pulsar_toa.py on it at the lowest priority. Nothing here folds a
real file or touches the real .tim: the subprocess is stubbed.
"""

import json
from unittest.mock import MagicMock, patch

import h5py
import numpy as np
import pytest

import h1_web_scheduler as sched
import pulsar_toa


@pytest.fixture(autouse=True)
def clean_state():
    saved = (sched.current_process, sched.current_observation, sched.observation_end_time)
    yield
    sched.current_process, sched.current_observation, sched.observation_end_time = saved


def _recording(path, minutes):
    with h5py.File(path, "w") as hf:
        hf.create_dataset("power", data=np.ones((int(minutes * 60_000), 1), np.float32))
        hf.attrs["dt_s"] = 1e-3


class _Thread:
    started = []

    def __init__(self, target=None, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        _Thread.started.append((self.target, self.args))


def _stop(obs):
    proc = MagicMock()
    sched.current_process = proc
    sched.current_observation = obs
    _Thread.started = []
    with patch.object(sched.threading, "Thread", _Thread), \
         patch.object(sched, "SRT_CONTROLLER_URL", ""):
        sched.stop_observation()
    return [t for t, _ in _Thread.started]


def test_a_pulsar_run_updates_the_timing_when_it_ends():
    targets = _stop({"name": "Pulsar monitor", "coord_system": "pulsar",
                     "output_file": "/x/20260928_194200_pulsar.h5", "end_action": "none"})
    assert sched.update_pulsar_timing in targets


def test_other_runs_do_not():
    targets = _stop({"name": "Sun monitor", "coord_system": "object",
                     "output_file": "/x/20260928_120000_track.h5", "end_action": "none"})
    assert sched.update_pulsar_timing not in targets


def test_it_runs_pulsar_toa_quietly_with_a_segment_per_four_hours(tmp_path, monkeypatch):
    monkeypatch.setattr(pulsar_toa, "OBS_DIR", str(tmp_path))
    path = str(tmp_path / "20260928_194200_pulsar.h5")
    _recording(path, 30)
    done = MagicMock(returncode=0, stdout="session_2026-09-28T1942 MJD ... S/N  15.0\nwritten: ...\n", stderr="")
    with patch.object(sched.subprocess, "run", return_value=done) as run:
        result = sched.update_pulsar_timing(path)
    cmd = run.call_args[0][0]
    assert result["returncode"] == 0
    assert cmd[:3] == ["nice", "-n", "19"]
    assert "pulsar_toa.py" in " ".join(cmd) and path in cmd
    assert cmd[cmd.index("--segments") + 1] == "1"                   # half an hour: no segments
    assert sched.pulsar_timing_command(path, 16.0)[-1] == "4"
    # A 16 h window records a few seconds short: still four 4 h segments,
    # or they would replace the lines of the same names with 5.3 h ones.
    assert sched.pulsar_timing_command(path, 15.98)[-1] == "4"


def test_the_plot_is_drawn_into_the_cache_once_the_toas_are_in(tmp_path, monkeypatch):
    monkeypatch.setattr(pulsar_toa, "OBS_DIR", str(tmp_path))
    path = str(tmp_path / "20260928_194200_pulsar.h5")
    _recording(path, 30)
    for code, drawn in ((0, [path]), (1, [])):
        calls = []
        monkeypatch.setattr(sched, "prerender_observation_plot", calls.append)
        with patch.object(sched.subprocess, "run", return_value=MagicMock(returncode=code, stdout="", stderr="")):
            sched.update_pulsar_timing(path)
        assert calls == drawn                         # not after a failed timing run


def test_a_prerendered_plot_is_what_the_page_gets(tmp_path, monkeypatch):
    """Drawn at the lowest priority in a subprocess, stored under the key
    the Observe tab's request makes, so its first Plot Result is a hit."""
    import os
    import time
    rec = tmp_path / "20261019_182116_pulsar.h5"
    rec.write_bytes(b"recording")
    old = time.time() - 3600
    os.utime(rec, (old, old))
    info = {"output_file": str(rec), "mode": "pulsar", "name": "Pulsar monitor", "transit_minutes": None}
    monkeypatch.setattr(sched, "_observation_info", lambda f: dict(info))
    monkeypatch.setattr(sched, "PLOT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(sched, "OBSERVE_PLOT_PNG", str(tmp_path / "last.png"))
    cmds = []

    def run(cmd, **kw):
        cmds.append(cmd)
        a = json.loads(cmd[-1])
        open(a["png"], "wb").write(b"PNG pre")
        open(a["pdf"], "wb").write(b"PDF")
        return MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(sched.subprocess, "run", run)
    assert sched.prerender_observation_plot(str(rec))
    assert cmds[0][:3] == ["nice", "-n", "19"]
    assert sorted(os.listdir(tmp_path / "cache")) and not any("tmp" in f for f in os.listdir(tmp_path / "cache"))
    r = sched.app.test_client().get("/api/observe/plot?file=" + rec.name)
    assert r.headers["X-Plot-Cache"] == "hit" and r.data == b"PNG pre"


def test_a_failed_start_or_a_stray_file_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(pulsar_toa, "OBS_DIR", str(tmp_path / "observations"))
    (tmp_path / "observations").mkdir()
    short = str(tmp_path / "observations" / "20260928_194200_pulsar.h5")
    _recording(short, 2)
    elsewhere = str(tmp_path / "20260928_200000_pulsar.h5")
    _recording(elsewhere, 30)
    with patch.object(sched.subprocess, "run") as run:
        assert sched.update_pulsar_timing(short) is None
        assert sched.update_pulsar_timing(elsewhere) is None
        assert sched.update_pulsar_timing(str(tmp_path / "missing.h5")) is None
    assert not run.called
