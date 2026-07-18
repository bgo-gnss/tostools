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

# Parent stations that exist in TOS but are missing from the deployed
# stations.cfg (discovered during the 2026-05-12 fleet-gap synthesis run).
# Each one hosted devices that would otherwise produce phantom gaps.
# Pass these via ``extra_parent_ids`` (or rely on the default in
# :func:`scan_fleet_gaps`) to avoid silent under-enumeration.
KNOWN_MISSING_FROM_CFG_PARENT_IDS: tuple[int, ...] = (
    18409,  # Fagradalsfjall
    4243,  # Bláfjöll
    4239,  # Bárðabunga
    5444,  # Hestalda
)


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


def read_station_roles(cfg_path: str) -> Dict[str, str]:
    """Map marker → ``station_role`` (``active``/``passive``) from stations.cfg.

    ``station_role = passive`` marks data-source-only stations (GLOBK
    reference-frame ties / regional context) that IMO does not operate and
    that have no TOS counterpart — fleet enumeration must skip them BEFORE
    marker→TOS resolution, or every fleet run burns one futile TOS HTTP call
    per passive marker (GLOBAL_SITES_investigation.md §4.4).

    Role parsing delegates to :func:`gps_parser.parse_station_role` — the
    CANONICAL fail-open parser (missing/empty/unknown → ``active``; a typo
    must never hide an operated station from the fleet audits). Do not
    re-implement the parse locally. A missing or unreadable cfg yields an
    empty map — callers treat absent markers as active.
    """
    # Lazy import, mirroring archive.py's optional gps_parser usage —
    # only fleet enumeration needs it, not all of tostools.
    from gps_parser import parse_station_role

    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    return {
        s.upper(): parse_station_role(cfg.get(s, "station_role", fallback=""))
        for s in cfg.sections()
        if s and s.isupper()
    }


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
    progress: Optional[Callable[[int, int], None]] = None,
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
        progress: Optional ``(current, total)`` callback fired after each
            marker-resolution step. ``total`` is the marker count from
            ``stations.cfg``; the infrastructure and extras phases are
            cheap and not reported. Useful for showing the operator that
            the slow ~100s marker-resolution loop is alive.

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
        total_markers = len(markers)
        for i, marker in enumerate(markers, 1):
            try:
                eid = resolve_marker_to_entity_id(client, marker)
            except Exception as exc:
                logger.warning(
                    "enumerate_known_parents: resolve_marker_to_entity_id(%r) raised: %s",
                    marker,
                    exc,
                )
                eid = None
            if eid is None:
                logger.debug(
                    "enumerate_known_parents: marker %r not found in TOS; skipping",
                    marker,
                )
            else:
                _add(eid)
            if progress:
                progress(i, total_markers)
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


def device_timeline_via_parent_history(client, id_entity: int) -> DeviceTimeline:
    """One device's :class:`DeviceTimeline`, built straight from its
    ``parent_history`` — O(1) TOS calls, **no** global :func:`build_join_index`.

    The global index walks every known parent's ``children_connections``
    (``enumerate_known_parents`` + ~200 fetches, ~110 s on the live IMO fleet)
    so that *any* device's timeline becomes a dict lookup. When you only need
    ONE — or a handful of — device(s) (the decommission apply path, a
    single-device ``device show``), that whole-fleet cost is wasted:
    ``GET /entity/parent_history/{id}`` returns the same joins from the child's
    side in a single call. It is also strictly *more* complete than the index,
    which silently omits joins to parents outside ``enumerate_known_parents``.

    The connection id is the row's ``id`` here (``children_connections`` names
    it ``id_entity_connection``); we accept either. ``time_to`` is ``None`` for
    an open join. Rows with no usable connection id are skipped.
    """
    joins: List[Join] = []
    for row in client.get_parent_history(int(id_entity)) or []:
        conn_raw = row.get("id_entity_connection")
        if conn_raw is None:
            conn_raw = row.get("id")
        if conn_raw is None:
            continue
        try:
            conn_id = int(conn_raw)
            parent_id = int(row.get("id_entity_parent") or 0)
        except (TypeError, ValueError):
            continue
        time_to = row.get("time_to")
        # The endpoint returns JSON null → None for open joins, but be
        # defensive about a stringified "None"/"" leaking through.
        if time_to is not None and str(time_to).strip() in ("", "None"):
            time_to = None
        joins.append(
            Join(
                id_entity_connection=conn_id,
                id_entity_parent=parent_id,
                id_entity_child=int(id_entity),
                time_from=str(row.get("time_from") or ""),
                time_to=time_to,
            )
        )
    return DeviceTimeline(int(id_entity), joins)


# ---------------------------------------------------------------------------
# Fleet-wide gap report (synthesis plan §5 step 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FleetGapDevice:
    """One device row in the fleet-gap report.

    ``gaps`` is the list of :class:`Gap` instances surviving the
    ``min_days`` filter. ``is_truly_orphan`` mirrors
    :attr:`DeviceTimeline.is_truly_orphan` (joins exist but none open).
    The ``subtype`` / ``serial`` / ``model`` fields are populated only
    when ``enrich=True`` was passed to :func:`scan_fleet_gaps`; otherwise
    they remain ``None`` (the report is still useful — operators can
    follow up with ``tos audit device --id <n>``).

    ``last_parent_id`` / ``last_parent_name`` describe where a truly-orphan
    device was last attached (the ``time_to`` of its most-recent closed
    join). They're ``None`` for devices that have at least one open join.
    """

    id_entity: int
    gaps: List[Gap]
    is_truly_orphan: bool
    subtype: Optional[str] = None
    serial: Optional[str] = None
    model: Optional[str] = None
    last_parent_id: Optional[int] = None
    last_parent_name: Optional[str] = None
    # Populated only when scan_fleet_gaps(with_timelines=True). Carries
    # the full join history of the device so a single invocation drives
    # both the gap surface and the drill-down — no second index walk.
    timeline: Optional["DeviceTimelineReport"] = None

    @property
    def max_gap_days(self) -> float:
        """Longest gap duration, or 0 when there are no gaps."""
        return max((g.duration_days for g in self.gaps), default=0.0)


@dataclass
class FleetGapReport:
    """Result of a fleet-wide gap scan.

    A read-only snapshot. The ``devices`` list contains every device that
    matched the inclusion rules (gap above threshold or, if requested,
    truly-orphan), sorted by longest gap descending then by ``id_entity``.
    Counts are pre-computed properties so renderers don't have to reduce.

    ``parent_names`` is populated when ``scan_fleet_gaps(with_timelines=
    True)`` was called, so renderers showing the embedded timelines can
    label parents without re-querying. Empty dict when timelines were
    not requested.
    """

    min_days: float
    parents_walked: int
    parents_failed: int
    total_joins: int
    total_devices: int
    devices: List[FleetGapDevice]
    parent_names: Dict[int, Optional[str]] = field(default_factory=dict)

    @property
    def gap_count(self) -> int:
        return sum(len(d.gaps) for d in self.devices)

    @property
    def devices_with_gaps(self) -> int:
        return sum(1 for d in self.devices if d.gaps)

    @property
    def orphan_count(self) -> int:
        return sum(1 for d in self.devices if d.is_truly_orphan)


def _open_attribute_value(
    attributes: Optional[List[Dict[str, Any]]], code: str
) -> Optional[str]:
    """Return the value of the open attribute period for *code*, or None.

    TOS attribute periods carry ``date_to`` (not ``time_to``; that's for
    connections). The open period is the one with ``date_to is None``.
    Mirrors :func:`tostools.audit._open_attr_value` to avoid the
    cross-module import.
    """
    for attr in attributes or []:
        if attr.get("code") != code:
            continue
        if attr.get("date_to") is not None:
            continue
        value = attr.get("value")
        if value is not None:
            return str(value)
    return None


def _enrich_device(
    client: TOSClient,
    id_entity: int,
    parent_names: Dict[int, Optional[str]],
    timeline: DeviceTimeline,
) -> Dict[str, Any]:
    """Fetch a device's subtype / serial / model and last-parent name.

    One ``get_entity_history`` call per device; meant for the small
    set of devices that actually appear in the report (gaps + orphans).
    Returns a dict suitable for splatting into :class:`FleetGapDevice`.
    Best-effort: a missing or unreadable entity yields ``None`` fields.
    """
    out: Dict[str, Any] = {
        "subtype": None,
        "serial": None,
        "model": None,
        "last_parent_id": None,
        "last_parent_name": None,
    }
    try:
        history = client.get_entity_history(id_entity)
    except Exception as exc:  # network errors, transient TOS failures
        logger.warning(
            "scan_fleet_gaps: enrich %d raised: %s; reporting unenriched",
            id_entity,
            exc,
        )
        history = None
    if history:
        out["subtype"] = history.get("code_entity_subtype") or None
        attrs = history.get("attributes") or []
        out["serial"] = _open_attribute_value(attrs, "serial_number")
        out["model"] = _open_attribute_value(attrs, "model")

    if not timeline.is_currently_attached and timeline.closed_joins:
        last = max(
            timeline.closed_joins,
            key=lambda j: j.time_to or "",
        )
        out["last_parent_id"] = last.id_entity_parent
        out["last_parent_name"] = parent_names.get(last.id_entity_parent)
    return out


def scan_fleet_gaps(
    client: TOSClient,
    *,
    min_days: float = 30.0,
    include_orphans: bool = True,
    enrich: bool = True,
    subtype: Optional[str] = None,
    parents: Optional[Iterable[ParentEntity]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    enumerate_progress: Optional[Callable[[int, int], None]] = None,
    with_timelines: bool = False,
) -> FleetGapReport:
    """Walk the fleet via :func:`build_join_index` and report gaps + orphans.

    The output is the answer to the user's "are there gaps where
    receivers are not accounted for?" question. Pure-read, no auth.

    Args:
        client: An unauthenticated :class:`TOSClient`.
        min_days: Minimum gap duration to surface. Empirical guidance
            from the 2026-05-12 fleet probe: ≥30 returns the actionable
            tail (~50 devices), ≥365 the high-confidence subset (~40).
            Below ~7 the result set is dominated by date-rounding
            artifacts.
        include_orphans: Also report devices whose timeline is non-empty
            but has no open join (the audit's I1-orphan signal, derived
            from the index rather than from ``id_entity_parent``).
        enrich: Fetch subtype/serial/model for each reported device via
            one ``get_entity_history`` call apiece (~50–100 extra calls
            for a typical fleet). Required when ``subtype`` filters.
            Disable for the fastest report — operators can follow up
            with ``tos audit device --id <n>``.
        subtype: When set, retain only devices whose
            ``code_entity_subtype`` matches. Requires ``enrich=True``.
        parents: Override the parent enumeration. ``None`` defaults to
            :func:`enumerate_known_parents` augmented with
            :data:`KNOWN_MISSING_FROM_CFG_PARENT_IDS` so the four parents
            known to be absent from ``stations.cfg`` (Fagradalsfjall,
            Bláfjöll, Bárðabunga, Hestalda) don't generate phantom gaps.
        progress: Forwarded to :func:`build_join_index`. Useful for
            ``tos audit fleet-gaps`` to show "walking parent N/M".
        enumerate_progress: Forwarded to :func:`enumerate_known_parents`
            (only used when ``parents`` is None). The marker-resolution
            loop dominates wall-clock for a default run (~100s for ~191
            markers) — surface it so the operator sees the slow step.
        with_timelines: When True, attach the full per-device
            :class:`DeviceTimelineReport` to each row's ``timeline``
            field and populate ``report.parent_names``. Reuses the same
            join index that fleet-gaps already built (no second walk).
            This is the drill-down companion mode — operators can see
            both the surfaced gap and the surrounding join history in
            a single invocation. Implies ``enrich=True`` for the
            timeline's own metadata population.

    Returns:
        :class:`FleetGapReport`. Devices sorted by longest gap descending,
        then by ``id_entity`` for stability. Truly-orphan devices with
        no gaps still appear when ``include_orphans=True``; they sort
        below all gap-bearing rows because their ``max_gap_days`` is 0.
    """
    if subtype is not None and not enrich:
        raise ValueError("scan_fleet_gaps: subtype filter requires enrich=True")
    if with_timelines and not enrich:
        # Timelines carry per-device metadata; without enrichment they'd
        # have None subtype/serial/model. Keep the contract tight.
        raise ValueError("scan_fleet_gaps: with_timelines=True requires enrich=True")

    if parents is None:
        parents = enumerate_known_parents(
            client,
            extra_parent_ids=KNOWN_MISSING_FROM_CFG_PARENT_IDS,
            progress=enumerate_progress,
        )
    parent_list = list(parents)
    parent_names: Dict[int, Optional[str]] = {p.id_entity: p.name for p in parent_list}

    index = build_join_index(client, parents=parent_list, progress=progress)

    rows: List[FleetGapDevice] = []
    for did in index.device_ids:
        tl = index.timeline(did)
        gaps = tl.gaps(min_days=min_days)
        is_orphan = tl.is_truly_orphan
        if not gaps and not (include_orphans and is_orphan):
            continue

        meta: Dict[str, Any] = {
            "subtype": None,
            "serial": None,
            "model": None,
            "last_parent_id": None,
            "last_parent_name": None,
        }
        if enrich:
            meta = _enrich_device(client, did, parent_names, tl)
            if subtype is not None and meta["subtype"] != subtype:
                continue
        elif is_orphan and tl.closed_joins:
            # Cheap last-parent name without enrichment — the index
            # already tells us where the device was last seen.
            last = max(tl.closed_joins, key=lambda j: j.time_to or "")
            meta["last_parent_id"] = last.id_entity_parent
            meta["last_parent_name"] = parent_names.get(last.id_entity_parent)

        timeline_report = None
        if with_timelines:
            timeline_report = DeviceTimelineReport(
                id_entity=did,
                subtype=meta["subtype"],
                serial=meta["serial"],
                model=meta["model"],
                is_currently_attached=tl.is_currently_attached,
                is_truly_orphan=is_orphan,
                joins=list(tl.joins),
                # Full history view: surface every gap, not just the
                # min_days-filtered ones used for the headline row.
                gaps=tl.gaps(min_days=0.0),
            )

        rows.append(
            FleetGapDevice(
                id_entity=did,
                gaps=gaps,
                is_truly_orphan=is_orphan,
                **meta,
                timeline=timeline_report,
            )
        )

    rows.sort(key=lambda d: (-d.max_gap_days, d.id_entity))

    return FleetGapReport(
        min_days=min_days,
        parents_walked=index.parents_walked,
        parents_failed=index.parents_failed,
        total_joins=index.total_joins,
        total_devices=len(index.device_ids),
        devices=rows,
        parent_names=parent_names if with_timelines else {},
    )


# ---------------------------------------------------------------------------
# Per-device timeline report (synthesis plan §3: tos audit timeline)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceTimelineReport:
    """One device's complete join history, enriched with metadata.

    ``joins`` is the chronologically-sorted list of every join the
    device has ever had (open or closed). ``gaps`` is the filtered list
    of coverage gaps from :meth:`DeviceTimeline.gaps`. Metadata fields
    (``subtype`` / ``serial`` / ``model``) are populated only when
    ``enrich=True`` was passed to :func:`get_device_timelines`.

    A device with no joins indexed (e.g. an id that doesn't exist or
    wasn't reachable from any walked parent) still produces a report —
    the report just has ``joins=[]``. The CLI surfaces that as
    "no joins indexed" rather than failing.
    """

    id_entity: int
    subtype: Optional[str]
    serial: Optional[str]
    model: Optional[str]
    is_currently_attached: bool
    is_truly_orphan: bool
    joins: List[Join]
    gaps: List[Gap]


@dataclass
class TimelinesReport:
    """Result of :func:`get_device_timelines` — one entry per requested id.

    ``parent_names`` maps parent ``id_entity`` to the open ``name``
    attribute (or ``None`` when TOS doesn't carry one), so renderers
    can label parents without re-querying. Built from the walked
    parent list.
    """

    parents_walked: int
    parents_failed: int
    total_joins: int
    total_devices: int
    parent_names: Dict[int, Optional[str]]
    timelines: List[DeviceTimelineReport]


def get_device_timelines(
    client: TOSClient,
    ids: Iterable[int],
    *,
    min_gap_days: float = 0.0,
    enrich: bool = True,
    parents: Optional[Iterable[ParentEntity]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    enumerate_progress: Optional[Callable[[int, int], None]] = None,
) -> TimelinesReport:
    """Return per-device full timelines for the given device ids.

    Builds the global join index once (the same ~110s cost as
    :func:`scan_fleet_gaps`) and looks up each id with O(1) per id. The
    index build is amortised across all ids in a single invocation —
    pass every id of interest in one call.

    Args:
        client: An unauthenticated :class:`TOSClient`.
        ids: Iterable of device ``id_entity`` values to report on. Ids
            with no indexed joins still produce a report row (with
            empty ``joins``), which lets the CLI tell "not in index"
            apart from "lookup failed".
        min_gap_days: Threshold passed to :meth:`DeviceTimeline.gaps`.
            Default 0 surfaces *every* gap regardless of duration —
            timeline view normally wants the full picture, unlike the
            fleet-gap view which filters noise.
        enrich: Fetch subtype/serial/model per device. One
            :meth:`TOSClient.get_entity_history` call per id, so the
            cost is linear in ``len(ids)``.
        parents: Override the parent enumeration. ``None`` defaults to
            :func:`enumerate_known_parents` augmented with
            :data:`KNOWN_MISSING_FROM_CFG_PARENT_IDS`.
        progress / enumerate_progress: Forwarded to the index build
            and parent enumeration respectively. Same semantics as
            :func:`scan_fleet_gaps`.

    Returns:
        :class:`TimelinesReport`. ``timelines`` is in the order the ids
        were requested (deduplicated, first occurrence wins).
    """
    if parents is None:
        parents = enumerate_known_parents(
            client,
            extra_parent_ids=KNOWN_MISSING_FROM_CFG_PARENT_IDS,
            progress=enumerate_progress,
        )
    parent_list = list(parents)
    parent_names: Dict[int, Optional[str]] = {p.id_entity: p.name for p in parent_list}

    index = build_join_index(client, parents=parent_list, progress=progress)

    seen: set[int] = set()
    ordered_ids: List[int] = []
    for raw in ids:
        did = int(raw)
        if did in seen:
            continue
        seen.add(did)
        ordered_ids.append(did)

    timelines: List[DeviceTimelineReport] = []
    for did in ordered_ids:
        tl = index.timeline(did)
        meta: Dict[str, Any] = {
            "subtype": None,
            "serial": None,
            "model": None,
        }
        if enrich:
            enriched = _enrich_device(client, did, parent_names, tl)
            meta["subtype"] = enriched["subtype"]
            meta["serial"] = enriched["serial"]
            meta["model"] = enriched["model"]
        timelines.append(
            DeviceTimelineReport(
                id_entity=did,
                subtype=meta["subtype"],
                serial=meta["serial"],
                model=meta["model"],
                is_currently_attached=tl.is_currently_attached,
                is_truly_orphan=tl.is_truly_orphan,
                joins=list(tl.joins),
                gaps=tl.gaps(min_days=min_gap_days),
            )
        )

    return TimelinesReport(
        parents_walked=index.parents_walked,
        parents_failed=index.parents_failed,
        total_joins=index.total_joins,
        total_devices=len(index.device_ids),
        parent_names=parent_names,
        timelines=timelines,
    )
