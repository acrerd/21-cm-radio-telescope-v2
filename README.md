# SRT Drive Controller

Firmware for the Small Radio Telescope alt-azimuth drive system at Acre Road Observatory, Glasgow.

## Hardware

- **Microcontroller:** Arduino Due (ARM Cortex-M3)
- **Motors:** DC motors with H-bridge drivers
- **Position sensing:** Reed switch encoders (0.5° resolution)
- **Current sensing:** ACS712 hall-effect sensors

## Features

- Automatic homing on startup
- Smooth motion with acceleration/deceleration ramps
- Real-time position and current reporting (1 Hz)
- Overcurrent and stall protection
- Persistent configuration storage in flash
- Dual serial interface (USB + UART for ESP32)

## Quick Start

### Build and Upload

```bash
# Install PlatformIO CLI if needed
pip install platformio

# Build
pio run

# Upload to Arduino Due
pio run --target upload

# Open serial monitor
pio device monitor
```

### Basic Commands

| Command | Description |
|---------|-------------|
| `45 180` | Slew to Alt=45°, Az=180° |
| `home` | Run homing sequence |
| `stop` | Emergency stop |
| `reset` | Clear fault and re-home |
| `status` | Show current position |
| `config` | Show configuration |
| `help` | List all commands |

### Configuration

```
set current 4.5    # Set current limit to 4.5A
set homeaz 175     # Set home azimuth to 175°
save               # Save to flash
```

## Documentation

See [docs/SRT_DRIVE_MANUAL.md](docs/SRT_DRIVE_MANUAL.md) for the complete operations manual.

## Pin Assignments

| Function | Pin |
|----------|-----|
| Az PWM | 8 |
| Az Direction | 9 |
| Alt PWM | 10 |
| Alt Direction | 11 |
| Az Encoder | 12 |
| Alt Encoder | 13 |
| Az Current | A1 |
| Alt Current | A0 |
| Serial1 TX | 18 |
| Serial1 RX | 19 |

## License

MIT License - Acre Road Observatory, University of Glasgow
