// Host-side definitions for the two globals that pointing.cpp expects the
// firmware to provide. Only the storage is needed - a test that exercises the
// mount limits sets the fields it cares about directly.
#include <Arduino.h>
#include "settings.h"

SerialShim Serial;
Settings settings;
