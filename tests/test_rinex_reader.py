"""Unit tests for :mod:`tostools.rinex.reader`.

Covers the auto-resolve helpers added with PR #9 — the daily Hatanaka
filename parser, the directory scanner, and the most-recent-walker.
Filesystem fixtures are built under ``tmp_path``; no network, no
side effects.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from tostools.rinex.reader import (
    MONTHS,
    _parse_daily_rinex_date,
    _parse_hourly_raw_date,
    _scan_dir_dates,
    find_most_recent_rinex,
    find_station_archive_range,
)

# ---------------------------------------------------------------------------
# _parse_daily_rinex_date — the regex contract is the load-bearing piece
# ---------------------------------------------------------------------------


def test_parse_daily_rinex_date_yy_pivot_post_2000():
    """YY < 80 should resolve to 2000+YY (modern era)."""
    assert _parse_daily_rinex_date("HAUC2660.26D", "HAUC") == datetime(2026, 9, 23)


def test_parse_daily_rinex_date_yy_pivot_pre_2000():
    """YY >= 80 should resolve to 1900+YY (legacy era)."""
    assert _parse_daily_rinex_date("HAUC0010.98D", "HAUC") == datetime(1998, 1, 1)


def test_parse_daily_rinex_date_doy_to_calendar():
    """DOY 100 in 2026 is April 10."""
    assert _parse_daily_rinex_date("HAUC1000.26D", "HAUC") == datetime(2026, 4, 10)


def test_parse_daily_rinex_date_accepts_observation_o_extension():
    """``[DO]`` in the regex — both D (Hatanaka compressed) and O (raw
    RINEX) should parse."""
    assert _parse_daily_rinex_date("HAUC2660.26O", "HAUC") == datetime(2026, 9, 23)


def test_parse_daily_rinex_date_accepts_compression_suffix():
    """``.Z`` and ``.gz`` should both be tolerated."""
    assert _parse_daily_rinex_date("HAUC2660.26D.Z", "HAUC") == datetime(2026, 9, 23)
    assert _parse_daily_rinex_date("HAUC2660.26D.gz", "HAUC") == datetime(2026, 9, 23)


def test_parse_daily_rinex_date_rejects_wrong_station():
    """Station code is fixed by the caller — a file from a different
    station mustn't match (or auto-resolve would return cross-station files)."""
    assert _parse_daily_rinex_date("RHOF2660.26D", "HAUC") is None


def test_parse_daily_rinex_date_rejects_non_rinex_filename():
    assert _parse_daily_rinex_date("not_a_rinex_file.txt", "HAUC") is None


def test_parse_daily_rinex_date_is_case_insensitive():
    """re.IGNORECASE on the pattern — operators sometimes paste lowercase
    station codes."""
    assert _parse_daily_rinex_date("hauc2660.26d", "HAUC") == datetime(2026, 9, 23)


# ---------------------------------------------------------------------------
# _scan_dir_dates
# ---------------------------------------------------------------------------


def test_scan_dir_dates_skips_unrecognised_filenames(tmp_path: Path):
    (tmp_path / "HAUC1000.26D").touch()
    (tmp_path / "README.txt").touch()
    (tmp_path / "HAUC2000.26D").touch()
    out = _scan_dir_dates(tmp_path, _parse_daily_rinex_date, "HAUC")
    assert len(out) == 2
    dates = sorted(d for d, _ in out)
    assert dates == [datetime(2026, 4, 10), datetime(2026, 7, 19)]


def test_scan_dir_dates_missing_directory_returns_empty(tmp_path: Path):
    """Non-existent directory is a normal state (sparse archive) — must
    not raise."""
    missing = tmp_path / "does-not-exist"
    assert _scan_dir_dates(missing, _parse_daily_rinex_date, "HAUC") == []


def test_scan_dir_dates_empty_directory_returns_empty(tmp_path: Path):
    assert _scan_dir_dates(tmp_path, _parse_daily_rinex_date, "HAUC") == []


# ---------------------------------------------------------------------------
# find_most_recent_rinex — the integration walker
# ---------------------------------------------------------------------------


def _make_layout(
    base: Path, station: str, year: int, month_name: str, files: list[str]
) -> Path:
    """Build ``<base>/<YYYY>/<mon>/<STA>/15s_24hr/rinex/<file>`` for tests."""
    d = base / str(year) / month_name / station / "15s_24hr" / "rinex"
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).touch()
    return d


def test_find_most_recent_rinex_returns_latest_doy_in_latest_month(
    tmp_path: Path,
):
    """When multiple files exist in the latest populated month, the one
    with the highest DOY wins."""
    _make_layout(
        tmp_path,
        "HAUC",
        2026,
        "sep",
        ["HAUC2540.26D", "HAUC2600.26D", "HAUC2660.26D"],
    )
    result = find_most_recent_rinex("HAUC", base_dir=tmp_path)
    assert result is not None
    assert result.name == "HAUC2660.26D"


def test_find_most_recent_rinex_picks_latest_month_in_latest_year(
    tmp_path: Path,
):
    """Two months populated in the same year — the later month wins."""
    _make_layout(tmp_path, "HAUC", 2026, "jan", ["HAUC0050.26D"])
    _make_layout(tmp_path, "HAUC", 2026, "oct", ["HAUC2750.26D"])
    result = find_most_recent_rinex("HAUC", base_dir=tmp_path)
    assert result is not None
    assert result.parent.name == "rinex"
    assert result.name == "HAUC2750.26D"


def test_find_most_recent_rinex_picks_latest_year(tmp_path: Path):
    """A 2026 file should beat any 2025 file."""
    _make_layout(tmp_path, "HAUC", 2025, "dec", ["HAUC3650.25D"])
    _make_layout(tmp_path, "HAUC", 2026, "jan", ["HAUC0010.26D"])
    result = find_most_recent_rinex("HAUC", base_dir=tmp_path)
    assert result is not None
    # Year 2026 (later) trumps month 'dec' in 2025.
    assert "2026" in str(result)
    assert result.name == "HAUC0010.26D"


def test_find_most_recent_rinex_missing_base_returns_none(tmp_path: Path):
    """Operator points --rinex-base-dir at a path that doesn't exist
    (typo, wrong host) → return None, not crash."""
    assert find_most_recent_rinex("HAUC", base_dir=tmp_path / "missing") is None


def test_find_most_recent_rinex_no_files_returns_none(tmp_path: Path):
    """Layout structure exists but the rinex/ dir is empty."""
    _make_layout(tmp_path, "HAUC", 2026, "sep", [])
    assert find_most_recent_rinex("HAUC", base_dir=tmp_path) is None


def test_find_most_recent_rinex_falls_through_to_earlier_month(tmp_path: Path):
    """Newer month directory exists but is empty (e.g. nothing arrived
    yet this month) → walker steps backward to the earlier populated
    month."""
    _make_layout(tmp_path, "HAUC", 2026, "sep", ["HAUC2660.26D"])
    _make_layout(tmp_path, "HAUC", 2026, "oct", [])  # exists but empty
    result = find_most_recent_rinex("HAUC", base_dir=tmp_path)
    assert result is not None
    assert result.name == "HAUC2660.26D"


def test_months_constant_is_lowercase_calendar_order():
    """Smoke check on the constant — used to walk archive dirs in
    chronological order. Order matters for the descending-walk in
    find_most_recent_rinex."""
    assert MONTHS[0] == "jan"
    assert MONTHS[-1] == "dec"
    assert len(MONTHS) == 12


# ---------------------------------------------------------------------------
# _parse_hourly_raw_date — PR #10
# ---------------------------------------------------------------------------


def test_parse_hourly_raw_date_extracts_timestamp():
    """``STA<YYYY><MM><DD><HHMM>...`` → full datetime including HH:MM."""
    assert _parse_hourly_raw_date("HAUC202604211530.T02", "HAUC") == datetime(
        2026, 4, 21, 15, 30
    )


def test_parse_hourly_raw_date_accepts_any_suffix():
    """The hourly parser only anchors on the prefix — any extension /
    suffix after the 12-digit timestamp is ignored."""
    assert _parse_hourly_raw_date("HAUC202604211530.tar.gz", "HAUC") == datetime(
        2026, 4, 21, 15, 30
    )


def test_parse_hourly_raw_date_rejects_wrong_station():
    assert _parse_hourly_raw_date("RHOF202604211530.T02", "HAUC") is None


def test_parse_hourly_raw_date_rejects_invalid_calendar():
    """Month 13 → ValueError inside the datetime constructor → return None
    instead of raising."""
    assert _parse_hourly_raw_date("HAUC202613211530.T02", "HAUC") is None


def test_parse_hourly_raw_date_rejects_non_timestamp_prefix():
    assert _parse_hourly_raw_date("HAUCnotastamp.T02", "HAUC") is None


# ---------------------------------------------------------------------------
# find_station_archive_range — PR #10
# ---------------------------------------------------------------------------


def _make_daily_layout(
    base: Path, station: str, year: int, month_name: str, files: list[str]
) -> Path:
    d = base / str(year) / month_name / station / "15s_24hr" / "rinex"
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).touch()
    return d


def _make_hourly_layout(
    base: Path, station: str, year: int, month_name: str, files: list[str]
) -> Path:
    d = base / str(year) / month_name / station / "1Hz_1hr" / "raw"
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).touch()
    return d


def test_find_station_archive_range_daily_only(tmp_path: Path):
    """Daily files in two years — first is earliest in earliest year,
    last is latest in latest year."""
    _make_daily_layout(tmp_path, "HAUC", 2024, "mar", ["HAUC0750.24D"])
    _make_daily_layout(tmp_path, "HAUC", 2026, "sep", ["HAUC2660.26D"])
    r = find_station_archive_range("HAUC", base_dir=tmp_path)
    assert r["first"] == datetime(2024, 3, 15)
    assert r["last"] == datetime(2026, 9, 23)
    assert r["15s_24hr"]["first"] == datetime(2024, 3, 15)
    assert r["15s_24hr"]["last"] == datetime(2026, 9, 23)
    assert r["1Hz_1hr"]["first"] is None
    assert r["1Hz_1hr"]["last"] is None


def test_find_station_archive_range_hourly_only(tmp_path: Path):
    _make_hourly_layout(tmp_path, "HAUC", 2025, "feb", ["HAUC202502010800.T02"])
    _make_hourly_layout(tmp_path, "HAUC", 2025, "dec", ["HAUC202512311959.T02"])
    r = find_station_archive_range("HAUC", base_dir=tmp_path)
    assert r["first"] == datetime(2025, 2, 1, 8, 0)
    assert r["last"] == datetime(2025, 12, 31, 19, 59)


def test_find_station_archive_range_combines_kinds(tmp_path: Path):
    """When both kinds exist, the overall first/last spans both
    layouts. Hildur's worked example pattern: 1 Hz raw often captures
    the true first/last minute of service."""
    _make_daily_layout(tmp_path, "HAUC", 2007, "sep", ["HAUC2450.07D"])
    _make_daily_layout(tmp_path, "HAUC", 2026, "apr", ["HAUC1110.26D"])
    _make_hourly_layout(tmp_path, "HAUC", 2009, "jan", ["HAUC200901010001.T02"])
    _make_hourly_layout(tmp_path, "HAUC", 2018, "feb", ["HAUC201802231230.T02"])

    r = find_station_archive_range("HAUC", base_dir=tmp_path)
    # Overall first = earliest of (2007-09-02 daily, 2009-01-01 hourly)
    assert r["first"] == datetime(2007, 9, 2)
    # Overall last = latest of (2026-04-21 daily, 2018-02-23 hourly)
    assert r["last"] == datetime(2026, 4, 21)
    # Per-kind details preserved.
    assert r["15s_24hr"]["last"] == datetime(2026, 4, 21)
    assert r["1Hz_1hr"]["last"] == datetime(2018, 2, 23, 12, 30)


def test_find_station_archive_range_missing_base_returns_empty(tmp_path: Path):
    r = find_station_archive_range("HAUC", base_dir=tmp_path / "missing")
    assert r["first"] is None
    assert r["last"] is None
    assert r["15s_24hr"] == {}
    assert r["1Hz_1hr"] == {}


def test_find_station_archive_range_empty_layout_returns_nones(tmp_path: Path):
    """Layout dirs exist but no recognised files → all None."""
    _make_daily_layout(tmp_path, "HAUC", 2026, "sep", [])
    r = find_station_archive_range("HAUC", base_dir=tmp_path)
    assert r["first"] is None
    assert r["last"] is None
