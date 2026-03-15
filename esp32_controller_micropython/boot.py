# boot.py
import time
time.sleep(2)

from config import ETH_ENABLED

# Initialize Ethernet if enabled
if ETH_ENABLED:
    try:
        from ethernet import ethernet
        if ethernet.init():
            ethernet.connect(timeout=5)
    except Exception as e:
        print(f"Ethernet init error: {e}")

from wifi_manager import wifi
wifi.startup()

import main
main.main()
