"""Reference player: reconstruct any exported stimulus item as a 50 kHz numpy trace.

Parses the *generated firmware* (``firmware/eel_core/eel_stimuli.cpp``, format
v2) — so it validates the actual arrays the Teensy compiles — and reconstructs any
item (real/synthetic volley or localization) as the full-rate
signed trace the device emits, at a chosen amplitude and polarity, using the
shared additive-mixer engine (:func:`export_teensy_stimuli.reconstruct_item`).
Overlapping pulses SUM, exactly as the firmware must. Use it to (a) look at the
stimulus and (b) push it back through the detection pipeline as an end-to-end
sanity check before it touches water.

Examples::

    fakefish-render info --firmware firmware/eel_core/eel_stimuli.cpp
    fakefish-render render --index 0 --amplitude 0.8 --polarity -1 --out /tmp/i0.npy
    fakefish-render render --index 20 --wav /tmp/i20.wav --png /tmp/i20.png
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import typer

from fakefish.viz.loggers import configure_logging, get_logger

from fakefish import export_teensy_stimuli as ex
from fakefish import _resources as _res

log = get_logger(__name__)
app = typer.Typer(
    add_completion=False,
    help="Reconstruct exported Teensy stimulus items as numpy traces.",
)


def _parse_sample_rate(header: Path) -> int:
    for line in header.read_text().splitlines():
        if "STIM_SAMPLE_RATE_HZ" in line and "#define" in line:
            return int(line.split()[2])
    return ex.PLAYBACK_RATE_HZ


def _load(firmware: Path) -> tuple[dict, int]:
    parsed = ex.parse_firmware(firmware)
    header = firmware.with_suffix(".h")
    rate = _parse_sample_rate(header) if header.exists() else ex.PLAYBACK_RATE_HZ
    return parsed, rate


@app.command()
def info(
    firmware: Path = typer.Option(
        _res.DEFAULT_FIRMWARE, "--firmware", "-f"
    ),
    verbose: int = typer.Option(2, "--verbose", "-v", count=True),
) -> None:
    """List the items in the firmware library."""
    configure_logging(verbose)
    parsed, rate = _load(firmware)
    log.info("sample rate: %d Hz", rate)
    log.info(
        "EOD_HV: %d samples (|peak|=%d)",
        parsed["EOD_HV"].size,
        int(np.abs(parsed["EOD_HV"]).max()),
    )
    log.info("%d items:", len(parsed["items"]))
    for i, it in enumerate(parsed["items"]):
        dur = (
            int(np.cumsum(it["ipi_samp"].astype(np.int64))[-1]) / rate
            if it["n"]
            else 0.0
        )
        log.info(
            "  [%2d] %-13s group=%-2d n=%-4d dur=%5.1fs amp=%s",
            i,
            ex.STIM_KIND_NAMES[it["kind"]],
            it["group"],
            it["n"],
            dur,
            "envelope" if it["rel_amp"] is not None else "full",
        )


@app.command()
def render(
    index: int = typer.Option(0, "--index", "-i", help="item index (see `info`)"),
    amplitude: float = typer.Option(1.0, "--amplitude", "-a"),
    polarity: int = typer.Option(1, "--polarity", "-p", help="+1 or -1"),
    firmware: Path = typer.Option(
        _res.DEFAULT_FIRMWARE, "--firmware", "-f"
    ),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="save .npy"),
    wav: Optional[Path] = typer.Option(None, "--wav", help="save 16-bit wav"),
    png: Optional[Path] = typer.Option(None, "--png", help="save a preview figure"),
    verbose: int = typer.Option(1, "--verbose", "-v", count=True),
) -> None:
    """Reconstruct item ``index`` as the full-rate additive-mixed trace and save it."""
    configure_logging(verbose)
    parsed, rate = _load(firmware)
    if index >= len(parsed["items"]):
        raise typer.BadParameter(
            f"index {index} out of range (have {len(parsed['items'])})"
        )
    it = parsed["items"][index]
    trace = ex.reconstruct_item(
        parsed["EOD_HV"],
        it["ipi_samp"],
        it["rel_amp"],
        amplitude=amplitude,
        polarity=polarity,
    )
    log.info(
        "item %d (%s): %d pulses -> %d samples (%.2f s @ %d Hz), amp %.2f pol %+d, peak %.3f",
        index,
        ex.STIM_KIND_NAMES[it["kind"]],
        it["n"],
        trace.size,
        trace.size / rate,
        rate,
        amplitude,
        polarity,
        float(np.max(np.abs(trace))),
    )
    if out is not None:
        np.save(out, trace.astype(np.float32))
        log.info("wrote %s", out)
    if wav is not None:
        from scipy.io import wavfile

        peak = np.max(np.abs(trace)) or 1.0
        wavfile.write(wav, rate, (trace / peak * 32767).astype(np.int16))
        log.info("wrote %s", wav)
    if png is not None:
        _preview(trace, rate, index, ex.STIM_KIND_NAMES[it["kind"]], png)
        log.info("wrote %s", png)


def _preview(trace: np.ndarray, rate: int, index: int, kind: str, png: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from fakefish.viz.plotstyle import CATEGORICAL, full_page

    dac_a, dac_b = ex.split_dac(trace)
    t = np.arange(trace.size) / rate
    fig, axes = full_page(height_cm=8.0, nrows=2, ncols=1, sharex=True)
    axes[0].plot(t, trace, color=CATEGORICAL[0], lw=0.6)
    axes[0].set_ylabel("signed trace")
    axes[0].set_title(f"item {index} ({kind}) reconstructed @ {rate} Hz")
    axes[1].plot(t, dac_a, color=CATEGORICAL[0], lw=0.6, label="ch A (DAC / PWM+RC)")
    axes[1].plot(t, -dac_b, color=CATEGORICAL[1], lw=0.6, label="−ch B")
    axes[1].set_ylabel("channel")
    axes[1].set_xlabel("time (s)")
    axes[1].legend(loc="upper right")
    fig.savefig(png)
    plt.close(fig)


if __name__ == "__main__":
    app()
