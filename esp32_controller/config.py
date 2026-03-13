# config.py - Configuration settings for ESP32-S3

# WiFi Access Point settings
WIFI_AP_SSID = "SRT_Controller"
WIFI_AP_PASSWORD = "radio1420"

# Serial connection to Arduino Due (UART1)
# ESP32-S3 pins - adjust to match your wiring
# Avoid GPIO19/20 (USB), GPIO26-32 (not exposed on most boards)
DUE_UART_TX = 17  # ESP32-S3 GPIO pin -> Due RX
DUE_UART_RX = 18  # ESP32-S3 GPIO pin -> Due TX
DUE_BAUD_RATE = 115200

# Stellarium server
STELLARIUM_PORT = 10001

# Web server
WEB_PORT = 80

# Observer location (set to your location)
OBSERVER_LAT = 55.9    # Glasgow, degrees
OBSERVER_LON = -4.3    # Glasgow, degrees

# NTP server
NTP_SERVER = "pool.ntp.org"
