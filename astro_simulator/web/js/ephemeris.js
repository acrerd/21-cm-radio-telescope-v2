// Solar-system ephemeris and velocity-frame offsets.
// Sun and Moon follow Meeus, "Astronomical Algorithms" (same series
// as the repo's validated C++/notebook ports); the Moon adds distance
// terms so topocentric parallax (~1 deg) can be applied.
// Accuracy: Sun ~0.01 deg, Moon ~0.3 deg, frame offsets ~40 m/s —
// all far inside the simulator's >=1.5 deg beams and 1.29 km/s channels.

import { D2R, R2D, precessDateToJ2000, eqToGal, galToEq, lst,
         sepDeg } from "./coordinates.js";

const AU_M = 1.495978707e11;

// Sun: apparent geocentric RA/Dec (J2000) and distance (AU)
export function sunPosition(jd) {
  const T = (jd - 2451545.0) / 36525.0;
  const L0 = ((280.46646 + 36000.76983 * T + 0.0003032 * T * T) % 360 + 360) % 360;
  const M = ((357.52911 + 35999.05029 * T - 0.0001537 * T * T) % 360 + 360) % 360;
  const Mr = M * D2R;
  const C = (1.914602 - 0.004817 * T - 0.000014 * T * T) * Math.sin(Mr)
          + (0.019993 - 0.000101 * T) * Math.sin(2 * Mr)
          + 0.000289 * Math.sin(3 * Mr);
  const sunLon = L0 + C;
  const nu = M + C;                                  // true anomaly
  const e = 0.016708634 - 0.000042037 * T - 0.0000001267 * T * T;
  const R = 1.000001018 * (1 - e * e) / (1 + e * Math.cos(nu * D2R));
  const omega = 125.04 - 1934.136 * T;
  const lonApp = sunLon - 0.00569 - 0.00478 * Math.sin(omega * D2R);
  const lonR = lonApp * D2R;
  const eps0 = 23.439291 - 0.0130042 * T - 0.00000016 * T * T
             + 0.000000504 * T ** 3;
  const eps = (eps0 + 0.00256 * Math.cos(omega * D2R)) * D2R;
  const ra = Math.atan2(Math.cos(eps) * Math.sin(lonR), Math.cos(lonR));
  const dec = Math.asin(Math.sin(eps) * Math.sin(lonR));
  const j2000 = precessDateToJ2000(((ra * R2D) % 360 + 360) % 360,
                                   dec * R2D, jd);
  return { ra: j2000.ra, dec: j2000.dec, rAU: R };
}

export function sunGalactic(jd) {
  const s = sunPosition(jd);
  return eqToGal(s.ra, s.dec);
}

// Moon: Meeus ch. 47 principal terms, geocentric of-date + distance
function moonGeocentric(jd) {
  const T = (jd - 2451545.0) / 36525.0;
  const n360 = (x) => ((x % 360) + 360) % 360;
  const Lp = n360(218.3164477 + 481267.88123421 * T - 0.0015786 * T * T
                  + T ** 3 / 538841 - T ** 4 / 65194000);
  const D = n360(297.8501921 + 445267.1114034 * T - 0.0018819 * T * T
                 + T ** 3 / 545868 - T ** 4 / 113065000);
  const M = n360(357.5291092 + 35999.0502909 * T - 0.0001536 * T * T
                 + T ** 3 / 24490000);
  const Mp = n360(134.9633964 + 477198.8675055 * T + 0.0087414 * T * T
                  + T ** 3 / 69699 - T ** 4 / 14712000);
  const F = n360(93.2720950 + 483202.0175233 * T - 0.0036539 * T * T
                 - T ** 3 / 3526000 + T ** 4 / 863310000);
  const A1 = n360(119.75 + 131.849 * T);
  const A2 = n360(53.09 + 479264.290 * T);
  const A3 = n360(313.45 + 481266.484 * T);
  const E = 1 - 0.002516 * T - 0.0000074 * T * T;
  const s = Math.sin, c = Math.cos, r = D2R;
  const Dr = D * r, Mr = M * r, Mpr = Mp * r, Fr = F * r;

  let sumL =
      6288774 * s(Mpr) + 1274027 * s(2 * Dr - Mpr) + 658314 * s(2 * Dr)
    + 213618 * s(2 * Mpr) - 185116 * E * s(Mr) - 114332 * s(2 * Fr)
    + 58793 * s(2 * Dr - 2 * Mpr) + 57066 * E * s(2 * Dr - Mr - Mpr)
    + 53322 * s(2 * Dr + Mpr) + 45758 * E * s(2 * Dr - Mr)
    - 40923 * E * s(Mr - Mpr) - 34720 * s(Dr) - 30383 * E * s(Mr + Mpr)
    + 15327 * s(2 * Dr - 2 * Fr) - 12528 * s(Mpr + 2 * Fr)
    + 10980 * s(Mpr - 2 * Fr) + 10675 * s(4 * Dr - Mpr)
    + 10034 * s(3 * Mpr) + 8548 * s(4 * Dr - 2 * Mpr)
    - 7888 * E * s(2 * Dr + Mr - Mpr) - 6766 * E * s(2 * Dr + Mr)
    - 5163 * s(Dr - Mpr) + 4987 * E * s(Dr + Mr)
    + 4036 * E * s(2 * Dr - Mr + Mpr);
  sumL += 3958 * s(A1 * r) + 1962 * s((Lp - F) * r) + 318 * s(A2 * r);

  let sumB =
      5128122 * s(Fr) + 280602 * s(Mpr + Fr) + 277693 * s(Mpr - Fr)
    + 173237 * s(2 * Dr - Fr) + 55413 * s(2 * Dr - Mpr + Fr)
    + 46271 * s(2 * Dr - Mpr - Fr) + 32573 * s(2 * Dr + Fr)
    + 17198 * s(2 * Mpr + Fr) + 9266 * s(2 * Dr + Mpr - Fr)
    + 8822 * s(2 * Mpr - Fr) - 8216 * E * s(2 * Dr - Mr - Fr)
    + 4324 * s(2 * Dr - 2 * Mpr - Fr) + 4200 * s(2 * Dr + Mpr + Fr)
    - 3359 * E * s(2 * Dr + Mr - Fr)
    + 2463 * E * s(2 * Dr - Mr - Mpr + Fr) + 2211 * E * s(2 * Dr - Mr + Fr)
    + 2065 * E * s(2 * Dr - Mr - Mpr - Fr) - 1870 * E * s(Mr - Mpr - Fr);
  sumB += -2235 * s(Lp * r) + 382 * s(A3 * r) + 175 * s((A1 - F) * r)
        + 175 * s((A1 + F) * r) + 127 * s((Lp - Mp) * r)
        - 115 * s((Lp + Mp) * r);

  // distance terms (Meeus 47.A, principal cosines), metres via km
  const sumR =
      -20905355 * c(Mpr) - 3699111 * c(2 * Dr - Mpr) - 2955968 * c(2 * Dr)
    - 569925 * c(2 * Mpr) + 48888 * E * c(Mr) - 3149 * c(2 * Fr)
    + 246158 * c(2 * Dr - 2 * Mpr) - 152138 * E * c(2 * Dr - Mr - Mpr)
    - 170733 * c(2 * Dr + Mpr) - 204586 * E * c(2 * Dr - Mr)
    - 129620 * E * c(Mr - Mpr) + 108743 * c(Dr) + 104755 * E * c(Mr + Mpr)
    + 10321 * c(2 * Dr - 2 * Fr) + 79661 * c(Mpr - 2 * Fr)
    - 34782 * c(4 * Dr - Mpr) - 23210 * c(3 * Mpr)
    - 21636 * c(4 * Dr - 2 * Mpr) + 24208 * E * c(2 * Dr + Mr - Mpr)
    + 30824 * E * c(2 * Dr + Mr) - 8379 * c(Dr - Mpr)
    - 16675 * E * c(Dr + Mr) - 12831 * E * c(2 * Dr - Mr + Mpr)
    - 10445 * c(2 * Dr + 2 * Mpr);

  const eclLon = Lp + sumL / 1e6;
  const eclLat = sumB / 1e6;
  const distKm = 385000.56 + sumR / 1000;
  const eps = (23.439291 - 0.0130042 * T) * D2R;
  const lonR2 = eclLon * D2R, latR2 = eclLat * D2R;
  const xe = Math.cos(latR2) * Math.cos(lonR2);
  const ye = Math.cos(latR2) * Math.sin(lonR2);
  const ze = Math.sin(latR2);
  const xq = xe;
  const yq = ye * Math.cos(eps) - ze * Math.sin(eps);
  const zq = ye * Math.sin(eps) + ze * Math.cos(eps);
  return { ra: ((Math.atan2(yq, xq) * R2D) % 360 + 360) % 360,
           dec: Math.asin(zq) * R2D, distKm };
}

// Moon: topocentric galactic l/b for a site (parallax ~1 deg matters)
export function moonGalacticTopo(jd, site) {
  const g = moonGeocentric(jd);                       // of-date frame
  const phi = site.lat * D2R;
  // geodetic -> geocentric observer position (Meeus ch. 11)
  const u = Math.atan(0.99664719 * Math.tan(phi));
  const rSinP = 0.99664719 * Math.sin(u)
              + (site.height / 6378140) * Math.sin(phi);
  const rCosP = Math.cos(u) + (site.height / 6378140) * Math.cos(phi);
  const sinPi = 6378.14 / g.distKm;                   // horizontal parallax
  const H = (lst(jd, site.lon) * 15.0 - g.ra) * D2R;  // local hour angle
  const dec = g.dec * D2R;
  const dAlpha = Math.atan2(
      -rCosP * sinPi * Math.sin(H),
      Math.cos(dec) - rCosP * sinPi * Math.cos(H));
  const raT = g.ra + dAlpha * R2D;
  const decT = Math.atan2(
      (Math.sin(dec) - rSinP * sinPi) * Math.cos(dAlpha),
      Math.cos(dec) - rCosP * sinPi * Math.cos(H)) * R2D;
  const j2000 = precessDateToJ2000(((raT % 360) + 360) % 360, decT, jd);
  return eqToGal(j2000.ra, j2000.dec);
}

// Cas A secular fade: Perley & Butler (2017) epoch-2000 anchor (1749 Jy at
// 1420 MHz) and their ~0.53 %/yr L-band ageing rate; ~1520 Jy in 2026. The
// old Baars 2500 Jy @1965 anchor was ~20 % above Baars' own formula there.
export function casAFluxJy(decimalYr) {
  return 1748.9 * Math.exp(-0.0053 * (decimalYr - 2000.0));
}

// Tau A (Crab) fade: Baars 875 Jy @1420, 1977, at 0.167 %/yr (Aller &
// Reynolds 1985); ~805 Jy in 2026.
export function tauAFluxJy(decimalYr) {
  return 875.0 * Math.exp(-0.00167 * (decimalYr - 1977.0));
}

// The five analytic continuum sources, evaluated at jd for a site
export function continuumSources(jd, decimalYr, site) {
  const fixed = [
    ["Cyg A", eqToGal(299.868, 40.734), 1590.0],
    ["Cas A", eqToGal(350.850, 58.815), casAFluxJy(decimalYr)],
    ["Tau A", eqToGal(83.633, 22.015), tauAFluxJy(decimalYr)],
  ];
  const sun = sunGalactic(jd);
  const moon = moonGalacticTopo(jd, site);
  return fixed.map(([n, g, f]) => ({ name: n, l: g.l, b: g.b, jy: f }))
    .concat([{ name: "Sun", l: sun.l, b: sun.b, jy: 5.0e5 },
             { name: "Moon", l: moon.l, b: moon.b, jy: 890.0 }]);
}

// ---- velocity frames ------------------------------------------------
// LSRK apex: 20 km/s toward RA 18h Dec +30 (B1900); the exact ICRS
// direction below is astropy's FK4(B1900)->ICRS of that point, so the
// E-term/frame subtleties cost us nothing.
const APEX_RA = 270.9593925696, APEX_DEC = 30.0046709533;

// v to ADD to a v_LSRK axis to express it in the SSB frame
export function ssbOffset(lDeg, bDeg) {
  const eq = galToEq(lDeg, bDeg);
  return -20.0e3 * Math.cos(sepDeg(APEX_RA, APEX_DEC, eq.ra, eq.dec) * D2R);
}

// Earth barycentric velocity (m/s, equatorial J2000 xyz) by central
// difference of the Meeus geocentric Sun (Earth = -Sun); the Sun's own
// ~13 m/s barycentric wander is inside the stated error budget.
function earthVelocity(jd) {
  const dt = 0.02;                                    // days
  const p = (t) => {
    const s = sunPosition(t);
    const ra = s.ra * D2R, dec = s.dec * D2R;
    const r = s.rAU * AU_M;
    return [r * Math.cos(dec) * Math.cos(ra),
            r * Math.cos(dec) * Math.sin(ra),
            r * Math.sin(dec)];
  };
  const a = p(jd - dt), b = p(jd + dt);
  const f = -1.0 / (2 * dt * 86400);                  // Earth = -Sun
  return [(b[0] - a[0]) * f, (b[1] - a[1]) * f, (b[2] - a[2]) * f];
}

// Barycentric radial-velocity correction toward a target (m/s):
// projection of observer velocity (orbital + diurnal) on the target.
export function barycentricCorrection(lDeg, bDeg, jd, site) {
  const eq = galToEq(lDeg, bDeg);
  const ra = eq.ra * D2R, dec = eq.dec * D2R;
  const n = [Math.cos(dec) * Math.cos(ra),
             Math.cos(dec) * Math.sin(ra),
             Math.sin(dec)];
  const ve = earthVelocity(jd);
  // diurnal rotation: eastward, magnitude w*R*cos(geocentric lat)
  const phi = site.lat * D2R;
  const u = Math.atan(0.99664719 * Math.tan(phi));
  const rCosP = Math.cos(u) + (site.height / 6378140) * Math.cos(phi);
  const vRot = 465.10 * rCosP;
  const theta = lst(jd, site.lon) * 15.0 * D2R;       // local sidereal
  const vd = [-vRot * Math.sin(theta), vRot * Math.cos(theta), 0.0];
  return (ve[0] + vd[0]) * n[0] + (ve[1] + vd[1]) * n[1]
       + (ve[2] + vd[2]) * n[2];
}

// v to ADD to a v_LSRK axis for the requested frame — mirrors the
// desktop frame_offset(): 'lsr' | 'ssb' | 'topo'
export function frameOffset(lDeg, bDeg, frame, jd, site) {
  if (frame === "lsr") return 0.0;
  const dv = ssbOffset(lDeg, bDeg);
  if (frame === "ssb") return dv;
  return dv - barycentricCorrection(lDeg, bDeg, jd, site);
}
