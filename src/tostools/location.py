"""Helpers for the ``tos location add`` CLI — the ``land`` site entity.

A "location" in TOS is an entity with ``code_entity_subtype="land"``
(``entity_type=location``). TOS describes it as the *"land-based location of
one or multiple colocated stations — required parent of any land station in
regular operations."* It carries only the physical-site attributes
(``name``, ``lat``, ``lon``, ``altitude``) and is the parent that GPS / SIL /
other geophysical stations hang under via an entity_connection.

This module holds the attribute-shaping and coordinate-validation logic so it
can be unit-tested without argparse or the TOS API. The user-facing entrypoint
is ``tostools.tos._location_add_main``.

**Reuse over duplication is the common path.** A new GPS station is frequently
added at a site that *already* hosts another instrument — most often a SIL
seismic station — so the ``land`` site usually already exists and must be
reused, not duplicated. ``TOSWriter.find_land_location_by_name`` is the reuse
lookup; the CLI treats "already exists" as a friendly idempotent result, not
an error.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .device import normalize_date_start  # re-export for callers
from .devices import open_attribute

__all__ = [
    "LOCATION_SUBTYPE",
    "LOCATION_REQUIRED_ATTR_CODES",
    "LOCATION_OPTIONAL_ATTR_CODES",
    "normalize_date_start",
    "validate_latitude",
    "validate_longitude",
    "validate_altitude",
    "build_location_attributes",
    "summarize_location_children",
]

# The TOS ``code_entity_subtype`` for a land site (id 103 in
# ``GET /entity_subtypes/``, entity_type=location).
LOCATION_SUBTYPE = "land"

# Catalog ``locations`` scope, ``required_for == ["land"]`` (the catalog key is
# being corrected from the stale ``staðsetning`` as part of this work).
LOCATION_REQUIRED_ATTR_CODES = ("name", "lat", "lon", "altitude")

# Order drives the attribute order in :func:`build_location_attributes`.
LOCATION_OPTIONAL_ATTR_CODES = ("lon_isn93", "lat_isn93", "identifier", "notes")


def _validate_float(label: str, raw: str) -> str:
    """Parse *raw* as a float and return it unchanged (TOS stores strings).

    Raises ``ValueError`` with a *label*-prefixed message on a non-numeric
    value, so the CLI can surface which coordinate was bad.
    """
    if raw is None or str(raw).strip() == "":
        raise ValueError(f"{label} must be a non-empty number")
    try:
        float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be numeric, got {raw!r}") from None
    return str(raw)


def validate_latitude(raw: str) -> str:
    """Validate a latitude in decimal degrees (``-90 ≤ lat ≤ 90``)."""
    value = _validate_float("lat", raw)
    if not -90.0 <= float(value) <= 90.0:
        raise ValueError(f"lat must be between -90 and 90, got {raw!r}")
    return value


def validate_longitude(raw: str) -> str:
    """Validate a longitude in decimal degrees (``-180 ≤ lon ≤ 180``)."""
    value = _validate_float("lon", raw)
    if not -180.0 <= float(value) <= 180.0:
        raise ValueError(f"lon must be between -180 and 180, got {raw!r}")
    return value


def validate_altitude(raw: str) -> str:
    """Validate an altitude in metres (any finite number)."""
    return _validate_float("altitude", raw)


def build_location_attributes(
    *,
    name: str,
    lat: str,
    lon: str,
    altitude: str,
    date_start: str,
    lon_isn93: Optional[str] = None,
    lat_isn93: Optional[str] = None,
    identifier: Optional[str] = None,
    notes: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Shape the attribute list for ``create_entity("land", ...)``.

    Every attribute value (required + provided optionals) carries
    ``date_from=date_start`` and an explicit ``date_to=None`` (open period) —
    the same shape the TOS ``/entities`` endpoint expects, matching
    :func:`tostools.device.build_required_attributes`.

    Coordinates are validated (numeric + range) before shaping. ``name`` must
    be non-empty. Optionals are emitted only when provided, in
    :data:`LOCATION_OPTIONAL_ATTR_CODES` order.

    Raises:
        ValueError: empty ``name`` or invalid coordinate.
    """
    if not name or not str(name).strip():
        raise ValueError("name must be a non-empty string")

    attrs: List[Dict[str, Any]] = [
        {"code": "name", "value": name, "date_from": date_start, "date_to": None},
        {
            "code": "lat",
            "value": validate_latitude(lat),
            "date_from": date_start,
            "date_to": None,
        },
        {
            "code": "lon",
            "value": validate_longitude(lon),
            "date_from": date_start,
            "date_to": None,
        },
        {
            "code": "altitude",
            "value": validate_altitude(altitude),
            "date_from": date_start,
            "date_to": None,
        },
    ]

    optionals = {
        "lon_isn93": lon_isn93,
        "lat_isn93": lat_isn93,
        "identifier": identifier,
        "notes": notes,
    }
    for code in LOCATION_OPTIONAL_ATTR_CODES:
        value = optionals.get(code)
        if value is not None and str(value) != "":
            attrs.append(
                {
                    "code": code,
                    "value": value,
                    "date_from": date_start,
                    "date_to": None,
                }
            )
    return attrs


def summarize_location_children(
    writer: Any,
    location_id: int,
    *,
    open_only: bool = True,
) -> List[Dict[str, Any]]:
    """Summarise the stations/devices attached to a ``land`` site.

    Used to answer "what's already here?" — the common case is a site that
    already hosts a SIL seismic station (or another GPS station) that an
    operator is now colocating GPS with. Both ``tos location add`` (show the
    existing site) and ``tos station add`` (show the site being attached to)
    render this.

    For each child connection it resolves the child's ``code_entity_subtype``,
    its ``subtype`` attribute (the human label — ``"GPS stöð"`` /
    ``"SIL stöð"`` — that distinguishes GPS from SIL even though both are
    ``geophysical``), its ``name``, and the join's ``time_from`` / ``time_to``.

    Args:
        writer: any object exposing ``get_entity_history(id)`` (``TOSWriter``
            or ``TOSClient``).
        location_id: the ``land`` site's ``id_entity``.
        open_only: when ``True`` (default), only currently-open joins
            (``time_to is None``) are returned.

    Returns:
        A list of dicts (most-recent ``time_from`` first):
        ``{id_entity, code_entity_subtype, subtype, name, time_from,
        time_to, open}``. Empty list when the site has no children or its
        history can't be fetched.
    """
    history = writer.get_entity_history(location_id)
    if not history:
        return []
    children: List[Dict[str, Any]] = []
    for conn in history.get("children_connections") or []:
        time_to = conn.get("time_to")
        if open_only and time_to is not None:
            continue
        child_id = conn.get("id_entity_child")
        if child_id is None:
            continue
        child_hist = writer.get_entity_history(int(child_id)) or {}
        children.append(
            {
                "id_entity": int(child_id),
                "code_entity_subtype": child_hist.get("code_entity_subtype"),
                "subtype": open_attribute(child_hist, "subtype"),
                "name": open_attribute(child_hist, "name"),
                "time_from": conn.get("time_from"),
                "time_to": time_to,
                "open": time_to is None,
            }
        )
    children.sort(key=lambda c: c.get("time_from") or "", reverse=True)
    return children
