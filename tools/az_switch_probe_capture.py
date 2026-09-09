#!/usr/bin/env python3
"""Open the Due's USB port (which resets it and triggers its boot-home), wait for
that to finish, send PROBE <cycles>, and save the streamed samples as CSV."""
import sys, time, serial
PORT, OUT = "/dev/ttyACM0", sys.argv[1]
CYCLES = int(sys.argv[2]) if len(sys.argv) > 2 else 6
s = serial.Serial(PORT, 115200, timeout=1)
t0 = time.time(); ready = False
print("waiting for the boot-home to finish...", flush=True)
while time.time() - t0 < 180:
    line = s.readline().decode(errors="replace").strip()
    if not line: continue
    if "Homing complete" in line or "Status:Ready" in line:
        ready = True; break
if not ready: print("Due did not report Ready; continuing anyway", flush=True)
time.sleep(1.0); s.reset_input_buffer()
s.write(f"PROBE {CYCLES}\n".encode()); s.flush()
n = 0; t0 = time.time()
with open(OUT, "w") as f:
    while time.time() - t0 < 600:
        line = s.readline().decode(errors="replace").strip()
        if not line: continue
        if line.startswith("PROBE START"):
            f.write(line.split(" ", 2)[2] + "\n"); continue
        if line.startswith("PROBE END"): break
        if line.startswith("PROBE ABORTED"): print(line); break
        if line.count(",") == 4:
            f.write(line + "\n"); n += 1
print(f"captured {n} samples to {OUT}", flush=True)
