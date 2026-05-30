"""Detect equipment-change events with no corresponding vitjun (Phase D).

The lifecycle-tracker work (Phase C, ``add-visit`` ACTION verb) gave
operators a way to leave audit-trail vitjanir on physical interventions
(firmware bumps, sent-for-repair, back-from-repair). This audit closes
the loop: walk a station's join history, find equipment-change events
that have no vitjun within ±N days, and surface them as triage-emittable
violations.

Rule
----

For each child-device join-open event at the station within the
``--since`` window (default last 2 years):

* **skip** the cleanup-artifact pattern (``time_from = 2014-10-17``,
  the fleet-wide bulk-load date — these aren't real install dates;
  see memory ``project_2014_10_17_metadata_cleanup_artifacts``)
* **skip** events filtered by the SUPPRESS file
* **flag** as a violation when no vitjun on the station has
  ``start_time`` within ±``coverage_window_days`` of the event date

Scope intentionally narrow for v1:

* **Opens only** — not close events, not attribute writes (firmware
  bumps, status flips). Widen with future ``--include-closes`` /
  ``--include-attribute-writes`` if operator demand surfaces.
* **Station-attached vitjanir only** — empirically zero GPS-device
  vitjanir exist today (2026-05-30 fleet probe). Adding the
  device-side check now would be dead weight + extra HTTP per join.
  Future ``--include-device-visits`` flag for forward-compat.
* **Last 2 years by default** — pre-vitjun-era stations have huge
  gaps; auditing all history would overwhelm the SUPPRESS workflow.

Per-violation triage shape::

    #ACTION <device_id> add-visit change <event_date> \\
    "<FILL_WORK>"  # device deployment at station — operator confirms reason

The reason defaults to ``change`` since a new join-open is a change in
the deployment topology. Operator edits before applying.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .api.tos_client import TOSClient
from .audit import _resolve_station_entity
from .audit_attribute_dates import (
    SuppressionParseError,
    _date_only,
    _open_attribute_value,
    _station_display_name,
    _station_joins_by_device,
)

# Fleet-wide cleanup-artifact date — every join opened on this day is
# a TOS bulk-load artifact, not a real install. Same constant as the
# attribute-dates / device-list filters; duplicated here to keep the
# import surface clean (the audit modules don't depend on tos.py).
_CLEANUP_ARTIFACT_DATE = "2014-10-17"

# Default ``--since`` window: last 2 years from "now". Operator extends
# via CLI. The hard-coded "2 years" matches the advisor sequencing
# recommendation — pre-vitjun-era stations need a SUPPRESS sweep
# before they're useful to audit, and 2y keeps the first run scoped.
_DEFAULT_SINCE_YEARS = 2

# Default coverage window: ±7 days around each event. Wide enough to
# catch "vitjun written next business day" but narrow enough to
# distinguish per-event coverage from coincidental overlap.
_DEFAULT_COVERAGE_WINDOW_DAYS = 7

# (device_id, event_date) — events anchored on the device that
# changed deployment. Same 2-tuple key shape as missing_attributes.
CoverageSuppressionKey = Tuple[int, str]

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_COVERAGE_SUPPRESSIONS_PATH = (
    _REPO_ROOT / "data" / "audit_suppressions" / "visit_coverage.txt"
)

# Triage placeholder for the work text — operators ALWAYS need to
# supply this; the audit can't infer "what happened" from a join row.
FILL_WORK_PLACEHOLDER = "<FILL_WORK>"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VisitCoverageViolation:
    """One join-open event with no vitjun within the coverage window.

    Carries enough context for triage emission:
    ``device_id`` is the entity that joined the station,
    ``device_label`` is its open serial/model (best effort) so the
    triage file reads naturally,
    ``event_date`` is the ``time_from`` of the join (YYYY-MM-DD),
    ``device_subtype`` lets the renderer group output by device type.
    """

    device_id: int
    device_subtype: str
    device_label: Optional[str]
    event_date: str
    # The window we checked — useful for the report header / debug.
    coverage_window_days: int


@dataclass(frozen=True)
class SuppressedCoverage:
    """A coverage hit that was filtered by the suppression file."""

    violation: VisitCoverageViolation
    suppressions_path: Path
    line_no: int


@dataclass
class StationVisitCoverageReport:
    """Result of :func:`audit_station_visit_coverage`."""

    station_id: int
    station_name: Optional[str]
    since: str  # YYYY-MM-DD — earliest event date considered
    coverage_window_days: int
    audited_events: int = 0
    violations: List[VisitCoverageViolation] = field(default_factory=list)
    suppressed: List[SuppressedCoverage] = field(default_factory=list)
    suppressions_path: Optional[Path] = None
    suppressions_errors: List[SuppressionParseError] = field(default_factory=list)
    suppressions_disabled: bool = False

    @property
    def has_violations(self) -> bool:
        return bool(self.violations)

    @property
    def suppressed_count(self) -> int:
        return len(self.suppressed)


# ---------------------------------------------------------------------------
# Suppression file (SUPPRESS <device_id> <event_date>)
# ---------------------------------------------------------------------------


def load_coverage_suppressions(
    path: Optional[Path] = None,
) -> Tuple[Dict[CoverageSuppressionKey, int], List[SuppressionParseError], Path]:
    """Parse a SUPPRESS-style file for the visit-coverage audit.

    Format: one ``SUPPRESS <device_id> <event_date>`` per known-good
    gap. Comments start with ``#``, blank lines ignored.

    Returns ``(suppressions, errors, resolved_path)``. File-not-found
    is silent — the suppression file is opt-in.
    """
    if path is None:
        path = DEFAULT_COVERAGE_SUPPRESSIONS_PATH

    suppressions: Dict[CoverageSuppressionKey, int] = {}
    errors: List[SuppressionParseError] = []

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return suppressions, errors, path

    for i, line in enumerate(text.splitlines(), 1):
        raw = line
        if "#" in line:
            line = line.split("#", 1)[0]
        line = line.strip()
        if not line:
            continue

        tokens = line.split()
        if tokens[0] != "SUPPRESS":
            errors.append(
                SuppressionParseError(
                    line_no=i,
                    message=(
                        f"expected line to start with 'SUPPRESS' "
                        f"(got {tokens[0]!r})"
                    ),
                    raw=raw,
                )
            )
            continue
        if len(tokens) < 3:
            errors.append(
                SuppressionParseError(
                    line_no=i,
                    message=(
                        "SUPPRESS line requires 2 arguments: "
                        f"<device_id> <event_date> (got {len(tokens) - 1})"
                    ),
                    raw=raw,
                )
            )
            continue
        try:
            device_id = int(tokens[1])
        except ValueError:
            errors.append(
                SuppressionParseError(
                    line_no=i,
                    message=f"device_id must be an integer (got {tokens[1]!r})",
                    raw=raw,
                )
            )
            continue
        event_date = _date_only(tokens[2])
        suppressions[(device_id, event_date)] = i

    return suppressions, errors, path


# ---------------------------------------------------------------------------
# Coverage lookup helper
# ---------------------------------------------------------------------------


def _vitjun_dates(client: TOSClient, station_id: int) -> List[str]:
    """Pull every vitjun on the station and return their YYYY-MM-DD
    start dates.

    Skips rows with no start_time (defensive — should never happen
    against live TOS).
    """
    rows = client.list_maintenance_visits(station_id) or []
    out: List[str] = []
    for r in rows:
        start = r.get("start_time")
        if not start:
            continue
        out.append(_date_only(str(start)))
    return out


def _has_coverage(
    event_date: str,
    vitjun_dates: List[str],
    window_days: int,
) -> bool:
    """True iff any vitjun start_time is within ±window_days of event_date.

    Both dates are YYYY-MM-DD strings — parsed once each, simple
    timedelta comparison.
    """
    try:
        event_dt = datetime.strptime(event_date, "%Y-%m-%d")
    except ValueError:
        # Malformed event date — treat as uncovered (the operator's
        # data is broken; surface the violation).
        return False
    window = timedelta(days=window_days)
    for vd in vitjun_dates:
        try:
            v_dt = datetime.strptime(vd, "%Y-%m-%d")
        except ValueError:
            continue
        if abs(v_dt - event_dt) <= window:
            return True
    return False


# ---------------------------------------------------------------------------
# Main audit entry point
# ---------------------------------------------------------------------------


def audit_station_visit_coverage(
    client: TOSClient,
    *,
    name: Optional[str] = None,
    id_entity: Optional[int] = None,
    since: Optional[str] = None,
    coverage_window_days: int = _DEFAULT_COVERAGE_WINDOW_DAYS,
    suppressions_path: Optional[Path] = None,
    use_suppressions: bool = True,
) -> StationVisitCoverageReport:
    """Walk a station's join-open events and flag uncovered ones.

    Parameters
    ----------
    client
        Unauthenticated :class:`TOSClient`. Two HTTPs:
        ``get_entity_history(station_id)`` for the join history +
        ``list_maintenance_visits(station_id)`` for the coverage set.
    name / id_entity
        Station identifier — pass one or the other.
    since
        YYYY-MM-DD floor. Events with ``time_from < since`` are
        skipped. Defaults to today minus 2 years.
    coverage_window_days
        Half-width of the coverage window. ``±7`` by default.
    suppressions_path
        Override the suppression file location.
    use_suppressions
        When False, skip the SUPPRESS file entirely.

    Returns
    -------
    StationVisitCoverageReport

    Raises
    ------
    LookupError
        Station not found.
    ValueError
        Neither ``name`` nor ``id_entity`` set.
    """
    if since is None:
        since_dt = datetime.now(timezone.utc) - timedelta(
            days=365 * _DEFAULT_SINCE_YEARS
        )
        since = since_dt.strftime("%Y-%m-%d")
    since_floor = _date_only(since)

    if use_suppressions:
        suppressions, supp_errors, supp_path = load_coverage_suppressions(
            suppressions_path
        )
    else:
        suppressions = {}
        supp_errors = []
        supp_path = suppressions_path or DEFAULT_COVERAGE_SUPPRESSIONS_PATH

    station_history = _resolve_station_entity(client, name=name, id_entity=id_entity)
    station_id = int(station_history["id_entity"])
    station_name = _station_display_name(station_history, name)

    report = StationVisitCoverageReport(
        station_id=station_id,
        station_name=station_name,
        since=since_floor,
        coverage_window_days=coverage_window_days,
        suppressions_path=supp_path,
        suppressions_errors=supp_errors,
        suppressions_disabled=not use_suppressions,
    )

    # Pull vitjun dates once — same set for every event.
    vitjun_dates = _vitjun_dates(client, station_id)

    joins_by_device = _station_joins_by_device(station_history)
    for device_id, joins in joins_by_device.items():
        for join in joins:
            tf_raw = join.get("time_from")
            if not tf_raw:
                continue
            event_date = _date_only(str(tf_raw))
            # Skip cleanup-artifact bulk-load events.
            if event_date == _CLEANUP_ARTIFACT_DATE:
                continue
            # Skip events before --since cutoff.
            if event_date < since_floor:
                continue

            report.audited_events += 1

            if _has_coverage(event_date, vitjun_dates, coverage_window_days):
                continue

            # Resolve device label (best effort — one extra HTTP per
            # device per audit; cached on the client side if reused).
            device_history = client.get_entity_history(device_id) or {}
            device_subtype = device_history.get("code_entity_subtype") or "unknown"
            serial = _open_attribute_value(device_history, "serial_number")
            model = _open_attribute_value(device_history, "model")
            label_parts = [p for p in (serial, model) if p]
            device_label = " ".join(label_parts) if label_parts else None

            violation = VisitCoverageViolation(
                device_id=device_id,
                device_subtype=device_subtype,
                device_label=device_label,
                event_date=event_date,
                coverage_window_days=coverage_window_days,
            )

            supp_line = suppressions.get((device_id, event_date))
            if supp_line is not None:
                report.suppressed.append(
                    SuppressedCoverage(
                        violation=violation,
                        suppressions_path=supp_path,
                        line_no=supp_line,
                    )
                )
            else:
                report.violations.append(violation)

    # Stable ordering: oldest event first within a device, devices by
    # id ascending. Helps both rendering and triage-file readability.
    report.violations.sort(key=lambda v: (v.device_id, v.event_date))
    return report


# ---------------------------------------------------------------------------
# Triage emitter
# ---------------------------------------------------------------------------


def format_triage_file(
    report: StationVisitCoverageReport,
    *,
    audit_command: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> str:
    """Render a visit-coverage report as an operator-editable action file.

    Each violation becomes a commented ``#ACTION <device_id> add-visit
    change <event_date> "<FILL_WORK>"`` line. Operator replaces
    ``<FILL_WORK>`` with what actually happened, uncomments, and feeds
    the file into ``tos audit apply``.

    The default reason is ``change`` — a new join-open is a change in
    deployment topology. Operator edits if a different reason fits
    (``repairs`` for swap-after-failure, etc.).
    """
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    lines: List[str] = []
    station_label = report.station_name or "<unknown>"
    lines.append("# === tos audit visit-coverage — triage action file ===")
    lines.append(f"# Generated:  {generated_at}")
    lines.append(f"# Station:    {station_label!r} (id_entity={report.station_id})")
    if audit_command:
        lines.append(f"# Audit cmd:  {audit_command}")
    lines.append(f"# Window:     ±{report.coverage_window_days} days around each event")
    lines.append(f"# Since:      {report.since}")
    lines.append(f"# Events:     {report.audited_events} audited")
    lines.append(f"# Violations: {len(report.violations)}")
    lines.append("#")
    lines.append("# Format: one ACTION per line, '#' for comments.")
    lines.append("#")
    lines.append(
        "#   ACTION <device_id> add-visit <reason_csv> <date> "
        '"<work_text>" [open|closed]'
    )
    lines.append("#")
    lines.append("# Workflow:")
    lines.append(
        f"#   1. Replace each {FILL_WORK_PLACEHOLDER} with what actually happened."
    )
    lines.append("#      Adjust the reason if 'change' is wrong (e.g. 'repairs').")
    lines.append("#   2. Uncomment the ACTION line(s) you want to fire.")
    lines.append("#   3. tos audit apply <file>          # dry-run preview")
    lines.append("#   4. tos audit apply <file> --apply  # commit writes")
    lines.append("#")
    lines.append("# Alternative for known-good gaps: copy the SUPPRESS hint into")
    lines.append("# data/audit_suppressions/visit_coverage.txt instead.")
    lines.append("")

    if not report.violations:
        lines.append("# (no violations — nothing to triage)")
        lines.append("")
        return "\n".join(lines)

    # Group by device for readability — most stations have a handful
    # of devices, each with a few events.
    by_device: Dict[int, List[VisitCoverageViolation]] = {}
    for v in report.violations:
        by_device.setdefault(v.device_id, []).append(v)

    for device_id in sorted(by_device):
        vs = by_device[device_id]
        first = vs[0]
        label_part = f" {first.device_label!r}" if first.device_label else ""
        lines.append(
            f"# --- {first.device_subtype} id_entity={device_id}{label_part} ---"
        )
        for v in vs:
            work_token = shlex.quote(FILL_WORK_PLACEHOLDER)
            lines.append(
                f"#ACTION {device_id} add-visit change {v.event_date} " f"{work_token}"
            )
            lines.append(
                f"#   SUPPRESS {device_id} {v.event_date}"
                "  # copy to data/audit_suppressions/visit_coverage.txt"
            )
        lines.append("")

    return "\n".join(lines)
