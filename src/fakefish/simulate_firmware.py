"""Simulate the Teensy 4.1 PWM+RC output chain to evaluate duty-quantizer options.

The 4.1 renders the EOD as 8-bit PWM duty updated at STIM_SAMPLE_RATE_HZ (50 kHz),
which drives two DRV8871 half-bridges on a 36 V rail at a 100 kHz complementary
carrier, is smoothed by the per-channel 2-pole output RC (composite -3 dB ~1.23 kHz;
see ``firmware/README.md``), and is then recorded at ~48 kHz. Low-voltage
(localization) pulses sit ~50x below volley peak, so they land on ~5 duty codes.

This models that chain for two quantizers — plain truncation and first-order
error-feedback noise shaping (the shipped driver; see ``out_hal.h::shape``) — and quantifies what shaping
buys: in-band effective bits, the rendered LV pulse after the RC, where the shaped
noise lands, and whether the RC + 48 kHz recording remove it. It also checks the
two failure modes: idle tones at zero, and the accumulator interacting with
between-playback polarity flips.

This is an ANALYSIS tool — it does not touch the firmware.

    fakefish-simulate analyze
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import math  # noqa: E402
from scipy.signal import (  # noqa: E402
    bilinear,
    butter,
    lfilter,
    resample_poly,
    sosfiltfilt,
    welch,
)

from fakefish.viz.loggers import configure_logging, get_logger  # noqa: E402
from fakefish.viz.plotstyle import CATEGORICAL, full_page  # noqa: E402

from fakefish import export_teensy_stimuli as ex  # noqa: E402
from fakefish import _constants as K  # noqa: E402
from fakefish import _resources as _res  # noqa: E402
import typer  # noqa: E402

log = get_logger(__name__)
app = typer.Typer(
    add_completion=False, help="Simulate the Teensy 4.1 PWM+RC output chain."
)


@app.callback()
def _main() -> None:
    """Simulate the Teensy 4.1 PWM+RC output chain (duty-quantizer evaluation)."""


FS = 50_000  # duty update rate (STIM_SAMPLE_RATE_HZ)
REC_FS = 48_000  # recording sampler
# Output filter: two cascaded UNBUFFERED RC sections per channel, single-ended to a star
# ground (firmware/README.md). Unbuffered => H(s) = 1/(1 + 3sRC + (sRC)^2), so the composite
# -3 dB (~1.23 kHz) sits well BELOW the 3.29 kHz per-section corner. RC_FC is the -3 dB point,
# derived rather than hardcoded so changing a component value updates every figure.
OUT_R = 220.0  # ohm, per section
OUT_C = 220e-9  # farad, per section
RC_FC = math.sqrt((math.sqrt(53.0) - 7.0) / 2.0) / (2.0 * math.pi * OUT_R * OUT_C)  # ~1230.6 Hz
SIGNAL_BAND = 5_000  # EOD energy band (FWHM 0.8 ms -> a few kHz)
DUTY_MAX = 255  # 8-bit PWM
STEP = 128  # int16 units per duty code (32767 >> 7 == 255)
FIG_DIR = _res.FIGS_DIR / "firmware_sim"


# ==========================================================================
# Quantizers (per channel; the driver splits sign across two channels)
# ==========================================================================


def quantize_truncate(mag: np.ndarray) -> np.ndarray:
    """Baseline (REJECTED) quantizer: duty = mag >> 7 (floor), clamped 0..255.

    Kept as the comparison arm. The shipped driver uses first-order error-feedback
    noise shaping instead — ``shape()`` in firmware/eel_core/out_hal.h.
    """
    q = np.floor(np.asarray(mag, dtype=np.float64) / STEP).astype(np.int64)
    return np.clip(q, 0, DUTY_MAX)


def quantize_shaped(mag: np.ndarray, err0: float = 0.0) -> tuple[np.ndarray, float]:
    """First-order error-feedback noise shaping on the duty quantizer.

    q = round((mag + err)/STEP); err carries the residual into the next sample so
    the quantization error is high-pass ("noise-shaped") out of the signal band.
    Returns (duty, final_err) — final_err lets the caller check reset behaviour.
    """
    mag = np.asarray(mag, dtype=np.float64)
    out = np.empty(mag.size, dtype=np.int64)
    err = float(err0)
    for i in range(mag.size):
        v = mag[i] + err
        q = int(np.floor(v / STEP + 0.5))  # round to nearest duty code
        if q < 0:
            q = 0
        elif q > DUTY_MAX:
            q = DUTY_MAX
        err = v - q * STEP
        out[i] = q
    return out, err


def render_channels(signed_i16: np.ndarray, shaped: bool):
    """Split a signed int16 stream into two duty channels and reconstruct the
    normalised differential the electrodes see: (duty_a - duty_b)/255."""
    mag_a = np.maximum(signed_i16, 0)
    mag_b = np.maximum(-signed_i16, 0)
    if shaped:
        a, _ = quantize_shaped(mag_a)
        b, _ = quantize_shaped(mag_b)
    else:
        a, b = quantize_truncate(mag_a), quantize_truncate(mag_b)
    return (a - b) / DUTY_MAX


# ==========================================================================
# Analog + recording chain
# ==========================================================================


def rc_lowpass(x: np.ndarray, fs: int = FS, r: float = OUT_R, c: float = OUT_C) -> np.ndarray:
    """The real per-channel output filter: TWO cascaded, UNBUFFERED RC sections to GND.

    Each electrode sees ``[R]--+--[R]--+--`` with a cap from each node to the star
    ground (see ``firmware/README.md``). Because the second section LOADS the first,
    the sections do not multiply::

        H(s) = 1 / (1 + 3*s*R*C + (s*R*C)^2)      <- note the 3, not 2

    so this is NOT ``(1 + sRC)^2``, and NOT a 2nd-order Butterworth either: it is
    overdamped with Q = 1/3 and two REAL poles (1.26 kHz and 8.61 kHz at 220R/220nF).
    The composite -3 dB lands at ~1.23 kHz — well BELOW the 3.29 kHz per-section
    corner, which is the detail this model previously got wrong in two ways at once.

    Applied as a SINGLE CAUSAL PASS. The earlier implementation used ``sosfiltfilt``
    (forward-backward), which squares the magnitude response — exactly double the dB
    at every frequency — and forces zero phase. An analog RC does neither.
    """
    tau = r * c
    b, a = bilinear([1.0], [tau * tau, 3.0 * tau, 1.0], fs=fs)
    return lfilter(b, a, x)


def out_filter_gain_db(f: float, r: float = OUT_R, c: float = OUT_C) -> float:
    """Exact |H| in dB of the real 2-pole output network at frequency ``f``.

    |H| = 1/|1 - (wRC)^2 + j*3wRC|. Use this for every reported gain — a 1-pole
    formula understates the roll-off badly (e.g. -18.3 dB vs the true -21.8 dB at
    10 kHz) and a buffered (1+sRC)^2 form is wrong near the corner.
    """
    x = 2.0 * math.pi * f * r * c
    return float(20.0 * math.log10(1.0 / abs(complex(1.0 - x * x, 3.0 * x))))


def band_limit(x: np.ndarray, fc: float, fs: int, order: int = 4) -> np.ndarray:
    """A pure ANALYSIS band-limiter — not a model of any hardware.

    Used to isolate in-band residuals before taking an RMS. Zero-phase (``sosfiltfilt``)
    is the right choice HERE, precisely because it is not pretending to be an analog
    filter: it must not shift the residual in time relative to the signal it is compared
    against. Do not use it to model the output network — that is ``rc_lowpass``.
    """
    sos = butter(order, fc / (fs / 2), btype="low", output="sos")
    return sosfiltfilt(sos, x)


def record_48k(x: np.ndarray, fs: int = FS, rec_fs: int = REC_FS) -> np.ndarray:
    """Anti-alias + resample to the recorder's 48 kHz (its own AA before the ADC)."""
    from math import gcd

    g = gcd(fs, rec_fs)
    return resample_poly(x, rec_fs // g, fs // g)


def _ideal(signed_i16: np.ndarray) -> np.ndarray:
    """Un-quantised duty (fractional) — the target the quantizer approximates."""
    return signed_i16.astype(np.float64) / (STEP * DUTY_MAX)


# ==========================================================================
# Metrics
# ==========================================================================


def in_band_noise_rms(
    residual: np.ndarray, band: float = SIGNAL_BAND, fs: int = FS
) -> float:
    sos = butter(4, band / (fs / 2), btype="low", output="sos")
    return float(np.std(sosfiltfilt(sos, residual)))


def enob(
    signal_i16_peak: float, residual: np.ndarray, band: float = SIGNAL_BAND
) -> float:
    """Effective bits in-band, referenced to full scale (32767)."""
    noise = in_band_noise_rms(residual, band)
    if noise <= 0:
        return float("inf")
    return float(
        np.log2(32767.0 / (noise * STEP * DUTY_MAX))
    )  # noise back to int16 units


# ==========================================================================
# analyze
# ==========================================================================


@app.command()
def analyze(
    firmware: Path = typer.Option(
        _res.DEFAULT_FIRMWARE, "--firmware", "-f"
    ),
    lv_ratio: float = typer.Option(1 / 50, "--lv-ratio", help="LV peak / volley peak"),
    verbose: int = typer.Option(2, "--verbose", "-v", count=True),
) -> None:
    configure_logging(verbose)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    eod = ex.parse_firmware(firmware)["EOD_HV"].astype(np.float64)
    lv_peak = 32767.0 * lv_ratio  # LV EOD peak in int16 units
    lv_eod = eod / np.max(np.abs(eod)) * lv_peak  # one LV pulse
    log.info(
        "LV peak = %.0f int16 -> %d duty codes (truncated)", lv_peak, int(lv_peak) >> 7
    )

    # ---- (1) a repeated LV pulse train (localization ~10 Hz) ----
    ipi = int(round(FS * 0.1))  # 10 Hz
    train = np.zeros(ipi * 8 + eod.size)
    for k in range(8):
        train[k * ipi : k * ipi + eod.size] += lv_eod
    train_i16 = np.round(train).astype(np.int64)

    ideal = rc_lowpass(_ideal(train_i16))
    base = rc_lowpass(render_channels(train_i16, shaped=False))
    shap = rc_lowpass(render_channels(train_i16, shaped=True))
    res_base = render_channels(train_i16, shaped=False) - _ideal(train_i16)
    res_shap = render_channels(train_i16, shaped=True) - _ideal(train_i16)

    enob_base = enob(lv_peak, res_base)
    enob_shap = enob(lv_peak, res_shap)
    nb = in_band_noise_rms(res_base) * STEP * DUTY_MAX
    ns = in_band_noise_rms(res_shap) * STEP * DUTY_MAX
    log.info(
        "in-band (<%d Hz) noise RMS: truncate=%.2f int16, shaped=%.2f int16",
        SIGNAL_BAND,
        nb,
        ns,
    )
    log.info(
        "in-band ENOB: truncate=%.1f bits, shaped=%.1f bits  (+%.1f bits)",
        enob_base,
        enob_shap,
        enob_shap - enob_base,
    )

    # after the 48 kHz recorder
    ideal_rec = record_48k(rc_lowpass(_ideal(train_i16)))
    base_rec = record_48k(rc_lowpass(render_channels(train_i16, shaped=False)))
    shap_rec = record_48k(rc_lowpass(render_channels(train_i16, shaped=True)))
    n = min(ideal_rec.size, base_rec.size, shap_rec.size)
    rms_base_rec = np.std((base_rec[:n] - ideal_rec[:n])) * STEP * DUTY_MAX
    rms_shap_rec = np.std((shap_rec[:n] - ideal_rec[:n])) * STEP * DUTY_MAX
    log.info(
        "after 16 kHz RC + 48 kHz recorder, residual RMS: truncate=%.2f, shaped=%.2f int16",
        rms_base_rec,
        rms_shap_rec,
    )

    # ---- (2) idle-tone check: zero input, and a small DC offset ----
    zero_duty, zero_err = quantize_shaped(np.zeros(4096))
    dc_small = 8.0  # ~1/16 of a duty step (int16) — a hypothetical stray bias
    dc_duty, _ = quantize_shaped(np.full(4096, dc_small))
    log.info(
        "idle at ZERO input: shaped duty all-zero=%s, final err=%.3f (no tone)",
        bool(np.all(zero_duty == 0)),
        zero_err,
    )

    # ---- (3) polarity-flip / accumulator reset ----
    # playback 1 (pol +): channel A active, leaves residual err. playback 2 (pol -):
    # channel A is now idle (target 0) — a non-reset err spuriously ticks it.
    _, errA_after = quantize_shaped(lv_eod)
    idle_no_reset, _ = quantize_shaped(np.zeros(64), err0=errA_after)
    idle_reset, _ = quantize_shaped(np.zeros(64), err0=0.0)
    log.info(
        "polarity flip: leftover errA=%.1f -> idle channel WITHOUT reset emits %d spurious code(s); "
        "WITH reset: %d",
        errA_after,
        int(np.sum(idle_no_reset > 0)),
        int(np.sum(idle_reset > 0)),
    )

    _plots(
        train_i16,
        ideal,
        base,
        shap,
        res_base,
        res_shap,
        dc_small,
        dc_duty,
        errA_after,
        enob_base,
        enob_shap,
    )


def _plots(
    train_i16,
    ideal,
    base,
    shap,
    res_base,
    res_shap,
    dc_small,
    dc_duty,
    errA_after,
    enob_base,
    enob_shap,
):
    t = np.arange(train_i16.size) / FS * 1e3
    fig, axes = full_page(height_cm=17.0, nrows=3, ncols=2)

    # A: LV pulse after RC — ideal vs truncate vs shaped (zoom on first pulse)
    ax = axes[0, 0]
    zoom = t < 8.0
    ax.plot(t[zoom], ideal[zoom] * STEP * DUTY_MAX, color="0.5", lw=2.0, label="ideal")
    ax.plot(
        t[zoom],
        base[zoom] * STEP * DUTY_MAX,
        color=CATEGORICAL[1],
        lw=1.0,
        label=f"truncate ({enob_base:.1f} b)",
    )
    ax.plot(
        t[zoom],
        shap[zoom] * STEP * DUTY_MAX,
        color=CATEGORICAL[0],
        lw=1.0,
        label=f"shaped ({enob_shap:.1f} b)",
    )
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("post-RC (int16-equiv)")
    ax.legend(fontsize=6)
    ax.set_title("A · LV pulse after 16 kHz RC", fontsize=8)

    # B: residual (quantization error) after RC, in-band
    ax = axes[0, 1]
    ax.plot(
        t[zoom],
        res_base[zoom] * STEP * DUTY_MAX,
        color=CATEGORICAL[1],
        lw=0.7,
        label="truncate",
    )
    ax.plot(
        t[zoom],
        res_shap[zoom] * STEP * DUTY_MAX,
        color=CATEGORICAL[0],
        lw=0.7,
        label="shaped",
    )
    ax.axhline(0, color="grey", lw=0.5)
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("residual (int16)")
    ax.legend(fontsize=6)
    ax.set_title("B · quantization residual (pre-RC noise, shown post-RC)", fontsize=8)

    # C: noise spectrum — where the shaped noise lands + RC + Nyquist markers
    ax = axes[1, 0]
    for res, c, lbl in [
        (res_base, CATEGORICAL[1], "truncate"),
        (res_shap, CATEGORICAL[0], "shaped"),
    ]:
        f, pxx = welch(res * STEP * DUTY_MAX, fs=FS, nperseg=2048)
        ax.semilogy(f / 1e3, pxx, color=c, lw=1.0, label=lbl)
    ax.axvline(RC_FC / 1e3, color="0.4", ls="--", lw=1, label="RC 16 kHz")
    ax.axvline(SIGNAL_BAND / 1e3, color="0.6", ls=":", lw=1, label="signal 5 kHz")
    ax.axvline(
        REC_FS / 2e3, color=CATEGORICAL[4], ls="-.", lw=1, label="48 k Nyq 24 kHz"
    )
    ax.set_xlabel("frequency (kHz)")
    ax.set_ylabel("noise PSD")
    ax.legend(fontsize=6)
    ax.set_title("C · quant-noise PSD", fontsize=8)

    # D: RC transfer + shaped-noise attenuation
    ax = axes[1, 1]
    from scipy.signal import sosfreqz

    sos = butter(1, RC_FC / (FS / 2), btype="low", output="sos")
    w, h = sosfreqz(sos, worN=2048, fs=FS)
    ax.plot(
        w / 1e3,
        20 * np.log10(np.abs(h) + 1e-9),
        color=CATEGORICAL[2],
        lw=1.2,
        label="RC 1-pole",
    )
    ax.axvline(REC_FS / 2e3, color=CATEGORICAL[4], ls="-.", lw=1, label="48 k Nyq")
    ax.axhline(0, color="grey", lw=0.5)
    ax.set_ylim(-40, 5)
    ax.set_xlabel("frequency (kHz)")
    ax.set_ylabel("RC gain (dB)")
    ax.legend(fontsize=6)
    ax.set_title("D · RC attenuation", fontsize=8)

    # E: idle-tone check — spectrum of shaped output for a small DC bias
    ax = axes[2, 0]
    f, pxx = welch(dc_duty.astype(float), fs=FS, nperseg=1024)
    ax.semilogy(f / 1e3, pxx, color=CATEGORICAL[0], lw=1.0)
    ax.axvline(SIGNAL_BAND / 1e3, color="0.6", ls=":", lw=1, label="signal 5 kHz")
    tone = FS * (dc_small / STEP)
    ax.axvline(
        tone / 1e3,
        color=CATEGORICAL[1],
        ls="--",
        lw=1,
        label=f"predicted tone {tone / 1e3:.1f} kHz",
    )
    ax.set_xlabel("frequency (kHz)")
    ax.set_ylabel("PSD")
    ax.legend(fontsize=6)
    ax.set_title(
        f"E · idle tone: stray {dc_small:.0f}-i16 DC (0 input=none)", fontsize=8
    )

    # F: polarity-flip glitch (idle channel, err not reset)
    ax = axes[2, 1]
    ax.axis("off")
    idle_nr, _ = quantize_shaped(np.zeros(64), err0=errA_after)
    idle_r, _ = quantize_shaped(np.zeros(64), err0=0.0)
    lines = [
        "Polarity flip (accumulator per channel):",
        "  within a playback: constant polarity -> one",
        "  channel active, other target 0 -> err stays 0.",
        "  between playbacks the ACTIVE channel swaps.",
        "",
        f"leftover errA after a volley: {errA_after:.1f} int16",
        f"idle ch WITHOUT reset -> {int(np.sum(idle_nr > 0))} spurious duty code(s)",
        f"idle ch WITH reset (err=0)   -> {int(np.sum(idle_r > 0))}",
        "",
        "=> reset both accumulators at eel_player_start.",
        "=> baseline between pulses is exactly 0 -> no idle tone.",
    ]
    ax.text(
        0.0,
        1.0,
        "\n".join(lines),
        va="top",
        ha="left",
        family="monospace",
        fontsize=8,
        transform=ax.transAxes,
    )
    ax.set_title("F · accumulator reset on polarity flip", fontsize=8)

    fig.suptitle(
        "Teensy 4.1 PWM+RC: 8-bit truncation vs first-order noise shaping (LV pulse)"
    )
    out = FIG_DIR / "firmware_sim.png"
    fig.savefig(out)
    plt.close(fig)
    log.info("wrote %s", out)


# ==========================================================================
# DAC fidelity: PWM+RC IS a DAC — full waveform, sample-rate + carrier + 3.5 vs 4.1
# ==========================================================================


def _build_eod(rate_hz: int) -> np.ndarray:
    """The full mean EOD waveform (signed int16, incl. negative phase) at rate_hz."""
    import dataclasses

    cfg = dataclasses.replace(ex.Config.from_yaml(_res.DEFAULT_CONFIG), playback_hz=rate_hz)
    tmpl, oh = ex.load_global_template(cfg.resolve_alignment_json())
    return ex.build_waveform(tmpl, oh, cfg).samples_i16.astype(np.float64)


def _quant_shaped(mag: np.ndarray, step: int, duty_max: int, shaped: bool) -> np.ndarray:
    if not shaped:
        return np.clip(np.floor(mag / step), 0, duty_max)
    out = np.empty(mag.size)
    err = 0.0
    for i in range(mag.size):
        v = mag[i] + err
        q = float(min(max(np.floor(v / step + 0.5), 0), duty_max))
        err = v - q * step
        out[i] = q
    return out


def render_41(eod_i16: np.ndarray, bits: int = 8, shaped: bool = True) -> np.ndarray:
    """4.1 PWM path: sign-split + (shaped) duty quantize -> normalised differential
    (duty_a - duty_b)/duty_max in [-1, 1] (the full bipolar waveform, both phases)."""
    step = 32768 >> bits
    dmax = (1 << bits) - 1
    a = _quant_shaped(np.maximum(eod_i16, 0), step, dmax, shaped)
    b = _quant_shaped(np.maximum(-eod_i16, 0), step, dmax, shaped)
    return (a - b) / dmax


def render_35(eod_i16: np.ndarray, bits: int = 12) -> np.ndarray:
    """3.5 DAC path: per-channel N-bit truncation (the driver's s>>3), normalised
    differential — same sign-split as the 4.1, just a real DAC (no shaping, no RC)."""
    return render_41(eod_i16, bits, shaped=False)


def carrier_ripple_pp(duty_frac: float, carrier_hz: float, rc_fc: float = RC_FC,
                      sim_fs: float = 12e6) -> float:
    """Peak-to-peak residual ripple (fraction of full scale) after a 1-pole RC for a
    constant-duty PWM at ``carrier_hz`` — models the analog carrier feedthrough."""
    from scipy.signal import butter, lfilter

    n = int(sim_fs * 80 / carrier_hz)
    t = np.arange(n) / sim_fs
    pwm = ((t * carrier_hz) % 1.0 < duty_frac).astype(np.float64)
    b, a = butter(1, rc_fc / (sim_fs / 2), btype="low")
    y = lfilter(b, a, pwm)[n // 2 :]  # discard settling
    return float(y.max() - y.min())


@app.command()
def dac(verbose: int = typer.Option(2, "--verbose", "-v", count=True)) -> None:
    """Evaluate the 4.1 PWM+RC as a DAC: sample rate, carrier, and 4.1-vs-3.5-vs-source."""
    configure_logging(verbose)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    UP = 400_000  # common overlay rate

    # One common source (built once at UP), downsampled to 50/100 k so the overlay
    # isolates sample-rate + quantization effects (not per-rate build differences).
    src_i16 = _build_eod(UP)
    eod50 = np.round(resample_poly(src_i16, 1, UP // 50_000))
    eod100 = np.round(resample_poly(src_i16, 1, UP // 100_000))
    src = src_i16 / 32767.0  # smooth analog reference

    def to_common(norm_signal: np.ndarray, rate: int, rc: bool) -> np.ndarray:
        z = np.repeat(norm_signal, UP // rate)          # ZOH to the common rate
        return rc_lowpass(z, UP) if rc else z

    o35 = to_common(render_35(eod50, 12), 50_000, rc=False)      # 3.5: 12-bit DAC, no RC
    o41_50 = to_common(render_41(eod50, 8, True), 50_000, rc=True)
    o41_100 = to_common(render_41(eod100, 8, True), 100_000, rc=True)

    # align all on their peak for overlay
    def peak_align(x):
        return int(np.argmax(np.abs(x)))

    def shift_to(x, ref_peak):
        d = ref_peak - peak_align(x)
        return np.roll(x, d)

    rp = peak_align(src)
    o35, o41_50, o41_100 = shift_to(o35, rp), shift_to(o41_50, rp), shift_to(o41_100, rp)
    t = (np.arange(src.size) - rp) / UP * 1e3  # ms about the peak

    # metrics: RMS error vs source, in the signal band
    def rmse(x):
        n = min(x.size, src.size)
        return float(np.std(band_limit((x[:n] - src[:n]), SIGNAL_BAND, UP, order=4)) * 32767)

    log.info("RMS error vs source (int16): 3.5/12-bit=%.1f  4.1@50k=%.1f  4.1@100k=%.1f",
             rmse(o35), rmse(o41_50), rmse(o41_100))

    # carrier residual: 8-bit/586 kHz vs 7-bit/1.17 MHz, worst-case duty 0.5
    r8 = carrier_ripple_pp(0.5, 150e6 / 256) * 100
    r7 = carrier_ripple_pp(0.5, 150e6 / 128) * 100
    log.info("586 kHz carrier residual after the output filter: %.1f%% pp; 1.17 MHz: %.1f%% pp", r8, r7)
    # exact magnitude of the real 2-pole output network
    for fq in (5e3, 16e3, 50e3, 586e3, 1170e3):
        log.info("  output-filter gain @ %6.0f kHz: %.1f dB", fq / 1e3, out_filter_gain_db(fq))

    _dac_plots(t, src, o35, o41_50, o41_100, r8, r7)


def _dac_plots(t, src, o35, o41_50, o41_100, r8, r7):
    n = min(src.size, o35.size, o41_50.size, o41_100.size)
    t, src, o35, o41_50, o41_100 = t[:n], src[:n], o35[:n], o41_50[:n], o41_100[:n]
    sc = 32767.0
    fig, axes = full_page(height_cm=15.0, nrows=2, ncols=2)

    for ax, win, ttl in [(axes[0, 0], (t > -1.5) & (t < 2.0), "A · full EOD (incl. negative phase)"),
                         (axes[0, 1], (t > -0.6) & (t < 0.4), "B · rising-edge zoom")]:
        ax.plot(t[win], src[win] * sc, color="0.5", lw=2.2, label="source")
        ax.plot(t[win], o35[win] * sc, color=CATEGORICAL[2], lw=1.0, label="3.5 (12-bit DAC)")
        ax.plot(t[win], o41_50[win] * sc, color=CATEGORICAL[1], lw=1.0, label="4.1 @50k+RC")
        ax.plot(t[win], o41_100[win] * sc, color=CATEGORICAL[0], lw=1.0, label="4.1 @100k+RC")
        ax.axhline(0, color="grey", lw=0.5)
        ax.set_xlabel("time (ms)")
        ax.set_ylabel("int16-equiv")
        ax.legend(fontsize=6)
        ax.set_title(ttl, fontsize=8)

    # C: error vs source (4.1 @50k vs @100k) — the sample-rate cost
    ax = axes[1, 0]
    win = (t > -0.6) & (t < 1.0)
    ax.plot(t[win], (o41_50[win] - src[win]) * sc, color=CATEGORICAL[1], lw=0.8, label="4.1 @50k − source")
    ax.plot(t[win], (o41_100[win] - src[win]) * sc, color=CATEGORICAL[0], lw=0.8, label="4.1 @100k − source")
    ax.axhline(0, color="grey", lw=0.5)
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("error (int16)")
    ax.legend(fontsize=6)
    ax.set_title("C · sample-rate cost (50 k staircase ripple vs 100 k)", fontsize=8)

    # D: carrier residual text
    ax = axes[1, 1]
    ax.axis("off")
    lines = [
        "Carrier residual after the 1-pole 16 kHz RC",
        "(worst-case duty 0.5, % of full scale pp):",
        "",
        f"  8-bit / 585.9 kHz  ->  {r8:.1f}%  pp",
        f"  7-bit / 1.17 MHz   ->  {r7:.1f}%  pp   (-6 dB)",
        "",
        "586 kHz is >> the eel's band and the <5 kHz",
        "detection band; a 48 kHz recorder's own",
        "anti-alias removes it before its ADC.",
        "If it must drop further: raise the carrier",
        "(7-bit/1.17 MHz) + noise shaping recovers the",
        "lost bit — NOT a 2nd RC pole (two 440/22 nF",
        "stages pull the corner to ~6 kHz, into the EOD).",
    ]
    ax.text(0.0, 1.0, "\n".join(lines), va="top", ha="left", family="monospace", fontsize=8,
            transform=ax.transAxes)
    ax.set_title("D · carrier headroom", fontsize=8)

    fig.suptitle("Teensy 4.1 PWM+RC as a DAC — full waveform vs 3.5 vs source (1-pole 16 kHz RC)")
    out = FIG_DIR / "firmware_dac.png"
    fig.savefig(out)
    plt.close(fig)
    log.info("wrote %s", out)


# ==========================================================================
# carrier — residual PWM carrier on an LV pulse after the RC, 50 kHz vs 100 kHz
# ==========================================================================
# The output stage moved from a DRV8833 at 9 V / 50 kHz to two DRV8871 single-bridge drivers at
# 36 V / 100 kHz (firmware config.h PWM_CARRIER_HZ). This command models the ACTUAL carrier
# switching (unlike `analyze`/`render_channels`, which model only the duty quantizer at FS) so the
# residual carrier can be compared 50 kHz vs 100 kHz after the 1-pole 16 kHz output RC.


def render_carrier_analog(
    signed_i16: np.ndarray, carrier_hz: float, sim_fs: float = 30_000_000.0
) -> tuple[np.ndarray, np.ndarray]:
    """The analog differential the electrodes see: 8-bit noise-shaped duty per FS sample -> REAL
    PWM edges at ``carrier_hz`` -> 1-pole 16 kHz RC. Returns (t_s, y) with y in [-1, 1] (fraction
    of full scale). sim_fs (30 MHz) is ~1.2x 256*carrier at 100 kHz (~300 sim-samples/carrier
    period, ~1.2 per 8-bit duty LSB) -- enough to render the RC-filtered ripple for the plot; raise
    it if the carrier ever approaches the 8-bit resolution ceiling (~586 kHz)."""
    from scipy.signal import butter, lfilter

    da = _quant_shaped(np.maximum(signed_i16, 0.0), STEP, DUTY_MAX, True) / DUTY_MAX
    db = _quant_shaped(np.maximum(-signed_i16, 0.0), STEP, DUTY_MAX, True) / DUTY_MAX
    dur = signed_i16.size / FS
    n = int(round(sim_fs * dur))
    t = np.arange(n) / sim_fs
    idx = np.minimum((t * FS).astype(np.int64), signed_i16.size - 1)
    phase = (t * carrier_hz) % 1.0  # 0..1 within each carrier period
    diff = (phase < da[idx]).astype(np.float64) - (phase < db[idx]).astype(np.float64)
    b, a = butter(1, RC_FC / (sim_fs / 2), btype="low")
    return t, lfilter(b, a, diff)


def _rc_gain_db(f: float, fc: float = RC_FC) -> float:
    """Exact 1-pole RC magnitude in dB (|H| = 1/sqrt(1+(f/fc)^2))."""
    return float(20.0 * np.log10(1.0 / np.sqrt(1.0 + (f / fc) ** 2)))


def _alias_48k(f: float, rec_fs: int = REC_FS) -> float:
    """Where a tone at ``f`` folds when sampled at ``rec_fs`` (into 0..rec_fs/2)."""
    return float(abs(f - round(f / rec_fs) * rec_fs))


@app.command()
def carrier(
    firmware: Path = typer.Option(_res.DEFAULT_FIRMWARE, "--firmware", "-f"),
    lv_ratio: float = typer.Option(1 / 50, "--lv-ratio", help="LV peak / volley peak"),
    old_hz: float = typer.Option(50_000.0, "--old-carrier", help="previous DRV8833 carrier"),
    new_hz: float = typer.Option(
        100_000.0, "--new-carrier", help="new DRV8871 carrier (config.h PWM_CARRIER_HZ)"
    ),
    verbose: int = typer.Option(2, "--verbose", "-v", count=True),
) -> None:
    """Residual PWM carrier on a low-voltage localization pulse after the 1-pole 16 kHz RC, at the
    old 50 kHz (DRV8833/9 V) vs the new 100 kHz (DRV8871/36 V) carrier. Doubling the carrier moves
    it from ~3x to ~6x the RC corner (~10 dB -> ~16 dB rejection) and folds the 48 kHz-grid alias
    from ~2 kHz (in-band) up to ~4 kHz. Writes firmware_carrier.png."""
    configure_logging(verbose)
    FIG_DIR.mkdir(parents=True, exist_ok=True)

    # One low-voltage EOD pulse (localization sits ~50x below the volley peak), padded with silence
    # so the pulse (where duty > 0, hence where the carrier rides) is bracketed by a clean baseline.
    eod = ex.parse_firmware(firmware)["EOD_HV"].astype(np.float64)
    lv_peak = 32767.0 * lv_ratio
    lv_eod = eod / np.max(np.abs(eod)) * lv_peak
    pad = int(round(FS * 1.0e-3))  # 1 ms of silence each side
    pulse = np.zeros(lv_eod.size + 2 * pad)
    pulse[pad : pad + lv_eod.size] = lv_eod
    pulse_i16 = np.round(pulse).astype(np.int64)

    t_old, y_old = render_carrier_analog(pulse_i16, old_hz)
    t_new, y_new = render_carrier_analog(pulse_i16, new_hz)
    # Reference: the infinite-carrier / infinite-resolution limit (un-quantised fractional duty,
    # no PWM) through the SAME causal 1-pole 16 kHz RC as y_old/y_new -- so panel A is apples-to-
    # apples (a single causal |H|, not the zero-phase |H|^2 of rc_lowpass).
    from scipy.signal import lfilter as _lfilter

    _b, _a = butter(1, RC_FC / (FS / 2), btype="low")
    ideal = _lfilter(_b, _a, _ideal(pulse_i16))

    # Worst-case (duty 0.5) residual carrier after the RC, pp % of full scale, and the RC gain +
    # 48 kHz-grid alias fold at each carrier — the headline numbers behind the config.h comment.
    r_old = carrier_ripple_pp(0.5, old_hz) * 100
    r_new = carrier_ripple_pp(0.5, new_hz) * 100
    log.info(
        "worst-case (duty 0.5) carrier ripple after 16 kHz RC: %.2f%% pp @ %.0f kHz, "
        "%.2f%% pp @ %.0f kHz  (%.1f dB better)",
        r_old, old_hz / 1e3, r_new, new_hz / 1e3, 20 * np.log10(r_old / r_new),
    )
    for f in (old_hz, new_hz):
        log.info(
            "  RC gain @ %3.0f kHz: %+.1f dB (%.1fx corner); 48 kHz-grid alias -> %.1f kHz",
            f / 1e3, _rc_gain_db(f), f / RC_FC, _alias_48k(f) / 1e3,
        )

    _carrier_plots(t_old, y_old, t_new, y_new, pulse_i16, ideal, old_hz, new_hz, r_old, r_new)


def _carrier_plots(t_old, y_old, t_new, y_new, pulse_i16, ideal, old_hz, new_hz, r_old, r_new):
    sc = STEP * DUTY_MAX  # int16-equiv full scale
    t_ms_old, t_ms_new = t_old * 1e3, t_new * 1e3
    t_fs = np.arange(pulse_i16.size) / FS * 1e3
    lbl_old, lbl_new = f"{old_hz / 1e3:.0f} kHz", f"{new_hz / 1e3:.0f} kHz"
    fig, axes = full_page(height_cm=15.0, nrows=2, ncols=2)

    # A: the whole LV pulse after RC — old vs new carrier over the ideal-after-RC reference.
    ax = axes[0, 0]
    ax.plot(t_fs, ideal * sc, color="0.5", lw=2.2, label="ideal (after RC)")
    ax.plot(t_ms_old, y_old * sc, color=CATEGORICAL[1], lw=0.8, label=f"{lbl_old} carrier")
    ax.plot(t_ms_new, y_new * sc, color=CATEGORICAL[0], lw=0.8, label=f"{lbl_new} carrier")
    ax.axhline(0, color="grey", lw=0.5)
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("post-RC (int16-equiv)")
    ax.legend(fontsize=6)
    ax.set_title("A · LV pulse after 16 kHz RC", fontsize=8)

    # B: zoom on the pulse PEAK (where duty is highest, so the residual carrier ripple is largest) —
    # the 100 kHz sawtooth ripple is about half the 50 kHz one.
    ax = axes[0, 1]
    pk_ms = int(np.argmax(np.abs(pulse_i16))) / FS * 1e3  # pulse_i16 is already padded; do NOT re-add pad
    lo, hi = pk_ms - 0.30, pk_ms + 0.30
    for t_ms, y, c, lbl in [
        (t_ms_old, y_old, CATEGORICAL[1], lbl_old),
        (t_ms_new, y_new, CATEGORICAL[0], lbl_new),
    ]:
        w = (t_ms >= lo) & (t_ms <= hi)
        ax.plot(t_ms[w], y[w] * sc, color=c, lw=0.8, label=lbl)
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("post-RC (int16-equiv)")
    ax.legend(fontsize=6)
    ax.set_title("B · residual carrier ripple (pulse-peak zoom)", fontsize=8)

    # C: constant-duty carrier ripple pp vs duty — the general suppression (worst near duty 0.5).
    ax = axes[1, 0]
    duties = np.linspace(0.02, 0.98, 25)
    rp_old = np.array([carrier_ripple_pp(d, old_hz) for d in duties]) * 100
    rp_new = np.array([carrier_ripple_pp(d, new_hz) for d in duties]) * 100
    ax.plot(duties, rp_old, color=CATEGORICAL[1], lw=1.2, label=lbl_old)
    ax.plot(duties, rp_new, color=CATEGORICAL[0], lw=1.2, label=lbl_new)
    ax.set_xlabel("PWM duty (fraction)")
    ax.set_ylabel("carrier ripple after RC (% FS, pp)")
    ax.legend(fontsize=6)
    ax.set_title("C · residual carrier vs duty (worst-case near 0.5)", fontsize=8)

    # D: the numbers behind the config.h comment.
    ax = axes[1, 1]
    ax.axis("off")
    lines = [
        "PWM carrier 50 -> 100 kHz",
        "(DRV8871 upgrade; single-ended grid)",
        "",
        f"1-pole {RC_FC / 1e3:.0f} kHz RC (2x220 Ohm + 22 nF):",
        f"   50 kHz  {_rc_gain_db(old_hz):+.1f} dB  ({old_hz / RC_FC:.1f}x corner)",
        f"  100 kHz  {_rc_gain_db(new_hz):+.1f} dB  ({new_hz / RC_FC:.1f}x corner)",
        "",
        "worst-case (duty 0.5) ripple after RC:",
        f"   50 kHz  {r_old:.2f}% pp",
        f"  100 kHz  {r_new:.2f}% pp  ({20 * np.log10(r_old / r_new):.1f} dB better)",
        "",
        "48 kHz-grid alias fold:",
        f"   50 kHz -> {_alias_48k(old_hz) / 1e3:.0f} kHz (in-band, bad)",
        f"  100 kHz -> {_alias_48k(new_hz) / 1e3:.0f} kHz (higher, RC-cut first)",
        "",
        "single-ended grid has no common-mode",
        "rejection -> suppress the carrier at source.",
    ]
    ax.text(0.0, 1.0, "\n".join(lines), va="top", ha="left", family="monospace",
            fontsize=7.5, transform=ax.transAxes)
    ax.set_title("D · why 100 kHz", fontsize=8, loc="left")

    fig.suptitle(
        "Fakefish PWM carrier 50 vs 100 kHz — residual carrier on an LV pulse after the 16 kHz RC"
    )
    out = FIG_DIR / "firmware_carrier.png"
    fig.savefig(out)
    plt.close(fig)
    log.info("wrote %s", out)


# ==========================================================================
# marker — the out-of-band 10 kHz sine anchor through the RC + 48 kHz recorder
# ==========================================================================

# Single-sourced from shared/stim_constants.json (see fakefish.gen_constants) rather than
# re-derived here, so the simulation models the SAME marker the card actually carries.
MARKER_FREQ_HZ = K.MARKER_FREQ_HZ
MARKER_LUT = K.MARKER_LUT.astype(np.int64)  # one exact unit-sine cycle at 10 kHz / 50 kHz


def marker_i16(total: int, ramp: int, amplitude: float) -> np.ndarray:
    """The signed-int16 sine marker: LUT[t%n] * amplitude * raised-cosine on/off ramp,
    clamped. The twin of build_sd_card.render_marker, which bakes it into the WAVs."""
    per = MARKER_LUT.size
    t = np.arange(total)
    env = np.ones(total, dtype=np.float64)
    if ramp > 0 and total >= 2 * ramp:
        on = t < ramp
        env[on] = 0.5 * (1.0 - np.cos(np.pi * t[on] / ramp))
        off = t >= total - ramp
        env[off] = 0.5 * (1.0 - np.cos(np.pi * (total - t[off]) / ramp))
    s = amplitude * env * MARKER_LUT[t % per].astype(np.float64)
    return np.clip(np.round(s), -32767, 32767).astype(np.int64)


@app.command()
def marker(
    amplitude: float = typer.Option(0.90, "--amplitude", "-a", help="marker level (0..1)"),
    ramp: int = typer.Option(100, "--ramp", help="raised-cosine ramp samples"),
    verbose: int = typer.Option(2, "--verbose", "-v", count=True),
) -> None:
    """Push the 10 kHz sine ANCHOR through the real output chain (8-bit noise-shaped PWM ->
    1-pole 16 kHz RC -> 48 kHz recorder) and confirm it survives as a clean out-of-band
    line. The tone (not a pulse) is what locates each playback and pins the clock drift."""
    configure_logging(verbose)
    FIG_DIR.mkdir(parents=True, exist_ok=True)

    # 1 s of the lead-in marker (== MARKER_FREQ_HZ exact cycles) is enough for a tight spectrum.
    total = FS  # 1 s
    m_i16 = marker_i16(total, ramp, amplitude)
    diff = render_channels(m_i16, shaped=True)          # 8-bit noise-shaped, differential
    rc = rc_lowpass(diff, FS)                            # the 2-pole per-channel output filter
    rec = record_48k(rc)                                  # what the 48 kHz ADC captures
    ideal = _ideal(m_i16)                                # the un-quantised target

    # exact gain of the real 2-pole output network, and the recorded line.
    for fq in (5e3, 10e3, 15e3):
        log.info("output-filter gain @ %.0f kHz: %.2f dB (%d samples/cycle)",
                 fq / 1e3, out_filter_gain_db(fq), int(round(FS / fq)))
    f, p = welch(rec, REC_FS, nperseg=8192)
    k = int(np.argmin(np.abs(f - MARKER_FREQ_HZ)))
    band = (f > 1.0) & (np.abs(f - MARKER_FREQ_HZ) > 300)  # everything off the marker line
    peak_to_floor_db = 10 * np.log10(p[k] / np.median(p[band]))
    log.info("recorded marker line: %.0f dB above the off-line median (clean anchor)",
             peak_to_floor_db)
    log.info("net DC of the emitted marker (int16 units): %d (odd-symmetric -> ~0)",
             int(m_i16.sum()))

    _marker_plots(m_i16, ideal, rec, f, p, amplitude, ramp, peak_to_floor_db)


def _marker_plots(m_i16, ideal, rec, f, p, amplitude, ramp, peak_to_floor_db):
    sc = 32767.0
    fig, axes = full_page(height_cm=15.0, nrows=2, ncols=2)

    # A: a few cycles — ideal marker vs what the 48 kHz recorder captures (a clean sine).
    # Index each signal by its OWN sample rate over the SAME absolute steady-state window
    # (both the RC and the resampler are zero-phase, so aligned in time they overlay).
    ax = axes[0, 0]
    t0, span = 0.010, 800e-6  # start 10 ms in (steady), show 0.8 ms (~4 cycles)
    i0, i1 = int(t0 * FS), int((t0 + span) * FS)
    r0, r1 = int(t0 * REC_FS), int((t0 + span) * REC_FS)
    ax.plot((np.arange(i0, i1) - i0) / FS * 1e6, ideal[i0:i1] * sc, color="0.5", lw=2.4,
            label="ideal (LUT)")
    ax.plot((np.arange(r0, r1) - r0) / REC_FS * 1e6, rec[r0:r1] * sc, color=CATEGORICAL[1],
            lw=1.2, marker="o", ms=2.5, label="recorded @48 kHz")
    ax.axhline(0, color="grey", lw=0.5)
    ax.set_xlabel("time (µs)")
    ax.set_ylabel("int16-equiv")
    ax.legend(fontsize=6)
    ax.set_title("A · 10 kHz tone: LUT vs recorded (steady state)", fontsize=8)

    # B: onset — the raised-cosine ramp (click-free zero-start into the tone).
    ax = axes[0, 1]
    win = int(2.5 * ramp)
    t_ms = np.arange(win) / FS * 1e3
    ax.plot(t_ms, m_i16[:win], color=CATEGORICAL[0], lw=0.9)
    ax.plot(t_ms, amplitude * sc * _cos_ramp(np.arange(win), ramp), color=CATEGORICAL[3],
            lw=1.4, ls="--", label="raised-cosine envelope")
    ax.plot(t_ms, -amplitude * sc * _cos_ramp(np.arange(win), ramp), color=CATEGORICAL[3],
            lw=1.4, ls="--")
    ax.axhline(0, color="grey", lw=0.5)
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("emitted int16")
    ax.legend(fontsize=6)
    ax.set_title("B · onset ramp (zero-start, anti-click + no splatter)", fontsize=8)

    # C: recorded spectrum — a single 10 kHz line, well off the biological band.
    ax = axes[1, 0]
    ax.semilogy(f / 1e3, p / p.max(), color=CATEGORICAL[1], lw=0.9)
    ax.axvline(MARKER_FREQ_HZ / 1e3, color=CATEGORICAL[0], lw=1.0, ls=":", label="10 kHz marker")
    ax.axvspan(0, 1.8, color="0.8", alpha=0.5, label="wave-fish EOD band ≤1.8 kHz")
    ax.set_xlim(0, 24)
    ax.set_ylim(1e-9, 2)
    ax.set_xlabel("frequency (kHz)")
    ax.set_ylabel("recorded PSD (norm.)")
    ax.legend(fontsize=6)
    ax.set_title("C · recorded spectrum — one line", fontsize=8)

    # D: the facts.
    ax = axes[1, 1]
    ax.axis("off")
    g = out_filter_gain_db
    lines = [
        "Out-of-band 10 kHz sine ANCHOR",
        "",
        f"1-pole {RC_FC / 1e3:.0f} kHz diff. RC (22 nF/440 Ω):",
        f"   4 kHz  {g(4e3):+.2f} dB  (12.5 samp/cyc)",
        f"   5 kHz  {g(5e3):+.2f} dB  (10 samp/cyc, LUT)",
        f"   6 kHz  {g(6e3):+.2f} dB  (8.33 samp/cyc)",
        "",
        f"recorded 5 kHz line: {peak_to_floor_db:.0f} dB above",
        "   the off-line PSD median (48 kHz ADC)",
        "",
        "above the electrosensory band and off the",
        "whole local biological band -> narrowband",
        "detector flags it with ~0 false positives.",
        "The continuous known tone also pins the",
        "recorder-vs-playback clock-drift factor.",
    ]
    ax.text(0.0, 1.0, "\n".join(lines), va="top", ha="left", family="monospace",
            fontsize=7.5, transform=ax.transAxes)
    ax.set_title("D · why 5 kHz", fontsize=8, loc="left")

    fig.suptitle("Fakefish 5 kHz sine marker through the PWM+RC+48 kHz recorder chain")
    out = FIG_DIR / "firmware_marker.png"
    fig.savefig(out)
    plt.close(fig)
    log.info("wrote %s", out)


def _cos_ramp(t: np.ndarray, ramp: int) -> np.ndarray:
    """The raised-cosine onset envelope (0->1 over [0,ramp), then flat) for the B panel."""
    env = np.ones(t.size, dtype=np.float64)
    on = t < ramp
    env[on] = 0.5 * (1.0 - np.cos(np.pi * t[on] / ramp))
    return env


if __name__ == "__main__":
    app()
