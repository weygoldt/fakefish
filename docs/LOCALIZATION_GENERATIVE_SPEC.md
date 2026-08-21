# Generative spec for the resting localization discharge of *Electrophorus*

**Purpose.** Everything the fakefish playback device needs to tick along like a
resting eel: how long to wait for the next pulse, given where it is now. Fitted
to 29 h of single-eel stretches mined from the FLONA 2025 field dataset — 1 761
slices, 99 010 resting intervals, 42 recordings, 15 sites — not guessed.

**The unit is one interval.** This is an **online** model: the device calls it
once per pulse, forever. It is not a sequence generator and there is nothing to
precompute. The state is five floats; the update is a handful of `expf` calls
and a 41-entry table lookup.

**Scope.** The output is an interval in seconds, plus a yes/no on whether the
fish interrupts itself with a fast run. The remote's **rate** and **randomness**
knobs map onto it directly — see §5. Pulse waveform and amplitude belong to
the synthesiser. Hunting volleys belong to
[`../volley_dynamics/VOLLEY_GENERATIVE_SPEC.md`](../volley_dynamics/VOLLEY_GENERATIVE_SPEC.md);
this model just tells you *when* one starts and hands time back afterwards.

**Three files to copy** (nothing else — `loc_model.py` imports only numpy and
the standard library):

| file | what it is |
| --- | --- |
| `scripts/loc_model.py` | reference implementation, runnable as-is |
| `model/loc_model_params.json` | every fitted number, with tables |
| this file | the model, the provenance, and the caveats |

```python
from loc_model import LocalizationModel
import numpy as np

m = LocalizationModel.from_json("loc_model_params.json")
rng = np.random.default_rng(0)
state = m.new_state(rng)                  # once, at power-on

while True:
    ipi = m.next_interval(state, rng)     # seconds until the next pulse
    sleep(ipi); emit_pulse()
    if m.interrupt(state, rng):           # the fish decides to burst
        d = fire_volley()
        m.advance(state, d)               # time passed outside the rhythm
```

---

## 1. The model

An interval is

```
z    = offset + x_fast + x_medium + x_slow + sd_white * e        # the state score
IPI  = Q(z)                                                      # table lookup
```

`Q` is the measured interval quantile function (§3). Each `x` is an
Ornstein–Uhlenbeck component that relaxes in **wall-clock time**: after waiting
`dt` seconds,

```
a  = exp(-dt / tau)
x <- a * x + sqrt(var * (1 - a^2)) * e
```

and `dt` is the interval just emitted. `e` is unit-variance noise from §4 — not
Gaussian. `offset` is drawn once per deployment and never changes.

That is the whole model. It says: *the eel has a discharge rate that wanders on
a few-second and a ~minute timescale, and each pulse adds a bit of fresh
jitter on top.*

### What the Poisson process gets wrong

Everything above exists to fix one thing. In real recordings consecutive
log-intervals are correlated — **0.55 at lag 1, 0.42 at lag 3, 0.36 at lag 5,
0.27 at lag 10, still 0.21 at lag 20**. A Poisson process is flat at zero at
every lag. It reproduces the mean rate and nothing else: no slow phases, no fast
phases, no drift. Figure `L1_resting_rhythm.png` puts a real minute, a model
minute and a Poisson minute on top of each other; the difference is not subtle.

### Why the state relaxes in time, not in pulses

This matters *because* the device's intervals span an order of magnitude. After
one 3 s silence the fish has forgotten more than it has after three 100 ms
intervals, and a per-pulse AR(1) cannot express that. Refitting the same 99 010
intervals with a per-pulse AR(1) costs **366 nats** of likelihood at one
component and **699** at two (§2.2). It is not a stylistic choice.

---

## 2. Parameters

All values from `loc_model_params.json`.

### 2.1 The state

| component | time constant | share of log-interval variance | sd (score units) |
| --- | --- | --- | --- |
| white, per pulse | — | **0.379** | 0.616 |
| `fast` | **3.2 s** | **0.436** | 0.660 |
| `medium` | **95.8 s** | 0.110 | 0.331 |
| `slow` | 3 726 s (62 min) | 0.061 | 0.246 |
| `offset`, per deployment | fixed | 0.014 | 0.120 |

The variances sum to 1 by construction, so the score is standard normal and the
quantile table in §3 *is* the interval distribution.

**Dropping components.** `LocalizationModel(params, n_components=2)` folds
`slow` into the drawn offset — correct for any run short against an hour, and it
removes one state float. `n_components=1` folds `medium` in too and is audibly
worse: the minute-scale drift disappears.

**How `tau_fast` was chosen.** The Kalman MLE (§2.2) puts it at **3.88 s**, and
that is the number to quote for the *conditional* fit. The shipped value is
**3.2 s**, chosen by a four-point search that minimises the error on the
measured autocorrelation curve when the model is run end to end. The two
criteria need not agree, because the model is misspecified in two known ways —
it is sampled at its own pulse times, and its noise is not Gaussian — and under
misspecification a conditional likelihood and a marginal second moment pick
different optima. The autocorrelation is the statistic this model exists to
reproduce, so it wins. Both numbers are in the JSON
(`tau_fast_s`, `tau_fast_s_kalman_mle`).

### 2.2 Evidence for the shape

Exact Kalman likelihood over the rank-Gaussianised sequences, 99 010 intervals:

| model | negative log-likelihood | worse than best |
| --- | --- | --- |
| 1 component, AR per pulse | 115 703.6 | 996 |
| 1 component, OU in time | 115 337.3 | 630 |
| 2 components, AR per pulse | 115 406.4 | 699 |
| **2 components, OU in time** | **114 718.2** | **11** |
| 3 components, OU in time | 114 707.3 | 0 |

Read it as: time beats pulse count (366 nats at one component, 699 at two), a
second timescale beats one (619 nats), and a third buys 11 nats for two more
parameters — so two it is, plus the slow structure of §2.3 which a 60 s window
cannot see at all.

The profile likelihood on `tau_fast` is sharp — Δ nats 188 / 62 / 10 / 0.8 / 19
/ 100 at 2.5 / 3.0 / 3.5 / 4.0 / 4.5 / 5.5 s — so the gap to the ACF-matched
3.2 s is worth stating rather than papering over, even though it is small.

### 2.3 The slow structure, and where it stops being drift

A 60 s slice cannot resolve anything slower than itself, so the slow components
come from the covariance of *slice-mean* scores against their separation, within
one recording:

| separation | 60 s | 208 s | 526 s | 1 796 s | 7 195 s | 65 860 s |
| --- | --- | --- | --- | --- | --- | --- |
| covariance | 0.136 | 0.092 | 0.075 | 0.046 | 0.030 | 0.012 |

The rate is still drifting at every separation we can measure — three decades of
time and it has only fallen five-fold. Two OU components plus a floor fit it
(figure `L3_evidence.png`).

The floor is **0.012–0.014**, and it is a *deployment* offset rather than a
property of a place: two slices from **different sites** have covariance
**−0.002**, i.e. zero. So there is no site term to carry, and `offset` is
simply which fish, on which night, at what distance.

### 2.4 The switch out of the resting rhythm

A **fast run** is a maximal run of consecutive pulses with every interval at or
below the 25 ms antimode — from a bare doublet up to a full hunting volley.
5 469 of them in 1 928 episodes.

| | value |
| --- | --- |
| launch probability, **per resting pulse** | **0.0203** (1 in 49) |
| logit against the state score | `-4.872 - 0.914 * m` (as fitted: −4.112; see below) |
| episodes that are a single run | **57 %** |
| runs per episode, 25 / 50 / 75 / 95 % | 1 / 1 / 3 / 10 |
| pulses per run, 25 / **50** / 75 / 95 % | 2 / **2** / 6 / 37 |
| run duration, 25 / **50** / 75 % | 6.7 / **17** / 41 ms |
| gap to the next run in the episode | 55 / **162** / 473 ms |
| within-run interval | 3.0 / **5.0** / 9.9 ms |

**The hazard is per pulse, not per second**, and that is a measurement. Across
the state range the launch probability moves 5-fold per pulse (0.053 → 0.010)
but **58-fold per second** (0.357 → 0.006). A per-second hazard would be wrong
by an order of magnitude at one end or the other. The residual per-pulse
dependence is kept as the logistic slope; with it, the residual dispersion is
1.006, i.e. what is left looks like independent coin flips.

The intercept shipped (−4.872) is not the fitted one (−4.112). The logistic was
fitted against a Kalman *filter's* estimate of the state — the best a
noisy observer can do — while the model applies it to its own true state, whose
distribution is wider and pulse-weighted toward the fast side. The intercept is
re-solved by simulation so the launch rate per resting pulse still lands on
0.0203; the slope transfers unchanged.

**Doublets and volleys are one distribution.** Only 30 % of fast runs reach the
5 pulses that `volley_dynamics` calls a volley — but that subset has median 12
pulses over 84 ms, which is that analysis's `ordinary` volley (median 12 pulses,
103 ms) measured independently. So: hand runs of ≥ 5 pulses to the volley
sampler, render the doublets from the within-run interval table, and the two
models meet without a seam.

---

## 3. The interval table

41 knots, evenly spaced in the score from −3.5 to +3.5, holding `log(IPI)` in
seconds. Linear interpolation between knots; **clamp** outside.

| score | −3.5 | −2.8 | −2.1 | −1.4 | −0.7 | 0.0 | +0.7 | +1.4 | +2.1 | +2.8 | +3.5 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| interval (ms) | 30 | 43 | 64 | 116 | 282 | 523 | 1 321 | 3 410 | 9 147 | 21 333 | 33 395 |

The clamp is deliberate at both ends. The bottom of the table is the 25 ms
volley antimode, so the resting rhythm can never intrude into volley territory;
the top is the 99.98th percentile of 99 010 measured intervals, beyond which
there is nothing to extrapolate from.

**This table is calibrated, and the calibration is load-bearing.** Feeding the
raw measured quantiles in gives a device that runs **43 % fast** — a simulated
median interval of 181 ms against the measured 317 ms. The reason: the
model is observed at its *own* pulse times. A state running fast emits many
pulses before it has relaxed; a slow state emits few. So the distribution of
scores *as seen pulse by pulse* is shifted toward the fast side, and the table
has to undo that shift. It is solved by iterating simulate → read off the score
CDF → re-map, which is why the knot at score 0 is 491 ms and not the measured
median of 317 ms. The raw measured distribution is kept in the JSON under
`marginal.percentile_table` and `marginal.uncalibrated_ipi_ms_knots` — use those
for comparison, never as the table.

Measured resting intervals, for reference:

| percentile | 1 | 5 | 25 | **50** | 75 | 95 | 99 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| interval (ms) | 36 | 57 | 140 | **317** | 538 | 1 861 | 6 384 |

**Two rate numbers that must not be conflated.** The median resting interval is
317 ms — **3.15 Hz** — while a single eel emits about **1.1 pulses per second**
averaged over a slice. Both are right: the distribution is heavy-tailed, and one
30 s silence contributes one interval to the median while eating half the
average. Quote 3.15 Hz for "how fast does it tick" and 1.1 Hz for "how many
pulses will I hear".

---

## 4. The noise is peaked, not Gaussian

Real one-step innovations have **excess kurtosis +1.5**: the fish holds its rate
almost exactly and then jumps. Gaussian noise of the same variance gets the
interval distribution and the autocorrelation right and still sounds wrong,
because a pure shape statistic gives it away — **median CV2 0.60 against a
measured 0.38**, where `CV2 = 2|I2-I1|/(I2+I1)` and 1.0 is Poisson.

So all noise in the model is drawn from a unit-variance two-scale Gaussian
mixture: with probability **0.7** use sd **0.343**, else sd **1.749**
(excess kurtosis 5.45 in the elementary draw; the innovation, being a sum, comes
out near the measured 1.5). On a microcontroller that is one extra uniform and a
branch. The mixture width was fitted to the median CV2 and hits it: 0.383
against 0.382.

---

## 5. The two RC knobs

The remote has a **rate** knob and a **randomness** knob. Both map onto this
model cleanly, and they are **orthogonal by construction**: every pulse-indexed
statistic — regularity, autocorrelation, bursts per pulse — depends on
`randomness` alone, and `rate` is a pure time dilation on top of whatever
`randomness` set.

```python
model.rate = 1.0          # 1.0 = the measured eel, 3.1 Hz tick
model.randomness = 1.0    # 0 = metronome, 1 = the measured eel
```

### 5.1 Rate is a time dilation — of the clock, not just the intervals

Dividing the intervals is only half of it. The knob must **stretch the time
constants with them**, and that is a measurement, not a convenience. Refitting
the memory as `tau * (own tempo)^gamma`, where "own tempo" is each slice's
median interval relative to the population's, peaks sharply at **gamma = 1**:

| `gamma` | 0 (fixed in seconds) | 0.25 | 0.5 | 0.75 | **1 (fixed in pulses)** | 1.25 |
| --- | --- | --- | --- | --- | --- | --- |
| nats worse than best | 413 | 240 | 111 | 29 | **0** | 30 |

So a slower fish is a *time-dilated* fish: it takes the same number of pulses to
forget, not the same number of seconds. Note this is not in tension with §1 —
*within* one fish the state still decays in real seconds (that is the 366-nat
result), it is the fish's characteristic timescale that follows its own tempo.

The practical consequence is large. Halve the rate with `tau` fixed and the
state relaxes between pulses, so the wander washes out and the device drifts
back toward Poisson exactly at the slow settings. Stretch `tau` with the
intervals and the texture is identical at every setting — measured across an
8-fold rate range, CV2 and the lag-1 autocorrelation do not move at all
(figure `L7_knobs.png`, right panel).

### 5.2 Randomness scales the state score

```
z = randomness * (offset + x_fast + x_medium + x_slow + sd_white * e)
```

At 0 that is a metronome at exactly the nominal rate — what the old device did
with its randomness knob at zero. At 1 it is the measured eel.

It is worth being clear about what this knob is *not*. It does not blend toward
a Poisson process: the lag-1 autocorrelation stays at ~0.52 across the whole
range (it is scale-invariant, and only the curvature of the interval table
moves it at all). The knob dials **how much** the rate varies while leaving
**how it varies over time** alone — an eel that is more or less variable, not
an eel that is more or less random. That is the right shape for the knob, and
it means every setting still sounds like a fish.

Measured through the device's own loop, at `rate = 1`:

| randomness | tick (Hz) | pulses/s | CV2 | bursts/min |
| --- | --- | --- | --- | --- |
| 0.0 | 2.93 | 3.88 | 0.000 | 7.9 |
| 0.5 | 3.04 | 3.41 | 0.235 | 7.4 |
| **1.0** | **3.11** | **2.11** | **0.451** | **4.7** |
| 1.5 | 3.73 | 1.07 | 0.539 | 2.2 |

**Useful range 0 to ~1.5.** Above that CV2 saturates at ~0.56, because the
score starts running into the ends of the interval table. Real single-eel
slices span CV2 0.25-0.61 at the quartiles (0.06-0.87 at 5-95 %), so the knob
covers from the most metronomic real fish to roughly the 70th percentile of
irregularity. It also *fixes* the residual noted in §6: one fitted model has one
regularity, but real eels vary, and this knob is where that variation lives.

### 5.3 What the rate knob holds fixed — a real choice

A heavy-tailed interval distribution cannot hold its median and its mean at
once, so the knob has to pick one, and the two differ by a factor of ~2:

* `rate_anchor = "median"` (**the default**) holds the **tick tempo**, 1/median.
  This is the number quoted as an eel's discharge rate — 3.15 Hz — and it is
  the only one of the two that is stable enough to control: over a 60 s window
  the realised average pulse rate varies 2.5-fold on its own, in real fish and
  in the model alike, so anchoring the mean buys no short-run control anyway.
* `rate_anchor = "mean"` holds **average pulses per second**, which is what the
  Poisson device's rate knob meant.

The cost of the default is visible in the table above: sweeping randomness at a
fixed tempo moves the dose from 3.9 to 1.1 pulses/s. If an experiment needs
randomness varied at matched dose, either flip the anchor or compensate with
`knobs.gain_mean_anchor` in the parameter file — both tables are shipped.

### 5.4 What is held regardless

The **burst rate stays at 1 launch per 49 resting pulses** at every setting. It
would otherwise fall threefold as the state stopped wandering, because the
hazard reads the state; `knobs.hazard_logit_intercept` re-solves the intercept
per randomness setting to prevent that. Bursts per *minute* still scale with the
rate knob, as they should — the hazard is per pulse.

Two small tables make all of the above true, both in `knobs`:
`gain_*_anchor` (a tempo factor per randomness setting — at randomness 0 the
calibrated table would otherwise emit its own score-zero knot, 523 ms, instead
of the fish's median) and `hazard_logit_intercept`. Both are indexed by
`knobs.randomness` and read with linear interpolation; the reference
implementation does it in the `randomness` setter.

Where the knobs sit for a real fish: `rate` 1.0 by definition, with real slices
spanning 0.65-1.3 at the quartiles (0.4-3.1 at 5-95 %), and `randomness` ~1.0,
spanning roughly 0.55-1.5.

One thing the knobs cannot remove: the ~1 h component and the per-deployment
offset have not mixed inside any single run, so one power-on sits at its own
tempo — roughly +-25 % around the knob setting, and it stays there. That is the
model reproducing individual variation, not the knob being imprecise. Pass
`new_state(rng, offset=0.0)` and use `n_components=2` if a run has to land on
the nominal tempo exactly.

---

## 6. Validation

`loc_validate.py` simulates the same experiment that was measured — independent
60 s slices, one fish each — runs the identical segmentation and estimators, and
compares, with a Poisson process of the same mean rate carried through every
panel (figure `L5_model_validation.png`).

| | real | model | Poisson |
| --- | --- | --- | --- |
| resting interval, 25/50/75 % (ms) | 140 / 317 / 538 | 133 / 304 / 519 | 194 / 431 / 839 |
| pulses per second present | 1.11 / 1.69 / 2.79 | 1.15 / 1.82 / 2.92 | 1.60 / 1.72 / 1.84 |
| CV2 per slice (median) | 0.424 | 0.450 | 0.925 |
| fast runs per resting pulse | 0.0552 | 0.0529 | 0.0455 |
| pulses per fast run, 25/50/75 % | 2 / 2 / 6 | 2 / 2 / 6 | 2 / 2 / 2 |
| **log-interval ACF, lag 1** | **0.549** | **0.568** | 0.001 |
| lag 2 | 0.482 | 0.519 | 0.000 |
| lag 5 | 0.355 | 0.418 | −0.001 |
| lag 10 | 0.270 | 0.311 | 0.002 |
| lag 20 | 0.213 | 0.207 | 0.000 |

The drift panels are the ones nothing was fitted to term-by-term: over 1–30 s
windows and over 60 s–10⁵ s separations the model tracks the measurement, while
the Poisson null falls away as pure counting noise.

**Known residuals**, documented rather than chased:

- **The spread of CV2 across fish is too narrow** (model 0.39–0.53 against a
  real 0.25–0.61). Real eels differ in how regular they are; the model has one
  noise shape for all of them. Drawing the mixture width per deployment would
  fix it.
- **Rate drift over 8–30 s windows is ~25 % low.** The model matches to about
  5 s and then under-drifts.
- **The autocorrelation runs ~0.05 high in the middle lags**, and everything is
  ~5 % fast on the interval quantiles.

None of these are worth another parameter (the CV2 spread is what the
randomness knob of §5 is for). A real eel's discharge is far more
flexible than five floats — it responds to prey, neighbours, light and
temperature — and the goal here is a device that produces *reasonably realistic*
sequences in real time, not a fish simulator.

---

## 7. On a microcontroller

Five floats of state, four `expf` and four noise draws per pulse.

```c
typedef struct { float x[3], offset, pending_dt, m; } loc_state_t;

static const float TAU[3] = { 3.200f, 95.754f, 3726.07f };
static const float SD [3] = { 0.6604f, 0.3314f, 0.2463f };   /* sqrt(var) */
static const float SD_WHITE = 0.6158f, SD_OFFSET = 0.1195f;
static const float MIX_P = 0.7f, MIX_LO = 0.3426f, MIX_HI = 1.7491f;
static const float HAZ_B0 = -4.872f, HAZ_B1 = -0.914f;
static const float LOG_IPI[41] = { /* marginal.log_ipi_knots, score -3.5..+3.5 */ };

static float noise(void) {                      /* unit variance, peaked */
    return (randf() < MIX_P ? MIX_LO : MIX_HI) * gauss();
}

/* Set by the knobs: tempo = rate / gain(randomness), and s = randomness.
   gain() and haz_b0() are 21-entry lookups over knobs.randomness. */
static float g_tempo = 1.0f, g_s = 1.0f;

float loc_next_interval(loc_state_t *st) {
    float m = 0.0f;
    for (int i = 0; i < 3; i++) {
        float a = expf(-(st->pending_dt * g_tempo) / TAU[i]);   /* time dilation */
        st->x[i] = a * st->x[i] + SD[i] * sqrtf(1.0f - a * a) * noise();
        m += st->x[i];
    }
    m = g_s * (st->offset + m);
    st->m = m;
    float z = m + g_s * SD_WHITE * noise();
    float u = (z + 3.5f) * (40.0f / 7.0f);           /* index into the table */
    if (u < 0.0f) u = 0.0f;
    if (u > 39.999f) u = 39.999f;
    int   i = (int)u;
    float f = u - (float)i;
    float ipi = expf(LOG_IPI[i] + f * (LOG_IPI[i + 1] - LOG_IPI[i])) / g_tempo;
    st->pending_dt = ipi;
    return ipi;
}

void loc_set_knobs(float rate, float randomness) {   /* from the RC channels */
    g_s     = randomness;
    g_tempo = rate / gain(randomness);
    HAZ_B0  = haz_b0(randomness);
}

void  loc_advance(loc_state_t *st, float dt) { st->pending_dt += dt; }
int   loc_interrupt(const loc_state_t *st) {
    return randf() < 1.0f / (1.0f + expf(-(HAZ_B0 + HAZ_B1 * st->m)));
}
```

Initialise with `x[i] = SD[i] * noise()`, `offset = SD_OFFSET * noise()`,
`pending_dt = 0`, **then discard 40 intervals**. The components' stationary
distribution is defined in continuous time while the model is observed at its
own pulse times, so a cold state starts noticeably slow and takes tens of pulses
to settle; the reference implementation burns those in inside `new_state`.

If `expf` is too expensive: with a typical interval of 0.3–0.5 s, `a_medium =
1 - dt/95.8` is accurate to better than 0.01 %, `a_slow` is 1.0 for any run
under ten minutes (fold `slow` into the offset instead), and only the fast
component needs a real exponential. The alternative is to age the state on a
fixed timer tick with precomputed constants and no `expf` at all.

---

## 8. Caveats

**Long silences are real, and the model will produce them.** 1.5 % of resting
intervals exceed 5 s and 0.6 % exceed 10 s (the longest measured is 54 s), so
the device will occasionally go quiet for that long. That is measured behaviour, not a bug. The evidence it is
not a recording artifact: the amplitude of the pulses *bracketing* an interval
is 1.00 × the slice median in every interval band from 25 ms to 60 s (figure
`L6_caveats.png`, n = 874 in the 10–60 s band). A fish drifting out of range
would fade first; these do not. The eel stops.

**The tail beyond ~10 s is a lower bound**, though. Slices are 60 s, so a longer
interval cannot be observed at all, and a slice dominated by silence fails the
≥ 30-pulse mining gate. If anything, real eels are quieter than this model.

**The diel cycle is not in the model.** Median resting interval by hour: 258 ms
at 21–24 h against 414 ms at 12–15 h — the fish ticks **1.6 × faster in the
early night**. The model draws one offset and holds it. If that matters, shift
the offset by hand; the measured curve is in the JSON under
`evidence.diel_median_resting_ipi`.

**No individual identity.** The per-deployment offset carries only 1.4 % of the
variance, and its sd is 0.12 in score units, ~13 % on the median interval. Two
model fish are more alike than two real ones. This is partly real — the
different-site covariance really is zero — and partly the mining gate, which
selects fish that are close, isolated and steadily tracked.

**The model does not know about other fish.** Every slice was chosen precisely
because exactly one eel was present. Whatever an eel does in company —
and it does do something — is outside this.

**Site coverage is uneven**: 1 761 slices over 15 sites, with `line_site_2_3`
(263) and `site_4` (210) supplying a quarter of them. `line_site_9` is excluded
throughout (drifting channels, phantom detections).

**Intervals below 2 ms never enter anything.** They are detector artifacts, not
discharges — see the volley-dynamics README. The model's floor is 25 ms anyway.

---

## 9. Regenerating

From the eeltracker repo, against the live dataset:

```sh
python scripts/loc_extract.py     # ~10 s, 43 recordings -> 1761 slices
python scripts/loc_fit.py         # ~100 s, writes model/loc_model_params.json
python scripts/loc_plot.py        # L1-L4, L6
python scripts/loc_validate.py    # L5, and the regression test
```

`loc_fit.py --quick` skips the model-comparison and profile-likelihood passes
(the evidence in §2.2) and takes ~40 s. `loc_validate.py` is the regression
test for any change to the model — run it after touching anything.

Background on the mining recipe, the two gates, and what the slices are:
`README.md` in this directory.
