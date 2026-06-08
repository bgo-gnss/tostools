"""Helpers for the ``tos station add`` CLI — the ``geophysical`` station entity.

A "station" in TOS is an entity with ``code_entity_subtype="geophysical"``
(``entity_type=station``) — the GPS/SIL/gas/infrasound measurement station. It
must hang under a ``land`` site (see :mod:`tostools.location`) and carries the
GPS attribute set (marker, operational_class, bedrock_*, …).

This module shapes + validates the station's required attributes so the logic
is unit-testable without argparse or the TOS API. The required set and the
per-code default values are read from ``data/attribute_codes.yaml`` (the
``stations`` scope, ``required_for`` containing ``geophysical``) so the verb
stays in sync with the catalog the audits use — no duplicated constant.

The user-facing entrypoint is ``tostools.tos._station_add_main``.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit_attribute_dates import load_catalog_scoped
from .device import normalize_date_start  # re-export for callers
from .location import validate_altitude, validate_latitude, validate_longitude

__all__ = [
    "STATION_SUBTYPE",
    "station_required_codes",
    "build_required_station_attributes",
    "normalize_date_start",
    "validate_latitude",
    "validate_longitude",
    "validate_altitude",
]

# TOS ``code_entity_subtype`` for a GPS/geophysical station (id 111 in
# ``GET /entity_subtypes/``, entity_type=station).
STATION_SUBTYPE = "geophysical"

# Codes the CLI validates as coordinates before shaping.
_COORD_VALIDATORS = {
    "lat": validate_latitude,
    "lon": validate_longitude,
    "altitude": validate_altitude,
}


def station_required_codes(
    catalog_path: Optional[Path] = None,
) -> "OrderedDict[str, Optional[str]]":
    """Return ``{code: default_value}`` for every geophysical-required station attr.

    Reads the ``stations`` scope of ``attribute_codes.yaml`` and keeps each
    code whose ``gps_required_for`` contains ``"geophysical"``. Uses
    ``gps_required_for`` (not ``tos_required_for``) to match the
    missing-attributes audit (``audit_missing_attributes`` keys on the same
    field) — so a station built here satisfies ``tos station verify``.
    ``default_value`` is the catalog default (``None`` when the operator must
    supply it). Insertion order follows the catalog.
    """
    scoped = load_catalog_scoped(catalog_path)
    stations = scoped.get("stations", {})
    out: "OrderedDict[str, Optional[str]]" = OrderedDict()
    for code, entry in stations.items():
        required = entry.get("gps_required_for") or []
        if "geophysical" in required:
            default = entry.get("default_value")
            # YAML ``~`` → None; normalise empty string to None too.
            out[code] = default if (default is not None and default != "") else None
    return out


def build_required_station_attributes(
    *,
    provided: Dict[str, Optional[str]],
    date_start: str,
    catalog_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Shape the attribute list for ``create_entity("geophysical", ...)``.

    For each geophysical-required code, the value is taken from *provided*
    (operator input) when present, else the catalog default. The ``date_start``
    code's value is *date_start* itself. Coordinate codes (lat/lon/altitude)
    are validated. Every attribute carries ``date_from=date_start`` and an
    explicit ``date_to=None`` (open period).

    Args:
        provided: ``{code: value}`` from the CLI. A missing or ``None`` value
            falls back to the catalog default.
        date_start: the ``date_from`` for every row, and the value of the
            ``date_start`` attribute.
        catalog_path: override for the catalog (tests / alternate networks).

    Raises:
        ValueError: a required code has neither a provided value nor a catalog
            default (all such codes reported together), or a coordinate is
            invalid.
    """
    required = station_required_codes(catalog_path)
    attrs: List[Dict[str, Any]] = []
    missing: List[str] = []

    for code, default in required.items():
        if code == "date_start":
            value: Optional[str] = date_start
        else:
            value = provided.get(code)
            if value is None or value == "":
                value = default

        if value is None or value == "":
            missing.append(code)
            continue

        if code == "marker":
            # TOS stores markers lowercase (e.g. "hedi") — normalise so a new
            # station matches the fleet convention and is found by
            # find_station_by_marker. See its docstring.
            value = str(value).lower()

        if code in _COORD_VALIDATORS:
            value = _COORD_VALIDATORS[code](value)  # raises ValueError if bad

        attrs.append(
            {"code": code, "value": value, "date_from": date_start, "date_to": None}
        )

    if missing:
        raise ValueError(
            "missing required station attribute(s) with no value and no "
            "catalog default: " + ", ".join(missing)
        )
    return attrs
