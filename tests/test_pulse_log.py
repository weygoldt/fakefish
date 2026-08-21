"""Tests for the pulse-log reader (``src/fakefish/pulse_log.py``).

The centrepiece is a ROUND-TRIP against ``tests/data/pulse_log_golden.csv``.
That file is not hand-written: it is emitted by the firmware's own host
self-test (``firmware/eel_core/host_test/pulse_log_selftest.cpp --emit``) through
the same formatters the Teensy uses, and ``check.sh`` fails if the binary stops
reproducing it byte-for-byte. So the C writer and this Python reader are pinned
to one artifact and cannot drift apart silently — changing either side without
the other breaks the gate.
"""

from __future__ import annotations

import numpy as np
import pytest

from fakefish import pulse_log as pl

GOLDEN = pl.Path(__file__).parent / "data" / "pulse_log_golden.csv"


@pytest.fixture
def golden() -> pl.PulseLogFile:
    return pl.read(GOLDEN)


# ===== the golden round trip ===============================================
def test_golden_exists():
    """The committed golden log must be present — it is the shared contract."""
    assert GOLDEN.is_file(), (
        "regenerate with: firmware/eel_core/host_test/pulse_log_selftest --emit"
    )


def test_header_provenance(golden):
    assert golden.format_version == 2
    assert golden.sample_rate_hz == 50000
    assert golden.file_index == 7
    assert golden.rtc_valid is True
    # Ties the rows back to the exact stimulus library that produced them.
    assert golden.header_int("stim_format_version") == 3
    assert golden.header_int("n_stim_items") == 31
    assert golden.header_int("eod_hv_len") == 131
    assert golden.header_int("eod_net_integral_x1000") == 41577
    assert golden.header["build"] == "Jan  1 2026 00:00:00"


def test_header_carries_l3_surface_keys(golden):
    """The control surface appends its own keys; the reader takes them generically."""
    assert golden.header_int("marker_pulses_volley") == 2
    assert golden.header_int("marker_pulses_sham") == 4
    assert golden.header_int("marker_ipi_samp") == 500
    assert golden.header_int("volley_item_first") == 7
    assert golden.header_int("volley_item_count") == 18
    assert golden.header_int("trial_p_volley_milli") == 500


def test_row_and_pulse_counts(golden):
    assert len(golden.records) == 39
    assert len(golden.pulses()) == 20
    assert len(golden.pulses("LOC")) == 7
    assert len(golden.pulses("MARKER")) == 8
    assert len(golden.pulses("VOLLEY")) == 5


def test_seq_is_contiguous(golden):
    assert [r.seq for r in golden.records] == list(range(len(golden.records)))
    assert golden.integrity.seq_breaks == ()


def test_golden_event_grammar_is_one_the_firmware_can_emit(golden):
    """The golden is the only worked example of the event grammar anyone will read.

    Two rules the sketch enforces structurally: localization spans are balanced
    (``begin_loc`` always emits LOCON, and LOCOFF is emitted only when leaving
    SRC_LOC), and a TRIAL is always followed by its marker pulses, because
    ``begin_marker`` unconditionally starts the count-coded burst.
    """
    depth = 0
    for r in golden.records:
        if r.event == "LOCON":
            assert depth == 0, f"LOCON at seq {r.seq} while already localizing"
            depth += 1
        elif r.event == "LOCOFF":
            assert depth == 1, f"unpaired LOCOFF at seq {r.seq}"
            depth -= 1
    assert depth == 0, "the log ends mid-localization"

    for trial in golden.trials():
        following = golden.records[golden.records.index(trial) + 1]
        assert following.event == "MARKER", (
            f"trial {trial.trial} is not followed by its marker"
        )


def test_every_event_type_is_covered(golden):
    """The golden is only a useful contract if it exercises the whole schema."""
    seen = {r.event for r in golden.records}
    assert seen == {
        "BOOT", "ANCHOR", "LINK", "LOCON", "LOC", "LOCOFF",
        "TRIAL", "MARKER", "VOLLEY", "DROP", "SHAM", "GAP",
    }


# ===== the sentinel rule ===================================================
def test_non_library_pulses_have_no_item(golden):
    """LOC and MARKER pulses come from no library item — item must be None.

    Not 0 (``STIM_ITEMS[0]`` is a real recorded volley) and not -1 (which Python
    would silently resolve to the LAST item). Either substitution would quietly
    misattribute the bulk of a log without anything appearing to fail.
    """
    for r in golden.pulses("LOC") + golden.pulses("MARKER"):
        assert r.item is None, f"{r.event} row {r.seq} must not claim a library item"


def test_item_column_is_never_a_sentinel(golden):
    """The item column is empty or a real index — never -1, and never a stand-in.

    Checked on the raw column rather than the parsed value, because the whole
    point is that the sentinel must not survive the WRITER. (-1 is legitimate
    elsewhere: ``pol`` is +1/-1.)
    """
    n_items = golden.header_int("n_stim_items")
    item_col = pl.COLUMNS.index("item")
    for line in GOLDEN.read_text().splitlines():
        if line.startswith("#") or line.startswith("seq,"):
            continue
        raw = line.split(",")[item_col]
        if raw == "":
            continue
        assert raw.lstrip("-").isdigit(), f"item column not numeric: {raw!r}"
        assert 0 <= int(raw) < n_items, f"item {raw} outside the library"


def test_absent_sentinels_never_reach_the_file():
    """The u16/u32 'absent' markers must render as empty columns, never as numbers."""
    body = "\n".join(
        ln for ln in GOLDEN.read_text().splitlines() if not ln.startswith("#")
    )
    assert "65535" not in body
    assert "4294967295" not in body


def test_volley_pulses_carry_item_and_pulse_index(golden):
    vol = golden.pulses("VOLLEY")
    assert [r.item for r in vol] == [13, 13, 13, 20, 20]
    # pulse index within the item makes a partial volley obvious and lets an
    # analysis check the logged tick deltas against the library's IPIs.
    assert [r.pulse for r in vol] == [0, 1, 2, 0, 1]
    assert golden.volley_items() == {1: 13, 3: 20}


def test_volley_amp_is_the_applied_per_pulse_amplitude(golden):
    """amp_m must include the item's per-pulse envelope, not just the master scale.

    Every volley item the RC device can draw carries a rel_amp table running 255
    down to 204, which the engine applies on top of the global scale. Logging the
    scale alone would overstate the tail of every volley by up to ~20 % — in the
    file that is supposed to be the exact ground truth.
    """
    for trial_id in (1, 3):
        vol = [r for r in golden.pulses("VOLLEY") if r.trial == trial_id]
        amps = [r.amp_m for r in vol]
        assert amps == sorted(amps, reverse=True), "envelope must decay down the burst"
        assert amps[0] == vol[0].master_m, "the first pulse is at full scale"
        assert amps[-1] < vol[-1].master_m, "later pulses are below the master scale"


def test_marker_pulses_carry_a_pulse_index_but_no_item(golden):
    mk = golden.pulses("MARKER")
    # trials 1 and 3 (volley) have 2-pulse markers; trial 2 (sham) has a 4-pulse marker.
    assert [r.trial for r in mk] == [1, 1, 2, 2, 2, 2, 3, 3]
    assert [r.pulse for r in mk] == [0, 1, 0, 1, 2, 3, 0, 1]
    assert all(r.item is None for r in mk)


# ===== blinding ============================================================
def test_requested_vs_resolved_kind_identifies_blinded_trials(golden):
    trials = golden.trials()
    assert len(trials) == 3
    # Two blinded trials from the RC lever: requested RANDOM, firmware drew.
    assert trials[0].req == "R" and trials[0].res == "V" and trials[0].blinded is True
    assert trials[1].req == "R" and trials[1].res == "S" and trials[1].blinded is True
    # One explicit bench trial from a panel button — must NOT be pooled with them.
    assert trials[2].req == "V" and trials[2].res == "V" and trials[2].blinded is False


def test_sham_trial_is_visible_despite_emitting_nothing(golden):
    """A sham puts nothing in the water, so the SHAM row is its only trace."""
    shams = golden.events("SHAM")
    assert len(shams) == 1
    assert shams[0].trial == 2
    # ...and that trial fired a marker but no volley pulses at all.
    assert not [r for r in golden.pulses("VOLLEY") if r.trial == 2]
    assert len([r for r in golden.pulses("MARKER") if r.trial == 2]) == 4


def test_blinded_is_none_on_non_trial_rows(golden):
    assert all(r.blinded is None for r in golden.records if r.event != "TRIAL")


# ===== settings ============================================================
def test_settings_are_milli_units(golden):
    loc = golden.pulses("LOC")[0]
    # 0.225 == master 0.90 / VOLLEY_AMP_RATIO 4: the golden's synthetic session now
    # uses the shipped ratio, where it predated the 2:1 -> 4:1 change.
    assert loc.amp_m == 225 and loc.amp == pytest.approx(0.225)
    assert loc.master_m == 900 and loc.master_amp == pytest.approx(0.90)
    assert loc.rand_m == 1000 and loc.randomness == pytest.approx(1.0)
    # The nominal MEDIAN interval in whole samples: a 5 Hz tick tempo at 50 kHz. Median,
    # not mean — the rate knob anchors the tick tempo, and on a heavy-tailed interval
    # distribution the two differ by about a factor of two.
    assert loc.tick_ipi == 10000
    assert golden.tick_hz(loc) == pytest.approx(5.0)
    assert loc.pol == -1


def test_every_pulse_row_carries_its_own_settings(golden):
    """Self-contained rows: a truncated file stays interpretable to its last row."""
    for r in golden.pulses():
        assert r.master_m is not None
        assert r.rand_m is not None
        assert r.tick_ipi is not None
        assert r.amp_m is not None


# ===== time ================================================================
def test_pulse_times(golden):
    ticks = golden.pulse_ticks("LOC")
    assert ticks.dtype == np.int64
    assert ticks[0] == 100000
    np.testing.assert_allclose(golden.pulse_times_s("LOC")[0], 2.0)
    # The jitter is what makes the train self-identifying under cross-correlation;
    # the tick deltas ARE the drawn IPIs.
    assert not np.all(np.diff(ticks[:3]) == np.diff(ticks[:3])[0])


def test_absolute_time_needs_two_valid_anchors(golden):
    """With a dead coin cell absolute time is unavailable — say so, never invent it."""
    assert len(golden.events("ANCHOR")) == 2
    assert len(golden.anchors()) == 1  # the second anchor's clock is not set
    with pytest.raises(pl.PulseLogError, match="at least|>=2|coin cell"):
        golden.absolute_time(np.array([0]))


def test_free_running_rtc_is_rejected_not_treated_as_wall_clock(golden):
    """A Teensy with no coin cell free-runs upward from a small value — not from 0.

    Filtering on merely ``> 0`` would feed that counter into the least-squares fit
    and report confident 1970 timestamps. The reader applies the same floor the
    firmware uses for its ``rtc_valid`` header key.
    """
    raw = [r.val for r in golden.events("ANCHOR")]
    assert 372 in raw, "the golden must exercise a plausible free-running reading"
    assert all(v >= pl.RTC_MIN_VALID for _, v in golden.anchors())
    assert 372 not in [v for _, v in golden.anchors()]


def test_absolute_time_rejects_degenerate_anchors():
    """Two anchors that carry no slope must raise, not return a collapsed mapping."""
    same_rtc = pl.parse_text(
        _synthetic_log(
            [
                "0,0,ANCHOR,,,,,,900,200,10000,1755720000,,",
                "1,1000000,ANCHOR,,,,,,900,200,10000,1755720000,,",
            ]
        )
    )
    with pytest.raises(pl.PulseLogError, match="span no time"):
        same_rtc.absolute_time(np.array([0, 500000]))

    same_tick = pl.parse_text(
        _synthetic_log(
            [
                "0,500,ANCHOR,,,,,,900,200,10000,1755720000,,",
                "1,500,ANCHOR,,,,,,900,200,10000,1755720020,,",
            ]
        )
    )
    with pytest.raises(pl.PulseLogError, match="span no time"):
        same_tick.absolute_time(np.array([0]))


def test_absolute_time_interpolates_between_anchors():
    text = _synthetic_log(
        [
            "0,0,ANCHOR,,,,,,900,200,10000,1755720000,,",
            "1,500000,LOC,,,,-1,450,900,200,10000,,,",
            "2,1000000,ANCHOR,,,,,,900,200,10000,1755720020,,",
        ]
    )
    lg = pl.parse_text(text)
    assert len(lg.anchors()) == 2
    # 1e6 ticks at 50 kHz == 20 s, matching the 20 s of RTC between the anchors.
    got = lg.absolute_time(np.array([0, 500000, 1000000]))
    np.testing.assert_allclose(got, [1755720000, 1755720010, 1755720020], rtol=0, atol=1e-6)


# ===== integrity ===========================================================
def test_integrity_reports_the_drop(golden):
    it = golden.integrity
    assert it.dropped_records == 3
    assert it.drop_events == 1
    assert not it.clean


def test_drop_row_marks_where_the_stream_resumed(golden):
    drop = golden.events("DROP")[0]
    assert drop.val == 3
    # The next row is the pulse that resumed the stream, at the same tick.
    nxt = golden.records[golden.records.index(drop) + 1]
    assert nxt.tick == drop.tick


def test_integrity_reports_the_gap(golden):
    assert golden.integrity.gaps == (6,)


def test_gap_row_has_no_tick_but_boot_keeps_its_zero(golden):
    """A GAP row is written by loop(), which cannot read the ISR-owned counter.

    An empty tick says "not applicable" in the file's own convention. A literal 0
    would read as a real event at device time zero — and would be indistinguishable
    from BOOT's legitimate tick 0.
    """
    gap = golden.events("GAP")[0]
    assert gap.tick is None
    boot = golden.events("BOOT")[0]
    assert boot.tick == 0, "BOOT's tick 0 is real and must survive"
    assert all(r.tick is not None for r in golden.pulses()), "pulses always have ticks"


def test_clean_log_reports_clean():
    lg = pl.parse_text(_synthetic_log(["0,0,LOC,,,,-1,450,900,200,10000,,,"]))
    assert lg.integrity.clean


def test_truncated_final_row_is_detected():
    """A power cut cuts the last line mid-write. Admit it, do not silently drop it."""
    text = _synthetic_log(
        [
            "0,0,LOC,,,,-1,450,900,200,10000,,,",
            "1,10000,LOC,,,,-1,450,900,200,10000,,,",
        ]
    )
    torn = text + "2,20000,LOC,,,,-1,45"  # no trailing newline
    lg = pl.parse_text(torn)
    assert lg.integrity.truncated
    assert not lg.integrity.clean
    assert len(lg.records) == 2  # the torn row is dropped, not half-parsed


def test_row_torn_inside_its_last_column_is_still_dropped():
    """A cut at or after the 13th comma leaves 14 fields — a field count cannot catch it.

    The last column is ``res``, so a half-parsed row would read as a trial with no
    outcome, which ``info`` books as a SHAM. A volley silently becomes a sham in the
    ground-truth record. The last row of a truncated file is therefore never trusted.
    """
    text = _synthetic_log(["0,0,TRIAL,,,1,1,,900,200,10000,,R,V"])
    torn = text + "1,10000,TRIAL,,,2,1,,900,200,10000,,R,"  # cut just before 'V'
    lg = pl.parse_text(torn)
    assert lg.integrity.truncated
    assert len(lg.records) == 1, "the 14-field torn row must be dropped too"
    assert lg.trials()[0].res == pl.KIND_VOLLEY


def test_corrupt_number_raises_the_documented_error_type():
    """Card corruption must surface as PulseLogError, naming the row.

    PulseLogError subclasses ValueError, so a bare ValueError from int() would slip
    past every `except PulseLogError` handler — including every test.
    """
    bad = _synthetic_log(["0,11:873,LOC,,,,-1,450,900,200,10000,,,"])
    with pytest.raises(pl.PulseLogError, match="row 0"):
        pl.parse_text(bad)


def test_seq_break_is_reported():
    lg = pl.parse_text(
        _synthetic_log(
            [
                "0,0,LOC,,,,-1,450,900,200,10000,,,",
                "5,10000,LOC,,,,-1,450,900,200,10000,,,",
            ]
        )
    )
    assert lg.integrity.seq_breaks == (1,)
    assert not lg.integrity.clean


# ===== rejection of malformed files ========================================
def test_non_log_file_rejected():
    with pytest.raises(pl.PulseLogError, match="not a fakefish pulse log"):
        pl.parse_text("seq,tick,event\n0,0,LOC\n")


def test_empty_file_rejected():
    with pytest.raises(pl.PulseLogError):
        pl.parse_text("")


def test_unsupported_format_version_rejected():
    text = _synthetic_log([], version=99)
    with pytest.raises(pl.PulseLogError, match="unsupported pulse-log format_version"):
        pl.parse_text(text)


def test_missing_format_version_rejected():
    text = f"{pl.MAGIC}\n#sample_rate_hz=50000\n{','.join(pl.COLUMNS)}\n"
    with pytest.raises(pl.PulseLogError, match="unsupported pulse-log format_version"):
        pl.parse_text(text)


def test_wrong_columns_rejected():
    text = f"{pl.MAGIC}\n#format_version=2\n#sample_rate_hz=50000\nseq,tick,event\n"
    with pytest.raises(pl.PulseLogError, match="unexpected column row"):
        pl.parse_text(text)


def test_short_row_in_the_middle_rejected():
    text = _synthetic_log(
        [
            "0,0,LOC,,,,-1,450,900,200,10000,,,",
            "1,1,LOC,,",
            "2,2,LOC,,,,-1,450,900,200,10000,,,",
        ]
    )
    with pytest.raises(pl.PulseLogError, match="expected 14"):
        pl.parse_text(text)


def test_bad_pulse_kind_rejected(golden):
    with pytest.raises(pl.PulseLogError, match="not a pulse event"):
        golden.pulses("TRIAL")


# ===== directory enumeration ===============================================
def test_iter_logs_orders_by_index(tmp_path):
    for name in ("PULS0007.CSV", "PULS0001.CSV", "PULS0010.CSV", "notes.txt", "PULS.CSV"):
        (tmp_path / name).write_text("")
    (tmp_path / "PULS0002.CSV").mkdir()  # a directory must not be listed
    got = [p.name for p in pl.iter_logs(tmp_path)]
    assert got == ["PULS0001.CSV", "PULS0007.CSV", "PULS0010.CSV"]


def test_iter_logs_accepts_lowercase(tmp_path):
    (tmp_path / "puls0003.csv").write_text("")
    assert [p.name for p in pl.iter_logs(tmp_path)] == ["puls0003.csv"]


# ===== helpers =============================================================
def _synthetic_log(rows: list[str], version: int = 2) -> str:
    """Build a minimal well-formed log around ``rows``."""
    head = [
        pl.MAGIC,
        f"#format_version={version}",
        "#sample_rate_hz=50000",
        "#rtc_valid=1",
        ",".join(pl.COLUMNS),
    ]
    return "\n".join(head + rows) + "\n"
