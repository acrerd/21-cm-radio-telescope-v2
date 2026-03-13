#!/usr/bin/env python3
"""
Hydrogen Line (21cm) Receiver for SDR
Supports Ettus B210 and RTL-SDR
Uses GNU Radio for signal processing, PyQtGraph for display

Measures spectrum around 1420.405 MHz, displays real-time integrated spectrum
and waterfall, and writes integrated data to HDF5.
"""

import argparse
import numpy as np
import h5py
import time
import os
from datetime import datetime, timezone
from collections import deque

from gnuradio import gr, fft, blocks, analog
from gnuradio.fft import window
from PyQt5 import QtWidgets, QtCore
import pyqtgraph as pg

# Configuration (can be overridden via environment variables)
CENTER_FREQ = float(os.environ.get('H1_CENTER_FREQ', 1420.405e6))
FFT_SIZE = int(os.environ.get('H1_FFT_SIZE', 4096))
INTEGRATION_TIME = float(os.environ.get('H1_INTEGRATION_TIME', 3.0))
OUTPUT_FILE = os.environ.get('H1_OUTPUT_FILE', "h1_data.h5")
WATERFALL_HISTORY = 100     # Number of spectra to show in waterfall

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
        return create_demo_source(sample_rate)

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
        source.set_center_freq(center_freq, 0)
        source.set_gain(gain, 0)
        source.set_antenna("RX2", 0)

        actual_freq = source.get_center_freq(0)
        actual_rate = source.get_samp_rate(0)
        actual_gain = source.get_gain(0)

    elif sdr_type == 'rtlsdr':
        import osmosdr
        source = osmosdr.source(args="numchan=1 rtl=0")
        source.set_sample_rate(sample_rate)
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
    print(f"  Gain: {actual_gain:.1f} dB")

    return source, throttle, actual_rate


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
            self.sdr_source, self.throttle, actual_rate = create_sdr_source(
                self.sdr_type, self.sample_rate, self.center_freq, self.gain
            )
            self.sample_rate = actual_rate
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

        # Moving average for integration (match INTEGRATION_TIME for consistent SNR)
        avg_length = max(1, int(self.sample_rate / self.fft_size * INTEGRATION_TIME))
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


class H1ReceiverWindow(QtWidgets.QMainWindow):
    """Main window with PyQtGraph displays for integrated spectrum."""

    def __init__(self, sdr_type='b210', sample_rate=None, gain=None):
        super().__init__()

        self.sdr_type = sdr_type
        self.center_freq = CENTER_FREQ
        self.fft_size = FFT_SIZE

        # Get defaults
        defaults = SDR_DEFAULTS.get(sdr_type, SDR_DEFAULTS['demo'])
        self.sample_rate = sample_rate if sample_rate else defaults['sample_rate']
        self.gain = gain if gain else defaults['gain']

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

        # HDF5 setup
        self.hf = self._init_hdf5()
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
        self.save_timer.start(int(INTEGRATION_TIME * 1000))

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
        layout = QtWidgets.QVBoxLayout(central)

        # Status bar
        gain_str = "N/A" if self.sdr_type == 'demo' else f"{self.gain} dB"
        sdr_str = f"{self.sdr_type.upper()}" + (" (simulated)" if self.sdr_type == 'demo' else "")
        self.status_label = QtWidgets.QLabel(
            f"SDR: {sdr_str} | "
            f"Center: {self.center_freq/1e6:.3f} MHz | "
            f"Span: {self.sample_rate/1e6:.2f} MHz | "
            f"Gain: {gain_str} | "
            f"Integration: {INTEGRATION_TIME}s"
        )
        self.status_label.setStyleSheet("font-family: monospace; font-size: 12px;")
        layout.addWidget(self.status_label)

        # PyQtGraph setup
        pg.setConfigOptions(antialias=True)

        # Spectrum plot
        self.spectrum_widget = pg.PlotWidget(title="Integrated Spectrum")
        self.spectrum_widget.setLabel('left', 'Power', units='dB')
        self.spectrum_widget.setLabel('bottom', 'Frequency', units='MHz')
        self.spectrum_widget.showGrid(x=True, y=True)
        self.spectrum_widget.setYRange(-80, -20)

        # H1 line marker
        h1_line = pg.InfiniteLine(pos=1420.405, angle=90, pen=pg.mkPen('r', style=QtCore.Qt.DashLine))
        self.spectrum_widget.addItem(h1_line)

        self.spectrum_curve = self.spectrum_widget.plot(
            self.freq_axis_mhz,
            np.zeros(self.fft_size),
            pen=pg.mkPen('c', width=1)
        )
        layout.addWidget(self.spectrum_widget, stretch=2)

        # Waterfall plot
        self.waterfall_widget = pg.PlotWidget(title="Waterfall (Integrated Spectra)")
        self.waterfall_widget.setLabel('left', 'Time', units='integrations')
        self.waterfall_widget.setLabel('bottom', 'Frequency', units='MHz')

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

        layout.addWidget(self.waterfall_widget, stretch=1)

        # Count label
        count_layout = QtWidgets.QHBoxLayout()
        count_layout.addStretch()
        self.count_label = QtWidgets.QLabel("Spectra saved: 0")
        self.count_label.setStyleSheet("font-family: monospace; font-size: 12px;")
        count_layout.addWidget(self.count_label)
        layout.addLayout(count_layout)

    def _update_display(self):
        """Update spectrum and waterfall displays."""
        try:
            spectrum = self.flowgraph.get_spectrum()
            if len(spectrum) == self.fft_size:
                # Update spectrum plot
                self.spectrum_curve.setData(self.freq_axis_mhz, spectrum)

                # Update waterfall
                self.waterfall_data.append(spectrum.copy())
                if len(self.waterfall_data) > 0:
                    waterfall_array = np.array(self.waterfall_data)
                    self.waterfall_img.setImage(
                        waterfall_array.T,
                        autoLevels=False,
                        levels=(-70, -30)
                    )
                    # Update transform for correct scaling
                    freq_min = self.freq_axis_mhz[0]
                    freq_max = self.freq_axis_mhz[-1]
                    self.waterfall_img.setRect(
                        freq_min, 0,
                        freq_max - freq_min, len(self.waterfall_data)
                    )
        except Exception as e:
            print(f"Display update error: {e}")

    def _init_hdf5(self):
        """Initialize HDF5 file."""
        hf = h5py.File(OUTPUT_FILE, 'w')

        hf.create_dataset('frequency_hz', data=self.freq_axis_hz)

        hf.create_dataset('spectra_db',
                          shape=(0, self.fft_size),
                          maxshape=(None, self.fft_size),
                          dtype='float32',
                          chunks=(1, self.fft_size),
                          compression='gzip',
                          compression_opts=4)

        hf.create_dataset('timestamps',
                          shape=(0,),
                          maxshape=(None,),
                          dtype='float64')

        hf.create_dataset('integration_times',
                          shape=(0,),
                          maxshape=(None,),
                          dtype='float32')

        hf.attrs['sdr_type'] = self.sdr_type
        hf.attrs['center_freq_hz'] = self.center_freq
        hf.attrs['sample_rate_hz'] = self.sample_rate
        hf.attrs['fft_size'] = self.fft_size
        hf.attrs['gain_db'] = self.gain
        hf.attrs['nominal_integration_time'] = INTEGRATION_TIME
        hf.attrs['created'] = datetime.now(timezone.utc).isoformat()

        return hf

    def _save_spectrum(self):
        """Save current spectrum to HDF5."""
        try:
            spectrum = self.flowgraph.get_spectrum()

            if len(spectrum) == self.fft_size:
                timestamp = time.time()
                integration_time = timestamp - self.last_save_time
                self.last_save_time = timestamp

                n = self.hf['spectra_db'].shape[0]
                self.hf['spectra_db'].resize((n + 1, self.fft_size))
                self.hf['timestamps'].resize((n + 1,))
                self.hf['integration_times'].resize((n + 1,))

                self.hf['spectra_db'][n, :] = spectrum.astype(np.float32)
                self.hf['timestamps'][n] = timestamp
                self.hf['integration_times'][n] = integration_time
                self.hf.flush()

                self.spectrum_count += 1
                self.count_label.setText(f"Spectra saved: {self.spectrum_count}")

        except Exception as e:
            print(f"Error saving spectrum: {e}")

    def start(self):
        """Start the flowgraph."""
        self.flowgraph.start()

    def closeEvent(self, event):
        """Handle window close."""
        self.display_timer.stop()
        self.save_timer.stop()
        self.flowgraph.stop()
        self.flowgraph.wait()
        self.hf.close()
        print(f"\nTotal spectra saved: {self.spectrum_count}")
        print(f"Data written to: {OUTPUT_FILE}")
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

    args = parser.parse_args()

    # Create Qt application
    app = QtWidgets.QApplication([])

    # Create and show receiver
    receiver = H1ReceiverWindow(
        sdr_type=args.sdr,
        sample_rate=args.sample_rate,
        gain=args.gain
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
