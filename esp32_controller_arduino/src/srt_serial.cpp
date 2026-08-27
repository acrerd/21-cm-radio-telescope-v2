// srt_serial.cpp - Serial communication with Arduino Due

#include "srt_serial.h"
#include "sync.h"

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
    spliceCount(0),
    homingErrAltFirst(NAN),
    homingErrAzFirst(NAN),
    homingErrAltSecond(NAN),
    homingErrAzSecond(NAN),
    homingSecondApproach(false),
    homingReportTime(0),
    logHead(0),
    logCount(0) {
}

void SRTSerial::logMessage(char direction, const String &msg) {
    // Entries are reassigned at >2 Hz on loopTask while /serial/log iterates
    // them on async_tcp, and the UI polls that endpoint every second.
    SRTLock lock;
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
    SRTLock lock;
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

// Status getters. The scalar ones are 32-bit and atomic in themselves, but they
// are still taken under the lock so that a caller reading several of them sees
// one consistent status line rather than a mix of two.
float SRTSerial::getCurrentAlt()  { SRTLock lock; return currentAlt; }
float SRTSerial::getCurrentAz()   { SRTLock lock; return currentAz; }
float SRTSerial::getTargetAlt()   { SRTLock lock; return targetAlt; }
float SRTSerial::getTargetAz()    { SRTLock lock; return targetAz; }
float SRTSerial::getAltCurrentA() { SRTLock lock; return altCurrentA; }
float SRTSerial::getAzCurrentA()  { SRTLock lock; return azCurrentA; }
bool  SRTSerial::getIsSlewing()   { SRTLock lock; return isSlewing; }
bool  SRTSerial::getCalibratorOn(){ SRTLock lock; return calibratorOn; }

// The String getters are the ones that matter: the copy is made while the lock
// is held, so it cannot race a reassignment in parseStatus().
String SRTSerial::getStatusStr()  { SRTLock lock; return statusStr; }
String SRTSerial::getFaultStr()   { SRTLock lock; return faultStr; }
uint32_t SRTSerial::getMalformedCount() {
    SRTLock lock;
    return spliceCount;
}

// Parse "Homing: <axis> limit reached at N pulses (D deg)" and the two phase
// markers, latching the reported error. The lock is already held by the read
// loop's caller path via logMessage's scope? No - handleHomingLine is called
// from processSerialData without the lock, and /status reads under one, so
// take it here.
void SRTSerial::handleHomingLine(const String &line) {
    SRTLock lock;
    if (line.indexOf("Drive to limits") >= 0) {
        // A new homing begins: clear the latch and start on the first approach.
        homingErrAltFirst = NAN; homingErrAzFirst = NAN;
        homingErrAltSecond = NAN; homingErrAzSecond = NAN;
        homingSecondApproach = false;
        return;
    }
    if (line.indexOf("Re-approach") >= 0) {
        homingSecondApproach = true;
        return;
    }
    int paren = line.indexOf('(');
    if (line.indexOf("limit reached at ") < 0 || paren < 0) return;
    float deg = line.substring(paren + 1).toFloat();      // "-1.50 deg)" -> -1.50
    bool az = line.indexOf("Azimuth") >= 0;
    if (homingSecondApproach) {
        if (az) homingErrAzSecond = deg; else homingErrAltSecond = deg;
    } else {
        if (az) homingErrAzFirst = deg; else homingErrAltFirst = deg;
    }
    homingReportTime = time(nullptr);
}

String SRTSerial::getHomingReportJSON() {
    SRTLock lock;
    if (homingReportTime == 0) return "null";
    auto num = [](float v) -> String {
        return isnan(v) ? String("null") : String(v, 2);
    };
    String j = "{";
    j += "\"alt_error_first_deg\":" + num(homingErrAltFirst) + ",";
    j += "\"az_error_first_deg\":" + num(homingErrAzFirst) + ",";
    j += "\"alt_error_second_deg\":" + num(homingErrAltSecond) + ",";
    j += "\"az_error_second_deg\":" + num(homingErrAzSecond) + ",";
    j += "\"utc\":" + String((unsigned long)homingReportTime);
    j += "}";
    return j;
}

String SRTSerial::getLastStatus() { SRTLock lock; return lastStatus; }

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

    // The default 256-byte RX buffer holds only about four status lines. Headroom
    // rather than a fix - the Due is now rate-limited at source - but it means a
    // burst (homing, or a firmware that floods again) is buffered instead of
    // being dropped mid-line and spliced. Must precede begin().
    uart->setRxBufferSize(1024);
    uart->begin(baudRate, SERIAL_8N1, rxPin, txPin);
    uart->setTimeout(10);  // 10ms timeout instead of default 1000ms
    Serial.println("SRTSerial: Serial2 initialized");

    // Send test message
    uart->println("ESP32_HELLO");
    Serial.println("SRTSerial: Sent test message");
}

// Every send below holds the lock across the whole write. println() is two
// separate writes - payload, then CRLF - so without this the 1 Hz STATUS poll
// on loopTask can splice into a goto issued from async_tcp: the Due sees
// "STATUS45.0 180.0", drops it, and the slew is silently lost. A mangled but
// still parseable pair of numbers would be worse, commanding a wrong position.

void SRTSerial::sendDriveTarget(float driveAlt, float driveAz) {
    if (uart) {
        char cmd[32];
        snprintf(cmd, sizeof(cmd), "%.1f %.1f", driveAlt, driveAz);
        SRTLock lock;
        logMessage('T', cmd);
        uart->println(cmd);
    }
}

void SRTSerial::sendHome() {
    if (uart) {
        SRTLock lock;
        logMessage('T', "HOME");
        uart->println("HOME");
    }
}

void SRTSerial::sendStop() {
    if (uart) {
        SRTLock lock;
        logMessage('T', "STOP");
        uart->println("STOP");
    }
}

void SRTSerial::sendReset() {
    if (uart) {
        SRTLock lock;
        logMessage('T', "RESET");
        uart->println("RESET");
    }
}

void SRTSerial::sendCalibrator(bool on) {
    if (uart) {
        SRTLock lock;
        logMessage('T', on ? "CAL ON" : "CAL OFF");
        uart->println(on ? "CAL ON" : "CAL OFF");
    }
}

void SRTSerial::requestStatus() {
    // Don't log STATUS requests - too noisy (every second)
    if (uart) {
        SRTLock lock;
        uart->println("STATUS");
    }
}

// Deliberately does not take the lock around the read loop. readStringUntil()
// blocks for up to the 10 ms timeout per line and up to 5 lines are read, so
// holding the lock here could stall an async_tcp handler for 50 ms - the exact
// failure this locking is meant to prevent. It is safe unlocked: RX is only
// ever serviced from loopTask, and the writes it races are on the TX side.
// logMessage() and parseStatus() take the lock themselves for the shared state.
// Count non-overlapping occurrences of needle. Used to spot spliced lines: the
// markers below appear exactly once in a well-formed status line, so a second
// one means two lines have been welded together by dropped bytes.
static int countOccurrences(const String &haystack, const String &needle) {
    int count = 0, at = 0;
    const int step = needle.length();
    while ((at = haystack.indexOf(needle, at)) >= 0) {
        count++;
        at += step;
    }
    return count;
}

// Is this a whole, single status line?
//
//   Alt:%.1f Az:%.1f Ialt:%.1fA Iaz:%.1fA Status:<state> [<fault>] -> Alt:%.1f Az:%.1f Cal:ON|OFF
//
// The old check - starts with "Alt:", contains " Az:", contains "Status:" - was
// satisfied by a splice like "...Status:R" + "Iaz:-0.0A Cal:OFF", which then
// parsed and put "RIaz:-0.0A" on the web UI. "Alt:" and " Az:" legitimately
// appear twice when slewing, because of the " -> " target, so they cannot be
// counted; Ialt/Iaz/Status/Cal appear exactly once in every form of the line.
static bool statusLineLooksIntact(const String &line) {
    if (!line.startsWith("Alt:")) return false;
    if (line.indexOf(" Az:") < 0) return false;
    static const char *markers[] = {"Ialt:", "Iaz:", "Status:", "Cal:"};
    for (const char *marker : markers) {
        // Once on its own catches a second copy welded in by a splice - the
        // real case was "Status:R" joined to "Iaz:-0.0A", giving two "Iaz:".
        if (countOccurrences(line, marker) != 1) return false;
        // Once with its leading space catches the other direction, where the
        // preceding field was eaten: "Az:12Ialt:0.0A" has one "Ialt:" but no
        // " Ialt:".
        if (countOccurrences(line, String(" ") + marker) != 1) return false;
    }
    // Cal is always last, so a line truncated anywhere fails here.
    return line.endsWith(" Cal:ON") || line.endsWith(" Cal:OFF");
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

        if (statusLineLooksIntact(line)) {
            lastValidLine = line;
            logMessage('R', line);  // Log valid status lines
        } else if (line.startsWith("Homing:")) {
            // The Due's homing progress lines are not status lines and were
            // being dropped as junk (issue #24). Log them, and latch the
            // count error they carry so /status can report it after the
            // status flood has scrolled the line out of the log buffer.
            logMessage('R', line);
            handleHomingLine(line);
        } else if (line.length() > 0) {
            // Kept visible rather than dropped silently: a run of these is the
            // signature of the UART being flooded again.
            spliceCount++;
        }
    }

    if (lastValidLine.length() > 0) {
        parseStatus(lastValidLine);
        return true;
    }
    return false;
}

void SRTSerial::parseStatus(const String &line) {
    // Reassigns statusStr/faultStr/lastStatus once per second on loopTask while
    // /status copies them on async_tcp. Held for the whole parse so a reader
    // never sees a half-updated set of fields either.
    SRTLock lock;
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
