"""Unit tests for :mod:`tostools.history`.

Covers the bootstrap-parent-list enumeration. TOSClient is mocked; no
network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from tostools.history import (
    DEVICE_GRAVEYARD_ID,
    KNOWN_INFRASTRUCTURE_IDS,
    KNOWN_WAREHOUSE_IDS,
    STATION_SUBTYPES,
    ParentEntity,
    default_station_cfg_path,
    enumerate_known_parents,
    read_station_markers,
    resolve_marker_to_entity_id,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _history(
    id_entity: int,
    subtype: str = "geophysical",
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a minimal ``/history/entity/<id>/`` response."""
    attrs: List[Dict[str, Any]] = []
    if name is not None:
        attrs.append({"code": "name", "value_varchar": name, "time_to": None})
    return {
        "id_entity": id_entity,
        "code_entity_subtype": subtype,
        "attributes": attrs,
        "children_connections": [],
    }


def _marker_hit(
    marker: str,
    id_entity: int,
    *,
    distance: int = 0,
    name_lvl_two: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a basic_search hit with ``code='marker'``."""
    return {
        "code": "marker",
        "distance": distance,
        "value_varchar": marker,
        "id_entity": id_entity,
        "id_lvl_three": id_entity,
        "name_lvl_two": name_lvl_two,
    }


def _client(
    histories: Dict[int, Dict[str, Any]],
    search_hits: Optional[List[Dict[str, Any]]] = None,
) -> MagicMock:
    c = MagicMock()
    c.get_entity_history.side_effect = lambda i: histories.get(int(i))
    c.basic_search.return_value = list(search_hits or [])
    return c


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_known_infrastructure_includes_warehouses_and_graveyard():
    """Sanity: the infrastructure tuple is the warehouse tuple + graveyard."""
    assert set(KNOWN_INFRASTRUCTURE_IDS) == set(KNOWN_WAREHOUSE_IDS) | {
        DEVICE_GRAVEYARD_ID
    }


def test_b9_jord_is_id_4():
    """The primary GPS warehouse must remain id_entity=4 (B9 - Kjallari - Jörð).
    Audit module and existing tooling depend on this constant."""
    assert 4 in KNOWN_WAREHOUSE_IDS


def test_graveyard_is_id_14():
    """The 'Hent' device graveyard must remain id_entity=14. Discovered
    2026-05-12 via probe of vi-api.vedur.is."""
    assert DEVICE_GRAVEYARD_ID == 14


def test_station_subtypes_includes_core_types():
    """Smoke: the subtype set must contain the canonical GNSS-host types."""
    assert "geophysical" in STATION_SUBTYPES
    assert "meteorological" in STATION_SUBTYPES
    assert "general_station" in STATION_SUBTYPES


# ---------------------------------------------------------------------------
# ParentEntity
# ---------------------------------------------------------------------------


def test_parent_entity_from_history_station():
    h = _history(50, subtype="geophysical", name="RHOF")
    p = ParentEntity.from_history(50, h)
    assert p.id_entity == 50
    assert p.name == "RHOF"
    assert p.code_subtype == "geophysical"
    assert p.role == "station"


def test_parent_entity_from_history_warehouse():
    h = _history(4, subtype="area", name="B9 - Kjallari - Jörð")
    p = ParentEntity.from_history(4, h)
    assert p.code_subtype == "area"
    assert p.role == "warehouse"


def test_parent_entity_from_history_stock_is_warehouse():
    """Subtype ``stock`` is used for parent warehouses that group multiple
    ``area`` sub-warehouses (e.g. id=8 Vagnhöfði). Treated as warehouse,
    not "other" — discovered in live probe 2026-05-12."""
    h = _history(8, subtype="stock", name="Vagnhöfði")
    p = ParentEntity.from_history(8, h)
    assert p.code_subtype == "stock"
    assert p.role == "warehouse"


def test_parent_entity_from_history_graveyard():
    h = _history(14, subtype="discarded", name="Hent")
    p = ParentEntity.from_history(14, h)
    assert p.role == "graveyard"


def test_parent_entity_from_history_unknown_subtype_is_other():
    h = _history(99, subtype="some_future_subtype", name="?")
    p = ParentEntity.from_history(99, h)
    assert p.role == "other"


def test_parent_entity_from_history_no_name_attribute():
    """Mirrors the live-data quirk: some legacy parents have no ``name``
    attribute in their own ``attributes`` payload (their display name is
    only available via basic_search's lvl_two chain)."""
    h = _history(18409, subtype="geophysical")  # no name
    p = ParentEntity.from_history(18409, h)
    assert p.name is None
    assert p.role == "station"  # still a station, just unnamed in attrs


def test_parent_entity_skips_closed_name_attribute():
    """An attribute with ``time_to`` set is closed; we want the open name only."""
    h = {
        "id_entity": 50,
        "code_entity_subtype": "geophysical",
        "attributes": [
            {"code": "name", "value_varchar": "old", "time_to": "2024-01-01"},
            {"code": "name", "value_varchar": "current", "time_to": None},
        ],
    }
    p = ParentEntity.from_history(50, h)
    assert p.name == "current"


# ---------------------------------------------------------------------------
# read_station_markers
# ---------------------------------------------------------------------------


def test_read_station_markers_returns_upper_section_names(tmp_path: Path):
    cfg = tmp_path / "stations.cfg"
    cfg.write_text(
        "[RHOF]\n"
        "marker = RHOF\n"
        "router_ip = 10.0.0.1\n"
        "\n"
        "[REYK]\n"
        "router_ip = 10.0.0.2\n"
        "\n"
        "[lowercase_section]\n"
        "ignored = true\n"
    )
    markers = read_station_markers(str(cfg))
    assert "RHOF" in markers
    assert "REYK" in markers
    assert "lowercase_section" not in markers


def test_read_station_markers_skips_default_section(tmp_path: Path):
    """ConfigParser's special DEFAULT section must not leak into the list."""
    cfg = tmp_path / "stations.cfg"
    cfg.write_text("[DEFAULT]\nfoo = bar\n[RHOF]\nbaz = qux\n")
    markers = read_station_markers(str(cfg))
    # DEFAULT is omitted by configparser.sections()
    assert markers == ["RHOF"]


# ---------------------------------------------------------------------------
# resolve_marker_to_entity_id
# ---------------------------------------------------------------------------


def test_resolve_marker_returns_id_on_exact_match():
    client = _client(
        histories={},
        search_hits=[_marker_hit("RHOF", id_entity=4521)],
    )
    assert resolve_marker_to_entity_id(client, "RHOF") == 4521


def test_resolve_marker_is_case_insensitive():
    """The cfg may use any case; markers in TOS are stored lowercase. We
    match case-insensitively so the natural workflow works."""
    client = _client(
        histories={},
        search_hits=[_marker_hit("rhof", id_entity=4521)],
    )
    assert resolve_marker_to_entity_id(client, "RHOF") == 4521


def test_resolve_marker_rejects_distance_non_zero():
    """basic_search returns fuzzy matches; only exact (distance=0) wins."""
    client = _client(
        histories={},
        search_hits=[_marker_hit("RHOF", id_entity=4521, distance=8)],
    )
    assert resolve_marker_to_entity_id(client, "RHOF") is None


def test_resolve_marker_rejects_wrong_code():
    """Only ``code='marker'`` hits count — value matches in other
    attributes (name, description, etc.) are noise."""
    client = _client(
        histories={},
        search_hits=[
            {
                "code": "name",
                "distance": 0,
                "value_varchar": "RHOF",
                "id_entity": 9999,
            }
        ],
    )
    assert resolve_marker_to_entity_id(client, "RHOF") is None


def test_resolve_marker_returns_none_when_no_hit():
    client = _client(histories={}, search_hits=[])
    assert resolve_marker_to_entity_id(client, "UNKNOWN") is None


def test_resolve_marker_falls_back_to_id_lvl_three():
    """Some hits carry ``id_lvl_three`` but no top-level ``id_entity``;
    accept either to mirror :func:`tostools.audit._find_device_by_serial`."""
    client = _client(
        histories={},
        search_hits=[
            {
                "code": "marker",
                "distance": 0,
                "value_varchar": "RHOF",
                "id_lvl_three": 4521,
            }
        ],
    )
    assert resolve_marker_to_entity_id(client, "RHOF") == 4521


# ---------------------------------------------------------------------------
# enumerate_known_parents
# ---------------------------------------------------------------------------


def test_enumerate_known_parents_returns_infrastructure_when_no_cfg():
    """With no stations.cfg and no extras, only the 7 hardcoded
    infrastructure entities should appear."""
    histories = {
        eid: _history(eid, subtype="area", name=f"warehouse-{eid}")
        for eid in KNOWN_WAREHOUSE_IDS
    }
    histories[DEVICE_GRAVEYARD_ID] = _history(
        DEVICE_GRAVEYARD_ID, subtype="discarded", name="Hent"
    )
    client = _client(histories=histories)

    result = enumerate_known_parents(client, station_cfg_path=None)

    assert {p.id_entity for p in result} == set(KNOWN_INFRASTRUCTURE_IDS)
    # One graveyard, the rest warehouses
    graveyards = [p for p in result if p.role == "graveyard"]
    warehouses = [p for p in result if p.role == "warehouse"]
    assert len(graveyards) == 1
    assert len(warehouses) == len(KNOWN_WAREHOUSE_IDS)


def test_enumerate_known_parents_skips_unreadable_infrastructure(caplog):
    """When TOS cannot return history for a hardcoded ID (deleted entity,
    transient error), the function logs a warning and skips it rather
    than failing the whole bootstrap."""
    # Only the graveyard returns history; all warehouses return None.
    histories = {
        DEVICE_GRAVEYARD_ID: _history(
            DEVICE_GRAVEYARD_ID, subtype="discarded", name="Hent"
        )
    }
    client = _client(histories=histories)

    with caplog.at_level("WARNING"):
        result = enumerate_known_parents(client, station_cfg_path=None)

    assert {p.id_entity for p in result} == {DEVICE_GRAVEYARD_ID}
    # One warning per missing warehouse.
    skip_warnings = [r for r in caplog.records if "no history" in r.getMessage()]
    assert len(skip_warnings) == len(KNOWN_WAREHOUSE_IDS)


def test_enumerate_known_parents_resolves_stations_from_cfg(tmp_path: Path):
    """Markers in stations.cfg get resolved to entity IDs and added."""
    cfg = tmp_path / "stations.cfg"
    cfg.write_text("[RHOF]\n[REYK]\n")

    histories = {
        eid: _history(eid, subtype="area", name=f"warehouse-{eid}")
        for eid in KNOWN_WAREHOUSE_IDS
    }
    histories[DEVICE_GRAVEYARD_ID] = _history(
        DEVICE_GRAVEYARD_ID, subtype="discarded", name="Hent"
    )
    # Two stations
    histories[4521] = _history(4521, subtype="geophysical", name="Raufarhöfn")
    histories[4530] = _history(4530, subtype="geophysical", name="Reykjavík")
    # basic_search returns the marker hits — same payload for any search
    # call because the test client doesn't differentiate by search term.
    client = _client(
        histories=histories,
        search_hits=[
            _marker_hit("RHOF", id_entity=4521),
            _marker_hit("REYK", id_entity=4530),
        ],
    )

    result = enumerate_known_parents(client, station_cfg_path=str(cfg))
    ids = {p.id_entity for p in result}

    assert ids == set(KNOWN_INFRASTRUCTURE_IDS) | {4521, 4530}
    # The stations are tagged correctly
    stations = [p for p in result if p.role == "station"]
    assert {p.id_entity for p in stations} == {4521, 4530}


def test_enumerate_known_parents_silently_skips_unresolvable_marker(
    tmp_path: Path,
):
    """Markers that have no exact match in TOS are silently skipped; the
    rest of the bootstrap is unaffected."""
    cfg = tmp_path / "stations.cfg"
    cfg.write_text("[RHOF]\n[NOPE]\n")

    histories = {eid: _history(eid, subtype="area") for eid in KNOWN_WAREHOUSE_IDS}
    histories[DEVICE_GRAVEYARD_ID] = _history(DEVICE_GRAVEYARD_ID, subtype="discarded")
    histories[4521] = _history(4521, subtype="geophysical", name="Raufarhöfn")
    # Only RHOF resolves; NOPE has no hit.
    client = _client(
        histories=histories,
        search_hits=[_marker_hit("RHOF", id_entity=4521)],
    )

    result = enumerate_known_parents(client, station_cfg_path=str(cfg))
    ids = {p.id_entity for p in result}

    # Infrastructure + RHOF only; NOPE is dropped without error.
    assert ids == set(KNOWN_INFRASTRUCTURE_IDS) | {4521}


def test_enumerate_known_parents_accepts_extra_ids():
    """`extra_parent_ids` injects known-missing parents (the
    Fagradalsfjall / Bláfjöll / Bárðabunga / Hestalda gap)."""
    histories = {eid: _history(eid, subtype="area") for eid in KNOWN_WAREHOUSE_IDS}
    histories[DEVICE_GRAVEYARD_ID] = _history(DEVICE_GRAVEYARD_ID, subtype="discarded")
    # Simulate the 4 stations missing from stations.cfg
    for eid in (18409, 4243, 4239, 5444):
        histories[eid] = _history(eid, subtype="geophysical", name=None)
    client = _client(histories=histories)

    result = enumerate_known_parents(
        client,
        station_cfg_path=None,
        extra_parent_ids=(18409, 4243, 4239, 5444),
    )
    ids = {p.id_entity for p in result}

    assert {18409, 4243, 4239, 5444}.issubset(ids)
    # The extras are stations even though their name is None.
    extras = [p for p in result if p.id_entity in {18409, 4243, 4239, 5444}]
    assert all(p.role == "station" and p.name is None for p in extras)


def test_enumerate_known_parents_dedupes_overlaps(tmp_path: Path):
    """If a caller-supplied extra_parent_id overlaps with stations.cfg or
    the hardcoded infrastructure, the returned list still contains each
    entity exactly once."""
    cfg = tmp_path / "stations.cfg"
    cfg.write_text("[RHOF]\n")

    histories = {eid: _history(eid, subtype="area") for eid in KNOWN_WAREHOUSE_IDS}
    histories[DEVICE_GRAVEYARD_ID] = _history(DEVICE_GRAVEYARD_ID, subtype="discarded")
    histories[4521] = _history(4521, subtype="geophysical", name="Raufarhöfn")
    client = _client(
        histories=histories,
        search_hits=[_marker_hit("RHOF", id_entity=4521)],
    )

    # Supply 4521 again as an extra + 4 (already in infrastructure)
    result = enumerate_known_parents(
        client,
        station_cfg_path=str(cfg),
        extra_parent_ids=(4521, 4),
    )
    ids = [p.id_entity for p in result]

    # No id appears twice
    assert len(ids) == len(set(ids))
    # All expected ids are present
    assert set(ids) == set(KNOWN_INFRASTRUCTURE_IDS) | {4521}


# ---------------------------------------------------------------------------
# default_station_cfg_path
# ---------------------------------------------------------------------------


def test_default_station_cfg_path_uses_env_var(tmp_path: Path, monkeypatch):
    """``GPS_CONFIG_PATH`` env var wins when set and the file exists."""
    cfg = tmp_path / "stations.cfg"
    cfg.write_text("[RHOF]\n")
    monkeypatch.setenv("GPS_CONFIG_PATH", str(tmp_path))
    assert default_station_cfg_path() == str(cfg)


def test_default_station_cfg_path_falls_back_to_xdg(monkeypatch, tmp_path: Path):
    """When the env var is unset, fall back to
    ``~/.config/gpsconfig/stations.cfg``."""
    monkeypatch.delenv("GPS_CONFIG_PATH", raising=False)
    # Redirect HOME so we don't hit the developer's real cfg.
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".config" / "gpsconfig"
    cfg_dir.mkdir(parents=True)
    cfg_path = cfg_dir / "stations.cfg"
    cfg_path.write_text("[RHOF]\n")
    assert default_station_cfg_path() == str(cfg_path)


def test_default_station_cfg_path_returns_none_when_missing(
    monkeypatch, tmp_path: Path
):
    """When neither candidate exists, return None (caller decides what to do)."""
    monkeypatch.delenv("GPS_CONFIG_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no .config/gpsconfig under here
    assert default_station_cfg_path() is None
