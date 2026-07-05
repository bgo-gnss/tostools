"""Regression: the header rewrite must keep a newline between END OF HEADER and
the first epoch line.

read_rinex_header returns the header ending at "END OF HEADER" with no trailing
newline; _write_rinex_file concatenates it with the (epoch-first) data bytes. If
the boundary newline is dropped the lines glue ("END OF HEADER> 2026 ...") — valid
RINEX2/3 structure breaks and downstream RNX2CRX fails ("ERROR when reading line
N"). This bit EPOS dissemination (set-header path) for every station.
"""

import logging

from tostools.rinex.corrector import _write_rinex_file

_HDR = (
    "     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n"
    "RHOF                                                        MARKER NAME\n"
    "                                                            END OF HEADER\n"
)
_DATA = "> 2026 06 26 00 00  0.0000000  0  1\nG01  25213991.484 6\n"


def test_boundary_newline_preserved(tmp_path):
    src = tmp_path / "RHOF0010.26o"
    src.write_bytes((_HDR + _DATA).encode())

    # new_header as read_rinex_header yields it: ends at END OF HEADER, no newline.
    new_header = (
        "     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n"
        "RHOF00ISL                                                   MARKER NAME\n"
        "                                                            END OF HEADER"
    )
    out = tmp_path / "out.26o"
    _write_rinex_file(src, new_header, out, logging.getLogger("t"))

    text = out.read_text()
    assert "END OF HEADER\n> 2026" in text, "epoch line must start on its own line"
    assert "END OF HEADER>" not in text, "header and epoch must not be glued"
    # data section preserved verbatim
    assert "G01  25213991.484 6" in text


def test_boundary_newline_not_doubled(tmp_path):
    src = tmp_path / "s.26o"
    src.write_bytes((_HDR + _DATA).encode())
    # new_header already newline-terminated → must not add a second blank line.
    new_header = _HDR  # ends with END OF HEADER + "\n"
    out = tmp_path / "o.26o"
    _write_rinex_file(src, new_header, out, logging.getLogger("t"))
    assert "END OF HEADER\n\n>" not in out.read_text()
    assert "END OF HEADER\n> 2026" in out.read_text()
