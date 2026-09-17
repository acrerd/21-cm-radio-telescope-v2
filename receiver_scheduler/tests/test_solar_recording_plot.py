"""A solar track's recording is plotted as flux against the clock, the way its
live view was drawn - not as its spectrum (2026-09-14).

The live view and the recording plot are two renderings of one measurement,
and until this they disagreed: the live trace showed solar flux in SFU
against UTC, corrected to above the atmosphere, and the recording of the same
run came back as a flat continuum spectrum. `plot_mode_for` now applies the
live view's own rule to the file, and `plot_observation(mode="solar")`
reproduces the live conversion from `solar_flux_series`.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import h1_web_scheduler as sched          # noqa: E402
import observation_plot                    # noqa: E402


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("obs, observation_mode, expected", [
    ({"coord_system": "object", "object_name": "sun"}, "track", "solar"),
    ({"coord_system": "object", "object_name": "Sun"}, "track", "solar"),
    # A solar drift is watched as flux too, so its file is drawn the same way.
    ({"coord_system": "object", "object_name": "sun"}, "drift", "solar"),
    ({"coord_system": "object", "object_name": "cas a"}, "track", "spectrum"),
    ({"coord_system": "galactic"}, "drift", "drift"),
    ({"coord_system": "altaz"}, "drift", "drift"),
    ({"coord_system": "galactic"}, "track", "spectrum"),
])
def test_plot_mode_follows_the_live_view(obs, observation_mode, expected):
    assert sched.plot_mode_for(obs, observation_mode) == expected


def test_a_recorded_solar_track_is_read_as_solar(tmp_path, monkeypatch):
    """`_observation_info`, which the plot endpoint and the catalogue use,
    reads the file's own attributes and lands on 'solar'."""
    path = _solar_recording(tmp_path, monkeypatch, n_records=4)
    monkeypatch.setattr(sched, "observations_folder", lambda: os.path.realpath(str(tmp_path)))
    info = sched._observation_info(os.path.basename(path))
    assert info["mode"] == "solar"
    assert info["transit_minutes"] is None


# ---------------------------------------------------------------------------
# The conversion, against the live view's
# ---------------------------------------------------------------------------

def test_the_series_is_the_live_conversion():
    """One kelvin of antenna temperature becomes the live view's SFU, then is
    divided by the atmospheric transmission at the Sun's elevation - the
    number is a few per cent above the raw conversion, never below it."""
    from observatory import antenna_temperature_to_flux
    stamps = np.array([1789405000.0 + 10 * i for i in range(6)])     # 2026-09-14 ~13 UTC
    spectra = np.ones((6, 8))                                         # 1 K in every channel
    t, values, opacity, group = observation_plot.solar_flux_series(spectra, stamps, True)
    raw = antenna_temperature_to_flux(1.0)
    assert group == 1 and t.shape == values.shape == (6,)
    assert opacity is True
    assert np.all(values > raw) and np.all(values < raw * 1.06), (values, raw)


def test_uncalibrated_series_stays_in_counts():
    stamps = np.arange(5, dtype=float)
    spectra = np.full((5, 4), 0.002)
    t, values, opacity, group = observation_plot.solar_flux_series(spectra, stamps, False)
    assert opacity is False
    assert np.allclose(values, 0.002)


def test_a_long_run_is_binned_whole():
    """Thousands of records are binned down rather than truncated: every
    point covers the same number of records and the run's end is on the
    plot."""
    n = 4 * observation_plot._SOLAR_MAX_POINTS + 7
    stamps = 1789405000.0 + np.arange(n, dtype=float)
    spectra = np.linspace(0.0, 1.0, n)[:, None] * np.ones((n, 3))
    t, values, _opacity, group = observation_plot.solar_flux_series(spectra, stamps, False)
    assert group == 5
    assert values.size == n // 5
    assert t[-1] > stamps[-1] - 10          # the last point is at the end of the run
    assert values[-1] > 0.99               # and carries the end's value


# ---------------------------------------------------------------------------
# The picture
# ---------------------------------------------------------------------------

def _solar_recording(tmp_path, monkeypatch, n_records=30):
    """A fixed-instrument two-product file whose attributes say 'the Sun'."""
    import tuning
    import b210_h1_receiver as rx
    import bandpass
    import rf_calibration
    monkeypatch.setattr(bandpass, "load_bandpass", lambda *a, **k: None)
    monkeypatch.setattr(rf_calibration, "load_calibration", lambda *a, **k: None)
    inst = tuning.fixed_instrument()
    lo, hi = inst["h1_band_hz"]
    f_h1 = np.linspace(lo, hi, 128)
    f_wide = inst["lo_hz"] + np.linspace(-4e6, 4e6, 256, endpoint=False)
    path = str(tmp_path / "20260913_171703_track.h5")
    hf = rx.init_hdf5(path, f_h1, len(f_h1), "demo", inst["lo_hz"],
                      inst["sample_rate_hz"], inst["gain_db"],
                      wide={"freq_axis_hz": f_wide, "channels": len(f_wide)},
                      instrument=inst)
    try:
        hf.attrs["coord_system"] = "object"
        hf.attrs["object_name"] = "sun"
        hf.attrs["observation_mode"] = "track"
        hf.attrs["obs_name"] = "Solar track"
        for i in range(n_records):
            h1 = np.full(len(f_h1), 0.003)
            wide = np.full(len(f_wide), 0.004 + 0.0002 * np.sin(i / 3.0))
            rx.append_spectrum(hf, h1, 1789405000.0 + 5.0 * i, 5.0, len(f_h1),
                               wide_linear=wide)
    finally:
        hf.close()
    return path


@pytest.mark.skipif(not observation_plot.MATPLOTLIB_AVAILABLE, reason="no matplotlib")
def test_the_solar_recording_renders_as_flux_against_the_clock(tmp_path, monkeypatch):
    path = _solar_recording(tmp_path, monkeypatch)
    out = str(tmp_path / "solar.png")
    captured = {}
    real = observation_plot._plot_solar

    def spy(ax, spectra, stamps, calibrated, **kw):
        captured["n"] = spectra.shape[0]
        captured["header"] = kw.get("header")
        result = real(ax, spectra, stamps, calibrated, **kw)
        captured["xlabel"] = ax.get_xlabel()
        captured["ylabel"] = ax.get_ylabel()
        return result
    monkeypatch.setattr(observation_plot, "_plot_solar", spy)
    # No calibration for this file - not in force and not carried in it -
    # so the axis is honest counts, not invented SFU.
    import rf_calibration
    monkeypatch.setattr(rf_calibration, "calibration_for",
                        lambda header: (None, "no gain calibration for this tuning", None))
    observation_plot.plot_observation(path, out, name="Solar track", mode="solar")
    assert os.path.getsize(out) > 10000
    assert captured["n"] == 30
    assert captured["xlabel"].startswith("UTC on 2026-09-14")
    assert "counts" in captured["ylabel"]

    # With a calibration the same file is drawn in SFU above the atmosphere.
    monkeypatch.setattr(rf_calibration, "calibration_for",
                        lambda header: ({"gain_counts_per_k": 1.0e-5, "t_sys_k": 350.0,
                                         "observed_utc": "2026-09-13T16:10:00"}, "", "current"))
    observation_plot.plot_observation(path, out, name="Solar track", mode="solar")
    assert captured["ylabel"] == "Solar flux (SFU, above the atmosphere)"
