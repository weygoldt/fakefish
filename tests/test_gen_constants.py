"""Guard the shared-constants codegen.

``shared/stim_constants.json`` is only authoritative if the checked-in generated files
actually match it. These tests are what enforce that: if someone hand-edits
``firmware/eel_core/stim_levels.h`` or ``src/fakefish/_constants.py``, or edits the JSON
without re-running ``fakefish-gen-constants``, the suite fails and names the stale file.

They also pin the load-bearing playback invariants (whole marker cycles, net-DC-zero
tone, non-overlapping localization) so a well-meaning tweak to the JSON fails loudly
here rather than quietly on a fish.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from fakefish import _constants as K
from fakefish import _resources as _res
from fakefish import build_sd_card as bc
from fakefish import gen_constants as gc


def test_generated_files_are_up_to_date():
    """Regenerating must produce byte-identical output — no `git diff` after codegen."""
    for path, expected in gc.generate().items():
        assert path.exists(), f"{path} is missing — run `uv run fakefish-gen-constants`"
        actual = path.read_text()
        assert actual == expected, (
            f"{path} is STALE. Run `uv run fakefish-gen-constants` and commit the result "
            f"(never hand-edit a generated file)."
        )


def test_codegen_is_idempotent():
    """Rendering twice from the same JSON yields the same bytes (no clock/rng/dict order)."""
    assert gc.generate() == gc.generate()


def test_marker_lut_matches_the_frozen_literal():
    """The derived LUT must reproduce the values the shipped library was authored with.

    This is the check that lets the LUT be *derived* rather than stored. 32767 is
    load-bearing — 32640 and 32768 both give a different table.
    """
    assert list(gc.marker_lut(10000, 50000)) == [0, 31163, 19260, -19260, -31163]


def test_marker_lut_has_zero_net_charge():
    """The electrodes have no DC-blocking cap, so the tone must sum to exactly zero."""
    assert int(np.sum(K.MARKER_LUT)) == 0


def test_rendered_marker_tones_have_zero_net_charge():
    """Net-DC zero must survive the raised-cosine ramp, not just hold per-cycle."""
    leadin = bc.render_marker(K.MARKER_LEADIN_SAMPLES, K.MARKER_AMPLITUDE)
    cal = bc.render_marker(K.MARKER_CAL_SAMPLES, K.MARKER_CAL_AMPLITUDE)
    assert int(np.sum(leadin.astype(np.int64))) == 0
    assert int(np.sum(cal.astype(np.int64))) == 0


def test_build_sd_card_reexports_match_the_generated_module():
    """`build_sd_card.X` must BE `_constants.X` — the re-export must not drift."""
    for name in (
        "RATE_HZ",
        "MARKER_RAMP_SAMPLES",
        "MARKER_LEADIN_SAMPLES",
        "MARKER_CAL_SAMPLES",
        "LOC_PLAYBACK_SAMPLES",
        "D_LOC_PLAYBACK_SAMPLES",
        "D_INTERPHASE_GAP_SAMPLES",
        "VOLLEY_AMPLITUDE",
        "LOC_AMPLITUDE",
        "MARKER_AMPLITUDE",
        "MARKER_CAL_AMPLITUDE",
        "FULLSCALE_PULSE_PEAK_MV",
    ):
        assert getattr(bc, name) == getattr(K, name), f"build_sd_card.{name} drifted"
    assert np.array_equal(bc.MARKER_LUT, K.MARKER_LUT)


def test_galleries_draw_the_same_levels_as_the_card():
    """CLAUDE.md invariant: the galleries must depict the levels the card actually bakes."""
    from fakefish import _gallery_marker as gm

    assert gm.VOLLEY_AMPLITUDE == bc.VOLLEY_AMPLITUDE
    assert gm.LOC_AMPLITUDE == bc.LOC_AMPLITUDE
    assert gm.MARKER_AMPLITUDE == bc.MARKER_AMPLITUDE
    assert gm.MARKER_LEADIN_S * bc.RATE_HZ == bc.MARKER_LEADIN_SAMPLES
    assert gm.D_LOC_PLAYBACK_S * bc.RATE_HZ == bc.D_LOC_PLAYBACK_SAMPLES
    assert round(gm.D_INTERPHASE_GAP_S * bc.RATE_HZ) == bc.D_INTERPHASE_GAP_SAMPLES


def test_localization_pulses_cannot_overlap():
    """locgen's single-pulse scheduler is only correct while refractory > one EOD.

    Mirrors the static_assert in firmware/eel_core/locgen.h.
    """
    c = gc.load_constants()
    refractory = c["rc_path"]["loc_rate"]["refractory_samp"]
    eod_len = int(gc.OUT_HEADER.parent.joinpath("eel_stimuli.h").read_text()
                  .split("#define EOD_HV_LEN")[1].split()[0])
    assert refractory > eod_len


def test_validate_rejects_a_non_integer_marker_cycle():
    """A frequency that does not divide the sample rate must fail loudly, not round."""
    c = gc.load_constants()
    c["sd_path"]["sine_marker"]["freq_hz"] = 7000  # 50000 / 7000 is not an integer
    with pytest.raises(ValueError, match="integer multiple"):
        gc.validate(c, gc.RATE_HZ)


def test_validate_rejects_an_out_of_range_level():
    c = gc.load_constants()
    c["sd_path"]["levels"]["volley"] = 1.5
    with pytest.raises(ValueError, match="outside 0..1"):
        gc.validate(c, gc.RATE_HZ)


def test_validate_rejects_a_marker_tag_that_does_not_fit():
    c = gc.load_constants()
    c["rc_path"]["pulse_marker"]["pulses_sham"] = 99
    with pytest.raises(ValueError, match="max_pulses"):
        gc.validate(c, gc.RATE_HZ)


def test_frozen_provenance_marker_contract_matches_the_codegen():
    """The library's DECLARED marker contract must equal what the card is BUILT with.

    ``data/stimuli_provenance.json`` is byte-frozen, so ``export_teensy_stimuli`` restates
    the marker frequency and tone durations as literals rather than importing them. That is
    correct — but it means the frozen contract and ``shared/stim_constants.json`` could
    silently disagree, and a downstream detector keys off the frozen values. This is the
    check that they never diverge.
    """
    frozen = json.loads(_res.data_file("stimuli_provenance.json").read_text())
    marker = frozen["lead_in_marker"]
    assert marker["nominal_freq_hz"] == K.MARKER_FREQ_HZ
    assert marker["leadin_s"] == K.MARKER_LEADIN_S
    assert marker["calibration_s"] == K.MARKER_CAL_S
    assert frozen["playback_sample_rate_hz"] == K.RATE_HZ


def test_source_json_is_valid_json_with_stripped_comments():
    """`_comment` keys are documentation only and must never reach the renderers."""
    raw = json.loads(gc.SOURCE_JSON.read_text())
    assert "_comment" in raw  # the file documents itself
    stripped = gc.load_constants()

    def has_comment(node) -> bool:
        if isinstance(node, dict):
            return "_comment" in node or any(has_comment(v) for v in node.values())
        if isinstance(node, list):
            return any(has_comment(v) for v in node)
        return False

    assert not has_comment(stripped)
