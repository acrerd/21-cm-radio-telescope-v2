#pragma once
#include <Arduino.h>
class Preferences {
public:
    bool begin(const char *, bool = false) { return false; }
    void end() {}
    bool isKey(const char *) { return false; }
    void clear() {}
    unsigned getUInt(const char *, unsigned d = 0) { return d; }
    float getFloat(const char *, float d = 0) { return d; }
    String getString(const char *, const char *d = "") { return String(d); }
    void putUInt(const char *, unsigned) {}
    void putFloat(const char *, float) {}
    void putString(const char *, const String &) {}
};
