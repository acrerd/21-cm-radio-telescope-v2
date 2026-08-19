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
    void sendCalibrator(bool on);
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
    bool getCalibratorOn();
    String getLastStatus();

    // Status lines from the Due that failed the whole-line check. Should stay
    // at zero; a rising count means the UART is being flooded and lines are
    // arriving spliced. Surfaced through /status so it shows up as a number
    // rather than as an unexplained glitch in the readout.
    uint32_t getMalformedCount();

    // Serial log access
    String getLogJSON();
    void logESP(const String &msg);  // Log ESP32 diagnostic message

private:
    void logMessage(char direction, const String &msg);
    void parseStatus(const String &line);

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
    bool calibratorOn;
    uint32_t spliceCount;

    // Ring buffer for serial log
    SerialLogEntry logBuffer[SERIAL_LOG_SIZE];
    int logHead;  // Next write position
    int logCount; // Number of entries (up to SERIAL_LOG_SIZE)
};

// Global instance
extern SRTSerial srtSerial;

#endif // SRT_SERIAL_H
