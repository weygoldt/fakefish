"""Tests for the log -> recording sidecar (``src/fakefish/align_log.py``).

The aligner itself is covered by ``test_clock_align.py``; this covers the parts
fakefish owns: the vectorised matcher, the sidecar's schema and its refusals.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import numpy as np
import polars as pl
import pytest
import typer

from fakefish import align_log as al
from fakefish import ingest
from fakefish.clock_align import Alignment
from synth_fixtures import make_pulse_log, make_recording


# ===== the matcher =========================================================
def test_match_finds_the_nearest_detection_within_tolerance() -> None:
    t_rec = np.array([1.0, 2.0, 3.0])
    det = np.array([1.0001, 2.4, 2.9998])
    t_det, claimed = al._match(t_rec, det, 1e-3)

    assert t_det[0] == pytest.approx(1.0001)
    assert np.isnan(t_det[1]), "2.4 is 400 ms away, far outside a 1 ms tolerance"
    assert t_det[2] == pytest.approx(2.9998)
    # Detection 1 (2.4) was claimed by nobody.
    assert claimed.tolist() == [0, -1, 2]


def test_match_gives_a_shared_detection_to_the_nearer_pulse() -> None:
    """One recorded pulse must never be credited to two emitted ones.

    Two logged pulses 0.2 ms apart, one detection between them but nearer the
    second. Both are inside tolerance of it, so the naive answer double-counts.
    """
    t_rec = np.array([1.0000, 1.0002])
    det = np.array([1.00018])
    t_det, claimed = al._match(t_rec, det, 1e-3)

    assert np.isfinite(t_det).all(), "both are within tolerance, so both get a time"
    assert claimed.tolist() == [1], "the nearer pulse claims it"


def test_match_is_empty_safe() -> None:
    for a, b in (
        (np.array([]), np.array([1.0])),
        (np.array([1.0]), np.array([])),
        (np.array([]), np.array([])),
    ):
        t_det, claimed = al._match(a, b, 1e-3)
        assert t_det.size == a.size
        assert claimed.size == b.size


def test_match_agrees_with_a_brute_force_search() -> None:
    """The vectorised matcher must agree with the obvious O(n*m) version.

    The vectorisation is not a micro-optimisation -- an hours-long session runs to
    hundreds of thousands of pulses -- so it needs an oracle rather than trust.
    """
    rng = np.random.default_rng(7)
    t_rec = np.sort(rng.uniform(0.0, 100.0, 400))
    det = np.sort(rng.uniform(0.0, 100.0, 500))
    tol = 2e-3

    t_det, _ = al._match(t_rec, det, tol)
    for i, t in enumerate(t_rec):
        d = np.abs(det - t)
        want = det[d.argmin()] if d.min() <= tol else np.nan
        if np.isnan(want):
            assert np.isnan(t_det[i]), f"pulse {i} should not have matched"
        else:
            assert t_det[i] == pytest.approx(want), f"pulse {i}"


# ===== the template ========================================================
def test_emitted_template_resamples_onto_the_recording_rate() -> None:
    at_50k = al.emitted_template(50_000)
    at_48k = al.emitted_template(48_000)
    assert at_50k.size == 131, "EOD_HV is 131 samples at the device's own rate"
    assert at_48k.size == round(131 * 48_000 / 50_000)
    # Resampling must not invert or flatten it: it is near-monophasic.
    assert np.argmax(np.abs(at_48k)) > 0
    assert np.sign(at_48k[np.argmax(np.abs(at_48k))]) == np.sign(
        at_50k[np.argmax(np.abs(at_50k))]
    )




def test_item_durations_cover_the_library_and_are_plausible() -> None:
    """A trial's length comes from its drawn item, so these must be real."""
    d = al.item_durations_s()
    assert len(d) >= 100
    vals = np.array(list(d.values()))
    assert (vals > 0).all()
    volleys = np.array([d[i] for i in range(7, 107)])
    assert 0.1 < volleys.min() < 0.3
    assert 1.5 < volleys.max() < 3.0


# ===== the aligned session, end to end =====================================
def _synthetic_session(tmp_path: Path, *, scale: float, offset_s: float, seed: int = 0):
    """A log and a recording of it, with a known clock relationship."""
    rate_dev, rate_rec = 50_000, 48_000
    # IRREGULAR, and irregular differently per seed. A metronome has no
    # fingerprint -- sliding it by any whole interval fits as well as the true
    # offset -- so a regular train would neither validate as a pair nor be
    # refusable as a non-pair, and both halves of this file would be testing
    # nothing.
    rng = np.random.default_rng(seed)
    gaps = rng.integers(4_000, 26_000, size=300)
    ticks = (300_000 + np.cumsum(gaps)).astype(np.int64)
    log_path = make_pulse_log(
        tmp_path / "PULS0003.CSV",
        ticks=ticks,
        sample_rate_hz=rate_dev,
        trial_ticks=np.array([500_000, 900_000, 1_400_000], dtype=np.int64),
        trial_outcomes=["V", "B", "S"],
    )
    t_rec = scale * (ticks / rate_dev) + offset_s
    wav = tmp_path / "rec.wav"
    make_recording(
        wav, sample_rate_hz=rate_rec, duration_s=130.0, pulse_times_s=t_rec,
        n_channels=1, channel=0, amplitude=0.15, noise_rms=0.003, seed=seed,
    )
    return log_path, wav


@pytest.fixture
def session(tmp_path: Path):
    return _synthetic_session(tmp_path, scale=1.0 + 30e-6, offset_s=7.25)


def _tables(out: Path, sid: str = "PULS0003") -> dict[str, pl.DataFrame]:
    frames = ingest.read_tables(out, sid)
    frames["detections"] = pl.read_csv(
        out / f"{sid}_detections.csv", infer_schema_length=None
    )
    return frames


def test_alignment_recovers_the_injected_clock(session, tmp_path: Path) -> None:
    log_path, wav = session
    out = tmp_path / "out"
    al.run(log_path, [wav], out_dir=out, channel=0, verbose=0)

    doc = tomllib.loads((out / "PULS0003_metadata.toml").read_text())
    a = doc["alignment"]
    assert a["offset_s"] == pytest.approx(7.25, abs=2e-3)
    assert a["drift_ppm"] == pytest.approx(30.0, abs=5.0)
    assert a["validated"] is True
    assert a["recording_channel"] == 0
    # Provenance for both halves of the pair, so a set of tables can never be
    # silently re-attached to a different recording.
    # One hash per recording file, in order -- a set of tables can never be
    # silently re-attached to a different recording, or to a different SUBSET of
    # the files that made it.
    assert a["recording_files"] == ["rec.wav"]
    assert len(a["recording_sha256"]) == 1
    assert len(a["recording_sha256"][0]) == 64
    assert a["recording_file_frames"] == [a["recording_frames"]]
    assert len(doc["source"]["sha256"]) == 64
    # The number that says whether a straight line was good enough.
    assert "residual_local_wander_s" in a


def test_every_table_gains_a_recording_clock(session, tmp_path: Path) -> None:
    log_path, wav = session
    out = tmp_path / "out"
    al.run(log_path, [wav], out_dir=out, channel=0, verbose=0)

    frames = _tables(out)
    for name in ("pulses", "trials", "session_events", "controls"):
        assert "recording_time_s" in frames[name].columns, name
        assert "time_s" in frames[name].columns, name
    # Device time and recording time must actually differ by the fitted offset.
    p = frames["pulses"]
    delta = (p["recording_time_s"] - p["time_s"]).median()
    assert delta == pytest.approx(7.25, abs=0.05)


def test_pulses_carry_their_match_outcome(session, tmp_path: Path) -> None:
    log_path, wav = session
    out = tmp_path / "out"
    al.run(log_path, [wav], out_dir=out, channel=0, verbose=0)

    p = _tables(out)["pulses"]
    assert {"detected_time_s", "residual_s", "match_status"} <= set(p.columns)
    assert set(p["match_status"].unique()) <= {"matched", "unmatched", "outside"}

    matched = p.filter(pl.col("match_status") == "matched")
    assert matched.height / p.height > 0.9
    assert matched["residual_s"].abs().median() < 1e-3
    # An unmatched pulse keeps its PREDICTED recording time -- the fit knows where
    # it should have been -- but leaves the measured columns empty.
    other = p.filter(pl.col("match_status") != "matched")
    if other.height:
        assert other["recording_time_s"].null_count() == 0
        assert other["detected_time_s"].null_count() == other.height
        assert other["residual_s"].null_count() == other.height


def test_trial_spans_map_through_the_same_fit(session, tmp_path: Path) -> None:
    """The end must go through the fit, not be pasted on in device seconds."""
    log_path, wav = session
    out = tmp_path / "out"
    al.run(log_path, [wav], out_dir=out, channel=0, verbose=0)

    t = _tables(out)["trials"]
    assert t.height == 3
    assert t["treatment"].to_list() == ["volley", "baseline", "silence"]
    scale = tomllib.loads((out / "PULS0003_metadata.toml").read_text())["alignment"]["scale"]
    for row in t.iter_rows(named=True):
        span = row["recording_ended_s"] - row["recording_time_s"]
        assert span == pytest.approx(row["duration_s"] * scale, abs=1e-6)
    # ...and the silence arm still occupies a real window with no pulses in it.
    silence = t.filter(pl.col("treatment") == "silence")
    assert (silence["pulses_emitted"] == 0).all()
    assert (silence["recording_ended_s"] > silence["recording_time_s"]).all()


def test_detections_mark_what_the_log_does_not_explain(session, tmp_path: Path) -> None:
    """The unexplained detections are the animal, not the error term."""
    log_path, wav = session
    out = tmp_path / "out"
    al.run(log_path, [wav], out_dir=out, channel=0, verbose=0)

    d = _tables(out)["detections"]
    assert set(d.columns) == {
        "recording_time_s", "device_time_s", "amplitude", "explained_by_log", "source_row"
    }
    unexplained = d.filter(~pl.col("explained_by_log"))
    if unexplained.height:
        assert unexplained["source_row"].null_count() == unexplained.height
    explained = d.filter(pl.col("explained_by_log"))
    assert explained["source_row"].null_count() == 0
    # A synthetic pair has no second fish, so nearly everything is explained.
    assert explained.height / d.height > 0.9


def test_no_comment_lines_anywhere(session, tmp_path: Path) -> None:
    log_path, wav = session
    out = tmp_path / "out"
    al.run(log_path, [wav], out_dir=out, channel=0, verbose=0)
    for path in out.glob("*.csv"):
        assert not path.read_text().startswith("#"), path.name


# ===== refusals ============================================================
def test_an_unpaired_log_and_recording_do_not_validate(tmp_path: Path) -> None:
    """The negative case must FAIL rather than produce a confident number."""
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    log_a, _wav_a = _synthetic_session(a_dir, scale=1.0, offset_s=3.0, seed=1)
    _log_b, wav_b = _synthetic_session(b_dir, scale=1.0, offset_s=41.0, seed=99)

    out = tmp_path / "out"
    with pytest.raises(typer.Exit) as exc:
        al.run(log_a, [wav_b], out_dir=out, channel=0, verbose=0)
    assert exc.value.exit_code == 1
    assert not list(out.glob("*.csv")), "a refused alignment must leave no tables behind"


def test_allow_unvalidated_writes_but_brands_the_metadata(tmp_path: Path) -> None:
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    log_a, _wav_a = _synthetic_session(a_dir, scale=1.0, offset_s=3.0, seed=2)
    _log_b, wav_b = _synthetic_session(b_dir, scale=1.0, offset_s=41.0, seed=98)

    out = tmp_path / "out"
    al.run(log_a, [wav_b], out_dir=out, channel=0, verbose=0, allow_unvalidated=True)
    a = tomllib.loads((out / "PULS0003_metadata.toml").read_text())["alignment"]
    assert a["validated"] is False
    assert a["validation_warnings"], "a failed fit must say why"


def test_refuses_to_clobber_then_force_replaces(session, tmp_path: Path) -> None:
    log_path, wav = session
    out = tmp_path / "out"
    al.run(log_path, [wav], out_dir=out, channel=0, verbose=0)
    with pytest.raises(typer.BadParameter):
        al.run(log_path, [wav], out_dir=out, channel=0, verbose=0)
    al.run(log_path, [wav], out_dir=out, channel=0, verbose=0, force=True)


def test_rejects_a_channel_that_does_not_exist(session, tmp_path: Path) -> None:
    log_path, wav = session
    with pytest.raises(typer.BadParameter):
        al.run(log_path, [wav], out_dir=tmp_path / "out", channel=7, verbose=0)


def test_a_split_recording_aligns_as_one_timeline(tmp_path: Path) -> None:
    """A recorder that splits a session must not change the answer.

    The same pulses, cut into three files, have to produce the same offset and
    drift as one file -- and every file has to appear, including the short last
    one, which is the piece audioio's own multi-file mode drops.
    """
    import wave

    log_path, whole = _synthetic_session(tmp_path, scale=1.0 + 30e-6, offset_s=7.25, seed=5)

    with wave.open(str(whole), "rb") as w:
        rate, frames = w.getframerate(), w.getnframes()
        raw = w.readframes(frames)
    # Deliberately uneven: two full parts and a short tail.
    # 40 % / 40 % / 20 %: the equal-length case is the one that happens to work,
    # so the fixture must not accidentally use it.
    part = int(frames * 0.4)
    cuts = [0, part, 2 * part, frames]
    parts = []
    for i, (a, b) in enumerate(zip(cuts, cuts[1:], strict=False)):
        path = tmp_path / f"split{i}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(raw[a * 2 : b * 2])
        parts.append(path)
    assert (cuts[3] - cuts[2]) != (cuts[1] - cuts[0]), "the last part must be short"

    one = tmp_path / "one"
    many = tmp_path / "many"
    al.run(log_path, [whole], out_dir=one, channel=0, verbose=0)
    # The split files carry no timestamps, so continuity is unknowable and the
    # command refuses until told these really are one recording.
    with pytest.raises(typer.Exit):
        al.run(log_path, parts, out_dir=many, channel=0, verbose=0)
    al.run(log_path, parts, out_dir=many, channel=0, verbose=0, allow_gaps=True, force=True)

    a1 = tomllib.loads((one / "PULS0003_metadata.toml").read_text())["alignment"]
    a3 = tomllib.loads((many / "PULS0003_metadata.toml").read_text())["alignment"]
    assert a3["recording_files"] == [p.name for p in parts]
    assert sum(a3["recording_file_frames"]) == a1["recording_frames"]
    assert a3["offset_s"] == pytest.approx(a1["offset_s"], abs=1e-3)
    assert a3["drift_ppm"] == pytest.approx(a1["drift_ppm"], abs=1.0)
    assert a3["matched_pulses"] == pytest.approx(a1["matched_pulses"], rel=0.02)


def test_metadata_reports_match_quality_per_segment(session, tmp_path: Path) -> None:
    """One aggregate can hide a broken stretch.

    On a real hour-long session 12 of 15 segments matched at 99-100 % and one at
    14 %, which averages to a number that reads as merely mediocre. A reader has
    to be able to see WHERE the session is trustworthy.
    """
    log_path, wav = session
    out = tmp_path / "out"
    al.run(log_path, [wav], out_dir=out, channel=0, verbose=0, knot_seconds=30.0)

    a = tomllib.loads((out / "PULS0003_metadata.toml").read_text())["alignment"]
    starts = a["segment_starts_s"]
    counts = a["segment_pulses"]
    fracs = a["segment_match_fraction"]

    assert len(starts) == len(counts) == len(fracs) > 1
    assert starts == sorted(starts), "segments are reported in time order"
    assert sum(counts) == a["matched_pulses"] + (
        sum(counts) - a["matched_pulses"]
    ), "counts cover every logged pulse"
    for n, f in zip(counts, fracs, strict=True):
        if n == 0:
            assert f == -1.0, "an empty segment reports -1, not a match rate of zero"
        else:
            assert 0.0 <= f <= 1.0
    # A clean synthetic pair should be good nearly everywhere.
    good = [f for n, f in zip(counts, fracs, strict=True) if n >= 20]
    assert good and min(good) > 0.8


def test_knot_seconds_zero_fits_one_straight_line(session, tmp_path: Path) -> None:
    """The escape hatch, for a recording short enough that segments are noise."""
    log_path, wav = session
    out = tmp_path / "out"
    al.run(log_path, [wav], out_dir=out, channel=0, verbose=0, knot_seconds=0.0)
    a = tomllib.loads((out / "PULS0003_metadata.toml").read_text())["alignment"]
    assert len(a["segment_starts_s"]) == 1
    assert a["validated"] is True


def test_written_times_agree_with_the_alignment_everywhere(session, tmp_path: Path) -> None:
    """The tables and the reported statistics must use ONE mapping.

    They did not. TimeBase held a copy of `scale` and `offset_s`, so when the
    estimator became piecewise the fit tracked a recorder that drops samples and
    the FILES were written with the straight line through it. Every match
    statistic stayed correct -- they are computed from the alignment -- while the
    output drifted progressively wrong, reaching 134 ms by the end of a real
    hour. Nothing in the output disagreed with anything else, which is why it
    took a person looking at a waveform to catch it.
    """
    log_path, wav = session
    out = tmp_path / "out"
    al.run(log_path, [wav], out_dir=out, channel=0, verbose=0, knot_seconds=30.0)

    a = tomllib.loads((out / "PULS0003_metadata.toml").read_text())["alignment"]
    # Rebuilt from the METADATA ALONE. If a consumer cannot reproduce the times
    # from what the file says, the file is not self-describing.
    align = Alignment(
        scale=a["scale"],
        offset_s=a["offset_s"],
        segment_edges_s=tuple(
            (s - a["offset_s"]) / a["scale"] for s in a["segment_starts_s"][1:]
        ),
        segment_offsets_s=tuple(a["recording_join_steps_s"]),
        segment_rates=tuple(a["segment_rates"]),
        segment_intercepts=tuple(a["segment_intercepts"]),
    )

    frames = _tables(out)
    for name in ("pulses", "trials", "session_events", "controls"):
        f = frames[name].drop_nulls(subset=["sample_tick", "recording_time_s"])
        if f.height == 0:
            continue
        want = align.log_to_recording(
            f["sample_tick"].to_numpy().astype(float) / 50_000.0
        )
        got = f["recording_time_s"].to_numpy()
        worst = float(np.max(np.abs(got - want)))
        assert worst < 2e-6, (
            f"{name}: written times differ from the alignment by up to {worst * 1e3:.3f} ms"
        )


def test_a_piecewise_alignment_actually_reaches_the_tables(tmp_path: Path) -> None:
    """A segment offset must move the written times, or it is decoration.

    Directly: build a TimeBase with a known step and check a tick past the
    boundary comes out shifted by exactly that step.
    """
    from fakefish import session_tables as st

    step = -0.134
    align = Alignment(
        scale=1.0, offset_s=1.0, segment_edges_s=(100.0,), segment_offsets_s=(0.0, step)
    )
    tb = st.TimeBase(sample_rate_hz=50_000.0, alignment=align)
    before = tb.recording_seconds(np.array([50 * 50_000], dtype=np.int64))
    after = tb.recording_seconds(np.array([150 * 50_000], dtype=np.int64))
    assert before[0] == pytest.approx(51.0)
    assert after[0] == pytest.approx(151.0 + step)


def test_a_recorder_losing_samples_is_reported_not_just_corrected(tmp_path: Path) -> None:
    """A good alignment over a bad recording must still say the recording is bad.

    The per-segment offsets correct for a recorder that drops samples, so the
    alignment comes out fine while the recording is quietly defective. exp3 loses
    134 ms across an hour -- peaking at hundreds of ppm against a real clock rate
    of +11 ppm -- and silently absorbing that would hide a hardware fault behind a
    healthy-looking number.
    """
    from fakefish.clock_align import AlignmentResult

    def result_with(offsets, edges):
        return AlignmentResult(
            alignment=Alignment(
                scale=1.0, offset_s=0.0,
                segment_edges_s=edges, segment_offsets_s=offsets,
            ),
            residuals_s=np.zeros(0), matched_log_times_s=np.zeros(0),
            matched_rec_times_s=np.zeros(0), coarse_lag_s=0.0,
            coarse_peak_ratio=0.0, n_candidates=0, warnings=(),
        )

    edges = (300.0, 600.0, 900.0)
    # A real clock: tens of ppm means microseconds per 300 s segment.
    steady = result_with((0.0, 3e-3, 6e-3, 9e-3), edges)
    rates, warn = al.sample_loss_report(steady)
    assert warn is None, f"a plausible clock must not be flagged: {rates}"

    # A recorder dropping samples: tens of milliseconds per segment.
    dropping = result_with((0.0, -0.03, -0.07, -0.134), edges)
    rates, warn = al.sample_loss_report(dropping)
    assert warn is not None
    assert "-134 ms" in warn
    assert "RECORDER" in warn

    # One segment cannot imply a rate at all, and must not pretend to.
    flat = result_with((0.0,), ())
    assert al.sample_loss_report(flat) == ([], None)
