"""Tests for the per-period constellation reconstruction (binary search)."""

from datetime import date, timedelta
from pathlib import Path

from tostools.constellation import ConstellationReading
from tostools.constellation_history import (
    ConstellationSegment,
    segment_by_constellation,
    system_first_seen,
)


def _seg(df, dt, systems):
    return ConstellationSegment(
        date_from=df,
        date_to=dt,
        systems=systems,
        reliable=True,
        n_files=1,
        first_file=Path("/x"),
        last_file=Path("/x"),
    )


GPS = frozenset({"GPS"})
GPS_GLO = frozenset({"GPS", "GLO"})
GPS_GLO_GAL = frozenset({"GPS", "GLO", "GAL"})


def _files(n: int):
    """n dated files; path name encodes the index so the mock can look it up."""
    d0 = date(2020, 1, 1)
    return [(d0 + timedelta(days=i), Path(f"/x/f{i}.rnx")) for i in range(n)]


def _reader(systems_by_index, *, reliable=True, unreadable=frozenset()):
    """Mock read_fn keyed by file index; records how many reads happened."""
    calls = {"n": 0}

    def read(path: Path):
        calls["n"] += 1
        idx = int(path.stem[1:])
        if idx in unreadable:
            return None
        return ConstellationReading(
            version=3.04, systems=systems_by_index(idx), reliable=reliable
        )

    return read, calls


def test_uniform_period_is_one_segment_and_two_reads():
    files = _files(100)
    read, calls = _reader(lambda i: GPS)
    segs = segment_by_constellation(files, read)
    assert len(segs) == 1
    assert segs[0].systems == GPS
    assert segs[0].date_from == files[0][0]
    assert segs[0].date_to == files[-1][0]
    assert segs[0].n_files == 100
    assert calls["n"] == 2  # endpoints only — the whole point


def test_single_change_found_by_binary_search():
    # indices 0..59 = GPS, 60..99 = GPS+GLO (boundary between 59 and 60)
    files = _files(100)
    read, calls = _reader(lambda i: GPS if i < 60 else GPS_GLO)
    segs = segment_by_constellation(files, read)
    assert [s.systems for s in segs] == [GPS, GPS_GLO]
    assert segs[0].date_to == files[59][0]
    assert segs[1].date_from == files[60][0]
    assert calls["n"] < 20  # log2(100) ~ 7, not 100


def test_multiple_changes():
    # GPS [0..29], GPS+GLO [30..69], GPS+GLO+GAL [70..99]
    def sysfn(i):
        if i < 30:
            return GPS
        if i < 70:
            return GPS_GLO
        return GPS_GLO_GAL

    files = _files(100)
    read, _ = _reader(sysfn)
    segs = segment_by_constellation(files, read)
    assert [s.systems for s in segs] == [GPS, GPS_GLO, GPS_GLO_GAL]
    assert segs[0].date_to == files[29][0]
    assert segs[1].date_from == files[30][0]
    assert segs[1].date_to == files[69][0]
    assert segs[2].date_from == files[70][0]


def test_reliability_flag_propagates_r2():
    files = _files(10)
    read, _ = _reader(lambda i: GPS, reliable=False)
    segs = segment_by_constellation(files, read)
    assert len(segs) == 1
    assert segs[0].reliable is False


def test_unreadable_endpoints_are_skipped():
    # first two + last one unreadable; interior all GPS
    files = _files(20)
    read, _ = _reader(lambda i: GPS, unreadable=frozenset({0, 1, 19}))
    segs = segment_by_constellation(files, read)
    assert len(segs) == 1
    assert segs[0].date_from == files[2][0]  # first readable
    assert segs[0].date_to == files[18][0]  # last readable


def test_empty_and_all_unreadable():
    assert segment_by_constellation([]) == []
    files = _files(5)
    read, _ = _reader(lambda i: GPS, unreadable=frozenset(range(5)))
    assert segment_by_constellation(files, read) == []


def test_empty_systems_reading_is_skipped_not_a_segment():
    # A patch of empty-systems readings (unparseable headers, e.g. the NYLA
    # Dec-2022 files) must NOT anchor spurious segments — the surrounding
    # readable run covers those dates.
    def sysfn(i):
        if 40 <= i < 50:
            return frozenset()  # empty → treated as unreadable
        return GPS_GLO if i < 45 else GPS_GLO_GAL

    files = _files(100)
    read, _ = _reader(sysfn)
    segs = segment_by_constellation(files, read)
    # Only the two real states survive; no empty segment between them.
    assert [s.systems for s in segs] == [GPS_GLO, GPS_GLO_GAL]
    assert all(s.systems for s in segs)


def test_system_first_seen_monotonic_additions():
    # GPS+GLO from Jul, GAL added in Dec — GAL's date_from is the Dec segment.
    segs = [
        _seg(date(2022, 7, 22), date(2022, 12, 15), GPS_GLO),
        _seg(date(2022, 12, 16), date(2023, 2, 8), GPS_GLO_GAL),
    ]
    first = system_first_seen(segs)
    assert first["GPS"] == date(2022, 7, 22)
    assert first["GLO"] == date(2022, 7, 22)
    assert first["GAL"] == date(2022, 12, 16)


def test_system_first_seen_drops_install_day_blip():
    # 1-day install blip tracks BDS+SBAS; the settled state is GPS+GLO+GAL.
    blip = frozenset({"BDS", "GAL", "GLO", "GPS", "SBAS"})
    segs = [
        _seg(date(2023, 2, 9), date(2023, 2, 9), blip),  # 1 day → dropped
        _seg(date(2023, 2, 10), date(2026, 7, 9), GPS_GLO_GAL),
    ]
    first = system_first_seen(segs, min_segment_days=4)
    assert set(first) == {"GPS", "GLO", "GAL"}  # BDS/SBAS blip not recorded
    assert first["GAL"] == date(2023, 2, 10)


def test_system_first_seen_all_short_keeps_longest():
    segs = [
        _seg(date(2023, 1, 1), date(2023, 1, 1), GPS),
        _seg(date(2023, 1, 2), date(2023, 1, 3), GPS_GLO),  # longest (2 days)
    ]
    first = system_first_seen(segs, min_segment_days=10)
    assert set(first) == {"GPS", "GLO"}


def test_single_file_period():
    files = _files(1)
    read, calls = _reader(lambda i: GPS_GLO)
    segs = segment_by_constellation(files, read)
    assert len(segs) == 1
    assert segs[0].systems == GPS_GLO
    assert segs[0].date_from == segs[0].date_to == files[0][0]
