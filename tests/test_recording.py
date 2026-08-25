"""Tests for joining a split recording into one timeline.

The failure this guards against is silent: a recorder splits a session, one file
is quietly left out, and every number downstream is computed on a recording that
is shorter than the one that was made.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from fakefish.recording import Recording, bext_closed_at


def _write_wav(path: Path, x: np.ndarray, rate: int = 48_000, channels: int = 1) -> Path:
    """A plain 16-bit WAV. No bext chunk, so continuity is unknowable from it."""
    data = np.clip(np.rint(np.asarray(x) * 32767.0), -32768, 32767).astype("<i2")
    if channels > 1:
        data = np.repeat(data[:, None], channels, axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return path


def _ramp_parts(tmp_path: Path, lengths: list[int], rate: int = 48_000) -> list[Path]:
    """Files whose samples are a continuous ramp, so a join is checkable by value."""
    paths, start = [], 0
    for i, n in enumerate(lengths):
        x = (np.arange(start, start + n, dtype=np.float64) % 10_000) / 20_000.0
        paths.append(_write_wav(tmp_path / f"part{i:02d}.wav", x, rate))
        start += n
    return paths


def test_joins_every_file_including_a_short_last_one(tmp_path: Path) -> None:
    """The last file of a split recording is always short. It must not be dropped.

    audioio's own multi-file mode decides continuity from each file's metadata
    start time, but this recorder stamps the CLOSE time; the two errors cancel
    while files are equal length and stop cancelling at the short final one, so
    it silently returns the recording minus its tail. On exp3 that lost 13.7 of
    60.4 minutes with no error raised.
    """
    lengths = [48_000, 48_000, 48_000, 12_345]
    paths = _ramp_parts(tmp_path, lengths)
    with Recording.open(paths) as rec:
        assert len(rec.parts) == 4, "every file given must appear"
        assert rec.frames == sum(lengths)
        assert rec.duration_s == pytest.approx(sum(lengths) / 48_000)
        assert [p.frames for p in rec.parts] == lengths
        assert [p.start_frame for p in rec.parts] == [0, 48_000, 96_000, 144_000]


def test_reads_across_a_file_boundary(tmp_path: Path) -> None:
    """A read spanning a join must return one contiguous stretch of samples."""
    paths = _ramp_parts(tmp_path, [1000, 1000, 500])
    with Recording.open(paths) as rec:
        whole = rec.read(0, rec.frames, 0)
        assert whole.size == 2500
        # Across the first join, the samples must continue the ramp.
        across = rec.read(990, 1010, 0)
        assert across.size == 20
        assert np.allclose(across, whole[990:1010], atol=1e-4)
        # And the joined stream must equal the concatenation of its parts.
        parts = [rec.read(p.start_frame, p.start_frame + p.frames, 0) for p in rec.parts]
        assert np.allclose(np.concatenate(parts), whole, atol=1e-9)


def test_reads_are_clamped_not_wrapped(tmp_path: Path) -> None:
    paths = _ramp_parts(tmp_path, [1000, 500])
    with Recording.open(paths) as rec:
        assert rec.read(-50, 10, 0).size == 10
        assert rec.read(1400, 99_999, 0).size == 100
        assert rec.read(500, 500, 0).size == 0
        assert rec.read(900, 100, 0).size == 0


def test_blocks_cover_everything_and_overlap(tmp_path: Path) -> None:
    """Every sample must appear in some block, and blocks must overlap.

    Overlap is what stops a pulse landing on a boundary from being cut in half by
    the matched filter and missed.
    """
    paths = _ramp_parts(tmp_path, [48_000 * 3])
    with Recording.open(paths) as rec:
        blocks = list(rec.blocks(0, block_s=1.0, overlap_s=0.1))
        assert len(blocks) == 3
        starts = [t0 for _x, t0 in blocks]
        assert starts == pytest.approx([0.0, 1.0, 2.0])
        # Every block but the last carries its overlap.
        assert blocks[0][0].size == 48_000 + 4_800
        assert blocks[-1][0].size == 48_000, "the last block stops at the end"
        covered = sum(x.size for x, _ in blocks)
        assert covered > rec.frames, "blocks overlap, so they over-cover"


def test_rejects_files_that_are_not_one_recording(tmp_path: Path) -> None:
    a = _write_wav(tmp_path / "a.wav", np.zeros(1000), rate=48_000, channels=1)
    b = _write_wav(tmp_path / "b.wav", np.zeros(1000), rate=44_100, channels=1)
    c = _write_wav(tmp_path / "c.wav", np.zeros(1000), rate=48_000, channels=2)
    with pytest.raises(ValueError, match="not"):
        Recording.open([a, b])
    with pytest.raises(ValueError, match="not"):
        Recording.open([a, c])


def test_rejects_a_channel_that_does_not_exist(tmp_path: Path) -> None:
    paths = _ramp_parts(tmp_path, [1000])
    with Recording.open(paths) as rec:
        with pytest.raises(ValueError, match="channel"):
            rec.read(0, 100, 5)


def test_no_files_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Recording.open([])


# ===== continuity ==========================================================
def _add_bext(path: Path, when: str) -> None:
    """Append a minimal bext chunk carrying an origination date and time."""
    body = bytearray(602)
    body[320:330] = when[:10].encode("latin-1")
    body[330:338] = when[11:19].encode("latin-1")
    raw = bytearray(path.read_bytes())
    chunk = b"bext" + struct.pack("<I", len(body)) + bytes(body)
    raw.extend(chunk)
    struct.pack_into("<I", raw, 4, len(raw) - 8)
    path.write_bytes(bytes(raw))


def test_continuity_is_unknown_without_timestamps(tmp_path: Path) -> None:
    """No timestamp is not the same as no gap, and must not read as 'fine'."""
    paths = _ramp_parts(tmp_path, [48_000, 48_000])
    with Recording.open(paths) as rec:
        assert rec.join_gaps_s() == [None]
        problems = rec.continuity_problems()
        assert len(problems) == 1
        assert "continuity unknown" in problems[0]


def test_gapless_files_report_no_problem(tmp_path: Path) -> None:
    """Close times one file-duration apart mean nothing was lost at the join."""
    paths = _ramp_parts(tmp_path, [48_000 * 10, 48_000 * 10])
    _add_bext(paths[0], "2026-08-25 12:00:00")
    _add_bext(paths[1], "2026-08-25 12:00:10")  # exactly the second file's length
    with Recording.open(paths) as rec:
        assert rec.join_gaps_s()[0] == pytest.approx(0.0, abs=0.01)
        assert rec.continuity_problems() == []


def test_a_real_gap_is_reported(tmp_path: Path) -> None:
    """A recorder that stopped between files must not be silently spanned."""
    paths = _ramp_parts(tmp_path, [48_000 * 10, 48_000 * 10])
    _add_bext(paths[0], "2026-08-25 12:00:00")
    _add_bext(paths[1], "2026-08-25 12:00:40")  # 30 s more than the file is long
    with Recording.open(paths) as rec:
        assert rec.join_gaps_s()[0] == pytest.approx(30.0, abs=0.01)
        problems = rec.continuity_problems()
        assert len(problems) == 1
        assert "missing" in problems[0]


def test_bext_closed_at_is_none_when_absent(tmp_path: Path) -> None:
    path = _write_wav(tmp_path / "plain.wav", np.zeros(100))
    assert bext_closed_at(path) is None
    assert bext_closed_at(tmp_path / "does_not_exist.wav") is None
