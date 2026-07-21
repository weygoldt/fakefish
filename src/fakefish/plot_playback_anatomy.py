"""Show the anatomy of one fakefish playback: the 10 kHz sine lead-in marker, the fixed
per-item gap, and the stimulus onset — for a representative volley and a representative
localization item. Answers "where does the 10 kHz tone land relative to stimulus onset?".

Left column: the FULL session (full 1 s tone band -> gap -> item), onset at t = 0.
Right column: a real-waveform SEAM zoom — the actual 10 kHz sine cycles ending (with the
raised-cosine offset ramp), the silent gap, and the first stimulus pulses.

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
    MARKER_LEADIN_S,
    VOLLEY_AMPLITUDE,
    draw_leadin,
    marker_cycles,
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
    seam_tone_ms: float = typer.Option(3.0, "--seam-tone-ms", help="ms of real tone in the tail zoom"),
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

    fig, axes = full_page(height_cm=9.5, nrows=2, ncols=2)
    for r, (it, amp, colour, name, rate) in enumerate(rows):
        gap_samp = int(gaps[it["_idx"]])
        gap_s = gap_samp / hz
        trace = ex.reconstruct_item(eod, it["ipi_samp"], it["rel_amp"]) * amp
        t = np.arange(trace.size) / hz  # onset at t = 0

        # --- left: full session (full 1 s tone band -> gap -> item), onset at t=0 ---
        axL = axes[r, 0]
        x_left = draw_leadin(axL, gap_samp, hz, show_s=MARKER_LEADIN_S)  # full tone
        show = t <= min(t[-1], 1.6)
        axL.plot(t[show], trace[show], color=colour, lw=0.5, zorder=3)
        axL.set_xlim(x_left, min(t[-1], 1.6))
        axL.set_ylim(-1.0, 1.15)
        axL.set_title(f"{name} · {rate} · gap {gap_s * 1000:.0f} ms", fontsize=8)
        axL.set_ylabel("output level (× full scale)", fontsize=7)
        axL.tick_params(labelsize=6)
        axL.axhline(0, color="0.7", lw=0.4, zorder=0)
        if r == 0:
            axL.annotate("10 kHz lead-in (1 s)", (-MARKER_LEADIN_S / 2 - gap_s, MARKER_AMPLITUDE),
                         fontsize=6, color=MARKER_COLOR, ha="center", va="bottom")
            axL.annotate("onset", (0, 1.12), fontsize=6, color="0.35", ha="center", va="top")
        axL.set_xlabel("time relative to onset (s)", fontsize=7)

        # --- right: micro-zoom on the tone TAIL — the real 10 kHz cycles + clean offset
        #     ramp to 0 (the ~200 ms gap dwarfs this, so it is a separate zoom; the left
        #     panel carries the gap->onset relationship). x in ms before onset. ---
        axR = axes[r, 1]
        n_tone = int(seam_tone_ms / 1000 * hz)
        tone = marker_cycles(n_tone, hz)
        ramp = min(int(0.002 * hz), n_tone)  # 2 ms raised-cosine offset ramp
        env = np.ones(n_tone)
        k = np.arange(ramp)
        env[n_tone - ramp:] = 0.5 * (1 + np.cos(np.pi * (k + 1) / ramp))  # 1 -> 0
        tone = tone * env
        # place the tone end at 0 on this axis (ms before it), so cycles are resolvable
        t_tone_ms = np.linspace(-seam_tone_ms, 0.0, n_tone)
        axR.plot(t_tone_ms, tone, color=MARKER_COLOR, lw=1.0, zorder=3)
        axR.axhline(0, color="0.7", lw=0.4, zorder=0)
        axR.set_xlim(-seam_tone_ms, 0.6)
        # zoom the y-axis to the marker's real (low) amplitude so the cycles + offset ramp
        # are legible — this detail panel is about tone SHAPE, not the level comparison.
        axR.set_ylim(-MARKER_AMPLITUDE * 1.45, MARKER_AMPLITUDE * 1.6)
        axR.set_title(
            f"10 kHz tone tail (real cycles) → {gap_s * 1000:.0f} ms gap → onset",
            fontsize=8,
        )
        axR.tick_params(labelsize=6)
        axR.set_xlabel("time before tone end (ms)", fontsize=7)
        axR.annotate(
            f"tone ends,\nthen {gap_s * 1000:.0f} ms silence\nbefore onset",
            (0, -MARKER_AMPLITUDE * 0.8), fontsize=6, color="0.4", ha="right", va="center",
        )

    fig.suptitle("Anatomy of a fakefish playback — 10 kHz lead-in marker → gap → stimulus onset")
    saved = save_figure(fig, out)
    log.info("wrote %s (volley gap %d samp, loc gap %d samp)", saved,
             int(gaps[volley["_idx"]]), int(gaps[loc["_idx"]]))


if __name__ == "__main__":
    app()
