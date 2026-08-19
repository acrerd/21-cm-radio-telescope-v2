#pragma once
#include <string>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cctype>
#include <cstdint>
#define DEG_TO_RAD 0.017453292519943295
#define RAD_TO_DEG 57.29577951308232
#define PROGMEM
struct String : std::string {
    String() {}
    String(const char *s) : std::string(s ? s : "") {}
    String(const std::string &s) : std::string(s) {}
    String(double v, int dp) { char b[64]; snprintf(b, sizeof(b), "%.*f", dp, v); assign(b); }
    unsigned length() const { return (unsigned)size(); }
    char charAt(int i) const { return (*this)[i]; }
    const char *c_str() const { return std::string::c_str(); }
    int indexOf(const String &s, int from = 0) const { auto p = find(s, from); return p == npos ? -1 : (int)p; }
    int indexOf(char c, int from = 0) const { auto p = find(c, from); return p == npos ? -1 : (int)p; }
    String substring(int a, int b) const { return String(std::string(*this, a, b - a)); }
    String &operator+=(char c) { push_back(c); return *this; }
    String &operator+=(const char *s) { append(s); return *this; }
    String &operator+=(const String &s) { append(s); return *this; }
};
inline String operator+(const String &a, const char *b) { String r(a); r += b; return r; }
inline String operator+(const char *a, const String &b) { String r(a); r += b; return r; }
inline String operator+(const String &a, const String &b) { String r(a); r += b; return r; }
struct SerialShim {
    void println(const char *s = "") { printf("%s\n", s); }
    void printf(const char *f, ...) {}
};
extern SerialShim Serial;
