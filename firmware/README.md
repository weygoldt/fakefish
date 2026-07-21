# Fakefish playback firmware

Plays the exported electric-eel stimulus library on a **Teensy 4.1** driving a
dipole in water. It is a **hand-held field stimulator**: you hold it, point the
electrodes at a wild eel, and press one of four buttons to play a stimulus. Only the
**electrodes** go in the water — the buttons and the indicator LED are dry operator
controls in your hand.

The library, playback engine, and control layer are all kept **device-agnostic** (the
board seam is one `out_write()`), but only the 4.1 driver ships — everything lives in
**one self-contained Arduino sketch folder**:

```
eel_fakefish/                 ← open THIS folder in the Arduino IDE (nothing to copy)
  eel_fakefish.ino            Teensy 4.1 driver: dual PWM + RC low-pass (PWM+RC = DAC)
  eel_stimuli.h / .cpp        generated library (waveform + StimItem table) — DO NOT edit by hand
  eel_player.h / .cpp         shared additive-mixer engine (device-agnostic, host-tested)
  eel_control.h               shared control: 4 program buttons + indicator LED + sessions
  host_test/                  compile the engine + control logic on a PC and check them
```

The Arduino IDE compiles every `.ino` plus the `.h/.cpp` in the sketch folder (and
in a `src/` subdir) together, so **all sources sit in `eel_fakefish/`** — no manual
copy dance, no second driver to collide with. `host_test/` is a plain subdir the
Arduino build ignores (it is not `src/`), so the PC self-tests ride along untouched.

> Only the 4.1 driver is maintained. A Teensy 3.5 dual-DAC `out_write()` existed in
> earlier history and can be recovered from git if a 3.5 ever comes back; because the
> engine/library/control are board-independent, re-adding a board is just one file.

## The data format (v3)

`eel_stimuli.h` defines one signed `int16 EOD_HV[]` waveform (@ `STIM_SAMPLE_RATE_HZ`
= 50 kHz, `|peak| = 32767`, endpoints 0) and one unified table:

```c
typedef struct {
  const uint16_t* ipi_samp;  // wait-before-pulse in samples; ipi_samp[0]==0
  const uint8_t*  rel_amp;   // per-pulse relative voltage 0..255; NULL => full-scale
  uint16_t n; uint8_t kind; uint8_t group;
} StimItem;
const StimItem STIM_ITEMS[N_STIM_ITEMS];
const uint16_t STIM_LEAD_GAP_SAMP[N_STIM_ITEMS];   // v3: per-item sine-marker lead-in gap
```

Everything device-specific (PWM duty, polarity, output level) is **not** in the data —
it lives in the driver. `rel_amp` is an optional per-pulse **physiological** voltage
envelope: synthetic volleys start already-strong and then wind gently down to 80 % of
full over the discharge (`rel_amp` carries that decay); real volleys and localization
are uniform full-scale (`rel_amp == NULL`). The *absolute* level — and the
localization↔volley contrast — is a firmware concern by design (see *Output levels*):
the recorded amplitude is distance-confounded, so it is deliberately left out of the data. `kind` is one of `STIM_REAL_VOLLEY / STIM_SYNTH_VOLLEY /
STIM_LOCALIZATION`; `group` is a per-source provenance id (reserved).

**v3** adds `STIM_LEAD_GAP_SAMP[i]` — the per-item lead-in gap for the sine marker (see
*The out-of-band sine marker* below). The `EOD_HV` waveform and the `StimItem` table are
byte-identical to v2; v3 only appends the parallel gap array.

## Playback = additive mixing (not fire-and-wait)

The EOD (`EOD_HV_LEN` ≈ 131 samples, 2.6 ms) is **longer than the shortest IPI**
(down to 1.6 ms), so consecutive pulses overlap and must be **summed**.
`eel_player` is an overlap-add mixer over a ring buffer: the driver's 50 kHz clock
calls `eel_player_next()` for one signed `int16` per tick. It is the C twin of
`tools/export_teensy_stimuli.py :: reconstruct_item` and is validated **0 LSB**
against it (`host_test`). Output is clamped to ±32767 (overlap at the shipped rates
peaks at 1.0×full-scale; clipping would only begin above ~763 Hz).

## Output stage — PWM + RC low-pass IS a DAC

The driver's `out_write(int16 s)` splits the signed, polarity-applied sample across
two channels (`+` → channel A, `−` → channel B), a bipolar differential drive across
the dipole. **Both channels carry signal** (the EOD's negative phase is rendered on
channel B), and which channel leads encodes the playback polarity (randomised between
playbacks, constant within one). It plays the *full* EOD waveform (both phases), not
an approximation. Two FlexPWM pins (2, 3, same submodule → phase-aligned carriers) at
the **585.9 kHz** carrier (150 MHz FlexPWM clock ÷ 256, 8-bit duty), each followed by
a **1-pole RC**.

> ### ⚠️ HARDWARE: the bridging cap must be **22 nF, not 220 nF**
> Differential RC — **one 220 Ω per channel** and a **single cap BRIDGING the two
> nodes** (across A–B, floating; NOT to GND). Electrodes sit on nodes A and B:
> ```
>   pin 2 ──[220 Ω]──┬── node A ──► electrode A
>                    ├─[ C ]─┐   (C across A–B)
>   pin 3 ──[220 Ω]──┴───────┴── node B ──► electrode B
> ```
> The two 220 Ω add (440 Ω) in series with the bridging cap → differential corner
> f_c = 1/(2π·440·C):
> - **22 nF** → f_c = **16.4 kHz**: EOD passes (flat <2 kHz, ~410 µs edge intact) and
>   the 10 kHz marker at −1.4 dB; 586 kHz carrier down −31 dB (Z_C ≈ 12 Ω ≪ 440 Ω). **Correct.**
> - **220 nF** → f_c = 1.6 kHz: sits *inside* the EOD band, −16 dB @10 kHz, doubles
>   the edge. **The firmware is wrong with 220 nF — swap the cap.**
>
> Bridging (vs caps-to-GND) is preferable here: differential source impedance is
> **440 Ω** (not 880) → ~2× less conductivity-dependent amplitude loss into the
> water, and one fewer part. **Caveat:** a bridging cap filters the *differential*
> carrier but **not common-mode**. If the recorder is single-ended or the water
> shares a ground with the rig, add a small **~2.2–4.7 nF from each node to GND** on
> top of the 22 nF bridge, and verify the recorder's anti-alias at 586 kHz (it folds
> to ~9.9 kHz in a 48 kHz ADC). A floating/differential recording needs neither.

- **Resolution:** 8-bit duty; `out_write()` applies **first-order error-feedback
  noise shaping** per channel (`q=(v+64)>>7; err=v-(q<<7)`), exploiting the ~10×
  oversampling (50 kHz vs a <5 kHz signal) to push quantization error out of band
  → **~+3 effective bits in-band**, so low-voltage pulses (otherwise ~5 duty
  codes) render cleanly. Accumulators reset per playback; no extra analog stage.
- **Sample rate:** 50 kHz is adequate — the EOD edge is ~410 µs (~20 samples at
  50 kHz), so the residual is RC/quantization-limited, not sample-limited; 100 kHz
  gives no meaningful in-band gain (`simulate_firmware.py dac`).
- **Carrier residual** after the 1-pole RC is ~6 % pp at 586 kHz — far above the
  eel's and the detector's band; a 48 kHz recorder's own anti-alias removes it. If
  it must drop, **raise the carrier** (7-bit / 1.17 MHz, −6 dB, noise shaping
  hands the lost bit back), **not** a 2nd RC pole (two 440 Ω/22 nF stages pull the
  corner to ~6 kHz, into the EOD).
- **Fidelity:** the PWM+RC path costs ~1 % in-band RMS; the full waveform (both
  phases) is otherwise clean (`simulate_firmware.py dac`).

## The out-of-band sine marker (the anchor)

The same grids and lines that record wild eels also record whatever the fakefish plays at
them, so downstream we must know which pulses were the playback. A pure **~10 kHz sine** is the
**anchor** that solves this: it **locates** each playback in the recording and its
apparent frequency **pins the recorder-vs-playback clock drift**. It is *not* a per-pulse
label — a separate offline stage aligns the known IPI sequence to reconstruct exactly
what played; the sine just brackets the window. (The nominal frequency is frozen in
`tools/stimuli_provenance.json` → `lead_in_marker.nominal_freq_hz` for that detector.)

Why a sine (and why 10 kHz):

- **It does not perturb the animal.** 10 kHz sits **well above the eel's electrosensory
  band** (the EOD's own energy is below ~2 kHz), so — unlike the old 50 Hz eel-EOD
  calibration train, a metronomic "conspecific" doing a behaviour no eel ever does — a
  continuous narrowband tone carries none of the temporal/waveform structure a
  pulse-receptor parses.
  ⚠️ **This is an assumption, not a measured fact.** No *Electrophorus* electroreceptor
  tuning curve has ever been measured — the out-of-band claim rests on tuning curves from
  congeners, whose best frequencies sit below ~2 kHz. Eels are also Otophysi (Weberian
  apparatus, hearing to ~5 kHz); 10 kHz clears that acoustic ceiling by an octave too, but
  "outside the *electrosensory* band" would only mean "undetectable" if the electrodes also
  radiate negligible acoustic energy. There is no captive animal to test any of this on, so
  the assumption travels into the field unverified — and the frequency is baked irreversibly
  into every recording made with it. If it ever needs settling, the test is behavioural and
  belongs in the field: compare responses to the tone **alone** against the tone **plus** a
  stimulus.
- **Near-zero false positives.** A sustained pure line is off the *entire* local
  biological band (pulse fish are broadband transients; wave-fish EODs ≤ ~1.8 kHz), so a
  narrowband spectral-peak detector flags it cleanly.
- **Hardware-clean.** 10 kHz passes the 16.4 kHz differential RC at **≈−1.4 dB** (no cap
  change), is exactly **5 output samples/cycle** at 50 kHz (an exact zero-start lookup
  table), and is captured cleanly by the 48 kHz recorder (well under its 24 kHz Nyquist).
  `tools/simulate_firmware.py marker` renders the whole chain (LUT → 8-bit noise-shaped
  PWM → RC → 48 kHz recorder): the recorded line sits ~140 dB above the off-line floor.

> **Why 10 kHz and not 6 or 8?** The output rate is 50 kHz, so an exact lookup table needs
> `50000 / f` to be an **integer** number of samples per cycle *and* to divide the 1 s
> lead-in and 10 s cal lengths. 5 kHz (10 samp/cyc) and 10 kHz (5 samp/cyc) qualify; 6 kHz
> (8.33) and 8 kHz (6.25) do **not** — a fractional period spreads quantisation harmonics
> into the band and costs ~46 dB of line purity for nothing. 10 kHz is the clean stop
> furthest from both the electrosensory and the otophysan-hearing ranges.

**Generation.** One exact unit-sine cycle is a 5-entry `int16` lookup table
(`MARKER_LUT` in `eel_control.h`); it sums to zero (odd symmetry → **zero net charge per
cycle → no electrolysis** even over the long calibration tone). Each tone gets a short
**raised-cosine on/off ramp** (`MARKER_RAMP_SAMPLES`, ~2 ms): a hard-gated tone would
splatter broadband energy *down* into the EOD band and click, so the ramp is both an
anti-click and a spectral-purity requirement. It is generated *outside* the overlap-add
pulse engine (which stays byte-identical); the driver just feeds the samples to the same
`out_write()`.

The marker's two levels are covered under *Output levels* below — the lead-in and the
calibration tone are separate knobs because they answer opposite constraints.

## Output levels — the four knobs you tune

Every signal has its **own absolute level** on a 0..1 scale, all four in the `.ino`. They
are **independent**: none scales any other, so what you set is what you get.

| Knob | Drives | Default | Why that value |
|------|--------|---------|----------------|
| `VOLLEY_AMPLITUDE` | volley items (C, and D's strike) | `0.90` (~2.98 V pk) | the strike is an eel's loudest discharge — the reference level |
| `LOC_AMPLITUDE` | localization items — **both B and D's lead-in loc** | `0.45` (~1.49 V pk) | a localization discharge is genuinely lower-voltage at the source than a volley. **Keep it below `VOLLEY_AMPLITUDE`** or a "localization" reads as a strike |
| `MARKER_AMPLITUDE` | the 1 s sine lead-in (B/C/D) | `0.25` (~0.79 V pk) | must be detectable at whatever range you play from — raise for reach, lower if you are close enough to a recorder to clip it |
| `MARKER_CAL_AMPLITUDE` | the 10 s calibration tone (A) | `0.25` (~0.79 V pk) | played **against a recording electrode**, where full scale would clip the recorder's input |

**One localization knob serves both B and D.** The level follows the *item's own kind*
(`item_amplitude_for_kind()`), so the sensing discharge is identical whether you play it
alone or as the lead into a strike — the two cannot drift apart.

**Why calibration is its own knob.** It is run with the electrodes right next to a
recording electrode, so the recorder sees the tone at essentially zero distance. Turning it
down costs nothing: the SNR case for a loud marker is a **far-field** argument, and at
contact range there is SNR to burn.

Amplitude is a firmware concern **by design** — the recorded amplitude is
distance-confounded, so it is deliberately left out of the stimulus library.

**Level → volts.** Each phase drives one PWM rail and the sine LUT peaks at 31163/32767, so

```
V_peak ≈ 3.3 V × amplitude × 31163/32640 ≈ 3.15 V × amplitude
```

The output is **bipolar differential**, so peak-to-peak is **twice** that — full scale is
**~5.7 Vpp, not 3.3 V**. The 16.4 kHz RC costs ~1.4 dB at the 10 kHz marker and is nearly
flat over the EOD band (<2 kHz). These are
**open-circuit** figures: in water the 440 Ω source impedance divides against the electrode
impedance, so the delivered level is lower and conductivity-dependent — **scope it on the
bench**.

| amplitude | duty code | V peak | Vpp |
|-----------|-----------|--------|-----|
| 0.90 | 219 | 2.83 V | 5.67 V |
| 0.32 | 78 | 1.01 V | 2.02 V |
| 0.25 | 61 | 0.79 V | 1.58 V |
| 0.20 | 49 | 0.63 V | 1.27 V |

Resolution is not a concern at the quiet end: 0.25 still peaks at duty code **61 of 255**,
and the noise shaper hands back ~3 more effective bits on top — far from the ~5-code regime
that motivated the shaper.

**Two roles, distinguished by duration alone:**

- **Calibration** (`MARKER_CAL_S`, 10 s, at the quiet `MARKER_CAL_AMPLITUDE`): the bare
  tone — a bench level/electrode check, and a long tone to measure the clock-drift factor.
  **No item follows.**
- **Lead-in** (`MARKER_LEADIN_S`, 1 s) before every volley/localization playback: the tone,
  then that item's **fixed per-item silent gap** (`STIM_LEAD_GAP_SAMP[i]`, ~50–200 ms), then
  the item. The gap is drawn once at export (deterministic, seeded) and **fixed per item**,
  recorded in `tools/stimuli_provenance.json` — so downstream, the marker's recorded offset
  plus the item's known gap locates its first pulse and helps identify which item played.
  (An eel might learn the tone→gap timing, but it can't tell *which* stimulus is coming until
  the pulses themselves start — and it should not perceive the 10 kHz at all.)
  Button **D** anchors its *whole* loc→volley sequence on a **single** lead-in — it does not
  re-announce before the volley — and separates the two halves with a fixed
  `D_INTERPHASE_GAP_MS` (default 300 ms). That gap is a firmware constant rather than
  library data, so downstream can reconstruct the loc→volley lattice without regenerating
  `eel_stimuli`.

## Playback control (four program buttons)

The driver uses the device-agnostic control layer (`eel_control.h`, the twin of
`eel_player.h`): **four dedicated buttons, one per program**. The button *is* the program
— there is no mode to latch and no stop button. The `loop()` in the `.ino` is this real
interface, not a placeholder: nothing plays until you press a button.

**One press = one complete playback.** A press while idle plays that program once, start
to finish. A press **while something is playing is ignored** — playbacks are
**uninterruptible** and always run to completion. That is deliberate: it guarantees that a
10 kHz marker in the recording is *always* followed by the pulses it announced, so a marker
can never be stranded by a half-played trial. Item polarity is randomised per session (the
symmetric marker has none).

**The four programs:**

| Button | Program | One press plays… |
|--------|---------|------------------|
| **A** (pin 5) | CALIBRATION | a **10 s 10 kHz sine** (the bare anchor tone) at the quiet `MARKER_CAL_AMPLITUDE`, then stops |
| **B** (pin 6) | LOCALIZATION | a **1 s 10 kHz sine lead-in**, the item's per-item gap, then a random localization item, **bounded to `LOC_PLAYBACK_S` seconds**, at `LOC_AMPLITUDE` |
| **C** (pin 7) | VOLLEY | a **1 s lead-in**, the item's per-item gap, then a random volley-family item (real or synthetic), in full at full scale — starting already-strong (synthetic ones then decay gently to ~80 %) |
| **D** (pin 8) | LOCALIZATION → VOLLEY | **one** 1 s lead-in, the gap, a **short** localization (`D_LOC_PLAYBACK_S`, default 5 s) at `LOC_AMPLITUDE`, a brief silence (`D_INTERPHASE_GAP_MS`, default 300 ms), then a volley at full scale — both halves sharing **one** polarity |

Calibration is the bare **10 s sine marker** (see *The out-of-band sine marker* above) —
bench level/electrode check plus the long high-SNR tone that measures the clock drift. It
replaced the old 50 Hz eel-EOD calibration train, whose metronomic conspecific waveform
would have perturbed the animal. Its knobs are `#define MARKER_CAL_S` (10 s, in
`eel_control.h`) for length and `MARKER_CAL_AMPLITUDE` (in the `.ino`) for level — it plays
**much quieter than the lead-in** because you hold it against a recording electrode (see
*Two levels* above). (Run the 10 s cal only with **no focal animal present** — it is the
single longest marker exposure.) There is no longer a fixed-level eel-pulse render check;
if you need one on the bench, press B or C to play a real EOD item.

**Button D — the sense-then-strike sequence.** D composes the hunting motif an eel
actually performs: localize, then strike. Earlier firmware made you assemble that by hand
(play a localization, stop it, flip a switch, fire a volley); D pre-composes it, which is
why the stop toggle is gone. Both halves ride under **one** marker and share **one**
polarity — a real fish neither re-announces itself nor flips its dipole between sensing
and striking. D's localization is deliberately **short** (5 s, not B's 20 s) so a trial
stays snappy and you get more probes per session; a whole D press is ~7 s.

> **Honest caveat.** D is a *sequence-level* caricature of a hunt, not a replay of one.
> It steps amplitude 0.5×→1.0× and inserts a silence, where a real eel ramps rate and
> amplitude continuously into the strike. A faithful ramp would need the stimulus library
> regenerated with a combined loc→volley item.

**Sessions.** One press builds a **session**: an ordered list of segments — `SEG_TONE`
(the marker), `SEG_SILENCE` (a gap), `SEG_ITEM` (a library item) — that the 50 kHz sample
clock walks, one sample per tick, to the end. Every program is just a different segment
list (`build_session()` in `eel_control.h`), so D chains loc→gap→volley without a single
new state in the ISR. Only `SEG_ITEM` touches `eel_player`, which is what keeps the engine
byte-identical and the marker and gaps purely driver-level.

## The indicator LED (pin 13)

Pin 13 is the Teensy 4.1's built-in LED (an external LED on pin 13 mirrors it). It is the
operator's only feedback — it is **dry, hand-held UI above the water and never cues the
fish**:

| While playing | LED |
|---------------|-----|
| the 10 kHz marker tone | **solid** for the whole tone (cal or lead-in) |
| a stimulus item | **one blink per EOD pulse** |
| a gap, or idle | **dark** |

The blink is a **retriggerable one-shot**: each pulse onset re-arms a `LED_BLINK_MS`
(default 6 ms) countdown. So a localization's 100 ms–1 s intervals read as **discrete,
countable flashes** (~2.5 % duty at 4 Hz), while a volley's 2.5–3.3 ms intervals re-arm
the countdown before it expires and merge into **one continuous glow** (~77 % duty) that
visibly flickers as the volley's rate decays. That merging is physical, not a bug — and
pin 13 still toggles per pulse for a scope.

Two implementation details that are load-bearing rather than incidental:

- The blink is driven by the **engine's pulse onsets** (`EelPlayer.last_onset`), *not* by
  thresholding the output sample. The EOD waveform is **biphasic** — it crosses zero
  between its lobes — so an `|sample| > threshold` detector would fire **twice per pulse**.
- The solid tone likewise cannot come from the output sample: a 10 kHz sine crosses zero
  10 000 times a second, so the marker phase drives the LED **HIGH unconditionally**.

The LED write is a single `digitalWriteFast(13, …)` (one fast-GPIO register write, a few
cycles) inside the 50 kHz ISR — negligible against the ~12 000-cycle tick budget.

**Localization amplitude.** See *Output levels* above: localization plays at
`LOC_AMPLITUDE` and volley at `VOLLEY_AMPLITUDE` — a perceptible loc↔volley contrast, with
one localization knob shared by B and D.

**Localization playback length.** Localization trains are stored **full length (~60 s)**
in the library, but you rarely want to sit through all of it. `LOC_PLAYBACK_S` (a
`#define` in `eel_control.h`, default **20 s**) sets how many seconds one press of **B**
actually plays: if it is **shorter** than the stored train only the first `LOC_PLAYBACK_S`
seconds play; if it is **longer** than the stored train the train **loops** (the seam keeps
the cadence) to fill it. **D** uses its own, shorter `D_LOC_PLAYBACK_S` (default **5 s**)
so the sense→strike sequence stays snappy. Volley and calibration always play in full.

### Wiring

Pins are `#define`s at the top of `eel_control.h` — change them there if your rig
needs different ones. The defaults avoid the PWM pins (2, 3) and A0 (`randomSeed`, which
is digital pin 14), and none of them is tied to bootloader entry (the 4.1 uses a dedicated
PROGRAM button).

```
Buttons: each ──[push-button]── GND          (INPUT_PULLUP, active-low)

  pin 5 ── A  CALIBRATION            (10 s 10 kHz sine)
  pin 6 ── B  LOCALIZATION           (lead-in → gap → localization)
  pin 7 ── C  VOLLEY                 (lead-in → gap → volley)
  pin 8 ── D  LOCALIZATION → VOLLEY  (one lead-in → gap → short loc → silence → volley)

LED    : pin 13 ── [220 Ω] ──|>|── GND       (the on-board LED mirrors it for free)
```

Pin 13 sources only a few mA cleanly, so size the series resistor for that — or drive a
small NPN/MOSFET if you want a bright indicator in daylight. Pin 13 boots LOW and the
Teensy loader blinks it while programming; both are cosmetic.

To re-map which button selects which program, edit the `BTN_*_PIN` defines in
`eel_control.h` (the `EelProgram` enum value is the index into `BTN_PINS[]`). Buttons are
**debounced in software** (a hand-rolled 25 ms `millis()` edge-detector,
`BTN_DEBOUNCE_MS`, one state per button) — no library dependency. If you prefer, the
Teensy-idiomatic `Bounce2` library is a drop-in alternative, but the hand-rolled version
keeps the sketch dependency-free. All four debouncers tick every `loop()`, so a held
button cannot spurious-retrigger when a playback ends.

## Electrode DC (deliberate decision)

The EOD is monophasic on purpose (an eel must perceive it as an eel), so each
pulse injects net-positive charge that between-playback polarity flips don't null
within a burst. **No DC-blocking cap / no biphasic variant** — a series cap would
high-pass and distort the near-DC pulse shape. This is safe here because the
electrodes are **V4A stainless and exchangeable**, only a few volts are applied,
and current flows only in short pulses (low duty), not continuously.

## Build

1. **Swap the bridging cap to 22 nF first** (see the hardware warning above).
2. Open **`firmware/eel_fakefish/`** in the Arduino IDE (Teensyduino). Everything is
   already in that one folder — no copying.
3. Tools → Board → **Teensy 4.1**. (A non-4.1 build raises a `#warning`.)
4. Tune the four output levels in `eel_fakefish.ino` (see *Output levels*; and the control pins in
   `eel_control.h`, if your rig differs), wire the four buttons + the LED
   (see *Wiring* above), and flash.
5. `loop()` is the four-button interface — press a button to play that program once.
   The LED is solid through the tone and blinks per pulse through the stimulus.

## Regenerate the library / test the engine

```sh
# regenerate eel_fakefish/eel_stimuli.{h,cpp} (real + synthetic population) from the data:
python tools/export_teensy_stimuli.py export --config tools/stimuli_config.yaml

# validate the engine on a PC against the Python reference (0 LSB expected):
g++ -I firmware/eel_fakefish \
    firmware/eel_fakefish/eel_player.cpp firmware/eel_fakefish/eel_stimuli.cpp \
    firmware/eel_fakefish/host_test/eel_player_selftest.cpp -lm -o /tmp/selftest
/tmp/selftest 0 1.0 1     # item 0, amplitude 1.0, polarity +1 -> samples on stdout

# check the device-agnostic control logic (debounce, session construction for all four
# programs, LED blink/onset logic, per-class item lists, 10 kHz sine marker
# LUT/ramp/spectral-purity) on a PC -- prints "OK", exit 0:
g++ -I firmware/eel_fakefish \
    firmware/eel_fakefish/eel_player.cpp firmware/eel_fakefish/eel_stimuli.cpp \
    firmware/eel_fakefish/host_test/eel_control_selftest.cpp -lm -o /tmp/control_selftest
/tmp/control_selftest

# render the sine marker through the RC + 48 kHz recorder model (writes a PNG):
python tools/simulate_firmware.py marker
```

## Stimulus galleries

These render "what goes into the water" for each program, at absolute device output
levels (marker at `MARKER_AMPLITUDE`, localization at `LOC_AMPLITUDE`, volley at
`VOLLEY_AMPLITUDE` — the real height relationships). PNGs are gitignored; each has
`--out`. The shared marker/level constants live in `tools/_gallery_marker.py` and
**mirror the firmware** — keep them in sync when the sketch's marker or output levels
change.

```sh
python tools/plot_volley_gallery.py         # B: every volley item
python tools/plot_localization_gallery.py   # B: every localization sequence
python tools/plot_loc_volley_gallery.py     # D: every localize -> strike sequence
python tools/plot_playback_anatomy.py       # marker -> gap -> onset anatomy (+ tone-tail zoom)
```

Program **D** has no library item — the firmware composes it from a localization and a
volley at runtime — so `plot_loc_volley_gallery.py` reconstructs the emitted sequence
(one marker → gap → short localization → interphase gap → volley) and draws every volley
in its striking context, cycling the localizations so each is represented.
