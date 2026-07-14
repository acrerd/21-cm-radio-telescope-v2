#!/usr/bin/env python3
"""Tests for sun_scan.py scan geometry."""

import math
import os
import sys
import threading
from unittest.mock import patch

import pytest

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
