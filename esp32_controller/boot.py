# boot.py - Runs on ESP32-S3 startup before main.py

from wifi_manager import wifi

# Start WiFi - tries saved network, always enables AP for configuration
wifi.startup()
