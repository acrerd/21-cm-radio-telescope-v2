// wifi_manager.cpp - WiFi connection management

#include "wifi_manager.h"
#include "config.h"
#include "settings.h"
#include <Preferences.h>
#include <esp_task_wdt.h>

WiFiManager wifiManager;
Preferences wifiPrefs;

WiFiManager::WiFiManager() : apActive(false), wifiDisabled(false) {
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
            Serial.println("WiFi STA timeout - AP still active");
            return false;
        }
        delay(500);
    }

    connectedSSID = ssid;
    Serial.printf("Connected to %s, IP: %s\n", ssid.c_str(),
                  WiFi.localIP().toString().c_str());
    return true;
}

void WiFiManager::startAP() {
    // Configure AP - assumes WiFi mode is already set (AP or AP_STA)
    WiFi.setSleep(false);
    WiFi.setTxPower(WIFI_POWER_19_5dBm);

    // Configure AP with static IP
    IPAddress local_IP(192, 168, 4, 1);
    IPAddress gateway(192, 168, 4, 1);
    IPAddress subnet(255, 255, 255, 0);
    WiFi.softAPConfig(local_IP, gateway, subnet);

    // Try channel 6 (often less congested), up to 4 clients
    bool result = WiFi.softAP(settings.apSSID.c_str(), settings.apPassword.c_str(), 6, false, 4);
    apActive = result;

    if (result) {
        Serial.printf("AP started: %s at %s\n", settings.apSSID.c_str(),
                      WiFi.softAPIP().toString().c_str());
    } else {
        Serial.println("softAP FAILED to start!");
    }

    delay(500);  // Let AP stabilize
}

bool WiFiManager::startup() {
    // Step 1: Start AP immediately - guarantees a connection path within seconds
    WiFi.mode(WIFI_AP);
    delay(100);
    startAP();

    // Step 2: Try saved STA credentials on top of the running AP
    String ssid, password;
    if (loadCredentials(ssid, password)) {
        Serial.printf("Trying saved WiFi: %s\n", ssid.c_str());
        WiFi.mode(WIFI_AP_STA);  // Keep AP running, add STA
        WiFi.begin(ssid.c_str(), password.c_str());

        unsigned long start = millis();
        while (WiFi.status() != WL_CONNECTED) {
            if (millis() - start > (unsigned long)WIFI_CONNECT_TIMEOUT * 1000) {
                Serial.println("STA connection timeout - AP still active");
                return false;
            }
            delay(500);
        }

        connectedSSID = ssid;
        Serial.printf("Also connected to %s, IP: %s\n", ssid.c_str(),
                      WiFi.localIP().toString().c_str());
        return true;
    }

    Serial.println("No saved WiFi credentials - AP mode only");
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

bool WiFiManager::isWiFiEnabled() {
    return !wifiDisabled;
}

void WiFiManager::disableWiFi() {
    if (wifiDisabled) return;

    Serial.println("Disabling WiFi to save power...");
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    apActive = false;
    wifiDisabled = true;
    connectedSSID = "";
    Serial.println("WiFi disabled");
}

void WiFiManager::enableWiFi() {
    if (!wifiDisabled) return;

    Serial.println("Enabling WiFi...");
    wifiDisabled = false;

    // Restart WiFi - try saved credentials or fall back to AP
    startup();
    Serial.println("WiFi enabled");
}
