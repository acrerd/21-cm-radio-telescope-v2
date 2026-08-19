// settings.cpp - Runtime configurable settings with NVS persistence

#include "settings.h"
#include "config.h"
#include <Preferences.h>

Settings settings;

void Settings::resetToDefaults() {
    observerLat = OBSERVER_LAT;
    observerLon = OBSERVER_LON;
    mountAzMin = MOUNT_AZ_MIN;
    mountAzMax = MOUNT_AZ_MAX;
    mountAltMin = MOUNT_ALT_MIN;
    mountAltMax = MOUNT_ALT_MAX;
    horizonAlt = TRACKING_HORIZON_ALT;
    galacticMinAlt = GALACTIC_PLANE_MIN_ALT;
    stowAlt = STOW_ALT;
    stowAz = STOW_AZ;
    positionDeadband = POSITION_DEADBAND;
    apSSID = WIFI_AP_SSID;
    apPassword = WIFI_AP_PASSWORD;
    pageName = "SRT Controller";
    // Ethernet defaults
    ethUseDHCP = true;
    ethStaticIP = DEFAULT_ETH_STATIC_IP;
    ethGateway = DEFAULT_ETH_GATEWAY;
    ethSubnet = DEFAULT_ETH_SUBNET;
    ethDNS = DEFAULT_ETH_DNS;
    // WiFi enabled by default
    wifiEnabled = true;
}

void Settings::load() {
    // Start with defaults
    resetToDefaults();

    // Override with saved values if they exist
    Preferences prefs;
    prefs.begin("settings", true);  // read-only

    if (prefs.isKey("obsLat")) {
        observerLat = prefs.getDouble("obsLat", OBSERVER_LAT);
        observerLon = prefs.getDouble("obsLon", OBSERVER_LON);
        mountAzMin = prefs.getFloat("azMin", MOUNT_AZ_MIN);
        mountAzMax = prefs.getFloat("azMax", MOUNT_AZ_MAX);
        mountAltMin = prefs.getFloat("altMin", MOUNT_ALT_MIN);
        mountAltMax = prefs.getFloat("altMax", MOUNT_ALT_MAX);
        horizonAlt = prefs.getFloat("horizonAlt", TRACKING_HORIZON_ALT);
        galacticMinAlt = prefs.getFloat("galMinAlt", GALACTIC_PLANE_MIN_ALT);
        // New keys as of the stow rename. A controller upgraded in place has no
        // stowAlt/stowAz, so it falls back to the default stow position and the
        // wanted one is set once in the UI afterwards - deliberately no
        // migration from the old homeAlt/homeAz keys, which meant the same
        // thing but under a name that collided with the Due's encoder origin.
        stowAlt = prefs.getFloat("stowAlt", STOW_ALT);
        stowAz = prefs.getFloat("stowAz", STOW_AZ);
        positionDeadband = prefs.getFloat("deadband", POSITION_DEADBAND);
        apSSID = prefs.getString("apSSID", WIFI_AP_SSID);
        apPassword = prefs.getString("apPass", WIFI_AP_PASSWORD);
        pageName = prefs.getString("pageName", "SRT Controller");
        // Ethernet settings
        ethUseDHCP = prefs.getBool("ethDHCP", true);
        ethStaticIP = prefs.getString("ethIP", DEFAULT_ETH_STATIC_IP);
        ethGateway = prefs.getString("ethGW", DEFAULT_ETH_GATEWAY);
        ethSubnet = prefs.getString("ethSub", DEFAULT_ETH_SUBNET);
        ethDNS = prefs.getString("ethDNS", DEFAULT_ETH_DNS);
        // wifiEnabled is not loaded from NVS - always true on boot
        // Can only be disabled from web interface during a session
        Serial.println("Settings loaded from NVS");
    } else {
        Serial.println("Using default settings");
    }

    prefs.end();
}

void Settings::save() {
    Preferences prefs;
    prefs.begin("settings", false);  // read-write

    prefs.putDouble("obsLat", observerLat);
    prefs.putDouble("obsLon", observerLon);
    prefs.putFloat("azMin", mountAzMin);
    prefs.putFloat("azMax", mountAzMax);
    prefs.putFloat("altMin", mountAltMin);
    prefs.putFloat("altMax", mountAltMax);
    prefs.putFloat("horizonAlt", horizonAlt);
    prefs.putFloat("galMinAlt", galacticMinAlt);
    prefs.putFloat("stowAlt", stowAlt);
    prefs.putFloat("stowAz", stowAz);
    prefs.putFloat("deadband", positionDeadband);
    prefs.putString("apSSID", apSSID);
    prefs.putString("apPass", apPassword);
    prefs.putString("pageName", pageName);
    // Ethernet settings
    prefs.putBool("ethDHCP", ethUseDHCP);
    prefs.putString("ethIP", ethStaticIP);
    prefs.putString("ethGW", ethGateway);
    prefs.putString("ethSub", ethSubnet);
    prefs.putString("ethDNS", ethDNS);
    // wifiEnabled is not persisted - always starts true on boot

    prefs.end();
    Serial.println("Settings saved to NVS");
}
