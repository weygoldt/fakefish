# fakefish

Play back an electric fish's EOD (electric organ discharge) into water with a
**Teensy 4.1** driving a dipole. Two devices are built from one shared core:

- a **hand-held field stimulator** — you hold it, point the electrodes at a wild fish, and
  press one of six buttons to play a stimulus from an SD card;
- a **boat-mounted RC stimulator** — it rides a catamaran airboat and is flown from an RC
  transmitter over an optically-isolated link, **synthesising** its stimuli live.

In both cases only the **electrodes** go in the water; the buttons, panel and indicator LED
are dry operator controls.

The library shipped here is electric-**eel** (*Electrophorus*) discharges, but the firmware
plays back any species' EOD table.

> ⚠️ **Amplitude warning — read before the first bench run.** Both devices now drive the
> **same 36 V output stage** (two DRV8871 single-bridge drivers, 100 kHz complementary PWM;
> details in [`firmware/README.md`](firmware/README.md)). The hand-held device **migrated**
> onto it from an old direct-pin stage, which is roughly a **six-fold jump in output
> voltage**. It therefore needs the **new 36 V hardware built** before it can be bench-tested,
> and the absolute level must be **re-scoped** and trimmed (`MASTER_GAIN`) before it goes near
> a fish. This firmware must not be run on the old direct-pin hardware.
>
> Full scale is the **rail**: because the eel EOD is monophasic, only one bridge ever drives while
> the other is braked to 0 V, so a discharge reaches **at most ~36 V**. A `0.90` volley pulse is
> roughly **32 V** and a `0.45` localization pulse roughly **16 V**, open-circuit at nominal rail.
> (It is *not* ~72 Vpp — that would need a biphasic signal driving both bridges in opposite
> directions at once, which never happens here.)
>
> **Every millivolt figure the toolchain prints is stale.** `FULLSCALE_PULSE_PEAK_MV` (3313.0)
> was derived from the retired 3.3 V rail, so the renderer's mV CLI and a built card's
> `manifest.json` are ~11× low on this hardware. Fractions of full scale are correct and
> rail-independent — prefer them until the rail is scoped and the constant re-derived.

---

## Three layers, two devices

The two operator interfaces have almost nothing in common — six buttons in your hand versus
four RC channels off a transmitter — but they must put the **same** signal in the water,
through the **same** amplifier, out of the **same** stimulus library. So the firmware is split
into three layers, and only the top one differs per device:

| Layer | What it owns | Where it lives |
|-------|--------------|----------------|
| **L1 — output HAL** | one signed `int16` sample → two DRV8871 half-bridges. Pins, PWM carrier, noise shaping, the braked-not-floating idle, boot order. | `firmware/eel_core/config.h`, `out_hal.h` |
| **L2 — sample producers** | what the samples *are*: the overlap-add engine, the SD WAV streaming runtime, the live localization scheduler, the generated stimulus library. | `firmware/eel_core/eel_player.*`, `sd_player.h`, `locgen.h`, `eel_stimuli.*` |
| **L3 — control surfaces** | what the *operator* does: pin maps, debounce, RC decode, the session state machine, the LED vocabulary, the 50 kHz ISR that ties them together. | `firmware/eel_fakefish_button/`, `firmware/eel_fakefish_rc/` |

**Why it is built this way.** One output stage means a hardware fix (the complementary-drive
brake, the 100 kHz carrier, the boot ordering in `out_begin()`) is made once and both devices
inherit it. One engine means both devices reconstruct pulses from the same byte-frozen
library. A new operator interface is then a **new L3 folder**, not a fork of the firmware.

**Each sketch is self-contained.** Arduino has no include path outside a sketch folder, so
every sketch carries a **committed copy** of the core at `<sketch>/src/eel_core/`, produced by
`firmware/sync_core.sh`. That is what makes IDE-open, `arduino-cli` and rsync-to-bench all
work with zero configuration. `firmware/eel_core/` is the **single source of truth** — edit
there, run `make sync`, commit the copies; `tests/test_firmware_sync.py` fails if any copy
drifts.

**One source for the numbers.** `shared/stim_constants.json` is the single source for the
playback/session constants. `uv run fakefish-gen-constants` renders it into **both**
`firmware/eel_core/stim_levels.h` (C) and `src/fakefish/_constants.py` (Python). Never
hand-edit either generated file — the next codegen run reverts it, and `make check` fails
while it is stale.

---

## The two devices

| | **`eel_fakefish_button`** — hand-held | **`eel_fakefish_rc`** — boat |
|---|---|---|
| **Who holds it** | the operator, in the hand, at the fish | a catamaran airboat; operator on the transmitter |
| **Trigger** | 6 program buttons (pins 5–10), one press = one complete playback, uninterruptible | 4 RC channels via a PC817 opto-isolator (pins 4–7) **OR-ed** with 3 panel buttons (pins 9–11) for the bench |
| **Stimulus source** | **pre-rendered SD WAVs** — mono `int16` @ 50 kHz, one directory per button, built by `fakefish-build-card` | **live synthesis** on-device — `locgen` for the localization train, `eel_player` for the volley, over the mean EOD |
| **What it can play** | calibration train · localization · volley · loc→volley · song | continuous localization train (rate + jitter) · one-shot **blinded trial** (volley or sham, drawn by the firmware) |
| **Marker** | **6 EOD pulses @ 10 Hz, alternating polarity**, baked into every WAV (`SD_MARKER_*`) | **coded EOD burst @ 100 Hz, single polarity**, live: 2 pulses = volley, 4 = sham (`PULSE_MARKER_*`) |
| **Level control** | `MASTER_GAIN` in the `.ino` (per-stimulus levels baked into the WAVs) | CH6 amplitude pot sets the volley; localization is derived at half (`PANEL_VOLLEY_AMP` on the bench) |
| **LED (pin 13)** | solid while streaming; ~1 Hz blink = no SD card | flash per pulse; distinct pattern for a sham; double-blink = RC link lost; **inverse blink = logging failed, output suppressed** |
| **Needs an SD card** | **yes** — to *read* the stimulus WAVs | **yes** — to *write* the per-pulse log (no WAVs needed; it creates `/LOGS/` itself) |
| **Logs to the card** | not yet ([`TODO.md`](TODO.md) §5) | **every pulse**, with its exact 50 kHz sample tick — and it **will not stimulate without a working card** |

**The two markers are deliberately not unified.** Both are made of eel pulses now — nothing either
device emits is out of band — but they carry **different codes**, because they answer different
questions. The SD device's is a 6-pulse, 10 Hz burst with **alternating polarity**: it says *this
is a playback*, and the alternation is a pattern no eel produces and the firmware's random polarity
flip cannot erase. The RC device's is a 2- or 4-pulse, 100 Hz burst at a **single polarity**, where
the *count* tags volley vs sham. Same material, different codes — which is why the constants carry
distinct `SD_MARKER_*` / `PULSE_MARKER_*` prefixes; a bare `MARKER_*` name in this repo would be a
trap.

---

## What goes into the water

The library is a mean eel EOD waveform (`EOD_HV`, 131 samples @ 50 kHz) plus **timing**: 31
items of inter-pulse intervals and per-pulse relative amplitudes — 5 real volleys, 18
synthesised volleys, 8 localization trains (`uv run fakefish-render info` lists them). A
stimulus is that waveform replayed on those intervals; pulses that out-run the EOD length are
summed by the overlap-add engine.

- **Localization** — a slow, jittered train (≤ 20 Hz), the discharge an eel emits while
  cruising and probing. Played at the lower level.
- **Volley** — the high-rate discharge burst with a decaying-rate envelope: the strike. Played
  at the higher level. Its peak is calibrated against the real recorded population — **~330 Hz
  held over the first 50 ms**, decaying with τ ≈ 0.31 s — and the synthetic volleys are
  measured against the real ones on a *sustained* rate, never on `1/min(IPI)` (see
  [`TODO.md`](TODO.md) §6). The **level ratio between volley and localization is the
  experiment**, so the SD WAVs are absolute-scaled, never peak-normalised.
- **Localize → strike** — a localization lead, a short gap, then the volley (SD program D);
  one marker and one polarity span the whole sequence.
- **Sham** (RC only) — the coded marker fires and the LED shows it, but **nothing** goes into
  the water: the no-stimulus control.

**Why any marker at all.** The same grids and lines that record wild fish also record whatever
the fakefish plays at them, so downstream we must know which pulses were ours. The marker
locates each playback in the recording and pins the recorder-vs-playback clock drift. The SD
device's marker used to be a 10 kHz sine tone; it was retired because the output filter is 21.8 dB
down at 10 kHz, and because everything the device puts in the water should be made of eel pulses.
The full rationale for both codes — why alternation, why an even pulse count, why the RC device
counts instead — is in [`firmware/README.md`](firmware/README.md).

**Polarity is randomised per playback.** The eel EOD is monophasic on purpose, so each pulse
injects net charge; flipping the sign of a whole playback at random keeps the net near zero
across a session and stops the electrodes pitting. The sign of the sample *is* the polarity —
it selects which electrode drives.

---

## Repo layout

```
shared/
  stim_constants.json          SINGLE SOURCE for the playback/session constants (codegen input)

firmware/
  eel_core/                    CANONICAL shared core — edit HERE, never a sketch's copy
    config.h                   L1  output stage, sample clock, LED pin, AMP_DEBUG switch
    out_hal.h                  L1  out_begin/out_write/out_brake/out_silence/shape
    stim_levels.h              GENERATED from shared/stim_constants.json — do not edit
    eel_player.{h,cpp}         L2  overlap-add engine (marker + volley)
    eel_stimuli.{h,cpp}        L2  GENERATED stimulus library (byte-frozen contract)
    sd_player.h                L2  SD WAV streaming (header parse, ring buffer, random pick)
    locgen.h                   L2  live localization scheduler
    host_test/                 shared host self-tests (eel_player, sd_player)

  eel_fakefish_button/         L3  SURFACE — 6-button hand-held SD WAV player
    eel_fakefish_button.ino    50 kHz ISR, MASTER_GAIN, SD-loss recovery
    button_control.h           6 buttons (pins 5-10) -> SD dirs /A../F, debounce, LED
    src/eel_core/              SYNCED COPY of firmware/eel_core (committed)
    host_test/                 button_control_selftest.cpp + _amalgam.cpp

  eel_fakefish_rc/             L3  SURFACE — 4-channel RC + 3-button panel, live synthesis
    eel_fakefish_rc.ino        50 kHz ISR, session state machine, published-target model
    rc_control.h               PC817 decode (pins 4-7), calibration, conditioning, failsafe
    panel_control.h            3 panel buttons (pins 9-11) + the LED feedback vocabulary
    src/eel_core/              SYNCED COPY of firmware/eel_core (committed)
    host_test/                 rc_control_selftest.cpp, panel_control_selftest.cpp, _amalgam.cpp

  rc_input_test/               standalone RC bring-up diagnostic (read-only; bundles no core)
  sync_core.sh                 eel_core -> each sketch's src/eel_core
  README.md                    hardware doc: output stage, wiring, markers, card layout

src/fakefish/                  the Python toolchain (installable package)
  gen_constants.py             codegen: stim_constants.json -> stim_levels.h + _constants.py
  _constants.py                GENERATED — do not edit
  build_sd_card.py             render the library (+ song) to a one-dir-per-button WAV card
  export_teensy_stimuli.py     library regeneration from the source recordings (needs dataset)
  synthetic_volleys.py         volley population model + synthesis
  render_stimulus.py           parse/reconstruct the committed library
  simulate_firmware.py         model the PWM output chain (dac / carrier / marker)
  plot_*.py, _gallery_marker.py  the stimulus galleries + playback anatomy
  stimuli_qc.py, _resources.py, viz/   QC, path resolution, vendored figure + logging helpers

data/                          export config, frozen provenance, tuned volley model/populations
tests/                         export, card, codegen, and core-sync guards
check.sh  Makefile             the acceptance gate — `make check`
```

---

## Quickstart

**You need:** a Teensy 4.1 and the 36 V DRV8871 output stage wired per
[`firmware/README.md`](firmware/README.md). For the hand-held device also: a microSD card, six
momentary push-buttons, an indicator LED. For the RC device: a HY-M154 4-channel PC817 board,
an FS-i6X / FS-iA6B RC link, three panel buttons, and **a microSD card** — blank is fine, it is
for the pulse log, and the device refuses to stimulate without one.

### 1 · Flash a sketch (no Python, no dataset)

Open the surface's folder in the Arduino IDE (Teensyduino) — everything the sketch needs is in
that one folder, nothing to copy:

| Sketch | Open this folder |
|--------|------------------|
| hand-held SD player | `firmware/eel_fakefish_button/` |
| RC / panel unit | `firmware/eel_fakefish_rc/` |
| RC bring-up diagnostic | `firmware/rc_input_test/` |

Then **Tools → Board → Teensy 4.1** and **Upload**. (A non-4.1 board raises a `#warning`.)

For the RC unit, flash `rc_input_test` **first** to confirm the receiver → PC817 → Teensy path,
paste the measured pulse widths into the `RC_CAL_*` constants in `rc_control.h`, and re-flash
the real sketch.

### 2 · Build an SD card (hand-held device only)

```sh
git clone https://github.com/weygoldt/fakefish && cd fakefish
uv sync                                          # or: pip install -e .
uv run fakefish-build-card --out /path/to/sdcard
```

This renders the whole stimulus library to the card — **one directory per button** (you'll see
`/A /B /C /D /F` plus a `manifest.json`; `/E` is left unused). Copy them to the root of a FAT32
microSD and put it in the Teensy's built-in slot. No field dataset is needed; it renders from
the committed library. Program **F** plays `data/rickroll.wav` if present (gitignored),
otherwise a synthesised melody; `--song mytune.wav` overrides it.

| Button | Pin | Directory | One press plays |
|--------|-----|-----------|-----------------|
| **A** | 5 | `/A` | a **calibration** train (10 s of single-polarity eel pulses @ 50 Hz) |
| **B** | 6 | `/B` | a random **localization** session |
| **C** | 7 | `/C` | a random **volley** session |
| **D** | 8 | `/D` | a random **localize → strike** session |
| **E** | 9 | `/E` | *(spare — empty directory, self-mutes)* |
| **F** | 10 | `/F` | the **song** |

### 3 · Wire it (once)

Buttons/panel: each from its pin to **GND** (`INPUT_PULLUP`, active-low). LED on pin 13. The
electrodes hang off the two DRV8871 boards — IN1 held HIGH on pins 2/3, IN2 PWM'd on pins 0/1.
Full wiring in [`firmware/README.md`](firmware/README.md).

### 4 · Use it

- **Hand-held:** point the electrodes at the fish and **press a button once** — it plays that
  stimulus start to finish (playbacks are uninterruptible). Each press picks a *random* WAV
  from that button's directory and a random polarity. LED solid while playing; a **~1 Hz blink
  means no SD card** (reseat it — it re-mounts automatically).
- **RC:** CH3 throttle = localization on/off + rate (up to 20 Hz); CH4 right stick = throw high
  → run one **blinded trial**, which the firmware resolves to a volley or a sham (throwing low
  does nothing at all; one-shot, re-arms at centre); CH5 pot = jitter; CH6 pot =
  amplitude. Losing the link turns localization off and can never start a trial. On the bench
  with no transmitter, the three panel buttons drive the same state machine: pin 9 toggles
  localization, pin 10 fires a volley, pin 11 fires a sham. **Put a (blank) microSD card in
  before you start**: it logs every pulse to `/LOGS/PULSnnnn.CSV` and will not stimulate
  without one — a steady LED with a brief dark notch each second means logging has failed.
  Afterwards, read the card with `uv run fakefish-pulse-log info /path/PULS0000.CSV`.

### 5 · Tune — no reflash where possible

- **Hand-held output level:** `MASTER_GAIN` in `eel_fakefish_button.ino` (reflash). Set it on a
  scope first — on the 36 V stage `1.0` is a far hotter output than this device produced before
  the migration.
- **Reassign a button:** move WAVs between the card's directories.
- **Change a stimulus or the song:** re-run `fakefish-build-card`.
- **RC amplitude / rate / jitter:** the transmitter pots, live.

### 6 · Regenerate the library (needs the source recordings, NOT shipped)

```sh
uv run fakefish-export export --config data/stimuli_config.yaml   # rewrites eel_core/eel_stimuli.{h,cpp}
bash firmware/sync_core.sh                                        # propagate to each sketch's src/eel_core
make check
```

`fakefish-export` reads the source NIX recordings from a field-data workspace set in
`data/stimuli_config.yaml → paths.eods_root` and re-emits the firmware library plus the frozen
provenance. It is **not** needed to flash or to build a card.

To change a playback constant instead, edit `shared/stim_constants.json`, run `make gen`, and
commit **both** generated files.

---

## The Python toolchain

An editable install (`uv sync`, or `pip install -e .`); run from the repo root. Figures land in
`figs/` (gitignored).

| Console script | Needs | What it does |
|---|---|---|
| `fakefish-gen-constants` | — | render `shared/stim_constants.json` → `stim_levels.h` + `_constants.py` (`--check` verifies without writing) |
| `fakefish-build-card` | committed library | render the library (+ song) to a one-dir-per-button WAV card |
| `fakefish-render` | committed library | `info` lists the library items; `render` reconstructs one item as a trace |
| `fakefish-simulate` | — | model the output chain: `dac`, `carrier`, `marker`, `analyze` |
| `fakefish-gallery-volley` | committed library | every volley session at absolute output levels |
| `fakefish-gallery-localization` | committed library | every localization session |
| `fakefish-gallery-loc-volley` | committed library | every localize → strike (program D) session |
| `fakefish-anatomy` | committed library | marker → gap → onset playback anatomy |
| `fakefish-pulse-log` | a device's SD log | `info` summarises one `/LOGS/PULSnnnn.CSV` (provenance, pulse counts, trials, integrity); `pulses` lists the emitted pulses |
| `fakefish-export` | **source recordings** | `scan` mines candidate scenes; `export` re-emits the firmware library + provenance |
| `fakefish-synth-volleys` | **source recordings** | `analyze` / `synthesize` / `compare` / `overlap-demo` the volley population model |

Everything above the `fakefish-export` line runs against the **committed** firmware library, so
no dataset is needed:

```sh
uv run fakefish-render info
uv run fakefish-gallery-volley
uv run fakefish-simulate marker
```

---

## Adding a future control surface

A new operator interface is a new **L3 folder**, not a firmware fork:

1. **Create `firmware/eel_fakefish_<name>/`.** The `eel_fakefish_*` prefix is load-bearing —
   `sync_core.sh` and `tests/test_firmware_sync.py` both glob it, so a correctly named folder
   is covered by the gate the day it is added.
2. **Write `eel_fakefish_<name>.ino` plus your control header(s).** Include the core as
   `"src/eel_core/config.h"`, `"src/eel_core/out_hal.h"`, and whichever L2 producer you need.
   Never open-code the output stage: call `out_begin()` first in `setup()` (its
   IN1-HIGH → carrier → brake order is load-bearing) and `out_silence()` at every gap. Keep the
   pure logic **above** the `#ifdef ARDUINO` line so it can be host-tested.
3. **Bundle the core:** `bash firmware/sync_core.sh` (or `make sync`), then commit the produced
   `src/eel_core/` copy.
4. **Add `host_test/_amalgam.cpp`** — a two-line translation unit that `#include`s `<Arduino.h>`
   and `"../eel_fakefish_<name>.ino"`. It must live in `host_test/` (the Arduino build compiles
   every `.cpp` in the sketch root, so a root-level amalgam would collide with `setup()`/
   `loop()`). Copy an existing one.
5. **Wire it into the gate:** add a `syntax_check_sketch eel_fakefish_<name>` line to group 3 of
   `check.sh`, and a `run_selftest` line to group 2 for each host self-test you added.

Two slots are **documented but deliberately unbuilt** (no empty stubs in the tree):

- **A self-test / bring-up surface** — a scope-calibration harness for the output stage, in the
  spirit of the retired `fakefish-drv-hwtest`. Part of it already exists as the `AMP_DEBUG`
  compile switch in `firmware/eel_core/config.h`, which replaces normal playback with a
  duty-sweep routine; a standalone surface would grow that into its own sketch.
- **A de-novo-synthesis handheld** — a hand-held device that synthesises live like the RC unit
  instead of streaming WAVs. `locgen.h` was extracted into L2 for exactly this: it can be
  reused without dragging in the RC decode layer.

---

## Validation

```sh
make check     # the full acceptance gate
make gen       # regenerate stim_levels.h + _constants.py from shared/stim_constants.json
make sync      # copy firmware/eel_core into each sketch's src/eel_core
make test      # pytest only
make lint      # ruff only
```

`make check` runs four groups, fastest-failing first:

1. **codegen + core-sync are idempotent** — a stale generated file or an unsynced sketch copy
   fails here.
2. **host self-tests** — the pure logic (WAV parse, ring buffer, debounce, RC decode +
   conditioning, the engine dumper) compiled with `g++` on the PC; each must print `OK`.
3. **Teensy compile per sketch** — every sketch pulled through its `host_test/_amalgam.cpp` with
   the real `arm-none-eabi-g++`, `-fsyntax-only -Wall -Wextra` clean. Override
   `TEENSY_ROOT` / `TEENSY_GXX` if your Teensyduino install lives elsewhere.
4. **python** — `pytest` and `ruff`.

**What the gate does not cover: flashing a real Teensy 4.1 and scoping the output.** That is the
owner's bench step and is never claimed done here. The hand-held device in particular needs the
**new 36 V hardware built** and its ~6× output re-scoped before it is used.

---

## Notes for maintainers

- **The generated library and provenance are byte-frozen contracts.**
  `firmware/eel_core/eel_stimuli.{h,cpp}` and `data/stimuli_provenance.json` are the shipped
  output; the toolchain regenerates them, but the checked-in copies are the reference and
  regeneration must stay byte-reproducible.
- **Never hand-edit a generated file:** `firmware/eel_core/stim_levels.h`,
  `src/fakefish/_constants.py` (from `shared/stim_constants.json`), or any sketch's
  `src/eel_core/` copy (from `firmware/eel_core/`). Edit the source and re-run the generator.
- **Firmware is bench-owned.** Nothing in this repo may claim a sketch was flashed or
  field-tested.
- This toolchain and firmware were extracted from the `eeltracker` analysis package. One
  provenance field (`eeltracker_git_commit` in `data/stimuli_provenance.json`) keeps that
  historical name so a regenerated provenance matches the frozen schema.
