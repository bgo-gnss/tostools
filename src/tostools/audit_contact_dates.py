"""Flag contact↔station relationships with a migration-artifact date.

When TOS contacts were bulk-loaded into the new system, each
contact↔station relationship row got a ``time_from`` set to the
*moment of the load*, not the date the contact actually started
owning/operating the station. Fleet probe (2026-05-31) found these
cluster on a handful of bulk-load instants, each shared identically
across dozens of relationships:

    2025-02-04T15:32:38  ×26    2025-02-05T11:19:42  ×8
    2025-09-12T09:41:14  ×4

The tell-tale signal: a **non-midnight time-of-day**. Genuine
ownership-start dates are recorded at ``T00:00:00`` (33 of 38 real-date
relationships in the probe); the migration bulk-loads all carry a real
clock time (and within a batch, the *identical* instant — 26
relationships cannot independently start at exactly 15:32:38).

So the audit rule is::

    Flag a relationship whose per_time_from has a non-midnight time
    component (the HH:MM:SS part is not 00:00:00).

This auto-catches every migration batch without a hardcoded date list,
and is robust to future bulk loads. False positives (a genuine
relationship that happens to carry a clock time) are handled by the
SUPPRESS file + the fact that every emitted ACTION is commented for
operator review.

The suggested fix backdates ``time_from`` to the station's
``earliest_known`` (the ``start`` token, resolved at apply time) —
same anchor the attribute / visit audits use. For VÍ-owned sites
that's effectively the station founding date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .api.tos_client import TOSClient
from .audit import _resolve_station_entity
from .audit_attribute_dates import (
    SuppressionParseError,
    _station_display_name,
)

# id_contact_entity_relationship is globally unique, so the suppression
# key is a 1-tuple (just the relationship id) — no date anchor needed.
ContactDateSuppressionKey = int

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONTACT_DATES_SUPPRESSIONS_PATH = (
    _REPO_ROOT / "data" / "audit_suppressions" / "contact_dates.txt"
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContactDateViolation:
    """One contact↔station relationship with a migration-artifact date.

    Carries enough context for triage emission + a readable report:
    ``id_relationship`` is the ``id_contact_entity_relationship`` the
    ACTION targets, ``per_time_from`` is the suspect timestamp,
    ``contact_label`` is the contact's name/organization, ``role`` is
    its role on the station.
    """

    id_relationship: int
    id_contact: Optional[int]
    contact_label: Optional[str]
    role: Optional[str]
    per_time_from: str


@dataclass(frozen=True)
class SuppressedContactDate:
    """A contact-date hit that was filtered by the suppression file."""

    violation: ContactDateViolation
    suppressions_path: Path
    line_no: int


@dataclass
class StationContactDatesReport:
    """Result of :func:`audit_station_contact_dates`."""

    station_id: int
    station_name: Optional[str]
    audited_relationships: int = 0
    violations: List[ContactDateViolation] = field(default_factory=list)
    suppressed: List[SuppressedContactDate] = field(default_factory=list)
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
# Suppression file (SUPPRESS <id_relationship>)
# ---------------------------------------------------------------------------


def load_contact_dates_suppressions(
    path: Optional[Path] = None,
) -> Tuple[Dict[ContactDateSuppressionKey, int], List[SuppressionParseError], Path]:
    """Parse a SUPPRESS-style file for the contact-dates audit.

    Format: one ``SUPPRESS <id_relationship>`` per known-good
    relationship (a genuine non-midnight date the operator confirms is
    correct). Comments start with ``#``, blank lines ignored.

    Returns ``(suppressions, errors, resolved_path)``. File-not-found
    is silent — the suppression file is opt-in.
    """
    if path is None:
        path = DEFAULT_CONTACT_DATES_SUPPRESSIONS_PATH

    suppressions: Dict[ContactDateSuppressionKey, int] = {}
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
                    message=f"expected line to start with 'SUPPRESS' (got {tokens[0]!r})",
                    raw=raw,
                )
            )
            continue
        if len(tokens) < 2:
            errors.append(
                SuppressionParseError(
                    line_no=i,
                    message="SUPPRESS line requires 1 argument: <id_relationship>",
                    raw=raw,
                )
            )
            continue
        try:
            id_relationship = int(tokens[1])
        except ValueError:
            errors.append(
                SuppressionParseError(
                    line_no=i,
                    message=f"id_relationship must be an integer (got {tokens[1]!r})",
                    raw=raw,
                )
            )
            continue
        suppressions[id_relationship] = i

    return suppressions, errors, path


# ---------------------------------------------------------------------------
# Migration-artifact detection
# ---------------------------------------------------------------------------


def _is_migration_artifact(per_time_from: Optional[str]) -> bool:
    """True iff the timestamp carries a non-midnight time-of-day.

    Genuine ownership-start dates are recorded at ``T00:00:00``; TOS
    migration bulk-loads carry a real clock time (and an identical
    instant per batch). A non-midnight time component is the signal.

    Empty / malformed / date-only timestamps are NOT flagged (no time
    component to be suspicious of).
    """
    if not per_time_from or "T" not in per_time_from:
        return False
    time_part = per_time_from.split("T", 1)[1][:8]
    if len(time_part) < 8:
        return False
    return time_part != "00:00:00"


# ---------------------------------------------------------------------------
# Main audit entry point
# ---------------------------------------------------------------------------


def audit_station_contact_dates(
    client: TOSClient,
    *,
    name: Optional[str] = None,
    id_entity: Optional[int] = None,
    suppressions_path: Optional[Path] = None,
    use_suppressions: bool = True,
) -> StationContactDatesReport:
    """Flag a station's contact relationships with migration-artifact dates.

    Parameters
    ----------
    client
        Unauthenticated :class:`TOSClient`. One HTTP:
        ``get_contacts(station_id)`` for the relationship list.
    name / id_entity
        Station identifier — pass one or the other.
    suppressions_path
        Override the suppression file location.
    use_suppressions
        When False, skip the SUPPRESS file entirely.

    Returns
    -------
    StationContactDatesReport

    Raises
    ------
    LookupError
        Station not found.
    ValueError
        Neither ``name`` nor ``id_entity`` set.
    """
    if use_suppressions:
        suppressions, supp_errors, supp_path = load_contact_dates_suppressions(
            suppressions_path
        )
    else:
        suppressions = {}
        supp_errors = []
        supp_path = suppressions_path or DEFAULT_CONTACT_DATES_SUPPRESSIONS_PATH

    station_history = _resolve_station_entity(client, name=name, id_entity=id_entity)
    station_id = int(station_history["id_entity"])
    station_name = _station_display_name(station_history, name)

    report = StationContactDatesReport(
        station_id=station_id,
        station_name=station_name,
        suppressions_path=supp_path,
        suppressions_errors=supp_errors,
        suppressions_disabled=not use_suppressions,
    )

    relationships = client.get_contacts(station_id) or []
    for rel in relationships:
        report.audited_relationships += 1
        per_time_from = rel.get("per_time_from")
        if not _is_migration_artifact(per_time_from):
            continue

        id_rel_raw = rel.get("id_contact_entity_relationship")
        if id_rel_raw is None:
            # Can't emit a patch without the relationship id — skip.
            continue
        try:
            id_relationship = int(id_rel_raw)
        except (TypeError, ValueError):
            continue

        violation = ContactDateViolation(
            id_relationship=id_relationship,
            id_contact=rel.get("id_contact"),
            contact_label=rel.get("name") or rel.get("organization"),
            role=rel.get("role"),
            per_time_from=str(per_time_from),
        )

        supp_line = suppressions.get(id_relationship)
        if supp_line is not None:
            report.suppressed.append(
                SuppressedContactDate(
                    violation=violation,
                    suppressions_path=supp_path,
                    line_no=supp_line,
                )
            )
        else:
            report.violations.append(violation)

    report.violations.sort(key=lambda v: v.id_relationship)
    return report


# ---------------------------------------------------------------------------
# Triage emitter
# ---------------------------------------------------------------------------


def format_triage_file(
    report: StationContactDatesReport,
    *,
    audit_command: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> str:
    """Render a contact-dates report as an operator-editable action file.

    Each violation becomes a commented ``#ACTION <station_id>
    patch-contact-relationship <id_rel> time_from start`` line. The
    ``start`` token resolves at apply time to the station's
    earliest_known (the founding date for VÍ-owned sites). Operator
    reviews, uncomments, and feeds the file into ``tos audit apply``.
    """
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    lines: List[str] = []
    station_label = report.station_name or "<unknown>"
    lines.append("# === tos audit contact-dates — triage action file ===")
    lines.append(f"# Generated:  {generated_at}")
    lines.append(f"# Station:    {station_label!r} (id_entity={report.station_id})")
    if audit_command:
        lines.append(f"# Audit cmd:  {audit_command}")
    lines.append(f"# Relationships: {report.audited_relationships} audited")
    lines.append(f"# Violations:    {len(report.violations)}")
    lines.append("#")
    lines.append("# Each flagged relationship has a non-midnight per_time_from — a")
    lines.append(
        "# TOS-migration bulk-load timestamp, not a real ownership-start date."
    )
    lines.append("#")
    lines.append(
        "#   ACTION <station_id> patch-contact-relationship <id_rel> time_from start"
    )
    lines.append("#")
    lines.append("# `start` resolves at apply time to the station's earliest_known")
    lines.append("# (founding date for VÍ-owned sites). Edit to a specific date if a")
    lines.append("# contact genuinely started later than founding.")
    lines.append("#")
    lines.append("# Workflow:")
    lines.append("#   1. Review each line. Uncomment the ones to fix.")
    lines.append("#   2. tos audit apply <file>          # dry-run preview")
    lines.append("#   3. tos audit apply <file> --apply  # commit writes")
    lines.append("#")
    lines.append("# Genuine non-midnight dates: copy the SUPPRESS hint into")
    lines.append("# data/audit_suppressions/contact_dates.txt instead.")
    lines.append("")

    if not report.violations:
        lines.append("# (no violations — nothing to triage)")
        lines.append("")
        return "\n".join(lines)

    for v in report.violations:
        label = f" {v.contact_label!r}" if v.contact_label else ""
        role = f" role={v.role}" if v.role else ""
        lines.append(
            f"# rel {v.id_relationship} — contact {v.id_contact}{label}{role}"
            f"  (per_time_from={v.per_time_from})"
        )
        lines.append(
            f"#ACTION {report.station_id} patch-contact-relationship "
            f"{v.id_relationship} time_from start"
        )
        lines.append(
            f"#   SUPPRESS {v.id_relationship}"
            "  # copy to data/audit_suppressions/contact_dates.txt"
        )
        lines.append("")

    return "\n".join(lines)
