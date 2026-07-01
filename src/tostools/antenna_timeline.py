"""Antenna timeline for a station, from archived RINEX headers.

The antenna analog of :mod:`tostools.receiver_timeline`. Where that module reads
``REC # / TYPE / VERS`` to segment receiver/firmware history, this reads
``ANT # / TYPE`` (antenna model + radome + serial) and ``ANTENNA: DELTA H/E/N``
(the ARP eccentricity / antenna height) to segment antenna history — the empirical
truth for ``onboard-station``'s antenna + monument-height fields.

It reuses the generalized binary-search segmenter (``_segment_range`` /
``_coalesce``) and the streaming header reader (``_fast_header_text``) from
:mod:`tostools.receiver_timeline`; the only new code here is the antenna header
parse + normalization. Unlike the receiver timeline there is no brand-run
bounding (antenna changes don't correlate with the raw file extension), so the
header binary-search runs over the whole rinex-only day list.

Equality is on NORMALIZED fields (type via ``to_igs_antenna``, radome upper, serial
through the shared placeholder filter, height rounded to the F14.4 stored
precision) so a boundary is a real change, not formatting noise. A RINEX header
missing either record reads as ``None`` and is treated as "no data, not a
boundary" — never a half-populated header that would fragment against fully-read
neighbours.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .archive import cold_archive_prepath, walk_station_timeline
from .receiver_timeline import (
    _fast_header_text,
    _norm_serial,
    _segment_range,
    read_rinex_header,
)
from .standards.igs_equipment import to_igs_antenna

logger = logging.getLogger(__name__)

_ANT_LABEL = "ANT # / TYPE"
_DELTA_LABEL = "ANTENNA: DELTA H/E/N"


# --------------------------------------------------------------------------- header
@dataclass(frozen=True)
class AntennaHeader:
    serial: Optional[str]
    atype: Optional[str]
    radome: Optional[str]
    delta_h: Optional[float]
    delta_e: Optional[float]
    delta_n: Optional[float]

    @property
    def key(self) -> tuple:
        """Normalized identity — equal keys ⇒ no real antenna change.

        Height is part of the key (a re-seat / height re-entry opens a segment,
        the antenna analog of a firmware bump). Use :func:`current_antenna_unit`
        / :func:`current_antenna_install_date` for the antenna-UNIT view that
        ignores height-only boundaries.
        """
        return (
            _norm_atype(self.atype),
            _norm_radome(self.radome),
            _norm_serial(self.serial),
            self.delta_h,
            self.delta_e,
            self.delta_n,
        )

    @property
    def unit_key(self) -> tuple:
        """Antenna-unit identity (type + radome + serial), height ignored."""
        return (
            _norm_atype(self.atype),
            _norm_radome(self.radome),
            _norm_serial(self.serial),
        )

    @property
    def is_known(self) -> bool:
        """An antenna identity needs a type or a serial.

        Radome is excluded: it normalizes to ``NONE`` (never falsy), so a header
        with only a radome and no model/serial is still "no data", not a boundary.
        """
        return bool(_norm_atype(self.atype) or _norm_serial(self.serial))

    def __str__(self) -> str:
        h = "?" if self.delta_h is None else f"{self.delta_h:.4f}"
        return (
            f"{self.atype or '?'}/{self.radome or 'NONE'} sn={self.serial or '?'} "
            f"h={h}"
        )


def _norm_atype(atype: Optional[str]) -> Optional[str]:
    if not atype:
        return None
    igs = to_igs_antenna(atype.strip())
    return (igs or atype.strip().upper()) or None


def _norm_radome(radome: Optional[str]) -> str:
    """Normalize a radome code; a blank/missing radome is the IGS sentinel ``NONE``.

    RINEX writers spell "no radome" inconsistently — blank in one era, ``NONE`` in
    another — for the *same* physical antenna (observed at RHOF across the
    2026 RINEX3 cutover). Collapsing blank → ``NONE`` (their shared meaning) stops
    that formatting drift from fragmenting one antenna into phantom segments.
    """
    if not radome or not radome.strip():
        return "NONE"
    return radome.strip().upper()


def parse_antenna_lines(header: str) -> Optional[AntennaHeader]:
    """Parse the two antenna records out of a RINEX header block.

    Returns ``None`` unless BOTH ``ANT # / TYPE`` and a parseable
    ``ANTENNA: DELTA H/E/N`` are present — a header missing or garbling either
    record is an incomplete read, treated as no-data (not a boundary), so it
    can never fragment against fully-read neighbours.
    """
    ant_line: Optional[str] = None
    delta_line: Optional[str] = None
    for line in header.splitlines():
        if _ANT_LABEL in line:
            ant_line = line
        elif _DELTA_LABEL in line:
            delta_line = line
    if ant_line is None or delta_line is None:
        return None

    # ANT # / TYPE : A20 (serial) + A20 (16-char model + 4-char radome). The
    # model/radome split is by WHITESPACE, not fixed columns: IGS antenna names
    # carry no internal spaces, while RINEX writers place the radome at varying
    # columns (RHOF's 'TRM57971.00 NONE' vs 'TRM57971.00     NONE' across eras).
    # First token = model, second (if any) = radome; fixed columns mis-split the
    # embedded-radome spelling into the model field.
    body = ant_line[:40]
    if len(body) < 21:
        return None
    serial = body[0:20].strip() or None
    tokens = body[20:40].split()
    atype = tokens[0] if tokens else None
    radome = tokens[1] if len(tokens) > 1 else None

    delta = _parse_delta(delta_line)
    if delta is None:
        return None
    dh, de, dn = delta
    return AntennaHeader(
        serial=serial, atype=atype, radome=radome, delta_h=dh, delta_e=de, delta_n=dn
    )


def _parse_delta(line: str) -> Optional[Tuple[float, float, float]]:
    """Parse ``ANTENNA: DELTA H/E/N`` (3F14.4) → (h, e, n) rounded to 4 dp.

    All three are read together: a present record always carries all three
    (0.0 is a legitimate value), an absent/garbled one yields ``None`` for the
    whole record. Rounding to 4 dp matches the F14.4 stored precision so no
    sub-precision jitter creates phantom boundaries.
    """
    body = line[:42]
    try:
        h = round(float(body[0:14]), 4)
        e = round(float(body[14:28]), 4)
        n = round(float(body[28:42]), 4)
    except (ValueError, IndexError):
        return None
    return h, e, n


def _antenna_text(path) -> Optional[str]:
    """Header text of one archived RINEX file (fast stream, shared-reader fallback)."""
    fast = _fast_header_text(path)
    if fast is not None:
        return fast
    try:
        data = read_rinex_header(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.debug("_antenna_text(%s): %s", path, exc)
        return None
    if not data or "header" not in data:
        return None
    header = data["header"]
    return header if isinstance(header, str) else None


def read_antenna_header(path) -> Optional[AntennaHeader]:
    """Antenna header of one archived RINEX file, or None on any failure."""
    text = _antenna_text(path)
    if text is None:
        return None
    return parse_antenna_lines(text)


# ------------------------------------------------------------------------- timeline
@dataclass(frozen=True)
class AntennaSegment:
    start: date
    end: date
    header: AntennaHeader


def build_antenna_timeline(
    station: str,
    root=None,
    *,
    rate: str = "15s_24hr",
    read_fn: Callable[[Path], Optional[AntennaHeader]] = read_antenna_header,
) -> List[AntennaSegment]:
    """Antenna segments for a station from archived RINEX headers.

    Binary-searches header-key boundaries over the station's rinex-only day list
    (no brand-run bounding — antenna changes don't track the raw extension).
    Segments break on any antenna-key change, including height; collapse
    height-only boundaries with :func:`coalesce_antenna_units` for a unit view.
    """
    archive_root = Path(root) if root is not None else cold_archive_prepath()
    rinex_days = list(
        walk_station_timeline(station, archive_root, rate=rate, prefer_raw=False)
    )
    if not rinex_days:
        return []
    return _segment_range(
        rinex_days,
        0,
        len(rinex_days) - 1,
        read_fn,
        {},
        segment_cls=AntennaSegment,
    )


def _same_antenna_unit(a: tuple, b: tuple) -> bool:
    """Same physical antenna (type + radome + serial), ignoring height.

    Mirrors :func:`receiver_timeline._same_unit`: an unknown serial is a
    wildcard so a garbled read does not mark a unit boundary.
    """
    if a[0] != b[0] or a[1] != b[1]:
        return False
    if a[2] is not None and b[2] is not None and a[2] != b[2]:
        return False
    return True


def coalesce_antenna_units(
    timeline: List[AntennaSegment],
) -> List[AntennaSegment]:
    """Merge adjacent height-only boundaries into one physical-antenna segment."""
    out: List[AntennaSegment] = []
    for s in timeline:
        if out and _same_antenna_unit(out[-1].header.unit_key, s.header.unit_key):
            p = out[-1]
            out[-1] = AntennaSegment(p.start, max(p.end, s.end), p.header)
        else:
            out.append(s)
    return out


def current_antenna_install_date(timeline: List[AntennaSegment]) -> Optional[date]:
    """Date the *current physical antenna* first appears (across height re-entries).

    Walks back from the last segment over height-only boundaries (same
    type+radome+serial) — the antenna analog of
    :func:`receiver_timeline.current_receiver_install_date`. This is the date a
    ``cfg add-antenna`` / ``replace-antenna`` should carry, not the date of the
    latest height re-entry.
    """
    if not timeline:
        return None
    cur = timeline[-1].header.unit_key
    start = timeline[-1].start
    for seg in reversed(timeline[:-1]):
        if _same_antenna_unit(seg.header.unit_key, cur):
            start = seg.start
        else:
            break
    return start
