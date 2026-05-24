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


def test_device_show_pretty_path_calls_display_device_record_and_prints_parents(
    capsys,
):
    """Happy path: resolve a device by id, render via display_device_record
    (mocked), then print parent_history with names resolved via
    get_entity_history."""
    from tostools.tos import _device_show_main

    fake_history = {
        "id_entity": 17234,
        "code_entity_subtype": "gnss_receiver",
        "attributes": [
            {"code": "serial_number", "value": "3102", "date_to": None},
        ],
    }
    parent_rows = [
        _join(20100, 4, "2012-06-27 00:00:00", time_to="2014-10-17 00:00:00"),
        _join(28104, 4258, "2014-10-17 00:00:00"),  # open
    ]

    # Parent name resolver: get_entity_history(pid) returns an entity dict
    # with an open "name" attribute.
    def fake_get_entity_history(pid):
        names = {4: "B9 - Kjallari - Jörð", 4258: "Hedinshofdi"}
        return {
            "attributes": [
                {"code": "name", "value": names.get(pid, "?"), "date_to": None},
            ]
        }

    with (
        patch("tostools.devices.find_device", return_value=fake_history) as fd,
        patch("tostools.tos.display_device_record") as ddr,
        patch.object(TOSClient, "get_parent_history", return_value=parent_rows) as gph,
        patch.object(
            TOSClient, "get_entity_history", side_effect=fake_get_entity_history
        ),
    ):
        rc = _device_show_main(_show_args(id_entity=17234))

    assert rc == 0
    fd.assert_called_once()
    # The renderer must be invoked with with_joins=False so it doesn't
    # build the slow global join index — fast device inspection is the
    # whole point of this verb.
    assert ddr.call_args.kwargs["with_joins"] is False
    assert ddr.call_args.kwargs["id_entity"] == 17234
    gph.assert_called_once_with(17234)

    out = capsys.readouterr().out
    assert "Parent history (2 join(s)):" in out
    assert "B9 - Kjallari - Jörð" in out
    assert "Hedinshofdi" in out
    assert "id_connection=28104" in out
    assert "[open  ]" in out
    assert "[closed]" in out


def test_device_show_no_parent_history_prints_orphan_notice(capsys):
    """A device with no parent_history rows is either truly orphan or
    unreachable — say so explicitly rather than printing an empty section."""
    from tostools.tos import _device_show_main

    with (
        patch("tostools.devices.find_device", return_value={"id_entity": 17234}),
        patch("tostools.tos.display_device_record"),
        patch.object(TOSClient, "get_parent_history", return_value=[]),
    ):
        rc = _device_show_main(_show_args(id_entity=17234))

    assert rc == 0
    out = capsys.readouterr().out
    assert "Parent history:" in out
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
        patch("tostools.tos.display_device_record") as ddr,
        patch.object(TOSClient, "get_parent_history", return_value=parent_rows),
    ):
        rc = _device_show_main(_show_args(id_entity=17234, json=True))

    assert rc == 0
    ddr.assert_not_called()  # JSON mode skips the pretty renderer

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
