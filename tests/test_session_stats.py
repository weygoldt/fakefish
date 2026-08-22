"""The derived session structure — and the one distinction it exists to preserve.

``LOCOFF`` is written for two unrelated reasons: a trial preempting the localization
train, and the operator releasing the throttle. Counting them together is not a rounding
error, it is a wrong answer — on the 2026-08-22 field log a naive count said the gate
cycled 27 times when 20 of those were trials and only 7 were the throttle, and the
difference was the whole diagnosis. Most of what is pinned here is that split.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from fakefish import pulse_log as pl
from fakefish import session_stats as ss

GOLDEN = Path(__file__).parent / "data" / "pulse_log_golden.csv"


@pytest.fixture
def golden():
    return pl.read(GOLDEN)


# ===== localization runs ====================================================
def test_runs_split_trial_preemption_from_the_throttle(golden):
    runs = ss.loc_runs(golden)
    assert runs, "the golden session must contain a localization run"
    assert all(r.end_s >= r.start_s for r in runs)
    assert all(r.ended_by in (ss.ENDED_BY_TRIAL, ss.ENDED_BY_GATE, ss.ENDED_BY_EOF) for r in runs)


def test_a_trial_preemption_is_not_a_gate_release():
    """A LOCOFF immediately followed by TRIAL is the protocol, not the operator."""
    text = _synth(
        [
            ("LOCON", 0),
            ("LOC", 10),
            ("LOCOFF", 20),
            ("TRIAL", 20),
            ("MARKER", 21),
        ]
    )
    log = pl.parse_text(text)
    runs = ss.loc_runs(log)
    assert len(runs) == 1
    assert runs[0].ended_by == ss.ENDED_BY_TRIAL
    assert ss.summarise(log).loc_runs_gate == 0


def test_a_bare_locoff_is_a_gate_release():
    text = _synth([("LOCON", 0), ("LOC", 10), ("LOCOFF", 20), ("ANCHOR", 30)])
    log = pl.parse_text(text)
    runs = ss.loc_runs(log)
    assert len(runs) == 1
    assert runs[0].ended_by == ss.ENDED_BY_GATE
    assert ss.summarise(log).loc_runs_gate == 1


def test_a_run_still_open_at_eof_is_reported():
    """A file that ends mid-run must not silently drop the run."""
    log = pl.parse_text(_synth([("LOCON", 0), ("LOC", 10), ("LOC", 20)]))
    runs = ss.loc_runs(log)
    assert len(runs) == 1
    assert runs[0].ended_by == ss.ENDED_BY_EOF
    assert runs[0].n_pulses == 2


# ===== intervals ============================================================
def test_intervals_do_not_straddle_a_run_boundary():
    """The gap between two runs is the operator, not the fish.

    Pooling it would put a spurious multi-second tail into every rhythm statistic — the
    exact quantity the fitted model is judged on.
    """
    log = pl.parse_text(
        _synth(
            [
                ("LOCON", 0),
                ("LOC", 0),
                ("LOC", 50_000),      # 1.0 s later
                ("LOCOFF", 60_000),
                ("LOCON", 5_000_000),  # 100 s of throttle-off
                ("LOC", 5_000_000),
                ("LOC", 5_050_000),   # 1.0 s later
            ]
        )
    )
    _, ipi = ss.loc_intervals(log)
    assert ipi.size == 2, "one interval per run, never the gap between them"
    assert np.allclose(ipi, 1.0)


# ===== the raw decode =======================================================
def test_summary_reports_throttle_reached_zero(golden):
    """v3 answers directly what once needed inverting the rate ladder."""
    s = ss.summarise(golden)
    assert s.zero_us == 905
    # The golden's throttle sits 210 us above its zero and never returns, so the
    # session honestly reports that the throttle was never seen at rest.
    assert s.throttle_reached_zero is False


def test_summary_admits_when_the_raw_decode_is_absent():
    """On a pre-v3 log the answer is None — unknown, never a confident False."""
    log = pl.parse_text(_synth([("LOCON", 0), ("LOC", 10)], version=2))
    s = ss.summarise(log)
    assert s.throttle_reached_zero is None
    assert s.zero_us is None
    assert ss.control_track(log).has_raw_decode is False


def test_control_track_tracks_the_raw_decode(golden):
    track = ss.control_track(golden)
    assert track.has_raw_decode
    above = track.throttle_above_zero_us
    assert above is not None
    finite = above[np.isfinite(above)]
    assert finite.size and np.allclose(finite, 1115 - 905)


# ===== helpers ==============================================================
def _synth(events, *, version: int = 3) -> str:
    """Build a minimal in-memory log. Keeps these tests independent of the golden."""
    cols = pl.COLUMNS_BY_VERSION[version]
    head = [
        "#fakefish-pulse-log",
        f"#format_version={version}",
        "#sample_rate_hz=50000",
        "#file_index=1",
        "#" + ",".join(cols),
        ",".join(cols),
    ]
    rows = []
    for seq, (event, tick) in enumerate(events):
        cells = [str(seq), str(tick), event] + [""] * (len(cols) - 3)
        cells[10] = "10000"  # tick_ipi
        cells[9] = "1000"    # rand_m
        cells[8] = "900"     # master_m
        if version >= 3:
            cells[14] = "1115"
            cells[18] = "905"
        rows.append(",".join(cells))
    return "\n".join(head + rows) + "\n"
