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
#include <ESPAsyncWebServer.h>

AsyncWebServer webServer(WEB_PORT);
SRTState state;  // Global state instance

void setupWebServer() {
    // Serve main page
    webServer.on("/", HTTP_GET, [](AsyncWebServerRequest *request) {
        AsyncWebServerResponse *response = request->beginResponse(200, "text/html", INDEX_HTML);
        response->addHeader("Cache-Control", "no-cache");
        request->send(response);
    });

    webServer.on("/index.html", HTTP_GET, [](AsyncWebServerRequest *request) {
        request->redirect("/");
    });

    // Status endpoint
    webServer.on("/status", HTTP_GET, [](AsyncWebServerRequest *request) {
        String json = "{";
        json += "\"alt\":" + String(srtSerial.getCurrentAlt(), 2) + ",";
        json += "\"az\":" + String(srtSerial.getCurrentAz(), 2) + ",";
        json += "\"target_alt\":" + String(srtSerial.getTargetAlt(), 2) + ",";
        json += "\"target_az\":" + String(srtSerial.getTargetAz(), 2) + ",";
        json += "\"alt_current_a\":" + String(srtSerial.getAltCurrentA(), 2) + ",";
        json += "\"az_current_a\":" + String(srtSerial.getAzCurrentA(), 2) + ",";
        json += "\"status\":\"" + srtSerial.getStatusStr() + "\",";
        json += "\"fault\":\"" + srtSerial.getFaultStr() + "\",";
        json += "\"is_slewing\":" + String(srtSerial.getIsSlewing() ? "true" : "false") + ",";
        json += "\"raw\":\"" + srtSerial.getLastStatus() + "\"";
        json += "}";
        request->send(200, "application/json", json);
    });

    // Tracking status
    webServer.on("/tracking", HTTP_GET, [](AsyncWebServerRequest *request) {
        String json = "{";
        json += "\"enabled\":" + String(state.trackingEnabled ? "true" : "false") + ",";
        json += "\"ra\":" + String(state.currentRA, 4) + ",";
        json += "\"dec\":" + String(state.currentDec, 2) + ",";
        json += "\"target_name\":\"" + state.targetName + "\",";
        json += "\"waiting_for_wrap\":" + String(state.waitingForWrap ? "true" : "false") + ",";
        json += "\"waiting_for_rise\":" + String(state.waitingForRise ? "true" : "false");
        json += "}";
        request->send(200, "application/json", json);
    });

    // Ephemeris
    webServer.on("/ephemeris", HTTP_GET, [](AsyncWebServerRequest *request) {
        double sunRA, sunDec, moonRA, moonDec;
        getSunPosition(sunRA, sunDec);
        getMoonPosition(moonRA, moonDec);

        double sunAlt, sunAz, moonAlt, moonAz;
        raDecToAltAz(sunRA, sunDec, settings.observerLat, settings.observerLon, sunAlt, sunAz);
        raDecToAltAz(moonRA, moonDec, settings.observerLat, settings.observerLon, moonAlt, moonAz);

        char json[256];
        snprintf(json, sizeof(json),
            "{\"sun\":{\"ra\":%.4f,\"dec\":%.2f,\"alt\":%.2f,\"az\":%.2f},"
            "\"moon\":{\"ra\":%.4f,\"dec\":%.2f,\"alt\":%.2f,\"az\":%.2f}}",
            sunRA, sunDec, sunAlt, sunAz,
            moonRA, moonDec, moonAlt, moonAz);
        request->send(200, "application/json", json);
    });

    // Goto RA/Dec
    webServer.on("/goto", HTTP_GET, [](AsyncWebServerRequest *request) {
        float ra = request->arg("ra").toFloat();
        float dec = request->arg("dec").toFloat();
        state.currentRA = ra;
        state.currentDec = dec;
        state.targetName = "";
        state.waitingForWrap = false;
        state.waitingForRise = false;
        state.trackingEnabled = true;
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Goto Galactic
    webServer.on("/goto/galactic", HTTP_GET, [](AsyncWebServerRequest *request) {
        float l = request->arg("l").toFloat();
        float b = request->arg("b").toFloat();
        double ra, dec;
        galacticToEquatorial(l, b, ra, dec);
        state.currentRA = ra;
        state.currentDec = dec;
        state.targetName = "Gal l=" + String(l, 1) + " b=" + String(b, 1);
        state.waitingForWrap = false;
        state.waitingForRise = false;
        state.trackingEnabled = true;
        char json[64];
        snprintf(json, sizeof(json), "{\"ok\":true,\"ra\":%.4f,\"dec\":%.2f}", ra, dec);
        request->send(200, "application/json", json);
    });

    // Track enable/disable - use /tracking/enable to avoid route conflict with /track/*
    webServer.on("/tracking/enable", HTTP_GET, [](AsyncWebServerRequest *request) {
        bool enable = request->arg("enable") == "1";
        state.trackingEnabled = enable;
        if (!enable) {
            state.targetName = "";
            state.waitingForWrap = false;
            state.waitingForRise = false;
        }
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Track Sun
    webServer.on("/track/sun", HTTP_GET, [](AsyncWebServerRequest *request) {
        double ra, dec;
        getSunPosition(ra, dec);
        state.currentRA = ra;
        state.currentDec = dec;
        state.targetName = "Sun";
        state.waitingForWrap = false;
        state.waitingForRise = false;
        state.trackingEnabled = true;
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Track Moon
    webServer.on("/track/moon", HTTP_GET, [](AsyncWebServerRequest *request) {
        double ra, dec;
        getMoonPosition(ra, dec);
        state.currentRA = ra;
        state.currentDec = dec;
        state.targetName = "Moon";
        state.waitingForWrap = false;
        state.waitingForRise = false;
        state.trackingEnabled = true;
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Track RA/Dec
    webServer.on("/track/radec", HTTP_GET, [](AsyncWebServerRequest *request) {
        float ra = request->arg("ra").toFloat();
        float dec = request->arg("dec").toFloat();
        state.currentRA = ra;
        state.currentDec = dec;
        state.targetName = "";
        state.waitingForWrap = false;
        state.waitingForRise = false;
        state.trackingEnabled = true;
        request->send(200, "application/json", "{\"ok\":true}");
    });

    // Track Galactic
    webServer.on("/track/galactic", HTTP_GET, [](AsyncWebServerRequest *request) {
        float l = request->arg("l").toFloat();
        float b = request->arg("b").toFloat();
        double ra, dec;
        galacticToEquatorial(l, b, ra, dec);
        state.currentRA = ra;
        state.currentDec = dec;
        state.targetName = "Gal l=" + String(l, 1) + " b=" + String(b, 1);
        state.waitingForWrap = false;
        state.waitingForRise = false;
        state.trackingEnabled = true;
        char json[64];
        snprintf(json, sizeof(json), "{\"ok\":true,\"ra\":%.4f,\"dec\":%.2f}", ra, dec);
        request->send(200, "application/json", json);
    });

    // Direct Alt/Az
    webServer.on("/direct", HTTP_GET, [](AsyncWebServerRequest *request) {
        float alt = request->arg("alt").toFloat();
        float az = request->arg("az").toFloat();
        state.targetAlt = alt;
        state.targetAz = az;
        state.trackingEnabled = false;
        state.targetName = "";
        state.waitingForWrap = false;
        state.waitingForRise = false;
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
