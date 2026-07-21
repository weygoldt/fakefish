"""Vendored figure + logging helpers for the fakefish tools.

Standalone copy of the deck figure system (``plotstyle`` page geometry, Inter
font, deck colours), the single save path (``figsave``), and a stdlib logging
shim (``loggers``). Re-exports the symbols the tools import so a call site can
reach them from :mod:`fakefish.viz` directly if preferred.
"""

from fakefish.viz.figsave import save_figure
from fakefish.viz.loggers import configure_logging, get_logger
from fakefish.viz.plotstyle import CATEGORICAL, blank_figure, full_page

__all__ = [
    "save_figure",
    "configure_logging",
    "get_logger",
    "CATEGORICAL",
    "blank_figure",
    "full_page",
]
