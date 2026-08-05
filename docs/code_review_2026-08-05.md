# Code review: ESP32 controller and receiver scheduler

**Date:** 5 August 2026
**Scope:** `esp32_controller_arduino/src/` (all files), `receiver_scheduler/h1_web_scheduler.py`,
`receiver_scheduler/sun_scan.py`, plus a brief pass over `test_sun_scan.py` / `test_scheduler.py`.
**Method:** read-only review by three parallel reviewers (one per subsystem), each instructed to
verify every claim against the actual code before reporting; the three most severe claims were
independently re-verified afterwards. No code was changed.

**Fix status (updated 5 August 2026):** P1, P3, P5, S1, S2, S3, S4, and S5 are fixed,
tested, and pushed (commits `c338a42`, `dee77ac`, `7a4e0f6`, `6881581`, `3533542`,
`131fd8e`); S10 is partially mitigated by the S1 rework. Per-finding status notes appear
inline below. The remaining items — all ESP32 firmware findings and the medium/low
scheduler items — are still open.

**Overall verdict:** the coordinate mathematics, the Due status-line parsing, the JS-to-endpoint
wiring, and the sun-scan geometry were all checked and found correct — the foundations are sound.
The defects cluster into three themes:

1. The ESP32 firmware has **no locking between its two tasks** (async web server vs Arduino loop).
2. The scheduler **holds its main lock across multi-minute blocking operations** and mismanages
   subprocess lifecycles.
3. The sun scan **does not release hardware or honour cancellation on error paths**.

---

## 1. ESP32 controller firmware

ESPAsyncWebServer handlers run on the `async_tcp` FreeRTOS task; tracking and serial polling run
on the Arduino `loopTask`. There is no mutex anywhere in the firmware.

### C1. HIGH — Unsynchronized cross-task access to heap `String` state
`src/srt_serial.h:42-46,70`, `srt_serial.cpp:22-29,35-66,164-245`, `web_server.cpp:110-143`,
`main.cpp:220-230`.
`parseStatus()` reassigns `statusStr`, `faultStr`, `lastStatus` (String realloc/free) once per
second on loopTask while `/status` copies them on async_tcp. `logMessage()` reassigns
`logBuffer[i].message` (every RX/TX line, ≥2/s) while `/serial/log` iterates and copies the same
entries — the UI polls this every second, so the race window is exercised constantly. Same pattern
for `state.targetName`. A String copy concurrent with reassignment is a use-after-free →
intermittent heap corruption and reboots.

### C2. HIGH — Concurrent UART writes to the Due can interleave
`src/srt_serial.cpp:92-134`.
`sendTarget`/`sendStop`/`sendHome`/`requestStatus` are called from both tasks (tracking loop and
web handlers). `println` is two separate writes (payload, then CRLF), so calls from the two tasks
can splice mid-line. Scenario: the 1 Hz `STATUS` poll fires while a user clicks Go To → the Due
receives `STATUS45.0 180.0` (invalid, dropped) and the slew command is silently lost; a mangled
but parseable number pair could in the worst case command a wrong position.

### C3. HIGH — `/wifi/scan` blocks the network stack ~4 s, then permanently subscribes async_tcp to the task watchdog
`src/wifi_manager.cpp:118-136`, `web_server.cpp:613-626`.
The synchronous scan freezes all web traffic and the Stellarium TCP server. Afterwards
`esp_task_wdt_add(NULL)` runs in the async_tcp context (the preceding `esp_task_wdt_delete(NULL)`
fails because that task was never subscribed), so async_tcp — which blocks in queue waits and
never resets the watchdog — triggers "Task watchdog got triggered" every WDT period from then on
(and reboots the controller if TWDT panic is enabled).

### C4. HIGH — `/wifi/connect` blocks async_tcp up to 15 s; reply usually never reaches a softAP client
`src/wifi_manager.cpp:36-57`, `web_server.cpp:629-639`.
The whole network stack is frozen while connecting. If the browser is on the softAP (the normal
case when configuring WiFi), `WiFi.begin()` in AP_STA mode retunes the AP to the STA channel,
dropping the client mid-request; the JSON response is lost and the UI hangs on "Connecting".

### C5. MEDIUM-HIGH — Use-after-free race on the Stellarium client pointer
`src/stellarium.cpp:31-32,116-123,137-144`.
`handleStellariumServer()` (loopTask) checks `stellariumClient && stellariumClient->connected()`
then calls `write()`, while async_tcp may concurrently run `onDisconnect`, which nulls the pointer
and `delete`s the client. A disconnect coinciding with the 100 ms position send dereferences a
freed `AsyncClient`.

### C6. MEDIUM — Stellarium is shown the commanded target, not the measured position
`src/stellarium.cpp:36`.
`sendPositionToStellarium()` converts `state.targetAlt/targetAz` instead of
`srtSerial.getCurrentAlt()/getCurrentAz()`. The reticle jumps instantly to the target on goto and
never shows the real slew; before any command it reports Alt/Az (0,0) regardless of dish position.

### C7. MEDIUM — `/goto` and `/goto/galactic` silently enable continuous tracking
`src/web_server.cpp:233-252` (and 207-230) both call `prepareTrackingTarget()`, which sets
`state.trackingEnabled = true` (line 70). The UI promises "slew once"; in reality a one-off goto
sidereally tracks the spot indefinitely, including auto-parking to home when it sets.
*(Independently re-verified.)*

### C8. MEDIUM — Stellarium TCP receive has no message framing
`src/stellarium.cpp:76-101`.
A goto split across TCP segments is silently discarded; two coalesced gotos execute only the
first — the telescope can go to an older target than Stellarium believes it sent.

### C9. MEDIUM — Blocking NTP sync (≤10 s) runs in `loop()` on Ethernet reconnect
`src/main.cpp:143-168` (called from loop at 429-433). During a link flap, tracking, Due status
parsing, and Stellarium updates all stall for up to 10 s.

### C10. MEDIUM — Settings doubles can tear across tasks; no range validation
`src/web_server.cpp:680-719`.
`/settings/save` assigns 8-byte doubles (`observerLat/Lon`) on async_tcp while `updateTracking`
reads them on loopTask — a torn read can emit one wild slew command. No validation:
`mount_az_min > mount_az_max` is accepted silently (clamps then always command `mountAzMax`);
`lat=200` corrupts all coordinate math.

### C11. LOW-MEDIUM — Target above `mountAltMax` is only logged
`src/main.cpp:264-266`.
Unlike the below-horizon branch (parks at home, sets `waitingForRise`), the too-high branch does
nothing but a debug printf. The dish silently freezes with the UI still showing active tracking.

### C12. LOW-MEDIUM — SSID-borne XSS in the embedded UI
`src/index_html.h:475` builds `onclick="selectNetwork('<ssid>')"` unescaped and `:522` inserts
`/serial/log` messages via `innerHTML`. A hostile SSID executes script in the controller UI
(which can then hit any mutating endpoint, including slews). A `"` in an SSID also breaks the
`/wifi/scan` JSON (`web_server.cpp:619`).

### C13. LOW — `/settings` JSON built in a fixed 600-byte buffer with unbounded string settings
`src/web_server.cpp:730-746`.
Oversized `ap_ssid`/`ap_password`/`page_name` (browser `maxlength` is the only guard) truncate
the JSON mid-structure → the Settings tab dies until an NVS reset. The AP password is also served
in cleartext to any client.

### C14. LOW — Moon ephemeris is geocentric (no topocentric parallax)
`src/coordinates.cpp:256-377`.
The Moon's horizontal parallax is ~57 arcmin; ignoring it biases computed Alt/Az by up to ~1° —
comparable to the pointing-model corrections the project calibrates for. Sun/galactic unaffected.

### C15. LOW — `data/index.html` has drifted from the served embedded UI
Nothing serves `data/index.html`; the "auto-generated" `src/index_html.h` has since been edited
directly. Edits to `data/index.html` — or a rerun of the generator — silently discard the newer
embedded UI.

**Verified non-issues:** the positional status parser matches the Due's actual `outputStatus()`
format (the `" -> "` section is only emitted in `STATE_DRIVING`, so `isSlewing` is correct);
route registration order matches the ESPAsyncWebServer fork's prefix rules; every `fetch()` in
`index_html.h` has a matching endpoint with matching parameters; GMST/precession/galactic
formulas and quadrant conventions are mutually consistent and correct for east-positive longitude.

---

## 2. Scheduler (`receiver_scheduler/h1_web_scheduler.py`)

### S1. HIGH — `process_lock` is held across a multi-minute blocking slew wait
**FIXED** in `3533542`: the start is claimed briefly under the lock, pointing/slew wait run
unlocked and abortable, stop cancels an in-flight start; regression-tested.
`h1_web_scheduler.py:943-1023`. *(Independently re-verified.)*
`start_observation()` holds the lock through `srt_point_telescope()` (up to ~9 s of HTTP retries),
`srt_wait_for_slew()` (polls up to `slew_timeout` = 300 s), and `srt_set_calibrator()`. While a
slew is stuck, `/api/status`, `/api/stop`, `/api/receiver/status`, `/api/sunscan/start`, and the
scheduler thread all block on the lock — the operator cannot stop or even see status exactly when
the telescope is misbehaving.

### S2. HIGH — Wildcard CORS on an unauthenticated control API enables drive-by command execution
**FIXED** in `131fd8e`: CORS is now restricted to the configured controller origins (the
ESP32 web UI legitimately calls this API cross-origin — verified in `index_html.h` before
changing), `/api/config` rejects unknown keys against the defaults, and observation
filenames are contained inside the data folder via realpath checks. Residual (accepted):
the API remains unauthenticated for same-network HTTP clients by design.
`:173-179` sets `Access-Control-Allow-Origin: *`; `/api/config` POST (`:3247-3251`) does
`cfg.update(request.json)` with no validation; `_find_platformio()` (`:471-473`) returns the
configured `platformio_path` verbatim and `_run_firmware_update()` (`:526-538`) executes it.
Any web page in a browser on the scheduler host/LAN can set `platformio_path` to an arbitrary
binary and trigger `/api/firmware/update`. Related: `generate_filename()` (`:919-928`) joins
unvalidated `obs['filename']`; absolute paths or `../` escape the data folder.

### S3. HIGH — Scheduled observations bypass the SDR reservation held by a sun scan / calibration day
**FIXED** in `131fd8e` with preemption semantics (a new scheduled observation always
wins): the start cancels a running Sun scan / calibration day and waits up to 10 minutes
for the SDR to be released before launching; the wait runs in the unlocked start phase and
is abortable by stop. If the scan never releases, the attempt fails into the S5 backoff.
`:943-1009` checks only `current_process`, never `sun_scan_state["running"]` or
`cal_day_state["running"]` (those checks exist only in the manual-start endpoints). A due
observation can launch the receiver while `sun_scan.py` holds the B210 — device-open failure or
contention, with the telescope simultaneously commanded to two different targets.

### S4. HIGH — No SIGTERM handler and no process group: receiver orphaned when the scheduler dies
**FIXED** in `6881581` (SIGTERM handler unwinds through the Ctrl+C cleanup, which now also
stops thread-based calibration observations). The process-group half was deliberately left:
the receiver spawns no children, so the handler alone closes the orphan risk.
`:3702-3710` cleanup runs only on `KeyboardInterrupt`/clean exit; no `signal.signal(SIGTERM, …)`
anywhere; `Popen` (`:1009`, `:3153`) uses no process group / job object. `systemctl stop` or a
crash orphans the receiver, which keeps the B210 claimed (USRP claims are exclusive); every
observation after restart fails until the orphan is killed by hand.

### S5. HIGH — A receiver that crashes at startup is restarted in a tight loop all slot
**FIXED** in `7a4e0f6`: failed and short-lived starts are counted per schedule slot; after
three the scheduler gives up with a clear log message. Run Now remains available to retry.
`:1201-1226`. When the receiver exits early (e.g. SDR open failure), the still-due observation
re-enters the start branch every 5 s: re-point, re-wait, new timestamped output file. A B210 USB
glitch turns a 60-minute slot into hundreds of spawned processes, empty `.h5` files, and hundreds
of goto commands to the ESP32.

### S6. MEDIUM — Calibration observation globals mutated without the lock
`:1031-1043`, `:1105-1112` write `current_observation` / `observation_end_time` lock-free;
`:1171`, `:3096-3104` read them lock-free. `/api/status` can compute `remaining` from a mismatched
pair; the scheduler thread can see a half-cleared calibration observation and restart it.

### S7. MEDIUM — Observation identity is tracked by name; duplicates break start/preempt logic
`:1203`, `:1211` compare `running_name == due_obs.get('name')`. Nothing prevents duplicate names
(edit, JSON import `:2466`, POST `:3082`). Two enabled entries with the same name on consecutive
hours: the second silently never starts (kept on the first's frequency/gain/target for its whole
slot). The JS `isRunning()` (`:2019-2021`) has the same flaw for edit/delete blocking.

### S8. MEDIUM — UI reports "Schedule saved!" even when the server rejected the save
JS `saveSchedule()` (`:1961-1965`) and `autoSave()` (`:2084-2088`) ignore the response;
`post_schedule` (`:3080-3088`) 400s whenever `find_clashes()` finds any overlap among all enabled
entries — including stale past ones (`find_clashes` `:883,891` treats empty `start_date` as
*today*, so dateless leftovers create phantom clashes). Edits silently vanish on reload.

### S9. MEDIUM — Embedded JS uses the UTC date as the default local date
`:2097`, `:1850`, `:1863`, `:2215`, `:2343` use `toISOString().slice(0,10)`. At 00:30 BST the
default date is *yesterday*; the server-side due check (`:1195`) then never fires and the
observation silently never runs. `clearPast` and the countdown mis-classify in the same window.

### S10. MEDIUM — Failed satellite observation leaks the tracking thread
**PARTIALLY FIXED** in `3533542` (S1 rework): a start that fails or is aborted after
tracking began now stops the tracking thread, and the S5 early-exit cleanup path also runs
`stop_observation()`. A tracker left by other exotic paths would still persist.
`:957-964` starts `start_satellite_tracking()` before the receiver `Popen` (`:1009`); if the Popen
raises or the receiver Python is missing, `start_observation` returns False with the tracking loop
alive, sending `/direct` every second (`:666`) indefinitely — the dish silently chases satellites
until something else stops it.

### S11. MEDIUM — Check-then-act races between receiver-start, sunscan-start, calday-start endpoints
`:3128-3153` vs `:3352-3374` vs `:3414-3434` — each reads the various running flags as separate
unlocked reads; the sun-scan thread sets `running=True` only after starting (`:2876`). Two
concurrent requests can both pass all checks.

### S12. LOW-MEDIUM — Windows stop path hard-kills the receiver
`:1051-1054` — on win32 `terminate()` is `TerminateProcess`; no chance to flush/close the HDF5
file. `stop_booted_receiver` (`:702`) uses `terminate()` unconditionally. Mitigated in practice by
the observatory host being Linux.

### S13. LOW — `/api/schedule` accepts arbitrary JSON with no shape validation
`:3082-3087`. A non-list body throws in `find_clashes`; a string `duration_minutes` makes
`:1191` produce a string and `:1195` raise `TypeError` inside the scheduler thread every 5 s —
log spam and skipped schedule evaluation until the file is hand-edited.

### S14. LOW — `SRT_CONTROLLER_URL` mutated from multiple threads without a lock
`:218-220` (fallback promotion), `:3254` (config POST). A satellite-tracker call mid-flight
against the old URL can overwrite an operator's fresh config change.

**Verified non-issues:** `srt_api_call` always uses a 3 s timeout; all JS fetch endpoints match
Flask routes and parameters; the `H1_*` env-var contract matches the receiver;
`stop_booted_receiver` releases the lock before the blocking `wait()`; `dms_to_decimal` handles
integer `-0°` correctly (a fractional `-0.5°` DMS target cannot be expressed with `deg=0`, but the
UI only produces integer degrees).

---

## 3. Sun scan (`receiver_scheduler/sun_scan.py`)

### P1. HIGH — B210/USRP session is not closed on any exception path out of the scan loop
**FIXED** in `c338a42`: the scan loop is wrapped in try/finally; the failed-slew test now
asserts `close()` is called (closes P9's test gap too).
`sun_scan.py:661-662, 709-779`. *(Independently re-verified.)*
`b210_meter.close()` (`:778-779`) is reached only on normal completion or the cancellation
`break`; there is no `try/finally`. Any mid-scan exception — slew timeout/fault (`:119,132,139`),
"Sun set during scan" (`:674`), refine failure (`:705`), grid-clamp error (`:681`), SDR error in
`measure()` — propagates with the USRP still claimed; cleanup then depends on CPython refcount
timing, and a calibration-day retry's `MultiUSRP()` can fail with a USB-claim error.

### P2. MEDIUM — `cancel_event` is checked only once per grid point
`:710` is the sole check. `_slew_to` (`:102-141`) has no cancel parameter and polls up to
`slew_timeout` (300 s under the scheduler); `slew_and_refine` (`:688-706`) can chain up to three
such slews plus the row-start backlash slew; SDR integration (`:764-768`) is uninterruptible.
Cancel can take more than 15 minutes to bite.

### P3. MEDIUM — Fit covariance scaled by `max(χ²ᵣ, 1e-12)` instead of `max(χ²ᵣ, 1)`
**FIXED** in `dee77ac`, with a test pinning that consistent scans keep floored errors.
Note: the significance gate is now stricter — some previously-passing calibrations may be
rejected; that is the intended behaviour.
`:1100-1101`. Per-scan sigmas are deliberately floored at the 0.144° mount quantisation
(`:911-912`), but when scans are mutually consistent (typical — quantisation error is partly
systematic), `χ²ᵣ ≪ 1` shrinks the parameter errors back below the floor, inflating
`parameter_significance` / `min_tilt_significance` — the exact quantities the weak-calibration
gate relies on.

### P4. LOW-MEDIUM — `measure_power` b210 failure swallows the root cause
`:309-319` logs "trying GNU Radio..." but no such path exists; the actual UHD exception is
discarded and a generic `RuntimeError` raised. Dead inside `sun_scan()` itself (which uses
`_B210PowerMeter`) but it is the public API path.

### P5. LOW — Module-header pointing-model equation has the opposite azimuth signs to the code
**FIXED** in `dee77ac` (header corrected to match the implemented, test-verified matrix).
`:846-847` vs the implemented (and test-verified, `test_sun_scan.py:303`) matrix at `:941-945`.
The code is right; the header comment is stale — a live trap given this file's history of
azimuth-sign fixes.

### P6. LOW — Config `slew_timeout` loaded but never used by `sun_scan()`
`:71` defines it; `sun_scan()` (`:562`) hard-codes 120 s and never reads it. CLI runs use 120 s
while scheduler runs use 300 s — a slow slew that succeeds under the scheduler fails from the CLI.

### P7. LOW — Corrupt `scheduler_config.json` crashes the scan
`:73-80` catches only `FileNotFoundError`; `json.JSONDecodeError` propagates. Inconsistent with
`load_pointing_data`/`load_pointing_model` (`:896`, `:1184`), which both handle it.

### P8. LOW — A NaN power sample breaks the output image silently
`:796-801` masks NaN for the fit, but `generate_image` (`:818`) gets the full grid and
`Normalize(vmin=power_grid.min(), …)` (`:514`) becomes NaN-scaled — blank/garbled colormap, no
error.

### P9. LOW — Test gap on the P1 leak path
**FIXED** in `c338a42` alongside P1 (the failed-slew test now asserts `close()`).
`test_sun_scan.py:134-154` exercises exactly the failed-slew path but never asserts
`meter.close()` was called (it currently is not); `:187` does assert it on the success path.

### P10. LOW — `_measure_power_uhd` opens a fresh `MultiUSRP` per call with no release
`:203-205` — GC-only cleanup; combined with P4 the public one-shot path both leaks-by-GC and
cannot report why UHD failed.

**Verified non-issues:** ephem radian/degree handling and `observer.lat = str(...)` parsing;
`cos(alt)` guards at command and mid-scan conversion; azimuth wraparound is a non-issue at this
site (Sun below horizon near the 0/360 seam; mount range 0–353 with clamp detection);
Gaussian-fit bounds/initial guess/edge-pinning/FWHM gates are sound including n=3; backlash
direction is mechanically consistent; the 0-based progress callback is consumed correctly.

---

## Suggested fix priority

Cheap and high-value first:

1. ~~Move the slew wait outside `process_lock` (S1).~~ **Done** (`3533542`).
2. ~~`try/finally` around the sun-scan loop for `b210_meter.close()` (P1).~~ **Done** (`c338a42`).
3. ~~Retry limit / backoff in the scheduler restart loop (S5) and a SIGTERM handler
   (S4).~~ **Done** (`7a4e0f6`, `6881581`).
4. Make `/goto` genuinely slew-once (C7) — needs the bench ESP32 (compile-check only here);
   check `/goto` callers in the scheduler and sun scan first in case they rely on
   goto-implies-tracking.
5. One FreeRTOS mutex around the ESP32 shared state plus a UART-write lock (C1, C2, C5,
   C10) — bench session with `tools/due_emulator.py`, soak overnight before deploying.
6. Fix the `/wifi/scan` watchdog subscription (C3). ~~Validate `/api/config` keys
   (S2).~~ **Done** (`131fd8e`, together with S3 preemption).
