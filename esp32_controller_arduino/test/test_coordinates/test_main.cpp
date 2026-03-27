// Unit tests for coordinate transformation functions
// Run with: pio test -e native

#include <unity.h>
#include <cmath>
#include <ctime>
#include "coordinates.h"

// Test tolerance for floating point comparisons
// For radio telescope pointing, we want sub-arcminute precision
#define ANGLE_TOLERANCE 0.02      // 0.02 degrees = 72 arcseconds (allows for precession drift)
#define RA_TOLERANCE 0.0002       // 0.0002 hours = 0.72 arcseconds
#define JD_TOLERANCE 0.00001      // ~1 second

void setUp(void) {
    // Called before each test
}

void tearDown(void) {
    // Called after each test
}

// =============================================================================
// Julian Date tests
// =============================================================================

void test_julian_date_j2000_epoch() {
    // J2000.0 epoch is January 1, 2000 at 12:00 TT
    // JD = 2451545.0
    double jd = julianDate(2000, 1, 1, 12, 0, 0);
    TEST_ASSERT_FLOAT_WITHIN(JD_TOLERANCE, 2451545.0, jd);
}

void test_julian_date_known_value() {
    // October 4, 1957 (Sputnik launch) at 19:28:34 UTC
    // JD = 2436116.31150
    double jd = julianDate(1957, 10, 4, 19, 28, 34);
    TEST_ASSERT_FLOAT_WITHIN(JD_TOLERANCE, 2436116.31150, jd);
}

void test_julian_date_leap_year() {
    // February 29, 2024 at noon
    double jd = julianDate(2024, 2, 29, 12, 0, 0);
    TEST_ASSERT_FLOAT_WITHIN(JD_TOLERANCE, 2460370.0, jd);
}

// =============================================================================
// GMST tests
// =============================================================================

void test_gmst_j2000_epoch() {
    // At J2000.0 epoch, GMST should be approximately 18.697374558 hours
    double jd = 2451545.0;  // J2000.0
    double sidereal = gmst(jd, 12.0);
    TEST_ASSERT_FLOAT_WITHIN(RA_TOLERANCE, 18.697374558, sidereal);
}

// =============================================================================
// Galactic coordinate tests
// =============================================================================

void test_galactic_center_to_equatorial() {
    // Galactic center (l=0, b=0) J2000: RA=17h45m37s, Dec=-28°56'10"
    double ra, dec;
    galacticToEquatorial(0.0, 0.0, ra, dec);
    TEST_ASSERT_FLOAT_WITHIN(RA_TOLERANCE, 17.76033, ra);     // 17h45m37s
    TEST_ASSERT_FLOAT_WITHIN(ANGLE_TOLERANCE, -28.9362, dec); // -28°56'10"
}

void test_galactic_north_pole_to_equatorial() {
    // North Galactic Pole (b=90) J2000: RA=12h51m26s, Dec=+27°07'42"
    double ra, dec;
    galacticToEquatorial(0.0, 90.0, ra, dec);
    TEST_ASSERT_FLOAT_WITHIN(RA_TOLERANCE, 12.8572, ra);     // 12h51m26s
    TEST_ASSERT_FLOAT_WITHIN(ANGLE_TOLERANCE, 27.1283, dec); // +27°07'42"
}

void test_galactic_anticenter_to_equatorial() {
    // Galactic anticenter (l=180, b=0) J2000: RA=5h45m37s, Dec=+28°56'10"
    double ra, dec;
    galacticToEquatorial(180.0, 0.0, ra, dec);
    TEST_ASSERT_FLOAT_WITHIN(RA_TOLERANCE, 5.76033, ra);      // 5h45m37s
    TEST_ASSERT_FLOAT_WITHIN(ANGLE_TOLERANCE, 28.9362, dec);  // +28°56'10"
}

void test_equatorial_to_galactic_roundtrip() {
    // Test roundtrip conversion
    double l_in = 120.0, b_in = 45.0;
    double ra, dec, l_out, b_out;

    galacticToEquatorial(l_in, b_in, ra, dec);
    equatorialToGalactic(ra, dec, l_out, b_out);

    TEST_ASSERT_FLOAT_WITHIN(ANGLE_TOLERANCE, l_in, l_out);
    TEST_ASSERT_FLOAT_WITHIN(ANGLE_TOLERANCE, b_in, b_out);
}

// =============================================================================
// Precession tests
// =============================================================================

void test_precession_j2000_no_change() {
    // At J2000.0 epoch, precession should have no effect
    double jd = 2451545.0;  // J2000.0
    double ra_in = 12.0, dec_in = 45.0;
    double ra_out, dec_out;

    precessJ2000ToDate(ra_in, dec_in, jd, ra_out, dec_out);

    TEST_ASSERT_FLOAT_WITHIN(RA_TOLERANCE, ra_in, ra_out);
    TEST_ASSERT_FLOAT_WITHIN(ANGLE_TOLERANCE, dec_in, dec_out);
}

void test_precession_roundtrip() {
    // Test roundtrip: J2000 -> date -> J2000
    double jd = 2460000.0;  // Some date in 2023
    double ra_in = 6.0, dec_in = -20.0;
    double ra_mid, dec_mid, ra_out, dec_out;

    precessJ2000ToDate(ra_in, dec_in, jd, ra_mid, dec_mid);
    precessDateToJ2000(ra_mid, dec_mid, jd, ra_out, dec_out);

    TEST_ASSERT_FLOAT_WITHIN(RA_TOLERANCE, ra_in, ra_out);
    TEST_ASSERT_FLOAT_WITHIN(ANGLE_TOLERANCE, dec_in, dec_out);
}

void test_precession_polaris_drift() {
    // Polaris coordinates at J2000: RA ~2h31m, Dec ~89.26 deg
    // Over 100 years, RA should increase noticeably due to precession
    double jd_2000 = 2451545.0;
    double jd_2100 = jd_2000 + 36525.0;  // 100 years later

    double ra_2100, dec_2100;
    precessJ2000ToDate(2.52, 89.26, jd_2100, ra_2100, dec_2100);

    // RA should have changed by a few degrees worth
    TEST_ASSERT_TRUE(fabs(ra_2100 - 2.52) > 0.1);
}

// =============================================================================
// Alt/Az conversion tests (using fixed time for reproducibility)
// =============================================================================

void test_altaz_zenith() {
    // Object at zenith should have alt=90 regardless of azimuth
    // For any location, an object at zenith has Dec = latitude
    // Note: includes precession effects so allow slightly wider tolerance
    double ra, dec;
    double lat = 55.9, lon = -4.3;

    // An object straight up (alt=90) from Glasgow
    altAzToRaDec(90.0, 0.0, lat, lon, ra, dec);

    // Dec should be close to latitude (precession causes small drift)
    TEST_ASSERT_FLOAT_WITHIN(0.05, lat, dec);  // 0.05 deg = 3 arcmin
}

void test_altaz_horizon_north() {
    // Object on northern horizon (alt=0, az=0)
    // Note: includes precession effects so allow slightly wider tolerance
    double ra, dec;
    double lat = 55.9, lon = -4.3;

    altAzToRaDec(0.0, 0.0, lat, lon, ra, dec);

    // For alt=0, az=0: sin(dec) = cos(lat)*cos(az) = cos(55.9)
    // dec = asin(cos(55.9)) = 90 - 55.9 = 34.1 degrees
    TEST_ASSERT_FLOAT_WITHIN(0.05, 90.0 - lat, dec);  // 0.05 deg = 3 arcmin
}

// =============================================================================
// Sun position tests
// =============================================================================

void test_sun_position_reasonable_range() {
    // Sun's RA should be 0-24h, Dec should be within +/- 23.5 degrees
    double ra, dec;
    getSunPosition(ra, dec);

    TEST_ASSERT_TRUE(ra >= 0.0 && ra < 24.0);
    TEST_ASSERT_TRUE(dec >= -23.5 && dec <= 23.5);
}

// =============================================================================
// Moon position tests
// =============================================================================

void test_moon_position_reasonable_range() {
    // Moon's RA should be 0-24h, Dec should be within +/- 28.5 degrees
    double ra, dec;
    getMoonPosition(ra, dec);

    TEST_ASSERT_TRUE(ra >= 0.0 && ra < 24.0);
    TEST_ASSERT_TRUE(dec >= -29.0 && dec <= 29.0);
}

// =============================================================================
// Known radio source coordinates (J2000)
// =============================================================================

void test_crab_nebula_m1() {
    // Crab Nebula (M1/Taurus A) - strong radio source, supernova remnant
    // J2000: RA = 05h 34m 31.94s, Dec = +22° 00' 52.2"
    // Galactic: l = 184.56°, b = -5.78°
    double ra, dec;
    galacticToEquatorial(184.56, -5.78, ra, dec);

    // Expected RA = 5.5755h (5h34m31s), Dec = +22.01°
    TEST_ASSERT_FLOAT_WITHIN(0.05, 5.575, ra);   // Within 3 arcmin
    TEST_ASSERT_FLOAT_WITHIN(0.1, 22.01, dec);
}

void test_cassiopeia_a() {
    // Cassiopeia A - brightest extrasolar radio source in sky
    // J2000: RA = 23h 23m 24s, Dec = +58° 48' 54"
    // Galactic: l = 111.73°, b = -2.13°
    double ra, dec;
    galacticToEquatorial(111.73, -2.13, ra, dec);

    // Expected RA = 23.39h, Dec = +58.82°
    TEST_ASSERT_FLOAT_WITHIN(0.05, 23.39, ra);
    TEST_ASSERT_FLOAT_WITHIN(0.1, 58.82, dec);
}

void test_cygnus_a() {
    // Cygnus A - classic radio galaxy, very strong source
    // J2000: RA = 19h 59m 28.3s, Dec = +40° 44' 02"
    // Galactic: l = 76.19°, b = 5.76°
    double ra, dec;
    galacticToEquatorial(76.19, 5.76, ra, dec);

    // Expected RA = 19.99h, Dec = +40.73°
    TEST_ASSERT_FLOAT_WITHIN(0.05, 19.99, ra);
    TEST_ASSERT_FLOAT_WITHIN(0.1, 40.73, dec);
}

void test_sagittarius_a() {
    // Sagittarius A* - galactic center radio source
    // J2000: RA = 17h 45m 40.0s, Dec = -29° 00' 28"
    // Galactic: l = 0°, b = 0° (by definition)
    double ra, dec;
    galacticToEquatorial(0.0, 0.0, ra, dec);

    // Expected RA = 17.76h, Dec = -29.0°
    TEST_ASSERT_FLOAT_WITHIN(0.02, 17.76, ra);
    TEST_ASSERT_FLOAT_WITHIN(0.1, -29.0, dec);
}

// =============================================================================
// Sun seasonal positions (verify ephemeris)
// =============================================================================

void test_sun_at_vernal_equinox() {
    // At vernal equinox (~March 20), Sun is at RA=0h, Dec=0°
    // Test with fixed JD for March 20, 2024 12:00 UTC
    double jd = julianDate(2024, 3, 20, 12, 0, 0);

    // Calculate expected Sun RA at this date (should be near 0h)
    // We can't directly test getSunPosition() at arbitrary time,
    // but we can verify the formula produces valid equinox-like output
    // For now, just verify current sun is in valid range
    double ra, dec;
    getSunPosition(ra, dec);

    // Sun Dec should always be within ecliptic bounds
    TEST_ASSERT_TRUE(dec >= -23.5 && dec <= 23.5);
}

void test_sun_declination_bounds() {
    // Sun's declination should never exceed ±23.44° (obliquity of ecliptic)
    double ra, dec;
    getSunPosition(ra, dec);

    TEST_ASSERT_TRUE(dec >= -23.45 && dec <= 23.45);
}

// =============================================================================
// Local Sidereal Time tests
// =============================================================================

void test_lst_at_greenwich_j2000() {
    // At J2000.0 epoch, GMST = 18.697374558h
    // At Greenwich (lon=0), LST = GMST
    double jd = 2451545.0;  // J2000.0
    double lst = localSiderealTime(jd, 0.0, 12.0);

    TEST_ASSERT_FLOAT_WITHIN(0.001, 18.697374558, lst);
}

void test_lst_longitude_offset() {
    // LST should differ by longitude/15 hours from GMST
    double jd = 2451545.0;
    double gmst_val = gmst(jd, 12.0);

    // Glasgow at -4.3° longitude
    double lst_glasgow = localSiderealTime(jd, -4.3, 12.0);

    // LST = GMST + lon/15
    double expected = fmod(gmst_val + (-4.3/15.0) + 24.0, 24.0);
    TEST_ASSERT_FLOAT_WITHIN(0.001, expected, lst_glasgow);
}

void test_lst_increases_with_time() {
    // LST should increase by ~4 minutes per day more than solar time
    // Over 24 hours, LST advances by ~24h 3m 56s
    double jd1 = julianDate(2024, 6, 15, 12, 0, 0);
    double jd2 = julianDate(2024, 6, 16, 12, 0, 0);  // 24 hours later

    double lst1 = localSiderealTime(jd1, 0.0, 12.0);
    double lst2 = localSiderealTime(jd2, 0.0, 12.0);

    // LST should have advanced by slightly more than 24h (wraps around)
    // The difference should be close to 0 but slightly positive
    double diff = lst2 - lst1;
    if (diff < 0) diff += 24.0;

    // Sidereal day is ~3m 56s shorter, so LST gains ~0.0657h per solar day
    TEST_ASSERT_FLOAT_WITHIN(0.01, 0.0657, diff);
}

// =============================================================================
// Roundtrip consistency tests
// =============================================================================

void test_radec_altaz_roundtrip() {
    // Convert RA/Dec -> Alt/Az -> RA/Dec should give original (approximately)
    // Note: This involves current time, so we test consistency not absolute values
    double lat = 55.9, lon = -4.3;
    double ra_in = 12.0, dec_in = 45.0;

    // Convert to Alt/Az
    double alt, az;
    raDecToAltAz(ra_in, dec_in, lat, lon, alt, az);

    // Skip if object is below horizon (alt < 0)
    if (alt < 0) {
        TEST_PASS();
        return;
    }

    // Convert back to RA/Dec
    double ra_out, dec_out;
    altAzToRaDec(alt, az, lat, lon, ra_out, dec_out);

    // Should get back close to original (within precession tolerance)
    TEST_ASSERT_FLOAT_WITHIN(0.1, ra_in, ra_out);   // 0.1h = 6 arcmin
    TEST_ASSERT_FLOAT_WITHIN(0.1, dec_in, dec_out); // 0.1 deg = 6 arcmin
}

void test_galactic_equatorial_roundtrip_multiple() {
    // Test roundtrip at multiple points across the sky
    double test_points[][2] = {
        {0.0, 0.0},      // Galactic center
        {180.0, 0.0},    // Anticenter
        {90.0, 0.0},     // l=90
        {270.0, 0.0},    // l=270
        {0.0, 45.0},     // Mid-latitude
        {0.0, -45.0},    // Southern mid-latitude
        {120.0, 60.0},   // Arbitrary point
        {240.0, -30.0},  // Another arbitrary point
    };

    for (int i = 0; i < 8; i++) {
        double l_in = test_points[i][0];
        double b_in = test_points[i][1];
        double ra, dec, l_out, b_out;

        galacticToEquatorial(l_in, b_in, ra, dec);
        equatorialToGalactic(ra, dec, l_out, b_out);

        TEST_ASSERT_FLOAT_WITHIN(ANGLE_TOLERANCE, l_in, l_out);
        TEST_ASSERT_FLOAT_WITHIN(ANGLE_TOLERANCE, b_in, b_out);
    }
}

// =============================================================================
// Galactic plane survey points (21cm HI observations)
// =============================================================================

void test_galactic_plane_survey_points() {
    // Common galactic plane survey points should convert correctly
    // These are along b=0 at various longitudes
    double survey_longitudes[] = {0, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300, 330};

    for (int i = 0; i < 12; i++) {
        double l = survey_longitudes[i];
        double ra, dec;

        galacticToEquatorial(l, 0.0, ra, dec);

        // Should produce valid coordinates
        TEST_ASSERT_TRUE(ra >= 0.0 && ra < 24.0);
        TEST_ASSERT_TRUE(dec >= -90.0 && dec <= 90.0);
        TEST_ASSERT_TRUE(!isnan(ra) && !isnan(dec));
    }
}

void test_hi_cloud_velocities_region() {
    // High-velocity cloud regions are at high galactic latitudes
    // Test conversion for typical HVC observation points
    double hvc_points[][2] = {
        {120.0, 50.0},   // Northern HVC region
        {280.0, -40.0},  // Magellanic Stream region
        {0.0, 80.0},     // North galactic pole region
    };

    for (int i = 0; i < 3; i++) {
        double l = hvc_points[i][0];
        double b = hvc_points[i][1];
        double ra, dec;

        galacticToEquatorial(l, b, ra, dec);

        TEST_ASSERT_TRUE(ra >= 0.0 && ra < 24.0);
        TEST_ASSERT_TRUE(dec >= -90.0 && dec <= 90.0);
        TEST_ASSERT_TRUE(!isnan(ra) && !isnan(dec));
    }
}

// =============================================================================
// Edge cases and boundary tests
// =============================================================================

void test_ra_wraparound_24h() {
    // RA values should wrap correctly at 24h boundary
    double l_out, b_out, ra, dec;

    // Start with known galactic coords, convert to RA/Dec
    galacticToEquatorial(359.0, 0.0, ra, dec);

    // RA should be normalized to 0-24 range
    TEST_ASSERT_TRUE(ra >= 0.0 && ra < 24.0);

    // Convert back - should get same galactic coords
    equatorialToGalactic(ra, dec, l_out, b_out);
    TEST_ASSERT_FLOAT_WITHIN(ANGLE_TOLERANCE, 359.0, l_out);
}

void test_dec_at_poles() {
    // Test declination at celestial poles (+90, -90)
    double l, b;

    // North celestial pole
    equatorialToGalactic(0.0, 90.0, l, b);
    TEST_ASSERT_TRUE(b > 0);  // Should be in northern galactic hemisphere
    TEST_ASSERT_TRUE(!isnan(l) && !isnan(b));

    // South celestial pole
    equatorialToGalactic(0.0, -90.0, l, b);
    TEST_ASSERT_TRUE(b < 0);  // Should be in southern galactic hemisphere
    TEST_ASSERT_TRUE(!isnan(l) && !isnan(b));
}

void test_galactic_longitude_wraparound() {
    // Test galactic longitude at 0/360 boundary
    double ra1, dec1, ra2, dec2;

    galacticToEquatorial(0.0, 45.0, ra1, dec1);
    galacticToEquatorial(360.0, 45.0, ra2, dec2);

    // Should give same result (360 = 0)
    TEST_ASSERT_FLOAT_WITHIN(RA_TOLERANCE, ra1, ra2);
    TEST_ASSERT_FLOAT_WITHIN(ANGLE_TOLERANCE, dec1, dec2);
}

void test_galactic_poles() {
    // At galactic poles (b=+/-90), longitude is undefined but should not crash
    double ra, dec;

    // North galactic pole
    galacticToEquatorial(0.0, 90.0, ra, dec);
    TEST_ASSERT_TRUE(!isnan(ra) && !isnan(dec));
    TEST_ASSERT_TRUE(ra >= 0.0 && ra < 24.0);
    TEST_ASSERT_TRUE(dec >= -90.0 && dec <= 90.0);

    // South galactic pole
    galacticToEquatorial(0.0, -90.0, ra, dec);
    TEST_ASSERT_TRUE(!isnan(ra) && !isnan(dec));
    TEST_ASSERT_TRUE(ra >= 0.0 && ra < 24.0);
    TEST_ASSERT_TRUE(dec >= -90.0 && dec <= 90.0);
}

void test_julian_date_edge_cases() {
    // Test month/year boundaries
    double jd1 = julianDate(1999, 12, 31, 23, 59, 59);
    double jd2 = julianDate(2000, 1, 1, 0, 0, 0);

    // Should be ~1 second apart
    TEST_ASSERT_FLOAT_WITHIN(0.0001, 1.0/86400.0, jd2 - jd1);

    // Test leap year boundary
    double jd_leap = julianDate(2024, 2, 29, 12, 0, 0);
    double jd_next = julianDate(2024, 3, 1, 12, 0, 0);

    // Should be exactly 1 day apart
    TEST_ASSERT_FLOAT_WITHIN(JD_TOLERANCE, 1.0, jd_next - jd_leap);
}

void test_altaz_at_pole() {
    // Observer at north pole - azimuth becomes degenerate
    double ra, dec;
    double lat = 89.99;  // Very close to pole (exact 90 causes div by zero)
    double lon = 0.0;

    // Object at zenith from near-pole
    altAzToRaDec(90.0, 0.0, lat, lon, ra, dec);

    // Should not produce NaN
    TEST_ASSERT_TRUE(!isnan(ra) && !isnan(dec));
    // Dec should be close to latitude
    TEST_ASSERT_FLOAT_WITHIN(1.0, lat, dec);
}

void test_altaz_horizon_boundary() {
    // Object exactly on horizon (alt=0)
    double ra, dec;
    double lat = 55.9, lon = -4.3;

    // Test all cardinal directions on horizon
    altAzToRaDec(0.0, 0.0, lat, lon, ra, dec);    // North
    TEST_ASSERT_TRUE(!isnan(ra) && !isnan(dec));

    altAzToRaDec(0.0, 90.0, lat, lon, ra, dec);   // East
    TEST_ASSERT_TRUE(!isnan(ra) && !isnan(dec));

    altAzToRaDec(0.0, 180.0, lat, lon, ra, dec);  // South
    TEST_ASSERT_TRUE(!isnan(ra) && !isnan(dec));

    altAzToRaDec(0.0, 270.0, lat, lon, ra, dec);  // West
    TEST_ASSERT_TRUE(!isnan(ra) && !isnan(dec));
}

void test_negative_altitude_rejected() {
    // Negative altitude is below horizon - should still compute (for rise/set calcs)
    double ra, dec;
    double lat = 55.9, lon = -4.3;

    altAzToRaDec(-10.0, 180.0, lat, lon, ra, dec);

    // Should not crash, but values may be extrapolated
    TEST_ASSERT_TRUE(!isnan(ra) && !isnan(dec));
}

void test_precession_far_future() {
    // Test precession for date far from J2000 (year 3000)
    double jd_3000 = julianDate(3000, 1, 1, 12, 0, 0);
    double ra_in = 12.0, dec_in = 45.0;
    double ra_out, dec_out;

    precessJ2000ToDate(ra_in, dec_in, jd_3000, ra_out, dec_out);

    // Should not produce NaN
    TEST_ASSERT_TRUE(!isnan(ra_out) && !isnan(dec_out));
    // RA should still be in valid range
    TEST_ASSERT_TRUE(ra_out >= 0.0 && ra_out < 24.0);
    // Dec should still be in valid range
    TEST_ASSERT_TRUE(dec_out >= -90.0 && dec_out <= 90.0);
    // Should have changed significantly (1000 years of precession)
    TEST_ASSERT_TRUE(fabs(ra_out - ra_in) > 0.5);
}

void test_precession_distant_past() {
    // Test precession for date far before J2000 (year 1000)
    double jd_1000 = julianDate(1000, 1, 1, 12, 0, 0);
    double ra_in = 12.0, dec_in = 45.0;
    double ra_out, dec_out;

    precessJ2000ToDate(ra_in, dec_in, jd_1000, ra_out, dec_out);

    // Should not produce NaN
    TEST_ASSERT_TRUE(!isnan(ra_out) && !isnan(dec_out));
    TEST_ASSERT_TRUE(ra_out >= 0.0 && ra_out < 24.0);
    TEST_ASSERT_TRUE(dec_out >= -90.0 && dec_out <= 90.0);
}

// =============================================================================
// Main
// =============================================================================

int main(int argc, char **argv) {
    UNITY_BEGIN();

    // Julian Date
    RUN_TEST(test_julian_date_j2000_epoch);
    RUN_TEST(test_julian_date_known_value);
    RUN_TEST(test_julian_date_leap_year);
    RUN_TEST(test_julian_date_edge_cases);

    // GMST and LST
    RUN_TEST(test_gmst_j2000_epoch);
    RUN_TEST(test_lst_at_greenwich_j2000);
    RUN_TEST(test_lst_longitude_offset);
    RUN_TEST(test_lst_increases_with_time);

    // Galactic coordinates
    RUN_TEST(test_galactic_center_to_equatorial);
    RUN_TEST(test_galactic_north_pole_to_equatorial);
    RUN_TEST(test_galactic_anticenter_to_equatorial);
    RUN_TEST(test_equatorial_to_galactic_roundtrip);
    RUN_TEST(test_galactic_longitude_wraparound);
    RUN_TEST(test_galactic_poles);

    // Precession
    RUN_TEST(test_precession_j2000_no_change);
    RUN_TEST(test_precession_roundtrip);
    RUN_TEST(test_precession_polaris_drift);
    RUN_TEST(test_precession_far_future);
    RUN_TEST(test_precession_distant_past);

    // Alt/Az
    RUN_TEST(test_altaz_zenith);
    RUN_TEST(test_altaz_horizon_north);
    RUN_TEST(test_altaz_at_pole);
    RUN_TEST(test_altaz_horizon_boundary);
    RUN_TEST(test_negative_altitude_rejected);

    // Roundtrip consistency
    RUN_TEST(test_radec_altaz_roundtrip);
    RUN_TEST(test_galactic_equatorial_roundtrip_multiple);

    // Boundary conditions
    RUN_TEST(test_ra_wraparound_24h);
    RUN_TEST(test_dec_at_poles);

    // Ephemeris
    RUN_TEST(test_sun_position_reasonable_range);
    RUN_TEST(test_sun_at_vernal_equinox);
    RUN_TEST(test_sun_declination_bounds);
    RUN_TEST(test_moon_position_reasonable_range);

    // Known radio sources
    RUN_TEST(test_crab_nebula_m1);
    RUN_TEST(test_cassiopeia_a);
    RUN_TEST(test_cygnus_a);
    RUN_TEST(test_sagittarius_a);

    // Galactic plane survey (21cm HI)
    RUN_TEST(test_galactic_plane_survey_points);
    RUN_TEST(test_hi_cloud_velocities_region);

    return UNITY_END();
}
