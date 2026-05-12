"""Device-history-reconstruction primitives (step 2 of the synthesis plan).

This module owns the read-only primitives used to walk and reconstruct the
full join history of GNSS devices in TOS. The first deliverable here is
:func:`enumerate_known_parents` — the "bootstrap parent list" needed by the
forthcoming :func:`build_join_index` (step 3 of the synthesis).

Why a separate module
---------------------
TOS exposes ``children_connections`` only from the parent side; a device's
own ``id_entity_parent`` attribute can be stale (see GitHub issue #17, fixed
in PR #18). So to reconstruct a device's complete timeline we have to walk
every parent the device might have touched. That requires knowing the parent
set up front.

The synthesis note
``~/notes/bgovault/2.Areas/VI_GPS_Library/1778612553-tostools-history-reconstruction-leverage.md``
captures the full design (§2.3 for the join index, §5 step 2 for this
enumeration).

What this module is NOT
-----------------------
- Not a writer (no auth needed).
- Not a substitute for ``tostools.audit``; the audit module continues to
  serve the I1/I2 invariant-check CLI. Both modules read TOS but answer
  different questions.
- Not a replacement for any ``tosGPS`` functionality. Pure addition.

API surface
-----------
* :class:`ParentEntity` — frozen dataclass describing one parent.
* :data:`KNOWN_INFRASTRUCTURE_IDS` — hardcoded entity IDs for the warehouse
  network + graveyard. Stable across deployments.
* :func:`enumerate_known_parents` — top-level entry point. Returns the
  bootstrap parent list (infrastructure + stations from ``stations.cfg``,
  optionally augmented).
"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

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
