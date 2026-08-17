"""Multi-panel QC figure for the Teensy stimulus export.

Rendered by ``export_teensy_stimuli.py export`` into ``data/stimuli_qc.pdf``.
The whole point is that the export can be *seen* to be correct before it ever
touches water. Panels:

* waveform triptych — global template vs resampled vs int16-quantised, overlaid,
  with a quantisation-residual trace; FWHM / duration / peak-to-peak / net-charge
  annotated;
* a reconstructed channel A / channel B trace for the first pulses of one volley —
  the sign-split magnitudes handed to ``out_write()`` (pre-quantiser targets; the
  hardware drives two DRV8871 bridges via PWM, it has no DACs);
* per volley exemplar — instantaneous rate vs pulse index; IPI histogram with the
  10 ms (>100 Hz peak) requirement marked; and the three single-fish panels (CV2
  over pulse index with p95/max marked, the detrended-IPI lag-1 scatter, and the
  spatial amplitude-vector cluster scatter with any purified pulses highlighted);
* localization trains — rate over time and IPI histogram.

Figures are built with the vendored figure constructors (``fakefish.viz``),
so the convention (two widths, 300 dpi, Inter, deck colours) is honoured.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402

from fakefish.viz.plotstyle import CATEGORICAL  # noqa: E402

from fakefish import export_teensy_stimuli as ex  # noqa: E402


def _scene_arrays(cfg, scene):
    """Re-read (times, amp_kept, amp_removed, track_ids) for a scene."""
    site_dir = cfg.eods_root / scene.site
    h5 = next(site_dir.glob(f"{scene.recording}*.h5"))
    rec = ex.open_recording(h5, scene.site, cfg.original_hz_fallback)
    try:
        kept = np.asarray(scene.rows)
        removed = np.asarray(scene.removed_rows)
        t = np.sort(rec.times_s[kept])
        amp_all = rec.amplitude_vectors(
            np.concatenate([kept, removed]) if removed.size else kept
        )
        amp_kept = amp_all[: kept.size]
        amp_removed = (
            amp_all[kept.size :] if removed.size else np.zeros((0, amp_all.shape[1]))
        )
        tids = rec.track_id[kept]
    finally:
        rec.close()
    return t, amp_kept, amp_removed, tids


def _pca2(x: np.ndarray) -> np.ndarray:
    """L2-normalise rows then project to 2D by PCA (for the cluster scatter)."""
    if x.shape[0] == 0:
        return x[:, :2] if x.shape[1] >= 2 else np.zeros((0, 2))
    n = np.linalg.norm(x, axis=1, keepdims=True)
    xn = x / np.where(n > 0, n, 1.0)
    xc = xn - xn.mean(axis=0, keepdims=True)
    u, s, vt = np.linalg.svd(xc, full_matrices=False)
    return xc @ vt[:2].T


def _panel_waveform(fig, gs, waveform) -> None:
    ax = fig.add_subplot(gs[0, :])
    up_hz = waveform.playback_hz
    orig_hz = waveform.original_hz
    t_orig = np.arange(waveform.pre_resample_norm.size) / orig_hz * 1e6
    t_orig -= t_orig[int(np.argmax(np.abs(waveform.pre_resample_norm)))]
    t_rs = np.arange(waveform.samples_norm.size) / up_hz * 1e6
    t_rs -= t_rs[int(np.argmax(np.abs(waveform.samples_norm)))]
    i16f = waveform.samples_i16.astype(float) / 32767.0

    ax.plot(
        t_orig,
        waveform.pre_resample_norm,
        color=CATEGORICAL[0],
        lw=1.2,
        label=f"template @ {orig_hz / 1000:.0f} kHz",
    )
    ax.plot(
        t_rs,
        waveform.samples_norm,
        color=CATEGORICAL[1],
        lw=1.0,
        ls="--",
        label=f"resampled @ {up_hz / 1000:.0f} kHz",
    )
    ax.plot(
        t_rs, i16f, color=CATEGORICAL[2], lw=0.8, alpha=0.8, label="int16 quantised"
    )
    ax.axhline(0, color="grey", lw=0.5)
    ax.set_xlabel("time (µs)")
    ax.set_ylabel("amplitude (norm)")
    ax.legend(loc="upper right")
    ax.set_title(
        f"EOD waveform — {waveform.n} samp, {waveform.duration_us:.0f} µs, "
        f"FWHM {waveform.fwhm_us:.0f} µs"
    )
    ax.text(
        0.01,
        0.97,
        f"p2p {waveform.peak_to_peak_norm:.2f}   net ∫ {waveform.net_integral:+.1f}   "
        f"pos/neg area {waveform.pos_neg_area_ratio:.1f} → monophasic: randomise polarity",
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=7,
    )

    # quantisation residual
    axr = fig.add_subplot(gs[1, :])
    axr.plot(t_rs, (i16f - waveform.samples_norm), color=CATEGORICAL[4], lw=0.8)
    axr.axhline(0, color="grey", lw=0.5)
    axr.set_xlabel("time (µs)")
    axr.set_ylabel("int16 − float")
    axr.set_title("quantisation residual")


def _panel_dac(fig, gs, waveform, ipi_us, n_pulses=5) -> None:
    ax = fig.add_subplot(gs[2, :])
    ipi = np.asarray(ipi_us)[: n_pulses + 1]
    trace = ex.reconstruct_trace(
        waveform.samples_i16,
        ipi,
        waveform.playback_hz,
        amplitude=1.0,
        polarity=1,
        pad_us=500.0,
    )
    dac_a, dac_b = ex.split_dac(trace)
    t = np.arange(trace.size) / waveform.playback_hz * 1e3
    ax.plot(t, dac_a, color=CATEGORICAL[0], lw=0.9, label="DAC_A = max(w,0)")
    ax.plot(t, -dac_b, color=CATEGORICAL[1], lw=0.9, label="−DAC_B = −max(−w,0)")
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("DAC (norm)")
    ax.legend(loc="upper right")
    ax.set_title(f"reconstructed Teensy output — first {n_pulses} pulses of one volley")


def _page_waveform(pdf, waveform, first_volley_ipi) -> None:
    from fakefish.viz.plotstyle import blank_figure

    fig = blank_figure(width="full", height_cm=16.0)
    gs = fig.add_gridspec(3, 1, height_ratios=[3, 1.2, 2.2], hspace=0.6)
    _panel_waveform(fig, gs, waveform)
    _panel_dac(fig, gs, waveform, first_volley_ipi)
    fig.suptitle("Teensy EOD waveform — export fidelity", y=0.995)
    pdf.savefig(fig)
    plt.close(fig)


def _page_volley(pdf, cfg, scene, ipi_real) -> None:
    from fakefish.viz.plotstyle import full_page

    t, amp_kept, amp_removed, tids = _scene_arrays(cfg, scene)
    fig, axes = full_page(height_cm=17.0, nrows=3, ncols=2)
    verdict = "SINGLE-FISH" if scene.single_fish else "REJECT: " + scene.reject_reason
    fig.suptitle(
        f"VOLLEY  {scene.site} / {scene.recording[:22]}  ev{scene.event_id}  —  {verdict}"
    )

    # A: instantaneous rate vs pulse index
    rate, _ = ex._rate_profile(t)
    ax = axes[0, 0]
    ax.plot(np.arange(rate.size), rate, "-o", color=CATEGORICAL[0], ms=2.5)
    ax.set_xlabel("interval index")
    ax.set_ylabel("instantaneous rate (Hz)")
    ax.set_title(
        f"A · rate profile (peak {scene.peak_rate_hz:.0f} Hz, gamma {scene.gamma_score:.2f})"
    )

    # B: IPI histogram with the >100 Hz-peak (10 ms) requirement marked. A real
    # volley must have mass to the LEFT of the line (its fastest pulses reach the
    # volley regime); a train sitting entirely right of it is a fast-localization
    # fragment the export gate would drop.
    ax = axes[0, 1]
    ipi_ms_real = np.asarray(ipi_real)[1:] / 1000.0
    bins = np.linspace(0, max(ipi_ms_real.max(), 1.0), 30)
    ax.hist(ipi_ms_real, bins=bins, color=CATEGORICAL[0], alpha=0.7, label="IPI")
    ax.axvline(
        10.0,
        color=CATEGORICAL[4],
        lw=1.0,
        ls="--",
        label="10 ms (100 Hz peak)",
    )
    ax.set_xlabel("IPI (ms)")
    ax.set_ylabel("count")
    ax.legend()
    ax.set_title(f"B · IPI histogram (min {ipi_ms_real.min():.1f} ms)")

    # C: CV2 over pulse index
    ax = axes[1, 0]
    cv2 = ex.cv2_series(t)
    ax.plot(np.arange(cv2.size), cv2, "-", color=CATEGORICAL[2], lw=0.9)
    ax.axhline(
        scene.cv2_p95,
        color=CATEGORICAL[1],
        lw=0.8,
        ls="--",
        label=f"p95={scene.cv2_p95:.2f}",
    )
    ax.axhline(
        scene.cv2_max,
        color=CATEGORICAL[4],
        lw=0.8,
        ls=":",
        label=f"max={scene.cv2_max:.2f}",
    )
    ax.set_ylim(0, max(1.0, scene.cv2_max * 1.1))
    ax.set_xlabel("interval index")
    ax.set_ylabel("CV2")
    ax.legend()
    ax.set_title("C · temporal regularity (CV2)")

    # D: detrended IPI lag-1 scatter
    ax = axes[1, 1]
    ipi = np.diff(t)
    ipi = ipi[ipi > 0]
    if ipi.size >= 4:
        win = max(3, (ipi.size // 8) | 1)
        trend = ex._moving_median(ipi, win)
        resid = (ipi - trend) * 1e3
        ax.scatter(
            resid[:-1],
            resid[1:],
            s=8,
            color=CATEGORICAL[3],
            edgecolor="k",
            linewidths=0.3,
        )
    ax.axhline(0, color="grey", lw=0.5)
    ax.axvline(0, color="grey", lw=0.5)
    ax.set_xlabel("detrended IPI[i] (ms)")
    ax.set_ylabel("detrended IPI[i+1] (ms)")
    ax.set_title(f"D · serial structure (lag-1 r={scene.lag1_autocorr:+.2f})")

    # E: spatial amplitude-vector cluster scatter
    ax = axes[2, 0]
    allamp = np.vstack([amp_kept, amp_removed]) if amp_removed.size else amp_kept
    proj = _pca2(allamp)
    nk = amp_kept.shape[0]
    ax.scatter(
        proj[:nk, 0], proj[:nk, 1], s=8, color=CATEGORICAL[0], label="kept", alpha=0.7
    )
    if amp_removed.size:
        ax.scatter(
            proj[nk:, 0],
            proj[nk:, 1],
            s=30,
            color=CATEGORICAL[1],
            marker="x",
            label=f"purified out ({amp_removed.shape[0]})",
        )
    ax.set_xlabel("amp-vector PC1")
    ax.set_ylabel("amp-vector PC2")
    ax.legend()
    ax.set_title(
        f"E · spatial signature (dom {scene.spatial_dominant_frac:.2f}, sil {scene.spatial_silhouette:.2f})"
    )

    # F: text metrics
    ax = axes[2, 1]
    ax.axis("off")
    lines = [
        f"pulses (kept):        {scene.n_pulses}",
        f"purified out:         {np.asarray(scene.removed_rows).size}",
        f"duration:             {scene.t_end - scene.t_start:.2f} s",
        f"peak rate:            {scene.peak_rate_hz:.0f} Hz",
        f"gamma-shape score:    {scene.gamma_score:.2f}",
        "",
        "-- diagnostics (eel volleys are ms-irregular) --",
        f"CV2 mean/p95/max:     {scene.cv2_mean:.2f} / {scene.cv2_p95:.2f} / {scene.cv2_max:.2f}",
        f"lag-1 autocorr:       {scene.lag1_autocorr:+.2f}",
        "",
        "-- verdict cues --",
        f"2nd-cluster sil:      {scene.spatial_silhouette:.2f}",
        f"2nd-cluster frac:     {scene.spatial_minority_frac:.2f}",
        f"temporal interleave:  {scene.spatial_interleave:.2f}",
        f"concurrent tracks:    {scene.n_concurrent_tracks}",
        "",
        f"VERDICT:              {'SINGLE FISH' if scene.single_fish else 'REJECT'}",
    ]
    if not scene.single_fish:
        lines.append(f"reason:               {scene.reject_reason}")
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
    ax.set_title("F · single-fish verdict")

    pdf.savefig(fig)
    plt.close(fig)


def _page_localization(pdf, cfg, scenes, ipis) -> None:
    from fakefish.viz.plotstyle import full_page

    if not scenes:
        return
    n = len(scenes)
    fig, axes = full_page(height_cm=4.5 * n + 1.5, nrows=n, ncols=2, squeeze=False)
    fig.suptitle("LOCALIZATION trains (single-fish resting/exploring discharge)")
    for r, (scene, (label, ipi)) in enumerate(zip(scenes, ipis, strict=True)):
        t, _, _, _ = _scene_arrays(cfg, scene)
        rate, mid = ex._rate_profile(t)
        ax = axes[r, 0]
        ax.plot(mid - mid[0], rate, "-", color=CATEGORICAL[0], lw=0.8)
        ax.set_xlabel("time (s)")
        ax.set_ylabel("rate (Hz)")
        ax.set_title(
            f"{scene.site} track{scene.event_id}: {scene.n_pulses} pulses, "
            f"{scene.t_end - scene.t_start:.0f} s",
            fontsize=8,
        )
        ax = axes[r, 1]
        ipi_ms = np.asarray(ipi)[1:] / 1000.0
        ax.hist(ipi_ms, bins=40, color=CATEGORICAL[2], alpha=0.7)
        ax.set_xlabel("IPI (ms)")
        ax.set_ylabel("count")
        ax.set_title(
            f"IPI: cv2_p95 {scene.cv2_p95:.2f}, il {scene.spatial_interleave:.2f}",
            fontsize=8,
        )
    pdf.savefig(fig)
    plt.close(fig)


def render_qc(
    cfg, waveform, vol_scenes, loc_scenes, volleys, localizations
) -> Path:
    """Render the full multi-page QC PDF."""
    out = cfg.out_dir / "stimuli_qc.pdf"
    first_ipi = volleys[0][1] if volleys else np.array([0], dtype=np.uint32)
    with PdfPages(out) as pdf:
        _page_waveform(pdf, waveform, first_ipi)
        for scene, (label, ipi) in zip(vol_scenes, volleys, strict=True):
            _page_volley(pdf, cfg, scene, ipi)
        _page_localization(pdf, cfg, loc_scenes, localizations)
    return out
