#!/usr/bin/env python3
"""Tests for sun_scan.py scan geometry."""

import json
import math
import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

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
        ) == (32.0, 101.0)


def test_slew_arrival_is_judged_in_the_drive_frame():
    """The sky target and the drive reading differ by the pointing model.

    /direct takes a sky position and reports the drive coordinates it
    commanded; /status reports drive coordinates. Comparing the reading against
    the sky target instead would be out by the whole calibration - here 2 deg
    of azimuth, four times the tolerance - and the slew would never be seen to
    finish.
    """
    responses = [
        {"ok": True, "drive_alt": 32.3, "drive_az": 99.2},
        {"alt": 32.3, "az": 99.2, "is_slewing": False,
         "fault_active": False, "status": "Ready"},
    ]

    with patch.object(sun_scan, "_srt_api", side_effect=responses):
        assert sun_scan._slew_to(
            "http://controller", 32.1, 101.2,
            slew_timeout=1, position_tolerance=0.5,
        ) == (32.3, 99.2)


def test_slew_falls_back_to_the_sky_target_on_older_firmware():
    """Firmware without a resident model has one frame, so the target is it."""
    responses = [
        {"ok": True},
        {"alt": 32.0, "az": 101.0, "is_slewing": False,
         "fault_active": False, "status": "Ready"},
    ]

    with patch.object(sun_scan, "_srt_api", side_effect=responses):
        assert sun_scan._slew_to(
            "http://controller", 32.1, 101.2,
            slew_timeout=1, position_tolerance=0.5,
        ) == (32.0, 101.0)


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


def test_slew_returns_promptly_when_cancelled_mid_slew():
    """Cancel must bite during a slew, not after slew_timeout expires.

    The telescope never arrives, so before cancellation was threaded into
    _slew_to this call polled for the whole slew_timeout - 300 s under the
    scheduler, and a scan chains several such slews.
    """
    cancel = threading.Event()
    poll_count = {"n": 0}

    def fake_api(base_url, endpoint, params=None):
        if endpoint == "/direct":
            return {"ok": True}
        poll_count["n"] += 1
        if poll_count["n"] == 3:
            cancel.set()          # cancel arrives mid-slew
        # Still slewing, never reaches the target
        return {"alt": 0.0, "az": 0.0, "is_slewing": True,
                "fault_active": False, "status": "Slewing"}

    with patch.object(sun_scan, "_srt_api", side_effect=fake_api):
        started = time.monotonic()
        with pytest.raises(sun_scan.ScanCancelled):
            sun_scan._slew_to(
                "http://controller", 32.0, 101.0,
                slew_timeout=300, position_tolerance=0.5,
                cancel_event=cancel,
            )
        elapsed = time.monotonic() - started

    # Bites on the poll after the event is set, not at slew_timeout.
    assert poll_count["n"] <= 4
    assert elapsed < 10


def test_scan_cancelled_is_a_runtime_error():
    """Callers catching RuntimeError - the scheduler among them - keep working."""
    assert issubclass(sun_scan.ScanCancelled, RuntimeError)


def test_cancellable_sleep_returns_immediately_when_set():
    cancel = threading.Event()
    cancel.set()
    started = time.monotonic()
    assert sun_scan._cancellable_sleep(30, cancel) is True
    assert time.monotonic() - started < 1


def _echo_slew(base_url, alt, az, *args, **kwargs):
    """Stand-in for _slew_to: a perfect mount that lands exactly on target.

    _slew_to returns the DRIVE position reached, which the scan records as the
    abscissa for its Gaussian, so a mock must return a position rather than a
    bare success flag.
    """
    return (alt, az)


def test_hardware_scan_stops_on_first_failed_slew():
    progress = []
    meter = MagicMock()

    with patch.object(sun_scan, "get_sun_altaz", return_value=(30.0, 100.0)), \
         patch.object(sun_scan, "_slew_to",
                      side_effect=RuntimeError("controller rejected movement")) as slew, \
         patch.object(sun_scan, "_B210PowerMeter", return_value=meter), \
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
    # The B210 session must be released even when the scan aborts early,
    # otherwise the claimed USRP blocks every subsequent scan/observation.
    meter.close.assert_called_once()


def test_hardware_scan_reuses_one_b210_session_for_all_points():
    meter = MagicMock()
    meter.measure.side_effect = [float(i) for i in range(1, 10)]
    fit_result = {
        "alt_error_deg": 0.0,
        "az_error_deg": 0.0,
        "az_error_sky_deg": 0.0,
        "amplitude": 8.0,
        "sigma_deg": 1.0,
        "offset": 1.0,
        "beam_fwhm_deg": 2.355,
        "fit_errors": {"alt_err": 0.1, "az_err": 0.1, "sigma_err": 0.1},
        "success": True,
    }

    with patch.object(sun_scan, "get_sun_altaz", return_value=(30.0, 100.0)), \
         patch.object(sun_scan, "_slew_to", side_effect=_echo_slew), \
         patch.object(sun_scan, "_B210PowerMeter", return_value=meter) as factory, \
         patch.object(sun_scan, "fit_pointing_error", return_value=fit_result), \
         patch.object(sun_scan.time, "sleep"):
        result = sun_scan.sun_scan(
            n=3,
            integration_time_s=0.1,
            sdr_type="b210",
            srt_url="http://controller",
            output_image=None,
        )

    factory.assert_called_once_with(1420.405752e6, 2.4e6, 40.0)
    assert meter.measure.call_count == 9
    meter.close.assert_called_once()
    assert result["fit"]["success"] is True


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
    """Scan records that a perfect mount with a known model would produce.

    The recorded altitude error is what a scan actually measures: the mount
    term plus atmospheric refraction, because the beam has to point where the
    Sun appears. The fit must take the refraction back out, so recovering
    ``expected`` exactly is also a check that it does.
    """
    _alts = [25.0, 35.0, 45.0, 55.0, 50.0, 30.0]
    # Cycle when more azimuths are asked for, so a caller needing enough scans
    # to fit a session drift still gets a spread of altitudes.
    altitudes = np.array([_alts[i % len(_alts)] for i in range(len(azimuths))])
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
            "alt_error_deg": errors[i] + sun_scan.refraction_deg(altitudes[i]),
            "az_error_deg": errors[n + i],
            "alt_error_uncertainty_deg": 0.05,
            "az_error_uncertainty_deg": 0.05,
            "fit_success": True,
            "record_version": sun_scan._SCAN_RECORD_VERSION,
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
    # The same four numbers under the names the controller uses, and the TRUE
    # site position recorded alongside rather than adjusted by the tilt.
    assert {k: model["terms"][k] for k in ("IE", "IA", "AN", "AE")} == pytest.approx(
        {"IE": expected[0], "IA": expected[1],
         "AN": expected[2], "AE": expected[3]}, abs=1e-9)
    # These scans span enough azimuth for a scale term, and it must come out at
    # zero on data with no scale error rather than soaking up part of IA.
    assert model["terms"]["AZSCALE"] == pytest.approx(0.0, abs=1e-9)
    assert model["site_lat"] == pytest.approx(55.9)
    assert model["site_lon"] == pytest.approx(-4.3)
    assert "effective_lat" not in model
    assert "effective_lon" not in model


def test_pointing_model_rejects_narrow_sun_coverage():
    data, _ = _synthetic_pointing_data([100, 105, 110, 115])

    model = sun_scan.fit_pointing_model(data)

    assert model["success"] is False
    assert "coverage" in model["error"]


def test_pointing_model_document_carries_the_fitted_terms():
    """The wire format holds the parameters, not anything derived from them."""
    data, expected = _synthetic_pointing_data([80, 105, 135, 165, 195, 225])
    model = sun_scan.fit_pointing_model(data, true_lat=55.9, true_lon=-4.3)

    document = sun_scan.pointing_model_document(model, fitted_utc="2026-08-19T10:00:00Z")

    assert document["version"] == 1
    assert document["fitted_utc"] == "2026-08-19T10:00:00Z"
    assert document["n_scans"] == 6
    assert document["frame"] == "cross_elevation"
    assert document["terms"]["IE"] == pytest.approx(expected[0])
    assert document["terms"]["AE"] == pytest.approx(expected[3])
    assert document["site"] == {"lat": 55.9, "lon": -4.3}
    assert set(document["residual_rms_deg"]) == {"alt", "xel"}
    # No effective position, and no azimuth rotation to cancel one.
    assert "effective_lat" not in document
    assert "az_site_rotation_deg" not in document
    # Must survive the round trip through JSON that the POST performs.
    assert json.loads(json.dumps(document)) == document


def test_pointing_model_document_refuses_a_model_without_terms():
    with pytest.raises(ValueError, match="no fitted terms"):
        sun_scan.pointing_model_document({"n_scans": 6})


def test_pointing_model_document_refuses_a_non_finite_term():
    with pytest.raises(ValueError, match="not finite"):
        sun_scan.pointing_model_document({"terms": {"IE": float("nan")}})


def test_scan_records_the_drive_position_not_the_request():
    """The Due rounds a commanded float to the half-degree pulse grid.

    Fitting against the requested float puts up to a quarter of a degree of
    that rounding straight into every scan point. The scan must use the drive
    position the mount reports having reached instead.
    """
    captured = {}

    def rounding_slew(base_url, alt, az, *args, **kwargs):
        # What the Due does: round(deg x PULSES_PER_DEGREE) / PULSES_PER_DEGREE.
        return (round(alt * 2.0) / 2.0, round(az * 2.0) / 2.0)

    def capture_fit(alt_offsets, az_offsets, power, beam_fwhm_hint=3.0):
        captured["alt"] = np.asarray(alt_offsets)
        captured["az"] = np.asarray(az_offsets)
        return {
            "alt_error_deg": 0.0, "az_error_deg": 0.0, "az_error_sky_deg": 0.0,
            "amplitude": 8.0, "sigma_deg": 1.0, "offset": 1.0,
            "beam_fwhm_deg": 2.355,
            "fit_errors": {"alt_err": 0.1, "az_err": 0.1, "sigma_err": 0.1},
            "success": True,
        }

    meter = MagicMock()
    meter.measure.side_effect = [float(i) for i in range(1, 10)]

    # A Sun altitude deliberately off the half-degree grid, so the rounded
    # positions cannot coincide with the requested offsets by luck.
    sun_alt = 30.3
    with patch.object(sun_scan, "get_sun_altaz", return_value=(sun_alt, 100.3)), \
         patch.object(sun_scan, "_slew_to", side_effect=rounding_slew), \
         patch.object(sun_scan, "_B210PowerMeter", return_value=meter), \
         patch.object(sun_scan, "fit_pointing_error", side_effect=capture_fit), \
         patch.object(sun_scan.time, "sleep"):
        result = sun_scan.sun_scan(
            n=3, grid_spacing_deg=1.5, integration_time_s=0.1,
            sdr_type="b210", srt_url="http://controller", output_image=None,
        )

    # Every abscissa is a real drive position minus the Sun, so it sits on the
    # pulse grid relative to the Sun's altitude rather than on the requested
    # 1.5 degree spacing.
    on_grid = np.allclose((captured["alt"] + sun_alt) * 2.0,
                          np.round((captured["alt"] + sun_alt) * 2.0))
    assert on_grid
    # And it is not simply the intended offsets: the Sun sits off the grid.
    assert not np.allclose(np.sort(np.unique(captured["alt"])), [-1.5, 0.0, 1.5])
    assert result["record_version"] == sun_scan._SCAN_RECORD_VERSION


def _rotate_horizon_frame(alt_deg, az_deg, tilt_north_deg, tilt_east_deg):
    """Alt/az of a source after tilting the mount's vertical axis.

    Independent of the design matrix: builds the source unit vector in the
    north/east/up frame, rotates the frame, and reads the angles back.
    """
    alt, az = math.radians(alt_deg), math.radians(az_deg)
    vec = np.array([math.cos(alt) * math.cos(az),
                    math.cos(alt) * math.sin(az),
                    math.sin(alt)])
    tn, te = math.radians(tilt_north_deg), math.radians(tilt_east_deg)
    # Tipping "up" toward north is a rotation about the east axis, and toward
    # east a rotation about the north axis.
    about_east = np.array([[math.cos(-tn), 0, math.sin(-tn)],
                           [0, 1, 0],
                           [-math.sin(-tn), 0, math.cos(-tn)]])
    about_north = np.array([[1, 0, 0],
                            [0, math.cos(te), -math.sin(te)],
                            [0, math.sin(te), math.cos(te)]])
    rotated = about_north @ (about_east @ vec)
    return (math.degrees(math.asin(rotated[2])),
            math.degrees(math.atan2(rotated[1], rotated[0])) % 360.0)


@pytest.mark.parametrize("alt_deg,az_deg", [
    (45.3, 235.6), (33.3, 259.1), (12.4, 290.1), (30.0, 120.0), (60.0, 15.0),
])
def test_pointing_model_matrix_matches_frame_rotation(alt_deg, az_deg):
    """The design matrix must be the derivative of a real frame rotation.

    Generating test data from the same matrix that is being tested cannot catch
    a sign error in it, so compare against independently rotated coordinates.
    """
    step = 1e-3
    matrix = sun_scan._pointing_model_matrix(np.array([alt_deg]), np.array([az_deg]))

    for column, (tn, te) in ((2, (step, 0.0)), (3, (0.0, step))):
        alt_hi, az_hi = _rotate_horizon_frame(alt_deg, az_deg, tn, te)
        alt_lo, az_lo = _rotate_horizon_frame(alt_deg, az_deg, -tn, -te)
        d_alt = (alt_hi - alt_lo) / (2 * step)
        d_az = ((az_hi - az_lo + 180.0) % 360.0 - 180.0) / (2 * step)

        assert matrix[0, column] == pytest.approx(d_alt, abs=1e-3)
        assert matrix[1, column] == pytest.approx(d_az, abs=1e-3)


def test_refraction_is_removed_before_the_terms_are_fitted():
    """The controller applies refraction itself, so the model must not.

    Fitting the raw measured error would push refraction into IE and TF, and
    the controller would then add it a second time - about 0.09 deg at low
    elevation, which is the size of the error being chased.
    """
    data, expected = _synthetic_pointing_data([80, 105, 135, 165, 195, 225])
    unrefracted = [dict(entry) for entry in data]
    for entry, alt in zip(unrefracted, [25.0, 35.0, 45.0, 55.0, 50.0, 30.0]):
        entry["alt_error_deg"] -= sun_scan.refraction_deg(alt)

    with_refraction = sun_scan.fit_pointing_model(data, true_lat=55.9, true_lon=-4.3)
    without = sun_scan.fit_pointing_model(unrefracted, true_lat=55.9, true_lon=-4.3)

    assert with_refraction["alt_offset_deg"] == pytest.approx(expected[0], abs=1e-9)
    # Removing refraction from data that never had it biases the altitude zero
    # point by roughly the refraction over the sampled elevations.
    assert without["alt_offset_deg"] < expected[0] - 0.02


def test_refraction_matches_the_controller_formula():
    """Bennett scaled by 1.15, the same numbers as refractionDeg() in pointing.cpp."""
    assert sun_scan.refraction_deg(90.0) == pytest.approx(0.0, abs=1e-9)
    assert sun_scan.refraction_deg(11.0) == pytest.approx(0.094, abs=0.01)
    assert sun_scan.refraction_deg(5.0) > sun_scan.refraction_deg(20.0)
    assert sun_scan.refraction_deg(-5.0) < 1.0


def test_scans_recorded_before_the_resident_model_are_not_pooled():
    """Version 1 records are residuals against a model no longer knowable."""
    data, _ = _synthetic_pointing_data([80, 105, 135, 165, 195, 225])
    for entry in data:
        del entry["record_version"]

    model = sun_scan.fit_pointing_model(data, true_lat=55.9, true_lon=-4.3)

    assert model["success"] is False
    assert model["n_superseded"] == 6
    assert "predate" in model["error"]


def test_scan_uncertainty_includes_the_encoder_grid():
    """The centroid uncertainty is not the whole error.

    Recording the reached drive position removed the Due's commanded-versus-
    reported rounding from the fit, but the reported position is still a count
    of half-degree pulses, so the beam axis is somewhere inside that cell.
    curve_fit assumes exact abscissae and cannot know this, so however small a
    centroid uncertainty a scan claims, no scan is better than the grid.

    Without it the 2026-08-20 calibration day fitted at reduced chi-squared 28
    and could not be applied; with it, 1.05.
    """
    data, _ = _synthetic_pointing_data([80, 105, 135, 165, 195, 225])
    for entry in data:
        entry["alt_error_uncertainty_deg"] = 1e-6
        entry["az_error_uncertainty_deg"] = 1e-6

    model = sun_scan.fit_pointing_model(data, true_lat=55.9, true_lon=-4.3)

    assert model["success"] is True
    # A vanishing stated sigma must not buy a vanishing parameter error.
    assert model["parameter_errors_deg"]["tilt_north"] > 0.01
    assert sun_scan._ENCODER_QUANTISATION_SIGMA_DEG == pytest.approx(0.1443, abs=1e-4)


def test_azimuth_scale_is_fitted_and_exported():
    """A scaling between measured and true drive azimuth is a mount property.

    The reported azimuth is a count of encoder pulses; if a pulse is not exactly
    half a degree the error grows with distance from the encoder zero. No
    geometric term can represent that, so leaving it out pushes it into the
    tilt.
    """
    azimuths = [60, 90, 120, 150, 180, 210, 240, 270]
    data, expected = _synthetic_pointing_data(azimuths)
    scale = 0.004  # deg per deg
    for entry in data:
        entry["az_error_deg"] += scale * entry["sun_az_deg"]

    model = sun_scan.fit_pointing_model(data, true_lat=55.9, true_lon=-4.3)

    assert model["success"] is True
    assert model["az_scale"]["deg_per_deg"] == pytest.approx(scale, abs=5e-4)
    # Exported for the controller, which applies it like any other term.
    assert model["terms"]["AZSCALE"] == pytest.approx(scale, abs=5e-4)
    # And the tilt is recovered rather than bent to absorb the scale.
    assert model["terms"]["AN"] == pytest.approx(expected[2], abs=0.05)
    assert model["terms"]["AE"] == pytest.approx(expected[3], abs=0.05)


def test_azimuth_scale_not_fitted_on_a_narrow_arc():
    """Over a short arc a scale error is indistinguishable from a constant offset.

    Fitting it anyway would let it trade against IA, so it is left out and
    omitted from the terms rather than sent as a fitted zero.
    """
    data, _ = _synthetic_pointing_data([150, 160, 170, 180, 190, 200])

    model = sun_scan.fit_pointing_model(data, true_lat=55.9, true_lon=-4.3)

    assert model["success"] is True
    assert model["az_scale"] is None
    assert "AZSCALE" not in model["terms"]


def test_azimuth_scale_reaches_the_wire_document():
    azimuths = [60, 90, 120, 150, 180, 210, 240, 270]
    data, _ = _synthetic_pointing_data(azimuths)
    for entry in data:
        entry["az_error_deg"] += 0.004 * entry["sun_az_deg"]
    model = sun_scan.fit_pointing_model(data, true_lat=55.9, true_lon=-4.3)

    document = sun_scan.pointing_model_document(model)

    assert document["terms"]["AZSCALE"] == pytest.approx(0.004, abs=5e-4)


def test_pointing_model_reports_tilt_significance():
    data, _ = _synthetic_pointing_data([80, 105, 135, 165, 195, 225])
    noisy = [dict(entry) for entry in data]
    # Swamp the tilt signal with a large constant altitude error.
    for entry in noisy:
        entry["alt_error_deg"] += 5.0

    model = sun_scan.fit_pointing_model(noisy, true_lat=55.9, true_lon=-4.3)

    assert "min_tilt_significance" in model
    assert model["parameter_significance"]["alt_offset"] > model["min_tilt_significance"]
