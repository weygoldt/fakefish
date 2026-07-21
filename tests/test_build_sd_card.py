"""Tests for the SD-card WAV export (build_sd_card).

Validate against the *committed firmware* reference: the rendered WAVs must reproduce
``export_teensy_stimuli.reconstruct_item`` byte-for-byte at the baked absolute levels, and
the card must have the right one-directory-per-button structure and WAV format.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.io import wavfile

from fakefish import _resources as _res
from fakefish import build_sd_card as bc
from fakefish import export_teensy_stimuli as ex

FW = _res.DEFAULT_FIRMWARE


@pytest.fixture(scope="module")
def parsed():
    return ex.parse_firmware(FW)


def _split(parsed):
    items = parsed["items"]
    vol = [i for i, it in enumerate(items) if it["kind"] in (ex.STIM_REAL_VOLLEY, ex.STIM_SYNTH_VOLLEY)]
    loc = [i for i, it in enumerate(items) if it["kind"] == ex.STIM_LOCALIZATION]
    return vol, loc


def test_marker_zero_start_zero_dc():
    m = bc.render_marker(bc.MARKER_LEADIN_SAMPLES, bc.MARKER_AMPLITUDE)
    assert m.dtype == np.int16
    assert m.size == bc.MARKER_LEADIN_SAMPLES
    assert int(m[0]) == 0  # zero-start (LUT[0] == 0)
    assert int(m.sum()) == 0  # zero net charge (odd-symmetric LUT)
    assert int(np.abs(m).max()) == round(bc.MARKER_AMPLITUDE * 31163)
    cal = bc.render_marker(bc.MARKER_CAL_SAMPLES, bc.MARKER_CAL_AMPLITUDE)
    assert cal.size == 10 * bc.RATE_HZ


def test_volley_parity_vs_reconstruct(parsed):
    """Every volley WAV item body must be byte-identical to reconstruct_item at 0.90."""
    eod = parsed["EOD_HV"]
    vol, _ = _split(parsed)
    for i in vol:
        it = parsed["items"][i]
        a = bc._to_i16(bc.render_item(eod, it["ipi_samp"], it["rel_amp"], bc.VOLLEY_AMPLITUDE))
        b = bc._to_i16(ex.reconstruct_item(eod, it["ipi_samp"], it["rel_amp"],
                                           amplitude=bc.VOLLEY_AMPLITUDE, polarity=1, pad_samp=0))
        assert np.array_equal(a, b), f"volley {i} diverges"


def test_windowed_loc_parity(parsed):
    """Windowed+looped loc must equal reconstruct_item truncated to the window (0 LSB)."""
    eod = parsed["EOD_HV"]
    _, loc = _split(parsed)
    for i in loc:
        it = parsed["items"][i]
        for win in (bc.LOC_PLAYBACK_SAMPLES, bc.D_LOC_PLAYBACK_SAMPLES):
            a = bc.render_item(eod, it["ipi_samp"], it["rel_amp"], bc.LOC_AMPLITUDE,
                               max_samples=win, loop=True)
            assert a.size == win
            full = ex.reconstruct_item(eod, it["ipi_samp"], it["rel_amp"],
                                       amplitude=bc.LOC_AMPLITUDE, polarity=1, pad_samp=0)
            ref = np.concatenate([full, np.zeros(max(0, win - full.size))])[:win]
            assert np.array_equal(bc._to_i16(a), bc._to_i16(ref)), f"loc {i} win {win}"


def test_build_card_structure_and_levels(tmp_path, parsed):
    out = tmp_path / "card"
    vol, loc = _split(parsed)
    bc.build_card(out, FW, bc.CardConfig(d_pairings=6))

    # one directory per button, expected counts (D limited to 6 for speed)
    assert len(list((out / "A").glob("*.wav"))) == 1
    assert len(list((out / "B").glob("*.wav"))) == len(loc)
    assert len(list((out / "C").glob("*.wav"))) == len(vol)
    assert len(list((out / "D").glob("*.wav"))) == 6
    assert len(list((out / "F").glob("*.wav"))) == 1
    assert (out / "manifest.json").exists()

    # every WAV is mono int16 @ 50 kHz
    for wav in out.rglob("*.wav"):
        rate, data = wavfile.read(wav)
        assert rate == bc.RATE_HZ
        assert data.dtype == np.int16
        assert data.ndim == 1

    # a volley WAV carries the baked VOLLEY level (peak == 0.90 * 32767), not peak-normalised
    vi = vol[0]
    rate, data = wavfile.read(out / "C" / f"volley_{vi:02d}.wav")
    start = bc.MARKER_LEADIN_SAMPLES + int(parsed["lead_gap_samp"][vi])
    ref = bc._to_i16(ex.reconstruct_item(parsed["EOD_HV"], parsed["items"][vi]["ipi_samp"],
                                         parsed["items"][vi]["rel_amp"],
                                         amplitude=bc.VOLLEY_AMPLITUDE, polarity=1, pad_samp=0))
    assert np.array_equal(data[start:start + ref.size], ref)
    assert int(np.abs(data).max()) == round(bc.VOLLEY_AMPLITUDE * 32767)


def test_song_zero_mean(tmp_path):
    song = bc.synth_song(amplitude=0.5)
    assert song.dtype == np.int16
    # peak ~= 0.5 full scale (a sample rarely lands exactly on the sine crest -> 16383/16384)
    assert 16383 <= int(np.abs(song).max()) <= 16384
    assert abs(float(song.mean())) < 5.0  # ~zero-mean -> safe under a polarity flip


def test_mv_roundtrip():
    for a in (0.25, 0.45, 0.90):
        assert bc.mv_to_amplitude(bc.amplitude_to_mv(a)) == pytest.approx(a, abs=1e-6)
