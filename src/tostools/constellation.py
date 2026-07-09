"""Extract the GNSS constellations a receiver actually records, from data.

TOS carries per-receiver constellation toggles (``GPS``/``GLO``/``GAL``/``BDS``/
``QZSS``/``SBAS``/``IRN``), but nothing verifies them against what the receiver
is really tracking — so they drift or (usually) sit empty. This module reads the
constellation set from the archive, which the ``constellations`` audit then
cross-checks against TOS.

Ground-truth priority (per the operator convention):

1. **Live receiver query** — authoritative for the *current* config (PolaRX5;
   done receivers-side via ``rec-config``). Out of scope here (no receiver
   access from tostools); the audit notes it as the preferred confirmation.
2. **Raw decode** (SBF) — the actual recorded systems, for historical periods.
   Also out of scope for v1 (needs a decoder); a follow-up.
3. **RINEX header** — this module. Reliable for **RINEX 3** (per-system
   ``SYS / # / OBS TYPES`` lines). **RINEX 2 can UNDER-report**: re-rinexing a
   multi-constellation raw into a single-constellation R2 is common, and the R2
   header carries no per-system list — so an R2 reading is flagged
   ``reliable=False`` and must be confirmed against raw / the live receiver
   before concluding a system is absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Optional, Union

# RINEX-3 satellite-system letter → TOS constellation attribute code.
RINEX3_SYS_TO_CODE = {
    "G": "GPS",
    "R": "GLO",
    "E": "GAL",
    "C": "BDS",
    "J": "QZSS",
    "S": "SBAS",
    "I": "IRN",
}

# All TOS constellation codes, canonical order (baseline first).
TOS_CONSTELLATION_CODES = ("GPS", "GLO", "GAL", "BDS", "QZSS", "SBAS", "IRN")

_VERSION_LABEL = "RINEX VERSION / TYPE"
_SYS_OBS_LABEL = "SYS / # / OBS TYPES"
_R2_TYPES_LABEL = "# / TYPES OF OBSERV"


@dataclass(frozen=True)
class ConstellationReading:
    """The constellation set read from one RINEX header.

    ``reliable`` is True only for a RINEX-3 header (per-system OBS-TYPES lines);
    a RINEX-2 header can under-report, so ``reliable=False`` means "systems is a
    lower bound — confirm absence against raw / live receiver".
    """

    version: Optional[float]
    systems: FrozenSet[str]  # TOS codes, e.g. {"GPS", "GLO", "GAL"}
    reliable: bool
    source_path: Optional[str] = None


def _parse_version(line: str) -> Optional[float]:
    """First whitespace token of the VERSION/TYPE line as a float."""
    tok = line.split()[:1]
    if not tok:
        return None
    try:
        return float(tok[0])
    except ValueError:
        return None


def systems_from_header(header_text: str) -> ConstellationReading:
    """Extract the constellation set from RINEX header text.

    RINEX 3: collect the leading system letter of every ``SYS / # / OBS TYPES``
    line (continuation lines have a blank first column and are skipped).
    RINEX 2: no per-system list exists in the header → ``reliable=False`` and a
    best-effort set from the single satellite-system char (``G``/``R``; ``M`` =
    mixed → empty, since the exact set isn't in the header).
    """
    version: Optional[float] = None
    systems: set = set()
    sat_system_char: Optional[str] = None

    for raw in header_text.splitlines():
        if _VERSION_LABEL in raw:
            version = _parse_version(raw)
            # Col 40 of the VERSION/TYPE line carries the satellite-system char
            # (G/R/E/C/M/…); used only as the R2 best-effort fallback.
            field = raw[:60]
            m = re.search(r"\b([GRECJSIM])\b", field[20:])
            if m:
                sat_system_char = m.group(1)
        elif _SYS_OBS_LABEL in raw:
            letter = raw[:1]
            code = RINEX3_SYS_TO_CODE.get(letter)
            if code:
                systems.add(code)

    if version is not None and version >= 3.0:
        return ConstellationReading(
            version=version, systems=frozenset(systems), reliable=True
        )

    # RINEX 2 (or unknown): header cannot enumerate the set reliably.
    best_effort: set = set()
    if sat_system_char in RINEX3_SYS_TO_CODE:
        best_effort.add(RINEX3_SYS_TO_CODE[sat_system_char])
    return ConstellationReading(
        version=version, systems=frozenset(best_effort), reliable=False
    )


def read_constellations(path: Union[str, Path]) -> Optional[ConstellationReading]:
    """Read the constellation set from an archived RINEX file (``.Z``/``.gz``).

    Reuses the fast header streamer from ``receiver_timeline`` (stops at
    ``END OF HEADER``); falls back to the robust shared reader for uncompressed
    files. Returns ``None`` when no header can be read.
    """
    from .receiver_timeline import _fast_header_text
    from .rinex.reader import read_rinex_header

    path = Path(path)
    text = _fast_header_text(path)
    if text is None:
        parsed = read_rinex_header(str(path))
        if not parsed or "header" not in parsed:
            return None
        text = "\n".join(parsed["header"])
    reading = systems_from_header(text)
    return ConstellationReading(
        version=reading.version,
        systems=reading.systems,
        reliable=reading.reliable,
        source_path=str(path),
    )
