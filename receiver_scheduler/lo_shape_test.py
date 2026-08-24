#!/usr/bin/env python3
"""Does the bandpass shape move rigidly with the local oscillator?

This is the validity test for frequency switching. If the instrumental shape,
expressed against baseband frequency, is the same at every LO setting, then
differencing two spectra taken at two LO settings cancels it exactly. Whatever
fails to overlay is fixed in *sky* frequency instead - the RF filter ahead of
the LNA, or a reflection - and that residual is the error floor on the method.

Held fixed so that only the LO moves: the sample rate (7.0 MHz, chosen because
plan_tuning's minimum for the largest offset here is exactly that, so no run has
its rate raised out from under it), the channel count, the gain, and the field.

The offsets are visited up and then back down again rather than in one sweep.
Time and LO offset are otherwise confounded: a slow thermal drift through a
monotonic sequence reads as a dependence on the LO, which is precisely the
mistake that made a fading ground signal look like a property of azimuth on
2026-08-21. The palindrome separates them - a genuine LO dependence is
symmetric about the turning point, a drift is antisymmetric.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = "/home/astro/radioconda/bin/python"
RECEIVER = os.path.join(HERE, "b210_h1_receiver.py")
CONTROLLER = "http://192.168.50.120"

SAMPLE_RATE = 7.0e6
CHANNELS = 15360               # 5-smooth; 455.7 Hz per channel at 7.0 MHz
INTEGRATION = 3.0
RECORDS = 25                   # 75 s on each setting
OFFSETS_MHZ = [0.6, 0.8, 1.0, 1.2]
SEQUENCE = OFFSETS_MHZ + OFFSETS_MHZ[::-1]


def controller_status():
    try:
        with urllib.request.urlopen(CONTROLLER + "/status", timeout=8) as r:
            return json.load(r)
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        return {"error": str(exc)}


def run_one(offset_mhz, index, total):
    out = os.path.join(HERE, "data",
                       "bandpass_lo_%03d_%d.h5" % (round(offset_mhz * 100), index))
    env = dict(os.environ)
    env["H1_OUTPUT_FILE"] = out
    env["H1_CENTER_FREQ"] = "1420405751.768"
    env["H1_FFT_SIZE"] = str(CHANNELS)
    env["H1_INTEGRATION_TIME"] = str(INTEGRATION)
    env["H1_LO_OFFSET"] = str(offset_mhz * 1e6)

    before = controller_status()
    print("[%d/%d] LO offset %.2f MHz -> %s" % (index + 1, total, offset_mhz,
                                                os.path.basename(out)), flush=True)
    print("        pointing before: gal l=%s b=%s  alt=%s az=%s"
          % (before.get("gal_l"), before.get("gal_b"),
             before.get("alt"), before.get("az")), flush=True)

    proc = subprocess.Popen(
        [PYTHON, RECEIVER, "--sdr", "b210", "--headless",
         "--sample-rate", str(SAMPLE_RATE), "--gain", "40"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=HERE)

    deadline = time.time() + RECORDS * INTEGRATION + 25
    tail = []
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    proc.terminate()
    try:
        tail = proc.communicate(timeout=25)[0].splitlines()
    except subprocess.TimeoutExpired:
        proc.kill()
        tail = proc.communicate()[0].splitlines()

    after = controller_status()
    print("        pointing after : gal l=%s b=%s  alt=%s az=%s"
          % (after.get("gal_l"), after.get("gal_b"),
             after.get("alt"), after.get("az")), flush=True)
    for line in tail[-6:]:
        print("        | " + line, flush=True)
    return out


def main():
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    status = controller_status()
    if "error" in status:
        print("Controller unreachable: %s" % status["error"])
        return 1
    print("Field: gal l=%.2f b=%.2f at alt %.1f, tracking assumed on."
          % (status["gal_l"], status["gal_b"], status["alt"]))
    print("Sample rate %.2f MHz, %d channels (%.1f Hz), %d x %.1f s per setting."
          % (SAMPLE_RATE / 1e6, CHANNELS, SAMPLE_RATE / CHANNELS,
             RECORDS, INTEGRATION))
    print("Sequence: %s\n" % " ".join("%.2f" % o for o in SEQUENCE))

    written = []
    for i, off in enumerate(SEQUENCE):
        written.append(run_one(off, i, len(SEQUENCE)))
        print(flush=True)
    print("Done. Files:")
    for w in written:
        size = os.path.getsize(w) if os.path.exists(w) else -1
        print("   %s  %d bytes" % (os.path.basename(w), size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
