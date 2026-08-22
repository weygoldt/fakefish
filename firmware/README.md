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
    out_hal.h                  L1  out_begin / out_write / out_silence / out_brake
    stim_levels.h              GENERATED from shared/stim_constants.json — do not edit
    eel_player.{h,cpp}         L2  overlap-add engine (marker + volley)
    eel_stimuli.{h,cpp}        L2  generated stimulus library (byte-frozen)
    sd_player.h                L2  SD WAV streaming runtime
    locgen.h                   L2  renders one localization pulse
    loc_rhythm.h               L2  the fitted resting rhythm (when the next one is)
    loc_model_params.h         L2  its fitted tables (GENERATED from data/)
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
| A (`+` phase) | pin **2** | pin **22** | A (on OUT1) |
| B (`−` phase) | pin **3** | pin **23** | B (on OUT1) |

```
  Teensy pin 2 ── DRV8871 A IN1 (held HIGH) ──┐
  Teensy pin 22 ─ DRV8871 A IN2 (100 kHz PWM) ┴─ OUT1 ──[220Ω]──┬──[220Ω]──┬── electrode A
                                                             [220nF]    [220nF]
                                                                │          │
                                                            star GND   star GND

  Teensy pin 3 ── DRV8871 B IN1 (held HIGH) ──┐
  Teensy pin 23 ─ DRV8871 B IN2 (100 kHz PWM) ┴─ OUT1 ──[220Ω]──┬──[220Ω]──┬── electrode B
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

**Idle is an explicit brake, not a float.** It is never "both inputs low" (that would coast).
Use `out_silence()` — not a bare `out_brake()` — at every gap boundary, or residual shaper error
leaks stray duty into the silence. `out_silence()` zeroes the shaper and brakes both bridges to
duty 0, so idle is **hard 0 V single-ended on both electrodes**, not merely differentially zero.
Do not make it conditional — see
[The driver dead zone](#the-driver-dead-zone--real-measured-and-deliberately-not-compensated).

**Bring-up order is load-bearing** and is encapsulated in `out_begin()`: hold both IN1 HIGH
first (so each bridge's high side is defined before its IN2 modulates it), then set the carrier,
then `out_silence()` to the braked idle. Call it first from `setup()`; never open-code it.

The DRV8871 has internal dead-time, so antiphase IN1/IN2 is shoot-through-safe with **no
software dead-time** — and here only IN2 ever toggles, with IN1 steady.

- **Carrier: 100 kHz** on the IN2 pins (true FlexPWM on the 4.1, FTM0 on the 3.5). Do not revert
  to the 585.9 kHz Teensy default. `PWM_CARRIER_HZ` in `config.h` carries a `static_assert` that
  it stays ≤ 100 kHz and that full 8-bit duty is still achievable there (150 MHz FlexPWM clock
  ÷ 256).
- **The pinout is chosen to be identical on a Teensy 4.1 and a Teensy 3.5**, so one wiring
  harness serves both. IN2 sits on **22/23** because pins 0/1 (used until 2026-08-21) have no
  FTM channel at all on the 3.5's MK64FX512 and so cannot carry a carrier there; 22/23 are
  FlexPWM4_0_A / FlexPWM4_1_A on the 4.1 and FTM0_CH0 / FTM0_CH1 on the 3.5, and are free on
  both control surfaces. The full derivation is in `config.h`. **An already-built 4.1 board has
  to have those two wires moved** — an un-rewired board runs, logs and blinks normally while
  emitting nothing.
- **Resolution:** 8-bit duty with first-order error-feedback **noise shaping** (~+3 in-band
  bits — measured +3.5 b at the repo's own 5 kHz band definition), running at the 50 kHz waveform
  sample rate — independent of the carrier, so raising the carrier does not touch it. Its
  quantisation noise is high-passed toward the 25 kHz sample-Nyquist, where the output filter is
  already 35.7 dB down. The **bottom** of the duty range is not usable as-is — see
  [The driver dead zone](#the-driver-dead-zone--real-measured-and-deliberately-not-compensated).

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

### A better network for the next build — three sections, same resistance

**Not built. This is a design for whenever a board is next opened up or made from scratch**, and it
is the recommended layout for the hand-held unit's 36 V stage, which does not exist yet (`TODO.md`
§1) and therefore costs nothing extra to build this way.

**The problem with the present network is its topology, not its cutoff.** The two sections are
identical and unbuffered, so the second loads the first and the pole pair splits **6.9 : 1** —
1.256 kHz and 8.609 kHz, Q = 1/3. A 2-pole's stopband is set by the *geometric mean* of its poles
(3.29 kHz here) while its passband droop is set by the *lower* one. So the network pays the droop
of a 1.23 kHz filter and collects only the stopband of a 3.29 kHz one. That gap is pure waste, and
a third section recovers most of it **at the same total series resistance and the same dissipation**.

```
 DRV8871 OUT1 ──[220Ω 2W]──┬──[110Ω]──┬──[110Ω]──┬── electrode
                        [150nF]     [100nF]    [100nF]
                           │           │          │
                       star GND    star GND   star GND
```

| position | part | why this value |
|---|---|---|
| R1 | **220 Ω**, 2 W metal oxide axial | unchanged — the *only* resistor that gets hot (see below) |
| C1 | **150 nF** film, 100 V | |
| R2, R3 | **110 Ω**, 0.25 W metal film 1 % | ≤ 7 mW each; two 220 Ω in parallel also works |
| C2, C3 | **100 nF** film, 100 V | |

Two resistor values, two capacitor values, all E6/E24, six parts per channel. **Film (MKT/MKP) is
mandatory, not a preference:** each shunt sits at a large mean DC on a single-ended 36 V rail, and
class-2 ceramic's DC-bias derating would make the filter *amplitude-dependent* — which would break
the property (invariant 4) that marker / localization / volley level **ratios** survive the chain.

**What it buys**, open circuit — which is the operating case in the poorly-conducting blackwater the
field site sits in, where the load barely damps the network at all:

| | present 220 Ω/220 nF ×2 | three-section | |
|---|---|---|---|
| rejection @ 100 kHz | −59.35 dB (24.7 mV) | **−60.71 dB (21.1 mV)** | better |
| RMS pulse shape residual | 4.77 % | **2.93 %** | −39 % |
| pulse peak loss | −0.52 dB | **−0.31 dB** | better |
| 10–90 % rise (409.6 µs true) | 454.4 µs (+10.9 %) | **440.0 µs (+7.4 %)** | better |
| FWHM change | +5.3 % | +3.2 % | better |
| group delay | 137 µs | 106 µs | better |
| −3 dB, open / into 1 kΩ | 1228 / 2117 Hz | 1644 / 2726 Hz | — |
| total series R per channel | 440 Ω | **440 Ω** | unchanged |
| worst-case dissipation in R1 | 1.47 W | **1.47 W** | unchanged |

**The one debit, stated so it is not discovered later.** Matched at 100 kHz, a three-section ladder
is *shallower* in the 10–50 kHz mid-band than the two-section it replaces (−17.6 vs −21.8 dB at
10 kHz; −30.9 vs −35.7 dB at 25 kHz), and that is where the noise shaper puts its energy. Measured
on a real 20 Hz / level-0.45 localization train through the bit-exact `shape()`: total shaper
leakage at the electrode rises **0.704 → 1.039 LSB rms (−85.9 → −82.5 dB re pulse peak)**. The
in-band 0–1 kHz part barely moves (0.198 → 0.205 LSB); all the extra sits at 5–25 kHz, an order of
magnitude above the EOD band and trivially removed in analysis. Above 200 kHz the three-section is
*better* (−78.2 vs −71.4 dB).

**Why not the obvious alternatives.**

- **Just raise the cutoff** (swap the caps for 150 nF): halves the shape error, but spends 6.6 dB
  of stopband and lands at −52.7 dB, *below* the −55 dB the recording requires. Only safe once the
  recorder's anti-alias response is measured, which it has not been.
- **Impedance-stagger the two existing sections** (R2/R1 = C1/C2 = k): this genuinely does move the
  corner 1228 → 1975 Hz at unchanged stopband — but Q → 0.5 needs large k, and total series
  resistance is R1(1+k). Keeping the resistance down instead means dropping R1, and carrier
  dissipation is `V²·d(1−d)/R1` — **independent of C**. The tidy-looking 22 Ω/2.2 µF + 220 Ω/220 nF
  version needs **21.9 W** in position 1. Thermally impossible.
- **A fourth section:** 1.08 % vs 1.14 % at Rtot 880 Ω. Three is the knee.
- **A series inductor** would beat everything here (+4.6 dB of delivered field, 21× less shape
  error, no heat), but it is a fourth part type, needs matching across channels, can peak with
  ±20 % tolerance, and is an antenna next to a preamp. Deliberately not recommended.
- **A differential bridging cap across the electrodes:** leaves +29.7 dB of unfiltered *common-mode*
  carrier (753 mV vs 24.7 mV). Decisive against a single-ended recording grid. This is why the one
  the retired direct-pin stage used was not carried over.

**Do not simply add a third RC to the untouched network.** Bolting 110 Ω + 100 nF onto the existing
220 Ω/220 nF ×2 gives −76.4 dB at 100 kHz — 17 dB nobody needs — and drags the corner down to
908 Hz, **doubling** the shape error to 7.66 %. The capacitors have to be retuned with it.

---

## The driver dead zone — real, measured, and deliberately not compensated

**The physics is real. The compensation was worse than the problem.** Keep the first half of this
section; do not reintroduce the second.

**The mechanism.** `analogWrite()` maps duty `q` to an IN2-LOW (= DRIVE) time of `(q+1)/256` of the
carrier period. At 100 kHz that period is 10 µs, so:

| duty code | 0 | 1 | 4 | 8 | 11 | 21 | 32 |
|---|---|---|---|---|---|---|---|
| commanded drive pulse | 39 ns | 78 ns | 195 ns | 352 ns | 469 ns | 859 ns | 1289 ns |

The DRV8871 needs a minimum input pulse width to respond at all — **400 ns typical, 800 ns
guaranteed** (SLVSCY9B, Recommended Operating Conditions footnote 1). The bottom codes are
therefore in a region the datasheet does not specify, and the flanks and the negative
pre-potential of every EOD sit exactly there. On worst-case silicon the pulse would be hollowed
out from the bottom as the amplitude control came down, changing timbre rather than simply getting
quieter, and going silent below roughly one-sixteenth of full scale.

### What was tried, and what the bench said

Between 2026-08-21 and 2026-08-22 (commit `3af9f33`, reverted) both boards were held at
`OUT_PEDESTAL_DUTY = 21` — the datasheet-guaranteed 800 ns bound — whenever the device was armed to
emit, with the signal scaled into the codes above it. On paper the offset was identical on both
electrodes, so it was pure common mode and cancelled across the dipole.

On the hardware it was decisive in the other direction. **Teensy 3.5, pins 22/23, 36 V, the built
2× 220 Ω/220 nF filter, one variable, both ways:**

| `OUT_PEDESTAL_DUTY` | result |
|---|---|
| **21** | output-filter wiring runs **hot**; the signal in water is a **cacophony** |
| **0** | clean pulses, no noise, and **the amplitude pot works to the bottom of its travel without cutting out** |

That last cell is the whole finding: **the dead-zone shutoff does not reproduce on this hardware.**
These DRV8871s evidently sit near the 400 ns *typical* figure rather than the 800 ns guarantee. The
pedestal was justified against worst-case silicon plus an ear observation made on a **different
output path** — Teensy 4.1 with IN2 on pins 0/1, which are FlexPWM1 *X-channel* pins — and was
never verified on the path it shipped to.

### Why it was so destructive

Idle stopped being a hard brake. Both bridges switched a 36 V square **continuously** — through
every silence of a localization train, which is over 98 % of it — instead of sitting at 0 V. Two
consequences, both new:

- **~0.47 W per channel in the first filter resistor**, continuous, where there had been
  essentially none between pulses. `P = V²·d(1−d)/R1` with `d = 8.667 %` on the 3.5 (FTM0_MOD 599
  → cval 548 → 52/600 counts of drive).
- **Both electrodes sat at ~3.1 V DC** plus carrier residue. The filter is series-R / shunt-C, so
  it passes DC untouched — and the recording model here is **single-ended** (see the carrier
  discussion in `config.h`). "Common mode" is not "silent" for anything listening in that water.

### Do not reintroduce it, and do not try a smaller one

The A/B proves the pedestal caused both symptoms; it does not prove which path carried the heat.
The arithmetic says the first series resistor must dissipate 24–1100× more than the ground return
at *every* value of R1, so either R1 was the hot part and the wiring was warm by conduction, or
current was bypassing R1 through the bridge — cross-conduction at the 867 ns detection knee, which
is unspecified silicon behaviour rather than a spec violation. **A smaller pedestal moves closer to
that knee, not further from it.**

Any future attempt has to solve the common-mode problem first — the electrodes must return to 0 V
between pulses — not merely pick a gentler number.

`OUT_PEDESTAL_DUTY`, `OUT_SIGNAL_DUTY_MAX`, `out_scale_to_headroom()`, `out_arm()`,
`out_disarm()`, `out_armed()`, `out_idle_duty()` and `shape()`'s `qmax` parameter are **gone** — do
not reintroduce those names. `out_silence()` is once again an unconditional brake on both bridges,
and that is load-bearing: it is the state the hardware is known good in.

### Measuring a board's real threshold

`AMP_DEBUG` is retained — it is the right tool for this and is independent of any compensation
scheme. If the question ever comes back on different silicon:

- Set `AMP_DEBUG 1` in `eel_core/config.h` and change `AMP_DEBUG_SWEEP_LEVELS` to walk the
  *bottom* of the range finely — `{ 0, 1, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 21, 24, 32 }`. The
  routine bypasses the noise shaper and prints the commanded duty, so a scope reading maps straight
  onto the number the firmware sent.
- Probe one electrode single-ended to battery ground. Find the lowest duty code that produces any
  output at all, and the lowest code from which output is linear in duty.
- While there, read the intercept of the duty→volts line: `t_DEAD = 220 ns` is 2.2 % of the period,
  worth up to 5.6 duty codes of systematic offset, and it is not modelled anywhere in this chain.

The pure half of the mapping is host-tested by `eel_core/host_test/out_hal_selftest.cpp`, which
sweeps the real `EOD_HV` at every level the devices use and asserts that **a zero sample brakes
both boards to duty 0** — the regression guard for exactly the failure above.

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
| CH6 pot / `PANEL_VOLLEY_AMP` (RC) | the volley level; localization is derived at a quarter | `1.00` on the bench |

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
- **`locgen`** — renders one localization pulse: one EOD, then silence, until the next onset.
  It can be a trivial phase counter rather than an overlap-add engine only because localization
  is ≤ 20 Hz and `LOC_REFRACTORY_SAMP` exceeds `EOD_HV_LEN`, so pulses can never overlap — a
  `static_assert` in `locgen.h` enforces exactly that.
- **`loc_rhythm`** — decides *when* that next onset is: the **fitted resting rhythm**, see
  [The resting rhythm](#the-resting-rhythm-a-fitted-model-not-a-random-draw) below. Paired with
  `locgen` but deliberately separate, so the rhythm can be tested against the Python reference
  with no notion of a waveform.
- **`eel_stimuli`** — the generated library: `EOD_HV` (131 samples @ 50 kHz) plus 34 items.
  Byte-frozen; regenerated only by `fakefish-export`.
- **`pulse_log`** — the **mirror image of `sd_player`**: where that one has the ISR *pop* samples
  `loop()` read from the card, this one has the ISR *push* event records that `loop()` writes to
  the card. Same rule, same lock-free SPSC ring, same pure/glue split. Used by the RC device; the
  button device gets the file for free but does not reference it yet. See
  [Pulse logging](#pulse-logging).

All produce `int16` at 32767 full scale, 50 kHz. A producer a sketch does not reference simply
links out.

---

## The resting rhythm — a fitted model, not a random draw

Between trials the RC device ticks along like a resting eel. Since **2026-08-21** that rhythm is
a model **fitted to 99 010 measured resting intervals** (29 h of single-eel stretches, 1 761
slices, 42 recordings, 15 sites of FLONA 2025) rather than the unit-mean lognormal draw it used
before. `firmware/eel_core/loc_rhythm.h` is the port; `docs/LOCALIZATION_GENERATIVE_SPEC.md` is
the spec.

### What was wrong with the lognormal

It is a **renewal process**: every interval drawn independently. That fixes the mean rate and
nothing else — consecutive log-intervals are uncorrelated at every lag. A real eel's correlate
**0.55 at lag 1, 0.36 at lag 5 and still 0.21 at lag 20**. Its discharge rate *wanders*, over a
few seconds and over a minute, and that wander is the one thing a renewal process cannot produce.
It is also the thing that makes a recording sound like a fish rather than a random number
generator.

### The model

```
z   = randomness * (offset + x_fast + x_medium + x_slow + sd_white * e)
IPI = Q(z) / tempo
```

`Q` is a 41-knot measured interval quantile table. Each `x` is an Ornstein–Uhlenbeck component
that relaxes in **wall-clock time** — `x ← a·x + sd·√(1−a²)·e` with `a = exp(−dt/τ)`, τ = 2.5 s,
96 s and 62 min. Five floats of state; three `expf`, two Box–Muller pairs and eight PRNG words
per interval, paid on the one tick that closes an interval, not per sample.

Four properties are **measurements, not modelling choices**, and none should be simplified away:

1. **The state relaxes in time, not in pulse count.** After a 3 s silence the fish has forgotten
   more than after three 100 ms intervals; refitting per-pulse costs 699 nats of likelihood.
2. **Two timescales, plus a slow one and a per-power-on offset.** One is audibly not enough
   (619 nats).
3. **The noise is peaked, not Gaussian** — a two-scale mixture. Gaussian noise of the same
   variance gets the interval distribution *and* the autocorrelation right and still sounds
   wrong: median CV2 0.60 against a measured 0.38.
4. **Long silences are real.** 1.5 % of resting intervals exceed 5 s and 0.6 % exceed 10 s; the
   table's top knot is 32 s. The device *will* occasionally go quiet that long. The pulses
   bracketing a real 30 s silence are exactly as loud as the rest of the recording, so the fish
   did not swim away — it stopped. The retired `RC_IPI_MAX_FACTOR`, which clamped the lognormal's
   tail at 4× the mean, is gone on purpose. **Do not clamp it back.**

### The two knobs, and what changed about them

| | old (lognormal) | now (fitted) |
|---|---|---|
| CH3 | mean pulses/s, 1–20 Hz linear | **tick tempo** (1/median), 0.5–20 Hz **logarithmic** |
| CH5 | jitter CV, 0–0.8 | **randomness**, 0–1.5 (1.0 = the measured eel) |

**The CH3 ladder is logarithmic**, so every rung is the same *ratio* from the next (~21 % at 20
steps over 0.5–20 Hz) rather than the same number of Hz. It was linear 1–20 Hz until 2026-08-22,
which spent about 85 % of the throttle's travel above anything a real eel does and squeezed the
whole biological range into the bottom rungs — and put 1 Hz inside the ~6 % of travel between the
fail-safe cutoff and the enable threshold, so the slow settings were effectively unreachable. A
field log confirmed it: the operator never got below 4 Hz. Geometric spacing gives six rungs below
1.5 Hz where the old ladder had one, ~0.1 Hz steps near 1 Hz, and lands the measured eel at almost
exactly mid-throttle (√(0.5 × 20) = 3.16 Hz against a nominal 3.153).

**CH3's meaning changed and the number did not.** A setting of 5 Hz used to mean five pulses per
second on average; it now means the fish *ticks* at 5 Hz, which at randomness 1.0 delivers about
3.3 pulses/s. Both are right — the interval distribution is heavy-tailed, so one long silence
contributes a single interval to the median while eating half the average. A heavy-tailed
distribution cannot hold its median and its mean at once, and this device **anchors the median**,
because that is the number quoted as an eel's discharge rate and the only one that keeps a
throttle labelled in Hz honest. (`knobs.gain_mean_anchor` in the model JSON is the table that
would restore the old meaning; spec §5.3 has the argument.)

Rate is a **pure time dilation**: it divides the intervals *and* stretches the time constants
with them. That is measured — refitting the memory as `τ·(own tempo)^γ` peaks sharply at γ = 1
and beats γ = 0 by 413 nats. Halving the rate with τ fixed instead would let the state relax
between pulses, so the wander washes out and the device drifts back toward a renewal process
exactly at the slow settings, which is where it matters most.

Randomness scales the state score. It does **not** blend toward Poisson — the lag-1
autocorrelation stays around 0.52 across the whole range. It dials how *much* the rate varies,
leaving how it varies over time alone, so every setting still sounds like a fish.

### What is deliberately not implemented

The model also carries `interrupt()`: the fish's own decision to launch a fast run, about **1 in
49 resting pulses**. It is **not** ported. This surface runs a blinded trial design in which a
marker's pulse count *is* the trial record, so a spontaneous burst would be an unmarked volley in
the water, and one landing inside a sham would destroy the no-stimulus control. Every volley this
device emits is operator-requested. The generated header carries no hazard constants at all, and
`tests/test_loc_model.py` asserts that, so it cannot be wired up by accident.

The model also does not carry the **diel cycle** (real eels tick ~1.6× faster in the early night
than at midday) or any notion of other fish present. Both are in the spec's caveats.

### Why the parameters are generated, and the one trap to know about

`firmware/eel_core/loc_model_params.h` is **generated** from `data/loc_model_params.json` by
`fakefish-gen-constants`, alongside `stim_levels.h`. That is not ceremony. The spec ships a
ready-to-paste C listing in its §7, and **that listing disagrees with the fitted parameters it
sits beside** — `TAU[0]` 3.200 against a shipped `tau_fast_s` of 2.5, `HAZ_B0` −4.872 against
−4.8118, and both mixture widths off in the third decimal. The JSON is the self-consistent one:
its interval table was calibrated *at* τ = 2.5, and running the reference sampler with it
reproduces the spec's own §6 validation column, while substituting 3.2 moves the median interval
off the measured 317 ms. Anyone hand-typing the listing would get a device that is subtly not the
model, with nothing to catch it.

### How it is verified

Two golden-file gates, facing opposite directions, so neither side can move alone:

- `tests/data/loc_rhythm_golden.csv` is **generated** by `src/fakefish/loc_model.py` — the
  vendored reference — with its noise draws *injected* rather than drawn, so the two
  implementations can be compared state for state despite sharing no generator. Each row carries
  the four noise values one interval consumes and the state the reference reached.
- `tests/test_loc_model.py` fails if the golden drifts from the Python. `loc_rhythm_selftest`
  (run by `check.sh`) fails if the C drifts from the golden. Five knob settings are covered,
  including both ends of the interval table's clamp.

Regenerate the golden with `uv run python tests/test_loc_model.py --emit`.

### Both devices, one model

The SD/button device uses it too. Its localization WAVs render from the localization *items* in
the library, and those items are now drawn from the same fitted rhythm — one draw per rung of the
`LOC_SYNTH_RATES_HZ` tempo ladder, generated in `synthetic_volleys.py` and baked in by
`fakefish-export`. There is no second way to generate a localization pulse in this repo.

Two things had to move to let the model through unbent, and both are worth knowing:

**The library's IPI field is `uint32` (format v4).** It was `uint16` — a ceiling of 65535 samples,
1.31 s — which was not a tail limit but a core-of-the-distribution one: at the 1 Hz rung 35 % of
the model's intervals are longer than that, and even at 10 Hz 1.9 % are. Clamping would have
deformed every item at every rung, so the format holds the model rather than the model being bent
to fit the format. It costs +2 bytes per pulse; the packet is 58.7 kB against a 131 kB budget.

**A finite item cannot hold a labelled tempo, so each train is time-scaled onto its rung.** The
model's components relax over 3 s, 96 s and 62 min, so a 60 s item — of which program B renders
only the first 20 s — is far too short to average the slow ones out. Left alone a "5 Hz" rung
realises anywhere in 1.8–10.3 Hz and the rungs come out in the wrong *order*. Since rate is a pure
time dilation, scaling every time in a drawn train by one factor is exactly what the rate knob
does, so the rung becomes exact by construction while CV2 and the autocorrelation — both
scale-invariant — are untouched.

One consequence of the rung being a *tempo*: a 3 Hz rung delivers about 1.9 pulses/s, not 3, and
the 1 Hz rung gives program D's 5 s lead only ~2 pulses. That is a 1 Hz fish with the model's tail,
not a defect; pick a faster rung if program D needs a denser lead.

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
localization (≤ 20 Hz) and below the volley peak (~330 Hz sustained). Polarity is **not** alternated —
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
| CH3 throttle | 4 | localization on/off (debounced, hysteretic) + **tick tempo** 0.5–20 Hz, log ladder |
| CH4 stick | 5 | one-shot: throw high = run one **blinded trial**; throw low does **nothing**; re-arms at centre |
| CH5 pot | 6 | localization **randomness** (0 = metronome, 1.0 = the measured eel, to 1.5) |
| CH6 pot | 7 | amplitude → volley level; localization derived at a quarter |
| panel LOC / VOLLEY / SHAM | 9 / 10 / 11 | the same three actions |

**The PC817 inverts** — the pin is LOW while the servo pulse is HIGH, so the firmware measures
the LOW duration. The board is bare-collector: receiver signal → input `Vx`, receiver GND →
input-side `G` (this is the isolation barrier), output `Vx` → Teensy pin as `INPUT_PULLUP` (the
internal pull-up *is* the collector load), output `G` → Teensy GND. **Nothing** connects to the
Teensy 3.3 V. If widths jitter, add an external 1–2 kΩ pull-up per `Vx`.

**Calibration must be redone on any new rig.** The opto offsets every width ~300 µs low of
nominal. Flash `rc_input_test`, hold each control at both extremes, and paste the measured
values into `RC_CAL_*` in `rc_control.h`.

**Blinded trials.** The CH4 lever says *"run a trial"*, not *which* trial. The firmware draws
volley-vs-sham itself (`TRIAL_P_VOLLEY`, default 0.5) inside the sample-clock ISR at the moment
of playback, so the operator cannot choose the trial type and their timing and position cannot
correlate with it. Throwing the lever the other way is completely inert — it cannot fire, and it
does not even consume the arm. The three-button bench panel keeps **explicit** volley and sham
buttons, because bench testing needs determinism; blinding is a property of the field instrument.

The draw lives in the ISR rather than in `loop()` for a concrete reason: `random()` is already
called from the ISR (polarity, the volley item pick) and must stay single-caller. Drawing at
playback time also means a request that is later discarded never consumes a draw.

The localization rhythm is deliberately **not** on that stream — it runs its own generator.
Sharing one would make the blinded volley/sham sequence depend on how many localization pulses
happened to precede a throw, so the operator's rate knob would silently reach into which trials
came out volleys.

This blinds the *choice*, not the *record*: the LED still shows its distinct sham pattern
afterwards, and the marker's pulse count still tags which trial fired — analysis needs that.

**Failsafe:** losing CH3 turns localization off; the trigger cannot fire without a live throw, so
signal loss can never *start* a trial. A volley already playing always runs to completion.
Presence is tracked by change-detection on a millisecond clock, wrap-safe over ~49 days.

**LED:** flash per pulse, a distinct 3-blink pattern for a fired sham (which produces no output —
it is the no-stimulus control), a double-blink per second when the RC link is lost after having
been present, and — outranking all of them — a steady **inverse blink** (on, with a brief dark
notch each second) when logging has failed and output is therefore suppressed. That one is
deliberately the only *inverted* pattern in the vocabulary; every other is a short flash on a dark
background, and a blocked device otherwise looks exactly like an idle one.

**This device now needs an SD card.** It still needs no *WAV card* — it live-synthesises
everything — but it writes a per-pulse log and **will not stimulate without one**. See
[Pulse logging](#pulse-logging).

**Concurrency:** the 50 kHz ISR is the single owner of all playback state. `loop()` only decodes
inputs and publishes targets through aligned 32-bit volatile words, latched by the ISR at pulse
boundaries. The sample clock runs at a higher priority than the RC pin ISRs so input capture can
never perturb output timing.

---

## Pulse logging

The RC device writes **one row per emitted pulse** to its SD card, stamped with the exact 50 kHz
sample tick that placed it. Core: `eel_core/pulse_log.h`. Reader: `fakefish-pulse-log`
(`src/fakefish/pulse_log.py`).

### Why

Volley and sham **trials** are already identifiable in a recording — the count-coded marker tags
them (2 pulses volley, 4 sham). The **localization train is not**. It is single-polarity EOD
pulses from a rhythm **fitted to real resting eels**, and since 2026-08-21 it is a closer forgery
than it ever was — it reproduces the wander that used to be the one giveaway. Nothing in the
water distinguishes it from biology. Without a log, every analysis of a
recording made during playback has to treat an unknown subset of pulses as possibly ours — and
localization is the thing that runs *continuously* for a whole session.

Second reason: the trigger is **blinded** — the firmware draws volley-vs-sham in the ISR and the
operator does not know which fired. The marker records that in the water; the log records it on
the card. Two independent records of the same fact, on purpose.

### Logging is a precondition for output

**No working log, no stimulation** — at boot *and* mid-session. An unlogged localization pulse is
indistinguishable from a real fish, so emitting one silently poisons the recording; refusing is the
lesser failure.

What "blocking" means precisely:

- **Nothing in flight is ever truncated.** A marker, volley or sham already playing runs to
  completion, exactly as an RC link loss never aborts one. A half-played volley is not "no data",
  it is an artefact that looks like data. Only playback *starts* are gated, plus localization
  stopping at a clean pulse boundary.
- **A failure is announced, loudly.** The LED switches to the inverse blink described above.
- **Recovery is automatic and explicit.** `loop()` retries the card every 2 s. On success it opens
  a **new** indexed file — never reopening the interrupted one, whose tail length is unknowable —
  writes the full header again, and records a `GAP` row naming the file that was cut short.
- **A throw made while blocked is discarded, not queued**, so a stale request cannot fire without
  warning the moment the card recovers.

### Files: indexed, never overwritten

`/LOGS/PULS0000.CSV` … `PULS9999.CSV`, 8.3-safe, one file **per power-cycle**, opened **at boot**.

- Indexed rather than dated **because the RTC may reset**: with a dead coin cell every boot would
  produce the same date-based name and collide. The RTC time is recorded *inside* the file instead.
- Opened at boot rather than lazily, because that proves the card is **writable** (a bare
  `SD.begin()` mount probe does not) and moves the first write failure to boot — where a card swap
  is cheap — instead of to the first trial you were waiting for.
- The next index is always **highest existing + 1**, never lowest-free, so the index orders the
  sessions even after files are removed.
- A subdirectory, because a FAT32 **root** directory is capped at 512 entries.
- Exhausting `PULS9999` is treated as a logging failure (fault LED, no output) rather than
  wrapping — wrapping would overwrite, and never-overwrite is the point. Teensyduino also stamps
  FAT timestamps from the RTC, a free second channel for session order.

### The time base is the sample counter, not the RTC

Rows carry a **64-bit sample tick**: 20 µs resolution, and by construction the same clock that
placed the pulse. The Teensy RTC has 1 s resolution, so it would be both far coarser and more
expensive per pulse. Absolute time comes from periodic `ANCHOR` rows (every 10 s) that pair a tick
with an RTC reading; `PulseLogFile.absolute_time()` fits through them, recovering wall-clock far
more precisely than any single reading.

This makes the design **RTC-optional by construction**: with no coin cell you lose the absolute
anchor and keep exact relative timing. Nothing else changes. The anchor also proves the device was
*alive* through a quiet stretch — otherwise "idle" and "died" look identical after a power-cut
truncation.

> The log tick is a **separate** counter from the sketch's `g_tick`. `g_tick` stays `uint32_t`
> because the LED code relies on 32-bit wrap arithmetic; a log spanning more than 23.9 h needs one
> that does not wrap.

### Overflow is recorded, never silent

If the ring fills — realistically only when an SD block-erase stalls `loop()` during a volley
burst — the ISR counts the losses and the next successful push emits a `DROP` row carrying the
exact count, stamped with the tick at which the stream resumed. **A log that quietly omits pulses
turns into wrong analysis; one that admits a gap turns into a caveat.** The ring is sized for
latency, not bandwidth: 512 records absorbs ~250 ms of stall at the worst burst rate (~400 Hz),
against a steady rate of ~1 kB/s at 20 Hz localization.

### File format

A `#key=value` header block, then a comment column line, then the bare column line, then rows.
`pandas.read_csv(path, comment='#')` works directly, as does the bundled reader.

| Column | Meaning |
|---|---|
| `seq` | monotonic row counter within the file — a break means the file was torn or edited |
| `tick` | 64-bit sample counter (÷ `sample_rate_hz` for seconds of device time). **Empty on `GAP`**, which `loop()` writes and which therefore has no reading of the ISR-owned counter — `BOOT`'s tick 0 is real |
| `event` | `BOOT` `LOC` `MARKER` `VOLLEY` `TRIAL` `SHAM` `LOCON` `LOCOFF` `LINK` `ANCHOR` `DROP` `GAP` |
| `item` | library item index — **empty when the pulse came from no item** |
| `pulse` | index of this pulse within its item (`MARKER`, `VOLLEY`) |
| `trial` | trial id, tying a trial's `MARKER` rows to its `VOLLEY` rows or its `SHAM` row |
| `pol` | playback polarity, +1 / −1 |
| `amp_m` | amplitude applied to **this** pulse, ×1000 — for a `VOLLEY` row this includes the item's per-pulse envelope, so it decays down the burst while `master_m` holds |
| `master_m` | the master (volley) amplitude setting in force, ×1000 |
| `rand_m` | localization **randomness** knob in force, ×1000 (0 = metronome, 1000 = the measured eel) |
| `tick_ipi` | nominal **median** localization IPI in whole samples — the tick tempo, not the mean |
| `val` | event-specific: `DROP` records lost, `LINK` 1 = up, `ANCHOR` RTC unix seconds, `GAP`/`BOOT` file index |
| `req` | trial **requested**: `R` = blinded lever, `V`/`S` = explicit panel button |
| `res` | trial **resolved**: `V` / `S` — what the firmware actually drew |

Three properties of the schema are load-bearing:

- **An empty column means "not applicable", never zero.** For `item` this is critical:
  `STIM_ITEMS[0]` is a *real recorded volley*, so a `0` default would silently attribute every
  localization and marker pulse to it. `-1` is no better as a written value — `STIM_ITEMS[-1]`
  does not raise in Python, it quietly returns the last item. Hence: empty.
- **Every row carries the settings in force at that instant**, rather than delta-encoding them.
  The data cost is negligible and the realistic field failure is a file *truncated by power loss*;
  with delta encoding a torn head makes everything after it ambiguous.
- **`req` and `res` are both recorded.** Only the pair distinguishes a genuinely blinded trial
  (requested `R`) from a bench-forced one (requested `V`/`S`). With the outcome alone, a bench test
  silently contaminates the trial set.

`item` + `pulse` together also buy a real integrity check: look up the *expected* IPI in the
library and compare it against the logged tick deltas. That confirms the engine emitted what it
was told to, and makes a partial volley obvious rather than looking like a short one. The volley
item index is recoverable from **nowhere else** — not from the marker, not from the settings, and
from a recording only by matching the IPI sequence against all 18 candidates.

### Aligning a log to a recording

The log alone **is** sufficient to align — this is the intended procedure:

1. The log gives pulse times as sample indices in Teensy time (exact, 20 µs).
2. Detect pulse times in the recording.
3. Cross-correlate the two point processes to find the offset.
4. Every logged pulse then maps to a recorded pulse; everything unmatched is a real fish.

**The rhythm's irregularity is what makes this work.** A perfectly periodic train would correlate
ambiguously — an equally good peak at every IPI. A train whose intervals vary gives any stretch a
unique fingerprint and a sharp peak, and the fitted rhythm varies *more* than the lognormal it
replaced (median CV2 ~0.42 at randomness 1.0, and a heavy tail that the old 4×-mean clamp used to
cut off), so alignment got better rather than worse. The randomness that makes the stimulus
biologically realistic also makes it uniquely identifiable. Detection precision is not the limit:
an EOD is ~800 µs FWHM, so each peak localises to well under a millisecond.

**Rate is only controllable on average, and less so as CH5 rises.** The state relaxes in
wall-clock time, so when the rhythm wanders to the fast side the intervals shorten, which means
*less* relaxation per pulse, which keeps it fast. Over tens of thousands of pulses it averages to
the commanded tempo; over a two-minute window at randomness 1.5 the realised rate can sit several
times off it and feel stuck. Measured in the field (2026-08-22): commanded 4 Hz at randomness 1.5
produced a run that opened correctly at 1940/259/133/211 ms, took a genuine 4.3 s silence, then
collapsed to a near-constant 38 ms for fifty pulses. That is the model, not a fault — the same
short-window scatter that makes a synthetic localization item need time-scaling onto its rung. If
an experiment needs the *rate* controlled rather than the texture, run CH5 low.

**What CH5 at 0 actually costs.** An exact metronome has no fingerprint — sliding the log by any
whole interval fits as well as the true offset, so the localization train stops being usable for
alignment. It is not fatal: each trial's coded marker burst is short and distinctive, so it still
gives a sharp unambiguous anchor, and the drift can be interpolated between those anchors. So the
real cost scales with how often you fire: frequent trials, workable; long quiet stretches between
them, and you are interpolating drift across a gap with nothing to check it against. The better
reason to keep CH5 near 1.0 is simply that 0 is not a fish — no eel discharges at a fixed
interval. Use 0 when a predictable train is the point of the test.

> **The one real constraint is clock drift.** The Teensy's 50 kHz clock and the recorder's 48 kHz
> clock are independent crystals. Two parts at ±20 ppm can differ by ~40 ppm ≈ **144 ms per hour**.
> Unambiguous per-pulse assignment needs alignment inside half an inter-pulse interval — at the
> 20 Hz maximum that is ±25 ms, which 40 ppm consumes in **about 10 minutes**. So a single global
> offset is fine for a short recording and degrades over a session: fit **offset + rate** (two
> parameters), or cross-correlate in windows and track the offset. Crystal drift is near-constant
> over minutes to hours, so a linear fit usually suffices; go piecewise if residuals grow. The
> nominal rates are exactly commensurate (50 : 48 = 25 : 24), so there is no awkward resampling
> ratio — only ppm-level crystal error. Always report a **match quality** (what fraction of logged
> pulses found a recorded pulse within tolerance): it is both the confidence measure and the signal
> that the device was out of range, or that the card and the recording came from different sessions.

Markers are **useful redundancy, not a requirement** — a 2-or-4 pulse burst at exactly 100 Hz is a
short distinctive anchor that independently confirms volley vs sham. `pol` is a further independent
confirmation channel: the log predicts the sign of each playback pulse in the recording.

The reader is shipped; the aligner is not yet — see `TODO.md`.

### The golden log

`tests/data/pulse_log_golden.csv` is emitted by `pulse_log_selftest --emit` through the **real
firmware formatters** and parsed by `tests/test_pulse_log.py` with the **real Python reader**.
`check.sh` regenerates it and fails on any diff, so the C writer and the Python reader are pinned
to one artifact and cannot drift apart silently. To change the format deliberately: edit
`pulse_log.h`, rebuild the self-test, regenerate the golden, and update the reader.

```sh
g++ -std=c++17 -Wall -Wextra firmware/eel_core/host_test/pulse_log_selftest.cpp -o /tmp/plog
/tmp/plog --emit > tests/data/pulse_log_golden.csv
```

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
g++ -std=c++17 firmware/eel_core/host_test/pulse_log_selftest.cpp -o /tmp/t && /tmp/t
g++ -std=c++17 firmware/eel_fakefish_button/host_test/button_control_selftest.cpp -o /tmp/t && /tmp/t
g++ -std=c++17 firmware/eel_fakefish_rc/host_test/panel_control_selftest.cpp -o /tmp/t && /tmp/t
g++ -std=c++17 firmware/eel_fakefish_rc/src/eel_core/eel_stimuli.cpp \
    firmware/eel_fakefish_rc/host_test/rc_control_selftest.cpp -lm -o /tmp/t && /tmp/t
```

Each prints `OK`. The exception is `eel_core/host_test/eel_player_selftest.cpp`, which is a
sample **dumper** for diffing against the Python reference, not an assertion suite — it prints
samples and never `OK`. `pulse_log_selftest` has a second mode, `--emit`, which regenerates
`tests/data/pulse_log_golden.csv` (see [The golden log](#the-golden-log)); the gate diffs it.

## Bench bring-up checklist

Hardware is bench-owned; none of this is ever claimed done by an agent.

1. **Idle is braked, not floating.** With nothing playing *and nothing scheduled* (lever down —
   i.e. **disarmed**), scope one electrode single-ended to battery GND: it must sit **hard at 0 V**,
   with no drift or slow leak. If it floats, IN2 is not actually driven — recheck the pin 22/23
   wiring. This must hold **between pulses too**, not only when nothing is scheduled: a build that
   idles at a non-zero common-mode level is the failure documented in
   [The driver dead zone](#the-driver-dead-zone--real-measured-and-deliberately-not-compensated).
2. **Duty → volts is linear and reaches the rail.** Set `AMP_DEBUG 1` in `eel_core/config.h`,
   re-flash, and read the Serial sweep against the scope. This replaces normal operation with a
   self-contained calibration routine and never starts the sample ISR.
3. **Confirm the amplitude control keeps the pulse's character all the way down** — no thinning
   out, no cutting off at the bottom of the travel. If it ever does, measure the driver's real
   minimum pulse width before changing anything; procedure in
   [The driver dead zone](#the-driver-dead-zone--real-measured-and-deliberately-not-compensated).
4. **Measure the filter**, don't trust the marking — see the DC-bias caveat above.
5. **Set `MASTER_GAIN` on the scope** before going near an animal. A discharge reaches at most
   ~36 V — the rail — so a `0.90` volley pulse is ~32 V and a `0.45` localization pulse ~16 V.
6. **Check the pulse marker** survives into your recorder: that the six alternating pulses at 10 Hz
   resolve individually and that the alternation is unambiguous against real fish. That is a
   *detection* question, not a level-budget one, and only a recording settles it (`TODO.md` §2).
7. **RC device:** flash `rc_input_test` first, confirm all four channels, and redo `RC_CAL_*`.
