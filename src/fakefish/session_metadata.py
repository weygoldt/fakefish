"""The single-valued half of a session, as TOML.

Everything in the device log's ``#key=value`` preamble lands here, grouped and
renamed, plus what the reader worked out about the file's own integrity and what
produced the derived tables. The CSVs then hold tabular data and nothing else.

NOTHING IS DROPPED. :func:`unmapped_keys` is the guard on that promise: it
reports any header key this module does not know, so a firmware that starts
writing a new one fails a test rather than losing it silently.

TOML is hand-written rather than pulled from a library. The values here are
flat scalars in a handful of sections, the standard library can only *read*
TOML, and the firmware will eventually have to emit the same shape from C --
where `key = value` is a `printf` and a dependency is not an option.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Optional

from fakefish.pulse_log import PulseLogFile

#: Header keys that become metadata, grouped into the sections they are written
#: under. The left column is the device log's name, the right is what a person
#: reading the file sees.
#:
#: Keys absent from a given log (an older format version, a different surface)
#: are simply not written -- an empty value would be a claim.
SECTIONS: dict[str, dict[str, str]] = {
    "clock": {
        "sample_rate_hz": "sample_rate_hz",
        "anchor_period_samp": "anchor_period_samples",
    },
    "stimulus_library": {
        "stim_format_version": "format_version",
        "n_stim_items": "item_count",
        "eod_hv_len": "eod_length_samples",
        "eod_net_integral_x1000": "eod_net_integral_x1000",
        "volley_item_first": "first_volley_item",
        "volley_item_count": "volley_item_count",
    },
    "trial_design": {
        "trial_w_volley_milli": "volley_weight_permille",
        "trial_w_baseline_milli": "baseline_weight_permille",
        "trial_w_silence_milli": "silence_weight_permille",
        "trial_base_tick_milli_hz": "baseline_tick_millihz",
        "trial_base_randomness_milli": "baseline_randomness_permille",
        # Pre-v4 two-arm designs. Kept so an old log converts without loss, and
        # named so nobody mistakes it for the three-arm weights above.
        "trial_p_volley_milli": "legacy_volley_probability_permille",
    },
    "localization": {
        "loc_rhythm_fitted": "uses_fitted_rhythm",
        "loc_nominal_tick_milli_hz": "nominal_tick_millihz",
        "loc_randomness_max_milli": "randomness_max_permille",
        "loc_refractory_samp": "refractory_samples",
        "loc_rate_anchor_median": "rate_anchors_the_median",
    },
    "logging": {
        "ring_size": "ring_size",
    },
    # Retired with the RC marker on 2026-08-22. Only old logs carry these; they
    # convert rather than vanish.
    "retired_marker": {
        "marker_ipi_samp": "interval_samples",
        "marker_pulses_volley": "volley_pulse_count",
        "marker_pulses_sham": "sham_pulse_count",
        "marker_amp_milli": "amplitude_permille",
    },
}

#: Header keys handled individually in :func:`build`, rather than by the table
#: above, because they are renamed, combined or turned into a different type.
_SPECIAL = {
    "format_version",
    "file_index",
    "rtc_unix",
    "rtc_valid",
    "boot_rtc_unix",
    "build",
    "surface",
}

#: Keys whose value is a boolean flag written as 0/1 in the log.
_BOOLEAN = {"uses_fitted_rhythm", "rate_anchors_the_median"}

DEVICES = {0: "eel_fakefish_rc", 1: "eel_fakefish_button"}


def unmapped_keys(log: PulseLogFile) -> set[str]:
    """Header keys this module would silently discard. Must always be empty.

    The whole point of the conversion is that nothing is lost, and the failure
    mode is quiet: a firmware adds a key, the converter does not know it, and it
    disappears from every derived session from then on. This is what a test
    asserts against.
    """
    known = set(_SPECIAL)
    for mapping in SECTIONS.values():
        known |= set(mapping)
    return set(log.header) - known


def _iso(unix: Optional[int]) -> Optional[str]:
    if unix is None:
        return None
    return dt.datetime.fromtimestamp(int(unix), dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(
    log: PulseLogFile,
    *,
    source_path: Path,
    source_sha256: str,
    tool: str,
    extra: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, dict[str, Any]]:
    """Every single-valued fact about a session, grouped into TOML sections."""
    h = log.header

    def num(key: str) -> Optional[int]:
        v = log.header_int(key)
        return None if v is None else int(v)

    doc: dict[str, dict[str, Any]] = {}

    doc["session"] = _drop_none(
        {
            "session_id": source_path.stem,
            "device": DEVICES.get(num("surface")) if "surface" in h else None,
            "file_index": num("file_index"),
            "firmware_build": h.get("build"),
            # The device's own clock at the moment the file was opened. It is a
            # Teensy RTC: good to a second, and free-running from a small value
            # if the coin cell is dead, which `clock_is_valid` reports.
            "recorded_at": _iso(num("rtc_unix")),
            "booted_at": _iso(num("boot_rtc_unix")),
            "clock_is_valid": bool(num("rtc_valid")) if "rtc_valid" in h else None,
        }
    )

    for section, mapping in SECTIONS.items():
        body: dict[str, Any] = {}
        for src, dst in mapping.items():
            if src not in h:
                continue
            v = num(src)
            body[dst] = bool(v) if dst in _BOOLEAN else v
        if body:
            doc[section] = body

    pulses = log.pulses()
    trials = log.trials()
    arms = {"volley": "V", "baseline": "B", "silence": "S"}
    doc["counts"] = {
        "pulses": len(pulses),
        "trials": len(trials),
        **{f"{name}_trials": sum(1 for t in trials if t.res == code)
           for name, code in arms.items()},
        "rows_in_source": len(log.records),
    }

    it = log.integrity
    doc["integrity"] = {
        "records_lost": it.dropped_records,
        "drop_events": it.drop_events,
        "sequence_breaks": len(it.seq_breaks),
        "truncated_by_power_loss": it.truncated,
        "interrupted_file_indices": list(it.gaps),
    }

    doc["source"] = _drop_none(
        {
            "file": source_path.name,
            "sha256": source_sha256,
            "format_version": num("format_version"),
            "converted_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "converted_by": tool,
        }
    )

    for section, body in (extra or {}).items():
        doc[section] = _drop_none(body)
    return doc


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def dumps(doc: dict[str, dict[str, Any]]) -> str:
    """Render the sections as TOML. Flat scalars only, which is all this needs."""
    out: list[str] = []
    for section, body in doc.items():
        if not body:
            continue
        out.append(f"[{section}]")
        width = max(len(k) for k in body)
        for k, v in body.items():
            out.append(f"{k.ljust(width)} = {_fmt(v)}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write(path: Path, doc: dict[str, dict[str, Any]]) -> None:
    path.write_text(dumps(doc), encoding="utf-8")
