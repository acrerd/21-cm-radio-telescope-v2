// settings.h - Runtime configurable settings with NVS persistence

#ifndef SETTINGS_H
#define SETTINGS_H

#include <Arduino.h>

class Settings {
public:
    // Observer location
    double observerLat;
    double observerLon;

    // Mechanical mount limits, in DRIVE coordinates. Applied after the pointing
    // model, because that is the frame the mount actually moves in.
    float mountAzMin;
    float mountAzMax;
    float mountAltMin;
    float mountAltMax;

    // Local observing horizon, in TRUE altitude. Applied before the pointing
    // model. Separate from mountAltMin on purpose - see config.h.
    float horizonAlt;

    // Lowest TRUE altitude at which the galactic-plane target may be acquired.
    // Distinct from horizonAlt: that one parks the dish, this one only decides
    // where on the plane tracking begins.
    float galacticMinAlt;

    // Stow position, in DRIVE coordinates - where the dish parks when idle or
    // when its target sets. The pointing model is bypassed on this path; see
    // config.h for why parking is treated as mechanical rather than sky.
    // Unrelated to the Due's homeAlt/homeAz, which are also drive coordinates
    // but define the encoder origin.
    float stowAlt;
    float stowAz;

    // Tracking
    float positionDeadband;

    // WiFi AP
    String apSSID;
    String apPassword;

    // Display
    String pageName;

    // Ethernet (WT32-ETH01 only)
    bool ethUseDHCP;
    String ethStaticIP;
    String ethGateway;
    String ethSubnet;
    String ethDNS;

    // WiFi power state (can be disabled to save power when using Ethernet)
    bool wifiEnabled;

    // Load settings from NVS (uses defaults if not saved)
    void load();

    // Save settings to NVS
    void save();

    // Reset to defaults
    void resetToDefaults();
};

extern Settings settings;

#endif // SETTINGS_H
