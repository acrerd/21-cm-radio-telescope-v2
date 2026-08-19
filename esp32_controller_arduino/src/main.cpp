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
#include <esp_sntp.h>

#if ETHERNET_ENABLED
#include <ETH.h>
#endif

// Debug print macros - only print if Serial is connected
#define DBG(x) if (Serial) { x; }
#include "settings.h"
#include "state.h"
#include "sync.h"
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
uint32_t lastTrackingRevision = 0;

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
            // Always re-arm: the old gate on timeSynced meant a flag that latched
            // true once was never re-examined, so a reconnect never re-synced.
            ethNeedNtpSync = true;
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

// Has the SNTP client been initialised? configTime() must only run once; after
// that a link change is handled with esp_sntp_restart().
static bool sntpStarted = false;
// millis() of the last SNTP restart attempt while the clock has never synced.
static unsigned long lastSyncRetryMs = 0;

// Called by the lwIP SNTP task on every successful sync. It runs off loopTask,
// so it touches nothing but the 32-bit scalars in state - no String, no logging,
// no blocking - and hands the reporting to updateClockStatus() via a flag.
static void onTimeSync(struct timeval *tv) {
    uint32_t nowMs = millis();
    uint32_t newEpoch = (uint32_t)tv->tv_sec;

    if (state.lastSyncEpoch != 0) {
        // What we believed the time was, versus what it actually is. This
        // applied correction is the drift diagnostic: a steady per-hour value is
        // genuine crystal drift, one large jump is a clock that never synced.
        //
        // Both timestamps carry their sub-second part. Rounding the baseline to
        // whole seconds would inject up to +/-1000 ms of noise, which would bury
        // the signal being measured - an hour at 30 ppm is only ~108 ms.
        int64_t expected = (int64_t)state.lastSyncEpoch * 1000 +
                           (int64_t)(state.lastSyncUsec / 1000) +
                           (int64_t)(nowMs - state.lastSyncMillis);
        int64_t actual = (int64_t)newEpoch * 1000 + (int64_t)(tv->tv_usec / 1000);
        int64_t delta = actual - expected;
        if (delta > INT32_MAX) delta = INT32_MAX;
        if (delta < INT32_MIN) delta = INT32_MIN;
        state.lastSyncOffsetMs = (int32_t)delta;
    } else {
        state.lastSyncOffsetMs = 0;  // first sync this boot: no baseline to compare against
    }

    state.lastSyncEpoch = newEpoch;
    state.lastSyncUsec = (uint32_t)tv->tv_usec;
    state.lastSyncMillis = nowMs;
    state.syncCount++;
    state.syncEventPending = true;
}

// Start the SNTP client, or nudge it after a link change.
//
// Non-blocking by design. The previous version waited up to 10 s for the clock
// to merely look *plausible* (later than 2001), which a soft reset satisfies on
// the first iteration because the RTC keeps running across it - so it reported
// "NTP time synced" on a clock that had never been near a time server, and after
// an OTA update that was every boot. Success is now only ever declared by
// onTimeSync(), which fires when SNTP has actually set the clock.
void syncTimeNTP() {
    if (!sntpStarted) {
        esp_sntp_set_time_sync_notification_cb(onTimeSync);
        esp_sntp_set_sync_interval(NTP_SYNC_INTERVAL_MS);
        // Two servers: the name first, the numeric fallback behind it, so a
        // DNS failure degrades to a working clock instead of no clock.
        configTime(0, 0, NTP_SERVER, NTP_SERVER_FALLBACK);
        sntpStarted = true;
        Serial.printf("SNTP started (%s), resync every %lu s\n",
                      NTP_SERVER, NTP_SYNC_INTERVAL_MS / 1000UL);
        return;
    }

    // configTime() is sntp_stop() followed by sntp_init(), and setup() reaches
    // this function three times on the way up - Ethernet, WiFi startup, then
    // the loop's link-up flag - so it is guarded to run once and later calls
    // just restart the client.
    esp_sntp_restart();
    Serial.println("SNTP restarted");
}

// Clock health reporting, called from loop() so that all String and logging work
// stays on loopTask. Warn-only: a stale clock never blocks tracking.
void updateClockStatus() {
    if (state.syncEventPending) {
        state.syncEventPending = false;
        bool first = (state.syncCount <= 1);
        // timeSource is a String read by /time/status on async_tcp.
        SRTLock lock;
        state.timeSynced = true;
        state.timeSource = "NTP";

        time_t now = (time_t)state.lastSyncEpoch;
        struct tm *t = gmtime(&now);
        Serial.printf("NTP sync: %04d-%02d-%02d %02d:%02d:%02d UTC (offset %+ld ms)\n",
                      t->tm_year + 1900, t->tm_mon + 1, t->tm_mday,
                      t->tm_hour, t->tm_min, t->tm_sec,
                      (long)state.lastSyncOffsetMs);

        char msg[64];
        if (first) {
            snprintf(msg, sizeof(msg), "NTP synced (first since boot)");
        } else {
            snprintf(msg, sizeof(msg), "NTP resync, offset %+ld ms",
                     (long)state.lastSyncOffsetMs);
        }
        srtSerial.logESP(msg);

        if (state.clockStaleWarned) {
            srtSerial.logESP("Clock sync recovered");
            state.clockStaleWarned = false;
        }
        return;
    }

    // A clock that has never synced this boot gets periodic retries. SNTP is
    // otherwise only (re)started by an Ethernet link event, so anything else
    // that disturbs the network stack - a WiFi power cycle, for one - leaves it
    // dead with nothing to revive it. Cheap: one UDP poll every few minutes.
    if (sntpStarted && state.lastSyncEpoch == 0) {
        unsigned long now = millis();
        if (now - lastSyncRetryMs >= NTP_RETRY_INTERVAL_MS) {
            lastSyncRetryMs = now;
            Serial.println("Clock never synced - reinitialising SNTP");
            syncTimeNTP();
        }
    }

    if (!state.clockStaleWarned && state.lastSyncEpoch != 0 &&
        clockSyncAgeS() > CLOCK_STALE_WARN_S) {
        state.clockStaleWarned = true;
        srtSerial.logESP("WARNING: no NTP sync for over 5 hours - pointing may drift");
        Serial.println("WARNING: clock stale, no NTP sync for over 5 hours");
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

    // Read any available status from Due. Deliberately before the lock is
    // taken: these block on the UART, and holding the lock across a blocking
    // read would stall async_tcp. Both take the lock internally where needed.
    srtSerial.readStatus();

    // Request fresh status
    srtSerial.requestStatus();

    // Everything below reads and writes SRTState and Settings that async_tcp
    // handlers mutate: the target name and RA/Dec, the observer position, the
    // waiting flags. Holding the lock for the rest of the function makes one
    // tracking update atomic against any request that lands mid-computation,
    // so a target cannot change between being converted and being sent.
    SRTLock lock;

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
            lastTrackingRevision = state.trackingRevision;
            DBG(Serial.println("Tracking enabled - sending initial position"));
            srtSerial.logESP("Tracking enabled");
        }
        if (lastTrackingRevision != state.trackingRevision) {
            lastSentAlt = -999;
            lastSentAz = -999;
            lastTrackingRevision = state.trackingRevision;
            DBG(Serial.println("Tracking target/mode changed - forcing update"));
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
            } else if (state.targetName == "Galactic Bulge") {
                GalacticPlaneTarget target;
                getGalacticBulgeTrackingTarget(settings.observerLat, settings.observerLon,
                                               effectiveTrackingHorizonAlt(settings.mountAltMin),
                                               target);
                if (target.found) {
                    state.currentRA = target.ra;
                    state.currentDec = target.dec;
                }
            }
            lastEphemerisUpdate = now;
        }

        // Convert current RA/Dec to Alt/Az
        double alt, az;
        raDecToAltAz(state.currentRA, state.currentDec, settings.observerLat, settings.observerLon, alt, az);

        state.targetAlt = alt;
        state.targetAz = az;

        // Check if below the local observing horizon/tree clearance.
        double minTrackingAlt = effectiveTrackingHorizonAlt(settings.mountAltMin);
        if (alt < minTrackingAlt) {
            if (!state.waitingForRise) {
                state.waitingForRise = true;
                DBG(Serial.printf("Target below horizon: Alt=%.1f Min=%.1f\n", alt, minTrackingAlt));
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
        lastTrackingRevision = state.trackingRevision;
    }
}

void setup() {
    // Must come first: everything below may lock, and no other task exists yet
    // to contend with, so this is the one safe moment to create the mutex.
    srtSyncInit();

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
        // Connected to WiFi - start SNTP (no-op if Ethernet already started it)
        syncTimeNTP();
    }

    if (!state.timeSynced) {
        Serial.println("Clock not yet NTP synced - SNTP polling in background");
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
    updateClockStatus();
    // Radio power changes requested by /wifi/power happen here, not in the
    // handler: they involve hundreds of milliseconds of delays that would
    // otherwise freeze every network client.
    wifiManager.servicePowerRequest();

    // Check if Ethernet connected and needs NTP sync. syncTimeNTP() no longer
    // blocks, so this cannot stall tracking or Due status parsing on a link flap.
    #if ETHERNET_ENABLED
    if (ethNeedNtpSync && ethConnected) {
        ethNeedNtpSync = false;
        Serial.println("Ethernet up - (re)starting SNTP");
        syncTimeNTP();
    }
    #endif

    delay(10);
}
