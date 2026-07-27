"""
Two-channel USB oscilloscope front-end (Windows, Python).

pip install pyqt5 pyqtgraph pyserial numpy

Run:  python scope_app.py

NOTE ON CALIBRATION CONSTANTS (top of file): the attenuation ratios and
DC-offset voltage below are placeholders. Because the AC/DC coupling and
attenuation are physical switches on the board with no digital readback,
this app can't sense their position -- you tell it what the switch is set
to via the dropdowns, and it applies the matching constant. Measure your
actual board's divider ratios and offset bias with a known reference
voltage and update ATTEN_RATIOS / DC_OFFSET_VOLTS accordingly; the values
here are only a reasonable starting guess based on the schematic.
"""

import sys
import struct
import threading
import collections

import numpy as np
import serial
import serial.tools.list_ports
from PyQt5 import QtCore, QtWidgets, QtGui
import pyqtgraph as pg

# ----------------------------------------------------------------------
# Calibration constants -- EDIT to match your measured board values
# ----------------------------------------------------------------------
VREF = 3.30                 # ADC reference voltage
ADC_MAX = 4095.0            # 12-bit
DC_OFFSET_VOLTS = 1.65      # mid-rail bias injected by the DC-offset stage
ATTEN_RATIOS = {"1x": 1.0, "3x": 3.0, "7x": 7.0}

SYNC0, SYNC1 = 0xAA, 0x55
SAMPLES_PER_FRAME = 256      # must match firmware SAMPLES_PER_HALF
FRAME_LEN = 2 + 1 + 2 + SAMPLES_PER_FRAME * 4 + 1

RING_SECONDS = 5.0           # how much history to keep in memory


# ----------------------------------------------------------------------
# Serial reader thread
# ----------------------------------------------------------------------
class SerialReader(QtCore.QThread):
    frame_ready = QtCore.pyqtSignal(np.ndarray, np.ndarray)
    dropped_frame = QtCore.pyqtSignal(int)
    connected = QtCore.pyqtSignal(bool, str)

    def __init__(self, port):
        super().__init__()
        self.port_name = port
        self._running = False
        self._ser = None
        self._last_seq = None
        self._lock = threading.Lock()

    def run(self):
        try:
            self._ser = serial.Serial(self.port_name, baudrate=115200, timeout=0.5)
        except Exception as e:
            self.connected.emit(False, str(e))
            return

        self.connected.emit(True, self.port_name)
        self._running = True
        self._ser.reset_input_buffer()
        self._ser.write(b"S")

        buf = bytearray()
        while self._running:
            try:
                chunk = self._ser.read(4096)
            except Exception:
                break
            if not chunk:
                continue
            buf.extend(chunk)
            self._extract_frames(buf)

        try:
            self._ser.write(b"X")
            self._ser.close()
        except Exception:
            pass

    def _extract_frames(self, buf: bytearray):
        while True:
            sync_idx = buf.find(bytes([SYNC0, SYNC1]))
            if sync_idx < 0:
                if len(buf) > 4096:
                    del buf[:-2]
                return
            if sync_idx > 0:
                del buf[:sync_idx]
            if len(buf) < FRAME_LEN:
                return

            frame = buf[:FRAME_LEN]
            seq = frame[2]
            n = frame[3] | (frame[4] << 8)
            if n != SAMPLES_PER_FRAME:
                # desynced -- drop one byte and retry
                del buf[:1]
                continue

            payload = frame[5:5 + n * 4]
            checksum = frame[5 + n * 4]
            calc = 0
            for b in payload:
                calc ^= b
            del buf[:FRAME_LEN]

            if calc != checksum:
                continue  # corrupt frame, drop silently (counted via seq gap below)

            if self._last_seq is not None:
                expected = (self._last_seq + 1) & 0xFF
                if seq != expected:
                    gap = (seq - expected) & 0xFF
                    self.dropped_frame.emit(gap)
            self._last_seq = seq

            raw = np.frombuffer(bytes(payload), dtype="<u2")
            ch0 = raw[0::2].astype(np.float64)
            ch1 = raw[1::2].astype(np.float64)
            self.frame_ready.emit(ch0, ch1)

    def set_rate(self, arr_ticks: int):
        if self._ser and self._ser.is_open:
            self._ser.write(bytes([ord("R"), arr_ticks & 0xFF, (arr_ticks >> 8) & 0xFF]))

    def stop(self):
        self._running = False


# ----------------------------------------------------------------------
# Draggable cursor pair (used twice: time cursors + voltage cursors)
# ----------------------------------------------------------------------
class CursorPair(QtCore.QObject):
    moved = QtCore.pyqtSignal()

    def __init__(self, plot, angle, pos_a, pos_b, color):
        super().__init__()
        pen = pg.mkPen(color=color, width=1, style=QtCore.Qt.DashLine)
        self.a = pg.InfiniteLine(pos=pos_a, angle=angle, movable=True, pen=pen)
        self.b = pg.InfiniteLine(pos=pos_b, angle=angle, movable=True, pen=pen)
        plot.addItem(self.a)
        plot.addItem(self.b)
        self.a.sigPositionChanged.connect(self.moved.emit)
        self.b.sigPositionChanged.connect(self.moved.emit)

    def values(self):
        return self.a.value(), self.b.value()

    def set_visible(self, vis):
        self.a.setVisible(vis)
        self.b.setVisible(vis)


# ----------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------
class ScopeWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("STM32F411 Dual-Channel Scope")
        self.resize(1200, 750)

        self.sample_rate_hz = 200_000  # per-channel, must match firmware default
        self.hold = False
        self.reader = None

        self.buf_len = int(RING_SECONDS * self.sample_rate_hz)
        self.ch0_buf = collections.deque(maxlen=self.buf_len)
        self.ch1_buf = collections.deque(maxlen=self.buf_len)
        self.dropped_total = 0

        self._build_ui()

        self.refresh_timer = QtCore.QTimer()
        self.refresh_timer.timeout.connect(self._redraw)
        self.refresh_timer.start(33)  # ~30 fps

    # ---------------- UI construction ----------------
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)

        # --- Toolbar row ---
        bar = QtWidgets.QHBoxLayout()
        outer.addLayout(bar)

        self.port_box = QtWidgets.QComboBox()
        self._refresh_ports()
        bar.addWidget(QtWidgets.QLabel("Port:"))
        bar.addWidget(self.port_box)

        connect_btn = QtWidgets.QPushButton("Connect")
        connect_btn.clicked.connect(self._toggle_connect)
        self.connect_btn = connect_btn
        bar.addWidget(connect_btn)

        bar.addSpacing(20)
        for ch in (0, 1):
            bar.addWidget(QtWidgets.QLabel(f"CH{ch} Coupling:"))
            box = QtWidgets.QComboBox()
            box.addItems(["DC", "AC"])
            setattr(self, f"coupling{ch}", box)
            bar.addWidget(box)

            bar.addWidget(QtWidgets.QLabel(f"CH{ch} Atten:"))
            abox = QtWidgets.QComboBox()
            abox.addItems(list(ATTEN_RATIOS.keys()))
            setattr(self, f"atten{ch}", abox)
            bar.addWidget(abox)

        bar.addSpacing(20)
        bar.addWidget(QtWidgets.QLabel("Math:"))
        self.math_box = QtWidgets.QComboBox()
        self.math_box.addItems(["Off", "CH0 - CH1", "CH1 - CH0", "CH0 + CH1"])
        bar.addWidget(self.math_box)

        bar.addStretch()

        self.hold_btn = QtWidgets.QPushButton("Hold")
        self.hold_btn.setCheckable(True)
        self.hold_btn.clicked.connect(self._toggle_hold)
        bar.addWidget(self.hold_btn)

        self.cursor_btn = QtWidgets.QPushButton("Cursors")
        self.cursor_btn.setCheckable(True)
        self.cursor_btn.clicked.connect(self._toggle_cursors)
        bar.addWidget(self.cursor_btn)

        stop_btn = QtWidgets.QPushButton("Stop / Reset")
        stop_btn.clicked.connect(self._stop_reset)
        bar.addWidget(stop_btn)

        # --- Plot ---
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.setLabel("left", "Voltage", units="V")
        self.plot.addLegend()
        outer.addWidget(self.plot, stretch=1)

        self.curve0 = self.plot.plot(pen=pg.mkPen("y", width=1), name="CH0")
        self.curve1 = self.plot.plot(pen=pg.mkPen("c", width=1), name="CH1")
        self.curve_math = self.plot.plot(pen=pg.mkPen("m", width=1), name="Math")

        # custom wheel zoom: plain wheel = time (X), Ctrl+wheel = voltage (Y)
        self.plot.getViewBox().wheelEvent = self._wheel_zoom

        # status/readout row
        self.status_lbl = QtWidgets.QLabel("Not connected")
        outer.addWidget(self.status_lbl)
        self.cursor_lbl = QtWidgets.QLabel("")
        outer.addWidget(self.cursor_lbl)

        # cursors (created hidden, toggled visible on demand)
        vb = self.plot.getViewBox()
        self.tcursors = CursorPair(self.plot, 90, -0.001, 0.001, "orange")
        self.vcursors = CursorPair(self.plot, 0, 0.5, -0.5, "green")
        self.tcursors.set_visible(False)
        self.vcursors.set_visible(False)
        self.tcursors.moved.connect(self._update_cursor_readout)
        self.vcursors.moved.connect(self._update_cursor_readout)

    def _refresh_ports(self):
        self.port_box.clear()
        for p in serial.tools.list_ports.comports():
            self.port_box.addItem(p.device)

    # ---------------- connection handling ----------------
    def _toggle_connect(self):
        if self.reader is None:
            port = self.port_box.currentText()
            if not port:
                QtWidgets.QMessageBox.warning(self, "No port", "Select a COM port first.")
                return
            self.reader = SerialReader(port)
            self.reader.frame_ready.connect(self._on_frame)
            self.reader.dropped_frame.connect(self._on_dropped)
            self.reader.connected.connect(self._on_connected)
            self.reader.start()
            self.connect_btn.setText("Disconnect")
        else:
            self.reader.stop()
            self.reader.wait(1000)
            self.reader = None
            self.connect_btn.setText("Connect")
            self.status_lbl.setText("Disconnected")

    def _on_connected(self, ok, msg):
        if ok:
            self.status_lbl.setText(f"Connected: {msg}")
        else:
            QtWidgets.QMessageBox.critical(self, "Connection failed", msg)
            self.reader = None
            self.connect_btn.setText("Connect")

    def _on_dropped(self, gap):
        self.dropped_total += gap
        self.status_lbl.setText(f"Streaming -- dropped frames total: {self.dropped_total}")

    # ---------------- data handling ----------------
    def _on_frame(self, raw_ch0, raw_ch1):
        if self.hold:
            return  # discard while held -- display stays frozen
        self.ch0_buf.extend(raw_ch0.tolist())
        self.ch1_buf.extend(raw_ch1.tolist())

    def _convert(self, raw_array, channel_idx):
        """ADC counts -> input-referred volts, given this channel's
        coupling/attenuation dropdown selections."""
        atten_label = getattr(self, f"atten{channel_idx}").currentText()
        ratio = ATTEN_RATIOS[atten_label]
        v_adc = (raw_array / ADC_MAX) * VREF
        v_input = (v_adc - DC_OFFSET_VOLTS) * ratio
        # Coupling selection doesn't change the math here: AC coupling
        # already removes the DC component in hardware before the ADC
        # sees it, so v_input in AC mode is "AC component only" and in
        # DC mode is "full signal" -- both are correctly scaled by the
        # same formula. The dropdown exists so the UI label/readout can
        # tell the user which they're looking at.
        return v_input

    def _redraw(self):
        if len(self.ch0_buf) < 2:
            return
        ch0_raw = np.array(self.ch0_buf)
        ch1_raw = np.array(self.ch1_buf)
        n = min(len(ch0_raw), len(ch1_raw))
        ch0_raw, ch1_raw = ch0_raw[-n:], ch1_raw[-n:]

        t = (np.arange(n) - n) / self.sample_rate_hz  # negative time, "now" at 0

        v0 = self._convert(ch0_raw, 0)
        v1 = self._convert(ch1_raw, 1)

        self.curve0.setData(t, v0)
        self.curve1.setData(t, v1)

        math_mode = self.math_box.currentText()
        if math_mode == "CH0 - CH1":
            self.curve_math.setData(t, v0 - v1)
        elif math_mode == "CH1 - CH0":
            self.curve_math.setData(t, v1 - v0)
        elif math_mode == "CH0 + CH1":
            self.curve_math.setData(t, v0 + v1)
        else:
            self.curve_math.setData([], [])

    # ---------------- controls ----------------
    def _toggle_hold(self):
        self.hold = self.hold_btn.isChecked()

    def _toggle_cursors(self):
        vis = self.cursor_btn.isChecked()
        self.tcursors.set_visible(vis)
        self.vcursors.set_visible(vis)
        self._update_cursor_readout()

    def _update_cursor_readout(self):
        if not self.cursor_btn.isChecked():
            self.cursor_lbl.setText("")
            return
        t_a, t_b = self.tcursors.values()
        v_a, v_b = self.vcursors.values()
        dt = abs(t_b - t_a)
        dv = abs(v_b - v_a)
        freq = (1.0 / dt) if dt > 0 else float("inf")
        self.cursor_lbl.setText(
            f"Δt = {dt*1e3:.4f} ms  (f = {freq:.2f} Hz)   |   ΔV = {dv:.4f} V"
        )

    def _stop_reset(self):
        self.ch0_buf.clear()
        self.ch1_buf.clear()
        self.dropped_total = 0
        self.curve0.clear()
        self.curve1.clear()
        self.curve_math.clear()
        self.status_lbl.setText("Reset. " + ("Streaming stopped." if self.reader is None else "Still connected."))

    def _wheel_zoom(self, ev):
        vb = self.plot.getViewBox()
        modifiers = QtWidgets.QApplication.keyboardModifiers()
        factor = 0.9 if ev.angleDelta().y() > 0 else 1.1
        if modifiers & QtCore.Qt.ControlModifier:
            vb.scaleBy((1.0, factor))   # voltage (Y) zoom
        else:
            vb.scaleBy((factor, 1.0))   # time (X) zoom
        ev.accept()

    def closeEvent(self, ev):
        if self.reader is not None:
            self.reader.stop()
            self.reader.wait(1000)
        ev.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    pg.setConfigOptions(antialias=True, background="k", foreground="w")
    win = ScopeWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
