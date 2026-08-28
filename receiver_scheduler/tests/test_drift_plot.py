"""The drift-scan plot: total power per record, drawn even when the bandpass
step has NaN'd the channels outside the measured band, with the crossing
recorded in the file marked where the file says it is."""

import numpy as np
import pytest

import observation_plot as op

matplotlib = pytest.importorskip("matplotlib")
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def _axes():
    fig, ax = plt.subplots()
    return fig, ax


def test_channels_dropped_by_the_bandpass_do_not_blank_the_plot():
    # Ten records, 100 channels, with the outer 20 marked NaN as
    # bandpass.apply_bandpass does for channels outside the measured band.
    spectra = np.ones((10, 100)) * np.arange(1, 11)[:, None]
    spectra[:, :10] = np.nan
    spectra[:, -10:] = np.nan
    stamps = 1.7e9 + 3.0 * np.arange(10)
    fig, ax = _axes()
    try:
        op._plot_drift(ax, spectra, stamps, None)
        y = ax.get_lines()[0].get_ydata()
        assert np.all(np.isfinite(y))
        assert y == pytest.approx(np.arange(1, 11))
    finally:
        plt.close(fig)


def test_axis_label_and_marker_follow_the_arguments():
    spectra = np.ones((4, 8))
    stamps = 1.7e9 + 60.0 * np.arange(4)
    fig, ax = _axes()
    try:
        op._plot_drift(ax, spectra, stamps, 1.5, ylabel="Antenna temperature (K)",
                       transit_label="beam crossing (recorded)")
        assert ax.get_ylabel() == "Antenna temperature (K)"
        assert [l.get_label() for l in ax.get_lines()][-1] == "beam crossing (recorded)"
    finally:
        plt.close(fig)


def test_recorded_crossing_is_measured_from_the_first_record():
    stamps = np.array([1.7e9, 1.7e9 + 60.0])
    from datetime import datetime
    crossing = datetime.fromtimestamp(1.7e9 + 150.0)   # local naive, as the scheduler writes it
    header = {"drift_crossing_time": crossing.strftime("%Y-%m-%d %H:%M:%S")}
    assert op.recorded_crossing_minutes(header, stamps) == pytest.approx(2.5)
    assert op.recorded_crossing_minutes({}, stamps) is None
    assert op.recorded_crossing_minutes({"drift_crossing_time": "yesterday"}, stamps) is None
    assert op.recorded_crossing_minutes(header, np.array([])) is None
