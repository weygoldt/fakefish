"""Derived session structure from a pulse log — what happened, and when.

:mod:`fakefish.pulse_log` is the *reader*: it turns a ``PULSnnnn.CSV`` into rows and
says whether the file is intact. This module is the layer above it — it turns those
rows into the session's **structure**, which is what an analysis and the overview
figure both need:

* :func:`loc_runs` — the localization train's on/off spans, **classified by what
  ended each one**;
* :func:`trials` — one record per trial, with its marker, its outcome and its volley;
* :func:`control_track` — the operator's knobs as time series;
* :func:`loc_intervals` — the realised inter-pulse intervals of the resting rhythm;
* :func:`summarise` — the headline numbers, including the two the 2026-08-22
  supply-offset fault made worth checking every session.

Nothing here plots — but not because matplotlib is optional. It is a hard dependency of
the package, and the deck figure system in :mod:`fakefish.viz` is the standard every
figure goes through. The split is the repo's existing shape: :mod:`fakefish.pulse_log`
reads, ``plot_*`` modules draw, and the layer in between is the one an analysis wants to
import on its own. Concretely, ``fakefish-session stats`` needs these numbers and no
figure, and these functions are the ones worth asserting on in tests.

**Why the run classification is the load-bearing part.** A ``LOCOFF`` row means the
localization train stopped, and it is written for *two completely different reasons*:
the operator released the throttle, or a trial preempted the train (``begin_marker``
logs one before the marker starts). Counting them together is actively misleading —
on the 2026-08-22 log a naive count said the gate cycled 27 times, when 20 of those
were trials and only 7 were the throttle. The difference was the whole diagnosis.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from fakefish.pulse_log import PulseLogFile, PulseRecord

__all__ = [
    "LocRun",
    "Trial",
    "ControlTrack",
    "SessionSummary",
    "loc_runs",
    "trials",
    "control_track",
    "loc_intervals",
    "summarise",
]


# ---------------------------------------------------------------------------
# Localization runs
# ---------------------------------------------------------------------------

#: Why a localization run ended.
ENDED_BY_TRIAL = "trial"
ENDED_BY_GATE = "gate"
ENDED_BY_EOF = "eof"


@dataclass(frozen=True)
class LocRun:
    """One continuous stretch of the localization train being enabled."""

    start_s: float
    end_s: float
    n_pulses: int
    ended_by: str
    """``"trial"`` (a trial preempted it), ``"gate"`` (the operator released the
    throttle, or logging failed), or ``"eof"`` (the file ended while it ran).

    Only ``"gate"`` says anything about the operator's throttle. Pooling the three is
    how a 7-release session reads as a 27-release one.
    """

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def loc_runs(log: PulseLogFile) -> list[LocRun]:
    """The localization train's enabled spans, in order, classified by what ended them.

    A run is ``LOCON`` … ``LOCOFF``. The classification reads the row *after* the
    ``LOCOFF``: the firmware writes ``LOCOFF`` immediately before ``TRIAL``/``MARKER``
    when a trial preempts the train, and writes it alone when the gate released.
    """
    rate = float(log.sample_rate_hz)
    runs: list[LocRun] = []
    start: Optional[int] = None
    pulses = 0

    for i, rec in enumerate(log.records):
        if rec.event == "LOCON":
            start = rec.tick
            pulses = 0
        elif rec.event == "LOC":
            pulses += 1
        elif rec.event == "LOCOFF" and start is not None and rec.tick is not None:
            nxt = log.records[i + 1].event if i + 1 < len(log.records) else None
            ended = ENDED_BY_TRIAL if nxt in ("TRIAL", "MARKER") else ENDED_BY_GATE
            runs.append(LocRun(start / rate, rec.tick / rate, pulses, ended))
            start = None
            pulses = 0

    if start is not None:
        last = max((r.tick for r in log.records if r.tick is not None), default=start)
        runs.append(LocRun(start / rate, last / rate, pulses, ENDED_BY_EOF))
    return runs


# ---------------------------------------------------------------------------
# Trials
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trial:
    """One trial and everything it emitted — which for one of the three arms is nothing.

    The three arms (v4) are VOLLEY, BASELINE and SILENCE. They are all the same
    LENGTH: every trial draws a library item and the two non-volley arms hold for
    exactly the duration that item would have taken. :attr:`item` is therefore
    populated for all three, and is the only record of how long a silent arm ran.
    """

    trial_id: int
    start_s: float
    requested: Optional[str]
    resolved: Optional[str]
    blinded: bool
    marker_pulses: int
    volley_pulses: int
    base_pulses: int
    """Pulses emitted by a BASELINE arm — a fish present and not hunting. Counted
    separately from the ambient localization train because they sit at the same
    amplitude, so nothing else could tell the treatment from the fish beside it."""
    item: Optional[int]
    volley_span_s: float
    """Onset of the first emitted pulse to onset of the last, for whichever arm ran.
    0.0 for SILENCE, which emits nothing at all — that is the no-stimulus control, not
    a missing measurement, and it is also 0.0 for a BASELINE arm that emitted a single
    pulse, which is common: the median arm carries about two."""

    @property
    def is_volley(self) -> bool:
        return self.resolved == "V"

    @property
    def is_baseline(self) -> bool:
        """A resting-rhythm arm: a fish is present and NOT hunting."""
        return self.resolved == "B"

    @property
    def is_silence(self) -> bool:
        """The no-stimulus arm. ``S`` is the SILENCE arm; the code is the old two-arm
        SHAM character, kept because the quantity is unchanged."""
        return self.resolved == "S"


def trials(log: PulseLogFile) -> list[Trial]:
    """One :class:`Trial` per ``TRIAL`` row, with its marker and volley folded in."""
    rate = float(log.sample_rate_hz)
    by_id: dict[int, list[PulseRecord]] = {}
    for rec in log.records:
        if rec.trial:
            by_id.setdefault(rec.trial, []).append(rec)

    out: list[Trial] = []
    for rec in log.events("TRIAL"):
        if rec.trial is None or rec.tick is None:
            continue
        rows = by_id.get(rec.trial, [])
        markers = [r for r in rows if r.event == "MARKER"]
        volleys = [r for r in rows if r.event == "VOLLEY" and r.tick is not None]
        bases = [r for r in rows if r.event == "BASE" and r.tick is not None]
        # Whichever arm ran, the span is over ITS pulses. A volley and a baseline arm
        # never coexist in one trial, so concatenating is unambiguous.
        emitted = volleys + bases
        span = (emitted[-1].tick - emitted[0].tick) / rate if len(emitted) > 1 else 0.0
        # v4 puts the drawn item on the TRIAL row for every arm, which is the only place
        # a SILENCE arm records one. Fall back to the pulses so v2/v3 files still resolve
        # a volley's item, where the TRIAL row's column was empty.
        item = rec.item
        if item is None:
            item = next((r.item for r in emitted if r.item is not None), None)
        out.append(
            Trial(
                trial_id=rec.trial,
                start_s=rec.tick / rate,
                requested=rec.req,
                resolved=rec.res,
                blinded=bool(rec.blinded),
                marker_pulses=len(markers),
                volley_pulses=len(volleys),
                base_pulses=len(bases),
                item=item,
                volley_span_s=span,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Control track
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlTrack:
    """The operator's settings as time series, sampled at every logged row.

    Every row carries the settings in force at that instant — that is the format's own
    design, so that a file torn by a power cut stays interpretable up to the tear — which
    means the control track needs no separate settings events and has no interpolation.
    """

    t_s: np.ndarray
    tick_hz: np.ndarray
    """Commanded localization TICK TEMPO (one over the median interval), Hz. **Not** the
    realised pulse rate: the interval distribution is heavy-tailed, so at randomness 1.0 a
    5 Hz tick delivers roughly 3.3 pulses per second."""
    randomness: np.ndarray
    master_amp: np.ndarray

    ch_us: Optional[np.ndarray]
    """``(n, 4)`` raw decoded RC widths in µs, or ``None`` on a pre-v3 log.

    The near end of the decode chain: every other array here is derived from these through
    a calibration and a quantiser.
    """
    zero_us: Optional[np.ndarray]
    """The captured session zero in µs, or ``None`` on a pre-v3 log."""

    @property
    def has_raw_decode(self) -> bool:
        return self.ch_us is not None

    @property
    def throttle_above_zero_us(self) -> Optional[np.ndarray]:
        """CH3 width minus the session zero — the throttle's true travel, in µs.

        This is the number the whole 2026-08-22 investigation was trying to see. At rest it
        must sit at ~0; anything else means the zero is stale or was never captured.
        """
        if self.ch_us is None or self.zero_us is None:
            return None
        return self.ch_us[:, 0] - self.zero_us


def control_track(log: PulseLogFile) -> ControlTrack:
    """Every row's settings, as parallel arrays. Rows with no tick are skipped."""
    rate = float(log.sample_rate_hz)
    rows = [r for r in log.records if r.tick is not None]

    def col(pick, scale=1.0):
        return np.array(
            [np.nan if pick(r) is None else pick(r) * scale for r in rows], dtype=float
        )

    t = np.array([r.tick for r in rows], dtype=float) / rate
    hz = np.array(
        [
            np.nan if (r.tick_ipi is None or r.tick_ipi <= 0) else rate / r.tick_ipi
            for r in rows
        ],
        dtype=float,
    )

    raw = None
    zero = None
    if any(r.ch_us[0] is not None for r in rows):
        raw = np.array(
            [[np.nan if v is None else float(v) for v in r.ch_us] for r in rows],
            dtype=float,
        )
        zero = col(lambda r: r.zero_us)

    return ControlTrack(
        t_s=t,
        tick_hz=hz,
        randomness=col(lambda r: r.rand_m, 1e-3),
        master_amp=col(lambda r: r.master_m, 1e-3),
        ch_us=raw,
        zero_us=zero,
    )


# ---------------------------------------------------------------------------
# The resting rhythm as realised
# ---------------------------------------------------------------------------


def loc_intervals(log: PulseLogFile) -> tuple[np.ndarray, np.ndarray]:
    """``(t_s, ipi_s)`` for consecutive localization pulses **within one run**.

    Intervals that straddle a run boundary are dropped: the gap between one run's last
    pulse and the next run's first is the operator's throttle and a trial, not the fish's
    rhythm, and pooling them would put a spurious multi-second tail into every summary.
    """
    rate = float(log.sample_rate_hz)
    runs = loc_runs(log)
    ticks = np.array(
        [r.tick for r in log.pulses("LOC") if r.tick is not None], dtype=float
    )
    if ticks.size < 2:
        return np.zeros(0), np.zeros(0)

    t = ticks / rate
    keep_t: list[float] = []
    keep_d: list[float] = []
    for run in runs:
        inside = t[(t >= run.start_s) & (t <= run.end_s)]
        if inside.size < 2:
            continue
        keep_t.extend(inside[1:].tolist())
        keep_d.extend(np.diff(inside).tolist())
    return np.array(keep_t), np.array(keep_d)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionSummary:
    """Headline numbers for one session."""

    duration_s: float
    n_loc: int
    n_marker: int
    n_volley: int
    n_base: int
    """Pulses emitted by BASELINE arms, across the whole session. Kept apart from
    ``n_loc`` on purpose: both are localization-amplitude pulses, so pooling them would
    make the treatment unrecoverable from the summary."""
    n_trials: int
    n_volley_trials: int
    n_baseline_trials: int
    n_sham_trials: int
    """Trials that resolved to the SILENCE arm. Named for the ``S`` code, which is the
    two-arm design's SHAM character kept because the quantity never changed."""
    n_unresolved_trials: int
    """Trials whose ``res`` this reader does not recognise — a newer firmware, or a
    corrupt column. Counted rather than folded into an arm, so the three arm totals
    always mean what they say."""
    n_blinded: int

    loc_runs_total: int
    loc_runs_gate: int
    """Runs ended by the operator releasing the throttle. The only count that says
    anything about the throttle — the rest were trials preempting the train."""
    loc_runs_trial: int

    tick_hz_min: float
    tick_hz_max: float
    randomness_min: float
    randomness_max: float

    ipi_median_s: float
    ipi_cv2: float
    """Median CV2 = median of ``2|d(i+1) - d(i)| / (d(i+1) + d(i))``, the local
    irregularity measure the fitted rhythm is calibrated against (~0.38 in the source
    recordings, ~0.42 at randomness 1.0). Scale-free, so it is comparable across tempos."""

    throttle_reached_zero: Optional[bool]
    """Did the throttle ever decode to its own zero? ``None`` on a pre-v3 log.

    The direct form of the question that took a whole log inversion to answer on
    2026-08-22, when the answer was no and localization therefore could not be stopped.
    """
    zero_us: Optional[int]
    throttle_span_us: Optional[float]


def _cv2(d: np.ndarray) -> float:
    if d.size < 2:
        return float("nan")
    a, b = d[:-1], d[1:]
    denom = a + b
    ok = denom > 0
    if not np.any(ok):
        return float("nan")
    return float(np.median(2.0 * np.abs(b[ok] - a[ok]) / denom[ok]))


def summarise(log: PulseLogFile) -> SessionSummary:
    """Reduce a session to the numbers worth printing."""
    rate = float(log.sample_rate_hz)
    ticks = [r.tick for r in log.records if r.tick is not None]
    duration = (max(ticks) - min(ticks)) / rate if len(ticks) > 1 else 0.0

    runs = loc_runs(log)
    tr = trials(log)
    track = control_track(log)
    _, ipi = loc_intervals(log)

    def rng(a: np.ndarray) -> tuple[float, float]:
        finite = a[np.isfinite(a)]
        if finite.size == 0:
            return float("nan"), float("nan")
        return float(finite.min()), float(finite.max())

    hz_lo, hz_hi = rng(track.tick_hz)
    rnd_lo, rnd_hi = rng(track.randomness)

    reached_zero: Optional[bool] = None
    zero_us: Optional[int] = None
    span_us: Optional[float] = None
    above = track.throttle_above_zero_us
    if above is not None:
        finite = above[np.isfinite(above)]
        if finite.size:
            # The zero is a running minimum of the width, so "reached zero" is exact
            # equality, not a tolerance: the firmware cannot report a width below it.
            reached_zero = bool(finite.min() <= 0.0)
            span_us = float(finite.max() - finite.min())
        z = track.zero_us[np.isfinite(track.zero_us)] if track.zero_us is not None else None
        if z is not None and z.size:
            zero_us = int(z[-1])

    return SessionSummary(
        duration_s=duration,
        n_loc=len(log.pulses("LOC")),
        n_marker=len(log.pulses("MARKER")),
        n_volley=len(log.pulses("VOLLEY")),
        n_base=len(log.pulses("BASE")),
        n_trials=len(tr),
        n_volley_trials=sum(1 for t in tr if t.resolved == "V"),
        n_baseline_trials=sum(1 for t in tr if t.resolved == "B"),
        n_sham_trials=sum(1 for t in tr if t.resolved == "S"),
        n_unresolved_trials=sum(1 for t in tr if t.resolved not in ("V", "B", "S")),
        n_blinded=sum(1 for t in tr if t.blinded),
        loc_runs_total=len(runs),
        loc_runs_gate=sum(1 for r in runs if r.ended_by == ENDED_BY_GATE),
        loc_runs_trial=sum(1 for r in runs if r.ended_by == ENDED_BY_TRIAL),
        tick_hz_min=hz_lo,
        tick_hz_max=hz_hi,
        randomness_min=rnd_lo,
        randomness_max=rnd_hi,
        ipi_median_s=float(np.median(ipi)) if ipi.size else float("nan"),
        ipi_cv2=_cv2(ipi),
        throttle_reached_zero=reached_zero,
        zero_us=zero_us,
        throttle_span_us=span_us,
    )
