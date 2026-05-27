"""Orchestrator for ``tos station triage <STN>`` — auto-generate a combined
triage file for one station.

Runs each available audit (missing-attributes, attribute-dates,
verify-from-rinex) against the station, aggregates findings, and
renders a single ACTION-style file consumable by ``tos audit apply``.

Design principles
-----------------

* **Reuses existing audit code paths.** Calls
  :func:`audit_missing_attributes.audit_station_missing_attributes` +
  :func:`audit_attribute_dates.audit_station_attribute_dates` directly
  rather than re-implementing. Each section's body is built by the
  audit module's own ``format_triage_file`` helper.

* **Commented ACTIONs by default.** Operator opts IN by uncommenting.
  This matches the hand-written convention used through the SAVI
  reconstruction work. ``--aggressive`` flag (future) could flip this
  for fleet maintenance.

* **Section-by-confidence.** Sections are ordered by how confident the
  audit is in its suggestion: cleanup-artifact backdates (HIGH —
  anchored in well-documented pattern), catalog-default backfills
  (MEDIUM — defaults are documented but may need site-specific
  override), placeholder-required attributes (LOW — operator MUST
  fill).

* **No partial state.** Either generate the whole file or fail loudly.
  Half-built triage files are dangerous.

The output file is deterministic given the same TOS state + a fixed
``generated_at`` (passable in tests). This lets ``test_station_triage.py``
pin format stability.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from tostools.api.tos_client import TOSClient
from tostools.audit_attribute_dates import (
    StationAttributeDateReport,
    audit_station_attribute_dates,
)
from tostools.audit_attribute_dates import format_triage_file as format_dates_triage
from tostools.audit_missing_attributes import (
    StationMissingAttributesReport,
    audit_station_missing_attributes,
)
from tostools.audit_missing_attributes import (
    format_triage_file as format_missing_triage,
)

logger = logging.getLogger(__name__)


@dataclass
class StationTriageReport:
    """Aggregated audit findings for one station.

    Cherry-picks the sub-reports produced by each individual audit so
    the renderer can iterate uniformly. Sub-reports stay as their own
    typed dataclasses — this aggregator does not flatten or normalize
    them, since each audit's ``format_triage_file`` already knows how
    to render itself.
    """

    station: str
    station_id: Optional[int]
    generated_at: str
    # Per-audit sub-reports — None if the audit was skipped or had no
    # findings worth surfacing. The renderer omits empty sections.
    missing: Optional[StationMissingAttributesReport] = None
    dates: Optional[StationAttributeDateReport] = None
    notes: List[str] = field(default_factory=list)

    @property
    def total_findings(self) -> int:
        """Number of actionable suggestions across all sub-reports.

        Used in the header line so operators can tell at a glance
        whether a station needs attention.
        """
        n = 0
        if self.missing is not None:
            n += len(self.missing.violations)
        if self.dates is not None:
            n += len(self.dates.violations)
        return n


def generate_station_triage(
    station: str,
    *,
    client: Optional[TOSClient] = None,
    generated_at: Optional[str] = None,
) -> StationTriageReport:
    """Run all audits on ``station`` and aggregate into a single report.

    Parameters
    ----------
    station
        Station marker (e.g. ``"HEDI"``) or display name. Resolved by
        the underlying audits; both accept the same identifier shape.
    client
        Optional :class:`TOSClient`. One is constructed if omitted —
        each audit shares the same client so token-cached lookups
        amortise across the run.
    generated_at
        Optional ISO-8601 timestamp; defaults to ``utcnow()``. Pinned
        explicitly in tests to keep the format byte-deterministic.

    Returns
    -------
    StationTriageReport
        Aggregated findings. Caller renders via
        :func:`format_station_triage`.
    """
    if client is None:
        client = TOSClient()

    if generated_at is None:
        # Drop the +00:00 suffix that isoformat() emits and substitute "Z"
        # — keeps the header header byte-deterministic + concise.
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        generated_at = now.isoformat(timespec="seconds") + "Z"

    notes: List[str] = []

    # === Section: missing attributes ===
    missing_report: Optional[StationMissingAttributesReport]
    try:
        missing_report = audit_station_missing_attributes(client, name=station)
    except Exception as exc:  # noqa: BLE001
        logger.warning("missing-attributes audit failed on %s: %s", station, exc)
        notes.append(f"missing-attributes audit FAILED: {exc}")
        missing_report = None

    # === Section: suspicious attribute dates ===
    dates_report: Optional[StationAttributeDateReport]
    try:
        dates_report = audit_station_attribute_dates(client, name=station)
    except Exception as exc:  # noqa: BLE001
        logger.warning("attribute-dates audit failed on %s: %s", station, exc)
        notes.append(f"attribute-dates audit FAILED: {exc}")
        dates_report = None

    # Resolve a station_id for the header. Prefer whichever sub-report
    # successfully looked one up; both audits resolve the same way.
    station_id: Optional[int] = None
    if missing_report is not None:
        station_id = missing_report.station_id
    elif dates_report is not None:
        station_id = dates_report.station_id

    return StationTriageReport(
        station=station,
        station_id=station_id,
        generated_at=generated_at,
        missing=missing_report,
        dates=dates_report,
        notes=notes,
    )


def format_station_triage(report: StationTriageReport) -> str:
    """Render a :class:`StationTriageReport` as a multi-section triage file.

    Output structure (sections that have no findings are omitted):

    1. **Header** — station identity, audit summary, run/verify hints
    2. **Suspicious attribute dates** (HIGH confidence — cleanup-artifact
       pattern is well-documented)
    3. **Missing required attributes** (MEDIUM/LOW per FILL placeholders)
    4. **Footer** — verification command hints

    Each sub-section's body is delegated to the originating audit
    module's ``format_triage_file`` so format-stability of those audits
    is preserved.
    """
    parts: List[str] = []

    parts.append(_build_header(report))

    if report.dates is not None and report.dates.violations:
        parts.append(_section_dates(report))

    if report.missing is not None and report.missing.violations:
        parts.append(_section_missing(report))

    if report.notes:
        parts.append(_section_notes(report))

    parts.append(_build_footer(report))

    return "\n\n".join(parts) + "\n"


def _build_header(report: StationTriageReport) -> str:
    summary_lines = [
        f"#   missing-attributes:  "
        f"{'(failed)' if report.missing is None else f'{len(report.missing.violations)} violation(s)'}",
        f"#   attribute-dates:     "
        f"{'(failed)' if report.dates is None else f'{len(report.dates.violations)} violation(s)'}",
    ]
    return (
        f"# === {report.station} station triage — auto-generated "
        f"{report.generated_at} ===\n"
        f"#\n"
        f"# Station id_entity={report.station_id}  ({report.total_findings} "
        f"total finding(s))\n"
        f"# Audit summary:\n" + "\n".join(summary_lines) + "\n#\n"
        "# Run:\n"
        "#   tos audit apply <this_file>          # dry-run (safe default)\n"
        "#   tos audit apply <this_file> --apply  # commit\n"
        "#\n"
        "# Convention: ACTION lines below are SUGGESTED, COMMENTED OUT by\n"
        "# default. To accept, remove the leading '#'. To customize, edit\n"
        "# values + uncomment. To skip, leave commented or delete the line.\n"
        "# Replace any <FILL_VALUE> placeholders before --apply.\n"
        "#\n"
        "# Date tokens: `start` (entity's earliest_known) and `now` (today\n"
        "# UTC) are resolved at apply-time by `tos audit apply` — operators\n"
        "# don't need to look up concrete dates for the common case."
    )


def _section_dates(report: StationTriageReport) -> str:
    assert report.dates is not None
    body = format_dates_triage(
        report.dates,
        audit_command=f"tos audit attribute-dates {report.station}",
        generated_at=report.generated_at,
    )
    header = (
        "# ──────────────────────────────────────────────────────────────────\n"
        "# Section: suspicious attribute dates\n"
        "# CONFIDENCE: HIGH — date_from=2014-10-17 is the fleet-wide bulk-load\n"
        "# pattern (see memory project_2014_10_17_metadata_cleanup_artifacts).\n"
        "# Backdating to actual install date typically uncontroversial.\n"
        "# ──────────────────────────────────────────────────────────────────"
    )
    return header + "\n" + body.rstrip()


def _substitute_fill_date_with_start(body: str) -> str:
    """Rewrite ``<FILL_DATE>`` placeholders to ``start``.

    The audit's per-entity ``format_triage_file`` emits ``<FILL_DATE>``
    when it has no specific date hint to suggest. With the apply
    dispatcher's ``now`` / ``start`` token resolver in place, ``start``
    is a better default: it resolves at apply-time to the entity's
    audit-computed earliest_known anchor, which is exactly the date
    most operators would otherwise hand-type.

    This is a pure-substitution post-process: we don't touch
    ``<FILL_VALUE>`` (those genuinely need operator input), and we
    don't touch lines that already carry a concrete date.
    """
    return body.replace("<FILL_DATE>", "start")


def _section_missing(report: StationTriageReport) -> str:
    assert report.missing is not None
    body = format_missing_triage(
        report.missing,
        audit_command=f"tos audit missing-attributes {report.station}",
        generated_at=report.generated_at,
    )
    # Swap <FILL_DATE> → start so most lines are immediately applyable
    # without operator typing — the dispatcher's token resolver will
    # convert `start` to the entity's earliest_known at apply-time.
    body = _substitute_fill_date_with_start(body)
    header = (
        "# ──────────────────────────────────────────────────────────────────\n"
        "# Section: missing required attributes\n"
        "# CONFIDENCE: MEDIUM/LOW — lines with concrete suggested values are\n"
        "# MEDIUM (catalog default; verify against site). Lines with <FILL_VALUE>\n"
        "# are LOW — operator MUST replace before --apply.\n"
        "#\n"
        "# Date tokens (resolved at apply-time by `tos audit apply`):\n"
        "#   `start` = entity's earliest_known date (audit anchor)\n"
        "#   `now`   = today UTC\n"
        "# `<FILL_DATE>` placeholders below have been pre-substituted with\n"
        "# `start` — operator can edit if a different date is correct.\n"
        "# ──────────────────────────────────────────────────────────────────"
    )
    return header + "\n" + body.rstrip()


def _section_notes(report: StationTriageReport) -> str:
    return (
        "# ──────────────────────────────────────────────────────────────────\n"
        "# Notes (audit-runtime warnings)\n"
        "# ──────────────────────────────────────────────────────────────────\n#\n"
        + "\n".join(f"#   {n}" for n in report.notes)
    )


def _build_footer(report: StationTriageReport) -> str:
    return (
        "# ──────────────────────────────────────────────────────────────────\n"
        "# Verify (run after --apply lands)\n"
        "# ──────────────────────────────────────────────────────────────────\n"
        "#\n"
        f"#   tos audit missing-attributes {report.station}\n"
        f"#   tos audit attribute-dates {report.station}\n"
        f"#   tos audit verify-from-rinex --station {report.station}\n"
        f"#   tos device list --station {report.station} --all\n"
        f"#   tosGPS PrintTOS {report.station}"
    )


def default_triage_path(station: str, base_dir: Optional[Path] = None) -> Path:
    """Return the default output path for an auto-generated triage file.

    Format: ``data/triage/<STN>/<STN>_audit_<YYYYMMDD>.txt``. The ``data/``
    convention matches other ops data shipped in the repo
    (``data/attribute_codes.yaml``, ``data/station_config/``). Per-
    station subdirectory keeps long-running fleet maintenance organised.

    Parameters
    ----------
    station
        Marker / station name. Lowercased for the directory + filename.
    base_dir
        Repo root override; defaults to CWD.
    """
    if base_dir is None:
        base_dir = Path.cwd()
    stn = station.lower()
    date_stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    return base_dir / "data" / "triage" / stn / f"{stn}_audit_{date_stamp}.txt"
