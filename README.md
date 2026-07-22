# fakefish

Play back an electric fish's EOD (electric organ discharge) into water with a
**Teensy 4.1** driving a dipole. It is a **hand-held field stimulator**: you hold
it, point the electrodes at a wild fish, and press one of six buttons to play a
stimulus. Only the electrodes go in the water — the buttons and the indicator LED
are dry operator controls in your hand.

The library shipped here is electric-**eel** (*Electrophorus*) discharges, but the
firmware plays back any species' EOD table; for *weakly* electric fish the Teensy's
own voltage is enough to drive the whole system.

This repo has two independent halves:

- **`firmware/`** — a self-contained Arduino sketch. Flashing needs **no Python and
  no dataset**. This is all you need to run the stimulator.
- **`src/fakefish/`** — a Python toolchain (dev tooling) to regenerate, render,
  simulate, and visualise the stimulus library. Optional.

---

## Quickstart — build a card → flash → play

1. **Build the SD card** (needs the Python toolchain but no dataset — it renders from the
   committed library): `uv run fakefish-build-card --out /path/to/sdcard`, then put the
   microSD in the Teensy 4.1's built-in slot.
2. Open **`firmware/eel_fakefish/`** in the Arduino IDE (open the *folder* — every source
   lives inside it, nothing to copy). Select **Teensy 4.1**. **Compile & upload.**

Six one-shot program buttons (A calibration · B localization · C volley ·
D localization→volley · E spare · F song) and a pin-13 indicator LED (solid = playing,
~1 Hz blink = no card). Each stimulus is a WAV under one directory per button on the card;
reassign a button by moving WAVs between directories — no reflash. Full instructions, the
card layout, and the design rationale are in
**[`firmware/README.md`](firmware/README.md)** — the crown-jewel doc.

> ⚠️ The firmware has **not** been bench-flashed with the latest changes. Test on a
> real Teensy 4.1 before field use.

---

## The Python toolchain (optional)

Regenerate / inspect the stimulus library. Requires an editable install; the
toolchain is meant to run from the repo root.

```sh
uv sync            # or: pip install -e . && pip install -e '.[dev]'
```

### Runs against the committed files — no dataset needed

The shipped `firmware/eel_fakefish/eel_stimuli.{h,cpp}` is already generated, so
these parse it directly:

```sh
uv run fakefish-render info                 # list the library items
uv run fakefish-gallery-volley              # voltage-over-time gallery of every volley
uv run fakefish-gallery-localization        # localization-train gallery
uv run fakefish-gallery-loc-volley          # program-D (loc→volley) gallery
uv run fakefish-anatomy                     # marker → gap → onset playback anatomy
uv run fakefish-simulate marker             # model the Teensy PWM+RC marker output
uv run pytest                               # the export/engine unit tests
```

Figures land in `figs/` (gitignored).

### Regeneration — needs the source recordings (NOT shipped)

`fakefish-export` reads the source NIX recordings (a field-data workspace set in
`data/stimuli_config.yaml → paths.eods_root`) and re-emits the firmware library +
provenance. `fakefish-synth-volleys` fits/synthesises the volley population. Neither
is needed to flash; both require data this repo does not ship.

```sh
uv run fakefish-export scan   --config data/stimuli_config.yaml
uv run fakefish-export export --config data/stimuli_config.yaml
uv run fakefish-synth-volleys analyze
```

---

## Layout

```
firmware/
  eel_fakefish/          the Arduino sketch — open THIS in the IDE
    eel_fakefish.ino     Teensy 4.1 driver (50 kHz clock, dual PWM + RC = DAC)
    sd_player.h          SD WAV streaming (parse, ring buffer, per-button random pick)
    eel_control.h        6 program buttons + button→dir map + indicator LED
    eel_stimuli.{h,cpp}  the stimulus-library SOURCE build_sd_card renders → WAVs (frozen)
    eel_player.{h,cpp}   (retired) old on-device overlap-add engine — see firmware/README.md
    host_test/           PC self-tests for the streaming + control logic
  README.md              flashing + full design doc
src/fakefish/            the Python toolchain (installable package)
  build_sd_card.py       render the library (+ song) to a one-dir-per-button WAV card
  viz/                   vendored figure + logging helpers (deck figure system)
  _resources.py          repo-relative path resolution
data/                    export config + frozen provenance + tuned volley model/populations
tests/                   export + engine unit tests
```

## Notes for maintainers

- **The library, provenance, and firmware are treated as byte-frozen contracts.**
  `eel_stimuli.{h,cpp}` and `data/stimuli_provenance.json` are the shipped output;
  the toolchain regenerates them but the checked-in copies are the reference.
- **`src/fakefish/_gallery_marker.py` mirrors the firmware** (marker frequency /
  amplitude, output levels in `eel_control.h` + `eel_fakefish.ino`). The firmware is
  the source of truth — if it changes, update the mirror.
- This toolchain and firmware were extracted from the `eeltracker` analysis package.
  One provenance field (`eeltracker_git_commit` in `data/stimuli_provenance.json`)
  keeps that historical name so a regenerated provenance matches the frozen schema.
