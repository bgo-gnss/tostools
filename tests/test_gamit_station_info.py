"""Tests for the GAMIT SOPAC station.info parser.

No network — pure parsing. Fixture lines are real-format VOTT campaign
occupations plus edge cases (open sentinel, all-zero serial, malformed rows).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tostools.standards.gamit_station_info import (
    Occupation,
    format_occupation,
    parse_line,
    parse_station_info,
)

HEADER = (
    "*SITE  Station Name      Session Start      Session Stop       "
    "Ant Ht   HtCod  Ant N    Ant E    Receiver Type         Vers      "
    "            SwVer  Receiver SN           Antenna Type     Dome   Antenna SN"
)

# Real VOTT lines from station.info.sopac.apr05 (columns preserved verbatim).
VOTT_2012 = (
    " VOTT  Vottur            2012 155 00 00 00  2012 165 00 00 00   0.0000  "
    "DHARP   0.0000  -0.0004  TRIMBLE 5700          2.01                   "
    "2.01  0220331856            TRM41249.00      NONE   60004115"
)
VOTT_2014 = (
    " VOTT  Vottur            2014 150 00 00 00  2014 160 00 00 00   0.0000  "
    "DHARP   0.0000   0.0000  TRIMBLE NETR9         NP 4.62 / SP 4.62      "
    "4.62  5229K50746            TRM57971.00      NONE   5000117697"
)
# Antenna SN is GAMIT's all-zeros "unknown" filler.
VOTT_2016 = (
    " VOTT  Vottur            2016 150 00 00 00  2016 170 00 00 00   0.0000  "
    "DHARP   0.0000   0.0000  TRIMBLE 5700          2.30                   "
    "2.30  0224093032            TRM41249.00      NONE   0000000000"
)
# Open-ended (continuous) session: 9999 999 stop sentinel.
OPEN_LINE = (
    " REYK  Reykjavik         2020 001 00 00 00  9999 999 00 00 00   0.0000  "
    "DHARP   0.0000   0.0000  SEPT POLARX5          5.3.0                  "
    "5.30  3012345               LEIAR25.R4       LEIT   725281"
)
SEPARATOR = (
    " ----  ----------------  9999 999 00 00 00  9999 999 00 00 00   0.0000  "
    "DHARP   0.0000   0.0000  ---                   ---                    "
    "---   ---                   ---              ---    ---"
)
MALFORMED_DOY = (
    " BARC  Bardarbunga Cald  2014 244200 00 00  9999 999 00 00 00   0.0000  "
    "DHARP   0.0000   0.0000  TRIMBLE NETR9         5.0                    "
    "5.00  9999999               TRM59800.00      NONE   1234567"
)


def test_parse_single_occupation_fields():
    occ = parse_line(VOTT_2012)
    assert isinstance(occ, Occupation)
    assert occ.marker == "VOTT"
    assert occ.station_name == "Vottur"
    # DOY 155 of leap-year 2012 == 3 June.
    assert occ.time_from == datetime(2012, 6, 3, 0, 0, 0)
    assert occ.time_to == datetime(2012, 6, 13, 0, 0, 0)
    assert occ.receiver_type == "TRIMBLE 5700"
    assert occ.receiver_sn == "0220331856"
    assert occ.antenna_type == "TRM41249.00"
    assert occ.dome == "NONE"
    assert occ.antenna_sn == "60004115"
    assert occ.antenna_height == "0.0000"
    assert occ.htcod == "DHARP"
    assert occ.is_open is False


def test_multiword_receiver_and_vers_fields():
    occ = parse_line(VOTT_2014)
    assert occ.receiver_type == "TRIMBLE NETR9"
    assert occ.vers == "NP 4.62 / SP 4.62"
    assert occ.swver == "4.62"
    assert occ.receiver_sn == "5229K50746"


def test_all_zero_serial_normalised_to_empty():
    occ = parse_line(VOTT_2016)
    assert occ.antenna_sn == ""  # 0000000000 -> "" (unknown)


def test_open_ended_session():
    occ = parse_line(OPEN_LINE)
    assert occ.time_from == datetime(2020, 1, 1)
    assert occ.time_to is None
    assert occ.is_open is True
    assert occ.dome == "LEIT"


def test_header_and_separator_skipped():
    assert parse_line(HEADER) is None
    assert parse_line(SEPARATOR) is None
    assert parse_line("") is None
    assert parse_line("* a comment") is None


def test_malformed_line_raises_in_strict_parse_line():
    with pytest.raises(ValueError):
        parse_line(MALFORMED_DOY)


def test_parse_station_info_skips_malformed_by_default():
    lines = [HEADER, VOTT_2012, MALFORMED_DOY, VOTT_2014]
    occs = parse_station_info(lines)
    # Malformed BARC dropped; two good VOTT rows kept.
    assert [o.marker for o in occs] == ["VOTT", "VOTT"]


def test_parse_station_info_strict_reraises():
    with pytest.raises(ValueError):
        parse_station_info([VOTT_2012, MALFORMED_DOY], strict=True)


def test_marker_filter_case_insensitive():
    lines = [VOTT_2012, OPEN_LINE, VOTT_2014]
    occs = parse_station_info(lines, marker="vott")
    assert len(occs) == 2
    assert {o.marker for o in occs} == {"VOTT"}


@pytest.mark.parametrize("line", [VOTT_2012, VOTT_2014, VOTT_2016, OPEN_LINE])
def test_round_trip_parse_format_parse(line):
    """parse → format → parse is stable (acceptance: station.info round-trips)."""
    occ = parse_line(line)
    assert occ is not None
    reformatted = format_occupation(occ)
    occ2 = parse_line(reformatted)
    assert occ2 == occ


def test_format_open_session_uses_sentinel():
    occ = parse_line(OPEN_LINE)
    out = format_occupation(occ)
    assert "9999 999 00 00 00" in out
    assert parse_line(out).time_to is None


def test_full_vott_occupation_sequence():
    lines = [HEADER, VOTT_2012, VOTT_2014, VOTT_2016]
    occs = parse_station_info(lines, marker="VOTT")
    assert len(occs) == 3
    # File order preserved.
    assert [o.time_from.year for o in occs] == [2012, 2014, 2016]
    assert all(o.time_to is not None for o in occs)
