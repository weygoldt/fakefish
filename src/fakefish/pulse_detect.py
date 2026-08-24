"""Matched-filter detection of electric-organ pulses.

Scope, stated so it is not mistaken for something larger: this finds pulses that
look like a **known template**. It is the tool for locating playback in a
recording and for measuring inter-pulse intervals. It is *not* a classifier and
makes no attempt to tell an animal's discharge from the stimulator's --
``eeltracker`` has a trained network for that, and re-implementing a worse one
here would be a trap.

Two properties are load-bearing:

**Both a robust threshold and an absolute floor.** A blockwise robust threshold
adapts to whatever noise it is measuring, so in silence it fires constantly. On
the sample recording an 8-sigma matched-filter threshold produced ~1300
"detections" in 40 s of pure noise floor (peak 0.0008 of full scale). The floor
is what makes the threshold mean something across a whole file.

**The template can be learned from the data.** The emitted ``EOD_HV`` is
monophasic; what a submerged electrode records is biphasic, because coupling and
the amplifier's high-pass differentiate it (``docs/RECON.md`` §3.4, §4.2).
Cross-correlating the emitted shape against the recorded one throws away SNR and
biases the peak. :func:`refine_template` closes that loop.
"""

# ADOPTED from playback-explorer (core/detect.py) on 2026-08-24, when that project's
# browser UI was retired in favour of claudian. It is fakefish's code now -- there is no
# upstream to re-drop from, so this is deliberately NOT a vendored copy under the
# invariant 10/11 discipline (no VENDORED_SHA256, no re-drop rule). Its tests came with
# it: tests/test_clock_align.py and tests/test_pulse_detect.py.

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.signal import fftconvolve

__all__ = [
    "DetectionParams",
    "Detections",
    "detect_pulses",
    "normalise_template",
    "refine_template",
    "robust_scale",
]


@dataclass(frozen=True, slots=True)
class DetectionParams:
    """Everything that changes which pulses are found."""

    snr_threshold: float = 8.0
    """Matched-filter peak height, in robust standard deviations."""

    absolute_floor: float = 0.0
    """Minimum raw peak amplitude, as a fraction of full scale.

    Zero disables it, which is only sensible on synthetic data. Use
    :func:`suggest_absolute_floor` to derive one from the recording.
    """

    refractory_s: float = 0.002
    """Minimum separation between detections. The emitted pulse's energy width
    and the source detector's own resolution limit are both 2 ms."""

    polarity: str = "both"
    """``"positive"``, ``"negative"`` or ``"both"``.

    ``"both"`` rectifies the matched-filter output, which is required here: the
    firmware flips each pulse's polarity at random and records the flip in the
    log's ``pol`` column, so a sign-sensitive detector would miss half of them.
    """

    sidelobe_ratio: float = 0.35
    """Drop a detection weaker than this fraction of a nearby stronger one.

    Correlating a **biphasic** pulse against a biphasic template produces
    secondary maxima either side of the true one -- measured on the synthetic
    fixture, a sidelobe at exactly +2.0 ms carrying 5 % of the true peak's
    score. The refractory window does not remove it, because 2.0 ms is exactly
    the separation the refractory permits.

    A spurious detection 2 ms from a real one is nearly harmless for alignment
    (nothing in the log matches it) and actively wrong for inter-pulse
    intervals, which is a primary view. Set to 0 to disable.
    """

    sidelobe_window_s: float = 0.006
    """How far to look for the stronger neighbour. Three refractory periods:
    wide enough to cover the template's own sidelobes, narrow enough that two
    genuine pulses in a 300 Hz volley (3.3 ms apart) are only suppressed if one
    is less than a third the height of the other, which is not what a volley
    looks like."""


@dataclass(frozen=True, slots=True)
class Detections:
    """Detected pulses and the numbers needed to judge them."""

    times_s: NDArray[np.float64]
    """Time of each pulse's **largest excursion**, seconds, on the input's clock.

    The pulse's own peak, not the matched filter's peak and not the template's
    centre. Those differ by a constant that depends on the template's shape --
    0.54 ms for the recorded biphasic pulse -- and picking the wrong one is
    invisible in an alignment while breaking anything that cuts a window around
    a detection.
    """

    scores: NDArray[np.float64]
    """Matched-filter response at each peak, in robust standard deviations."""

    amplitudes: NDArray[np.float64]
    """Raw signal peak within each detection's window, fraction of full scale."""

    polarities: NDArray[np.int8]
    """``+1`` or ``-1``: which sign of the template matched."""

    noise_scale: float
    """The robust scale the threshold was measured against."""

    threshold: float
    """The score a peak had to clear."""

    n_rejected_by_floor: int
    """Peaks that cleared the SNR threshold but not the amplitude floor.

    Reported rather than discarded silently: a large number here means the floor
    is doing the work and the SNR threshold is set too low.
    """

    @property
    def n(self) -> int:
        """How many pulses were detected."""
        return int(self.times_s.size)

    def ipi_s(self) -> NDArray[np.float64]:
        """Inter-pulse intervals in seconds. Length ``n - 1``."""
        return np.diff(self.times_s)

    def rate_hz(self) -> NDArray[np.float64]:
        """Instantaneous rate at each interval, in Hz."""
        ipi = self.ipi_s()
        with np.errstate(divide="ignore"):
            return np.where(ipi > 0, 1.0 / ipi, np.inf)


def robust_scale(x: NDArray[np.floating]) -> float:
    """Return a median-absolute-deviation estimate of the noise scale.

    Scaled by 1/0.6745 so it matches a standard deviation for Gaussian noise.
    Robust because the signal being searched for is, by construction, the part
    of the data that is not noise -- a plain standard deviation would measure the
    pulses and then be used as a threshold for finding them.
    """
    med = np.median(x)
    mad = float(np.median(np.abs(x - med)))
    return mad / 0.6745 if mad > 0 else float(np.std(x)) or 1e-12


def normalise_template(template: NDArray[np.floating]) -> NDArray[np.float64]:
    """Zero-mean and unit-norm a template.

    Zero-mean so a DC offset in the recording cannot produce a response, and
    unit-norm so the matched-filter output is in the signal's own units and
    comparable between templates.
    """
    t = np.asarray(template, dtype=np.float64)
    t = t - t.mean()
    norm = float(np.linalg.norm(t))
    if norm == 0:
        raise ValueError("template is all zeros after removing its mean")
    return t / norm


def suggest_absolute_floor(
    x: NDArray[np.floating], fraction: float = 0.05, percentile: float = 99.9
) -> float:
    """Derive an amplitude floor from the recording's own dynamic range.

    A fixed number would be wrong at a different gain or a different distance,
    so the floor is a fraction of the signal's high percentile. The percentile
    rather than the maximum, because one clipped sample must not set it.

    Parameters
    ----------
    x
        Samples, fraction of full scale.
    fraction
        Fraction of the percentile to use.
    percentile
        Which percentile stands in for "a real pulse".

    Returns
    -------
    float
    """
    return float(fraction * np.percentile(np.abs(x), percentile))


def detect_pulses(
    x: NDArray[np.floating],
    sample_rate_hz: float,
    template: NDArray[np.floating],
    params: DetectionParams | None = None,
    t0_s: float = 0.0,
) -> Detections:
    """Find pulses matching ``template`` in one channel.

    Parameters
    ----------
    x
        1-D samples, fraction of full scale.
    sample_rate_hz
        Sample rate of ``x``.
    template
        Pulse shape at the **same** sample rate. Resample first if it came from
        a device running at a different rate.
    params
        Thresholds. Defaults are for a clean signal; set
        :attr:`DetectionParams.absolute_floor` on real data.
    t0_s
        Time of ``x[0]``, added to every returned time.

    Returns
    -------
    Detections

    Raises
    ------
    ValueError
        If ``x`` is not 1-D, or is shorter than the template.
    """
    p = params or DetectionParams()
    sig = np.asarray(x, dtype=np.float64)
    if sig.ndim != 1:
        raise ValueError(f"expected one channel, got shape {sig.shape}")
    tmpl = normalise_template(template)
    if sig.size < tmpl.size:
        raise ValueError(
            f"signal is {sig.size} samples, shorter than the {tmpl.size}-sample template"
        )

    sig = sig - sig.mean()
    response = fftconvolve(sig, tmpl[::-1], mode="same")

    # Convert a correlation index into the index of the pulse's own peak.
    #
    # With 'same' mode, a correlation maximum at index i means the template
    # STARTS at i - len(tmpl)//2, so the template's largest excursion lands at
    # i - len(tmpl)//2 + argmax|tmpl|. Reporting i directly would put every
    # detection at the template's geometric centre instead -- for the recorded
    # biphasic pulse that is a constant +0.54 ms error, which is invisible in an
    # alignment (a constant offset is absorbed by `offset_s`) and quietly
    # destroys template refinement, whose snippets would all be extracted 26
    # samples off centre and average to noise.
    peak_shift = int(np.argmax(np.abs(tmpl))) - tmpl.size // 2

    if p.polarity == "positive":
        magnitude = response
    elif p.polarity == "negative":
        magnitude = -response
    else:
        magnitude = np.abs(response)

    scale = robust_scale(response)
    threshold = p.snr_threshold * scale

    refractory = max(round(p.refractory_s * sample_rate_hz), 1)
    found = _peaks_above(magnitude, threshold, refractory)
    if p.sidelobe_ratio > 0 and found.size > 1:
        window = max(round(p.sidelobe_window_s * sample_rate_hz), 1)
        found = _drop_sidelobes(found, magnitude[found], window, p.sidelobe_ratio)
    peaks = np.clip(found + peak_shift, 0, sig.size - 1)

    if peaks.size == 0:
        empty_f = np.empty(0, dtype=np.float64)
        return Detections(
            times_s=empty_f,
            scores=empty_f,
            amplitudes=empty_f,
            polarities=np.empty(0, dtype=np.int8),
            noise_scale=scale,
            threshold=threshold,
            n_rejected_by_floor=0,
        )

    half = tmpl.size // 2
    amps = np.array(
        [
            float(np.abs(sig[max(i - half, 0) : min(i + half + 1, sig.size)]).max())
            for i in peaks
        ]
    )
    keep = amps >= p.absolute_floor
    n_rejected = int((~keep).sum())
    peaks, found, amps = peaks[keep], found[keep], amps[keep]

    return Detections(
        times_s=t0_s + peaks / float(sample_rate_hz),
        scores=magnitude[found] / scale,
        amplitudes=amps,
        polarities=np.where(response[found] >= 0, 1, -1).astype(np.int8),
        noise_scale=scale,
        threshold=threshold,
        n_rejected_by_floor=n_rejected,
    )


def _peaks_above(
    magnitude: NDArray[np.float64], threshold: float, refractory: int
) -> NDArray[np.int64]:
    """Return local maxima above ``threshold``, at least ``refractory`` apart.

    Greedy by descending height rather than left-to-right: with a refractory
    window, taking the first crossing can suppress a larger peak a few samples
    later, which biases every interval that follows it.
    """
    above = np.flatnonzero(magnitude > threshold)
    if above.size == 0:
        return np.empty(0, dtype=np.int64)

    order = above[np.argsort(magnitude[above])[::-1]]
    taken: list[int] = []
    blocked = np.zeros(magnitude.size, dtype=bool)
    for i in order:
        if blocked[i]:
            continue
        taken.append(int(i))
        lo = max(int(i) - refractory + 1, 0)
        hi = min(int(i) + refractory, magnitude.size)
        blocked[lo:hi] = True
    return np.array(sorted(taken), dtype=np.int64)


def _drop_sidelobes(
    indices: NDArray[np.int64],
    scores: NDArray[np.float64],
    window: int,
    ratio: float,
) -> NDArray[np.int64]:
    """Remove peaks that are much weaker than a close neighbour.

    A detection is dropped when another detection within ``window`` samples is
    more than ``1 / ratio`` times as strong. That is the signature of a
    template sidelobe; two genuine pulses in a volley are of comparable height.

    Parameters
    ----------
    indices
        Peak positions, sorted ascending.
    scores
        Matched-filter magnitude at each peak.
    window
        Neighbourhood half-width, samples.
    ratio
        Height ratio below which a peak counts as a sidelobe.

    Returns
    -------
    numpy.ndarray
        The surviving indices, still sorted.
    """
    keep = np.ones(indices.size, dtype=bool)
    for i in range(indices.size):
        lo = np.searchsorted(indices, indices[i] - window, side="left")
        hi = np.searchsorted(indices, indices[i] + window, side="right")
        neighbourhood = scores[lo:hi]
        if neighbourhood.size > 1 and scores[i] < ratio * neighbourhood.max():
            keep[i] = False
    return indices[keep]


def refine_template(
    x: NDArray[np.floating],
    detections: Detections,
    sample_rate_hz: float,
    width_s: float = 0.004,
    max_snippets: int = 200,
    t0_s: float = 0.0,
) -> NDArray[np.float64]:
    """Re-estimate the pulse shape from the strongest detections.

    The recorded shape is not the emitted one, so this closes the loop: detect
    with the emitted template, average what was found, and re-detect with the
    average. Snippets are **sign-aligned** before averaging -- the firmware flips
    polarity per pulse, so averaging raw snippets would cancel the pulse and
    return noise.

    Parameters
    ----------
    x
        The same 1-D signal the detections came from.
    detections
        Output of :func:`detect_pulses`.
    sample_rate_hz
        Sample rate.
    width_s
        Total width of the extracted template.
    max_snippets
        How many of the strongest detections to average.
    t0_s
        Time of ``x[0]``, matching what was passed to :func:`detect_pulses`.

    Returns
    -------
    numpy.ndarray
        A zero-mean, unit-norm template.

    Raises
    ------
    ValueError
        If no complete snippet can be extracted.
    """
    sig = np.asarray(x, dtype=np.float64)
    width = max(round(width_s * sample_rate_hz), 8)
    half = width // 2

    order = np.argsort(detections.scores)[::-1][:max_snippets]
    snippets = []
    for k in order:
        centre = round((detections.times_s[k] - t0_s) * sample_rate_hz)
        lo, hi = centre - half, centre - half + width
        if lo < 0 or hi > sig.size:
            continue
        snip = sig[lo:hi]
        # Sign-align: without this the random per-pulse polarity averages away.
        snippets.append(snip * float(detections.polarities[k]))

    if not snippets:
        raise ValueError("no complete snippet could be extracted; widen the signal")
    mean = np.mean(np.stack(snippets), axis=0)
    return normalise_template(mean)
