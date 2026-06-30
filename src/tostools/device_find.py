"""``tos device find --serial`` — the serial → entity dup-guard.

Walks the **global join index** to report every TOS device entity carrying a
given serial, its current parent (station / B9 / none), and join count, then
classifies the result into the create-vs-move-vs-reopen bucket every
reconstruction (``add-receiver``, ``onboard-station``, duplicate arbitration)
must gate on **before** it mints a device.

Why not :func:`tostools.devices.find_device`? That helper resolves via
``basic_search``, which returns ``None`` for serials that *do* exist whenever
the search index is stale — so a caller reads "not found" and creates a
**second** entity for a unit TOS already knows. That is the GRAN incident.
The join-index walk reads every parent's ``children_connections`` directly, so
a serial that exists anywhere in the fleet is found.

The walk is a **floor, not a census**: if some parent fails to read, a device
living only under it is invisible. This module therefore treats "no match
**while** parents failed to read" as :data:`INCONCLUSIVE` — never as proof of
absence. Only a clean walk (zero parents failed) with zero matches yields
:data:`CREATE`. That coverage hard-gate is the entire point of the verb.

Read-only; no credentials needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .audit_duplicate_serials import (
    DeviceScanRow,
    ProgressCb,
    _is_placeholder,
    scan_fleet_devices,
)
from .history import TOSClient

logger = logging.getLogger(__name__)

# --- Buckets -----------------------------------------------------------------
# The action a reconstruction should take given the lookup result.
CREATE = "create"
"""Serial found nowhere in a clean walk → safe to create a new device."""

REOPEN = "reopen"
"""Exactly one entity, currently detached (parked in B9 / orphan) → reopen or
move that entity; do NOT create a new one."""

ATTACHED = "attached"
"""Exactly one entity, currently attached to a parent → it already exists and
is live; move it if relocating, otherwise it is already correct."""

DUPLICATE = "duplicate"
"""Two or more entities share the serial → a create-instead-of-move defect;
arbitrate with ``tos audit duplicate-serials``."""

INCONCLUSIVE = "inconclusive"
"""No match BUT one or more parents failed to read — absence cannot be
confirmed. Never create on this result (the GRAN footgun)."""

# Exit-code contract for the CLI. The verb is a dup-guard, so the natural
# shell gate ``tos device find … && tos device add …`` must be SAFE: exit 0
# happens ONLY for CREATE (serial provably absent). Every "exists" / "unsure"
# bucket exits non-zero with a distinct code so scripts can branch:
#   0 create · 1 inconclusive · 3 exists (attached/reopen) · 4 duplicate
EXIT_CODES = {
    CREATE: 0,
    INCONCLUSIVE: 1,
    ATTACHED: 3,
    REOPEN: 3,
    DUPLICATE: 4,
}


@dataclass
class SerialLookup:
    """Result of :func:`find_devices_by_serial`."""

    serial: str
    subtype: Optional[str]
    matches: List[DeviceScanRow]
    coverage: Dict[str, int] = field(default_factory=dict)
    bucket: str = INCONCLUSIVE

    @property
    def parents_failed(self) -> int:
        return self.coverage.get("parents_failed", 0)

    @property
    def is_placeholder_serial(self) -> bool:
        """True when the queried serial is itself a TOS placeholder pattern
        (``receiver-*`` / all-same-digit / non-numeric) — a lookup on such a
        value groups unrelated devices and should be read with suspicion."""
        return _is_placeholder(self.serial)

    @property
    def recommendation(self) -> str:
        """One-line operator guidance for the resolved bucket."""
        if self.bucket == CREATE:
            return (
                "Not found in a clean fleet walk — safe to CREATE a new device "
                "(e.g. tos device add / receivers cfg add-receiver)."
            )
        if self.bucket == INCONCLUSIVE:
            return (
                f"Not found, but {self.parents_failed} parent(s) failed to "
                "read — absence is UNCONFIRMED. Do NOT create. Re-run when the "
                "fleet is fully reachable, or pass an explicit device id."
            )
        if self.bucket == DUPLICATE:
            return (
                f"{len(self.matches)} entities share this serial — a "
                "create-instead-of-move DUPLICATE. Arbitrate with "
                "`tos audit duplicate-serials`; do NOT create."
            )
        # A single match on an incomplete walk is not provably unique — a
        # second entity could live under an unread parent (a latent
        # DUPLICATE). Dup-mint is still prevented ("do NOT create"), but flag
        # the uncertainty so the operator does not assume singularity.
        uniq = (
            f" ({self.parents_failed} parent(s) unread — a second entity may "
            "exist; not provably unique)"
            if self.parents_failed
            else ""
        )
        only = self.matches[0]
        where = only.open_parent_name or only.open_parent_id
        if self.bucket == ATTACHED:
            return (
                f"Exists as id_entity={only.id_entity}, attached to {where}. "
                f"MOVE it if relocating; otherwise it is already correct. Do "
                f"NOT create.{uniq}"
            )
        # REOPEN
        return (
            f"Exists as id_entity={only.id_entity}, detached (last parent "
            f"{where}). REOPEN / move that entity; do NOT create.{uniq}"
        )

    def to_json_dict(self) -> Dict[str, Any]:
        return {
            "serial": self.serial,
            "subtype": self.subtype,
            "bucket": self.bucket,
            "recommendation": self.recommendation,
            "serial_is_placeholder": self.is_placeholder_serial,
            "coverage": self.coverage,
            "matches": [
                {
                    "id_entity": m.id_entity,
                    "subtype": m.subtype,
                    "serial": m.serial,
                    "open_parent_id": m.open_parent_id,
                    "open_parent_name": m.open_parent_name,
                    "n_joins": m.n_joins,
                    "parked": m.parked,
                }
                for m in self.matches
            ],
        }


def _classify(matches: List[DeviceScanRow], coverage: Dict[str, int]) -> str:
    """Map matches + walk coverage to a bucket.

    The coverage hard-gate lives here: zero matches yields :data:`CREATE`
    **only** when the walk was complete (``parents_failed == 0``); otherwise
    it is :data:`INCONCLUSIVE`.
    """
    if len(matches) >= 2:
        return DUPLICATE
    if len(matches) == 1:
        return ATTACHED if not matches[0].parked else REOPEN
    # Zero matches — absence is only trustworthy if the walk was complete.
    if coverage.get("parents_failed", 0) > 0:
        return INCONCLUSIVE
    return CREATE


def find_devices_by_serial(
    client: TOSClient,
    serial: str,
    *,
    subtype: Optional[str] = None,
    parents: Optional[Any] = None,
    index: Optional[Any] = None,
    progress: Optional[ProgressCb] = None,
) -> SerialLookup:
    """Locate every device entity whose open serial equals ``serial``.

    Args:
        client: An unauthenticated :class:`TOSClient`.
        serial: The hardware serial to locate. Matched exactly (whitespace-
            trimmed on both sides).
        subtype: When set, restrict matches to this **canonical** TOS subtype
            (e.g. ``gnss_receiver``). Callers holding a short alias should
            canonicalise first (``audit.canonical_subtype``).
        parents / index: Optional pre-built fleet walk to reuse across many
            lookups — see :func:`scan_fleet_devices`. Omit for a one-shot
            ~110s walk.
        progress: Optional per-device progress callback.

    Returns:
        A :class:`SerialLookup` carrying the matched rows, the walk
        ``coverage``, and the resolved :data:`bucket`.
    """
    target = (serial or "").strip()
    coverage: Dict[str, int] = {}
    rows = scan_fleet_devices(
        client,
        parents=parents,
        index=index,
        progress=progress,
        coverage=coverage,
    )
    matches = [
        r
        for r in rows
        if (r.serial or "").strip() == target
        and (subtype is None or r.subtype == subtype)
    ]
    matches.sort(key=lambda r: r.id_entity)
    bucket = _classify(matches, coverage)
    return SerialLookup(
        serial=target,
        subtype=subtype,
        matches=matches,
        coverage=coverage,
        bucket=bucket,
    )


def format_report(lookup: SerialLookup) -> str:
    """Render a :class:`SerialLookup` as a human-readable report."""
    lines: List[str] = []
    subtype_note = f" (subtype={lookup.subtype})" if lookup.subtype else ""
    lines.append(f"Serial {lookup.serial!r}{subtype_note}: {lookup.bucket.upper()}")
    if lookup.is_placeholder_serial:
        lines.append(
            "  ⚠ this serial is a PLACEHOLDER pattern — matches may be unrelated."
        )

    if lookup.matches:
        lines.append(
            f"  {len(lookup.matches)} matching entit"
            f"{'y' if len(lookup.matches) == 1 else 'ies'}:"
        )
        for m in lookup.matches:
            where = m.open_parent_name or (
                f"id={m.open_parent_id}" if m.open_parent_id else "no parent"
            )
            state = "parked" if m.parked else "attached"
            lines.append(
                f"    id_entity={m.id_entity}  subtype={m.subtype}  "
                f"{state} @ {where}  ({m.n_joins} join"
                f"{'' if m.n_joins == 1 else 's'})"
            )
    else:
        lines.append("  no matching entities.")

    walked = lookup.coverage.get("parents_walked", 0)
    failed = lookup.coverage.get("parents_failed", 0)
    cov = f"  coverage: {walked} parents walked, {failed} failed"
    if failed:
        # Wording depends on whether we found anything: an empty result on an
        # incomplete walk means absence is unconfirmed; a match means the
        # result is real but its completeness (uniqueness) is unconfirmed.
        cov += (
            " — completeness UNCONFIRMED (a match may exist under a dropped " "parent)"
            if lookup.matches
            else " — absence UNCONFIRMED (floor, not census)"
        )
    lines.append(cov)
    lines.append(f"  → {lookup.recommendation}")
    return "\n".join(lines)
