"""Guard the shared-constants codegen.

``shared/stim_constants.json`` is only authoritative if the checked-in generated files
actually match it. These tests are what enforce that: if someone hand-edits
``firmware/eel_core/stim_levels.h`` or ``src/fakefish/_constants.py``, or edits the JSON
without re-running ``fakefish-gen-constants``, the suite fails and names the stale file.

They also pin the load-bearing playback invariants — the marker burst's EVEN pulse count
(which is what makes it charge-balanced now that the sine LUT is gone), whole-sample pulse
intervals, a marker rate that stays clear of the volley range and cannot overlap its own
pulses, and non-overlapping localization — so a well-meaning tweak to the JSON fails loudly
here rather than quietly on a fish.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from fakefish import _constants as K
from fakefish import _resources as _res
from fakefish import build_sd_card as bc
from fakefish import export_teensy_stimuli as ex
from fakefish import gen_constants as gc

#: Constants that belonged to the RETIRED 10 kHz sine marker. They were deleted with it;
#: if any of them comes back, something has been un-migrated by hand.
RETIRED_SINE_NAMES = (
    "MARKER_LUT",
    "MARKER_FREQ_HZ",
    "MARKER_RAMP_SAMPLES",
    "MARKER_LEADIN_S",
    "MARKER_LEADIN_SAMPLES",
    "MARKER_CAL_S",
    "MARKER_CAL_SAMPLES",
    "MARKER_CAL_AMPLITUDE",
)


@pytest.fixture(scope="module")
def parsed():
    return ex.parse_firmware(_res.DEFAULT_FIRMWARE)


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


# ===== JSON -> generated module -> rendered samples ==================================
def test_the_json_marker_contract_reaches_the_rendered_burst(parsed):
    """Walk the whole chain for the marker: JSON -> _constants -> build_sd_card -> samples.

    This is what replaces the retired sine-LUT tests. The old marker's zero-net-charge
    property lived in a table of five literals; the new one is a *structural* property of
    the rendered burst (an even number of alternating, identical pulses), so the check has
    to follow the constants all the way to the int16 samples the card carries.
    """
    pm = gc.load_constants()["sd_path"]["pulse_marker"]
    assert pm["n_pulses"] == K.MARKER_N_PULSES == bc.MARKER_N_PULSES
    assert pm["rate_hz"] == K.MARKER_RATE_HZ == bc.MARKER_RATE_HZ
    assert gc.ipi_samples(pm["rate_hz"], gc.RATE_HZ) == K.MARKER_IPI_SAMPLES
    assert K.MARKER_SPAN_SAMPLES == K.MARKER_IPI_SAMPLES * (K.MARKER_N_PULSES - 1)

    burst = bc.render_pulse_marker(parsed["EOD_HV"], bc.Levels())
    assert burst.size == K.MARKER_SPAN_SAMPLES + parsed["EOD_HV"].size
    assert int(burst.astype(np.int64).sum()) == 0, "the JSON promises a charge-balanced burst"


def test_the_json_calibration_contract_reaches_the_rendered_train(parsed):
    """Same chain for program A, whose length and rate are authored in the JSON too."""
    cal = gc.load_constants()["sd_path"]["calibration"]
    assert cal["rate_hz"] == K.CAL_RATE_HZ == bc.CAL_RATE_HZ
    assert gc.ipi_samples(cal["rate_hz"], gc.RATE_HZ) == K.CAL_IPI_SAMPLES
    assert cal["duration_s"] == K.CAL_S
    assert K.CAL_SAMPLES == round(K.CAL_S * K.RATE_HZ)
    assert bc.render_calibration(parsed["EOD_HV"], bc.Levels()).size == K.CAL_SAMPLES


def test_the_retired_sine_constants_are_gone():
    """The 10 kHz sine marker was removed; none of its constants may reappear."""
    for name in RETIRED_SINE_NAMES:
        assert not hasattr(K, name), f"_constants.{name} is a retired sine-marker constant"
        assert not hasattr(bc, name), f"build_sd_card.{name} is a retired sine-marker constant"
    assert not hasattr(bc, "render_marker")  # replaced by render_pulse_marker
    assert not hasattr(gc, "marker_lut")  # the LUT is not derived any more — there is none


def test_build_sd_card_reexports_match_the_generated_module():
    """`build_sd_card.X` must BE `_constants.X` — the re-export must not drift."""
    for name in (
        "RATE_HZ",
        "MARKER_N_PULSES",
        "MARKER_RATE_HZ",
        "MARKER_IPI_SAMPLES",
        "CAL_RATE_HZ",
        "CAL_IPI_SAMPLES",
        "CAL_SAMPLES",
        "LOC_PLAYBACK_SAMPLES",
        "D_LOC_PLAYBACK_SAMPLES",
        "D_INTERPHASE_GAP_SAMPLES",
        "VOLLEY_AMPLITUDE",
        "LOC_AMPLITUDE",
        "MARKER_AMPLITUDE",
        "CAL_AMPLITUDE",
        "FULLSCALE_PULSE_PEAK_MV",
    ):
        assert getattr(bc, name) == getattr(K, name), f"build_sd_card.{name} drifted"

    # the Levels dataclass is the card's level contract: its defaults are the generated ones
    lv = bc.Levels()
    assert (lv.volley, lv.loc, lv.marker, lv.calibration) == (
        K.VOLLEY_AMPLITUDE, K.LOC_AMPLITUDE, K.MARKER_AMPLITUDE, K.CAL_AMPLITUDE
    )


def test_galleries_draw_the_same_levels_as_the_card():
    """CLAUDE.md invariant: the galleries must depict the levels the card actually bakes."""
    from fakefish import _gallery_marker as gm

    assert gm.VOLLEY_AMPLITUDE == bc.VOLLEY_AMPLITUDE
    assert gm.LOC_AMPLITUDE == bc.LOC_AMPLITUDE
    assert gm.MARKER_AMPLITUDE == bc.MARKER_AMPLITUDE
    assert gm.D_LOC_PLAYBACK_S * bc.RATE_HZ == bc.D_LOC_PLAYBACK_SAMPLES
    assert round(gm.D_INTERPHASE_GAP_S * bc.RATE_HZ) == bc.D_INTERPHASE_GAP_SAMPLES

    # Name-agnostic drift check: every constant the galleries re-export that the codegen
    # also defines must BE the generated value, never a local literal. This keeps covering
    # the marker geometry (span, pulse count, IPI) whatever the gallery chooses to draw.
    shared = [n for n in gm.__all__ if hasattr(K, n)]
    assert {"VOLLEY_AMPLITUDE", "LOC_AMPLITUDE", "MARKER_AMPLITUDE"} <= set(shared)
    for name in shared:
        assert getattr(gm, name) == getattr(K, name), f"_gallery_marker.{name} drifted"

    for name in RETIRED_SINE_NAMES:
        assert not hasattr(gm, name), (
            f"_gallery_marker.{name} still describes the retired 10 kHz sine marker — the "
            f"galleries must depict the alternating-polarity pulse burst the card bakes"
        )


# ===== The validators: every rule the JSON must obey ================================
def test_validate_rejects_an_odd_marker_pulse_count():
    """An odd burst leaves a whole EOD of net charge on cap-less electrodes."""
    c = gc.load_constants()
    c["sd_path"]["pulse_marker"]["n_pulses"] = 5
    with pytest.raises(ValueError, match="ODD"):
        gc.validate(c, gc.RATE_HZ)


def test_validate_rejects_a_burst_too_short_to_alternate():
    """0 is even, but a burst with fewer than two pulses has no alternation to detect."""
    c = gc.load_constants()
    c["sd_path"]["pulse_marker"]["n_pulses"] = 0
    with pytest.raises(ValueError, match="at least 2"):
        gc.validate(c, gc.RATE_HZ)


@pytest.mark.parametrize("group", ["pulse_marker", "calibration"])
def test_validate_rejects_a_rate_that_does_not_divide_the_sample_rate(group):
    """A rate must land on whole samples, so the train is exactly periodic — not rounded."""
    c = gc.load_constants()
    c["sd_path"][group]["rate_hz"] = 3.0  # 50000 / 3 is not an integer
    with pytest.raises(ValueError, match="does not divide"):
        gc.validate(c, gc.RATE_HZ)


def test_validate_rejects_a_marker_rate_that_would_overlap_its_pulses():
    """The burst is a sequence of DISCRETE pulses; overlap would blur the alternation."""
    eod_len = gc._eod_hv_len()
    assert eod_len, "could not read EOD_HV_LEN — the overlap check would be vacuous"
    c = gc.load_constants()
    c["sd_path"]["pulse_marker"]["rate_hz"] = 500.0  # 100-sample IPI < one EOD
    with pytest.raises(ValueError, match="overlap"):
        gc.validate(c, gc.RATE_HZ, eod_len=eod_len)


def test_validate_rejects_a_marker_rate_inside_the_volley_range():
    """Volleys peak at 300-400 Hz; a marker up there stops being distinguishable."""
    c = gc.load_constants()
    c["sd_path"]["pulse_marker"]["rate_hz"] = 200.0  # whole-sample and non-overlapping...
    with pytest.raises(ValueError, match="volley rate range"):
        gc.validate(c, gc.RATE_HZ, eod_len=gc._eod_hv_len())


def test_validate_rejects_an_out_of_range_level():
    c = gc.load_constants()
    c["sd_path"]["levels"]["volley"] = 1.5
    with pytest.raises(ValueError, match="outside 0..1"):
        gc.validate(c, gc.RATE_HZ)


def test_validate_rejects_a_returning_rc_marker():
    """The RC marker is gone, and coming back has to be a decision rather than an edit.

    Removed 2026-08-22: the per-pulse log is a precondition for output, so a trial was
    already recorded — and for a SHAM the burst was not redundant at all, it was four eel
    pulses inside the no-stimulus control. Re-adding the JSON block without re-adding an
    emitter would leave a key that silently generates nothing, so the validator refuses it
    outright and says why.
    """
    c = gc.load_constants()
    assert "pulse_marker" not in c["rc_path"], "the RC marker must stay out of the JSON"
    c["rc_path"]["pulse_marker"] = {"ipi_samp": 500, "pulses_volley": 2, "pulses_sham": 4}
    with pytest.raises(ValueError, match="no-stimulus control"):
        gc.validate(c, gc.RATE_HZ)


def test_the_sd_marker_survived_the_rc_one():
    """Deleting one marker must not delete the other.

    The SD device writes no pulse log at all, so its 6-pulse alternating lead-in is the
    only record that a playback happened — the premise that retired the RC marker simply
    does not hold there.
    """
    c = gc.load_constants()
    assert c["sd_path"]["pulse_marker"]["n_pulses"] == 6
    header = gc.render_header(c)
    assert "SD_MARKER_N_PULSES" in header
    assert "PULSE_MARKER_IPI_SAMP" not in header


def test_the_shipped_json_passes_its_own_validators():
    """The rules above are only worth having if the checked-in file actually obeys them."""
    gc.validate(gc.load_constants(), gc.RATE_HZ, eod_len=gc._eod_hv_len())


def test_localization_pulses_cannot_overlap():
    """locgen's single-pulse scheduler is only correct while refractory > one EOD.

    Mirrors the static_assert in firmware/eel_core/locgen.h.
    """
    c = gc.load_constants()
    refractory = c["rc_path"]["loc_rate"]["refractory_samp"]
    eod_len = int(gc.OUT_HEADER.parent.joinpath("eel_stimuli.h").read_text()
                  .split("#define EOD_HV_LEN")[1].split()[0])
    assert refractory > eod_len


# ===== The frozen provenance ========================================================
def test_frozen_provenance_records_the_RETIRED_sine_marker(parsed):
    """``data/stimuli_provenance.json`` describes a marker the card no longer emits.

    That file is BYTE-FROZEN — it is the shipped contract of the *library*, exported once
    from the field recordings, and it cannot be regenerated in this repo. Its
    ``lead_in_marker`` block therefore still declares the retired 10 kHz sine tone (1 s
    lead-in, 10 s calibration), which is simply history: the card is now built with an
    alternating-polarity eel-pulse burst and a pulse-train calibration, from
    ``shared/stim_constants.json``.

    So this test no longer asserts the two AGREE — they deliberately do not. It pins the
    frozen block as the retired contract (a hand-edit to a byte-frozen file fails here),
    states in code that today's marker is a different mechanism, and keeps checking the
    parts of the provenance that ARE still live: the sample rate, and the per-item lead
    gap, which survived the marker change unchanged and still separates marker from
    stimulus in every B/C/D WAV.
    """
    frozen = json.loads(_res.data_file("stimuli_provenance.json").read_text())
    marker = frozen["lead_in_marker"]

    # (1) the frozen block, verbatim — the RETIRED sine contract
    assert marker["nominal_freq_hz"] == 10000
    assert marker["leadin_s"] == 1.0
    assert marker["calibration_s"] == 10.0
    assert "sine" in marker["note"]

    # (2) today's marker is not that marker, and does not pretend to be
    assert not hasattr(K, "MARKER_FREQ_HZ")  # there is no marker frequency any more
    assert K.MARKER_SPAN_S != marker["leadin_s"]  # 0.5 s burst, not a 1 s tone
    assert K.CAL_RATE_HZ == 50.0  # program A is a pulse train, not a 10 kHz tone

    # (3) what IS still live in the frozen file, and must stay in sync with the library
    assert frozen["playback_sample_rate_hz"] == K.RATE_HZ
    gaps = parsed["lead_gap_samp"]
    assert gaps is not None
    frozen_gaps = [e["gap_samp"] for e in marker["per_item_gap"]]
    assert frozen_gaps == [int(g) for g in gaps], (
        "the frozen per-item lead gaps no longer match STIM_LEAD_GAP_SAMP in the library"
    )
    lo_ms, hi_ms = marker["lead_gap_ms_lo"], marker["lead_gap_ms_hi"]
    assert all(lo_ms <= g / K.RATE_HZ * 1000.0 <= hi_ms for g in gaps)


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
