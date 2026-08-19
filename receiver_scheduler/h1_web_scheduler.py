#!/usr/bin/env python3
"""
Web-based scheduler interface for H1 Receiver.
Provides an interactive HTML/JavaScript UI for managing observation schedules.

Run with: python h1_web_scheduler.py
Then open: http://localhost:5000 on the scheduler host.
The SRT controller web UI is normally at http://192.168.106.120/.
"""

import json
import os
import shutil
import subprocess
import sys
import signal
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

# Suppress noisy Flask/werkzeug request logs
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# Configuration
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEDULE_FILE = os.path.join(_SCRIPT_DIR, "h1_schedule.json")
CONFIG_FILE = os.path.join(_SCRIPT_DIR, "scheduler_config.json")
RECEIVER_SCRIPT = os.path.join(_SCRIPT_DIR, "b210_h1_receiver.py")

# Default configuration - overridden by scheduler_config.json if present
_DEFAULT_CONFIG = {
    "banner_name": "H1 Receiver Scheduler",
    "banner_subtitle": "Hydrogen Line (21cm) Observation Manager",
    "srt_controller_url": "http://192.168.106.120",
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
    "observer_lat": 55.902444,
    "observer_lon": -4.307861,
    "observer_elevation": 50,
    "min_elevation": 10.0,
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


def srt_api_call(endpoint: str, params: Optional[dict] = None) -> Optional[dict]:
    """Make an API call to the SRT controller.

    Returns JSON response as dict, or None on error.
    """
    global SRT_CONTROLLER_URL

    candidates = _controller_url_candidates()
    if not candidates:
        return None

    last_error = None
    for base_url in candidates:
        url = f"{base_url}{endpoint}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                payload = response.read().decode(errors="replace")
                result = json.loads(payload, strict=False)
                with controller_settings_lock:
                    if base_url != SRT_CONTROLLER_URL:
                        log.info("SRT controller reachable at %s", base_url)
                        SRT_CONTROLLER_URL = base_url
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


def srt_wait_for_slew(timeout: Optional[int] = None,
                      cancel_event: Optional[threading.Event] = None) -> bool:
    """Wait for telescope to finish slewing.

    Polls /status until is_slewing is false, or timeout is reached.
    The is_slewing field is set by the mount when status contains 'Slewing'
    or a ' -> ' target indicator.

    Returns True if slew complete, False if timeout, error, or the
    cancel_event was set.
    """
    if not SRT_CONTROLLER_URL:
        return True

    timeout = timeout or SRT_SLEW_TIMEOUT
    start_time = time.time()

    log.info("SRT waiting for slew to complete...")

    # Brief delay to allow slew to begin before we start polling
    time.sleep(2)

    while time.time() - start_time < timeout:
        if cancel_event is not None and cancel_event.is_set():
            log.info("SRT slew wait aborted")
            return False
        status = srt_get_status()
        if status:
            is_slewing = status.get('is_slewing', False)
            current_alt = status.get('alt', 0)
            current_az = status.get('az', 0)

            if not is_slewing:
                elapsed = time.time() - start_time
                log.info("SRT slew complete in %.1fs - at Alt=%.2f° Az=%.2f°", elapsed, current_alt, current_az)
                return True

        time.sleep(2)  # Poll every 2 seconds

    log.warning("SRT timeout waiting for slew to complete")
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
    """Send telescope to a named position."""
    if not SRT_CONTROLLER_URL:
        return True
    if name == "home":
        result = srt_api_call("/home")
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
                log.info("Calibration: physical homing complete at Alt=%.2f° Az=%.2f°",
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
    global observation_starting, starting_observation_name

    # Calibration day: runs as a background thread, not a subprocess
    if obs.get('coord_system') == 'calibration':
        return _start_calibration_observation(obs, duration_override)

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
        if sun_scan_state["running"] or cal_day_state["running"]:
            log.info("Scheduled observation preempts the running Sun scan/calibration")
            sun_scan_cancel.set()
            cal_day_cancel.set()
            deadline = time.time() + SUN_SCAN_PREEMPT_TIMEOUT
            while sun_scan_state["running"] or cal_day_state["running"]:
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
            '--sdr', obs.get('sdr_type', 'b210'),
            '--gain', str(obs.get('gain_db', 40)),
            '--sample-rate', str(obs.get('bandwidth_mhz', 2.4) * 1e6),
        ]

        with process_lock:
            if start_abort.is_set():
                log.info("Observation start aborted")
                return False
            try:
                current_process = subprocess.Popen(
                    cmd, env=env,
                    cwd=os.path.abspath(os.path.join(_SCRIPT_DIR, "..")))
            except Exception as e:
                log.error("Error starting observation: %s", e)
                return False
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


def stop_observation() -> bool:
    """Stop current observation."""
    global current_process, current_observation, observation_end_time

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
        current_process = None
        current_observation = None
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
                    # A dead process while this slot is still due means the
                    # receiver exited early. Count it and clean up, so a
                    # crash-looping receiver is not respawned every 5 s
                    # (hammering the telescope with slews) for the rest of
                    # the slot.
                    if (current_observation is not None
                            and current_observation.get('name', '') == due_obs.get('name', '')):
                        _record_start_failure(due_obs, "receiver exited early")
                        stop_observation()
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
        .form-group input, .form-group select { padding: 8px; border: 1px solid #333; border-radius: 5px; background: #0f0f23; color: #fff; font-size: 14px; }
        .form-group input:focus, .form-group select:focus { outline: none; border-color: #00d4ff; }
        .form-group.wide { grid-column: span 2; }
        .coord-row { display: flex; gap: 5px; align-items: center; }
        .coord-row input { width: 60px; text-align: center; }
        .coord-row span { color: #888; font-size: 12px; }
        .schedule-list { margin-top: 20px; }
        .schedule-item { background: #0f0f23; border: 1px solid #333; border-radius: 8px; padding: 15px; margin-bottom: 10px; display: grid; grid-template-columns: auto 1fr auto; gap: 15px; align-items: center; }
        .schedule-item.disabled { opacity: 0.5; }
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
            <div class="tab" onclick="switchTab('config')">Configuration</div>
            <div class="tab" onclick="switchTab('log')">Log</div>
        </div>

        <div class="tab-content active" id="tab-scheduler">
            <div class="toolbar">
                <button class="btn btn-primary" onclick="openAddModal()">+ Add Observation</button>
                <button class="btn btn-secondary" onclick="saveSchedule()">Save Schedule</button>
                <button class="btn btn-secondary" onclick="document.getElementById('loadFile').click()">Load</button>
                <input type="file" id="loadFile" class="file-input" accept=".json" onchange="loadFile(event)">
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
                        <input type="number" id="ssGridN" min="3" max="15" step="2" value="5">
                    </div>
                    <div class="form-group">
                        <label>Grid Spacing (degrees)</label>
                        <input type="number" id="ssSpacing" min="0.1" max="10" step="0.1" value="1.5">
                    </div>
                    <div class="form-group">
                        <label>Beam FWHM Hint (degrees)</label>
                        <input type="number" id="ssBeamFwhm" min="0.5" max="20" step="0.1" value="3.0">
                    </div>
                    <div class="section-title">Receiver Settings</div>
                    <div class="form-group">
                        <label>Integration Time per Point (s)</label>
                        <input type="number" id="ssIntegration" min="0.1" max="60" step="0.1" value="3.0">
                    </div>
                    <div class="form-group">
                        <label>Center Frequency (MHz)</label>
                        <input type="number" id="ssCenterFreq" step="any" value="1420.405752">
                    </div>
                    <div class="form-group">
                        <label>Bandwidth (MHz)</label>
                        <input type="number" id="ssBandwidth" step="0.1" value="2.4">
                    </div>
                    <div class="form-group">
                        <label>Gain (dB)</label>
                        <input type="number" id="ssGain" min="0" max="80" value="40">
                    </div>
                    <div class="form-group">
                        <label>SDR Type</label>
                        <select id="ssSdrType">
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
                            <input type="number" id="cdInterval" min="5" max="120" value="30">
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

        <div class="tab-content" id="tab-config">
            <div class="config-form">
                <div class="section-title">Appearance</div>
                <div class="form-group">
                    <label>Banner Name</label>
                    <input type="text" id="cfgBannerName" placeholder="H1 Receiver Scheduler">
                </div>
                <div class="form-group">
                    <label>Banner Subtitle</label>
                    <input type="text" id="cfgBannerSubtitle" placeholder="Hydrogen Line (21cm) Observation Manager">
                </div>

                <div class="section-title">SRT Telescope Controller</div>
                <div class="form-group">
                    <label>Controller URL (leave empty to disable)</label>
                    <input type="text" id="cfgControllerUrl" placeholder="http://192.168.106.120">
                </div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Slew Timeout (seconds)</label>
                        <input type="number" id="cfgSlewTimeout" min="10" max="600">
                    </div>
                    <div class="form-group">
                        <label>Position Tolerance (degrees)</label>
                        <input type="number" id="cfgPositionTolerance" step="0.1" min="0.1" max="5">
                    </div>
                </div>

                <div class="section-title">Observer Location</div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Latitude (degrees, +N)</label>
                        <input type="number" id="cfgObsLat" step="0.001" min="-90" max="90">
                    </div>
                    <div class="form-group">
                        <label>Longitude (degrees, +E)</label>
                        <input type="number" id="cfgObsLon" step="0.001" min="-180" max="180">
                    </div>
                    <div class="form-group">
                        <label>Elevation (metres)</label>
                        <input type="number" id="cfgObsElev" step="1" min="0" max="9000">
                    </div>
                    <div class="form-group">
                        <label>Min Elevation for passes (degrees)</label>
                        <input type="number" id="cfgMinElev" step="1" min="0" max="90">
                    </div>
                </div>

                <div class="section-title">Receiver</div>
                <div class="form-group">
                    <label>Receiver Python Executable</label>
                    <input type="text" id="cfgReceiverPythonPath" placeholder="/home/astro/radioconda/bin/python">
                </div>

                <div class="section-title">Data Output</div>
                <div class="form-group">
                    <label>Output Folder</label>
                    <input type="text" id="cfgDataFolder" placeholder="Path to store observation data files">
                </div>

                <div class="section-title">Log</div>
                <div class="form-group">
                    <label>Log Lines to Display</label>
                    <input type="number" id="cfgLogLines" min="20" max="1000" step="10">
                </div>

                <div class="section-title">Notifications</div>
                <div class="form-group">
                    <label>Sound on Start/Stop</label>
                    <select id="cfgSoundEnabled">
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
                    <input type="checkbox" id="logAutoRefresh" onchange="toggleLogRefresh()" checked> Auto-refresh (5s)
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
                <input type="hidden" id="obsIndex" value="-1">

                <div class="form-grid">
                    <div class="form-group wide">
                        <label>Observation Name</label>
                        <input type="text" id="obsName" required placeholder="e.g., Galactic Center Survey">
                    </div>
                </div>

                <div class="section-title">Schedule <span style="font-weight:normal; font-size:11px; color:#888;">(Local Time)</span></div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Start Date</label>
                        <input type="date" id="obsStartDate" onchange="onCoordChange()">
                    </div>
                    <div class="form-group">
                        <label>Start Time</label>
                        <input type="time" id="obsStartTime" required onchange="updateEndTime()">
                    </div>
                    <div class="form-group">
                        <label>Duration (minutes)</label>
                        <input type="number" id="obsDuration" min="1" max="1440" required onchange="updateEndTime()">
                    </div>
                    <div class="form-group">
                        <label>End Time</label>
                        <input type="time" id="obsEndTime" disabled style="background:#2a2a4a; color:#aaa;">
                    </div>
                </div>
                <div id="clashWarning" style="display:none; color:#ff4757; background:#3a1a1a; padding:8px 12px; border-radius:5px; margin-top:5px; font-size:13px;"></div>

                <div class="section-title">Target Coordinates</div>
                <div class="coord-section">
                    <div class="form-grid">
                        <div class="form-group">
                            <label>Coordinate System</label>
                            <select id="obsCoordSystem" onchange="updateCoordLabels()">
                                <option value="altaz">Alt/Az (Horizontal)</option>
                                <option value="radec">RA/Dec (Equatorial J2000)</option>
                                <option value="galactic">Galactic (l, b)</option>
                                <option value="drift">Drift Scan (fixed pointing)</option>
                                <option value="object">Solar System Object</option>
                                <option value="satellite">Satellite (TLE)</option>
                                <option value="calibration">Calibration Day (Sun Scan)</option>
                            </select>
                        </div>
                    </div>
                    <div class="form-grid" id="objectSelector" style="margin-top:15px; display:none">
                        <div class="form-group">
                            <label>Object</label>
                            <select id="obsObjectName">
                                <option value="sun">Sun</option>
                                <option value="moon">Moon</option>
                            </select>
                        </div>
                    </div>
                    <div id="satelliteInput" style="margin-top:15px; display:none">
                        <div style="display:flex; gap:10px; align-items:flex-end; margin-bottom:10px;">
                            <div class="form-group" style="flex:1; margin:0;">
                                <label>Search CelesTrak by name or NORAD ID</label>
                                <input type="text" id="tleSearch" placeholder="e.g. ISS, NOAA 19, 25544" style="width:100%;">
                            </div>
                            <button class="btn btn-primary" type="button" onclick="fetchTle()" style="white-space:nowrap;">Fetch TLE</button>
                        </div>
                        <div id="tleResults" style="display:none; margin-bottom:10px;">
                            <div class="form-group">
                                <label>Select satellite</label>
                                <select id="tleResultSelect" onchange="selectTleResult()" style="width:100%;"></select>
                            </div>
                        </div>
                        <div class="form-group">
                            <label>TLE (paste, search above, or load from file)</label>
                            <textarea id="obsTleText" rows="4" style="width:100%; padding:8px; border:1px solid #333; border-radius:5px; background:#0f0f23; color:#fff; font-family:monospace; font-size:12px; resize:vertical;" placeholder="ISS (ZARYA)
1 25544U 98067A   ...
2 25544  51.6400  ..."></textarea>
                        </div>
                        <div style="display:flex; gap:10px; margin-top:8px; align-items:center; flex-wrap:wrap;">
                            <button class="btn btn-primary" type="button" onclick="predictPass()">Compute Next Pass</button>
                            <label class="btn btn-secondary" style="margin:0; cursor:pointer;">
                                Load TLE File <input type="file" accept=".tle,.txt" style="display:none" onchange="loadTleFile(event)">
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
                                <input type="number" id="obsCalGridN" min="3" max="15" step="2" value="5">
                            </div>
                            <div class="form-group">
                                <label>Grid Spacing (degrees)</label>
                                <input type="number" id="obsCalSpacing" min="0.1" max="10" step="0.1" value="1.5">
                            </div>
                            <div class="form-group">
                                <label>Scan Interval (minutes)</label>
                                <input type="number" id="obsCalInterval" min="5" max="120" value="30">
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
                                <select id="obsDriftFrame" onchange="updateCoordLabels()">
                                    <option value="radec">RA/Dec (J2000)</option>
                                    <option value="galactic">Galactic (l, b)</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Beam-Crossing Time T (local)</label>
                                <input type="time" id="obsDriftTime" onchange="updateDriftDerived()">
                            </div>
                            <div class="form-group">
                                <label>Window &plusmn; (minutes)</label>
                                <input type="number" id="obsDriftWindow" min="1" max="720" value="30" onchange="updateDriftDerived()">
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
                                <input type="number" id="coord1Deg" min="-90" max="360" value="45" onchange="onCoordChange()">
                                <span id="coord1Unit1">deg</span>
                                <input type="number" id="coord1Min" min="0" max="59" value="0" onchange="onCoordChange()">
                                <span>min</span>
                                <input type="number" id="coord1Sec" min="0" max="59.99" step="0.01" value="0" onchange="onCoordChange()">
                                <span>sec</span>
                            </div>
                        </div>
                        <div class="form-group">
                            <label id="coord2Label">Azimuth</label>
                            <div class="coord-row">
                                <input type="number" id="coord2Deg" min="-90" max="360" value="180" onchange="onCoordChange()">
                                <span>deg</span>
                                <input type="number" id="coord2Min" min="0" max="59" value="0" onchange="onCoordChange()">
                                <span>min</span>
                                <input type="number" id="coord2Sec" min="0" max="59.99" step="0.01" value="0" onchange="onCoordChange()">
                                <span>sec</span>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="section-title">Receiver Settings</div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Center Frequency (MHz)</label>
                        <input type="number" id="obsCenterFreq" step="any" required value="1420.405752">
                    </div>
                    <div class="form-group">
                        <label>Bandwidth (MHz)</label>
                        <input type="number" id="obsBandwidth" step="0.1" required value="2.4">
                    </div>
                    <div class="form-group">
                        <label>Gain (dB)</label>
                        <input type="number" id="obsGain" min="0" max="80" required value="40">
                    </div>
                    <div class="form-group">
                        <label>Channels (FFT)</label>
                        <select id="obsChannels">
                            <option value="1024">1024</option>
                            <option value="2048">2048</option>
                            <option value="4096" selected>4096</option>
                            <option value="8192">8192</option>
                            <option value="16384">16384</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Integration Time (s)</label>
                        <input type="number" id="obsIntegration" step="0.1" min="0.1" required value="3.0">
                    </div>
                    <div class="form-group">
                        <label>SDR Type</label>
                        <select id="obsSdrType">
                            <option value="b210">Ettus B210</option>
                            <option value="rtlsdr">RTL-SDR</option>
                            <option value="demo">Demo (Simulated)</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Calibrator (Noise Source)</label>
                        <select id="obsCalibrator">
                            <option value="off">Off</option>
                            <option value="on">On</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>When Done</label>
                        <select id="obsEndAction">
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
                        <input type="text" id="obsFilename" placeholder="auto-generated if empty">
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
            document.getElementById('objectSelector').style.display = isObject ? '' : 'none';
            document.getElementById('satelliteInput').style.display = isSat ? '' : 'none';
            document.getElementById('calibrationInput').style.display = isCal ? '' : 'none';
            document.getElementById('driftInput').style.display = isDrift ? '' : 'none';
            document.getElementById('coordInputs').style.display = (isObject || isSat || isCal) ? 'none' : '';
            // Drift scans derive start time and duration from T and the window
            document.getElementById('obsStartTime').disabled = isDrift;
            document.getElementById('obsDuration').disabled = isDrift;
            if (isObject || isSat || isCal) return;
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

        function renderSchedule() {
            const list = document.getElementById('scheduleList');
            if (schedule.length === 0) {
                list.innerHTML = '<div class="empty-state">No observations scheduled.</div>';
                return;
            }
            list.innerHTML = schedule.map((obs, i) => `
                <div class="schedule-item ${obs.enabled ? '' : 'disabled'} ${currentObs?.name === obs.name ? 'current-obs' : ''}">
                    <input type="checkbox" class="checkbox" ${obs.enabled ? 'checked' : ''} onchange="toggleEnabled(${i})">
                    <div class="schedule-info">
                        <div class="field"><div class="field-label">Name</div><div class="field-value">${obs.name}</div></div>
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
            `).join('');
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
            const now = new Date();
            const before = schedule.length;
            schedule = schedule.filter(obs => {
                const date = obs.start_date || localDateStr(now);
                const dur = obs.duration_minutes || 0;
                const start = new Date(`${date}T${obs.start_time || '00:00'}`);
                const end = new Date(start.getTime() + dur * 60000);
                return end > now;
            });
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
            if (name === 'sunscan') { pollSunScan(); loadCalModel(); }
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
                    ssPollTimer = setInterval(pollSunScan, 2000);
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
                    document.getElementById('cdStartBtn').style.display = 'none';
                    document.getElementById('cdStopBtn').style.display = 'inline-block';
                    let info = '<span style="color:#00d4ff;">Running</span> &mdash; ';
                    info += data.scans_completed + ' scans completed';
                    if (data.phase === 'waiting_for_sunrise') {
                        info += '<br><span style="color:#ffaa00;">Waiting for the Sun to reach 5&deg; altitude</span>';
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
                    if (data.scans_completed > 0) {
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
                    if (m.parameter_errors_deg) {
                        html += '<tr><td style="color:#888; padding:4px 8px;">Offset uncertainty</td><td>&plusmn;' + m.parameter_errors_deg.alt_offset.toFixed(3) + '&deg; alt, &plusmn;' + m.parameter_errors_deg.az_offset.toFixed(3) + '&deg; az</td></tr>';
                    }
                    if (m.effective_lat !== undefined) {
                        html += '<tr><td style="color:#888; padding:4px 8px;">Effective lat</td><td style="color:#00d4ff; font-weight:bold;">' + m.effective_lat.toFixed(6) + '&deg;</td></tr>';
                        html += '<tr><td style="color:#888; padding:4px 8px;">Effective lon</td><td style="color:#00d4ff; font-weight:bold;">' + m.effective_lon.toFixed(6) + '&deg;</td></tr>';
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
            if (!confirm('Clear all accumulated pointing data?')) return;
            fetch('/api/calday/clear', {method: 'POST'}).then(r => r.json()).then(data => {
                if (!data.success) {
                    document.getElementById('cdStatus').innerHTML = '<span style="color:#ff4757;">Clear failed: ' + (data.error || 'Unknown error') + '</span>';
                    return;
                }
                document.getElementById('cdModel').innerHTML = '<span style="color:#888;">No model fitted yet.</span>';
                document.getElementById('cdApplyBtn').style.display = 'none';
                document.getElementById('cdPlotContainer').innerHTML = '';
                document.getElementById('cdStatus').innerHTML = '<span style="color:#888;">Data cleared.</span>';
            }).catch(e => {
                document.getElementById('cdStatus').innerHTML = '<span style="color:#ff4757;">Clear request failed: ' + e + '</span>';
            });
        }

        function applyModel() {
            if (!confirm('Apply the effective lat/lon and constant alt/az offsets to the ESP32 controller, and update the scheduler location?')) return;
            fetch('/api/calday/apply', {method: 'POST'}).then(r => r.json()).then(data => {
                if (data.success) {
                    alert('Applied!\\nEffective lat: ' + data.effective_lat.toFixed(6) + '\\nEffective lon: ' + data.effective_lon.toFixed(6) + '\\nAlt offset: ' + data.alt_offset_deg.toFixed(4) + '\u00b0\\nAz offset: ' + data.az_offset_deg.toFixed(4) + '\u00b0');
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
                if (d.data && d.data.length > 0) {
                    document.getElementById('cdStatus').innerHTML = '<span style="color:#888;">Idle &mdash; ' + d.data.length + ' scans collected</span>';
                }
            });
        }

        // ---- Configuration ----
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
                document.getElementById('cfgReceiverPythonPath').value = cfg.receiver_python_path || cfg.python_path || '';
                document.getElementById('cfgDataFolder').value = cfg.data_output_folder || '';
                document.getElementById('cfgLogLines').value = cfg.log_lines || 100;
                document.getElementById('cfgSoundEnabled').value = cfg.sound_enabled !== false ? 'true' : 'false';
                soundEnabled = cfg.sound_enabled !== false;
            });
        }

        function saveConfig() {
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


def _run_calibration_day(params: dict):
    """Run repeated sun scans at a fixed interval until sunset or cancelled."""
    from sun_scan import get_sun_altaz, save_scan_to_pointing_data

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
        return jsonify({'success': True})
    except Exception as exc:
        log.error("Could not clear calibration data: %s", exc, exc_info=True)
        return jsonify({'success': False, 'error': str(exc)}), 500


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
        )
        if model.get("success"):
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

    eff_lat = model.get("effective_lat")
    eff_lon = model.get("effective_lon")
    if eff_lat is None or eff_lon is None:
        return jsonify({'success': False, 'error': 'Model has no effective lat/lon'})

    # Update ESP32 controller
    if not SRT_CONTROLLER_URL:
        return jsonify({'success': False,
                        'error': 'SRT controller URL is disabled; cannot apply the model'})

    alt_offset = model.get("alt_offset_deg")
    # The effective longitude rotates azimuth by a constant that is not part of
    # the fitted tilt, so push the offset that has that rotation removed.
    az_offset = model.get("az_offset_command_deg")
    if az_offset is None:
        return jsonify({
            'success': False,
            'error': ('Saved model predates the effective-longitude azimuth '
                      'correction; fit the model again before applying'),
        })
    if not all(isinstance(v, (int, float)) and math.isfinite(v)
               for v in (eff_lat, eff_lon, alt_offset, az_offset)):
        return jsonify({'success': False, 'error': 'Model contains invalid correction values'})
    if not -90.0 <= eff_lat <= 90.0 or not -180.0 <= eff_lon <= 180.0:
        return jsonify({'success': False,
                        'error': 'Model effective latitude/longitude is outside valid bounds'})

    result = srt_api_call("/settings/save", {
        "observer_lat": f"{eff_lat:.6f}",
        "observer_lon": f"{eff_lon:.6f}",
    })
    if not (result and result.get("ok")):
        return jsonify({'success': False,
                        'error': f'Failed to update ESP32 observer position: {result}'})

    offset_result = srt_api_call("/offset", {
        "alt": f"{alt_offset:.6f}",
        "az": f"{az_offset:.6f}",
    })
    if not (offset_result and offset_result.get("ok")):
        return jsonify({
            'success': False,
            'error': ('Observer position was updated, but pointing offsets failed; '
                      f'controller response: {offset_result}'),
            'partial': True,
        }), 502
    log.info("Applied effective lat=%.6f lon=%.6f and offsets alt=%+.4f az=%+.4f to ESP32",
             eff_lat, eff_lon, alt_offset, az_offset)

    # Update scheduler config too
    cfg = load_config()
    cfg["observer_lat"] = eff_lat
    cfg["observer_lon"] = eff_lon
    try:
        save_config(cfg)
    except Exception as exc:
        log.error("Controller updated but scheduler config save failed: %s", exc,
                  exc_info=True)
        return jsonify({
            'success': False,
            'error': f'Controller updated, but scheduler configuration could not be saved: {exc}',
            'partial': True,
        }), 500
    log.info("Updated scheduler config with effective lat/lon")

    return jsonify({
        'success': True,
        'effective_lat': eff_lat,
        'effective_lon': eff_lon,
        'alt_offset_deg': alt_offset,
        'az_offset_deg': az_offset,
    })


@app.route('/api/calday/model', methods=['GET'])
def api_calday_model():
    from sun_scan import load_pointing_model
    model = load_pointing_model()
    return jsonify(model or {'success': False, 'error': 'No model fitted yet'})


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
