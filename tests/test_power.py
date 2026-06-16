"""Tests for ``tostools.power`` — shared-power surfacing (W1).

``summarize_site_power`` walks a ``land`` site's colocated stations (and any
power joined directly to the site) and returns one row per power device, so an
operator reusing a site sees the existing supply instead of duplicating it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from tostools.power import (
    POWER_DEVICE_SUBTYPES,
    SENSOR_TIED_POWER_SUBTYPES,
    summarize_site_power,
)


class _FakeHistoryWriter:
    """Serves ``get_entity_history`` from a fixed ``{id: history}`` map."""

    def __init__(self, histories: Dict[int, Dict[str, Any]]) -> None:
        self._histories = histories

    def get_entity_history(self, entity_id: int) -> Optional[Dict[str, Any]]:
        return self._histories.get(int(entity_id))


def _attr(code: str, value: str) -> Dict[str, Any]:
    return {"code": code, "value": value, "date_from": "2000", "date_to": None}


def _station(name: str, *children) -> Dict[str, Any]:
    return {
        "code_entity_subtype": "geophysical",
        "attributes": [_attr("name", name)],
        "children_connections": list(children),
    }


def _device(subtype: str, model: str = "M", serial: str = "S") -> Dict[str, Any]:
    return {
        "code_entity_subtype": subtype,
        "attributes": [_attr("model", model), _attr("serial_number", serial)],
        "children_connections": [],
    }


def _conn(child_id: int, time_from: str = "2010", time_to=None) -> Dict[str, Any]:
    return {"id_entity_child": child_id, "time_from": time_from, "time_to": time_to}


def test_subtype_sets_are_sane() -> None:
    assert "battery" in POWER_DEVICE_SUBTYPES
    assert "solar_panel" in POWER_DEVICE_SUBTYPES
    # sensor-tied power is a subset of the power family
    assert SENSOR_TIED_POWER_SUBTYPES <= POWER_DEVICE_SUBTYPES


def test_aggregates_power_across_colocated_stations() -> None:
    histories = {
        4360: {  # the land site
            "code_entity_subtype": "land",
            "children_connections": [_conn(5487, "2010-06"), _conn(18071, "2010-08")],
        },
        5487: _station("Mjóaskarð", _conn(16486), _conn(4905)),
        18071: _station("Mjóaskarð - Endurvarpi", _conn(21468)),
        16486: _device("battery", "Vision"),
        4905: _device("gnss_receiver", "PolaRx5"),  # NOT power — ignored
        21468: _device("battery", "Sunark"),
    }
    rows = summarize_site_power(_FakeHistoryWriter(histories), 4360)
    assert {r["id_entity"] for r in rows} == {16486, 21468}
    by_id = {r["id_entity"]: r for r in rows}
    assert by_id[16486]["on_station"] == 5487
    assert by_id[16486]["on_station_name"] == "Mjóaskarð"
    assert by_id[16486]["model"] == "Vision"
    assert by_id[21468]["on_station"] == 18071


def test_site_direct_power_has_no_station_and_sorts_first() -> None:
    histories = {
        1: {
            "code_entity_subtype": "land",
            "children_connections": [_conn(2, "2010"), _conn(9, "2009")],
        },
        # 9 is power joined DIRECTLY to the site (W3 target state)
        9: _device("solar_panel", "Offgridtec"),
        2: _station("Stn", _conn(3)),
        3: _device("battery"),
    }
    rows = summarize_site_power(_FakeHistoryWriter(histories), 1)
    assert rows[0]["id_entity"] == 9  # site-direct sorts before station power
    assert rows[0]["on_station"] is None
    assert rows[1]["id_entity"] == 3
    assert rows[1]["on_station"] == 2


def test_open_only_filters_closed_station_join() -> None:
    histories = {
        1: {
            "code_entity_subtype": "land",
            "children_connections": [
                _conn(2, "2000", time_to="2005"),  # station no longer at site
                _conn(3, "2006"),
            ],
        },
        2: _station("Old", _conn(20)),
        3: _station("Now", _conn(30)),
        20: _device("battery"),
        30: _device("battery"),
    }
    fw = _FakeHistoryWriter(histories)
    assert {r["id_entity"] for r in summarize_site_power(fw, 1)} == {30}
    assert {r["id_entity"] for r in summarize_site_power(fw, 1, open_only=False)} == {
        20,
        30,
    }


def test_open_only_filters_closed_power_join() -> None:
    histories = {
        1: {
            "code_entity_subtype": "land",
            "children_connections": [_conn(2)],
        },
        2: _station(
            "Stn",
            _conn(20, "2000", time_to="2005"),  # battery swapped out
            _conn(21, "2005"),
        ),
        20: _device("battery", "Old"),
        21: _device("battery", "New"),
    }
    rows = summarize_site_power(_FakeHistoryWriter(histories), 1)
    assert [r["id_entity"] for r in rows] == [21]


def test_sensor_tied_power_is_flagged() -> None:
    histories = {
        1: {"code_entity_subtype": "land", "children_connections": [_conn(2)]},
        2: _station("Wx", _conn(20), _conn(21)),
        20: _device("anemometer_power_pack"),
        21: _device("battery"),
    }
    rows = summarize_site_power(_FakeHistoryWriter(histories), 1)
    flags = {r["id_entity"]: r["sensor_tied"] for r in rows}
    assert flags[20] is True
    assert flags[21] is False


def test_empty_when_no_power() -> None:
    histories = {
        1: {"code_entity_subtype": "land", "children_connections": [_conn(2)]},
        2: _station("Stn", _conn(3)),
        3: _device("gnss_receiver"),
    }
    assert summarize_site_power(_FakeHistoryWriter(histories), 1) == []


def test_empty_when_history_missing() -> None:
    assert summarize_site_power(_FakeHistoryWriter({}), 999) == []
