// coordinates.h - Astronomical coordinate transformations

#ifndef COORDINATES_H
#define COORDINATES_H

#ifdef ARDUINO
#include <Arduino.h>
#else
// Host-side tools may not provide constants normally defined by Arduino.h.
#ifndef DEG_TO_RAD
#define DEG_TO_RAD 0.017453292519943295
#endif
#ifndef RAD_TO_DEG
#define RAD_TO_DEG 57.29577951308232
#endif
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif
#include <cmath>
#include <ctime>
#endif

// Julian Date calculation
double julianDate(int year, int month, int day, int hour, int minute, int second);

// Greenwich Mean Sidereal Time (hours)
double gmst(double jd, double hoursUT = -1);

// Local Sidereal Time (hours)
double localSiderealTime(double jd, double longitude, double hoursUT = -1);

// Precession from J2000 to date
void precessJ2000ToDate(double raHours, double decDeg, double jd, double &raOut, double &decOut);

// Precession from date to J2000
void precessDateToJ2000(double raHours, double decDeg, double jd, double &raOut, double &decOut);

// RA/Dec (J2000) to Alt/Az
void raDecToAltAz(double raHours, double decDeg, double latDeg, double lonDeg, double &altOut, double &azOut);

// Alt/Az to RA/Dec (J2000) - for Stellarium feedback
void altAzToRaDec(double altDeg, double azDeg, double latDeg, double lonDeg, double &raOut, double &decOut);

// Sun position (J2000 RA/Dec)
void getSunPosition(double &raOut, double &decOut);

// Moon position (J2000 RA/Dec)
void getMoonPosition(double &raOut, double &decOut);

// Galactic to Equatorial (J2000)
void galacticToEquatorial(double lDeg, double bDeg, double &raOut, double &decOut);

// Equatorial (J2000) to Galactic
void equatorialToGalactic(double raHours, double decDeg, double &lOut, double &bOut);

struct GalacticPlaneTarget {
    bool found = false;
    double l = 0.0;      // galactic longitude chosen, degrees
    double b = 0.0;      // always 0 - the plane itself
    double ra = 0.0;
    double dec = 0.0;
    double alt = 0.0;
    double az = 0.0;
};

// The point on the galactic plane (b=0) nearest the galactic centre that is at
// or above minAltDeg, searched outwards in galactic longitude from l=0.
//
// minAltDeg is an ACQUISITION floor, not an observing horizon: it decides where
// tracking starts, and the target is then followed down as it sets, until the
// ordinary horizon check parks the dish. It is deliberately high (45 degrees by
// default) because the galactic centre is far south of this site - it culminates
// at about 5 degrees from Glasgow and never clears the trees - so the useful
// question is not "is the plane up" but "where is the plane high enough to be
// worth observing".
//
// Sets target.found = false when no part of the plane reaches minAltDeg.
void getGalacticPlaneTrackingTarget(double latDeg, double lonDeg, double minAltDeg,
                                    GalacticPlaneTarget &target);

#endif // COORDINATES_H
