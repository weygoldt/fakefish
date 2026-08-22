"""Session overview figure — what the device did, when, during one playback.

One `PULSnnnn.CSV` in, one full-page timeline out. Every panel shares the device-time
axis, so a trial in the raster lines up with the throttle position that preceded it and
with the rhythm it interrupted::

    uv run fakefish-session timeline /path/PULS0037.CSV

The panels, top to bottom, are ordered by how far they sit from the water:

1. **Session state** — when localization was running, and what stopped each run. The
   colour is the classification, because ``LOCOFF`` alone is ambiguous: a trial
   preempting the train and the operator releasing the throttle write the same row, and
   conflating them made a 7-release session read as a 27-release one on 2026-08-22.
2. **Pulses** — every emitted pulse, by kind. This is the ground truth against a
   recording; everything else on the page is context for it.
3. **Rhythm** — the realised instantaneous rate against the commanded tick tempo. The two
   are *not* meant to coincide: the fitted rhythm is heavy-tailed, so the commanded median
   sits well above the realised mean rate, and watching the scatter around it is how you
   see the model working rather than a fault.
4. **Controls** — the knobs, and (on a v3 log) the raw throttle travel above its captured
   zero, which is the measurement every other control number is derived from.

Figures go to ``figs/`` (gitignored, regenerable) through the package figure convention
in :mod:`fakefish.viz` — never a hand-passed ``figsize`` or ``dpi``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import typer

from fakefish import session_stats as ss
from fakefish._resources import FIGS_DIR
from fakefish.pulse_log import read
from fakefish.viz.figsave import CATEGORICAL, full_page, save_figure
from fakefish.viz.loggers import configure_logging, get_logger

log = get_logger(__name__)
app = typer.Typer(
    add_completion=False,
    help="Overview figures for the RC device's per-pulse SD session logs.",
)

#: Pulse kinds, in the raster's row order — quietest at the top, loudest at the bottom.
_PULSE_ROWS = (
    ("LOC", "localization", CATEGORICAL[0]),
    ("MARKER", "marker", CATEGORICAL[4]),
    ("VOLLEY", "volley", CATEGORICAL[1]),
)

#: How a localization run ended -> (colour, label).
_RUN_STYLE = {
    ss.ENDED_BY_GATE: (CATEGORICAL[2], "ended by throttle"),
    ss.ENDED_BY_TRIAL: (CATEGORICAL[3], "ended by trial"),
    ss.ENDED_BY_EOF: (CATEGORICAL[5], "ran to end of file"),
}


def _panel_state(ax, log_file, runs, trial_list) -> None:
    """Localization runs as bands, trials as stems above them."""
    seen: set[str] = set()
    for run in runs:
        colour, label = _RUN_STYLE.get(run.ended_by, (CATEGORICAL[5], run.ended_by))
        ax.axvspan(
            run.start_s,
            run.end_s,
            ymin=0.0,
            ymax=0.55,
            color=colour,
            alpha=0.55,
            linewidth=0,
            label=label if label not in seen else None,
        )
        seen.add(label)

    # Trials cluster — several within a few seconds is normal — so the letters are
    # staggered across two rows rather than overprinting each other. Alternating blindly
    # would look tidier and lie about spacing; this only lifts a label when its neighbour
    # is close enough to collide.
    span = max((tr.start_s for tr in trial_list), default=1.0) or 1.0
    prev_s = -np.inf
    high = False
    for tr in trial_list:
        volley = tr.resolved == "V"
        colour = CATEGORICAL[1] if volley else CATEGORICAL[4]
        high = (tr.start_s - prev_s) < 0.02 * span and not high
        prev_s = tr.start_s
        top = 0.98 if not high else 1.10
        ax.vlines(tr.start_s, 0.62, top, color=colour, linewidth=1.2)
        ax.text(
            tr.start_s,
            top + 0.02,
            "V" if volley else "S",
            ha="center",
            va="bottom",
            fontsize=6,
            color=colour,
        )

    # A dropped RC link is the one thing that can stop the session without the operator.
    for rec in log_file.events("LINK"):
        if rec.tick is not None and rec.val == 0:
            ax.axvline(rec.tick / log_file.sample_rate_hz, color="0.35", ls=":", lw=0.9)

    ax.set_ylim(0, 1.30)
    ax.set_yticks([])
    ax.set_ylabel("session", rotation=0, ha="right", va="center")
    if seen:
        ax.legend(loc="lower left", ncols=3, frameon=False, fontsize=6,
                  bbox_to_anchor=(0.0, -0.04))


def _panel_raster(ax, log_file) -> None:
    """One tick per emitted pulse, one row per kind."""
    rate = float(log_file.sample_rate_hz)
    for row, (kind, label, colour) in enumerate(_PULSE_ROWS):
        ticks = [r.tick for r in log_file.pulses(kind) if r.tick is not None]
        if not ticks:
            continue
        t = np.array(ticks, dtype=float) / rate
        ax.vlines(t, row + 0.12, row + 0.88, color=colour, linewidth=0.35, alpha=0.85)
    ax.set_ylim(-0.15, len(_PULSE_ROWS) - 0.05)
    ax.set_yticks([i + 0.5 for i in range(len(_PULSE_ROWS))])
    ax.set_yticklabels([label for _, label, _ in _PULSE_ROWS], fontsize=7)
    ax.set_ylabel("pulses", rotation=0, ha="right", va="center")


def _panel_rhythm(ax, log_file, track) -> None:
    """Realised instantaneous rate against the commanded tick tempo."""
    t, ipi = ss.loc_intervals(log_file)
    if ipi.size:
        ax.plot(
            t,
            1.0 / ipi,
            ".",
            ms=1.6,
            color=CATEGORICAL[0],
            alpha=0.55,
            label="realised 1/IPI",
        )
    finite = np.isfinite(track.tick_hz)
    if np.any(finite):
        ax.step(
            track.t_s[finite],
            track.tick_hz[finite],
            where="post",
            color=CATEGORICAL[1],
            lw=1.0,
            label="commanded tick tempo",
        )
    ax.set_yscale("log")
    ax.set_ylabel("rate (Hz)")
    ax.legend(loc="upper left", ncols=2, frameon=False, fontsize=6)


def _panel_controls(ax, track) -> None:
    """The knobs, plus the raw throttle travel when the log carries it."""
    finite = np.isfinite(track.randomness)
    ax.step(
        track.t_s[finite],
        track.randomness[finite],
        where="post",
        color=CATEGORICAL[4],
        lw=1.0,
        label="randomness",
    )
    finite = np.isfinite(track.master_amp)
    ax.step(
        track.t_s[finite],
        track.master_amp[finite],
        where="post",
        color=CATEGORICAL[2],
        lw=1.0,
        label="volley amplitude",
    )
    ax.set_ylim(-0.05, 1.15)
    ax.set_ylabel("knob (0–1)")

    above = track.throttle_above_zero_us
    if above is not None and np.any(np.isfinite(above)):
        # THE MEASUREMENT, not a derived setting. Its own axis in µs because that is the
        # unit the fault lived in: a resting throttle must sit at 0 here, and on the
        # 2026-08-22 log it sat ~200 µs up with no way to see it.
        twin = ax.twinx()
        finite = np.isfinite(above)
        twin.step(
            track.t_s[finite],
            above[finite],
            where="post",
            color="0.35",
            lw=0.8,
            label="throttle above zero",
        )
        twin.axhline(0.0, color="0.35", lw=0.5, ls=":")
        twin.set_ylabel("CH3 above zero (µs)", fontsize=7)
        twin.tick_params(labelsize=7)
        handles = ax.get_legend_handles_labels()[0] + twin.get_legend_handles_labels()[0]
        labels = ax.get_legend_handles_labels()[1] + twin.get_legend_handles_labels()[1]
        ax.legend(handles, labels, loc="upper left", ncols=3, frameon=False, fontsize=6)
    else:
        ax.legend(loc="upper left", ncols=2, frameon=False, fontsize=6)


def build_timeline(log_file, *, title: Optional[str] = None):
    """Build the four-panel session timeline. Returns the figure."""
    runs = ss.loc_runs(log_file)
    trial_list = ss.trials(log_file)
    track = ss.control_track(log_file)

    # The raster and the rhythm carry the detail, so they get the height; the state strip
    # is a band and a row of letters and needs almost none.
    fig, axes = full_page(
        height_cm=15.0,
        nrows=4,
        sharex=True,
        gridspec_kw={"height_ratios": [0.8, 1.25, 1.5, 1.05]},
    )
    _panel_state(axes[0], log_file, runs, trial_list)
    _panel_raster(axes[1], log_file)
    _panel_rhythm(axes[2], log_file, track)
    _panel_controls(axes[3], track)

    axes[-1].set_xlabel("device time (s)")
    for ax in axes:
        ax.margins(x=0.005)
    if title:
        fig.suptitle(title)
    return fig


@app.command()
def stats(
    path: Path = typer.Argument(..., help="A PULSnnnn.CSV pulse log."),
    verbose: int = typer.Option(2, "--verbose", "-v", count=True),
) -> None:
    """Print the session's derived numbers — structure, rhythm and controls.

    Complements ``fakefish-pulse-log info``, which reports what the *file* contains and
    whether it is intact. This reports what the *session* did.
    """
    configure_logging(verbose)
    log_file = read(path)
    s = ss.summarise(log_file)

    log.info("session: %.1f s of device time", s.duration_s)
    log.info(
        "  pulses: %d localization, %d marker, %d volley", s.n_loc, s.n_marker, s.n_volley
    )
    log.info(
        "  trials: %d (%d volley / %d sham), %d blinded",
        s.n_trials,
        s.n_volley_trials,
        s.n_sham_trials,
        s.n_blinded,
    )
    # The split is the point: a bare LOCOFF count conflates the operator with the protocol.
    log.info(
        "  localization runs: %d total = %d ended by the throttle, %d preempted by a trial",
        s.loc_runs_total,
        s.loc_runs_gate,
        s.loc_runs_trial,
    )
    log.info("  commanded tick tempo: %.2f - %.2f Hz", s.tick_hz_min, s.tick_hz_max)
    log.info("  randomness knob: %.3f - %.3f", s.randomness_min, s.randomness_max)
    log.info(
        "  realised rhythm: median IPI %.0f ms, CV2 %.2f",
        s.ipi_median_s * 1e3,
        s.ipi_cv2,
    )

    if s.throttle_reached_zero is None:
        log.info("  raw decode: not recorded (pre-v3 log)")
    else:
        log.info(
            "  session zero: %s us; throttle travel spanned %.0f us",
            s.zero_us,
            s.throttle_span_us if s.throttle_span_us is not None else float("nan"),
        )
        if s.throttle_reached_zero:
            log.info("  throttle reached its zero: YES (it could be turned off)")
        else:
            log.warning(
                "  throttle NEVER reached its zero — localization could not be stopped "
                "from the transmitter. Check the session zero against RC_CAL_THROTTLE_MIN."
            )


@app.command()
def timeline(
    path: Path = typer.Argument(..., help="A PULSnnnn.CSV pulse log."),
    out_dir: Path = typer.Option(
        None, "--out-dir", "-o", help="Where to write (default: figs/sessions/)."
    ),
    fmt: str = typer.Option("png", "--format", "-f", help="png or pdf."),
    verbose: int = typer.Option(2, "--verbose", "-v", count=True),
) -> None:
    """Render the session overview timeline for one pulse log."""
    configure_logging(verbose)
    log_file = read(path)
    summary = ss.summarise(log_file)

    stem = path.stem
    target = (out_dir or (FIGS_DIR / "sessions")) / f"session_{stem}"
    title = (
        f"{stem} · {summary.duration_s:.0f} s · {summary.n_loc} loc, "
        f"{summary.n_trials} trials ({summary.n_volley_trials}V/{summary.n_sham_trials}S)"
    )
    fig = build_timeline(log_file, title=title)
    written = save_figure(fig, target, fmt=fmt)
    log.info("wrote %s", written)


if __name__ == "__main__":  # pragma: no cover
    app()
