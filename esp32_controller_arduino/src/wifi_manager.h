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

    // Connect to a network (station mode)
    bool connectSTA(const String &ssid, const String &password, int timeout = 15);

    // Start access point
    void startAP();

    // Startup sequence - try saved network, fallback to AP
    bool startup();

    // Scan for networks
    int scanNetworks();
    String getScannedSSID(int index);
    int getScannedRSSI(int index);
    bool isScannedSecure(int index);

    // Status
    bool isSTAConnected();
    bool isAPActive();
    String getSTAIP();
    String getAPIP();
    String getConnectedSSID();

private:
    String connectedSSID;
    bool apActive;
};

// Global instance
extern WiFiManager wifiManager;

#endif // WIFI_MANAGER_H
