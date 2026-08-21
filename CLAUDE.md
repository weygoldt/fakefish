# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## What this is

Electric-fish EOD **playback**: **two** Teensy 4.1 dipole stimulators built on **one shared
firmware core**, plus a Python toolchain that generates/renders/simulates the stimulus
library. The repo was extracted from the `eeltracker` analysis package (main `aa3be6e`) to
stand on its own — cloneable and flashable with no dependency on `eeltracker` or the field
dataset.

`README.md` (project overview, toolchain, card build) and `firmware/README.md` (hardware: output
stage, filter, markers, per-surface design) were rewritten for the merged tree and are current.
Open work is tracked in `TODO.md`.

## Three layers, two surfaces

The firmware is layered; the split is load-bearing and is spelled out in every file header.

- **L1 — output stage / HAL.** `firmware/eel_core/config.h` + `out_hal.h`. Two DRV8871
  single-bridge drivers (one per electrode) on a **36 V** rail, driven as a true
  complementary pair: IN1 held HIGH (pins 2/3), IN2 PWM'd at the *complement* of the wanted
  duty (pins 0/1), 100 kHz carrier, 8-bit, LED on pin 13. Idle = both electrodes actively
  **braked to GND**, never coasting. `out_begin()` encapsulates the load-bearing bring-up
  order (IN1 HIGH → carrier → brake). `AMP_DEBUG 1` replaces all playback with a scope
  calibration routine. HAL functions are `static inline` on purpose — each sketch compiles
  its own copy into its own binary.
- **L2 — sample producers.** `eel_player.{h,cpp}` (overlap-add engine, the C twin of
  `export_teensy_stimuli.reconstruct_item`), `eel_stimuli.{h,cpp}` (the generated library),
  `sd_player.h` (SD WAV streaming runtime), `locgen.h` (live localization scheduler),
  `pulse_log.h` (per-pulse SD event log — the **mirror image** of `sd_player.h`: there the ISR
  pops samples `loop()` read from the card, here the ISR pushes records `loop()` writes to it;
  same lock-free SPSC ring, same pure/`#ifdef ARDUINO` split). Device-agnostic: they produce
  `int16` samples (or records) and know nothing about pins.
- **L3 — control surfaces.** One sketch folder per device; each owns its inputs, its session
  state machine and its ISR, and nothing else.
  - **`firmware/eel_fakefish_button/`** — 6-button hand-held **SD WAV player**.
    `button_control.h` (buttons on pins 5–10 → SD dirs `/A`…`/F`: A calibration, B
    localization, C volley, D loc→volley, E spare, F song) + `eel_fakefish_button.ino`.
    Uses L2 `sd_player` only; `MASTER_GAIN` in the `.ino` is the single output trim.
  - **`firmware/eel_fakefish_rc/`** — 4-channel **RC + 3-button panel**, **live synthesis**.
    `rc_control.h` (PC817-isolated decode; CH3 throttle→loc on/off + rate on pin 4, CH4
    trigger→volley/sham on pin 5, CH5 jitter on pin 6, CH6 amplitude on pin 7 — pins are 4–7
    not 5–8 because pin 8 was dead on the build board), `panel_control.h` (pins 9–11 +
    the LED-feedback vocabulary) + `eel_fakefish_rc.ino`. Uses L2 `eel_player` + `locgen` +
    `eel_stimuli` + `pulse_log`. No `MASTER_GAIN` — amplitude is live (CH6, or
    `PANEL_VOLLEY_AMP` on the bench). **It needs an SD card** — not for playback (it
    live-synthesises everything, no WAV card) but for the per-pulse log, and it refuses to
    stimulate without one (invariant 9).
- **`firmware/rc_input_test/`** — a standalone, read-only RC bring-up diagnostic. Bundles no
  core, drives no output pins. Keep its pin numbers in sync with `rc_control.h`'s `RC_PIN_*`.
- **`src/fakefish/`** — the Python toolchain (installable package, console scripts). One
  deduped package serving both devices.
- **`shared/stim_constants.json`** — the single source for playback/session constants,
  rendered into C **and** Python by codegen (invariant 2).

## Load-bearing invariants

1. **`firmware/eel_core/` is canonical; every `<sketch>/src/eel_core/` is a COMMITTED COPY.**
   Arduino has no include path outside a sketch folder, so each sketch bundles a byte-identical
   copy — that is what makes every sketch self-contained (IDE-open / `arduino-cli` /
   rsync-to-bench, zero config). `firmware/sync_core.sh` is the only sanctioned way to produce
   those copies (`*.h` + `*.cpp`, non-recursive; `host_test/` deliberately excluded). **Never
   edit a sketch's `src/eel_core/`** — edit `firmware/eel_core/`, run the script, commit the
   result. `tests/test_firmware_sync.py` compares bytes and fails on any drift; `check.sh` runs
   the script and fails on any resulting `git diff`.
2. **Shared constants are GENERATED — edit the JSON, never the outputs.**
   `shared/stim_constants.json` is the single source; `uv run fakefish-gen-constants`
   (`src/fakefish/gen_constants.py`) renders it into **both**
   `firmware/eel_core/stim_levels.h` (C `#define`s for the sketches) and
   `src/fakefish/_constants.py` (module constants for the toolchain). **Never hand-edit either
   generated file** — the next codegen run silently reverts it, and
   `tests/test_gen_constants.py` + `fakefish-gen-constants --check` fail on a stale one.
   `build_sd_card.py`, `simulate_firmware.py` and `_gallery_marker.py` all *import*
   `_constants`, so the WAV card and the galleries can no longer drift. (This replaces the old
   "`build_sd_card.py` owns the levels; `_gallery_marker.py` mirrors them" rule — that drift
   hazard is fixed.) Two things are deliberately **not** in the JSON: the **sample rate**
   (single-sourced as `STIM_SAMPLE_RATE_HZ` in the export-generated `eel_stimuli.h` /
   `export_teensy_stimuli.PLAYBACK_RATE_HZ`; every sample-unit figure is derived from it) and
   **`StimKind`** (single-sourced in the export tool). Pins, thresholds, debounce and
   calibration are per-surface (L3) or per-board (L1) and stay in their own headers.
3. **The generated stimulus library is byte-frozen — and regenerating it needs the `export`
   dependency group, or it silently produces an EMPTY library.** `firmware/eel_core/eel_stimuli.{h,cpp}`
   and `data/stimuli_provenance.json` are the shipped contract. The toolchain can regenerate
   them (needs the source recordings), but do not hand-edit the checked-in copies, and keep
   regeneration byte-reproducible — e.g. the `// GENERATED by tools/export_teensy_stimuli.py`
   banner string and the `eeltracker_git_commit` provenance key are kept verbatim so a
   regenerated file matches the frozen schema. Only the value of the key (this repo's HEAD)
   changes. `fakefish-export` writes to `firmware/eel_core/` (`paths.firmware_dir` in
   `data/stimuli_config.yaml`) — **a re-export must be followed by `bash firmware/sync_core.sh`.**

   The **full regeneration sequence**, which needs the source recordings AND the `export`
   dependency group (`nixio` to open the NIX/HDF5 recordings, `scikit-learn` for the spatial
   clustering that separates fish — neither is in the default install):

   ```sh
   uv run --group export fakefish-export scan -c data/stimuli_config.yaml   # -> data/stimuli_candidates.json (gitignored)
   uv run fakefish-synth-volleys synthesize                                  # -> data/synthetic_population.npz (+ QC)
   uv run --group export fakefish-export export -c data/stimuli_config.yaml  # -> eel_stimuli.{h,cpp} + provenance
   bash firmware/sync_core.sh
   ```

   `synthesize` needs **no recordings** — it draws from the vendored model (invariant 10), so
   only `scan` and `export` need the dataset.

   **Without the `export` group the scan skipped every recording with a per-file WARNING and
   wrote an empty manifest, from which `export` would regenerate an EMPTY library over the
   frozen one.** `scan` now raises on a missing reader and refuses to write an empty manifest;
   do not weaken those guards. `scan` is deterministic (KMeans is seeded), so a re-run
   reproduces `data/multifish_volley_candidates.csv` byte-for-byte and re-selects the same 7
   real exemplars — **verify that after any re-export**: items 0–6 and `EOD_HV` must come back
   byte-identical, and only the synthetic **volleys** (7–106) may move. The 8 localization
   items must come back byte-identical too — `build_population` draws volleys and localization
   from decoupled RNG streams precisely so a volley-side change is reviewable, and
   `generate_localization` deliberately does **not** snap to the sample grid (which would move
   every train by up to one sample and destroy that property for nothing).
4. **TWO MARKER CODES, deliberately not unified.** Both devices mark with **eel pulses** —
   nothing this project emits is out of band any more — but the *codes* differ.
   - **Button/SD:** **6 pulses at a fixed 10 Hz (IPI 5000 samples = 100 ms) with ALTERNATING
     polarity**, at level 0.5, baked into every `/B`/`/C`/`/D` WAV by
     `build_sd_card.render_pulse_marker` (`SD_MARKER_*` in C, `MARKER_*` in `_constants.py`).
     Three load-bearing properties: **alternation is the detection cue** (no eel alternates and a
     localization train is single-polarity, so it cannot be confused with biology or with the
     stimulus); it **survives the per-press random polarity flip**, which negates the whole WAV —
     so **detect the pattern, never the sign**; and the count is **EVEN**, which makes the burst
     charge-balanced (the codegen asserts evenness). The per-item lead gap is unchanged:
     `[marker] → [STIM_LEAD_GAP_SAMP] → [stimulus]`.
   - **RC:** a **live coded burst at 100 Hz, SINGLE polarity**, tagged by pulse count —
     volley = 2, sham = 4 (`PULSE_MARKER_*`).

   The distinct prefixes are the point: a bare `MARKER_*` name in a repo with two marker codes is
   a trap. Do not "unify" them.

   Program **A** on the card changed with the marker: it is a 10 s **single-polarity** eel-pulse
   train at 50 Hz (`CAL_*`, level 0.45), not a tone — single-polarity on purpose, so it reads as a
   plain reference signal and never as a code.

   *(History: the SD marker was a 10 kHz sine tone until 2026-08-17. It was removed because the
   2-pole output filter is −21.8 dB at 10 kHz and because everything the device emits should be
   made of eel pulses. `MARKER_LUT`, `MARKER_FREQ_HZ`, `MARKER_RAMP_SAMPLES`, `MARKER_LEADIN_*`,
   `MARKER_CAL_*`, `Levels.marker_cal`, `build_sd_card.render_marker` and `_marker_envelope` are
   **gone** — do not reintroduce those names.)*
5. **Flashing needs no Python; BOTH devices now need an SD card, for different reasons.** Both
   sketches compile and flash with no Python and no dataset. Button-device **playback** reads a
   WAV card (one directory per button) built by `fakefish-build-card`, which renders from the
   committed library and needs Python but **no dataset**. The RC device still needs **no WAV
   card** — it live-generates over the `EOD_HV` waveform baked into `eel_stimuli.{h,cpp}` — but
   it does need a **writable** card for the per-pulse log, and refuses to stimulate without one
   (invariant 9). Its card needs no prepared content: it creates `/LOGS/` itself. Only the
   *library regeneration* path (`fakefish-export`, reading
   `data/stimuli_config.yaml → paths.eods_root`) needs the source recordings (not shipped).
   Keep that split intact.
6. **Both devices now run the SAME 36 V DRV8871 stage — and the button device's migration is
   unproven on hardware.** It previously drove pins 2/3 directly at a 585.9 kHz carrier for an
   open-circuit full scale of ~5.7 Vpp; full-scale `int16` now means the ~36 V rail, so the
   baked-in levels (0.90 volley / 0.45 loc) mean roughly 32 V / 16 V — about a **six-fold**
   output jump. Note the ceiling is the **rail, ~36 V, not twice it**: the eel EOD is monophasic
   and `out_write()` sign-splits, so one bridge drives while the other is braked to 0 V. A
   "~72 Vpp differential" figure (which older revisions of the docs carried) would require a
   *biphasic* signal driving both bridges in opposite directions at once, which never happens —
   do not reintroduce it. That is the intended consequence of the 36 V decision, not a bug, but the
   button firmware **must not** run on the old direct-pin hardware: new 36 V hardware has to be
   built, then the level re-scoped and set with `MASTER_GAIN` before it goes near a fish.
7. **Firmware is bench-owned.** Do not claim it was flashed or field-tested (the hardware
   itself is built and fixed). `make check` proves it compiles, not that it works in water.
8. **The Arduino build has three sharp edges. Respect all of them.**
   - **`_amalgam.cpp` must live in `host_test/`, never at a sketch root.** The Arduino build
     compiles *every* `.cpp` in the sketch root (and recursively under `src/`) but ignores
     other subdirectories — a stray `_amalgam.cpp` at the root would be compiled into the real
     firmware and collide with the `.ino`'s `setup()`/`loop()`. In `host_test/` it is invisible
     to Arduino and still reachable by `check.sh`'s syntax check.
   - **Exactly ONE reachable `eel_stimuli.h` per sketch.** `#pragma once` dedupes by file
     identity, so two *distinct* copies both get included and every `#define`/typedef/extern
     redefines — a hard compile error, not a warning. Each sketch reaches the library through
     its own `src/eel_core/` and nowhere else.
   - **Include `out_hal.h` from exactly one translation unit per sketch** (the `.ino`). Its
     noise-shaper accumulators are file-static; two including TUs would silently keep two
     divergent shaper states.

9. **The RC pulse log is a PRECONDITION FOR OUTPUT, and its format is pinned by a golden file.**
   `firmware/eel_core/pulse_log.h` logs **one row per emitted pulse** (localization, marker and
   volley alike) with the exact 50 kHz sample tick. Four things are load-bearing:
   - **No working log ⇒ no stimulation**, at boot *and* mid-session. The localization train is
     built to be indistinguishable from a real eel, so an unlogged pulse silently poisons the
     recording. But **nothing in flight is ever truncated** — a marker/volley/sham already
     playing runs to completion, and localization stops at a pulse boundary; only playback
     *starts* are gated. A truncated volley is an artefact that looks like data.
   - **The ISR NEVER touches the card, and is the ring's ONLY producer.** `loop()` is the
     consumer and must never push. An event that originates in `loop()` (e.g. RC link state) is
     published as a volatile word and *watched* by the ISR, which pushes the record itself —
     the same publish/latch idiom as `g_trig_seq`/`g_trig_seen`. Do not "simplify" this by
     pushing from `loop()`.
   - **Absent fields render as EMPTY CSV columns, never as a number.** `STIM_ITEMS[0]` is a real
     recorded volley, so a `0` default would misattribute every localization and marker pulse;
     and `-1` is no safer as a *written* value, because `STIM_ITEMS[-1]` does not raise in
     Python, it quietly returns the last item. `-1` is the in-memory sentinel only.
   - **`tests/data/pulse_log_golden.csv` is generated, not authored.** `pulse_log_selftest
     --emit` produces it through the real firmware formatters and `tests/test_pulse_log.py`
     parses it with the real Python reader; `check.sh` diffs it. Changing the format means
     editing `pulse_log.h`, regenerating the golden, and updating `src/fakefish/pulse_log.py` —
     the gate fails if you do only one. Sizing constants (`PULSELOG_RING_SIZE`, flush and
     anchor periods) live in the header, **not** in `shared/stim_constants.json`, which
     invariant 2 reserves for playback/session values.

   Full rationale, the file format, and the log↔recording alignment procedure (including the
   clock-drift caveat) are in `firmware/README.md` → "Pulse logging".

10. **The volley model is VENDORED, not fitted here — and the pool size is capped by the
    pulse log, not by flash.** `src/fakefish/volley_model.py` and
    `data/volley_model_params.json` are **byte-identical copies** from
    `eeltracker/analyses/volley_dynamics/`, where the model was fitted to the 200 strongest
    hunting volleys in the FLONA 2025 dataset and where `volley_validate.py` is its regression
    test. `docs/VOLLEY_GENERATIVE_SPEC.md` is the shipped spec. **Never edit any of the three**
    — a copy has no merge base, so an in-place edit is silently reverted by the next drop.
    Change the model upstream and re-drop all three, updating `VENDORED_SHA256` in
    `tests/test_volley_model.py` in the same commit; that test is what makes this a gate rather
    than a comment.

    `synthetic_volleys.py` owns only the wiring between the model's event series and the item
    table, and three parts of it are load-bearing:

    - **`SYNTH_MIN_IPI_SAMP = 100` (2.00 ms).** Set by the EOD's *energy width* (99 % of
      `EOD_HV`'s energy is inside 1.92 ms), **not** its 2.62 ms length. Measured: a 2.0 ms floor
      gives zero overlap-clip; unclamped, 3 % of volleys clip. It is also the source detector's
      own resolution limit, so the model has no support below it. The retired 2.5 ms floor and
      its matching 381 Hz "physics ceiling" cost 11 % of the sustained peak for nothing.
    - **Per-volley amplitude normalisation.** The model's amplitude is relative to a volley's
      *median* pulse and exceeds 1.0 at onset; `rel_amp` encodes 0..1 in a byte. Normalising to
      the volley's own peak keeps the measured envelope shape and discards only the absolute
      level — which the spec (§5) says is not a trustworthy measurement anyway. There is
      deliberately **no floor** on the envelope; the loc-vs-volley separation is a firmware
      level instead (`VOLLEY_AMP_RATIO` 4, i.e. localization at a quarter).
    - **`N_SYNTH_VOLLEYS = 100` is bounded by `pulse_log.h`, not flash.** The log stores the
      library item in an `int8_t` with `-1` as the absent sentinel, so the whole library must
      stay under **128 items**; at 100 volleys the highest index is 112 and the packet is
      37.4 kB of a 131 kB budget. Growing past 128 is a **log format change** (invariant 9) plus
      its golden file — not a bump of this constant. `RC_VOLLEY_ITEM_COUNT` must track it.

    Localization is **not** covered by this model (its §1.3: one call is one volley), and its
    §2.3 between-volley rate is explicitly an upper bound. `LOC_SYNTH_RATES_HZ` is therefore a
    designed ladder, marked in the source as the seam where a fitted localization model lands.

## Toolchain conventions

- **Path resolution** goes through `src/fakefish/_resources.py`: it finds the repo root by
  walking up to the dir containing **`firmware/eel_core`** (the canonical core — *not* a
  sketch), then exposes `FIRMWARE_DIR`, `DEFAULT_FIRMWARE`
  (`firmware/eel_core/eel_stimuli.cpp`), `DEFAULT_CONFIG`, `DATA_DIR`, `FIGS_DIR`. Tools
  resolve inputs from `data/` and write regenerable figures to `figs/` (gitignored). Do not
  re-introduce CWD-relative `tools/...` path literals, and never point a tool at a sketch's
  `src/eel_core/` copy.
- **Console scripts** are declared in `pyproject.toml [project.scripts]`, each pointing at a
  module `:app` Typer entry point:
  - codegen (no dataset): `fakefish-gen-constants`
  - regeneration (**needs the source recordings + `--group export`**): `fakefish-export`,
    and `fakefish-synth-volleys analyze` (QC population only — `synthesize` and `compare` run
    off the vendored model and the committed caches, no recordings)
  - reading a device's SD log (no dataset, no library): `fakefish-pulse-log`
  - against the committed library (no dataset): `fakefish-render`, `fakefish-build-card`,
    `fakefish-simulate`, `fakefish-gallery-volley`, `fakefish-gallery-localization`,
    `fakefish-gallery-loc-volley`, `fakefish-anatomy`
- **Figures** are built with the vendored deck figure system in `src/fakefish/viz/`
  (`plotstyle` page geometry + colours, `figsave.save_figure`). Never pass `figsize=` or a
  non-300 `dpi=` (`FIGURE_DPI == PUBLICATION_DPI == DIAGNOSTIC_DPI == 300`). The deck font is
  **Inter**, with `Noto Sans` / `DejaVu Sans` fallbacks — if Inter is not installed figures
  still render (in the fallback font); matplotlib needs a writable font cache (`MPLCONFIGDIR`).
- **Logging** via `from fakefish.viz.loggers import get_logger, configure_logging`
  (stdlib; verbosity 0=WARNING, 1=INFO, 2+=DEBUG).
- **ruff excludes `firmware/`** (it is C/C++). `make gen` / `make sync` / `make test` /
  `make lint` are front doors for the individual steps. **`make figs`** rebuilds every figure
  in the gitignored `figs/` from the committed library (no recordings); the one figure step it
  omits is `fakefish-synth-volleys analyze`, which refreshes the real-volley QC population and
  does need the recordings + `--group export`.

## Validate before committing

Run the gate: **`make check`** (i.e. `bash check.sh`). Four groups, ordered by how fast they
fail; each must print `ok`:

1. **generated files are in sync** — `fakefish-gen-constants --check` (JSON → `stim_levels.h`
   + `_constants.py`), then `firmware/sync_core.sh` must leave **no** `git diff` under
   `firmware/*/src/eel_core/*` **and** leave no *untracked* file there. The untracked check
   matters because `sync_core.sh` does `rm -rf && cp`: a sketch copy that was never committed
   is silently recreated on every run, so the diff stays clean while a fresh clone fails to
   compile. (Both pathspecs need the trailing `/*` — a git pathspec containing a wildcard is
   fnmatch'd against the FULL path rather than treated as a directory prefix, so the older
   `firmware/*/src/eel_core` form matched nothing and this step was blind until 2026-08-20.
   `tests/test_firmware_sync.py` compares bytes in Python and was enforcing invariant 1
   throughout.)
2. **host self-tests** (pure logic, PC `g++ -std=c++17 -Wall -Wextra`) —
   `sd_player_selftest`, `pulse_log_selftest`, `rc_control_selftest`, `panel_control_selftest`,
   `button_control_selftest` must each print exactly `OK`. **`eel_player_selftest` is a sample
   DUMPER, not an assertion suite — it never prints `OK`**; it streams one item's samples for
   diffing against the Python reference, and the gate only requires a clean build, exit 0 and
   >1000 lines of output. This group also re-emits `tests/data/pulse_log_golden.csv` from
   `pulse_log_selftest --emit` and fails on any diff (invariant 9).
3. **Teensy compile, per sketch** — `arm-none-eabi-g++ -fsyntax-only -std=gnu++17 -Wall
   -Wextra` through each sketch's `host_test/_amalgam.cpp`, for `eel_fakefish_button`,
   `eel_fakefish_rc` and `rc_input_test`. Must be warning-free. `arduino-cli` is not installed
   here, so this is a syntax check, not a link. Override `TEENSY_GXX` / `TEENSY_ROOT` if the
   Teensyduino toolchain lives elsewhere.
4. **python** — `uv run pytest -q` (tests use `tmp_path`; no dataset) and `uv run ruff check .`.

Workflow reminders that keep group 1 green:

- edited `shared/stim_constants.json` → `uv run fakefish-gen-constants`, commit **both**
  generated files;
- edited `firmware/eel_core/` **or** re-ran `fakefish-export` → `bash firmware/sync_core.sh`,
  commit every sketch's `src/eel_core/`.

Smoke-run a tool that reads the committed library when touching the toolchain (e.g.
`uv run fakefish-render info`).

**What the gate does NOT cover:** flashing a real Teensy 4.1 and scoping the output. That is
the owner's bench step and is never claimed done here — especially for the button device,
whose 36 V hardware still has to be built (invariant 6).