"""Fleet-wide orchestrators for ``tos fleet triage`` / ``tos fleet status``.

Phase 4 of the station_triage pipeline (Phase 1 = single-station triage,
Phases 2-3 = verify verb + verify-from-rinex section, Phase 4 = fleet
loops).

Design principles
-----------------

* **Reuse existing single-station code paths.** Calls
  :func:`station_triage.generate_station_triage` per-station; the
  ``fleet`` layer does not re-implement any audit logic. It is a loop,
  a progress reporter, a results aggregator, and a JSON / text
  renderer — nothing else.
* **Sequential execution.** ~173 GNSS stations, each a few seconds of
  HTTP. A fleet run is 5-15 min. Sequential keeps the code obvious and
  the progress reporting honest; parallelism is a follow-up if a real
  ops scenario demands it (TODO comment below ``_iterate_fleet``).
* **One station's failure does not stop the run.** Each per-station
  call is wrapped — exceptions are captured into the result row so the
  rest of the fleet still gets audited / triaged. ``fleet status``
  exit code 2 surfaces "audit broken on at least one station".
* **Skip-clean by default for triage.** Writing 100+ empty triage files
  every day creates noise. Operators opt IN to include-clean if they
  want the full inventory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from tostools.api.tos_client import TOSClient
from tostools.audit import REAL_STATION_SUBTYPES
from tostools.audit_contact_dates import (
    ContactDateViolation,
    audit_station_contact_dates,
)
from tostools.history import (
    ParentEntity,
    default_station_cfg_path,
    read_station_markers,
    read_station_roles,
    resolve_marker_to_entity_id,
)
from tostools.station_triage import (
    STATUS_EXIT_CODE,
    STATUS_MARK,
    StationTriageReport,
    classify_station_triage,
    default_triage_path,
    format_station_triage,
    generate_station_triage,
    now_iso_utc,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FleetStationResult:
    """Outcome of running one audit pass against one station.

    ``status`` carries the same three-way verdict as ``tos station
    verify``:

      * ``"clean"`` — audits ran and found nothing
      * ``"findings"`` — at least one violation surfaced
      * ``"failed"`` — at least one audit raised (lookup error,
        malformed catalog, ...). Distinct from ``findings`` so cron / CI
        can tell "station needs work" from "oracle broken".
    """

    station: str
    station_id: Optional[int]
    status: str  # "clean" | "findings" | "failed"
    findings_count: int
    missing_count: int
    dates_count: int
    rinex_count: int
    coverage_count: int = 0
    notes: List[str] = field(default_factory=list)
    # Only populated for the triage flow: where the file landed on disk
    # (None when the station was clean and skip-clean was on).
    triage_path: Optional[Path] = None
    # Caught exception text, surfaced on status="failed" for the
    # specific case where the per-station call raised outright (rather
    # than a sub-audit catching its own exception into ``notes``).
    error: Optional[str] = None


@dataclass
class FleetRunSummary:
    """Aggregate of every per-station result + run-level metadata."""

    run_kind: str  # "triage" | "status"
    generated_at: str
    results: List[FleetStationResult] = field(default_factory=list)
    skipped_no_id: List[str] = field(default_factory=list)
    # When triage runs, the per-station triage files land under this
    # directory tree. Surfaced in the summary so the operator can grep
    # / open the output.
    out_dir: Optional[Path] = None
    with_archive: bool = False

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def clean(self) -> int:
        return sum(1 for r in self.results if r.status == "clean")

    @property
    def findings(self) -> int:
        return sum(1 for r in self.results if r.status == "findings")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")

    @property
    def total_findings(self) -> int:
        return sum(r.findings_count for r in self.results)

    def exit_code(self) -> int:
        """Verify-style oracle exit code rolled up across the fleet.

        Uses the canonical per-status mapping from
        :data:`station_triage.STATUS_EXIT_CODE` and takes the worst
        seen, so a single failed audit promotes the whole run to 2
        even if 172 stations are clean. ``failed`` (2) > ``findings``
        (1) > ``clean`` (0) by design — operators need to know "the
        oracle is broken" before "K stations need work".
        """
        return max(
            (STATUS_EXIT_CODE[r.status] for r in self.results),
            default=0,
        )


# ---------------------------------------------------------------------------
# Fleet enumeration
# ---------------------------------------------------------------------------


def enumerate_fleet_stations(
    client: TOSClient,
    *,
    station_cfg_path: Optional[str] = None,
    include: Optional[Sequence[str]] = None,
    exclude: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    enumerate_progress: Optional[Callable[[int, int], None]] = None,
) -> List[ParentEntity]:
    """Return the fleet of GNSS stations as a list of :class:`ParentEntity`.

    Drives off ``stations.cfg`` directly (rather than
    :func:`history.enumerate_known_parents` which also pulls in
    infrastructure / warehouses). Each section name in the cfg is a
    marker; we resolve to ``id_entity`` via
    :func:`history.resolve_marker_to_entity_id` and check
    ``code_entity_subtype in REAL_STATION_SUBTYPES``.

    The returned :class:`ParentEntity` rows carry the **marker** in
    their ``name`` field — not the human-readable Icelandic station
    name. Downstream :func:`station_triage.generate_station_triage`
    expects markers (e.g. ``"HEDI"``), so wiring is direct: pass
    ``parent.name`` straight through.

    Filters (all optional, AND'd together):

      ``include``  Keep only these markers (case-insensitive). Applied
                   **before** marker→id resolution, so a 2-station run
                   makes ~2 HTTP calls, not 173. Worth knowing for
                   smoke-test runs.
      ``exclude``  Drop these markers. Same pre-resolution filter.
      ``limit``    Stop after N markers (post-filter). Test helper.

    Raises
    ------
    RuntimeError
        When zero stations resolve (cfg missing, all markers filtered
        out, or none resolve to a geophysical entity).
    """
    include_set = {m.upper() for m in include} if include else None
    exclude_set = {m.upper() for m in exclude} if exclude else set()

    cfg_path = station_cfg_path or default_station_cfg_path()
    if not cfg_path:
        raise RuntimeError(
            "enumerate_fleet_stations: no stations.cfg found. Set "
            "$GPS_CONFIG_PATH or pass --stations-cfg PATH explicitly."
        )

    markers = read_station_markers(cfg_path)
    # Passive (data-source-only) stations have no TOS counterpart — drop
    # them BEFORE marker→id resolution so they cost zero HTTP calls
    # (same pre-resolution rationale as include/exclude).
    roles = read_station_roles(cfg_path)
    candidates: List[str] = []
    passive_skipped = 0
    for m in markers:
        upper = m.upper()
        if roles.get(upper, "active") == "passive":
            passive_skipped += 1
            continue
        if include_set is not None and upper not in include_set:
            continue
        if upper in exclude_set:
            continue
        candidates.append(m)
    if passive_skipped:
        logger.info(
            "enumerate_fleet_stations: skipped %d passive "
            "(data-source-only) stations",
            passive_skipped,
        )
    if limit is not None:
        candidates = candidates[:limit]

    if not candidates:
        raise RuntimeError(
            "enumerate_fleet_stations: zero markers after filter. "
            "Check --include / --exclude / --stations-cfg arguments."
        )

    stations: List[ParentEntity] = []
    total = len(candidates)
    for i, marker in enumerate(candidates, start=1):
        if enumerate_progress is not None:
            enumerate_progress(i, total)
        try:
            eid = resolve_marker_to_entity_id(client, marker)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "enumerate_fleet_stations: resolve(%r) raised: %s; skipping",
                marker,
                exc,
            )
            continue
        if eid is None:
            logger.debug(
                "enumerate_fleet_stations: marker %r not found in TOS",
                marker,
            )
            continue
        try:
            history = client.get_entity_history(eid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "enumerate_fleet_stations: get_entity_history(%d) raised: "
                "%s; skipping",
                eid,
                exc,
            )
            continue
        if not history:
            continue
        subtype = str(history.get("code_entity_subtype") or "")
        if subtype not in REAL_STATION_SUBTYPES:
            # E.g. an obsolete marker that now points at a non-station
            # entity — skip silently. Hit-rate is near-100% in practice.
            continue
        stations.append(
            ParentEntity(
                id_entity=eid,
                name=marker,  # marker, not display name — see docstring
                code_subtype=subtype,
                role="station",
            )
        )

    if not stations:
        raise RuntimeError(
            "enumerate_fleet_stations: zero stations resolved. "
            f"Cfg {cfg_path} listed {total} candidate(s) after filters; "
            "none were geophysical entities reachable in TOS."
        )

    return stations


# ---------------------------------------------------------------------------
# Per-station evaluation (shared between triage + status)
# ---------------------------------------------------------------------------


def _result_from_report(
    station: str, report: StationTriageReport
) -> FleetStationResult:
    """Build a :class:`FleetStationResult` from a triage report.

    Status classification delegates to
    :func:`station_triage.classify_station_triage` so single-station
    verify + fleet status share one oracle definition. Same with the
    rinex count via :attr:`StationRinexReport.finding_count`.
    """
    missing_count = len(report.missing.violations) if report.missing else 0
    dates_count = len(report.dates.violations) if report.dates else 0
    rinex_count = report.rinex.finding_count if report.rinex is not None else 0
    coverage_count = (
        len(report.coverage.violations) if report.coverage is not None else 0
    )
    return FleetStationResult(
        station=station,
        station_id=report.station_id,
        status=classify_station_triage(report),
        findings_count=report.total_findings,
        missing_count=missing_count,
        dates_count=dates_count,
        rinex_count=rinex_count,
        coverage_count=coverage_count,
        notes=list(report.notes),
    )


PerStationFn = Callable[[ParentEntity, StationTriageReport, "FleetStationResult"], None]


def _iterate_fleet(
    client: TOSClient,
    stations: Sequence[ParentEntity],
    *,
    per_station_fn: PerStationFn,
    progress: Optional[Callable[[int, int, FleetStationResult], None]] = None,
    triage_kwargs: Optional[Dict[str, Any]] = None,
    generated_at: Optional[str] = None,
) -> List[FleetStationResult]:
    """Shared loop body for fleet triage + fleet status.

    For each station: resolve marker → run generate_station_triage →
    classify into a :class:`FleetStationResult` → call
    ``per_station_fn(parent, report, result)`` for any side-effect (write
    triage file, mutate ``result.triage_path``, …) → emit progress.

    The result is passed mutable into the callback so triage can stash
    its written path back onto the row without a side-channel dict.

    Exceptions thrown by ``generate_station_triage`` itself (rare —
    each sub-audit already catches into ``notes``) and exceptions
    thrown by the side-effect callback are both caught and promoted to
    ``status="failed"`` so a single broken station does not abort the
    rest of the fleet.

    Parallelism note: sequential by design. A typical 173-station run
    on a warm cache is 5-15 minutes. If wall-clock becomes a problem,
    swap this loop for ``concurrent.futures.ThreadPoolExecutor`` —
    each ``generate_station_triage`` is I/O-bound and independent.
    """
    results: List[FleetStationResult] = []
    triage_kwargs = dict(triage_kwargs or {})

    for idx, parent in enumerate(stations, start=1):
        marker = parent.name or f"id={parent.id_entity}"
        try:
            report = generate_station_triage(
                marker,
                client=client,
                generated_at=generated_at,
                **triage_kwargs,
            )
            result = _result_from_report(marker, report)
            try:
                per_station_fn(parent, report, result)
            except Exception as exc:  # noqa: BLE001
                # Side-effect failure (e.g. write_text IOError) should
                # not mask the audit findings — promote to failed and
                # preserve the underlying counts.
                logger.warning("fleet side-effect failed for %s: %s", marker, exc)
                result.status = "failed"
                result.error = f"side-effect failed: {exc}"
                result.notes.append(result.error)
        except Exception as exc:  # noqa: BLE001
            # Catastrophic per-station failure — log and continue.
            logger.warning("fleet station %s raised: %s", marker, exc)
            result = FleetStationResult(
                station=marker,
                station_id=parent.id_entity,
                status="failed",
                findings_count=0,
                missing_count=0,
                dates_count=0,
                rinex_count=0,
                coverage_count=0,
                error=str(exc),
                notes=[f"generate_station_triage raised: {exc}"],
            )

        results.append(result)
        if progress is not None:
            progress(idx, len(stations), result)

    return results


# ---------------------------------------------------------------------------
# Run kinds
# ---------------------------------------------------------------------------


def run_fleet_triage(
    client: TOSClient,
    *,
    stations: Optional[Sequence[ParentEntity]] = None,
    out_dir: Optional[Path] = None,
    include_clean: bool = False,
    use_suppressions: bool = True,
    suppressions_path: Optional[Path] = None,
    catalog_path: Optional[Path] = None,
    with_archive: bool = False,
    archive_root: Optional[Path] = None,
    min_gap_days: float = 30.0,
    with_coverage: bool = False,
    coverage_since: Optional[str] = None,
    coverage_window_days: int = 7,
    station_cfg_path: Optional[str] = None,
    include: Optional[Sequence[str]] = None,
    exclude: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    progress: Optional[Callable[[int, int, FleetStationResult], None]] = None,
    enumerate_progress: Optional[Callable[[int, int], None]] = None,
    generated_at: Optional[str] = None,
) -> FleetRunSummary:
    """Generate per-station triage files across the fleet.

    Files land under ``data/triage/<station>/<station>_audit_<DATE>.txt``
    (one subdirectory per station — see
    :func:`station_triage.default_triage_path`). Same-day re-runs
    overwrite that day's file; subsequent days produce a new dated
    file alongside.

    ``include_clean=False`` (default) skips writing files for stations
    where every audit returned no findings. Operators almost never
    want a 100+ empty-file dump; opt in with ``--include-clean`` when
    a full inventory is desired.

    Per-station failures (e.g. transient TOS lookup error) are
    captured in the result but do NOT abort the run.

    Parameters mirror :func:`station_triage.generate_station_triage`
    one-for-one; the fleet layer is a loop over them.
    """
    generated_at = generated_at or now_iso_utc()
    if out_dir is None:
        # Default to the gps-tos-corrections repo (not cwd-relative) so triage
        # files land in their canonical, versioned home.
        from .archive import tos_corrections_dir

        out_dir = tos_corrections_dir()

    if stations is None:
        stations = enumerate_fleet_stations(
            client,
            station_cfg_path=station_cfg_path,
            include=include,
            exclude=exclude,
            limit=limit,
            enumerate_progress=enumerate_progress,
        )

    triage_kwargs = {
        "use_suppressions": use_suppressions,
        "suppressions_path": suppressions_path,
        "catalog_path": catalog_path,
        "with_archive": with_archive,
        "archive_root": archive_root,
        "min_gap_days": min_gap_days,
        "with_coverage": with_coverage,
        "coverage_since": coverage_since,
        "coverage_window_days": coverage_window_days,
    }

    def _write_triage(
        parent: ParentEntity,
        report: StationTriageReport,
        result: FleetStationResult,
    ) -> None:
        # Skip clean stations unless operator opted in. ``failed``
        # stations still write so the operator has a record of the
        # failure notes (and can debug from the file).
        if result.status == "clean" and not include_clean:
            return
        marker = parent.name or f"id={parent.id_entity}"
        # default_triage_path() returns base_dir/data/triage/<stn>/...,
        # but here we drive the triage root directly via ``out_dir``
        # (which may be anywhere — tmp_path in tests, an explicit
        # operator override on the CLI). Reuse the date-stamped
        # filename portion from default_triage_path so naming stays
        # consistent across single-station + fleet flows.
        canonical = default_triage_path(marker)
        out_path = out_dir / marker.lower() / canonical.name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = format_station_triage(report)
        out_path.write_text(rendered, encoding="utf-8")
        result.triage_path = out_path

    results = _iterate_fleet(
        client,
        stations,
        per_station_fn=_write_triage,
        progress=progress,
        triage_kwargs=triage_kwargs,
        generated_at=generated_at,
    )

    return FleetRunSummary(
        run_kind="triage",
        generated_at=generated_at,
        results=results,
        out_dir=out_dir,
        with_archive=with_archive,
    )


def run_fleet_verify(
    client: TOSClient,
    *,
    stations: Optional[Sequence[ParentEntity]] = None,
    use_suppressions: bool = True,
    suppressions_path: Optional[Path] = None,
    catalog_path: Optional[Path] = None,
    with_archive: bool = False,
    archive_root: Optional[Path] = None,
    min_gap_days: float = 30.0,
    with_coverage: bool = False,
    coverage_since: Optional[str] = None,
    coverage_window_days: int = 7,
    station_cfg_path: Optional[str] = None,
    include: Optional[Sequence[str]] = None,
    exclude: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    progress: Optional[Callable[[int, int, FleetStationResult], None]] = None,
    enumerate_progress: Optional[Callable[[int, int], None]] = None,
    generated_at: Optional[str] = None,
) -> FleetRunSummary:
    """Run :func:`station_triage.generate_station_triage` across the
    fleet without writing anything — the verify oracle in bulk form.

    The returned :class:`FleetRunSummary` is the input to
    :func:`format_fleet_summary` for human-readable output and to
    :func:`fleet_summary_to_dict` for JSON.
    """
    generated_at = generated_at or now_iso_utc()
    if stations is None:
        stations = enumerate_fleet_stations(
            client,
            station_cfg_path=station_cfg_path,
            include=include,
            exclude=exclude,
            limit=limit,
            enumerate_progress=enumerate_progress,
        )

    triage_kwargs = {
        "use_suppressions": use_suppressions,
        "suppressions_path": suppressions_path,
        "catalog_path": catalog_path,
        "with_archive": with_archive,
        "archive_root": archive_root,
        "min_gap_days": min_gap_days,
        "with_coverage": with_coverage,
        "coverage_since": coverage_since,
        "coverage_window_days": coverage_window_days,
    }

    # Verify is read-only — _iterate_fleet still classifies and builds
    # the FleetStationResult; no per-station side effect required.
    results = _iterate_fleet(
        client,
        stations,
        per_station_fn=lambda *_args: None,
        progress=progress,
        triage_kwargs=triage_kwargs,
        generated_at=generated_at,
    )

    return FleetRunSummary(
        run_kind="status",
        generated_at=generated_at,
        results=results,
        with_archive=with_archive,
    )


# ---------------------------------------------------------------------------
# Fleet contact-dates sweep (migration-artifact relationship dates)
# ---------------------------------------------------------------------------


@dataclass
class FleetContactDatesStation:
    """One station's contact-dates result in the fleet sweep."""

    marker: str
    station_id: Optional[int]
    violations: List[ContactDateViolation] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class FleetContactDatesSummary:
    """Aggregate of a fleet-wide contact-dates sweep."""

    generated_at: str
    stations: List[FleetContactDatesStation] = field(default_factory=list)

    @property
    def stations_audited(self) -> int:
        return len(self.stations)

    @property
    def clean(self) -> int:
        return sum(1 for s in self.stations if not s.violations and not s.error)

    @property
    def with_violations(self) -> int:
        return sum(1 for s in self.stations if s.violations)

    @property
    def errors(self) -> int:
        return sum(1 for s in self.stations if s.error)

    @property
    def total_violations(self) -> int:
        return sum(len(s.violations) for s in self.stations)

    @property
    def all_violations(
        self,
    ) -> "List[tuple[FleetContactDatesStation, ContactDateViolation]]":
        out = []
        for s in self.stations:
            for v in s.violations:
                out.append((s, v))
        return out


def run_fleet_contact_dates(
    client: TOSClient,
    *,
    stations: Optional[Sequence[ParentEntity]] = None,
    use_suppressions: bool = True,
    suppressions_path: Optional[Path] = None,
    station_cfg_path: Optional[str] = None,
    include: Optional[Sequence[str]] = None,
    exclude: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    progress: Optional[Callable[[int, int, FleetContactDatesStation], None]] = None,
    enumerate_progress: Optional[Callable[[int, int], None]] = None,
    generated_at: Optional[str] = None,
) -> FleetContactDatesSummary:
    """Sweep :func:`audit_station_contact_dates` across the GNSS fleet.

    Read-only: one ``get_entity_history`` + one ``get_contacts`` per
    station (after marker resolution). Migration artifacts are a
    one-time cleanup, so this is a standalone sweep — not wired into the
    recurring verify oracle.

    Per-station failures are captured on the result, never abort the run.
    """
    generated_at = generated_at or now_iso_utc()
    if stations is None:
        stations = enumerate_fleet_stations(
            client,
            station_cfg_path=station_cfg_path,
            include=include,
            exclude=exclude,
            limit=limit,
            enumerate_progress=enumerate_progress,
        )

    summary = FleetContactDatesSummary(generated_at=generated_at)
    for idx, parent in enumerate(stations, start=1):
        marker = parent.name or f"id={parent.id_entity}"
        st = FleetContactDatesStation(marker=marker, station_id=parent.id_entity)
        try:
            report = audit_station_contact_dates(
                client,
                id_entity=parent.id_entity,
                use_suppressions=use_suppressions,
                suppressions_path=suppressions_path,
            )
            st.violations = list(report.violations)
        except Exception as exc:  # noqa: BLE001
            logger.warning("contact-dates sweep failed for %s: %s", marker, exc)
            st.error = str(exc)
        summary.stations.append(st)
        if progress is not None:
            progress(idx, len(stations), st)
    return summary


#: Roles whose `time_from` can always be safely backdated to the
#: station's `start` (founding): the owner owned the station from
#: founding regardless of which org. Non-owner roles (data_owner,
#: operator, observer) may have genuinely later real start dates, so
#: those stay commented for operator review.
_HIGH_CONFIDENCE_CONTACT_ROLES = frozenset({"owner"})


def _classify_contact_violation(
    s: "FleetContactDatesStation",
    v: "ContactDateViolation",
    owner_counts: Dict[Optional[int], int],
) -> str:
    """Three-way confidence bucket for one flagged relationship.

    * ``high``  — owner role, **open** (per_time_to is None), and the
      ONLY flagged owner on its station. Backdating to ``start``
      (founding) is unambiguously correct. Emitted uncommented.
    * ``transfer`` — owner role but **closed** (per_time_to set) OR the
      station has >1 flagged owner. These are ownership-transfer /
      multi-owner histories where a blanket backdate-to-founding would
      overlap periods or erase the transfer (e.g. Jarðvísindastofnun →
      Veðurstofa). The *current* owner's time_from is often the real
      transfer date, not founding. Emitted COMMENTED for review.
    * ``low``   — non-owner role (data_owner / operator / observer);
      may have a genuinely recent real start date. Emitted COMMENTED.
    """
    role = v.role or ""
    if role not in _HIGH_CONFIDENCE_CONTACT_ROLES:
        return "low"
    if v.per_time_to is not None:
        return "transfer"
    if owner_counts.get(s.station_id, 0) > 1:
        return "transfer"
    return "high"


def format_fleet_contact_dates_triage(
    summary: FleetContactDatesSummary,
) -> str:
    """Render a single combined triage file for the fleet sweep.

    Three confidence buckets (see :func:`_classify_contact_violation`):

    * **HIGH** (owner, open, single-owner-on-station) — emitted
      **uncommented**: backdating to ``start``/founding is unambiguous.
    * **TRANSFER** (owner but closed or multi-owner station) — emitted
      **commented**: ownership-transfer histories where a blanket
      backdate would overlap/erase periods. Needs operator review.
    * **LOW** (non-owner roles) — emitted **commented** for review.

    Each line is a ``patch-contact-relationship <id_rel> time_from
    start`` ACTION whose ``id_entity`` slot is the station.
    """
    # Per-station count of flagged OWNER relationships — drives the
    # multi-owner / transfer demotion.
    owner_counts: Dict[Optional[int], int] = {}
    for s, v in summary.all_violations:
        if (v.role or "") in _HIGH_CONFIDENCE_CONTACT_ROLES:
            owner_counts[s.station_id] = owner_counts.get(s.station_id, 0) + 1

    high, transfer, low = [], [], []
    for s, v in summary.all_violations:
        bucket = _classify_contact_violation(s, v, owner_counts)
        {"high": high, "transfer": transfer, "low": low}[bucket].append((s, v))

    def _emit(items, *, commented: bool) -> None:
        prefix = "#ACTION" if commented else "ACTION"
        if not items:
            lines.append("# (none)")
            return
        for s, v in sorted(items, key=lambda sv: (sv[0].marker, sv[1].id_relationship)):
            label = f" {v.contact_label!r}" if v.contact_label else ""
            to_note = f" → {v.per_time_to}" if v.per_time_to else ""
            lines.append(
                f"# {s.marker} rel {v.id_relationship}{label} role={v.role}"
                f"  (per_time_from={v.per_time_from}{to_note})"
            )
            lines.append(
                f"{prefix} {s.station_id} patch-contact-relationship "
                f"{v.id_relationship} time_from start"
            )

    lines: List[str] = []
    lines.append("# === tos fleet contact-dates — combined triage action file ===")
    lines.append(f"# Generated:  {summary.generated_at}")
    lines.append(f"# Stations:   {summary.stations_audited} audited")
    lines.append(f"# Violations: {summary.total_violations} total")
    lines.append(f"#   HIGH     (owner, open, single — uncommented):   {len(high)}")
    lines.append(f"#   TRANSFER (owner but closed / multi-owner):      {len(transfer)}")
    lines.append(f"#   LOW      (non-owner role):                      {len(low)}")
    lines.append("#")
    lines.append("# Each line backdates time_from to `start` (the station's")
    lines.append("# earliest_known / founding date), resolved at apply time.")
    lines.append("#")
    lines.append("# Workflow:")
    lines.append("#   tos audit apply <file>          # dry-run preview")
    lines.append("#   tos audit apply <file> --apply  # commit writes")
    lines.append("")

    lines.append("# ── HIGH confidence: owner relationships, open, sole owner of the")
    lines.append("#    station (owner owned it from founding → `start` is correct) ──")
    _emit(high, commented=False)
    lines.append("")

    lines.append("# ── TRANSFER / multi-owner: owner relationships that are CLOSED or")
    lines.append("#    share the station with another flagged owner. The current")
    lines.append("#    owner's time_from is often the real TRANSFER date, not")
    lines.append("#    founding — backdating blindly overlaps periods / erases the")
    lines.append("#    transfer. REVIEW each station's owner history first ──")
    _emit(transfer, commented=True)
    lines.append("")

    lines.append("# ── LOW confidence: non-owner roles (data_owner / operator /")
    lines.append("#    observer) may have a genuinely recent start date — REVIEW")
    lines.append("#    each before uncommenting ──")
    _emit(low, commented=True)
    lines.append("")
    return "\n".join(lines)


def format_fleet_contact_dates_report(summary: FleetContactDatesSummary) -> str:
    """Human-readable fleet contact-dates report (counts + batch breakdown)."""
    from collections import Counter

    lines: List[str] = []
    lines.append(
        f"=== FLEET CONTACT-DATES — {summary.generated_at} "
        f"({summary.stations_audited} station(s)) ==="
    )
    lines.append(f"  clean:            {summary.clean}")
    lines.append(f"  with violations:  {summary.with_violations}")
    if summary.errors:
        lines.append(f"  errors:           {summary.errors}")
    lines.append(f"  total flagged relationships: {summary.total_violations}")

    byday: "Counter[str]" = Counter(
        v.per_time_from[:10] for _s, v in summary.all_violations
    )
    if byday:
        lines.append("")
        lines.append("  flagged per_time_from by day:")
        for day, n in byday.most_common():
            lines.append(f"    {day}: {n}")
    return "\n".join(lines) + "\n"


def fleet_contact_dates_to_dict(summary: FleetContactDatesSummary) -> Dict[str, Any]:
    """JSON-serialisable view of a fleet contact-dates sweep."""
    return {
        "kind": "fleet-contact-dates",
        "generated_at": summary.generated_at,
        "totals": {
            "stations_audited": summary.stations_audited,
            "clean": summary.clean,
            "with_violations": summary.with_violations,
            "errors": summary.errors,
            "total_violations": summary.total_violations,
        },
        "violations": [
            {
                "marker": s.marker,
                "station_id": s.station_id,
                "id_relationship": v.id_relationship,
                "id_contact": v.id_contact,
                "contact_label": v.contact_label,
                "role": v.role,
                "per_time_from": v.per_time_from,
                "per_time_to": v.per_time_to,
            }
            for s, v in summary.all_violations
        ],
        "errors": [
            {"marker": s.marker, "station_id": s.station_id, "error": s.error}
            for s in summary.stations
            if s.error
        ],
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def format_fleet_summary(
    summary: FleetRunSummary,
    *,
    show_clean: bool = False,
    sort_by_findings: bool = True,
) -> str:
    """Render a :class:`FleetRunSummary` as a multi-line text report.

    Header: counts by status + total findings + run kind.
    Body: per-station rows for everything except ``clean`` (which is
    suppressed unless ``show_clean=True``). Rows sort by findings
    descending so the loudest stations appear first.
    """
    lines: List[str] = []
    kind_label = {"triage": "FLEET TRIAGE", "status": "FLEET STATUS"}.get(
        summary.run_kind, summary.run_kind.upper()
    )
    lines.append(
        f"=== {kind_label} — {summary.generated_at} "
        f"({summary.total} station(s)) ==="
    )
    lines.append(
        f"  clean:    {summary.clean}\n"
        f"  findings: {summary.findings}\n"
        f"  failed:   {summary.failed}\n"
        f"  total findings across fleet: {summary.total_findings}"
    )
    if summary.with_archive:
        lines.append("  (--with-archive enabled — rinex audit included)")
    if summary.out_dir is not None:
        lines.append(f"  triage files: {summary.out_dir}")

    rows = list(summary.results)
    if not show_clean:
        rows = [r for r in rows if r.status != "clean"]
    if sort_by_findings:
        # findings desc, then station name asc for stability.
        rows.sort(key=lambda r: (-r.findings_count, r.station))

    if rows:
        lines.append("")
        lines.append(
            f"{'':2}  {'STN':<8}  {'id':>6}  {'status':<8}  "
            f"{'find':>4}  {'miss':>4}  {'date':>4}  {'rinex':>5}  "
            f"{'cov':>4}  notes"
        )
        for r in rows:
            mark = STATUS_MARK.get(r.status, "?")
            note_blurb = ""
            if r.notes:
                # Surface first note compactly; full set is in JSON.
                note_blurb = "  " + r.notes[0][:80]
            elif r.error:
                note_blurb = "  " + r.error[:80]
            lines.append(
                f"{mark:2}  {r.station[:8]:<8}  "
                f"{(r.station_id if r.station_id is not None else '—'):>6}  "
                f"{r.status:<8}  {r.findings_count:>4}  "
                f"{r.missing_count:>4}  {r.dates_count:>4}  "
                f"{r.rinex_count:>5}  {r.coverage_count:>4}{note_blurb}"
            )
    elif not show_clean and summary.clean == summary.total:
        lines.append("")
        lines.append("  (all stations clean)")

    return "\n".join(lines) + "\n"


def fleet_summary_to_dict(summary: FleetRunSummary) -> Dict[str, Any]:
    """JSON-serializable view of a :class:`FleetRunSummary`."""
    return {
        "run_kind": summary.run_kind,
        "generated_at": summary.generated_at,
        "with_archive": summary.with_archive,
        "out_dir": str(summary.out_dir) if summary.out_dir is not None else None,
        "totals": {
            "total": summary.total,
            "clean": summary.clean,
            "findings": summary.findings,
            "failed": summary.failed,
            "total_findings": summary.total_findings,
        },
        "exit_code": summary.exit_code(),
        "results": [
            {
                "station": r.station,
                "station_id": r.station_id,
                "status": r.status,
                "findings_count": r.findings_count,
                "missing_count": r.missing_count,
                "dates_count": r.dates_count,
                "rinex_count": r.rinex_count,
                "coverage_count": r.coverage_count,
                "notes": list(r.notes),
                "error": r.error,
                "triage_path": (
                    str(r.triage_path) if r.triage_path is not None else None
                ),
            }
            for r in summary.results
        ],
    }
