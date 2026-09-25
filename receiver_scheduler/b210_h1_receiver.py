#!/usr/bin/env python3
"""
Hydrogen Line (21cm) Receiver for SDR
Supports Ettus B210 and RTL-SDR
Uses GNU Radio for signal processing, PyQtGraph for display

Measures spectrum around 1420.405752 MHz, displays real-time integrated spectrum
and waterfall, and writes integrated data to HDF5.
"""

import argparse
import numpy as np
import h5py
import signal
import threading
import time
import json
import os
import re
import sys
from datetime import datetime, timezone
import math
from collections import deque

from gnuradio import gr, fft, blocks, analog, filter
from gnuradio.fft import window

from tuning import (ANALOG_BW_FACTOR as DEFAULT_ANALOG_BW_FACTOR,
                    DEFAULT_LO_OFFSET_HZ, describe_tuning, plan_tuning,
                    fixed_instrument, h1_subband_plan, describe_instrument)
import observation_files
import pilot as pilot_mod

# Qt is for the console display only. --headless runs the whole acquisition
# and recording path without it, so an observation over ssh neither needs a
# display nor needs PyQt installed at all.
#
# The flag is read from argv here, before the import, rather than left to
# argparse in main(). Importing PyQt5 and pyqtgraph is not free even when
# nothing is drawn - it maps the Qt5 core, gui and widget libraries into every
# observation - and an import cannot be undone once main() has started. Crude,
# but the alternative is splitting the GUI into its own module, and this keeps
# the receiver one file.
_WANT_GUI = '--headless' not in sys.argv
try:
    if not _WANT_GUI:
        raise ImportError("--headless: Qt deliberately not imported")
    from PyQt5 import QtWidgets, QtCore
    import pyqtgraph as pg
    QT_AVAILABLE = True
except ImportError as _qt_import_error:            # pragma: no cover
    QT_AVAILABLE = False
    _QT_IMPORT_ERROR = _qt_import_error
    # The GUI classes below subclass Qt types, so give them something to
    # subclass. Any attempt to instantiate one fails loudly; --headless never
    # touches them.
    class _QtMissing:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "PyQt5/pyqtgraph are not available, so the receiver GUI cannot "
                f"run ({_QT_IMPORT_ERROR}). Use --headless to record without "
                "it.")

    class _QtNamespace:
        QMainWindow = _QtMissing
        AxisItem = _QtMissing

        def __getattr__(self, name):
            return _QtMissing

    QtWidgets = QtCore = pg = _QtNamespace()

# Configuration (can be overridden via environment variables)
CENTER_FREQ = float(os.environ.get('H1_CENTER_FREQ', 1420.405752e6))
ANALOG_BW_FACTOR = float(os.environ.get('H1_ANALOG_BW_FACTOR',
                                        DEFAULT_ANALOG_BW_FACTOR))
# How far the LO sits above the line, so the DC artefact misses it. Set to 0
# to tune straight at the line, which is what every observation did before
# 2026-08-24 and is only useful for reproducing those.
LO_OFFSET_HZ = float(os.environ.get('H1_LO_OFFSET', DEFAULT_LO_OFFSET_HZ))
FFT_SIZE = int(os.environ.get('H1_FFT_SIZE', 4096))
INTEGRATION_TIME = float(os.environ.get('H1_INTEGRATION_TIME', 3.0))
# Which 10 MHz reference to run from. "auto" (the default) tries the external
# input and keeps it only if the device reports a lock, falling back to the
# internal TCXO and saying so. "external" insists and fails if it cannot lock;
# "internal" never looks. UHD does NOT do any of this on its own: a B200-series
# device defaults to `internal` and has no `auto` source, so before 2026-09-22
# a reference could sit on REF IN with the radio running off its own TCXO and
# nothing anywhere reporting it. The TCXO is good to about -2.4 ppm measured
# (0.08 km/s), which is why this went unnoticed.
CLOCK_SOURCE = (os.environ.get('H1_CLOCK_SOURCE') or 'auto').strip().lower()
# How long to let the reference PLL settle before believing `ref_locked`.
CLOCK_LOCK_TIMEOUT_S = float(os.environ.get('H1_CLOCK_LOCK_TIMEOUT', 2.0))
# What the device actually ended up running from: (source, locked). Set by
# create_sdr_source and written into every recording, because "which clock was
# this taken on" is not recoverable from the data afterwards.
CLOCK_STATE = (None, None)
# Where to record. The scheduler always sets H1_OUTPUT_FILE, so this default
# is for running the receiver by hand from a terminal - and it is resolved
# against this file rather than the working directory. It used to be the bare
# name "h1_data.h5", which meant a hand-started receiver dropped its recording
# wherever it happened to be launched from; started through the scheduler that
# was the repository root, and 22 files had collected there by 2026-08-25.
_DEFAULT_OUTPUT_FOLDER = observation_files.observations_folder(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
OUTPUT_FILE = os.environ.get('H1_OUTPUT_FILE') or observation_files.observation_filename(
    _DEFAULT_OUTPUT_FOLDER, observation_files.MANUAL_MODE)
WATERFALL_HISTORY = 100     # Number of spectra to show in waterfall

# Default blank-sky system temperature for the total-power calibration:
# SAWbird+ H1 LNA noise figure ~0.6 dB (~43 K), CMB + atmosphere +
# galactic background ~10 K, spillover and feed losses ~17 K.
DEFAULT_CAL_TEMP_K = 70.0

# Power-bar range applied when a calibration is taken: bar bottom just
# below blank sky, bar top at the approximate total system temperature
# with the beam on the quiet Sun (~1e3 K for a dish this size at 21 cm).
CAL_BAR_MIN_K = 50.0
CAL_BAR_SUN_K = float(os.environ.get('H1_SUN_TEMP_K', 1000.0))

# H I rest frequency (MHz) and speed of light (km/s) for the velocity
# axis drawn along the top of the spectrum plot
H1_REST_FREQ_MHZ = 1420.405752
C_KMS = 299792.458

# SDR-specific defaults
SDR_DEFAULTS = {
    'b210': {
        'sample_rate': 2.4e6,
        'gain': 30,
    },
    'rtlsdr': {
        'sample_rate': 2.048e6,
        'gain': 30,
    },
    'demo': {
        'sample_rate': 2.4e6,
        'gain': 0,
    }
}


def create_demo_source(sample_rate):
    """Create a simulated noise source for demo/testing without hardware."""
    print("  Using simulated noise source (demo mode)")
    print(f"  Sample rate: {sample_rate / 1e6:.3f} MHz")
    print(f"  Gain: N/A (demo)")
    source = analog.noise_source_c(analog.GR_GAUSSIAN, 0.1, 0)
    throttle = blocks.throttle(gr.sizeof_gr_complex, sample_rate, True)
    return source, throttle, sample_rate


def select_clock_source(usrp, requested=None, mboard=0):
    """Choose the 10 MHz reference and report what was actually achieved.

    Returns `(source, locked)`, where `source` is what the device ended up
    running from and `locked` is the `ref_locked` sensor if it could be read.

    The whole point is that selecting `external` is NOT self-checking: with
    nothing on REF IN the device runs on unlocked, so believing the request
    would report a disciplined clock that is not. The lock is what is trusted,
    never the request - the same rule the homing follows for its limit switch.
    """
    req = (requested if requested is not None else CLOCK_SOURCE) or 'auto'
    req = req.strip().lower()
    if req not in ('auto', 'internal', 'external'):
        print("  Clock: unknown source %r, using auto" % req, flush=True)
        req = 'auto'

    def locked():
        try:
            return bool(usrp.get_mboard_sensor('ref_locked', mboard).to_bool())
        except Exception:
            return None            # not every device exposes it

    if req == 'internal':
        usrp.set_clock_source('internal', mboard)
        print("  Clock: internal TCXO (H1_CLOCK_SOURCE=internal)", flush=True)
        return 'internal', None

    try:
        usrp.set_clock_source('external', mboard)
    except Exception as exc:
        if req == 'external':
            raise
        print("  Clock: external reference not selectable (%s); internal TCXO" % exc,
              flush=True)
        return 'internal', None

    # The reference PLL needs a moment; ask repeatedly rather than once.
    deadline = time.time() + max(0.0, CLOCK_LOCK_TIMEOUT_S)
    state = locked()
    while state is not True and time.time() < deadline:
        time.sleep(0.1)
        state = locked()

    if state is True:
        print("  Clock: EXTERNAL 10 MHz reference, locked", flush=True)
        return 'external', True
    if req == 'external':
        usrp.set_clock_source('internal', mboard)
        raise RuntimeError(
            "H1_CLOCK_SOURCE=external but the device never reported ref_locked "
            "(sensor read %r after %.1f s) - check the 10 MHz into REF IN"
            % (state, CLOCK_LOCK_TIMEOUT_S))
    usrp.set_clock_source('internal', mboard)
    print("  Clock: no external reference detected (ref_locked=%r); internal TCXO"
          % (state,), flush=True)
    return 'internal', state


def create_sdr_source(sdr_type, sample_rate, center_freq, gain):
    """Create appropriate SDR source block."""
    throttle = None

    if sdr_type == 'demo':
        src, thr, rate = create_demo_source(sample_rate)
        return src, thr, rate, center_freq      # demo tunes exactly, by fiat

    if sdr_type == 'b210':
        from gnuradio import uhd
        source = uhd.usrp_source(
            ",".join(("type=b200", "")),
            uhd.stream_args(
                cpu_format="fc32",
                args="",
                channels=[0],
            ),
        )
        # Before the rate and the tuning: changing the reference re-locks the
        # synthesisers, so a frequency set against the old one would be redone.
        global CLOCK_STATE
        CLOCK_STATE = select_clock_source(source)
        source.set_samp_rate(sample_rate)
        source.set_bandwidth(sample_rate * ANALOG_BW_FACTOR, 0)
        source.set_center_freq(center_freq, 0)
        source.set_gain(gain, 0)
        source.set_antenna("RX2", 0)

        actual_freq = source.get_center_freq(0)
        actual_rate = source.get_samp_rate()
        actual_gain = source.get_gain(0)

    elif sdr_type == 'rtlsdr':
        import osmosdr
        source = osmosdr.source(args="numchan=1 rtl=0")
        source.set_sample_rate(sample_rate)
        source.set_bandwidth(sample_rate, 0)
        source.set_center_freq(center_freq, 0)
        source.set_freq_corr(0, 0)
        source.set_dc_offset_mode(0, 0)
        source.set_iq_balance_mode(0, 0)
        source.set_gain_mode(False, 0)
        source.set_gain(gain, 0)
        source.set_if_gain(20, 0)
        source.set_bb_gain(20, 0)

        actual_freq = source.get_center_freq(0)
        actual_rate = source.get_sample_rate()
        actual_gain = source.get_gain(0)

    else:
        raise ValueError(f"Unknown SDR type: {sdr_type}")

    print(f"  Center frequency: {actual_freq / 1e6:.6f} MHz")
    print(f"  Sample rate: {actual_rate / 1e6:.3f} MHz")
    if abs(actual_freq - center_freq) > 1.0:
        # The frequency axis is built from this, and an axis that is wrong by
        # 10 kHz is a velocity scale wrong by 2 km/s. The sample rate has always
        # been taken from the hardware rather than the request; the frequency
        # had not been, which was a latent error waiting for a device that could
        # not hit what it was asked for. This B210 tunes exactly - measured
        # 0.000 kHz across the band used here - so nothing was ever wrong,
        # which is precisely why it would have stayed invisible.
        print(f"  NOTE: requested {center_freq / 1e6:.6f} MHz, hardware tuned "
              f"{(actual_freq - center_freq) / 1e3:+.3f} kHz away; the frequency "
              f"axis follows the hardware")
    print(f"  Gain: {actual_gain:.1f} dB")

    return source, throttle, actual_rate, actual_freq


class GNURadioFlowgraph(gr.top_block):
    """GNU Radio flowgraph for signal processing only."""

    def __init__(self, sdr_type, sample_rate, center_freq, gain, fft_size):
        gr.top_block.__init__(self, "H1 Processor", catch_exceptions=True)

        self.sdr_type = sdr_type
        self.sample_rate = sample_rate
        self.center_freq = center_freq
        self.gain = gain
        self.fft_size = fft_size

        self._build_blocks()
        self._connect_blocks()

    def _build_blocks(self):
        """Create GNU Radio blocks."""
        print(f"Initializing {self.sdr_type.upper()}...")
        try:
            self.sdr_source, self.throttle, actual_rate, actual_freq = \
                create_sdr_source(self.sdr_type, self.sample_rate,
                                  self.center_freq, self.gain)
            self.sample_rate = actual_rate
            # Follow the hardware, as the sample rate already did.
            self.center_freq = actual_freq
        except Exception as e:
            print(f"  Failed to initialize {self.sdr_type.upper()}: {e}")
            print("  Falling back to demo mode...")
            self.sdr_type = 'demo'
            self.sdr_source, self.throttle, actual_rate = create_demo_source(self.sample_rate)
            self.sample_rate = actual_rate

        # Stream to vector for FFT
        self.stream_to_vector = blocks.stream_to_vector(
            gr.sizeof_gr_complex, self.fft_size
        )

        # FFT
        self.fft_block = fft.fft_vcc(
            self.fft_size,
            True,
            window.blackmanharris(self.fft_size),
            True,
            1
        )

        # Complex to mag squared
        self.complex_to_mag_sq = blocks.complex_to_mag_squared(self.fft_size)

        # Short moving average for display smoothing only (0.5s)
        # Longer integration is done in Python for unlimited duration
        display_avg_time = 0.5  # seconds
        avg_length = max(1, int(self.sample_rate / self.fft_size * display_avg_time))
        self.moving_avg = blocks.moving_average_ff(
            avg_length,
            1.0 / avg_length,
            4000,
            self.fft_size
        )

        # Log10 and scale to dB
        self.nlog10 = blocks.nlog10_ff(10, self.fft_size, -10 * np.log10(self.fft_size))

        # Probe for getting spectrum data
        self.probe = blocks.probe_signal_vf(self.fft_size)

    def _connect_blocks(self):
        """Connect the flowgraph blocks."""
        if self.throttle is not None:
            self.connect((self.sdr_source, 0), (self.throttle, 0))
            signal_source = self.throttle
        else:
            signal_source = self.sdr_source

        self.connect((signal_source, 0), (self.stream_to_vector, 0))
        self.connect((self.stream_to_vector, 0), (self.fft_block, 0))
        self.connect((self.fft_block, 0), (self.complex_to_mag_sq, 0))
        self.connect((self.complex_to_mag_sq, 0), (self.moving_avg, 0))
        self.connect((self.moving_avg, 0), (self.nlog10, 0))
        self.connect((self.nlog10, 0), (self.probe, 0))

    def get_spectrum(self):
        """Get current integrated spectrum from probe."""
        return np.array(self.probe.level())


class _VectorAccumulator(gr.sync_block):
    """A sink that sums the power spectra it is given, exactly.

    The recorder reads and resets it once per integration period, so every
    spectrum the flowgraph computed goes into the record and none is counted
    twice - unlike the GUI path's probe, which samples a running mean at
    10 Hz. Fed by integrate_ff, which pre-sums a few dozen spectra in C++ so
    this Python block is called a few times a second, not thousands.
    """

    def __init__(self, vlen, presum):
        gr.sync_block.__init__(self, name="accumulate", in_sig=[(np.float32, vlen)],
                               out_sig=None)
        self.vlen = int(vlen)
        self.presum = int(presum)          # spectra already summed per input vector
        self._lock = threading.Lock()
        self._sum = np.zeros(self.vlen, dtype=np.float64)
        self._n = 0

    def work(self, input_items, output_items):
        block = input_items[0]
        with self._lock:
            self._sum += block.sum(axis=0, dtype=np.float64)
            self._n += block.shape[0]
        return block.shape[0]

    def take(self):
        """(mean spectrum, number of spectra) since the last call; (None, 0) if none."""
        with self._lock:
            if not self._n:
                return None, 0
            mean = self._sum / (self._n * self.presum)
            self._sum = np.zeros(self.vlen, dtype=np.float64)
            n, self._n = self._n * self.presum, 0
        return mean, n


class _PilotGate(gr.sync_block):
    """The pilot's recovery sink (issue #30): finds the comb by its own signature, and sums it.

    Fed two C++ branches off the pilot's own *unwindowed* FFT, each already
    summed over `presum` frames in C++ so this Python block runs at the
    flowgraph's sink rate (~50 Hz) rather than at 7800 frames a second. The
    first version did per-frame arithmetic here and cost 55-70% of a core -
    GNU Radio handed it 3.2 frames a call whatever `set_min_noutput_items`
    asked for, so it was call overhead, not work.

    - input 0: the cross-spectrum, sum over the block of F conj(R).
    - input 1: the power spectrum, sum over the block of |F|^2.

    A rectangular window is what makes the pilot's reference flat across the
    band: the frame is periodic at exactly the FFT length, so it has no
    leakage at all and every bin measures its own response. Through the wide
    product's Blackman-Harris window the reference instead follows the window
    in time, which for a swept chirp means zero at both band edges - where
    the filter's tilt has to be measured.

    **A block holds the comb when it says so coherently.** The cross-spectrum
    carries a phase ramp across the band - the fixed framing offset between
    transmit and receive - so its inverse transform peaks at that offset, and
    the height of that peak against the block's own noise is the statistic.
    It is *absolute*: the noise comes from the same block's own power, so
    nothing is compared with a running baseline and nothing depends on how
    bright the sky is. Measured per 20 ms block: 2.6 sigma with no comb (the
    expected maximum of 1024 Rayleigh draws), 286 with a full one, 32 with
    only a twentieth of the block covered, and the same on the Sun as on cold
    sky. The threshold sits at 8.

    Two earlier designs are recorded here because both are worse. Judging by
    the block's *power* against a median of recent blocks works, but it is
    second-hand: it deadlocks the first time the received power steps up and
    stays up (a slew onto the Sun, or the carrier starting late) because
    every block then reads "on" and the baseline never updates again, and its
    margin has to be large enough to beat the block scatter yet small enough
    to catch a comb that raises the total power by only 30% when the Sun is
    in the beam. Trusting the *command* is simpler but silent: the transmit
    buffers empty tens of milliseconds after the comb is switched off, and a
    science record that caught that tail would be reduced as sky. The
    coherent test needs neither a baseline nor the command, and would have
    caught a 1 ms tail.

    A block is committed only when it and both its neighbours agree, which
    drops the partial block at each edge of a burst - two blocks in 150,
    where counting a partial one as whole would bias the response by up to
    1.3%.

    take() hands back (X over the committed on-blocks, on-frames, frames,
    per-bin noise power) - the last measured on the off-blocks in the same
    FFT, carried across records so a record that is entirely burst still has
    one.
    """

    _NOISE_MEMORY = 0.2               # weight of a new record's off-blocks

    def __init__(self, vlen, presum, sigma):
        gr.sync_block.__init__(self, name="pilot_gate",
                               in_sig=[(np.complex64, vlen), (np.float32, vlen)],
                               out_sig=None)
        self.vlen = int(vlen)
        self.presum = int(presum)
        self._sigma = float(sigma)
        self._lock = threading.Lock()
        self._held = []               # (on, X, P) awaiting their neighbours
        self._x = np.zeros(self.vlen, dtype=np.complex128)
        self._noise = np.zeros(self.vlen, dtype=np.float64)
        self._noise_est = None        # per-bin, carried between records
        self._n_on = 0
        self._n_off = 0
        self._n = 0
        self.peak_sigma = 0.0         # the loudest block since the last take

    def set_reference_power(self, ref_power):
        """|R|^2 per bin: zero where the comb has no tone, so the statistic
        and its noise are both taken over the comb's own bins alone."""
        self._ref_pow = np.asarray(ref_power, dtype=np.float64)

    def work(self, input_items, output_items):
        X, P = input_items[0], input_items[1]
        n = min(X.shape[0], P.shape[0])
        with self._lock:
            for i in range(n):
                xb = X[i].astype(np.complex128)
                pb = P[i].astype(np.float64)
                # Var(X_j) = (sum of |F_j|^2 over the block) |R_j|^2, so the
                # delay transform's noise is that summed over the band.
                var = float((pb * self._ref_pow).sum()) / (self.vlen ** 2)
                peak = float(np.abs(np.fft.ifft(xb)).max())
                sigma = peak / math.sqrt(var) if var > 0 else 0.0
                self.peak_sigma = max(self.peak_sigma, sigma)
                on = sigma > self._sigma
                self._held.append((on, xb, pb))
                if len(self._held) >= 3:
                    a, b, c = self._held[-3], self._held[-2], self._held[-1]
                    if a[0] and b[0] and c[0]:
                        self._x += b[1]
                        self._n_on += self.presum
                    elif not (a[0] or b[0] or c[0]):
                        self._noise += b[2]
                        self._n_off += self.presum
                    del self._held[0]
                self._n += self.presum
        return n

    def take(self):
        """(X over on-blocks, on-frames, frames, per-bin noise power)."""
        with self._lock:
            x, n_on, n = self._x.copy(), self._n_on, self._n
            if self._n_off:
                fresh = self._noise / self._n_off
                self._noise_est = (fresh if self._noise_est is None else
                                   (1 - self._NOISE_MEMORY) * self._noise_est
                                   + self._NOISE_MEMORY * fresh)
            noise = (self._noise_est if self._noise_est is not None
                     else np.zeros(self.vlen))
            self._x = np.zeros(self.vlen, dtype=np.complex128)
            self._noise = np.zeros(self.vlen, dtype=np.float64)
            self._n_on = 0
            self._n_off = 0
            self._n = 0
            self.peak_sigma = 0.0
        return (x if n_on else None), n_on, n, noise


class TwoProductFlowgraph(gr.top_block):
    """The fixed instrument's flowgraph: one stream, two spectra (issue #27).

    The wide product is a coarse FFT of the whole band - continuum, RFI and
    the calibration comb live there. The H I product is a frequency-
    translating decimator centred on the sub-band, so the LO's DC spur at the
    band's low edge is outside it, followed by a fine FFT. Both branches sum
    their power spectra inside GNU Radio (integrate_ff, then an accumulating
    sink) and the recorder takes the mean once per integration period, in
    linear power - no dB round trip, no sampling of a running average.

    Decimating before the fine FFT is what keeps the cost down: the 2048-point
    transform runs at 4 Msps rather than 8, and the wide 1024-point one is
    cheap. The first version used moving_average_ff for the half-second
    smoothing the GUI path has, and profiled at a full core per branch - a
    3906-vector history over 1024-wide vectors - overflowing at 8 Msps where
    the old single graph did not. Summing is what was wanted anyway.
    """

    SINK_RATE_HZ = 50.0                # how often the Python sink is called

    def __init__(self, sdr_type, instrument, strict=True):
        gr.top_block.__init__(self, "H1 two-product processor", catch_exceptions=True)
        self.instrument = dict(instrument)
        self.sdr_type = sdr_type
        # No falling back to demo mode in a recording. On 2026-08-26 a gain
        # calibration could not open the B210 - an orphaned receiver still
        # held it - and recorded three minutes of synthetic noise instead,
        # which the fit then took for the sky: T_sys on its floor, a negative
        # correlation, and a bad calibration stored. A receiver that cannot
        # open the radio it was asked for must fail loudly.
        self.strict = strict
        self.sample_rate = float(instrument["sample_rate_hz"])
        self.center_freq = float(instrument["lo_hz"])
        self.gain = float(instrument["gain_db"])
        self.wide_channels = int(instrument["wide_channels"])
        self.subband = h1_subband_plan(instrument)
        self.pilot_cfg = pilot_mod.config_from(instrument)
        self.pilot = None                  # the plan, once the rate and LO are known
        self._build_blocks()
        self._build_pilot()
        self._connect_blocks()

    def _build_blocks(self):
        print(f"Initializing {self.sdr_type.upper()} (fixed instrument)...")
        try:
            self.sdr_source, self.throttle, actual_rate, actual_freq = \
                create_sdr_source(self.sdr_type, self.sample_rate,
                                  self.center_freq, self.gain)
            self.sample_rate = actual_rate
            self.center_freq = actual_freq
        except Exception as e:
            print(f"  Failed to initialize {self.sdr_type.upper()}: {e}", flush=True)
            if self.strict and self.sdr_type != 'demo':
                raise RuntimeError(
                    "could not open the %s (%s); refusing to record synthetic noise "
                    "in its place - is another receiver holding it?"
                    % (self.sdr_type.upper(), e))
            print("  Falling back to demo mode...")
            self.sdr_type = 'demo'
            self.sdr_source, self.throttle, actual_rate = create_demo_source(self.sample_rate)
            self.sample_rate = actual_rate

        # --- wide product: the whole band, coarsely ---
        nw = self.wide_channels
        self.wide_s2v = blocks.stream_to_vector(gr.sizeof_gr_complex, nw)
        self.wide_fft = fft.fft_vcc(nw, True, window.blackmanharris(nw), True, 1)
        self.wide_mag = blocks.complex_to_mag_squared(nw)
        # Per-bin power normalised by the transform length, so the counts
        # scale - and the gain in counts/K - does not depend on how many
        # channels the band is cut into. The old graph did this inside its
        # dB stage (-10 log10 N); the first version of this one dropped it,
        # and halving the H I channel count on 2026-08-26 halved every
        # count and silently invalidated the stored gain.
        self.wide_norm = blocks.multiply_const_vff([1.0 / nw] * nw)
        wide_presum = max(1, int(self.sample_rate / nw / self.SINK_RATE_HZ))
        self.wide_sum = blocks.integrate_ff(wide_presum, nw)
        self.wide_acc = _VectorAccumulator(nw, wide_presum)

        # --- H I product: translate, decimate, then a fine FFT ---
        sb = self.subband
        decim = int(self.instrument["h1_decimation"])
        taps = filter.firdes.low_pass(1.0, self.sample_rate, sb["cutoff_hz"],
                                      sb["transition_hz"])
        self.h1_xlate = filter.freq_xlating_fir_filter_ccf(
            decim, taps, sb["offset_from_lo_hz"], self.sample_rate)
        nh = int(sb["channels"])
        self.h1_s2v = blocks.stream_to_vector(gr.sizeof_gr_complex, nh)
        self.h1_fft = fft.fft_vcc(nh, True, window.blackmanharris(nh), True, 1)
        self.h1_mag = blocks.complex_to_mag_squared(nh)
        self.h1_norm = blocks.multiply_const_vff([1.0 / nh] * nh)
        h1_presum = max(1, int(sb["out_rate_hz"] / nh / self.SINK_RATE_HZ))
        self.h1_sum = blocks.integrate_ff(h1_presum, nh)
        self.h1_acc = _VectorAccumulator(nh, h1_presum)
        print("  H I sub-band: %.3f-%.3f MHz, %d taps, decimate by %d to %.1f Msps, "
              "%d channels of %.2f kHz" % (sb["band_hz"][0] / 1e6, sb["band_hz"][1] / 1e6,
                                          len(taps), decim, sb["out_rate_hz"] / 1e6,
                                          nh, sb["channel_width_hz"] / 1e3))

    def _build_pilot(self):
        """The pilot (issue #30): a transmit branch and a gated cross-spectrum branch.

        Transmit: a repeating wide-FFT frame into TX/RX of the same B210 at
        the same rate and LO as the receive side - zeros between bursts, the
        full-band comb during one. Recovery: the wide FFT's frames, gated per
        frame on the pilot's presence, multiplied by the conjugate of the
        frame's own windowed spectrum and summed. With the TX unconnected the
        recovery runs and finds nothing, which the recorder writes down.

        In demo mode there is no TX; with `demo_inject` the frame is added
        into the synthetic stream instead so the recovery can be tested.
        """
        cfg = self.pilot_cfg
        self.pilot_gate = None
        self.pilot_sink = None
        self.pilot_carrier_src = None
        self.pilot_comb_src = None
        self.pilot_comb_gain = None
        self.pilot_inject = None
        self._burst_on = False
        if not cfg["enabled"]:
            return
        self.pilot = pilot_mod.plan(cfg, self.center_freq, self.sample_rate, self.wide_channels)
        if len(self.pilot["bins"]) == 0:
            self.pilot = None
            return
        if cfg["tone_enabled"] and self.pilot["tone_bin"] is None:
            print("  Pilot WARNING: the carrier at %.3f MHz is outside the sampled band "
                  "or inside the LO guard; running without it"
                  % (cfg["tone_hz"] / 1e6), flush=True)
        nw = self.wide_channels
        # The comb is switched with a multiplier rather than by rewriting the
        # source's data: set_data on a running vector source rewrites 1024
        # complex samples under the scheduler's feet, where set_k touches one
        # scalar. The carrier's own source is never touched, so the carrier
        # is genuinely uninterrupted - which is what makes its power during a
        # burst the right reference for the records after it.
        scale = float(cfg["demo_inject_scale"]) if (self.sdr_type != 'b210'
                                                    and cfg.get("demo_inject")) else 1.0
        if self.sdr_type == 'b210' or cfg.get("demo_inject"):
            self.pilot_carrier_src = blocks.vector_source_c(
                (self.pilot["idle_frame"] * scale).tolist(), True)
            self.pilot_comb_src = blocks.vector_source_c(
                (self.pilot["comb_frame"] * scale).tolist(), True)
            self.pilot_comb_gain = blocks.multiply_const_cc(0.0)
            self.pilot_tx_add = blocks.add_cc()
        if self.sdr_type == 'b210':
            from gnuradio import uhd
            self.pilot_sink = uhd.usrp_sink(
                ",".join(("type=b200", "")),
                uhd.stream_args(cpu_format="fc32", args="", channels=[0]))
            self.pilot_sink.set_samp_rate(self.sample_rate)
            self.pilot_sink.set_center_freq(self.center_freq, 0)
            self.pilot_sink.set_gain(float(cfg["tx_gain_db"]), 0)
            self.pilot_sink.set_antenna("TX/RX", 0)
            tx_f = self.pilot_sink.get_center_freq(0)
            print("  Pilot TX: %.3f Msps at %.6f MHz, gain %.1f dB; %s"
                  % (self.pilot_sink.get_samp_rate() / 1e6, tx_f / 1e6,
                     self.pilot_sink.get_gain(0), pilot_mod.describe(cfg)), flush=True)
            if abs(tx_f - self.center_freq) > 0.5:
                # Different synthesiser settings on the two sides would let
                # the tones' phase slip and the coherent sum average away.
                print("  Pilot WARNING: TX tuned %.1f Hz from the RX LO; the recovery "
                      "will not accumulate coherently" % (tx_f - self.center_freq), flush=True)
        elif cfg.get("demo_inject"):
            self.pilot_inject = self.pilot_tx_add
            self.pilot_adder = blocks.add_cc()
        # The pilot's own FFT, unwindowed: fft_vcc with an empty window is
        # rectangular, which is what keeps the reference flat (see _PilotGate).
        # Fanned out from the wide product's framer, so no second
        # stream_to_vector and the two see exactly the same frames.
        self.pilot_fft = fft.fft_vcc(nw, True, [], True, 1)
        # Both branches summed in C++ over the same presum the other products
        # use, so the Python sink runs at the flowgraph's sink rate.
        presum = max(1, int(self.sample_rate / nw / self.SINK_RATE_HZ))
        ref = np.conj(np.asarray(self.pilot["reference"], dtype=np.complex64))
        self.pilot_mult = blocks.multiply_const_vcc(ref.tolist())
        self.pilot_xint = blocks.integrate_cc(presum, nw)
        self.pilot_mag = blocks.complex_to_mag_squared(nw)
        self.pilot_pint = blocks.integrate_ff(presum, nw)
        self.pilot_gate = _PilotGate(nw, presum, cfg["gate_sigma"])
        self.pilot_gate.set_reference_power(np.abs(self.pilot["reference"]) ** 2)

    def set_burst(self, on):
        """Comb on or off.

        The carrier has a source of its own and is never touched, so it runs
        through the burst uninterrupted - which is what makes its power during
        a burst the right reference for the records after it. The comb is
        switched by a multiplier rather than by rewriting a running vector
        source's samples under the scheduler. The gate is told nothing: it
        finds the comb by its own coherent signature, so the transmit
        buffers' latency needs no allowance and a record that caught a tail
        is known rather than assumed away.
        """
        if self.pilot_comb_gain is None or bool(on) == self._burst_on:
            return
        self.pilot_comb_gain.set_k(1.0 if on else 0.0)
        self._burst_on = bool(on)

    def _connect_blocks(self):
        src = self.sdr_source
        if self.throttle is not None:
            self.connect((self.sdr_source, 0), (self.throttle, 0))
            src = self.throttle
        if self.pilot_comb_gain is not None:
            self.connect((self.pilot_carrier_src, 0), (self.pilot_tx_add, 0))
            self.connect((self.pilot_comb_src, 0), (self.pilot_comb_gain, 0))
            self.connect((self.pilot_comb_gain, 0), (self.pilot_tx_add, 1))
        if self.pilot_inject is not None:
            self.connect((src, 0), (self.pilot_adder, 0))
            self.connect((self.pilot_inject, 0), (self.pilot_adder, 1))
            src = self.pilot_adder
        if self.pilot_sink is not None:
            self.connect((self.pilot_tx_add, 0), (self.pilot_sink, 0))
        if self.pilot_gate is not None:
            self.connect((self.wide_s2v, 0), (self.pilot_fft, 0))
            self.connect((self.pilot_fft, 0), (self.pilot_mult, 0))
            self.connect((self.pilot_mult, 0), (self.pilot_xint, 0))
            self.connect((self.pilot_xint, 0), (self.pilot_gate, 0))
            self.connect((self.pilot_fft, 0), (self.pilot_mag, 0))
            self.connect((self.pilot_mag, 0), (self.pilot_pint, 0))
            self.connect((self.pilot_pint, 0), (self.pilot_gate, 1))
        self.connect((src, 0), (self.wide_s2v, 0))
        self.connect((self.wide_s2v, 0), (self.wide_fft, 0))
        self.connect((self.wide_fft, 0), (self.wide_mag, 0))
        self.connect((self.wide_mag, 0), (self.wide_norm, 0))
        self.connect((self.wide_norm, 0), (self.wide_sum, 0))
        self.connect((self.wide_sum, 0), (self.wide_acc, 0))
        self.connect((src, 0), (self.h1_xlate, 0))
        self.connect((self.h1_xlate, 0), (self.h1_s2v, 0))
        self.connect((self.h1_s2v, 0), (self.h1_fft, 0))
        self.connect((self.h1_fft, 0), (self.h1_mag, 0))
        self.connect((self.h1_mag, 0), (self.h1_norm, 0))
        self.connect((self.h1_norm, 0), (self.h1_sum, 0))
        self.connect((self.h1_sum, 0), (self.h1_acc, 0))

    # Frequency axes, in true sky frequency, fftshifted like the probes.
    def wide_freq_axis(self):
        return self.center_freq + np.fft.fftshift(
            np.fft.fftfreq(self.wide_channels, 1.0 / self.sample_rate))

    def h1_freq_axis(self):
        sb = self.subband
        return sb["centre_hz"] + np.fft.fftshift(
            np.fft.fftfreq(int(sb["channels"]), 1.0 / sb["out_rate_hz"]))

    def h1_keep(self):
        """Which fine channels lie inside the H I sub-band (the rest are the
        decimator's transition band and are not recorded)."""
        f = self.h1_freq_axis()
        lo, hi = self.subband["band_hz"]
        return (f >= lo) & (f <= hi)

    def take_wide(self):
        """(mean wide spectrum, spectra counted) since the last call."""
        return self.wide_acc.take()

    def take_h1(self):
        return self.h1_acc.take()

    def take_pilot(self):
        """(X over on-frames, on-frames, frames, off-frame noise per bin) since the last call."""
        if self.pilot_gate is None:
            return None, 0, 0, 0.0
        return self.pilot_gate.take()


class _OverflowCounter:
    """Count UHD's overflow marks so the recording can say when samples were lost.

    UHD writes a bare `O` to the terminal for every overflow and nothing else
    reaches Python - gr-uhd exposes no counter. The 2026-08-26 ladder showed
    why that matters: at 16 Msps most of the stream was dropped and the file
    still held tidy 3 s records with nothing in them to say so. So the
    process's stdout and stderr are routed through a pipe, copied on to
    where they were going, and the lone capital O's counted on the way -
    lone, because the receiver's own messages contain the letter.
    """

    def __init__(self):
        self.count = 0
        self.underflows = 0
        self._lock = threading.Lock()
        self._orig = {}
        self._thread = None
        self._read_fd = None

    def start(self):
        try:
            r, w = os.pipe()
            for fd in (1, 2):
                self._orig[fd] = os.dup(fd)
                os.dup2(w, fd)
            os.close(w)
            self._read_fd = r
            self._thread = threading.Thread(target=self._pump, daemon=True)
            self._thread.start()
        except OSError:
            self._read_fd = None

    _MESSAGE = re.compile(rb"(\d+) overflows? occurred")
    # The transmit side (the pilot's sink) prints a "U" per underflow, and
    # its "In the last N ms, M underflows occurred" summary only minutes
    # later at irregular intervals - the first run with the summary counted
    # (2026-09-22) had twelve marks in the log and zeros in every record.
    # So the marks are counted here, as they arrive: the carrier is silent
    # for every underflow while the sky noise is not, and a record's carrier
    # power reads low by the fraction of it the transmitter missed. A mark
    # is a U not inside a word: the marks come in runs ("UUUU") and butt on
    # to the next message ("Uusrp_sink"), so a U after another U or before a
    # lower-case letter or an overflow's O counts, while the "UHD" and
    # "USB" of the start-up banner do not.
    _UMARK = re.compile(rb"(?<![A-NP-TV-Za-z])U(?![A-NP-TV-Z])")

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
            # gr-uhd follows its run of O's with "In the last N ms, M
            # overflows occurred", which is the exact count; the O's are
            # only a picture of it. The tail is carried over so a message
            # split across two reads is still seen whole.
            data = carry + chunk
            n = sum(int(m.group(1)) for m in self._MESSAGE.finditer(data))
            u = len(self._UMARK.findall(chunk))
            cut = data.rfind(b"\n")
            carry = data[cut + 1:] if cut >= 0 else data[-64:]
            if n or u:
                with self._lock:
                    self.count += n
                    self.underflows += u

    def take(self):
        """Overflows since the last call."""
        with self._lock:
            n, self.count = self.count, 0
        return n

    def take_underflows(self):
        """Transmit underflows since the last call."""
        with self._lock:
            n, self.underflows = self.underflows, 0
        return n

    def stop(self):
        # Put the descriptors back first: that closes the pipe's last writer,
        # the pump sees EOF, and everything still in the pipe - the final
        # "Total spectra saved" line included - is copied through before
        # the read end is closed.
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:                                 # noqa: BLE001
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


def init_hdf5(filename, freq_axis_hz, fft_size, sdr_type, center_freq,
              sample_rate, gain, tuning_plan=None, segment=0, segment_reason='',
              wide=None, instrument=None, pilot=None, pilot_burst_every=0):
    """Create the observation file and its datasets.

    Module level and Qt-free so the window and the headless recorder write
    byte-identical files - the layout, the attributes and the scheduler
    metadata are the thing downstream code reads, and two copies of it would
    drift apart.

    `freq_axis_hz`/`fft_size` describe the H I product, which keeps the
    original dataset names (`frequency_hz`, `spectra_*`) so every existing
    reader sees a new file as it saw an old one. `wide`, when given as
    {'freq_axis_hz': ..., 'channels': n}, adds the continuum product under
    `frequency_hz_wide` / `spectra_wide_*`, and `instrument` (the fixed
    instrument dict, tuning.fixed_instrument) is recorded whole. A file with
    a wide product also carries `overflows` and `underflows`, one count per record.
    """
    # The recordings folder may not exist yet - on a fresh checkout, or the
    # first time a hand-started receiver runs. Creating it here covers every
    # caller, including a mid-run roll to a new file, where failing would lose
    # the rest of the observation rather than merely failing to start it.
    folder = os.path.dirname(os.path.abspath(filename))
    if folder:
        os.makedirs(folder, exist_ok=True)

    # libver='latest' is what makes SWMR possible below: a recording can then
    # be read while it is being written. It writes the HDF5 1.10 file format,
    # fine for h5py, current MATLAB and the notebook.
    hf = h5py.File(filename, 'w', libver='latest')

    hf.create_dataset('frequency_hz', data=freq_axis_hz)

    # The bandpass correction, one value per channel, stored once. It is a
    # function of frequency alone and constant for the whole run, so a single
    # array of a few thousand floats makes every spectrum in the file exactly
    # reversible - raw = (kelvin + T_sys) * gain * correction - without anyone
    # needing this repository to evaluate a polynomial.
    #
    # Channels the template cannot speak for carry 1.0 rather than NaN, and
    # bandpass_valid says which those are. That is what keeps the filter skirts
    # in the file: they are stored uncorrected but recoverable, so a future
    # bandpass fit can still use them. Dropping them at write time would have
    # discarded 18% of the recorded band permanently.
    correction, valid = _bandpass_correction(
        freq_axis_hz, {'center_freq_hz': center_freq,
                       'sample_rate_hz': sample_rate, 'gain_db': gain})
    hf.create_dataset('bandpass_correction', data=correction.astype('float32'))
    hf.create_dataset('bandpass_valid', data=valid)

    # Spectra are stored in kelvin when the instrument is calibrated for this
    # tuning, and in raw counts when it is not. The dataset *name* carries the
    # units rather than an attribute, so a reader written for one cannot
    # silently misread the other: an old script asking for spectra_linear on a
    # calibrated file gets a KeyError, which is the safe way to be wrong.
    #
    # Both require the bandpass template as well as the gain. The gain was
    # fitted against corrected spectra, so applying it to uncorrected ones
    # would be mixing two scales - better to leave the file in counts and say
    # so than to write a number that looks like a temperature.
    # The spectra dataset is created further down, once the tuning attributes
    # exist: whether the file can be written in kelvin depends on whether the
    # calibration applies to *this* tuning, and that question cannot be asked
    # before the tuning has been recorded.

    hf.create_dataset('timestamps',
                      shape=(0,),
                      maxshape=(None,),
                      dtype='float64')

    hf.create_dataset('integration_times',
                      shape=(0,),
                      maxshape=(None,),
                      dtype='float32')

    hf.attrs['sdr_type'] = sdr_type
    hf.attrs['center_freq_hz'] = center_freq
    hf.attrs['sample_rate_hz'] = sample_rate
    hf.attrs['fft_size'] = fft_size
    hf.attrs['gain_db'] = gain
    hf.attrs['nominal_integration_time'] = INTEGRATION_TIME
    # Which reference the sample clock ran from. Not recoverable from the data
    # afterwards, and it decides whether a fitted velocity shift is the TCXO's
    # -2.4 ppm or something real.
    if CLOCK_STATE[0]:
        hf.attrs['clock_source'] = CLOCK_STATE[0]
        hf.attrs['clock_ref_locked'] = (-1 if CLOCK_STATE[1] is None
                                        else int(bool(CLOCK_STATE[1])))
    # frequency_hz already holds true sky frequency, so nothing downstream has
    # to know the LO was offset. These say where the DC artefact went, which is
    # the one thing a spectrum cannot show for itself.
    if tuning_plan:
        hf.attrs['lo_offset_hz'] = tuning_plan['lo_offset_hz']
        hf.attrs['sky_center_freq_hz'] = tuning_plan['sky_center_freq_hz']
        hf.attrs['dc_artefact_freq_hz'] = tuning_plan['tuned_center_freq_hz']
        hf.attrs['sample_rate_requested_hz'] = tuning_plan['requested_sample_rate_hz']
    if instrument:
        # The fixed instrument, whole, and the two bands it defines - what
        # every consumer needs to cut continuum from hydrogen.
        import json as _json
        hf.attrs['instrument'] = _json.dumps(instrument)
        hf.attrs['h1_band_hz'] = [float(x) for x in instrument['h1_band_hz']]
        hf.attrs['continuum_band_hz'] = [float(x) for x in instrument['continuum_band_hz']]
        hf.attrs['dc_artefact_freq_hz'] = float(instrument['lo_hz'])
        hf.attrs['sky_center_freq_hz'] = float(np.mean(instrument['h1_band_hz']))
        hf.attrs['sample_rate_requested_hz'] = float(
            instrument['h1_band_hz'][1] - instrument['h1_band_hz'][0])
        hf.attrs['product'] = 'h1'
    hf.attrs['created'] = datetime.now(timezone.utc).isoformat()

    _embed_calibration(hf)

    if wide is not None:
        # The continuum product: its own axis and per-channel correction (the
        # wide template, when there is one that applies), and counts unless
        # both that template and the gain apply. Same unit-in-the-name rule.
        wide_axis = np.asarray(wide['freq_axis_hz'], dtype=float)
        nw = int(wide['channels'])
        hf.create_dataset('frequency_hz_wide', data=wide_axis)
        w_corr, w_valid = _bandpass_correction(
            wide_axis, {'center_freq_hz': center_freq,
                        'sample_rate_hz': sample_rate, 'gain_db': gain},
            product='wide')
        hf.create_dataset('bandpass_correction_wide', data=w_corr.astype('float32'))
        hf.create_dataset('bandpass_valid_wide', data=w_valid)
        hf.create_dataset('overflows', shape=(0,), maxshape=(None,), dtype='int32')
        hf.create_dataset('underflows', shape=(0,), maxshape=(None,), dtype='int32')

    # The pilot (issue #30). Per record: whether it was a burst (1), a science
    # record that caught a burst's tail (2) or clean (0); the level and slope
    # *applied* (unit and zero when nothing was) and the index of the
    # correction vector applied (-1 for none), the burst detection SNR and
    # the ok flag. Per detected burst: the complex response per bin. Each
    # time the applied passband correction changes: the vectors, on both
    # axes. `pilot_reference` is the response the level is measured against
    # - filled in when known (SWMR allows writing an existing dataset, not
    # creating one), zeros until then - and `pilot_anchored` says whether it
    # came from the calibration or from this run's first detected burst.
    pilot_cfg = None
    if instrument and isinstance(instrument.get('pilot'), dict):
        pilot_cfg = instrument['pilot']
    if pilot_cfg is not None:
        import json as _json
        hf.attrs['pilot'] = _json.dumps(pilot_cfg)
    if pilot is not None and pilot_cfg and pilot_cfg.get('enabled'):
        nb_p = len(pilot['bins'])
        hf.attrs['pilot_centre_hz'] = float(pilot['centre_hz'])
        hf.attrs['pilot_applied'] = 1
        # The cadence actually used: the configuration asks for an interval
        # in seconds, and what that comes to in records depends on this
        # observation's integration time.
        hf.attrs['pilot_burst_every_records'] = int(pilot_burst_every)
        hf.create_dataset('pilot_bins_wide', data=np.asarray(pilot['bins'], dtype='int32'))
        hf.create_dataset('pilot_reference', data=np.zeros(nb_p, dtype='complex64'))
        hf.create_dataset('pilot_anchored', data=np.zeros(1, dtype='int8'))
        if pilot.get('tone_bin') is not None:
            hf.attrs['pilot_tone_hz'] = float(pilot['tone_hz'])
            hf.attrs['pilot_tone_applied'] = 1 if pilot_cfg.get('tone_apply') else 0
        for name, dt in (('pilot_burst', 'int8'), ('pilot_level', 'float32'),
                         ('pilot_slope', 'float32'), ('pilot_snr', 'float32'),
                         ('pilot_ok', 'int8'), ('pilot_correction_index', 'int32'),
                         ('pilot_tone_power', 'float32'), ('pilot_tone_level', 'float32'),
                         ('pilot_tone_ok', 'int8')):
            hf.create_dataset(name, shape=(0,), maxshape=(None,), dtype=dt)
        hf.create_dataset('pilot_shape_time', shape=(0,), maxshape=(None,), dtype='float64')
        hf.create_dataset('pilot_shape', shape=(0, nb_p), maxshape=(None, nb_p),
                          dtype='complex64', chunks=(1, max(nb_p, 1)))
        n_fine = len(freq_axis_hz)
        hf.create_dataset('pilot_correction_h1', shape=(0, n_fine), maxshape=(None, n_fine),
                          dtype='float32', chunks=(1, n_fine))
        if wide is not None:
            nw_p = int(wide['channels'])
            hf.create_dataset('pilot_correction_wide', shape=(0, nw_p), maxshape=(None, nw_p),
                              dtype='float32', chunks=(1, nw_p))

    # Spectra are stored in kelvin when the instrument is calibrated for this
    # tuning, and in raw counts when it is not. The dataset *name* carries the
    # units rather than an attribute, so a reader written for one cannot
    # silently misread the other: a script asking for spectra_linear on a
    # calibrated file gets a KeyError, which is the safe way to be wrong.
    #
    # Both the template and the gain have to apply. The gain was fitted against
    # corrected spectra, so applying it to uncorrected ones would mix two
    # scales - better to leave the file in counts and say so than to write a
    # number that looks like a temperature.
    cal_gain, cal_t_sys = _calibration_for_writing(hf)
    spectra_name = 'spectra_kelvin' if cal_gain else 'spectra_linear'
    hf.attrs['spectra_units'] = 'K' if cal_gain else 'counts'
    if cal_gain:
        hf.attrs['applied_gain_counts_per_k'] = cal_gain
        hf.attrs['applied_t_sys_k'] = cal_t_sys
    hf.create_dataset(spectra_name,
                      shape=(0, fft_size),
                      maxshape=(None, fft_size),
                      dtype='float32',
                      chunks=(1, fft_size),
                      compression='gzip',
                      compression_opts=4)
    if wide is not None:
        nw = int(wide['channels'])
        # Kelvin only when the wide template applies too; the gain is the
        # same scale for both products because the wide template is
        # normalised over the H I band (bandpass.fit_bandpass).
        wide_cal = bool(cal_gain) and bool(hf['bandpass_valid_wide'][:].any())
        hf.attrs['spectra_wide_units'] = 'K' if wide_cal else 'counts'
        hf.create_dataset('spectra_wide_kelvin' if wide_cal else 'spectra_wide_linear',
                          shape=(0, nw), maxshape=(None, nw), dtype='float32',
                          chunks=(1, nw), compression='gzip', compression_opts=4)

    # Observation metadata from scheduler
    import json as _json
    obs_meta = os.environ.get('H1_OBS_METADATA', '')
    if obs_meta:
        try:
            meta = _json.loads(obs_meta)
            for key, val in meta.items():
                if isinstance(val, bool):
                    hf.attrs[key] = int(val)
                elif val is not None and val != '':
                    hf.attrs[key] = val
        except Exception:
            pass

    # Which piece of a session this is, and why the previous piece ended - a
    # mid-run roll to a new file. Written here, not by the caller afterwards,
    # because nothing may add attributes once SWMR mode is on.
    if segment:
        hf.attrs['segment'] = int(segment)
        hf.attrs['segment_reason'] = str(segment_reason or '')

    # Single-writer / multiple-reader: from here on the file can be opened
    # for reading by another process - the Observe tab, a notebook - while
    # the spectra are still arriving. The constraints are that no dataset or
    # attribute may be created after this point, and that anything extended
    # must be chunked; both hold by the layout above (every extensible
    # dataset has a maxshape, every attribute is written before the first
    # spectrum, and a change of geometry rolls to a new file rather than
    # restructuring this one). Readers see the file as of the last flush,
    # which append_spectrum does per record.
    hf.flush()
    hf.swmr_mode = True
    return hf


def _embed_calibration(hf):
    """Store the bandpass template and gain calibration in force, in the file.

    The recording itself stays raw - counts, uncorrected - and that is right:
    a better template or a better gain should be able to re-reduce it later.
    But raw is only useful if what it takes to reduce it travels with it.
    Without this the file is reducible on exactly one machine, for exactly as
    long as nobody remeasures anything: the pipeline reads whichever
    bandpass_template.json and gain_calibration.json happen to be on disk at
    the time it is asked, so an archived observation silently acquires whatever
    calibration is current rather than the one it was taken under.

    Stored whether or not they match this tuning. Whether a calibration applies
    is a decision for whoever reduces the file - `bandpass.applies_to` and
    `rf_calibration.calibration_applies_to` both check the tuning, which is in
    these attributes already - and recording only the ones that happened to
    match would throw away the evidence for that judgement.

    Written as JSON strings: they are a few kilobytes, they are read back with
    one json.loads, and they do not need the HDF5 schema to grow a field every
    time the calibration does. Missing or unreadable files are not an error -
    an observation is worth more than its provenance.
    """
    import json as _json

    here = os.path.dirname(os.path.abspath(__file__))
    for name, attr in (("bandpass_template.json", "bandpass_template"),
                       ("bandpass_template_wide.json", "bandpass_template_wide"),
                       ("gain_calibration.json", "gain_calibration")):
        try:
            with open(os.path.join(here, name)) as fh:
                doc = _json.load(fh)
            hf.attrs[attr] = _json.dumps(doc)
        except (OSError, ValueError):
            continue
    # The beam and the site, because turning antenna temperature into flux
    # needs them and they are measured quantities that could change.
    try:
        import sys as _sys
        sim = os.path.join(os.path.dirname(here), "astro_simulator")
        if sim not in _sys.path:
            _sys.path.insert(0, sim)
        import instrument
        hf.attrs["beam_fwhm_deg"] = instrument.beam_fwhm_deg()
        hf.attrs["effective_area_m2"] = instrument.effective_area_m2()
        hf.attrs["site_lat_deg"] = instrument.SITE_LAT_DEG
        hf.attrs["site_lon_deg"] = instrument.SITE_LON_DEG
        hf.attrs["site_height_m"] = instrument.SITE_HEIGHT_M
    except Exception:                                     # noqa: BLE001
        pass


def _pilot_reference_from_calibration(planned, instrument=None):
    """The per-bin pilot response stored with the gain calibration, if it was
    made for this pilot plan and this tuning; else None (issue #30).

    The tuning check matters as much as the bins. The pilot's level is the
    *power gain relative to the reference*, so a reference taken at a
    different receiver gain reads as the gain ratio - a 10 dB override is a
    factor of ten, outside the sanity band, and every burst would then be
    refused with no fallback. Same for the LO and the rate, which move the
    passband under the bins. The calibration's own `applies_to` check makes
    the same demand of the bandpass and the gain, and for the same reason.
    """
    import json as _json
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "gain_calibration.json")) as fh:
            cal = _json.load(fh)
        ref = cal.get("pilot_reference")
        if not ref:
            return None
        if list(int(b) for b in ref.get("bins", [])) != [int(b) for b in planned["bins"]]:
            return None
        if instrument is not None:
            for key, tol in (("gain_db", 0.01), ("lo_hz", 1.0), ("sample_rate_hz", 1.0)):
                want, have = ref.get(key), instrument.get(key)
                if want is None or have is None or abs(float(want) - float(have)) > tol:
                    print("  Pilot: the calibration's reference was taken at a different "
                          "%s; self-referencing this run instead" % key, flush=True)
                    return None
        h = np.asarray(ref["response_re"], dtype=float) + 1j * np.asarray(ref["response_im"], dtype=float)
        return h if len(h) == len(planned["bins"]) else None
    except Exception:                                     # noqa: BLE001
        return None


def _calibration_for_writing(hf):
    """(gain, T_sys) to record spectra in kelvin with, or (None, None).

    Both the bandpass template and the gain have to apply to this tuning. The
    check is the same one the reduction uses, run against the attributes just
    written, so a file is never calibrated by a gain that the pipeline would
    afterwards refuse.
    """
    import json as _json

    try:
        header = dict(hf.attrs)
        import sys as _sys
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in _sys.path:
            _sys.path.insert(0, here)
        import bandpass as _bp
        import rf_calibration as _rf
        template = _bp.load_bandpass()
        ok_bp, _ = _bp.applies_to(template, header)
        if not ok_bp:
            return None, None
        cal = _rf.load_calibration()
        ok_cal, _ = _rf.calibration_applies_to(cal, header)
        if not ok_cal or not cal.get("gain_counts_per_k"):
            return None, None
        return float(cal["gain_counts_per_k"]), float(cal["t_sys_k"])
    except Exception:                                     # noqa: BLE001
        return None, None


def _bandpass_correction(freq_axis_hz, header=None, product='h1'):
    """(correction, valid) for this frequency axis, from the template in force.

    `product` picks the template: the H I one (bandpass_template.json) or
    the wide one (bandpass_template_wide.json), each fitted on its own
    product's channels.

    The correction is what the spectrum is divided by. Where the template has
    nothing to say the correction is 1.0 and valid is False, so the channel is
    stored as it was measured and can still be recovered.

    The header gates the whole thing through applies_to: the template's
    polynomial is a function of frequency *relative to the LO it was fitted
    at*, and the hardware's passband moves with the LO. Evaluated at a
    different tuning the polynomial still returns finite numbers over
    whatever frequencies happen to overlap - retuning 1420.4 to 1419.0 left
    60% of channels "covered" by a shape displaced 1.4 MHz from the real
    passband, marked valid, and quietly divided into the live summary. A
    correction for the wrong tuning is worse than none.
    """
    import numpy as _np

    ones = _np.ones(len(freq_axis_hz), dtype=float)
    novalid = _np.zeros(len(freq_axis_hz), dtype=bool)
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        import sys as _sys
        if here not in _sys.path:
            _sys.path.insert(0, here)
        import bandpass as _bp
        template = _bp.load_bandpass(product=product)
        if not template:
            return ones, novalid
        if header is not None and not _bp.applies_to(template, header)[0]:
            return ones, novalid
        model = _np.asarray(_bp.evaluate(template, _np.asarray(freq_axis_hz, float)),
                            dtype=float)
        valid = _np.isfinite(model) & (model > 0)
        correction = _np.where(valid, model, 1.0)
        return correction, valid
    except Exception:                                     # noqa: BLE001
        return ones, novalid


def append_spectrum(hf, avg_linear, timestamp, integration_time, fft_size,
                    wide_linear=None, overflows=0, pilot=None, underflows=0):
    """Append one integrated spectrum, flushing so a reader sees it promptly.

    `wide_linear` is the continuum product's record for a file that has one;
    `overflows` the UHD overflow count during this record, `underflows` the
    pilot transmitter's.

    `timestamp` is passed as the *end* of the integration (time.time() when the
    record was taken). A record is the mean over its whole interval, so it
    belongs at the CENTRE - it is shifted back by half the integration here, once,
    so the HDF5 and the live sidecar both carry the midpoint. Stamping the end put
    every feature half an integration late on the time axis (a Sun-drift transit
    came out tau/2 = 15 s late, 2026-08-27). Commissioning recordings are junked,
    so there is no compatibility shim, and analysis must no longer subtract tau/2.
    """
    timestamp = timestamp - integration_time / 2.0
    name = 'spectra_kelvin' if 'spectra_kelvin' in hf else 'spectra_linear'
    values = avg_linear
    # The pilot (issue #30): a clean science record with a recent burst
    # behind it is divided by the burst's level-and-slope factor and by the
    # current passband correction vector; the numbers and the vector's index
    # are stored beside the record so read_observation multiplies them back
    # exactly. Burst records and records that caught a burst's tail are
    # written as measured and flagged; nothing is applied to them.
    p_burst, p_level, p_slope, p_ok, p_snr, p_idx = 0, 1.0, 0.0, 0, 0.0, -1
    p_tone_power, p_tone_level, p_tone_ok = 0.0, 1.0, 0
    if pilot is not None and 'pilot_level' in hf:
        p_burst = int(pilot.get('burst', 0) or 0)
        p_ok = 1 if (pilot.get('ok') and not p_burst) else 0
        if p_ok:
            p_level, p_slope = float(pilot['level']), float(pilot['slope'])
            p_idx = int(pilot.get('corr_index', -1))
        p_snr = float(pilot.get('snr', 0.0) or 0.0)
        p_tone_power = float(pilot.get('tone_power', 0.0) or 0.0)
        # The carrier's factor is flat across the band - it is the fast
        # common-mode gain - and is applied only when the entry says so
        # (`tone_apply`), which waits on the bench correlation test.
        if pilot.get('tone_ok') and not p_burst:
            p_tone_ok = 1
            p_tone_level = float(pilot.get('tone_level', 1.0))
    apply_pilot = ('pilot_level' in hf) and p_ok
    apply_tone = ('pilot_tone_level' in hf) and p_tone_ok and int(hf.attrs.get('pilot_tone_applied', 0))
    tone_div = p_tone_level if apply_tone else 1.0
    if name == 'spectra_kelvin':
        # counts -> kelvin, with the correction the file already carries, so
        # what is written is exactly what the stored numbers reverse.
        divisor = hf['bandpass_correction'][:] * hf.attrs['applied_gain_counts_per_k']
        if apply_pilot:
            divisor = divisor * pilot_mod.factor(p_level, p_slope, hf['frequency_hz'][:],
                                                 hf.attrs['pilot_centre_hz'])
            if p_idx >= 0 and 'pilot_correction_h1' in hf:
                divisor = divisor * hf['pilot_correction_h1'][p_idx, :]
        values = avg_linear / (divisor * tone_div) - hf.attrs['applied_t_sys_k']
    n = hf[name].shape[0]
    hf[name].resize((n + 1, fft_size))
    hf['timestamps'].resize((n + 1,))
    hf['integration_times'].resize((n + 1,))
    hf[name][n, :] = np.asarray(values, dtype=np.float32)
    hf['timestamps'][n] = timestamp
    hf['integration_times'][n] = integration_time
    if wide_linear is not None and 'frequency_hz_wide' in hf:
        wname = 'spectra_wide_kelvin' if 'spectra_wide_kelvin' in hf else 'spectra_wide_linear'
        wvalues = np.asarray(wide_linear, dtype=float)
        if wname == 'spectra_wide_kelvin':
            wdiv = hf['bandpass_correction_wide'][:] * hf.attrs['applied_gain_counts_per_k']
            if apply_pilot:
                wdiv = wdiv * pilot_mod.factor(p_level, p_slope, hf['frequency_hz_wide'][:],
                                               hf.attrs['pilot_centre_hz'])
                if p_idx >= 0 and 'pilot_correction_wide' in hf:
                    wdiv = wdiv * hf['pilot_correction_wide'][p_idx, :]
            wvalues = wvalues / (wdiv * tone_div) - hf.attrs['applied_t_sys_k']
        nw = hf[wname].shape[1]
        hf[wname].resize((n + 1, nw))
        hf[wname][n, :] = wvalues.astype(np.float32)
    if 'overflows' in hf:
        hf['overflows'].resize((n + 1,))
        hf['overflows'][n] = int(overflows)
    if 'underflows' in hf:
        hf['underflows'].resize((n + 1,))
        hf['underflows'][n] = int(underflows)
    if 'pilot_level' in hf:
        for dname, val in (('pilot_burst', p_burst), ('pilot_level', p_level),
                           ('pilot_slope', p_slope), ('pilot_snr', p_snr), ('pilot_ok', p_ok),
                           ('pilot_correction_index', p_idx),
                           ('pilot_tone_power', p_tone_power),
                           ('pilot_tone_level', p_tone_level if apply_tone else 1.0),
                           ('pilot_tone_ok', p_tone_ok)):
            hf[dname].resize((n + 1,))
            hf[dname][n] = val
    hf.flush()
    _append_live_summary(hf, avg_linear, timestamp, integration_time, n + 1,
                         wide_linear=wide_linear, overflows=overflows,
                         pilot={'burst': p_burst, 'ok': p_ok, 'level': p_level, 'slope': p_slope,
                                'snr': p_snr, 'seen': int(bool((pilot or {}).get('seen'))),
                                'tone_ok': p_tone_ok, 'tone_level': p_tone_level,
                                'tone_ratio': float((pilot or {}).get('tone_ratio', 0.0) or 0.0)}
                         if 'pilot_level' in hf else None)
    return n + 1


def append_pilot_correction(hf, h1_vec, wide_vec):
    """Store a new applied passband correction (issue #30); returns its index."""
    if 'pilot_correction_h1' not in hf:
        return -1
    n = hf['pilot_correction_h1'].shape[0]
    hf['pilot_correction_h1'].resize((n + 1, hf['pilot_correction_h1'].shape[1]))
    hf['pilot_correction_h1'][n, :] = np.asarray(h1_vec, dtype=np.float32)
    if wide_vec is not None and 'pilot_correction_wide' in hf:
        hf['pilot_correction_wide'].resize((n + 1, hf['pilot_correction_wide'].shape[1]))
        hf['pilot_correction_wide'][n, :] = np.asarray(wide_vec, dtype=np.float32)
    hf.flush()
    return n


def append_pilot_shape(hf, t_mid, response):
    """One detected burst's per-bin response (issue #30)."""
    if 'pilot_shape' not in hf:
        return
    n = hf['pilot_shape'].shape[0]
    hf['pilot_shape'].resize((n + 1, hf['pilot_shape'].shape[1]))
    hf['pilot_shape'][n, :] = np.asarray(response, dtype=np.complex64)
    hf['pilot_shape_time'].resize((n + 1,))
    hf['pilot_shape_time'][n] = float(t_mid)
    hf.flush()


def set_pilot_reference(hf, response, anchored):
    """Record what the pilot level is measured against, once it is known."""
    if 'pilot_reference' not in hf:
        return
    hf['pilot_reference'][:] = np.asarray(response, dtype=np.complex64)
    hf['pilot_anchored'][0] = 1 if anchored else 0
    hf.flush()


def _continuum_channels(hf, freq_axis_hz):
    """The wide-product channels the continuum is measured over: inside the
    continuum band, outside the H I band, clear of the LO spur."""
    f = np.asarray(freq_axis_hz, float)
    keep = np.ones(f.shape, dtype=bool)
    try:
        lo, hi = [float(x) for x in hf.attrs['continuum_band_hz']]
        keep &= (f >= lo) & (f <= hi)
    except Exception:                                     # noqa: BLE001
        pass
    try:
        h_lo, h_hi = [float(x) for x in hf.attrs['h1_band_hz']]
        keep &= ~((f >= h_lo) & (f <= h_hi))
    except Exception:                                     # noqa: BLE001
        pass
    try:
        keep &= np.abs(f - float(hf.attrs['dc_artefact_freq_hz'])) > 60e3
    except Exception:                                     # noqa: BLE001
        pass
    return keep


def _append_live_summary(hf, avg_linear, timestamp, integration_time, count,
                         wide_linear=None, overflows=0, pilot=None):
    """One line per record in a plain text file beside the HDF5.

    So something can watch an observation while it runs. The HDF5 itself cannot
    be read while it is being written - no SWMR, so a second opener hits the
    file lock - and a live *spectrum* display is the receiver rewrite, issue
    #15. A live *flux* display is not the same problem: it needs one number per
    record, not the spectrum, and a few bytes of text cannot corrupt the
    recording no matter what goes wrong here.

    The number is the median across the band. Median rather than mean because
    it ignores the LO artefact and any narrow interference without having to
    find them, and for a broadband source like the Sun it is the continuum
    level.

    Taken over the *bandpass-corrected* spectrum, restricted to the channels
    the template covers - because that is the scale the gain was fitted on,
    and the scheduler's live endpoint applies that gain to this number. The
    first version took the raw median over the full band, on the reasoning
    that the template normalises to a median of one so the scales are "very
    nearly" the same. They are - to about 8%, the band edges rolling off -
    and 8% of a 353 K system temperature is 28 K. Watching the Sun at a
    thousand kelvin nobody noticed; the first blank-sky drift scan read a
    steady -24 K, which is how "very nearly" was measured. Without a template
    the raw median goes out as before, and the endpoint reports counts.

    Every failure here is swallowed. This is a convenience for whoever is
    watching; an observation must never be lost because a summary line could
    not be written.
    """
    try:
        values = avg_linear
        try:
            valid = hf['bandpass_valid'][:]
            if valid.any():
                values = (avg_linear / hf['bandpass_correction'][:])[valid]
        except Exception:                                 # noqa: BLE001
            pass
        path = os.path.splitext(hf.filename)[0] + ".live.jsonl"
        record = {"t": float(timestamp),
                  "tau": float(integration_time),
                  "n": int(count),
                  "median": float(np.median(values))}
        if wide_linear is not None and 'frequency_hz_wide' in hf:
            # The continuum: the wide product over the continuum band with
            # the H I band and the spur cut out, bandpass-corrected where
            # the wide template speaks, on the same gain scale as `median`.
            w = np.asarray(wide_linear, dtype=float)
            keep = _continuum_channels(hf, hf['frequency_hz_wide'][:])
            try:
                w_valid = hf['bandpass_valid_wide'][:]
                if w_valid.any():
                    w = w / hf['bandpass_correction_wide'][:]
                    keep &= w_valid
            except Exception:                             # noqa: BLE001
                pass
            if keep.any():
                record["continuum"] = float(np.median(w[keep]))
            record["overflows"] = int(overflows)
        if pilot is not None:
            # The pilot as applied this record, so the live view can skip the
            # bursts and say "corrected" or "not detected".
            record["pilot_burst"] = int(pilot.get("burst", 0))
            record["pilot_seen"] = int(pilot.get("seen", 0))
            record["pilot_ok"] = int(pilot.get("ok", 0))
            record["pilot_level"] = float(pilot.get("level", 1.0))
            record["pilot_slope"] = float(pilot.get("slope", 0.0))
            record["pilot_snr"] = float(pilot.get("snr", 0.0))
        line = json.dumps(record)
        with open(path, "a") as fh:
            fh.write(line + "\n")
    except Exception:                                     # noqa: BLE001
        pass


class HeadlessRecorder:
    """Acquire and record with no Qt at all.

    The observing path must not depend on a display or on PyQt being
    importable: the observatory is worked over ssh, and an unattended
    observation that needs a desktop session is one that fails at 03:00. This
    runs the same GNURadioFlowgraph the window runs - that class has never had
    any Qt in it - and replaces only what the window was providing around it:
    a 10 Hz tick to accumulate spectra, an integration-period tick to write
    one, and the HDF5 file. Both front ends go through init_hdf5 and
    append_spectrum, so the files are identical.

    Running the GUI offscreen (QT_QPA_PLATFORM=offscreen) also worked and was
    the stopgap, but it kept Qt as a hard dependency of every observation and
    spent the whole run rebuilding a waterfall image for nobody to look at.
    """

    TICK_S = 0.1                      # 10 Hz, matching the GUI's accumulation

    def __init__(self, sdr_type='b210', sample_rate=None, gain=None,
                 output_file=None, instrument=None):
        # The fixed instrument (issue #27): the scheduler passes it as
        # H1_INSTRUMENT, a hand-started headless receiver takes the defaults,
        # and either way the file records exactly what a scheduled one does.
        # --sample-rate and --gain belong to the GUI; here they are noted and
        # ignored, so a stale launcher cannot retune a scheduled observation.
        if instrument is None:
            try:
                instrument = fixed_instrument(json.loads(os.environ.get('H1_INSTRUMENT') or '{}'))
            except ValueError:
                instrument = fixed_instrument()
        if sample_rate or gain:
            print("  NOTE: --sample-rate/--gain are ignored in headless mode; the "
                  "fixed instrument decides (issue #27)", flush=True)
        self.instrument = instrument
        self.integration_time = INTEGRATION_TIME
        self.output_file = output_file or OUTPUT_FILE
        print("  " + describe_instrument(instrument))

        self.flowgraph = TwoProductFlowgraph(sdr_type, instrument)
        self.sdr_type = self.flowgraph.sdr_type
        self.sample_rate = self.flowgraph.sample_rate
        self.center_freq = self.flowgraph.center_freq
        self.gain = self.flowgraph.gain

        # The H I product keeps the legacy names and axis; only the channels
        # inside the sub-band are recorded.
        self.h1_keep = self.flowgraph.h1_keep()
        self.freq_axis_hz = self.flowgraph.h1_freq_axis()[self.h1_keep]
        self.fft_size = int(self.h1_keep.sum())
        self.wide_axis_hz = self.flowgraph.wide_freq_axis()
        self.wide_channels = int(self.flowgraph.wide_channels)

        self.hf = init_hdf5(self.output_file, self.freq_axis_hz, self.fft_size,
                            sdr_type=self.sdr_type,
                            center_freq=self.center_freq,
                            sample_rate=self.sample_rate, gain=self.gain,
                            wide={'freq_axis_hz': self.wide_axis_hz,
                                  'channels': self.wide_channels},
                            instrument=instrument, pilot=self.flowgraph.pilot,
                            pilot_burst_every=pilot_mod.burst_every(
                                self.flowgraph.pilot_cfg, self.integration_time))
        # The pilot's bookkeeping (issue #30): anchored to the calibration's
        # stored reference when it has one for this plan, else to this run's
        # first detected record.
        self.pilot_tracker = None
        # A burst costs one whole record, so how often to send one depends on
        # how long a record is: the configuration asks for an interval in
        # seconds and this is what it comes to here.
        self._burst_every = pilot_mod.burst_every(self.flowgraph.pilot_cfg,
                                                  self.integration_time)
        self._record_index = 0
        self._burst_record = False
        self._corr_version = None
        self._corr_index = -1
        if self.flowgraph.pilot is not None:
            ref = _pilot_reference_from_calibration(self.flowgraph.pilot, instrument)
            self.pilot_tracker = pilot_mod.PilotTracker(
                self.flowgraph.pilot_cfg, self.flowgraph.pilot, ref,
                expected_interval_s=self._burst_every * self.integration_time)
            if ref is not None:
                set_pilot_reference(self.hf, ref, anchored=True)
            print("  Pilot: comb burst every %d records (%.0f s), %s" % (
                self._burst_every, self._burst_every * self.integration_time,
                "anchored to the calibration" if ref is not None
                else "self-referenced until the calibration stores a pilot reference"),
                flush=True)
        self._pilot_reported = None
        self.spectrum_count = 0
        self._stop = threading.Event()
        self.overflows = _OverflowCounter()

    def request_stop(self, *_args):
        """Signal handler and API: finish the current tick and shut down."""
        self._stop.set()

    def _pilot_begin_record(self):
        """Decide whether the record just started is a burst, and switch the comb on if so."""
        self._burst_record = False
        if self.pilot_tracker is None or self._burst_every <= 0:
            return
        every = self._burst_every
        if every > 0 and self._record_index % every == every - 1:
            self._burst_record = True
            self.flowgraph.set_burst(True)

    def _pilot_tick(self, now, period_start):
        """Switch the comb off a margin before the burst record ends, so the
        transmit buffers have drained before the next record begins."""
        if self._burst_record and self.flowgraph._burst_on:
            margin = pilot_mod.burst_off_margin_s(self.flowgraph.pilot_cfg,
                                                  self.integration_time)
            if now >= period_start + self.integration_time - margin:
                self.flowgraph.set_burst(False)

    def _pilot_record(self, wide_mean, now, tau):
        """This record's pilot outcome, for append_spectrum and the file's side datasets."""
        if self.pilot_tracker is None:
            return None
        self.flowgraph.set_burst(False)
        was_burst = self._burst_record
        self._record_index += 1
        cfg = self.flowgraph.pilot_cfg
        try:
            tone = pilot_mod.tone_power(np.asarray(wide_mean, dtype=float),
                                        self.flowgraph.pilot, cfg)
            xspec, n_on, n_total, noise = self.flowgraph.take_pilot()
            if was_burst:
                b = self.pilot_tracker.burst(xspec, n_on, noise, now)
                if b['ok']:
                    if not self.pilot_tracker.anchored and not np.any(self.hf['pilot_reference'][:]):
                        set_pilot_reference(self.hf, self.pilot_tracker.reference_h, anchored=False)
                    append_pilot_shape(self.hf, now - tau / 2.0, b['h'])
                # A pilot that is never there costs a record every interval
                # for nothing - the state until the vertex dipole is wired -
                # so stop asking after a few, and say so once.
                if self.pilot_tracker.give_up() and self._burst_every:
                    self._burst_every = 0
                    print("  Pilot: not detected in %d bursts; no more will be sent this "
                          "run (is the transmitter connected to the vertex dipole?). The "
                          "carrier keeps running and its power is still recorded."
                          % self.pilot_tracker.bursts, flush=True)
                if b['ok'] != self._pilot_reported:
                    self._pilot_reported = b['ok']
                    print("  Pilot burst %s (median SNR %.1f, %d of %d frames on)%s" % (
                        "detected" if b['ok'] else "not detected", b['snr'], n_on, n_total,
                        "" if b['ok'] else " - nothing applied, as with no pilot"), flush=True)
                # The carrier runs through the burst too, so its power here is
                # the level the records after it are measured against.
                if tone['detected']:
                    self.pilot_tracker.set_tone_reference(tone['power'], now)
                return {'burst': 1, 'ok': 0, 'level': 1.0, 'slope': 0.0, 'snr': b['snr'],
                        'seen': b['ok'], 'corr_index': -1, 'tone_power': tone['power'],
                        'tone_ratio': tone['ratio'], 'tone_ok': 0, 'tone_level': 1.0}
            tone_level, tone_ok = self.pilot_tracker.tone_level(tone, now)
            base = {'tone_power': tone['power'], 'tone_ratio': tone['ratio'],
                    'tone_ok': tone_ok, 'tone_level': tone_level}
            if n_on > 0:
                # A science record that caught the tail of a burst: measured,
                # flagged, dropped downstream.
                return dict(base, burst=2, ok=0, level=1.0, slope=0.0, snr=0.0,
                            seen=0, corr_index=-1, tone_ok=0, tone_level=1.0)
            level, slope, ok = self.pilot_tracker.correction(now)
            if not ok:
                return dict(base, burst=0, ok=0, level=1.0, slope=0.0, snr=0.0,
                            seen=0, corr_index=-1)
            version, h_mean = self.pilot_tracker.shape(now)
            if h_mean is not None and version != self._corr_version:
                plan = self.flowgraph.pilot
                band = self.instrument['h1_band_hz']
                ref = self.pilot_tracker.reference_h
                h1_vec = pilot_mod.correction_vector(h_mean, plan, self.freq_axis_hz, band, ref)
                wide_vec = pilot_mod.correction_vector(h_mean, plan, self.wide_axis_hz, band, ref)
                self._corr_index = append_pilot_correction(self.hf, h1_vec, wide_vec)
                self._corr_version = version
            return dict(base, burst=0, ok=1, level=level, slope=slope, snr=0.0,
                        seen=0, corr_index=self._corr_index)
        except Exception as exc:                          # noqa: BLE001
            print(f"  Pilot: estimate failed ({exc}); record left uncorrected", flush=True)
            return {'burst': 1 if was_burst else 0, 'ok': 0, 'level': 1.0, 'slope': 0.0,
                    'snr': 0.0, 'seen': 0, 'corr_index': -1}

    def run(self):
        """Acquire until stopped, writing one record per integration time."""
        print(f"Recording to {self.output_file}", flush=True)
        print(f"  {self.sdr_type}, {self.sample_rate/1e6:.3f} Msps, LO "
              f"{self.center_freq/1e6:.6f} MHz, gain {self.gain}, "
              f"H I {self.fft_size} channels + continuum {self.wide_channels}, "
              f"tau {self.integration_time}s", flush=True)
        self.overflows.start()
        self.flowgraph.start()
        period_start = time.time()
        self._pilot_begin_record()
        try:
            while not self._stop.is_set():
                self._stop.wait(self.TICK_S)
                now = time.time()
                self._pilot_tick(now, period_start)
                if now - period_start < self.integration_time:
                    continue
                # The sinks have been summing every spectrum since the last
                # take; the record is their mean over the period.
                try:
                    h1, n_h1 = self.flowgraph.take_h1()
                    wide, n_wide = self.flowgraph.take_wide()
                except Exception as exc:
                    print(f"Error reading the flowgraph: {exc}", flush=True)
                    break
                if h1 is None or wide is None:
                    continue
                pil = self._pilot_record(wide, now, now - period_start)
                self.spectrum_count = append_spectrum(
                    self.hf, np.asarray(h1, dtype=float)[self.h1_keep], now,
                    now - period_start, self.fft_size,
                    wide_linear=np.asarray(wide, dtype=float),
                    overflows=self.overflows.take(), pilot=pil,
                    underflows=self.overflows.take_underflows())
                period_start = now
                self._pilot_begin_record()
        finally:
            self.flowgraph.stop()
            self.flowgraph.wait()
            self.hf.close()
            self.overflows.stop()
            print(f"Total spectra saved: {self.spectrum_count}", flush=True)
            print(f"Data written to: {self.output_file}", flush=True)


# ---------------------------------------------------------------------------
# Pulsar mode (H1_MODE=pulsar): a fast filterbank, not spectra
#
# A pulsar is only ever a *folded* detection on this dish (pulsar_fold.py):
# the band power every millisecond for an hour, added up at the period
# afterwards. So this mode records no spectra at all. The stream goes through
# a short FFT - NCHAN channels across the whole 8 MHz - and the channel
# powers are summed for DT_S and written as one row. Sixteen channels at a
# millisecond is 230 MB an hour; raw voltages would be 115 GB and buy
# nothing, since dispersion across our band is a tenth of a pulse width.
#
# Time comes from two places on purpose: the cadence from the B210's clock
# (a row is exactly presum x NCHAN samples), the start from the host clock
# at flowgraph start. A fold needs the first stable and the second to about
# a millisecond; pulsar timing would need a PPS, and this is not that.
# Overflows break the cadence - a dropped block shifts every later row - so
# UHD's marks are recorded against the row they landed near, and the fold
# reports them; at 8 Msps this graph is far lighter than the two-product one.


class _RowSink(gr.sync_block):
    """Collect rows of channel power, and the radio's own time marks.

    gr-uhd tags the first sample of the stream and the first sample after
    every overflow with `rx_time`, the device clock's reading for that
    sample - the one fact that lets a fold re-align across a dropped block
    instead of smearing over it. The tags propagate down the chain with
    their offsets scaled by the decimation, so here they arrive as a row
    index and a time; the recorder writes them as `time_marks`.
    """

    def __init__(self, vlen):
        gr.sync_block.__init__(self, name="pulsar_row_sink",
                               in_sig=[(np.float32, vlen)], out_sig=None)
        self.vlen = vlen
        self._rows = []
        self._marks = []
        self._lock = threading.Lock()

    def work(self, input_items, output_items):
        n = len(input_items[0])
        block = np.array(input_items[0], dtype=np.float32, copy=True)
        marks = []
        try:
            import pmt
            for tag in self.get_tags_in_window(0, 0, n):
                if pmt.symbol_to_string(tag.key) != "rx_time":
                    continue
                full = pmt.to_uint64(pmt.tuple_ref(tag.value, 0))
                frac = pmt.to_double(pmt.tuple_ref(tag.value, 1))
                marks.append((int(tag.offset), float(full) + frac))
        except Exception:                                 # noqa: BLE001
            pass
        with self._lock:
            self._rows.append(block)
            self._marks.extend(marks)
        return n

    def take(self):
        with self._lock:
            rows, self._rows = self._rows, []
        return np.concatenate(rows, axis=0) if rows else np.empty((0, self.vlen), dtype=np.float32)

    def take_marks(self):
        with self._lock:
            marks, self._marks = self._marks, []
        return marks


class PulsarFlowgraph(gr.top_block):
    """One stream, NCHAN channel powers every DT_S."""

    def __init__(self, sdr_type, instrument, nchan, dt_s, strict=True):
        gr.top_block.__init__(self, "pulsar filterbank", catch_exceptions=True)
        self.instrument = dict(instrument)
        self.sdr_type = sdr_type
        self.strict = strict
        self.sample_rate = float(instrument["sample_rate_hz"])
        self.center_freq = float(instrument["lo_hz"])
        self.gain = float(instrument["gain_db"])
        self.nchan = int(nchan)
        print(f"Initializing {self.sdr_type.upper()} (pulsar filterbank)...")
        try:
            self.sdr_source, self.throttle, actual_rate, actual_freq = \
                create_sdr_source(self.sdr_type, self.sample_rate, self.center_freq, self.gain)
            self.sample_rate = actual_rate
            self.center_freq = actual_freq
        except Exception as e:
            print(f"  Failed to initialize {self.sdr_type.upper()}: {e}", flush=True)
            if self.strict and self.sdr_type != 'demo':
                raise RuntimeError("could not open the %s (%s); refusing to record synthetic "
                                   "noise in its place" % (self.sdr_type.upper(), e))
            self.sdr_type = 'demo'
            self.sdr_source, self.throttle, actual_rate = create_demo_source(self.sample_rate)
            self.sample_rate = actual_rate
        # The device clock set to the host's now, so the rx_time tags read
        # as unix time: with the external reference fitted the B210's clock
        # *is* the reference, and every row's time follows from the last
        # mark and the row count. (A PPS into the B210 and set_time_next_pps
        # would make this absolute to a microsecond, for timing; not needed
        # for a fold.)
        if self.sdr_type == 'b210':
            try:
                from gnuradio import uhd
                self.sdr_source.set_time_now(uhd.time_spec(time.time()))
            except Exception as exc:                          # noqa: BLE001
                print(f"  NOTE: could not set the device time: {exc}", flush=True)
        n = self.nchan
        # presum vectors per row: the row length is exact in samples, so the
        # cadence is the radio's clock and dt_s is what it comes to.
        self.presum = max(1, int(round(self.sample_rate / n * dt_s)))
        self.dt_s = self.presum * n / self.sample_rate
        self.s2v = blocks.stream_to_vector(gr.sizeof_gr_complex, n)
        # Rectangular window: sixteen coarse channels want flat response
        # across each, and leakage between them does not matter for a fold.
        self.fftb = fft.fft_vcc(n, True, [1.0] * n, True, 1)
        self.mag = blocks.complex_to_mag_squared(n)
        self.norm = blocks.multiply_const_vff([1.0 / (n * self.presum)] * n)
        self.summer = blocks.integrate_ff(self.presum, n)
        self.sink = _RowSink(n)
        chain = [self.sdr_source] + ([self.throttle] if self.throttle is not None else []) \
                + [self.s2v, self.fftb, self.mag, self.norm, self.summer, self.sink]
        for a, b in zip(chain[:-1], chain[1:]):
            self.connect(a, b)
        print("  %d channels of %.3f MHz, %d transforms per row, rows of %.4f ms"
              % (n, self.sample_rate / n / 1e6, self.presum, 1e3 * self.dt_s), flush=True)

    def freq_axis(self):
        return self.center_freq + np.fft.fftshift(np.fft.fftfreq(self.nchan, 1.0 / self.sample_rate))


def _embed_obs_metadata(hf):
    """H1_OBS_METADATA (the scheduler's entry) as attributes, as init_hdf5 does."""
    obs_meta = os.environ.get('H1_OBS_METADATA', '')
    if not obs_meta:
        return
    try:
        meta = json.loads(obs_meta)
    except Exception:                                     # noqa: BLE001
        return
    for key, val in meta.items():
        if isinstance(val, bool):
            hf.attrs[key] = int(val)
        elif val is not None and val != '':
            hf.attrs[key] = val


def init_pulsar_hdf5(filename, freq_axis_hz, dt_s, sdr_type, center_freq, sample_rate,
                     gain, instrument, t0_unix):
    """A pulsar-mode file: `power` rows (N x nchan, float32) at `dt_s` from
    `t0_unix`, the channel axis, and `overflow_marks` (row index, count) for
    every batch of UHD overflows. Everything is created before SWMR is
    switched on, so the file can be read while it records."""
    import observatory as _inst      # the site, from the layer everything may depend on
    os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
    hf = h5py.File(filename, 'w', libver='latest')
    n = len(freq_axis_hz)
    hf.create_dataset('frequency_hz', data=np.asarray(freq_axis_hz, float))
    hf.create_dataset('power', shape=(0, n), maxshape=(None, n), dtype='f4', chunks=(1024, n))
    hf.create_dataset('overflow_marks', shape=(0, 2), maxshape=(None, 2), dtype='i8', chunks=(64, 2))
    # (row index, device time in unix seconds) at the start and after every
    # overflow, from gr-uhd's rx_time tags: the fold's time axis.
    hf.create_dataset('time_marks', shape=(0, 2), maxshape=(None, 2), dtype='f8', chunks=(64, 2))
    hf.attrs['mode'] = 'pulsar'
    hf.attrs['observation_mode'] = observation_files.PULSAR_MODE
    hf.attrs['dt_s'] = float(dt_s)
    hf.attrs['nchan'] = int(n)
    hf.attrs['t0_unix'] = float(t0_unix)
    hf.attrs['created_utc'] = datetime.now(timezone.utc).isoformat()
    hf.attrs['sdr_type'] = str(sdr_type)
    hf.attrs['center_freq_hz'] = float(center_freq)
    hf.attrs['sample_rate_hz'] = float(sample_rate)
    hf.attrs['gain_db'] = float(gain)
    hf.attrs['instrument'] = json.dumps(instrument)
    hf.attrs['site_lat_deg'] = float(_inst.SITE_LAT_DEG)
    hf.attrs['site_lon_deg'] = float(_inst.SITE_LON_DEG)
    hf.attrs['site_height_m'] = float(_inst.SITE_HEIGHT_M)
    _embed_obs_metadata(hf)
    hf.flush()
    hf.swmr_mode = True
    return hf


class PulsarRecorder:
    """Record the fast filterbank until stopped. No spectra, no calibration:
    the fold (pulsar_fold.py) works in fractional excess per channel."""

    TICK_S = 0.5
    FLUSH_S = 5.0

    def __init__(self, sdr_type='b210', output_file=None, instrument=None, nchan=None, dt_s=None):
        import pulsar_fold
        if instrument is None:
            try:
                instrument = fixed_instrument(json.loads(os.environ.get('H1_INSTRUMENT') or '{}'))
            except ValueError:
                instrument = fixed_instrument()
        self.instrument = instrument
        self.output_file = output_file or OUTPUT_FILE
        self.nchan = int(nchan or os.environ.get('H1_PULSAR_NCHAN') or pulsar_fold.NCHAN)
        self.dt_s = float(dt_s or os.environ.get('H1_PULSAR_DT_S') or pulsar_fold.DT_S)
        print("  " + describe_instrument(instrument))
        self.flowgraph = PulsarFlowgraph(sdr_type, instrument, self.nchan, self.dt_s)
        self.sdr_type = self.flowgraph.sdr_type
        self.hf = None
        self.rows = 0
        self._stop = threading.Event()
        self.overflows = _OverflowCounter()
        self.overflow_total = 0
        self.time_marks = 0

    def request_stop(self, *_args):
        self._stop.set()

    def _append(self, rows):
        if not len(rows):
            return
        ds = self.hf['power']
        ds.resize(self.rows + len(rows), axis=0)
        ds[self.rows:self.rows + len(rows), :] = rows
        self.rows += len(rows)

    def _mark_overflows(self, n):
        if not n:
            return
        ds = self.hf['overflow_marks']
        k = ds.shape[0]
        ds.resize(k + 1, axis=0)
        ds[k, :] = (self.rows, n)
        self.overflow_total += n

    def _write_time_marks(self, marks):
        if not marks:
            return
        ds = self.hf['time_marks']
        k = ds.shape[0]
        ds.resize(k + len(marks), axis=0)
        ds[k:k + len(marks), :] = np.asarray(marks, dtype='f8')
        self.time_marks += len(marks)

    def run(self):
        print(f"Recording pulsar filterbank to {self.output_file}", flush=True)
        self.overflows.start()
        self.flowgraph.start()
        t0 = time.time()
        self.hf = init_pulsar_hdf5(self.output_file, self.flowgraph.freq_axis(), self.flowgraph.dt_s,
                                   self.sdr_type, self.flowgraph.center_freq, self.flowgraph.sample_rate,
                                   self.flowgraph.gain, self.instrument, t0)
        print(f"  {self.sdr_type}, {self.flowgraph.sample_rate/1e6:.3f} Msps, LO "
              f"{self.flowgraph.center_freq/1e6:.6f} MHz, gain {self.flowgraph.gain}, "
              f"{self.nchan} channels every {1e3*self.flowgraph.dt_s:.3f} ms", flush=True)
        last_flush = last_report = t0
        try:
            while not self._stop.is_set():
                self._stop.wait(self.TICK_S)
                self._append(self.flowgraph.sink.take())
                self._write_time_marks(self.flowgraph.sink.take_marks())
                self._mark_overflows(self.overflows.take())
                now = time.time()
                if now - last_flush >= self.FLUSH_S:
                    self.hf.flush()
                    last_flush = now
                if now - last_report >= 60.0:
                    expected = (now - t0) / self.flowgraph.dt_s
                    print("  %d rows in %.0f s (%.2f%% of the clock's count), %d overflows, %d time marks"
                          % (self.rows, now - t0, 100.0 * self.rows / max(expected, 1.0),
                             self.overflow_total, self.time_marks), flush=True)
                    last_report = now
        finally:
            self.flowgraph.stop()
            self.flowgraph.wait()
            self._append(self.flowgraph.sink.take())
            self._write_time_marks(self.flowgraph.sink.take_marks())
            self._mark_overflows(self.overflows.take())
            self.hf.flush()
            self.hf.close()
            self.overflows.stop()
            print(f"Total rows saved: {self.rows} ({self.rows * self.flowgraph.dt_s:.0f} s), "
                  f"{self.overflow_total} overflows", flush=True)
            print(f"Data written to: {self.output_file}", flush=True)


class VelocityAxisItem(pg.AxisItem):
    """Secondary x-axis showing topocentric radio velocity in km/s,
    v = c (f0 - f) / f0, relative to the H I rest frequency.

    The linked view's coordinates are frequency in MHz, so ticks are
    chosen at round velocities and mapped back to their frequency
    positions; only the labels are in km/s. No LSR correction is
    applied (that would need the pointing direction, which the
    receiver doesn't know)."""

    def __init__(self, rest_freq_mhz):
        super().__init__(orientation='top')
        self.rest_freq_mhz = rest_freq_mhz
        self.setLabel('Radio velocity, topocentric (km/s)')

    def _freq_to_vel(self, f_mhz):
        return C_KMS * (self.rest_freq_mhz - f_mhz) / self.rest_freq_mhz

    def _vel_to_freq(self, v_kms):
        return self.rest_freq_mhz * (1.0 - v_kms / C_KMS)

    def tickValues(self, minVal, maxVal, size):
        # View range arrives in MHz; velocity runs opposite to frequency
        vmin = self._freq_to_vel(maxVal)
        vmax = self._freq_to_vel(minVal)
        return [
            (spacing, [self._vel_to_freq(v) for v in ticks])
            for spacing, ticks in super().tickValues(vmin, vmax, size)
        ]

    def tickStrings(self, values, scale, spacing):
        # values are frequency coordinates; spacing is the km/s step
        # chosen in tickValues, which sets the decimals needed
        places = max(0, int(np.ceil(-np.log10(spacing)))) if spacing < 1 else 0
        return [f"{self._freq_to_vel(v):.{places}f}" for v in values]


class H1ReceiverWindow(QtWidgets.QMainWindow):
    """Main window with PyQtGraph displays for integrated spectrum."""

    def __init__(self, sdr_type='b210', sample_rate=None, gain=None, show_controls=False):
        super().__init__()

        self.sdr_type = sdr_type
        self.integration_time = INTEGRATION_TIME
        self.show_controls = show_controls

        # Get defaults
        defaults = SDR_DEFAULTS.get(sdr_type, SDR_DEFAULTS['demo'])
        requested_rate = sample_rate if sample_rate else defaults['sample_rate']
        self.gain = gain if gain else defaults['gain']

        # Same tuning plan as the headless recorder, so the window shows what
        # an observation would actually record.
        self.tuning = plan_tuning(CENTER_FREQ, requested_rate, FFT_SIZE,
                                  LO_OFFSET_HZ)
        self.sky_center_freq = self.tuning['sky_center_freq_hz']
        self.center_freq = self.tuning['tuned_center_freq_hz']
        self.sample_rate = self.tuning['sample_rate_hz']
        self.fft_size = self.tuning['channels']
        print("  " + describe_tuning(self.tuning))

        # Display settings
        self.waterfall_min = -70
        self.waterfall_max = -30
        self.spectrum_log_scale = True  # dB by default; toggled in the GUI
        self.line_width = 1             # curve width; cycles 1x / 2x / 4x
        self.recording = True           # gates HDF5 writes only

        # Create flowgraph
        self.flowgraph = GNURadioFlowgraph(
            self.sdr_type, self.sample_rate, self.center_freq, self.gain, self.fft_size
        )
        # Update from actual values
        self.sdr_type = self.flowgraph.sdr_type
        self.sample_rate = self.flowgraph.sample_rate

        # Frequency axis
        self.freq_axis_hz = self._get_frequency_axis()
        self.freq_axis_mhz = self.freq_axis_hz / 1e6

        # Waterfall history
        self.waterfall_data = deque(maxlen=WATERFALL_HISTORY)

        # Total-power strip chart: raw per-tick samples for the sliding
        # integration window (6000 ticks at 10 Hz = 600 s, matching the
        # maximum integration time) used by the calibration button, plus
        # one plotted point per completed integration period. The history
        # is pruned by timestamp so its span survives integration-time
        # changes; the plotted span is adjustable from the GUI.
        self.power_raw = deque(maxlen=6000)
        self.power_window_min = 10  # plotted time-series span, minutes
        self.power_history = deque()
        self.power_block_sum = 0.0
        self.power_block_count = 0
        self.power_block_start = None
        # Kelvin per arb. unit for the total-power chart; None = uncalibrated
        self.kelvin_per_unit = None

        # Python-side accumulator for long integration times
        # GNU Radio does short averaging for display; Python accumulates for saves
        self.accumulator = None  # Will hold sum of linear power spectra
        self.accumulator_count = 0
        self.accumulator_start_time = time.time()

        # HDF5 setup
        self.output_file = OUTPUT_FILE
        self.hdf5_segment = 0
        self.hf = self._init_hdf5(self.output_file)
        self.spectrum_count = 0
        self.last_save_time = time.time()

        # Build GUI
        self._build_gui()

        # Display update timer (faster for smooth display)
        self.display_timer = QtCore.QTimer()
        self.display_timer.timeout.connect(self._update_display)
        self.display_timer.start(100)  # 10 Hz display update

        # HDF5 save timer
        self.save_timer = QtCore.QTimer()
        self.save_timer.timeout.connect(self._save_spectrum)
        self.save_timer.start(int(self.integration_time * 1000))

    def _get_frequency_axis(self):
        """Generate frequency axis for the spectrum."""
        freq_offset = np.fft.fftshift(np.fft.fftfreq(self.fft_size, 1.0 / self.sample_rate))
        return self.center_freq + freq_offset

    def _build_gui(self):
        """Build the PyQtGraph GUI."""
        # Window setup
        title = f"Hydrogen Line (21cm) Receiver - {self.sdr_type.upper()}"
        if self.sdr_type == 'demo':
            title += " (Simulated Data)"
        self.setWindowTitle(title)
        self.setMinimumSize(1000, 700)

        # Central widget
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_layout = QtWidgets.QHBoxLayout(central)

        # Left side: plots
        plot_widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(plot_widget)
        layout.setContentsMargins(0, 0, 0, 0)

        # Status bar
        gain_str = "N/A" if self.sdr_type == 'demo' else f"{self.gain} dB"
        sdr_str = f"{self.sdr_type.upper()}" + (" (simulated)" if self.sdr_type == 'demo' else "")
        self.status_label = QtWidgets.QLabel(
            f"SDR: {sdr_str} | "
            f"Center: {self.center_freq/1e6:.3f} MHz | "
            f"Span: {self.sample_rate/1e6:.2f} MHz | "
            f"Gain: {gain_str} | "
            f"Integration: {self.integration_time}s"
        )
        self.status_label.setStyleSheet("font-family: monospace; font-size: 12px;")
        layout.addWidget(self.status_label)

        # PyQtGraph setup
        pg.setConfigOptions(antialias=True)

        # Vertical splitter so the three plots' heights can be dragged
        self.plot_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.plot_splitter.setHandleWidth(6)

        # Spectrum plot
        self.spectrum_widget = pg.PlotWidget(title="Integrated Spectrum")
        self.spectrum_widget.setLabel('left', 'Power', units='dB')
        # Coordinates are MHz; display the axis in GHz (a fixed scale,
        # otherwise pyqtgraph's auto SI prefix turns "MHz" into "kMHz")
        spec_freq_axis = self.spectrum_widget.getAxis('bottom')
        spec_freq_axis.setLabel('Frequency', units='GHz')
        spec_freq_axis.enableAutoSIPrefix(False)
        spec_freq_axis.setScale(1e-3)
        self.spectrum_widget.setAxisItems(
            {'top': VelocityAxisItem(H1_REST_FREQ_MHZ)})
        self.spectrum_widget.showGrid(x=True, y=True)
        self.spectrum_widget.enableAutoRange()

        # H1 line marker
        h1_line = pg.InfiniteLine(pos=H1_REST_FREQ_MHZ, angle=90, pen=pg.mkPen('r', style=QtCore.Qt.DashLine))
        self.spectrum_widget.addItem(h1_line)

        self.spectrum_curve = self.spectrum_widget.plot(
            self.freq_axis_mhz,
            np.zeros(self.fft_size),
            pen=pg.mkPen('c', width=1)
        )
        self.plot_splitter.addWidget(self.spectrum_widget)

        # Waterfall plot (one row per saved integration)
        self.waterfall_widget = pg.PlotWidget(title="Waterfall (Saved Integrations)")
        self.waterfall_widget.setLabel('left', 'Integration', units='#')
        wf_freq_axis = self.waterfall_widget.getAxis('bottom')
        wf_freq_axis.setLabel('Frequency', units='GHz')
        wf_freq_axis.enableAutoSIPrefix(False)
        wf_freq_axis.setScale(1e-3)

        # Image item for waterfall
        self.waterfall_img = pg.ImageItem()
        self.waterfall_widget.addItem(self.waterfall_img)

        # Set up color map
        colormap = pg.colormap.get('viridis')
        self.waterfall_img.setColorMap(colormap)

        # Scale waterfall to frequency axis
        freq_min = self.freq_axis_mhz[0]
        freq_max = self.freq_axis_mhz[-1]
        freq_scale = (freq_max - freq_min) / self.fft_size
        self.waterfall_img.setTransform(
            QtWidgets.QGraphicsScene().views()[0].transform() if False else
            pg.QtGui.QTransform().scale(freq_scale, 1).translate(freq_min / freq_scale, 0)
        )
        self.waterfall_widget.setXRange(freq_min, freq_max)

        self.plot_splitter.addWidget(self.waterfall_widget)

        # Total power vs time strip chart (linear power, sliding integration)
        self.power_plot_widget = pg.PlotWidget(
            title="Total Power vs Time",
            axisItems={'bottom': pg.DateAxisItem(orientation='bottom')}
        )
        self.power_plot_widget.setLabel('left', 'Total Power (linear, arb.)')
        self.power_plot_widget.setLabel('bottom', 'Local Time')
        self.power_plot_widget.showGrid(x=True, y=True)
        self.power_plot_widget.enableAutoRange()
        self.power_curve = self.power_plot_widget.plot(
            pen=pg.mkPen('y', width=1)
        )
        # Long windows can hold hundreds of thousands of points; let
        # pyqtgraph downsample to the visible pixel width when drawing
        self.power_curve.setDownsampling(auto=True)
        self.plot_splitter.addWidget(self.power_plot_widget)

        # Extra space on window resize is shared 2:1:1; drag the handles
        # to change the proportions, remembered across restarts
        self.plot_splitter.setStretchFactor(0, 2)
        self.plot_splitter.setStretchFactor(1, 1)
        self.plot_splitter.setStretchFactor(2, 1)
        settings = QtCore.QSettings("SRT", "h1_receiver")
        state = settings.value("plot_splitter_state")
        if state is not None:
            self.plot_splitter.restoreState(state)
        else:
            self.plot_splitter.setSizes([350, 175, 175])
        layout.addWidget(self.plot_splitter, stretch=1)

        # Count label
        count_layout = QtWidgets.QHBoxLayout()
        count_layout.addStretch()
        self.count_label = QtWidgets.QLabel("Spectra saved: 0")
        self.count_label.setStyleSheet("font-family: monospace; font-size: 12px;")
        count_layout.addWidget(self.count_label)
        layout.addLayout(count_layout)

        main_layout.addWidget(plot_widget, stretch=1)

        # Right side: control panel (only when run as main)
        if self.show_controls:
            self._build_control_panel(main_layout)

    def _build_control_panel(self, parent_layout):
        """Build the control panel for adjusting parameters."""
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(280)
        panel_layout = QtWidgets.QVBoxLayout(panel)
        panel_layout.setContentsMargins(10, 5, 5, 5)

        # SDR Info (read-only)
        info_group = QtWidgets.QGroupBox("SDR Info")
        info_layout = QtWidgets.QFormLayout(info_group)
        info_layout.addRow("Type:", QtWidgets.QLabel(self.sdr_type.upper()))
        self.resolution_label = QtWidgets.QLabel(f"{self.sample_rate/self.fft_size/1e3:.3f} kHz")
        info_layout.addRow("Resolution:", self.resolution_label)
        panel_layout.addWidget(info_group)

        # RF Settings
        rf_group = QtWidgets.QGroupBox("RF Settings")
        rf_layout = QtWidgets.QFormLayout(rf_group)

        self.freq_spin = QtWidgets.QDoubleSpinBox()
        self.freq_spin.setRange(1400, 1440)
        self.freq_spin.setDecimals(3)
        self.freq_spin.setSuffix(" MHz")
        self.freq_spin.setValue(self.center_freq / 1e6)
        self.freq_spin.valueChanged.connect(self._on_freq_changed)
        rf_layout.addRow("Center Freq:", self.freq_spin)

        self.gain_spin = QtWidgets.QSpinBox()
        self.gain_spin.setRange(0, 76)
        self.gain_spin.setSuffix(" dB")
        self.gain_spin.setValue(int(self.gain))
        self.gain_spin.setEnabled(self.sdr_type != 'demo')
        self.gain_spin.valueChanged.connect(self._on_gain_changed)
        rf_layout.addRow("Gain:", self.gain_spin)

        self.bw_combo = QtWidgets.QComboBox()
        bw_options = [0.5, 1.0, 1.5, 2.0, 2.4, 3.0, 4.0, 5.0]
        for bw in bw_options:
            self.bw_combo.addItem(f"{bw} MHz", bw * 1e6)
        # Select current bandwidth
        current_bw_mhz = self.sample_rate / 1e6
        idx = self.bw_combo.findText(f"{current_bw_mhz} MHz")
        if idx >= 0:
            self.bw_combo.setCurrentIndex(idx)
        self.bw_combo.currentIndexChanged.connect(self._on_bandwidth_changed)
        rf_layout.addRow("Bandwidth:", self.bw_combo)

        self.fft_combo = QtWidgets.QComboBox()
        fft_options = [128, 256, 512, 1024, 2048, 4096, 8192, 16384]
        for fft_size in fft_options:
            self.fft_combo.addItem(str(fft_size), fft_size)
        idx = self.fft_combo.findData(self.fft_size)
        if idx >= 0:
            self.fft_combo.setCurrentIndex(idx)
        self.fft_combo.currentIndexChanged.connect(self._on_fft_size_changed)
        rf_layout.addRow("FFT Size:", self.fft_combo)

        panel_layout.addWidget(rf_group)

        # Integration Settings
        int_group = QtWidgets.QGroupBox("Integration (Save)")
        int_layout = QtWidgets.QFormLayout(int_group)

        # No buffer limit - Python-side accumulation allows arbitrary duration
        self.int_spin = QtWidgets.QDoubleSpinBox()
        self.int_spin.setRange(1.0, 600.0)  # 1 second to 10 minutes
        self.int_spin.setDecimals(1)
        self.int_spin.setSuffix(" s")
        self.int_spin.setValue(self.integration_time)
        self.int_spin.valueChanged.connect(self._on_integration_changed)
        int_layout.addRow("Integration:", self.int_spin)

        # Show accumulator status
        self.accum_label = QtWidgets.QLabel("0 samples")
        int_layout.addRow("Accumulated:", self.accum_label)

        # Recording control
        self.record_btn = QtWidgets.QPushButton("Stop Recording")
        self.record_btn.setCheckable(True)
        self.record_btn.setChecked(True)  # Start recording by default
        self.record_btn.setStyleSheet("""
            QPushButton { background-color: #aa0000; color: white; font-weight: bold; }
            QPushButton:checked { background-color: #00aa00; }
        """)
        self.record_btn.clicked.connect(self._on_record_toggle)
        int_layout.addRow(self.record_btn)

        panel_layout.addWidget(int_group)

        # Spectrum Display Settings
        spec_group = QtWidgets.QGroupBox("Spectrum Display")
        spec_layout = QtWidgets.QFormLayout(spec_group)

        self.autoscale_btn = QtWidgets.QPushButton("Auto Scale")
        self.autoscale_btn.clicked.connect(self._on_autoscale_spectrum)
        spec_layout.addRow(self.autoscale_btn)

        self.scale_btn = QtWidgets.QPushButton("Scale: dB")
        self.scale_btn.setCheckable(True)  # unchecked = dB, checked = linear
        self.scale_btn.clicked.connect(self._on_scale_toggle)
        spec_layout.addRow(self.scale_btn)

        panel_layout.addWidget(spec_group)

        # Total Power Display
        power_group = QtWidgets.QGroupBox("Total Power")
        power_layout = QtWidgets.QFormLayout(power_group)

        # Bar range in dB; restored from the last calibration if one has
        # been saved (see _on_calibrate / _on_power_range_changed)
        settings = QtCore.QSettings("SRT", "h1_receiver")
        self.power_min = int(settings.value("power_bar_min", -80))
        self.power_max = int(settings.value("power_bar_max", -20))
        if self.power_min >= self.power_max:
            self.power_min, self.power_max = -80, -20

        self.power_bar = QtWidgets.QProgressBar()
        self.power_bar.setRange(self.power_min, self.power_max)
        self.power_bar.setValue(-50)
        self.power_bar.setTextVisible(False)
        self.power_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid grey;
                border-radius: 2px;
                background-color: #1a1a1a;
            }
            QProgressBar::chunk {
                background-color: #00aa00;
            }
        """)
        power_layout.addRow(self.power_bar)

        self.power_label = QtWidgets.QLabel("--.-- dB")
        self.power_label.setStyleSheet("font-family: monospace; font-size: 14px; font-weight: bold;")
        self.power_label.setAlignment(QtCore.Qt.AlignCenter)
        power_layout.addRow(self.power_label)

        self.power_min_spin = QtWidgets.QSpinBox()
        self.power_min_spin.setRange(-120, 50)
        self.power_min_spin.setSuffix(" dB")
        self.power_min_spin.setValue(self.power_min)
        self.power_min_spin.valueChanged.connect(self._on_power_range_changed)
        power_layout.addRow("Min:", self.power_min_spin)

        self.power_max_spin = QtWidgets.QSpinBox()
        self.power_max_spin.setRange(-120, 50)
        self.power_max_spin.setSuffix(" dB")
        self.power_max_spin.setValue(self.power_max)
        self.power_max_spin.valueChanged.connect(self._on_power_range_changed)
        power_layout.addRow("Max:", self.power_max_spin)

        self.width_btn = QtWidgets.QPushButton("Line Width: 1x")
        self.width_btn.clicked.connect(self._on_width_toggle)
        power_layout.addRow(self.width_btn)

        self.power_window_spin = QtWidgets.QSpinBox()
        self.power_window_spin.setRange(1, 480)
        self.power_window_spin.setSuffix(" min")
        self.power_window_spin.setValue(self.power_window_min)
        self.power_window_spin.valueChanged.connect(
            self._on_power_window_changed)
        power_layout.addRow("Window:", self.power_window_spin)

        panel_layout.addWidget(power_group)

        # Temperature calibration for the total-power chart
        cal_group = QtWidgets.QGroupBox("Temperature Calibration")
        cal_layout = QtWidgets.QFormLayout(cal_group)

        self.cal_temp_spin = QtWidgets.QDoubleSpinBox()
        self.cal_temp_spin.setRange(1.0, 100000.0)
        self.cal_temp_spin.setDecimals(1)
        self.cal_temp_spin.setSuffix(" K")
        self.cal_temp_spin.setValue(DEFAULT_CAL_TEMP_K)
        cal_layout.addRow("Blank-sky T:", self.cal_temp_spin)

        self.cal_btn = QtWidgets.QPushButton("Set current level = T")
        self.cal_btn.clicked.connect(self._on_calibrate)
        cal_layout.addRow(self.cal_btn)

        self.cal_clear_btn = QtWidgets.QPushButton("Clear calibration")
        self.cal_clear_btn.clicked.connect(self._on_clear_calibration)
        cal_layout.addRow(self.cal_clear_btn)

        self.cal_status_label = QtWidgets.QLabel("Uncalibrated (arb. units)")
        self.cal_status_label.setStyleSheet(
            "font-family: monospace; font-size: 11px;")
        cal_layout.addRow(self.cal_status_label)

        panel_layout.addWidget(cal_group)

        # Waterfall Settings
        wf_group = QtWidgets.QGroupBox("Waterfall Display")
        wf_layout = QtWidgets.QFormLayout(wf_group)

        self.wf_min_spin = QtWidgets.QSpinBox()
        self.wf_min_spin.setRange(-120, 0)
        self.wf_min_spin.setSuffix(" dB")
        self.wf_min_spin.setValue(self.waterfall_min)
        self.wf_min_spin.valueChanged.connect(self._on_waterfall_range_changed)
        wf_layout.addRow("Color Min:", self.wf_min_spin)

        self.wf_max_spin = QtWidgets.QSpinBox()
        self.wf_max_spin.setRange(-120, 0)
        self.wf_max_spin.setSuffix(" dB")
        self.wf_max_spin.setValue(self.waterfall_max)
        self.wf_max_spin.valueChanged.connect(self._on_waterfall_range_changed)
        wf_layout.addRow("Color Max:", self.wf_max_spin)

        self.autoscale_wf_btn = QtWidgets.QPushButton("Auto Scale")
        self.autoscale_wf_btn.clicked.connect(self._on_autoscale_waterfall)
        wf_layout.addRow(self.autoscale_wf_btn)

        self.clear_wf_btn = QtWidgets.QPushButton("Clear")
        self.clear_wf_btn.clicked.connect(self._on_clear_waterfall)
        wf_layout.addRow(self.clear_wf_btn)

        panel_layout.addWidget(wf_group)

        # Spacer
        panel_layout.addStretch()

        parent_layout.addWidget(panel)

    def _on_freq_changed(self, value):
        """Handle center frequency change."""
        new_freq = value * 1e6
        if self.sdr_type == 'b210':
            try:
                self.flowgraph.sdr_source.set_center_freq(new_freq, 0)
                self.center_freq = self.flowgraph.sdr_source.get_center_freq(0)
                self._update_freq_axis()
                self._roll_hdf5_file("freq")
                self._update_status()
            except Exception as e:
                print(f"Error setting frequency: {e}")
        elif self.sdr_type == 'rtlsdr':
            try:
                self.flowgraph.sdr_source.set_center_freq(new_freq, 0)
                self.center_freq = self.flowgraph.sdr_source.get_center_freq(0)
                self._update_freq_axis()
                self._roll_hdf5_file("freq")
                self._update_status()
            except Exception as e:
                print(f"Error setting frequency: {e}")

    def _on_gain_changed(self, value):
        """Handle gain change."""
        if self.sdr_type == 'b210':
            try:
                self.flowgraph.sdr_source.set_gain(value, 0)
                self.gain = self.flowgraph.sdr_source.get_gain(0)
                self._update_status()
            except Exception as e:
                print(f"Error setting gain: {e}")
        elif self.sdr_type == 'rtlsdr':
            try:
                self.flowgraph.sdr_source.set_gain(value, 0)
                self.gain = self.flowgraph.sdr_source.get_gain(0)
                self._update_status()
            except Exception as e:
                print(f"Error setting gain: {e}")

    def _on_bandwidth_changed(self, index):
        """Handle bandwidth/sample rate change - requires flowgraph rebuild for demo mode."""
        new_rate = self.bw_combo.currentData()
        if new_rate == self.sample_rate:
            return

        try:
            if self.sdr_type == 'b210':
                self.flowgraph.sdr_source.set_samp_rate(new_rate)
                self.sample_rate = self.flowgraph.sdr_source.get_samp_rate()
                # Re-track the AD9361 analog filter — it does not follow
                # runtime rate changes on its own
                self.flowgraph.sdr_source.set_bandwidth(self.sample_rate, 0)
                self.flowgraph.sample_rate = self.sample_rate
                self._update_freq_axis()
                self._reset_accumulator()
                self._invalidate_calibration("bandwidth")
                self._roll_hdf5_file("rate")
                self._update_status()
                print(f"Bandwidth changed to {self.sample_rate/1e6:.2f} MHz")
            elif self.sdr_type == 'rtlsdr':
                self.flowgraph.sdr_source.set_sample_rate(new_rate)
                self.sample_rate = self.flowgraph.sdr_source.get_sample_rate()
                # Re-track the tuner IF filter after the rate change
                self.flowgraph.sdr_source.set_bandwidth(self.sample_rate, 0)
                self.flowgraph.sample_rate = self.sample_rate
                self._update_freq_axis()
                self._reset_accumulator()
                self._invalidate_calibration("bandwidth")
                self._roll_hdf5_file("rate")
                self._update_status()
                print(f"Bandwidth changed to {self.sample_rate/1e6:.2f} MHz")
            elif self.sdr_type == 'demo':
                # Demo mode requires full flowgraph rebuild
                self._rebuild_flowgraph(new_rate, self.fft_size)
        except Exception as e:
            print(f"Error setting bandwidth: {e}")

    def _on_fft_size_changed(self, index):
        """Handle FFT size change - requires flowgraph rebuild."""
        new_fft_size = self.fft_combo.currentData()
        if new_fft_size == self.fft_size:
            return
        self._rebuild_flowgraph(self.sample_rate, new_fft_size)

    def _rebuild_flowgraph(self, new_sample_rate, new_fft_size):
        """Rebuild the entire flowgraph with new parameters."""
        # Prevent re-entry during rebuild
        if hasattr(self, '_rebuilding') and self._rebuilding:
            return
        self._rebuilding = True

        # Store old values for rollback
        old_sample_rate = self.sample_rate
        old_fft_size = self.fft_size

        try:
            self.flowgraph.stop()
            self.flowgraph.wait()

            # Disconnect all existing connections
            self.flowgraph.disconnect_all()

            # Update values
            self.sample_rate = new_sample_rate
            self.fft_size = new_fft_size
            self.flowgraph.sample_rate = new_sample_rate
            self.flowgraph.fft_size = new_fft_size

            # Recreate all blocks
            if self.sdr_type == 'demo':
                self.flowgraph.throttle = blocks.throttle(gr.sizeof_gr_complex, new_sample_rate, True)

            self.flowgraph.stream_to_vector = blocks.stream_to_vector(
                gr.sizeof_gr_complex, self.fft_size
            )
            self.flowgraph.fft_block = fft.fft_vcc(
                self.fft_size, True,
                window.blackmanharris(self.fft_size), True, 1
            )
            self.flowgraph.complex_to_mag_sq = blocks.complex_to_mag_squared(self.fft_size)

            # Short moving average for display smoothing only (0.5s)
            spectra_per_sec = self.sample_rate / self.fft_size
            display_avg_time = 0.5
            avg_length = max(1, int(spectra_per_sec * display_avg_time))
            self.flowgraph.moving_avg = blocks.moving_average_ff(
                avg_length, 1.0 / avg_length, 4000, self.fft_size
            )
            self.flowgraph.nlog10 = blocks.nlog10_ff(
                10, self.fft_size, -10 * np.log10(self.fft_size)
            )
            self.flowgraph.probe = blocks.probe_signal_vf(self.fft_size)

            # Reconnect everything
            if self.flowgraph.throttle is not None:
                self.flowgraph.connect((self.flowgraph.sdr_source, 0), (self.flowgraph.throttle, 0))
                signal_source = self.flowgraph.throttle
            else:
                signal_source = self.flowgraph.sdr_source

            self.flowgraph.connect((signal_source, 0), (self.flowgraph.stream_to_vector, 0))
            self.flowgraph.connect((self.flowgraph.stream_to_vector, 0), (self.flowgraph.fft_block, 0))
            self.flowgraph.connect((self.flowgraph.fft_block, 0), (self.flowgraph.complex_to_mag_sq, 0))
            self.flowgraph.connect((self.flowgraph.complex_to_mag_sq, 0), (self.flowgraph.moving_avg, 0))
            self.flowgraph.connect((self.flowgraph.moving_avg, 0), (self.flowgraph.nlog10, 0))
            self.flowgraph.connect((self.flowgraph.nlog10, 0), (self.flowgraph.probe, 0))

            # Update frequency axis (incl. waterfall x-range) and display
            self._update_freq_axis()
            self.spectrum_curve.setData(self.freq_axis_mhz,
                                        np.zeros(self.fft_size))
            self.waterfall_data.clear()

            # Reset Python-side accumulator
            self._reset_accumulator()
            self._invalidate_calibration("bandwidth/FFT size")
            self._roll_hdf5_file("fft")

            self.flowgraph.start()
            print(f"Flowgraph rebuilt: {self.sample_rate/1e6:.2f} MHz, FFT={self.fft_size}, resolution={self.sample_rate/self.fft_size/1e3:.3f} kHz")
            self._update_status()

        except Exception as e:
            print(f"Error rebuilding flowgraph: {e}")
            # Try to restore old values
            self.sample_rate = old_sample_rate
            self.fft_size = old_fft_size
        finally:
            self._rebuilding = False

    def _on_integration_changed(self, value):
        """Handle integration time change - restarts save timer."""
        if value == self.integration_time:
            return

        self.integration_time = value

        # Restart the save timer with new interval
        self.save_timer.stop()
        self.save_timer.setInterval(int(value * 1000))
        self.save_timer.start()

        # Reset accumulator to start fresh with new integration period
        self._reset_accumulator()

        print(f"Integration time changed to {value:.1f}s")
        self._update_status()

    def _on_power_range_changed(self):
        """Handle power bar range change; persist it for the next run."""
        self.power_min = self.power_min_spin.value()
        self.power_max = self.power_max_spin.value()
        self.power_bar.setRange(self.power_min, self.power_max)
        settings = QtCore.QSettings("SRT", "h1_receiver")
        settings.setValue("power_bar_min", self.power_min)
        settings.setValue("power_bar_max", self.power_max)

    def _on_waterfall_range_changed(self):
        """Handle waterfall color range change."""
        self.waterfall_min = self.wf_min_spin.value()
        self.waterfall_max = self.wf_max_spin.value()

    def _on_clear_waterfall(self):
        """Clear the waterfall history."""
        self.waterfall_data.clear()
        self.waterfall_img.clear()

    def _on_autoscale_waterfall(self):
        """Auto-scale waterfall color range to fit current data."""
        if len(self.waterfall_data) == 0:
            return
        waterfall_array = np.array(self.waterfall_data)
        data_min = int(np.floor(np.min(waterfall_array)))
        data_max = int(np.ceil(np.max(waterfall_array)))
        # Update spinboxes (which triggers _on_waterfall_range_changed)
        self.wf_min_spin.setValue(data_min)
        self.wf_max_spin.setValue(data_max)

    def _on_record_toggle(self):
        """Toggle HDF5 recording; averaging and display are unaffected."""
        self.recording = self.record_btn.isChecked()
        if self.recording:
            self.record_btn.setText("Stop Recording")
            print("Recording started")
        else:
            self.record_btn.setText("Start Recording")
            print("Recording stopped")

    def _on_autoscale_spectrum(self):
        """Auto-scale the spectrum plot to fit current data."""
        self.spectrum_widget.enableAutoRange()
        self.spectrum_widget.autoRange()

    def _on_scale_toggle(self):
        """Switch the spectrum plot between dB and linear power."""
        self.spectrum_log_scale = not self.scale_btn.isChecked()
        self.scale_btn.setText(
            "Scale: dB" if self.spectrum_log_scale else "Scale: Linear")
        self._update_spectrum_label()
        self.spectrum_widget.enableAutoRange()
        self.spectrum_widget.autoRange()

    def _on_width_toggle(self):
        """Cycle the total-power trace width through 1x, 2x and 4x."""
        self.line_width = {1: 2, 2: 4, 4: 1}[self.line_width]
        self.width_btn.setText(f"Line Width: {self.line_width}x")
        self.power_curve.setPen(pg.mkPen('y', width=self.line_width))

    def _on_power_window_changed(self, value):
        """Resize the plotted total-power history to the requested minutes."""
        self.power_window_min = value
        self._prune_power_history()
        self._redraw_power_curve()
        self.power_plot_widget.enableAutoRange()

    def _prune_power_history(self, now=None):
        """Drop plotted total-power points older than the display window."""
        if now is None:
            now = time.time()
        cutoff = now - self.power_window_min * 60
        while self.power_history and self.power_history[0][0] < cutoff:
            self.power_history.popleft()

    def _redraw_power_curve(self):
        """Push the total-power history to the strip chart, applying the
        temperature calibration if one is active."""
        scale = (self.kelvin_per_unit
                 if self.kelvin_per_unit is not None else 1.0)
        self.power_curve.setData(
            [pt[0] for pt in self.power_history],
            [pt[1] * scale for pt in self.power_history]
        )

    def _update_spectrum_label(self):
        """Set the spectrum y-axis label for the current scale mode."""
        if self.spectrum_log_scale:
            self.spectrum_widget.setLabel('left', 'Power', units='dB')
        elif self.kelvin_per_unit is not None:
            self.spectrum_widget.setLabel('left', 'Temperature', units='K')
        else:
            self.spectrum_widget.setLabel('left', 'Power (linear, arb.)')

    def _on_calibrate(self):
        """Scale the total-power chart so the current level reads as the
        entered blank-sky temperature."""
        if not self.power_raw:
            print("Cannot calibrate: no power samples yet")
            return
        current = sum(p for _, p in self.power_raw) / len(self.power_raw)
        if current <= 0:
            print("Cannot calibrate: current power is not positive")
            return
        temp_k = self.cal_temp_spin.value()
        self.kelvin_per_unit = temp_k / current
        self.power_plot_widget.setLabel('left', 'System Temperature',
                                        units='K')
        self.cal_status_label.setText(
            f"Calibrated: {self.kelvin_per_unit:.4g} K/unit")
        # Re-span the power bar from just below blank sky to roughly the
        # on-Sun level; the spinbox signals persist the values via
        # _on_power_range_changed
        bar_min = 10 * np.log10(CAL_BAR_MIN_K / self.kelvin_per_unit)
        bar_max = 10 * np.log10(CAL_BAR_SUN_K / self.kelvin_per_unit)
        self.power_min_spin.setValue(round(bar_min))
        self.power_max_spin.setValue(round(bar_max))
        self._redraw_power_curve()
        self.power_plot_widget.enableAutoRange()
        self._update_spectrum_label()
        if not self.spectrum_log_scale:
            self.spectrum_widget.enableAutoRange()
        print(f"Calibrated: current level = {temp_k:.1f} K "
              f"({self.kelvin_per_unit:.4g} K/unit)")

    def _on_clear_calibration(self):
        """Revert the total-power chart to arbitrary linear units."""
        self.kelvin_per_unit = None
        self.power_plot_widget.setLabel('left', 'Total Power (linear, arb.)')
        if hasattr(self, 'cal_status_label'):
            self.cal_status_label.setText("Uncalibrated (arb. units)")
        self._redraw_power_curve()
        self.power_plot_widget.enableAutoRange()
        self._update_spectrum_label()
        if not self.spectrum_log_scale:
            self.spectrum_widget.enableAutoRange()

    def _invalidate_calibration(self, reason):
        """Drop the temperature calibration when the raw power scale
        changes (bandwidth or FFT size)."""
        if self.kelvin_per_unit is not None:
            print(f"Temperature calibration cleared: {reason} changed")
            self._on_clear_calibration()

    def _reset_accumulator(self):
        """Reset the Python-side spectrum accumulator."""
        self.accumulator = None
        self.accumulator_count = 0
        self.accumulator_start_time = time.time()
        # Restart the total-power smoothing window and the per-integration
        # plot block too, so samples taken with different bandwidth/FFT/
        # integration settings don't mix
        self.power_raw.clear()
        self.power_block_sum = 0.0
        self.power_block_count = 0
        self.power_block_start = None

    def _update_freq_axis(self):
        """Update frequency axis after center frequency change."""
        self.freq_axis_hz = self._get_frequency_axis()
        self.freq_axis_mhz = self.freq_axis_hz / 1e6
        freq_min = self.freq_axis_mhz[0]
        freq_max = self.freq_axis_mhz[-1]
        self.waterfall_widget.setXRange(freq_min, freq_max)

    def _update_status(self):
        """Update status label and resolution display."""
        gain_str = "N/A" if self.sdr_type == 'demo' else f"{self.gain:.0f} dB"
        sdr_str = f"{self.sdr_type.upper()}" + (" (simulated)" if self.sdr_type == 'demo' else "")
        self.status_label.setText(
            f"SDR: {sdr_str} | "
            f"Center: {self.center_freq/1e6:.3f} MHz | "
            f"Span: {self.sample_rate/1e6:.2f} MHz | "
            f"Gain: {gain_str} | "
            f"Integration: {self.integration_time}s"
        )
        # Update resolution label if control panel exists
        if hasattr(self, 'resolution_label'):
            self.resolution_label.setText(f"{self.sample_rate/self.fft_size/1e3:.3f} kHz")

    def _update_display(self):
        """Update spectrum display and accumulate for integration."""
        try:
            spectrum_db = self.flowgraph.get_spectrum()
            if len(spectrum_db) == self.fft_size:
                # Accumulate for Python-side integration (in linear power domain)
                # Convert dB back to linear for proper averaging
                linear_power = 10 ** (spectrum_db / 10)
                if self.accumulator is None:
                    self.accumulator = linear_power.copy()
                else:
                    self.accumulator += linear_power
                self.accumulator_count += 1

                # Calculate integrated spectrum for display
                avg_linear = self.accumulator / self.accumulator_count
                integrated_db = 10 * np.log10(avg_linear)

                # Update spectrum plot with integrated data (shows true SNR)
                if self.spectrum_log_scale:
                    self.spectrum_curve.setData(self.freq_axis_mhz,
                                                integrated_db)
                elif self.kelvin_per_unit is not None:
                    # Per-channel temperature: scaled so a flat noise
                    # floor reads the calibrated system temperature
                    self.spectrum_curve.setData(
                        self.freq_axis_mhz,
                        avg_linear * (self.kelvin_per_unit * self.fft_size))
                else:
                    self.spectrum_curve.setData(self.freq_axis_mhz,
                                                avg_linear)

                # Total-power strip chart: one point per completed
                # integration period (adjacent ticks are ~fully correlated
                # over the integration window, so finer plotting adds no
                # information and wide-pen redraws at 10 Hz stall the GUI).
                # power_raw keeps the per-tick sliding window for the
                # calibration button.
                now = time.time()
                tick_power = float(np.sum(linear_power))
                self.power_raw.append((now, tick_power))
                window = max(self.integration_time, 0.1)
                while self.power_raw and self.power_raw[0][0] < now - window:
                    self.power_raw.popleft()
                if self.power_block_start is None:
                    self.power_block_start = now
                self.power_block_sum += tick_power
                self.power_block_count += 1
                if now - self.power_block_start >= window:
                    self.power_history.append(
                        (now, self.power_block_sum / self.power_block_count))
                    self.power_block_sum = 0.0
                    self.power_block_count = 0
                    self.power_block_start = now
                    self._prune_power_history(now)
                    self._redraw_power_curve()

                # Update accumulator display if control panel exists
                if hasattr(self, 'accum_label'):
                    elapsed = time.time() - self.accumulator_start_time
                    self.accum_label.setText(f"{self.accumulator_count} samples ({elapsed:.1f}s)")

                # Update total power display
                if hasattr(self, 'power_bar'):
                    # Sum power in linear domain, convert to dB
                    total_linear = np.sum(avg_linear)
                    total_db = 10 * np.log10(total_linear)
                    self.power_label.setText(f"{total_db:.2f} dB")
                    # Clamp to bar range
                    bar_val = int(np.clip(total_db, self.power_min, self.power_max))
                    self.power_bar.setValue(bar_val)

        except Exception as e:
            print(f"Display update error: {e}")

    def _init_hdf5(self, filename):
        """Initialize HDF5 file."""
        # The segment number and the reason for the roll go in through
        # init_hdf5, which writes them before it switches the file to SWMR
        # mode - after that, no attribute may be added.
        return init_hdf5(filename, self.freq_axis_hz, self.fft_size,
                         sdr_type=self.sdr_type, center_freq=self.center_freq,
                         sample_rate=self.sample_rate, gain=self.gain,
                         tuning_plan=getattr(self, 'tuning', None),
                         segment=getattr(self, 'hdf5_segment', 0),
                         segment_reason=getattr(self, 'hdf5_roll_reason', ''))

    def _next_hdf5_filename(self, reason):
        """A fresh recording, when the spectral geometry changed under us.

        Named like any other recording - the time, and the mode - rather than
        by the reason for the roll. The reason and the segment number go into
        the new file as attributes instead, which is where a reader can act on
        them; in the name they only produced things like
        `h1_data_rate_03_20260819T080527Z.h5`, where every word after the time
        was already an attribute of the file it named.
        """
        self.hdf5_segment += 1
        folder = os.path.dirname(os.path.abspath(self.output_file)) or "."
        self.hdf5_roll_reason = reason
        return observation_files.observation_filename(
            folder, self._recording_mode())

    def _recording_mode(self):
        """The word the filename carries: what the mount was doing.

        Taken from the observation metadata the scheduler passed in, so a
        rolled file keeps the mode of the run it belongs to. A receiver started
        by hand has no metadata and gets `manual`.
        """
        try:
            meta = json.loads(os.environ.get('H1_OBS_METADATA', '') or '{}')
        except ValueError:
            return observation_files.MANUAL_MODE
        return meta.get('observation_mode') or observation_files.MANUAL_MODE

    def _roll_hdf5_file(self, reason):
        """Start a new HDF5 file when frequency axis or FFT width changes."""
        if not hasattr(self, 'hf'):
            return

        try:
            self.hf.flush()
            self.hf.close()
        except Exception:
            pass

        self.output_file = self._next_hdf5_filename(reason)
        self.hf = self._init_hdf5(self.output_file)
        self.spectrum_count = 0
        self.waterfall_data.clear()
        self.waterfall_img.clear()

        if hasattr(self, 'count_label'):
            self.count_label.setText(f"Spectra saved: 0 (file: {os.path.basename(self.output_file)})")

        print(f"Started new HDF5 file for {reason} change: {self.output_file}")

    def _ensure_hdf5_geometry(self):
        """Ensure the active HDF5 datasets match the current FFT geometry."""
        name = 'spectra_kelvin' if 'spectra_kelvin' in self.hf else 'spectra_linear'
        if self.hf[name].shape[1] != self.fft_size:
            self._roll_hdf5_file(f"fft{self.fft_size}")
            return

        if self.hf['frequency_hz'].shape[0] != self.fft_size:
            self._roll_hdf5_file(f"freq{self.fft_size}")

    def _save_spectrum(self):
        """Finish the integration period: update the waterfall and, when
        recording, save the averaged spectrum to HDF5."""
        try:
            if self.accumulator is None or self.accumulator_count == 0:
                return

            # Calculate average in linear power domain
            avg_linear = self.accumulator / self.accumulator_count
            timestamp = time.time()
            integration_time = timestamp - self.accumulator_start_time

            if self.recording:
                # Save to HDF5 in linear power (radiometric accuracy)
                self._ensure_hdf5_geometry()
                append_spectrum(self.hf, avg_linear, timestamp,
                                integration_time, self.fft_size)

            # Add to waterfall (one row per integration, in dB for display)
            spectrum_db = 10 * np.log10(avg_linear)
            self.waterfall_data.append(spectrum_db.copy())
            if len(self.waterfall_data) > 0:
                waterfall_array = np.array(self.waterfall_data)
                self.waterfall_img.setImage(
                    waterfall_array.T,
                    autoLevels=False,
                    levels=(self.waterfall_min, self.waterfall_max)
                )
                freq_min = self.freq_axis_mhz[0]
                freq_max = self.freq_axis_mhz[-1]
                self.waterfall_img.setRect(
                    freq_min, 0,
                    freq_max - freq_min, len(self.waterfall_data)
                )

            if self.recording:
                self.spectrum_count += 1
                self.count_label.setText(
                    f"Spectra saved: {self.spectrum_count} "
                    f"(last: {self.accumulator_count} samples, "
                    f"{integration_time:.1f}s)"
                )

            # Reset accumulator for next integration period
            self.accumulator = None
            self.accumulator_count = 0
            self.accumulator_start_time = time.time()

        except Exception as e:
            print(f"Error saving spectrum: {e}")

    def start(self):
        """Start the flowgraph."""
        self.flowgraph.start()

    def closeEvent(self, event):
        """Handle window close."""
        QtCore.QSettings("SRT", "h1_receiver").setValue(
            "plot_splitter_state", self.plot_splitter.saveState())
        self.display_timer.stop()
        self.save_timer.stop()
        self.flowgraph.stop()
        self.flowgraph.wait()
        self.hf.close()
        print(f"\nTotal spectra saved: {self.spectrum_count}")
        print(f"Data written to: {self.output_file}")
        event.accept()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Hydrogen Line (21cm) Receiver',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python b210_h1_receiver.py --sdr b210
  python b210_h1_receiver.py --sdr rtlsdr
  python b210_h1_receiver.py --sdr rtlsdr --gain 45 --sample-rate 2.048e6
  python b210_h1_receiver.py --sdr demo   # Test GUI without hardware
        """
    )
    parser.add_argument(
        '--sdr', '-s',
        choices=['b210', 'rtlsdr', 'demo'],
        default='b210',
        help='SDR type: b210, rtlsdr, or demo for testing without hardware (default: b210)'
    )
    parser.add_argument(
        '--gain', '-g',
        type=float,
        default=None,
        help='RF gain in dB (default: 30 for both SDRs)'
    )
    parser.add_argument(
        '--sample-rate', '-r',
        type=float,
        default=None,
        help='Sample rate in Hz (default: 2.4e6 for B210, 2.048e6 for RTL-SDR)'
    )
    parser.add_argument(
        '--headless',
        action='store_true',
        help='Record without any GUI: no Qt, no display needed. This is how '
             'scheduled and Observe-tab observations run.'
    )

    args = parser.parse_args()

    if args.headless:
        # No QApplication, no widgets, no event loop. Ends on SIGTERM, which
        # is what stop_observation sends, or on Ctrl-C.
        # H1_MODE=pulsar: the fast filterbank instead of spectra (pulsar_fold.py).
        if os.environ.get('H1_MODE', '').strip().lower() == 'pulsar':
            recorder = PulsarRecorder(sdr_type=args.sdr)
        else:
            recorder = HeadlessRecorder(sdr_type=args.sdr,
                                        sample_rate=args.sample_rate,
                                        gain=args.gain)
        signal.signal(signal.SIGTERM, recorder.request_stop)
        signal.signal(signal.SIGINT, recorder.request_stop)
        recorder.run()
        return

    if not QT_AVAILABLE:
        print("The receiver GUI needs PyQt5 and pyqtgraph, which are not "
              f"importable here ({_QT_IMPORT_ERROR}).\n"
              "Use --headless to record without a display.", file=sys.stderr)
        raise SystemExit(1)

    # Create Qt application
    app = QtWidgets.QApplication([])

    # Create and show receiver (with control panel when run as main)
    receiver = H1ReceiverWindow(
        sdr_type=args.sdr,
        sample_rate=args.sample_rate,
        gain=args.gain,
        show_controls=True
    )
    receiver.show()

    # Start flowgraph
    receiver.start()

    print(f"\nH1 Receiver started")
    sdr_name = receiver.sdr_type.upper()
    if receiver.sdr_type == 'demo':
        sdr_name += " (simulated data - no hardware)"
    print(f"  SDR: {sdr_name}")
    print(f"  Center frequency: {CENTER_FREQ/1e6:.6f} MHz")
    print(f"  Sample rate: {receiver.sample_rate/1e6:.3f} MHz")
    print(f"  FFT size: {FFT_SIZE} bins")
    print(f"  Frequency resolution: {receiver.sample_rate/FFT_SIZE/1e3:.3f} kHz")
    print(f"  Integration time: {INTEGRATION_TIME} s")
    print(f"  Output file: {OUTPUT_FILE}")
    print("\nClose the window to stop\n")

    # Run Qt event loop
    app.exec_()


if __name__ == "__main__":
    main()
