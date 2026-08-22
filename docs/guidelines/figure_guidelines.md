<!--
VENDORED from eeltracker (`docs/guidelines/figure_guidelines.md`), where the deck
figure system was written and where `tests/test_figure_convention.py` is its
regression test. Copied here on 2026-08-22 with only the module paths rewritten
(`eeltracker.utils` -> `fakefish.viz`); `src/fakefish/viz/{plotstyle,figsave,loggers}.py`
are the matching byte-level copies of the modules it describes.

A copy has no merge base, so DO NOT edit this in place — change it upstream and
re-drop, the same discipline invariants 10 and 11 apply to the volley and
localization models.
-->

# Principles for Award-Winning Scientific Data Visualizations

## Start from the question, not the data

**Know what claim the figure is making.** Every good figure answers one specific question ("does treatment affect outcome?", "how does X scale with Y?", "where in parameter space does the regime change?"). If you can't state the question in one sentence, the figure isn't ready. Figures that try to show "the data" without a question end up as dashboards — informative in aggregate, persuasive about nothing.

**Choose the chart type from the question, not from habit.** Comparison across categories → dot plot or ordered bar. Distribution → histogram, density, or strip plot. Relationship between two continuous variables → scatter, with a fit only if the fit is the point. Change over time → line. Composition → stacked bar or, rarely, a treemap. Pie charts are almost never the right answer.

**One figure, one message.** If the figure supports two claims, it's probably two figures. The exception is a small-multiples grid where the repeated structure *is* the message.

## Respect the data

**Show the data, not just the summary.** Bar charts with error bars hide everything interesting — distribution shape, outliers, sample size, bimodality. Prefer dot plots, strip plots, jittered points, or box-plus-points for small to moderate N; violins or ridgeline plots for larger N. The "bar-and-whisker" default has probably caused more wrong inferences in biology than any other convention.

**Make uncertainty visible and honest.** Every quantitative claim gets error bars, confidence bands, or a posterior interval, with the caption stating exactly what they represent (SD, SEM, 95% CI, 90% credible interval). These are not interchangeable, and conflating them is a giveaway that the analysis is shallow.

**Don't truncate axes to manufacture an effect, and don't extend them to hide one.** Y-axes for ratios and proportions generally start at zero; axes for differences and changes generally don't. The right choice depends on what comparison the reader is being invited to make — be deliberate, and be ready to defend it.

**Match the transformation to the data's structure.** Log scales for data spanning orders of magnitude; rank or quantile transforms when the absolute values mislead; centered/standardized scales for comparing effects across measurements with different units. A linear axis on a log-distributed variable is a choice that hides structure.

## Direct the eye

**Establish a clear visual hierarchy.** The most important element should be the most visually salient — through size, contrast, color, or position. Everything else recedes. A plot where every element competes for attention is a plot where nothing wins.

**Use color with intent, not decoration.** A small palette (two or three data colors plus a neutral) used consistently beats a rainbow. Reserve one high-contrast accent for the single thing you want the reader to notice. Use perceptually uniform colormaps (viridis, cividis, magma) for continuous data — never jet or rainbow, which introduce false boundaries and fail in grayscale and for colorblind readers. Diverging colormaps only for diverging data (around a meaningful midpoint).

**Annotate directly on the plot.** Labels next to the curves they describe; arrows pointing at the feature you're discussing; a short text annotation calling out the key value. Legends are friction — the reader's eye has to bounce between legend and plot, losing the comparison each time. Direct labels are free for the reader.

**Minimize non-data ink.** Drop gridlines unless they aid reading. Remove the top and right spines. Kill chart titles inside the figure if a caption or slide title carries the message. Tick marks should be few and informative, not dense and decorative. Every pixel earns its place.

## Encode quantitatively

**Use the strongest perceptual channel for the most important comparison.** Position on a common scale is the most accurate channel humans have, followed by length, then angle, then area, then color hue. Encode the comparison you most want the reader to make precisely using position — which is why dot plots beat bar charts beat pies for category comparisons, and why bubble charts struggle (area is a weak channel).

**Don't encode the same variable twice unless it actually helps.** Mapping a category to both color and shape is sometimes useful for accessibility; mapping the same continuous value to both x-position and color is usually noise.

**Order categorical axes meaningfully.** Sort by the value being plotted, by a substantive ordering (severity, time, dose), or by a clustering — almost never alphabetically. Alphabetical order is a refusal to make a design decision.

## Small multiples and composition

**Prefer small multiples to overplotted single panels.** If six conditions overlap in a tangle on one axis, six small panels with shared scales will almost always read better. The eye is excellent at scanning a grid; it's poor at disentangling a snarl. Shared axes across panels make cross-panel comparison trivial.

**Within multi-panel figures, enforce alignment and consistency.** Same axis ranges where comparison matters, same color encoding across panels, aligned axes so the reader can scan vertically or horizontally without realigning. Visual rhyme between panels is what makes a complex figure feel like one thing rather than five things stapled together.

**Layout encodes meaning.** Panel order should follow the argument — left to right, top to bottom, the way the reader will follow. Labels (A, B, C) help only if the order they imply matches the reading order.

## Typography and labels

**Axis labels are sentences, not codes.** "Tumor volume (mm³)" not "vol". "Time after infection (days)" not "t". The reader shouldn't need a key to understand the axes.

**Units always, and consistent across the figure.** If one panel uses µm and another uses nm, you've added cognitive load for no reason.

**Caption carries the message, axes carry the measurements.** A strong caption states what the figure shows and what to conclude ("Treatment reduces volume by 40% across all dose levels (n=12 per group, error bars 95% CI)") — not just "Tumor volume across conditions." For talks, the slide title does this job; for papers, the caption does.

## Reproducibility and rigor

**Generate figures from code, not by hand.** Every figure should be regenerable from raw data with a single command. Hand-tweaked figures break the moment a data point changes, and they hide decisions that should be inspectable. Use matplotlib, ggplot2, plotnine, vega-lite, or similar — whichever, but versioned.

**Keep raw and processed data separate from plotting code.** The pipeline should be: raw data → cleaning → analysis → plotting, each stage inspectable. A figure script that reaches back into raw CSVs and does cleaning inline is a figure that can't be audited.

**Build the figure at final size.** A plot designed at full-screen and shrunk to a column will have unreadable text and overlapping points. Set the figure dimensions in the plotting code to match the final use (presentation slide, paper column, full page), and design at that size from the start.

**Export as vector (SVG, PDF) for line art and small-N plots; raster (PNG at high DPI) only when necessary** (huge scatter plots, heatmaps, anything with millions of elements). Vector survives zoom; raster doesn't.

## The package figure convention (enforced)

Every figure in fakefish — the galleries, analysis plots, slide
figures, docs — obeys one convention so it drops into a paper or the
ICN deck without re-touching size or type. It is **enforced** by
`tests/test_figure_convention.py`, which fails if any figure module sets
its own `figsize=` or a non-300 `dpi=`. The single source of truth is
`fakefish.viz.plotstyle`.

**Two widths only. Never pass `figsize=`.** Build every figure with a
constructor — width is always one of two blessed values (half = 8.5 cm,
a journal single column; full = 17 cm, text / double-column width).
Height is free.

```
from fakefish.viz.plotstyle import full_page, half_page
from fakefish.viz.figsave import save_figure

fig, axes = full_page(height_cm=6.0, nrows=1, ncols=3)   # 17 cm wide
# ... plot ...
save_figure(fig, out_dir / "my_figure")                  # 300 dpi, mkdir, close
```

`full_page` / `half_page` are **drop-in for `plt.subplots`**: they
return `(fig, axes)` squeezed exactly like it (bare Axes for 1x1, 1-D
for a single row/col, 2-D otherwise) — just without a `figsize`. Other
constructors: `figure(width, nrows, ncols, height_cm=...)` (general),
`mosaic(spec, width=, height_cm=)` (for `subplot_mosaic`),
`blank_figure(width=, height_cm=)` (a bare `plt.figure` for GridSpec /
SubFigures). The legacy `figsave.figure(nrows, ncols, panel_size_cm=...)`
still works — it snaps the requested width to half/full.

**Uniform fonts and colours come for free.** The constructors apply the
package style, so every figure uses the same font — **Inter** (the slide
deck's font; falls back to Noto Sans / DejaVu Sans where Inter isn't
installed) — at the same per-class sizes (ticks 8, axis labels 9, legend
8, axes title 10, suptitle 11 pt) and the deck colour scheme: sequential
`viridis` / `magma`, a fixed mako/crest/flare categorical `prop_cycle`
(`plotstyle.CATEGORICAL`), and a mako brightness ramp for identity
(`plotstyle.identity_colors(n)`). Never jet / turbo / rainbow.

## Saving figures in fakefish

**One shared save path.** All figures are written through
`fakefish.viz.figsave`. Don't re-implement `savefig` + `mkdir` +
`close` per module; import the helper (`save` is an alias of
`save_figure`):

```
from fakefish.viz.figsave import save_figure, save_with_letters
```

`fakefish.viz.figsave` re-exports the constructors alongside the savers
(`figure`, `full_page`, `half_page`, `mosaic`, `save`, `save_figure`,
`save_publication_panel`, `save_with_letters`), so one import gives a call
site everything it needs.

**Every saved figure is 300 dpi.** The tier names survive only as
aliases — they all point at 300 now:

```
PUBLICATION_DPI = DIAGNOSTIC_DPI = 300   # all saved figures
ANIMATION_DPI   = 150                    # video frames only (see below)
```

`save_figure`, `save_with_letters`, and `save_publication_panel` all
write at 300. The one exception is **video animations**: a frame is
sized in pixels, not as a print figure, so `FuncAnimation.save(dpi=...)`
may use `ANIMATION_DPI`. That is the only place a non-300 `dpi=` is
allowed, and only via the named constant.

**Naming: one module, one figure.** The default figure stem is
`<leaf_subpackage>_<module>` — the module file's parent directory name
plus its own stem (a package `__init__.py` collapses to just the leaf).
`figsave.default_figure_name(__file__)` computes it:

```
default_figure_name(".../circadian/__init__.py")        -> "circadian"
default_figure_name(".../scripts/q3_polarization.py")   -> "scripts_q3_polarization"
```

Use this as the default; rename to something more informative per-figure
when one module emits several distinct figures.

**`bbox_inches="tight"` is opt-in, not default.** The save helpers do not
pass it, because it strips the padding `constrained_layout` reserves for
`suptitle` and panel letters. Only add it deliberately, at the call site,
when you know the figure has no constrained-layout-reserved margins to
protect.

## The meta-principle

**The best visualization is the simplest one that supports the claim.** Award-winning figures rarely impress through complexity — they impress because every element is doing necessary work, the comparison the figure exists to enable is made trivial for the reader, and nothing distracts from it. Most "clever" visualizations would be better as a clearer version of something familiar. Restraint reads as confidence; ornament reads as compensation.
