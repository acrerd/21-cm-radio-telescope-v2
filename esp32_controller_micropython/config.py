# config.py - Configuration settings for ESP32-S3

# WiFi Access Point settings (always available for configuration)
WIFI_AP_SSID = "SRT_Controller"
WIFI_AP_PASSWORD = "radio1420"

# Serial connection to Arduino Due (UART1)
# Avoid GPIO19/20 (USB), GPIO26-32 (not exposed on most boards)

# Standard ESP32-S3 dev board:
# DUE_UART_TX = 17  # ESP32-S3 GPIO pin -> Due RX
# DUE_UART_RX = 18  # ESP32-S3 GPIO pin -> Due TX

# ESP32-S3 Super Mini (GPIO 17/18 not exposed):
DUE_UART_TX = 5   # ESP32-S3 GPIO pin -> Due RX (pin 19)
DUE_UART_RX = 6   # ESP32-S3 GPIO pin <- Due TX (pin 18)

DUE_BAUD_RATE = 115200

# W5500 Ethernet (SPI connection)
# Disabled for ESP32-S3 Super Mini (WiFi only)
ETH_ENABLED = False
ETH_SPI_ID = 1        # SPI bus (1 or 2)
ETH_SCK = 12          # SPI clock
ETH_MOSI = 11         # SPI data out
ETH_MISO = 13         # SPI data in
ETH_CS = 10           # Chip select
ETH_RST = 9           # Reset pin (set to None if tied to 3.3V)

# Ethernet IP configuration
ETH_USE_DHCP = True   # True for DHCP, False for static IP
ETH_STATIC_IP = "192.168.1.100"
ETH_STATIC_MASK = "255.255.255.0"
ETH_STATIC_GW = "192.168.1.1"
ETH_STATIC_DNS = "8.8.8.8"

# Stellarium server
STELLARIUM_PORT = 10001

# Web server
WEB_PORT = 80

# Observer location (Acre Road Observatory, Glasgow)
OBSERVER_LAT = 55.902426   # Latitude (degrees)
OBSERVER_LON = -4.307865   # Longitude (degrees)

# NTP server
NTP_SERVER = "pool.ntp.org"

# Mount software limits (degrees)
# These should match the Arduino Due config (operational limits inside hardware)
MOUNT_AZ_MIN = 2.0     # Azimuth minimum (2 deg inside hardware limit)
MOUNT_AZ_MAX = 353.0   # Azimuth maximum (2 deg inside hardware limit)
MOUNT_ALT_MIN = 0.0    # Altitude minimum
MOUNT_ALT_MAX = 90.0   # Altitude maximum

# Home position (degrees) - telescope parks here when waiting
HOME_ALT = 0.0
HOME_AZ = 180.0

# Position deadband (degrees) - don't command moves smaller than this
# Prevents motor hunting when target is very close to current position
POSITION_DEADBAND = 0.25
