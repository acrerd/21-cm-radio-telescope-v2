// diag.h - what the controller was doing when it stopped or restarted.
//
// On 2026-09-30 tracking stopped at 14:09 and the controller rebooted at 14:35
// with nothing on record: the reset reason lives only until the next reset,
// the serial log is RAM, and the loop has no watchdog, so a hang is silent.
// This keeps a small record in RTC memory, which survives every reset except
// a power cycle: the loop stage last entered, the uptime, the last target sent
// to the Due, the heap low-water mark, and a ring of the controller's own
// events (everything logESP writes). At boot the previous record becomes the
// "previous boot" report on /diag and in /status; the reset reason says how
// it ended (panic, watchdog, brownout, power, software).

#ifndef DIAG_H
#define DIAG_H

#include <Arduino.h>

// Loop stages, for the breadcrumb.
enum DiagStage : uint8_t {
    DIAG_OTA = 1, DIAG_WEB, DIAG_STELLARIUM, DIAG_TRACK_READ, DIAG_TRACK_POLL,
    DIAG_TRACK_LOCK, DIAG_TRACK_COMPUTE, DIAG_TRACK_SEND, DIAG_CLOCK, DIAG_WIFI,
    DIAG_ETH, DIAG_RESOLVER, DIAG_IDLE
};

void diagBoot();                    // first thing in setup()
void diagWatchdogStart();           // end of setup(): the loop's own watchdog
void diagStage(uint8_t stage);      // cheap; called at each loop stage
void diagLoopTick();                // once per loop pass: heartbeat, gaps, heap
void diagTargetSent();              // tracking sent a target to the Due
void diagEvent(const char *msg);    // into the RTC ring (logESP calls this)
void diagOtaProgress();             // keep the watchdog fed through an upload
String diagJSON();                  // this boot and the previous one
const char *diagResetReason();      // how the previous boot ended
uint32_t diagLoopAgeMs();           // since the loop last completed a pass
uint32_t diagBootCount();

#endif
