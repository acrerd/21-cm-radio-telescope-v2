// main.cpp - SRT Controller main application
// ESP32-S3 Arduino version

#include <Arduino.h>
#include <WiFi.h>
#include <time.h>

#include "config.h"
#include "settings.h"
#include "state.h"
#include "wifi_manager.h"
#include "srt_serial.h"
#include "web_server.h"
#include "stellarium.h"
#include "coordinates.h"

// External state
extern SRTState state;

// Tracking loop variables
unsigned long lastTrackingUpdate = 0;
unsigned long lastEphemerisUpdate = 0;
float lastSentAlt = -999;
float lastSentAz = -999;
bool wasTracking = false;

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
    } else {
        Serial.println("NTP sync failed");
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

    if (state.trackingEnabled) {
        // Detect tracking just enabled - force immediate send
        if (!wasTracking) {
            lastSentAlt = -999;
            lastSentAz = -999;
            Serial.println("Tracking enabled - sending initial position");
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
                Serial.printf("Target below horizon: Alt=%.1f\n", alt);
                Serial.println("Parking at home, waiting for target to rise...");
                srtSerial.sendTarget(settings.homeAlt, settings.homeAz);
                lastSentAlt = settings.homeAlt;
                lastSentAz = settings.homeAz;
            }
        }
        // Check if above zenith limit
        else if (alt > settings.mountAltMax) {
            Serial.printf("Target above altitude limit: Alt=%.1f\n", alt);
        }
        // Check azimuth limits
        else if (!isAzWithinLimits(az)) {
            if (!state.waitingForWrap) {
                state.waitingForWrap = true;
                Serial.printf("Target outside az limits: Az=%.1f\n", az);
                Serial.println("Waiting for circumpolar wrap-around...");
            }
        }
        else {
            // Target is within all limits
            if (state.waitingForRise) {
                state.waitingForRise = false;
                Serial.printf("Target risen: Alt=%.1f Az=%.1f\n", alt, az);
                Serial.println("Resuming tracking...");
                lastSentAlt = -999;
                lastSentAz = -999;
            }
            if (state.waitingForWrap) {
                state.waitingForWrap = false;
                Serial.printf("Target back in az limits: Alt=%.1f Az=%.1f\n", alt, az);
                Serial.println("Repositioning to resume tracking...");
                lastSentAlt = -999;
                lastSentAz = -999;
            }

            // Apply deadband
            if (lastSentAlt < -900 || lastSentAz < -900 ||
                fabs(alt - lastSentAlt) >= settings.positionDeadband ||
                fabs(az - lastSentAz) >= settings.positionDeadband) {
                Serial.printf("Tracking %s: sending Alt=%.1f Az=%.1f\n",
                              state.targetName.c_str(), alt, az);
                srtSerial.sendTarget(alt, az);
                lastSentAlt = alt;
                lastSentAz = az;
            }
        }
    } else {
        wasTracking = false;
    }
}

void setup() {
    // Initialize USB Serial for debug
    Serial.begin(115200);
    Serial.setTxTimeoutMs(0);  // Don't block on serial output
    delay(3000);  // Give USB time to enumerate

    Serial.println("\n\nSRT Controller starting...");

    // Load settings from NVS
    settings.load();

    // Initialize serial to Due
    srtSerial.begin(DUE_UART_TX, DUE_UART_RX, DUE_BAUD_RATE);
    Serial.println("Due serial initialized");

    // Initialize WiFi
    if (wifiManager.startup()) {
        // Connected to WiFi - sync time with NTP
        syncTimeNTP();
    }

    if (!state.timeSynced) {
        Serial.println("NTP not synced - waiting for browser time sync");
    }

    // Start web server
    setupWebServer();

    // Stellarium async server
    setupStellariumServer();

    Serial.printf("Free memory: %d bytes\n", ESP.getFreeHeap());
    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("WiFi IP: %s\n", WiFi.localIP().toString().c_str());
    }
    Serial.printf("AP IP: %s\n", WiFi.softAPIP().toString().c_str());
}

void loop() {
    handleWebServer();
    handleStellariumServer();
    updateTracking();
    delay(10);
}
