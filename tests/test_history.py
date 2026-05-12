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


# ===========================================================================
# Step 3 — Join, JoinIndex, DeviceTimeline, Gap, build_join_index
# ===========================================================================


from tostools.history import (  # noqa: E402  re-import to keep step-3 block self-contained
    DeviceTimeline,
    Gap,
    Join,
    JoinIndex,
    _connection_to_join,
    _parse_iso,
    build_join_index,
)


def _join(
    child: int,
    parent: int,
    time_from: str,
    time_to: Optional[str] = None,
    *,
    connection_id: int = 0,
) -> Join:
    return Join(
        id_entity_connection=connection_id,
        id_entity_parent=parent,
        id_entity_child=child,
        time_from=time_from,
        time_to=time_to,
    )


def _parent(
    id_entity: int,
    *,
    role: str = "station",
    subtype: str = "geophysical",
    name: Optional[str] = None,
) -> ParentEntity:
    return ParentEntity(
        id_entity=id_entity,
        name=name,
        code_subtype=subtype,
        role=role,
    )


def _parent_history_with_conns(
    id_entity: int,
    conns: List[Dict[str, Any]],
    subtype: str = "geophysical",
) -> Dict[str, Any]:
    return {
        "id_entity": id_entity,
        "code_entity_subtype": subtype,
        "attributes": [],
        "children_connections": conns,
    }


def _conn(
    *,
    id_entity_connection: int,
    parent: int,
    child: int,
    time_from: str,
    time_to: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id_entity_connection": id_entity_connection,
        "id_entity_parent": parent,
        "id_entity_child": child,
        "time_from": time_from,
        "time_to": time_to,
    }


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------


def test_join_is_open_when_time_to_is_none():
    j = _join(100, 50, "2025-03-15")
    assert j.is_open is True


def test_join_is_closed_when_time_to_is_set():
    j = _join(100, 50, "2025-03-15", "2025-06-30")
    assert j.is_open is False


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------


def test_parse_iso_standard_datetime():
    dt = _parse_iso("2025-11-05T00:00:00")
    assert dt is not None
    assert dt.year == 2025 and dt.month == 11 and dt.day == 5


def test_parse_iso_with_z_suffix():
    dt = _parse_iso("2025-11-05T00:00:00Z")
    assert dt is not None
    assert dt.year == 2025


def test_parse_iso_date_only_fallback():
    """If TOS ever returns a date without time, we still parse."""
    dt = _parse_iso("2025-11-05")
    assert dt is not None
    assert dt.year == 2025


def test_parse_iso_returns_none_for_garbage():
    assert _parse_iso("not-a-date") is None
    assert _parse_iso(None) is None
    assert _parse_iso("") is None


# ---------------------------------------------------------------------------
# _connection_to_join
# ---------------------------------------------------------------------------


def test_connection_to_join_uses_connection_parent_id():
    """When the conn has id_entity_parent, that's what we use (not the
    fallback). Real TOS data always carries this."""
    conn = _conn(
        id_entity_connection=42,
        parent=50,
        child=100,
        time_from="2025-03-15",
    )
    j = _connection_to_join(conn, fallback_parent_id=999)
    assert j is not None
    assert j.id_entity_parent == 50  # from conn, not fallback
    assert j.id_entity_child == 100
    assert j.id_entity_connection == 42


def test_connection_to_join_falls_back_to_walked_parent_id_when_missing():
    """Some legacy connection rows omit id_entity_parent; we fall back to
    the parent we're currently walking."""
    conn = {
        "id_entity_connection": 42,
        "id_entity_child": 100,
        "time_from": "2025-03-15",
        "time_to": None,
    }
    j = _connection_to_join(conn, fallback_parent_id=999)
    assert j is not None
    assert j.id_entity_parent == 999


def test_connection_to_join_returns_none_for_missing_child_id():
    conn = {
        "id_entity_parent": 50,
        "time_from": "2025-03-15",
        "time_to": None,
    }
    j = _connection_to_join(conn, fallback_parent_id=999)
    assert j is None


# ---------------------------------------------------------------------------
# DeviceTimeline — sorting and properties
# ---------------------------------------------------------------------------


def test_timeline_sorts_joins_by_time_from():
    """Joins arrive from arbitrary parents in any order; the timeline
    must sort them chronologically for gap detection to work."""
    tl = DeviceTimeline(
        100,
        [
            _join(100, 60, "2025-06-30", None),
            _join(100, 50, "2020-01-01", "2025-05-15"),
            _join(100, 70, "2018-04-01", "2019-12-31"),
        ],
    )
    times = [j.time_from for j in tl.joins]
    assert times == sorted(times)


def test_timeline_open_and_closed_partition():
    tl = DeviceTimeline(
        100,
        [
            _join(100, 50, "2020-01-01", "2025-05-15"),
            _join(100, 60, "2025-06-30", None),
        ],
    )
    assert len(tl.open_joins) == 1
    assert tl.open_joins[0].id_entity_parent == 60
    assert len(tl.closed_joins) == 1
    assert tl.closed_joins[0].id_entity_parent == 50


def test_timeline_is_currently_attached_when_any_open_join():
    tl = DeviceTimeline(100, [_join(100, 60, "2025-06-30", None)])
    assert tl.is_currently_attached is True


def test_timeline_is_truly_orphan_when_joins_but_none_open():
    """The audit's I1-orphan signal, but derived from full index."""
    tl = DeviceTimeline(
        100,
        [_join(100, 50, "2020-01-01", "2025-05-15")],
    )
    assert tl.is_truly_orphan is True
    assert tl.is_currently_attached is False


def test_timeline_empty_is_not_orphan():
    """A device with NO joins anywhere isn't 'orphan' — it's not in the
    index at all. is_truly_orphan requires joins-but-none-open."""
    tl = DeviceTimeline(100, [])
    assert tl.is_truly_orphan is False
    assert tl.is_currently_attached is False


# ---------------------------------------------------------------------------
# DeviceTimeline.gaps
# ---------------------------------------------------------------------------


def test_gaps_empty_when_fewer_than_two_joins():
    tl = DeviceTimeline(100, [_join(100, 50, "2025-03-15")])
    assert tl.gaps() == []


def test_gaps_detects_simple_gap():
    """Models device 19969 (Grindavík vestur close 2025-05-05 → Grindavík
    miðja open 2025-11-05 = ~184-day gap)."""
    tl = DeviceTimeline(
        19969,
        [
            _join(19969, 19968, "2023-11-15T00:00:00", "2025-05-05T00:00:00"),
            _join(19969, 19964, "2025-11-05T00:00:00", None),
        ],
    )
    gaps = tl.gaps()
    assert len(gaps) == 1
    g = gaps[0]
    assert g.id_entity == 19969
    assert g.after.id_entity_parent == 19968
    assert g.before.id_entity_parent == 19964
    assert 180 < g.duration_days < 190
    assert g.time_from == "2025-05-05T00:00:00"
    assert g.time_to == "2025-11-05T00:00:00"


def test_gaps_min_days_threshold_filters_short_artifacts():
    """1–30 day "gaps" are typically date-rounding artifacts (per advisor
    caveat). The threshold drops them."""
    tl = DeviceTimeline(
        100,
        [
            _join(100, 50, "2020-01-01", "2020-06-01"),
            _join(100, 60, "2020-06-05", None),  # 4-day gap
        ],
    )
    assert len(tl.gaps(min_days=0)) == 1
    assert tl.gaps(min_days=30) == []


def test_gaps_ignores_overlap_as_gap():
    """When the next join starts BEFORE the previous one closes, that's
    overlap (an I2-style anomaly), not a gap."""
    tl = DeviceTimeline(
        100,
        [
            _join(100, 50, "2020-01-01", "2025-12-31"),
            _join(100, 60, "2024-06-15", None),  # starts inside join-1
        ],
    )
    assert tl.gaps() == []


def test_gaps_ignores_pair_starting_with_open_join():
    """Two opens (or open-then-closed) is multi-open territory, not a gap."""
    tl = DeviceTimeline(
        100,
        [
            _join(100, 50, "2020-01-01", None),  # open
            _join(100, 60, "2025-06-30", None),  # open
        ],
    )
    assert tl.gaps() == []


def test_gaps_handles_unparseable_dates():
    """If TOS ever returns malformed dates, skip the affected gap rather
    than crashing."""
    tl = DeviceTimeline(
        100,
        [
            _join(100, 50, "2020-01-01", "garbage"),
            _join(100, 60, "2025-06-30", None),
        ],
    )
    # Bad close date → no gap surfaced, no exception.
    assert tl.gaps() == []


def test_gaps_multiple_gaps_in_one_timeline():
    """A device that bounced through several stations with gaps between."""
    tl = DeviceTimeline(
        100,
        [
            _join(100, 50, "2018-01-01", "2019-06-30"),
            _join(100, 60, "2020-01-15", "2022-08-15"),  # gap 1: ~6.5 months
            _join(100, 70, "2024-03-01", None),  # gap 2: ~18.5 months
        ],
    )
    gaps = tl.gaps(min_days=30)
    assert len(gaps) == 2
    # First gap is between 50-close and 60-open
    assert gaps[0].after.id_entity_parent == 50
    assert gaps[0].before.id_entity_parent == 60
    # Second between 60-close and 70-open
    assert gaps[1].after.id_entity_parent == 60
    assert gaps[1].before.id_entity_parent == 70


# ---------------------------------------------------------------------------
# JoinIndex
# ---------------------------------------------------------------------------


def test_join_index_timeline_for_known_device():
    idx = JoinIndex(
        by_child={
            100: [
                _join(100, 50, "2020-01-01", "2025-05-15"),
                _join(100, 60, "2025-06-30", None),
            ]
        }
    )
    tl = idx.timeline(100)
    assert isinstance(tl, DeviceTimeline)
    assert len(tl.joins) == 2


def test_join_index_timeline_for_unknown_device_returns_empty():
    idx = JoinIndex()
    tl = idx.timeline(999)
    assert tl.joins == []
    assert tl.is_currently_attached is False


def test_join_index_total_joins_counts_across_all_devices():
    idx = JoinIndex(
        by_child={
            100: [_join(100, 50, "2020-01-01")],
            200: [
                _join(200, 50, "2020-01-01", "2022-01-01"),
                _join(200, 60, "2022-02-01", None),
            ],
        }
    )
    assert idx.total_joins == 3


def test_join_index_device_ids_returns_sorted_unique():
    idx = JoinIndex(
        by_child={
            200: [_join(200, 50, "2020-01-01")],
            100: [_join(100, 50, "2020-01-01")],
        }
    )
    assert idx.device_ids == [100, 200]


# ---------------------------------------------------------------------------
# build_join_index
# ---------------------------------------------------------------------------


def test_build_join_index_aggregates_across_parents():
    """A device joined to parent A then to parent B shows both joins in
    its timeline after one full index build."""
    h_a = _parent_history_with_conns(
        50,
        conns=[
            _conn(
                id_entity_connection=1,
                parent=50,
                child=100,
                time_from="2020-01-01",
                time_to="2022-05-15",
            )
        ],
    )
    h_b = _parent_history_with_conns(
        60,
        conns=[
            _conn(
                id_entity_connection=2,
                parent=60,
                child=100,
                time_from="2023-03-15",
                time_to=None,
            )
        ],
    )
    client = MagicMock()
    client.get_entity_history.side_effect = lambda i: {50: h_a, 60: h_b}.get(int(i))

    idx = build_join_index(client, parents=[_parent(50), _parent(60)])

    assert idx.parents_walked == 2
    assert idx.parents_failed == 0
    tl = idx.timeline(100)
    assert len(tl.joins) == 2
    # Sorted chronologically
    assert tl.joins[0].id_entity_parent == 50
    assert tl.joins[1].id_entity_parent == 60
    # Closed → Open transition is a recoverable gap
    gaps = tl.gaps()
    assert len(gaps) == 1


def test_build_join_index_skips_unreadable_parent():
    """A parent whose history returns None (deleted? transient error?) is
    counted as failed; the rest of the index is unaffected."""
    h_b = _parent_history_with_conns(
        60,
        conns=[
            _conn(
                id_entity_connection=2,
                parent=60,
                child=100,
                time_from="2023-03-15",
            )
        ],
    )
    client = MagicMock()
    client.get_entity_history.side_effect = lambda i: {60: h_b}.get(int(i))

    idx = build_join_index(client, parents=[_parent(50), _parent(60)])

    assert idx.parents_walked == 1
    assert idx.parents_failed == 1
    assert idx.timeline(100).joins[0].id_entity_parent == 60


def test_build_join_index_progress_callback_fires_per_parent():
    h = _parent_history_with_conns(50, conns=[])
    client = MagicMock()
    client.get_entity_history.return_value = h

    calls: List[tuple] = []
    idx = build_join_index(
        client,
        parents=[_parent(50), _parent(60), _parent(70)],
        progress=lambda i, total: calls.append((i, total)),
    )
    _ = idx  # unused
    assert calls == [(1, 3), (2, 3), (3, 3)]


def test_build_join_index_handles_get_entity_history_exception():
    """A network error reading one parent should NOT abort the whole
    index build."""
    h_good = _parent_history_with_conns(60, conns=[])

    def hist(i):
        if int(i) == 50:
            raise RuntimeError("transient TOS error")
        return h_good if int(i) == 60 else None

    client = MagicMock()
    client.get_entity_history.side_effect = hist

    idx = build_join_index(client, parents=[_parent(50), _parent(60)])

    assert idx.parents_walked == 1
    assert idx.parents_failed == 1


def test_build_join_index_calls_enumerate_known_parents_when_none(monkeypatch):
    """The convenience case ``parents=None`` calls enumerate_known_parents
    so callers don't need to wire it up themselves."""
    import tostools.history as histmod

    sentinel = [_parent(4, role="warehouse", subtype="area")]
    h = _parent_history_with_conns(4, conns=[], subtype="area")
    client = MagicMock()
    client.get_entity_history.return_value = h

    monkeypatch.setattr(histmod, "enumerate_known_parents", lambda c: sentinel)
    idx = build_join_index(client)

    assert idx.parents_walked == 1


def test_build_join_index_ignores_malformed_connection_rows():
    """A row missing id_entity_child is dropped; index still built from
    the well-formed rows."""
    h = _parent_history_with_conns(
        50,
        conns=[
            {"id_entity_connection": 1, "time_from": "2020-01-01"},  # no child
            _conn(
                id_entity_connection=2,
                parent=50,
                child=100,
                time_from="2021-01-01",
            ),
        ],
    )
    client = MagicMock()
    client.get_entity_history.return_value = h

    idx = build_join_index(client, parents=[_parent(50)])

    assert idx.total_joins == 1
    assert 100 in idx.device_ids


# ---------------------------------------------------------------------------
# Gap dataclass
# ---------------------------------------------------------------------------


def test_gap_time_from_and_time_to_properties():
    j_after = _join(100, 50, "2020-01-01", "2022-05-15")
    j_before = _join(100, 60, "2023-03-15", None)
    g = Gap(id_entity=100, after=j_after, before=j_before, duration_days=304.0)
    assert g.time_from == "2022-05-15"
    assert g.time_to == "2023-03-15"
