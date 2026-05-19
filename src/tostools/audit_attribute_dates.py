"""Detect misdated TOS attribute periods (rule 3 from the design doc).

TOS auto-stamps an attribute period's ``date_from`` with the date the value
was *entered* into TOS, not the date it became applicable to the device.
Worked example: ARHO Ashtech has ``serial_number='13831'`` with
``date_from=2014-10-17`` even though the receiver lived at the station from
2002-01-01. The new synthesis chain renders TOS faithfully, so the phantom
date propagates into PrintTOS / sitelog / GAMIT.

Rule 3 — per ``.interrogate-tos-attribute-dates-audit.md``::

    For each inherent attribute period on a device joined to the audited
    station, flag the period when its ``date_from`` is later than
    ``earliest_known``, where::

        earliest_known = min(
            earliest attribute period date_from on the device,
            earliest station-side join time_from for the device,
        )

The station-side join is the discriminator. When every attribute on a device
is stamped at the data-entry date but the station's join to that device
carries a much earlier ``time_from``, the contradiction surfaces the bug.

Layer 2 of the 4-layer DoD — detection only. Suppression file (Layer 3) and
``--triage`` emission (Layer 4) land in later commits; this module just
returns structured violations.

Module surface
--------------
:func:`audit_station_attribute_dates` — main entry point. Takes a
:class:`TOSClient` plus station id/marker, walks every child device's
attribute periods, returns a :class:`StationAttributeDateReport`.

:func:`load_catalog` — read and flatten ``data/attribute_codes.yaml``.

:func:`classification_for` — resolve a code's classification for a given
device subtype (handles the polymorphic scalar-or-per-subtype-dict shape).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

from .api.tos_client import TOSClient
from .audit import (
    GPS_DEVICE_SUBTYPES,
    _resolve_station_entity,
    canonical_subtype,
)

# ---------------------------------------------------------------------------
# Catalog location
# ---------------------------------------------------------------------------

# The repo-root data file is the canonical location. Editable installs
# (``pip install -e .``) reach it via ``src/tostools/<this>.py`` →
# parent.parent.parent. Override with the env var (e.g. for CI) or the
# ``--catalog`` CLI flag.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CATALOG_PATH = _REPO_ROOT / "data" / "attribute_codes.yaml"
CATALOG_ENV_VAR = "TOSTOOLS_ATTRIBUTE_CODES_PATH"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttributeDateViolation:
    """One attribute period whose ``date_from`` violates rule 3.

    The ``(id_entity, code, date_from)`` triple is the natural suppression
    key — Layer 3 (suppression file) will key off this triple.
    """

    id_entity: int
    subtype: str
    serial: Optional[str]
    code: str
    date_from: str
    value: Optional[str]
    earliest_known: str
    # Which anchor determined ``earliest_known``:
    # "join"  — the station-side join is earlier than every attribute date
    # "attribute" — an attribute period is earlier (or tied with the join)
    anchor_source: str


@dataclass
class StationAttributeDateReport:
    """Result of :func:`audit_station_attribute_dates`."""

    station_id: int
    station_name: Optional[str]
    audited_devices: int = 0
    devices_skipped: int = 0
    unknown_codes: List[str] = field(default_factory=list)
    violations: List[AttributeDateViolation] = field(default_factory=list)

    @property
    def has_violations(self) -> bool:
        return bool(self.violations)


# ---------------------------------------------------------------------------
# Catalog loading + classification
# ---------------------------------------------------------------------------


def load_catalog(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Load ``attribute_codes.yaml`` and flatten the three scopes into a
    single ``{code: entry}`` map.

    Path resolution: explicit ``path`` arg → env ``TOSTOOLS_ATTRIBUTE_CODES_PATH``
    → :data:`DEFAULT_CATALOG_PATH`. The devices/locations/stations scopes
    are merged with devices winning on the rare collision; each entry
    carries a ``_scope`` key so callers can disambiguate when relevant.

    Raises :class:`FileNotFoundError` if no catalog is reachable —
    audit cannot run without it.
    """
    if path is None:
        env_path = os.environ.get(CATALOG_ENV_VAR)
        path = Path(env_path) if env_path else DEFAULT_CATALOG_PATH
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    flat: Dict[str, Dict[str, Any]] = {}
    for scope in ("devices", "locations", "stations"):
        for code, entry in (data.get(scope) or {}).items():
            if code in flat:
                continue
            merged = dict(entry)
            merged["_scope"] = scope
            flat[code] = merged
    return flat


def classification_for(entry: Dict[str, Any], subtype: str) -> Optional[str]:
    """Resolve a catalog entry's classification for one device subtype.

    Returns ``"inherent"`` / ``"mutable"`` when the code applies; ``None``
    when:

    * classification is ``"TODO"`` (operator hasn't reviewed yet)
    * the code is a per-subtype dict and ``subtype`` isn't a key
    * the code is scalar but its ``applies_to`` excludes ``subtype``
      (rare — TOS data hygiene issue, but skip rather than misclassify)
    """
    cls = entry.get("classification")
    if cls is None or cls == "TODO":
        return None
    if isinstance(cls, dict):
        resolved = cls.get(subtype)
        return str(resolved) if resolved else None
    applies_to = entry.get("applies_to") or []
    if applies_to and subtype not in applies_to:
        return None
    return str(cls)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _date_only(s: str) -> str:
    """Return the ``YYYY-MM-DD`` prefix of an ISO date or datetime.

    TOS mixes ``"2002-01-01"`` and ``"2002-01-01 00:00:00"`` in the same
    payloads. Comparing the full strings lexically misorders the two —
    ``"2002-01-01" < "2002-01-01 00:00:00"`` — so we normalise to the
    date portion before comparing. Rule 3 fires on gross misdating
    (months / years), so time-of-day precision is intentionally dropped.
    """
    return s[:10]


def _earliest_attribute_date(history: Dict[str, Any]) -> Optional[str]:
    """Return the earliest ``date_from`` across every attribute period.

    None when the device has no attributes with date_from (unusual but
    possible during partial TOS imports). Already date-only normalised so
    the caller can compare against the join anchor directly.
    """
    earliest: Optional[str] = None
    for a in history.get("attributes") or []:
        df = a.get("date_from")
        if not df:
            continue
        df_d = _date_only(str(df))
        if earliest is None or df_d < earliest:
            earliest = df_d
    return earliest


def _station_joins_by_device(
    station_history: Dict[str, Any],
) -> Dict[int, List[Dict[str, Any]]]:
    """Group ``children_connections`` by ``id_entity_child``.

    Devices can have multiple joins to the same station (came back after
    a stint elsewhere); preserve all of them so the caller can pick the
    earliest ``time_from`` as the anchor.
    """
    out: Dict[int, List[Dict[str, Any]]] = {}
    for conn in station_history.get("children_connections") or []:
        cid = conn.get("id_entity_child")
        if cid is None:
            continue
        out.setdefault(int(cid), []).append(conn)
    return out


def _open_attribute_value(history: Dict[str, Any], code: str) -> Optional[str]:
    """Mirror of :func:`tostools.devices.open_attribute` (avoids the import)."""
    for a in history.get("attributes") or []:
        if a.get("code") != code:
            continue
        if a.get("date_to") is not None:
            continue
        v = a.get("value")
        if v is not None:
            return str(v)
    return None


def _station_display_name(
    station_history: Dict[str, Any], fallback: Optional[str]
) -> Optional[str]:
    """Pull a human label for the station — open ``name`` attribute, else
    fall back to whatever the operator typed."""
    name = _open_attribute_value(station_history, "name")
    return name or fallback


# ---------------------------------------------------------------------------
# Main audit entry point
# ---------------------------------------------------------------------------


def audit_station_attribute_dates(
    client: TOSClient,
    *,
    name: Optional[str] = None,
    id_entity: Optional[int] = None,
    subtypes: Optional[Sequence[str]] = None,
    include_mutable: bool = False,
    catalog_path: Optional[Path] = None,
) -> StationAttributeDateReport:
    """Apply rule 3 to every device joined to a station.

    Parameters
    ----------
    client
        Unauthenticated :class:`TOSClient`. No writes; basic_search +
        get_entity_history only.
    name / id_entity
        Station identifier — pass one or the other. ``name`` is the
        4-letter marker (RHOF, ARHO) or full display name; resolution
        delegates to :func:`tostools.audit._resolve_station_entity`,
        which prefers markers and disambiguates Jarðeðlisstöð collisions.
    subtypes
        Device subtypes to audit. Defaults to the GPS quartet
        (:data:`tostools.audit.GPS_DEVICE_SUBTYPES`). Pass canonical or
        short forms (resolved via :func:`canonical_subtype`).
    include_mutable
        When False (default), only ``inherent`` attribute codes are
        checked — firmware bumps and other mutable transitions are
        skipped. Set True to surface every mismatched date_from for
        debugging.
    catalog_path
        Override the catalog file location. Defaults to
        :data:`DEFAULT_CATALOG_PATH` or the ``TOSTOOLS_ATTRIBUTE_CODES_PATH``
        env var.

    Returns
    -------
    StationAttributeDateReport
        ``has_violations`` is True when rule 3 fired on at least one
        attribute period. Unknown attribute codes (TOS has them, catalog
        doesn't) accumulate in ``unknown_codes`` for operator follow-up
        — they don't trigger violations.

    Raises
    ------
    LookupError
        Station not found.
    ValueError
        Invalid subtype name, or neither ``name`` nor ``id_entity`` set.
    FileNotFoundError
        Catalog file missing.
    """
    catalog = load_catalog(catalog_path)

    if subtypes:
        wanted = tuple(canonical_subtype(s) for s in subtypes)
    else:
        wanted = GPS_DEVICE_SUBTYPES

    station_history = _resolve_station_entity(client, name=name, id_entity=id_entity)
    station_id = int(station_history["id_entity"])
    station_name = _station_display_name(station_history, name)

    report = StationAttributeDateReport(
        station_id=station_id,
        station_name=station_name,
    )
    unknown_codes_seen: set[str] = set()

    joins_by_device = _station_joins_by_device(station_history)

    for device_id, joins in joins_by_device.items():
        history = client.get_entity_history(device_id)
        if not history:
            continue

        dev_subtype = history.get("code_entity_subtype")
        if dev_subtype not in wanted:
            report.devices_skipped += 1
            continue
        report.audited_devices += 1

        earliest_attr = _earliest_attribute_date(history)
        join_dates = [
            _date_only(str(j["time_from"])) for j in joins if j.get("time_from")
        ]
        earliest_join = min(join_dates) if join_dates else None

        candidates = [d for d in (earliest_attr, earliest_join) if d]
        if not candidates:
            continue
        earliest_known = min(candidates)

        # Anchor source — "join" when the join is the sole/earlier signal,
        # "attribute" when an attribute date matches or precedes the join.
        if earliest_attr is None:
            anchor_source = "join"
        elif earliest_join is None or earliest_attr <= earliest_join:
            anchor_source = "attribute"
        else:
            anchor_source = "join"

        open_serial = _open_attribute_value(history, "serial_number")

        for attr in history.get("attributes") or []:
            code_raw = attr.get("code")
            if not code_raw:
                continue
            code = str(code_raw)

            entry = catalog.get(code)
            if entry is None:
                unknown_codes_seen.add(code)
                continue
            if entry.get("gps_relevance") != "yes":
                continue

            cls = classification_for(entry, str(dev_subtype))
            if cls is None:
                continue
            if cls == "mutable" and not include_mutable:
                continue

            df_raw = attr.get("date_from")
            if not df_raw:
                continue
            df = _date_only(str(df_raw))
            if df <= earliest_known:
                continue

            value_raw = attr.get("value")
            value = str(value_raw) if value_raw is not None else None

            report.violations.append(
                AttributeDateViolation(
                    id_entity=device_id,
                    subtype=str(dev_subtype),
                    serial=open_serial,
                    code=code,
                    date_from=df,
                    value=value,
                    earliest_known=earliest_known,
                    anchor_source=anchor_source,
                )
            )

    report.unknown_codes = sorted(unknown_codes_seen)
    # Stable sort by (device, code, date_from) for deterministic output.
    report.violations.sort(key=lambda v: (v.id_entity, v.code, v.date_from))
    return report
