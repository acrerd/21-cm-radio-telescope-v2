# wifi_manager.py - WiFi connection management with credential storage

import network
import json
import time

from config import WIFI_AP_SSID, WIFI_AP_PASSWORD

CREDENTIALS_FILE = "wifi_creds.json"
CONNECT_TIMEOUT = 15  # seconds


class WiFiManager:
    """Manages WiFi connections - tries saved network first, falls back to AP mode"""

    def __init__(self):
        self.sta = network.WLAN(network.STA_IF)
        self.ap = network.WLAN(network.AP_IF)
        self.connected_ssid = None

    def load_credentials(self):
        """Load saved WiFi credentials from file"""
        try:
            with open(CREDENTIALS_FILE, 'r') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def save_credentials(self, ssid, password):
        """Save WiFi credentials to file"""
        creds = {"ssid": ssid, "password": password}
        with open(CREDENTIALS_FILE, 'w') as f:
            json.dump(creds, f)

    def clear_credentials(self):
        """Delete saved credentials"""
        try:
            import os
            os.remove(CREDENTIALS_FILE)
        except OSError:
            pass

    def connect_sta(self, ssid, password, timeout=CONNECT_TIMEOUT):
        """Connect to a WiFi network in station mode"""
        print(f"Connecting to WiFi: {ssid}")
        self.sta.active(True)
        self.sta.connect(ssid, password)

        start = time.time()
        while not self.sta.isconnected():
            if time.time() - start > timeout:
                print(f"WiFi connection timeout")
                self.sta.active(False)
                return False
            time.sleep(0.5)

        self.connected_ssid = ssid
        print(f"Connected to {ssid}")
        print(f"IP: {self.sta.ifconfig()[0]}")
        return True

    def start_ap(self):
        """Start soft access point"""
        self.ap.active(True)
        self.ap.config(
            essid=WIFI_AP_SSID,
            password=WIFI_AP_PASSWORD,
            authmode=network.AUTH_WPA2_PSK,
            channel=6
        )

        while not self.ap.active():
            time.sleep(0.1)

        print(f"AP active: {WIFI_AP_SSID}")
        print(f"AP IP: {self.ap.ifconfig()[0]}")

    def scan_networks(self):
        """Scan for available WiFi networks"""
        self.sta.active(True)
        try:
            networks = self.sta.scan()
            # Return list of (ssid, rssi, security) tuples
            result = []
            seen = set()
            for net in networks:
                ssid = net[0].decode('utf-8')
                if ssid and ssid not in seen:
                    seen.add(ssid)
                    result.append({
                        "ssid": ssid,
                        "rssi": net[3],
                        "secure": net[4] != 0
                    })
            # Sort by signal strength
            result.sort(key=lambda x: x["rssi"], reverse=True)
            return result
        except Exception as e:
            print(f"Scan error: {e}")
            return []

    def startup(self):
        """
        Startup sequence:
        1. Try to connect to saved network (STA mode)
        2. If that fails, start AP mode instead
        Note: AP+STA simultaneous mode doesn't work on this board
        """
        # Ensure AP is off initially
        self.ap.active(False)

        # Try saved credentials first
        creds = self.load_credentials()
        if creds:
            ssid = creds.get("ssid")
            password = creds.get("password")
            if ssid:
                if self.connect_sta(ssid, password):
                    print("Connected to WiFi - AP mode disabled")
                    return True

        # STA failed or no credentials - use AP mode
        self.sta.active(False)  # Disable STA before starting AP
        self.start_ap()
        print("AP mode active - connect to configure WiFi")
        return False

    def get_status(self):
        """Return current WiFi status"""
        sta_connected = self.sta.isconnected()
        return {
            "ap_active": self.ap.active(),
            "ap_ssid": WIFI_AP_SSID,
            "ap_ip": self.ap.ifconfig()[0] if self.ap.active() else None,
            "sta_connected": sta_connected,
            "sta_ssid": self.connected_ssid if sta_connected else None,
            "sta_ip": self.sta.ifconfig()[0] if sta_connected else None,
        }


# Global instance
wifi = WiFiManager()
