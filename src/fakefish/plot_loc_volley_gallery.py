"""Plot the loc->volley (program D) sequences the fakefish composes at runtime.

Programs B (localization) and C (volley) each play one library item; program **D** is
the sense-then-strike motif the *firmware* assembles from two — there is no "loc+volley"
item in the library, so this gallery reconstructs what a D press actually emits:

    [10 kHz marker] -> [gap] -> [short localization, D_LOC_PLAYBACK_S] -> [interphase gap]
                                                                         -> [volley]

under ONE marker, at absolute device output levels (localization at LOC_AMPLITUDE, the
strike at the higher VOLLEY_AMPLITUDE — the amplitude step is D's whole point). The
firmware draws the loc and the volley at random per press; this gallery shows one
representative pairing per volley (cycling through the localizations so every one appears)
so you can see every volley in its striking context and confirm the sequence anatomy.

    fakefish-gallery-loc-volley [--firmware ...] [--out ...]
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
    D_INTERPHASE_GAP_S,
    D_LOC_PLAYBACK_S,
    LOC_AMPLITUDE,
    VOLLEY_AMPLITUDE,
    draw_leadin,
)

log = get_logger(__name__)
app = typer.Typer(add_completion=False)

VOLLEY_KINDS = (ex.STIM_REAL_VOLLEY, ex.STIM_SYNTH_VOLLEY)
KIND_ORDER = {ex.STIM_REAL_VOLLEY: 0, ex.STIM_SYNTH_VOLLEY: 1}
LOC_COLOR = CATEGORICAL[2]     # the localization lead-in phase
VOLLEY_COLOR = CATEGORICAL[0]  # the strike


@app.command()
def main(
    firmware: Path = typer.Option(
        _res.DEFAULT_FIRMWARE, "--firmware", "-f"
    ),
    out: Path = typer.Option(_res.FIGS_DIR / "loc_volley_gallery.png", "--out", "-o"),
    marker_show_s: float = typer.Option(
        0.15, "--marker-show-s", help="seconds of the 1 s lead-in tone to draw (>=1 = full)"
    ),
    verbose: int = typer.Option(1, "--verbose", "-v", count=True),
) -> None:
    """Draw every volley in loc->volley (program D) mode: one marker, a short localization
    lead, an interphase gap, then the strike — at absolute device output levels."""
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
        onsets = np.cumsum(it["ipi_samp"].astype(np.int64))
        it["_dur"] = int(onsets[-1]) / hz
    vol = [it for it in items if it["kind"] in VOLLEY_KINDS]
    loc = [it for it in items if it["kind"] == ex.STIM_LOCALIZATION]
    if not vol or not loc:
        raise typer.BadParameter("need at least one volley and one localization item")
    for it in vol:
        it["_peak"] = hz / int(it["ipi_samp"][1:].min())
    for it in loc:
        it["_rate"] = it["n"] / it["_dur"]
    # volleys in the same order as the volley gallery; localizations by ascending rate,
    # cycled across the volleys so every localization is represented.
    vol.sort(key=lambda it: (KIND_ORDER[it["kind"]], it["_dur"]))
    loc.sort(key=lambda it: it["_rate"])
    log.info("%d volleys x cycling %d localizations", len(vol), len(loc))

    d_loc_n = int(round(D_LOC_PLAYBACK_S * hz))   # the SHORT localization window (truncates)

    ncol = 4
    nrow = int(np.ceil(len(vol) / ncol))
    fig, axes = full_page(
        height_cm=2.9 * nrow + 1.4, nrows=nrow, ncols=ncol, squeeze=False, sharey=True
    )
    for i, v in enumerate(vol):
        ax = axes[i // ncol][i % ncol]
        lc = loc[i % len(loc)]   # the localization this D press leads with

        # D anchors on ONE marker and uses the FIRST item's (the localization's) gap.
        loc_gap = int(gaps[lc["_idx"]])
        x_left = draw_leadin(ax, loc_gap, hz, show_s=marker_show_s)

        # localization lead: the stored train truncated to D's short window, at LOC level.
        loc_trace = ex.reconstruct_item(eod, lc["ipi_samp"], lc["rel_amp"])[:d_loc_n]
        loc_trace = loc_trace * LOC_AMPLITUDE
        t_loc = np.arange(loc_trace.size) / hz            # localization onset at t = 0
        ax.plot(t_loc, loc_trace, color=LOC_COLOR, lw=0.4, zorder=3)

        loc_end = t_loc[-1] if t_loc.size else 0.0
        strike_start = loc_end + D_INTERPHASE_GAP_S
        ax.axvspan(loc_end, strike_start, color="0.85", alpha=0.8, lw=0, zorder=0)  # gap

        # the strike: the full volley at VOLLEY level, after the interphase gap.
        vol_trace = ex.reconstruct_item(eod, v["ipi_samp"], v["rel_amp"]) * VOLLEY_AMPLITUDE
        t_vol = strike_start + np.arange(vol_trace.size) / hz
        ax.plot(t_vol, vol_trace, color=VOLLEY_COLOR, lw=0.4, zorder=3)

        ax.set_title(
            f"loc {lc['_rate']:.1f} Hz → volley {v['_peak']:.0f} Hz "
            f"· {t_vol[-1]:.1f} s",
            fontsize=6,
        )
        ax.set_xlim(x_left, t_vol[-1])
        ax.set_ylim(-1.0, 1.05)
        ax.tick_params(labelsize=5)
        ax.axhline(0, color="0.7", lw=0.4, zorder=0)
        if i == 0:   # direct phase labels (once), not a legend
            ax.text(loc_end / 2, 0.95, "localize", fontsize=5, color=LOC_COLOR,
                    ha="center", va="top")
            ax.text(strike_start, 0.95, "strike", fontsize=5, color=VOLLEY_COLOR,
                    ha="left", va="top")
    for j in range(len(vol), nrow * ncol):
        axes[j // ncol][j % ncol].axis("off")
    fig.suptitle(
        f"Program D — localize ({D_LOC_PLAYBACK_S:.0f} s) → "
        f"{D_INTERPHASE_GAP_S * 1000:.0f} ms gap → strike, one marker · {len(vol)}"
    )
    fig.supxlabel("time relative to localization onset (s)", fontsize=8)
    fig.supylabel("output level (× full scale)", fontsize=8)
    saved = save_figure(fig, out)
    log.info("wrote %s", saved)


if __name__ == "__main__":
    app()
