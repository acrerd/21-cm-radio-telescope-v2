// state.h - Shared global state for SRT Controller

#ifndef STATE_H
#define STATE_H

#include <Arduino.h>
#include "config.h"

struct SRTState {
    // Tracking state
    float currentRA = 0.0;       // hours
    float currentDec = 0.0;      // degrees
    float targetAlt = 0.0;       // degrees
    float targetAz = 0.0;        // degrees
    bool trackingEnabled = false;
    String targetName = "";      // "Sun", "Moon", "Gal l=x b=y", or empty for manual
    bool azOnlyTracking = false; // True to follow target azimuth while holding fixed altitude
    float azOnlyAlt = 0.0;       // Fixed altitude for azimuth-only tracking
    bool altOnlyTracking = false; // True to follow target altitude while holding fixed azimuth
    float altOnlyAz = 0.0;        // Fixed azimuth for altitude-only tracking
    bool waitingForWrap = false; // True when target is outside az limits
    bool waitingForRise = false; // True when target is below horizon
    unsigned long movementHoldUntil = 0; // Suppress automatic tracking sends until this millis()
    uint32_t trackingRevision = 0; // Increment when target/mode changes to force a fresh command

    // Pointing offset for scanning/mapping (degrees)
    float offsetAlt = 0.0;
    float offsetAz = 0.0;

    // Time state
    bool timeSynced = false;
    String timeSource = "";      // "NTP", "browser", or empty

    // Clock sync tracking. The SNTP notification callback runs on the lwIP task,
    // so every field it touches is a plain 32-bit scalar - never a String, which
    // would reintroduce the cross-task use-after-free of finding C1. 32-bit
    // aligned members are read and written atomically here, so no torn reads.
    volatile uint32_t lastSyncEpoch = 0;        // UTC seconds at last real sync, 0 = never
    volatile uint32_t lastSyncUsec = 0;         // ...and its sub-second part
    volatile uint32_t lastSyncMillis = 0;       // millis() at that sync
    volatile int32_t  lastSyncOffsetMs = 0;     // correction applied at that sync
    volatile uint32_t syncCount = 0;            // successful syncs since boot
    volatile bool     syncEventPending = false; // set by the callback, consumed by loop()
    bool clockStaleWarned = false;              // loopTask only: warn once per stale episode

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

// Seconds since the last genuine NTP sync. Returns 0 when the clock has never
// synced this boot, so callers must check the state string rather than treating
// a zero age as "just synced".
inline uint32_t clockSyncAgeS() {
    if (state.lastSyncEpoch == 0) return 0;
    return (uint32_t)((millis() - state.lastSyncMillis) / 1000UL);
}

// "never", "unverified" and "stale" have different causes and different fixes,
// so they must be distinguishable rather than collapsed into one synced flag.
// "unverified" is a clock set from the browser: usable, but not NTP-checked.
inline const char *clockSyncState() {
    if (state.lastSyncEpoch == 0) return state.timeSynced ? "unverified" : "never";
    return (clockSyncAgeS() > CLOCK_STALE_WARN_S) ? "stale" : "ok";
}

#endif // STATE_H
