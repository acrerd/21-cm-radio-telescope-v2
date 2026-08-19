// wifi_manager.h - WiFi connection management

#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include <Arduino.h>
#include <WiFi.h>

class WiFiManager {
public:
    WiFiManager();

    // Load saved credentials from file
    bool loadCredentials(String &ssid, String &password);

    // Save credentials to file
    void saveCredentials(const String &ssid, const String &password);

    // Clear saved credentials
    void clearCredentials();

    // Connect to a network (station mode). Blocking - boot use only, never
    // from a request handler; see beginSTA().
    bool connectSTA(const String &ssid, const String &password, int timeout = 15);

    // Start associating and return immediately. Poll isSTAConnected().
    void beginSTA(const String &ssid, const String &password);

    // Start access point
    void startAP();

    // Startup sequence - try saved network, fallback to AP
    bool startup();

    // Scan for networks. Asynchronous: startScan() returns immediately and
    // scanStatus() reports progress, so no handler ever blocks on the radio.
    void startScan();
    int scanStatus();   // -1 running, -2 failed/no result, >=0 network count
    int scanStartResultCode() const;  // rc of the scanNetworks() call itself
    bool retryScanStart();            // re-attempt a start that was refused
    bool scanStartAttemptsLeft() const;
    bool scanWasRequested() const;
    unsigned long scanAgeMs() const;
    String getScannedSSID(int index);
    int getScannedRSSI(int index);
    bool isScannedSecure(int index);

    // Status
    bool isSTAConnected();
    bool isAPActive();
    bool isWiFiEnabled();
    String getSTAIP();
    String getAPIP();
    String getConnectedSSID();

    // Power control (for saving power when using Ethernet)
    void disableWiFi();
    void enableWiFi();

private:
    bool wifiDisabled;
    String connectedSSID;
    bool apActive;
    static const int SCAN_START_MAX_ATTEMPTS = 6;
    bool scanRequested;
    unsigned long scanStartedMs;
    int scanStartResult;
    int scanStartAttempts;
};

// Global instance
extern WiFiManager wifiManager;

#endif // WIFI_MANAGER_H
