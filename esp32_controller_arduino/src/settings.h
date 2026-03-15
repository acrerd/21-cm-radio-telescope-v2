// settings.h - Runtime configurable settings with NVS persistence

#ifndef SETTINGS_H
#define SETTINGS_H

#include <Arduino.h>

class Settings {
public:
    // Observer location
    double observerLat;
    double observerLon;

    // Mount limits
    float mountAzMin;
    float mountAzMax;
    float mountAltMin;
    float mountAltMax;

    // Home position
    float homeAlt;
    float homeAz;

    // Tracking
    float positionDeadband;

    // WiFi AP
    String apSSID;
    String apPassword;

    // Display
    String pageName;

    // Load settings from NVS (uses defaults if not saved)
    void load();

    // Save settings to NVS
    void save();

    // Reset to defaults
    void resetToDefaults();
};

extern Settings settings;

#endif // SETTINGS_H
