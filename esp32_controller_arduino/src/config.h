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

// NTP server. Deliberately a public pool address rather than a local host, to
// keep hard-coded site addresses out of the firmware; under a private point-to-
// point link the observatory computer routes this out to the internet.
#define NTP_SERVER "pool.ntp.org"
// Numeric fallback, tried when the name cannot be resolved. DNS resolution has
// been observed to fail on this controller while routing is perfectly healthy:
// pointed at this address the clock syncs in under six seconds, pointed at the
// name it never syncs at all. time.cloudflare.com anycast; not a site-local
// address, so it does not tie the firmware to this observatory.
#define NTP_SERVER_FALLBACK "162.159.200.123"

// Ethernet static-IP fallback, used only if DHCP is turned off in the web UI.
// The controller runs on DHCP and its address is pinned by MAC on the host, so
// these are never applied in normal operation — but they are what the Static IP
// form is pre-filled with, so they must name the link the controller is actually
// on. They were left at a 192.168.1.x factory placeholder, which meant one tick
// of that box would have moved the controller off the link entirely, reachable
// only over the WiFi AP.
#define DEFAULT_ETH_STATIC_IP "192.168.50.120"
#define DEFAULT_ETH_GATEWAY   "192.168.50.1"
#define DEFAULT_ETH_SUBNET    "255.255.255.0"
#define DEFAULT_ETH_DNS       "192.168.50.1"

// Clock discipline. The lwIP SNTP client polls in the background at this
// interval; nothing blocks waiting for it. Five hours of drift is itself
// harmless - the warning exists to catch a failed sync *mechanism* within an
// observing session rather than days later.
#define NTP_SYNC_INTERVAL_MS 3600000UL  // re-sync every hour
#define CLOCK_STALE_WARN_S   18000UL    // warn after 5 h with no successful sync
#define NTP_RETRY_INTERVAL_MS 300000UL  // retry every 5 min while never synced

// Mount software limits (degrees)
#define MOUNT_AZ_MIN 2.0
#define MOUNT_AZ_MAX 353.0
#define MOUNT_ALT_MIN 0.0
#define MOUNT_ALT_MAX 90.0

// Minimum TRUE altitude for a sky target to clear the local horizon and trees.
// This is a sky quantity and it belongs to the true frame: it is tested against
// the target's true altitude, before the pointing model converts it to drive
// coordinates. MOUNT_ALT_MIN above is a mechanical limit in the drive frame and
// is applied afterwards. The two used to be merged by taking whichever was
// larger, which quietly reinterpreted a mechanical stop as a sky horizon (and
// vice versa) and made neither testable on its own.
#define TRACKING_HORIZON_ALT 10.0

// Acquisition floor for the galactic-plane target (degrees). Where tracking is
// allowed to START; the target is then followed down as it sets, until the
// ordinary observing horizon above parks the dish. High on purpose - see
// getGalacticPlaneTrackingTarget() in coordinates.h.
#define GALACTIC_PLANE_MIN_ALT 45.0

// Stow position (degrees) - where the dish parks when idle or when its target
// sets. Zenith, facing south: straight up sheds rain and snow and presents the
// smallest profile to the wind.
//
// These are DRIVE coordinates, and they are the one sky-facing-looking setting
// that is not in the true frame. Parking is a mechanical act - "leave the mount
// here" - not an observation, so the pointing model is deliberately bypassed
// on the stow path. At the zenith that distinction is not academic: azimuth is
// degenerate there, every azimuth points at the same piece of sky, and the
// model's azimuth term carries tan(alt), so it asks for a 50-degree correction
// that would move the beam by exactly nothing. Through the model this position
// parks at drive azimuth 170; bypassing it, the mount rests where it is told.
//
// Still not to be confused with the Due's cfg.homeAlt/homeAz, which are also
// drive coordinates but mean the encoder origin - what the limit-switch stall
// corresponds to - rather than a place to park.
#define STOW_ALT 90.0
#define STOW_AZ 180.0

// Position deadband (degrees)
#define POSITION_DEADBAND 0.25

// Credentials file path
#define WIFI_CREDS_FILE "/wifi_creds.txt"

#endif // CONFIG_H
