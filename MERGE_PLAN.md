# MERGE_PLAN — unify `fakefish` + `fakefish-rc` into one 3-layer repo

**Status:** design LOCKED (grilled 2026-08-17). This file is the execution spec. Run it in a
Claude Code session **rooted at `/home/weygoldt/wrk/analyses/fakefish`** (NOT an eeltracker
worktree — native git is required and works there). Execute the numbered steps in order; each
step is a commit and leaves the tree valid. Bench-flashing is the owner's step and is never
claimed done.

> Why a plan instead of direct execution: the authoring session was git-isolated inside an
> eeltracker worktree and could not run git against this repo. All design decisions below are
> already made — do **not** re-grill. Read this whole file first, then execute.

---

## 0. The two source repos

- **`/home/weygoldt/wrk/analyses/fakefish`** (THIS repo, the merge BASE — keep its name + history):
  6-button **full-SD WAV player** + the Python authoring toolchain. `main` is clean.
- **`/home/weygoldt/wrk/analyses/fakefish-rc`** (read-only SOURCE / archive — do NOT modify, do NOT
  delete until both merged sketches are bench-verified): real-time **RC synthesis** on the
  corrected **DRV8871 / 36 V / 100 kHz complementary** output stage. `main` is clean.
- `fakefish-drv-hwtest` is **gone from disk** — nothing to fold in (recreate as a self-test surface
  later only if wanted).

Both repos share a **byte-identical** Python package (`src/fakefish/`) except
`simulate_firmware.py` (rc's is a strict superset), and a **byte-identical** overlap-add engine
(`eel_player.{h,cpp}`, `eel_stimuli.{h,cpp}` — `diff -q` clean incl. the 779-line
`eel_stimuli.cpp`). So the merge is a **restructure**, not a conflict-resolution.

---

## 1. Locked design decisions (do not revisit)

| # | Decision | Choice |
|---|----------|--------|
| Q1 | Repo base | Merge into **`fakefish`** (origin, canonical name). Bring rc's RC firmware + corrected L1 in; extract shared core; **archive `fakefish-rc`** (keep on disk). Build on fakefish history; rc lands as new commits referencing rc source SHAs. |
| Q2 | Flash/transport | Must support **IDE-open + arduino-cli + rsync-to-bench**, all zero-config → **self-contained sketches** (core bundled in each sketch's `src/`). |
| Q3 | Core sync | **Committed copies** via `firmware/sync_core.sh` (copies canonical `firmware/eel_core/` → each sketch's `src/eel_core/`). A test asserts the sync leaves **no `git diff`**. |
| Q4 | Constant drift | **Codegen from one `shared/stim_constants.json`** → `firmware/eel_core/stim_levels.h` + `src/fakefish/_constants.py`. (Honest scope — see §4.) |
| Q5 | Acceptance gate | sync + codegen leave no `git diff` · all host self-tests print `OK` · every sketch `arm-none-eabi-g++ -fsyntax-only -Wall -Wextra` clean · `pytest` + `ruff` green. Bench flash = owner. |
| Q6 | Future surfaces | **Document the slots only** (self-test, de-novo handheld). No empty stub sketches. |
| Q7 | Docs | **Top `README.md` (3-layer arch + 2 devices) + `firmware/README.md` (corrected hardware + per-surface wiring) + merged `CLAUDE.md`.** Rewrite every stale filter/output section. |
| Q8 | History | Archive fakefish-rc; build on fakefish history; rc firmware imported as new commits citing source SHAs. |
| Q9 | Backup | **Create a private GitHub remote + push** — AFTER `gh auth login` (current token is invalid). Confirm repo name `fakefish` before creating. |

**User-confirmed hardware decision:** ALL devices run on the shared **36 V DRV8871 / 100 kHz /
complementary-brake** output stage. The button device MIGRATES off its old 585.9 kHz direct-pin
~5.7 Vpp stage — a real hardware + firmware change and a **~6× output-voltage jump** (trim at the
bench via `MASTER_GAIN`; the baked WAV levels stay 0.90 volley / 0.45 loc, now against the 36 V
rail).

---

## 2. Target layout

```
fakefish/                              # merged repo; python package stays `fakefish`
├── shared/
│   └── stim_constants.json            # SINGLE SOURCE for playback/session constants (§4)
├── firmware/
│   ├── eel_core/                      # CANONICAL shared L1+L2 core (single source of truth)
│   │   ├── config.h                   #   L1 output-stage/HAL config (DRV pins 0-3, 100kHz, 8-bit, brake)  [from rc config.h, HAL block only]
│   │   ├── out_hal.h                  #   L1: out_write/out_brake/out_silence/shape/drive_board (DRV8871 complementary) + AMP_DEBUG
│   │   ├── stim_levels.h              #   GENERATED from shared/stim_constants.json (do not hand-edit)
│   │   ├── eel_player.h/.cpp          #   L2: overlap-add engine (byte-identical today)
│   │   ├── eel_stimuli.h/.cpp         #   L2: generated stimulus library (EOD_HV + StimItem table, format v3)  [export tool owns]
│   │   ├── sd_player.h                #   L2: SD WAV streaming runtime (button path)
│   │   ├── locgen.h                   #   L2: live localization scheduler (extracted from rc .ino; used by RC now, de-novo later)
│   │   └── host_test/                 #   shared host self-tests for engine / sd_player
│   ├── eel_fakefish_button/           # L3 surface: 6-button SD player
│   │   ├── eel_fakefish_button.ino
│   │   ├── button_control.h           #   6-button surface (was fakefish eel_control.h)
│   │   ├── src/eel_core/              #   SYNCED copy of ../../eel_core (committed; sync_core.sh)
│   │   └── host_test/                 #   button-control selftest
│   ├── eel_fakefish_rc/               # L3 surface: 4-ch RC + panel
│   │   ├── eel_fakefish_rc.ino
│   │   ├── rc_control.h               #   RC decode (PC817, pins 4-7)   [from rc]
│   │   ├── panel_control.h            #   panel buttons + surface config (was rc eel_control.h + rc config.h L3 block)
│   │   ├── src/eel_core/              #   SYNCED copy of ../../eel_core (committed; sync_core.sh)
│   │   └── host_test/                 #   rc_control + panel selftests
│   ├── rc_input_test/                 # standalone RC bring-up diagnostic sketch (kept as-is, from rc)
│   ├── sync_core.sh                   # materializes eel_core -> each sketch's src/eel_core
│   └── README.md                      # corrected shared hardware + per-surface wiring
├── src/fakefish/                      # ONE deduped python package (fakefish base; take rc's simulate_firmware.py)
│   ├── ... (existing modules) ...
│   ├── gen_constants.py               # NEW: codegen stim_constants.json -> stim_levels.h + _constants.py
│   └── _constants.py                  # GENERATED from shared/stim_constants.json (do not hand-edit)
├── data/ · tests/ · figs/
├── pyproject.toml                     # +fakefish-gen-constants script
├── README.md                          # 3-layer architecture + the two devices
└── CLAUDE.md                          # merged invariants
```

**Naming note:** the two surfaces' control headers are DIFFERENT concerns, so rename to avoid
confusion (`button_control.h`, `panel_control.h`) rather than two files both named `eel_control.h`.
Keep each surface's `.ino` named `<foldername>.ino` (Arduino requirement).

**Pin sanity (no intra-sketch collisions):** HAL uses pins 0,1 (DRV IN2/PWM), 2,3 (DRV IN1/HIGH),
13 (LED). Button surface adds buttons 5–10 (SD uses BUILTIN_SDCARD, no GPIO). RC surface adds RC
4–7 + panel 9–11. Neither sketch collides internally; cross-sketch pin reuse is fine (separate
boards).

---

## 3. Layer boundaries (what goes where, and why)

- **L1 — output HAL (`eel_core/out_hal.h` + `eel_core/config.h`).** Authoritative source =
  `fakefish-rc/firmware/eel_fakefish/eel_fakefish.ino` (the `shape`/`drive_board`/`out_write`/
  `out_brake`/`out_silence` block, ~lines 82–114) + `fakefish-rc/.../config.h` output-stage block
  (~lines 7–64) + `AMP_DEBUG`. These are `static inline` in a header today — keep them
  header-only (`static inline`) so each sketch compiles its own copy (separate binaries → no ODR
  issue). The button device's OLD `out_write` (direct-pin 585.9 kHz, `analogWrite(PWM_A_PIN,…)`) is
  **deleted** and replaced by `#include "src/eel_core/out_hal.h"`.
- **L2 — sample producers (`eel_core/`).** `eel_player` (overlap-add, pure-pull `int16`, no ISR),
  `eel_stimuli` (the generated library), `sd_player` (SD WAV runtime), `locgen` (live loc
  scheduler). All int16 / 32767 full-scale / 50 kHz. The button surface uses `sd_player`; the RC
  surface uses `eel_player` + `locgen`. Producers a sketch doesn't reference link out — fine.
- **L3 — control surfaces (per sketch folder).** Button: `button_control.h` (6 buttons → SD dirs,
  one-shot, uninterruptible, LED). RC: `rc_control.h` (PC817 decode) + `panel_control.h` (panel
  buttons + the RC session logic: pulse-burst marker, throttle gate, volley/sham trigger). The RC
  `.ino`'s `onSampleTick` ISR (single owner) stays in the RC sketch (it wires L2 producers to L1
  `out_write`).

---

## 4. Codegen — `shared/stim_constants.json` (HONEST scope)

**Finding that refines the grilling premise:** the SD migration already moved the button device's
levels/marker/timings OUT of firmware into `build_sd_card.py`, and the RC firmware's marker is a
DIFFERENT mechanism (a coded pulse-burst, not the SD sine tone). So there is little literal
C↔Python value duplication left. What DOES exist is that **playback/session constants are scattered
across two files in two languages**: SD-path constants in `src/fakefish/build_sd_card.py`, RC-path
constants in `fakefish-rc/firmware/eel_fakefish/config.h`. The codegen consolidates ALL of them into
one authoritative JSON and generates both a firmware header and a Python module. This is a real
single-source-of-truth win and future-proofs a live-marker/live-level surface — even though today
each group is consumed by only one side.

**Two markers stay distinct** (do NOT try to unify): the SD **sine tone** (10 kHz, one cycle =
5 samples at 50 kHz, `[0,31163,19260,-19260,-31163]`, sum 0; derive the LUT at codegen from
`round(32767·sin(2π·k/(fs/freq)))`) and the RC **pulse-burst** preamble (2/4-pulse coded). They are
different mechanisms; the JSON just holds both constant groups.

### `shared/stim_constants.json` (proposed schema)

```
{
  "sd_path": {                      // consumed by build_sd_card.py (Python), + firmware if a live surface ever bakes live
    "levels": { "volley": 0.90, "loc": 0.45, "sine_marker": 0.25, "sine_marker_cal": 0.25 },
    "sine_marker": { "freq_hz": 10000, "ramp_samples": 100 },
    "session": { "leadin_s": 1, "cal_s": 10, "loc_s": 20, "d_loc_s": 5, "d_gap_ms": 300 },
    "fullscale_pulse_peak_mv": 3313.0
  },
  "rc_path": {                      // consumed by the RC firmware (C)
    "pulse_marker": { "ipi_samp": 500, "pulses_volley": 2, "pulses_sham": 4, "max_pulses": 8, "amp": 0.5 },
    "amp": { "volley_amp_ratio": 2.0, "panel_volley_amp": 1.00, "master_min": 0.0, "master_max": 1.0 },
    "loc_rate": { "min_hz": 1.0, "max_hz": 20.0, "panel_rate_hz": 5.0, "panel_cv": 0.2 }
  }
}
```

Exact CURRENT values + locations to migrate FROM (verify before deleting the originals):
- SD path — `src/fakefish/build_sd_card.py`: `RATE_HZ`(L49, = `ex.PLAYBACK_RATE_HZ`=50000),
  `MARKER_LUT`(L53), `MARKER_RAMP_SAMPLES=100`(L54), `MARKER_LEADIN_SAMPLES=1*RATE`(L55),
  `MARKER_CAL_SAMPLES=10*RATE`(L56), `LOC_PLAYBACK_SAMPLES=20*RATE`(L58),
  `D_LOC_PLAYBACK_SAMPLES=5*RATE`(L59), `D_INTERPHASE_GAP_SAMPLES=300*RATE//1000`(L60),
  `VOLLEY_AMPLITUDE=0.90`(L64), `LOC_AMPLITUDE=0.45`(L65), `MARKER_AMPLITUDE=0.25`(L66),
  `MARKER_CAL_AMPLITUDE=0.25`(L67), `FULLSCALE_PULSE_PEAK_MV=3313.0`(L76).
- RC path — `fakefish-rc/firmware/eel_fakefish/config.h`: `MARKER_IPI_SAMP 500`(L171),
  `MARKER_PULSES_VOLLEY 2`(L172), `MARKER_PULSES_SHAM 4`(L173), `MARKER_MAX_PULSES 8`(L174),
  `MARKER_AMP 0.5`(L147), `VOLLEY_AMP_RATIO 2.0`(L146), `MASTER_AMP_MIN/MAX 0/1`(L144-145),
  `PANEL_VOLLEY_AMP 1.00`(L201), `PANEL_RATE_HZ 5.0`(L199), `PANEL_CV 0.2`(L200),
  `LOC_RATE_MIN/MAX_HZ 1/20`(L117-118).

### `src/fakefish/gen_constants.py` (new console script `fakefish-gen-constants`)
- Reads `shared/stim_constants.json`.
- Emits `firmware/eel_core/stim_levels.h`: `#define`s for the rc_path group (pulse_marker, amp,
  loc_rate) that the RC firmware consumes, PLUS the sd_path group as documented `#define`s for
  future live surfaces. Session times are emitted BOTH in seconds and as sample-units computed with
  `STIM_SAMPLE_RATE_HZ` (via `#include "eel_stimuli.h"`), so the 50 kHz rate stays single-sourced in
  the export-generated `eel_stimuli.h`. Derive the sine `MARKER_LUT` array in the header from
  freq/rate. Add a generated-banner + `#pragma once`.
- Emits `src/fakefish/_constants.py`: the sd_path group as module constants (levels, marker LUT
  derived from freq/rate, session sample-units from `ex.PLAYBACK_RATE_HZ`, fullscale). `build_sd_card.py`
  imports these instead of declaring them inline.
- **Wire consumers:** `build_sd_card.py` → `from fakefish import _constants as K` (delete its inline
  block L49–L76, use `K.*`); RC firmware `config.h` → delete the rc_path constants, `#include
  "stim_levels.h"` (via `src/eel_core/stim_levels.h`).
- **StimKind is already single-sourced** in the export tool (it defines the Python `STIM_*` and emits
  the C enum) — leave it out of the JSON.
- **Codegen must be idempotent + in the gate:** running `fakefish-gen-constants` produces no
  `git diff` when the JSON is unchanged (a `pytest` guard asserts this, like the sync guard).
  Add invariants: `sample_rate_hz % marker_freq_hz == 0` (whole samples/cycle) and each tone length
  an integer number of cycles (net-DC 0). These hold today (50000 ÷ 5).

---

## 5. Validation harness (wire early; run after every step)

Add a top-level `Makefile` (or `check.sh`) target `check` that runs all of:

```sh
# 1. codegen + core-sync are idempotent (no drift)
uv run fakefish-gen-constants && git diff --exit-code -- firmware/eel_core/stim_levels.h src/fakefish/_constants.py
bash firmware/sync_core.sh    && git diff --exit-code -- 'firmware/*/src/eel_core'

# 2. host self-tests (pure logic, PC g++) — each must print OK
#    build each host_test/*.cpp with g++ -I its sketch/core dir (see each host_test README/comment)
for t in firmware/eel_core/host_test firmware/eel_fakefish_button/host_test firmware/eel_fakefish_rc/host_test; do
  # compile+run every *_selftest.cpp under $t (see existing commands in the current firmware/README.md)
  :
done

# 3. Teensy syntax compile per sketch (catches HAL/signature errors host tests can't)
TEENSY_GXX=/home/weygoldt/.arduino15/packages/teensy/tools/teensy-compile/11.3.1/arm/bin/arm-none-eabi-g++
CORE=/home/weygoldt/.arduino15/packages/teensy/hardware/avr/1.60.0/cores/teensy4
for S in eel_fakefish_button eel_fakefish_rc; do
  # amalgam.cpp = `#include <Arduino.h>` then `#include ".../<S>.ino"`; compile -fsyntax-only
  "$TEENSY_GXX" -fsyntax-only -std=gnu++17 -Wall -Wextra -mcpu=cortex-m7 -mthumb \
    -mfloat-abi=hard -mfpu=fpv5-d16 -D__IMXRT1062__ -DARDUINO_TEENSY41 -DF_CPU=600000000 \
    -DUSB_SERIAL -DLAYOUT_US_ENGLISH -DARDUINO=10819 -DTEENSYDUINO=160 \
    -I"$CORE" -I"firmware/$S" -I"firmware/$S/src/eel_core" firmware/$S/_amalgam.cpp
done

# 4. python
uv run pytest -q
uv run ruff check .
```

Notes: `arduino-cli` is NOT installed — the `-fsyntax-only` path IS the firmware gate (matches Q5).
The Teensy `arm-none-eabi-g++` is at the path above (verified present). `uv` is present. Reproduce
the exact host-test compile commands from the CURRENT `fakefish/firmware/README.md` ("Test the
firmware logic on a PC") and the rc `firmware/README.md`. When a sketch bundles the core in
`src/eel_core`, add `-I firmware/<S>/src/eel_core` to BOTH the amalgam compile and the host-test
compiles.

---

## 6. Execution steps (each = one commit; keep the tree valid)

> Work on a branch `merge-3layer` off `main`; fast-forward `main` when the gate is green (solo repo,
> but a branch gives a clean rollback point for this big restructure). `git reset --hard && git
> clean -fd` returns to clean `main` at any time. The old `firmware/eel_fakefish/` is NOT deleted
> until BOTH new sketches are green (step 8) — safe rollback until then.

**C0 — branch + scaffold.** `git switch -c merge-3layer`. Create empty `firmware/eel_core/`,
`firmware/eel_core/host_test/`, `shared/`. Add `firmware/sync_core.sh` (bash: for each
`firmware/eel_fakefish_*/`, `rm -rf src/eel_core && mkdir -p src/eel_core && cp
firmware/eel_core/*.{h,cpp} .../src/eel_core/`; exclude `host_test/`). No behavior yet. Commit.

**C1 — build the core (L1+L2).**
- Copy the byte-identical L2 files from THIS repo into `firmware/eel_core/`: `eel_player.{h,cpp}`,
  `eel_stimuli.{h,cpp}`, `sd_player.h`. **COPY, do not `git mv`** — the originals must stay in
  `firmware/eel_fakefish/` so the old sketch keeps building until the C8 cutover (they are deleted
  with the old sketch in C8, leaving `eel_core/` the sole canonical copy).
- Create `firmware/eel_core/out_hal.h`: paste the DRV8871 HAL (`shape`/`drive_board`/`out_write`/
  `out_brake`/`out_silence`, `err_a/err_b`) from `../fakefish-rc/firmware/eel_fakefish/eel_fakefish.ino`
  (~L82–114) + the `AMP_DEBUG` routine. Keep `static inline`. `#include "config.h"`.
- Create `firmware/eel_core/config.h`: paste the OUTPUT-STAGE block from
  `../fakefish-rc/firmware/eel_fakefish/config.h` (~L7–64: DRV pins 0-3, `PWM_CARRIER_HZ 100000`,
  `PWM_BITS 8`, `PWM_DUTY_MAX`, `SAMPLE_RATE_HZ`, `LED_PIN 13`, the 3 static_asserts, `AMP_DEBUG`
  default 0). Do NOT copy the RC L3 constants (marker/amp/loc/panel) — those go via codegen (§4) or
  the RC surface.
- Extract `firmware/eel_core/locgen.h`: the live localization scheduler (`locgen_*`) from the rc
  `.ino`. Header-only `static inline`.
- Move the engine/sd_player host tests into `firmware/eel_core/host_test/`.
- Do NOT wire any sketch yet. Commit "extract shared L1/L2 core from fakefish-rc (rc <SHA>)".

**C2 — codegen the shared constants (§4).** Add `shared/stim_constants.json`,
`src/fakefish/gen_constants.py` (+ `fakefish-gen-constants` in `pyproject.toml`). Run it → generate
`firmware/eel_core/stim_levels.h` + `src/fakefish/_constants.py`. Rewire `build_sd_card.py` to import
`_constants`. Add the codegen no-diff `pytest` guard. `uv run pytest -q` green. Commit.

**C3 — RC surface.** Create `firmware/eel_fakefish_rc/`:
- `eel_fakefish_rc.ino` = the rc `.ino` with the HAL/engine/locgen bodies REMOVED and replaced by
  `#include "src/eel_core/out_hal.h"` / `eel_player.h` / `locgen.h` / `stim_levels.h`. The
  `onSampleTick` ISR + loop() stay.
- `rc_control.h` = rc's `rc_control.h` (PC817 decode). `panel_control.h` = rc's `eel_control.h`
  (panel buttons/debounce) + the RC L3 constants that did NOT go to codegen (panel pins, RC pin map).
- `src/eel_core/` = `bash firmware/sync_core.sh` output (committed). `host_test/` = rc_control +
  panel selftests + a `_amalgam.cpp`.
- Gate: rc host tests OK + `eel_fakefish_rc` `-fsyntax-only` clean. Commit "add RC control-surface
  sketch on shared core (from fakefish-rc <SHA>)".

**C4 — button surface (the 36 V HAL port).** Create `firmware/eel_fakefish_button/`:
- `eel_fakefish_button.ino` = THIS repo's `firmware/eel_fakefish/eel_fakefish.ino` with its OLD
  `out_write`/`shape` (direct-pin 585.9 kHz, `PWM_A_PIN/PWM_B_PIN` L~50–54, out_write L~86) DELETED
  and replaced by `#include "src/eel_core/out_hal.h"`. Keep `sd_player` streaming, `MASTER_GAIN`
  (L55), the 50 kHz ISR, and per-press random polarity. Remove the stale
  `analogWriteFrequency(585937.5)` setup; use the core's `out_hal` setup (IN1 HIGH → carrier →
  brake).
- `button_control.h` = THIS repo's `eel_control.h` (6 buttons pins 5–10, LED 13, `BTN_DIRS`).
- `src/eel_core/` = sync output (committed). `host_test/` = the button-control + sd_player selftests
  + `_amalgam.cpp`.
- **Amplitude note (put in code + docs):** on the 36 V stage full-scale int16 → ~36 V, so the baked
  WAV levels (0.90 volley / 0.45 loc) now mean ~32 V / ~16 V — a ~6× jump from the old ~5.7 Vpp
  stage. Intended (that's the 36 V decision). Set final absolute level at the bench via `MASTER_GAIN`.
- Gate: button host tests OK + `eel_fakefish_button` `-fsyntax-only` clean. Commit "port button SD
  player onto shared 36V DRV8871 HAL".

**C5 — sync guard + Makefile.** Add the `firmware/sync_core.sh` no-diff `pytest`/shell guard and the
top-level `Makefile`/`check.sh` (§5). `make check` green. Commit.

**C6 — diagnostic sketch.** Copy `../fakefish-rc/firmware/rc_input_test/` → `firmware/rc_input_test/`
(standalone, no core). `-fsyntax-only` clean. Commit.

**C7 — Python dedup.** Replace `src/fakefish/simulate_firmware.py` with fakefish-rc's (strict
superset — adds the `carrier` subcommand). Confirm no other `src/fakefish/**` differs from rc
(they're byte-identical). `uv run pytest -q` + `ruff` green. Commit "adopt rc simulate_firmware
(superset: +carrier)".

**C8 — remove the old sketch (the cutover).** Delete `firmware/eel_fakefish/` (the pre-merge single
sketch; its files now live in `eel_core/` + the two surfaces). Update `src/fakefish/_resources.py`
(`project_root` marker was `firmware/eel_fakefish`; retarget to `firmware/eel_core` — it holds
`eel_stimuli.cpp`, the export target — and `DEFAULT_FIRMWARE` to `firmware/eel_core/eel_stimuli.cpp`).
Update export/render/build-card default firmware paths accordingly. `make check` green. Commit
"cut over to per-surface sketches; retire firmware/eel_fakefish".

**C9 — docs (§7).** Rewrite:
- Top `README.md`: the 3-layer architecture (L1 HAL / L2 producers / L3 surfaces), the two devices,
  how to add a future surface, the toolchain.
- `firmware/README.md`: the CORRECTED shared hardware — 2× DRV8871 / 36 V / 100 kHz / complementary
  brake, and **two per-channel 2nd-order single-ended lowpasses** (2× 22 nF + 220 Ω in series PER
  channel) replacing the stale single 22 nF (A↔B) differential cap; recompute the carrier-rejection
  figures for a 2nd-order per-channel RC; per-surface wiring (button 6-btn + SD; RC PC817 + panel).
- Per-surface `README`s if useful. Merge `CLAUDE.md` (both repos' invariants: byte-frozen library,
  bench-owned firmware, figure convention; drop the rc-only stale bits).
- **Stale sections to replace (source locations):** `fakefish-rc/firmware/README.md` L~199–220 (the
  DRV8871 table + ASCII diagram with `[220 Ω]…[22 nF](A↔B)`) and L~244–252/267 (single-pole
  differential rejection math); `fakefish/firmware/README.md` L~75–109 + `fakefish/README.md`
  L26/46/123 (the ENTIRE old 585.9 kHz / ~5.7 Vpp / 1-pole differential direct-pin description — the
  button device no longer uses it). Commit.

**C10 — final gate + TODO.** `make check` fully green. Add `TODO.md` notes: (a) button device needs
NEW 36 V DRV8871 hardware built before bench test + its ~6× amplitude re-scoped on a scope; (b)
future surfaces (self-test à la deleted drv-hwtest; de-novo-synth handheld) slot in as new sketch
folders; (c) the lobe/marker mechanisms differ per device by design. Merge `merge-3layer` → `main`.
Commit/merge.

**C11 — backup (owner action).** `gh auth login` (token currently invalid), then
`gh repo create fakefish --private --source=. --remote=origin --push` (confirm the name first).

---

## 7. Post-merge gates the OWNER still owns
1. **Bench-flash + scope BOTH sketches** on real 36 V DRV8871 hardware — the button device first
   needs its 36 V output stage built (it's migrating off the old direct-pin stage). Never claimed
   done by an agent.
2. Only AFTER both merged sketches are bench-verified: archive/delete `fakefish-rc` (its git history
   is preserved in its own `.git`; keep it read-only until then).
3. Re-scope the button device's absolute output on a scope and set `MASTER_GAIN` (6× jump).

## 8. Caveats / gotchas for the executor
- The overlap-add engine + `eel_stimuli` are byte-identical across repos — a plain copy from THIS
  repo is authoritative; do NOT re-derive.
- HAL functions are `static inline` in a header ON PURPOSE (each sketch is a separate binary; no ODR
  problem). Preserve the setup() ordering (IN1 HIGH → set carrier → `out_brake`) and use
  `out_silence()` (not just `out_brake()`) at gap boundaries, or stray duty leaks.
- `config.h` in fakefish-rc MIXES L1 + L3 — split carefully (L1 → `eel_core/config.h`; L3 marker/amp
  → codegen; L3 panel/RC pins → `panel_control.h`).
- Arduino compiles ALL `.cpp` under a sketch's `src/` recursively and IGNORES other subdirs — so
  `host_test/` is safe, and `src/eel_core/*.cpp` (eel_player, eel_stimuli) compile per sketch. The
  button sketch will compile `eel_stimuli.cpp`/`eel_player.cpp` even though it only uses `sd_player`
  — harmless (linked out).
- `gh` token is invalid → the push waits for `gh auth login`. `arduino-cli` absent → use the
  `-fsyntax-only` gate (that was the chosen gate anyway).
- Do NOT delete `fakefish-rc/` or `firmware/eel_fakefish/` early. Keep both until their replacements
  are green (and, for fakefish-rc, bench-verified).

---
_This plan file can be committed as a record or removed after the merge — owner's call._
