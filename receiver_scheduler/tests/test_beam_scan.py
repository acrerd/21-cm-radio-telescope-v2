"""The beam as a calibration product: measured from a Sun drift, kept in a
file, read by everything through instrument.measured_beam()."""
import json
import math
import os

import numpy as np
import pytest

import observatory  # noqa: F401  (puts astro_simulator/ on the path)
import beam_scan
import instrument


def _drift(fwhm=4.5, half_span=10.0, step=0.04, t_sys=190.0, t_sun=1600.0, offset=0.1, noise=0.0, seed=1):
    """A Gaussian beam crossed by the Sun at impact parameter `offset`."""
    s = np.arange(-half_span, half_span + step, step)
    theta = np.sqrt(s ** 2 + offset ** 2)
    beam = np.exp(-4 * np.log(2) * (theta / fwhm) ** 2)
    rng = np.random.default_rng(seed)
    power = 1e-5 * (t_sys + t_sun * beam) * (1 + noise * rng.standard_normal(len(s)))
    signed = np.sign(s) * theta
    return signed, power


def test_a_gaussian_beam_integrates_to_its_own_solid_angle():
    signed, power = _drift(fwhm=4.5)
    r = beam_scan.analyse_profile(signed, power, t_sys_k=190.0)
    assert r["ok"], r["why"]
    assert r["solid_angle_sq_deg"] == pytest.approx(1.133 * 4.5 ** 2, rel=0.02)
    assert r["fwhm_deg"] == pytest.approx(4.5, rel=0.01)
    assert r["gaussian_fwhm_deg"] == pytest.approx(4.5, rel=0.01)
    assert r["side_disagreement"] < 0.02
    assert r["peak_over_baseline"] == pytest.approx(1 + 1600 / 190, rel=0.02)
    assert r["t_a_peak_k"] == pytest.approx(1600, rel=0.02)


def test_noise_does_not_move_the_solid_angle_much():
    signed, power = _drift(fwhm=4.5, noise=0.003)
    r = beam_scan.analyse_profile(signed, power)
    assert r["ok"], r["why"]
    assert r["solid_angle_sq_deg"] == pytest.approx(1.133 * 4.5 ** 2, rel=0.05)


def test_a_short_scan_or_a_miss_is_not_adopted():
    signed, power = _drift(half_span=4.0)
    r = beam_scan.analyse_profile(signed, power)
    assert not r["ok"] and any("short" in w or "baseline" in w for w in r["why"])
    signed, power = _drift(offset=1.5)
    r = beam_scan.analyse_profile(signed, power)
    assert not r["ok"] and any("from the beam centre" in w for w in r["why"])


def test_the_sidelobe_is_reported_when_the_scan_reaches_it():
    signed, power = _drift(half_span=12.0)
    power = power + 1e-5 * 1600 * 0.02 * np.exp(-4 * np.log(2) * ((np.abs(signed) - 7.0) / 1.5) ** 2)
    r = beam_scan.analyse_profile(signed, power)
    assert r["sidelobe_measured"] is True
    assert 0.01 < r["sidelobe_peak_fraction"] < 0.04


def test_saved_beam_is_what_instrument_reads(tmp_path, monkeypatch):
    signed, power = _drift(fwhm=4.3)
    r = beam_scan.analyse_profile(signed, power)
    r.update(measured_utc="2026-09-24T12:30:00Z", source_file="x.h5", version=1,
             profile={"theta_deg": list(map(float, signed)), "power": list(map(float, power))})
    monkeypatch.setattr(beam_scan, "BEAM_ARCHIVE_DIR", str(tmp_path / "arch"))
    path = str(tmp_path / "beam.json")
    beam_scan.save_beam_calibration(r, path=path)
    saved = json.load(open(path))
    assert "profile" not in saved and saved["fwhm_deg"] == pytest.approx(4.3, rel=0.01)
    assert len(beam_scan.list_beam_calibrations()) == 1
    monkeypatch.setattr(instrument, "_BEAM_CALIBRATION", path)
    b = instrument.measured_beam()
    assert b["fwhm_deg"] == pytest.approx(4.3, rel=0.01)
    assert instrument.beam_fwhm_deg() == pytest.approx(4.3, rel=0.01)
    assert instrument.beam_solid_angle_sr() == pytest.approx(saved["solid_angle_sq_deg"] * math.radians(1) ** 2)
    # And the collecting area follows the measured solid angle.
    lam = instrument.C_M_S / instrument.H1_REST_FREQ_HZ
    assert instrument.effective_area_m2() == pytest.approx(lam ** 2 / instrument.beam_solid_angle_sr())


def test_the_routes_report_and_refuse_sensibly(tmp_path, monkeypatch):
    import h1_web_scheduler as sched
    sched.app.config["TESTING"] = True
    monkeypatch.setattr(beam_scan, "BEAM_FILE", str(tmp_path / "none.json"))
    with sched.app.test_client() as c:
        d = c.get("/api/beam/status").get_json()
        assert d["success"] and d["in_force"] is None and d["drift_minutes"] == beam_scan.DRIFT_MINUTES
        assert c.post("/api/beam/analyse", json={"file": "nope.h5"}).status_code == 404
        assert c.post("/api/beam/analyse", json={}).status_code == 400
        # A beam scan entry is a Sun drift that reduces itself when it ends.
        e = sched.beam_scan_entry(__import__("datetime").datetime(2026, 9, 25, 12, 9))
        assert e["beam_scan"] and e["coord_system"] == "drift" and e["object_name"] == "sun"
        assert e["duration_minutes"] == beam_scan.DRIFT_MINUTES and e["drift_time"] == "13:09"


def test_no_file_or_a_bad_file_falls_back_to_the_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(instrument, "_BEAM_CALIBRATION", str(tmp_path / "none.json"))
    assert instrument.measured_beam() is None
    assert instrument.beam_fwhm_deg() == instrument.BEAM_FWHM_REF_DEG
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"fwhm_deg": 0.5, "solid_angle_sq_deg": 0.3}))
    monkeypatch.setattr(instrument, "_BEAM_CALIBRATION", str(bad))
    assert instrument.measured_beam() is None
