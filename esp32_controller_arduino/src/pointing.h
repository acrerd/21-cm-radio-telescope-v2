// pointing.h - Resident pointing calibration and the drive/true frame transform
//
// Two coordinate frames meet here, and the boundary is this file:
//
//   True alt/az   - where the dish is actually looking on the sky. Everything
//                   astronomical is computed in this frame, using the TRUE site
//                   latitude and longitude.
//   Drive         - what the mount mechanically is. Encoder pulses from the
//                   limit switches, quantised to 0.5 degrees. The Due works
//                   only in this frame, and that does not change.
//
//   true alt/az --[ trueToDrive ]--> drive --> Due --.
//                      ^                             |
//               requested drive <-- compared -- reported drive
//                      |
//               [ driveToTrue ] --> true alt/az   (display, RA/Dec, Stellarium)
//
// Arrival and slew-completion checks compare requested drive against reported
// drive, so they stay wholly inside the drive frame and the two never mix.
//
// The calibration is held here and in NVS. It used to be smuggled to the
// controller as a fictitious observer latitude and longitude plus a push to the
// operator's pointing-offset boxes. That had two failures: the web UI reported
// a location the telescope is not at, and the offset half lived in RAM only, so
// every reboot silently discarded half the model. Measured on 2026-08-19, the
// azimuth error that reappeared after a power cycle was 1.97 degrees against a
// stored az offset of 1.98 - about a quarter of the signal, lost silently.

#ifndef POINTING_H
#define POINTING_H

#include <Arduino.h>

// Term names follow the usual alt-az pointing-model convention, and the first
// four match the design matrix the scheduler already fits:
//
//   dAlt = IE + AN*cos(az) + AE*sin(az) - TF*cos(alt)
//   dAz  = IA + (AN*sin(az) - AE*cos(az))*tan(alt) + CA/cos(alt) + NPAE*tan(alt)
//
// where dAlt/dAz are added to a TRUE position to obtain the DRIVE position.
//
// All seven slots always exist and default to zero, so a four-term model is
// simply one with CA, NPAE and TF left at zero. That way a later fit can add
// terms without any change to the storage, the transform or the wire format.
struct PointingModel {
    bool  loaded = false;      // false means identity - never guess a transform
    float IE = 0.0f;           // elevation index error (deg)
    float IA = 0.0f;           // azimuth index error (deg)
    float AN = 0.0f;           // azimuth axis tilt, north (deg)
    float AE = 0.0f;           // azimuth axis tilt, east (deg)
    float CA = 0.0f;           // collimation error (deg)
    float NPAE = 0.0f;         // az/alt axis non-perpendicularity (deg)
    float TF = 0.0f;           // tube flexure (deg)
    uint32_t version = 0;      // schema version of the loaded model
    uint32_t nScans = 0;       // scans the fit was derived from
    String fittedUtc = "";     // when it was fitted, for display
};

extern PointingModel pointingModel;

// Load from NVS at boot. Absent or malformed leaves the identity transform.
void pointingLoad();

// Persist the current model to NVS.
void pointingSave();

// Erase the stored model from NVS, leaving the in-RAM one alone. Separate from
// pointingClear() because it is a flash erase of tens of milliseconds and must
// not happen under the cross-task lock; a caller that holds the lock zeroes the
// struct itself and calls this after releasing it.
void pointingEraseStored();

// Zero every term and erase the NVS keys. "No model stored" and "all terms
// zero" behave identically, so a cleared model is the identity transform.
void pointingClear();

// Replace the model from a JSON document (the schema the scheduler writes).
// Unknown term names are ignored so a later, richer model can be pushed to
// older firmware without bricking pointing. Returns false and leaves the
// current model untouched if version is missing or a number is malformed -
// a partial model is worse than none.
bool pointingApplyJson(const String &json, String &errorOut);

// Serialise the current model, for the GUI and for /pointing.
String pointingToJson();

// True sky position -> drive position. Refraction is applied here too, so a
// caller passes the geometric position of the source and gets the mount
// coordinates that put the beam on it.
void trueToDrive(double trueAlt, double trueAz, double &driveAlt, double &driveAz);

// Drive position -> true sky position. Used wherever a MEASURED position is
// displayed or converted - /status RA/Dec, galactic l/b, Stellarium. Evaluating
// the correction at the drive coordinates and negating it is accurate to second
// order, which is far below the 0.5 degree encoder quantum for corrections of
// this size.
void driveToTrue(double driveAlt, double driveAz, double &trueAlt, double &trueAz);

// Mount limit helpers. These are DRIVE-frame quantities - the mechanical stops
// the mount actually has - so they are only ever applied to a position that has
// already been through trueToDrive(). The local observing horizon is the
// separate, TRUE-frame test in settings.horizonAlt; do not conflate them.
bool driveAltWithinLimits(double driveAlt);
bool driveAzWithinLimits(double driveAz);
void clampToMountLimits(double &driveAlt, double &driveAz);

// Radio refraction, in degrees, for a geometric altitude. Bennett's formula
// scaled by 1.15: radio refractivity is about 315 N-units against 273 optical,
// the difference being the water-vapour wet term. Nominal atmosphere - there is
// no weather station, and the dry/wet swing is well under the residual of a
// single calibration scan.
double refractionDeg(double trueAltDeg);

#endif // POINTING_H
