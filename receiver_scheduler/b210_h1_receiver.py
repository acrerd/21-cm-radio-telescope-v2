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
from collections import deque

from gnuradio import gr, fft, blocks, analog, filter
from gnuradio.fft import window

from tuning import (ANALOG_BW_FACTOR as DEFAULT_ANALOG_BW_FACTOR,
                    DEFAULT_LO_OFFSET_HZ, describe_tuning, plan_tuning,
                    fixed_instrument, h1_subband_plan, describe_instrument)
import observation_files

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
        'gain': 40,
    },
    'rtlsdr': {
        'sample_rate': 2.048e6,
        'gain': 40,
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

    def __init__(self, sdr_type, instrument):
        gr.top_block.__init__(self, "H1 two-product processor", catch_exceptions=True)
        self.instrument = dict(instrument)
        self.sdr_type = sdr_type
        self.sample_rate = float(instrument["sample_rate_hz"])
        self.center_freq = float(instrument["lo_hz"])
        self.gain = float(instrument["gain_db"])
        self.wide_channels = int(instrument["wide_channels"])
        self.subband = h1_subband_plan(instrument)
        self._build_blocks()
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
            print(f"  Failed to initialize {self.sdr_type.upper()}: {e}")
            print("  Falling back to demo mode...")
            self.sdr_type = 'demo'
            self.sdr_source, self.throttle, actual_rate = create_demo_source(self.sample_rate)
            self.sample_rate = actual_rate

        # --- wide product: the whole band, coarsely ---
        nw = self.wide_channels
        self.wide_s2v = blocks.stream_to_vector(gr.sizeof_gr_complex, nw)
        self.wide_fft = fft.fft_vcc(nw, True, window.blackmanharris(nw), True, 1)
        self.wide_mag = blocks.complex_to_mag_squared(nw)
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
        h1_presum = max(1, int(sb["out_rate_hz"] / nh / self.SINK_RATE_HZ))
        self.h1_sum = blocks.integrate_ff(h1_presum, nh)
        self.h1_acc = _VectorAccumulator(nh, h1_presum)
        print("  H I sub-band: %.3f-%.3f MHz, %d taps, decimate by %d to %.1f Msps, "
              "%d channels of %.2f kHz" % (sb["band_hz"][0] / 1e6, sb["band_hz"][1] / 1e6,
                                          len(taps), decim, sb["out_rate_hz"] / 1e6,
                                          nh, sb["channel_width_hz"] / 1e3))

    def _connect_blocks(self):
        src = self.sdr_source
        if self.throttle is not None:
            self.connect((self.sdr_source, 0), (self.throttle, 0))
            src = self.throttle
        self.connect((src, 0), (self.wide_s2v, 0))
        self.connect((self.wide_s2v, 0), (self.wide_fft, 0))
        self.connect((self.wide_fft, 0), (self.wide_mag, 0))
        self.connect((self.wide_mag, 0), (self.wide_sum, 0))
        self.connect((self.wide_sum, 0), (self.wide_acc, 0))
        self.connect((src, 0), (self.h1_xlate, 0))
        self.connect((self.h1_xlate, 0), (self.h1_s2v, 0))
        self.connect((self.h1_s2v, 0), (self.h1_fft, 0))
        self.connect((self.h1_fft, 0), (self.h1_mag, 0))
        self.connect((self.h1_mag, 0), (self.h1_sum, 0))
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
            cut = data.rfind(b"\n")
            carry = data[cut + 1:] if cut >= 0 else data[-64:]
            if n:
                with self._lock:
                    self.count += n

    def take(self):
        """Overflows since the last call."""
        with self._lock:
            n, self.count = self.count, 0
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
              wide=None, instrument=None):
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
    a wide product also carries `overflows`, one count per record.
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
                    wide_linear=None, overflows=0):
    """Append one integrated spectrum, flushing so a reader sees it promptly.

    `wide_linear` is the continuum product's record for a file that has one;
    `overflows` the UHD overflow count during this record.
    """
    name = 'spectra_kelvin' if 'spectra_kelvin' in hf else 'spectra_linear'
    values = avg_linear
    if name == 'spectra_kelvin':
        # counts -> kelvin, with the correction the file already carries, so
        # what is written is exactly what the stored numbers reverse.
        values = (avg_linear / hf['bandpass_correction'][:]
                  / hf.attrs['applied_gain_counts_per_k']
                  - hf.attrs['applied_t_sys_k'])
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
            wvalues = (wvalues / hf['bandpass_correction_wide'][:]
                       / hf.attrs['applied_gain_counts_per_k']
                       - hf.attrs['applied_t_sys_k'])
        nw = hf[wname].shape[1]
        hf[wname].resize((n + 1, nw))
        hf[wname][n, :] = wvalues.astype(np.float32)
    if 'overflows' in hf:
        hf['overflows'].resize((n + 1,))
        hf['overflows'][n] = int(overflows)
    hf.flush()
    _append_live_summary(hf, avg_linear, timestamp, integration_time, n + 1,
                         wide_linear=wide_linear, overflows=overflows)
    return n + 1


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
                         wide_linear=None, overflows=0):
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
                            instrument=instrument)
        self.spectrum_count = 0
        self._stop = threading.Event()
        self.overflows = _OverflowCounter()

    def request_stop(self, *_args):
        """Signal handler and API: finish the current tick and shut down."""
        self._stop.set()

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
        try:
            while not self._stop.is_set():
                self._stop.wait(self.TICK_S)
                now = time.time()
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
                self.spectrum_count = append_spectrum(
                    self.hf, np.asarray(h1, dtype=float)[self.h1_keep], now,
                    now - period_start, self.fft_size,
                    wide_linear=np.asarray(wide, dtype=float),
                    overflows=self.overflows.take())
                period_start = now
        finally:
            self.flowgraph.stop()
            self.flowgraph.wait()
            self.hf.close()
            self.overflows.stop()
            print(f"Total spectra saved: {self.spectrum_count}", flush=True)
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
        help='RF gain in dB (default: 40 for both SDRs)'
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
