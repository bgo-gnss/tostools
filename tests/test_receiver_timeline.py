"""Tests for tostools.receiver_timeline (RINEX-header receiver/firmware timeline)."""

from datetime import date, timedelta
from pathlib import Path

from tostools.archive import ArchiveDay
from tostools.receiver_timeline import (
    ReceiverHeader,
    ReceiverSegment,
    _norm_fw,
    _norm_serial,
    _segment_range,
    current_receiver_install_date,
    parse_receiver_line,
)


# --------------------------------------------------------------------------- parse
class TestParse:
    def test_real_lines(self):
        line = "3016143             SEPT POLARX5        5.3.0               REC # / TYPE / VERS"
        h = parse_receiver_line(line)
        assert (h.serial, h.rtype, h.firmware) == ("3016143", "SEPT POLARX5", "5.3.0")

    def test_unknown_serial_asterisks(self):
        line = "******              TRIMBLE 4700        1.12                REC # / TYPE / VERS"
        h = parse_receiver_line(line)
        assert h.rtype == "TRIMBLE 4700"
        assert _norm_serial(h.serial) is None  # ****** → unknown

    def test_not_a_rec_line(self):
        assert (
            parse_receiver_line(
                "     2.11           OBSERVATION DATA    M   RINEX VERSION / TYPE"
            )
            is None
        )

    def test_garbled_short(self):
        assert (
            parse_receiver_line("REC # / TYPE / VERS") is None
        )  # label only, no fields


# ----------------------------------------------------------------------- normalize
class TestNormalizeFirmware:
    def test_nav_sig_collapses_to_plain(self):
        # the OLKE phantom-boundary case: same fw written two ways
        assert _norm_fw("Nav 1.12 Sig 0.00") == _norm_fw("1.12")

    def test_np_sp_collapses(self):
        assert _norm_fw("NP 4.62 / SP 4.62") == _norm_fw("4.62")

    def test_septentrio_two_digit_minor(self):
        assert _norm_fw("5.50") == _norm_fw("5.5.0")

    def test_three_part_unchanged(self):
        assert _norm_fw("5.3.0") == "5.3.0"


class TestNormalizeSerial:
    def test_unknown_and_synthetic_strip(self):
        for v in ("******", "0000000000", "antenna-RVIT20150625", "----"):
            assert _norm_serial(v) is None

    def test_real_pass(self):
        for v in ("3016143", "5218K84655", "20147817"):
            assert _norm_serial(v) == v


class TestKey:
    def test_same_receiver_diff_firmware_differs(self):
        a = ReceiverHeader("3016143", "SEPT POLARX5", "5.3.0")
        b = ReceiverHeader("3016143", "SEPT POLARX5", "5.6.0")
        assert a.key != b.key

    def test_nav_sig_firmware_same_key(self):
        a = ReceiverHeader("7817", "TRIMBLE 4700", "Nav 1.12 Sig 0.00")
        b = ReceiverHeader("7817", "TRIMBLE 4700", "1.12")
        assert a.key == b.key  # no phantom boundary


# --------------------------------------------------------------- binary-search split
def _days(seq: str):
    """One ArchiveDay per char; file_path encodes the receiver letter."""
    base = date(2000, 1, 1)
    return [
        ArchiveDay(obs_date=base + timedelta(days=i), family="rinex", file_path=Path(c))
        for i, c in enumerate(seq)
    ]


def _reader(path: Path):
    """Synthetic header read: letter → header, '-' → None (unreadable)."""
    c = str(path)
    if c == "-":
        return None
    return ReceiverHeader(serial=c, rtype=c, firmware=c)


def _segs(seq: str):
    days = _days(seq)
    return _segment_range(days, 0, len(days) - 1, _reader, {})


class TestSegmentRange:
    def test_single_segment(self):
        s = _segs("AAAA")
        assert len(s) == 1
        assert s[0].start == date(2000, 1, 1) and s[0].end == date(2000, 1, 4)
        assert s[0].header.rtype == "A"

    def test_single_day(self):
        assert len(_segs("A")) == 1

    def test_one_boundary(self):
        s = _segs("AAAABBBB")
        assert [seg.header.rtype for seg in s] == ["A", "B"]
        assert s[0].end == date(2000, 1, 4) and s[1].start == date(2000, 1, 5)

    def test_boundary_at_zero_and_last(self):
        assert [seg.header.rtype for seg in _segs("AB")] == ["A", "B"]

    def test_multiple_boundaries(self):
        assert [seg.header.rtype for seg in _segs("AAABBBCCC")] == ["A", "B", "C"]

    def test_revert_is_caught(self):
        # X→Y→X reverts are caught (the mid-sample inspects the interior even
        # when the endpoints match), down to a single interior day at a midpoint:
        assert [seg.header.rtype for seg in _segs("AAABBBAAA")] == ["A", "B", "A"]
        assert [seg.header.rtype for seg in _segs("ABA")] == ["A", "B", "A"]

    def test_residual_limitation_one_day_off_midpoint(self):
        # ACCEPTED residual limit: a ~1-day run that aligns with no recursion
        # midpoint can still be missed. Real receiver runs span months over daily
        # sampling, so this never bites; documented so it's intentional.
        # "ABAAAAA": the 1-day B at index 1 never lands on a sampled midpoint.
        assert [seg.header.rtype for seg in _segs("ABAAAAA")] == ["A"]

    def test_none_holes_within_run_are_skipped(self):
        # holes don't create phantom boundaries
        assert [seg.header.rtype for seg in _segs("A--AA")] == ["A"]

    def test_none_hole_at_endpoints(self):
        assert [seg.header.rtype for seg in _segs("-AAA")] == ["A"]
        assert [seg.header.rtype for seg in _segs("AAA-")] == ["A"]

    def test_none_at_boundary(self):
        assert [seg.header.rtype for seg in _segs("AA--BB")] == ["A", "B"]

    def test_all_none_is_empty(self):
        assert _segs("----") == []


# ------------------------------------------------------ current_receiver_install_date
class TestCurrentReceiverInstallDate:
    """The install date must look PAST firmware-only segment boundaries to the
    date the physical receiver UNIT first appeared (the OLKE bug: PolaRX5 sn
    3016143 fragmented across fw 5.1.1/5.3.0/5.6.0 must date to 2017, not the
    latest bump)."""

    def _seg(self, start, end, rtype, serial, fw):
        return ReceiverSegment(start, end, ReceiverHeader(serial, rtype, fw))

    def test_back_coalesces_across_firmware_bumps(self):
        timeline = [
            self._seg(
                date(2013, 2, 28),
                date(2017, 6, 25),
                "TRIMBLE NETR9",
                "5218K84655",
                "4.62",
            ),
            self._seg(
                date(2017, 7, 8), date(2019, 10, 14), "SEPT POLARX5", "3016143", "5.1.1"
            ),
            self._seg(
                date(2019, 10, 15),
                date(2026, 5, 17),
                "SEPT POLARX5",
                "3016143",
                "5.3.0",
            ),
            self._seg(
                date(2026, 5, 18), date(2026, 5, 23), "SEPT POLARX5", "3016143", "5.6.0"
            ),
        ]
        assert current_receiver_install_date(timeline) == date(2017, 7, 8)

    def test_stops_at_serial_change(self):
        """A genuine serial change (different unit, same model) is a boundary."""
        timeline = [
            self._seg(
                date(2018, 1, 1), date(2019, 1, 1), "SEPT POLARX5", "1111111", "5.1.1"
            ),
            self._seg(
                date(2019, 1, 2), date(2020, 1, 1), "SEPT POLARX5", "2222222", "5.3.0"
            ),
        ]
        assert current_receiver_install_date(timeline) == date(2019, 1, 2)

    def test_unknown_serial_is_wildcard(self):
        """A None (garbled) serial mid-run does not break the unit run."""
        timeline = [
            self._seg(
                date(2017, 7, 8), date(2018, 1, 1), "SEPT POLARX5", "3016143", "5.1.1"
            ),
            self._seg(
                date(2018, 1, 2), date(2018, 2, 1), "SEPT POLARX5", None, "5.1.1"
            ),
            self._seg(
                date(2018, 2, 2), date(2020, 1, 1), "SEPT POLARX5", "3016143", "5.3.0"
            ),
        ]
        assert current_receiver_install_date(timeline) == date(2017, 7, 8)

    def test_single_segment(self):
        timeline = [
            self._seg(
                date(2024, 1, 1), date(2024, 6, 1), "SEPT POLARX5", "3016143", "5.5.0"
            )
        ]
        assert current_receiver_install_date(timeline) == date(2024, 1, 1)

    def test_empty(self):
        assert current_receiver_install_date([]) is None


# ----------------------------------------------------- streaming header reader
class TestFastHeaderRead:
    """`read_receiver_header` streams gzip -dc and stops at END OF HEADER,
    falling back to the shared reader otherwise. Both paths must yield the
    same parsed receiver identity."""

    _HEADER = (
        "     3.04           OBSERVATION DATA    M                   "
        "RINEX VERSION / TYPE\n"
        "3016143             SEPT POLARX5        5.6.0               "
        "REC # / TYPE / VERS\n"
        "                                                            "
        "END OF HEADER\n"
    )

    def _write_gz(self, tmp_path, name, header, tail_mb=0):
        import gzip

        body = header + ("X" * (tail_mb * 1024 * 1024))  # bulk after the header
        p = tmp_path / name
        with gzip.open(p, "wb") as f:
            f.write(body.encode())
        return p

    def test_fast_path_gz_parses_rec_line(self, tmp_path):
        from tostools.receiver_timeline import read_receiver_header

        p = self._write_gz(tmp_path, "OLKE1410.26D.gz", self._HEADER, tail_mb=2)
        h = read_receiver_header(p)
        assert h is not None
        assert (h.serial, h.rtype, h.firmware) == ("3016143", "SEPT POLARX5", "5.6.0")

    def test_fast_path_stops_early(self, tmp_path):
        """Header is found without inflating a large tail — _fast_header_text
        returns only up to END OF HEADER, not the whole 3 MB body."""
        from tostools.receiver_timeline import _fast_header_text

        p = self._write_gz(tmp_path, "OLKE1420.26D.gz", self._HEADER, tail_mb=3)
        text = _fast_header_text(p)
        assert text is not None
        assert text.rstrip().endswith("END OF HEADER")
        assert len(text) < 100 * 1024  # nowhere near the 3 MB body

    def test_uncompressed_name_falls_back(self, tmp_path):
        """A non-.Z/.gz name isn't fast-pathed → _fast_header_text returns None
        (caller falls back to the shared reader)."""
        from tostools.receiver_timeline import _fast_header_text

        p = tmp_path / "OLKE1410.26o"
        p.write_text(self._HEADER)
        assert _fast_header_text(p) is None

    def test_no_marker_bails_to_none(self, tmp_path):
        """A .gz stream with no END OF HEADER bails (→ fallback), doesn't hang."""
        from tostools.receiver_timeline import _fast_header_text

        p = self._write_gz(tmp_path, "junk.gz", "no marker here\n", tail_mb=0)
        assert _fast_header_text(p) is None
