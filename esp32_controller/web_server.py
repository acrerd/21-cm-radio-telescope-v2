# web_server.py - Web interface for SRT control

import socket
import json
import gc

from config import WEB_PORT, ETH_ENABLED
from wifi_manager import wifi
from coordinates import get_sun_position, get_moon_position, galactic_to_equatorial
import state  # Shared global state
# main imported lazily to avoid circular import during boot

# Import ethernet if enabled
if ETH_ENABLED:
    try:
        from ethernet import ethernet
    except ImportError:
        ethernet = None
else:
    ethernet = None


# HTML loaded from file to reduce memory during compilation
_html_cache = None

def get_html_page():
    """Load HTML from file, cached after first load"""
    global _html_cache
    if _html_cache is None:
        gc.collect()  # Free memory before loading large file
        with open('index.html', 'r') as f:
            _html_cache = f.read()
    return _html_cache


def url_decode(s):
    """Decode URL-encoded string"""
    result = s.replace('+', ' ')
    i = 0
    decoded = ''
    while i < len(result):
        if result[i] == '%' and i + 2 < len(result):
            try:
                decoded += chr(int(result[i+1:i+3], 16))
                i += 3
                continue
            except ValueError:
                pass
        decoded += result[i]
        i += 1
    return decoded


def parse_query_string(qs):
    """Parse a query string into a dict"""
    params = {}
    if qs:
        for pair in qs.split('&'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                params[k] = url_decode(v)
    return params


def start_web_server(srt):
    """Start the HTTP web server"""
    # Pre-load HTML and report memory
    gc.collect()
    print(f"Free memory before HTML load: {gc.mem_free()} bytes")
    get_html_page()  # Cache the HTML now
    gc.collect()
    print(f"Free memory after HTML load: {gc.mem_free()} bytes")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', WEB_PORT))
    server.listen(5)

    print(f"Web server listening on port {WEB_PORT}")

    while True:
        try:
            client, addr = server.accept()
            handle_http_request(client, srt)
        except Exception as e:
            print(f"Web server error: {e}")


def handle_http_request(client, srt):
    """Handle a single HTTP request"""
    try:
        request = client.recv(1024).decode()
        if not request:
            client.close()
            return

        # Parse request line
        lines = request.split('\r\n')
        if not lines:
            client.close()
            return

        parts = lines[0].split(' ')
        if len(parts) < 2:
            client.close()
            return

        path = parts[1]

        # Split path and query string
        if '?' in path:
            path, qs = path.split('?', 1)
        else:
            qs = ''
        params = parse_query_string(qs)

        # Route requests
        if path in ('/', '/index.html'):
            send_response(client, get_html_page(), 'text/html')

        elif path == '/docs':
            # Serve full documentation from help.html file
            try:
                with open('help.html', 'r') as f:
                    html = f.read()
                send_response(client, html, 'text/html')
            except Exception:
                send_response(client, 'Documentation not found', 'text/plain', 404)

        elif path == '/status':
            srt.read_status()
            status = srt.get_status_dict()
            send_response(client, json.dumps(status), 'application/json')

        elif path == '/tracking':
            # Return current tracking state
            result = {
                "enabled": state.tracking_enabled,
                "ra": state.current_ra,
                "dec": state.current_dec,
                "target_name": state.target_name,
                "waiting_for_wrap": state.waiting_for_wrap,
                "waiting_for_rise": state.waiting_for_rise
            }
            send_response(client, json.dumps(result), 'application/json')

        elif path == '/ephemeris':
            # Return Sun and Moon positions (RA/Dec and Alt/Az)
            from coordinates import ra_dec_to_alt_az
            from config import OBSERVER_LAT, OBSERVER_LON
            sun_ra, sun_dec = get_sun_position()
            sun_alt, sun_az = ra_dec_to_alt_az(sun_ra, sun_dec, OBSERVER_LAT, OBSERVER_LON)
            moon_ra, moon_dec = get_moon_position()
            moon_alt, moon_az = ra_dec_to_alt_az(moon_ra, moon_dec, OBSERVER_LAT, OBSERVER_LON)
            # Format with explicit precision to avoid json.dumps rounding
            result = (f'{{"sun":{{"ra":{sun_ra:.4f},"dec":{sun_dec:.2f},'
                      f'"alt":{sun_alt:.2f},"az":{sun_az:.2f}}},'
                      f'"moon":{{"ra":{moon_ra:.4f},"dec":{moon_dec:.2f},'
                      f'"alt":{moon_alt:.2f},"az":{moon_az:.2f}}}}}')
            send_response(client, result, 'application/json')

        elif path == '/goto':
            ra = float(params.get('ra', 0))
            dec = float(params.get('dec', 0))
            state.current_ra = ra
            state.current_dec = dec
            state.target_name = None
            state.waiting_for_wrap = False
            state.waiting_for_rise = False
            state.tracking_enabled = True
            send_response(client, '{"ok":true}', 'application/json')

        elif path == '/goto/galactic':
            l = float(params.get('l', 0))
            b = float(params.get('b', 0))
            ra, dec = galactic_to_equatorial(l, b)
            state.current_ra = ra
            state.current_dec = dec
            state.target_name = f"Gal l={l:.1f} b={b:.1f}"
            state.waiting_for_wrap = False
            state.waiting_for_rise = False
            state.tracking_enabled = True
            result = {"ok": True, "ra": ra, "dec": dec}
            send_response(client, json.dumps(result), 'application/json')

        elif path == '/track':
            enable = params.get('enable', '0') == '1'
            state.tracking_enabled = enable
            if not enable:
                state.target_name = None
                state.waiting_for_wrap = False
                state.waiting_for_rise = False
            send_response(client, '{"ok":true}', 'application/json')

        elif path == '/track/sun':
            ra, dec = get_sun_position()
            state.current_ra = ra
            state.current_dec = dec
            state.target_name = "Sun"
            state.waiting_for_wrap = False
            state.waiting_for_rise = False
            state.tracking_enabled = True
            send_response(client, '{"ok":true}', 'application/json')

        elif path == '/track/moon':
            ra, dec = get_moon_position()
            state.current_ra = ra
            state.current_dec = dec
            state.target_name = "Moon"
            state.waiting_for_wrap = False
            state.waiting_for_rise = False
            state.tracking_enabled = True
            send_response(client, '{"ok":true}', 'application/json')

        elif path == '/track/radec':
            ra = float(params.get('ra', 0))
            dec = float(params.get('dec', 0))
            state.current_ra = ra
            state.current_dec = dec
            state.target_name = None
            state.waiting_for_wrap = False
            state.waiting_for_rise = False
            state.tracking_enabled = True
            send_response(client, '{"ok":true}', 'application/json')

        elif path == '/track/galactic':
            l = float(params.get('l', 0))
            b = float(params.get('b', 0))
            ra, dec = galactic_to_equatorial(l, b)
            state.current_ra = ra
            state.current_dec = dec
            state.target_name = f"Gal l={l:.1f} b={b:.1f}"
            state.waiting_for_wrap = False
            state.waiting_for_rise = False
            state.tracking_enabled = True
            result = {"ok": True, "ra": ra, "dec": dec}
            send_response(client, json.dumps(result), 'application/json')

        # Time endpoints
        elif path == '/time/status':
            import main
            status = main.get_time_status()
            send_response(client, json.dumps(status), 'application/json')

        elif path == '/time/set':
            import main
            timestamp = int(params.get('timestamp', 0))
            if timestamp > 0:
                ok = main.set_time_from_timestamp(timestamp)
                result = {"ok": ok}
            else:
                result = {"ok": False, "error": "Invalid timestamp"}
            send_response(client, json.dumps(result), 'application/json')

        elif path == '/direct':
            alt = float(params.get('alt', 0))
            az = float(params.get('az', 0))
            state.target_alt = alt
            state.target_az = az
            state.tracking_enabled = False
            state.target_name = None
            state.waiting_for_wrap = False
            state.waiting_for_rise = False
            srt.send_target(alt, az)
            send_response(client, '{"ok":true}', 'application/json')

        # Ethernet endpoint
        elif path == '/eth/status':
            if ethernet:
                status = ethernet.get_status()
                status['enabled'] = ETH_ENABLED
            else:
                status = {"enabled": False, "available": False, "connected": False, "ip": None, "mac": None}
            send_response(client, json.dumps(status), 'application/json')

        # WiFi endpoints
        elif path == '/wifi/status':
            status = wifi.get_status()
            creds = wifi.load_credentials()
            status['saved_ssid'] = creds.get('ssid') if creds else None
            send_response(client, json.dumps(status), 'application/json')

        elif path == '/wifi/scan':
            networks = wifi.scan_networks()
            send_response(client, json.dumps(networks), 'application/json')

        elif path == '/wifi/connect':
            ssid = params.get('ssid', '')
            password = params.get('password', '')
            if wifi.connect_sta(ssid, password):
                wifi.save_credentials(ssid, password)
                result = {"ok": True, "ip": wifi.sta.ifconfig()[0]}
            else:
                result = {"ok": False, "error": "Connection failed"}
            send_response(client, json.dumps(result), 'application/json')

        elif path == '/wifi/forget':
            wifi.clear_credentials()
            send_response(client, '{"ok":true}', 'application/json')

        else:
            send_response(client, 'Not Found', 'text/plain', 404)

    except Exception as e:
        print(f"HTTP error: {e}")
    finally:
        client.close()


def send_response(client, body, content_type, status=200):
    """Send an HTTP response"""
    status_text = 'OK' if status == 200 else 'Not Found'
    response = f"HTTP/1.1 {status} {status_text}\r\n"
    # Add charset for text types
    if content_type.startswith('text/') or content_type == 'application/json':
        response += f"Content-Type: {content_type}; charset=utf-8\r\n"
    else:
        response += f"Content-Type: {content_type}\r\n"
    body_bytes = body.encode('utf-8')
    response += f"Content-Length: {len(body_bytes)}\r\n"
    response += "Connection: close\r\n"
    response += "\r\n"
    client.send(response.encode('utf-8'))
    client.send(body_bytes)
