"""Export electric-eel playback stimuli as a compile-time C header for a Teensy.

The stimulus a Teensy "fakefish" (3.5 or 4.1) plays into water through a dipole is
built from **a mean EOD waveform** (the shape of one pulse) and **pulse-time
sequences** (the timing), plus an optional **per-pulse relative voltage envelope**
(the physiological rise/fall of a discharge — e.g. the synthetic volleys' onset
ramp). What is *deliberately excluded* is the *recorded* pulse amplitude: it
confounds the true EOD amplitude with the fish's distance/orientation, so
replaying it would replay the fish's trajectory, not its discharge. The absolute
amplitude is a firmware-side global scale (the relative envelope rides on top of
it); polarity is a firmware concern too.

This tool reads the source recording NIX outputs (production workspace) and writes:

* ``firmware/eel_fakefish/eel_stimuli.h`` / ``.cpp`` — the generated arrays (into the sketch).
* ``data/stimuli_qc.pdf``       — a multi-panel QC figure (see ``qc.py``-style panels here).
* ``data/stimuli_provenance.json`` — full provenance of every selected scene.
* ``data/multifish_volley_candidates.csv`` — rejected overlapping-volley scenes.

Two subcommands (run from the repo root; needs the source dataset, not shipped)::

    fakefish-export scan   --config data/stimuli_config.yaml
    fakefish-export export --config data/stimuli_config.yaml

``scan`` mines every configured recording for candidate single-fish volleys and
localization windows, scores each on three independent single-fish axes
(temporal CV2, serial autocorrelation, spatial amplitude-vector clustering), and
writes a candidate manifest for human confirmation. ``export`` takes the
confirmed picks (from the config, or the top auto-ranked candidates) and emits
the firmware + QC + provenance.

The waveform is the global cross-recording alignment template
(``global_pulse_alignment_eel_eod.json``), not per-scene cutouts — it is the mean
EOD shape across the whole dataset, with its flanks intact.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
import math
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import typer
import yaml
from scipy.signal import resample_poly

from fakefish import _resources as _res
from fakefish.viz.loggers import configure_logging, get_logger

log = get_logger(__name__)

app = typer.Typer(
    add_completion=False,
    help="Export electric-eel playback stimuli (waveform + IPI timing) as a Teensy C header.",
)

# --------------------------------------------------------------------------
# Constants / schema strings (read block names from the file; production data
# carries both a legacy `Volleys_*` and the current `Hunting_*` block — we use
# the current Hunting block as the volley seed).
# --------------------------------------------------------------------------

DEFAULT_SAMPLE_RATE_HZ = 48000.0
PLAYBACK_RATE_HZ = 50000
# v3 adds STIM_LEAD_GAP_SAMP[] — the per-item deterministic lead-in gap (samples of
# silence between the sine marker and the item's first pulse). The EOD waveform
# and the StimItem table are byte-identical to v2; v3 is a strict superset.
STIM_FORMAT_VERSION = 3

# Per-item lead-in gap: each library item gets its OWN randomly-chosen-but-then-fixed
# offset between the sine marker's end and its first pulse, drawn once here (deterministic,
# seeded off cfg.seed) and baked into the header + provenance. Downstream, the marker's
# offset in the recording plus this known per-item gap synchronises and helps identify
# which item played. A fixed substream keeps the gap draw independent of any other RNG use.
LEAD_GAP_RNG_SUBSTREAM = 0xEE15  # "eel sine" — arbitrary fixed salt for reproducibility

PULSES_EEL_BLOCK = "pulses_eel_eod"
HUNTING_EEL_BLOCK = "Hunting_eel_eod"
POSITION_PRED_EEL_BLOCK = "Position estimation predictions_eel_eod"
TRACKING_BLOCK = "Tracking"
PULSES_METADATA_SECTION = "pulses_metadata"

ALIGNMENT_TEMPLATE_KEY = "template"
ALIGNMENT_FILENAME = "global_pulse_alignment_eel_eod.json"


# ==========================================================================
# Config
# ==========================================================================


@dataclass(slots=True)
class Config:
    """Reproducible export configuration (loaded from ``stimuli_config.yaml``)."""

    eods_root: Path
    alignment_json: Optional[Path]
    firmware_dir: Path
    out_dir: Path
    sites: list[str]

    playback_hz: int = PLAYBACK_RATE_HZ
    original_hz_fallback: float = DEFAULT_SAMPLE_RATE_HZ

    # waveform processing
    trim_frac: float = 0.005  # trim to where |w| first/last exceeds this * peak
    flank_frac: float = 0.12  # fraction of length each side used as baseline
    window_type: str = "hann"  # taper drawing the flanks smoothly to 0: hann|tukey|none
    window_tukey_alpha: float = 0.5  # flat-top fraction for the tukey taper

    # volley detection (step 1 — the shape detector)
    volley_min_pulses: int = 8
    volley_max_ipi_ms: float = 25.0  # antimode of the volley/localization IPI bimodal
    volley_peak_in_first_frac: float = 0.5  # peak rate must fall in the first half
    # every EXPORTED volley must reach a peak instantaneous rate above this (i.e.
    # a min IPI below 1000/volley_min_peak_hz ms) — the "is it actually a volley,
    # not a fast localization train or a truncated fragment" gate. Applied both at
    # scene selection (pick a >100 Hz volley) and as a hard emit-time backstop that
    # drops any volley-family item that never gets there. 100 Hz == IPI < 10 ms.
    volley_min_peak_hz: float = 100.0
    # ... and no HIGHER than this: a single Electrophorus volley peaks at a few hundred
    # Hz, so a real train peaking above ~400 Hz is two fish volleying together (rates
    # add) — a multi-fish artifact, not a single-fish stimulus. Gates real-volley
    # selection (the synthetic model caps itself, see synthetic_volleys.py).
    volley_max_peak_hz: float = 400.0

    # purity (step 2). CV2 + lag-1 are computed and PLOTTED as diagnostics, but
    # real eel volleys are intrinsically irregular at the ms scale (CV2 ~ 1 for a
    # verified single fish) and hunting fish MOVE (amplitude vector drifts), so
    # the verdict is driven by the spatial-TEMPORAL interleaving of any second
    # cluster (2 concurrent fish interleave; one moving fish is time-segregated)
    # plus whether another tracker track is concurrently active.
    cv2_report_only: bool = True  # keep CV2/lag-1 as diagnostics, not hard gates
    cv2_extreme_median: float = (
        0.80  # a single soft guard: median CV2 this high is pathological
    )
    spatial_sil_sep: float = (
        0.45  # silhouette above this => the 2 clusters are genuinely separated
    )
    two_fish_min_frac: float = (
        0.08  # a second cluster at least this large can be a real 2nd fish
    )
    interleave_thresh: float = (
        0.45  # minority runs/count above this => temporally interleaved => 2 fish
    )
    max_purify_frac: float = 0.02  # sprinkled contaminant removal cap
    concurrent_margin_s: float = 2.0  # window padding when counting other active tracks
    concurrent_min_pulses: int = (
        5  # an "active" other track needs at least this many pulses in-window
    )
    gate_concurrent_tracks: bool = (
        False  # HARD-reject on any nearby other track (strict; off by default —
    )
    # a fish localizing elsewhere on its own track does not contaminate
    # the target's IPI; reported as a diagnostic either way)
    max_concurrent_tracks: int = 0  # used only when gate_concurrent_tracks is True
    overlapping_volley_margin_s: float = (
        1.0  # two Hunting events this close overlap => multi-fish volley scene
    )

    # selection
    n_volley_exemplars: int = 5
    n_localization_exemplars: int = 3
    localization_window_s: float = 60.0
    localization_ipi_ms_lo: float = 60.0
    localization_ipi_ms_hi: float = 1000.0
    # localization multi-fish audit. NOTE: a low-IPI *sprinkle* alone is NOT a
    # reliable 2nd-fish cue — a single resting/exploring eel's localization IPI is
    # intrinsically wide (~6-8 % of intervals fall below 50 ms even for one fish; the
    # fitted single-fish model reproduces this). The reliable "2nd fish in the scene"
    # signal is a concurrently-active OTHER track, so localization exemplars are gated
    # to at most localization_max_concurrent_tracks (default 0 = no other fish active).
    # localization_low_ipi_ms is kept only to REPORT the low-IPI fraction per pick as a
    # diagnostic (written to provenance), not to gate.
    localization_max_concurrent_tracks: int = 0
    localization_low_ipi_ms: float = 50.0
    seed: int = 0

    # ---- Sine-marker lead-in gap (see LEAD_GAP_RNG_SUBSTREAM above) ----------
    # Each VOLLEY/LOCALIZATION playback is preceded by a ~1 s sine marker; a short
    # silent gap then separates the marker from the item's first pulse. The gap is drawn
    # once PER ITEM in [lead_gap_ms_lo, lead_gap_ms_hi] (uniform, deterministic) and fixed —
    # never re-drawn per playback — so downstream can key off the marker offset + the item's
    # known gap. Jittered ACROSS items (weak per-item fingerprint), fixed WITHIN an item.
    lead_gap_ms_lo: float = 50.0
    lead_gap_ms_hi: float = 200.0

    # explicit picks (empty => auto top-N). Each item: {recording, event_id}.
    volley_picks: list[dict] = field(default_factory=list)
    localization_picks: list[dict] = field(default_factory=list)

    # include the synthetic volley/localization population in the exported library
    include_synthetic: bool = True
    # localization exemplars are trimmed to their longest run with no gap beyond
    # this (a silence >1.31 s can't fit the uint16 sample-IPI and isn't "steady")
    localization_max_gap_s: float = 1.2

    # flash budget (128 kB — trivial on the 3.5's 512 kB / 4.1's 8 MB)
    max_flash_bytes: int = 131072

    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        raw = yaml.safe_load(Path(path).read_text())
        paths = raw.get("paths", {})
        scope = raw.get("scope", {})
        wf = raw.get("waveform", {})
        vd = raw.get("volley_detector", {})
        pu = raw.get("purity", {})
        se = raw.get("selection", {})
        bu = raw.get("budget", {})
        sr = raw.get("sample_rate", {})
        mk = raw.get("marker", {})

        eods_root = Path(paths["eods_root"]).expanduser()
        align = paths.get("alignment_json")
        sites = scope.get("sites", "all_lines")
        if sites == "all_lines" or sites is None:
            sites = [
                "line_site_1_2",
                "line_site_2_3",
                "line_site_3_4",
                "line_site_4",
                "line_site_6",
                "line_site_7",
                "line_site_9",
            ]
        return cls(
            eods_root=eods_root,
            alignment_json=Path(align).expanduser() if align else None,
            firmware_dir=Path(
                paths.get("firmware_dir", "firmware/eel_fakefish")
            ).expanduser(),
            out_dir=Path(paths.get("out_dir", "data")).expanduser(),
            sites=list(sites),
            playback_hz=int(sr.get("playback_hz", PLAYBACK_RATE_HZ)),
            original_hz_fallback=float(
                sr.get("original_hz_fallback", DEFAULT_SAMPLE_RATE_HZ)
            ),
            trim_frac=float(wf.get("trim_frac", 0.005)),
            flank_frac=float(wf.get("flank_frac", 0.12)),
            window_type=str(wf.get("window", "hann")),
            window_tukey_alpha=float(wf.get("window_tukey_alpha", 0.5)),
            volley_min_pulses=int(vd.get("min_pulses", 8)),
            volley_max_ipi_ms=float(vd.get("volley_max_ipi_ms", 25.0)),
            volley_peak_in_first_frac=float(vd.get("peak_in_first_frac", 0.5)),
            volley_min_peak_hz=float(vd.get("volley_min_peak_hz", 100.0)),
            volley_max_peak_hz=float(vd.get("volley_max_peak_hz", 400.0)),
            cv2_report_only=bool(pu.get("cv2_report_only", True)),
            cv2_extreme_median=float(pu.get("cv2_extreme_median", 0.80)),
            spatial_sil_sep=float(pu.get("spatial_sil_sep", 0.45)),
            two_fish_min_frac=float(pu.get("two_fish_min_frac", 0.08)),
            interleave_thresh=float(pu.get("interleave_thresh", 0.45)),
            max_purify_frac=float(pu.get("max_purify_frac", 0.02)),
            concurrent_margin_s=float(pu.get("concurrent_margin_s", 2.0)),
            concurrent_min_pulses=int(pu.get("concurrent_min_pulses", 5)),
            gate_concurrent_tracks=bool(pu.get("gate_concurrent_tracks", False)),
            max_concurrent_tracks=int(pu.get("max_concurrent_tracks", 0)),
            overlapping_volley_margin_s=float(
                pu.get("overlapping_volley_margin_s", 1.0)
            ),
            n_volley_exemplars=int(se.get("n_volley_exemplars", 5)),
            n_localization_exemplars=int(se.get("n_localization_exemplars", 3)),
            localization_window_s=float(se.get("localization_window_s", 60.0)),
            localization_ipi_ms_lo=float(se.get("localization_ipi_ms_lo", 60.0)),
            localization_ipi_ms_hi=float(se.get("localization_ipi_ms_hi", 1000.0)),
            localization_max_concurrent_tracks=int(
                se.get("localization_max_concurrent_tracks", 0)
            ),
            localization_low_ipi_ms=float(se.get("localization_low_ipi_ms", 50.0)),
            seed=int(se.get("seed", 0)),
            lead_gap_ms_lo=float(mk.get("lead_gap_ms_lo", 50.0)),
            lead_gap_ms_hi=float(mk.get("lead_gap_ms_hi", 200.0)),
            volley_picks=list(se.get("volley_picks", [])),
            localization_picks=list(se.get("localization_picks", [])),
            include_synthetic=bool(se.get("include_synthetic", True)),
            localization_max_gap_s=float(se.get("localization_max_gap_s", 1.2)),
            max_flash_bytes=int(bu.get("max_flash_bytes", 131072)),
        )

    def resolve_alignment_json(self) -> Path:
        if self.alignment_json is not None:
            return self.alignment_json
        return self.eods_root / ALIGNMENT_FILENAME


# ==========================================================================
# Waveform pipeline
# ==========================================================================


@dataclass(slots=True)
class Waveform:
    """A processed, quantised playback waveform plus its shape metrics."""

    samples_i16: np.ndarray  # int16, |peak| == 32767, endpoints == 0
    samples_norm: np.ndarray  # float, |peak| == 1 (pre-quantisation, resampled)
    pre_resample_norm: np.ndarray  # float, |peak| == 1, at original_hz (for QC overlay)
    playback_hz: int
    original_hz: float
    fwhm_us: float
    duration_us: float
    peak_to_peak_norm: float
    pos_area: float
    neg_area: float
    net_integral: float  # signed area (normalised units * samples)
    pos_neg_area_ratio: float

    @property
    def n(self) -> int:
        return int(self.samples_i16.size)


def load_global_template(alignment_json: Path) -> tuple[np.ndarray, float]:
    """Load the signed global mean EOD template and its native sample rate.

    Returns ``(template, original_hz)``. ``template`` is the signed
    cross-recording mean waveform (peak-normalised, flanks intact); the
    alignment file does not itself store a sample rate, so we return the
    dataset's recording rate (48 kHz) which the cutouts — and therefore the
    template's sample axis — are defined on.
    """
    d = json.loads(Path(alignment_json).read_text())
    template = np.asarray(d[ALIGNMENT_TEMPLATE_KEY], dtype=np.float64)
    return template, DEFAULT_SAMPLE_RATE_HZ


def _fwhm_samples(w: np.ndarray) -> float:
    """Full width at half maximum of |w| about its peak, in samples (interp)."""
    a = np.abs(w)
    pk = int(np.argmax(a))
    half = a[pk] / 2.0
    # left crossing
    left = pk
    while left > 0 and a[left] > half:
        left -= 1
    right = pk
    while right < a.size - 1 and a[right] > half:
        right += 1

    def _interp(i_lo: int, i_hi: int) -> float:
        y_lo, y_hi = a[i_lo], a[i_hi]
        if y_hi == y_lo:
            return float(i_lo)
        return i_lo + (half - y_lo) / (y_hi - y_lo)

    xl = _interp(left, min(left + 1, a.size - 1))
    xr = _interp(right, max(right - 1, 0))
    return float(abs(xr - xl))


def build_waveform(template: np.ndarray, original_hz: float, cfg: Config) -> Waveform:
    """Turn the raw global template into the quantised playback waveform.

    Steps (in order): baseline-correct on the flanks, centre on the peak, trim to
    the 0.5%-of-peak support and pad symmetrically, resample to the playback
    rate, normalise ``|peak| = 1``, force the endpoints to exactly 0, quantise to
    int16 with ``|peak| == 32767``.
    """
    w = np.asarray(template, dtype=np.float64).copy()

    # 1. baseline-correct using the flanks.
    k = max(1, int(round(cfg.flank_frac * w.size)))
    baseline = float(np.mean(np.concatenate([w[:k], w[-k:]])))
    w = w - baseline

    # 1b. taper the flanks smoothly to exactly zero with a peak-centred window.
    #     The eel EOD template carries a broad ~1 %-of-peak pre-pulse shoulder;
    #     left as-is it makes the 0.5 % support span the whole cutout (~6 ms) and
    #     produces a sustained near-DC pedestal that clicks/pits on playback. A
    #     Hann taper centred on the peak draws the surrounding baseline to 0
    #     while barely touching the central spike (window ~= 1 there).
    pk = int(np.argmax(np.abs(w)))
    w = w * _centered_window(w.size, pk, cfg.window_type, cfg.window_tukey_alpha)

    # 2. centre on the peak (|.| argmax) and 3. trim to the 0.5% support,
    #    padded symmetrically so the peak stays centred.
    pk = int(np.argmax(np.abs(w)))
    peak_val = np.abs(w[pk])
    thr = cfg.trim_frac * peak_val
    above = np.where(np.abs(w) > thr)[0]
    lo, hi = int(above[0]), int(above[-1])
    half = max(pk - lo, hi - pk)
    lo_s, hi_s = pk - half, pk + half
    # pad with zeros if the symmetric window runs off either end
    left_pad = max(0, -lo_s)
    right_pad = max(0, hi_s - (w.size - 1))
    w = np.concatenate([np.zeros(left_pad), w, np.zeros(right_pad)])
    lo_s += left_pad
    hi_s += left_pad
    w = w[lo_s : hi_s + 1]

    # keep the pre-resample, |peak|=1 waveform for the QC overlay
    pre_resample = w / np.max(np.abs(w))

    # 4. resample to the playback rate via polyphase filtering (not naive interp).
    up, down = _resample_ratio(cfg.playback_hz, original_hz)
    w_rs = resample_poly(w, up, down)

    # 5. normalise |peak| = 1.
    w_rs = w_rs / np.max(np.abs(w_rs))

    # 6. force endpoints to exactly 0 (a non-zero endpoint is a DC click).
    w_rs[0] = 0.0
    w_rs[-1] = 0.0

    # 7. quantise to int16 with |peak| == 32767.
    i16 = _quantise_i16(w_rs)

    fwhm = _fwhm_samples(w_rs)
    pos_area = float(np.sum(w_rs[w_rs > 0]))
    neg_area = float(-np.sum(w_rs[w_rs < 0]))
    net = float(np.sum(w_rs))
    ratio = float(pos_area / neg_area) if neg_area > 0 else math.inf
    return Waveform(
        samples_i16=i16,
        samples_norm=w_rs,
        pre_resample_norm=pre_resample,
        playback_hz=cfg.playback_hz,
        original_hz=original_hz,
        fwhm_us=fwhm / cfg.playback_hz * 1e6,
        duration_us=w_rs.size / cfg.playback_hz * 1e6,
        peak_to_peak_norm=float(np.max(w_rs) - np.min(w_rs)),
        pos_area=pos_area,
        neg_area=neg_area,
        net_integral=net,
        pos_neg_area_ratio=ratio,
    )


def _centered_window(n: int, peak: int, kind: str, tukey_alpha: float) -> np.ndarray:
    """A taper window of length ``n`` whose maximum sits on ``peak``.

    ``hann`` (default) tapers both flanks to exactly 0 with a raised cosine;
    ``tukey`` keeps a flat top (fraction ``1-alpha``) and cosine-tapers only the
    edges (less pulse distortion, still zero endpoints); ``none`` is a no-op.
    The window is built symmetric about ``peak`` and cropped to ``[0, n)`` so an
    off-centre peak still gets a symmetric taper.
    """
    if kind == "none":
        return np.ones(n)
    half = max(peak, n - 1 - peak)
    length = 2 * half + 1
    if kind == "tukey":
        from scipy.signal.windows import tukey

        full = tukey(length, alpha=tukey_alpha, sym=True)
    elif kind == "hann":
        full = np.hanning(length)
    else:
        raise ValueError(f"unknown window type {kind!r} (hann|tukey|none)")
    start = half - peak
    return full[start : start + n]


def _resample_ratio(playback_hz: int, original_hz: float) -> tuple[int, int]:
    """Integer up/down factors for resample_poly (48000 -> 50000 = 25/24)."""
    o = int(round(original_hz))
    g = math.gcd(int(playback_hz), o)
    return playback_hz // g, o // g


def _quantise_i16(w_norm: np.ndarray) -> np.ndarray:
    """Quantise a |peak|==1 float waveform to int16 with |peak| == 32767."""
    i16 = np.round(w_norm * 32767.0).astype(np.int64)
    i16 = np.clip(i16, -32767, 32767)
    # guarantee the peak reaches exactly +/-32767 after rounding
    pk = int(np.argmax(np.abs(w_norm)))
    i16[pk] = int(np.sign(w_norm[pk]) * 32767) if w_norm[pk] != 0 else i16[pk]
    i16[0] = 0
    i16[-1] = 0
    return i16.astype(np.int16)


# ==========================================================================
# IPI sequences
# ==========================================================================


def ipi_us_from_times(times_s: np.ndarray) -> np.ndarray:
    """Convert absolute pulse times (s) to the wait-before-pulse micros array.

    ``IPI_US[0] == 0`` (nothing to wait before the first pulse); ``IPI_US[i]`` is
    the microseconds to wait before emitting pulse ``i``.
    """
    times_s = np.asarray(times_s, dtype=np.float64)
    d = np.diff(times_s)
    ipi = np.concatenate([[0.0], d * 1e6])
    return np.round(ipi).astype(np.uint32)


def onsets_samples(ipi_us: np.ndarray, playback_hz: int) -> np.ndarray:
    """Cumulative pulse-onset sample indices at the playback rate from an IPI train."""
    cum_us = np.cumsum(np.asarray(ipi_us, dtype=np.float64))
    return np.round(cum_us * 1e-6 * playback_hz).astype(np.int64)


def reconstruct_trace(
    eod_i16: np.ndarray,
    ipi_us: np.ndarray,
    playback_hz: int,
    *,
    amplitude: float = 1.0,
    polarity: int = 1,
    pad_us: float = 2000.0,
) -> np.ndarray:
    """Reconstruct the full playback trace the Teensy emits for one IPI train.

    The EOD waveform is placed (additively) at each pulse onset, scaled by
    ``amplitude`` and multiplied by ``polarity`` (``+1`` / ``-1``, held constant
    for a playback). Returns a float array at ``playback_hz``. This is exactly
    what :mod:`render_stimulus` produces and what the DAC-reconstruction QC panel
    draws — the single source of truth for "what goes into the water".
    """
    eod = (
        np.asarray(eod_i16, dtype=np.float64)
        / 32767.0
        * float(amplitude)
        * int(polarity)
    )
    onsets = onsets_samples(ipi_us, playback_hz)
    pad = int(round(pad_us * 1e-6 * playback_hz))
    total = int(onsets[-1]) + eod.size + pad
    trace = np.zeros(total, dtype=np.float64)
    for o in onsets:
        s = int(o) + pad
        trace[s : s + eod.size] += eod
    return trace


def split_dac(trace: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Split a signed trace into the two unipolar DAC channels.

    ``DAC_A = max(w, 0)``, ``DAC_B = max(-w, 0)`` — the firmware's sign split.
    """
    return np.maximum(trace, 0.0), np.maximum(-trace, 0.0)


def reconstruct_item(
    eod_i16: np.ndarray,
    ipi_samp: np.ndarray,
    rel_amp: Optional[np.ndarray],
    *,
    amplitude: float = 1.0,
    polarity: int = 1,
    pad_samp: int = 100,
) -> np.ndarray:
    """Reference additive-mixer engine (host twin of the firmware playback loop).

    Places the EOD at each pulse onset (cumulative sum of the sample-IPIs),
    scaled by the per-pulse relative amplitude (``rel_amp[i]/255`` or 1.0), a
    global ``amplitude``, and a constant ``polarity``; overlapping pulses SUM.
    The C engine on each Teensy must reproduce this sample-for-sample.
    """
    eod = (
        np.asarray(eod_i16, dtype=np.float64)
        / 32767.0
        * float(amplitude)
        * int(polarity)
    )
    ipi_samp = np.asarray(ipi_samp, dtype=np.int64)
    onsets = np.cumsum(ipi_samp)
    if rel_amp is None:
        amps = np.ones(ipi_samp.size)
    else:
        amps = np.asarray(rel_amp, dtype=np.float64) / 255.0
    total = int(onsets[-1]) + eod.size + pad_samp
    trace = np.zeros(total, dtype=np.float64)
    for o, a in zip(onsets, amps, strict=True):
        s = int(o) + pad_samp
        trace[s : s + eod.size] += eod * a
    return trace


# ==========================================================================
# NIX recording access
# ==========================================================================


@dataclass(slots=True)
class Recording:
    """Lazy accessor for the per-recording arrays we need."""

    path: Path
    site: str
    samplerate_hz: float
    centers: np.ndarray  # (N,) int64 sample centres of eel pulses
    hunting_id: Optional[np.ndarray]  # (N,) int32 volley id per eel pulse, or None
    # joined per-eel-pulse cross-checks (NaN / -1 where the pulse has no row):
    track_id: np.ndarray  # (N,) int32, -1 where unassigned/missing
    center_mu: np.ndarray  # (N, 3) float32 world xyz cm, NaN where missing
    _nix: object = None
    _raw: object = None  # lazy handle to raw_pulses DataArray

    @property
    def times_s(self) -> np.ndarray:
        return self.centers / self.samplerate_hz

    @property
    def recording_id(self) -> str:
        return self.path.stem

    def amplitude_vectors(self, rows: np.ndarray) -> np.ndarray:
        """Per-channel peak-to-peak amplitude vectors for the given pulse rows.

        ``(len(rows), n_channels)``. Computed directly from the raw cutouts (so
        every pulse is covered and no join is needed). Peak-to-peak is used
        rather than ``argmax|amp|`` so the common-average-reference lobe flip
        does not scramble the spatial signature.
        """
        rows = np.asarray(rows)
        r0, r1 = int(rows.min()), int(rows.max())
        block = self._raw[r0 : r1 + 1]  # (r1-r0+1, n_samp, n_ch)
        sub = block[rows - r0]  # (len(rows), n_samp, n_ch)
        return sub.max(axis=1) - sub.min(axis=1)  # (len(rows), n_ch)

    def close(self) -> None:
        if self._nix is not None:
            self._nix.close()
            self._nix = None


def open_recording(path: Path, site: str, original_hz_fallback: float) -> Recording:
    """Open a NIX recording and pull the small per-pulse arrays (raw stays lazy)."""
    import nixio

    f = nixio.File.open(str(path), nixio.FileMode.ReadOnly)
    blocks = {b.name: b for b in f.blocks}
    pe = blocks[PULSES_EEL_BLOCK]
    centers = pe.data_arrays["centers"][:]

    samplerate = _read_samplerate(f, original_hz_fallback)

    hunting_id = None
    if HUNTING_EEL_BLOCK in blocks:
        hb = blocks[HUNTING_EEL_BLOCK]
        if "hunting_id" in hb.data_arrays:
            hunting_id = hb.data_arrays["hunting_id"][:]

    n = centers.size
    track_id = np.full(n, -1, dtype=np.int32)
    center_mu = np.full((n, 3), np.nan, dtype=np.float64)

    if POSITION_PRED_EEL_BLOCK in blocks and TRACKING_BLOCK in blocks:
        pp = blocks[POSITION_PRED_EEL_BLOCK]
        tr = blocks[TRACKING_BLOCK]
        pidx = pp.data_arrays["pulse_index"][:]  # sample centres (subset)
        cmu = pp.data_arrays["pred_center_mu_world_xyz"][:]
        tids = tr.data_arrays["track_ids"][:]
        # join subset rows back to eel-pulse rows by matching sample centre
        pos = {int(c): i for i, c in enumerate(centers)}
        for j, c in enumerate(pidx):
            i = pos.get(int(c))
            if i is None:
                continue
            center_mu[i] = cmu[j]
            if j < tids.size:
                track_id[i] = int(tids[j])

    rec = Recording(
        path=path,
        site=site,
        samplerate_hz=samplerate,
        centers=centers,
        hunting_id=hunting_id,
        track_id=track_id,
        center_mu=center_mu,
        _nix=f,
    )
    rec._raw = pe.data_arrays["raw_pulses"]
    return rec


def _read_samplerate(nix_file, fallback: float) -> float:
    for sec in nix_file.sections:
        if sec.name == PULSES_METADATA_SECTION:
            for key in ("pulse_samplerate",):
                if key in [p.name for p in sec.props]:
                    return float(list(sec.props[key].values)[0])
            for sub in sec.sections:
                if sub.name == "metadata" and "samplerate" in [
                    p.name for p in sub.props
                ]:
                    return float(list(sub.props["samplerate"].values)[0])
    return fallback


# ==========================================================================
# Step 1 — candidate volley detection (the gamma-shape rate detector)
# ==========================================================================


@dataclass(slots=True)
class Scene:
    """A candidate volley or localization scene with its purity metrics."""

    kind: str  # "volley" | "localization"
    recording: str
    site: str
    event_id: int  # hunting id (volley) or track id (localization)
    rows: np.ndarray  # eel-pulse row indices (post-purify)
    removed_rows: np.ndarray  # rows purified out (off-cluster contaminants)
    t_start: float
    t_end: float
    n_pulses: int
    mean_amp: float  # mean per-pulse peak-to-peak amplitude (SNR proxy, for ranking)
    # step-1 shape detector
    gamma_score: float
    peak_rate_hz: float
    peak_rate_frac: float  # where in the train the peak rate falls (0..1)
    # step-2 purity (three axes — CV2 + lag-1 reported; verdict driven by the
    # spatial-temporal interleaving + concurrent-track cues, see score_purity)
    cv2_mean: float
    cv2_p95: float
    cv2_max: float
    lag1_autocorr: float
    spatial_dominant_frac: float
    spatial_silhouette: float
    spatial_interleave: float  # runs-of-minority / count: ~1 interleaved (2 fish), ~0 segregated (movement)
    spatial_minority_frac: float
    n_concurrent_tracks: int  # OTHER tracks active in the window ± margin (diagnostic)
    overlapping_volley: (
        bool  # another Hunting event overlaps this one (multi-fish volley scene)
    )
    single_fish: bool
    reject_reason: str

    def to_row(self) -> dict:
        d = dataclasses.asdict(self)
        d["rows"] = self.rows.tolist()
        d["removed_rows"] = self.removed_rows.tolist()
        return d


def _rate_profile(times_s: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Instantaneous rate (1/IPI) per interval and its midpoint times."""
    d = np.diff(times_s)
    d = np.where(d <= 0, np.nan, d)
    rate = 1.0 / d
    mid = 0.5 * (times_s[1:] + times_s[:-1])
    return rate, mid


def gamma_shape_score(times_s: np.ndarray, cfg: Config) -> tuple[float, float, float]:
    """Score how well a train's rate profile matches the volley gamma shape.

    A single eel's volley rate rises to a peak within the first pulses then
    decays monotone-ish back toward the localization rate — an inverted,
    left-leaning gamma. We reward: (a) a sharp onset, (b) the peak rate landing
    in the first ``peak_in_first_frac`` of the train, (c) a monotone-ish decay
    after the peak. Returns ``(score, peak_rate_hz, peak_rate_frac)``.

    This is a *detector* (finds candidates); it survives contamination — two
    eels volleying together still make a smooth gamma at ~double the rate.
    Purity is established separately in :func:`score_purity`.
    """
    rate, _ = _rate_profile(times_s)
    rate = rate[np.isfinite(rate)]
    if rate.size < 3:
        return 0.0, 0.0, 0.0
    peak_i = int(np.argmax(rate))
    peak_rate = float(rate[peak_i])
    frac = peak_i / max(1, rate.size - 1)

    # (b) peak in the first part of the train
    peak_early = (
        1.0
        if frac <= cfg.volley_peak_in_first_frac
        else max(0.0, 1.0 - (frac - cfg.volley_peak_in_first_frac))
    )
    # (c) monotone-ish decay after the peak: fraction of post-peak steps that
    #     do not increase rate (allow small wiggle).
    post = rate[peak_i:]
    if post.size >= 2:
        steps = np.diff(post)
        tol = 0.10 * peak_rate
        decay = float(np.mean(steps <= tol))
    else:
        decay = 0.0
    # (a) sharp onset: peak clearly above the tail median
    tail = rate[max(peak_i + 1, 1) :]
    tail_med = float(np.median(tail)) if tail.size else peak_rate
    onset = float(np.clip((peak_rate - tail_med) / max(peak_rate, 1e-9), 0.0, 1.0))

    score = float(0.4 * peak_early + 0.4 * decay + 0.2 * onset)
    return score, peak_rate, frac


# ==========================================================================
# Step 2 — single-fish purity on three independent axes
# ==========================================================================


def cv2_series(times_s: np.ndarray) -> np.ndarray:
    """Local coefficient of variation between adjacent IPIs.

    ``CV2_i = 2 |IPI[i+1]-IPI[i]| / (IPI[i+1]+IPI[i])``. Invariant to slow rate
    drift by construction — so it measures *smoothness* of the IPI evolution
    without any template. Single fish with a smooth gamma decay => CV2 ~ 0; a
    superposition of two trains pushes CV2 toward 1.
    """
    ipi = np.diff(np.asarray(times_s, dtype=np.float64))
    ipi = ipi[ipi > 0]
    if ipi.size < 2:
        return np.zeros(0)
    a, b = ipi[:-1], ipi[1:]
    denom = a + b
    return 2.0 * np.abs(b - a) / np.where(denom > 0, denom, np.nan)


def lag1_autocorr_detrended(times_s: np.ndarray) -> float:
    """Lag-1 autocorrelation of the detrended IPI series.

    Interleaving two fish makes short (A->B) intervals tend to be followed by
    long (B->A) ones — a characteristically *negative* lag-1 correlation. A
    single fish shows ~none after detrending the smooth rate ramp.
    """
    ipi = np.diff(np.asarray(times_s, dtype=np.float64))
    ipi = ipi[ipi > 0]
    if ipi.size < 4:
        return 0.0
    # detrend the smooth gamma ramp with a short moving-median.
    win = max(3, (ipi.size // 8) | 1)
    trend = _moving_median(ipi, win)
    resid = ipi - trend
    r = resid[:-1]
    s = resid[1:]
    rs = np.std(r) * np.std(s)
    if rs < 1e-30:
        return 0.0
    return float(np.mean((r - r.mean()) * (s - s.mean())) / rs)


def _moving_median(x: np.ndarray, win: int) -> np.ndarray:
    n = x.size
    half = win // 2
    out = np.empty(n)
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out[i] = np.median(x[lo:hi])
    return out


def spatial_clustering(
    amp_vectors: np.ndarray,
) -> tuple[np.ndarray, float, float, float]:
    """2-means on the L2-normalised across-channel amplitude vectors.

    Returns ``(labels, silhouette, minority_frac, dominant_frac)`` with the rows
    in the order given (assumed time-ordered so the caller can measure temporal
    interleaving of the labels). The normalisation removes overall amplitude
    (distance) so the vector encodes the source's *direction/position*.
    """
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    x = np.asarray(amp_vectors, dtype=np.float64)
    norm = np.linalg.norm(x, axis=1, keepdims=True)
    x = x / np.where(norm > 0, norm, 1.0)
    n = x.shape[0]
    if n < 4:
        return np.zeros(n, dtype=int), 0.0, 0.0, 1.0
    km = KMeans(n_clusters=2, n_init=5, random_state=0).fit(x)
    labels = km.labels_
    counts = np.bincount(labels, minlength=2)
    minority_frac = counts.min() / n
    dominant_frac = counts.max() / n
    try:
        sil = float(silhouette_score(x, labels)) if counts.min() >= 1 else 0.0
    except ValueError:
        sil = 0.0
    return labels, sil, float(minority_frac), float(dominant_frac)


def temporal_interleave(labels: np.ndarray) -> float:
    """How temporally interleaved the minority cluster is, in ``[0, 1]``.

    ``runs_of_minority / count_of_minority``: ~1 when every minority pulse is
    isolated among majority pulses (two fish discharging *concurrently*), ~1/k
    when the minority forms one contiguous block (one fish that *moved* from one
    position to another during the scene). This is the axis that tells a moving
    single fish apart from two co-active fish — a genuinely independent cue from
    the pure spatial split.
    """
    labels = np.asarray(labels)
    if labels.size == 0:
        return 0.0
    counts = np.bincount(labels, minlength=2)
    minority = int(np.argmin(counts))
    m = labels == minority
    n_minority = int(m.sum())
    if n_minority == 0:
        return 0.0
    runs = int(np.sum(np.diff(np.concatenate([[0], m.astype(int)])) == 1))
    return runs / n_minority


def concurrent_track_count(
    rec: Recording, t0: float, t1: float, exclude_tid: int, cfg: Config
) -> int:
    """Number of OTHER tracks with >= ``concurrent_min_pulses`` in the window ± margin.

    Catches the scene the study is about — one volleying eel plus a *concurrent*
    second fish (another volley, or a background localizer) that the tracker put
    on a different track — even when the target track's own IPI train is clean.
    """
    m = cfg.concurrent_margin_s
    times = rec.times_s
    in_win = (times >= t0 - m) & (times <= t1 + m)
    tids = rec.track_id[in_win]
    tids = tids[(tids >= 0) & (tids != exclude_tid)]
    if tids.size == 0:
        return 0
    uniq, cnt = np.unique(tids, return_counts=True)
    return int(np.sum(cnt >= cfg.concurrent_min_pulses))


def _dominant_track(track_ids: np.ndarray) -> int:
    valid = track_ids[track_ids >= 0]
    if valid.size == 0:
        return -1
    uniq, cnt = np.unique(valid, return_counts=True)
    return int(uniq[np.argmax(cnt)])


def purify_scene(
    rows: np.ndarray, amp_vectors: np.ndarray, cfg: Config
) -> tuple[np.ndarray, np.ndarray]:
    """Remove a small, unambiguously off-cluster, *interleaved* contaminant.

    A sprinkled background-localizer pulse is a tiny (<= ``max_purify_frac``),
    genuinely-separated (silhouette), temporally-interleaved minority. A large or
    time-segregated minority is NOT purified — it is either a second fish (reject)
    or the target fish moving (keep). Returns ``(kept_rows, removed_rows)``, rows
    assumed time-ordered.
    """
    rows = np.asarray(rows)
    labels, sil, minority_frac, _ = spatial_clustering(amp_vectors)
    interleave = temporal_interleave(labels)
    counts = np.bincount(labels, minlength=2)
    minority = int(np.argmin(counts))
    if (
        0 < minority_frac <= cfg.max_purify_frac
        and sil >= cfg.spatial_sil_sep
        and interleave >= cfg.interleave_thresh
    ):
        return rows[labels != minority], rows[labels == minority]
    return rows, np.asarray([], dtype=rows.dtype)


def score_purity(rec: Recording, rows: np.ndarray, cfg: Config) -> dict:
    """Score a scene on all three axes and combine into a single-fish verdict.

    CV2 and lag-1 autocorrelation are computed and reported (the panels the user
    scrutinises), but eel volleys are intrinsically ms-scale irregular, so the
    *verdict* rests on the two tracker-informed cues: a genuinely separated AND
    temporally-interleaved second amplitude cluster (=> two co-active fish, or a
    tracker merge), and any concurrently-active other track in the window.
    """
    times = rec.times_s[rows]
    order = np.argsort(times)
    rows = rows[order]
    times = times[order]

    cv2 = cv2_series(times)
    cv2_mean = float(np.mean(cv2)) if cv2.size else 0.0
    cv2_p95 = float(np.percentile(cv2, 95)) if cv2.size else 0.0
    cv2_max = float(np.max(cv2)) if cv2.size else 0.0
    cv2_median = float(np.median(cv2)) if cv2.size else 0.0
    r1 = lag1_autocorr_detrended(times)

    amp = rec.amplitude_vectors(rows)
    mean_amp = float(np.mean(amp.max(axis=1))) if amp.size else 0.0
    labels, sil, minority_frac, dominant = spatial_clustering(amp)
    interleave = temporal_interleave(labels)

    main_tid = _dominant_track(rec.track_id[rows])
    n_conc = concurrent_track_count(
        rec, float(times[0]), float(times[-1]), main_tid, cfg
    )

    # A moving single fish traces one contiguous amplitude arc (interleave low);
    # two co-active fish (or a tracker merge) interleave in time (interleave high).
    two_fish_spatial = (
        sil >= cfg.spatial_sil_sep
        and minority_frac >= cfg.two_fish_min_frac
        and interleave >= cfg.interleave_thresh
    )
    cv2_pathological = (
        not cfg.cv2_report_only
    ) and cv2_median >= cfg.cv2_extreme_median
    conc_reject = cfg.gate_concurrent_tracks and n_conc > cfg.max_concurrent_tracks

    single = (not two_fish_spatial) and (not cv2_pathological) and (not conc_reject)
    reason = ""
    if not single:
        bad = []
        if two_fish_spatial:
            bad.append(
                f"interleaved-2nd-cluster(sil={sil:.2f},min={minority_frac:.2f},il={interleave:.2f})"
            )
        if conc_reject:
            bad.append(f"concurrent_tracks={n_conc}")
        if cv2_pathological:
            bad.append(f"cv2_median={cv2_median:.2f}")
        reason = ";".join(bad)
    return dict(
        rows=rows,
        mean_amp=mean_amp,
        cv2_mean=cv2_mean,
        cv2_p95=cv2_p95,
        cv2_max=cv2_max,
        lag1_autocorr=r1,
        spatial_dominant_frac=dominant,
        spatial_silhouette=sil,
        spatial_interleave=interleave,
        spatial_minority_frac=minority_frac,
        n_concurrent_tracks=n_conc,
        single_fish=single,
        reject_reason=reason,
    )


def _event_spans(rec: Recording, cfg: Config) -> dict[int, tuple[float, float]]:
    """Time span (t0, t1) of every Hunting event, for overlap detection."""
    spans: dict[int, tuple[float, float]] = {}
    if rec.hunting_id is None:
        return spans
    for ev in np.unique(rec.hunting_id[rec.hunting_id >= 0]):
        t = rec.times_s[rec.hunting_id == ev]
        spans[int(ev)] = (float(t.min()), float(t.max()))
    return spans


def _overlaps_other_volley(
    ev: int, spans: dict[int, tuple[float, float]], margin: float
) -> bool:
    t0, t1 = spans[ev]
    for other, (o0, o1) in spans.items():
        if other == ev:
            continue
        if o0 - margin <= t1 and o1 + margin >= t0:
            return True
    return False


def detect_volleys(rec: Recording, cfg: Config) -> list[Scene]:
    """Find candidate volley scenes, score shape (step 1) and purity (step 2)."""
    scenes: list[Scene] = []
    if rec.hunting_id is None:
        return scenes
    spans = _event_spans(rec, cfg)
    ids = np.unique(rec.hunting_id[rec.hunting_id >= 0])
    for ev in ids:
        rows = np.where(rec.hunting_id == ev)[0]
        if rows.size < cfg.volley_min_pulses:
            continue
        times = np.sort(rec.times_s[rows])
        gscore, peak_rate, frac = gamma_shape_score(times, cfg)

        amp = rec.amplitude_vectors(rows)
        kept, removed = purify_scene(rows, amp, cfg)
        pur = score_purity(rec, kept, cfg)
        kept = pur["rows"]
        kt = np.sort(rec.times_s[kept])

        # overlapping-volley scene (two eels volleying at once): the studied
        # phenomenon and the worst stimulus -> excluded, routed to the manifest.
        overlap = _overlaps_other_volley(
            int(ev), spans, cfg.overlapping_volley_margin_s
        )
        single = pur["single_fish"] and not overlap
        reason = pur["reject_reason"]
        if overlap:
            reason = ";".join(filter(None, [reason, "overlapping-volley"]))

        scenes.append(
            Scene(
                kind="volley",
                recording=rec.recording_id,
                site=rec.site,
                event_id=int(ev),
                rows=kept,
                removed_rows=removed,
                t_start=float(kt[0]),
                t_end=float(kt[-1]),
                n_pulses=int(kept.size),
                mean_amp=pur["mean_amp"],
                gamma_score=gscore,
                peak_rate_hz=peak_rate,
                peak_rate_frac=frac,
                cv2_mean=pur["cv2_mean"],
                cv2_p95=pur["cv2_p95"],
                cv2_max=pur["cv2_max"],
                lag1_autocorr=pur["lag1_autocorr"],
                spatial_dominant_frac=pur["spatial_dominant_frac"],
                spatial_silhouette=pur["spatial_silhouette"],
                spatial_interleave=pur["spatial_interleave"],
                spatial_minority_frac=pur["spatial_minority_frac"],
                n_concurrent_tracks=pur["n_concurrent_tracks"],
                overlapping_volley=overlap,
                single_fish=single,
                reject_reason=reason,
            )
        )
    return scenes


def detect_localization(rec: Recording, cfg: Config) -> list[Scene]:
    """Find ~60 s single-fish localization windows (steady low-rate discharge)."""
    scenes: list[Scene] = []
    # candidate tracks: those with a >= window_s span of tracked pulses.
    have_track = rec.track_id >= 0
    if not have_track.any():
        return scenes
    for tid in np.unique(rec.track_id[have_track]):
        rows = np.where(rec.track_id == tid)[0]
        if rows.size < 20:
            continue
        times = rec.times_s[rows]
        order = np.argsort(times)
        rows, times = rows[order], times[order]
        if times[-1] - times[0] < cfg.localization_window_s:
            continue
        # steady localization rate band
        ipi_ms = np.diff(times) * 1e3
        med = float(np.median(ipi_ms))
        if not (cfg.localization_ipi_ms_lo <= med <= cfg.localization_ipi_ms_hi):
            continue
        # take the densest window_s slice with in-band median IPI
        w0 = _pick_window(times, cfg.localization_window_s)
        if w0 is None:
            continue
        lo, hi = w0
        win_rows = rows[lo:hi]
        win_times = times[lo:hi]
        if win_rows.size < 20:
            continue
        # require the window to actually SPAN ~the target duration (a densest
        # slice can be front-loaded into a short active burst then go silent).
        if win_times[-1] - win_times[0] < 0.75 * cfg.localization_window_s:
            continue
        # cheap temporal pre-gate (times only) before the expensive raw load.
        # Localization is a STEADY discharge, so (unlike volleys) a loose CV2
        # ceiling here just skips obviously-ragged windows before loading cutouts.
        wt = times[lo:hi]
        cheap_cv2 = cv2_series(wt)
        if cheap_cv2.size and np.percentile(cheap_cv2, 95) > 1.0:
            continue
        amp = rec.amplitude_vectors(win_rows)
        kept, removed = purify_scene(win_rows, amp, cfg)
        pur = score_purity(rec, kept, cfg)
        kept = pur["rows"]
        kt = np.sort(rec.times_s[kept])
        gscore, peak_rate, frac = gamma_shape_score(kt, cfg)
        scenes.append(
            Scene(
                kind="localization",
                recording=rec.recording_id,
                site=rec.site,
                event_id=int(tid),
                rows=kept,
                removed_rows=removed,
                t_start=float(kt[0]),
                t_end=float(kt[-1]),
                n_pulses=int(kept.size),
                mean_amp=pur["mean_amp"],
                gamma_score=gscore,
                peak_rate_hz=peak_rate,
                peak_rate_frac=frac,
                cv2_mean=pur["cv2_mean"],
                cv2_p95=pur["cv2_p95"],
                cv2_max=pur["cv2_max"],
                lag1_autocorr=pur["lag1_autocorr"],
                spatial_dominant_frac=pur["spatial_dominant_frac"],
                spatial_silhouette=pur["spatial_silhouette"],
                spatial_interleave=pur["spatial_interleave"],
                spatial_minority_frac=pur["spatial_minority_frac"],
                n_concurrent_tracks=pur["n_concurrent_tracks"],
                overlapping_volley=False,
                single_fish=pur["single_fish"],
                reject_reason=pur["reject_reason"],
            )
        )
    return scenes


def _pick_window(times: np.ndarray, window_s: float) -> Optional[tuple[int, int]]:
    """Return (lo, hi) row slice of the densest ``window_s`` span of ``times``."""
    n = times.size
    best = None
    best_count = 0
    j = 0
    for i in range(n):
        while j < n and times[j] - times[i] <= window_s:
            j += 1
        count = j - i
        if count > best_count:
            best_count = count
            best = (i, j)
    return best


# ==========================================================================
# C emission + round-trip parsing
# ==========================================================================


def _c_int_array(values: np.ndarray, per_line: int = 12) -> str:
    vals = [str(int(v)) for v in values]
    lines = []
    for i in range(0, len(vals), per_line):
        lines.append("  " + ", ".join(vals[i : i + per_line]) + ",")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Format v2 — one device-agnostic StimItem library for both Teensy 3.5 and 4.1
# --------------------------------------------------------------------------

STIM_REAL_VOLLEY = 0
STIM_SYNTH_VOLLEY = 1
STIM_LOCALIZATION = 2
STIM_KIND_NAMES = {
    0: "real_volley",
    1: "synth_volley",
    2: "localization",
}
# sizeof(StimItem) with 4-byte pointers on the Teensy 4.1 (Cortex-M7):
# ptr + ptr + uint16 + uint8 + uint8 = 12 bytes (padded).
STIM_ITEM_STRUCT_BYTES = 12


@dataclass(slots=True)
class StimItemRec:
    """One device-agnostic playback item: timing + optional per-pulse amplitude."""

    label: str
    kind: int  # STIM_* constant
    group: int  # per-source provenance id (reserved; one per exported source)
    ipi_samp: np.ndarray  # uint16, wait-before-pulse in playback samples, [0]=0
    rel_amp: Optional[
        np.ndarray
    ]  # uint8 (0..255 == 0..1.0), or None => uniform full-scale


def ipi_samples_from_times(times_s: np.ndarray, playback_hz: int) -> np.ndarray:
    """Absolute pulse times (s) -> wait-before-pulse in playback SAMPLES (uint16).

    Onsets can only land on 50 kHz sample boundaries, so sample-counts are the
    honest unit (20 us granularity). Raises if any interval exceeds the uint16
    ceiling (65535 samples = 1.31 s at 50 kHz).
    """
    d = np.diff(np.asarray(times_s, dtype=np.float64))
    samp = np.round(np.concatenate([[0.0], d * playback_hz])).astype(np.int64)
    if samp.size and int(samp[1:].max(initial=0)) > 65535:
        raise ValueError(
            f"IPI {samp.max()} samp > 65535 ({samp.max() / playback_hz:.2f}s) exceeds uint16"
        )
    return samp.astype(np.uint16)


def rel_amp_to_u8(rel: np.ndarray) -> np.ndarray:
    """Per-pulse relative amplitude (0..1) -> uint8 (0..255)."""
    return np.clip(np.round(np.asarray(rel, dtype=np.float64) * 255.0), 0, 255).astype(
        np.uint8
    )


def lead_gaps_for_items(n_items: int, cfg: Config, playback_hz: int) -> np.ndarray:
    """Per-item lead-in gap in playback SAMPLES (uint16), deterministic given cfg.seed.

    One gap per item, drawn uniformly in ``[lead_gap_ms_lo, lead_gap_ms_hi]`` and rounded
    to a 50 kHz sample count. Fixed per item (baked into the header + provenance), NOT
    re-drawn per playback: downstream, the sine marker's offset in the recording plus this
    known gap locates the item's first pulse (and the per-item spread is a weak fingerprint).
    A dedicated RNG substream keeps this draw independent of any other seeded step.
    """
    rng = np.random.default_rng([cfg.seed, LEAD_GAP_RNG_SUBSTREAM])
    gaps_ms = rng.uniform(cfg.lead_gap_ms_lo, cfg.lead_gap_ms_hi, size=n_items)
    gaps_samp = np.round(gaps_ms / 1000.0 * playback_hz).astype(np.int64)
    if gaps_samp.size and int(gaps_samp.max(initial=0)) > 65535:
        raise ValueError(
            f"lead-in gap {gaps_samp.max()} samp exceeds uint16 "
            f"(lower lead_gap_ms_hi={cfg.lead_gap_ms_hi})"
        )
    return gaps_samp.astype(np.uint16)


def emit_firmware(
    waveform: Waveform,
    items: list[StimItemRec],
    lead_gaps: np.ndarray,
    header_path: Path,
    source_path: Path,
) -> None:
    """Write the v3 ``eel_stimuli.h`` + ``eel_stimuli.cpp`` (one StimItem table).

    The data is 100% device-agnostic: one signed int16 waveform + per-item
    (uint16 sample-IPI, optional uint8 relative amplitude) + a parallel per-item
    uint16 lead-in gap. The DAC-vs-PWM difference lives entirely in each firmware's
    write_sample() driver, and because pulses overlap (waveform is longer than the
    shortest IPI) the shared engine ADDITIVELY MIXES pulses at the sample clock —
    never fire-and-wait. ``lead_gaps`` (length == len(items)) is emitted as
    ``STIM_LEAD_GAP_SAMP[]``: the sine-marker lead-in gap the driver plays before each
    item (silence, driver-level; the overlap-add engine never sees it).
    """
    banner = "// GENERATED by tools/export_teensy_stimuli.py — do not edit by hand.\n"
    n = len(items)
    if lead_gaps.shape != (n,):
        raise ValueError(f"lead_gaps has {lead_gaps.shape}, expected ({n},)")
    net_q = int(round(waveform.net_integral * 1000))

    h = [
        banner,
        "#pragma once",
        "#include <stdint.h>",
        "#include <stddef.h>  // NULL",
        "",
    ]
    h.append(f"#define STIM_FORMAT_VERSION   {STIM_FORMAT_VERSION}")
    h.append(f"#define STIM_SAMPLE_RATE_HZ   {waveform.playback_hz}")
    h.append("")
    h.append(
        "// ---- EOD waveform (device-agnostic) --------------------------------------"
    )
    h.append(
        "// Signed int16, |peak| == 32767, endpoints 0. Each firmware maps a sample to"
    )
    h.append(
        "// its output by splitting the sign across two channels (3.5: two DACs; 4.1: two"
    )
    h.append(
        "// PWM+RC channels) -> a bipolar differential drive; both phases render (the"
    )
    h.append(
        "// negative phase on the second channel), and which channel leads encodes polarity."
    )
    h.append(
        "// Near-monophasic (net integral is positive) -> polarity is a firmware choice,"
    )
    h.append("// constant within a playback, randomised between playbacks.")
    h.append(f"#define EOD_HV_LEN  {waveform.n}")
    h.append(
        f"#define EOD_NET_INTEGRAL_X1000  {net_q}   // informational DC-load metric"
    )
    h.append("extern const int16_t EOD_HV[EOD_HV_LEN];")
    h.append("")
    h.append(
        "// ---- Stimulus item library (TIMING + relative amplitude, NO absolute amp) -"
    )
    h.append(
        "// ipi_samp[i] = samples to WAIT BEFORE pulse i (ipi_samp[0]==0). rel_amp[i] is"
    )
    h.append(
        "// the per-pulse relative voltage 0..255 (==0..1.0); rel_amp==NULL means every"
    )
    h.append("// pulse is full-scale. Absolute amplitude is a firmware GLOBAL scale.")
    h.append("typedef enum {")
    h.append("  STIM_REAL_VOLLEY=0, STIM_SYNTH_VOLLEY=1, STIM_LOCALIZATION=2")
    h.append("} StimKind;")
    h.append("typedef struct {")
    h.append(
        "  const uint16_t* ipi_samp;   // length n; wait-before-pulse in samples; [0]=0"
    )
    h.append(
        "  const uint8_t*  rel_amp;    // length n; 0..255; NULL => all full-scale"
    )
    h.append("  uint16_t        n;          // number of pulses")
    h.append("  uint8_t         kind;       // StimKind")
    h.append(
        "  uint8_t         group;      // per-source provenance id (reserved)"
    )
    h.append("} StimItem;")
    h.append(f"#define N_STIM_ITEMS  {n}")
    h.append("extern const StimItem STIM_ITEMS[N_STIM_ITEMS];")
    h.append("")
    h.append(
        "// ---- Per-item sine-marker lead-in gap ------------------------------------"
    )
    h.append(
        "// STIM_LEAD_GAP_SAMP[i] = samples of SILENCE the driver plays between the sine"
    )
    h.append(
        "// sine marker and item i's first pulse. Randomly chosen once at export (seeded,"
    )
    h.append(
        "// deterministic) then FIXED per item — so the marker's recorded offset plus this"
    )
    h.append(
        "// known gap locates/identifies the item downstream. Driver-level (a silent lead;"
    )
    h.append("// the overlap-add engine never sees it). Parallel to STIM_ITEMS.")
    h.append("extern const uint16_t STIM_LEAD_GAP_SAMP[N_STIM_ITEMS];")
    h.append("")
    header_path.write_text("\n".join(h) + "\n")

    c = [banner, f'#include "{header_path.name}"', ""]
    c.append("const int16_t EOD_HV[EOD_HV_LEN] = {")
    c.append(_c_int_array(waveform.samples_i16))
    c.append("};")
    c.append("")
    for i, it in enumerate(items):
        c.append(
            f"// item {i}: {STIM_KIND_NAMES[it.kind]} group={it.group} — {it.label}"
        )
        c.append(f"static const uint16_t ITEM_{i}_IPI[{it.ipi_samp.size}] = {{")
        c.append(_c_int_array(it.ipi_samp))
        c.append("};")
        if it.rel_amp is not None:
            c.append(f"static const uint8_t ITEM_{i}_AMP[{it.rel_amp.size}] = {{")
            c.append(_c_int_array(it.rel_amp))
            c.append("};")
    c.append("")
    c.append("const StimItem STIM_ITEMS[N_STIM_ITEMS] = {")
    for i, it in enumerate(items):
        amp = f"ITEM_{i}_AMP" if it.rel_amp is not None else "NULL"
        c.append(
            f"  {{ ITEM_{i}_IPI, {amp}, {it.ipi_samp.size}, {it.kind}, {it.group} }},"
            f"  // {STIM_KIND_NAMES[it.kind]}"
        )
    c.append("};")
    c.append("")
    c.append(
        "// Per-item lead-in gap (samples of silence after the sine marker, before"
    )
    c.append("// the item's first pulse). Parallel to STIM_ITEMS; fixed per item.")
    c.append("const uint16_t STIM_LEAD_GAP_SAMP[N_STIM_ITEMS] = {")
    c.append(_c_int_array(lead_gaps))
    c.append("};")
    c.append("")
    source_path.write_text("\n".join(c) + "\n")


def parse_firmware(source_path: Path) -> dict:
    """Parse a generated v2 ``.cpp`` back into numpy arrays (round-trip check).

    Returns ``{"EOD_HV": int16, "items": [{"ipi_samp": uint16, "rel_amp": uint8|None,
    "n": int, "kind": int, "group": int}, ...]}`` in ``STIM_ITEMS[]`` order.
    """
    import re

    text = source_path.read_text()

    def _named_arrays(src: str) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        pat = re.compile(
            r"(?:static\s+)?const\s+(int16_t|uint16_t|uint8_t)\s+"
            r"(\w+)\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}",
            re.S,
        )
        for m in pat.finditer(src):
            ctype, name, body = m.group(1), m.group(2), m.group(3)
            nums = [int(x) for x in re.findall(r"-?\d+", body)]
            dt = {"int16_t": np.int16, "uint16_t": np.uint16, "uint8_t": np.uint8}[
                ctype
            ]
            out[name] = np.array(nums, dtype=dt)
        return out

    arrays = _named_arrays(text)
    result: dict = {"EOD_HV": arrays["EOD_HV"].astype(np.int16), "items": []}
    gaps = arrays.get("STIM_LEAD_GAP_SAMP")
    result["lead_gap_samp"] = None if gaps is None else gaps.astype(np.uint16)

    # parse the STIM_ITEMS[] aggregate initialiser: one {ipi, amp|NULL, n, kind, group} per item
    m = re.search(r"STIM_ITEMS\s*\[[^\]]*\]\s*=\s*\{(.*)\}\s*;", text, re.S)
    if m:
        for row in re.finditer(r"\{([^{}]*)\}", m.group(1)):
            toks = [t.strip() for t in row.group(1).split(",")]
            ipi_name, amp_name = toks[0], toks[1]
            n, kind, group = int(toks[2]), int(toks[3]), int(toks[4])
            result["items"].append(
                {
                    "ipi_samp": arrays[ipi_name].astype(np.uint16),
                    "rel_amp": None
                    if amp_name == "NULL"
                    else arrays[amp_name].astype(np.uint8),
                    "n": n,
                    "kind": kind,
                    "group": group,
                }
            )
    return result


# ==========================================================================
# Packet size
# ==========================================================================


def packet_bytes(waveform: Waveform, items: list["StimItemRec"]) -> int:
    """Flash bytes the v3 library occupies (waveform int16 + uint16 IPI + uint8 amp +
    the per-item uint16 lead-in gap array)."""
    b = waveform.n * 2
    for it in items:
        b += it.ipi_samp.size * 2
        if it.rel_amp is not None:
            b += it.rel_amp.size * 1
    b += len(items) * STIM_ITEM_STRUCT_BYTES  # the StimItem descriptor table
    b += len(items) * 2  # STIM_LEAD_GAP_SAMP[] (uint16 per item)
    return b


# ==========================================================================
# CLI
# ==========================================================================


def _iter_recordings(cfg: Config):
    for site in cfg.sites:
        site_dir = cfg.eods_root / site
        if not site_dir.is_dir():
            log.warning("site dir missing: %s", site_dir)
            continue
        for h5 in sorted(site_dir.glob("*_pulses.h5")):
            yield site, h5


def _git_commit(repo: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@app.command()
def scan(
    config: Path = typer.Option(..., "--config", "-c", help="stimuli_config.yaml"),
    verbose: int = typer.Option(1, "--verbose", "-v", count=True),
) -> None:
    """Mine every configured recording for candidate single-fish scenes."""
    configure_logging(verbose)
    cfg = Config.from_yaml(config)
    cfg.out_dir.mkdir(parents=True, exist_ok=True)

    all_scenes: list[Scene] = []
    for site, h5 in _iter_recordings(cfg):
        log.info("scanning %s / %s", site, h5.name)
        try:
            rec = open_recording(h5, site, cfg.original_hz_fallback)
        except Exception as exc:  # pragma: no cover - defensive I/O
            log.warning("skip %s: %s", h5.name, exc)
            continue
        try:
            vs = detect_volleys(rec, cfg)
            ls = detect_localization(rec, cfg)
            log.info(
                "  %d volley candidates, %d localization windows", len(vs), len(ls)
            )
            all_scenes.extend(vs)
            all_scenes.extend(ls)
        finally:
            rec.close()

    _write_manifests(cfg, all_scenes)


def _write_manifests(cfg: Config, scenes: list[Scene]) -> None:
    manifest = cfg.out_dir / "stimuli_candidates.json"
    manifest.write_text(json.dumps([s.to_row() for s in scenes], indent=2))
    log.info("wrote %s (%d scenes)", manifest, len(scenes))

    # multi-fish rejects (overlapping volleys) -> separate CSV
    rejects = [s for s in scenes if s.kind == "volley" and not s.single_fish]
    csv_path = cfg.out_dir / "multifish_volley_candidates.csv"
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "recording",
                "site",
                "event_id",
                "t_start",
                "t_end",
                "n_pulses",
                "gamma_score",
                "cv2_p95",
                "cv2_max",
                "lag1_autocorr",
                "spatial_silhouette",
                "spatial_minority_frac",
                "spatial_interleave",
                "n_concurrent_tracks",
                "reject_reason",
            ]
        )
        for s in rejects:
            w.writerow(
                [
                    s.recording,
                    s.site,
                    s.event_id,
                    f"{s.t_start:.3f}",
                    f"{s.t_end:.3f}",
                    s.n_pulses,
                    f"{s.gamma_score:.3f}",
                    f"{s.cv2_p95:.3f}",
                    f"{s.cv2_max:.3f}",
                    f"{s.lag1_autocorr:.3f}",
                    f"{s.spatial_silhouette:.3f}",
                    f"{s.spatial_minority_frac:.3f}",
                    f"{s.spatial_interleave:.3f}",
                    s.n_concurrent_tracks,
                    s.reject_reason,
                ]
            )
    log.info("wrote %s (%d multi-fish rejects)", csv_path, len(rejects))

    # human-facing summary of the single-fish picks
    single = [s for s in scenes if s.single_fish]
    vol = sorted(
        [s for s in single if s.kind == "volley"],
        key=lambda s: (s.n_pulses, s.mean_amp),
        reverse=True,
    )
    loc = sorted(
        [s for s in single if s.kind == "localization"],
        key=lambda s: s.n_pulses,
        reverse=True,
    )
    log.info("=== single-fish VOLLEY candidates (top 12, by completeness x SNR) ===")
    for s in vol[:12]:
        log.info(
            "  %-24s ev%-4d n=%-3d peak=%5.0fHz amp=%4.0f gamma=%.2f cv2med=%.2f "
            "sil=%.2f il=%.2f conc=%d",
            s.recording,
            s.event_id,
            s.n_pulses,
            s.peak_rate_hz,
            s.mean_amp,
            s.gamma_score,
            s.cv2_mean,
            s.spatial_silhouette,
            s.spatial_interleave,
            s.n_concurrent_tracks,
        )
    log.info("=== single-fish LOCALIZATION windows (top 8) ===")
    for s in loc[:8]:
        log.info(
            "  %-24s track%-4d n=%-4d dur=%.0fs cv2_p95=%.2f sil=%.2f il=%.2f conc=%d",
            s.recording,
            s.event_id,
            s.n_pulses,
            s.t_end - s.t_start,
            s.cv2_p95,
            s.spatial_silhouette,
            s.spatial_interleave,
            s.n_concurrent_tracks,
        )


def low_ipi_frac(times_s: np.ndarray, low_ipi_ms: float) -> float:
    """Fraction of inter-pulse intervals shorter than ``low_ipi_ms`` (a DIAGNOSTIC).

    Reported per localization pick so the low-IPI content is visible. It is NOT used
    to gate: a single wide-localizing eel produces a similar sprinkle on its own
    (~6-8 % of intervals below 50 ms), so a low-IPI fraction alone does not
    distinguish one fish from two — the concurrent-track count does.
    """
    t = np.sort(np.asarray(times_s, dtype=np.float64))
    if t.size < 2:
        return 0.0
    ipi_ms = np.diff(t) * 1e3
    return float((ipi_ms < low_ipi_ms).mean())


def _select_scenes(cfg: Config, scenes: list[Scene]) -> tuple[list[Scene], list[Scene]]:
    """Resolve explicit picks (config) or auto-rank the top single-fish scenes."""
    by_key = {(s.recording, s.kind, s.event_id): s for s in scenes}

    def _pick(picks: list[dict], kind: str) -> list[Scene]:
        out = []
        for p in picks:
            key = (p["recording"], kind, int(p["event_id"]))
            if key in by_key:
                out.append(by_key[key])
            else:
                log.warning("pick not found in scan: %s", p)
        return out

    vol = _pick(cfg.volley_picks, "volley")
    loc = _pick(cfg.localization_picks, "localization")

    if not vol:
        # complete (onset->decay), clean (low interleave), high-SNR single-fish
        # volleys; one per recording (different fish, no pseudoreplication). The
        # tracker already isolates the fish, so SNR/completeness rank above strict
        # scene isolation; n_concurrent_tracks is reported for the human to judge.
        # peak_rate_hz > volley_min_peak_hz keeps only trains that actually reach
        # the volley regime — it drops a fast-localization / weak fragment (e.g. an
        # 82 Hz "volley" that never gets to a <10 ms IPI) and picks a real one from
        # the next recording instead. peak_rate_frac <= volley_peak_in_first_frac
        # additionally drops a PARTIAL volley whose peak lands late in the train (a
        # capture that missed the onset burst — not a representative full volley).
        cands = [
            s
            for s in scenes
            if s.kind == "volley"
            and s.single_fish
            and s.gamma_score >= 0.6
            and s.n_pulses >= 25
            and s.spatial_interleave < 0.3
            and cfg.volley_min_peak_hz < s.peak_rate_hz <= cfg.volley_max_peak_hz
            and s.peak_rate_frac <= cfg.volley_peak_in_first_frac
        ]
        cands.sort(key=lambda s: (s.mean_amp, s.n_pulses), reverse=True)
        seen = set()
        for s in cands:
            if s.recording in seen:
                continue
            seen.add(s.recording)
            vol.append(s)
            if len(vol) >= cfg.n_volley_exemplars:
                break
    if not loc:
        # single-fish localization windows, one per recording. Beyond the scan's
        # spatial-temporal single-fish verdict, require a CLEAN SCENE: no other fish
        # tracked concurrently in the window (n_concurrent_tracks <= the config cap,
        # default 0). This is the reliable "2nd fish intruding" cue — unlike a low-IPI
        # sprinkle, which a single wide-localizing eel produces on its own and so does
        # NOT gate on. Windows are ranked by pulse count / SNR among the clean ones.
        cands = [
            s
            for s in scenes
            if s.kind == "localization"
            and s.single_fish
            and s.n_concurrent_tracks <= cfg.localization_max_concurrent_tracks
        ]
        cands.sort(key=lambda s: (s.n_pulses, s.mean_amp), reverse=True)
        seen = set()
        for s in cands:
            if s.recording in seen:
                continue
            seen.add(s.recording)
            loc.append(s)
            if len(loc) >= cfg.n_localization_exemplars:
                break
        if len(loc) < cfg.n_localization_exemplars:
            log.info(
                "only %d localization window(s) have a clean scene "
                "(<=%d concurrent tracks); using those (config allows 2-3)",
                len(loc),
                cfg.localization_max_concurrent_tracks,
            )
    return vol, loc


@app.command()
def export(
    config: Path = typer.Option(..., "--config", "-c", help="stimuli_config.yaml"),
    verbose: int = typer.Option(1, "--verbose", "-v", count=True),
) -> None:
    """Build the waveform + selected exemplars and emit firmware/QC/provenance."""
    configure_logging(verbose)
    cfg = Config.from_yaml(config)
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    cfg.firmware_dir.mkdir(parents=True, exist_ok=True)

    # 1. waveform from the global template
    align = cfg.resolve_alignment_json()
    template, original_hz = load_global_template(align)
    waveform = build_waveform(template, original_hz, cfg)
    _report_waveform(waveform)

    # 2. scenes (from cached scan, else scan now)
    manifest = cfg.out_dir / "stimuli_candidates.json"
    if manifest.exists():
        scenes = _load_scenes(manifest)
    else:
        log.info("no cached scan; scanning now")
        scenes = []
        for site, h5 in _iter_recordings(cfg):
            try:
                rec = open_recording(h5, site, cfg.original_hz_fallback)
            except Exception:  # pragma: no cover
                continue
            try:
                scenes.extend(detect_volleys(rec, cfg))
                scenes.extend(detect_localization(rec, cfg))
            finally:
                rec.close()

    vol_scenes, loc_scenes = _select_scenes(cfg, scenes)
    log.info(
        "selected %d volley + %d localization exemplars",
        len(vol_scenes),
        len(loc_scenes),
    )

    # 3. Build the unified device-agnostic item library. Real volleys + real
    #    localization from the scan; then the synthetic population (synth volleys carry a
    #    per-pulse voltage decay envelope) if present. QC keeps the µs form.
    hz = waveform.playback_hz
    items: list[StimItemRec] = []
    qc_vol, qc_loc = [], []
    provenance_scenes = []
    group = 0
    for s in vol_scenes:
        times = _scene_times(cfg, s)
        ipi_samp = ipi_samples_from_times(times, hz)
        _assert_no_overlap(ipi_samp, waveform, s)
        label = f"{s.recording}#ev{s.event_id}"
        items.append(StimItemRec(label, STIM_REAL_VOLLEY, group, ipi_samp, None))
        provenance_scenes.append(_scene_provenance(cfg, s, "volley"))
        qc_vol.append((label, ipi_us_from_times(times)))
        group += 1
    for s in loc_scenes:
        times = _longest_gapfree(_scene_times(cfg, s), cfg.localization_max_gap_s)
        ipi_samp = ipi_samples_from_times(times, hz)
        label = f"{s.recording}#track{s.event_id}"
        items.append(StimItemRec(label, STIM_LOCALIZATION, group, ipi_samp, None))
        frac = low_ipi_frac(times, cfg.localization_low_ipi_ms)
        provenance_scenes.append(
            _scene_provenance(cfg, s, "localization", low_ipi_frac=frac)
        )
        qc_loc.append((label, ipi_us_from_times(times)))
        group += 1
    n_real = len(items)
    if cfg.include_synthetic:
        group = _append_synthetic_items(cfg, hz, items, group)
    n_synth = len(items) - n_real

    # Hard >100 Hz-peak backstop: drop any volley-family item that never reaches a
    # <10 ms IPI (a fast localization train or a truncated fragment slipping past
    # selection / synthesis). Localization items pass through untouched. This is the
    # single chokepoint that guarantees every played volley reaches the volley regime.
    items = _enforce_volley_peak(items, hz, cfg.volley_min_peak_hz)
    log.info(
        "library: %d real-scene + %d synthetic items built, %d total after the "
        ">%.0f Hz-peak gate",
        n_real,
        n_synth,
        len(items),
        cfg.volley_min_peak_hz,
    )

    # 4. per-item sine-marker lead-in gaps (deterministic, fixed once here) + emit
    #    firmware (v3 unified StimItem table + the parallel lead-gap array)
    lead_gaps = lead_gaps_for_items(len(items), cfg, hz)
    header = cfg.firmware_dir / "eel_stimuli.h"
    source = cfg.firmware_dir / "eel_stimuli.cpp"
    emit_firmware(waveform, items, lead_gaps, header, source)
    log.info("wrote %s and %s", header, source)

    # 5. round-trip self-check + 6. budget
    _self_check_roundtrip(source, waveform, items, lead_gaps)
    size = packet_bytes(waveform, items)
    log.info(
        "packet size: %d bytes (%.1f kB) / budget %d",
        size,
        size / 1024,
        cfg.max_flash_bytes,
    )
    if size > cfg.max_flash_bytes:
        log.error("OVER BUDGET by %d bytes", size - cfg.max_flash_bytes)

    # 7. provenance
    _write_provenance(
        cfg, align, waveform, provenance_scenes, size, items, lead_gaps
    )

    # 8. QC pdf (real-exemplar review; uses the µs form)
    from fakefish import stimuli_qc  # sibling module; matplotlib kept out of the scan path

    stimuli_qc.render_qc(cfg, waveform, vol_scenes, loc_scenes, qc_vol, qc_loc)
    log.info("done.")


VOLLEY_KINDS = (STIM_REAL_VOLLEY, STIM_SYNTH_VOLLEY)


def item_peak_rate_hz(it: StimItemRec, playback_hz: int) -> float:
    """Peak instantaneous rate (Hz) of an item = playback_hz / its shortest IPI.

    ``ipi_samp[0]`` is 0 (nothing to wait before pulse 0), so the shortest *real*
    interval is ``min(ipi_samp[1:])``; a single-pulse item has no interval and is
    treated as rate 0. This is the exact quantity the >100 Hz gate tests.
    """
    body = np.asarray(it.ipi_samp)[1:]
    if body.size == 0:
        return 0.0
    min_ipi = int(body.min())
    if min_ipi <= 0:
        return math.inf
    return playback_hz / min_ipi


def _enforce_volley_peak(
    items: list[StimItemRec], playback_hz: int, min_peak_hz: float
) -> list[StimItemRec]:
    """Drop every volley-family item that never reaches a peak rate > ``min_peak_hz``.

    A real volley or synth volley must reach the volley regime — a
    peak instantaneous rate above ``min_peak_hz`` (i.e. some IPI shorter than
    ``1000/min_peak_hz`` ms). Anything that doesn't is a fast-localization train or
    a truncated fragment and is removed here, loudly, so it can never play in VOLLEY
    mode. Localization items are never volley-gated and pass through unchanged.
    """
    kept: list[StimItemRec] = []
    dropped: list[tuple[str, float]] = []
    for it in items:
        if it.kind in VOLLEY_KINDS:
            peak = item_peak_rate_hz(it, playback_hz)
            if not (peak > min_peak_hz):
                dropped.append((it.label, peak))
                continue
        kept.append(it)
    if dropped:
        log.warning(
            "dropped %d volley item(s) below the >%.0f Hz-peak requirement:",
            len(dropped),
            min_peak_hz,
        )
        for label, peak in dropped:
            log.warning("  %-32s peak %.1f Hz (min IPI too long)", label, peak)
    return kept


def _append_synthetic_items(
    cfg: Config, hz: int, items: list[StimItemRec], group: int
) -> int:
    """Append the synthetic volley + localization population (if generated)."""
    synth_path = cfg.out_dir / "synthetic_population.npz"
    if not synth_path.exists():
        log.info(
            "no synthetic population at %s — run synthetic_volleys.py synthesize",
            synth_path,
        )
        return group
    import synthetic_volleys as sv

    for v in sv.load_synthetic(synth_path):
        if v.kind == "volley":
            ipi_samp = ipi_samples_from_times(v.times_s, hz)
            # synthetic volleys carry a physiological per-pulse voltage envelope: full at
            # onset, gently decaying to VOLLEY_DECAY_FLOOR (a real amplitude change, NOT
            # the excluded distance amplitude). Localization stays uniform full-scale
            # (rel_amp=None) — its loc↔volley amplitude contrast is a firmware scale.
            rel = rel_amp_to_u8(v.rel_amp)
            items.append(StimItemRec(v.label, STIM_SYNTH_VOLLEY, group, ipi_samp, rel))
        else:
            times = _longest_gapfree(v.times_s, cfg.localization_max_gap_s)
            ipi_samp = ipi_samples_from_times(times, hz)
            items.append(StimItemRec(v.label, STIM_LOCALIZATION, group, ipi_samp, None))
        group += 1
    return group


def _report_waveform(w: Waveform) -> None:
    log.info("=== EOD waveform ===")
    log.info(
        "  %d samples @ %d Hz  (%.0f us, FWHM %.0f us)",
        w.n,
        w.playback_hz,
        w.duration_us,
        w.fwhm_us,
    )
    log.info("  peak-to-peak (norm) = %.3f", w.peak_to_peak_norm)
    log.info(
        "  NET CHARGE (integral) = %+.3f  pos/neg area = %.2f  --> %s",
        w.net_integral,
        w.pos_neg_area_ratio,
        "STRONGLY MONOPHASIC: randomise polarity between playbacks to avoid pitting electrodes"
        if abs(w.pos_neg_area_ratio - 1.0) > 0.25
        else "roughly balanced",
    )


def _scene_times(cfg: Config, s: Scene) -> np.ndarray:
    """Re-read exact pulse times for a scene's rows from its recording."""
    site_dir = cfg.eods_root / s.site
    h5 = next(site_dir.glob(f"{s.recording}.h5"), None)
    if h5 is None:
        h5 = next(site_dir.glob(f"{s.recording}*.h5"))
    rec = open_recording(h5, s.site, cfg.original_hz_fallback)
    try:
        times = np.sort(rec.times_s[np.asarray(s.rows)])
    finally:
        rec.close()
    return times


def _longest_gapfree(times_s: np.ndarray, max_gap_s: float) -> np.ndarray:
    """Longest contiguous run of ``times`` with no inter-pulse gap beyond max_gap_s.

    A localization exemplar with a multi-second silence isn't a steady train (and
    a gap >1.31 s can't fit the uint16 sample-IPI), so we keep only the longest
    unbroken stretch.
    """
    t = np.sort(np.asarray(times_s, dtype=np.float64))
    if t.size < 2:
        return t
    gaps = np.diff(t) > max_gap_s
    # segment boundaries where a gap exceeds the limit
    breaks = np.flatnonzero(gaps) + 1
    segs = np.split(np.arange(t.size), breaks)
    best = max(segs, key=len)
    return t[best]


def _assert_no_overlap(ipi_samp: np.ndarray, waveform: Waveform, s: Scene) -> None:
    """Report how many intervals are shorter than the waveform (pulses overlap).

    This is informational, NOT an error: real volley intervals are genuinely
    shorter than the 2.6 ms waveform, so the firmware's playback engine
    additively mixes overlapping pulses rather than firing-and-waiting.
    """
    body = np.asarray(ipi_samp)[1:]
    if body.size and int(body.min()) < waveform.n:
        n_bad = int((body < waveform.n).sum())
        log.info(
            "%s#%d: %d/%d intervals shorter than the %d-sample waveform — pulses overlap "
            "(engine mixes additively; expected for high-rate volleys)",
            s.recording,
            s.event_id,
            n_bad,
            body.size,
            waveform.n,
        )


def _scene_provenance(
    cfg: Config, s: Scene, kind: str, low_ipi_frac: Optional[float] = None
) -> dict:
    return dict(
        kind=kind,
        recording=s.recording,
        site=s.site,
        event_id=s.event_id,
        t_start_s=s.t_start,
        t_end_s=s.t_end,
        n_pulses=s.n_pulses,
        n_purified_out=int(np.asarray(s.removed_rows).size),
        mean_amp=s.mean_amp,
        gamma_score=s.gamma_score,
        peak_rate_hz=s.peak_rate_hz,
        # localization diagnostic: share of gap-free-run IPIs below
        # localization_low_ipi_ms (informational — single-fish localization is wide,
        # so this is NOT a contamination gate; null for volleys).
        low_ipi_frac=low_ipi_frac,
        cv2_mean=s.cv2_mean,
        cv2_p95=s.cv2_p95,
        cv2_max=s.cv2_max,
        lag1_autocorr=s.lag1_autocorr,
        spatial_dominant_frac=s.spatial_dominant_frac,
        spatial_silhouette=s.spatial_silhouette,
        spatial_interleave=s.spatial_interleave,
        spatial_minority_frac=s.spatial_minority_frac,
        n_concurrent_tracks=s.n_concurrent_tracks,
        overlapping_volley=s.overlapping_volley,
        single_fish=s.single_fish,
    )


def _lead_in_marker_provenance(
    cfg: Config, items: list[StimItemRec], lead_gaps: np.ndarray, playback_hz: int
) -> dict:
    """The frozen sine-marker + per-item lead-in-gap contract (see the detector handoff).

    Firmware realises the tone itself (frequency/durations/amplitude live in the sketch);
    the values here are the FROZEN nominal contract a downstream marker detector keys off,
    plus the deterministic per-item gaps generated at export.
    """
    return dict(
        # nominal marker contract (realised in firmware/eel_fakefish/eel_control.h):
        nominal_freq_hz=10000,   # realised in eel_control.h (MARKER_FREQ_HZ)
        leadin_s=1.0,
        calibration_s=10.0,
        note=(
            "out-of-band 10 kHz sine ANCHOR: 10 s in CALIBRATION mode (replaces the old "
            "50 Hz eel-EOD cal train), ~1 s lead-in before every volley/localization "
            "playback. Zero-start LUT + raised-cosine ramp; amplitude is a firmware global "
            "(decoupled from the localization scale). Duration alone distinguishes the 10 s "
            "cal from the 1 s lead-in."
        ),
        lead_gap_ms_lo=cfg.lead_gap_ms_lo,
        lead_gap_ms_hi=cfg.lead_gap_ms_hi,
        # per-item deterministic gap (STIM_LEAD_GAP_SAMP[]): the silence between the marker
        # and each item's first pulse. Fixed per item so downstream can sync/identify.
        per_item_gap=[
            dict(
                index=i,
                label=it.label,
                kind=STIM_KIND_NAMES[it.kind],
                gap_samp=int(lead_gaps[i]),
                gap_ms=round(float(lead_gaps[i]) / playback_hz * 1000.0, 3),
            )
            for i, it in enumerate(items)
        ],
    )


def _write_provenance(
    cfg: Config,
    align: Path,
    waveform: Waveform,
    scenes: list[dict],
    size: int,
    items: list[StimItemRec],
    lead_gaps: np.ndarray,
) -> None:
    repo = _res.ROOT
    prov = dict(
        stim_format_version=STIM_FORMAT_VERSION,
        export_date_utc=datetime.now(timezone.utc).isoformat(),
        # NB: this provenance key is retained verbatim so a regenerated
        # provenance keeps the same schema as the frozen data/stimuli_provenance.json
        # downstream contract. The value is now this repo's HEAD.
        eeltracker_git_commit=_git_commit(repo),
        eods_root=str(cfg.eods_root),
        alignment_json=str(align),
        alignment_json_sha256=_sha256(align),
        original_sample_rate_hz=waveform.original_hz,
        playback_sample_rate_hz=waveform.playback_hz,
        resampling_method="scipy.signal.resample_poly",
        waveform=dict(
            n_samples=waveform.n,
            fwhm_us=waveform.fwhm_us,
            duration_us=waveform.duration_us,
            peak_to_peak_norm=waveform.peak_to_peak_norm,
            net_integral=waveform.net_integral,
            pos_neg_area_ratio=waveform.pos_neg_area_ratio,
        ),
        packet_bytes=size,
        flash_budget_bytes=cfg.max_flash_bytes,
        selection_criteria=dataclasses.asdict(_selection_summary(cfg)),
        lead_in_marker=_lead_in_marker_provenance(
            cfg, items, lead_gaps, waveform.playback_hz
        ),
        scenes=scenes,
    )
    out = cfg.out_dir / "stimuli_provenance.json"
    out.write_text(json.dumps(prov, indent=2, default=str))
    log.info("wrote %s", out)


@dataclass(slots=True)
class _SelectionSummary:
    volley_min_pulses: int
    volley_max_ipi_ms: float
    volley_min_peak_hz: float
    volley_max_peak_hz: float
    spatial_sil_sep: float
    two_fish_min_frac: float
    interleave_thresh: float
    max_purify_frac: float
    concurrent_margin_s: float
    concurrent_min_pulses: int
    max_concurrent_tracks: int
    localization_window_s: float
    localization_max_concurrent_tracks: int
    localization_low_ipi_ms: float


def _selection_summary(cfg: Config) -> _SelectionSummary:
    return _SelectionSummary(
        volley_min_pulses=cfg.volley_min_pulses,
        volley_max_ipi_ms=cfg.volley_max_ipi_ms,
        volley_min_peak_hz=cfg.volley_min_peak_hz,
        volley_max_peak_hz=cfg.volley_max_peak_hz,
        spatial_sil_sep=cfg.spatial_sil_sep,
        two_fish_min_frac=cfg.two_fish_min_frac,
        interleave_thresh=cfg.interleave_thresh,
        max_purify_frac=cfg.max_purify_frac,
        concurrent_margin_s=cfg.concurrent_margin_s,
        concurrent_min_pulses=cfg.concurrent_min_pulses,
        max_concurrent_tracks=cfg.max_concurrent_tracks,
        localization_window_s=cfg.localization_window_s,
        localization_max_concurrent_tracks=cfg.localization_max_concurrent_tracks,
        localization_low_ipi_ms=cfg.localization_low_ipi_ms,
    )


def _self_check_roundtrip(
    source: Path, waveform: Waveform, items: list[StimItemRec], lead_gaps: np.ndarray
) -> None:
    parsed = parse_firmware(source)
    assert np.array_equal(parsed["EOD_HV"], waveform.samples_i16), (
        "waveform round-trip failed"
    )
    assert len(parsed["items"]) == len(items), "item count round-trip failed"
    assert parsed["lead_gap_samp"] is not None, "STIM_LEAD_GAP_SAMP missing"
    assert np.array_equal(parsed["lead_gap_samp"], lead_gaps), "lead-gap round-trip failed"
    for it, got in zip(items, parsed["items"], strict=True):
        assert np.array_equal(it.ipi_samp, got["ipi_samp"]), (
            f"IPI round-trip failed: {it.label}"
        )
        if it.rel_amp is None:
            assert got["rel_amp"] is None, f"rel_amp should be NULL: {it.label}"
        else:
            assert np.array_equal(it.rel_amp, got["rel_amp"]), (
                f"rel_amp round-trip: {it.label}"
            )
        assert it.kind == got["kind"] and it.group == got["group"], (
            f"meta round-trip: {it.label}"
        )
    log.info("round-trip self-check OK (%d items)", len(items))


def _load_scenes(manifest: Path) -> list[Scene]:
    raw = json.loads(manifest.read_text())
    out = []
    for d in raw:
        d = dict(d)
        d["rows"] = np.asarray(d["rows"], dtype=np.int64)
        d["removed_rows"] = np.asarray(d["removed_rows"], dtype=np.int64)
        out.append(Scene(**d))
    return out


if __name__ == "__main__":
    app()
