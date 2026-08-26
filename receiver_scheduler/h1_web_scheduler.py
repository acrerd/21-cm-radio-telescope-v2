#!/usr/bin/env python3
"""
Web-based scheduler interface for H1 Receiver.
Provides an interactive HTML/JavaScript UI for managing observation schedules.

Run with: python h1_web_scheduler.py
Then open: http://localhost:5000 on the scheduler host.
The SRT controller web UI is normally at http://192.168.50.120/, on the private
point-to-point Ethernet link between the observatory computer and the controller.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import signal
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, asdict
from typing import Optional
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
import logging
import logging.handlers
import math
import urllib.request
import urllib.error
import urllib.parse

# The observatory's surveyed position, written down in exactly one place.
from observatory import SITE_HEIGHT_M, SITE_LAT_DEG, SITE_LON_DEG

# Naming and placing recordings. Shared with b210_h1_receiver, which rolls its
# own files and must land them in the same folder under the same convention -
# the scheduler cannot import the receiver back (it pulls in GNU Radio at
# module scope), so the convention lives in a module they can both have.
import observation_files
import numpy as np

try:
    import ephem
    EPHEM_AVAILABLE = True
except ImportError:
    EPHEM_AVAILABLE = False

# Logging setup - logs to both console and rotating file
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(LOG_DIR, "scheduler.log")

log = logging.getLogger("scheduler")
log.setLevel(logging.DEBUG)

_fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(_fmt)
log.addHandler(_console)

_file = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3)
_file.setLevel(logging.DEBUG)
_file.setFormatter(_fmt)
log.addHandler(_file)

# The scan modules keep their own loggers, and handlers were only ever attached
# to this one - so everything they said went nowhere. That is tolerable for a
# ten-minute Sun raster watched from the page, and not for a horizon scan that
# runs for an hour and a half unattended overnight: the per-azimuth results and
# the "extending the cut upwards" lines are precisely what someone reads the
# next morning to find out what happened.
for _module in ("sun_scan", "horizon_scan"):
    _module_log = logging.getLogger(_module)
    _module_log.setLevel(logging.INFO)
    _module_log.addHandler(_console)
    _module_log.addHandler(_file)

# Suppress noisy Flask/werkzeug request logs
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# Configuration
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEDULE_FILE = os.path.join(_SCRIPT_DIR, "h1_schedule.json")
CONFIG_FILE = os.path.join(_SCRIPT_DIR, "scheduler_config.json")
RECEIVER_SCRIPT = os.path.join(_SCRIPT_DIR, "b210_h1_receiver.py")
# The web simulator, served from this app so the two share an origin.
SIMULATOR_DIR = os.path.join(os.path.dirname(_SCRIPT_DIR), "astro_simulator", "web")

# Default configuration - overridden by scheduler_config.json if present
_DEFAULT_CONFIG = {
    "banner_name": "H1 Receiver Scheduler",
    "banner_subtitle": "Hydrogen Line (21cm) Observation Manager",
    # The controller sits on a private link owned by this host (issue #10), so
    # this address is ours permanently rather than the observatory LAN's to
    # renumber. The fallbacks are mDNS over the same link and the controller's
    # own WiFi AP, which stays up regardless of the Ethernet settings.
    "srt_controller_url": "http://192.168.50.120",
    "srt_controller_fallback_urls": [
        "http://srt-controller.local",
        "http://192.168.4.1",
    ],
    "slew_timeout": 300,
    "homing_timeout": 300,
    "calibration_home_before_scan": True,
    "position_tolerance": 0.5,
    "python_path": "",
    "data_output_folder": os.path.join(_SCRIPT_DIR, "data"),
    "log_lines": 100,
    "sound_enabled": True,
    "platformio_path": "",
    "firmware_update_env": "wt32-eth01-ota",
    "receiver_python_path": "/home/astro/radioconda/bin/python",
    # The true site, identical to OBSERVER_LAT/OBSERVER_LON in
    # esp32_controller_arduino/src/config.h and to sun_scan.py.
    "observer_lat": SITE_LAT_DEG,
    "observer_lon": SITE_LON_DEG,
    "observer_elevation": 50,
    "min_elevation": 10.0,
    # The fixed instrument (issue #27): the B210's tuning is not a
    # per-observation choice. These are the numbers every scheduled
    # observation records with, and they are normally never touched; the
    # defaults and the reasoning are in tuning.py. Unset (None) means the
    # default. Shown read-only on the Settings tab.
    "receiver_lo_hz": None,
    "receiver_sample_rate_hz": None,
    "receiver_gain_db": None,
    "receiver_wide_channels": None,
    "receiver_h1_band_hz": None,
    "receiver_h1_channels": None,
    # `obstruction_sectors` used to live here: a hand-entered
    # [az_min, az_max, min_sun_alt] list, in practice the single blanket entry
    # [[45, 120, 30]] read off one calibration day. It was always a stand-in
    # for a measurement nobody had yet, and it was retired on 2026-08-25 once
    # the measurement existed. Everything that consumed it now derives its
    # sectors from the measured horizon profile instead (horizon_store), which
    # knows each azimuth separately rather than averaging a whole quadrant into
    # one number - the eastern floors it replaced range from 15 to 30 deg.
    #
    # The consumers still take sector triples, so they stayed pure functions of
    # what they are handed; only the source changed.
    # Whether to check pointing against the *measured* horizon profile (see
    # horizon_store) rather than only the flat min_elevation and the hand-
    # entered sectors above. On by default, and advisory by design: it says
    # what is behind the trees and never refuses. A profile can be months old -
    # the trees will have grown or been cut since - so stopping an observation
    # on the word of a stale measurement is worse than knowingly taking a
    # contaminated one. Each tab and each scheduled observation carries its own
    # copy so one can be turned off without turning off the rest.
    "respect_local_horizon": True,
    # Safety camera: a USB webcam watching the dish. One frame per request, on
    # demand - see /api/camera/snapshot.
    "camera_device": "/dev/video0",
    "camera_resolution": "640x480",
    # Which PipeWire node to capture from. Empty means the default video
    # source, which is right while there is only one camera on the machine.
    "camera_pipewire_target": "",
}


def load_config() -> dict:
    """Load config from JSON file, merged with defaults."""
    cfg = dict(_DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                cfg.update(json.load(f))
        except Exception as e:
            log.error("Error loading config: %s", e)
    return cfg


def save_config(cfg: dict):
    """Save config to JSON file."""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)
    log.info("Configuration saved")


def get_config_value(key: str):
    """Get a single config value."""
    return load_config().get(key, _DEFAULT_CONFIG.get(key))


# Guards the mutable controller settings below. They are written from Flask
# request threads (the config POST) and from whichever thread happens to make
# the API call that promotes a working fallback URL, while the scheduler thread
# reads them continuously.
controller_settings_lock = threading.RLock()

# Load initial config
_config = load_config()
SRT_CONTROLLER_URL = _config["srt_controller_url"] or None
SRT_SLEW_TIMEOUT = _config["slew_timeout"]
SRT_POSITION_TOLERANCE = _config["position_tolerance"]
PYTHON_PATH = _config["python_path"] or None
ESP32_FIRMWARE_DIR = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "esp32_controller_arduino"))
FIRMWARE_UPDATE_ENV = _config.get("firmware_update_env", "wt32-eth01-ota")
RADIOCONDA_REEXEC_ENV = "H1_SCHEDULER_RADIOCONDA_REEXEC"

# How long a preempting scheduled observation waits for a cancelled Sun
# scan / calibration day to release the SDR before giving up on this
# attempt (the scheduler retries, subject to the start-failure backoff).
SUN_SCAN_PREEMPT_TIMEOUT = 600

# Before a pointing model is pushed to the telescope, the mount tilt has to be
# measured rather than merely fitted, and the model has to describe the scans.
# Sun positions from a single half-day leave the tilts degenerate with the
# constant offsets, which shows up as low significance rather than as a
# geometry failure.
CALDAY_MIN_TILT_SIGNIFICANCE = 3.0
CALDAY_MAX_REDUCED_CHI_SQUARED = 4.0

firmware_update_lock = threading.Lock()
firmware_update_state = {
    "running": False,
    "success": None,
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "message": "",
    "output": [],
}


def _normalize_controller_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return url.strip().rstrip("/") or None


def _controller_url_candidates() -> list[str]:
    cfg = load_config()
    with controller_settings_lock:
        current = SRT_CONTROLLER_URL
    candidates = [
        os.environ.get("SRT_CONTROLLER_URL"),
        current,
        cfg.get("srt_controller_url"),
    ]
    candidates.extend(cfg.get("srt_controller_fallback_urls", []))

    urls = []
    seen = set()
    for url in candidates:
        normalized = _normalize_controller_url(url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
    return urls

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    """Allow the ESP32-served control page to call this local scheduler API.

    Only the configured controller origins are allowed - a wildcard here
    would let any web page open in a browser on this machine drive the
    scheduler (including the firmware-update endpoint).
    """
    origin = _normalize_controller_url(request.headers.get("Origin"))
    if origin and origin in _controller_url_candidates():
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# =============================================================================
# SRT Telescope Controller Integration
# =============================================================================

def dms_to_decimal(deg: int, min: int, sec: float, is_ra: bool = False) -> float:
    """Convert degrees/minutes/seconds to decimal.

    For RA (is_ra=True): input is hours/minutes/seconds, output is decimal hours.
    For Dec/Alt/Az/Galactic: input and output are degrees.
    """
    sign = -1 if deg < 0 else 1
    decimal = abs(deg) + min / 60.0 + sec / 3600.0
    return sign * decimal


def srt_api_call(endpoint: str, params: Optional[dict] = None,
                 json_body: Optional[dict] = None,
                 timeout: int = 3) -> Optional[dict]:
    """Make an API call to the SRT controller.

    With json_body the call is a POST carrying that document. The controller
    takes the pointing model that way rather than as query arguments: it is a
    document with a schema, applied whole or not at all, and a URL that a proxy
    or a browser can truncate is the wrong carrier for that.

    Returns JSON response as dict, or None on error.
    """
    global SRT_CONTROLLER_URL

    candidates = _controller_url_candidates()
    if not candidates:
        return None

    data = None
    headers = {}
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"

    last_error = None
    for base_url in candidates:
        url = f"{base_url}{endpoint}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        try:
            request = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode(errors="replace")
                result = json.loads(payload, strict=False)
                with controller_settings_lock:
                    if base_url != SRT_CONTROLLER_URL:
                        log.info("SRT controller reachable at %s", base_url)
                        SRT_CONTROLLER_URL = base_url
                return result
        except urllib.error.HTTPError as e:
            # The controller answers a rejected request with a 4xx and a JSON
            # body saying why. urlopen raises before the caller ever sees it, so
            # it is read here - "Term IE is 45.000 deg" is worth relaying, and a
            # host that answered at all is not one to fail over from.
            try:
                result = json.loads(e.read().decode(errors="replace"), strict=False)
            except Exception:
                last_error = e
                log.warning("SRT API error via %s: %s", base_url, e)
                continue
            log.warning("SRT API rejected %s: %s", endpoint,
                        result.get("error", result))
            return result
        except Exception as e:
            last_error = e
            log.warning("SRT API error via %s: %s", base_url, e)

    log.warning("SRT connection error after trying %s: %s", ", ".join(candidates), last_error)
    return None


def srt_get_settings() -> Optional[dict]:
    """Get controller settings (observer location, home position, etc)."""
    return srt_api_call("/settings")


def sync_observer_from_controller():
    """Fetch observer location from the SRT controller and update config."""
    if not SRT_CONTROLLER_URL:
        return
    settings = srt_get_settings()
    if not settings:
        return
    lat = settings.get('observer_lat')
    lon = settings.get('observer_lon')
    if lat is not None and lon is not None:
        cfg = load_config()
        if cfg.get('observer_lat') != lat or cfg.get('observer_lon') != lon:
            cfg['observer_lat'] = lat
            cfg['observer_lon'] = lon
            save_config(cfg)
            log.info("Observer location synced from controller: lat=%.6f lon=%.6f", lat, lon)


def srt_get_status() -> Optional[dict]:
    """Get current telescope status (position, tracking, etc)."""
    return srt_api_call("/status")


def srt_get_tracking() -> Optional[dict]:
    """Get current tracking state."""
    return srt_api_call("/tracking")


def srt_point_telescope(obs: dict) -> bool:
    """Command the telescope to point at the observation's target coordinates.

    Converts DMS coordinates to decimal and calls the appropriate ESP32 endpoint
    based on the coordinate system.

    Returns True if command was sent successfully.
    """
    if not SRT_CONTROLLER_URL:
        log.info("SRT controller URL not configured - skipping telescope control")
        return True  # Don't block observation if telescope control disabled

    coord_system = obs.get('coord_system', 'altaz')
    drift_frame = obs.get('drift_frame', 'radec')

    # Convert DMS to decimal
    coord1 = dms_to_decimal(
        obs.get('coord1_deg', 0),
        obs.get('coord1_min', 0),
        obs.get('coord1_sec', 0.0),
        is_ra=(coord_system == 'radec'
               or (coord_system == 'drift' and drift_frame == 'radec'))
    )
    coord2 = dms_to_decimal(
        obs.get('coord2_deg', 0),
        obs.get('coord2_min', 0),
        obs.get('coord2_sec', 0.0),
        is_ra=False
    )

    # Call appropriate endpoint based on coordinate system
    if coord_system == 'altaz':
        # Alt/Az: use direct control (no tracking needed for fixed position)
        endpoint = "/direct"
        params = {"alt": coord1, "az": coord2}
        log.info("SRT commanding telescope to Alt=%.2f° Az=%.2f°", coord1, coord2)

    elif coord_system == 'radec':
        # RA/Dec: use tracking mode (follows as Earth rotates)
        endpoint = "/track/radec"
        params = {"ra": coord1, "dec": coord2}
        log.info("SRT commanding telescope to track RA=%.3fh Dec=%.2f°", coord1, coord2)

    elif coord_system == 'galactic':
        # Galactic: use tracking mode
        endpoint = "/track/galactic"
        params = {"l": coord1, "b": coord2}
        log.info("SRT commanding telescope to track Gal l=%.2f° b=%.2f°", coord1, coord2)

    elif coord_system == 'object':
        # Named solar system object: use dedicated endpoint
        object_name = obs.get('object_name', '').lower()
        if object_name == 'sun':
            endpoint = "/track/sun"
            params = {}
            log.info("SRT commanding telescope to track the Sun")
        elif object_name == 'moon':
            endpoint = "/track/moon"
            params = {}
            log.info("SRT commanding telescope to track the Moon")
        else:
            log.error("SRT unknown object: %s", object_name)
            return False

    elif coord_system == 'drift':
        # Drift scan: park the dish where the source will be at the slot
        # mid-point (the beam-crossing time T) and leave tracking off.
        beam_time = drift_beam_time(obs)
        pointing = compute_drift_pointing(drift_frame, coord1, coord2, beam_time,
                                          obs.get('object_name', ''))
        if pointing is None:
            log.error("SRT drift scan requires PyEphem on the scheduler host")
            return False
        alt, az = pointing
        if not (DRIFT_MIN_ALT <= alt <= DRIFT_MAX_ALT and 0.0 <= az <= DRIFT_MAX_AZ):
            log.error("SRT drift pointing unreachable: Alt=%.2f° Az=%.2f° at %s",
                      alt, az, beam_time.strftime('%Y-%m-%d %H:%M'))
            return False
        # The mount parks on a 0.5 deg drive grid, so "where the source is at
        # T" is not somewhere it can park. Park instead on the grid point the
        # source's track passes closest to, and record when it does: the
        # crossing is then exact in time and the miss is only across the
        # drift. Needs the controller's model to place the grid on the sky.
        park = plan_drift_parking(obs, beam_time, srt_pointing_terms())
        if park is not None:
            alt, az = park['true_alt'], park['true_az']
            obs['drift_drive_alt'] = park['drive_alt']
            obs['drift_drive_az'] = park['drive_az']
            obs['drift_crossing_time'] = _crossing_time_str(park['crossing'])
            obs['drift_crossing_offset_deg'] = round(park['offset_deg'], 3)
            log.info("SRT drift scan: parking on drive grid Alt=%.1f Az=%.1f; the source "
                     "crosses it at %s, %.3f deg off beam centre (%+.0f s from T)",
                     park['drive_alt'], park['drive_az'],
                     park['crossing'].strftime('%H:%M:%S'), park['offset_deg'],
                     (park['crossing'] - beam_time).total_seconds())
        else:
            log.warning("SRT drift scan: controller pointing model not readable - parking "
                        "at the source's position at T; the controller will round it "
                        "by up to a quarter of a degree per axis")
        # Stash the computed pointing so it lands in the observation metadata
        obs['drift_beam_time'] = beam_time.strftime('%Y-%m-%d %H:%M')
        obs['drift_alt'] = round(alt, 3)
        obs['drift_az'] = round(az, 3)
        endpoint = "/direct"
        params = {"alt": alt, "az": az}
        log.info("SRT drift scan: fixed pointing Alt=%.2f° Az=%.2f°, beam crossing at %s",
                 alt, az, beam_time.strftime('%H:%M'))

    elif coord_system == 'satellite':
        # Satellite: tracking thread handles continuous updates
        log.info("SRT satellite mode - tracking thread will send updates")
        return True

    else:
        log.error("SRT unknown coordinate system: %s", coord_system)
        return False

    result = srt_api_call(endpoint, params)
    if result and result.get('ok'):
        log.info("SRT command accepted")
        return True
    else:
        log.error("SRT command failed: %s", result)
        return False


# The mount quantises to half a degree (two encoder pulses per degree), so
# arrival cannot be judged more tightly than that. 0.6 leaves a little margin
# over one quantum without being loose enough to accept the wrong target.
def horizon_obstruction_sectors(cfg=None, respect=None, margin_deg=None):
    """Obstruction sectors derived from the measured horizon.

    The single source of obstruction knowledge since the hand-entered sectors
    were retired. Returns an empty list when no horizon has been measured,
    which means nothing is excluded - the honest answer when nothing is known,
    and the reason a site should run a horizon scan before trusting a
    calibration.

    `margin_deg` is how far above the measured floor the *beam centre* has to
    sit. It defaults to the full beamwidth; callers pointing something with
    more reach than a single beam pass a larger one - see
    `sun_raster_obstruction_sectors`.
    """
    cfg = cfg if cfg is not None else load_config()
    if respect is None:
        respect = bool(cfg.get("respect_local_horizon", True))
    if not respect:
        return []
    import horizon_store
    profile = horizon_store.load_active()
    if not profile:
        return []
    return horizon_store.horizon_sectors(profile, margin_deg=margin_deg)


def sun_raster_obstruction_sectors(cfg=None, n=None, spacing_deg=None):
    """Obstruction sectors for a Sun raster, from its centre alone.

    Used in exactly one place: excluding already-recorded scans from the
    pointing fit. Scan records store `sun_alt_deg` and `sun_az_deg` but not the
    raster's `n` or spacing, so the geometry cannot be reconstructed and the
    extent has to be an allowance added to the Sun's position instead. Anything
    checking a raster it is *about* to drive should enumerate the points
    instead - `sun_scan.raster_obstruction` - which is exact, and which is what
    the calibration day and the Sun scan both do.

    The allowance below is that approximation.

    A raster is not a single pointing. The lowest row sits (n-1)/2 * spacing
    below the Sun, so the Sun's own altitude clearing the trees is not enough -
    the bottom of the raster has to clear them too, or the foliage climbs into
    the lower rows, puts a ramp under the source and drags the fitted centroid
    down into it. That is what happened on 2026-08-20: four scans at Sun
    altitudes of 18-29 deg in the east fitted 0.5-1.2 deg low while evening
    scans at the *same altitudes* in the west were clean.

    This is the whole content of the retired sectors' 30 deg eastern floor.
    That number was read off a calibration day with the raster folded in, and
    dropping to a beam-only margin when the sectors went would have quietly
    readmitted exactly those scans. Here the same allowance is computed from
    the raster geometry rather than remembered as a constant.
    """
    cfg = cfg if cfg is not None else load_config()
    if n is None:
        n = 5
    if spacing_deg is None:
        spacing_deg = 1.5
    import horizon_store
    half_extent = max(0.0, (float(n) - 1.0) / 2.0 * float(spacing_deg))
    return horizon_obstruction_sectors(
        cfg, margin_deg=horizon_store.beam_margin_deg() + half_extent)


def local_horizon_warning(alt_deg=None, az_deg=None, respect=None):
    """Say whether this sky position is behind the measured horizon.

    Returns a sentence, or None when the position is clear, when no horizon has
    been measured, or when the caller has the check switched off.

    Called with no position, it reads where the dish actually is from /status -
    `true_alt`/`true_az`, the sky frame, which is the frame the horizon was
    measured in. Doing it that way rather than converting each observation's
    own coordinates means one code path serves alt/az, RA/Dec, galactic and
    named objects alike, with no second ephemeris to disagree with the
    controller's.

    Advisory only. It never prevents anything - see `respect_local_horizon` in
    the default config for why.
    """
    if respect is None:
        respect = bool(get_config_value("respect_local_horizon"))
    if not respect:
        return None
    import horizon_store
    profile = horizon_store.load_active()
    if not profile:
        return None
    if alt_deg is None or az_deg is None:
        status = srt_get_status()
        if not status:
            return None
        alt_deg = status.get("true_alt", status.get("alt"))
        az_deg = status.get("true_az", status.get("az"))
        if alt_deg is None or az_deg is None:
            return None
    return horizon_store.horizon_warning(profile, float(alt_deg), float(az_deg))


SRT_ARRIVAL_TOLERANCE_DEG = 0.6

# Nothing is accepted as "arrived" until this long after the command. The
# controller does not begin moving the instant it answers, and its reported
# target lags the command as well, so an immediate check sees the mount sitting
# still on the *previous* target and calls that success. See the docstring.
SRT_SLEW_START_GRACE_S = 5.0


def _az_difference(a: float, b: float) -> float:
    """Smallest angle between two azimuths, across the 0/360 join."""
    return abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)


# Sky-frame arrival tolerance. The drive quantises to half a degree, the
# pointing model contributes a little more, and nothing here needs to resolve
# better than that.
SRT_SKY_TOLERANCE_DEG = 1.0


def _sky_separation_deg(ra1_h, dec1_deg, ra2_h, dec2_deg) -> float:
    """Angle between two sky positions. RA in hours, declination in degrees."""
    ra1 = math.radians(float(ra1_h) * 15.0)
    ra2 = math.radians(float(ra2_h) * 15.0)
    d1, d2 = math.radians(float(dec1_deg)), math.radians(float(dec2_deg))
    cos_sep = (math.sin(d1) * math.sin(d2)
               + math.cos(d1) * math.cos(d2) * math.cos(ra1 - ra2))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sep))))


def srt_wait_for_slew(timeout: Optional[int] = None,
                      cancel_event: Optional[threading.Event] = None) -> bool:
    """Wait until the telescope has actually reached what it was told to.

    Polls /status until the reported drive position matches the drive target the
    controller is holding, and it is no longer slewing, for two consecutive
    polls.

    Checking the position matters, and this function used not to. It returned as
    soon as `is_slewing` was false, which is false for the first couple of
    seconds after any command because the mount has not started moving yet. On
    2026-08-24 a calibration commanded to l=36 b=40 - alt 33, azimuth 108 -
    reported "slew complete in 2.0s" with the mount still parked at alt 75,
    azimuth 286 from the previous target, and the recording then ran while the
    dish swept fifty degrees across the sky. Every scheduled observation went
    through the same check, so this was never only a calibration problem.

    Judged drive against drive: /status reports the mount's drive position and
    the drive target it is holding, and comparing a drive reading against a sky
    target would be out by the whole pointing model, so the slew would never be
    seen to finish. For a tracking target the reported target moves with the sky,
    which is correct - "arrived" then means keeping up with it.

    Returns True on arrival, False on timeout, error, or cancellation. A
    controller too old to report its target falls back to the flag alone, and
    says so, because refusing to observe at all would be worse.
    """
    if not SRT_CONTROLLER_URL:
        return True

    timeout = timeout or SRT_SLEW_TIMEOUT
    start_time = time.time()
    log.info("SRT waiting for slew to complete...")

    settled = 0
    warned_no_target = False
    while time.time() - start_time < timeout:
        if cancel_event is not None and cancel_event.is_set():
            log.info("SRT slew wait aborted")
            return False

        status = srt_get_status()
        if status:
            elapsed = time.time() - start_time
            is_slewing = bool(status.get('is_slewing', False))
            alt = status.get('alt')
            az = status.get('az')
            target_alt = status.get('target_alt')
            target_az = status.get('target_az')

            # While tracking, the honest question is whether the dish points at
            # what it has been told to track, and both halves of that are
            # available live: /status gives the RA/Dec the dish is on, /tracking
            # gives the RA/Dec it has adopted. Neither lags a command, because
            # the controller sets its tracking target synchronously inside the
            # request handler, before answering. The drive-frame target does
            # lag - it is scraped from the Due's status line - and while the
            # mount was still following the *previous* target it kept reporting
            # that one, so a drive-frame comparison agreed with itself and
            # called a fifty-degree error an arrival.
            tracking = srt_get_tracking() or {}
            if tracking.get('enabled') and status.get('ra') is not None:
                try:
                    sep = _sky_separation_deg(status['ra'], status['dec'],
                                              tracking['ra'], tracking['dec'])
                except (TypeError, ValueError, KeyError):
                    sep = float('inf')
                arrived = not is_slewing and sep <= SRT_SKY_TOLERANCE_DEG
                if arrived and elapsed >= 1.0:
                    settled += 1
                    if settled >= 2:
                        log.info("SRT arrived in %.1fs - %.2f deg from the "
                                 "tracked target, drive Alt=%.2f Az=%.2f",
                                 elapsed, sep, float(alt or 0), float(az or 0))
                        return True
                else:
                    settled = 0
                time.sleep(1.0)
                continue

            if target_alt is None or target_az is None:
                if not warned_no_target:
                    log.warning("SRT controller does not report its target, so "
                                "arrival cannot be verified - falling back to "
                                "the slewing flag alone")
                    warned_no_target = True
                arrived = not is_slewing
                d_alt = d_az = float('nan')
            else:
                try:
                    d_alt = abs(float(alt) - float(target_alt))
                    d_az = _az_difference(az, target_az)
                except (TypeError, ValueError):
                    d_alt = d_az = float('inf')
                arrived = (not is_slewing
                           and d_alt <= SRT_ARRIVAL_TOLERANCE_DEG
                           and d_az <= SRT_ARRIVAL_TOLERANCE_DEG)

            # The grace period is the whole point: without it the very first
            # poll sees a mount that has not started moving and calls it done.
            if arrived and elapsed >= SRT_SLEW_START_GRACE_S:
                settled += 1
                if settled >= 2:
                    log.info("SRT arrived in %.1fs - drive Alt=%.2f Az=%.2f "
                             "(%.2f, %.2f from target)",
                             elapsed, float(alt or 0), float(az or 0), d_alt, d_az)
                    return True
            else:
                settled = 0

        time.sleep(1.0)

    status = srt_get_status() or {}
    tracking = srt_get_tracking() or {}
    if tracking.get('enabled') and status.get('ra') is not None:
        try:
            sep = _sky_separation_deg(status['ra'], status['dec'],
                                      tracking['ra'], tracking['dec'])
        except (TypeError, ValueError, KeyError):
            sep = float('nan')
        log.warning("SRT timeout waiting for slew: still %.2f deg from %s, at "
                    "drive Alt=%s Az=%s", sep,
                    tracking.get('target_name') or 'the tracked target',
                    status.get('alt'), status.get('az'))
    else:
        log.warning("SRT timeout waiting for slew: at Alt=%s Az=%s, target Alt=%s Az=%s",
                    status.get('alt'), status.get('az'),
                    status.get('target_alt'), status.get('target_az'))
    return False


def srt_set_calibrator(on: bool) -> bool:
    """Turn the calibrator noise source on or off."""
    if not SRT_CONTROLLER_URL:
        return True
    result = srt_api_call("/calibrator", {"on": "1" if on else "0"})
    if result and result.get('ok'):
        log.info("Calibrator %s", "ON" if on else "OFF")
        return True
    else:
        log.error("Failed to set calibrator: %s", result)
        return False


def srt_go_position(name: str, alt: float, az: float) -> bool:
    """Send telescope to a named position.

    Stow goes through /go-home, not /direct. Parking is mechanical - "leave the
    mount here" - so the controller holds settings.stowAlt/stowAz in DRIVE
    coordinates and deliberately bypasses the pointing model on that path.
    /direct takes a TRUE sky position and applies the model, so stowing through
    it puts the stow in the wrong frame. At the default zenith stow that is not
    academic: azimuth is degenerate at the pole and the model's tan(alt) term
    asks for a ~10 deg azimuth correction that moves the beam by nothing, so the
    dish parked at drive azimuth 170 instead of 180 and swung 10 deg to get
    there. Using /go-home also honours whatever stow the controller is actually
    configured with, rather than assuming the 90/180 default.
    """
    if not SRT_CONTROLLER_URL:
        return True
    if name == "home":
        result = srt_api_call("/home")
    elif name == "stow":
        result = srt_api_call("/go-home")
    else:
        result = srt_api_call("/direct", {"alt": alt, "az": az})
    if result and result.get('ok'):
        if name == "home":
            log.info("Telescope running the physical homing sequence")
        else:
            log.info("Telescope going to %s (Alt=%.1f° Az=%.1f°)", name, alt, az)
        return True
    else:
        log.error("Failed to send telescope to %s: %s", name, result)
        return False


def srt_home_and_wait(timeout: int = 300,
                      cancel_event: Optional[threading.Event] = None) -> dict:
    """Run the Due physical homing sequence and wait for a new Ready state."""
    result = srt_api_call("/home")
    if not (result and result.get("ok")):
        raise RuntimeError(f"SRT controller rejected the homing command: {result}")

    log.info("Calibration: physical homing sequence requested")
    started = False
    started_at = time.time()
    last_status = None
    while time.time() - started_at < timeout:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Sun scan cancelled during telescope homing")
        status = srt_get_status()
        if status:
            last_status = status
            state = str(status.get("status", "")).strip().lower()
            if status.get("fault_active") or state == "fault":
                detail = status.get("fault") or status.get("status") or "unknown fault"
                raise RuntimeError(f"Telescope homing failed: {detail}")
            if state == "homing":
                started = True
            elif started and state == "ready" and not status.get("is_slewing", False):
                log.info("Calibration: physical homing complete at drive Alt=%.2f° Az=%.2f°",
                         float(status.get("alt", 0.0)), float(status.get("az", 0.0)))
                return status
        if not started and time.time() - started_at >= 10:
            raise RuntimeError(
                "Telescope did not begin the physical homing sequence; "
                f"last controller status was {last_status}")
        time.sleep(0.5)

    raise RuntimeError(
        f"Telescope homing timed out after {timeout}s; last status was {last_status}")


_HOMING_LIMIT_MESSAGES = {"Homing: Azimuth limit reached": "az",
                          "Homing: Altitude limit reached": "alt"}
_HOMING_POSITION_RE = re.compile(r"Alt:(-?\d+(?:\.\d+)?)\s+Az:(-?\d+(?:\.\d+)?)")


def _homing_counters(messages: list) -> dict:
    """What the encoder counters read as each axis hit its stop, per approach.

    `messages` are the Due's lines in order. The Due keeps reporting its
    position while `driveToLimits` runs, so the last position line before
    "Homing: <axis> limit reached" is the counter at the stall. Each axis
    reaches its stop twice - the first approach, from wherever the mount was,
    and the re-approach after backing off 5 degrees - so the first reading is
    the count error accumulated since the previous homing (the stop is the
    true zero) and the second is the repeatability of the stop itself.
    Returns {'first': {'alt': x, 'az': y}, 'second': {...}}, with an axis
    missing where its lines were not captured.
    """
    out = {"first": {}, "second": {}}
    last_pos = None
    for msg in messages:
        m = _HOMING_POSITION_RE.search(msg)
        if m:
            last_pos = (float(m.group(1)), float(m.group(2)))
            continue
        axis = _HOMING_LIMIT_MESSAGES.get(msg.strip())
        if axis and last_pos is not None:
            reading = last_pos[0] if axis == "alt" else last_pos[1]
            phase = "first" if axis not in out["first"] else "second"
            out[phase][axis] = reading
    return out


def srt_home_with_report(timeout: int = 300,
                         cancel_event: Optional[threading.Event] = None) -> dict:
    """Run the physical homing and report what the counters read at the stops.

    The wait is srt_home_and_wait's; on top of it the controller's serial
    log is polled while the homing runs, because that buffer holds only the
    last 30 lines - fifteen seconds of status - and the reading at the stall
    is gone by the time the sequence finishes (learned the hard way on
    2026-08-26, when a homing run without this capture lost the one
    measurement it was for). Issue #24 is the Due printing the number itself.

    Returns the final /status plus a 'counters' entry from _homing_counters.
    """
    result = srt_api_call("/home")
    if not (result and result.get("ok")):
        raise RuntimeError(f"SRT controller rejected the homing command: {result}")
    log.info("Physical homing sequence requested (both axes into their stops)")

    seen = set()
    messages = []

    def poll_serial():
        entries = srt_api_call("/serial/log")
        if not isinstance(entries, list):
            return
        for e in entries:
            if not isinstance(e, dict) or e.get("dir") != "RX":
                continue
            key = (e.get("time"), e.get("msg"))
            if key in seen:
                continue
            seen.add(key)
            messages.append(str(e.get("msg", "")))

    started = False
    started_at = time.time()
    last_status = None
    while time.time() - started_at < timeout:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("cancelled during telescope homing")
        poll_serial()
        status = srt_get_status()
        if status:
            last_status = status
            state = str(status.get("status", "")).strip().lower()
            if status.get("fault_active") or state == "fault":
                detail = status.get("fault") or status.get("status") or "unknown fault"
                raise RuntimeError(f"Telescope homing failed: {detail}")
            if state == "homing":
                started = True
            elif started and state == "ready" and not status.get("is_slewing", False):
                poll_serial()
                counters = _homing_counters(messages)
                first, second = counters["first"], counters["second"]
                fmt = lambda d: ("Alt=%s Az=%s" % (
                    ("%.1f" % d["alt"]) if "alt" in d else "?",
                    ("%.1f" % d["az"]) if "az" in d else "?"))
                log.info("Homing complete at drive Alt=%.2f Az=%.2f. Counters at the stops: "
                         "first approach %s (count error accumulated since the last homing), "
                         "re-approach %s (repeatability)",
                         float(status.get("alt", 0.0)), float(status.get("az", 0.0)),
                         fmt(first), fmt(second))
                if not first:
                    log.warning("Homing: the Due's limit messages were not captured from "
                                "/serial/log, so the count error is unknown")
                return dict(status, counters=counters)
        if not started and time.time() - started_at >= 10:
            raise RuntimeError(
                "Telescope did not begin the physical homing sequence; "
                f"last controller status was {last_status}")
        time.sleep(0.5)

    raise RuntimeError(
        f"Telescope homing timed out after {timeout}s; last status was {last_status}")


def srt_stop_tracking() -> bool:
    """Stop telescope tracking."""
    if not SRT_CONTROLLER_URL:
        return True

    result = srt_api_call("/tracking/enable", {"enable": "0"})
    return bool(result and result.get('ok', False))


def _controller_host() -> Optional[str]:
    """Return the configured ESP32 host for PlatformIO OTA upload."""
    for url in _controller_url_candidates():
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname:
            return parsed.hostname
    return None


def _find_platformio() -> Optional[str]:
    """Locate a PlatformIO CLI executable."""
    cfg = load_config()
    configured = cfg.get("platformio_path")
    if configured:
        return configured

    for name in ("pio", "platformio"):
        found = shutil.which(name)
        if found:
            return found

    candidates = [
        os.path.expanduser("~/.platformio/penv/bin/pio"),
        os.path.expanduser("~/.platformio/penv/Scripts/pio.exe"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _set_firmware_state(**kwargs):
    with firmware_update_lock:
        firmware_update_state.update(kwargs)


def _append_firmware_output(line: str):
    with firmware_update_lock:
        firmware_update_state["output"].append(line.rstrip())
        firmware_update_state["output"] = firmware_update_state["output"][-200:]


def _run_firmware_update():
    """Build and upload ESP32 firmware over OTA in a background thread."""
    pio = _find_platformio()
    if not pio:
        _set_firmware_state(
            running=False,
            success=False,
            finished_at=datetime.now().isoformat(),
            returncode=None,
            message="PlatformIO CLI was not found. Set platformio_path in scheduler_config.json or install pio.",
        )
        return

    upload_host = _controller_host()
    if not upload_host:
        _set_firmware_state(
            running=False,
            success=False,
            finished_at=datetime.now().isoformat(),
            returncode=None,
            message="No SRT controller URL is configured for OTA upload.",
        )
        return

    env_name = load_config().get("firmware_update_env", FIRMWARE_UPDATE_ENV)
    cmd = [pio, "run", "-e", env_name, "-t", "upload", "--upload-port", upload_host]
    _set_firmware_state(message="Running: " + " ".join(cmd), output=[])
    log.info("Firmware update starting: %s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=ESP32_FIRMWARE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            _append_firmware_output(line)
            log.info("firmware: %s", line.rstrip())
        returncode = proc.wait()
        success = returncode == 0
        _set_firmware_state(
            running=False,
            success=success,
            finished_at=datetime.now().isoformat(),
            returncode=returncode,
            message=(
                "Firmware upload complete. The ESP32 is rebooting; the controller website may be unavailable for up to about 100 seconds."
                if success else f"Firmware update failed with exit code {returncode}."
            ),
        )
        log.info("Firmware update %s", "complete" if success else f"failed ({returncode})")
    except Exception as exc:
        log.error("Firmware update error: %s", exc, exc_info=True)
        _set_firmware_state(
            running=False,
            success=False,
            finished_at=datetime.now().isoformat(),
            returncode=None,
            message=f"Firmware update error: {exc}",
        )


# =============================================================================
# Satellite Tracking (TLE)
# =============================================================================

def _get_observer() -> 'ephem.Observer':
    """Create a PyEphem Observer from config."""
    obs = ephem.Observer()
    obs.lat = str(get_config_value("observer_lat"))
    obs.lon = str(get_config_value("observer_lon"))
    obs.elevation = get_config_value("observer_elevation")
    return obs


# Mount limits used to sanity-check computed drift-scan pointings. Alt is
# clamped to the horizon and the mechanical 90 deg stop; the azimuth limit
# switch sits at ~355 deg so 355-360 is a dead zone the mount cannot reach.
DRIFT_MIN_ALT = 0.0
DRIFT_MAX_ALT = 90.0
DRIFT_MAX_AZ = 355.0


def _local_to_ephem_utc(when_local: datetime) -> datetime:
    """Convert a naive local datetime to the naive UTC datetime PyEphem expects."""
    return when_local.astimezone().astimezone(timezone.utc).replace(tzinfo=None)


DRIFT_OBJECTS = {'sun': lambda: ephem.Sun(), 'moon': lambda: ephem.Moon(),
                 'jupiter': lambda: ephem.Jupiter()} if EPHEM_AVAILABLE else {}


def _drift_body(frame: str, coord1: float, coord2: float, object_name: str = ''):
    """The body a drift scan parks for: a fixed RA/Dec or l/b, or a solar
    system object by name - the Sun or Moon drifting through a parked beam,
    which ephem places at the crossing time like any other body. Added
    2026-08-26 after the Sun's galactic coordinates were computed by hand
    for a pointing test and came out pointing at the galactic centre."""
    if frame == 'object':
        name = str(object_name or '').strip().lower()
        if name not in DRIFT_OBJECTS:
            raise ValueError("no drift-scan object called %r (sun, moon, jupiter)" % object_name)
        return DRIFT_OBJECTS[name]()
    if frame == 'galactic':
        gal = ephem.Galactic(math.radians(coord1), math.radians(coord2),
                             epoch=ephem.J2000)
        eq = ephem.Equatorial(gal)
        ra, dec = eq.ra, eq.dec
    else:
        ra = math.radians(coord1 * 15.0)
        dec = math.radians(coord2)
    body = ephem.FixedBody()
    body._ra = ra
    body._dec = dec
    body._epoch = ephem.J2000
    return body


def observation_altaz_at(obs: dict, when_local: datetime) -> Optional[tuple]:
    """Where an observation's target sits at a given local time, in the sky frame.

    Returns (alt, az) in degrees, or None when the target has no position this
    code can work out - a satellite, whose window comes from its own pass
    prediction, or anything PyEphem is not available for.

    Note what "the target" means for the two parked modes. An alt/az
    observation and a drift scan both park the dish and leave it: the pointing
    does not change through the observation, so their answer is the same at
    every time asked. That is not a special case to code around, it just means
    a trim of one of those is all-or-nothing.
    """
    if not EPHEM_AVAILABLE:
        return None
    system = obs.get('coord_system', 'altaz')
    drift_frame = obs.get('drift_frame', 'radec')
    is_ra = (system == 'radec' or (system == 'drift' and drift_frame == 'radec'))
    coord1 = dms_to_decimal(obs.get('coord1_deg', 0), obs.get('coord1_min', 0),
                            obs.get('coord1_sec', 0.0), is_ra=is_ra)
    coord2 = dms_to_decimal(obs.get('coord2_deg', 0), obs.get('coord2_min', 0),
                            obs.get('coord2_sec', 0.0), is_ra=False)

    if system == 'altaz':
        return float(coord1), float(coord2)
    if system == 'drift':
        # Parked where the source will be at the beam-crossing time, which is
        # the middle of the scan - not at `when_local`.
        start = _observation_start_datetime(obs)
        if start is None:
            return None
        transit = start + timedelta(minutes=float(obs.get('duration_minutes', 30)) / 2.0)
        return compute_drift_pointing(drift_frame, coord1, coord2, transit,
                                      obs.get('object_name', ''))

    observer = _get_observer()
    observer.date = _local_to_ephem_utc(when_local)
    if system == 'object':
        name = str(obs.get('object_name', '')).strip().lower()
        bodies = {'sun': ephem.Sun, 'moon': ephem.Moon, 'jupiter': ephem.Jupiter,
                  'venus': ephem.Venus, 'mars': ephem.Mars, 'saturn': ephem.Saturn}
        maker = bodies.get(name)
        if maker is None:
            return None
        body = maker()
    elif system in ('radec', 'galactic'):
        body = _drift_body('galactic' if system == 'galactic' else 'radec',
                           coord1, coord2)
    else:
        return None
    body.compute(observer)
    return math.degrees(body.alt), math.degrees(body.az)


def _observation_start_datetime(obs: dict) -> Optional[datetime]:
    """The observation's scheduled start as a naive local datetime."""
    date_str = obs.get('start_date') or datetime.now().strftime('%Y-%m-%d')
    time_str = obs.get('start_time') or ''
    try:
        return datetime.strptime("%s %s" % (date_str, time_str), '%Y-%m-%d %H:%M')
    except ValueError:
        return None


# How finely the visible window is searched. One minute is well below the rate
# at which anything moves through a 5 deg beam and keeps a whole night's search
# to a few hundred evaluations.
_HORIZON_TRIM_STEP_MIN = 1.0


def horizon_visible_window(obs: dict, profile=None, margin_deg=None):
    """The longest stretch of an observation's window with the target clear.

    Returns (start, duration_minutes, note) with the trimmed window, or
    (None, 0, note) when no part of it is clear. Returns the window unchanged
    when there is nothing to go on - no profile, no ephemeris, a satellite.

    The *longest clear stretch* rather than simply "clear at both ends",
    because the measured horizon is not a single altitude: a target can be
    clear at the start and the end of a window and pass behind a tower in
    between, and with the dome towers reaching 45 deg that is not hypothetical.
    Taking the longest run means the observation that comes out of it is clear
    throughout, and trimming an already-clear window is a no-op - which matters
    because the trim is applied to the stored entry and must not creep every
    time it is saved.
    """
    import horizon_store
    start = _observation_start_datetime(obs)
    duration = float(obs.get('duration_minutes', 30) or 0)
    if start is None or duration <= 0:
        return start, duration, None
    # Three kinds of observation are never trimmed, and each for its own
    # reason. A satellite's window comes from its own pass prediction. A
    # calibration day follows the Sun all day and already refuses to scan
    # through a configured sector. A horizon scan is the thing that *measures*
    # the horizon - trimming it against the last measurement would stop it
    # re-measuring wherever the sky was previously found blocked, which is
    # exactly where a pruning most needs re-measuring.
    if obs.get('coord_system') in ('satellite', 'calibration', 'horizon'):
        return start, duration, None
    profile = horizon_store.load_active() if profile is None else profile
    if not profile or not horizon_store.profile_floors(profile):
        return start, duration, None

    margin = (horizon_store.beam_margin_deg() if margin_deg is None
              else float(margin_deg))
    steps = max(2, int(math.ceil(duration / _HORIZON_TRIM_STEP_MIN)) + 1)
    clear = []
    for i in range(steps):
        offset = min(duration, i * _HORIZON_TRIM_STEP_MIN)
        pos = observation_altaz_at(obs, start + timedelta(minutes=offset))
        if pos is None:
            return start, duration, None
        alt, az = pos
        clear.append((offset, alt >= horizon_store.horizon_floor(profile, az) + margin))

    # Longest run of consecutive clear samples.
    best_from = best_to = None
    run_from = None
    for offset, ok in clear:
        if ok and run_from is None:
            run_from = offset
        if not ok and run_from is not None:
            if best_from is None or (offset - run_from) > (best_to - best_from):
                best_from, best_to = run_from, offset
            run_from = None
    if run_from is not None:
        last = clear[-1][0]
        if best_from is None or (last - run_from) > (best_to - best_from):
            best_from, best_to = run_from, last

    if best_from is None or best_to <= best_from:
        return None, 0.0, ("the target is behind the measured horizon for the "
                           "whole of this window")
    if best_from == 0.0 and best_to >= duration:
        return start, duration, None                     # already clear throughout

    note = ("trimmed to the %.0f min the target is clear of the measured "
            "horizon (was %.0f min from %s)"
            % (best_to - best_from, duration, start.strftime('%H:%M')))
    return start + timedelta(minutes=best_from), best_to - best_from, note


def apply_horizon_trim(obs: dict):
    """Rewrite an observation's window to the part with the target clear.

    Modifies and returns the observation. The stored entry carries the trimmed
    times so the schedule shows what will actually run; the trim is idempotent,
    because a window that is already clear throughout comes back unchanged.

    An observation with no clear window at all is left with its times intact
    and marked, so the scheduler can skip it and say why rather than silently
    running it into the trees.
    """
    if not obs.get('respect_local_horizon', True):
        obs.pop('horizon_note', None)
        obs.pop('horizon_blocked', None)
        return obs
    start, duration, note = horizon_visible_window(obs)
    if start is None:
        obs['horizon_blocked'] = True
        obs['horizon_note'] = note
        return obs
    obs['horizon_blocked'] = False
    if note:
        obs['start_date'] = start.strftime('%Y-%m-%d')
        obs['start_time'] = start.strftime('%H:%M')
        obs['duration_minutes'] = max(1, int(round(duration)))
        obs['horizon_note'] = note
    else:
        obs.pop('horizon_note', None)
    return obs


def compute_drift_pointing(frame: str, coord1: float, coord2: float,
                           when_local: datetime, object_name: str = '') -> Optional[tuple]:
    """Alt/Az (degrees) at which a source will sit at the given local time.

    This is the fixed pointing for a drift scan: park the dish there with
    tracking off and the source crosses beam centre at when_local.
    Returns None if PyEphem is unavailable.
    """
    if not EPHEM_AVAILABLE:
        return None
    observer = _get_observer()
    observer.date = _local_to_ephem_utc(when_local)
    body = _drift_body(frame, coord1, coord2, object_name)
    body.compute(observer)
    return math.degrees(body.alt), math.degrees(body.az)


def drift_beam_time(obs: dict, now: Optional[datetime] = None) -> datetime:
    """Beam-crossing time T of a drift entry: the mid-point of its scheduled slot.

    The slot is stored as start = T - window and duration = 2 * window, so the
    mid-point recovers T exactly. Deriving T from the *scheduled* slot (not the
    actual start) keeps the geometry correct on a late start, and makes daily
    repeats self-correcting: each day's pointing is recomputed for that day's T.
    """
    now = now or datetime.now()
    date_str = obs.get('start_date') or now.strftime('%Y-%m-%d')
    try:
        start = datetime.strptime(f"{date_str} {obs.get('start_time', '')}",
                                  '%Y-%m-%d %H:%M')
    except ValueError:
        start = now
    return start + timedelta(minutes=obs.get('duration_minutes', 30) / 2.0)


def _drift_track(obs: dict):
    """The source's true (alt, az) as a function of local time, for a drift entry."""
    drift_frame = obs.get('drift_frame', 'radec')
    coord1 = dms_to_decimal(obs.get('coord1_deg', 0), obs.get('coord1_min', 0),
                            obs.get('coord1_sec', 0.0), is_ra=(drift_frame == 'radec'))
    coord2 = dms_to_decimal(obs.get('coord2_deg', 0), obs.get('coord2_min', 0),
                            obs.get('coord2_sec', 0.0), is_ra=False)
    object_name = obs.get('object_name', '')

    def track(when_local: datetime):
        return compute_drift_pointing(drift_frame, coord1, coord2, when_local, object_name)
    return track


def srt_pointing_terms() -> Optional[dict]:
    """The pointing model the controller is applying, from its /pointing endpoint.

    Returns the term dict ({} for a controller with no model loaded, which
    still applies refraction), or None if it cannot be read - in which case
    the drive frame is unknown here and nothing may be assumed about it.
    """
    result = srt_api_call("/pointing")
    if not result or 'terms' not in result:
        return None
    if not result.get('loaded'):
        return {}
    try:
        return {k: float(v) for k, v in result['terms'].items()}
    except (TypeError, ValueError):
        return None


def plan_drift_parking(obs: dict, beam_time: datetime, terms: Optional[dict]) -> Optional[dict]:
    """Park a drift scan on the drive grid point its source passes closest to.

    Without the controller's model (`terms` None) the grid cannot be placed on
    the sky from here, so the answer is None and the caller parks at the
    source's position at T as before - the controller then rounds by up to a
    quarter of a degree per axis and the crossing time is only approximate.
    """
    if terms is None:
        return None
    import drift_park
    track = _drift_track(obs)
    if track(beam_time) is None:
        return None
    return drift_park.choose_parking(track, beam_time, terms)


def _crossing_time_str(when: datetime) -> str:
    return when.strftime('%Y-%m-%d %H:%M:%S')


def confirm_drift_parking(obs: dict) -> None:
    """After the slew: where the mount actually parked, and when the source crosses it.

    The drive position reported by /status is compared with the grid point
    chosen at the start - a disagreement means the model copied into
    drift_park no longer matches the controller's, and is logged. The
    crossing time is then recomputed, model-free, from the true position the
    controller itself reports for the parked mount, so what goes in the file
    depends on the controller's transform and not on the copy.
    """
    status = srt_get_status()
    if not status or status.get('true_alt') is None or status.get('true_az') is None:
        return
    import drift_park
    planned = (obs.get('drift_drive_alt'), obs.get('drift_drive_az'))
    got = (status.get('alt'), status.get('az'))
    if planned[0] is not None and planned[1] is not None and None not in got:
        if abs(float(got[0]) - float(planned[0])) > 1e-6 or abs(float(got[1]) - float(planned[1])) > 1e-6:
            log.warning("SRT drift scan: parked at drive Alt=%.1f Az=%.1f, not the planned "
                        "Alt=%.1f Az=%.1f - the scheduler's copy of the pointing model "
                        "disagrees with the controller's", got[0], got[1], planned[0], planned[1])
    try:
        when, sep = drift_park.crossing_at(_drift_track(obs),
                                           float(status['true_alt']), float(status['true_az']),
                                           drift_beam_time(obs))
    except Exception as e:  # ephem quirks; the planned values stand
        log.warning("SRT drift scan: could not recompute the crossing from the parked "
                    "position: %s", e)
        return
    obs['drift_drive_alt'] = got[0]
    obs['drift_drive_az'] = got[1]
    obs['drift_crossing_time'] = _crossing_time_str(when)
    obs['drift_crossing_offset_deg'] = round(sep, 3)
    log.info("SRT drift scan: parked at drive Alt=%s Az=%s (true %.2f/%.2f); source crosses "
             "at %s, %.3f deg off beam centre", got[0], got[1],
             float(status['true_alt']), float(status['true_az']),
             when.strftime('%H:%M:%S'), sep)


def parse_tle(tle_text: str) -> tuple:
    """Parse TLE text into (name, line1, line2).

    Accepts 2-line or 3-line format (with or without name line).
    """
    lines = [l.strip() for l in tle_text.strip().splitlines() if l.strip()]
    if len(lines) == 3:
        return lines[0], lines[1], lines[2]
    elif len(lines) == 2:
        return "SATELLITE", lines[0], lines[1]
    else:
        raise ValueError(f"Expected 2 or 3 TLE lines, got {len(lines)}")


def predict_next_pass(tle_text: str) -> Optional[dict]:
    """Predict the next pass of a satellite above minimum elevation.

    Returns dict with rise/set times, max elevation, duration, etc.
    or None if no pass found.
    """
    if not EPHEM_AVAILABLE:
        return None

    name, line1, line2 = parse_tle(tle_text)
    sat = ephem.readtle(name, line1, line2)
    obs = _get_observer()
    min_el = get_config_value("min_elevation")

    # Search up to 24 hours ahead
    obs.date = ephem.now()
    for _ in range(48):  # up to 48 attempts to find a good pass
        try:
            rise_time, rise_az, max_time, max_el, set_time, set_az = obs.next_pass(sat)
        except Exception as e:
            log.error("Pass prediction error: %s", e)
            return None

        max_el_deg = math.degrees(max_el)
        if max_el_deg >= min_el:
            rise_dt = ephem.Date(rise_time).datetime()
            set_dt = ephem.Date(set_time).datetime()
            max_dt = ephem.Date(max_time).datetime()
            duration_min = (set_dt - rise_dt).total_seconds() / 60

            return {
                "name": name,
                "rise_time_utc": rise_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "rise_time_local": ephem.localtime(rise_time).strftime("%Y-%m-%d %H:%M:%S"),
                "rise_az": round(math.degrees(rise_az), 1),
                "max_time_utc": max_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "max_el": round(max_el_deg, 1),
                "set_time_utc": set_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "set_time_local": ephem.localtime(set_time).strftime("%Y-%m-%d %H:%M:%S"),
                "set_az": round(math.degrees(set_az), 1),
                "duration_minutes": round(duration_min, 1),
                "start_date": ephem.localtime(rise_time).strftime("%Y-%m-%d"),
                "start_time": ephem.localtime(rise_time).strftime("%H:%M"),
            }

        # Skip past this pass and try the next one
        obs.date = set_time + ephem.minute

    return None


# Satellite tracking thread
_sat_tracking_stop = threading.Event()
_sat_tracking_thread: Optional[threading.Thread] = None


def _satellite_tracking_loop(tle_text: str):
    """Background loop that sends alt/az updates every second for a satellite."""
    try:
        name, line1, line2 = parse_tle(tle_text)
        sat = ephem.readtle(name, line1, line2)
        obs = _get_observer()

        log.info("Satellite tracking started: %s", name)

        while not _sat_tracking_stop.is_set():
            obs.date = ephem.now()
            sat.compute(obs)
            alt_deg = math.degrees(sat.alt)
            az_deg = math.degrees(sat.az)

            if alt_deg > 0:
                srt_api_call("/direct", {"alt": round(alt_deg, 2), "az": round(az_deg, 2)})

            _sat_tracking_stop.wait(1.0)

        log.info("Satellite tracking stopped: %s", name)
    except Exception as e:
        log.error("Satellite tracking error: %s", e, exc_info=True)


def start_satellite_tracking(tle_text: str):
    """Start the satellite tracking background thread."""
    global _sat_tracking_thread
    stop_satellite_tracking()
    _sat_tracking_stop.clear()
    _sat_tracking_thread = threading.Thread(
        target=_satellite_tracking_loop, args=(tle_text,), daemon=True
    )
    _sat_tracking_thread.start()


def stop_satellite_tracking():
    """Stop the satellite tracking background thread."""
    global _sat_tracking_thread
    if _sat_tracking_thread and _sat_tracking_thread.is_alive():
        _sat_tracking_stop.set()
        _sat_tracking_thread.join(timeout=5)
    _sat_tracking_thread = None


def stop_booted_receiver():
    """Stop the receiver process that was launched from the scheduler page."""
    global receiver_boot_process
    with receiver_boot_lock:
        proc = receiver_boot_process
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def receiver_python_path() -> str:
    """Return the Python executable used by receiver processes."""
    cfg_path = (load_config().get("receiver_python_path") or "").strip()
    if cfg_path:
        if os.path.isabs(cfg_path) or os.sep in cfg_path:
            resolved = cfg_path
        else:
            radioconda_bin = "/home/astro/radioconda/bin"
            resolved = shutil.which(
                cfg_path,
                path=radioconda_bin + os.pathsep + os.environ.get("PATH", ""),
            ) or cfg_path
        default_receiver = _DEFAULT_CONFIG["receiver_python_path"]
        if resolved != default_receiver or os.path.exists(resolved):
            return resolved
        # The baked-in radioconda default is preferred, but only when present.
        # On other machines, fall through to legacy/current Python.
    radioconda_python = "/home/astro/radioconda/bin/python"
    if os.path.exists(radioconda_python):
        return radioconda_python
    if PYTHON_PATH:
        return PYTHON_PATH
    return sys.executable


def receiver_process_env(base_env: Optional[dict] = None, python_path: Optional[str] = None) -> dict:
    """Build an environment that matches the selected receiver Python."""
    env = dict(base_env or os.environ.copy())
    python_path = python_path or receiver_python_path()
    conda_prefix = os.path.dirname(os.path.dirname(python_path))
    if os.path.isdir(conda_prefix):
        env["CONDA_PREFIX"] = conda_prefix
        env["PATH"] = os.path.dirname(python_path) + os.pathsep + env.get("PATH", "")
    return env


def _same_executable(left: str, right: str) -> bool:
    """Compare two executable paths without requiring both to exist."""
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.abspath(left) == os.path.abspath(right)


def maybe_reexec_scheduler_under_receiver_python() -> bool:
    """Restart the scheduler under radioconda so Sun scans and SDR code import cleanly."""
    target = receiver_python_path()
    if os.environ.get(RADIOCONDA_REEXEC_ENV) == "1":
        return False
    if not target or not os.path.exists(target):
        log.warning("Receiver Python not found, staying on current interpreter: %s", target)
        return False
    if _same_executable(sys.executable, target):
        return False

    env = receiver_process_env(python_path=target)
    env[RADIOCONDA_REEXEC_ENV] = "1"
    script = os.path.abspath(__file__)
    print(f"Restarting scheduler under receiver Python: {target}", flush=True)
    os.execvpe(target, [target, script, *sys.argv[1:]], env)
    return True


def _proc_running(proc: Optional[subprocess.Popen]) -> bool:
    return proc is not None and proc.poll() is None


def receiver_status_snapshot() -> dict:
    """Return the active receiver process, whether manual or observation-owned."""
    with process_lock:
        obs_proc = current_process
        obs_running = _proc_running(obs_proc)
        obs_name = current_observation.get("name") if current_observation else None

    if obs_running:
        return {
            "success": True,
            "running": True,
            "source": "observation",
            "pid": obs_proc.pid,
            "returncode": None,
            "observation": obs_name,
            "python": receiver_python_path(),
            "scheduler_python": sys.executable,
        }

    with receiver_boot_lock:
        boot_proc = receiver_boot_process
        boot_running = _proc_running(boot_proc)
        boot_returncode = None if boot_proc is None else boot_proc.poll()
        boot_pid = boot_proc.pid if boot_running else None

    return {
        "success": True,
        "running": boot_running,
        "source": "manual" if boot_running else "idle",
        "pid": boot_pid,
        "returncode": boot_returncode,
        "observation": None,
        "python": receiver_python_path(),
        "scheduler_python": sys.executable,
    }


# Current running observation
current_process: Optional[subprocess.Popen] = None
current_observation: Optional[dict] = None
observation_end_time: Optional[datetime] = None
process_lock = threading.Lock()
# True while start_observation is pointing/waiting for the slew with the
# lock released; start_abort lets stop_observation cancel that in-flight
# start. Both are only written under process_lock.
observation_starting = False
starting_observation_name = ''
start_abort = threading.Event()
receiver_boot_process: Optional[subprocess.Popen] = None
receiver_boot_lock = threading.Lock()
scheduler_running = True

# Sun scan state
sun_scan_thread: Optional[threading.Thread] = None
sun_scan_cancel = threading.Event()
sun_scan_state: dict = {
    "running": False,
    "progress": 0,
    "total": 0,
    "point_info": None,
    "result": None,
    "error": None,
    "image_path": None,
}

# Horizon scan state. A separate activity from the Sun scan but the same
# contract: it holds the SDR and the mount, so a scheduled observation
# preempts it, and it is cancellable between points.
horizon_thread: Optional[threading.Thread] = None
horizon_cancel = threading.Event()

# RF calibration: measuring the bandpass template and the counts-to-kelvin gain.
# Both own the SDR and the mount for a few minutes, so they take the same terms
# as the horizon scan - a scheduled observation preempts them, and they are
# cancellable. Kept separate from horizon_state because they can be re-run often
# and their results are small stored artefacts rather than a profile.
rf_thread: Optional[threading.Thread] = None
rf_cancel = threading.Event()
rf_state: dict = {
    "running": False,
    "job": None,
    "stage": "",
    "target": None,
    "error": None,
    "started_utc": None,
    "result": None,
    # When the current timed stage will end, so the page can count down without
    # polling faster. None whenever the stage has no knowable duration - a slew
    # takes as long as it takes, and a fake countdown is worse than none.
    "stage_ends_utc": None,
    "stage_total_s": None,
    # Whether this run checks the pointing against the measured horizon, and
    # what it found. Advisory: the run proceeds either way.
    "respect_horizon": True,
    "horizon_warning": None,
}
horizon_state: dict = {
    "running": False,
    "progress": 0,
    "total": 0,
    "point_info": None,
    "profile": None,
    "error": None,
    "started_utc": None,
}

# Calibration day state
cal_day_thread: Optional[threading.Thread] = None
cal_day_cancel = threading.Event()
cal_day_state: dict = {
    "running": False,
    "finished": False,
    "phase": "idle",
    "scans_completed": 0,
    "consecutive_failures": 0,
    "last_scan_error": None,
    "next_scan_time": None,
    "interval_minutes": 30,
    "error": None,
}


# Default observation template
DEFAULT_OBSERVATION = {
    "name": "New Observation",
    "coord_system": "altaz",  # altaz, radec, galactic
    "coord1_deg": 45,         # Alt, RA (as degrees), or Gal Lon
    "coord1_min": 0,
    "coord1_sec": 0.0,
    "coord2_deg": 180,        # Az, Dec, or Gal Lat
    "coord2_min": 0,
    "coord2_sec": 0.0,
    "start_date": "",         # YYYY-MM-DD, empty = today
    "start_time": "12:00",
    "duration_minutes": 30,
    "center_freq_mhz": 1420.405752,
    "bandwidth_mhz": 2.4,
    "gain_db": 40,
    "channels": 4096,
    "integration_time_s": 3.0,
    "filename": "",  # Auto-generated if empty
    "sdr_type": "b210",
    "calibrator": False,
    "end_action": "none",
    # Check the pointing against the measured horizon and say so in the log if
    # the target is behind the trees. Advisory - it never stops the run. On for
    # observations already saved as well as new ones, which is safe precisely
    # because it only ever warns.
    "respect_local_horizon": True,
    # Horizon scans (coord_system "horizon") have no target: they visit every
    # azimuth in turn, so these describe the sweep instead of a position.
    "horizon_az_start": 5.0,
    "horizon_az_end": 350.0,
    "horizon_az_step": 5.0,
    "horizon_alt_step": 5.0,
    "horizon_alt_start": 5.0,
    "horizon_alt_max": 60.0,
    "horizon_settle_s": 2.0,
    # Its own field rather than sharing integration_time_s with the sun scan:
    # a raster point wants seconds against a bright source, a horizon point
    # wants a couple against a step of order the system temperature, and one
    # field serving both means whichever was set last silently wins.
    "horizon_integration_s": 2.0,
    "enabled": True,
}


def find_clashes(schedule: list) -> list:
    """Check for overlapping enabled observations. Returns list of clash descriptions."""
    clashes = []
    # A dateless entry is not "today". Treating it as today made every leftover
    # entry without a date collide with whatever is genuinely scheduled for
    # today, so POST /api/schedule rejected saves for clashes that do not
    # exist - the save then failed while the UI reported success (S8).
    enabled = [obs for obs in schedule
               if obs.get('enabled', True)
               and obs.get('start_time')
               and obs.get('start_date')]
    for a_idx, a in enumerate(enabled):
        a_date = a['start_date']
        try:
            a_start = datetime.strptime(f"{a_date} {a['start_time']}", '%Y-%m-%d %H:%M')
        except ValueError:
            continue
        a_end = a_start + timedelta(minutes=a.get('duration_minutes', 30))
        for b_idx in range(a_idx + 1, len(enabled)):
            b = enabled[b_idx]
            b_date = b['start_date']
            try:
                b_start = datetime.strptime(f"{b_date} {b['start_time']}", '%Y-%m-%d %H:%M')
            except ValueError:
                continue
            b_end = b_start + timedelta(minutes=b.get('duration_minutes', 30))
            if a_start < b_end and b_start < a_end:
                clashes.append(f"'{a.get('name')}' and '{b.get('name')}'")
    return clashes


def load_schedule() -> list:
    """Load schedule from JSON file."""
    if os.path.exists(SCHEDULE_FILE):
        try:
            with open(SCHEDULE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            log.error("Error loading schedule: %s", e)
    return []


def save_schedule(schedule: list):
    """Save schedule to JSON file."""
    with open(SCHEDULE_FILE, 'w') as f:
        json.dump(schedule, f, indent=2)


def observations_folder() -> str:
    """The folder every recording goes in, created if it is not there yet."""
    folder = os.path.realpath(
        observation_files.observations_folder(
            get_config_value("data_output_folder")))
    os.makedirs(folder, exist_ok=True)
    return folder


def generate_filename(obs: dict) -> str:
    """Where this observation records: <observations>/YYYYMMDD_HHMMSS_<mode>.h5

    The name carries the time and whether the mount tracked or sat parked, and
    nothing else. The target name, the calibrator flag, the coordinates, the
    tuning and the calibration in force are all attributes inside the file, so
    putting any of them in the name only creates a second copy that can
    disagree with the first - after a rename, or after the schedule entry is
    edited. See observation_files for the reasoning and for why an `altaz`
    entry is a drift scan.

    An explicit filename from the operator still wins, and is still contained:
    relative subfolders are fine, absolute paths and ../ escapes are not.
    """
    folder = observations_folder()
    if obs.get('filename'):
        # An operator's name is a stem, not a full filename: "Cas A drift
        # scan" typed into the box produced a recording with no extension at
        # all (2026-08-26), invisible to every *.h5 listing - the notebook,
        # the Observe tab - while sitting right there in the folder.
        name = obs['filename'].strip()
        if not name.lower().endswith(('.h5', '.hdf5')):
            name += '.h5'
        candidate = os.path.realpath(os.path.join(folder, name))
        if candidate != folder and candidate.startswith(folder + os.sep):
            os.makedirs(os.path.dirname(candidate), exist_ok=True)
            return candidate
        log.warning("Ignoring output filename outside the observations folder: %r",
                    obs['filename'])
    return observation_files.observation_filename(
        folder, observation_files.observation_mode(obs))


def hardware_in_use():
    """What currently owns the SDR and the mount, or None if nothing does.

    One list, in one place. Each start path used to carry its own, grown an
    entry at a time, and they had drifted: on 2026-08-25 a Sun scan would start
    while a horizon scan was running, a calibration day would start while
    either a horizon scan or an RF calibration was, and a horizon scan would
    start during an RF calibration. Every one of those was refused in the
    opposite order, which is why none had been noticed - starting things in the
    habitual sequence works.

    The consequence is not a tidy error. The horizon scan drives the mount for
    two hours; a Sun scan begun alongside it rasters wherever the horizon scan
    has just moved to, both claim the B210, and the profile records whatever
    the mount happened to be pointing at.

    Deliberately does not cover a *scheduled* observation, which preempts the
    others rather than deferring to them - it is the only one whose slot cannot
    simply be re-run later.

    Acquires process_lock, so never call it while already holding it.
    """
    with process_lock:
        observing = current_process is not None and current_process.poll() is None
    if observing:
        return "an observation is recording"
    if sun_scan_state["running"]:
        return "a Sun scan is running"
    if cal_day_state["running"]:
        return "a calibration day is running"
    if horizon_state["running"]:
        return "a horizon scan is running"
    if rf_state["running"]:
        return "an RF calibration is running"
    # The manual receiver holds the B210 just as firmly as anything else, and
    # was missed when this matrix was first written on 2026-08-25 - the Sun
    # scan and calibration day happened to check it separately, so a horizon
    # scan or an RF calibration would start straight on top of it and both
    # would claim the device. Enumerating the *subsystems* rather than the
    # *claimants* is what let it through.
    with receiver_boot_lock:
        if _proc_running(receiver_boot_process):
            return "the receiver was started by hand and holds the B210"
    return None


def start_observation(obs: dict, duration_override: int = None) -> bool:
    """Start an observation.

    For calibration observations: runs repeated sun scans.
    For all others: commands telescope + starts SDR receiver.
    """
    global current_process, current_observation, observation_end_time
    global current_receiver_log
    global observation_starting, starting_observation_name

    # Calibration day: runs as a background thread, not a subprocess
    if obs.get('coord_system') == 'calibration':
        return _start_calibration_observation(obs, duration_override)

    # Horizon scan: likewise a thread, and likewise owns the SDR and the mount
    # for its whole run.
    if obs.get('coord_system') == 'horizon':
        return _start_horizon_observation(obs, duration_override)

    # An observation whose window has no part with the target clear of the
    # measured horizon does not run. This is the one place the horizon stops
    # something outright, and it is a decision taken when the schedule was
    # saved - the entry is marked then, and this is where the mark is honoured.
    # A horizon scan is exempt above, and necessarily so: it is the thing that
    # measures the horizon, and would otherwise refuse to run wherever the last
    # measurement said the sky was blocked.
    if obs.get('horizon_blocked') and obs.get('respect_local_horizon', True):
        log.warning("Skipping %s: %s", obs.get('name', 'observation'),
                    obs.get('horizon_note') or "behind the measured horizon")
        return False

    # Claim the start under the lock, then do the slow telescope work
    # (pointing and the slew wait, potentially minutes) with the lock
    # released so /api/status and /api/stop stay responsive throughout.
    with process_lock:
        if current_process is not None and current_process.poll() is None:
            return False
        if observation_starting:
            return False
        observation_starting = True
        starting_observation_name = obs.get('name', '')
        start_abort.clear()

        # A manually started receiver is useful for warm-up/testing, but scheduled
        # observations need exclusive access to the SDR.
        stop_booted_receiver()

    satellite_tracking_started = False
    try:
        # A scheduled observation always wins over a running Sun scan or
        # calibration day: cancel it and wait for the SDR to be released.
        # Cancellation is only polled between grid points, so a slew plus
        # an integration can pass before it takes effect.
        if (sun_scan_state["running"] or cal_day_state["running"]
                or horizon_state["running"] or rf_state["running"]):
            log.info("Scheduled observation preempts the running Sun scan/calibration/horizon scan/RF calibration")
            sun_scan_cancel.set()
            cal_day_cancel.set()
            horizon_cancel.set()
            rf_cancel.set()
            deadline = time.time() + SUN_SCAN_PREEMPT_TIMEOUT
            while (sun_scan_state["running"] or cal_day_state["running"]
                   or horizon_state["running"] or rf_state["running"]):
                if start_abort.is_set():
                    log.info("Observation start aborted while waiting for the Sun scan to stop")
                    return False
                if time.time() >= deadline:
                    log.error("Sun scan did not release the SDR within %ds - aborting start",
                              SUN_SCAN_PREEMPT_TIMEOUT)
                    return False
                time.sleep(1)
            log.info("Sun scan/calibration stopped; continuing with the observation")

        # Home first if the entry asks for it: the encoder counts are only as
        # good as the last homing, and a scan that depends on absolute
        # pointing (a drift scan) is worth the three minutes. A homing that
        # fails or faults is a reason not to observe, not to carry on.
        if SRT_CONTROLLER_URL and obs.get('home_first'):
            try:
                report = srt_home_with_report(cancel_event=start_abort)
            except RuntimeError as e:
                if start_abort.is_set():
                    log.info("Observation start aborted during homing")
                else:
                    log.error("Homing before the observation failed: %s - aborting", e)
                return False
            obs['homing_counters'] = report.get('counters', {})

        # Point telescope at target and wait for slew before recording
        if SRT_CONTROLLER_URL:
            if not srt_point_telescope(obs):
                log.error("SRT failed to command telescope - aborting observation")
                return False

            if obs.get('coord_system') == 'satellite':
                # Start satellite tracking thread (sends /direct every second)
                tle_text = obs.get('tle_text', '')
                if tle_text:
                    start_satellite_tracking(tle_text)
                    satellite_tracking_started = True
                else:
                    log.error("No TLE data for satellite observation")
                    return False
            elif not srt_wait_for_slew(cancel_event=start_abort):
                if start_abort.is_set():
                    log.info("Observation start aborted during slew")
                    return False
                log.warning("SRT slew timeout - starting observation at current position")
            elif obs.get('coord_system') == 'drift':
                confirm_drift_parking(obs)

        # Where the dish actually ended up, against the measured horizon. For
        # everything except a scheduled observation this only ever warns: the
        # profile may be months old and the trees will have moved since, so it
        # is here to be read afterwards when a spectrum looks warm rather than
        # to cancel the night. A scheduled observation has already had its
        # window trimmed to the visible part, so a warning here means the trim
        # could not find one, or the profile changed after it was scheduled.
        if SRT_CONTROLLER_URL:
            warning = local_horizon_warning(
                respect=obs.get('respect_local_horizon', True))
            if warning:
                log.warning("Local horizon: %s", warning)

        # Set calibrator state
        if SRT_CONTROLLER_URL:
            srt_set_calibrator(obs.get('calibrator', False))

        if start_abort.is_set():
            log.info("Observation start aborted")
            return False

        output_file = generate_filename(obs)
        env = os.environ.copy()
        env['H1_OUTPUT_FILE'] = output_file
        # The fixed instrument (issue #27): the tuning is not the entry's to
        # choose. Whatever an old entry still carries in center_freq_mhz,
        # bandwidth_mhz, channels or gain_db is ignored.
        env['H1_INSTRUMENT'] = json.dumps(instrument_in_force())
        env['H1_INTEGRATION_TIME'] = str(obs.get('integration_time_s', 3.0))
        env['H1_OBS_METADATA'] = json.dumps({
            'obs_name': obs.get('name', ''),
            # Free text from the schedule form. Lands as the `comment`
            # attribute - the receiver skips empty strings, so a recording
            # without one simply has no such attribute.
            'comment': obs.get('comment', ''),
            'coord_system': obs.get('coord_system', ''),
            # The same word the filename carries, so a renamed file still says
            # whether the mount was tracking. The name is a handle; this is the
            # record.
            'observation_mode': observation_files.observation_mode(obs),
            'object_name': obs.get('object_name', ''),
            'coord1_deg': obs.get('coord1_deg', 0),
            'coord1_min': obs.get('coord1_min', 0),
            'coord1_sec': obs.get('coord1_sec', 0.0),
            'coord2_deg': obs.get('coord2_deg', 0),
            'coord2_min': obs.get('coord2_min', 0),
            'coord2_sec': obs.get('coord2_sec', 0.0),
            'calibrator': obs.get('calibrator', False),
            'duration_minutes': obs.get('duration_minutes', 30),
            'start_date': obs.get('start_date', ''),
            'start_time': obs.get('start_time', ''),
            'tle_text': obs.get('tle_text', ''),
            'drift_frame': obs.get('drift_frame', ''),
            'drift_window_min': obs.get('drift_window_min', 0),
            'drift_beam_time': obs.get('drift_beam_time', ''),
            'drift_alt': obs.get('drift_alt', ''),
            'drift_az': obs.get('drift_az', ''),
            # The grid point the mount was parked on, when the source crosses
            # it, and by how much it misses beam centre - the crossing time
            # the plot marks and the fit should use, rather than the slot's
            # mid-point. Absent when the controller's model could not be
            # read at the start.
            'drift_drive_alt': obs.get('drift_drive_alt', ''),
            'drift_drive_az': obs.get('drift_drive_az', ''),
            'drift_crossing_time': obs.get('drift_crossing_time', ''),
            'drift_crossing_offset_deg': obs.get('drift_crossing_offset_deg', ''),
            # Whether the mount was homed just before this recording, and
            # what the counters read at the stops on the first approach -
            # the count error accumulated since the previous homing, in
            # drive degrees. Absent unless the entry asked for a homing.
            'homed_first': bool(obs.get('home_first', False)),
            'homing_count_error_alt_deg':
                (obs.get('homing_counters') or {}).get('first', {}).get('alt', ''),
            'homing_count_error_az_deg':
                (obs.get('homing_counters') or {}).get('first', {}).get('az', ''),
        })

        python_exe = receiver_python_path()
        if not os.path.exists(python_exe):
            log.error("Receiver Python not found: %s", python_exe)
            return False
        env = receiver_process_env(env, python_exe)
        cmd = [
            python_exe,
            RECEIVER_SCRIPT,
            # No GUI, and no Qt imported at all: an observation must not depend
            # on a display or on a desktop session, because the host is worked
            # over ssh and an unattended run at 03:00 has neither. The manual
            # receiver boot (/api/receiver/start) keeps the window - that is
            # what it is for.
            '--headless',
            '--sdr', obs.get('sdr_type', 'b210'),
        ]

        # Give the receiver its own log file rather than letting it inherit the
        # scheduler's stdout. Inherited, its output lands wherever the
        # scheduler was launched from - which on 2026-08-23 meant a Qt
        # "could not connect to display" failure went to a file nobody opens,
        # while scheduler.log and the Log tab showed a clean start and no hint
        # that the receiver had died a second later.
        receiver_log_path = os.path.splitext(output_file)[0] + '.receiver.log'
        try:
            receiver_log = open(receiver_log_path, 'ab', buffering=0)
        except OSError as exc:
            log.warning("Cannot write the receiver log %s (%s); its output "
                        "will be lost", receiver_log_path, exc)
            receiver_log = None

        with process_lock:
            if start_abort.is_set():
                log.info("Observation start aborted")
                if receiver_log is not None:
                    receiver_log.close()
                return False
            try:
                current_process = subprocess.Popen(
                    cmd, env=env,
                    stdout=receiver_log or None,
                    stderr=subprocess.STDOUT if receiver_log else None,
                    cwd=os.path.abspath(os.path.join(_SCRIPT_DIR, "..")))
                current_receiver_log = receiver_log_path if receiver_log else None
            except Exception as e:
                log.error("Error starting observation: %s", e)
                if receiver_log is not None:
                    receiver_log.close()
                return False
            finally:
                # The child holds its own descriptor; ours is not needed and
                # would keep the file open across the whole observation.
                if receiver_log is not None:
                    receiver_log.close()
            now = datetime.now()
            duration = duration_override or obs.get('duration_minutes', 30)
            observation_end_time = now + timedelta(minutes=duration)
            current_observation = {
                **obs,
                'output_file': output_file,
                'started_at': now.isoformat(),
                'ends_at': observation_end_time.isoformat()
            }
            log.info("Started: %s (ends at %s)", obs.get('name'), observation_end_time.strftime('%H:%M:%S'))
            # Tracking ownership passes to the running observation
            satellite_tracking_started = False
            return True
    finally:
        if satellite_tracking_started:
            # The start failed or was aborted after tracking began; don't
            # leave the tracking thread slewing the dish indefinitely.
            stop_satellite_tracking()
        with process_lock:
            observation_starting = False
            starting_observation_name = ''


# The observation that most recently finished, so its file can still be found
# after current_observation has been cleared. One slot, in memory: it is a
# convenience for the Observe tab, and the files themselves remain the record.
last_observation: Optional[dict] = None
last_observation_lock = threading.Lock()

# Where the last finished observation is remembered across restarts. The plot
# is drawn on demand from this, so holding it only in memory meant every
# scheduler restart lost the ability to plot the run that had just finished -
# and restarts happen for unrelated reasons, in the middle of an observing
# session. It is a pointer to a file that is already on disk, so persisting it
# costs nothing and keeps the Observe tab useful across one.
LAST_OBSERVATION_FILE = os.path.join(_SCRIPT_DIR, 'last_observation.json')


def _save_last_observation(info: dict):
    try:
        with open(LAST_OBSERVATION_FILE, 'w') as f:
            json.dump(info, f, indent=2)
    except OSError as exc:
        log.warning("Could not remember the last observation: %s", exc)


def _load_last_observation():
    """Recover the pointer, if the file it names is still there."""
    global last_observation
    try:
        with open(LAST_OBSERVATION_FILE) as f:
            info = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    if not isinstance(info, dict) or not info.get('output_file'):
        return
    if not os.path.exists(info['output_file']):
        log.info("The last observation's file has gone: %s", info['output_file'])
        return
    with last_observation_lock:
        last_observation = info
    log.info("Last observation recovered: %s", info.get('name') or info['output_file'])


# Where the running receiver's output is going, so its last words can be put
# into the operational record when it dies.
current_receiver_log: Optional[str] = None
_RECEIVER_LOG_TAIL_LINES = 12


def _log_receiver_output():
    """Copy the tail of the receiver's own log into scheduler.log.

    Only on an unexpected exit. The receiver is chatty in normal running and
    the whole point of giving it a file of its own was to keep that out of the
    operational record; what belongs there is why it stopped.
    """
    path = current_receiver_log
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, 'r', errors='replace') as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        log.warning("Could not read the receiver log %s: %s", path, exc)
        return
    tail = [ln for ln in lines if ln.strip()][-_RECEIVER_LOG_TAIL_LINES:]
    if not tail:
        log.error("The receiver wrote nothing to %s before exiting", path)
        return
    log.error("Last words from the receiver (%s):", path)
    for line in tail:
        log.error("    %s", line)


def _record_finished_observation(obs: Optional[dict]):
    """Remember where a finished observation put its data, and what shape it is."""
    global last_observation
    if not obs or not obs.get('output_file'):
        return
    # Calibration days and horizon scans write their own products and have no
    # spectra to plot; they end through their own paths anyway.
    if obs.get('coord_system') in ('calibration', 'horizon'):
        return
    drift = obs.get('coord_system') == 'drift'
    duration = obs.get('duration_minutes', 30)
    with last_observation_lock:
        last_observation = {
            'name': obs.get('name', ''),
            'output_file': obs['output_file'],
            'coord_system': obs.get('coord_system', ''),
            # Carried so a finished solar track can still be identified and its
            # flux re-derived: the live plot stays up after the run ends, and
            # needs both what it was pointed at and what it was tuned to.
            # Absent from anything recorded before 2026-08-25, which simply
            # reads as "not a solar track".
            'object_name': obs.get('object_name', ''),
            'center_freq_mhz': obs.get('center_freq_mhz'),
            'bandwidth_mhz': obs.get('bandwidth_mhz'),
            'channels': obs.get('channels'),
            'gain_db': obs.get('gain_db'),
            'mode': 'drift' if drift else 'spectrum',
            # The scan is laid out so the source crosses beam centre at the
            # mid-point; the plot marks it there.
            'transit_minutes': (duration / 2.0) if drift else None,
            'drift_crossing_time': obs.get('drift_crossing_time', ''),
            'drift_crossing_offset_deg': obs.get('drift_crossing_offset_deg', ''),
            'started_at': obs.get('started_at'),
            # The end that was *planned*, kept alongside the one that happened.
            # A drift plot's time axis is the observation's window, and it must
            # not shrink to the data the moment the run finishes: a scan that
            # was stopped early should go on showing the stretch it never
            # reached, or the plot quietly redraws itself as a complete one.
            'ends_at': obs.get('ends_at'),
            'ended_at': datetime.now().isoformat(timespec='seconds'),
        }
        _save_last_observation(last_observation)
    # A receiver that died before opening its file leaves nothing behind, and
    # saying otherwise sends whoever reads this looking for a file that was
    # never written.
    if os.path.exists(obs['output_file']):
        log.info("Observation data left in %s", obs['output_file'])
    else:
        log.warning("No data file was written: %s does not exist",
                    obs['output_file'])


def stop_observation() -> bool:
    """Stop current observation."""
    global current_process, current_observation, observation_end_time
    global current_receiver_log

    # Abort any in-flight start (pointing/slew wait runs outside the lock);
    # the starter notices within one poll cycle and abandons the launch.
    was_starting = observation_starting
    start_abort.set()

    # Handle calibration observations (thread-based, not subprocess)
    if current_observation and current_observation.get('coord_system') == 'calibration':
        name = current_observation.get('name', '?')
        end_action = current_observation.get('end_action', 'none')
        cal_day_cancel.set()
        sun_scan_cancel.set()
        if SRT_CONTROLLER_URL and end_action == 'home':
            srt_go_position("home", 0, 0)
        elif SRT_CONTROLLER_URL and end_action == 'stow':
            srt_go_position("stow", 90, 180)
        log.info("Stopped calibration: %s", name)
        current_observation = None
        observation_end_time = None
        return True

    # Horizon scan: same shape as the calibration branch above. A partial
    # profile is never saved, so stopping simply abandons the run.
    if current_observation and current_observation.get('coord_system') == 'horizon':
        name = current_observation.get('name', '?')
        end_action = current_observation.get('end_action', 'none')
        horizon_cancel.set()
        if SRT_CONTROLLER_URL and end_action == 'home':
            srt_go_position("home", 0, 0)
        elif SRT_CONTROLLER_URL and end_action == 'stow':
            srt_go_position("stow", 90, 180)
        log.info("Stopped horizon scan: %s", name)
        current_observation = None
        observation_end_time = None
        return True

    with process_lock:
        if current_process is None:
            return was_starting

        name = current_observation.get('name', '?') if current_observation else '?'

        if sys.platform == 'win32':
            current_process.terminate()
        else:
            current_process.send_signal(signal.SIGTERM)

        try:
            current_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            current_process.kill()
            current_process.wait()

        # Stop satellite tracking if active
        stop_satellite_tracking()

        # Ensure calibrator is off when observation ends
        if SRT_CONTROLLER_URL and current_observation and current_observation.get('calibrator'):
            srt_set_calibrator(False)

        # Return telescope to home/stow if requested
        if SRT_CONTROLLER_URL and current_observation:
            end_action = current_observation.get('end_action', 'none')
            if end_action == 'home':
                srt_go_position("home", 0, 0)
            elif end_action == 'stow':
                srt_go_position("stow", 90, 180)

        log.info("Stopped: %s", name)
        _record_finished_observation(current_observation)
        current_process = None
        current_observation = None
        current_receiver_log = None
        observation_end_time = None
        return True


def _start_calibration_observation(obs: dict, duration_override: int = None) -> bool:
    """Start a calibration day as a scheduled observation.

    Uses the calibration day thread but tracks it as a running observation
    so the scheduler knows it's active.
    """
    global current_observation, observation_end_time, cal_day_thread

    if cal_day_state["running"]:
        log.warning("Calibration day already running")
        return False

    with process_lock:
        if current_process is not None and current_process.poll() is None:
            log.warning("Receiver busy — cannot start calibration")
            return False

    # Calibration owns the SDR for the entire run, just like a normal scheduled
    # observation. Stop a receiver launched manually from the website first.
    stop_booted_receiver()

    duration = duration_override or obs.get('duration_minutes', 480)
    now = datetime.now()
    observation_end_time = now + timedelta(minutes=duration)
    current_observation = {
        **obs,
        'started_at': now.isoformat(),
        'ends_at': observation_end_time.isoformat(),
    }

    raw_params = {
        "n": obs.get("cal_grid_n", 5),
        "grid_spacing_deg": obs.get("cal_spacing_deg", 1.5),
        "integration_time_s": obs.get("integration_time_s", 3.0),
        "center_freq_mhz": obs.get("center_freq_mhz", 1420.405752),
        "bandwidth_mhz": obs.get("bandwidth_mhz", 2.4),
        "gain_db": obs.get("gain_db", 40),
        "sdr_type": obs.get("sdr_type", "b210"),
        "beam_fwhm_deg": 3.0,
        "interval_minutes": obs.get("cal_interval_min", 30),
    }
    try:
        params = _validate_sun_scan_params(raw_params, include_interval=True)
    except ValueError as exc:
        log.error("Invalid scheduled calibration parameters: %s", exc)
        current_observation = None
        observation_end_time = None
        return False
    params["scheduled"] = True

    cal_day_cancel.clear()
    cal_day_thread = threading.Thread(target=_run_calibration_day, args=(params,),
                                      daemon=True)
    cal_day_thread.start()

    log.info("Started calibration day: %s (ends at %s)",
             obs.get('name'), observation_end_time.strftime('%H:%M:%S'))
    return True


def _start_horizon_observation(obs: dict, duration_override: int = None) -> bool:
    """Start a horizon scan as a scheduled observation.

    The horizon is a twice-a-year measurement - after the trees are pruned, or
    once a season has changed what they block - and it wants a dark, dry, calm
    night rather than whoever happens to be awake. So it is schedulable on the
    same terms as the calibration day: a thread that owns the SDR and the mount,
    tracked as the running observation so the scheduler neither starts a second
    one nor restarts this one when it finishes.
    """
    global current_observation, observation_end_time, horizon_thread

    if horizon_state["running"]:
        log.warning("Horizon scan already running")
        return False

    with process_lock:
        if current_process is not None and current_process.poll() is None:
            log.warning("Receiver busy - cannot start the horizon scan")
            return False

    stop_booted_receiver()

    duration = duration_override or obs.get('duration_minutes', 180)
    now = datetime.now()
    observation_end_time = now + timedelta(minutes=duration)
    current_observation = {
        **obs,
        'started_at': now.isoformat(),
        'ends_at': observation_end_time.isoformat(),
    }

    params = {
        "az_start": float(obs.get("horizon_az_start", 5.0)),
        "az_end": float(obs.get("horizon_az_end", 350.0)),
        "az_step": float(obs.get("horizon_az_step", 5.0)),
        "alt_step": float(obs.get("horizon_alt_step", 5.0)),
        "alt_start": float(obs.get("horizon_alt_start", 5.0)),
        "alt_max": float(obs.get("horizon_alt_max", 60.0)),
        "settle_s": float(obs.get("horizon_settle_s", 2.0)),
        "integration_time_s": float(obs.get("horizon_integration_s", 2.0)),
        "center_freq_mhz": float(obs.get("center_freq_mhz", 1420.405752)),
        "bandwidth_mhz": float(obs.get("bandwidth_mhz", 2.4)),
        "gain_db": float(obs.get("gain_db", 40)),
        "sdr_type": obs.get("sdr_type", "b210"),
    }

    horizon_cancel.clear()
    horizon_thread = threading.Thread(target=_run_horizon_scan, args=(params,),
                                      daemon=True)
    horizon_thread.start()

    log.info("Started horizon scan: %s (ends at %s)",
             obs.get('name'), observation_end_time.strftime('%H:%M:%S'))
    return True


# Consecutive failed or short-lived starts per schedule slot, keyed by
# (name, start_date, start_time). Only the scheduler thread touches this,
# so no lock is needed; manual starts through the API are never blocked.
MAX_START_FAILURES = 3
_failed_starts = {}


def _slot_key(obs: dict) -> tuple:
    return (obs.get('name', ''), obs.get('start_date', ''),
            obs.get('start_time', ''))


def _too_many_start_failures(obs: dict) -> bool:
    return _failed_starts.get(_slot_key(obs), 0) >= MAX_START_FAILURES


def _record_start_failure(obs: dict, reason: str):
    if len(_failed_starts) > 200:  # prune slots from past days
        _failed_starts.clear()
    key = _slot_key(obs)
    count = _failed_starts.get(key, 0) + 1
    _failed_starts[key] = count
    if count >= MAX_START_FAILURES:
        log.error("Giving up on '%s' for this slot after %d failed starts "
                  "(%s); fix the fault, then use Run Now to retry",
                  obs.get('name'), count, reason)
    else:
        log.warning("Start of '%s' failed (%s) - attempt %d/%d",
                    obs.get('name'), reason, count, MAX_START_FAILURES)


def _same_booking(running, due, running_name=''):
    """Is the observation running the very booking that is due?

    By identity, not by name. Two bookings can share a name - the simulator
    names entries by target, so a scan started now and one due later are both
    "Drift scan l=184.6 b=-5.8" - and a name match here meant the second was
    taken to be already running and never started. Where both sides carry a
    start date and time they must agree; a run without them (Start Now from
    the Observe tab records none) can only be matched by name.
    """
    name = (running or {}).get('name', running_name)
    if name != due.get('name', ''):
        return False
    for key in ('start_date', 'start_time'):
        a, b = (running or {}).get(key), due.get(key)
        if a and b and a != b:
            return False
    return True


def scheduler_thread():
    """Background thread that checks schedule and starts/stops observations."""
    global scheduler_running

    log.info("Background scheduler started")
    # Recover the pointer to the last finished observation, so a restart does
    # not cost the Observe tab its plot.
    _load_last_observation()
    last_schedule_seen = None

    while scheduler_running:
        try:
            now = datetime.now()
            schedule = load_schedule()

            # Say what is scheduled when it *changes*, and not otherwise.
            #
            # This used to dump the whole schedule every thirty seconds. The
            # file handler is at DEBUG, so all of it landed in scheduler.log -
            # some 8600 lines a day of the same two disabled calibration days,
            # in a file that rotates at 5 MB. It was not merely noise: it
            # evicted the actual history of what the telescope did, which is
            # what that file is for.
            #
            # A schedule that has not changed carries no information the last
            # line did not. A schedule that *has* changed is worth a permanent
            # record, so this is INFO rather than DEBUG - it is an operational
            # event, not a diagnostic.
            summary = tuple((obs.get('name'), obs.get('start_date', ''),
                             obs.get('start_time', ''), obs.get('enabled', True))
                            for obs in schedule)
            if summary != last_schedule_seen:
                if last_schedule_seen is not None:
                    log.info("Schedule changed: %d observation(s)", len(schedule))
                for name, date, at, enabled in summary:
                    log.info("  - %s: %s %s%s", name,
                             date or now.strftime('%Y-%m-%d'), at,
                             "" if enabled else " (disabled)")
                last_schedule_seen = summary

            # Check if current observation should end
            with process_lock:
                if observation_end_time and now >= observation_end_time:
                    log.info("Duration complete")

            if observation_end_time and now >= observation_end_time:
                stop_observation()

            # A receiver that exits by itself ends the observation, whether or
            # not a schedule slot is still due.
            #
            # There is a branch further down that handles this, but only while
            # due_obs is set, so it never sees a Run Now observation - the kind
            # the Observe tab starts. On 2026-08-23 one of those died on
            # startup (no DISPLAY, so QApplication could not initialise) and
            # nothing reaped it: no stop, no end_action, no record for the
            # Observe tab, and the mount went on tracking a target for an
            # observation that had stopped existing. /api/status reported
            # "not running" over the top of all of it.
            #
            # Read the process under the lock and act outside it, because
            # stop_observation takes the same lock.
            with process_lock:
                proc = current_process
                starting = observation_starting
                dead_obs = dict(current_observation) if current_observation else None
            if proc is not None and not starting:
                returncode = proc.poll()
                if returncode is not None:
                    log.error("Receiver for '%s' exited on its own (return "
                              "code %s) - ending the observation",
                              (dead_obs or {}).get('name', '?'), returncode)
                    _log_receiver_output()
                    # Count it against the slot before clearing the state.
                    # Reaping here means the branch below no longer sees the
                    # dead process, so without this a scheduled observation
                    # whose receiver crashes on startup would be restarted
                    # every 5 s for the rest of its slot, slewing the telescope
                    # each time. _record_start_failure keys on
                    # (name, start_date, start_time), which a Run Now entry
                    # also has - blank date and time - and nothing consults for
                    # it, so counting is harmless there.
                    if dead_obs is not None:
                        _record_start_failure(dead_obs, "receiver exited early")
                    stop_observation()

            # Find which observation should be active right now
            due_obs = None
            due_scheduled = None
            due_remaining = None
            for obs in schedule:
                if not obs.get('enabled', True):
                    continue
                obs_date = obs.get('start_date', '')
                obs_time = obs.get('start_time', '')
                if not obs_time:
                    continue
                if not obs_date:
                    obs_date = now.strftime('%Y-%m-%d')
                try:
                    scheduled = datetime.strptime(f"{obs_date} {obs_time}", '%Y-%m-%d %H:%M')
                except ValueError:
                    continue
                duration_sec = obs.get('duration_minutes', 30) * 60
                end_time = scheduled + timedelta(seconds=duration_sec)
                diff = (now - scheduled).total_seconds()
                remaining_sec = (end_time - now).total_seconds()
                if diff >= 0 and remaining_sec > 60:
                    due_obs = obs
                    due_scheduled = scheduled
                    due_remaining = int(remaining_sec / 60)
                    break

            with process_lock:
                is_running = ((current_process is not None and current_process.poll() is None)
                              or observation_starting)
                if observation_starting:
                    running_name = starting_observation_name
                else:
                    running_name = current_observation.get('name', '') if current_observation else ''
            # Also count calibration day as running
            if not is_running and current_observation and current_observation.get('coord_system') == 'calibration':
                # A naturally completed/failed calibration remains owned by its
                # scheduled slot so the scheduler does not restart it repeatedly.
                is_running = cal_day_state["running"] or cal_day_state["finished"]
            # And the horizon scan, for the same reason: it finishes when it has
            # been round the sky, typically well inside its slot, and must not
            # then be started again from the top.
            if not is_running and current_observation and current_observation.get('coord_system') == 'horizon':
                is_running = horizon_state["running"] or bool(horizon_state["profile"])

            if due_obs:
                if is_running and _same_booking(current_observation, due_obs,
                                                running_name):
                    # Already running the correct observation
                    pass
                elif is_running:
                    # Preempt: stop current, start the one that's due
                    log.info("Preempting '%s' for '%s'", running_name, due_obs.get('name'))
                    stop_observation()
                    if not start_observation(due_obs, duration_override=due_remaining):
                        _record_start_failure(due_obs, "failed to start")
                else:
                    # A receiver that exited early has already been counted and
                    # cleaned up by the reaping above, which does it for every
                    # observation rather than only for one whose slot is still
                    # due. The backoff below is what stops a crash-looping
                    # receiver being respawned every 5 s (hammering the
                    # telescope with slews) for the rest of the slot.
                    if not _too_many_start_failures(due_obs):
                        diff = (now - due_scheduled).total_seconds()
                        if diff < 60:
                            log.info("Scheduled start: %s", due_obs.get('name'))
                        else:
                            log.info("Late start: %s (%dmin remaining)", due_obs.get('name'), due_remaining)
                        if not start_observation(due_obs, duration_override=due_remaining):
                            _record_start_failure(due_obs, "failed to start")

        except Exception as e:
            log.error("Scheduler error: %s", e, exc_info=True)

        time.sleep(5)  # Check every 5 seconds

    log.info("Background scheduler stopped")


# HTML Template
# The operator page lives in web/ as ordinary static files - index.html,
# app.css, app.js - and is served by the `index` and `page_asset` routes below.
#
# It used to be a 3184-line Python string right here, 41% of this file, holding
# 2144 lines of JavaScript. Keeping it in a string meant Python's escapes were
# applied to the JavaScript before the browser ever saw it, which cost two dead
# pages in two days: a stray apostrophe from a \' on 2026-08-24, and a \n
# written into an alert on 2026-08-25 that broke the string across lines. Both
# times every handler on the page died at once while the server went on
# answering in a millisecond.
#
# Editing the page no longer needs the scheduler restarted, either - the files
# are read from disk per request, so a browser refresh is enough.
PAGE_DIR = os.path.join(_SCRIPT_DIR, "web")


# =============================================================================
# Sun Scan Integration
# =============================================================================

def _validate_sun_scan_params(raw: dict, include_interval: bool = False) -> dict:
    """Validate and normalise values received from the calibration web forms."""
    if not isinstance(raw, dict):
        raise ValueError("Request body must be a JSON object")

    def number(key, default, minimum, maximum, integer=False):
        value = raw.get(key, default)
        try:
            parsed = int(value) if integer else float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number") from None
        if not math.isfinite(float(parsed)) or not minimum <= parsed <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
        return parsed

    params = {
        "n": number("n", 5, 3, 15, integer=True),
        "grid_spacing_deg": number("grid_spacing_deg", 1.5, 0.1, 10.0),
        "integration_time_s": number("integration_time_s", 3.0, 0.1, 60.0),
        "center_freq_mhz": number("center_freq_mhz", 1420.405752, 0.001, 100000.0),
        "bandwidth_mhz": number("bandwidth_mhz", 2.4, 0.01, 100.0),
        "gain_db": number("gain_db", 40.0, 0.0, 100.0),
        "beam_fwhm_deg": number("beam_fwhm_deg", 3.0, 0.1, 30.0),
    }
    if params["n"] % 2 == 0:
        raise ValueError("n must be odd so the raster has a centre point")
    sdr_type = str(raw.get("sdr_type", "b210")).strip().lower()
    if sdr_type not in {"b210", "rtlsdr", "demo"}:
        raise ValueError("sdr_type must be b210, rtlsdr, or demo")
    params["sdr_type"] = sdr_type
    params["respect_local_horizon"] = bool(raw.get("respect_local_horizon", True))
    if include_interval:
        params["interval_minutes"] = number(
            "interval_minutes", 30, 5, 120, integer=True)
    return params

def _sun_scan_progress(idx, total, info):
    """Progress callback for sun_scan — updates global state."""
    sun_scan_state["progress"] = idx + 1
    sun_scan_state["total"] = total
    sun_scan_state["point_info"] = info


def _run_sun_scan(params: dict):
    """Run sun scan in a background thread."""
    global sun_scan_state
    sun_scan_state.update(running=True, progress=0, total=0,
                          point_info=None, result=None, error=None,
                          image_path=None)
    try:
        from sun_scan import sun_scan as do_sun_scan

        cfg = load_config()
        data_folder = get_config_value("data_output_folder")
        os.makedirs(data_folder, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        image_path = os.path.join(data_folder, f"sun_scan_{timestamp}.png")

        controller_url = None
        if params.get("sdr_type", "b210") != "demo":
            status = srt_get_status()
            controller_url = _normalize_controller_url(SRT_CONTROLLER_URL)
            if not status or not controller_url:
                tried = ", ".join(_controller_url_candidates()) or "no configured URLs"
                raise RuntimeError(
                    f"Cannot reach the SRT controller for the Sun scan (tried: {tried})")
            if status.get("fault_active"):
                detail = status.get("fault") or status.get("status") or "unknown fault"
                raise RuntimeError(f"SRT controller reports a telescope fault: {detail}")
            log.info("Sun scan using SRT controller at %s", controller_url)
            if params.get("home_before_scan"):
                cal_day_state["phase"] = "homing"
                srt_home_and_wait(
                    timeout=int(cfg.get("homing_timeout", 300)),
                    cancel_event=sun_scan_cancel)
                cal_day_state["phase"] = "scanning"

        result = do_sun_scan(
            n=params.get("n", 5),
            grid_spacing_deg=params.get("grid_spacing_deg", 1.5),
            integration_time_s=params.get("integration_time_s", 3.0),
            srt_url=controller_url,
            lat=cfg.get("observer_lat"),
            lon=cfg.get("observer_lon"),
            elevation=cfg.get("observer_elevation", 50),
            sdr_type=params.get("sdr_type", "b210"),
            center_freq=params.get("center_freq_mhz", 1420.405752) * 1e6,
            sample_rate=params.get("bandwidth_mhz", 2.4) * 1e6,
            gain=params.get("gain_db", 40.0),
            output_image=image_path,
            slew_timeout=cfg.get("slew_timeout", 300),
            position_tolerance=cfg.get("position_tolerance", 0.5),
            beam_fwhm_deg=params.get("beam_fwhm_deg", 3.0),
            progress_callback=_sun_scan_progress,
            cancel_event=sun_scan_cancel,
        )

        # Convert numpy array to list for JSON serialisation
        result["power_grid"] = result["power_grid"].tolist()
        sun_scan_state["result"] = result
        sun_scan_state["image_path"] = image_path
        if not result.get("fit", {}).get("success"):
            fit_error = result.get("fit", {}).get("error") or "Gaussian fit quality checks failed"
            sun_scan_state["error"] = f"Sun scan fit rejected: {fit_error}"
            log.error("%s", sun_scan_state["error"])
            return
        log.info("Sun scan complete: dAlt=%+.3f° dAz=%+.3f°",
                 result["alt_error_deg"], result["az_error_deg"])
    except Exception as exc:
        log.error("Sun scan failed: %s", exc)
        sun_scan_state["error"] = str(exc)
    finally:
        sun_scan_state["running"] = False


def _run_horizon_scan(params: dict):
    """Map the obstructed horizon, in a worker thread."""
    from horizon_scan import (generate_horizon_plot, horizon_strip_scan,
                              save_horizon_profile)

    # The azimuth count is known before the first cut finishes, and the first
    # cut is the slowest one - it has no previous answer to start its window
    # from. Leaving total at zero until the first progress callback showed
    # "azimuth 0 of 0" for the first minute or two of a ninety-minute scan.
    az_start = params.get("az_start", 5.0)
    az_end = params.get("az_end", 350.0)
    az_step = params.get("az_step", 5.0)
    expected = max(1, int((az_end - az_start) / az_step) + 1)

    horizon_state.update(running=True, progress=0, total=expected, point_info=None,
                         profile=None, error=None,
                         started_utc=datetime.now(timezone.utc).isoformat())
    horizon_cancel.clear()
    try:
        def progress(idx, total, info):
            horizon_state.update(progress=idx + 1, total=total, point_info=info)

        profile = horizon_strip_scan(
            az_start=az_start,
            az_end=az_end,
            az_step=az_step,
            alt_start=params.get("alt_start", 5.0),
            alt_step=params.get("alt_step", 5.0),
            alt_max=params.get("alt_max", 60.0),
            settle_s=params.get("settle_s", 2.0),
            integration_time_s=params.get("integration_time_s", 2.0),
            home_every_strips=params.get("home_every_strips", 2),
            beam_fwhm_deg=params.get("beam_fwhm_deg", 5.8),
            sdr_type=params.get("sdr_type", "b210"),
            center_freq=params.get("center_freq_mhz", 1420.405752) * 1e6,
            # Bandwidth is a free choice here rather than a trade-off: at
            # 2.4 MHz and 0.5 s the radiometric precision is 9e-4, and at 1 MHz
            # it is 1.4e-3, against a sky-to-ground step of order 60%. Narrower
            # samples less spectrum either side of the protected 1420 MHz band,
            # so it is the safer choice against terrestrial RFI.
            sample_rate=params.get("bandwidth_mhz", 2.4) * 1e6,
            gain=params.get("gain_db", 40.0),
            srt_url=SRT_CONTROLLER_URL,
            slew_timeout=SRT_SLEW_TIMEOUT,
            position_tolerance=SRT_POSITION_TOLERANCE,
            progress_callback=progress,
            cancel_event=horizon_cancel,
        )
        save_horizon_profile(profile)
        horizon_state["profile"] = profile
        # The partial written after every strip is superseded by the archived
        # profile the moment the scan completes; leaving it behind is what
        # made the data folder read as if every scan had been abandoned.
        try:
            from horizon_scan import _partial_path
            started = datetime.fromisoformat(
                str(profile.get("started_utc", "")).replace("Z", "+00:00"))
            partial = _partial_path(started)
            if os.path.exists(partial):
                os.remove(partial)
        except (ValueError, OSError):
            pass
        data_folder = get_config_value("data_output_folder")
        os.makedirs(data_folder, exist_ok=True)
        try:
            generate_horizon_plot(profile,
                                  os.path.join(data_folder, "horizon_profile.png"))
        except Exception as exc:                      # noqa: BLE001
            log.warning("Could not generate the horizon plot: %s", exc)
        log.info("Horizon scan complete: %d azimuths in %.0f min",
                 profile["n_azimuths"], profile["duration_s"] / 60)
    except Exception as exc:                          # noqa: BLE001
        # A partial profile must never replace a complete one - that file is the
        # observatory's horizon - but throwing the measurements away is worse.
        # An abandoned run on 2026-08-21 would have discarded ninety minutes of
        # perfectly good cuts because the estimator was misbehaving, which is a
        # reason to reprocess them, not to lose them. So partials are kept
        # beside the real profile under their own name.
        partial = getattr(exc, "partial_profile", None)
        if partial and partial.get("entries"):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = os.path.join(_SCRIPT_DIR, f"horizon_partial_{stamp}.json")
            try:
                from horizon_scan import save_horizon_profile
                save_horizon_profile(partial, path)
                log.info("Kept %d azimuths from the abandoned scan in %s",
                         len(partial["entries"]), path)
            except Exception as save_exc:             # noqa: BLE001
                log.warning("Could not keep the partial horizon profile: %s", save_exc)
        log.error("Horizon scan failed: %s", exc)
        horizon_state["error"] = str(exc)
    finally:
        horizon_state["running"] = False


def _run_calibration_day(params: dict):
    """Run repeated sun scans at a fixed interval until sunset or cancelled."""
    from sun_scan import (get_sun_altaz, parse_obstruction_sectors,
                          raster_obstruction, save_scan_to_pointing_data)

    interval = params.get("interval_minutes", 30)
    cal_day_state.update(running=True, finished=False, phase="starting",
                         scans_completed=0, consecutive_failures=0,
                         last_scan_error=None, error=None,
                         interval_minutes=interval, next_scan_time=None)
    cal_day_cancel.clear()

    try:
        while not cal_day_cancel.is_set():
            cfg = load_config()
            lat = cfg.get("observer_lat", 55.9)
            lon = cfg.get("observer_lon", -4.3)
            elev = cfg.get("observer_elevation", 50)

            # Check sun is up — wait for sunrise if not
            sun_alt, sun_az = get_sun_altaz(lat, lon, elev)
            if sun_alt < 5.0:
                # If sun has already been up and set, we're done for the day
                if cal_day_state["scans_completed"] > 0:
                    log.info("Calibration day: sun has set (%.1f°), finishing",
                             sun_alt)
                    cal_day_state["phase"] = "complete"
                    cal_day_state["finished"] = True
                    break
                # Otherwise wait for sunrise
                log.info("Calibration day: waiting for sun to rise (alt=%.1f°)",
                         sun_alt)
                cal_day_state["phase"] = "waiting_for_sunrise"
                cal_day_state["error"] = None
                while sun_alt < 5.0:
                    if cal_day_cancel.is_set():
                        return
                    time.sleep(30)
                    sun_alt, _ = get_sun_altaz(lat, lon, elev)
                log.info("Calibration day: sun is up (alt=%.1f°), starting scans",
                         sun_alt)
                continue

            # Skip the treeline. A scan taken through it is not a weak scan, it
            # is a wrong one - the foliage is a bright extended source under the
            # Sun and the Gaussian centroid slides into it - and it would be
            # saved looking as respectable as any other. Waiting costs one
            # interval; the fit would have to throw the scan out anyway.
            # Beam-only margin here, not the raster allowance: the raster's
            # reach is no longer an allowance to add, it is enumerated point by
            # point below. Adding both would count the extent twice.
            sectors = parse_obstruction_sectors(horizon_obstruction_sectors(cfg))
            bad = raster_obstruction(sun_alt, sun_az,
                                     params.get("n", 5),
                                     params.get("grid_spacing_deg", 1.5),
                                     sectors)
            if bad:
                log.info("Calibration day: Sun at alt=%.1f° az=%.1f° would put a "
                         "raster point at alt=%.1f° az=%.1f° into the measured "
                         "horizon (%.1f° short); waiting for it to clear",
                         sun_alt, sun_az, bad["alt_deg"], bad["az_deg"],
                         bad["shortfall_deg"])
                cal_day_state["phase"] = "waiting_for_clear_horizon"
                if cal_day_cancel.wait(60):
                    return
                continue

            # Wait for any running single scan to finish
            while sun_scan_state["running"]:
                if cal_day_cancel.is_set():
                    return
                time.sleep(1)

            # Run a scan. Hardware scans establish a physical limit reference
            # first. One rejected attempt is automatically re-homed and retried;
            # only the final outcome counts toward consecutive failures.
            scan_started_at = datetime.now()
            max_attempts = 2
            scan_params = dict(params)
            scan_params["home_before_scan"] = (
                params.get("sdr_type", "b210") != "demo"
                and bool(cfg.get("calibration_home_before_scan", True)))
            for attempt in range(1, max_attempts + 1):
                log.info("Calibration day: starting scan %d (attempt %d/%d)",
                         cal_day_state["scans_completed"] + 1, attempt, max_attempts)
                cal_day_state["phase"] = "scanning"
                sun_scan_cancel.clear()
                _run_sun_scan(scan_params)
                if cal_day_cancel.is_set():
                    return
                if sun_scan_state.get("result") and not sun_scan_state.get("error"):
                    break
                error = sun_scan_state.get("error") or "Sun scan produced no result"
                if attempt < max_attempts and not cal_day_cancel.is_set():
                    cal_day_state["last_scan_error"] = error
                    cal_day_state["phase"] = "retrying"
                    log.warning(
                        "Calibration day scan attempt rejected; re-homing and retrying: %s",
                        error)
                    time.sleep(2)

            # Save result to pointing data
            if sun_scan_state.get("result") and not sun_scan_state.get("error"):
                save_scan_to_pointing_data(sun_scan_state["result"])
                cal_day_state["scans_completed"] += 1
                cal_day_state["consecutive_failures"] = 0
                cal_day_state["last_scan_error"] = None
                log.info("Calibration day: scan %d complete",
                         cal_day_state["scans_completed"])
            else:
                error = sun_scan_state.get("error") or "Sun scan produced no result"
                cal_day_state["last_scan_error"] = error
                cal_day_state["consecutive_failures"] += 1
                log.error("Calibration day scan failed (%d/3): %s",
                          cal_day_state["consecutive_failures"], error)
                if cal_day_state["consecutive_failures"] >= 3:
                    raise RuntimeError(
                        f"Calibration stopped after 3 consecutive scan failures: {error}")

            # Keep a start-to-start cadence so slow scans do not accumulate
            # timing drift across the day.
            next_time = scan_started_at + timedelta(minutes=interval)
            cal_day_state["next_scan_time"] = next_time.isoformat()
            cal_day_state["phase"] = "waiting_for_next_scan"
            log.info("Calibration day: next scan at %s",
                     next_time.strftime("%H:%M:%S"))

            while datetime.now() < next_time:
                if cal_day_cancel.is_set():
                    return
                time.sleep(5)

    except Exception as exc:
        log.error("Calibration day error: %s", exc, exc_info=True)
        cal_day_state["error"] = str(exc)
        cal_day_state["phase"] = "error"
        cal_day_state["finished"] = True
    finally:
        cal_day_state["running"] = False
        cal_day_state["next_scan_time"] = None
        if cal_day_cancel.is_set() and cal_day_state["phase"] != "error":
            cal_day_state["phase"] = "stopped"
            cal_day_state["finished"] = True
        log.info("Calibration day ended (%d scans)",
                 cal_day_state["scans_completed"])


@app.route('/')
def index():
    """The operator page, as a static file.

    The banner name and subtitle are no longer rendered in; the page asks
    /api/config for them on load. That is what lets this be a plain file with
    no template engine between it and the browser.
    """
    return send_from_directory(PAGE_DIR, "index.html")


@app.route('/app.css')
@app.route('/js/<path:path>')
def page_asset(path=None):
    """The page's own stylesheet and scripts.

    send_from_directory contains the path itself, so a traversal out of web/
    404s rather than being served. Flask sends these with Cache-Control:
    no-cache, so the browser revalidates and an edit shows up on refresh.
    """
    name = ("js/" + path) if path else request.path.lstrip("/")
    return send_from_directory(PAGE_DIR, name)


@app.route('/simulator/')
@app.route('/simulator/<path:path>')
def simulator(path='index.html'):
    """Serve astro_simulator/web as static files.

    The page is a plain ES-module app that needs nothing but a file host, and
    serving it from here rather than beside it is the whole point: it is then
    same origin with this API, so the Realise button can command the telescope
    through /api/simulator/realise with no cross-origin request anywhere.

    send_from_directory contains the path itself (safe_join), so a traversal
    out of the simulator directory 404s rather than being served.
    """
    resp = send_from_directory(SIMULATOR_DIR, path)
    # The page gunzips the sky bundles itself, with DecompressionStream("gzip")
    # in js/main.js. Flask infers Content-Encoding: gzip from the .gz suffix,
    # which makes the browser decode them in transit - the page's own
    # decompression then fails on already-plain bytes, and the loading progress
    # counts a Content-Length that no longer describes what arrives. They have
    # to go out as opaque bytes.
    if path.endswith('.gz'):
        resp.headers.pop('Content-Encoding', None)
    return resp


# The last observation the simulator handed over, for the Observe tab to pick
# up. Held here rather than posted between browser frames so it survives a
# reload of either page and works whether the simulator is in the tab's iframe
# or open in a window of its own. One slot: it is the latest hand-off, not a
# queue. Written by the Realise endpoint, read by /api/observe/params.
observe_params = None
observe_params_lock = threading.Lock()

# What Realise is allowed to carry into an observation, with the bounds the
# receiver and the schedule form already impose. Anything outside them is
# clamped rather than rejected: the hand-off is a starting point the operator
# reviews on the Observe tab, and refusing the whole thing because one box was
# odd would be worse than handing over a sane value.
_OBSERVE_PARAM_LIMITS = {
    'center_freq_mhz': (1000.0, 2000.0),
    'bandwidth_mhz': (0.02, 8.0),
    'integration_time_s': (0.1, 3600.0),
    'channels': (2, 65536),
    'duration_minutes': (1.0, 1435.0),
}


def _clamped(name, value, default):
    lo, hi = _OBSERVE_PARAM_LIMITS[name]
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    v = max(lo, min(hi, v))
    return int(round(v)) if isinstance(default, int) else v


def _record_observe_params(body, glon, glat, mode):
    """Store what the simulator was simulating, in observation terms."""
    global observe_params
    tau = _clamped('integration_time_s', body.get('integration_time_s'), 3.0)
    # Wrapped and clamped here as well as at the caller. What this stores is
    # what the Observe tab will hand to /api/start, so it has to be a valid
    # pointing on its own terms rather than because one caller happened to
    # normalise first.
    glon = glon % 360.0
    glat = max(-90.0, min(90.0, glat))
    params = {
        'mode': 'drift' if mode == 'cont' else 'spectrum',
        'l': round(glon, 4),
        'b': round(glat, 4),
        'center_freq_mhz': _clamped('center_freq_mhz',
                                    body.get('center_freq_mhz'), 1420.405752),
        'bandwidth_mhz': _clamped('bandwidth_mhz', body.get('bandwidth_mhz'), 2.4),
        'channels': _clamped('channels', body.get('channels'), 4096),
        # tau means two different things in the simulator, and each maps to a
        # different field here.
        #
        #   spectrum  sigma = (T_sys + T) / sqrt(npol . df . tau): the
        #             radiometer equation over the whole spectrum, so tau is
        #             the length of the observation. It sets the DURATION, and
        #             the receiver's per-spectrum integration is left to the
        #             tab - that is a recording granularity the simulation says
        #             nothing about. Averaging the run's spectra gives an
        #             effective integration of the duration either way, which
        #             is what reproduces the simulated noise.
        #
        #   drift     n = duration / tau: tau is the time per sample and the
        #             scan-length box is the duration. Both map straight
        #             across.
        'integration_time_s': tau if mode == 'cont' else None,
        'duration_minutes': (_clamped('duration_minutes',
                                      body.get('scan_minutes'), 240.0)
                             if mode == 'cont'
                             # Floored at a minute by _clamped: the scheduler
                             # will not take a slot with under 60 s left, and a
                             # shorter run integrates longer than the
                             # simulation did rather than less, so the noise
                             # can only come out better than shown.
                             else _clamped('duration_minutes', tau / 60.0, 1.0)),
        'source_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }
    with observe_params_lock:
        observe_params = params
    log.info("Realise: handed %s parameters to the Observe tab "
             "(f_c %.4f MHz, BW %.3f MHz, %d ch, %.1f min%s)",
             params['mode'], params['center_freq_mhz'],
             params['bandwidth_mhz'], params['channels'],
             params['duration_minutes'],
             f", tau {params['integration_time_s']:.2f} s"
             if params['integration_time_s'] else "")


@app.route('/api/observe/params', methods=['GET'])
def api_observe_params():
    """The observation parameters last handed over by the simulator's Realise.

    Returns {'available': False} until Realise has been pressed at least once
    since the scheduler started.
    """
    with observe_params_lock:
        if observe_params is None:
            return jsonify({'available': False})
        return jsonify({'available': True, 'params': dict(observe_params)})


# The most recent "fit model" result from the Observe tab, kept until it is
# applied or replaced. In memory only: it is a proposal, and the calibration
# in force stays in gain_calibration.json until someone applies it.
last_observe_fit = None
OBSERVE_FIT_PLOT = os.path.join(_SCRIPT_DIR, 'data', 'observe_fit.png')


@app.route('/api/observe/fit', methods=['POST'])
def api_observe_fit():
    """Fit the simulator to the last finished observation: gain and T_sys.

    The same fit the RF tab makes from a purpose-taken calibration field,
    applied to whatever was just observed - so any tracked spectrum of the
    plane doubles as a check on the calibration in force, and the drift with
    temperature (2.1% in eight hours on 2026-08-25) can be watched rather
    than discovered. The result is a proposal: reported, drawn over the data,
    and compared with the calibration in force; /api/observe/fit/apply makes
    it the calibration.

    Refused for what cannot be fitted honestly: a run still recording (the
    file is locked), a drift scan (it sweeps the sky, and the fit needs one
    direction), and the Sun or Moon (no H I model).
    """
    global last_observe_fit
    import observation_plot
    import rf_calibration

    chosen = (request.get_json(silent=True) or {}).get('file') or request.args.get('file')
    if chosen:
        try:
            info = _observation_info(chosen)
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 404
    else:
        with last_observation_lock:
            if last_observation is None:
                return jsonify({'success': False,
                                'error': 'No observation has finished yet'}), 404
            info = dict(last_observation)
    # A recording in progress is fitted on what it has so far - the file is
    # readable live (SWMR) - and says so in the result.
    live = bool(current_observation and current_observation.get('output_file')
                and os.path.realpath(current_observation['output_file'])
                    == os.path.realpath(info.get('output_file', '')))
    path = info.get('output_file', '')
    if not os.path.exists(path):
        return jsonify({'success': False, 'error': 'the recording is missing: %s'
                        % os.path.basename(path)}), 404
    drift = observation_files.observation_mode(info) == 'drift'
    if info.get('coord_system') == 'object':
        return jsonify({'success': False,
                        'error': 'no H I model for the %s' % info.get('object_name', 'object')}), 400
    try:
        _, _, stamps, _, header = observation_plot.read_observation(path)
        when = (datetime.fromtimestamp(float(stamps[len(stamps) // 2]), tz=timezone.utc)
                if len(stamps) else datetime.now(timezone.utc))
        direction = observation_plot.observation_direction(header, when)
        if direction is None:
            return jsonify({'success': False,
                            'error': 'the recording does not say where it was pointed'}), 400
        glon, glat = direction
        if drift:
            # A drift scan is a total-power measurement: counts(t) against the
            # simulator's predicted drift curve, two parameters, no bandpass
            # template needed - the bandpass shape is a constant inside the
            # gain. See drift_fit. Reported and drawn, never applied as the
            # per-channel calibration, which it is not.
            import drift_fit
            cal = drift_fit.fit_total_power(path)
            observation_plot.plot_drift_fit(cal, OBSERVE_FIT_PLOT)
        else:
            cal = rf_calibration.calibrate_observation(path, glon, glat)
            cal['source_file'] = os.path.basename(path)
            observation_plot.plot_gain_check(cal, OBSERVE_FIT_PLOT)
    except KeyError as exc:
        # A recording without the product the fit needs - one from before
        # the fixed instrument asked for as continuum.
        return jsonify({'success': False, 'error': str(exc).strip('"\'')}), 400
    except (ValueError, RuntimeError) as exc:
        return jsonify({'success': False, 'error': str(exc)}), 409
    except Exception as exc:                              # noqa: BLE001
        log.error("Observe-tab fit failed: %s", exc, exc_info=True)
        return jsonify({'success': False, 'error': str(exc)}), 500
    last_observe_fit = cal
    in_force = rf_calibration.load_calibration() or {}
    compare = None
    if in_force.get('gain_counts_per_k'):
        compare = {
            'gain_ratio': cal['gain_counts_per_k'] / in_force['gain_counts_per_k'],
            't_sys_delta_k': cal['t_sys_k'] - in_force.get('t_sys_k', 0.0),
            'in_force_observed_utc': in_force.get('observed_utc'),
        }
    log.info("Observe-tab fit of %s: gain %.4g counts/K, T_sys %.1f K, "
             "correlation %.3f", os.path.basename(path), cal['gain_counts_per_k'],
             cal['t_sys_k'], cal.get('correlation', float('nan')))
    return jsonify({'success': True, 'fit': {
        'gain_counts_per_k': cal['gain_counts_per_k'],
        't_sys_k': cal['t_sys_k'],
        'correlation': cal.get('correlation'),
        'velocity_shift_km_s': cal.get('velocity_shift_km_s'),
        'residual_rms_k': cal.get('residual_rms_k'),
        'glon': glon, 'glat': glat,
        'source_file': cal['source_file'],
        'records_used': cal.get('records_used'),
        'approximate': cal.get('approximate'),
        'kind': cal.get('kind', 'spectral'),
        'live': live,
        # Only a per-channel fit can become the calibration in force, and
        # never one made on a recording that is still arriving.
        'applicable': cal.get('kind', 'spectral') != 'total_power' and not live,
    }, 'compare': compare,
       'trustworthy_shift': rf_calibration.trustworthy_velocity_shift(cal) is not None})


@app.route('/api/observe/fit/plot', methods=['GET'])
def api_observe_fit_plot():
    from flask import send_file
    if last_observe_fit is None or not os.path.exists(OBSERVE_FIT_PLOT):
        return jsonify({'success': False, 'error': 'no fit has been made yet'}), 404
    return send_file(OBSERVE_FIT_PLOT, mimetype='image/png', max_age=0)


@app.route('/api/observe/fit/apply', methods=['POST'])
def api_observe_fit_apply():
    """Make the last Observe-tab fit the calibration in force."""
    import rf_calibration
    if last_observe_fit is None:
        return jsonify({'success': False, 'error': 'no fit to apply'}), 404
    if last_observe_fit.get('kind') == 'total_power':
        return jsonify({'success': False,
                        'error': 'a total-power fit carries the bandpass shape '
                                 'inside its gain; it is not the per-channel '
                                 'calibration and cannot be applied as one'}), 400
    rf_calibration.save_calibration(last_observe_fit)
    log.info("RF calibration: applied the Observe-tab fit of %s (gain %.4g "
             "counts/K, T_sys %.1f K)", last_observe_fit.get('source_file'),
             last_observe_fit['gain_counts_per_k'], last_observe_fit['t_sys_k'])
    return jsonify({'success': True})


@app.route('/api/observe/last', methods=['GET'])
def api_observe_last():
    """Metadata for the observation that most recently finished."""
    with last_observation_lock:
        if last_observation is None:
            return jsonify({'available': False})
        info = dict(last_observation)
    info['available'] = True
    info['exists'] = os.path.exists(info['output_file'])
    info['filename'] = os.path.basename(info['output_file'])
    if info['exists']:
        try:
            info['size_bytes'] = os.path.getsize(info['output_file'])
        except OSError:
            info['size_bytes'] = None
    return jsonify(info)


# ---------------------------------------------------------------------------
# RF calibration: the bandpass template and the counts-to-kelvin gain
#
# Two short observations that both point somewhere, record a spectrum and fit
# something to it. They are here rather than in a script because the gain drifts,
# so the useful version of this is "calibrate now, from the web page", not a
# thing someone remembers to run from a shell.

RF_LOCKMAN_L, RF_LOCKMAN_B = 150.0, 53.0     # the H I minimum: an empty band

# How much hydrogen may lie *outside* the template's masked window before the
# direction is unfit to measure a bandpass in. Inside the window the line is
# masked and interpolated across, so it does no harm; outside it, the polynomial
# fits emission as instrument response and then subtracts it from every
# observation the template is ever applied to. The Lockman Hole runs 1.3 K at
# its peak and essentially nothing beyond the mask; the plane runs a hundred.
RF_BANDPASS_MAX_LINE_K = 1.5


def _rf_emission_outside_mask(glon, glat):
    """Model H I beyond the window the bandpass fit masks out.

    The number that matters is not the line peak - that is masked - but what
    survives past the mask edges, because that is what a template would absorb.
    """
    import numpy as np

    import bandpass
    import rf_calibration

    sim = rf_calibration.load_simulator()
    v_ms, t_a = sim.spectrum(float(glon), float(glat))[:2]
    freq = rf_calibration.H1_REST_FREQ_HZ * (1.0 - np.asarray(v_ms, float)
                                             / rf_calibration.C_M_S)
    outside = np.abs(freq - rf_calibration.H1_REST_FREQ_HZ) > bandpass.DEFAULT_LINE_MASK_HZ
    t_a = np.asarray(t_a, float)
    return {"peak_k": float(np.nanmax(t_a)),
            "outside_mask_k": float(np.nanmax(t_a[outside])) if outside.any() else 0.0}


def _rf_observe(name, glon, glat, duration_s, sdr_type="b210",
                integration_s=3.0, channels=4096, bandwidth_mhz=2.0, slew=True):
    """Record for a while and return the file, optionally pointing first.

    Uses the ordinary receiver path, headless, so the file carries the same
    tuning header any observation does - which is what lets the bandpass
    template match itself to the data later.

    With slew=False the dish is left exactly where it is. That is how the
    bandpass is measured: a template must be taken close in time *and* elevation
    to whatever it will reduce, and slewing to a fixed field guarantees the
    wrong elevation. The caller is then responsible for knowing where the dish
    points and whether that is somewhere a bandpass can honestly be measured.
    """
    obs = {
        "name": name,
        "coord_system": "galactic",
        "coord1_deg": glon, "coord1_min": 0, "coord1_sec": 0.0,
        "coord2_deg": glat, "coord2_min": 0, "coord2_sec": 0.0,
        "center_freq_mhz": 1420.405752,
        "bandwidth_mhz": bandwidth_mhz,
        "channels": channels,
        "integration_time_s": integration_s,
        "gain_db": 40,
        "sdr_type": sdr_type,
        "duration_minutes": max(1, int(round(duration_s / 60.0))),
    }

    if slew:
        rf_state["stage"] = "slewing to l=%.0f b=%.0f" % (glon, glat)
        if not srt_point_telescope(obs):
            raise RuntimeError("the telescope would not accept the pointing")
        if not srt_wait_for_slew(cancel_event=rf_cancel):
            if rf_cancel.is_set():
                raise RuntimeError("cancelled during the slew")
            log.warning("RF calibration: slew timed out, recording anyway")
    else:
        log.info("RF calibration: recording where the dish already points, "
                 "l=%.2f b=%.2f", glon, glat)

    # Worth saying loudly here: a gain calibration compares a measured
    # spectrum against a simulated one, and the simulator knows about the sky
    # but not about the treeline. Foliage in the beam adds a continuum the
    # model has no term for, so it lands in T_sys and the fitted gain rather
    # than being flagged as a bad field.
    warning = local_horizon_warning(respect=rf_state.get("respect_horizon", True))
    if warning:
        log.warning("RF calibration, local horizon: %s", warning)
        rf_state["horizon_warning"] = warning
    else:
        rf_state["horizon_warning"] = None

    out = os.path.join(_SCRIPT_DIR, "data",
                       "rf_%s_%s.h5" % (name.lower().replace(" ", "_"),
                                        datetime.now().strftime("%Y%m%d_%H%M%S")))
    os.makedirs(os.path.dirname(out), exist_ok=True)

    env = os.environ.copy()
    env["H1_OUTPUT_FILE"] = out
    env["H1_INSTRUMENT"] = json.dumps(instrument_in_force())
    env["H1_INTEGRATION_TIME"] = str(integration_s)
    env["H1_OBS_METADATA"] = json.dumps({
        "obs_name": name, "coord_system": "galactic",
        "coord1_deg": glon, "coord1_min": 0, "coord1_sec": 0.0,
        "coord2_deg": glat, "coord2_min": 0, "coord2_sec": 0.0,
    })
    python_exe = receiver_python_path()
    env = receiver_process_env(env, python_exe)

    rf_state["stage"] = "recording"
    rf_state["stage_total_s"] = float(duration_s)
    rf_state["stage_ends_utc"] = (
        datetime.now(timezone.utc) + timedelta(seconds=duration_s)
    ).isoformat(timespec="seconds")
    proc = subprocess.Popen(
        [python_exe, RECEIVER_SCRIPT, "--sdr", sdr_type, "--headless"],
        env=env, cwd=_SCRIPT_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + duration_s
        while time.time() < deadline:
            if rf_cancel.is_set():
                raise RuntimeError("cancelled while recording")
            if proc.poll() is not None:
                raise RuntimeError("the receiver exited early (%s)" % proc.returncode)
            time.sleep(0.5)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    rf_state["stage_ends_utc"] = None
    rf_state["stage_total_s"] = None
    if not os.path.exists(out):
        raise RuntimeError("the receiver wrote no file")
    return out


def _run_rf_calibration(job, params):
    """Worker for both calibration jobs."""
    import bandpass
    import rf_calibration

    rf_state.update(running=True, job=job, stage="starting", error=None,
                    result=None, target=None, stage_ends_utc=None,
                    stage_total_s=None,
                    started_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    try:
        stop_booted_receiver()
        duration_s = float(params.get("duration_s", 120))
        sdr_type = params.get("sdr_type", "b210")

        if job == "bandpass":
            # Measures wherever the dish points - but where it points, not
            # where it is passing through. Pressed 23 s after "Go to Lockman
            # Hole" on 2026-08-26 it read the direction mid-slew, 12 deg
            # short, and refused the field for hydrogen that was not there.
            if SRT_CONTROLLER_URL:
                rf_state["stage"] = "waiting for the dish to arrive"
                if not srt_wait_for_slew(cancel_event=rf_cancel):
                    if rf_cancel.is_set():
                        raise RuntimeError("cancelled while waiting for the slew")
                    log.warning("RF calibration: slew wait timed out; measuring where the dish is")
            status = srt_get_status() or {}
            glon, glat = status.get("gal_l"), status.get("gal_b")
            if glon is None or glat is None:
                raise RuntimeError("the controller did not report where the "
                                   "dish is pointing, so the bandpass cannot be "
                                   "attributed to a direction")
            emission = _rf_emission_outside_mask(float(glon), float(glat))
            target = {"glon": float(glon), "glat": float(glat),
                      "alt_deg": status.get("alt"), "az_deg": status.get("az"),
                      "expected_peak_k": emission["peak_k"],
                      "outside_mask_k": emission["outside_mask_k"],
                      "why": "wherever the dish is: a template must match the "
                             "elevation and the hour of what it will reduce"}
            rf_state["target"] = target
            if emission["outside_mask_k"] > RF_BANDPASS_MAX_LINE_K:
                raise RuntimeError(
                    "l=%.1f b=%.1f has %.1f K of H I outside the masked window, "
                    "which the template would fit as instrument response and "
                    "then subtract from every observation. Point somewhere "
                    "emptier - the Lockman Hole, l=150 b=+52, runs 1.3 K."
                    % (glon, glat, emission["outside_mask_k"]))
            path = _rf_observe("Bandpass template", float(glon), float(glat),
                               duration_s, sdr_type, slew=False)
            rf_state["stage"] = "fitting the response"
            # Both products from the one recording (issue #27): the H I
            # template, and the continuum product's own.
            fitted = bandpass.fit_both_from_observation(
                path, "l=%.0f b=%+.0f" % (glon, glat))
            template, out = fitted["h1"]
            rf_state["result"] = {
                "kind": "bandpass",
                "degree": template["degree"],
                "band_mhz": 2 * template["u_scale_hz"] / 1e6,
                "residual_pct": 100 * template["fit_residual_rms"],
                "channels": template["n_channels_fitted"],
                "file": os.path.basename(path),
                "stored": os.path.basename(out),
                "glon": float(glon), "glat": float(glat),
                "alt_deg": status.get("alt"),
            }
            if "wide" in fitted:
                wide_t, wide_out = fitted["wide"]
                rf_state["result"]["wide"] = {
                    "residual_pct": 100 * wide_t["fit_residual_rms"],
                    "channels": wide_t["n_channels_fitted"],
                    "band_mhz": 2 * wide_t["u_scale_hz"] / 1e6,
                    "stored": os.path.basename(wide_out),
                }
            log.info("RF calibration: bandpass template refitted, residual %.3f%%%s",
                     100 * template["fit_residual_rms"],
                     ("; continuum product %.3f%%" % (100 * fitted["wide"][0]["fit_residual_rms"])
                      if "wide" in fitted else ""))

        elif job == "gain":
            cfg = load_config()
            if params.get("glon") is not None:
                # Chosen by the operator and still taken as given: a direction
                # can be worth calibrating on for reasons this does not know.
                # It is no longer taken *blind*, though - the skyline used to
                # be the thing only the operator could see, and since it was
                # measured the check below says what is behind the beam. It
                # warns rather than refuses, because the profile is a
                # measurement of something that grows.
                target = {"glon": params["glon"], "glat": params["glat"],
                          "chosen_by": "operator"}
                alt, az = rf_calibration._sky_position(
                    target["glon"], target["glat"], datetime.now(timezone.utc),
                    float(cfg.get("observer_lat", SITE_LAT_DEG)),
                    float(cfg.get("observer_lon", SITE_LON_DEG)),
                    float(cfg.get("observer_elevation", 50)))
                target["alt_deg"] = float(alt[0])
                target["az_deg"] = float(az[0])
                if target["alt_deg"] < 5.0:
                    raise RuntimeError(
                        "l=%.0f b=%.0f is at altitude %.1f - below the horizon"
                        % (target["glon"], target["glat"], target["alt_deg"]))
                behind = local_horizon_warning(
                    target["alt_deg"], target["az_deg"],
                    respect=rf_state.get("respect_horizon", True))
                if behind:
                    log.warning("RF calibration target chosen by hand is behind "
                                "the measured horizon: %s", behind)
                    rf_state["horizon_warning"] = behind
            else:
                rf_state["stage"] = "choosing a pointing"
                target = rf_calibration.calibration_target_now(
                    lat=float(cfg.get("observer_lat", SITE_LAT_DEG)),
                    lon=float(cfg.get("observer_lon", SITE_LON_DEG)),
                    elevation_m=float(cfg.get("observer_elevation", 50)),
                    obstruction_sectors=horizon_obstruction_sectors(cfg))
                if not target:
                    raise RuntimeError("nothing is above the lowest usable "
                                       "altitude right now, in any direction")
                if target.get("notes"):
                    log.warning("RF calibration target is compromised: %s",
                                "; ".join(target["notes"]))
            rf_state["target"] = target
            path = _rf_observe("Gain calibration", target["glon"], target["glat"],
                               duration_s, sdr_type)
            rf_state["stage"] = "fitting gain and system temperature"
            cal = rf_calibration.calibrate_observation(
                path, target["glon"], target["glat"])
            cal["target"] = target
            rf_calibration.save_calibration(cal)
            rf_state["result"] = {
                "kind": "gain",
                "gain_counts_per_k": cal["gain_counts_per_k"],
                "t_sys_k": cal["t_sys_k"],
                "t_sys_bound_active": cal["t_sys_bound_active"],
                "correlation": cal["correlation"],
                "residual_rms_k": cal["residual_rms_k"],
                "model_peak_k": cal["model_peak_k"],
                "file": os.path.basename(path),
            }
            log.info("RF calibration: gain %.4g counts/K, T_sys %.1f K "
                     "(l=%.0f b=%.0f, correlation %.3f)",
                     cal["gain_counts_per_k"], cal["t_sys_k"],
                     target["glon"], target["glat"], cal["correlation"])
        else:
            raise ValueError("unknown calibration job: %s" % job)

        rf_state["stage"] = "done"
    except Exception as exc:                              # noqa: BLE001
        rf_state["error"] = str(exc)
        rf_state["stage"] = "failed"
        log.error("RF calibration (%s) failed: %s", job, exc)
    finally:
        rf_state["running"] = False
        rf_state["stage_ends_utc"] = None
        rf_state["stage_total_s"] = None


@app.route('/api/rf/status', methods=['GET'])
def api_rf_status():
    """Everything the RF calibration tab needs to draw itself."""
    import bandpass
    import rf_calibration

    template = bandpass.load_bandpass()
    cal = rf_calibration.load_calibration()
    return jsonify({
        "success": True,
        "state": rf_state,
        "bandpass": None if not template else {
            "created_utc": template.get("created_utc"),
            "source_name": template.get("source_name"),
            "degree": template.get("degree"),
            "band_mhz": template.get("u_scale_hz", 0) / 1e6,
            "residual_pct": 100 * template.get("fit_residual_rms", 0),
            "lo_mhz": template.get("config", {}).get("lo_hz", 0) / 1e6,
            "sample_rate_mhz": template.get("config", {}).get("sample_rate_hz", 0) / 1e6,
        },
        "gain": None if not cal else {
            "created_utc": cal.get("created_utc"),
            "observed_utc": cal.get("observed_utc"),
            "gain_counts_per_k": cal.get("gain_counts_per_k"),
            "t_sys_k": cal.get("t_sys_k"),
            "t_sys_bound_active": cal.get("t_sys_bound_active"),
            "t_sys_level": cal.get("t_sys_level"),
            "implied_loss_db": cal.get("implied_loss_db"),
            "implied_ppm": cal.get("implied_ppm"),
            "velocity_shift_km_s": cal.get("velocity_shift_km_s"),
            "correlation": cal.get("correlation"),
            "residual_rms_k": cal.get("residual_rms_k"),
            "glon": cal.get("glon"), "glat": cal.get("glat"),
        },
    })


@app.route('/api/rf/target', methods=['GET'])
def api_rf_target():
    """Directions worth calibrating against right now.

    Still a list rather than a choice, but the reason has changed. It used to
    be that the software did not know the skyline: the obstruction sectors
    described the eastern treeline and nothing else, so on 2026-08-24 the
    best-scoring direction came out at azimuth 15, straight into a dome tower.
    Since the horizon was measured the candidates are screened against it in
    every direction, and each one is returned with how far it clears the
    measured floor so the operator can see that rather than take it on trust.

    It stays a list because clearing the horizon is not the same as being a
    good calibration field, and because a profile is a measurement of a
    changing thing - the trees grow between scans.

    `?glon=&glat=` additionally evaluates one direction of the operator's own,
    which is what the typed boxes use.
    """
    import rf_calibration
    cfg = load_config()
    try:
        import observatory
        targets = rf_calibration.calibration_candidates_now(
            lat=float(cfg.get("observer_lat", SITE_LAT_DEG)),
            lon=float(cfg.get("observer_lon", SITE_LON_DEG)),
            elevation_m=float(cfg.get("observer_elevation", 50)),
            obstruction_sectors=horizon_obstruction_sectors(cfg))
        import horizon_store
        profile = horizon_store.load_active()
        for t in targets:
            t["horizon"] = horizon_store.horizon_clearance(
                profile, t["alt_deg"], t["az_deg"]) if profile else {"known": False}
    except Exception as exc:                              # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500

    chosen = None
    if request.args.get('glon') not in (None, '') and \
            request.args.get('glat') not in (None, ''):
        try:
            glon = float(request.args['glon'])
            glat = float(request.args['glat'])
            alt, az = rf_calibration._sky_position(
                glon, glat, datetime.now(timezone.utc),
                float(cfg.get("observer_lat", SITE_LAT_DEG)),
                float(cfg.get("observer_lon", SITE_LON_DEG)),
                float(cfg.get("observer_elevation", 50)))
            chosen = {"glon": glon, "glat": glat,
                      "alt_deg": round(float(alt[0]), 2),
                      "az_deg": round(float(az[0]), 2)}
            chosen["horizon"] = horizon_store.horizon_clearance(
                profile, chosen["alt_deg"], chosen["az_deg"]) \
                if profile else {"known": False}
            chosen["warning"] = horizon_store.horizon_warning(
                profile, chosen["alt_deg"], chosen["az_deg"]) if profile else None
        except (TypeError, ValueError, IndexError):
            chosen = None

    return jsonify({"success": True, "targets": targets, "chosen": chosen,
                    "horizon_measured": bool(profile),
                    "beam_fwhm_deg": observatory.beam_fwhm_deg(),
                    "main_beam_efficiency": rf_calibration.MAIN_BEAM_EFFICIENCY})


# The Lockman Hole, RA 10h45m Dec +58 (J2000): the least hydrogen on the
# northern sky, 1.3 K at its peak, and circumpolar from Glasgow (lower
# culmination at alt 24). The bandpass measurement deliberately does not
# slew - a template belongs to the elevation and hour it will reduce - so
# getting there is a separate act, this one.
LOCKMAN_HOLE_GLON = 149.77
LOCKMAN_HOLE_GLAT = 52.03


@app.route('/api/rf/goto', methods=['POST'])
def api_rf_goto():
    """Track a galactic direction, for a bandpass measurement to follow.

    Body: {"glon": deg, "glat": deg}; the Lockman Hole if omitted. Refused
    while anything owns the mount, and if the direction is below the minimum
    elevation. Behind the measured horizon it goes anyway and says so - the
    profile may be stale, and the operator can see the trees.
    """
    import rf_calibration
    busy = hardware_in_use()
    if busy:
        return jsonify({"success": False, "error": "Cannot move the dish: %s" % busy}), 409
    body = request.get_json(silent=True) or {}
    try:
        glon = float(body.get("glon", LOCKMAN_HOLE_GLON))
        glat = float(body.get("glat", LOCKMAN_HOLE_GLAT))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "glon and glat must be numbers"}), 400
    cfg = load_config()
    try:
        alt, az = rf_calibration._sky_position(
            glon, glat, datetime.now(timezone.utc),
            float(cfg.get("observer_lat", SITE_LAT_DEG)),
            float(cfg.get("observer_lon", SITE_LON_DEG)),
            float(cfg.get("observer_elevation", 50)))
        alt, az = float(alt[0]), float(az[0])
    except Exception as exc:                              # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500
    min_el = float(get_config_value('min_elevation') or 0.0)
    if alt < min_el:
        return jsonify({"success": False,
                        "error": "l=%.1f b=%.1f is at alt %.1f now, below the %g deg minimum elevation"
                                 % (glon, glat, alt, min_el),
                        "alt_deg": round(alt, 2), "az_deg": round(az, 2)}), 409
    warning = None
    try:
        import horizon_store
        profile = horizon_store.load_active()
        if profile:
            warning = horizon_store.horizon_warning(profile, alt, az)
    except Exception:                                     # noqa: BLE001
        warning = None
    if not SRT_CONTROLLER_URL:
        return jsonify({"success": False, "error": "no telescope controller configured"}), 503
    result = srt_api_call("/track/galactic", {"l": glon, "b": glat})
    if not (result and result.get("ok")):
        return jsonify({"success": False,
                        "error": "the controller refused: %s" % ((result or {}).get("error") or result)}), 502
    log.info("RF: tracking l=%.2f b=%.2f (alt %.1f az %.1f) for a bandpass measurement%s",
             glon, glat, alt, az, (" - " + warning) if warning else "")
    return jsonify({"success": True, "glon": glon, "glat": glat,
                    "alt_deg": round(alt, 2), "az_deg": round(az, 2), "warning": warning})


@app.route('/api/rf/bandpass/plot', methods=['GET'])
def api_rf_bandpass_plot():
    """Before and after, so the correction can be checked by eye.

    Drawn from the observation the template was fitted from, which is the
    honest self-check: it shows what was measured, the curve fitted through it
    and what dividing by that curve leaves. It is not proof the template
    generalises - that came from applying one run's fit to a different run - but
    it is the check that catches a template fitted to the wrong thing.
    """
    from flask import send_file

    import bandpass
    import observation_plot

    template = bandpass.load_bandpass()
    if not template:
        return jsonify({'success': False,
                        'error': 'No bandpass template has been measured yet'}), 404
    src = os.path.join(_SCRIPT_DIR, 'data', template.get('source_file', ''))
    if not template.get('source_file') or not os.path.exists(src):
        return jsonify({'success': False,
                        'error': 'The observation this template was fitted from '
                                 'is no longer in the data folder, so the '
                                 'before-and-after cannot be drawn'}), 404
    out = os.path.join(_SCRIPT_DIR, 'data', 'bandpass_check.png')
    try:
        observation_plot.plot_bandpass_check(src, out, template)
    except Exception as exc:                              # noqa: BLE001
        log.error("Bandpass check plot failed: %s", exc)
        return jsonify({'success': False, 'error': str(exc)}), 500
    return send_file(out, mimetype='image/png', max_age=0)


@app.route('/api/rf/gain/plot', methods=['GET'])
def api_rf_gain_plot():
    """The calibrated spectrum over the model it was calibrated against.

    Redrawn from the same reduction the fit used, so the picture and the number
    cannot disagree. Three views because they fail differently: the spectrum
    shows whether the line profile is right, the scatter shows whether the
    relation is linear - the assumption the whole calibration rests on - and the
    residual shows *where* it is wrong, which a correlation coefficient cannot.
    """
    from flask import send_file

    import observation_plot
    import rf_calibration

    cal = rf_calibration.load_calibration()
    if not cal:
        return jsonify({'success': False,
                        'error': 'No gain calibration has been made yet'}), 404
    out = os.path.join(_SCRIPT_DIR, 'data', 'gain_check.png')
    try:
        observation_plot.plot_gain_check(cal, out)
    except Exception as exc:                              # noqa: BLE001
        log.error("Gain check plot failed: %s", exc)
        return jsonify({'success': False, 'error': str(exc)}), 500
    return send_file(out, mimetype='image/png', max_age=0)


@app.route('/api/rf/run', methods=['POST'])
def api_rf_run():
    """Start a calibration. Refuses if anything else owns the SDR."""
    global rf_thread
    # One shared matrix rather than this endpoint's own list; see
    # hardware_in_use for the four holes that drift produced.
    busy = hardware_in_use()
    if busy:
        return jsonify({"success": False, "error": "Cannot start an RF calibration: %s" % busy}), 409

    data = request.get_json(silent=True) or {}
    job = data.get("job", "")
    if job not in ("bandpass", "gain"):
        return jsonify({"success": False,
                        "error": "job must be 'bandpass' or 'gain'"}), 400
    if rf_state["running"]:
        return jsonify({"success": False,
                        "error": "a calibration is already running"}), 409
    if current_observation is not None or horizon_state["running"] \
            or sun_scan_state["running"] or cal_day_state["running"]:
        return jsonify({"success": False,
                        "error": "the telescope is busy with an observation"}), 409
    with process_lock:
        if current_process is not None and current_process.poll() is None:
            return jsonify({"success": False,
                            "error": "the receiver is busy"}), 409

    params = {"duration_s": float(data.get("duration_s", 120)),
              "sdr_type": data.get("sdr_type", "b210")}
    rf_state["respect_horizon"] = bool(data.get("respect_local_horizon", True))
    rf_state["horizon_warning"] = None
    if data.get("glon") is not None and data.get("glat") is not None:
        try:
            params["glon"] = float(data["glon"])
            params["glat"] = float(data["glat"])
        except (TypeError, ValueError):
            return jsonify({"success": False,
                            "error": "l and b must be numbers"}), 400
        if not -90.0 <= params["glat"] <= 90.0:
            return jsonify({"success": False,
                            "error": "b must be between -90 and +90"}), 400
    rf_cancel.clear()
    rf_thread = threading.Thread(target=_run_rf_calibration,
                                 args=(job, params), daemon=True)
    rf_thread.start()
    return jsonify({"success": True, "job": job})


@app.route('/api/rf/cancel', methods=['POST'])
def api_rf_cancel():
    rf_cancel.set()
    return jsonify({"success": True})


# Parsed live-summary records, per file, with how far through it we have read.
# The page polls every ten seconds for the whole length of an observation, and
# at a tenth-second integration the summary reaches 36000 lines an hour - so
# re-reading and re-parsing it each time would grow without bound on the very
# machine that is recording. Only the bytes added since last time are read.
_live_cache: dict = {}
_live_cache_lock = threading.Lock()


# How much of the start of a run to leave off the live plot.
#
# Measured on the 2026-08-25 12-minute run at 3 s per record: the first record
# came in 7.9% below the settled level and the second was already within 1.5%,
# where the run's own wander is about +-1%. So the transient is one record's
# worth of flowgraph startup, five times outside the ordinary scatter, and
# everything after it is the receiver being itself.
#
# Expressed as a time rather than a record count because the cause is: it is
# the flowgraph settling, which takes as long as it takes whatever integration
# was asked for. At the 0.1 s a burst watch would use, a count of one would
# leave most of the transient on the plot.
#
# Deliberately small. The slow wander that follows is not warm-up and must not
# be trimmed away - it never settles, it is still drifting at twelve minutes,
# and for a flux monitor it is the systematic that sets how well the Sun can
# be measured at all. Hiding it would be flattering the instrument.
LIVE_WARMUP_S = 5.0


def _live_records(path):
    """Every summary record for this observation, parsed once each.

    Returns a copy. The cached list is appended to under the lock, and handing
    the live one out would let a second poll - another browser tab is enough -
    grow it while the first was still iterating it to bin.
    """
    with _live_cache_lock:
        entry = _live_cache.get(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        if entry is None or size < entry["offset"]:
            # A new run, or the file was replaced under us: start again rather
            # than splicing the tail of one observation onto another.
            entry = {"offset": 0, "records": []}
            _live_cache[path] = entry
            if len(_live_cache) > 8:
                for stale in list(_live_cache)[:-8]:
                    _live_cache.pop(stale, None)
        if size == entry["offset"]:
            return list(entry["records"])
        try:
            with open(path) as fh:
                fh.seek(entry["offset"])
                fresh = fh.read()
        except OSError:
            return list(entry["records"])
        # A record the receiver is midway through writing has no newline yet;
        # leave it for next time rather than parsing half of it.
        cut = fresh.rfind("\n")
        if cut < 0:
            return list(entry["records"])
        entry["offset"] += len(fresh[:cut + 1].encode())
        for line in fresh[:cut].split("\n"):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                item = {"t": float(rec["t"]),
                        "tau": float(rec["tau"]),
                        "median": float(rec["median"])}
                # A fixed-instrument recording also reports the continuum -
                # the wide product over the continuum band, no hydrogen in
                # it - and the overflows during the record (issue #27).
                if rec.get("continuum") is not None:
                    item["continuum"] = float(rec["continuum"])
                if rec.get("overflows") is not None:
                    item["overflows"] = int(rec["overflows"])
                entry["records"].append(item)
            except (ValueError, KeyError, TypeError):
                continue
        return list(entry["records"])


@app.route('/api/sun/position', methods=['GET'])
def api_sun_position():
    """Where the Sun is now, and whether the dish could look at it.

    So the Observe tab can show the target of a solar track rather than asking
    for coordinates it would only ignore - the controller follows the Sun from
    its own ephemeris, and there is nothing for an operator to type.
    """
    from sun_scan import get_sun_altaz
    import horizon_store

    cfg = load_config()
    try:
        alt, az = get_sun_altaz(float(cfg.get("observer_lat", SITE_LAT_DEG)),
                                float(cfg.get("observer_lon", SITE_LON_DEG)),
                                float(cfg.get("observer_elevation", 50)))
    except Exception as exc:                              # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500
    profile = horizon_store.load_active()
    warning = (horizon_store.horizon_warning(profile, alt, az)
               if profile and cfg.get("respect_local_horizon", True) else None)
    return jsonify({"success": True, "alt_deg": round(float(alt), 2),
                    "az_deg": round(float(az), 2),
                    "up": bool(alt > 0.0), "horizon_warning": warning})


def live_plot_kind(obs):
    """Which live plot this observation gets, or None for no plot.

    'solar' converts the band power to flux above the atmosphere; 'drift' plots
    antenna temperature against a time axis fixed to the observation's own
    start and stop, so the source's transit through the beam can be watched
    against where it was predicted to fall.

    A drift scan is identified the same way its filename is - by what the mount
    does, not by which box the entry was typed into - so an `altaz` entry gets
    the plot too. It is parked, the sky moves through the beam, and that is a
    drift scan whatever it was called.

    A tracked spectrum gets no live plot. Its band power is meant to be
    constant, so an autoscaled trace of it is a magnified picture of the noise:
    it looks like structure, it is not, and there is nothing to compare it to.
    """
    if (obs.get('coord_system') == 'object'
            and str(obs.get('object_name', '')).lower() == 'sun'):
        return 'solar'
    if observation_files.observation_mode(obs) == 'drift':
        return 'drift'
    return None


def _epoch(stamp):
    """Local naive ISO timestamp to UTC seconds, or None.

    The scheduler writes started_at/ends_at with datetime.now(), so they are
    naive local time and .timestamp() reads them as such - which is what makes
    them comparable with the receiver's time.time() record stamps.
    """
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return None


@app.route('/api/observe/live', methods=['GET'])
def api_observe_live():
    """The running observation's band power, in solar flux units.

    Reads the summary the receiver writes beside its HDF5 - one line per
    record - rather than the recording itself, which cannot be opened while it
    is being written. See _append_live_summary in b210_h1_receiver.py.

    The conversion is the stored calibration and nothing new: counts to kelvin
    through the fitted gain, minus the fitted system temperature, then the
    antenna theorem with the *measured* beam. T_sys is subtracted, so what is
    plotted is the source alone. Against the Sun that term is small - a
    thousand kelvin of Sun against three hundred and fifty of system - so an
    error in T_sys moves the flux by a few percent rather than dominating it.

    Reports honestly when it cannot convert: without a calibration for this
    tuning the counts are returned unlabelled, because counts are honestly
    arbitrary and wrong flux units are not.
    """
    import numpy as np
    import rf_calibration
    import tuning
    from observatory import antenna_temperature_to_flux

    # The run in progress if there is one, otherwise the last one to finish.
    # A solar flux curve is the whole record of its run and worth looking at
    # after the Sun has been left, so the plot stays up until the next
    # observation replaces it - which is exactly when it stops being current.
    obs, finished = current_observation, False
    if not obs or not obs.get('output_file'):
        with last_observation_lock:
            obs = last_observation
        finished = True
    if not obs or not obs.get('output_file'):
        return jsonify({'success': False, 'error': 'Nothing is recording'}), 404
    path = os.path.splitext(obs['output_file'])[0] + '.live.jsonl'
    try:
        limit = max(1, min(5000, int(request.args.get('limit', 2000))))
    except (TypeError, ValueError):
        limit = 2000

    records = _live_records(path)
    dropped = 0
    if records:
        # Leave off records contaminated by the flowgraph settle - but in
        # proportion. The first version dropped everything inside LIVE_WARMUP_S
        # of the first record, written when records were 0.1-10 s and the cost
        # was at most a few seconds of trace. At a 60 s integration the same
        # rule discarded the entire first record to remove ~5 s of settle - an
        # 8% contamination of one point, purged by doubling the wait for the
        # first point from one minute to two. Now a record is dropped only
        # when the settle window covers more than a tenth of it: a 10 s solar
        # record still goes (half contaminated, measured 8% low), a 60 s drift
        # record stays, carrying a ~1% dip that is visibly the first point of
        # the run.
        #
        # Record timestamps are the *end* of the integration, so the run
        # started one integration before the first stamp.
        run_start = records[0]["t"] - records[0].get("tau", 0.0)
        warm_end = run_start + LIVE_WARMUP_S
        kept = []
        for r in records:
            tau = max(r.get("tau", 0.0), 1e-9)
            overlap = max(0.0, min(warm_end, r["t"]) - (r["t"] - tau))
            if overlap / tau > 0.1:
                dropped += 1
            else:
                kept.append(r)
        # Never drop everything: early in a run the warm-up is all there is,
        # and an empty plot would look like a receiver that is not recording.
        if kept:
            records = kept
        else:
            dropped = 0
    # The window the plot's time axis spans, for a drift scan: the observation's
    # own start and stop rather than the extent of the data so far. An axis that
    # grows with the data cannot show how far through a transit is, and rescales
    # under the reader every few seconds; a fixed one shows the scan filling in
    # towards a marked crossing time.
    kind = live_plot_kind(obs)
    t_start, t_end = _epoch(obs.get('started_at')), _epoch(obs.get('ends_at'))
    if t_end is None:
        t_end = _epoch(obs.get('ended_at'))
    window = {'kind': kind, 't_start': t_start, 't_end': t_end}
    if kind == 'drift' and t_start is not None and t_end is not None and t_end > t_start:
        # The moment the source crosses the parked beam. Computed at the start
        # from the grid point the mount was actually parked on, when the
        # controller's model was readable; otherwise the slot's mid-point,
        # which is what the pointing was laid out for and is right to within
        # the mount's quarter-degree rounding.
        crossing = _epoch(obs.get('drift_crossing_time'))
        window['t_transit'] = crossing if crossing is not None else t_start + (t_end - t_start) / 2.0
        window['crossing_offset_deg'] = obs.get('drift_crossing_offset_deg') or None

    # Whether the calibration applies is a property of the tuning, not of the
    # data, so it is decided before the records branch. It used to be hardcoded
    # False in the empty response, which was invisible while an empty response
    # drew nothing - but a drift scan draws its axes while waiting for the
    # first record, and at a 30 s integration that meant a solid minute of a
    # perfectly calibrated instrument labelled "uncalibrated".
    cal = rf_calibration.load_calibration()
    cal_ok, cal_why = rf_calibration.calibration_applies_to(cal, obs_header(obs))

    if not records:
        return jsonify(dict(window, success=True, points=[],
                            calibrated=bool(cal_ok),
                            why='' if cal_ok else cal_why,
                            name=obs.get('name'), finished=finished,
                            note='the receiver has not written a record yet'))

    # The Sun's elevation across the run, for the airmass correction. Sampled at
    # fifty points and interpolated rather than computed per record: elevation
    # moves smoothly, a two-hour run holds thousands of records, and this is
    # recomputed on every poll.
    is_solar = kind == 'solar'
    sun_alt = None
    if is_solar and records and EPHEM_AVAILABLE:
        try:
            t0, t1 = records[0]["t"], records[-1]["t"]
            knots = [t0 + (t1 - t0) * i / 49.0 for i in range(50)]
            obsv = _get_observer()
            alts = []
            for when in knots:
                obsv.date = datetime.utcfromtimestamp(when)
                body = ephem.Sun()
                body.compute(obsv)
                alts.append(math.degrees(float(body.alt)))
            sun_alt = (knots, alts)
        except Exception:                                 # noqa: BLE001
            sun_alt = None

    # Bin the whole run down rather than showing its tail. A short integration
    # makes records fast - 0.1 s gives 36000 an hour - and a plot of the last N
    # of those would silently be a plot of the last three minutes of a
    # three-hour run, which is the worst kind of wrong: it looks complete.
    # Binning keeps the whole run on screen and averages down the noise that
    # the short integration cost in the first place.
    group = max(1, int(math.ceil(len(records) / float(limit))))
    # The live trace is a continuum measurement, so where the recording
    # reports the continuum product (fixed instrument) that is what is
    # drawn; a legacy recording's band median stands in for it.
    value = (lambda r: r.get('continuum', r['median']))
    points = []
    overflowed = 0
    for start in range(0, len(records), group):
        chunk = records[start:start + group]
        counts = sum(value(r) for r in chunk) / len(chunk)
        when = chunk[len(chunk) // 2]['t']
        point = {'t': when, 'tau': sum(r['tau'] for r in chunk),
                 'n': len(chunk), 'counts': counts}
        lost = sum(r.get('overflows', 0) for r in chunk)
        if lost:
            point['overflows'] = lost
            overflowed += 1
        if cal_ok:
            t_a = counts / cal['gain_counts_per_k'] - cal['t_sys_k']
            point['t_a_k'] = t_a
            flux = antenna_temperature_to_flux(t_a)
            if sun_alt is not None:
                # Corrected to above the atmosphere, which is what a published
                # index quotes. Applied here and never to the recording - the
                # file keeps what the receiver measured, as it does for the
                # bandpass, the gain and the velocity frame.
                alt = float(np.interp(when, sun_alt[0], sun_alt[1]))
                trans = rf_calibration.atmospheric_transmission(alt)
                if trans and trans == trans and trans > 0.5:
                    point['alt_deg'] = round(alt, 2)
                    point['airmass'] = round(rf_calibration.airmass(alt), 2)
                    point['sfu_measured'] = flux
                    flux = flux / trans
            point['sfu'] = flux
        points.append(point)
    return jsonify(dict(window,
                        success=True, points=points, calibrated=bool(cal_ok),
                        records=len(records), binned=group,
                        warmup_dropped=dropped, warmup_s=LIVE_WARMUP_S,
                        opacity_applied=bool(sun_alt is not None and cal_ok),
                        zenith_opacity=rf_calibration.ZENITH_OPACITY_NEPERS,
                        why='' if cal_ok else cal_why,
                        name=obs.get('name'),
                        finished=finished,
                        t_sys_k=(cal or {}).get('t_sys_k') if cal_ok else None,
                        started_at=obs.get('started_at'),
                        ended_at=obs.get('ended_at'),
                        ends_at=obs.get('ends_at')))


def instrument_in_force():
    """The fixed instrument the receiver records with: tuning.fixed_instrument
    with whatever the config overrides (issue #27)."""
    import tuning
    return tuning.fixed_instrument(load_config())


def tuning_instrument_keys():
    import tuning
    return set(tuning.INSTRUMENT_KEYS)


def obs_header(obs=None):
    """The tuning fields calibration_applies_to needs, for any observation.

    Every scheduled observation records with the fixed instrument, so the
    header is the same for all of them; the entry is accepted for the callers
    that still pass one. The observation's own header is inside the HDF5,
    which can be busy while it records, so the values come from the
    instrument in force - they are what the receiver was told to use.
    """
    inst = instrument_in_force()
    return {'center_freq_hz': inst['lo_hz'],
            'sample_rate_hz': inst['sample_rate_hz'],
            'gain_db': inst['gain_db'],
            'h1_band_hz': inst['h1_band_hz'],
            'continuum_band_hz': inst['continuum_band_hz']}


def _observation_info(filename):
    """What the plot and the fit need to know about one recording, from the
    file itself rather than from the session's memory of it.

    `filename` is a basename inside the observations folder; anything else
    is refused, so a URL cannot reach outside it. The recording's own
    attributes supply the name, the coordinate system and the mode - the
    same facts last_observation carries for the run that just finished, so
    either source can drive the same code.
    """
    from observation_plot import open_readonly
    folder = observations_folder()
    path = os.path.realpath(os.path.join(folder, os.path.basename(filename or '')))
    if not filename or os.path.dirname(path) != folder or not os.path.isfile(path):
        raise ValueError('no such recording: %s' % filename)
    with open_readonly(path) as hf:
        a = dict(hf.attrs)
    info = {'output_file': path,
            'name': str(a.get('obs_name', os.path.basename(path))),
            'coord_system': str(a.get('coord_system', '')),
            'object_name': str(a.get('object_name', '')),
            'comment': str(a.get('comment', '')),
            'duration_minutes': a.get('duration_minutes')}
    mode = a.get('observation_mode')
    if mode is None:
        mode = observation_files.observation_mode(info)
    info['mode'] = 'drift' if str(mode) == 'drift' else 'spectrum'
    try:
        info['transit_minutes'] = (float(a['duration_minutes']) / 2.0
                                   if info['mode'] == 'drift' else None)
    except (KeyError, TypeError, ValueError):
        info['transit_minutes'] = None
    return info


H1_LINE_HZ = 1420405751.768


def _recording_details(path):
    """The facts about a recording that decide what can be done with it.

    Read from the file: tuning (sky centre, LO, sample rate, the band it
    spans), whether the H I line is in that band and where the fit window
    lies, the spectral geometry, the calibration state, the pointing and the
    comment. The point is to have these beside the plot, so a question like
    "did the recorded band overlap the line at that tuning?" is answered by
    looking rather than by arithmetic.
    """
    import drift_fit
    from observation_plot import open_readonly
    with open_readonly(path) as hf:
        a = dict(hf.attrs)
        name = 'spectra_kelvin' if 'spectra_kelvin' in hf else 'spectra_linear'
        n_rec, n_ch = hf[name].shape
        freq = hf['frequency_hz'][:]
        taus = hf['integration_times'][:] if 'integration_times' in hf else []
        # The continuum product, where the file has one (fixed instrument).
        wide_freq = hf['frequency_hz_wide'][:] if 'frequency_hz_wide' in hf else None
        wide_units = str(a.get('spectra_wide_units', '')) if wide_freq is not None else ''
        overflows_total = int(np.asarray(hf['overflows'][:]).sum()) if 'overflows' in hf else None
    f = lambda k, d=None: (float(a[k]) if k in a and a[k] is not None else d)
    lo_hz, sr = f('center_freq_hz'), f('sample_rate_hz')
    sky_hz = f('sky_center_freq_hz', lo_hz)
    band = (float(freq.min()), float(freq.max())) if len(freq) else (None, None)
    # The continuum window is measured on the wide product; a recording
    # without one has no continuum window.
    if wide_freq is not None and a.get('continuum_band_hz') is not None:
        win_lo, win_hi, _ = drift_fit._band_window(a, wide_freq)
    else:
        win_lo = win_hi = None
    line_in_band = band[0] is not None and band[0] <= H1_LINE_HZ <= band[1]
    line_in_window = win_lo is not None and win_lo <= H1_LINE_HZ <= win_hi
    mode = a.get('observation_mode')
    if mode is None:
        mode = observation_files.observation_mode({'coord_system': str(a.get('coord_system', ''))})
    d = {
        'filename': os.path.basename(path),
        'name': str(a.get('obs_name', '')), 'comment': str(a.get('comment', '')),
        'mode': str(mode), 'coord_system': str(a.get('coord_system', '')),
        'coord1_deg': f('coord1_deg'), 'coord2_deg': f('coord2_deg'),
        'drift_frame': str(a.get('drift_frame', '')),
        'drift_alt': f('drift_alt'), 'drift_az': f('drift_az'),
        'drift_drive_alt': f('drift_drive_alt'), 'drift_drive_az': f('drift_drive_az'),
        'drift_crossing_time': str(a.get('drift_crossing_time', '')),
        'drift_crossing_offset_deg': f('drift_crossing_offset_deg'),
        'created': str(a.get('created', '')),
        'records': int(n_rec), 'channels': int(n_ch),
        'integration_s': (float(np.median(taus)) if len(taus) else f('nominal_integration_time')),
        'sky_center_mhz': sky_hz / 1e6 if sky_hz else None,
        'lo_mhz': lo_hz / 1e6 if lo_hz else None,
        'sample_rate_mhz': sr / 1e6 if sr else None,
        'band_mhz': [band[0] / 1e6, band[1] / 1e6] if band[0] is not None else None,
        'fit_window_mhz': [win_lo / 1e6, win_hi / 1e6] if win_lo is not None else None,
        'channel_khz': (sr / n_ch / 1e3) if sr and n_ch else None,
        'h1_line_mhz': H1_LINE_HZ / 1e6,
        'h1_in_band': bool(line_in_band), 'h1_in_fit_window': bool(line_in_window),
        'h1_offset_from_lo_mhz': ((H1_LINE_HZ - lo_hz) / 1e6) if lo_hz else None,
        # Fixed-instrument recordings: both products, their bands, and
        # whether any samples were lost (issue #27).
        'products': (['h1', 'wide'] if wide_freq is not None else ['h1']),
        'h1_band_mhz': ([float(a['h1_band_hz'][0]) / 1e6, float(a['h1_band_hz'][1]) / 1e6]
                        if a.get('h1_band_hz') is not None else None),
        'continuum_band_mhz': ([float(a['continuum_band_hz'][0]) / 1e6,
                                float(a['continuum_band_hz'][1]) / 1e6]
                               if a.get('continuum_band_hz') is not None else None),
        'wide_channels': (int(len(wide_freq)) if wide_freq is not None else None),
        'wide_units': wide_units or None,
        'overflows_total': overflows_total,
        'gain_db': f('gain_db'), 'sdr_type': str(a.get('sdr_type', '')),
        'units': str(a.get('spectra_units', 'K' if name == 'spectra_kelvin' else 'counts')),
        'applied_gain_counts_per_k': f('applied_gain_counts_per_k'),
        'applied_t_sys_k': f('applied_t_sys_k'),
    }
    return d


@app.route('/api/observe/info', methods=['GET'])
def api_observe_info():
    """Recording details for the chosen file, for the table beside the plot."""
    import numpy as np  # noqa: F401  (used by _recording_details through the module)
    chosen = request.args.get('file')
    try:
        info = _observation_info(chosen)
        return jsonify({'success': True, 'details': _recording_details(info['output_file'])})
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 404
    except (OSError, BlockingIOError) as exc:
        return jsonify({'success': False, 'error': 'the recording is locked (still '
                        'being written?): %s' % exc}), 409


@app.route('/api/observe/download', methods=['GET'])
def api_observe_download():
    """The chosen recording, as a file. Same containment as everything else
    that takes ?file=: a basename inside the observations folder, or 404."""
    from flask import send_file
    try:
        info = _observation_info(request.args.get('file'))
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 404
    return send_file(info['output_file'], as_attachment=True,
                     download_name=os.path.basename(info['output_file']),
                     mimetype='application/x-hdf5', max_age=0)


@app.route('/api/observations', methods=['GET'])
def api_observations():
    """Every recording in the observations folder, newest first.

    Read from the files' own attributes so what is listed is what is there -
    the session's last_observation is one entry among them, marked so the
    page can select it by default. A file the receiver is still writing
    cannot be opened (the HDF5 lock) and is listed as recording.
    """
    from observation_plot import open_readonly
    folder = observations_folder()
    live = os.path.realpath(current_observation['output_file']) \
        if current_observation and current_observation.get('output_file') else None
    rows = []
    for name in os.listdir(folder):
        if not name.lower().endswith(('.h5', '.hdf5')):
            continue
        path = os.path.join(folder, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        row = {'filename': name, 'size_bytes': st.st_size,
               'mtime': datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds'),
               'recording': False}
        try:
            with open_readonly(path) as hf:
                a = dict(hf.attrs)
            # Recording, and readable: the file is in SWMR mode, so the page
            # can plot what has arrived so far.
            row['recording'] = os.path.realpath(path) == live
            row.update(name=str(a.get('obs_name', '')),
                       comment=str(a.get('comment', '')),
                       coord_system=str(a.get('coord_system', '')),
                       created=str(a.get('created', '')),
                       units=str(a.get('spectra_units', '')))
            mode = a.get('observation_mode')
            row['mode'] = str(mode) if mode is not None else \
                observation_files.observation_mode(row)
        except (OSError, BlockingIOError, RuntimeError):
            # Being written by a receiver from before SWMR: not readable yet.
            row.update(name='', comment='', coord_system='', created='',
                       units='', mode='', recording=True, locked=True)
        rows.append(row)
    # By the recording's own creation stamp where it has one - it survives a
    # rename or a copy, and two files written in the same second tie on
    # mtime - falling back to the file's modification time.
    rows.sort(key=lambda r: r.get('created') or r['mtime'], reverse=True)
    with last_observation_lock:
        last = (os.path.basename(last_observation['output_file'])
                if last_observation and last_observation.get('output_file') else None)
    return jsonify({'success': True, 'observations': rows, 'last': last})


@app.route('/api/observe/plot', methods=['GET'])
def api_observe_plot():
    """Render the last finished observation to a PNG.

    Drawn on demand rather than when the observation ends: the run may finish
    with nobody watching, and a plot nobody asked for is one more thing to keep
    in step with the file.
    """
    import observation_plot
    # ?file=<basename> picks any recording in the folder; without it, the
    # run that most recently finished.
    chosen = request.args.get('file')
    if chosen:
        try:
            info = _observation_info(chosen)
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 404
    else:
        with last_observation_lock:
            if last_observation is None:
                return jsonify({'success': False,
                                'error': 'No observation has finished yet'}), 404
            info = dict(last_observation)
    out = os.path.join(_SCRIPT_DIR, 'data', 'last_observation.png')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        observation_plot.plot_observation(
            info['output_file'], out, name=info.get('name', ''),
            mode=info.get('mode', 'spectrum'),
            transit_minutes=info.get('transit_minutes'))
    except FileNotFoundError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 404
    except (RuntimeError, ValueError) as exc:
        # Still recording, no spectra, or no h5py/matplotlib - all things the
        # operator can act on, so the reason goes back rather than a 500.
        return jsonify({'success': False, 'error': str(exc)}), 409
    except Exception as exc:
        log.error("Observation plot failed: %s", exc, exc_info=True)
        return jsonify({'success': False, 'error': str(exc)}), 500
    from flask import send_file
    return send_file(out, mimetype='image/png')


@app.route('/api/simulator/realise', methods=['POST'])
def api_simulator_realise():
    """Point the telescope at what the simulator is simulating.

    Deliberately a narrow endpoint rather than a general /api/controller/<path>
    proxy. Serving the simulator from this origin already hands that page the
    scheduler's authority; forwarding arbitrary paths would hand it the whole
    unauthenticated controller API as well, including the drive and
    configuration endpoints. This exposes the two commands Realise means, with
    their arguments validated here.

    Both come from astro_simulator.py's Realise button, which keeps this
    capability on the desktop:
      H I map     - track the galactic coordinate.
      continuum   - a drift scan: tracking off, parked where the source will be
                    half a scan from now, so it crosses beam centre at the
                    middle of the scan.
    """
    body = request.json or {}
    try:
        glon = float(body.get('l'))
        glat = float(body.get('b'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'l and b must be numbers'}), 400
    if not (math.isfinite(glon) and math.isfinite(glat)):
        return jsonify({'success': False, 'error': 'l and b must be finite'}), 400

    mode = body.get('mode', 'hi')
    if mode not in ('hi', 'cont'):
        return jsonify({'success': False, 'error': f'unknown mode {mode!r}'}), 400

    glon = glon % 360.0
    glat = max(-90.0, min(90.0, glat))

    # Hand the receiver settings to the Observe tab whichever branch runs, and
    # before either commands anything: the point of Realise is that the real
    # observation is set up like the simulated one, and that should not depend
    # on the telescope having been reachable at that moment. A target that is
    # below the horizon right now is the clearest case - the pointing is
    # refused, and the settings are exactly what is wanted on the Observe tab
    # to book it for a time when it is up. Every reply from here on says the
    # copy happened, so the page reports it rather than inferring it.
    _record_observe_params(body, glon, glat, mode)

    if mode == 'hi':
        log.info("Realise: tracking galactic l=%.3f deg b=%.3f deg", glon, glat)
        result = srt_api_call('/track/galactic',
                              {'l': round(glon, 3), 'b': round(glat, 3)})
        if result is None:
            return jsonify({'params_copied': True, 'success': False,
                            'error': 'SRT controller not reachable'}), 502
        return jsonify({'params_copied': True, 'success': True, 'action': 'track',
                        'l': glon, 'b': glat, 'controller': result})

    if not EPHEM_AVAILABLE:
        return jsonify({'params_copied': True, 'success': False,
                        'error': 'PyEphem is unavailable, so the drift-scan '
                                 'pointing cannot be computed'}), 501
    try:
        minutes = float(body.get('scan_minutes', 240.0))
    except (TypeError, ValueError):
        minutes = 240.0
    if not math.isfinite(minutes):
        minutes = 240.0
    minutes = max(2.0, min(1435.0, minutes))

    transit = datetime.now() + timedelta(minutes=minutes / 2.0)
    pointing = compute_drift_pointing('galactic', glon, glat, transit)
    if pointing is None:
        return jsonify({'params_copied': True, 'success': False,
                        'error': 'drift pointing could not be computed'}), 500
    alt, az = pointing
    # The controller enforces its own observing horizon and mount limits and
    # says why it refused; this only rejects what is unambiguously pointless,
    # so that "below the horizon in half a scan" reads as that rather than as a
    # controller error.
    if alt <= 0.0:
        return jsonify({'params_copied': True, 'success': False,
                        'error': f'the drift-scan start is below the horizon '
                                 f'(alt {alt:.1f} deg) - not sent'}), 400

    log.info("Realise: drift scan of galactic l=%.3f deg b=%.3f deg - parking "
             "at alt %.2f deg az %.2f deg, transit in %.0f min",
             glon, glat, alt, az, minutes / 2.0)
    srt_api_call('/tracking/enable', {'enable': 0})
    result = srt_api_call('/direct', {'alt': round(alt, 2), 'az': round(az, 2)})
    if result is None:
        return jsonify({'params_copied': True, 'success': False,
                        'error': 'SRT controller not reachable'}), 502
    return jsonify({'params_copied': True, 'success': True, 'action': 'drift',
                    'l': glon, 'b': glat, 'alt': alt, 'az': az,
                    'transit_minutes': minutes / 2.0, 'controller': result})


@app.route('/api/schedule', methods=['GET'])
def get_schedule():
    return jsonify(load_schedule())


# Per-entry tuning fields from before the fixed instrument (issue #27).
_RETIRED_TUNING_KEYS = ('center_freq_mhz', 'bandwidth_mhz', 'channels', 'gain_db')


def _store_schedule(schedule):
    """Trim, check for clashes, and save. Returns (notes, clashes).

    Saves only when there are no clashes. Shared by POST /api/schedule and by
    the simulator's Schedule button, so an entry arriving by either route
    gets the same horizon trim and the same refusal.
    """
    # Trim each window to the part where the target clears the measured
    # horizon, before the clash check - two observations that no longer
    # overlap once trimmed are not a clash, and one that has been trimmed into
    # a clash is one. Done here rather than in the browser so it holds for
    # anything that posts a schedule, and because the horizon profile lives on
    # this side. Idempotent: re-saving an already-trimmed entry changes
    # nothing, which is what makes rewriting the stored times safe.
    notes = []
    for obs in schedule if isinstance(schedule, list) else []:
        # The tuning is the fixed instrument's (issue #27). Entries saved
        # before it, or posted by a stale tab, still carry their own; strip
        # them so nothing downstream can mistake them for a choice.
        for key in _RETIRED_TUNING_KEYS:
            obs.pop(key, None)
        if not isinstance(obs, dict):
            continue
        before = (obs.get('start_time'), obs.get('duration_minutes'))
        try:
            apply_horizon_trim(obs)
        except Exception as exc:                          # noqa: BLE001
            log.warning("Could not check %s against the horizon: %s",
                        obs.get('name'), exc)
            continue
        if obs.get('horizon_blocked'):
            notes.append("%s: %s" % (obs.get('name', 'observation'),
                                     obs.get('horizon_note')))
            log.warning("Local horizon: %s will not run - %s",
                        obs.get('name'), obs.get('horizon_note'))
        elif (obs.get('start_time'), obs.get('duration_minutes')) != before:
            notes.append("%s: %s" % (obs.get('name', 'observation'),
                                     obs.get('horizon_note')))
            log.info("Local horizon: %s %s", obs.get('name'),
                     obs.get('horizon_note'))

    # Stored in chronological order, so every reader - the page, the log at
    # startup, anyone opening the JSON - sees the bookings in the order they
    # will run. Undated or untimed entries go last, in the order given.
    if isinstance(schedule, list):
        schedule.sort(key=_schedule_order)
    clashes = find_clashes(schedule)
    if not clashes:
        save_schedule(schedule)
    return notes, clashes


def _schedule_order(obs):
    """Sort key: start datetime, with entries that have none at the end."""
    try:
        return (0, datetime.strptime('%s %s' % (obs.get('start_date'), obs.get('start_time')),
                                     '%Y-%m-%d %H:%M'))
    except (TypeError, ValueError):
        return (1, datetime.max)


@app.route('/api/schedule', methods=['POST'])
def post_schedule():
    schedule = request.json
    notes, clashes = _store_schedule(schedule)
    if clashes:
        return jsonify({'success': False, 'error': f'Schedule has clashing observations: {clashes}'}), 400
    # Hand back what was actually stored. The trim may have moved the times
    # just posted, and a client that keeps showing what it *sent* is showing a
    # schedule that will not run - which is exactly what happened: the alert
    # announced a trim while the list and the edit window went on displaying
    # 06:00 for an entry stored as 09:42.
    return jsonify({'success': True, 'horizon_notes': notes,
                    'schedule': schedule})


SIMULATOR_COMMENT = "Observation set via the Simulator"


# A booking from the simulator starts no sooner than this many seconds ahead,
# rounded up to the whole minute: the scheduler thread polls every 30 s and
# needs more than a minute of slot left when it looks.
SIMULATOR_LEAD_S = 45
SIMULATOR_MIN_SPECTRUM_MIN = 2


def _next_whole_minute(when: datetime) -> datetime:
    """`when` rounded up to the next whole minute (unchanged if already one)."""
    floored = when.replace(second=0, microsecond=0)
    return floored if floored == when else floored + timedelta(minutes=1)


def _simulator_epoch(stamp):
    """The simulator's clock as naive local time - the schedule's frame.

    The page sends its clock as UTC ISO (pinned or live). Missing or
    unparseable means now.
    """
    try:
        when = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone().replace(tzinfo=None)
    except (TypeError, ValueError):
        return datetime.now()


@app.route('/api/simulator/schedule', methods=['POST'])
def api_simulator_schedule():
    """Turn what the simulator is showing into a schedule entry.

    The page's Schedule button. Where Realise commanded the telescope now,
    this books the observation: the pointing, the receiver settings and the
    mode become an entry exactly as the schedule form would have made it,
    with the comment saying where it came from. Nothing moves.

    Times come from the simulator's own clock, which may be pinned to another
    moment - that is the point of pinning it. Both modes *start at that
    moment*: a tracked spectrum runs for the simulator's integration time; a
    drift scan runs for the scan length, parked where the target will be at
    the mid-point, so the source crosses beam centre half a scan in - which
    is what the simulator's drift panel draws. The first version centred a
    drift scan on the target's next meridian transit instead, on the grounds
    that transit is the classical geometry; with the clock live that booked
    tomorrow morning for a scan asked for now, and the scheduler's drift
    machinery has never needed transit - it parks for any T. A transit-centred
    scan is still one click away: pin the clock to transit minus half a scan.
    The entry then goes through the same trim and clash check as any other
    save.
    """
    body = request.json or {}
    try:
        glon = float(body.get('l'))
        glat = float(body.get('b'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'l and b must be numbers'}), 400
    if not (math.isfinite(glon) and math.isfinite(glat)):
        return jsonify({'success': False, 'error': 'l and b must be finite'}), 400
    mode = body.get('mode', 'hi')
    if mode not in ('hi', 'cont'):
        return jsonify({'success': False, 'error': f'unknown mode {mode!r}'}), 400
    glon = glon % 360.0
    glat = max(-90.0, min(90.0, glat))
    drift = mode == 'cont'
    epoch = _simulator_epoch(body.get('epoch_utc'))
    tau = _clamped('integration_time_s', body.get('integration_time_s'), 3.0)

    # When it starts. The schedule works in whole minutes, so the clock is
    # rounded *up*, with at least SIMULATOR_LEAD_S in hand: a live clock at
    # 21:25:57 used to book 21:25, a minute already gone, and a one-minute
    # spectrum then arrived in the list expired - scheduler_thread will not
    # take a slot with under a minute left, and the page says so. A pinned
    # clock in the past cannot be observed at all and is refused rather than
    # booked dead.
    now = datetime.now()
    if epoch < now - timedelta(seconds=60):
        return jsonify({'success': False,
                        'error': "the simulator's clock is in the past (%s); set it "
                                 "ahead, or press Now" % epoch.strftime('%Y-%m-%d %H:%M')}), 409
    epoch = _next_whole_minute(max(epoch, now + timedelta(seconds=SIMULATOR_LEAD_S)))

    entry = {
        'name': ('Drift scan' if drift else 'Spectrum')
                + ' l=%.1f b=%+.1f' % (glon, glat),
        'comment': SIMULATOR_COMMENT,
        'coord_system': 'drift' if drift else 'galactic',
        'object_name': '', 'tle_text': '',
        # Decimal degrees in the degrees field; dms_to_decimal sums as given.
        'coord1_deg': round(glon, 4), 'coord1_min': 0, 'coord1_sec': 0,
        'coord2_deg': round(glat, 4), 'coord2_min': 0, 'coord2_sec': 0,
        # No tuning: the fixed instrument's (issue #27), whatever the
        # simulator page was showing.
        'sdr_type': 'b210', 'calibrator': False,
        'end_action': 'none', 'respect_local_horizon': True,
        'filename': '', 'enabled': True,
        'drift_frame': 'galactic', 'drift_time': '', 'drift_window_min': 30,
    }
    if drift:
        scan = _clamped('duration_minutes', body.get('scan_minutes'), 240.0)
        window = max(1, int(round(scan / 2.0)))
        start = epoch
        crossing = start + timedelta(minutes=window)
        entry.update(
            drift_time=crossing.strftime('%H:%M'),
            drift_window_min=window,
            start_date=start.strftime('%Y-%m-%d'),
            start_time=start.strftime('%H:%M'),
            duration_minutes=2 * window,
            integration_time_s=tau,          # time per sample
        )
    else:
        # tau is the whole integration for a simulated spectrum (see
        # _record_observe_params); the per-spectrum record length is a
        # granularity the simulation says nothing about. Two minutes at
        # least: a one-minute slot is inside scheduler_thread's own cutoff
        # from the moment it starts.
        minutes = max(SIMULATOR_MIN_SPECTRUM_MIN, int(round(tau / 60.0)))
        entry.update(
            start_date=epoch.strftime('%Y-%m-%d'),
            start_time=epoch.strftime('%H:%M'),
            duration_minutes=minutes,
            integration_time_s=3.0,
        )
    start_dt = datetime.strptime(entry['start_date'] + ' ' + entry['start_time'],
                                 '%Y-%m-%d %H:%M')
    end_dt = start_dt + timedelta(minutes=entry['duration_minutes'])
    entry['end_date'] = end_dt.strftime('%Y-%m-%d')
    entry['end_time'] = end_dt.strftime('%H:%M')

    schedule = load_schedule()
    schedule.append(entry)
    notes, clashes = _store_schedule(schedule)
    if clashes:
        return jsonify({'success': False,
                        'error': 'clashes with the schedule: %s' % clashes}), 409
    log.info("Simulator scheduled: %s at %s %s (%d min)", entry['name'],
             entry['start_date'], entry['start_time'], entry['duration_minutes'])
    return jsonify({'success': True, 'entry': entry, 'horizon_notes': notes})


@app.route('/api/status', methods=['GET'])
def get_status():
    with process_lock:
        running = current_process is not None and current_process.poll() is None
    # Also count calibration day as running
    if not running and current_observation and current_observation.get('coord_system') == 'calibration':
        running = cal_day_state["running"] or cal_day_state["finished"]
    if not running and current_observation and current_observation.get('coord_system') == 'horizon':
        running = horizon_state["running"]
    remaining = None
    if running and observation_end_time:
        remaining = max(0, (observation_end_time - datetime.now()).total_seconds())
    return jsonify({
        'running': running,
        'observation': current_observation if running else None,
        'remaining_seconds': remaining
    })


@app.route('/api/receiver/status', methods=['GET'])
def api_receiver_status():
    """Report whether a manual or scheduled receiver process is running."""
    return jsonify(receiver_status_snapshot())


@app.route('/api/receiver/start', methods=['POST'])
def api_receiver_start():
    """Start the B210 receiver using radioconda so the scheduler can launch it."""
    global receiver_boot_process
    python_path = receiver_python_path()
    repo_root = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))

    if not os.path.exists(RECEIVER_SCRIPT):
        return jsonify({'success': False, 'running': False,
                        'error': f'Receiver script not found: {RECEIVER_SCRIPT}'}), 404
    if not os.path.exists(python_path):
        return jsonify({'success': False, 'running': False,
                        'error': f'Python not found: {python_path}'}), 400

    if sun_scan_state["running"] or cal_day_state["running"]:
        return jsonify({
            'success': False,
            'running': False,
            'error': 'The B210 is reserved by Sun Scan calibration',
            'python': python_path,
        }), 409

    status = receiver_status_snapshot()
    if status["running"] and status["source"] == "observation":
        return jsonify(status)

    with receiver_boot_lock:
        if receiver_boot_process is not None and receiver_boot_process.poll() is None:
            return jsonify({'success': True, 'running': True,
                            'source': 'manual',
                            'pid': receiver_boot_process.pid,
                            'returncode': None,
                            'observation': None,
                            'python': python_path})

        env = receiver_process_env(python_path=python_path)
        # Tell it where to record. Without this it falls back to its own
        # default, and before 2026-08-25 that default was a bare "h1_data.h5"
        # resolved against the working directory - which is the repository
        # root, where it had left 22 stray files. Marked `manual` rather than
        # track or drift because nobody commanded the mount.
        env["H1_OUTPUT_FILE"] = observation_files.observation_filename(
            observations_folder(), observation_files.MANUAL_MODE)

        cmd = [python_path, RECEIVER_SCRIPT, "--sdr", "b210"]
        try:
            receiver_boot_process = subprocess.Popen(cmd, cwd=repo_root, env=env)
        except Exception as exc:
            log.error("Failed to start receiver: %s", exc)
            return jsonify({'success': False, 'running': False, 'error': str(exc),
                            'python': python_path}), 500

        log.info("Receiver started: %s", " ".join(cmd))
        return jsonify({'success': True, 'running': True,
                        'source': 'manual',
                        'pid': receiver_boot_process.pid,
                        'returncode': None,
                        'observation': None,
                        'python': python_path})


@app.route('/api/start', methods=['POST'])
def api_start():
    """A manual start: Start Now on the Observe tab, or Run Now on a row.

    Refused, not queued and not preempting, if an observation is already
    recording - preemption is the scheduler thread's business, for a booking
    whose slot has come due. The refusal says which run is in the way and
    when it ends; the old message conflated this with a start that failed
    and sent the operator to a log that had nothing in it.
    """
    obs = request.json
    with process_lock:
        busy = (current_process is not None and current_process.poll() is None)
        running = dict(current_observation) if busy and current_observation else None
        starting = observation_starting
    if busy or starting:
        name = (running or {}).get('name') or starting_observation_name or 'an observation'
        ends = (running or {}).get('ends_at', '')
        ends = ' until ' + ends[11:16] + ' local' if len(ends) >= 16 else ''
        return jsonify({'success': False,
                        'error': "'%s' is already recording%s. Stop it first, or book "
                                 "this in the schedule - a booking preempts at its slot."
                                 % (name, ends)}), 409
    success = start_observation(obs)
    return jsonify({'success': success,
                    'error': None if success else 'Failed to start - see the Log tab'})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    success = stop_observation()
    return jsonify({'success': success})


@app.route('/api/stop_all', methods=['POST'])
def api_stop_all():
    """Stop receiver/scheduled run and cancel telescope tracking via the ESP32."""
    obs_stopped = stop_observation()
    tracking_stopped = srt_stop_tracking()
    return jsonify({'success': obs_stopped or tracking_stopped,
                    'observation_stopped': obs_stopped,
                    'tracking_stopped': tracking_stopped})


@app.route('/api/firmware/update', methods=['POST'])
def api_firmware_update():
    """Start an ESP32 OTA firmware update from the local project checkout."""
    with firmware_update_lock:
        if firmware_update_state["running"]:
            return jsonify({'success': False, 'error': 'Firmware update already running'}), 409
        firmware_update_state.update({
            "running": True,
            "success": None,
            "started_at": datetime.now().isoformat(),
            "finished_at": None,
            "returncode": None,
            "message": "Starting firmware update...",
            "output": [],
        })

    thread = threading.Thread(target=_run_firmware_update, daemon=True)
    thread.start()
    return jsonify({'success': True, 'status': firmware_update_state})


@app.route('/api/firmware/status', methods=['GET'])
def api_firmware_status():
    with firmware_update_lock:
        return jsonify(dict(firmware_update_state))


@app.route('/api/telescope', methods=['GET'])
def api_telescope():
    """Get telescope (SRT controller) status."""
    if not SRT_CONTROLLER_URL:
        return jsonify({
            'configured': False,
            'url': None,
            'connected': False,
            'status': None,
            'tracking': None
        })

    status = srt_get_status()
    tracking = srt_get_tracking()

    return jsonify({
        'configured': True,
        'url': SRT_CONTROLLER_URL,
        'connected': status is not None,
        'status': status,
        'tracking': tracking
    })


@app.route('/api/config', methods=['GET'])
def api_get_config():
    return jsonify(load_config())


@app.route('/api/config', methods=['POST'])
def api_post_config():
    global SRT_CONTROLLER_URL, SRT_SLEW_TIMEOUT, SRT_POSITION_TOLERANCE, PYTHON_PATH
    updates = request.json or {}
    # Only keys that exist in the defaults are configurable; anything else
    # is rejected loudly rather than silently stored.
    unknown = sorted(set(updates) - set(_DEFAULT_CONFIG))
    if unknown:
        return jsonify({'success': False,
                        'error': f"Unknown config keys: {', '.join(unknown)}"}), 400
    cfg = load_config()
    # The instrument (issue #27): an override has to describe a receiver that
    # can be built - the sub-band inside the decimated rate, channels
    # positive, the bands ordered - or it is refused whole, before anything
    # is written. And a change is logged loudly, because every recording
    # after it is uncalibrated until the bandpass and gain are re-measured.
    instrument_keys = [k for k in updates if k.startswith('receiver_')
                       and k[len('receiver_'):] in tuning_instrument_keys()]
    if instrument_keys:
        import tuning
        try:
            merged = dict(cfg, **updates)
            inst = tuning.fixed_instrument(merged)
            tuning.h1_subband_plan(inst)
            if inst['sample_rate_hz'] <= 0 or inst['gain_db'] < 0:
                raise ValueError("sample rate and gain must be positive")
            if inst['h1_band_hz'][0] >= inst['h1_band_hz'][1]:
                raise ValueError("the H I band's low edge must be below its high edge")
            half = 0.5 * inst['sample_rate_hz']
            if (inst['h1_band_hz'][0] < inst['lo_hz'] - half
                    or inst['h1_band_hz'][1] > inst['lo_hz'] + half):
                raise ValueError("the H I band lies outside the sampled band")
            if inst['h1_channels'] < 64 or inst['wide_channels'] < 64:
                raise ValueError("channel counts must be at least 64")
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            return jsonify({'success': False,
                            'error': 'instrument refused: %s' % exc}), 400
        before = instrument_in_force()
        if inst != before:
            log.warning("INSTRUMENT CHANGED by the Configuration tab: %s -> %s. The "
                        "bandpass templates and gain calibration belong to the old "
                        "tuning; re-measure them before trusting any kelvin.",
                        tuning.describe_instrument(before), tuning.describe_instrument(inst))
    cfg.update(updates)
    save_config(cfg)
    # Apply to running process, as one unit: the scheduler thread must not see
    # a new controller URL paired with the previous slew timeout.
    with controller_settings_lock:
        SRT_CONTROLLER_URL = cfg.get("srt_controller_url") or None
        SRT_SLEW_TIMEOUT = cfg.get("slew_timeout", 300)
        SRT_POSITION_TOLERANCE = cfg.get("position_tolerance", 0.5)
        PYTHON_PATH = cfg.get("python_path") or None
    # Re-sync observer location if controller URL changed
    sync_observer_from_controller()
    return jsonify({'success': True})


@app.route('/api/fetch_tle', methods=['POST'])
def api_fetch_tle():
    """Fetch TLE from CelesTrak by name or NORAD ID."""
    data = request.json
    query = data.get('query', '').strip()
    if not query:
        return jsonify({'success': False, 'error': 'No search query'}), 400

    # Try NORAD ID (numeric) or name search
    if query.isdigit():
        url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={query}&FORMAT=TLE"
    else:
        url = f"https://celestrak.org/NORAD/elements/gp.php?NAME={urllib.parse.quote(query)}&FORMAT=TLE"

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'H1-Scheduler/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode().strip()

        if not body or 'No GP data found' in body:
            return jsonify({'success': False, 'error': f'No TLE found for "{query}"'})

        # Parse all returned TLEs into a list
        lines = body.splitlines()
        results = []
        i = 0
        while i < len(lines):
            if i + 2 < len(lines) and lines[i+1].startswith('1 ') and lines[i+2].startswith('2 '):
                results.append({
                    'name': lines[i].strip(),
                    'tle': lines[i] + '\n' + lines[i+1] + '\n' + lines[i+2]
                })
                i += 3
            else:
                i += 1

        if not results:
            return jsonify({'success': False, 'error': 'Unexpected TLE format'})

        return jsonify({'success': True, 'results': results})
    except urllib.error.URLError as e:
        return jsonify({'success': False, 'error': f'Network error: {e}'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/predict_pass', methods=['POST'])
def api_predict_pass():
    """Predict next satellite pass from TLE."""
    if not EPHEM_AVAILABLE:
        return jsonify({'success': False, 'error': 'PyEphem not installed'}), 500
    data = request.json
    tle_text = data.get('tle_text', '')
    if not tle_text:
        return jsonify({'success': False, 'error': 'No TLE data provided'}), 400
    try:
        result = predict_next_pass(tle_text)
        if result:
            return jsonify({'success': True, 'pass': result})
        else:
            min_el = get_config_value("min_elevation")
            return jsonify({'success': False, 'error': f'No pass above {min_el}° found in next 24h'})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/drift_preview', methods=['GET'])
def api_drift_preview():
    """Preview a drift-scan pointing.

    Query params: frame (radec|galactic), coord1/coord2 (decimal hours for RA,
    degrees otherwise), date (YYYY-MM-DD, optional, default today) and time
    (HH:MM) - the beam-crossing time T in local time.

    Returns the alt/az the dish would be parked at, reachability warnings, and
    the source's next meridian transit (the classical drift-scan choice).
    """
    if not EPHEM_AVAILABLE:
        return jsonify({'success': False, 'error': 'PyEphem not installed'}), 500
    frame = request.args.get('frame', 'radec')
    if frame not in ('radec', 'galactic', 'object'):
        return jsonify({'success': False, 'error': f"Unknown frame '{frame}'"}), 400
    try:
        coord1 = float(request.args.get('coord1') or 0.0)
        coord2 = float(request.args.get('coord2') or 0.0)
        date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
        when = datetime.strptime(f"{date_str} {request.args.get('time', '')}",
                                 '%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return jsonify({'success': False,
                        'error': 'Need coord1, coord2 and time=HH:MM (local)'}), 400

    try:
        alt, az = compute_drift_pointing(frame, coord1, coord2, when,
                                         request.args.get('object', ''))

        warnings = []
        reachable = True
        if alt < DRIFT_MIN_ALT:
            warnings.append('below the horizon')
            reachable = False
        elif alt < get_config_value('min_elevation'):
            warnings.append(f"below the {get_config_value('min_elevation'):g}° minimum elevation")
        if az > DRIFT_MAX_AZ:
            warnings.append('in the azimuth dead zone (355-360°)')
            reachable = False

        # Next meridian transit after T. The few minutes of slack keep the
        # reported transit stable once the user adopts it as T.
        observer = _get_observer()
        observer.date = _local_to_ephem_utc(when - timedelta(minutes=5))
        transit_local = ephem.localtime(observer.next_transit(_drift_body(frame, coord1, coord2)))

        return jsonify({
            'success': True,
            'alt': round(alt, 2),
            'az': round(az, 2),
            'reachable': reachable,
            'warnings': warnings,
            'next_transit_date': transit_local.strftime('%Y-%m-%d'),
            'next_transit_time': transit_local.strftime('%H:%M'),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/log', methods=['GET'])
def api_get_log():
    n = get_config_value("log_lines") or 100
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
        lines = [l.rstrip() for l in all_lines[-n:]]
    except FileNotFoundError:
        lines = ["(no log file yet)"]
    except Exception as e:
        lines = [f"Error reading log: {e}"]
    return jsonify({'lines': lines})


# =============================================================================
# Sun Scan API
# =============================================================================

@app.route('/api/sunscan/start', methods=['POST'])
def api_sunscan_start():
    global sun_scan_thread
    # One shared matrix rather than this endpoint's own list; see
    # hardware_in_use for the four holes that drift produced.
    busy = hardware_in_use()
    if busy:
        return jsonify({'success': False, 'error': 'Cannot start a Sun scan: %s' % busy}), 409

    if sun_scan_state["running"] or (sun_scan_thread and sun_scan_thread.is_alive()):
        return jsonify({'success': False, 'error': 'Scan already running'})
    if cal_day_state["running"]:
        return jsonify({'success': False, 'error': 'Calibration day is already running'})

    # Check receiver is not in use
    with process_lock:
        if current_process is not None and current_process.poll() is None:
            return jsonify({'success': False,
                            'error': 'Receiver is busy with an observation'})
    receiver_status = receiver_status_snapshot()
    if receiver_status["running"]:
        return jsonify({'success': False,
                        'error': 'Receiver is already running; stop it before a Sun scan'})

    try:
        params = _validate_sun_scan_params(request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    # Every point of the raster against the measured horizon, before any of it
    # is driven. Unlike everywhere else the horizon is consulted, this one
    # *refuses*: a scan with foliage in any of its points is not a weak scan
    # but a wrong one - the ramp under the source pulls the single Gaussian's
    # centroid, the fit succeeds, and the result is saved looking as
    # respectable as any other. There is nothing to be gained by taking it.
    #
    # Unticking "respect local horizon" is the way to take one anyway.
    warning = None
    if params.get("respect_local_horizon", True):
        try:
            from sun_scan import (get_sun_altaz, parse_obstruction_sectors,
                                  raster_obstruction)
            cfg = load_config()
            sun_alt, sun_az = get_sun_altaz(
                float(cfg.get("observer_lat", SITE_LAT_DEG)),
                float(cfg.get("observer_lon", SITE_LON_DEG)),
                float(cfg.get("observer_elevation", 50)))
            sectors = parse_obstruction_sectors(horizon_obstruction_sectors(cfg))
            bad = raster_obstruction(sun_alt, sun_az, params["n"],
                                     params["grid_spacing_deg"], sectors)
            if bad:
                warning = ("the Sun is at alt %.1f° az %.1f°, which puts a raster "
                           "point at alt %.1f° az %.1f° into the measured horizon, "
                           "%.1f° short of clearing it by a beamwidth. Foliage at "
                           "1420 MHz is a ~290 K source, so that point would drag "
                           "the fitted centroid rather than just adding noise."
                           % (sun_alt, sun_az, bad["alt_deg"], bad["az_deg"],
                              bad["shortfall_deg"]))
                log.warning("Refusing the Sun scan: %s", warning)
                sun_scan_state["horizon_warning"] = warning
                return jsonify({'success': False, 'error': warning,
                                'horizon_blocked': True}), 409
        except (KeyError, ValueError, TypeError) as exc:
            log.debug("Could not check the raster against the horizon: %s", exc)
    sun_scan_state["horizon_warning"] = warning

    sun_scan_cancel.clear()
    sun_scan_thread = threading.Thread(target=_run_sun_scan, args=(params,),
                                       daemon=True)
    sun_scan_thread.start()
    return jsonify({'success': True, 'horizon_warning': warning})


@app.route('/api/sunscan/stop', methods=['POST'])
def api_sunscan_stop():
    sun_scan_cancel.set()
    return jsonify({'success': True})


@app.route('/api/sunscan/status', methods=['GET'])
def api_sunscan_status():
    return jsonify({
        'running': sun_scan_state["running"],
        'progress': sun_scan_state["progress"],
        'total': sun_scan_state["total"],
        'point_info': sun_scan_state["point_info"],
        'result': sun_scan_state["result"],
        'error': sun_scan_state["error"],
        'has_image': sun_scan_state["image_path"] is not None
                     and os.path.isfile(sun_scan_state["image_path"]),
        'horizon_warning': sun_scan_state.get("horizon_warning"),
    })


@app.route('/api/sunscan/image', methods=['GET'])
def api_sunscan_image():
    img = sun_scan_state.get("image_path")
    if img and os.path.isfile(img):
        from flask import send_file
        return send_file(img, mimetype='image/png')
    return ('', 404)


# =============================================================================
# Calibration Day API
# =============================================================================

@app.route('/api/calday/start', methods=['POST'])
def api_calday_start():
    global cal_day_thread
    # One shared matrix rather than this endpoint's own list; see
    # hardware_in_use for the four holes that drift produced.
    busy = hardware_in_use()
    if busy:
        return jsonify({'success': False, 'error': 'Cannot start a calibration day: %s' % busy}), 409

    if cal_day_state["running"] or (cal_day_thread and cal_day_thread.is_alive()):
        return jsonify({'success': False, 'error': 'Calibration day already running'})
    if sun_scan_state["running"]:
        return jsonify({'success': False, 'error': 'A Sun scan is already running'})
    with process_lock:
        if current_process is not None and current_process.poll() is None:
            return jsonify({'success': False,
                            'error': 'Receiver is busy with an observation'})
    receiver_status = receiver_status_snapshot()
    if receiver_status["running"]:
        return jsonify({'success': False,
                        'error': 'Receiver is already running; stop it before calibration'})
    try:
        params = _validate_sun_scan_params(
            request.get_json(silent=True) or {}, include_interval=True)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    cal_day_cancel.clear()
    cal_day_thread = threading.Thread(target=_run_calibration_day, args=(params,),
                                      daemon=True)
    cal_day_thread.start()
    return jsonify({'success': True})


@app.route('/api/calday/stop', methods=['POST'])
def api_calday_stop():
    cal_day_cancel.set()
    sun_scan_cancel.set()  # also stop any in-progress scan
    return jsonify({'success': True})


@app.route('/api/calday/status', methods=['GET'])
def api_calday_status():
    return jsonify({
        'running': cal_day_state["running"],
        'finished': cal_day_state["finished"],
        'phase': cal_day_state["phase"],
        'scans_completed': cal_day_state["scans_completed"],
        'consecutive_failures': cal_day_state["consecutive_failures"],
        'last_scan_error': cal_day_state["last_scan_error"],
        'next_scan_time': cal_day_state["next_scan_time"],
        'interval_minutes': cal_day_state["interval_minutes"],
        'error': cal_day_state["error"],
        'scan_running': sun_scan_state["running"],
        'scan_progress': sun_scan_state["progress"],
        'scan_total': sun_scan_state["total"],
    })


@app.route('/api/calday/data', methods=['GET'])
def api_calday_data():
    try:
        from sun_scan import load_pointing_data
        return jsonify({'data': load_pointing_data()})
    except Exception as exc:
        log.error("Could not load calibration data: %s", exc, exc_info=True)
        return jsonify({'data': [], 'error': str(exc)}), 500


@app.route('/api/calday/clear', methods=['POST'])
def api_calday_clear():
    if cal_day_state["running"] or sun_scan_state["running"]:
        return jsonify({'success': False,
                        'error': 'Stop calibration before clearing its data'}), 409
    try:
        from sun_scan import clear_pointing_data
        clear_pointing_data()
    except Exception as exc:
        log.error("Could not clear calibration data: %s", exc, exc_info=True)
        return jsonify({'success': False, 'error': str(exc)}), 500

    # Deleting the scan history here is not enough on its own: the controller
    # holds the fitted model in its own flash and would go on applying it. The
    # clear has to reach the telescope or the two halves disagree, which is the
    # state this whole arrangement exists to prevent.
    controller_cleared = None
    if SRT_CONTROLLER_URL:
        result = srt_api_call("/pointing/clear")
        controller_cleared = bool(result and result.get("ok"))
        if not controller_cleared:
            return jsonify({
                'success': False,
                'error': ('Calibration data was cleared, but the controller still '
                          f'holds its pointing model; response: {result}'),
                'partial': True,
            }), 502

    return jsonify({'success': True, 'controller_cleared': controller_cleared})


@app.route('/api/calday/fit', methods=['POST'])
def api_calday_fit():
    if cal_day_state["running"] or sun_scan_state["running"]:
        return jsonify({'success': False,
                        'error': 'Stop calibration before fitting the model'}), 409
    try:
        from sun_scan import fit_pointing_model, save_pointing_model, generate_calibration_plot
        cfg = load_config()
        model = fit_pointing_model(
            true_lat=cfg.get("observer_lat"),
            true_lon=cfg.get("observer_lon"),
            obstruction_sectors=sun_raster_obstruction_sectors(cfg),
        )
        if model.get("success"):
            # Stamped at fit time, not at apply time, so the date the controller
            # reports is when the model was measured rather than when someone
            # last pressed a button.
            model["fitted_utc"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            save_pointing_model(model)
            data_folder = get_config_value("data_output_folder")
            os.makedirs(data_folder, exist_ok=True)
            plot_path = os.path.join(data_folder, "calibration_day.png")
            try:
                generate_calibration_plot(model, plot_path)
                model["plot_path"] = plot_path
            except Exception as exc:
                model["plot_warning"] = f"Model fitted but plot failed: {exc}"
                log.warning("Could not generate calibration plot: %s", exc)
        return jsonify(model)
    except Exception as exc:
        log.error("Pointing model fit failed: %s", exc, exc_info=True)
        return jsonify({'success': False, 'error': f'Pointing model fit failed: {exc}'}), 500


@app.route('/api/calday/plot', methods=['GET'])
def api_calday_plot():
    data_folder = get_config_value("data_output_folder")
    plot_path = os.path.join(data_folder, "calibration_day.png")
    if os.path.isfile(plot_path):
        from flask import send_file
        return send_file(plot_path, mimetype='image/png')
    return ('', 404)


@app.route('/api/calday/apply', methods=['POST'])
def api_calday_apply():
    """Apply the fitted pointing model to the ESP32 controller settings."""
    from sun_scan import load_pointing_model
    model = load_pointing_model()
    if not model or not model.get("success"):
        return jsonify({'success': False, 'error': 'No valid pointing model to apply'})
    # Geometry checks only say the four parameters are separable, not that they
    # were actually measured.  The fit already reports how significant each
    # parameter is and how well it describes the scans, so require both before
    # anything is pushed to the telescope.
    try:
        geometry_valid = (
            int(model.get("n_scans", 0)) >= 4 and
            float(model.get("az_coverage_deg", 0)) >= 30 and
            math.isfinite(float(model.get("condition_number", float("inf")))) and
            float(model.get("condition_number", float("inf"))) <= 1e4
        )
    except (TypeError, ValueError):
        geometry_valid = False
    if not geometry_valid:
        return jsonify({
            'success': False,
            'error': ('Saved model predates the calibration quality checks or has '
                      'insufficient coverage; fit the model again before applying'),
        })

    try:
        significance = float(model["min_tilt_significance"])
        chi_squared = float(model["reduced_chi_squared"])
    except (KeyError, TypeError, ValueError):
        return jsonify({
            'success': False,
            'error': ('Saved model predates the tilt significance and chi-squared '
                      'checks; fit the model again before applying'),
        })
    if not math.isfinite(significance) or significance < CALDAY_MIN_TILT_SIGNIFICANCE:
        return jsonify({
            'success': False,
            'error': (f'Mount tilt is only measured to {significance:.1f} sigma; '
                      f'at least {CALDAY_MIN_TILT_SIGNIFICANCE:.0f} is required. '
                      'Collect scans across a wider spread of Sun positions - '
                      'a morning set as well as an afternoon one.'),
        })
    if not math.isfinite(chi_squared) or chi_squared > CALDAY_MAX_REDUCED_CHI_SQUARED:
        return jsonify({
            'success': False,
            'error': (f'Model does not describe the scans (reduced chi-squared '
                      f'{chi_squared:.1f}, limit {CALDAY_MAX_REDUCED_CHI_SQUARED:.0f}); '
                      'the residuals are far larger than the scan uncertainties, so '
                      'something other than mount tilt is moving the pointing.'),
        })

    # Update ESP32 controller
    if not SRT_CONTROLLER_URL:
        return jsonify({'success': False,
                        'error': 'SRT controller URL is disabled; cannot apply the model'})

    # The model is sent as itself. It used to be smuggled across as a fictitious
    # observer latitude and longitude plus a write to the operator's pointing
    # offset boxes, which left the controller reporting a location the telescope
    # is not at and put half the calibration somewhere a reboot erased. The
    # observer position is the true site position and is no longer touched here.
    from sun_scan import pointing_model_document
    try:
        document = pointing_model_document(model, fitted_utc=model.get("fitted_utc"))
    except (ValueError, TypeError) as exc:
        return jsonify({
            'success': False,
            'error': (f'Model cannot be sent to the controller: {exc}. '
                      'Saved models fitted before the resident pointing model do '
                      'not carry the fitted terms; fit the model again.'),
        })

    result = srt_api_call("/pointing/apply", json_body=document, timeout=10)
    if not (result and result.get("ok")):
        detail = (result or {}).get("error") or result
        return jsonify({'success': False,
                        'error': f'Controller rejected the pointing model: {detail}'}), 502

    log.info("Applied pointing model to ESP32: %s",
             ", ".join(f"{k}={v:+.4f}" for k, v in document["terms"].items()))

    return jsonify({
        'success': True,
        'terms': document["terms"],
        'n_scans': document["n_scans"],
        'fitted_utc': document["fitted_utc"],
        'controller_model': result.get("model"),
    })


@app.route('/api/calday/model', methods=['GET'])
def api_calday_model():
    from sun_scan import load_pointing_model
    model = load_pointing_model()
    return jsonify(model or {'success': False, 'error': 'No model fitted yet'})


# =============================================================================
# Horizon scan
# =============================================================================

@app.route('/api/instrument', methods=['GET'])
def api_instrument():
    """The fixed instrument every scheduled observation records with (issue
    #27), for the pages to show - read-only by design."""
    import tuning
    inst = instrument_in_force()
    plan = tuning.h1_subband_plan(inst)
    return jsonify({
        'success': True,
        'instrument': inst,
        'description': tuning.describe_instrument(inst),
        'lo_mhz': inst['lo_hz'] / 1e6,
        'sample_rate_mhz': inst['sample_rate_hz'] / 1e6,
        'gain_db': inst['gain_db'],
        'h1_band_mhz': [inst['h1_band_hz'][0] / 1e6, inst['h1_band_hz'][1] / 1e6],
        'h1_channel_khz': plan['channel_width_hz'] / 1e3,
        'h1_channels': int(round((inst['h1_band_hz'][1] - inst['h1_band_hz'][0])
                                 / plan['channel_width_hz'])),
        'continuum_band_mhz': [inst['continuum_band_hz'][0] / 1e6,
                               inst['continuum_band_hz'][1] / 1e6],
        'wide_channels': inst['wide_channels'],
        'wide_channel_khz': inst['sample_rate_hz'] / inst['wide_channels'] / 1e3,
        'overridden': sorted(k for k in tuning.INSTRUMENT_KEYS
                             if load_config().get('receiver_' + k) not in (None, '')),
    })


@app.route('/api/tuning', methods=['GET'])
def api_tuning():
    """What the receiver will actually be tuned to, for a requested setup.

    The page asks rather than working it out, so there is one implementation of
    the rule and the number shown is the number used.
    """
    from tuning import describe_tuning, plan_tuning
    try:
        centre = float(request.args.get('center_freq_mhz', 1420.405752)) * 1e6
        bandwidth = float(request.args.get('bandwidth_mhz', 2.4)) * 1e6
        channels = int(float(request.args.get('channels', 4096)))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'bad tuning request'}), 400
    plan = plan_tuning(centre, bandwidth, channels)
    plan['description'] = describe_tuning(plan)
    plan['success'] = True
    return jsonify(plan)


@app.route('/api/horizon/start', methods=['POST'])
def api_horizon_start():
    global horizon_thread
    # One shared matrix rather than this endpoint's own list; see
    # hardware_in_use for the four holes that drift produced.
    busy = hardware_in_use()
    if busy:
        return jsonify({'success': False, 'error': 'Cannot start a horizon scan: %s' % busy}), 409

    if horizon_state["running"]:
        return jsonify({'success': False, 'error': 'A horizon scan is already running'}), 409
    if sun_scan_state["running"] or cal_day_state["running"]:
        return jsonify({'success': False,
                        'error': 'A Sun scan or calibration day is using the telescope'}), 409
    with process_lock:
        observing = current_process is not None and current_process.poll() is None
    if observing:
        return jsonify({'success': False,
                        'error': 'An observation is running; it owns the SDR'}), 409
    params = request.json or {}
    horizon_thread = threading.Thread(target=_run_horizon_scan, args=(params,),
                                      daemon=True)
    horizon_thread.start()
    return jsonify({'success': True})


@app.route('/api/horizon/stop', methods=['POST'])
def api_horizon_stop():
    horizon_cancel.set()
    return jsonify({'success': True})


@app.route('/api/horizon/status', methods=['GET'])
def api_horizon_status():
    return jsonify({
        'running': horizon_state["running"],
        'progress': horizon_state["progress"],
        'total': horizon_state["total"],
        'point_info': horizon_state["point_info"],
        'error': horizon_state["error"],
        'started_utc': horizon_state["started_utc"],
    })


@app.route('/api/horizon/profile', methods=['GET'])
def api_horizon_profile():
    """The stored profile, summarised - the raw cuts are far too big for the UI.

    Defaults to the horizon in force. `?name=` reads any archived scan instead,
    so two can be compared before one of them is chosen.
    """
    import horizon_store
    from horizon_scan import load_horizon_profile, profile_floors
    name = request.args.get('name')
    if name:
        try:
            profile = horizon_store.load_profile(name)
        except ValueError:
            return jsonify({'success': False, 'error': 'Bad profile name'}), 400
    else:
        profile = load_horizon_profile()
    if not profile:
        return jsonify({'success': False, 'error': 'No horizon profile measured yet'})
    entries = profile.get("entries", [])
    return jsonify({
        'success': True,
        'measured_utc': profile.get("finished_utc"),
        'name': horizon_store.profile_name(profile),
        'date': horizon_store.profile_date(profile),
        'active_name': horizon_store.active_name(),
        'is_active': horizon_store.profile_name(profile) == horizon_store.active_name(),
        # Carried so the page can say so: a profile measured with the demo SDR
        # describes a synthetic horizon and must never be mistaken for the
        # observatory's, least of all by whoever later wires it into the
        # exclusion of real observations.
        'sdr_type': profile.get("sdr_type"),
        'n_azimuths': profile.get("n_azimuths"),
        'az_step_deg': profile.get("az_step_deg"),
        'duration_s': profile.get("duration_s"),
        'clearance_fraction': profile.get("clearance_fraction"),
        'beam_fwhm_deg': profile.get("beam_fwhm_deg"),
        'strips': profile.get("strips", []),
        'sky_references': [
            {'utc': r.get('utc'), 'level': r.get('level'), 'sigma': r.get('sigma')}
            for r in profile.get("sky_references", [])
        ],
        'complete': profile.get("complete"),
        'floors': profile_floors(profile),
        'azimuths': [
            {
                'az': e["az_deg"],
                'edge': (e["fit"] or {}).get("edge_reported_deg"),
                'clear': (e["fit"] or {}).get("alt_clear"),
                'estimator': (e["fit"] or {}).get("estimator"),
                'quality': (e["fit"] or {}).get("quality"),
            } for e in entries
        ],
    })


@app.route('/api/horizon/profiles', methods=['GET'])
def api_horizon_profiles():
    """Every horizon we have measured, newest first, with the chosen one flagged.

    They are kept rather than overwritten because the horizon is seasonal: the
    trees leaf out and are cut back, so an older scan is not a worse scan, and
    which one describes the sky today is a judgement the operator makes.
    """
    import horizon_store
    return jsonify({'success': True,
                    'profiles': horizon_store.list_profiles(),
                    'active': horizon_store.active_name(),
                    'chosen': horizon_store.active_record()})


@app.route('/api/horizon/profiles/select', methods=['POST'])
def api_horizon_select():
    """Choose which measured horizon the rest of the system believes."""
    import horizon_store
    data = request.get_json(silent=True) or {}
    name = data.get('name')
    if not name:
        return jsonify({'success': False, 'error': 'No profile named'}), 400
    try:
        record = horizon_store.set_active(name, note=str(data.get('note') or ''))
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({'success': False, 'error': str(exc)}), 404
    log.info("Horizon in force set to %s%s", record['active'],
             (" (%s)" % record['note']) if record['note'] else "")
    return jsonify({'success': True, 'active': record['active'],
                    'profiles': horizon_store.list_profiles()})


@app.route('/api/horizon/landscape', methods=['GET'])
def api_horizon_landscape():
    """The measured horizon as a Stellarium landscape, ready to install.

    Polygonal rather than a panorama: Stellarium fills the ground below a list
    of azimuth/altitude pairs, so what it draws is the measurement itself
    rather than an artist's impression of it. `use=clearance` (default) draws
    the radiometrically clean sky, `use=edge` the geometric skyline.
    """
    from horizon_scan import load_horizon_profile, zip_stellarium_landscape
    use = 'edge' if request.args.get('use') == 'edge' else 'clearance'
    profile = load_horizon_profile()
    if not profile:
        return jsonify({'success': False,
                        'error': 'No horizon profile measured yet'}), 404
    data_folder = get_config_value("data_output_folder")
    os.makedirs(data_folder, exist_ok=True)
    zip_path = os.path.join(data_folder, f"acreroad_{use}_landscape.zip")
    try:
        zip_stellarium_landscape(profile, zip_path, use=use)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    from flask import send_file
    return send_file(zip_path, mimetype='application/zip', as_attachment=True,
                     download_name=os.path.basename(zip_path))


@app.route('/api/horizon/skyplot', methods=['GET'])
def api_horizon_skyplot():
    """The available sky as an equal-area polar chart.

    Drawn on demand rather than only at the end of a scan, because it has to
    follow whichever profile is in force - and because it is cheap. Cached per
    profile name, so switching between two scans to compare them redraws each
    once and then serves from disk.
    """
    import horizon_store
    from horizon_scan import generate_sky_plot, load_horizon_profile
    name = request.args.get('name')
    if name:
        try:
            profile = horizon_store.load_profile(name)
        except ValueError:
            return jsonify({'success': False, 'error': 'Bad profile name'}), 400
    else:
        profile = load_horizon_profile()
    if not profile:
        return ('', 404)
    folder = get_config_value("data_output_folder")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "sky_%s.png" % horizon_store.profile_name(profile))
    if not os.path.isfile(path):
        try:
            generate_sky_plot(profile, path)
        except (ImportError, ValueError) as exc:
            log.warning("Could not draw the sky plot: %s", exc)
            return ('', 404)
    from flask import send_file
    return send_file(path, mimetype='image/png')


@app.route('/api/horizon/plot', methods=['GET'])
def api_horizon_plot():
    plot_path = os.path.join(get_config_value("data_output_folder"),
                             "horizon_profile.png")
    if os.path.isfile(plot_path):
        from flask import send_file
        return send_file(plot_path, mimetype='image/png')
    return ('', 404)


# =============================================================================
# Safety camera
# =============================================================================

# One frame at a time. Capture is a subprocess that takes the camera for its
# duration, so two clicks landing together would have the second fail and look
# like a broken camera rather than a queued one.
_camera_lock = threading.Lock()
CAMERA_CAPTURE_TIMEOUT = 20

# The first frames off a USB webcam are black or wildly mis-exposed while its
# automatic gain settles, and a black picture is worse than none on a safety
# camera: it reads as "nothing is there". Every frame is written over the same
# place and only the last survives.
#
# How many are needed depends on how recently the camera last ran, because the
# sensor keeps its exposure state across a quick close and reopen. Measured on
# this camera against a settled 15-frame reference, a single frame came out at
# -0.2% mean luminance after 1s idle, -1.0% after 3s, -3.0% after 5s, then
# -11.8% after 15s and -17.3% after a minute. So the state survives a few
# seconds and is gone by fifteen; six is inside the flat part with margin, and
# covers both auto-refresh rates offered by the page.
#
# This is what makes a 1s auto-refresh cheap: 15 frames is 0.67s of streaming
# and about 0.1 core-seconds, while 2 frames is 0.13s and a quarter of the CPU.
# Process start-up is only 40ms, so the frames are nearly the whole cost.
CAMERA_COLD_FRAMES = 15
CAMERA_WARM_FRAMES = 2
CAMERA_WARM_WINDOW_S = 6.0

# When the camera last delivered a frame (monotonic). Written under
# _camera_lock, which is also what stops two captures interleaving.
_camera_last_capture = 0.0


def _camera_env() -> dict:
    """Environment for the capture subprocess.

    Two things have to be right. PipeWire is reached through the user runtime
    directory, which is present when the scheduler is started from the desktop
    launcher but is worth defaulting rather than assuming. And the capture uses
    the *system* GStreamer, because radioconda's build has neither pipewiresrc
    nor v4l2src: conda's library and plugin paths must not be handed to it or it
    loads half of one installation and half of the other.
    """
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    for conda_var in ("GST_PLUGIN_PATH", "GST_PLUGIN_SYSTEM_PATH", "LD_LIBRARY_PATH"):
        env.pop(conda_var, None)
    return env


def _camera_resolution(cfg: dict) -> tuple[int, int]:
    """Requested capture size, falling back to the default on anything odd."""
    text = str(cfg.get("camera_resolution") or _DEFAULT_CONFIG["camera_resolution"])
    try:
        width, height = (int(part) for part in text.lower().split("x", 1))
        if width > 0 and height > 0:
            return width, height
    except (TypeError, ValueError):
        pass
    log.warning("Ignoring camera_resolution %r; using %s",
                text, _DEFAULT_CONFIG["camera_resolution"])
    return (int(part) for part in _DEFAULT_CONFIG["camera_resolution"].split("x"))


def _newest_frame(workdir: str) -> str | None:
    frames = sorted(f for f in os.listdir(workdir) if f.endswith(".jpg"))
    return os.path.join(workdir, frames[-1]) if frames else None


def _capture_via_pipewire(workdir: str, width: int, height: int,
                          target: str, frames: int) -> tuple[str | None, str]:
    """Capture through PipeWire, which owns the camera on a desktop session.

    This is the path that works here. On this host wireplumber holds
    /dev/video0 open for the life of the session, so anything opening the V4L2
    device directly gets EBUSY however idle the camera looks; going through
    PipeWire shares it instead of fighting for it.
    """
    gst = shutil.which("gst-launch-1.0", path="/usr/bin:/bin")
    if not gst:
        return None, "the system gst-launch-1.0 is not installed"
    source = ["pipewiresrc", f"num-buffers={frames}"]
    if target:
        source.append(f"target-object={target}")
    command = [gst, "-q"] + source + [
        "!", "videoconvert", "!", "videoscale",
        "!", f"video/x-raw,width={width},height={height}",
        "!", "jpegenc", "quality=85",
        "!", "multifilesink", f"location={os.path.join(workdir, 'frame%03d.jpg')}",
    ]
    try:
        result = subprocess.run(command, capture_output=True, env=_camera_env(),
                                timeout=CAMERA_CAPTURE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f"PipeWire did not deliver a frame within {CAMERA_CAPTURE_TIMEOUT}s"
    frame = _newest_frame(workdir)
    if result.returncode != 0 or not frame:
        stderr = (result.stderr or b"").decode(errors="replace").strip()
        return None, stderr.splitlines()[-1] if stderr else "no frame from PipeWire"
    return frame, ""


def _capture_via_v4l2(workdir: str, device: str, width: int,
                      height: int, frames: int) -> tuple[str | None, str]:
    """Capture straight off the V4L2 device with ffmpeg.

    The fallback for when there is no desktop session running and so no
    PipeWire to ask - the device is then free for the taking.
    """
    ffmpeg = os.path.join(os.path.dirname(sys.executable), "ffmpeg")
    if not (os.path.isfile(ffmpeg) and os.access(ffmpeg, os.X_OK)):
        ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None, "ffmpeg was not found"
    if not os.path.exists(device):
        return None, f"{device} does not exist"
    frame = os.path.join(workdir, "frame.jpg")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "v4l2", "-video_size", f"{width}x{height}", "-i", device,
        "-frames:v", str(frames), "-update", "1", "-q:v", "3",
        "-y", frame,
    ]
    try:
        result = subprocess.run(command, capture_output=True, env=_camera_env(),
                                timeout=CAMERA_CAPTURE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f"{device} did not deliver a frame within {CAMERA_CAPTURE_TIMEOUT}s"
    if result.returncode != 0 or not os.path.isfile(frame):
        stderr = (result.stderr or b"").decode(errors="replace").strip()
        if "Device or resource busy" in stderr:
            return None, f"{device} is held by another application"
        return None, stderr.splitlines()[-1] if stderr else "no frame from ffmpeg"
    return frame, ""


@app.route('/api/camera/snapshot', methods=['GET'])
def api_camera_snapshot():
    """A single frame from the safety camera, as JPEG.

    Deliberately one frame per request rather than a stream: the point is to
    look at the dish when you want to, and a permanently open V4L2 device would
    be one more thing holding hardware across an overnight run.
    """
    cfg = load_config()
    device = cfg.get("camera_device") or _DEFAULT_CONFIG["camera_device"]
    target = str(cfg.get("camera_pipewire_target") or "")
    width, height = _camera_resolution(cfg)

    global _camera_last_capture

    if not _camera_lock.acquire(timeout=CAMERA_CAPTURE_TIMEOUT):
        return jsonify({'success': False,
                        'error': 'Another snapshot is still being captured'}), 409
    try:
        # A capture that failed says nothing about the sensor's state, so the
        # next one after a failure pays the full warm-up again.
        warm = (time.monotonic() - _camera_last_capture) <= CAMERA_WARM_WINDOW_S
        frames = CAMERA_WARM_FRAMES if warm else CAMERA_COLD_FRAMES
        with tempfile.TemporaryDirectory(prefix="srt-camera-") as workdir:
            captured_utc = datetime.now(timezone.utc)
            frame, pipewire_error = _capture_via_pipewire(workdir, width, height,
                                                          target, frames)
            source = "pipewire"
            v4l2_error = ""
            if not frame:
                # Not a retry of the same thing: the two paths fail in opposite
                # circumstances, PipeWire when there is no session to ask and
                # V4L2 when there is one holding the device.
                frame, v4l2_error = _capture_via_v4l2(workdir, device, width,
                                                      height, frames)
                source = "v4l2"
            if not frame:
                # Forget how warm the camera was. Whatever just went wrong may
                # have been the device disappearing and re-enumerating, which
                # resets the sensor: the next capture has to prove the exposure
                # rather than assume it.
                _camera_last_capture = 0.0
                log.warning("Camera capture failed: pipewire: %s; v4l2: %s",
                            pipewire_error, v4l2_error)
                return jsonify({
                    'success': False,
                    'error': (f'Could not capture a frame. Through PipeWire: '
                              f'{pipewire_error}. Directly from {device}: {v4l2_error}.'),
                }), 503
            with open(frame, "rb") as f:
                jpeg = f.read()
        _camera_last_capture = time.monotonic()
    finally:
        _camera_lock.release()

    return app.response_class(jpeg, mimetype="image/jpeg", headers={
        # The browser must never show a cached frame on a safety camera.
        "Cache-Control": "no-store, must-revalidate",
        "X-Capture-Time": captured_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "X-Capture-Source": source,
        "X-Capture-Frames": str(frames),
    })


def _handle_sigterm(signum, frame):
    """Turn SIGTERM into SystemExit so main()'s cleanup runs.

    Without this, `systemctl stop` (or any kill) orphans the receiver
    subprocess, which keeps the B210 claimed and blocks every observation
    after a scheduler restart until the orphan is killed by hand.
    """
    raise SystemExit(0)


def main():
    global scheduler_running

    import argparse
    parser = argparse.ArgumentParser(description='H1 Receiver Web Scheduler')
    parser.add_argument('--port', type=int, default=5000, help='Port to run on')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind to')
    args = parser.parse_args()

    maybe_reexec_scheduler_under_receiver_python()

    # Check if gnuradio is available
    try:
        import gnuradio
        gr_status = "OK"
    except ImportError:
        gr_status = "NOT FOUND - run from radioconda environment!"

    print(f"\n{'='*50}")
    print("H1 Receiver Web Scheduler")
    print(f"{'='*50}")
    print(f"GNU Radio: {gr_status}")
    print(f"Python: {sys.executable}")
    if SRT_CONTROLLER_URL:
        print(f"SRT Controller: {SRT_CONTROLLER_URL}")
    else:
        print("SRT Controller: DISABLED (telescope control off)")
    # Sync observer location from controller
    sync_observer_from_controller()

    print(f"\nOpen your browser to: http://{args.host}:{args.port}")
    print("Scheduler is ACTIVE - observations will start automatically")
    print("\nPress Ctrl+C to stop\n")

    # Suppress Flask request logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    # Run cleanup on SIGTERM (systemd stop, plain kill), not just Ctrl+C.
    # Signal handlers run in the main thread, which is the one blocked in
    # app.run(), so the SystemExit unwinds through the finally below.
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # Start background scheduler thread
    sched_thread = threading.Thread(target=scheduler_thread, daemon=True)
    sched_thread.start()

    try:
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        scheduler_running = False
        if current_process or current_observation:
            stop_observation()
        stop_booted_receiver()


if __name__ == '__main__':
    main()
