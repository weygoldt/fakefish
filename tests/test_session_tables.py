"""Tests for the readable session tables and their metadata sidecar.

The conversion's whole promise is that it is friendlier WITHOUT losing anything,
so most of what is checked here is losslessness rather than prettiness.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import numpy as np
import polars as pl
import pytest
import typer

from fakefish import ingest
from fakefish import session_metadata as meta
from fakefish import session_tables as st
from fakefish.pulse_log import read
from synth_fixtures import make_pulse_log


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    ticks = np.arange(200, dtype=np.int64) * 12_000 + 400_000
    return make_pulse_log(
        tmp_path / "PULS0007.CSV",
        ticks=ticks,
        trial_ticks=np.array([600_000, 900_000, 1_200_000, 1_500_000], dtype=np.int64),
        trial_outcomes=["V", "B", "S", "V"],
    )


# ===== nothing is dropped ==================================================
def test_every_header_key_has_a_home(log_path: Path) -> None:
    """The guard on "nothing is lost" for the single-valued half.

    A firmware that starts writing a new header key must fail here rather than
    have it quietly vanish from every converted session thereafter.
    """
    assert meta.unmapped_keys(read(log_path)) == set()


def test_every_source_column_survives_somewhere(log_path: Path, tmp_path: Path) -> None:
    """The guard on "nothing is lost" for the tabular half.

    Each column of the device log must reappear -- renamed, split by event type,
    or converted to real units -- in one of the four tables.
    """
    out = tmp_path / "out"
    ingest.run(log_path, out_dir=out, verbose=0)
    frames = ingest.read_tables(out, "PULS0007")
    everywhere = set().union(*(set(f.columns) for f in frames.values()))

    # tick, seq and the settings columns, by their new names.
    assert {"sample_tick", "source_row", "time_s"} <= everywhere
    assert {"volley_amplitude", "randomness", "tick_hz", "tick_interval_s"} <= everywhere
    assert set(st.RADIO_CHANNELS) <= everywhere
    assert "receiver_zero_us" in everywhere
    # item / pulse / trial / pol / amp_m
    assert {"stimulus_item", "pulse_index_in_item", "trial_number", "polarity",
            "amplitude"} <= everywhere
    # req / res
    assert {"requested", "treatment", "was_blinded"} <= everywhere
    # val, which used to mean four different things, split into named columns.
    assert {"file_index", "clock_unix", "records_lost", "radio_link_up"} <= everywhere


def test_no_column_means_two_things(log_path: Path, tmp_path: Path) -> None:
    """`val` was a file index, a unix clock and a lost-record count at once.

    Each of those is now its own column, and each is populated on exactly the
    event that has it -- which is the property that made the old file hard to
    read in a spreadsheet.
    """
    out = tmp_path / "out"
    ingest.run(log_path, out_dir=out, verbose=0)
    ev = ingest.read_tables(out, "PULS0007")["session_events"]

    boots = ev.filter(pl.col("event") == "boot")
    anchors = ev.filter(pl.col("event") == "clock_anchor")
    assert boots["file_index"].null_count() == 0
    assert boots["clock_unix"].null_count() == boots.height
    assert anchors["clock_unix"].null_count() == 0
    assert anchors["file_index"].null_count() == anchors.height


# ===== the tables themselves ===============================================
def test_pulses_carry_their_treatment(log_path: Path, tmp_path: Path) -> None:
    """A pulse says which arm it belongs to, so filtering needs no join."""
    out = tmp_path / "out"
    ingest.run(log_path, out_dir=out, verbose=0)
    p = ingest.read_tables(out, "PULS0007")["pulses"]

    assert set(p["pulse_type"].unique()) <= set(st.PULSE_TYPES.values())
    # The ambient localization train belongs to no trial, so it has no treatment.
    ambient = p.filter(pl.col("trial_number").is_null())
    assert ambient.height > 0
    assert ambient["treatment"].null_count() == ambient.height
    # A pulse inside a trial names its arm in words.
    in_trial = p.filter(pl.col("trial_number").is_not_null())
    assert set(in_trial["treatment"].unique()) <= set(st.TREATMENTS.values())


def test_amplitudes_are_fractions_not_milli_units(log_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    ingest.run(log_path, out_dir=out, verbose=0)
    frames = ingest.read_tables(out, "PULS0007")
    amp = frames["pulses"]["amplitude"].drop_nulls()
    assert amp.max() <= 1.0 and amp.min() >= 0.0
    ctl = frames["controls"]["volley_amplitude"].drop_nulls()
    assert ctl.max() <= 1.0


def test_controls_are_one_row_per_change_and_lossless(log_path: Path, tmp_path: Path) -> None:
    """The compression must be exact, not approximate.

    An as-of join back onto the pulses has to reproduce the value the device
    stamped on each row, or the table is a lie that happens to be shorter.
    """
    out = tmp_path / "out"
    ingest.run(log_path, out_dir=out, verbose=0)
    frames = ingest.read_tables(out, "PULS0007")
    controls, pulses = frames["controls"], frames["pulses"]

    assert controls.height < pulses.height, "settings change far less often than pulses fire"

    log = read(log_path)
    want = {
        r.seq: (None if r.master_m is None else r.master_m / 1000.0)
        for r in log.pulses()
    }
    rebuilt = (
        pulses.select("source_row", "time_s")
        .sort("time_s")
        .join_asof(
            controls.select("time_s", "volley_amplitude").sort("time_s"),
            on="time_s",
            strategy="backward",
        )
    )
    for row in rebuilt.iter_rows(named=True):
        assert row["volley_amplitude"] == pytest.approx(want[row["source_row"]]), (
            f"row {row['source_row']} does not reconstruct"
        )


def test_trials_include_the_silence_arm_with_a_real_span(log_path: Path, tmp_path: Path) -> None:
    """The silence arm emits nothing, so this table is the only place it exists."""
    out = tmp_path / "out"
    ingest.run(log_path, out_dir=out, verbose=0)
    t = ingest.read_tables(out, "PULS0007")["trials"]

    assert t["treatment"].to_list() == ["volley", "baseline", "silence", "volley"]
    silence = t.filter(pl.col("treatment") == "silence")
    assert (silence["pulses_emitted"] == 0).all()
    assert (silence["duration_s"] > 0).all()
    assert (silence["ended_s"] > silence["time_s"]).all()


def test_treatments_are_words_not_codes(log_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    ingest.run(log_path, out_dir=out, verbose=0)
    t = ingest.read_tables(out, "PULS0007")["trials"]
    assert not set(t["treatment"].unique()) & {"V", "B", "S"}
    assert set(t["requested"].unique()) == {"random"}
    assert t["was_blinded"].all()


def test_tables_carry_no_comment_lines(log_path: Path, tmp_path: Path) -> None:
    """A spreadsheet should open these directly: header row, then data."""
    out = tmp_path / "out"
    ingest.run(log_path, out_dir=out, verbose=0)
    for name in ("pulses", "trials", "session_events", "controls"):
        text = (out / f"PULS0007_{name}.csv").read_text()
        assert not text.startswith("#")
        assert "\n#" not in text


# ===== metadata ============================================================
def test_metadata_is_readable_toml_with_the_facts_in_it(log_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    ingest.run(log_path, out_dir=out, verbose=0)
    doc = tomllib.loads((out / "PULS0007_metadata.toml").read_text())

    assert doc["session"]["session_id"] == "PULS0007"
    assert doc["session"]["device"] == "eel_fakefish_rc"
    assert doc["clock"]["sample_rate_hz"] == 50_000
    assert doc["stimulus_library"]["item_count"] == 113
    assert doc["counts"]["trials"] == 4
    assert doc["counts"]["silence_trials"] == 1
    # Integrity moves out of the reader and into the file, where a person sees it.
    assert doc["integrity"]["records_lost"] == 0
    assert doc["integrity"]["truncated_by_power_loss"] is False
    # Provenance: which log, and exactly which bytes of it.
    assert doc["source"]["file"] == "PULS0007.CSV"
    assert len(doc["source"]["sha256"]) == 64
    assert doc["source"]["format_version"] == 4


def test_metadata_round_trips_through_a_toml_parser() -> None:
    """Hand-written TOML must actually be TOML, including the awkward types."""
    doc = {
        "a": {"s": 'quotes " and \\ backslash', "i": 3, "f": 1.5, "b": True},
        "b": {"empty_list": [], "list": [1, 2, 3]},
    }
    assert tomllib.loads(meta.dumps(doc)) == doc


# ===== the command =========================================================
def test_ingest_refuses_to_clobber_then_force_replaces(log_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    ingest.run(log_path, out_dir=out, verbose=0)
    with pytest.raises(typer.BadParameter):
        ingest.run(log_path, out_dir=out, verbose=0)
    ingest.run(log_path, out_dir=out, verbose=0, force=True)


def test_session_id_names_the_files(log_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    ingest.run(log_path, out_dir=out, session_id="exp7", verbose=0)
    assert (out / "exp7_pulses.csv").is_file()
    assert (out / "exp7_metadata.toml").is_file()


def test_the_source_log_is_never_modified(log_path: Path, tmp_path: Path) -> None:
    before = log_path.read_bytes()
    ingest.run(log_path, out_dir=tmp_path / "out", verbose=0)
    assert log_path.read_bytes() == before


def test_device_time_only_when_there_is_no_recording(log_path: Path, tmp_path: Path) -> None:
    """No recording means no `recording_time_s` column at all.

    An empty column would be a claim that the mapping exists and is unknown; its
    absence says plainly that nothing was aligned.
    """
    out = tmp_path / "out"
    ingest.run(log_path, out_dir=out, verbose=0)
    for frame in ingest.read_tables(out, "PULS0007").values():
        assert "recording_time_s" not in frame.columns
