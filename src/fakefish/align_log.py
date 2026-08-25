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
``--detections-out``, which writes every detected pulse together with whether
the log accounts for it: the ones it does not are the candidate responses.

A third file answers when each treatment started and stopped, and it is written
BY DEFAULT (``--no-trials`` opts out). It needs to be separate because a SILENCE
arm emits nothing, so it has no pulse rows anywhere -- a third of the trials are
absent from the per-pulse sidecar, and they are the control condition.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Optional

import numpy as np
import polars as pl
import typer
from numpy.typing import NDArray
from scipy.io import wavfile

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
    detect_pulses,
    refine_template,
    suggest_absolute_floor,
)
from fakefish.pulse_log import PulseLogFile, read
from fakefish.viz.loggers import configure_logging, get_logger

log = get_logger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)

#: Sidecar schema version. Bumped for a breaking change to the columns or the
#: header keys, exactly as the pulse log's own version is.
SIDECAR_FORMAT_VERSION = 1

#: The sidecar's magic first line. Deliberately NOT the pulse log's, so nothing
#: can mistake a derived file for a device record.
SIDECAR_MAGIC = "#fakefish-align"

#: Tolerance for calling a logged pulse "matched" in the sidecar's ``status``.
#:
#: This is NOT the estimator's own ladder, which ends at 300 us. That ladder
#: exists to *fit* tightly; reused as a verdict it rejects genuine pulses. On
#: exp2 it calls 96.8 % matched where 500 us calls 99.7 %, and the 3 % it drops
#: are real pulses displaced by short-term clock wander (measured: 546 us of
#: non-linear excursion that a 2-parameter fit cannot absorb). Recorded in the
#: header so a reader never has to guess which number produced a status.
DEFAULT_MATCH_TOLERANCE_S = 500e-6

COLUMNS = (
    "seq",
    "tick",
    "event",
    "trial",
    "t_log_s",
    "t_rec_s",
    "offset_s",
    "t_det_s",
    "resid_s",
    "status",
)

DETECTION_COLUMNS = ("index", "t_rec_s", "t_log_s", "amplitude", "matched_seq", "status")

TRIAL_COLUMNS = (
    "trial",
    "arm",
    "item",
    "t_log_start_s",
    "t_rec_start_s",
    "t_rec_end_s",
    "duration_s",
    "n_pulses",
    "n_matched",
)

#: The log's one-character arm codes, spelled out. ``S`` is the SILENCE arm; the
#: character is the two-arm design's SHAM code, kept because the quantity never
#: changed (see ``pulse_log.KIND_SHAM``).
ARM_NAMES = {"V": "VOLLEY", "B": "BASELINE", "S": "SILENCE"}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_channel(path: Path, channel: int) -> tuple[NDArray[np.float64], int, int]:
    """One channel of a WAV as float64 in -1..1, plus its rate and frame count.

    ``scipy.io.wavfile`` returns the stored integer type, so the scale depends on
    the bit depth; normalise by the dtype's range rather than by the data's own
    peak, which would silently rescale a quiet recording and move every absolute
    threshold with it.

    It also cannot memory-map 24-bit files, so this reads the whole recording:
    ~2.6 GB per hour of stereo 24-bit at 48 kHz. Fine for the minutes-long
    stereo sessions this is for; a grid stream that runs for days needs a
    windowed reader instead.
    """
    rate, data = wavfile.read(str(path))
    if data.ndim == 1:
        data = data[:, None]
    n_frames, n_channels = data.shape
    if not 0 <= channel < n_channels:
        raise typer.BadParameter(
            f"--channel {channel} but {path.name} has {n_channels} channel(s) (0-indexed)"
        )
    x = data[:, channel]
    if np.issubdtype(x.dtype, np.integer):
        x = x.astype(np.float64) / float(np.iinfo(x.dtype).max)
    else:
        x = x.astype(np.float64)
    return x, int(rate), int(n_frames)


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
) -> tuple[AlignmentResult, dict[str, NDArray[np.float64]]]:
    """Fit the clock mapping and place every logged pulse on the recording.

    Pure: no file I/O, so it is testable without a WAV. Returns the fit plus the
    per-pulse arrays the sidecar is written from.
    """
    ticks = log_file.pulse_ticks()
    t_log = ticks / float(log_file.sample_rate_hz)
    result = estimate_alignment(t_log, detections_s)
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
    recording: Annotated[Path, typer.Argument(help="The WAV recorded during that session.")],
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

    x, rate, n_frames = read_channel(recording, channel)
    duration_s = n_frames / float(rate)
    log.info(
        "%s: %d pulses over %.1f s | %s: ch%d, %d Hz, %.1f s",
        log_path.name, n_pulses, float(np.ptp(log_file.pulse_times_s())),
        recording.name, channel, rate, duration_s,
    )

    floor = suggest_absolute_floor(x, fraction=0.05)
    params = DetectionParams(snr_threshold=8.0, absolute_floor=floor)
    first = detect_pulses(x, rate, emitted_template(rate), params)
    # Close the loop on the template: the recorded pulse is a differentiated EOD,
    # so the emitted shape is a seed and nothing more.
    found = detect_pulses(x, rate, refine_template(x, first, rate), params) if first.n else first
    log.info("detected %d pulses (%d before refining the template)", found.n, first.n)

    tol = tolerance_ms * 1e-3
    result, cols = align(log_file, found.times_s, recording_duration_s=duration_s, tolerance_s=tol)
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
    tb = tables.TimeBase(
        sample_rate_hz=float(log_file.sample_rate_hz), scale=a.scale, offset_s=a.offset_s
    )
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
            recording, channel, rate, n_frames, result, passed, reasons,
            found, n_matched, tol, params, cols,
        )},
    )
    doc["counts"].update({f"rows_{k}": v for k, v in counts.items()})
    meta.write(paths["metadata"], doc)

    log.info("%s + %s -> %s", log_path.name, recording.name, out_dir)
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
    recording, channel, rate, n_frames, result, passed, reasons,
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
        "recording_file": recording.name,
        "recording_sha256": _sha256(recording),
        "recording_rate_hz": int(rate),
        "recording_frames": int(n_frames),
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
        "detect_snr_threshold": float(params.snr_threshold),
        "detect_absolute_floor": round(float(params.absolute_floor), 9),
        "detect_refractory_s": float(params.refractory_s),
        "validated": bool(passed),
        "validation_warnings": list(reasons),
        "fit_warnings": list(result.warnings),
    }


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
