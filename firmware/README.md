# Fakefish playback firmware

Two Teensy 4.1 dipole stimulators built from **one shared core**, plus a standalone bring-up
diagnostic. This file documents the hardware they share and the design of each layer; the
project overview is in the [top-level README](../README.md).

> ### ⚠ Read this before powering anything
>
> Both devices drive a **36 V** output stage. Because the eel EOD is monophasic, only **one**
> bridge ever drives while the other is braked hard to 0 V — so a discharge reaches **at most
> ~36 V**, the rail. (It is *not* ~72 Vpp: that would need a biphasic signal driving both
> bridges in opposite directions at the same instant, which this device never produces.) A
> `0.90`-level volley pulse is roughly **32 V** and a `0.45` localization pulse roughly **16 V**,
> open-circuit at nominal rail. Earlier revisions of this device ran a ~5.7 Vpp direct-pin
> stage — that stage is **retired**, and firmware built from this tree must **not** be flashed
> onto that old hardware. Set the absolute level on a scope, with `MASTER_GAIN`, before
> anything goes in water near an animal. Flashing and scoping are bench steps and are the
> owner's, never an agent's.

---

## Three layers

| Layer | Owns | Files |
|-------|------|-------|
| **L1 — output HAL** | one signed `int16` → two DRV8871 half-bridges: pins, carrier, noise shaping, the braked idle, boot order | `eel_core/config.h`, `eel_core/out_hal.h` |
| **L2 — sample producers** | what the samples *are* | `eel_core/eel_player.{h,cpp}`, `sd_player.h`, `locgen.h`, `eel_stimuli.{h,cpp}` |
| **L3 — control surfaces** | what the *operator* does, and the ISR that ties L2 to L1 | `eel_fakefish_button/`, `eel_fakefish_rc/` |

`firmware/eel_core/` is the **single source of truth**. Arduino has no include path outside a
sketch folder, so each sketch carries a **committed copy** at `<sketch>/src/eel_core/`, made by
`firmware/sync_core.sh`. Edit the canonical core, run `make sync`, commit both. Never edit a
sketch's copy — `tests/test_firmware_sync.py` fails if one drifts, and your edit is overwritten
on the next sync.

```
firmware/
  eel_core/                    canonical shared core (edit HERE)
    config.h                   L1  pins, carrier, sample clock, LED, AMP_DEBUG
    out_hal.h                  L1  out_begin / out_write / out_brake / out_silence / shape
    stim_levels.h              GENERATED from shared/stim_constants.json — do not edit
    eel_player.{h,cpp}         L2  overlap-add engine (marker + volley)
    eel_stimuli.{h,cpp}        L2  generated stimulus library (byte-frozen)
    sd_player.h                L2  SD WAV streaming runtime
    locgen.h                   L2  live localization scheduler
    host_test/                 shared host self-tests
  eel_fakefish_button/         L3  hand-held 6-button SD player
  eel_fakefish_rc/             L3  boat RC + panel, live synthesis
  rc_input_test/               standalone RC bring-up diagnostic (no core)
  sync_core.sh                 eel_core -> each sketch's src/eel_core
```

## Build and flash

Open the sketch folder in the Arduino IDE (Teensyduino), select **Tools → Board → Teensy 4.1**,
and upload. Everything the sketch needs is inside its folder — nothing to install, no Python,
no dataset.

| Device | Sketch folder |
|--------|---------------|
| hand-held SD player | `eel_fakefish_button/` |
| boat RC + panel unit | `eel_fakefish_rc/` |
| RC bring-up diagnostic | `rc_input_test/` |

The Arduino build compiles every `.cpp` in the sketch root and recursively under `src/`, and
**ignores** other subdirectories — which is why `host_test/` is safe to keep alongside, and why
each sketch's `_amalgam.cpp` (the syntax-check harness) lives in `host_test/` and never in the
sketch root.

---

## Output stage — two DRV8871 drivers on a 36 V rail, complementary drive

`out_write(int16 s)` **sign-splits** the polarity-applied sample across two single-bridge
drivers (`+` → board A, `−` → board B): a monophasic pulse drives **one** board while the other
is braked, and which board leads encodes polarity. The result is bipolar across the dipole.

Each board is a **true complementary pair** — four Teensy pins, not two:

| Board | IN1 (held HIGH) | IN2 (100 kHz PWM) | Electrode |
|-------|-----------------|-------------------|-----------|
| A (`+` phase) | pin **2** | pin **0** | A (on OUT1) |
| B (`−` phase) | pin **3** | pin **1** | B (on OUT1) |

```
  Teensy pin 2 ── DRV8871 A IN1 (held HIGH) ──┐
  Teensy pin 0 ── DRV8871 A IN2 (100 kHz PWM) ┴─ OUT1 ──[220Ω]──┬──[220Ω]──┬── electrode A
                                                             [220nF]    [220nF]
                                                                │          │
                                                            star GND   star GND

  Teensy pin 3 ── DRV8871 B IN1 (held HIGH) ──┐
  Teensy pin 1 ── DRV8871 B IN2 (100 kHz PWM) ┴─ OUT1 ──[220Ω]──┬──[220Ω]──┬── electrode B
                                                             [220nF]    [220nF]
                                                                │          │
                                                            star GND   star GND

  OUT2 = bridge return · nSLEEP held high · IN2 is NOT grounded
```

**Why complementary (drive ↔ brake, never coast).** The electrode sits on **OUT1**, which goes
high only when `IN1 = 1`:

| IN1 | IN2 | OUT1 | State |
|-----|-----|------|-------|
| 1 | 0 | rail | **drive** |
| 1 | 1 | GND | **brake** (both low-side FETs on → OUT actively pulled to 0 V) |
| 0 | 0 | Hi-Z | coast (floats — *the old failure mode, now unused*) |

So **IN1 is held steadily HIGH and IN2 is PWM'd at the complement of the wanted duty**
(`IN2 = 255 − duty`): IN2 low → drive, IN2 high → brake, average output `= duty/255 × rail`,
linear. The previous build grounded IN2 and PWM'd IN1 alone, so the PWM low phase **coasted**
(OUT1 floating) — on the scope, a two-stage pulse decay (a fast driven decay handing off to a
slow leak-through-water tail, which no real EOD does) plus single-ended bleedthrough that a
floating node cannot filter. Braking to ground on the off phase removes both.

**Idle is an explicit brake, not a float.** `out_silence()` commands duty 0 on both boards
*and* zeroes the noise shaper; it is never "both inputs low" (that would coast). Use it — not a
bare `out_brake()` — at every gap boundary, or residual shaper error leaks stray duty into the
silence.

**Bring-up order is load-bearing** and is encapsulated in `out_begin()`: hold both IN1 HIGH
first (so each bridge's high side is defined before its IN2 modulates it), then set the carrier,
then brake. Call it first from `setup()`; never open-code it.

The DRV8871 has internal dead-time, so antiphase IN1/IN2 is shoot-through-safe with **no
software dead-time** — and here only IN2 ever toggles, with IN1 steady.

- **Carrier: 100 kHz** on the IN2 pins (both true FlexPWM). Do not revert to the 585.9 kHz
  Teensy default. `PWM_CARRIER_HZ` in `config.h` carries a `static_assert` that it stays
  ≤ 100 kHz and that full 8-bit duty is still achievable there (150 MHz FlexPWM clock ÷ 256).
- **Resolution:** 8-bit duty with first-order error-feedback **noise shaping** (~+3 in-band
  bits), running at the 50 kHz waveform sample rate — independent of the carrier, so raising the
  carrier does not touch it. Its quantisation noise is high-passed toward the 25 kHz
  sample-Nyquist, where the output filter is already 35.7 dB down.

---

## The output filter — a 2-pole passive RC per channel

Each electrode is filtered **single-ended to a star ground** by two cascaded RC sections,
220 Ω / 220 nF each. The sections are **unbuffered**, so the second loads the first and the
network is *not* two independent poles:

```
H(s) = 1 / (1 + 3·sRC + (sRC)²)        RC = 48.4 µs,  Q = 1/3 (overdamped, two real poles)
```

The middle coefficient is **3, not 2** — that is the inter-section loading. Getting this wrong
is the standard trap here, so a few consequences worth stating explicitly:

| Quantity | Value |
|---|---|
| Per-section corner `1/(2πRC)` | 3.288 kHz — **not** the −3 dB point; the network is already at exactly 1/3 (−9.54 dB) here |
| Poles | 1.256 kHz and 8.609 kHz (both real) |
| **Composite −3 dB** | **≈ 1.23 kHz** (±15%, dominated by capacitor tolerance) |
| Roll-off | −40 dB/decade |
| DC group delay | 145 µs (`3RC`) |

If the two sections *were* buffered the corner would be 2.12 kHz; treating them as two
independent 3.29 kHz poles is wrong by a factor of 2.7. Note that this mistake is **invisible at
the carrier** (buffered vs loaded differ by 0.02 dB at 100 kHz) — only the corner and mid-band
reveal it, so spot-checking the carrier number does not validate the model.

**Response** (open-circuit, ideal components):

| f | 100 Hz | 500 Hz | 1 kHz | 2 kHz | 3 kHz | 5 kHz | 10 kHz | 25 kHz | 50 kHz | 100 kHz |
|---|---|---|---|---|---|---|---|---|---|---|
| \|H\| | −0.03 dB | −0.65 | −2.19 | −5.71 | −8.76 | −13.53 | **−21.80** | −35.74 | −47.41 | **−59.35** |

### What this costs and buys

**The EOD barely notices.** That is not because the filter is gentle — it is because the eel
EOD is a slow monophasic hump, not a fast spike. Of `EOD_HV`'s energy, 50 % lies below 236 Hz,
90 % below 575 Hz and 99 % below 904 Hz, i.e. almost all of it below the corner. Pushing the
real committed waveform through the real network:

- peak **−0.52 dB**, energy-weighted attenuation −0.30 dB (93 % of energy survives)
- 10–90 % rise time 420 → 454 µs (**+8 %**), FWHM 800 → 841 µs (+5 %)
- waveform correlation ρ = 0.9988; after removing the best-fit delay and gain, a genuine
  **~5 % RMS** residual shape change (most of the apparent change is pure latency, not distortion)
- net charge preserved exactly (the network is DC-coupled, gain 1 at DC)

**The carrier gain is large.** At 100 kHz the filter is 59.4 dB down, against 15.8 dB for the
1-pole ~16 kHz design this replaced — a **43.6 dB improvement**, and 30 dB more rejection of
shaper noise at the 25 kHz sample-Nyquist. (That is the measured *effect*; the design intent is
not recorded anywhere, so this file does not claim it was the reason.)

**This filter is why the 10 kHz sine marker was retired.** At 10 kHz the network is −21.80 dB
down — a factor of 12.3, against −1.4 dB under the 1-pole design the docs used to describe. Rather
than fight it, the SD device's marker was rebuilt out of **eel pulses** (see the marker section
below), which sit in the same sub-kHz band as the stimulus and therefore lose the same ~0.5 dB.
Nothing either device emits is out of band any more.

### Caveats on these numbers

- **All figures are open-circuit.** In water the load divides against the 880 Ω total series
  resistance. Quoted as a **differential** load between the electrodes, DC gain follows
  `R_diff/(R_diff + 880)`: 2 kΩ → −3.2 dB with the corner pushed to 1.7 kHz; 1 kΩ → −5.5 dB and
  2.1 kHz. That division is larger than everything the filter does to the EOD, and it dominates
  the delivered field. *(Watch the convention: a single-ended-to-ground load of `R` is the same
  case as a differential load of `2R`.)*
- **High frequencies are nearly load-invariant** — 10 kHz moves only −21.8 → −22.7 dB from open
  circuit to a 440 Ω differential load — while the EOD band is divided down hard. That asymmetry
  used to matter for the out-of-band sine marker. Now that every emitted signal is an eel pulse the
  useful consequence is the opposite one: **marker, localization and volley all sit in the same
  band, so their level *ratios* survive the load exactly** and only the absolute level moves.
- **Capacitor DC-bias derating is unmodelled and is the most likely reason a bench measurement
  will disagree.** Each shunt cap sits at a large mean DC voltage on a single-ended 36 V rail;
  class-2 ceramics (X7R/X5R, and far worse Y5V) can lose a large fraction of their nominal
  capacitance under tens of volts of bias, which would push the real corner **above** 1.23 kHz,
  possibly well above. Check the parts' dielectric, and measure the assembled network rather
  than trusting the marking.
- Cap ESR/ESL, dielectric absorption and resistor parasitics are also unmodelled; they matter
  mainly at and above the carrier, where real rejection may be **worse** than −59.4 dB.
- The A and B networks are independent and share only the star ground, so the differential
  response equals the single-ended one **while they are matched**. Mismatch is the mechanism by
  which the common-mode pedestal (below) leaks differential; at ±10 % it is a few per cent and
  benign. The retired single bridging cap was immune to this by construction.

---

## Levels and volts

Per-stimulus levels are **baked into the WAVs** by `build_sd_card.py` for the SD device, and set
live by the amplitude control on the RC device. The levels themselves are authored in
`shared/stim_constants.json` and generated into `eel_core/stim_levels.h` — change them there and
run `make gen`, never by editing the header.

The firmware keeps exactly one knob:

| Knob | Drives | Default |
|------|--------|---------|
| `MASTER_GAIN` (button `.ino`) | every streamed sample | `1.0` (levels exactly as rendered) |
| CH6 pot / `PANEL_VOLLEY_AMP` (RC) | the volley level; localization is derived at half | `1.00` on the bench |

**Level → volts.** Amplitude `1.0` means the **full rail** — and on this device that is the whole
story, because the eel EOD is monophasic. `out_write()` **sign-splits** each sample: one bridge
drives at `|s|` while the other is braked hard to 0 V, so at any instant exactly one electrode is
above ground. A discharge therefore reaches **at most the rail, ~36 V**, never twice it. The
~72 Vpp figure earlier revisions of this file quoted would require a *biphasic* signal driving both
bridges in opposite directions at the same instant, which never happens here.

```
V_peak(differential) ≈ 36 V × amplitude          max ≈ 36 V per discharge
0.90 volley  →  ~32 V
0.50 marker  →  ~18 V
0.45 loc     →  ~16 V
```

These are **open-circuit, nominal-rail** figures — the output filter costs a further ~0.5 dB on the
pulse peak, in water the load divides them down (see above), and battery sag and the driver's drop
make them a few per cent optimistic. This is still a large step up from the retired ~5.7 Vpp
direct-pin stage and still has to be **scoped** before it goes near an animal.

> **`FULLSCALE_PULSE_PEAK_MV` is stale.** `shared/stim_constants.json` still carries `3313.0`, a
> constant derived from the retired 3.3 V rail (`3.3 V × 32767/32640`). The renderer's mV
> CLI and the `levels_nominal_mv` field in a built card's `manifest.json` are therefore roughly
> **11× low** on this hardware. Treat every mV figure the toolchain prints as belonging to the
> old stage until the rail is scoped and the constant re-derived. Fractions of full scale are
> correct and rail-independent; prefer them.

---

## Sample producers (L2)

- **`eel_player`** — the overlap-add engine. Reconstructs an item from the stored inter-pulse
  intervals and per-pulse relative amplitudes, summing pulses that out-run the EOD length.
  Pure-pull `int16`, no ISR of its own. Used for the RC device's marker and volley.
- **`sd_player`** — SD WAV streaming: header parse, ring buffer, per-button random pick. The ISR
  never touches the card; `loop()` refills the ring (single-producer/single-consumer). Used by
  the button device.
- **`locgen`** — the live localization scheduler: one EOD pulse, then silence, until the next
  onset. It can be a trivial phase counter rather than an overlap-add engine only because
  localization is ≤ 20 Hz and `LOC_REFRACTORY_SAMP` exceeds `EOD_HV_LEN`, so pulses can never
  overlap — a `static_assert` in `locgen.h` enforces exactly that.
- **`eel_stimuli`** — the generated library: `EOD_HV` (131 samples @ 50 kHz) plus 31 items.
  Byte-frozen; regenerated only by `fakefish-export`.

All produce `int16` at 32767 full scale, 50 kHz. A producer a sketch does not reference simply
links out.

---

## The two markers — different codes, deliberately not unified

Both devices tag their trials so the recording can tell playback from wild fish, and both now do it
with **eel pulses**. What differs is the **code**, because the two markers answer different
questions: the SD marker says *this is a playback*, the RC marker says *what comes next is a volley
/ a sham*. The constants carry distinct prefixes (`SD_MARKER_*` vs `PULSE_MARKER_*`) precisely
because a bare `MARKER_*` name in this repo would be a trap.

### SD device: 6 pulses at 10 Hz, alternating polarity (baked into the WAVs)

`build_sd_card.render_pulse_marker` prepends `SD_MARKER_N_PULSES` (**6**) EOD pulses at a fixed
`SD_MARKER_IPI_SAMP` (**5000 samples = 100 ms = 10 Hz**) at level `SD_LEVEL_MARKER` (**0.5**) to
every `/B`, `/C` and `/D` session. First onset to last onset is **0.5 s**; the rendered burst is
25 131 samples (0.503 s) counting the last pulse's tail. The playback structure is unchanged —
**[marker] → [per-item gap] → [stimulus]**, where the gap is the byte-frozen `STIM_LEAD_GAP_SAMP`
(50–200 ms, fixed per item at export).

Three properties make it work:

- **Alternating polarity is the detection cue.** No real eel alternates, and a localization train
  is single-polarity, so the pattern cannot be confused with biology or with the stimulus it
  introduces. 10 Hz on its own would sit inside the localization range — it is the *alternation*,
  not the rate, that identifies the burst.
- **It survives the per-press polarity flip.** The firmware negates the whole WAV at random, so the
  absolute sign is unpredictable — but the alternation is invariant under negation. A detector must
  key on the **pattern**, never on the sign.
- **It is charge-balanced**, because the pulse count is **even**: three pulses of each polarity of
  an identical waveform sum to ~0 net charge (the rendered burst's differential mean is 0 to
  floating-point precision). That replaces the zero-sum property the old sine LUT provided, and the
  codegen asserts the count stays even.

The 100 ms IPI is ~38× one `EOD_HV` (131 samples), so marker pulses can never overlap.

**Why it replaced the 10 kHz sine.** Two reasons, both plain: the 2-pole output filter attenuates
10 kHz by **−21.8 dB** (see above), and everything this device puts in the water should be made of
eel pulses. The pulse marker is **barely touched by the filter** — eel pulses are sub-kHz (99 % of
`EOD_HV`'s energy lies below 904 Hz), so it loses the same ~0.5 dB the stimulus does, in water as
well as open-circuit.

**Program A is a calibration *train*, not a tone.** 10 s of a **single-polarity** EOD train at
`SD_CAL_RATE_HZ` (**50 Hz**, `SD_CAL_IPI_SAMP` = 1000 samples) at level `SD_LEVEL_CALIBRATION`
(**0.45**) — a plain reference signal for setting gain and checking the rig. Single-polarity is
deliberate: it is not a code, and it can never be mistaken for the alternating lead-in.

### RC device: 2 or 4 pulses at 100 Hz, same polarity (synthesised live)

A short EOD burst at a fixed 100 Hz IPI, tagged by **pulse count**: `PULSE_MARKER_PULSES_VOLLEY`
(2) then the discharge, or `PULSE_MARKER_PULSES_SHAM` (4) then silence. 100 Hz sits clearly above
localization (≤ 20 Hz) and below the volley peak (~300–400 Hz). Polarity is **not** alternated —
the burst shares the single randomised polarity of the playback it precedes, so here it is the
*count*, not a pattern, that carries the information.

---

## Control surfaces (L3)

### Hand-held SD player — `eel_fakefish_button/`

Six one-shot program buttons (pins 5–10, `INPUT_PULLUP`, each to GND). One press while idle
picks a random WAV from that button's SD directory and streams it once, start to finish; presses
during playback are ignored. Polarity is randomised per press.

| Button | Pin | Dir | Plays |
|---|---|---|---|
| A | 5 | `/A` | calibration train (10 s of single-polarity pulses @ 50 Hz) |
| B | 6 | `/B` | localization session |
| C | 7 | `/C` | volley session |
| D | 8 | `/D` | localize → strike |
| E | 9 | `/E` | *(spare; empty dir self-mutes)* |
| F | 10 | `/F` | song |

Card built by `fakefish-build-card`; FAT32, directories at the root, Teensy 4.1 built-in SDIO
slot (no GPIO). LED (pin 13) solid while streaming; ~1 Hz blink means no card — it re-mounts
automatically when one is inserted. A buffer underrun emits silence and keeps the cadence rather
than stalling the ISR.

### Boat RC + panel — `eel_fakefish_rc/`

Four RC channels through a **HY-M154 PC817** opto board, OR-ed with three panel buttons so the
same binary runs on the bench with no transmitter.

| Control | Pin | Does |
|---|---|---|
| CH3 throttle | 4 | localization on/off (debounced, hysteretic) + rate 1–20 Hz |
| CH4 stick | 5 | one-shot: throw high = volley, low = sham; re-arms at centre |
| CH5 pot | 6 | localization jitter (CV) |
| CH6 pot | 7 | amplitude → volley level; localization derived at half |
| panel LOC / VOLLEY / SHAM | 9 / 10 / 11 | the same three actions |

**The PC817 inverts** — the pin is LOW while the servo pulse is HIGH, so the firmware measures
the LOW duration. The board is bare-collector: receiver signal → input `Vx`, receiver GND →
input-side `G` (this is the isolation barrier), output `Vx` → Teensy pin as `INPUT_PULLUP` (the
internal pull-up *is* the collector load), output `G` → Teensy GND. **Nothing** connects to the
Teensy 3.3 V. If widths jitter, add an external 1–2 kΩ pull-up per `Vx`.

**Calibration must be redone on any new rig.** The opto offsets every width ~300 µs low of
nominal. Flash `rc_input_test`, hold each control at both extremes, and paste the measured
values into `RC_CAL_*` in `rc_control.h`.

**Failsafe:** losing CH3 turns localization off; the trigger cannot fire without a live throw, so
signal loss can never *start* a trial. A volley already playing always runs to completion.
Presence is tracked by change-detection on a millisecond clock, wrap-safe over ~49 days.

**LED:** flash per pulse, a distinct 3-blink pattern for a fired sham (which produces no output —
it is the no-stimulus control), and a double-blink per second when the RC link is lost after
having been present.

**Concurrency:** the 50 kHz ISR is the single owner of all playback state. `loop()` only decodes
inputs and publishes targets through aligned 32-bit volatile words, latched by the ISR at pulse
boundaries. The sample clock runs at a higher priority than the RC pin ISRs so input capture can
never perturb output timing.

---

## Electrode DC (a deliberate decision)

The eel EOD is monophasic, so every pulse injects net charge, and there is **no series blocking
capacitor** anywhere in the output network (the filter is DC-coupled, gain 1 at DC). Charge is
balanced instead by **randomising polarity per playback**, which keeps the net near zero across a
session and stops the electrodes pitting. Use V4A stainless.

One consequence worth knowing: because the drive is single-ended against a star ground, any signal
that *alternates* polarity leaves a **common-mode DC pedestal** — both electrodes sit at the same
positive mean, since neither is ever driven below ground. The retired 10 kHz sine, a continuous
bipolar tone, produced a large one (~2.8 V on *both* electrodes at level 0.25). The alternating
pulse marker produces the same kind of pedestal but a tiny one: six ~2.6 ms pulses in 0.5 s is a
low duty cycle, so at level 0.5 the mean is only **~0.09 V per electrode**, with the differential
mean exactly zero — that zero *is* the charge balance.

Single-polarity playbacks (localization, volley, and the program-A calibration train) have no
common-mode pedestal at all; instead they carry a **differential** DC, e.g. ~0.68 V mean on the one
driven electrode across the 10 s calibration train. That is precisely the net charge the per-press
polarity flip exists to cancel. None of this makes a field problem on a floating battery, but it is
a real single-ended signal if the rig ever shares a ground with the recording grid.

---

## Adding a new surface

1. `mkdir firmware/eel_fakefish_<name>/`, with `<name>.ino` matching the folder name.
2. Include the core as `"src/eel_core/…"`; run `bash firmware/sync_core.sh` and commit the copy.
   Keep exactly **one** reachable `eel_stimuli.h` — a sketch-local second copy is a hard compile
   error, since `#pragma once` does not dedupe distinct files.
3. Call `out_begin()` first in `setup()`; use `out_silence()` at every gap.
4. Add `host_test/_amalgam.cpp` (two lines) so `make check` syntax-checks it, and host tests for
   whatever pure logic the surface adds.

Two slots are documented but deliberately unbuilt: a **self-test surface** (a resurrected
`drv-hwtest`) and a **de-novo-synthesis handheld** (which would reuse `locgen` without the RC
decode layer).

---

## Testing

```sh
make check          # the whole gate
```

That runs, in order: codegen and core-sync idempotence; every host self-test; a per-sketch
`arm-none-eabi-g++ -fsyntax-only -Wall -Wextra` compile; then `pytest` and `ruff`. `arduino-cli`
is not required — the syntax compile is the firmware gate, and it catches what host tests
structurally cannot (the ISR, HAL signatures, Arduino glue, include paths).

Individual host tests, if you want them one at a time:

```sh
g++ -std=c++17 firmware/eel_core/host_test/sd_player_selftest.cpp -o /tmp/t && /tmp/t
g++ -std=c++17 firmware/eel_fakefish_button/host_test/button_control_selftest.cpp -o /tmp/t && /tmp/t
g++ -std=c++17 firmware/eel_fakefish_rc/host_test/panel_control_selftest.cpp -o /tmp/t && /tmp/t
g++ -std=c++17 firmware/eel_fakefish_rc/src/eel_core/eel_stimuli.cpp \
    firmware/eel_fakefish_rc/host_test/rc_control_selftest.cpp -lm -o /tmp/t && /tmp/t
```

Each prints `OK`. The exception is `eel_core/host_test/eel_player_selftest.cpp`, which is a
sample **dumper** for diffing against the Python reference, not an assertion suite — it prints
samples and never `OK`.

## Bench bring-up checklist

Hardware is bench-owned; none of this is ever claimed done by an agent.

1. **Idle is braked, not floating.** With nothing playing, scope one electrode single-ended to
   battery GND: it must sit **hard at 0 V**, with no drift or slow leak. If it floats, IN2 is not
   actually driven — recheck the pin 0/1 wiring.
2. **Duty → volts is linear and reaches the rail.** Set `AMP_DEBUG 1` in `eel_core/config.h`,
   re-flash, and read the Serial sweep against the scope. This replaces normal operation with a
   self-contained calibration routine and never starts the sample ISR.
3. **Measure the filter**, don't trust the marking — see the DC-bias caveat above.
4. **Set `MASTER_GAIN` on the scope** before going near an animal. A discharge reaches at most
   ~36 V — the rail — so a `0.90` volley pulse is ~32 V and a `0.45` localization pulse ~16 V.
5. **Check the pulse marker** survives into your recorder: that the six alternating pulses at 10 Hz
   resolve individually and that the alternation is unambiguous against real fish. That is a
   *detection* question, not a level-budget one, and only a recording settles it (`TODO.md` §2).
6. **RC device:** flash `rc_input_test` first, confirm all four channels, and redo `RC_CAL_*`.
