// config.h - Configuration settings for SRT Controller
// Supports ESP32-S3 and WT32-ETH01

#ifndef CONFIG_H
#define CONFIG_H

// WiFi Access Point settings
#define WIFI_AP_SSID "SRT_Controller"
#define WIFI_AP_PASSWORD "radio1420"

// Network discovery name. The controller should be reachable as
// http://srt-controller.local/ on networks that support mDNS.
#define CONTROLLER_HOSTNAME "srt-controller"

// Network OTA updates. After one serial flash with this firmware installed,
// routine WT32 updates can be uploaded over Ethernet.
#define OTA_ENABLED 1
#define OTA_PORT 3232
#define OTA_PASSWORD "srt-ota-1420"

// WiFi connection timeout (seconds)
#define WIFI_CONNECT_TIMEOUT 15

// =============================================================================
// Board-specific pin configuration
// =============================================================================

#ifdef BOARD_WT32_ETH01
    // WT32-ETH01: Serial to Due via GPIO4/14
    // Note: GPIO32/33 labelled CFG/485_EN on RS-485 variants - avoid those
    // TX0/RX0 (GPIO1/3) reserved for programming - no need to disconnect Due
    #define DUE_UART_TX 4    // WT32 IO4 -> Due RX (pin 19)
    #define DUE_UART_RX 14   // WT32 IO14 <- Due TX (pin 18)

    // Ethernet PHY configuration (LAN8720) - pin numbers only
    // The actual PHY type constants are defined by ETH.h
    #define ETH_PHY_ADDR_CFG    1
    #define ETH_PHY_MDC_PIN     23
    #define ETH_PHY_MDIO_PIN    18
    #define ETH_PHY_POWER_PIN   16

    // Enable Ethernet support
    #define ETHERNET_ENABLED 1
#else
    // ESP32-S3 Super Mini: Serial to Due via GPIO5/6
    #define DUE_UART_TX 5    // ESP32-S3 GPIO -> Due RX (pin 19)
    #define DUE_UART_RX 6    // ESP32-S3 GPIO <- Due TX (pin 18)

    // No Ethernet on ESP32-S3 Super Mini
    #define ETHERNET_ENABLED 0
#endif

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

// Minimum altitude for sky targets to clear the local horizon/trees.
// This is intentionally separate from the mechanical mount lower limit.
#define TRACKING_HORIZON_ALT 10.0

inline double effectiveTrackingHorizonAlt(double mountAltMin) {
    return mountAltMin > TRACKING_HORIZON_ALT ? mountAltMin : TRACKING_HORIZON_ALT;
}

// Home position (degrees)
#define HOME_ALT 0.0
#define HOME_AZ 0.0

// Position deadband (degrees)
#define POSITION_DEADBAND 0.25

// Credentials file path
#define WIFI_CREDS_FILE "/wifi_creds.txt"

#endif // CONFIG_H
