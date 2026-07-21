# Fakefish playback firmware

Plays a stimulus library from an **SD card** on a **Teensy 4.1** driving a dipole in
water. It is a **hand-held field stimulator**: you hold it, point the electrodes at a wild
fish, and press one of six buttons to play a stimulus. Only the **electrodes** go in the
water — the buttons and the indicator LED are dry operator controls in your hand.

Every stimulus is a pre-rendered **mono `int16` @ 50 kHz WAV** on the card, organised **one
directory per button**. All stimulus *generation* is offline Python
(`src/fakefish/build_sd_card.py`, the `fakefish-build-card` tool); the firmware just reads
the card and streams the file. Everything lives in **one self-contained Arduino sketch
folder**:

```
eel_fakefish/                 ← open THIS folder in the Arduino IDE (nothing to copy)
  eel_fakefish.ino            Teensy 4.1 driver: 50 kHz clock, dual PWM + RC (PWM+RC = DAC)
  sd_player.h                 SD WAV streaming: header parse, ring buffer, per-button random pick
  eel_control.h               6 program buttons, debounce, button→directory map, indicator LED
  eel_stimuli.h / .cpp        the stimulus-library SOURCE that build_sd_card.py renders → WAVs
  eel_player.h / .cpp         (retired) the old on-device overlap-add engine — see below
  host_test/                  compile the pure logic on a PC and check it
```

> **Retired, not deleted.** `eel_stimuli.{h,cpp}` (the waveform + IPI library) and
> `eel_player.{h,cpp}` (the overlap-add engine) are the old *flash-library* player: the
> firmware used to reconstruct each stimulus on-device from these tables. In the SD
> architecture the reconstruction happens **offline in Python** and the result is a WAV on
> the card, so the firmware no longer uses them. They stay in the tree because
> `build_sd_card.py` reads `eel_stimuli.cpp` as the **library source** it renders. The
> Arduino build still compiles them (harmless dead code); moving them out of the sketch
> folder is a noted follow-up.

## The SD card — one directory per button

`fakefish-build-card` renders the whole library (plus a demo song) into a directory tree.
Copy that tree to the root of a FAT32 microSD and insert it:

```
/A/   calibration   one 10 s 10 kHz sine tone
/B/   localization  one WAV per localization item  (marker → gap → 20 s loc @ LOC level)
/C/   volley        one WAV per volley item         (marker → gap → volley @ VOLLEY level)
/D/   loc→volley    loc→volley sessions             (marker → gap → 5 s loc → 300 ms → volley)
/E/   (spare)       unassigned — leave empty (a press self-mutes)
/F/   song          the demo song
```

A press **lists that button's directory, picks a `.wav` at random, and streams it.** To
reassign a button, move WAVs between directories on the card — **no reflash.** A
missing/empty directory simply self-mutes. Build a card with:

```sh
fakefish-build-card --out /path/to/sdcard      # renders /A../F + manifest.json
```

Each WAV already carries the whole session: the **10 kHz sine marker lead-in, the per-item
gap, and the stimulus at its absolute output level are all baked in** by the renderer (see
*Levels* and *The out-of-band sine marker*). The firmware adds only a per-press polarity
flip and a global gain.

## Playback = stream a WAV without ever stalling the ISR

The 50 kHz sample clock (`onSampleTick`, an `IntervalTimer` at 20 µs) must **never block on
the SD card**. Samples flow through a **lock-free single-producer / single-consumer ring
buffer** (`SdpRing` in `sd_player.h`): `loop()` refills it from the card (`sdp_prefetch` →
`File::read`, in ~½ KB bursts), and the ISR only **pops** one sample per tick. The ring is
4096 samples (~82 ms of headroom), far more than the card's worst-case read latency, and the
producer/consumer touch only their own index (a compiler barrier publishes the data write
before the index), so no lock is needed on the single M7 core. On a buffer underrun (an SD
stall) the ISR emits silence and keeps the cadence rather than glitching; when the file's
data chunk is fully read and the ring drains, the ISR tears the clock down.

Each popped sample is scaled by the playback's polarity and `MASTER_GAIN`, clamped to
±32767, and handed to `out_write()` — the same PWM+RC output stage as before.

## Output stage — PWM + RC low-pass IS a DAC

The driver's `out_write(int16 s)` splits the signed, polarity-applied sample across two
channels (`+` → channel A, `−` → channel B), a bipolar differential drive across the dipole.
**Both channels carry signal** (the EOD's negative phase is rendered on channel B), and
which channel leads encodes the playback polarity (randomised per press, constant within
one). Two FlexPWM pins (2, 3, same submodule → phase-aligned carriers) at the **585.9 kHz**
carrier (150 MHz FlexPWM clock ÷ 256, 8-bit duty), each followed by a **1-pole RC**. This
stage is **unchanged** from the flash-library firmware.

> ### ⚠️ HARDWARE: the bridging cap must be **22 nF, not 220 nF**
> Differential RC — **one 220 Ω per channel** and a **single cap BRIDGING the two nodes**
> (across A–B, floating; NOT to GND). Electrodes sit on nodes A and B:
> ```
>   pin 2 ──[220 Ω]──┬── node A ──► electrode A
>                    ├─[ C ]─┐   (C across A–B)
>   pin 3 ──[220 Ω]──┴───────┴── node B ──► electrode B
> ```
> The two 220 Ω add (440 Ω) in series with the bridging cap → differential corner
> f_c = 1/(2π·440·C):
> - **22 nF** → f_c = **16.4 kHz**: EOD passes (flat <2 kHz, ~410 µs edge intact) and the
>   10 kHz marker at −1.4 dB; 586 kHz carrier down −31 dB. **Correct.**
> - **220 nF** → f_c = 1.6 kHz: sits *inside* the EOD band. **Wrong — swap the cap.**
>
> Bridging (vs caps-to-GND) gives a **440 Ω** differential source impedance (less
> conductivity-dependent amplitude loss) and one fewer part. **Caveat:** it filters the
> *differential* carrier but not common-mode; if the recorder is single-ended or shares a
> ground with the rig, add a small **~2.2–4.7 nF node→GND** on top of the 22 nF bridge.

- **Resolution:** 8-bit duty; `out_write()` applies **first-order error-feedback noise
  shaping** per channel (`q=(v+64)>>7; err=v-(q<<7)`), exploiting the ~10× oversampling
  (50 kHz vs a <5 kHz signal) → **~+3 effective bits in-band**, so low-voltage pulses render
  cleanly. Accumulators reset per playback.
- **Sample rate:** 50 kHz is adequate — the EOD edge is ~410 µs (~20 samples); 100 kHz
  gives no meaningful in-band gain (`fakefish-simulate dac`).
- **Carrier residual** after the RC is ~6 % pp at 586 kHz — far above the eel/detector band;
  a 48 kHz recorder's anti-alias removes it. If it must drop, **raise the carrier**
  (7-bit / 1.17 MHz), **not** a 2nd RC pole.

## Levels — one field knob, the rest baked into the WAVs

In the flash-library firmware the output levels were four `#define`s in the sketch. In the
SD architecture the **per-stimulus levels are baked into the WAV samples** by
`build_sd_card.py` (the localization ↔ volley ↔ marker ratios must survive, so the WAVs are
**absolute-scaled, not peak-normalised**). The firmware keeps a **single** knob:

| Knob | Drives | Default |
|------|--------|---------|
| `MASTER_GAIN` (`.ino`) | every streamed sample (overall output trim) | `1.0` (levels exactly as rendered) |

Lower `MASTER_GAIN` if you are close to a recording electrode; `1.0` is full rendered scale
(the WAVs already use the headroom, so values >1 only clip). The baked levels reproduce the
old defaults — volley `0.90`, localization `0.45`, marker `0.25` of full scale — and can be
changed (in mV or fraction) at the renderer; see `build_sd_card.py`.

**Level → volts.** Each phase drives one PWM rail:

```
V_peak ≈ 3.3 V × amplitude × LUT_peak/32640      (≈ 3.15 V × amplitude for the sine marker;
                                                  a pulse peaks at 32767, ~5 % hotter)
```

The output is **bipolar differential**, so full scale is **~5.7 Vpp, not 3.3 V**. These are
**open-circuit** figures: in water the 440 Ω source impedance divides against the electrode
impedance, so the delivered level is lower and conductivity-dependent — **scope it on the
bench.** The renderer can print a *nominal* mV for each level via this map, but that mV is a
nominal source level, **not** a calibrated field strength (real field strength depends on
distance and geometry). Amplitude is deliberately kept out of any calibration claim.

## The out-of-band sine marker (the anchor)

The same grids and lines that record wild fish also record whatever the fakefish plays at
them, so downstream we must know which pulses were the playback. A pure **~10 kHz sine** is
the **anchor**: it **locates** each playback in the recording and its apparent frequency
**pins the recorder-vs-playback clock drift**. It is not a per-pulse label — a separate
offline stage aligns the known IPI sequence; the sine just brackets the window. In the SD
architecture the marker is **rendered into the WAVs** by `build_sd_card.py` (it is no longer
synthesised on-device), but its properties are unchanged: one exact unit-sine cycle is **5
output samples/cycle** at 50 kHz (an exact zero-start LUT that **sums to zero → no
electrolysis**), with a short raised-cosine on/off ramp (anti-click + spectral purity). The
nominal frequency is frozen in `data/stimuli_provenance.json → lead_in_marker` for the
detector.

Why a sine, and why 10 kHz:

- **It does not perturb the animal.** 10 kHz sits **well above the eel's electrosensory
  band** (the EOD's energy is below ~2 kHz), unlike a metronomic conspecific pulse train.
  ⚠️ **This is an assumption, not a measured fact** — no *Electrophorus* electroreceptor
  tuning curve has ever been measured, and eels are Otophysi (hearing to ~5 kHz), so
  "outside the electrosensory band" only means "undetectable" if the electrodes also radiate
  negligible acoustic energy. There is no captive animal to test on, so the frequency travels
  into the field unverified and baked into every recording. If it ever needs settling, the
  test is behavioural and in the field: tone **alone** vs tone **plus** a stimulus.
- **Near-zero false positives.** A sustained pure line is off the entire local biological
  band, so a narrowband spectral-peak detector flags it cleanly.
- **Hardware-clean.** 10 kHz passes the 16.4 kHz differential RC at ≈−1.4 dB and is captured
  cleanly by a 48 kHz recorder. `fakefish-simulate marker` renders the whole chain.

> **Why 10 kHz and not 6 or 8?** An exact LUT needs `50000 / f` to be an integer that also
> divides the 1 s lead-in and 10 s cal lengths: 5 kHz (10 samp/cyc) and 10 kHz (5 samp/cyc)
> qualify; 6 kHz (8.33) and 8 kHz (6.25) do not. 10 kHz is the clean stop furthest from both
> the electrosensory and otophysan-hearing ranges.

**Calibration vs lead-in, distinguished by duration.** `/A` is a bare **10 s** tone — a
bench level/electrode check and a long high-SNR tone to measure clock drift; run it with **no
focal animal present.** Every `/B` `/C` `/D` WAV opens with a **1 s** lead-in, then that
item's fixed per-item silent gap, then the stimulus. `/D` anchors its whole loc→volley
sequence on a **single** lead-in and shares **one** polarity between the two halves.

## Playback control (six program buttons)

**Six dedicated buttons, one per program.** The button *is* the program — no mode to latch,
no stop button. `loop()` polls the (software-debounced) buttons every pass; nothing plays
until you press one.

**One press = one complete playback.** A press while idle plays that button's random WAV
once, start to finish. A press **while something is playing is ignored** — playbacks are
**uninterruptible**, so a 10 kHz marker in the recording is always followed by the pulses it
announced. **Polarity is randomised per press** (see *Electrode DC*).

| Button | Directory | One press plays… |
|--------|-----------|------------------|
| **A** (pin 5) | `/A` | a random WAV — the **10 s 10 kHz calibration** tone |
| **B** (pin 6) | `/B` | a random **localization** session (marker → gap → 20 s loc @ LOC level) |
| **C** (pin 7) | `/C` | a random **volley** session (marker → gap → volley @ VOLLEY level) |
| **D** (pin 8) | `/D` | a random **loc→volley** session (one marker → gap → 5 s loc → 300 ms → volley) |
| **E** (pin 9) | `/E` | (spare/unassigned — empty directory, self-mutes) |
| **F** (pin 10) | `/F` | the **song** (a demo — no marker) |

## The indicator LED (pin 13)

Pin 13 is the Teensy 4.1's built-in LED (an external LED on pin 13 mirrors it). It is dry,
hand-held UI **above the water — it never cues the fish**. It is a plain **"playing"
indicator: solid while a WAV streams, dark when idle.** (The flash-library firmware blinked
per EOD pulse; a raw WAV carries no pulse-onset information, and the biphasic EOD defeats a
threshold detector, so the SD player uses a simple busy light.) A **~1 Hz error blink** means
**no SD card mounted** — insert/reseat the card and it re-mounts automatically. The LED write
is one `digitalWriteFast(13, …)` inside the ISR (a few cycles).

## Robustness (bench-owned, host-tested where possible)

The SD path is hardened against a set of field failure modes (unit-tested in
`host_test/sd_player_selftest.cpp` where they are pure logic):

- **Hidden / macOS AppleDouble files skipped.** The directory scan rejects any basename
  starting with `.` (e.g. `._loc_00.wav`, `.Spotlight-V100`) so a card touched by a Mac does
  not fill a directory with un-playable `._*.wav` sidecars that would randomly self-mute a
  button. The count pass and the open pass share one predicate, so they stay in lockstep.
- **SD-loss recovery.** A read error mid-playback, or a failed press, flags the card and
  drops `loop()` into the error-blink + re-mount retry, so a card that loses contact after
  boot recovers instead of silently dead-ending until a power cycle.
- **Robust header parse.** The WAV header is read in a 512 B window and chunks are walked
  with an overflow-guarded advance, so a corrupt `csize` can never spin the parse into a hang
  and a non-canonical header just self-mutes.

### Wiring

Pins are `#define`s at the top of `eel_control.h`. The defaults avoid the PWM pins (2, 3) and
A0 (`randomSeed`, digital pin 14); the 4.1's SDIO card slot is on dedicated pads and does not
collide. **Verify pins 9/10 are free on your board.**

```
Buttons: each ──[push-button]── GND          (INPUT_PULLUP, active-low)

  pin 5 ── A  /A  CALIBRATION
  pin 6 ── B  /B  LOCALIZATION
  pin 7 ── C  /C  VOLLEY
  pin 8 ── D  /D  LOCALIZATION → VOLLEY
  pin 9 ── E  /E  (spare)
  pin 10 ─ F  /F  SONG

LED    : pin 13 ── [220 Ω] ──|>|── GND       (the on-board LED mirrors it for free)
SD     : the Teensy 4.1's built-in microSD socket (SDIO — no wiring)
```

Buttons are **debounced in software** (a hand-rolled 25 ms `millis()` edge-detector,
`BTN_DEBOUNCE_MS`), all six ticked every `loop()` so a held button cannot spurious-retrigger.
To re-map a button, edit `BTN_*_PIN` / `BTN_DIRS[]` in `eel_control.h`.

## Electrode DC (deliberate decision)

The eel EOD is monophasic on purpose (an eel must perceive it as an eel), so each pulse
injects net-positive charge. **Per-press polarity randomisation** keeps the net charge near
zero across a session — the firmware flips the sign of the whole streamed WAV at random each
press (the WAVs are rendered at polarity +1; the marker LUT sums to zero and the song is
zero-mean, so flipping their sign is harmless). **No DC-blocking cap / no biphasic variant** —
a series cap would high-pass and distort the near-DC pulse shape. This is safe here because
the electrodes are **V4A stainless and exchangeable**, only a few volts are applied, and
current flows only in short pulses.

## Build

1. **Swap the bridging cap to 22 nF first** (see the hardware warning above).
2. Build a card: `fakefish-build-card --out /path/to/sdcard`, then insert the microSD.
3. Open **`firmware/eel_fakefish/`** in the Arduino IDE (Teensyduino). Everything is in that
   one folder — no copying.
4. Tools → Board → **Teensy 4.1**. (A non-4.1 build raises a `#warning`.)
5. Optionally tune `MASTER_GAIN` in `eel_fakefish.ino`; wire the six buttons + the LED (see
   *Wiring*), and flash.
6. `loop()` is the six-button interface — press a button to play a random WAV from its
   directory. The LED is solid while playing; a ~1 Hz blink means no card.

## Test the firmware logic on a PC

The pure logic (WAV parse, ring buffer, gain/polarity, random pick, debounce) is host-tested
— each prints `OK`, exit 0:

```sh
g++ -std=c++17 firmware/eel_fakefish/host_test/sd_player_selftest.cpp -o /tmp/sdp && /tmp/sdp
g++ -std=c++17 firmware/eel_fakefish/host_test/eel_control_selftest.cpp -o /tmp/ctl && /tmp/ctl
```

The library source + old overlap-add engine still validate 0 LSB against the Python
reference (they render the WAVs offline now, but the check guards the committed library):

```sh
g++ -I firmware/eel_fakefish firmware/eel_fakefish/eel_player.cpp \
    firmware/eel_fakefish/eel_stimuli.cpp \
    firmware/eel_fakefish/host_test/eel_player_selftest.cpp -lm -o /tmp/eng && /tmp/eng 0 1.0 1
```

## Regenerate the library / render the card

```sh
# regenerate eel_fakefish/eel_stimuli.{h,cpp} from the source recordings (needs the dataset):
fakefish-export export --config data/stimuli_config.yaml

# render the WAV card from the committed library (no dataset needed):
fakefish-build-card --out /path/to/sdcard

# model the PWM+RC marker / DAC output (writes a PNG):
fakefish-simulate marker
```

## Stimulus galleries

These render "what goes into the water" for each program at absolute output levels; the
shared marker/level constants in `src/fakefish/_gallery_marker.py` and `build_sd_card.py`
**mirror the firmware** — keep them in sync if the levels change.

```sh
fakefish-gallery-volley          # every volley session
fakefish-gallery-localization    # every localization session
fakefish-gallery-loc-volley      # every localize → strike (program D) session
fakefish-anatomy                 # marker → gap → onset anatomy (+ tone-tail zoom)
```
