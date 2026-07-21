"""Repo-relative path resolution for the fakefish tools.

The tools used to run as plain scripts from the repo root, so their input and
output paths were written CWD-relative (``firmware/eel_fakefish/eel_stimuli.cpp``,
``tools/stimuli_config.yaml``, ...). As an installed package the tools may run
from any directory, so paths are resolved here relative to the *project root* —
the directory that contains ``firmware/eel_fakefish`` — discovered by walking up
from this file. That works for an editable install (``uv sync`` / ``pip install
-e .``), which is how the toolchain is used; flashing needs no Python at all.

Layout resolved here::

    <root>/firmware/eel_fakefish/eel_stimuli.cpp   the generated library (parsed by tools)
    <root>/data/                                   versioned inputs + the frozen provenance
    <root>/figs/                                   regenerable figure output (gitignored)
"""

from __future__ import annotations

from pathlib import Path


def project_root() -> Path:
    """Return the repo root (the dir containing ``firmware/eel_fakefish``).

    Walks up from this module. Falls back to the current working directory if
    the marker is not found (e.g. a non-editable install detached from the
    source tree) so that running from the repo root still resolves.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "firmware" / "eel_fakefish").is_dir():
            return parent
    return Path.cwd()


ROOT: Path = project_root()

#: Versioned data assets: the export config, the frozen provenance contract, and
#: the tuned volley model + populations. The export ``out_dir`` writes here too.
DATA_DIR: Path = ROOT / "data"

#: The self-contained Teensy sketch folder.
FIRMWARE_DIR: Path = ROOT / "firmware" / "eel_fakefish"

#: The generated stimulus library the tools parse (``ex.parse_firmware``).
DEFAULT_FIRMWARE: Path = FIRMWARE_DIR / "eel_stimuli.cpp"

#: The reproducible export configuration.
DEFAULT_CONFIG: Path = DATA_DIR / "stimuli_config.yaml"

#: Regenerable figure output (gitignored). Galleries / simulations write here.
FIGS_DIR: Path = ROOT / "figs"


def data_file(name: str) -> Path:
    """Path to a versioned data asset under ``data/`` (read or write)."""
    return DATA_DIR / name
