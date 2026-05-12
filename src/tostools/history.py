"""Device-history-reconstruction primitives (synthesis plan §5 steps 2 & 3).

This module owns the read-only primitives used to walk and reconstruct the
full join history of GNSS devices in TOS.

Why a separate module
---------------------
TOS exposes ``children_connections`` only from the parent side; a device's
own ``id_entity_parent`` attribute can be stale (see GitHub issue #17, fixed
in PR #18). To reconstruct a device's complete timeline we have to walk
every parent it might have touched — :func:`build_join_index` does that
once and gives us O(1) per-device lookup afterwards via
:meth:`JoinIndex.timeline`. Gap detection on the timeline surfaces the
"device was somewhere unrecorded" cases the user cares about.

The synthesis note
``~/notes/bgovault/2.Areas/VI_GPS_Library/1778612553-tostools-history-reconstruction-leverage.md``
captures the full design.

What this module is NOT
-----------------------
- Not a writer (no auth needed).
- Not a substitute for ``tostools.audit``; the audit module continues to
  serve the I1/I2 invariant-check CLI. Both modules read TOS but answer
  different questions.
- Not a replacement for any ``tosGPS`` functionality. Pure addition.

API surface
-----------
Parent enumeration (§5 step 2):

* :class:`ParentEntity` — frozen dataclass describing one parent.
* :data:`KNOWN_INFRASTRUCTURE_IDS` — hardcoded entity IDs for the warehouse
  network + graveyard. Stable across deployments.
* :func:`enumerate_known_parents` — returns the bootstrap parent list
  (infrastructure + stations from ``stations.cfg``, optionally augmented).

Join index + device timeline (§5 step 3):

* :class:`Join` — one parent→child join from a parent's children_connections.
* :class:`JoinIndex` — global child→[joins] map, dict-backed.
* :class:`DeviceTimeline` — one device's full chronologically-sorted joins,
  with ``open_joins``, ``closed_joins``, ``is_currently_attached``,
  ``is_truly_orphan`` properties and ``gaps(min_days=…)`` detection.
* :class:`Gap` — one unrecorded period between two adjacent closed→any joins.
* :func:`build_join_index` — walk parents, return JoinIndex (~10s for the
  IMO fleet).
"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .api.tos_client import TOSClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hardcoded infrastructure
# ---------------------------------------------------------------------------
#
# These IDs are stable across TOS deployments and discovered through the
# 2026-05-12 probe of vi-api.vedur.is. They cannot be enumerated by any
# basic_search query (the device graveyard in particular has no attribute
# that matches "Lager" / "warehouse"), hence the hardcoded list.
#
# Verify membership with::
#
#   for eid in KNOWN_INFRASTRUCTURE_IDS:
#       h = client.get_entity_history(eid)
#       print(eid, h.get("code_entity_subtype"), h.get("attributes"))
#
# If any ID disappears from TOS, :func:`enumerate_known_parents` logs a
# warning and skips it; it does not raise. New warehouses should be
# discovered via ``basic_search('Lager')`` and added here.

KNOWN_WAREHOUSE_IDS: tuple[int, ...] = (
    4,  # B9 - Kjallari - Jörð         (primary GPS warehouse)
    5,  # B9 - Kjallari - Vatn         (hydrological sister warehouse)
    8,  # Vagnhöfði                    (offsite warehouse — parent location)
    10,  # Vagnhöfði - Kjallari - Jörð  (Vagnhöfði sub-warehouse, host of 3 audit-orphans)
    13,  # Vagnhöfði - Rafgeymaskúr     (Vagnhöfði sub-warehouse, battery shed)
    16920,  # B9 - Verkstæði - Veður       (weather workshop warehouse)
)

# The "device graveyard": id_entity=14, name='Hent', subtype='discarded'.
# Description: "Tæki sem hefur verið hent og eru ekki lengur í eigu VÍ"
#              (devices that have been thrown away, no longer owned by IMO).
# Has ~1989 children_connections — covers most retired device endpoints.
DEVICE_GRAVEYARD_ID: int = 14

KNOWN_INFRASTRUCTURE_IDS: tuple[int, ...] = KNOWN_WAREHOUSE_IDS + (DEVICE_GRAVEYARD_ID,)


# Subtypes treated as "station" (a device deployed in the field has one of these
# as its current parent). Sourced from the TOS /entity_subtypes endpoint
# probed 2026-05-12. Not exhaustive of TOS's full subtype universe — only
# those a GNSS receiver might plausibly be joined to.
STATION_SUBTYPES: frozenset[str] = frozenset(
    {
        "geophysical",  # SIL, GPS, gas, infrasound — the GNSS-receiver host type
        "meteorological",  # weather stations
        "general_station",  # generic station
        "remote_sensing",  # radar
        "ocean",  # ocean-based
        "ship",  # ship-based
        "total_station",
        "land",  # land-based location parent
    }
)

# Subtypes treated as "warehouse" (storage/inventory parents). Distinct from
# STATION_SUBTYPES because warehouse children aren't "deployed". Discovered
# 2026-05-12: ``area`` is the canonical warehouse subtype (B9, Vagnhöfði
# sub-locations), ``stock`` is used for parent locations that group multiple
# sub-warehouses (e.g. id=8 'Vagnhöfði' is subtype=stock and contains
# id=10 'Vagnhöfði - Kjallari - Jörð' which is subtype=area).
WAREHOUSE_SUBTYPES: frozenset[str] = frozenset({"area", "stock"})


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParentEntity:
    """One entity that can host devices (station / warehouse / graveyard).

    ``role`` is a coarse classification derived from ``code_subtype``; it
    exists so callers can format / group / colour-code without re-encoding
    the subtype taxonomy.
    """

    id_entity: int
    name: Optional[str]
    code_subtype: str
    role: str  # "station" | "warehouse" | "graveyard" | "other"

    @classmethod
    def from_history(cls, id_entity: int, history: Dict[str, Any]) -> "ParentEntity":
        """Build a :class:`ParentEntity` from a TOS ``/history/entity/<id>/`` response.

        Falls back gracefully when the entity has no ``name`` attribute
        in its own ``attributes`` payload (which happens for some legacy
        station entities — see :func:`enumerate_known_parents`'s docstring
        for the basic_search-based name-recovery path).
        """
        name = _open_attr(history.get("attributes"), "name")
        subtype = str(history.get("code_entity_subtype") or "")
        return cls(
            id_entity=int(id_entity),
            name=name,
            code_subtype=subtype,
            role=_role_for(subtype),
        )


def _role_for(code_subtype: str) -> str:
    if code_subtype in WAREHOUSE_SUBTYPES:
        return "warehouse"
    if code_subtype == "discarded":
        return "graveyard"
    if code_subtype in STATION_SUBTYPES:
        return "station"
    return "other"


def _open_attr(attributes: Optional[list], code: str) -> Optional[str]:
    """Return the value of the open (``time_to is None``) attribute with the
    given ``code``, or None."""
    if not attributes:
        return None
    for a in attributes:
        if a.get("code") != code:
            continue
        if a.get("time_to") is not None:
            continue
        value = a.get("value_varchar")
        if value is None:
            value = a.get("value")
        return str(value) if value is not None else None
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def default_station_cfg_path() -> Optional[str]:
    """Return the conventional path to the deployed ``stations.cfg``, or
    ``None`` if not present.

    Search order mirrors what the rest of the ecosystem already does:
    ``$GPS_CONFIG_PATH/stations.cfg`` → ``~/.config/gpsconfig/stations.cfg``.
    """
    env_dir = os.environ.get("GPS_CONFIG_PATH")
    candidates = []
    if env_dir:
        candidates.append(os.path.join(env_dir, "stations.cfg"))
    candidates.append(os.path.expanduser("~/.config/gpsconfig/stations.cfg"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def read_station_markers(cfg_path: str) -> List[str]:
    """Return the list of station markers (section names) from a stations.cfg.

    Stations.cfg uses one INI section per station, keyed by the marker
    (e.g. ``[RHOF]``).  Sections that look like global config (lowercase
    or contain ``=`` characters) are skipped.
    """
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    return [s for s in cfg.sections() if s and s.isupper()]


def resolve_marker_to_entity_id(client: TOSClient, marker: str) -> Optional[int]:
    """Resolve a 4-letter station marker to its TOS ``id_entity``.

    Uses ``basic_search`` and accepts only hits where:

    - ``code == 'marker'``
    - ``distance == 0`` (exact match)
    - ``value_varchar`` matches the marker case-insensitively

    Returns ``None`` if no exact match is found (e.g. for markers that
    are in ``stations.cfg`` but not yet in TOS).
    """
    target = marker.lower()
    for h in client.basic_search(marker):
        if h.get("code") != "marker":
            continue
        if h.get("distance") not in (0, None):
            continue
        value = h.get("value_varchar")
        if not isinstance(value, str) or value.lower() != target:
            continue
        eid = h.get("id_entity") or h.get("id_lvl_three")
        if eid:
            return int(eid)
    return None


def enumerate_known_parents(
    client: TOSClient,
    *,
    station_cfg_path: Optional[str] = None,
    extra_parent_ids: Iterable[int] = (),
) -> List[ParentEntity]:
    """Return the bootstrap parent list for device-history reconstruction.

    The returned list is the union of:

    1. **Hardcoded infrastructure**: warehouses (B9 + Vagnhöfði) and the
       device graveyard. See :data:`KNOWN_INFRASTRUCTURE_IDS`. These cannot
       be enumerated via ``basic_search``; the IDs are stable per TOS
       deployment.
    2. **Stations from ``stations.cfg``** (optional): each section name in
       the cfg is a station marker; we resolve it to its TOS ``id_entity``
       via :func:`resolve_marker_to_entity_id`. Markers that don't exist
       in TOS are silently skipped.
    3. **Caller-supplied** ``extra_parent_ids``: useful for hardening the
       list against the known gap that ``stations.cfg`` is incomplete
       (e.g., Fagradalsfjall (18409), Bláfjöll (4243), Bárðabunga (4239),
       Hestalda (5444) — these are parents in TOS that aren't in cfg).

    Each id is looked up once via ``/history/entity/<id>/``. Entities that
    cannot be read (404, transient errors) are logged at WARNING and
    skipped — the returned list is best-effort, never raises on a single
    missing entity.

    Note on stale ``id_entity_parent`` attributes: some legacy station
    entities have ``name=None`` in their own attribute history (the name
    is exposed only via ``basic_search`` lvl chains, not via the entity's
    own attributes). In those cases the returned :class:`ParentEntity`
    carries ``name=None`` rather than a name from another source — the
    caller can backfill from ``basic_search`` if cosmetic naming matters.

    Args:
        client: An unauthenticated :class:`TOSClient`.
        station_cfg_path: Path to ``stations.cfg``. If ``None``, falls back
            to :func:`default_station_cfg_path`; if still nothing, the
            stations-from-cfg step is skipped (only infrastructure and
            ``extra_parent_ids`` are returned).
        extra_parent_ids: Caller-supplied additional parent entity ids.
            Useful for known-missing stations not in ``stations.cfg``.

    Returns:
        List of :class:`ParentEntity`, deduplicated by ``id_entity``.
        Order: infrastructure first, then stations (in cfg order), then
        extras. Useful as a stable bootstrap input for the join index.
    """
    seen: Dict[int, ParentEntity] = {}

    def _add(eid: int) -> None:
        if eid in seen:
            return
        try:
            h = client.get_entity_history(eid)
        except Exception as exc:  # network errors are non-fatal
            logger.warning(
                "enumerate_known_parents: get_entity_history(%d) raised: %s; skipping",
                eid,
                exc,
            )
            return
        if not h:
            logger.warning(
                "enumerate_known_parents: no history for id_entity=%d; skipping",
                eid,
            )
            return
        seen[eid] = ParentEntity.from_history(eid, h)

    # 1. Infrastructure
    for eid in KNOWN_INFRASTRUCTURE_IDS:
        _add(eid)

    # 2. Stations from stations.cfg
    cfg_path = station_cfg_path or default_station_cfg_path()
    if cfg_path:
        markers = read_station_markers(cfg_path)
        logger.info(
            "enumerate_known_parents: resolving %d markers from %s",
            len(markers),
            cfg_path,
        )
        for marker in markers:
            try:
                eid = resolve_marker_to_entity_id(client, marker)
            except Exception as exc:
                logger.warning(
                    "enumerate_known_parents: resolve_marker_to_entity_id(%r) raised: %s",
                    marker,
                    exc,
                )
                continue
            if eid is None:
                logger.debug(
                    "enumerate_known_parents: marker %r not found in TOS; skipping",
                    marker,
                )
                continue
            _add(eid)
    else:
        logger.info(
            "enumerate_known_parents: no stations.cfg available; "
            "returning infrastructure + extra_parent_ids only"
        )

    # 3. Caller-supplied extras
    for eid in extra_parent_ids:
        _add(int(eid))

    return list(seen.values())


# ---------------------------------------------------------------------------
# Join records, indexed by child
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Join:
    """One parent→child join, sourced from a parent's ``children_connections``.

    The shape mirrors the raw TOS payload directly — we don't enrich with
    parent_name or child_subtype here (audit.JoinRecord does that). Keeping
    Join lean lets the index stay cheap to build.
    """

    id_entity_connection: int
    id_entity_parent: int
    id_entity_child: int
    time_from: str  # ISO 8601 datetime string from TOS
    time_to: Optional[str]  # None = currently open

    @property
    def is_open(self) -> bool:
        return self.time_to is None


@dataclass(frozen=True)
class Gap:
    """A period during which the device has no recorded parent.

    Bounded by the close of ``after`` (the preceding closed join) and the
    open of ``before`` (the next join, open or closed). The user's
    "device was at B9 but the move was never recorded" case shows up here.
    """

    id_entity: int
    after: Join  # the closed join that ends the previous coverage
    before: Join  # the next join that starts a new coverage period
    duration_days: float  # decimal days between after.time_to and before.time_from

    @property
    def time_from(self) -> Optional[str]:
        """The start of the gap (when the previous join closed)."""
        return self.after.time_to

    @property
    def time_to(self) -> str:
        """The end of the gap (when the next join opens)."""
        return self.before.time_from


class DeviceTimeline:
    """The complete join history of one device, sorted chronologically.

    Constructed by :meth:`JoinIndex.timeline`. Provides gap detection and
    "is this device currently attached?" queries.
    """

    __slots__ = ("id_entity", "joins")

    def __init__(self, id_entity: int, joins: Sequence[Join]):
        self.id_entity = int(id_entity)
        # Stable order: by time_from, with open joins after closed ones at
        # the same time_from (so the chronological narrative reads
        # "closed-then-opened" not "opened-then-closed").
        self.joins: List[Join] = sorted(
            joins,
            key=lambda j: (j.time_from or "", 0 if not j.is_open else 1),
        )

    def __repr__(self) -> str:
        return f"DeviceTimeline(id_entity={self.id_entity}, n_joins={len(self.joins)})"

    @property
    def open_joins(self) -> List[Join]:
        return [j for j in self.joins if j.is_open]

    @property
    def closed_joins(self) -> List[Join]:
        return [j for j in self.joins if not j.is_open]

    @property
    def is_currently_attached(self) -> bool:
        """True if at least one open join exists."""
        return any(j.is_open for j in self.joins)

    @property
    def is_truly_orphan(self) -> bool:
        """True if the device has joins (so we know about it) but none open
        (no current parent in TOS — the audit's I1-orphan signal, but
        derived from the full index rather than ``id_entity_parent``).
        """
        return bool(self.joins) and not self.is_currently_attached

    def gaps(self, *, min_days: float = 0.0) -> List[Gap]:
        """Surface periods where no parent is recorded.

        A gap is the time between the close of one join and the open of
        the next adjacent (sorted) join, when the previous one is closed
        and its ``time_to`` precedes the next one's ``time_from``.

        ``min_days`` filters short gaps (typically date-rounding artifacts;
        see synthesis note §2.3). Default 0 returns every gap; production
        callers will want a threshold (start with 30 and calibrate).
        """
        if len(self.joins) < 2:
            return []
        out: List[Gap] = []
        for prev, curr in zip(self.joins, self.joins[1:]):
            if prev.is_open:
                # Two opens or open-then-closed — that's overlap territory,
                # not a gap. The audit's I1-multi-open / I2 checks deal
                # with that case; we skip here.
                continue
            prev_end = _parse_iso(prev.time_to)
            curr_start = _parse_iso(curr.time_from)
            if prev_end is None or curr_start is None:
                continue
            delta_days = (curr_start - prev_end).total_seconds() / 86400.0
            if delta_days <= 0:
                # Overlap, not a gap.
                continue
            if delta_days < min_days:
                continue
            out.append(
                Gap(
                    id_entity=self.id_entity,
                    after=prev,
                    before=curr,
                    duration_days=delta_days,
                )
            )
        return out


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Best-effort ISO 8601 → datetime; returns None on failure.

    TOS returns dates like ``'2025-11-05T00:00:00'`` (no timezone). We
    accept the trailing ``Z`` form too for safety. Date-only fallback
    handles malformed entries.
    """
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        head = s.split("T")[0]
        try:
            return datetime.fromisoformat(head)
        except ValueError:
            return None


@dataclass
class JoinIndex:
    """Global child→[joins] map, the cheap foundation of device-timeline lookup.

    Built by :func:`build_join_index`. Lookup is O(1) per device.

    Internal storage is a plain dict; the dataclass wrapper exists so we
    can attach metadata (build time, parents visited) without making the
    API surface large.
    """

    by_child: Dict[int, List[Join]] = field(default_factory=dict)
    parents_walked: int = 0
    parents_failed: int = 0

    def timeline(self, id_entity: int) -> DeviceTimeline:
        """Return the :class:`DeviceTimeline` for one device.

        If no joins are indexed for that id, returns an empty timeline
        (useful: ``timeline.is_currently_attached`` will be False).
        """
        joins = self.by_child.get(int(id_entity), [])
        return DeviceTimeline(id_entity, joins)

    @property
    def total_joins(self) -> int:
        return sum(len(v) for v in self.by_child.values())

    @property
    def device_ids(self) -> List[int]:
        """All distinct child ids appearing in the index, sorted."""
        return sorted(self.by_child.keys())


# ---------------------------------------------------------------------------
# Index builder
# ---------------------------------------------------------------------------


def _connection_to_join(
    conn: Dict[str, Any], fallback_parent_id: int
) -> Optional[Join]:
    """Convert one ``children_connections[i]`` entry into a :class:`Join`.

    Returns None for malformed entries (missing child id). The parent id
    in the connection record is preferred; we fall back to the walked
    parent's id when TOS omits it (rare but possible for some legacy
    rows).
    """
    cid_raw = conn.get("id_entity_child")
    if cid_raw is None:
        return None
    try:
        cid = int(cid_raw)
    except (TypeError, ValueError):
        return None
    pid_raw = conn.get("id_entity_parent")
    try:
        pid = int(pid_raw) if pid_raw is not None else int(fallback_parent_id)
    except (TypeError, ValueError):
        pid = int(fallback_parent_id)
    return Join(
        id_entity_connection=int(conn.get("id_entity_connection") or 0),
        id_entity_parent=pid,
        id_entity_child=cid,
        time_from=str(conn.get("time_from") or ""),
        time_to=conn.get("time_to"),
    )


def build_join_index(
    client: TOSClient,
    parents: Optional[Iterable[ParentEntity]] = None,
    *,
    progress: Optional[Callable[[int, int], None]] = None,
) -> JoinIndex:
    """Walk every parent's ``children_connections`` and aggregate joins by child.

    This is the cheap O(P) primitive that makes per-device timeline lookup
    O(1). For the live IMO TOS state (~200 parents, ~5000 joins total),
    a fresh build takes ~10 seconds; subsequent ``.timeline(id)`` calls
    are dict lookups.

    Args:
        client: An unauthenticated :class:`TOSClient`.
        parents: An iterable of :class:`ParentEntity` to walk. If ``None``,
            calls :func:`enumerate_known_parents` to use the bootstrap list.
            Pass a custom iterable when you know exactly which parents
            matter (e.g. only the warehouses, for a fast spot check).
        progress: Optional ``(current, total)`` callback fired after each
            parent fetch. Useful for CLI progress bars.

    Returns:
        A :class:`JoinIndex`. ``parents_walked`` / ``parents_failed`` on the
        returned index reveal whether any parent failed to read (transient
        TOS errors); the index is still usable, just missing those joins.
    """
    if parents is None:
        parents = enumerate_known_parents(client)
    parent_list = list(parents)

    index = JoinIndex()
    total = len(parent_list)
    for i, parent in enumerate(parent_list, 1):
        try:
            h = client.get_entity_history(parent.id_entity)
        except Exception as exc:
            logger.warning(
                "build_join_index: parent %d (%s) raised: %s; skipping",
                parent.id_entity,
                parent.name,
                exc,
            )
            index.parents_failed += 1
            if progress:
                progress(i, total)
            continue
        if not h:
            logger.warning(
                "build_join_index: parent %d (%s) returned no history; skipping",
                parent.id_entity,
                parent.name,
            )
            index.parents_failed += 1
            if progress:
                progress(i, total)
            continue
        index.parents_walked += 1
        for conn in h.get("children_connections") or []:
            join = _connection_to_join(conn, parent.id_entity)
            if join is None:
                continue
            index.by_child.setdefault(join.id_entity_child, []).append(join)
        if progress:
            progress(i, total)

    return index
