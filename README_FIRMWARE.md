# Firmware: dual-channel streaming acquisition (STM32F411RET6)

This is written to be dropped into an **STM32CubeIDE** project generated from
a `.ioc` file, not a from-scratch bare-metal project — regenerating all of
HAL/USB middleware by hand would just be re-deriving what CubeMX already
does correctly. Below is the exact CubeMX configuration to set up first,
then `main_additions.c` contains everything you write by hand.

## 1. CubeMX peripheral configuration

**RCC**
- HSE: Crystal/Ceramic Resonator (matches your 8 MHz X1)
- Enable USB_OTG_FS clock source from PLL

**Clock Configuration tab**
- HSE 8 MHz → PLL → SYSCLK = 96 MHz (max stable for USB: PLL48CLK must land
  exactly on 48 MHz, so with 8 MHz HSE: PLLM=8, PLLN=192, PLLP=2 → 96 MHz
  SYSCLK, PLLQ=4 → 48 MHz USB clock). CubeMX will compute this for you if
  you type in target SYSCLK=96 and let it auto-solve PLLQ for 48.
- APB1 = 48 MHz, APB2 = 96 MHz (timer/ADC clocks scale off these)

**ADC1**
- Mode: Independent, 12-bit resolution
- IN0 → PA0 (your ADC1_IN0 net), IN1 → PA1 (ADC1_IN1 net)
- Scan Conversion Mode: Enabled
- Continuous Conversion Mode: Disabled (we trigger via timer instead —
  gives you a deterministic, evenly-spaced sample rate rather than
  free-running-as-fast-as-possible, which matters for accurate time-axis
  reconstruction on the PC side)
- External Trigger: Timer 2 Trigger Out event, rising edge
- DMA: Enabled, Circular mode, Half-Word to Half-Word, Peripheral increment
  disabled, Memory increment enabled
- Rank 1 = Channel 0, Rank 2 = Channel 1, both Sample Time = 15 cycles
  (fast; increase to 56/84 cycles later if you see noisy readings — trades
  max rate for settling accuracy against your 47 Ω/4.7 nF LPF's source
  impedance)

**TIM2**
- Internal Clock, no prescaler needed if you compute ARR directly from
  48 MHz (APB1 timer clock is x2'd to 96 MHz on F4 since APB1 prescaler > 1)
- Trigger Event Selection (TRGO) = Update Event
- ARR set at runtime by firmware (see `set_sample_rate()` below) rather than
  fixed in CubeMX, so you can tune it from the PC app without reflashing

**USB_DEVICE**
- Class: Communication Device Class (Virtual Port Com) — this gets you a
  COM port on Windows with zero custom driver work. (A custom bulk-only
  WinUSB class would squeeze out somewhat more throughput than CDC's
  interrupt+bulk overhead, but CDC is dramatically simpler to get right
  first; revisit only if you actually hit the throughput ceiling in
  practice and need the last ~20%.)

Generate the project, then add `main_additions.c`'s contents into the
generated `main.c` in the marked `USER CODE` sections (function bodies go
in `USER CODE 4`, includes in `USER CODE Includes`, the calls to
`Scope_Init()` and `Scope_Loop()` go into `USER CODE 2` / the `while(1)`
loop in `USER CODE WHILE`).

## 2. Wire protocol (MCU → PC)

Every streamed frame:

| Offset | Bytes | Field                                    |
|--------|-------|-------------------------------------------|
| 0      | 2     | Sync = `0xAA 0x55`                        |
| 2      | 1     | Sequence number (wraps 0–255)              |
| 3      | 2     | Sample-pair count N (little-endian u16)   |
| 5      | N*4   | N × (ch0 u16 LE, ch1 u16 LE), 12-bit right-justified |
| 5+N*4  | 1     | Checksum = XOR of all payload bytes        |

PC → MCU commands (single bytes, sent over the same CDC endpoint):
- `'S'` — start streaming
- `'X'` — stop streaming / reset buffers
- `'R'` followed by 2 bytes (u16 LE) — set timer ARR directly (sample period
  in timer ticks), acked with `'r'` + the value actually applied

The sequence number lets the PC side detect dropped frames (a gap in the
sequence = a USB stall the buffer swap didn't recover from) without needing
any retransmission logic in firmware — worth surfacing as a "dropped
frames" counter in the app rather than hiding it.

## 3. Known limits to keep in mind

- **VCAP1**: before you flash this, confirm there really is a 2.2 µF cap on
  VCAP1 in the fab'd board — I couldn't fully confirm this from the
  schematic export. Missing it means the chip may brown out under load
  even though it appears to boot at idle.
- **Flash-based capture** isn't included here since you're going with live
  streaming; if you want an offline "single burst capture, no PC needed"
  mode later, that's a separate firmware path (SRAM ring buffer + write to
  internal flash only on an explicit "save" trigger, never continuously,
  to protect flash endurance).
