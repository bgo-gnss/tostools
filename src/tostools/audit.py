"""Invariant audit for TOS device entities and stations.

Library functions :func:`audit_device` and :func:`audit_station` verify the
invariants defined in the device-warehouse design doc:

* **I1** — every device has exactly one open join at any instant.
* **I2** — every station has at most one open join per device subtype.

The CLI in :func:`tostools.tos._audit_main` is the user-facing wrapper; this
module owns the read-only logic so it can be reused by ``cfg move`` /
``cfg update`` / ``cfg correct`` as pre- and post-write gates.

Design reference
----------------
``/home/bgo/notes/bgovault/2.Areas/VI_GPS_Library/1778592216-device-warehouse-design.md``
captures the full invariant set and the decision matrix.

API surface — unauthenticated reads
-----------------------------------
Audit takes a :class:`TOSClient`, not a :class:`TOSWriter`. TOS exposes
``/basic_search/`` and ``/history/entity/<id>/`` without authentication, and
TOSWriter would force a credential prompt on every audit run; TOSClient stays
silent. Pre- and post-write gates inside ``cfg move`` (which already needs an
authenticated writer) can construct a side TOSClient cheaply or hand the
audit module a writer that exposes the same duck-typed ``get_entity_history``
+ ``basic_search`` methods.

Limitation: current-state audit only
------------------------------------
TOS exposes ``children_connections`` only from the parent side; a device's
history endpoint returns ``id_entity_parent`` (the most-recent parent) but no
``parents_connections`` list (see
``receivers/cfg/location_check.py:108-110``). We therefore audit a device's
**current** I1 state — does it have exactly one open join right now? — and
cannot enumerate historical joins from the device side. This is sufficient
for pre- and post-write gates because every operation that mutates joins
specifies the device explicitly, so the parent is always known. Reconstructing
a device's full move history would require walking all possible parents and is
out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .api.tos_client import TOSClient

# B9 - Kjallari - Jörð — the virtual warehouse for GPS gear (design doc I3).
B9_JORD_ID_ENTITY: int = 4

# GPS-domain subtypes we audit.
GPS_DEVICE_SUBTYPES: tuple[str, ...] = (
    "gnss_receiver",
    "antenna",
    "radome",
    "monument",
)

# Short-name aliases accepted by the CLI; resolved to canonical TOS subtypes.
SUBTYPE_ALIASES: Dict[str, str] = {
    "receiver": "gnss_receiver",
    "gnss_receiver": "gnss_receiver",
    "antenna": "antenna",
    "radome": "radome",
    "monument": "monument",
}

# Station-completeness expectation for GPS sites. Radome is intentionally
# omitted — many sites run bare and "no radome" is a legitimate steady state.
GPS_STATION_EXPECTED_SUBTYPES: tuple[str, ...] = (
    "gnss_receiver",
    "antenna",
    "monument",
)

# Station entity subtypes that represent **real physical sites** where I2
# (at most one open join per device subtype) applies.
#
# TOS does NOT enforce any structure here — both real stations and
# warehouses are sibling subtypes of the same generic `Stöðvar` entity
# type. We impose the distinction so a warehouse like B9 - Kjallari - Jörð
# (which legitimately holds many devices of the same subtype) doesn't trip
# I2. Add SIL station subtypes here when audit grows beyond GPS.
REAL_STATION_SUBTYPES: tuple[str, ...] = ("geophysical",)

# Default search terms used to enumerate the deployed device population for
# fleet-wide audits (:func:`list_orphan_devices`). Sourced from the F audit
# in the design doc (2026-05-12), which found 246 gnss_receivers across
# these five model strings.
DEFAULT_ORPHAN_SCAN_MODELS: Dict[str, tuple[str, ...]] = {
    "gnss_receiver": ("POLARX5", "NetR9", "NetRS", "NetR5", "GR10"),
    # Antenna / radome / monument seed lists are TBD — extend as the F audit
    # is broadened to those subtypes.
}


def canonical_subtype(raw: str) -> str:
    """Resolve a short or canonical subtype name to the TOS canonical form."""
    try:
        return SUBTYPE_ALIASES[raw]
    except KeyError as exc:
        raise ValueError(
            f"Unknown subtype {raw!r}. Valid: "
            f"{', '.join(sorted(set(SUBTYPE_ALIASES)))}"
        ) from exc


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JoinRecord:
    """A single parent-child join, as seen from the parent's
    ``children_connections``."""

    id_entity_parent: int
    id_entity_child: int
    time_from: str
    time_to: Optional[str]
    parent_name: Optional[str] = None
    child_subtype: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.time_to is None


@dataclass
class DeviceAuditReport:
    """Result of :func:`audit_device`.

    ``open_joins`` is the list of currently-open joins to the device's
    ``current_parent_id``. Invariant I1 holds when ``len(open_joins) == 1``;
    callers should treat any other length as a violation.
    """

    id_entity: int
    subtype: str
    serial: Optional[str] = None
    current_parent_id: Optional[int] = None
    current_parent_name: Optional[str] = None
    current_parent_subtype: Optional[str] = None
    open_joins: List[JoinRecord] = field(default_factory=list)
    invariant_I1_ok: bool = True
    invariant_violations: List[str] = field(default_factory=list)

    @property
    def has_violations(self) -> bool:
        return bool(self.invariant_violations)


@dataclass
class OrphanScanResult:
    """Result of :func:`list_orphan_devices` — a fleet-wide I1 scan."""

    subtype: str
    models_searched: List[str] = field(default_factory=list)
    total_audited: int = 0
    orphan_reports: List["DeviceAuditReport"] = field(default_factory=list)

    @property
    def violation_count(self) -> int:
        return len(self.orphan_reports)


@dataclass
class StationAuditReport:
    """Result of :func:`audit_station`.

    ``open_children_by_subtype`` maps each child subtype to its open joins.
    Invariant I2 holds when every list has length ≤ 1.

    ``completeness_warnings`` flag absent expected subtypes
    (:data:`GPS_STATION_EXPECTED_SUBTYPES`); these are advisory only — partial
    sets are legitimate during physical maintenance.
    """

    id_entity: int
    name: Optional[str] = None
    subtype: Optional[str] = None
    is_real_station: bool = True
    open_children_by_subtype: Dict[str, List[JoinRecord]] = field(default_factory=dict)
    invariant_I2_ok: bool = True
    invariant_violations: List[str] = field(default_factory=list)
    completeness_warnings: List[str] = field(default_factory=list)

    @property
    def has_violations(self) -> bool:
        return bool(self.invariant_violations)


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def _open_attr_value(
    attributes: Optional[List[Dict[str, Any]]], code: str
) -> Optional[str]:
    """Return the value of the open period for *code*, or ``None``."""
    for attr in attributes or []:
        if attr.get("code") != code:
            continue
        if attr.get("date_to") is not None:
            continue
        value = attr.get("value")
        if value is not None:
            return str(value)
    return None


def _connection_to_join(
    conn: Dict[str, Any],
    *,
    parent_id: int,
    parent_name: Optional[str] = None,
    child_subtype: Optional[str] = None,
) -> JoinRecord:
    return JoinRecord(
        id_entity_parent=parent_id,
        id_entity_child=int(conn["id_entity_child"]),
        time_from=str(conn["time_from"]),
        time_to=conn.get("time_to"),
        parent_name=parent_name,
        child_subtype=child_subtype,
    )


# ---------------------------------------------------------------------------
# Device audit
# ---------------------------------------------------------------------------


def _find_device_by_serial(
    client: TOSClient, subtype: str, serial: str
) -> Optional[int]:
    """Return the id_entity for a device by (subtype, exact serial), or None.

    Mirrors :meth:`TOSWriter.find_device_by_serial` but without auth: walks
    basic_search hits, filters to ``code='serial_number'`` exact matches,
    then verifies the candidate's subtype via the history endpoint.
    """
    hits = client.basic_search(serial)
    for hit in hits:
        if hit.get("code") != "serial_number":
            continue
        if hit.get("distance") not in (0, None):
            continue
        if hit.get("value_varchar") != serial:
            continue
        candidate_id = hit.get("id_lvl_three") or hit.get("id_entity")
        if not candidate_id:
            continue
        history = client.get_entity_history(int(candidate_id))
        if history and history.get("code_entity_subtype") == subtype:
            return int(history["id_entity"])
    return None


def _resolve_device_entity(
    client: TOSClient,
    *,
    serial: Optional[str],
    id_entity: Optional[int],
    subtype: Optional[str],
) -> Dict[str, Any]:
    if id_entity is not None:
        history = client.get_entity_history(int(id_entity))
        if not history:
            raise LookupError(f"No entity with id_entity={id_entity}")
        return history
    if not serial:
        raise ValueError("audit_device requires either id_entity or serial")
    if not subtype:
        raise ValueError("audit_device requires --subtype when resolving by serial")
    canonical = canonical_subtype(subtype)
    found_id = _find_device_by_serial(client, canonical, serial)
    if not found_id:
        raise LookupError(f"No {canonical} with serial {serial!r}")
    history = client.get_entity_history(found_id)
    if not history:
        raise LookupError(f"No history for id_entity={found_id}")
    return history


def audit_device(
    client: TOSClient,
    *,
    serial: Optional[str] = None,
    id_entity: Optional[int] = None,
    subtype: Optional[str] = None,
) -> DeviceAuditReport:
    """Verify a device's I1 state (exactly one open join).

    Pass either ``id_entity`` (preferred when known) **or** ``serial`` +
    ``subtype``. Returns a :class:`DeviceAuditReport`; never raises on
    invariant violations — those populate ``invariant_violations``. Raises
    :class:`LookupError` if the device cannot be resolved, and
    :class:`ValueError` for insufficient arguments.

    Invariant I1 check:

    1. Resolve the device's history.
    2. Read ``id_entity_parent`` (current parent, or ``None``).
    3. If no parent → I1 violation (orphan with no recorded location).
    4. Else fetch the parent's ``children_connections``, count open joins
       (``time_to is None``) where ``id_entity_child == device_id``:
       0 → I1 violation (closed-without-replacement orphan);
       1 → I1 OK;
       ≥2 → I1 violation (multi-open at same parent — unusual).
    """
    history = _resolve_device_entity(
        client, serial=serial, id_entity=id_entity, subtype=subtype
    )

    device_id = int(history["id_entity"])
    device_subtype = str(history.get("code_entity_subtype") or "")
    device_serial = _open_attr_value(history.get("attributes"), "serial_number")
    parent_id_raw = history.get("id_entity_parent")
    parent_id = int(parent_id_raw) if parent_id_raw else None

    report = DeviceAuditReport(
        id_entity=device_id,
        subtype=device_subtype,
        serial=device_serial,
        current_parent_id=parent_id,
    )

    if parent_id is None:
        report.invariant_I1_ok = False
        report.invariant_violations.append(
            "I1 no-parent: device has no current parent in TOS"
        )
        return report

    parent_history = client.get_entity_history(parent_id)
    if not parent_history:
        report.invariant_I1_ok = False
        report.invariant_violations.append(
            f"I1 read-error: cannot read history for parent "
            f"id_entity={parent_id} (audit incomplete)"
        )
        return report

    report.current_parent_name = _open_attr_value(
        parent_history.get("attributes"), "name"
    )
    parent_subtype = parent_history.get("code_entity_subtype")
    if parent_subtype:
        report.current_parent_subtype = str(parent_subtype)

    open_matches: List[JoinRecord] = []
    for conn in parent_history.get("children_connections") or []:
        if int(conn.get("id_entity_child") or 0) != device_id:
            continue
        if conn.get("time_to") is not None:
            continue
        open_matches.append(
            _connection_to_join(
                conn,
                parent_id=parent_id,
                parent_name=report.current_parent_name,
                child_subtype=device_subtype,
            )
        )

    report.open_joins = open_matches

    parent_label = (
        f"{report.current_parent_name!r}" if report.current_parent_name else "?"
    )
    if len(open_matches) == 0:
        report.invariant_I1_ok = False
        report.invariant_violations.append(
            f"I1 orphan: last at {parent_label} (id_entity={parent_id}), "
            f"attachment closed without replacement"
        )
    elif len(open_matches) > 1:
        report.invariant_I1_ok = False
        report.invariant_violations.append(
            f"I1 multi-open: {len(open_matches)} simultaneous open joins "
            f"to parent {parent_label} (id_entity={parent_id})"
        )

    return report


# ---------------------------------------------------------------------------
# Station audit
# ---------------------------------------------------------------------------


def _resolve_station_entity(
    client: TOSClient,
    *,
    name: Optional[str],
    id_entity: Optional[int],
) -> Dict[str, Any]:
    """Look up a station entity by id, marker, or display name.

    The CLI accepts either ``--id <n>`` or a positional argument like
    ``RHOF``. Operators typically type the **marker** (the 4-letter station
    identifier) — TOS stores that as an attribute with ``code='marker'``.
    The display name (``code='name'``) is the long Icelandic name
    (``Raufarhöfn``). We try marker first, then fall back to name, so the
    common case (``tos audit station RHOF``) just works.
    """
    if id_entity is not None:
        history = client.get_entity_history(int(id_entity))
        if not history:
            raise LookupError(f"No entity with id_entity={id_entity}")
        return history
    if not name:
        raise ValueError("audit_station requires either id_entity or name")
    hits = client.basic_search(name)
    # TOS stores station markers as lowercase ("rhof") regardless of how
    # operators type them. Compare case-insensitively so the natural
    # workflow (`tos audit station RHOF`) just works. Names follow the
    # same lenient rule for Icelandic-character consistency.
    target = name.lower()

    def _exact(code: str) -> List[Dict[str, Any]]:
        matches = []
        for hit in hits:
            if hit.get("code") != code:
                continue
            value = hit.get("value_varchar")
            if isinstance(value, str) and value.lower() == target:
                matches.append(hit)
        return matches

    # 1. Markers are intended to be globally unique; first marker match wins.
    for hit in _exact("marker"):
        entity_id = hit.get("id_entity")
        if entity_id:
            history = client.get_entity_history(int(entity_id))
            if history:
                return history

    # 2. Names can collide (e.g. "Raufarhöfn" has a weather station entity
    #    AND a geophysical station entity AND a couple of parent entities).
    #    Dedupe candidates by id_entity, then prefer ones whose
    #    `subtype_lvl_two` is "Jarðeðlisstöð" (real physical GPS site).
    name_hits = _exact("name")
    by_id: Dict[int, Dict[str, Any]] = {}
    for hit in name_hits:
        entity_id = hit.get("id_entity")
        if entity_id:
            by_id.setdefault(int(entity_id), hit)

    if len(by_id) == 1:
        chosen_id = next(iter(by_id))
        history = client.get_entity_history(chosen_id)
        if history:
            return history

    if len(by_id) > 1:
        geophysical = {
            eid: h
            for eid, h in by_id.items()
            if h.get("subtype_lvl_two") == "Jarðeðlisstöð"
        }
        if len(geophysical) == 1:
            chosen_id = next(iter(geophysical))
            history = client.get_entity_history(chosen_id)
            if history:
                return history
        if len(geophysical) > 1:
            candidates = ", ".join(
                f"id_entity={eid} ({h.get('subtype_lvl_two')})"
                for eid, h in geophysical.items()
            )
            raise LookupError(
                f"Multiple geophysical stations match name {name!r}: "
                f"{candidates}. Disambiguate with --id."
            )
        # Multiple matches but none are geophysical — surface them.
        candidates = ", ".join(
            f"id_entity={eid} ({h.get('subtype_lvl_two')!r})"
            for eid, h in by_id.items()
        )
        raise LookupError(
            f"Multiple entities match name {name!r} but none is a "
            f"geophysical station: {candidates}. Disambiguate with --id."
        )

    raise LookupError(
        f"No station entity with exact marker or name {name!r}. "
        "Try --id <id_entity> if the marker is non-standard."
    )


def audit_station(
    client: TOSClient,
    *,
    name: Optional[str] = None,
    id_entity: Optional[int] = None,
) -> StationAuditReport:
    """Verify a station's I2 state and emit completeness warnings.

    Pass either ``name`` (exact match via basic_search) or ``id_entity``.
    Returns a :class:`StationAuditReport`; never raises on invariant
    violations. Raises :class:`LookupError` if the station cannot be resolved
    and :class:`ValueError` for insufficient arguments.

    Invariant I2: groups open ``children_connections`` by each child's
    ``code_entity_subtype`` (one extra ``/history/entity/<id>/`` per open
    child to learn the subtype). Multi-open per subtype → I2 violation.

    Completeness: emits a non-blocking warning for each subtype in
    :data:`GPS_STATION_EXPECTED_SUBTYPES` that has no open child. Partial
    sets are legitimate during physical maintenance.
    """
    history = _resolve_station_entity(client, name=name, id_entity=id_entity)

    station_id = int(history["id_entity"])
    station_name = _open_attr_value(history.get("attributes"), "name") or name
    station_subtype_raw = history.get("code_entity_subtype")
    station_subtype = str(station_subtype_raw) if station_subtype_raw else None
    is_real_station = station_subtype in REAL_STATION_SUBTYPES

    report = StationAuditReport(
        id_entity=station_id,
        name=station_name,
        subtype=station_subtype,
        is_real_station=is_real_station,
    )

    open_by_subtype: Dict[str, List[JoinRecord]] = {}
    for conn in history.get("children_connections") or []:
        if conn.get("time_to") is not None:
            continue
        child_id_raw = conn.get("id_entity_child")
        if child_id_raw is None:
            continue
        child_id = int(child_id_raw)
        child_history = client.get_entity_history(child_id)
        if not child_history:
            continue
        child_subtype = str(child_history.get("code_entity_subtype") or "")
        if not child_subtype:
            continue
        join = _connection_to_join(
            conn,
            parent_id=station_id,
            parent_name=station_name,
            child_subtype=child_subtype,
        )
        open_by_subtype.setdefault(child_subtype, []).append(join)

    report.open_children_by_subtype = open_by_subtype

    # I2 + completeness apply only to real physical stations. Warehouses
    # (B9 - Kjallari - Jörð and any other `Lager`-style entity) legitimately
    # hold many devices of the same subtype and have no completeness
    # expectation — skip both checks.
    if is_real_station:
        for subtype, joins in open_by_subtype.items():
            if len(joins) > 1:
                report.invariant_I2_ok = False
                ids = ", ".join(str(j.id_entity_child) for j in joins)
                report.invariant_violations.append(
                    f"I2 duplicate {subtype}: {len(joins)} open children "
                    f"(ids: {ids}), expected at most one"
                )
        for expected in GPS_STATION_EXPECTED_SUBTYPES:
            if not open_by_subtype.get(expected):
                report.completeness_warnings.append(
                    f"missing {expected} (no open child of that subtype)"
                )

    return report


# ---------------------------------------------------------------------------
# Fleet-wide orphan scan
# ---------------------------------------------------------------------------


def _device_ids_by_model_search(
    client: TOSClient,
    subtype: str,
    models: Sequence[str],
) -> List[int]:
    """Enumerate device id_entity values for *subtype* by model search.

    For each *model* string, ``basic_search`` returns hits across all
    attribute codes; we keep only ``code='model'`` hits and resolve each
    candidate via the history endpoint to verify the canonical subtype.
    Results are deduplicated, preserving discovery order.
    """
    seen: Dict[int, None] = {}
    for model in models:
        hits = client.basic_search(model)
        for hit in hits:
            if hit.get("code") != "model":
                continue
            candidate_id_raw = hit.get("id_lvl_three") or hit.get("id_entity")
            if not candidate_id_raw:
                continue
            candidate_id = int(candidate_id_raw)
            if candidate_id in seen:
                continue
            history = client.get_entity_history(candidate_id)
            if not history:
                continue
            if history.get("code_entity_subtype") != subtype:
                continue
            seen[candidate_id] = None
    return list(seen)


def list_orphan_devices(
    client: TOSClient,
    *,
    subtype: str,
    models: Optional[Sequence[str]] = None,
) -> OrphanScanResult:
    """Audit a population of devices and return those with I1 violations.

    Enumerates devices of *subtype* via :func:`_device_ids_by_model_search`
    over *models* (defaults to :data:`DEFAULT_ORPHAN_SCAN_MODELS` for the
    subtype), then runs :func:`audit_device` on each. Devices whose audit
    reports `invariant_I1_ok=False` are collected into
    :class:`OrphanScanResult.orphan_reports`.

    This is the fleet-wide version of the F audit in the design doc — the
    same workflow that found 18 closed-without-replacement orphans across
    246 gnss_receivers on 2026-05-12.
    """
    canonical = canonical_subtype(subtype)
    if models is None:
        models = DEFAULT_ORPHAN_SCAN_MODELS.get(canonical, ())
        if not models:
            raise ValueError(
                f"No default model list for subtype {canonical!r}. "
                f"Pass --model to seed the search."
            )

    device_ids = _device_ids_by_model_search(client, canonical, models)

    result = OrphanScanResult(
        subtype=canonical,
        models_searched=list(models),
        total_audited=len(device_ids),
        orphan_reports=[],
    )

    for device_id in device_ids:
        try:
            report = audit_device(client, id_entity=device_id)
        except LookupError:
            continue
        if not report.invariant_I1_ok:
            result.orphan_reports.append(report)

    return result


# ---------------------------------------------------------------------------
# Human-readable explanations (used by the CLI under --verbose)
#
# The library returns structured reports + short tagged violation strings
# (I1/I2 prefixes for grep). These helpers translate that into plain English
# paragraphs covering "what this means", "expected state", and "how to fix
# it". They are shared between the CLI and any future web/phone frontend so
# users get the same prose everywhere.
# ---------------------------------------------------------------------------


_EXPECTED_DEVICE_STATE = (
    "Every device should have exactly one currently-open join — either to a "
    "station (deployed in the field) or to B9-Jörð, id_entity=4 (the "
    "warehouse for GPS gear that isn't deployed)."
)

_FIX_HINT_MANUAL = (
    "Find where the device physically is now. In the TOS web UI, open a new "
    "parent-child join from that location to the device, with time_from = "
    "the date it arrived there. CLI tool `cfg fix` (todo #5) will automate "
    "this in a later step."
)


def explain_device_violations(report: DeviceAuditReport) -> str:
    """Return a multi-line human explanation of a device's I1 violation.

    Returns an empty string when the report has no violations. Output is
    three paragraphs (What this means / Expected state / To fix), indented
    so it nests inside CLI output. Pick the dominant violation type to
    explain — for a device, that's a single I1 condition.
    """
    if report.invariant_I1_ok:
        return ""

    parent_label = (
        f"{report.current_parent_name!r}" if report.current_parent_name else "?"
    )
    parent_ref = (
        f" (id_entity={report.current_parent_id})"
        if report.current_parent_id is not None
        else ""
    )

    if report.current_parent_id is None:
        what = (
            f"This {report.subtype} (id_entity={report.id_entity}) has no "
            "current parent recorded in TOS at all. The device exists as an "
            "entity, but TOS doesn't think it lives anywhere — not at a "
            "station, not in the warehouse."
        )
    elif not report.open_joins:
        what = (
            f"This {report.subtype} (id_entity={report.id_entity}) was last "
            f"attached to {parent_label}{parent_ref}, but that attachment "
            "(join) has been closed (an end date was set) and no new "
            "attachment was opened to replace it. In TOS, the device is now "
            '"in limbo" — not at any location.'
        )
    elif len(report.open_joins) > 1:
        what = (
            f"This {report.subtype} (id_entity={report.id_entity}) has "
            f"{len(report.open_joins)} simultaneously open joins to "
            f"{parent_label}{parent_ref}. A device can only physically be in "
            "one place at a time, so two open joins to the same parent is "
            "an internal inconsistency in TOS."
        )
    else:
        # I1 ok-but-not-quite (e.g. read error). Fall back to a generic note.
        what = (
            f"Audit could not fully verify this {report.subtype} "
            f"(id_entity={report.id_entity}); see the violation lines above."
        )

    return (
        f"  What this means:\n    {what}\n\n"
        f"  Expected state:\n    {_EXPECTED_DEVICE_STATE}\n\n"
        f"  To fix:\n    {_FIX_HINT_MANUAL}"
    )


def explain_station_violations(report: StationAuditReport) -> str:
    """Return a multi-line human explanation of a station's I2 violation.

    Returns an empty string when the report has no I2 violations.
    Completeness warnings are advisory and don't trigger this output.
    """
    if report.invariant_I2_ok:
        return ""

    what = (
        f"Station {report.name!r} (id_entity={report.id_entity}) has more "
        "than one open join for at least one device subtype. TOS allows "
        "this, but operationally a station can only have one active "
        "receiver / antenna / etc. — having two open joins means one of "
        "them should have been closed when the swap happened."
    )
    expected = (
        "At most one open join per device subtype at this station. If a "
        "swap happened, the older device's join should have time_to set to "
        "the swap date."
    )
    fix = (
        "Identify which device is actually at the station now. In the TOS "
        "web UI, edit the older join(s) and set time_to to the swap date "
        "(use the date stamped on the field-replacement record). The "
        "displaced device should then be joined to its real new location "
        "(another station, or B9-Jörð, id_entity=4)."
    )

    return (
        f"  What this means:\n    {what}\n\n"
        f"  Expected state:\n    {expected}\n\n"
        f"  To fix:\n    {fix}"
    )


def orphan_scan_preamble() -> str:
    """Return a fixed paragraph explaining what an I1 orphan is.

    Used at the top of ``tos audit orphans`` output under ``--verbose``;
    one preamble for the whole list is enough since every row is the same
    violation type.
    """
    return (
        'What an "I1 orphan" means:\n'
        "  These devices were last attached to a station, but the\n"
        "  attachment was closed (an end date was set) and no new\n"
        "  attachment was opened. In TOS they currently live nowhere.\n"
        "\n"
        "Expected state:\n"
        "  " + _EXPECTED_DEVICE_STATE + "\n"
        "\n"
        "To fix one of these:\n"
        "  " + _FIX_HINT_MANUAL
    )
