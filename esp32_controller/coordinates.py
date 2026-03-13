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
    """Calculate Greenwich Mean Sidereal Time in hours from Julian Date"""
    T = (jd - 2451545.0) / 36525.0

    # GMST at 0h UT in seconds
    gmst_sec = 24110.54841 + 8640184.812866 * T + 0.093104 * T**2 - 6.2e-6 * T**3

    # Add rotation since 0h UT
    gmst_sec += 86400 * 1.00273790935 * ((jd - 0.5) % 1)

    # Convert to hours and normalize to 0-24
    gmst_hours = (gmst_sec / 3600) % 24

    return gmst_hours


def local_sidereal_time(jd, longitude):
    """Calculate Local Sidereal Time in hours"""
    lst = gmst(jd) + longitude / 15.0
    return lst % 24


def ra_dec_to_alt_az(ra_hours, dec_deg, lat_deg, lon_deg):
    """
    Convert RA/Dec to Alt/Az

    Args:
        ra_hours: Right Ascension in hours (0-24)
        dec_deg: Declination in degrees (-90 to +90)
        lat_deg: Observer latitude in degrees
        lon_deg: Observer longitude in degrees (west negative)

    Returns:
        (altitude, azimuth) in degrees
    """
    # Get current time from RTC
    t = time.gmtime()
    jd = julian_date(t[0], t[1], t[2], t[3], t[4], t[5])

    # Calculate hour angle
    lst = local_sidereal_time(jd, lon_deg)
    ha_hours = lst - ra_hours
    ha_rad = math.radians(ha_hours * 15)  # Convert to radians

    # Convert to radians
    dec_rad = math.radians(dec_deg)
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
    Convert Alt/Az to RA/Dec (for Stellarium feedback)

    Args:
        alt_deg: Altitude in degrees
        az_deg: Azimuth in degrees (0=N, 90=E)
        lat_deg: Observer latitude in degrees
        lon_deg: Observer longitude in degrees

    Returns:
        (ra_hours, dec_deg)
    """
    # Convert to radians
    alt_rad = math.radians(alt_deg)
    az_rad = math.radians(az_deg)
    lat_rad = math.radians(lat_deg)

    # Calculate declination
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

    # Get current LST and calculate RA
    t = time.gmtime()
    jd = julian_date(t[0], t[1], t[2], t[3], t[4], t[5])
    lst = local_sidereal_time(jd, lon_deg)

    ra_hours = lst - math.degrees(ha_rad) / 15
    ra_hours = ra_hours % 24

    dec_deg = math.degrees(dec_rad)

    return ra_hours, dec_deg


def get_sun_position():
    """
    Calculate the Sun's RA/Dec for the current time.
    Based on Meeus "Astronomical Algorithms" - accurate to ~0.01 degree.

    Returns:
        (ra_hours, dec_deg)
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

    ra_hours = (math.degrees(ra_rad) / 15) % 24
    dec_deg = math.degrees(dec_rad)

    return ra_hours, dec_deg


def get_moon_position():
    """
    Calculate the Moon's RA/Dec for the current time.
    Based on Meeus "Astronomical Algorithms" Ch. 47 - accurate to ~0.3 degree.

    Returns:
        (ra_hours, dec_deg)
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

    ra_hours = (math.degrees(ra_rad) / 15) % 24
    dec_deg = math.degrees(dec_rad)

    return ra_hours, dec_deg


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
