"""Unit tests for :mod:`tostools.audit_missing_attributes`.

No network — :class:`tostools.api.tos_client.TOSClient` is mocked. Catalog
fixtures are written to ``tmp_path`` as minimal in-memory YAML; the goal is
focused per-rule coverage of the walker, not a full integration test.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from tostools.audit_missing_attributes import audit_station_missing_attributes


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _attr(code: str, value, date_from: str = "2010-01-01", date_to=None):
    return {
        "code": code,
        "value": value,
        "date_from": date_from,
        "date_to": date_to,
    }


def _conn(id_child: int, time_from: str = "2010-01-01", time_to=None):
    return {
        "id_entity_child": id_child,
        "time_from": time_from,
        "time_to": time_to,
    }


def _device(id_entity: int, subtype: str, attributes):
    return {
        "id_entity": id_entity,
        "code_entity_subtype": subtype,
        "attributes": list(attributes),
        "children_connections": [],
    }


def _station(id_entity: int, name: str, connections, *, extra_attrs=()):
    return {
        "id_entity": id_entity,
        "code_entity_subtype": "geophysical",
        "attributes": [_attr("name", name)] + list(extra_attrs),
        "children_connections": list(connections),
    }


def _client_for(history_by_id):
    """Mock client whose ``get_entity_history`` dispatches by id and whose
    ``basic_search`` is unused (we resolve by id_entity directly)."""
    client = MagicMock()
    client.get_entity_history.side_effect = lambda i: history_by_id.get(int(i))
    return client


# Minimal catalog exercising the rule across both scopes. Mirrors the real
# data/attribute_codes.yaml structure but trimmed to the cases the tests cover.
_CATALOG_YAML = dedent("""
    devices:
      serial_number:
        icelandic_label: Raðnúmer
        description: Physical device identity
        classification: inherent
        gps_required_for: [gnss_receiver, antenna, monument]
        applies_to: [gnss_receiver, antenna, radome, monument]
        gps_relevance: "yes"

      model:
        icelandic_label: Tegund tækis
        classification: inherent
        gps_required_for: [gnss_receiver, antenna]
        applies_to: [gnss_receiver, antenna, monument]
        gps_relevance: "yes"

      subtype:
        # Cross-scope collision with stations.subtype — TOS uses the same
        # code on both scopes. In the devices scope it's classified TODO
        # / not GPS-relevant.
        icelandic_label: Undirtegund
        classification: TODO
        gps_required_for: []
        applies_to: [gnss_receiver, antenna, radome, monument]
        gps_relevance: "no"

      inscription:
        # Monument-only — exercises monument-specific filtering.
        icelandic_label: Áletrun
        classification: inherent
        gps_required_for: [monument]
        applies_to: [monument]
        gps_relevance: "yes"

      not_relevant:
        # gps_relevance: "no" — must be skipped even when required matches.
        icelandic_label: Hiti
        classification: inherent
        gps_required_for: [gnss_receiver]
        applies_to: [gnss_receiver]
        gps_relevance: "no"

    stations:
      marker:
        icelandic_label: Auðkenni
        classification: inherent
        gps_required_for: [geophysical]
        applies_to: [geophysical]
        gps_relevance: "yes"

      name:
        classification: inherent
        gps_required_for: [geophysical]
        applies_to: [geophysical]
        gps_relevance: "yes"

      date_start:
        classification: inherent
        gps_required_for: [geophysical]
        applies_to: [geophysical]
        gps_relevance: "yes"

      subtype:
        # Cross-scope collision — the regression the scoped loader fixes.
        # In stations scope this is GPS-required with a default value.
        icelandic_label: Undirtegund
        classification: inherent
        gps_required_for: [geophysical]
        default_value: "GPS stöð"
        applies_to: [geophysical]
        gps_relevance: "yes"

      altitude:
        classification: inherent
        gps_required_for: [geophysical]
        applies_to: [geophysical]
        gps_relevance: "yes"
    """).strip()


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    p = tmp_path / "attribute_codes.yaml"
    p.write_text(_CATALOG_YAML, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Walker — basic cases
# ---------------------------------------------------------------------------


def test_clean_station_emits_no_violations(catalog_path: Path):
    """Station + one device, all required attributes present → no
    violations, audited_entities == 2."""
    station = _station(
        100,
        "Test Station",
        connections=[_conn(200)],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("date_start", "2010-01-01"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
        ],
    )
    device = _device(
        200,
        "gnss_receiver",
        [_attr("serial_number", "SN001"), _attr("model", "POLARX5")],
    )
    client = _client_for({100: station, 200: device})

    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    assert not report.has_violations
    assert report.audited_entities == 2
    assert report.devices_skipped == 0


def test_station_missing_date_start_flagged(catalog_path: Path):
    """The REYK gap: station entity has no ``date_start`` open period."""
    station = _station(
        100,
        "Test Station",
        connections=[],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
            # date_start missing
        ],
    )
    client = _client_for({100: station})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    assert report.has_violations
    codes = {v.code for v in report.violations}
    assert "date_start" in codes
    assert report.audited_entities == 1


def test_station_subtype_pre_filled_from_default(catalog_path: Path):
    """When the catalog has a ``default_value``, the violation carries it
    in ``suggested_value`` for triage pre-fill."""
    station = _station(
        100,
        "Test Station",
        connections=[],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("date_start", "2010-01-01"),
            _attr("altitude", 12.3),
            # subtype missing
        ],
    )
    client = _client_for({100: station})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    subtype_vio = next(v for v in report.violations if v.code == "subtype")
    assert subtype_vio.suggested_value == "GPS stöð"
    assert subtype_vio.scope == "stations"


def test_violation_without_default_has_no_suggested_value(catalog_path: Path):
    """Codes without a catalog ``default_value`` get ``suggested_value=None``
    — the triage emitter renders ``<FILL_VALUE>``."""
    station = _station(
        100,
        "Test Station",
        connections=[],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
            # date_start missing — no default in catalog
        ],
    )
    client = _client_for({100: station})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    date_vio = next(v for v in report.violations if v.code == "date_start")
    assert date_vio.suggested_value is None


# ---------------------------------------------------------------------------
# Walker — device handling
# ---------------------------------------------------------------------------


def test_device_missing_serial_number_flagged(catalog_path: Path):
    """Open device missing required attribute on devices scope."""
    station = _station(
        100,
        "Test Station",
        connections=[_conn(200, time_from="2015-06-01")],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("date_start", "2010-01-01"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
        ],
    )
    device = _device(
        200,
        "gnss_receiver",
        [_attr("model", "POLARX5")],
        # serial_number missing
    )
    client = _client_for({100: station, 200: device})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    serial_vios = [v for v in report.violations if v.code == "serial_number"]
    assert len(serial_vios) == 1
    v = serial_vios[0]
    assert v.id_entity == 200
    assert v.subtype == "gnss_receiver"
    assert v.scope == "devices"
    # Earliest open-join time_from used as date hint for the triage line.
    assert v.suggested_date_from == "2015-06-01"


def test_closed_device_join_is_skipped(catalog_path: Path):
    """Devices whose join is closed (``time_to`` set) are removed from the
    station; their missing attributes are not a current violation."""
    station = _station(
        100,
        "Test Station",
        connections=[_conn(200, time_from="2010-01-01", time_to="2015-01-01")],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("date_start", "2010-01-01"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
        ],
    )
    # Device exists but its missing attrs should NOT be flagged because
    # the join is closed.
    device = _device(200, "gnss_receiver", [])
    client = _client_for({100: station, 200: device})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    assert all(v.id_entity != 200 for v in report.violations)
    # Station-side audit still ran:
    assert report.audited_entities == 1


def test_non_gps_device_subtype_is_skipped(catalog_path: Path):
    """A router (subtype outside the GPS quartet) bumps devices_skipped
    and contributes no violations."""
    station = _station(
        100,
        "Test Station",
        connections=[_conn(200)],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("date_start", "2010-01-01"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
        ],
    )
    router = _device(200, "router", [_attr("subtype", "4G LTE")])
    client = _client_for({100: station, 200: router})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    assert report.devices_skipped == 1
    assert all(v.id_entity != 200 for v in report.violations)


def test_monument_specific_inscription_flagged(catalog_path: Path):
    """``inscription`` only applies to monuments — naturally picked up via
    the gps_required_for filter when the device subtype is ``monument``."""
    station = _station(
        100,
        "Test Station",
        connections=[_conn(300)],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("date_start", "2010-01-01"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
        ],
    )
    monument = _device(300, "monument", [_attr("serial_number", "MON001")])
    client = _client_for({100: station, 300: monument})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    insc = [v for v in report.violations if v.code == "inscription"]
    assert len(insc) == 1
    assert insc[0].subtype == "monument"


# ---------------------------------------------------------------------------
# Walker — scope isolation (cross-scope collision regression)
# ---------------------------------------------------------------------------


def test_station_subtype_not_shadowed_by_devices_subtype(catalog_path: Path):
    """The regression the rename + scoped loader fixes: TOS uses the same
    code ``subtype`` on both scopes. The station-scope rule has
    ``gps_required_for: [geophysical]`` with a default; the devices-scope
    rule is TODO / not GPS-relevant. The walker must hit the station-scope
    rule when auditing the station entity, not be silenced by the devices
    rule."""
    station = _station(
        100,
        "Test Station",
        connections=[],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("date_start", "2010-01-01"),
            _attr("altitude", 12.3),
            # subtype missing — must flag with stations-scope rule
        ],
    )
    client = _client_for({100: station})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    subtype_vios = [v for v in report.violations if v.code == "subtype"]
    assert len(subtype_vios) == 1
    v = subtype_vios[0]
    assert v.scope == "stations"
    assert v.suggested_value == "GPS stöð"


def test_device_with_present_subtype_emits_no_subtype_violation(
    catalog_path: Path,
):
    """The devices-scope ``subtype`` rule has gps_relevance=no — even when
    a device has no ``subtype`` attribute, it must not be flagged. Confirms
    the gps_relevance filter."""
    station = _station(
        100,
        "Test Station",
        connections=[_conn(200)],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("date_start", "2010-01-01"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
        ],
    )
    device = _device(
        200,
        "gnss_receiver",
        [_attr("serial_number", "SN001"), _attr("model", "POLARX5")],
        # subtype attribute absent — but gps_relevance="no" in devices scope
        # so it must not flag
    )
    client = _client_for({100: station, 200: device})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    assert not any(v.code == "subtype" and v.scope == "devices" for v in report.violations)


def test_not_relevant_code_skipped_even_when_required(catalog_path: Path):
    """A code with gps_required_for: [gnss_receiver] BUT gps_relevance: 'no'
    is NOT audited — gps_relevance gates above gps_required_for."""
    station = _station(
        100,
        "Test Station",
        connections=[_conn(200)],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("date_start", "2010-01-01"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
        ],
    )
    device = _device(
        200,
        "gnss_receiver",
        [_attr("serial_number", "SN001"), _attr("model", "POLARX5")],
        # `not_relevant` absent — must not flag (gps_relevance=no)
    )
    client = _client_for({100: station, 200: device})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    assert all(v.code != "not_relevant" for v in report.violations)


# ---------------------------------------------------------------------------
# Walker — date hint selection
# ---------------------------------------------------------------------------


def test_device_with_multiple_open_joins_picks_earliest_date(
    catalog_path: Path,
):
    """A device returns to a station after a stint elsewhere — multiple
    open joins. The earliest time_from anchors the date hint."""
    station = _station(
        100,
        "Test Station",
        connections=[
            _conn(200, time_from="2018-06-01"),
            _conn(200, time_from="2012-03-15"),
            _conn(200, time_from="2020-01-01"),
        ],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("date_start", "2010-01-01"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
        ],
    )
    device = _device(200, "antenna", [])  # missing serial_number + model
    client = _client_for({100: station, 200: device})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    serial_vios = [v for v in report.violations if v.code == "serial_number"]
    assert len(serial_vios) == 1
    assert serial_vios[0].suggested_date_from == "2012-03-15"


def test_station_violations_have_no_date_hint(catalog_path: Path):
    """Station-level missing attributes don't carry a date hint — the
    operator picks the date when uncommenting the triage line."""
    station = _station(
        100,
        "Test Station",
        connections=[],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
            # date_start missing
        ],
    )
    client = _client_for({100: station})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    for v in report.violations:
        assert v.suggested_date_from is None


# ---------------------------------------------------------------------------
# Walker — report bookkeeping
# ---------------------------------------------------------------------------


def test_audited_entities_counts_station_plus_open_devices(catalog_path: Path):
    """One station + two open devices + one closed device + one router
    (skipped). audited_entities counts station + open GPS devices only."""
    station = _station(
        100,
        "Test Station",
        connections=[
            _conn(200),
            _conn(201),
            _conn(202, time_to="2015-01-01"),  # closed
            _conn(203),  # router → skipped
        ],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("date_start", "2010-01-01"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
        ],
    )
    history_by_id = {
        100: station,
        200: _device(200, "gnss_receiver",
                     [_attr("serial_number", "A"), _attr("model", "X")]),
        201: _device(201, "antenna",
                     [_attr("serial_number", "B"), _attr("model", "Y")]),
        202: _device(202, "gnss_receiver", []),
        203: _device(203, "router", []),
    }
    client = _client_for(history_by_id)
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    assert report.audited_entities == 3  # station + 2 open GPS devices
    assert report.devices_skipped == 1   # router
    assert not report.has_violations


def test_report_carries_station_id_and_name(catalog_path: Path):
    station = _station(
        100,
        "Test Station Display Name",
        connections=[],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("date_start", "2010-01-01"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
        ],
    )
    client = _client_for({100: station})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    assert report.station_id == 100
    assert report.station_name == "Test Station Display Name"
