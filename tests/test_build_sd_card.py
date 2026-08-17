"""Tests for the SD-card WAV export (build_sd_card).

Two things are checked here.

**Parity with the committed firmware reference.** The rendered stimulus WAVs must reproduce
``export_teensy_stimuli.reconstruct_item`` byte-for-byte at the baked absolute levels, and
the card must have the right one-directory-per-button structure and WAV format.

**The two pulse trains the card synthesises itself** — the lead-in marker and program A's
calibration train. Neither comes from the frozen library, so their properties are only ever
enforced here. They are deliberately tested the way a *detector* would look at a recording:
segment the trace into pulses, then assert on the onsets and on the SIGN of each pulse's
peak. That is the level at which the marker's contract is actually written —

* the marker ALTERNATES polarity (nothing biological does, and the localization train it
  introduces does not), and
* the calibration train does NOT (it is a plain reference signal, not a code),

and asserting it on the rendered samples is what stops the two from converging.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.io import wavfile

from fakefish import _constants as K
from fakefish import _resources as _res
from fakefish import build_sd_card as bc
from fakefish import export_teensy_stimuli as ex

FW = _res.DEFAULT_FIRMWARE

FULL_SCALE = 32767  # the firmware's clamp16 bound; the EOD LUT peaks here


@pytest.fixture(scope="module")
def parsed():
    return ex.parse_firmware(FW)


def _split(parsed):
    items = parsed["items"]
    vol = [i for i, it in enumerate(items) if it["kind"] in (ex.STIM_REAL_VOLLEY, ex.STIM_SYNTH_VOLLEY)]
    loc = [i for i, it in enumerate(items) if it["kind"] == ex.STIM_LOCALIZATION]
    return vol, loc


def _pulses(x: np.ndarray, eod_len: int) -> tuple[np.ndarray, np.ndarray]:
    """Segment a pulse train into ``(onsets, signed peaks)`` the way a detector would.

    Runs of |x| above 5% of the trace peak are grouped, splitting wherever the silence is
    longer than one EOD — so this recovers the pulses without being told where they are.
    The returned peak keeps its SIGN, which is the whole point: the marker's identity is a
    sign pattern, not an amplitude.
    """
    x = np.asarray(x, dtype=np.int64)
    peak = int(np.abs(x).max())
    assert peak > 0, "silent trace — nothing to segment"
    idx = np.flatnonzero(np.abs(x) > 0.05 * peak)
    groups = np.split(idx, np.flatnonzero(np.diff(idx) > eod_len) + 1)
    onsets = np.array([int(g[0]) for g in groups], dtype=np.int64)
    peaks = np.array([int(x[g[np.argmax(np.abs(x[g]))]]) for g in groups], dtype=np.int64)
    return onsets, peaks


# ===== The lead-in marker (prepended to every B/C/D session) ==========================
def test_marker_is_an_even_burst_at_the_fixed_ipi(parsed):
    """MARKER_N_PULSES pulses, exactly MARKER_IPI_SAMPLES apart, spanning MARKER_SPAN."""
    eod = parsed["EOD_HV"]
    m = bc.render_pulse_marker(eod, bc.Levels())
    assert m.dtype == np.int16

    onsets, _ = _pulses(m, eod.size)
    assert onsets.size == bc.MARKER_N_PULSES
    assert np.array_equal(np.diff(onsets), np.full(bc.MARKER_N_PULSES - 1, bc.MARKER_IPI_SAMPLES))
    assert onsets[0] < eod.size  # the burst starts at t=0 (peak is inside the first EOD)

    # the rate is exact in whole samples, and the WAV holds the onset-to-onset span plus
    # the final pulse's tail — the layout the session assembly then prepends.
    assert bc.MARKER_IPI_SAMPLES * bc.MARKER_RATE_HZ == bc.RATE_HZ
    assert int(onsets[-1] - onsets[0]) == K.MARKER_SPAN_SAMPLES
    assert m.size == K.MARKER_SPAN_SAMPLES + eod.size
    assert K.MARKER_SPAN_S == pytest.approx(K.MARKER_SPAN_SAMPLES / bc.RATE_HZ)

    # baked at the marker level, not peak-normalised (+/-1 LSB of rounding)
    assert abs(int(np.abs(m).max()) - round(bc.MARKER_AMPLITUDE * FULL_SCALE)) <= 1


def test_marker_polarity_alternates(parsed):
    """The DETECTION CUE: successive pulses have opposite sign.

    No eel alternates, and a localization train is single-polarity, so this pattern cannot
    be confused with biology or with the stimulus the marker introduces.
    """
    eod = parsed["EOD_HV"]
    _, peaks = _pulses(bc.render_pulse_marker(eod, bc.Levels()), eod.size)
    signs = np.sign(peaks)
    assert np.all(signs != 0)
    assert np.array_equal(signs[1:], -signs[:-1]), f"not alternating: {signs.tolist()}"


def test_marker_alternation_survives_a_global_polarity_flip(parsed):
    """The property the firmware relies on: the per-press flip negates the WHOLE WAV.

    So the absolute sign of any pulse is unpredictable, but the alternation is invariant.
    A detector must key on the PATTERN, never on the sign.
    """
    eod = parsed["EOD_HV"]
    m = bc.render_pulse_marker(eod, bc.Levels())
    on_a, pk_a = _pulses(m, eod.size)
    on_b, pk_b = _pulses(-m.astype(np.int64), eod.size)

    assert np.array_equal(on_b, on_a)  # timing untouched
    assert np.array_equal(np.sign(pk_b), -np.sign(pk_a))  # every sign flipped, and yet:
    assert np.array_equal(np.sign(pk_b)[1:], -np.sign(pk_b)[:-1])  # still alternating


def test_marker_is_exactly_charge_balanced(parsed):
    """Even pulse count => the burst carries no net charge (the electrodes have no cap).

    The cancellation is EXACT, not approximate: the pulses are identical up to sign (one
    IPI is far longer than one EOD, so none overlap and none is truncated), int16 rounding
    is symmetric about zero, and the count is even. This replaces the zero-sum property the
    retired sine LUT provided.
    """
    eod = parsed["EOD_HV"]
    assert bc.MARKER_IPI_SAMPLES > eod.size  # no overlap => pulses really are identical
    assert bc.MARKER_N_PULSES % 2 == 0

    m = bc.render_pulse_marker(eod, bc.Levels())
    assert int(m.astype(np.int64).sum()) == 0

    # ...and it is the EVEN count doing the work, not luck: an odd burst leaves one whole
    # EOD of net charge behind. This is what gen_constants' evenness check protects.
    odd = bc._pulse_train(eod, bc.MARKER_N_PULSES - 1, bc.MARKER_IPI_SAMPLES,
                          bc.MARKER_AMPLITUDE, alternate=True)
    assert int(odd.astype(np.int64).sum()) != 0


# ===== Program A: the calibration train ==============================================
def test_calibration_train_is_single_polarity(parsed):
    """Program A is a plain reference signal — deliberately NOT a code.

    Right length, right pulse count, one polarity throughout. Single-polarity is exactly
    what stops it being read as the alternating lead-in marker.
    """
    eod = parsed["EOD_HV"]
    cal = bc.render_calibration(eod, bc.Levels())
    assert cal.dtype == np.int16
    assert cal.size == bc.CAL_SAMPLES == round(K.CAL_S * bc.RATE_HZ)

    onsets, peaks = _pulses(cal, eod.size)
    assert onsets.size == bc.CAL_SAMPLES // bc.CAL_IPI_SAMPLES
    assert np.array_equal(np.diff(onsets), np.full(onsets.size - 1, bc.CAL_IPI_SAMPLES))
    assert bc.CAL_IPI_SAMPLES * bc.CAL_RATE_HZ == bc.RATE_HZ

    signs = np.sign(peaks)
    assert np.all(signs == signs[0]), "calibration must NOT alternate — that is the marker"
    assert abs(int(np.abs(cal).max()) - round(bc.CAL_AMPLITUDE * FULL_SCALE)) <= 1

    # Consequence of single polarity: unlike the marker it does NOT self-balance. Its net
    # charge is cancelled ACROSS presses by the firmware's random flip, as for localization.
    assert int(cal.astype(np.int64).sum()) != 0


# ===== Parity with the frozen library ================================================
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


def _write_tone_wav(path, rate=48000, channels=2, seconds=0.2):
    t = np.arange(int(seconds * rate))
    tone = (0.9 * 32767 * np.sin(2 * np.pi * 440 * t / rate)).astype(np.int16)
    data = np.stack([tone] * channels, axis=1) if channels > 1 else tone
    wavfile.write(path, rate, data)
    return path


def test_build_card_structure_and_levels(tmp_path, parsed):
    out = tmp_path / "card"
    vol, loc = _split(parsed)
    song = _write_tone_wav(tmp_path / "song_src.wav")  # small song so the test skips data/rickroll.wav
    bc.build_card(out, FW, bc.CardConfig(d_pairings=6, song_wav=song))

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

    # a volley WAV is [marker burst][per-item gap][volley], with the baked VOLLEY level
    # (peak == 0.90 * 32767), not peak-normalised.
    eod = parsed["EOD_HV"]
    marker = bc.render_pulse_marker(eod, bc.CardConfig().levels)
    assert marker.size == K.MARKER_SPAN_SAMPLES + eod.size  # the layout offset, spelled out
    vi = vol[0]
    rate, data = wavfile.read(out / "C" / f"volley_{vi:02d}.wav")
    start = marker.size + int(parsed["lead_gap_samp"][vi])
    ref = bc._to_i16(ex.reconstruct_item(eod, parsed["items"][vi]["ipi_samp"],
                                         parsed["items"][vi]["rel_amp"],
                                         amplitude=bc.VOLLEY_AMPLITUDE, polarity=1, pad_samp=0))
    assert np.array_equal(data[:marker.size], marker)          # marker first, unmodified
    assert not np.any(data[marker.size:start])                 # then the silent lead gap
    assert np.array_equal(data[start:start + ref.size], ref)   # then the stimulus
    assert int(np.abs(data).max()) == round(bc.VOLLEY_AMPLITUDE * FULL_SCALE)

    # program A is a BARE train: no marker in front of it (a calibration signal is not a
    # playback to be identified), and the full authored duration.
    rate, cal = wavfile.read(out / "A" / "calibration.wav")
    assert cal.size == bc.CAL_SAMPLES
    onsets, peaks = _pulses(cal, eod.size)
    assert onsets[0] < eod.size and onsets.size == bc.CAL_SAMPLES // bc.CAL_IPI_SAMPLES
    assert np.all(np.sign(peaks) == np.sign(peaks[0]))


def test_song_zero_mean(tmp_path):
    song = bc.synth_song(amplitude=0.5)
    assert song.dtype == np.int16
    # peak ~= 0.5 full scale (a sample rarely lands exactly on the sine crest -> 16383/16384)
    assert 16383 <= int(np.abs(song).max()) <= 16384
    assert abs(float(song.mean())) < 5.0  # ~zero-mean -> safe under a polarity flip


def test_load_wav_song(tmp_path):
    # stereo int16 @ 48 kHz -> mono int16 @ 50 kHz, peak-normalised, DC-removed
    src = _write_tone_wav(tmp_path / "src.wav", rate=48000, channels=2, seconds=0.3)
    song = bc.load_wav_song(src, amplitude=0.5)
    assert song.dtype == np.int16
    assert song.ndim == 1
    assert abs(song.size - int(0.3 * bc.RATE_HZ)) < 200      # resampled 48k -> 50k
    assert 16383 <= int(np.abs(song).max()) <= 16384         # peak-normalised to ~0.5 FS
    assert abs(float(song.mean())) < 5.0                     # DC removed

    # an 8-bit UNSIGNED WAV (centred at 128) must convert without a DC pedestal
    t = np.arange(int(0.1 * 44100))
    u8 = (128 + 100 * np.sin(2 * np.pi * 220 * t / 44100)).astype(np.uint8)
    p8 = tmp_path / "u8.wav"
    wavfile.write(p8, 44100, u8)
    s8 = bc.load_wav_song(p8, amplitude=0.5)
    assert s8.dtype == np.int16 and abs(float(s8.mean())) < 20.0


def test_mv_roundtrip():
    for a in (0.25, 0.45, 0.90):
        assert bc.mv_to_amplitude(bc.amplitude_to_mv(a)) == pytest.approx(a, abs=1e-6)
