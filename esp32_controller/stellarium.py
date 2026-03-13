# stellarium.py - Stellarium Telescope Protocol server

import socket
import struct
import time
import _thread

from config import STELLARIUM_PORT, OBSERVER_LAT, OBSERVER_LON
from coordinates import alt_az_to_ra_dec
import main as app  # Access global state


def start_stellarium_server():
    """Start TCP server for Stellarium telescope protocol"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', STELLARIUM_PORT))
    server.listen(1)

    print(f"Stellarium server listening on port {STELLARIUM_PORT}")

    while True:
        try:
            client, addr = server.accept()
            print(f"Stellarium connected from {addr}")
            _thread.start_new_thread(handle_stellarium_client, (client,))
        except Exception as e:
            print(f"Stellarium accept error: {e}")
            time.sleep(1)


def handle_stellarium_client(client):
    """Handle a Stellarium client connection"""
    try:
        while True:
            # Try to read a command
            data = client.recv(20)
            if not data:
                break

            if len(data) >= 20:
                # Parse goto command
                msg_len, msg_type = struct.unpack('<HH', data[0:4])

                if msg_type == 0:  # Goto command
                    # Stellarium sends RA/Dec as unsigned 32-bit values
                    # RA: 0x00000000 to 0x100000000 = 0h to 24h
                    # Dec: 0x00000000 to 0x100000000 = -90° to +90° (with 0x80000000 = 0°)
                    ra_raw, dec_raw = struct.unpack('<II', data[12:20])

                    # Convert to hours and degrees
                    ra_hours = ra_raw * 24.0 / 0x100000000
                    dec_deg = (dec_raw * 180.0 / 0x100000000) - 90.0

                    print(f"Stellarium goto: RA={ra_hours:.4f}h, Dec={dec_deg:.4f}°")

                    # Update global target
                    app.current_ra = ra_hours
                    app.current_dec = dec_deg
                    app.tracking_enabled = True

            # Send current position back to Stellarium
            send_position_to_stellarium(client)

            time.sleep(0.1)

    except Exception as e:
        print(f"Stellarium client error: {e}")
    finally:
        client.close()
        print("Stellarium client disconnected")


def send_position_to_stellarium(client):
    """Send current telescope position to Stellarium"""
    # Convert current Alt/Az back to RA/Dec for Stellarium
    ra_hours, dec_deg = alt_az_to_ra_dec(
        app.target_alt, app.target_az,
        OBSERVER_LAT, OBSERVER_LON
    )

    # Convert to Stellarium format
    ra_raw = int((ra_hours / 24.0) * 0x100000000) & 0xFFFFFFFF
    dec_raw = int(((dec_deg + 90.0) / 180.0) * 0x100000000) & 0xFFFFFFFF

    # Build response message
    # Length: 24 bytes, Type: 0, Time: current microseconds, RA, Dec, Status
    current_time = time.ticks_us()
    msg = struct.pack('<HHQIIi',
        24,           # message length
        0,            # message type
        current_time, # timestamp (microseconds)
        ra_raw,       # RA
        dec_raw,      # Dec
        0             # status
    )

    try:
        client.send(msg)
    except Exception as e:
        print(f"Error sending to Stellarium: {e}")
