"""Shared constants + drawing for the fakefish stimulus galleries.

The levels and timings here are IMPORTED from the generated ``fakefish._constants``
(source: ``shared/stim_constants.json``) — the same file that generates the firmware's
``stim_levels.h`` and feeds ``build_sd_card.py``. They used to be re-declared literals in
this module, which meant the galleries could silently drift away from the card they claim
to depict; they are now single-sourced and cannot.

The galleries draw **absolute device output** on a shared y-axis: the electrode drive
as a fraction of the device's full scale (±1). So the lead-in marker is drawn at its
own level (``MARKER_AMPLITUDE``), a localization item at ``LOC_AMPLITUDE``, and a volley
at ``VOLLEY_AMPLITUDE`` — the same amplitude relationships the hardware actually emits,
not each item normalised to its own peak. Each playback is
``[marker burst] -> [per-item gap] -> [item]``; ``draw_leadin`` draws the marker + gap to
the LEFT of stimulus onset, which the caller places at ``t = 0``.

**The marker is a pulse burst, not a tone.** It is ``MARKER_N_PULSES`` (even, so the burst
is charge-balanced) EOD pulses at ``MARKER_RATE_HZ`` with **alternating polarity**, baked
into every B/C/D WAV by ``build_sd_card.render_pulse_marker``. The alternation is the
detection cue — no eel alternates and a localization train is single-polarity — and it is
what survives the firmware's per-press random polarity flip (which negates the whole WAV,
so the absolute sign is meaningless). Drawing it as a band would hide exactly the property
that identifies it, so ``draw_leadin`` draws the individual alternating pulses.
"""

from __future__ import annotations

import numpy as np

# ---- Single-sourced from shared/stim_constants.json (do not re-declare here) ---------
from fakefish._constants import (
    CAL_AMPLITUDE,  # program A's calibration train
    CAL_IPI_SAMPLES,  # its fixed interval
    CAL_RATE_HZ,  # its steady (single-polarity) rate
    CAL_S,  # its duration
    D_INTERPHASE_GAP_S,  # silence between program D's loc and volley
    D_LOC_PLAYBACK_S,  # program D's short localization lead
    LOC_AMPLITUDE,  # localization items
    MARKER_AMPLITUDE,  # lead-in level, x full scale
    MARKER_IPI_SAMPLES,  # spacing between marker pulses
    MARKER_N_PULSES,  # EVEN => the burst is charge-balanced
    MARKER_RATE_HZ,  # the burst's fixed rate
    MARKER_SPAN_S,  # first onset -> last onset
    VOLLEY_AMPLITUDE,  # volley items, and program D's strike
)

#: This module is the galleries' single import point for both the level constants and the
#: drawing helpers, so the names above are deliberately RE-EXPORTED (the plot_* modules
#: import them from here, not from _constants). Declared explicitly so the re-export is
#: intentional rather than an accident a linter would strip.
__all__ = [
    "CAL_AMPLITUDE",
    "CAL_IPI_SAMPLES",
    "CAL_RATE_HZ",
    "CAL_S",
    "D_INTERPHASE_GAP_S",
    "D_LOC_PLAYBACK_S",
    "LOC_AMPLITUDE",
    "MARKER_AMPLITUDE",
    "MARKER_COLOR",
    "MARKER_IPI_SAMPLES",
    "MARKER_N_PULSES",
    "MARKER_RATE_HZ",
    "MARKER_SPAN_S",
    "MARKER_TAG",
    "MARKER_TAG_LONG",
    "VOLLEY_AMPLITUDE",
    "draw_leadin",
    "marker_pulses",
]

MARKER_COLOR = "#c8a21e"  # a warm ochre, distinct from the CATEGORICAL item colours
#: In-panel tag for the burst — short enough to sit left of stimulus onset in a 4-column
#: gallery panel. Both forms are built from the constants, so neither can disagree with
#: what is drawn.
MARKER_TAG = f"{MARKER_N_PULSES}× alt"
MARKER_TAG_LONG = f"{MARKER_N_PULSES} alternating pulses @ {MARKER_RATE_HZ:g} Hz"


def draw_leadin(
    ax,
    gap_samp: int,
    hz: int,
    *,
    color: str = MARKER_COLOR,
    show_s: float = 0.15,
    label: bool = True,
) -> float:
    """Draw the alternating-polarity pulse marker + the per-item silent gap left of
    stimulus onset (the caller draws the item at ``t >= 0``). Returns the left x-limit.

    The burst is drawn as ``MARKER_N_PULSES`` discrete stems, ``MARKER_IPI_SAMPLES`` apart,
    flipping between ``+MARKER_AMPLITUDE`` and ``-MARKER_AMPLITUDE`` — the alternation is
    the whole point of the marker, so it must be visible rather than smeared into a band.
    A stem stands for one EOD pulse (a few ms wide — invisible at gallery scale); the last
    stem is drawn at the start of the gap, i.e. within one EOD of its true onset. Use
    ``marker_pulses`` where the real waveform matters.

    ``show_s`` is the requested pre-onset window. The burst is only ``MARKER_SPAN_S`` long
    and is ALWAYS drawn in full, so ``show_s`` can widen that window but never truncate
    the burst (the old sine tone ran for 1 s and had to be cropped; this one does not).
    """
    gap_s = gap_samp / hz
    ipi_s = MARKER_IPI_SAMPLES / hz
    lead_s = max(float(show_s), MARKER_SPAN_S + 0.5 * ipi_s)  # never crop the burst
    x_left = -(gap_s + lead_s)
    first_onset = -(gap_s + MARKER_SPAN_S)
    for k in range(MARKER_N_PULSES):
        x = first_onset + k * ipi_s
        y = MARKER_AMPLITUDE if k % 2 == 0 else -MARKER_AMPLITUDE
        ax.plot([x, x], [0.0, y], color=color, lw=0.9, solid_capstyle="butt", zorder=3)
        ax.plot([x], [y], marker="o", ms=1.3, mfc=color, mec=color, zorder=3)
    ax.axvspan(-gap_s, 0.0, color="0.85", alpha=0.8, lw=0, zorder=0)  # the gap
    ax.axvline(0.0, color="0.35", lw=0.7, ls="--", zorder=4)          # stimulus onset
    if label:
        # Well clear of the stem tips (same full-scale units): in a wide window the burst
        # is a narrow cluster, so a tag at the stems' own level would sit on top of it.
        ax.text(x_left, MARKER_AMPLITUDE + 0.2, MARKER_TAG, fontsize=4.5, ha="left",
                va="bottom", color=color)
    return x_left


def marker_pulses(eod_i16, amplitude: float = MARKER_AMPLITUDE) -> np.ndarray:
    """The REAL lead-in burst as a signed trace in full-scale units (peak == amplitude).

    Mirrors ``build_sd_card.render_pulse_marker`` — the same ``MARKER_N_PULSES`` copies of
    the EOD, ``MARKER_IPI_SAMPLES`` apart, with every other one negated — for panels that
    want the true waveform instead of ``draw_leadin``'s stems. The trace ENDS where the
    per-item gap begins, so a caller places it at ``t = -gap_s - trace.size / hz``.
    """
    eod = np.asarray(eod_i16, dtype=np.float64) / 32767.0 * float(amplitude)
    out = np.zeros(MARKER_IPI_SAMPLES * (MARKER_N_PULSES - 1) + eod.size, dtype=np.float64)
    for k in range(MARKER_N_PULSES):
        s = k * MARKER_IPI_SAMPLES
        out[s : s + eod.size] += eod if k % 2 == 0 else -eod
    return out
