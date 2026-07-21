"""Tests for the Teensy stimulus export tool (format v2).

Exercise the pure library functions (no NIX data): the waveform pipeline, IPI
construction (µs and 50 kHz sample-units), the >100 Hz-peak volley gate, the unified
StimItem C emission + round-trip parse, and the shared additive-mixer engine. The
tool ships as the installed ``fakefish`` package, so it imports directly.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from fakefish import export_teensy_stimuli as ex


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _synthetic_template(
    n: int = 289, peak_at: int = 144, width: float = 8.0
) -> np.ndarray:
    """A monophasic, peak-normalised, flanks-at-zero pulse (stand-in for the template)."""
    x = np.arange(n) - peak_at
    w = np.exp(-0.5 * (x / width) ** 2)
    w -= 0.15 * np.exp(-0.5 * ((x - 3 * width) / width) ** 2)
    w = w - np.mean(np.concatenate([w[:20], w[-20:]]))
    return w / np.max(np.abs(w))


@pytest.fixture
def cfg(tmp_path: Path) -> "ex.Config":
    return ex.Config(
        eods_root=tmp_path,
        alignment_json=None,
        firmware_dir=tmp_path / "firmware",
        out_dir=tmp_path / "out",
        sites=[],
        trim_frac=0.005,
    )


@pytest.fixture
def waveform(cfg) -> "ex.Waveform":
    return ex.build_waveform(_synthetic_template(), ex.DEFAULT_SAMPLE_RATE_HZ, cfg)


def _make_items() -> list[ex.StimItemRec]:
    hz = ex.PLAYBACK_RATE_HZ
    v0 = ex.ipi_samples_from_times(np.cumsum([0.0, 0.02, 0.03, 0.05, 0.02]), hz)
    v1 = ex.ipi_samples_from_times(np.cumsum([0.0, 0.015, 0.04, 0.02]), hz)
    ramp = ex.rel_amp_to_u8(np.array([0.1, 0.25, 1.0, 1.0]))
    loc = ex.ipi_samples_from_times(np.cumsum([0.0, 0.1, 0.12, 0.1, 0.11]), hz)
    return [
        ex.StimItemRec("recA#ev1", ex.STIM_REAL_VOLLEY, 0, v0, None),
        # a per-pulse rel_amp array still round-trips through the format even though the
        # shipped synthetic volleys no longer carry one — keep the envelope path covered.
        ex.StimItemRec("synthB", ex.STIM_SYNTH_VOLLEY, 1, v1, ramp),
        ex.StimItemRec("recA#track3", ex.STIM_LOCALIZATION, 2, loc, None),
    ]


def _make_gaps(n: int) -> np.ndarray:
    """A small deterministic per-item lead-in-gap array for the emit round-trip tests."""
    return np.arange(1, n + 1, dtype=np.uint16) * 100  # 100, 200, 300, ... samples


# --------------------------------------------------------------------------
# waveform invariants
# --------------------------------------------------------------------------


def test_waveform_endpoints_exactly_zero(waveform):
    assert waveform.samples_i16[0] == 0
    assert waveform.samples_i16[-1] == 0
    assert waveform.samples_norm[0] == 0.0
    assert waveform.samples_norm[-1] == 0.0


def test_waveform_peak_is_full_scale(waveform):
    assert np.abs(waveform.samples_i16).max() == 32767


def test_waveform_playback_rate_is_50k(waveform):
    assert waveform.playback_hz == ex.PLAYBACK_RATE_HZ
    assert waveform.n == pytest.approx(waveform.pre_resample_norm.size * 25 / 24, abs=2)


def test_resample_ratio_48_to_50():
    assert ex._resample_ratio(50000, 48000.0) == (25, 24)


# --------------------------------------------------------------------------
# IPI construction
# --------------------------------------------------------------------------


def test_ipi_first_zero_rest_positive():
    ipi = ex.ipi_us_from_times(np.array([1.0, 1.01, 1.05, 1.06, 1.10]))
    assert ipi[0] == 0
    assert np.all(ipi[1:] > 0)
    assert ipi.dtype == np.uint32


def test_ipi_samples_from_times_units_and_ceiling():
    # 2 ms, 8 ms @ 50 kHz -> 100, 400 samples
    ipi = ex.ipi_samples_from_times(np.array([0.0, 0.002, 0.010]), 50000)
    assert ipi.tolist() == [0, 100, 400]
    assert ipi.dtype == np.uint16
    # a >1.31 s gap overflows uint16 and must raise
    with pytest.raises(ValueError):
        ex.ipi_samples_from_times(np.array([0.0, 2.0]), 50000)


def test_rel_amp_to_u8():
    u = ex.rel_amp_to_u8(np.array([0.0, 0.1, 0.25, 1.0]))
    assert u.tolist() == [0, 26, 64, 255]
    assert u.dtype == np.uint8


# --------------------------------------------------------------------------
# >100 Hz-peak volley gate
# --------------------------------------------------------------------------


def test_item_peak_rate_hz():
    hz = ex.PLAYBACK_RATE_HZ
    # fastest interval 2 ms (100 samp) -> peak 500 Hz
    fast = ex.ipi_samples_from_times(np.cumsum([0.0, 0.05, 0.002, 0.05]), hz)
    it = ex.StimItemRec("v", ex.STIM_SYNTH_VOLLEY, 0, fast, None)
    assert ex.item_peak_rate_hz(it, hz) == pytest.approx(500.0, rel=1e-6)
    # a single-pulse item has no interval -> rate 0
    one = ex.StimItemRec("v1", ex.STIM_REAL_VOLLEY, 0, np.array([0], np.uint16), None)
    assert ex.item_peak_rate_hz(one, hz) == 0.0


def test_enforce_volley_peak_drops_slow_volleys_keeps_localization():
    hz = ex.PLAYBACK_RATE_HZ
    slow = ex.ipi_samples_from_times(np.cumsum([0.0, 0.02, 0.03, 0.02]), hz)  # 50 Hz peak
    fast = ex.ipi_samples_from_times(np.cumsum([0.0, 0.05, 0.004, 0.05]), hz)  # 250 Hz peak
    loc = ex.ipi_samples_from_times(np.cumsum([0.0, 0.1, 0.12, 0.1]), hz)  # ~10 Hz, never gated
    items = [
        ex.StimItemRec("slow_real", ex.STIM_REAL_VOLLEY, 0, slow, None),
        ex.StimItemRec("fast_synth", ex.STIM_SYNTH_VOLLEY, 1, fast, None),
        ex.StimItemRec("slow_synth", ex.STIM_SYNTH_VOLLEY, 2, slow, None),
        ex.StimItemRec("loc", ex.STIM_LOCALIZATION, 3, loc, None),
    ]
    kept = ex._enforce_volley_peak(items, hz, 100.0)
    labels = [it.label for it in kept]
    assert labels == ["fast_synth", "loc"]  # both slow volley-family items dropped
    # a localization train that dips below 10 ms is NOT a volley item -> never dropped
    assert any(it.kind == ex.STIM_LOCALIZATION for it in kept)


# --------------------------------------------------------------------------
# v2 StimItem C emission + round-trip
# --------------------------------------------------------------------------


def test_firmware_roundtrip_within_1_lsb(waveform, tmp_path):
    items = _make_items()
    gaps = _make_gaps(len(items))
    header, source = tmp_path / "eel_stimuli.h", tmp_path / "eel_stimuli.cpp"
    ex.emit_firmware(waveform, items, gaps, header, source)
    parsed = ex.parse_firmware(source)

    assert np.array_equal(parsed["EOD_HV"], waveform.samples_i16)
    assert (
        np.max(np.abs(parsed["EOD_HV"].astype(int) - waveform.samples_i16.astype(int)))
        <= 1
    )
    assert len(parsed["items"]) == len(items)
    for it, got in zip(items, parsed["items"], strict=True):
        assert np.array_equal(it.ipi_samp, got["ipi_samp"])
        assert it.kind == got["kind"] and it.group == got["group"]
        if it.rel_amp is None:
            assert got["rel_amp"] is None
        else:
            assert np.array_equal(it.rel_amp, got["rel_amp"])
    assert np.array_equal(parsed["lead_gap_samp"], gaps)  # per-item lead-gap round-trips


def test_ipi0_zero_rest_positive_in_firmware(waveform, tmp_path):
    items = _make_items()
    header, source = tmp_path / "eel_stimuli.h", tmp_path / "eel_stimuli.cpp"
    ex.emit_firmware(waveform, items, _make_gaps(len(items)), header, source)
    for it in ex.parse_firmware(source)["items"]:
        assert it["ipi_samp"][0] == 0
        assert np.all(it["ipi_samp"][1:] > 0)


def test_header_defines(waveform, tmp_path):
    items = _make_items()
    header, source = tmp_path / "eel_stimuli.h", tmp_path / "eel_stimuli.cpp"
    ex.emit_firmware(waveform, items, _make_gaps(len(items)), header, source)
    h = header.read_text()
    assert f"#define EOD_HV_LEN  {waveform.n}" in h
    assert f"#define STIM_SAMPLE_RATE_HZ   {ex.PLAYBACK_RATE_HZ}" in h
    assert f"#define N_STIM_ITEMS  {len(items)}" in h
    assert "#define STIM_FORMAT_VERSION   3" in h
    assert "typedef struct" in h and "StimItem" in h
    assert "extern const uint16_t STIM_LEAD_GAP_SAMP[N_STIM_ITEMS];" in h


def test_lead_gaps_deterministic_and_in_range(cfg):
    hz = ex.PLAYBACK_RATE_HZ
    g1 = ex.lead_gaps_for_items(31, cfg, hz)
    g2 = ex.lead_gaps_for_items(31, cfg, hz)
    assert g1.dtype == np.uint16 and g1.shape == (31,)
    assert np.array_equal(g1, g2)  # deterministic given cfg.seed
    lo = cfg.lead_gap_ms_lo / 1000 * hz
    hi = cfg.lead_gap_ms_hi / 1000 * hz
    assert g1.min() >= lo - 1 and g1.max() <= hi + 1  # within [lo, hi] ms (rounding slack)
    # a different seed gives a different draw (the salt is fixed, only seed varies)
    cfg2 = ex.dataclasses.replace(cfg, seed=cfg.seed + 1)
    assert not np.array_equal(ex.lead_gaps_for_items(31, cfg2, hz), g1)


def test_packet_under_budget(waveform):
    assert ex.packet_bytes(waveform, _make_items()) < 131072


# --------------------------------------------------------------------------
# additive-mixer engine (reference for the firmware)
# --------------------------------------------------------------------------


def test_reconstruct_item_sums_overlapping_pulses(waveform):
    # two pulses closer than the waveform length must ADD, not truncate
    gap = waveform.n // 3
    ipi = np.array([0, gap], dtype=np.uint16)
    trace = ex.reconstruct_item(waveform.samples_i16, ipi, None)
    # single-pulse reference
    single = ex.reconstruct_item(
        waveform.samples_i16, np.array([0], dtype=np.uint16), None
    )
    assert trace.max() > single.max()  # overlap region summed above one pulse


def test_reconstruct_item_applies_rel_amp_and_polarity(waveform):
    ipi = np.array([0, 500], dtype=np.uint16)
    full = ex.reconstruct_item(waveform.samples_i16, ipi, None)
    half = ex.reconstruct_item(
        waveform.samples_i16, ipi, ex.rel_amp_to_u8(np.array([0.5, 0.5]))
    )
    assert half.max() == pytest.approx(full.max() * (128 / 255), rel=0.02)
    neg = ex.reconstruct_item(waveform.samples_i16, ipi, None, polarity=-1)
    assert np.allclose(neg, -full)  # polarity -1 negates the whole trace


def test_split_dac_is_sign_split():
    a, b = ex.split_dac(np.array([-2.0, -1.0, 0.0, 1.0, 3.0]))
    assert np.array_equal(a, np.array([0, 0, 0, 1, 3.0]))
    assert np.array_equal(b, np.array([2.0, 1, 0, 0, 0]))


def test_cv2_zero_for_regular_and_high_for_alternating():
    assert np.allclose(ex.cv2_series(np.arange(0, 1.0, 0.1)), 0.0)
    times = np.cumsum([0.0] + [0.01, 0.09] * 10)
    assert np.median(ex.cv2_series(times)) > 0.5
