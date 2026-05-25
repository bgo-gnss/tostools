"""Archive helpers for the cold raw/RINEX store.

The cold archive (``/mnt/rawgpsdata`` on production, ``/mnt_data/rawgpsdata``
on developer laptops) is the long-term store of receiver-format raw files
and Hatanaka-compressed RINEX. Layout:

    <prepath>/<YYYY>/<mon>/<STATION>/<rate>/<leaf>/<file>

where ``rate ∈ {15s_24hr, 1Hz_1hr}`` and ``leaf ∈ {raw, rinex}``. ``raw/``
carries the receiver's native binary format (``.sbf`` for Septentrio,
``.T02``/``.T01`` for Trimble NetR9/NetRS, ``.dat`` for Trimble 4000SST,
etc.). ``rinex/`` carries Hatanaka-compressed RINEX (format-neutral wrt
receiver brand).

This module exposes the primitives used by the ``tos audit verify-from-rinex``
verb (and any future consumer that wants to compare TOS state against the
archive). All functions are pure / no side effects beyond `Path.iterdir`.

Resolution order for the archive root (highest priority first):

  1. Explicit ``override`` argument to :func:`cold_archive_prepath`
  2. Environment variable ``TOSTOOLS_ARCHIVE_ROOT``
  3. ``[archive_paths] cold_archive_prepath`` in ``receivers.cfg`` (shared
     with the receivers package; canonical home — single source of truth
     so both packages see the same value)
  4. Probe known mount points (``/mnt/rawgpsdata`` first for prod, then
     ``/mnt_data/rawgpsdata`` for dev laptops)
  5. Raise ``FileNotFoundError`` with a helpful message

The cfg lookup uses the same search order as ``tostools.api.tos_writer``'s
``_find_database_cfg``: ``GPS_CONFIG_PATH`` env → ``gps_parser`` config dir
→ ``~/.config/gpsconfig/``.
"""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Union

from .rinex.reader import MONTHS

# Receiver-brand classification from file extension. Conservative: the
# `unknown` family is used when no rule fires — operators see it in the
# report and can extend the table.
_RAW_FORMAT_FAMILIES: Dict[str, str] = {
    "sbf": "septentrio",  # POLARX2 / POLARX5 raw
    "T02": "trimble_netr9",  # NetR9
    "T01": "trimble_netrs",  # NetRS
    "T00": "trimble_other",  # other Trimble
    "dat": "trimble_4000",  # 4000SST
}

# RINEX-derived formats are brand-neutral (the file went through sbf2rin
# or rnx2crx) — they tell us a day was observed but not which receiver.
# Pattern: STA<DDD>0.<YY>[Do]  (Hatanaka 'D' or observation 'o') ± .Z/.gz
_RINEX_NAME_RE = re.compile(
    r"^(?P<sta>[A-Z0-9]{4})(?P<doy>\d{3})0\.(?P<yy>\d{2})[Dod]" r"(?:\.(?:Z|gz))?$"
)

# Raw-filename date pattern. Receivers package's getSeptentrio writes
# names like ``SAVI201407080000a.sbf`` (yyyymmdd + hhmm + session letter).
_RAW_NAME_RE = re.compile(
    r"^(?P<sta>[A-Z0-9]{4})(?P<yyyy>\d{4})(?P<mm>\d{2})(?P<dd>\d{2})"
    r"\d{4}[a-z]?\.(?P<ext>[A-Za-z0-9]+)$"
)


# ---------------------------------------------------------------------------
# Archive root resolution
# ---------------------------------------------------------------------------


def _find_receivers_cfg() -> Optional[Path]:
    """Locate ``receivers.cfg`` using the shared gpsconfig search order.

    Mirrors :func:`tostools.api.tos_writer._find_database_cfg`. The cfg is
    shared between the receivers package and tostools; tostools only
    reads from it (never writes).
    """
    candidates: List[Path] = []

    gps_config_env = os.environ.get("GPS_CONFIG_PATH")
    if gps_config_env:
        candidates.append(Path(gps_config_env) / "receivers.cfg")

    try:
        import gps_parser  # type: ignore[import]

        config_dir = gps_parser.ConfigParser().config_path
        if config_dir:
            candidates.append(Path(config_dir) / "receivers.cfg")
    except Exception:  # noqa: BLE001
        pass

    candidates.append(Path.home() / ".config" / "gpsconfig" / "receivers.cfg")

    for p in candidates:
        if p.is_file():
            return p
    return None


_PROBE_PATHS = ("/mnt/rawgpsdata", "/mnt_data/rawgpsdata")


def cold_archive_prepath(override: Optional[Union[str, Path]] = None) -> Path:
    """Resolve the cold-archive root path.

    Resolution order (first hit wins):
      1. Explicit ``override`` (CLI flag value).
      2. Env var ``TOSTOOLS_ARCHIVE_ROOT``.
      3. ``[archive_paths] cold_archive_prepath`` in the shared
         ``receivers.cfg``.
      4. Probe ``/mnt/rawgpsdata`` then ``/mnt_data/rawgpsdata`` and
         return the first that exists.
      5. Raise :class:`FileNotFoundError` with a helpful message listing
         every candidate that was checked.

    Returns the resolved path; does not verify the path is non-empty —
    operators can pass a path that exists-but-is-empty (e.g. dormant
    autofs mount) and the caller will surface "no archived data" at the
    walk step.
    """
    if override is not None:
        return Path(override)

    env = os.environ.get("TOSTOOLS_ARCHIVE_ROOT")
    if env:
        return Path(env)

    cfg_path = _find_receivers_cfg()
    if cfg_path is not None:
        parser = configparser.ConfigParser()
        try:
            parser.read(cfg_path)
            if parser.has_option("archive_paths", "cold_archive_prepath"):
                value = parser.get("archive_paths", "cold_archive_prepath").strip()
                if value:
                    return Path(value)
        except (configparser.Error, OSError):
            # Don't surface cfg-parse errors as fatal — fall through to
            # mount probing so a malformed cfg never blocks a verb that
            # could otherwise work via probe.
            pass

    for cand in _PROBE_PATHS:
        if Path(cand).is_dir():
            return Path(cand)

    raise FileNotFoundError(
        "cold_archive_prepath unresolved. Searched: "
        "--archive-root (none) → env TOSTOOLS_ARCHIVE_ROOT (unset) → "
        f"receivers.cfg [archive_paths] cold_archive_prepath (not found) "
        f"→ probe {_PROBE_PATHS} (none mounted). "
        "Add the entry to ~/.config/gpsconfig/receivers.cfg or pass "
        "--archive-root."
    )


# ---------------------------------------------------------------------------
# Filename → format-family classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileClassification:
    """One filename's parsed shape."""

    family: str  # septentrio, trimble_netr9, rinex, unknown, ...
    date: Optional[date]  # parsed observation date if filename encoded it
    is_raw: bool  # raw receiver format vs derived (rinex/hatanaka)
    extension: str  # bare extension, no leading dot


def classify_file_format(filename: str) -> FileClassification:
    """Classify a single archived filename by extension + name shape.

    Examples:

    >>> c = classify_file_format("SAVI201407080000a.sbf")
    >>> c.family, c.is_raw, c.date.isoformat()
    ('septentrio', True, '2007-08-08')

    >>> c = classify_file_format("SAVI201607020000a.T02")
    >>> c.family, c.is_raw, c.date.isoformat()
    ('trimble_netr9', True, '2026-07-02')

    >>> c = classify_file_format("SAVI1840.16D.Z")
    >>> c.family, c.is_raw
    ('rinex', False)

    Returns a :class:`FileClassification` with ``family='unknown'`` when no
    rule matches (and ``date=None``); the caller decides whether to skip
    or surface.
    """
    # Try receiver-raw pattern first (carries explicit YYYY-MM-DD).
    raw_match = _RAW_NAME_RE.match(filename)
    if raw_match:
        ext = raw_match.group("ext")
        family = _RAW_FORMAT_FAMILIES.get(ext, "unknown")
        try:
            obs_date = date(
                int(raw_match.group("yyyy")),
                int(raw_match.group("mm")),
                int(raw_match.group("dd")),
            )
        except ValueError:
            obs_date = None
        return FileClassification(
            family=family,
            date=obs_date,
            is_raw=True,
            extension=ext,
        )

    # RINEX / Hatanaka — brand-neutral; YY+DOY encoded.
    rinex_match = _RINEX_NAME_RE.match(filename)
    if rinex_match:
        try:
            yy = int(rinex_match.group("yy"))
            year = 2000 + yy if yy < 80 else 1900 + yy
            doy = int(rinex_match.group("doy"))
            obs_date = date.fromordinal(date(year, 1, 1).toordinal() + doy - 1)
        except ValueError:
            obs_date = None
        return FileClassification(
            family="rinex",
            date=obs_date,
            is_raw=False,
            extension=filename.split(".", 1)[1] if "." in filename else "",
        )

    return FileClassification(
        family="unknown",
        date=None,
        is_raw=False,
        extension=filename.rsplit(".", 1)[-1] if "." in filename else "",
    )


# ---------------------------------------------------------------------------
# Archive walking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveDay:
    """One archived day for a station."""

    obs_date: date
    family: str
    file_path: Path

    @property
    def is_raw(self) -> bool:
        return self.family not in ("rinex", "unknown")


def walk_station_timeline(
    marker: str,
    root: Union[str, Path],
    *,
    rate: str = "15s_24hr",
    prefer_raw: bool = True,
) -> Iterator[ArchiveDay]:
    """Yield archived days for a station, chronologically.

    Walks ``<root>/<YYYY>/<mon>/<MARKER>/<rate>/{raw,rinex}/`` and
    classifies each filename. When ``prefer_raw=True`` (default), the
    ``raw/`` leaf is consulted first per day — if a raw file is found
    its family wins (gives the receiver-brand signal). Otherwise falls
    through to ``rinex/`` (brand-neutral but date-attesting).

    Args:
        marker: Four-character station code (case-insensitive on input;
            uppercased for directory lookup).
        root: Archive root (typically :func:`cold_archive_prepath`).
        rate: Subdirectory under ``<MARKER>``, e.g. ``"15s_24hr"`` or
            ``"1Hz_1hr"``.
        prefer_raw: If True, prefer the ``raw/`` leaf for brand
            attribution; only fall through to ``rinex/`` per day when
            ``raw/`` has nothing. If False, only look at ``rinex/``.

    Yields:
        :class:`ArchiveDay` per archived day, in chronological order.
        Days where neither leaf yields a classifiable file are skipped
        silently.
    """
    base = Path(root)
    sta = marker.upper()
    if not base.is_dir():
        return

    leaves = ("raw", "rinex") if prefer_raw else ("rinex",)

    # Collect everything across the year tree first so we can sort
    # chronologically. For per-station audits this is a small set
    # (~5K-10K days max for the oldest stations); fine to materialize.
    found: Dict[date, ArchiveDay] = {}

    years = sorted([p.name for p in base.iterdir() if p.is_dir() and p.name.isdigit()])
    for y in years:
        for mon in MONTHS:
            for leaf in leaves:
                leaf_dir = base / y / mon / sta / rate / leaf
                if not leaf_dir.is_dir():
                    continue
                for f in leaf_dir.iterdir():
                    if not f.is_file():
                        continue
                    cls = classify_file_format(f.name)
                    if cls.date is None:
                        continue
                    # raw wins over rinex for the same day; if both
                    # exist, the raw entry was inserted first (leaves
                    # tuple order) and we don't clobber it with rinex.
                    if cls.date in found and found[cls.date].is_raw:
                        continue
                    found[cls.date] = ArchiveDay(
                        obs_date=cls.date,
                        family=cls.family,
                        file_path=f,
                    )

    for obs_date in sorted(found):
        yield found[obs_date]


# ---------------------------------------------------------------------------
# Transition + gap detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrandTransition:
    """A change in receiver-brand family between two consecutive archived days."""

    date_before: date  # last day of `family_before`
    date_after: date  # first day of `family_after`
    family_before: str
    family_after: str
    file_before: Path
    file_after: Path


def detect_brand_transitions(
    timeline: List[ArchiveDay],
) -> List[BrandTransition]:
    """Find every day-to-day change in family across a station's timeline.

    Ignores transitions to/from the ``rinex`` family (brand-neutral; not
    a real equipment change). Reports the day boundaries so the operator
    can correlate with TOS-claimed transition dates.
    """
    out: List[BrandTransition] = []
    if len(timeline) < 2:
        return out
    prev = timeline[0]
    for cur in timeline[1:]:
        if cur.family != prev.family:
            # Skip transitions involving the brand-neutral 'rinex' family
            # — those don't tell us anything about the receiver hardware.
            if "rinex" in (cur.family, prev.family):
                prev = cur
                continue
            out.append(
                BrandTransition(
                    date_before=prev.obs_date,
                    date_after=cur.obs_date,
                    family_before=prev.family,
                    family_after=cur.family,
                    file_before=prev.file_path,
                    file_after=cur.file_path,
                )
            )
        prev = cur
    return out


@dataclass(frozen=True)
class BrandRun:
    """A contiguous span of one receiver-brand family in the timeline.

    Produced by :func:`coalesce_brand_runs`. ``rinex_only_days`` counts
    how many days inside the span carried only RINEX (raw missing); the
    span itself is attributed to a single brand because the rinex days
    were absorbed into the surrounding (same-brand) span.

    ``ambiguous=True`` marks spans the coalescer couldn't attribute to a
    real brand — namely rinex runs at the start/end of the timeline (no
    bracketing brand) or rinex runs between two *different* brands
    (operator must resolve manually, e.g. by reading the RINEX header).
    """

    family: str
    start: date
    end: date
    days: int
    rinex_only_days: int
    ambiguous: bool = False


@dataclass(frozen=True)
class RinexOnlySpan:
    """A contiguous span where the archive holds only RINEX, no raw.

    Operationally important: surfaces "we're losing raw" anomalies (the
    receiver was running, RINEX was produced via sbf2rin / runpkr00,
    but the raw file was never archived or was deleted). Two adjacent
    rinex-only days collapse into one span.
    """

    start: date
    end: date
    days: int


@dataclass(frozen=True)
class DataGap:
    """A multi-day gap with no archived data."""

    last_day_with_data: date
    next_day_with_data: date
    duration_days: int  # gap size: next - last - 1


def detect_data_gaps(
    timeline: List[ArchiveDay],
    *,
    min_days: int = 30,
) -> List[DataGap]:
    """Find gaps of at least ``min_days`` between consecutive archived days.

    Useful for surfacing extended station downtimes (cabling/network/
    receiver-broken). The SAVI 2014-07-08 → 2016-07-02 case is the
    canonical example — 725-day gap.
    """
    out: List[DataGap] = []
    if len(timeline) < 2:
        return out
    prev = timeline[0]
    for cur in timeline[1:]:
        delta = (cur.obs_date - prev.obs_date).days
        if delta > min_days:
            out.append(
                DataGap(
                    last_day_with_data=prev.obs_date,
                    next_day_with_data=cur.obs_date,
                    duration_days=delta - 1,
                )
            )
        prev = cur
    return out


def coalesce_brand_runs(timeline: List[ArchiveDay]) -> List[BrandRun]:
    """Collapse the per-day timeline into brand-aware contiguous spans.

    The naive run-detector (used in v0 of the verb) shows every
    family-change as a new run, which puts ``rinex`` rows in the brand
    timeline. RINEX is format-neutral — those days aren't a brand
    change, just days where the raw file is missing.

    This coalescer absorbs ``rinex`` runs into their surrounding brand
    when both neighbours have the SAME real brand (which is the typical
    pattern — receiver didn't change, raw was just lost for a stretch).
    The absorbed rinex day count is recorded on the surviving
    :class:`BrandRun` as ``rinex_only_days``.

    Edge cases marked ``ambiguous=True``:

    * Leading rinex run (no brand before it) — emitted as
      ``family="rinex"`` so the operator can decide what brand to
      attribute those days to.
    * Trailing rinex run (no brand after it) — same.
    * Rinex sandwiched between two *different* real brands — emitted as
      its own ambiguous ``family="rinex"`` run; the operator must
      resolve manually (e.g. by reading a RINEX header from the
      ambiguous span).
    """
    if not timeline:
        return []

    # First pass: build naive same-family runs (every change → new run).
    @dataclass
    class _Naive:
        family: str
        start: date
        end: date
        days: int

    naive: List[_Naive] = []
    run_start = timeline[0]
    prev = timeline[0]
    for cur in timeline[1:]:
        if cur.family != prev.family:
            naive.append(
                _Naive(
                    family=run_start.family,
                    start=run_start.obs_date,
                    end=prev.obs_date,
                    days=(prev.obs_date - run_start.obs_date).days + 1,
                )
            )
            run_start = cur
        prev = cur
    naive.append(
        _Naive(
            family=run_start.family,
            start=run_start.obs_date,
            end=prev.obs_date,
            days=(prev.obs_date - run_start.obs_date).days + 1,
        )
    )

    # Second pass: walk the naive runs, fold rinex into surrounding
    # same-brand neighbours; emit ambiguous spans where unresolvable.
    out: List[BrandRun] = []
    i = 0
    while i < len(naive):
        cur = naive[i]
        if cur.family != "rinex":
            # Look ahead: if the next run is rinex AND the one after
            # that is the SAME brand as cur, absorb both into cur.
            absorbed_rinex = 0
            run_end = cur.end
            j = i + 1
            while (
                j + 1 < len(naive)
                and naive[j].family == "rinex"
                and naive[j + 1].family == cur.family
            ):
                absorbed_rinex += naive[j].days
                run_end = naive[j + 1].end
                j += 2
            out.append(
                BrandRun(
                    family=cur.family,
                    start=cur.start,
                    end=run_end,
                    days=(run_end - cur.start).days + 1,
                    rinex_only_days=absorbed_rinex,
                )
            )
            i = j
        else:
            # Standalone rinex run — at start, at end, or between
            # different brands. Emit as ambiguous.
            out.append(
                BrandRun(
                    family="rinex",
                    start=cur.start,
                    end=cur.end,
                    days=cur.days,
                    rinex_only_days=cur.days,
                    ambiguous=True,
                )
            )
            i += 1
    return out


def detect_rinex_only_spans(timeline: List[ArchiveDay]) -> List[RinexOnlySpan]:
    """Surface contiguous spans where the archive carries only RINEX.

    Operationally important — RINEX-only days mean the raw receiver
    file is missing (never archived or deleted). Adjacent rinex days
    collapse into one span so the operator sees the windows at a glance.

    Decoupled from :func:`coalesce_brand_runs`: that function answers
    "what brand was here?" by absorbing rinex into surrounding brand;
    this function answers "where is raw missing?" regardless of brand.
    """
    out: List[RinexOnlySpan] = []
    span_start: Optional[ArchiveDay] = None
    prev: Optional[ArchiveDay] = None
    for day in timeline:
        if day.family == "rinex":
            if span_start is None:
                span_start = day
            prev = day
        else:
            if span_start is not None and prev is not None:
                out.append(
                    RinexOnlySpan(
                        start=span_start.obs_date,
                        end=prev.obs_date,
                        days=(prev.obs_date - span_start.obs_date).days + 1,
                    )
                )
                span_start = None
                prev = None
    if span_start is not None and prev is not None:
        out.append(
            RinexOnlySpan(
                start=span_start.obs_date,
                end=prev.obs_date,
                days=(prev.obs_date - span_start.obs_date).days + 1,
            )
        )
    return out
