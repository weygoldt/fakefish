"""Model real eel volleys and synthesise a population for Teensy playback.

The tracker fragments real volleys at rapid amplitude transitions, so the volleys
we extract from the pipeline are only the *body* of a discharge — they start
already-strong and are mostly short (median ~0.2 s). A real volley, per field
observation, is richer:

* a **localization prelude** — the excited fish ticks at ~10-50 Hz (≈1/10 of the
  volley voltage) for a second or two beforehand;
* a **voltage ramp** — already at the ~2 ms volley IPI, the discharge ramps up
  over the first few pulses (≈1/10 → 1/4 → full "stunning" pulse);
* a **smooth IPI decay** — the instantaneous rate rises to a peak within the first
  pulses then decays back toward the localization rate (an inverted, left-leaning
  gamma), over 0.5-2 s (sometimes longer). Mid-volley rate *troughs* in the
  recording are missed-pulse detection artifacts, not real slowdowns.

This module (1) extracts the structure of the real population, (2) fits a rough
generative model, and (3) synthesises a population of volleys of varied lengths plus
localization activity, using the one mean EOD waveform.

Every synthetic volley **starts already-strong**: the localization prelude and the
onset voltage ramp are deliberately *not* synthesised. For hand-timed dynamic
playback the user supplies the localization lead-in live (play localization → stop →
switch → trigger the volley), so a volley must be *just the volley* — a high-rate
discharge that fires at full amplitude immediately, with no low-voltage preamble. Over
its course two things vary: the instantaneous rate decays from a per-volley peak toward
a tail rate, and a gentle per-pulse voltage envelope winds the amplitude down from full
to ``VOLLEY_DECAY_FLOOR`` (80 %), beginning at a random point in the volley's first
third. That envelope is a *physiological* relative amplitude — what ``rel_amp`` is for,
distinct from the distance-confounded recorded amplitude we exclude; the absolute output
level, and the localization↔volley amplitude contrast, remain firmware global scales.
The floor stays above the localization level so a decaying volley never dips into
localization territory.

Subcommands::

    fakefish-synth-volleys overlap-demo   # what mean-waveform overlap looks like at high rate
    fakefish-synth-volleys analyze        # extract real population, plot, fit model
    fakefish-synth-volleys synthesize     # generate the synthetic population
    fakefish-synth-volleys compare        # synthetic vs real
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
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
MODEL_JSON = OUT_DIR / "volley_model.json"
SYNTH_NPZ = OUT_DIR / "synthetic_population.npz"

# Realistic single-fish volley peak rate. A real Electrophorus volley peaks at a few
# hundred Hz; anything much above ~400 Hz is TWO fish volleying together (the rates
# add), a multi-fish artifact — NOT a single-fish stimulus. So:
#  - the model is fit only on real volleys peaking below VOLLEY_MULTIFISH_PEAK_HZ, and
#  - the sampled per-volley peak is held to the SUSTAINED band below.
VOLLEY_MULTIFISH_PEAK_HZ = 450.0  # real peak above this = two fish, drop from the fit

# PEAK RATE IS MEASURED AS A *SUSTAINED* RATE, NOT AS 1/min(IPI).
#
# 1/min(IPI) is an EXTREME-VALUE statistic: its expected value grows with the number of
# intervals you draw. Simulating lognormal IPIs at the fitted jitter (CV 0.166) around a
# true 300 Hz, 1/min(IPI) reports 429 Hz over 37 intervals but 463 Hz over 125. Real
# volleys carry ~37 intervals and synthetic ones ~125, so calibrating the synthesis so
# that 1/min(IPI) matches compares unlike with unlike — and silently forces the synthetic
# SUSTAINED rate down to compensate.
#
# That is exactly what happened: the old code deflated the sampled peak by a constant
# PEAK_JITTER_INFLATION = 1.35 (removed). Measured on the real population the raw/sustained
# ratio is 1.133, not 1.35, and the resulting synthetic volleys ran at 283 Hz sustained
# against the real 348 Hz — 18 % slow — while their 1/min(IPI) matched almost exactly.
#
# So the peak is now measured with sustained_peak_hz() on both sides, and the sampled
# distribution is used AS IS with no inflation factor: generate_volley()'s `rate_peak` is
# the instantaneous rate at t=0, which is the same quantity sustained_peak_hz() measures.
SUSTAINED_PEAK_WINDOW = 5         # intervals a "peak" must hold for, to not be one outlier
SYNTH_PEAK_SUSTAINED_LO_HZ = 300.0  # a proper volley reaches at least this
# Ceiling set by PHYSICS, not by taste: EOD_HV is 131 samples = 2.62 ms at 50 kHz, so a
# sustained rate above 1/2.62 ms = 382 Hz means the MEAN IPI is shorter than one EOD and
# the pulses overlap permanently. Exactly 1 of 41 real volleys sustains above that.
SYNTH_PEAK_SUSTAINED_HI_HZ = 381.0
# hard IPI floor (ms): no pulse faster than this. 2.5 ms == a 400 Hz ceiling, which is
# >= the 2 ms field minimum AND hard-caps the realised peak at the single-fish
# ceiling — a 2.0 ms floor would let the fastest jittered pulse reach 500 Hz. The
# deflated distribution spreads the rest down to ~300 Hz.
SYNTH_MIN_IPI_MS = 2.5
# min IPI (ms) of the fastest pulse a volley must contain to count as reaching the
# >100 Hz volley regime — the same 10 ms the export gate tests.
VOLLEY_PEAK_MAX_IPI_MS = 10.0

# Synthetic localization: a set of standalone resting/exploring trains spanning the
# realistic 1-10 Hz range, one per target average rate. Each is lognormally jittered
# (LOC_SYNTH_CV) with IPIs clamped under LOC_MAX_IPI_S so a low-rate (1 Hz) train
# neither overflows the uint16 sample-IPI (65535 samp = 1.31 s) nor gets chopped by
# the gap-free trim (localization_max_gap_s = 1.2 s).
LOC_SYNTH_RATES_HZ = [1.0, 2.0, 3.0, 5.0, 7.0, 10.0]
LOC_SYNTH_CV = 0.3
LOC_MAX_IPI_S = 1.15

# Volley amplitude decay (a per-pulse *relative* voltage envelope, distinct from the
# excluded distance-confounded recorded amplitude). A synthetic volley starts at full
# amplitude and gently winds DOWN to VOLLEY_DECAY_FLOOR of max over its course, mimicking
# the physiological voltage decay of a real discharge. The decay BEGINS at a random point
# within the first VOLLEY_DECAY_START_MAX_FRAC of the volley (so a volley always STARTS
# strong) and reaches the floor at the last pulse. The floor stays well above the
# localization level (a firmware 0.5x scale), so a volley is never mistaken for
# localization — that separation is the point of the 0.8 floor.
VOLLEY_DECAY_FLOOR = 0.8
VOLLEY_DECAY_START_MAX_FRAC = 1.0 / 3.0


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
# The generative model
# ==========================================================================


@dataclass
class VolleyModel:
    """Rough generative model of eel volley + localization discharge.

    Volley instantaneous rate decays from a peak toward a tail rate as
    ``rate(t) = (rate_peak - rate_end) * exp(-t / tau_s) + rate_end`` (an
    inverted, left-leaning gamma in rate). ``rate_peak`` and ``tau_s`` are
    genuinely fit from the real fragments (robust loss, all points). ``rate_end``
    is NOT identifiable from the data (all real fragments are t<0.8 s where the
    rate is still ~68 Hz) — it is a **biological prior** for the volley-tail rate
    before the discharge blends into localization, and is *sampled per volley*
    from ``[rate_end_lo_hz, rate_end_hi_hz]`` at synthesis so the population
    reflects that uncertainty rather than freezing one value.

    Between-volley variability: each synthetic volley draws its own ``rate_peak``
    (lognormal from the real single-fish SUSTAINED peak distribution — see
    ``sustained_peak_hz``, applied with no inflation factor and capped to the
    ``SYNTH_PEAK_SUSTAINED_*`` band) and ``tau`` (around the fitted value). Real volleys
    peaking above ``VOLLEY_MULTIFISH_PEAK_HZ`` are two fish volleying at once (rates add)
    and are excluded — a single eel does not discharge at >400 Hz.

    Every synthetic volley starts already-strong (the onset voltage ramp and
    localization prelude are deliberately not synthesised — see the module docstring)
    and gently winds its per-pulse amplitude down to ``VOLLEY_DECAY_FLOOR`` over its
    course. The absolute output level and the localization↔volley contrast remain
    firmware global scales, not data.
    """

    # volley rate decay — nominal curve (rate_peak/tau fit; rate_end a prior)
    rate_peak_hz: float
    rate_end_hz: float  # PRIOR (not fit); sampled per volley at synthesis
    tau_s: float
    ipi_jitter_cv: float  # multiplicative lognormal jitter on each IPI

    # between-volley variability, on the SUSTAINED peak (see sustained_peak_hz). These
    # are the rate the volley HOLDS at onset, in Hz — not 1/min(IPI), and not deflated.
    peak_log_mu: float = 0.0
    peak_log_sigma: float = 0.0
    peak_lo_hz: float = 300.0  # sustained rate_peak floor
    peak_hi_hz: float = 381.0  # sustained rate_peak cap (one EOD per IPI; see module top)
    tau_lo_s: float = 0.2
    tau_hi_s: float = 0.6
    # the volley-tail rate the decay approaches (a biological PRIOR, not fit — all real
    # fragments are t<0.8 s where the rate is still high); sampled per volley at synthesis.
    rate_end_lo_hz: float = 12.0
    rate_end_hi_hz: float = 60.0

    # playback safety
    min_ipi_ms: float = 2.5  # hard floor: no IPI below this (2.5 ms == 400 Hz ceiling)

    # resting localization (fit, lognormal on IPI ms)
    loc_log_mu: float = 0.0
    loc_log_sigma: float = 0.0
    loc_median_hz: float = 0.0

    def rate_at(
        self, t: np.ndarray, rate_peak=None, rate_end=None, tau=None
    ) -> np.ndarray:
        rp = self.rate_peak_hz if rate_peak is None else rate_peak
        re = self.rate_end_hz if rate_end is None else rate_end
        ta = self.tau_s if tau is None else tau
        return (rp - re) * np.exp(-t / ta) + re


def fit_volley_params(pop: list[RealVolley]) -> dict:
    """Fit the volley-side model parameters from the real population alone.

    Split out from :func:`fit_model` so it can be re-run from the cached population
    (``data/real_volley_population.npz``) without the source recordings or the
    unshipped candidates file — see the ``refit-peaks`` command. The localization
    parameters are the only ones that need anything beyond this cache.

    Uses a robust (soft-L1) loss on ALL clean+artifact points rather than
    one-sided deletion of high-IPI outliers, so missed-pulse spikes are
    down-weighted symmetrically instead of truncating the upper tail (which
    biased ``rate_peak`` upward ~4%). ``rate_end`` is fixed to a nominal prior for
    the display curve; the sampled range is what synthesis uses.
    """
    from scipy.optimize import curve_fit

    ts, rates, cvs, peaks = [], [], [], []
    for v in pop:
        if v.ipi_ms.size < 5:
            continue
        tmid = 0.5 * (v.t[1:] + v.t[:-1])
        ts.append(tmid)
        rates.append(1000.0 / v.ipi_ms)
        trend = ex._moving_median(v.ipi_ms, max(3, (v.ipi_ms.size // 6) | 1))
        good = ~flag_missed_pulses(v.ipi_ms)
        cvs.append(np.std((v.ipi_ms[good] - trend[good]) / trend[good]))
        # The SUSTAINED peak, not the stored 1/min(IPI) — see sustained_peak_hz().
        peaks.append(sustained_peak_hz(v.ipi_ms))
    ts = np.concatenate(ts)
    rates = np.concatenate(rates)
    peaks = np.asarray(peaks, dtype=float)

    def _decay(t, rp, tau):
        return (rp - RATE_END_NOMINAL) * np.exp(-t / tau) + RATE_END_NOMINAL

    RATE_END_NOMINAL = 20.0  # display-curve prior; synthesis samples [lo, hi]
    (rp, tau), _ = curve_fit(
        _decay,
        ts,
        rates,
        p0=[370, 0.4],
        bounds=([100, 0.05], [900, 3.0]),
        loss="soft_l1",
        f_scale=50.0,
        maxfev=40000,
    )
    # `peaks` are the real single-fish SUSTAINED peaks (_good_volleys already dropped
    # multi-fish >450 Hz and late-peak fragments). No inflation factor is applied: the
    # sampled rate_peak IS a sustained rate (the generator's instantaneous rate at t=0),
    # so it is the same quantity, measured the same way, on both sides. Clipping into the
    # band BEFORE taking the log keeps the weak partial fragments in the fit as band-edge
    # mass instead of dragging the mean down to a rate no real volley sustains.
    peak_lo = SYNTH_PEAK_SUSTAINED_LO_HZ
    peak_hi = SYNTH_PEAK_SUSTAINED_HI_HZ
    log_pk = np.log(np.clip(peaks, peak_lo, peak_hi))
    return {
        "rate_peak_hz": float(rp),
        "rate_end_hz": RATE_END_NOMINAL,
        "tau_s": float(tau),
        "ipi_jitter_cv": float(np.clip(np.median(cvs), 0.05, 0.5)),
        "peak_log_mu": float(np.mean(log_pk)),
        "peak_log_sigma": float(np.std(log_pk)),
        "peak_lo_hz": float(peak_lo),
        "peak_hi_hz": float(peak_hi),
        "min_ipi_ms": SYNTH_MIN_IPI_MS,
        "tau_lo_s": float(tau * 0.6),
        "tau_hi_s": float(tau * 1.7),
    }


def fit_model(pop: list[RealVolley], loc_ipi_ms: np.ndarray) -> VolleyModel:
    """Fit the full model: the volley-side parameters plus the localization ones."""
    log_ipi = np.log(loc_ipi_ms)
    return VolleyModel(
        **fit_volley_params(pop),
        loc_log_mu=float(np.mean(log_ipi)),
        loc_log_sigma=float(np.std(log_ipi)),
        loc_median_hz=float(1000.0 / np.exp(np.median(log_ipi))),
    )


def save_model(m: VolleyModel, path: Path) -> None:
    path.write_text(json.dumps(asdict(m), indent=2))


def load_model(path: Path) -> VolleyModel:
    return VolleyModel(**json.loads(Path(path).read_text()))


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


def _volley_decay_envelope(times_s: np.ndarray, rng) -> np.ndarray:
    """A per-pulse volley amplitude envelope: full-scale until a random onset within the
    first ``VOLLEY_DECAY_START_MAX_FRAC`` of the volley, then a linear decay reaching
    ``VOLLEY_DECAY_FLOOR`` at the last pulse. Always starts strong; the floor stays above
    the localization level so the volley reads as a distinct, higher-voltage discharge."""
    n = times_s.size
    total = float(times_s[-1]) if n else 0.0
    if n < 2 or total <= 0.0:
        return np.ones(n)
    t_start = float(rng.uniform(0.0, VOLLEY_DECAY_START_MAX_FRAC * total))
    frac = np.clip((times_s - t_start) / (total - t_start), 0.0, 1.0)
    return 1.0 + (VOLLEY_DECAY_FLOOR - 1.0) * frac


def generate_volley(
    model: VolleyModel, body_duration_s: float, rng, label: str
) -> SyntheticVolley:
    """One synthetic volley — a high-rate discharge that starts strong then gently decays.

    There is no localization prelude and no onset voltage ramp: for hand-timed dynamic
    playback the user supplies the localization lead-in live, so a synthetic volley is
    *just the volley*, firing at full amplitude from its first pulse (this also matches
    the real tracker fragments, which start already-strong). The instantaneous *rate*
    decays from ``rate_peak`` toward ``rate_end`` with time constant ``tau`` (all sampled
    per volley), and a gentle per-pulse amplitude envelope winds the voltage down from
    full to ``VOLLEY_DECAY_FLOOR`` over the volley (starting at a random point in its first
    third — see ``_volley_decay_envelope``).
    """
    floor = model.min_ipi_ms
    # per-volley decay parameters (between-volley variability)
    rate_peak = float(
        np.clip(
            np.exp(rng.normal(model.peak_log_mu, model.peak_log_sigma)),
            model.peak_lo_hz,
            model.peak_hi_hz,
        )
    )
    tau = float(rng.uniform(model.tau_lo_s, model.tau_hi_s))
    rate_end = float(rng.uniform(model.rate_end_lo_hz, model.rate_end_hi_hz))

    times: list[float] = []
    t = 0.0
    while t < body_duration_s:
        rate = float(
            model.rate_at(
                np.array([t]), rate_peak=rate_peak, rate_end=rate_end, tau=tau
            )[0]
        )
        if times:
            t += _lognormal_ipi(rate, model.ipi_jitter_cv, rng, floor)
        if t >= body_duration_s:
            break
        times.append(t)

    times = np.asarray(times) - times[0]
    return SyntheticVolley(
        kind="volley",
        label=label,
        times_s=times,
        rel_amp=_volley_decay_envelope(times, rng),
        body_duration_s=body_duration_s,
    )


def generate_localization(
    model: VolleyModel,
    duration_s: float,
    rng,
    label: str,
    rate_hz: float | None = None,
    cv: float = LOC_SYNTH_CV,
) -> SyntheticVolley:
    """A standalone localization train at a fixed target average ``rate_hz`` (Hz).

    IPIs are lognormally jittered around ``1/rate_hz`` (``cv``) and clamped under
    ``LOC_MAX_IPI_S`` so a low-rate (1 Hz) train neither overflows the uint16 sample-
    IPI nor gets fragmented by the gap-free trim. If ``rate_hz`` is None, the fitted
    resting distribution is used instead (the wide real-localization lognormal).
    """
    times, t = [], 0.0
    while t < duration_s:
        times.append(t)
        if rate_hz is not None:
            ipi = _lognormal_ipi(rate_hz, cv, rng, model.min_ipi_ms)
        else:
            ipi = float(np.exp(rng.normal(model.loc_log_mu, model.loc_log_sigma)) / 1000.0)
        t += min(ipi, LOC_MAX_IPI_S)
    times = np.asarray(times)
    return SyntheticVolley(
        kind="localization",
        label=label,
        times_s=times,
        rel_amp=np.ones(times.size),  # localization is uniform full-scale (no decay)
        body_duration_s=duration_s,
    )


def build_population(model: VolleyModel, seed: int = 0) -> list[SyntheticVolley]:
    """A population of synthetic volleys (varied body lengths, all starting
    already-strong) + localization trains. Labels carry the played duration."""
    # INDEPENDENT RNG STREAMS for the two families. They used to share one generator, so
    # the localization trains consumed whatever draws were left after the volleys — and any
    # change to the volley model (which changes how many draws a volley takes) silently
    # re-rolled every localization train too, even though not one localization parameter had
    # moved. Separate streams keep a volley-side change confined to the volleys.
    rng_vol = np.random.default_rng(seed)
    rng_loc = np.random.default_rng(seed + 1_000_000)
    # Body lengths span 0.1 s to 4 s, LOG-SPACED (each step ~1.85x the last).
    #
    # The range comes from the owner's field observation, not from the recorded population:
    # real volleys run from short bursts up to ~20 s, and the volleys in
    # real_volley_population.npz (median 0.20 s) are TRACKER FRAGMENTS — truncated, and
    # biased short because genuinely long volleys are rare. So their duration distribution
    # is evidence about the tracker, not about the animal, and must not set this ladder.
    # (Their RATE is trustworthy and does set the peak — see sustained_peak_hz.)
    #
    # Log spacing, not linear: the range spans 40x, so linear steps would spend almost every
    # item on the long end. Log steps give equal resolution per octave, which also keeps the
    # rare long volleys from crowding out the short strong bursts that dominate in nature.
    #
    # The short end is deliberately BELOW one tau (~0.31 s): a 0.1 s body is cut off after
    # ~0.3 tau, so its rate barely decays and it plays as a short burst held near peak. That
    # used to be excluded as "truncated" (the ladder started at 0.6 s), but a brief
    # full-rate burst is a real and common discharge, and it is the strong-event end of the
    # range the experiment cares about.
    body_lengths = [0.1, 0.2, 0.35, 0.65, 1.2, 2.2, 4.0]
    out: list[SyntheticVolley] = []
    for L in body_lengths:
        for rep in range(3):
            v = generate_volley(model, L, rng_vol, "")
            # 2 dp on the body length: the log-spaced ladder has 0.35 s and 0.65 s steps,
            # which 1 dp would round to "0.3"/"0.7" and misreport in the provenance.
            v.label = f"synth_volley_body{L:.2f}s_tot{v.times_s[-1]:.2f}s_{rep}"
            out.append(v)
    # a set of localization trains spanning the resting/exploring rate range (1-10 Hz),
    # one per target average rate.
    for r in LOC_SYNTH_RATES_HZ:
        out.append(generate_localization(model, 60.0, rng_loc, f"synth_loc_{r:g}hz", rate_hz=r))
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
# Localization extraction + comparison figures
# ==========================================================================


def extract_localization_ipi(cfg: ex.Config, candidates: Path) -> np.ndarray:
    from collections import defaultdict

    d = json.loads(Path(candidates).read_text())
    loc = [s for s in d if s["kind"] == "localization" and s["single_fish"]]
    by_rec = defaultdict(list)
    for s in loc:
        by_rec[(s["site"], s["recording"])].append(s)
    ipis = []
    for (site, rec_id), scenes in by_rec.items():
        h5 = next((cfg.eods_root / site).glob(f"{rec_id}*.h5"))
        rec = ex.open_recording(h5, site, cfg.original_hz_fallback)
        try:
            for s in scenes:
                tt = np.sort(rec.times_s[np.asarray(s["rows"])])
                ipis.append(np.diff(tt) * 1e3)
        finally:
            rec.close()
    return np.concatenate(ipis)


def plot_model_fit(pop: list[RealVolley], model: VolleyModel, out: Path) -> None:
    fig, axes = full_page(height_cm=7.0, nrows=1, ncols=2)
    ax = axes[0]
    for v in pop:
        if v.ipi_ms.size < 5:
            continue
        tmid = 0.5 * (v.t[1:] + v.t[:-1])
        missed = flag_missed_pulses(v.ipi_ms)
        ax.scatter(tmid[~missed], 1000 / v.ipi_ms[~missed], s=3, color="0.6", alpha=0.4)
        ax.scatter(
            tmid[missed],
            1000 / v.ipi_ms[missed],
            s=8,
            color=CATEGORICAL[1],
            marker="x",
            alpha=0.6,
        )
    tt = np.linspace(0, 1.2, 200)
    ax.plot(
        tt,
        model.rate_at(tt),
        color=CATEGORICAL[0],
        lw=2,
        label=f"model: peak {model.rate_peak_hz:.0f} Hz, τ={model.tau_s:.2f}s (fit); "
        f"tail {model.rate_end_lo_hz:.0f}–{model.rate_end_hi_hz:.0f} Hz (prior)",
    )
    ax.axvline(0.8, color="0.4", ls=":", lw=1, label="real-data support limit (~0.8 s)")
    ax.axhline(
        model.loc_median_hz,
        color=CATEGORICAL[2],
        ls="--",
        lw=1,
        label=f"localization median {model.loc_median_hz:.0f} Hz",
    )
    ax.set_xlabel("time from volley onset (s)")
    ax.set_ylabel("instantaneous rate (Hz)")
    ax.legend(fontsize=7)
    ax.set_title(
        "Volley rate decay (grey real · ✕ missed-pulse · line model)", fontsize=8
    )

    ax = axes[1]
    li = extract_localization_ipi(
        ex.Config.from_yaml(_res.DEFAULT_CONFIG),
        str(_res.data_file("stimuli_candidates.json")),
    )
    ax.hist(
        li,
        bins=np.logspace(np.log10(20), np.log10(2000), 40),
        color=CATEGORICAL[2],
        alpha=0.7,
    )
    ax.axvline(
        np.exp(model.loc_log_mu),
        color="k",
        lw=1,
        label=f"median {model.loc_median_hz:.0f} Hz",
    )
    ax.set_xscale("log")
    ax.set_xlabel("localization IPI (ms)")
    ax.set_ylabel("count")
    ax.legend(fontsize=7)
    ax.set_title("Resting localization IPI (lognormal)", fontsize=8)
    fig.suptitle("Volley + localization model fit")
    fig.savefig(out)
    plt.close(fig)
    log.info("wrote %s", out)


def plot_synthetic_gallery(synth: list[SyntheticVolley], out: Path) -> None:
    """Grid of every synthetic volley's per-pulse relative amplitude (blue — starts full,
    decays to VOLLEY_DECAY_FLOOR) with the instantaneous rate (red) over time."""
    vol = [s for s in synth if s.kind == "volley"]
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
        "Synthetic volleys — relative amplitude (blue) + instantaneous rate (red) over time"
    )
    fig.supxlabel("time (s)", fontsize=8)
    fig.supylabel("relative amplitude", fontsize=8)
    fig.savefig(out)
    plt.close(fig)
    log.info("wrote %s", out)


def plot_comparison(
    real: list[RealVolley],
    synth: list[SyntheticVolley],
    model: VolleyModel,
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
    tt = np.linspace(0, 2.0, 300)
    ax.plot(tt, model.rate_at(tt), color=CATEGORICAL[0], lw=2)
    ax.set_xlim(0, 1.2)
    ax.set_xlabel("time from volley onset (s)")
    ax.set_ylabel("rate (Hz)")
    ax.set_title("A · rate decay (grey real · red synth · line model)", fontsize=8)

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
    ax.set_xlabel("volley-body IPI (ms)")
    ax.set_ylabel("density")
    ax.legend(fontsize=6)
    ax.set_title(
        "B · volley IPI (synth tail > real max = model extrapolation)", fontsize=8
    )

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
    ax.set_title("C · duration (synth extends past fragments)", fontsize=8)

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
    ax.axhline(VOLLEY_DECAY_FLOOR, color="0.4", ls=":", lw=0.8)
    ax.set_ylim(0, 1.12)
    ax.set_xlabel("normalised time")
    ax.set_ylabel("relative amplitude")
    ax.set_title(
        "D · amplitude (grey real = recorded/distance · red synth = physiological decay)",
        fontsize=8,
    )

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
    """Extract the real volley population, plot it, and fit the generative model."""
    configure_logging(verbose)
    cfg = ex.Config.from_yaml(config)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    pop = extract_population(cfg, str(_res.data_file("stimuli_candidates.json")))
    save_population(pop, POP_CACHE)
    plot_real_amplitude_over_time(pop, FIG_DIR / "real_volleys_amplitude.png")
    loc_ipi = extract_localization_ipi(cfg, str(_res.data_file("stimuli_candidates.json")))
    model = fit_model(pop, loc_ipi)
    save_model(model, MODEL_JSON)
    log.info(
        "model: peak=%.0fHz end=%.0fHz tau=%.2fs jitter_cv=%.2f loc_median=%.0fHz",
        model.rate_peak_hz,
        model.rate_end_hz,
        model.tau_s,
        model.ipi_jitter_cv,
        model.loc_median_hz,
    )
    plot_model_fit(pop, model, FIG_DIR / "model_fit.png")


@app.command("refit-peaks")
def refit_peaks(
    verbose: int = typer.Option(2, "--verbose", "-v", count=True),
) -> None:
    """Re-fit the VOLLEY-side model parameters from the cached real population.

    Unlike ``analyze`` this needs **no source recordings** and no candidates file — it
    reads ``data/real_volley_population.npz``, which is versioned. The localization
    parameters are carried over from the existing model unchanged, because they are the
    only ones that cannot be derived from that cache.

    Use it after changing how the peak rate is measured (``sustained_peak_hz``) or the
    ``SYNTH_PEAK_SUSTAINED_*`` band. Follow with ``synthesize`` and then
    ``fakefish-export`` to carry the change into the firmware library.
    """
    configure_logging(verbose)
    pop = load_population(POP_CACHE)
    old = load_model(MODEL_JSON)
    fitted = fit_volley_params(pop)
    new = VolleyModel(
        **fitted,
        loc_log_mu=old.loc_log_mu,
        loc_log_sigma=old.loc_log_sigma,
        loc_median_hz=old.loc_median_hz,
    )
    save_model(new, MODEL_JSON)
    log.info("re-fit from %d cached real volleys (localization params carried over)", len(pop))
    for f in ("rate_peak_hz", "tau_s", "ipi_jitter_cv", "peak_log_mu",
              "peak_log_sigma", "peak_lo_hz", "peak_hi_hz"):
        o, n = getattr(old, f), getattr(new, f)
        mark = "" if abs(n - o) < 1e-9 else "   <-- CHANGED"
        log.info("  %-16s %10.4f -> %10.4f%s", f, o, n, mark)
    log.info(
        "sampled peak median %.1f Hz -> %.1f Hz",
        float(np.exp(old.peak_log_mu)),
        float(np.exp(new.peak_log_mu)),
    )


@app.command()
def synthesize(
    seed: int = typer.Option(0, "--seed"),
    verbose: int = typer.Option(2, "--verbose", "-v", count=True),
) -> None:
    """Generate the synthetic volley + localization population from the fitted model."""
    configure_logging(verbose)
    model = load_model(MODEL_JSON)
    pop = build_population(model, seed=seed)
    save_synthetic(pop, SYNTH_NPZ, min_ipi_ms=model.min_ipi_ms)
    nvol = sum(v.kind == "volley" for v in pop)
    log.info("synthesised %d volleys + %d localization trains", nvol, len(pop) - nvol)

    # playback-safety QC (validator asks): min IPI floor + per-volley net charge.
    cfg = ex.Config.from_yaml(_res.DEFAULT_CONFIG)
    w = _eod_waveform(cfg)
    vols = [v for v in pop if v.kind == "volley"]
    min_ipi = min(float(np.min(np.diff(v.times_s))) * 1e3 for v in vols)
    charges = [net_charge(v, w.net_integral) for v in vols]
    peak_sums = [
        float(_reconstruct_with_amp(w, *_synthetic_to_ipi_amp(v)).max()) for v in vols
    ]
    log.info(
        "min IPI across population: %.2f ms (floor %.2f) — no overlap-clip: %s",
        min_ipi,
        model.min_ipi_ms,
        "OK" if max(peak_sums) <= 1.001 else f"CLIP {max(peak_sums):.3f}",
    )
    log.info(
        "per-volley net charge (n-pulse, norm·samp): median %.0f max %.0f — "
        "MONOPHASIC: needs a DC-blocking / charge-balanced output stage, not just "
        "between-playback polarity flips",
        float(np.median(charges)),
        float(np.max(charges)),
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
    model = load_model(MODEL_JSON)
    plot_comparison(real, synth, model, cfg, FIG_DIR / "synthetic_vs_real.png")


if __name__ == "__main__":
    app()
