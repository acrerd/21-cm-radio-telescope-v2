// coordinates.cpp - Astronomical coordinate transformations

#include "coordinates.h"
#include "config.h"
#include <Arduino.h>
#include <math.h>
#include <time.h>

// DEG_TO_RAD and RAD_TO_DEG are defined in Arduino.h.

double julianDate(int year, int month, int day, int hour, int minute, int second) {
    if (month <= 2) {
        year -= 1;
        month += 12;
    }

    int A = year / 100;
    int B = 2 - A + A / 4;

    double jdBase = (int)(365.25 * (year + 4716)) + (int)(30.6001 * (month + 1)) + day + B - 1524.5;
    double timeFrac = ((double)hour + (double)minute / 60.0 + (double)second / 3600.0) / 24.0;

    return jdBase + timeFrac;
}

double gmst(double jd, double hoursUT) {
    // Calculate JD at 0h UT
    double jd0 = floor(jd - 0.5) + 0.5;

    // Hours since 0h UT
    double H = (hoursUT >= 0) ? hoursUT : (jd - jd0) * 24.0;

    // Days since J2000.0 at 0h UT
    double D0 = jd0 - 2451545.0;
    double T = D0 / 36525.0;

    // GMST at 0h UT in hours (IAU 1982 formula)
    double gmst0 = 6.697374558 + 0.06570982441908 * D0 + 1.00273790935 * H + 0.000026 * T * T;

    // Normalize to 0-24 hours
    gmst0 = fmod(gmst0, 24.0);
    if (gmst0 < 0) gmst0 += 24.0;

    return gmst0;
}

double localSiderealTime(double jd, double longitude, double hoursUT) {
    double lst = gmst(jd, hoursUT) + longitude / 15.0;
    lst = fmod(lst, 24.0);
    if (lst < 0) lst += 24.0;
    return lst;
}

void precessJ2000ToDate(double raHours, double decDeg, double jd, double &raOut, double &decOut) {
    // Julian centuries from J2000.0
    double T = (jd - 2451545.0) / 36525.0;

    // Precession angles in arcseconds (IAU 1976)
    double zetaA = (2306.2181 + 1.39656 * T - 0.000139 * T * T) * T +
                   (0.30188 - 0.000344 * T) * T * T + 0.017998 * T * T * T;
    double zA = (2306.2181 + 1.39656 * T - 0.000139 * T * T) * T +
                (1.09468 + 0.000066 * T) * T * T + 0.018203 * T * T * T;
    double thetaA = (2004.3109 - 0.85330 * T - 0.000217 * T * T) * T -
                    (0.42665 + 0.000217 * T) * T * T - 0.041833 * T * T * T;

    // Convert to radians
    double zeta = zetaA / 3600.0 * DEG_TO_RAD;
    double z = zA / 3600.0 * DEG_TO_RAD;
    double theta = thetaA / 3600.0 * DEG_TO_RAD;

    // Original coordinates in radians
    double ra0 = raHours * 15.0 * DEG_TO_RAD;
    double dec0 = decDeg * DEG_TO_RAD;

    // Apply precession rotation
    double A = cos(dec0) * sin(ra0 + zeta);
    double B = cos(theta) * cos(dec0) * cos(ra0 + zeta) - sin(theta) * sin(dec0);
    double C = sin(theta) * cos(dec0) * cos(ra0 + zeta) + cos(theta) * sin(dec0);

    // New coordinates
    double raRad = atan2(A, B) + z;
    double decRad = asin(C);

    // Convert back to hours and degrees
    raOut = fmod((raRad * RAD_TO_DEG) / 15.0, 24.0);
    if (raOut < 0) raOut += 24.0;
    decOut = decRad * RAD_TO_DEG;
}

void precessDateToJ2000(double raHours, double decDeg, double jd, double &raOut, double &decOut) {
    // Julian centuries from J2000.0
    double T = (jd - 2451545.0) / 36525.0;

    // Precession angles in arcseconds (IAU 1976)
    double zetaA = (2306.2181 + 1.39656 * T - 0.000139 * T * T) * T +
                   (0.30188 - 0.000344 * T) * T * T + 0.017998 * T * T * T;
    double zA = (2306.2181 + 1.39656 * T - 0.000139 * T * T) * T +
                (1.09468 + 0.000066 * T) * T * T + 0.018203 * T * T * T;
    double thetaA = (2004.3109 - 0.85330 * T - 0.000217 * T * T) * T -
                    (0.42665 + 0.000217 * T) * T * T - 0.041833 * T * T * T;

    // Convert to radians
    double zeta = zetaA / 3600.0 * DEG_TO_RAD;
    double z = zA / 3600.0 * DEG_TO_RAD;
    double theta = thetaA / 3600.0 * DEG_TO_RAD;

    // Current coordinates in radians
    double ra = raHours * 15.0 * DEG_TO_RAD;
    double dec = decDeg * DEG_TO_RAD;

    // Apply inverse precession rotation
    double A = cos(dec) * sin(ra - z);
    double B = cos(theta) * cos(dec) * cos(ra - z) + sin(theta) * sin(dec);
    double C = -sin(theta) * cos(dec) * cos(ra - z) + cos(theta) * sin(dec);

    // J2000 coordinates
    double ra0Rad = atan2(A, B) - zeta;
    double dec0Rad = asin(C);

    // Convert back to hours and degrees
    raOut = fmod((ra0Rad * RAD_TO_DEG) / 15.0, 24.0);
    if (raOut < 0) raOut += 24.0;
    decOut = dec0Rad * RAD_TO_DEG;
}

void raDecToAltAz(double raHours, double decDeg, double latDeg, double lonDeg, double &altOut, double &azOut) {
    // Get current time
    time_t now = time(nullptr);
    struct tm *t = gmtime(&now);

    // Compute hours since midnight with full precision
    double hoursUT = (double)t->tm_hour + (double)t->tm_min / 60.0 + (double)t->tm_sec / 3600.0;

    double jd = julianDate(t->tm_year + 1900, t->tm_mon + 1, t->tm_mday,
                           t->tm_hour, t->tm_min, t->tm_sec);

    // Precess from J2000 to current date
    double raNow, decNow;
    precessJ2000ToDate(raHours, decDeg, jd, raNow, decNow);

    // Calculate hour angle
    double lst = localSiderealTime(jd, lonDeg, hoursUT);
    double haHours = lst - raNow;
    double haRad = haHours * 15.0 * DEG_TO_RAD;

    // Convert to radians
    double decRad = decNow * DEG_TO_RAD;
    double latRad = latDeg * DEG_TO_RAD;

    // Calculate altitude
    double sinAlt = sin(decRad) * sin(latRad) + cos(decRad) * cos(latRad) * cos(haRad);
    double altRad = asin(sinAlt);

    // Calculate azimuth
    double cosAz = (sin(decRad) - sin(altRad) * sin(latRad)) / (cos(altRad) * cos(latRad));

    // Clamp to valid range
    if (cosAz > 1.0) cosAz = 1.0;
    if (cosAz < -1.0) cosAz = -1.0;
    double azRad = acos(cosAz);

    // Adjust azimuth quadrant
    if (sin(haRad) > 0) {
        azRad = 2.0 * M_PI - azRad;
    }

    altOut = altRad * RAD_TO_DEG;
    azOut = azRad * RAD_TO_DEG;
}

void altAzToRaDec(double altDeg, double azDeg, double latDeg, double lonDeg, double &raOut, double &decOut) {
    // Convert to radians
    double altRad = altDeg * DEG_TO_RAD;
    double azRad = azDeg * DEG_TO_RAD;
    double latRad = latDeg * DEG_TO_RAD;

    // Calculate declination
    double sinDec = sin(altRad) * sin(latRad) + cos(altRad) * cos(latRad) * cos(azRad);
    double decRad = asin(sinDec);

    // Calculate hour angle
    double cosHA = (sin(altRad) - sin(latRad) * sin(decRad)) / (cos(latRad) * cos(decRad));
    if (cosHA > 1.0) cosHA = 1.0;
    if (cosHA < -1.0) cosHA = -1.0;
    double haRad = acos(cosHA);

    if (sin(azRad) > 0) {
        haRad = 2.0 * M_PI - haRad;
    }

    // Get current LST and calculate RA
    time_t now = time(nullptr);
    struct tm *t = gmtime(&now);
    double hoursUT = (double)t->tm_hour + (double)t->tm_min / 60.0 + (double)t->tm_sec / 3600.0;
    double jd = julianDate(t->tm_year + 1900, t->tm_mon + 1, t->tm_mday,
                           t->tm_hour, t->tm_min, t->tm_sec);
    double lst = localSiderealTime(jd, lonDeg, hoursUT);

    double raNow = lst - (haRad * RAD_TO_DEG) / 15.0;
    raNow = fmod(raNow, 24.0);
    if (raNow < 0) raNow += 24.0;
    double decNow = decRad * RAD_TO_DEG;

    // Precess back to J2000
    precessDateToJ2000(raNow, decNow, jd, raOut, decOut);
}

void getSunPosition(double &raOut, double &decOut) {
    time_t now = time(nullptr);
    struct tm *t = gmtime(&now);
    double jd = julianDate(t->tm_year + 1900, t->tm_mon + 1, t->tm_mday,
                           t->tm_hour, t->tm_min, t->tm_sec);

    // Julian centuries since J2000.0
    double T = (jd - 2451545.0) / 36525.0;

    // Geometric mean longitude of the Sun
    double L0 = fmod(280.46646 + 36000.76983 * T + 0.0003032 * T * T, 360.0);
    if (L0 < 0) L0 += 360.0;

    // Mean anomaly of the Sun
    double M = fmod(357.52911 + 35999.05029 * T - 0.0001537 * T * T, 360.0);
    if (M < 0) M += 360.0;
    double Mrad = M * DEG_TO_RAD;

    // Equation of center
    double C = (1.914602 - 0.004817 * T - 0.000014 * T * T) * sin(Mrad) +
               (0.019993 - 0.000101 * T) * sin(2 * Mrad) +
               0.000289 * sin(3 * Mrad);

    // Sun's true longitude
    double sunLon = L0 + C;

    // Apparent longitude
    double omega = 125.04 - 1934.136 * T;
    double sunLonApparent = sunLon - 0.00569 - 0.00478 * sin(omega * DEG_TO_RAD);
    double sunLonRad = sunLonApparent * DEG_TO_RAD;

    // Mean obliquity of the ecliptic
    double eps0 = 23.439291 - 0.0130042 * T - 0.00000016 * T * T + 0.000000504 * T * T * T;
    double eps = eps0 + 0.00256 * cos(omega * DEG_TO_RAD);
    double epsRad = eps * DEG_TO_RAD;

    // Convert ecliptic to equatorial
    double raRad = atan2(cos(epsRad) * sin(sunLonRad), cos(sunLonRad));
    double decRad = asin(sin(epsRad) * sin(sunLonRad));

    double raApparent = fmod((raRad * RAD_TO_DEG) / 15.0, 24.0);
    if (raApparent < 0) raApparent += 24.0;
    double decApparent = decRad * RAD_TO_DEG;

    // Precess from apparent to J2000
    precessDateToJ2000(raApparent, decApparent, jd, raOut, decOut);
}

void getMoonPosition(double &raOut, double &decOut) {
    time_t now = time(nullptr);
    struct tm *t = gmtime(&now);
    double jd = julianDate(t->tm_year + 1900, t->tm_mon + 1, t->tm_mday,
                           t->tm_hour, t->tm_min, t->tm_sec);

    // Julian centuries since J2000.0
    double T = (jd - 2451545.0) / 36525.0;

    // Moon's mean longitude
    double Lp = fmod(218.3164477 + 481267.88123421 * T - 0.0015786 * T * T +
                     T * T * T / 538841.0 - T * T * T * T / 65194000.0, 360.0);
    if (Lp < 0) Lp += 360.0;

    // Moon's mean elongation
    double D = fmod(297.8501921 + 445267.1114034 * T - 0.0018819 * T * T +
                    T * T * T / 545868.0 - T * T * T * T / 113065000.0, 360.0);

    // Sun's mean anomaly
    double M = fmod(357.5291092 + 35999.0502909 * T - 0.0001536 * T * T +
                    T * T * T / 24490000.0, 360.0);

    // Moon's mean anomaly
    double Mp = fmod(134.9633964 + 477198.8675055 * T + 0.0087414 * T * T +
                     T * T * T / 69699.0 - T * T * T * T / 14712000.0, 360.0);

    // Moon's argument of latitude
    double F = fmod(93.2720950 + 483202.0175233 * T - 0.0036539 * T * T -
                    T * T * T / 3526000.0 + T * T * T * T / 863310000.0, 360.0);

    // Additional arguments
    double A1 = fmod(119.75 + 131.849 * T, 360.0);
    double A2 = fmod(53.09 + 479264.290 * T, 360.0);
    double A3 = fmod(313.45 + 481266.484 * T, 360.0);

    // Eccentricity correction
    double E = 1.0 - 0.002516 * T - 0.0000074 * T * T;

    // Convert to radians
    double Drad = D * DEG_TO_RAD;
    double Mrad = M * DEG_TO_RAD;
    double Mprad = Mp * DEG_TO_RAD;
    double Frad = F * DEG_TO_RAD;
    double A1rad = A1 * DEG_TO_RAD;
    double A2rad = A2 * DEG_TO_RAD;
    double A3rad = A3 * DEG_TO_RAD;
    double Lprad = Lp * DEG_TO_RAD;

    // Sum of longitude terms (most significant)
    double sumL = 6288774.0 * sin(Mprad) +
                  1274027.0 * sin(2 * Drad - Mprad) +
                  658314.0 * sin(2 * Drad) +
                  213618.0 * sin(2 * Mprad) +
                  -185116.0 * E * sin(Mrad) +
                  -114332.0 * sin(2 * Frad) +
                  58793.0 * sin(2 * Drad - 2 * Mprad) +
                  57066.0 * E * sin(2 * Drad - Mrad - Mprad) +
                  53322.0 * sin(2 * Drad + Mprad) +
                  45758.0 * E * sin(2 * Drad - Mrad) +
                  -40923.0 * E * sin(Mrad - Mprad) +
                  -34720.0 * sin(Drad) +
                  -30383.0 * E * sin(Mrad + Mprad) +
                  15327.0 * sin(2 * Drad - 2 * Frad) +
                  -12528.0 * sin(Mprad + 2 * Frad) +
                  10980.0 * sin(Mprad - 2 * Frad) +
                  10675.0 * sin(4 * Drad - Mprad) +
                  10034.0 * sin(3 * Mprad) +
                  8548.0 * sin(4 * Drad - 2 * Mprad);

    // Additional longitude corrections
    sumL += 3958.0 * sin(A1rad) + 1962.0 * sin(Lprad - Frad) + 318.0 * sin(A2rad);

    // Sum of latitude terms
    double sumB = 5128122.0 * sin(Frad) +
                  280602.0 * sin(Mprad + Frad) +
                  277693.0 * sin(Mprad - Frad) +
                  173237.0 * sin(2 * Drad - Frad) +
                  55413.0 * sin(2 * Drad - Mprad + Frad) +
                  46271.0 * sin(2 * Drad - Mprad - Frad) +
                  32573.0 * sin(2 * Drad + Frad) +
                  17198.0 * sin(2 * Mprad + Frad) +
                  9266.0 * sin(2 * Drad + Mprad - Frad) +
                  8822.0 * sin(2 * Mprad - Frad) +
                  -8216.0 * E * sin(2 * Drad - Mrad - Frad) +
                  4324.0 * sin(2 * Drad - 2 * Mprad - Frad) +
                  4200.0 * sin(2 * Drad + Mprad + Frad);

    // Additional latitude corrections
    sumB += -2235.0 * sin(Lprad) + 382.0 * sin(A3rad) +
            175.0 * sin(A1rad - Frad) + 175.0 * sin(A1rad + Frad) +
            127.0 * sin(Lprad - Mprad) - 115.0 * sin(Lprad + Mprad);

    // Ecliptic coordinates
    double eclLon = Lp + sumL / 1000000.0;
    double eclLat = sumB / 1000000.0;

    double eclLonRad = eclLon * DEG_TO_RAD;
    double eclLatRad = eclLat * DEG_TO_RAD;

    // Mean obliquity
    double eps = 23.439291 - 0.0130042 * T;
    double epsRad = eps * DEG_TO_RAD;

    // Convert ecliptic to equatorial
    double xEcl = cos(eclLatRad) * cos(eclLonRad);
    double yEcl = cos(eclLatRad) * sin(eclLonRad);
    double zEcl = sin(eclLatRad);

    double xEq = xEcl;
    double yEq = yEcl * cos(epsRad) - zEcl * sin(epsRad);
    double zEq = yEcl * sin(epsRad) + zEcl * cos(epsRad);

    double raRad = atan2(yEq, xEq);
    double decRad = asin(zEq);

    double raApparent = fmod((raRad * RAD_TO_DEG) / 15.0, 24.0);
    if (raApparent < 0) raApparent += 24.0;
    double decApparent = decRad * RAD_TO_DEG;

    // Precess to J2000
    precessDateToJ2000(raApparent, decApparent, jd, raOut, decOut);
}

void galacticToEquatorial(double lDeg, double bDeg, double &raOut, double &decOut) {
    // Galactic coordinate system constants (J2000)
    const double raGNP = 192.85948 * DEG_TO_RAD;   // RA of North Galactic Pole
    const double decGNP = 27.12825 * DEG_TO_RAD;   // Dec of North Galactic Pole
    const double lNCP = 122.93192 * DEG_TO_RAD;    // Galactic longitude of North Celestial Pole

    double lRad = lDeg * DEG_TO_RAD;
    double bRad = bDeg * DEG_TO_RAD;

    // Calculate declination
    double sinDec = sin(decGNP) * sin(bRad) + cos(decGNP) * cos(bRad) * cos(lNCP - lRad);
    double decRad = asin(sinDec);

    // Calculate right ascension
    double y = cos(bRad) * sin(lNCP - lRad);
    double x = cos(decGNP) * sin(bRad) - sin(decGNP) * cos(bRad) * cos(lNCP - lRad);
    double raRad = raGNP + atan2(y, x);

    // Normalize
    raOut = fmod((raRad * RAD_TO_DEG) / 15.0, 24.0);
    if (raOut < 0) raOut += 24.0;
    decOut = decRad * RAD_TO_DEG;
}

void equatorialToGalactic(double raHours, double decDeg, double &lOut, double &bOut) {
    // Galactic coordinate system constants (J2000)
    const double raGNP = 192.85948 * DEG_TO_RAD;
    const double decGNP = 27.12825 * DEG_TO_RAD;
    const double lNCP = 122.93192 * DEG_TO_RAD;

    double raRad = raHours * 15.0 * DEG_TO_RAD;
    double decRad = decDeg * DEG_TO_RAD;

    // Calculate galactic latitude
    double sinB = sin(decGNP) * sin(decRad) + cos(decGNP) * cos(decRad) * cos(raRad - raGNP);
    double bRad = asin(sinB);

    // Calculate galactic longitude
    double y = cos(decRad) * sin(raRad - raGNP);
    double x = cos(decGNP) * sin(decRad) - sin(decGNP) * cos(decRad) * cos(raRad - raGNP);
    double lRad = lNCP - atan2(y, x);

    lOut = fmod(lRad * RAD_TO_DEG, 360.0);
    if (lOut < 0) lOut += 360.0;
    bOut = bRad * RAD_TO_DEG;
}

void getGalacticPlaneTrackingTarget(double latDeg, double lonDeg, double minAltDeg,
                                    GalacticPlaneTarget &target) {
    target.found = false;

    // Walk outwards along the plane from the galactic centre, taking the first
    // longitude that reaches minAltDeg. Because the two candidates at each step
    // are the same angular distance from the centre - b is zero for both, so
    // their separation from l=0 is just the longitude difference - "closest to
    // the centre" is settled by the step, and the pair is a free choice.
    for (double distance = 0.0; distance <= 180.0; distance += 0.5) {
        double candidates[2] = {distance, 360.0 - distance};
        int candidateCount = (distance == 0.0) ? 1 : 2;   // l=0 is its own mirror
        GalacticPlaneTarget best;
        bool haveCandidate = false;

        for (int i = 0; i < candidateCount; i++) {
            double l = candidates[i];
            if (l >= 360.0) l -= 360.0;
            double ra, dec, alt, az;
            galacticToEquatorial(l, 0.0, ra, dec);
            raDecToAltAz(ra, dec, latDeg, lonDeg, alt, az);
            if (alt < minAltDeg) continue;
            // Between the two, take the higher: equally close to the centre, but
            // it stays observable longer and looks through less atmosphere.
            if (haveCandidate && alt <= best.alt) continue;
            haveCandidate = true;
            best.found = true;
            best.l = l;
            best.b = 0.0;
            best.ra = ra;
            best.dec = dec;
            best.alt = alt;
            best.az = az;
        }

        if (haveCandidate) {
            target = best;
            return;
        }
    }
}
