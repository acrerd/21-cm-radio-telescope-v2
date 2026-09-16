#!/usr/bin/env python3
"""Can the B210 and its USB link carry the pilot? Full-duplex throughput test (issue #30).

Runs the fixed instrument's real receive graph (TwoProductFlowgraph: wide and
H I products at the observatory's rate) and, on the same device, a transmit
stream at the same rate - one FFT frame of samples repeated forever, exactly
as the pilot will be sent - plus the cross-spectrum branch the pilot recovery
will need (one complex multiply-accumulate per wide bin per frame). It then
counts what UHD complains about and what the host spends.

Nothing is radiated: the transmit frame is all zeros unless --amplitude is
given, and the TX gain is the minimum. The zeros still cross the USB link and
the DAC path at full rate, which is what is being tested.

    python tools/b210_duplex_throughput.py                 # 8 Msps, 120 s
    python tools/b210_duplex_throughput.py --rates 8 16 --seconds 180
    python tools/b210_duplex_throughput.py --no-tx         # the receive graph alone, for comparison
    python tools/b210_duplex_throughput.py --sdr demo      # graph plumbing only, no radio

Reports, per rate: RX overflows (UHD's 'O' and its "N overflows occurred"
messages), TX underruns ('U'), late/sequence/dropped marks ('L', 'S', 'D'),
the spectra actually accumulated against the number the rate implies, and
the process's CPU by thread. PASS means no overflow, no underrun and the
spectra count within 1% of expectation.

Needs the radio: refuses if a scheduler observation is running (asks the
scheduler at localhost:5000 first, when it answers).
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SCHED = os.path.join(os.path.dirname(HERE), "receiver_scheduler")
sys.path.insert(0, SCHED)

# The receiver module imports Qt unless told not to, and reads that from argv
# before anything else; this tool never wants a display.
if "--headless" not in sys.argv:
    sys.argv.append("--headless")

import numpy as np                                   # noqa: E402
from gnuradio import gr, blocks                      # noqa: E402

import b210_h1_receiver as rx                        # noqa: E402
from tuning import fixed_instrument                  # noqa: E402

SCHEDULER_URL = "http://localhost:5000"


def scheduler_busy():
    """True if the scheduler says an observation holds the radio; None if it did not answer."""
    try:
        with urllib.request.urlopen(SCHEDULER_URL + "/api/status", timeout=3) as r:
            d = json.load(r)
        return bool(d.get("running"))
    except Exception:                                 # noqa: BLE001
        return None


class _MarkCounter:
    """Count UHD's single-letter complaints and its overflow messages on stdout/stderr.

    Same trick as the receiver's _OverflowCounter (the descriptors are routed
    through a pipe and copied on), widened to the transmit side: 'U' is an
    underrun, 'L' a late packet, 'S' a sequence error, 'D' a dropped packet.
    Lone letters only - the tool's own text contains all of them.
    """
    LETTERS = "OULSD"
    _MESSAGE = re.compile(rb"(\d+) overflows? occurred")

    def __init__(self):
        self.counts = {c: 0 for c in self.LETTERS}
        self.message_overflows = 0
        self._lock = threading.Lock()
        self._orig = {}
        self._read_fd = None
        self._thread = None

    def start(self):
        r, w = os.pipe()
        for fd in (1, 2):
            self._orig[fd] = os.dup(fd)
            os.dup2(w, fd)
        os.close(w)
        self._read_fd = r
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        carry = b""
        while True:
            try:
                chunk = os.read(self._read_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            try:
                os.write(self._orig[1], chunk)
            except OSError:
                pass
            data = carry + chunk
            with self._lock:
                for m in self._MESSAGE.finditer(data):
                    self.message_overflows += int(m.group(1))
                # A lone mark is a letter bounded by whitespace/line ends.
                for m in re.finditer(rb"(?:^|(?<=\s))([OULSD])(?=\s|$)", data):
                    self.counts[m.group(1).decode()] += 1
            cut = data.rfind(b"\n")
            carry = data[cut + 1:] if cut >= 0 else data[-64:]

    def snapshot(self):
        with self._lock:
            return dict(self.counts), self.message_overflows

    def stop(self):
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:                             # noqa: BLE001
            pass
        for fd, orig in self._orig.items():
            try:
                os.dup2(orig, fd)
                os.close(orig)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._read_fd is not None:
            try:
                os.close(self._read_fd)
            except OSError:
                pass


class _ComplexVectorAccumulator(gr.sync_block):
    """Sum complex vectors: the cross-spectrum branch's sink."""

    def __init__(self, vlen):
        gr.sync_block.__init__(self, "cross-spectrum accumulator",
                               in_sig=[(np.complex64, vlen)], out_sig=None)
        self._sum = np.zeros(vlen, dtype=np.complex128)
        self._n = 0
        self._lock = threading.Lock()

    def work(self, input_items, output_items):
        with self._lock:
            self._sum += input_items[0].sum(axis=0)
            self._n += len(input_items[0])
        return len(input_items[0])

    def take(self):
        with self._lock:
            s, n = self._sum.copy(), self._n
            self._sum[:] = 0
            self._n = 0
        return s, n


def pilot_frame(nbins, amplitude, tone_bins=()):
    """One FFT frame of the transmit waveform: zeros, or the given tone bins at `amplitude`.

    Tones are placed on bin centres, so every RX frame holds a whole number
    of cycles of each - the frame-synchronous condition the pilot relies on.
    """
    frame = np.zeros(nbins, dtype=np.complex64)
    if amplitude > 0 and tone_bins:
        spec = np.zeros(nbins, dtype=np.complex128)
        for b in tone_bins:
            spec[b % nbins] = 1.0
        frame = (np.fft.ifft(spec) * nbins).astype(np.complex64)
        frame *= amplitude / np.abs(frame).max()
    return frame


def build(sdr_type, rate_hz, tx, cross, amplitude, tx_gain_db):
    inst = fixed_instrument({"sample_rate_hz": rate_hz})
    fg = rx.TwoProductFlowgraph(sdr_type, inst, strict=(sdr_type != "demo"))
    nbins = int(inst["wide_channels"])
    extras = {}
    if cross:
        # The recovery the pilot will need, at full cost even though the
        # reference here is arbitrary: conj(reference spectrum) times each
        # wide FFT frame, summed.
        ref = np.exp(2j * np.pi * np.random.default_rng(1).random(nbins)).astype(np.complex64)
        mult = blocks.multiply_const_vcc(np.conj(ref))
        presum = max(1, int(fg.sample_rate / nbins / fg.SINK_RATE_HZ))
        integ = blocks.integrate_cc(presum, nbins)
        acc = _ComplexVectorAccumulator(nbins)
        fg.connect((fg.wide_fft, 0), (mult, 0))
        fg.connect((mult, 0), (integ, 0))
        fg.connect((integ, 0), (acc, 0))
        extras.update(mult=mult, integ=integ, cross_acc=acc, cross_presum=presum)
    if tx and sdr_type == "b210":
        from gnuradio import uhd
        frame = pilot_frame(nbins, amplitude, tone_bins=(nbins // 4, nbins // 2, 3 * nbins // 4))
        src = blocks.vector_source_c(frame.tolist(), True)
        sink = uhd.usrp_sink(",".join(("type=b200", "")),
                             uhd.stream_args(cpu_format="fc32", args="", channels=[0]))
        sink.set_samp_rate(fg.sample_rate)
        sink.set_center_freq(fg.center_freq, 0)
        sink.set_gain(tx_gain_db, 0)
        sink.set_antenna("TX/RX", 0)
        fg.connect((src, 0), (sink, 0))
        extras.update(tx_src=src, tx_sink=sink,
                      tx_actual=(sink.get_samp_rate(), sink.get_center_freq(0), sink.get_gain(0)))
    return fg, nbins, extras


def thread_cpu(proc, before):
    """CPU seconds by thread since `before` ({tid: (name, user+system)})."""
    out = []
    now = {}
    for t in proc.threads():
        try:
            with open(f"/proc/{proc.pid}/task/{t.id}/comm") as f:
                name = f.read().strip()
        except OSError:
            name = str(t.id)
        now[t.id] = (name, t.user_time + t.system_time)
    for tid, (name, cpu) in now.items():
        prev = before.get(tid, (name, 0.0))[1]
        out.append((cpu - prev, name))
    out.sort(reverse=True)
    return out, now


def run_once(args, rate_hz, tx, cross):
    import psutil
    proc = psutil.Process()
    label = f"{rate_hz/1e6:.0f} Msps, TX {'on' if tx else 'off'}, cross-spectrum {'on' if cross else 'off'}"
    print(f"\n=== {label} ===", flush=True)
    marks = _MarkCounter()
    marks.start()
    try:
        fg, nbins, extras = build(args.sdr, rate_hz, tx, cross, args.amplitude, args.tx_gain)
        if "tx_actual" in extras:
            r, f, g = extras["tx_actual"]
            print(f"  TX: {r/1e6:.3f} Msps at {f/1e6:.6f} MHz, gain {g:.1f} dB, "
                  f"frame amplitude {args.amplitude}", flush=True)
        fg.start()
        t0 = time.time()
        time.sleep(min(5.0, args.seconds / 4))          # let the pipeline fill before counting
        for acc in (fg.wide_acc, fg.h1_acc):
            acc.take()
        if "cross_acc" in extras:
            extras["cross_acc"].take()
        marks_start, msg_start = marks.snapshot()
        _, thr_before = thread_cpu(proc, {})
        proc.cpu_percent(None)
        t_start = time.time()
        wide_n = h1_n = cross_n = 0
        while time.time() - t_start < args.seconds:
            time.sleep(1.0)
            _, n = fg.take_wide()
            wide_n += n
            _, n = fg.take_h1()
            h1_n += n
            if "cross_acc" in extras:
                _, n = extras["cross_acc"].take()
                cross_n += n * extras["cross_presum"]      # integrated vectors -> frames
        elapsed = time.time() - t_start
        cpu_total = proc.cpu_percent(None)
        by_thread, _ = thread_cpu(proc, thr_before)
        fg.stop()
        fg.wait()
        time.sleep(0.5)
        marks_end, msg_end = marks.snapshot()
    finally:
        marks.stop()
    # gr-uhd prints O/U marks whether or not it follows with a message; the
    # message count is the exact figure when it appears, the letters the floor.
    o = max(marks_end["O"] - marks_start["O"], msg_end - msg_start)
    u = marks_end["U"] - marks_start["U"]
    other = {c: marks_end[c] - marks_start[c] for c in "LSD"}
    frames_expected = fg.sample_rate / nbins * elapsed
    h1_expected = fg.subband["out_rate_hz"] / fg.subband["channels"] * elapsed
    wide_pct = 100 * wide_n / frames_expected
    h1_pct = 100 * h1_n / h1_expected
    print(f"  ran {elapsed:.0f} s")
    print(f"  RX overflows: {o}   TX underruns: {u}   late/seq/dropped: {other}")
    print(f"  wide frames accumulated: {wide_n} of {frames_expected:.0f} expected ({wide_pct:.1f}%)")
    print(f"  H I frames accumulated:  {h1_n} of {h1_expected:.0f} expected ({h1_pct:.1f}%)")
    if cross:
        print(f"  cross-spectrum frames:   {cross_n} ({100*cross_n/frames_expected:.1f}%)")
    print(f"  process CPU: {cpu_total:.0f}% of one core; hottest threads:")
    for cpu, name in by_thread[:6]:
        if cpu > 0.05:
            print(f"     {100*cpu/elapsed:5.0f}%  {name}")
    ok = (o == 0 and u == 0 and abs(wide_pct - 100) < 1.0 and abs(h1_pct - 100) < 1.0)
    print(f"  {'PASS' if ok else 'FAIL'}: {label}", flush=True)
    return ok


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rates", nargs="+", type=float, default=[8.0], help="sample rates to try, Msps")
    p.add_argument("--seconds", type=float, default=120.0, help="measurement time per case")
    p.add_argument("--sdr", default="b210", choices=("b210", "demo"))
    p.add_argument("--no-tx", action="store_true", help="receive graph only")
    p.add_argument("--no-cross", action="store_true", help="leave out the cross-spectrum branch")
    p.add_argument("--amplitude", type=float, default=0.0,
                   help="TX frame amplitude, 0..1 full scale; 0 sends zeros (default, nothing radiated)")
    p.add_argument("--tx-gain", type=float, default=0.0, help="TX gain, dB (default the minimum)")
    p.add_argument("--force", action="store_true", help="run even if the scheduler reports an observation")
    args = p.parse_args([a for a in sys.argv[1:] if a != "--headless"])

    if args.sdr == "b210" and not args.force:
        busy = scheduler_busy()
        if busy:
            sys.exit("the scheduler has an observation running; the radio is not free (--force to override)")
        if busy is None:
            print("  (scheduler not answering; assuming the radio is free)")
    results = []
    for r in args.rates:
        results.append(run_once(args, r * 1e6, tx=not args.no_tx, cross=not args.no_cross))
    print("\nSummary: " + ", ".join(f"{r:.0f} Msps {'PASS' if ok else 'FAIL'}" for r, ok in zip(args.rates, results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
