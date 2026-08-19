// wifi_manager.cpp - WiFi connection management

#include "wifi_manager.h"
#include "config.h"
#include "settings.h"
#include <Preferences.h>
// esp_task_wdt.h is deliberately not included any more: nothing here should
// touch the task watchdog. See the note above startScan().

WiFiManager wifiManager;
Preferences wifiPrefs;

WiFiManager::WiFiManager()
    : wifiDisabled(false), apActive(false),
      powerChangePending(false), powerChangeTarget(false),
      scanRequested(false), scanStartedMs(0), scanStartResult(WIFI_SCAN_FAILED),
      scanStartAttempts(0) {
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
    WiFi.setHostname(CONTROLLER_HOSTNAME);
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

// Begin an association without waiting for the result.
//
// The blocking form above is still used at boot, where blocking is harmless
// and there is no client waiting on a response. It must not be called from a
// request handler: it blocks the network task for up to 15 s, and worse, in
// AP_STA mode WiFi.begin() retunes the AP to the STA's channel, which drops
// any browser connected over the softAP mid-request. The reply then never
// arrives however long the handler waits, so the UI hangs on "Connecting".
// Callers should return at once and poll /wifi/status instead.
void WiFiManager::beginSTA(const String &ssid, const String &password) {
    Serial.printf("Starting WiFi association: %s\n", ssid.c_str());
    if (WiFi.getMode() != WIFI_AP_STA) {
        WiFi.mode(WIFI_AP_STA);
    }
    WiFi.setHostname(CONTROLLER_HOSTNAME);
    WiFi.begin(ssid.c_str(), password.c_str());
    connectedSSID = ssid;
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
    WiFi.setHostname(CONTROLLER_HOSTNAME);
    delay(100);
    startAP();

    // Step 2: Try saved STA credentials on top of the running AP
    String ssid, password;
    if (loadCredentials(ssid, password)) {
        Serial.printf("Trying saved WiFi: %s\n", ssid.c_str());
        WiFi.mode(WIFI_AP_STA);  // Keep AP running, add STA
        WiFi.setHostname(CONTROLLER_HOSTNAME);
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

// Start an asynchronous scan. Returns immediately; poll scanStatus().
//
// This used to be a synchronous scan that blocked the caller for about four
// seconds, and it bracketed that with esp_task_wdt_delete(NULL) /
// esp_task_wdt_add(NULL). Those two lines were the real problem. The delete
// failed because the calling task - async_tcp - was never subscribed to the
// task watchdog in the first place, and the add then subscribed it
// permanently. async_tcp blocks in queue waits and never resets the watchdog,
// so from the first scan onwards the controller reported "Task watchdog got
// triggered" every watchdog period, and would reboot outright with TWDT panic
// enabled. Nothing here needs to touch the watchdog at all, so it no longer
// does.
void WiFiManager::startScan() {
    // Keep the AP running while scanning. Only switch when needed - a mode
    // change is not free, and this runs on the network task.
    if (WiFi.getMode() != WIFI_AP_STA) {
        WiFi.mode(WIFI_AP_STA);
    }

    WiFi.scanDelete();
    scanStartResult = WiFi.scanNetworks(true);  // async - returns immediately
    scanRequested = true;
    scanStartedMs = millis();
    scanStartAttempts = 1;
    Serial.printf("Async WiFi scan started (rc=%d)\n", scanStartResult);
}

// Try again to start a scan that refused to begin.
//
// esp_wifi_scan_start() fails outright if the STA interface is not up yet, and
// it is not up the instant WiFi.mode(WIFI_AP_STA) returns. The old blocking
// scan hid this behind a delay(100). A request handler cannot afford to sleep,
// so instead the start is retried on subsequent polls, a second or so apart,
// which costs the caller nothing.
bool WiFiManager::retryScanStart() {
    if (!scanRequested || scanStartAttempts >= SCAN_START_MAX_ATTEMPTS) {
        return false;
    }
    scanStartAttempts++;
    scanStartResult = WiFi.scanNetworks(true);
    scanStartedMs = millis();
    Serial.printf("Async WiFi scan retry %d (rc=%d)\n",
                  scanStartAttempts, scanStartResult);
    return scanStartResult != WIFI_SCAN_FAILED;
}

// WIFI_SCAN_RUNNING (-1) while in progress, WIFI_SCAN_FAILED (-2) on failure or
// when no scan has produced a result, otherwise the number of networks found.
int WiFiManager::scanStatus() {
    return WiFi.scanComplete();
}

// Result of the WiFi.scanNetworks() call itself, as opposed to the scan: -1
// means it started, -2 means it could not be started at all. Kept separate so a
// scan that never began is not reported as one still in progress.
int WiFiManager::scanStartResultCode() const {
    return scanStartResult;
}

bool WiFiManager::scanStartAttemptsLeft() const {
    return scanStartAttempts < SCAN_START_MAX_ATTEMPTS;
}

bool WiFiManager::scanWasRequested() const {
    return scanRequested;
}

unsigned long WiFiManager::scanAgeMs() const {
    return scanRequested ? (millis() - scanStartedMs) : 0;
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

    // Bring the AP up, then start any saved station association without
    // waiting for it. startup() would block here for the whole connect
    // timeout, and this runs where that is not acceptable.
    WiFi.mode(WIFI_AP);
    WiFi.setHostname(CONTROLLER_HOSTNAME);
    startAP();

    String ssid, password;
    if (loadCredentials(ssid, password)) {
        beginSTA(ssid, password);
    }
    Serial.println("WiFi enabled");
}

// Ask for the radio to be powered on or off. Safe to call from a request
// handler: it only sets a flag.
//
// The work itself must not run on async_tcp. Enabling means a mode change, an
// AP start with its settling delays and possibly a station association - the
// original path blocked that task for the full connect timeout, so the reply to
// /wifi/power never reached the browser and the UI reported a failure for an
// operation that had actually succeeded. Disabling is quicker but is deferred
// the same way for consistency.
void WiFiManager::requestPower(bool enable) {
    powerChangeTarget = enable;
    powerChangePending = true;
}

// Called from loop(). Performs a pending power change on loopTask, where a few
// hundred milliseconds of radio delays cost one tracking update rather than
// freezing every network client.
void WiFiManager::servicePowerRequest() {
    if (!powerChangePending) return;
    powerChangePending = false;

    if (powerChangeTarget) {
        enableWiFi();
    } else {
        disableWiFi();
    }
}
