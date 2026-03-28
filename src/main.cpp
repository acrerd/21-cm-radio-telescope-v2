/**
 * SRT Drive Controller
 *
 * Controls alt-az drive for Small Radio Telescope.
 * Arduino Due with H-bridge motor drivers and reed switch encoders.
 *
 * Serial Commands:
 *   DRIVE <alt> <az>     - Slew to position (or just: <alt> <az>)
 *   HOME                 - Run homing sequence
 *   STOP                 - Emergency stop
 *   STATUS               - Show current status
 *   CONFIG               - Show all configuration
 *   SET <param> <value>  - Set configuration parameter
 *   SAVE                 - Save configuration to flash
 *   LOAD                 - Load configuration from flash
 *   DEFAULTS             - Reset to factory defaults
 *   HELP                 - Show command help
 *
 * ESP32 Bridge (WT32-ETH01 via Native USB):
 *   Serial output from ESP32 is forwarded to Native USB port for monitoring
 */

#include <Arduino.h>
#include <DueFlashStorage.h>
#include <math.h>
#include "config.h"

// =============================================================================
// ESP32 SERIAL COMMUNICATION (WT32-ETH01)
// =============================================================================
// The Due communicates with the WT32-ETH01 via Serial1 (pins 18/19).
// The Native USB port (SerialUSB) bridges to Serial1 for monitoring.
//
// Hardware connections (directly, no level shifter needed - both 3.3V):
//   Due Pin 18 (TX1) -> WT32-ETH01 IO14 (ESP32 RX)
//   Due Pin 19 (RX1) <- WT32-ETH01 IO4  (ESP32 TX)
//   GND              -- GND
//
// Note: IO4/IO14 are used instead of IO32/IO33 because some WT32-ETH01
// variants have IO32/IO33 labelled CFG/485_EN for RS-485 circuitry.
// =============================================================================

#define ESP_BRIDGE_ENABLED  1       // Set to 0 to disable bridge functionality
#define ESP_BRIDGE_BAUD     115200  // Baud rate for ESP32 serial monitoring

// =============================================================================
// SIMULATION MODE - Override hardware I/O with software stubs
// =============================================================================
//
// When SIMULATION_MODE is defined, all motor control I/O is intercepted by
// shadow variables. simulatePulses() generates position feedback based on
// the commanded PWM speed, closing the control loop without real hardware.
//
// Build with: pio run -e simulation
// =============================================================================

#ifdef SIMULATION_MODE

// Shadow state for motor control outputs
static int simPwmAz = PWM_STOP;
static int simPwmAlt = PWM_STOP;
static bool simDirAz = true;    // true = HIGH (East/Up)
static bool simDirAlt = true;

// Fractional pulse accumulators
static float simAccumAz = 0.0f;
static float simAccumAlt = 0.0f;
static unsigned long simLastUpdateMs = 0;

// Override analogWrite - capture PWM values instead of hitting hardware
static void simAnalogWrite(uint32_t pin, uint32_t val) {
    if (pin == PIN_PWM_AZ)       simPwmAz = val;
    else if (pin == PIN_PWM_ALT) simPwmAlt = val;
}
#define analogWrite(pin, val) simAnalogWrite(pin, val)

// Override digitalWrite - capture direction state, but pass through ESP bridge pins
static void simDigitalWrite(uint32_t pin, uint32_t val) {
    if (pin == PIN_DIR_AZ)       simDirAz = (val == HIGH);
    else if (pin == PIN_DIR_ALT) simDirAlt = (val == HIGH);
    else {
        // Pass through to real hardware for non-motor pins (ESP bridge, etc.)
        PIO_SetOutput(g_APinDescription[pin].pPort, g_APinDescription[pin].ulPin,
                      val, 0, PIO_PULLUP);
    }
}
#define digitalWrite(pin, val) simDigitalWrite(pin, val)

// Override digitalRead - return no-fault state for motor pins, real read for others
static int simDigitalRead(uint32_t pin) {
    if (pin == PIN_FF1_AZ || pin == PIN_FF2_AZ || pin == PIN_FF1_ALT || pin == PIN_FF2_ALT) {
        return LOW;  // Fault flags LOW = no fault
    }
    // Real read for other pins
    return (PIO_Get(g_APinDescription[pin].pPort, PIO_INPUT, g_APinDescription[pin].ulPin) ? HIGH : LOW);
}
#define digitalRead(pin) simDigitalRead(pin)

// Override analogRead - return zero-current ADC value
static int simAnalogRead(uint32_t pin) {
    (void)pin;
    // Return ADC value corresponding to 2.5V offset (zero current)
    return (int)((CURRENT_SENSOR_OFFSET_V / ADC_REFERENCE_V) * ADC_RESOLUTION_BITS);
}
#define analogRead(pin) simAnalogRead(pin)

// Override pinMode - no-op for motor pins, real setup for others (ESP bridge)
static void simPinMode(uint32_t pin, uint32_t mode) {
    // Only actually configure non-motor pins (ESP bridge pins 2, 3, etc.)
    if (pin != PIN_PWM_AZ && pin != PIN_PWM_ALT &&
        pin != PIN_DIR_AZ && pin != PIN_DIR_ALT &&
        pin != PIN_PULSE_AZ && pin != PIN_PULSE_ALT &&
        pin != PIN_FF1_AZ && pin != PIN_FF2_AZ &&
        pin != PIN_FF1_ALT && pin != PIN_FF2_ALT &&
        pin != PIN_RESET_AZ && pin != PIN_RESET_ALT) {
        if (mode == OUTPUT) {
            PIO_Configure(g_APinDescription[pin].pPort, PIO_OUTPUT_0,
                          g_APinDescription[pin].ulPin, g_APinDescription[pin].ulPinConfiguration);
        } else {
            PIO_Configure(g_APinDescription[pin].pPort, PIO_INPUT,
                          g_APinDescription[pin].ulPin, g_APinDescription[pin].ulPinConfiguration);
        }
    }
}
#define pinMode(pin, mode) simPinMode(pin, mode)

#define attachInterrupt(a, b, c)    ((void)0)
#define analogReadResolution(x)     ((void)0)

#endif // SIMULATION_MODE

// Flash storage for configuration
DueFlashStorage flashStorage;

// Active configuration (loaded from flash or defaults)
Config cfg;

// =============================================================================
// FORWARD DECLARATIONS
// =============================================================================

const char* getFaultString();
void outputStatus();
void outputStatusIfChanged();
void updatePrevStatus();

#if ESP_BRIDGE_ENABLED
void setupESPBridge();
void handleESPBridge();
#endif

// =============================================================================
// STATE DEFINITIONS
// =============================================================================

typedef enum {
    STATE_INIT,
    STATE_HOMING,
    STATE_IDLE,
    STATE_DRIVING,
    STATE_FAULT
} SystemState;

typedef enum {
    FAULT_NONE = 0,
    FAULT_AZ_SHORT,
    FAULT_ALT_SHORT,
    FAULT_AZ_OVERTEMP,
    FAULT_ALT_OVERTEMP,
    FAULT_AZ_UNDERVOLT,
    FAULT_ALT_UNDERVOLT,
    FAULT_AZ_OVERCURRENT,
    FAULT_ALT_OVERCURRENT,
    FAULT_AZ_STALL,
    FAULT_ALT_STALL,
    FAULT_AZ_POSITION_BOUNDS,
    FAULT_ALT_POSITION_BOUNDS
} FaultCode;

// =============================================================================
// GLOBAL VARIABLES
// =============================================================================

// System state
volatile SystemState systemState = STATE_INIT;
volatile FaultCode faultCode = FAULT_NONE;

// Position tracking (in pulses, 2 pulses = 1 degree)
volatile int32_t positionAz = 0;    // Current azimuth position
volatile int32_t positionAlt = 0;   // Current altitude position

// Pulse timing for debounce and stall detection
volatile unsigned long lastPulseAz = 0;
volatile unsigned long lastPulseAlt = 0;

// Target position (in pulses)
int32_t targetAz = 0;
int32_t targetAlt = 0;

// Motion state per axis
typedef enum {
    MOTION_IDLE,        // Not moving
    MOTION_DRIVING,     // Moving toward target
    MOTION_STOPPING     // Decelerating to reverse direction
} MotionState;

MotionState motionStateAz = MOTION_IDLE;
MotionState motionStateAlt = MOTION_IDLE;

// Current direction for each axis (true = positive/increasing, false = negative/decreasing)
bool currentDirAz = true;   // true = East (HIGH), false = West (LOW)
bool currentDirAlt = true;  // true = Up (HIGH), false = Down (LOW)

// Timing for ramps
unsigned long driveStartTimeAz = 0;
unsigned long driveStartTimeAlt = 0;
unsigned long stopStartTimeAz = 0;
unsigned long stopStartTimeAlt = 0;

// Speed tracking for smooth stopping (PWM value when stop was initiated)
int stopStartPwmAz = PWM_STOP;
int stopStartPwmAlt = PWM_STOP;

// Serial input buffers (one for each port)
char serialBuffer[64];
int serialIndex = 0;
char lastLineEndChar = 0;  // Track CR/LF to handle CRLF pairs

#if ENABLE_SERIAL1
char serial1Buffer[64];
int serial1Index = 0;
char lastLineEndChar1 = 0;  // Track CR/LF for Serial1
#endif

// Previous state tracking (for change detection)
int32_t prevPositionAz = 0;
int32_t prevPositionAlt = 0;
int32_t prevTargetAz = 0;
int32_t prevTargetAlt = 0;
SystemState prevSystemState = STATE_INIT;
FaultCode prevFaultCode = FAULT_NONE;

// Calibrator state
bool calibratorOn = false;

// =============================================================================
// DUAL SERIAL OUTPUT HELPER
// =============================================================================

// Print to Programming Port (Serial) only
// Serial1 is reserved exclusively for ESP32 bridge - no cross-talk
void printAll(const char* str) {
    Serial.print(str);
}

void printAllLn(const char* str) {
    Serial.println(str);
}

void printAllFloat(float val, int decimals) {
    Serial.print(val, decimals);
}

void printAllInt(int val) {
    Serial.print(val);
}

void printPrompt() {
    printAll("> ");
}

// =============================================================================
// CONFIGURATION MANAGEMENT
// =============================================================================

uint32_t calculateChecksum(const Config* c) {
    uint32_t sum = 0;
    const uint8_t* p = (const uint8_t*)c;
    // Sum all bytes except the checksum field itself
    for (size_t i = 0; i < sizeof(Config) - sizeof(uint32_t); i++) {
        sum += p[i];
    }
    return sum;
}

void loadDefaults() {
    cfg.magic = CONFIG_MAGIC;
    // Hardware limits (physical limit switches)
    cfg.altHwMin = DEFAULT_ALT_HW_MIN;
    cfg.altHwMax = DEFAULT_ALT_HW_MAX;
    cfg.azHwMin = DEFAULT_AZ_HW_MIN;
    cfg.azHwMax = DEFAULT_AZ_HW_MAX;
    // Software limits (operational, inside hardware limits)
    cfg.altMin = DEFAULT_ALT_MIN;
    cfg.altMax = DEFAULT_ALT_MAX;
    cfg.azMin = DEFAULT_AZ_MIN;
    cfg.azMax = DEFAULT_AZ_MAX;
    cfg.homeAlt = DEFAULT_HOME_ALT;
    cfg.homeAz = DEFAULT_HOME_AZ;
    cfg.rampUpMs = DEFAULT_RAMP_UP_MS;
    cfg.rampDownDeg = DEFAULT_RAMP_DOWN_DEG;
    cfg.stopRampMs = DEFAULT_STOP_RAMP_MS;
    cfg.currentLimit = DEFAULT_CURRENT_LIMIT;
    #ifdef SIMULATION_MODE
    cfg.stallTimeoutMs = SIM_STALL_TIMEOUT_MS;  // Faster stall detection in simulation
    #else
    cfg.stallTimeoutMs = DEFAULT_STALL_TIMEOUT;
    #endif
    cfg.checksum = calculateChecksum(&cfg);
}

bool loadConfig() {
    // Read config from flash
    byte* p = flashStorage.readAddress(0);
    memcpy(&cfg, p, sizeof(Config));

    // Validate magic and checksum
    if (cfg.magic != CONFIG_MAGIC) {
        return false;
    }

    uint32_t expectedChecksum = calculateChecksum(&cfg);
    if (cfg.checksum != expectedChecksum) {
        return false;
    }

    return true;
}

void saveConfig() {
    cfg.magic = CONFIG_MAGIC;
    cfg.checksum = calculateChecksum(&cfg);

    // Write config to flash
    byte* data = (byte*)&cfg;
    for (size_t i = 0; i < sizeof(Config); i++) {
        flashStorage.write(i, data[i]);
    }
}

// Helper to get home offset in pulses (calculated from config)
int32_t getHomeAzOffsetPulses() {
    return (int32_t)((cfg.homeAz - cfg.azMin) * PULSES_PER_DEGREE);
}

int32_t getHomeAltOffsetPulses() {
    return (int32_t)((cfg.homeAlt - cfg.altMin) * PULSES_PER_DEGREE);
}

// Helper to get ramp down pulses from degrees
int32_t getRampDownPulses() {
    return (int32_t)(cfg.rampDownDeg * PULSES_PER_DEGREE);
}

// =============================================================================
// INTERRUPT SERVICE ROUTINES
// =============================================================================

void pulseAzISR() {
    unsigned long now = millis();
    if ((now - lastPulseAz) >= DEBOUNCE_MS) {
        // Determine direction from DIR pin state
        if (digitalRead(PIN_DIR_AZ) == HIGH) {
            positionAz++;   // Moving East (increasing)
        } else {
            positionAz--;   // Moving West (decreasing)
        }
    }
    lastPulseAz = now;
}

void pulseAltISR() {
    unsigned long now = millis();
    if ((now - lastPulseAlt) >= DEBOUNCE_MS) {
        // Determine direction from DIR pin state
        if (digitalRead(PIN_DIR_ALT) == HIGH) {
            positionAlt++;  // Moving Up (increasing)
        } else {
            positionAlt--;  // Moving Down (decreasing)
        }
    }
    lastPulseAlt = now;
}

// =============================================================================
// SIMULATION - Pulse Generation
// =============================================================================

#ifdef SIMULATION_MODE
/**
 * Generate simulated encoder pulses based on current motor PWM output.
 * Must be called every loop iteration (including inside performHoming loops).
 *
 * Converts the shadow PWM value to a speed fraction, accumulates fractional
 * pulses over real elapsed time, and updates the volatile position counters.
 * Simulates physical hard stops at the configured position limits - when
 * position reaches a limit, pulses stop (just like a real stalled motor),
 * which naturally triggers the stall detection logic.
 */
void simulatePulses() {
    unsigned long now = millis();
    if (simLastUpdateMs == 0) {
        simLastUpdateMs = now;
        return;
    }

    float dt = (float)(now - simLastUpdateMs) / 1000.0f;
    simLastUpdateMs = now;
    if (dt <= 0.0f || dt > 0.5f) return;  // Guard against timing glitches

    float maxPulseRate = SIM_MAX_SPEED_DEG_S * PULSES_PER_DEGREE;

    // Simulated hard stops at HARDWARE limits (physical limit switches)
    int32_t azLimitLow  = (int32_t)(cfg.azHwMin * PULSES_PER_DEGREE);
    int32_t azLimitHigh = (int32_t)(cfg.azHwMax * PULSES_PER_DEGREE);
    int32_t altLimitLow  = (int32_t)(cfg.altHwMin * PULSES_PER_DEGREE);
    int32_t altLimitHigh = (int32_t)(cfg.altHwMax * PULSES_PER_DEGREE);

    // --- Azimuth axis ---
    float speedAz = (float)(PWM_STOP - simPwmAz) / (float)(PWM_STOP - PWM_FULL_SPEED);
    if (speedAz > 0.0f) {
        simAccumAz += speedAz * maxPulseRate * dt;
        while (simAccumAz >= 1.0f) {
            int32_t nextPos = simDirAz ? (positionAz + 1) : (positionAz - 1);
            if (nextPos < azLimitLow || nextPos > azLimitHigh) {
                simAccumAz = 0.0f;  // Hit simulated hard stop
                break;
            }
            simAccumAz -= 1.0f;
            positionAz = nextPos;
            lastPulseAz = now;
        }
    }

    // --- Altitude axis ---
    float speedAlt = (float)(PWM_STOP - simPwmAlt) / (float)(PWM_STOP - PWM_FULL_SPEED);
    if (speedAlt > 0.0f) {
        simAccumAlt += speedAlt * maxPulseRate * dt;
        while (simAccumAlt >= 1.0f) {
            int32_t nextPos = simDirAlt ? (positionAlt + 1) : (positionAlt - 1);
            if (nextPos < altLimitLow || nextPos > altLimitHigh) {
                simAccumAlt = 0.0f;
                break;
            }
            simAccumAlt -= 1.0f;
            positionAlt = nextPos;
            lastPulseAlt = now;
        }
    }
}
#endif // SIMULATION_MODE

// =============================================================================
// MOTOR CONTROL FUNCTIONS
// =============================================================================

void stopMotorAz() {
    analogWrite(PIN_PWM_AZ, PWM_STOP);
    motionStateAz = MOTION_IDLE;
}

void stopMotorAlt() {
    analogWrite(PIN_PWM_ALT, PWM_STOP);
    motionStateAlt = MOTION_IDLE;
}

void stopAllMotors() {
    stopMotorAz();
    stopMotorAlt();
}

// Helper to check if an axis is moving
bool isMovingAz() { return motionStateAz != MOTION_IDLE; }
bool isMovingAlt() { return motionStateAlt != MOTION_IDLE; }

void enableDrivers() {
    digitalWrite(PIN_RESET_AZ, HIGH);
    digitalWrite(PIN_RESET_ALT, HIGH);
}

void disableDrivers() {
    digitalWrite(PIN_RESET_AZ, LOW);
    digitalWrite(PIN_RESET_ALT, LOW);
}

/**
 * Calculate PWM value based on distance to target and time since start.
 * Uses linear ramp-up and quadratic ramp-down.
 *
 * @param pulsesRemaining Absolute distance to target in pulses
 * @param driveStartTime Time when motion started (millis)
 * @return PWM value (255 = stop, 0 = full speed)
 */
int calculatePWM(int32_t pulsesRemaining, unsigned long driveStartTime) {
    unsigned long elapsed = millis() - driveStartTime;
    int pwmSpeed;
    int32_t rampDownPulses = getRampDownPulses();

    // Ramp-down phase: quadratic deceleration near target
    if (pulsesRemaining <= rampDownPulses && pulsesRemaining > 0) {
        // Speed decreases quadratically as we approach target
        int speedReduction = pulsesRemaining * pulsesRemaining;
        pwmSpeed = 218 - speedReduction;
        if (pwmSpeed < PWM_FULL_SPEED) pwmSpeed = PWM_FULL_SPEED;
        if (pwmSpeed > PWM_MIN_SPEED) pwmSpeed = PWM_MIN_SPEED;
        return pwmSpeed;
    }

    // Ramp-up phase: linear acceleration at start
    if (elapsed < cfg.rampUpMs) {
        // Linear interpolation from PWM_MIN_SPEED to PWM_FULL_SPEED
        float rampFraction = (float)elapsed / (float)cfg.rampUpMs;
        pwmSpeed = PWM_MIN_SPEED - (int)(rampFraction * (PWM_MIN_SPEED - PWM_FULL_SPEED));
        if (pwmSpeed < PWM_FULL_SPEED) pwmSpeed = PWM_FULL_SPEED;
        return pwmSpeed;
    }

    // Cruise phase: full speed
    return PWM_FULL_SPEED;
}

// =============================================================================
// CURRENT SENSING
// =============================================================================

// Auto-calibrated zero-current offsets (set during startup)
float currentOffsetAz = CURRENT_SENSOR_OFFSET_V;
float currentOffsetAlt = CURRENT_SENSOR_OFFSET_V;

void calibrateCurrentSensors() {
    // Average multiple readings with motors off to find true zero offset
    const int samples = 50;
    long sumAz = 0;
    long sumAlt = 0;

    printAllLn("Calibrating current sensors...");

    for (int i = 0; i < samples; i++) {
        sumAz += analogRead(PIN_CURRENT_AZ);
        sumAlt += analogRead(PIN_CURRENT_ALT);
        delay(10);
    }

    float avgAdcAz = (float)sumAz / samples;
    float avgAdcAlt = (float)sumAlt / samples;

    // Convert ADC readings to voltage - this is our zero-current offset
    currentOffsetAz = (ADC_REFERENCE_V / ADC_RESOLUTION_BITS) * avgAdcAz;
    currentOffsetAlt = (ADC_REFERENCE_V / ADC_RESOLUTION_BITS) * avgAdcAlt;

    printAll("  Az offset: ");
    printAllFloat(currentOffsetAz, 3);
    printAllLn("V");
    printAll("  Alt offset: ");
    printAllFloat(currentOffsetAlt, 3);
    printAllLn("V");
}

float readCurrentAz() {
    int adcValue = analogRead(PIN_CURRENT_AZ);
    float voltage = (ADC_REFERENCE_V / ADC_RESOLUTION_BITS) * adcValue;
    float current = (voltage - currentOffsetAz) / CURRENT_SENSOR_SENSITIVITY;
    return current;
}

float readCurrentAlt() {
    int adcValue = analogRead(PIN_CURRENT_ALT);
    float voltage = (ADC_REFERENCE_V / ADC_RESOLUTION_BITS) * adcValue;
    float current = (voltage - currentOffsetAlt) / CURRENT_SENSOR_SENSITIVITY;
    return current;
}

// =============================================================================
// SAFETY CHECKS
// =============================================================================

FaultCode checkFaultFlags() {
    int ff1Az = digitalRead(PIN_FF1_AZ);
    int ff2Az = digitalRead(PIN_FF2_AZ);
    int ff1Alt = digitalRead(PIN_FF1_ALT);
    int ff2Alt = digitalRead(PIN_FF2_ALT);

    // Check azimuth faults
    if (ff1Az == LOW && ff2Az == HIGH) return FAULT_AZ_SHORT;
    if (ff1Az == HIGH && ff2Az == LOW) return FAULT_AZ_OVERTEMP;
    if (ff1Az == HIGH && ff2Az == HIGH) return FAULT_AZ_UNDERVOLT;

    // Check altitude faults
    if (ff1Alt == LOW && ff2Alt == HIGH) return FAULT_ALT_SHORT;
    if (ff1Alt == HIGH && ff2Alt == LOW) return FAULT_ALT_OVERTEMP;
    if (ff1Alt == HIGH && ff2Alt == HIGH) return FAULT_ALT_UNDERVOLT;

    return FAULT_NONE;
}

FaultCode checkCurrentLimits() {
    if (isMovingAz()) {
        float currentAz = fabs(readCurrentAz());
        if (currentAz > cfg.currentLimit) {
            return FAULT_AZ_OVERCURRENT;
        }
    }

    if (isMovingAlt()) {
        float currentAlt = fabs(readCurrentAlt());
        if (currentAlt > cfg.currentLimit) {
            return FAULT_ALT_OVERCURRENT;
        }
    }

    return FAULT_NONE;
}

FaultCode checkStall() {
    unsigned long now = millis();

    // Only check for stall when actively driving (not when stopping to reverse)
    if (motionStateAz == MOTION_DRIVING && (now - lastPulseAz) > cfg.stallTimeoutMs) {
        return FAULT_AZ_STALL;
    }

    if (motionStateAlt == MOTION_DRIVING && (now - lastPulseAlt) > cfg.stallTimeoutMs) {
        return FAULT_ALT_STALL;
    }

    return FAULT_NONE;
}

// Position bounds tolerance - if position exceeds hardware limits by this much,
// something is seriously wrong (encoder noise, broken stop, etc.)
#define POSITION_BOUNDS_TOLERANCE 5.0  // degrees beyond hardware limits = fault

FaultCode checkPositionBounds() {
    // Convert pulse counts to degrees
    float azDeg = positionAz / (float)PULSES_PER_DEGREE;
    float altDeg = positionAlt / (float)PULSES_PER_DEGREE;

    // Check if position exceeds hardware limits + tolerance
    // This catches encoder miscounts, broken hard stops, etc.
    if (azDeg < cfg.azHwMin - POSITION_BOUNDS_TOLERANCE ||
        azDeg > cfg.azHwMax + POSITION_BOUNDS_TOLERANCE) {
        return FAULT_AZ_POSITION_BOUNDS;
    }

    if (altDeg < cfg.altHwMin - POSITION_BOUNDS_TOLERANCE ||
        altDeg > cfg.altHwMax + POSITION_BOUNDS_TOLERANCE) {
        return FAULT_ALT_POSITION_BOUNDS;
    }

    return FAULT_NONE;
}

void runSafetyChecks() {
    FaultCode fault;

    // Check position bounds first (catches encoder/mechanical failures)
    fault = checkPositionBounds();
    if (fault != FAULT_NONE) {
        stopAllMotors();
        faultCode = fault;
        systemState = STATE_FAULT;
        return;
    }

    // Check fault flags from motor drivers
    fault = checkFaultFlags();
    if (fault != FAULT_NONE) {
        stopAllMotors();
        faultCode = fault;
        systemState = STATE_FAULT;
        return;
    }

    // Check current limits
    fault = checkCurrentLimits();
    if (fault != FAULT_NONE) {
        stopAllMotors();
        faultCode = fault;
        systemState = STATE_FAULT;
        return;
    }

    // Check for stalled motors (but not during homing - stall is expected at limits)
    if (systemState == STATE_DRIVING) {
        fault = checkStall();
        if (fault != FAULT_NONE) {
            stopAllMotors();
            faultCode = fault;
            systemState = STATE_FAULT;
            return;
        }
    }
}

// =============================================================================
// POSITION VALIDATION
// =============================================================================

// Two-tier limit system:
// 1. Hardware limits - absolute physical stops, never exceeded
// 2. Software limits - operational limits, can be exceeded by up to TOLERANCE
//
// Normal operation stays within software limits.
// Exceeding software limits by up to TOLERANCE triggers a warning but is allowed.
// Exceeding software limits by more than TOLERANCE, or any hardware limit, is rejected.

bool isValidTarget(float altDeg, float azDeg) {
    // Check HARDWARE limits first (absolute - never exceed)
    if (altDeg < cfg.altHwMin || altDeg > cfg.altHwMax) {
        printAll("ERROR: Altitude ");
        printAllFloat(altDeg, 1);
        printAll(" exceeds hardware limits (");
        printAllFloat(cfg.altHwMin, 1);
        printAll(" to ");
        printAllFloat(cfg.altHwMax, 1);
        printAllLn(" deg)");
        return false;
    }

    if (azDeg < cfg.azHwMin || azDeg > cfg.azHwMax) {
        printAll("ERROR: Azimuth ");
        printAllFloat(azDeg, 1);
        printAll(" exceeds hardware limits (");
        printAllFloat(cfg.azHwMin, 1);
        printAll(" to ");
        printAllFloat(cfg.azHwMax, 1);
        printAllLn(" deg)");
        return false;
    }

    // Check SOFTWARE limits with tolerance
    float altTolMin = cfg.altMin - SOFTWARE_LIMIT_TOLERANCE;
    float altTolMax = cfg.altMax + SOFTWARE_LIMIT_TOLERANCE;
    float azTolMin = cfg.azMin - SOFTWARE_LIMIT_TOLERANCE;
    float azTolMax = cfg.azMax + SOFTWARE_LIMIT_TOLERANCE;

    // Clamp tolerance limits to hardware limits
    if (altTolMin < cfg.altHwMin) altTolMin = cfg.altHwMin;
    if (altTolMax > cfg.altHwMax) altTolMax = cfg.altHwMax;
    if (azTolMin < cfg.azHwMin) azTolMin = cfg.azHwMin;
    if (azTolMax > cfg.azHwMax) azTolMax = cfg.azHwMax;

    // Check altitude against software limits + tolerance
    if (altDeg < altTolMin || altDeg > altTolMax) {
        printAll("ERROR: Altitude ");
        printAllFloat(altDeg, 1);
        printAll(" exceeds software limits + tolerance (");
        printAllFloat(altTolMin, 1);
        printAll(" to ");
        printAllFloat(altTolMax, 1);
        printAllLn(" deg)");
        return false;
    }

    // Check azimuth against software limits + tolerance
    if (azDeg < azTolMin || azDeg > azTolMax) {
        printAll("ERROR: Azimuth ");
        printAllFloat(azDeg, 1);
        printAll(" exceeds software limits + tolerance (");
        printAllFloat(azTolMin, 1);
        printAll(" to ");
        printAllFloat(azTolMax, 1);
        printAllLn(" deg)");
        return false;
    }

    // Warn if exceeding software limits (but within tolerance)
    if (altDeg < cfg.altMin || altDeg > cfg.altMax) {
        printAll("WARNING: Altitude ");
        printAllFloat(altDeg, 1);
        printAll(" outside software limits (");
        printAllFloat(cfg.altMin, 1);
        printAll(" to ");
        printAllFloat(cfg.altMax, 1);
        printAllLn(" deg)");
    }

    if (azDeg < cfg.azMin || azDeg > cfg.azMax) {
        printAll("WARNING: Azimuth ");
        printAllFloat(azDeg, 1);
        printAll(" outside software limits (");
        printAllFloat(cfg.azMin, 1);
        printAll(" to ");
        printAllFloat(cfg.azMax, 1);
        printAllLn(" deg)");
    }

    return true;
}

// =============================================================================
// HOMING SEQUENCE
// =============================================================================

void performHoming() {
    printAllLn("Homing: Driving to limit switches...");

    // Reset pulse timestamps
    lastPulseAz = millis();
    lastPulseAlt = millis();

    // Set direction toward limit switches (LOW = West/Down)
    digitalWrite(PIN_DIR_AZ, LOW);
    digitalWrite(PIN_DIR_ALT, LOW);

    // Start motors at moderate speed
    analogWrite(PIN_PWM_AZ, 100);   // Moderate speed for homing
    analogWrite(PIN_PWM_ALT, 100);

    motionStateAz = MOTION_DRIVING;
    motionStateAlt = MOTION_DRIVING;

    bool azAtLimit = false;
    bool altAtLimit = false;

    // Drive until both axes hit their limit switches (no pulses = at limit)
    while (!azAtLimit || !altAtLimit) {
        unsigned long now = millis();

        // Output status when it changes
        outputStatusIfChanged();

        // Check if Az has stopped (at limit)
        if (!azAtLimit && (now - lastPulseAz) > cfg.stallTimeoutMs) {
            stopMotorAz();
            azAtLimit = true;
            printAllLn("Homing: Azimuth limit switch reached");
        }

        // Check if Alt has stopped (at limit)
        if (!altAtLimit && (now - lastPulseAlt) > cfg.stallTimeoutMs) {
            stopMotorAlt();
            altAtLimit = true;
            printAllLn("Homing: Altitude limit switch reached");
        }

        // Safety check for faults
        FaultCode fault = checkFaultFlags();
        if (fault != FAULT_NONE) {
            stopAllMotors();
            faultCode = fault;
            systemState = STATE_FAULT;
            printAll("Homing ABORTED: ");
            printAllLn(getFaultString());
            return;
        }

        // Check current limits
        fault = checkCurrentLimits();
        if (fault != FAULT_NONE) {
            stopAllMotors();
            faultCode = fault;
            systemState = STATE_FAULT;
            printAll("Homing ABORTED: ");
            printAllLn(getFaultString());
            return;
        }

        delay(10);
        #ifdef SIMULATION_MODE
        simulatePulses();
        #endif
    }

    // At limit switches - reset position counters
    positionAz = 0;
    positionAlt = 0;

    printAllLn("Homing: At limit switches, moving to home position...");

    // Now drive to home position offset
    // Set direction away from limits (HIGH = East/Up)
    digitalWrite(PIN_DIR_AZ, HIGH);
    digitalWrite(PIN_DIR_ALT, HIGH);

    // Reset pulse timestamps
    lastPulseAz = millis();
    lastPulseAlt = millis();

    // Calculate home offsets from config
    int32_t homeAzOffset = getHomeAzOffsetPulses();
    int32_t homeAltOffset = getHomeAltOffsetPulses();

    // Start driving to home offset
    driveStartTimeAz = millis();
    driveStartTimeAlt = millis();
    motionStateAz = (homeAzOffset > 0) ? MOTION_DRIVING : MOTION_IDLE;
    motionStateAlt = (homeAltOffset > 0) ? MOTION_DRIVING : MOTION_IDLE;

    while (motionStateAz != MOTION_IDLE || motionStateAlt != MOTION_IDLE) {
        unsigned long now = millis();

        // Output status when it changes
        outputStatusIfChanged();

        // Update Az motor
        if (motionStateAz == MOTION_DRIVING) {
            int32_t remaining = homeAzOffset - positionAz;
            if (remaining <= 0) {
                stopMotorAz();
            } else {
                int pwm = calculatePWM(remaining, driveStartTimeAz);
                analogWrite(PIN_PWM_AZ, pwm);
            }
        }

        // Update Alt motor
        if (motionStateAlt == MOTION_DRIVING) {
            int32_t remaining = homeAltOffset - positionAlt;
            if (remaining <= 0) {
                stopMotorAlt();
            } else {
                int pwm = calculatePWM(remaining, driveStartTimeAlt);
                analogWrite(PIN_PWM_ALT, pwm);
            }
        }

        // Safety checks
        FaultCode fault = checkFaultFlags();
        if (fault != FAULT_NONE) {
            stopAllMotors();
            faultCode = fault;
            systemState = STATE_FAULT;
            printAll("Homing ABORTED: ");
            printAllLn(getFaultString());
            return;
        }

        fault = checkCurrentLimits();
        if (fault != FAULT_NONE) {
            stopAllMotors();
            faultCode = fault;
            systemState = STATE_FAULT;
            printAll("Homing ABORTED: ");
            printAllLn(getFaultString());
            return;
        }

        // Stall detection - if driving but no pulses, motors aren't connected
        if (motionStateAz == MOTION_DRIVING && (now - lastPulseAz) > cfg.stallTimeoutMs) {
            stopAllMotors();
            faultCode = FAULT_AZ_STALL;
            systemState = STATE_FAULT;
            printAllLn("Homing ABORTED: Azimuth motor not responding");
            return;
        }
        if (motionStateAlt == MOTION_DRIVING && (now - lastPulseAlt) > cfg.stallTimeoutMs) {
            stopAllMotors();
            faultCode = FAULT_ALT_STALL;
            systemState = STATE_FAULT;
            printAllLn("Homing ABORTED: Altitude motor not responding");
            return;
        }

        delay(MAIN_LOOP_DELAY_MS);
        #ifdef SIMULATION_MODE
        simulatePulses();
        #endif
    }

    // Now at home position - set position to home coordinates
    positionAz = (int32_t)round(cfg.homeAz * PULSES_PER_DEGREE);
    positionAlt = (int32_t)round(cfg.homeAlt * PULSES_PER_DEGREE);
    targetAz = positionAz;
    targetAlt = positionAlt;

    printAllLn("");
    printAll("Homing complete. Position: Alt=");
    printAllFloat(cfg.homeAlt, 1);
    printAll(" Az=");
    printAllFloat(cfg.homeAz, 1);
    printAllLn("");
    printAllLn("Ready. Type HELP for commands.");

    // Set state before updating prev status to avoid spurious status output
    systemState = STATE_IDLE;
    updatePrevStatus();

    printPrompt();
}

// =============================================================================
// MOTION CONTROL
// =============================================================================

/**
 * Calculate PWM for stopping ramp (decelerating to reverse).
 * Linear deceleration from current speed to stop.
 */
int calculateStopPWM(int startPwm, unsigned long stopStartTime) {
    unsigned long elapsed = millis() - stopStartTime;

    if (elapsed >= cfg.stopRampMs) {
        return PWM_STOP;  // Fully stopped
    }

    // Linear interpolation from startPwm to PWM_STOP
    float fraction = (float)elapsed / (float)cfg.stopRampMs;
    int pwm = startPwm + (int)(fraction * (PWM_STOP - startPwm));
    return pwm;
}

/**
 * Update motion for a single axis.
 * Handles state transitions: IDLE -> DRIVING -> STOPPING -> IDLE -> DRIVING (reversed)
 */
void updateAxisMotion(
    int32_t target,
    volatile int32_t* position,
    MotionState* motionState,
    bool* currentDir,
    unsigned long* driveStartTime,
    unsigned long* stopStartTime,
    int* stopStartPwm,
    volatile unsigned long* lastPulse,
    int pinPwm,
    int pinDir
) {
    int32_t diff = target - *position;
    bool needsPositiveDir = (diff > 0);  // true = increase position
    int32_t remaining = abs(diff);

    switch (*motionState) {
        case MOTION_IDLE:
            if (remaining > 0) {
                // Start moving toward target
                *currentDir = needsPositiveDir;
                digitalWrite(pinDir, needsPositiveDir ? HIGH : LOW);
                *driveStartTime = millis();
                *lastPulse = millis();
                *motionState = MOTION_DRIVING;
            }
            break;

        case MOTION_DRIVING:
            if (remaining == 0) {
                // Reached target
                analogWrite(pinPwm, PWM_STOP);
                *motionState = MOTION_IDLE;
            } else if (needsPositiveDir != *currentDir) {
                // Target changed direction - need to stop first
                // Capture current PWM for smooth deceleration
                // Estimate current speed based on ramp position
                unsigned long elapsed = millis() - *driveStartTime;
                if (elapsed < cfg.rampUpMs) {
                    // Still ramping up - use current ramp position
                    float rampFraction = (float)elapsed / (float)cfg.rampUpMs;
                    *stopStartPwm = PWM_MIN_SPEED - (int)(rampFraction * (PWM_MIN_SPEED - PWM_FULL_SPEED));
                } else {
                    // At full speed
                    *stopStartPwm = PWM_FULL_SPEED;
                }
                *stopStartTime = millis();
                *motionState = MOTION_STOPPING;
            } else {
                // Continue driving toward target
                int pwm = calculatePWM(remaining, *driveStartTime);
                analogWrite(pinPwm, pwm);
            }
            break;

        case MOTION_STOPPING:
            {
                int pwm = calculateStopPWM(*stopStartPwm, *stopStartTime);
                analogWrite(pinPwm, pwm);

                if (pwm >= PWM_STOP - 5) {  // Close enough to stopped
                    // Now reverse direction and start driving
                    analogWrite(pinPwm, PWM_STOP);

                    // Recalculate direction (target may have changed again)
                    diff = target - *position;
                    remaining = abs(diff);

                    if (remaining > 0) {
                        needsPositiveDir = (diff > 0);
                        *currentDir = needsPositiveDir;
                        digitalWrite(pinDir, needsPositiveDir ? HIGH : LOW);
                        *driveStartTime = millis();
                        *lastPulse = millis();
                        *motionState = MOTION_DRIVING;
                    } else {
                        *motionState = MOTION_IDLE;
                    }
                }
            }
            break;
    }
}

void updateMotion() {
    // Update azimuth axis
    updateAxisMotion(
        targetAz, &positionAz,
        &motionStateAz, &currentDirAz,
        &driveStartTimeAz, &stopStartTimeAz, &stopStartPwmAz,
        &lastPulseAz,
        PIN_PWM_AZ, PIN_DIR_AZ
    );

    // Update altitude axis
    updateAxisMotion(
        targetAlt, &positionAlt,
        &motionStateAlt, &currentDirAlt,
        &driveStartTimeAlt, &stopStartTimeAlt, &stopStartPwmAlt,
        &lastPulseAlt,
        PIN_PWM_ALT, PIN_DIR_ALT
    );

    // Update system state
    if (motionStateAz != MOTION_IDLE || motionStateAlt != MOTION_IDLE) {
        systemState = STATE_DRIVING;
    } else {
        systemState = STATE_IDLE;
    }
}

// =============================================================================
// SERIAL COMMUNICATION
// =============================================================================

const char* getStatusString() {
    switch (systemState) {
        case STATE_INIT:    return "Initializing";
        case STATE_HOMING:  return "Homing";
        case STATE_IDLE:    return "Ready";
        case STATE_DRIVING:
            // Check if either axis is reversing
            if (motionStateAz == MOTION_STOPPING || motionStateAlt == MOTION_STOPPING) {
                return "Reversing";
            }
            return "Slewing";
        case STATE_FAULT:   return "FAULT";
        default:            return "Unknown";
    }
}

const char* getFaultString() {
    switch (faultCode) {
        case FAULT_NONE:            return "None";
        case FAULT_AZ_SHORT:        return "Azimuth motor short circuit";
        case FAULT_ALT_SHORT:       return "Altitude motor short circuit";
        case FAULT_AZ_OVERTEMP:     return "Azimuth motor overheating";
        case FAULT_ALT_OVERTEMP:    return "Altitude motor overheating";
        case FAULT_AZ_UNDERVOLT:    return "Azimuth motor undervoltage";
        case FAULT_ALT_UNDERVOLT:   return "Altitude motor undervoltage";
        case FAULT_AZ_OVERCURRENT:  return "Azimuth motor overcurrent";
        case FAULT_ALT_OVERCURRENT: return "Altitude motor overcurrent";
        case FAULT_AZ_STALL:            return "Azimuth motor stalled";
        case FAULT_ALT_STALL:           return "Altitude motor stalled";
        case FAULT_AZ_POSITION_BOUNDS:  return "Azimuth position out of bounds";
        case FAULT_ALT_POSITION_BOUNDS: return "Altitude position out of bounds";
        default:                        return "Unknown fault";
    }
}

// Check if status has changed since last output
bool statusChanged() {
    // Check position change (round to 0.5 degree to avoid noise)
    int32_t posAzRounded = positionAz / (PULSES_PER_DEGREE / 2);
    int32_t posAltRounded = positionAlt / (PULSES_PER_DEGREE / 2);
    int32_t prevAzRounded = prevPositionAz / (PULSES_PER_DEGREE / 2);
    int32_t prevAltRounded = prevPositionAlt / (PULSES_PER_DEGREE / 2);

    if (posAzRounded != prevAzRounded) return true;
    if (posAltRounded != prevAltRounded) return true;
    if (systemState != prevSystemState) return true;
    if (faultCode != prevFaultCode) return true;
    if (targetAz != prevTargetAz) return true;
    if (targetAlt != prevTargetAlt) return true;

    return false;
}

// Update previous state after outputting status
void updatePrevStatus() {
    prevPositionAz = positionAz;
    prevPositionAlt = positionAlt;
    prevTargetAz = targetAz;
    prevTargetAlt = targetAlt;
    prevSystemState = systemState;
    prevFaultCode = faultCode;
}

void outputStatus() {
    float altDeg = (float)positionAlt / PULSES_PER_DEGREE;
    float azDeg = (float)positionAz / PULSES_PER_DEGREE;
    float currentAlt = readCurrentAlt();
    float currentAz = readCurrentAz();

    // Build status string for both Serial (USB) and Serial1 (ESP32)
    char statusLine[128];
    int pos = 0;

    pos += snprintf(statusLine + pos, sizeof(statusLine) - pos, "Alt:%.1f Az:%.1f",
                    altDeg, azDeg);
    pos += snprintf(statusLine + pos, sizeof(statusLine) - pos, " Ialt:%.2fA Iaz:%.2fA",
                    fabs(currentAlt), fabs(currentAz));
    pos += snprintf(statusLine + pos, sizeof(statusLine) - pos, " Status:%s",
                    getStatusString());

    if (systemState == STATE_FAULT) {
        pos += snprintf(statusLine + pos, sizeof(statusLine) - pos, " [%s]",
                        getFaultString());
    }

    if (systemState == STATE_DRIVING) {
        pos += snprintf(statusLine + pos, sizeof(statusLine) - pos, " -> Alt:%.1f Az:%.1f",
                        (float)targetAlt / PULSES_PER_DEGREE,
                        (float)targetAz / PULSES_PER_DEGREE);
    }

    // Calibrator state
    pos += snprintf(statusLine + pos, sizeof(statusLine) - pos, " Cal:%s",
                    calibratorOn ? "ON" : "OFF");

    // Output to Programming Port (USB)
    Serial.println(statusLine);

    // Output to Serial1 (ESP32) so it can read status
    #if ENABLE_SERIAL1
    Serial1.println(statusLine);
    #endif

    updatePrevStatus();
}

// Output status only if something changed
void outputStatusIfChanged() {
    if (statusChanged()) {
        outputStatus();
    }
}

// Show help message
void showHelp() {
    printAllLn("Commands:");
    printAllLn("  <alt> <az>       - Slew to position (e.g., 45.0 180.0)");
    printAllLn("  DRIVE <alt> <az> - Slew to position");
    printAllLn("  HOME             - Run homing sequence");
    printAllLn("  STOP             - Emergency stop");
    printAllLn("  CAL [ON|OFF]     - Toggle/set calibrator");
    printAllLn("  RESET            - Clear fault and re-home");
    printAllLn("  STATUS           - Show current status");
    printAllLn("  CONFIG           - Show configuration");
    printAllLn("  SET <param> <val>- Set parameter (see below)");
    printAllLn("  SAVE             - Save config to flash");
    printAllLn("  LOAD             - Load config from flash");
    printAllLn("  DEFAULTS         - Reset to factory defaults");
    #if ESP_BRIDGE_ENABLED
    printAllLn("");
    printAllLn("ESP32 Bridge: Serial1 <-> Native USB (monitoring only)");
    #endif
    printAllLn("");
    printAllLn("SET parameters:");
    printAllLn("  ALTHWMIN, ALTHWMAX - Altitude hardware limits (deg)");
    printAllLn("  AZHWMIN, AZHWMAX   - Azimuth hardware limits (deg)");
    printAllLn("  ALTMIN, ALTMAX     - Altitude software limits (deg)");
    printAllLn("  AZMIN, AZMAX       - Azimuth software limits (deg)");
    printAllLn("  HOMEALT, HOMEAZ    - Home position (deg)");
    printAllLn("  RAMPUP           - Accel time (ms)");
    printAllLn("  RAMPDOWN         - Decel distance (deg)");
    printAllLn("  STOPRAMP         - Reversal decel time (ms)");
    printAllLn("  CURRENT          - Current limit (A)");
    printAllLn("  STALL            - Stall timeout (ms)");
}

// Show current configuration with SET parameter names
void showConfig() {
    printAllLn("Configuration (use SET <param> <value>):");
    printAllLn("Hardware limits (physical limit switches):");
    printAll("  ALTHWMIN="); printAllFloat(cfg.altHwMin, 1);
    printAll("  ALTHWMAX="); printAllFloat(cfg.altHwMax, 1); printAllLn(" (deg)");
    printAll("  AZHWMIN="); printAllFloat(cfg.azHwMin, 1);
    printAll("   AZHWMAX="); printAllFloat(cfg.azHwMax, 1); printAllLn(" (deg)");
    printAllLn("Software limits (operational, inside hardware limits):");
    printAll("  ALTMIN="); printAllFloat(cfg.altMin, 1);
    printAll("   ALTMAX="); printAllFloat(cfg.altMax, 1); printAllLn(" (deg)");
    printAll("  AZMIN="); printAllFloat(cfg.azMin, 1);
    printAll("    AZMAX="); printAllFloat(cfg.azMax, 1); printAllLn(" (deg)");
    printAllLn("Other settings:");
    printAll("  HOMEALT="); printAllFloat(cfg.homeAlt, 1);
    printAll(" HOMEAZ="); printAllFloat(cfg.homeAz, 1); printAllLn(" (deg)");
    printAll("  RAMPUP="); printAllInt(cfg.rampUpMs); printAllLn(" (ms)");
    printAll("  RAMPDOWN="); printAllFloat(cfg.rampDownDeg, 1); printAllLn(" (deg)");
    printAll("  STOPRAMP="); printAllInt(cfg.stopRampMs); printAllLn(" (ms)");
    printAll("  CURRENT="); printAllFloat(cfg.currentLimit, 1); printAllLn(" (A)");
    printAll("  STALL="); printAllInt(cfg.stallTimeoutMs); printAllLn(" (ms)");
}

// Execute a drive command
void executeDrive(float alt, float az) {
    if (isValidTarget(alt, az)) {
        if (systemState == STATE_FAULT) {
            printAllLn("ERROR: Cannot slew while in FAULT state. Power cycle to reset.");
        } else if (systemState == STATE_HOMING) {
            printAllLn("ERROR: Cannot slew while homing in progress.");
        } else {
            targetAlt = (int32_t)round(alt * PULSES_PER_DEGREE);
            targetAz = (int32_t)round(az * PULSES_PER_DEGREE);
            printAll("Slewing to Alt:");
            printAllFloat(alt, 1);
            printAll(" Az:");
            printAllFloat(az, 1);
            printAllLn("");
        }
    }
}

// Case-insensitive string comparison
bool strEqualsIgnoreCase(const char* a, const char* b) {
    while (*a && *b) {
        if (toupper(*a) != toupper(*b)) return false;
        a++; b++;
    }
    return *a == *b;
}

// Process SET command
void processSetCommand(const char* param, float value) {
    // Hardware limits
    if (strEqualsIgnoreCase(param, "ALTHWMIN")) {
        cfg.altHwMin = value;
        printAll("Alt hardware min set to "); printAllFloat(value, 1); printAllLn(" deg");
    } else if (strEqualsIgnoreCase(param, "ALTHWMAX")) {
        cfg.altHwMax = value;
        printAll("Alt hardware max set to "); printAllFloat(value, 1); printAllLn(" deg");
    } else if (strEqualsIgnoreCase(param, "AZHWMIN")) {
        cfg.azHwMin = value;
        printAll("Az hardware min set to "); printAllFloat(value, 1); printAllLn(" deg");
    } else if (strEqualsIgnoreCase(param, "AZHWMAX")) {
        cfg.azHwMax = value;
        printAll("Az hardware max set to "); printAllFloat(value, 1); printAllLn(" deg");
    }
    // Software limits
    else if (strEqualsIgnoreCase(param, "ALTMIN")) {
        cfg.altMin = value;
        printAll("Alt software min set to "); printAllFloat(value, 1); printAllLn(" deg");
    } else if (strEqualsIgnoreCase(param, "ALTMAX")) {
        cfg.altMax = value;
        printAll("Alt software max set to "); printAllFloat(value, 1); printAllLn(" deg");
    } else if (strEqualsIgnoreCase(param, "AZMIN")) {
        cfg.azMin = value;
        printAll("Az software min set to "); printAllFloat(value, 1); printAllLn(" deg");
    } else if (strEqualsIgnoreCase(param, "AZMAX")) {
        cfg.azMax = value;
        printAll("Az software max set to "); printAllFloat(value, 1); printAllLn(" deg");
    } else if (strEqualsIgnoreCase(param, "HOMEALT")) {
        cfg.homeAlt = value;
        printAll("Home alt set to "); printAllFloat(value, 1); printAllLn(" deg");
    } else if (strEqualsIgnoreCase(param, "HOMEAZ")) {
        cfg.homeAz = value;
        printAll("Home az set to "); printAllFloat(value, 1); printAllLn(" deg");
    } else if (strEqualsIgnoreCase(param, "RAMPUP")) {
        cfg.rampUpMs = (uint16_t)value;
        printAll("Ramp up set to "); printAllInt(cfg.rampUpMs); printAllLn(" ms");
    } else if (strEqualsIgnoreCase(param, "RAMPDOWN")) {
        cfg.rampDownDeg = value;
        printAll("Ramp down set to "); printAllFloat(value, 1); printAllLn(" deg");
    } else if (strEqualsIgnoreCase(param, "STOPRAMP")) {
        cfg.stopRampMs = (uint16_t)value;
        printAll("Stop ramp set to "); printAllInt(cfg.stopRampMs); printAllLn(" ms");
    } else if (strEqualsIgnoreCase(param, "CURRENT")) {
        cfg.currentLimit = value;
        printAll("Current limit set to "); printAllFloat(value, 1); printAllLn(" A");
    } else if (strEqualsIgnoreCase(param, "STALL")) {
        cfg.stallTimeoutMs = (uint16_t)value;
        printAll("Stall timeout set to "); printAllInt(cfg.stallTimeoutMs); printAllLn(" ms");
    } else {
        printAll("ERROR: Unknown parameter '"); printAll(param); printAllLn("'");
    }
}

// Process a complete command line
void processCommand(const char* buffer) {
    char cmd[16];
    float val1, val2;
    char param[16];

    // Skip leading whitespace
    while (*buffer == ' ') buffer++;

    // Empty command
    if (*buffer == '\0') return;

    // Try parsing as two floats first (simple drive command)
    if (sscanf(buffer, "%f %f", &val1, &val2) == 2) {
        // Check if first token is a number (not a command like DRIVE)
        if (isdigit(buffer[0]) || buffer[0] == '-' || buffer[0] == '.') {
            executeDrive(val1, val2);
            return;
        }
    }

    // Parse command word
    if (sscanf(buffer, "%15s", cmd) != 1) return;

    // Command dispatch
    if (strEqualsIgnoreCase(cmd, "HELP") || cmd[0] == '?') {
        showHelp();
    }
    else if (strEqualsIgnoreCase(cmd, "DRIVE")) {
        if (sscanf(buffer, "%*s %f %f", &val1, &val2) == 2) {
            executeDrive(val1, val2);
        } else {
            printAllLn("Usage: DRIVE <altitude> <azimuth>");
        }
    }
    else if (strEqualsIgnoreCase(cmd, "HOME")) {
        if (systemState == STATE_FAULT) {
            printAllLn("ERROR: Cannot home while in FAULT state. Power cycle to reset.");
        } else if (systemState == STATE_HOMING) {
            printAllLn("Already homing.");
        } else {
            printAllLn("Starting homing sequence...");
            systemState = STATE_HOMING;
            performHoming();
        }
    }
    else if (strEqualsIgnoreCase(cmd, "STOP")) {
        stopAllMotors();
        printAllLn("STOPPED");
    }
    else if (strEqualsIgnoreCase(cmd, "CAL")) {
        // Parse optional ON/OFF argument
        char arg[8];
        if (sscanf(buffer, "%*s %7s", arg) == 1) {
            if (strEqualsIgnoreCase(arg, "ON") || strcmp(arg, "1") == 0) {
                calibratorOn = true;
            } else if (strEqualsIgnoreCase(arg, "OFF") || strcmp(arg, "0") == 0) {
                calibratorOn = false;
            } else {
                printAllLn("Usage: CAL [ON|OFF]");
                return;
            }
        } else {
            // Toggle if no argument
            calibratorOn = !calibratorOn;
        }
        digitalWrite(PIN_CALIBRATOR, calibratorOn ? HIGH : LOW);
        printAll("Calibrator: ");
        printAllLn(calibratorOn ? "ON" : "OFF");
    }
    else if (strEqualsIgnoreCase(cmd, "RESET")) {
        if (systemState != STATE_FAULT) {
            printAllLn("No fault to reset.");
        } else {
            printAllLn("Clearing fault and re-homing...");
            faultCode = FAULT_NONE;
            systemState = STATE_HOMING;
            performHoming();
        }
    }
    else if (strEqualsIgnoreCase(cmd, "STATUS")) {
        outputStatus();
    }
    else if (strEqualsIgnoreCase(cmd, "CONFIG")) {
        showConfig();
    }
    else if (strEqualsIgnoreCase(cmd, "SET")) {
        if (sscanf(buffer, "%*s %15s %f", param, &val1) == 2) {
            processSetCommand(param, val1);
        } else {
            printAllLn("Usage: SET <parameter> <value>");
        }
    }
    else if (strEqualsIgnoreCase(cmd, "SAVE")) {
        saveConfig();
        printAllLn("Configuration saved to flash.");
    }
    else if (strEqualsIgnoreCase(cmd, "LOAD")) {
        if (loadConfig()) {
            printAllLn("Configuration loaded from flash.");
        } else {
            printAllLn("No valid configuration in flash. Using defaults.");
            loadDefaults();
        }
    }
    else if (strEqualsIgnoreCase(cmd, "DEFAULTS")) {
        loadDefaults();
        printAllLn("Configuration reset to defaults.");
    }
    // Debug: force pin 2 LOW/HIGH to test GPIO
    else if (strEqualsIgnoreCase(cmd, "TEST2")) {
        pinMode(2, OUTPUT);
        digitalWrite(2, LOW);
        printAllLn("Pin 2 forced LOW - measure with multimeter");
    }
    else if (strEqualsIgnoreCase(cmd, "TEST2H")) {
        pinMode(2, OUTPUT);
        digitalWrite(2, HIGH);
        printAllLn("Pin 2 forced HIGH - measure with multimeter");
    }
    else {
        // Try as simple two-number command
        if (sscanf(buffer, "%f %f", &val1, &val2) == 2) {
            executeDrive(val1, val2);
        } else {
            printAll("Unknown command: "); printAllLn(cmd);
            printAllLn("Type HELP for commands.");
        }
    }
}

void processSerialInput() {
    // Process USB serial (Programming Port)
    while (Serial.available() > 0) {
        char c = Serial.read();

        if (c == '\n' || c == '\r') {
            // Skip second char of CRLF or LFCR pair
            if ((lastLineEndChar == '\r' && c == '\n') ||
                (lastLineEndChar == '\n' && c == '\r')) {
                lastLineEndChar = 0;
                continue;
            }
            lastLineEndChar = c;

            if (serialIndex > 0) {
                printAllLn("");  // Echo newline
                serialBuffer[serialIndex] = '\0';
                processCommand(serialBuffer);
                serialIndex = 0;
                printPrompt();
            } else {
                // Empty line - just print new prompt
                printAllLn("");
                printPrompt();
            }
        } else if (c == '\b' || c == 127) {
            lastLineEndChar = 0;
            // Handle backspace
            if (serialIndex > 0) {
                serialIndex--;
                printAll("\b \b");  // Erase character on screen
            }
        } else if (serialIndex < (int)(sizeof(serialBuffer) - 1)) {
            lastLineEndChar = 0;
            serialBuffer[serialIndex++] = c;
            // Echo character back to originating port only
            Serial.print(c);
        }
    }

    // Process Serial1 commands (from ESP32 or direct connection)
    #if ENABLE_SERIAL1
    while (Serial1.available() > 0) {
        char c = Serial1.read();

        #if ESP_BRIDGE_ENABLED
        // Forward to SerialUSB for monitoring when bridge is enabled
        SerialUSB.write(c);
        #endif

        if (c == '\n' || c == '\r') {
            if ((lastLineEndChar1 == '\r' && c == '\n') ||
                (lastLineEndChar1 == '\n' && c == '\r')) {
                lastLineEndChar1 = 0;
                continue;
            }
            lastLineEndChar1 = c;

            if (serial1Index > 0) {
                serial1Buffer[serial1Index] = '\0';
                processCommand(serial1Buffer);
                serial1Index = 0;
                #if !ESP_BRIDGE_ENABLED
                Serial1.println();
                Serial1.print("> ");
                #endif
            }
            #if !ESP_BRIDGE_ENABLED
            else {
                Serial1.println();
                Serial1.print("> ");
            }
            #endif
        } else if (c == '\b' || c == 127) {
            lastLineEndChar1 = 0;
            if (serial1Index > 0) {
                serial1Index--;
                #if !ESP_BRIDGE_ENABLED
                Serial1.print("\b \b");
                #endif
            }
        } else if (serial1Index < (int)(sizeof(serial1Buffer) - 1)) {
            lastLineEndChar1 = 0;
            serial1Buffer[serial1Index++] = c;
            #if !ESP_BRIDGE_ENABLED
            Serial1.print(c);  // Echo to Serial1 only when no bridge
            #endif
        }
    }
    #endif
}

// =============================================================================
// ESP32 BRIDGE FUNCTIONS (WT32-ETH01)
// Serial monitoring only - programming done externally via USB-TTL adapter
// =============================================================================

#if ESP_BRIDGE_ENABLED

void setupESPBridge() {
    // Initialize Native USB for ESP32 serial monitoring
    SerialUSB.begin(ESP_BRIDGE_BAUD);
}

// Handle ESP32 serial bridge - forward PC commands to ESP32
// Note: Serial1->SerialUSB forwarding is now done in processSerialInput()
// where we also process commands from ESP32
void handleESPBridge() {
    // Forward Native USB -> Serial1 (PC to ESP32)
    while (SerialUSB.available()) {
        Serial1.write(SerialUSB.read());
    }
}

#endif // ESP_BRIDGE_ENABLED

// =============================================================================
// SETUP AND MAIN LOOP
// =============================================================================

void setup() {
    // Configure fault flag inputs
    pinMode(PIN_FF1_AZ, INPUT);
    pinMode(PIN_FF2_AZ, INPUT);
    pinMode(PIN_FF1_ALT, INPUT);
    pinMode(PIN_FF2_ALT, INPUT);

    // Configure encoder inputs
    pinMode(PIN_PULSE_AZ, INPUT);
    pinMode(PIN_PULSE_ALT, INPUT);

    // Configure and initialize reset outputs (enable drivers)
    pinMode(PIN_RESET_AZ, OUTPUT);
    pinMode(PIN_RESET_ALT, OUTPUT);
    enableDrivers();

    // Configure and initialize PWM outputs (motors stopped)
    pinMode(PIN_PWM_AZ, OUTPUT);
    pinMode(PIN_PWM_ALT, OUTPUT);
    stopAllMotors();

    // Configure and initialize direction outputs
    pinMode(PIN_DIR_AZ, OUTPUT);
    pinMode(PIN_DIR_ALT, OUTPUT);
    digitalWrite(PIN_DIR_AZ, HIGH);
    digitalWrite(PIN_DIR_ALT, HIGH);

    // Configure calibrator output (off by default)
    pinMode(PIN_CALIBRATOR, OUTPUT);
    digitalWrite(PIN_CALIBRATOR, LOW);
    calibratorOn = false;

    // Attach interrupts for position sensing
    attachInterrupt(digitalPinToInterrupt(PIN_PULSE_AZ), pulseAzISR, FALLING);
    attachInterrupt(digitalPinToInterrupt(PIN_PULSE_ALT), pulseAltISR, FALLING);

    // Configure ADC for 12-bit resolution
    analogReadResolution(12);

    // Initialize serial communication
    Serial.begin(SERIAL_BAUD);
    while (!Serial) {
        ; // Wait for serial port (Programming Port USB)
    }

    #if ENABLE_SERIAL1
    // Initialize Serial1 for ESP32 connection (TX1=pin 18, RX1=pin 19)
    Serial1.begin(ESP_BRIDGE_BAUD);  // 460800 for fast ESP32 programming
    #endif

    #if ESP_BRIDGE_ENABLED
    // Initialize ESP32 bridge (Native USB <-> Serial1)
    setupESPBridge();
    #endif

    // Load configuration from flash (or use defaults)
    if (!loadConfig()) {
        loadDefaults();
    }

    printAllLn("=================================");
    printAllLn("SRT Drive Controller v1.2");
    printAllLn("Acre Road Observatory, Glasgow");
    #ifdef SIMULATION_MODE
    printAllLn("*** SIMULATION MODE ***");
    #endif
    #if ESP_BRIDGE_ENABLED
    printAllLn("ESP32 Bridge: Native USB <-> Serial1");
    #endif
    printAllLn("=================================");
    printAllLn("");
    printAllLn("Type HELP for commands.");
    printAllLn("");
    #ifdef SIMULATION_MODE
    // Set initial simulated position (as if telescope is at an arbitrary position)
    positionAz = (int32_t)(SIM_INITIAL_AZ_DEG * PULSES_PER_DEGREE);
    positionAlt = (int32_t)(SIM_INITIAL_ALT_DEG * PULSES_PER_DEGREE);
    simLastUpdateMs = millis();
    #endif

    // Calibrate current sensors while motors are off
    calibrateCurrentSensors();

    printAllLn("Starting homing sequence...");

    // Begin homing sequence
    systemState = STATE_HOMING;
    performHoming();
}

void loop() {
    #if ESP_BRIDGE_ENABLED
    // Bridge Native USB <-> Serial1 (ESP32) - runs every loop, always active
    handleESPBridge();
    #endif

    // Safety checks (always run)
    runSafetyChecks();

    // Process serial commands
    processSerialInput();

    // Update motor control (if not in fault state)
    if (systemState != STATE_FAULT && systemState != STATE_HOMING) {
        updateMotion();
    }

    #ifdef SIMULATION_MODE
    simulatePulses();
    #endif

    // Output status only when something changes
    outputStatusIfChanged();

    delay(MAIN_LOOP_DELAY_MS);
}
