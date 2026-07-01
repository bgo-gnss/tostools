"""Tests for ``tostools.antenna_timeline`` — the antenna RINEX-header timeline.

Header parsing is tested directly; the timeline build injects a fake ``read_fn``
and monkeypatches ``walk_station_timeline`` so no cold archive is touched.

The load-bearing parse test is the incomplete-header rule: a header missing the
``ANTENNA: DELTA`` record must read as ``None`` (no-data, not a boundary) so it
can't fragment against fully-read neighbours.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import List

import tostools.antenna_timeline as at
from tostools.archive import ArchiveDay

# A well-formed pair of antenna records (columns matter: ANT # / TYPE is
# A20 serial + A20 model+radome; DELTA is 3F14.4 = H, E, N).
_ANT = "5678                TRM59800.00     SCIS                    ANT # / TYPE"
_DELTA = (
    "        1.0510        0.0000        0.0000                  ANTENNA: DELTA H/E/N"
)
_HEADER = f"{_ANT}\n{_DELTA}\nblah\nEND OF HEADER\n"


def test_parse_full_header():
    h = at.parse_antenna_lines(_HEADER)
    assert h is not None
    assert h.serial == "5678"
    assert h.atype == "TRM59800.00"
    assert h.radome == "SCIS"
    assert h.delta_h == 1.051
    assert h.delta_e == 0.0
    assert h.delta_n == 0.0
    assert h.is_known is True


def test_missing_delta_record_is_none():
    # ANT line present, DELTA absent → incomplete read → None (not half-populated).
    assert at.parse_antenna_lines(f"{_ANT}\nEND OF HEADER\n") is None


def test_missing_ant_record_is_none():
    assert at.parse_antenna_lines(f"{_DELTA}\nEND OF HEADER\n") is None


def test_garbled_delta_is_none():
    bad_delta = "        x.xxxx        0.0000        0.0000                  ANTENNA: DELTA H/E/N"
    assert at.parse_antenna_lines(f"{_ANT}\n{bad_delta}\nEND OF HEADER\n") is None


def _ant_line(serial: str, typeradome: str) -> str:
    return f"{serial:<20}{typeradome:<20}{'':<20}ANT # / TYPE"


def test_radome_spelling_drift_is_one_antenna():
    # RHOF regression: the SAME Trimble antenna, radome written three ways across
    # eras — embedded ('TRM57971.00 NONE'), blank, and standard-column 'NONE'.
    # All must normalize to one identity, not three phantom segments.
    embedded = at.parse_antenna_lines(
        f"{_ant_line('1441045161', 'TRM57971.00 NONE')}\n{_DELTA}\nEND OF HEADER\n"
    )
    blank = at.parse_antenna_lines(
        f"{_ant_line('1441045161', 'TRM57971.00')}\n{_DELTA}\nEND OF HEADER\n"
    )
    standard = at.parse_antenna_lines(
        f"{_ant_line('1441045161', 'TRM57971.00     NONE')}\n{_DELTA}\nEND OF HEADER\n"
    )
    assert embedded.atype == "TRM57971.00"  # radome not glued to the model
    assert embedded.radome == "NONE"  # explicit token parsed out
    assert blank.radome is None  # raw field stays None when the column is blank
    # ...but the normalized identity (what drives coalescing) collapses all three.
    assert embedded.key == blank.key == standard.key


def test_height_is_in_key_but_not_unit_key():
    a = at.AntennaHeader("5678", "TRM59800.00", "NONE", 1.0510, 0.0, 0.0)
    b = at.AntennaHeader("5678", "TRM59800.00", "NONE", 0.0083, 0.0, 0.0)
    assert a.key != b.key  # height change IS a segment boundary
    assert a.unit_key == b.unit_key  # ...but the same physical antenna


# --- timeline build (fake archive) -------------------------------------------


def _day(i: int, d: date) -> ArchiveDay:
    return ArchiveDay(obs_date=d, family="rinex", file_path=Path(str(i)))


def _wire(monkeypatch, days: List[ArchiveDay]):
    monkeypatch.setattr(at, "walk_station_timeline", lambda *a, **k: iter(days))


def test_build_antenna_timeline_detects_swap(monkeypatch):
    days = [_day(i, date(2020, 1, 1 + i)) for i in range(6)]
    _wire(monkeypatch, days)
    a = at.AntennaHeader("AAA", "TRM59800.00", "NONE", 0.0, 0.0, 0.0)
    b = at.AntennaHeader("BBB", "LEIAR25.R4", "LEIT", 0.1, 0.0, 0.0)
    headers = {Path(str(i)): (a if i < 3 else b) for i in range(6)}

    segs = at.build_antenna_timeline("TEST", root="/x", read_fn=lambda p: headers[p])
    assert [s.header.serial for s in segs] == ["AAA", "BBB"]
    assert segs[0].start == date(2020, 1, 1)
    assert segs[1].start == date(2020, 1, 4)


def test_height_reentry_segments_but_unit_coalesces(monkeypatch):
    days = [_day(i, date(2021, 6, 1 + i)) for i in range(4)]
    _wire(monkeypatch, days)
    low = at.AntennaHeader("S1", "TRM59800.00", "NONE", 0.0, 0.0, 0.0)
    high = at.AntennaHeader("S1", "TRM59800.00", "NONE", 1.0, 0.0, 0.0)
    headers = {Path(str(i)): (low if i < 2 else high) for i in range(4)}

    segs = at.build_antenna_timeline("T", root="/x", read_fn=lambda p: headers[p])
    assert len(segs) == 2  # height re-entry opens a new segment
    units = at.coalesce_antenna_units(segs)
    assert len(units) == 1  # ...but it's one physical antenna
    # install date walks back across the height-only boundary
    assert at.current_antenna_install_date(segs) == date(2021, 6, 1)


def test_empty_archive_is_empty(monkeypatch):
    _wire(monkeypatch, [])
    assert at.build_antenna_timeline("T", root="/x", read_fn=lambda p: None) == []
