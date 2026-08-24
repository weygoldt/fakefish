"""Generators for synthetic recordings and logs with known ground truth.

ADOPTED from playback-explorer's tests/fixtures/synth.py on 2026-08-24, alongside the
aligner itself (src/fakefish/clock_align.py). Two changes: soundfile swapped for
scipy.io.wavfile, which fakefish already uses, and the malformed-LIST WAV generator
dropped -- it existed for a repair backend that did not come across.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from numpy.typing import NDArray

__all__ = [
    "GroundTruth",
    "biphasic_template",
    "make_aligned_pair",
    "make_pulse_log",
    "make_recording",
]


@dataclass(frozen=True)
class GroundTruth:
    """Everything a test needs to know about a synthetic recording."""

    path: Path
    sample_rate_hz: int
    n_channels: int
    n_frames: int
    pulse_times_s: NDArray[np.float64]
    """Exact peak times of the injected pulses, in seconds."""

    pulse_amplitudes: NDArray[np.float64]
    """Peak amplitude of each injected pulse, as a fraction of full scale."""

    channel: int
    """Channel the pulses were injected into."""

    noise_rms: float
    template_peak_index: int
    """Index of the template's largest-magnitude sample, so a test can convert
    between a detection's reported index and the injected peak time."""

    extra: dict[str, float] = field(default_factory=dict)
    """Method-specific truth, e.g. the injected clock ``scale`` and ``offset_s``."""


def biphasic_template(sample_rate_hz: int, width_s: float = 0.002) -> NDArray[np.float64]:
    """A recorded-looking electric-organ pulse.

    Shaped after the mean pulse measured in the sample recording
    (``docs/RECON.md`` §3.4): a dominant negative lobe followed by a positive
    lobe at about 0.46 of its amplitude, ~2 ms of support. That is what the grid
    sees; the *emitted* ``EOD_HV`` is monophasic, and the difference is exactly
    the point of ``ASSUMPTIONS.md`` A-10.

    Parameters
    ----------
    sample_rate_hz
        Sample rate of the recording the template will be injected into.
    width_s
        Total support in seconds.

    Returns
    -------
    numpy.ndarray
        Peak-normalised to a maximum absolute value of 1.0.
    """
    n = max(round(width_s * sample_rate_hz), 8)
    t = np.linspace(-2.5, 4.5, n)
    # A Gaussian negative lobe plus a wider, weaker positive lobe after it.
    neg = -np.exp(-0.5 * ((t + 0.8) / 0.55) ** 2)
    pos = 0.46 * np.exp(-0.5 * ((t - 0.55) / 0.85) ** 2)
    w = neg + pos
    w -= w.mean()
    return w / np.abs(w).max()


def make_recording(
    path: Path,
    *,
    pulse_times_s: NDArray[np.float64] | list[float],
    duration_s: float = 10.0,
    sample_rate_hz: int = 48_000,
    n_channels: int = 4,
    channel: int = 1,
    amplitude: float | NDArray[np.float64] = 0.2,
    noise_rms: float = 0.002,
    seed: int = 0,
    subtype: str = "PCM_16",
) -> GroundTruth:
    """Write a WAV with pulses at exactly known times.

    The pulse template's *peak* lands on the requested time, so a detector that
    reports peak positions can be compared against ``pulse_times_s`` directly
    with no convention to remember.

    Parameters
    ----------
    path
        Where to write.
    pulse_times_s
        Peak times, seconds from the start of the file.
    duration_s, sample_rate_hz, n_channels
        Shape of the file.
    channel
        Channel to inject into. Other channels get noise only.
    amplitude
        Scalar, or one value per pulse, as a fraction of full scale.
    noise_rms
        Gaussian noise level on every channel, as a fraction of full scale.
        Set to 0 for a noiseless fixture.
    seed
        RNG seed, so a fixture is byte-reproducible.
    subtype
        libsndfile subtype to write.

    Returns
    -------
    GroundTruth
    """
    rng = np.random.default_rng(seed)
    n_frames = round(duration_s * sample_rate_hz)
    times = np.asarray(pulse_times_s, dtype=np.float64)
    amps = np.broadcast_to(np.asarray(amplitude, dtype=np.float64), times.shape).copy()

    x = rng.normal(0.0, noise_rms, size=(n_frames, n_channels)) if noise_rms > 0 else np.zeros(
        (n_frames, n_channels)
    )

    tmpl = biphasic_template(sample_rate_hz)
    peak_i = int(np.argmax(np.abs(tmpl)))
    for t, a in zip(times, amps, strict=True):
        start = round(t * sample_rate_hz) - peak_i
        lo, hi = max(start, 0), min(start + tmpl.size, n_frames)
        if hi <= lo:
            continue
        x[lo:hi, channel] += a * tmpl[lo - start : hi - start]

    # scipy writes the dtype it is handed, so map the subtype explicitly rather than
    # letting float64 through as a 64-bit WAV nothing reads.
    if subtype == "PCM_16":
        data = np.clip(np.rint(x * 32767.0), -32768, 32767).astype(np.int16)
    elif subtype == "PCM_24":
        # scipy has no 24-bit writer; 32-bit int is read back at the same scale and the
        # detection path cannot tell the difference.
        data = np.clip(np.rint(x * 2147483647.0), -2147483648, 2147483647).astype(np.int32)
    elif subtype == "FLOAT":
        data = x.astype(np.float32)
    else:
        raise ValueError(f"unsupported subtype {subtype!r}")
    wavfile.write(str(path), sample_rate_hz, data)
    return GroundTruth(
        path=Path(path),
        sample_rate_hz=sample_rate_hz,
        n_channels=n_channels,
        n_frames=n_frames,
        pulse_times_s=times,
        pulse_amplitudes=amps,
        channel=channel,
        noise_rms=noise_rms,
        template_peak_index=peak_i,
    )


def make_aligned_pair(
    tmp_path: Path,
    *,
    scale: float = 1.0 + 40e-6,
    offset_s: float = 12.345,
    n_pulses: int = 400,
    log_duration_s: float = 300.0,
    recording_duration_s: float = 340.0,
    sample_rate_hz: int = 48_000,
    tick_rate_hz: int = 50_000,
    noise_rms: float = 0.003,
    amplitude: float = 0.15,
    seed: int = 7,
    missing_fraction: float = 0.0,
) -> tuple[Path, Path, GroundTruth]:
    """Build a log and a recording related by a *known* offset and drift.

    The recording contains a pulse at ``scale * t_log + offset_s`` for every
    pulse the log claims, which is precisely the relationship
    :mod:`playback_explorer.core.align` has to recover.

    Parameters
    ----------
    tmp_path
        Directory to write ``synthetic_log.CSV`` and ``synthetic_rec.wav`` into.
    scale
        Injected clock scale. ``1 + 40e-6`` is 40 ppm, the figure fakefish's own
        docs use for this hardware.
    offset_s
        Injected offset, seconds.
    n_pulses
        How many pulses the log claims.
    log_duration_s
        Span of the logged pulse train.
    recording_duration_s
        Length of the recording, which must cover the mapped log span.
    sample_rate_hz, tick_rate_hz
        Recording rate and device tick rate. Deliberately different (48 kHz vs
        50 kHz), as they are in reality.
    noise_rms, amplitude
        Recording noise and pulse level.
    seed
        RNG seed.
    missing_fraction
        Fraction of logged pulses to *omit* from the recording, simulating
        pulses lost below the detection threshold.

    Returns
    -------
    tuple
        ``(log_path, wav_path, ground_truth)``. The ground truth's ``extra``
        carries ``scale`` and ``offset_s``.
    """
    rng = np.random.default_rng(seed)
    # A realistic mixture: sparse localization plus a few dense volleys, since a
    # uniform train would make the cross-correlation artificially easy.
    n_volley = max(n_pulses // 8, 1)
    loc_t = np.sort(rng.uniform(0.0, log_duration_s, n_pulses - n_volley))
    volley_start = rng.uniform(0.0, log_duration_s - 1.0)
    volley_t = volley_start + np.cumsum(rng.uniform(0.0025, 0.004, n_volley))
    t_log = np.sort(np.concatenate([loc_t, volley_t]))

    ticks = np.round(t_log * tick_rate_hz).astype(np.int64)
    ticks = np.unique(ticks)
    t_log = ticks / tick_rate_hz

    t_rec = scale * t_log + offset_s
    keep = np.ones(t_rec.size, dtype=bool)
    if missing_fraction > 0:
        keep[rng.random(t_rec.size) < missing_fraction] = False
    inside = (t_rec >= 0.05) & (t_rec <= recording_duration_s - 0.05)

    wav_path = Path(tmp_path) / "synthetic_rec.wav"
    truth = make_recording(
        wav_path,
        pulse_times_s=t_rec[keep & inside],
        duration_s=recording_duration_s,
        sample_rate_hz=sample_rate_hz,
        n_channels=2,
        channel=0,
        amplitude=amplitude,
        noise_rms=noise_rms,
        seed=seed + 1,
    )

    log_path = Path(tmp_path) / "synthetic_log.CSV"
    make_pulse_log(log_path, ticks=ticks, sample_rate_hz=tick_rate_hz)

    return log_path, wav_path, GroundTruth(
        path=wav_path,
        sample_rate_hz=truth.sample_rate_hz,
        n_channels=truth.n_channels,
        n_frames=truth.n_frames,
        pulse_times_s=t_log,
        pulse_amplitudes=truth.pulse_amplitudes,
        channel=0,
        noise_rms=noise_rms,
        template_peak_index=truth.template_peak_index,
        extra={"scale": scale, "offset_s": offset_s},
    )


_V3_COLUMNS = (
    "seq,tick,event,item,pulse,trial,pol,amp_m,master_m,rand_m,tick_ipi,val,req,res,"
    "ch3_us,ch4_us,ch5_us,ch6_us,zero_us"
)


def make_pulse_log(
    path: Path,
    *,
    ticks: NDArray[np.int64] | list[int],
    sample_rate_hz: int = 50_000,
    rtc_unix: int = 1_787_500_000,
    anchor_period_samp: int = 500_000,
    kind: str = "LOC",
    trial_ticks: NDArray[np.int64] | list[int] | None = None,
    trial_outcomes: list[str] | None = None,
) -> Path:
    """Write a minimal but genuinely valid fakefish v3 pulse log.

    "Valid" means ``fakefish.pulse_log.read`` accepts it -- the fixture is
    checked against the real reader rather than against this app's expectations,
    so a fixture that drifts from the format fails loudly.

    Parameters
    ----------
    path
        Where to write.
    ticks
        Sample ticks of the emitted pulses.
    sample_rate_hz
        Device tick rate, written into the header.
    rtc_unix
        Wall-clock time at tick 0.
    anchor_period_samp
        How often to emit an ``ANCHOR`` row. The default matches the firmware's
        10 s at 50 kHz.
    kind
        Event name for the pulse rows.
    trial_ticks
        Ticks at which to emit ``TRIAL`` rows. ``None`` writes no trials.
    trial_outcomes
        Per-trial resolution, ``"V"`` or ``"S"``. Defaults to alternating,
        starting with a volley. A ``"V"`` trial also gets one ``VOLLEY`` pulse
        row and a ``"S"`` trial gets one ``SHAM`` row, so the file exercises
        both the trial table and the per-pulse table -- a sham emits nothing
        into the water but is still logged, which is the property most worth
        having in a fixture.

    Returns
    -------
    pathlib.Path
        ``path``, for chaining.
    """
    ticks = np.asarray(ticks, dtype=np.int64)
    last_tick = int(ticks[-1]) if ticks.size else 0

    header = [
        "#fakefish-pulse-log",
        "#format_version=3",
        "#file_index=1",
        f"#sample_rate_hz={sample_rate_hz}",
        f"#rtc_unix={rtc_unix}",
        "#rtc_valid=1",
        f"#boot_rtc_unix={rtc_unix}",
        "#stim_format_version=4",
        "#n_stim_items=113",
        f"#anchor_period_samp={anchor_period_samp}",
        "#ring_size=512",
        "#surface=0",
        "#loc_rhythm_fitted=1",
        "#" + _V3_COLUMNS,
        _V3_COLUMNS,
    ]

    def row(seq: int, tick: int | None, event: str, **kw: object) -> str:
        cols = dict.fromkeys(_V3_COLUMNS.split(","), "")
        cols["seq"] = str(seq)
        cols["tick"] = "" if tick is None else str(tick)
        cols["event"] = event
        cols["master_m"] = "1000"
        cols["rand_m"] = "1000"
        for k, v in kw.items():
            cols[k] = "" if v is None else str(v)
        return ",".join(cols[name] for name in _V3_COLUMNS.split(","))

    # Anchors and pulses are interleaved in tick order, exactly as the device
    # writes them; a reader that assumed grouping would pass on a grouped file
    # and fail in the field.
    anchor_ticks = list(range(1, last_tick + anchor_period_samp, anchor_period_samp))
    events: list[tuple[int, str, dict[str, object]]] = [
        (t, "ANCHOR", {"val": rtc_unix + (t - 1) // sample_rate_hz}) for t in anchor_ticks
    ]
    events += [(int(t), kind, {"pol": 1, "amp_m": 250, "tick_ipi": 15_850}) for t in ticks]

    if trial_ticks is not None:
        tt = [int(t) for t in np.asarray(trial_ticks, dtype=np.int64)]
        outcomes = trial_outcomes or ["V" if i % 2 == 0 else "S" for i in range(len(tt))]
        for i, (t, res) in enumerate(zip(tt, outcomes, strict=True)):
            events.append((t, "TRIAL", {"trial": i, "req": "R", "res": res}))
            if res == "V":
                events.append(
                    (t + 50, "VOLLEY", {"trial": i, "item": 7, "pulse": 0,
                                        "pol": 1, "amp_m": 900})
                )
            else:
                events.append((t, "SHAM", {"trial": i}))

    events.sort(key=lambda e: e[0])

    lines = [*header, row(0, 0, "BOOT")]
    for i, (tick, event, kw) in enumerate(events, start=1):
        lines.append(row(i, tick, event, **kw))

    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Path(path)
