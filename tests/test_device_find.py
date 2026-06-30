"""Tests for ``tostools.device_find`` — the ``tos device find`` dup-guard.

We build small in-memory ``JoinIndex`` fixtures and inject them directly via
``find_devices_by_serial(index=..., parents=...)`` — no monkeypatching, no
~110s fleet walk. Only ``get_entity_history`` on the client is faked.

The load-bearing test is :func:`test_not_found_with_failed_parents_is_inconclusive`
— absence must NOT collapse to CREATE when the walk was incomplete (the GRAN
duplicate-mint footgun).
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

import tostools.device_find as df
import tostools.tos as tos_cli
from tostools.audit_duplicate_serials import DeviceScanRow
from tostools.history import Join, JoinIndex, ParentEntity


def _client(histories: Dict[int, Dict[str, Any]]) -> MagicMock:
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


def _index(by_child: Dict[int, List[Join]], *, parents_failed: int = 0) -> JoinIndex:
    return JoinIndex(
        by_child=by_child,
        parents_walked=3,
        parents_failed=parents_failed,
    )


# Parent ids
HOTJ, BRTT, B9 = 100, 200, 999


def test_not_found_clean_walk_is_create():
    by_child = {4910: [_join(HOTJ, 4910, "2012-06-27T00:00:00", None)]}
    index = _index(by_child)
    client = _client({4910: _dev_hist("gnss_receiver", "AAA111")})

    res = df.find_devices_by_serial(
        client, "3070340", parents=[_parent(HOTJ, "HOTJ")], index=index
    )
    assert res.matches == []
    assert res.bucket == df.CREATE
    assert "CREATE" in df.format_report(res)


def test_not_found_with_failed_parents_is_inconclusive():
    # The coverage hard-gate: a device under the unreadable parent could carry
    # this serial, so absence is unconfirmed → never CREATE.
    by_child = {4910: [_join(HOTJ, 4910, "2012-06-27T00:00:00", None)]}
    index = _index(by_child, parents_failed=1)
    client = _client({4910: _dev_hist("gnss_receiver", "AAA111")})

    res = df.find_devices_by_serial(
        client, "3070340", parents=[_parent(HOTJ, "HOTJ")], index=index
    )
    assert res.matches == []
    assert res.bucket == df.INCONCLUSIVE
    assert res.parents_failed == 1
    assert "UNCONFIRMED" in df.format_report(res)


def test_single_attached_is_attached():
    by_child = {16358: [_join(BRTT, 16358, "2019-08-02T00:00:00", None)]}
    index = _index(by_child)
    client = _client({16358: _dev_hist("gnss_receiver", "5048K71916")})

    res = df.find_devices_by_serial(
        client, "5048K71916", parents=[_parent(BRTT, "BRTT")], index=index
    )
    assert [m.id_entity for m in res.matches] == [16358]
    assert res.bucket == df.ATTACHED
    assert res.matches[0].open_parent_name == "BRTT"
    assert res.matches[0].parked is False


def test_single_parked_is_reopen():
    # closed join → detached (parked in B9 / orphan)
    by_child = {4910: [_join(B9, 4910, "2012-06-27T00:00:00", "2019-08-01T00:00:00")]}
    index = _index(by_child)
    client = _client({4910: _dev_hist("gnss_receiver", "3070340")})

    res = df.find_devices_by_serial(
        client, "3070340", parents=[_parent(B9, "B9")], index=index
    )
    assert res.bucket == df.REOPEN
    assert res.matches[0].parked is True


def test_two_entities_is_duplicate():
    by_child = {
        4910: [_join(HOTJ, 4910, "2012-06-27T00:00:00", "2019-08-01T00:00:00")],
        16358: [_join(BRTT, 16358, "2019-08-02T00:00:00", None)],
    }
    index = _index(by_child)
    client = _client(
        {
            4910: _dev_hist("gnss_receiver", "5048K71916"),
            16358: _dev_hist("gnss_receiver", "5048K71916"),
        }
    )
    res = df.find_devices_by_serial(
        client,
        "5048K71916",
        parents=[_parent(HOTJ, "HOTJ"), _parent(BRTT, "BRTT")],
        index=index,
    )
    assert [m.id_entity for m in res.matches] == [4910, 16358]
    assert res.bucket == df.DUPLICATE


def test_subtype_filter_excludes_other_subtypes():
    # Same serial on a receiver and an antenna; --subtype isolates one.
    by_child = {
        4910: [_join(HOTJ, 4910, "2012-06-27T00:00:00", None)],
        16358: [_join(BRTT, 16358, "2019-08-02T00:00:00", None)],
    }
    index = _index(by_child)
    client = _client(
        {
            4910: _dev_hist("gnss_receiver", "SHARED1"),
            16358: _dev_hist("antenna", "SHARED1"),
        }
    )
    parents = [_parent(HOTJ, "HOTJ"), _parent(BRTT, "BRTT")]

    # Without subtype: two matches → DUPLICATE across subtypes.
    res_any = df.find_devices_by_serial(client, "SHARED1", parents=parents, index=index)
    assert res_any.bucket == df.DUPLICATE

    # With subtype: only the receiver matches → ATTACHED.
    res_rx = df.find_devices_by_serial(
        client, "SHARED1", subtype="gnss_receiver", parents=parents, index=index
    )
    assert [m.id_entity for m in res_rx.matches] == [4910]
    assert res_rx.bucket == df.ATTACHED


def test_serial_whitespace_is_trimmed():
    by_child = {16358: [_join(BRTT, 16358, "2019-08-02T00:00:00", None)]}
    index = _index(by_child)
    client = _client({16358: _dev_hist("gnss_receiver", " 5048K71916 ")})

    res = df.find_devices_by_serial(
        client, "5048K71916", parents=[_parent(BRTT, "BRTT")], index=index
    )
    assert [m.id_entity for m in res.matches] == [16358]


def test_to_json_dict_shape():
    by_child = {16358: [_join(BRTT, 16358, "2019-08-02T00:00:00", None)]}
    index = _index(by_child)
    client = _client({16358: _dev_hist("gnss_receiver", "5048K71916")})

    res = df.find_devices_by_serial(
        client, "5048K71916", parents=[_parent(BRTT, "BRTT")], index=index
    )
    d = res.to_json_dict()
    assert d["serial"] == "5048K71916"
    assert d["bucket"] == df.ATTACHED
    assert d["coverage"]["parents_failed"] == 0
    assert d["matches"][0]["id_entity"] == 16358
    assert d["matches"][0]["open_parent_name"] == "BRTT"


# --- CLI handler: the exit code is a create-gate (0 == CREATE only) ----------


def _scan_row(eid: int, parked: bool) -> DeviceScanRow:
    return DeviceScanRow(
        id_entity=eid,
        subtype="gnss_receiver",
        serial="S",
        open_parent_id=1,
        open_parent_name="X",
        n_joins=1,
        parked=parked,
    )


@pytest.mark.parametrize(
    "bucket, matches, coverage, expected_rc",
    [
        (df.CREATE, [], {"parents_failed": 0}, 0),
        (df.INCONCLUSIVE, [], {"parents_failed": 1}, 1),
        (df.ATTACHED, [_scan_row(10, False)], {"parents_failed": 0}, 3),
        (df.REOPEN, [_scan_row(10, True)], {"parents_failed": 0}, 3),
        (
            df.DUPLICATE,
            [_scan_row(10, False), _scan_row(11, False)],
            {"parents_failed": 0},
            4,
        ),
    ],
)
def test_cli_exit_code_is_create_gate(
    monkeypatch, capsys, bucket, matches, coverage, expected_rc
):
    """`tos device find … && tos device add …` must be a safe gate: exit 0
    only when the serial is provably absent (CREATE)."""
    lookup = df.SerialLookup(
        serial="S", subtype=None, matches=matches, coverage=coverage, bucket=bucket
    )
    monkeypatch.setattr(df, "find_devices_by_serial", lambda *a, **k: lookup)
    args = argparse.Namespace(
        serial="S",
        subtype=None,
        server="vi-api.vedur.is",
        port=443,
        json=False,
        no_progress=True,
    )
    rc = tos_cli._device_find_main(args)
    assert rc == expected_rc
    # The bucket is surfaced in the plain-text report.
    assert bucket.upper() in capsys.readouterr().out


def test_cli_json_path_emits_json_and_same_code(monkeypatch, capsys):
    import json as _json

    lookup = df.SerialLookup(
        serial="S",
        subtype=None,
        matches=[],
        coverage={"parents_failed": 0},
        bucket=df.CREATE,
    )
    monkeypatch.setattr(df, "find_devices_by_serial", lambda *a, **k: lookup)
    args = argparse.Namespace(
        serial="S",
        subtype=None,
        server="vi-api.vedur.is",
        port=443,
        json=True,
        no_progress=True,
    )
    rc = tos_cli._device_find_main(args)
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["bucket"] == df.CREATE
    assert payload["serial"] == "S"
