// pointing.cpp - Resident pointing calibration and the drive/true frame transform
//
// See pointing.h for the frame diagram and for why the calibration lives here
// rather than in the observer position and the operator's offset boxes.

#include "pointing.h"
#include "settings.h"
#include <Preferences.h>
#include <math.h>

PointingModel pointingModel;

// NVS namespace. Separate from "settings" on purpose: the calibration is not a
// preference, it is a measurement, and "Reset to defaults" in the settings tab
// must not silently discard it.
static const char *POINTING_NVS_NS = "pointing";

// Largest correction any single axis may be asked for. The calibration is a
// small correction by definition - the whole measured error on this mount is
// around two degrees - so anything past this is a broken or mis-scaled model
// rather than a pointing term, and must not be allowed to fling the mount at a
// limit switch. Clamping is silent by design: the value is still reported by
// /pointing, so a model that hits the clamp is visible without a slew being
// commanded from it.
static const double POINTING_MAX_CORRECTION_DEG = 10.0;
// An azimuth scale error of 5% would be 17 deg across the mount's range. A real
// one is tenths of a percent; anything past this is a fault, not a calibration.
static const double POINTING_MAX_AZSCALE = 0.05;

// tan(alt) and 1/cos(alt) diverge at the zenith, where azimuth is degenerate
// anyway. Clamp the altitude those two terms see: at 89 deg tan(alt) is already
// 57, far past anything a real mount term needs, and the total is clamped again
// afterwards.
static const double POINTING_MAX_TAN_ALT_DEG = 89.0;

static double normaliseAz(double az) {
    while (az < 0.0) az += 360.0;
    while (az >= 360.0) az -= 360.0;
    return az;
}

static double clampCorrection(double d) {
    if (!isfinite(d)) return 0.0;
    if (d > POINTING_MAX_CORRECTION_DEG) return POINTING_MAX_CORRECTION_DEG;
    if (d < -POINTING_MAX_CORRECTION_DEG) return -POINTING_MAX_CORRECTION_DEG;
    return d;
}

// The correction to ADD to a position to move it from the true frame into the
// drive frame, evaluated at the position given. Both transforms below share it:
// the forward one evaluates it at the true position, the inverse at the drive
// position and negates it.
static void modelCorrection(double altDeg, double azDeg, double &dAlt, double &dAz) {
    dAlt = 0.0;
    dAz = 0.0;
    if (!pointingModel.loaded) return;

    const double az = azDeg * DEG_TO_RAD;
    double altForTan = altDeg;
    if (altForTan > POINTING_MAX_TAN_ALT_DEG) altForTan = POINTING_MAX_TAN_ALT_DEG;
    if (altForTan < -POINTING_MAX_TAN_ALT_DEG) altForTan = -POINTING_MAX_TAN_ALT_DEG;
    const double alt = altForTan * DEG_TO_RAD;

    const double sinAz = sin(az), cosAz = cos(az);
    const double tanAlt = tan(alt), cosAlt = cos(alt);
    const double secAlt = (fabs(cosAlt) > 1e-6) ? (1.0 / cosAlt) : 0.0;

    dAlt = pointingModel.IE
         + pointingModel.AN * cosAz
         + pointingModel.AE * sinAz
         - pointingModel.TF * cos(altDeg * DEG_TO_RAD);

    dAz = pointingModel.IA
        + (pointingModel.AN * sinAz - pointingModel.AE * cosAz) * tanAlt
        + pointingModel.CA * secAlt
        + pointingModel.NPAE * tanAlt
        // Scale error accumulating from the encoder zero. azDeg is the true
        // azimuth here rather than the drive azimuth the encoder actually
        // reads; they differ by the correction itself, so using it costs
        // AZSCALE times about a degree - under 0.005 deg, and driveToTrue
        // inverts whatever this does by fixed point regardless.
        + pointingModel.AZSCALE * azDeg;

    dAlt = clampCorrection(dAlt);
    dAz = clampCorrection(dAz);
}

// Refraction is physics, not calibration, so it is applied whether or not a
// model is loaded. The scheduler must therefore remove it from a scan's
// measured error before fitting, or the fitted terms would absorb it and it
// would then be counted twice.
double refractionDeg(double trueAltDeg) {
    double h = trueAltDeg;
    if (h < -1.0) h = -1.0;   // Bennett diverges below this; nothing observes there
    if (h > 90.0) h = 90.0;
    const double arg = (h + 7.31 / (h + 4.4)) * DEG_TO_RAD;
    const double t = tan(arg);
    // Past the zenith the argument exceeds 90 deg and the tangent turns
    // negative. Refraction is zero there, not negative.
    if (!(t > 0.0)) return 0.0;
    return 1.15 / (60.0 * t);
}

void trueToDrive(double trueAlt, double trueAz, double &driveAlt, double &driveAz) {
    // The mount must point where the source appears, not where it geometrically
    // is, so refraction is added before the model terms are evaluated.
    const double apparentAlt = trueAlt + refractionDeg(trueAlt);
    double dAlt, dAz;
    modelCorrection(apparentAlt, trueAz, dAlt, dAz);
    driveAlt = apparentAlt + dAlt;
    driveAz = normaliseAz(trueAz + dAz);
}

void driveToTrue(double driveAlt, double driveAz, double &trueAlt, double &trueAz) {
    // Inverted by fixed point on trueToDrive() itself, so the two directions
    // cannot drift apart: whatever the forward transform does, this undoes
    // exactly that.
    //
    // Simply negating the correction evaluated at the drive position is only
    // first order. That is fine for the four-term model, but with CA and NPAE
    // present the azimuth correction carries sec(alt) and tan(alt), and near
    // the zenith the first-order answer is out by around a quarter of a degree
    // - half the encoder quantum, which is not a rounding error. Three passes
    // bring it to well under a thousandth of a degree everywhere the mount can
    // point; the correction is small and slowly varying, so it converges fast.
    double tAlt = driveAlt, tAz = driveAz;
    for (int i = 0; i < 3; i++) {
        double fAlt, fAz;
        trueToDrive(tAlt, tAz, fAlt, fAz);
        // Azimuth residual taken the short way round, so a guess either side of
        // due north does not chase itself through 360 degrees.
        double residualAz = driveAz - fAz;
        while (residualAz > 180.0) residualAz -= 360.0;
        while (residualAz < -180.0) residualAz += 360.0;
        tAlt += driveAlt - fAlt;
        tAz += residualAz;
    }
    trueAlt = tAlt;
    trueAz = normaliseAz(tAz);
}

// -----------------------------------------------------------------------------
// Mount limits
// -----------------------------------------------------------------------------

bool driveAltWithinLimits(double driveAlt) {
    return driveAlt >= settings.mountAltMin && driveAlt <= settings.mountAltMax;
}

bool driveAzWithinLimits(double driveAz) {
    return driveAz >= settings.mountAzMin && driveAz <= settings.mountAzMax;
}

void clampToMountLimits(double &driveAlt, double &driveAz) {
    if (driveAlt < settings.mountAltMin) driveAlt = settings.mountAltMin;
    if (driveAlt > settings.mountAltMax) driveAlt = settings.mountAltMax;
    if (driveAz < settings.mountAzMin) driveAz = settings.mountAzMin;
    if (driveAz > settings.mountAzMax) driveAz = settings.mountAzMax;
}

// -----------------------------------------------------------------------------
// Persistence
// -----------------------------------------------------------------------------

void pointingLoad() {
    Preferences prefs;
    if (!prefs.begin(POINTING_NVS_NS, true)) {
        Serial.println("Pointing: no stored calibration (identity transform)");
        return;
    }
    if (!prefs.isKey("ver")) {
        prefs.end();
        Serial.println("Pointing: no stored calibration (identity transform)");
        return;
    }

    pointingModel.version = prefs.getUInt("ver", 0);
    pointingModel.IE = prefs.getFloat("IE", 0.0f);
    pointingModel.IA = prefs.getFloat("IA", 0.0f);
    pointingModel.AN = prefs.getFloat("AN", 0.0f);
    pointingModel.AE = prefs.getFloat("AE", 0.0f);
    pointingModel.CA = prefs.getFloat("CA", 0.0f);
    pointingModel.NPAE = prefs.getFloat("NPAE", 0.0f);
    pointingModel.TF = prefs.getFloat("TF", 0.0f);
    pointingModel.AZSCALE = prefs.getFloat("AZSCALE", 0.0f);
    pointingModel.nScans = prefs.getUInt("nScans", 0);
    pointingModel.fittedUtc = prefs.getString("utc", "");
    pointingModel.loaded = true;
    prefs.end();

    Serial.printf("Pointing model loaded: IE=%+.4f IA=%+.4f AN=%+.4f AE=%+.4f "
                  "CA=%+.4f NPAE=%+.4f TF=%+.4f AZSCALE=%+.5f (%u scans, %s)\n",
                  pointingModel.IE, pointingModel.IA, pointingModel.AN,
                  pointingModel.AE, pointingModel.CA, pointingModel.NPAE,
                  pointingModel.TF, pointingModel.AZSCALE,
                  (unsigned)pointingModel.nScans,
                  pointingModel.fittedUtc.length() ? pointingModel.fittedUtc.c_str()
                                                   : "no date");
}

void pointingSave() {
    Preferences prefs;
    if (!prefs.begin(POINTING_NVS_NS, false)) {
        Serial.println("Pointing: NVS open failed, model not persisted");
        return;
    }
    prefs.putUInt("ver", pointingModel.version);
    prefs.putFloat("IE", pointingModel.IE);
    prefs.putFloat("IA", pointingModel.IA);
    prefs.putFloat("AN", pointingModel.AN);
    prefs.putFloat("AE", pointingModel.AE);
    prefs.putFloat("CA", pointingModel.CA);
    prefs.putFloat("NPAE", pointingModel.NPAE);
    prefs.putFloat("TF", pointingModel.TF);
    prefs.putFloat("AZSCALE", pointingModel.AZSCALE);
    prefs.putUInt("nScans", pointingModel.nScans);
    prefs.putString("utc", pointingModel.fittedUtc);
    prefs.end();
    Serial.println("Pointing model saved to NVS");
}

void pointingEraseStored() {
    Preferences prefs;
    if (prefs.begin(POINTING_NVS_NS, false)) {
        prefs.clear();
        prefs.end();
    }
    Serial.println("Stored pointing model erased");
}

void pointingClear() {
    pointingModel = PointingModel();
    pointingEraseStored();
}

// -----------------------------------------------------------------------------
// JSON
// -----------------------------------------------------------------------------
//
// There is no JSON library in this build and the schema is small and fixed, so
// a scanner is cheaper than a dependency. It is strict about numbers - a
// malformed one rejects the whole document rather than applying a partial
// model - and tolerant about everything else, so a later, richer model can be
// pushed to this firmware without bricking pointing.

// Index just past the colon that follows "key", or -1. Searched within
// [from, to) so term names can be confined to the "terms" object and cannot
// match a like-named key elsewhere in the document.
static int keyValuePos(const String &s, const char *key, int from, int to) {
    String quoted = String("\"") + key + "\"";
    int at = s.indexOf(quoted, from);
    if (at < 0 || at >= to) return -1;
    int i = at + quoted.length();
    while (i < to && isspace((unsigned char)s.charAt(i))) i++;
    if (i >= to || s.charAt(i) != ':') return -1;
    return i + 1;
}

// PARSE_MISSING is not a failure: a term the fit did not produce stays at its
// default of zero. Only PARSE_BAD rejects the document.
enum ParseResult { PARSE_OK, PARSE_MISSING, PARSE_BAD };

static ParseResult parseNumber(const String &s, const char *key, int from, int to,
                               double &out) {
    int pos = keyValuePos(s, key, from, to);
    if (pos < 0) return PARSE_MISSING;
    const char *start = s.c_str() + pos;
    char *end = nullptr;
    double value = strtod(start, &end);
    if (end == start || !isfinite(value)) return PARSE_BAD;
    out = value;
    return PARSE_OK;
}

static ParseResult parseString(const String &s, const char *key, int from, int to,
                               String &out) {
    int pos = keyValuePos(s, key, from, to);
    if (pos < 0) return PARSE_MISSING;
    while (pos < to && isspace((unsigned char)s.charAt(pos))) pos++;
    if (pos >= to || s.charAt(pos) != '"') return PARSE_BAD;
    int close = s.indexOf('"', pos + 1);
    if (close < 0 || close >= to) return PARSE_BAD;
    out = s.substring(pos + 1, close);
    return PARSE_OK;
}

// Span of the object value of "key", from the opening brace to just past the
// matching close. Brace-matched rather than searched to the next '}', so a
// nested object inside terms would not truncate the span.
static bool objectSpan(const String &s, const char *key, int &start, int &end) {
    int pos = keyValuePos(s, key, 0, s.length());
    if (pos < 0) return false;
    while (pos < (int)s.length() && isspace((unsigned char)s.charAt(pos))) pos++;
    if (pos >= (int)s.length() || s.charAt(pos) != '{') return false;
    int depth = 0;
    for (int i = pos; i < (int)s.length(); i++) {
        char c = s.charAt(i);
        if (c == '{') depth++;
        else if (c == '}') {
            depth--;
            if (depth == 0) {
                start = pos;
                end = i + 1;
                return true;
            }
        }
    }
    return false;
}

// The fitted date is echoed straight back out by pointingToJson(), which does
// no escaping, so anything that could break the JSON it lands in is dropped
// here rather than at every point of use. It is a display label, not data.
static String sanitiseLabel(const String &in) {
    String out;
    for (size_t i = 0; i < in.length() && out.length() < 32; i++) {
        char c = in.charAt(i);
        if (c >= 0x20 && c < 0x7f && c != '"' && c != '\\') out += c;
    }
    return out;
}

bool pointingApplyJson(const String &json, String &errorOut) {
    double version = 0.0;
    if (parseNumber(json, "version", 0, json.length(), version) != PARSE_OK) {
        errorOut = "Missing or malformed \"version\"";
        return false;
    }
    if (version < 1.0) {
        errorOut = "Unsupported model version";
        return false;
    }

    int termsStart = 0, termsEnd = 0;
    if (!objectSpan(json, "terms", termsStart, termsEnd)) {
        errorOut = "Missing \"terms\" object";
        return false;
    }

    // Built into a scratch model so a malformed term late in the document
    // cannot leave the live one half-replaced.
    PointingModel next;
    // Each term carries its own plausible range. AZSCALE is why: it is a ratio,
    // not an angle, so the degree limit that guards the others would wave
    // through a scale error of 10, which is not a mount but a broken encoder.
    struct { const char *name; float *slot; double limit; const char *unit; } terms[] = {
        {"IE",   &next.IE,   POINTING_MAX_CORRECTION_DEG, "deg"},
        {"IA",   &next.IA,   POINTING_MAX_CORRECTION_DEG, "deg"},
        {"AN",   &next.AN,   POINTING_MAX_CORRECTION_DEG, "deg"},
        {"AE",   &next.AE,   POINTING_MAX_CORRECTION_DEG, "deg"},
        {"CA",   &next.CA,   POINTING_MAX_CORRECTION_DEG, "deg"},
        {"NPAE", &next.NPAE, POINTING_MAX_CORRECTION_DEG, "deg"},
        {"TF",   &next.TF,   POINTING_MAX_CORRECTION_DEG, "deg"},
        {"AZSCALE", &next.AZSCALE, POINTING_MAX_AZSCALE, "deg/deg"},
    };
    int found = 0;
    for (auto &term : terms) {
        double value = 0.0;
        ParseResult r = parseNumber(json, term.name, termsStart, termsEnd, value);
        if (r == PARSE_BAD) {
            errorOut = String("Malformed value for term ") + term.name;
            return false;
        }
        if (r == PARSE_MISSING) continue;
        if (fabs(value) > term.limit) {
            errorOut = String("Term ") + term.name + " is " + String(value, 5) +
                       " " + term.unit +
                       ", outside the plausible range for a pointing term";
            return false;
        }
        *term.slot = (float)value;
        found++;
    }
    if (found == 0) {
        errorOut = "No recognised terms in \"terms\"";
        return false;
    }

    double nScans = 0.0;
    if (parseNumber(json, "n_scans", 0, json.length(), nScans) == PARSE_OK &&
        nScans >= 0.0 && nScans < 4.0e9) {
        next.nScans = (uint32_t)nScans;
    }
    String utc;
    if (parseString(json, "fitted_utc", 0, json.length(), utc) == PARSE_OK) {
        next.fittedUtc = sanitiseLabel(utc);
    }
    next.version = (uint32_t)version;
    next.loaded = true;

    pointingModel = next;
    errorOut = "";
    return true;
}

String pointingToJson() {
    char buf[512];
    snprintf(buf, sizeof(buf),
        "{\"loaded\":%s,\"version\":%u,\"n_scans\":%u,\"fitted_utc\":\"%s\","
        "\"terms\":{\"IE\":%.5f,\"IA\":%.5f,\"AN\":%.5f,\"AE\":%.5f,"
        "\"CA\":%.5f,\"NPAE\":%.5f,\"TF\":%.5f,\"AZSCALE\":%.6f}}",
        pointingModel.loaded ? "true" : "false",
        (unsigned)pointingModel.version, (unsigned)pointingModel.nScans,
        pointingModel.fittedUtc.c_str(),
        pointingModel.IE, pointingModel.IA, pointingModel.AN, pointingModel.AE,
        pointingModel.CA, pointingModel.NPAE, pointingModel.TF,
        pointingModel.AZSCALE);
    return String(buf);
}
