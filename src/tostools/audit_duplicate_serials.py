"""Detect duplicate-device defects — entities that share a real serial.

When a physical receiver moves from station A to station B, the correct TOS
action is ``move_device`` (close A's join, open B's join on the *same*
entity). Historically some moves were entered as a **new device entity** at
B instead, leaving **two entity rows with the same serial** for one physical
unit. After the swap the redundant leg is *closed*, so ``fleet-sweep`` (which
only inspects the open receiver) never sees it — it is a latent
inventory-quality defect that needs its own detector.

This module is the detector. It walks the **global join index** (every
parent's ``children_connections`` — the ``fleet-gaps`` pattern), fetches each
candidate entity's open ``serial_number`` + subtype once, and groups by
``(subtype, serial)``. Any ``(subtype, serial)`` key with ≥2 distinct entity
ids is a duplicate group.

Design context: ``docs/architecture/dup-device-merge-scoping.md`` ("Detection"
section). This verb is step 1 of that doc — it sizes the problem and becomes
the verify oracle after merges.

Cost: one fleet join walk (~200 parent fetches, ~110s) plus one
``get_entity_history`` per candidate entity (the join index does not carry
serials). That makes this a multi-minute fleet walk; ``find_duplicate_serials``
emits stderr progress via the ``progress`` callback.

Read-only; no credentials needed (``build_join_index`` + ``get_entity_history``
are GET-only).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .audit_fleet_sweep import _synthetic
from .history import (
    KNOWN_MISSING_FROM_CFG_PARENT_IDS,
    TOSClient,
    _open_attribute_value,
    build_join_index,
    enumerate_known_parents,
)

logger = logging.getLogger(__name__)

ProgressCb = Callable[[int, int, int], None]


def _is_placeholder(serial: Any) -> bool:
    """True when *serial* is a TOS placeholder, not a real hardware serial.

    Extends :func:`tostools.audit_fleet_sweep._synthetic` (``receiver-*`` /
    ``0000000000`` / non-numeric) with an **all-same-digit** rule so values
    like ``99999999`` / ``11111111`` are also treated as placeholders. Many
    devices legitimately carry such "unknown" markers; without this they
    would group into one giant false duplicate. Real serials (e.g.
    ``5048K71916``, ``4100591``) are never all-same-digit, so they pass.
    """
    if _synthetic(serial):
        return True
    if not serial:
        return False
    s = str(serial).strip()
    return s.isdigit() and len(set(s)) == 1


def _current_location(joins: List[Any]) -> Dict[str, Any]:
    """Resolve a candidate's current/last parent from its join list.

    Returns ``{"open_parent_id", "n_joins", "parked"}``. When the device has
    an open join (``time_to is None``) it is *attached* and ``parked`` is
    False; otherwise we fall back to the latest-closed parent and mark it
    ``parked``. ``open_parent_id`` is None only when the device has no joins
    at all (should not happen for join-index candidates).
    """
    n_joins = len(joins)
    open_joins = [j for j in joins if j.time_to is None]
    if open_joins:
        return {
            "open_parent_id": open_joins[0].id_entity_parent,
            "n_joins": n_joins,
            "parked": False,
        }
    if joins:
        last = max(joins, key=lambda j: j.time_to or "")
        return {
            "open_parent_id": last.id_entity_parent,
            "n_joins": n_joins,
            "parked": True,
        }
    return {"open_parent_id": None, "n_joins": n_joins, "parked": True}


@dataclass
class DeviceScanRow:
    """One device's identity + current attachment, from a single fleet walk.

    The atomic output of :func:`scan_fleet_devices` — everything the
    duplicate-serial detector and the ``tos device find`` dup-guard both
    need, resolved identically so the two verbs can never disagree about
    where a device sits.
    """

    id_entity: int
    subtype: Optional[str]
    serial: Optional[str]
    open_parent_id: Optional[int]
    open_parent_name: Optional[str]
    n_joins: int
    parked: bool
    """True when the device has no open join (detached — in B9/warehouse or
    orphaned). False when it is currently attached to ``open_parent_id``."""


def scan_fleet_devices(
    client: TOSClient,
    *,
    parents: Optional[Any] = None,
    index: Optional[Any] = None,
    parent_names: Optional[Dict[int, Optional[str]]] = None,
    progress: Optional[ProgressCb] = None,
    coverage: Optional[Dict[str, int]] = None,
) -> List[DeviceScanRow]:
    """Walk the global join index once, returning every device's identity row.

    The shared fleet-walk primitive behind both :func:`find_duplicate_serials`
    and :func:`tostools.device_find.find_devices_by_serial`. Builds (or accepts
    a pre-built) join index, then fetches each candidate's subtype + open
    ``serial_number`` once and resolves its current/last parent.

    Cost: one fleet join walk (~200 parent fetches, ~110s) plus one
    ``get_entity_history`` per candidate. Batch callers that look up many
    serials in one session should build ``parents`` + ``index`` once
    (via :func:`enumerate_known_parents` / :func:`build_join_index`) and pass
    them in to avoid re-walking the fleet per call.

    Args:
        client: An unauthenticated :class:`TOSClient`.
        parents: Pre-enumerated parent list. When ``None`` (and ``index`` is
            also ``None``), enumerated via :func:`enumerate_known_parents`
            with :data:`KNOWN_MISSING_FROM_CFG_PARENT_IDS`.
        index: Pre-built :class:`JoinIndex`. When ``None`` it is built from
            ``parents``. Supply both to reuse one walk across many lookups.
        parent_names: ``{id_entity: name}`` for resolving ``open_parent_name``.
            Derived from ``parents`` when omitted; pass it explicitly when you
            supply a pre-built ``index`` but no ``parents`` list.
        progress: Optional ``(current, total, 0)`` callback fired after each
            per-device fetch (third arg is always 0 — kept for signature
            symmetry with the duplicate-serial progress line).
        coverage: Optional mutable dict, populated on return with
            ``parents_walked`` / ``parents_failed`` / ``total_devices``. When
            ``parents_failed > 0`` the walk is a **floor, not a census** — a
            device under a dropped parent is invisible. Callers that gate a
            create decision on "serial not found" MUST treat that case as
            inconclusive, not as proof of absence (the GRAN incident).

    Returns:
        One :class:`DeviceScanRow` per candidate device, in ``index.device_ids``
        order. Rows with an unreadable history carry ``subtype=None`` and
        ``serial=None`` but still record join location.
    """
    if index is None:
        if parents is None:
            parents = enumerate_known_parents(
                client,
                extra_parent_ids=KNOWN_MISSING_FROM_CFG_PARENT_IDS,
            )
        parents = list(parents)
        index = build_join_index(client, parents=parents)

    if parent_names is None:
        parent_names = {p.id_entity: p.name for p in (parents or [])}

    candidates = index.device_ids
    total = len(candidates)

    if coverage is not None:
        coverage["parents_walked"] = index.parents_walked
        coverage["parents_failed"] = index.parents_failed
        coverage["total_devices"] = total

    rows: List[DeviceScanRow] = []
    for i, did in enumerate(candidates, 1):
        try:
            history = client.get_entity_history(did)
        except Exception as exc:  # network errors, transient TOS failures
            logger.warning(
                "scan_fleet_devices: get_entity_history(%d) raised: %s; " "skipping",
                did,
                exc,
            )
            history = None
        if history:
            dev_subtype = history.get("code_entity_subtype") or None
            attrs = history.get("attributes") or []
            serial = _open_attribute_value(attrs, "serial_number")
        else:
            dev_subtype = None
            serial = None

        if progress:
            progress(i, total, 0)

        loc = _current_location(index.by_child.get(did, []))
        rows.append(
            DeviceScanRow(
                id_entity=did,
                subtype=dev_subtype,
                serial=serial,
                open_parent_id=loc["open_parent_id"],
                open_parent_name=parent_names.get(loc["open_parent_id"]),
                n_joins=loc["n_joins"],
                parked=loc["parked"],
            )
        )
    return rows


def find_duplicate_serials(
    client: TOSClient,
    *,
    subtype: Optional[str] = None,
    include_synthetic: bool = False,
    progress: Optional[ProgressCb] = None,
    coverage: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Find entities that share a real serial within a subtype.

    Walks the global join index (the ``fleet-gaps`` pattern), fetches each
    candidate entity's subtype + open ``serial_number`` once, and groups by
    ``(subtype, serial)``. Returns one group per ``(subtype, serial)`` key
    with ≥2 distinct entity ids.

    Args:
        client: An unauthenticated :class:`TOSClient`.
        subtype: When set, only group devices whose ``code_entity_subtype``
            matches (e.g. ``"gnss_receiver"``). ``None`` groups all subtypes;
            grouping is always keyed by ``(subtype, serial)`` so two different
            subtypes sharing a serial never collide.
        include_synthetic: When False (default), placeholder serials
            (``receiver-*`` / ``0000000000`` / all-same-digit like
            ``99999999`` / non-numeric) are excluded — many devices
            legitimately share an "unknown" placeholder and must not be
            reported as duplicates. Set True to include them.
        progress: Optional ``(current, total, n_dups)`` callback fired after
            each per-device serial fetch. ``n_dups`` is the running count of
            duplicate groups found so far (always 0 until grouping completes,
            so it is reported as 0 during the walk and is here for symmetry
            with the CLI progress line).
        coverage: Optional mutable dict the caller passes in; on return it is
            populated with ``parents_walked`` / ``parents_failed`` /
            ``total_devices`` from the join index. This is the verify-oracle
            coverage signal: when ``parents_failed > 0`` some parent's
            children were never walked, so the duplicate count is a **floor,
            not a census** (a dup living only under the dropped parent is
            invisible). Provided as an out-param so the ``-> list[dict]``
            return contract stays clean.

    Returns:
        List of group dicts, each::

            {
                "subtype": str | None,
                "serial": str,
                "entities": [
                    {"id_entity", "open_parent_id", "open_parent_name",
                     "n_joins", "parked"},
                    ...
                ],
            }

        Sorted by subtype then serial; entities within a group sorted by
        ``id_entity``. Only groups with ≥2 distinct entity ids are returned.
    """
    local_coverage: Dict[str, int] = {}
    rows = scan_fleet_devices(client, progress=progress, coverage=local_coverage)
    if coverage is not None:
        coverage.update(local_coverage)

    walked = local_coverage.get("parents_walked", 0)
    failed = local_coverage.get("parents_failed", 0)
    if failed:
        logger.warning(
            "find_duplicate_serials: %d/%d parents failed to read — duplicate "
            "count is a FLOOR, not a census (dups under dropped parents are "
            "invisible)",
            failed,
            walked + failed,
        )
    else:
        logger.info(
            "find_duplicate_serials: walked %d parents, %d candidate devices",
            walked,
            local_coverage.get("total_devices", 0),
        )

    # key (subtype, serial) -> {id_entity -> location dict}
    groups: Dict[tuple[Optional[str], str], Dict[int, Dict[str, Any]]] = {}

    for row in rows:
        if subtype is not None and row.subtype != subtype:
            continue
        if not row.serial:
            continue
        if not include_synthetic and _is_placeholder(row.serial):
            continue

        groups.setdefault((row.subtype, row.serial), {})[row.id_entity] = {
            "id_entity": row.id_entity,
            "open_parent_id": row.open_parent_id,
            "open_parent_name": row.open_parent_name,
            "n_joins": row.n_joins,
            "parked": row.parked,
        }

    out: List[Dict[str, Any]] = []
    for (grp_subtype, serial), by_id in groups.items():
        if len(by_id) < 2:
            continue
        entities = sorted(by_id.values(), key=lambda e: e["id_entity"])
        out.append(
            {
                "subtype": grp_subtype,
                "serial": serial,
                "entities": entities,
            }
        )

    out.sort(key=lambda g: (g["subtype"] or "", g["serial"]))
    return out


def format_report(groups: List[Dict[str, Any]], *, verbose: bool = False) -> str:
    """Render duplicate-serial groups as a human-readable report.

    Args:
        groups: Output of :func:`find_duplicate_serials`.
        verbose: When True, append a one-line explanation of what a
            duplicate group means and how to resolve it.

    Returns:
        A multi-line string. When ``groups`` is empty, a single clean line.
    """
    lines: List[str] = []
    if not groups:
        lines.append("No duplicate serials found.")
        if verbose:
            lines.append(
                "  (Every (subtype, serial) maps to exactly one entity — "
                "no create-instead-of-move duplicates detected.)"
            )
        return "\n".join(lines)

    n_entities = sum(len(g["entities"]) for g in groups)
    lines.append(
        f"Duplicate serials: {len(groups)} group(s), " f"{n_entities} entities."
    )
    lines.append("")
    for g in groups:
        subtype = g["subtype"] or "?"
        lines.append(
            f"{subtype}  serial {g['serial']}  ({len(g['entities'])} entities)"
        )
        for ent in g["entities"]:
            pid = ent["open_parent_id"]
            pname = ent["open_parent_name"]
            where = pname if pname else (f"id={pid}" if pid is not None else "?")
            state = "parked/closed" if ent["parked"] else "open"
            lines.append(
                f"    entity {ent['id_entity']:>7}  @ {where}  "
                f"[{state}, {ent['n_joins']} join(s)]"
            )
        lines.append("")

    if verbose:
        lines.append(
            "A duplicate group is one physical device modeled as two+ TOS "
            "entity rows (create-instead-of-move on a station transfer). "
            "Resolve with `tos device merge` (see "
            "docs/architecture/dup-device-merge-scoping.md)."
        )
    return "\n".join(lines).rstrip("\n")
