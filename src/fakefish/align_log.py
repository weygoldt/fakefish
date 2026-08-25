"""Map a pulse log onto a recording's clock, and write the mapping as a sidecar.

    t_recording = scale * t_log + offset

The stimulator's 50 kHz tick and the recorder's sample clock are independent
crystals started at unrelated instants, so nothing in a log can be placed in a
recording until that mapping is fitted. Two parts at +-20 ppm can differ by
~40 ppm, which is ~144 ms per hour -- so this fits **offset and drift**, not an
offset alone.

WHY A SIDECAR AND NOT A COLUMN IN THE LOG. The obvious move is to add a column
to ``PULSnnnn.CSV`` saying where each pulse landed. Three reasons not to:

* The log is the **device's** record and it sits in read-only field data. A
  derived column mutates a raw artifact.
* Its schema is pinned per version (``pulse_log.COLUMNS_BY_VERSION``) and the
  reader **raises** on an unexpected column row. An extra column would need a
  format bump the firmware could never satisfy, so the version number would
  start describing files no device can write.
* The offset is not a property of a pulse. It is a property of a **(log,
  recording) pair** -- the same log against two recorders has two answers.

So the mapping goes in its own file, joined back to the log on ``seq``.

WHAT THE SIDECAR IS FOR. The recording carries the stimulus *and* the animal's
response, so the two useful questions are "where in the recording is each pulse
I emitted" and "what else is in there". The first is this file. The second is
the detections table, which lists every pulse found in the audio together with
whether the log accounts for it: the ones it does not are the candidate responses.

A RECORDER MAY SPLIT A LONG SESSION INTO SEVERAL FILES. Pass them all, in order;
they are joined into one timeline whose frame 0 is the first frame of the first
file, which is how a viewer concatenates them too. Continuity is checked from the
files' own timestamps and a break is refused rather than silently spanned -- see
``fakefish.recording``.
"""

from __future__ import annotations

import hashlib
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated, Optional

import numpy as np
import polars as pl
import typer
from numpy.typing import NDArray

from fakefish import ingest
from fakefish import session_metadata as meta
from fakefish import session_tables as tables
from fakefish._resources import DEFAULT_FIRMWARE
from fakefish.clock_align import (
    AlignmentMethod,
    AlignmentResult,
    estimate_alignment,
    validate,
)
from fakefish.pulse_detect import (
    DetectionParams,
    Detections,
    detect_pulses,
    refine_template,
    suggest_absolute_floor,
)
from fakefish.pulse_log import PulseLogFile, read
from fakefish.recording import Recording
from fakefish.viz.loggers import configure_logging, get_logger

log = get_logger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)

#: Tolerance for calling a logged pulse "matched" in the pulses table.
#:
#: This is NOT the estimator's own ladder, which ends at 300 us. That ladder
#: exists to *fit* tightly; reused as a verdict it rejects genuine pulses. On
#: exp2 it calls 96.8 % matched where 500 us calls 99.7 %, and the 3 % it drops
#: are real pulses displaced by short-term clock wander (measured: 0.52 ms of
#: non-linear excursion a 2-parameter fit cannot absorb). Recorded in the
#: metadata so a reader never has to guess which number produced a status.
DEFAULT_MATCH_TOLERANCE_S = 500e-6

#: How often to place a segment boundary along the session, seconds.
#:
#: The lag does not follow a straight line for an hour. On exp3 it wanders 116 ms
#: end to end, so a single rate and offset match 17 % of pulses; boundaries every
#: 300 s take that to 86 % and recover +11.2 ppm against a directly measured ~+11.
#: A short session is unaffected -- exp2 goes 96.6 % to 96.8 %.
#:
#: 300 s rather than finer: closer spacing gives each segment fewer pulses, so its
#: seed correlation gets noisier, and exp3 falls back to 82 % at 150 s. This is a
#: floor on how much data a segment needs, not a statement about how fast clocks
#: wander.
DEFAULT_KNOT_S = 300.0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def emitted_template(sample_rate_hz: float) -> NDArray[np.float64]:
    """``EOD_HV`` resampled from the device's 50 kHz onto the recording's rate.

    A seed only. The recorded pulse is not the emitted one -- electrode coupling
    and the amplifier's high-pass differentiate a monophasic EOD into something
    biphasic -- so the caller refines this against the data before trusting it.
    """
    from fakefish.export_teensy_stimuli import PLAYBACK_RATE_HZ, parse_firmware

    eod = np.asarray(parse_firmware(DEFAULT_FIRMWARE)["EOD_HV"], dtype=np.float64)
    if sample_rate_hz == PLAYBACK_RATE_HZ:
        return eod
    n = int(round(eod.size * sample_rate_hz / PLAYBACK_RATE_HZ))
    src = np.arange(eod.size, dtype=np.float64) / PLAYBACK_RATE_HZ
    dst = np.arange(n, dtype=np.float64) / sample_rate_hz
    return np.interp(dst, src, eod)


def item_durations_s() -> dict[int, float]:
    """Every library item's duration in DEVICE seconds, by item index.

    This is what gives a trial its length. All three arms draw an item: VOLLEY
    plays it, BASELINE and SILENCE hold for exactly its duration, so the arms
    match in length by construction. An item runs from its first pulse to one
    EOD after its last, i.e. ``sum(ipi_samp) + EOD_HV_LEN`` -- ``ipi_samp[k]`` is
    the wait BEFORE pulse k, so ``[0]`` is 0.

    (A volley overruns this by 2 samples, 40 us, because it ends when the player
    runs dry rather than on the trial clock. Far below anything that matters here
    and deliberately not modelled.)
    """
    from fakefish.export_teensy_stimuli import PLAYBACK_RATE_HZ, parse_firmware

    parsed = parse_firmware(DEFAULT_FIRMWARE)
    eod_len = len(parsed["EOD_HV"])
    return {
        i: (int(np.sum(it["ipi_samp"])) + eod_len) / float(PLAYBACK_RATE_HZ)
        for i, it in enumerate(parsed["items"])
    }


# ===== detection over a whole recording ====================================
#: Blocks are big enough that the per-block matched filter dominates the
#: bookkeeping, and small enough that one channel of one block stays well under
#: a gigabyte as float64.
DETECT_BLOCK_S = 120.0

#: Overlap so a pulse straddling a boundary is seen whole by the filter. Two
#: orders above an EOD's 2.6 ms width, and far above the detector's own 4 ms
#: template window.
DETECT_OVERLAP_S = 0.5


def _calibration_sample(
    rec: Recording, channel: int, template: NDArray[np.float64], params: DetectionParams
) -> tuple[float, NDArray[np.float64]]:
    """A noise floor and a data-derived template, from a SAMPLE of the recording.

    Both were previously measured over the whole file. On an hour-long session
    that means two full passes, and the second exists only to learn the pulse
    shape -- which a few hundred pulses settle just as well as a few hundred
    thousand. So sample, calibrate, then make ONE full pass with the answer.

    Blocks are taken spread through the recording rather than from the start: a
    session commonly opens with the device out of the water (exp2's first 46 s
    are silent), and a floor measured there would be far too low.
    """
    want = 200  # refine_template's own max_snippets; more buys nothing
    n_blocks = max(int(np.ceil(rec.duration_s / DETECT_BLOCK_S)), 1)
    probe_at = sorted({0, n_blocks // 3, 2 * n_blocks // 3, n_blocks - 1})

    floors, snippets_from, got = [], [], 0
    for b in probe_at:
        start = int(b * DETECT_BLOCK_S * rec.rate)
        x = rec.read(start, start + int(DETECT_BLOCK_S * rec.rate), channel)
        if x.size == 0:
            continue
        floors.append(suggest_absolute_floor(x, fraction=0.05))
        if got < want:
            trial = detect_pulses(x, rec.rate, template, params)
            if trial.n:
                snippets_from.append((x, trial))
                got += trial.n
    if not floors:
        raise ValueError("the recording is empty")

    # The MEDIAN floor across probes, not the minimum: one quiet block would
    # otherwise set a threshold the rest of the session trips on constantly.
    floor = float(np.median(floors))
    refined = template
    for x, trial in snippets_from:
        refined = refine_template(x, trial, rec.rate)
        break
    return floor, refined


def detect_recording(
    rec: Recording,
    channel: int,
    template: NDArray[np.float64],
    params: DetectionParams,
) -> Detections:
    """Detect pulses across the whole recording, streaming block by block.

    Each block keeps only detections in its own non-overlapping window, so a
    pulse in the overlap is claimed by exactly one block -- never dropped,
    never counted twice.
    """
    times, scores, amps, pols = [], [], [], []
    rejected = 0
    noise = []
    for x, t0 in rec.blocks(channel, DETECT_BLOCK_S, DETECT_OVERLAP_S):
        if x.size == 0:
            continue
        d = detect_pulses(x, rec.rate, template, params, t0_s=t0)
        noise.append(d.noise_scale)
        rejected += d.n_rejected_by_floor
        # The block owns [t0, t0 + DETECT_BLOCK_S); the overlap tail belongs to
        # the next one. The final block owns everything to the end.
        hi = t0 + DETECT_BLOCK_S
        keep = d.times_s < hi if (t0 + DETECT_BLOCK_S) < rec.duration_s else np.ones(
            d.times_s.size, dtype=bool
        )
        times.append(d.times_s[keep])
        scores.append(d.scores[keep])
        amps.append(d.amplitudes[keep])
        pols.append(d.polarities[keep])

    def cat(parts, dtype):
        return np.concatenate(parts).astype(dtype) if parts else np.empty(0, dtype=dtype)

    return Detections(
        times_s=cat(times, np.float64),
        scores=cat(scores, np.float64),
        amplitudes=cat(amps, np.float64),
        polarities=cat(pols, np.int8),
        noise_scale=float(np.median(noise)) if noise else 0.0,
        threshold=params.snr_threshold,
        n_rejected_by_floor=int(rejected),
    )


def _match(
    t_rec_s: NDArray[np.float64],
    detections: NDArray[np.float64],
    tolerance_s: float,
) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """Nearest detection to each predicted time, or NaN where none is inside tol.

    Returns the matched detection times and, for each detection, the index of the
    log pulse that claimed it (-1 for none) -- so the caller can say which
    detections the log does *not* account for.

    Fully vectorised. A per-pulse Python loop is the one place this pipeline would
    fall over on a long session: an hours-long playback runs to hundreds of
    thousands of pulses, and at that size the loop costs more than the detection
    and the fit together.
    """
    t_det = np.full(t_rec_s.size, np.nan)
    claimed = np.full(detections.size, -1, dtype=np.int64)
    if detections.size == 0 or t_rec_s.size == 0:
        return t_det, claimed

    # The nearest detection is one of the two straddling the insertion point.
    idx = np.searchsorted(detections, t_rec_s)
    lo = np.clip(idx - 1, 0, detections.size - 1)
    hi = np.clip(idx, 0, detections.size - 1)
    d_lo = np.abs(detections[lo] - t_rec_s)
    d_hi = np.abs(detections[hi] - t_rec_s)
    take_hi = d_hi < d_lo
    best = np.where(take_hi, hi, lo)
    best_d = np.where(take_hi, d_hi, d_lo)

    ok = best_d <= tolerance_s
    t_det[ok] = detections[best[ok]]

    # A detection can sit inside two predicted times; the NEARER pulse claims it,
    # so one recorded pulse is never credited to two emitted ones. Resolve by
    # sorting (detection, distance) and keeping the first row per detection.
    pulses = np.flatnonzero(ok)
    if pulses.size:
        dets = best[pulses]
        order = np.lexsort((best_d[pulses], dets))
        dets_sorted = dets[order]
        first = np.ones(dets_sorted.size, dtype=bool)
        first[1:] = dets_sorted[1:] != dets_sorted[:-1]
        claimed[dets_sorted[first]] = pulses[order][first]
    return t_det, claimed


def align(
    log_file: PulseLogFile,
    detections_s: NDArray[np.float64],
    *,
    recording_duration_s: float,
    tolerance_s: float = DEFAULT_MATCH_TOLERANCE_S,
    file_edges_rec_s: tuple[float, ...] = (),
    knot_s: float = DEFAULT_KNOT_S,
) -> tuple[AlignmentResult, dict[str, NDArray[np.float64]]]:
    """Fit the clock mapping and place every logged pulse on the recording.

    Pure: no file I/O, so it is testable without a WAV. Returns the fit plus the
    per-pulse arrays the sidecar is written from.
    """
    ticks = log_file.pulse_ticks()
    t_log = ticks / float(log_file.sample_rate_hz)

    # A recorder can drop samples where it splits a file, so each file gets its
    # own offset. The boundaries are known in RECORDING time and the estimator
    # wants them on the DEVICE clock, so fit once without them to get a rough
    # mapping, convert, then fit again. Detection dominates the runtime; this
    # second fit is free by comparison.
    result = estimate_alignment(t_log, detections_s)
    rough = result.alignment
    knots: list[float] = [
        float((e - rough.offset_s) / rough.scale) for e in file_edges_rec_s
    ]
    if knot_s > 0 and t_log.size:
        knots += list(np.arange(t_log.min() + knot_s, t_log.max(), knot_s))
    if knots:
        result = estimate_alignment(
            t_log, detections_s, segment_edges_s=tuple(sorted(knots))
        )
    t_rec = result.alignment.log_to_recording(t_log)
    t_det, claimed = _match(t_rec, detections_s, tolerance_s)

    status = np.where(
        np.isfinite(t_det),
        "matched",
        np.where((t_rec < 0.0) | (t_rec > recording_duration_s), "outside", "unmatched"),
    )
    return result, {
        "t_log_s": t_log,
        "t_rec_s": t_rec,
        "t_det_s": t_det,
        "resid_s": t_det - t_rec,
        "status": status,
        "claimed": claimed,
    }




@app.command()
def run(
    log_path: Annotated[Path, typer.Argument(help="A PULSnnnn.CSV device pulse log.")],
    recordings: Annotated[
        list[Path],
        typer.Argument(
            help="The WAV(s) recorded during that session, IN ORDER -- or a single "
            "DIRECTORY holding them, which is expanded and sorted by name."
        ),
    ],
    out_dir: Annotated[
        Path, typer.Option("--out-dir", "-o", help="Directory to write the tables into.")
    ],
    channel: Annotated[
        int, typer.Option("--channel", "-c", help="Recording channel carrying the playback.")
    ] = 0,
    session_id: Annotated[
        Optional[str],
        typer.Option("--session-id", help="Name prefix. Defaults to the log's stem."),
    ] = None,
    tolerance_ms: Annotated[
        float, typer.Option("--tolerance-ms", help="Match tolerance for the status column.")
    ] = DEFAULT_MATCH_TOLERANCE_S * 1e3,
    allow_unvalidated: Annotated[
        bool, typer.Option("--allow-unvalidated", help="Write even if the fit fails validate().")
    ] = False,
    knot_seconds: Annotated[
        float,
        typer.Option(
            "--knot-seconds",
            help="Spacing of segment boundaries along the session. 0 fits one "
            "straight line, which is right only for a short recording.",
        ),
    ] = DEFAULT_KNOT_S,
    allow_gaps: Annotated[
        bool,
        typer.Option(
            "--allow-gaps",
            help="Join the files even if their timestamps say recording was interrupted.",
        ),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Replace existing output files.")
    ] = False,
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 2,
) -> None:
    """Fit log -> recording time and write the session's tables.

    Writes the same tables as ``fakefish-ingest`` -- pulses, trials, session
    events, controls -- with a ``recording_time_s`` column added to each, plus a
    ``detections`` table of every pulse found in the audio. A viewer then reads
    one set of files instead of joining device time against recording time.

    Exits non-zero if the fit fails ``validate()``, unless ``--allow-unvalidated``.
    An unvalidated alignment that reaches a viewer looks exactly like a good one,
    and every position drawn from it inherits the error.
    """
    configure_logging(verbose)
    stack = ExitStack()
    log_file = read(log_path)

    missing = meta.unmapped_keys(log_file)
    if missing:
        raise typer.BadParameter(
            f"{log_path.name} carries header keys this version does not know, and "
            f"converting would drop them: {sorted(missing)}. Add them to "
            f"session_metadata.SECTIONS."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    sid = session_id or log_path.stem
    paths = ingest.destinations(out_dir, sid)
    paths["detections"] = out_dir / f"{sid}_detections.csv"
    ingest.check_free(paths, force)

    n_pulses = len(log_file.pulses())
    if not n_pulses:
        raise typer.BadParameter(f"{log_path.name} contains no emitted pulses to align")

    files = Recording.resolve(recordings)
    if len(files) != len(recordings):
        log.info("%s -> %d file(s)", recordings[0], len(files))
    rec = Recording.open(files)
    stack.enter_context(rec)
    rate, duration_s = rec.rate, rec.duration_s
    if not 0 <= channel < rec.channels:
        raise typer.BadParameter(
            f"--channel {channel} but the recording has {rec.channels} channel(s) (0-indexed)"
        )
    log.info("%s: %d pulses over %.1f s", log_path.name, n_pulses,
             float(np.ptp(log_file.pulse_times_s())))
    log.info("recording: %s", rec.describe())

    # A gap between files means the joined timeline is wrong from that point on,
    # and every annotation after it lands in the wrong place. Refuse rather than
    # emit an alignment that looks fine and is not.
    problems = rec.continuity_problems()
    for line in problems:
        log.warning("  %s", line)
    if problems and not allow_gaps:
        log.error(
            "these files do not look like one continuous recording. "
            "Pass --allow-gaps to align them anyway as if they were."
        )
        raise typer.Exit(code=1)

    params = DetectionParams(snr_threshold=8.0, absolute_floor=0.0)
    floor, template = _calibration_sample(rec, channel, emitted_template(rate), params)
    params = DetectionParams(snr_threshold=8.0, absolute_floor=floor)
    found = detect_recording(rec, channel, template, params)
    log.info("detected %d pulses across the whole recording", found.n)

    tol = tolerance_ms * 1e-3
    result, cols = align(
        log_file, found.times_s, recording_duration_s=duration_s, tolerance_s=tol,
        file_edges_rec_s=tuple(p.start_frame / rec.rate for p in rec.parts[1:]),
        knot_s=knot_seconds,
    )
    passed, reasons = validate(result)
    n_matched = int(np.count_nonzero(cols["status"] == "matched"))

    log.info("%s", result.summary())
    log.info(
        "  drift %+.2f ppm | offset %.6f s | %d/%d matched within %.0f us (%.1f %%)",
        result.alignment.drift_ppm, result.alignment.offset_s,
        n_matched, n_pulses, tol * 1e6, 100.0 * n_matched / n_pulses,
    )
    if not passed:
        log.warning("alignment did NOT validate: %s", "; ".join(reasons))
        if not allow_unvalidated:
            log.error("refusing to write. Pass --allow-unvalidated to write it anyway.")
            raise typer.Exit(code=1)

    a = result.alignment
    tb = tables.TimeBase(sample_rate_hz=float(log_file.sample_rate_hz), alignment=a)
    counts = ingest.write_tables(
        log_file, tb, paths, item_durations_s=item_durations_s(),
        pulse_match=_match_frame(log_file, cols),
    )
    counts["detections"] = _write_detections(paths["detections"], found, cols, result, log_file)

    doc = meta.build(
        log_file,
        source_path=log_path,
        source_sha256=_sha256(log_path),
        tool="fakefish-align-log",
        extra={"alignment": _alignment_meta(
            rec, channel, result, passed, reasons,
            found, n_matched, tol, params, cols,
        )},
    )
    doc["counts"].update({f"rows_{k}": v for k, v in counts.items()})
    meta.write(paths["metadata"], doc)

    log.info("%s + %d recording file(s) -> %s", log_path.name, len(rec.parts), out_dir)
    for name in ("pulses", "trials", "session_events", "controls", "detections"):
        log.info("  %-16s %6d rows  %s", name, counts[name], paths[name].name)
    log.info("  %-16s %6s       %s", "metadata", "", paths["metadata"].name)


def _match_frame(log_file: PulseLogFile, cols: dict) -> pl.DataFrame:
    """Per-pulse match outcome, keyed by the source row so the join cannot slip."""
    seq = [r.seq for r in log_file.pulses()]
    return pl.DataFrame(
        {
            "source_row": pl.Series(seq, dtype=pl.Int64),
            "detected_time_s": pl.Series(np.round(cols["t_det_s"], 6)).fill_nan(None),
            "residual_s": pl.Series(np.round(cols["resid_s"], 9)).fill_nan(None),
            "match_status": cols["status"].tolist(),
        }
    )


def _alignment_meta(
    rec, channel, result, passed, reasons,
    found, n_matched, tol, params, cols,
) -> dict:
    """Everything the old '#' header carried about the fit, as TOML keys."""
    a = result.alignment
    resid = cols["resid_s"]
    ok = np.isfinite(resid)
    r = resid[ok]

    # Local wander: the median residual per 20 s window, peak to peak. A
    # 2-parameter fit absorbs CONSTANT drift, not short-term excursions -- on a
    # real session those run ~0.5 ms against a within-window spread of tens of
    # microseconds. This is the number that says whether the linear model was
    # good enough, so it is not optional and not derivable from the median.
    ptp_local = float("nan")
    if int(ok.sum()) > 4:
        t = cols["t_rec_s"][ok]
        bins = np.floor((t - t.min()) / 20.0).astype(int)
        meds = [float(np.median(r[bins == b])) for b in np.unique(bins) if (bins == b).sum() >= 3]
        if len(meds) > 1:
            ptp_local = max(meds) - min(meds)

    def maybe(v: float) -> Optional[float]:
        return None if not np.isfinite(v) else float(v)

    return {
        # The file LIST, in order, with each one's length -- so a viewer can place a
        # boundary and a reader can tell that a session came back split. Recording
        # time is seconds from the first frame of the first file.
        "recording_files": [p.path.name for p in rec.parts],
        "recording_file_frames": [p.frames for p in rec.parts],
        "recording_sha256": [_sha256(p.path) for p in rec.parts],
        "recording_join_steps_s": [
            round(float(o), 6) for o in result.alignment.segment_offsets_s
        ],
        "recording_join_gaps_s": [
            -999.0 if g is None else round(g, 3) for g in rec.join_gaps_s()
        ],
        "recording_rate_hz": int(rec.rate),
        "recording_frames": int(rec.frames),
        "recording_channel": int(channel),
        "method": AlignmentMethod.AUTO_MATCHED_FILTER.value,
        "model": "recording_time = scale * device_time + offset_s",
        "scale": float(a.scale),
        "drift_ppm": round(float(a.drift_ppm), 6),
        "offset_s": float(a.offset_s),
        "coarse_lag_s": round(float(result.coarse_lag_s), 9),
        "coarse_peak_ratio": round(float(result.coarse_peak_ratio), 6),
        "detections": int(found.n),
        "matched_pulses": int(n_matched),
        "match_fraction": round(n_matched / result.n_candidates, 6) if result.n_candidates else 0.0,
        "match_tolerance_s": float(tol),
        # The estimator's own ladder ends tighter than the verdict tolerance, so
        # the two fractions differ. Both are reported rather than one silently
        # standing in for the other.
        "fit_match_fraction": round(float(result.match_fraction), 6),
        "residual_median_s": maybe(float(np.median(r)) if r.size else float("nan")),
        "residual_mad_s": maybe(
            float(np.median(np.abs(r - np.median(r)))) if r.size else float("nan")
        ),
        "residual_p95_abs_s": maybe(
            float(np.percentile(np.abs(r), 95)) if r.size else float("nan")
        ),
        "residual_slope_ppm": maybe(float(a.residual_slope_ppm)),
        "residual_local_wander_s": maybe(ptp_local),
        # PER-SEGMENT match rates, because one aggregate can hide a broken
        # stretch: on exp3, 12 of 15 segments match at 99-100 % and one at 14 %,
        # which averages to a number that looks merely mediocre. A reader has to
        # be able to see WHERE a session is trustworthy, not just how much of it.
        "segment_starts_s": [round(float(v), 3) for v in _segment_starts(result, cols)],
        "segment_pulses": [int(v) for v in _segment_counts(result, cols)],
        "segment_match_fraction": [
            round(float(v), 4) for v in _segment_match(result, cols, tol)
        ],
        "detect_snr_threshold": float(params.snr_threshold),
        "detect_absolute_floor": round(float(params.absolute_floor), 9),
        "detect_refractory_s": float(params.refractory_s),
        "validated": bool(passed),
        "validation_warnings": list(reasons),
        "fit_warnings": list(result.warnings),
    }



def _segment_index(result, cols) -> np.ndarray:
    """Which segment each logged pulse falls in, on the device clock."""
    edges = np.asarray(result.alignment.segment_edges_s, dtype=np.float64)
    return np.searchsorted(edges, cols["t_log_s"], side="right")


def _segment_starts(result, cols) -> list[float]:
    edges = list(result.alignment.segment_edges_s)
    return [float(np.min(cols["t_log_s"])) if cols["t_log_s"].size else 0.0] + edges


def _segment_counts(result, cols) -> list[int]:
    seg = _segment_index(result, cols)
    n = len(result.alignment.segment_edges_s) + 1
    return list(np.bincount(seg, minlength=n)[:n])


def _segment_match(result, cols, tol: float) -> list[float]:
    """Fraction matched within `tol`, per segment. NaN where a segment is empty."""
    seg = _segment_index(result, cols)
    ok = cols["status"] == "matched"
    n = len(result.alignment.segment_edges_s) + 1
    out = []
    for k in range(n):
        m = seg == k
        out.append(float(ok[m].mean()) if m.any() else -1.0)
    return out


def _write_detections(path: Path, found, cols, result, log_file: PulseLogFile) -> int:
    """Every pulse found in the audio, and whether the log accounts for it.

    The ones it does not are the point rather than the error term: the recording
    carries the animal's response as well as the playback, so a detection with no
    logged pulse behind it is a candidate response. Hence ``unexplained`` and not
    ``spurious``.
    """
    claimed = cols["claimed"]
    seq = np.array([r.seq for r in log_file.pulses()], dtype=np.int64)
    matched = np.where(claimed >= 0, seq[np.clip(claimed, 0, seq.size - 1)], -1)
    frame = pl.DataFrame(
        {
            "recording_time_s": np.round(found.times_s, 6),
            "device_time_s": np.round(result.alignment.recording_to_log(found.times_s), 6),
            "amplitude": (
                np.round(np.asarray(found.amplitudes, dtype=np.float64), 6)
                if found.amplitudes is not None
                else np.full(found.times_s.size, np.nan)
            ),
            "explained_by_log": pl.Series(claimed >= 0, dtype=pl.Boolean),
            "source_row": pl.Series(matched, dtype=pl.Int64),
        }
    ).with_columns(
        # An unexplained detection matches no logged pulse. Null, never -1.
        pl.when(pl.col("source_row") < 0).then(None).otherwise(pl.col("source_row"))
        .alias("source_row")
    )
    frame.write_csv(path)
    return frame.height


if __name__ == "__main__":  # pragma: no cover
    app()
