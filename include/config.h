/**
 * SRT Drive Controller - Configuration
 *
 * All adjustable parameters are defined here for easy in-situ modification.
 */

#ifndef CONFIG_H
#define CONFIG_H

// =============================================================================
// PIN DEFINITIONS (Arduino Due)
// =============================================================================

// Motor driver fault flags (inputs, active HIGH after inversion)
#define PIN_FF2_AZ      4       // Azimuth Fault Flag 2 (purple)
#define PIN_FF1_AZ      5       // Azimuth Fault Flag 1 (blue)
#define PIN_FF2_ALT     6       // Altitude Fault Flag 2 (purple)
#define PIN_FF1_ALT     7       // Altitude Fault Flag 1 (blue)

// Motor driver reset (outputs, HIGH = enabled)
#define PIN_RESET_AZ    22      // Azimuth driver reset (black)
#define PIN_RESET_ALT   24      // Altitude driver reset (white)

// Motor PWM speed control (outputs, INVERTED: 255 = stop, 0 = full speed)
#define PIN_PWM_AZ      8       // Azimuth motor PWM (green)
#define PIN_PWM_ALT     10      // Altitude motor PWM (green)

// Motor direction control (outputs, INVERTED at driver)
// LOW = West/Down, HIGH = East/Up
#define PIN_DIR_AZ      9       // Azimuth direction (yellow)
#define PIN_DIR_ALT     11      // Altitude direction (yellow)

// Position encoder pulse inputs (2 pulses per degree, FALLING edge)
#define PIN_PULSE_AZ    12      // Azimuth encoder (blue)
#define PIN_PULSE_ALT   13      // Altitude encoder (orange)

// Current sensing (analog inputs, 12-bit ADC)
#define PIN_CURRENT_AZ  A1      // Azimuth current sensor (black)
#define PIN_CURRENT_ALT A0      // Altitude current sensor (white)

// =============================================================================
// DEFAULT VALUES - These are loaded into config struct at startup
// =============================================================================

// Hardware limits (degrees) - physical limit switches
#define DEFAULT_ALT_HW_MIN      0.0     // Altitude lower hardware limit (limit switch)
#define DEFAULT_ALT_HW_MAX      90.0    // Altitude upper hardware limit (zenith)
#define DEFAULT_AZ_HW_MIN       0.0     // Azimuth lower hardware limit (limit switch)
#define DEFAULT_AZ_HW_MAX       355.0   // Azimuth upper hardware limit (limit switch)

// Software limits (degrees) - operational limits, inside hardware limits
// Normal operation stays within these. They provide safety margin from hardware stops.
#define DEFAULT_ALT_MIN         0.0     // Altitude lower software limit
#define DEFAULT_ALT_MAX         90.0    // Altitude upper software limit
#define DEFAULT_AZ_MIN          2.0     // Azimuth lower software limit (2 deg inside HW)
#define DEFAULT_AZ_MAX          353.0   // Azimuth upper software limit (2 deg inside HW)

// Software limit tolerance (degrees) - how far past software limits before hard stop
// Allows brief excursions up to 4 degrees beyond software limits (but not beyond hardware)
#define SOFTWARE_LIMIT_TOLERANCE 4.0

// Home position (degrees)
#define DEFAULT_HOME_ALT        0.0     // Altitude home position
#define DEFAULT_HOME_AZ         180.0   // Azimuth home position

// Motion control
#define DEFAULT_RAMP_UP_MS      500     // Time to reach full speed (ms)
#define DEFAULT_RAMP_DOWN_DEG   7.0     // Start decel this many degrees before target
#define DEFAULT_STOP_RAMP_MS    300     // Time to stop before reversing (ms)

// Safety
#define DEFAULT_CURRENT_LIMIT   5.0     // Stop motor if current exceeds this (Amps)
#define DEFAULT_STALL_TIMEOUT   2000    // No pulses for this long = stalled (ms)

// =============================================================================
// ENCODER CONFIGURATION (fixed, not user-adjustable)
// =============================================================================

#define PULSES_PER_DEGREE   2       // Encoder resolution
#define DEBOUNCE_MS         15      // Minimum time between valid pulses

// =============================================================================
// PWM VALUES (fixed, hardware-dependent)
// =============================================================================

#define PWM_STOP            255     // Motor stopped (inverted logic)
#define PWM_FULL_SPEED      0       // Motor at maximum speed
#define PWM_MIN_SPEED       220     // Minimum speed during ramp

// =============================================================================
// CURRENT SENSOR CALIBRATION (fixed, hardware-dependent)
// =============================================================================

#define CURRENT_SENSOR_OFFSET_V     2.5     // Zero-current voltage
#define CURRENT_SENSOR_SENSITIVITY  0.066   // V/A (66mV/A)
#define ADC_REFERENCE_V             5.0     // Scaled to match old code
#define ADC_RESOLUTION_BITS         4096    // 12-bit ADC (2^12)

// =============================================================================
// CONFIGURATION STRUCTURE - Stored in flash
// =============================================================================

#define CONFIG_MAGIC        0x53525431  // "SRT1" - to validate stored config

typedef struct {
    uint32_t magic;             // Magic number to validate config

    // Hardware limits (degrees) - physical limit switches
    float altHwMin;
    float altHwMax;
    float azHwMin;
    float azHwMax;

    // Software limits (degrees) - operational limits, inside hardware limits
    float altMin;
    float altMax;
    float azMin;
    float azMax;

    // Home position (degrees)
    float homeAlt;
    float homeAz;

    // Motion control
    uint16_t rampUpMs;          // Acceleration time
    float rampDownDeg;          // Deceleration distance in degrees
    uint16_t stopRampMs;        // Reversal deceleration time

    // Safety
    float currentLimit;         // Overcurrent threshold (Amps)
    uint16_t stallTimeoutMs;    // Stall detection timeout

    uint32_t checksum;          // Simple checksum for validation
} Config;

// =============================================================================
// SERIAL COMMUNICATION
// =============================================================================

#define SERIAL_BAUD         115200
#define STATUS_INTERVAL_MS  1000    // Status output every 1 second

// Serial1 pins for ESP32 connection (active LOW logic levels - use level shifter!)
// TX1 = Pin 18, RX1 = Pin 19
#define ENABLE_SERIAL1      1       // Set to 0 to disable Serial1

// =============================================================================
// TIMING
// =============================================================================

#define MAIN_LOOP_DELAY_MS  10      // Main loop period

// =============================================================================
// SIMULATION MODE CONFIGURATION
// =============================================================================

#ifdef SIMULATION_MODE

#define SIM_MAX_SPEED_DEG_S     18.0    // Simulated max motor speed (degrees/second)
#define SIM_INITIAL_AZ_DEG      180.0   // Starting azimuth before homing (degrees)
#define SIM_INITIAL_ALT_DEG     45.0    // Starting altitude before homing (degrees)

#endif // SIMULATION_MODE

#endif // CONFIG_H
