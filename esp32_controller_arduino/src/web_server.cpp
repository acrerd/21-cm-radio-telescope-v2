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
#include "sync.h"
#include "wifi_manager.h"
#include "srt_serial.h"
#include "coordinates.h"
#include "pointing.h"
#include "index_html.h"
#include <time.h>
#include <WiFi.h>
#include <ESPAsyncWebServer.h>
// dns_getserver() reports the resolver lwIP will actually use. The per-netif
// addresses that ETH/WiFi report are not the same thing: dns_gethostbyname()
// consults these globals, so a netif holding a good address while these are
// unset resolves nothing and never puts a packet on the wire (issue #11).
#include <lwip/dns.h>
extern String dnsTraceEth;   // boot trace, defined in main.cpp
extern String dnsTraceWifi;
extern uint32_t resolverRestoreCount;

AsyncWebServer webServer(WEB_PORT);
SRTState state;  // Global state instance
extern bool mdnsRunning;
void syncTimeNTP();  // defined in main.cpp; non-blocking, safe from a handler

// Upload buffer for a pointing model POSTed to /pointing/apply. The body
// arrives in chunks, so it is reassembled here before being parsed. Held in
// the request's _tempObject rather than a file-scope buffer so two overlapping
// uploads cannot interleave into one document.
//
// 1536 bytes is several times the seven-term schema with every optional field
// present. A body past that is rejected rather than silently truncated - a
// truncated model would parse as a valid smaller one.
#define POINTING_JSON_MAX 1536
struct PointingBody {
    size_t len;
    bool truncated;
    char data[POINTING_JSON_MAX + 1];
};

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

// Everything in this file runs on the async_tcp task, while updateTracking()
// reads the same state on loopTask. Each helper below therefore applies its
// whole group of fields under the lock, so a tracking update in progress sees
// either all of a change or none of it - never a target name from one request
// paired with the RA/Dec of another.

static void clearCurrentTracking() {
    SRTLock lock;
    state.trackingEnabled = false;
    state.targetName = "";
    state.waitingForWrap = false;
    state.waitingForRise = false;
}

static void prepareTrackingTarget() {
    SRTLock lock;
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

// Apply a complete new tracking target atomically. Every goto/track endpoint
// sets the same three fields and then calls prepareTrackingTarget(); doing that
// through one locked helper is what keeps the sequence indivisible, and means a
// new endpoint cannot forget the lock by copying the pattern.
static void setTrackingTarget(double ra, double dec, const String &name) {
    SRTLock lock;
    state.currentRA = ra;
    state.currentDec = dec;
    state.targetName = name;
    prepareTrackingTarget();
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
        json += ",\"eth_dns\":\"" + ETH.dnsIP(0).toString() + "\"";
        #endif
        // Issue #11 diagnostics. ipaddr_ntoa() returns a shared static buffer,
        // so each result must be copied before the next call.
        json += ",\"sta_dns\":\"" + WiFi.dnsIP(0).toString() + "\"";
        String dns0 = String(ipaddr_ntoa(dns_getserver(0)));
        String dns1 = String(ipaddr_ntoa(dns_getserver(1)));
        json += ",\"lwip_dns0\":\"" + dns0 + "\"";
        json += ",\"lwip_dns1\":\"" + dns1 + "\"";
        json += ",\"wifi_mode\":" + String((int)WiFi.getMode());
        json += ",\"dns_after_eth\":\"" + dnsTraceEth + "\"";
        json += ",\"dns_after_wifi\":\"" + dnsTraceWifi + "\"";
        json += ",\"dns_restores\":" + String(resolverRestoreCount);
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
        // What the Due reports is a DRIVE position. "alt" and "az" keep that
        // meaning - the scheduler's slew-completion check and sun_scan.py's
        // record of where a source was found both need the drive frame, and
        // both compare against a drive target. The sky position it corresponds
        // to is published alongside as true_alt/true_az, and it is that one,
        // not the drive position, that RA/Dec and galactic l/b are computed
        // from: converting the drive position straight to RA/Dec would be
        // wrong by the whole pointing calibration.
        double driveAlt = srtSerial.getCurrentAlt();
        double driveAz  = srtSerial.getCurrentAz();
        double curAlt = driveAlt, curAz = driveAz;
        driveToTrue(driveAlt, driveAz, curAlt, curAz);
        double ra = 0.0, dec = 0.0, gl = 0.0, gb = 0.0;
        altAzToRaDec(curAlt, curAz, settings.observerLat, settings.observerLon, ra, dec);
        equatorialToGalactic(ra, dec, gl, gb);

        String json = "{";
        json += "\"alt\":" + String(driveAlt, 2) + ",";
        json += "\"az\":" + String(driveAz, 2) + ",";
        json += "\"true_alt\":" + String(curAlt, 2) + ",";
        json += "\"true_az\":" + String(curAz, 2) + ",";
        json += "\"pointing_loaded\":" + String(pointingModel.loaded ? "true" : "false") + ",";
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
        // Clock health is machine-readable here so the scheduler can record sync
        // age with an observation: a scan taken on a stale clock has a corrupted
        // sky position and must be identifiable after the fact.
        json += "\"clock_state\":\"" + String(clockSyncState()) + "\",";
        json += "\"clock_age_s\":" + String(state.lastSyncEpoch != 0 ? (long)clockSyncAgeS() : -1L) + ",";
        // Always zero in normal operation; non-zero means a cross-task lock
        // could not be acquired within its timeout and something ran unlocked.
        json += "\"lock_timeouts\":" + String((unsigned long)srtLockTimeouts) + ",";
        // Also always zero in normal operation. Non-zero means status lines are
        // arriving from the Due spliced together, which happens when its UART
        // output outruns this end - see the rate limit in the Due's
        // outputStatus(). A stale readout with a plausible-looking number in it
        // is the failure mode being guarded against here.
        json += "\"malformed_status\":" + String((unsigned long)srtSerial.getMalformedCount()) + ",";
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
        SRTLock lock;
        state.offsetAlt = 0.0;
        state.offsetAz = 0.0;
        request->send(200, "application/json", "{\"ok\":true,\"offset_alt\":0,\"offset_az\":0}");
    });

    // Set pointing offset (for scanning/mapping)
    webServer.on("/offset", HTTP_GET, [](AsyncWebServerRequest *request) {
        SRTLock lock;
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
        GalacticPlaneTarget plane;
        getGalacticPlaneTrackingTarget(settings.observerLat, settings.observerLon,
                                       settings.galacticMinAlt, plane);

        double sunAlt, sunAz, moonAlt, moonAz;
        raDecToAltAz(sunRA, sunDec, settings.observerLat, settings.observerLon, sunAlt, sunAz);
        raDecToAltAz(moonRA, moonDec, settings.observerLat, settings.observerLon, moonAlt, moonAz);

        char json[512];
        snprintf(json, sizeof(json),
            "{\"sun\":{\"ra\":%.4f,\"dec\":%.2f,\"alt\":%.2f,\"az\":%.2f},"
            "\"moon\":{\"ra\":%.4f,\"dec\":%.2f,\"alt\":%.2f,\"az\":%.2f},"
            "\"plane\":{\"found\":%s,\"l\":%.2f,\"b\":%.2f,"
            "\"ra\":%.4f,\"dec\":%.2f,\"alt\":%.2f,\"az\":%.2f,\"min_alt\":%.1f}}",
            sunRA, sunDec, sunAlt, sunAz,
            moonRA, moonDec, moonAlt, moonAz,
            plane.found ? "true" : "false",
            plane.l, plane.b, plane.ra, plane.dec, plane.alt, plane.az,
            settings.galacticMinAlt);
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
        double minTrackingAlt = settings.horizonAlt;
        if (tAlt < minTrackingAlt) {
            char err[128];
            snprintf(err, sizeof(err),
                "{\"ok\":false,\"error\":\"Target below horizon (alt=%.1f deg, min=%.1f deg)\"}",
                tAlt, minTrackingAlt);
            request->send(400, "application/json", err);
            return;
        }
        setTrackingTarget(ra, dec, "Gal l=" + String(l, 1) + " b=" + String(b, 1));
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
        double minTrackingAlt = settings.horizonAlt;
        if (tAlt < minTrackingAlt) {
            char err[128];
            snprintf(err, sizeof(err),
                "{\"ok\":false,\"error\":\"Target below horizon (alt=%.1f deg, min=%.1f deg)\"}",
                tAlt, minTrackingAlt);
            request->send(400, "application/json", err);
            return;
        }
        setTrackingTarget(ra, dec, "");
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Track enable/disable - use /tracking/enable to avoid route conflict with /track/*
    webServer.on("/tracking/enable", HTTP_GET, [](AsyncWebServerRequest *request) {
        SRTLock lock;
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
        SRTLock lock;
        state.movementHoldUntil = millis() + 10000UL;
        srtSerial.sendStop();
        srtSerial.logESP("Movement stopped for 10s");
        request->send(200, "application/json", "{\"ok\":true,\"hold_ms\":10000}");
    });

    webServer.on("/stop/slewing", HTTP_GET, [](AsyncWebServerRequest *request) {
        SRTLock lock;
        state.movementHoldUntil = millis() + 10000UL;
        srtSerial.sendStop();
        srtSerial.logESP("Slewing stopped for 10s");
        request->send(200, "application/json", "{\"ok\":true,\"hold_ms\":10000}");
    });

    // Stop automatic tracking without sending a motion stop.
    webServer.on("/stop/tracking", HTTP_GET, [](AsyncWebServerRequest *request) {
        SRTLock lock;
        state.movementHoldUntil = 0;
        clearCurrentTracking();
        srtSerial.logESP("Tracking stopped");
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Stop motion and cancel the current tracking target
    webServer.on("/stop/all", HTTP_GET, [](AsyncWebServerRequest *request) {
        SRTLock lock;
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
        SRTLock lock;
        state.movementHoldUntil = 0;
        srtSerial.sendReset();
        srtSerial.logESP("Reset fault");
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Run the Due homing sequence
    webServer.on("/home", HTTP_GET, [](AsyncWebServerRequest *request) {
        SRTLock lock;
        state.movementHoldUntil = 0;
        clearCurrentTracking();
        srtSerial.sendHome();
        srtSerial.logESP("HOME");
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Slew to the saved stow position from Settings. Stow is a DRIVE position -
    // parking is mechanical, not an observation - so the pointing model is
    // bypassed here; see config.h. Kept at /go-home because the scheduler and
    // any bookmarked UI call it by that path.
    //
    // state.targetAlt/targetAz are deliberately not written: they hold a TRUE
    // sky target, and putting drive coordinates in them would be the frame mix
    // this whole arrangement exists to prevent. They are inert once
    // clearCurrentTracking() has run, which is the next thing that happens.
    webServer.on("/go-home", HTTP_GET, [](AsyncWebServerRequest *request) {
        SRTLock lock;
        state.movementHoldUntil = 0;
        clearCurrentTracking();
        double driveAlt = settings.stowAlt, driveAz = settings.stowAz;
        clampToMountLimits(driveAlt, driveAz);
        srtSerial.sendDriveTarget(driveAlt, driveAz);
        char json[96];
        snprintf(json, sizeof(json),
                 "{\"ok\":true,\"drive_alt\":%.2f,\"drive_az\":%.2f}",
                 driveAlt, driveAz);
        srtSerial.logESP("Go to stow");
        request->send(200, "application/json", json);
    });

    // Track Sun
    webServer.on("/track/sun", HTTP_GET, [](AsyncWebServerRequest *request) {
        double ra, dec;
        getSunPosition(ra, dec);
        setTrackingTarget(ra, dec, "Sun");
        srtSerial.logESP("Track Sun");
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Track Moon
    webServer.on("/track/moon", HTTP_GET, [](AsyncWebServerRequest *request) {
        double ra, dec;
        getMoonPosition(ra, dec);
        setTrackingTarget(ra, dec, "Moon");
        srtSerial.logESP("Track Moon");
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Track the galactic plane, as near the centre as the acquisition floor
    // allows. Registered before "/track/galactic" - see the route ordering note
    // at the top of setupWebServer().
    webServer.on("/track/galactic-plane", HTTP_GET, [](AsyncWebServerRequest *request) {
        GalacticPlaneTarget target;
        getGalacticPlaneTrackingTarget(settings.observerLat, settings.observerLon,
                                       settings.galacticMinAlt, target);
        if (!target.found) {
            char err[160];
            snprintf(err, sizeof(err),
                     "{\"ok\":false,\"error\":\"No point on the galactic plane reaches "
                     "%.1f deg altitude just now; wait, or lower the acquisition floor\"}",
                     settings.galacticMinAlt);
            request->send(409, "application/json", err);
            return;
        }
        setTrackingTarget(target.ra, target.dec, "Galactic Plane");
        char msg[64];
        snprintf(msg, sizeof(msg), "Track galactic plane l=%.1f", target.l);
        srtSerial.logESP(msg);
        char json[128];
        snprintf(json, sizeof(json),
                 "{\"ok\":true,\"l\":%.2f,\"b\":%.2f,"
                 "\"ra\":%.4f,\"dec\":%.2f,\"alt\":%.2f,\"az\":%.2f}",
                 target.l, target.b, target.ra, target.dec, target.alt, target.az);
        request->send(200, "application/json", json);
    });

    // Track RA/Dec
    webServer.on("/track/radec", HTTP_GET, [](AsyncWebServerRequest *request) {
        float ra = request->arg("ra").toFloat();
        float dec = request->arg("dec").toFloat();
        double tAlt, tAz;
        raDecToAltAz(ra, dec, settings.observerLat, settings.observerLon, tAlt, tAz);
        double minTrackingAlt = settings.horizonAlt;
        if (tAlt < minTrackingAlt) {
            char err[128];
            snprintf(err, sizeof(err),
                "{\"ok\":false,\"error\":\"Target below horizon (alt=%.1f deg, min=%.1f deg)\"}",
                tAlt, minTrackingAlt);
            request->send(400, "application/json", err);
            return;
        }
        setTrackingTarget(ra, dec, "");
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
        double minTrackingAlt = settings.horizonAlt;
        if (tAlt < minTrackingAlt) {
            char err[128];
            snprintf(err, sizeof(err),
                "{\"ok\":false,\"error\":\"Target below horizon (alt=%.1f deg, min=%.1f deg)\"}",
                tAlt, minTrackingAlt);
            request->send(400, "application/json", err);
            return;
        }
        setTrackingTarget(ra, dec, "Gal l=" + String(l, 1) + " b=" + String(b, 1));
        char json[64];
        snprintf(json, sizeof(json), "{\"ok\":true,\"ra\":%.4f,\"dec\":%.2f}", ra, dec);
        request->send(200, "application/json", json);
    });

    // Change tracking axis mode for the current target
    webServer.on("/tracking/axis", HTTP_GET, [](AsyncWebServerRequest *request) {
        SRTLock lock;
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
        SRTLock lock;
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

    // Direct Alt/Az. The operator types a TRUE sky altitude and azimuth here,
    // the same frame every other target uses, so the model applies to it too.
    webServer.on("/direct", HTTP_GET, [](AsyncWebServerRequest *request) {
        SRTLock lock;
        float alt = request->arg("alt").toFloat();
        float az = request->arg("az").toFloat();
        double driveAlt, driveAz;
        if (state.trackingEnabled && state.azOnlyTracking) {
            state.azOnlyAlt = alt;
            trueToDrive(alt, state.targetAz, driveAlt, driveAz);
            clampToMountLimits(driveAlt, driveAz);
            srtSerial.sendDriveTarget(driveAlt, driveAz);
            request->send(200, "application/json", "{\"ok\":true,\"tracking\":true,\"updated\":\"alt\"}");
            return;
        }
        if (state.trackingEnabled && state.altOnlyTracking) {
            state.altOnlyAz = az;
            trueToDrive(state.targetAlt, az, driveAlt, driveAz);
            clampToMountLimits(driveAlt, driveAz);
            srtSerial.sendDriveTarget(driveAlt, driveAz);
            request->send(200, "application/json", "{\"ok\":true,\"tracking\":true,\"updated\":\"az\"}");
            return;
        }
        state.targetAlt = alt;
        state.targetAz = az;
        state.movementHoldUntil = 0;
        clearCurrentTracking();
        trueToDrive(alt, az, driveAlt, driveAz);
        if (!driveAltWithinLimits(driveAlt) || !driveAzWithinLimits(driveAz)) {
            char err[192];
            snprintf(err, sizeof(err),
                "{\"ok\":false,\"error\":\"Drive position %.1f/%.1f is outside the mount "
                "limits (alt %.1f..%.1f, az %.1f..%.1f)\"}",
                driveAlt, driveAz, settings.mountAltMin, settings.mountAltMax,
                settings.mountAzMin, settings.mountAzMax);
            request->send(400, "application/json", err);
            return;
        }
        srtSerial.sendDriveTarget(driveAlt, driveAz);
        char json[96];
        snprintf(json, sizeof(json), "{\"ok\":true,\"drive_alt\":%.2f,\"drive_az\":%.2f}",
                 driveAlt, driveAz);
        request->send(200, "application/json", json);
    });

    // Time status
    webServer.on("/time/status", HTTP_GET, [](AsyncWebServerRequest *request) {
        SRTLock lock;  // timeSource is a String reassigned by updateClockStatus()
        time_t now = time(nullptr);
        struct tm *t = gmtime(&now);
        char timeStr[32];
        snprintf(timeStr, sizeof(timeStr), "%04d-%02d-%02d %02d:%02d:%02d",
                 t->tm_year + 1900, t->tm_mon + 1, t->tm_mday,
                 t->tm_hour, t->tm_min, t->tm_sec);
        String json = "{";
        bool everSynced = (state.lastSyncEpoch != 0);
        bool stale = !everSynced || (clockSyncAgeS() > CLOCK_STALE_WARN_S);
        json += "\"synced\":" + String(state.timeSynced ? "true" : "false") + ",";
        json += "\"source\":\"" + state.timeSource + "\",";
        json += "\"sync_state\":\"" + String(clockSyncState()) + "\",";
        json += "\"stale\":" + String(stale ? "true" : "false") + ",";
        // -1 rather than 0 when never synced, so a consumer cannot mistake
        // "no sync has ever happened" for "synced a moment ago".
        json += "\"last_sync_age_s\":" + String(everSynced ? (long)clockSyncAgeS() : -1L) + ",";
        json += "\"last_offset_ms\":" + String((long)state.lastSyncOffsetMs) + ",";
        json += "\"sync_count\":" + String((unsigned long)state.syncCount) + ",";
        json += "\"utc\":\"" + String(timeStr) + "\",";
        json += "\"timestamp\":" + String((unsigned long)now);
        json += "}";
        request->send(200, "application/json", json);
    });

    // Force an NTP poll now. Registered before /time/set only for tidiness -
    // neither is a prefix of the other. Returns immediately; the result appears
    // in /time/status when the sync completes.
    webServer.on("/time/sync", HTTP_GET, [](AsyncWebServerRequest *request) {
        syncTimeNTP();
        request->send(200, "application/json",
                      "{\"ok\":true,\"status\":\"sync requested\"}");
    });

    // Time set
    webServer.on("/time/set", HTTP_GET, [](AsyncWebServerRequest *request) {
        unsigned long timestamp = request->arg("timestamp").toInt();
        if (timestamp > 0) {
            struct timeval tv;
            tv.tv_sec = timestamp;
            tv.tv_usec = 0;
            settimeofday(&tv, nullptr);
            SRTLock lock;
            state.timeSynced = true;
            state.timeSource = "browser";
            // Deliberately does not touch lastSyncEpoch: that records genuine NTP
            // syncs only, so a browser-set clock reports as "unverified" rather
            // than masquerading as a checked one. SNTP will correct it at the
            // next poll if the browser's own clock was wrong.
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
    // Asynchronous: the first call starts a scan and reports "running", and the
    // UI polls until the results are ready. Nothing here waits on the radio.
    webServer.on("/wifi/scan", HTTP_GET, [](AsyncWebServerRequest *request) {
        // "restart=1" forces a fresh scan; without it a completed scan keeps
        // returning its cached result. A query arg rather than a /wifi/scan/...
        // sub-route, which this handler would swallow - see the ordering note
        // at the top of this function.
        if (request->arg("restart") == "1") {
            wifiManager.startScan();
            request->send(200, "application/json", "{\"status\":\"running\"}");
            return;
        }

        int n = wifiManager.scanStatus();

        if (!wifiManager.scanWasRequested()) {
            // Nothing has been asked for yet: start one and let the caller poll.
            wifiManager.startScan();
            request->send(200, "application/json", "{\"status\":\"running\"}");
            return;
        }

        if (n == WIFI_SCAN_RUNNING) {
            // Report a stuck scan rather than "running" for ever. Do not
            // silently restart: repeatedly kicking a scan that cannot start
            // would hide the failure behind a permanent "running".
            if (wifiManager.scanAgeMs() > 20000UL) {
                request->send(200, "application/json",
                              "{\"status\":\"failed\",\"error\":\"scan timed out\"}");
                return;
            }
            request->send(200, "application/json", "{\"status\":\"running\"}");
            return;
        }

        if (n < 0) {
            // Either the scan never started, or it finished unsuccessfully. A
            // start is refused while the STA interface is still coming up after
            // the mode change, so retry it on this poll before giving up.
            if (wifiManager.retryScanStart() || wifiManager.scanStartAttemptsLeft()) {
                // Either it started, or there are attempts left for a later
                // poll. Only give up once those are exhausted.
                request->send(200, "application/json", "{\"status\":\"running\"}");
                return;
            }
            // Out of retries: report both codes so the difference between "did
            // not start" and "ran and failed" is visible rather than guessed at.
            // wifi_mode distinguishes a mode change that never took effect
            // (esp_wifi_scan_start refuses unless STA is up) from a scan that
            // was genuinely attempted. 1=STA 2=AP 3=AP_STA.
            String json = "{\"status\":\"failed\",\"complete_rc\":" + String(n) +
                          ",\"start_rc\":" + String(wifiManager.scanStartResultCode()) +
                          ",\"wifi_mode\":" + String((int)WiFi.getMode()) +
                          ",\"age_ms\":" + String(wifiManager.scanAgeMs()) + "}";
            request->send(200, "application/json", json);
            return;
        }

        String json = "{\"status\":\"done\",\"networks\":[";
        for (int i = 0; i < n; i++) {
            if (i > 0) json += ",";
            json += "{";
            // Escaped: an SSID is arbitrary text off the air, and a quote in
            // one would otherwise produce malformed JSON.
            json += "\"ssid\":\"" + jsonEscape(wifiManager.getScannedSSID(i)) + "\",";
            json += "\"rssi\":" + String(wifiManager.getScannedRSSI(i)) + ",";
            json += "\"secure\":" + String(wifiManager.isScannedSecure(i) ? "true" : "false");
            json += "}";
        }
        json += "]}";
        request->send(200, "application/json", json);
    });

    // WiFi connect
    // Starts the association and returns at once - it does not report whether
    // the connection succeeded, because it cannot. WiFi.begin() in AP_STA mode
    // retunes the AP to the STA's channel, so a browser connected over the
    // softAP is dropped mid-request and would never receive the reply no matter
    // how long this waited. The UI polls /wifi/status for the outcome.
    //
    // Credentials are saved before the attempt rather than after a success, so
    // that a reboot caused by the channel change still retries this network.
    webServer.on("/wifi/connect", HTTP_GET, [](AsyncWebServerRequest *request) {
        String ssid = request->arg("ssid");
        String password = request->arg("password");
        if (ssid.length() == 0) {
            request->send(400, "application/json",
                          "{\"ok\":false,\"error\":\"SSID required\"}");
            return;
        }
        wifiManager.saveCredentials(ssid, password);
        wifiManager.beginSTA(ssid, password);
        request->send(200, "application/json",
                      "{\"ok\":true,\"status\":\"connecting\"}");
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
            if (!enable) {
                #if ETHERNET_ENABLED
                if (!ethConnected) {
                    request->send(200, "application/json", "{\"ok\":false,\"error\":\"Cannot disable WiFi without Ethernet\"}");
                    return;
                }
                #endif
            }
            // Queue the radio work for loopTask and answer straight away.
            // Doing it inline blocked this task through an AP start and a full
            // station connect timeout, so the reply never reached the browser:
            // the UI reported that enabling had failed while it was in fact
            // succeeding, which made WiFi look impossible to turn back on.
            settings.wifiEnabled = enable;
            wifiManager.requestPower(enable);
            settings.save();
            request->send(200, "application/json",
                          "{\"ok\":true,\"pending\":true,\"wifi_enabled\":" +
                          String(enable ? "true" : "false") + "}");
        } else {
            // Just return current state
            request->send(200, "application/json", "{\"wifi_enabled\":" + String(wifiManager.isWiFiEnabled() ? "true" : "false") + "}");
        }
    });

    // Pointing calibration. /pointing/apply and /pointing/clear are registered
    // before /pointing - see the route ordering note at the top of this
    // function.
    //
    // The model is a POST body rather than query arguments because it is a
    // document with a schema, uploaded whole: partial application is exactly
    // what pointingApplyJson() refuses to do, and a URL that can be truncated
    // by a proxy or a browser is the wrong carrier for that.
    webServer.on("/pointing/apply", HTTP_POST,
        [](AsyncWebServerRequest *request) {
            PointingBody *body = (PointingBody *)request->_tempObject;
            if (!body || body->len == 0) {
                request->send(400, "application/json",
                              "{\"ok\":false,\"error\":\"Empty request body\"}");
                return;
            }
            if (body->truncated) {
                request->send(413, "application/json",
                              "{\"ok\":false,\"error\":\"Model document is too large\"}");
                return;
            }
            body->data[body->len] = '\0';
            String error;
            bool ok;
            {
                // pointingModel is read by updateTracking() on loopTask, so the
                // replacement is applied as one unit. The NVS write is outside
                // the lock, as with settings: it is tens of milliseconds and
                // nothing on loopTask writes the model.
                SRTLock lock;
                ok = pointingApplyJson(String(body->data), error);
            }
            if (!ok) {
                String json = "{\"ok\":false,\"error\":\"" + jsonEscape(error) + "\"}";
                request->send(400, "application/json", json);
                return;
            }
            pointingSave();
            srtSerial.logESP("Pointing model applied");
            request->send(200, "application/json",
                          "{\"ok\":true,\"model\":" + pointingToJson() + "}");
        },
        nullptr,
        [](AsyncWebServerRequest *request, uint8_t *data, size_t len,
           size_t index, size_t total) {
            if (index == 0) {
                // Freed by ~AsyncWebServerRequest with free(), so this must be
                // malloc'd and must not be a type with a destructor.
                request->_tempObject = malloc(sizeof(PointingBody));
                if (request->_tempObject) {
                    PointingBody *b = (PointingBody *)request->_tempObject;
                    b->len = 0;
                    b->truncated = false;
                }
            }
            PointingBody *body = (PointingBody *)request->_tempObject;
            if (!body) return;
            for (size_t i = 0; i < len; i++) {
                if (body->len >= POINTING_JSON_MAX) {
                    body->truncated = true;
                    return;
                }
                body->data[body->len++] = (char)data[i];
            }
        });

    // Zero the model and erase its NVS keys. A cleared model and an all-zero
    // model behave identically - both are the identity transform - so this is
    // safe to use after mechanical work without leaving pointing in a special
    // state.
    webServer.on("/pointing/clear", HTTP_GET, [](AsyncWebServerRequest *request) {
        {
            // Zeroed under the lock because loopTask reads the model on every
            // tracking update; the flash erase then happens outside it, as with
            // the settings save.
            SRTLock lock;
            pointingModel = PointingModel();
        }
        pointingEraseStored();
        srtSerial.logESP("Pointing model cleared");
        request->send(200, "application/json",
                      "{\"ok\":true,\"model\":" + pointingToJson() + "}");
    });

    webServer.on("/pointing", HTTP_GET, [](AsyncWebServerRequest *request) {
        SRTLock lock;
        request->send(200, "application/json", pointingToJson());
    });

    // Save settings (must be before /settings to avoid route conflict)
    webServer.on("/settings/save", HTTP_GET, [](AsyncWebServerRequest *request) {
        // observerLat/Lon are 8-byte doubles read by updateTracking() on
        // loopTask; an unlocked write there can be seen half-updated and emit
        // one wildly wrong slew command. Scoped so the NVS write at the end of
        // the handler happens outside the lock.
        {
            SRTLock lock;
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
            if (request->hasArg("horizon_alt")) {
                settings.horizonAlt = request->arg("horizon_alt").toFloat();
            }
            if (request->hasArg("galactic_min_alt")) {
                settings.galacticMinAlt = request->arg("galactic_min_alt").toFloat();
            }
            if (request->hasArg("stow_alt")) {
                settings.stowAlt = request->arg("stow_alt").toFloat();
            }
            if (request->hasArg("stow_az")) {
                settings.stowAz = request->arg("stow_az").toFloat();
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
        }  // lock released here - see below
        // Persist outside the lock. save() is an NVS flash write of tens of
        // milliseconds, and holding the lock across it would stall loopTask's
        // tracking update for that whole time. It needs no lock: nothing on
        // loopTask ever writes settings, so there is no writer to race.
        settings.save();
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Reset settings to defaults
    webServer.on("/settings/reset", HTTP_GET, [](AsyncWebServerRequest *request) {
        {
            SRTLock lock;
            settings.resetToDefaults();
        }
        settings.save();  // outside the lock, as above
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Get settings
    webServer.on("/settings", HTTP_GET, [](AsyncWebServerRequest *request) {
        char json[700];
        snprintf(json, sizeof(json),
            "{\"observer_lat\":%.6f,\"observer_lon\":%.6f,"
            "\"mount_az_min\":%.1f,\"mount_az_max\":%.1f,"
            "\"mount_alt_min\":%.1f,\"mount_alt_max\":%.1f,"
            "\"horizon_alt\":%.1f,\"galactic_min_alt\":%.1f,"
            "\"stow_alt\":%.1f,\"stow_az\":%.1f,"
            "\"position_deadband\":%.2f,"
            "\"ap_ssid\":\"%s\",\"ap_password\":\"%s\","
            "\"page_name\":\"%s\"}",
            settings.observerLat, settings.observerLon,
            settings.mountAzMin, settings.mountAzMax,
            settings.mountAltMin, settings.mountAltMax,
            settings.horizonAlt, settings.galacticMinAlt,
            settings.stowAlt, settings.stowAz,
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
