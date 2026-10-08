"""The day's solar flux products: FITS, RSTN3, the readers, and the hook that
rewrites them whenever a Sun track ends.

Nothing here reduces a real recording or writes into data/solar_flux: the
reduction was checked against the Observe tab's solar plot on 2026-10-07
(78.30 SFU both ways), and the subprocess the scheduler runs is stubbed.
"""
import importlib.util
import os
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import h1_web_scheduler as sched
import solar_flux_export as sfe

DAY = date(2026, 10, 7)
T0 = datetime(2026, 10, 7, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def clean_state():
    saved = (sched.current_process, sched.current_observation, sched.observation_end_time)
    yield
    sched.current_process, sched.current_observation, sched.observation_end_time = saved


class _Thread:
    started = []

    def __init__(self, target=None, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        _Thread.started.append((self.target, self.args))


def _stop(obs):
    sched.current_process = MagicMock()
    sched.current_observation = obs
    _Thread.started = []
    with patch.object(sched.threading, "Thread", _Thread), \
         patch.object(sched, "SRT_CONTROLLER_URL", ""):
        sched.stop_observation()
    return [t for t, _ in _Thread.started]


def test_a_sun_run_refreshes_the_products_when_it_ends():
    for name in ("Sun monitor", "Sun track"):
        targets = _stop({"name": name, "coord_system": "object", "object_name": "sun",
                         "output_file": "/x/20261007_111534_track.h5", "end_action": "none"})
        assert sched.update_solar_flux_export in targets


def test_other_runs_do_not():
    for obs in ({"coord_system": "pulsar", "output_file": "/x/a_pulsar.h5"},
                {"coord_system": "object", "object_name": "moon", "output_file": "/x/b_track.h5"},
                {"coord_system": "galactic", "output_file": "/x/c_track.h5"}):
        obs.update(name="x", end_action="none")
        assert sched.update_solar_flux_export not in _stop(obs)


def test_the_export_runs_quietly_on_the_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(sfe, "OBS_DIR", str(tmp_path))
    path = tmp_path / "20261007_111534_track.h5"
    path.write_bytes(b"")
    done = MagicMock(returncode=0, stdout="2026-10-07: 4743 rows\n", stderr="")
    with patch.object(sched.subprocess, "run", return_value=done) as run:
        result = sched.update_solar_flux_export(str(path))
    cmd = run.call_args[0][0]
    assert result["returncode"] == 0
    assert cmd[:3] == ["nice", "-n", "19"]
    assert "solar_flux_export.py" in " ".join(cmd)
    assert cmd[cmd.index("--recording") + 1] == str(path)


def test_a_stray_file_is_not_exported(tmp_path, monkeypatch):
    monkeypatch.setattr(sfe, "OBS_DIR", str(tmp_path / "observations"))
    (tmp_path / "observations").mkdir()
    elsewhere = tmp_path / "20261007_111534_track.h5"
    elsewhere.write_bytes(b"")
    with patch.object(sched.subprocess, "run") as run:
        assert sched.update_solar_flux_export(str(elsewhere)) is None
        assert sched.update_solar_flux_export(str(tmp_path / "observations" / "missing.h5")) is None
    assert not run.called


def _columns(times, flux, quality):
    n = len(times)
    return {"time": np.asarray(times, float), "flux": np.asarray(flux, float),
            "quality": np.asarray(quality, np.int16), "timedel": np.full(n, 3.0),
            "error": np.full(n, 0.12), "t_a": np.asarray(flux, float) * 30.0,
            "scallop": np.ones(n), "trans": np.full(n, 0.98), "alt": np.full(n, 25.0),
            "az": np.full(n, 180.0), "run": np.zeros(n, np.int16)}


def test_rstn3_records_are_the_archive_layout():
    c = _columns([3600.4, 3603.6, 3606.0, 3606.2, 3609.0, 86399.7],
                 [78.4, 78.6, 79.0, 80.0, np.nan, 77.0],
                 [0, 2, 0, 0, 0, 0])
    lines = sfe.rstn_lines(c, T0)
    assert all(len(ln) == 66 for ln in lines)
    assert lines[0][:18] == "ACRE20261007010000"
    field = lambda ln, mhz: ln[18 + 6 * sfe.RSTN_FREQS_MHZ.index(mhz):24 + 6 * sfe.RSTN_FREQS_MHZ.index(mhz)]
    assert field(lines[0], 1415) == "    78"
    assert all(field(lines[0], f).strip() == "" for f in sfe.RSTN_FREQS_MHZ if f != 1415)
    assert field(lines[1], 1415).strip() == ""          # flagged: the line is kept, blank
    assert [ln[12:18] for ln in lines] == ["010000", "010004", "010006", "010009"]
    assert field(lines[2], 1415) == "    79"              # two records on one second: the first
    assert field(lines[3], 1415).strip() == ""          # no flux: blank
    # 86399.7 rounds onto the next day and is left to that day's file.
    assert sfe.rstn_filename(DAY) == "07OCT26.ACR"


def test_a_run_is_clipped_to_the_day_and_numbered():
    stamps = T0 + np.array([-6.0, -3.0, 10.0, 13.0])
    rows = {"stamps": stamps, "taus": np.full(4, 3.0), "flux": np.array([1., 2., 3., 4.]),
            "t_a": np.ones(4), "scallop": np.ones(4), "trans": np.ones(4),
            "alt": np.ones(4), "az": np.ones(4), "quality": np.array([0, 0, 1, 0], np.int16)}
    later = dict(rows, stamps=T0 + np.array([5.0, 7.0, 20.0, 30.0]))
    run = {"sigma": 0.1}
    c, kept, t0 = sfe.collect_day(DAY, [(run, rows), (run, later)])
    assert t0 == T0
    assert list(c["time"]) == [5.0, 7.0, 10.0, 13.0, 20.0, 30.0]
    assert list(c["run"]) == [1, 1, 0, 0, 1, 1]
    assert [k["n_rows"] for k in kept] == [2, 4]
    assert kept[0]["mean_flux"] == 4.0                  # flagged row left out of the mean
    assert sfe.collect_day(date(2026, 10, 9), [(run, rows)]) is None


def _load_reader():
    spec = importlib.util.spec_from_file_location(
        "read_solar_flux", os.path.join(sfe.EXAMPLES_DIR, "read_solar_flux.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_header():
    return {"site_lat_deg": 55.902426, "site_lon_deg": -4.307865, "site_height_m": 50.0,
            "continuum_band_hz": [1415.70575e6, 1418.80575e6]}


def test_the_files_round_trip_through_the_python_reader(tmp_path):
    reader = _load_reader()
    c = _columns([36000.0, 36003.0, 36006.0], [78.4, 78.6, 79.2], [0, 0, 4])
    run = {"header": _run_header(), "file": "20261007_111534_track.h5", "obs_name": "Sun monitor",
           "sun_monitor": 1, "first": T0 + 36000.0, "last": T0 + 36006.0, "n_rows": 3,
           "mean_flux": 78.5, "sigma": 0.12, "gain": 1.4e-7, "t_sys": 191.5,
           "cal_utc": "2026-10-06T16:33:54", "cal_source": "current", "scallop": "carried",
           "k_alt": 0.81, "k_az": 1.04, "phase_alt": 0.036, "phase_az": -0.06,
           "mod_before": 0.81, "mod_after": 0.45, "terms_source": "the recording's own",
           "scallop_caption": "scallop removed", "beam_sq_deg": 20.96, "a_eff": 6.976,
           "beam_source": "measured 2026-09-25"}
    fits_path = sfe.write_fits(DAY, c, [run], T0, str(tmp_path / "a.fits"))
    times, flux, runs, header = reader.read_fits(fits_path)
    assert times[0] == datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    assert np.allclose(flux["FLUX"], [78.4, 78.6, 79.2])
    assert list(flux["QUALITY"]) == [0, 0, 4]
    assert str(flux["FLUX"].unit) == "1e-22 W / (Hz m2)"
    assert runs["SCALSOL"][0] == "carried" and header["TELESCOP"] == "Acre Road SRT"

    rstn_path = sfe.write_rstn(DAY, c, T0, str(tmp_path / sfe.rstn_filename(DAY)))
    with open(rstn_path, "rb") as fh:
        assert fh.read().count(b"\r\n") == 3
    times, values = reader.read_rstn(rstn_path)
    assert times[1] == datetime(2026, 10, 7, 10, 0, 3, tzinfo=timezone.utc)
    assert list(values[:2]) == [78.0, 79.0] and np.isnan(values[2])


def test_the_readme_and_readers_are_installed_beside_the_products(tmp_path):
    sfe.install_examples(str(tmp_path))
    for dest in sfe.EXAMPLE_FILES.values():
        assert (tmp_path / dest).is_file()
    # An out-of-date copy is replaced; the copy in git is the one that counts.
    stale = tmp_path / "examples" / "read_solar_flux.pro"
    stale.write_text("old")
    sfe.install_examples(str(tmp_path))
    assert stale.read_text() != "old"


def _cal(observed_utc, t_sys):
    return {"gain_counts_per_k": 1.4e-7, "t_sys_k": t_sys, "observed_utc": observed_utc,
            "config": {"lo_hz": 1418905752.0, "sample_rate_hz": 8e6, "gain_db": 20.0}}


def test_a_calibration_is_never_carried_across_the_feed_change(monkeypatch):
    """The new feed's gain on an old-feed Sun track read it 35% low: the
    tuning matched, the telescope did not."""
    header = {"center_freq_hz": 1418905752.0, "sample_rate_hz": 8e6, "gain_db": 20.0}
    new = _cal("2026-10-06T16:33:54+00:00", 191.5)
    old = _cal("2026-09-18T09:05:59+00:00", 349.5)
    monkeypatch.setattr(sfe.rf_calibration, "load_calibration", lambda *a, **k: new)
    monkeypatch.setattr(sfe.rf_calibration, "embedded_calibration", lambda h: old)
    before = datetime(2026, 9, 19, 10, tzinfo=timezone.utc).timestamp()
    after = datetime(2026, 10, 7, 10, tzinfo=timezone.utc).timestamp()
    assert sfe.calibration_for(header, before) == (old, "recorded with the observation")
    assert sfe.calibration_for(header, after) == (new, "current")
    monkeypatch.setattr(sfe.rf_calibration, "embedded_calibration", lambda h: None)
    cal, why = sfe.calibration_for(header, before)
    assert cal is None and "different instrument" in why


def test_the_beam_follows_the_feed(monkeypatch):
    monkeypatch.setattr(sfe.instrument, "measured_beam",
                        lambda: {"solid_angle_sq_deg": 20.96, "measured_utc": "2026-09-25T12:07:53Z"})
    before = datetime(2026, 9, 19, 10, tzinfo=timezone.utc).timestamp()
    after = datetime(2026, 10, 7, 10, tzinfo=timezone.utc).timestamp()
    old_sq, old_a, _ = sfe.beam_for(before)
    new_sq, new_a, src = sfe.beam_for(after)
    assert (old_sq, new_sq) == (21.6, 20.96) and src.startswith("measured")
    assert abs(new_a - 6.976) < 0.01                    # lambda^2/Omega, as the instrument computes it
    assert abs(old_a / new_a - 20.96 / 21.6) < 1e-9
