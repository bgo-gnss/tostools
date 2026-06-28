"""Tests for ``tostools.audit_duplicate_serials`` — the duplicate-serial
detector behind ``tos audit duplicate-serials``.

We monkeypatch ``build_join_index`` / ``enumerate_known_parents`` to return
small in-memory fixtures (avoiding the ~110s live fleet walk) and unit-test
``find_duplicate_serials``'s grouping + synthetic-filtering logic directly.
``Join`` / ``ParentEntity`` are real dataclasses; only ``get_entity_history``
on the client is faked, for per-device subtype/serial.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import tostools.audit_duplicate_serials as dupser
from tostools.history import Join, JoinIndex, ParentEntity


def _client(histories: Dict[int, Dict[str, Any]]) -> MagicMock:
    """A mock client exposing only ``get_entity_history`` (per-device fetch).

    ``MagicMock`` duck-types as a ``TOSClient`` for the type checker while
    we only wire the one method ``find_duplicate_serials`` actually calls.
    """
    client = MagicMock()
    client.get_entity_history.side_effect = lambda i: histories.get(int(i))
    return client


def _join(parent: int, child: int, time_from: str, time_to: Optional[str]) -> Join:
    return Join(
        id_entity_connection=child * 10 + parent,
        id_entity_parent=parent,
        id_entity_child=child,
        time_from=time_from,
        time_to=time_to,
    )


def _dev_hist(subtype: str, serial: Optional[str]) -> Dict[str, Any]:
    attrs: List[Dict[str, Any]] = []
    if serial is not None:
        attrs.append({"code": "serial_number", "value": serial, "date_to": None})
    return {"code_entity_subtype": subtype, "attributes": attrs}


def _parent(eid: int, name: str) -> ParentEntity:
    return ParentEntity(
        id_entity=eid, name=name, code_subtype="geophysical", role="station"
    )


def _wire(monkeypatch, *, by_child: Dict[int, List[Join]], parents: List[ParentEntity]):
    """Patch the module-level enumeration + index builders with fixtures."""
    index = JoinIndex(by_child=by_child, parents_walked=len(parents), parents_failed=0)

    monkeypatch.setattr(
        dupser, "enumerate_known_parents", lambda client, **kw: list(parents)
    )
    monkeypatch.setattr(
        dupser, "build_join_index", lambda client, parents=None, **kw: index
    )


# Parent ids
HOTJ, BRTT, B9 = 100, 200, 999


def test_real_serial_shared_by_two_is_reported(monkeypatch):
    by_child = {
        4910: [_join(HOTJ, 4910, "2012-06-27T00:00:00", "2019-08-01T00:00:00")],
        16358: [_join(BRTT, 16358, "2019-08-02T00:00:00", None)],
    }
    parents = [_parent(HOTJ, "HOTJ"), _parent(BRTT, "BRTT")]
    _wire(monkeypatch, by_child=by_child, parents=parents)
    client = _client(
        {
            4910: _dev_hist("gnss_receiver", "5048K71916"),
            16358: _dev_hist("gnss_receiver", "5048K71916"),
        }
    )

    groups = dupser.find_duplicate_serials(client)
    assert len(groups) == 1
    g = groups[0]
    assert g["subtype"] == "gnss_receiver"
    assert g["serial"] == "5048K71916"
    ids = [e["id_entity"] for e in g["entities"]]
    assert ids == [4910, 16358]

    # 4910 is parked (closed join), 16358 is open at BRTT.
    by_id = {e["id_entity"]: e for e in g["entities"]}
    assert by_id[4910]["parked"] is True
    assert by_id[4910]["open_parent_name"] == "HOTJ"
    assert by_id[16358]["parked"] is False
    assert by_id[16358]["open_parent_name"] == "BRTT"


def test_unique_serial_is_not_reported(monkeypatch):
    by_child = {
        4910: [_join(HOTJ, 4910, "2012-06-27T00:00:00", None)],
        16358: [_join(BRTT, 16358, "2019-08-02T00:00:00", None)],
    }
    parents = [_parent(HOTJ, "HOTJ"), _parent(BRTT, "BRTT")]
    _wire(monkeypatch, by_child=by_child, parents=parents)
    client = _client(
        {
            4910: _dev_hist("gnss_receiver", "AAA111"),
            16358: _dev_hist("gnss_receiver", "BBB222"),
        }
    )
    assert dupser.find_duplicate_serials(client) == []


def test_synthetic_serial_shared_excluded_by_default_included_with_flag(monkeypatch):
    by_child = {
        4910: [_join(HOTJ, 4910, "2012-06-27T00:00:00", None)],
        16358: [_join(BRTT, 16358, "2019-08-02T00:00:00", None)],
    }
    parents = [_parent(HOTJ, "HOTJ"), _parent(BRTT, "BRTT")]
    _wire(monkeypatch, by_child=by_child, parents=parents)
    # 99999999 is all-same-digit -> placeholder (caught by _is_placeholder,
    # NOT by audit_fleet_sweep._synthetic alone).
    client = _client(
        {
            4910: _dev_hist("gnss_receiver", "99999999"),
            16358: _dev_hist("gnss_receiver", "99999999"),
        }
    )
    assert dupser.find_duplicate_serials(client) == []

    groups = dupser.find_duplicate_serials(client, include_synthetic=True)
    assert len(groups) == 1
    assert groups[0]["serial"] == "99999999"


def test_receiver_prefix_synthetic_excluded_by_default(monkeypatch):
    by_child = {
        1: [_join(B9, 1, "2024-01-01T00:00:00", None)],
        2: [_join(B9, 2, "2024-02-01T00:00:00", None)],
    }
    parents = [_parent(B9, "B9")]
    _wire(monkeypatch, by_child=by_child, parents=parents)
    client = _client(
        {
            1: _dev_hist("gnss_receiver", "receiver-FOO-20240101"),
            2: _dev_hist("gnss_receiver", "receiver-FOO-20240101"),
        }
    )
    assert dupser.find_duplicate_serials(client) == []


def test_subtype_filter(monkeypatch):
    # A gnss_receiver pair AND an antenna pair sharing serials.
    by_child = {
        10: [_join(HOTJ, 10, "2020-01-01T00:00:00", None)],
        11: [_join(BRTT, 11, "2020-02-01T00:00:00", None)],
        20: [_join(HOTJ, 20, "2020-01-01T00:00:00", None)],
        21: [_join(BRTT, 21, "2020-02-01T00:00:00", None)],
    }
    parents = [_parent(HOTJ, "HOTJ"), _parent(BRTT, "BRTT")]
    _wire(monkeypatch, by_child=by_child, parents=parents)
    client = _client(
        {
            10: _dev_hist("gnss_receiver", "RX-DUP-1"),
            11: _dev_hist("gnss_receiver", "RX-DUP-1"),
            20: _dev_hist("antenna", "ANT-DUP-1"),
            21: _dev_hist("antenna", "ANT-DUP-1"),
        }
    )

    all_groups = dupser.find_duplicate_serials(client)
    assert len(all_groups) == 2

    rx_only = dupser.find_duplicate_serials(client, subtype="gnss_receiver")
    assert len(rx_only) == 1
    assert rx_only[0]["subtype"] == "gnss_receiver"
    assert rx_only[0]["serial"] == "RX-DUP-1"


def test_different_subtypes_same_serial_do_not_collide(monkeypatch):
    # Same serial string but different subtypes -> distinct keys, neither a dup.
    by_child = {
        10: [_join(HOTJ, 10, "2020-01-01T00:00:00", None)],
        20: [_join(BRTT, 20, "2020-02-01T00:00:00", None)],
    }
    parents = [_parent(HOTJ, "HOTJ"), _parent(BRTT, "BRTT")]
    _wire(monkeypatch, by_child=by_child, parents=parents)
    client = _client(
        {
            10: _dev_hist("gnss_receiver", "SHARED1"),
            20: _dev_hist("antenna", "SHARED1"),
        }
    )
    assert dupser.find_duplicate_serials(client) == []


def test_progress_callback_fires_per_device(monkeypatch):
    by_child = {
        10: [_join(HOTJ, 10, "2020-01-01T00:00:00", None)],
        11: [_join(BRTT, 11, "2020-02-01T00:00:00", None)],
    }
    parents = [_parent(HOTJ, "HOTJ"), _parent(BRTT, "BRTT")]
    _wire(monkeypatch, by_child=by_child, parents=parents)
    client = _client(
        {
            10: _dev_hist("gnss_receiver", "X1"),
            11: _dev_hist("gnss_receiver", "X2"),
        }
    )
    calls: List[tuple] = []
    dupser.find_duplicate_serials(
        client, progress=lambda i, t, n: calls.append((i, t, n))
    )
    assert calls == [(1, 2, 0), (2, 2, 0)]


def test_coverage_out_param_populated(monkeypatch):
    by_child = {
        10: [_join(HOTJ, 10, "2020-01-01T00:00:00", None)],
        11: [_join(BRTT, 11, "2020-02-01T00:00:00", None)],
    }
    parents = [_parent(HOTJ, "HOTJ"), _parent(BRTT, "BRTT")]
    _wire(monkeypatch, by_child=by_child, parents=parents)
    client = _client(
        {
            10: _dev_hist("gnss_receiver", "X1"),
            11: _dev_hist("gnss_receiver", "X2"),
        }
    )
    coverage: Dict[str, int] = {}
    dupser.find_duplicate_serials(client, coverage=coverage)
    assert coverage["parents_walked"] == 2
    assert coverage["parents_failed"] == 0
    assert coverage["total_devices"] == 2


def test_format_report_empty():
    out = dupser.format_report([])
    assert "No duplicate serials" in out
    assert dupser.format_report([], verbose=True).count("\n") >= 1


def test_format_report_renders_group():
    groups = [
        {
            "subtype": "gnss_receiver",
            "serial": "5048K71916",
            "entities": [
                {
                    "id_entity": 4910,
                    "open_parent_id": 100,
                    "open_parent_name": "HOTJ",
                    "n_joins": 1,
                    "parked": True,
                },
                {
                    "id_entity": 16358,
                    "open_parent_id": 200,
                    "open_parent_name": "BRTT",
                    "n_joins": 1,
                    "parked": False,
                },
            ],
        }
    ]
    out = dupser.format_report(groups, verbose=True)
    assert "5048K71916" in out
    assert "4910" in out and "16358" in out
    assert "HOTJ" in out and "BRTT" in out
    assert "parked/closed" in out
    assert "tos device merge" in out


def test_is_placeholder():
    assert dupser._is_placeholder("99999999") is True
    assert dupser._is_placeholder("00000000") is True
    assert dupser._is_placeholder("0000000000") is True
    assert dupser._is_placeholder("receiver-FOO-20240101") is True
    assert dupser._is_placeholder("ABCDEF") is True  # no digits
    assert dupser._is_placeholder("5048K71916") is False
    assert dupser._is_placeholder("4100591") is False
    assert dupser._is_placeholder(None) is False
    assert dupser._is_placeholder("") is False
