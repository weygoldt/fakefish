"""Plot every volley in the exported stimulus library as voltage over time.

Parses the generated firmware (``firmware/eel_core/eel_stimuli.cpp``), keeps the
two volley-family kinds (real volley / synth volley), reconstructs each one as the
full-rate signed trace the electrode emits (the additive-mixer output scaled to the
firmware's ``VOLLEY_AMPLITUDE``), and draws them in one gallery — the same "what goes
into the water" the firmware plays in VOLLEY mode. Every volley starts already-strong;
synthetic volleys then wind their amplitude gently down toward 80 % of that over the
discharge.

    fakefish-gallery-volley [--firmware ...] [--out ...]
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
    MARKER_N_PULSES,
    VOLLEY_AMPLITUDE,
    draw_leadin,
)

log = get_logger(__name__)
app = typer.Typer(add_completion=False)

# volley-family kinds -> (display label, colour)
KIND_STYLE = {
    ex.STIM_REAL_VOLLEY: ("real", CATEGORICAL[0]),
    ex.STIM_SYNTH_VOLLEY: ("synth", CATEGORICAL[1]),
}
# draw order: real fragments first, then the synthetic start-strong volleys
KIND_ORDER = {ex.STIM_REAL_VOLLEY: 0, ex.STIM_SYNTH_VOLLEY: 1}


@app.command()
def main(
    firmware: Path = typer.Option(
        _res.DEFAULT_FIRMWARE, "--firmware", "-f"
    ),
    out: Path = typer.Option(_res.FIGS_DIR / "volley_gallery.png", "--out", "-o"),
    marker_show_s: float = typer.Option(
        0.15, "--marker-show-s",
        help="pre-onset seconds to draw (clamped to at least the full marker burst)",
    ),
    verbose: int = typer.Option(1, "--verbose", "-v", count=True),
) -> None:
    """Draw every volley-family item as an output-level-over-time gallery, each preceded by
    its alternating-polarity pulse marker + fixed per-item gap (so you see where the burst
    lands relative to the stimulus onset at t=0). Levels are absolute device output: the
    volley plays at VOLLEY_AMPLITUDE and the marker at its own (lower) level."""
    configure_logging(verbose)
    parsed = ex.parse_firmware(firmware)
    gaps = parsed["lead_gap_samp"]
    header = firmware.with_suffix(".h")
    hz = ex.PLAYBACK_RATE_HZ
    for line in header.read_text().splitlines():
        if "STIM_SAMPLE_RATE_HZ" in line and "#define" in line:
            hz = int(line.split()[2])

    eod = parsed["EOD_HV"]
    # keep the original STIM_ITEMS index so each item can look up its lead-in gap
    vol = [
        {**it, "_idx": i}
        for i, it in enumerate(parsed["items"])
        if it["kind"] in KIND_STYLE
    ]
    # order by kind then by played duration
    for it in vol:
        it["_dur"] = int(np.cumsum(it["ipi_samp"].astype(np.int64))[-1]) / hz
        it["_peak"] = hz / int(it["ipi_samp"][1:].min())
    vol.sort(key=lambda it: (KIND_ORDER[it["kind"]], it["_dur"]))
    log.info("%d volley-family items", len(vol))

    ncol = 4
    nrow = int(np.ceil(len(vol) / ncol))
    fig, axes = full_page(
        height_cm=2.7 * nrow + 1.4, nrows=nrow, ncols=ncol, squeeze=False, sharey=True
    )
    for i, it in enumerate(vol):
        ax = axes[i // ncol][i % ncol]
        label, colour = KIND_STYLE[it["kind"]]
        trace = ex.reconstruct_item(eod, it["ipi_samp"], it["rel_amp"]) * VOLLEY_AMPLITUDE
        t = np.arange(trace.size) / hz  # stimulus onset at t = 0
        # the alternating pulse marker + this item's fixed gap, to the left of onset
        gap_ms = int(gaps[it["_idx"]]) / hz * 1000
        x_left = draw_leadin(ax, int(gaps[it["_idx"]]), hz, show_s=marker_show_s)
        ax.plot(t, trace, color=colour, lw=0.4, zorder=3)
        ax.set_title(
            f"{label} · {it['_peak']:.0f} Hz · {it['_dur']:.2f} s · gap {gap_ms:.0f} ms",
            fontsize=6,
        )
        ax.set_xlim(x_left, max(t[-1], 1e-3))
        ax.set_ylim(-1.0, 1.05)
        ax.tick_params(labelsize=5)
        ax.axhline(0, color="0.7", lw=0.4, zorder=0)
    for j in range(len(vol), nrow * ncol):
        axes[j // ncol][j % ncol].axis("off")
    fig.suptitle(
        f"Every volley — {MARKER_N_PULSES}-pulse alternating marker → gap → onset (t=0) "
        f"→ volley · {len(vol)} items"
    )
    fig.supxlabel("time relative to stimulus onset (s)", fontsize=8)
    fig.supylabel("output level (× full scale)", fontsize=8)
    saved = save_figure(fig, out)
    log.info("wrote %s", saved)


if __name__ == "__main__":
    app()
