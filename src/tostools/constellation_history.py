"""Reconstruct a receiver period's satellite-system history from the archive.

The satellite systems a receiver tracked can change within one device tenure
(a firmware/config change that adds Galileo, say). To recover the full history
for a period ``[date_from, date_to)`` WITHOUT reading every daily file, we use
the endpoints-first + binary-search method (bgo):

* read the constellations of the FIRST and LAST archived RINEX in the span;
* if they are identical, the whole span is that set — done in two reads;
* if they differ, binary-search for the boundary date where the set changed
  (the last index still matching the start's set → the change is at +1), emit
  that run, jump to the boundary, and repeat until the span is fully segmented.

Cost is ``O((changes + 1) · log n)`` header reads, never the whole period.

Reading source is RINEX (:func:`tostools.constellation.read_constellations`):
a RINEX-3 ``SYS / # / OBS TYPES`` header is authoritative (``reliable=True``);
RINEX-2 under-reports (``reliable=False``) — a caveat the caller surfaces.
Because reconstruction runs PER receiver period, one era's RINEX version is
consistent within a segment, so R2 and R3 readings are not cross-compared.

Assumption: "endpoints equal ⇒ uniform" can miss a change that *reverts* inside
the span. For constellations that is safe — systems are added, effectively
monotonic; a receiver does not un-track a system mid-tenure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .archive import classify_file_format
from .constellation import ConstellationReading, read_constellations

DatedFile = Tuple[date, Path]
ReadFn = Callable[[Path], Optional[ConstellationReading]]


@dataclass
class ConstellationSegment:
    """One run of constant satellite systems within a receiver period."""

    date_from: date  # first archived date observed to carry ``systems``
    date_to: date  # last archived date still carrying ``systems``
    systems: frozenset  # constellation codes (GPS, GLO, GAL, …)
    reliable: bool  # False if either endpoint was an R2 (best-effort) reading
    n_files: int  # files spanned (index count, not files read)
    first_file: Path
    last_file: Path


def segment_by_constellation(
    files: List[DatedFile],
    read_fn: ReadFn = read_constellations,
) -> List[ConstellationSegment]:
    """Segment a date-sorted RINEX list into runs of constant systems.

    ``files`` must be sorted ascending by date. Headers are read lazily and
    cached, so a uniform span costs two reads. Files whose header cannot be
    read are skipped (they can't anchor a boundary); a span with no readable
    file yields no segment.
    """
    n = len(files)
    if n == 0:
        return []

    cache: Dict[int, Optional[ConstellationReading]] = {}

    def rd(i: int) -> Optional[ConstellationReading]:
        # An empty systems set is uninformative (unparseable header / no SYS
        # records) — treat it like an unreadable file so it never anchors a
        # segment; its date is covered by the surrounding readable run.
        if i not in cache:
            r = read_fn(files[i][1])
            cache[i] = r if (r is not None and r.systems) else None
        return cache[i]

    def first_readable(lo: int, hi: int) -> Optional[int]:
        """First readable index in [lo, hi] scanning upward; None if none/empty."""
        for i in range(max(0, lo), min(n - 1, hi) + 1):
            if rd(i) is not None:
                return i
        return None

    def last_readable(lo: int, hi: int) -> Optional[int]:
        """Last readable index in [lo, hi] scanning downward; None if none/empty."""
        for i in range(min(n - 1, hi), max(0, lo) - 1, -1):
            if rd(i) is not None:
                return i
        return None

    segments: List[ConstellationSegment] = []
    start = first_readable(0, n - 1)
    if start is None:
        return []

    while start is not None:
        end = last_readable(start, n - 1)  # last readable index at or after start
        if end is None or end < start:
            end = start
        sys_start = rd(start).systems  # type: ignore[union-attr]

        if rd(end).systems == sys_start:  # type: ignore[union-attr]
            run_end = end
        else:
            # Binary-search the last index whose systems still equal sys_start.
            lo, hi = start, end
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if rd(mid) is None:
                    # Nudge to a readable neighbour strictly inside (lo, hi).
                    nudged = first_readable(mid + 1, hi - 1)
                    if nudged is None:
                        nudged = last_readable(lo + 1, mid - 1)
                    if nudged is None or not (lo < nudged < hi):
                        break
                    mid = nudged
                if rd(mid).systems == sys_start:  # type: ignore[union-attr]
                    lo = mid
                else:
                    hi = mid
            run_end = lo

        r_from = rd(start)
        r_to = rd(run_end)
        segments.append(
            ConstellationSegment(
                date_from=files[start][0],
                date_to=files[run_end][0],
                systems=sys_start,
                reliable=bool(r_from and r_from.reliable and r_to and r_to.reliable),
                n_files=run_end - start + 1,
                first_file=files[start][1],
                last_file=files[run_end][1],
            )
        )
        start = first_readable(run_end + 1, n - 1)

    return segments


# --------------------------------------------------------------------------- #
# Archive file listing for a period
# --------------------------------------------------------------------------- #

_MONTHS = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)


def list_rinex_in_period(
    root: Path,
    marker: str,
    date_from: Optional[date],
    date_to: Optional[date],
    session: str = "15s_24hr",
) -> List[DatedFile]:
    """Date-sorted ``(date, path)`` for a station's RINEX in ``[date_from, date_to]``.

    Walks ``<root>/<YYYY>/<mon>/<MARKER>/<session>/rinex/``. ``date_from``/
    ``date_from`` is inclusive, ``date_to`` is EXCLUSIVE (it is the next
    receiver's install date, whose data belongs to that next period); ``None``
    means unbounded on that side (an open receiver period passes
    ``date_to=None``). Dates come from the filename via
    :func:`classify_file_format`.
    """
    root = Path(root)
    marker = marker.upper()
    out: List[DatedFile] = []
    y0 = date_from.year if date_from else 1994
    y1 = date_to.year if date_to else date.today().year
    for year in range(y0, y1 + 1):
        for mon in _MONTHS:
            rinex_dir = root / str(year) / mon / marker / session / "rinex"
            if not rinex_dir.is_dir():
                continue
            for p in rinex_dir.iterdir():
                if not p.is_file() or not p.name.startswith(marker):
                    continue
                fmt = classify_file_format(p.name)
                fdate = getattr(fmt, "date", None)
                if fdate is None:
                    continue
                if date_from and fdate < date_from:
                    continue
                if date_to and fdate >= date_to:  # exclusive: install day → next period
                    continue
                out.append((fdate, p))
    out.sort(key=lambda t: t[0])
    return out
