"""Spliced ownership of a GAMIT ``station.info`` file between TOS and its host copy.

WHY THIS EXISTS
---------------
The aim is for ``station.info`` to be *maintained through TOS* — rendered from TOS
rather than hand-curated on a GAMIT host. A from-scratch regeneration is not yet
feasible, because TOS does not hold everything the file does:

* a station may have history that PREDATES the date it entered TOS;
* campaign occupations are largely not imported into TOS at all;
* TOS has genuine internal gaps that station.info covers.

Regenerating today would therefore *delete* real history. This module implements
the safe intermediate: a **splice with per-site ownership**.

THE MODEL — one mechanism, not three
------------------------------------
Ownership is per *(site, time-interval)*. The ``managed region`` for a site is
simply the **union of that site's TOS session intervals**. Then:

* rows/parts of the host file **inside** that union  -> superseded (TOS renders them)
* rows/parts **outside** it                          -> preserved byte-for-byte

Pre-TOS history, residue (a TOS gap), and a row straddling the union edge are not
three special cases — they are all "outside the union", and fall out of one
interval subtraction. ``classify_site`` labels them differently for *reporting*
only; ``splice_site`` does not care which label applies.

Two consequences worth stating, because they are the point:

* **A site absent from TOS has an empty managed region, so nothing is removed.**
  Campaign-only stations are therefore untouched by construction, not by care.
* **The residue is a first-class measured quantity** — the convergence progress
  bar. It falls to zero as occupations are imported into TOS
  (``receivers cfg import-campaigns``), at which point the managed region covers
  the whole file and the splice *becomes* a plain regeneration.

DETERMINISM AND THE DRIFT GUARD
-------------------------------
Splicing is deterministic given (TOS rows, host rows, boundaries), so the guard
against drift stays a **total equality check**: re-splice and compare against the
committed bytes. No field-by-field heuristics required, and it works *before* TOS
is authoritative.

BOUNDARIES ARE EXPLICIT DATA
----------------------------
``managed_from`` is a deliberate, reviewable, committed value per site — not a
value silently derived at run time. TOS's earliest session is a *good seed* but
not automatically trustworthy: KAST (start 10-01 vs 10-24) is literally a
boundary dispute, and HRSC/VMEY carry contested dates. So
:func:`compute_managed_from` proposes, and
:class:`BoundaryTable` records the reviewed decision. An entry whose
``managed_from`` is ``None`` means "no boundary -> fully unmanaged", which is the
correct state for every campaign site not yet in TOS.

Timestamps use the GAMIT day-of-year resolution (00:00:00). An open-ended session
(``time_to is None``) is treated as extending to :data:`FAR_FUTURE`.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .gamit_station_info import Occupation

__all__ = [
    "FAR_FUTURE",
    "RowTier",
    "ClassifiedRow",
    "SiteOwnership",
    "BoundaryEntry",
    "BoundaryTable",
    "compute_managed_from",
    "classify_site",
    "splice_site",
    "build_report",
    "format_summary",
    "format_rows_csv",
]

#: Stands in for the ``9999 999`` open-ended sentinel during interval arithmetic.
FAR_FUTURE = datetime(9999, 12, 31, 23, 59, 59)


class RowTier(str, Enum):
    """How one host-file row relates to the TOS-managed region (reporting only)."""

    #: Entirely before the first TOS session -> legacy history, preserved.
    PRE_TOS = "pre-tos"
    #: Inside a TOS session interval -> superseded by TOS.
    MANAGED = "managed"
    #: After the managed region began, but inside a TOS *gap* -> preserved.
    RESIDUE = "residue"
    #: Straddles a managed-region edge -> split; the outside part is preserved.
    STRADDLED = "straddled"


@dataclass(frozen=True)
class ClassifiedRow:
    """One host occupation with its tier and, when split, its surviving extent."""

    occupation: Occupation
    tier: RowTier
    #: Portions of this row that survive the splice (empty for MANAGED rows).
    preserved_fragments: Tuple[Tuple[datetime, Optional[datetime]], ...] = ()
    #: Human-readable note for the report (e.g. "split at 2014-290").
    note: str = ""

    @property
    def marker(self) -> str:
        return self.occupation.marker


@dataclass
class SiteOwnership:
    """Ownership state for one site."""

    marker: str
    #: The reviewed boundary in force, or None when the site is fully unmanaged.
    managed_from: Optional[datetime]
    #: What TOS's own earliest session says (the seed), or None if absent from TOS.
    tos_earliest: Optional[datetime]
    #: True when ``managed_from`` is a proposal rather than a reviewed decision.
    boundary_is_proposed: bool
    rows: List[ClassifiedRow]
    tos_rows: List[Occupation]
    #: The managed region actually in force: merged, sorted TOS intervals floored
    #: at ``managed_from``. Stored so classification and splicing provably agree
    #: (recomputing it in the splice would let the two drift apart).
    managed_union: Tuple[Tuple[datetime, datetime], ...] = ()

    @property
    def in_tos(self) -> bool:
        return bool(self.tos_rows)

    @property
    def managed_rows(self) -> List[ClassifiedRow]:
        return [r for r in self.rows if r.tier is RowTier.MANAGED]

    @property
    def residue_rows(self) -> List[ClassifiedRow]:
        return [r for r in self.rows if r.tier is RowTier.RESIDUE]

    @property
    def pre_tos_rows(self) -> List[ClassifiedRow]:
        return [r for r in self.rows if r.tier is RowTier.PRE_TOS]

    @property
    def straddled_rows(self) -> List[ClassifiedRow]:
        return [r for r in self.rows if r.tier is RowTier.STRADDLED]

    @property
    def residue_count(self) -> int:
        """Size of the convergence backlog for this site (0 == fully TOS-owned)."""
        return len(self.residue_rows)

    def summary_row(self) -> Dict[str, object]:
        return {
            "marker": self.marker,
            "in_tos": self.in_tos,
            "managed_from": _fmt(self.managed_from),
            "tos_earliest": _fmt(self.tos_earliest),
            "proposed": self.boundary_is_proposed,
            "host_rows": len(self.rows),
            "tos_rows": len(self.tos_rows),
            "managed": len(self.managed_rows),
            "pre_tos": len(self.pre_tos_rows),
            "residue": self.residue_count,
            "straddled": len(self.straddled_rows),
        }


@dataclass(frozen=True)
class BoundaryEntry:
    """A committed per-site boundary decision."""

    marker: str
    #: None -> fully unmanaged (site not in TOS, or its boundary is not yet decided).
    managed_from: Optional[datetime]
    #: Provenance, e.g. "tos-seed" or "reviewed:2026-10-13".
    source: str = "tos-seed"
    note: str = ""


def _fmt(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d") if dt else ""


def _interval(occ: Occupation) -> Tuple[datetime, datetime]:
    """Half-open [start, stop) interval for an occupation."""
    return (occ.time_from, occ.time_to or FAR_FUTURE)


def _merge(intervals: Iterable[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
    """Merge overlapping/adjacent intervals into a sorted disjoint union."""
    ordered = sorted(intervals, key=lambda iv: (iv[0], iv[1]))
    out: List[Tuple[datetime, datetime]] = []
    for start, stop in ordered:
        if stop <= start:
            continue  # degenerate; ignore
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], stop))
        else:
            out.append((start, stop))
    return out


def _intersect(
    iv: Tuple[datetime, datetime], union: Sequence[Tuple[datetime, datetime]]
) -> List[Tuple[datetime, datetime]]:
    """Parts of ``iv`` that fall inside some interval of ``union``."""
    out: List[Tuple[datetime, datetime]] = []
    for us, ue in union:
        start, stop = max(iv[0], us), min(iv[1], ue)
        if stop > start:
            out.append((start, stop))
    return out


def _subtract(
    row: Tuple[datetime, datetime], union: Sequence[Tuple[datetime, datetime]]
) -> List[Tuple[datetime, datetime]]:
    """Parts of ``row`` that fall outside every interval in ``union``."""
    pieces = [row]
    for us, ue in union:
        nxt: List[Tuple[datetime, datetime]] = []
        for ps, pe in pieces:
            if ue <= ps or us >= pe:  # disjoint
                nxt.append((ps, pe))
                continue
            if ps < us:
                nxt.append((ps, us))
            if pe > ue:
                nxt.append((ue, pe))
        pieces = nxt
        if not pieces:
            break
    return [(s, e) for s, e in pieces if e > s]


def compute_managed_from(tos_rows: Sequence[Occupation]) -> Optional[datetime]:
    """Propose a site's boundary: its earliest TOS session start.

    A *proposal*, not a decision — see the module docstring on boundary disputes.
    Returns None when the site has no TOS sessions at all.
    """
    if not tos_rows:
        return None
    return min(r.time_from for r in tos_rows)


class BoundaryTable:
    """The committed per-site boundary decisions (``managed_from``)."""

    def __init__(self, entries: Optional[Dict[str, BoundaryEntry]] = None):
        self._entries: Dict[str, BoundaryEntry] = dict(entries or {})

    @classmethod
    def load(cls, path: Union[str, Path]) -> "BoundaryTable":
        """Read a boundary CSV (``marker,managed_from,source,note``).

        A blank ``managed_from`` records a deliberate *fully unmanaged* decision,
        which is distinct from the marker being absent from the table entirely.
        """
        entries: Dict[str, BoundaryEntry] = {}
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                marker = (row.get("marker") or "").strip().upper()
                if not marker:
                    continue
                raw = (row.get("managed_from") or "").strip()
                entries[marker] = BoundaryEntry(
                    marker=marker,
                    managed_from=_parse_date(raw) if raw else None,
                    source=(row.get("source") or "").strip() or "tos-seed",
                    note=(row.get("note") or "").strip(),
                )
        return cls(entries)

    def save(self, path: Union[str, Path], sites: Sequence[SiteOwnership]) -> None:
        """Write the table, seeding from TOS for markers not already recorded.

        Existing entries are never overwritten: a reviewed decision outranks a
        fresh proposal.
        """
        out: Dict[str, BoundaryEntry] = dict(self._entries)
        for site in sites:
            if site.marker in out:
                continue
            out[site.marker] = BoundaryEntry(
                marker=site.marker,
                managed_from=site.tos_earliest,
                source="tos-seed" if site.tos_earliest else "absent-from-tos",
                note="",
            )
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["marker", "managed_from", "source", "note"])
            for marker in sorted(out):
                e = out[marker]
                w.writerow([e.marker, _fmt(e.managed_from), e.source, e.note])

    def get(self, marker: str) -> Optional[BoundaryEntry]:
        return self._entries.get(marker.upper())

    def __contains__(self, marker: str) -> bool:
        return marker.upper() in self._entries

    def __len__(self) -> int:
        return len(self._entries)


def _parse_date(raw: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {raw!r} (want YYYY-MM-DD)")


def classify_site(
    marker: str,
    host_rows: Sequence[Occupation],
    tos_rows: Sequence[Occupation],
    *,
    boundary: Optional[BoundaryEntry] = None,
) -> SiteOwnership:
    """Classify a site's host rows against its TOS-managed region.

    The managed region is ``[managed_from, inf)`` intersected with the union of
    the TOS session intervals. When no boundary entry is supplied the proposed
    seed (TOS's earliest session) is used and marked ``boundary_is_proposed``.
    """
    marker = marker.upper()
    tos_earliest = compute_managed_from(tos_rows)

    proposed = boundary is None
    if proposed:
        managed_from = tos_earliest
    else:
        managed_from = boundary.managed_from

    # Managed region: TOS intervals, floored at the boundary.
    if managed_from is None:
        union: List[Tuple[datetime, datetime]] = []
    else:
        union = _merge(
            (max(_interval(r)[0], managed_from), _interval(r)[1])
            for r in tos_rows
            if _interval(r)[1] > managed_from
        )

    rows: List[ClassifiedRow] = []
    for occ in host_rows:
        iv = _interval(occ)
        surviving = _subtract(iv, union)
        if not surviving:
            rows.append(ClassifiedRow(occ, RowTier.MANAGED))
            continue

        # Not fully managed: decide the reporting label.
        covers_all = len(surviving) == 1 and surviving[0] == iv
        if covers_all:
            before = managed_from is not None and iv[1] <= managed_from
            tier = RowTier.PRE_TOS if before else RowTier.RESIDUE
            note = ""
        else:
            tier = RowTier.STRADDLED
            note = "split at managed-region edge(s): " + ", ".join(
                f"{s.strftime('%Y-%m-%d')}..{_fmt(e) if e != FAR_FUTURE else 'open'}"
                for s, e in surviving
            )
        rows.append(
            ClassifiedRow(
                occupation=occ,
                tier=tier,
                preserved_fragments=tuple(
                    (s, None if e == FAR_FUTURE else e) for s, e in surviving
                ),
                note=note,
            )
        )

    return SiteOwnership(
        marker=marker,
        managed_from=managed_from,
        tos_earliest=tos_earliest,
        boundary_is_proposed=proposed,
        rows=rows,
        tos_rows=list(tos_rows),
        managed_union=tuple(union),
    )


def splice_site(site: SiteOwnership) -> Tuple[List[Occupation], List[str]]:
    """Build the spliced occupation list for one site.

    The managed portion of the output is the TOS rows **clipped to the managed
    region** — never the TOS rows as-is. Clipping matters in two cases that would
    otherwise corrupt the file:

    * a fully-unmanaged site (no boundary, or absent from TOS) has an empty
      managed region, so **no TOS row may be injected at all** — injecting them
      would duplicate rows the host already owns;
    * when a reviewed boundary floors an earlier TOS session, that session must
      be clipped to the boundary rather than re-introducing the very rows the
      boundary was set to protect.

    Returns ``(occupations, adjustments)`` sorted by start time, where
    ``adjustments`` describes every row that was split, clipped or superseded —
    the things a reviewer must see, since each subtly changes GAMIT history.
    """
    out: List[Occupation] = []
    adjustments: List[str] = []
    union = site.managed_union

    # --- managed region: TOS rows, clipped to the region in force ---
    for tos_row in site.tos_rows:
        iv = _interval(tos_row)
        pieces = _intersect(iv, union)
        if not pieces:
            adjustments.append(
                f"{site.marker}: TOS row {tos_row.time_from.date()}"
                f"..{_fmt(tos_row.time_to) or 'open'} NOT applied"
                f" (outside the managed region)"
            )
            continue
        for start, stop in pieces:
            clipped = start != iv[0] or stop != iv[1]
            if clipped:
                adjustments.append(
                    f"{site.marker}: TOS row {tos_row.time_from.date()}"
                    f"..{_fmt(tos_row.time_to) or 'open'} clipped to "
                    f"{start.date()}..{_fmt(None if stop == FAR_FUTURE else stop) or 'open'}"
                )
            out.append(
                replace(
                    tos_row,
                    time_from=start,
                    time_to=None if stop == FAR_FUTURE else stop,
                )
            )

    # --- everything outside the region: preserved host rows, split at the edges ---
    for row in site.rows:
        if row.tier is RowTier.MANAGED:
            adjustments.append(
                f"{site.marker}: host row {row.occupation.time_from.date()}"
                f"..{_fmt(row.occupation.time_to) or 'open'} superseded by TOS"
            )
            continue
        if not row.preserved_fragments:
            continue
        if row.tier is RowTier.STRADDLED:
            adjustments.append(
                f"{site.marker}: SPLIT host row "
                f"{row.occupation.time_from.date()}.."
                f"{_fmt(row.occupation.time_to) or 'open'} -> {len(row.preserved_fragments)} "
                f"preserved fragment(s); {row.note}"
            )
        for frag_start, frag_stop in row.preserved_fragments:
            out.append(
                replace(
                    row.occupation,
                    time_from=frag_start,
                    time_to=frag_stop,
                )
            )

    out.sort(key=lambda o: (o.time_from, o.time_to or FAR_FUTURE))
    return out, adjustments


def build_report(
    host_rows_by_marker: Dict[str, List[Occupation]],
    tos_rows_by_marker: Dict[str, List[Occupation]],
    *,
    boundaries: Optional[BoundaryTable] = None,
    only_markers: Optional[Sequence[str]] = None,
) -> List[SiteOwnership]:
    """Classify every site present in either source, sorted by marker."""
    markers = set(host_rows_by_marker) | set(tos_rows_by_marker)
    if only_markers:
        wanted = {m.upper() for m in only_markers}
        markers &= wanted
    table = boundaries or BoundaryTable()
    sites = [
        classify_site(
            marker,
            host_rows_by_marker.get(marker, []),
            tos_rows_by_marker.get(marker, []),
            boundary=table.get(marker),
        )
        for marker in sorted(markers)
    ]
    return sites


SUMMARY_COLUMNS = (
    "marker",
    "in_tos",
    "managed_from",
    "tos_earliest",
    "proposed",
    "host_rows",
    "tos_rows",
    "managed",
    "pre_tos",
    "residue",
    "straddled",
)


def format_summary(sites: Sequence[SiteOwnership]) -> str:
    """Human-readable summary table plus the headline totals."""
    lines = [
        f"{'MARKER':<7}{'IN_TOS':<8}{'MANAGED_FROM':<14}{'HOST':>5}"
        f"{'TOS':>5}{'MG':>5}{'PRE':>5}{'RES':>5}{'SPLIT':>6}  BOUNDARY"
    ]
    for s in sites:
        r = s.summary_row()
        lines.append(
            f"{str(r['marker']):<7}{'yes' if r['in_tos'] else 'NO':<8}"
            f"{str(r['managed_from']) or '-':<14}{r['host_rows']:>5}{r['tos_rows']:>5}"
            f"{r['managed']:>5}{r['pre_tos']:>5}{r['residue']:>5}{r['straddled']:>6}"
            f"  {'proposed' if r['proposed'] else 'reviewed'}"
        )
    lines.append("")
    lines.append(_totals_line(sites))
    return "\n".join(lines)


def _totals_line(sites: Sequence[SiteOwnership]) -> str:
    total_host = sum(len(s.rows) for s in sites)
    total_tos = sum(len(s.tos_rows) for s in sites)
    managed = sum(len(s.managed_rows) for s in sites)
    pre = sum(len(s.pre_tos_rows) for s in sites)
    residue = sum(s.residue_count for s in sites)
    straddled = sum(len(s.straddled_rows) for s in sites)
    no_tos = [s.marker for s in sites if not s.in_tos]
    return (
        f"TOTAL sites={len(sites)}  host_rows={total_host}  tos_rows={total_tos}\n"
        f"  managed (TOS takes over) : {managed}\n"
        f"  pre-TOS preserved        : {pre}\n"
        f"  RESIDUE (backlog)        : {residue}\n"
        f"  straddled/split          : {straddled}\n"
        f"  sites absent from TOS    : {len(no_tos)}"
        + (f"  -> {', '.join(no_tos[:20])}{' …' if len(no_tos) > 20 else ''}" if no_tos else "")
    )


def format_rows_csv(sites: Sequence[SiteOwnership]) -> str:
    """Per-row CSV of the classification, for review in a spreadsheet."""
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "marker",
            "tier",
            "host_from",
            "host_to",
            "preserved_from",
            "preserved_to",
            "receiver_type",
            "receiver_sn",
            "antenna_type",
            "dome",
            "antenna_height",
            "note",
        ]
    )
    for site in sites:
        for row in site.rows:
            occ = row.occupation
            frags = row.preserved_fragments or ((None, None),)
            for frag_start, frag_stop in frags:
                w.writerow(
                    [
                        occ.marker,
                        row.tier.value,
                        occ.time_from.strftime("%Y-%m-%d"),
                        _fmt(occ.time_to) or "open",
                        _fmt(frag_start),
                        _fmt(frag_stop) or ("open" if frag_start else ""),
                        occ.receiver_type,
                        occ.receiver_sn,
                        occ.antenna_type,
                        occ.dome,
                        occ.antenna_height,
                        row.note,
                    ]
                )
    return buf.getvalue()
