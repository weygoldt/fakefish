# Generative spec for synthetic *Electrophorus* hunting volleys

**Purpose.** Everything needed to synthesise one realistic volley from a single
extracted pulse waveform: what rate to fire at, how that rate evolves, how long
the volley lasts, how the pulses are spaced, and how amplitude behaves. Fitted
to the 200 strongest hunting volleys in the FLONA 2025 field dataset
(43 recordings, 16 sites), not guessed.

**The unit is one volley** — a single continuous burst, the thing one button
press should produce. One call to the sampler gives one volley and nothing else.
The device's own localization rhythm is outside this model: it keeps ticking,
and the button interrupts it with a volley.

**Scope.** The output is an **event time series**: pulse times plus a per-pulse
relative amplitude. Nothing else — the pulse waveform belongs to the
synthesiser, and absolute amplitude is a free knob (the model's amplitude is
relative to the volley's own median pulse, so multiply by whatever the device
uses). `render()` in the reference implementation convolves the events with a
template purely so the figures can show a waveform; it is not part of the model.

**Three files to copy** (nothing else is needed — `volley_model.py` imports only
numpy and the standard library):

| file | what it is |
| --- | --- |
| `scripts/volley_model.py` | reference sampler, runnable as-is |
| `model/volley_model_params.json` | every fitted number, with quantiles and ECDF tables |
| this file | the model, the provenance, and the caveats |

Illustrations: `figures/V7_synthetic_showcase.png` (what the output looks like),
`figures/V8_parameter_comparison.png` and `figures/V6_model_validation.png`
(synthetic against real), `figures/V9_amplitude_check.png` (the amplitude
correction).

```python
from volley_model import VolleyModel
import numpy as np

m = VolleyModel.from_json("volley_model_params.json")
rng = np.random.default_rng(0)

t, amp = m.sample_volley(rng)   # ONE volley: pulse times [s], relative amplitude
                                # -> hand straight to the pulse synthesiser

# to inspect or reuse the parameters of a particular volley:
p = m.draw_parameters(rng, "strong")             # r_start_hz, duration_s, lam, cv2, ...
t, amp = m.sample_volley(rng, params=p)
```

Two volley strengths are fitted, selectable with `kind`:

| `kind` | start rate | duration | pulses | what it is |
| --- | --- | --- | --- | --- |
| `"strong"` (default) | ~393 Hz | ~0.47 s | ~88 | the extreme volleys this analysis selected for — what a "fire a hunting volley" button should emit |
| `"ordinary"` | ~134 Hz | ~0.10 s | ~12 | the everyday volleys occurring alongside them |

---

## 1. The model

### 1.1 Rate

Three numbers define a volley: start rate `r_start` [Hz], decay `λ` [-],
duration `D` [s]. The instantaneous rate is

```
r(f) = r_start · exp(-λ · f)        f = (t - t_start) / D  ∈ [0, 1]
```

Two things about this are measurements, not modelling choices:

- **There is no rise.** The volley starts at its peak rate. Median time from
  onset to peak rate is **18 ms**, and the lower quartile is **0 ms**; as a
  fraction of the volley, the median is 0.04. A ramp-up envelope would be wrong.
  (The apparent ~0.1 s rise in a peak-aligned population average is an artifact
  of averaging events whose peaks sit at different points.)
- **The decay is log-linear in the volley's own time.** Median rate falls
  359 → 70 Hz across the volley with a near-constant slope in log-rate, i.e. a
  self-similar profile: long volleys decay slowly, short ones fast.

`λ` is fitted by **moment matching**, not least squares: λ is chosen so that
∫r dt equals the observed pulse count. A least-squares fit of log-rate matches
the shape but integrates to the wrong number of pulses, and a stimulator emits
pulses, not shapes. (Using the least-squares λ gave synthetic volleys 15 % too
many pulses; moment matching removed the bias exactly.)

### 1.2 Pulse times

Pulse times come from **integrating `r(t)`** — take the current instantaneous
interval, jitter it multiplicatively, step forward — **not** from a Poisson
process. This is the single most important realism point in the whole spec:

> Measured local irregularity is **CV2 ≈ 0.12** for strong volleys, where a Poisson
> process gives 1.0. A real volley is nearly clockwork. Drawing exponential
> intervals would give the right mean rate and completely the wrong texture.

`CV2 = 2|I(i+1) − I(i)| / (I(i+1) + I(i))`, median over the volley.

On top of the smooth decay the real rate **wanders**: the residual of log-rate
around the fitted decay has sd ≈ 0.26 with a correlation time ≈ 6 % of the volley
(~29 ms at a median-length one). The sampler adds this as a multiplicative
Ornstein–Uhlenbeck factor. Its variance is **budgeted against** the per-pulse
jitter, not added on top — the measured CV2 already contains the wander, so
injecting both at full strength makes every synthetic volley raggeder than any
real one.

### 1.3 What is deliberately not modelled

In nature a volley usually does not occur alone: over a 25 s window around a
strong one there is a median of 1 more volley (IQR 0–4), typically a few seconds
later, with the fish's slow localization discharge (~3 Hz) filling the gaps.
None of that is in the sampler — one call is one volley — because the device
provides the localization rhythm and the operator provides the timing. The
measured clustering statistics are kept in the parameter file under
`episode_context_NOT_USED_BY_SAMPLER` in case they are ever wanted.

### 1.4 Amplitude

Amplitude within a volley is **smooth, and decays gently**:

```
A(f) = A_volley · 10^(trend · (f − 0.5)) · 10^N(0, 0.0062)
```

| fraction of volley | 0.0 | 0.1 | 0.2 | 0.3 | 0.4 | 0.5 | 0.6 | 0.7 | 0.8 | 0.9 | 1.0 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ×volley median | 1.13 | 1.08 | 1.01 | 1.00 | 0.98 | 0.98 | 0.96 | 0.95 | 0.95 | 0.95 | 0.94 |

Per volley, the log₁₀ amplitude change from start to end (`trend`) has median
**−0.106** (**−22 %**), IQR −0.19 to −0.01, drawn per volley from its ECDF.

Pulse-to-pulse jitter around that envelope is **0.0062 in log₁₀ ≈ ±1.4 %**, held
**fixed** rather than drawn: the spread across real volleys is dominated by
measurement noise, since jitter falls with loudness down to ~0.3 % for the
loudest. Even 1.4 % is an upper bound — set
`model.amp_log10_jitter = 0.003` for a close, loud fish.

> **This corrects the first pass of this spec**, which reported a flat envelope
> and ~23 % jitter. Both were measurement artifacts. Amplitude was being read
> from `PositionFeatures.AMPLITUDE`, whose estimator fails catastrophically on
> the occasional single pulse; a plain standard deviation is dominated by those
> outliers. The same estimator measured with a **robust** statistic gives 2.0 %,
> and raw cutouts (peak-to-peak on one fixed channel, bypassing the estimator
> entirely) give **1.4 %**. With the outlier noise removed, the ~20 % decay that
> had been buried under it is visible. Evidence: `figures/V9_amplitude_check.png`,
> `scripts/volley_amplitude.py`, n = 178 strong volleys across 15 sites
> (`line_site_9` excluded — its channels drift and saturate).

Caveat on interpretation: recorded amplitude is source amplitude × distance
attenuation, and a striking fish moves. Some of the −22 % is the fish receding
from the electrode rather than its output falling. Individual volleys vary — a
few rise — so `trend` is drawn per volley from its ECDF, not fixed.

---

## 2. Parameters

All values from `volley_model_params.json`. Quantiles are 25 / **50** / 75
unless stated.

### 2.1 Volley parameters

Sample `(r_start, log D, λ)` jointly from a multivariate normal, clipped to the
1st–99th percentile box. Sampling the three marginals independently would break
their correlations — a volley that is both fast and long is not the same object
as either alone.

**Strong volley** (`strong_volley`, n = 198), mean `[385.96, −0.7411, 1.5792]`:

```
cov =  [[ 7974.8142,  -12.0593,   24.5047],
        [  -12.0593,    0.4425,    0.0789],
        [   24.5047,    0.0789,    0.4084]]
clip_lo = [149.418, -1.867,  0.054]
clip_hi = [573.723,  0.817,  2.706]
```

**Ordinary volley** (`ordinary_volley`, n = 565), mean `[169.30, −2.0965, 0.5020]`:

```
cov =  [[13443.931,   -25.8775,   45.9248],
        [  -25.8775,    1.0245,    0.1333],
        [   45.9248,    0.1333,    0.6175]]
clip_lo = [ 32.088, -3.987, -1.549]
clip_hi = [506.776,  0.259,  2.595]
```

Resulting marginals:

| | strong 25 / **50** / 75 | strong 5–95 % | ordinary 25 / **50** / 75 |
| --- | --- | --- | --- |
| `r_start` (Hz) | 335 / **393** / 454 | 233–512 | 84 / **134** / 222 |
| `D` (s) | 0.279 / **0.466** / 0.751 | 0.189–1.421 | 0.061 / **0.103** / 0.239 |
| `λ` | 1.17 / **1.65** / 2.10 | 0.44–2.49 | 0.06 / **0.42** / 0.97 |
| pulses per volley | 58 / **88** / 142 | 39–264 | 7 / **12** / 26 |
| CV2 | 0.070 / **0.121** / 0.177 | 0.030–0.340 | 0.126 / **0.226** / 0.395 |
| profile fit R² | 0.44 / **0.65** / 0.79 | | 0.10 / **0.31** / 0.59 |

Draw **CV2 per volley** from its ECDF, not as a fixed constant — real volleys
run from near-clockwork (0.03) to quite ragged (0.34), and one value makes every
synthetic volley identically metronomic.

The `ordinary` class is the weaker fit of the two: its volleys are ~12 pulses
long, where a two-parameter decay is barely identifiable (profile fit R² 0.31 vs
0.65 for `strong`), and re-fitted synthetic ordinary volleys come out ~30 % long.
Use it for flavour, not for precision.

### 2.2 Rate sub-structure

| | 25 / **50** / 75 |
| --- | --- |
| log-rate residual sd | 0.206 / **0.258** / 0.307 |
| correlation time (fraction of volley) | 0.043 / **0.061** / 0.088 |

### 2.3 Clustering context (not used by the sampler)

| | 25 / **50** / 75 | 5–95 % |
| --- | --- | --- |
| further volleys within 25 s | 0 / **1** / 4 | 0–13 |
| next volley's onset − this one's (s) | 1.09 / **4.06** / 9.07 | −0.63 to 17.4 |
| localization rate between volleys (Hz) | 1.57 / **3.22** / 6.46 | 0.42–12.1 |
| its interval (s) | 0.066 / **0.115** / 0.241 | 0.032–0.919 |
| its CV2 | **0.53** | |

### 2.4 Amplitude parameters

From `amplitude_raw` in the JSON (raw cutouts, n = 178 strong volleys):

| | 5 % | 25 / **50** / 75 | 95 % |
| --- | --- | --- | --- |
| pulse-to-pulse log₁₀ jitter (robust sd) | 0.0015 | 0.0029 / **0.0062** / 0.0123 | 0.0302 |
| within-volley log₁₀ trend (start → end) | −0.387 | −0.192 / **−0.106** / −0.012 | +0.114 |

For reference, the superseded estimator-based numbers on the same volleys:
robust 0.0087, plain sd 0.0890. Draw `trend` per volley from
`within_burst_log10_trend_ecdf`.

The **step** between a volley pulse and the fish's resting pulse
(median ×1.4, IQR 0.70–13.7, n = 54) stays in the JSON under `amplitude` but is
**not a usable measurement** — see §5. Treat it as part of the amplitude knob.

---

## 3. How long is a volley? (the truncation question)

Three separate effects, in increasing order of importance.

**1. Window truncation — handled.** Bursts touching the edge of the 25 s
analysis window are right-censored and are dropped, not fitted: 68 of 934.

**2. The volley-splitting threshold — small.** A volley ends where the IPI
crosses 25 ms, so duration is partly a definition. Median strong-volley duration
under other thresholds:

| split threshold | 15 ms | **25 ms** | 40 ms | 60 ms |
| --- | --- | --- | --- | --- |
| median duration (s) | 0.329 | **0.464** | 0.504 | 0.561 |

Going from 25 ms to 60 ms lengthens the median by 21 %. The definition matters,
but it is not a factor-level effect.

**3. Selection — this is the big one.** The 200 windows were ranked by peak rate
**×** duration, so the strong volley was chosen partly *for being long*. A
duration-unbiased comparison is an ordinary volley matched on start rate
(`r_start ≥ 335 Hz`, the 5th percentile of strong ones):

| | 25 / **50** / 75 (s) |
| --- | --- |
| strong volley | 0.279 / **0.466** / 0.751 |
| rate-matched ordinary volley | 0.041 / **0.080** / 0.152 |

**A fast volley is ~6× shorter than the selected strong ones.** So "how long is a
strong volley" has two correct answers, and which one to generate depends on the
question:

- *"the strongest volleys that occur in the wild"* → the **main** distribution
  (median 0.47 s). Use this for a stimulus meant to represent an extreme
  predatory volley.
- *"a fast volley drawn at random"* → the **`ordinary`** distribution
  (median 0.10 s). Use this for background realism.

If you ever chain volleys, do not give them all `strong` durations — that would
make a sequence ~5× more intense than any real fish produces.

---

## 4. Validation

`volley_validate.py` draws 5× as many synthetic volleys as there are real ones,
re-fits them with the same estimator, and compares (figure
`figures/V6_model_validation.png`; see also `V8_parameter_comparison.png`).
Strong volleys, quartiles:

| | real | synthetic |
| --- | --- | --- |
| duration (s) | 0.279 / 0.466 / 0.751 | 0.300 / 0.477 / 0.735 |
| `r_start` (Hz) | 335 / 393 / 454 | 318 / 385 / 462 |
| CV2 | 0.070 / 0.121 / 0.177 | 0.098 / 0.131 / 0.182 |
| pulses per volley | 58 / 88 / 142 | 58 / 90 / 141 |

The median rate-versus-time-since-onset profile tracks the real one across the
whole volley, inside the inter-quartile band.

**Known residuals** (documented, not hidden):

- **Pooled within-volley IPI is ~15 % slow.** Real 2.81 / **3.69** / 6.67 ms
  (5–95 %: 2.10–14.4) against synthetic 2.97 / **4.26** / 6.49 ms (1.94–12.7).
  Real volleys have more very-fast *and* more very-slow intervals than a smooth
  decay plus OU wander produces. Closing this needs heavier-tailed within-volley
  rate structure; the OU sd cannot simply be raised because that breaks CV2.
- **CV2's lower tail is too high** (synthetic 0.098 vs real 0.070 at Q1): with
  the OU always on at full strength the sampler cannot make a volley as
  clockwork as the most regular real ones. Drawing the OU sd per volley,
  correlated with CV2, would fix it.
- **`ordinary` volleys come out ~30 % long** (0.143 s vs 0.103 s median) and
  ~25 % fast. At ~12 pulses the three-parameter fit is barely identifiable. The
  `strong` class, which is what a button should fire, matches tightly.

---

## 5. Caveats

**The amplitude step is weakly constrained — treat it as a knob.** (The
within-volley envelope and jitter in §2.4 are solid; only the step is not.)
Volley
amplitude ÷ pre-volley baseline has median ×1.43 but an IQR of 0.70–13.7 on
n = 54, and a quarter of cases are *below* 1. Biologically the volley is the
strong (main) organ and the baseline is the weak (Sachs) organ, so a large step
is expected; the measurement does not resolve it, because (a) recorded amplitude
confounds output with distance and orientation, (b) point-blank high-voltage
pulses clip and are dropped at detection, and (c) amplitudes over the recorder's
±1200 mV ceiling are reconstructed from a template fit and get noisier the more
channels clip. **Do not read ×1.4 as the physiological ratio.** The
within-volley envelope and jitter are solid; the step is not.

**Do not ask for intervals below 2 ms.** The production detector runs
`find_peaks(distance_ms=1.5)`, so nothing faster is resolvable, and raw-trace
reconstruction showed the sub-2 ms intervals in the data are spurious extra
detections, not extra EODs (`figures/V5_detector_check.png`). Peak rates above
~500 Hz in the source data are a measurement limit, not biology. Consider
clamping the stimulator's IPI at ≥ 2 ms; the sampler's own 5th-percentile
within-volley interval is 1.94 ms.

**The `strong` population is the extreme tail by construction** — the top 1.4 %
of volley runs in the dataset. That is the point for a hunting-volley button,
but do not read these numbers as a typical discharge.

**One fish, imperfectly isolated.** A 25 s window around a volley contains the
whole scene; pulses were attributed to the volleying fish by amplitude
fingerprint (96 % pure inside the volley, ~50 % of raw pulses removed at
baseline). Residual contamination makes the localization rate in §2.3 an **upper
bound** on one fish's resting discharge.

**Site coverage is uneven.** 16 sites and 35 recordings contribute, but two
sites supply 40 % of the events.

---

## 6. Regenerating

From the eeltracker repo, against the live dataset:

```sh
python scripts/volley_extract.py --stage scan     # ~1 min, 43 recordings
python scripts/volley_extract.py --stage window   # ~2 min
python scripts/volley_fit.py                      # writes volley_model_params.json
python scripts/volley_amplitude.py                # MUST run after fit — adds amplitude_raw
python scripts/volley_validate.py                 # writes V6_model_validation.png
python scripts/volley_showcase.py                 # writes V7 and V8
```

`volley_fit.py` rewrites the whole parameter file, so `volley_amplitude.py` has
to run after it or the corrected amplitude block is lost.

`volley_validate.py` is the regression test for any change to the model: it
compares synthetic against real on duration, start rate, CV2, volley size, the
rate profile, and the IPI distribution. Run it after touching anything.

Background on how the 200 volleys were selected, the focal-fish attribution, and
the detector artifact: `README.md` in this directory.
