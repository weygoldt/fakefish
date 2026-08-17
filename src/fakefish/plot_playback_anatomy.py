"""Show the anatomy of one fakefish playback: the alternating-polarity pulse marker, the
fixed per-item gap, and the stimulus onset — for a representative volley and a
representative localization item. Answers "where does the marker land relative to
stimulus onset, and how do I recognise it in a recording?".

Left column: the FULL session (marker burst -> gap -> item), onset at t = 0, with the
marker drawn as stems so its alternation reads at session scale.
Right column: a real-waveform zoom on the lead-in — the actual EOD pulses of the burst,
alternating +/-, then the silent gap, then the first stimulus pulses.

The marker is MARKER_N_PULSES EOD pulses at MARKER_RATE_HZ with ALTERNATING polarity: no
eel alternates and a localization train is single-polarity, so the pattern is the cue. It
also survives the firmware's per-press polarity flip (which negates the whole WAV), so the
absolute sign in these panels is arbitrary — only the alternation is meaningful.

Levels are absolute device output (marker / localization / volley each at their own
firmware amplitude), so the panels show the real height relationships between them.

    fakefish-anatomy [--firmware ...] [--out ...]
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import numpy as np  # noqa: E402
import typer  # noqa: E402

from fakefish.viz.figsave import save_figure  # noqa: E402
from fakefish.viz.loggers import configure_logging, get_logger  # noqa: E402
from fakefish.viz.plotstyle import CATEGORICAL, full_page  # noqa: E402

from fakefish import export_teensy_stimuli as ex  # noqa: E402
from fakefish import _resources as _res  # noqa: E402
from fakefish._gallery_marker import (  # noqa: E402
    LOC_AMPLITUDE,
    MARKER_AMPLITUDE,
    MARKER_COLOR,
    MARKER_N_PULSES,
    MARKER_SPAN_S,
    MARKER_TAG_LONG,
    VOLLEY_AMPLITUDE,
    draw_leadin,
    marker_pulses,
)

log = get_logger(__name__)
app = typer.Typer(add_completion=False)


def _pick(items, key, target):
    """The item whose `key(it)` is closest to `target`."""
    return min(items, key=lambda it: abs(key(it) - target))


@app.command()
def main(
    firmware: Path = typer.Option(
        _res.DEFAULT_FIRMWARE, "--firmware", "-f"
    ),
    out: Path = typer.Option(_res.FIGS_DIR / "playback_anatomy.png", "--out", "-o"),
    zoom_post_ms: float = typer.Option(
        120.0, "--zoom-post-ms", help="ms of stimulus drawn after onset in the lead-in zoom"
    ),
    verbose: int = typer.Option(1, "--verbose", "-v", count=True),
) -> None:
    """Draw the marker -> gap -> onset anatomy for a representative volley + localization."""
    configure_logging(verbose)
    parsed = ex.parse_firmware(firmware)
    gaps = parsed["lead_gap_samp"]
    eod = parsed["EOD_HV"]
    header = firmware.with_suffix(".h")
    hz = ex.PLAYBACK_RATE_HZ
    for line in header.read_text().splitlines():
        if "STIM_SAMPLE_RATE_HZ" in line and "#define" in line:
            hz = int(line.split()[2])

    items = [{**it, "_idx": i} for i, it in enumerate(parsed["items"])]
    for it in items:
        it["_dur"] = int(np.cumsum(it["ipi_samp"].astype(np.int64))[-1]) / hz
    volleys = [it for it in items if it["kind"] in (ex.STIM_REAL_VOLLEY, ex.STIM_SYNTH_VOLLEY)]
    locs = [it for it in items if it["kind"] == ex.STIM_LOCALIZATION]
    # a representative ~0.9 s volley and a representative ~5 Hz localization
    volley = _pick(volleys, lambda it: it["_dur"], 0.9)
    loc = _pick(locs, lambda it: it["n"] / it["_dur"], 5.0)
    rows = [
        (volley, VOLLEY_AMPLITUDE, CATEGORICAL[1], "volley",
         f"{hz / int(volley['ipi_samp'][1:].min()):.0f} Hz peak"),
        (loc, LOC_AMPLITUDE, CATEGORICAL[2], "localization",
         f"{loc['n'] / loc['_dur']:.1f} Hz avg"),
    ]

    marker = marker_pulses(eod)          # the REAL burst, in full-scale units
    zoom_post_s = zoom_post_ms / 1000.0

    fig, axes = full_page(height_cm=9.5, nrows=2, ncols=2)
    for r, (it, amp, colour, name, rate) in enumerate(rows):
        gap_samp = int(gaps[it["_idx"]])
        gap_s = gap_samp / hz
        trace = ex.reconstruct_item(eod, it["ipi_samp"], it["rel_amp"]) * amp
        t = np.arange(trace.size) / hz  # onset at t = 0

        # --- left: full session (marker burst -> gap -> item), onset at t=0 ---
        axL = axes[r, 0]
        # no in-panel tag: this figure spells the marker out in the row-0 annotation below
        x_left = draw_leadin(axL, gap_samp, hz, label=False)
        show = t <= min(t[-1], 1.6)
        axL.plot(t[show], trace[show], color=colour, lw=0.5, zorder=3)
        axL.set_xlim(x_left, min(t[-1], 1.6))
        axL.set_ylim(-1.0, 1.15)
        axL.set_title(f"{name} · {rate} · gap {gap_s * 1000:.0f} ms", fontsize=8)
        axL.set_ylabel("output level (× full scale)", fontsize=7)
        axL.tick_params(labelsize=6)
        axL.axhline(0, color="0.7", lw=0.4, zorder=0)
        if r == 0:
            axL.annotate(
                f"{MARKER_TAG_LONG} ({MARKER_SPAN_S:g} s)",
                (-MARKER_SPAN_S / 2 - gap_s, MARKER_AMPLITUDE + 0.05),
                fontsize=6, color=MARKER_COLOR, ha="center", va="bottom",
            )
            axL.annotate("onset", (0, 1.12), fontsize=6, color="0.35", ha="center", va="top")
        axL.set_xlabel("time relative to onset (s)", fontsize=7)

        # --- right: the lead-in as the REAL waveform — every marker pulse is one EOD, and
        #     consecutive ones are mirror images. This is the panel that shows what to look
        #     for in a recording; the left panel carries the session-scale relationship. ---
        axR = axes[r, 1]
        t_marker = -gap_s - (marker.size - np.arange(marker.size)) / hz  # ends at gap start
        axR.axvspan(-gap_s, 0.0, color="0.85", alpha=0.8, lw=0, zorder=0)  # the gap
        axR.axvline(0.0, color="0.35", lw=0.7, ls="--", zorder=4)          # stimulus onset
        axR.plot(t_marker, marker, color=MARKER_COLOR, lw=0.7, zorder=3)
        post = t <= zoom_post_s
        axR.plot(t[post], trace[post], color=colour, lw=0.5, zorder=3)
        axR.axhline(0, color="0.7", lw=0.4, zorder=0)
        axR.set_xlim(t_marker[0] - 0.03, zoom_post_s)
        axR.set_ylim(-1.0, 1.15)
        axR.set_title(
            f"the real burst → {gap_s * 1000:.0f} ms gap → onset", fontsize=8
        )
        axR.tick_params(labelsize=6)
        axR.set_xlabel("time relative to onset (s)", fontsize=7)
        if r == 0:
            for k in range(MARKER_N_PULSES):
                # pulse k's true onset: the trace starts at pulse 0 and steps by one IPI,
                # which is the span (first onset -> last onset) over the N-1 intervals
                x = t_marker[0] + k * MARKER_SPAN_S / (MARKER_N_PULSES - 1)
                up = k % 2 == 0
                axR.annotate(
                    "+" if up else "−",
                    (x, MARKER_AMPLITUDE + 0.06 if up else -MARKER_AMPLITUDE - 0.06),
                    fontsize=6, color=MARKER_COLOR, ha="center",
                    va="bottom" if up else "top",
                )
            axR.annotate(
                "the SIGN is random per press —\nthe ALTERNATION is the cue",
                (-gap_s - 0.015, 1.12), fontsize=5.5, color="0.4", ha="right", va="top",
            )

    fig.suptitle(
        f"Anatomy of a fakefish playback — {MARKER_N_PULSES}-pulse alternating marker "
        "→ gap → stimulus onset"
    )
    saved = save_figure(fig, out)
    log.info("wrote %s (volley gap %d samp, loc gap %d samp)", saved,
             int(gaps[volley["_idx"]]), int(gaps[loc["_idx"]]))


if __name__ == "__main__":
    app()
