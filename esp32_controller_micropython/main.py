# main.py - SRT Controller main application

import time
import ntptime
import _thread
from machine import RTC

from config import (
    DUE_UART_TX, DUE_UART_RX, DUE_BAUD_RATE,
    NTP_SERVER, OBSERVER_LAT, OBSERVER_LON,
    MOUNT_AZ_MIN, MOUNT_AZ_MAX, MOUNT_ALT_MIN, MOUNT_ALT_MAX,
    HOME_ALT, HOME_AZ, POSITION_DEADBAND
)
from coordinates import ra_dec_to_alt_az, get_sun_position, get_moon_position
from srt_serial import SRTSerial
import state  # Shared global state


def sync_time_ntp():
    """Sync time from NTP server"""
    try:
        ntptime.host = NTP_SERVER
        ntptime.settime()
        state.time_synced = True
        state.time_source = "NTP"
        print("NTP time synced")
        return True
    except Exception as e:
        print(f"NTP sync failed: {e}")
        return False


def set_time_from_timestamp(unix_timestamp):
    """Set RTC from Unix timestamp (seconds since 1970-01-01 UTC)

    Called by web server when browser sends its time.
    """
    try:
        # Convert Unix timestamp to time tuple
        # MicroPython's time epoch is 2000-01-01, so adjust
        # Unix epoch: 1970-01-01, MicroPython epoch: 2000-01-01
        # Difference: 946684800 seconds
        mp_timestamp = unix_timestamp - 946684800

        # Get time tuple from timestamp
        tm = time.gmtime(mp_timestamp)

        # Set RTC: (year, month, day, weekday, hours, minutes, seconds, subseconds)
        rtc = RTC()
        rtc.datetime((tm[0], tm[1], tm[2], tm[6], tm[3], tm[4], tm[5], 0))

        state.time_synced = True
        state.time_source = "browser"
        print(f"Time set from browser: {tm[0]}-{tm[1]:02d}-{tm[2]:02d} {tm[3]:02d}:{tm[4]:02d}:{tm[5]:02d} UTC")
        return True
    except Exception as e:
        print(f"Failed to set time: {e}")
        return False


def get_time_status():
    """Return current time status"""
    t = time.gmtime()
    return {
        "synced": state.time_synced,
        "source": state.time_source,
        "utc": f"{t[0]}-{t[1]:02d}-{t[2]:02d} {t[3]:02d}:{t[4]:02d}:{t[5]:02d}",
        "timestamp": time.time() + 946684800  # Convert to Unix timestamp
    }


def is_az_within_limits(az):
    """Check if azimuth is within software limits"""
    return MOUNT_AZ_MIN <= az <= MOUNT_AZ_MAX


def tracking_loop(srt):
    """Background thread: update Alt/Az from RA/Dec and send to Due

    Handles two waiting conditions:
    - Below horizon: parks at home, waits for target to rise
    - Azimuth wrap: waits for circumpolar target to reappear in limits
    """
    ephemeris_counter = 0
    last_sent_alt = None  # Track last sent position for deadband
    last_sent_az = None
    was_tracking = False  # Track state transitions

    status_request_counter = 0
    while True:
        # Read any available status from Due
        got_status = srt.read_status()

        # If no status received and it's been a while, request it
        if not got_status:
            status_request_counter += 1
            if status_request_counter >= 2:  # Request every 2 seconds if idle
                srt.request_status()
                status_request_counter = 0
        else:
            status_request_counter = 0

        if state.tracking_enabled:
            # Detect tracking just enabled - force immediate send
            if not was_tracking:
                last_sent_alt = None
                last_sent_az = None
                print("Tracking enabled - sending initial position")
            was_tracking = True
            # For Sun/Moon, refresh their positions periodically
            if state.target_name == "Sun":
                if ephemeris_counter == 0:
                    state.current_ra, state.current_dec = get_sun_position()
            elif state.target_name == "Moon":
                if ephemeris_counter == 0:
                    state.current_ra, state.current_dec = get_moon_position()

            # Update ephemeris every 30 seconds for Sun/Moon
            ephemeris_counter = (ephemeris_counter + 1) % 30

            # Convert current RA/Dec to Alt/Az
            alt, az = ra_dec_to_alt_az(
                state.current_ra, state.current_dec,
                OBSERVER_LAT, OBSERVER_LON
            )

            state.target_alt = alt
            state.target_az = az

            # Check if below horizon - go to home and wait
            if alt < MOUNT_ALT_MIN:
                if not state.waiting_for_rise:
                    state.waiting_for_rise = True
                    print(f"Target below horizon: Alt={alt:.1f}")
                    print("Parking at home, waiting for target to rise...")
                    srt.send_target(HOME_ALT, HOME_AZ)
                    last_sent_alt = HOME_ALT
                    last_sent_az = HOME_AZ
                # Continue waiting
            # Check if above zenith limit
            elif alt > MOUNT_ALT_MAX:
                print(f"Target above altitude limit: Alt={alt:.1f}")
            # Check azimuth limits (circumpolar wrap-around handling)
            elif not is_az_within_limits(az):
                if not state.waiting_for_wrap:
                    state.waiting_for_wrap = True
                    print(f"Target outside az limits: Az={az:.1f}")
                    print("Waiting for circumpolar wrap-around...")
                # Continue waiting
            else:
                # Target is within all limits
                if state.waiting_for_rise:
                    state.waiting_for_rise = False
                    print(f"Target risen: Alt={alt:.1f} Az={az:.1f}")
                    print("Resuming tracking...")
                    last_sent_alt = None  # Force send after state change
                    last_sent_az = None
                if state.waiting_for_wrap:
                    state.waiting_for_wrap = False
                    print(f"Target back in az limits: Alt={alt:.1f} Az={az:.1f}")
                    print("Repositioning to resume tracking...")
                    last_sent_alt = None  # Force send after state change
                    last_sent_az = None

                # Apply deadband - only send if moved more than threshold
                if (last_sent_alt is None or last_sent_az is None or
                    abs(alt - last_sent_alt) >= POSITION_DEADBAND or
                    abs(az - last_sent_az) >= POSITION_DEADBAND):
                    print(f"Tracking {state.target_name}: sending Alt={alt:.1f} Az={az:.1f}")
                    srt.send_target(alt, az)
                    last_sent_alt = alt
                    last_sent_az = az
        else:
            was_tracking = False

        time.sleep(1)


def main():
    print("SRT Controller starting...")

    # Only try NTP if we have a WiFi connection (STA mode)
    from wifi_manager import wifi
    if wifi.sta.isconnected():
        for i in range(3):
            if sync_time_ntp():
                break
            time.sleep(2)

    if not state.time_synced:
        print("NTP not synced - waiting for browser time sync")

    # Initialize serial to Due
    srt = SRTSerial(DUE_UART_TX, DUE_UART_RX, DUE_BAUD_RATE)

    # Start tracking thread
    _thread.start_new_thread(tracking_loop, (srt,))

    # Import and start servers (imported here to avoid circular import)
    from stellarium import start_stellarium_server
    from web_server import start_web_server

    # Start Stellarium server (runs in background)
    _thread.start_new_thread(start_stellarium_server, ())

    # Start web server (blocking - runs in main thread)
    start_web_server(srt)


if __name__ == "__main__":
    main()
