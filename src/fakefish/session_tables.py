"""Turn a device pulse log into tables a person can read.

The log the device writes is built for a microcontroller and for a file that may
be cut off mid-row by a power loss: a ``#key=value`` preamble, then ONE table
holding pulses, trial markers and housekeeping together, with the operator's
control settings repeated on every single row. That is the right shape for the
firmware. It is a poor shape for anyone opening it in a spreadsheet.

Three things make it hard to read, and each is fixed here:

* **One table, many kinds of row.** A ``val`` column meant the file index on a
  ``BOOT`` row, a unix clock on ``ANCHOR``, and a count of lost records on
  ``DROP``. Splitting by what the row *is* lets every column mean one thing.
* **Settings repeated on every row.** ``master_m``/``rand_m``/``tick_ipi`` and
  the four raw radio widths are stamped on all ~2400 rows because a truncated
  file must stay interpretable up to the tear. They become
  :func:`controls_table`, one row per *change* -- lossless under an as-of join,
  and a control track a plot can use directly.
* **Machine units.** ``amp_m = 225`` is 0.225 of full scale; ``tick_ipi`` is an
  interval in samples. Real units here, with the exact integers kept beside them
  so nothing is lost.

NOTHING IS DROPPED. Every column of the source becomes a named column here, or a
key in the metadata, or (for ``seq``) a ``source_row`` back-reference that ties
each row to the exact line of the device log it came from.

The source log is never modified. It is the device's record and the only
primary artifact; everything here is derived and regenerable.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

import numpy as np
import polars as pl

from fakefish.pulse_log import PulseLogFile

#: ``event`` values in the device log that represent one emitted pulse, mapped to
#: the word this writes. ``BASE`` is a baseline-arm pulse and is deliberately not
#: folded into localization: both sit at the same amplitude, so once the ambient
#: train resumes beside an arm nothing else could separate treatment from
#: background.
PULSE_TYPES = {
    "LOC": "localization",
    "BASE": "baseline",
    "VOLLEY": "volley",
    "MARKER": "marker",
}

#: The log's one-character arm codes. ``S`` is the SILENCE arm -- the character is
#: the old two-arm design's SHAM code, kept in the log because the quantity never
#: changed, and spelled out here because "sham" says less than "silence" does.
TREATMENTS = {"V": "volley", "B": "baseline", "S": "silence"}

#: What was asked for, as opposed to what the firmware drew. ``random`` is the
#: blinded request and the only one a current device makes.
REQUESTS = {"R": "random", "V": "volley", "B": "baseline", "S": "silence"}

#: Housekeeping rows, mapped to a word that says what happened.
SESSION_EVENTS = {
    "BOOT": "boot",
    "ANCHOR": "clock_anchor",
    "LINK": "radio_link",
    "DROP": "records_dropped",
    "GAP": "log_gap",
    "LOCON": "localization_started",
    "LOCOFF": "localization_stopped",
    "SHAM": "silence_arm",
}

#: The four radio channels, in the order the log stores them, named for what the
#: operator actually holds. ``rc_control.h``: CH3 throttle, CH4 trigger, CH5
#: randomness, CH6 amplitude.
RADIO_CHANNELS = (
    "throttle_pulse_us",
    "trigger_pulse_us",
    "randomness_pulse_us",
    "amplitude_pulse_us",
)

#: Devices, by the log's ``surface`` key.
DEVICES = {0: "eel_fakefish_rc", 1: "eel_fakefish_button"}


@dataclass(frozen=True)
class TimeBase:
    """How to turn a device sample tick into the times the tables carry.

    ``recording`` maps device seconds onto a recording's clock
    (``t_rec = scale * t_log + offset``). When it is absent the tables carry
    device time only, and every ``recording_time_s`` column is left out rather
    than filled with something untrue.
    """

    sample_rate_hz: float
    scale: Optional[float] = None
    offset_s: Optional[float] = None

    @property
    def has_recording(self) -> bool:
        return self.scale is not None and self.offset_s is not None

    def seconds(self, ticks: np.ndarray) -> np.ndarray:
        return np.asarray(ticks, dtype=np.float64) / self.sample_rate_hz

    def recording_seconds(self, ticks: np.ndarray) -> np.ndarray:
        if not self.has_recording:
            raise ValueError("no recording alignment in this time base")
        return self.scale * self.seconds(ticks) + self.offset_s


def _f(a, places: int = 6) -> np.ndarray:
    """Round a float column once, vectorised, rather than per row."""
    return np.round(np.asarray(a, dtype=np.float64), places)


def _opt_int(values) -> pl.Series:
    """An integer column whose absent entries are null, never 0 and never -1.

    The device log's own rule, and for the same reason: ``STIM_ITEMS[0]`` is a
    real recorded volley and ``STIM_ITEMS[-1]`` does not raise in Python, so a
    written stand-in becomes a silent misattribution.
    """
    return pl.Series([None if v is None else int(v) for v in values], dtype=pl.Int64)


def _milli(v: Optional[int]) -> Optional[float]:
    """A milli-unit integer as the fraction it means."""
    return None if v is None else v / 1000.0


def _add_recording_time(frame: pl.DataFrame, ticks, tb: TimeBase, name: str) -> pl.DataFrame:
    """Insert a recording-clock column right after the device-time one."""
    if not tb.has_recording or frame.height == 0:
        return frame
    col = pl.Series(name, _f(tb.recording_seconds(np.asarray(ticks, dtype=np.int64))))
    at = frame.columns.index("time_s") + 1
    return frame.insert_column(at, col)


# --------------------------------------------------------------------------- #
# pulses
# --------------------------------------------------------------------------- #
def pulses_table(log: PulseLogFile, tb: TimeBase) -> pl.DataFrame:
    """One row per pulse the device actually put in the water.

    ``treatment`` is carried on the pulse rather than left to a join: a viewer
    filtering "show me the baseline arms" should not have to reconstruct the
    trial table first. It is empty for the ambient localization train, which
    belongs to no trial.
    """
    recs = [r for r in log.records if r.event in PULSE_TYPES and r.tick is not None]
    arm_of = {t.trial: TREATMENTS.get(t.res or "") for t in log.trials() if t.trial is not None}
    ticks = np.array([r.tick for r in recs], dtype=np.int64)

    frame = pl.DataFrame(
        {
            "time_s": _f(tb.seconds(ticks)),
            "pulse_type": [PULSE_TYPES[r.event] for r in recs],
            "trial_number": _opt_int([r.trial for r in recs]),
            "treatment": [
                arm_of.get(r.trial) if r.trial is not None else None for r in recs
            ],
            "amplitude": pl.Series(
                [_milli(r.amp_m) for r in recs], dtype=pl.Float64
            ).round(4),
            "polarity": _opt_int([r.pol for r in recs]),
            "stimulus_item": _opt_int([r.item for r in recs]),
            "pulse_index_in_item": _opt_int([r.pulse for r in recs]),
            "sample_tick": pl.Series(ticks, dtype=pl.Int64),
            "source_row": pl.Series([r.seq for r in recs], dtype=pl.Int64),
        }
    )
    return _add_recording_time(frame, ticks, tb, "recording_time_s")


# --------------------------------------------------------------------------- #
# trials
# --------------------------------------------------------------------------- #
def trials_table(
    log: PulseLogFile, tb: TimeBase, item_durations_s: Optional[dict[int, float]] = None
) -> pl.DataFrame:
    """One row per trial, with when it started and when it stopped.

    THE SILENCE ARM EXISTS ONLY HERE. It emits nothing, so it has no pulse rows
    anywhere -- on a real session that is a third of the trials, and it is the
    control condition. A view built on pulses alone shows the treatment and
    silently omits what it is meant to be compared against.

    ``ended_s`` comes from the LENGTH OF THE DRAWN ITEM, never from the last
    pulse. All three arms draw an item and the two silent ones hold for exactly
    its duration, which is what makes the arms the same length by construction;
    a baseline arm carrying a single pulse still occupies its whole window and
    measured from its pulses would collapse to an instant.
    """
    trials = [t for t in log.trials() if t.tick is not None and t.trial is not None]
    by_trial: dict[int, list] = {}
    for r in log.records:
        if r.trial is not None and r.event in PULSE_TYPES:
            by_trial.setdefault(r.trial, []).append(r)

    ticks = np.array([t.tick for t in trials], dtype=np.int64)
    starts = tb.seconds(ticks)
    durations = [
        None if (item_durations_s is None or t.item is None)
        else item_durations_s.get(t.item)
        for t in trials
    ]
    ends = np.array(
        [s + d if d is not None else np.nan for s, d in zip(starts, durations, strict=True)]
    )

    frame = pl.DataFrame(
        {
            "trial_number": pl.Series([t.trial for t in trials], dtype=pl.Int64),
            "treatment": [TREATMENTS.get(t.res or "") for t in trials],
            "requested": [REQUESTS.get(t.req or "") for t in trials],
            "was_blinded": pl.Series([t.req == "R" for t in trials], dtype=pl.Boolean),
            "time_s": _f(starts),
            "ended_s": pl.Series(_f(ends)).fill_nan(None),
            "duration_s": pl.Series(
                [np.nan if d is None else d for d in durations], dtype=pl.Float64
            ).round(6).fill_nan(None),
            "stimulus_item": _opt_int([t.item for t in trials]),
            "pulses_emitted": pl.Series(
                [len(by_trial.get(t.trial, [])) for t in trials], dtype=pl.Int64
            ),
            "polarity": _opt_int([t.pol for t in trials]),
            "sample_tick": pl.Series(ticks, dtype=pl.Int64),
            "source_row": pl.Series([t.seq for t in trials], dtype=pl.Int64),
        }
    )
    frame = _add_recording_time(frame, ticks, tb, "recording_time_s")
    if tb.has_recording and frame.height:
        # The end maps through the SAME affine fit as the start, rather than being
        # pasted on in device seconds -- at 14 ppm the difference is microseconds,
        # but it would be microseconds of the wrong quantity.
        end_ticks = ticks + np.nan_to_num(np.asarray(durations, dtype=np.float64)) * tb.sample_rate_hz
        rec_end = np.where(
            np.isfinite(ends), tb.recording_seconds(end_ticks), np.nan
        )
        at = frame.columns.index("ended_s") + 1
        frame = frame.insert_column(
            at, pl.Series("recording_ended_s", _f(rec_end)).fill_nan(None)
        )
    return frame


# --------------------------------------------------------------------------- #
# session events
# --------------------------------------------------------------------------- #
def session_events_table(log: PulseLogFile, tb: TimeBase) -> pl.DataFrame:
    """Boot, clock anchors, radio link changes, dropped records, log gaps.

    Everything the device says about ITSELF rather than about the water. The
    source log packs all of this into one ``val`` column that means a file index
    on a boot row, a unix clock on an anchor, and a count on a drop -- three
    unrelated quantities sharing a name. Each gets its own column here, so a
    column always means one thing and an empty cell always means "not applicable
    to this kind of event".
    """
    wanted = {"BOOT", "ANCHOR", "LINK", "DROP", "GAP", "LOCON", "LOCOFF", "SHAM"}
    recs = [r for r in log.records if r.event in wanted]
    ticks = [r.tick for r in recs]
    known = np.array([t if t is not None else 0 for t in ticks], dtype=np.int64)

    def clock_iso(r) -> Optional[str]:
        if r.event != "ANCHOR" or r.val is None:
            return None
        return dt.datetime.fromtimestamp(r.val, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    frame = pl.DataFrame(
        {
            # A GAP row is written by loop(), which cannot read the ISR-owned
            # counter, so it genuinely has no tick. Empty, not zero -- zero is
            # BOOT's legitimate tick.
            "time_s": pl.Series(
                [None if t is None else round(t / tb.sample_rate_hz, 6) for t in ticks],
                dtype=pl.Float64,
            ),
            "event": [SESSION_EVENTS[r.event] for r in recs],
            "file_index": _opt_int(
                [r.val if r.event in ("BOOT", "GAP") else None for r in recs]
            ),
            "clock_unix": _opt_int(
                [r.val if r.event == "ANCHOR" else None for r in recs]
            ),
            "clock_time": [clock_iso(r) for r in recs],
            "records_lost": _opt_int(
                [r.val if r.event == "DROP" else None for r in recs]
            ),
            "radio_link_up": pl.Series(
                [None if r.event != "LINK" else bool(r.val) for r in recs],
                dtype=pl.Boolean,
            ),
            "trial_number": _opt_int([r.trial for r in recs]),
            "sample_tick": _opt_int(ticks),
            "source_row": pl.Series([r.seq for r in recs], dtype=pl.Int64),
        }
    )
    if tb.has_recording and frame.height:
        rec = np.where(
            np.array([t is not None for t in ticks]),
            tb.recording_seconds(known),
            np.nan,
        )
        frame = frame.insert_column(
            1, pl.Series("recording_time_s", _f(rec)).fill_nan(None)
        )
    return frame


# --------------------------------------------------------------------------- #
# controls
# --------------------------------------------------------------------------- #
def controls_table(log: PulseLogFile, tb: TimeBase) -> pl.DataFrame:
    """The operator's knobs over time -- ONE ROW PER CHANGE, not per pulse.

    The device stamps these on every row because a file cut off by a power loss
    must stay interpretable up to the tear. That is right for the device and
    wrong for a reader: in a real session the values change only when a hand
    moves, so ~2400 identical rows become a few dozen.

    Lossless: an as-of join on ``time_s`` reconstructs the per-pulse value
    exactly. The raw widths are kept beside the derived settings because they are
    the near end of the decode chain -- the 2026-08-22 supply-offset fault could
    only be diagnosed from them, and everything else here is derived through the
    very calibration that was under suspicion.
    """
    recs = [r for r in log.records if r.tick is not None]

    def state(r) -> tuple:
        return (r.master_m, r.rand_m, r.tick_ipi, r.ch_us, r.zero_us)

    keep, last = [], object()
    for r in recs:
        s = state(r)
        if s != last:
            keep.append(r)
            last = s
    if not keep:
        keep = []

    ticks = np.array([r.tick for r in keep], dtype=np.int64)
    ipi = [r.tick_ipi for r in keep]
    frame = pl.DataFrame(
        {
            "time_s": _f(tb.seconds(ticks)) if keep else np.array([], dtype=np.float64),
            "volley_amplitude": pl.Series(
                [_milli(r.master_m) for r in keep], dtype=pl.Float64
            ).round(4),
            "randomness": pl.Series(
                [_milli(r.rand_m) for r in keep], dtype=pl.Float64
            ).round(4),
            # A TICK TEMPO -- one over the median interval -- not an average pulse
            # rate. At randomness 1.0 a 5 Hz tick delivers about 3.3 pulses/s,
            # because the fitted rhythm is heavy-tailed.
            "tick_hz": pl.Series(
                [None if not v else round(tb.sample_rate_hz / v, 4) for v in ipi],
                dtype=pl.Float64,
            ),
            "tick_interval_s": pl.Series(
                [None if v is None else round(v / tb.sample_rate_hz, 6) for v in ipi],
                dtype=pl.Float64,
            ),
            **{
                name: _opt_int([
                    r.ch_us[i] if i < len(r.ch_us) else None for r in keep
                ])
                for i, name in enumerate(RADIO_CHANNELS)
            },
            "receiver_zero_us": _opt_int([r.zero_us for r in keep]),
            "sample_tick": pl.Series(ticks, dtype=pl.Int64),
            "source_row": pl.Series([r.seq for r in keep], dtype=pl.Int64),
        }
    )
    return _add_recording_time(frame, ticks, tb, "recording_time_s")
