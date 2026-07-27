# Windows scope app -- quick start

```
pip install pyqt5 pyqtgraph pyserial numpy
python scope_app.py
```

1. Plug the board in, it should enumerate as a COM port (STMicroelectronics
   Virtual COM Port, once you've set the USB VID/PID/strings in the CubeMX
   USB_DEVICE config -- default ST VID works fine for development).
2. Pick the COM port from the dropdown, click Connect. This sends `'S'` to
   the firmware to start streaming.
3. Set the CH0/CH1 coupling and attenuation dropdowns to **match your
   physical switch positions** -- the firmware has no way to read those
   switches, so this is manual on purpose.
4. **Hold** freezes the displayed trace (acquisition keeps running
   underneath, new data is just not pushed into the plot until you release
   it) -- functions like a normal scope's hold/run-stop for reading a
   waveform without it scrolling under you.
5. **Scroll wheel** over the plot zooms the time axis; **Ctrl+scroll** zooms
   voltage.
6. **Cursors** toggles two time cursors (vertical, draggable) and two
   voltage cursors (horizontal, draggable); the readout line below the plot
   shows Δt, the implied frequency, and ΔV live as you drag them.
7. **Math** dropdown adds a third trace: CH0−CH1, CH1−CH0, or CH0+CH1.
8. **Stop/Reset** clears the buffers and the plot without disconnecting.

## Things you'll likely need to tune
- `DC_OFFSET_VOLTS` and `ATTEN_RATIOS` at the top of `scope_app.py` are
  placeholders based on the schematic's apparent divider values -- measure
  your actual board with a known DC reference on each attenuation setting
  and correct these constants, or the displayed voltage will be
  systematically off by a fixed scale/offset per range.
- `sample_rate_hz` in `ScopeWindow.__init__` must match whatever rate the
  firmware is actually running (default 200 kHz aggregate in the firmware
  README). If you send an `'R'` rate-change command, update this value to
  match or your time axis will be wrong (amplitude is unaffected).
- If you see "dropped frames" climbing in the status bar, the sample rate
  is outrunning what your USB link/host can sustain -- back it off via the
  `set_rate()` method (not yet wired to a UI control, intentionally left as
  a config constant until you've found a rate that reliably shows zero
  drops on your actual machine).
