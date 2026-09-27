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


def test_mjd_text_keeps_the_nanoseconds():
    t = 1790467200.3005433
    s = T.mjd_string(t)
    from astropy.time import Time
    back = Time(int(s.split(".")[0]), float("0." + s.split(".")[1]), format="mjd", scale="utc").unix
    assert back == pytest.approx(t, abs=2e-7)                     # float64 unix's own resolution
    assert len(s.split(".")[1]) == 13


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
