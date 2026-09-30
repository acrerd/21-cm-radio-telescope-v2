"""TOAs from folded profiles: the fit, the arrival time, the .tim file, PINT."""
import json
import os
import subprocess

import h5py
import numpy as np
import pytest

import pulsar_fold as PF
import pulsar_toa as T

PINT_PY = "/home/astro/radioconda/envs/pint/bin/python"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_fit_finds_a_sub_bin_shift_and_its_error_is_honest():
    """Monte Carlo: the fitted shift is unbiased and its quoted error matches
    the scatter - the error bars are what a timing fit weights by."""
    tm = T.template(1024, 1e-3, 32e6, 1413e6)
    rng = np.random.default_rng(3)
    true = 0.01234
    k = np.arange(1, 513)
    shifted = np.fft.irfft(np.concatenate([[np.fft.rfft(tm)[0]], np.fft.rfft(tm)[1:] * np.exp(-2j * np.pi * k * true)]), n=1024)
    taus, sig = [], []
    for _ in range(300):
        f = T.fit_shift(0.3 + 0.05 * shifted + 0.004 * rng.standard_normal(1024), tm)
        taus.append(f["tau"]); sig.append(f["sigma_tau"])
    taus, sig = np.array(taus), np.array(sig)
    assert np.mean(taus) == pytest.approx(true, abs=3 * np.std(taus) / np.sqrt(len(taus)))
    assert np.std(taus) / np.mean(sig) == pytest.approx(1.0, abs=0.15)
    assert 1e-4 < np.mean(sig) < 1e-2                              # sub-bin: a bin is 1e-3


def test_a_run_offset_from_the_ephemeris_gives_the_offset_back(tmp_path):
    """A synthetic hour with its pulse 0.02 of a period late of the
    ephemeris: the TOA is where the phase model reads 0.5 + 0.02."""
    p = dict(PF.lookup("B0329+54"))
    dt, n, t0 = 1e-3, 3_600_000, 1790000000.0
    t = t0 + dt * (np.arange(n) + PF.ROW_CENTRE)
    ph, pm = PF.absolute_phase(t, p, 1413e6)
    tm = T.template(1024, dt, 32e6, 1413e6)
    late = 0.02
    pulse = np.interp(((ph - late) % 1.0) * 1024 - 0.5, np.arange(1024), tm, period=1024)
    rng = np.random.default_rng(5)
    power = (1.0 + 0.004 * pulse + 0.0056 * rng.standard_normal(n)).astype(np.float32)[:, None]
    path = str(tmp_path / "late_pulsar.h5")
    with h5py.File(path, "w") as hf:
        hf.create_dataset("frequency_hz", data=np.array([1413e6]))
        hf.create_dataset("power", data=power, chunks=(4096, 1))
        hf.create_dataset("time_marks", data=np.array([[0, t0]]))
        hf.attrs["dt_s"] = dt; hf.attrs["t0_unix"] = t0; hf.attrs["sample_rate_hz"] = 32e6
    toas = T.toas_for_recording(path, segments=2, tmpl=tm)
    assert len(toas) == 3 and toas[0]["pps"] == 0
    whole = toas[0]
    assert whole["tau"] == pytest.approx(late, abs=4 * whole["sigma_tau"])
    at, _ = PF.absolute_phase(np.array([whole["t_unix"] - 10, whole["t_unix"], whole["t_unix"] + 10]), p, 1413e6)
    assert (at[1] - 0.5 - whole["tau"] + 0.5) % 1.0 - 0.5 == pytest.approx(0.0, abs=1e-6)
    assert whole["err_us"] == pytest.approx(whole["sigma_tau"] * pm * 1e6, rel=1e-3)


def _synthetic_run(path, t0, minutes, late, seed, session=""):
    """B0329 at the absolute phase, `late` of a period behind the ephemeris."""
    p = dict(PF.lookup("B0329+54"))
    dt, n = 1e-3, int(minutes * 60_000)
    t = t0 + dt * (np.arange(n) + PF.ROW_CENTRE)
    ph, _ = PF.absolute_phase(t, p, 1413e6)
    tm = T.template(1024, dt, 32e6, 1413e6)
    pulse = np.interp(((ph - late) % 1.0) * 1024 - 0.5, np.arange(1024), tm, period=1024)
    rng = np.random.default_rng(seed)
    power = (1.0 + 0.004 * pulse + 0.0056 * rng.standard_normal(n)).astype(np.float32)[:, None]
    with h5py.File(path, "w") as hf:
        hf.create_dataset("frequency_hz", data=np.array([1413e6]))
        hf.create_dataset("power", data=power, chunks=(4096, 1))
        hf.create_dataset("time_marks", data=np.array([[0, t0]]))
        hf.attrs["dt_s"] = dt; hf.attrs["t0_unix"] = t0; hf.attrs["sample_rate_hz"] = 32e6
        if session:
            hf.attrs["pulsar_session"] = session
            hf.attrs["time_source"] = "pps"; hf.attrs["time_pps_verified"] = 1
    return tm


def test_a_night_in_pieces_is_one_toa_and_the_pieces_are_checks(tmp_path, monkeypatch):
    """A pulsar-monitor night interrupted by a booking: two 40-min pieces an
    hour apart, same session. Their folds add at the absolute phase into one
    night TOA, which recovers the offset with a smaller error than either
    piece; each piece's own TOA is named as a check and kept out of the .tim."""
    monkeypatch.setattr(T, "PROFILE_CACHE_DIR", str(tmp_path / "profiles"))
    late, t0 = 0.02, 1790000000.0
    runs = [(str(tmp_path / "20260928_200000_pulsar.h5"), t0, 11),
            (str(tmp_path / "20260928_214000_pulsar.h5"), t0 + 6000.0, 12)]
    per_run = []
    for path, start, seed in runs:
        tm = _synthetic_run(path, start, 40, late, seed, session="2026-09-28")
        analysis = PF.analyse_file(path, toa_bins=T.TOA_BINS)
        assert analysis[1].get("pulsar_session") == "2026-09-28"
        T.cache_profile(path, analysis[0]["toa"], analysis[1])
        per_run.append(T.timing_toas(path, analysis, segments=1))
    toas = per_run[-1]
    names = [t["name"] for t in toas]
    assert names == ["20260928_214000_pulsar.h5_p", "night_2026-09-28"]
    night = toas[-1]
    piece = toas[0]
    assert night["pieces"] == 2 and night["tobs_s"] == pytest.approx(4800, abs=5)
    assert night["pps"] == 1
    assert night["tau"] == pytest.approx(late, abs=4 * night["sigma_tau"])
    assert night["sigma_tau"] < 0.85 * piece["sigma_tau"]                  # ~1/sqrt(2)
    p = dict(PF.lookup("B0329+54"))
    at, _ = PF.absolute_phase(np.array([night["t_unix"] - 10, night["t_unix"], night["t_unix"] + 10]), p, 1413e6)
    assert (at[1] - 0.5 - night["tau"] + 0.5) % 1.0 - 0.5 == pytest.approx(0.0, abs=1e-6)
    tim = str(tmp_path / "b0329.tim")
    T.write_tim(per_run[0] + per_run[1], tim)
    assert _names(tim) == ["night_2026-09-28"]                            # rewritten, not duplicated
    assert _names(T.segment_file(tim)) == ["20260928_200000_pulsar.h5_p", "20260928_214000_pulsar.h5_p"]


def test_mjd_text_keeps_the_nanoseconds():
    t = 1790467200.3005433
    s = T.mjd_string(t)
    from astropy.time import Time
    back = Time(int(s.split(".")[0]), float("0." + s.split(".")[1]), format="mjd", scale="utc").unix
    assert back == pytest.approx(t, abs=2e-7)                     # float64 unix's own resolution
    assert len(s.split(".")[1]) == 13


def _toa(name, mjd):
    return dict(name=name, mjd="%.13f" % mjd, err_us=300.0, freq_mhz=1413.0, snr=10.0,
                tobs_s=3600, bw_mhz=32.0, pps=0, template="t")


def _names(path):
    return [ln.split()[0] for ln in open(path) if ln.strip() and not ln.startswith(("FORMAT", "C "))]


def test_segments_are_kept_out_of_the_tim_a_fit_reads(tmp_path):
    """A segment is the same data as its night again, and a timing program
    fitting one file would count the night twice: the nights go in the .tim,
    the segments in a file of their own, and one written the old way - all
    in one file - is split on the next write."""
    tim = str(tmp_path / "b0329.tim")
    seg = T.segment_file(tim)
    assert seg == str(tmp_path / "b0329_segments.tim")
    with open(tim, "w") as fh:
        fh.write("FORMAT 1\n" + T.tim_line(_toa("a.h5_s0", 61309.9)) + "\n"
                 + T.tim_line(_toa("a.h5", 61310.1)) + "\n" + T.tim_line(_toa("a.h5_s1", 61310.2)) + "\n")
    T.write_tim([_toa("b.h5", 61311.1), _toa("b.h5_s0", 61310.9), _toa("a.h5", 61310.1)], tim)
    assert _names(tim) == ["a.h5", "b.h5"]
    assert _names(seg) == ["a.h5_s0", "a.h5_s1", "b.h5_s0"]                  # by time
    assert open(seg).readline() == "FORMAT 1\n"
    assert any(ln.startswith("C ") for ln in open(seg))


def test_a_night_without_segments_makes_no_segment_file(tmp_path):
    tim = str(tmp_path / "b0329.tim")
    T.write_tim([_toa("a.h5", 61310.1)], tim)
    assert _names(tim) == ["a.h5"] and not os.path.exists(T.segment_file(tim))


@pytest.mark.skipif(not os.path.exists(PINT_PY), reason="no PINT environment")
def test_pint_agrees_with_our_phase_and_reads_our_tim(tmp_path):
    """PINT's arrivals at our site, from our par, sit at one fractional phase
    of absolute_phase (1 us over 5 h, 2026-09-27), and written back through
    our .tim writer they give PINT zero residuals."""
    par = T.write_par(str(tmp_path / "b.par"))
    mj = tmp_path / "mjds.json"
    json.dump([61310.0 + i / 24 for i in range(6)], open(mj, "w"))
    site = ["55.902426", "-4.307865", "50.0"]
    out = subprocess.run([PINT_PY, os.path.join(HERE, "pint_tools.py"), "fake", par, str(mj), "1413"] + site,
                         capture_output=True, text=True, timeout=600)
    t = np.array(json.loads(out.stdout.strip().splitlines()[-1])["t_unix"])
    p = dict(PF.B0329, phase_offset=0.0)
    fr = np.array([PF.absolute_phase(np.array([x - 30, x, x + 30]), p, 1413e6)[0][1] % 1.0 for x in t])
    spread_us = ((fr - fr[0] + 0.5) % 1.0 - 0.5) * 0.7145e6
    assert np.max(np.abs(spread_us)) < 5.0
    toas = [dict(name="f%d" % i, t_unix=x, mjd=T.mjd_string(x), err_us=1.0, freq_mhz=1413.0, snr=1,
                 tobs_s=0, bw_mhz=32, pps=1, template="t") for i, x in enumerate(t)]
    tim = T.write_tim(toas, str(tmp_path / "rt.tim"))
    out = subprocess.run([PINT_PY, os.path.join(HERE, "pint_tools.py"), "residuals", par, tim] + site,
                         capture_output=True, text=True, timeout=600)
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert np.max(np.abs(res["resid_us"])) < 0.5


@pytest.mark.skipif(not os.path.exists(PINT_PY), reason="no PINT environment")
def test_the_fit_reports_when_f0_is_free(tmp_path):
    """Three nights spanning a day free F0, and the result must still come
    back as JSON: PINT's uncertainties are longdouble, and the first real
    night that freed F0 (2026-09-29) crashed the fit with nothing on stdout."""
    par = T.write_par(str(tmp_path / "b.par"))
    mj = tmp_path / "mjds.json"
    json.dump([61310.1, 61311.1, 61312.1], open(mj, "w"))
    site = ["55.902426", "-4.307865", "50.0"]
    out = subprocess.run([PINT_PY, os.path.join(HERE, "pint_tools.py"), "fake", par, str(mj), "1413"] + site,
                         capture_output=True, text=True, timeout=600)
    t = json.loads(out.stdout.strip().splitlines()[-1])["t_unix"]
    toas = [dict(name="n%d.h5" % i, t_unix=x, mjd=T.mjd_string(x), err_us=500.0, freq_mhz=1413.0, snr=10,
                 tobs_s=3600, bw_mhz=32, pps=1, template="t") for i, x in enumerate(t)]
    tim = T.write_tim(toas, str(tmp_path / "b0329.tim"))
    fit = T.timing_fit(tim, par)
    assert "error" not in fit, fit
    assert fit["free"] == ["F0"] and fit["n_whole"] == 3
    c = T.PF.B0329                                   # the catalogue, carried to the data epoch
    f_then = c["f0"] + c["f1"] * (fit["pepoch"] - c["pepoch_mjd"]) * 86400.0
    assert 0 < fit["P_err"] < 1e-7 and abs(fit["P"] - 1 / f_then) < 5 * fit["P_err"]


def _stored_night(store, name, frac, weight, cal=None):
    """A profile store entry made directly: weighted sums S = C * frac."""
    extra = {} if cal is None else {"cal_t_sys_k": cal[0], "cal_effective_area_m2": cal[1]}
    np.savez(os.path.join(store, name + ".npz"), S=weight * frac, C=weight * np.ones_like(frac), hours=1.0,
             phase_offset=PF.B0329["phase_offset"], row_centre=PF.ROW_CENTRE, nbins=T.TOA_BINS, **extra)


def test_the_stack_puts_each_night_on_its_own_calibration(tmp_path, monkeypatch):
    """Two nights with the same fractional pulse, recorded at T_sys 200 and
    400 K, and a third with no calibration stored, which takes the one in
    force (300 K). Equal weights, so the stack in kelvin is 300 K times the
    fraction - each night converted before adding, not one T_sys for all."""
    store = tmp_path / "profiles"; store.mkdir()
    monkeypatch.setattr(T, "PROFILE_CACHE_DIR", str(store))
    monkeypatch.setattr(T, "OBS_DIR", str(tmp_path / "none"))
    monkeypatch.setattr(PF, "calibration_in_force",
                        lambda: {"cal_t_sys_k": 300.0, "cal_effective_area_m2": 7.0})
    frac = np.zeros(T.TOA_BINS); frac[T.TOA_BINS // 2] = 1e-4
    _stored_night(str(store), "a_pulsar", frac, 2.0, cal=(200.0, 7.0))
    _stored_night(str(store), "b_pulsar", frac, 2.0, cal=(400.0, 7.0))
    _stored_night(str(store), "c_pulsar", frac, 2.0)
    acc = T.accumulated_profile(fold_missing=False, calibrated=True)
    assert acc["t_sys_mean_k"] == pytest.approx(300.0)
    assert acc["kelvin"].max() == pytest.approx(300.0 * 1e-4)
    k_jy = 7.0 / (2 * PF.BOLTZMANN) * 1e-26
    assert acc["jansky"].max() == pytest.approx(300.0 * 1e-4 / k_jy)
    prof, hours, used = T.accumulated_profile(fold_missing=False)      # the plain form is unchanged
    assert len(used) == 3 and prof.max() == pytest.approx(1e-4)


def test_a_recording_is_scaled_by_its_own_calibration_not_todays(monkeypatch):
    monkeypatch.setattr(PF, "calibration_in_force",
                        lambda: {"cal_t_sys_k": 187.0, "cal_effective_area_m2": 7.0})
    t, k, where = PF.radiometer_scale({"cal_t_sys_k": 349.0, "cal_effective_area_m2": 6.2})
    assert (t, where) == (349.0, "recorded") and k == pytest.approx(6.2 / (2 * PF.BOLTZMANN) * 1e-26)
    t, k, where = PF.radiometer_scale({})
    assert (t, where) == (187.0, "current")
    monkeypatch.setattr(PF, "calibration_in_force", lambda: {})
    assert PF.radiometer_scale({}) == (None, None, "")


def test_the_mean_flux_is_the_pulse_area_over_the_period():
    """A boxcar 10 ms wide of fractional height h in a 0.714 s period: the
    period-averaged fraction is h * 10 ms / P, times T_sys over K/Jy."""
    nb, P, h = 1024, 0.714, 1e-3
    ph = (np.arange(nb) + 0.5) / nb
    p = np.where(np.abs(ph - 0.5) < 0.005 / P, h, 0.0)
    s, e = PF.mean_flux_mjy(p, P, 200.0, 2.5e-3)
    area = p.sum() / nb
    assert s == pytest.approx(area * 200.0 / 2.5e-3 * 1e3)
    assert s == pytest.approx(h * 0.010 / P * 200.0 / 2.5e-3 * 1e3, rel=0.05)
    assert e == 0.0                                            # a noiseless off-pulse region
