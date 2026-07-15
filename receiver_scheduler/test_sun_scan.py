#!/usr/bin/env python3
"""Tests for sun_scan.py scan geometry."""

import math
import os
import sys
import threading
from unittest.mock import patch

import pytest
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import sun_scan
except ImportError:
    pytest.skip("Could not import sun_scan", allow_module_level=True)


def test_sun_offset_to_command_clamps_azimuth():
    cmd_alt, cmd_az, cos_alt, clamped = sun_scan._sun_offset_to_command(
        30.0, 352.0, 2.0, 10.0)

    assert cmd_alt == pytest.approx(32.0)
    assert cos_alt == pytest.approx(math.cos(math.radians(30.0)))
    assert cmd_az == 353.0
    assert clamped is True


def test_sun_scan_recomputes_sun_and_returns_mid_scan_position():
    # First no-time call is the initial horizon check.  The following calls are
    # per-point Sun positions; the command should follow those moving positions.
    no_time_positions = [(30.0, 100.0)] + [(30.0, 110.0 + i) for i in range(9)]
    progress = []

    def fake_get_sun_altaz(lat, lon, elevation=0, when=None):
        if when is not None:
            return 60.0, 200.0
        return no_time_positions.pop(0)

    def fake_fit(alt_offsets, az_offsets, power, beam_fwhm_hint=3.0):
        return {
            "alt_error_deg": 1.0,
            "az_error_deg": 2.0,
            "az_error_sky_deg": 2.0,
            "amplitude": 10.0,
            "sigma_deg": 1.0,
            "offset": 1.0,
            "beam_fwhm_deg": 2.355,
            "fit_errors": {"alt_err": 0.25, "az_err": 0.5, "sigma_err": 0.1},
            "success": True,
        }

    with patch.object(sun_scan, "get_sun_altaz", side_effect=fake_get_sun_altaz), \
         patch.object(sun_scan, "measure_power", return_value=1.0), \
         patch.object(sun_scan, "fit_pointing_error", side_effect=fake_fit):
        result = sun_scan.sun_scan(
            n=3,
            grid_spacing_deg=1.0,
            integration_time_s=0.0,
            sdr_type="demo",
            output_image=None,
            progress_callback=lambda idx, total, info: progress.append(info),
        )

    assert len(progress) == 9
    assert no_time_positions == []

    first_cmd = progress[0]
    # n=3 scans row 0 from col 2 to 0, so the first sky az offset is +1 degree.
    assert first_cmd["sun_az"] == 110.0
    assert first_cmd["cmd_az"] == pytest.approx(110.0 + 1.0 / math.cos(math.radians(30.0)))

    # Mid-scan Sun position is used for the saved comparison point.
    assert result["sun_alt_deg"] == 60.0
    assert result["sun_az_deg"] == 200.0

    # The fit is in cross-elevation sky degrees; the public az_error_deg is a
    # mount azimuth correction using the mid-scan altitude.
    assert result["az_error_sky_deg"] == 2.0
    assert result["az_error_deg"] == pytest.approx(4.0)
    assert result["fit"]["fit_errors"]["az_err_sky"] == 0.5
    assert result["fit"]["fit_errors"]["az_err"] == pytest.approx(1.0)


def test_cancelled_sun_scan_does_not_fit_partial_data():
    cancel = threading.Event()
    cancel.set()

    with patch.object(sun_scan, "get_sun_altaz", return_value=(30.0, 100.0)), \
         patch.object(sun_scan, "fit_pointing_error") as fit:
        with pytest.raises(RuntimeError, match="cancelled"):
            sun_scan.sun_scan(
                n=3,
                integration_time_s=0.0,
                sdr_type="demo",
                output_image=None,
                cancel_event=cancel,
            )

    fit.assert_not_called()


def test_slew_verifies_final_position():
    responses = [
        {"ok": True},
        {"alt": 32.0, "az": 101.0, "is_slewing": False,
         "fault_active": False, "status": "Ready"},
    ]

    with patch.object(sun_scan, "_srt_api", side_effect=responses):
        assert sun_scan._slew_to(
            "http://controller", 32.1, 101.2,
            slew_timeout=1, position_tolerance=0.5,
        ) is True


def test_slew_fails_when_telescope_does_not_move():
    responses = [
        {"ok": True},
        {"alt": 0.0, "az": 0.0, "is_slewing": False,
         "fault_active": False, "status": "Ready"},
    ]

    with patch.object(sun_scan, "_srt_api", side_effect=responses):
        with pytest.raises(RuntimeError, match="stopped before reaching"):
            sun_scan._slew_to(
                "http://controller", 32.0, 101.0,
                slew_timeout=1, position_tolerance=0.5, start_grace_s=0,
            )


def test_hardware_scan_stops_on_first_failed_slew():
    progress = []

    with patch.object(sun_scan, "get_sun_altaz", return_value=(30.0, 100.0)), \
         patch.object(sun_scan, "_slew_to",
                      side_effect=RuntimeError("controller rejected movement")) as slew, \
         patch.object(sun_scan, "measure_power") as measure:
        with pytest.raises(RuntimeError, match="controller rejected movement"):
            sun_scan.sun_scan(
                n=3,
                integration_time_s=0.1,
                sdr_type="b210",
                srt_url="http://controller",
                output_image=None,
                progress_callback=lambda *args: progress.append(args),
            )

    assert slew.call_count == 1
    measure.assert_not_called()
    assert progress == []


def test_fit_pointing_error_recovers_clean_gaussian():
    if sun_scan.curve_fit is None:
        pytest.skip("scipy is not installed")

    offsets = np.linspace(-3.0, 3.0, 5)
    az_grid, alt_grid = np.meshgrid(offsets, offsets)
    power = sun_scan._gaussian_2d(
        (alt_grid.ravel(), az_grid.ravel()),
        10.0, 0.45, -0.35, 1.25, 1.0)

    fit = sun_scan.fit_pointing_error(
        alt_grid.ravel(), az_grid.ravel(), power, beam_fwhm_hint=3.0)

    assert fit["success"] is True
    assert fit["alt_error_deg"] == pytest.approx(0.45, abs=1e-3)
    assert fit["az_error_deg"] == pytest.approx(-0.35, abs=1e-3)
    assert fit["r_squared"] == pytest.approx(1.0)


def test_fit_pointing_error_rejects_flat_power():
    offsets = np.linspace(-1.0, 1.0, 3)
    az_grid, alt_grid = np.meshgrid(offsets, offsets)

    with pytest.raises(ValueError, match="no usable variation"):
        sun_scan.fit_pointing_error(
            alt_grid.ravel(), az_grid.ravel(), np.ones(9))


def test_sun_scan_rejects_grid_clipped_by_mount_limits():
    with patch.object(sun_scan, "get_sun_altaz", return_value=(30.0, 352.0)):
        with pytest.raises(RuntimeError, match="safe mount range"):
            sun_scan.sun_scan(
                n=3,
                grid_spacing_deg=10.0,
                integration_time_s=0.0,
                sdr_type="demo",
                output_image=None,
            )


def _synthetic_pointing_data(azimuths):
    altitudes = np.array([25.0, 35.0, 45.0, 55.0, 50.0, 30.0])[:len(azimuths)]
    azimuths = np.asarray(azimuths, dtype=float)
    expected = np.array([0.35, -0.20, 0.12, -0.08])
    matrix = sun_scan._pointing_model_matrix(altitudes, azimuths)
    errors = matrix @ expected
    n = len(azimuths)
    data = []
    for i in range(n):
        data.append({
            "sun_alt_deg": altitudes[i],
            "sun_az_deg": azimuths[i],
            "alt_error_deg": errors[i],
            "az_error_deg": errors[n + i],
            "alt_error_uncertainty_deg": 0.05,
            "az_error_uncertainty_deg": 0.05,
            "fit_success": True,
        })
    return data, expected


def test_pointing_model_recovers_weighted_four_parameter_solution():
    data, expected = _synthetic_pointing_data([80, 105, 135, 165, 195, 225])

    model = sun_scan.fit_pointing_model(data, true_lat=55.9, true_lon=-4.3)

    assert model["success"] is True
    assert model["az_coverage_deg"] >= 30.0
    actual = [model["alt_offset_deg"], model["az_offset_deg"],
              model["tilt_north_deg"], model["tilt_east_deg"]]
    assert actual == pytest.approx(expected, abs=1e-9)
    assert model["effective_lat"] == pytest.approx(55.9 + expected[2])
    assert model["effective_lon"] == pytest.approx(
        -4.3 + expected[3] / math.cos(math.radians(55.9)))


def test_pointing_model_rejects_narrow_sun_coverage():
    data, _ = _synthetic_pointing_data([100, 105, 110, 115])

    model = sun_scan.fit_pointing_model(data)

    assert model["success"] is False
    assert "coverage" in model["error"]
