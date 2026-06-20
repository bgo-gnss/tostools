"""Receiver / firmware timeline for a station, from archived RINEX headers.

Complements :mod:`tostools.archive`'s brand detection. ``detect_brand_transitions``
keys on the *raw* file extension (``.T02``→trimble, ``.sbf``→septentrio) — a fast,
no-decompression locator of receiver SWAPS — but it goes ``ambiguous`` wherever
only RINEX (no raw) was archived, which is exactly where many current receivers
live (e.g. OLKE is RINEX-only since 2017, the whole PolaRX5 era). The RINEX
``REC # / TYPE / VERS`` header carries type / serial / firmware even there, so
reading it is the only way to identify the receiver on rinex-only spans — and the
only way to see *firmware* changes (which never change the file extension).

Efficiency (the hybrid): the brand runs from ``coalesce_brand_runs`` are free
(directory listing) and bound the search — a ``.T02``→``.sbf`` junction is a known
boundary, so we only **binary-search RINEX headers WITHIN each brand run** (and
read the two headers either side of a junction to name the exact models). Headers
are read at O(sub-segments · log n) days, not every day; for a 25-year station
that's a few dozen ``gzip -dc`` reads, not thousands.

Equality is on NORMALIZED fields (firmware ``5.50``≡``5.5.0``, type via
``to_igs_receiver``, placeholder/synthetic serials → unknown) so a boundary is a
real change, not header-formatting noise. A garbled/truncated header reads as
``None`` and is treated as "no data, not a boundary".

This module only *reports* the timeline. Driving the TOS write
(``cfg replace-receiver`` / ``reconcile --push-tos``) is a separate,
operator-reviewed step — RINEX-derived dates are a strong proxy, not gospel.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .archive import (
    ArchiveDay,
    coalesce_brand_runs,
    cold_archive_prepath,
    walk_station_timeline,
)
from .rinex.reader import read_rinex_header
from .standards.igs_equipment import to_igs_receiver

logger = logging.getLogger(__name__)

_REC_LABEL = "REC # / TYPE / VERS"


# --------------------------------------------------------------------------- header
@dataclass(frozen=True)
class ReceiverHeader:
    serial: Optional[str]
    rtype: Optional[str]
    firmware: Optional[str]

    @property
    def key(self) -> tuple:
        """Normalized identity — equal keys ⇒ no real receiver change."""
        return (
            _norm_type(self.rtype),
            _norm_serial(self.serial),
            _norm_fw(self.firmware),
        )

    @property
    def is_known(self) -> bool:
        return any(self.key)

    def __str__(self) -> str:
        return f"{self.rtype or '?'} sn={self.serial or '?'} fw={self.firmware or '?'}"


def _norm_type(rtype: Optional[str]) -> Optional[str]:
    if not rtype:
        return None
    igs = to_igs_receiver(rtype.strip())
    return (igs or rtype.strip().upper()) or None


def _norm_serial(serial: Optional[str]) -> Optional[str]:
    if not serial:
        return None
    s = serial.strip()
    # RINEX "unknown serial": all-asterisks / punctuation, or zeros.
    if not any(ch.isalnum() for ch in s) or set(s) <= {"0"}:
        return None
    # TOS synthetic identifiers ({subtype}-{STN}[-]{YYYYMMDD}).
    if re.match(r"^[a-z]+-[A-Za-z0-9]{4}-?\d{8}$", s, re.IGNORECASE):
        return None
    return s


def _norm_fw(fw: Optional[str]) -> Optional[str]:
    """Firmware compare key — collapse the known vendor formats to one form.

    * Trimble NetR* ``NP 4.62 / SP 4.62`` → ``4.62`` (the NP/nav version).
    * Trimble 4700/4000 ``Nav 1.12 Sig 0.00`` → ``1.12`` (the Nav version; Sig is
      a separate signal-processor rev that tracks Nav, so it's noise here).
    * Septentrio compact two-digit minor ``5.50`` → ``5.5.0``.

    The point is a *stable* key so ``Nav 1.12 Sig 0.00`` and ``1.12`` (the same
    firmware written two ways across the archive) don't fragment into phantom
    segments. The segment still carries the raw string for display.
    """
    if not fw:
        return None
    s = fw.strip()
    m = re.match(r"^(?:NP|Nav)\s+([\d.]+(?:-[\w.]+)?)", s, re.IGNORECASE)
    if m:  # "NP 4.62 / SP 4.62" or "Nav 1.12 Sig 0.00" → the leading version
        s = m.group(1)
    parts = s.split(".")
    if (
        len(parts) == 2
        and parts[0].isdigit()
        and parts[1].isdigit()
        and len(parts[1]) == 2
    ):
        s = f"{parts[0]}.{parts[1][0]}.{parts[1][1]}"
    return s.lower() or None


def parse_receiver_line(line: str) -> Optional[ReceiverHeader]:
    """Parse one ``REC # / TYPE / VERS`` header line (A20,A20,A20)."""
    if _REC_LABEL not in line:
        return None
    body = line[:60]
    if len(body) < 21:
        return None
    serial = body[0:20].strip() or None
    rtype = body[20:40].strip() or None
    firmware = body[40:60].strip() or None
    if serial is None and rtype is None and firmware is None:
        return None
    return ReceiverHeader(serial=serial, rtype=rtype, firmware=firmware)


def read_receiver_header(path) -> Optional[ReceiverHeader]:
    """Receiver header of one archived RINEX file, or None on any failure."""
    try:
        data = read_rinex_header(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.debug("read_receiver_header(%s): %s", path, exc)
        return None
    if not data or "header" not in data:
        return None
    header = data["header"]
    if not isinstance(header, str):
        return None
    for line in header.splitlines():
        if _REC_LABEL in line:
            return parse_receiver_line(line)
    return None


# ------------------------------------------------------------------------- timeline
@dataclass(frozen=True)
class ReceiverSegment:
    start: date
    end: date
    header: ReceiverHeader


def _segment_range(
    days: List[ArchiveDay],
    lo: int,
    hi: int,
    read_fn: Callable[[Path], Optional[ReceiverHeader]],
    cache: Dict[int, Optional[ReceiverHeader]],
) -> List[ReceiverSegment]:
    """Binary-search header-key boundaries over ``days[lo..hi]`` (inclusive)."""

    def hdr(i: int) -> Optional[ReceiverHeader]:
        if i not in cache:
            cache[i] = read_fn(days[i].file_path)
        return cache[i]

    def valid_from(idx: int, step: int, stop: int):
        i = idx
        while (step > 0 and i <= stop) or (step < 0 and i >= stop):
            h = hdr(i)
            if h is not None and h.is_known:
                return i, h
            i += step
        return None

    def segs(a: int, b: int) -> List[ReceiverSegment]:
        left = valid_from(a, 1, b)
        right = valid_from(b, -1, a)
        if left is None or right is None:
            return []
        li, lh = left
        ri, rh = right
        if lh.key == rh.key:
            # Matching endpoints usually mean one segment — but verify the
            # interior isn't a revert (X→Y→X) hidden between them: sample the
            # midpoint, and only if it ALSO matches do we trust a single segment.
            # One extra read per range; catches any revert whose run reaches a
            # recursion midpoint (i.e. all but vanishingly short ones).
            if ri - li > 1:
                mi = (li + ri) // 2
                mh = hdr(mi)
                if mh is not None and mh.is_known and mh.key != lh.key:
                    return _coalesce(segs(li, mi) + segs(mi, ri))
            return [ReceiverSegment(days[li].obs_date, days[ri].obs_date, lh)]
        if ri - li <= 1:
            return [
                ReceiverSegment(days[li].obs_date, days[li].obs_date, lh),
                ReceiverSegment(days[ri].obs_date, days[ri].obs_date, rh),
            ]
        mid = (li + ri) // 2
        return _coalesce(segs(li, mid) + segs(mid, ri))

    return segs(lo, hi)


def _coalesce(segs: List[ReceiverSegment]) -> List[ReceiverSegment]:
    out: List[ReceiverSegment] = []
    for s in segs:
        if out and out[-1].header.key == s.header.key:
            p = out[-1]
            out[-1] = ReceiverSegment(p.start, max(p.end, s.end), p.header)
        else:
            out.append(s)
    return out


def build_receiver_timeline(
    station: str,
    root=None,
    *,
    rate: str = "15s_24hr",
    read_fn: Callable[[Path], Optional[ReceiverHeader]] = read_receiver_header,
) -> List[ReceiverSegment]:
    """Receiver/firmware segments for a station from archived RINEX headers.

    Brand runs (cheap, raw-extension based) bound the per-range header
    binary-search so a known ``.T02``→``.sbf`` junction is never re-confirmed by
    decompressing; segments are coalesced across run boundaries (a rinex-only gap
    between two same-receiver spans is not a change).
    """
    archive_root = Path(root) if root is not None else cold_archive_prepath()
    # RINEX path per day (raw days have RINEX too) — needed to read the header.
    rinex_days = list(
        walk_station_timeline(station, archive_root, rate=rate, prefer_raw=False)
    )
    if not rinex_days:
        return []

    # Cheap brand runs bound the search ranges (no header reads).
    raw_days = list(
        walk_station_timeline(station, archive_root, rate=rate, prefer_raw=True)
    )
    runs = coalesce_brand_runs(raw_days) if raw_days else []
    bounds = sorted({r.start for r in runs} | {r.end for r in runs})

    # Translate brand-run date bounds into rinex_days index chunks.
    cache: Dict[int, Optional[ReceiverHeader]] = {}
    out: List[ReceiverSegment] = []
    chunk_starts = [0]
    if bounds:
        for b in bounds:
            idx = _first_index_on_or_after(rinex_days, b)
            if idx is not None and idx not in chunk_starts:
                chunk_starts.append(idx)
    chunk_starts = sorted(set(chunk_starts))
    chunk_starts.append(len(rinex_days))
    for a, b in zip(chunk_starts, chunk_starts[1:]):
        if b - 1 < a:
            continue
        out.extend(_segment_range(rinex_days, a, b - 1, read_fn, cache))
    return _coalesce(out)


def _first_index_on_or_after(days: List[ArchiveDay], d: date) -> Optional[int]:
    for i, day in enumerate(days):
        if day.obs_date >= d:
            return i
    return None


def current_install(timeline: List[ReceiverSegment]) -> Optional[ReceiverSegment]:
    """Most recent segment — its ``start`` is the current install proxy."""
    return timeline[-1] if timeline else None
