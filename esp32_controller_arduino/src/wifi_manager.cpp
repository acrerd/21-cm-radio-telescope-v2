// wifi_manager.cpp - WiFi connection management

#include "wifi_manager.h"
#include "config.h"
#include <Preferences.h>
#include <esp_task_wdt.h>

WiFiManager wifiManager;
Preferences wifiPrefs;

WiFiManager::WiFiManager() : apActive(false) {
}

bool WiFiManager::loadCredentials(String &ssid, String &password) {
    wifiPrefs.begin("wifi", true);  // read-only
    ssid = wifiPrefs.getString("ssid", "");
    password = wifiPrefs.getString("pass", "");
    wifiPrefs.end();
    return ssid.length() > 0;
}

void WiFiManager::saveCredentials(const String &ssid, const String &password) {
    wifiPrefs.begin("wifi", false);  // read-write
    wifiPrefs.putString("ssid", ssid);
    wifiPrefs.putString("pass", password);
    wifiPrefs.end();
}

void WiFiManager::clearCredentials() {
    wifiPrefs.begin("wifi", false);
    wifiPrefs.clear();
    wifiPrefs.end();
}

bool WiFiManager::connectSTA(const String &ssid, const String &password, int timeout) {
    Serial.printf("Connecting to WiFi: %s\n", ssid.c_str());

    // Use AP+STA mode to keep AP running while connecting
    WiFi.mode(WIFI_AP_STA);
    WiFi.begin(ssid.c_str(), password.c_str());

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (millis() - start > (unsigned long)timeout * 1000) {
            Serial.println("WiFi connection timeout");
            return false;
        }
        delay(500);
    }

    connectedSSID = ssid;
    Serial.printf("Connected to %s\n", ssid.c_str());
    Serial.printf("IP: %s\n", WiFi.localIP().toString().c_str());
    return true;
}

void WiFiManager::startAP() {
    // Disconnect any previous connection
    WiFi.disconnect(true);
    delay(100);

    WiFi.mode(WIFI_OFF);
    delay(100);

    WiFi.mode(WIFI_AP);
    delay(100);

    // Disable WiFi sleep to keep AP alive
    WiFi.setSleep(false);

    // Max TX power for better range
    WiFi.setTxPower(WIFI_POWER_19_5dBm);

    // Configure AP with static IP
    IPAddress local_IP(192, 168, 4, 1);
    IPAddress gateway(192, 168, 4, 1);
    IPAddress subnet(255, 255, 255, 0);
    WiFi.softAPConfig(local_IP, gateway, subnet);

    // Try channel 6 (often less congested)
    bool result = WiFi.softAP(WIFI_AP_SSID, WIFI_AP_PASSWORD, 6, false, 4);

    if (result) {
        Serial.println("softAP started successfully");
    } else {
        Serial.println("softAP FAILED to start!");
    }

    apActive = true;
    delay(1000);  // Let AP stabilize

    Serial.printf("AP SSID: %s\n", WIFI_AP_SSID);
    Serial.printf("AP IP: %s\n", WiFi.softAPIP().toString().c_str());
    Serial.printf("AP MAC: %s\n", WiFi.softAPmacAddress().c_str());
    Serial.printf("AP Channel: %d\n", WiFi.channel());
    Serial.printf("Stations connected: %d\n", WiFi.softAPgetStationNum());
}

bool WiFiManager::startup() {
    // Try saved credentials first
    String ssid, password;
    if (loadCredentials(ssid, password)) {
        if (connectSTA(ssid, password)) {
            Serial.println("Connected to WiFi - AP mode disabled");
            return true;
        }
    }

    // STA failed or no credentials - use AP mode
    WiFi.disconnect();
    startAP();
    Serial.println("AP mode active - connect to configure WiFi");
    return false;
}

int WiFiManager::scanNetworks() {
    // Use AP+STA mode to keep AP running while scanning
    WiFi.mode(WIFI_AP_STA);
    delay(100);

    Serial.println("Starting WiFi scan (watchdog disabled)...");

    // Disable watchdog for this task during scan
    esp_task_wdt_delete(NULL);

    // Blocking scan
    int n = WiFi.scanNetworks(false, false, false, 300);  // sync scan, 300ms per channel

    // Re-enable watchdog
    esp_task_wdt_add(NULL);

    Serial.printf("Scan found %d networks\n", n);
    return n;
}

String WiFiManager::getScannedSSID(int index) {
    return WiFi.SSID(index);
}

int WiFiManager::getScannedRSSI(int index) {
    return WiFi.RSSI(index);
}

bool WiFiManager::isScannedSecure(int index) {
    return WiFi.encryptionType(index) != WIFI_AUTH_OPEN;
}

bool WiFiManager::isSTAConnected() {
    return WiFi.status() == WL_CONNECTED;
}

bool WiFiManager::isAPActive() {
    return apActive;
}

String WiFiManager::getSTAIP() {
    return WiFi.localIP().toString();
}

String WiFiManager::getAPIP() {
    return WiFi.softAPIP().toString();
}

String WiFiManager::getConnectedSSID() {
    return connectedSSID;
}
