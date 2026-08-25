"""Alignment against synthetic ground truth: a known offset and a known drift.

The sample data cannot serve here -- its WAV and its log are 253 days apart and
are not a pair (``docs/RECON.md`` §4.1) -- so every case below has its answer
known by construction.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fakefish.clock_align import (
    MAX_PLAUSIBLE_DRIFT_PPM,
    Alignment,
    coarse_lag,
    estimate_alignment,
    nudge,
    two_point_alignment,
    validate,
)
from fakefish.pulse_detect import (
    DetectionParams,
    detect_pulses,
    refine_template,
    suggest_absolute_floor,
)
from fakefish.clock_align import AlignmentMethod
from synth_fixtures import biphasic_template, make_aligned_pair

#: Tolerances the alignment must meet, and which the README quotes.
OFFSET_TOLERANCE_S = 1e-3
DRIFT_TOLERANCE_PPM = 5.0


def _train(seed: int, n: int, duration: float) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.sort(rng.uniform(0.0, duration, n))


# --------------------------------------------------------------------------- #
# Coarse lag
# --------------------------------------------------------------------------- #
def test_coarse_lag_finds_a_pure_offset() -> None:
    log_t = _train(0, 500, 300.0)
    lag, ratio = coarse_lag(log_t, log_t + 12.345)
    assert lag == pytest.approx(12.345, abs=0.005)
    assert ratio > 50, "an exact pairing should give a tall, isolated peak"


def test_coarse_lag_finds_a_negative_offset() -> None:
    log_t = _train(1, 400, 200.0) + 50.0
    lag, _ = coarse_lag(log_t, log_t - 30.0)
    assert lag == pytest.approx(-30.0, abs=0.005)


def test_coarse_lag_on_unrelated_trains_has_a_low_peak_ratio() -> None:
    """The number that would have caught the sample data's false pairing."""
    a = _train(2, 500, 300.0)
    b = _train(3, 500, 300.0)
    _, ratio = coarse_lag(a, b)
    assert ratio < 20, f"unrelated trains should not correlate strongly, got {ratio:.1f}"


def test_coarse_lag_on_empty_input_is_zero_not_an_error() -> None:
    assert coarse_lag(np.empty(0), np.array([1.0])) == (0.0, 0.0)


# --------------------------------------------------------------------------- #
# The headline: recover a known offset and drift
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("scale", "offset"),
    [
        (1.0, 0.0),
        (1.0, 12.345),
        (1.0 + 40e-6, 12.345),
        (1.0 - 40e-6, -5.5),
        (1.0 + 200e-6, 61.25),
    ],
)
def test_a_known_offset_and_drift_are_recovered(scale: float, offset: float) -> None:
    log_t = _train(4, 1200, 600.0)
    rec_t = scale * log_t + offset
    result = estimate_alignment(log_t, rec_t)

    assert result.alignment.offset_s == pytest.approx(offset, abs=OFFSET_TOLERANCE_S)
    assert result.alignment.drift_ppm == pytest.approx(
        (scale - 1.0) * 1e6, abs=DRIFT_TOLERANCE_PPM
    )
    assert result.match_fraction > 0.99
    assert result.alignment.median_abs_residual_s < 1e-4


def test_the_fit_is_never_returned_pre_validated() -> None:
    """Computing a mapping and accepting one are different acts."""
    log_t = _train(5, 300, 120.0)
    result = estimate_alignment(log_t, log_t + 3.0)
    assert result.alignment.method is AlignmentMethod.AUTO_MATCHED_FILTER
    assert not result.alignment.validated


def test_missing_detections_do_not_break_the_fit() -> None:
    """Half the pulses fall below threshold; the mapping must still be right."""
    rng = np.random.default_rng(6)
    log_t = _train(6, 2000, 600.0)
    scale, offset = 1.0 + 40e-6, 7.25
    keep = rng.random(log_t.size) > 0.5
    rec_t = scale * log_t[keep] + offset

    result = estimate_alignment(log_t, rec_t)
    assert result.alignment.offset_s == pytest.approx(offset, abs=OFFSET_TOLERANCE_S)
    assert result.alignment.drift_ppm == pytest.approx(40.0, abs=DRIFT_TOLERANCE_PPM)


def test_spurious_extra_detections_do_not_break_the_fit() -> None:
    """Noise adds detections the log never claimed; they must not drag the fit."""
    rng = np.random.default_rng(7)
    log_t = _train(7, 1000, 400.0)
    scale, offset = 1.0 + 25e-6, -3.5
    rec_t = np.sort(
        np.concatenate([scale * log_t + offset, rng.uniform(-3.5, 397.0, 500)])
    )
    result = estimate_alignment(log_t, rec_t)
    assert result.alignment.offset_s == pytest.approx(offset, abs=OFFSET_TOLERANCE_S)
    assert result.alignment.drift_ppm == pytest.approx(25.0, abs=DRIFT_TOLERANCE_PPM)


def test_jittered_detections_still_give_the_right_mapping() -> None:
    """Detection is quantised by the sample rate; the fit averages that out."""
    rng = np.random.default_rng(8)
    log_t = _train(8, 1500, 500.0)
    scale, offset = 1.0 + 30e-6, 20.0
    rec_t = np.sort(scale * log_t + offset + rng.normal(0, 50e-6, log_t.size))
    result = estimate_alignment(log_t, rec_t)
    assert result.alignment.offset_s == pytest.approx(offset, abs=OFFSET_TOLERANCE_S)
    assert result.alignment.drift_ppm == pytest.approx(30.0, abs=DRIFT_TOLERANCE_PPM)
    assert result.alignment.median_abs_residual_s < 1e-4


@given(
    offset=st.floats(min_value=-60.0, max_value=60.0),
    ppm=st.floats(min_value=-100.0, max_value=100.0),
)
@settings(max_examples=25, deadline=None)
def test_offset_and_drift_recovery_is_a_property_not_a_lucky_case(
    offset: float, ppm: float
) -> None:
    log_t = _train(9, 900, 400.0)
    scale = 1.0 + ppm * 1e-6
    result = estimate_alignment(log_t, scale * log_t + offset)
    assert result.alignment.offset_s == pytest.approx(offset, abs=OFFSET_TOLERANCE_S)
    assert result.alignment.drift_ppm == pytest.approx(ppm, abs=DRIFT_TOLERANCE_PPM)


# --------------------------------------------------------------------------- #
# Honest reporting of a bad fit
# --------------------------------------------------------------------------- #
def test_unrelated_trains_are_reported_as_suspect() -> None:
    """The sample data's situation, reproduced: a fit that must not be trusted."""
    a = _train(10, 800, 400.0)
    b = _train(11, 800, 400.0)
    result = estimate_alignment(a, b)

    passed, reasons = validate(result)
    assert not passed
    assert reasons
    assert result.warnings
    assert result.coarse_peak_ratio < 20


def test_a_good_fit_passes_validation() -> None:
    log_t = _train(12, 1200, 500.0)
    result = estimate_alignment(log_t, (1 + 40e-6) * log_t + 9.0)
    passed, reasons = validate(result)
    assert passed, reasons


def test_validation_lists_every_failure_at_once() -> None:
    """One round of fixing should be able to address all of them."""
    a = _train(13, 200, 100.0)
    b = _train(14, 200, 100.0)
    _, reasons = validate(
        estimate_alignment(a, b),
        max_median_residual_s=1e-9,
        min_match_fraction=0.99,
        min_peak_ratio=1e6,
    )
    assert len(reasons) >= 2


def test_an_implausible_fitted_drift_is_flagged() -> None:
    """A drift the fit *does* converge on, but which no crystal produces.

    600 ppm over 60 s is 36 ms of accumulated drift -- inside the first match
    tolerance, so the fit succeeds and reports it. That is the case the drift
    guard exists for.
    """
    log_t = _train(15, 800, 60.0)
    scale = 1.0 + 600e-6
    result = estimate_alignment(log_t, scale * log_t + 1.0)
    assert result.alignment.drift_ppm == pytest.approx(600.0, abs=20.0)
    assert any("drift" in w for w in result.warnings), result.warnings
    passed, reasons = validate(result)
    assert not passed
    assert any("drift" in r for r in reasons)


def test_an_extreme_drift_breaks_the_coarse_stage_and_says_so() -> None:
    """1500 ppm over 300 s is 0.45 s of drift, which smears the correlation.

    The fit then cannot converge, and the honest outcome is not a large fitted
    drift -- it is a low correlation peak and a low match fraction. Both are
    reported, and validation refuses the result. Asserting a "drift" warning
    here would be asserting a failure mode the code does not actually have.
    """
    log_t = _train(15, 800, 300.0)
    scale = 1.0 + 3 * MAX_PLAUSIBLE_DRIFT_PPM * 1e-6
    result = estimate_alignment(log_t, scale * log_t + 1.0)

    assert result.match_fraction < 0.1
    assert result.coarse_peak_ratio < 20
    assert len(result.warnings) >= 2
    passed, reasons = validate(result)
    assert not passed
    assert reasons


def test_the_peak_ratio_is_finite_for_sparse_trains() -> None:
    """Peak-to-median was infinite here, which is worse than useless.

    Most lags of a sparse train correlate to exactly zero, so a median
    denominator is 0 and the ratio explodes -- measured at 5e16 before this was
    changed to peak-to-sidelobe.
    """
    a = np.array([1.0, 5.0, 9.0])
    b = np.array([1.5, 5.5, 9.5])
    _, ratio = coarse_lag(a, b)
    assert np.isfinite(ratio)
    assert 0 < ratio < 1e4


def test_the_peak_ratio_separates_a_real_pairing_from_a_spurious_one() -> None:
    log_t = _train(20, 600, 300.0)
    _, good = coarse_lag(log_t, log_t + 4.0)
    _, bad = coarse_lag(log_t, _train(21, 600, 300.0))
    assert good > 5 * bad, f"real {good:.1f} vs spurious {bad:.1f}"


def test_match_fraction_uses_the_right_denominator() -> None:
    """A log much longer than the recording must not look like a great match."""
    log_t = _train(16, 4000, 2000.0)
    inside = log_t[log_t < 100.0]
    result = estimate_alignment(log_t, inside + 5.0)
    assert result.n_candidates < log_t.size
    assert result.n_candidates == pytest.approx(inside.size, rel=0.05)
    assert result.match_fraction > 0.95


def test_empty_trains_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        estimate_alignment(np.empty(0), np.array([1.0]))
    with pytest.raises(ValueError, match="non-empty"):
        estimate_alignment(np.array([1.0]), np.empty(0))


def test_drift_is_not_fitted_from_too_few_pairs() -> None:
    """Two points always fit a line exactly, which is the trap."""
    log_t = np.array([1.0, 2.0, 3.0])
    result = estimate_alignment(log_t, log_t + 4.0)
    assert result.alignment.scale == 1.0
    assert result.alignment.offset_s == pytest.approx(4.0, abs=1e-6)


def test_fit_drift_can_be_switched_off() -> None:
    log_t = _train(17, 800, 300.0)
    result = estimate_alignment(log_t, (1 + 40e-6) * log_t + 2.0, fit_drift=False)
    assert result.alignment.scale == 1.0


# --------------------------------------------------------------------------- #
# Manual paths
# --------------------------------------------------------------------------- #
def test_two_point_alignment_is_exact_and_validated() -> None:
    a = two_point_alignment(log_a_s=10.0, rec_a_s=20.0, log_b_s=110.0, rec_b_s=120.01)
    assert a.method is AlignmentMethod.MANUAL_TWO_POINT
    assert a.validated, "a human placed both points; that is what validation means here"
    assert a.log_to_recording(np.array([10.0, 110.0])) == pytest.approx([20.0, 120.01])
    assert a.drift_ppm == pytest.approx(100.0, abs=0.1)


def test_two_anchors_at_one_log_time_are_refused() -> None:
    with pytest.raises(ValueError, match="different log times"):
        two_point_alignment(5.0, 1.0, 5.0, 2.0)


def test_nudge_shifts_without_touching_the_scale() -> None:
    base = two_point_alignment(0.0, 1.0, 100.0, 101.0)
    moved = nudge(base, 0.25)
    assert moved.scale == base.scale
    assert moved.offset_s == pytest.approx(base.offset_s + 0.25)
    assert moved.method is AlignmentMethod.MANUAL_NUDGE


def test_nudge_does_not_manufacture_validation() -> None:
    log_t = _train(18, 500, 200.0)
    unvalidated = estimate_alignment(log_t, log_t + 1.0).alignment
    assert not nudge(unvalidated, 0.1).validated


def test_round_trip_through_the_mapping() -> None:
    a = two_point_alignment(0.0, 3.0, 1000.0, 1003.05)
    t = np.array([0.0, 1.0, 500.0, 999.0])
    back = a.recording_to_log(a.log_to_recording(t))
    np.testing.assert_allclose(back, t, atol=1e-9)


# --------------------------------------------------------------------------- #
# End to end: detect from audio, then align
# --------------------------------------------------------------------------- #
def test_detect_then_align_recovers_the_injected_clock(tmp_path: Path) -> None:
    """The whole chain, on a synthetic recording with a known offset and drift."""
    scale, offset = 1.0 + 40e-6, 12.345
    log_path, wav_path, _truth = make_aligned_pair(
        tmp_path,
        scale=scale,
        offset_s=offset,
        n_pulses=600,
        log_duration_s=280.0,
        recording_duration_s=320.0,
        noise_rms=0.003,
        amplitude=0.15,
    )
    # fakefish's own readers, so this exercises the shipped path rather than a
    # test-only one: pulse_log for the log, align_log.read_channel for the WAV.
    from fakefish.pulse_log import read as read_log
    from fakefish.recording import Recording

    table = read_log(log_path)
    log_times = table.pulse_times_s()
    with Recording.open([wav_path]) as rec:
        x, sr = rec.read(0, rec.frames, 0), rec.rate

    template = biphasic_template(sr)
    floor = suggest_absolute_floor(x, fraction=0.05)
    params = DetectionParams(snr_threshold=8.0, absolute_floor=floor)

    first = detect_pulses(x, sr, template, params)
    assert first.n > 0.8 * log_times.size, f"only found {first.n} of {log_times.size}"

    # Close the loop on the template, as the app does.
    better = refine_template(x, first, sr)
    found = detect_pulses(x, sr, better, params)

    result = estimate_alignment(log_times, found.times_s)
    assert result.alignment.offset_s == pytest.approx(offset, abs=OFFSET_TOLERANCE_S)
    assert result.alignment.drift_ppm == pytest.approx(40.0, abs=DRIFT_TOLERANCE_PPM)
    assert result.match_fraction > 0.9
    passed, reasons = validate(result)
    assert passed, f"{result.summary()} -- {reasons}"


# ===== per-segment offsets =================================================
def test_a_step_at_a_segment_boundary_tilts_an_unsegmented_fit() -> None:
    """The failure this models, and why it is not obvious in the output.

    A recorder that drops samples where it splits a file puts a STEP in the
    mapping. Least squares cannot represent a step, so it does not report one --
    it tilts the slope to split the difference, and the mapping comes out wrong
    everywhere rather than wrong after the join. Measured on exp3: a ~32 ms drop
    at one of three joins turned a true +11 ppm into a fitted +2.6 ppm.
    """
    rng = np.random.default_rng(4)
    t_log = np.sort(rng.uniform(0.0, 1200.0, 3000))
    scale, offset, step_at, step = 1.0 + 11e-6, 5.0, 800.0, -0.032
    truth = scale * t_log + offset + np.where(t_log >= step_at, step, 0.0)
    det = np.sort(truth + rng.normal(0.0, 30e-6, t_log.size))

    flat = estimate_alignment(t_log, det)
    assert abs(flat.alignment.drift_ppm - 11.0) > 3.0, (
        "the unsegmented fit should be dragged off the true rate by the step"
    )

    seg = estimate_alignment(t_log, det, segment_edges_s=(step_at,))
    assert seg.alignment.drift_ppm == pytest.approx(11.0, abs=1.5)
    assert seg.alignment.is_piecewise
    # The recovered step is the drop, relative to the first segment.
    assert seg.alignment.segment_offsets_s[0] == pytest.approx(0.0, abs=1e-6)
    assert seg.alignment.segment_offsets_s[1] == pytest.approx(step, abs=2e-3)
    assert seg.match_fraction > flat.match_fraction


def test_segmentation_does_not_disturb_a_clean_recording() -> None:
    """Handing boundaries to a recording with no steps must change nothing.

    Otherwise the estimator would be trading a real improvement on split
    recordings for a quiet regression on every intact one.
    """
    rng = np.random.default_rng(5)
    t_log = np.sort(rng.uniform(0.0, 900.0, 2000))
    scale, offset = 1.0 + 20e-6, 3.5
    det = np.sort(scale * t_log + offset + rng.normal(0.0, 30e-6, t_log.size))

    flat = estimate_alignment(t_log, det)
    seg = estimate_alignment(t_log, det, segment_edges_s=(300.0, 600.0))
    assert seg.alignment.drift_ppm == pytest.approx(flat.alignment.drift_ppm, abs=0.5)
    assert seg.match_fraction == pytest.approx(flat.match_fraction, abs=0.02)
    # Every recovered step is essentially zero: nothing was lost at these joins.
    assert max(abs(o) for o in seg.alignment.segment_offsets_s) < 1e-3


def test_piecewise_mapping_round_trips() -> None:
    """recording_to_log must invert log_to_recording across a step."""
    a = Alignment(
        scale=1.0 + 11e-6,
        offset_s=5.0,
        segment_edges_s=(800.0,),
        segment_offsets_s=(0.0, -0.032),
    )
    t = np.array([0.0, 400.0, 799.0, 801.0, 1200.0])
    back = a.recording_to_log(a.log_to_recording(t))
    assert back == pytest.approx(t, abs=1e-6)


def test_a_segment_with_too_few_pairs_borrows_its_neighbour() -> None:
    """An unmeasured segment must not be asserted to be clean.

    Snapping it to zero would claim the join lost nothing, which is a claim the
    data does not support; carrying the nearest measured offset at least says
    'the same as next door'.
    """
    rng = np.random.default_rng(6)
    t_log = np.sort(rng.uniform(0.0, 600.0, 1500))
    det = np.sort((1.0 + 10e-6) * t_log + 2.0 + rng.normal(0.0, 30e-6, t_log.size))
    # A boundary right at the end, so the final segment holds almost nothing.
    res = estimate_alignment(t_log, det, segment_edges_s=(599.0,))
    assert len(res.alignment.segment_offsets_s) == 2
    assert res.alignment.segment_offsets_s[1] == pytest.approx(
        res.alignment.segment_offsets_s[0], abs=1e-9
    )


def test_a_lag_that_wanders_needs_seeded_segments() -> None:
    """A session-long wander defeats one straight line, and seeding is what saves it.

    Real recordings do not drift linearly for an hour: exp3's lag wanders 116 ms
    end to end. Segments alone are not enough -- every segment starts from the one
    global lag, so a segment whose true lag is tens of milliseconds away never
    pairs at the first tolerance rung and is never refined. Each segment has to be
    SEEDED from its own correlation first.
    """
    rng = np.random.default_rng(11)
    t_log = np.sort(rng.uniform(0.0, 3000.0, 6000))
    scale, offset = 1.0 + 11e-6, 6.15
    # Drift plus occasional sample drops, which is what a recorder actually does.
    # NOT a fast sinusoid: exp3's non-drift wander is only 1-2 ppm, so a constant
    # offset per segment is a good local model. What breaks a single line is the
    # STEPS, and they do not fall on the knots.
    wander = np.zeros_like(t_log)
    for at, size in ((640.0, -0.030), (1490.0, -0.025), (2380.0, -0.035)):
        wander += np.where(t_log > at, size, 0.0)
    det = np.sort(scale * t_log + offset + wander + rng.normal(0.0, 30e-6, t_log.size))

    flat = estimate_alignment(t_log, det)
    knots = tuple(np.arange(300.0, 3000.0, 300.0))
    seeded = estimate_alignment(t_log, det, segment_edges_s=knots)

    assert flat.match_fraction < 0.5, "one straight line cannot hold a stepping lag"
    # The segments holding a step are split by it and cannot fully recover; the
    # rest do, which is the difference between an unusable fit and a usable one.
    assert seeded.match_fraction > 0.6
    # And the rate comes back, which the unsegmented fit gets badly wrong because
    # least squares tilts the slope to absorb what it cannot represent.
    assert seeded.alignment.drift_ppm == pytest.approx(11.0, abs=3.0)
    assert abs(flat.alignment.drift_ppm - 11.0) > abs(seeded.alignment.drift_ppm - 11.0)


def test_a_wild_segment_seed_is_refused_not_followed() -> None:
    """A segment that correlates a second away found the wrong pulses.

    Following such a seed drags the mapping somewhere the refinement cannot come
    back from. Measured: interpolating through windows like this turned a session
    that aligned at 99.6 % into one that aligned at 64 %.
    """
    rng = np.random.default_rng(12)
    t_log = np.sort(rng.uniform(0.0, 1200.0, 3000))
    det = np.sort(t_log + 4.0 + rng.normal(0.0, 30e-6, t_log.size))
    # Decoys a full 5 s away, dense enough to win a correlation in one segment.
    decoy = np.sort(rng.uniform(600.0, 900.0, 4000) + 9.0)
    res = estimate_alignment(
        t_log, np.sort(np.concatenate([det, decoy])), segment_edges_s=(300.0, 600.0, 900.0)
    )
    # No segment may be pulled a whole second off the session lag.
    assert max(abs(o) for o in res.alignment.segment_offsets_s) < 1.0
    assert res.match_fraction > 0.8
