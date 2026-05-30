"""Tests for `tos station show <STN>` — read-only station inspection.

Pins the dispatch surface (default view / --all / --device / --json),
the resolve→fetch→render plumbing, and the failure modes (missing
station, missing payload).
"""

from __future__ import annotations

import json
from argparse import Namespace
from unittest.mock import patch

from tostools.api.tos_client import TOSClient
from tostools.tos import _station_show_main


def _show_args(**overrides):
    """Namespace matching the `tos station show` argparser defaults.

    Note: ``no_visits`` defaults to True in this helper so the existing
    test suite (written before Phase A.2 of the vitjanir expansion)
    doesn't need a per-test ``TOSClient.list_maintenance_visits`` patch.
    New visit-aware tests pass ``no_visits=False`` explicitly and patch
    the method themselves.
    """
    defaults = {
        "station": "HEDI",
        "show_all": False,
        "device_mode": False,
        "attributes_only": False,
        "no_visits": True,
        "json": False,
        "server": "vi-api.vedur.is",
        "port": 443,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


def _station_payload(station_id=4257, with_closed=True):
    """One representative station entity payload.

    Two children: an open gnss_receiver join (NETR9) and a closed older
    one (POLARX2). One open + one closed station-level attribute period.
    """
    return {
        "id_entity": station_id,
        "code_entity_subtype": "geophysical",
        "attributes": [
            {
                "code": "marker",
                "value": "HEDI",
                "date_from": "2006-06-29",
                "date_to": None,
                "id_attribute_value": 12001,
            },
            {
                "code": "name",
                "value": "Heðinshöfði",
                "date_from": "2006-06-29",
                "date_to": None,
                "id_attribute_value": 12002,
            },
            {
                "code": "status",
                "value": "virkt",
                "date_from": "2012-06-27",
                "date_to": None,
                "id_attribute_value": 12003,
            },
            # Closed attribute period — only shown with --all.
            {
                "code": "name",
                "value": "Heðinshöfði (legacy)",
                "date_from": "2003-01-01",
                "date_to": "2006-06-29",
                "id_attribute_value": 11900,
            },
        ],
        "children_connections": [
            {
                "id_entity_child": 21197,
                "id_entity_connection": 28726,
                "time_from": "2014-10-17T00:00:00",
                "time_to": None,
            }
        ]
        + (
            [
                {
                    "id_entity_child": 4830,
                    "id_entity_connection": 5942,
                    "time_from": "2006-06-29T00:00:00",
                    "time_to": "2014-10-17T00:00:00",
                }
            ]
            if with_closed
            else []
        ),
    }


def _child_payloads():
    """Map of child id → entity history. Open NETR9, closed POLARX2."""
    return {
        21197: {
            "code_entity_subtype": "gnss_receiver",
            "attributes": [
                {
                    "code": "serial_number",
                    "value": "5545R50370",
                    "date_to": None,
                    "date_from": "2014-10-17",
                },
                {
                    "code": "model",
                    "value": "TRIMBLE NETR9",
                    "date_to": None,
                    "date_from": "2014-10-17",
                },
                {
                    "code": "status",
                    "value": "virkt",
                    "date_to": None,
                    "date_from": "2014-10-17",
                },
            ],
        },
        4830: {
            "code_entity_subtype": "gnss_receiver",
            "attributes": [
                {
                    "code": "serial_number",
                    "value": "1001",
                    "date_to": None,
                    "date_from": "2006-06-29",
                },
                {
                    "code": "model",
                    "value": "SEPT POLARX2",
                    "date_to": None,
                    "date_from": "2006-06-29",
                },
            ],
        },
    }


def _fake_get_entity_history_factory(station_id):
    """Build a side_effect callable that returns the station + its kids."""
    station = _station_payload(station_id)
    kids = _child_payloads()

    def fake(eid):
        if eid == station_id:
            return station
        return kids.get(eid, {})

    return fake


# ---------------------------------------------------------------------------
# Default view (no flags)
# ---------------------------------------------------------------------------


def test_show_default_renders_open_only(capsys):
    """Default view: identity + open attribute periods + open joined
    devices. No closed-period sections, no past-devices table, no
    device-attribute-history.

    We assert only on section titles + scope labels — table cell content
    is subject to Rich's column-width truncation in non-TTY runs and is
    covered by the JSON-shape tests below.
    """
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
    ):
        rc = _station_show_main(_show_args())

    assert rc == 0
    out = capsys.readouterr().out
    # Station header (one-liner, never wrapped).
    assert "Station id=" in out
    assert "HEDI" in out
    # Open-attributes section renders.
    assert "Current attributes (open periods only)" in out
    # Closed-period sections must NOT appear in default view.
    assert "Attribute history" not in out
    assert "Past devices" not in out
    assert "Device attribute history" not in out
    # One open join present (HEDI fixture has POLARX2 closed + NETR9 open).
    assert "1 open join(s)" in out


def test_show_all_adds_closed_sections(capsys):
    """--all surfaces three new sections:
    * "Attribute history (closed periods only)" — only closed, since
      the open periods are already shown above
    * "Past devices — N closed join(s)" — split from the open table
    * "Device attribute history" — closed attribute periods on
      currently-joined children (firmware bumps etc.)
    """
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
    ):
        rc = _station_show_main(_show_args(show_all=True))

    assert rc == 0
    out = capsys.readouterr().out
    # Closed attribute periods table.
    assert "Attribute history (closed periods only)" in out
    # Joined-devices is split into two tables.
    assert "1 open join(s)" in out
    assert "1 closed join(s)" in out
    # Device-attribute-history section is rendered (even when (none)).
    assert "Device attribute history" in out


def test_show_all_split_open_closed_omits_closed_when_none(capsys):
    """When the station has no closed joins, the Past-devices table
    still renders its title + "(no closed joins)" placeholder so the
    operator sees the section exists. (Helps distinguish "no closed
    joins" from "I forgot --all".)"""
    # Fixture with only an open child (no closed joins).
    station = _station_payload(station_id=4257, with_closed=False)

    def fake(eid):
        if eid == 4257:
            return station
        return _child_payloads().get(eid, {})

    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(TOSClient, "get_entity_history", side_effect=fake),
    ):
        rc = _station_show_main(_show_args(show_all=True))

    assert rc == 0
    out = capsys.readouterr().out
    assert "Past devices — 0 closed join(s)" in out
    assert "(no closed joins)" in out


def test_show_all_device_attribute_history_renders_when_present(capsys):
    """If a currently-joined child has closed attribute periods (a
    firmware bump, status flip), the Device-attribute-history table
    shows them. Asserted via the table title — cell content is subject
    to Rich truncation."""
    station = _station_payload(station_id=4257, with_closed=False)
    # Inject a firmware-bump pair onto the open NETR9 child.
    kids = _child_payloads()
    kids[21197]["attributes"].extend(
        [
            {
                "code": "firmware_version",
                "value": "4.85",
                "date_from": "2014-10-17",
                "date_to": "2018-05-23",
                "id_attribute_value": 40001,
            },
            {
                "code": "firmware_version",
                "value": "5.10",
                "date_from": "2018-05-23",
                "date_to": None,
                "id_attribute_value": 40002,
            },
        ]
    )

    def fake(eid):
        if eid == 4257:
            return station
        return kids.get(eid, {})

    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(TOSClient, "get_entity_history", side_effect=fake),
    ):
        rc = _station_show_main(_show_args(show_all=True))

    assert rc == 0
    out = capsys.readouterr().out
    # The (none) suffix should NOT appear since we injected a closed period.
    assert (
        "Device attribute history (closed periods on currently-joined "
        "devices): (none)"
    ) not in out
    assert "Device attribute history" in out


# ---------------------------------------------------------------------------
# --device delegation
# ---------------------------------------------------------------------------


def test_show_device_mode_delegates_to_device_list():
    """--device routes to `tos device list --station <STN>`. We patch
    `_device_list_main` and confirm it's called with the resolved
    station + the matching `--all` flag."""
    with patch("tostools.tos._device_list_main", return_value=0) as dl:
        rc = _station_show_main(_show_args(device_mode=True))

    assert rc == 0
    dl.assert_called_once()
    delegated = dl.call_args.args[0]
    assert delegated.station == "HEDI"
    assert delegated.all is False
    # Device-list contract expects these standard filters defaulted.
    assert delegated.subtype is None
    assert delegated.model is None


def test_show_device_mode_passes_all_through(capsys):
    """--device --all → device-list with all=True (closed joins shown)."""
    del capsys  # silence Pyright; we don't read output, the delegate would
    with patch("tostools.tos._device_list_main", return_value=0) as dl:
        _station_show_main(_show_args(device_mode=True, show_all=True))

    delegated = dl.call_args.args[0]
    assert delegated.all is True


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def test_show_json_default_shape(capsys):
    """JSON mode emits a stable shape: id_entity, marker, name, subtype,
    include_closed flag, history (None unless --all), children list."""
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
    ):
        rc = _station_show_main(_show_args(json=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["id_entity"] == 4257
    assert payload["marker"] == "HEDI"
    assert payload["name"] == "Heðinshöfði"
    assert payload["subtype"] == "geophysical"
    assert payload["include_closed"] is False
    # `history` only carries when --all; default keeps it null to avoid
    # leaking the closed-period payload into automation that didn't
    # opt-in.
    assert payload["history"] is None
    # One open child in default view.
    assert len(payload["children"]) == 1
    assert payload["children"][0]["serial"] == "5545R50370"


def test_show_json_with_all_includes_history(capsys):
    """--json --all emits the full history payload + closed children."""
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
    ):
        rc = _station_show_main(_show_args(json=True, show_all=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["include_closed"] is True
    assert payload["history"] is not None
    # Both children present (open + closed).
    assert len(payload["children"]) == 2


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_show_orders_station_attributes_by_canonical_priority(capsys):
    """Station-attribute table puts the canonical identity + location
    fields at the top in a fixed order, with everything else
    alphabetical after. Asserts on the rendered text (stable across
    Rich versions; the codes are short enough not to be truncated).

    Priority:
      subtype, marker, name, iers_domes_number, lon, lat, altitude,
      operational_class, in_network_epos, <rest alphabetical>
    """
    station = {
        "id_entity": 4328,
        "code_entity_subtype": "geophysical",
        "attributes": [
            # Intentionally NOT in priority order to verify the
            # renderer reorders rather than echoing input order.
            {
                "code": "iers_domes_number",
                "value": "10204M002",
                "date_to": None,
                "date_from": "2024-11-07",
            },
            {
                "code": "altitude",
                "value": 82.84,
                "date_to": None,
                "date_from": "1997-05-27",
            },
            {
                "code": "lon",
                "value": -15.197917,
                "date_to": None,
                "date_from": "1997-05-27",
            },
            {
                "code": "subtype",
                "value": "GPS stöð",
                "date_to": None,
                "date_from": "1997-05-27",
            },
            {
                "code": "name",
                "value": "Höfn",
                "date_to": None,
                "date_from": "1997-05-27",
            },
            {
                "code": "lat",
                "value": 64.267293,
                "date_to": None,
                "date_from": "1997-05-27",
            },
            {
                "code": "marker",
                "value": "hofn",
                "date_to": None,
                "date_from": "1997-05-27",
            },
            {
                "code": "operational_class",
                "value": "B",
                "date_to": None,
                "date_from": "1997-05-27",
            },
            {
                "code": "in_network_epos",
                "value": "nei",
                "date_to": None,
                "date_from": "1997-05-27",
            },
            {
                "code": "aaa_extra",
                "value": "x",
                "date_to": None,
                "date_from": "2000-01-01",
            },
            {
                "code": "zzz_extra",
                "value": "z",
                "date_to": None,
                "date_from": "2000-01-01",
            },
        ],
        "children_connections": [],
    }
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4328),
        patch.object(TOSClient, "get_entity_history", return_value=station),
    ):
        rc = _station_show_main(_show_args(station="HOFN"))

    assert rc == 0
    out = capsys.readouterr().out

    # Each code's first occurrence in the output is what the table puts
    # it in row order. Slice out station-attribute codes only.
    expected_priority_order = [
        "subtype",
        "marker",
        "name",
        "iers_domes_number",
        "lon",
        "lat",
        "altitude",
        "operational_class",
        "in_network_epos",
        "aaa_extra",
        "zzz_extra",
    ]
    indices = [out.find(c) for c in expected_priority_order]
    # All present.
    assert all(i >= 0 for i in indices), f"missing codes: {indices}"
    # Strictly increasing (priority block first, alphabetical-extras after).
    assert indices == sorted(
        indices
    ), f"codes out of order: {list(zip(expected_priority_order, indices))}"


def test_show_orders_devices_by_subtype_then_date(capsys):
    """Both joined-device tables (open and closed) are ordered by
    subtype priority (gnss_receiver, antenna, radome, monument) and
    chronologically within each subtype group.

    Asserted via the JSON shape so we're insensitive to Rich's
    column-width truncation.
    """
    # Hand-build a station with a deliberately-shuffled order of
    # children, including one of every quartet subtype, plus an
    # extra closed receiver in chronological reverse-order to verify
    # the within-subtype date sort.
    station = {
        "id_entity": 5000,
        "code_entity_subtype": "geophysical",
        "attributes": [
            {
                "code": "marker",
                "value": "TEST",
                "date_to": None,
                "date_from": "2000-01-01",
            },
            {
                "code": "name",
                "value": "Test",
                "date_to": None,
                "date_from": "2000-01-01",
            },
        ],
        "children_connections": [
            # Intentionally not in priority order:
            {
                "id_entity_child": 101,
                "id_entity_connection": 9001,
                "time_from": "2010-01-01",
                "time_to": None,
            },  # monument
            {
                "id_entity_child": 102,
                "id_entity_connection": 9002,
                "time_from": "2010-01-01",
                "time_to": None,
            },  # antenna
            {
                "id_entity_child": 103,
                "id_entity_connection": 9003,
                "time_from": "2010-01-01",
                "time_to": None,
            },  # gnss_receiver
            {
                "id_entity_child": 104,
                "id_entity_connection": 9004,
                "time_from": "2010-01-01",
                "time_to": None,
            },  # radome
            # Two closed receivers, intentionally reverse-chronological:
            {
                "id_entity_child": 201,
                "id_entity_connection": 9101,
                "time_from": "2005-01-01",
                "time_to": "2007-01-01",
            },  # receiver, earlier
            {
                "id_entity_child": 202,
                "id_entity_connection": 9102,
                "time_from": "2007-01-01",
                "time_to": "2010-01-01",
            },  # receiver, later
        ],
    }
    kids = {
        101: {"code_entity_subtype": "monument", "attributes": []},
        102: {"code_entity_subtype": "antenna", "attributes": []},
        103: {"code_entity_subtype": "gnss_receiver", "attributes": []},
        104: {"code_entity_subtype": "radome", "attributes": []},
        201: {"code_entity_subtype": "gnss_receiver", "attributes": []},
        202: {"code_entity_subtype": "gnss_receiver", "attributes": []},
    }

    def fake(eid):
        if eid == 5000:
            return station
        return kids.get(eid, {})

    with (
        patch("tostools.tos._resolve_parent_id", return_value=5000),
        patch.object(TOSClient, "get_entity_history", side_effect=fake),
    ):
        rc = _station_show_main(_show_args(station="TEST", show_all=True, json=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # Open table: receiver → antenna → radome → monument.
    assert [r["subtype"] for r in payload["children_open"]] == [
        "gnss_receiver",
        "antenna",
        "radome",
        "monument",
    ]
    # Closed table: receivers sorted chronologically within their group.
    closed_subtypes = [r["subtype"] for r in payload["children_closed"]]
    closed_dates = [r["time_from"] for r in payload["children_closed"]]
    assert closed_subtypes == ["gnss_receiver", "gnss_receiver"]
    assert closed_dates == ["2005-01-01", "2007-01-01"]


def test_show_renders_drill_hint_with_example_device_id(capsys):
    """The footer prints "Drill deeper:" with copy-pasteable commands
    referencing one open device id per subtype (gnss_receiver
    prioritized first since firmware is the most common drill-down)."""
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
    ):
        rc = _station_show_main(_show_args())

    assert rc == 0
    out = capsys.readouterr().out
    assert "Drill deeper:" in out
    # The HEDI fixture's open gnss_receiver is id 21197 — prioritized
    # in the hint as the first example.
    assert "tos device show --id 21197" in out
    assert "--attributes-history" in out
    assert "tos audit timeline 21197" in out
    # Station-level shortcuts referencing the typed station marker.
    assert "tos station verify HEDI" in out
    assert "tos station triage HEDI" in out


def test_show_drill_hint_absent_in_json_mode(capsys):
    """JSON output is for automation — no hint, no markup. The hint
    block belongs only in pretty-text mode."""
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
    ):
        rc = _station_show_main(_show_args(json=True))

    assert rc == 0
    out = capsys.readouterr().out
    assert "Drill deeper:" not in out


def test_show_renders_contacts_section(capsys):
    """Default mode includes a Contacts section sourced from
    `TOSClient.get_contacts`. The id / role / name / phone columns
    are pinned by the rendered title + cell content."""
    contacts = [
        {
            "id_contact": 2471,
            "role": "owner",
            "role_is": "Eigandi stöðvar",
            "name": "Deutsche GeoForschungsZentrum GFZ",
            "organization": "Deutsche GeoForschungsZentrum GFZ",
            "phone_primary": None,
            "address": None,
            "per_time_from": "1997-05-27T00:00:00",
            "per_time_to": None,
        }
    ]
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
        patch.object(TOSClient, "get_contacts", return_value=contacts),
    ):
        rc = _station_show_main(_show_args())

    assert rc == 0
    out = capsys.readouterr().out
    assert "Contacts — 1 record(s)" in out
    # Role label appears (might be wrapped — match the unique prefix).
    assert "Eigandi" in out
    assert "Deutsche" in out  # name (might be truncated in narrow Rich)
    # id_contact column added so operator can drill via /entity/<id>.
    assert "2471" in out


def test_show_renders_no_contacts_placeholder(capsys):
    """Station with no mapped contacts shows the (no contacts mapped)
    placeholder so the section's presence is unambiguous."""
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
        patch.object(TOSClient, "get_contacts", return_value=[]),
    ):
        rc = _station_show_main(_show_args())

    assert rc == 0
    out = capsys.readouterr().out
    assert "Contacts — 0 record(s)" in out
    assert "(no contacts mapped)" in out


def test_show_attributes_only_short_circuits(capsys):
    """--attributes mode: header + open-attribute table ONLY. No
    joined devices, no contacts, no drill hint. Saves network IO too
    (per-child get_entity_history + get_contacts skipped)."""
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
        # Should NOT be called in attributes-only mode.
        patch.object(TOSClient, "get_contacts") as get_contacts,
    ):
        rc = _station_show_main(_show_args(attributes_only=True))

    assert rc == 0
    out = capsys.readouterr().out
    # Attribute section present.
    assert "Current attributes (open periods only)" in out
    # Suppressed sections.
    assert "Joined devices" not in out
    assert "Past devices" not in out
    assert "Contacts" not in out
    assert "Drill deeper" not in out
    # Contacts endpoint never queried — saves an HTTP roundtrip.
    get_contacts.assert_not_called()


def test_show_attributes_only_with_all_includes_closed_attrs(capsys):
    """--attributes --all: open + closed attribute tables, still no
    devices/contacts/drill-hint."""
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
        patch.object(TOSClient, "get_contacts", return_value=[]),
    ):
        rc = _station_show_main(_show_args(attributes_only=True, show_all=True))

    assert rc == 0
    out = capsys.readouterr().out
    assert "Current attributes (open periods only)" in out
    assert "Attribute history (closed periods only)" in out
    assert "Joined devices" not in out
    assert "Contacts" not in out


def test_show_json_includes_contacts(capsys):
    """JSON payload exposes the raw contacts list. Pinned so downstream
    automation can branch on owner / operator / point-of-contact."""
    contacts = [
        {"role": "owner", "name": "VI"},
        {"role": "operator", "name": "Op"},
    ]
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
        patch.object(TOSClient, "get_contacts", return_value=contacts),
    ):
        rc = _station_show_main(_show_args(json=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["contacts"] == contacts
    assert payload["attributes_only"] is False


def test_show_colors_station_attribute_values():
    """The Value column in the station-attribute table is wrapped in
    cyan — matches the legacy `Property | Value` formatter so both
    views read consistently. Pinned at the rich-markup level so we
    don't depend on what the terminal can actually render."""
    from tostools.tos import _color_value

    # Non-status values get the station-value color (cyan).
    assert "cyan" in _color_value("hofn", "marker")
    assert "cyan" in _color_value("10204M002", "iers_domes_number")
    # Status code skips the cyan path and uses the red override for
    # suspicious values; non-suspicious status values fall through to
    # plain rendering (matches `_color_status`'s contract).
    assert "red" in _color_value("bilað", "status")
    assert _color_value("virkt", "status") == "virkt"
    # None values render as the em-dash placeholder.
    assert _color_value(None, "marker") == "—"


def test_show_colors_device_subtypes():
    """Joined-devices subtype column applies the per-subtype palette
    aligned with the legacy `tosGPS PrintTOS` group colors:
    gnss_receiver green, antenna/radome red, monument yellow.
    Asserted on the markup helper directly — the rendered table is
    subject to Rich width truncation."""
    from tostools.tos import _color_subtype

    assert "green" in _color_subtype("gnss_receiver")
    assert "red" in _color_subtype("antenna")
    assert "red" in _color_subtype("radome")
    assert "yellow" in _color_subtype("monument")
    # Unknown subtype falls through uncoloured (no rich brackets).
    assert _color_subtype("digitizer") == "digitizer"


def test_show_dates_use_blue_default_yellow_artifact():
    """Date cells render blue by default (matches the legacy From/To
    column style); the 2014-10-17 cleanup-artifact override keeps its
    yellow treatment so suspicious backdates still pop."""
    from tostools.tos import _color_date

    assert "blue" in _color_date("2013-05-05T00:00:00")
    assert "yellow" in _color_date("2014-10-17T00:00:00")
    assert _color_date(None) == "—"


def test_show_unresolvable_station_returns_1(capsys):
    """Unknown marker → exit 1 + stderr message. Distinct from exit 2
    (verify's audit-failure code) so callers can branch."""
    with patch("tostools.tos._resolve_parent_id", return_value=None):
        rc = _station_show_main(_show_args(station="XXXX"))

    assert rc == 1
    err = capsys.readouterr().err
    assert "No station found for marker 'XXXX'" in err


def test_show_empty_payload_returns_1(capsys):
    """Resolver returned an id but `get_entity_history` came back empty
    → exit 1, distinct from a malformed marker."""
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(TOSClient, "get_entity_history", return_value={}),
    ):
        rc = _station_show_main(_show_args())

    assert rc == 1
    err = capsys.readouterr().err
    assert "id_entity=4257 returned no history payload" in err


# ---------------------------------------------------------------------------
# Phase A.2 — aggregated vitjanir section
# ---------------------------------------------------------------------------


def _visit(id, *, start, completed=True, reason="Viðgerð", work="—"):
    """Compact vitjun row matching the list-endpoint shape."""
    return {
        "id": id,
        "maintenance_type": "on_site",
        "start_time": f"{start}T10:00:00",
        "end_time": f"{start}T11:00:00",
        "reason": reason,
        "participants": "bgo@vedur.is",
        "participants_names": "Benedikt G. Ófeigsson",
        "work": work,
        "remaining": None,
        "completed": completed,
    }


def test_show_visits_aggregates_station_plus_joined_devices(capsys):
    """Default view (no_visits=False) renders the Recent vitjanir
    section with rows from BOTH the station and each currently-joined
    device, with attribution labels distinguishing them. This is the
    forward-compat hook for Phase C lifecycle-tracker vitjanir."""
    station_visits = [_visit(5490, start="2026-05-22", work="skipti um kapal")]
    device_visits = {
        21197: [_visit(9001, start="2025-12-10", work="firmware 3.00 → 3.01")],
    }

    def fake_list_visits(self_, eid):
        if eid == 4257:
            return station_visits
        return device_visits.get(eid, [])

    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
        patch.object(TOSClient, "get_contacts", return_value=[]),
        patch.object(
            TOSClient,
            "list_maintenance_visits",
            autospec=True,
            side_effect=fake_list_visits,
        ),
    ):
        rc = _station_show_main(_show_args(no_visits=False))

    assert rc == 0
    out = capsys.readouterr().out
    # Section title shows the aggregate count + joined-device count.
    assert "Recent vitjanir" in out
    # The "(aggregated from station + 1 joined device(s))" line is a
    # plain console.print, not the Rich table title — survives non-TTY
    # console-width truncation.
    assert "aggregated from station + 1 joined device(s)" in out
    # Both ids surface (Rich may wrap long labels, so check ids only).
    assert "5490" in out
    assert "9001" in out
    # Attribution column present (Rich wraps the cell content, so
    # check the column header + the per-source kind separately).
    assert "source" in out
    assert "station" in out
    assert "21197" in out


def test_show_no_visits_suppresses_section_and_skips_http(capsys):
    """--no-visits suppresses the section AND avoids the
    list_maintenance_visits roundtrips entirely (relevant on slow
    links / when the operator only wants identity + attributes)."""
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
        patch.object(TOSClient, "get_contacts", return_value=[]),
        patch.object(
            TOSClient, "list_maintenance_visits", autospec=True
        ) as list_visits,
    ):
        rc = _station_show_main(_show_args(no_visits=True))

    assert rc == 0
    out = capsys.readouterr().out
    assert "Recent vitjanir" not in out
    list_visits.assert_not_called()


def test_show_visits_trims_to_open_plus_last_3_closed_by_default(capsys):
    """Default trim: every open visit + 3 most-recent closed.
    --all (show_all=True) extends to full history."""
    # 1 open + 5 closed on the station, none on the device.
    station_visits = [
        _visit(1, start="2024-01-01", completed=False),  # open
        _visit(2, start="2026-05-22", completed=True),
        _visit(3, start="2025-12-15", completed=True),
        _visit(4, start="2023-05-17", completed=True),
        _visit(5, start="2018-02-06", completed=True),
        _visit(6, start="2015-08-08", completed=True),
    ]

    def fake_list_visits(self_, eid):
        if eid == 4257:
            return station_visits
        return []

    # Default — open + 3 closed = 4 rendered, 2 hidden
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
        patch.object(TOSClient, "get_contacts", return_value=[]),
        patch.object(
            TOSClient,
            "list_maintenance_visits",
            autospec=True,
            side_effect=fake_list_visits,
        ),
    ):
        rc = _station_show_main(_show_args(no_visits=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Recent vitjanir — 4 record(s)" in out
    assert "2 more closed" in out
    assert "--all" in out


def test_show_json_includes_visits(capsys):
    """JSON payload carries an `visits` array with __source_label per row."""
    station_visits = [_visit(5490, start="2026-05-22")]

    def fake_list_visits(self_, eid):
        return station_visits if eid == 4257 else []

    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
        patch.object(TOSClient, "get_contacts", return_value=[]),
        patch.object(
            TOSClient,
            "list_maintenance_visits",
            autospec=True,
            side_effect=fake_list_visits,
        ),
    ):
        rc = _station_show_main(_show_args(no_visits=False, json=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "visits" in payload
    assert len(payload["visits"]) == 1
    assert payload["visits"][0]["id"] == 5490
    assert payload["visits"][0]["__source_label"] == "station HEDI"
    assert payload["visits"][0]["__source_kind"] == "station"
