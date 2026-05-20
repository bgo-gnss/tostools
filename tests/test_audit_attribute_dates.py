"""Unit tests for :mod:`tostools.audit_attribute_dates`.

No network — :class:`tostools.api.tos_client.TOSClient` is mocked. Catalog
fixtures are written to ``tmp_path`` as minimal in-memory YAML; suppression
fixtures are written the same way and passed via ``suppressions_path``.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from tostools.audit_attribute_dates import (
    AttributeDateViolation,
    StationAttributeDateReport,
    SuppressedEntry,
    SuppressionParseError,
    _date_only,
    _earliest_attribute_date,
    _station_joins_by_device,
    audit_station_attribute_dates,
    classification_for,
    format_triage_file,
    load_catalog,
    load_catalog_scoped,
    load_suppressions,
    validate_codes_against_catalog,
)
from tostools.tos import _parse_action_file

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _attr(code: str, value, date_from: str, date_to=None):
    """Build one ``attributes[]`` entry shaped like TOS returns."""
    return {
        "code": code,
        "value": value,
        "date_from": date_from,
        "date_to": date_to,
    }


def _conn(id_child: int, time_from: str, time_to=None):
    """Build one ``children_connections[]`` entry shaped like TOS returns."""
    return {
        "id_entity_child": id_child,
        "time_from": time_from,
        "time_to": time_to,
    }


def _device(
    id_entity: int,
    subtype: str,
    attributes,
):
    return {
        "id_entity": id_entity,
        "code_entity_subtype": subtype,
        "attributes": list(attributes),
        "children_connections": [],
    }


def _station(id_entity: int, name: str, connections):
    return {
        "id_entity": id_entity,
        "code_entity_subtype": "geophysical",
        "attributes": [_attr("name", name, "2000-01-01")],
        "children_connections": list(connections),
    }


def _client_for(history_by_id):
    """Mock client whose ``get_entity_history`` dispatches by id."""
    client = MagicMock()
    client.get_entity_history.side_effect = lambda i: history_by_id.get(int(i))
    return client


# Minimal in-memory catalog covering everything the test suite exercises.
# Keeping this small + explicit beats parsing the full repo catalog in tests
# (the repo catalog is the integration-test contract; this is unit-level).
_CATALOG_YAML = dedent("""
    devices:
      serial_number:
        icelandic_label: Raðnúmer
        description: Physical device identity
        classification: inherent
        tos_required_for: [gnss_receiver, antenna]
        gps_required_for: [gnss_receiver, antenna, monument]
        applies_to: [gnss_receiver, antenna, monument]
        gps_relevance: "yes"

      model:
        icelandic_label: Tegund tækis
        description: Manufacturer/model
        classification: inherent
        tos_required_for: [gnss_receiver]
        applies_to: [gnss_receiver, antenna, monument]
        gps_relevance: "yes"

      firmware_version:
        icelandic_label: Útgáfa fastbúnaðar
        description: Firmware
        classification: mutable
        applies_to: [gnss_receiver]
        gps_relevance: "yes"

      antenna_offset_north:
        icelandic_label: Loftnetshliðrun norður
        description: Antenna offset north
        classification:
          antenna: mutable
          monument: inherent
        applies_to: [antenna, monument]
        gps_relevance: "yes"

      todo_code:
        icelandic_label: TODO
        description: Not yet classified
        classification: TODO
        applies_to: [gnss_receiver]
        gps_relevance: "yes"

      not_relevant_code:
        icelandic_label: Hiti
        description: Seismic-domain attribute
        classification: inherent
        applies_to: [seismometer]
        gps_relevance: "no"

    locations:
      address:
        icelandic_label: Heimilisfang
        description: Address
        classification: mutable
        applies_to: [station]
        gps_relevance: "no"

    stations:
      marker:
        icelandic_label: Stöðvarmerki
        description: Station marker
        classification: inherent
        applies_to: [station]
        gps_relevance: "no"
    """).strip()


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    p = tmp_path / "attribute_codes.yaml"
    p.write_text(_CATALOG_YAML, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# _date_only
# ---------------------------------------------------------------------------


def test_date_only_bare_date_passthrough():
    assert _date_only("2014-10-17") == "2014-10-17"


def test_date_only_strips_space_separated_time():
    assert _date_only("2014-10-17 00:00:00") == "2014-10-17"


def test_date_only_strips_t_separated_time():
    assert _date_only("2014-10-17T08:30:00") == "2014-10-17"


# ---------------------------------------------------------------------------
# classification_for
# ---------------------------------------------------------------------------


def test_classification_scalar_inherent_applies():
    entry = {"classification": "inherent", "applies_to": ["gnss_receiver", "antenna"]}
    assert classification_for(entry, "gnss_receiver") == "inherent"
    assert classification_for(entry, "antenna") == "inherent"


def test_classification_scalar_excluded_by_applies_to_returns_none():
    entry = {"classification": "inherent", "applies_to": ["gnss_receiver"]}
    assert classification_for(entry, "monument") is None


def test_classification_per_subtype_dict_resolves():
    """The polymorphic `antenna_offset_north` shape — different classification
    per subtype — is the catalog's hardest case. Verify both directions."""
    entry = {
        "classification": {"antenna": "mutable", "monument": "inherent"},
        "applies_to": ["antenna", "monument"],
    }
    assert classification_for(entry, "antenna") == "mutable"
    assert classification_for(entry, "monument") == "inherent"


def test_classification_dict_missing_subtype_returns_none():
    """Dict form, but the device's subtype isn't keyed → skip silently."""
    entry = {"classification": {"antenna": "mutable"}, "applies_to": ["antenna"]}
    assert classification_for(entry, "monument") is None


def test_classification_todo_returns_none():
    """Unclassified entries (operator hasn't reviewed) are skipped, not
    treated as inherent — they neither flag nor warn."""
    entry = {"classification": "TODO", "applies_to": ["gnss_receiver"]}
    assert classification_for(entry, "gnss_receiver") is None


def test_classification_missing_returns_none():
    assert classification_for({}, "gnss_receiver") is None


# ---------------------------------------------------------------------------
# load_catalog
# ---------------------------------------------------------------------------


def test_load_catalog_flattens_scopes(catalog_path: Path):
    """Devices + locations + stations entries should all be reachable by code,
    each tagged with its source ``_scope``."""
    catalog = load_catalog(catalog_path)
    assert catalog["serial_number"]["_scope"] == "devices"
    assert catalog["address"]["_scope"] == "locations"
    assert catalog["marker"]["_scope"] == "stations"


def test_load_catalog_devices_wins_on_collision(tmp_path: Path):
    """If the same code appears in two scopes, devices wins (declared first
    in the scope iteration order). Documents the contract for future YAML
    authors so they know which scope is canonical."""
    yaml_text = dedent("""
        devices:
          duplicate_code:
            classification: inherent
            applies_to: [gnss_receiver]
            gps_relevance: "yes"
            why: "from devices"
        locations:
          duplicate_code:
            classification: mutable
            applies_to: [station]
            gps_relevance: "no"
            why: "from locations"
        """).strip()
    p = tmp_path / "cat.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    catalog = load_catalog(p)
    assert catalog["duplicate_code"]["_scope"] == "devices"
    assert catalog["duplicate_code"]["why"] == "from devices"


def test_load_catalog_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_catalog(tmp_path / "does-not-exist.yaml")


# ---------------------------------------------------------------------------
# load_catalog_scoped
# ---------------------------------------------------------------------------


def test_load_catalog_scoped_preserves_scopes(catalog_path: Path):
    """The scoped view keeps the three scopes separate — Layer 6 walker
    iterates rules per-entity-type, so cross-scope code collisions must
    stay distinct (unlike the flat view where devices wins)."""
    scoped = load_catalog_scoped(catalog_path)
    assert set(scoped.keys()) == {"devices", "locations", "stations"}
    assert "serial_number" in scoped["devices"]
    assert "address" in scoped["locations"]
    assert "marker" in scoped["stations"]


def test_load_catalog_scoped_keeps_cross_scope_collisions_distinct(tmp_path: Path):
    """When the same code appears in two scopes, each entry survives under
    its own scope key — the regression the rename + scoped view exists to
    fix (TOS uses ``subtype`` on both stations and devices)."""
    yaml_text = dedent("""
        devices:
          subtype:
            classification: TODO
            applies_to: [gnss_receiver, antenna, radome, monument]
            gps_relevance: "no"
            why: "from devices"
        stations:
          subtype:
            classification: inherent
            tos_required_for: [geophysical]
            gps_required_for: [geophysical]
            default_value: "GPS stöð"
            applies_to: [geophysical]
            gps_relevance: "yes"
            why: "from stations"
        """).strip()
    p = tmp_path / "cat.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    scoped = load_catalog_scoped(p)
    assert scoped["devices"]["subtype"]["why"] == "from devices"
    assert scoped["stations"]["subtype"]["why"] == "from stations"
    assert scoped["stations"]["subtype"]["default_value"] == "GPS stöð"


def test_load_catalog_scoped_attaches_scope_to_each_entry(catalog_path: Path):
    """Parity with the flat view — every entry carries ``_scope``."""
    scoped = load_catalog_scoped(catalog_path)
    assert scoped["devices"]["serial_number"]["_scope"] == "devices"
    assert scoped["locations"]["address"]["_scope"] == "locations"
    assert scoped["stations"]["marker"]["_scope"] == "stations"


def test_load_catalog_scoped_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_catalog_scoped(tmp_path / "does-not-exist.yaml")


def test_load_catalog_derived_from_scoped(catalog_path: Path):
    """The flat view is a flatten of the scoped view — they must agree on
    every code the flat view exposes."""
    scoped = load_catalog_scoped(catalog_path)
    flat = load_catalog(catalog_path)
    for code, entry in flat.items():
        scope = entry["_scope"]
        assert scoped[scope][code] == entry


# ---------------------------------------------------------------------------
# _earliest_attribute_date + _station_joins_by_device
# ---------------------------------------------------------------------------


def test_earliest_attribute_date_min_across_codes():
    history = {
        "attributes": [
            _attr("serial_number", "X", "2014-10-17 00:00:00"),
            _attr("model", "Y", "2002-01-01"),
            _attr("status", "virkt", "2015-03-01T08:00:00"),
        ]
    }
    assert _earliest_attribute_date(history) == "2002-01-01"


def test_earliest_attribute_date_none_when_no_attributes():
    assert _earliest_attribute_date({"attributes": []}) is None


def test_earliest_attribute_date_skips_missing_date_from():
    history = {"attributes": [_attr("code", "v", None)]}  # type: ignore[arg-type]
    assert _earliest_attribute_date(history) is None


def test_station_joins_by_device_groups_multiple_visits():
    """Devices can rejoin a station after a stint elsewhere — both joins
    must be preserved so the caller can pick the earliest as anchor."""
    station = _station(
        100,
        "RHOF",
        [
            _conn(200, "2010-01-01", "2012-06-01"),
            _conn(200, "2014-01-01"),
            _conn(300, "2015-01-01"),
        ],
    )
    grouped = _station_joins_by_device(station)
    assert sorted(grouped.keys()) == [200, 300]
    assert len(grouped[200]) == 2
    assert len(grouped[300]) == 1


# ---------------------------------------------------------------------------
# load_suppressions
# ---------------------------------------------------------------------------


def test_load_suppressions_missing_file_silent(tmp_path: Path):
    """The suppression file is opt-in. File-not-found returns empty, no
    error — so a fresh checkout audits without complaint."""
    path = tmp_path / "missing.txt"
    suppressions, errors, resolved = load_suppressions(path)
    assert suppressions == {}
    assert errors == []
    assert resolved == path


def test_load_suppressions_parses_multiple_lines(tmp_path: Path):
    p = tmp_path / "supp.txt"
    p.write_text(
        dedent("""
            # comment
            SUPPRESS 4773 serial_number 2014-10-17  # ARHO Ashtech

            SUPPRESS 4501 serial_number 2014-10-17
            """),
        encoding="utf-8",
    )
    suppressions, errors, _ = load_suppressions(p)
    assert errors == []
    # Line numbers are 1-indexed against the file contents (blank prefix line = 1).
    assert (4773, "serial_number", "2014-10-17") in suppressions
    assert (4501, "serial_number", "2014-10-17") in suppressions


def test_load_suppressions_normalises_pasted_datetime(tmp_path: Path):
    """Operator pastes a full ISO datetime from `tos audit show`. The parser
    normalises to YYYY-MM-DD so the lookup matches the date-only form the
    audit uses internally — without this, the line would silently fail to
    suppress anything."""
    p = tmp_path / "supp.txt"
    p.write_text(
        "SUPPRESS 4773 serial_number 2014-10-17T00:00:00\n",
        encoding="utf-8",
    )
    suppressions, errors, _ = load_suppressions(p)
    assert errors == []
    assert (4773, "serial_number", "2014-10-17") in suppressions


def test_load_suppressions_collects_not_suppress_verb(tmp_path: Path):
    p = tmp_path / "supp.txt"
    p.write_text("LOL 4773 serial_number 2014-10-17\n", encoding="utf-8")
    suppressions, errors, _ = load_suppressions(p)
    assert suppressions == {}
    assert len(errors) == 1
    assert errors[0].line_no == 1
    assert "SUPPRESS" in errors[0].message


def test_load_suppressions_collects_too_few_tokens(tmp_path: Path):
    p = tmp_path / "supp.txt"
    p.write_text("SUPPRESS 4773 serial_number\n", encoding="utf-8")
    suppressions, errors, _ = load_suppressions(p)
    assert suppressions == {}
    assert len(errors) == 1
    assert "requires 3 arguments" in errors[0].message


def test_load_suppressions_collects_bad_id_entity(tmp_path: Path):
    p = tmp_path / "supp.txt"
    p.write_text("SUPPRESS NOTANID serial_number 2014-10-17\n", encoding="utf-8")
    suppressions, errors, _ = load_suppressions(p)
    assert suppressions == {}
    assert len(errors) == 1
    assert "int" in errors[0].message


def test_load_suppressions_collects_bad_date(tmp_path: Path):
    p = tmp_path / "supp.txt"
    p.write_text("SUPPRESS 4773 serial_number not-a-date\n", encoding="utf-8")
    suppressions, errors, _ = load_suppressions(p)
    assert suppressions == {}
    assert len(errors) == 1
    assert "YYYY-MM-DD" in errors[0].message


def test_load_suppressions_collects_all_errors_at_once(tmp_path: Path):
    """Multiple typos in one file → one fix cycle, not many. Mirrors
    ``_parse_action_file`` collect-and-report-all behaviour."""
    p = tmp_path / "supp.txt"
    p.write_text(
        dedent("""
            SUPPRESS NOTANID serial_number 2014-10-17
            SUPPRESS 4501 serial_number not-a-date
            LOL hello world
            SUPPRESS 4773 serial_number 2014-10-17
            """),
        encoding="utf-8",
    )
    suppressions, errors, _ = load_suppressions(p)
    assert len(errors) == 3
    # The valid line still produces a parsed entry.
    assert (4773, "serial_number", "2014-10-17") in suppressions


# ---------------------------------------------------------------------------
# audit_station_attribute_dates — end-to-end with mocked TOSClient
# ---------------------------------------------------------------------------


def test_audit_flags_inherent_period_later_than_attribute_anchor(catalog_path: Path):
    """Worked example pattern: serial_number stamped at data-entry date is
    later than the device's own date_start. The earliest_known anchor is
    the attribute (model dated 2002-01-01), and anchor_source reflects
    'attribute'."""
    device = _device(
        4773,
        "gnss_receiver",
        [
            _attr("serial_number", "13831", "2014-10-17 00:00:00"),
            _attr("model", "ASHTECH UZ-12", "2002-01-01 00:00:00"),
            _attr("firmware_version", "CJ12", "2002-01-01 00:00:00"),
        ],
    )
    station = _station(4233, "Árholt", [_conn(4773, "2002-01-01 00:00:00")])
    client = _client_for({4233: station, 4773: device})

    report = audit_station_attribute_dates(
        client, id_entity=4233, catalog_path=catalog_path
    )

    assert isinstance(report, StationAttributeDateReport)
    assert report.has_violations is True
    assert report.audited_devices == 1
    assert report.devices_skipped == 0
    assert len(report.violations) == 1

    v = report.violations[0]
    assert v.id_entity == 4773
    assert v.code == "serial_number"
    assert v.date_from == "2014-10-17"
    assert v.earliest_known == "2002-01-01"
    assert v.anchor_source == "attribute"


def test_audit_anchor_source_join_when_join_predates_attributes(
    catalog_path: Path,
):
    """When every attribute is co-stamped at the data-entry date but the
    station-side join carries an earlier time_from, the join IS the
    discriminator and anchor_source reports 'join'. This is the stricter
    variant from the destination doc."""
    device = _device(
        9001,
        "gnss_receiver",
        [
            _attr("serial_number", "SN-9001", "2014-10-17"),
            _attr("model", "TRIMBLE NETR9", "2014-10-17"),
        ],
    )
    station = _station(100, "TEST", [_conn(9001, "2010-05-01")])
    client = _client_for({100: station, 9001: device})

    report = audit_station_attribute_dates(
        client, id_entity=100, catalog_path=catalog_path
    )

    assert len(report.violations) == 2
    for v in report.violations:
        assert v.earliest_known == "2010-05-01"
        assert v.anchor_source == "join"


def test_audit_skips_mutable_codes_by_default(catalog_path: Path):
    """firmware_version is mutable; firmware bumps should not trip rule 3
    in the default inherent-only mode."""
    device = _device(
        9002,
        "gnss_receiver",
        [
            _attr("serial_number", "SN-9002", "2010-01-01"),
            _attr("firmware_version", "1.0", "2010-01-01", "2012-01-01"),
            _attr("firmware_version", "2.0", "2012-01-01"),  # later than earliest
        ],
    )
    station = _station(100, "TEST", [_conn(9002, "2010-01-01")])
    client = _client_for({100: station, 9002: device})

    report = audit_station_attribute_dates(
        client, id_entity=100, catalog_path=catalog_path
    )
    assert report.has_violations is False


def test_audit_include_mutable_surfaces_firmware(catalog_path: Path):
    """With --include-mutable, the later firmware period IS flagged."""
    device = _device(
        9003,
        "gnss_receiver",
        [
            _attr("serial_number", "SN-9003", "2010-01-01"),
            _attr("firmware_version", "1.0", "2010-01-01", "2012-01-01"),
            _attr("firmware_version", "2.0", "2012-01-01"),
        ],
    )
    station = _station(100, "TEST", [_conn(9003, "2010-01-01")])
    client = _client_for({100: station, 9003: device})

    report = audit_station_attribute_dates(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        include_mutable=True,
    )
    codes = sorted(v.code for v in report.violations)
    assert "firmware_version" in codes


def test_audit_devices_skipped_outside_subtypes(catalog_path: Path):
    """A station may have non-quartet children (digitizers, gps_clocks);
    those are counted as skipped, not failed."""
    receiver = _device(
        9004,
        "gnss_receiver",
        [_attr("serial_number", "SN-9004", "2010-01-01")],
    )
    digitizer = _device(
        9005,
        "digitizer",
        [_attr("serial_number", "DIG-X", "2010-01-01")],
    )
    station = _station(
        100,
        "TEST",
        [_conn(9004, "2010-01-01"), _conn(9005, "2010-01-01")],
    )
    client = _client_for({100: station, 9004: receiver, 9005: digitizer})

    report = audit_station_attribute_dates(
        client, id_entity=100, catalog_path=catalog_path
    )

    assert report.audited_devices == 1
    assert report.devices_skipped == 1


def test_audit_unknown_codes_captured_not_flagged(catalog_path: Path):
    """TOS attribute codes missing from the catalog should accumulate in
    unknown_codes for operator follow-up — not raise, not flag."""
    device = _device(
        9006,
        "gnss_receiver",
        [
            _attr("serial_number", "SN-9006", "2010-01-01"),
            _attr("never_seen_before_code", "v", "2020-01-01"),
        ],
    )
    station = _station(100, "TEST", [_conn(9006, "2010-01-01")])
    client = _client_for({100: station, 9006: device})

    report = audit_station_attribute_dates(
        client, id_entity=100, catalog_path=catalog_path
    )
    assert "never_seen_before_code" in report.unknown_codes
    # No violation for the unknown code.
    assert all(v.code != "never_seen_before_code" for v in report.violations)


def test_audit_suppression_routes_to_suppressed_not_violations(
    catalog_path: Path, tmp_path: Path
):
    """The DoD round-trip in unit form: SUPPRESS the rule-3 hit, expect it
    on report.suppressed (with file:lineno traceability) and gone from
    report.violations."""
    device = _device(
        4773,
        "gnss_receiver",
        [
            _attr("serial_number", "13831", "2014-10-17"),
            _attr("model", "ASHTECH UZ-12", "2002-01-01"),
        ],
    )
    station = _station(4233, "Árholt", [_conn(4773, "2002-01-01")])
    client = _client_for({4233: station, 4773: device})

    supp = tmp_path / "supp.txt"
    supp.write_text(
        "SUPPRESS 4773 serial_number 2014-10-17  # known-good\n",
        encoding="utf-8",
    )

    report = audit_station_attribute_dates(
        client,
        id_entity=4233,
        catalog_path=catalog_path,
        suppressions_path=supp,
    )

    assert report.violations == []
    assert report.has_violations is False  # reflects POST-filter list
    assert report.suppressed_count == 1
    entry = report.suppressed[0]
    assert isinstance(entry, SuppressedEntry)
    assert entry.violation.id_entity == 4773
    assert entry.violation.code == "serial_number"
    assert entry.line_no == 1
    assert entry.suppressions_path == supp


def test_audit_no_suppressions_flag_bypasses_file(catalog_path: Path, tmp_path: Path):
    """``use_suppressions=False`` ignores the file entirely; every rule-3
    hit lands in ``violations``."""
    device = _device(
        4773,
        "gnss_receiver",
        [
            _attr("serial_number", "13831", "2014-10-17"),
            _attr("model", "ASHTECH UZ-12", "2002-01-01"),
        ],
    )
    station = _station(4233, "Árholt", [_conn(4773, "2002-01-01")])
    client = _client_for({4233: station, 4773: device})

    supp = tmp_path / "supp.txt"
    supp.write_text("SUPPRESS 4773 serial_number 2014-10-17\n", encoding="utf-8")

    report = audit_station_attribute_dates(
        client,
        id_entity=4233,
        catalog_path=catalog_path,
        suppressions_path=supp,
        use_suppressions=False,
    )

    assert len(report.violations) == 1
    assert report.suppressed == []
    assert report.suppressions_disabled is True


def test_audit_suppression_typos_surface_on_report(catalog_path: Path, tmp_path: Path):
    """Parser errors propagate to ``report.suppressions_errors`` so the CLI
    can warn — the audit still runs with whatever valid entries were
    parsed."""
    device = _device(
        9007,
        "gnss_receiver",
        [
            _attr("serial_number", "SN-9007", "2014-10-17"),
            _attr("model", "M", "2002-01-01"),
        ],
    )
    station = _station(100, "TEST", [_conn(9007, "2002-01-01")])
    client = _client_for({100: station, 9007: device})

    supp = tmp_path / "supp.txt"
    supp.write_text(
        "SUPPRESS NOTANID serial_number 2014-10-17\n",
        encoding="utf-8",
    )

    report = audit_station_attribute_dates(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        suppressions_path=supp,
    )

    assert len(report.suppressions_errors) == 1
    assert isinstance(report.suppressions_errors[0], SuppressionParseError)
    # Audit still ran; the hit was not silenced (the typo line couldn't apply).
    assert len(report.violations) == 1


def test_audit_polymorphic_classification_antenna_vs_monument(
    catalog_path: Path,
):
    """`antenna_offset_north` is mutable on antenna, inherent on monument.
    A later-than-earliest period on monument should flag; the same shape
    on antenna should NOT (mutable, default-skip)."""
    antenna = _device(
        7001,
        "antenna",
        [
            _attr("serial_number", "ANT-1", "2010-01-01"),
            _attr("antenna_offset_north", "0.05", "2015-06-01"),
        ],
    )
    monument = _device(
        7002,
        "monument",
        [
            _attr("serial_number", "MON-1", "2010-01-01"),
            _attr("antenna_offset_north", "0.05", "2015-06-01"),
        ],
    )
    station = _station(
        100, "TEST", [_conn(7001, "2010-01-01"), _conn(7002, "2010-01-01")]
    )
    client = _client_for({100: station, 7001: antenna, 7002: monument})

    report = audit_station_attribute_dates(
        client, id_entity=100, catalog_path=catalog_path
    )

    offset_flags = [v for v in report.violations if v.code == "antenna_offset_north"]
    assert len(offset_flags) == 1
    assert offset_flags[0].subtype == "monument"


def test_audit_violations_sorted_deterministically(catalog_path: Path):
    """Output order: by (id_entity, code, date_from). Important for diff-
    friendly CI logs and for matching against suppression files."""
    devices = {
        # Insert in non-sorted id order to confirm sort happens at the end.
        100: _station(
            100,
            "TEST",
            [_conn(8001, "2002-01-01"), _conn(8000, "2002-01-01")],
        ),
        8000: _device(
            8000,
            "gnss_receiver",
            [
                _attr("serial_number", "S-8000", "2014-10-17"),
                _attr("model", "M", "2002-01-01"),
            ],
        ),
        8001: _device(
            8001,
            "gnss_receiver",
            [
                _attr("serial_number", "S-8001", "2014-10-17"),
                _attr("model", "M", "2002-01-01"),
            ],
        ),
    }
    client = _client_for(devices)
    report = audit_station_attribute_dates(
        client, id_entity=100, catalog_path=catalog_path
    )
    ids = [v.id_entity for v in report.violations]
    assert ids == sorted(ids)


def test_audit_violation_dataclass_is_frozen():
    """AttributeDateViolation is the suppression key carrier; freezing it
    keeps the (id_entity, code, date_from) triple stable across the
    detection→suppression pipeline."""
    v = AttributeDateViolation(
        id_entity=1,
        subtype="gnss_receiver",
        serial="SN",
        code="serial_number",
        date_from="2014-10-17",
        value="13831",
        earliest_known="2002-01-01",
        anchor_source="attribute",
    )
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        v.id_entity = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# format_triage_file (Layer 4)
# ---------------------------------------------------------------------------


def _report_with(violations) -> StationAttributeDateReport:
    return StationAttributeDateReport(
        station_id=4233,
        station_name="Árholt",
        audited_devices=len({v.id_entity for v in violations}),
        violations=list(violations),
    )


def _violation(
    id_entity: int,
    code: str,
    date_from: str,
    earliest_known: str,
    *,
    subtype: str = "gnss_receiver",
    serial: str = "SN-X",
    value: str = "VAL",
    anchor_source: str = "attribute",
) -> AttributeDateViolation:
    return AttributeDateViolation(
        id_entity=id_entity,
        subtype=subtype,
        serial=serial,
        code=code,
        date_from=date_from,
        value=value,
        earliest_known=earliest_known,
        anchor_source=anchor_source,
    )


def test_triage_empty_report_emits_header_with_no_action_lines():
    """A clean station should still produce a parseable file — header +
    explicit `# (no violations)` line, zero ACTIONs."""
    report = _report_with([])
    out = format_triage_file(report, generated_at="2026-05-19T00:00:00+00:00")
    assert "(no violations" in out
    # No uncommented ACTION lines anywhere.
    actions, errors = _parse_action_file(out)
    assert actions == []
    assert errors == []


def test_triage_emits_one_block_per_violation():
    """Each violation gets a comment block + one commented ACTION line."""
    v1 = _violation(4773, "serial_number", "2014-10-17", "2002-01-01")
    report = _report_with([v1])
    out = format_triage_file(report, generated_at="2026-05-19T00:00:00+00:00")
    assert "id_entity=4773" in out
    assert "violation: serial_number date_from=2014-10-17" in out
    # ACTION line is present but commented out.
    assert (
        "#ACTION 4773 patch-attribute-date serial_number 2014-10-17 2002-01-01" in out
    )


def test_triage_action_lines_are_commented_by_default():
    """The DoD requires commented-out ACTIONs — operator uncomments to
    apply. A parser round-trip should produce zero parsed actions."""
    v1 = _violation(4773, "serial_number", "2014-10-17", "2002-01-01")
    v2 = _violation(4501, "serial_number", "2014-10-17", "2012-08-28")
    report = _report_with([v1, v2])
    out = format_triage_file(report, generated_at="2026-05-19T00:00:00+00:00")
    actions, errors = _parse_action_file(out)
    assert errors == []
    assert actions == []


def test_triage_round_trip_after_uncommenting_one_action():
    """The triage file is the apply pipeline's input. After the operator
    strips a leading `#` from one ACTION line, _parse_action_file must
    parse it cleanly — this is the contract between layers 4 and the
    existing apply infrastructure."""
    v = _violation(4773, "serial_number", "2014-10-17", "2002-01-01")
    report = _report_with([v])
    out = format_triage_file(report, generated_at="2026-05-19T00:00:00+00:00")

    edited = out.replace(
        "#ACTION 4773 patch-attribute-date",
        "ACTION 4773 patch-attribute-date",
        1,
    )
    actions, errors = _parse_action_file(edited)
    assert errors == []
    assert len(actions) == 1
    a = actions[0]
    assert a.id_entity == 4773
    assert a.verb == "patch-attribute-date"
    assert a.args == ["serial_number", "2014-10-17", "2002-01-01"]


def test_triage_output_is_deterministic_for_same_inputs():
    """Same report + same timestamp must produce byte-identical output —
    diff-friendly so operators can commit triage files alongside
    suppressions and audit decisions over time."""
    violations = [
        _violation(4773, "serial_number", "2014-10-17", "2002-01-01"),
        _violation(4501, "serial_number", "2014-10-17", "2012-08-28"),
    ]
    report = _report_with(violations)
    a = format_triage_file(report, generated_at="2026-05-19T00:00:00+00:00")
    b = format_triage_file(report, generated_at="2026-05-19T00:00:00+00:00")
    assert a == b


def test_triage_groups_by_device_within_sort_order():
    """Violations on the same device should appear together. Devices
    appear in id_entity order."""
    violations = [
        _violation(
            4773,
            "serial_number",
            "2014-10-17",
            "2002-01-01",
            subtype="gnss_receiver",
            serial="13831",
        ),
        _violation(
            4501,
            "serial_number",
            "2014-10-17",
            "2012-08-28",
            subtype="antenna",
            serial="1441047035",
        ),
        _violation(
            4501,
            "model",
            "2014-10-17",
            "2012-08-28",
            subtype="antenna",
            serial="1441047035",
        ),
    ]
    report = _report_with(violations)
    out = format_triage_file(report, generated_at="2026-05-19T00:00:00+00:00")

    # 4501 (antenna) comes before 4773 (gnss_receiver) by id order.
    idx_4501 = out.index("id_entity=4501")
    idx_4773 = out.index("id_entity=4773")
    assert idx_4501 < idx_4773

    # Within 4501, model + serial_number both appear under one device header.
    block_4501 = out[idx_4501:idx_4773]
    assert "model" in block_4501
    assert "serial_number" in block_4501
    # Two ACTION lines and one device header — confirms grouping.
    assert block_4501.count("id_entity=4501") == 1
    assert block_4501.count("#ACTION 4501 patch-attribute-date") == 2


def test_triage_includes_audit_command_in_header():
    report = _report_with([])
    out = format_triage_file(
        report,
        audit_command="tos audit attribute-dates ARHO",
        generated_at="2026-05-19T00:00:00+00:00",
    )
    assert "Audit cmd:  tos audit attribute-dates ARHO" in out


def test_triage_includes_suppress_hint_per_violation():
    """The triage block must also offer the SUPPRESS alternative — closes
    the loop with Layer 3 for known-good entries."""
    v = _violation(4773, "serial_number", "2014-10-17", "2002-01-01")
    out = format_triage_file(
        _report_with([v]), generated_at="2026-05-19T00:00:00+00:00"
    )
    assert "SUPPRESS 4773 serial_number 2014-10-17" in out


# ---------------------------------------------------------------------------
# validate_codes_against_catalog (Layer 5)
# ---------------------------------------------------------------------------


def test_validate_codes_passes_on_known(catalog_path: Path):
    catalog = load_catalog(catalog_path)
    # Empty input and known codes both pass without raising.
    validate_codes_against_catalog([], catalog)
    validate_codes_against_catalog(["serial_number", "model"], catalog)


def test_validate_codes_raises_with_did_you_mean(catalog_path: Path):
    catalog = load_catalog(catalog_path)
    # Close match present in the catalog should appear in the message.
    with pytest.raises(ValueError, match="firmware_version"):
        validate_codes_against_catalog(
            ["fimrware_version"], catalog, flag_label="--include code"
        )


def test_validate_codes_raises_when_no_close_match(catalog_path: Path):
    catalog = load_catalog(catalog_path)
    # A wildly off code falls through to the catalog-pointer hint.
    with pytest.raises(ValueError, match="attribute_codes.yaml"):
        validate_codes_against_catalog(["wxyz123"], catalog)


def test_validate_codes_uses_flag_label_in_message(catalog_path: Path):
    catalog = load_catalog(catalog_path)
    with pytest.raises(ValueError, match="Unknown --exclude code"):
        validate_codes_against_catalog(
            ["nope_nope"], catalog, flag_label="--exclude code"
        )


# ---------------------------------------------------------------------------
# audit_station_attribute_dates — --include / --exclude semantics
# ---------------------------------------------------------------------------


def test_include_audits_mutable_code(catalog_path: Path):
    """`firmware_version` is mutable — default audit skips it. With
    --include firmware_version, the data-entry pattern is surfaced."""
    device = _device(
        9100,
        "gnss_receiver",
        [
            _attr("serial_number", "SN", "2010-01-01"),
            _attr("firmware_version", "1.0", "2010-01-01", "2012-01-01"),
            _attr("firmware_version", "2.0", "2012-01-01"),
        ],
    )
    station = _station(100, "TEST", [_conn(9100, "2010-01-01")])
    client = _client_for({100: station, 9100: device})

    report = audit_station_attribute_dates(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        include_codes=["firmware_version"],
    )
    codes = sorted(v.code for v in report.violations)
    assert "firmware_version" in codes


def test_include_audits_todo_classified_code(catalog_path: Path):
    """A code with classification=TODO is normally skipped (operator
    hasn't reviewed). --include overrides — useful for spot-checking."""
    device = _device(
        9101,
        "gnss_receiver",
        [
            _attr("serial_number", "SN", "2010-01-01"),
            _attr("todo_code", "v", "2014-10-17"),
        ],
    )
    station = _station(100, "TEST", [_conn(9101, "2010-01-01")])
    client = _client_for({100: station, 9101: device})

    report = audit_station_attribute_dates(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        include_codes=["todo_code"],
    )
    codes = sorted(v.code for v in report.violations)
    assert "todo_code" in codes


def test_include_audits_gps_relevance_no_code(catalog_path: Path):
    """`not_relevant_code` is gps_relevance=no — normally filtered out
    even though it's classified inherent. --include bypasses that."""
    device = _device(
        9102,
        "gnss_receiver",
        [
            _attr("serial_number", "SN", "2010-01-01"),
            _attr("not_relevant_code", "v", "2014-10-17"),
        ],
    )
    station = _station(100, "TEST", [_conn(9102, "2010-01-01")])
    client = _client_for({100: station, 9102: device})

    report = audit_station_attribute_dates(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        include_codes=["not_relevant_code"],
    )
    assert "not_relevant_code" in [v.code for v in report.violations]


def test_include_overrides_applies_to_exclusion(catalog_path: Path):
    """`not_relevant_code` has applies_to=[seismometer], so on a
    gnss_receiver it would normally be skipped. --include forces it."""
    device = _device(
        9103,
        "gnss_receiver",
        [
            _attr("serial_number", "SN", "2010-01-01"),
            _attr("not_relevant_code", "v", "2014-10-17"),
        ],
    )
    station = _station(100, "TEST", [_conn(9103, "2010-01-01")])
    client = _client_for({100: station, 9103: device})

    report = audit_station_attribute_dates(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        include_codes=["not_relevant_code"],
    )
    # Even though applies_to=[seismometer] excludes gnss_receiver, the
    # include flag bypasses that gate.
    assert any(v.code == "not_relevant_code" for v in report.violations)


def test_exclude_drops_inherent_code(catalog_path: Path):
    """An exclude on `serial_number` (normally flagged inherent) must
    drop it entirely — not present in violations, not present in
    suppressed (different code path)."""
    device = _device(
        9104,
        "gnss_receiver",
        [
            _attr("serial_number", "SN", "2014-10-17"),
            _attr("model", "M", "2002-01-01"),
        ],
    )
    station = _station(100, "TEST", [_conn(9104, "2002-01-01")])
    client = _client_for({100: station, 9104: device})

    report = audit_station_attribute_dates(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        exclude_codes=["serial_number"],
    )
    codes = [v.code for v in report.violations]
    suppressed_codes = [s.violation.code for s in report.suppressed]
    assert "serial_number" not in codes
    assert "serial_number" not in suppressed_codes
    assert "serial_number" in report.excluded_codes


def test_exclude_wins_over_include_on_same_code(catalog_path: Path):
    """The destination doc / advisor contract: exclude is the more
    conservative side. A code in both sets is silenced (exclude wins)."""
    device = _device(
        9105,
        "gnss_receiver",
        [
            _attr("serial_number", "SN", "2010-01-01"),
            _attr("firmware_version", "1.0", "2010-01-01", "2012-01-01"),
            _attr("firmware_version", "2.0", "2012-01-01"),
        ],
    )
    station = _station(100, "TEST", [_conn(9105, "2010-01-01")])
    client = _client_for({100: station, 9105: device})

    report = audit_station_attribute_dates(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        include_codes=["firmware_version"],
        exclude_codes=["firmware_version"],
    )
    assert not any(v.code == "firmware_version" for v in report.violations)
    # Audit honoured the exclude — the include is dropped from the
    # report (so verbose output doesn't lie about what was audited).
    assert "firmware_version" not in report.included_codes
    assert "firmware_version" in report.excluded_codes


def test_include_unmatched_recorded_for_silent_no_op_detection(
    catalog_path: Path,
):
    """If --include names a valid catalog code but no device on the
    station has that attribute, the code shows up in
    `included_codes_unmatched` so the CLI can warn the operator."""
    device = _device(
        9106,
        "gnss_receiver",
        [
            _attr("serial_number", "SN", "2014-10-17"),
            _attr("model", "M", "2002-01-01"),
            # No firmware_version on this device.
        ],
    )
    station = _station(100, "TEST", [_conn(9106, "2002-01-01")])
    client = _client_for({100: station, 9106: device})

    report = audit_station_attribute_dates(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        include_codes=["firmware_version"],
    )
    assert report.included_codes_unmatched == ["firmware_version"]


def test_include_unmatched_empty_when_code_was_seen(catalog_path: Path):
    """Matched codes shouldn't appear in the unmatched list — the
    distinction is meaningful for the silent-no-op warning."""
    device = _device(
        9107,
        "gnss_receiver",
        [
            _attr("serial_number", "SN", "2010-01-01"),
            _attr("firmware_version", "1.0", "2010-01-01", "2012-01-01"),
            _attr("firmware_version", "2.0", "2012-01-01"),
        ],
    )
    station = _station(100, "TEST", [_conn(9107, "2010-01-01")])
    client = _client_for({100: station, 9107: device})

    report = audit_station_attribute_dates(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        include_codes=["firmware_version"],
    )
    assert report.included_codes_unmatched == []


def test_include_unknown_code_raises_with_did_you_mean(
    catalog_path: Path,
):
    """Validation happens before any TOS reads — typos surface
    immediately with a suggestion."""
    client = _client_for({100: _station(100, "TEST", [])})
    with pytest.raises(ValueError, match="serial_number"):
        audit_station_attribute_dates(
            client,
            id_entity=100,
            catalog_path=catalog_path,
            include_codes=["serial_numberr"],  # extra 'r'
        )


def test_exclude_unknown_code_raises(catalog_path: Path):
    """Same validation contract for --exclude."""
    client = _client_for({100: _station(100, "TEST", [])})
    with pytest.raises(ValueError, match="Unknown --exclude code"):
        audit_station_attribute_dates(
            client,
            id_entity=100,
            catalog_path=catalog_path,
            exclude_codes=["totally_made_up_code"],
        )


def test_include_locations_scope_code_handled_gracefully(catalog_path: Path):
    """The catalog's locations/stations scopes are flattened into the
    same lookup. Including `address` (a locations-scope code) shouldn't
    crash — it just lands in included_codes_unmatched because devices
    don't carry location attributes."""
    device = _device(
        9108,
        "gnss_receiver",
        [_attr("serial_number", "SN", "2010-01-01")],
    )
    station = _station(100, "TEST", [_conn(9108, "2010-01-01")])
    client = _client_for({100: station, 9108: device})

    report = audit_station_attribute_dates(
        client,
        id_entity=100,
        catalog_path=catalog_path,
        include_codes=["address"],
    )
    assert "address" in report.included_codes
    assert "address" in report.included_codes_unmatched
