// srt_serial.h - Serial communication with Arduino Due

#ifndef SRT_SERIAL_H
#define SRT_SERIAL_H

#include <Arduino.h>

#define SERIAL_LOG_SIZE 30  // Number of log entries to keep

struct SerialLogEntry {
    time_t utcTime;           // UTC timestamp (0 if not synced)
    unsigned long millis;     // millis() as fallback
    char direction;           // 'T' = TX to Due, 'R' = RX from Due, 'E' = ESP32 diagnostic
    String message;
};

class SRTSerial {
public:
    SRTSerial();

    // Initialize UART
    void begin(int txPin, int rxPin, int baudRate);

    // Send commands to Due. The Due works only in DRIVE coordinates, so this
    // takes drive coordinates and nothing else - named for the frame precisely
    // because every caller holds a true sky position a moment earlier and the
    // conversion must be visible at the call site. See pointing.h.
    void sendDriveTarget(float driveAlt, float driveAz);
    void sendHome();
    void sendStop();
    void sendReset();
    void requestStatus();

    // Read status from Due
    bool readStatus();

    // Status getters. Defined out of line in the .cpp because every one of them
    // is called from async_tcp while loopTask is reassigning the members: the
    // String getters must take their copy under the lock, or the copy races a
    // reassignment and reads freed heap.
    float getCurrentAlt();
    float getCurrentAz();
    float getTargetAlt();
    float getTargetAz();
    float getAltCurrentA();
    float getAzCurrentA();
    String getStatusStr();
    String getFaultStr();
    bool getIsSlewing();
    String getLastStatus();

    // Status lines from the Due that failed the whole-line check. Should stay
    // at zero; a rising count means the UART is being flooded and lines are
    // arriving spliced. Surfaced through /status so it shows up as a number
    // rather than as an unexplained glitch in the readout.
    uint32_t getMalformedCount();

    // The encoder error the Due reports at each homing stop (issue #24),
    // parsed from "Homing: <axis> limit reached at N pulses (D deg)" and
    // latched. The read loop drops non-status lines, and the status flood
    // during a homing scrolls the log buffer faster than a reader can poll,
    // so the number is held here and surfaced on /status instead. First
    // approach is the counter at the stop: a fixed switch-detection offset
    // (normally -2.0..+0.5 deg, most often alt -1.0 / az -0.5) plus any count
    // drift since the last homing - the scheduler separates the two
    // (homing_drift). The re-approach after the back-off is the repeatability. NaN until seen.
    String getHomingReportJSON();

    // Drive acknowledgement (issue #34). The Due answers every drive target
    // with "ACK DRIVE <alt> <az>" or "ERR DRIVE <reason>". A target with no
    // answer after DRIVE_ACK_TIMEOUT_MS is sent once more, and counted lost if
    // that goes unanswered too. Re-sending starts only once an ACK has been
    // seen, so a Due flashed before 2026-09-30 is left alone. Counters, the
    // last refusal and whether ACKs are arriving at all go to /status.
    String getDriveAckJSON();

    // Serial log access
    String getLogJSON();
    void logESP(const String &msg);  // Log ESP32 diagnostic message

private:
    void logMessage(char direction, const String &msg);
    void parseStatus(const String &line);
    void handleHomingLine(const String &line);
    void handleDriveReply(const String &line);
    void serviceDriveAck();

    HardwareSerial *uart;
    String lastStatus;
    float currentAlt;
    float currentAz;
    float targetAlt;
    float targetAz;
    float altCurrentA;
    float azCurrentA;
    String statusStr;
    String faultStr;
    bool isSlewing;
    uint32_t spliceCount;

    // Homing error latch (see getHomingReportJSON).
    float homingErrAltFirst;
    float homingErrAzFirst;
    float homingErrAltSecond;
    float homingErrAzSecond;
    bool homingSecondApproach;
    bool homingReapproachSkipped;   // the Due found no need for a re-approach (#33)
    bool homingFromUnknown;         // started without a known position: counters are not drift
    time_t homingReportTime;

    // Drive acknowledgement state (see getDriveAckJSON).
    String drivePendingCmd;         // the line last sent, as sent
    float drivePendingAlt;          // its target rounded to the Due's 0.5 deg grid
    float drivePendingAz;
    unsigned long drivePendingSince;
    bool drivePending;
    bool driveResent;
    bool driveAckSeen;
    uint32_t driveAcks;
    uint32_t driveErrors;
    uint32_t driveResends;
    uint32_t driveLost;
    uint32_t unknownCommands;
    String lastDriveError;

    // Ring buffer for serial log
    SerialLogEntry logBuffer[SERIAL_LOG_SIZE];
    int logHead;  // Next write position
    int logCount; // Number of entries (up to SERIAL_LOG_SIZE)
};

// Global instance
extern SRTSerial srtSerial;

#endif // SRT_SERIAL_H
