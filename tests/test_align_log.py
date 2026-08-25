"""Tests for the log -> recording sidecar (``src/fakefish/align_log.py``).

The aligner itself is covered by ``test_clock_align.py``; this covers the parts
fakefish owns: the vectorised matcher, the sidecar's schema and its refusals.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl
import pytest
import typer

from fakefish import align_log as al
from synth_fixtures import make_aligned_pair, make_pulse_log, make_recording


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


# ===== the sidecar, end to end =============================================
@pytest.fixture
def pair(tmp_path: Path):
    return make_aligned_pair(
        tmp_path,
        scale=1.0 + 30e-6,
        offset_s=9.75,
        n_pulses=400,
        log_duration_s=200.0,
        recording_duration_s=240.0,
        noise_rms=0.003,
        amplitude=0.15,
    )


def _run(log_path, wav_path, out, **kw):
    al.run(log_path, wav_path, out=out, channel=0, verbose=0, **kw)


def test_sidecar_recovers_the_injected_clock(pair, tmp_path: Path) -> None:
    log_path, wav_path, _truth = pair
    out = tmp_path / "a.align.csv"
    _run(log_path, wav_path, out)

    text = out.read_text()
    assert text.startswith(al.SIDECAR_MAGIC + "\n"), (
        "the sidecar must not be mistakable for a pulse log"
    )
    header = {
        k: v
        for k, _, v in (ln[1:].partition("=") for ln in text.splitlines() if ln.startswith("#"))
    }
    assert header["format_version"] == str(al.SIDECAR_FORMAT_VERSION)
    assert header["validated"] == "1"
    assert float(header["offset_s"]) == pytest.approx(9.75, abs=2e-3)
    assert float(header["drift_ppm"]) == pytest.approx(30.0, abs=5.0)
    assert header["model"] == "linear2"
    # Provenance: both inputs are hashed, so a sidecar can never be silently
    # re-paired with a different recording.
    assert len(header["log_sha256"]) == 64
    assert len(header["recording_sha256"]) == 64
    assert header["recording_channel"] == "0"
    # The wander number is what says whether the linear model was adequate.
    assert "residual_ptp_local_s" in header


def test_sidecar_columns_and_absent_fields(pair, tmp_path: Path) -> None:
    log_path, wav_path, _truth = pair
    out = tmp_path / "a.align.csv"
    _run(log_path, wav_path, out)

    frame = pl.read_csv(out, comment_prefix="#", infer_schema_length=None)
    assert frame.columns == list(al.COLUMNS)
    assert frame.height > 300

    # t_rec_s is written for EVERY row: the fit predicts a time whether or not a
    # detector found one there.
    assert frame["t_rec_s"].null_count() == 0
    # ...while an unmatched row leaves the measured columns EMPTY, never 0 and
    # never -1. Same rule as the pulse log's own absent fields.
    unmatched = frame.filter(pl.col("status") != "matched")
    if unmatched.height:
        assert unmatched["t_det_s"].null_count() == unmatched.height
        assert unmatched["resid_s"].null_count() == unmatched.height
    assert set(frame["status"].unique()) <= {"matched", "unmatched", "outside"}

    matched = frame.filter(pl.col("status") == "matched")
    assert matched.height / frame.height > 0.9
    assert matched["resid_s"].abs().median() < 1e-3


def test_offset_column_is_consistent_with_the_two_time_columns(pair, tmp_path: Path) -> None:
    """`offset_s` is a convenience, so it must not be able to disagree."""
    log_path, wav_path, _truth = pair
    out = tmp_path / "a.align.csv"
    _run(log_path, wav_path, out)
    f = pl.read_csv(out, comment_prefix="#", infer_schema_length=None)
    delta = (f["t_rec_s"] - f["t_log_s"] - f["offset_s"]).abs().max()
    assert delta < 2e-6, "rounding only"


def test_detections_file_marks_what_the_log_does_not_explain(pair, tmp_path: Path) -> None:
    """The unexplained detections are the point, not the error term.

    The recording carries the animal's response as well as the playback, so a
    detection with no logged pulse behind it is a candidate response.
    """
    log_path, wav_path, _truth = pair
    out = tmp_path / "a.align.csv"
    det_out = tmp_path / "a.detections.csv"
    _run(log_path, wav_path, out, detections_out=det_out)

    f = pl.read_csv(det_out, comment_prefix="#", infer_schema_length=None)
    assert f.columns == list(al.DETECTION_COLUMNS)
    assert set(f["status"].unique()) <= {"explained", "unexplained"}
    # An unexplained detection has no seq: null, never -1.
    un = f.filter(pl.col("status") == "unexplained")
    if un.height:
        assert un["matched_seq"].null_count() == un.height
    exp = f.filter(pl.col("status") == "explained")
    assert exp["matched_seq"].null_count() == 0
    # On a synthetic pair with no second fish, almost everything is explained.
    assert exp.height / f.height > 0.9


def test_refuses_to_overwrite(pair, tmp_path: Path) -> None:
    log_path, wav_path, _truth = pair
    out = tmp_path / "a.align.csv"
    out.write_text("do not clobber me\n")
    with pytest.raises(typer.BadParameter):
        _run(log_path, wav_path, out)
    assert out.read_text() == "do not clobber me\n"


def test_rejects_a_channel_that_does_not_exist(pair, tmp_path: Path) -> None:
    log_path, wav_path, _truth = pair
    with pytest.raises(typer.BadParameter):
        al.run(log_path, wav_path, out=tmp_path / "a.csv", channel=7, verbose=0)


def test_an_unpaired_log_and_recording_do_not_validate(tmp_path: Path) -> None:
    """The negative case, and it must FAIL rather than produce a confident number.

    Two synthetic sessions with unrelated pulse trains. `validate()` has to refuse
    them, the command has to exit non-zero, and nothing may be written -- an
    unvalidated alignment reaching a viewer looks exactly like a good one.
    """
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    log_a, _wav_a, _ = make_aligned_pair(
        a_dir, scale=1.0, offset_s=5.0, n_pulses=300, log_duration_s=150.0,
        recording_duration_s=180.0, noise_rms=0.003, amplitude=0.15, seed=1,
    )
    _log_b, wav_b, _ = make_aligned_pair(
        b_dir, scale=1.0, offset_s=61.0, n_pulses=300, log_duration_s=150.0,
        recording_duration_s=180.0, noise_rms=0.003, amplitude=0.15, seed=99,
    )
    out = tmp_path / "cross.align.csv"
    with pytest.raises(typer.Exit) as exc:
        al.run(log_a, wav_b, out=out, channel=0, verbose=0)
    assert exc.value.exit_code == 1
    assert not out.exists(), "a refused alignment must leave no file behind"


def test_allow_unvalidated_writes_but_says_so(tmp_path: Path) -> None:
    """The escape hatch exists, and it must brand the file rather than hide."""
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    log_a, _wav_a, _ = make_aligned_pair(
        a_dir, scale=1.0, offset_s=5.0, n_pulses=300, log_duration_s=150.0,
        recording_duration_s=180.0, noise_rms=0.003, amplitude=0.15, seed=2,
    )
    _log_b, wav_b, _ = make_aligned_pair(
        b_dir, scale=1.0, offset_s=61.0, n_pulses=300, log_duration_s=150.0,
        recording_duration_s=180.0, noise_rms=0.003, amplitude=0.15, seed=98,
    )
    out = tmp_path / "cross.align.csv"
    al.run(log_a, wav_b, out=out, channel=0, verbose=0, allow_unvalidated=True)
    header = out.read_text()
    assert "#validated=0" in header
    assert "#validation_warnings=" in header
    reasons = next(
        ln for ln in header.splitlines() if ln.startswith("#validation_warnings=")
    )
    assert reasons != "#validation_warnings=", "a failed fit must say why"


# ===== trial spans =========================================================
def test_item_durations_cover_the_library_and_are_plausible() -> None:
    """A trial's length comes from its drawn item, so these must be real."""
    d = al.item_durations_s()
    assert len(d) >= 100
    vals = np.array(list(d.values()))
    assert (vals > 0).all()
    # The RC device's volley range (items 7..106) runs ~0.2-2.2 s.
    volleys = np.array([d[i] for i in range(7, 107)])
    assert 0.1 < volleys.min() < 0.3
    assert 1.5 < volleys.max() < 3.0


def _pair_with_trials(tmp_path: Path):
    """A synthetic pair whose log carries all three arms."""
    rate_dev, rate_rec = 50_000, 48_000
    scale, offset = 1.0 + 20e-6, 4.5
    ticks = np.arange(60, dtype=np.int64) * 15_000 + 500_000
    trial_ticks = np.array([700_000, 1_000_000, 1_300_000, 1_600_000], dtype=np.int64)
    log_path = make_pulse_log(
        tmp_path / "log.CSV", ticks=ticks, sample_rate_hz=rate_dev,
        trial_ticks=trial_ticks, trial_outcomes=["V", "B", "S", "S"],
    )
    t_rec = scale * (ticks / rate_dev) + offset
    wav = tmp_path / "rec.wav"
    make_recording(
        wav, sample_rate_hz=rate_rec, duration_s=60.0, pulse_times_s=t_rec,
        n_channels=1, channel=0, amplitude=0.15, noise_rms=0.003,
    )
    return log_path, wav


def test_trials_sidecar_contains_every_arm_including_silence(tmp_path: Path) -> None:
    """The SILENCE arm emits nothing, so this file is the ONLY place it exists.

    A viewer built on the per-pulse sidecar alone would draw the treatment and
    silently omit the control it is meant to be compared against.
    """
    log_path, wav = _pair_with_trials(tmp_path)
    out = tmp_path / "a.align.csv"
    trials_out = tmp_path / "a.trials.csv"
    al.run(log_path, wav, out=out, channel=0, verbose=0,
           trials_out=trials_out, allow_unvalidated=True)

    f = pl.read_csv(trials_out, comment_prefix="#", infer_schema_length=None)
    assert f.columns == list(al.TRIAL_COLUMNS)
    assert f.height == 4, "every TRIAL row appears, whatever it resolved to"
    assert f["arm"].to_list() == ["VOLLEY", "BASELINE", "SILENCE", "SILENCE"]

    # A silent arm has no pulses but still occupies a real window.
    silence = f.filter(pl.col("arm") == "SILENCE")
    assert (silence["n_pulses"] == 0).all()
    assert (silence["duration_s"] > 0).all()
    assert (silence["t_rec_end_s"] > silence["t_rec_start_s"]).all()


def test_trial_span_comes_from_the_item_not_from_its_pulses(tmp_path: Path) -> None:
    """A baseline arm carrying one pulse still occupies its whole window.

    Measured from its pulses it would collapse to an instant, which is exactly
    the mistake this file exists to prevent.
    """
    log_path, wav = _pair_with_trials(tmp_path)
    out = tmp_path / "a.align.csv"
    trials_out = tmp_path / "a.trials.csv"
    al.run(log_path, wav, out=out, channel=0, verbose=0,
           trials_out=trials_out, allow_unvalidated=True)

    f = pl.read_csv(trials_out, comment_prefix="#", infer_schema_length=None)
    base = f.filter(pl.col("arm") == "BASELINE")
    assert base.height == 1
    row = base.row(0, named=True)
    assert row["n_pulses"] == 1, "the fixture's baseline arm has only its anchor"
    # ...yet its span is the drawn item's duration, not zero.
    durations = al.item_durations_s()
    assert row["duration_s"] == pytest.approx(durations[row["item"]], rel=1e-6)
    assert row["t_rec_end_s"] - row["t_rec_start_s"] > 0.1


def test_trial_spans_are_ordered_and_do_not_overlap(tmp_path: Path) -> None:
    log_path, wav = _pair_with_trials(tmp_path)
    out = tmp_path / "a.align.csv"
    trials_out = tmp_path / "a.trials.csv"
    al.run(log_path, wav, out=out, channel=0, verbose=0,
           trials_out=trials_out, allow_unvalidated=True)

    f = pl.read_csv(trials_out, comment_prefix="#", infer_schema_length=None)
    start = f["t_rec_start_s"].to_numpy()
    end = f["t_rec_end_s"].to_numpy()
    assert (np.diff(start) > 0).all(), "trials must come out in time order"
    assert (end[:-1] < start[1:]).all(), "one trial must finish before the next starts"


def test_trials_refuses_to_overwrite(tmp_path: Path) -> None:
    log_path, wav = _pair_with_trials(tmp_path)
    trials_out = tmp_path / "a.trials.csv"
    trials_out.write_text("keep me\n")
    with pytest.raises(typer.BadParameter):
        al.run(log_path, wav, out=tmp_path / "a.align.csv", channel=0, verbose=0,
               trials_out=trials_out, allow_unvalidated=True)
    assert trials_out.read_text() == "keep me\n"


def test_trials_file_is_written_by_default(tmp_path: Path) -> None:
    """Default-on, because a viewer without it is missing the control condition."""
    log_path, wav = _pair_with_trials(tmp_path)
    out = tmp_path / "alignment.csv"
    al.run(log_path, wav, out=out, channel=0, verbose=0, allow_unvalidated=True)

    derived = tmp_path / "alignment.trials.csv"
    assert derived.is_file(), "the trial spans must appear without being asked for"
    f = pl.read_csv(derived, comment_prefix="#", infer_schema_length=None)
    assert f.height == 4
    assert "SILENCE" in f["arm"].to_list()


def test_no_trials_opts_out(tmp_path: Path) -> None:
    log_path, wav = _pair_with_trials(tmp_path)
    out = tmp_path / "alignment.csv"
    al.run(log_path, wav, out=out, channel=0, verbose=0,
           no_trials=True, allow_unvalidated=True)
    assert not (tmp_path / "alignment.trials.csv").exists()
    assert out.is_file()


def test_a_log_with_no_trials_still_writes_a_span_file(pair, tmp_path: Path) -> None:
    """An empty trial table is a valid answer and must not crash the run."""
    log_path, wav_path, _truth = pair
    out = tmp_path / "alignment.csv"
    al.run(log_path, wav_path, out=out, channel=0, verbose=0)
    f = pl.read_csv(
        tmp_path / "alignment.trials.csv", comment_prefix="#", infer_schema_length=None
    )
    assert f.height == 0
    assert f.columns == list(al.TRIAL_COLUMNS)


def test_every_destination_is_checked_before_the_expensive_work(tmp_path: Path) -> None:
    """A collision on the SECOND file must not leave the first one written.

    Detection over an hour-long recording takes minutes; discovering the clash
    afterwards would throw that away and leave a half-written set behind.
    """
    log_path, wav = _pair_with_trials(tmp_path)
    out = tmp_path / "alignment.csv"
    (tmp_path / "alignment.trials.csv").write_text("in the way\n")

    with pytest.raises(typer.BadParameter) as exc:
        al.run(log_path, wav, out=out, channel=0, verbose=0, allow_unvalidated=True)
    assert "alignment.trials.csv" in str(exc.value)
    assert not out.exists(), "nothing may be written once a destination is taken"
