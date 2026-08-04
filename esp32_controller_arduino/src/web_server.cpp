// web_server.cpp - Async Web interface for SRT control

#include "web_server.h"
#include "config.h"

// Ethernet state (defined in main.cpp for WT32-ETH01)
#if ETHERNET_ENABLED
#include <ETH.h>
extern bool ethConnected;
extern String ethIP;
#endif
#include "settings.h"
#include "state.h"
#include "wifi_manager.h"
#include "srt_serial.h"
#include "coordinates.h"
#include "index_html.h"
#include <time.h>
#include <WiFi.h>
#include <ESPAsyncWebServer.h>

AsyncWebServer webServer(WEB_PORT);
SRTState state;  // Global state instance
extern bool mdnsRunning;

static String jsonEscape(const String &value) {
    String escaped;
    escaped.reserve(value.length() + 8);
    for (size_t i = 0; i < value.length(); i++) {
        const unsigned char c = static_cast<unsigned char>(value.charAt(i));
        switch (c) {
            case '"': escaped += "\\\""; break;
            case '\\': escaped += "\\\\"; break;
            case '\b': escaped += "\\b"; break;
            case '\f': escaped += "\\f"; break;
            case '\n': escaped += "\\n"; break;
            case '\r': escaped += "\\r"; break;
            case '\t': escaped += "\\t"; break;
            default:
                if (c < 0x20) {
                    char unicodeEscape[7];
                    snprintf(unicodeEscape, sizeof(unicodeEscape), "\\u%04x", c);
                    escaped += unicodeEscape;
                } else {
                    escaped += static_cast<char>(c);
                }
        }
    }
    return escaped;
}

static void clearCurrentTracking() {
    state.trackingEnabled = false;
    state.targetName = "";
    state.waitingForWrap = false;
    state.waitingForRise = false;
}

static void prepareTrackingTarget() {
    state.waitingForWrap = false;
    state.waitingForRise = false;
    state.movementHoldUntil = 0;
    if (state.azOnlyTracking) {
        state.azOnlyAlt = srtSerial.getCurrentAlt();
    }
    if (state.altOnlyTracking) {
        state.altOnlyAz = srtSerial.getCurrentAz();
    }
    state.trackingRevision++;
    state.trackingEnabled = true;
}

// Route ordering matters. ESPAsyncWebServer matches a handler when the request
// URL equals its URI *or starts with that URI plus a slash*, and the first
// registered match wins. A handler for "/goto" therefore also swallows
// "/goto/galactic" unless the longer route is registered first. Always register
// the most specific path before any prefix of it.
void setupWebServer() {
    // Tiny diagnostics first: these are useful when the full UI cannot load.
    webServer.on("/ping", HTTP_GET, [](AsyncWebServerRequest *request) {
        request->send(200, "text/plain", "ok");
    });

    webServer.on("/network", HTTP_GET, [](AsyncWebServerRequest *request) {
        String json = "{";
        json += "\"heap\":" + String(ESP.getFreeHeap()) + ",";
        json += "\"ap_ip\":\"" + WiFi.softAPIP().toString() + "\",";
        json += "\"sta_connected\":" + String(WiFi.status() == WL_CONNECTED ? "true" : "false") + ",";
        json += "\"sta_ip\":\"" + WiFi.localIP().toString() + "\"";
        #if ETHERNET_ENABLED
        json += ",\"eth_connected\":" + String(ethConnected ? "true" : "false");
        json += ",\"eth_ip\":\"" + ethIP + "\"";
        #endif
        json += "}";
        request->send(200, "application/json", json);
    });

    // Serve main page
    webServer.on("/", HTTP_GET, [](AsyncWebServerRequest *request) {
        AsyncWebServerResponse *response = request->beginResponse_P(200, "text/html", INDEX_HTML);
        response->addHeader("Cache-Control", "no-cache");
        request->send(response);
    });

    webServer.on("/index.html", HTTP_GET, [](AsyncWebServerRequest *request) {
        request->redirect("/");
    });

    // Status endpoint
    webServer.on("/status", HTTP_GET, [](AsyncWebServerRequest *request) {
        // Compute RA/Dec and Galactic l/b for current alt/az
        double curAlt = srtSerial.getCurrentAlt();
        double curAz  = srtSerial.getCurrentAz();
        double ra = 0.0, dec = 0.0, gl = 0.0, gb = 0.0;
        altAzToRaDec(curAlt, curAz, settings.observerLat, settings.observerLon, ra, dec);
        equatorialToGalactic(ra, dec, gl, gb);

        String json = "{";
        json += "\"alt\":" + String(curAlt, 2) + ",";
        json += "\"az\":" + String(curAz, 2) + ",";
        json += "\"ra\":" + String(ra, 4) + ",";
        json += "\"dec\":" + String(dec, 2) + ",";
        json += "\"gal_l\":" + String(gl, 2) + ",";
        json += "\"gal_b\":" + String(gb, 2) + ",";
        json += "\"target_alt\":" + String(srtSerial.getTargetAlt(), 2) + ",";
        json += "\"target_az\":" + String(srtSerial.getTargetAz(), 2) + ",";
        json += "\"alt_current_a\":" + String(srtSerial.getAltCurrentA(), 2) + ",";
        json += "\"az_current_a\":" + String(srtSerial.getAzCurrentA(), 2) + ",";
        json += "\"status\":\"" + jsonEscape(srtSerial.getStatusStr()) + "\",";
        json += "\"fault\":\"" + jsonEscape(srtSerial.getFaultStr()) + "\",";
        bool faultActive = (srtSerial.getStatusStr() == "FAULT") || (srtSerial.getFaultStr().length() > 0);
        json += "\"fault_active\":" + String(faultActive ? "true" : "false") + ",";
        json += "\"is_slewing\":" + String(srtSerial.getIsSlewing() ? "true" : "false") + ",";
        json += "\"calibrator\":" + String(srtSerial.getCalibratorOn() ? "true" : "false") + ",";
        json += "\"raw\":\"" + jsonEscape(srtSerial.getLastStatus()) + "\"";
        json += "}";
        request->send(200, "application/json", json);
    });

    // Serial log endpoint
    webServer.on("/serial/log", HTTP_GET, [](AsyncWebServerRequest *request) {
        request->send(200, "application/json", srtSerial.getLogJSON());
    });

    // Calibrator control
    webServer.on("/calibrator", HTTP_GET, [](AsyncWebServerRequest *request) {
        bool on = srtSerial.getCalibratorOn();
        if (request->hasArg("on")) {
            on = (request->arg("on") == "1" || request->arg("on") == "true");
            srtSerial.sendCalibrator(on);
        }
        String json = "{\"ok\":true,\"calibrator\":" + String(on ? "true" : "false") + "}";
        request->send(200, "application/json", json);
    });

    // Clear pointing offset. Must be registered before "/offset" - see the
    // route ordering note at the top of setupWebServer().
    webServer.on("/offset/clear", HTTP_GET, [](AsyncWebServerRequest *request) {
        state.offsetAlt = 0.0;
        state.offsetAz = 0.0;
        request->send(200, "application/json", "{\"ok\":true,\"offset_alt\":0,\"offset_az\":0}");
    });

    // Set pointing offset (for scanning/mapping)
    webServer.on("/offset", HTTP_GET, [](AsyncWebServerRequest *request) {
        if (request->hasArg("alt")) {
            state.offsetAlt = request->arg("alt").toFloat();
        }
        if (request->hasArg("az")) {
            state.offsetAz = request->arg("az").toFloat();
        }
        String json = "{\"ok\":true,\"offset_alt\":" + String(state.offsetAlt, 2) +
                      ",\"offset_az\":" + String(state.offsetAz, 2) + "}";
        request->send(200, "application/json", json);
    });

    // Ephemeris
    webServer.on("/ephemeris", HTTP_GET, [](AsyncWebServerRequest *request) {
        double sunRA, sunDec, moonRA, moonDec;
        getSunPosition(sunRA, sunDec);
        getMoonPosition(moonRA, moonDec);
        GalacticPlaneTarget bulge;
        getGalacticBulgeTrackingTarget(settings.observerLat, settings.observerLon,
                                       effectiveTrackingHorizonAlt(settings.mountAltMin),
                                       bulge);

        double sunAlt, sunAz, moonAlt, moonAz;
        raDecToAltAz(sunRA, sunDec, settings.observerLat, settings.observerLon, sunAlt, sunAz);
        raDecToAltAz(moonRA, moonDec, settings.observerLat, settings.observerLon, moonAlt, moonAz);

        char json[512];
        snprintf(json, sizeof(json),
            "{\"sun\":{\"ra\":%.4f,\"dec\":%.2f,\"alt\":%.2f,\"az\":%.2f},"
            "\"moon\":{\"ra\":%.4f,\"dec\":%.2f,\"alt\":%.2f,\"az\":%.2f},"
            "\"bulge\":{\"found\":%s,\"bulge_visible\":%s,\"l\":%.2f,\"b\":%.2f,"
            "\"ra\":%.4f,\"dec\":%.2f,\"alt\":%.2f,\"az\":%.2f}}",
            sunRA, sunDec, sunAlt, sunAz,
            moonRA, moonDec, moonAlt, moonAz,
            bulge.found ? "true" : "false",
            bulge.bulgeVisible ? "true" : "false",
            bulge.l, bulge.b, bulge.ra, bulge.dec, bulge.alt, bulge.az);
        request->send(200, "application/json", json);
    });

    // Goto Galactic. Must be registered before "/goto" - see the route
    // ordering note at the top of setupWebServer().
    webServer.on("/goto/galactic", HTTP_GET, [](AsyncWebServerRequest *request) {
        float l = request->arg("l").toFloat();
        float b = request->arg("b").toFloat();
        double ra, dec;
        galacticToEquatorial(l, b, ra, dec);
        double tAlt, tAz;
        raDecToAltAz(ra, dec, settings.observerLat, settings.observerLon, tAlt, tAz);
        double minTrackingAlt = effectiveTrackingHorizonAlt(settings.mountAltMin);
        if (tAlt < minTrackingAlt) {
            char err[128];
            snprintf(err, sizeof(err),
                "{\"ok\":false,\"error\":\"Target below horizon (alt=%.1f deg, min=%.1f deg)\"}",
                tAlt, minTrackingAlt);
            request->send(400, "application/json", err);
            return;
        }
        state.currentRA = ra;
        state.currentDec = dec;
        state.targetName = "Gal l=" + String(l, 1) + " b=" + String(b, 1);
        prepareTrackingTarget();
        char json[64];
        snprintf(json, sizeof(json), "{\"ok\":true,\"ra\":%.4f,\"dec\":%.2f}", ra, dec);
        request->send(200, "application/json", json);
    });

    // Goto RA/Dec
    webServer.on("/goto", HTTP_GET, [](AsyncWebServerRequest *request) {
        float ra = request->arg("ra").toFloat();
        float dec = request->arg("dec").toFloat();
        double tAlt, tAz;
        raDecToAltAz(ra, dec, settings.observerLat, settings.observerLon, tAlt, tAz);
        double minTrackingAlt = effectiveTrackingHorizonAlt(settings.mountAltMin);
        if (tAlt < minTrackingAlt) {
            char err[128];
            snprintf(err, sizeof(err),
                "{\"ok\":false,\"error\":\"Target below horizon (alt=%.1f deg, min=%.1f deg)\"}",
                tAlt, minTrackingAlt);
            request->send(400, "application/json", err);
            return;
        }
        state.currentRA = ra;
        state.currentDec = dec;
        state.targetName = "";
        prepareTrackingTarget();
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Track enable/disable - use /tracking/enable to avoid route conflict with /track/*
    webServer.on("/tracking/enable", HTTP_GET, [](AsyncWebServerRequest *request) {
        bool enable = request->arg("enable") == "1";
        state.trackingEnabled = enable;
        if (!enable) {
            clearCurrentTracking();
            srtSerial.logESP("Tracking stopped");
        }
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Stop current motion but allow automatic tracking to resume after 10 seconds
    webServer.on("/stop/movement", HTTP_GET, [](AsyncWebServerRequest *request) {
        state.movementHoldUntil = millis() + 10000UL;
        srtSerial.sendStop();
        srtSerial.logESP("Movement stopped for 10s");
        request->send(200, "application/json", "{\"ok\":true,\"hold_ms\":10000}");
    });

    webServer.on("/stop/slewing", HTTP_GET, [](AsyncWebServerRequest *request) {
        state.movementHoldUntil = millis() + 10000UL;
        srtSerial.sendStop();
        srtSerial.logESP("Slewing stopped for 10s");
        request->send(200, "application/json", "{\"ok\":true,\"hold_ms\":10000}");
    });

    // Stop automatic tracking without sending a motion stop.
    webServer.on("/stop/tracking", HTTP_GET, [](AsyncWebServerRequest *request) {
        state.movementHoldUntil = 0;
        clearCurrentTracking();
        srtSerial.logESP("Tracking stopped");
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Stop motion and cancel the current tracking target
    webServer.on("/stop/all", HTTP_GET, [](AsyncWebServerRequest *request) {
        state.movementHoldUntil = 0;
        clearCurrentTracking();
        srtSerial.sendStop();
        srtSerial.logESP("STOP all");
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Clear mount fault only when the Due reports one
    webServer.on("/reset", HTTP_GET, [](AsyncWebServerRequest *request) {
        bool faultActive = (srtSerial.getStatusStr() == "FAULT") || (srtSerial.getFaultStr().length() > 0);
        if (!faultActive) {
            request->send(409, "application/json", "{\"ok\":false,\"error\":\"No fault active\"}");
            return;
        }
        state.movementHoldUntil = 0;
        srtSerial.sendReset();
        srtSerial.logESP("Reset fault");
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Run the Due homing sequence
    webServer.on("/home", HTTP_GET, [](AsyncWebServerRequest *request) {
        state.movementHoldUntil = 0;
        clearCurrentTracking();
        srtSerial.sendHome();
        srtSerial.logESP("HOME");
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Slew to the saved home position from Settings.
    webServer.on("/go-home", HTTP_GET, [](AsyncWebServerRequest *request) {
        state.targetAlt = settings.homeAlt;
        state.targetAz = settings.homeAz;
        state.movementHoldUntil = 0;
        clearCurrentTracking();
        srtSerial.sendTarget(settings.homeAlt, settings.homeAz);
        char json[64];
        snprintf(json, sizeof(json), "{\"ok\":true,\"alt\":%.2f,\"az\":%.2f}",
                 settings.homeAlt, settings.homeAz);
        srtSerial.logESP("Go home");
        request->send(200, "application/json", json);
    });

    // Track Sun
    webServer.on("/track/sun", HTTP_GET, [](AsyncWebServerRequest *request) {
        double ra, dec;
        getSunPosition(ra, dec);
        state.currentRA = ra;
        state.currentDec = dec;
        state.targetName = "Sun";
        prepareTrackingTarget();
        srtSerial.logESP("Track Sun");
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Track Moon
    webServer.on("/track/moon", HTTP_GET, [](AsyncWebServerRequest *request) {
        double ra, dec;
        getMoonPosition(ra, dec);
        state.currentRA = ra;
        state.currentDec = dec;
        state.targetName = "Moon";
        prepareTrackingTarget();
        srtSerial.logESP("Track Moon");
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Track Galactic Bulge, falling back to the nearest visible galactic plane point.
    webServer.on("/track/galactic-bulge", HTTP_GET, [](AsyncWebServerRequest *request) {
        GalacticPlaneTarget target;
        getGalacticBulgeTrackingTarget(settings.observerLat, settings.observerLon,
                                       effectiveTrackingHorizonAlt(settings.mountAltMin),
                                       target);
        if (!target.found) {
            request->send(409, "application/json",
                          "{\"ok\":false,\"error\":\"No galactic plane point is above the horizon\"}");
            return;
        }
        state.currentRA = target.ra;
        state.currentDec = target.dec;
        state.targetName = "Galactic Bulge";
        prepareTrackingTarget();
        srtSerial.logESP(target.bulgeVisible ? "Track Galactic Bulge"
                                             : "Track galactic plane near bulge");
        char json[128];
        snprintf(json, sizeof(json),
                 "{\"ok\":true,\"bulge_visible\":%s,\"l\":%.2f,\"b\":%.2f,"
                 "\"ra\":%.4f,\"dec\":%.2f}",
                 target.bulgeVisible ? "true" : "false",
                 target.l, target.b, target.ra, target.dec);
        request->send(200, "application/json", json);
    });

    // Track RA/Dec
    webServer.on("/track/radec", HTTP_GET, [](AsyncWebServerRequest *request) {
        float ra = request->arg("ra").toFloat();
        float dec = request->arg("dec").toFloat();
        double tAlt, tAz;
        raDecToAltAz(ra, dec, settings.observerLat, settings.observerLon, tAlt, tAz);
        double minTrackingAlt = effectiveTrackingHorizonAlt(settings.mountAltMin);
        if (tAlt < minTrackingAlt) {
            char err[128];
            snprintf(err, sizeof(err),
                "{\"ok\":false,\"error\":\"Target below horizon (alt=%.1f deg, min=%.1f deg)\"}",
                tAlt, minTrackingAlt);
            request->send(400, "application/json", err);
            return;
        }
        state.currentRA = ra;
        state.currentDec = dec;
        state.targetName = "";
        prepareTrackingTarget();
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Track Galactic
    webServer.on("/track/galactic", HTTP_GET, [](AsyncWebServerRequest *request) {
        float l = request->arg("l").toFloat();
        float b = request->arg("b").toFloat();
        double ra, dec;
        galacticToEquatorial(l, b, ra, dec);
        double tAlt, tAz;
        raDecToAltAz(ra, dec, settings.observerLat, settings.observerLon, tAlt, tAz);
        double minTrackingAlt = effectiveTrackingHorizonAlt(settings.mountAltMin);
        if (tAlt < minTrackingAlt) {
            char err[128];
            snprintf(err, sizeof(err),
                "{\"ok\":false,\"error\":\"Target below horizon (alt=%.1f deg, min=%.1f deg)\"}",
                tAlt, minTrackingAlt);
            request->send(400, "application/json", err);
            return;
        }
        state.currentRA = ra;
        state.currentDec = dec;
        state.targetName = "Gal l=" + String(l, 1) + " b=" + String(b, 1);
        prepareTrackingTarget();
        char json[64];
        snprintf(json, sizeof(json), "{\"ok\":true,\"ra\":%.4f,\"dec\":%.2f}", ra, dec);
        request->send(200, "application/json", json);
    });

    // Change tracking axis mode for the current target
    webServer.on("/tracking/axis", HTTP_GET, [](AsyncWebServerRequest *request) {
        String mode = request->arg("mode");
        if (mode == "az") {
            state.azOnlyTracking = true;
            state.altOnlyTracking = false;
            state.azOnlyAlt = request->hasArg("alt") ? request->arg("alt").toFloat() : srtSerial.getCurrentAlt();
            srtSerial.logESP("Tracking azimuth only");
        } else if (mode == "alt") {
            state.azOnlyTracking = false;
            state.altOnlyTracking = true;
            state.altOnlyAz = request->hasArg("az") ? request->arg("az").toFloat() : srtSerial.getCurrentAz();
            srtSerial.logESP("Tracking altitude only");
        } else if (mode == "both") {
            state.azOnlyTracking = false;
            state.altOnlyTracking = false;
            srtSerial.logESP("Tracking both axes");
        } else {
            request->send(400, "application/json", "{\"ok\":false,\"error\":\"Use mode=az, mode=alt, or mode=both\"}");
            return;
        }
        state.trackingRevision++;

        String json = "{\"ok\":true,\"az_only\":" + String(state.azOnlyTracking ? "true" : "false") +
                      ",\"alt_only\":" + String(state.altOnlyTracking ? "true" : "false") +
                      ",\"az_only_alt\":" + String(state.azOnlyAlt, 2) +
                      ",\"alt_only_az\":" + String(state.altOnlyAz, 2) + "}";
        request->send(200, "application/json", json);
    });

    // Tracking status. Keep this after /tracking/enable and /tracking/axis
    // because ESPAsyncWebServer route matching can otherwise treat those URLs
    // as this shorter status route.
    webServer.on("/tracking", HTTP_GET, [](AsyncWebServerRequest *request) {
        String json = "{";
        json += "\"enabled\":" + String(state.trackingEnabled ? "true" : "false") + ",";
        json += "\"ra\":" + String(state.currentRA, 4) + ",";
        json += "\"dec\":" + String(state.currentDec, 2) + ",";
        json += "\"target_name\":\"" + state.targetName + "\",";
        json += "\"az_only\":" + String(state.azOnlyTracking ? "true" : "false") + ",";
        json += "\"az_only_alt\":" + String(state.azOnlyAlt, 2) + ",";
        json += "\"alt_only\":" + String(state.altOnlyTracking ? "true" : "false") + ",";
        json += "\"alt_only_az\":" + String(state.altOnlyAz, 2) + ",";
        json += "\"waiting_for_wrap\":" + String(state.waitingForWrap ? "true" : "false") + ",";
        json += "\"waiting_for_rise\":" + String(state.waitingForRise ? "true" : "false") + ",";
        json += "\"offset_alt\":" + String(state.offsetAlt, 2) + ",";
        json += "\"offset_az\":" + String(state.offsetAz, 2);
        json += "}";
        request->send(200, "application/json", json);
    });

    // Direct Alt/Az
    webServer.on("/direct", HTTP_GET, [](AsyncWebServerRequest *request) {
        float alt = request->arg("alt").toFloat();
        float az = request->arg("az").toFloat();
        if (state.trackingEnabled && state.azOnlyTracking) {
            state.azOnlyAlt = alt;
            srtSerial.sendTarget(alt, state.targetAz);
            request->send(200, "application/json", "{\"ok\":true,\"tracking\":true,\"updated\":\"alt\"}");
            return;
        }
        if (state.trackingEnabled && state.altOnlyTracking) {
            state.altOnlyAz = az;
            srtSerial.sendTarget(state.targetAlt, az);
            request->send(200, "application/json", "{\"ok\":true,\"tracking\":true,\"updated\":\"az\"}");
            return;
        }
        state.targetAlt = alt;
        state.targetAz = az;
        state.movementHoldUntil = 0;
        clearCurrentTracking();
        srtSerial.sendTarget(alt, az);
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Time status
    webServer.on("/time/status", HTTP_GET, [](AsyncWebServerRequest *request) {
        time_t now = time(nullptr);
        struct tm *t = gmtime(&now);
        char timeStr[32];
        snprintf(timeStr, sizeof(timeStr), "%04d-%02d-%02d %02d:%02d:%02d",
                 t->tm_year + 1900, t->tm_mon + 1, t->tm_mday,
                 t->tm_hour, t->tm_min, t->tm_sec);
        String json = "{";
        json += "\"synced\":" + String(state.timeSynced ? "true" : "false") + ",";
        json += "\"source\":\"" + state.timeSource + "\",";
        json += "\"utc\":\"" + String(timeStr) + "\",";
        json += "\"timestamp\":" + String((unsigned long)now);
        json += "}";
        request->send(200, "application/json", json);
    });

    // Time set
    webServer.on("/time/set", HTTP_GET, [](AsyncWebServerRequest *request) {
        unsigned long timestamp = request->arg("timestamp").toInt();
        if (timestamp > 0) {
            struct timeval tv;
            tv.tv_sec = timestamp;
            tv.tv_usec = 0;
            settimeofday(&tv, nullptr);
            state.timeSynced = true;
            state.timeSource = "browser";
            Serial.printf("Time set from browser: %lu\n", timestamp);
            srtSerial.logESP("Browser time sync");
            request->send(200, "application/json", "{\"ok\":true}");
        } else {
            request->send(200, "application/json", "{\"ok\":false,\"error\":\"Invalid timestamp\"}");
        }
    });

    // WiFi/Network status
    webServer.on("/wifi/status", HTTP_GET, [](AsyncWebServerRequest *request) {
        String savedSSID, savedPass;
        wifiManager.loadCredentials(savedSSID, savedPass);
        String json = "{";
        json += "\"hostname\":\"" + String(CONTROLLER_HOSTNAME) + "\",";
        json += "\"mdns\":\"http://" + String(CONTROLLER_HOSTNAME) + ".local\",";
        json += "\"mdns_running\":" + String(mdnsRunning ? "true" : "false") + ",";
        json += "\"ota_enabled\":" + String(OTA_ENABLED ? "true" : "false") + ",";
        json += "\"ota_port\":" + String(OTA_PORT) + ",";
        // Ethernet status (WT32-ETH01 only)
        #if ETHERNET_ENABLED
        json += "\"eth_available\":true,";
        json += "\"eth_connected\":" + String(ethConnected ? "true" : "false") + ",";
        json += "\"eth_ip\":\"" + ethIP + "\",";
        json += "\"eth_mac\":\"" + ETH.macAddress() + "\",";
        json += "\"eth_dhcp\":" + String(settings.ethUseDHCP ? "true" : "false") + ",";
        json += "\"eth_static_ip\":\"" + settings.ethStaticIP + "\",";
        json += "\"eth_gateway\":\"" + settings.ethGateway + "\",";
        json += "\"eth_subnet\":\"" + settings.ethSubnet + "\",";
        json += "\"eth_dns\":\"" + settings.ethDNS + "\",";
        #else
        json += "\"eth_available\":false,";
        json += "\"eth_connected\":false,";
        json += "\"eth_ip\":\"\",";
        json += "\"eth_mac\":\"\",";
        json += "\"eth_dhcp\":true,";
        json += "\"eth_static_ip\":\"\",";
        json += "\"eth_gateway\":\"\",";
        json += "\"eth_subnet\":\"\",";
        json += "\"eth_dns\":\"\",";
        #endif
        // WiFi status
        json += "\"wifi_enabled\":" + String(wifiManager.isWiFiEnabled() ? "true" : "false") + ",";
        json += "\"ap_active\":" + String(wifiManager.isAPActive() ? "true" : "false") + ",";
        json += "\"ap_ssid\":\"" + settings.apSSID + "\",";
        json += "\"ap_ip\":\"" + (wifiManager.isAPActive() ? wifiManager.getAPIP() : String("")) + "\",";
        json += "\"wifi_mac\":\"" + WiFi.macAddress() + "\",";
        json += "\"sta_connected\":" + String(wifiManager.isSTAConnected() ? "true" : "false") + ",";
        json += "\"sta_ssid\":\"" + (wifiManager.isSTAConnected() ? wifiManager.getConnectedSSID() : String("")) + "\",";
        json += "\"sta_ip\":\"" + (wifiManager.isSTAConnected() ? wifiManager.getSTAIP() : String("")) + "\",";
        json += "\"saved_ssid\":\"" + savedSSID + "\"";
        json += "}";
        request->send(200, "application/json", json);
    });

    // Save Ethernet settings (requires reboot to apply)
    webServer.on("/eth/save", HTTP_GET, [](AsyncWebServerRequest *request) {
        #if ETHERNET_ENABLED
        if (request->hasArg("dhcp")) {
            settings.ethUseDHCP = (request->arg("dhcp") == "1");
        }
        if (request->hasArg("ip")) {
            settings.ethStaticIP = request->arg("ip");
        }
        if (request->hasArg("gateway")) {
            settings.ethGateway = request->arg("gateway");
        }
        if (request->hasArg("subnet")) {
            settings.ethSubnet = request->arg("subnet");
        }
        if (request->hasArg("dns")) {
            settings.ethDNS = request->arg("dns");
        }
        settings.save();
        request->send(200, "application/json", "{\"ok\":true,\"reboot_required\":true}");
        #else
        request->send(200, "application/json", "{\"ok\":false,\"error\":\"Ethernet not available\"}");
        #endif
    });

    // WiFi scan
    webServer.on("/wifi/scan", HTTP_GET, [](AsyncWebServerRequest *request) {
        int n = wifiManager.scanNetworks();
        String json = "[";
        for (int i = 0; i < n; i++) {
            if (i > 0) json += ",";
            json += "{";
            json += "\"ssid\":\"" + wifiManager.getScannedSSID(i) + "\",";
            json += "\"rssi\":" + String(wifiManager.getScannedRSSI(i)) + ",";
            json += "\"secure\":" + String(wifiManager.isScannedSecure(i) ? "true" : "false");
            json += "}";
        }
        json += "]";
        request->send(200, "application/json", json);
    });

    // WiFi connect
    webServer.on("/wifi/connect", HTTP_GET, [](AsyncWebServerRequest *request) {
        String ssid = request->arg("ssid");
        String password = request->arg("password");
        if (wifiManager.connectSTA(ssid, password)) {
            wifiManager.saveCredentials(ssid, password);
            String json = "{\"ok\":true,\"ip\":\"" + wifiManager.getSTAIP() + "\"}";
            request->send(200, "application/json", json);
        } else {
            request->send(200, "application/json", "{\"ok\":false,\"error\":\"Connection failed\"}");
        }
    });

    // WiFi forget
    webServer.on("/wifi/forget", HTTP_GET, [](AsyncWebServerRequest *request) {
        wifiManager.clearCredentials();
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // WiFi power control (enable/disable to save power when using Ethernet)
    webServer.on("/wifi/power", HTTP_GET, [](AsyncWebServerRequest *request) {
        #if ETHERNET_ENABLED
        if (!ethConnected) {
            request->send(200, "application/json", "{\"ok\":false,\"error\":\"Cannot disable WiFi without Ethernet connection\"}");
            return;
        }
        #endif

        if (request->hasArg("enable")) {
            bool enable = (request->arg("enable") == "1");
            if (enable) {
                wifiManager.enableWiFi();
                settings.wifiEnabled = true;
            } else {
                #if ETHERNET_ENABLED
                if (!ethConnected) {
                    request->send(200, "application/json", "{\"ok\":false,\"error\":\"Cannot disable WiFi without Ethernet\"}");
                    return;
                }
                #endif
                wifiManager.disableWiFi();
                settings.wifiEnabled = false;
            }
            settings.save();
            request->send(200, "application/json", "{\"ok\":true,\"wifi_enabled\":" + String(enable ? "true" : "false") + "}");
        } else {
            // Just return current state
            request->send(200, "application/json", "{\"wifi_enabled\":" + String(wifiManager.isWiFiEnabled() ? "true" : "false") + "}");
        }
    });

    // Save settings (must be before /settings to avoid route conflict)
    webServer.on("/settings/save", HTTP_GET, [](AsyncWebServerRequest *request) {
        if (request->hasArg("observer_lat")) {
            settings.observerLat = request->arg("observer_lat").toDouble();
        }
        if (request->hasArg("observer_lon")) {
            settings.observerLon = request->arg("observer_lon").toDouble();
        }
        if (request->hasArg("mount_az_min")) {
            settings.mountAzMin = request->arg("mount_az_min").toFloat();
        }
        if (request->hasArg("mount_az_max")) {
            settings.mountAzMax = request->arg("mount_az_max").toFloat();
        }
        if (request->hasArg("mount_alt_min")) {
            settings.mountAltMin = request->arg("mount_alt_min").toFloat();
        }
        if (request->hasArg("mount_alt_max")) {
            settings.mountAltMax = request->arg("mount_alt_max").toFloat();
        }
        if (request->hasArg("home_alt")) {
            settings.homeAlt = request->arg("home_alt").toFloat();
        }
        if (request->hasArg("home_az")) {
            settings.homeAz = request->arg("home_az").toFloat();
        }
        if (request->hasArg("position_deadband")) {
            settings.positionDeadband = request->arg("position_deadband").toFloat();
        }
        if (request->hasArg("ap_ssid")) {
            settings.apSSID = request->arg("ap_ssid");
        }
        if (request->hasArg("ap_password")) {
            settings.apPassword = request->arg("ap_password");
        }
        if (request->hasArg("page_name")) {
            settings.pageName = request->arg("page_name");
        }
        settings.save();
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Reset settings to defaults
    webServer.on("/settings/reset", HTTP_GET, [](AsyncWebServerRequest *request) {
        settings.resetToDefaults();
        settings.save();
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Get settings
    webServer.on("/settings", HTTP_GET, [](AsyncWebServerRequest *request) {
        char json[600];
        snprintf(json, sizeof(json),
            "{\"observer_lat\":%.6f,\"observer_lon\":%.6f,"
            "\"mount_az_min\":%.1f,\"mount_az_max\":%.1f,"
            "\"mount_alt_min\":%.1f,\"mount_alt_max\":%.1f,"
            "\"home_alt\":%.1f,\"home_az\":%.1f,"
            "\"position_deadband\":%.2f,"
            "\"ap_ssid\":\"%s\",\"ap_password\":\"%s\","
            "\"page_name\":\"%s\"}",
            settings.observerLat, settings.observerLon,
            settings.mountAzMin, settings.mountAzMax,
            settings.mountAltMin, settings.mountAltMax,
            settings.homeAlt, settings.homeAz,
            settings.positionDeadband,
            settings.apSSID.c_str(), settings.apPassword.c_str(),
            settings.pageName.c_str());
        request->send(200, "application/json", json);
    });

    // 404 handler
    webServer.onNotFound([](AsyncWebServerRequest *request) {
        request->send(404, "text/plain", "Not Found");
    });

    webServer.begin();
    Serial.printf("Async web server listening on port %d\n", WEB_PORT);
}

void handleWebServer() {
    // AsyncWebServer handles itself - nothing needed here
}
