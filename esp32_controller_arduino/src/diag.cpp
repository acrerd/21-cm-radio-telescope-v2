// diag.cpp - see diag.h.

#include "diag.h"
#include <esp_system.h>
#include <esp_task_wdt.h>
#include <time.h>
#include <esp_debug_helpers.h>
#include <esp_private/panic_internal.h>
#include <soc/cpu.h>
#include <xtensa/xtensa_context.h>

#define DIAG_MAGIC 0x53525445u      // "SRTE": layout with the panic record (2026-10-02)
#define DIAG_BT_DEPTH 16
#define DIAG_EVENTS 24
#define DIAG_EVENT_LEN 64
#define LOOP_WDT_S 30               // a pass of the loop taking this long is a hang

struct DiagRecord {
    uint32_t magic;
    uint32_t bootCount;
    uint32_t stage;
    uint32_t uptimeMs;
    uint32_t lastTargetMs;          // uptime of the last target sent to the Due (0 = none)
    uint32_t maxLoopGapMs;
    uint32_t minFreeHeap;
    uint32_t minMaxAlloc;
    uint32_t head;                  // next event slot
    uint32_t count;
    char events[DIAG_EVENTS][DIAG_EVENT_LEN];
    // Written by the panic handler as the boot dies (__wrap_esp_panic_handler):
    // what the ESP32 otherwise prints only to the board's own serial port.
    uint32_t panicked;
    uint32_t panicCore;
    uint32_t panicException;        // panic_exception_t: 2 TWDT, 3 abort, 4 fault
    uint32_t panicAddr;
    uint32_t panicDepth;
    uint32_t panicBacktrace[DIAG_BT_DEPTH];
    char panicReason[48];
    char panicStage[4];             // spare; keeps the record a multiple of 4
};

// RTC slow memory, not initialised at boot: survives panic, watchdog and
// software resets; lost on power-on and brownout (the magic then fails).
RTC_NOINIT_ATTR static DiagRecord rtc;

static DiagRecord prev;             // the previous boot's record, copied at boot
static bool prevValid = false;
static esp_reset_reason_t resetReason = ESP_RST_UNKNOWN;
static uint32_t lastTickMs = 0;
static bool wdtOn = false;

static const char *reasonName(esp_reset_reason_t r) {
    switch (r) {
        case ESP_RST_POWERON:   return "power-on";
        case ESP_RST_EXT:       return "external pin";
        case ESP_RST_SW:        return "software (restart or OTA)";
        case ESP_RST_PANIC:     return "panic (exception or abort)";
        case ESP_RST_INT_WDT:   return "interrupt watchdog";
        case ESP_RST_TASK_WDT:  return "task watchdog";
        case ESP_RST_WDT:       return "other watchdog";
        case ESP_RST_DEEPSLEEP: return "deep sleep";
        case ESP_RST_BROWNOUT:  return "brownout";
        case ESP_RST_SDIO:      return "SDIO";
        default:                return "unknown";
    }
}

static const char *stageName(uint32_t s) {
    switch (s) {
        case DIAG_OTA:           return "OTA handle";
        case DIAG_WEB:           return "web server";
        case DIAG_STELLARIUM:    return "Stellarium server";
        case DIAG_TRACK_READ:    return "tracking: reading the Due";
        case DIAG_TRACK_POLL:    return "tracking: polling the Due";
        case DIAG_TRACK_LOCK:    return "tracking: taking the lock";
        case DIAG_TRACK_COMPUTE: return "tracking: computing the target";
        case DIAG_TRACK_SEND:    return "tracking: sending the target";
        case DIAG_CLOCK:         return "clock status";
        case DIAG_WIFI:          return "WiFi power service";
        case DIAG_ETH:           return "Ethernet / NTP restart";
        case DIAG_RESOLVER:      return "DNS resolver check";
        case DIAG_IDLE:          return "loop delay";
        default:                 return "setup or none";
    }
}

void diagBoot() {
    resetReason = esp_reset_reason();
    if (rtc.magic == DIAG_MAGIC && rtc.count <= DIAG_EVENTS && rtc.head < DIAG_EVENTS) {
        prev = rtc;
        prevValid = true;
    }
    uint32_t boots = prevValid ? rtc.bootCount + 1 : 1;
    memset(&rtc, 0, sizeof(rtc));
    rtc.magic = DIAG_MAGIC;
    rtc.bootCount = boots;
    rtc.minFreeHeap = 0xFFFFFFFFu;
    rtc.minMaxAlloc = 0xFFFFFFFFu;
    char msg[DIAG_EVENT_LEN];
    snprintf(msg, sizeof(msg), "boot %lu: reset by %s", (unsigned long)boots, reasonName(resetReason));
    diagEvent(msg);
}

// The panic hook (2026-10-02). On 2026-10-01 the controller panicked at 20:53
// BST in the stage that reads the Due, and its backtrace went to the WT32's
// UART0, which nothing records. The link is wrapped (-Wl,--wrap=
// esp_panic_handler in platformio.ini) so this runs first: it copies the
// reason, the faulting address and up to 16 return addresses into the RTC
// record, then hands over to the real handler, which prints and resets as
// before. Decode the addresses with xtensa-esp32-elf-addr2line against the
// ELF of the build that was running (kept by flash_controller, see CLAUDE.md).
// Nothing here allocates or locks: the heap or the lock may be what failed.
extern "C" void __real_esp_panic_handler(panic_info_t *info);
extern "C" void __wrap_esp_panic_handler(panic_info_t *info) {
    rtc.panicked = 1;
    rtc.panicCore = (uint32_t)info->core;
    rtc.panicException = (uint32_t)info->exception;
    rtc.panicAddr = (uint32_t)info->addr;
    const char *r = info->reason ? info->reason : "";
    size_t n = 0;
    for (; n < sizeof(rtc.panicReason) - 1 && r[n]; n++) rtc.panicReason[n] = r[n];
    rtc.panicReason[n] = 0;
    rtc.panicDepth = 0;
    const XtExcFrame *f = (const XtExcFrame *)info->frame;
    if (f) {
        esp_backtrace_frame_t bt = {(uint32_t)f->pc, (uint32_t)f->a1, (uint32_t)f->a0, f};
        rtc.panicBacktrace[rtc.panicDepth++] = esp_cpu_process_stack_pc(bt.pc);
        while (rtc.panicDepth < DIAG_BT_DEPTH && bt.next_pc && esp_backtrace_get_next_frame(&bt)) {
            rtc.panicBacktrace[rtc.panicDepth++] = esp_cpu_process_stack_pc(bt.pc);
        }
    }
    __real_esp_panic_handler(info);
}

void diagWatchdogStart() {
    // The loop task's own watchdog. The core's task watchdog watches only the
    // idle task, so a hung loop - tracking silently stopped - never reset
    // anything. 30 s, not the core's 5: a pass can legitimately spend a few
    // seconds in readStringUntil timeouts or a WiFi power change.
    esp_task_wdt_init(LOOP_WDT_S, true);
    if (esp_task_wdt_add(NULL) == ESP_OK) wdtOn = true;
}

void diagStage(uint8_t stage) {
    rtc.stage = stage;
}

void diagLoopTick() {
    uint32_t now = millis();
    if (lastTickMs != 0) {
        uint32_t gap = now - lastTickMs;
        if (gap > rtc.maxLoopGapMs) rtc.maxLoopGapMs = gap;
    }
    lastTickMs = now;
    rtc.uptimeMs = now;
    uint32_t fh = ESP.getFreeHeap();
    uint32_t ma = ESP.getMaxAllocHeap();
    if (fh < rtc.minFreeHeap) rtc.minFreeHeap = fh;
    if (ma < rtc.minMaxAlloc) rtc.minMaxAlloc = ma;
    if (wdtOn) esp_task_wdt_reset();
}

void diagTargetSent() {
    rtc.lastTargetMs = millis();
}

void diagOtaProgress() {
    if (wdtOn) esp_task_wdt_reset();
}

void diagEvent(const char *msg) {
    char stamp[16];
    time_t t = time(nullptr);
    if (t > 1000000000) {
        struct tm tmv;
        gmtime_r(&t, &tmv);
        snprintf(stamp, sizeof(stamp), "%02d:%02d:%02d", tmv.tm_hour, tmv.tm_min, tmv.tm_sec);
    } else {
        snprintf(stamp, sizeof(stamp), "+%lus", (unsigned long)(millis() / 1000));
    }
    snprintf(rtc.events[rtc.head], DIAG_EVENT_LEN, "%s %s", stamp, msg);
    rtc.head = (rtc.head + 1) % DIAG_EVENTS;
    if (rtc.count < DIAG_EVENTS) rtc.count++;
}

static String esc(const char *s) {
    String o;
    for (; *s; s++) {
        if (*s == '"' || *s == '\\') o += '\\';
        if ((unsigned char)*s >= 32) o += *s;
    }
    return o;
}

static String recordJSON(const DiagRecord &r, bool current) {
    uint32_t now = current ? millis() : r.uptimeMs;
    String j = "{\"boot\":" + String((unsigned long)r.bootCount);
    j += ",\"uptime_s\":" + String((unsigned long)(now / 1000));
    j += ",\"last_stage\":\"" + String(stageName(r.stage)) + "\"";
    j += ",\"last_target_age_s\":";
    j += r.lastTargetMs ? String((unsigned long)((now - r.lastTargetMs) / 1000)) : String("null");
    j += ",\"max_loop_gap_ms\":" + String((unsigned long)r.maxLoopGapMs);
    j += ",\"min_free_heap\":" + String((unsigned long)(r.minFreeHeap == 0xFFFFFFFFu ? 0 : r.minFreeHeap));
    j += ",\"min_max_alloc\":" + String((unsigned long)(r.minMaxAlloc == 0xFFFFFFFFu ? 0 : r.minMaxAlloc));
    if (r.panicked) {
        static const char *kinds[] = {"debug", "interrupt watchdog", "task watchdog", "abort", "fault"};
        char b[16];
        j += ",\"panic\":{\"reason\":\"" + esc(r.panicReason) + "\"";
        j += ",\"kind\":\"" + String(r.panicException < 5 ? kinds[r.panicException] : "?") + "\"";
        j += ",\"core\":" + String((unsigned long)r.panicCore);
        snprintf(b, sizeof(b), "0x%08lx", (unsigned long)r.panicAddr);
        j += ",\"addr\":\"" + String(b) + "\",\"backtrace\":[";
        for (uint32_t i = 0; i < r.panicDepth && i < DIAG_BT_DEPTH; i++) {
            snprintf(b, sizeof(b), "0x%08lx", (unsigned long)r.panicBacktrace[i]);
            j += (i ? ",\"" : "\"") + String(b) + "\"";
        }
        j += "]}";
    }
    j += ",\"events\":[";
    uint32_t start = (r.count < DIAG_EVENTS) ? 0 : r.head;
    for (uint32_t i = 0; i < r.count; i++) {
        uint32_t idx = (start + i) % DIAG_EVENTS;
        char buf[DIAG_EVENT_LEN + 1];
        memcpy(buf, r.events[idx], DIAG_EVENT_LEN);
        buf[DIAG_EVENT_LEN] = 0;
        if (i) j += ",";
        j += "\"" + esc(buf) + "\"";
    }
    j += "]}";
    return j;
}

const char *diagResetReason() { return reasonName(resetReason); }
uint32_t diagLoopAgeMs() { return millis() - lastTickMs; }
uint32_t diagBootCount() { return rtc.bootCount; }

String diagJSON() {
    String j = "{\"reset_reason\":\"" + String(reasonName(resetReason)) + "\"";
    j += ",\"loop_watchdog\":" + String(wdtOn ? "true" : "false");
    j += ",\"free_heap\":" + String((unsigned long)ESP.getFreeHeap());
    j += ",\"max_alloc\":" + String((unsigned long)ESP.getMaxAllocHeap());
    j += ",\"loop_age_ms\":" + String((unsigned long)(millis() - lastTickMs));
    j += ",\"this_boot\":" + recordJSON(rtc, true);
    j += ",\"previous_boot\":";
    j += prevValid ? recordJSON(prev, false) : String("null");
    j += "}";
    return j;
}
