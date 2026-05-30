"""Tests for `TOSClient` read-only methods.

Companion to `test_tos_writer.py` (write-path) — this file covers the
no-auth GET / POST endpoints that the read-only client exposes.
"""

from __future__ import annotations

from unittest.mock import patch

from tostools.api.tos_client import TOSClient

# ---------------------------------------------------------------------------
# get_parent_history — wraps GET /entity/parent_history/{id}
# ---------------------------------------------------------------------------


def _join(id_connection, parent, time_from, time_to=None):
    """One row as returned by /entity/parent_history/{id}."""
    return {
        "id": id_connection,
        "id_entity_child": 17234,
        "id_entity_parent": parent,
        "time_from": time_from,
        "time_to": time_to,
    }


def test_get_parent_history_sorts_by_time_from_ascending():
    """Open / closed joins are intermixed in TOS payloads — the timeline
    should read chronologically top-to-bottom so a 'where has this device
    been?' inspection is intuitive."""
    client = TOSClient()
    with patch.object(client, "_make_request") as req:
        req.return_value = [
            _join(28104, 4258, "2014-10-17 00:00:00"),  # open
            _join(20100, 4, "2012-06-27 00:00:00", time_to="2014-10-17 00:00:00"),
            _join(19000, 4257, "2007-08-08 00:00:00", time_to="2007-09-07 00:00:00"),
        ]
        result = client.get_parent_history(17234)

    req.assert_called_once_with("/entity/parent_history/17234")
    assert [j["id"] for j in result] == [19000, 20100, 28104]


def test_get_parent_history_returns_empty_list_for_non_list_payload():
    """Endpoint failures, unexpected dict responses, etc. — the read-only
    inspection path should treat these as 'no joins' rather than raise."""
    client = TOSClient()
    with patch.object(client, "_make_request") as req:
        req.return_value = None
        assert client.get_parent_history(17234) == []
    with patch.object(client, "_make_request") as req:
        req.return_value = {"error": "not found"}
        assert client.get_parent_history(17234) == []


def test_get_parent_history_handles_empty_list():
    client = TOSClient()
    with patch.object(client, "_make_request") as req:
        req.return_value = []
        assert client.get_parent_history(17234) == []


def test_get_parent_history_tolerates_missing_time_from():
    """Partial TOS rows (missing time_from) should sort to the front via
    the empty-string default, not crash the sort key."""
    client = TOSClient()
    with patch.object(client, "_make_request") as req:
        req.return_value = [
            _join(1, 100, "2020-01-01"),
            {"id": 2, "id_entity_parent": 200},  # no time_from
        ]
        result = client.get_parent_history(17234)
    # The partial row sorts first (empty string < any date).
    assert [j["id"] for j in result] == [2, 1]


# ---------------------------------------------------------------------------
# tos device show CLI — _device_show_main
# ---------------------------------------------------------------------------


def _show_args(**overrides):
    """Build an argparse.Namespace shaped like `tos device show`'s parser."""
    from argparse import Namespace

    defaults = {
        "id_entity": None,
        "serial": None,
        "subtype": None,
        "server": "vi-api.vedur.is",
        "port": 443,
        "json": False,
        # Section flags (mutually exclusive at parse time; default all False
        # = "print all sections").
        "section_list": False,
        "section_attributes": False,
        "section_attributes_history": False,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


def test_device_show_requires_id_or_serial():
    from tostools.tos import _device_show_main

    rc = _device_show_main(_show_args())
    assert rc == 2


def test_device_show_serial_requires_subtype():
    from tostools.tos import _device_show_main

    rc = _device_show_main(_show_args(serial="3102"))
    assert rc == 2


def test_device_show_pretty_path_renders_all_sections(capsys):
    """Default (no section flag): renders header + open attributes +
    attribute history + parent history. Rich tables wrap each section."""
    from tostools.tos import _device_show_main

    fake_history = {
        "id_entity": 17234,
        "code_entity_subtype": "gnss_receiver",
        "attributes": [
            {
                "code": "serial_number",
                "value": "3102",
                "date_from": "2014-10-17T00:00:00",
                "date_to": None,
                "id_attribute_value": 60001,
            },
            {
                "code": "model",
                "value": "SEPT POLARX2",
                "date_from": "2007-09-07T00:00:00",
                "date_to": None,
                "id_attribute_value": 60002,
            },
        ],
    }
    parent_rows = [
        _join(20100, 4, "2012-06-27 00:00:00", time_to="2014-10-17 00:00:00"),
        _join(28104, 4258, "2014-10-17 00:00:00"),  # open
    ]

    def fake_get_entity_history(pid):
        names = {4: "B9 - Kjallari - Jörð", 4258: "Hedinshofdi"}
        return {
            "attributes": [
                {"code": "name", "value": names.get(pid, "?"), "date_to": None},
            ]
        }

    with (
        patch("tostools.devices.find_device", return_value=fake_history) as fd,
        patch.object(TOSClient, "get_parent_history", return_value=parent_rows) as gph,
        patch.object(
            TOSClient, "get_entity_history", side_effect=fake_get_entity_history
        ),
    ):
        rc = _device_show_main(_show_args(id_entity=17234))

    assert rc == 0
    fd.assert_called_once()
    gph.assert_called_once_with(17234)

    out = capsys.readouterr().out
    # All four sections present.
    assert "Device id=" in out
    assert "Current attributes" in out
    assert "Attribute history" in out
    assert "Parent history" in out
    # Parent names resolved into the table.
    assert "B9 - Kjallari" in out or "B9 -" in out  # rich may wrap long names
    assert "Hedinshofdi" in out
    # ID values from the fixture appear.
    assert "28104" in out


def test_device_show_section_list_renders_only_parent_history(capsys):
    """--list suppresses attributes and attribute-history sections."""
    from tostools.tos import _device_show_main

    with (
        patch("tostools.devices.find_device", return_value={"id_entity": 17234}),
        patch.object(
            TOSClient,
            "get_parent_history",
            return_value=[_join(28104, 4258, "2014-10-17 00:00:00")],
        ),
        patch.object(
            TOSClient,
            "get_entity_history",
            return_value={
                "attributes": [{"code": "name", "value": "X", "date_to": None}]
            },
        ),
    ):
        rc = _device_show_main(_show_args(id_entity=17234, section_list=True))

    assert rc == 0
    out = capsys.readouterr().out
    assert "Parent history" in out
    assert "Current attributes" not in out
    assert "Attribute history" not in out


def test_device_show_section_attributes_renders_only_open_attributes(capsys):
    """--attributes suppresses parent-history and full-history sections."""
    from tostools.tos import _device_show_main

    fake_history = {
        "id_entity": 17234,
        "attributes": [
            {
                "code": "serial_number",
                "value": "3102",
                "date_from": "2014-10-17T00:00:00",
                "date_to": None,
                "id_attribute_value": 60001,
            }
        ],
    }
    with (
        patch("tostools.devices.find_device", return_value=fake_history),
        patch.object(TOSClient, "get_parent_history", return_value=[]),
    ):
        rc = _device_show_main(_show_args(id_entity=17234, section_attributes=True))

    assert rc == 0
    out = capsys.readouterr().out
    assert "Current attributes" in out
    assert "Attribute history" not in out
    assert "Parent history" not in out


def test_device_show_section_attributes_history_renders_only_history(capsys):
    """--attributes-history suppresses header, open-attrs, parent-history."""
    from tostools.tos import _device_show_main

    fake_history = {
        "id_entity": 17234,
        "attributes": [
            {
                "code": "model",
                "value": "X",
                "date_from": "2007-09-07T00:00:00",
                "date_to": None,
                "id_attribute_value": 60002,
            }
        ],
    }
    with (
        patch("tostools.devices.find_device", return_value=fake_history),
        patch.object(TOSClient, "get_parent_history", return_value=[]),
    ):
        rc = _device_show_main(
            _show_args(id_entity=17234, section_attributes_history=True)
        )

    assert rc == 0
    out = capsys.readouterr().out
    assert "Attribute history" in out
    assert "Current attributes" not in out
    assert "Parent history" not in out


def test_device_show_no_parent_history_prints_orphan_notice(capsys):
    """A device with no parent_history rows is either truly orphan or
    unreachable — say so explicitly rather than printing an empty section."""
    from tostools.tos import _device_show_main

    with (
        patch("tostools.devices.find_device", return_value={"id_entity": 17234}),
        patch.object(TOSClient, "get_parent_history", return_value=[]),
    ):
        rc = _device_show_main(_show_args(id_entity=17234, section_list=True))

    assert rc == 0
    out = capsys.readouterr().out
    assert "orphan or never joined" in out


def test_device_show_json_emits_structured_payload(capsys):
    """--json should bypass the pretty-print path entirely and emit a
    single JSON object containing the raw history + parent_history."""
    import json

    from tostools.tos import _device_show_main

    fake_history = {
        "id_entity": 17234,
        "code_entity_subtype": "gnss_receiver",
    }
    parent_rows = [_join(28104, 4258, "2014-10-17 00:00:00")]

    with (
        patch("tostools.devices.find_device", return_value=fake_history),
        patch.object(TOSClient, "get_parent_history", return_value=parent_rows),
    ):
        rc = _device_show_main(_show_args(id_entity=17234, json=True))

    assert rc == 0

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["id_entity"] == 17234
    assert payload["history"] == fake_history
    assert payload["parent_history"] == parent_rows


def test_device_show_lookup_failure_returns_1(capsys):
    """LookupError / ValueError from find_device should turn into a
    clean stderr message + exit code 1, not a stack trace."""
    from tostools.tos import _device_show_main

    with patch(
        "tostools.devices.find_device", side_effect=LookupError("no such serial")
    ):
        rc = _device_show_main(_show_args(serial="9999", subtype="gnss_receiver"))

    assert rc == 1
    err = capsys.readouterr().err
    assert "Device lookup failed" in err
    assert "no such serial" in err


def test_device_show_routes_serial_lookup_to_find_device():
    """The --serial / --subtype path must forward the kwargs to find_device."""
    from tostools.tos import _device_show_main

    with (
        patch(
            "tostools.devices.find_device",
            return_value={"id_entity": 17234},
        ) as fd,
        patch("tostools.tos.display_device_record"),
        patch.object(TOSClient, "get_parent_history", return_value=[]),
    ):
        _device_show_main(_show_args(serial="3102", subtype="gnss_receiver"))

    fd.assert_called_once()
    kwargs = fd.call_args.kwargs
    assert kwargs["serial"] == "3102"
    assert kwargs["subtype"] == "gnss_receiver"
    assert kwargs["id_entity"] is None


# ---------------------------------------------------------------------------
# tos device list CLI — _device_list_main + _resolve_parent_id
# ---------------------------------------------------------------------------


def _list_args(**overrides):
    """argparse.Namespace shaped like `tos device list`'s parser."""
    from argparse import Namespace

    defaults = {
        "station": None,
        "location": None,
        "all": False,
        "server": "vi-api.vedur.is",
        "port": 443,
        "json": False,
        # Standard device-filter args (added by add_device_filter_arguments).
        "subtype": None,
        "model": None,
        "status": None,
        "serial": None,
        "date": None,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


def test_device_show_accepts_id_flag_form():
    """`tos device show --id N` is the supported alternative to the
    positional `tos device show N`. Mirrors `tos audit show --id N` so
    the same `--id` syntax works across every drill-into-one-entity
    verb (matters for copy-pasting drill-hint output).
    """
    from tostools.tos import main

    captured = {}

    def spy(args):
        captured["id_entity"] = args.id_entity
        captured["serial"] = args.serial
        return 0

    with patch("tostools.tos._device_show_main", side_effect=spy):
        rc = main(["device", "show", "--id", "16099"])

    assert rc == 0
    assert captured["id_entity"] == 16099
    assert captured["serial"] is None


def test_device_show_flag_wins_when_both_forms_given():
    """When operator provides both positional + --id, --id wins.
    Matches argparse's last-wins convention for repeated flags."""
    from tostools.tos import main

    captured = {}

    def spy(args):
        captured["id_entity"] = args.id_entity
        return 0

    with patch("tostools.tos._device_show_main", side_effect=spy):
        rc = main(["device", "show", "99999", "--id", "11111"])

    assert rc == 0
    # --id wins.
    assert captured["id_entity"] == 11111


def test_resolve_parent_id_by_station_marker_case_insensitive():
    """The TOS web UI / underlying find_station_by_marker normalize to
    lowercase. The resolver must match either case."""
    from tostools.tos import _resolve_parent_id

    client = TOSClient()
    hits = [
        {
            "code": "marker",
            "distance": 0,
            "value_varchar": "savi",
            "type_lvl_two": "stöð",
            "id_entity": 4440,
        }
    ]
    with patch.object(client, "basic_search", return_value=hits):
        assert _resolve_parent_id(client, station_marker="SAVI") == 4440
        assert _resolve_parent_id(client, station_marker="savi") == 4440


def test_resolve_parent_id_rejects_non_station_hits():
    """A marker match on a non-station entity (type_lvl_two != stöð)
    must be skipped — otherwise a warehouse marker could shadow a real
    station lookup."""
    from tostools.tos import _resolve_parent_id

    client = TOSClient()
    hits = [
        {
            "code": "marker",
            "distance": 0,
            "value_varchar": "savi",
            "type_lvl_two": "vöruhús",  # wrong type
            "id_entity": 999,
        }
    ]
    with patch.object(client, "basic_search", return_value=hits):
        assert _resolve_parent_id(client, station_marker="SAVI") is None


def test_resolve_parent_id_by_location_name_exact_match():
    """Location lookup is exact (case-sensitive), filters to code='name'
    + distance=0 to avoid partial / fuzzy hits."""
    from tostools.tos import _resolve_parent_id

    client = TOSClient()
    hits = [
        {
            "code": "name",
            "distance": 0,
            "value_varchar": "B9 - Kjallari - Jörð",
            "id_entity": 4,
        },
        {
            "code": "name",
            "distance": 1,  # fuzzy hit, must be skipped
            "value_varchar": "B9 - Kjallari - Jörð (other)",
            "id_entity": 999,
        },
    ]
    with patch.object(client, "basic_search", return_value=hits):
        assert _resolve_parent_id(client, location_name="B9 - Kjallari - Jörð") == 4


def test_resolve_parent_id_returns_none_when_no_hit():
    from tostools.tos import _resolve_parent_id

    client = TOSClient()
    with patch.object(client, "basic_search", return_value=[]):
        assert _resolve_parent_id(client, station_marker="XXXX") is None
        assert _resolve_parent_id(client, location_name="nope") is None


def test_device_list_unknown_parent_returns_1(capsys):
    """Unresolvable --station / --location must fail cleanly with exit 1."""
    from tostools.tos import _device_list_main

    with patch("tostools.tos._resolve_parent_id", return_value=None):
        rc = _device_list_main(_list_args(station="XXXX"))

    assert rc == 1
    err = capsys.readouterr().err
    assert "No parent entity found for station marker 'XXXX'" in err


def test_device_list_default_excludes_closed_joins(capsys):
    """Default behaviour shows only open joins. --all surfaces closed too.
    Each child's enriched row carries serial, model, subtype, status, the
    join's time_from/time_to, and id_connection."""
    from tostools.tos import _device_list_main

    parent = {
        "id_entity": 4440,
        "attributes": [{"code": "name", "value": "Saltvík", "date_to": None}],
        "children_connections": [
            {  # open NETR9
                "id_entity_child": 21197,
                "id_entity_connection": 28726,
                "time_from": "2026-05-22T12:00:00",
                "time_to": None,
            },
            {  # closed ASHTECH
                "id_entity_child": 4840,
                "id_entity_connection": 5942,
                "time_from": "2007-08-08T00:00:00",
                "time_to": "2007-09-07T00:00:00",
            },
        ],
    }

    def fake_get_entity_history(eid):
        if eid == 4440:
            return parent
        if eid == 21197:
            return {
                "code_entity_subtype": "gnss_receiver",
                "attributes": [
                    {"code": "serial_number", "value": "5545R50370", "date_to": None},
                    {"code": "model", "value": "TRIMBLE NETR9", "date_to": None},
                    {"code": "status", "value": "virkt", "date_to": None},
                ],
            }
        if eid == 4840:
            return {
                "code_entity_subtype": "gnss_receiver",
                "attributes": [
                    {"code": "serial_number", "value": "320", "date_to": None},
                    {"code": "model", "value": "ASHTECH UZ-12", "date_to": None},
                ],
            }
        return {}

    with (
        patch("tostools.tos._resolve_parent_id", return_value=4440),
        patch.object(
            TOSClient, "get_entity_history", side_effect=fake_get_entity_history
        ),
    ):
        rc = _device_list_main(_list_args(station="SAVI"))

    assert rc == 0
    out = capsys.readouterr().out
    # Header includes resolved parent name + id.
    assert "Saltvík" in out
    assert "id_entity=4440" in out
    # Open join: NETR9 21197 shows up.
    assert "21197" in out
    assert "5545R50370" in out
    assert "TRIMBLE NETR9" in out
    # Closed join: ASHTECH 4840 should NOT show in default view.
    assert "ASHTECH" not in out
    assert "4840" not in out
    assert "1 open join(s)" in out


def test_device_list_all_includes_closed(capsys):
    from tostools.tos import _device_list_main

    parent = {
        "id_entity": 4440,
        "attributes": [{"code": "name", "value": "Saltvík", "date_to": None}],
        "children_connections": [
            {
                "id_entity_child": 4840,
                "id_entity_connection": 5942,
                "time_from": "2007-08-08T00:00:00",
                "time_to": "2007-09-07T00:00:00",
            },
        ],
    }

    def fake_get_entity_history(eid):
        if eid == 4440:
            return parent
        return {
            "code_entity_subtype": "gnss_receiver",
            "attributes": [
                {"code": "serial_number", "value": "320", "date_to": None},
                {"code": "model", "value": "ASHTECH UZ-12", "date_to": None},
            ],
        }

    with (
        patch("tostools.tos._resolve_parent_id", return_value=4440),
        patch.object(
            TOSClient, "get_entity_history", side_effect=fake_get_entity_history
        ),
    ):
        rc = _device_list_main(_list_args(station="SAVI", all=True))

    assert rc == 0
    out = capsys.readouterr().out
    assert "320" in out
    assert "ASHTECH UZ-12" in out
    assert "1 all join(s)" in out


def test_device_list_json_emits_structured_payload(capsys):
    import json

    from tostools.tos import _device_list_main

    parent = {
        "id_entity": 4440,
        "attributes": [{"code": "name", "value": "Saltvík", "date_to": None}],
        "children_connections": [
            {
                "id_entity_child": 21197,
                "id_entity_connection": 28726,
                "time_from": "2026-05-22T12:00:00",
                "time_to": None,
            }
        ],
    }

    def fake_get_entity_history(eid):
        if eid == 4440:
            return parent
        return {
            "code_entity_subtype": "gnss_receiver",
            "attributes": [
                {"code": "serial_number", "value": "5545R50370", "date_to": None},
                {"code": "model", "value": "TRIMBLE NETR9", "date_to": None},
                {"code": "status", "value": "virkt", "date_to": None},
            ],
        }

    with (
        patch("tostools.tos._resolve_parent_id", return_value=4440),
        patch.object(
            TOSClient, "get_entity_history", side_effect=fake_get_entity_history
        ),
    ):
        rc = _device_list_main(_list_args(station="SAVI", json=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["parent_id_entity"] == 4440
    assert payload["parent_name"] == "Saltvík"
    assert payload["include_closed"] is False
    assert len(payload["devices"]) == 1
    dev = payload["devices"][0]
    assert dev["id_entity"] == 21197
    assert dev["serial"] == "5545R50370"
    assert dev["model"] == "TRIMBLE NETR9"
    assert dev["subtype"] == "gnss_receiver"
    assert dev["status"] == "virkt"
    assert dev["id_connection"] == 28726


# ---------------------------------------------------------------------------
# apply_device_filters — reusable helper for any entity-listing CLI
# ---------------------------------------------------------------------------


def _row(**overrides):
    """A canonical enriched row used by `tos device list` and any future
    consumer of apply_device_filters."""
    base = {
        "id_entity": 1,
        "serial": "5039K70763",
        "model": "TRIMBLE NETR9",
        "subtype": "gnss_receiver",
        "status": "virkt",
        "time_from": "2007-09-07T00:00:00",
        "time_to": None,
        "id_connection": 5931,
    }
    base.update(overrides)
    return base


def _filter_args(**overrides):
    """argparse.Namespace shaped like add_device_filter_arguments output."""
    from argparse import Namespace

    defaults = {
        "subtype": None,
        "model": None,
        "status": None,
        "serial": None,
        "date": None,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


def test_apply_device_filters_no_constraints_passes_through():
    from tostools.tos import apply_device_filters

    rows = [_row(id_entity=1), _row(id_entity=2, subtype="antenna")]
    assert apply_device_filters(rows, _filter_args()) == rows


def test_apply_device_filters_subtype_exact_match():
    from tostools.tos import apply_device_filters

    rows = [
        _row(id_entity=1, subtype="gnss_receiver"),
        _row(id_entity=2, subtype="antenna"),
    ]
    out = apply_device_filters(rows, _filter_args(subtype="antenna"))
    assert [r["id_entity"] for r in out] == [2]


def test_apply_device_filters_model_substring_case_insensitive():
    from tostools.tos import apply_device_filters

    rows = [
        _row(id_entity=1, model="TRIMBLE NETR9"),
        _row(id_entity=2, model="ASHTECH UZ-12"),
        _row(id_entity=3, model="SEPT POLARX2"),
    ]
    # Substring + case-insensitive: 'netr9' matches 'TRIMBLE NETR9'.
    out = apply_device_filters(rows, _filter_args(model="netr9"))
    assert [r["id_entity"] for r in out] == [1]


def test_apply_device_filters_status_exact_match():
    from tostools.tos import apply_device_filters

    rows = [
        _row(id_entity=1, status="virkt"),
        _row(id_entity=2, status="bilað"),
        _row(id_entity=3, status="óvirkt"),
    ]
    out = apply_device_filters(rows, _filter_args(status="bilað"))
    assert [r["id_entity"] for r in out] == [2]


def test_apply_device_filters_serial_substring_case_sensitive():
    from tostools.tos import apply_device_filters

    rows = [
        _row(id_entity=1, serial="5039K70763"),
        _row(id_entity=2, serial="5545R50370"),
    ]
    # Substring matches both '5039' and '70763' against the first serial.
    out = apply_device_filters(rows, _filter_args(serial="5039"))
    assert [r["id_entity"] for r in out] == [1]
    # Case-sensitive — lowercase 'k' shouldn't match the uppercase 'K'.
    out2 = apply_device_filters(rows, _filter_args(serial="k70763"))
    assert out2 == []


def test_apply_device_filters_date_on_date_during_open_join():
    """Open join (time_to=None): device is present at any date >= time_from."""
    from tostools.tos import apply_device_filters

    rows = [_row(id_entity=1, time_from="2007-09-07T00:00:00", time_to=None)]
    assert apply_device_filters(rows, _filter_args(date="2010-01-01")) == rows
    assert apply_device_filters(rows, _filter_args(date="2007-09-07")) == rows
    # Before join: excluded.
    assert apply_device_filters(rows, _filter_args(date="2006-01-01")) == []


def test_apply_device_filters_date_during_closed_join():
    """Closed join: device present iff time_from <= date < time_to.
    Boundary on time_to is exclusive (device is gone at time_to)."""
    from tostools.tos import apply_device_filters

    rows = [
        _row(
            id_entity=1,
            time_from="2007-08-08T00:00:00",
            time_to="2007-09-07T00:00:00",
        )
    ]
    # Inside window.
    assert apply_device_filters(rows, _filter_args(date="2007-08-20")) == rows
    # After window — excluded.
    assert apply_device_filters(rows, _filter_args(date="2008-01-01")) == []
    # On the time_to boundary — excluded (device is gone at that moment).
    assert apply_device_filters(rows, _filter_args(date="2007-09-07")) == []


def test_apply_device_filters_combines_filters_with_and():
    """Multiple filters are AND'd."""
    from tostools.tos import apply_device_filters

    rows = [
        _row(
            id_entity=1, subtype="gnss_receiver", model="TRIMBLE NETR9", status="virkt"
        ),
        _row(
            id_entity=2, subtype="gnss_receiver", model="ASHTECH UZ-12", status="virkt"
        ),
        _row(id_entity=3, subtype="antenna", model="AERAT2775_42", status="virkt"),
    ]
    out = apply_device_filters(
        rows, _filter_args(subtype="gnss_receiver", model="NETR9")
    )
    assert [r["id_entity"] for r in out] == [1]


def test_apply_device_filters_tolerates_missing_args_attrs():
    """If args has only some of the filter attrs (e.g. caller used
    add_device_filter_arguments(with_date=False)), missing ones are
    treated as no constraint — getattr default."""
    from argparse import Namespace

    from tostools.tos import apply_device_filters

    # Namespace missing `date` entirely.
    args = Namespace(subtype=None, model=None, status=None, serial=None)
    rows = [_row(id_entity=1)]
    assert apply_device_filters(rows, args) == rows


# ---------------------------------------------------------------------------
# apply_attribute_filters — reusable helper for attribute-period filtering
# ---------------------------------------------------------------------------


def _period(**overrides):
    """A canonical TOS attribute-value period (open by default)."""
    base = {
        "id_attribute_value": 33584,
        "code": "model",
        "value": "TRIMBLE NETR9",
        "date_from": "2007-09-07T00:00:00",
        "date_to": None,
    }
    base.update(overrides)
    return base


def _attr_filter_args(**overrides):
    """argparse.Namespace shaped like add_attribute_filter_arguments output."""
    from argparse import Namespace

    defaults = {
        "codes": None,
        "value": None,
        "on_date": None,
        "suspicious": False,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


def test_apply_attribute_filters_no_constraints_passes_through():
    from tostools.tos import apply_attribute_filters

    periods = [_period(code="model"), _period(code="serial_number", value="5039K70763")]
    assert apply_attribute_filters(periods, _attr_filter_args()) == periods


def test_apply_attribute_filters_code_multiple_or():
    """Multiple --code values are OR'd within the filter, AND'd with others."""
    from tostools.tos import apply_attribute_filters

    periods = [
        _period(id_attribute_value=1, code="model"),
        _period(id_attribute_value=2, code="firmware_version"),
        _period(id_attribute_value=3, code="serial_number"),
    ]
    out = apply_attribute_filters(
        periods, _attr_filter_args(codes=["model", "firmware_version"])
    )
    assert [p["id_attribute_value"] for p in out] == [1, 2]


def test_apply_attribute_filters_value_substring_case_insensitive():
    from tostools.tos import apply_attribute_filters

    periods = [
        _period(id_attribute_value=1, value="TRIMBLE NETR9"),
        _period(id_attribute_value=2, value="ASHTECH UZ-12"),
    ]
    out = apply_attribute_filters(periods, _attr_filter_args(value="netr9"))
    assert [p["id_attribute_value"] for p in out] == [1]


def test_apply_attribute_filters_value_skips_none_value():
    """A period with a missing/None value can't match a substring."""
    from tostools.tos import apply_attribute_filters

    periods = [_period(id_attribute_value=1, value=None)]
    assert apply_attribute_filters(periods, _attr_filter_args(value="anything")) == []


def test_apply_attribute_filters_on_date_open_period():
    """Open period: active at any date >= date_from."""
    from tostools.tos import apply_attribute_filters

    periods = [_period(date_from="2007-09-07T00:00:00", date_to=None)]
    assert (
        apply_attribute_filters(periods, _attr_filter_args(on_date="2010-01-01"))
        == periods
    )
    assert (
        apply_attribute_filters(periods, _attr_filter_args(on_date="2006-01-01")) == []
    )


def test_apply_attribute_filters_on_date_closed_period_exclusive_boundary():
    """Closed period: active iff date_from <= date < date_to.
    Boundary on date_to is exclusive — value retired AT that moment."""
    from tostools.tos import apply_attribute_filters

    periods = [
        _period(
            date_from="2007-08-08T00:00:00",
            date_to="2007-09-07T00:00:00",
        )
    ]
    assert (
        apply_attribute_filters(periods, _attr_filter_args(on_date="2007-08-20"))
        == periods
    )
    # On the date_to boundary — excluded.
    assert (
        apply_attribute_filters(periods, _attr_filter_args(on_date="2007-09-07")) == []
    )


def test_apply_attribute_filters_suspicious_matches_cleanup_artifact_date():
    """--suspicious filters to periods opening on 2014-10-17 (the
    fleet-wide bulk-load date pattern). Other open / closed periods
    irrelevant — only the date_from matters."""
    from tostools.tos import apply_attribute_filters

    periods = [
        _period(id_attribute_value=1, date_from="2007-09-07T00:00:00"),
        _period(id_attribute_value=2, date_from="2014-10-17T00:00:00"),
        _period(id_attribute_value=3, date_from="2014-10-17T12:30:00"),
        _period(id_attribute_value=4, date_from="2015-10-17T00:00:00"),
    ]
    out = apply_attribute_filters(periods, _attr_filter_args(suspicious=True))
    assert [p["id_attribute_value"] for p in out] == [2, 3]


def test_apply_attribute_filters_combines_with_and():
    """Multiple filters are AND'd."""
    from tostools.tos import apply_attribute_filters

    periods = [
        _period(id_attribute_value=1, code="model", value="TRIMBLE NETR9"),
        _period(id_attribute_value=2, code="serial_number", value="5039K70763"),
        _period(id_attribute_value=3, code="model", value="ASHTECH UZ-12"),
    ]
    out = apply_attribute_filters(
        periods, _attr_filter_args(codes=["model"], value="netr9")
    )
    assert [p["id_attribute_value"] for p in out] == [1]


def test_apply_attribute_filters_tolerates_missing_args_attrs():
    """Caller that didn't run add_attribute_filter_arguments still
    gets pass-through behaviour (all defaults missing → no constraint)."""
    from argparse import Namespace

    from tostools.tos import apply_attribute_filters

    args = Namespace()  # No attribute filter attrs at all.
    periods = [_period()]
    assert apply_attribute_filters(periods, args) == periods


# ---------------------------------------------------------------------------
# tos device show — attribute-filter integration
# ---------------------------------------------------------------------------


def _show_filter_args(**overrides):
    """Show args including the new attribute filter defaults."""
    base = {
        "codes": None,
        "value": None,
        "on_date": None,
        "suspicious": False,
    }
    base.update(overrides)
    return _show_args(**base)


def test_device_show_attributes_filters_to_listed_codes(capsys):
    """--code restricts the attribute-history table to listed codes only.
    Uses --attributes-history (which skips the header) so the test can
    assert against the table content without the header's open-attr
    summary getting in the way."""
    from tostools.tos import _device_show_main

    fake_history = {
        "id_entity": 4830,
        "attributes": [
            {
                "code": "serial_number",
                "value": "5039K70763",
                "date_from": "2014-10-17T00:00:00",
                "date_to": None,
                "id_attribute_value": 33582,
            },
            {
                "code": "model",
                "value": "TRIMBLE NETR9",
                "date_from": "2007-09-07T00:00:00",
                "date_to": None,
                "id_attribute_value": 33584,
            },
            {
                "code": "firmware_version",
                "value": "4.1.7",
                "date_from": "2007-09-07T00:00:00",
                "date_to": None,
                "id_attribute_value": 33585,
            },
        ],
    }
    with (
        patch("tostools.devices.find_device", return_value=fake_history),
        patch.object(TOSClient, "get_parent_history", return_value=[]),
    ):
        rc = _device_show_main(
            _show_filter_args(
                id_entity=4830,
                section_attributes_history=True,
                codes=["model", "firmware_version"],
            )
        )

    assert rc == 0
    out = capsys.readouterr().out
    # Both kept codes' VALUES appear in the table (NETR9 may wrap mid-cell
    # so assert on a substring that doesn't span the wrap point).
    assert "NETR9" in out
    assert "4.1.7" in out
    # serial_number suppressed by the code filter — value AND its code
    # label both absent from the (header-less) output.
    assert "5039K70763" not in out
    assert "serial_number" not in out


def test_device_show_attributes_suspicious_only_cleanup_artifact_rows(capsys):
    """--suspicious filters to attribute periods opening on 2014-10-17.
    --attributes-history avoids the header that would otherwise show the
    suppressed value."""
    from tostools.tos import _device_show_main

    fake_history = {
        "id_entity": 4830,
        "attributes": [
            {
                "code": "serial_number",
                "value": "5039K70763",
                "date_from": "2014-10-17T00:00:00",  # cleanup-artifact date
                "date_to": None,
                "id_attribute_value": 33582,
            },
            {
                "code": "model",
                "value": "TRIMBLE NETR9",
                "date_from": "2007-09-07T00:00:00",  # NOT cleanup-artifact
                "date_to": None,
                "id_attribute_value": 33584,
            },
        ],
    }
    with (
        patch("tostools.devices.find_device", return_value=fake_history),
        patch.object(TOSClient, "get_parent_history", return_value=[]),
    ):
        rc = _device_show_main(
            _show_filter_args(
                id_entity=4830,
                section_attributes_history=True,
                suspicious=True,
            )
        )

    assert rc == 0
    out = capsys.readouterr().out
    # serial_number (2014-10-17) survives the filter.
    assert "5039K70763" in out
    # model (2007-09-07) suppressed — value AND code label both absent.
    assert "TRIMBLE NETR9" not in out
    assert "model" not in out


# ---------------------------------------------------------------------------
# apply_visit_filters — pins the read-side vitjun-filter semantics
# ---------------------------------------------------------------------------
def _visit(**overrides):
    """Build a vitjun row matching the shape from
    :meth:`TOSClient.list_maintenance_visits`."""
    defaults = {
        "id": 1000,
        "maintenance_type": "on_site",
        "start_time": "2024-06-15T10:00:00",
        "end_time": "2024-06-15T11:30:00",
        "reason": "Viðgerð",
        "participants": "bgo@vedur.is",
        "participants_names": "Benedikt G. Ófeigsson",
        "work": "—",
        "remaining": None,
        "completed": True,
    }
    defaults.update(overrides)
    return defaults


def _visit_filter_args(**overrides):
    from argparse import Namespace

    defaults = {
        "visit_type": None,
        "reasons": None,
        "since": None,
        "participants": None,
        "open_only": False,
        "completed_only": False,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


def test_apply_visit_filters_no_constraints_passes_through():
    from tostools.tos import apply_visit_filters

    rows = [_visit(id=1), _visit(id=2, maintenance_type="remote")]
    assert apply_visit_filters(rows, _visit_filter_args()) == rows


def test_apply_visit_filters_visit_type_exact_match():
    from tostools.tos import apply_visit_filters

    rows = [
        _visit(id=1, maintenance_type="on_site"),
        _visit(id=2, maintenance_type="remote"),
    ]
    out = apply_visit_filters(rows, _visit_filter_args(visit_type="on_site"))
    assert [r["id"] for r in out] == [1]


def test_apply_visit_filters_reason_translates_to_icelandic_display():
    """--reason takes English codes; matches against the Icelandic
    display strings TOS emits on the list endpoint."""
    from tostools.tos import apply_visit_filters

    rows = [
        _visit(id=1, reason="Breyting"),
        _visit(id=2, reason="Viðgerð"),
        _visit(id=3, reason="Endurbætur"),
    ]
    out = apply_visit_filters(rows, _visit_filter_args(reasons=["repairs"]))
    assert [r["id"] for r in out] == [2]
    out = apply_visit_filters(
        rows, _visit_filter_args(reasons=["change", "improvements"])
    )
    assert [r["id"] for r in out] == [1, 3]


def test_apply_visit_filters_reason_substring_match_within_comma_joined():
    """Rows with multiple active reasons get comma-joined display
    strings — the filter should still match on substring."""
    from tostools.tos import apply_visit_filters

    rows = [_visit(id=1, reason="Breyting, Viðgerð")]
    assert apply_visit_filters(rows, _visit_filter_args(reasons=["repairs"])) == rows


def test_apply_visit_filters_since_lex_compare_yyyy_mm_dd():
    from tostools.tos import apply_visit_filters

    rows = [
        _visit(id=1, start_time="2023-01-15T08:00:00"),
        _visit(id=2, start_time="2026-05-22T15:00:00"),
        _visit(id=3, start_time="2018-02-06T09:30:00"),
    ]
    out = apply_visit_filters(rows, _visit_filter_args(since="2023-01-01"))
    assert sorted(r["id"] for r in out) == [1, 2]


def test_apply_visit_filters_since_excludes_rows_with_missing_start():
    """A row that has no start_time can't be range-compared — drop
    rather than silently pass through."""
    from tostools.tos import apply_visit_filters

    rows = [_visit(id=1, start_time=None)]
    assert apply_visit_filters(rows, _visit_filter_args(since="2023-01-01")) == []


def test_apply_visit_filters_participants_matches_names_or_emails():
    """--participants is case-insensitive substring against either the
    resolved names OR the raw email list (whichever has a value)."""
    from tostools.tos import apply_visit_filters

    rows = [
        _visit(
            id=1,
            participants="bgo@vedur.is",
            participants_names="Benedikt G. Ófeigsson",
        ),
        _visit(
            id=2,
            participants="bhb@vedur.is",
            participants_names="Bergur Hermanns Bergsson",
        ),
    ]
    # Email substring
    out = apply_visit_filters(rows, _visit_filter_args(participants="bgo"))
    assert [r["id"] for r in out] == [1]
    # Name substring (case-insensitive)
    out = apply_visit_filters(rows, _visit_filter_args(participants="bergur"))
    assert [r["id"] for r in out] == [2]


def test_apply_visit_filters_open_vs_completed():
    from tostools.tos import apply_visit_filters

    rows = [
        _visit(id=1, completed=True),
        _visit(id=2, completed=False),
        _visit(id=3, completed=None),
    ]
    assert [
        r["id"] for r in apply_visit_filters(rows, _visit_filter_args(open_only=True))
    ] == [2, 3]
    assert [
        r["id"]
        for r in apply_visit_filters(rows, _visit_filter_args(completed_only=True))
    ] == [1]


def test_apply_visit_filters_combines_with_and():
    from tostools.tos import apply_visit_filters

    rows = [
        _visit(
            id=1,
            maintenance_type="on_site",
            reason="Viðgerð",
            completed=True,
            start_time="2023-05-17T08:00:00",
        ),
        _visit(
            id=2,
            maintenance_type="remote",
            reason="Viðgerð",
            completed=True,
            start_time="2023-05-17T08:00:00",
        ),
        _visit(
            id=3,
            maintenance_type="on_site",
            reason="Breyting",
            completed=True,
            start_time="2023-05-17T08:00:00",
        ),
    ]
    out = apply_visit_filters(
        rows,
        _visit_filter_args(
            visit_type="on_site",
            reasons=["repairs"],
            completed_only=True,
        ),
    )
    assert [r["id"] for r in out] == [1]


def test_apply_visit_filters_tolerates_missing_args_attrs():
    """Behaves as "no constraint" when the args namespace lacks visit
    filter attrs (e.g. helper invoked outside the standard CLI path)."""
    from argparse import Namespace

    from tostools.tos import apply_visit_filters

    rows = [_visit(id=1)]
    bare_ns = Namespace()
    assert apply_visit_filters(rows, bare_ns) == rows
