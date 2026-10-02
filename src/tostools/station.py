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
    "KNOWN_STATION_SUBTYPES",
    "fold_subtype",
    "observed_station_subtypes",
    "resolve_station_subtype",
]

# TOS ``code_entity_subtype`` for a GPS/geophysical station (id 111 in
# ``GET /entity_subtypes/``, entity_type=station).
STATION_SUBTYPE = "geophysical"

#: The ``subtype`` values the geophysical fleet actually uses, observed
#: 2026-10-02 across all 445 geophysical stations (``GPS stöð`` 219,
#: ``SIL stöð`` 142, the rest in single or low double figures).
#:
#: TOS does NOT constrain this attribute — ``/admin_attribute_rows`` gives it
#: ``python_constraint: ".*"`` and ``autocomplete: true``, so the web UI
#: completes from whatever is already in the database. The fleet's values
#: therefore ARE the vocabulary, and there is no authoritative enum to read.
#:
#: This list is the offline FLOOR; :func:`observed_station_subtypes` refreshes
#: it from the live fleet when a value is not recognised. Being out of date
#: can only cost a needless live lookup, never a wrong refusal.
KNOWN_STATION_SUBTYPES = (
    "GPS stöð",
    "SIL stöð",
    "Móða",
    "SRS stöð",
    "DOAS",
    "Crowcon",
    "Infrasound",
    "Tengistöð",
    "Jarðhitavöktun",
    "Þenslumælistöð",
    "Multigas",
    "Togmælastöð",
    "Endurvarpi",
    "DissolvedCO2",
    "Alstöð",
    "Insar",
)

#: Icelandic letters that are not accented vowels, so NFKD leaves them alone.
#: Folding them is the whole point of this comparison: a terminal or keyboard
#: without the Icelandic layout types "GPS stod", and that must resolve to
#: "GPS stöð" rather than silently create an 18th subtype that every GPS verb
#: then treats as not found.
_FOLD_PAIRS = (
    ("ð", "d"),
    ("þ", "th"),
    ("æ", "ae"),
    ("ø", "o"),
)


def fold_subtype(value: str) -> str:
    """Casefold, strip, and remove Icelandic diacritics from ``value``.

    Used only for COMPARISON — the canonical spelling is always what gets
    written to TOS. Stripping also repairs real data: one station carries
    ``'SRS stöð\t'``, with a trailing tab.
    """
    import unicodedata

    out = (value or "").strip().casefold()
    for src, dst in _FOLD_PAIRS:
        out = out.replace(src, dst)
    out = unicodedata.normalize("NFKD", out)
    out = "".join(ch for ch in out if not unicodedata.combining(ch))
    return " ".join(out.split())


def observed_station_subtypes(client: Any, domain: str = STATION_SUBTYPE) -> List[str]:
    """Distinct open ``subtype`` values across ``domain``'s stations.

    One bulk call (:meth:`TOSClient.list_stations`). Whitespace-stripped and
    deduplicated, so the ``'SRS stöð\t'`` row does not present itself as a
    separate option.
    """
    seen: List[str] = []
    for row in client.list_stations(domain) or []:
        for attr in row.get("attributes") or []:
            if attr.get("code") != "subtype" or attr.get("date_to") is not None:
                continue
            value = (attr.get("value") or "").strip()
            if value and value not in seen:
                seen.append(value)
    return seen


def resolve_station_subtype(
    requested: str, known: "Any" = KNOWN_STATION_SUBTYPES
) -> Optional[str]:
    """Map ``requested`` onto ``known``'s spelling, or ``None``.

    Three rungs, each stricter about what it will forgive than the next is:

    1. exact
    2. case- and whitespace-insensitive
    3. **diacritic-folded** — ``"GPS stod"`` resolves to ``"GPS stöð"``

    Rung 3 exists because the alternative is silent and permanent. TOS does
    not constrain this attribute, so a mistyped subtype is accepted and
    creates a station that every GPS verb then treats as NOT FOUND: the
    station filter is lenient about an ABSENT subtype, never a wrong one.

    Returns ``None`` when nothing matches, and also when two known values
    fold together — guessing between them would be the same mistake the
    station filter refuses to make.
    """
    if not requested:
        return None
    candidates = list(known)
    for value in candidates:
        if requested == value:
            return value
    target = (requested or "").strip().casefold()
    for value in candidates:
        if target == value.strip().casefold():
            return value
    folded = fold_subtype(requested)
    matches = [v for v in candidates if fold_subtype(v) == folded]
    # Dedupe by canonical spelling: 'SRS stöð' and 'SRS stöð\t' are one value.
    unique = []
    for value in matches:
        if value.strip() not in [u.strip() for u in unique]:
            unique.append(value)
    if len(unique) == 1:
        return unique[0].strip()
    return None


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
