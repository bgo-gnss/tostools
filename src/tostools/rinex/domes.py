"""IERS DOMES number validation — the single discriminator for MARKER NUMBER.

Policy (bgo, 2026-07-13): the RINEX ``MARKER NUMBER`` header field carries the
station's IERS DOMES number and **nothing else**. In the absence of a real DOMES
the line is skipped entirely — never filled with the 4-char station id, which is
what ``MARKER NAME`` already carries. Historically both the archive and the EPOS
dissemination paths fell back to the station id (the "NYLA cross-contamination"),
so most of the fleet had a bogus MARKER NUMBER. This module is the one place that
decides "is this value a real DOMES?", used by every write and compare path.

An IERS DOMES number is exactly 9 characters: a 5-digit area/site code, a single
point-type letter (``M`` monument / ``S`` survey / ``P`` etc.), and a 3-digit
sequence — e.g. ``10230M001``. A bare 4-char station id (``AKUR``) never matches,
so it is treated as "no DOMES".
"""

from __future__ import annotations

import re

# 5-digit code + 1 point-type letter + 3-digit sequence, e.g. 10230M001
_DOMES_RE = re.compile(r"^\d{5}[A-Z]\d{3}$")


def is_iers_domes(value: object) -> bool:
    """True iff *value* is a well-formed IERS DOMES number (e.g. ``10230M001``)."""
    return bool(_DOMES_RE.match(str(value or "").strip().upper()))


def domes_or_skip(value: object) -> str:
    """Return the normalized DOMES (upper, stripped) if *value* is one, else ``""``.

    ``""`` is the "skip the MARKER NUMBER line" signal — a 4-char station id, a
    blank, or any non-DOMES string all collapse to it.
    """
    v = str(value or "").strip().upper()
    return v if _DOMES_RE.match(v) else ""
