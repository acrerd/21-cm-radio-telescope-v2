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
from flask import Flask, render_template_string, jsonify, request, send_from_directory
import logging
import logging.handlers
import math
import urllib.request
import urllib.error
import urllib.parse

# The observatory's surveyed position, written down in exactly one place.
from observatory import SITE_HEIGHT_M, SITE_LAT_DEG, SITE_LON_DEG

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
    # The obstructed horizon, as [az_min, az_max, min_sun_alt] sectors. The Sun
    # is not scanned while it sits inside one, and scans already on file from
    # inside one are left out of the pointing fit: the trees are a 290 K source
    # at 1.4 GHz, not a screen, so they pull the fitted centroid down towards
    # themselves. See _DEFAULT_OBSTRUCTION_SECTORS in sun_scan.py, which holds
    # the same value and the measurement it came from.
    "obstruction_sectors": [[45.0, 120.0, 30.0]],
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
        pointing = compute_drift_pointing(drift_frame, coord1, coord2, beam_time)
        if pointing is None:
            log.error("SRT drift scan requires PyEphem on the scheduler host")
            return False
        alt, az = pointing
        if not (DRIFT_MIN_ALT <= alt <= DRIFT_MAX_ALT and 0.0 <= az <= DRIFT_MAX_AZ):
            log.error("SRT drift pointing unreachable: Alt=%.2f° Az=%.2f° at %s",
                      alt, az, beam_time.strftime('%Y-%m-%d %H:%M'))
            return False
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


def _drift_body(frame: str, coord1: float, coord2: float) -> 'ephem.FixedBody':
    """Build a FixedBody from RA/Dec (decimal hours, degrees) or galactic l/b (degrees)."""
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


def compute_drift_pointing(frame: str, coord1: float, coord2: float,
                           when_local: datetime) -> Optional[tuple]:
    """Alt/Az (degrees) at which a source will sit at the given local time.

    This is the fixed pointing for a drift scan: park the dish there with
    tracking off and the source crosses beam centre at when_local.
    Returns None if PyEphem is unavailable.
    """
    if not EPHEM_AVAILABLE:
        return None
    observer = _get_observer()
    observer.date = _local_to_ephem_utc(when_local)
    body = _drift_body(frame, coord1, coord2)
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


def generate_filename(obs: dict) -> str:
    """Generate output filename for observation, placed in the data output folder."""
    data_folder = os.path.realpath(get_config_value("data_output_folder"))
    os.makedirs(data_folder, exist_ok=True)
    if obs.get('filename'):
        # Contain the file inside the data folder: relative subfolders are
        # fine, but absolute paths and ../ escapes are rejected.
        candidate = os.path.realpath(os.path.join(data_folder, obs['filename']))
        if candidate != data_folder and candidate.startswith(data_folder + os.sep):
            return candidate
        log.warning("Ignoring output filename outside the data folder: %r",
                    obs['filename'])
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = obs.get('name', 'obs').replace(' ', '_').lower()
    cal = "_cal" if obs.get('calibrator') else ""
    return os.path.join(data_folder, f"h1_{name}{cal}_{timestamp}.h5")


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

        # Set calibrator state
        if SRT_CONTROLLER_URL:
            srt_set_calibrator(obs.get('calibrator', False))

        if start_abort.is_set():
            log.info("Observation start aborted")
            return False

        output_file = generate_filename(obs)
        env = os.environ.copy()
        env['H1_OUTPUT_FILE'] = output_file
        env['H1_CENTER_FREQ'] = str(obs.get('center_freq_mhz', 1420.405752) * 1e6)
        env['H1_FFT_SIZE'] = str(obs.get('channels', 4096))
        env['H1_INTEGRATION_TIME'] = str(obs.get('integration_time_s', 3.0))
        env['H1_OBS_METADATA'] = json.dumps({
            'obs_name': obs.get('name', ''),
            'coord_system': obs.get('coord_system', ''),
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
            '--gain', str(obs.get('gain_db', 40)),
            '--sample-rate', str(obs.get('bandwidth_mhz', 2.4) * 1e6),
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
            'mode': 'drift' if drift else 'spectrum',
            # The scan is laid out so the source crosses beam centre at the
            # mid-point; the plot marks it there.
            'transit_minutes': (duration / 2.0) if drift else None,
            'started_at': obs.get('started_at'),
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


def scheduler_thread():
    """Background thread that checks schedule and starts/stops observations."""
    global scheduler_running

    log.info("Background scheduler started")
    # Recover the pointer to the last finished observation, so a restart does
    # not cost the Observe tab its plot.
    _load_last_observation()
    last_debug = 0

    while scheduler_running:
        try:
            now = datetime.now()
            schedule = load_schedule()

            # Debug output every 30 seconds
            if now.second % 30 < 5 and time.time() - last_debug > 25:
                last_debug = time.time()
                log.debug("%d observations loaded", len(schedule))
                for obs in schedule:
                    obs_date = obs.get('start_date', '') or now.strftime('%Y-%m-%d')
                    obs_time = obs.get('start_time', '')
                    enabled = obs.get('enabled', True)
                    log.debug("  - %s: %s %s (enabled=%s)", obs.get('name'), obs_date, obs_time, enabled)

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
                if is_running and running_name == due_obs.get('name', ''):
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
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ banner_name }}</title>
    <style>
        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
        body { margin: 0; padding: 20px; background: #1a1a2e; color: #eee; min-height: 100vh; }
        h1 { color: #00d4ff; margin-bottom: 10px; }
        .subtitle { color: #888; margin-bottom: 20px; }
        .container { max-width: 1400px; margin: 0 auto; }
        /* The bandpass check is read on the observatory console, never on a
           phone, and the whole point is seeing fine structure across the band.
           Let it out of the 1400px column on a wide screen; below that it
           simply fills the column like everything else. */
        @media (min-width: 1460px) {
            .rf-wide { width: calc(100vw - 40px); max-width: calc(100vw - 40px);
                       margin-left: calc(-50vw + 700px); }
        }
        .rf-wide img { display: block; }
        .status-bar { background: #16213e; padding: 15px; border-radius: 8px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; gap: 18px; flex-wrap: wrap; }
        .status-indicator { display: flex; align-items: center; gap: 10px; }
        .status-group { display: flex; align-items: center; gap: 26px; flex-wrap: wrap; }
        .status-dot { width: 12px; height: 12px; border-radius: 50%; background: #666; }
        .status-dot.running { background: #00ff88; animation: pulse 1s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
        .btn { padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 14px; font-weight: 500; transition: all 0.2s; }
        .btn-primary { background: #00d4ff; color: #000; }
        .btn-primary:hover { background: #00a8cc; }
        .btn-danger { background: #ff4757; color: #fff; }
        .btn-danger:hover { background: #ff3344; }
        .btn-secondary { background: #444; color: #fff; }
        .btn-secondary:hover { background: #555; }
        .btn-success { background: #00ff88; color: #000; }
        .btn-success:hover { background: #00cc6a; }
        .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px; }
        .form-group { display: flex; flex-direction: column; }
        .form-group label { font-size: 11px; color: #888; margin-bottom: 5px; text-transform: uppercase; }
        /* Unit symbols are case-sensitive: MHz, dB and s are not MHZ, DB and S,
           and the uppercasing above would rewrite them into something that
           means milli/decibel-nothing. Wrap the unit, not the whole label, so
           the caps styling on the wording itself stays. */
        .form-group label .unit { text-transform: none; }
        .form-group input, .form-group select { padding: 8px; border: 1px solid #333; border-radius: 5px; background: #0f0f23; color: #fff; font-size: 14px; }
        .form-group input:focus, .form-group select:focus { outline: none; border-color: #00d4ff; }
        .form-group.wide { grid-column: span 2; }
        .coord-row { display: flex; gap: 5px; align-items: center; }
        .coord-row input { width: 60px; text-align: center; }
        .coord-row span { color: #888; font-size: 12px; }
        .schedule-list { margin-top: 20px; }
        .schedule-item { background: #0f0f23; border: 1px solid #333; border-radius: 8px; padding: 15px; margin-bottom: 10px; display: grid; grid-template-columns: auto 1fr auto; gap: 15px; align-items: center; }
        .schedule-item.disabled { opacity: 0.5; }
        /* scheduler_thread() will never pick this slot up - see
           neverRunsReason(). Deliberately not styled the same as .disabled: an
           operator has to be able to tell "I switched this off" from "its time
           has gone", and both can be true of the same row at once. */
        .schedule-item.wont-run { opacity: 0.55; border-style: dashed; border-color: #2b2b3d; }
        .schedule-item.wont-run .field-value { color: #8b8b9c; }
        .tag-wont-run { display: inline-block; margin-left: 8px; padding: 1px 6px; border-radius: 4px;
                        background: #2b2b3d; color: #9a9aad; font-size: 10px; letter-spacing: 0.5px;
                        text-transform: uppercase; vertical-align: middle; }
        .schedule-item .checkbox { width: 20px; height: 20px; cursor: pointer; }
        .schedule-info { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; }
        .schedule-info .field { font-size: 12px; }
        .schedule-info .field-label { color: #666; font-size: 10px; }
        .schedule-info .field-value { color: #fff; }
        .schedule-actions { display: flex; gap: 8px; }
        .btn-icon { padding: 6px 10px; font-size: 12px; }
        .toolbar { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }
        .file-input { display: none; }
        .current-obs { background: #1a3a1a; border: 1px solid #00ff88; }
        .empty-state { text-align: center; padding: 40px; color: #666; }
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); justify-content: center; align-items: center; z-index: 1000; }
        .modal.active { display: flex; }
        .modal-content { background: #16213e; border-radius: 8px; padding: 25px; width: 95%; max-width: 800px; max-height: 90vh; overflow-y: auto; }
        .modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .modal-title { font-size: 20px; color: #00d4ff; }
        .close-btn { background: none; border: none; color: #888; font-size: 24px; cursor: pointer; }
        .close-btn:hover { color: #fff; }
        .modal-footer { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }
        .section-title { color: #00d4ff; font-size: 14px; margin: 20px 0 10px 0; padding-bottom: 5px; border-bottom: 1px solid #333; }
        .coord-section { background: #0f0f23; padding: 15px; border-radius: 8px; margin-bottom: 15px; }
        .tabs { display: flex; gap: 0; margin-bottom: 20px; border-bottom: 2px solid #333; }
        .tab { padding: 10px 25px; cursor: pointer; color: #888; border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all 0.2s; font-size: 14px; }
        .tab:hover { color: #ccc; }
        .tab.active { color: #00d4ff; border-bottom-color: #00d4ff; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .config-form { background: #0f0f23; border: 1px solid #333; border-radius: 8px; padding: 20px; max-width: 700px; }
        .config-form .form-group { margin-bottom: 15px; }
        .config-form input { width: 100%; }
        .config-saved { color: #00ff88; font-size: 13px; display: none; margin-left: 10px; }
        .log-container { background: #0a0a1a; border: 1px solid #333; border-radius: 8px; padding: 15px; font-family: monospace; font-size: 12px; line-height: 1.6; max-height: 70vh; overflow-y: auto; white-space: pre-wrap; word-break: break-all; color: #ccc; }
        .log-controls { display: flex; gap: 10px; align-items: center; margin-bottom: 10px; }
        .log-controls select, .log-controls input { padding: 6px 10px; border: 1px solid #333; border-radius: 5px; background: #0f0f23; color: #fff; font-size: 13px; }
    </style>
</head>
<body>
    <div class="container">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <div>
                <h1>{{ banner_name }}</h1>
                <p class="subtitle">{{ banner_subtitle }}</p>
            </div>
            <div style="text-align:right;">
                <div id="currentDate" style="color:#888; font-size:14px;"></div>
                <div style="display:flex; gap:20px; justify-content:flex-end; align-items:baseline;">
                    <div>
                        <div style="color:#888; font-size:10px; text-transform:uppercase;">Local Time</div>
                        <div id="currentTime" style="color:#00d4ff; font-size:32px; font-family:monospace; font-weight:bold;"></div>
                    </div>
                    <div>
                        <div style="color:#888; font-size:10px; text-transform:uppercase;">UTC</div>
                        <div id="utcTime" style="color:#ff9500; font-size:24px; font-family:monospace; font-weight:bold;"></div>
                    </div>
                </div>
            </div>
        </div>
        <div class="status-bar">
            <div class="status-group">
                <div class="status-indicator">
                    <div class="status-dot" id="statusDot"></div>
                    <span id="statusText">Idle</span>
                </div>
                <div class="status-indicator">
                    <div class="status-dot" id="telescopeDot"></div>
                    <span id="telescopeText">Telescope: --</span>
                </div>
                <div class="status-indicator">
                    <div class="status-dot" id="receiverDot"></div>
                    <span id="receiverText">Receiver: --</span>
                    <button class="btn btn-secondary" id="receiverBootBtn" onclick="bootReceiver()" title="Start the B210 receiver with radioconda Python.">Start receiver</button>
                </div>
            </div>
            <div>
                <button class="btn btn-danger" id="stopBtn" style="display:none" onclick="stopObs()">Stop</button>
            </div>
        </div>
        <div class="tabs">
            <div class="tab active" onclick="switchTab('scheduler')">Scheduler</div>
            <div class="tab" onclick="switchTab('sunscan')">Sun Scan</div>
            <div class="tab" onclick="switchTab('horizon')">Horizon</div>
            <div class="tab" onclick="switchTab('rf')">RF calibration</div>
            <div class="tab" onclick="switchTab('camera')">Camera</div>
            <div class="tab" onclick="switchTab('simulator')">Simulator</div>
            <div class="tab" onclick="switchTab('observe')">Observe</div>
            <div class="tab" onclick="switchTab('config')">Configuration</div>
            <div class="tab" onclick="switchTab('log')">Log</div>
        </div>

        <div class="tab-content active" id="tab-scheduler">
            <div class="toolbar">
                <button class="btn btn-primary" onclick="openAddModal()">+ Add Observation</button>
                <button class="btn btn-secondary" onclick="saveSchedule()">Save Schedule</button>
                <button class="btn btn-secondary" onclick="document.getElementById('loadFile').click()">Load</button>
                <input autocomplete="off" type="file" id="loadFile" class="file-input" accept=".json" onchange="loadFile(event)">
                <button class="btn btn-secondary" onclick="exportSchedule()">Export JSON</button>
                <button class="btn btn-secondary" onclick="clearPast()">Clear Past</button>
            </div>
            <!-- Shown only when an auto-save was rejected. Auto-save is the path
                 that silently loses edits, so a failure has to be visible. -->
            <div id="autoSaveWarning" style="display:none;background:#752;color:#fff;
                 padding:8px 12px;border-radius:4px;margin-bottom:10px;"></div>
            <div class="schedule-list" id="scheduleList">
                <div class="empty-state">No observations scheduled.</div>
            </div>
        </div>

        <div class="tab-content" id="tab-sunscan">
            <div style="display:flex; gap:20px; flex-wrap:wrap;">
                <div class="config-form" style="flex:0 0 340px;">
                    <div class="section-title">Scan Parameters</div>
                    <div class="form-group">
                        <label>Grid Size (n x n)</label>
                        <input autocomplete="off" type="number" id="ssGridN" min="3" max="15" step="2" value="5">
                    </div>
                    <div class="form-group">
                        <label>Grid Spacing (degrees)</label>
                        <input autocomplete="off" type="number" id="ssSpacing" min="0.1" max="10" step="0.1" value="1.5">
                    </div>
                    <div class="form-group">
                        <label>Beam FWHM Hint (degrees)</label>
                        <input autocomplete="off" type="number" id="ssBeamFwhm" min="0.5" max="20" step="0.1" value="3.0">
                    </div>
                    <div class="section-title">Receiver Settings</div>
                    <div class="form-group">
                        <label>Integration Time per Point <span class="unit">(s)</span></label>
                        <input autocomplete="off" type="number" id="ssIntegration" min="0.1" max="60" step="0.1" value="3.0">
                    </div>
                    <div class="form-group">
                        <label>Center Frequency <span class="unit">(MHz)</span></label>
                        <input autocomplete="off" type="number" id="ssCenterFreq" step="any" value="1420.405752">
                    </div>
                    <div class="form-group">
                        <label>Bandwidth <span class="unit">(MHz)</span></label>
                        <input autocomplete="off" type="number" id="ssBandwidth" step="0.1" value="2.4">
                    </div>
                    <div class="form-group">
                        <label>Gain <span class="unit">(dB)</span></label>
                        <input autocomplete="off" type="number" id="ssGain" min="0" max="80" value="40">
                    </div>
                    <div class="form-group">
                        <label>SDR Type</label>
                        <select autocomplete="off" id="ssSdrType">
                            <option value="b210">Ettus B210</option>
                            <option value="rtlsdr">RTL-SDR</option>
                            <option value="demo">Demo (Simulated)</option>
                        </select>
                    </div>
                    <div style="margin-top:15px; display:flex; gap:10px;">
                        <button class="btn btn-primary" id="ssStartBtn" onclick="startSunScan()">Start Sun Scan</button>
                        <button class="btn btn-danger" id="ssStopBtn" style="display:none" onclick="stopSunScan()">Stop</button>
                    </div>
                </div>
                <div style="flex:1; min-width:300px;">
                    <div class="section-title">Status</div>
                    <div id="ssStatus" style="background:#0f0f23; border:1px solid #333; border-radius:8px; padding:15px; margin-bottom:15px;">
                        <span style="color:#888;">Idle — configure parameters and click Start.</span>
                    </div>
                    <div id="ssProgress" style="display:none; margin-bottom:15px;">
                        <div style="background:#333; border-radius:4px; height:20px; overflow:hidden;">
                            <div id="ssProgressBar" style="background:#00d4ff; height:100%; width:0%; transition:width 0.3s;"></div>
                        </div>
                        <div id="ssProgressText" style="color:#888; font-size:12px; margin-top:5px;">0 / 0</div>
                    </div>
                    <div class="section-title">Results</div>
                    <div id="ssResults" style="background:#0f0f23; border:1px solid #333; border-radius:8px; padding:15px; margin-bottom:15px;">
                        <span style="color:#888;">No results yet.</span>
                    </div>
                    <div id="ssImageContainer" style="text-align:center;">
                    </div>
                </div>
            </div>

            <div style="margin-top:30px; border-top:2px solid #333; padding-top:20px;">
                <div style="display:flex; gap:20px; flex-wrap:wrap;">
                    <div class="config-form" style="flex:0 0 340px;">
                        <div class="section-title">Calibration Day</div>
                        <p style="color:#888; font-size:12px; margin-bottom:15px;">
                            Run repeated sun scans to determine the effective observer lat/lon
                            (correcting for mount tilt) and constant alt/az offsets.
                        </p>
                        <div class="form-group">
                            <label>Scan Interval (minutes)</label>
                            <input autocomplete="off" type="number" id="cdInterval" min="5" max="120" value="30">
                        </div>
                        <div style="margin-top:15px; display:flex; gap:10px; flex-wrap:wrap;">
                            <button class="btn btn-success" id="cdStartBtn" onclick="startCalDay()">Start Calibration Day</button>
                            <button class="btn btn-danger" id="cdStopBtn" style="display:none" onclick="stopCalDay()">Stop</button>
                            <button class="btn btn-primary" id="cdFitBtn" onclick="fitModel()">Fit Model</button>
                            <button class="btn btn-secondary" onclick="clearCalData()">Clear Data</button>
                        </div>
                    </div>
                    <div style="flex:1; min-width:300px;">
                        <div class="section-title">Calibration Status</div>
                        <div id="cdStatus" style="background:#0f0f23; border:1px solid #333; border-radius:8px; padding:15px; margin-bottom:15px;">
                            <span style="color:#888;">Idle.</span>
                        </div>
                        <div class="section-title">Pointing Model</div>
                        <div id="cdModel" style="background:#0f0f23; border:1px solid #333; border-radius:8px; padding:15px; margin-bottom:15px;">
                            <span style="color:#888;">No model fitted yet.</span>
                        </div>
                        <div style="display:flex; gap:10px; margin-bottom:15px;">
                            <button class="btn btn-success" id="cdApplyBtn" style="display:none" onclick="applyModel()">Apply to Telescope</button>
                        </div>
                        <div id="cdPlotContainer" style="text-align:center;">
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <div class="tab-content" id="tab-rf">
            <div class="card">
                <div class="section-title">Bandpass template</div>
                <div style="color:#888; font-size:13px; margin-bottom:12px; max-width:70ch;">
                    Everything from the feed to the spectrum shapes the band &mdash; the
                    SAWbird&rsquo;s filter, the amplifier after it, the AD9361&rsquo;s decimation
                    chain and the FPGA&rsquo;s down-converter. Measuring the whole product at
                    once means pointing at sky with no hydrogen in it &mdash; the Lockman
                    Hole, l=150 b=+53, peaks at only 1.3 K. The line itself is masked and
                    the fit bridges it, so &ldquo;empty&rdquo; is only assumed where it is true.
                    <br><br>
                    <strong>It measures wherever the dish is already pointing, and does not
                    slew.</strong> A template has to match the elevation and the hour of
                    whatever it will reduce &mdash; two taken 4.7 hours apart differed by 2%
                    &mdash; so moving to a fixed field would guarantee the wrong one. A
                    direction carrying more than 1.5&nbsp;K of hydrogen outside the masked
                    window is refused: that is emission a template would fit as instrument
                    response and then subtract from every observation.
                </div>
                <div id="rfBandpassStatus" class="mono" style="padding:10px 12px; background:#0f0f23; border:1px solid #333; border-radius:6px; color:#888; font-size:12px;">
                    Loading&hellip;
                </div>
                <div style="margin-top:12px;">
                    <button class="btn" onclick="rfRun('bandpass')">Measure bandpass</button>
                    <button class="btn" onclick="rfLoadBandpassPlot()">Refresh plot</button>
                    <span style="color:#666; font-size:12px; margin-left:10px;">
                        ~2 min, where the dish is pointing now
                    </span>
                </div>
                <div id="rfBandpassPlot" class="rf-wide" style="margin-top:14px; text-align:center; color:#666; font-size:12px;">
                    Loading the before-and-after&hellip;
                </div>
            </div>

            <div class="card">
                <div class="section-title">Gain and system temperature</div>
                <div style="color:#888; font-size:13px; margin-bottom:12px; max-width:70ch;">
                    With the band flat, counts still are not kelvin. The simulator gives
                    the antenna temperature any direction should show through this beam,
                    so a straight line through counts against kelvin gives the gain as its
                    slope and the system temperature from its intercept. The pointing is
                    chosen <strong>now</strong>, not named: the gain drifts, and a fixed source is
                    below the horizon for most of the day. It searches the sky rather than
                    the plane alone &mdash; hydrogen does not stop at b&nbsp;=&nbsp;0, and when
                    the plane has set there is usually still a 70 K peak a few degrees off
                    it, higher in the sky than the plane ever was.
                </div>
                <div style="color:#888; font-size:12px; margin-bottom:6px;">
                    Suggestions for right now, brightest first and spread at least 25&deg;
                    apart. <strong>Check the direction against the skyline</strong> &mdash; the
                    software knows the eastern treeline and nothing else, so it cannot
                    see the dome towers.
                </div>
                <div id="rfTargets" style="margin-bottom:12px; overflow-x:auto;">
                    Finding candidates&hellip;
                </div>
                <div class="form-grid" style="max-width:520px;">
                    <div class="form-group">
                        <label>Galactic longitude <span class="unit">l (&deg;)</span></label>
                        <input autocomplete="off" type="number" id="rfGlon" step="0.1" value="">
                    </div>
                    <div class="form-group">
                        <label>Galactic latitude <span class="unit">b (&deg;)</span></label>
                        <input autocomplete="off" type="number" id="rfGlat" step="0.1" value="">
                    </div>
                </div>
                <div id="rfChosen" class="mono" style="padding:8px 12px; margin:8px 0 10px; background:#0f0f23; border:1px solid #333; border-radius:6px; color:#888; font-size:12px;">
                    Pick a suggestion, or type a direction. Left blank, one is chosen
                    automatically &mdash; which is what walked into a tower.
                </div>
                <div id="rfGainStatus" class="mono" style="padding:10px 12px; background:#0f0f23; border:1px solid #333; border-radius:6px; color:#888; font-size:12px;">
                    Loading&hellip;
                </div>
                <div style="margin-top:12px;">
                    <button class="btn" onclick="rfRun('gain')">Calibrate gain now</button>
                    <button class="btn" onclick="rfLoadGainPlot()">Refresh plot</button>
                    <button class="btn btn-danger" onclick="rfCancel()">Stop</button>
                    <span style="color:#666; font-size:12px; margin-left:10px;">
                        needs a bandpass template first
                    </span>
                </div>
                <div id="rfGainPlot" class="rf-wide" style="margin-top:14px; text-align:center; color:#666; font-size:12px;">
                    No gain calibration yet.
                </div>
            </div>

            <div class="card">
                <div class="section-title">Progress</div>
                <div id="rfProgress" style="color:#888; font-size:13px;">Idle.</div>
            </div>
        </div>

        <div class="tab-content" id="tab-horizon">
            <div style="display:flex; gap:20px; flex-wrap:wrap;">
                <div class="config-form" style="flex:0 0 320px;">
                    <div class="section-title">Horizon Scan</div>
                    <p style="color:#888; font-size:12px; margin-bottom:12px;">
                        Maps the skyline by radiometry: trees, roofs and the dome
                        tower are all near 290&nbsp;K at 1420&nbsp;MHz, so lowering
                        the beam through them is a large step in total power. Run it
                        at night, dry and calm &mdash; the Sun would swamp the sky
                        level and wet or moving foliage changes what is measured.
                    </p>
                    <div class="form-group">
                        <label>Azimuth Step (degrees)</label>
                        <input autocomplete="off" type="number" id="hzAzStep" min="1" max="30" step="1" value="5">
                    </div>
                    <div class="form-group">
                        <label>Altitude Step (degrees)</label>
                        <input autocomplete="off" type="number" id="hzAltStep" min="1" max="15" step="1" value="5">
                    </div>
                    <div class="form-group">
                        <label>Altitude Start (degrees)</label>
                        <input autocomplete="off" type="number" id="hzAltStart" min="1" max="30" step="1" value="5">
                    </div>
                    <div class="form-group">
                        <label>Altitude Ceiling (degrees)</label>
                        <input autocomplete="off" type="number" id="hzAltMax" min="10" max="85" step="5" value="60">
                    </div>
                    <div class="form-group">
                        <label>Settle after Slew <span class="unit">(s)</span></label>
                        <input autocomplete="off" type="number" id="hzSettle" min="0" max="10" step="0.5" value="2">
                    </div>
                    <div class="form-group">
                        <label>Integration per Point <span class="unit">(s)</span></label>
                        <input autocomplete="off" type="number" id="hzIntegration" min="0.1" max="10" step="0.1" value="2">
                    </div>
                    <div class="form-group">
                        <label>Re-home every N strips</label>
                        <input autocomplete="off" type="number" id="hzHomeEvery" min="0" max="10" step="1" value="2">
                    </div>
                    <div class="form-group">
                        <label>SDR</label>
                        <select autocomplete="off" id="hzSdrType">
                            <option value="b210">B210</option>
                            <option value="rtlsdr">RTL-SDR</option>
                            <option value="demo">Demo (no hardware)</option>
                        </select>
                    </div>
                    <div style="display:flex; gap:10px; margin-top:15px;">
                        <button class="btn btn-primary" id="hzStartBtn" onclick="startHorizon()">Start Scan</button>
                        <button class="btn btn-danger" id="hzStopBtn" style="display:none" onclick="stopHorizon()">Stop</button>
                    </div>
                    <div id="hzStatus" style="margin-top:12px; font-size:13px;">
                        <span style="color:#888;">Idle.</span>
                    </div>
                </div>
                <div style="flex:1; min-width:420px;">
                    <div class="section-title">Measured Profile</div>
                    <div id="hzProfile" style="background:#0f0f23; border:1px solid #333; border-radius:8px; padding:15px; margin-bottom:15px;">
                        <span style="color:#888;">No horizon profile measured yet.</span>
                    </div>
                    <div id="hzLandscape" style="display:none; margin-bottom:15px;">
                        <a class="btn btn-secondary" href="/api/horizon/landscape?use=clearance"
                           style="text-decoration:none;">Stellarium landscape (clean sky)</a>
                        <a class="btn btn-secondary" href="/api/horizon/landscape?use=edge"
                           style="text-decoration:none; margin-left:8px;">Stellarium landscape (skyline)</a>
                        <p style="color:#888; font-size:12px; margin-top:8px;">
                            A polygonal landscape built from the measurement itself.
                            Install in Stellarium with Sky and Viewing Options &rarr;
                            Landscape &rarr; Add/remove landscapes.
                        </p>
                    </div>
                    <div id="hzPlotContainer" style="text-align:center;"></div>
                </div>
            </div>
        </div>

        <div class="tab-content" id="tab-camera">
            <div class="config-form">
                <div class="section-title">Safety Camera</div>
                <div style="display:flex; gap:10px; align-items:center; margin-bottom:12px;">
                    <button class="btn btn-primary" id="camRefreshBtn" onclick="refreshCamera()">Refresh</button>
                    <label style="color:#888; font-size:12px;">
                        Auto-refresh
                        <select autocomplete="off" id="camAutoRefresh" onchange="onCameraAutoChange()" style="margin-left:6px;">
                            <option value="0" selected>Off</option>
                            <option value="1">Every 1 s</option>
                            <option value="5">Every 5 s</option>
                        </select>
                    </label>
                    <span id="camStatus" style="color:#888; font-size:12px;">Not captured yet.</span>
                </div>
                <div id="camView" style="text-align:center; background:#0f0f23; border:1px solid #333; border-radius:8px; padding:15px; min-height:120px;">
                    <span style="color:#888;">Press Refresh to capture a frame.</span>
                </div>
                <p style="color:#888; font-size:12px; margin-top:10px;">
                    One frame is captured per refresh, straight from the camera &mdash;
                    nothing is recorded or kept on disk, and the device is released
                    again immediately. Auto-refresh stops on its own whenever this tab
                    is not the one on screen, so a forgotten browser tab cannot leave
                    the camera running overnight.
                </p>
            </div>
        </div>

        <div class="tab-content" id="tab-simulator">
            <div id="simHost" style="height:calc(100vh - 190px); min-height:520px;
                                     border:1px solid #333; border-radius:8px;
                                     overflow:hidden; background:#fbfcfd;">
                <div style="color:#888; padding:15px;">Loading the simulator&hellip;</div>
            </div>
            <p style="color:#888; font-size:12px; margin-top:10px;">
                The sky simulator, served from this scheduler so the two are one
                origin. Its <strong>Realise</strong> button hands the simulated
                observation to the telescope &mdash; tracking the galactic
                coordinate on the H&nbsp;I map, or parking for a drift scan on the
                continuum map. It loads about 33&nbsp;MB of sky data the first time
                this tab is opened, and stays loaded afterwards.
            </p>
        </div>

        <div class="tab-content" id="tab-observe">
            <div class="config-form">
                <div class="section-title">Observation</div>
                <div id="obvSource" style="color:#888; font-size:12px; margin-bottom:12px;">
                    Set the fields here, or press <strong>Realise</strong> in the
                    Simulator tab to copy them from what you are simulating.
                </div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Type</label>
                        <select autocomplete="off" id="obvMode" onchange="onObserveModeChange()">
                            <option value="spectrum">Spectrum (tracked)</option>
                            <option value="drift">Drift scan</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Galactic l <span class="unit">(deg)</span></label>
                        <input autocomplete="off" type="number" id="obvL" step="any" value="132">
                    </div>
                    <div class="form-group">
                        <label>Galactic b <span class="unit">(deg)</span></label>
                        <input autocomplete="off" type="number" id="obvB" step="any" value="-1">
                    </div>
                    <div class="form-group">
                        <label id="obvDurationLabel">Total integration time <span class="unit">(min)</span></label>
                        <input autocomplete="off" type="number" id="obvDuration" min="1" max="1435" step="1" value="30">
                    </div>
                </div>
                <div class="section-title">Receiver Settings</div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Center Frequency <span class="unit">(MHz)</span></label>
                        <input autocomplete="off" type="number" id="obvCenterFreq" oninput="scheduleObserveTuning()" step="any" value="1420.405752">
                    </div>
                    <div class="form-group">
                        <label>Bandwidth <span class="unit">(MHz)</span></label>
                        <input autocomplete="off" type="number" id="obvBandwidth" oninput="scheduleObserveTuning()" step="0.1" value="2.4">
                    </div>
                    <div class="form-group">
                        <label>Gain <span class="unit">(dB)</span></label>
                        <input autocomplete="off" type="number" id="obvGain" min="0" max="80" value="40">
                    </div>
                    <div class="form-group">
                        <label>Channels</label>
                        <input autocomplete="off" type="number" id="obvChannels" oninput="scheduleObserveTuning()" min="2" max="65536" step="1" value="4096">
                    </div>
                    <div class="form-group">
                        <label>Integration per record <span class="unit">(s)</span></label>
                        <input autocomplete="off" type="number" id="obvIntegration" min="0.1" step="0.1" value="3.0">
                    </div>
                    <div class="form-group">
                        <label>SDR</label>
                        <select autocomplete="off" id="obvSdr">
                            <option value="b210">B210</option>
                            <option value="rtlsdr">RTL-SDR</option>
                        </select>
                    </div>
                </div>
                <div id="obvTuning" style="margin-top:10px; padding:10px 12px; background:#0f0f23; border:1px solid #333; border-radius:6px; color:#888; font-size:12px;">
                    Working out the tuning&hellip;
                </div>

                <div class="section-title">Output</div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Name</label>
                        <input autocomplete="off" type="text" id="obvName" value="Simulator target">
                    </div>
                    <div class="form-group">
                        <label>Filename <span class="unit">(blank = automatic)</span></label>
                        <input autocomplete="off" type="text" id="obvFilename" placeholder="h1_....h5">
                    </div>
                    <div class="form-group">
                        <label>When Finished</label>
                        <select autocomplete="off" id="obvEndAction">
                            <option value="stow">Stow</option>
                            <option value="home">Home</option>
                            <option value="none">Leave pointing</option>
                        </select>
                    </div>
                </div>
                <div style="display:flex; gap:10px; align-items:center; margin-top:15px; flex-wrap:wrap;">
                    <button class="btn btn-success" id="obvStartBtn" onclick="observeStartNow()">Start Now</button>
                    <button class="btn btn-secondary" onclick="observeToScheduler()">Send to Scheduler&hellip;</button>
                    <button class="btn btn-secondary" onclick="loadObserveParams(true)">Copy from Simulator</button>
                    <span id="obvStatus" style="color:#888; font-size:12px;"></span>
                </div>
                <div class="section-title" style="margin-top:20px;">Last Observation</div>
                <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                    <button class="btn btn-secondary" onclick="showObservePlot()">Plot Result</button>
                    <span id="obvLastInfo" style="color:#888; font-size:12px;">
                        Nothing has finished yet this session.
                    </span>
                </div>
                <div id="obvPlot" class="rf-wide" style="margin-top:12px;"></div>
                <p style="color:#888; font-size:12px; margin-top:12px;">
                    <strong>Start Now</strong> points the telescope and starts the
                    receiver immediately; it is not owned by a schedule slot, so a
                    scheduled observation will preempt it.
                    <strong>Send to Scheduler</strong> opens the Add Observation form
                    with these values filled in, to be booked for a time.
                    A drift scan parks the dish and leaves tracking off &mdash; the
                    source crosses the beam centre half a duration from the start.
                </p>
            </div>
        </div>

        <div class="tab-content" id="tab-config">
            <div class="config-form">
                <div class="section-title">Appearance</div>
                <div class="form-group">
                    <label>Banner Name</label>
                    <input autocomplete="off" type="text" id="cfgBannerName" placeholder="H1 Receiver Scheduler">
                </div>
                <div class="form-group">
                    <label>Banner Subtitle</label>
                    <input autocomplete="off" type="text" id="cfgBannerSubtitle" placeholder="Hydrogen Line (21cm) Observation Manager">
                </div>

                <div class="section-title">SRT Telescope Controller</div>
                <div class="form-group">
                    <label>Controller URL (leave empty to disable)</label>
                    <input autocomplete="off" type="text" id="cfgControllerUrl" placeholder="http://192.168.50.120">
                </div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Slew Timeout (seconds)</label>
                        <input autocomplete="off" type="number" id="cfgSlewTimeout" min="10" max="600">
                    </div>
                    <div class="form-group">
                        <label>Position Tolerance (degrees)</label>
                        <input autocomplete="off" type="number" id="cfgPositionTolerance" step="0.1" min="0.1" max="5">
                    </div>
                </div>

                <div class="section-title">Observer Location</div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Latitude (degrees, +N)</label>
                        <input autocomplete="off" type="number" id="cfgObsLat" step="0.001" min="-90" max="90">
                    </div>
                    <div class="form-group">
                        <label>Longitude (degrees, +E)</label>
                        <input autocomplete="off" type="number" id="cfgObsLon" step="0.001" min="-180" max="180">
                    </div>
                    <div class="form-group">
                        <label>Elevation (metres)</label>
                        <input autocomplete="off" type="number" id="cfgObsElev" step="1" min="0" max="9000">
                    </div>
                    <div class="form-group">
                        <label>Min Elevation for passes (degrees)</label>
                        <input autocomplete="off" type="number" id="cfgMinElev" step="1" min="0" max="90">
                    </div>
                </div>

                <div class="section-title">Obstructed Horizon</div>
                <div class="form-group">
                    <label>Sectors (az_min-az_max:min_sun_alt, comma separated)</label>
                    <input autocomplete="off" type="text" id="cfgObstructionSectors" placeholder="45-120:30">
                    <p style="color:#888; font-size:12px; margin-top:6px;">
                        While the Sun is inside one of these azimuth ranges and below
                        the stated altitude it is not scanned, and scans already on
                        file from there are left out of the pointing fit. Trees emit
                        at 1420&nbsp;MHz, so a raster that catches the skyline drags
                        the fitted beam centre down into it.
                    </p>
                </div>

                <div class="section-title">Safety Camera</div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Video Device</label>
                        <input autocomplete="off" type="text" id="cfgCameraDevice" placeholder="/dev/video0">
                    </div>
                    <div class="form-group">
                        <label>Capture Resolution</label>
                        <input autocomplete="off" type="text" id="cfgCameraResolution" placeholder="640x480">
                    </div>
                </div>

                <div class="section-title">Receiver</div>
                <div class="form-group">
                    <label>Receiver Python Executable</label>
                    <input autocomplete="off" type="text" id="cfgReceiverPythonPath" placeholder="/home/astro/radioconda/bin/python">
                </div>

                <div class="section-title">Data Output</div>
                <div class="form-group">
                    <label>Output Folder</label>
                    <input autocomplete="off" type="text" id="cfgDataFolder" placeholder="Path to store observation data files">
                </div>

                <div class="section-title">Log</div>
                <div class="form-group">
                    <label>Log Lines to Display</label>
                    <input autocomplete="off" type="number" id="cfgLogLines" min="20" max="1000" step="10">
                </div>

                <div class="section-title">Notifications</div>
                <div class="form-group">
                    <label>Sound on Start/Stop</label>
                    <select autocomplete="off" id="cfgSoundEnabled">
                        <option value="true">Enabled</option>
                        <option value="false">Disabled</option>
                    </select>
                </div>

                <div style="margin-top:20px; display:flex; align-items:center;">
                    <button class="btn btn-primary" type="button" onclick="saveConfig()">Save Configuration</button>
                    <span class="config-saved" id="configSaved">Saved!</span>
                </div>
            </div>
        </div>

        <div class="tab-content" id="tab-log">
            <div class="log-controls">
                <button class="btn btn-secondary" onclick="loadLog()">Refresh</button>
                <label style="color:#888; font-size:12px;">
                    <input autocomplete="off" type="checkbox" id="logAutoRefresh" onchange="toggleLogRefresh()" checked> Auto-refresh (5s)
                </label>
            </div>
            <div class="log-container" id="logContent">Loading log...</div>
        </div>
    </div>

    <div class="modal" id="obsModal">
        <div class="modal-content">
            <div class="modal-header">
                <h2 class="modal-title" id="modalTitle">Add Observation</h2>
                <button class="close-btn" onclick="closeModal()">&times;</button>
            </div>
            <form id="obsForm">
                <input autocomplete="off" type="hidden" id="obsIndex" value="-1">

                <div class="form-grid">
                    <div class="form-group wide">
                        <label>Observation Name</label>
                        <input autocomplete="off" type="text" id="obsName" required placeholder="e.g., Galactic Center Survey">
                    </div>
                </div>

                <div class="section-title">Schedule <span style="font-weight:normal; font-size:11px; color:#888;">(Local Time)</span></div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Start Date</label>
                        <input autocomplete="off" type="date" id="obsStartDate" onchange="onCoordChange()">
                    </div>
                    <div class="form-group">
                        <label>Start Time</label>
                        <input autocomplete="off" type="time" id="obsStartTime" required onchange="updateEndTime()">
                    </div>
                    <div class="form-group">
                        <label>Duration (minutes)</label>
                        <input autocomplete="off" type="number" id="obsDuration" min="1" max="1440" required onchange="updateEndTime()">
                    </div>
                    <div class="form-group">
                        <label>End Time</label>
                        <input autocomplete="off" type="time" id="obsEndTime" disabled style="background:#2a2a4a; color:#aaa;">
                    </div>
                </div>
                <div id="clashWarning" style="display:none; color:#ff4757; background:#3a1a1a; padding:8px 12px; border-radius:5px; margin-top:5px; font-size:13px;"></div>

                <div class="section-title">Target Coordinates</div>
                <div class="coord-section">
                    <div class="form-grid">
                        <div class="form-group">
                            <label>Coordinate System</label>
                            <select autocomplete="off" id="obsCoordSystem" onchange="updateCoordLabels()">
                                <option value="altaz">Alt/Az (Horizontal)</option>
                                <option value="radec">RA/Dec (Equatorial J2000)</option>
                                <option value="galactic">Galactic (l, b)</option>
                                <option value="drift">Drift Scan (fixed pointing)</option>
                                <option value="object">Solar System Object</option>
                                <option value="satellite">Satellite (TLE)</option>
                                <option value="calibration">Calibration Day (Sun Scan)</option>
                                <option value="horizon">Horizon Scan</option>
                            </select>
                        </div>
                    </div>
                    <div class="form-grid" id="objectSelector" style="margin-top:15px; display:none">
                        <div class="form-group">
                            <label>Object</label>
                            <select autocomplete="off" id="obsObjectName">
                                <option value="sun">Sun</option>
                                <option value="moon">Moon</option>
                            </select>
                        </div>
                    </div>
                    <div id="satelliteInput" style="margin-top:15px; display:none">
                        <div style="display:flex; gap:10px; align-items:flex-end; margin-bottom:10px;">
                            <div class="form-group" style="flex:1; margin:0;">
                                <label>Search CelesTrak by name or NORAD ID</label>
                                <input autocomplete="off" type="text" id="tleSearch" placeholder="e.g. ISS, NOAA 19, 25544" style="width:100%;">
                            </div>
                            <button class="btn btn-primary" type="button" onclick="fetchTle()" style="white-space:nowrap;">Fetch TLE</button>
                        </div>
                        <div id="tleResults" style="display:none; margin-bottom:10px;">
                            <div class="form-group">
                                <label>Select satellite</label>
                                <select autocomplete="off" id="tleResultSelect" onchange="selectTleResult()" style="width:100%;"></select>
                            </div>
                        </div>
                        <div class="form-group">
                            <label>TLE (paste, search above, or load from file)</label>
                            <textarea autocomplete="off" id="obsTleText" rows="4" style="width:100%; padding:8px; border:1px solid #333; border-radius:5px; background:#0f0f23; color:#fff; font-family:monospace; font-size:12px; resize:vertical;" placeholder="ISS (ZARYA)
1 25544U 98067A   ...
2 25544  51.6400  ..."></textarea>
                        </div>
                        <div style="display:flex; gap:10px; margin-top:8px; align-items:center; flex-wrap:wrap;">
                            <button class="btn btn-primary" type="button" onclick="predictPass()">Compute Next Pass</button>
                            <label class="btn btn-secondary" style="margin:0; cursor:pointer;">
                                Load TLE File <input autocomplete="off" type="file" accept=".tle,.txt" style="display:none" onchange="loadTleFile(event)">
                            </label>
                            <span id="passInfo" style="color:#888; font-size:12px;"></span>
                        </div>
                        <div id="passDetails" style="display:none; margin-top:10px; padding:10px; background:#0f0f23; border-radius:5px; font-size:12px; color:#ccc;">
                        </div>
                    </div>
                    <div id="calibrationInput" style="margin-top:15px; display:none">
                        <p style="color:#888; font-size:12px; margin-bottom:10px;">
                            Runs repeated sun scans for the observation duration to determine
                            effective observer lat/lon and pointing offsets.
                        </p>
                        <div class="form-grid">
                            <div class="form-group">
                                <label>Grid Size (n x n)</label>
                                <input autocomplete="off" type="number" id="obsCalGridN" min="3" max="15" step="2" value="5">
                            </div>
                            <div class="form-group">
                                <label>Grid Spacing (degrees)</label>
                                <input autocomplete="off" type="number" id="obsCalSpacing" min="0.1" max="10" step="0.1" value="1.5">
                            </div>
                            <div class="form-group">
                                <label>Scan Interval (minutes)</label>
                                <input autocomplete="off" type="number" id="obsCalInterval" min="5" max="120" value="30">
                            </div>
                        </div>
                    </div>
                    <div id="horizonInput" style="margin-top:15px; display:none">
                        <p style="color:#888; font-size:12px; margin-bottom:10px;">
                            Maps the obstructed horizon by radiometry, one altitude cut
                            per azimuth. Schedule it for a dark, dry, calm night: the Sun
                            swamps the sky level, and wet or wind-blown foliage is not the
                            horizon you want recorded. Allow about two hours.
                        </p>
                        <div class="form-grid">
                            <div class="form-group">
                                <label>Azimuth Step (degrees)</label>
                                <input autocomplete="off" type="number" id="obsHorizonAzStep" min="1" max="30" step="1" value="5">
                            </div>
                            <div class="form-group">
                                <label>Altitude Step (degrees)</label>
                                <input autocomplete="off" type="number" id="obsHorizonAltStep" min="1" max="15" step="1" value="5">
                            </div>
                            <div class="form-group">
                                <label>Azimuth Start (degrees)</label>
                                <input autocomplete="off" type="number" id="obsHorizonAzStart" min="0" max="360" step="1" value="5">
                            </div>
                            <div class="form-group">
                                <label>Azimuth End (degrees)</label>
                                <input autocomplete="off" type="number" id="obsHorizonAzEnd" min="0" max="360" step="1" value="350">
                            </div>
                        </div>
                    </div>
                    <div id="driftInput" style="margin-top:15px; display:none">
                        <p style="color:#888; font-size:12px; margin-bottom:10px;">
                            The dish parks where the source will be at the beam-crossing
                            time and stays fixed while the sky drifts through the beam.
                            Recording runs from T&minus;window to T+window; start time and
                            duration are derived automatically.
                        </p>
                        <div class="form-grid">
                            <div class="form-group">
                                <label>Source Frame</label>
                                <select autocomplete="off" id="obsDriftFrame" onchange="updateCoordLabels()">
                                    <option value="radec">RA/Dec (J2000)</option>
                                    <option value="galactic">Galactic (l, b)</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Beam-Crossing Time T (local)</label>
                                <input autocomplete="off" type="time" id="obsDriftTime" onchange="updateDriftDerived()">
                            </div>
                            <div class="form-group">
                                <label>Window &plusmn; (minutes)</label>
                                <input autocomplete="off" type="number" id="obsDriftWindow" min="1" max="720" value="30" onchange="updateDriftDerived()">
                            </div>
                        </div>
                        <div style="display:flex; gap:10px; margin-top:8px; align-items:center; flex-wrap:wrap;">
                            <button class="btn btn-primary" type="button" onclick="useNextTransit()">Use Next Transit</button>
                            <span id="driftPreview" style="color:#888; font-size:12px;"></span>
                        </div>
                    </div>
                    <div class="form-grid" id="coordInputs" style="margin-top:15px">
                        <div class="form-group">
                            <label id="coord1Label">Altitude</label>
                            <div class="coord-row">
                                <input autocomplete="off" type="number" id="coord1Deg" min="-90" max="360" value="45" onchange="onCoordChange()">
                                <span id="coord1Unit1">deg</span>
                                <input autocomplete="off" type="number" id="coord1Min" min="0" max="59" value="0" onchange="onCoordChange()">
                                <span>min</span>
                                <input autocomplete="off" type="number" id="coord1Sec" min="0" max="59.99" step="0.01" value="0" onchange="onCoordChange()">
                                <span>sec</span>
                            </div>
                        </div>
                        <div class="form-group">
                            <label id="coord2Label">Azimuth</label>
                            <div class="coord-row">
                                <input autocomplete="off" type="number" id="coord2Deg" min="-90" max="360" value="180" onchange="onCoordChange()">
                                <span>deg</span>
                                <input autocomplete="off" type="number" id="coord2Min" min="0" max="59" value="0" onchange="onCoordChange()">
                                <span>min</span>
                                <input autocomplete="off" type="number" id="coord2Sec" min="0" max="59.99" step="0.01" value="0" onchange="onCoordChange()">
                                <span>sec</span>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="section-title">Receiver Settings</div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Center Frequency <span class="unit">(MHz)</span></label>
                        <input autocomplete="off" type="number" id="obsCenterFreq" step="any" required value="1420.405752">
                    </div>
                    <div class="form-group">
                        <label>Bandwidth <span class="unit">(MHz)</span></label>
                        <input autocomplete="off" type="number" id="obsBandwidth" step="0.1" required value="2.4">
                    </div>
                    <div class="form-group">
                        <label>Gain <span class="unit">(dB)</span></label>
                        <input autocomplete="off" type="number" id="obsGain" min="0" max="80" required value="40">
                    </div>
                    <div class="form-group">
                        <label>Channels (FFT)</label>
                        <select autocomplete="off" id="obsChannels">
                            <option value="1024">1024</option>
                            <option value="2048">2048</option>
                            <option value="4096" selected>4096</option>
                            <option value="8192">8192</option>
                            <option value="16384">16384</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Integration Time <span class="unit">(s)</span></label>
                        <input autocomplete="off" type="number" id="obsIntegration" step="0.1" min="0.1" required value="3.0">
                    </div>
                    <div class="form-group">
                        <label>SDR Type</label>
                        <select autocomplete="off" id="obsSdrType">
                            <option value="b210">Ettus B210</option>
                            <option value="rtlsdr">RTL-SDR</option>
                            <option value="demo">Demo (Simulated)</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Calibrator (Noise Source)</label>
                        <select autocomplete="off" id="obsCalibrator">
                            <option value="off">Off</option>
                            <option value="on">On</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>When Done</label>
                        <select autocomplete="off" id="obsEndAction">
                            <option value="none">Stay</option>
                            <option value="home">Go Home (Alt 0, Az 0)</option>
                            <option value="stow">Stow (Alt 90, Az 180)</option>
                        </select>
                    </div>
                </div>

                <div class="section-title">Output</div>
                <div class="form-grid">
                    <div class="form-group wide">
                        <label>Filename (leave empty for auto)</label>
                        <input autocomplete="off" type="text" id="obsFilename" placeholder="auto-generated if empty">
                    </div>
                </div>

                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">Save Observation</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        let schedule = [];
        let currentObs = null;

        const COORD_CONFIG = {
            altaz: {
                c1: 'Altitude', c2: 'Azimuth', u1: 'deg',
                c1_min: 0, c1_max: 90,      // Alt: 0 to 90 (above horizon)
                c2_min: 0, c2_max: 359      // Az: 0 to 359
            },
            radec: {
                c1: 'Right Ascension', c2: 'Declination', u1: 'h',
                c1_min: 0, c1_max: 23,      // RA: 0h to 23h (+ min/sec)
                c2_min: -90, c2_max: 90     // Dec: -90 to +90
            },
            galactic: {
                c1: 'Galactic Longitude (l)', c2: 'Galactic Latitude (b)', u1: 'deg',
                c1_min: 0, c1_max: 359,     // l: 0 to 359
                c2_min: -90, c2_max: 90     // b: -90 to +90
            }
        };

        const DEFAULTS = {
            name: "New Observation",
            coord_system: "altaz",
            coord1_deg: 45, coord1_min: 0, coord1_sec: 0,
            coord2_deg: 180, coord2_min: 0, coord2_sec: 0,
            start_date: "", start_time: "12:00",
            duration_minutes: 30,
            center_freq_mhz: 1420.405752,
            bandwidth_mhz: 2.4,
            gain_db: 40,
            channels: 4096,
            integration_time_s: 3.0,
            filename: "",
            sdr_type: "b210",
            calibrator: false,
            end_action: "none",
            enabled: true,
            drift_frame: "radec",
            drift_time: "12:00",
            drift_window_min: 30
        };

        function updateClock() {
            const now = new Date();
            const date = now.toLocaleDateString('en-US', {weekday:'long', year:'numeric', month:'long', day:'numeric'});
            const localTime = now.toLocaleTimeString('en-US', {hour12:false, hour:'2-digit', minute:'2-digit', second:'2-digit'});
            const utcTime = now.toISOString().substring(11, 19);
            document.getElementById('currentDate').textContent = date;
            document.getElementById('currentTime').textContent = localTime;
            document.getElementById('utcTime').textContent = utcTime;
        }

        document.addEventListener('DOMContentLoaded', () => {
            updateClock();
            setInterval(updateClock, 1000);
            loadSchedule();
            updateStatus();
            updateTelescope();
            updateReceiver();
            setInterval(updateStatus, 2000);
            setInterval(updateTelescope, 5000);
            setInterval(updateReceiver, 3000);
            fetch('/api/config').then(r => r.json()).then(cfg => {
                soundEnabled = cfg.sound_enabled !== false;
            });
        });

        document.getElementById('obsForm').addEventListener('submit', e => {
            e.preventDefault();
            saveObservation();
        });

        function updateEndTime() {
            const date = document.getElementById('obsStartDate').value || localDateStr(new Date());
            const time = document.getElementById('obsStartTime').value;
            const dur = parseInt(document.getElementById('obsDuration').value) || 0;
            if (!time) return;
            const start = new Date(`${date}T${time}`);
            const end = new Date(start.getTime() + dur * 60000);
            const hh = String(end.getHours()).padStart(2,'0');
            const mm = String(end.getMinutes()).padStart(2,'0');
            document.getElementById('obsEndTime').value = `${hh}:${mm}`;
            checkClash();
        }

        function getObsInterval(obs) {
            const date = obs.start_date || localDateStr(new Date());
            const start = new Date(`${date}T${obs.start_time}`);
            const end = new Date(start.getTime() + (obs.duration_minutes || 0) * 60000);
            return {start, end};
        }

        function checkClash() {
            const editIdx = parseInt(document.getElementById('obsIndex').value);
            const date = document.getElementById('obsStartDate').value || localDateStr(new Date());
            const time = document.getElementById('obsStartTime').value;
            const dur = parseInt(document.getElementById('obsDuration').value) || 0;
            if (!time) return;
            const newStart = new Date(`${date}T${time}`);
            const newEnd = new Date(newStart.getTime() + dur * 60000);
            const warn = document.getElementById('clashWarning');
            const clashes = [];
            schedule.forEach((obs, i) => {
                if (i === editIdx || !obs.enabled || !obs.start_time) return;
                const {start, end} = getObsInterval(obs);
                if (newStart < end && newEnd > start) {
                    clashes.push(obs.name);
                }
            });
            if (clashes.length > 0) {
                warn.textContent = 'Clashes with: ' + clashes.join(', ');
                warn.style.display = 'block';
            } else {
                warn.style.display = 'none';
            }
            return clashes.length > 0;
        }

        function updateCoordLabels() {
            const sys = document.getElementById('obsCoordSystem').value;
            const isObject = sys === 'object';
            const isSat = sys === 'satellite';
            const isCal = sys === 'calibration';
            const isDrift = sys === 'drift';
            const isHorizon = sys === 'horizon';
            document.getElementById('objectSelector').style.display = isObject ? '' : 'none';
            document.getElementById('satelliteInput').style.display = isSat ? '' : 'none';
            document.getElementById('calibrationInput').style.display = isCal ? '' : 'none';
            document.getElementById('horizonInput').style.display = isHorizon ? '' : 'none';
            document.getElementById('driftInput').style.display = isDrift ? '' : 'none';
            // A horizon scan has no target: it goes to every azimuth in turn.
            document.getElementById('coordInputs').style.display =
                (isObject || isSat || isCal || isHorizon) ? 'none' : '';
            // Drift scans derive start time and duration from T and the window
            document.getElementById('obsStartTime').disabled = isDrift;
            document.getElementById('obsDuration').disabled = isDrift;
            if (isObject || isSat || isCal || isHorizon) return;
            if (isDrift) updateDriftDerived();
            const cfg = COORD_CONFIG[isDrift ? document.getElementById('obsDriftFrame').value : sys];
            document.getElementById('coord1Label').textContent = cfg.c1;
            document.getElementById('coord2Label').textContent = cfg.c2;
            document.getElementById('coord1Unit1').textContent = cfg.u1;
            // Set min/max limits
            const c1 = document.getElementById('coord1Deg');
            const c2 = document.getElementById('coord2Deg');
            c1.min = cfg.c1_min; c1.max = cfg.c1_max;
            c2.min = cfg.c2_min; c2.max = cfg.c2_max;
            // Clamp current values to valid range
            c1.value = Math.max(cfg.c1_min, Math.min(cfg.c1_max, c1.value));
            c2.value = Math.max(cfg.c2_min, Math.min(cfg.c2_max, c2.value));
        }

        function onCoordChange() {
            if (document.getElementById('obsCoordSystem').value === 'drift') {
                updateDriftDerived();
            }
        }

        function dmsToDecimalJs(deg, min, sec) {
            const d = parseInt(deg) || 0, m = parseInt(min) || 0, s = parseFloat(sec) || 0;
            const sign = d < 0 ? -1 : 1;
            return sign * (Math.abs(d) + m / 60 + s / 3600);
        }

        function localDateStr(d) {
            return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
        }

        function driftBeamDate() {
            // Date of the beam-crossing time T: the entry's date field (or today)
            const date = document.getElementById('obsStartDate').value || localDateStr(new Date());
            const t = document.getElementById('obsDriftTime').value;
            return t ? new Date(`${date}T${t}`) : null;
        }

        function updateDriftDerived() {
            const Tdt = driftBeamDate();
            if (!Tdt) return;
            const w = parseInt(document.getElementById('obsDriftWindow').value) || 30;
            const startDt = new Date(Tdt.getTime() - w * 60000);
            document.getElementById('obsStartTime').value =
                String(startDt.getHours()).padStart(2,'0') + ':' + String(startDt.getMinutes()).padStart(2,'0');
            document.getElementById('obsDuration').value = 2 * w;
            updateEndTime();
            fetchDriftPreview();
        }

        let driftNextTransit = null;
        function fetchDriftPreview() {
            const Tdt = driftBeamDate();
            if (!Tdt) return;
            const frame = document.getElementById('obsDriftFrame').value;
            const c1 = dmsToDecimalJs(document.getElementById('coord1Deg').value,
                                      document.getElementById('coord1Min').value,
                                      document.getElementById('coord1Sec').value);
            const c2 = dmsToDecimalJs(document.getElementById('coord2Deg').value,
                                      document.getElementById('coord2Min').value,
                                      document.getElementById('coord2Sec').value);
            const params = new URLSearchParams({
                frame: frame, coord1: c1, coord2: c2,
                date: localDateStr(Tdt),
                time: document.getElementById('obsDriftTime').value
            });
            fetch('/api/drift_preview?' + params).then(r => r.json()).then(data => {
                const el = document.getElementById('driftPreview');
                if (!data.success) {
                    el.textContent = 'Preview unavailable: ' + (data.error || 'unknown error');
                    el.style.color = '#ff4757';
                    driftNextTransit = null;
                    return;
                }
                driftNextTransit = {date: data.next_transit_date, time: data.next_transit_time};
                let text = `At T: Alt ${data.alt.toFixed(1)}°, Az ${data.az.toFixed(1)}°`;
                if (data.warnings.length) text += ' — ' + data.warnings.join('; ');
                text += ` | next transit ${data.next_transit_date} ${data.next_transit_time}`;
                el.textContent = text;
                el.style.color = data.reachable ? (data.warnings.length ? '#ffa502' : '#2ed573') : '#ff4757';
            }).catch(() => {
                document.getElementById('driftPreview').textContent = 'Preview unavailable';
                driftNextTransit = null;
            });
        }

        function useNextTransit() {
            if (!driftNextTransit) { fetchDriftPreview(); return; }
            document.getElementById('obsStartDate').value = driftNextTransit.date;
            document.getElementById('obsDriftTime').value = driftNextTransit.time;
            updateDriftDerived();
        }

        function formatCoord(deg, min, sec, isRA) {
            const d = parseInt(deg) || 0;
            const m = parseInt(min) || 0;
            const s = parseFloat(sec) || 0;
            if (isRA) {
                return `${d}h ${m}m ${s.toFixed(1)}s`;
            }
            const sign = d < 0 ? '-' : '+';
            return `${sign}${Math.abs(d)}° ${m}' ${s.toFixed(1)}"`;
        }

        function formatCoordDisplay(obs) {
            const sys = obs.coord_system || 'altaz';
            if (sys === 'object') {
                const name = obs.object_name || 'unknown';
                return `Object: ${name.charAt(0).toUpperCase() + name.slice(1)}`;
            }
            if (sys === 'satellite') {
                const tle = obs.tle_text || '';
                const name = tle.split('\\n')[0] || 'Satellite';
                return `Sat: ${name.substring(0, 20)}`;
            }
            if (sys === 'calibration') {
                const n = obs.cal_grid_n || 5;
                const interval = obs.cal_interval_min || 30;
                return `Cal: ${n}x${n} every ${interval}min`;
            }
            if (sys === 'drift') {
                const isRA = (obs.drift_frame || 'radec') === 'radec';
                const c1 = formatCoord(obs.coord1_deg, obs.coord1_min, obs.coord1_sec, isRA);
                const c2 = formatCoord(obs.coord2_deg, obs.coord2_min, obs.coord2_sec, false);
                return `Drift ${isRA ? 'RA/Dec' : 'Gal'}: ${c1}, ${c2} @ ${obs.drift_time} ±${obs.drift_window_min}min`;
            }
            const isRA = sys === 'radec';
            const c1 = formatCoord(obs.coord1_deg, obs.coord1_min, obs.coord1_sec, isRA);
            const c2 = formatCoord(obs.coord2_deg, obs.coord2_min, obs.coord2_sec, false);
            const labels = {altaz: 'Alt/Az', radec: 'RA/Dec', galactic: 'Gal'};
            return `${labels[sys]}: ${c1}, ${c2}`;
        }

        function loadSchedule() {
            fetch('/api/schedule').then(r => r.json()).then(data => {
                schedule = data;
                renderSchedule();
            });
        }

        function saveSchedule() {
            // The server rejects clashing schedules with a 400 and a reason.
            // Announcing success regardless meant edits silently vanished on
            // the next reload, with nothing on screen to explain it.
            postSchedule().then(r => {
                if (r.ok) { alert('Schedule saved!'); }
                else { alert('Schedule NOT saved: ' + r.error); }
            });
        }

        // Single place that POSTs the schedule and reports what the server said.
        function postSchedule() {
            return fetch('/api/schedule', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(schedule)
            }).then(resp => resp.json().catch(() => ({}))
                .then(d => ({ok: resp.ok && d.success !== false,
                             error: d.error || ('HTTP ' + resp.status)})))
              .catch(e => ({ok: false, error: String(e)}));
        }

        function formatEndTime(obs) {
            // For the currently running observation, show the actual end time from the server
            if (currentObs && currentObs.name === obs.name && currentObs.ends_at) {
                const end = new Date(currentObs.ends_at);
                const hh = String(end.getHours()).padStart(2,'0');
                const mm = String(end.getMinutes()).padStart(2,'0');
                return hh + ':' + mm + ' (live)';
            }
            if (obs.end_time) {
                return (obs.end_date !== obs.start_date ? obs.end_date + ' ' : '') + obs.end_time;
            }
            return obs.duration_minutes + ' min';
        }

        // End of an entry's one and only slot, or null if it does not have one.
        //
        // This mirrors scheduler_thread() rather than reading the End column,
        // so what the list greys out is exactly what the background thread
        // would refuse to start. Two details have to be copied:
        //
        //   - the window is start_time + duration_minutes. end_date/end_time
        //     are display fields; the scheduler never reads them, and nothing
        //     keeps the two in step if someone edits one.
        //   - a dateless entry is not a past entry. The scheduler fills in
        //     today's date on every pass, so it comes round again every day
        //     and must never be greyed out. (find_clashes() makes the same
        //     distinction, for the same reason.)
        function obsSlotEnd(obs) {
            if (!obs.start_date || !obs.start_time) return null;
            const start = new Date(`${obs.start_date}T${obs.start_time}`);
            if (isNaN(start)) return null;
            return new Date(start.getTime() + (obs.duration_minutes || 0) * 60000);
        }

        function isExpired(obs) {
            const end = obsSlotEnd(obs);
            // The 60 s is scheduler_thread()'s own cutoff: it needs more than a
            // minute left in the window before it will take a slot, so the last
            // minute is already dead time and is shown as such.
            return end !== null && end.getTime() - Date.now() <= 60000;
        }

        // Why this entry can never run, or null if it still can. Expiry is the
        // ordinary case. The other two are only reachable through a hand-edited
        // or imported schedule.json - the Add form requires a start time - and
        // there they are invisible faults: scheduler_thread() skips the entry
        // outright while it sits in the list looking perfectly normal. Named
        // rather than lumped in with "Expired", because "its time has passed"
        // and "this entry is malformed" want different fixes.
        function neverRunsReason(obs) {
            if (!obs.start_time) return 'No start time';
            if (obs.start_date && obsSlotEnd(obs) === null) return 'Bad date';
            return isExpired(obs) ? 'Expired' : null;
        }

        function renderSchedule() {
            const list = document.getElementById('scheduleList');
            if (schedule.length === 0) {
                list.innerHTML = '<div class="empty-state">No observations scheduled.</div>';
                return;
            }
            list.innerHTML = schedule.map((obs, i) => {
              const dead = neverRunsReason(obs);
              return `
                <div class="schedule-item ${obs.enabled ? '' : 'disabled'} ${dead ? 'wont-run' : ''} ${currentObs?.name === obs.name ? 'current-obs' : ''}">
                    <input autocomplete="off" type="checkbox" class="checkbox" ${obs.enabled ? 'checked' : ''} onchange="toggleEnabled(${i})">
                    <div class="schedule-info">
                        <div class="field"><div class="field-label">Name</div><div class="field-value">${obs.name}${dead ? '<span class="tag-wont-run">' + dead + '</span>' : ''}</div></div>
                        <div class="field"><div class="field-label">Start</div><div class="field-value">${obs.start_date || 'Today'} ${obs.start_time}</div></div>
                        <div class="field"><div class="field-label">End</div><div class="field-value">${formatEndTime(obs)}</div></div>
                        <div class="field"><div class="field-label">Coordinates</div><div class="field-value">${formatCoordDisplay(obs)}</div></div>
                        <div class="field"><div class="field-label">Frequency</div><div class="field-value">${obs.center_freq_mhz} MHz</div></div>
                        <div class="field"><div class="field-label">BW / Gain</div><div class="field-value">${obs.bandwidth_mhz} MHz / ${obs.gain_db} dB</div></div>
                        <div class="field"><div class="field-label">Cal / End</div><div class="field-value">${obs.calibrator ? 'CAL' : '-'} / ${({home:'Home',stow:'Stow'})[obs.end_action] || '-'}</div></div>
                        <div class="field"><div class="field-label">Channels</div><div class="field-value">${obs.channels}</div></div>
                        <div class="field"><div class="field-label">Integration</div><div class="field-value">${obs.integration_time_s}s</div></div>
                    </div>
                    <div class="schedule-actions">
                        <button class="btn btn-success btn-icon" onclick="runNow(${i})" title="Run Now">▶</button>
                        <button class="btn btn-secondary btn-icon" onclick="cloneObs(${i})" title="Clone">⧉</button>
                        <button class="btn btn-secondary btn-icon" onclick="editObs(${i})" title="Edit">✎</button>
                        <button class="btn btn-danger btn-icon" onclick="deleteObs(${i})" title="Delete">✕</button>
                    </div>
                </div>
              `;
            }).join('');
        }

        function openAddModal() {
            document.getElementById('modalTitle').textContent = 'Add Observation';
            document.getElementById('obsIndex').value = -1;
            fillForm(DEFAULTS);
            document.getElementById('obsModal').classList.add('active');
        }

        function isRunning(obs) {
            return currentObs && currentObs.name === obs.name;
        }

        function editObs(i) {
            if (isRunning(schedule[i])) {
                alert('Cannot edit while running. Stop the observation first.');
                return;
            }
            document.getElementById('modalTitle').textContent = 'Edit Observation';
            document.getElementById('obsIndex').value = i;
            fillForm(schedule[i]);
            document.getElementById('obsModal').classList.add('active');
        }

        function cloneObs(i) {
            document.getElementById('modalTitle').textContent = 'Clone Observation';
            document.getElementById('obsIndex').value = -1;
            const clone = Object.assign({}, schedule[i]);
            clone.name = clone.name + ' (copy)';
            clone.start_date = '';
            clone.start_time = '';
            clone.end_date = '';
            clone.end_time = '';
            clone.filename = '';
            fillForm(clone);
            document.getElementById('obsModal').classList.add('active');
        }

        function fillForm(obs) {
            document.getElementById('obsName').value = obs.name || DEFAULTS.name;
            document.getElementById('obsCoordSystem').value = obs.coord_system || DEFAULTS.coord_system;
            document.getElementById('obsObjectName').value = obs.object_name || 'sun';
            document.getElementById('obsTleText').value = obs.tle_text || '';
            document.getElementById('coord1Deg').value = obs.coord1_deg ?? DEFAULTS.coord1_deg;
            document.getElementById('coord1Min').value = obs.coord1_min ?? DEFAULTS.coord1_min;
            document.getElementById('coord1Sec').value = obs.coord1_sec ?? DEFAULTS.coord1_sec;
            document.getElementById('coord2Deg').value = obs.coord2_deg ?? DEFAULTS.coord2_deg;
            document.getElementById('coord2Min').value = obs.coord2_min ?? DEFAULTS.coord2_min;
            document.getElementById('coord2Sec').value = obs.coord2_sec ?? DEFAULTS.coord2_sec;
            document.getElementById('obsStartDate').value = obs.start_date || '';
            document.getElementById('obsStartTime').value = obs.start_time || DEFAULTS.start_time;
            document.getElementById('obsDuration').value = obs.duration_minutes ?? DEFAULTS.duration_minutes;
            document.getElementById('obsCenterFreq').value = obs.center_freq_mhz ?? DEFAULTS.center_freq_mhz;
            document.getElementById('obsBandwidth').value = obs.bandwidth_mhz ?? DEFAULTS.bandwidth_mhz;
            document.getElementById('obsGain').value = obs.gain_db ?? DEFAULTS.gain_db;
            document.getElementById('obsChannels').value = obs.channels || DEFAULTS.channels;
            document.getElementById('obsIntegration').value = obs.integration_time_s ?? DEFAULTS.integration_time_s;
            document.getElementById('obsSdrType').value = obs.sdr_type || DEFAULTS.sdr_type;
            document.getElementById('obsCalibrator').value = obs.calibrator ? 'on' : 'off';
            document.getElementById('obsEndAction').value = obs.end_action || 'none';
            document.getElementById('obsFilename').value = obs.filename || '';
            document.getElementById('obsCalGridN').value = obs.cal_grid_n || 5;
            document.getElementById('obsCalSpacing').value = obs.cal_spacing_deg || 1.5;
            document.getElementById('obsCalInterval').value = obs.cal_interval_min || 30;
            document.getElementById('obsHorizonAzStep').value = obs.horizon_az_step || 5;
            document.getElementById('obsHorizonAltStep').value = obs.horizon_alt_step || 5;
            document.getElementById('obsHorizonAzStart').value = obs.horizon_az_start ?? 5;
            document.getElementById('obsHorizonAzEnd').value = obs.horizon_az_end ?? 350;
            document.getElementById('obsDriftFrame').value = obs.drift_frame || DEFAULTS.drift_frame;
            document.getElementById('obsDriftTime').value = obs.drift_time || DEFAULTS.drift_time;
            document.getElementById('obsDriftWindow').value = obs.drift_window_min ?? DEFAULTS.drift_window_min;
            updateCoordLabels();
            updateEndTime();
        }

        function closeModal() {
            document.getElementById('obsModal').classList.remove('active');
        }

        function autoSave() {
            // Auto-save schedule to server whenever changes are made
            postSchedule().then(r => {
                const el = document.getElementById('autoSaveWarning');
                if (r.ok) {
                    console.log('Schedule auto-saved');
                    if (el) { el.style.display = 'none'; }
                    return;
                }
                // Auto-save is silent when it works, but must not be silent
                // when it fails - this is the path that loses edits.
                console.warn('Auto-save failed:', r.error);
                if (el) { el.textContent = 'Not saved: ' + r.error; el.style.display = 'block'; }
            });
        }

        function saveObservation() {
            if (checkClash()) {
                alert('Cannot save: this observation clashes with another scheduled observation.');
                return;
            }
            const i = parseInt(document.getElementById('obsIndex').value);
            const isDrift = document.getElementById('obsCoordSystem').value === 'drift';
            let startDate = document.getElementById('obsStartDate').value || localDateStr(new Date());
            let startTime = document.getElementById('obsStartTime').value;
            let duration = parseInt(document.getElementById('obsDuration').value);
            if (isDrift) {
                // The date field holds the date of T; a scan whose window opens
                // before midnight starts on the previous day.
                const w = parseInt(document.getElementById('obsDriftWindow').value) || 30;
                const Tdt = new Date(`${startDate}T${document.getElementById('obsDriftTime').value}`);
                const driftStart = new Date(Tdt.getTime() - w * 60000);
                startDate = localDateStr(driftStart);
                startTime = String(driftStart.getHours()).padStart(2,'0') + ':' + String(driftStart.getMinutes()).padStart(2,'0');
                duration = 2 * w;
            }
            const startDt = new Date(`${startDate}T${startTime}`);
            const endDt = new Date(startDt.getTime() + duration * 60000);
            const endDate = localDateStr(endDt);
            const endTime = String(endDt.getHours()).padStart(2,'0') + ':' + String(endDt.getMinutes()).padStart(2,'0');
            const obs = {
                name: document.getElementById('obsName').value,
                coord_system: document.getElementById('obsCoordSystem').value,
                object_name: document.getElementById('obsObjectName').value,
                tle_text: document.getElementById('obsTleText').value,
                coord1_deg: parseInt(document.getElementById('coord1Deg').value) || 0,
                coord1_min: parseInt(document.getElementById('coord1Min').value) || 0,
                coord1_sec: parseFloat(document.getElementById('coord1Sec').value) || 0,
                coord2_deg: parseInt(document.getElementById('coord2Deg').value) || 0,
                coord2_min: parseInt(document.getElementById('coord2Min').value) || 0,
                coord2_sec: parseFloat(document.getElementById('coord2Sec').value) || 0,
                start_date: startDate,
                start_time: startTime,
                end_date: endDate,
                end_time: endTime,
                duration_minutes: duration,
                center_freq_mhz: parseFloat(document.getElementById('obsCenterFreq').value),
                bandwidth_mhz: parseFloat(document.getElementById('obsBandwidth').value),
                gain_db: parseFloat(document.getElementById('obsGain').value),
                channels: parseInt(document.getElementById('obsChannels').value),
                integration_time_s: parseFloat(document.getElementById('obsIntegration').value),
                sdr_type: document.getElementById('obsSdrType').value,
                calibrator: document.getElementById('obsCalibrator').value === 'on',
                end_action: document.getElementById('obsEndAction').value,
                filename: document.getElementById('obsFilename').value,
                cal_grid_n: parseInt(document.getElementById('obsCalGridN').value) || 5,
                cal_spacing_deg: parseFloat(document.getElementById('obsCalSpacing').value) || 1.5,
                cal_interval_min: parseInt(document.getElementById('obsCalInterval').value) || 30,
                horizon_az_step: parseFloat(document.getElementById('obsHorizonAzStep').value) || 5,
                horizon_alt_step: parseFloat(document.getElementById('obsHorizonAltStep').value) || 5,
                horizon_az_start: parseFloat(document.getElementById('obsHorizonAzStart').value) || 5,
                horizon_az_end: parseFloat(document.getElementById('obsHorizonAzEnd').value) || 350,
                drift_frame: document.getElementById('obsDriftFrame').value,
                drift_time: document.getElementById('obsDriftTime').value,
                drift_window_min: parseInt(document.getElementById('obsDriftWindow').value) || 30,
                enabled: i >= 0 ? schedule[i].enabled : true
            };
            if (i >= 0) { schedule[i] = obs; } else { schedule.push(obs); }
            closeModal();
            renderSchedule();
            autoSave();
        }

        function deleteObs(i) {
            if (isRunning(schedule[i])) {
                alert('Cannot delete while running. Stop the observation first.');
                return;
            }
            if (confirm('Delete this observation?')) {
                schedule.splice(i, 1);
                renderSchedule();
                autoSave();
            }
        }

        function toggleEnabled(i) {
            if (isRunning(schedule[i])) {
                alert('Cannot disable while running. Stop the observation first.');
                return;
            }
            schedule[i].enabled = !schedule[i].enabled;
            renderSchedule();
            autoSave();
        }

        function runNow(i) {
            fetch('/api/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(schedule[i])
            }).then(r => r.json()).then(data => {
                if (data.success) updateStatus();
                else alert('Failed: ' + (data.error || 'Unknown'));
            });
        }

        function stopObs() {
            fetch('/api/stop', {method: 'POST'}).then(() => updateStatus());
        }

        function formatRemaining(seconds) {
            if (!seconds) return '';
            const m = Math.floor(seconds / 60);
            const s = Math.floor(seconds % 60);
            return ` (${m}m ${s}s remaining)`;
        }

        let wasRunning = null;
        let soundEnabled = true;

        function playTone(freqs, duration) {
            if (!soundEnabled) return;
            try {
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                const stepDur = duration / freqs.length;
                freqs.forEach((freq, i) => {
                    const osc = ctx.createOscillator();
                    const gain = ctx.createGain();
                    osc.type = 'sine';
                    osc.frequency.value = freq;
                    gain.gain.value = 0.15;
                    osc.connect(gain);
                    gain.connect(ctx.destination);
                    osc.start(ctx.currentTime + i * stepDur);
                    osc.stop(ctx.currentTime + (i + 1) * stepDur);
                });
            } catch(e) {}
        }

        function playStartSound() { playTone([440, 554, 659], 0.4); }
        function playStopSound()  { playTone([659, 554, 440], 0.4); }

        function nextObsCountdown() {
            const now = new Date();
            let nearest = null;
            let nearestName = '';
            schedule.forEach(obs => {
                if (!obs.enabled || !obs.start_time) return;
                const date = obs.start_date || localDateStr(now);
                const start = new Date(`${date}T${obs.start_time}`);
                if (start > now && (!nearest || start < nearest)) {
                    nearest = start;
                    nearestName = obs.name;
                }
            });
            if (!nearest) return '';
            const diff = Math.floor((nearest - now) / 1000);
            if (diff <= 0) return '';
            const h = Math.floor(diff / 3600);
            const m = Math.floor((diff % 3600) / 60);
            const s = diff % 60;
            let t = '';
            if (h > 0) t += h + 'h ';
            t += m + 'm ' + s + 's';
            return ` \u2014 Next: ${nearestName} in ${t}`;
        }

        function updateStatus() {
            fetch('/api/status').then(r => r.json()).then(data => {
                const dot = document.getElementById('statusDot');
                const text = document.getElementById('statusText');
                const btn = document.getElementById('stopBtn');
                if (data.running) {
                    dot.classList.add('running');
                    const remaining = formatRemaining(data.remaining_seconds);
                    text.textContent = `Running: ${data.observation?.name || '?'}${remaining}`;
                    btn.style.display = 'inline-block';
                    if (wasRunning === false) playStartSound();
                    currentObs = data.observation;
                } else {
                    dot.classList.remove('running');
                    text.textContent = 'Idle' + nextObsCountdown();
                    btn.style.display = 'none';
                    if (wasRunning === true) playStopSound();
                    currentObs = null;
                }
                wasRunning = data.running;
                renderSchedule();
            });
        }

        function updateTelescope() {
            fetch('/api/telescope').then(r => r.json()).then(data => {
                const dot = document.getElementById('telescopeDot');
                const text = document.getElementById('telescopeText');
                if (!data.configured) {
                    dot.style.background = '#666';
                    text.textContent = 'Telescope: Disabled';
                } else if (!data.connected) {
                    dot.style.background = '#ff4444';
                    text.textContent = 'Telescope: Offline';
                } else {
                    const s = data.status;
                    const t = data.tracking;
                    dot.style.background = '#00ff88';
                    let info = `Alt ${s.alt.toFixed(1)}° Az ${s.az.toFixed(1)}°`;
                    if (t && t.enabled) {
                        info += ' [Tracking]';
                    }
                    text.textContent = info;
                }
            }).catch(() => {
                const dot = document.getElementById('telescopeDot');
                const text = document.getElementById('telescopeText');
                dot.style.background = '#666';
                text.textContent = 'Telescope: --';
            });
        }

        function setReceiverUi(data) {
            const dot = document.getElementById('receiverDot');
            const text = document.getElementById('receiverText');
            const btn = document.getElementById('receiverBootBtn');
            if (!dot || !text || !btn) return;
            if (data.running) {
                dot.classList.add('running');
                dot.style.background = '';
                const label = data.source === 'observation' ? 'Observation' : 'Started';
                const obs = data.observation ? ` (${data.observation})` : '';
                text.textContent = `Receiver: ${label}${obs}${data.pid ? ' #' + data.pid : ''}`;
                btn.disabled = true;
                btn.title = data.source === 'observation'
                    ? 'A scheduled observation is using the B210 receiver.'
                    : 'The B210 receiver process is already running.';
            } else {
                dot.classList.remove('running');
                dot.style.background = data.returncode === null ? '#666' : '#ff9500';
                text.textContent = data.returncode === null ? 'Receiver: Idle' : `Receiver: Stopped (${data.returncode})`;
                btn.disabled = false;
                btn.title = `Start the B210 receiver with ${data.python || 'radioconda Python'}.`;
            }
        }

        function updateReceiver() {
            fetch('/api/receiver/status').then(r => r.json()).then(setReceiverUi).catch(() => {
                setReceiverUi({running: false, returncode: null});
            });
        }

        function bootReceiver() {
            const btn = document.getElementById('receiverBootBtn');
            if (btn) btn.disabled = true;
            fetch('/api/receiver/start', {method: 'POST'})
                .then(r => r.json())
                .then(data => {
                    if (!data.success && !data.running) {
                        alert('Receiver start failed: ' + (data.error || 'Unknown error'));
                    }
                    setReceiverUi(data);
                })
                .catch(e => alert('Receiver start failed: ' + e))
                .finally(updateReceiver);
        }

        function exportSchedule() {
            const blob = new Blob([JSON.stringify(schedule, null, 2)], {type: 'application/json'});
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'h1_schedule.json';
            a.click();
        }

        function clearPast() {
            const before = schedule.length;
            // Removes exactly the rows badged "Expired" - past entries only,
            // not the malformed ones neverRunsReason() also greys out, since
            // those want fixing rather than silently deleting. It used to
            // substitute today's date for a dateless entry, which deleted
            // recurring entries that were still going to run tomorrow.
            schedule = schedule.filter(obs => !isExpired(obs));
            const removed = before - schedule.length;
            if (removed > 0) {
                renderSchedule();
                autoSave();
            }
            alert(removed > 0 ? `Removed ${removed} past observation(s).` : 'No past observations to clear.');
        }

        let tleResultsData = [];

        function fetchTle() {
            const query = document.getElementById('tleSearch').value.trim();
            if (!query) { alert('Enter a satellite name or NORAD ID.'); return; }
            const info = document.getElementById('passInfo');
            info.textContent = 'Fetching from CelesTrak...';
            info.style.color = '#888';
            fetch('/api/fetch_tle', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({query: query})
            }).then(r => r.json()).then(data => {
                if (data.success && data.results.length > 0) {
                    tleResultsData = data.results;
                    if (data.results.length === 1) {
                        // Single result - use it directly
                        document.getElementById('obsTleText').value = data.results[0].tle;
                        document.getElementById('tleResults').style.display = 'none';
                        info.textContent = 'TLE fetched - click Compute Next Pass';
                        info.style.color = '#00ff88';
                    } else {
                        // Multiple results - show dropdown
                        const sel = document.getElementById('tleResultSelect');
                        sel.innerHTML = data.results.map((r, i) =>
                            `<option value="${i}">${r.name}</option>`
                        ).join('');
                        document.getElementById('tleResults').style.display = '';
                        selectTleResult();
                        info.textContent = data.results.length + ' satellites found - select one';
                        info.style.color = '#00d4ff';
                    }
                } else {
                    document.getElementById('tleResults').style.display = 'none';
                    info.textContent = data.error || 'Not found';
                    info.style.color = '#ff4757';
                }
            }).catch(e => {
                info.textContent = 'Error: ' + e;
                info.style.color = '#ff4757';
            });
        }

        function selectTleResult() {
            const idx = parseInt(document.getElementById('tleResultSelect').value);
            if (tleResultsData[idx]) {
                document.getElementById('obsTleText').value = tleResultsData[idx].tle;
            }
        }

        function predictPass() {
            const tle = document.getElementById('obsTleText').value.trim();
            if (!tle) { alert('Paste or load a TLE first.'); return; }
            const info = document.getElementById('passInfo');
            info.textContent = 'Computing...';
            info.style.color = '#888';
            fetch('/api/predict_pass', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({tle_text: tle})
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    const p = data.pass;
                    // Auto-fill schedule fields
                    document.getElementById('obsStartDate').value = p.start_date;
                    document.getElementById('obsStartTime').value = p.start_time;
                    document.getElementById('obsDuration').value = Math.ceil(p.duration_minutes);
                    document.getElementById('obsName').value = document.getElementById('obsName').value || p.name;
                    updateEndTime();
                    info.textContent = 'Pass found!';
                    info.style.color = '#00ff88';
                    document.getElementById('passDetails').style.display = 'block';
                    document.getElementById('passDetails').innerHTML =
                        `<b>${p.name}</b><br>` +
                        `Rise: ${p.rise_time_local} (Az ${p.rise_az}\\u00b0)<br>` +
                        `Max:  ${p.max_time_utc} UTC (El ${p.max_el}\\u00b0)<br>` +
                        `Set:  ${p.set_time_local} (Az ${p.set_az}\\u00b0)<br>` +
                        `Duration: ${p.duration_minutes} min`;
                } else {
                    info.textContent = data.error || 'No pass found';
                    info.style.color = '#ff4757';
                    document.getElementById('passDetails').style.display = 'none';
                }
            }).catch(e => {
                info.textContent = 'Error: ' + e;
                info.style.color = '#ff4757';
            });
        }

        function loadTleFile(e) {
            const file = e.target.files[0];
            if (file) {
                const reader = new FileReader();
                reader.onload = ev => {
                    document.getElementById('obsTleText').value = ev.target.result.trim();
                    document.getElementById('passInfo').textContent = 'TLE loaded from file';
                    document.getElementById('passInfo').style.color = '#00d4ff';
                };
                reader.readAsText(file);
            }
            e.target.value = '';
        }

        function loadFile(e) {
            const file = e.target.files[0];
            if (file) {
                const reader = new FileReader();
                reader.onload = ev => {
                    try {
                        schedule = JSON.parse(ev.target.result);
                        renderSchedule();
                        alert('Loaded!');
                    } catch { alert('Invalid JSON'); }
                };
                reader.readAsText(file);
            }
            e.target.value = '';
        }

        // ---- Tabs ----
        function switchTab(name) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            document.querySelector(`.tab[onclick="switchTab('${name}')"]`).classList.add('active');
            document.getElementById('tab-' + name).classList.add('active');
            if (name === 'config') loadConfig();
            if (name === 'log') loadLog();
            if (name === 'sunscan') { pollSunScan(); pollCalDay(); loadCalModel(); }
            // Leaving the tab stops the loop; scheduleCameraRefresh cancels
            // itself whenever the camera tab is not the one on screen.
            if (name === 'horizon') pollHorizon();
            if (name === 'observe') refreshObserveTuning();
            if (name === 'rf') {
                rfRefresh(); rfRefreshTarget(); rfShowChosen();
                rfLoadBandpassPlot(); rfLoadGainPlot();
            }
            if (name === 'simulator') showSimulator();
            if (name === 'observe') { loadObserveParams(false); loadObserveLast(); }
            if (name === 'camera' && !camObjectUrl) refreshCamera();
            else scheduleCameraRefresh();
        }

        // ---- Simulator ----
        // Built on first visit and then left alone. It fetches ~33 MB of sky
        // data and decodes it to a ~80 MB cube, so rebuilding the frame on
        // every tab switch would repeat the whole load; hiding it costs
        // nothing, since .tab-content already toggles display.
        //
        // Built here rather than in the markup for a second reason: a canvas
        // laid out inside a display:none parent sizes to zero. switchTab adds
        // .active before calling this, so by now the host is on screen.
        let simFrame = null;

        function showSimulator() {
            if (simFrame) return;
            const host = document.getElementById('simHost');
            host.innerHTML = '';
            simFrame = document.createElement('iframe');
            simFrame.src = '/simulator/';
            simFrame.style.cssText = 'width:100%; height:100%; border:0; display:block;';
            simFrame.title = 'Sky simulator';
            host.appendChild(simFrame);
        }

        // ---- Observe ----
        // Stamp of the hand-off already on the form, so opening the tab picks up
        // a Realise that happened since it was last looked at, and does not
        // overwrite edits made here in the meantime.
        let obvAppliedStamp = null;

        function onObserveModeChange() {
            const drift = document.getElementById('obvMode').value === 'drift';
            // In drift mode the duration IS the scan length, and the source
            // transits at its mid-point; saying so beats a tooltip.
            document.getElementById('obvDurationLabel').innerHTML =
                drift ? 'Scan length <span class="unit">(min)</span> &mdash; transit at mid-point'
                      : 'Total integration time <span class="unit">(min)</span> &mdash; the simulator&rsquo;s &tau;';
        }

        function loadObserveParams(force) {
            fetch('/api/observe/params').then(r => r.json()).then(d => {
                const info = document.getElementById('obvSource');
                if (!d.available) {
                    if (force) setObserveStatus('Nothing handed over yet - press Realise in the Simulator tab.', '#ffa502');
                    return;
                }
                const p = d.params;
                if (!force && p.source_utc === obvAppliedStamp) return;
                obvAppliedStamp = p.source_utc;
                document.getElementById('obvMode').value = p.mode;
                document.getElementById('obvL').value = p.l;
                document.getElementById('obvB').value = p.b;
                document.getElementById('obvCenterFreq').value = p.center_freq_mhz;
                document.getElementById('obvBandwidth').value = p.bandwidth_mhz;
                document.getElementById('obvChannels').value = p.channels;
                document.getElementById('obvDuration').value = p.duration_minutes;
                // Null for a tracked spectrum: there tau is the length of the
                // observation, which has gone into the duration, and how finely
                // the run is chopped into saved spectra is ours to choose. In
                // drift mode tau is the time per sample and does belong here.
                if (p.integration_time_s) {
                    document.getElementById('obvIntegration').value = p.integration_time_s;
                }
                onObserveModeChange();
                const when = new Date(p.source_utc);
                info.innerHTML = 'Copied from the simulator at <strong>' +
                    when.toLocaleTimeString() + '</strong> &mdash; ' +
                    (p.mode === 'drift' ? 'drift scan' : 'tracked spectrum') +
                    ' of l=' + p.l.toFixed(2) + '&deg;, b=' + p.b.toFixed(2) + '&deg;. ' +
                    'Edit anything below before starting.';
            }).catch(e => setObserveStatus('Could not read the simulator hand-off: ' + e, '#ff4757'));
        }

        function setObserveStatus(text, colour) {
            const el = document.getElementById('obvStatus');
            el.textContent = text;
            el.style.color = colour || '#888';
        }

        // Build the schedule-entry shape the rest of the app already speaks, so
        // Start Now and Send to Scheduler hand over exactly the same document.
        function observeToObs() {
            const drift = document.getElementById('obvMode').value === 'drift';
            const num = (id, dflt) => {
                const v = parseFloat(document.getElementById(id).value);
                return Number.isFinite(v) ? v : dflt;
            };
            const l = num('obvL', 0), b = num('obvB', 0);
            const obs = Object.assign({}, DEFAULTS, {
                name: document.getElementById('obvName').value.trim() || 'Simulator target',
                coord_system: drift ? 'drift' : 'galactic',
                drift_frame: 'galactic',
                // Decimal degrees in the degrees field, which dms_to_decimal
                // sums as given; the minutes and seconds boxes are for the
                // schedule form's benefit, not this one's.
                coord1_deg: l, coord1_min: 0, coord1_sec: 0,
                coord2_deg: b, coord2_min: 0, coord2_sec: 0,
                duration_minutes: Math.round(num('obvDuration', 30)),
                center_freq_mhz: num('obvCenterFreq', 1420.405752),
                bandwidth_mhz: num('obvBandwidth', 2.4),
                gain_db: num('obvGain', 40),
                channels: Math.round(num('obvChannels', 4096)),
                integration_time_s: num('obvIntegration', 3.0),
                sdr_type: document.getElementById('obvSdr').value,
                filename: document.getElementById('obvFilename').value.trim(),
                end_action: document.getElementById('obvEndAction').value,
                calibrator: false,
                enabled: true,
                // No date or time: for a Run Now start the scheduler reads the
                // drift beam-crossing time as now + half the duration, which is
                // what a drift scan started this moment means.
                start_date: '', start_time: '',
            });
            return obs;
        }

        function observeStartNow() {
            const obs = observeToObs();
            const btn = document.getElementById('obvStartBtn');
            btn.disabled = true;
            setObserveStatus('Pointing and starting the receiver...', '#00d4ff');
            fetch('/api/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(obs)
            }).then(r => r.json()).then(d => {
                if (d.success) {
                    setObserveStatus('Observation started.', '#2ed573');
                    updateStatus();
                } else {
                    setObserveStatus('Failed: ' + (d.error || 'unknown') + ' - see the Log tab.', '#ff4757');
                }
            }).catch(e => setObserveStatus('Failed: ' + e, '#ff4757'))
              .finally(() => { btn.disabled = false; });
        }

        function loadObserveLast() {
            fetch('/api/observe/last').then(r => r.json()).then(d => {
                const el = document.getElementById('obvLastInfo');
                if (!d.available) {
                    el.textContent = 'Nothing has finished yet this session.';
                    return;
                }
                const kind = d.mode === 'drift' ? 'Drift scan' : 'Spectrum';
                const size = d.size_bytes ? ' &mdash; ' + (d.size_bytes / 1e6).toFixed(1) + ' MB' : '';
                el.innerHTML = kind + ' &ldquo;' + d.name + '&rdquo;, ended ' +
                    new Date(d.ended_at).toLocaleTimeString() +
                    ' &mdash; <code>' + d.filename + '</code>' + size +
                    (d.exists ? '' : ' <span style="color:#ff4757;">(file missing)</span>');
            }).catch(() => {});
        }

        function showObservePlot() {
            const host = document.getElementById('obvPlot');
            host.innerHTML = '<span style="color:#888; font-size:12px;">Drawing&hellip;</span>';
            // Fetched rather than dropped straight into an img src: a refusal
            // comes back as JSON with a reason - still recording, no spectra -
            // and a broken image icon would throw that away.
            fetch('/api/observe/plot?' + Date.now()).then(r => {
                if (!r.ok) return r.json().then(d => { throw new Error(d.error || ('HTTP ' + r.status)); });
                return r.blob();
            }).then(b => {
                const url = URL.createObjectURL(b);
                host.innerHTML = '<img src="' + url + '" style="width:100%; height:auto; '
                               + 'border-radius:8px; border:1px solid #333;">';
            }).catch(e => {
                host.innerHTML = '<span style="color:#ffa502; font-size:12px;">' + e.message + '</span>';
            });
        }

        function observeToScheduler() {
            // Reuse the Add Observation form rather than duplicating date, time
            // and clash handling here: it is the one place that knows how a
            // drift slot is laid out around its transit.
            document.getElementById('modalTitle').textContent = 'Add Observation';
            document.getElementById('obsIndex').value = -1;
            fillForm(observeToObs());
            document.getElementById('obsModal').classList.add('active');
        }

        // ---- Horizon scan ----
        let hzPollTimer = null;

        function startHorizon() {
            const params = {
                az_step: parseFloat(document.getElementById('hzAzStep').value) || 5,
                alt_step: parseFloat(document.getElementById('hzAltStep').value) || 5,
                alt_start: parseFloat(document.getElementById('hzAltStart').value) || 5,
                alt_max: parseFloat(document.getElementById('hzAltMax').value) || 60,
                settle_s: parseFloat(document.getElementById('hzSettle').value),
                integration_time_s: parseFloat(document.getElementById('hzIntegration').value) || 2,
                home_every_strips: parseInt(document.getElementById('hzHomeEvery').value, 10),
                sdr_type: document.getElementById('hzSdrType').value,
            };
            fetch('/api/horizon/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(params)
            }).then(r => r.json()).then(d => {
                if (!d.success) {
                    document.getElementById('hzStatus').innerHTML =
                        '<span style="color:#ff4757;">' + (d.error || 'Could not start') + '</span>';
                    return;
                }
                pollHorizon();
            }).catch(e => alert('Horizon scan request failed: ' + e));
        }

        function stopHorizon() {
            fetch('/api/horizon/stop', {method: 'POST'}).then(() => pollHorizon());
        }

        function pollHorizon() {
            fetch('/api/horizon/status').then(r => r.json()).then(d => {
                const status = document.getElementById('hzStatus');
                document.getElementById('hzStartBtn').style.display = d.running ? 'none' : 'inline-block';
                document.getElementById('hzStopBtn').style.display = d.running ? 'inline-block' : 'none';
                if (d.running) {
                    let info = '<span style="color:#00d4ff;">Scanning</span> &mdash; azimuth ' +
                               d.progress + ' of ' + d.total;
                    const p = d.point_info;
                    if (p) {
                        info += '<br><span style="color:#888;">az ' + p.az.toFixed(0) + '&deg;: ' +
                                (p.edge === null ? 'no edge found'
                                 : 'edge ' + p.edge.toFixed(1) + '&deg;, clear above ' +
                                   (p.clear === null ? '?' : p.clear.toFixed(1)) + '&deg;') +
                                ' (' + p.estimator + ')</span>';
                    }
                    status.innerHTML = info;
                } else if (d.error) {
                    status.innerHTML = '<span style="color:#ff4757;">' + d.error + '</span>';
                } else {
                    status.innerHTML = '<span style="color:#888;">Idle.</span>';
                }
                if (d.running) {
                    if (hzPollTimer) clearTimeout(hzPollTimer);
                    hzPollTimer = setTimeout(pollHorizon, 2000);
                } else {
                    if (hzPollTimer) { clearTimeout(hzPollTimer); hzPollTimer = null; }
                    loadHorizonProfile();
                }
            }).catch(() => {});
        }

        function loadHorizonProfile() {
            fetch('/api/horizon/profile').then(r => r.json()).then(m => {
                const box = document.getElementById('hzProfile');
                if (!m.success) {
                    box.innerHTML = '<span style="color:#888;">' + (m.error || 'No profile') + '</span>';
                    return;
                }
                const clears = m.azimuths.map(a => a.clear).filter(v => v !== null);
                const edges = m.azimuths.map(a => a.edge).filter(v => v !== null);
                const envelope = m.azimuths.filter(a => a.estimator === 'envelope').length;
                const highest = m.azimuths.reduce((b, a) =>
                    (a.edge !== null && (b === null || a.edge > b.edge)) ? a : b, null);
                let html = '<table style="width:100%; font-size:13px;">';
                const row = (k, v) => '<tr><td style="color:#888; padding:4px 8px;">' + k +
                                      '</td><td>' + v + '</td></tr>';
                if (m.sdr_type === 'demo') {
                    html += row('<span style="color:#ff4757;">Source</span>',
                        '<span style="color:#ff4757;">Simulated &mdash; this is not the ' +
                        'observatory horizon</span>');
                }
                html += row('Measured', new Date(m.measured_utc).toLocaleString());
                html += row('Azimuths', m.n_azimuths + ' at ' + m.az_step_deg + '&deg; spacing' +
                            (envelope ? ' <span style="color:#ffa502;">(' + envelope +
                             ' by envelope)</span>' : ''));
                html += row('Duration', (m.duration_s / 60).toFixed(0) + ' min');
                if (highest) {
                    html += row('Highest obstruction', highest.edge.toFixed(1) + '&deg; at az ' +
                                highest.az.toFixed(0) + '&deg;');
                }
                if (edges.length) {
                    const med = a => a.slice().sort((x, y) => x - y)[Math.floor(a.length / 2)];
                    html += row('Median edge', med(edges).toFixed(1) + '&deg;');
                    html += row('Median clearance', med(clears).toFixed(1) + '&deg;');
                }
                const refs = m.sky_references || [];
                if (refs.length > 1) {
                    // The sky reference is the run's own health check: it is the
                    // same position every time, so a drift in it is the
                    // instrument, not the sky. A collapse in it is what a
                    // mount that has lost its position looks like from here.
                    const levels = refs.map(r => r.level);
                    const drift = 100 * (Math.max(...levels) - Math.min(...levels))
                                  / Math.min(...levels);
                    html += row('Sky reference drift',
                        '<span style="color:' + (drift > 10 ? '#ff4757' : '#888') + ';">' +
                        drift.toFixed(1) + '% across ' + refs.length + ' checks</span>');
                }
                if (m.complete === false) {
                    html += row('<span style="color:#ffa502;">Incomplete</span>',
                        '<span style="color:#ffa502;">azimuths still blocked at the ceiling</span>');
                }
                const b = {};
                if (b.available) {
                    // Up-cuts against down-cuts: a real horizon has no reason to
                    // zigzag with the parity of the azimuth index, so anything
                    // significant here is backlash in the altitude axis.
                    const sig = b.significance;
                    html += row('Up minus down cuts',
                        '<span style="color:' + (sig > 3 ? '#ff4757' : '#888') + ';">' +
                        (b.up_minus_down_deg >= 0 ? '+' : '') + b.up_minus_down_deg.toFixed(3) +
                        ' &plusmn; ' + b.uncertainty_deg.toFixed(3) + '&deg; (' +
                        sig.toFixed(1) + ' sigma)</span>');
                }
                html += '</table>';
                box.innerHTML = html;
                document.getElementById('hzLandscape').style.display = '';
                document.getElementById('hzPlotContainer').innerHTML =
                    '<img src="/api/horizon/plot?' + Date.now() + '" style="max-width:100%; border-radius:8px; border:1px solid #333;">';
            }).catch(() => {});
        }


        // ---- what the receiver will actually be tuned to ----
        // The B210 is a direct-conversion receiver, so the tuned frequency
        // lands on the FFT's DC bin and UHD's automatic offset correction
        // subtracts whatever is there - including the line. The LO is
        // therefore offset, and the sample rate raised if it must be to keep
        // the line in the flat part of the band. Saying so here means the
        // numbers typed above are never silently replaced.
        let obvTuningTimer = null;

        function refreshObserveTuning() {
            const params = new URLSearchParams({
                center_freq_mhz: document.getElementById('obvCenterFreq').value || 1420.405752,
                bandwidth_mhz: document.getElementById('obvBandwidth').value || 2.4,
                channels: document.getElementById('obvChannels').value || 4096,
            });
            fetch('/api/tuning?' + params).then(r => r.json()).then(p => {
                const box = document.getElementById('obvTuning');
                if (!p.success) { box.textContent = p.error || 'Tuning unavailable'; return; }
                const mhz = v => (v / 1e6).toFixed(6);
                let html = '<div style="color:#00d4ff; margin-bottom:4px;">The receiver will be tuned to '
                         + mhz(p.tuned_center_freq_hz) + ' MHz</div>';
                html += '<div>' + (p.lo_offset_hz / 1e6).toFixed(2) + ' MHz above '
                     + mhz(p.sky_center_freq_hz) + ' MHz, so the DC artefact lands clear of the line '
                     + 'instead of on top of it.</div>';
                if (p.sample_rate_raised) {
                    html += '<div style="color:#ffa502; margin-top:4px;">Bandwidth raised '
                         + (p.requested_sample_rate_hz / 1e6).toFixed(2) + ' &rarr; '
                         + (p.sample_rate_hz / 1e6).toFixed(2) + ' MHz to keep the line in the flat '
                         + 'part of the band';
                    if (p.channels !== p.requested_channels) {
                        html += ', and channels ' + p.requested_channels + ' &rarr; ' + p.channels
                             + ' to hold ' + (p.channel_width_hz / 1e3).toFixed(2) + ' kHz resolution';
                    }
                    html += '.</div>';
                }
                box.innerHTML = html;
            }).catch(() => {});
        }

        function scheduleObserveTuning() {
            if (obvTuningTimer) clearTimeout(obvTuningTimer);
            obvTuningTimer = setTimeout(refreshObserveTuning, 250);
        }


        // ---- RF calibration ----
        // Two measurements that own the dish for a couple of minutes each. The
        // page polls only while the tab is open and something is running: this
        // controller has known cross-task locking weaknesses (issue #1), so idle
        // tabs must not sit on it.
        let rfPollTimer = null;
        let rfTickTimer = null;
        let rfPlotIsCurrent = false;
        let rfEndsAt = null;      // ms since epoch, or null for an untimed stage
        let rfTotalS = null;

        function rfStartTicking() {
            if (!rfTickTimer) rfTickTimer = setInterval(rfTick, 250);
            rfTick();
        }

        function rfStopTicking() {
            if (rfTickTimer) { clearInterval(rfTickTimer); rfTickTimer = null; }
            rfEndsAt = null; rfTotalS = null;
        }

        function rfTick() {
            const box = document.getElementById('rfCountdown');
            if (!box) return;
            if (!rfEndsAt) {
                // A slew has no knowable duration; say so rather than invent one.
                box.innerHTML = '<span style="color:#888; font-size:12px;">'
                              + 'no fixed duration for this step</span>';
                return;
            }
            const left = Math.max(0, (rfEndsAt - Date.now()) / 1000);
            const total = rfTotalS || 1;
            const done = Math.min(100, Math.max(0, 100 * (1 - left / total)));
            const mm = Math.floor(left / 60), ss = Math.floor(left % 60);
            box.innerHTML =
                '<div style="font-size:26px; font-variant-numeric:tabular-nums; color:#00d4ff;">'
                + mm + ':' + (ss < 10 ? '0' : '') + ss + '</div>'
                + '<div style="color:#888; font-size:12px; margin-bottom:6px;">'
                + Math.round(left) + ' s of ' + Math.round(total) + ' s remaining</div>'
                + '<div style="height:8px; background:#0a0a1a; border:1px solid #333; '
                + 'border-radius:4px; overflow:hidden;">'
                + '<div style="height:100%; width:' + done.toFixed(1) + '%; '
                + 'background:#00d4ff; transition:width .25s linear;"></div></div>';
        }

        function rfAge(iso) {
            if (!iso) return '';
            const mins = (Date.now() - Date.parse(iso)) / 60000;
            if (!isFinite(mins)) return '';
            if (mins < 90) return Math.round(mins) + ' min ago';
            if (mins < 60 * 48) return (mins / 60).toFixed(1) + ' hours ago';
            return (mins / 1440).toFixed(1) + ' days ago';
        }

        function rfRefresh() {
            fetch('/api/rf/status').then(r => r.json()).then(d => {
                if (!d.success) return;
                const bp = document.getElementById('rfBandpassStatus');
                if (!d.bandpass) {
                    bp.innerHTML = '<span style="color:#ffa502;">No template stored &mdash; '
                                 + 'spectra are not being corrected.</span>';
                } else {
                    bp.innerHTML =
                        '<div style="color:#00d4ff;">Order ' + d.bandpass.degree
                        + ' over &plusmn;' + d.bandpass.band_mhz.toFixed(3) + ' MHz, residual '
                        + d.bandpass.residual_pct.toFixed(3) + '%</div>'
                        + '<div>measured ' + rfAge(d.bandpass.created_utc)
                        + (d.bandpass.source_name ? ' at ' + d.bandpass.source_name : '')
                        + ', at LO ' + d.bandpass.lo_mhz.toFixed(6) + ' MHz, '
                        + d.bandpass.sample_rate_mhz.toFixed(3) + ' Msps</div>'
                        + '<div style="color:#666;">Only applies to observations at that '
                        + 'exact tuning; anything else is left uncorrected and says so.</div>';
                }

                const g = document.getElementById('rfGainStatus');
                if (!d.gain) {
                    g.innerHTML = '<span style="color:#ffa502;">Not calibrated &mdash; '
                                + 'spectra are in counts, not kelvin.</span>';
                } else {
                    const warn = d.gain.t_sys_bound_active
                        ? '<div style="color:#ff4757;">T_sys hit its 50 K floor &mdash; the fit '
                        + 'is against the bound, not a measurement. Check the bandpass template '
                        + 'and that the slew arrived.</div>' : '';
                    // The floor at 50 K only catches errors of one sign. A run
                    // that recorded while the mount was still slewing fitted
                    // 467 K and said nothing about it.
                    const hot = d.gain.t_sys_implausible
                        ? '<div style="color:#ff4757;">T_sys is far hotter than any working '
                        + 'system &mdash; suspect a recording that began before the mount '
                        + 'arrived, a stale bandpass template, or ground in the beam.</div>' : '';
                    const weak = (d.gain.correlation < 0.8)
                        ? '<div style="color:#ffa502;">Weak correlation: little lever arm in '
                        + 'this pointing, so T_sys is poorly determined.</div>' : '';
                    g.innerHTML =
                        '<div style="color:#00d4ff;">T_sys ' + d.gain.t_sys_k.toFixed(1)
                        + ' K &nbsp; gain ' + d.gain.gain_counts_per_k.toExponential(3)
                        + ' counts/K</div>'
                        + '<div>from l=' + Math.round(d.gain.glon) + ' b=' + Math.round(d.gain.glat)
                        + ', ' + rfAge(d.gain.observed_utc)
                        + ' &nbsp; r=' + d.gain.correlation.toFixed(3)
                        + ' &nbsp; residual ' + d.gain.residual_rms_k.toFixed(2) + ' K</div>'
                        + (d.gain.implied_ppm
                           ? '<div style="color:#888;">receiver clock '
                             + d.gain.implied_ppm.toFixed(2) + ' ppm ('
                             + d.gain.velocity_shift_km_s.toFixed(2)
                             + ' km/s), fitted with the gain</div>'
                           : '')
                        + (d.gain.implied_loss_db
                           ? '<div style="color:#888;">equivalent to '
                             + d.gain.implied_loss_db.toFixed(2)
                             + ' dB of loss ahead of the LNA, if that is what it is</div>'
                           : '')
                        + warn + hot + weak;
                }

                const st = d.state || {};
                const prog = document.getElementById('rfProgress');
                if (st.running) {
                    let t = st.target ? (' &mdash; l=' + Math.round(st.target.glon)
                          + ' b=' + Math.round(st.target.glat)
                          + (st.target.alt_deg ? ' at alt ' + st.target.alt_deg.toFixed(0) : '')) : '';
                    rfEndsAt = st.stage_ends_utc ? Date.parse(st.stage_ends_utc) : null;
                    rfTotalS = st.stage_total_s || null;
                    prog.innerHTML = '<span style="color:#00d4ff;">' + st.job + ': '
                                   + (st.stage || 'working') + '</span>' + t
                                   + '<div id="rfCountdown" style="margin-top:10px;"></div>';
                    rfStartTicking();
                } else if (st.error) {
                    prog.innerHTML = '<span style="color:#ff4757;">' + st.job + ' failed: '
                                   + st.error + '</span>';
                } else if (st.result) {
                    prog.innerHTML = '<span style="color:#2ed573;">' + st.job
                                   + ' finished.</span>';
                } else {
                    prog.textContent = 'Idle.';
                }

                if (st.running && !rfPollTimer) rfPollTimer = setInterval(rfRefresh, 2000);
                if (!st.running && rfPollTimer) { clearInterval(rfPollTimer); rfPollTimer = null; }
                if (!st.running) rfStopTicking();
                // A finished bandpass job means the stored template changed, so
                // the plot on screen is of the previous one.
                if (!st.running && st.result && !rfPlotIsCurrent) {
                    rfPlotIsCurrent = true;
                    if (st.job === 'bandpass') rfLoadBandpassPlot();
                    if (st.job === 'gain') rfLoadGainPlot();
                }
                if (st.running) rfPlotIsCurrent = false;
            }).catch(() => {});
        }

        function rfRefreshTarget() {
            fetch('/api/rf/target').then(r => r.json()).then(d => {
                const box = document.getElementById('rfTargets');
                if (!d.success) { box.textContent = d.error || 'unavailable'; return; }
                const list = d.targets || [];
                if (!list.length) {
                    box.innerHTML = '<span style="color:#ff4757;">Nothing is high enough '
                                  + 'right now, in any direction.</span>';
                    return;
                }
                let h = '<table style="width:100%; border-collapse:collapse; font-size:12px;">'
                      + '<tr style="color:#666; text-align:left;">'
                      + '<th style="padding:4px 8px;">l, b</th>'
                      + '<th style="padding:4px 8px;">alt</th>'
                      + '<th style="padding:4px 8px;">az</th>'
                      + '<th style="padding:4px 8px;">looking</th>'
                      + '<th style="padding:4px 8px;">expected peak</th>'
                      + '<th></th></tr>';
                list.forEach(function (t) {
                    const b = (t.glat >= 0 ? '+' : '') + Math.round(t.glat);
                    h += '<tr style="border-top:1px solid #262640;">'
                       + '<td class="mono" style="padding:5px 8px; color:#00d4ff;">l='
                       + Math.round(t.glon) + ' b=' + b + '</td>'
                       + '<td class="mono" style="padding:5px 8px;">' + t.alt_deg.toFixed(1) + '&deg;</td>'
                       + '<td class="mono" style="padding:5px 8px;">' + t.az_deg.toFixed(1) + '&deg;</td>'
                       + '<td style="padding:5px 8px; color:#ffa502;">' + t.compass + '</td>'
                       + '<td class="mono" style="padding:5px 8px;">' + t.expected_peak_k.toFixed(0) + ' K</td>'
                       + '<td style="padding:3px 8px;"><button class="btn" style="padding:3px 10px; font-size:11px;"'
                       + ' onclick="rfUse(' + t.glon + ',' + t.glat + ')">Use</button></td>'
                       + '</tr>';
                });
                // Say what the predicted peaks assume. The Simulator tab
                // defaults to a main-beam efficiency of 0.7 and this to 1.0, so
                // the same direction reads 9.5 K there and 13.5 K here; without
                // the assumptions on screen that difference is a mystery.
                box.innerHTML = h + '</table>'
                    + '<div style="color:#666; font-size:11px; margin-top:6px;">'
                    + 'peaks predicted for a ' + (d.beam_fwhm_deg || 0).toFixed(2)
                    + '&deg; beam at main-beam efficiency '
                    + (d.main_beam_efficiency || 0).toFixed(2)
                    + ' &mdash; the Simulator tab defaults to 0.70, which reads '
                    + (0.70 / (d.main_beam_efficiency || 1)).toFixed(2)
                    + '&times; these values</div>';
            }).catch(() => {});
        }

        function rfLoadBandpassPlot() {
            const host = document.getElementById('rfBandpassPlot');
            host.textContent = 'Drawing\u2026';
            fetch('/api/rf/bandpass/plot?' + Date.now()).then(r => {
                if (!r.ok) return r.json().then(d => { throw new Error(d.error || ('HTTP ' + r.status)); });
                return r.blob();
            }).then(b => {
                const url = URL.createObjectURL(b);
                host.innerHTML = '<img src="' + url + '" style="width:100%; height:auto; '
                               + 'border-radius:8px; border:1px solid #333;">';
            }).catch(e => {
                host.innerHTML = '<span style="color:#ffa502;">' + e.message + '</span>';
            });
        }

        function rfLoadGainPlot() {
            const host = document.getElementById('rfGainPlot');
            host.textContent = 'Drawing\u2026';
            fetch('/api/rf/gain/plot?' + Date.now()).then(r => {
                if (!r.ok) return r.json().then(d => { throw new Error(d.error || ('HTTP ' + r.status)); });
                return r.blob();
            }).then(b => {
                const url = URL.createObjectURL(b);
                host.innerHTML = '<img src="' + url + '" style="width:100%; height:auto; '
                               + 'border-radius:8px; border:1px solid #333;">';
            }).catch(e => {
                host.innerHTML = '<span style="color:#888;">' + e.message + '</span>';
            });
        }

        function rfUse(l, b) {
            document.getElementById('rfGlon').value = l;
            document.getElementById('rfGlat').value = b;
            rfShowChosen();
        }

        function rfShowChosen() {
            const l = document.getElementById('rfGlon').value;
            const b = document.getElementById('rfGlat').value;
            const box = document.getElementById('rfChosen');
            if (l === '' || b === '') {
                box.innerHTML = 'Nothing chosen &mdash; a direction will be picked '
                              + 'automatically, which is what walked into a tower.';
            } else {
                box.innerHTML = 'Will calibrate on <span style="color:#00d4ff;">l=' + l
                              + ' b=' + b + '</span>.';
            }
        }

        function rfRun(job) {
            const secs = job === 'gain' ? 180 : 120;
            const body = {job: job, duration_s: secs};
            if (job === 'gain') {
                const l = document.getElementById('rfGlon').value;
                const b = document.getElementById('rfGlat').value;
                if (l !== '' && b !== '') { body.glon = parseFloat(l); body.glat = parseFloat(b); }
            }
            fetch('/api/rf/run', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            }).then(r => r.json()).then(d => {
                if (!d.success) { alert(d.error || 'could not start'); return; }
                rfRefresh();
            }).catch(e => alert(e));
        }

        function rfCancel() {
            fetch('/api/rf/cancel', {method: 'POST'}).then(() => rfRefresh());
        }

        // ---- Safety camera ----
        // Fetched as a blob rather than pointed at with an <img src>: it keeps
        // the previous frame on screen while the next is being taken, and a
        // failure arrives as the server's actual message instead of a broken
        // image icon.
        let camObjectUrl = null;
        let camTimer = null;

        function cameraTabVisible() {
            const tab = document.getElementById('tab-camera');
            return !document.hidden && tab && tab.classList.contains('active');
        }

        // Chained from the end of each capture rather than run on an interval:
        // at 1 s the capture takes a good fraction of the gap, and setInterval
        // would queue requests behind each other the moment one ran long.
        function scheduleCameraRefresh() {
            if (camTimer) { clearTimeout(camTimer); camTimer = null; }
            const every = parseInt(document.getElementById('camAutoRefresh').value, 10);
            if (!every || !cameraTabVisible()) return;
            camTimer = setTimeout(refreshCamera, every * 1000);
        }

        function onCameraAutoChange() {
            if (camTimer) { clearTimeout(camTimer); camTimer = null; }
            if (parseInt(document.getElementById('camAutoRefresh').value, 10)) refreshCamera();
        }

        // A hidden tab keeps its timers in some browsers and throttles them in
        // others; neither should leave the camera streaming, so pause outright
        // and pick up again when the page comes back.
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                if (camTimer) { clearTimeout(camTimer); camTimer = null; }
            } else {
                scheduleCameraRefresh();
            }
        });

        function refreshCamera() {
            const button = document.getElementById('camRefreshBtn');
            const status = document.getElementById('camStatus');
            button.disabled = true;
            status.textContent = 'Capturing…';
            fetch('/api/camera/snapshot', {cache: 'no-store'}).then(r => {
                if (!r.ok) {
                    return r.json()
                        .catch(() => ({error: 'HTTP ' + r.status}))
                        .then(d => { throw new Error(d.error || ('HTTP ' + r.status)); });
                }
                const captured = r.headers.get('X-Capture-Time');
                const frames = r.headers.get('X-Capture-Frames');
                return r.blob().then(blob => ({blob, captured, frames}));
            }).then(({blob, captured, frames}) => {
                const url = URL.createObjectURL(blob);
                document.getElementById('camView').innerHTML =
                    '<img src="' + url + '" alt="Safety camera view" ' +
                    'style="max-width:100%; border-radius:6px;">';
                // Only after the new frame is on screen, or the browser may
                // still be decoding the old one.
                if (camObjectUrl) URL.revokeObjectURL(camObjectUrl);
                camObjectUrl = url;
                const when = captured ? new Date(captured) : new Date();
                status.innerHTML = '<span style="color:#00d4ff;">Captured ' +
                    when.toLocaleTimeString() + '</span>' +
                    (frames ? '<span style="color:#888;"> &middot; ' + frames +
                              (frames === '1' ? ' frame' : ' frames') + '</span>' : '');
            }).catch(e => {
                status.innerHTML = '<span style="color:#ff4757;">' + e.message + '</span>';
            }).finally(() => {
                button.disabled = false;
                // Chained even after a failure, so a camera that comes back
                // recovers on its own rather than needing a click.
                scheduleCameraRefresh();
            });
        }

        // ---- Sun Scan ----
        let ssPollTimer = null;

        function startSunScan() {
            const params = {
                n: parseInt(document.getElementById('ssGridN').value),
                grid_spacing_deg: parseFloat(document.getElementById('ssSpacing').value),
                integration_time_s: parseFloat(document.getElementById('ssIntegration').value),
                center_freq_mhz: parseFloat(document.getElementById('ssCenterFreq').value),
                bandwidth_mhz: parseFloat(document.getElementById('ssBandwidth').value),
                gain_db: parseFloat(document.getElementById('ssGain').value),
                sdr_type: document.getElementById('ssSdrType').value,
                beam_fwhm_deg: parseFloat(document.getElementById('ssBeamFwhm').value),
            };
            fetch('/api/sunscan/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(params)
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    document.getElementById('ssStartBtn').style.display = 'none';
                    document.getElementById('ssStopBtn').style.display = 'inline-block';
                    document.getElementById('ssProgress').style.display = 'block';
                    document.getElementById('ssStatus').innerHTML = '<span style="color:#00d4ff;">Starting sun scan...</span>';
                    document.getElementById('ssImageContainer').innerHTML = '';
                    if (!ssPollTimer) ssPollTimer = setInterval(pollSunScan, 2000);
                } else {
                    document.getElementById('ssStatus').innerHTML = '<span style="color:#ff4757;">Error: ' + (data.error || 'Unknown') + '</span>';
                }
            });
        }

        function stopSunScan() {
            fetch('/api/sunscan/stop', {method: 'POST'}).then(r => r.json()).then(data => {
                document.getElementById('ssStatus').innerHTML = '<span style="color:#ff4757;">Scan stopped.</span>';
            });
        }

        function pollSunScan() {
            fetch('/api/sunscan/status').then(r => r.json()).then(data => {
                if (data.running) {
                    // The scan may have been started by the schedule, by the
                    // calibration day, or before this page was loaded, so the
                    // poll starts its own timer rather than relying on the
                    // Start button having done it.
                    if (!ssPollTimer) ssPollTimer = setInterval(pollSunScan, 2000);
                    document.getElementById('ssStartBtn').style.display = 'none';
                    document.getElementById('ssStopBtn').style.display = 'inline-block';
                    document.getElementById('ssProgress').style.display = 'block';
                    const pct = data.total > 0 ? (data.progress / data.total * 100) : 0;
                    document.getElementById('ssProgressBar').style.width = pct + '%';
                    document.getElementById('ssProgressText').textContent = data.progress + ' / ' + data.total + ' grid points';
                    let info = '<span style="color:#00d4ff;">Scanning...</span>';
                    if (data.point_info) {
                        info += '<br><span style="color:#ccc; font-size:13px;">'
                            + 'Point ' + data.point_info.point + '/' + data.point_info.total
                            + ' &mdash; offset (' + data.point_info.dalt.toFixed(1) + ', ' + data.point_info.daz_sky.toFixed(1) + ')&deg;'
                            + ' &rarr; Alt=' + data.point_info.cmd_alt.toFixed(1) + '&deg; Az=' + data.point_info.cmd_az.toFixed(1) + '&deg;'
                            + '</span>';
                    }
                    document.getElementById('ssStatus').innerHTML = info;
                } else {
                    // Scan finished or idle
                    document.getElementById('ssStartBtn').style.display = 'inline-block';
                    document.getElementById('ssStopBtn').style.display = 'none';
                    if (ssPollTimer) { clearInterval(ssPollTimer); ssPollTimer = null; }

                    if (data.error) {
                        document.getElementById('ssStatus').innerHTML = '<span style="color:#ff4757;">Error: ' + data.error + '</span>';
                        document.getElementById('ssProgress').style.display = 'none';
                    } else if (data.result) {
                        const r = data.result;
                        document.getElementById('ssProgress').style.display = 'none';
                        document.getElementById('ssStatus').innerHTML = '<span style="color:#00ff88;">Scan complete!</span>';
                        let html = '<table style="width:100%; font-size:13px; color:#ccc;">';
                        html += '<tr><td style="color:#888; padding:4px 8px;">Sun Position</td><td>Alt ' + r.sun_alt_deg.toFixed(2) + '&deg; &nbsp; Az ' + r.sun_az_deg.toFixed(2) + '&deg;</td></tr>';
                        html += '<tr><td style="color:#888; padding:4px 8px;">Pointing Error</td><td style="color:#00d4ff; font-size:16px; font-weight:bold;">&Delta;Alt = ' + (r.alt_error_deg >= 0 ? '+' : '') + r.alt_error_deg.toFixed(3) + '&deg; &nbsp; &Delta;Az = ' + (r.az_error_deg >= 0 ? '+' : '') + r.az_error_deg.toFixed(3) + '&deg;</td></tr>';
                        html += '<tr><td style="color:#888; padding:4px 8px;">Beam FWHM</td><td>' + r.beam_fwhm_deg.toFixed(2) + '&deg;</td></tr>';
                        html += '<tr><td style="color:#888; padding:4px 8px;">Fit Success</td><td>' + (r.fit.success ? '<span style="color:#00ff88;">Yes</span>' : '<span style="color:#ff4757;">No (peak pixel fallback)</span>') + '</td></tr>';
                        if (r.fit.fit_errors) {
                            html += '<tr><td style="color:#888; padding:4px 8px;">Fit Uncertainty</td><td>&plusmn;' + r.fit.fit_errors.alt_err.toFixed(3) + '&deg; alt, &plusmn;' + r.fit.fit_errors.az_err.toFixed(3) + '&deg; az</td></tr>';
                        }
                        html += '<tr><td style="color:#888; padding:4px 8px;">Grid</td><td>' + r.n + '&times;' + r.n + ' @ ' + r.grid_spacing_deg + '&deg; spacing</td></tr>';
                        html += '<tr><td style="color:#888; padding:4px 8px;">Integration</td><td>' + r.integration_time_s + 's per point</td></tr>';
                        html += '<tr><td style="color:#888; padding:4px 8px;">Timestamp</td><td>' + r.timestamp + '</td></tr>';
                        html += '</table>';
                        document.getElementById('ssResults').innerHTML = html;

                        if (data.has_image) {
                            document.getElementById('ssImageContainer').innerHTML =
                                '<img src="/api/sunscan/image?' + Date.now() + '" style="max-width:100%; border-radius:8px; border:1px solid #333; margin-top:10px;">';
                        }
                    } else {
                        document.getElementById('ssStatus').innerHTML = '<span style="color:#888;">Idle &mdash; configure parameters and click Start.</span>';
                    }
                }
            });
        }

        // ---- Calibration Day ----
        let cdPollTimer = null;
        // Number of scans on file, from /api/calday/data. Held here rather
        // than written straight into cdStatus: loadCalModel() and pollCalDay()
        // are both in flight when the tab opens, and whichever landed last
        // used to win — which is how a running calibration day could be
        // reported as "Idle".
        let cdArchiveCount = null;

        function startCalDay() {
            const params = {
                n: parseInt(document.getElementById('ssGridN').value),
                grid_spacing_deg: parseFloat(document.getElementById('ssSpacing').value),
                integration_time_s: parseFloat(document.getElementById('ssIntegration').value),
                center_freq_mhz: parseFloat(document.getElementById('ssCenterFreq').value),
                bandwidth_mhz: parseFloat(document.getElementById('ssBandwidth').value),
                gain_db: parseFloat(document.getElementById('ssGain').value),
                sdr_type: document.getElementById('ssSdrType').value,
                beam_fwhm_deg: parseFloat(document.getElementById('ssBeamFwhm').value),
                interval_minutes: parseInt(document.getElementById('cdInterval').value),
            };
            fetch('/api/calday/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(params)
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    document.getElementById('cdStartBtn').style.display = 'none';
                    document.getElementById('cdStopBtn').style.display = 'inline-block';
                    cdPollTimer = setInterval(pollCalDay, 3000);
                    pollCalDay();
                } else {
                    document.getElementById('cdStatus').innerHTML = '<span style="color:#ff4757;">Error: ' + (data.error || 'Unknown') + '</span>';
                }
            }).catch(e => {
                document.getElementById('cdStatus').innerHTML = '<span style="color:#ff4757;">Could not start calibration: ' + e + '</span>';
            });
        }

        function stopCalDay() {
            fetch('/api/calday/stop', {method: 'POST'}).then(() => pollCalDay()).catch(e => {
                document.getElementById('cdStatus').innerHTML = '<span style="color:#ff4757;">Stop request failed: ' + e + '</span>';
            });
        }

        function pollCalDay() {
            fetch('/api/calday/status').then(r => r.json()).then(data => {
                if (data.running) {
                    // A calibration day outlives this page — it can be started
                    // by the schedule, and it runs for a whole day across any
                    // number of browser reloads. Start the timer from here so
                    // the display follows the run rather than the button press.
                    if (!cdPollTimer) cdPollTimer = setInterval(pollCalDay, 3000);
                    document.getElementById('cdStartBtn').style.display = 'none';
                    document.getElementById('cdStopBtn').style.display = 'inline-block';
                    let info = '<span style="color:#00d4ff;">Running</span> &mdash; ';
                    info += data.scans_completed + ' scans completed';
                    if (data.phase === 'waiting_for_sunrise') {
                        info += '<br><span style="color:#ffaa00;">Waiting for the Sun to reach 5&deg; altitude</span>';
                    } else if (data.phase === 'waiting_for_clear_horizon') {
                        info += '<br><span style="color:#ffaa00;">Sun is behind the obstructed horizon; waiting for it to clear</span>';
                    } else if (data.phase === 'homing') {
                        info += '<br><span style="color:#ffaa00;">Running physical homing sequence before scan</span>';
                    } else if (data.phase === 'retrying') {
                        info += '<br><span style="color:#ffaa00;">Re-homing and automatically retrying rejected scan</span>';
                    } else if (data.scan_running) {
                        info += '<br><span style="color:#ccc;">Scan in progress (' + data.scan_progress + '/' + data.scan_total + ' points)</span>';
                    } else if (data.next_scan_time) {
                        const next = new Date(data.next_scan_time).toLocaleTimeString();
                        info += '<br><span style="color:#888;">Next scan at ' + next + '</span>';
                    }
                    if (data.last_scan_error) {
                        if (data.consecutive_failures === 0 && (data.phase === 'homing' || data.phase === 'retrying')) {
                            info += '<br><span style="color:#ff9500;">Previous attempt rejected; automatic retry active: ' + data.last_scan_error + '</span>';
                        } else {
                            info += '<br><span style="color:#ff9500;">Last scan failed (' + data.consecutive_failures + '/3): ' + data.last_scan_error + '</span>';
                        }
                    }
                    document.getElementById('cdStatus').innerHTML = info;
                } else {
                    document.getElementById('cdStartBtn').style.display = 'inline-block';
                    document.getElementById('cdStopBtn').style.display = 'none';
                    if (cdPollTimer) { clearInterval(cdPollTimer); cdPollTimer = null; }
                    let info = '<span style="color:#888;">Idle</span>';
                    if (cdArchiveCount) {
                        info += ' &mdash; ' + cdArchiveCount + ' scans on file';
                    } else if (data.scans_completed > 0) {
                        info += ' &mdash; ' + data.scans_completed + ' scans collected';
                    }
                    if (data.error) {
                        info += '<br><span style="color:#ff4757;">' + data.error + '</span>';
                    } else if (data.phase === 'complete') {
                        info += '<br><span style="color:#00ff88;">Completed at sunset.</span>';
                    } else if (data.phase === 'stopped') {
                        info += '<br><span style="color:#ffaa00;">Stopped by user or schedule.</span>';
                    }
                    document.getElementById('cdStatus').innerHTML = info;
                }
            }).catch(e => {
                document.getElementById('cdStatus').innerHTML = '<span style="color:#ff4757;">Status request failed: ' + e + '</span>';
            });
        }

        function fitModel() {
            document.getElementById('cdModel').innerHTML = '<span style="color:#888;">Fitting model...</span>';
            fetch('/api/calday/fit', {method: 'POST'}).then(r => r.json()).then(m => {
                if (m.success) {
                    let html = '<table style="width:100%; font-size:13px; color:#ccc;">';
                    html += '<tr><td style="color:#888; padding:4px 8px;">Scans used</td><td>' + m.n_scans + '</td></tr>';
                    html += '<tr><td style="color:#888; padding:4px 8px;">Alt zero offset</td><td>' + (m.alt_offset_deg >= 0 ? '+' : '') + m.alt_offset_deg.toFixed(3) + '&deg;</td></tr>';
                    html += '<tr><td style="color:#888; padding:4px 8px;">Az zero offset</td><td>' + (m.az_offset_deg >= 0 ? '+' : '') + m.az_offset_deg.toFixed(3) + '&deg;</td></tr>';
                    html += '<tr><td style="color:#888; padding:4px 8px;">N-S tilt (AN)</td><td>' + (m.tilt_north_deg >= 0 ? '+' : '') + m.tilt_north_deg.toFixed(3) + '&deg;</td></tr>';
                    html += '<tr><td style="color:#888; padding:4px 8px;">E-W tilt (AE)</td><td>' + (m.tilt_east_deg >= 0 ? '+' : '') + m.tilt_east_deg.toFixed(3) + '&deg;</td></tr>';
                    html += '<tr><td style="color:#888; padding:4px 8px;">RMS residual</td><td>alt ' + m.rms_alt_deg.toFixed(3) + '&deg;, az ' + m.rms_az_deg.toFixed(3) + '&deg;</td></tr>';
                    html += '<tr><td style="color:#888; padding:4px 8px;">Sun azimuth coverage</td><td>' + m.az_coverage_deg.toFixed(1) + '&deg;</td></tr>';
                    html += '<tr><td style="color:#888; padding:4px 8px;">Fit condition</td><td>' + m.condition_number.toFixed(1) + '</td></tr>';
                    // How well the model places the beam, not the uncertainty on
                    // any one term. Those are ~-0.9 correlated with each other,
                    // so each is individually loose while the combination is
                    // tight: showing sigma(IA) here read as +-0.50 deg for a
                    // model whose cross-validated pointing error was 0.021 deg.
                    // The per-term errors stay in the API for diagnostics.
                    if (m.pointing_sigma_alt_deg !== undefined && m.pointing_sigma_alt_deg !== null) {
                        html += '<tr><td style="color:#888; padding:4px 8px;">Pointing uncertainty</td><td>&plusmn;' + m.pointing_sigma_alt_deg.toFixed(3) + '&deg; alt, &plusmn;' + m.pointing_sigma_xel_deg.toFixed(3) + '&deg; cross-el</td></tr>';
                    }
                    if (m.n_outliers) {
                        html += '<tr><td style="color:#888; padding:4px 8px;">Outliers rejected</td><td style="color:#ffa502;">' + m.n_outliers + ' (more than ' + m.outlier_sigma.toFixed(0) + '&sigma; from the model)</td></tr>';
                    }
                    if (m.n_superseded) {
                        html += '<tr><td style="color:#888; padding:4px 8px;">Older scans skipped</td><td style="color:#ffa502;">' + m.n_superseded + ' (recorded before the resident pointing model)</td></tr>';
                    }
                    if (m.n_obstructed) {
                        html += '<tr><td style="color:#888; padding:4px 8px;">Behind the horizon</td><td style="color:#ffa502;">' + m.n_obstructed + ' (Sun inside an obstruction sector)</td></tr>';
                    }
                    html += '</table>';
                    document.getElementById('cdModel').innerHTML = html;
                    document.getElementById('cdApplyBtn').style.display = 'inline-block';
                    // Show plot
                    document.getElementById('cdPlotContainer').innerHTML =
                        '<img src="/api/calday/plot?' + Date.now() + '" style="max-width:100%; border-radius:8px; border:1px solid #333; margin-top:10px;">';
                } else {
                    document.getElementById('cdModel').innerHTML = '<span style="color:#ff4757;">' + (m.error || 'Fit failed') + '</span>';
                }
            }).catch(e => {
                document.getElementById('cdModel').innerHTML = '<span style="color:#ff4757;">Model request failed: ' + e + '</span>';
            });
        }

        function clearCalData() {
            if (!confirm('Clear all accumulated pointing data, and erase the pointing model stored on the telescope controller?')) return;
            fetch('/api/calday/clear', {method: 'POST'}).then(r => r.json()).then(data => {
                if (!data.success) {
                    document.getElementById('cdStatus').innerHTML = '<span style="color:#ff4757;">Clear failed: ' + (data.error || 'Unknown error') + '</span>';
                    return;
                }
                document.getElementById('cdModel').innerHTML = '<span style="color:#888;">No model fitted yet.</span>';
                document.getElementById('cdApplyBtn').style.display = 'none';
                document.getElementById('cdPlotContainer').innerHTML = '';
                cdArchiveCount = 0;
                document.getElementById('cdStatus').innerHTML = '<span style="color:#888;">Data cleared.</span>';
            }).catch(e => {
                document.getElementById('cdStatus').innerHTML = '<span style="color:#ff4757;">Clear request failed: ' + e + '</span>';
            });
        }

        function applyModel() {
            if (!confirm('Store this pointing model on the telescope controller? It replaces the model currently in the controller flash and takes effect on the next slew.')) return;
            fetch('/api/calday/apply', {method: 'POST'}).then(r => r.json()).then(data => {
                if (data.success) {
                    var t = data.terms || {};
                    var lines = Object.keys(t).map(function (k) {
                        return k + ': ' + (t[k] >= 0 ? '+' : '') + t[k].toFixed(4) + '\u00b0';
                    });
                    alert('Stored on the controller.\\n\\n' + lines.join('\\n') +
                          '\\n\\nFitted from ' + data.n_scans + ' scans at ' + data.fitted_utc + '.');
                } else {
                    alert('Failed: ' + (data.error || 'Unknown error'));
                }
            }).catch(e => alert('Apply request failed: ' + e));
        }

        // Load existing model on tab open
        function loadCalModel() {
            fetch('/api/calday/model').then(r => r.json()).then(m => {
                if (m.success) {
                    fitModel();  // re-render
                }
            });
            fetch('/api/calday/data').then(r => r.json()).then(d => {
                if (d.data) {
                    cdArchiveCount = d.data.length;
                    // Re-render through pollCalDay, which knows whether a
                    // calibration day is running; writing "Idle" from here is
                    // what made a live run look stopped.
                    pollCalDay();
                }
            });
        }

        // ---- Configuration ----
        // The obstruction sectors are stored as [az_min, az_max, min_sun_alt]
        // triples and edited as "45-120:30, 300-330:12" — three numbers in a
        // box beat hand-written JSON in a box.
        function formatObstructionSectors(sectors) {
            if (!Array.isArray(sectors)) return '';
            return sectors.map(s => s[0] + '-' + s[1] + ':' + s[2]).join(', ');
        }

        function parseObstructionSectors(text) {
            const sectors = [];
            for (const part of text.split(',')) {
                const chunk = part.trim();
                if (!chunk) continue;
                // Character classes spelled out: this whole page is a Python
                // string, where a backslash escape would be Python's first.
                const m = chunk.match(/^(-?[0-9.]+) *- *(-?[0-9.]+) *: *(-?[0-9.]+)$/);
                if (!m) return null;
                const values = [parseFloat(m[1]), parseFloat(m[2]), parseFloat(m[3])];
                if (values.some(v => !isFinite(v))) return null;
                sectors.push(values);
            }
            return sectors;
        }

        function loadConfig() {
            fetch('/api/config').then(r => r.json()).then(cfg => {
                document.getElementById('cfgBannerName').value = cfg.banner_name || '';
                document.getElementById('cfgBannerSubtitle').value = cfg.banner_subtitle || '';
                document.getElementById('cfgControllerUrl').value = cfg.srt_controller_url || '';
                document.getElementById('cfgSlewTimeout').value = cfg.slew_timeout || 300;
                document.getElementById('cfgPositionTolerance').value = cfg.position_tolerance || 0.5;
                document.getElementById('cfgObsLat').value = cfg.observer_lat ?? 55.9;
                document.getElementById('cfgObsLon').value = cfg.observer_lon ?? -4.3;
                document.getElementById('cfgObsElev').value = cfg.observer_elevation ?? 50;
                document.getElementById('cfgMinElev').value = cfg.min_elevation ?? 10;
                document.getElementById('cfgObstructionSectors').value =
                    formatObstructionSectors(cfg.obstruction_sectors);
                document.getElementById('cfgCameraDevice').value = cfg.camera_device || '';
                document.getElementById('cfgCameraResolution').value = cfg.camera_resolution || '';
                document.getElementById('cfgReceiverPythonPath').value = cfg.receiver_python_path || cfg.python_path || '';
                document.getElementById('cfgDataFolder').value = cfg.data_output_folder || '';
                document.getElementById('cfgLogLines').value = cfg.log_lines || 100;
                document.getElementById('cfgSoundEnabled').value = cfg.sound_enabled !== false ? 'true' : 'false';
                soundEnabled = cfg.sound_enabled !== false;
            });
        }

        function saveConfig() {
            // Refuse the whole save rather than store an empty mask: silently
            // dropping a typo here would let the next calibration day scan
            // straight through the trees.
            const sectors = parseObstructionSectors(
                document.getElementById('cfgObstructionSectors').value);
            if (sectors === null) {
                alert('Obstruction sectors must look like "45-120:30", separated by commas.');
                return;
            }
            const cfg = {
                banner_name: document.getElementById('cfgBannerName').value,
                banner_subtitle: document.getElementById('cfgBannerSubtitle').value,
                srt_controller_url: document.getElementById('cfgControllerUrl').value,
                slew_timeout: parseInt(document.getElementById('cfgSlewTimeout').value) || 300,
                position_tolerance: parseFloat(document.getElementById('cfgPositionTolerance').value) || 0.5,
                observer_lat: parseFloat(document.getElementById('cfgObsLat').value) || 0,
                observer_lon: parseFloat(document.getElementById('cfgObsLon').value) || 0,
                observer_elevation: parseFloat(document.getElementById('cfgObsElev').value) || 0,
                min_elevation: parseFloat(document.getElementById('cfgMinElev').value) || 10,
                obstruction_sectors: sectors,
                camera_device: document.getElementById('cfgCameraDevice').value,
                camera_resolution: document.getElementById('cfgCameraResolution').value,
                receiver_python_path: document.getElementById('cfgReceiverPythonPath').value,
                data_output_folder: document.getElementById('cfgDataFolder').value,
                log_lines: parseInt(document.getElementById('cfgLogLines').value) || 100,
                sound_enabled: document.getElementById('cfgSoundEnabled').value === 'true',
            };
            soundEnabled = cfg.sound_enabled;
            fetch('/api/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(cfg)
            }).then(r => r.json()).then(data => {
                if (data.success) {
                    const el = document.getElementById('configSaved');
                    el.style.display = 'inline';
                    setTimeout(() => el.style.display = 'none', 3000);
                    updateTelescope();
                } else {
                    alert('Error saving: ' + (data.error || 'Unknown'));
                }
            });
        }

        // ---- Log ----
        let logRefreshTimer = null;

        function loadLog() {
            fetch('/api/log').then(r => r.json()).then(data => {
                const el = document.getElementById('logContent');
                el.textContent = data.lines.join('\\n') || '(empty log)';
                el.scrollTop = el.scrollHeight;
            }).catch(() => {
                document.getElementById('logContent').textContent = 'Error loading log';
            });
        }

        function toggleLogRefresh() {
            if (document.getElementById('logAutoRefresh').checked) {
                logRefreshTimer = setInterval(loadLog, 5000);
            } else {
                clearInterval(logRefreshTimer);
                logRefreshTimer = null;
            }
        }

        // Start log auto-refresh
        logRefreshTimer = setInterval(loadLog, 5000);
    </script>
</body>
</html>
'''


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
                          save_scan_to_pointing_data, sun_is_obstructed)

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
            sectors = parse_obstruction_sectors(cfg.get("obstruction_sectors"))
            if sun_is_obstructed(sun_alt, sun_az, sectors):
                log.info("Calibration day: Sun at alt=%.1f° az=%.1f° is behind a "
                         "configured obstruction; waiting for it to clear",
                         sun_alt, sun_az)
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
    cfg = load_config()
    return render_template_string(HTML_TEMPLATE,
        banner_name=cfg.get('banner_name', _DEFAULT_CONFIG['banner_name']),
        banner_subtitle=cfg.get('banner_subtitle', _DEFAULT_CONFIG['banner_subtitle']))


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

    out = os.path.join(_SCRIPT_DIR, "data",
                       "rf_%s_%s.h5" % (name.lower().replace(" ", "_"),
                                        datetime.now().strftime("%Y%m%d_%H%M%S")))
    os.makedirs(os.path.dirname(out), exist_ok=True)

    env = os.environ.copy()
    env["H1_OUTPUT_FILE"] = out
    env["H1_CENTER_FREQ"] = str(1420.405752e6)
    env["H1_FFT_SIZE"] = str(channels)
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
        [python_exe, RECEIVER_SCRIPT, "--sdr", sdr_type, "--headless",
         "--sample-rate", str(bandwidth_mhz * 1e6), "--gain", "40"],
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
                    "emptier - the Lockman Hole, l=150 b=53, runs 1.3 K."
                    % (glon, glat, emission["outside_mask_k"]))
            path = _rf_observe("Bandpass template", float(glon), float(glat),
                               duration_s, sdr_type, slew=False)
            rf_state["stage"] = "fitting the response"
            template, out = bandpass.fit_from_observation(
                path, "l=%.0f b=%+.0f" % (glon, glat))
            rf_state["result"] = {
                "kind": "bandpass",
                "degree": template["degree"],
                "band_mhz": template["u_scale_hz"] / 1e6,
                "residual_pct": 100 * template["fit_residual_rms"],
                "channels": template["n_channels_fitted"],
                "file": os.path.basename(path),
                "stored": os.path.basename(out),
                "glon": float(glon), "glat": float(glat),
                "alt_deg": status.get("alt"),
            }
            log.info("RF calibration: bandpass template refitted, residual %.3f%%",
                     100 * template["fit_residual_rms"])

        elif job == "gain":
            cfg = load_config()
            if params.get("glon") is not None:
                # Chosen by the operator, and taken as given even if it is low
                # or faint: they can see the skyline, and this cannot. The
                # obstruction sectors know only about the eastern treeline, so
                # an automatic choice once landed straight on a dome tower.
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
            else:
                rf_state["stage"] = "choosing a pointing"
                target = rf_calibration.calibration_target_now(
                    lat=float(cfg.get("observer_lat", SITE_LAT_DEG)),
                    lon=float(cfg.get("observer_lon", SITE_LON_DEG)),
                    elevation_m=float(cfg.get("observer_elevation", 50)),
                    obstruction_sectors=cfg.get("obstruction_sectors"))
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
            "t_sys_implausible": cal.get("t_sys_implausible"),
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

    A list rather than a choice. The software does not know the skyline - the
    obstruction sectors describe the eastern treeline and nothing else, and the
    dome towers are not in them - so on 2026-08-24 the best-scoring direction
    came out at azimuth 15, straight into a tower. The operator knows what is
    there; this only knows what is bright and how high it is.
    """
    import rf_calibration
    cfg = load_config()
    try:
        import observatory
        targets = rf_calibration.calibration_candidates_now(
            lat=float(cfg.get("observer_lat", SITE_LAT_DEG)),
            lon=float(cfg.get("observer_lon", SITE_LON_DEG)),
            elevation_m=float(cfg.get("observer_elevation", 50)),
            obstruction_sectors=cfg.get("obstruction_sectors"))
    except Exception as exc:                              # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500
    return jsonify({"success": True, "targets": targets,
                    "beam_fwhm_deg": observatory.beam_fwhm_deg(),
                    "main_beam_efficiency": rf_calibration.MAIN_BEAM_EFFICIENCY})


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


@app.route('/api/observe/plot', methods=['GET'])
def api_observe_plot():
    """Render the last finished observation to a PNG.

    Drawn on demand rather than when the observation ends: the run may finish
    with nobody watching, and a plot nobody asked for is one more thing to keep
    in step with the file.
    """
    import observation_plot
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


@app.route('/api/schedule', methods=['POST'])
def post_schedule():
    schedule = request.json
    # Server-side clash validation
    clashes = find_clashes(schedule)
    if clashes:
        return jsonify({'success': False, 'error': f'Schedule has clashing observations: {clashes}'}), 400
    save_schedule(schedule)
    return jsonify({'success': True})


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
    obs = request.json
    success = start_observation(obs)
    return jsonify({'success': success, 'error': None if success else 'Failed to start or already running'})


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
    if frame not in ('radec', 'galactic'):
        return jsonify({'success': False, 'error': f"Unknown frame '{frame}'"}), 400
    try:
        coord1 = float(request.args.get('coord1'))
        coord2 = float(request.args.get('coord2'))
        date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
        when = datetime.strptime(f"{date_str} {request.args.get('time', '')}",
                                 '%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return jsonify({'success': False,
                        'error': 'Need coord1, coord2 and time=HH:MM (local)'}), 400

    try:
        alt, az = compute_drift_pointing(frame, coord1, coord2, when)

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
    sun_scan_cancel.clear()
    sun_scan_thread = threading.Thread(target=_run_sun_scan, args=(params,),
                                       daemon=True)
    sun_scan_thread.start()
    return jsonify({'success': True})


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
            obstruction_sectors=cfg.get("obstruction_sectors"),
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
    """The stored profile, summarised - the raw cuts are far too big for the UI."""
    from horizon_scan import load_horizon_profile, profile_floors
    profile = load_horizon_profile()
    if not profile:
        return jsonify({'success': False, 'error': 'No horizon profile measured yet'})
    entries = profile.get("entries", [])
    return jsonify({
        'success': True,
        'measured_utc': profile.get("finished_utc"),
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
        'control_azimuths': profile.get("control_azimuths", []),
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
