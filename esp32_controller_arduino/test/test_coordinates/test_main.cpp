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
    double ra, dec;
    double lat = 55.9, lon = -4.3;

    // An object straight up (alt=90) from Glasgow
    altAzToRaDec(90.0, 0.0, lat, lon, ra, dec);

    // Dec should be exactly equal to latitude
    TEST_ASSERT_FLOAT_WITHIN(ANGLE_TOLERANCE, lat, dec);
}

void test_altaz_horizon_north() {
    // Object on northern horizon (alt=0, az=0)
    double ra, dec;
    double lat = 55.9, lon = -4.3;

    altAzToRaDec(0.0, 0.0, lat, lon, ra, dec);

    // For alt=0, az=0: sin(dec) = cos(lat)*cos(az) = cos(55.9)
    // dec = asin(cos(55.9)) = 90 - 55.9 = 34.1 degrees
    TEST_ASSERT_FLOAT_WITHIN(ANGLE_TOLERANCE, 90.0 - lat, dec);
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
// Main
// =============================================================================

int main(int argc, char **argv) {
    UNITY_BEGIN();

    // Julian Date
    RUN_TEST(test_julian_date_j2000_epoch);
    RUN_TEST(test_julian_date_known_value);
    RUN_TEST(test_julian_date_leap_year);

    // GMST
    RUN_TEST(test_gmst_j2000_epoch);

    // Galactic coordinates
    RUN_TEST(test_galactic_center_to_equatorial);
    RUN_TEST(test_galactic_north_pole_to_equatorial);
    RUN_TEST(test_galactic_anticenter_to_equatorial);
    RUN_TEST(test_equatorial_to_galactic_roundtrip);

    // Precession
    RUN_TEST(test_precession_j2000_no_change);
    RUN_TEST(test_precession_roundtrip);
    RUN_TEST(test_precession_polaris_drift);

    // Alt/Az
    RUN_TEST(test_altaz_zenith);
    RUN_TEST(test_altaz_horizon_north);

    // Ephemeris
    RUN_TEST(test_sun_position_reasonable_range);
    RUN_TEST(test_moon_position_reasonable_range);

    return UNITY_END();
}
