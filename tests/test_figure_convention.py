"""VENDORED from eeltracker (`tests/test_figure_convention.py`), the regression test
for the deck figure system this package copies in `src/fakefish/viz/`. Re-dropped
2026-08-22 with the scan paths pointed at `src/fakefish` and the module imports
rewritten; keep it in step with the upstream copy rather than editing it here.

Enforce the package-wide figure convention.

The convention (see ``docs/guidelines/figure_guidelines.md`` and
``fakefish.viz.plotstyle``):

- every figure is one of two widths — half (8.5 cm) or full (17 cm);
- every saved figure is 300 dpi;
- font sizes per annotation class are uniform;
- colours come from the deck scheme.

It is enforced by *routing every figure through* the constructors in
``fakefish.viz.plotstyle`` (re-exported by ``utils.figsave`` and
``fakefish.viz.figsave``). This test fails if any figure module sets a figure
size or resolution itself instead of going through that one module:

- a ``figsize=`` keyword anywhere outside the canonical modules, or
- a numeric ``dpi=`` that is not 300 (named constants like
  ``ANIMATION_DPI`` / ``FIGURE_DPI`` are fine).

When this test fails it prints every offending ``file:line`` so the fix
is mechanical: build the figure with ``half_page`` / ``full_page`` /
``figure`` and save with ``save_figure`` instead.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories whose figure code must obey the convention.
SCAN_DIRS = [
    REPO_ROOT / "src" / "fakefish",
]

# Never scanned: third-party, vcs, other jobs' worktrees, archived one-offs.
EXCLUDE_PARTS = {".venv", ".git", ".claude", "__pycache__", "_archive", "node_modules"}

# The convention's single source of truth — these *define* sizing/dpi and
# are therefore allowed to contain ``figsize=`` / numeric ``dpi=``.
CANONICAL_FILES = {
    REPO_ROOT / "src" / "fakefish" / "viz" / "plotstyle.py",
    REPO_ROOT / "src" / "fakefish" / "viz" / "figsave.py",
}

# Documented, deliberate exceptions (each needs a reason). Video frames are
# sized in pixels, not as print figures, so an animation may pick a custom
# figure size / frame dpi. Keep this set SMALL and justified.
EXEMPT_FILES: dict[Path, str] = {
    # (populated only if a figure genuinely cannot use half/full width —
    #  e.g. a video animation needing exact pixel dimensions)
}

ALLOWED_DPI = {300}


def _in_scope(path: Path) -> bool:
    # Check parts *relative* to the repo root: when running inside a git
    # worktree the repo root itself sits under ``.claude/worktrees/...``,
    # so absolute-part matching would exclude every file.
    try:
        rel_parts = set(path.relative_to(REPO_ROOT).parts)
    except ValueError:
        rel_parts = set(path.parts)
    if rel_parts & EXCLUDE_PARTS:
        return False
    if path in CANONICAL_FILES or path in EXEMPT_FILES:
        return False
    return True


def _iter_py_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_DIRS:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if _in_scope(p):
                files.append(p)
    return sorted(files)


def _scan_file(path: Path) -> list[str]:
    """Return human-readable violation strings for one file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover
        return [f"{path}: could not parse ({exc})"]

    rel = path.relative_to(REPO_ROOT)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg is None:
            continue
        if node.arg == "figsize":
            violations.append(
                f"{rel}:{node.value.lineno}: figsize= set at call site — "
                f"use half_page()/full_page()/figure() instead"
            )
        elif node.arg == "dpi":
            val = node.value
            if isinstance(val, ast.Constant) and isinstance(val.value, (int, float)):
                if int(val.value) not in ALLOWED_DPI:
                    violations.append(
                        f"{rel}:{val.lineno}: dpi={val.value} — saved figures "
                        f"must be 300 dpi (save via figsave.save_figure)"
                    )
    return violations


FILES = _iter_py_files()


def test_some_figure_files_were_scanned():
    """Guard against the scanner silently finding nothing (wrong paths)."""
    assert len(FILES) > 10, f"expected to scan the package, found {len(FILES)}"


def test_no_call_site_sets_figsize_or_bad_dpi():
    """No figure module may set its own size/resolution — route via plotstyle."""
    all_violations: list[str] = []
    for path in FILES:
        all_violations.extend(_scan_file(path))
    if all_violations:
        report = "\n".join(all_violations)
        n_files = len({v.split(":")[0] for v in all_violations})
        pytest.fail(
            f"{len(all_violations)} figure-convention violation(s) across "
            f"{n_files} file(s):\n{report}",
            pytrace=False,
        )


# --------------------------------------------------------------------------
# Positive locks on the canonical API (catch regressions in plotstyle itself)
# --------------------------------------------------------------------------


def test_constructors_produce_blessed_widths():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from fakefish.viz import plotstyle as ps

    # squeezed (drop-in for plt.subplots): 1-D for a single row
    fig, axes = ps.half_page(height_cm=5.0, ncols=2)
    assert abs(fig.get_size_inches()[0] / ps.cm - ps.HALF_WIDTH_CM) < 1e-6
    assert axes.shape == (2,)
    plt.close(fig)

    # single panel returns a bare Axes, like plt.subplots(1, 1)
    fig, ax = ps.full_page(height_cm=6.0)
    assert isinstance(ax, plt.Axes)
    assert abs(fig.get_size_inches()[0] / ps.cm - ps.FULL_WIDTH_CM) < 1e-6
    plt.close(fig)

    # legacy panel_size_cm path snaps width to half/full and keeps nrows/ncols
    from fakefish.viz import figsave as fs

    fig, axes = fs.figure(2, 3, panel_size_cm=(9.0, 7.0))
    assert axes.shape == (2, 3)
    assert abs(fig.get_size_inches()[0] / ps.cm - ps.FULL_WIDTH_CM) < 1e-6
    plt.close(fig)


def test_style_enforces_fonts_and_dpi():
    import matplotlib as mpl

    from fakefish.viz import plotstyle as ps

    ps.set_light_style()
    assert mpl.rcParams["savefig.dpi"] == ps.FIGURE_DPI == 300
    # Inter is the package default font family (matches the slide deck).
    assert mpl.rcParams["font.family"] == ["sans-serif"]
    assert mpl.rcParams["font.sans-serif"][0] == "Inter"
    assert mpl.rcParams["axes.labelsize"] == 9
    assert mpl.rcParams["xtick.labelsize"] == 8
    assert mpl.rcParams["axes.titlesize"] == 10
    assert mpl.rcParams["legend.fontsize"] == 8
    assert mpl.rcParams["figure.titlesize"] == 11
    cycle = mpl.rcParams["axes.prop_cycle"].by_key()["color"]
    assert cycle[:2] == ps.CATEGORICAL[:2]
