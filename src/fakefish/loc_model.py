"""Reference implementation of the resting localization-discharge rhythm.

Self-contained on purpose: **numpy and the standard library only**, no
eeltracker imports. Copy this file plus ``loc_model_params.json`` into the
fakefish repo and it runs as-is.

    from loc_model import LocalizationModel
    import numpy as np

    m = LocalizationModel.from_json("loc_model_params.json")
    m.rate = 1.0                        # <- the RC "rate" knob
    m.randomness = 1.0                  # <- the RC "randomness" knob
    rng = np.random.default_rng(0)
    st = m.new_state(rng)               # once, at power-on

    while True:
        ipi = m.next_interval(st, rng)  # <- seconds until the next pulse
        sleep(ipi); emit_pulse()
        if m.interrupt(st, rng):        # the fish's own decision to burst
            d = fire_volley()           # the volley model owns this
            m.advance(st, d)            # tell the rhythm how much time passed

**This is an online model, not a sequence generator.** There is one call the
device needs — :meth:`next_interval` — and it costs three exponentials, four
normal draws and a table lookup. Nothing is precomputed and nothing is
replayed, so it can run forever.

THE MODEL, in one paragraph. An interval is ``IPI = Q(z)``, where ``Q`` is the
measured interval quantile function (a 41-entry table) and ``z`` is a standard
normal *state score*. ``z`` is **not** redrawn independently each pulse: it is
an offset plus two or three Ornstein-Uhlenbeck components that relax in
wall-clock time, plus a white per-pulse term. The eel has a discharge rate that
wanders — over a few seconds, over a minute, and over an hour — and only part of
each interval is fresh noise.

WHY NOT A POISSON PROCESS. In real recordings consecutive log-intervals are
correlated: 0.55 at lag 1 and still 0.36 at lag 5. A Poisson process has exactly
zero at every lag. It produces the right *mean* rate and a fish that sounds like
a random number generator: no slow phases, no fast phases, no minute-scale
drift. Three details of this model exist only to fix that, and none should be
simplified away:

1. **The state relaxes in TIME, not in pulses.** After a 3 s silence the fish
   has forgotten more than after three 100 ms intervals. Fitting the same data
   with a per-pulse AR(1) instead costs 366 nats of likelihood.
2. **Two timescales, not one** (another 619 nats): ~3.2 s and ~96 s. A third,
   ~1 h, matters only for runs longer than about ten minutes.
3. **Intervals come from a measured quantile table**, not from a lognormal. The
   real distribution is right-skewed in log space, with a heavy tail of genuine
   multi-second silences.
4. **The noise is peaked, not Gaussian.** Real one-step innovations have excess
   kurtosis +1.5: the fish usually holds its rate almost exactly and
   occasionally jumps. Gaussian noise of the same variance gives the right
   spread and a visibly twitchier fish (median CV2 0.60 against a measured
   0.38). The model draws its noise from a two-scale Gaussian mixture instead —
   one extra uniform and a branch.

The switch out of the resting rhythm — :meth:`interrupt` — is a hazard **per
pulse**, not per second. That is a measurement: over a 13-fold range of current
discharge rate the launch probability moves 5-fold per pulse but 58-fold per
second.

TWO KNOBS, for the RC remote. :attr:`rate` and :attr:`randomness` replace the
Poisson device's two controls and are **orthogonal by construction** — every
pulse-indexed statistic (regularity, autocorrelation, burst rate per pulse)
depends on ``randomness`` alone, and ``rate`` is a pure time dilation on top.

* :attr:`rate` — tempo multiplier, 1.0 = the measured eel (median interval
  317 ms). It divides the intervals **and stretches the time constants with
  them**, which is what the data says a slower fish actually is: fitting the
  memory as ``tau * (own tempo)^gamma`` peaks sharply at ``gamma = 1`` and
  beats ``gamma = 0`` by 413 nats. Halve the rate with ``tau`` fixed instead and
  the texture washes out, because the state would relax between pulses.
* :attr:`randomness` — 0.0 is a metronome at exactly the nominal rate (what the
  old device did with its randomness knob at zero), 1.0 is the measured eel.
  It scales the state score, so it moves regularity and autocorrelation
  together the way an animal does, rather than blending toward a Poisson
  process. Real eels span roughly 0.55-1.5 on this knob.

By default ``rate`` holds the **tick tempo** — 1 / median interval, the number
quoted as an eel's discharge rate. A heavy-tailed interval distribution cannot
hold its median and its mean at once, so sweeping ``randomness`` at a fixed
tempo does move the average pulses per second (2.1x from 0 to 1, as the long
silences appear). Set ``rate_anchor = "mean"`` to hold the dose instead, or
compensate with ``knobs.gain_mean_anchor``. Either way the burst rate stays at
1 launch per 49 resting pulses. The tables that make all that true live under
``knobs`` in the parameter file.

Parameter provenance and every caveat: ``LOCALIZATION_GENERATIVE_SPEC.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Component names in the order the state vector holds them. Dropping "slow"
# (``n_components=2``) folds its variance into the per-deployment offset, which
# is correct for runs short compared with its ~1 h time constant.
COMPONENTS = ("fast", "medium", "slow")


@dataclass
class RestingState:
    """Everything the device has to keep between pulses: k+2 floats."""

    x: np.ndarray                  # OU component values, in score units
    offset: float                  # per-deployment constant, in score units
    pending_dt: float = 0.0        # elapsed time not yet applied to ``x``
    m: float = 0.0                 # last predicted score (offset + sum x)
    extra: dict = field(default_factory=dict)


class LocalizationModel:
    """The resting rhythm of one *Electrophorus*, as an online state update."""

    def __init__(self, params: dict, n_components: int = 3):
        if not 1 <= n_components <= 3:
            raise ValueError("n_components must be 1, 2 or 3")
        self.params = params
        s = params["state"]
        names = COMPONENTS[:n_components]
        self.tau = np.array([s[f"tau_{n}_s"] for n in names], float)
        self.var = np.array([s[f"var_{n}"] for n in names], float)
        # Whatever is not given a time constant becomes a constant offset drawn
        # once per deployment — including the components that were dropped.
        self.var_offset = float(s["var_offset"]) + float(
            sum(s[f"var_{n}"] for n in COMPONENTS[n_components:]))
        self.sd_white = float(np.sqrt(s["var_white"]))
        # Unit-variance, leptokurtic elementary noise (see the module docstring).
        nz = params["noise"]
        self.mix_p = float(nz["mix_p"])
        self.mix_sd = (float(nz["mix_sd_lo"]), float(nz["mix_sd_hi"]))

        mg = params["marginal"]
        self._zk = np.asarray(mg["z_knots"], float)
        self._lk = np.asarray(mg["log_ipi_knots"], float)

        sw = params["switch"]
        self.hazard_b0_ref = float(sw["hazard_logit_intercept"])
        self.hazard_b1 = float(sw["hazard_logit_slope"])
        self._episode_size = _ecdf_sampler(sw["episode_size_ecdf"])
        self._episode_gap = _ecdf_sampler(sw["within_episode_gap_s_ecdf"])
        self._run_pulses = _ecdf_sampler(sw["fast_run_pulses_ecdf"])
        self._run_ipi_ms = _ecdf_sampler(sw["within_run_ipi_ms_ecdf"])

        kn = params.get("knobs")
        self._knob_s = np.asarray(kn["randomness"], float) if kn else np.array([1.0])
        self._knob_gain = {a: np.asarray(kn[f"gain_{a}_anchor"], float)
                           for a in ("median", "mean")} if kn else {
                               "median": np.array([1.0]), "mean": np.array([1.0])}
        self._knob_b0 = np.asarray(kn["hazard_logit_intercept"], float) if kn \
            else np.array([self.hazard_b0_ref])
        #: Which statistic the ``rate`` knob holds fixed as ``randomness`` moves.
        #: "median" (the default) holds the **tick tempo** — 1 / the median
        #: interval, the number quoted as an eel's discharge rate, and the only
        #: one of the two that is stable enough to control: over a 60 s window
        #: the realised average pulse rate varies 2.5-fold on its own, in real
        #: fish and in the model alike, so anchoring it buys no short-run
        #: control. "mean" holds average pulses per second instead — what the
        #: Poisson device's knob meant. Sweeping randomness at a fixed tempo
        #: does change the dose (2.1x from 0 to 1); ``knobs.gain_mean_anchor``
        #: in the parameter file is the factor to compensate with.
        self.rate_anchor = "median"
        self.rate = 1.0
        self.randomness = 1.0

    # ------------------------------------------------------------- the knobs

    @property
    def randomness(self) -> float:
        """0 = metronome at the nominal rate, 1 = the measured eel."""
        return self._randomness

    @randomness.setter
    def randomness(self, value: float) -> None:
        self._randomness = float(np.clip(value, 0.0, self._knob_s[-1]))
        # The gain keeps the median interval where the rate knob says it is, and
        # the intercept keeps the burst rate where the fish had it. Both are
        # measured tables, not corrections: at randomness 0 the calibrated
        # score-to-interval table would otherwise emit its own score-zero knot
        # (523 ms), not the fish's median (317 ms).
        self._gain = float(np.interp(self._randomness, self._knob_s,
                                     self._knob_gain[self.rate_anchor]))
        self.hazard_b0 = float(np.interp(self._randomness, self._knob_s,
                                         self._knob_b0))

    def noise(self, rng: np.random.Generator, size=None) -> np.ndarray | float:
        """Unit-variance noise with the measured excess kurtosis.

        A two-component Gaussian scale mixture: with probability ``mix_p`` the
        draw is the narrow one. Matching only the variance (i.e. using a plain
        normal) reproduces the interval *distribution* and the autocorrelation
        but makes every step visibly jumpier than a real fish.
        """
        lo, hi = self.mix_sd
        sd = np.where(rng.random(size) < self.mix_p, lo, hi)
        return sd * rng.normal(0.0, 1.0, size)

    @classmethod
    def from_json(cls, path: str | Path, n_components: int = 3) -> "LocalizationModel":
        return cls(json.loads(Path(path).read_text()), n_components)

    # ----------------------------------------------------------------- state

    def new_state(self, rng: np.random.Generator, offset: float | None = None,
                  burn_in: int = 40) -> RestingState:
        """A fresh fish, warmed up to its running distribution.

        ``offset`` is that individual-and-deployment's constant tempo shift in
        score units (sd 0.12 with all three components, ~13 % on the median
        interval). Pass a value to pin it — e.g. 0.0 for an average fish.

        ``burn_in`` matters. The components' stationary distribution is defined
        in continuous time, but the model is *observed* at its own pulse times,
        which over-visits the fast side — that is the whole reason the interval
        table is calibrated (see the spec). A cold state therefore starts
        noticeably slow and takes tens of pulses to settle. Forty discarded
        intervals at power-on cost microseconds and remove the transient.
        """
        if offset is None:
            offset = float(np.sqrt(self.var_offset) * self.noise(rng))
        x = np.sqrt(self.var) * self.noise(rng, self.var.size)
        st = RestingState(x=x, offset=float(offset))
        st.m = st.offset + float(st.x.sum())
        for _ in range(burn_in):
            self.next_interval(st, rng)
        return st

    def advance(self, st: RestingState, dt: float) -> None:
        """Tell the rhythm that ``dt`` seconds passed outside its control.

        Call this after a volley, a pause, or anything else that consumed time
        between pulses. It only accumulates; the decay is applied on the next
        :meth:`next_interval`, so calling it twice is the same as calling it
        once with the sum.
        """
        st.pending_dt += float(dt)

    # ------------------------------------------------------------ the update

    def next_interval(self, st: RestingState, rng: np.random.Generator) -> float:
        """Seconds from this pulse to the next one. **The online call.**

        Ages the state by however much time has passed since the last draw,
        takes a fresh score, and maps it through the interval quantile table.
        The interval just returned becomes the next ageing step, which is what
        makes the memory decay in time rather than in pulses.

        Respects :attr:`rate` and :attr:`randomness`.
        """
        # One effective tempo carries both knobs' time dilation, so the state
        # ages in the same stretched clock the intervals are emitted in and the
        # pulse-indexed texture is left untouched.
        tempo = self.rate / self._gain
        dt = st.pending_dt
        if dt > 0:
            a = np.exp(-(dt * tempo) / self.tau)
            st.x = a * st.x + self.noise(rng, self.var.size) * np.sqrt(
                self.var * (1.0 - a * a))
        s = self._randomness
        st.m = s * (st.offset + float(st.x.sum()))
        z = st.m + s * self.sd_white * float(self.noise(rng))
        ipi = self.interval_for_score(z) / tempo
        st.pending_dt = ipi
        return ipi

    def interval_for_score(self, z: float) -> float:
        """Quantile table: score -> interval in seconds, clamped to the table.

        The clamp is deliberate. The table's ends are the 0.02nd and 99.98th
        percentiles of ~99 000 measured intervals (25 ms and ~32 s); beyond
        them there is no measurement to extrapolate from, and the lower end
        coincides with the resting/volley antimode, so the resting rhythm can
        never intrude into volley territory.
        """
        return float(np.exp(np.interp(z, self._zk, self._lk)))

    def interrupt(self, st: RestingState, rng: np.random.Generator) -> bool:
        """Does a fast run start at this pulse? A hazard per pulse, not per second.

        Uses the score the last :meth:`next_interval` predicted, so the decision
        is made from the same state that set the interval — a fish already
        discharging fast is ~5x more likely to burst *per pulse* than a slow one.
        """
        p = 1.0 / (1.0 + np.exp(-(self.hazard_b0 + self.hazard_b1 * st.m)))
        return bool(rng.random() < p)

    # --------------------------------------------------------- interruptions

    def sample_episode(self, rng: np.random.Generator) -> list[tuple[int, float]]:
        """One interruption episode as ``[(pulses_in_run, gap_before_run_s), ...]``.

        The **first** run opens on the pulse that triggered it, so it needs
        ``pulses_in_run - 1`` more; every later run opens ``gap`` seconds after
        the previous run's last pulse and needs all ``pulses_in_run``.

        57 % of episodes are a single run; the rest are trains of runs a few
        hundred ms apart. Run sizes are heavy-tailed — median 2 pulses (a bare
        doublet), 95th percentile 37 — and the ``>= 5``-pulse tail is exactly
        the ``ordinary`` volley of the volley-dynamics model. Feed those to the
        volley sampler; render the doublets yourself.
        """
        n = max(1, int(round(float(self._episode_size(rng)))))
        out = []
        for i in range(n):
            k = max(2, int(round(float(self._run_pulses(rng)))))
            gap = 0.0 if i == 0 else float(self._episode_gap(rng))
            out.append((k, gap))
        return out

    def sample_run_intervals(self, n_pulses: int, rng: np.random.Generator) -> np.ndarray:
        """Crude within-run intervals — a placeholder, not the volley model.

        Draws ``n_pulses - 1`` intervals from the pooled measured within-run
        distribution. It gets the pulse *count* and rough spacing right, which
        is all the validation script needs; a device should hand runs of >= 5
        pulses to the volley model instead, which has their rate profile,
        decay and amplitude envelope.
        """
        return 1e-3 * np.asarray(self._run_ipi_ms(rng, max(n_pulses - 1, 0)), float)

    # ------------------------------------------------------------ convenience

    def sample_train(self, duration_s: float, rng: np.random.Generator,
                     interruptions: bool = True, state: RestingState | None = None):
        """``(times, is_run_pulse)`` for ``duration_s`` seconds. For tests and figures.

        A device never calls this — it calls :meth:`next_interval` in its own
        loop. It exists so the validation script can measure a synthetic train
        with exactly the estimator used on the real ones.
        """
        st = state if state is not None else self.new_state(rng)
        t = 0.0
        times = [0.0]
        is_run = [False]
        while t < duration_s:
            t += self.next_interval(st, rng)
            if t >= duration_s:
                break
            times.append(t)
            is_run.append(False)
            if interruptions and self.interrupt(st, rng):
                spent = 0.0
                for i, (k, gap) in enumerate(self.sample_episode(rng)):
                    if i:
                        # Runs after the first need their own opening pulse; the
                        # first run opens on the resting pulse just emitted.
                        spent += gap
                        times.append(t + spent)
                        is_run.append(True)
                    for dt in self.sample_run_intervals(k, rng):
                        spent += float(dt)
                        times.append(t + spent)
                        is_run.append(True)
                self.advance(st, spent)
                t += spent
        return np.asarray(times), np.asarray(is_run)


def _ecdf_sampler(table: dict):
    """Inverse-CDF sampler over a percentile table."""
    p = np.asarray(table["percentile"], float) / 100.0
    v = np.asarray(table["value"], float)

    def draw(rng: np.random.Generator, size=None):
        return np.interp(rng.random(size), p, v)

    return draw


def _params_path() -> Path:
    """Find the parameter file next to this module, or in the repo layout."""
    here = Path(__file__).resolve().parent
    for cand in (here / "loc_model_params.json",
                 here.parent / "model" / "loc_model_params.json"):
        if cand.exists():
            return cand
    raise FileNotFoundError("loc_model_params.json not found next to loc_model.py")


def _demo() -> None:
    model = LocalizationModel.from_json(_params_path())
    rng = np.random.default_rng(0)
    st = model.new_state(rng)
    ipi = np.array([model.next_interval(st, rng) for _ in range(20000)])
    print(f"20 000 resting intervals: median {1e3 * np.median(ipi):.0f} ms "
          f"({1 / np.median(ipi):.2f} Hz), 5-95 % "
          f"{1e3 * np.percentile(ipi, 5):.0f}-{1e3 * np.percentile(ipi, 95):.0f} ms")
    log = np.log(ipi)
    y = log - log.mean()
    print("  log-interval autocorrelation lag 1..5: "
          + " ".join(f"{np.mean(y[:-k] * y[k:]) / y.var():.3f}" for k in range(1, 6)))
    cv2 = 2 * np.abs(np.diff(ipi)) / (ipi[1:] + ipi[:-1])
    print(f"  CV2 {np.median(cv2):.3f}   (Poisson would be ~1.0 and autocorrelation 0)")
    t, run = model.sample_train(600.0, rng)
    print(f"  10 min train: {t.size} pulses, {run.sum()} of them inside fast runs, "
          f"{t.size / 600:.2f} pulses/s overall")
    print("  knobs (tick tempo held; rate is a pure time dilation):")
    for rate in (0.5, 1.0, 2.0):
        row = []
        for s_knob in (0.0, 0.5, 1.0, 1.5):
            model.rate, model.randomness = rate, s_knob
            # Averaged over fresh states: the ~1 h component has not mixed
            # inside any one run, so a single deployment sits at an offset.
            x = np.concatenate([
                [model.next_interval(st, rng) for _ in range(1500)]
                for st in (model.new_state(rng) for _ in range(12))])
            c = 2 * np.abs(np.diff(x)) / (x[1:] + x[:-1])
            row.append(f"{1 / np.median(x):5.2f} Hz/CV2 {np.median(c):.2f}")
        print(f"    rate {rate:.1f}: " + "  ".join(row))
    model.rate, model.randomness = 1.0, 1.0


if __name__ == "__main__":
    _demo()
