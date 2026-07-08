// stellarium.cpp - Stellarium Telescope Protocol server (Async version)

#include "stellarium.h"
#include "config.h"
#include "settings.h"
#include "state.h"
#include "coordinates.h"
#include "srt_serial.h"
#include <AsyncTCP.h>

static AsyncServer* stellariumServer = nullptr;
static AsyncClient* stellariumClient = nullptr;
static unsigned long lastPositionSend = 0;

extern SRTState state;

static void prepareStellariumTrackingTarget() {
    state.waitingForWrap = false;
    state.waitingForRise = false;
    state.movementHoldUntil = 0;
    if (state.azOnlyTracking) {
        state.azOnlyAlt = srtSerial.getCurrentAlt();
    }
    if (state.altOnlyTracking) {
        state.altOnlyAz = srtSerial.getCurrentAz();
    }
    state.trackingEnabled = true;
}

void sendPositionToStellarium() {
    if (!stellariumClient || !stellariumClient->connected()) return;

    // Convert current Alt/Az back to RA/Dec for Stellarium
    double raHours, decDeg;
    altAzToRaDec(state.targetAlt, state.targetAz, settings.observerLat, settings.observerLon, raHours, decDeg);

    // Convert to Stellarium format
    uint32_t raRaw = (uint32_t)((raHours / 24.0) * 0x100000000ULL) & 0xFFFFFFFF;
    int32_t decSigned = (int32_t)((decDeg / 90.0) * 0x40000000);

    // Build response message (24 bytes)
    uint8_t msg[24];
    uint16_t msgLen = 24;
    uint16_t msgType = 0;
    uint64_t timestamp = (uint64_t)micros();

    // Pack in little-endian format
    msg[0] = msgLen & 0xFF;
    msg[1] = (msgLen >> 8) & 0xFF;
    msg[2] = msgType & 0xFF;
    msg[3] = (msgType >> 8) & 0xFF;

    for (int i = 0; i < 8; i++) {
        msg[4 + i] = (timestamp >> (i * 8)) & 0xFF;
    }

    msg[12] = raRaw & 0xFF;
    msg[13] = (raRaw >> 8) & 0xFF;
    msg[14] = (raRaw >> 16) & 0xFF;
    msg[15] = (raRaw >> 24) & 0xFF;

    msg[16] = decSigned & 0xFF;
    msg[17] = (decSigned >> 8) & 0xFF;
    msg[18] = (decSigned >> 16) & 0xFF;
    msg[19] = (decSigned >> 24) & 0xFF;

    msg[20] = 0;
    msg[21] = 0;
    msg[22] = 0;
    msg[23] = 0;

    stellariumClient->write((const char*)msg, 24);
}

void onStellariumData(void* arg, AsyncClient* client, void* data, size_t len) {
    if (len >= 20) {
        uint8_t* bytes = (uint8_t*)data;

        uint16_t msgLen = bytes[0] | (bytes[1] << 8);
        uint16_t msgType = bytes[2] | (bytes[3] << 8);

        if (msgType == 0) {  // Goto command
            uint32_t raRaw = bytes[12] | (bytes[13] << 8) | (bytes[14] << 16) | (bytes[15] << 24);
            int32_t decSigned = bytes[16] | (bytes[17] << 8) | (bytes[18] << 16) | (bytes[19] << 24);

            double raHours = (double)raRaw * 24.0 / 4294967296.0;
            double decDeg = (double)decSigned * 90.0 / 1073741824.0;

            Serial.printf("Stellarium goto: RA=%.4fh, Dec=%.4f\n", raHours, decDeg);
            char logBuf[48];
            snprintf(logBuf, sizeof(logBuf), "Stellarium: RA=%.3fh Dec=%.1f", raHours, decDeg);
            srtSerial.logESP(logBuf);

            state.currentRA = raHours;
            state.currentDec = decDeg;
            state.targetName = "";
            prepareStellariumTrackingTarget();
        }
    }
}

void onStellariumClient(void* arg, AsyncClient* client) {
    Serial.println("Stellarium client connected");
    srtSerial.logESP("Stellarium connected");

    if (stellariumClient && stellariumClient != client) {
        // Disconnect old client
        stellariumClient->close();
    }

    stellariumClient = client;

    client->onData(onStellariumData, nullptr);

    client->onDisconnect([](void* arg, AsyncClient* c) {
        Serial.println("Stellarium client disconnected");
        srtSerial.logESP("Stellarium disconnected");
        if (stellariumClient == c) {
            stellariumClient = nullptr;
        }
        delete c;
    }, nullptr);

    client->onError([](void* arg, AsyncClient* c, int8_t error) {
        Serial.printf("Stellarium error: %d\n", error);
    }, nullptr);
}

void setupStellariumServer() {
    stellariumServer = new AsyncServer(STELLARIUM_PORT);
    stellariumServer->onClient(onStellariumClient, nullptr);
    stellariumServer->begin();
    Serial.printf("Stellarium async server listening on port %d\n", STELLARIUM_PORT);
}

void handleStellariumServer() {
    // Send position updates every 100ms if client connected
    if (stellariumClient && stellariumClient->connected()) {
        if (millis() - lastPositionSend >= 100) {
            sendPositionToStellarium();
            lastPositionSend = millis();
        }
    }
}
