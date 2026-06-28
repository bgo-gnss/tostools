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
    parents = enumerate_known_parents(
        client,
        extra_parent_ids=KNOWN_MISSING_FROM_CFG_PARENT_IDS,
    )
    parent_list = list(parents)
    parent_names: Dict[int, Optional[str]] = {p.id_entity: p.name for p in parent_list}

    index = build_join_index(client, parents=parent_list)

    candidates = index.device_ids
    total = len(candidates)

    if coverage is not None:
        coverage["parents_walked"] = index.parents_walked
        coverage["parents_failed"] = index.parents_failed
        coverage["total_devices"] = total

    if index.parents_failed:
        logger.warning(
            "find_duplicate_serials: %d/%d parents failed to read — duplicate "
            "count is a FLOOR, not a census (dups under dropped parents are "
            "invisible)",
            index.parents_failed,
            index.parents_walked + index.parents_failed,
        )
    else:
        logger.info(
            "find_duplicate_serials: walked %d parents, %d candidate devices",
            index.parents_walked,
            total,
        )

    # key (subtype, serial) -> {id_entity -> location dict}
    groups: Dict[tuple[Optional[str], str], Dict[int, Dict[str, Any]]] = {}

    for i, did in enumerate(candidates, 1):
        try:
            history = client.get_entity_history(did)
        except Exception as exc:  # network errors, transient TOS failures
            logger.warning(
                "find_duplicate_serials: get_entity_history(%d) raised: %s; "
                "skipping",
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

        if subtype is not None and dev_subtype != subtype:
            continue
        if not serial:
            continue
        if not include_synthetic and _is_placeholder(serial):
            continue

        loc = _current_location(index.by_child.get(did, []))
        loc["id_entity"] = did
        loc["open_parent_name"] = parent_names.get(loc["open_parent_id"])
        groups.setdefault((dev_subtype, serial), {})[did] = loc

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
