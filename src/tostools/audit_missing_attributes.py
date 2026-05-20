"""Detect missing required TOS attributes (Layer 6 of the audit work).

The Layers 2-5 audit (``audit_attribute_dates``) checks the dates of attributes
that *exist*. This module checks the *presence* of attributes that *should*
exist — surfacing REYK-style gaps where the station entity is missing its
``date_start``, or where a device is missing a required ``serial_number``.

Rule — per ``.interrogate-tos-audit-missing-attributes.md``::

    For each entity in scope (the station + its open child devices), iterate
    the catalog rules for that entity's scope. Flag every code where
    ``entity.code_entity_subtype ∈ entry['gps_required_for']`` AND the entity
    has no open attribute period for that code.

The walker fans out from one station:

* The station entity itself is audited against ``catalog['stations']`` —
  station subtype is always ``geophysical`` for real GPS sites.
* Each open child device (``time_to`` is None) is audited against
  ``catalog['devices']`` — restricted to the GPS quartet
  (gnss_receiver, antenna, radome, monument). Monument-specific catalog
  entries are reached naturally via ``applies_to: [monument]``.

Iterating *only* the entity's natural catalog scope prevents the cross-scope
collision pattern (``subtype``, ``date_start``, ``lat``, …) from shadowing
station-level rules behind device-level ones. See
:func:`tostools.audit_attribute_dates.load_catalog_scoped`.

Suppression-file integration and CLI wiring land in the next step (Layer 6
task #10); this module exposes the walker + data model only.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .api.tos_client import TOSClient
from .audit import GPS_DEVICE_SUBTYPES, _resolve_station_entity
from .audit_attribute_dates import (
    SuppressionParseError,
    _date_only,
    _open_attribute_value,
    _station_display_name,
    _station_joins_by_device,
    classification_for,
    load_catalog_scoped,
)

# (id_entity, code) — "missing" has no date anchor, so the suppression key
# is shorter than the attribute-dates audit's 3-tuple.
MissingSuppressionKey = Tuple[int, str]

# Layer 3 — committed-in-repo suppression file for missing-attributes.
# Format: one ``SUPPRESS <id_entity> <code>`` per known-good gap.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MISSING_SUPPRESSIONS_PATH = (
    _REPO_ROOT / "data" / "audit_suppressions" / "missing_attributes.txt"
)

# Placeholders rendered in triage output for codes without catalog defaults
# or when a sensible date_from anchor isn't available.
FILL_VALUE_PLACEHOLDER = "<FILL_VALUE>"
FILL_DATE_PLACEHOLDER = "<FILL_DATE>"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MissingAttributeViolation:
    """One catalog code an entity is required to have but doesn't.

    Carries enough context for triage emission:
    ``suggested_value`` pre-fills the ACTION line when the catalog has a
    ``default_value`` (e.g. ``subtype`` → ``"GPS stöð"``); ``suggested_date_from``
    pre-fills the date when the entity is a device (we use its earliest open
    join's ``time_from``). Stations don't carry a sensible date hint — the
    operator picks the value when they uncomment the line.
    """

    id_entity: int
    subtype: str
    name: Optional[str]
    code: str
    scope: str  # "stations" or "devices" — which catalog scope the rule came from
    suggested_value: Optional[str]
    suggested_date_from: Optional[str]


@dataclass(frozen=True)
class SuppressedMissing:
    """A missing-attributes hit that was filtered by the suppression file."""

    violation: MissingAttributeViolation
    suppressions_path: Path
    line_no: int


@dataclass
class StationMissingAttributesReport:
    """Result of :func:`audit_station_missing_attributes`."""

    station_id: int
    station_name: Optional[str]
    audited_entities: int = 0
    devices_skipped: int = 0
    violations: List[MissingAttributeViolation] = field(default_factory=list)
    suppressed: List[SuppressedMissing] = field(default_factory=list)
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
# Suppression file parsing (Layer 3 — 2-tuple key)
# ---------------------------------------------------------------------------


def load_missing_suppressions(
    path: Optional[Path] = None,
) -> Tuple[Dict[MissingSuppressionKey, int], List[SuppressionParseError], Path]:
    """Parse a SUPPRESS-style file for the missing-attributes audit.

    Format: one ``SUPPRESS <id_entity> <code>`` per line. Comments start
    with ``#`` and run to end-of-line; blank lines are ignored. The key
    is a 2-tuple — there's no ``date_from`` anchor since "missing" has
    no date. Mirrors :func:`tostools.audit_attribute_dates.load_suppressions`
    in spirit; the shorter shape is the only material difference.

    Returns ``(suppressions, errors, resolved_path)``:

    * ``suppressions`` — ``{(id_entity, code): line_no}`` mapping. Line
      numbers are kept so verbose output can show which file line
      silenced each entry.
    * ``errors`` — collected malformed lines; the caller decides whether
      to abort or continue with the parsed entries.
    * ``resolved_path`` — the path actually tried.

    File-not-found is NOT an error. Returns an empty mapping. The
    suppression file is opt-in.
    """
    if path is None:
        path = DEFAULT_MISSING_SUPPRESSIONS_PATH

    suppressions: Dict[MissingSuppressionKey, int] = {}
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
                        f"<id_entity> <code> (got {len(tokens) - 1})"
                    ),
                    raw=raw,
                )
            )
            continue
        try:
            id_entity = int(tokens[1])
        except ValueError:
            errors.append(
                SuppressionParseError(
                    line_no=i,
                    message=f"id_entity must be int, got {tokens[1]!r}",
                    raw=raw,
                )
            )
            continue
        code = tokens[2]
        suppressions[(id_entity, code)] = i

    return suppressions, errors, path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _device_display_label(history: Dict[str, Any]) -> Optional[str]:
    """Pull a human label for a device — open serial_number, then open model."""
    for code in ("serial_number", "model"):
        v = _open_attribute_value(history, code)
        if v:
            return v
    return None


def _earliest_open_attribute_date(history: Dict[str, Any]) -> Optional[str]:
    """Min ``date_from`` across all OPEN attribute periods on an entity.

    Used as the tier-2 cascade signal for ``inherent`` attribute date
    hints when the entity has no explicit ``date_start`` of its own.
    The operator's observation: inherent attrs like ``marker``, ``name``,
    ``lat``, ``lon``, ``subtype`` are set together at TOS creation time,
    so their min ``date_from`` is a reliable proxy for "when did this
    entity start existing in TOS".

    Closed periods (``date_to`` set) are excluded — a historical value
    from 1995 that was later closed shouldn't anchor the date for a
    new attribute period in 2018.
    """
    earliest: Optional[str] = None
    for a in history.get("attributes") or []:
        if a.get("date_to") is not None:
            continue
        df = a.get("date_from")
        if not df:
            continue
        df_d = _date_only(str(df))
        if earliest is None or df_d < earliest:
            earliest = df_d
    return earliest


def _resolve_inherent_date_hint(
    history: Dict[str, Any],
    fallback: Optional[str],
) -> Optional[str]:
    """Cascade-resolve a date hint for an ``inherent`` attribute violation.

    Order:
    1. The entity's open ``date_start`` attribute value (if any).
    2. The earliest open attribute ``date_from`` on the entity (proxy
       for "when did this entity show up in TOS").
    3. The caller-supplied ``fallback`` — for devices this is the
       earliest open-join ``time_from``, which the dispatcher can't
       see but the walker can.
    4. ``None`` — triage will emit ``<FILL_DATE>``.

    Returns date-only ``YYYY-MM-DD`` or None.
    """
    ds = _open_attribute_value(history, "date_start")
    if ds:
        ds_d = _date_only(str(ds))
        if len(ds_d) == 10:
            return ds_d
    earliest = _earliest_open_attribute_date(history)
    if earliest:
        return earliest
    return fallback


def _required_codes_in_scope(
    scope_rules: Dict[str, Dict[str, Any]],
    entity_subtype: str,
) -> List[Tuple[str, Dict[str, Any]]]:
    """Return ``[(code, entry)]`` for rules where ``entity_subtype`` is
    required AND ``gps_relevance == 'yes'``.

    Filtering on ``gps_relevance`` keeps "no" entries (clearly seismic /
    meteorological) and "maybe" entries (operator review still pending)
    out of the audit until they're explicitly classified.
    """
    out: List[Tuple[str, Dict[str, Any]]] = []
    for code, entry in scope_rules.items():
        if entry.get("gps_relevance") != "yes":
            continue
        required = entry.get("gps_required_for") or []
        if entity_subtype not in required:
            continue
        out.append((code, entry))
    return out


def _audit_entity(
    *,
    report: StationMissingAttributesReport,
    scope_rules: Dict[str, Dict[str, Any]],
    history: Dict[str, Any],
    entity_id: int,
    entity_subtype: str,
    entity_label: Optional[str],
    scope_name: str,
    device_join_date_hint: Optional[str] = None,
) -> None:
    """Walk one entity (station, device, or monument).

    Increments ``report.audited_entities``, appends to ``report.violations``.
    The caller passes the correct catalog scope (``catalog['stations']`` for
    a station entity, ``catalog['devices']`` for a device/monument) so
    cross-scope code collisions stay distinct.

    ``device_join_date_hint`` — the earliest open-join ``time_from`` for
    this entity from the station's perspective; passed as the tier-3
    fallback of the inherent date cascade (devices only — stations have
    no parent join, so callers pass ``None``).

    Per-violation date hint resolution:

    * ``classification == 'inherent'`` (per the polymorphic catalog
      shape — handled by :func:`classification_for`): cascade through
      :func:`_resolve_inherent_date_hint` — entity's open ``date_start``,
      then the entity's earliest open attribute ``date_from``, then
      ``device_join_date_hint``, then ``None``.
    * ``classification == 'mutable'`` / ``'TODO'`` / unclassified:
      ``suggested_date_from`` is ``None``. The operator must consciously
      pick a date — no silent default for transitions.
    """
    report.audited_entities += 1
    for code, entry in _required_codes_in_scope(scope_rules, entity_subtype):
        if _open_attribute_value(history, code) is not None:
            continue
        default = entry.get("default_value")
        suggested_value = str(default) if default is not None else None
        # Date hint depends on classification — only inherent codes get
        # an auto-filled date; mutable codes leave <FILL_DATE> for the
        # operator to fill in consciously.
        classification = classification_for(entry, entity_subtype)
        if classification == "inherent":
            suggested_date = _resolve_inherent_date_hint(
                history, fallback=device_join_date_hint
            )
        else:
            suggested_date = None
        report.violations.append(
            MissingAttributeViolation(
                id_entity=entity_id,
                subtype=entity_subtype,
                name=entity_label,
                code=code,
                scope=scope_name,
                suggested_value=suggested_value,
                suggested_date_from=suggested_date,
            )
        )


# ---------------------------------------------------------------------------
# Main audit entry point
# ---------------------------------------------------------------------------


def audit_station_missing_attributes(
    client: TOSClient,
    *,
    name: Optional[str] = None,
    id_entity: Optional[int] = None,
    subtypes: Optional[Sequence[str]] = None,
    catalog_path: Optional[Path] = None,
    suppressions_path: Optional[Path] = None,
    use_suppressions: bool = True,
) -> StationMissingAttributesReport:
    """Walk a station + its open child devices and flag missing required
    attributes.

    Parameters
    ----------
    client
        Unauthenticated :class:`TOSClient`. No writes; ``basic_search`` +
        ``get_entity_history`` only.
    name / id_entity
        Station identifier — pass one or the other. Resolution delegates
        to :func:`tostools.audit._resolve_station_entity`, which prefers
        markers and disambiguates ``geophysical`` station collisions.
    subtypes
        Device subtypes to audit. Defaults to
        :data:`tostools.audit.GPS_DEVICE_SUBTYPES`
        (gnss_receiver, antenna, radome, monument).
    catalog_path
        Override the catalog file location. Defaults to the canonical
        repo path / ``TOSTOOLS_ATTRIBUTE_CODES_PATH`` env var.
    suppressions_path
        Override the suppression file location. Defaults to
        :data:`DEFAULT_MISSING_SUPPRESSIONS_PATH`. File-not-found is silent.
    use_suppressions
        When False, skip the suppression file entirely — every missing
        hit lands in ``violations``. Equivalent to ``--no-suppressions``.

    Returns
    -------
    StationMissingAttributesReport
        ``has_violations`` reflects the **filtered** violations list —
        suppressed entries are preserved on ``report.suppressed`` so
        verbose output can show what was silenced.

    Raises
    ------
    LookupError
        Station not found.
    ValueError
        Neither ``name`` nor ``id_entity`` set.
    FileNotFoundError
        Catalog file missing (suppression file missing is not an error).
    """
    scoped = load_catalog_scoped(catalog_path)
    stations_rules = scoped.get("stations") or {}
    devices_rules = scoped.get("devices") or {}

    wanted = tuple(subtypes) if subtypes else GPS_DEVICE_SUBTYPES

    if use_suppressions:
        suppressions, supp_errors, supp_path = load_missing_suppressions(
            suppressions_path
        )
    else:
        suppressions = {}
        supp_errors = []
        supp_path = suppressions_path or DEFAULT_MISSING_SUPPRESSIONS_PATH

    station_history = _resolve_station_entity(client, name=name, id_entity=id_entity)
    station_id = int(station_history["id_entity"])
    station_subtype = station_history.get("code_entity_subtype") or "geophysical"
    station_name = _station_display_name(station_history, name)

    report = StationMissingAttributesReport(
        station_id=station_id,
        station_name=station_name,
        suppressions_path=supp_path,
        suppressions_errors=supp_errors,
        suppressions_disabled=not use_suppressions,
    )

    # 1. Station entity itself — iterate stations scope.
    #    Stations have no parent join, so no device_join_date_hint.
    _audit_entity(
        report=report,
        scope_rules=stations_rules,
        history=station_history,
        entity_id=station_id,
        entity_subtype=station_subtype,
        entity_label=station_name,
        scope_name="stations",
        device_join_date_hint=None,
    )

    # 2. Each open child device — iterate devices scope. Closed joins
    #    (time_to set) are skipped: a removed device's missing attributes
    #    aren't a current operational gap.
    joins_by_device = _station_joins_by_device(station_history)
    for device_id, joins in joins_by_device.items():
        open_joins = [j for j in joins if j.get("time_to") is None]
        if not open_joins:
            continue

        history = client.get_entity_history(device_id)
        if not history:
            continue

        dev_subtype = history.get("code_entity_subtype") or ""
        if dev_subtype not in wanted:
            report.devices_skipped += 1
            continue

        device_label = _device_display_label(history)
        join_dates = [
            _date_only(str(j["time_from"]))
            for j in open_joins
            if j.get("time_from")
        ]
        suggested_date = min(join_dates) if join_dates else None

        _audit_entity(
            report=report,
            scope_rules=devices_rules,
            history=history,
            entity_id=device_id,
            entity_subtype=dev_subtype,
            entity_label=device_label,
            scope_name="devices",
            device_join_date_hint=suggested_date,
        )

    # Apply suppressions — partition violations into kept vs suppressed.
    if suppressions:
        kept: List[MissingAttributeViolation] = []
        for v in report.violations:
            key = (v.id_entity, v.code)
            line_no = suppressions.get(key)
            if line_no is not None:
                report.suppressed.append(
                    SuppressedMissing(
                        violation=v,
                        suppressions_path=supp_path,
                        line_no=line_no,
                    )
                )
            else:
                kept.append(v)
        report.violations = kept

    return report


# ---------------------------------------------------------------------------
# Triage file emission (Layer 4)
# ---------------------------------------------------------------------------


def _quote_value(value: Optional[str]) -> str:
    """Render a value for the ACTION line — shlex-quote when needed.

    ``None`` becomes the ``<FILL_VALUE>`` placeholder so the operator
    has to fill it in before applying. Values with spaces or shell
    metacharacters get single-quoted (e.g. ``GPS stöð`` → ``'GPS stöð'``),
    matching the shlex-split parsing the apply verb (task #11) will use.
    """
    if value is None:
        return FILL_VALUE_PLACEHOLDER
    return shlex.quote(value)


def format_triage_file(
    report: StationMissingAttributesReport,
    *,
    audit_command: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> str:
    """Render a missing-attributes report as an operator-editable
    action file.

    Each violation becomes a commented ``#ACTION <id> add-attribute
    <code> <value> <date_from>`` line. The operator reviews, fills in
    ``<FILL_VALUE>`` / ``<FILL_DATE>`` placeholders where present,
    uncomments the lines they want to apply, then feeds the file into
    ``tos audit apply`` (which dispatches to the ``add-attribute`` verb
    once Layer 6 task #11 lands).

    Parameters
    ----------
    report
        The audit report. Only ``report.violations`` is consulted —
        suppressed entries are intentionally NOT emitted.
    audit_command
        Optional command-line string captured at audit time; rendered
        in the header so the file is self-documenting.
    generated_at
        Optional ISO timestamp. Defaults to ``datetime.utcnow()`` at
        call time. Pass an explicit value in tests to keep output
        byte-deterministic.

    Returns
    -------
    str
        Newline-terminated file contents, safe to write directly with
        :meth:`pathlib.Path.write_text`.

    Notes
    -----
    Violations are grouped by entity (station first, then devices) so
    the operator can read the file linearly and decide per-entity what
    to fill in.
    """
    from datetime import datetime, timezone

    if generated_at is None:
        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    lines: List[str] = []
    station_label = report.station_name or "<unknown>"
    lines.append("# === tos audit missing-attributes — triage action file ===")
    lines.append(f"# Generated:  {generated_at}")
    lines.append(f"# Station:    {station_label!r} (id_entity={report.station_id})")
    if audit_command:
        lines.append(f"# Audit cmd:  {audit_command}")
    lines.append(f"# Violations: {len(report.violations)}")
    lines.append("#")
    lines.append("# Format: one ACTION per line, '#' for comments.")
    lines.append("#")
    lines.append("#   ACTION <id_entity> add-attribute \\")
    lines.append("#          <code> <value> <date_from>")
    lines.append("#")
    lines.append("# Workflow:")
    lines.append("#   1. Review each block below. Replace any")
    lines.append(
        f"#      {FILL_VALUE_PLACEHOLDER} / {FILL_DATE_PLACEHOLDER}"
        " placeholders with the value/date you"
    )
    lines.append("#      know is correct for the entity.")
    lines.append("#   2. Uncomment the ACTION line(s) you want to fire.")
    lines.append("#   3. tos audit apply <file>          # dry-run preview")
    lines.append("#   4. tos audit apply <file> --apply  # commit writes")
    lines.append("#")
    lines.append("# <date_from> accepts two shortcuts in addition to YYYY-MM-DD:")
    lines.append("#   now    — today's UTC date (use for mutable transitions)")
    lines.append("#   start  — entity's earliest open attribute date_from")
    lines.append("#            (use for inherent backfills you forgot to record)")
    lines.append("#")
    lines.append("# Alternative for known-good gaps: copy the SUPPRESS hint into")
    lines.append("# data/audit_suppressions/missing_attributes.txt instead.")
    lines.append("")

    if not report.violations:
        lines.append("# (no violations — nothing to triage)")
        lines.append("")
        return "\n".join(lines)

    # Group by entity for readability. Station entity always comes first
    # if present; devices follow in id_entity order.
    station_vios: List[MissingAttributeViolation] = []
    by_device: Dict[int, List[MissingAttributeViolation]] = {}
    entity_meta: Dict[int, Tuple[str, Optional[str]]] = {}
    for v in report.violations:
        if v.id_entity == report.station_id:
            station_vios.append(v)
        else:
            by_device.setdefault(v.id_entity, []).append(v)
        entity_meta[v.id_entity] = (v.subtype, v.name)

    def _emit_entity_block(
        entity_id: int, entity_vios: List[MissingAttributeViolation]
    ) -> None:
        subtype, label = entity_meta[entity_id]
        label_part = f" {label!r}" if label else ""
        lines.append(f"# --- {subtype} id_entity={entity_id}{label_part} ---")
        for v in entity_vios:
            value_token = _quote_value(v.suggested_value)
            date_token = v.suggested_date_from or FILL_DATE_PLACEHOLDER
            suggested_note = ""
            if v.suggested_value is not None:
                suggested_note = f"  (default: {v.suggested_value!r})"
            elif v.suggested_date_from is not None:
                suggested_note = f"  (date hint: {v.suggested_date_from})"
            lines.append(f"# missing: {v.code}{suggested_note}")
            lines.append(
                f"#ACTION {v.id_entity} add-attribute "
                f"{v.code} {value_token} {date_token}"
            )
            lines.append(f"# (or suppress: SUPPRESS {v.id_entity} {v.code})")
            lines.append("")

    if station_vios:
        _emit_entity_block(report.station_id, station_vios)

    for did in sorted(by_device):
        _emit_entity_block(did, by_device[did])

    return "\n".join(lines)
