"""Convert a device pulse log into readable tables plus a metadata sidecar.

    fakefish-ingest PULS0002.CSV --out-dir exp2/

writes ``<id>_pulses.csv``, ``<id>_trials.csv``, ``<id>_session_events.csv``,
``<id>_controls.csv`` and ``<id>_metadata.toml``. The source log is never
touched: it is the device's record, and everything here is derived and
regenerable from it.

Use ``fakefish-align-log`` instead when there is a recording to align against --
it writes the same tables with recording-clock columns added, so a viewer reads
one set of files rather than joining two.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Optional

import polars as pl
import typer

from fakefish import session_metadata as meta
from fakefish import session_tables as tables
from fakefish.pulse_log import PulseLogFile, read
from fakefish.viz.loggers import configure_logging, get_logger

log = get_logger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)

#: File name suffixes, so a directory holding several sessions stays sorted by
#: session rather than interleaved by kind.
SUFFIXES = {
    "pulses": "_pulses.csv",
    "trials": "_trials.csv",
    "session_events": "_session_events.csv",
    "controls": "_controls.csv",
    "metadata": "_metadata.toml",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def destinations(out_dir: Path, session_id: str) -> dict[str, Path]:
    return {k: out_dir / f"{session_id}{s}" for k, s in SUFFIXES.items()}


def check_free(paths: dict[str, Path], force: bool) -> None:
    """Refuse to clobber, and check EVERY destination before doing any work.

    A half-written set is worse than none: the metadata would describe tables
    that were never replaced, and nothing on disk would say so.
    """
    if force:
        return
    taken = [p for p in paths.values() if p.exists()]
    if taken:
        raise typer.BadParameter(
            "refusing to overwrite: "
            + ", ".join(str(p) for p in taken)
            + "  (pass --force to replace)"
        )


def write_tables(
    log_file: PulseLogFile,
    tb: tables.TimeBase,
    paths: dict[str, Path],
    *,
    item_durations_s: Optional[dict[int, float]] = None,
    pulse_match: Optional[pl.DataFrame] = None,
) -> dict[str, int]:
    """Write the four CSVs. Returns the row count of each.

    ``pulse_match`` carries the per-pulse alignment outcome when there is a
    recording. It is joined on ``source_row`` rather than by position, so a
    change in either builder cannot silently shift one column against another.
    """
    pulses = tables.pulses_table(log_file, tb)
    if pulse_match is not None and pulses.height:
        pulses = pulses.join(pulse_match, on="source_row", how="left")
    frames = {
        "pulses": pulses,
        "trials": tables.trials_table(log_file, tb, item_durations_s),
        "session_events": tables.session_events_table(log_file, tb),
        "controls": tables.controls_table(log_file, tb),
    }
    counts = {}
    for name, frame in frames.items():
        frame.write_csv(paths[name])
        counts[name] = frame.height
    return counts


@app.command()
def run(
    log_path: Annotated[Path, typer.Argument(help="A PULSnnnn.CSV device pulse log.")],
    out_dir: Annotated[
        Path, typer.Option("--out-dir", "-o", help="Directory to write the tables into.")
    ],
    session_id: Annotated[
        Optional[str],
        typer.Option("--session-id", help="Name prefix. Defaults to the log's stem."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Replace existing output files.")
    ] = False,
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 2,
) -> None:
    """Convert one device log into readable tables plus metadata."""
    configure_logging(verbose)
    log_file = read(log_path)

    # The promise is that nothing is lost. A header key nobody mapped would be
    # dropped silently, so it is an error rather than a warning.
    missing = meta.unmapped_keys(log_file)
    if missing:
        raise typer.BadParameter(
            f"{log_path.name} carries header keys this version does not know, and "
            f"converting would drop them: {sorted(missing)}. Add them to "
            f"session_metadata.SECTIONS."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    sid = session_id or log_path.stem
    paths = destinations(out_dir, sid)
    check_free(paths, force)

    tb = tables.TimeBase(sample_rate_hz=float(log_file.sample_rate_hz))
    durations = _item_durations()
    counts = write_tables(log_file, tb, paths, item_durations_s=durations)

    doc = meta.build(
        log_file,
        source_path=log_path,
        source_sha256=sha256(log_path),
        tool="fakefish-ingest",
    )
    doc["counts"].update({f"rows_{k}": v for k, v in counts.items()})
    meta.write(paths["metadata"], doc)

    log.info("%s -> %s", log_path.name, out_dir)
    for name in ("pulses", "trials", "session_events", "controls"):
        log.info("  %-16s %6d rows  %s", name, counts[name], paths[name].name)
    log.info("  %-16s %6s       %s", "metadata", "", paths["metadata"].name)


def _item_durations() -> Optional[dict[int, float]]:
    """Item durations, so a trial gets an end time.

    Optional on purpose: the durations come from the committed stimulus library,
    and a log recorded against a different library would give wrong ends. If the
    library cannot be read the trials table simply has no ``ended_s``, which is
    honest, rather than an invented one.
    """
    try:
        from fakefish.align_log import item_durations_s

        return item_durations_s()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("no stimulus library available, so trials get no end time (%s)", exc)
        return None


def read_tables(out_dir: Path, session_id: str) -> dict[str, pl.DataFrame]:
    """Read a converted session back. Convenience for tests and notebooks."""
    paths = destinations(out_dir, session_id)
    return {
        name: pl.read_csv(paths[name], infer_schema_length=None)
        for name in ("pulses", "trials", "session_events", "controls")
    }


if __name__ == "__main__":  # pragma: no cover
    app()
