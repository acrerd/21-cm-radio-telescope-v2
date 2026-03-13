# main.py - SRT Controller main application

import time
import ntptime
import _thread
from machine import UART, Pin

from config import (
    DUE_UART_TX, DUE_UART_RX, DUE_BAUD_RATE,
    NTP_SERVER, OBSERVER_LAT, OBSERVER_LON
)
from coordinates import ra_dec_to_alt_az, get_sun_position, get_moon_position
from web_server import start_web_server
from stellarium import start_stellarium_server
from srt_serial import SRTSerial

# Global state
current_ra = 0.0       # hours
current_dec = 0.0      # degrees
target_alt = 0.0       # degrees
target_az = 0.0        # degrees
tracking_enabled = False
target_name = None     # "Sun", "Moon", "Gal l=x b=y", or None for manual RA/Dec


def sync_time():
    """Sync time from NTP server"""
    try:
        ntptime.host = NTP_SERVER
        ntptime.settime()
        print("NTP time synced")
        return True
    except Exception as e:
        print(f"NTP sync failed: {e}")
        return False


def tracking_loop(srt):
    """Background thread: update Alt/Az from RA/Dec and send to Due"""
    global target_alt, target_az, current_ra, current_dec

    ephemeris_counter = 0

    while True:
        if tracking_enabled:
            # For Sun/Moon, refresh their positions periodically
            # (they move relative to stars)
            if target_name == "Sun":
                if ephemeris_counter == 0:
                    current_ra, current_dec = get_sun_position()
            elif target_name == "Moon":
                if ephemeris_counter == 0:
                    current_ra, current_dec = get_moon_position()

            # Update ephemeris every 30 seconds for Sun/Moon
            ephemeris_counter = (ephemeris_counter + 1) % 30

            # Convert current RA/Dec to Alt/Az
            alt, az = ra_dec_to_alt_az(
                current_ra, current_dec,
                OBSERVER_LAT, OBSERVER_LON
            )
            target_alt = alt
            target_az = az

            # Only send if above horizon
            if alt > 0:
                srt.send_target(alt, az)
            else:
                print(f"Target below horizon: Alt={alt:.1f}")

        time.sleep(1)


def main():
    global tracking_enabled

    print("SRT Controller starting...")

    # Sync time (retry a few times)
    for i in range(3):
        if sync_time():
            break
        time.sleep(2)

    # Initialize serial to Due
    srt = SRTSerial(DUE_UART_TX, DUE_UART_RX, DUE_BAUD_RATE)

    # Start tracking thread
    _thread.start_new_thread(tracking_loop, (srt,))

    # Start Stellarium server (runs in background)
    _thread.start_new_thread(start_stellarium_server, ())

    # Start web server (blocking - runs in main thread)
    start_web_server(srt)


if __name__ == "__main__":
    main()
