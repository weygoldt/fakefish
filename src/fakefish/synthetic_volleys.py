"""Synthesise a population of realistic eel volleys for Teensy playback.

**The volley model is not fitted here.** It is :mod:`fakefish.volley_model`, a
verbatim copy of the reference sampler from the eeltracker ``volley_dynamics``
analysis, driven by ``data/volley_model_params.json``. That model was fitted to
the 200 strongest hunting volleys in the FLONA 2025 field dataset (43 recordings,
16 sites) and validated there against re-fitted synthetic draws.
``docs/VOLLEY_GENERATIVE_SPEC.md`` is the shipped copy of its spec — the model,
every fitted number, and every caveat.

**Do not edit either vendored file.** Both are byte-identical to their source in
``eeltracker/analyses/volley_dynamics/`` so that dropping in a newer fit is a plain
copy and ``diff`` is the drift check. A change to the *model* belongs upstream,
where ``volley_validate.py`` is its regression test. ``tests/test_volley_model.py``
pins the copies' sha256 here.

Three things in the model are measurements, not modelling choices, and the wiring
below must not quietly undo them:

1. **Pulse times integrate the rate curve** with a small multiplicative jitter —
   *not* a Poisson process. Real volleys are nearly clockwork (CV2 ~ 0.12, where
   Poisson is 1.0). A Poisson train has the right mean rate and the wrong texture.
2. **There is no ramp-up.** A volley starts at its peak rate and decays from there
   (median time-to-peak 18 ms, lower quartile 0 ms).
3. **Amplitude decays smoothly**, ~20 % across the volley, with only ~1.4 %
   pulse-to-pulse jitter.

What THIS module owns is everything between the model's event series and the
firmware's item table:

* **the sample grid.** The model emits float times; the device emits pulses on
  50 kHz sample boundaries. Volleys are generated *on that grid*, so the numbers
  the QC prints are exactly the numbers the device plays.
* **the IPI floor** (``SYNTH_MIN_IPI_SAMP``) — below it pulses collide
  spike-on-spike and the overlap-add engine saturates. See the constant.
* **amplitude normalisation.** The model's amplitude is relative to a volley's own
  MEDIAN pulse, so it exceeds 1.0 at onset; firmware ``rel_amp`` is a 0..255
  encoding of 0..1. Each volley is normalised to its own peak, which discards only
  the absolute level — and the spec is explicit that absolute level is a free knob.
* **the localization trains**, which the volley model does not cover.
* **the population**: how many volleys, and the playback-safety QC over them.

Every synthetic volley is *just the volley* — a high-rate discharge firing at full
amplitude from its first pulse, with no localization prelude and no onset ramp.
That is both what the model says (point 2 above) and what hand-timed playback
needs: the operator supplies the localization lead-in live.

Subcommands::

    fakefish-synth-volleys overlap-demo   # what mean-waveform overlap looks like at high rate
    fakefish-synth-volleys analyze        # extract the real population (QC cross-check) + plot
    fakefish-synth-volleys synthesize     # generate the synthetic population
    fakefish-synth-volleys compare        # synthetic vs real
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from fakefish.viz.loggers import configure_logging, get_logger  # noqa: E402
from fakefish.viz.plotstyle import (  # noqa: E402
    CATEGORICAL,
    full_page,
)

from fakefish import export_teensy_stimuli as ex  # noqa: E402
from fakefish import _resources as _res  # noqa: E402
from fakefish import _constants as K  # noqa: E402
import typer  # noqa: E402

log = get_logger(__name__)
app = typer.Typer(
    add_completion=False, help="Model + synthesise eel volleys for playback."
)


@app.callback()
def _main() -> None:
    """Model real eel volleys and synthesise a population for Teensy playback."""


OUT_DIR = _res.DATA_DIR
FIG_DIR = _res.FIGS_DIR / "synthetic_volleys"
POP_CACHE = OUT_DIR / "real_volley_population.npz"
SYNTH_NPZ = OUT_DIR / "synthetic_population.npz"

# ==========================================================================
# What this module adds on top of the vendored model
# ==========================================================================

# The fitted parameters, vendored verbatim (see the module docstring).
VOLLEY_MODEL_JSON = OUT_DIR / "volley_model_params.json"

# Everything the library fires is a `strong` volley: the extreme hunting volleys the
# source analysis selected for (~393 Hz start, ~0.47 s, ~88 pulses). That is what a
# "fire a hunting volley" trigger should emit. The model's `ordinary` class is the
# everyday volley and is deliberately NOT used here — the spec (§4) reports it re-fits
# ~30 % long, because at ~12 pulses a three-parameter decay is barely identifiable.
VOLLEY_KIND = "strong"

# How many synthetic volleys the library carries. The RC device draws one uniformly per
# trigger, so the POOL IS THE SAMPLING DISTRIBUTION: more items = a finer discrete
# approximation of the fitted joint (r_start, duration, lambda) distribution. Flash is
# cheap — ~3 B per pulse, ~330 B for a median volley.
#
# 100 is not a round number, it is set by the LOG FORMAT. `pulse_log.h` types the library
# item as `int8_t` with -1 as the absent sentinel (PLOG_ABSENT_ITEM), so an item index
# must fit a signed byte: at most 128 items in the whole library. With the real scenes and
# the localization trains alongside, 100 volleys puts the highest index at 112 — inside,
# with room to grow. Going beyond is not a flash question but a pulse-log FORMAT change
# (CLAUDE.md invariant 9, and the golden file that pins it).
N_SYNTH_VOLLEYS = 100

# Pulses land on the 50 kHz sample grid, so samples are the honest unit for the floor.
#
# THE FLOOR IS SET BY THE EOD'S ENERGY WIDTH, NOT ITS LENGTH. EOD_HV is 131 samples
# (2.62 ms) long but back-loaded with a low tail: 99 % of its energy is inside 1.92 ms.
# Two pulses closer than that collide spike-on-spike and the overlap-add sum saturates;
# further apart they only overlap tail-on-spike, which adds harmlessly. Measured over 400
# model volleys: a 2.00 ms floor gives ZERO overlap-clip, an unclamped train clips 3 % of
# volleys, and the clamp costs nothing in aggregate — duration, pulse count and sustained peak
# are unchanged, because the intervals it moves move by microseconds.
#
# IT DOES LEAVE A VISIBLE SIGNATURE, and that is worth knowing before analysing a recording:
# **9.5 % of shipped intervals sit exactly on the floor**, a delta spike where real volleys
# have a smooth tail down to ~2.1 ms. (6.4 % of raw model draws fall below 2.0 ms; the rest of
# the pile-up is intervals that simply round to 100 samples.) Not hidden and not a bug — the
# spec's own §5 says the sub-2 ms intervals in the source data are spurious EXTRA DETECTIONS
# rather than extra EODs, so clamping them moves the model toward biology. But it does it by
# stacking them on one value instead of redistributing them.
#
# 2.0 ms is independently the source detector's resolution limit: the analysis ran
# `find_peaks(distance_ms=1.5)`, and its raw-trace check (spec §5) showed the sub-2 ms
# intervals in the data are spurious EXTRA DETECTIONS, not extra EODs. So the model has no
# support below it either. Physics and provenance land on the same number.
#
# The retired 2.5 ms floor — and the 381 Hz "physics ceiling" that went with it, derived
# from the EOD's full 2.62 ms LENGTH rather than its energy width — cost 11 % of the
# sustained peak for no gain in overlap safety.
SYNTH_MIN_IPI_SAMP = 100  # 2.00 ms at 50 kHz
SYNTH_MIN_IPI_MS = SYNTH_MIN_IPI_SAMP / ex.PLAYBACK_RATE_HZ * 1e3

# min IPI (ms) of the fastest pulse a volley must contain to count as reaching the
# >100 Hz volley regime — the same 10 ms the export gate tests.
VOLLEY_PEAK_MAX_IPI_MS = 10.0

# A real train peaking above this is TWO fish volleying together (the rates add), a
# multi-fish artifact. Used ONLY to select the real population that `analyze` and
# `compare` plot as an independent cross-check — the model's own selection happened
# upstream and does not use this.
VOLLEY_MULTIFISH_PEAK_HZ = 450.0
SUSTAINED_PEAK_WINDOW = 5  # intervals a "peak" must hold for, to not be one outlier

# Synthetic localization: a set of standalone resting/exploring trains spanning the
# realistic 1-10 Hz range, one per target average rate. Each is lognormally jittered
# (LOC_SYNTH_CV) with IPIs clamped under LOC_MAX_IPI_S so a low-rate (1 Hz) train
# neither overflows the uint16 sample-IPI (65535 samp = 1.31 s) nor gets chopped by
# the gap-free trim (localization_max_gap_s = 1.2 s).
#
# THIS IS THE SEAM FOR A PROPER LOCALIZATION MODEL, AND THE MODEL HAS NOW LANDED — but
# not here yet. It is a designed rate ladder, not a fit: the volley model deliberately
# does not cover localization (its own §1.3 — one call is one volley), and its §2.3
# measures ~3.2 Hz between volleys but labels that an UPPER BOUND, contaminated by
# imperfectly-separated neighbours, so it is not used.
#
# Since 2026-08-21 there IS a fitted resting rhythm — ``fakefish.loc_model`` +
# ``data/loc_model_params.json``, vendored from eeltracker exactly as the volley model
# was, with ``docs/LOCALIZATION_GENERATIVE_SPEC.md`` as its spec. The RC device already
# uses it live (``firmware/eel_core/loc_rhythm.h``). This ladder is what is left over: it
# generates the LOCALIZATION ITEMS baked into the byte-frozen library, which the SD card's
# programs B and D render from, so replacing it here is not a code change but a
# **library re-export** — and that needs the source recordings, which are not in this repo
# (CLAUDE.md invariant 3 has the four-step sequence).
#
# So the two paths currently disagree, deliberately and visibly: the RC device ticks like
# a fitted eel, the WAV card ticks on this ladder. Closing that is the next re-export, and
# it must be done as one commit — regenerate, verify items 0-6 and EOD_HV come back
# byte-identical, then re-run ``firmware/sync_core.sh``. See TODO.md.
LOC_SYNTH_RATES_HZ = [1.0, 2.0, 3.0, 5.0, 7.0, 10.0]
LOC_SYNTH_CV = 0.3
LOC_MAX_IPI_S = 1.15

# Where localization sits relative to a volley, as a fraction. Read from the SD-path
# levels because those are the pair the codegen renders into Python; the RC path
# expresses the same decision as VOLLEY_AMP_RATIO in stim_levels.h. Used only to
# ANNOTATE how deep a volley's amplitude envelope runs — nothing here clamps to it.
LOC_LEVEL_FRAC = K.LOC_AMPLITUDE / K.VOLLEY_AMPLITUDE

# N_SYNTH_VOLLEYS volleys will not fit on a readable page; the gallery shows this many,
# evenly spaced through the duration-sorted population.
GALLERY_MAX_VOLLEYS = 24


# ==========================================================================
# Extract the real volley population
# ==========================================================================


@dataclass
class RealVolley:
    recording: str
    site: str
    event_id: int
    t: np.ndarray  # pulse times from onset (s), sorted
    ipi_ms: np.ndarray  # inter-pulse intervals (ms), len = n-1
    amp: np.ndarray  # per-pulse peak-to-peak amplitude (max channel)
    n: int
    duration: float
    peak_rate_hz: float


def _good_volleys(
    candidates: Path,
    min_pulses=20,
    max_il=0.35,
    min_gamma=0.6,
    min_peak_hz=100.0,
    max_peak_hz=VOLLEY_MULTIFISH_PEAK_HZ,
    max_peak_frac=0.5,
) -> list[dict]:
    d = json.loads(Path(candidates).read_text())
    return [
        s
        for s in d
        if s["kind"] == "volley"
        and s["single_fish"]
        and s["n_pulses"] >= min_pulses
        and s["spatial_interleave"] < max_il
        and s["gamma_score"] >= min_gamma
        # only trains that actually reach the volley regime shape the model — an
        # 82 Hz "volley" is a fast localization train and biases the peak low.
        and s["peak_rate_hz"] > min_peak_hz
        # ... and a train peaking above ~450 Hz is TWO fish volleying together (the
        # rates add): a multi-fish artifact that must not inflate the single-fish peak.
        and s["peak_rate_hz"] < max_peak_hz
        # ... and a full volley shows its peak EARLY (rise -> peak -> decay); a train
        # whose peak lands late is a partial capture, not a representative volley.
        and s["peak_rate_frac"] <= max_peak_frac
    ]


def extract_population(cfg: ex.Config, candidates: Path) -> list[RealVolley]:
    """Re-read every good single-fish volley's per-pulse time + amplitude."""
    from collections import defaultdict

    good = _good_volleys(candidates)
    by_rec = defaultdict(list)
    for s in good:
        by_rec[(s["site"], s["recording"])].append(s)

    out: list[RealVolley] = []
    for (site, rec_id), scenes in sorted(by_rec.items()):
        h5 = next((cfg.eods_root / site).glob(f"{rec_id}*.h5"))
        rec = ex.open_recording(h5, site, cfg.original_hz_fallback)
        try:
            for s in scenes:
                rows = np.asarray(s["rows"], dtype=np.int64)
                t = rec.times_s[rows]
                order = np.argsort(t)
                rows, t = rows[order], t[order]
                t0 = t - t[0]
                amp = rec.amplitude_vectors(rows).max(axis=1)
                out.append(
                    RealVolley(
                        recording=rec_id,
                        site=site,
                        event_id=int(s["event_id"]),
                        t=t0,
                        ipi_ms=np.diff(t) * 1e3,
                        amp=amp,
                        n=int(rows.size),
                        duration=float(t0[-1]),
                        peak_rate_hz=float(s["peak_rate_hz"]),
                    )
                )
        finally:
            rec.close()
    log.info("extracted %d real volleys", len(out))
    return out


def save_population(pop: list[RealVolley], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    obj = {}
    for i, v in enumerate(pop):
        obj[f"t_{i}"] = v.t
        obj[f"ipi_{i}"] = v.ipi_ms
        obj[f"amp_{i}"] = v.amp
    meta = [
        dict(
            recording=v.recording,
            site=v.site,
            event_id=v.event_id,
            n=v.n,
            duration=v.duration,
            peak_rate_hz=v.peak_rate_hz,
        )
        for v in pop
    ]
    np.savez(path, meta=json.dumps(meta), **obj)


def load_population(path: Path) -> list[RealVolley]:
    z = np.load(path, allow_pickle=True)
    meta = json.loads(str(z["meta"]))
    out = []
    for i, m in enumerate(meta):
        out.append(
            RealVolley(
                recording=m["recording"],
                site=m["site"],
                event_id=m["event_id"],
                t=z[f"t_{i}"],
                ipi_ms=z[f"ipi_{i}"],
                amp=z[f"amp_{i}"],
                n=m["n"],
                duration=m["duration"],
                peak_rate_hz=m["peak_rate_hz"],
            )
        )
    return out


# ==========================================================================
# Missed-pulse artifact handling
# ==========================================================================


def sustained_peak_hz(ipi_ms: np.ndarray, k: int = SUSTAINED_PEAK_WINDOW) -> float:
    """Peak pulse rate that the volley actually HELD, in Hz.

    The max of a ``k``-interval rolling median of the instantaneous rate. A peak that
    only one interval reaches is a detection artifact (or one lucky jitter draw), not a
    physiological burst — and ``1/min(IPI)`` reports exactly that, with a bias that grows
    with the number of intervals drawn. This estimator is insensitive to both, which is
    what makes it comparable between real volleys (~37 intervals) and synthetic ones
    (~125). See the SUSTAINED_PEAK_WINDOW block at the top of this module.

    It is also the quantity ``generate_volley`` samples: ``rate_peak`` is the
    instantaneous rate at t=0, so real and synthetic are measured on the same footing.
    """
    ipi = np.asarray(ipi_ms, dtype=float)
    ipi = ipi[ipi > 0]
    if ipi.size == 0:
        return float("nan")
    rate = 1000.0 / ipi
    if rate.size < k:
        return float(np.median(rate))
    win = np.lib.stride_tricks.sliding_window_view(rate, k)
    return float(np.median(win, axis=1).max())


def flag_missed_pulses(ipi_ms: np.ndarray, factor: float = 1.8) -> np.ndarray:
    """Boolean mask of IPIs that are missed-pulse artifacts (a doubled interval).

    A missed pulse merges two real intervals into one, so the recorded IPI spikes
    ABOVE the smooth local trend (a rate *trough*). We flag an IPI as artifactual
    when it exceeds ``factor`` × the robust local trend (median of its neighbours).
    """
    ipi = np.asarray(ipi_ms, dtype=float)
    if ipi.size < 5:
        return np.zeros(ipi.size, bool)
    trend = ex._moving_median(ipi, max(3, (ipi.size // 6) | 1))
    return ipi > factor * trend


# ==========================================================================
# High-rate overlap demo (task 1)
# ==========================================================================


def _eod_waveform(cfg: ex.Config) -> ex.Waveform:
    tmpl, oh = ex.load_global_template(cfg.resolve_alignment_json())
    return ex.build_waveform(tmpl, oh, cfg)


@app.command("overlap-demo")
def overlap_demo(
    config: Path = typer.Option(_res.DEFAULT_CONFIG, "--config", "-c"),
    verbose: int = typer.Option(2, "--verbose", "-v", count=True),
) -> None:
    """Show what the mean waveform does when pulses overlap at high volley rates."""
    configure_logging(verbose)
    cfg = ex.Config.from_yaml(config)
    w = _eod_waveform(cfg)
    hz = w.playback_hz
    dur_ms = w.duration_us / 1e3
    log.info("waveform %d samp, %.2f ms", w.n, dur_ms)

    FIG_DIR.mkdir(parents=True, exist_ok=True)
    fig, axes = full_page(height_cm=19.0, nrows=5, ncols=1)
    # single pulse
    tw = np.arange(w.n) / hz * 1e3
    axes[0].plot(tw, w.samples_i16 / 32767, color=CATEGORICAL[0], lw=1.2)
    axes[0].set_title(
        f"single mean EOD ({dur_ms:.2f} ms, FWHM {w.fwhm_us / 1e3:.2f} ms)", fontsize=8
    )
    axes[0].set_ylabel("norm")
    axes[0].set_xlabel("time (ms)")

    # constant-rate trains: realistic peak, realized synthetic max, clip onset, clipping
    for ax, ipi_ms in [(axes[1], 4.0), (axes[2], 2.0), (axes[3], 1.31), (axes[4], 1.0)]:
        n = 14
        ipi = np.concatenate([[0], np.full(n - 1, ipi_ms * 1000)]).astype(np.uint32)
        trace = ex.reconstruct_trace(w.samples_i16, ipi, hz, pad_us=1000.0)
        t = np.arange(trace.size) / hz * 1e3
        onsets = ex.onsets_samples(ipi, hz)
        pad = int(round(1000e-6 * hz))
        for o in onsets:
            s = int(o) + pad
            seg = np.zeros_like(trace)
            seg[s : s + w.n] = w.samples_i16 / 32767
            ax.plot(t, seg, color="0.8", lw=0.4)
        ax.plot(t, trace, color=CATEGORICAL[1], lw=1.0)
        ax.axhline(1.0, color="0.4", ls=":", lw=0.7)
        peak_sum = float(trace.max())
        clip = " → CLIPS full-scale" if peak_sum > 1.001 else ""
        ax.set_title(
            f"{1000 / ipi_ms:.0f} Hz (IPI {ipi_ms:.2f} ms) — summed peak {peak_sum:.3f}{clip}",
            fontsize=8,
        )
        ax.set_ylabel("norm")
        ax.set_xlabel("time (ms)")
    fig.suptitle(
        "Mean-waveform overlap vs volley rate (realistic ≤500 Hz clean; clipping onset ~763 Hz)"
    )
    out = FIG_DIR / "overlap_demo.png"
    fig.savefig(out)
    plt.close(fig)
    log.info("wrote %s", out)


# ==========================================================================
# Plot the real population (task: amplitude over time)
# ==========================================================================


def plot_real_amplitude_over_time(pop: list[RealVolley], out: Path) -> None:
    """Grid: every real volley's per-pulse amplitude vs time-from-onset."""
    pop = sorted(pop, key=lambda v: v.duration, reverse=True)
    ncol = 5
    nrow = int(np.ceil(len(pop) / ncol))
    fig, axes = full_page(
        height_cm=2.6 * nrow + 1.0, nrows=nrow, ncols=ncol, squeeze=False
    )
    for i, v in enumerate(pop):
        ax = axes[i // ncol][i % ncol]
        ax.plot(v.t, v.amp, "-o", color=CATEGORICAL[0], ms=1.8, lw=0.5)
        ax.set_title(
            f"{v.site[-3:]} ev{v.event_id}\nn={v.n} {v.duration:.2f}s", fontsize=6
        )
        ax.tick_params(labelsize=5)
    for j in range(len(pop), nrow * ncol):
        axes[j // ncol][j % ncol].axis("off")
    fig.suptitle(
        "Real single-fish volleys — amplitude vs time (all start strong; ramp-up fragmented off)"
    )
    fig.supxlabel("time from onset (s)", fontsize=8)
    fig.supylabel("peak-to-peak amplitude", fontsize=8)
    fig.savefig(out)
    plt.close(fig)
    log.info("wrote %s", out)


# ==========================================================================
# Synthesis
# ==========================================================================


@dataclass
class SyntheticVolley:
    kind: str  # "volley" | "localization"
    label: str
    times_s: np.ndarray  # pulse times from 0
    rel_amp: np.ndarray  # per-pulse relative amplitude (firmware scales globally)
    body_duration_s: float


def _lognormal_ipi(rate_hz: float, cv: float, rng, floor_ms: float = 1.6) -> float:
    """A positive, lognormally-jittered IPI (s) for a target rate, clamped to a floor."""
    mean_ipi = 1.0 / rate_hz
    sigma = np.sqrt(np.log(1 + cv * cv))
    mu = np.log(mean_ipi) - 0.5 * sigma * sigma
    return float(max(np.exp(rng.normal(mu, sigma)), floor_ms / 1000.0))


def _load_volley_model():
    """The vendored sampler, bound to the vendored parameters.

    Deferred import so that a toolchain command which never synthesises (``render``,
    ``build-card``) does not pay for it, and so the vendored module stays a leaf: it
    imports numpy and the standard library only, and nothing in fakefish imports *it*
    except here.
    """
    from fakefish.volley_model import VolleyModel

    return VolleyModel.from_json(VOLLEY_MODEL_JSON)


def _snap_to_grid(times_s: np.ndarray, hz: int, min_samp: int) -> np.ndarray:
    """Quantise pulse times onto the playback sample grid, enforcing a minimum IPI.

    Two jobs, both load-bearing, and they have to happen together:

    * **Quantise.** The device can only place an onset on a sample boundary, and the
      export rounds to samples anyway (``ipi_samples_from_times``). Doing it here instead
      means the population we QC is bit-for-bit the population the device plays, rather
      than a float idealisation of it that rounds differently.
    * **Enforce the floor.** Each pulse is pushed to at least ``min_samp`` after its
      predecessor — and no further. Later pulses keep their own drawn times wherever those
      already clear the floor, so the train RE-SYNCS after a violation instead of
      accumulating a shift. Measured over the population this leaves total duration
      unchanged to 3 decimal places; a naive "add the deficit to everything downstream"
      clamp would stretch the volley and slow its tail.

    Returns integer sample offsets from the first pulse, as float seconds.
    """
    samp = np.round(np.asarray(times_s, float) * hz).astype(np.int64)
    samp -= samp[0]
    for i in range(1, samp.size):
        if samp[i] - samp[i - 1] < min_samp:
            samp[i] = samp[i - 1] + min_samp
    return samp / float(hz)


def generate_volley(model, rng, label: str, hz: int = ex.PLAYBACK_RATE_HZ) -> SyntheticVolley:
    """One synthetic volley, drawn from the vendored model and made playable.

    The model supplies the whole event series — pulse times and per-pulse relative
    amplitude — from a joint draw of ``(r_start, duration, lambda)`` plus a per-volley
    CV2 and amplitude trend. Nothing about the volley's shape is decided here.

    Two transforms turn that event series into something the firmware can emit, and
    both are lossy in a direction the spec explicitly permits:

    * **the sample grid + IPI floor** — see :func:`_snap_to_grid`.
    * **amplitude normalisation to the volley's own peak.** The model's amplitude is
      relative to the volley's MEDIAN pulse, so it runs above 1.0 near onset (median
      max 1.16, 95th percentile 1.78) while firmware ``rel_amp`` encodes 0..1 in a
      byte. Dividing by the per-volley max preserves the measured envelope SHAPE
      exactly and discards only the absolute level — which the spec (§5) is explicit
      is not a trustworthy measurement anyway, and belongs to the amplitude knob.

    What it does NOT do is impose a floor on the envelope. The trend is drawn per volley
    from its measured ECDF, whose tail is long: the quietest pulse reaches ~0.39 of the
    volley's own peak at the 5th percentile, and the deepest single draw in the shipped
    population fades to **0.08**. 3 % of volleys end below the localization level. That is
    the measurement, and a volley is identified by its RATE, not its level — a 300 Hz train
    at 0.4 amplitude is not mistakable for a 5 Hz localization train. (The retired
    ``VOLLEY_DECAY_FLOOR = 0.8`` would have clipped 64 % of volleys. The loc-vs-volley
    separation it was protecting is now a firmware level instead: localization is
    volley / ``volley_amp_ratio``, and that ratio moved 2 -> 4.)

    One caveat travels with that tail, from spec §1.4: recorded amplitude is source
    amplitude x distance attenuation, and a striking fish moves. A volley that fades to 8 %
    is far more likely a fish swimming away from the electrode than an organ winding down.
    Those draws are kept because the spec draws ``trend`` per volley from the ECDF rather
    than fixing it, and truncating a fitted tail by eye is how a model stops being one —
    but see TODO.md if the near-silent tail turns out to matter in the water.
    """
    times, amp = model.sample_volley(rng, VOLLEY_KIND)
    times = _snap_to_grid(times, hz, SYNTH_MIN_IPI_SAMP)
    return SyntheticVolley(
        kind="volley",
        label=label,
        times_s=times,
        rel_amp=np.asarray(amp, float) / float(np.max(amp)),
        body_duration_s=float(times[-1]),
    )


def generate_localization(
    duration_s: float,
    rng,
    label: str,
    rate_hz: float,
    cv: float = LOC_SYNTH_CV,
) -> SyntheticVolley:
    """A standalone localization train at a fixed target average ``rate_hz`` (Hz).

    IPIs are lognormally jittered around ``1/rate_hz`` (``cv``) and clamped under
    ``LOC_MAX_IPI_S`` so a low-rate (1 Hz) train neither overflows the uint16
    sample-IPI nor gets fragmented by the gap-free trim.

    ``rate_hz`` is now REQUIRED. It used to be optional, falling back to a fitted
    resting lognormal; that fit is retired (see ``LOC_SYNTH_RATES_HZ``), and a
    silent fallback to a distribution that no longer exists is worse than an
    argument error.
    """
    times, t = [], 0.0
    while t < duration_s:
        times.append(t)
        t += min(_lognormal_ipi(rate_hz, cv, rng, SYNTH_MIN_IPI_MS), LOC_MAX_IPI_S)
    # NOT snapped to the sample grid, unlike a volley. The snap is there to make the IPI
    # FLOOR exact, and localization runs two orders of magnitude away from it — so here it
    # would only re-round already-rounded numbers, moving every train by up to one sample
    # (20 us). That costs something real: the six localization items come out
    # BYTE-IDENTICAL across a volley-model change, which is what makes such a change
    # reviewable — any localization row that moves in the export diff is a genuine
    # regression rather than noise. Keeping them still is worth more than uniformity.
    times = np.asarray(times)
    return SyntheticVolley(
        kind="localization",
        label=label,
        times_s=times,
        rel_amp=np.ones(times.size),  # localization is uniform full-scale (no decay)
        body_duration_s=duration_s,
    )


def build_population(model, seed: int = 0) -> list[SyntheticVolley]:
    """``N_SYNTH_VOLLEYS`` volleys drawn from the model, plus the localization trains.

    **There is no duration ladder any more.** The old one (log-spaced 0.1-4 s, 3 reps
    each) existed because the only volleys this repo could see were tracker *fragments*,
    whose durations measured the segmentation rather than the animal — so duration had to
    be a design choice. The vendored model fits duration jointly with start rate and decay
    on whole volleys, and its own §3 works through the truncation question, so duration is
    now drawn like everything else and the correlations survive: a volley that is both
    fast and long is not the same object as either alone.

    The pool is large (see ``N_SYNTH_VOLLEYS``) precisely because the RC device draws from
    it uniformly. With 21 items a uniform draw was a lumpy stand-in for the fitted
    distribution; with 100 it is a good one, and flash is nearly free.

    One thing the fitted range does NOT cover: the model tops out around 2.3 s, but a
    volley in the field runs much longer. That is a blind spot of the source analysis, not
    biology — a 25 s analysis window cannot contain a 20 s volley without right-censoring
    it, and censored bursts were dropped rather than fitted. It is left as a known gap
    here rather than papered over by extrapolation; see TODO.md.
    """
    # INDEPENDENT RNG STREAMS for the two families. They used to share one generator, so
    # the localization trains consumed whatever draws were left after the volleys — and any
    # change to the volley model (which changes how many draws a volley takes) silently
    # re-rolled every localization train too, even though not one localization parameter had
    # moved. Separate streams keep a volley-side change confined to the volleys.
    rng_vol = np.random.default_rng(seed)
    rng_loc = np.random.default_rng(seed + 1_000_000)
    out: list[SyntheticVolley] = []
    for i in range(N_SYNTH_VOLLEYS):
        v = generate_volley(model, rng_vol, "")
        # The label carries what the draw produced, not what was asked for — there is no
        # requested duration any more. 2 dp: the population spans 0.15-2.3 s, where 1 dp
        # would collapse the short end into three distinct values.
        v.label = f"synth_volley_{i:03d}_dur{v.times_s[-1]:.2f}s_n{v.times_s.size}"
        out.append(v)
    # a set of localization trains spanning the resting/exploring rate range (1-10 Hz),
    # one per target average rate.
    for r in LOC_SYNTH_RATES_HZ:
        out.append(generate_localization(60.0, rng_loc, f"synth_loc_{r:g}hz", rate_hz=r))
    return out


def net_charge(v: SyntheticVolley, single_pulse_integral: float) -> float:
    """Per-volley accumulated net charge ≈ Σ rel_amp × single-pulse net integral.

    A multi-second monophasic burst injects sustained unidirectional charge that
    between-playback polarity randomisation does NOT null within a burst — the QC
    number the firmware's output stage must handle.
    """
    return float(np.sum(v.rel_amp) * single_pulse_integral)


def save_synthetic(
    pop: list[SyntheticVolley], path: Path, min_ipi_ms: float = 1.6
) -> None:
    obj = {}
    meta = []
    for i, v in enumerate(pop):
        if v.times_s.size > 1:
            dmin = float(np.min(np.diff(v.times_s))) * 1e3
            assert dmin >= min_ipi_ms - 1e-6, (
                f"{v.label}: min IPI {dmin:.3f} ms < floor {min_ipi_ms}"
            )
            # every synthetic VOLLEY must reach the >100 Hz regime (some IPI < 10 ms),
            # so the export's hard peak gate never has to drop one. Fail loud if the
            # peak floor + jitter ever fails to deliver it.
            if v.kind == "volley":
                assert dmin < VOLLEY_PEAK_MAX_IPI_MS + 1e-6, (
                    f"{v.label}: min IPI {dmin:.3f} ms >= {VOLLEY_PEAK_MAX_IPI_MS} ms "
                    f"— volley never reaches >100 Hz peak"
                )
        obj[f"t_{i}"] = v.times_s
        obj[f"a_{i}"] = v.rel_amp
        meta.append(
            dict(
                kind=v.kind,
                label=v.label,
                body_duration_s=v.body_duration_s,
            )
        )
    np.savez(path, meta=json.dumps(meta), **obj)


def load_synthetic(path: Path) -> list[SyntheticVolley]:
    z = np.load(path, allow_pickle=True)
    meta = json.loads(str(z["meta"]))
    return [
        SyntheticVolley(
            kind=m["kind"],
            label=m["label"],
            times_s=z[f"t_{i}"],
            rel_amp=z[f"a_{i}"],
            body_duration_s=m["body_duration_s"],
        )
        for i, m in enumerate(meta)
    ]


# ==========================================================================
# Comparison figures
# ==========================================================================


def plot_synthetic_gallery(synth: list[SyntheticVolley], out: Path) -> None:
    """Grid of synthetic volleys: per-pulse relative amplitude (blue) + instantaneous
    rate (red) over time.

    The population is ``N_SYNTH_VOLLEYS`` strong, which is far too many to read on one
    page, so the gallery shows an evenly-spaced slice through it ORDERED BY DURATION —
    which, because duration is drawn jointly with rate and decay, is also a slice
    through the shape of the population rather than an arbitrary subset."""
    vol = sorted((s for s in synth if s.kind == "volley"), key=lambda v: v.times_s[-1])
    if len(vol) > GALLERY_MAX_VOLLEYS:
        idx = np.round(np.linspace(0, len(vol) - 1, GALLERY_MAX_VOLLEYS)).astype(int)
        vol = [vol[i] for i in idx]
    ncol = 3
    nrow = int(np.ceil(len(vol) / ncol))
    fig, axes = full_page(
        height_cm=2.8 * nrow + 1.0, nrows=nrow, ncols=ncol, squeeze=False
    )
    for i, v in enumerate(vol):
        ax = axes[i // ncol][i % ncol]
        ax.plot(v.times_s, v.rel_amp, "-o", color=CATEGORICAL[0], ms=1.8, lw=0.4)
        ax.set_ylim(0.0, 1.08)
        rate = 1000.0 / (np.diff(v.times_s) * 1e3)
        ax2 = ax.twinx()
        ax2.plot(
            0.5 * (v.times_s[1:] + v.times_s[:-1]),
            rate,
            color=CATEGORICAL[1],
            lw=0.6,
            alpha=0.7,
        )
        ax2.tick_params(labelsize=5)
        ax.set_title(v.label, fontsize=6)
        ax.tick_params(labelsize=5)
    for j in range(len(vol), nrow * ncol):
        axes[j // ncol][j % ncol].axis("off")
    fig.suptitle(
        f"{len(vol)} of {sum(s.kind == 'volley' for s in synth)} synthetic volleys, "
        "by duration · amplitude (blue), rate (red)"
    )
    fig.supxlabel("time (s)", fontsize=8)
    fig.supylabel("relative amplitude", fontsize=8)
    fig.savefig(out)
    plt.close(fig)
    log.info("wrote %s", out)


def plot_comparison(
    real: list[RealVolley],
    synth: list[SyntheticVolley],
    cfg: ex.Config,
    out: Path,
) -> None:
    w = _eod_waveform(cfg)
    synth_vol = [s for s in synth if s.kind == "volley"]
    fig, axes = full_page(height_cm=16.0, nrows=3, ncols=2)

    # A: rate decay real vs synthetic vs model
    ax = axes[0, 0]
    for v in real:
        if v.ipi_ms.size < 5:
            continue
        tmid = 0.5 * (v.t[1:] + v.t[:-1])
        m = ~flag_missed_pulses(v.ipi_ms)
        ax.scatter(tmid[m], 1000 / v.ipi_ms[m], s=2, color="0.6", alpha=0.3)
    for v in synth_vol:
        tb = v.times_s  # the whole volley is the body (no prelude)
        if tb.size < 3:
            continue
        tmid = 0.5 * (tb[1:] + tb[:-1])
        ax.scatter(
            tmid, 1000 / (np.diff(tb) * 1e3), s=2, color=CATEGORICAL[1], alpha=0.3
        )
    # No model curve to overlay: the fitted decay lives in the volley's OWN fractional
    # time (r(f) = r_start·exp(-λf)), so it has no single absolute-time trace. The
    # population of red points IS the model, drawn.
    ax.set_xlim(0, 1.2)
    ax.set_xlabel("time from volley onset (s)")
    ax.set_ylabel("rate (Hz)")
    ax.set_title("A · rate decay · grey real, red synthetic", fontsize=8)

    # B: body IPI histograms
    ax = axes[0, 1]
    real_ipi = np.concatenate(
        [v.ipi_ms[~flag_missed_pulses(v.ipi_ms)] for v in real if v.ipi_ms.size >= 5]
    )
    synth_ipi = np.concatenate(
        [np.diff(v.times_s) * 1e3 for v in synth_vol if v.times_s.size > 3]
    )
    bins = np.logspace(np.log10(1.5), np.log10(80), 40)
    ax.hist(
        real_ipi,
        bins=bins,
        color="0.6",
        alpha=0.6,
        density=True,
        label="real (t<0.8 s)",
    )
    ax.hist(
        synth_ipi,
        bins=bins,
        histtype="step",
        color=CATEGORICAL[1],
        lw=1.5,
        density=True,
        label="synthetic (full)",
    )
    ax.axvline(
        float(np.max(real_ipi)),
        color="0.4",
        ls=":",
        lw=1,
        label=f"real max {np.max(real_ipi):.0f} ms",
    )
    ax.set_xscale("log")
    # Explicit decade-ish ticks: matplotlib's default log minor labels collide into an
    # unreadable smear at this width.
    ax.set_xticks([2, 3, 5, 10, 20, 40, 80])
    ax.set_xticklabels(["2", "3", "5", "10", "20", "40", "80"])
    ax.minorticks_off()
    ax.set_xlabel("volley-body IPI (ms)")
    ax.set_ylabel("density")
    ax.legend(fontsize=6)
    ax.set_title("B · volley IPI · spike at 2 ms = the floor", fontsize=8)

    # C: duration distribution
    ax = axes[1, 0]
    dmax = max(2.2, max((v.times_s[-1] for v in synth_vol), default=2.2))
    dbins = np.linspace(0, dmax * 1.05, 22)
    ax.hist(
        [v.duration for v in real],
        bins=dbins,
        color="0.6",
        alpha=0.6,
        label="real (fragmented)",
    )
    ax.hist(
        [v.times_s[-1] for v in synth_vol],
        bins=dbins,
        histtype="step",
        color=CATEGORICAL[1],
        lw=1.5,
        label="synthetic (total)",
    )
    ax.set_xlabel("total duration (s)")
    ax.set_ylabel("count")
    ax.legend(fontsize=7)
    ax.set_title("C · duration · synth = whole volleys, real = fragments", fontsize=8)

    # D: amplitude — real recorded (distance-confounded) vs the synthetic physiological
    # voltage envelope (full -> VOLLEY_DECAY_FLOOR). The grey real curves are recorded
    # peak-to-peak, which varies with fish distance/orientation (not physiological); the
    # red synth curves are the modelled voltage wind-down each starting strong.
    ax = axes[1, 1]
    for v in real:
        tn = v.t / max(v.t[-1], 1e-9)
        ax.plot(tn, v.amp / np.max(v.amp), color="0.7", lw=0.4, alpha=0.4)
    for v in synth_vol:
        tn = v.times_s / max(v.times_s[-1], 1e-9)
        ax.plot(tn, v.rel_amp, color=CATEGORICAL[1], lw=0.6, alpha=0.5)
    ax.axhline(
        LOC_LEVEL_FRAC, color="0.4", ls=":", lw=0.8,
        label=f"localization level ({LOC_LEVEL_FRAC:.2f} x volley)",
    )
    ax.legend(fontsize=6, loc="lower left")
    ax.set_ylim(0, 1.12)
    ax.set_xlabel("normalised time")
    ax.set_ylabel("relative amplitude")
    ax.set_title("D · amplitude · grey recorded, red fitted envelope", fontsize=8)

    # a long synthetic volley to display as the played trace
    ex_vol = max(synth_vol, key=lambda v: v.times_s[-1])

    # E: a full synthetic volley reconstructed as the played DAC trace
    ax = axes[2, 0]
    ipi_us, rel = _synthetic_to_ipi_amp(ex_vol)
    trace = _reconstruct_with_amp(w, ipi_us, rel)
    t = np.arange(trace.size) / w.playback_hz
    ax.plot(t, trace, color=CATEGORICAL[0], lw=0.5)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("norm")
    ax.set_title(f"E · synthetic '{ex_vol.label}' as played", fontsize=8)

    # F: zoom on the onset — the volley fires at full amplitude from its first pulse
    # (no prelude, no ramp), the property that makes it usable for hand-timed dynamic
    # playback (localization → stop → switch → trigger volley).
    ax = axes[2, 1]
    zoom = t <= 0.12
    ax.plot(t[zoom], trace[zoom], color=CATEGORICAL[0], lw=0.8)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("norm")
    ax.set_title("F · onset zoom: starts strong (no ramp)", fontsize=8)
    fig.suptitle("Synthetic vs real volleys")
    fig.savefig(out)
    plt.close(fig)
    log.info("wrote %s", out)


def _synthetic_to_ipi_amp(v: SyntheticVolley) -> tuple[np.ndarray, np.ndarray]:
    ipi_us = ex.ipi_us_from_times(v.times_s)
    return ipi_us, v.rel_amp


def _reconstruct_with_amp(
    w: ex.Waveform, ipi_us: np.ndarray, rel_amp: np.ndarray, polarity: int = 1
) -> np.ndarray:
    """Reconstruct a trace where each pulse is scaled by its per-pulse relative amplitude."""
    eod = w.samples_i16.astype(np.float64) / 32767.0 * polarity
    onsets = ex.onsets_samples(ipi_us, w.playback_hz)
    pad = int(round(2000e-6 * w.playback_hz))
    total = int(onsets[-1]) + eod.size + pad
    trace = np.zeros(total)
    for o, a in zip(onsets, rel_amp, strict=True):
        s = int(o) + pad
        trace[s : s + eod.size] += eod * float(a)
    return trace


@app.command()
def analyze(
    config: Path = typer.Option(_res.DEFAULT_CONFIG, "--config", "-c"),
    verbose: int = typer.Option(2, "--verbose", "-v", count=True),
) -> None:
    """Re-extract the real volley population used as an independent QC cross-check.

    **This no longer fits anything.** The generative model is fitted upstream in
    eeltracker and vendored in (see the module docstring); all this does is refresh
    ``data/real_volley_population.npz``, the single-fish volleys that ``compare``
    plots the synthetic population against.

    That population is a *different selection* from the model's — extracted by this
    repo's own single-fish criteria (:func:`_good_volleys`), and consisting of tracker
    fragments rather than whole volleys — which is exactly what makes it useful here: it
    catches a wiring mistake between the model and the item table, not a modelling mistake.

    It shrank 41 -> 29 on 2026-08-21, the first time it was regenerated inside this repo.
    The 41-volley file came in with the initial eeltracker import and predates this repo's
    filters: 9 of the 12 events it lost peak ABOVE ``VOLLEY_MULTIFISH_PEAK_HZ`` (450 Hz),
    i.e. they are two fish volleying together with their rates adding — the exact artifact
    that filter exists to remove — and one sat below the 100 Hz volley floor. The surviving
    29 are a strict subset with the same distribution (duration median 0.20 -> 0.22 s,
    sustained peak 348 -> 345 Hz), so nothing downstream moved; the old file was simply
    carrying contamination into the grey reference curve.

    Needs the source recordings and the `export` dependency group.
    """
    configure_logging(verbose)
    cfg = ex.Config.from_yaml(config)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    pop = extract_population(cfg, str(_res.data_file("stimuli_candidates.json")))
    save_population(pop, POP_CACHE)
    plot_real_amplitude_over_time(pop, FIG_DIR / "real_volleys_amplitude.png")
    log.info(
        "real population: %d volleys, sustained peak median %.0f Hz, duration median %.2f s",
        len(pop),
        float(np.median([sustained_peak_hz(v.ipi_ms) for v in pop])),
        float(np.median([v.duration for v in pop])),
    )


@app.command()
def synthesize(
    seed: int = typer.Option(0, "--seed"),
    verbose: int = typer.Option(2, "--verbose", "-v", count=True),
) -> None:
    """Draw the synthetic volley + localization population from the vendored model."""
    configure_logging(verbose)
    FIG_DIR.mkdir(parents=True, exist_ok=True)  # figs/ is gitignored, so it may not exist
    model = _load_volley_model()
    pop = build_population(model, seed=seed)
    save_synthetic(pop, SYNTH_NPZ, min_ipi_ms=SYNTH_MIN_IPI_MS)
    vols = [v for v in pop if v.kind == "volley"]
    log.info(
        "synthesised %d volleys + %d localization trains from %s",
        len(vols),
        len(pop) - len(vols),
        VOLLEY_MODEL_JSON.name,
    )

    # ---- does the drawn population still look like the fitted one? -------------
    # The transforms in generate_volley (sample grid, IPI floor, amplitude
    # normalisation) are all meant to be nearly free. These are the numbers that say
    # so — compare them against docs/VOLLEY_GENERATIVE_SPEC.md §2.1 and §4.
    durs = np.array([v.times_s[-1] for v in vols])
    npulse = np.array([v.times_s.size for v in vols])
    peaks = np.array([sustained_peak_hz(np.diff(v.times_s) * 1e3) for v in vols])
    for name, arr, unit in (
        ("duration", durs, "s"),
        ("pulses", npulse, ""),
        ("sustained peak", peaks, "Hz"),
    ):
        q = np.percentile(arr, [25, 50, 75])
        log.info("  %-15s %8.3f / %8.3f / %8.3f %s (quartiles)", name, *q, unit)

    # ---- playback safety --------------------------------------------------------
    cfg = ex.Config.from_yaml(_res.DEFAULT_CONFIG)
    w = _eod_waveform(cfg)
    min_ipi = min(float(np.min(np.diff(v.times_s))) * 1e3 for v in vols)
    charges = [net_charge(v, w.net_integral) for v in vols]
    # Overlap-clip is the reason SYNTH_MIN_IPI_SAMP exists: the overlap-add engine sums
    # pulses that land within one EOD of each other, and a sum above full scale is
    # saturated, not scaled. Reconstruct every volley and look at the peak.
    peak_sums = [
        float(_reconstruct_with_amp(w, *_synthetic_to_ipi_amp(v)).max()) for v in vols
    ]
    worst = max(peak_sums)
    log.info(
        "min IPI across population: %.2f ms (floor %.2f) — overlap peak %.3f: %s",
        min_ipi,
        SYNTH_MIN_IPI_MS,
        worst,
        "OK" if worst <= 1.001 else "CLIP",
    )
    # The envelope has no floor by design; report how far it actually reaches, against
    # the firmware level that localization sits at.
    quietest = np.array([float(v.rel_amp.min()) for v in vols])
    loc_level = LOC_LEVEL_FRAC
    log.info(
        "quietest pulse per volley: median %.2f, 5th pct %.2f of the volley's own peak — "
        "%.0f%% end below the localization level (%.2f x volley)",
        float(np.median(quietest)),
        float(np.percentile(quietest, 5)),
        100.0 * float(np.mean(quietest < loc_level)),
        loc_level,
    )
    log.info(
        "per-volley net charge (n-pulse, norm·samp): median %.0f max %.0f — "
        "MONOPHASIC: needs a DC-blocking / charge-balanced output stage, not just "
        "between-playback polarity flips",
        float(np.median(charges)),
        float(np.max(charges)),
    )
    # The library item index is an int8_t in the pulse log (PLOG_ABSENT_ITEM = -1), so the
    # whole library must fit 128 items. Fail here, where it is cheap, rather than in the
    # export or — worse — silently in a log column that wraps negative.
    assert len(pop) <= 128, (
        f"{len(pop)} synthetic items alone exceed the 128-item pulse-log ceiling "
        f"(int8_t item field); see N_SYNTH_VOLLEYS"
    )
    plot_synthetic_gallery(pop, FIG_DIR / "synthetic_gallery.png")


@app.command()
def compare(
    config: Path = typer.Option(_res.DEFAULT_CONFIG, "--config", "-c"),
    verbose: int = typer.Option(2, "--verbose", "-v", count=True),
) -> None:
    """Compare the synthetic population against the real volleys."""
    configure_logging(verbose)
    cfg = ex.Config.from_yaml(config)
    real = load_population(POP_CACHE)
    synth = load_synthetic(SYNTH_NPZ)
    plot_comparison(real, synth, cfg, FIG_DIR / "synthetic_vs_real.png")


if __name__ == "__main__":
    app()
