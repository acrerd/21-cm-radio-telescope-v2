# config.py - Configuration settings for ESP32-S3

# WiFi Access Point settings (always available for configuration)
WIFI_AP_SSID = "SRT_Controller"
WIFI_AP_PASSWORD = "radio1420"

# Serial connection to Arduino Due (UART1)
# ESP32-S3 pins - adjust to match your wiring
# Avoid GPIO19/20 (USB), GPIO26-32 (not exposed on most boards)
DUE_UART_TX = 17  # ESP32-S3 GPIO pin -> Due RX
DUE_UART_RX = 18  # ESP32-S3 GPIO pin -> Due TX
DUE_BAUD_RATE = 115200

# W5500 Ethernet (SPI connection)
ETH_ENABLED = True
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

# Observer location (set to your location)
OBSERVER_LAT = 55.9    # Glasgow, degrees
OBSERVER_LON = -4.3    # Glasgow, degrees

# NTP server
NTP_SERVER = "pool.ntp.org"
