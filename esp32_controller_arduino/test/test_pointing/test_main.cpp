// Unit tests for the drive/true pointing transform.
//
// These run on the host, against the same pointing.cpp that is flashed. The
// mount itself cannot check this: on hardware a wrong transform looks like a
// telescope pointing somewhere plausible, and the whole point of issue #8 is
// that such an error stayed invisible for a long time.

#include <unity.h>
#include <cmath>
#include "pointing.h"
#include "settings.h"

// Well inside the 0.5 degree encoder quantum, but tight enough that a
// first-order inverse (which is out by ~0.2 degrees near the zenith with the
// seven-term model) fails rather than passes.
#define ROUND_TRIP_TOLERANCE 0.005
#define TERM_TOLERANCE 1e-5

void setUp(void) {
    pointingModel = PointingModel();
    settings.mountAltMin = 0.0f;
    settings.mountAltMax = 90.0f;
    settings.mountAzMin = 2.0f;
    settings.mountAzMax = 353.0f;
}

void tearDown(void) {}

// Azimuth is a circle: 0 and 360 are the same direction, and a round trip that
// lands a hair either side of due north must not read as a 360 degree error.
static void assertAzWithin(double tolerance, double expected, double actual,
                           unsigned line) {
    double diff = actual - expected;
    while (diff > 180.0) diff -= 360.0;
    while (diff < -180.0) diff += 360.0;
    UNITY_TEST_ASSERT_DOUBLE_WITHIN(tolerance, 0.0, diff, line, "azimuth differs");
}

// The scheduler's design matrix, transcribed from sun_scan.py, so that a change
// to either side of the wire shows up as a failing test here rather than as a
// telescope that points slightly wrong.
static void schedulerModel(double alt, double az, double IE, double IA,
                           double AN, double AE, double &dAlt, double &dAz) {
    const double a = az * DEG_TO_RAD, h = alt * DEG_TO_RAD;
    dAlt = IE + AN * cos(a) + AE * sin(a);
    dAz = IA + (AN * sin(a) - AE * cos(a)) * tan(h);
}

// =============================================================================
// Refraction
// =============================================================================

void test_refraction_is_zero_at_the_zenith() {
    TEST_ASSERT_DOUBLE_WITHIN(1e-6, 0.0, refractionDeg(90.0));
}

void test_refraction_grows_towards_the_horizon() {
    TEST_ASSERT_TRUE(refractionDeg(5.0) > refractionDeg(20.0));
    TEST_ASSERT_TRUE(refractionDeg(20.0) > refractionDeg(60.0));
}

void test_refraction_magnitude_at_low_elevation() {
    // Bennett scaled by 1.15 for radio: about 0.09 deg at 11 degrees altitude,
    // which is the elevation the 2026-08-12 eclipse run ended at.
    TEST_ASSERT_DOUBLE_WITHIN(0.01, 0.094, refractionDeg(11.0));
}

void test_refraction_stays_bounded_below_the_horizon() {
    // Bennett's formula diverges a few degrees under the horizon. Nothing
    // observes there, but a stow position or a clamped target can ask.
    TEST_ASSERT_TRUE(refractionDeg(-5.0) < 1.0);
    TEST_ASSERT_TRUE(std::isfinite(refractionDeg(-5.0)));
}

// =============================================================================
// The transform
// =============================================================================

void test_no_model_still_applies_refraction() {
    // "No model" means no calibration, not no physics.
    double driveAlt, driveAz;
    trueToDrive(30.0, 210.0, driveAlt, driveAz);
    TEST_ASSERT_DOUBLE_WITHIN(1e-9, 210.0, driveAz);
    TEST_ASSERT_DOUBLE_WITHIN(1e-9, 30.0 + refractionDeg(30.0), driveAlt);
}

void test_terms_match_the_scheduler_design_matrix() {
    pointingModel.loaded = true;
    pointingModel.IE = 0.32f;
    pointingModel.IA = -1.98f;
    pointingModel.AN = 0.41f;
    pointingModel.AE = -0.23f;

    for (double alt = 10.0; alt <= 70.0; alt += 15.0) {
        for (double az = 20.0; az < 350.0; az += 55.0) {
            const double apparent = alt + refractionDeg(alt);
            double expectedAlt, expectedAz;
            schedulerModel(apparent, az, pointingModel.IE, pointingModel.IA,
                           pointingModel.AN, pointingModel.AE, expectedAlt, expectedAz);
            double driveAlt, driveAz;
            trueToDrive(alt, az, driveAlt, driveAz);
            TEST_ASSERT_DOUBLE_WITHIN(TERM_TOLERANCE, apparent + expectedAlt, driveAlt);
            TEST_ASSERT_DOUBLE_WITHIN(TERM_TOLERANCE, az + expectedAz, driveAz);
        }
    }
}

void test_round_trip_four_term_model() {
    pointingModel.loaded = true;
    pointingModel.IE = 0.32f;
    pointingModel.IA = -1.98f;
    pointingModel.AN = 0.41f;
    pointingModel.AE = -0.23f;

    for (double alt = 10.0; alt <= 80.0; alt += 10.0) {
        for (double az = 5.0; az < 355.0; az += 25.0) {
            double driveAlt, driveAz, backAlt, backAz;
            trueToDrive(alt, az, driveAlt, driveAz);
            driveToTrue(driveAlt, driveAz, backAlt, backAz);
            TEST_ASSERT_DOUBLE_WITHIN(ROUND_TRIP_TOLERANCE, alt, backAlt);
            TEST_ASSERT_DOUBLE_WITHIN(ROUND_TRIP_TOLERANCE, az, backAz);
        }
    }
}

void test_round_trip_seven_term_model() {
    // CA and NPAE carry sec(alt) and tan(alt), so this is where a first-order
    // inverse falls apart near the zenith.
    pointingModel.loaded = true;
    pointingModel.IE = 0.32f;
    pointingModel.IA = -1.98f;
    pointingModel.AN = 0.41f;
    pointingModel.AE = -0.23f;
    pointingModel.CA = 0.15f;
    pointingModel.NPAE = -0.09f;
    pointingModel.TF = 0.07f;

    for (double alt = 10.0; alt <= 80.0; alt += 5.0) {
        for (double az = 5.0; az < 355.0; az += 11.0) {
            double driveAlt, driveAz, backAlt, backAz;
            trueToDrive(alt, az, driveAlt, driveAz);
            driveToTrue(driveAlt, driveAz, backAlt, backAz);
            TEST_ASSERT_DOUBLE_WITHIN(ROUND_TRIP_TOLERANCE, alt, backAlt);
            TEST_ASSERT_DOUBLE_WITHIN(ROUND_TRIP_TOLERANCE, az, backAz);
        }
    }
}

void test_round_trip_across_due_north() {
    // The azimuth residual in the inverse must be taken the short way round, or
    // a target a degree either side of north chases itself through 360.
    pointingModel.loaded = true;
    pointingModel.IA = 1.5f;
    pointingModel.AN = 0.4f;
    for (double az = 358.0; az <= 362.0; az += 1.0) {
        const double trueAz = az >= 360.0 ? az - 360.0 : az;
        double driveAlt, driveAz, backAlt, backAz;
        trueToDrive(40.0, trueAz, driveAlt, driveAz);
        driveToTrue(driveAlt, driveAz, backAlt, backAz);
        TEST_ASSERT_DOUBLE_WITHIN(ROUND_TRIP_TOLERANCE, 40.0, backAlt);
        assertAzWithin(ROUND_TRIP_TOLERANCE, trueAz, backAz, __LINE__);
    }
}

void test_a_wild_model_cannot_fling_the_mount() {
    pointingModel.loaded = true;
    pointingModel.IA = 9.0f;
    pointingModel.CA = 9.0f;
    pointingModel.NPAE = 9.0f;
    double driveAlt, driveAz;
    trueToDrive(89.9, 180.0, driveAlt, driveAz);
    TEST_ASSERT_TRUE(std::isfinite(driveAlt));
    TEST_ASSERT_TRUE(std::isfinite(driveAz));
    TEST_ASSERT_TRUE(fabs(driveAz - 180.0) <= 10.0 + 1e-6);
}

void test_drive_azimuth_is_normalised() {
    pointingModel.loaded = true;
    pointingModel.IA = -3.0f;
    double driveAlt, driveAz;
    trueToDrive(45.0, 1.0, driveAlt, driveAz);
    TEST_ASSERT_TRUE(driveAz >= 0.0 && driveAz < 360.0);
    TEST_ASSERT_DOUBLE_WITHIN(1e-6, 358.0, driveAz);
}

// =============================================================================
// Mount limits - drive frame only
// =============================================================================

void test_mount_limits_are_tested_in_the_drive_frame() {
    TEST_ASSERT_TRUE(driveAltWithinLimits(45.0));
    TEST_ASSERT_FALSE(driveAltWithinLimits(-1.0));
    TEST_ASSERT_FALSE(driveAltWithinLimits(91.0));
    TEST_ASSERT_TRUE(driveAzWithinLimits(180.0));
    TEST_ASSERT_FALSE(driveAzWithinLimits(0.0));
    TEST_ASSERT_FALSE(driveAzWithinLimits(359.0));
}

void test_clamp_moves_only_what_is_outside() {
    double alt = -5.0, az = 40.0;
    clampToMountLimits(alt, az);
    TEST_ASSERT_DOUBLE_WITHIN(1e-9, 0.0, alt);
    TEST_ASSERT_DOUBLE_WITHIN(1e-9, 40.0, az);

    alt = 45.0;
    az = 359.0;
    clampToMountLimits(alt, az);
    TEST_ASSERT_DOUBLE_WITHIN(1e-9, 45.0, alt);
    TEST_ASSERT_DOUBLE_WITHIN(1e-9, 353.0, az);
}

// =============================================================================
// The wire format
// =============================================================================

static const char *CANONICAL_MODEL =
    "{\"version\":1,\"fitted_utc\":\"2026-08-19T10:00:00Z\",\"n_scans\":32,"
    "\"frame\":\"cross_elevation\","
    "\"terms\":{\"IE\":0.3210,\"IA\":-1.9800,\"AN\":0.4100,\"AE\":-0.2300},"
    "\"site\":{\"lat\":55.902426,\"lon\":-4.307865},"
    "\"residual_rms_deg\":{\"alt\":0.04,\"xel\":0.05}}";

void test_canonical_document_is_accepted() {
    String error;
    TEST_ASSERT_TRUE(pointingApplyJson(String(CANONICAL_MODEL), error));
    TEST_ASSERT_TRUE(pointingModel.loaded);
    TEST_ASSERT_DOUBLE_WITHIN(1e-6, 0.321, pointingModel.IE);
    TEST_ASSERT_DOUBLE_WITHIN(1e-6, -1.98, pointingModel.IA);
    TEST_ASSERT_DOUBLE_WITHIN(1e-6, 0.41, pointingModel.AN);
    TEST_ASSERT_DOUBLE_WITHIN(1e-6, -0.23, pointingModel.AE);
    TEST_ASSERT_EQUAL_UINT32(32, pointingModel.nScans);
    TEST_ASSERT_EQUAL_STRING("2026-08-19T10:00:00Z", pointingModel.fittedUtc.c_str());
}

void test_absent_terms_default_to_zero() {
    String error;
    TEST_ASSERT_TRUE(pointingApplyJson(String(CANONICAL_MODEL), error));
    TEST_ASSERT_DOUBLE_WITHIN(1e-9, 0.0, pointingModel.CA);
    TEST_ASSERT_DOUBLE_WITHIN(1e-9, 0.0, pointingModel.NPAE);
    TEST_ASSERT_DOUBLE_WITHIN(1e-9, 0.0, pointingModel.TF);
}

void test_unknown_terms_are_ignored() {
    String error;
    TEST_ASSERT_TRUE(pointingApplyJson(String(
        "{\"version\":3,\"terms\":{\"IE\":0.1,\"IA\":0.2,\"AN\":0.3,\"AE\":0.4,"
        "\"CA\":0.5,\"NPAE\":0.6,\"TF\":0.7,\"SOMETHING_NEW\":9.9}}"), error));
    TEST_ASSERT_DOUBLE_WITHIN(1e-6, 0.7, pointingModel.TF);
    TEST_ASSERT_EQUAL_UINT32(3, pointingModel.version);
}

void test_a_rejected_document_leaves_the_model_untouched() {
    String error;
    TEST_ASSERT_TRUE(pointingApplyJson(String(CANONICAL_MODEL), error));
    const float before = pointingModel.IE;

    TEST_ASSERT_FALSE(pointingApplyJson(String("{\"terms\":{\"IE\":9.0}}"), error));
    TEST_ASSERT_FALSE(pointingApplyJson(String("{\"version\":1}"), error));
    TEST_ASSERT_FALSE(pointingApplyJson(
        String("{\"version\":1,\"terms\":{\"IE\":\"nonsense\"}}"), error));
    TEST_ASSERT_FALSE(pointingApplyJson(
        String("{\"version\":1,\"terms\":{\"IE\":45.0}}"), error));
    TEST_ASSERT_FALSE(pointingApplyJson(
        String("{\"version\":1,\"terms\":{\"NOT_A_TERM\":1.0}}"), error));

    TEST_ASSERT_DOUBLE_WITHIN(1e-9, before, pointingModel.IE);
}

void test_a_truncated_document_is_rejected() {
    // A truncated model must not parse as a valid smaller one - that is the
    // whole reason /pointing/apply refuses an oversized body outright.
    String error;
    TEST_ASSERT_FALSE(pointingApplyJson(
        String("{\"version\":1,\"terms\":{\"IE\":0.3,\"IA\":-1.9"), error));
}

void test_serialised_model_round_trips() {
    String error;
    TEST_ASSERT_TRUE(pointingApplyJson(String(CANONICAL_MODEL), error));
    const String emitted = pointingToJson();
    PointingModel saved = pointingModel;

    pointingModel = PointingModel();
    TEST_ASSERT_TRUE(pointingApplyJson(emitted, error));
    TEST_ASSERT_DOUBLE_WITHIN(1e-5, saved.IE, pointingModel.IE);
    TEST_ASSERT_DOUBLE_WITHIN(1e-5, saved.IA, pointingModel.IA);
    TEST_ASSERT_DOUBLE_WITHIN(1e-5, saved.AN, pointingModel.AN);
    TEST_ASSERT_DOUBLE_WITHIN(1e-5, saved.AE, pointingModel.AE);
    TEST_ASSERT_EQUAL_UINT32(saved.nScans, pointingModel.nScans);
}

void test_a_cleared_model_is_the_identity_transform() {
    String error;
    TEST_ASSERT_TRUE(pointingApplyJson(String(CANONICAL_MODEL), error));
    pointingClear();
    TEST_ASSERT_FALSE(pointingModel.loaded);

    double driveAlt, driveAz;
    trueToDrive(35.0, 120.0, driveAlt, driveAz);
    TEST_ASSERT_DOUBLE_WITHIN(1e-9, 120.0, driveAz);
    TEST_ASSERT_DOUBLE_WITHIN(1e-9, 35.0 + refractionDeg(35.0), driveAlt);
}

int main(int argc, char **argv) {
    UNITY_BEGIN();

    RUN_TEST(test_refraction_is_zero_at_the_zenith);
    RUN_TEST(test_refraction_grows_towards_the_horizon);
    RUN_TEST(test_refraction_magnitude_at_low_elevation);
    RUN_TEST(test_refraction_stays_bounded_below_the_horizon);

    RUN_TEST(test_no_model_still_applies_refraction);
    RUN_TEST(test_terms_match_the_scheduler_design_matrix);
    RUN_TEST(test_round_trip_four_term_model);
    RUN_TEST(test_round_trip_seven_term_model);
    RUN_TEST(test_round_trip_across_due_north);
    RUN_TEST(test_a_wild_model_cannot_fling_the_mount);
    RUN_TEST(test_drive_azimuth_is_normalised);

    RUN_TEST(test_mount_limits_are_tested_in_the_drive_frame);
    RUN_TEST(test_clamp_moves_only_what_is_outside);

    RUN_TEST(test_canonical_document_is_accepted);
    RUN_TEST(test_absent_terms_default_to_zero);
    RUN_TEST(test_unknown_terms_are_ignored);
    RUN_TEST(test_a_rejected_document_leaves_the_model_untouched);
    RUN_TEST(test_a_truncated_document_is_rejected);
    RUN_TEST(test_serialised_model_round_trips);
    RUN_TEST(test_a_cleared_model_is_the_identity_transform);

    return UNITY_END();
}
