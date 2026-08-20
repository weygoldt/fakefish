"""Reader for the RC device's per-pulse SD event log (``/LOGS/PULSnnnn.CSV``).

The RC stimulator writes one row for **every pulse it emits** — localization,
marker and volley alike — stamped with the exact 50 kHz sample tick that placed
it. This module is the consumer side of that contract: it parses the header
block, the rows, and the integrity records, and reconstructs absolute time from
the periodic RTC anchors.

Why the log exists. Volley/sham *trials* are identifiable in a recording (the
count-coded pulse marker tags them), but the **localization train is not** — it
is deliberately built to look exactly like a real cruising eel. Without the log,
any analysis of a recording made during playback must treat an unknown subset of
pulses as possibly ours. The log is also the on-device ground truth for the
blinded trigger, whose outcome the firmware draws in the ISR.

The writer is ``firmware/eel_core/pulse_log.h``. Both sides are pinned to one
committed artifact, ``tests/data/pulse_log_golden.csv``, which the firmware's
host self-test regenerates and ``check.sh`` diffs — so the C writer and this
reader cannot drift apart silently.

Two conventions matter when reading rows:

* **An empty column means "not applicable", never zero.** In particular an empty
  ``item`` means the pulse came from no library item at all (localization
  synthesises directly from ``EOD_HV``; the marker is built at runtime). It is
  parsed as ``None`` and never as ``0`` or ``-1`` — ``STIM_ITEMS[0]`` is a real
  recorded volley and ``STIM_ITEMS[-1]`` silently wraps in Python, so either
  substitution would quietly misattribute the bulk of a log.
* **Settings are integers.** Amplitudes and jitter are milli-units (``x1000``)
  and the rate is a mean inter-pulse interval in whole samples; the firmware
  formats no floats. :attr:`PulseRecord.amp` and friends expose the divided
  values.

Examples::

    fakefish-pulse-log info /LOGS/PULS0007.CSV
    fakefish-pulse-log pulses /LOGS/PULS0007.CSV --kind LOC
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
import typer

from fakefish.viz.loggers import configure_logging, get_logger

log = get_logger(__name__)
app = typer.Typer(
    add_completion=False,
    help="Read the RC device's per-pulse SD event log.",
)

#: Format versions this reader understands. The firmware bumps
#: ``PULSELOG_FORMAT_VERSION`` only for a breaking schema change, so refusing an
#: unknown one is better than silently misreading a future file.
SUPPORTED_FORMAT_VERSIONS = frozenset({1})

#: The magic first line every log starts with.
MAGIC = "#fakefish-pulse-log"

#: Mirrors ``PLOG_RTC_MIN_VALID`` in ``firmware/eel_core/pulse_log.h``. An RTC reading
#: below this is *not set* — no coin cell, or never programmed — and the Teensy's clock
#: free-runs from a small value after a power cycle. ``ANCHOR`` rows carry the **raw**
#: reading, so the reader must apply the same floor the writer uses for its ``rtc_valid``
#: header key; filtering on merely ``> 0`` would feed a free-running counter into the
#: wall-clock fit and produce confident 1970 timestamps.
RTC_MIN_VALID = 1_600_000_000

#: Column order, mirroring ``PLOG_COLUMNS`` in ``pulse_log.h``.
COLUMNS = (
    "seq",
    "tick",
    "event",
    "item",
    "pulse",
    "trial",
    "pol",
    "amp_m",
    "master_m",
    "cv_m",
    "rate_ipi",
    "val",
    "req",
    "res",
)

#: Events that represent one emitted pulse (one row each, never summarised).
PULSE_EVENTS = frozenset({"LOC", "MARKER", "VOLLEY"})

#: Trial-kind codes. ``R`` means the RC lever asked for a *blinded* trial and the
#: firmware drew the outcome; ``V``/``S`` mean an explicit bench request.
KIND_RANDOM = "R"
KIND_VOLLEY = "V"
KIND_SHAM = "S"


class PulseLogError(ValueError):
    """Raised when a file is not a readable pulse log."""


def _opt_int(raw: str) -> Optional[int]:
    """Parse a column that may be empty.

    Empty means *not applicable* and becomes ``None`` — never ``0``, which for
    ``item`` would be a real volley, and never ``-1``, which Python would
    happily use to index the last item.
    """
    raw = raw.strip()
    return int(raw) if raw else None


def _opt_str(raw: str) -> Optional[str]:
    raw = raw.strip()
    return raw or None


def _milli(value: Optional[int]) -> Optional[float]:
    return None if value is None else value / 1000.0


@dataclass(frozen=True)
class PulseRecord:
    """One row of the log."""

    seq: int
    tick: Optional[int]
    """Sample tick. ``None`` only on a ``GAP`` row, which ``loop()`` writes and which
    therefore has no reading of the ISR-owned counter — never a stand-in ``0``, which
    would be indistinguishable from ``BOOT``'s legitimate tick 0."""

    event: str
    item: Optional[int]
    pulse: Optional[int]
    trial: Optional[int]
    pol: Optional[int]
    amp_m: Optional[int]
    master_m: Optional[int]
    cv_m: Optional[int]
    rate_ipi: Optional[int]
    val: Optional[int]
    req: Optional[str]
    res: Optional[str]

    @property
    def is_pulse(self) -> bool:
        """True if this row is one emitted pulse."""
        return self.event in PULSE_EVENTS

    @property
    def amp(self) -> Optional[float]:
        """Amplitude applied to *this* pulse, as a fraction of full scale."""
        return _milli(self.amp_m)

    @property
    def master_amp(self) -> Optional[float]:
        """The master (volley) amplitude setting in force at this row."""
        return _milli(self.master_m)

    @property
    def cv(self) -> Optional[float]:
        """Localization jitter (coefficient of variation) in force at this row."""
        return _milli(self.cv_m)

    @property
    def blinded(self) -> Optional[bool]:
        """For a ``TRIAL`` row: was the outcome drawn by the firmware?

        ``True`` when the RC lever requested a random trial, ``False`` when a
        panel button forced an explicit kind (a bench test, which must not be
        pooled with blinded trials). ``None`` on any other row.
        """
        if self.event != "TRIAL" or self.req is None:
            return None
        return self.req == KIND_RANDOM


@dataclass(frozen=True)
class Integrity:
    """What the log admits about its own completeness.

    A log that quietly omits pulses turns into wrong analysis; one that admits a
    gap turns into a caveat. These are the admissions.
    """

    dropped_records: int
    """Records the firmware lost to a full ring (sum of every ``DROP`` row)."""

    drop_events: int
    """How many separate times the ring overflowed."""

    gaps: tuple[int, ...]
    """File indices that were cut short by a card failure (``GAP`` rows)."""

    seq_breaks: tuple[int, ...]
    """Row indices where ``seq`` was not the previous ``seq`` + 1.

    Non-empty means the file was concatenated, edited, or torn — the firmware
    always writes ``seq`` contiguously within one file.
    """

    truncated: bool
    """True if the final line was incomplete, i.e. cut by a power loss."""

    @property
    def clean(self) -> bool:
        """True when nothing was lost and nothing looks torn."""
        return (
            self.dropped_records == 0
            and not self.gaps
            and not self.seq_breaks
            and not self.truncated
        )


@dataclass
class PulseLogFile:
    """A parsed pulse log."""

    path: Optional[Path]
    header: dict[str, str]
    records: list[PulseRecord]
    integrity: Integrity

    # ----- header conveniences ---------------------------------------------
    def header_int(self, key: str, default: Optional[int] = None) -> Optional[int]:
        """Return a header value as an int, or ``default`` if absent."""
        raw = self.header.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    @property
    def format_version(self) -> int:
        return self.header_int("format_version", 0) or 0

    @property
    def sample_rate_hz(self) -> int:
        rate = self.header_int("sample_rate_hz")
        if not rate:
            raise PulseLogError("log header has no usable sample_rate_hz")
        return rate

    @property
    def file_index(self) -> Optional[int]:
        return self.header_int("file_index")

    @property
    def rtc_valid(self) -> bool:
        """Was the RTC set when this file was opened?

        The design is RTC-optional: with no coin cell you lose absolute time and
        keep exact relative timing. This says which you have.
        """
        return bool(self.header_int("rtc_valid", 0))

    # ----- row views --------------------------------------------------------
    def events(self, *names: str) -> list[PulseRecord]:
        """Rows whose ``event`` is one of ``names``."""
        wanted = frozenset(names)
        return [r for r in self.records if r.event in wanted]

    def pulses(self, kind: Optional[str] = None) -> list[PulseRecord]:
        """Every emitted pulse, optionally restricted to one event kind."""
        if kind is None:
            return [r for r in self.records if r.is_pulse]
        if kind not in PULSE_EVENTS:
            raise PulseLogError(f"{kind!r} is not a pulse event {sorted(PULSE_EVENTS)}")
        return [r for r in self.records if r.event == kind]

    def pulse_ticks(self, kind: Optional[str] = None) -> np.ndarray:
        """Sample ticks of the emitted pulses, as int64.

        Pulse rows always carry a tick — they are written by the ISR that placed the
        pulse — so a missing one means a corrupt file rather than an absent field.
        """
        ticks = [r.tick for r in self.pulses(kind)]
        if any(t is None for t in ticks):
            raise PulseLogError("a pulse row is missing its tick; the file is corrupt")
        return np.array(ticks, dtype=np.int64)

    def pulse_times_s(self, kind: Optional[str] = None) -> np.ndarray:
        """Emitted pulse times in seconds since the sample clock started."""
        return self.pulse_ticks(kind) / float(self.sample_rate_hz)

    def trials(self) -> list[PulseRecord]:
        """The ``TRIAL`` rows, in order."""
        return self.events("TRIAL")

    def volley_items(self) -> dict[int, int]:
        """Map trial id -> the library item index that trial played.

        Only volley trials appear: a sham plays no item at all. This index is
        recoverable from nowhere else — not from the marker, not from the
        settings — so it is the record that makes a volley trial reproducible.
        """
        out: dict[int, int] = {}
        for r in self.pulses("VOLLEY"):
            if r.trial is not None and r.item is not None:
                out[r.trial] = r.item
        return out

    # ----- absolute time ----------------------------------------------------
    def anchors(self) -> list[tuple[int, int]]:
        """``(tick, rtc_unix)`` pairs from the ``ANCHOR`` rows with a **set** RTC.

        Rows written while the RTC was unset are skipped: they still prove
        liveness, but they carry no wall-clock information. "Unset" uses the same
        floor the firmware uses (:data:`RTC_MIN_VALID`), not merely ``> 0`` — a
        Teensy with no coin cell free-runs upward from a small value after every
        power cycle, so a nonzero reading is not evidence of a real clock.
        """
        return [
            (r.tick, r.val)
            for r in self.events("ANCHOR")
            if r.tick is not None and r.val is not None and r.val >= RTC_MIN_VALID
        ]

    def absolute_time(self, ticks: np.ndarray) -> np.ndarray:
        """Interpolate ticks to absolute Unix seconds using the RTC anchors.

        The RTC has 1 s resolution while the tick has 20 us, so a straight
        least-squares fit through many anchors recovers wall-clock time far more
        precisely than any single RTC reading. Requires at least two usable
        anchors; with one or none, absolute time is simply unavailable and this
        raises rather than inventing a value.
        """
        pairs = self.anchors()
        if len(pairs) < 2:
            raise PulseLogError(
                f"need >=2 anchors with a valid RTC to place the log in absolute "
                f"time, found {len(pairs)} (was the coin cell fitted?)"
            )
        at = np.array([p[0] for p in pairs], dtype=np.float64)
        rt = np.array([p[1] for p in pairs], dtype=np.float64)
        # Two or more anchors can still carry no slope: all at one tick makes the fit
        # rank-deficient, all at one RTC second collapses every tick onto one instant.
        # Both would return a confident, meaningless mapping — raise instead, which is
        # what this docstring promises.
        if np.ptp(at) <= 0 or np.ptp(rt) <= 0:
            raise PulseLogError(
                f"anchors span no time ({len(pairs)} of them, tick span {np.ptp(at):g}, "
                f"RTC span {np.ptp(rt):g}); absolute time is not recoverable"
            )
        slope, intercept = np.polyfit(at, rt, 1)
        return np.asarray(ticks, dtype=np.float64) * slope + intercept


def _parse_header(lines: list[str]) -> tuple[dict[str, str], int]:
    """Parse the leading ``#`` block. Returns the keys and the first data row index."""
    if not lines:
        raise PulseLogError("not a fakefish pulse log: the file is empty")
    if lines[0].strip() != MAGIC:
        raise PulseLogError(
            f"not a fakefish pulse log: expected first line {MAGIC!r}, "
            f"got {lines[0].strip()!r}"
        )
    header: dict[str, str] = {}
    idx = 1
    while idx < len(lines) and lines[idx].startswith("#"):
        body = lines[idx][1:]
        if "=" in body:
            key, _, value = body.partition("=")
            header[key.strip()] = value.strip()
        # A '#' line without '=' is the commented column header; skipped here and
        # re-checked against COLUMNS once the bare header row is reached.
        idx += 1
    return header, idx


def parse_text(text: str, path: Optional[Path] = None) -> PulseLogFile:
    """Parse a pulse log from text. See :func:`read` for the file form."""
    lines = text.splitlines()
    header, idx = _parse_header(lines)

    version = header.get("format_version")
    try:
        version_i = int(version) if version is not None else -1
    except ValueError:
        version_i = -1
    if version_i not in SUPPORTED_FORMAT_VERSIONS:
        raise PulseLogError(
            f"unsupported pulse-log format_version {version!r}; this reader "
            f"understands {sorted(SUPPORTED_FORMAT_VERSIONS)}"
        )

    if idx >= len(lines):
        raise PulseLogError("log has a header but no column row")
    columns = tuple(c.strip() for c in lines[idx].split(","))
    if columns != COLUMNS:
        raise PulseLogError(
            f"unexpected column row: {columns!r}\nexpected: {COLUMNS!r}"
        )
    idx += 1

    # A power cut truncates the final line. Detect it rather than silently
    # dropping a malformed row: an admitted gap is a caveat, a silent one is a bug.
    truncated = bool(text) and not text.endswith("\n")

    records: list[PulseRecord] = []
    seq_breaks: list[int] = []
    dropped = 0
    drop_events = 0
    gaps: list[int] = []

    # A power cut can cut the final row ANYWHERE, including inside its last column — in which
    # case it still has 14 fields and would parse as a complete record with `res` silently
    # reading as absent, booking a volley trial as a sham. So the last row of a truncated file
    # is dropped on field count alone, never trusted.
    data_lines = lines[idx:]
    if truncated and data_lines:
        data_lines = data_lines[:-1]

    reader = csv.reader(data_lines)
    for row_i, row in enumerate(reader):
        if not row or (len(row) == 1 and not row[0].strip()):
            continue
        if len(row) != len(COLUMNS):
            raise PulseLogError(
                f"row {row_i} has {len(row)} columns, expected {len(COLUMNS)}: {row!r}"
            )
        try:
            rec = PulseRecord(
                seq=int(row[0]),
                tick=_opt_int(row[1]),
                event=row[2].strip(),
                item=_opt_int(row[3]),
                pulse=_opt_int(row[4]),
                trial=_opt_int(row[5]),
                pol=_opt_int(row[6]),
                amp_m=_opt_int(row[7]),
                master_m=_opt_int(row[8]),
                cv_m=_opt_int(row[9]),
                rate_ipi=_opt_int(row[10]),
                val=_opt_int(row[11]),
                req=_opt_str(row[12]),
                res=_opt_str(row[13]),
            )
        except ValueError as exc:
            # Card corruption is exactly what the Integrity machinery exists for, so it must
            # surface through the documented error type with the offending row named — not as
            # a bare ValueError that `except PulseLogError` would not catch.
            raise PulseLogError(f"row {row_i}: {exc}: {row!r}") from exc
        if records and rec.seq != records[-1].seq + 1:
            seq_breaks.append(len(records))
        if rec.event == "DROP":
            drop_events += 1
            dropped += rec.val or 0
        elif rec.event == "GAP" and rec.val is not None:
            gaps.append(rec.val)
        records.append(rec)

    integrity = Integrity(
        dropped_records=dropped,
        drop_events=drop_events,
        gaps=tuple(gaps),
        seq_breaks=tuple(seq_breaks),
        truncated=truncated,
    )
    return PulseLogFile(path=path, header=header, records=records, integrity=integrity)


def read(path: Path) -> PulseLogFile:
    """Read and parse one ``PULSnnnn.CSV`` pulse log."""
    path = Path(path)
    return parse_text(path.read_text(), path=path)


def iter_logs(directory: Path) -> Iterator[Path]:
    """Yield the ``PULSnnnn.CSV`` files in ``directory``, in index order.

    Index order is session order by construction: the firmware always opens
    highest-existing + 1 and never reuses a gap.
    """
    directory = Path(directory)
    found: list[tuple[int, Path]] = []
    for p in directory.iterdir():
        if not p.is_file():
            continue
        name = p.name.upper()
        if len(name) == 12 and name.startswith("PULS") and name.endswith(".CSV"):
            digits = name[4:8]
            if digits.isdigit():
                found.append((int(digits), p))
    for _, p in sorted(found):
        yield p


# ===== CLI =================================================================
@app.command()
def info(
    path: Path = typer.Argument(..., help="A PULSnnnn.CSV pulse log."),
    verbose: int = typer.Option(2, "--verbose", "-v", count=True),
) -> None:
    """Summarise one pulse log: provenance, counts, trials and integrity."""
    configure_logging(verbose)
    lg = read(path)

    log.info("file: %s", lg.path)
    log.info("format v%d, %d Hz sample clock", lg.format_version, lg.sample_rate_hz)
    log.info("file index: %s, RTC %s", lg.file_index, "set" if lg.rtc_valid else "NOT SET")
    build = lg.header.get("build")
    if build:
        log.info("firmware build: %s", build)
    log.info(
        "library: stim format v%s, %s items, EOD %s samples (net integral x1000 = %s)",
        lg.header.get("stim_format_version"),
        lg.header.get("n_stim_items"),
        lg.header.get("eod_hv_len"),
        lg.header.get("eod_net_integral_x1000"),
    )

    pulses = lg.pulses()
    log.info("%d rows, %d of them emitted pulses:", len(lg.records), len(pulses))
    for kind in sorted(PULSE_EVENTS):
        log.info("  %-6s %d", kind, len(lg.pulses(kind)))

    if pulses:
        ticks = lg.pulse_ticks()
        span = float(ticks[-1] - ticks[0]) / float(lg.sample_rate_hz)
        log.info("first->last pulse: %.3f s of device time", span)

    trials = lg.trials()
    blinded = sum(1 for t in trials if t.blinded)
    volleys = sum(1 for t in trials if t.res == KIND_VOLLEY)
    log.info(
        "%d trials (%d blinded, %d bench-forced): %d volley, %d sham",
        len(trials),
        blinded,
        len(trials) - blinded,
        volleys,
        len(trials) - volleys,
    )
    items = lg.volley_items()
    if items:
        log.info("volley items played: %s", sorted(items.values()))

    anchors = lg.anchors()
    log.info(
        "%d of %d RTC anchors carry a set clock", len(anchors), len(lg.events("ANCHOR"))
    )
    if len(anchors) >= 2 and pulses:
        t0 = lg.absolute_time(np.array([lg.pulse_ticks()[0]]))[0]
        log.info("first pulse at unix %.1f", t0)
    else:
        log.info("absolute time unavailable — relative timing is exact regardless")

    it = lg.integrity
    if it.clean:
        log.info("integrity: clean (no dropped records, no gaps, no torn rows)")
    else:
        log.warning(
            "integrity: %d record(s) lost in %d ring overflow(s); gaps from file(s) %s; "
            "%d seq break(s); %struncated",
            it.dropped_records,
            it.drop_events,
            list(it.gaps) or "none",
            len(it.seq_breaks),
            "" if it.truncated else "not ",
        )


@app.command()
def pulses(
    path: Path = typer.Argument(..., help="A PULSnnnn.CSV pulse log."),
    kind: Optional[str] = typer.Option(
        None, "--kind", "-k", help="LOC, MARKER or VOLLEY; default all."
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Rows to print (0 = all)."),
    verbose: int = typer.Option(2, "--verbose", "-v", count=True),
) -> None:
    """Print emitted pulses with their tick, time and attribution."""
    configure_logging(verbose)
    lg = read(path)
    rows = lg.pulses(kind)
    shown = rows if limit <= 0 else rows[:limit]
    rate = float(lg.sample_rate_hz)
    log.info("%d pulse(s)%s; showing %d", len(rows), f" of kind {kind}" if kind else "", len(shown))
    for r in shown:
        log.info(
            "  t=%12.6f s  tick=%-12d %-6s item=%-4s pulse=%-4s trial=%-4s pol=%-3s amp=%s",
            (r.tick or 0) / rate,
            r.tick if r.tick is not None else -1,
            r.event,
            "-" if r.item is None else r.item,
            "-" if r.pulse is None else r.pulse,
            "-" if r.trial is None else r.trial,
            "-" if r.pol is None else r.pol,
            "-" if r.amp is None else f"{r.amp:.3f}",
        )


if __name__ == "__main__":  # pragma: no cover
    app()
