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

    // Send commands to Due
    void sendTarget(float alt, float az);
    void sendHome();
    void sendStop();
    void sendReset();
    void sendCalibrator(bool on);
    void requestStatus();

    // Read status from Due
    bool readStatus();

    // Status getters
    float getCurrentAlt() { return currentAlt; }
    float getCurrentAz() { return currentAz; }
    float getTargetAlt() { return targetAlt; }
    float getTargetAz() { return targetAz; }
    float getAltCurrentA() { return altCurrentA; }
    float getAzCurrentA() { return azCurrentA; }
    String getStatusStr() { return statusStr; }
    String getFaultStr() { return faultStr; }
    bool getIsSlewing() { return isSlewing; }
    bool getCalibratorOn() { return calibratorOn; }
    String getLastStatus() { return lastStatus; }

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

    // Ring buffer for serial log
    SerialLogEntry logBuffer[SERIAL_LOG_SIZE];
    int logHead;  // Next write position
    int logCount; // Number of entries (up to SERIAL_LOG_SIZE)
};

// Global instance
extern SRTSerial srtSerial;

#endif // SRT_SERIAL_H
