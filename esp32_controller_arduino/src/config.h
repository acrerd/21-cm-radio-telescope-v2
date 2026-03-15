// config.h - Configuration settings for ESP32-S3 SRT Controller

#ifndef CONFIG_H
#define CONFIG_H

// WiFi Access Point settings
#define WIFI_AP_SSID "SRT_Controller"
#define WIFI_AP_PASSWORD "radio1420"

// WiFi connection timeout (seconds)
#define WIFI_CONNECT_TIMEOUT 15

// Serial connection to Arduino Due (UART1)
// ESP32-S3 Super Mini pinout
#define DUE_UART_TX 5   // ESP32-S3 GPIO -> Due RX (pin 19)
#define DUE_UART_RX 6   // ESP32-S3 GPIO <- Due TX (pin 18)
#define DUE_BAUD_RATE 115200

// Server ports
#define STELLARIUM_PORT 10001
#define WEB_PORT 80

// Observer location (Acre Road Observatory, Glasgow)
#define OBSERVER_LAT 55.902426
#define OBSERVER_LON -4.307865

// NTP server
#define NTP_SERVER "pool.ntp.org"

// Mount software limits (degrees)
#define MOUNT_AZ_MIN 2.0
#define MOUNT_AZ_MAX 353.0
#define MOUNT_ALT_MIN 0.0
#define MOUNT_ALT_MAX 90.0

// Home position (degrees)
#define HOME_ALT 0.0
#define HOME_AZ 180.0

// Position deadband (degrees)
#define POSITION_DEADBAND 0.25

// Credentials file path
#define WIFI_CREDS_FILE "/wifi_creds.txt"

#endif // CONFIG_H
