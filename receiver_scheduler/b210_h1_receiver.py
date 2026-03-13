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


class H1ReceiverWindow(QtWidgets.QMainWindow):
    """Main window with PyQtGraph displays for integrated spectrum."""

    def __init__(self, sdr_type='b210', sample_rate=None, gain=None, show_controls=False):
        super().__init__()

        self.sdr_type = sdr_type
        self.center_freq = CENTER_FREQ
        self.fft_size = FFT_SIZE
        self.integration_time = INTEGRATION_TIME
        self.show_controls = show_controls

        # Get defaults
        defaults = SDR_DEFAULTS.get(sdr_type, SDR_DEFAULTS['demo'])
        self.sample_rate = sample_rate if sample_rate else defaults['sample_rate']
        self.gain = gain if gain else defaults['gain']

        # Display settings
        self.waterfall_min = -70
        self.waterfall_max = -30

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

        # Python-side accumulator for long integration times
        # GNU Radio does short averaging for display; Python accumulates for saves
        self.accumulator = None  # Will hold sum of linear power spectra
        self.accumulator_count = 0
        self.accumulator_start_time = time.time()

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

        # Spectrum plot
        self.spectrum_widget = pg.PlotWidget(title="Integrated Spectrum")
        self.spectrum_widget.setLabel('left', 'Power', units='dB')
        self.spectrum_widget.setLabel('bottom', 'Frequency', units='MHz')
        self.spectrum_widget.showGrid(x=True, y=True)
        self.spectrum_widget.enableAutoRange()

        # H1 line marker
        h1_line = pg.InfiniteLine(pos=1420.405, angle=90, pen=pg.mkPen('r', style=QtCore.Qt.DashLine))
        self.spectrum_widget.addItem(h1_line)

        self.spectrum_curve = self.spectrum_widget.plot(
            self.freq_axis_mhz,
            np.zeros(self.fft_size),
            pen=pg.mkPen('c', width=1)
        )
        layout.addWidget(self.spectrum_widget, stretch=2)

        # Waterfall plot (one row per saved integration)
        self.waterfall_widget = pg.PlotWidget(title="Waterfall (Saved Integrations)")
        self.waterfall_widget.setLabel('left', 'Integration', units='#')
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
        self.recording = True

        panel_layout.addWidget(int_group)

        # Spectrum Display Settings
        spec_group = QtWidgets.QGroupBox("Spectrum Display")
        spec_layout = QtWidgets.QFormLayout(spec_group)

        self.autoscale_btn = QtWidgets.QPushButton("Auto Scale")
        self.autoscale_btn.clicked.connect(self._on_autoscale_spectrum)
        spec_layout.addRow(self.autoscale_btn)

        panel_layout.addWidget(spec_group)

        # Total Power Display
        power_group = QtWidgets.QGroupBox("Total Power")
        power_layout = QtWidgets.QFormLayout(power_group)

        self.power_min = -80
        self.power_max = -20

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

        panel_layout.addWidget(power_group)

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
                self._update_status()
            except Exception as e:
                print(f"Error setting frequency: {e}")
        elif self.sdr_type == 'rtlsdr':
            try:
                self.flowgraph.sdr_source.set_center_freq(new_freq, 0)
                self.center_freq = self.flowgraph.sdr_source.get_center_freq(0)
                self._update_freq_axis()
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
                self.flowgraph.sample_rate = self.sample_rate
                self._update_freq_axis()
                self._reset_accumulator()
                self._update_status()
                print(f"Bandwidth changed to {self.sample_rate/1e6:.2f} MHz")
            elif self.sdr_type == 'rtlsdr':
                self.flowgraph.sdr_source.set_sample_rate(new_rate)
                self.sample_rate = self.flowgraph.sdr_source.get_sample_rate()
                self.flowgraph.sample_rate = self.sample_rate
                self._update_freq_axis()
                self._reset_accumulator()
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

            # Update frequency axis and display
            self.freq_axis_hz = self._get_frequency_axis()
            self.freq_axis_mhz = self.freq_axis_hz / 1e6
            self.spectrum_curve.setData(self.freq_axis_mhz, np.zeros(self.fft_size))
            self.waterfall_data.clear()

            # Reset Python-side accumulator
            self._reset_accumulator()

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
        """Handle power bar range change."""
        self.power_min = self.power_min_spin.value()
        self.power_max = self.power_max_spin.value()
        self.power_bar.setRange(self.power_min, self.power_max)

    def _on_waterfall_range_changed(self):
        """Handle waterfall color range change."""
        self.waterfall_min = self.wf_min_spin.value()
        self.waterfall_max = self.wf_max_spin.value()

    def _on_clear_waterfall(self):
        """Clear the waterfall history."""
        self.waterfall_data.clear()

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
        """Toggle recording on/off."""
        self.recording = self.record_btn.isChecked()
        if self.recording:
            self.record_btn.setText("Stop Recording")
            self._reset_accumulator()
            self.save_timer.start()
            print("Recording started")
        else:
            self.record_btn.setText("Start Recording")
            self.save_timer.stop()
            print("Recording stopped")

    def _on_autoscale_spectrum(self):
        """Auto-scale the spectrum plot to fit current data."""
        self.spectrum_widget.enableAutoRange()
        self.spectrum_widget.autoRange()

    def _reset_accumulator(self):
        """Reset the Python-side spectrum accumulator."""
        self.accumulator = None
        self.accumulator_count = 0
        self.accumulator_start_time = time.time()

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
                self.spectrum_curve.setData(self.freq_axis_mhz, integrated_db)

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
        """Save accumulated spectrum to HDF5 and update waterfall."""
        try:
            if self.accumulator is None or self.accumulator_count == 0:
                return

            # Calculate average in linear domain, convert back to dB
            avg_linear = self.accumulator / self.accumulator_count
            spectrum_db = 10 * np.log10(avg_linear)

            timestamp = time.time()
            integration_time = timestamp - self.accumulator_start_time

            # Save to HDF5
            n = self.hf['spectra_db'].shape[0]
            self.hf['spectra_db'].resize((n + 1, self.fft_size))
            self.hf['timestamps'].resize((n + 1,))
            self.hf['integration_times'].resize((n + 1,))

            self.hf['spectra_db'][n, :] = spectrum_db.astype(np.float32)
            self.hf['timestamps'][n] = timestamp
            self.hf['integration_times'][n] = integration_time
            self.hf.flush()

            # Add to waterfall (one row per integration)
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

            self.spectrum_count += 1
            self.count_label.setText(
                f"Spectra saved: {self.spectrum_count} "
                f"(last: {self.accumulator_count} samples, {integration_time:.1f}s)"
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
