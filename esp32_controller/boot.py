# boot.py - Runs on ESP32-S3 startup before main.py

from config import ETH_ENABLED
from wifi_manager import wifi

# Start WiFi AP (always available for configuration)
wifi.startup()

# Start Ethernet if enabled
if ETH_ENABLED:
    try:
        from ethernet import ethernet
        if ethernet.connect():
            print(f"Ethernet ready: {ethernet.get_ip()}")
        else:
            print("Ethernet: no link, continuing with WiFi only")
    except Exception as e:
        print(f"Ethernet init error: {e}")
