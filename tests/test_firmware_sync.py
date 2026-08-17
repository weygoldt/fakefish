"""Guard the committed core copies inside each sketch.

``firmware/eel_core/`` is the single source of truth for the shared L1 (output HAL) and L2
(sample producer) code, but Arduino has no include path outside a sketch folder — so each
sketch carries a COMMITTED copy at ``<sketch>/src/eel_core/``, produced by
``firmware/sync_core.sh``. That duplication is what makes every sketch self-contained
(IDE-open, arduino-cli, or rsync-to-bench, all zero-config).

These tests are what stop the copies from becoming forks: a hand-edit to a sketch's
``src/eel_core``, or a change to the canonical core that was never re-synced, fails here.

Deliberately NON-MUTATING: this compares bytes rather than running ``sync_core.sh``, so a
test run never rewrites the working tree (``check.sh`` runs the script itself and diffs).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakefish import _resources as _res

FIRMWARE = _res.ROOT / "firmware"
CORE = FIRMWARE / "eel_core"

#: Every sketch that bundles the core. Kept as a glob so a new surface is covered the day
#: it is added, rather than the day someone remembers to update this list.
SKETCHES = sorted(p for p in FIRMWARE.glob("eel_fakefish_*") if p.is_dir())

#: What sync_core.sh copies: top-level sources only. host_test/ is excluded on purpose —
#: the tests are not compiled into the sketch.
CORE_FILES = sorted(p.name for p in CORE.iterdir() if p.suffix in {".h", ".cpp"})


def test_there_are_sketches_to_check():
    """A glob that silently matches nothing would make every test below vacuously pass."""
    assert SKETCHES, "no firmware/eel_fakefish_*/ sketches found"
    assert CORE_FILES, "firmware/eel_core/ has no .h/.cpp to sync"


@pytest.mark.parametrize("sketch", SKETCHES, ids=lambda p: p.name)
def test_sketch_core_copy_is_complete(sketch: Path):
    """The sketch's src/eel_core must hold exactly the canonical file set — no more, no less."""
    dest = sketch / "src" / "eel_core"
    assert dest.is_dir(), f"{dest} missing — run `bash firmware/sync_core.sh`"
    have = sorted(p.name for p in dest.iterdir() if p.is_file())
    assert have == CORE_FILES, (
        f"{dest} does not match firmware/eel_core — run `bash firmware/sync_core.sh` "
        f"and commit the result"
    )


@pytest.mark.parametrize("sketch", SKETCHES, ids=lambda p: p.name)
def test_sketch_core_copy_is_byte_identical(sketch: Path):
    """Every synced file must be byte-identical to the canonical core."""
    dest = sketch / "src" / "eel_core"
    for name in CORE_FILES:
        assert (dest / name).read_bytes() == (CORE / name).read_bytes(), (
            f"{dest / name} has DRIFTED from firmware/eel_core/{name}. Never edit a sketch's "
            f"src/eel_core — edit firmware/eel_core/ and run `bash firmware/sync_core.sh`."
        )


@pytest.mark.parametrize("sketch", SKETCHES, ids=lambda p: p.name)
def test_sketch_has_no_second_copy_of_a_core_header(sketch: Path):
    """A sketch-local copy of a core header would shadow the synced one.

    Two reachable copies of e.g. eel_stimuli.h is not a warning — ``#pragma once`` does not
    dedupe distinct files, so the second copy is a hard compile error (conflicting enum /
    typedef declarations). Catch it here with a clear message instead.
    """
    stray = [p.name for p in sketch.iterdir() if p.is_file() and p.name in CORE_FILES]
    assert not stray, (
        f"{sketch.name} has its own copy of {stray}, which shadows src/eel_core. "
        f"Delete it — the core is included as \"src/eel_core/<name>\"."
    )


@pytest.mark.parametrize("sketch", SKETCHES, ids=lambda p: p.name)
def test_sketch_amalgam_is_not_in_the_sketch_root(sketch: Path):
    """The Arduino build compiles every .cpp in the sketch root.

    _amalgam.cpp pulls the whole .ino into one translation unit for the -fsyntax-only gate;
    if it sat in the sketch root it would be built into the real firmware and collide with
    the sketch's own setup()/loop(). It belongs in host_test/, which Arduino ignores.
    """
    assert not (sketch / "_amalgam.cpp").exists(), (
        f"{sketch.name}/_amalgam.cpp would be compiled into the firmware — move it to host_test/"
    )
    assert (sketch / "host_test" / "_amalgam.cpp").exists(), (
        f"{sketch.name} has no host_test/_amalgam.cpp — the Teensy syntax gate cannot run"
    )
