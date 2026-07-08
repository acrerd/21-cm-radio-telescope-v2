// main.cpp - SRT Controller main application
// Supports ESP32-S3 and WT32-ETH01

#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include "config.h"  // Need config.h early for feature flags and Ethernet config
#if OTA_ENABLED
#include <ArduinoOTA.h>
#endif
#include <time.h>

#if ETHERNET_ENABLED
#include <ETH.h>
#endif

// Debug print macros - only print if Serial is connected
#define DBG(x) if (Serial) { x; }
#include "settings.h"
#include "state.h"
#include "wifi_manager.h"
#include "srt_serial.h"
#include "web_server.h"
#include "stellarium.h"
#include "coordinates.h"

// External state
extern SRTState state;

bool mdnsRunning = false;
bool otaRunning = false;

void startDiscoveryServices() {
    if (mdnsRunning) {
        return;
    }

    if (!MDNS.begin(CONTROLLER_HOSTNAME)) {
        Serial.println("mDNS start failed");
        return;
    }

    MDNS.addService("http", "tcp", WEB_PORT);
    MDNS.addService("stellarium", "tcp", STELLARIUM_PORT);
    mdnsRunning = true;
    Serial.printf("mDNS: http://%s.local/\n", CONTROLLER_HOSTNAME);
}

void startOTAService() {
#if OTA_ENABLED
    if (otaRunning) {
        return;
    }

    ArduinoOTA
        .setPort(OTA_PORT)
        .setHostname(CONTROLLER_HOSTNAME)
        .setPassword(OTA_PASSWORD)
        .setMdnsEnabled(false)
        .onStart([]() {
            Serial.println("OTA update starting");
            srtSerial.logESP("OTA update starting");
            srtSerial.sendStop();
        })
        .onEnd([]() {
            Serial.println("OTA update complete");
            srtSerial.logESP("OTA update complete");
        })
        .onProgress([](unsigned int progress, unsigned int total) {
            static int lastPercent = -1;
            int percent = total ? (progress * 100 / total) : 0;
            if (percent != lastPercent && percent % 10 == 0) {
                Serial.printf("OTA progress: %d%%\n", percent);
                lastPercent = percent;
            }
        })
        .onError([](ota_error_t error) {
            Serial.printf("OTA error: %u\n", error);
            srtSerial.logESP("OTA update failed");
        });

    ArduinoOTA.begin();
    otaRunning = true;
    MDNS.enableArduino(OTA_PORT, true);
    Serial.printf("OTA ready: %s.local:%d\n", CONTROLLER_HOSTNAME, OTA_PORT);
#endif
}

// Tracking loop variables
unsigned long lastTrackingUpdate = 0;
unsigned long lastEphemerisUpdate = 0;
float lastSentAlt = -999;
float lastSentAz = -999;
bool wasTracking = false;

// Ethernet state (WT32-ETH01 only)
#if ETHERNET_ENABLED
bool ethConnected = false;
bool ethNeedNtpSync = false;  // Flag to trigger NTP sync from main loop
String ethIP = "";

void onEthEvent(arduino_event_id_t event) {
    switch (event) {
        case ARDUINO_EVENT_ETH_START:
            Serial.println("ETH Started");
            ETH.setHostname(CONTROLLER_HOSTNAME);
            break;
        case ARDUINO_EVENT_ETH_CONNECTED:
            Serial.println("ETH Link Up");
            break;
        case ARDUINO_EVENT_ETH_GOT_IP:
            ethConnected = true;
            ethIP = ETH.localIP().toString();
            Serial.printf("ETH IP: %s, Speed: %dMbps, %s\n",
                ethIP.c_str(),
                ETH.linkSpeed(),
                ETH.fullDuplex() ? "Full Duplex" : "Half Duplex");
            // Request NTP sync if time not already synced
            if (!state.timeSynced) {
                ethNeedNtpSync = true;
            }
            startDiscoveryServices();
            startOTAService();
            break;
        case ARDUINO_EVENT_ETH_DISCONNECTED:
            Serial.println("ETH Disconnected");
            ethConnected = false;
            ethIP = "";
            break;
        case ARDUINO_EVENT_ETH_STOP:
            Serial.println("ETH Stopped");
            ethConnected = false;
            ethIP = "";
            break;
        default:
            break;
    }
}
#endif

// NTP sync
void syncTimeNTP() {
    Serial.println("Syncing time with NTP...");
    configTime(0, 0, NTP_SERVER);

    // Wait for time sync (max 10 seconds)
    time_t now = 0;
    int attempts = 0;
    while (now < 1000000000 && attempts < 20) {
        delay(500);
        now = time(nullptr);
        attempts++;
    }

    if (now > 1000000000) {
        state.timeSynced = true;
        state.timeSource = "NTP";
        struct tm *t = gmtime(&now);
        Serial.printf("NTP time synced: %04d-%02d-%02d %02d:%02d:%02d UTC\n",
                      t->tm_year + 1900, t->tm_mon + 1, t->tm_mday,
                      t->tm_hour, t->tm_min, t->tm_sec);
        srtSerial.logESP("NTP time synced");
    } else {
        Serial.println("NTP sync failed");
        srtSerial.logESP("NTP sync failed");
    }
}

// Check if azimuth is within limits
bool isAzWithinLimits(float az) {
    return az >= settings.mountAzMin && az <= settings.mountAzMax;
}

// Tracking update - called from loop
void updateTracking() {
    unsigned long now = millis();

    // Only poll Due once per second
    if (now - lastTrackingUpdate < 1000) {
        return;
    }
    lastTrackingUpdate = now;

    // Read any available status from Due
    srtSerial.readStatus();

    // Request fresh status
    srtSerial.requestStatus();

    if (state.movementHoldUntil != 0) {
        if ((long)(now - state.movementHoldUntil) < 0) {
            return;
        }
        state.movementHoldUntil = 0;
        lastSentAlt = -999;
        lastSentAz = -999;
        srtSerial.logESP("Movement hold released");
    }

    if (state.trackingEnabled) {
        // Detect tracking just enabled - force immediate send
        if (!wasTracking) {
            lastSentAlt = -999;
            lastSentAz = -999;
            DBG(Serial.println("Tracking enabled - sending initial position"));
            srtSerial.logESP("Tracking enabled");
        }
        wasTracking = true;

        // For Sun/Moon, refresh positions every 30 seconds
        if (now - lastEphemerisUpdate >= 30000) {
            if (state.targetName == "Sun") {
                double ra, dec;
                getSunPosition(ra, dec);
                state.currentRA = ra;
                state.currentDec = dec;
            } else if (state.targetName == "Moon") {
                double ra, dec;
                getMoonPosition(ra, dec);
                state.currentRA = ra;
                state.currentDec = dec;
            }
            lastEphemerisUpdate = now;
        }

        // Convert current RA/Dec to Alt/Az
        double alt, az;
        raDecToAltAz(state.currentRA, state.currentDec, settings.observerLat, settings.observerLon, alt, az);

        state.targetAlt = alt;
        state.targetAz = az;

        // Check if below horizon
        if (alt < settings.mountAltMin) {
            if (!state.waitingForRise) {
                state.waitingForRise = true;
                DBG(Serial.printf("Target below horizon: Alt=%.1f\n", alt));
                DBG(Serial.println("Parking at home, waiting for target to rise..."));
                srtSerial.logESP("Target below horizon - parking");
                srtSerial.sendTarget(settings.homeAlt, settings.homeAz);
                lastSentAlt = settings.homeAlt;
                lastSentAz = settings.homeAz;
            }
        }
        // Check if above zenith limit
        else if (alt > settings.mountAltMax) {
            DBG(Serial.printf("Target above altitude limit: Alt=%.1f\n", alt));
        }
        // Check azimuth limits
        else if (!isAzWithinLimits(az)) {
            if (!state.waitingForWrap) {
                state.waitingForWrap = true;
                DBG(Serial.printf("Target outside az limits: Az=%.1f\n", az));
                DBG(Serial.println("Waiting for circumpolar wrap-around..."));
                srtSerial.logESP("Az limits - waiting for wrap");
            }
        }
        else {
            // Target is within all limits
            if (state.waitingForRise) {
                state.waitingForRise = false;
                DBG(Serial.printf("Target risen: Alt=%.1f Az=%.1f\n", alt, az));
                DBG(Serial.println("Resuming tracking..."));
                srtSerial.logESP("Target risen - resuming");
                lastSentAlt = -999;
                lastSentAz = -999;
            }
            if (state.waitingForWrap) {
                state.waitingForWrap = false;
                DBG(Serial.printf("Target back in az limits: Alt=%.1f Az=%.1f\n", alt, az));
                DBG(Serial.println("Repositioning to resume tracking..."));
                srtSerial.logESP("Az wrap complete - resuming");
                lastSentAlt = -999;
                lastSentAz = -999;
            }

            // Apply pointing offset for scanning/mapping. Axis-only modes
            // track one coordinate while holding the other at a manual value.
            float baseAlt = state.azOnlyTracking ? state.azOnlyAlt : alt;
            float baseAz = state.altOnlyTracking ? state.altOnlyAz : az;
            float finalAlt = baseAlt + state.offsetAlt;
            float finalAz = baseAz + state.offsetAz;

            // Clamp to valid range
            if (finalAlt < settings.mountAltMin) finalAlt = settings.mountAltMin;
            if (finalAlt > settings.mountAltMax) finalAlt = settings.mountAltMax;
            if (finalAz < settings.mountAzMin) finalAz = settings.mountAzMin;
            if (finalAz > settings.mountAzMax) finalAz = settings.mountAzMax;

            // Apply deadband (check against final position including offset)
            if (lastSentAlt < -900 || lastSentAz < -900 ||
                fabs(finalAlt - lastSentAlt) >= settings.positionDeadband ||
                fabs(finalAz - lastSentAz) >= settings.positionDeadband) {
                if (state.offsetAlt != 0 || state.offsetAz != 0) {
                    DBG(Serial.printf("Tracking %s: base Alt=%.1f Az=%.1f, offset %.1f/%.1f, sending %.1f/%.1f\n",
                                  state.targetName.c_str(), alt, az, state.offsetAlt, state.offsetAz, finalAlt, finalAz));
                } else if (state.azOnlyTracking) {
                    DBG(Serial.printf("Az-only tracking %s: target Az=%.1f, fixed Alt=%.1f\n",
                                  state.targetName.c_str(), finalAz, finalAlt));
                } else if (state.altOnlyTracking) {
                    DBG(Serial.printf("Alt-only tracking %s: target Alt=%.1f, fixed Az=%.1f\n",
                                  state.targetName.c_str(), finalAlt, finalAz));
                } else {
                    DBG(Serial.printf("Tracking %s: sending Alt=%.1f Az=%.1f\n",
                                  state.targetName.c_str(), finalAlt, finalAz));
                }
                srtSerial.sendTarget(finalAlt, finalAz);
                lastSentAlt = finalAlt;
                lastSentAz = finalAz;
            }
        }
    } else {
        wasTracking = false;
    }
}

void setup() {
    // Initialize USB Serial for debug
    Serial.begin(115200);
    #ifdef BOARD_ESP32S3
    Serial.setTxTimeoutMs(0);  // Don't block on serial output (ESP32-S3 USB CDC)
    delay(3000);  // Give USB time to enumerate
    #else
    delay(1000);  // Standard UART needs less time
    #endif

    Serial.println("\n\nSRT Controller starting...");

    // Load settings from NVS
    settings.load();

    // Initialize serial to Due
    srtSerial.begin(DUE_UART_TX, DUE_UART_RX, DUE_BAUD_RATE);
    Serial.println("Due serial initialized");

    // Initialize Ethernet (WT32-ETH01 only)
    #if ETHERNET_ENABLED
    Serial.println("Initializing Ethernet...");
    Serial.printf("  Mode: %s\n", settings.ethUseDHCP ? "DHCP" : "Static IP");
    WiFi.onEvent(onEthEvent);
    // WT32-ETH01 uses LAN8720 PHY with specific pin configuration
    ETH.begin(ETH_PHY_ADDR_CFG, ETH_PHY_POWER_PIN, ETH_PHY_MDC_PIN,
              ETH_PHY_MDIO_PIN, ETH_PHY_LAN8720, ETH_CLOCK_GPIO0_IN);
    // Configure static IP if not using DHCP
    if (!settings.ethUseDHCP) {
        IPAddress ip, gateway, subnet, dns;
        if (ip.fromString(settings.ethStaticIP) &&
            gateway.fromString(settings.ethGateway) &&
            subnet.fromString(settings.ethSubnet) &&
            dns.fromString(settings.ethDNS)) {
            ETH.config(ip, gateway, subnet, dns);
            Serial.printf("  Static IP: %s\n", settings.ethStaticIP.c_str());
        } else {
            Serial.println("  Invalid static IP config, using DHCP");
        }
    }
    // Give Ethernet time to connect
    unsigned long ethStart = millis();
    while (!ethConnected && (millis() - ethStart < 5000)) {
        delay(100);
    }
    if (ethConnected) {
        Serial.println("Ethernet connected - syncing time");
        syncTimeNTP();
    }
    #endif

    // Always initialize WiFi on boot - can only be disabled from web interface
    if (wifiManager.startup()) {
        // Connected to WiFi - sync time with NTP
        if (!state.timeSynced) {
            syncTimeNTP();
        }
    }

    if (!state.timeSynced) {
        Serial.println("NTP not synced - waiting for browser time sync");
    }

    // Start web server
    setupWebServer();
    startDiscoveryServices();
    startOTAService();

    // Stellarium async server
    setupStellariumServer();

    Serial.printf("Free memory: %d bytes\n", ESP.getFreeHeap());
    #if ETHERNET_ENABLED
    if (ethConnected) {
        Serial.printf("Ethernet IP: %s\n", ethIP.c_str());
    }
    #endif
    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("WiFi IP: %s\n", WiFi.localIP().toString().c_str());
    }
    Serial.printf("AP IP: %s\n", WiFi.softAPIP().toString().c_str());
}

void loop() {
#if OTA_ENABLED
    ArduinoOTA.handle();
#endif
    handleWebServer();
    handleStellariumServer();
    updateTracking();

    // Check if Ethernet connected and needs NTP sync
    #if ETHERNET_ENABLED
    if (ethNeedNtpSync && ethConnected) {
        ethNeedNtpSync = false;
        Serial.println("Ethernet connected - syncing time via NTP");
        syncTimeNTP();
    }
    #endif

    delay(10);
}
