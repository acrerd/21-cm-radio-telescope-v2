// sync.h - Cross-task locking for state shared by loopTask and async_tcp
//
// ESPAsyncWebServer request handlers and the Stellarium AsyncTCP callbacks all
// run on the FreeRTOS `async_tcp` task. Tracking, Due serial polling and the
// clock status run on the Arduino `loopTask`. Both touch the same SRTState,
// Settings and SRTSerial members, so every such access needs a lock.
//
// One recursive mutex covers all of it. A single lock has no acquisition order
// to get wrong, and recursion means a locked method may call another locked
// method without deadlocking - SRTSerial::sendTarget() calling logMessage() is
// exactly that case.
//
// The rule for using it: critical sections stay short and never block. Do not
// hold this across a network wait, a delay, or a blocking UART read; an
// async_tcp handler stalled behind the lock is the very problem this file
// exists to avoid causing. Bounded UART writes are fine and are the point -
// they are what makes a command atomic against the 1 Hz status poll.

#ifndef SYNC_H
#define SYNC_H

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

extern SemaphoreHandle_t srtMutex;

// Count of lock acquisitions that timed out. Should always be zero; it is
// exposed through /status so that a locking mistake shows up as a number
// instead of as an unexplained glitch.
extern volatile uint32_t srtLockTimeouts;

// How long to wait before giving up on the lock. Every critical section here is
// microseconds to low milliseconds, so this can only expire if something is
// genuinely wrong.
#define SRT_LOCK_TIMEOUT_MS 2000

// Must be called before any other subsystem starts. Locking before this runs
// is a no-op rather than a crash, which keeps very early boot logging safe.
void srtSyncInit();

// RAII guard. Construct one at the top of a critical section; it releases when
// it goes out of scope, including on an early return.
//
// The wait is bounded rather than infinite on purpose. This board is recovered
// over OTA, which needs loopTask alive to run ArduinoOTA.handle(); a permanent
// block would leave only the FT232-and-buttons procedure. On timeout the caller
// proceeds unlocked, which is no worse than the unsynchronised behaviour this
// file replaces, and the counter records that it happened.
class SRTLock {
public:
    SRTLock() : held(false) {
        if (srtMutex) {
            held = (xSemaphoreTakeRecursive(srtMutex, pdMS_TO_TICKS(SRT_LOCK_TIMEOUT_MS)) == pdTRUE);
            if (!held) srtLockTimeouts++;
        }
    }
    ~SRTLock() {
        if (held) xSemaphoreGiveRecursive(srtMutex);
    }
private:
    bool held;
    SRTLock(const SRTLock &) = delete;
    SRTLock &operator=(const SRTLock &) = delete;
};

#endif // SYNC_H
