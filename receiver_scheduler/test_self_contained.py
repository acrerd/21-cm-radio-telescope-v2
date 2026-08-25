"""A recording must carry what it takes to reduce it.

The HDF5 files stay raw - counts, uncorrected - and that is deliberate: a
better bandpass or a better gain should be able to re-reduce an old
observation. But raw is only useful if the calibration travels with it.

Until 2026-08-25 it did not. The pipeline read whichever bandpass_template.json
and gain_calibration.json happened to be on disk when asked, so an archived
observation silently acquired whatever calibration was current rather than the
one it was taken under - and once the receiver was retuned, an old file could
not be turned into kelvin at all. It was reducible on one machine, for as long
as nobody remeasured anything.
"""

import json

import pytest

import bandpass
import rf_calibration as R


def header_with(cal=None, template=None, **over):
    h = {"center_freq_hz": 1421205752.0, "sample_rate_hz": 4500000.0,
         "gain_db": 40.0}
    if cal is not None:
        h["gain_calibration"] = json.dumps(cal)
    if template is not None:
        h["bandpass_template"] = json.dumps(template)
    h.update(over)
    return h


@pytest.fixture
def current():
    cal = R.load_calibration()
    if not cal:
        pytest.skip("no gain calibration on this machine")
    return cal


def test_the_stored_calibration_is_preferred_while_it_applies(current):
    """Re-reducing against a better measurement is why the file is kept raw."""
    cal, why, source = R.calibration_for(header_with(cal=current))
    assert source == "current"
    assert cal["gain_counts_per_k"] == current["gain_counts_per_k"]


def test_the_file_can_be_reduced_when_the_stored_one_is_gone(current):
    """The case that matters: another machine, or a later retune.

    Without the embedded copy this observation is stuck in counts forever, and
    nothing about the file says why - it simply plots unlabelled.
    """
    cal, why, source = R.calibration_for(header_with(cal=current),
                                         path="/nonexistent/gain.json")
    assert source == "recorded with the observation"
    assert cal["gain_counts_per_k"] == current["gain_counts_per_k"]
    assert cal["t_sys_k"] == current["t_sys_k"]


def test_an_embedded_calibration_for_the_wrong_tuning_is_still_refused(current):
    """Carrying a calibration is not the same as it applying.

    The file records what was in force, not what fits - deciding that at write
    time would throw away the evidence for the judgement. So the tuning check
    runs on the embedded copy exactly as it does on the current one.
    """
    wrong = header_with(cal=current, center_freq_hz=1400e6)
    cal, why, source = R.calibration_for(wrong, path="/nonexistent/gain.json")
    assert cal is None and source == ""
    assert "LO" in why


def test_a_file_from_before_this_reduces_exactly_as_it_did(current):
    """Older recordings have no embedded copy and must not break."""
    cal, why, source = R.calibration_for(header_with())
    assert source == "current"
    cal, why, source = R.calibration_for(header_with(), path="/nonexistent/gain.json")
    assert cal is None and source == ""


def test_the_bandpass_falls_back_the_same_way():
    import numpy as np

    template = bandpass.load_bandpass()
    if not template:
        pytest.skip("no bandpass template on this machine")
    freq = np.linspace(1420.0e6, 1420.8e6, 512)
    spectra = np.ones((3, freq.size))
    head = header_with(template=template)

    _, note = bandpass.apply_bandpass(freq, spectra, head)
    assert "bandpass corrected" in note
    _, note = bandpass.apply_bandpass(freq, spectra, head, path="/nonexistent/bp.json")
    assert "bandpass corrected" in note, "the file's own template should have served"
    _, note = bandpass.apply_bandpass(freq, spectra, header_with(),
                                      path="/nonexistent/bp.json")
    assert "not bandpass corrected" in note


def test_the_bandpass_correction_is_stored_per_channel(tmp_path):
    """One array, not a polynomial the reader has to know how to evaluate.

    The correction is a function of frequency alone and constant for a run, so
    storing it once - a few thousand floats, 6% of a short file - makes every
    spectrum exactly reversible without this repository. The alternative
    considered was writing both a raw and a calibrated copy of every spectrum,
    which would have doubled the recording for the same result.
    """
    import numpy as np

    import b210_h1_receiver as rx

    freq = np.linspace(1419.0e6, 1422.0e6, 2048)
    correction, valid = rx._bandpass_correction(freq)

    assert correction.shape == freq.shape == valid.shape
    assert np.all(np.isfinite(correction)), "a NaN here would poison the spectrum"
    assert np.all(correction > 0), "the correction is a divisor"
    assert np.all(correction[~valid] == 1.0), (
        "channels the template cannot speak for must be stored uncorrected, "
        "not dropped - they are what a later bandpass fit has to work from")


def test_a_recording_reverses_exactly(tmp_path):
    """raw = (kelvin + T_sys) * gain * correction, to float32 precision.

    This is the property that makes storing a calibrated spectrum safe at all:
    if the gain or the template is later found wrong, the measurement is still
    in there. Without the stored correction it is only recoverable by anyone
    who has both the polynomial and the code to evaluate it.
    """
    import numpy as np

    import b210_h1_receiver as rx

    cal = R.load_calibration()
    if not cal:
        pytest.skip("no gain calibration on this machine")
    freq = np.linspace(1419.5e6, 1421.5e6, 1024)
    correction, _ = rx._bandpass_correction(freq)
    raw = np.random.default_rng(1).normal(0.003, 3e-5, size=(4, freq.size))

    kelvin = raw / correction / cal["gain_counts_per_k"] - cal["t_sys_k"]
    back = (kelvin + cal["t_sys_k"]) * cal["gain_counts_per_k"] * correction
    assert np.allclose(back, raw, rtol=1e-9, atol=0)


def test_a_calibrated_recording_still_reduces_as_counts(tmp_path):
    """The pipeline must see counts however the file was stored.

    Storing kelvin is for whoever opens the file elsewhere. Inside here the
    reduction has to work in counts, because fitting a gain from spectra a gain
    has already been applied to is circular - reduce_for_fit would dutifully
    return unity and a system temperature of zero, and the calibration would
    look perfect while meaning nothing.
    """
    import numpy as np
    import h5py

    import observation_plot as op

    cal = R.load_calibration()
    if not cal:
        pytest.skip("no gain calibration on this machine")
    path = str(tmp_path / "calibrated.h5")
    freq = np.linspace(1420.0e6, 1420.8e6, 256)
    counts = np.full((3, freq.size), 0.0028)
    correction = np.linspace(0.95, 1.05, freq.size)
    kelvin = counts / correction / cal["gain_counts_per_k"] - cal["t_sys_k"]

    with h5py.File(path, "w") as hf:
        hf.create_dataset("frequency_hz", data=freq)
        hf.create_dataset("spectra_kelvin", data=kelvin.astype("float32"))
        hf.create_dataset("bandpass_correction", data=correction.astype("float32"))
        hf.create_dataset("timestamps", data=np.arange(3, dtype=float))
        hf.create_dataset("integration_times", data=np.full(3, 3.0))
        hf.attrs["spectra_units"] = "K"
        hf.attrs["applied_gain_counts_per_k"] = cal["gain_counts_per_k"]
        hf.attrs["applied_t_sys_k"] = cal["t_sys_k"]

    _, spectra, _, _, header = op.read_observation(path)
    assert header["spectra_units"] == "K", "the file says what it holds"
    assert np.allclose(spectra, counts, rtol=1e-5), (
        "read_observation must hand the pipeline counts, whatever is stored")


def test_an_uncalibrated_recording_is_left_in_counts(tmp_path):
    """No calibration for the tuning means no pretending there is one.

    Both the template and the gain have to apply. The gain was fitted against
    corrected spectra, so using it on uncorrected ones would mix two scales -
    a number that looks like a temperature and is not.
    """
    import b210_h1_receiver as rx
    import h5py

    path = str(tmp_path / "uncal.h5")
    hf = rx.init_hdf5(path, [1.0e9, 1.1e9], 2, "demo", 1.0e9, 2.0e6, 0.0)
    try:
        assert "spectra_linear" in hf
        assert "spectra_kelvin" not in hf
        assert hf.attrs["spectra_units"] == "counts"
        assert hf.attrs["gain_db"] == 0.0, (
            "the receiver gain must survive - it was once overwritten by the "
            "calibration gain through a shadowed parameter, and the file "
            "would not open at all")
    finally:
        hf.close()
