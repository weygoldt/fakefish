"""Package-wide figure convention — the single enforcement point.

Every figure in this project (analysis plots, slide-deck
figures, docs) must obey one convention so they drop into a paper or the
ICN slide deck without re-touching size or type:

- **Two widths only.** ``HALF_WIDTH_CM`` (8.5 cm, a journal single
  column) and ``FULL_WIDTH_CM`` (17 cm, text / double-column width).
  Height is free. Build figures with :func:`half_page`, :func:`full_page`,
  or :func:`figure` — never hand-pass ``figsize=`` at a call site.
- **300 dpi** on every saved figure (set in rcParams *and* in the save
  helpers in :mod:`fakefish.viz.figsave`).
- **Uniform font sizes per annotation class** — ticks 8, axis labels 9,
  legend 8, axes title 10, suptitle 11 — injected by :func:`set_base_style`
  so they apply the moment a style is set.
- **Deck colour scheme** — sequential ``viridis`` / ``magma``, a fixed
  6-colour mako/crest/flare categorical cycle (:data:`CATEGORICAL`), and a
  mako brightness ramp for identity (:func:`identity_colors`). No
  jet/turbo/rainbow.

The convention is enforced two ways: the constructors here snap any
requested width to half/full and apply the style, and
``tests/test_figure_convention.py`` fails CI if any figure module sets
``figsize=`` / ``dpi=`` itself instead of going through this module.
"""

import colorsys
from typing import Literal

import matplotlib.colors as mc
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
from matplotlib.colors import LinearSegmentedColormap, ListedColormap

cm = 1 / 2.54
mm = 1 / 25.4

# --------------------------------------------------------------------------
# Canonical figure convention (the two blessed widths + resolution + fonts)
# --------------------------------------------------------------------------

#: The only two figure widths allowed in the package. Height stays free.
HALF_WIDTH_CM = 8.5
FULL_WIDTH_CM = 17.0

#: Every saved figure is written at this resolution.
FIGURE_DPI = 300

#: Width keyword accepted by the figure constructors.
Width = Literal["half", "full"]

#: Font size (pt) per annotation class — uniform across the whole package.
FONT_SIZES: dict[str, float] = {
    "font.size": 8,          # base / generic text + annotations
    "axes.titlesize": 10,    # per-axes (panel) title
    "axes.labelsize": 9,     # x / y axis labels
    "xtick.labelsize": 8,    # tick labels
    "ytick.labelsize": 8,
    "legend.fontsize": 8,    # legend entries
    "legend.title_fontsize": 9,
    "figure.titlesize": 11,  # suptitle
}

#: Fixed 6-colour qualitative cycle drawn from the deck's mako/crest/flare
#: families (cool blues/teals with warm pink/purple accents). Used as the
#: categorical ``axes.prop_cycle`` so every figure cycles the same hues.
CATEGORICAL: list[str] = [
    "#366ca0",  # mako blue
    "#cc4663",  # flare red
    "#33858d",  # crest teal
    "#85d9b1",  # mako light green
    "#923371",  # flare purple
    "#413f80",  # mako indigo
]


def width_cm(width: "Width | float") -> float:
    """Resolve a width spec to centimetres.

    ``"half"`` / ``"full"`` map to :data:`HALF_WIDTH_CM` / :data:`FULL_WIDTH_CM`;
    a number is returned as-is (escape hatch for the rare oversized
    diagnostic, but call sites should prefer the two named widths).
    """
    if isinstance(width, (int, float)):
        return float(width)
    try:
        return {"half": HALF_WIDTH_CM, "full": FULL_WIDTH_CM}[width]
    except KeyError:
        raise ValueError(
            f"width must be 'half', 'full', or a number in cm; got {width!r}"
        ) from None


def snap_width(total_width_cm: float) -> float:
    """Snap an arbitrary requested width to the nearer of half / full.

    Lets the legacy ``figure(nrows, ncols, panel_size_cm=...)`` callers
    keep working while still landing on one of the two blessed widths.
    """
    midpoint = 0.5 * (HALF_WIDTH_CM + FULL_WIDTH_CM)
    return FULL_WIDTH_CM if total_width_cm >= midpoint else HALF_WIDTH_CM


def identity_colors(n: int) -> list[tuple[float, float, float]]:
    """``n`` colours along the mako brightness ramp for identity encoding.

    Monochromatic-by-brightness, never rainbow — the agreed way to encode
    identity / track id so it doesn't compete with the data colour scale.
    """
    if n <= 0:
        return []
    cmap = sns.color_palette("mako", as_cmap=True)
    if n == 1:
        return [cmap(0.55)[:3]]
    return [cmap(v)[:3] for v in np.linspace(0.15, 0.9, n)]


def adjust_alpha(color, amount=0.5):
    """
    Lightens (amount < 1) or darkens (amount > 1) the given color.

    Parameters
    ----------
    color : str or tuple
        Matplotlib color name or a tuple (r, g, b).
    amount : float
        Factor by which to adjust the color’s lightness.
        - Values between 0 and 1 will lighten the color
          (0 returns white).
        - Values > 1 will darken the color.

    Returns
    -------
    new_color : tuple
        The adjusted color as an RGB tuple.
    """
    try:
        # If 'color' is a named color like 'red', convert to hex
        c = mc.cnames[color]
    except KeyError:
        # Otherwise, assume it's already a valid hex or RGB
        c = color

    # Convert to RGB in [0, 1], then to HLS
    r, g, b = mc.to_rgb(c)
    h, lgt, s = colorsys.rgb_to_hls(r, g, b)

    # Adjust lightness
    # if amount < 1, we move lgt closer to 1 (white) -> lighten
    # if amount > 1, we move lgt closer to 0 (black) -> darken
    new_l = lgt + (1 - lgt) * (1 - amount) if amount < 1 else lgt * (2 - amount)

    # Convert back to RGB
    new_r, new_g, new_b = colorsys.hls_to_rgb(h, new_l, s)
    return (new_r, new_g, new_b)


def set_base_style():
    plt.rcParams.update(
        {
            # Inter is the deck font — make every figure match it. Noto Sans /
            # DejaVu Sans are fallbacks for machines where Inter isn't installed.
            "font.family": "sans-serif",
            "font.sans-serif": ["Inter", "Noto Sans", "DejaVu Sans"],
            # "text.usetex": True,
            # "text.latex.preamble": r"\usepackage{wasysym} \usepackage{siunitx} \sisetup{detect-all} \usepackage{sansmath} \sansmath \usepackage[sfdefault]{noto} \usepackage[T1]{fontenc}",
            "axes.titlelocation": "left",
            "axes.spines.left": True,
            "axes.spines.bottom": True,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "legend.frameon": False,
            "image.origin": "lower",
            "savefig.pad_inches": 0.0,
            # Resolution — every saved figure at 300 dpi (the package rule).
            "figure.dpi": FIGURE_DPI,
            "savefig.dpi": FIGURE_DPI,
            # Line / marker weights tuned for centimetre-sized print figures.
            "lines.linewidth": 1.0,
            "lines.markersize": 4.0,
            "axes.linewidth": 0.8,
            "xtick.major.width": 0.8,
            "ytick.major.width": 0.8,
            "grid.linewidth": 0.6,
        }
    )
    # Uniform font sizes per annotation class (the package rule). Applied
    # last so they win over any "medium"/"large" string sizes above.
    plt.rcParams.update(FONT_SIZES)


def set_light_style():
    set_base_style()
    plt.rcParams.update(
        {
            "lines.markeredgecolor": "k",
            "lines.markeredgewidth": 0.5,
            "patch.facecolor": "#000000",
            "patch.edgecolor": "#000000",
            "boxplot.flierprops.color": "#000000",
            "boxplot.flierprops.markeredgecolor": "#000000",
            "boxplot.boxprops.color": "#000000",
            "boxplot.whiskerprops.color": "#000000",
            "boxplot.capprops.color": "#000000",
            "boxplot.medianprops.color": "#000000",
            "boxplot.meanprops.color": "#000000",
            "boxplot.meanprops.markerfacecolor": "#000000",
            "boxplot.meanprops.markeredgecolor": "#000000",
            "text.color": "#000000",
            "axes.facecolor": "#FFFFFF",
            "axes.edgecolor": "#000000",
            "axes.labelcolor": "#000000",
            "axes.prop_cycle": plt.cycler("color", CATEGORICAL),
            "xtick.color": "#000000",
            "ytick.color": "#000000",
            "grid.color": "#7f7f7f",
            "figure.facecolor": "#FFFFFF",
            "figure.edgecolor": "#000000",
            "image.cmap": "viridis",
        }
    )


def set_dark_style():
    set_base_style()
    plt.rcParams.update(
        {
            "patch.facecolor": "#FFFFFF",
            "patch.edgecolor": "#FFFFFF",
            "boxplot.flierprops.color": "#FFFFFF",
            "boxplot.flierprops.markeredgecolor": "#FFFFFF",
            "boxplot.boxprops.color": "#FFFFFF",
            "boxplot.whiskerprops.color": "#FFFFFF",
            "boxplot.capprops.color": "#FFFFFF",
            "boxplot.medianprops.color": "#FFFFFF",
            "boxplot.meanprops.color": "#FFFFFF",
            "boxplot.meanprops.markerfacecolor": "#FFFFFF",
            "boxplot.meanprops.markeredgecolor": "#FFFFFF",
            "text.color": "#FFFFFF",
            "axes.facecolor": "#000000",
            "axes.edgecolor": "#FFFFFF",
            "axes.labelcolor": "#FFFFFF",
            "axes.prop_cycle": plt.cycler("color", CATEGORICAL),
            "xtick.color": "#FFFFFF",
            "ytick.color": "#FFFFFF",
            "grid.color": "#7f7f7f",
            "figure.facecolor": "#000000",
            "figure.edgecolor": "#FFFFFF",
            "image.cmap": "magma",
        }
    )


def apply_style(style: str = "light") -> None:
    """Apply one of the two package styles (``"light"`` / ``"dark"``)."""
    if style == "dark":
        set_dark_style()
    else:
        set_light_style()


# --------------------------------------------------------------------------
# Figure constructors — the only blessed way to make a figure
# --------------------------------------------------------------------------


def figure(
    width: "Width | float" = "full",
    nrows: int = 1,
    ncols: int = 1,
    *,
    height_cm: float | None = None,
    row_height_cm: float | None = None,
    panel_size_cm: tuple[float, float] | None = None,
    aspect: float = 0.62,
    style: str = "light",
    sharex: bool = False,
    sharey: bool = False,
    squeeze: bool = True,
    constrained_layout: bool = True,
    **subplots_kw,
):
    """Create a half- or full-page figure with the package style applied.

    This is the single entry point every figure should use instead of
    ``plt.subplots(figsize=...)``. The **width is always one of the two
    blessed values** (8.5 cm half / 17 cm full); height is free.

    Parameters
    ----------
    width
        ``"half"``, ``"full"``, or a width in cm (snapped to the nearer
        blessed width unless it equals one exactly).
    nrows, ncols
        Subplot grid.
    height_cm
        Total figure height in cm. Takes precedence over ``row_height_cm``
        and ``aspect``.
    row_height_cm
        Per-row height in cm; total height = ``nrows * row_height_cm``.
    panel_size_cm
        ``(panel_w, panel_h)`` — legacy compatibility shim. The requested
        total width ``ncols * panel_w`` is **snapped** to half/full, and the
        total height becomes ``nrows * panel_h``. Prefer ``width`` +
        ``row_height_cm`` in new code.
    aspect
        Used only when neither ``height_cm`` nor ``row_height_cm`` nor
        ``panel_size_cm`` is given: each panel's height = panel_width *
        ``aspect``.
    style
        ``"light"`` (default) or ``"dark"``.

    squeeze
        Like ``plt.subplots``: when ``True`` (default) a single axes is
        returned bare and a 1-D grid as a 1-D array — a drop-in for
        ``plt.subplots``. When ``False`` a 2-D ``ndarray`` is always
        returned (what the legacy inspect ``figure`` relies on).

    Returns
    -------
    (fig, axes)
    """
    apply_style(style)

    if panel_size_cm is not None:
        total_w_cm = snap_width(ncols * panel_size_cm[0])
        total_h_cm = nrows * panel_size_cm[1]
    else:
        total_w_cm = width_cm(width)
        if height_cm is not None:
            total_h_cm = height_cm
        elif row_height_cm is not None:
            total_h_cm = nrows * row_height_cm
        else:
            panel_w = total_w_cm / ncols
            total_h_cm = nrows * panel_w * aspect

    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(total_w_cm * cm, total_h_cm * cm),
        sharex=sharex,
        sharey=sharey,
        squeeze=squeeze,
        constrained_layout=constrained_layout,
        **subplots_kw,
    )
    if not squeeze:
        axes = np.atleast_2d(axes)
    return fig, axes


def half_page(
    height_cm: float = 6.0,
    nrows: int = 1,
    ncols: int = 1,
    *,
    style: str = "light",
    sharex: bool = False,
    sharey: bool = False,
    squeeze: bool = True,
    **subplots_kw,
):
    """An 8.5 cm-wide figure (journal single column). Height in cm.

    Returns ``(fig, axes)`` exactly like ``plt.subplots`` (squeezed) — a
    drop-in replacement, just without a ``figsize``.
    """
    return figure(
        "half", nrows, ncols, height_cm=height_cm, style=style,
        sharex=sharex, sharey=sharey, squeeze=squeeze, **subplots_kw,
    )


def full_page(
    height_cm: float = 8.0,
    nrows: int = 1,
    ncols: int = 1,
    *,
    style: str = "light",
    sharex: bool = False,
    sharey: bool = False,
    squeeze: bool = True,
    **subplots_kw,
):
    """A 17 cm-wide figure (text / double-column width). Height in cm.

    Returns ``(fig, axes)`` exactly like ``plt.subplots`` (squeezed) — a
    drop-in replacement, just without a ``figsize``.
    """
    return figure(
        "full", nrows, ncols, height_cm=height_cm, style=style,
        sharex=sharex, sharey=sharey, squeeze=squeeze, **subplots_kw,
    )


def mosaic(
    mosaic_spec,
    *,
    width: "Width | float" = "full",
    height_cm: float = 10.0,
    style: str = "light",
    constrained_layout: bool = True,
    **kw,
):
    """``plt.subplot_mosaic`` at a blessed width with the package style."""
    apply_style(style)
    total_w_cm = width_cm(width)
    fig, axd = plt.subplot_mosaic(
        mosaic_spec,
        figsize=(total_w_cm * cm, height_cm * cm),
        constrained_layout=constrained_layout,
        **kw,
    )
    return fig, axd


def blank_figure(
    width: "Width | float" = "full",
    height_cm: float = 8.0,
    *,
    style: str = "light",
    **kw,
):
    """A bare ``plt.figure`` at a blessed width (for GridSpec / SubFigures)."""
    apply_style(style)
    total_w_cm = width_cm(width)
    return plt.figure(figsize=(total_w_cm * cm, height_cm * cm), **kw)


def get_ylims(cls, ydata):
    allfunds_tmp = np.concatenate(ydata).ravel().tolist()
    lower = np.min(allfunds_tmp)
    upper = np.max(allfunds_tmp)
    return lower, upper


def transparent_fade_colormap(cmap):
    my_cmap = cmap(np.arange(cmap.N))
    my_cmap[:, -1] = np.linspace(0, 1, cmap.N)
    my_cmap = ListedColormap(my_cmap)
    return my_cmap


def truncate_colormap(cmap, minval=0.0, maxval=1.0, n=100):
    new_cmap = LinearSegmentedColormap.from_list(
        f"trunc({cmap.name},{minval:.2f},{maxval:.2f})",
        cmap(np.linspace(minval, maxval, n)),
    )
    return new_cmap


def hide_ax(ax):
    ax.xaxis.set_visible(False)
    plt.setp(ax.spines.values(), visible=False)
    ax.tick_params(left=False, labelleft=False)
    ax.patch.set_visible(False)


def hide_xax(ax):
    ax.xaxis.set_visible(False)
    ax.spines["bottom"].set_visible(False)


def hide_yax(ax):
    ax.yaxis.set_visible(False)
    ax.spines["left"].set_visible(False)


def set_boxplot_color(bp, color):
    plt.setp(bp["boxes"], color=color)
    plt.setp(bp["whiskers"], color=color)
    plt.setp(bp["caps"], color=color)
    plt.setp(bp["medians"], color="black")


def circle_annotate(ax, xy, xy_adjust_text, text):
    xy = np.array(xy)
    xy_adjust_text = np.array(xy_adjust_text)
    ax.text(
        *xy,
        " ",
        ha="center",
        va="center",
        color="black",
        zorder=1100,
        transform=ax.transData,
        clip_on=False,
        bbox=dict(
            boxstyle="circle,pad=0.1",
            fc="w",
            ec="k",
            lw=0.5,
            alpha=1,
        ),
        fontsize="small",
    )

    xy_text = xy + xy_adjust_text
    ax.text(
        *xy_text,
        text,
        ha="center",
        va="center_baseline",
        color="black",
        zorder=1101,
        transform=ax.transData,
        clip_on=False,
        fontsize="small",
    )


def letter_subplots(
    axes=None,
    letters=None,
    xoffset=-0.1,
    yoffset=1.0,
    **kwargs,
):
    """Add letters to the corners of subplots (panels). By default each axis is
    given an uppercase bold letter label placed in the upper-left corner.
    Args
        axes : list of pyplot ax objects. default plt.gcf().axes.
        letters : list of strings to use as labels, default ["A", "B", "C", ...]
        xoffset, yoffset : positions of each label relative to plot frame
        (default -0.1,1.0 = upper left margin). Can also be a list of
        offsets, in which case it should be the same length as the number of
        axes.
        Other keyword arguments will be passed to annotate() when panel letters
        are added.

    Returns
    -------
        list of strings for each label added to the axes
    Examples:
        Defaults:
            >>> fig, axes = plt.subplots(1,3)
            >>> letter_subplots() # boldfaced A, B, C

        Common labeling schemes inferred from the first letter:
            >>> fig, axes = plt.subplots(1,4)
            # panels labeled (a), (b), (c), (d)
            >>> letter_subplots(letters='(a)')
        Fully custom lettering:
            >>> fig, axes = plt.subplots(2,1)
            >>> letter_subplots(axes, letters=['(a.1)', '(b.2)'], fontweight='normal')
        Per-axis offsets:
            >>> fig, axes = plt.subplots(1,2)
            >>> letter_subplots(axes, xoffset=[-0.1, -0.15])

        Matrix of axes:
            >>> fig, axes = plt.subplots(2,2, sharex=True, sharey=True)
            # fig.axes is a list when axes is a 2x2 matrix
            >>> letter_subplots(fig.axes)
    """
    # get axes:
    if axes is None:
        axes = plt.gcf().axes
    # handle single axes:
    try:
        iter(axes)
    except TypeError:
        axes = [axes]

    # set up letter defaults (and corresponding fontweight):
    fontweight = "bold"
    ulets = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"[: len(axes)])
    llets = list("abcdefghijklmnopqrstuvwxyz"[: len(axes)])
    if letters is None or letters == "A":
        letters = ulets
    elif letters == "(a)":
        letters = [f"({lett})" for lett in llets]
        fontweight = "normal"
    elif letters == "(A)":
        letters = [f"({lett})" for lett in ulets]
        fontweight = "normal"
    elif letters in ("lower", "lowercase", "a"):
        letters = llets

    # make sure there are x and y offsets for each ax in axes:
    if isinstance(xoffset, (int, float)):
        xoffset = [xoffset] * len(axes)
    else:
        assert len(xoffset) == len(axes)
    if isinstance(yoffset, (int, float)):
        yoffset = [yoffset] * len(axes)
    else:
        assert len(yoffset) == len(axes)

    # defaults for annotate (kwargs is second so it can overwrite these defaults):
    my_defaults = dict(
        fontweight=fontweight,
        fontsize="large",
        ha="center",
        va="center",
        xycoords="axes fraction",
        annotation_clip=False,
    )
    kwargs = dict(list(my_defaults.items()) + list(kwargs.items()))

    list_txts = []
    for ax, lbl, xoff, yoff in zip(axes, letters, xoffset, yoffset, strict=False):
        t = ax.annotate(lbl, xy=(xoff, yoff), **kwargs)
        list_txts.append(t)

    return list_txts


def float_rgb_to_hex(rgb):
    return f"#{int(rgb[0] * 255):02x}{int(rgb[1] * 255):02x}{int(rgb[2] * 255):02x}"


def show_palette(colors):
    n_colors = len(colors)
    plt.figure(figsize=(n_colors, 1))
    plt.imshow([colors], extent=[0, n_colors, 0, 1], aspect="auto")
    plt.axis("off")
    plt.show()


def example_plot():
    figsize = (16 * cm, 8 * cm)
    fig, ax = plt.subplots(figsize=figsize)
    ax.plot([0, 1], [0, 1])
    ax.scatter([0, 1], [0, 1], color="r", s=10)
    circle_annotate(ax, (0.5, 0.5), (0, 0), "1")
    letter_subplots(ax)
    ax.set_title("Example Plot")
    plt.show()


def main():
    set_light_style()
    example_plot()
    set_dark_style()
    example_plot()


loser_color = adjust_alpha("#236477", 0.85)
winner_color = adjust_alpha("#4a6c2f", 0.85)
baseline_color = "grey"
other_color = "#595EC5"
# loser_color = "#2C828C"
# winner_color = "#4F9260"

cmap1_ = sns.color_palette("crest_r", as_cmap=True)
cmap2_ = sns.color_palette("flare", as_cmap=True)
loser_color = cmap1_(0.5)
winner_color = adjust_alpha(cmap1_(0.9), 1.2)
other_color = cmap1_(0.05)


if __name__ == "__main__":
    main()
    print(f"Winner color: {float_rgb_to_hex(winner_color)}")
    print(f"Loser color: {float_rgb_to_hex(loser_color)}")
