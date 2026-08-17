"""Plot every localization sequence in the exported library as voltage over time.

Parses the generated firmware (``firmware/eel_core/eel_stimuli.cpp``), keeps the
localization items (real single-fish windows + the synthetic 1-10 Hz set), and draws
each pulse train as the full-rate signed trace the electrode emits. A fixed leading
window is shown so the different average rates are directly comparable (denser panel =
faster localization); the title carries the average rate, pulse count, and full length.

    fakefish-gallery-localization [--firmware ...] [--out ...] [--window-s 12]
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
    MARKER_N_PULSES,
    draw_leadin,
)

log = get_logger(__name__)
app = typer.Typer(add_completion=False)


@app.command()
def main(
    firmware: Path = typer.Option(
        _res.DEFAULT_FIRMWARE, "--firmware", "-f"
    ),
    out: Path = typer.Option(_res.FIGS_DIR / "localization_gallery.png", "--out", "-o"),
    window_s: float = typer.Option(12.0, "--window-s", help="leading seconds to show"),
    marker_show_s: float = typer.Option(
        0.15, "--marker-show-s",
        help="pre-onset seconds to draw (clamped to at least the full marker burst)",
    ),
    verbose: int = typer.Option(1, "--verbose", "-v", count=True),
) -> None:
    """Draw every localization item as an output-level-over-time gallery, each preceded by
    its alternating-polarity pulse marker + fixed per-item gap (so you see where the burst
    lands relative to the stimulus onset at t=0). Levels are absolute device output: the
    localization plays at LOC_AMPLITUDE — below a volley's level, above the marker's."""
    configure_logging(verbose)
    parsed = ex.parse_firmware(firmware)
    gaps = parsed["lead_gap_samp"]
    header = firmware.with_suffix(".h")
    hz = ex.PLAYBACK_RATE_HZ
    for line in header.read_text().splitlines():
        if "STIM_SAMPLE_RATE_HZ" in line and "#define" in line:
            hz = int(line.split()[2])

    eod = parsed["EOD_HV"]
    loc = [
        {**it, "_idx": i}
        for i, it in enumerate(parsed["items"])
        if it["kind"] == ex.STIM_LOCALIZATION
    ]
    # a synthetic train is labelled with its target rate; real windows are lower-n and
    # come from the scan. We can't read labels from the firmware, so infer real vs synth
    # from whether the whole train is ~exactly 60 s (the synthetic duration).
    for it in loc:
        onsets = np.cumsum(it["ipi_samp"].astype(np.int64))
        it["_dur"] = int(onsets[-1]) / hz
        it["_rate"] = it["n"] / it["_dur"]
        it["_synth"] = it["_dur"] > 55.0  # synthetic trains are 60 s
    loc.sort(key=lambda it: (it["_synth"], it["_rate"]))
    log.info("%d localization items", len(loc))

    ncol = 2
    nrow = int(np.ceil(len(loc) / ncol))
    fig, axes = full_page(
        height_cm=2.9 * nrow + 1.2, nrows=nrow, ncols=ncol, squeeze=False, sharey=True
    )
    for i, it in enumerate(loc):
        ax = axes[i // ncol][i % ncol]
        colour = CATEGORICAL[2] if it["_synth"] else CATEGORICAL[0]
        kind = "synth" if it["_synth"] else "real"
        trace = ex.reconstruct_item(eod, it["ipi_samp"], it["rel_amp"]) * LOC_AMPLITUDE
        t = np.arange(trace.size) / hz  # stimulus onset at t = 0
        w = t <= window_s
        gap_ms = int(gaps[it["_idx"]]) / hz * 1000
        x_left = draw_leadin(ax, int(gaps[it["_idx"]]), hz, show_s=marker_show_s)
        ax.plot(t[w], trace[w], color=colour, lw=0.5, zorder=3)
        ax.set_title(
            f"{kind} · {it['_rate']:.1f} Hz avg · {it['n']} pulses · {it['_dur']:.0f} s "
            f"· gap {gap_ms:.0f} ms",
            fontsize=6.5,
        )
        ax.set_xlim(x_left, window_s)
        ax.set_ylim(-1.0, 1.05)
        ax.tick_params(labelsize=5)
        ax.axhline(0, color="0.7", lw=0.4, zorder=0)
    for j in range(len(loc), nrow * ncol):
        axes[j // ncol][j % ncol].axis("off")
    fig.suptitle(
        f"Every localization — {MARKER_N_PULSES}-pulse alternating marker → gap → onset · "
        f"first {window_s:.0f} s · {len(loc)} items"
    )
    fig.supxlabel("time relative to stimulus onset (s)", fontsize=8)
    fig.supylabel("output level (× full scale)", fontsize=8)
    saved = save_figure(fig, out)
    log.info("wrote %s", saved)


if __name__ == "__main__":
    app()
