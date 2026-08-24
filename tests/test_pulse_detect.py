"""Pulse detection: found where they are, not found where they are not."""

from __future__ import annotations

import numpy as np
import pytest

from fakefish.pulse_detect import (
    DetectionParams,
    detect_pulses,
    normalise_template,
    refine_template,
    robust_scale,
    suggest_absolute_floor,
)
from synth_fixtures import biphasic_template

SR = 48_000


def _shape_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Best normalised cross-correlation between two templates, over all lags.

    Compared at the best lag rather than at zero lag: ``refine_template``
    returns a **peak-centred** template while the source template has its peak a
    quarter of the way in, so a zero-lag dot product compares a pulse against
    mostly padding and reports ~0.03 for two identical shapes.
    """
    a = a - a.mean()
    b = b - b.mean()
    a = a / (np.linalg.norm(a) or 1.0)
    b = b / (np.linalg.norm(b) or 1.0)
    return float(np.abs(np.correlate(a, b, mode="full")).max())


def _signal(
    times_s: list[float],
    duration_s: float = 10.0,
    amplitude: float = 0.3,
    noise: float = 0.005,
    seed: int = 0,
    sr: int = SR,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (signal, template) with pulse peaks at exactly ``times_s``."""
    rng = np.random.default_rng(seed)
    n = int(duration_s * sr)
    x = rng.normal(0, noise, n) if noise > 0 else np.zeros(n)
    tmpl = biphasic_template(sr)
    peak_i = int(np.argmax(np.abs(tmpl)))
    for t in times_s:
        start = round(t * sr) - peak_i
        lo, hi = max(start, 0), min(start + tmpl.size, n)
        if hi > lo:
            x[lo:hi] += amplitude * tmpl[lo - start : hi - start]
    return x, tmpl


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def test_robust_scale_matches_sigma_for_gaussian_noise() -> None:
    rng = np.random.default_rng(0)
    assert robust_scale(rng.normal(0, 0.25, 200_000)) == pytest.approx(0.25, rel=0.02)


def test_robust_scale_ignores_a_few_huge_outliers() -> None:
    """The whole reason it is used: the pulses must not set their own threshold."""
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.01, 100_000)
    x[::500] = 5.0
    assert robust_scale(x) == pytest.approx(0.01, rel=0.05)
    assert np.std(x) > 10 * robust_scale(x)


def test_normalise_template_is_zero_mean_and_unit_norm() -> None:
    t = normalise_template(np.array([1.0, 2.0, 3.0, 4.0]))
    assert t.mean() == pytest.approx(0.0, abs=1e-12)
    assert np.linalg.norm(t) == pytest.approx(1.0)


def test_an_all_zero_template_is_refused() -> None:
    with pytest.raises(ValueError, match="all zeros"):
        normalise_template(np.zeros(10))


def test_a_constant_template_is_refused() -> None:
    """A DC template would respond to offset, not to shape."""
    with pytest.raises(ValueError, match="all zeros"):
        normalise_template(np.full(10, 3.0))


# --------------------------------------------------------------------------- #
# Detection accuracy
# --------------------------------------------------------------------------- #
def test_pulses_are_found_at_their_own_times() -> None:
    times = [1.0, 2.5, 4.25, 7.125]
    x, tmpl = _signal(times)
    got = detect_pulses(x, SR, tmpl, DetectionParams(absolute_floor=0.05))
    assert got.n == len(times)
    np.testing.assert_allclose(got.times_s, times, atol=1.5 / SR)


def test_time_offset_is_applied() -> None:
    x, tmpl = _signal([1.0])
    got = detect_pulses(x, SR, tmpl, DetectionParams(absolute_floor=0.05), t0_s=100.0)
    assert got.times_s[0] == pytest.approx(101.0, abs=1.5 / SR)


def test_both_polarities_are_found_and_correctly_labelled() -> None:
    """The firmware flips polarity per pulse, so the detector must not care.

    It must also *report* which way each pulse went, because the log's ``pol``
    column records the flip and the two can be cross-checked.
    """
    x, tmpl = _signal([1.0, 3.0], noise=0.002)
    x_flipped, _ = _signal([2.0, 4.0], noise=0.0)
    x = x - x_flipped  # pulses at 2 s and 4 s are inverted

    got = detect_pulses(
        x, SR, tmpl, DetectionParams(absolute_floor=0.05, polarity="both")
    )
    assert got.n == 4
    np.testing.assert_allclose(got.times_s, [1.0, 2.0, 3.0, 4.0], atol=1.5 / SR)
    np.testing.assert_array_equal(got.polarities, [1, -1, 1, -1])


def test_a_sign_restricted_mode_does_not_separate_polarities() -> None:
    """Measured, and worth knowing before anyone reaches for it.

    An inverted **biphasic** pulse still contains a genuine positive-going lobe,
    so ``polarity="positive"`` responds to it too -- at a shifted time. It finds
    all four pulses here, two of them mis-timed by half a millisecond. The
    ``polarities`` field from ``"both"`` is the way to tell the two apart; a
    sign-restricted filter is not.
    """
    x, tmpl = _signal([1.0, 3.0], noise=0.002)
    x_flipped, _ = _signal([2.0, 4.0], noise=0.0)
    x = x - x_flipped

    positive_only = detect_pulses(
        x, SR, tmpl, DetectionParams(absolute_floor=0.05, polarity="positive")
    )
    assert positive_only.n == 4
    # The inverted ones are found, but displaced -- which is the point.
    assert abs(positive_only.times_s[1] - 2.0) > 0.2e-3


def test_silence_yields_nothing() -> None:
    rng = np.random.default_rng(2)
    x = rng.normal(0, 0.001, SR * 5)
    tmpl = biphasic_template(SR)
    floor = suggest_absolute_floor(x, fraction=0.05)
    assert detect_pulses(x, SR, tmpl, DetectionParams(absolute_floor=max(floor, 0.02))).n == 0


def test_the_absolute_floor_rejects_structure_the_snr_threshold_accepts() -> None:
    """Why an SNR threshold alone is not enough (``ASSUMPTIONS.md`` A-9).

    A robust threshold is *relative* to the noise it measures, so a quiet
    stretch containing small pulse-shaped structure -- distant animals, a bit of
    coupling, an artefact -- clears it comfortably while being far too small to
    be the playback. On the real sample recording this produced ~1300
    "detections" in 40 s whose peak was 0.0008 of full scale.

    Here the same situation is built explicitly: pulses at 1/200 of a real
    one's amplitude, which the SNR threshold accepts and the floor rejects.
    """
    tiny_times = list(np.arange(0.5, 19.5, 0.5))
    x, tmpl = _signal(
        tiny_times, duration_s=20.0, amplitude=0.0015, noise=0.00005, seed=3
    )
    assert np.abs(x).max() < 0.01, "the structure must genuinely be tiny"

    no_floor = detect_pulses(x, SR, tmpl, DetectionParams(absolute_floor=0.0))
    assert no_floor.n >= len(tiny_times) - 2, (
        f"the SNR threshold alone accepts this structure; found {no_floor.n}"
    )

    with_floor = detect_pulses(x, SR, tmpl, DetectionParams(absolute_floor=0.05))
    assert with_floor.n == 0
    assert with_floor.n_rejected_by_floor == no_floor.n


def test_rejections_by_the_floor_are_reported_not_hidden() -> None:
    x, tmpl = _signal([1.0, 2.0], amplitude=0.3, noise=0.02)
    got = detect_pulses(x, SR, tmpl, DetectionParams(absolute_floor=0.5))
    assert got.n == 0
    assert got.n_rejected_by_floor >= 2


def test_the_refractory_window_is_respected() -> None:
    x, tmpl = _signal([1.0, 1.0005], noise=0.001)
    got = detect_pulses(
        x, SR, tmpl, DetectionParams(absolute_floor=0.05, refractory_s=0.002)
    )
    assert got.n == 1


def test_the_larger_of_two_close_peaks_wins() -> None:
    """Greedy left-to-right would keep the first and bias every later interval."""
    sr = SR
    n = sr * 2
    x = np.zeros(n)
    tmpl = biphasic_template(sr)
    peak_i = int(np.argmax(np.abs(tmpl)))
    for t, amp in ((1.0000, 0.10), (1.0008, 0.50)):
        start = round(t * sr) - peak_i
        x[start : start + tmpl.size] += amp * tmpl
    got = detect_pulses(
        x, sr, tmpl, DetectionParams(absolute_floor=0.02, refractory_s=0.002)
    )
    assert got.n == 1
    assert got.times_s[0] == pytest.approx(1.0008, abs=2 / sr)


def test_amplitudes_are_measured() -> None:
    x, tmpl = _signal([1.0, 3.0], amplitude=0.4, noise=0.001)
    got = detect_pulses(x, SR, tmpl, DetectionParams(absolute_floor=0.05))
    assert got.amplitudes.max() == pytest.approx(0.4, rel=0.1)


def test_ipi_and_rate() -> None:
    x, tmpl = _signal([1.0, 1.1, 1.3], noise=0.001)
    got = detect_pulses(x, SR, tmpl, DetectionParams(absolute_floor=0.05))
    np.testing.assert_allclose(got.ipi_s(), [0.1, 0.2], atol=1e-4)
    np.testing.assert_allclose(got.rate_hz(), [10.0, 5.0], rtol=1e-3)


@pytest.mark.parametrize("rate_hz", [5.0, 50.0, 300.0])
def test_a_dense_train_is_fully_recovered(rate_hz: float) -> None:
    """Volleys reach 300-400 Hz, which is where a detector starts to merge."""
    times = list(np.arange(0.5, 2.5, 1.0 / rate_hz))
    x, tmpl = _signal(times, duration_s=3.0, amplitude=0.3, noise=0.003)
    got = detect_pulses(
        x, SR, tmpl, DetectionParams(absolute_floor=0.05, refractory_s=0.002)
    )
    assert got.n == pytest.approx(len(times), rel=0.02)


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def test_a_two_dimensional_input_is_refused() -> None:
    with pytest.raises(ValueError, match="one channel"):
        detect_pulses(np.zeros((100, 2)), SR, biphasic_template(SR))


def test_a_signal_shorter_than_the_template_is_refused() -> None:
    with pytest.raises(ValueError, match="shorter than"):
        detect_pulses(np.zeros(10), SR, biphasic_template(SR))


# --------------------------------------------------------------------------- #
# Template refinement
# --------------------------------------------------------------------------- #
def test_refinement_recovers_the_true_shape() -> None:
    times = list(np.arange(0.5, 9.5, 0.25))
    x, tmpl = _signal(times, duration_s=10.0, amplitude=0.3, noise=0.01)
    first = detect_pulses(x, SR, tmpl, DetectionParams(absolute_floor=0.05))
    refined = refine_template(x, first, SR, width_s=0.004)

    similarity = _shape_similarity(refined, tmpl)
    assert similarity > 0.9, f"refined template correlates only {similarity:.3f}"


def test_refinement_sign_aligns_before_averaging() -> None:
    """Without sign alignment the random per-pulse polarity averages to noise."""
    sr = SR
    n = sr * 6
    tmpl = biphasic_template(sr)
    peak_i = int(np.argmax(np.abs(tmpl)))
    rng = np.random.default_rng(5)
    x = rng.normal(0, 0.005, n)
    signs = []
    for k, t in enumerate(np.arange(0.5, 5.5, 0.1)):
        sign = 1.0 if k % 2 == 0 else -1.0
        signs.append(sign)
        start = round(t * sr) - peak_i
        x[start : start + tmpl.size] += 0.3 * sign * tmpl
    assert set(signs) == {1.0, -1.0}

    found = detect_pulses(x, sr, tmpl, DetectionParams(absolute_floor=0.05))
    refined = refine_template(x, found, sr, width_s=0.004)
    # A sign-blind average would be noise: near-zero similarity to the truth.
    assert _shape_similarity(refined, tmpl) > 0.85


def test_refinement_with_no_usable_snippet_is_an_error() -> None:
    x, tmpl = _signal([0.0005], duration_s=0.01, noise=0.0)
    found = detect_pulses(x, SR, tmpl, DetectionParams(absolute_floor=0.0))
    with pytest.raises(ValueError, match="no complete snippet"):
        refine_template(x, found, SR, width_s=1.0)


# --------------------------------------------------------------------------- #
# Floor suggestion
# --------------------------------------------------------------------------- #
def test_suggested_floor_scales_with_the_signal() -> None:
    """A fixed number would be wrong at a different gain or distance."""
    loud, _ = _signal([1.0, 2.0], amplitude=0.8, noise=0.01)
    quiet, _ = _signal([1.0, 2.0], amplitude=0.08, noise=0.001)
    assert suggest_absolute_floor(loud) > 5 * suggest_absolute_floor(quiet)


def test_suggested_floor_is_not_set_by_one_clipped_sample() -> None:
    x, _ = _signal([1.0], amplitude=0.2, noise=0.005)
    with_spike = x.copy()
    with_spike[0] = 1.0
    assert suggest_absolute_floor(with_spike) == pytest.approx(
        suggest_absolute_floor(x), rel=0.05
    )
