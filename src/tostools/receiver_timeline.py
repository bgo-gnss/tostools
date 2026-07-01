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
that's a few dozen reads, not thousands. Each of those reads is itself cheap:
``read_receiver_header`` streams ``gzip -dc`` and stops at ``END OF HEADER``
rather than inflating the whole ~4 MB daily file for its ~2 KB header
(≈50× faster per read on the cold NFS archive), falling back to the robust
shared reader for uncompressed names or any failure.

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
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

from .archive import (
    ArchiveDay,
    coalesce_brand_runs,
    cold_archive_prepath,
    walk_station_timeline,
)
from .rinex.reader import read_rinex_header
from .standards.igs_equipment import to_igs_receiver

logger = logging.getLogger(__name__)

_END_OF_HEADER = b"END OF HEADER"
# A RINEX header is a few KB (multi-GNSS with many SYS/OBS TYPES lines still well
# under this). If we stream this far without the marker the file isn't a normal
# RINEX header — bail to the robust shared reader rather than inflate the rest.
_MAX_HEADER_BYTES = 256 * 1024

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


def _fast_header_text(path) -> Optional[str]:
    """Stream-decompress only the RINEX header (up to ``END OF HEADER``).

    The hot path reads a 24h ``.D.Z`` (Unix-compress/LZW Hatanaka) file purely
    to get the ~2 KB header at the top. Inflating the whole ~4 MB file with the
    pure-Python ``unlzw`` costs ~400 ms/file on the cold NFS archive; streaming
    GNU ``gzip -dc`` (which reads both ``.Z`` and ``.gz``) in chunks and stopping
    at the marker is ~8 ms/file — ≈50× — because the header is a tiny prefix.

    Returns the decoded header text, or ``None`` for anything we don't
    fast-path (uncompressed names) or any failure (no ``gzip``, no marker,
    garbled stream) so the caller falls back to the robust shared reader. Never
    raises.
    """
    name = str(path)
    if not name.endswith((".Z", ".gz")):
        return None  # uncompressed / short-name → let the shared reader handle it
    proc = None
    try:
        proc = subprocess.Popen(
            ["gzip", "-dc", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        buf = b""
        assert proc.stdout is not None
        while _END_OF_HEADER not in buf:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > _MAX_HEADER_BYTES:
                return None
        end = buf.find(_END_OF_HEADER)
        if end == -1:
            return None
        # Truncate at the marker (matches read_rinex_header's header slice) so
        # the trailing bytes of the chunk it landed in aren't returned.
        return buf[: end + len(_END_OF_HEADER)].decode("utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001 — fall back on any failure
        logger.debug("_fast_header_text(%s): %s", path, exc)
        return None
    finally:
        if proc is not None:
            if proc.stdout is not None:
                proc.stdout.close()
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()


def _rec_line_from_text(header: str) -> Optional[ReceiverHeader]:
    for line in header.splitlines():
        if _REC_LABEL in line:
            return parse_receiver_line(line)
    return None


def read_receiver_header(path) -> Optional[ReceiverHeader]:
    """Receiver header of one archived RINEX file, or None on any failure.

    Tries the fast streaming reader first (stops at ``END OF HEADER`` instead of
    inflating the whole file); falls back to the robust shared
    :func:`~tostools.rinex.reader.read_rinex_header` for uncompressed files or
    when the fast path can't produce a header.
    """
    fast = _fast_header_text(path)
    if fast is not None:
        return _rec_line_from_text(fast)
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
    return _rec_line_from_text(header)


# ------------------------------------------------------------------------- timeline
@dataclass(frozen=True)
class ReceiverSegment:
    start: date
    end: date
    header: ReceiverHeader


class _HeaderLike(Protocol):
    """Structural type for any RINEX-header identity the segmenter consumes."""

    @property
    def key(self) -> tuple: ...

    @property
    def is_known(self) -> bool: ...


def _segment_range(
    days: List[ArchiveDay],
    lo: int,
    hi: int,
    read_fn: Callable[[Path], Optional[_HeaderLike]],
    cache: Dict[int, Any],
    segment_cls: Callable[..., Any] = ReceiverSegment,
) -> List[Any]:
    """Binary-search header-key boundaries over ``days[lo..hi]`` (inclusive).

    Header-type-generic: ``read_fn`` returns any object exposing ``.key``
    (a normalized identity tuple) and ``.is_known`` (bool), and ``segment_cls``
    is the ``(start, end, header)`` frozen dataclass to emit — defaults to
    :class:`ReceiverSegment` so existing callers are unaffected; the antenna
    timeline passes ``AntennaSegment``. A ``None``/``not is_known`` header is a
    missing read, not a boundary.
    """

    def hdr(i: int) -> Optional[_HeaderLike]:
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

    def segs(a: int, b: int) -> List[Any]:
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
            return [segment_cls(days[li].obs_date, days[ri].obs_date, lh)]
        if ri - li <= 1:
            return [
                segment_cls(days[li].obs_date, days[li].obs_date, lh),
                segment_cls(days[ri].obs_date, days[ri].obs_date, rh),
            ]
        mid = (li + ri) // 2
        return _coalesce(segs(li, mid) + segs(mid, ri))

    return segs(lo, hi)


def _coalesce(segs: List[Any]) -> List[Any]:
    """Merge adjacent segments with equal ``header.key``.

    Reconstructs the merged segment via ``type(p)(...)`` so it works for any
    ``(start, end, header)`` segment dataclass (ReceiverSegment / AntennaSegment).
    """
    out: List[Any] = []
    for s in segs:
        if out and out[-1].header.key == s.header.key:
            p = out[-1]
            out[-1] = type(p)(p.start, max(p.end, s.end), p.header)
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
    """Most recent segment — its ``start`` is the latest firmware's date.

    Note this is the start of the trailing *firmware* segment, not necessarily
    when the physical receiver was installed (a firmware bump opens a new
    segment). For the install-date of the current receiver unit, use
    :func:`current_receiver_install_date`.
    """
    return timeline[-1] if timeline else None


def _same_unit(a: tuple, b: tuple) -> bool:
    """Same physical receiver (type + serial), ignoring firmware.

    Compares the first two components of :attr:`ReceiverHeader.key`. An
    unknown serial (``None``, e.g. a garbled read) is a wildcard — it does not
    mark a unit boundary — mirroring the both-known rule in
    ``classify_current_receiver``.
    """
    if a[0] != b[0]:
        return False
    if a[1] is not None and b[1] is not None and a[1] != b[1]:
        return False
    return True


def coalesce_receiver_units(
    timeline: List[ReceiverSegment],
) -> List[ReceiverSegment]:
    """Merge adjacent firmware-only boundaries into one physical-receiver segment.

    :func:`build_receiver_timeline` keys on ``(type, serial, firmware)`` — correct
    for a firmware timeline, but it fragments one physical unit across its firmware
    bumps. For a *receiver* (unit) view, coalesce neighbours that are
    :func:`_same_unit` (type+serial, ignoring firmware), keeping the earliest
    start and latest end. The retained header is the segment's FIRST (its firmware
    string is no longer meaningful for a unit view and should not be displayed).
    """
    out: List[ReceiverSegment] = []
    for s in timeline:
        if out and _same_unit(out[-1].header.key, s.header.key):
            p = out[-1]
            out[-1] = ReceiverSegment(p.start, max(p.end, s.end), p.header)
        else:
            out.append(s)
    return out


def current_receiver_install_date(timeline: List[ReceiverSegment]) -> Optional[date]:
    """Date the *current physical receiver* first appears in the archive.

    Walks backward from the last segment across firmware-only boundaries (same
    type + serial) and returns the earliest such start. This is the right date
    for a ``replace-receiver`` suggestion: the install date of the receiver
    UNIT, not of its latest firmware. For OLKE's PolaRX5 sn 3016143 — split
    into 5.1.1 / 5.3.0 / 5.6.0 firmware segments — this returns 2017-07-08
    (the 5.1.1 segment), not 2026-05-18 (the 5.6.0 bump).
    """
    if not timeline:
        return None
    cur_id = timeline[-1].header.key
    start = timeline[-1].start
    for seg in reversed(timeline[:-1]):
        if _same_unit(seg.header.key, cur_id):
            start = seg.start
        else:
            break
    return start
