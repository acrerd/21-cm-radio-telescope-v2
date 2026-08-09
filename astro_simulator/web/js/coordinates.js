// Coordinate and time routines for the web simulator.
// Ported from the repo's validated implementations
// (esp32_controller_arduino/src/coordinates.cpp and
// test_coordinates.ipynb, both checked against astropy).
// All angles in degrees unless noted; JD is the Julian Date (UTC).

export const D2R = Math.PI / 180.0;
export const R2D = 180.0 / Math.PI;

export function julianDate(y, mo, d, h, mi, s) {
  if (mo <= 2) { y -= 1; mo += 12; }
  const A = Math.trunc(y / 100);
  const B = 2 - A + Math.trunc(A / 4);
  let jd = Math.trunc(365.25 * (y + 4716))
         + Math.trunc(30.6001 * (mo + 1)) + d + B - 1524.5;
  jd += (h + mi / 60 + s / 3600) / 24;
  return jd;
}

export function jdFromDate(date) {   // JS Date (uses its UTC fields)
  return julianDate(date.getUTCFullYear(), date.getUTCMonth() + 1,
                    date.getUTCDate(), date.getUTCHours(),
                    date.getUTCMinutes(),
                    date.getUTCSeconds() + date.getUTCMilliseconds() / 1e3);
}

export function decimalYear(jd) {
  // close enough for the Cas A secular fade (days matter, not hours)
  return 2000.0 + (jd - 2451544.5) / 365.25;
}

// GMST in hours (IAU 1982)
export function gmst(jd) {
  const jd0 = Math.floor(jd - 0.5) + 0.5;
  const H = (jd - jd0) * 24.0;
  const D0 = jd0 - 2451545.0;
  const T = D0 / 36525.0;
  const g = 6.697374558 + 0.06570982441908 * D0
          + 1.00273790935 * H + 0.000026 * T * T;
  return ((g % 24) + 24) % 24;
}

export function lst(jd, lonDeg) {
  return (((gmst(jd) + lonDeg / 15.0) % 24) + 24) % 24;
}

// IAU 1976 precession angles (arcsec) for jd relative to J2000
function precAngles(jd) {
  const T = (jd - 2451545.0) / 36525.0;
  const zetaA = (2306.2181 + 1.39656 * T - 0.000139 * T * T) * T
              + (0.30188 - 0.000344 * T) * T * T + 0.017998 * T ** 3;
  const zA = (2306.2181 + 1.39656 * T - 0.000139 * T * T) * T
           + (1.09468 + 0.000066 * T) * T * T + 0.018203 * T ** 3;
  const thetaA = (2004.3109 - 0.85330 * T - 0.000217 * T * T) * T
               - (0.42665 + 0.000217 * T) * T * T - 0.041833 * T ** 3;
  return [zetaA / 3600 * D2R, zA / 3600 * D2R, thetaA / 3600 * D2R];
}

// J2000 -> equinox of date; ra/dec in degrees
export function precessJ2000ToDate(raDeg, decDeg, jd) {
  const [zeta, z, theta] = precAngles(jd);
  const ra0 = raDeg * D2R, dec0 = decDeg * D2R;
  const A = Math.cos(dec0) * Math.sin(ra0 + zeta);
  const B = Math.cos(theta) * Math.cos(dec0) * Math.cos(ra0 + zeta)
          - Math.sin(theta) * Math.sin(dec0);
  const C = Math.sin(theta) * Math.cos(dec0) * Math.cos(ra0 + zeta)
          + Math.cos(theta) * Math.sin(dec0);
  return { ra: (((Math.atan2(A, B) + z) * R2D) % 360 + 360) % 360,
           dec: Math.asin(Math.min(1, Math.max(-1, C))) * R2D };
}

// equinox of date -> J2000
export function precessDateToJ2000(raDeg, decDeg, jd) {
  const [zeta, z, theta] = precAngles(jd);
  const ra = raDeg * D2R, dec = decDeg * D2R;
  const A = Math.cos(dec) * Math.sin(ra - z);
  const B = Math.cos(theta) * Math.cos(dec) * Math.cos(ra - z)
          + Math.sin(theta) * Math.sin(dec);
  const C = -Math.sin(theta) * Math.cos(dec) * Math.cos(ra - z)
          + Math.cos(theta) * Math.sin(dec);
  return { ra: (((Math.atan2(A, B) - zeta) * R2D) % 360 + 360) % 360,
           dec: Math.asin(Math.min(1, Math.max(-1, C))) * R2D };
}

// RA/Dec (J2000, deg) -> Alt/Az (deg) at jd for a site
export function raDecToAltAz(raDeg, decDeg, latDeg, lonDeg, jd) {
  const now = precessJ2000ToDate(raDeg, decDeg, jd);
  const haRad = (lst(jd, lonDeg) * 15.0 - now.ra) * D2R;
  const dec = now.dec * D2R, lat = latDeg * D2R;
  const sinAlt = Math.sin(dec) * Math.sin(lat)
               + Math.cos(dec) * Math.cos(lat) * Math.cos(haRad);
  const alt = Math.asin(Math.min(1, Math.max(-1, sinAlt)));
  let cosAz = (Math.sin(dec) - Math.sin(alt) * Math.sin(lat))
            / (Math.cos(alt) * Math.cos(lat));
  cosAz = Math.min(1, Math.max(-1, cosAz));
  let az = Math.acos(cosAz);
  if (Math.sin(haRad) > 0) az = 2 * Math.PI - az;
  return { alt: alt * R2D, az: az * R2D };
}

// Alt/Az (deg) at jd -> galactic l/b (via of-date RA/Dec then J2000)
export function altAzToGal(altDeg, azDeg, latDeg, lonDeg, jd) {
  const alt = altDeg * D2R, az = azDeg * D2R, lat = latDeg * D2R;
  const sinDec = Math.sin(alt) * Math.sin(lat)
               + Math.cos(alt) * Math.cos(lat) * Math.cos(az);
  const dec = Math.asin(Math.min(1, Math.max(-1, sinDec)));
  const y = -Math.sin(az) * Math.cos(alt);
  const x = Math.sin(alt) * Math.cos(lat)
          - Math.cos(alt) * Math.sin(lat) * Math.cos(az);
  const ha = Math.atan2(y, x);
  const ra = ((lst(jd, lonDeg) * 15.0 - ha * R2D) % 360 + 360) % 360;
  const j2000 = precessDateToJ2000(ra, dec * R2D, jd);
  return eqToGal(j2000.ra, j2000.dec);
}

// Galactic <-> equatorial (J2000).  Same constants as the desktop app.
const RA_NGP = 192.85948 * D2R;
const DEC_NGP = 27.12825 * D2R;
const L_NCP = 122.93192 * D2R;

export function galToEq(lDeg, bDeg) {
  const l = lDeg * D2R, b = bDeg * D2R;
  const sinDec = Math.sin(DEC_NGP) * Math.sin(b)
               + Math.cos(DEC_NGP) * Math.cos(b) * Math.cos(L_NCP - l);
  const dec = Math.asin(Math.min(1, Math.max(-1, sinDec)));
  const y = Math.cos(b) * Math.sin(L_NCP - l);
  const x = Math.cos(DEC_NGP) * Math.sin(b)
          - Math.sin(DEC_NGP) * Math.cos(b) * Math.cos(L_NCP - l);
  const ra = RA_NGP + Math.atan2(y, x);
  return { ra: ((ra * R2D) % 360 + 360) % 360, dec: dec * R2D };
}

export function eqToGal(raDeg, decDeg) {
  const ra = raDeg * D2R, dec = decDeg * D2R;
  const sinB = Math.sin(DEC_NGP) * Math.sin(dec)
             + Math.cos(DEC_NGP) * Math.cos(dec) * Math.cos(ra - RA_NGP);
  const b = Math.asin(Math.min(1, Math.max(-1, sinB)));
  const y = Math.cos(dec) * Math.sin(ra - RA_NGP);
  const x = Math.cos(DEC_NGP) * Math.sin(dec)
          - Math.sin(DEC_NGP) * Math.cos(dec) * Math.cos(ra - RA_NGP);
  const l = L_NCP - Math.atan2(y, x);
  return { l: ((l * R2D) % 360 + 360) % 360, b: b * R2D };
}

// Angular separation (deg), haversine — mirrors the desktop helper
export function sepDeg(l1, b1, l2, b2) {
  const p1 = b1 * D2R, p2 = b2 * D2R;
  const dl = (l1 - l2) * D2R;
  const s = Math.sin((p1 - p2) / 2) ** 2
          + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * Math.asin(Math.min(1, Math.sqrt(s))) * R2D;
}
