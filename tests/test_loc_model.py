"""Guard the vendored resting-rhythm model, and the C port that has to agree with it.

The model is **not fitted in this repo**. ``src/fakefish/loc_model.py`` and
``data/loc_model_params.json`` are byte-identical copies of the reference sampler and
fitted parameters from the eeltracker ``localization_rhythm`` analysis, whose own
``loc_validate.py`` is the model's regression test. What can go wrong *here* is not "is
the model right" but:

* someone edits a vendored file in place instead of re-dropping it from upstream, so a
  later re-drop silently reverts the fix (the failure mode invariant 1 guards for the
  firmware core, invariant 2 for the generated constants, and ``test_volley_model`` for
  the companion volley model);
* the vendored module grows a dependency on fakefish and stops being droppable;
* **the C port drifts from the Python it was derived from.** This is the real hazard, and
  the one the spec itself walked into: its §7 listing ships hand-written C whose ``TAU[0]``
  (3.200), ``HAZ_B0`` (−4.872) and both mixture widths disagree with the fitted parameters
  in the JSON beside it. Anyone pasting that listing would get a device that is subtly not
  the model, with nothing to catch it. So the tables are GENERATED
  (``gen_constants.render_loc_header``) and the recurrence is checked against a golden
  produced by the reference implementation itself.

``tests/data/loc_rhythm_golden.csv`` is that golden. It is **generated, not authored**:
this module renders it from ``fakefish.loc_model`` and fails if the committed copy differs,
and ``firmware/eel_core/host_test/loc_rhythm_selftest.cpp`` replays the same injected noise
through the C and fails if the state or the interval diverges. Both directions are covered,
so neither side can move alone. Regenerate with::

    uv run python tests/test_loc_model.py --emit
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from fakefish import _resources as _res
from fakefish import gen_constants as gc
from fakefish.loc_model import LocalizationModel, RestingState

#: sha256 of the vendored files as dropped from
#: ``eeltracker/analyses/localization_rhythm/{scripts/loc_model.py,model/loc_model_params.json}``.
VENDORED_SHA256 = {
    "src/fakefish/loc_model.py":
        "3bc5a038c263b34fd5c6ac9a5a692e95e5571b8f3d0eeba4c7cdc12fbe76b793",
    "data/loc_model_params.json":
        "f9adc5bad4a6a569804523d833af280e94e1cf18ee582b60b6acdc2bd99b07b4",
}

GOLDEN = _res.ROOT / "tests" / "data" / "loc_rhythm_golden.csv"


@pytest.fixture(scope="module")
def params() -> dict:
    return gc.load_loc_model()


@pytest.fixture(scope="module")
def model(params: dict) -> LocalizationModel:
    return LocalizationModel(params)


# ===== the vendored copies stay vendored ==============================================


@pytest.mark.parametrize("rel", sorted(VENDORED_SHA256))
def test_vendored_file_is_unmodified(rel: str) -> None:
    """A vendored file must be byte-identical to what was dropped in.

    Editing one in place is the failure this catches: the next drop from upstream reverts
    the edit without a conflict, because a copy has no merge base.
    """
    got = hashlib.sha256((_res.ROOT / rel).read_bytes()).hexdigest()
    assert got == VENDORED_SHA256[rel], (
        f"{rel} differs from the vendored copy.\n"
        f"  If you EDITED it: don't — change it upstream in eeltracker and re-drop.\n"
        f"  If you RE-DROPPED it: update VENDORED_SHA256[{rel!r}] to {got!r} in the same\n"
        f"  commit, re-run `uv run fakefish-gen-constants`, regenerate the golden\n"
        f"  (`uv run python tests/test_loc_model.py --emit`), and re-read\n"
        f"  docs/LOCALIZATION_GENERATIVE_SPEC.md for what moved."
    )


def test_vendored_sampler_imports_nothing_from_fakefish() -> None:
    """The sampler must stay a leaf: numpy + stdlib only.

    That is what lets it be a plain copy of the upstream file. An import of anything in
    fakefish would make the next drop a merge instead of a copy.
    """
    tree = ast.parse((_res.ROOT / "src/fakefish/loc_model.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert "fakefish" not in imported, f"vendored sampler imports fakefish: {imported}"


def test_spec_is_shipped_alongside_the_model() -> None:
    """The caveats travel with the numbers, or the numbers get misread."""
    spec = _res.ROOT / "docs" / "LOCALIZATION_GENERATIVE_SPEC.md"
    assert spec.exists(), "docs/LOCALIZATION_GENERATIVE_SPEC.md is the model's provenance"
    text = spec.read_text()
    # The caveats most likely to be lost if the spec is ever summarised away.
    assert "Long silences are real" in text          # do not clamp the tail
    assert "The diel cycle is not in the model" in text
    assert "hazard is per pulse, not per second" in text


# ===== what the C port assumes ========================================================


def test_generated_header_matches_the_vendored_parameters(params: dict) -> None:
    """``loc_model_params.h`` is a rendering of the JSON, not a transcription of the spec.

    The specific trap: the spec's §7 C listing disagrees with the fitted parameters it sits
    beside. Anything hand-typed from it would be wrong in a way that still runs.
    """
    header = (_res.FIRMWARE_DIR / "loc_model_params.h").read_text()
    assert gc._LOC_BANNER in header
    assert f"{params['state']['tau_fast_s']!r}f" in header, "tau_fast is not the fitted value"
    assert f"{params['noise']['mix_sd_lo']!r}f" in header
    assert f"{params['reference_statistics']['resting_ipi_median_hz']!r}f" in header
    # The burst hazard is deliberately NOT ported (see loc_rhythm.h) — the header may
    # explain its absence in prose, but must define nothing a future edit could wire up.
    code = [ln for ln in header.splitlines() if ln.strip() and not ln.lstrip().startswith("//")]
    assert not [ln for ln in code if "haz" in ln.lower()], code


def test_codegen_rejects_a_non_uniform_grid(params: dict) -> None:
    """The C indexes both tables by arithmetic, which needs a uniform grid.

    A re-drop that re-spaced either grid must fail in the codegen, loudly, rather than
    read the wrong knot forever after.
    """
    bad = json.loads(json.dumps(params))
    bad["marginal"]["z_knots"][7] += 0.01
    with pytest.raises(ValueError, match="not evenly spaced"):
        gc.validate_loc_model(bad)

    bad = json.loads(json.dumps(params))
    bad["knobs"]["randomness"][3] += 0.01
    with pytest.raises(ValueError, match="not evenly spaced"):
        gc.validate_loc_model(bad)


def test_codegen_rejects_a_broken_model(params: dict) -> None:
    """The other three things loc_rhythm.h relies on, each checked by breaking it."""
    bad = json.loads(json.dumps(params))
    bad["state"]["var_white"] += 0.1
    with pytest.raises(ValueError, match="not 1"):
        gc.validate_loc_model(bad)

    bad = json.loads(json.dumps(params))
    bad["marginal"]["log_ipi_knots"][20] = bad["marginal"]["log_ipi_knots"][19] - 1.0
    with pytest.raises(ValueError, match="not strictly increasing"):
        gc.validate_loc_model(bad)

    bad = json.loads(json.dumps(params))
    bad["knobs"]["gain_median_anchor"][10] = 1.5
    with pytest.raises(ValueError, match="mis-scaled at its own reference point"):
        gc.validate_loc_model(bad)


def test_the_table_floor_clears_the_scheduler_refractory(params: dict) -> None:
    """locgen is a single-pulse scheduler; overlapping localization pulses would break it.

    The model's fastest interval at tempo 1 must stay well above ``LOC_REFRACTORY_SAMP``,
    or the clamp in ``loc_rhythm_next_ipi_samp`` would stop being a safety net and start
    shaping the distribution.
    """
    from fakefish.export_teensy_stimuli import PLAYBACK_RATE_HZ

    floor_samp = np.exp(params["marginal"]["log_ipi_knots"][0]) * PLAYBACK_RATE_HZ
    levels = (_res.FIRMWARE_DIR / "stim_levels.h").read_text()
    refractory = next(
        int(ln.split()[2].rstrip("u"))
        for ln in levels.splitlines()
        if ln.startswith("#define LOC_REFRACTORY_SAMP")
    )
    assert floor_samp > 4 * refractory, (floor_samp, refractory)


# ===== the golden: the C's oracle =====================================================


class _ScriptedNoise(LocalizationModel):
    """The reference model with its noise draws replaced by a fixed sequence.

    Overriding :meth:`noise` (rather than seeding an RNG) is what makes the recurrence
    reproducible in a language that cannot share numpy's generator. The C self-test is fed
    the identical sequence and must land on the identical state.
    """

    def __init__(self, params: dict, draws: list[float]):
        super().__init__(params)
        self._draws = list(draws)
        self._i = 0

    def noise(self, rng, size=None):  # noqa: ARG002 — the scripted sequence replaces rng
        n = 1 if size is None else int(size)
        out = np.array(self._draws[self._i:self._i + n], float)
        self._i += n
        return out[0] if size is None else out


#: ``(rate, randomness, component noise scale, white noise scale)`` per block.
#:
#: The last block clamps the interval table hard at both ends — the clamp is hit on every
#: long silence, so it belongs in the oracle rather than in an untested branch. It does so
#: through the WHITE term only, which enters the score and never feeds back into the state.
#: Inflating the component noise instead would drive the OU state to 6 sigma, where float32
#: cancellation against a float64 oracle costs a few hundred ulps and the comparison stops
#: measuring the formula and starts measuring the arithmetic.
GOLDEN_BLOCKS = (
    (1.0, 1.0, 1.0, 1.0),   # the measured eel
    (0.5, 0.5, 1.0, 1.0),   # slow + tame: tempo < 1, gain < 1
    (2.0, 1.5, 1.0, 1.0),   # fast + wild: the top of the useful knob range
    (1.0, 0.0, 1.0, 1.0),   # metronome — randomness 0 must collapse the score exactly
    (1.0, 1.0, 1.0, 9.0),   # deliberate table clamping at both ends
)
GOLDEN_STEPS = 50


def render_golden(params: dict) -> str:
    """Render the golden CSV from the reference implementation. Generated, not authored."""
    n_comp = len(LocalizationModel(params).tau)
    rng = np.random.default_rng(20260821)
    lines = [
        "# GENERATED by tests/test_loc_model.py from src/fakefish/loc_model.py — DO NOT EDIT.",
        "#",
        "# The oracle for firmware/eel_core/loc_rhythm.h. Each row injects the four noise",
        "# draws one interval consumes and records the state the REFERENCE implementation",
        "# reaches. The C replays the same sequence and must agree; regenerate with",
        "#   uv run python tests/test_loc_model.py --emit",
        "#",
        "# Every block starts with a nonzero pending_dt (dt0) so each row consumes exactly",
        "# four draws. The reference skips the component noise entirely when no time has",
        "# passed, which happens only on the very first call of a power-on; the C mirrors that",
        "# branch and loc_rhythm_selftest asserts it separately rather than folding a",
        "# variable-width row into this table.",
        "block,rate,randomness,tempo,offset,dt0,step,n0,n1,n2,n3,x0,x1,x2,m,ipi_s",
    ]
    for b, (rate, randomness, comp_scale, white_scale) in enumerate(GOLDEN_BLOCKS):
        draws = list(rng.normal(0.0, 1.0, GOLDEN_STEPS * (n_comp + 1) + 8))
        # The white term is every (n_comp+1)-th draw; scaling only it clamps the interval
        # table without disturbing the state recurrence (see GOLDEN_BLOCKS).
        for i, _ in enumerate(draws):
            draws[i] *= white_scale if i % (n_comp + 1) == n_comp else comp_scale
        m = _ScriptedNoise(params, draws)
        m.rate, m.randomness = rate, randomness
        tempo = m.rate / m._gain
        # An explicit state, not new_state(): the golden tests the RECURRENCE, and a burn-in
        # would only bury it under 40 rows of the same arithmetic.
        offset = float(0.1 * (b + 1) - 0.2)
        dt0 = 0.25
        st = RestingState(x=np.array([0.3, -0.2, 0.05][:n_comp]), offset=offset,
                          pending_dt=dt0)
        st.m = st.offset + float(st.x.sum())
        for step in range(GOLDEN_STEPS):
            used = draws[m._i:m._i + n_comp + 1]
            ipi = m.next_interval(st, None)
            lines.append(
                f"{b},{rate!r},{randomness!r},{tempo:.17g},{offset!r},{dt0!r},{step},"
                + ",".join(f"{v:.17g}" for v in used)
                + ","
                + ",".join(f"{v:.17g}" for v in st.x)
                + f",{st.m:.17g},{ipi:.17g}"
            )
    return "\n".join(lines) + "\n"


def test_golden_matches_the_reference_implementation(params: dict) -> None:
    """The committed golden must be what the vendored model produces today.

    This is the Python half of the two-sided check: it stops the golden drifting from the
    model. The C half lives in ``check.sh`` (``loc_rhythm_selftest``), which stops the
    firmware drifting from the golden.
    """
    assert GOLDEN.exists(), f"{GOLDEN} is missing — regenerate it"
    assert GOLDEN.read_text() == render_golden(params), (
        "tests/data/loc_rhythm_golden.csv is stale — regenerate it with\n"
        "  uv run python tests/test_loc_model.py --emit\n"
        "and re-run check.sh so the C self-test is verified against the new oracle."
    )


def test_golden_exercises_both_ends_of_the_interval_table(params: dict) -> None:
    """A golden that never clamps would leave the clamp untested in the C.

    The clamp is not an edge case: the top of the table is where every multi-second silence
    comes from, and the bottom is what keeps the resting rhythm out of volley territory.
    """
    ipi = np.array([float(ln.split(",")[-1]) for ln in GOLDEN.read_text().splitlines()
                    if ln and not ln.startswith(("#", "block"))])
    lo = np.exp(params["marginal"]["log_ipi_knots"][0])
    hi = np.exp(params["marginal"]["log_ipi_knots"][-1])
    # Blocks run at tempo != 1, so compare against the widest possible scaled bounds.
    assert ipi.min() <= lo / 0.4, ipi.min()
    assert ipi.max() >= hi / 3.0, ipi.max()


# ===== the model as wired, at the device's knob settings ==============================


def test_the_rhythm_is_not_a_renewal_process(model: LocalizationModel) -> None:
    """The whole point: consecutive log-intervals are CORRELATED.

    Real recordings give 0.55 at lag 1 and 0.36 at lag 5; a Poisson or lognormal renewal
    process — which is what this device used until 2026-08-21 — is flat at zero at every
    lag. A regression here means someone replaced the wandering state with an independent
    draw per interval, which is the one thing the model exists to prevent.
    """
    rng = np.random.default_rng(7)
    st = model.new_state(rng)
    ipi = np.array([model.next_interval(st, rng) for _ in range(20000)])
    y = np.log(ipi) - np.log(ipi).mean()
    acf = [float(np.mean(y[:-k] * y[k:]) / y.var()) for k in (1, 5, 20)]
    assert acf[0] > 0.40, acf
    assert acf[1] > 0.25, acf
    assert acf[2] > 0.10, acf


def test_the_knobs_are_orthogonal(model: LocalizationModel) -> None:
    """Rate is a pure time dilation; every pulse-indexed statistic depends on randomness alone.

    Measured across an 8-fold rate range the texture must not move — that is what stretching
    the time constants with the intervals buys (gamma = 1 beats gamma = 0 by 413 nats). If
    someone "simplifies" the tempo out of the state update, CV2 and the autocorrelation
    start sliding with the rate knob and the device drifts back toward a renewal process at
    exactly the slow settings.
    """
    def texture(rate: float) -> tuple[float, float]:
        model.rate, model.randomness = rate, 1.0
        rng = np.random.default_rng(3)
        st = model.new_state(rng)
        ipi = np.array([model.next_interval(st, rng) for _ in range(12000)])
        cv2 = float(np.median(2 * np.abs(np.diff(ipi)) / (ipi[1:] + ipi[:-1])))
        y = np.log(ipi) - np.log(ipi).mean()
        return cv2, float(np.mean(y[:-1] * y[1:]) / y.var())

    slow, fast = texture(0.4), texture(3.2)
    model.rate = 1.0
    assert abs(slow[0] - fast[0]) < 0.06, (slow, fast)   # CV2
    assert abs(slow[1] - fast[1]) < 0.10, (slow, fast)   # lag-1 autocorrelation


def _tick_and_dose(model: LocalizationModel, rate: float, randomness: float) -> tuple[float, float]:
    """(tick tempo Hz, pulses per second) at one knob setting.

    Averaged over fresh states: the slow component has not mixed inside any one run, so a
    single power-on sits at its own offset (spec §5, individual variation reproduced on
    purpose). ``offset=0.0`` pins that out so the measurement is of the knob, not the fish.
    """
    model.rate, model.randomness = rate, randomness
    rng = np.random.default_rng(11)
    ipi = np.concatenate([
        [model.next_interval(st, rng) for _ in range(1200)]
        for st in (model.new_state(rng, offset=0.0) for _ in range(6))
    ])
    return 1.0 / float(np.median(ipi)), 1.0 / float(ipi.mean())


def test_rate_is_a_pure_time_dilation(model: LocalizationModel) -> None:
    """Doubling the rate knob exactly halves every interval, at any randomness.

    This is the knob's real guarantee, and it is exact because ``rate`` divides the
    intervals and stretches the time constants by the same factor. It is what lets the CH3
    ladder be labelled in Hz at all.
    """
    for randomness in (0.0, 1.0, 1.5):
        base, _ = _tick_and_dose(model, 1.0, randomness)
        for rate in (0.4, 2.0, 3.2):
            tick, _ = _tick_and_dose(model, rate, randomness)
            assert abs(tick / (rate * base) - 1.0) < 0.02, (rate, randomness, tick, base)
    model.rate, model.randomness = 1.0, 1.0


def test_the_median_anchor_holds_the_tempo_while_the_dose_moves(model: LocalizationModel) -> None:
    """Why this device anchors the MEDIAN, stated as the test that would fail if it did not.

    A heavy-tailed interval distribution cannot hold its median and its mean at once. Across
    the useful randomness range the shipped gain table keeps the TICK TEMPO inside a narrow
    band while the pulse DOSE moves several-fold — so CH3's Hz scale keeps meaning "the fish
    ticks at this rate", and sweeping randomness is a change of texture rather than of tempo.

    The band is not perfectly flat, and that is the shipped model, not a wiring error: spec
    §5.2's measured table reports 2.93 / 3.04 / 3.11 / 3.73 Hz at randomness 0 / 0.5 / 1 /
    1.5, which is what ``knobs.gain_median_anchor`` produces. (``knobs.response`` also
    carries a per-setting ``gain_median_anchor`` that differs slightly; it is diagnostic
    output from the fitting pass, not what the reference reads. Do not substitute it.)
    """
    ticks, doses = zip(*(_tick_and_dose(model, 1.0, s) for s in (0.0, 0.5, 1.0, 1.5)),
                       strict=True)
    model.rate, model.randomness = 1.0, 1.0

    nominal = model.params["reference_statistics"]["resting_ipi_median_hz"]
    assert 0.90 * nominal < min(ticks) and max(ticks) < 1.25 * nominal, ticks
    assert max(ticks) / min(ticks) < 1.4, ticks          # tempo: a narrow band ...
    assert max(doses) / min(doses) > 2.5, doses          # ... dose: several-fold
    assert doses[0] > doses[-1], doses                   # long silences appear with randomness


def test_randomness_zero_is_a_metronome(model: LocalizationModel) -> None:
    """The knob's bottom end must be exactly periodic, at the nominal tempo.

    That is the one setting an operator can verify by eye on the LED, and it is what the
    retired jitter knob did at CV 0.
    """
    model.randomness = 0.0
    rng = np.random.default_rng(5)
    st = model.new_state(rng)
    ipi = np.array([model.next_interval(st, rng) for _ in range(200)])
    assert float(ipi.std()) < 1e-9, float(ipi.std())
    model.randomness = 1.0


def test_long_silences_survive(model: LocalizationModel) -> None:
    """1.5 % of resting intervals exceed 5 s, and that is measured behaviour, not a bug.

    The pulses bracketing a real 30 s silence are exactly as loud as the rest of the
    recording, so the fish did not swim away — it stopped. A well-meaning clamp on the tail
    would be the most tempting change to make here and the most wrong.
    """
    rng = np.random.default_rng(13)
    st = model.new_state(rng)
    ipi = np.array([model.next_interval(st, rng) for _ in range(40000)])
    assert float(np.mean(ipi > 5.0)) > 0.005, float(np.mean(ipi > 5.0))
    assert float(ipi.max()) > 10.0, float(ipi.max())


if __name__ == "__main__":
    if "--emit" not in sys.argv:
        raise SystemExit("usage: python tests/test_loc_model.py --emit")
    Path(GOLDEN).write_text(render_golden(gc.load_loc_model()))
    print(f"wrote {GOLDEN}")
