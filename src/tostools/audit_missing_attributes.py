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
    load_catalog_scoped,
)

# (id_entity, code) — "missing" has no date anchor, so the suppression key
# is shorter than the attribute-dates audit's 3-tuple.
MissingSuppressionKey = Tuple[int, str]


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
# Helpers
# ---------------------------------------------------------------------------


def _device_display_label(history: Dict[str, Any]) -> Optional[str]:
    """Pull a human label for a device — open serial_number, then open model."""
    for code in ("serial_number", "model"):
        v = _open_attribute_value(history, code)
        if v:
            return v
    return None


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
    suggested_date_from: Optional[str] = None,
) -> None:
    """Walk one entity (station, device, or monument).

    Increments ``report.audited_entities``, appends to ``report.violations``.
    The caller passes the correct catalog scope (``catalog['stations']`` for
    a station entity, ``catalog['devices']`` for a device/monument) so
    cross-scope code collisions stay distinct.
    """
    report.audited_entities += 1
    for code, entry in _required_codes_in_scope(scope_rules, entity_subtype):
        if _open_attribute_value(history, code) is not None:
            continue
        default = entry.get("default_value")
        suggested_value = str(default) if default is not None else None
        report.violations.append(
            MissingAttributeViolation(
                id_entity=entity_id,
                subtype=entity_subtype,
                name=entity_label,
                code=code,
                scope=scope_name,
                suggested_value=suggested_value,
                suggested_date_from=suggested_date_from,
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

    Returns
    -------
    StationMissingAttributesReport
        ``has_violations`` is True when at least one required attribute is
        missing from an audited entity.

    Raises
    ------
    LookupError
        Station not found.
    ValueError
        Neither ``name`` nor ``id_entity`` set.
    FileNotFoundError
        Catalog file missing.
    """
    scoped = load_catalog_scoped(catalog_path)
    stations_rules = scoped.get("stations") or {}
    devices_rules = scoped.get("devices") or {}

    wanted = tuple(subtypes) if subtypes else GPS_DEVICE_SUBTYPES

    station_history = _resolve_station_entity(client, name=name, id_entity=id_entity)
    station_id = int(station_history["id_entity"])
    station_subtype = station_history.get("code_entity_subtype") or "geophysical"
    station_name = _station_display_name(station_history, name)

    report = StationMissingAttributesReport(
        station_id=station_id,
        station_name=station_name,
    )

    # 1. Station entity itself — iterate stations scope.
    _audit_entity(
        report=report,
        scope_rules=stations_rules,
        history=station_history,
        entity_id=station_id,
        entity_subtype=station_subtype,
        entity_label=station_name,
        scope_name="stations",
        suggested_date_from=None,
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
            suggested_date_from=suggested_date,
        )

    return report
