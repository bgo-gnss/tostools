"""Tests for tostools.receiver_timeline (RINEX-header receiver/firmware timeline)."""

from datetime import date, timedelta
from pathlib import Path

from tostools.archive import ArchiveDay
from tostools.receiver_timeline import (
    ReceiverHeader,
    _norm_fw,
    _norm_serial,
    _segment_range,
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
