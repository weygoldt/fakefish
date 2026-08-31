"""Guard the RC gallery's claim that it draws *the device's* volleys.

``fakefish-gallery-volley --rc`` exists to answer one question: what can the remote-control
playback device actually put in the water? That is narrower than the stimulus library —
the library also holds the real-volley fragments and the localization trains, and the RC
sketch draws from neither. The window it does draw from is declared once, in the sketch's
own ``rc_control.h``, and the gallery reads it from there rather than repeating it.

These tests fail if that window ever stops meaning what the gallery says it means: if the
constants move, if the window grows to cover an item that is not a synthetic volley, or if
an item inside it loses the per-pulse envelope the panels plot.
"""

from __future__ import annotations

import re

import pytest

from fakefish import _resources as _res
from fakefish import export_teensy_stimuli as ex
from fakefish.plot_volley_gallery import _rc_pool

RC_HEADER = _res.ROOT / "firmware" / "eel_fakefish_rc" / "rc_control.h"


@pytest.fixture(scope="module")
def items():
    return ex.parse_firmware(_res.DEFAULT_FIRMWARE)["items"]


def test_pool_is_read_from_the_sketch_not_guessed():
    """The helper must report exactly what the firmware header declares."""
    txt = RC_HEADER.read_text()
    want = {}
    for key in ("RC_VOLLEY_ITEM_FIRST", "RC_VOLLEY_ITEM_COUNT"):
        m = re.search(rf"^#define\s+{key}\s+(\d+)", txt, re.M)
        assert m is not None, f"{key} vanished from {RC_HEADER}"
        want[key] = int(m.group(1))
    assert _rc_pool(_res.ROOT) == (
        want["RC_VOLLEY_ITEM_FIRST"],
        want["RC_VOLLEY_ITEM_COUNT"],
    )


def test_pool_fits_inside_the_library(items):
    first, count = _rc_pool(_res.ROOT)
    assert first >= 0 and count > 0
    assert first + count <= len(items), "the RC draw would index past STIM_ITEMS"


def test_every_drawable_item_is_a_synth_volley_with_an_envelope(items):
    """The gallery titles each panel with an envelope range, so every item must carry one.

    This is also the blinding guarantee in disguise: the trial draws uniformly from this
    window, so if a non-volley ever fell inside it the device would sometimes play a
    localization train as a VOLLEY arm.
    """
    first, count = _rc_pool(_res.ROOT)
    for idx in range(first, first + count):
        it = items[idx]
        assert it["kind"] == ex.STIM_SYNTH_VOLLEY, f"item {idx} is kind {it['kind']}"
        assert it["rel_amp"] is not None, f"item {idx} has no per-pulse envelope"
        assert it["rel_amp"].max() <= 255 and it["rel_amp"].min() >= 0


def test_pool_excludes_the_real_volleys_and_localization_items(items):
    """The panels the RC device can never play must stay out of the gallery."""
    first, count = _rc_pool(_res.ROOT)
    outside = set(range(len(items))) - set(range(first, first + count))
    assert outside, "expected library items outside the RC window"
    kinds = {items[i]["kind"] for i in outside}
    assert ex.STIM_SYNTH_VOLLEY not in kinds, (
        "a synthetic volley sits outside the RC window — the gallery would omit a "
        "volley the pool was meant to contain"
    )
