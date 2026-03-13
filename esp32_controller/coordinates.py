# coordinates.py - Astronomical coordinate transformations

import math
import time


def julian_date(year, month, day, hour, minute, second):
    """Calculate Julian Date from calendar date/time (UTC)"""
    if month <= 2:
        year -= 1
        month += 12

    A = int(year / 100)
    B = 2 - A + int(A / 4)

    jd = int(365.25 * (year + 4716)) + int(30.6001 * (month + 1)) + day + B - 1524.5
    jd += (hour + minute / 60 + second / 3600) / 24

    return jd


def gmst(jd):
    """Calculate Greenwich Mean Sidereal Time in hours from Julian Date.

    Uses the IAU 1982 formula for GMST.
    """
    # Calculate JD at 0h UT (midnight) for this day
    jd0 = math.floor(jd - 0.5) + 0.5

    # Hours since 0h UT
    H = (jd - jd0) * 24.0

    # Days since J2000.0 at 0h UT
    D0 = jd0 - 2451545.0
    T = D0 / 36525.0

    # GMST at 0h UT in hours (IAU 1982 formula)
    gmst0 = 6.697374558 + 0.06570982441908 * D0 + 1.00273790935 * H + 0.000026 * T**2

    # Normalize to 0-24 hours
    gmst_hours = gmst0 % 24

    return gmst_hours


def precess_j2000_to_date(ra_hours, dec_deg, jd):
    """
    Precess J2000.0 coordinates to the given Julian Date.

    Uses the IAU 1976 precession model (Lieske et al.).
    Accurate to ~1 arcsec for dates within a few centuries of J2000.

    Args:
        ra_hours: Right Ascension in hours (J2000)
        dec_deg: Declination in degrees (J2000)
        jd: Target Julian Date

    Returns:
        (ra_hours, dec_deg) at the target epoch
    """
    # Julian centuries from J2000.0
    T = (jd - 2451545.0) / 36525.0

    # Precession angles in arcseconds (IAU 1976)
    zeta_A = (2306.2181 + 1.39656 * T - 0.000139 * T**2) * T + \
             (0.30188 - 0.000344 * T) * T**2 + 0.017998 * T**3
    z_A = (2306.2181 + 1.39656 * T - 0.000139 * T**2) * T + \
          (1.09468 + 0.000066 * T) * T**2 + 0.018203 * T**3
    theta_A = (2004.3109 - 0.85330 * T - 0.000217 * T**2) * T - \
              (0.42665 + 0.000217 * T) * T**2 - 0.041833 * T**3

    # Convert to radians
    zeta = math.radians(zeta_A / 3600)
    z = math.radians(z_A / 3600)
    theta = math.radians(theta_A / 3600)

    # Original coordinates in radians
    ra0 = math.radians(ra_hours * 15)
    dec0 = math.radians(dec_deg)

    # Apply precession rotation
    A = math.cos(dec0) * math.sin(ra0 + zeta)
    B = math.cos(theta) * math.cos(dec0) * math.cos(ra0 + zeta) - math.sin(theta) * math.sin(dec0)
    C = math.sin(theta) * math.cos(dec0) * math.cos(ra0 + zeta) + math.cos(theta) * math.sin(dec0)

    # New coordinates
    ra_rad = math.atan2(A, B) + z
    dec_rad = math.asin(C)

    # Convert back to hours and degrees
    ra_new = (math.degrees(ra_rad) / 15) % 24
    dec_new = math.degrees(dec_rad)

    return ra_new, dec_new


def precess_date_to_j2000(ra_hours, dec_deg, jd):
    """
    Precess coordinates from the given Julian Date back to J2000.0.

    This is the inverse of precess_j2000_to_date.

    Args:
        ra_hours: Right Ascension in hours (at date)
        dec_deg: Declination in degrees (at date)
        jd: Source Julian Date

    Returns:
        (ra_hours, dec_deg) at J2000.0
    """
    # Julian centuries from J2000.0
    T = (jd - 2451545.0) / 36525.0

    # Precession angles in arcseconds (IAU 1976)
    zeta_A = (2306.2181 + 1.39656 * T - 0.000139 * T**2) * T + \
             (0.30188 - 0.000344 * T) * T**2 + 0.017998 * T**3
    z_A = (2306.2181 + 1.39656 * T - 0.000139 * T**2) * T + \
          (1.09468 + 0.000066 * T) * T**2 + 0.018203 * T**3
    theta_A = (2004.3109 - 0.85330 * T - 0.000217 * T**2) * T - \
              (0.42665 + 0.000217 * T) * T**2 - 0.041833 * T**3

    # Convert to radians
    zeta = math.radians(zeta_A / 3600)
    z = math.radians(z_A / 3600)
    theta = math.radians(theta_A / 3600)

    # Current coordinates in radians
    ra = math.radians(ra_hours * 15)
    dec = math.radians(dec_deg)

    # Apply inverse precession rotation (swap zeta and z, negate angles)
    A = math.cos(dec) * math.sin(ra - z)
    B = math.cos(theta) * math.cos(dec) * math.cos(ra - z) + math.sin(theta) * math.sin(dec)
    C = -math.sin(theta) * math.cos(dec) * math.cos(ra - z) + math.cos(theta) * math.sin(dec)

    # J2000 coordinates
    ra0_rad = math.atan2(A, B) - zeta
    dec0_rad = math.asin(C)

    # Convert back to hours and degrees
    ra0 = (math.degrees(ra0_rad) / 15) % 24
    dec0 = math.degrees(dec0_rad)

    return ra0, dec0


def local_sidereal_time(jd, longitude):
    """Calculate Local Sidereal Time in hours"""
    lst = gmst(jd) + longitude / 15.0
    return lst % 24


def ra_dec_to_alt_az(ra_hours, dec_deg, lat_deg, lon_deg):
    """
    Convert RA/Dec (J2000) to Alt/Az

    Args:
        ra_hours: Right Ascension in hours (0-24), J2000 epoch
        dec_deg: Declination in degrees (-90 to +90), J2000 epoch
        lat_deg: Observer latitude in degrees
        lon_deg: Observer longitude in degrees (west negative)

    Returns:
        (altitude, azimuth) in degrees
    """
    # Get current time from RTC
    t = time.gmtime()
    jd = julian_date(t[0], t[1], t[2], t[3], t[4], t[5])

    # Precess from J2000 to current date
    ra_now, dec_now = precess_j2000_to_date(ra_hours, dec_deg, jd)

    # Calculate hour angle
    lst = local_sidereal_time(jd, lon_deg)
    ha_hours = lst - ra_now
    ha_rad = math.radians(ha_hours * 15)  # Convert to radians

    # Convert to radians
    dec_rad = math.radians(dec_now)
    lat_rad = math.radians(lat_deg)

    # Calculate altitude
    sin_alt = (math.sin(dec_rad) * math.sin(lat_rad) +
               math.cos(dec_rad) * math.cos(lat_rad) * math.cos(ha_rad))
    alt_rad = math.asin(sin_alt)

    # Calculate azimuth
    cos_az = ((math.sin(dec_rad) - math.sin(alt_rad) * math.sin(lat_rad)) /
              (math.cos(alt_rad) * math.cos(lat_rad)))

    # Clamp cos_az to valid range (handles floating point errors)
    cos_az = max(-1, min(1, cos_az))
    az_rad = math.acos(cos_az)

    # Adjust azimuth quadrant
    if math.sin(ha_rad) > 0:
        az_rad = 2 * math.pi - az_rad

    alt_deg = math.degrees(alt_rad)
    az_deg = math.degrees(az_rad)

    return alt_deg, az_deg


def alt_az_to_ra_dec(alt_deg, az_deg, lat_deg, lon_deg):
    """
    Convert Alt/Az to RA/Dec (J2000) for Stellarium feedback

    Args:
        alt_deg: Altitude in degrees
        az_deg: Azimuth in degrees (0=N, 90=E)
        lat_deg: Observer latitude in degrees
        lon_deg: Observer longitude in degrees

    Returns:
        (ra_hours, dec_deg) in J2000 coordinates
    """
    # Convert to radians
    alt_rad = math.radians(alt_deg)
    az_rad = math.radians(az_deg)
    lat_rad = math.radians(lat_deg)

    # Calculate declination (at current epoch)
    sin_dec = (math.sin(alt_rad) * math.sin(lat_rad) +
               math.cos(alt_rad) * math.cos(lat_rad) * math.cos(az_rad))
    dec_rad = math.asin(sin_dec)

    # Calculate hour angle
    cos_ha = ((math.sin(alt_rad) - math.sin(lat_rad) * math.sin(dec_rad)) /
              (math.cos(lat_rad) * math.cos(dec_rad)))
    cos_ha = max(-1, min(1, cos_ha))
    ha_rad = math.acos(cos_ha)

    if math.sin(az_rad) > 0:
        ha_rad = 2 * math.pi - ha_rad

    # Get current LST and calculate RA (at current epoch)
    t = time.gmtime()
    jd = julian_date(t[0], t[1], t[2], t[3], t[4], t[5])
    lst = local_sidereal_time(jd, lon_deg)

    ra_now = lst - math.degrees(ha_rad) / 15
    ra_now = ra_now % 24
    dec_now = math.degrees(dec_rad)

    # Precess back to J2000
    ra_j2000, dec_j2000 = precess_date_to_j2000(ra_now, dec_now, jd)

    return ra_j2000, dec_j2000


def get_sun_position():
    """
    Calculate the Sun's RA/Dec (J2000) for the current time.
    Based on Meeus "Astronomical Algorithms" - accurate to ~0.01 degree.

    Returns:
        (ra_hours, dec_deg) in J2000 coordinates
    """
    t = time.gmtime()
    jd = julian_date(t[0], t[1], t[2], t[3], t[4], t[5])

    # Julian centuries since J2000.0
    T = (jd - 2451545.0) / 36525.0

    # Geometric mean longitude of the Sun (degrees)
    L0 = (280.46646 + 36000.76983 * T + 0.0003032 * T**2) % 360

    # Mean anomaly of the Sun (degrees)
    M = (357.52911 + 35999.05029 * T - 0.0001537 * T**2) % 360
    M_rad = math.radians(M)

    # Equation of center (degrees)
    C = ((1.914602 - 0.004817 * T - 0.000014 * T**2) * math.sin(M_rad) +
         (0.019993 - 0.000101 * T) * math.sin(2 * M_rad) +
         0.000289 * math.sin(3 * M_rad))

    # Sun's true longitude (degrees)
    sun_lon = L0 + C

    # Apparent longitude (corrected for nutation and aberration)
    omega = 125.04 - 1934.136 * T
    sun_lon_apparent = sun_lon - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    sun_lon_rad = math.radians(sun_lon_apparent)

    # Mean obliquity of the ecliptic
    eps0 = 23.439291 - 0.0130042 * T - 0.00000016 * T**2 + 0.000000504 * T**3

    # Corrected obliquity
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))
    eps_rad = math.radians(eps)

    # Convert ecliptic to equatorial coordinates
    ra_rad = math.atan2(
        math.cos(eps_rad) * math.sin(sun_lon_rad),
        math.cos(sun_lon_rad)
    )
    dec_rad = math.asin(math.sin(eps_rad) * math.sin(sun_lon_rad))

    ra_apparent = (math.degrees(ra_rad) / 15) % 24
    dec_apparent = math.degrees(dec_rad)

    # Precess from apparent (equinox of date) back to J2000
    ra_j2000, dec_j2000 = precess_date_to_j2000(ra_apparent, dec_apparent, jd)

    return ra_j2000, dec_j2000


def get_moon_position():
    """
    Calculate the Moon's RA/Dec (J2000) for the current time.
    Based on Meeus "Astronomical Algorithms" Ch. 47 - accurate to ~0.3 degree.

    Returns:
        (ra_hours, dec_deg) in J2000 coordinates
    """
    t = time.gmtime()
    jd = julian_date(t[0], t[1], t[2], t[3], t[4], t[5])

    # Julian centuries since J2000.0
    T = (jd - 2451545.0) / 36525.0

    # Moon's mean longitude (degrees)
    Lp = (218.3164477 + 481267.88123421 * T - 0.0015786 * T**2 +
          T**3 / 538841 - T**4 / 65194000) % 360

    # Moon's mean elongation (degrees)
    D = (297.8501921 + 445267.1114034 * T - 0.0018819 * T**2 +
         T**3 / 545868 - T**4 / 113065000) % 360

    # Sun's mean anomaly (degrees)
    M = (357.5291092 + 35999.0502909 * T - 0.0001536 * T**2 +
         T**3 / 24490000) % 360

    # Moon's mean anomaly (degrees)
    Mp = (134.9633964 + 477198.8675055 * T + 0.0087414 * T**2 +
          T**3 / 69699 - T**4 / 14712000) % 360

    # Moon's argument of latitude (degrees)
    F = (93.2720950 + 483202.0175233 * T - 0.0036539 * T**2 -
         T**3 / 3526000 + T**4 / 863310000) % 360

    # Additional arguments
    A1 = (119.75 + 131.849 * T) % 360
    A2 = (53.09 + 479264.290 * T) % 360
    A3 = (313.45 + 481266.484 * T) % 360

    # Eccentricity correction
    E = 1 - 0.002516 * T - 0.0000074 * T**2

    # Convert to radians
    D_rad = math.radians(D)
    M_rad = math.radians(M)
    Mp_rad = math.radians(Mp)
    F_rad = math.radians(F)
    A1_rad = math.radians(A1)
    A2_rad = math.radians(A2)
    A3_rad = math.radians(A3)

    # Sum of longitude terms (most significant terms from Table 47.A)
    sum_l = (
        6288774 * math.sin(Mp_rad) +
        1274027 * math.sin(2*D_rad - Mp_rad) +
        658314 * math.sin(2*D_rad) +
        213618 * math.sin(2*Mp_rad) +
        -185116 * E * math.sin(M_rad) +
        -114332 * math.sin(2*F_rad) +
        58793 * math.sin(2*D_rad - 2*Mp_rad) +
        57066 * E * math.sin(2*D_rad - M_rad - Mp_rad) +
        53322 * math.sin(2*D_rad + Mp_rad) +
        45758 * E * math.sin(2*D_rad - M_rad) +
        -40923 * E * math.sin(M_rad - Mp_rad) +
        -34720 * math.sin(D_rad) +
        -30383 * E * math.sin(M_rad + Mp_rad) +
        15327 * math.sin(2*D_rad - 2*F_rad) +
        -12528 * math.sin(Mp_rad + 2*F_rad) +
        10980 * math.sin(Mp_rad - 2*F_rad) +
        10675 * math.sin(4*D_rad - Mp_rad) +
        10034 * math.sin(3*Mp_rad) +
        8548 * math.sin(4*D_rad - 2*Mp_rad) +
        -7888 * E * math.sin(2*D_rad + M_rad - Mp_rad) +
        -6766 * E * math.sin(2*D_rad + M_rad) +
        -5163 * math.sin(D_rad - Mp_rad) +
        4987 * E * math.sin(D_rad + M_rad) +
        4036 * E * math.sin(2*D_rad - M_rad + Mp_rad)
    )

    # Additional longitude corrections
    sum_l += (
        3958 * math.sin(A1_rad) +
        1962 * math.sin(Lp - F_rad) +
        318 * math.sin(A2_rad)
    )

    # Sum of latitude terms (most significant from Table 47.B)
    sum_b = (
        5128122 * math.sin(F_rad) +
        280602 * math.sin(Mp_rad + F_rad) +
        277693 * math.sin(Mp_rad - F_rad) +
        173237 * math.sin(2*D_rad - F_rad) +
        55413 * math.sin(2*D_rad - Mp_rad + F_rad) +
        46271 * math.sin(2*D_rad - Mp_rad - F_rad) +
        32573 * math.sin(2*D_rad + F_rad) +
        17198 * math.sin(2*Mp_rad + F_rad) +
        9266 * math.sin(2*D_rad + Mp_rad - F_rad) +
        8822 * math.sin(2*Mp_rad - F_rad) +
        -8216 * E * math.sin(2*D_rad - M_rad - F_rad) +
        4324 * math.sin(2*D_rad - 2*Mp_rad - F_rad) +
        4200 * math.sin(2*D_rad + Mp_rad + F_rad) +
        -3359 * E * math.sin(2*D_rad + M_rad - F_rad) +
        2463 * E * math.sin(2*D_rad - M_rad - Mp_rad + F_rad) +
        2211 * E * math.sin(2*D_rad - M_rad + F_rad) +
        2065 * E * math.sin(2*D_rad - M_rad - Mp_rad - F_rad) +
        -1870 * E * math.sin(M_rad - Mp_rad - F_rad)
    )

    # Additional latitude corrections
    sum_b += (
        -2235 * math.sin(Lp) +
        382 * math.sin(A3_rad) +
        175 * math.sin(A1_rad - F_rad) +
        175 * math.sin(A1_rad + F_rad) +
        127 * math.sin(Lp - Mp_rad) +
        -115 * math.sin(Lp + Mp_rad)
    )

    # Ecliptic longitude and latitude (degrees)
    ecl_lon = Lp + sum_l / 1000000
    ecl_lat = sum_b / 1000000

    ecl_lon_rad = math.radians(ecl_lon)
    ecl_lat_rad = math.radians(ecl_lat)

    # Mean obliquity of the ecliptic
    eps = 23.439291 - 0.0130042 * T
    eps_rad = math.radians(eps)

    # Convert ecliptic to equatorial
    x_ecl = math.cos(ecl_lat_rad) * math.cos(ecl_lon_rad)
    y_ecl = math.cos(ecl_lat_rad) * math.sin(ecl_lon_rad)
    z_ecl = math.sin(ecl_lat_rad)

    x_eq = x_ecl
    y_eq = y_ecl * math.cos(eps_rad) - z_ecl * math.sin(eps_rad)
    z_eq = y_ecl * math.sin(eps_rad) + z_ecl * math.cos(eps_rad)

    ra_rad = math.atan2(y_eq, x_eq)
    dec_rad = math.asin(z_eq)

    ra_apparent = (math.degrees(ra_rad) / 15) % 24
    dec_apparent = math.degrees(dec_rad)

    # Precess from apparent (equinox of date) back to J2000
    ra_j2000, dec_j2000 = precess_date_to_j2000(ra_apparent, dec_apparent, jd)

    return ra_j2000, dec_j2000


def galactic_to_equatorial(l_deg, b_deg):
    """
    Convert Galactic coordinates to Equatorial (J2000).

    Args:
        l_deg: Galactic longitude in degrees (0-360)
        b_deg: Galactic latitude in degrees (-90 to +90)

    Returns:
        (ra_hours, dec_deg)
    """
    # Galactic coordinate system constants (J2000)
    # RA of North Galactic Pole
    ra_ngp = math.radians(192.85948)
    # Dec of North Galactic Pole
    dec_ngp = math.radians(27.12825)
    # Galactic longitude of North Celestial Pole
    l_ncp = math.radians(122.93192)

    l_rad = math.radians(l_deg)
    b_rad = math.radians(b_deg)

    # Calculate declination
    sin_dec = (math.sin(dec_ngp) * math.sin(b_rad) +
               math.cos(dec_ngp) * math.cos(b_rad) * math.cos(l_ncp - l_rad))
    dec_rad = math.asin(sin_dec)

    # Calculate right ascension
    y = math.cos(b_rad) * math.sin(l_ncp - l_rad)
    x = (math.cos(dec_ngp) * math.sin(b_rad) -
         math.sin(dec_ngp) * math.cos(b_rad) * math.cos(l_ncp - l_rad))

    ra_rad = ra_ngp + math.atan2(y, x)

    # Normalize
    ra_hours = (math.degrees(ra_rad) / 15) % 24
    dec_deg = math.degrees(dec_rad)

    return ra_hours, dec_deg


def equatorial_to_galactic(ra_hours, dec_deg):
    """
    Convert Equatorial (J2000) to Galactic coordinates.

    Args:
        ra_hours: Right Ascension in hours (0-24)
        dec_deg: Declination in degrees (-90 to +90)

    Returns:
        (l_deg, b_deg) - Galactic longitude and latitude
    """
    # Galactic coordinate system constants (J2000)
    ra_ngp = math.radians(192.85948)
    dec_ngp = math.radians(27.12825)
    l_ncp = math.radians(122.93192)

    ra_rad = math.radians(ra_hours * 15)
    dec_rad = math.radians(dec_deg)

    # Calculate galactic latitude
    sin_b = (math.sin(dec_ngp) * math.sin(dec_rad) +
             math.cos(dec_ngp) * math.cos(dec_rad) * math.cos(ra_rad - ra_ngp))
    b_rad = math.asin(sin_b)

    # Calculate galactic longitude
    y = math.cos(dec_rad) * math.sin(ra_rad - ra_ngp)
    x = (math.cos(dec_ngp) * math.sin(dec_rad) -
         math.sin(dec_ngp) * math.cos(dec_rad) * math.cos(ra_rad - ra_ngp))

    l_rad = l_ncp - math.atan2(y, x)

    l_deg = math.degrees(l_rad) % 360
    b_deg = math.degrees(b_rad)

    return l_deg, b_deg
