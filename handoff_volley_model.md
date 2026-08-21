# Handoff prompt for the fakefish repo

Paste the block below into a fresh agent session rooted in
`~/wrk/analyses/fakefish`.

---

I need synthetic *Electrophorus* hunting volleys for the playback device, fitted
to real field recordings rather than guessed. The model is already built and
validated — your job is to wire it in, not to re-derive it.

Everything is in the sibling eeltracker repo, at
`~/wrk/analyses/eeltracker/analyses/volley_dynamics/`:

- **`VOLLEY_GENERATIVE_SPEC.md`** — read this first. The model, every fitted
  number, what was validated, and the caveats.
- **`scripts/volley_model.py`** — the reference sampler. numpy + stdlib only, no
  eeltracker imports. Copy it in as-is.
- **`cache/volley_model_params.json`** — the fitted parameters. Copy it in as-is.

Usage:

```python
from volley_model import VolleyModel
import numpy as np

model = VolleyModel.from_json("volley_model_params.json")
rng = np.random.default_rng()

t, amp = model.sample_volley(rng)   # ONE volley
```

**One call = one volley = one button press.** The output is an event series and
nothing more: `t` is pulse times in seconds from onset, `amp` is per-pulse
relative amplitude (1.0 = that volley's median pulse). Our synthesiser owns the
pulse waveform and keeps its own localization rhythm running, so it needs
neither from the model — just scale `amp` by the amplitude knob and fire the
events. (`model.render()` exists only to draw figures; ignore it.)

Three things in there are measurements, not modelling choices. Don't simplify
them away:

1. **Pulse times come from integrating the rate curve** with a small
   multiplicative jitter — *not* from a Poisson process. Real volleys are nearly
   clockwork (CV2 ≈ 0.12, where Poisson is 1.0). A Poisson train has the right
   mean rate and completely the wrong texture.
2. **No ramp-up.** A volley starts at its peak rate and decays from there.
3. **Amplitude decays smoothly**, ~20 % across the volley, with only ~1.4 %
   pulse-to-pulse jitter.

`kind="strong"` (the default: ~393 Hz start, ~0.47 s, ~88 pulses) is what a
hunting-volley button should fire. `kind="ordinary"` is the shorter everyday
volley and is a looser fit — see §4 of the spec before relying on it.

Two limits to respect: don't request intervals below 2 ms (the source detector
cannot resolve them), and treat the absolute volley-to-resting amplitude *step*
as your knob, not as a measurement — §5 explains why that one number is not
trustworthy while the within-volley envelope and jitter are.

If you change anything about the model, `scripts/volley_validate.py` in the
eeltracker repo is the regression test: it re-fits synthetic volleys with the
same estimator used on the real ones and compares the distributions.
