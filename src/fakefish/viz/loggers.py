"""Minimal stdlib logging helpers for the fakefish tools.

Vendored logging surface (previously supplied by the parent analysis package):
the same public functions ``configure_logging`` + ``get_logger`` and the same
verbosity convention (``0`` = WARNING, ``1`` = INFO, ``2+`` = DEBUG), but built
on the standard library only. The original also carried package-module
discovery used to keep the parent package quiet; the standalone tools do not
need it.
"""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
_DATEFMT = "[%X]"


def _level_for(verbosity: int) -> int:
    if verbosity <= 0:
        return logging.WARNING
    if verbosity == 1:
        return logging.INFO
    return logging.DEBUG


def configure_logging(
    verbosity: int = 0,
    log_to_file: bool = False,
    log_file: str = "fakefish.log",
) -> None:
    """Configure root logging for a CLI entry point.

    ``verbosity`` maps 0->WARNING, 1->INFO, 2+->DEBUG. ``force=True`` mirrors the
    original so repeated calls (e.g. across Typer sub-commands) re-apply cleanly.
    """
    level = _level_for(verbosity)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_to_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(_FORMAT))
        handlers.append(file_handler)

    logging.basicConfig(
        level=level,
        format=_FORMAT,
        datefmt=_DATEFMT,
        handlers=handlers,
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    """Return a module logger (thin wrapper for a consistent import surface)."""
    return logging.getLogger(name)
