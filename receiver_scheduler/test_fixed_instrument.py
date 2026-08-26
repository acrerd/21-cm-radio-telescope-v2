"""The fixed instrument (issue #27): one tuning, two products per recording.

tuning.fixed_instrument decides the numbers; the headless receiver writes the
H I product under the names every recording has always had and the
continuum product beside it; readers pick a product and fall back honestly
on files from before; the continuum never includes the hydrogen."""

import json
import os
import sys

import numpy as np
import pytest

import tuning

h5py = pytest.importorskip("h5py")


class TestFixedInstrument:
    def test_the_defaults_are_the_decided_numbers(self):
        inst = tuning.fixed_instrument()
        assert inst["lo_hz"] == pytest.approx(1418.905752e6)
        assert inst["sample_rate_hz"] == 8.0e6
        assert inst["gain_db"] == 40.0
        assert inst["h1_band_hz"] == pytest.approx([1419.005752e6, 1422.305752e6])
        assert inst["continuum_band_hz"] == pytest.approx([1415.705752e6, 1418.805752e6])

    def test_the_lo_is_outside_the_h1_band_and_the_bands_do_not_overlap(self):
        inst = tuning.fixed_instrument()
        lo = inst["lo_hz"]
        assert not (inst["h1_band_hz"][0] <= lo <= inst["h1_band_hz"][1])
        assert inst["continuum_band_hz"][1] < lo < inst["h1_band_hz"][0]
        # and both inside the sampled band with the 80 % window to spare
        half = 0.4 * inst["sample_rate_hz"]
        assert inst["continuum_band_hz"][0] >= lo - half
        assert inst["h1_band_hz"][1] <= lo + 0.45 * inst["sample_rate_hz"]

    def test_config_overrides_take_effect_with_either_key_style(self):
        inst = tuning.fixed_instrument({"receiver_gain_db": 35, "sample_rate_hz": "10e6",
                                        "receiver_lo_hz": None, "wide_channels": ""})
        assert inst["gain_db"] == 35.0
        assert inst["sample_rate_hz"] == 10.0e6
        assert inst["lo_hz"] == pytest.approx(tuning.FIXED_LO_HZ)   # None = default
        assert inst["wide_channels"] == tuning.WIDE_CHANNELS          # "" = default

    def test_the_subband_plan_fits_the_decimated_rate(self):
        plan = tuning.h1_subband_plan(tuning.fixed_instrument())
        assert plan["out_rate_hz"] == 4.0e6
        assert plan["offset_from_lo_hz"] == pytest.approx(1.75e6)
        assert plan["channel_width_hz"] == pytest.approx(4.0e6 / 2048)
        # the anti-alias filter is flat across the whole sub-band
        assert plan["cutoff_hz"] >= 0.5 * (plan["band_hz"][1] - plan["band_hz"][0])
        with pytest.raises(ValueError):
            tuning.h1_subband_plan(tuning.fixed_instrument({"h1_decimation": 4}))

    def test_describe_says_the_essentials(self):
        text = tuning.describe_instrument(tuning.fixed_instrument())
        assert "1418.905752" in text and "8.0 Msps" in text and "40 dB" in text


def _two_product_file(path, n_records=4, calibrated=False, monkeypatch=None):
    """A file as the headless recorder writes it, in demo mode."""
    import b210_h1_receiver as rx
    inst = tuning.fixed_instrument()
    lo, hi = inst["h1_band_hz"]
    f_h1 = np.linspace(lo, hi, 300)
    f_wide = inst["lo_hz"] + np.linspace(-4e6, 4e6, 256, endpoint=False)
    hf = rx.init_hdf5(path, f_h1, len(f_h1), "demo", inst["lo_hz"],
                      inst["sample_rate_hz"], inst["gain_db"],
                      wide={"freq_axis_hz": f_wide, "channels": len(f_wide)},
                      instrument=inst)
    try:
        for i in range(n_records):
            h1 = np.full(len(f_h1), 0.003)
            h1[np.abs(f_h1 - 1420.405752e6) < 50e3] = 0.005          # a line
            wide = np.full(len(f_wide), 0.002 + 1e-5 * i)
            wide[np.abs(f_wide - 1420.405752e6) < 50e3] = 0.004      # the line, coarsely
            wide[np.argmin(np.abs(f_wide - inst["lo_hz"]))] = 0.0005  # the DC notch
            rx.append_spectrum(hf, h1, 1.7e9 + 3.0 * i, 3.0, len(f_h1),
                               wide_linear=wide, overflows=i)
    finally:
        hf.close()
    return f_h1, f_wide


class TestTwoProductFile:
    def test_the_layout_and_the_legacy_names(self, tmp_path):
        path = str(tmp_path / "two.h5")
        _two_product_file(path)
        with h5py.File(path, "r") as hf:
            assert {"frequency_hz", "spectra_linear", "frequency_hz_wide",
                    "spectra_wide_linear", "overflows", "bandpass_correction_wide"} <= set(hf)
            assert hf["spectra_linear"].shape == (4, 300)
            assert hf["spectra_wide_linear"].shape == (4, 256)
            assert list(hf["overflows"][:]) == [0, 1, 2, 3]
            inst = json.loads(hf.attrs["instrument"])
            assert inst["sample_rate_hz"] == 8.0e6
            assert list(hf.attrs["h1_band_hz"]) == pytest.approx(inst["h1_band_hz"])
            assert hf.attrs["product"] == "h1"

    def test_readers_pick_a_product_and_legacy_files_fall_back(self, tmp_path):
        from observation_plot import read_observation, has_wide_product
        path = str(tmp_path / "two.h5")
        f_h1, f_wide = _two_product_file(path)
        f, s, stamps, taus, header = read_observation(path)
        assert f == pytest.approx(f_h1) and header["product_used"] == "h1"
        fw, sw, _, _, hw = read_observation(path, product="wide")
        assert fw == pytest.approx(f_wide) and hw["product_used"] == "wide"
        assert hw["overflows_total"] == 6
        assert has_wide_product(path)

        # A recording from before: one product, which is its own continuum.
        import b210_h1_receiver as rx
        old = str(tmp_path / "old.h5")
        freq = np.linspace(1419.0e6, 1423.5e6, 64)
        hf = rx.init_hdf5(old, freq, 64, "demo", 1421.2e6, 4.5e6, 40.0)
        rx.append_spectrum(hf, np.full(64, 0.003), 1.7e9, 1.0, 64)
        hf.close()
        assert not has_wide_product(old)
        fo, so, _, _, ho = read_observation(old, product="wide")
        assert fo == pytest.approx(freq) and ho["product_used"] == "legacy"

    def test_the_live_sidecar_carries_the_continuum(self, tmp_path):
        path = str(tmp_path / "two.h5")
        _two_product_file(path)
        lines = [json.loads(l) for l in open(str(tmp_path / "two.live.jsonl"))]
        assert len(lines) == 4
        assert "continuum" in lines[0] and "overflows" in lines[0]
        # the continuum is the wide product over the continuum band: the
        # line and the DC notch are outside it
        assert lines[0]["continuum"] == pytest.approx(0.002, rel=1e-6)
        assert lines[3]["overflows"] == 3


class TestContinuumExcludesHydrogen:
    def test_a_fixed_instrument_file_is_measured_over_its_continuum_band(self, tmp_path):
        import drift_fit
        from observation_plot import read_observation
        path = str(tmp_path / "two.h5")
        _two_product_file(path)
        fw, sw, _, _, hw = read_observation(path, product="wide")
        lo, hi, keep = drift_fit._band_window(hw, fw)
        inst = tuning.fixed_instrument()
        assert (lo, hi) == pytest.approx(tuple(inst["continuum_band_hz"]))
        assert not keep[np.abs(fw - 1420.405752e6) < 1.5e6].any()
        assert not keep[np.argmin(np.abs(fw - inst["lo_hz"]))]
        power = drift_fit.band_power(fw, sw, hw)
        assert power == pytest.approx([0.002 + 1e-5 * i for i in range(4)], rel=1e-6)

    def test_a_legacy_file_has_the_line_cut_out(self):
        import drift_fit
        header = {"center_freq_hz": 1421.2e6, "sample_rate_hz": 4.5e6,
                  "dc_artefact_freq_hz": 1421.2e6, "product_used": "legacy"}
        f = np.linspace(1419.0e6, 1423.4e6, 2000)
        lo, hi, keep = drift_fit._band_window(header, f)
        assert keep.any()
        assert not keep[np.abs(f - 1420.405752e6) <= 1.5e6].any()
        assert not keep[np.abs(f - 1421.2e6) <= 30e3].any()


class TestWideTemplate:
    def test_the_wide_template_is_fitted_on_its_own_product_and_normalised_on_the_h1_band(self, tmp_path, monkeypatch):
        import bandpass
        path = str(tmp_path / "two.h5")
        f_h1, f_wide = _two_product_file(path)
        # Replace the flat wide spectrum with a tilted one and refit.
        with h5py.File(path, "a") as hf:
            tilt = 1.0 + 0.1 * (f_wide - f_wide.mean()) / 4e6
            hf["spectra_wide_linear"][:] = (0.002 * tilt)[None, :].repeat(4, axis=0)
        out_h1 = str(tmp_path / "bp.json")
        out_wide = str(tmp_path / "bp_wide.json")
        monkeypatch.setattr(bandpass, "TEMPLATE_FILES", {"h1": out_h1, "wide": out_wide})
        fitted = bandpass.fit_both_from_observation(path, "test", degree=3)
        assert set(fitted) == {"h1", "wide"}
        wide_t = fitted["wide"][0]
        assert wide_t["product"] == "wide"
        assert wide_t["u_centre_hz"] == pytest.approx(f_wide.mean(), abs=2e4)
        assert wide_t["normalise_band_hz"] == pytest.approx(tuning.fixed_instrument()["h1_band_hz"])
        # Unit median over the H I band means the template reads ~1 there and
        # follows the tilt elsewhere.
        model = bandpass.evaluate(wide_t, f_wide)
        inb = (f_wide >= wide_t["normalise_band_hz"][0]) & (f_wide <= wide_t["normalise_band_hz"][1])
        assert np.nanmedian(model[inb]) == pytest.approx(1.0, abs=0.01)
        assert np.nanmedian(model[f_wide < 1417e6]) < 0.98
        assert os.path.exists(out_wide)
        # The stored H I template evaluates on the fine axis, the wide one on
        # the wide axis, each within its own span.
        assert np.isfinite(bandpass.evaluate(fitted["h1"][0], f_h1)).all()
