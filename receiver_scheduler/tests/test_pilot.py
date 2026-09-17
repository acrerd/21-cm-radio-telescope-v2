"""The pilot (issue #30): the B210's TX as the receiver's gain and passband reference, in bursts.

What these guard: the burst frame and its reference agree with what the
receive FFT does; a burst that is there is found and one that is not (the
TX unplugged) is reported absent and changes nothing; level, slope and the
passband correction are recovered from a synthetic response; the kelvin
write divides by them and read_observation multiplies them back exactly and
drops the burst records; the per-frame gate counts the frames a burst
occupied on the demo source; the scheduler carries the switch.
"""
import json
import sys
import time

import numpy as np
import pytest

import pilot
import tuning

h5py = pytest.importorskip("h5py")


def _plan(cfg_over=None):
    inst = tuning.fixed_instrument(cfg_over or {})
    return inst, pilot.plan(inst["pilot"], inst["lo_hz"], inst["sample_rate_hz"], inst["wide_channels"])


class TestPlan:
    def test_defaults_are_a_full_band_comb_in_bursts_around_a_continuous_carrier(self):
        inst, p = _plan()
        cfg = inst["pilot"]
        assert cfg["enabled"] and cfg["burst_interval_s"] == 60.0
        nb = inst["wide_channels"]
        # every bin but the LO guard and the carrier's own neighbourhood
        assert len(p["bins"]) == nb - (2 * cfg["dc_guard_bins"] + 1) - (2 * cfg["tone_guard_bins"] + 1)
        assert p["tone_bin"] not in set(p["bins"].tolist())
        # the idle frame is the carrier alone; the burst frame is comb plus
        # the same carrier, so the carrier is never interrupted
        assert np.abs(p["idle_frame"]).max() == pytest.approx(cfg["tone_amplitude"], rel=1e-5)
        assert np.abs(p["frame"] - p["idle_frame"]).max() == pytest.approx(
            cfg["burst_amplitude"], rel=1e-5)
        assert np.abs(p["frame"]).max() < 0.95                      # inside the DAC's rails

    def test_the_carrier_is_outside_every_measured_band(self):
        """It has to live somewhere nothing measures, or it comes back as
        exclusion machinery. The 800 kHz below the continuum band is free, and
        the analogue bandwidth is twice the sample rate so nothing rolls off."""
        inst, p = _plan()
        f = p["tone_hz"]
        assert not (inst["continuum_band_hz"][0] <= f <= inst["continuum_band_hz"][1])
        assert not (inst["h1_band_hz"][0] <= f <= inst["h1_band_hz"][1])
        assert inst["lo_hz"] - 0.5 * inst["sample_rate_hz"] < f < inst["continuum_band_hz"][0]
        # and far enough from the continuum edge that a strong carrier cannot
        # leak into it through the window
        assert (inst["continuum_band_hz"][0] - f) > 20 * inst["sample_rate_hz"] / inst["wide_channels"]

    def test_the_carrier_is_the_right_strength_relative_to_the_comb(self):
        """The pads set the absolute level; the digital ratio is what decides
        whether both hit their targets at once.

        Wanted since 2026-09-17: a comb tone at ~50x the noise in its own bin,
        which the receiver gain coming down to 20 dB is what affords, while the
        carrier stays at the 2.24x of whole-band noise power it already ran at.

        The carrier is held rather than carried up with the transmit gain
        because it runs in EVERY record, not just the bursts. Both come off one
        TX chain, so +10 dB moves both, and at the old 0.25 amplitude the
        carrier would have gone to 23x of band noise continuously - most of the
        new headroom spent, and a strong in-band CW tone is where
        intermodulation products come from, landing back across the measured
        band rather than staying in their own unmeasured bin.
        """
        inst, p = _plan()
        n = inst["wide_channels"]
        carrier = float(np.mean(np.abs(p["idle_frame"]) ** 2))
        comb_per_bin = float(np.mean(np.abs(p["frame"] - p["idle_frame"]) ** 2)) / len(p["bins"])
        COMB_RATIO = 50.0                     # the design target, per comb bin
        # what each comes to at the receiver, in units of whole-band noise power
        carrier_x = carrier / comb_per_bin * COMB_RATIO / n
        comb_x = COMB_RATIO * len(p["bins"]) / n
        assert carrier_x == pytest.approx(2.24, rel=0.2)
        assert 1 + carrier_x < 4.0                            # every science record
        # during a burst - and still an order below where the chain compresses
        # at 20 dB (x637, extrapolated from the 40 dB Sun drift's 8.45% deficit)
        assert 1 + carrier_x + comb_x < 637 / 10.0

    def test_off_means_off(self):
        inst, p = _plan({"receiver_pilot_enabled": False})
        assert not inst["pilot"]["enabled"] and len(p["bins"]) == 0
        inst2, _ = _plan({"receiver_pilot_burst_every_records": 0})
        assert not inst2["pilot"]["enabled"]
        with pytest.raises(ValueError):
            tuning.fixed_instrument({"receiver_pilot_burst_every_records": 1})
        with pytest.raises(ValueError):
            tuning.fixed_instrument({"receiver_pilot_burst_amplitude": 1.5})
        with pytest.raises(ValueError):
            tuning.fixed_instrument({"receiver_pilot_max_duty_cycle": 0.9})

    def test_the_cadence_is_a_time_not_a_record_count(self):
        """A record is not a fixed length: "every 20th record" is a burst a
        minute at 3 s and one every twenty minutes at 60 s. The interval is
        asked for in seconds, and met as far as the duty cap allows - a burst
        costs one whole record whatever its length, so at long integrations
        the cap wins and the answer is to use shorter records."""
        cfg = tuning.fixed_instrument()["pilot"]
        assert pilot.burst_every(cfg, 3.0) == 20            # a minute, 5%
        assert pilot.burst_every(cfg, 0.5) == 120           # still a minute, 0.8%
        assert pilot.burst_every(cfg, 10.0) == 20           # the duty cap, 200 s
        assert pilot.burst_every(cfg, 60.0) == 20           # the duty cap, 20 min
        for tau in (0.5, 3.0, 10.0, 60.0):                  # never above the cap
            assert 1.0 / pilot.burst_every(cfg, tau) <= cfg["max_duty_cycle"] + 1e-9
        # a larger duty buys cadence at long integrations, and an explicit
        # record count overrides the lot
        loose = tuning.fixed_instrument({"receiver_pilot_max_duty_cycle": 0.2})["pilot"]
        assert pilot.burst_every(loose, 60.0) == 5
        forced = tuning.fixed_instrument({"receiver_pilot_burst_every_records": 7})["pilot"]
        assert pilot.burst_every(forced, 3.0) == 7
        assert pilot.burst_every(tuning.fixed_instrument(
            {"receiver_pilot_enabled": False})["pilot"], 3.0) == 0

    def test_the_reference_is_flat_across_the_band(self):
        """A periodic frame under a rectangular window has no leakage at all,
        so every bin measures its own response with the same sensitivity.

        This is why the pilot has its own unwindowed FFT: through the wide
        product's Blackman-Harris window the reference follows the window in
        *time*, and a swept chirp maps time to frequency, so it collapses to
        zero at both band edges - exactly where the filter's tilt lives.
        """
        _, p = _plan()
        on = np.abs(p["reference"])[p["bins"]]
        assert on.max() / on.min() == pytest.approx(1.0, abs=1e-3)
        half = p["nbins"] // 2
        assert np.abs(p["reference"])[half] < 0.05 * on.min()
        # the windowed alternative, for the record: dead at the edges
        n = p["nbins"]
        m = np.arange(n)
        w = (0.35875 - 0.48829 * np.cos(2 * np.pi * m / (n - 1))
             + 0.14128 * np.cos(4 * np.pi * m / (n - 1))
             - 0.01168 * np.cos(6 * np.pi * m / (n - 1)))
        windowed = np.abs(np.fft.fftshift(np.fft.fft(w * p["frame"].astype(complex))))[p["bins"]]
        assert windowed.min() < 0.01 * np.median(windowed)


def _response(p, level, slope, ripple, ratio):
    """A field response with the given power level, tilt and SAW ripple, and
    the per-bin noise power that puts the burst `ratio` times above it."""
    bins = p["bins"]
    f = (p["freq_hz"][bins] - p["centre_hz"]) / 1e6
    h_ref = np.full(len(bins), 0.5 + 0j)
    gain_power = level * (1 + slope * f) * (1 + ripple * np.cos(2 * np.pi * f / 0.16))
    h = h_ref * np.sqrt(gain_power)
    noise = float(np.median(np.abs(h) ** 2 * np.abs(p["reference"][bins]) ** 2)) / ratio
    return h_ref, h, noise, 23400          # frames in a 3 s record at 8 Msps


def _synthetic(p, n_frames, h, noise_per_bin, rng, delay=0):
    """The accumulated cross-spectrum for a burst of response `h` (complex, per
    pilot bin; 0 for absent) in receive noise of the given per-bin power, as
    the receiver's gate would hand it over - with the framing offset `delay`
    the real thing has."""
    nb = p["nbins"]
    R = p["reference"]
    xspec = np.zeros(nb, dtype=complex)
    sig = np.sqrt(n_frames * noise_per_bin) * np.abs(R)
    xspec += sig * (rng.standard_normal(nb) + 1j * rng.standard_normal(nb)) / np.sqrt(2)
    xspec[p["bins"]] += n_frames * np.asarray(h) * np.abs(R[p["bins"]]) ** 2
    if delay:
        xspec = xspec * np.exp(-2j * np.pi * np.arange(nb) * delay / nb)
    return xspec, noise_per_bin


class TestEstimate:
    def test_nothing_there_is_reported_absent(self):
        inst, p = _plan()
        rng = np.random.default_rng(3)
        xs, noise = _synthetic(p, 23400, np.zeros(len(p["bins"])), 1.0, rng)
        est = pilot.estimate(xs, 23400, p, noise)
        assert est["snr_median"] < 2.5
        assert pilot.level_and_slope(est["h"], est["snr"], p, np.ones(len(p["bins"])), inst["pilot"]) is None

    def test_a_burst_at_the_noise_level_is_found_at_the_predicted_snr(self):
        """A burst whose power per bin equals the noise power gives SNR
        sqrt(frames) per bin: 153 per 3 s burst at 8 Msps, so 0.65% per bin."""
        inst, p = _plan()
        rng = np.random.default_rng(4)
        n = 23400
        a = 1.0 / np.abs(p["reference"][p["bins"]][0])        # tone power = noise power in the bin
        xs, noise = _synthetic(p, n, np.full(len(p["bins"]), a), 1.0, rng)
        est = pilot.estimate(xs, n, p, noise)
        assert est["snr_median"] == pytest.approx(np.sqrt(n), rel=0.1)

    def test_the_framing_offset_is_found_and_removed(self):
        inst, p = _plan()
        rng = np.random.default_rng(11)
        n = 23400
        a = 1.0 / np.abs(p["reference"][p["bins"]][0])
        h = np.full(len(p["bins"]), a * np.exp(0.3j))
        xs, noise = _synthetic(p, n, h, 1.0, rng, delay=137)
        est = pilot.estimate(xs, n, p, noise)
        assert est["delay_samples"] == 137
        # with the ramp removed the response is the flat one it started as
        ph = np.angle(est["h"])
        assert np.std(np.unwrap(ph)) < 0.05
        assert est["snr_median"] == pytest.approx(np.sqrt(n), rel=0.1)

    def test_level_and_slope_are_power_quantities_recovered_from_one_burst(self):
        """The counts are powers, so what divides them is |h|^2 - fitting the
        amplitude ratio and applying it to counts would be wrong by a square."""
        inst, p = _plan()
        rng = np.random.default_rng(5)
        level, slope = 1.03, -0.004                       # in power, as applied
        h_ref, h, noise, n = _response(p, level, slope, ripple=0.0, ratio=5.0)
        xs, _ = _synthetic(p, n, h, noise, rng, delay=42)
        est = pilot.estimate(xs, n, p, noise)
        lv, sl, err = pilot.level_and_slope(est["h"], est["snr"], p, h_ref, inst["pilot"])
        assert lv == pytest.approx(level, abs=0.002)
        assert sl == pytest.approx(slope, abs=0.0005)
        # the same numbers fitted on the amplitude ratio would be half the
        # departure from unity - the bug this guards
        assert abs((lv - 1) - 2 * (np.sqrt(lv) - 1)) < 0.001

    def test_the_ripple_is_recovered_by_averaging_bursts_and_one_burst_is_refused(self):
        """The SAW ripple, 0.1-0.15% of the passband and changing day to day,
        is the residual a stored template cannot follow. One burst's per-bin
        noise is larger than it; thirty, delay-filtered, take a 0.38 K ripple
        down to 0.10 K, below the 0.15 K thermal floor of a gain fit."""
        inst, p = _plan()
        cfg = inst["pilot"]
        rng = np.random.default_rng(7)
        lo, hi = inst["h1_band_hz"]
        fine = np.linspace(lo, hi, 845)
        ff = (fine - p["centre_hz"]) / 1e6
        expect = 1 + 0.0015 * np.cos(2 * np.pi * ff / 0.16)
        expect = expect / np.median(expect)
        h_ref, h, noise, n = _response(p, 1.03, -0.004, ripple=0.0015, ratio=5.0)
        tr = pilot.PilotTracker(cfg, p, reference_h=h_ref)
        residuals = {}
        for i in range(30):
            xs, _ = _synthetic(p, n, h, noise, rng)
            tr.burst(xs, n, noise, now=1000.0 + 60 * i)
            version, mean = tr.shape(1000.0 + 60 * i)
            if mean is None:
                # below the threshold the tracker refuses, rather than
                # handing over a shape whose noise exceeds the ripple
                assert tr.pilot_seconds < cfg["min_shape_pilot_s"]
                continue
            vec = pilot.correction_vector(mean, p, fine, (lo, hi), h_ref,
                                          max_delay_s=cfg["max_delay_us"] * 1e-6)
            residuals[i + 1] = float(np.std(vec - expect))
        # the first correction offered is the one at the 24 s threshold: nine
        # bursts of just under 3 s each, and it already beats leaving the ripple
        first = min(residuals)
        assert 8 <= first <= 10
        assert residuals[first] < np.std(expect - 1)
        # and it keeps improving as the averaging says it should, 1/sqrt(N)
        assert residuals[30] == pytest.approx(residuals[first] * np.sqrt(first / 30), rel=0.3)
        assert residuals[30] < 0.0004                          # 0.14 K on a 360 K system
        vec = pilot.correction_vector(tr.shape(1000.0 + 60 * 29)[1], p, fine, (lo, hi), h_ref,
                                      max_delay_s=cfg["max_delay_us"] * 1e-6)
        assert np.corrcoef(vec - 1, expect - 1)[0, 1] > 0.9
        assert np.median(vec) == pytest.approx(1.0, abs=1e-6)
        # the delay filter is what makes it affordable: the same bursts
        # without it leave twice the residual
        unfiltered = pilot.correction_vector(tr.shape(1000.0 + 60 * 29)[1], p, fine, (lo, hi),
                                             h_ref, max_delay_s=60e-6)
        assert np.std(unfiltered - expect) > 1.5 * residuals[30]

    def test_a_pilot_that_is_never_there_is_given_up_on(self):
        """It costs one record an interval for nothing - the state until the
        dipole is wired - but an intermittent one is a fault to record, not a
        reason to stop measuring."""
        inst, p = _plan()
        cfg = inst["pilot"]
        rng = np.random.default_rng(21)
        tr = pilot.PilotTracker(cfg, p, reference_h=np.full(len(p["bins"]), 0.5 + 0j))
        for i in range(cfg["give_up_after_bursts"]):
            assert not tr.give_up()
            xs, noise = _synthetic(p, 23400, np.zeros(len(p["bins"])), 1.0, rng)
            tr.burst(xs, 23400, noise, now=1000.0 + 60 * i)
        assert tr.give_up()
        # one that was seen once is never given up on
        h_ref, h, noise, n = _response(p, 1.0, 0.0, 0.0, 5.0)
        seen = pilot.PilotTracker(cfg, p, reference_h=h_ref)
        xs, _ = _synthetic(p, n, h, noise, rng)
        seen.burst(xs, n, noise, now=1000.0)
        for i in range(20):
            xs, _ = _synthetic(p, n, np.zeros(len(p["bins"])), noise, rng)
            seen.burst(xs, n, noise, now=1100.0 + 60 * i)
        assert seen.detected == 1 and not seen.give_up()

    def test_the_burst_switch_off_margin_fits_inside_the_record(self):
        """At 0.1 s records a flat 0.3 s margin would switch the comb off
        before it was ever on, and the record would be dropped for a burst
        that never happened."""
        cfg = tuning.fixed_instrument()["pilot"]
        assert pilot.burst_off_margin_s(cfg, 3.0) == pytest.approx(cfg["burst_off_margin_s"])
        assert pilot.burst_off_margin_s(cfg, 0.1) == pytest.approx(0.025)
        for tau in (0.05, 0.1, 1.0, 3.0, 60.0):
            assert 0 < pilot.burst_off_margin_s(cfg, tau) < tau

    def test_the_factor_is_the_line_and_is_clamped(self):
        f = np.array([1415e6, 1419e6, 1423e6])
        fac = pilot.factor(1.02, -0.005, f, 1419e6)
        assert fac == pytest.approx([1.02 * 1.02, 1.02, 1.02 * 0.98])
        assert pilot.factor(9.0, 0.0, f, 1419e6).max() == 2.0

    def test_the_tracker_holds_a_burst_then_lets_go(self):
        inst, p = _plan()
        cfg = inst["pilot"]
        rng = np.random.default_rng(6)
        n = 23400
        a = 1.0 / np.abs(p["reference"][p["bins"]][0])
        tr = pilot.PilotTracker(cfg, p, reference_h=None)
        assert tr.correction(0.0) == (1.0, 0.0, 0)
        xs, noise = _synthetic(p, n, np.full(len(p["bins"]), a), 1.0, rng)
        b1 = tr.burst(xs, n, noise, now=1000.0)
        assert b1["ok"] and b1["level"] == pytest.approx(1.0, abs=0.01) and not tr.anchored
        lvl, slp, ok = tr.correction(1030.0)
        assert ok and lvl == pytest.approx(1.0, abs=0.01)
        # the response raised by 5% in *amplitude* is 10.25% in power, which
        # is what the level means and what divides the counts
        xs, noise = _synthetic(p, n, np.full(len(p["bins"]), 1.05 * a), 1.0, rng)
        b2 = tr.burst(xs, n, noise, now=1060.0)
        assert b2["ok"] and b2["level"] == pytest.approx(1.05 ** 2, abs=0.01)
        assert tr.correction(1090.0)[0] == pytest.approx(1.05 ** 2, abs=0.01)
        # a vanished pilot: the burst reads absent, and after hold_bursts
        # intervals nothing is applied any more
        xs, noise = _synthetic(p, n, np.zeros(len(p["bins"])), 1.0, rng)
        b3 = tr.burst(xs, n, noise, now=1120.0)
        assert not b3["ok"]
        assert tr.correction(1150.0)[2] == 1                       # still within the hold
        assert tr.correction(1120.0 + 60 * cfg["hold_bursts"] + 100)[2] == 0
        version, mean = tr.shape(1130.0)
        # two 3 s bursts is 6 s of pilot, not the 24 s a shape correction
        # needs, and the tracker says so rather than handing over a noisy one
        assert mean is None and version == 2 and tr.detected == 2 and tr.bursts == 3
        assert tr.pilot_seconds == pytest.approx(6.0, rel=0.02)


def _file(path, monkeypatch, pilots, calibrated, fine_vec=None, pilot_over=None):
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
    inst = tuning.fixed_instrument(pilot_over or {})
    p = pilot.plan(inst["pilot"], inst["lo_hz"], inst["sample_rate_hz"], inst["wide_channels"])
    lo, hi = inst["h1_band_hz"]
    f_h1 = np.linspace(lo, hi, 300)
    f_wide = pilot.wide_axis(inst["lo_hz"], inst["sample_rate_hz"], inst["wide_channels"])
    hf = rx.init_hdf5(path, f_h1, len(f_h1), "demo", inst["lo_hz"], inst["sample_rate_hz"],
                      inst["gain_db"], wide={"freq_axis_hz": f_wide, "channels": len(f_wide)},
                      instrument=inst, pilot=p)
    raw_h1, raw_w = [], []
    try:
        if fine_vec is not None:
            idx = rx.append_pilot_correction(hf, fine_vec, np.ones(len(f_wide)))
            for pil in pilots:
                if pil.get("ok"):
                    pil["corr_index"] = idx
        for i, pil in enumerate(pilots):
            h1 = np.full(len(f_h1), 0.003) * (1 + 0.01 * i)
            wide = np.full(len(f_wide), 0.002) * (1 + 0.01 * i)
            raw_h1.append(h1.copy()); raw_w.append(wide.copy())
            rx.append_spectrum(hf, h1, 1.7e9 + 3.0 * i, 3.0, len(f_h1),
                               wide_linear=wide, overflows=0, pilot=pil)
    finally:
        hf.close()
    return f_h1, f_wide, np.array(raw_h1), np.array(raw_w), p


ABSENT_BURST = {"burst": 1, "ok": 0, "level": 1.0, "slope": 0.0, "snr": 0.9, "seen": 0, "corr_index": -1}
CLEAN = {"burst": 0, "ok": 0, "level": 1.0, "slope": 0.0, "snr": 0.0, "seen": 0, "corr_index": -1}


class TestFile:
    def test_layout_and_the_unplugged_tx_writes_unit_factors(self, tmp_path, monkeypatch):
        path = str(tmp_path / "p.h5")
        _file(path, monkeypatch, [CLEAN, ABSENT_BURST, CLEAN], calibrated=False)
        with h5py.File(path, "r") as hf:
            for name in ("pilot_burst", "pilot_level", "pilot_slope", "pilot_snr", "pilot_ok",
                         "pilot_correction_index", "pilot_shape", "pilot_shape_time",
                         "pilot_correction_h1", "pilot_correction_wide", "pilot_bins_wide",
                         "pilot_reference", "pilot_anchored"):
                assert name in hf, name
            assert list(hf["pilot_burst"][:]) == [0, 1, 0]
            assert list(hf["pilot_ok"][:]) == [0, 0, 0]
            assert list(hf["pilot_level"][:]) == [1.0, 1.0, 1.0]
            assert list(hf["pilot_correction_index"][:]) == [-1, -1, -1]
            assert json.loads(hf.attrs["pilot"])["burst_interval_s"] == 60.0
            assert hf["pilot_shape"].shape[0] == 0

    def test_kelvin_is_divided_by_factor_and_shape_and_read_back_exactly_without_bursts(self, tmp_path, monkeypatch):
        from observation_plot import read_observation
        path = str(tmp_path / "k.h5")
        fine_vec = 1 + 0.003 * np.cos(np.linspace(0, 40, 300))
        pilots = [dict(CLEAN),
                  {"burst": 1, "ok": 0, "level": 1.0, "slope": 0.0, "snr": 150.0, "seen": 1, "corr_index": -1},
                  {"burst": 0, "ok": 1, "level": 1.03, "slope": -0.004, "snr": 0.0, "seen": 0},
                  {"burst": 2, "ok": 0, "level": 1.0, "slope": 0.0, "snr": 0.0, "seen": 0, "corr_index": -1},
                  {"burst": 0, "ok": 1, "level": 0.98, "slope": 0.002, "snr": 0.0, "seen": 0}]
        f_h1, f_wide, raw_h1, raw_w, p = _file(path, monkeypatch, pilots, calibrated=True, fine_vec=fine_vec)
        with h5py.File(path, "r") as hf:
            assert "spectra_kelvin" in hf and int(hf.attrs["pilot_applied"]) == 1
            k = hf["spectra_kelvin"][:]
            fc = float(hf.attrs["pilot_centre_hz"])
            assert k[0] == pytest.approx(raw_h1[0] / 2.0e-6 - 300.0, rel=1e-6)
            expect = raw_h1[2] / (2.0e-6 * pilot.factor(1.03, -0.004, f_h1, fc) * fine_vec) - 300.0
            assert k[2] == pytest.approx(expect, rel=1e-5)
            assert list(hf["pilot_correction_index"][:]) == [-1, -1, 0, -1, 0]
            assert list(hf["pilot_burst"][:]) == [0, 1, 0, 2, 0]
        f, counts, stamps, taus, header = read_observation(path)
        # exact reversal, and the burst and the contaminated record dropped
        assert counts.shape[0] == 3 and len(stamps) == 3 and len(taus) == 3
        assert counts == pytest.approx(raw_h1[[0, 2, 4]], rel=1e-5)
        assert header["pilot_bursts"] == 1 and header["pilot_records_corrected"] == 2
        assert header["pilot_records_dropped"] == 2
        _, all_counts, _, _, _ = read_observation(path, drop_bursts=False)
        assert all_counts.shape[0] == 5
        fw, cw, _, _, hw = read_observation(path, product="wide")
        assert cw == pytest.approx(raw_w[[0, 2, 4]], rel=1e-5)

    def test_the_live_sidecar_marks_bursts(self, tmp_path, monkeypatch):
        path = str(tmp_path / "s.h5")
        _file(path, monkeypatch, [ABSENT_BURST, {"burst": 0, "ok": 1, "level": 1.02, "slope": 0.001,
                                                 "snr": 0.0, "seen": 0, "corr_index": -1}], calibrated=False)
        lines = [json.loads(l) for l in open(str(tmp_path / "s.live.jsonl"))]
        assert lines[0]["pilot_burst"] == 1 and lines[0]["pilot_seen"] == 0
        assert lines[1]["pilot_burst"] == 0 and lines[1]["pilot_ok"] == 1
        assert lines[1]["pilot_level"] == pytest.approx(1.02)


class TestDemoFlowgraph:
    def test_the_gate_counts_the_blocks_a_burst_occupied(self):
        """The recovery end to end on the demo source: with the comb switched
        on for part of the run the power gate counts those blocks and the
        burst is detected; with it off nothing is (the state until the dipole
        is wired).

        The flowgraph is stopped in a finally: a GNU Radio top block destroyed
        while still running calls std::terminate, which takes the whole test
        process with it - one failed assertion here brought down the suite.
        """
        if "--headless" not in sys.argv:
            sys.argv.append("--headless")
        import b210_h1_receiver as rx
        inst = tuning.fixed_instrument({"receiver_pilot_demo_inject": True})
        fg = rx.TwoProductFlowgraph("demo", inst, strict=False)
        assert fg.pilot is not None and fg.pilot_sink is None and fg.pilot_gate is not None
        fg.start()
        try:
            time.sleep(1.5)
            _, n_on, n_total, noise = fg.take_pilot()
            assert n_on == 0 and n_total > 1000                  # comb off: nothing gated on
            assert np.all(noise >= 0) and noise.mean() > 0       # per bin, from the off blocks
            fg.set_burst(True)
            time.sleep(1.0)
            fg.set_burst(False)
            time.sleep(0.5)
            xs, n_on, n_total, noise = fg.take_pilot()
        finally:
            fg.stop()
            fg.wait()
        assert 0 < n_on < n_total                                # on for part of the interval
        est = pilot.estimate(xs, n_on, fg.pilot, noise)
        assert est["snr_median"] > 10 * inst["pilot"]["detect_snr"]


class TestGate:
    """The gate decides which 20 ms blocks held the comb. Its failure modes
    are the ones that stay invisible until the transmitter is connected."""

    VLEN, PRESUM = 1024, 156

    def _gate(self, planned, sigma=8.0):
        if "--headless" not in sys.argv:
            sys.argv.append("--headless")
        import b210_h1_receiver as rx
        g = rx._PilotGate(self.VLEN, self.PRESUM, sigma)
        g.set_reference_power(np.abs(planned["reference"]) ** 2)
        return g

    def _blocks(self, planned, gate, fraction_on, blocks, sky=1.0, ratio=5.0, rng=None):
        """`blocks` blocks in which `fraction_on` of the frames carried the comb,
        on a sky `sky` times the reference brightness."""
        rng = rng or np.random.default_rng(5)
        bins, R = planned["bins"], planned["reference"]
        refpow = np.abs(R) ** 2
        X = np.zeros((blocks, self.VLEN), dtype=np.complex64)
        P = np.zeros((blocks, self.VLEN), dtype=np.float32)
        for i in range(blocks):
            p_row = np.full(self.VLEN, self.PRESUM * sky)
            x_row = (np.sqrt(self.PRESUM * sky * refpow)
                     * (rng.standard_normal(self.VLEN) + 1j * rng.standard_normal(self.VLEN))
                     / np.sqrt(2))
            if fraction_on:
                h = np.sqrt(ratio * sky / refpow[bins][0])
                ramp = np.exp(-2j * np.pi * np.arange(len(bins)) * 137 / self.VLEN)
                x_row[bins] += fraction_on * self.PRESUM * h * refpow[bins] * ramp
                p_row[bins] += fraction_on * self.PRESUM * ratio * sky
            X[i], P[i] = x_row, p_row
        gate.work([X, P], [])

    def test_it_finds_the_comb_by_its_own_signature_not_by_power_or_by_the_command(self):
        """The statistic is the cross-spectrum's delay peak against the
        block's own noise, so it is absolute. A power test would have to be
        judged against a running baseline - which deadlocks the first time
        the received power steps up and stays up - and the command would be
        silent about the tail the transmit buffers leave behind."""
        _, p = _plan()
        gate = self._gate(p)
        self._blocks(p, gate, 0.0, 40)
        _, n_on, n, noise = gate.take()
        assert n_on == 0 and n > 0 and noise.mean() > 0
        self._blocks(p, gate, 1.0, 40)
        _, n_on, n, _ = gate.take()
        assert n_on > 0.8 * n                       # all but the edge blocks

    def test_it_reads_the_same_on_the_sun_as_on_cold_sky(self):
        """The comb raises the total power by only 30% with the Sun in the
        beam, which a power test's margin would have to be under - while
        still beating the per-block scatter. This one does not care."""
        _, p = _plan()
        for sky in (1.0, 6.25):                     # cold sky; the Sun
            gate = self._gate(p)
            self._blocks(p, gate, 0.0, 20, sky=sky)
            assert gate.take()[1] == 0
            self._blocks(p, gate, 1.0, 30, sky=sky)
            assert gate.take()[1] > 0

    def test_it_catches_a_tail_a_command_would_have_missed(self):
        """The transmit buffers empty tens of milliseconds after the comb is
        switched off. A science record that caught that tail has to be known,
        or it is reduced as sky: a comb at five times the noise over 1% of a
        3 s record is a 7 K error."""
        _, p = _plan()
        gate = self._gate(p)
        self._blocks(p, gate, 0.0, 20)
        gate.take()
        self._blocks(p, gate, 0.05, 3)              # a twentieth of a block
        self._blocks(p, gate, 0.0, 20)
        _, n_on, _, _ = gate.take()
        assert n_on > 0

    def test_partial_blocks_at_the_edges_are_dropped(self):
        """A block half covered by the burst still reads on, but counting it
        whole would bias the response by up to 1.3%; it is committed only
        when both its neighbours agree."""
        _, p = _plan()
        gate = self._gate(p)
        self._blocks(p, gate, 0.0, 10)
        self._blocks(p, gate, 0.5, 1)               # the leading edge
        self._blocks(p, gate, 1.0, 20)
        self._blocks(p, gate, 0.5, 1)               # the trailing edge
        self._blocks(p, gate, 0.0, 10)
        _, n_on, _, _ = gate.take()
        assert n_on == pytest.approx(20 * self.PRESUM, rel=0.01)


class TestCarrier:
    def test_its_power_is_read_from_the_wide_product_and_needs_no_recovery(self):
        """At ~1000x the noise in its own bin the carrier is simply an excess
        in the channels it occupies, which the wide product already records."""
        inst, p = _plan()
        cfg = inst["pilot"]
        rng = np.random.default_rng(12)
        nb = inst["wide_channels"]
        noise = 3.0e-6
        wide = rng.exponential(noise, nb)                 # power spectrum of noise
        assert not pilot.tone_power(wide, p, cfg)["detected"]        # nothing there
        # a carrier spread over the window's main lobe
        for d, w in ((-2, 0.05), (-1, 0.5), (0, 1.0), (1, 0.5), (2, 0.05)):
            wide[p["tone_bin"] + d] += 400 * noise * w
        got = pilot.tone_power(wide, p, cfg)
        assert got["detected"] and got["ratio"] > 100
        assert got["power"] == pytest.approx(400 * noise * 2.1, rel=0.1)

    def test_a_stale_reference_stops_being_used(self):
        """If the bursts stop, the carrier's reference ages: it would then be
        carrying the slow drift the burst is supposed to carry, so it stops
        being applied rather than silently changing meaning."""
        inst, p = _plan()
        cfg = inst["pilot"]
        tr = pilot.PilotTracker(cfg, p, reference_h=None, expected_interval_s=60.0)
        tr.set_tone_reference(5.0, now=1000.0)
        assert tr.tone_level({"detected": True, "power": 5.05}, now=1060.0)[1] == 1
        stale = 1000.0 + cfg["hold_bursts"] * 60.0 + 10
        assert tr.tone_level({"detected": True, "power": 5.05}, now=stale) == (1.0, 0)

    def test_the_level_is_measured_against_the_last_burst_so_nothing_counts_twice(self):
        """The burst's level carries the slow change; the carrier carries only
        what happened since, so at a burst it reads unity by construction."""
        inst, p = _plan()
        tr = pilot.PilotTracker(inst["pilot"], p, reference_h=None)
        assert tr.tone_level({"detected": True, "power": 5.0}) == (1.0, 0)   # no reference yet
        tr.set_tone_reference(5.0)
        assert tr.tone_level({"detected": True, "power": 5.0})[0] == pytest.approx(1.0)
        lvl, ok = tr.tone_level({"detected": True, "power": 5.05})
        assert ok and lvl == pytest.approx(1.01)
        assert tr.tone_level({"detected": False, "power": 5.05}) == (1.0, 0)
        assert tr.tone_level({"detected": True, "power": 50.0}) == (1.0, 0)  # absurd, refused

    def test_it_is_recorded_but_not_applied_until_the_bench_test_says_so(self, tmp_path, monkeypatch):
        """Applying it corrects a wobble in the SAWbird or the B210, does
        nothing for an atmospheric one, and substitutes the transmit chain's
        own. So the series is recorded always and applied only on request."""
        from observation_plot import read_observation
        rec = {"burst": 0, "ok": 1, "level": 1.0, "slope": 0.0, "snr": 0.0, "seen": 0,
               "corr_index": -1, "tone_power": 5.0, "tone_ratio": 400.0,
               "tone_ok": 1, "tone_level": 1.02}
        off = str(tmp_path / "off.h5")
        _f, _fw, raw, _rw, _p = _file(off, monkeypatch, [dict(rec)], calibrated=True)
        with h5py.File(off, "r") as hf:
            assert int(hf.attrs["pilot_tone_applied"]) == 0
            assert hf["pilot_tone_power"][0] == pytest.approx(5.0)
            assert hf["pilot_tone_level"][0] == 1.0          # measured, not applied
            assert hf["spectra_kelvin"][0] == pytest.approx(raw[0] / 2.0e-6 - 300.0, rel=1e-6)
        on = str(tmp_path / "on.h5")
        _f, _fw, raw, _rw, _p = _file(on, monkeypatch, [dict(rec)], calibrated=True,
                                      pilot_over={"receiver_pilot_tone_apply": True})
        with h5py.File(on, "r") as hf:
            assert int(hf.attrs["pilot_tone_applied"]) == 1
            assert hf["pilot_tone_level"][0] == pytest.approx(1.02)
            assert hf["spectra_kelvin"][0] == pytest.approx(raw[0] / (2.0e-6 * 1.02) - 300.0, rel=1e-6)
        _, counts, _, _, header = read_observation(on)
        assert counts[0] == pytest.approx(raw[0], rel=1e-6)   # and reversed exactly
        assert header["pilot_tone_records"] == 1 and header["pilot_tone_applied"] == 1


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
        assert d["pilot"]["enabled"] and "burst" in d["pilot_description"]
        r = client.post("/api/config", json={"receiver_pilot_burst_interval_s": 120})
        assert r.status_code == 200, r.get_json()
        assert client.get("/api/instrument").get_json()["pilot"]["burst_interval_s"] == 120.0
        r = client.post("/api/config", json={"receiver_pilot_max_duty_cycle": 0.9})
        assert r.status_code == 400
        d = client.get("/api/pilot/status").get_json()
        assert d["success"] and "burst" in d["description"]

    def test_the_live_summary_counts_bursts_and_corrections(self, sched):
        recs = [{"pilot_ok": 0, "pilot_burst": 1, "pilot_seen": 1, "pilot_snr": 120.0},
                {"pilot_ok": 1, "pilot_burst": 0, "pilot_level": 1.02, "pilot_slope": 0.001},
                {"pilot_ok": 1, "pilot_burst": 0, "pilot_level": 1.01, "pilot_slope": 0.0}]
        s = sched._pilot_summary(recs)
        assert s["bursts"] == 1 and s["bursts_seen"] == 1 and s["corrected"] == 2
        assert s["latest_ok"] and s["latest_level"] == pytest.approx(1.01)
