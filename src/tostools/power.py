"""Power-infrastructure helpers — shared-power model (W1: surface site power).

Power at a GPS/seismic site is physically a **site-level** resource: colocated
stations share one supply (e.g. HEDI's GPS + SIL both run off the nearby farm's
mains). TOS, however, currently models power **per-station** — battery / solar /
power-pack devices hang off the individual station entity, never off the
shared ``land`` site. See ``docs/architecture/shared-power-model.md``.

This module is W1 of that work: a **read-only** aggregator that surfaces the
power already present at a site by walking the colocated stations, so an
operator reusing a site sees the shared supply and does not add a duplicate.
The W2 audit (`tos audit shared-power`) and W3 migration (reparent power
station→site) will build on the same :data:`POWER_DEVICE_SUBTYPES` set.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .devices import open_attribute

__all__ = [
    "POWER_DEVICE_SUBTYPES",
    "SENSOR_TIED_POWER_SUBTYPES",
    "summarize_site_power",
]

# TOS device subtypes that represent power infrastructure (entity_type=device),
# from ``GET /entity_subtypes/``. The shared-supply members of a site.
POWER_DEVICE_SUBTYPES = frozenset(
    {
        "battery",
        "solar_panel",
        "power_pack",
        "charge_regulator",
        "charger",
        "solar_charge_controller",
        "ups",
        "power_generator",
        "switch_poe",
        "anemometer_power_pack",
    }
)

# Power that is tied to a single instrument rather than the site supply — must
# NOT be treated as site-shared by the W2 audit. Surfaced by W1 (with its
# station context) but flagged here so downstream consumers can exclude it.
SENSOR_TIED_POWER_SUBTYPES = frozenset({"anemometer_power_pack"})


def _power_row(
    id_entity: int,
    history: Dict[str, Any],
    *,
    on_station: Optional[int],
    on_station_name: Optional[str],
    time_from: Optional[str],
    open_: bool,
) -> Dict[str, Any]:
    subtype = history.get("code_entity_subtype")
    return {
        "id_entity": id_entity,
        "subtype": subtype,
        "model": open_attribute(history, "model"),
        "serial": open_attribute(history, "serial_number"),
        # Where the device currently lives: a colocated station's id, or None
        # when it hangs directly off the `land` site (the W3 target state).
        "on_station": on_station,
        "on_station_name": on_station_name,
        "sensor_tied": subtype in SENSOR_TIED_POWER_SUBTYPES,
        "time_from": time_from,
        "open": open_,
    }


def summarize_site_power(
    writer: Any,
    land_id: int,
    *,
    open_only: bool = True,
) -> List[Dict[str, Any]]:
    """Aggregate the power devices present at a ``land`` site.

    Walks the site's children (the colocated stations) and, for each, its
    power-device children — because today power lives on the stations, not on
    the site. Also picks up any power device joined **directly** to the
    ``land`` site (``on_station=None``), which is the W3 migration target
    state, so this helper keeps working as power moves site-ward.

    Args:
        writer: any object exposing ``get_entity_history(id)`` (``TOSWriter``
            or ``TOSClient``).
        land_id: the ``land`` site's ``id_entity``.
        open_only: when ``True`` (default), only currently-open joins are
            considered (both the station→site join and the power→station join).

    Returns:
        One row per power device (sorted site-direct first, then by station,
        then most-recent ``time_from``):
        ``{id_entity, subtype, model, serial, on_station, on_station_name,
        sensor_tied, time_from, open}``. Empty list when the site has no
        power anywhere or its history can't be fetched.
    """
    land = writer.get_entity_history(land_id)
    if not land:
        return []

    rows: List[Dict[str, Any]] = []
    for conn in land.get("children_connections") or []:
        if open_only and conn.get("time_to") is not None:
            continue
        child_id = conn.get("id_entity_child")
        if child_id is None:
            continue
        child = writer.get_entity_history(int(child_id)) or {}
        child_subtype = child.get("code_entity_subtype")

        if child_subtype in POWER_DEVICE_SUBTYPES:
            # Power joined directly to the site (W3 target state).
            rows.append(
                _power_row(
                    int(child_id),
                    child,
                    on_station=None,
                    on_station_name=None,
                    time_from=conn.get("time_from"),
                    open_=conn.get("time_to") is None,
                )
            )
            continue

        # Otherwise treat the child as a colocated station and look for power
        # among ITS children (the current per-station modelling).
        station_name = open_attribute(child, "name")
        for sub_conn in child.get("children_connections") or []:
            if open_only and sub_conn.get("time_to") is not None:
                continue
            power_id = sub_conn.get("id_entity_child")
            if power_id is None:
                continue
            power = writer.get_entity_history(int(power_id)) or {}
            if power.get("code_entity_subtype") not in POWER_DEVICE_SUBTYPES:
                continue
            rows.append(
                _power_row(
                    int(power_id),
                    power,
                    on_station=int(child_id),
                    on_station_name=station_name,
                    time_from=sub_conn.get("time_from"),
                    open_=sub_conn.get("time_to") is None,
                )
            )

    # Site-direct power first (on_station None sorts before any int), then by
    # station id, then most-recent first.
    rows.sort(
        key=lambda r: (
            r["on_station"] is not None,
            r["on_station"] or 0,
            r.get("time_from") or "",
        ),
    )
    return rows
