// state.h - Shared global state for SRT Controller

#ifndef STATE_H
#define STATE_H

#include <Arduino.h>

struct SRTState {
    // Tracking state
    float currentRA = 0.0;       // hours
    float currentDec = 0.0;      // degrees
    float targetAlt = 0.0;       // degrees
    float targetAz = 0.0;        // degrees
    bool trackingEnabled = false;
    String targetName = "";      // "Sun", "Moon", "Gal l=x b=y", or empty for manual
    bool waitingForWrap = false; // True when target is outside az limits
    bool waitingForRise = false; // True when target is below horizon

    // Pointing offset for scanning/mapping (degrees)
    float offsetAlt = 0.0;
    float offsetAz = 0.0;

    // Time state
    bool timeSynced = false;
    String timeSource = "";      // "NTP", "browser", or empty

    // Due status
    float currentAlt = 0.0;
    float currentAz = 0.0;
    float altCurrentA = 0.0;
    float azCurrentA = 0.0;
    String statusStr = "UNKNOWN";
    String faultStr = "";
    bool isSlewing = false;
};

// Global state instance
extern SRTState state;

#endif // STATE_H
