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
    homeAlt = HOME_ALT;
    homeAz = HOME_AZ;
    positionDeadband = POSITION_DEADBAND;
    apSSID = WIFI_AP_SSID;
    apPassword = WIFI_AP_PASSWORD;
    pageName = "SRT Controller";
    // Ethernet defaults
    ethUseDHCP = true;
    ethStaticIP = "192.168.1.100";
    ethGateway = "192.168.1.1";
    ethSubnet = "255.255.255.0";
    ethDNS = "8.8.8.8";
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
        homeAlt = prefs.getFloat("homeAlt", HOME_ALT);
        homeAz = prefs.getFloat("homeAz", HOME_AZ);
        positionDeadband = prefs.getFloat("deadband", POSITION_DEADBAND);
        apSSID = prefs.getString("apSSID", WIFI_AP_SSID);
        apPassword = prefs.getString("apPass", WIFI_AP_PASSWORD);
        pageName = prefs.getString("pageName", "SRT Controller");
        // Ethernet settings
        ethUseDHCP = prefs.getBool("ethDHCP", true);
        ethStaticIP = prefs.getString("ethIP", "192.168.1.100");
        ethGateway = prefs.getString("ethGW", "192.168.1.1");
        ethSubnet = prefs.getString("ethSub", "255.255.255.0");
        ethDNS = prefs.getString("ethDNS", "8.8.8.8");
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
    prefs.putFloat("homeAlt", homeAlt);
    prefs.putFloat("homeAz", homeAz);
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
