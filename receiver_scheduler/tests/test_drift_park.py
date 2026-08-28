"""drift_park: the mount parks on a 0.5 deg drive grid, and a drift scan
should park on the grid point its source passes closest to, with the crossing
time known rather than assumed."""

import math
from datetime import datetime, timedelta

import pytest

import drift_park as dp

# The model the controller was applying on 2026-08-26 (/pointing), and two
# (drive, true) pairs it reported through /status that day. These pin the
# Python copy of trueToDrive to the controller's own arithmetic.
TERMS = {"IE": -0.50832, "IA": 1.68546, "AN": -0.46401, "AE": -0.90160,
         "CA": -1.04381, "NPAE": 0.0, "TF": 0.0, "AZSCALE": 0.000673}
REPORTED = [((44.0, 192.0), (43.84, 192.38)),
            ((0.0, 0.0), (0.39, 359.34))]


class TestModelCopy:
    @pytest.mark.parametrize("drive,true", REPORTED)
    def test_drive_to_true_matches_the_controller(self, drive, true):
        alt, az = dp.drive_to_true(drive[0], drive[1], TERMS)
        assert alt == pytest.approx(true[0], abs=0.006)
        assert az == pytest.approx(true[1], abs=0.006)

    @pytest.mark.parametrize("drive,true", REPORTED)
    def test_true_to_drive_lands_on_the_reported_grid_point(self, drive, true):
        alt, az = dp.true_to_drive(true[0], true[1], TERMS)
        assert (dp.quantise(alt), dp.quantise(az)) == drive

    def test_inverse_round_trips(self):
        # Including alt 87.5, where Cas A drifts and the sec(alt) azimuth
        # term is clamped: the controller's three passes leave a tenth of a
        # degree there, and the forward of what is sent must still land on
        # the grid point.
        for alt in (5.0, 30.0, 60.0, 85.0, 87.5):
            for az in (10.0, 120.0, 250.0, 350.0):
                t_alt, t_az = dp.drive_to_true(alt, az, TERMS)
                d_alt, d_az = dp.true_to_drive(t_alt, t_az, TERMS)
                assert d_alt == pytest.approx(alt, abs=1e-4)
                assert d_az == pytest.approx(az, abs=1e-4)

    def test_no_model_is_refraction_only(self):
        alt, az = dp.true_to_drive(30.0, 100.0, {})
        assert alt == pytest.approx(30.0 + dp.refraction_deg(30.0))
        assert az == 100.0
        assert dp.true_to_drive(30.0, 100.0, None) == (alt, az)

    def test_quantise_rounds_halves_away_from_zero_like_c(self):
        assert dp.quantise(43.75) == 44.0
        assert dp.quantise(43.74) == 43.5
        assert dp.quantise(0.24) == 0.0
        assert dp.quantise(0.25) == 0.5
        assert dp.quantise(0.0) == 0.0


def _westward_track(dec_deg, alt0, az0, t0):
    """A source moving west at the sidereal rate for its declination, in a
    locally flat sky around (alt0, az0): azimuth increases, altitude fixed."""
    rate = 15.0 / 3600.0 * math.cos(math.radians(dec_deg))  # deg/s on the sky

    def track(t):
        s = (t - t0).total_seconds()
        return alt0, az0 + rate * s / math.cos(math.radians(alt0))
    return track


class TestChooseParking:
    T0 = datetime(2026, 8, 26, 14, 0, 0)

    def test_parks_on_the_grid_and_reports_when_the_source_gets_there(self):
        # Start the source a little off a grid point in azimuth; altitude
        # 30.13 true is 0.13 + refraction - model from a grid line.
        track = _westward_track(0.0, 30.13, 180.2, self.T0)
        park = dp.choose_parking(track, self.T0, {})
        assert park["drive_alt"] % 0.5 == 0 and park["drive_az"] % 0.5 == 0
        # The chosen point is the true position of a grid point.
        d_alt, d_az = dp.true_to_drive(park["true_alt"], park["true_az"], {})
        assert (d_alt, d_az) == pytest.approx((park["drive_alt"], park["drive_az"]), abs=1e-4)
        # The source really is nearest that point at the reported time...
        alt, az = track(park["crossing"])
        assert dp.separation_deg(alt, az, park["true_alt"], park["true_az"]) == pytest.approx(
            park["offset_deg"], abs=1e-6)
        # ...which is within the search span, and the miss is only the
        # cross-drift part: 0.13 deg of altitude plus refraction, not the
        # quarter-degree the plain rounding would have left in azimuth.
        assert abs((park["crossing"] - self.T0).total_seconds()) <= dp.SEARCH_HALF_SPAN_S
        assert park["offset_deg"] < 0.2

    def test_prefers_the_closest_grid_point_to_the_track(self):
        # A track along a grid line in altitude, so the miss can be made zero.
        alt_true, _ = dp.drive_to_true(45.0, 200.0, {})
        track = _westward_track(0.0, alt_true, 199.8, self.T0)
        park = dp.choose_parking(track, self.T0, {})
        assert park["drive_alt"] == 45.0
        assert park["offset_deg"] < 1e-3
        # 0.2 deg of azimuth at alt 45 is 0.14 deg on the sky: 34 s of drift.
        assert (park["crossing"] - self.T0).total_seconds() == pytest.approx(34, abs=1.5)

    def test_a_fixed_source_crosses_at_the_requested_time(self):
        park = dp.choose_parking(lambda t: (45.0, 180.0), self.T0, {})
        assert park["crossing"] == self.T0

    def test_only_reachable_grid_points_are_chosen(self):
        """A source that sweeps azimuth through the 355-360 deg dead zone
        (Cas A transits due north at alt 87) must be parked on a grid point
        the mount can reach, just off exact transit - not on the closest
        point overall, which may be in the dead zone."""
        def track(t):
            m = (t - self.T0).total_seconds() / 60.0
            return 87.0, (1.6 - 4.7 * m) % 360.0
        reach = lambda da, dz: 0.0 <= da <= 90.0 and 0.0 <= dz <= 355.0
        park = dp.choose_parking(track, self.T0, {}, reachable=reach)
        assert reach(park["drive_alt"], park["drive_az"])
        # every candidate this returns really is a point the mount accepts
        assert 0.0 <= park["drive_az"] <= 355.0
        assert park["offset_deg"] < 0.5

    def test_none_when_no_grid_point_is_reachable(self):
        # A source that never leaves the dead zone.
        park = dp.choose_parking(lambda t: (87.0, 357.5), self.T0, {},
                                 reachable=lambda da, dz: dz <= 355.0)
        assert park is None

    def test_with_the_real_model_the_answer_is_a_grid_point(self):
        track = _westward_track(23.0, 43.9, 192.2, self.T0)
        park = dp.choose_parking(track, self.T0, TERMS)
        assert park["drive_alt"] % 0.5 == 0 and park["drive_az"] % 0.5 == 0
        d_alt, d_az = dp.true_to_drive(park["true_alt"], park["true_az"], TERMS)
        assert (dp.quantise(d_alt), dp.quantise(d_az)) == (park["drive_alt"], park["drive_az"])
        assert park["offset_deg"] <= 0.36


class TestCrossingAt:
    def test_finds_the_closest_approach_to_a_reported_position(self):
        t0 = datetime(2026, 8, 26, 14, 0, 0)
        track = _westward_track(0.0, 30.0, 180.0, t0)
        # A parked position 0.1 deg west (0.1155 deg of azimuth at alt 30)
        # and 0.05 deg high: crossed 24 s after t0, missed by 0.05 deg.
        when, sep = dp.crossing_at(track, 30.05, 180.0 + 0.1 / math.cos(math.radians(30.0)), t0)
        assert (when - t0).total_seconds() == pytest.approx(24, abs=1)
        assert sep == pytest.approx(0.05, abs=0.002)
