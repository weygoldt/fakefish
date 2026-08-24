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
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
from typing import Annotated, Optional

import numpy as np
import polars as pl
import typer
from numpy.typing import NDArray
from scipy.io import wavfile

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


def _header_lines(
    *,
    log_path: Path,
    rec_path: Path,
    channel: int,
    rate: int,
    n_frames: int,
    result: AlignmentResult,
    passed: bool,
    reasons: tuple[str, ...],
    n_det: int,
    n_matched: int,
    tolerance_s: float,
    params: DetectionParams,
    resid: NDArray[np.float64],
    t_rec: NDArray[np.float64],
    created: str,
) -> list[str]:
    a = result.alignment
    finite = resid[np.isfinite(resid)]

    def _num(v: float) -> str:
        return "" if not np.isfinite(v) else f"{v:.9g}"

    # Local wander: the median residual per 20 s window, peak-to-peak. A
    # 2-parameter fit absorbs CONSTANT drift, not short-term excursions -- on exp2
    # those run 546 us against a within-window spread of tens of microseconds. This
    # is the number that says whether the linear model was good enough, so it is
    # not optional and it is not derivable from the median alone.
    ptp_local = float("nan")
    ok = np.isfinite(resid)
    if int(ok.sum()) > 4:
        # Times and residuals from the SAME per-pulse arrays. Not the estimator's own
        # matched set, which is a different (tighter) selection and a different length.
        t = t_rec[ok]
        r = resid[ok]
        bins = np.floor((t - t.min()) / 20.0).astype(int)
        meds = [float(np.median(r[bins == b])) for b in np.unique(bins) if (bins == b).sum() >= 3]
        if len(meds) > 1:
            ptp_local = max(meds) - min(meds)

    return [
        SIDECAR_MAGIC,
        f"#format_version={SIDECAR_FORMAT_VERSION}",
        f"#created_utc={created}",
        f"#log={log_path.name}",
        f"#log_sha256={_sha256(log_path)}",
        f"#recording={rec_path.name}",
        f"#recording_sha256={_sha256(rec_path)}",
        f"#recording_rate_hz={rate}",
        f"#recording_frames={n_frames}",
        f"#recording_channel={channel}",
        "#model=linear2",
        f"#scale={a.scale:.12g}",
        f"#drift_ppm={a.drift_ppm:.6g}",
        f"#offset_s={a.offset_s:.9g}",
        f"#method={AlignmentMethod.AUTO_MATCHED_FILTER.value}",
        f"#coarse_lag_s={result.coarse_lag_s:.9g}",
        f"#coarse_peak_ratio={result.coarse_peak_ratio:.6g}",
        f"#n_log_pulses={result.n_candidates}",
        f"#n_detections={n_det}",
        f"#n_matched={n_matched}",
        f"#match_fraction={n_matched / result.n_candidates if result.n_candidates else 0.0:.6g}",
        f"#match_tolerance_s={tolerance_s:.6g}",
        f"#fit_match_fraction={result.match_fraction:.6g}",
        f"#residual_median_s={_num(float(np.median(finite)) if finite.size else float('nan'))}",
        f"#residual_mad_s={_num(float(np.median(np.abs(finite - np.median(finite)))) if finite.size else float('nan'))}",
        f"#residual_p95_abs_s={_num(float(np.percentile(np.abs(finite), 95)) if finite.size else float('nan'))}",
        f"#residual_slope_ppm={_num(a.residual_slope_ppm)}",
        f"#residual_ptp_local_s={_num(ptp_local)}",
        f"#detect_snr_threshold={params.snr_threshold:.6g}",
        f"#detect_absolute_floor={params.absolute_floor:.6g}",
        f"#detect_refractory_s={params.refractory_s:.6g}",
        f"#validated={1 if passed else 0}",
        f"#validation_warnings={'|'.join(reasons) if reasons else ''}",
        f"#fit_warnings={'|'.join(result.warnings) if result.warnings else ''}",
    ]


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
    log_path: Annotated[Path, typer.Argument(help="A PULSnnnn.CSV pulse log.")],
    recording: Annotated[Path, typer.Argument(help="The WAV recorded during that session.")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Sidecar CSV to write. Required.")],
    channel: Annotated[
        int, typer.Option("--channel", "-c", help="Recording channel carrying the playback.")
    ] = 0,
    tolerance_ms: Annotated[
        float, typer.Option("--tolerance-ms", help="Match tolerance for the status column.")
    ] = DEFAULT_MATCH_TOLERANCE_S * 1e3,
    detections_out: Annotated[
        Optional[Path],
        typer.Option("--detections-out", help="Also write every detection, matched or not."),
    ] = None,
    allow_unvalidated: Annotated[
        bool, typer.Option("--allow-unvalidated", help="Write even if the fit fails validate().")
    ] = False,
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 2,
) -> None:
    """Fit log -> recording time and write the sidecar.

    Exits non-zero if the fit fails ``validate()``, unless ``--allow-unvalidated``.
    That is deliberate: an unvalidated alignment that reaches a viewer looks
    exactly like a good one, and every downstream conclusion inherits it.
    """
    configure_logging(verbose)
    if out.exists():
        raise typer.BadParameter(f"{out} exists; refusing to overwrite")

    log_file = read(log_path)
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
    # Close the loop on the template: the recorded pulse is a differentiated
    # EOD, so the emitted shape is a seed and nothing more.
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

    created = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = _header_lines(
        log_path=log_path, rec_path=recording, channel=channel, rate=rate, n_frames=n_frames,
        result=result, passed=passed, reasons=reasons, n_det=int(found.n), n_matched=n_matched,
        tolerance_s=tol, params=params, resid=cols["resid_s"],
        t_rec=cols["t_rec_s"], created=created,
    )

    recs = log_file.pulses()
    seq = np.fromiter((r.seq for r in recs), dtype=np.int64, count=len(recs))
    tick = np.fromiter((r.tick for r in recs), dtype=np.int64, count=len(recs))
    frame = pl.DataFrame(
        {
            "seq": seq,
            "tick": tick,
            "event": [r.event for r in recs],
            # None, not a number: an absent trial must render as an empty column.
            "trial": pl.Series([r.trial for r in recs], dtype=pl.Int64),
            "t_log_s": _round(cols["t_log_s"], 6),
            "t_rec_s": _round(cols["t_rec_s"], 6),
            "offset_s": _round(cols["t_rec_s"] - cols["t_log_s"], 6),
            # NaN would be written as "NaN"; polars writes a null as an empty field,
            # which is the pulse log's own absent-is-empty rule and for the same
            # reason -- a written number gets used as one.
            "t_det_s": _nullable(cols["t_det_s"], 6),
            "resid_s": _nullable(cols["resid_s"], 9),
            "status": cols["status"].tolist(),
        }
    )
    _write_csv(out, header, frame)
    log.info("wrote %s (%d rows)", out, frame.height)

    if detections_out is not None:
        _write_detections(detections_out, found, cols, result, seq)


def _round(a: NDArray[np.float64], places: int) -> NDArray[np.float64]:
    """Round for output. Done here rather than with a per-row format string so the
    whole column is one vectorised operation."""
    return np.round(a, places)


def _nullable(a: NDArray[np.float64], places: int) -> pl.Series:
    """A float column whose non-finite entries become nulls, i.e. empty CSV fields."""
    rounded = np.round(a, places)
    return pl.Series(rounded).fill_nan(None)


def _write_csv(path: Path, header: list[str], frame: pl.DataFrame) -> None:
    """The '#key=value' block, the commented column row, then polars' own CSV.

    The commented column line mirrors the pulse log's layout so
    ``pl.read_csv(path, comment_prefix='#')`` works directly on either file.
    """
    with path.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(header) + "\n")
        fh.write("#" + ",".join(frame.columns) + "\n")
        frame.write_csv(fh)


def _write_detections(path: Path, found, cols, result, seq: NDArray[np.int64]) -> None:
    """Every detection, and whether the log accounts for it.

    The ones it does not are the point: the recording carries the animal's
    response as well as the stimulus, so a detection with no logged pulse behind
    it is a candidate response rather than an error. It is labelled
    ``unexplained`` and not ``spurious`` on purpose.
    """
    if path.exists():
        raise typer.BadParameter(f"{path} exists; refusing to overwrite")
    claimed = cols["claimed"]
    inv = result.alignment.recording_to_log(found.times_s)
    matched_seq = np.where(claimed >= 0, seq[np.clip(claimed, 0, seq.size - 1)], -1)
    frame = pl.DataFrame(
        {
            "index": np.arange(found.times_s.size, dtype=np.int64),
            "t_rec_s": _round(found.times_s, 6),
            "t_log_s": _round(inv, 6),
            "amplitude": (
                _round(np.asarray(found.amplitudes, dtype=np.float64), 6)
                if found.amplitudes is not None
                else np.full(found.times_s.size, np.nan)
            ),
            "matched_seq": pl.Series(matched_seq, dtype=pl.Int64),
            "status": np.where(claimed >= 0, "explained", "unexplained").tolist(),
        }
    ).with_columns(
        # An unmatched detection has no seq. Null, not -1: the sidecar inherits the
        # pulse log's rule that an absent field is an empty column, never a number
        # someone can index with.
        pl.when(pl.col("matched_seq") < 0).then(None).otherwise(pl.col("matched_seq"))
        .alias("matched_seq")
    )
    _write_csv(
        path,
        [SIDECAR_MAGIC + "-detections", f"#format_version={SIDECAR_FORMAT_VERSION}"],
        frame,
    )
    n_un = int(np.count_nonzero(claimed < 0))
    log.info("wrote %s (%d detections, %d unexplained by the log)", path, found.n, n_un)


if __name__ == "__main__":  # pragma: no cover
    app()
