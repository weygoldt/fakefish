"""Render the eel stimulus library (+ a demo song) to SD-card WAV files.

**Full-SD playback architecture.** Every stimulus is a pre-rendered mono ``int16`` @
50 kHz WAV on the Teensy 4.1's SD card, organised **one directory per program button**.
The firmware lists a button's directory, picks a WAV at random, applies a per-press
random **polarity flip** (anti-pitting) and a single ``MASTER_GAIN``, and streams it to
``out_write()``. All stimulus *generation* is this Python; the firmware just plays files.

Card layout (one dir per button; ``E`` is left unused)::

    /A/  calibration  : one 10 s 10 kHz sine tone
    /B/  localization : one WAV per loc item    = marker -> gap -> loc (20 s window)  @ LOC
    /C/  volley       : one WAV per volley item  = marker -> gap -> volley             @ VOLLEY
    /D/  loc->volley  : loc->volley sessions     = marker -> gap -> loc(5 s) -> gap -> volley
    /F/  song         : a supplied WAV (data/rickroll.wav, → mono/50 kHz) or a synth melody

Two fidelity rules make these WAVs a faithful migration of the on-device sessions
(``firmware/eel_fakefish/{eel_control.h,eel_fakefish.ino,eel_player.cpp}``):

1. **Absolute levels are baked in, NOT peak-normalised.** The loc/volley/marker level
   ratios (0.45 / 0.90 / 0.25 of full scale) must survive, so each item is scaled by its
   level fraction and clamped to +/-32767 exactly as the firmware's ``clamp16`` does.
2. **Polarity is NOT baked** (WAVs are rendered at polarity +1); the firmware applies the
   per-press +/-1 sign flip so the eel EOD, which is net-DC/monophasic, never pits the
   electrodes by always-same-polarity electrolysis.

The level constants and session structure are NOT declared here: they come from
``shared/stim_constants.json`` via the generated ``fakefish._constants``, which is also
what generates the firmware's ``stim_levels.h``. One file feeds both languages, and a
pytest guard fails if either generated file is stale — so the card, the galleries and the
sketches cannot drift apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import typer

from fakefish import _constants as K
from fakefish import _resources as _res
from fakefish import export_teensy_stimuli as ex
from fakefish.viz.loggers import configure_logging, get_logger

log = get_logger(__name__)
app = typer.Typer(add_completion=False, help="Render the stimulus library to SD-card WAVs.")

# ===== Playback constants — SINGLE-SOURCED, do not edit here ==========================
# Every value below comes from ``shared/stim_constants.json`` via the generated
# ``fakefish._constants`` — the same file the firmware header ``stim_levels.h`` is
# generated from. To change one, edit that JSON and re-run ``fakefish-gen-constants``;
# a pytest guard fails if the generated files are stale. They are re-bound at module
# level here so ``build_sd_card.RATE_HZ`` and friends keep working for callers/tests.
RATE_HZ = K.RATE_HZ  # 50000

# 10 kHz sine marker: one exact unit-sine cycle at 10 kHz / 50 kHz. Sums to 0 (zero net
# charge). Derived at codegen from round(32767 * sin(2*pi*k/n)).
MARKER_LUT = K.MARKER_LUT
MARKER_RAMP_SAMPLES = K.MARKER_RAMP_SAMPLES  # ~2 ms raised-cosine ramp (anti-click)
MARKER_LEADIN_SAMPLES = K.MARKER_LEADIN_SAMPLES  # 50000  (1 s lead-in before B/C/D)
MARKER_CAL_SAMPLES = K.MARKER_CAL_SAMPLES  # 500000 (10 s calibration tone, program A)

LOC_PLAYBACK_SAMPLES = K.LOC_PLAYBACK_SAMPLES  # 1_000_000  (B: 20 s localization window)
D_LOC_PLAYBACK_SAMPLES = K.D_LOC_PLAYBACK_SAMPLES  # 250_000   (D: 5 s localization lead)
D_INTERPHASE_GAP_SAMPLES = K.D_INTERPHASE_GAP_SAMPLES  # 15_000 (D: 300 ms loc->volley gap)

# Absolute output levels, as a fraction of full scale — baked into the WAVs rather than
# peak-normalised, because the level RATIOS are the experiment. These are the DEFAULTS
# that reproduce today's stimuli exactly; the CLI accepts mV overrides.
VOLLEY_AMPLITUDE = K.VOLLEY_AMPLITUDE  # C, and D's strike
LOC_AMPLITUDE = K.LOC_AMPLITUDE  # B, and D's lead-in localization
MARKER_AMPLITUDE = K.MARKER_AMPLITUDE  # 1 s lead-in tone (B/C/D)
MARKER_CAL_AMPLITUDE = K.MARKER_CAL_AMPLITUDE  # 10 s calibration tone (A)

# ===== Nominal mV <-> amplitude map ===================================================
# V_peak ~= 3.3 V * amplitude * LUT_peak/32640. A pulse peaks at 32767, so its
# open-circuit peak at full scale is ~3.3 * 32767/32640 ~= 3.313 V == 3313 mV.
# NOTE: this is a NOMINAL, open-circuit source level, NOT a calibrated field strength — in
# water the source impedance divides against the electrode/water load and the delivered
# level is lower and conductivity-dependent. Treat mV as "operator-set nominal source
# level", never as a measured field amplitude. (On the 36 V DRV8871 stage the absolute
# volts differ; this map is the historical nominal used to author the card.)
FULLSCALE_PULSE_PEAK_MV = K.FULLSCALE_PULSE_PEAK_MV


def mv_to_amplitude(mv: float) -> float:
    """Nominal source mV (open-circuit pulse peak) -> amplitude fraction (clamped 0..1)."""
    return float(np.clip(mv / FULLSCALE_PULSE_PEAK_MV, 0.0, 1.0))


def amplitude_to_mv(amp: float) -> float:
    """Amplitude fraction -> nominal source mV (open-circuit pulse peak)."""
    return float(amp) * FULLSCALE_PULSE_PEAK_MV


# ===== Marker synthesis (faithful port of eel_control.h::marker_sample) ================
def _marker_envelope(t: np.ndarray, total: int, ramp: int) -> np.ndarray:
    """Raised-cosine on/off envelope, vectorised twin of eel_control.h::marker_envelope."""
    env = np.ones(t.shape, dtype=np.float64)
    if ramp == 0 or total < 2 * ramp:
        return env
    rise = t < ramp
    env[rise] = 0.5 * (1.0 - np.cos(np.pi * t[rise] / ramp))
    fall = t >= (total - ramp)
    u = (total - t[fall]).astype(np.float64)  # ramp (at the seam) .. 1 (last sample)
    env[fall] = 0.5 * (1.0 - np.cos(np.pi * u / ramp))
    return env


def render_marker(total: int, amplitude: float, ramp: int = MARKER_RAMP_SAMPLES) -> np.ndarray:
    """One ``total``-sample 10 kHz marker tone as clamped ``int16`` (== marker_sample)."""
    t = np.arange(total)
    v = amplitude * _marker_envelope(t, total, ramp) * MARKER_LUT[t % MARKER_LUT.size]
    return _to_i16_from_units(v)


# ===== Item reconstruction (faithful port of eel_player.cpp windowing/looping) =========
def render_item(
    eod_i16: np.ndarray,
    ipi_samp: np.ndarray,
    rel_amp: Optional[np.ndarray],
    amplitude: float,
    *,
    polarity: int = 1,
    max_samples: int = 0,
    loop: bool = False,
) -> np.ndarray:
    """Reconstruct one library item as a signed float trace (peak per pulse == amplitude).

    Mirrors ``eel_player_start_windowed`` + ``eel_player_next``:

    - ``max_samples == 0`` and ``not loop``: play once, in full (delegates to the validated
      ``export_teensy_stimuli.reconstruct_item`` for byte parity).
    - ``max_samples > 0``: truncate at ``max_samples`` (hard stop, mid-pulse if needed).
    - ``loop`` (and ``n > 1``): when the item plays out before ``max_samples``, replay it
      with the first pulse one ``ipi_samp[1]`` gap after the last (the loop seam keeps the
      cadence). A no-op when one pass already exceeds ``max_samples``.

    Overlaps SUM (float, unclamped); clamping to +/-32767 happens at int16 conversion,
    which reproduces the firmware's per-sample ``clamp16``.
    """
    ipi_samp = np.asarray(ipi_samp, dtype=np.int64)
    n = ipi_samp.size
    if max_samples == 0 and not loop:
        return ex.reconstruct_item(
            eod_i16, ipi_samp, rel_amp, amplitude=amplitude, polarity=polarity, pad_samp=0
        )

    eod = np.asarray(eod_i16, dtype=np.float64) / 32767.0 * float(amplitude) * int(polarity)
    onsets_base = np.cumsum(ipi_samp)  # ipi_samp[0] == 0 -> pulse 0 at t=0
    last_onset_base = int(onsets_base[-1])
    amps = (
        np.ones(n) if rel_amp is None else np.asarray(rel_amp, dtype=np.float64) / 255.0
    )

    total = int(max_samples) if max_samples > 0 else last_onset_base + eod.size
    trace = np.zeros(total, dtype=np.float64)

    do_loop = bool(loop) and n > 1 and max_samples > 0
    period = last_onset_base + int(ipi_samp[1]) if do_loop else 0
    rep = 0
    while True:
        offset = rep * period
        if offset >= total:
            break
        for i in range(n):
            s = offset + int(onsets_base[i])
            if s >= total:
                break  # this rep's remaining pulses start past the window
            e = min(s + eod.size, total)
            trace[s:e] += eod[: e - s] * amps[i]
        if not do_loop:
            break
        rep += 1
    return trace


# ===== int16 conversion (firmware clamp16) ============================================
def _to_i16_from_units(v: np.ndarray) -> np.ndarray:
    """Round + clamp a signal already in int16 units (e.g. the marker) to int16."""
    return np.clip(np.rint(v), -32767, 32767).astype(np.int16)


def _to_i16(trace: np.ndarray) -> np.ndarray:
    """Scale a normalised item trace (peak per pulse == amplitude) to clamped int16."""
    return _to_i16_from_units(trace * 32767.0)


# ===== Session assembly (one press == one WAV) ========================================
@dataclass(slots=True)
class Levels:
    """Absolute output levels (fraction of full scale). Defaults reproduce the firmware."""

    volley: float = VOLLEY_AMPLITUDE
    loc: float = LOC_AMPLITUDE
    marker: float = MARKER_AMPLITUDE
    marker_cal: float = MARKER_CAL_AMPLITUDE


def render_calibration(levels: Levels) -> np.ndarray:
    """Program A: the bare 10 s 10 kHz calibration tone."""
    return render_marker(MARKER_CAL_SAMPLES, levels.marker_cal)


def render_localization(eod, loc_it, gap_samp: int, levels: Levels) -> np.ndarray:
    """Program B: marker -> per-item gap -> localization (20 s window @ LOC level)."""
    marker = render_marker(MARKER_LEADIN_SAMPLES, levels.marker)
    gap = np.zeros(int(gap_samp), dtype=np.int16)
    loc = _to_i16(
        render_item(
            eod, loc_it["ipi_samp"], loc_it["rel_amp"], levels.loc,
            max_samples=LOC_PLAYBACK_SAMPLES, loop=True,
        )
    )
    return np.concatenate([marker, gap, loc])


def render_volley(eod, vol_it, gap_samp: int, levels: Levels) -> np.ndarray:
    """Program C: marker -> per-item gap -> volley (played once, @ VOLLEY level)."""
    marker = render_marker(MARKER_LEADIN_SAMPLES, levels.marker)
    gap = np.zeros(int(gap_samp), dtype=np.int16)
    vol = _to_i16(render_item(eod, vol_it["ipi_samp"], vol_it["rel_amp"], levels.volley))
    return np.concatenate([marker, gap, vol])


def render_loc_volley(eod, loc_it, vol_it, gap_samp: int, levels: Levels) -> np.ndarray:
    """Program D: marker -> gap -> loc(5 s) -> interphase gap -> volley (one polarity)."""
    marker = render_marker(MARKER_LEADIN_SAMPLES, levels.marker)
    gap = np.zeros(int(gap_samp), dtype=np.int16)
    loc = _to_i16(
        render_item(
            eod, loc_it["ipi_samp"], loc_it["rel_amp"], levels.loc,
            max_samples=D_LOC_PLAYBACK_SAMPLES, loop=True,
        )
    )
    interphase = np.zeros(D_INTERPHASE_GAP_SAMPLES, dtype=np.int16)
    vol = _to_i16(render_item(eod, vol_it["ipi_samp"], vol_it["rel_amp"], levels.volley))
    return np.concatenate([marker, gap, loc, interphase, vol])


# ===== Demo song (synthesised melody; no copyrighted audio) ===========================
# "Never Gonna Give You Up" chorus hook, encoded as (semitones-from-A4, beats). Rendered as
# clean tones so the card carries no copyrighted recording — just the tune, for a demo.
_A4_HZ = 440.0
_SONG_HOOK = [
    # "Never gonna give you up"
    (-4, 0.5), (-2, 0.5), (1, 0.75), (-2, 0.75), (5, 1.0), (5, 1.0), (4, 1.5), (None, 0.5),
    # "Never gonna let you down"
    (-4, 0.5), (-2, 0.5), (1, 0.75), (-2, 0.75), (4, 1.0), (4, 1.0), (2, 0.75), (1, 0.25),
    (0, 0.5), (-2, 1.5), (None, 0.5),
]


def synth_song(bpm: float = 114.0, amplitude: float = 0.5) -> np.ndarray:
    """A recognisable demo melody as clean tones (int16). Zero-mean; polarity-flip safe."""
    beat_s = 60.0 / bpm
    ramp = int(0.008 * RATE_HZ)  # 8 ms attack/release (anti-click)
    win = np.ones(1)
    parts = []
    for semis, beats in _SONG_HOOK:
        nsamp = int(beats * beat_s * RATE_HZ)
        if semis is None:
            parts.append(np.zeros(nsamp, dtype=np.float64))
            continue
        freq = _A4_HZ * (2.0 ** (semis / 12.0))
        t = np.arange(nsamp) / RATE_HZ
        tone = np.sin(2.0 * np.pi * freq * t)
        env = np.ones(nsamp)
        r = min(ramp, nsamp // 2)
        if r > 0:
            win = 0.5 * (1.0 - np.cos(np.pi * np.arange(r) / r))
            env[:r] = win
            env[-r:] = win[::-1]
        parts.append(tone * env * amplitude)
    return _to_i16_from_units(np.concatenate(parts) * 32767.0)


def load_wav_song(path: Path, amplitude: float = 0.5) -> np.ndarray:
    """Load an arbitrary WAV as a mono ``int16`` @ 50 kHz song (mode c: a raw waveform).

    Handles any common PCM dtype → float, stereo → mono, resample to the firmware rate, and
    DC-removal + peak-normalisation to ``amplitude`` of full scale. The DC removal matters:
    the song is a *continuous* minutes-long playback (unlike the short pulse stimuli), and
    the firmware's polarity flip is only per-press, so a DC offset would inject net charge
    into the electrodes for the whole song — removing the mean keeps it balanced.
    """
    from math import gcd

    from scipy.io import wavfile
    from scipy.signal import resample_poly

    rate, data = wavfile.read(path)
    x = np.asarray(data, dtype=np.float64)
    if data.dtype == np.uint8:            # 8-bit PCM WAV is UNSIGNED, centred at 128
        x = (x - 128.0) / 128.0
    elif data.dtype == np.int16:
        x = x / 32768.0
    elif data.dtype == np.int32:
        x = x / 2147483648.0
    elif not np.issubdtype(data.dtype, np.floating):
        raise ValueError(f"unsupported WAV dtype for the song: {data.dtype}")
    if x.ndim > 1:                        # stereo / multichannel → mono
        x = x.mean(axis=1)
    if int(rate) != RATE_HZ:              # resample to the firmware playback rate
        g = gcd(int(rate), RATE_HZ)
        x = resample_poly(x, RATE_HZ // g, int(rate) // g)
    x = x - float(np.mean(x))             # remove DC (continuous playback; see docstring)
    peak = float(np.max(np.abs(x))) or 1.0
    x = x / peak * float(amplitude)
    return _to_i16_from_units(x * 32767.0)


# ===== Card builder ===================================================================
@dataclass(slots=True)
class CardConfig:
    """Knobs for the whole card build."""

    levels: Levels = field(default_factory=Levels)
    d_pairings: int = 0  # program-D WAVs: 0 == ALL loc x volley combos; N>0 == N rotated pairings
    song_wav: Optional[Path] = None  # /F source WAV; None -> data/rickroll.wav, else the synth
    song_bpm: float = 114.0
    song_amplitude: float = 0.5


def _dur_s(i16: np.ndarray) -> float:
    return i16.size / RATE_HZ


def build_card(out_dir: Path, firmware: Path, cfg: CardConfig) -> dict:
    """Render the full card into ``out_dir`` and return a manifest dict."""
    parsed = ex.parse_firmware(firmware)
    eod = parsed["EOD_HV"]
    gaps = parsed["lead_gap_samp"]
    items = parsed["items"]
    if gaps is None:
        raise ValueError("firmware has no STIM_LEAD_GAP_SAMP (needs format v3)")

    vol_idx = [i for i, it in enumerate(items)
               if it["kind"] in (ex.STIM_REAL_VOLLEY, ex.STIM_SYNTH_VOLLEY)]
    loc_idx = [i for i, it in enumerate(items) if it["kind"] == ex.STIM_LOCALIZATION]
    if not vol_idx or not loc_idx:
        raise ValueError("library needs at least one volley and one localization item")

    out_dir = Path(out_dir)
    manifest: dict = {
        "rate_hz": RATE_HZ,
        "format": "mono int16 PCM, absolute levels baked, polarity +1 (firmware flips)",
        "levels": {"volley": cfg.levels.volley, "loc": cfg.levels.loc,
                   "marker": cfg.levels.marker, "marker_cal": cfg.levels.marker_cal},
        "levels_nominal_mv": {k: round(amplitude_to_mv(v), 1) for k, v in
                              {"volley": cfg.levels.volley, "loc": cfg.levels.loc,
                               "marker": cfg.levels.marker}.items()},
        "buttons": {},
    }
    total_bytes = 0

    def _emit(button: str, name: str, i16: np.ndarray) -> None:
        nonlocal total_bytes
        d = out_dir / button
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        _write_wav(path, i16)
        total_bytes += i16.nbytes
        manifest["buttons"].setdefault(button, []).append(
            {"file": f"{button}/{name}", "samples": int(i16.size), "dur_s": round(_dur_s(i16), 3)}
        )

    # A: calibration
    _emit("A", "calibration.wav", render_calibration(cfg.levels))

    # B: one localization WAV per loc item
    for i in loc_idx:
        _emit("B", f"loc_{i:02d}.wav", render_localization(eod, items[i], int(gaps[i]), cfg.levels))

    # C: one volley WAV per volley item
    for i in vol_idx:
        _emit("C", f"volley_{i:02d}.wav", render_volley(eod, items[i], int(gaps[i]), cfg.levels))

    # D: loc->volley sessions. Default (d_pairings=0) = EVERY loc x volley combination, so a
    # random pick spans the full pairing space; d_pairings>0 renders N rotated pairings.
    if cfg.d_pairings > 0:
        pairs = [(loc_idx[k % len(loc_idx)], vol_idx[k % len(vol_idx)])
                 for k in range(cfg.d_pairings)]
    else:
        pairs = [(li, vi) for li in loc_idx for vi in vol_idx]
    for li, vi in pairs:
        _emit("D", f"locvol_{vi:02d}_{li:02d}.wav",
              render_loc_volley(eod, items[li], items[vi], int(gaps[li]), cfg.levels))

    # F: the song — a supplied WAV rendered to the card, or the synth fallback if absent.
    song_src = cfg.song_wav if cfg.song_wav is not None else (_res.DATA_DIR / "rickroll.wav")
    if Path(song_src).exists():
        song = load_wav_song(Path(song_src), cfg.song_amplitude)
        log.info("song: %s → %.1f s @ 50 kHz", Path(song_src).name, song.size / RATE_HZ)
    else:
        song = synth_song(cfg.song_bpm, cfg.song_amplitude)
        log.info("song: synthesised fallback (no %s found)", song_src)
    _emit("F", "song.wav", song)

    manifest["total_wavs"] = sum(len(v) for v in manifest["buttons"].values())
    manifest["total_mb"] = round(total_bytes / 1e6, 1)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def _write_wav(path: Path, i16: np.ndarray) -> None:
    from scipy.io import wavfile

    wavfile.write(path, RATE_HZ, np.ascontiguousarray(i16, dtype=np.int16))


# ===== CLI ============================================================================
@app.command()
def build(
    out: Path = typer.Option(..., "--out", "-o", help="SD-card root directory to fill"),
    firmware: Path = typer.Option(_res.DEFAULT_FIRMWARE, "--firmware", "-f"),
    d_pairings: int = typer.Option(0, "--d-pairings", help="program-D WAVs (0 = all loc x volley combos)"),
    song: Optional[Path] = typer.Option(None, "--song", help="song WAV for /F (default: data/rickroll.wav, else synth)"),
    verbose: int = typer.Option(1, "--verbose", "-v", count=True),
) -> None:
    """Render the whole stimulus library (+ song) into an SD-card directory tree."""
    configure_logging(verbose)
    cfg = CardConfig(d_pairings=d_pairings, song_wav=song)
    manifest = build_card(out, firmware, cfg)
    log.info("wrote %d WAVs (%.1f MB) to %s", manifest["total_wavs"], manifest["total_mb"], out)
    for button, files in manifest["buttons"].items():
        log.info("  /%s : %d WAVs", button, len(files))


if __name__ == "__main__":
    app()
