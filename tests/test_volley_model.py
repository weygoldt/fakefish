"""Guard the vendored volley model and the wiring that makes it playable.

The generative model is **not fitted in this repo**. ``src/fakefish/volley_model.py`` and
``data/volley_model_params.json`` are byte-identical copies of the reference sampler and
fitted parameters from the eeltracker ``volley_dynamics`` analysis, whose own
``volley_validate.py`` is the model's regression test. What can go wrong *here* is
therefore not "is the model right" but:

* someone edits a vendored file in place instead of re-dropping it from upstream, so a
  later re-drop silently reverts a fix (the same failure mode invariant 1 guards for the
  firmware core, and invariant 2 for the generated constants);
* the vendored module grows a dependency on fakefish and stops being droppable;
* the wiring in ``synthetic_volleys`` quietly undoes one of the three properties the spec
  calls measurements rather than modelling choices — near-clockwork timing, no ramp-up, a
  smoothly decaying amplitude envelope;
* the sample-grid / IPI-floor / amplitude-normalisation transforms stop doing what the
  library's playback safety depends on.

The sha256 pins are not integrity theatre: they are what turns "do not edit the vendored
files" from a comment into a gate. Updating the model means dropping in new files AND
updating these two constants in the same commit, which is exactly the moment to re-read
``docs/VOLLEY_GENERATIVE_SPEC.md``.
"""

from __future__ import annotations

import ast
import hashlib

import numpy as np
import pytest

from fakefish import _resources as _res
from fakefish import synthetic_volleys as sv
from fakefish.volley_model import VolleyModel

#: sha256 of the vendored files as dropped from
#: ``eeltracker/analyses/volley_dynamics/{scripts/volley_model.py,model/volley_model_params.json}``.
VENDORED_SHA256 = {
    "src/fakefish/volley_model.py":
        "fd61613f7af557e628974b2ff2dc322298663e86d71b740d50f578e4e0eac665",
    "data/volley_model_params.json":
        "4535420e1a25bbe6e99699f82640600335ec0f255888d22277cbf4262f858bd3",
}


@pytest.fixture(scope="module")
def model() -> VolleyModel:
    return sv._load_volley_model()


@pytest.fixture(scope="module")
def volleys(model: VolleyModel) -> list[np.ndarray]:
    """A population big enough for distribution claims, small enough to stay fast."""
    rng = np.random.default_rng(20260821)
    return [sv.generate_volley(model, rng, f"t{i}") for i in range(200)]


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
        f"  commit, and re-read docs/VOLLEY_GENERATIVE_SPEC.md for what moved."
    )


def test_vendored_sampler_imports_nothing_from_fakefish() -> None:
    """The sampler must stay a leaf: numpy + stdlib only.

    That is what lets it be a plain copy of the upstream file. An import of anything in
    fakefish would make the next drop a merge instead of a copy.
    """
    tree = ast.parse((_res.ROOT / "src/fakefish/volley_model.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert "fakefish" not in imported, f"vendored sampler imports fakefish: {imported}"


def test_spec_is_shipped_alongside_the_model() -> None:
    """The caveats travel with the numbers, or the numbers get misread."""
    spec = _res.ROOT / "docs" / "VOLLEY_GENERATIVE_SPEC.md"
    assert spec.exists(), "docs/VOLLEY_GENERATIVE_SPEC.md is the model's provenance"
    text = spec.read_text()
    # The three caveats most likely to be lost if the spec is ever summarised away.
    assert "Do not ask for intervals below 2 ms" in text
    assert "not a usable measurement" in text  # the amplitude step
    assert "There is no rise" in text


# ===== the retired in-repo fit stays retired ==========================================


def test_old_volley_fit_is_gone() -> None:
    """The hand-rolled fit was superseded, not left running in parallel.

    Two models producing volleys is how a population silently gets built from the wrong
    one. ``data/volley_model.json`` held the retired fit's parameters.
    """
    assert not (_res.DATA_DIR / "volley_model.json").exists()
    for name in ("fit_volley_params", "fit_model", "save_model", "load_model",
                 "refit_peaks", "_volley_decay_envelope", "VOLLEY_DECAY_FLOOR"):
        assert not hasattr(sv, name), f"{name} belongs to the retired volley fit"


# ===== the three properties the spec calls measurements ===============================


def test_volleys_are_near_clockwork_not_poisson(volleys) -> None:
    """CV2 ~ 0.12, where a Poisson process gives 1.0.

    This is the spec's single most important realism point: exponential intervals would
    reproduce the mean rate and destroy the texture. A regression here means someone
    replaced the rate-integrating sampler with a point process.
    """
    cv2s = []
    for v in volleys:
        ipi = np.diff(v.times_s)
        if ipi.size < 3:
            continue
        cv2s.append(np.median(2 * np.abs(np.diff(ipi)) / (ipi[1:] + ipi[:-1])))
    assert 0.05 < float(np.median(cv2s)) < 0.30, float(np.median(cv2s))


def test_volleys_start_at_their_peak_rate(volleys) -> None:
    """No ramp-up: median time from onset to peak rate is 18 ms, lower quartile 0.

    Measured as a fraction of the volley, the spec's median is 0.04. A synthesised
    onset envelope — which earlier versions of this repo deliberately excluded, and
    which a well-meaning "make it more realistic" change would add back — shows up here.
    """
    fracs = []
    for v in volleys:
        ipi = np.diff(v.times_s)
        if ipi.size < 10:
            continue
        # peak of a 5-interval rolling median, so one lucky jitter draw is not "the peak"
        win = np.lib.stride_tricks.sliding_window_view(1.0 / ipi, 5)
        i = int(np.argmax(np.median(win, axis=1)))
        fracs.append(v.times_s[i] / v.times_s[-1])
    assert float(np.median(fracs)) < 0.25, float(np.median(fracs))


def test_amplitude_envelope_is_smooth_and_mostly_decaying(volleys) -> None:
    """~20 % decay across the volley, with only ~1.4 % pulse-to-pulse jitter.

    Both halves matter. A flat envelope means the fitted trend was dropped; a ragged one
    means the per-pulse jitter was inflated back to the superseded ~23 % estimate that
    the raw-cutout measurement replaced (spec §1.4).
    """
    ratios, jitters = [], []
    for v in volleys:
        if v.rel_amp.size < 20:
            continue
        n = v.rel_amp.size // 5
        ratios.append(float(np.median(v.rel_amp[-n:]) / np.median(v.rel_amp[:n])))
        # robust pulse-to-pulse step, immune to the envelope itself
        d = np.abs(np.diff(np.log10(v.rel_amp)))
        jitters.append(float(np.median(d)))
    assert 0.70 < float(np.median(ratios)) < 0.95, float(np.median(ratios))
    assert float(np.median(jitters)) < 0.02, float(np.median(jitters))


# ===== the transforms that make it playable ===========================================


def test_every_interval_clears_the_ipi_floor(volleys) -> None:
    """Below the floor, pulses collide spike-on-spike and the overlap-add sum saturates.

    Checked in SAMPLES, because that is the unit the device works in and the unit the
    floor is defined in — a float-seconds check would pass on a train that rounds to 99
    samples at export.
    """
    for v in volleys:
        samp = np.round(np.diff(v.times_s) * sv.ex.PLAYBACK_RATE_HZ).astype(int)
        assert samp.min() >= sv.SYNTH_MIN_IPI_SAMP, (v.label, samp.min())


def test_pulse_times_land_exactly_on_the_sample_grid(volleys) -> None:
    """Generation happens on the grid so QC measures what the device plays.

    If times drift off the grid, ``ipi_samples_from_times`` rounds them at export and the
    population that was QC'd is not the population that ships.
    """
    for v in volleys[:20]:
        samp = v.times_s * sv.ex.PLAYBACK_RATE_HZ
        assert np.allclose(samp, np.round(samp), atol=1e-6), v.label


def test_relative_amplitude_fits_the_byte_encoding(volleys) -> None:
    """``rel_amp`` is encoded 0..255 as 0..1, so it must be normalised and positive.

    The model's own amplitude is relative to a volley's MEDIAN pulse and routinely
    exceeds 1.0 near onset — handing that to the export unnormalised would clip the
    loudest pulses of every volley.
    """
    for v in volleys:
        assert v.rel_amp.min() > 0.0, v.label
        assert v.rel_amp.max() == pytest.approx(1.0), (v.label, v.rel_amp.max())


def test_ipi_floor_barely_perturbs_the_population(model) -> None:
    """The floor must be a safety net, not a shaper.

    It clamps ~6 % of intervals, each by microseconds. If enforcing it measurably shortens
    or slows volleys, the floor has become a modelling choice — which is the failure the
    retired 2.5 ms floor actually had (it cost 11 % of the sustained peak).
    """
    rng_a = np.random.default_rng(4)
    rng_b = np.random.default_rng(4)
    raw = [model.sample_volley(rng_a, sv.VOLLEY_KIND)[0] for _ in range(60)]
    clamped = [sv.generate_volley(model, rng_b, "").times_s for _ in range(60)]
    stretch = [c[-1] / r[-1] for r, c in zip(raw, clamped, strict=True) if r.size > 5]
    assert float(np.median(stretch)) == pytest.approx(1.0, abs=0.01)


def test_no_volley_overlap_clips_the_output(model) -> None:
    """The reason the floor exists, checked end-to-end against the real EOD waveform.

    ``EOD_HV`` is 2.62 ms long but carries 99 % of its energy in 1.92 ms, so a 2.0 ms
    floor leaves only harmless tail-on-spike overlap. This reconstructs the summed trace
    the overlap-add engine would produce and asserts it never exceeds full scale.
    """
    cfg = sv.ex.Config.from_yaml(_res.DEFAULT_CONFIG)
    w = sv._eod_waveform(cfg)
    rng = np.random.default_rng(11)
    worst = 0.0
    for _ in range(40):
        v = sv.generate_volley(model, rng, "")
        trace = sv._reconstruct_with_amp(w, *sv._synthetic_to_ipi_amp(v))
        worst = max(worst, float(trace.max()))
    assert worst <= 1.001, worst


# ===== the population as shipped ======================================================


def test_population_fits_the_pulse_log_item_ceiling() -> None:
    """``pulse_log.h`` stores the library item in an int8_t (-1 = absent).

    So the whole library — real scenes, synthetic volleys and localization trains — must
    stay under 128 items. Growing past it is a log FORMAT change plus its golden file
    (invariant 9), not a bump of ``N_SYNTH_VOLLEYS``.
    """
    header = (_res.FIRMWARE_DIR / "pulse_log.h").read_text()
    assert "int8_t   item;" in header, "pulse-log item field changed — re-derive the ceiling"
    n_synth = sv.N_SYNTH_VOLLEYS + len(sv.LOC_SYNTH_RATES_HZ)
    assert n_synth <= 128, n_synth
    lib = _res.DEFAULT_FIRMWARE.with_suffix(".h").read_text()
    for line in lib.splitlines():
        if line.startswith("#define N_STIM_ITEMS"):
            assert int(line.split()[-1]) <= 128, line
            break
    else:
        pytest.fail("N_STIM_ITEMS not found in the generated library header")


def test_rc_item_window_matches_the_exported_library() -> None:
    """The RC device draws items ``FIRST .. FIRST+COUNT``; they must all be volleys.

    An off-by-one here is silent on the bench and fatal in the field: the device would
    fire a localization train as a hunting volley, or index past the table.
    """
    rc = (_res.ROOT / "firmware" / "eel_fakefish_rc" / "rc_control.h").read_text()
    vals = {}
    for line in rc.splitlines():
        for name in ("RC_VOLLEY_ITEM_FIRST", "RC_VOLLEY_ITEM_COUNT"):
            if line.startswith(f"#define {name} "):
                vals[name] = int(line.split()[-1])
    assert set(vals) == {"RC_VOLLEY_ITEM_FIRST", "RC_VOLLEY_ITEM_COUNT"}, vals
    assert vals["RC_VOLLEY_ITEM_COUNT"] == sv.N_SYNTH_VOLLEYS, (
        "RC_VOLLEY_ITEM_COUNT must track N_SYNTH_VOLLEYS", vals
    )

    items = sv.ex.parse_firmware(_res.DEFAULT_FIRMWARE)["items"]
    lo = vals["RC_VOLLEY_ITEM_FIRST"]
    hi = lo + vals["RC_VOLLEY_ITEM_COUNT"]
    assert hi <= len(items), (hi, len(items))
    for i in range(lo, hi):
        assert items[i]["kind"] == sv.ex.STIM_SYNTH_VOLLEY, (i, items[i]["kind"])
    assert items[hi - 1]["kind"] == sv.ex.STIM_SYNTH_VOLLEY
    if hi < len(items):
        assert items[hi]["kind"] != sv.ex.STIM_SYNTH_VOLLEY, (
            "the volley window stops short of the last synthetic volley", hi
        )


def test_shipped_population_matches_the_fitted_distribution() -> None:
    """The population on disk must still look like the model it claims to come from.

    Quartiles are compared against ``docs/VOLLEY_GENERATIVE_SPEC.md`` §2.1 (real strong
    volleys: duration 0.279 / 0.466 / 0.751 s, 58 / 88 / 142 pulses). Wide bounds on
    purpose — this catches a wiring mistake (wrong ``kind``, a lost transform, a stale
    npz), not a modelling drift, which is upstream's regression test to catch.
    """
    pop = sv.load_synthetic(_res.DATA_DIR / "synthetic_population.npz")
    vol = [v for v in pop if v.kind == "volley"]
    assert len(vol) == sv.N_SYNTH_VOLLEYS, len(vol)

    med_dur = float(np.median([v.times_s[-1] for v in vol]))
    med_n = float(np.median([v.times_s.size for v in vol]))
    assert 0.35 < med_dur < 0.60, med_dur
    assert 60 < med_n < 130, med_n

    # No localization train may reach into volley territory, and no volley may be slow
    # enough to read as localization — the two families have to stay separable by rate.
    for v in pop:
        ipi_ms = np.diff(v.times_s) * 1e3
        if v.kind == "volley":
            assert ipi_ms.min() < sv.VOLLEY_PEAK_MAX_IPI_MS, v.label
        else:
            assert ipi_ms.min() > 20.0, v.label


def test_generate_volley_is_deterministic_for_a_seed(model) -> None:
    """The library is a committed artifact; regenerating it must reproduce it.

    Non-determinism here would mean every re-export churns 100 items and the byte-frozen
    library (invariant 3) can never be verified.
    """
    a = sv.generate_volley(model, np.random.default_rng(99), "a")
    b = sv.generate_volley(model, np.random.default_rng(99), "b")
    assert np.array_equal(a.times_s, b.times_s)
    assert np.array_equal(a.rel_amp, b.rel_amp)


def test_localization_trains_do_not_depend_on_the_volley_stream(model, monkeypatch) -> None:
    """Decoupled RNG streams: a volley-side change must not re-roll localization.

    ``build_population`` draws volleys and localization from two independent generators
    precisely so that changing the volley model — which changes how many random draws a
    volley consumes — leaves every localization item byte-identical. That is what makes a
    model swap reviewable: localization rows in the export diff are then either unchanged,
    or a genuine regression.

    Proved by rebuilding with a different number of volleys, which is the largest possible
    perturbation of the volley stream's draw count.
    """
    def loc_of(pop):
        return {v.label: v.times_s for v in pop if v.kind == "localization"}

    a = loc_of(sv.build_population(model, seed=0))
    monkeypatch.setattr(sv, "N_SYNTH_VOLLEYS", 7)
    b = loc_of(sv.build_population(model, seed=0))

    assert set(a) == set(b) and a, sorted(a)
    for label in a:
        assert np.array_equal(a[label], b[label]), (
            f"{label} moved when only the VOLLEY count changed — the RNG streams have "
            f"been re-coupled"
        )
