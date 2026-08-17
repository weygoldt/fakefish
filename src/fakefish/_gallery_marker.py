"""Shared constants + drawing for the fakefish stimulus galleries.

The levels and timings here are IMPORTED from the generated ``fakefish._constants``
(source: ``shared/stim_constants.json``) — the same file that generates the firmware's
``stim_levels.h`` and feeds ``build_sd_card.py``. They used to be re-declared literals in
this module, which meant the galleries could silently drift away from the card they claim
to depict; they are now single-sourced and cannot.

The galleries draw **absolute device output** on a shared y-axis: the electrode drive
as a fraction of the device's full scale (±1). So the 10 kHz marker is drawn at its
own level (``MARKER_AMPLITUDE``), a localization item at ``LOC_AMPLITUDE``, and a volley
at ``VOLLEY_AMPLITUDE`` — the same amplitude relationships the hardware actually emits,
not each item normalised to its own peak. Each playback is
``[MARKER_LEADIN_S sine] -> [per-item gap] -> [item]``; ``draw_leadin`` draws the marker
+ gap to the LEFT of stimulus onset, which the caller places at ``t = 0``.
"""

from __future__ import annotations

import numpy as np

# ---- Single-sourced from shared/stim_constants.json (do not re-declare here) ---------
from fakefish._constants import (
    D_INTERPHASE_GAP_S,  # silence between program D's loc and volley
    D_LOC_PLAYBACK_S,  # program D's short localization lead
    LOC_AMPLITUDE,  # localization items
    MARKER_AMPLITUDE,  # lead-in level, x full scale
    MARKER_FREQ_HZ,  # the out-of-band anchor frequency
    MARKER_LEADIN_S,  # the pre-stimulus lead-in tone
    VOLLEY_AMPLITUDE,  # volley items, and program D's strike
)

#: This module is the galleries' single import point for both the level constants and the
#: drawing helpers, so the names above are deliberately RE-EXPORTED (the plot_* modules
#: import them from here, not from _constants). Declared explicitly so the re-export is
#: intentional rather than an accident a linter would strip.
__all__ = [
    "D_INTERPHASE_GAP_S",
    "D_LOC_PLAYBACK_S",
    "LOC_AMPLITUDE",
    "MARKER_AMPLITUDE",
    "MARKER_COLOR",
    "MARKER_FREQ_HZ",
    "MARKER_LEADIN_S",
    "MARKER_REL_AMP",
    "VOLLEY_AMPLITUDE",
    "draw_leadin",
    "marker_cycles",
]

# Kept as an alias so callers that drew the marker band keep working; it IS the marker's
# (relative-to-full-scale) amplitude.
MARKER_REL_AMP = MARKER_AMPLITUDE

MARKER_COLOR = "#c8a21e"  # a warm ochre, distinct from the CATEGORICAL item colours
_MARKER_KHZ = f"{MARKER_FREQ_HZ / 1000:g} kHz"


def draw_leadin(
    ax,
    gap_samp: int,
    hz: int,
    *,
    color: str = MARKER_COLOR,
    show_s: float = 0.15,
    label: bool = True,
) -> float:
    """Draw the sine lead-in marker band (at its true ``MARKER_AMPLITUDE``) + the per-item
    silent gap left of stimulus onset (the caller draws the item at ``t >= 0``). Only the
    last ``show_s`` of the ``MARKER_LEADIN_S`` tone is drawn so short items stay legible;
    pass ``show_s >= MARKER_LEADIN_S`` for the full tone. Returns the left x-limit to use.
    """
    gap_s = gap_samp / hz
    tone_len = min(show_s, MARKER_LEADIN_S)
    x_left = -(gap_s + tone_len)
    tone_end = -gap_s
    # the marker tone (drawn as a filled band — thousands of cycles don't resolve here),
    # at its real amplitude relative to the item traces on the same axis
    ax.fill_between(
        [x_left, tone_end], [-MARKER_AMPLITUDE] * 2, [MARKER_AMPLITUDE] * 2,
        color=color, alpha=0.35, lw=0, zorder=1,
    )
    ax.axvspan(tone_end, 0.0, color="0.85", alpha=0.8, lw=0, zorder=0)  # the gap
    ax.axvline(0.0, color="0.35", lw=0.7, ls="--", zorder=4)            # stimulus onset
    if label:
        full = tone_len >= MARKER_LEADIN_S
        tag = _MARKER_KHZ if full else f"{_MARKER_KHZ} ◄1 s"
        ax.text(x_left, MARKER_AMPLITUDE, tag, fontsize=4.5, ha="left", va="bottom",
                color=color)
    return x_left


def marker_cycles(n_samp: int, hz: int, amplitude: float = MARKER_AMPLITUDE) -> np.ndarray:
    """A short run of the actual marker sine (for a seam zoom that shows the real tone)."""
    t = np.arange(n_samp)
    return amplitude * np.sin(2.0 * np.pi * MARKER_FREQ_HZ * t / hz)
