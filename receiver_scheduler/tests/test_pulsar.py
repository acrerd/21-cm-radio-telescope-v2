"""Pulsar mode: a fast filterbank folded at the period (pulsar_fold.py).

What these guard: the catalogue lookup; the period brought to the telescope
(shorter when the site moves toward the pulsar, by v/c); a synthetic pulse
recovered from noise at the phase it was put in, at the signal-to-noise the
radiometer equation gives; and the scheduler's plumbing - the entry's mode
word, its sky position for the horizon trim, the plot it gets, the catalogue
route. Nothing here opens the radio or runs GNU Radio.
"""
import math
import os

import h5py
import numpy as np
import pytest

import observatory  # noqa: F401
import pulsar_fold as PF
import observation_files


def test_the_one_pulsar_answers_to_its_names_and_nothing_else_does():
    p = PF.lookup("B0329+54")
    assert p["period_s"] == pytest.approx(0.7145197, abs=1e-6) and 54.5 < p["dec_deg"] < 54.6
    assert PF.lookup("j0332+5434")["name"] == "B0329+54"
    assert PF.lookup(None)["name"] == "B0329+54"           # the default: there is only one
    assert PF.lookup("B0950+08") is None
    # the period derivative moves the period by a few parts in 10^6 by 2026
    p_now = PF.period_at(1790000000.0)
    assert 0 < p_now - p["period_s"] < 5e-6


def test_the_period_is_shorter_when_the_site_moves_toward_the_pulsar():
    p = PF.lookup("B0329+54")
    t = 1790000000.0
    v = PF.observer_velocity_toward(p["ra_deg"], p["dec_deg"], t)[0]
    assert abs(v) < 32e3, "Earth's orbit plus rotation cannot exceed ~31 km/s"
    ptopo = PF.topocentric_period(p["period_s"], p["ra_deg"], p["dec_deg"], t)[0]
    assert ptopo / p["period_s"] - 1 == pytest.approx(-v / PF.C_M_S, rel=1e-6)


def _synthetic(minutes=15.0, tsys=100.0, peak_k=0.05, seed=1, phase_at=12.5 / 64):   # centred on bin 12
    p = PF.lookup("B0329+54")
    dt = 1e-3
    n = int(minutes * 60 / dt)
    t = 1790000000.0 + dt * np.arange(n)
    phase, pmean = PF.phase_track(t, p["period_s"], p["ra_deg"], p["dec_deg"])
    width_s = 6.6e-3
    pulse = peak_k * np.exp(-0.5 * ((((phase - phase_at + 0.5) % 1.0) - 0.5) * pmean / (width_s / 2.355)) ** 2)
    sig = tsys / math.sqrt(0.5e6 * dt)
    rng = np.random.default_rng(seed)
    power = (tsys + pulse[:, None] + sig * rng.standard_normal((n, 16))).astype(np.float32)
    power *= np.linspace(0.8, 1.2, 16)[None, :].astype(np.float32)       # a tilt, as the SAW gives
    freq = 1418.9e6 + 0.5e6 * (np.arange(16) - 7.5)
    return t, freq, power, p, pmean


def test_a_buried_pulse_is_recovered_at_its_phase():
    # Four times B0329's pulse, so the test is about correctness rather than
    # about sensitivity: 15 min of the real thing at 100 K is only ~2.7 sigma
    # in an 11 ms bin, and a noise bin could outvote it.
    t, freq, power, p, pmean = _synthetic(minutes=15.0, peak_k=0.2)
    r = PF.analyse(t, freq, power, p, search_ppm=40.0)
    assert r["snr_predicted"] > 5.0, r["snr_predicted"]
    assert abs(r["peak_bin_predicted"] - int(0.2 * r["nbins"])) <= 1
    # the search does not wander off: within the phase resolution of the run
    assert abs(r["best_ppm"]) < 1e6 / (r["nbins"] * r["n_periods"]) * 3
    assert r["n_periods"] == pytest.approx(15 * 60 / pmean, rel=1e-3)


def test_no_pulse_no_detection():
    t, freq, power, p, _ = _synthetic(minutes=5.0, peak_k=0.0, seed=3)
    r = PF.analyse(t, freq, power, p, search_ppm=40.0)
    assert r["snr_predicted"] < 4.0


def test_the_scheduler_plumbing(tmp_path, monkeypatch):
    import h1_web_scheduler as sched
    from datetime import datetime
    entry = {"coord_system": "pulsar", "object_name": "B0329+54", "name": "PSR test"}
    assert observation_files.observation_mode(entry) == "pulsar"
    assert observation_files.observation_filename(str(tmp_path), observation_files.observation_mode(entry)).endswith("_pulsar.h5")
    assert sched.plot_mode_for(entry, "pulsar") == "pulsar"
    assert sched.live_plot_kind(entry) is None
    altaz = sched.observation_altaz_at(entry, datetime(2026, 9, 25, 3, 0))
    assert altaz is not None and 20 < altaz[0] < 90, "B0329+54 is circumpolar from Glasgow"
    assert sched.observation_altaz_at({"coord_system": "pulsar", "object_name": "nope"}, datetime(2026, 9, 25, 3, 0)) is None
    # an entry with no name is B0329+54: the mode has exactly one target
    assert sched.observation_altaz_at({"coord_system": "pulsar"}, datetime(2026, 9, 25, 3, 0)) == altaz


def test_a_recording_is_read_back_and_plotted(tmp_path):
    t, freq, power, p, pmean = _synthetic(minutes=3.0, seed=5)
    path = str(tmp_path / "x_pulsar.h5")
    with h5py.File(path, "w") as hf:
        hf.create_dataset("frequency_hz", data=freq)
        hf.create_dataset("power", data=power)
        hf.create_dataset("overflow_marks", data=np.zeros((0, 2), dtype="i8"))
        hf.attrs["mode"] = "pulsar"; hf.attrs["dt_s"] = 1e-3; hf.attrs["t0_unix"] = float(t[0])
        hf.attrs["pulsar_name"] = "B0329+54"; hf.attrs["obs_name"] = "PSR test"
    tt, ff, pp, attrs = PF.read_recording(path)
    assert pp.shape == power.shape and tt[1] - tt[0] == pytest.approx(1e-3, abs=1e-6)   # float64 at a unix epoch
    out = str(tmp_path / "fold.png")
    r = PF.plot_recording(path, out)
    assert os.path.exists(out) and r["pulsar"] == "B0329+54"
    import observation_plot
    assert observation_plot.plot_observation(path, str(tmp_path / "via_plot.png"), mode="pulsar") == str(tmp_path / "via_plot.png")


def test_rows_are_timed_from_the_radio_marks_across_a_gap():
    """A dropped block shifts every later row. With the radio's time marks the
    rows after the gap are timed from the mark, not from the count."""
    dt = 1e-3
    marks = [(0, 1000.0), (5000, 1000.0 + 5000 * dt + 0.25)]   # a quarter-second gap at row 5000
    t = PF.row_times(10000, dt, 999.0, marks)
    assert t[0] == 1000.0 and t[4999] == pytest.approx(1004.999)
    assert t[5000] == pytest.approx(1005.25) and t[9999] == pytest.approx(1010.249)
    assert PF.row_times(3, dt, 5.0, None).tolist() == pytest.approx([5.0, 5.001, 5.002])
    assert PF.row_times(3, dt, 5.0, np.empty((0, 2))).tolist() == pytest.approx([5.0, 5.001, 5.002])


def test_the_filterbank_export_for_presto(tmp_path):
    """A .fil PRESTO can read: header keys and values, channels highest first,
    and a gap filled so every real row sits where tstart + i*tsamp puts it."""
    import sigproc_export as SE
    dt, n, nchan = 1e-3, 3000, 16
    freq = 1418.9e6 + 0.5e6 * (np.arange(nchan) - 7.5)
    power = (np.arange(n)[:, None] * 0 + np.arange(nchan)[None, :] + 1.0).astype(np.float32)
    power[1500:] += 100.0                                     # mark the rows after the gap
    t0 = 1790380800.0
    marks = np.array([[0, t0], [1500, t0 + 1500 * dt + 0.050]])  # 50 rows dropped at row 1500
    path = str(tmp_path / "x_pulsar.h5")
    with h5py.File(path, "w") as hf:
        hf.create_dataset("frequency_hz", data=freq)
        hf.create_dataset("power", data=power)
        hf.create_dataset("time_marks", data=marks)
        hf.attrs["dt_s"] = dt; hf.attrs["t0_unix"] = t0; hf.attrs["pulsar_name"] = "B0329+54"
    out, s = SE.export(path)
    assert s["fill_rows"] == 50 and s["samples"] == n + 50 and s["gaps"] == 1
    h, hlen = SE.read_header(out)
    assert h["nchans"] == 16 and h["nbits"] == 32 and h["nifs"] == 1 and h["data_type"] == 1
    assert h["source_name"] == "J0332+5434"
    assert h["fch1"] == pytest.approx(freq.max() / 1e6) and h["foff"] == pytest.approx(-0.5)
    assert h["tsamp"] == pytest.approx(dt) and h["tstart"] == pytest.approx(t0 / 86400 + 40587.0, abs=1e-9)
    assert h["src_raj"] == pytest.approx(33259.4096, abs=1e-3) and h["src_dej"] == pytest.approx(543443.329, abs=1e-3)
    data = np.fromfile(out, dtype="<f4", offset=hlen).reshape(-1, 16)
    assert data.shape == (n + 50, 16)
    assert data[0].tolist() == list(range(16, 0, -1))           # highest frequency first
    fill = data[1500:1550, 0]
    assert data[1499, 0] == 16 and np.all(fill == fill[0]) and 16 < fill[0] < 116   # the channel's median, flat
    assert data[1550, 0] == 116                                 # the real rows resume 50 later


def test_the_streaming_fold_agrees_and_reads_in_blocks(tmp_path):
    """analyse_file never holds the recording in memory (a 4 h run in float64
    needed 5.3 GB and the kernel killed the scheduler on 2026-09-26); read in
    small blocks here, it must find the same pulse at the same phase."""
    t, freq, power, p, pmean = _synthetic(minutes=15.0, peak_k=0.2)
    path = str(tmp_path / "s_pulsar.h5")
    with h5py.File(path, "w") as hf:
        hf.create_dataset("frequency_hz", data=freq)
        hf.create_dataset("power", data=power, chunks=(1024, 16))
        hf.create_dataset("time_marks", data=np.array([[0, t[0]]]))
        hf.attrs["dt_s"] = 1e-3; hf.attrs["t0_unix"] = float(t[0]); hf.attrs["pulsar_name"] = "B0329+54"
    r, attrs = PF.analyse_file(path, block_rows=100_000, search_ppm=20.0)
    assert r["snr_matched"] > 5.0 and abs(r["peak_bin_predicted"] - 12) <= 1
    assert r["nchan"] == 16 and len(r["subints"]) >= 7


def test_a_one_channel_recording_folds_and_exports(tmp_path):
    """The default since 2026-09-26: the whole band as one channel. The fold,
    the plot and the PRESTO export must all take a single-column file."""
    import sigproc_export as SE
    t, freq, power, p, pmean = _synthetic(minutes=15.0, peak_k=0.2)
    one = power.sum(axis=1, keepdims=True)
    path = str(tmp_path / "one_pulsar.h5")
    with h5py.File(path, "w") as hf:
        hf.create_dataset("frequency_hz", data=np.array([1418.9e6]))
        hf.create_dataset("power", data=one, chunks=(1024, 1))
        hf.create_dataset("time_marks", data=np.array([[0, t[0]]]))
        hf.attrs["dt_s"] = 1e-3; hf.attrs["t0_unix"] = float(t[0]); hf.attrs["pulsar_name"] = "B0329+54"
        hf.attrs["channel_width_hz"] = 8e6; hf.attrs["sample_rate_hz"] = 8e6
    r, _ = PF.analyse_file(path, block_rows=100_000, search_ppm=20.0)
    assert r["nchan"] == 1 and r["snr_matched"] > 5.0
    out, s = SE.export(path)
    h, hlen = SE.read_header(out)
    assert h["nchans"] == 1 and h["foff"] == pytest.approx(-8.0) and h["fch1"] == pytest.approx(1418.9)
    assert PF.plot_recording(path, str(tmp_path / "one.png"))["nchan"] == 1


def test_an_artificial_pulsar_is_folded_at_its_own_period(tmp_path):
    """A recording with inject_period_s is our transmitter's pulse train:
    folded at that period in the receiver's own time, with no Doppler."""
    dt, P, n = 1e-3, 0.6, 300_000
    t0 = 1790400000.0
    phase = np.arange(n) * dt / P
    rng = np.random.default_rng(9)
    pulse = np.where((phase % 1.0) < 0.01, 0.02, 0.0)            # 1% duty, 2% of T_sys
    power = (1.0 + pulse + 0.011 * rng.standard_normal(n)).astype(np.float32)[:, None]
    path = str(tmp_path / "inj_pulsar.h5")
    with h5py.File(path, "w") as hf:
        hf.create_dataset("frequency_hz", data=np.array([1418.9e6]))
        hf.create_dataset("power", data=power, chunks=(1024, 1))
        hf.create_dataset("time_marks", data=np.array([[0, t0]]))
        hf.attrs["dt_s"] = dt; hf.attrs["t0_unix"] = t0
        hf.attrs["inject_period_s"] = P; hf.attrs["inject_duty"] = 0.01
    r, attrs = PF.analyse_file(path, block_rows=100_000, search_ppm=20.0)
    assert r["pulsar"] == "artificial pulsar" and r["period_topo_mean_s"] == P
    assert r["snr_matched"] > 20 and r["observer_velocity_m_s"] == [0.0, 0.0]
    assert r["peak_bin_predicted"] == 0                          # the gate opens at phase 0
