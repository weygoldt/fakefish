#!/usr/bin/env bash
# check.sh — the full acceptance gate for this repo. `make check` runs this.
#
# Four groups, in order of how fast they fail:
#   1. codegen + core-sync are idempotent   (a stale generated file or an unsynced core)
#   2. host self-tests                      (pure logic, PC g++)
#   3. Teensy syntax compile per sketch     (the ISR/HAL/include paths a host test can't reach)
#   4. python                               (pytest + ruff)
#
# What this gate does NOT cover: flashing a real Teensy 4.1 and scoping the output. That is the
# owner's bench step and is never claimed done here. arduino-cli is not installed, so group 3 is
# an -fsyntax-only compile with the real arm-none-eabi-g++ rather than a full link.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

TEENSY_ROOT="${TEENSY_ROOT:-$HOME/.arduino15/packages/teensy/hardware/avr/1.60.0}"
TEENSY_GXX="${TEENSY_GXX:-$HOME/.arduino15/packages/teensy/tools/teensy-compile/11.3.1/arm/bin/arm-none-eabi-g++}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0
pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail + 1)); }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# Run a command, reporting pass/fail; show its output only when it fails.
try() {
  local label="$1"; shift
  local out
  if out="$("$@" 2>&1)"; then pass "$label"; else bad "$label"; printf '%s\n' "$out" | sed 's/^/       /'; fi
}

# Compile a host self-test and require it to print exactly OK.
run_selftest() {
  local label="$1" bin="$TMP/$2"; shift 2
  local out
  if ! out="$(g++ -std=c++17 -Wall -Wextra "$@" -o "$bin" 2>&1)"; then
    bad "$label (compile)"; printf '%s\n' "$out" | sed 's/^/       /'; return
  fi
  if ! out="$("$bin" 2>&1)"; then
    bad "$label (run)"; printf '%s\n' "$out" | sed 's/^/       /'; return
  fi
  if [ "$out" != "OK" ]; then
    bad "$label (expected OK, got: $out)"; return
  fi
  pass "$label"
}

# -fsyntax-only compile of one sketch through its host_test/_amalgam.cpp. The Teensy headers go
# in with -isystem so -Wall -Wextra reports OUR code, not the vendored libraries.
#
# The part is a parameter because the firmware is meant to build UNCHANGED for a Teensy 4.1
# and a Teensy 3.5: the output stage's pinout was chosen to exist on both (config.h), and the
# PWM source clock is derived per part. That is easy to break from either side — a 4.1-only
# pin, or a constant pinned to the 4.1's clock — and neither shows up in a 4.1-only build. So
# both parts are compiled here.
syntax_check_sketch() {
  local sketch="$1" part="${2:-teensy41}"
  local out core cpu label
  case "$part" in
    teensy41)
      core=teensy4; label="4.1"
      cpu="-mcpu=cortex-m7 -mthumb -mfloat-abi=hard -mfpu=fpv5-d16
           -D__IMXRT1062__ -DARDUINO_TEENSY41 -DF_CPU=600000000" ;;
    teensy35)
      core=teensy3; label="3.5"
      cpu="-mcpu=cortex-m4 -mthumb -mfloat-abi=hard -mfpu=fpv4-sp-d16
           -D__MK64FX512__ -DARDUINO_TEENSY35 -DF_CPU=120000000" ;;
    *) bad "syntax_check_sketch: unknown part '$part'"; return ;;
  esac
  # shellcheck disable=SC2086
  if out="$("$TEENSY_GXX" -fsyntax-only -std=gnu++17 -Wall -Wextra $cpu \
        -DUSB_SERIAL -DLAYOUT_US_ENGLISH -DARDUINO=10819 -DTEENSYDUINO=160 \
        -isystem"$TEENSY_ROOT/cores/$core" \
        -isystem"$TEENSY_ROOT/libraries/SD/src" \
        -isystem"$TEENSY_ROOT/libraries/SdFat/src" \
        -isystem"$TEENSY_ROOT/libraries/SPI" \
        -I"firmware/$sketch" \
        "firmware/$sketch/host_test/_amalgam.cpp" 2>&1)" && [ -z "$out" ]; then
    pass "$sketch compiles for Teensy $label (-fsyntax-only, -Wall -Wextra clean)"
  else
    bad "$sketch compiles for Teensy $label"; printf '%s\n' "$out" | sed 's/^/       /'
  fi
}

# ===== 1. codegen + core-sync are idempotent ===============================
step "1. generated files are in sync with their sources"

try "JSON sources -> stim_levels.h + _constants.py + loc_model_params.h" \
    uv run fakefish-gen-constants --check

# NOTE THE TRAILING '/*' IN THESE PATHSPECS — it is load-bearing. A git pathspec containing a
# wildcard is matched with fnmatch against the FULL path, not treated as a directory prefix, so
# 'firmware/*/src/eel_core' matches nothing at all and the check silently passes on any diff.
# (It was written without it and was blind until 2026-08-20; invariant 1 was still enforced by
# tests/test_firmware_sync.py, which compares bytes in Python, but this step was not enforcing
# it.) Both forms are verified below to FAIL on a hand-edited copy.
CORE_COPIES='firmware/*/src/eel_core/*'
if bash firmware/sync_core.sh >/dev/null 2>&1; then
  if git diff --quiet -- "$CORE_COPIES"; then
    pass "firmware/eel_core -> each sketch's src/eel_core (sync_core.sh leaves no diff)"
  else
    bad "sync_core.sh produced a diff — a sketch's src/eel_core was hand-edited, or the core changed without a re-sync. Run 'bash firmware/sync_core.sh' and commit."
    git --no-pager diff --stat -- "$CORE_COPIES" | sed 's/^/       /'
  fi
  # `git diff` only sees TRACKED files, and sync_core.sh does `rm -rf $dest; cp` — so a sketch
  # copy that was never committed is silently recreated on every run and the diff above stays
  # clean. A new file in the canonical core would then be invisible to the gate while the
  # sketches fail to compile for anyone who clones the repo. Untracked copies are the signal.
  untracked="$(git ls-files --others --exclude-standard -- "$CORE_COPIES")"
  if [ -z "$untracked" ]; then
    pass "every synced src/eel_core copy is tracked by git"
  else
    bad "a synced src/eel_core copy is UNTRACKED — 'git add' it, or a fresh clone will not compile."
    printf '%s\n' "$untracked" | sed 's/^/       /'
  fi
else
  bad "sync_core.sh failed to run"
fi

# ===== 2. host self-tests ==================================================
step "2. host self-tests (pure logic, PC g++)"

run_selftest "eel_core     sd_player_selftest"     sdp \
    firmware/eel_core/host_test/sd_player_selftest.cpp
# The fitted resting rhythm, replayed against the Python reference. Its oracle is
# tests/data/loc_rhythm_golden.csv — generated by src/fakefish/loc_model.py with the noise
# draws injected, so the two implementations can be compared state for state despite having
# no shared generator. tests/test_loc_model.py holds the golden to the Python; this holds the
# C to the golden. Neither side can move alone, which is the whole point: the spec's own
# hand-written C listing disagrees with the parameters it ships beside.
run_selftest "eel_core     loc_rhythm_selftest"     locr \
    firmware/eel_core/host_test/loc_rhythm_selftest.cpp -lm
run_selftest "eel_core     pulse_log_selftest"     plog \
    firmware/eel_core/host_test/pulse_log_selftest.cpp
# The L1 duty mapping. Needs eel_stimuli.cpp because it sweeps the REAL EOD_HV at every level the
# devices use, asserting that a zero sample brakes BOTH boards to duty 0 — the pedestal regression
# guard (config.h -> "The driver dead zone").
run_selftest "eel_core     out_hal_selftest"       outhal \
    firmware/eel_core/host_test/out_hal_selftest.cpp firmware/eel_core/eel_stimuli.cpp

# The pulse-log GOLDEN is the single artifact shared by the firmware writer and the Python
# reader: this binary emits it through the real formatters, and tests/test_pulse_log.py parses
# the committed copy. Regenerating and diffing here is what stops the two sides drifting apart
# — a format change on either side without the other fails right now instead of in the field.
if [ -x "$TMP/plog" ]; then
  if "$TMP/plog" --emit 2>/dev/null | diff -q - tests/data/pulse_log_golden.csv >/dev/null 2>&1; then
    pass "pulse-log golden is current (tests/data/pulse_log_golden.csv)"
  else
    bad "tests/data/pulse_log_golden.csv is stale — regenerate it (see firmware/README.md, 'Pulse logging')"
    "$TMP/plog" --emit 2>/dev/null | diff - tests/data/pulse_log_golden.csv | head -20 | sed 's/^/       /'
  fi
else
  bad "pulse_log_selftest did not build — cannot verify the golden log"
fi
run_selftest "rc surface   rc_control_selftest"    rc \
    firmware/eel_fakefish_rc/src/eel_core/eel_stimuli.cpp \
    firmware/eel_fakefish_rc/host_test/rc_control_selftest.cpp -lm
run_selftest "rc surface   panel_control_selftest" panel \
    firmware/eel_fakefish_rc/host_test/panel_control_selftest.cpp
run_selftest "btn surface  button_control_selftest" btn \
    firmware/eel_fakefish_button/host_test/button_control_selftest.cpp

# eel_player_selftest is BOTH a sample dumper and an assertion suite, from one binary.
#   --verify  plays the whole library (both polarities, four amplitudes, plus the windowed
#             and looping paths) against a frozen ORACLE — the ring-buffer overlap-add
#             engine eel_player used before it became a per-tick sum — and requires exact
#             agreement. This is what makes a change to the engine fail the gate rather
#             than only a human's before/after diff.
#   <item>    streams one item's samples for diffing against the Python reference.
# NOTE the assertion suite runs on a PC, which has no FMA; it therefore cannot see a change
# to the SUMMATION ORDER, which does move samples on the Teensy. See eel_player.cpp.
if out="$(g++ -std=c++17 -Wall -Wextra -I firmware/eel_core \
        firmware/eel_core/eel_player.cpp firmware/eel_core/eel_stimuli.cpp \
        firmware/eel_core/host_test/eel_player_selftest.cpp -lm -o "$TMP/eng" 2>&1)"; then
  if out="$("$TMP/eng" --verify 2>&1)" && [ "${out##*$'\n'}" = "OK" ]; then
    pass "eel_core     eel_player_selftest --verify (engine == oracle over the whole library)"
  else
    bad "eel_player_selftest --verify"; printf '%s\n' "$out" | head -20 | sed 's/^/       /'
  fi
  n="$("$TMP/eng" 0 | wc -l)"
  if [ "$n" -gt 1000 ]; then
    pass "eel_core     eel_player_selftest (dumper: $n samples for item 0)"
  else
    bad "eel_player_selftest produced only $n samples"
  fi
else
  bad "eel_player_selftest (compile)"; printf '%s\n' "$out" | sed 's/^/       /'
fi

# ===== 3. Teensy syntax compile per sketch =================================
step "3. Teensy compile (arm-none-eabi-g++ -fsyntax-only)"

if [ ! -x "$TEENSY_GXX" ]; then
  bad "Teensy compiler not found at $TEENSY_GXX (set TEENSY_GXX / TEENSY_ROOT)"
else
  for part in teensy41 teensy35; do
    syntax_check_sketch eel_fakefish_button "$part"
    syntax_check_sketch eel_fakefish_rc     "$part"
    syntax_check_sketch rc_input_test       "$part"   # bring-up diagnostic (bundles no core)
  done
fi

# ===== 4. python ===========================================================
step "4. python"

try "pytest" uv run pytest -q
try "ruff"   uv run ruff check .

# ===== verdict =============================================================
if [ "$fail" -eq 0 ]; then
  printf '\n\033[32mall checks passed\033[0m — bench flash + scope remain the owner'"'"'s step.\n'
  exit 0
fi
printf '\n\033[31m%d check(s) FAILED\033[0m\n' "$fail"
exit 1
