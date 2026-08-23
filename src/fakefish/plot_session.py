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

import matplotlib.pyplot as plt
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

#: Pulse kinds in the raster, bottom row first. The localization train is the dense,
#: continuous thing a session is *made of*, so it sits at the base; the trial pulses
#: that interrupt it stack above.
_PULSE_ROWS = (
    ("LOC", "localization", CATEGORICAL[0]),
    ("MARKER", "marker", CATEGORICAL[4]),
    ("VOLLEY", "volley", CATEGORICAL[1]),
)

#: The one recessive fill for "localization was enabled". Deliberately ONE colour.
#: Colouring the band by how its run *ended* painted a property of the right-hand edge
#: across the whole width, which is what made this panel unreadable. How a run ended is
#: an EVENT, so it is marked at the boundary instead — and only the rare, meaningful
#: case is marked at all (see _panel_state).
_BAND = "#c9dbe6"


def _label_at(ax, x, y, text, colour, *, va="center", ha="left"):
    """Direct label, next to the thing it names.

    Legends are friction: the eye leaves the data, finds the key, carries a colour back
    and re-finds the trace, losing the comparison each time. With at most three traces
    per panel there is always room to say it in place.
    """
    ax.annotate(
        text,
        xy=(x, y),
        xytext=(2, 0),
        textcoords="offset points",
        color=colour,
        fontsize=7,
        va=va,
        ha=ha,
        annotation_clip=False,
    )


def _panel_state(ax, log_file, runs, trial_list) -> None:
    """What the operator did: localization on/off, and each trial's outcome.

    Three labelled rows, so nothing has to be decoded from a colour. Trials get one row
    per outcome rather than a letter per trial — twelve letters across 260 s collide,
    and a row you can scan carries the same information without any of them.
    """
    y_loc, y_sham, y_volley = 0.0, 1.0, 2.0

    for run in runs:
        ax.add_patch(
            plt.Rectangle(
                (run.start_s, y_loc - 0.3),
                run.duration_s,
                0.6,
                facecolor=_BAND,
                edgecolor="none",
                zorder=1,
            )
        )

    # THE ONLY RUN-END WORTH MARKING. A trial preempting the train is the protocol doing
    # its job and happens on every throw; the operator releasing the throttle is the
    # thing you actually want to find, and on a healthy log there are very few.
    gate_ends = [r.end_s for r in runs if r.ended_by == ss.ENDED_BY_GATE]
    for i, x in enumerate(gate_ends):
        ax.plot(
            [x], [y_loc], marker="|", ms=11, mew=1.8,
            color=CATEGORICAL[1], markeredgecolor=CATEGORICAL[1], zorder=3,
        )
        if i == 0:
            # Right-aligned, above the band: to the right of the last release lies the
            # link-loss rule and the figure edge.
            _label_at(
                ax, x, y_loc + 0.45, "throttle released ", CATEGORICAL[1],
                ha="right", va="bottom",
            )

    for tr in trial_list:
        y = y_volley if tr.resolved == "V" else y_sham
        # Indigo, not the palette's light green: a sham mark is a single 8 pt tick and
        # the pale end of the cycle disappears against white at that size.
        colour = CATEGORICAL[1] if tr.resolved == "V" else CATEGORICAL[5]
        ax.plot(
            [tr.start_s], [y], marker="|", ms=8, mew=1.5,
            color=colour, markeredgecolor=colour, zorder=3,
        )

    for rec in log_file.events("LINK"):
        if rec.tick is not None and rec.val == 0:
            ax.axvline(rec.tick / log_file.sample_rate_hz, color="0.55", ls=":", lw=0.8)

    ax.set_yticks([y_loc, y_sham, y_volley])
    ax.set_yticklabels(["localization on", "sham trial", "volley trial"])
    ax.set_ylim(-0.95, 2.5)
    ax.tick_params(axis="y", length=0)


def _panel_raster(ax, log_file) -> None:
    """Every pulse that entered the water, by kind. The figure's subject.

    Only kinds that actually occur get a row. The RC device stopped emitting markers on
    2026-08-22, so a current log has none — and an empty labelled row is non-data ink that
    also invites the reader to wonder what went missing. Older logs still have the row,
    because they still have the pulses.
    """
    rate = float(log_file.sample_rate_hz)
    present = [
        (label, colour, np.array(
            [r.tick for r in log_file.pulses(kind) if r.tick is not None], dtype=float
        ) / rate)
        for kind, label, colour in _PULSE_ROWS
        if log_file.pulses(kind)
    ]
    for row, (_, colour, t) in enumerate(present):
        ax.vlines(t, row + 0.12, row + 0.88, color=colour, linewidth=0.35, alpha=0.9)
    ax.set_ylim(-0.1, max(len(present), 1))
    ax.set_yticks([i + 0.5 for i in range(len(present))])
    ax.set_yticklabels([label for label, _, _ in present])
    ax.tick_params(axis="y", length=0)


def _panel_rhythm(ax, log_file, track) -> None:
    """Realised rate against what was asked for.

    They are not meant to coincide, and that is the point: the fitted rhythm is
    heavy-tailed, so the commanded MEDIAN sits above most of the realised scatter. Dots
    hugging the line means a session run at randomness 0 — a metronome, not a fish.
    """
    t, ipi = ss.loc_intervals(log_file)
    if ipi.size:
        ax.plot(
            t, 1.0 / ipi, ".", ms=1.8, alpha=0.45, zorder=2,
            color=CATEGORICAL[0], markeredgecolor="none", markerfacecolor=CATEGORICAL[0],
        )
        _label_at(ax, t[-1], float(np.median(1.0 / ipi[-50:])), " realised", CATEGORICAL[0])
    finite = np.isfinite(track.tick_hz)
    if np.any(finite):
        ax.step(
            track.t_s[finite], track.tick_hz[finite], where="post",
            color=CATEGORICAL[1], lw=1.1, zorder=3,
        )
        _label_at(
            ax, track.t_s[finite][-1], track.tick_hz[finite][-1],
            " commanded", CATEGORICAL[1],
        )
    ax.set_yscale("log")
    ax.set_ylabel("pulse rate (Hz)")


def _panel_controls(ax, track) -> None:
    """The two knobs, on their shared 0-1 scale."""
    for values, colour, name in (
        (track.randomness, CATEGORICAL[4], " randomness"),
        (track.master_amp, CATEGORICAL[2], " volley amplitude"),
    ):
        finite = np.isfinite(values)
        if not np.any(finite):
            continue
        ax.step(track.t_s[finite], values[finite], where="post", color=colour, lw=1.1)
        _label_at(ax, track.t_s[finite][-1], values[finite][-1], name, colour)
    ax.set_ylim(-0.06, 1.10)
    ax.set_yticks([0.0, 0.5, 1.0])
    ax.set_ylabel("knob (0-1)")


def _panel_throttle(ax, track) -> None:
    """The raw measurement, in the unit the 2026-08-22 fault lived in.

    Its own panel rather than a second y-axis on the knobs: two scales in one frame is
    friction, and this is the single trace that answers "could the fish be switched
    off?". At rest it must sit on the zero line.
    """
    above = track.throttle_above_zero_us
    finite = np.isfinite(above)
    ax.step(track.t_s[finite], above[finite], where="post", color="0.30", lw=0.9)
    ax.axhline(0.0, color=CATEGORICAL[1], lw=0.8, ls="--")
    _label_at(ax, track.t_s[finite][-1], 0.0, " at rest", CATEGORICAL[1])
    ax.set_ylabel("throttle (us\nabove zero)")


def build_timeline(log_file, *, title: Optional[str] = None):
    """Build the session timeline. Returns the figure.

    Panels run in the order the argument does — what the operator did, what came out of
    the electrodes, how it was paced, what the knobs said, what the receiver actually
    measured — and share the time axis, so a trial lines up with the throttle position
    that preceded it and the rhythm it interrupted.
    """
    runs = ss.loc_runs(log_file)
    trial_list = ss.trials(log_file)
    track = ss.control_track(log_file)
    raw = track.has_raw_decode

    # The raster is the subject and gets the height; the state strip is three rows of
    # marks and needs almost none. A pre-v3 log has no throttle panel to draw at all.
    ratios = [0.75, 1.5, 1.35, 0.9] + ([0.85] if raw else [])
    fig, axes = full_page(
        height_cm=15.5 if raw else 13.0,
        nrows=len(ratios),
        sharex=True,
        gridspec_kw={"height_ratios": ratios},
    )

    _panel_state(axes[0], log_file, runs, trial_list)
    _panel_raster(axes[1], log_file)
    _panel_rhythm(axes[2], log_file, track)
    _panel_controls(axes[3], track)
    if raw:
        _panel_throttle(axes[4], track)

    axes[-1].set_xlabel("time since power-on (s)")

    # Non-data ink, dropped as the guidelines ask. Under sharex every panel would draw
    # its own bottom spine and tick marks, stacking four rules through the middle of the
    # figure that carry nothing; only the bottom axis needs one. The categorical strips
    # lose their y-spine too — their rows are named, so the line measures nothing.
    span = max([r.end_s for r in runs] + list(track.t_s[-1:]) + [1.0])
    for ax in axes:
        ax.set_xlim(-0.01 * span, span * 1.10)
        ax.grid(False)
    for ax in axes[:-1]:
        ax.spines["bottom"].set_visible(False)
        ax.tick_params(axis="x", length=0)
    for ax in axes[:2]:
        ax.spines["left"].set_visible(False)
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

    # ALIGNMENT WARNING — and the condition is NO ANCHORS, not low randomness.
    #
    # A volley anchors a recording extremely well: 46-364 pulses at 300-400 Hz whose exact IPI
    # sequence is recoverable from the library via the logged item index. A sham emits nothing,
    # but its time interpolates between volley anchors — at ~1 ms over a typical 10-26 s gap,
    # which is why removing the marker cost so little. What cannot be placed is a session with
    # no volley at all AND a localization train too regular to fingerprint; an earlier version
    # of this warning fired on low randomness alone and would have cried wolf on every session
    # that simply ran the knob down.
    anchorless = s.n_volley == 0 and (
        s.n_loc == 0 or (np.isfinite(s.randomness_max) and s.randomness_max < 0.05)
    )
    if anchorless:
        log.warning(
            "  no volley pulses and no irregular localization — this session may have nothing "
            "a recording can be aligned against (the RC marker was removed 2026-08-22)"
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
