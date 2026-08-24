"""Log-to-recording clock alignment.

The stimulator's 50 kHz tick and the logger's sample clock are independent
crystals with independent start instants, so::

    t_recording = scale * t_log + offset

Everything downstream is wrong if this is wrong, which is why the estimator
reports how well it did rather than only what it found: the number of matched
pulses **against the number that could have matched**, the residual spread, and
the residual slope after the fit. A high match count over a huge candidate pool
is not evidence.

The estimate runs in two stages, because they fail differently:

1. **Coarse lag** by cross-correlating binned pulse trains. This finds an offset
   of arbitrary size, assumes no drift, and is where a wrong pairing shows up --
   as a peak barely above the background, which
   :attr:`AlignmentResult.coarse_peak_ratio` exposes.
2. **Refinement** by nearest-neighbour pairing and a robust linear fit with a
   shrinking match tolerance. This recovers drift and tightens the offset. It
   can only polish a coarse lag that was already right.

The log's own RTC anchors are deliberately not used for drift. One 1-second
quantisation step in a real field log manufactures an apparent -794 ppm
(``docs/RECON.md`` §1.5); anchors place a session on the wall clock to about a
second and nothing more.
"""

# ADOPTED from playback-explorer (core/align.py) on 2026-08-24, when that project's
# browser UI was retired in favour of claudian. It is fakefish's code now -- there is no
# upstream to re-drop from, so this is deliberately NOT a vendored copy under the
# invariant 10/11 discipline (no VENDORED_SHA256, no re-drop rule). Its tests came with
# it: tests/test_clock_align.py and tests/test_pulse_detect.py.

from __future__ import annotations

import enum
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


# `Alignment` and `AlignmentMethod` came from playback_explorer.schemas, which also
# pulls in polars. They are inlined here rather than imported: they are 90 lines of
# plain dataclass and enum, and dragging a dataframe library into fakefish to reach
# them would be the tail wagging the dog.


class AlignmentMethod(enum.StrEnum):
    """How a log-to-recording clock mapping was obtained."""

    NONE = "none"
    """No mapping established. Downstream must badge anything derived from it."""

    AUTO_MATCHED_FILTER = "auto_matched_filter"
    """Cross-correlation of detected pulses against logged pulse times."""

    MANUAL_TWO_POINT = "manual_two_point"
    """Two user-placed anchors."""

    MANUAL_NUDGE = "manual_nudge"
    """An automatic fit the user then adjusted by hand."""

    WALL_CLOCK = "wall_clock"
    """Offset only, from the log's RTC anchors and the recording's start time.

    Accurate to about +-1 s and carries **no** drift information: one 1-second
    quantisation step in a real field log manufactures an apparent -794 ppm. Good
    enough to propose a pairing, never good enough to epoch a trial.
    """


@dataclass(frozen=True, slots=True)
class Alignment:
    """The mapping from device-log time to recording time.

    ``t_recording_s = scale * t_log_s + offset_s``

    ``scale`` absorbs the relative rate error between the stimulator's 50 kHz tick
    and the recorder's sample clock; ``offset_s`` absorbs the arbitrary difference
    in their start instants. Two independent crystals at +-20 ppm can differ by
    ~40 ppm, which is ~144 ms per hour -- so a single offset is fine for a short
    recording and degrades across a session.

    ``n_matched / n_candidates`` is the statistic that says whether a fit is
    trustworthy. A high absolute match count over a huge candidate pool is not.

    ``validated`` is set only when a threshold check or a human accepted the fit.
    **Nothing downstream may use an unvalidated alignment without badging it.**
    """

    scale: float = 1.0
    offset_s: float = 0.0
    method: AlignmentMethod = AlignmentMethod.NONE
    n_matched: int = 0
    n_candidates: int = 0
    median_abs_residual_s: float = float("nan")
    residual_slope_ppm: float = float("nan")
    validated: bool = False

    @property
    def drift_ppm(self) -> float:
        """Relative rate error in parts per million."""
        return (self.scale - 1.0) * 1e6

    @property
    def match_fraction(self) -> float:
        """Fraction of candidate log pulses that were matched, 0 if none."""
        return self.n_matched / self.n_candidates if self.n_candidates else 0.0

    def log_to_recording(self, t_log_s: NDArray[np.float64]) -> NDArray[np.float64]:
        """Map device-log seconds onto recording seconds."""
        return self.scale * t_log_s + self.offset_s

    def recording_to_log(self, t_rec_s: NDArray[np.float64]) -> NDArray[np.float64]:
        """Map recording seconds back onto device-log seconds."""
        return (t_rec_s - self.offset_s) / self.scale


__all__ = [
    "DEFAULT_TOLERANCES_S",
    "MAX_PLAUSIBLE_DRIFT_PPM",
    "PEAK_EXCLUSION_S",
    "AlignmentResult",
    "estimate_alignment",
    "nudge",
    "two_point_alignment",
    "validate",
]

#: Match tolerances used in successive refinement passes, seconds. Starts loose
#: enough to survive a coarse lag that is a few tens of milliseconds out, ends
#: tight enough that only genuine pairs remain.
DEFAULT_TOLERANCES_S: tuple[float, ...] = (0.050, 0.020, 0.008, 0.003, 0.001, 0.0003)

#: Beyond this, a fitted drift is a symptom rather than a measurement. Crystals
#: in this class of hardware are tens of ppm; hundreds means the coarse lag
#: paired the wrong pulses, and the fit is describing that mistake.
MAX_PLAUSIBLE_DRIFT_PPM = 500.0

#: A fit needs at least this many pairs before its slope means anything. Two
#: points always fit a line exactly, which is the trap.
MIN_PAIRS_FOR_DRIFT = 20

#: Half-width of the zone around the correlation peak excluded from the
#: background estimate. Wide enough to exclude the peak's own shoulders,
#: which drift and jitter spread over several bins.
PEAK_EXCLUSION_S = 0.050


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    """A fitted mapping, plus everything needed to judge it."""

    alignment: Alignment
    residuals_s: NDArray[np.float64]
    """Detection time minus predicted time, per matched pair."""

    matched_log_times_s: NDArray[np.float64]
    matched_rec_times_s: NDArray[np.float64]

    coarse_lag_s: float
    """Offset found by cross-correlation, before refinement."""

    coarse_peak_ratio: float
    """Correlation peak over the RMS of the correlation away from the peak.

    The single most useful number for spotting a *wrong* alignment: a genuine
    pairing gives a tall, isolated peak, a spurious one gives something barely
    above the background. Peak-to-sidelobe rather than peak-to-median, because
    for sparse trains the median correlation is zero and the ratio would be
    infinite exactly when it is most needed.
    """

    n_candidates: int
    """Log pulses that fell inside the recorded window and could have matched."""

    warnings: tuple[str, ...] = ()
    """Human-readable reasons to distrust the fit."""

    @property
    def match_fraction(self) -> float:
        """Fraction of candidates that were matched."""
        return self.alignment.match_fraction

    def summary(self) -> str:
        """One line for the UI."""
        a = self.alignment
        return (
            f"{a.n_matched}/{self.n_candidates} matched "
            f"({100 * self.match_fraction:.1f} %), "
            f"drift {a.drift_ppm:+.1f} ppm, "
            f"median |residual| {a.median_abs_residual_s * 1e3:.3f} ms"
        )


def _binned_train(times: NDArray[np.float64], bin_s: float, n_bins: int) -> NDArray[np.float64]:
    """Bin pulse times into a train of counts."""
    v = np.zeros(n_bins, dtype=np.float64)
    idx = np.clip((times / bin_s).astype(np.int64), 0, n_bins - 1)
    np.add.at(v, idx, 1.0)
    return v


def coarse_lag(
    log_times_s: NDArray[np.float64],
    rec_times_s: NDArray[np.float64],
    bin_s: float = 0.002,
) -> tuple[float, float]:
    """Estimate the offset by cross-correlating binned pulse trains.

    Parameters
    ----------
    log_times_s
        Logged pulse times on the device clock.
    rec_times_s
        Detected pulse times on the recording clock.
    bin_s
        Bin width. 2 ms is one pulse width -- fine enough to localise the peak,
        coarse enough that a few hundred microseconds of drift across the
        session does not smear it away.

    Returns
    -------
    tuple
        ``(lag_s, peak_ratio)`` where ``lag_s`` is ``t_rec - t_log`` and
        ``peak_ratio`` is the peak over the median of the correlation.
    """
    if log_times_s.size == 0 or rec_times_s.size == 0:
        return 0.0, 0.0

    # Shift both trains to a common non-negative origin before binning. Without
    # this, a negative time clips into bin 0 and corrupts the correlation --
    # which is exactly what happens whenever the recording started *before* the
    # stimulator, i.e. whenever the offset is negative. Shifting both by the
    # same constant leaves their lag unchanged, so no correction is needed
    # afterwards. (Found by a Hypothesis case at offset -36 s.)
    origin = float(min(log_times_s.min(), rec_times_s.min()))
    log_shifted = log_times_s - origin
    rec_shifted = rec_times_s - origin

    span = float(max(log_shifted.max(), rec_shifted.max())) + 1.0
    n = max(int(np.ceil(span / bin_s)), 2)
    a = _binned_train(rec_shifted, bin_s, n)
    b = _binned_train(log_shifted, bin_s, n)

    size = 1 << int(np.ceil(np.log2(2 * n)))
    cc = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
    cc = np.concatenate([cc[-(n - 1) :], cc[:n]])
    lags = np.arange(-(n - 1), n) * bin_s

    k = int(np.argmax(cc))
    peak = float(cc[k])

    # Peak-to-SIDELOBE, not peak-to-median. For sparse trains most lags
    # correlate to exactly zero, so a median denominator is 0 -- or, after FFT
    # round-off, something like 1e-16, which turns the ratio into 5e16 and makes
    # the metric useless precisely where it is needed. The background is
    # measured as the RMS of the correlation outside an exclusion zone around
    # the peak, which is well defined however sparse the trains are.
    exclusion = max(round(PEAK_EXCLUSION_S / bin_s), 1)
    mask = np.ones(cc.size, dtype=bool)
    mask[max(k - exclusion, 0) : k + exclusion + 1] = False
    background = float(np.sqrt(np.mean(cc[mask] ** 2))) if mask.any() else 0.0
    ratio = peak / background if background > 0 else float("inf")
    return float(lags[k]), ratio


def _pair(
    log_times: NDArray[np.float64],
    rec_times: NDArray[np.float64],
    scale: float,
    offset: float,
    tolerance_s: float,
) -> tuple[NDArray[np.bool_], NDArray[np.float64]]:
    """Pair each log pulse with the nearest detection under the current mapping."""
    predicted = scale * log_times + offset
    idx = np.clip(np.searchsorted(rec_times, predicted), 1, rec_times.size - 1)
    left = rec_times[idx - 1]
    right = rec_times[idx]
    nearest = np.where(np.abs(predicted - left) <= np.abs(predicted - right), left, right)
    ok = np.abs(nearest - predicted) < tolerance_s
    return ok, nearest


def estimate_alignment(
    log_times_s: NDArray[np.float64],
    detected_times_s: NDArray[np.float64],
    *,
    bin_s: float = 0.002,
    tolerances_s: tuple[float, ...] = DEFAULT_TOLERANCES_S,
    fit_drift: bool = True,
) -> AlignmentResult:
    """Estimate ``t_rec = scale * t_log + offset`` from two pulse trains.

    Parameters
    ----------
    log_times_s
        Logged pulse times on the device clock. Need not be sorted.
    detected_times_s
        Detected pulse times on the recording clock.
    bin_s
        Bin width for the coarse stage.
    tolerances_s
        Match tolerances for successive refinement passes, loosest first.
    fit_drift
        Fit ``scale`` as well as ``offset``. Turning it off is right for a short
        recording, where a slope fitted over a few seconds is noise dressed as a
        measurement.

    Returns
    -------
    AlignmentResult
        With :attr:`AlignmentResult.alignment` **not validated** -- accepting a
        fit is a decision for :func:`validate` or for a human, never a side
        effect of computing one.

    Raises
    ------
    ValueError
        If either train is empty.
    """
    log_t = np.sort(np.asarray(log_times_s, dtype=np.float64))
    rec_t = np.sort(np.asarray(detected_times_s, dtype=np.float64))
    if log_t.size == 0 or rec_t.size == 0:
        raise ValueError("both pulse trains must be non-empty to align them")

    lag, ratio = coarse_lag(log_t, rec_t, bin_s)
    scale, offset = 1.0, lag
    warnings: list[str] = []

    for tol in tolerances_s:
        ok, nearest = _pair(log_t, rec_t, scale, offset, tol)
        n_ok = int(ok.sum())
        if n_ok < 2:
            warnings.append(
                f"refinement stopped at a {tol * 1e3:.1f} ms tolerance with only "
                f"{n_ok} pair(s)"
            )
            break
        if fit_drift and n_ok >= MIN_PAIRS_FOR_DRIFT and np.ptp(log_t[ok]) > 0:
            design = np.vstack([log_t[ok], np.ones(n_ok)]).T
            sol, *_ = np.linalg.lstsq(design, nearest[ok], rcond=None)
            scale, offset = float(sol[0]), float(sol[1])
        else:
            # Not enough leverage for a slope: shift only, keeping the scale.
            offset = float(np.median(nearest[ok] - scale * log_t[ok]))

    ok, nearest = _pair(log_t, rec_t, scale, offset, tolerances_s[-1])
    residuals = nearest[ok] - (scale * log_t[ok] + offset)
    n_matched = int(ok.sum())

    # Candidates are the log pulses that fall inside the recorded window under
    # this mapping. Without this denominator, "500 matched" reads as success
    # whether the log holds 520 pulses or 20 000.
    if rec_t.size:
        lo = (rec_t[0] - offset) / scale
        hi = (rec_t[-1] - offset) / scale
        n_candidates = int(((log_t >= lo) & (log_t <= hi)).sum())
    else:  # pragma: no cover - guarded above
        n_candidates = 0

    med_abs = float(np.median(np.abs(residuals))) if residuals.size else float("nan")
    slope_ppm = float("nan")
    if residuals.size >= MIN_PAIRS_FOR_DRIFT and np.ptp(log_t[ok]) > 0:
        slope_ppm = float(np.polyfit(log_t[ok], residuals, 1)[0] * 1e6)

    drift_ppm = (scale - 1.0) * 1e6
    if abs(drift_ppm) > MAX_PLAUSIBLE_DRIFT_PPM:
        warnings.append(
            f"fitted drift is {drift_ppm:+.0f} ppm, far beyond the ~tens of ppm a "
            f"crystal of this class shows; the coarse lag has probably paired the "
            f"wrong pulses"
        )
    if ratio < 20:
        warnings.append(
            f"the correlation peak is only {ratio:.1f}x the background; these two "
            f"trains may not be the same session"
        )
    if n_candidates and n_matched / n_candidates < 0.5:
        warnings.append(
            f"only {100 * n_matched / n_candidates:.1f} % of the log pulses inside "
            f"the recorded window were matched"
        )

    return AlignmentResult(
        alignment=Alignment(
            scale=scale,
            offset_s=offset,
            method=AlignmentMethod.AUTO_MATCHED_FILTER,
            n_matched=n_matched,
            n_candidates=n_candidates,
            median_abs_residual_s=med_abs,
            residual_slope_ppm=slope_ppm,
            validated=False,
        ),
        residuals_s=residuals,
        matched_log_times_s=log_t[ok],
        matched_rec_times_s=nearest[ok],
        coarse_lag_s=lag,
        coarse_peak_ratio=ratio,
        n_candidates=n_candidates,
        warnings=tuple(warnings),
    )


def two_point_alignment(
    log_a_s: float, rec_a_s: float, log_b_s: float, rec_b_s: float
) -> Alignment:
    """Build a mapping from two hand-placed anchors.

    Parameters
    ----------
    log_a_s, rec_a_s
        First anchor: a log time and the recording time the user matched it to.
    log_b_s, rec_b_s
        Second anchor, as far from the first as possible.

    Returns
    -------
    Alignment
        Marked ``validated`` -- a human placed both points, which is the whole
        content of validation for this method.

    Raises
    ------
    ValueError
        If the two anchors share a log time, which fixes no scale.
    """
    if log_b_s == log_a_s:
        raise ValueError("the two anchors must be at different log times")
    scale = (rec_b_s - rec_a_s) / (log_b_s - log_a_s)
    offset = rec_a_s - scale * log_a_s
    return Alignment(
        scale=scale,
        offset_s=offset,
        method=AlignmentMethod.MANUAL_TWO_POINT,
        n_matched=2,
        n_candidates=2,
        median_abs_residual_s=0.0,
        residual_slope_ppm=0.0,
        validated=True,
    )


def nudge(alignment: Alignment, delta_s: float) -> Alignment:
    """Shift an alignment in time, keeping its scale.

    The result is marked :attr:`AlignmentMethod.MANUAL_NUDGE` and stays
    validated only if it already was: moving a fit by hand does not invalidate a
    human's earlier judgement, and does not manufacture one either.
    """
    return Alignment(
        scale=alignment.scale,
        offset_s=alignment.offset_s + delta_s,
        method=AlignmentMethod.MANUAL_NUDGE,
        n_matched=alignment.n_matched,
        n_candidates=alignment.n_candidates,
        median_abs_residual_s=alignment.median_abs_residual_s,
        residual_slope_ppm=alignment.residual_slope_ppm,
        validated=alignment.validated,
    )


def validate(
    result: AlignmentResult,
    *,
    max_median_residual_s: float = 0.001,
    min_match_fraction: float = 0.8,
    min_peak_ratio: float = 20.0,
) -> tuple[bool, tuple[str, ...]]:
    """Decide whether a fit is good enough to use without a human looking.

    Deliberately separate from :func:`estimate_alignment`: computing a mapping
    and accepting one are different acts, and conflating them is how an
    unchecked number ends up positioning every trial in a figure.

    Parameters
    ----------
    result
        The fit to judge.
    max_median_residual_s
        Largest acceptable median absolute residual. 1 ms is the scale at which
        a mis-positioned trial epoch starts to matter for this data.
    min_match_fraction
        Smallest acceptable matched-to-candidate ratio.
    min_peak_ratio
        Smallest acceptable coarse correlation peak-to-background ratio.

    Returns
    -------
    tuple
        ``(passed, reasons)``. ``reasons`` is empty when it passed and lists
        every failure otherwise -- all of them, so one round of fixing can
        address the lot.
    """
    reasons: list[str] = []
    a = result.alignment
    if not np.isfinite(a.median_abs_residual_s):
        reasons.append("no pairs matched, so there is no residual to judge")
    elif a.median_abs_residual_s > max_median_residual_s:
        reasons.append(
            f"median residual {a.median_abs_residual_s * 1e3:.3f} ms exceeds "
            f"{max_median_residual_s * 1e3:.3f} ms"
        )
    if result.match_fraction < min_match_fraction:
        reasons.append(
            f"matched {100 * result.match_fraction:.1f} % of candidates, below "
            f"{100 * min_match_fraction:.0f} %"
        )
    if result.coarse_peak_ratio < min_peak_ratio:
        reasons.append(
            f"correlation peak {result.coarse_peak_ratio:.1f}x background, below "
            f"{min_peak_ratio:.0f}x"
        )
    if abs(a.drift_ppm) > MAX_PLAUSIBLE_DRIFT_PPM:
        reasons.append(f"implausible drift {a.drift_ppm:+.0f} ppm")
    return (not reasons), tuple(reasons)
