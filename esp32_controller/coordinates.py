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
    Uses a simplified algorithm accurate to ~1 degree.

    Returns:
        (ra_hours, dec_deg)
    """
    t = time.gmtime()
    jd = julian_date(t[0], t[1], t[2], t[3], t[4], t[5])

    # Days since J2000.0
    n = jd - 2451545.0

    # Mean longitude of the Sun (degrees)
    L = (280.460 + 0.9856474 * n) % 360

    # Mean anomaly of the Sun (degrees)
    g = (357.528 + 0.9856003 * n) % 360
    g_rad = math.radians(g)

    # Ecliptic longitude of the Sun (degrees)
    ecl_lon = L + 1.915 * math.sin(g_rad) + 0.020 * math.sin(2 * g_rad)
    ecl_lon_rad = math.radians(ecl_lon)

    # Obliquity of the ecliptic (degrees)
    obliquity = 23.439 - 0.0000004 * n
    obliquity_rad = math.radians(obliquity)

    # Convert ecliptic to equatorial coordinates
    ra_rad = math.atan2(
        math.cos(obliquity_rad) * math.sin(ecl_lon_rad),
        math.cos(ecl_lon_rad)
    )
    dec_rad = math.asin(math.sin(obliquity_rad) * math.sin(ecl_lon_rad))

    ra_hours = (math.degrees(ra_rad) / 15) % 24
    dec_deg = math.degrees(dec_rad)

    return ra_hours, dec_deg


def get_moon_position():
    """
    Calculate the Moon's RA/Dec for the current time.
    Uses a simplified algorithm accurate to ~1-2 degrees.

    Returns:
        (ra_hours, dec_deg)
    """
    t = time.gmtime()
    jd = julian_date(t[0], t[1], t[2], t[3], t[4], t[5])

    # Days since J2000.0
    n = jd - 2451545.0

    # Moon's mean longitude (degrees)
    L = (218.316 + 13.176396 * n) % 360

    # Moon's mean anomaly (degrees)
    M = (134.963 + 13.064993 * n) % 360
    M_rad = math.radians(M)

    # Moon's mean distance from ascending node (degrees)
    F = (93.272 + 13.229350 * n) % 360
    F_rad = math.radians(F)

    # Ecliptic longitude (degrees)
    ecl_lon = L + 6.289 * math.sin(M_rad)
    ecl_lon_rad = math.radians(ecl_lon)

    # Ecliptic latitude (degrees)
    ecl_lat = 5.128 * math.sin(F_rad)
    ecl_lat_rad = math.radians(ecl_lat)

    # Obliquity of the ecliptic
    obliquity = 23.439 - 0.0000004 * n
    obliquity_rad = math.radians(obliquity)

    # Convert ecliptic to equatorial
    # First convert to rectangular ecliptic
    x_ecl = math.cos(ecl_lat_rad) * math.cos(ecl_lon_rad)
    y_ecl = math.cos(ecl_lat_rad) * math.sin(ecl_lon_rad)
    z_ecl = math.sin(ecl_lat_rad)

    # Rotate to equatorial
    x_eq = x_ecl
    y_eq = y_ecl * math.cos(obliquity_rad) - z_ecl * math.sin(obliquity_rad)
    z_eq = y_ecl * math.sin(obliquity_rad) + z_ecl * math.cos(obliquity_rad)

    # Convert to RA/Dec
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
