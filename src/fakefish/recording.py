"""One recording, however many files the recorder split it into.

A TASCAM splits a long session at a size boundary -- exp3's hour came back as
four files, three of 931.97 s and a final 825.12 s. Alignment needs them as ONE
timeline, because ``recording_time_s`` is seconds from the first frame of the
first file and a viewer concatenates them the same way.

WHY THIS DOES NOT USE ``AudioLoader``'S OWN MULTI-FILE MODE, which is the obvious
thing to reach for. audioio decides whether consecutive files are one recording
by reading each file's start time from its metadata and checking that the next
one begins where the previous ended. The TASCAM's ``bext`` OriginationTime is
the time the file was **closed**, not opened. The two errors cancel while every
file has the same length, and stop cancelling at the last one -- which is always
short. So audioio accepts exactly the files before the final one and returns a
recording that is quietly missing its tail: on exp3 it reported 46.6 of 60.4
minutes, with no error at ``verbose=1`` and none in ``mode='relaxed'`` either.
Silently losing the last 13.7 minutes of a session is the worst possible failure
here, because everything it does report is correct.

So audioio is used as the per-file decoder -- it reads the 24-bit samples the
stdlib ``wave`` module cannot ("data type 'i3' not understood") and buffers
rather than loading whole files -- and the timeline is assembled here, where the
frame offsets are explicit and the file list is recorded in the metadata.
"""

from __future__ import annotations

import datetime as dt
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence

import numpy as np
from numpy.typing import NDArray

from fakefish.viz.loggers import get_logger

log = get_logger(__name__)

#: Consecutive files whose close times imply a gap larger than this are not one
#: continuous recording. ``bext`` timestamps have 1 s resolution, so anything
#: inside a second is quantisation rather than a real gap; 1.5 s leaves room for
#: rounding at both ends without accepting a genuine dropout.
MAX_JOIN_GAP_S = 1.5


@dataclass(frozen=True)
class Part:
    """One file of a recording, and where it sits on the joined timeline."""

    path: Path
    frames: int
    start_frame: int
    closed_at: Optional[dt.datetime]
    """When the recorder closed this file, from its ``bext`` chunk. ``None`` if
    the file carries no usable timestamp, which makes continuity unknowable
    rather than fine."""


def bext_closed_at(path: Path) -> Optional[dt.datetime]:
    """The BWF ``bext`` timestamp, which this recorder writes when it CLOSES.

    Verified on exp2: the file's ``TimeReference`` (its first frame, as samples
    since midnight) sat 259.0 s before this timestamp on a 256.6 s file. So it
    marks the end, not the start.
    """
    try:
        with path.open("rb") as fh:
            if fh.read(4) != b"RIFF":
                return None
            fh.read(8)
            while True:
                hdr = fh.read(8)
                if len(hdr) < 8:
                    return None
                cid, size = struct.unpack("<4sI", hdr)
                body = fh.read(size + (size & 1))
                if cid == b"bext":
                    date = body[320:330].decode("latin-1").strip("\x00 ")
                    time = body[330:338].decode("latin-1").strip("\x00 ")
                    return dt.datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, struct.error):
        return None


class Recording:
    """A read-only view of one or more WAV files as a single timeline.

    Open with :meth:`open`; use as a context manager. Frame 0 is the first frame
    of the first file, and ``duration_s`` covers every file given.
    """

    def __init__(self, parts: list[Part], loaders: list, rate: float, channels: int):
        self.parts = parts
        self._loaders = loaders
        self.rate = float(rate)
        self.channels = int(channels)
        self.frames = sum(p.frames for p in parts)

    @property
    def duration_s(self) -> float:
        return self.frames / self.rate

    @property
    def paths(self) -> list[Path]:
        return [p.path for p in self.parts]

    def __enter__(self) -> "Recording":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        for loader in self._loaders:
            try:
                loader.__exit__(None, None, None)
            except Exception:  # pragma: no cover - best effort
                pass
        self._loaders = []

    # ----- reading ---------------------------------------------------------
    def read(self, start: int, stop: int, channel: int) -> NDArray[np.float64]:
        """Frames ``[start, stop)`` of one channel, across file boundaries."""
        if not 0 <= channel < self.channels:
            raise ValueError(
                f"channel {channel} but the recording has {self.channels} (0-indexed)"
            )
        start = max(0, int(start))
        stop = min(self.frames, int(stop))
        if stop <= start:
            return np.empty(0, dtype=np.float64)

        out = np.empty(stop - start, dtype=np.float64)
        written = 0
        for part, loader in zip(self.parts, self._loaders, strict=True):
            p0, p1 = part.start_frame, part.start_frame + part.frames
            lo, hi = max(start, p0), min(stop, p1)
            if hi <= lo:
                continue
            chunk = np.asarray(loader[lo - p0 : hi - p0, channel], dtype=np.float64)
            out[written : written + chunk.size] = chunk
            written += chunk.size
        return out[:written]

    def blocks(
        self, channel: int, block_s: float = 60.0, overlap_s: float = 0.5
    ) -> Iterator[tuple[NDArray[np.float64], float]]:
        """Stream the channel in overlapping blocks of ``(samples, t0_seconds)``.

        Overlap exists so a pulse straddling a block edge is still seen whole by
        the matched filter; the caller drops duplicates from the overlap. Blocked
        rather than whole-file because an hour of 24-bit stereo at 48 kHz is
        ~1 GB, and as float64 for one channel it is still ~1.4 GB.
        """
        block = max(int(block_s * self.rate), 1)
        over = max(int(overlap_s * self.rate), 0)
        start = 0
        while start < self.frames:
            stop = min(start + block + over, self.frames)
            yield self.read(start, stop, channel), start / self.rate
            start += block

    # ----- construction ----------------------------------------------------
    @classmethod
    def open(cls, paths: Sequence[Path], *, buffer_s: float = 60.0) -> "Recording":
        """Open files IN THE ORDER GIVEN as one timeline.

        The order is the caller's to decide and is never inferred from the audio;
        for a split recording, sorting by filename is what the recorder's own
        numbering intends.
        """
        from audioio import AudioLoader

        if not paths:
            raise ValueError("no recording files given")

        parts: list[Part] = []
        loaders = []
        rate = channels = None
        frame = 0
        for path in paths:
            loader = AudioLoader(str(path), buffer_s, 10.0)
            loader.__enter__()
            if rate is None:
                rate, channels = loader.rate, loader.channels
            elif loader.rate != rate or loader.channels != channels:
                for other in loaders:
                    other.__exit__(None, None, None)
                loader.__exit__(None, None, None)
                raise ValueError(
                    f"{path.name} is {loader.channels} ch at {loader.rate:g} Hz but the "
                    f"recording started as {channels} ch at {rate:g} Hz -- these are not "
                    f"one recording"
                )
            loaders.append(loader)
            parts.append(
                Part(
                    path=Path(path),
                    frames=int(loader.frames),
                    start_frame=frame,
                    closed_at=bext_closed_at(Path(path)),
                )
            )
            frame += int(loader.frames)
        return cls(parts, loaders, rate, channels)

    # ----- continuity ------------------------------------------------------
    def join_gaps_s(self) -> list[Optional[float]]:
        """Seconds of recording missing at each file boundary, if knowable.

        Uses the close-time convention: two consecutive files are contiguous when
        their close times differ by exactly the LATER file's duration. ``None``
        where a file carries no usable timestamp.
        """
        gaps: list[Optional[float]] = []
        for a, b in zip(self.parts, self.parts[1:], strict=False):
            if a.closed_at is None or b.closed_at is None:
                gaps.append(None)
                continue
            gaps.append((b.closed_at - a.closed_at).total_seconds() - b.frames / self.rate)
        return gaps

    def continuity_problems(self) -> list[str]:
        """Boundaries that do not look like one continuous recording."""
        out = []
        for (a, b), gap in zip(
            zip(self.parts, self.parts[1:], strict=False), self.join_gaps_s(), strict=True
        ):
            if gap is None:
                out.append(f"{a.path.name} -> {b.path.name}: no timestamp, continuity unknown")
            elif abs(gap) > MAX_JOIN_GAP_S:
                out.append(f"{a.path.name} -> {b.path.name}: {gap:+.2f} s of recording missing")
        return out

    def describe(self) -> str:
        parts = ", ".join(f"{p.path.name} ({p.frames / self.rate:.1f} s)" for p in self.parts)
        return (
            f"{len(self.parts)} file(s), {self.channels} ch at {self.rate:g} Hz, "
            f"{self.duration_s:.1f} s total: {parts}"
        )
