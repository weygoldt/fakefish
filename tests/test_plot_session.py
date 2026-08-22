"""The session timeline figure builds — on a v3 log and on a v2 one.

The v2 case is the one worth a test: the raw-decode panel simply has no data there, and
the figure must render without it rather than raising. Field logs from before 2026-08-22
are not reproducible, so a reader that cannot open them is a reader that loses them.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

from fakefish import plot_session, pulse_log as pl  # noqa: E402

GOLDEN = Path(__file__).parent / "data" / "pulse_log_golden.csv"


def _synth(events, *, version: int = 3) -> str:
    """A minimal in-memory log. Local on purpose: a figure test should not import
    another test module, or a rename over there breaks the figures' coverage here."""
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
        cells[8], cells[9], cells[10] = "900", "1000", "10000"
        if version >= 3:
            cells[14], cells[18] = "1115", "905"
        rows.append(",".join(cells))
    return "\n".join(head + rows) + "\n"


def test_timeline_builds_from_the_golden():
    fig = plot_session.build_timeline(pl.read(GOLDEN), title="golden")
    try:
        assert len(fig.axes) >= 4, "the timeline has four stacked panels"
    finally:
        matplotlib.pyplot.close(fig)


def test_timeline_builds_without_the_raw_decode():
    """A v2 log has no widths; the controls panel must drop that trace, not fail."""
    log = pl.parse_text(_synth([("LOCON", 0), ("LOC", 10), ("LOC", 50_000)], version=2))
    fig = plot_session.build_timeline(log)
    try:
        # No twin axis is created when there is nothing to put on it.
        assert len(fig.axes) == 4
    finally:
        matplotlib.pyplot.close(fig)


def test_timeline_adds_the_raw_decode_axis_when_present():
    log = pl.parse_text(_synth([("LOCON", 0), ("LOC", 10), ("LOC", 50_000)], version=3))
    fig = plot_session.build_timeline(log)
    try:
        assert len(fig.axes) == 5, "the throttle-above-zero trace gets its own axis"
    finally:
        matplotlib.pyplot.close(fig)


def test_timeline_writes_a_file(tmp_path):
    from fakefish.viz.figsave import save_figure

    fig = plot_session.build_timeline(pl.read(GOLDEN))
    out = save_figure(fig, tmp_path / "session")
    assert out.is_file() and out.stat().st_size > 0
