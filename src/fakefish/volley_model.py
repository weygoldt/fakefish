"""Reference sampler for one synthetic *Electrophorus* hunting volley.

Self-contained on purpose: **numpy and the standard library only**, no
eeltracker imports. Copy this file plus ``volley_model_params.json`` into the
fakefish repo and it runs as-is.

    from volley_model import VolleyModel
    import numpy as np

    m = VolleyModel.from_json("volley_model_params.json")
    rng = np.random.default_rng(0)

    t, amp = m.sample_volley(rng)      # one volley: times [s], relative amplitude

One call is **one volley** — a single continuous burst, the thing a button press
should produce. The device's own localization rhythm is not this model's
business: it keeps ticking, and the button interrupts it with a volley.

**The output is an event time series, nothing more**: ``t`` is pulse times in
seconds starting at 0 and ending at the volley's duration, ``amp`` is the
per-pulse **relative** amplitude with 1.0 = the volley's own median pulse.
Feed those straight to a synthesiser that owns its pulse waveform, and scale
``amp`` by whatever absolute amplitude knob the device uses. :meth:`render` is a
convenience for *looking* at the result — it convolves the events with a
template so the figures can show a waveform — and is not part of the model.

THE MODEL, in one paragraph. A volley starts at its peak rate — there is no ramp
— and its rate decays log-linearly over its own duration,
``r(f) = r_start·exp(-λf)`` with ``f`` the fraction elapsed. Pulse times come
from *integrating* that rate with a small multiplicative jitter, **not** from a
Poisson process: real volleys are nearly clockwork (measured CV2 ≈ 0.12, where
Poisson would be 1.0). Amplitude decays gently and smoothly across the volley
(median −22 % start to end) with only ~1.4 % pulse-to-pulse jitter.

Two volley strengths are fitted, selectable with ``kind``:

* ``"strong"`` (default) — the extreme volleys this analysis selected for:
  ~393 Hz start, ~0.47 s, ~88 pulses. What a "fire a hunting volley" button
  should emit.
* ``"ordinary"`` — the everyday volleys that occur alongside them: ~134 Hz,
  ~0.10 s, ~12 pulses.

Parameter provenance and every caveat: ``VOLLEY_GENERATIVE_SPEC.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# CV2 = 2|I(i+1)-I(i)| / (I(i+1)+I(i)); 0 = clockwork, 1 = Poisson. For small
# multiplicative jitter CV2 ~ |ln I(i+1) - ln I(i)|, whose median is 0.6745 * sd.
# So a target CV2 fixes the sd of the consecutive log-interval difference.
CV2_TO_LOGDIFF_SD = 1.0 / 0.6745

KINDS = ("strong", "ordinary")

# A volley is defined as at least this many pulses (the same floor the analysis
# used to detect one). A short, slow parameter draw can render fewer than that —
# the sampler redraws rather than returning something that is not a volley.
MIN_PULSES = 5
MAX_REDRAWS = 20


@dataclass
class _MVN:
    """Joint (r_start, log duration, lambda) distribution for one volley class."""

    mean: np.ndarray
    cov: np.ndarray
    clip_lo: np.ndarray
    clip_hi: np.ndarray

    def draw(self, rng: np.random.Generator) -> tuple[float, float, float]:
        x = np.clip(rng.multivariate_normal(self.mean, self.cov),
                    self.clip_lo, self.clip_hi)
        return float(x[0]), float(np.exp(x[1])), float(x[2])


def _ecdf_sampler(table: dict):
    """Inverse-CDF sampler over a percentile table."""
    p = np.asarray(table["percentile"], float) / 100.0
    v = np.asarray(table["value"], float)

    def draw(rng: np.random.Generator, size=None):
        return np.interp(rng.random(size), p, v)

    return draw


class VolleyModel:
    """Sampler for one synthetic volley, parameterised from real recordings."""

    def __init__(self, params: dict):
        self.params = params
        self._mvn = {k: self._load_mvn(f"{k}_volley") for k in KINDS}
        self._cv2 = {k: _ecdf_sampler(params["timing_jitter"][f"cv2_{k}_ecdf"])
                     for k in KINDS}
        # Amplitude comes from the RAW-cutout measurement, not from the
        # PositionFeatures estimator: the estimator's occasional single-pulse
        # failures drag a plain standard deviation up to ~23 % jitter, 15x the
        # truth. See volley_amplitude.py.
        raw = params["amplitude_raw"]
        # Fixed, not drawn per volley: the spread across real volleys is
        # dominated by measurement noise (jitter falls with loudness, down to
        # ~0.003 for the loudest). The median here is already an upper bound —
        # set ``model.amp_log10_jitter = 0.003`` for a close, loud fish.
        self.amp_log10_jitter = raw["pulse_to_pulse_log10_jitter_sd_robust"]["q"]["50"]
        self._amp_trend = _ecdf_sampler(raw["within_burst_log10_trend_ecdf"])
        sub = params["rate_substructure"]
        self.sub_sd = sub["log_rate_residual_sd"]["q"]["50"]
        self.sub_tau_frac = sub["correlation_time_fraction_of_burst"]["q"]["50"]

    @classmethod
    def from_json(cls, path: str | Path) -> "VolleyModel":
        return cls(json.loads(Path(path).read_text()))

    def _load_mvn(self, key: str) -> _MVN:
        d = self.params[key]
        return _MVN(np.asarray(d["mean"], float), np.asarray(d["cov"], float),
                    np.asarray(d["clip_lo"], float), np.asarray(d["clip_hi"], float))

    # ----------------------------------------------------------------------

    def draw_parameters(self, rng: np.random.Generator, kind: str = "strong") -> dict:
        """The three numbers that define one volley, plus its texture knobs.

        ``(r_start, log D, lambda)`` are drawn jointly so their correlations
        survive — a volley that is both fast and long is not the same object as
        either alone — and clipped to the 1st-99th percentile box so an extreme
        normal draw cannot produce something never observed.
        """
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
        r_start, duration, lam = self._mvn[kind].draw(rng)
        return dict(
            kind=kind, r_start_hz=r_start, duration_s=duration, lam=lam,
            # Regularity is drawn per volley, not fixed: real ones run from
            # near-clockwork (CV2 0.03) to quite ragged (0.34).
            cv2=float(np.clip(self._cv2[kind](rng), 0.01, 1.0)),
            amp_trend_log10=float(self._amp_trend(rng)),
        )

    def sample_volley(self, rng: np.random.Generator, kind: str = "strong",
                      params: dict | None = None, substructure: bool = True):
        """One volley: ``(times, amplitude)``, times starting at 0.

        Times are produced by walking the rate curve — take the current
        instantaneous interval, jitter it, step forward — which keeps the train
        as regular as the real thing. Drawing exponential intervals instead (a
        Poisson process) would give the same mean rate and the wrong texture.

        Pass ``params`` from :meth:`draw_parameters` to render a volley whose
        parameters you chose or reused; otherwise a fresh one is drawn — and
        redrawn, up to ``MAX_REDRAWS`` times, if it renders fewer than
        ``MIN_PULSES`` pulses. With explicit ``params`` the result is returned
        as-is, however short: your parameters, your call.
        """
        if params is None:
            for _ in range(MAX_REDRAWS):
                p = self.draw_parameters(rng, kind)
                t, a = self._render_train(rng, p, substructure)
                if t.size >= MIN_PULSES:
                    return t, a
            return t, a
        return self._render_train(rng, params, substructure)

    def _render_train(self, rng: np.random.Generator, params: dict,
                      substructure: bool = True):
        p = params
        r0, D, lam = p["r_start_hz"], p["duration_s"], p["lam"]

        # Slow multiplicative wander of the rate around the fitted decay (an
        # Ornstein-Uhlenbeck factor), mean-corrected so it does not inflate the
        # pulse count that lambda was matched to.
        sub_sd = self.sub_sd if substructure else 0.0
        tau_sub = max(self.sub_tau_frac * D, 1e-6)
        # The measured CV2 already CONTAINS that wander, so the independent
        # per-pulse jitter is the remainder, not an addition on top: injecting
        # both at full strength makes every synthetic volley raggeder than any
        # real one. Budget the variance between the two sources.
        n_expect = max(r0 * D * (1 - np.exp(-lam)) / max(lam, 1e-6), 2.0)
        a_typ = np.exp(-(D / n_expect) / tau_sub)
        var_target = (CV2_TO_LOGDIFF_SD * p["cv2"]) ** 2
        var_ou = 2.0 * sub_sd ** 2 * (1.0 - a_typ)
        sigma = float(np.sqrt(max(var_target - var_ou, 1e-8) / 2.0))

        u = float(rng.normal(0.0, sub_sd)) if sub_sd > 0 else 0.0
        times, t, prev = [0.0], 0.0, 0.0
        while True:
            rate = r0 * np.exp(-lam * min(t / D, 1.0)) if D > 0 else r0
            if sub_sd > 0:
                a = np.exp(-(t - prev) / tau_sub)
                u = a * u + rng.normal(0.0, sub_sd * np.sqrt(max(1 - a * a, 0.0)))
                rate *= np.exp(u - 0.5 * sub_sd ** 2)
            rate = max(rate, 1.0)
            prev = t
            t += (1.0 / rate) * float(np.exp(rng.normal(0.0, sigma)))
            if t > D:
                break
            times.append(t)
        times = np.asarray(times)

        # Amplitude decays gently and smoothly across the volley. Centred on
        # f = 0.5 so the result has median 1, rather than first-pulse 1.
        f = times / D if D > 0 else np.zeros_like(times)
        env = 10 ** (p["amp_trend_log10"] * (f - 0.5))
        amp = env * 10 ** rng.normal(0.0, self.amp_log10_jitter, times.size)
        return times, amp

    def render(self, times: np.ndarray, amps: np.ndarray, template: np.ndarray,
               samplerate_hz: float, tail_s: float = 0.01) -> np.ndarray:
        """Place one extracted pulse ``template`` at every time, scaled by ``amps``.

        Illustration only. A synthesiser that owns its pulse waveform wants the
        ``(times, amps)`` event series, not this.
        """
        n = int((float(times[-1]) + tail_s) * samplerate_hz) + template.size
        sig = np.zeros(n)
        for t, a in zip(times, amps):
            i = int(round(t * samplerate_hz))
            if 0 <= i < n - template.size:
                sig[i:i + template.size] += a * template
        return sig


def _demo():
    here = Path(__file__).resolve().parent
    model = VolleyModel.from_json(here.parent / "model" / "volley_model_params.json")
    rng = np.random.default_rng(0)
    for kind in KINDS:
        p = model.draw_parameters(rng, kind)
        t, a = model.sample_volley(rng, params=p)
        ipi = np.diff(t) * 1e3
        print(f"{kind:9s} {p['r_start_hz']:5.0f} Hz start, "
              f"{1e3 * p['duration_s']:6.1f} ms, lambda {p['lam']:.2f} -> "
              f"{t.size:4d} pulses, IPI {ipi[0]:.2f} -> {ipi[-1]:.2f} ms, "
              f"amplitude {a[0]:.2f} -> {a[-1]:.2f}")


if __name__ == "__main__":
    _demo()
