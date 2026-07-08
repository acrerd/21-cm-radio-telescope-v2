// srt_serial.cpp - Serial communication with Arduino Due

#include "srt_serial.h"

SRTSerial srtSerial;

SRTSerial::SRTSerial() :
    uart(nullptr),
    currentAlt(0),
    currentAz(0),
    targetAlt(0),
    targetAz(0),
    altCurrentA(0),
    azCurrentA(0),
    statusStr("UNKNOWN"),
    isSlewing(false),
    calibratorOn(false),
    logHead(0),
    logCount(0) {
}

void SRTSerial::logMessage(char direction, const String &msg) {
    logBuffer[logHead].utcTime = time(nullptr);
    logBuffer[logHead].millis = millis();
    logBuffer[logHead].direction = direction;
    logBuffer[logHead].message = msg;
    logHead = (logHead + 1) % SERIAL_LOG_SIZE;
    if (logCount < SERIAL_LOG_SIZE) logCount++;
}

void SRTSerial::logESP(const String &msg) {
    logMessage('E', msg);
}

String SRTSerial::getLogJSON() {
    String json = "[";
    int start = (logCount < SERIAL_LOG_SIZE) ? 0 : logHead;
    for (int i = 0; i < logCount; i++) {
        int idx = (start + i) % SERIAL_LOG_SIZE;
        if (i > 0) json += ",";

        // Format timestamp - use UTC if available, otherwise millis
        String timeStr;
        if (logBuffer[idx].utcTime > 1000000000) {
            struct tm *t = gmtime(&logBuffer[idx].utcTime);
            char buf[12];
            snprintf(buf, sizeof(buf), "%02d:%02d:%02d",
                     t->tm_hour, t->tm_min, t->tm_sec);
            timeStr = buf;
        } else {
            timeStr = String(logBuffer[idx].millis / 1000) + "s";
        }

        json += "{\"time\":\"" + timeStr + "\",";
        const char* dirStr = (logBuffer[idx].direction == 'T') ? "TX" :
                             (logBuffer[idx].direction == 'R') ? "RX" : "ESP";
        json += "\"dir\":\"" + String(dirStr) + "\",";
        // Escape quotes in message
        String escaped = logBuffer[idx].message;
        escaped.replace("\\", "\\\\");
        escaped.replace("\"", "\\\"");
        json += "\"msg\":\"" + escaped + "\"}";
    }
    json += "]";
    return json;
}

void SRTSerial::begin(int txPin, int rxPin, int baudRate) {
    // Use Serial2 on standard ESP32 (Serial1 pins conflict with flash)
    uart = &Serial2;
    Serial.printf("SRTSerial: TX pin=%d, RX pin=%d, baud=%d\n", txPin, rxPin, baudRate);

    // Test: toggle TX pin manually to verify connectivity
    pinMode(txPin, OUTPUT);
    for (int i = 0; i < 5; i++) {
        digitalWrite(txPin, HIGH);
        delay(100);
        digitalWrite(txPin, LOW);
        delay(100);
    }
    Serial.println("SRTSerial: TX pin toggle test complete");

    uart->begin(baudRate, SERIAL_8N1, rxPin, txPin);
    uart->setTimeout(10);  // 10ms timeout instead of default 1000ms
    Serial.println("SRTSerial: Serial2 initialized");

    // Send test message
    uart->println("ESP32_HELLO");
    Serial.println("SRTSerial: Sent test message");
}

void SRTSerial::sendTarget(float alt, float az) {
    if (uart) {
        char cmd[32];
        snprintf(cmd, sizeof(cmd), "%.1f %.1f", alt, az);
        logMessage('T', cmd);
        uart->println(cmd);
    }
}

void SRTSerial::sendHome() {
    if (uart) {
        logMessage('T', "HOME");
        uart->println("HOME");
    }
}

void SRTSerial::sendStop() {
    if (uart) {
        logMessage('T', "STOP");
        uart->println("STOP");
    }
}

void SRTSerial::sendReset() {
    if (uart) {
        logMessage('T', "RESET");
        uart->println("RESET");
    }
}

void SRTSerial::sendCalibrator(bool on) {
    if (uart) {
        logMessage('T', on ? "CAL ON" : "CAL OFF");
        uart->println(on ? "CAL ON" : "CAL OFF");
    }
}

void SRTSerial::requestStatus() {
    // Don't log STATUS requests - too noisy (every second)
    if (uart) {
        uart->println("STATUS");
    }
}

bool SRTSerial::readStatus() {
    if (!uart) return false;

    String lastValidLine;
    int linesRead = 0;
    // Limit to 5 lines per call to prevent starving other tasks
    while (uart->available() && linesRead < 5) {
        String line = uart->readStringUntil('\n');
        line.trim();
        linesRead++;

        // Validate format. Slewing lines include a second target "Alt:"/" Az:"
        // after " -> ", so only require the current-position prefix.
        if (line.startsWith("Alt:") &&
            line.indexOf(" Az:") != -1 &&
            line.indexOf("Status:") != -1) {
            lastValidLine = line;
            logMessage('R', line);  // Log valid status lines
        }
    }

    if (lastValidLine.length() > 0) {
        parseStatus(lastValidLine);
        return true;
    }
    return false;
}

void SRTSerial::parseStatus(const String &line) {
    lastStatus = line;

    // Extract current position - Alt:45.0
    int altIdx = line.indexOf("Alt:");
    if (altIdx >= 0) {
        int endIdx = line.indexOf(' ', altIdx);
        if (endIdx > altIdx) {
            currentAlt = line.substring(altIdx + 4, endIdx).toFloat();
        }
    }

    // Extract Az:180.0
    int azIdx = line.indexOf(" Az:");
    if (azIdx >= 0) {
        int endIdx = line.indexOf(' ', azIdx + 1);
        if (endIdx > azIdx) {
            currentAz = line.substring(azIdx + 4, endIdx).toFloat();
        } else {
            // Az might be at end of line or followed by Ialt
            int ialtIdx = line.indexOf(" Ialt:");
            if (ialtIdx > azIdx) {
                currentAz = line.substring(azIdx + 4, ialtIdx).toFloat();
            }
        }
    }

    // Extract Ialt:0.15A
    int ialtIdx = line.indexOf("Ialt:");
    if (ialtIdx >= 0) {
        int endIdx = line.indexOf('A', ialtIdx);
        if (endIdx > ialtIdx) {
            altCurrentA = line.substring(ialtIdx + 5, endIdx).toFloat();
        }
    }

    // Extract Iaz:0.20A
    int iazIdx = line.indexOf("Iaz:");
    if (iazIdx >= 0) {
        int endIdx = line.indexOf('A', iazIdx);
        if (endIdx > iazIdx) {
            azCurrentA = line.substring(iazIdx + 4, endIdx).toFloat();
        }
    }

    // Extract Status:Ready or Status:Slewing
    int statusIdx = line.indexOf("Status:");
    if (statusIdx >= 0) {
        int endIdx = line.indexOf(' ', statusIdx + 7);
        if (endIdx < 0) endIdx = line.length();
        statusStr = line.substring(statusIdx + 7, endIdx);
    }

    // Check for fault message [...]
    int faultStart = line.indexOf('[');
    int faultEnd = line.indexOf(']');
    if (faultStart >= 0 && faultEnd > faultStart) {
        faultStr = line.substring(faultStart + 1, faultEnd);
    } else {
        faultStr = "";
    }

    // Check if slewing
    isSlewing = (line.indexOf(" -> ") >= 0) || (statusStr == "Slewing");

    // Extract target from "-> Alt:50.0 Az:200.0"
    int arrowIdx = line.indexOf(" -> ");
    if (arrowIdx >= 0) {
        int tAltIdx = line.indexOf("Alt:", arrowIdx);
        int tAzIdx = line.indexOf(" Az:", arrowIdx);
        if (tAltIdx > arrowIdx && tAzIdx > tAltIdx) {
            targetAlt = line.substring(tAltIdx + 4, tAzIdx).toFloat();
            targetAz = line.substring(tAzIdx + 4).toFloat();
        }
    }

    // Extract calibrator state Cal:ON or Cal:OFF
    int calIdx = line.indexOf("Cal:");
    if (calIdx >= 0) {
        calibratorOn = (line.substring(calIdx + 4, calIdx + 6) == "ON");
    }
}
