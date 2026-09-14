"""Unit tests for :mod:`tostools.audit_missing_attributes`.

No network — :class:`tostools.api.tos_client.TOSClient` is mocked. Catalog
fixtures are written to ``tmp_path`` as minimal in-memory YAML; the goal is
focused per-rule coverage of the walker, not a full integration test.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from tostools.audit_missing_attributes import (
    FILL_DATE_PLACEHOLDER,
    FILL_VALUE_PLACEHOLDER,
    MissingAttributeViolation,
    audit_station_missing_attributes,
    format_triage_file,
    load_missing_suppressions,
)

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
_CATALOG_YAML = dedent(
    """
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

      azimuth:
        # Recommended tier — a soft reminder, not a hard violation.
        icelandic_label: Áttarhorn
        classification: mutable
        gps_required_for: []
        gps_recommended_for: [antenna]
        default_value: "0.0"
        applies_to: [antenna]
        gps_relevance: "yes"

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
    """
).strip()


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    p = tmp_path / "attribute_codes.yaml"
    p.write_text(_CATALOG_YAML, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Walker — basic cases
# ---------------------------------------------------------------------------


def test_recommended_tier_is_reminder_not_violation(catalog_path: Path):
    """An antenna missing a ``gps_recommended_for`` code (azimuth) is flagged
    with severity 'recommended' — a logged reminder — and does NOT set
    has_violations (only required-tier misses fail the audit)."""
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
    # Antenna with all REQUIRED attrs present but no azimuth (recommended).
    antenna = _device(
        200,
        "antenna",
        [_attr("serial_number", "ANT9"), _attr("model", "TRM41249.00")],
    )
    client = _client_for({100: station, 200: antenna})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    rec = report.recommended_missing
    assert [v.code for v in rec] == ["azimuth"]
    assert rec[0].severity == "recommended"
    assert rec[0].suggested_value == "0.0"
    # Recommended-only misses must NOT fail the audit.
    assert not report.has_violations
    assert not report.hard_violations


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

    assert not any(
        v.code == "subtype" and v.scope == "devices" for v in report.violations
    )


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
        200: _device(
            200, "gnss_receiver", [_attr("serial_number", "A"), _attr("model", "X")]
        ),
        201: _device(
            201, "antenna", [_attr("serial_number", "B"), _attr("model", "Y")]
        ),
        202: _device(202, "gnss_receiver", []),
        203: _device(203, "router", []),
    }
    client = _client_for(history_by_id)
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )

    assert report.audited_entities == 3  # station + 2 open GPS devices
    assert report.devices_skipped == 1  # router
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


# ---------------------------------------------------------------------------
# Suppression file parsing
# ---------------------------------------------------------------------------


def test_load_missing_suppressions_parses_2tuple(tmp_path: Path):
    """One SUPPRESS line per known-good gap. Key is (id_entity, code)
    — no date anchor since 'missing' has no date_from."""
    p = tmp_path / "missing_attributes.txt"
    p.write_text(
        "# Header comment\n"
        "SUPPRESS 100 wigos_number\n"
        "SUPPRESS 200 firmware_version  # inline comment\n"
        "\n"
        "SUPPRESS 300 date_start\n",
        encoding="utf-8",
    )
    supps, errors, path = load_missing_suppressions(p)
    assert errors == []
    assert path == p
    assert (100, "wigos_number") in supps
    assert (200, "firmware_version") in supps
    assert (300, "date_start") in supps


def test_load_missing_suppressions_file_not_found_is_silent(tmp_path: Path):
    """The suppression file is opt-in — file-not-found returns empty,
    not an exception."""
    supps, errors, path = load_missing_suppressions(tmp_path / "nope.txt")
    assert supps == {}
    assert errors == []
    assert path == tmp_path / "nope.txt"


def test_load_missing_suppressions_collects_malformed_lines(tmp_path: Path):
    """Errors are collected, not raised — operator can fix every typo in
    one cycle."""
    p = tmp_path / "missing_attributes.txt"
    p.write_text(
        "SUPPRESS 100 wigos_number\n"
        "OOPS 200 wrong_verb\n"
        "SUPPRESS notanint code\n"
        "SUPPRESS 300\n"  # too few args
        "SUPPRESS 400 ok_code\n",
        encoding="utf-8",
    )
    supps, errors, _ = load_missing_suppressions(p)
    assert (100, "wigos_number") in supps
    assert (400, "ok_code") in supps
    assert len(supps) == 2
    assert len(errors) == 3
    messages = " ".join(e.message for e in errors)
    assert "expected line to start with 'SUPPRESS'" in messages
    assert "id_entity must be int" in messages
    assert "requires 2 arguments" in messages


def test_walker_filters_suppressed_violations(catalog_path: Path, tmp_path: Path):
    """Suppressed entries are removed from ``violations`` but preserved on
    ``suppressed`` so verbose output can show what was silenced."""
    station = _station(
        100,
        "Test Station",
        connections=[],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
            # date_start missing → will be flagged
        ],
    )
    supp_file = tmp_path / "missing_attributes.txt"
    supp_file.write_text("SUPPRESS 100 date_start\n", encoding="utf-8")

    client = _client_for({100: station})
    report = audit_station_missing_attributes(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        suppressions_path=supp_file,
    )

    assert not report.has_violations
    assert report.suppressed_count == 1
    assert report.suppressed[0].violation.code == "date_start"
    assert report.suppressed[0].line_no == 1


def test_walker_bypasses_suppressions_when_disabled(catalog_path: Path, tmp_path: Path):
    """``use_suppressions=False`` reports every hit regardless of the
    suppression file content."""
    station = _station(
        100,
        "Test Station",
        connections=[],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("subtype", "GPS stöð"),
            _attr("altitude", 12.3),
        ],
    )
    supp_file = tmp_path / "missing_attributes.txt"
    supp_file.write_text("SUPPRESS 100 date_start\n", encoding="utf-8")

    client = _client_for({100: station})
    report = audit_station_missing_attributes(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        suppressions_path=supp_file,
        use_suppressions=False,
    )

    assert report.has_violations
    assert any(v.code == "date_start" for v in report.violations)
    assert report.suppressions_disabled is True


# ---------------------------------------------------------------------------
# Triage file emission
# ---------------------------------------------------------------------------


def _station_with_two_gaps(catalog_path: Path):
    """Build a small fixture exercising both station + device gaps and
    both default-value + no-default cases."""
    station = _station(
        100,
        "Test Station",
        connections=[_conn(200, time_from="2015-06-01")],
        extra_attrs=[
            _attr("marker", "tst1"),
            _attr("altitude", 12.3),
            # subtype missing → has default
            # date_start missing → no default
        ],
    )
    device = _device(
        200,
        "gnss_receiver",
        [_attr("model", "POLARX5")],
        # serial_number missing → no default
    )
    client = _client_for({100: station, 200: device})
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path
    )
    return report


def test_triage_emits_header_and_workflow_block(catalog_path: Path):
    report = _station_with_two_gaps(catalog_path)
    out = format_triage_file(
        report,
        audit_command="tos audit missing-attributes tst1",
        generated_at="2026-05-20T00:00:00+00:00",
    )
    assert "tos audit missing-attributes — triage action file" in out
    assert "Generated:  2026-05-20T00:00:00+00:00" in out
    assert "tos audit missing-attributes tst1" in out
    assert f"Violations: {len(report.violations)}" in out
    assert "tos audit apply <file>" in out


def test_triage_default_value_pre_filled_in_action_line(catalog_path: Path):
    """A code with a catalog default_value renders its value into the
    ACTION line — no <FILL_VALUE> placeholder needed."""
    report = _station_with_two_gaps(catalog_path)
    out = format_triage_file(report, generated_at="2026-05-20T00:00:00+00:00")
    # subtype → default "GPS stöð" → shlex-quoted to "'GPS stöð'"
    assert "#ACTION 100 add-attribute subtype 'GPS stöð'" in out


def test_triage_fill_value_placeholder_when_no_default(catalog_path: Path):
    """date_start has no catalog default → triage emits the placeholder."""
    report = _station_with_two_gaps(catalog_path)
    out = format_triage_file(report, generated_at="2026-05-20T00:00:00+00:00")
    # date_start has no default, and station violations have no date hint
    # → both placeholders appear.
    assert (
        f"#ACTION 100 add-attribute date_start {FILL_VALUE_PLACEHOLDER} "
        f"{FILL_DATE_PLACEHOLDER}" in out
    )


def test_triage_device_date_hint_used_when_present(catalog_path: Path):
    """For device violations, the earliest open-join time_from is used
    as the date hint — no placeholder."""
    report = _station_with_two_gaps(catalog_path)
    out = format_triage_file(report, generated_at="2026-05-20T00:00:00+00:00")
    # serial_number missing → no default value, but join date 2015-06-01
    # used as date hint.
    assert (
        f"#ACTION 200 add-attribute serial_number {FILL_VALUE_PLACEHOLDER} "
        "2015-06-01" in out
    )


def test_triage_action_lines_commented_by_default(catalog_path: Path):
    """All ACTION lines must start with '#' so a freshly-emitted triage
    file is a no-op when fed through `tos audit apply` — the operator
    must explicitly uncomment what they want to fire."""
    report = _station_with_two_gaps(catalog_path)
    out = format_triage_file(report, generated_at="2026-05-20T00:00:00+00:00")
    action_lines = [
        line
        for line in out.splitlines()
        if "add-attribute" in line and line.strip().startswith(("ACTION", "#ACTION"))
    ]
    assert action_lines, "expected at least one ACTION line"
    for line in action_lines:
        assert line.startswith("#ACTION")


def test_triage_suppress_hint_per_violation(catalog_path: Path):
    """Every violation block carries a SUPPRESS hint the operator can
    paste into data/audit_suppressions/missing_attributes.txt."""
    report = _station_with_two_gaps(catalog_path)
    out = format_triage_file(report, generated_at="2026-05-20T00:00:00+00:00")
    assert "# (or suppress: SUPPRESS 100 subtype)" in out
    assert "# (or suppress: SUPPRESS 100 date_start)" in out
    assert "# (or suppress: SUPPRESS 200 serial_number)" in out


def test_triage_empty_report_yields_header_only(catalog_path: Path):
    """Clean station → header + 'no violations' note, no ACTION lines."""
    station = _station(
        100,
        "Clean Station",
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
    out = format_triage_file(report, generated_at="2026-05-20T00:00:00+00:00")
    assert "(no violations — nothing to triage)" in out
    # No actual ACTION lines (uncommented or commented-out) — the header's
    # format explanation contains the literal "add-attribute" as documentation,
    # but no `#ACTION ...` violation lines should be present.
    action_lines = [line for line in out.splitlines() if line.startswith("#ACTION")]
    assert action_lines == []


def test_triage_groups_station_first_then_devices(catalog_path: Path):
    """Station entity block always precedes device blocks so the operator
    reads the file linearly."""
    report = _station_with_two_gaps(catalog_path)
    out = format_triage_file(report, generated_at="2026-05-20T00:00:00+00:00")
    station_idx = out.index("# --- geophysical id_entity=100")
    device_idx = out.index("# --- gnss_receiver id_entity=200")
    assert station_idx < device_idx


def test_occupation_end_date_midday_end_bumps_to_next_day():
    """A sub-day occupation (e.g. HAMR antenna 4540: 1995-09-01T00:00 ->
    1995-09-01T16:30, the first half of a split session) must yield an
    EXCLUSIVE end of the next day, else the half-open window is the empty
    [09-01, 09-01) and no same-day fill can ever satisfy it.
    """
    from tostools.audit_missing_attributes import _occupation_end_date

    assert _occupation_end_date({"time_to": "1995-09-01T16:30:00"}) == "1995-09-02"
    # midnight end: date itself is the correct exclusive end
    assert _occupation_end_date({"time_to": "1995-09-10T00:00:00"}) == "1995-09-10"
    # open join
    assert _occupation_end_date({}) is None


def test_audit_windows_use_midday_bump():
    from tostools.audit_missing_attributes import _occupation_end_date as f

    j = {"time_from": "1995-09-01T00:00:00", "time_to": "1995-09-01T16:30:00"}
    assert f(j) == "1995-09-02"


# ---------------------------------------------------------------------------
# station.info unavailability guard
#
# An absent station.info is not benign: with no field-record oracle every
# suggestion degrades to the catalog default, and antenna_height — which has NO
# catalog default — becomes <FILL_VALUE>. That used to be silent, so a triage
# generated where the GAMIT mount is missing (rek-d01, 2026-09-14: HEID, HRIC,
# HS02, HUSM, HVEL) looked exactly like one where the field record was consulted
# and had nothing to say.
# ---------------------------------------------------------------------------


def test_resolve_for_audit_returns_note_when_nothing_resolves(monkeypatch):
    """No station.info anywhere -> (None, note) naming the chain that was tried."""
    from tostools.standards import gamit_station_info as gsi

    monkeypatch.setattr(gsi, "resolve_station_info", lambda override=None: None)
    src, note = gsi.resolve_for_audit()

    assert src is None
    assert note is not None
    # the note must be actionable: what was tried, what is lost, what to do
    for token in ("UNAVAILABLE", "antenna_height", "<FILL_VALUE>", "--station-info"):
        assert token in note


def test_resolve_for_audit_note_is_none_on_success(monkeypatch, tmp_path):
    """A resolved, READABLE copy yields no note — the guard must not cry wolf."""
    from tostools.standards import gamit_station_info as gsi

    real = tmp_path / "station.info"
    real.write_text("")
    fake = gsi.StationInfoSource(real, "mount")
    monkeypatch.setattr(gsi, "resolve_station_info", lambda override=None: fake)
    src, note = gsi.resolve_for_audit()

    assert src is fake
    assert note is None


def test_resolve_for_audit_flags_a_resolved_but_missing_path(monkeypatch, tmp_path):
    """resolve_station_info returns override/env/cfg paths WITHOUT checking they
    exist, and the occupation loader swallows OSError -> []. A typo'd
    --station-info must therefore be caught here, not degrade silently."""
    from tostools.standards import gamit_station_info as gsi

    fake = gsi.StationInfoSource(tmp_path / "nope.info", "override")
    monkeypatch.setattr(gsi, "resolve_station_info", lambda override=None: fake)
    src, note = gsi.resolve_for_audit()

    assert src is None
    assert note is not None and "not a readable file" in note


def test_report_carries_station_info_path_and_note(catalog_path: Path):
    """The audit records which station.info backed it, and the degradation."""
    station = _station(
        100,
        "Test Station",
        connections=[],
        extra_attrs=[_attr("marker", "tst1"), _attr("subtype", "GPS stöð")],
    )
    client = _client_for({100: station})
    report = audit_station_missing_attributes(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        station_info_path=None,
        station_info_note="no station.info resolved (tried: ...)",
    )

    assert report.station_info_note == "no station.info resolved (tried: ...)"
    assert report.station_info_degraded is True


def test_report_is_not_degraded_without_a_note(catalog_path: Path):
    """Default (no note) = healthy; ``degraded`` must stay False."""
    station = _station(
        100,
        "Test Station",
        connections=[],
        extra_attrs=[_attr("marker", "tst1"), _attr("subtype", "GPS stöð")],
    )
    client = _client_for({100: station})
    report = audit_station_missing_attributes(client, id_entity=100, catalog_path=catalog_path)

    assert report.station_info_note is None
    assert report.station_info_degraded is False


def test_triage_file_warns_when_station_info_degraded(catalog_path: Path):
    """The triage file itself must carry the warning — that is the artifact an
    operator reads, and a bare <FILL_VALUE> is indistinguishable from a
    checked-but-empty field record."""
    station = _station(
        100,
        "Test Station",
        connections=[],
        extra_attrs=[_attr("marker", "tst1"), _attr("subtype", "GPS stöð")],
    )
    client = _client_for({100: station})
    report = audit_station_missing_attributes(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        station_info_note="no station.info resolved (tried: mount, packaged)",
    )
    report.violations.append(
        MissingAttributeViolation(
            id_entity=100,
            subtype="geophysical",
            name="Test Station",
            code="altitude",
            scope="stations",
            suggested_value=None,
            suggested_date_from=None,
            suggested_date_to=None,
            value_basis=None,
            severity="required",
        )
    )

    out = format_triage_file(report, generated_at="2026-09-14T00:00:00Z")

    assert "DEGRADED AUDIT" in out
    assert "UNAVAILABLE" in out
    assert "CATALOG DEFAULTS" in out


def test_triage_file_names_the_station_info_when_present(catalog_path: Path, tmp_path: Path):
    """A healthy run names the copy it used, and emits no warning."""
    station = _station(
        100,
        "Test Station",
        connections=[],
        extra_attrs=[_attr("marker", "tst1"), _attr("subtype", "GPS stöð")],
    )
    client = _client_for({100: station})
    si = tmp_path / "station.info"
    si.write_text("")
    report = audit_station_missing_attributes(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        station_info_path=si,
    )
    out = format_triage_file(report, generated_at="2026-09-14T00:00:00Z")

    assert "DEGRADED AUDIT" not in out
    assert f"# station.info: {si}" in out


def test_audit_flags_a_station_info_path_that_is_not_a_file(catalog_path: Path):
    """A direct caller passing a missing path must not get a silent pass."""
    station = _station(
        100,
        "Test Station",
        connections=[],
        extra_attrs=[_attr("marker", "tst1"), _attr("subtype", "GPS stöð")],
    )
    client = _client_for({100: station})
    report = audit_station_missing_attributes(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        station_info_path=Path("/nonexistent/station.info"),
    )

    assert report.station_info_degraded is True
    assert "not a readable file" in report.station_info_note


def test_audit_does_not_flag_a_valid_file_without_the_marker(
    catalog_path: Path, tmp_path: Path
):
    """A valid station.info with no occupation for this marker is legitimate —
    absence of an occupation is not absence of the oracle."""
    station = _station(
        100,
        "Test Station",
        connections=[],
        extra_attrs=[_attr("marker", "tst1"), _attr("subtype", "GPS stöð")],
    )
    client = _client_for({100: station})
    si = tmp_path / "station.info"
    si.write_text(" TST1  Somewhere  2006 222 00 00 00  9999 999 00 00 00  1.0000\n")
    report = audit_station_missing_attributes(
        client, id_entity=100, catalog_path=catalog_path, station_info_path=si
    )

    assert report.station_info_note is None
    assert report.station_info_path == si


# ---------------------------------------------------------------------------
# _occupation_for — serial + DATE selection
#
# The same antenna can be re-installed with a DIFFERENT height (ISAK 190269:
# 1.0047 then 1.0358), and on a long-history station every receiver era adds
# another occupation. The old implementation matched an EXACT `time_from` and
# otherwise returned candidates[0] — the EARLIEST occupation — so any TOS
# window beginning part-way through an occupation silently got the wrong era's
# height. That is the common case, not the edge case: HVOL's 2019/2021/2026
# windows all resolved to its 1999 occupation (1.0443) instead of the covering
# 0.9780 one.
# ---------------------------------------------------------------------------


def _occ(sn, height, start, end=None):
    from tostools.standards.gamit_station_info import Occupation

    return Occupation(
        marker="TST1",
        station_name="Somewhere",
        time_from=datetime.fromisoformat(start),
        time_to=datetime.fromisoformat(end) if end else None,
        receiver_type="TRIMBLE 4700",
        receiver_sn="1",
        vers="1",
        swver="1",
        antenna_type="TRM29659.00",
        dome="SCIS",
        antenna_sn=sn,
        antenna_height=height,
        htcod="DHARP",
        antenna_north="0.0000",
        antenna_east="0.0000",
    )


# The ISAK shape: one antenna, two occupations, DIFFERENT heights.
_TWO = [
    _occ("190269", "1.0047", "2002-01-24", "2005-01-18"),
    _occ("190269", "1.0358", "2005-01-18", None),
]


def test_occupation_for_picks_the_covering_occupation():
    from tostools.audit_missing_attributes import _occupation_for

    assert _occupation_for(_TWO, "190269", "2003-06-01").antenna_height == "1.0047"
    assert _occupation_for(_TWO, "190269", "2010-06-01").antenna_height == "1.0358"


def test_occupation_for_does_not_flip_to_the_earliest_after_the_last_start():
    """The regression: a window INSIDE the final (open) occupation — the old
    code returned candidates[0] and produced the 1999-era height."""
    from tostools.audit_missing_attributes import _occupation_for

    for probe in ("2005-01-18", "2019-10-16", "2021-04-15", "2026-09-01"):
        assert (
            _occupation_for(_TWO, "190269", probe).antenna_height == "1.0358"
        ), probe


def test_occupation_for_exact_start_wins():
    from tostools.audit_missing_attributes import _occupation_for

    assert _occupation_for(_TWO, "190269", "2005-01-18").antenna_height == "1.0358"


def test_occupation_for_date_before_every_occupation_returns_the_earliest():
    from tostools.audit_missing_attributes import _occupation_for

    assert _occupation_for(_TWO, "190269", "1990-01-01").antenna_height == "1.0047"


def test_occupation_for_no_date_returns_the_earliest():
    """Back-compat: callers that pass no date keep the old first-match answer."""
    from tostools.audit_missing_attributes import _occupation_for

    assert _occupation_for(_TWO, "190269", None).antenna_height == "1.0047"


def test_occupation_for_unknown_or_missing_serial_returns_none():
    from tostools.audit_missing_attributes import _occupation_for

    assert _occupation_for(_TWO, "999999", "2010-06-01") is None
    assert _occupation_for(_TWO, None, "2010-06-01") is None
    assert _occupation_for(_TWO, "", "2010-06-01") is None


def test_occupation_for_accepts_date_only_and_iso_datetime_probes():
    """TOS mixes '2002-01-01' and '2002-01-01 00:00:00' in the same payloads."""
    from tostools.audit_missing_attributes import _occupation_for

    assert _occupation_for(_TWO, "190269", "2010-06-01").antenna_height == "1.0358"
    assert (
        _occupation_for(_TWO, "190269", "2010-06-01 00:00:00").antenna_height
        == "1.0358"
    )
    assert (
        _occupation_for(_TWO, "190269", "2010-06-01T12:34:56").antenna_height
        == "1.0358"
    )


def test_occupation_for_nested_spans_pick_the_most_specific():
    """If spans ever nest, the latest-starting covering occupation wins."""
    from tostools.audit_missing_attributes import _occupation_for

    nested = [
        _occ("1", "1.0", "2000-01-01", "2020-01-01"),
        _occ("1", "2.0", "2010-01-01", "2012-01-01"),
    ]
    assert _occupation_for(nested, "1", "2011-01-01").antenna_height == "2.0"
    assert _occupation_for(nested, "1", "2015-01-01").antenna_height == "1.0"


def test_occupation_for_matches_serial_exactly_not_by_prefix():
    from tostools.audit_missing_attributes import _occupation_for

    occs = [_occ("17042", "1.0", "2000-01-01"), _occ("170423", "2.0", "2000-01-01")]
    assert _occupation_for(occs, "170423", "2010-01-01").antenna_height == "2.0"


def test_occupation_for_is_order_independent():
    """`min()` must be chronological, not 'first in the list'. TOS payloads and
    station.info files are not guaranteed to arrive oldest-first, so a plain
    candidates[0] fallback would depend on file ordering."""
    from tostools.audit_missing_attributes import _occupation_for

    reversed_two = list(reversed(_TWO))
    assert _occupation_for(reversed_two, "190269", "1990-01-01").antenna_height == "1.0047"
    assert _occupation_for(reversed_two, "190269", None).antenna_height == "1.0047"
    # and the dated paths must not care about order either
    assert _occupation_for(reversed_two, "190269", "2003-06-01").antenna_height == "1.0047"
    assert _occupation_for(reversed_two, "190269", "2020-06-01").antenna_height == "1.0358"


def test_occupation_for_date_in_a_gap_after_the_last_end_picks_the_latest():
    """After every occupation has ENDED there is no covering span, so the
    'latest starting on or before' branch is the only correct answer -- the
    earliest-fallback would reach back to the first era instead."""
    from tostools.audit_missing_attributes import _occupation_for

    ended = [
        _occ("7", "1.0", "2000-01-01", "2005-01-01"),
        _occ("7", "2.0", "2005-01-01", "2010-01-01"),
    ]
    assert _occupation_for(ended, "7", "2015-01-01").antenna_height == "2.0"
    # and exactly ON the exclusive end date of the last one
    assert _occupation_for(ended, "7", "2010-01-01").antenna_height == "2.0"


def test_occupation_for_treats_time_to_as_exclusive():
    """Spans are ``[time_from, time_to)`` -- a date exactly ON an occupation's
    end belongs to the NEXT one, not to it. Nested spans make that observable
    (with adjacent spans both conventions pick the same answer)."""
    from tostools.audit_missing_attributes import _occupation_for

    nested = [
        _occ("1", "1.0", "2000-01-01", "2020-01-01"),
        _occ("1", "2.0", "2010-01-01", "2012-01-01"),
    ]
    # 2012-01-01 is the inner occupation's EXCLUSIVE end -> it no longer covers
    assert _occupation_for(nested, "1", "2012-01-01").antenna_height == "1.0"
    # the day before is still the inner one
    assert _occupation_for(nested, "1", "2011-12-31").antenna_height == "2.0"
