#!/usr/bin/env bash
# sync_core.sh — materialise the canonical core into every sketch that bundles it.
#
# firmware/eel_core/ is the SINGLE SOURCE OF TRUTH for the shared L1 (output HAL) and L2
# (sample producers) code. Arduino has no include path outside a sketch folder, so each
# sketch carries a COMMITTED COPY at <sketch>/src/eel_core/ — that is what makes every
# sketch self-contained (IDE-open, arduino-cli, or rsync-to-bench all work with zero config).
#
# This script is the only sanctioned way to produce those copies. Edit firmware/eel_core/,
# run this, commit the result. A test asserts that running it leaves NO `git diff` — so a
# hand-edited sketch copy, or a core edit that was never synced, fails the gate.
#
# Copies *.h and *.cpp from firmware/eel_core/ (NON-recursive: host_test/ is deliberately
# excluded — the host tests build against the canonical core, never against a sketch copy).
#
# Usage:  bash firmware/sync_core.sh          # from anywhere; paths resolve from this file
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
core="$here/eel_core"

if [ ! -d "$core" ]; then
  echo "sync_core.sh: canonical core not found at $core" >&2
  exit 1
fi

shopt -s nullglob

core_files=("$core"/*.h "$core"/*.cpp)
if [ ${#core_files[@]} -eq 0 ]; then
  echo "sync_core.sh: no .h/.cpp in $core — nothing to sync" >&2
  exit 1
fi

synced=0
for sketch in "$here"/eel_fakefish_*/; do
  dest="${sketch}src/eel_core"
  rm -rf "$dest"
  mkdir -p "$dest"
  cp -p "${core_files[@]}" "$dest/"
  echo "synced -> ${dest#$here/}"
  synced=$((synced + 1))
done

if [ "$synced" -eq 0 ]; then
  echo "sync_core.sh: no firmware/eel_fakefish_*/ sketches yet — nothing to sync"
fi
