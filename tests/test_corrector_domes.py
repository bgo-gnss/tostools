"""MARKER NUMBER ← DOMES emission in the TOS correction builder.

``_get_corrections_from_tos`` must emit a ``MARKER NUMBER`` correction from the
station's IERS DOMES number so a ``--fix-headers`` run can actually rewrite it.
When TOS carries no DOMES the policy is to STRIP the line (``STRIP_LINE``
sentinel) — never fall back to the 4-char marker/id. No network: the
``gps_metadata`` call is patched.
"""

from __future__ import annotations

from datetime import datetime

from tostools.rinex import corrector


def _station_data(*, domes):
    """Minimal gps_metadata() return: one covering session + optional DOMES."""
    data = {
        "device_history": [
            {
                "time_from": "2000-01-01T00:00:00",
                "time_to": None,
                "gnss_receiver": {
                    "serial_number": "3001",
                    "model": "TRIMBLE NETR9",
                    "firmware_version": "5.60",
                },
                "antenna": {
                    "serial_number": "1001",
                    "model": "TRM57971.00",
                    "antenna_height": 0.0,
                },
                "monument": {"monument_height": 1.014},
            }
        ]
    }
    if domes is not None:
        data["iers_domes_number"] = domes
    return data


def test_marker_number_emitted_from_domes(monkeypatch):
    monkeypatch.setattr(
        corrector, "gps_metadata", lambda *a, **k: _station_data(domes="10216M001")
    )
    corr = corrector._get_corrections_from_tos("RHOF", datetime(2010, 4, 1), 40)
    assert corr["MARKER NUMBER"] == ["10216M001"]
    # sanity: it still builds the other corrections in the same pass
    assert "MARKER NAME" in corr
    assert "ANTENNA: DELTA H/E/N" in corr


def test_marker_number_stripped_when_no_domes(monkeypatch):
    # No DOMES → MARKER NUMBER is stripped, never the 4-char marker/id.
    monkeypatch.setattr(
        corrector, "gps_metadata", lambda *a, **k: _station_data(domes=None)
    )
    corr = corrector._get_corrections_from_tos("RHOF", datetime(2010, 4, 1), 40)
    assert corr["MARKER NUMBER"] is corrector.STRIP_LINE


def test_marker_number_stripped_when_domes_blank(monkeypatch):
    monkeypatch.setattr(
        corrector, "gps_metadata", lambda *a, **k: _station_data(domes="   ")
    )
    corr = corrector._get_corrections_from_tos("RHOF", datetime(2010, 4, 1), 40)
    assert corr["MARKER NUMBER"] is corrector.STRIP_LINE


def test_marker_number_stripped_when_value_is_4char_not_domes(monkeypatch):
    # A 4-char id sitting in iers_domes_number is not a real DOMES → strip.
    monkeypatch.setattr(
        corrector, "gps_metadata", lambda *a, **k: _station_data(domes="RHOF")
    )
    corr = corrector._get_corrections_from_tos("RHOF", datetime(2010, 4, 1), 40)
    assert corr["MARKER NUMBER"] is corrector.STRIP_LINE


# ---------------------------------------------------------------------------
# _insert_header_record — write MARKER NUMBER when the header line is absent
# (pre-DOMES-era files, e.g. RHOF 2000-2011, carry no MARKER NUMBER line).
# ---------------------------------------------------------------------------

import logging  # noqa: E402

_LOG = logging.getLogger("test")

_HDR_NO_MNUM = (
    "     2.10           OBSERVATION DATA    G (GPS)             RINEX VERSION / TYPE\n"
    "RHOF                                                        MARKER NAME\n"
    "SOME AGENCY                                                 OBSERVER / AGENCY\n"
    "                                                            END OF HEADER\n"
)


def test_insert_marker_number_after_marker_name():
    new_line = "10216M001".ljust(20) + " " * 40 + "MARKER NUMBER"
    out = corrector._insert_header_record(_HDR_NO_MNUM, "MARKER NUMBER", new_line, _LOG)
    lines = out.splitlines()
    # MARKER NUMBER lands immediately after MARKER NAME
    names = [i for i, ln in enumerate(lines) if "MARKER NAME" in ln][0]
    assert "MARKER NUMBER" in lines[names + 1]
    assert "10216M001" in lines[names + 1]
    # nothing else lost; END OF HEADER still present and last
    assert "END OF HEADER" in lines[-1]
    # line endings preserved (still newline-terminated)
    assert out.endswith("\n")


def test_insert_generic_label_before_end_of_header():
    new_line = "x".ljust(60) + "INTERVAL"
    out = corrector._insert_header_record(_HDR_NO_MNUM, "INTERVAL", new_line, _LOG)
    lines = out.splitlines()
    eoh = [i for i, ln in enumerate(lines) if "END OF HEADER" in ln][0]
    assert "INTERVAL" in lines[eoh - 1]


def test_insert_no_anchor_leaves_header_unchanged():
    junk = "no anchors here at all\n"
    out = corrector._insert_header_record(junk, "MARKER NUMBER", "whatever", _LOG)
    assert out == junk


# ---------------------------------------------------------------------------
# STRIP_LINE sentinel — _apply_corrections removes an existing MARKER NUMBER
# line and inserts nothing (no-DOMES policy).
# ---------------------------------------------------------------------------

_HDR_ONLY_WITH_MNUM = (
    "     2.10           OBSERVATION DATA    G (GPS)             RINEX VERSION / TYPE\n"
    "RHOF                                                        MARKER NAME\n"
    "RHOF                                                        MARKER NUMBER\n"
    "SOME AGENCY                                                 OBSERVER / AGENCY\n"
    "                                                            END OF HEADER"
)


def test_strip_line_removes_existing_marker_number(tmp_path, monkeypatch):
    src = tmp_path / "rhof0010.10o"
    # On-disk file: real header (through END OF HEADER) + a data line.
    src.write_text(_HDR_ONLY_WITH_MNUM + "\n  DATA LINE THAT MUST SURVIVE\n")
    # read_rinex_header returns only the header portion (no data section).
    monkeypatch.setattr(
        corrector, "read_rinex_header", lambda p: {"header": _HDR_ONLY_WITH_MNUM}
    )
    out = corrector._apply_corrections(
        src, {"MARKER NUMBER": corrector.STRIP_LINE}, src, _LOG
    )
    text = out.read_text()
    assert "MARKER NUMBER" not in text
    assert "MARKER NAME" in text  # the similarly-named line is untouched
    assert "DATA LINE THAT MUST SURVIVE" in text
