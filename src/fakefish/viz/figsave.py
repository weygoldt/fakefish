"""Foundation-layer figure save helpers — the one save path.

Every figure in the package is written through this module so output
resolution, format, and directory creation are solved once. The figure
*construction* convention (the two blessed widths, fonts, colours) lives
in :mod:`fakefish.viz.plotstyle`; the constructors and constants are
re-exported here so a single import gives a call site everything it needs::

    from fakefish.viz.figsave import full_page, save_figure
    fig, axes = full_page(height_cm=6.0, ncols=2)
    ...
    save_figure(fig, out_dir / "my_figure")

Resolution
----------
Every *saved figure* is 300 dpi (the package rule); the legacy tier names
are kept as aliases so old imports resolve, but they all point at 300:

- ``PUBLICATION_DPI`` / ``DIAGNOSTIC_DPI`` (300): all saved figures.
- ``ANIMATION_DPI`` (150): per-frame rendering for *video* animations —
  the one documented exception (a video frame is sized in pixels, not a
  print figure), passed only to ``FuncAnimation.save(dpi=...)``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import numpy as np

from fakefish.viz import plotstyle as _ps
from fakefish.viz.plotstyle import (  # noqa: F401  (re-export surface)
    CATEGORICAL,
    FIGURE_DPI,
    FULL_WIDTH_CM,
    HALF_WIDTH_CM,
    blank_figure,
    cm,
    full_page,
    half_page,
    identity_colors,
    letter_subplots,
    mosaic,
    set_dark_style,
    set_light_style,
    width_cm,
)


# ---------------------------------------------------------------------------
# Resolution — all saved figures at 300 dpi; tier names kept as aliases.
# ---------------------------------------------------------------------------

PUBLICATION_DPI = FIGURE_DPI   # 300
DIAGNOSTIC_DPI = FIGURE_DPI    # 300 (was 150 — unified per the figure rule)
ANIMATION_DPI = 150            # video frames only (see module docstring)


# ---------------------------------------------------------------------------
# Figure scaffolding
# ---------------------------------------------------------------------------


def figure(
    nrows: int,
    ncols: int,
    *,
    panel_size_cm: tuple[float, float] = (6.0, 4.5),
    sharex: bool = False,
    sharey: bool = False,
    style: str = "light",
) -> tuple[plt.Figure, np.ndarray]:
    """Legacy ``(nrows × ncols)`` grid sized by per-panel cm — width-enforced.

    Kept with the historical signature so the ~40 inspect call sites of the
    form ``figure(2, 3, panel_size_cm=(9, 7))`` need no edit. The requested
    total width ``ncols * panel_w`` is **snapped** to the nearer blessed
    width (8.5 / 17 cm) by :func:`fakefish.viz.plotstyle.figure`, and the
    package style (fonts, 300 dpi, colours) is applied. New code should
    prefer :func:`half_page` / :func:`full_page`.
    """
    return _ps.figure(
        nrows=nrows,
        ncols=ncols,
        panel_size_cm=panel_size_cm,
        sharex=sharex,
        sharey=sharey,
        style=style,
        squeeze=False,
    )


def save_figure(
    fig: plt.Figure,
    output_path: Path,
    *,
    fmt: str = "png",
) -> Path:
    """Save a figure verbatim and close it (no panel letters)."""
    suffix = ".pdf" if fmt == "pdf" else ".png"
    out = output_path.with_suffix(suffix)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=DIAGNOSTIC_DPI)
    plt.close(fig)
    return out


def save_publication_panel(
    fig: plt.Figure,
    output_path: Path,
    *,
    fmt: str = "png",
    dpi: int = FIGURE_DPI,
) -> Path:
    """Save a single-panel figure (no panel letter). Kept for back-compat;
    identical to :func:`save_figure` now that every save is 300 dpi."""
    suffix = ".pdf" if fmt == "pdf" else ".png"
    out = output_path.with_suffix(suffix)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=dpi)
    plt.close(fig)
    return out


def save_with_letters(
    fig: plt.Figure,
    axes: Iterable[plt.Axes],
    output_path: Path,
    *,
    fmt: str = "png",
    letters: bool = True,
) -> Path:
    """Add panel letters, save to disk in the requested format, close the figure.

    Letters sit at axes-fraction (-0.05, 1.08) so they don't overlap axis
    titles. ``bbox_inches="tight"`` is *not* used: it strips the padding
    that ``constrained_layout`` reserved for ``suptitle``.
    """
    visible = [ax for ax in axes if ax.get_visible()]
    if letters and visible:
        letter_subplots(visible, xoffset=-0.05, yoffset=1.08)
    suffix = ".pdf" if fmt == "pdf" else ".png"
    out = output_path.with_suffix(suffix)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=DIAGNOSTIC_DPI)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# Figure naming
# ---------------------------------------------------------------------------


def default_figure_name(module_file: str) -> str:
    """Derive a stable figure stem from a module's ``__file__``.

    The agreed rule is ``<leaf_subpackage>_<module>``: take the module
    file's stem and its parent directory name (the leaf subpackage). A
    package ``__init__.py`` collapses to just the leaf name. Informative,
    per-figure renames come in later phases — this is the default.

    >>> default_figure_name("/a/b/circadian/__init__.py")
    'circadian'
    >>> default_figure_name("/a/migration/scripts/q3_polarization.py")
    'scripts_q3_polarization'
    """
    stem = Path(module_file).stem
    leaf = Path(module_file).parent.name
    return leaf if stem == "__init__" else f"{leaf}_{stem}"


# ---------------------------------------------------------------------------
# Blessed save alias
# ---------------------------------------------------------------------------

#: Preferred name for the one-line save. Identical to :func:`save_figure`
#: (writes at 300 dpi, makes parent dirs, closes the figure). New code can
#: ``from fakefish.viz.figsave import full_page, save``.
save = save_figure


def save_svg_and_png(fig: plt.Figure, output_path: Path) -> tuple[Path, Path]:
    """Write both an SVG (vector, for slides) and a 300-dpi PNG.

    Slide-deck figures generally want the SVG; the PNG is a raster
    fallback. Both land next to each other; the figure is closed once.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    svg = out.with_suffix(".svg")
    png = out.with_suffix(".png")
    fig.savefig(svg)
    fig.savefig(png, dpi=FIGURE_DPI)
    plt.close(fig)
    return svg, png
