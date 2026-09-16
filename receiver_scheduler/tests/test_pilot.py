"""The pilot (issue #30): the B210's TX as the receiver's gain and passband reference.

What these guard: the frame and its reference agree with what the receive
FFT does; a pilot that is there is found and one that is not (the TX
unplugged) is reported absent and changes nothing; the level and slope are
recovered from a synthetic response; the kelvin write divides by them and
read_observation multiplies them back exactly; the band windows leave the
tone bins out only in a file where the pilot was seen; and the scheduler
carries the switch.
"""
import json
import os
import sys

import numpy as np
import pytest

import pilot
import tuning

h5py = pytest.importorskip("h5py")


def _plan(cfg_over=None):
    inst = tuning.fixed_instrument(cfg_over or {})
    return inst, pilot.plan(inst["pilot"], inst["lo_hz"], inst["sample_rate_hz"],
                            inst["wide_channels"], inst["h1_band_hz"])


class TestPlan:
    def test_defaults_are_on_and_keep_out_of_the_h1_band(self):
        inst, p = _plan()
        assert inst["pilot"]["enabled"] and inst["pilot"]["mode"] == "continuum"
        assert len(p["bins"]) > 3
        lo, hi = inst["h1_band_hz"]
        f = p["freq_hz"][p["bins"]]
        assert not ((f >= lo) & (f <= hi)).any(), "no pilot tone inside the H I band"
        # the three tones are where the config says, on bin centres
        for t in inst["pilot"]["tones_hz"]:
            assert np.abs(f - t).min() < 0.5 * inst["sample_rate_hz"] / inst["wide_channels"]

    def test_tones_only_mode_has_just_the_tones_and_full_covers_the_band(self):
        inst_t, p_t = _plan({"receiver_pilot_mode": "tones"})
        assert len(p_t["bins"]) == 3
        inst_f, p_f = _plan({"receiver_pilot_mode": "full"})
        lo, hi = inst_f["h1_band_hz"]
        f = p_f["freq_hz"][p_f["bins"]]
        assert ((f >= lo) & (f <= hi)).any()

    def test_off_means_off(self):
        inst, p = _plan({"receiver_pilot_enabled": False})
        assert not inst["pilot"]["enabled"] and len(p["bins"]) == 0
        inst2 = tuning.fixed_instrument({"receiver_pilot_mode": "off"})
        assert not inst2["pilot"]["enabled"]

    def test_the_window_matches_gnu_radio_and_the_reference_is_isolated_per_bin(self):
        gr = pytest.importorskip("gnuradio.fft")
        w_gr = np.asarray(gr.window.blackmanharris(1024))
        assert np.abs(w_gr - pilot.blackman_harris(1024)).max() < 1e-6
        _, p = _plan()
        mags = np.abs(p["reference"])
        on = mags[p["bins"]]
        near = np.concatenate([p["bins"] + d for d in range(-3, 4)])
        off = np.delete(mags, np.clip(near, 0, len(mags) - 1))
        # every tone the same size on its own bin; nothing measurable four bins away
        assert on.max() / on.min() < 1.01
        assert off.max() < 1e-4 * on.min()

    def test_frame_peak_is_the_configured_amplitude(self):
        inst, p = _plan({"receiver_pilot_amplitude": 0.2})
        assert np.abs(p["frame"]).max() == pytest.approx(0.2, rel=1e-5)
        with pytest.raises(ValueError):
            tuning.fixed_instrument({"receiver_pilot_amplitude": 1.5})
        with pytest.raises(ValueError):
            tuning.fixed_instrument({"receiver_pilot_mode": "sideways"})


def _synthetic(p, n_frames, h, noise_per_bin, rng):
    """The accumulated cross-spectrum and wide power for a pilot of response `h`
    (complex, per pilot bin; 0 for absent) in receive noise of the given
    per-bin FFT power, as the receiver's accumulators would hand them over."""
    nb = p["nbins"]
    R = p["reference"]
    xspec = np.zeros(nb, dtype=complex)
    # Noise on X_j: sum over N frames of n_j conj(R_j) -> variance N * P * |R|^2.
    sig = np.sqrt(n_frames * noise_per_bin) * np.abs(R)
    xspec += sig * (rng.standard_normal(nb) + 1j * rng.standard_normal(nb)) / np.sqrt(2)
    xspec[p["bins"]] += n_frames * np.asarray(h) * np.abs(R[p["bins"]]) ** 2
    wide_mean = np.full(nb, noise_per_bin / nb)          # the receiver's |F|^2 / N
    return xspec, wide_mean


class TestEstimate:
    def test_nothing_there_is_reported_absent_and_applies_nothing(self):
        inst, p = _plan()
        rng = np.random.default_rng(3)
        xs, wm = _synthetic(p, 23400, np.zeros(len(p["bins"])), 1.0, rng)
        est = pilot.estimate(xs, 23400, p, wm)
        assert est["snr_median"] < 2.5
        est = pilot.relative(est, p, np.ones(len(p["bins"])), inst["pilot"])
        assert not est["detected"] and est["level"] == 1.0 and est["slope"] == 0.0

    def test_a_pilot_twenty_db_under_the_noise_is_found_at_the_predicted_snr(self):
        inst, p = _plan()
        rng = np.random.default_rng(4)
        n = 23400                                        # one 3 s record at 8 Msps
        # tone amplitude such that its power in the bin is 1% of the noise power
        a = 0.1 / np.abs(p["reference"][p["bins"]][0])
        h = np.full(len(p["bins"]), a)
        xs, wm = _synthetic(p, n, h, 1.0, rng)
        est = pilot.estimate(xs, n, p, wm)
        assert est["snr_median"] == pytest.approx(np.sqrt(n) * 0.1, rel=0.15)

    def test_level_and_slope_are_recovered_against_a_reference(self):
        inst, p = _plan()
        rng = np.random.default_rng(5)
        n = 23400 * 10
        ref = np.full(len(p["bins"]), 0.5 + 0j)
        f = (p["freq_hz"][p["bins"]] - p["centre_hz"]) / 1e6
        level, slope = 1.03, -0.004                      # +3%, tilted -0.4% per MHz
        h = ref * level * (1 + slope * f)
        xs, wm = _synthetic(p, n, h, 1.0, rng)
        est = pilot.relative(pilot.estimate(xs, n, p, wm), p, ref, inst["pilot"])
        assert est["detected"]
        assert est["level"] == pytest.approx(level, abs=0.002)
        assert est["slope"] == pytest.approx(slope, abs=0.0005)

    def test_the_factor_is_the_line_and_is_clamped(self):
        f = np.array([1415e6, 1419e6, 1423e6])
        fac = pilot.factor(1.02, -0.005, f, 1419e6)
        assert fac == pytest.approx([1.02 * 1.02, 1.02, 1.02 * 0.98])
        assert pilot.factor(9.0, 0.0, f, 1419e6).max() == 2.0

    def test_the_tracker_self_references_on_first_sight_and_forgets_smoothing_on_loss(self):
        inst, p = _plan()
        cfg = inst["pilot"]
        rng = np.random.default_rng(6)
        n = 23400 * 4
        a = 0.3 / np.abs(p["reference"][p["bins"]][0])
        tr = pilot.PilotTracker(cfg, p, reference_h=None)
        assert not tr.anchored
        xs, wm = _synthetic(p, n, np.full(len(p["bins"]), a), 1.0, rng)
        r1 = tr.update(xs, n, wm, now=1000.0)
        assert r1["ok"] and r1["level"] == pytest.approx(1.0, abs=0.01)
        xs, wm = _synthetic(p, n, np.full(len(p["bins"]), 1.05 * a), 1.0, rng)
        r2 = tr.update(xs, n, wm, now=1003.0)
        assert r2["ok"] and 1.0 < r2["level"] < 1.05          # smoothed over the two
        xs, wm = _synthetic(p, n, np.zeros(len(p["bins"])), 1.0, rng)
        r3 = tr.update(xs, n, wm, now=1006.0)
        assert not r3["ok"] and r3["level"] == 1.0 and r3["slope"] == 0.0
        assert tr.detected_records == 2 and tr.records == 3
        assert tr.shape_ready(1006.0) is None                 # interval not up
        assert tr.shape_ready(1000.0 + cfg["shape_interval_s"] + 1) is not None


def _file(path, monkeypatch, pilots, calibrated):
    """A two-product file as the recorder writes it, with the given per-record
    pilot dicts; calibrated=True fakes a template and gain that apply."""
    import b210_h1_receiver as rx
    import bandpass
    import rf_calibration
    if calibrated:
        monkeypatch.setattr(rx, "_bandpass_correction",
                            lambda f, header=None, product="h1": (np.full(len(f), 1.0), np.ones(len(f), bool)))
        monkeypatch.setattr(rx, "_calibration_for_writing", lambda hf: (2.0e-6, 300.0))
    else:
        monkeypatch.setattr(bandpass, "load_bandpass", lambda *a, **k: None)
        monkeypatch.setattr(rf_calibration, "load_calibration", lambda *a, **k: None)
    inst = tuning.fixed_instrument()
    p = pilot.plan(inst["pilot"], inst["lo_hz"], inst["sample_rate_hz"],
                   inst["wide_channels"], inst["h1_band_hz"])
    lo, hi = inst["h1_band_hz"]
    f_h1 = np.linspace(lo, hi, 300)
    f_wide = pilot.wide_axis(inst["lo_hz"], inst["sample_rate_hz"], inst["wide_channels"])
    hf = rx.init_hdf5(path, f_h1, len(f_h1), "demo", inst["lo_hz"], inst["sample_rate_hz"],
                      inst["gain_db"], wide={"freq_axis_hz": f_wide, "channels": len(f_wide)},
                      instrument=inst, pilot=p)
    raw_h1, raw_w = [], []
    try:
        for i, pil in enumerate(pilots):
            h1 = np.full(len(f_h1), 0.003) * (1 + 0.01 * i)
            wide = np.full(len(f_wide), 0.002) * (1 + 0.01 * i)
            raw_h1.append(h1.copy()); raw_w.append(wide.copy())
            rx.append_spectrum(hf, h1, 1.7e9 + 3.0 * i, 3.0, len(f_h1),
                               wide_linear=wide, overflows=0, pilot=pil)
    finally:
        hf.close()
    return f_h1, f_wide, np.array(raw_h1), np.array(raw_w), p


class TestFile:
    def test_layout_and_the_unplugged_tx_writes_unit_factors(self, tmp_path, monkeypatch):
        path = str(tmp_path / "p.h5")
        absent = {"ok": 0, "level": 1.0, "slope": 0.0, "snr": 0.9}
        _file(path, monkeypatch, [absent] * 3, calibrated=False)
        with h5py.File(path, "r") as hf:
            for name in ("pilot_level", "pilot_slope", "pilot_snr", "pilot_ok", "pilot_shape",
                         "pilot_shape_time", "pilot_bins_wide", "pilot_excluded_wide",
                         "pilot_reference", "pilot_anchored"):
                assert name in hf, name
            assert list(hf["pilot_ok"][:]) == [0, 0, 0]
            assert list(hf["pilot_level"][:]) == [1.0, 1.0, 1.0]
            assert json.loads(hf.attrs["pilot"])["mode"] == "continuum"
            assert hf["pilot_shape"].shape[0] == 0
            assert not np.any(hf["pilot_reference"][:]) and hf["pilot_anchored"][0] == 0

    def test_kelvin_is_divided_by_the_factor_and_read_back_exactly(self, tmp_path, monkeypatch):
        from observation_plot import read_observation
        path = str(tmp_path / "k.h5")
        pilots = [{"ok": 0, "level": 1.0, "slope": 0.0, "snr": 1.0},
                  {"ok": 1, "level": 1.03, "slope": -0.004, "snr": 20.0},
                  {"ok": 1, "level": 0.98, "slope": 0.002, "snr": 18.0}]
        f_h1, f_wide, raw_h1, raw_w, p = _file(path, monkeypatch, pilots, calibrated=True)
        with h5py.File(path, "r") as hf:
            assert "spectra_kelvin" in hf and int(hf.attrs["pilot_applied"]) == 1
            k = hf["spectra_kelvin"][:]
            fc = float(hf.attrs["pilot_centre_hz"])
            # record 0: no pilot, the plain conversion; record 1: divided by the line
            assert k[0] == pytest.approx(raw_h1[0] / 2.0e-6 - 300.0, rel=1e-6)
            expect = raw_h1[1] / (2.0e-6 * pilot.factor(1.03, -0.004, f_h1, fc)) - 300.0
            assert k[1] == pytest.approx(expect, rel=1e-6)
            assert list(hf["pilot_level"][:]) == pytest.approx([1.0, 1.03, 0.98])
        f, counts, stamps, taus, header = read_observation(path)
        assert counts == pytest.approx(raw_h1, rel=1e-6)                 # exact reversal
        fw, cw, _, _, hw = read_observation(path, product="wide")
        assert cw == pytest.approx(raw_w, rel=1e-6)
        assert header["pilot_detected_records"] == 2 and header["pilot_records"] == 3

    def test_band_windows_leave_the_tones_out_only_where_the_pilot_was_seen(self, tmp_path, monkeypatch):
        import drift_fit
        from observation_plot import read_observation
        absent = {"ok": 0, "level": 1.0, "slope": 0.0, "snr": 0.9}
        seen = {"ok": 1, "level": 1.0, "slope": 0.0, "snr": 15.0}
        path_a = str(tmp_path / "a.h5")
        path_b = str(tmp_path / "b.h5")
        _, f_wide, _, _, p = _file(path_a, monkeypatch, [absent] * 2, calibrated=False)
        _file(path_b, monkeypatch, [seen] * 2, calibrated=False)
        fw, sw, _, _, ha = read_observation(path_a, product="wide")
        _, _, _, _, hb = read_observation(path_b, product="wide")
        _, _, keep_a = drift_fit._band_window(ha, fw)
        _, _, keep_b = drift_fit._band_window(hb, fw)
        # nothing excluded for the unplugged TX; the tone bins gone once it was seen
        assert not pilot.excluded_channels(ha, fw).any()
        excl = pilot.excluded_channels(hb, fw)
        assert excl.any() and excl[p["bins"]].all()
        assert keep_a.sum() > keep_b.sum()
        assert not (keep_b & excl).any()
        # and on the fine axis the same tones are excluded by frequency
        fh = np.linspace(1415e6, 1423e6, 4000)
        assert pilot.excluded_channels(hb, fh).any()

    def test_the_live_sidecar_says_what_the_pilot_did(self, tmp_path, monkeypatch):
        path = str(tmp_path / "s.h5")
        _file(path, monkeypatch, [{"ok": 1, "level": 1.02, "slope": 0.001, "snr": 12.0}], calibrated=False)
        rec = json.loads(open(str(tmp_path / "s.live.jsonl")).readline())
        assert rec["pilot_ok"] == 1 and rec["pilot_level"] == pytest.approx(1.02)
        assert "continuum" in rec


class TestDemoFlowgraph:
    def test_an_injected_pilot_is_found_and_none_is_not(self):
        """The recovery branch end to end on the demo source: with the frame
        added digitally the pilot is detected; without, it is not (the state
        the telescope is in until the dipole is wired)."""
        import time
        if "--headless" not in sys.argv:
            sys.argv.append("--headless")
        import b210_h1_receiver as rx
        results = {}
        for inject in (True, False):
            inst = tuning.fixed_instrument({"receiver_pilot_demo_inject": inject})
            fg = rx.TwoProductFlowgraph("demo", inst, strict=False)
            assert fg.pilot is not None and fg.pilot_sink is None
            fg.start()
            time.sleep(1.5)
            wide, _ = fg.take_wide()
            xs, n = fg.take_pilot()
            fg.stop(); fg.wait()
            est = pilot.estimate(xs, n, fg.pilot, wide)
            results[inject] = est["snr_median"]
        assert results[True] >= 5 * inst["pilot"]["detect_snr"]
        assert results[False] < inst["pilot"]["detect_snr"]


class TestScheduler:
    @pytest.fixture
    def sched(self, tmp_path):
        from unittest.mock import patch
        import h1_web_scheduler as s
        with patch.object(s, "CONFIG_FILE", str(tmp_path / "config.json")), \
                patch.object(s, "SCHEDULE_FILE", str(tmp_path / "schedule.json")):
            yield s

    def test_the_entry_switch_turns_the_pilot_off_for_that_entry_only(self, sched):
        on = sched.instrument_for({"name": "x"})
        off = sched.instrument_for({"name": "y", "pilot_off": True})
        assert on["pilot"]["enabled"] and not off["pilot"]["enabled"]
        assert {k: v for k, v in on.items() if k != "pilot"} == {k: v for k, v in off.items() if k != "pilot"}

    def test_the_instrument_endpoint_and_the_config_keys_carry_the_pilot(self, sched):
        sched.app.config["TESTING"] = True
        client = sched.app.test_client()
        d = client.get("/api/instrument").get_json()
        assert d["pilot"]["enabled"] and "pilot" in d["pilot_description"]
        r = client.post("/api/config", json={"receiver_pilot_mode": "tones"})
        assert r.status_code == 200, r.get_json()
        assert client.get("/api/instrument").get_json()["pilot"]["mode"] == "tones"
        r = client.post("/api/config", json={"receiver_pilot_mode": "sideways"})
        assert r.status_code == 400
        d = client.get("/api/pilot/status").get_json()
        assert d["success"] and "pilot" in d["description"]
