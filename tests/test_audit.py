"""Unit tests for the audit module and the ``tos audit`` CLI handler.

No network required — TOSClient is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tostools.audit import (
    DEFAULT_ORPHAN_SCAN_MODELS,
    GPS_STATION_EXPECTED_SUBTYPES,
    DeviceAuditReport,
    JoinRecord,
    OrphanScanResult,
    StationAuditReport,
    audit_device,
    audit_station,
    canonical_subtype,
    list_orphan_devices,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _device_history(
    id_entity: int,
    subtype: str = "gnss_receiver",
    serial: str = "SN-X",
    parent_id=None,
):
    return {
        "id_entity": id_entity,
        "code_entity_subtype": subtype,
        "id_entity_parent": parent_id,
        "attributes": [
            {"code": "serial_number", "value": serial, "date_to": None},
        ],
        "children_connections": [],
    }


def _station_history(
    id_entity: int,
    name: str,
    connections=None,
    subtype: str = "geophysical",
):
    return {
        "id_entity": id_entity,
        "code_entity_subtype": subtype,
        "id_entity_parent": None,
        "attributes": [
            {"code": "name", "value": name, "date_to": None},
        ],
        "children_connections": list(connections or []),
    }


def _conn(id_child: int, time_from: str = "2025-01-01T00:00:00", time_to=None):
    return {
        "id_entity_child": id_child,
        "time_from": time_from,
        "time_to": time_to,
    }


def _search_hit(
    device_id: int,
    serial: str,
    lvl_two_id,
    lvl_two_name=None,
):
    """Build a basic_search distance=0 hit shaped like TOS returns.

    Pass ``lvl_two_id=None`` to model a device that basic_search can find
    by serial but reports no current parent (truly orphan signal).
    """
    return {
        "code": "serial_number",
        "distance": 0,
        "id_entity": device_id,
        "value_varchar": serial,
        "id_lvl_two": lvl_two_id,
        "name_lvl_two": lvl_two_name,
    }


# Device subtypes for which we auto-synthesize basic_search hits from the
# fixtures. Anything else (stations, antennas in non-receiver tests) does
# not get a hit, which is also how TOS would behave in production.
_DEVICE_SUBTYPES_FOR_AUTO_HITS = {
    "gnss_receiver",
    "antenna",
    "radome",
    "monument",
}


def _client_returning(history_by_id, hits=None):
    """Build a mock TOSClient whose get_entity_history dispatches by id.

    If ``hits`` is None, basic_search auto-synthesizes a distance=0 hit per
    device fixture in ``history_by_id`` (using its ``id_entity_parent`` as
    the synthesized lvl_two). This mirrors a consistent TOS state where
    basic_search and id_entity_parent agree, which is the default
    "happy-path" world the existing tests assume. Tests that exercise the
    fix-the-bug case (basic_search disagrees with stale id_entity_parent)
    must pass ``hits`` explicitly.

    All basic_search calls return the same auto-synthesized list — the
    audit code filters by ``id_entity`` and ``distance`` anyway, so a
    single combined list is sufficient.
    """
    client = MagicMock()
    client.get_entity_history.side_effect = lambda i: history_by_id.get(int(i))

    if hits is None:
        auto = []
        for ent_id, hist in history_by_id.items():
            subtype = hist.get("code_entity_subtype")
            if subtype not in _DEVICE_SUBTYPES_FOR_AUTO_HITS:
                continue
            serial = next(
                (
                    a.get("value")
                    for a in hist.get("attributes") or []
                    if a.get("code") == "serial_number" and a.get("date_to") is None
                ),
                None,
            )
            if not serial:
                continue
            parent_id = hist.get("id_entity_parent")
            parent_name = None
            if parent_id is not None:
                parent_hist = history_by_id.get(int(parent_id))
                if parent_hist:
                    parent_name = next(
                        (
                            a.get("value")
                            for a in parent_hist.get("attributes") or []
                            if a.get("code") == "name" and a.get("date_to") is None
                        ),
                        None,
                    )
            auto.append(_search_hit(int(ent_id), str(serial), parent_id, parent_name))
        hits = auto

    client.basic_search.return_value = list(hits)
    return client


# ---------------------------------------------------------------------------
# canonical_subtype
# ---------------------------------------------------------------------------


def test_canonical_subtype_short_alias():
    assert canonical_subtype("receiver") == "gnss_receiver"


def test_canonical_subtype_canonical_passthrough():
    assert canonical_subtype("gnss_receiver") == "gnss_receiver"
    assert canonical_subtype("antenna") == "antenna"


def test_canonical_subtype_rejects_unknown():
    with pytest.raises(ValueError, match="Unknown subtype"):
        canonical_subtype("nope")


# ---------------------------------------------------------------------------
# audit_device — happy path
# ---------------------------------------------------------------------------


def test_audit_device_single_open_join_is_I1_ok():
    device = _device_history(100, parent_id=50, serial="SN-100")
    station = _station_history(
        50, "RHOF", connections=[_conn(100, "2025-03-15T00:00:00")]
    )
    client = _client_returning({100: device, 50: station})

    report = audit_device(client, id_entity=100)

    assert isinstance(report, DeviceAuditReport)
    assert report.invariant_I1_ok is True
    assert report.invariant_violations == []
    assert report.id_entity == 100
    assert report.subtype == "gnss_receiver"
    assert report.serial == "SN-100"
    assert report.current_parent_id == 50
    assert report.current_parent_name == "RHOF"
    assert len(report.open_joins) == 1
    join = report.open_joins[0]
    assert join.id_entity_parent == 50
    assert join.id_entity_child == 100
    assert join.time_from == "2025-03-15T00:00:00"
    assert join.is_open


def test_audit_device_orphan_no_parent_is_I1_violation():
    device = _device_history(100, parent_id=None)
    client = _client_returning({100: device})

    report = audit_device(client, id_entity=100)

    assert report.invariant_I1_ok is False
    assert any("no current parent" in v for v in report.invariant_violations)
    assert report.current_parent_id is None
    assert report.open_joins == []


def test_audit_device_closed_without_replacement_is_I1_violation():
    """Device's id_entity_parent points to a station, but the join there is
    closed (time_to set). 18 such orphans were found in live TOS (design F)."""
    device = _device_history(100, parent_id=50)
    station = _station_history(
        50,
        "RHOF",
        connections=[_conn(100, time_to="2025-04-01T00:00:00")],
    )
    client = _client_returning({100: device, 50: station})

    report = audit_device(client, id_entity=100)

    assert report.invariant_I1_ok is False
    assert any("I1 orphan" in v for v in report.invariant_violations)
    assert any(
        "attachment closed without replacement" in v
        for v in report.invariant_violations
    )
    assert report.current_parent_id == 50
    assert report.current_parent_name == "RHOF"
    assert report.open_joins == []


def test_audit_device_uses_basic_search_lvl_two_over_stale_id_entity_parent():
    """The fix: when ``device.id_entity_parent`` is stale (still points to an
    old parent) but ``basic_search`` lvl_two reports a different current
    parent, the audit must trust basic_search. Models the live-probed case
    of id_entity=19969: stated parent was 'Grindavík vestur' (no open join
    there), real open join was at 'Grindavík miðja' (id_lvl_two from
    search). The audit should report I1 OK at the new parent."""
    device = _device_history(100, parent_id=50, serial="SN-100")  # stale ptr
    # Stale parent: device says it's here, but there's no open join.
    stale = _station_history(
        50, "Old Place", connections=[_conn(100, time_to="2024-01-01")]
    )
    # Real current parent per basic_search: device IS open-joined here.
    real = _station_history(70, "New Place", connections=[_conn(100, "2025-03-15")])
    client = _client_returning(
        {100: device, 50: stale, 70: real},
        hits=[_search_hit(100, "SN-100", lvl_two_id=70, lvl_two_name="New Place")],
    )

    report = audit_device(client, id_entity=100)

    assert report.invariant_I1_ok is True
    assert report.invariant_violations == []
    assert report.current_parent_id == 70  # basic_search wins, not 50
    assert report.current_parent_name == "New Place"
    assert len(report.open_joins) == 1
    assert report.open_joins[0].id_entity_parent == 70


def test_audit_device_falls_back_to_id_entity_parent_when_search_has_no_hit():
    """When basic_search returns no exact match for the device's serial
    (e.g. the ASHTECH Z-XII3 indexing gap in TOS), the audit falls back to
    the legacy ``id_entity_parent`` attribute and flags the fallback so
    callers can downgrade confidence."""
    device = _device_history(100, parent_id=50, serial="ZXII3-WEIRD")
    station = _station_history(50, "RHOF", connections=[_conn(100, "2025-03-15")])
    # Explicit empty hits → simulate basic_search indexing gap.
    client = _client_returning({100: device, 50: station}, hits=[])

    report = audit_device(client, id_entity=100)

    # I1 ok at the legacy parent, but flagged as "cannot-verify".
    assert report.invariant_I1_ok is True
    assert report.current_parent_id == 50
    assert any("cannot-verify" in v for v in report.invariant_violations)
    assert any("ZXII3-WEIRD" in v for v in report.invariant_violations)


def test_audit_device_search_hit_with_null_lvl_two_is_truly_orphan():
    """When basic_search returns a hit but its ``id_lvl_two`` is null, the
    device has no current parent — a genuine orphan, not a stale-pointer
    false positive."""
    device = _device_history(100, parent_id=None, serial="SN-100")
    client = _client_returning(
        {100: device},
        hits=[_search_hit(100, "SN-100", lvl_two_id=None)],
    )

    report = audit_device(client, id_entity=100)

    assert report.invariant_I1_ok is False
    assert report.current_parent_id is None
    assert any(
        "no current parent" in v or "id_lvl_two is null" in v
        for v in report.invariant_violations
    )


def test_audit_device_no_search_hit_and_no_id_entity_parent_is_unauditable():
    """No basic_search hit AND no id_entity_parent → can't determine any
    parent at all. Reported as ``I1 no-parent`` with the explicit reason."""
    device = _device_history(100, parent_id=None, serial="ORPHAN-SN")
    client = _client_returning({100: device}, hits=[])

    report = audit_device(client, id_entity=100)

    assert report.invariant_I1_ok is False
    assert report.current_parent_id is None
    assert any("no current parent signal" in v for v in report.invariant_violations)


def test_audit_device_multiple_open_joins_is_I1_violation():
    device = _device_history(100, parent_id=50)
    station = _station_history(
        50,
        "RHOF",
        connections=[_conn(100, "2025-01-01"), _conn(100, "2025-03-15")],
    )
    client = _client_returning({100: device, 50: station})

    report = audit_device(client, id_entity=100)

    assert report.invariant_I1_ok is False
    assert any("I1 multi-open" in v for v in report.invariant_violations)
    assert any("2 simultaneous" in v for v in report.invariant_violations)
    assert len(report.open_joins) == 2


def test_audit_device_unrelated_connections_ignored():
    """The parent has other children; only joins matching our device_id count."""
    device = _device_history(100, parent_id=50)
    station = _station_history(
        50,
        "RHOF",
        connections=[
            _conn(999, "2025-01-01"),
            _conn(100, "2025-03-15"),
            _conn(888, "2025-02-01"),
        ],
    )
    client = _client_returning({100: device, 50: station})

    report = audit_device(client, id_entity=100)

    assert report.invariant_I1_ok is True
    assert len(report.open_joins) == 1
    assert report.open_joins[0].id_entity_child == 100


# ---------------------------------------------------------------------------
# audit_device — resolution
# ---------------------------------------------------------------------------


def test_audit_device_serial_lookup_filters_by_subtype():
    """Serial resolution: basic_search → filter for code='serial_number' with
    distance=0 → verify candidate's subtype via history. The CLI passes the
    short ``receiver`` alias; we expect it resolved to ``gnss_receiver``
    before the subtype check."""
    device = _device_history(100, parent_id=50, serial="SN-100")
    station = _station_history(50, "RHOF", connections=[_conn(100, "2025-03-15")])
    client = _client_returning(
        {100: device, 50: station},
        hits=[_search_hit(100, "SN-100", lvl_two_id=50, lvl_two_name="RHOF")],
    )

    report = audit_device(client, serial="SN-100", subtype="receiver")

    # basic_search is called at least once with the serial — exact call
    # count is loose because audit_device may now consult it twice (once for
    # serial resolution, once for current-parent lookup via lvl_two).
    assert client.basic_search.called
    assert all(call.args == ("SN-100",) for call in client.basic_search.call_args_list)
    assert report.id_entity == 100
    assert report.invariant_I1_ok is True


def test_audit_device_serial_lookup_skips_wrong_subtype():
    """basic_search may return a hit for a same-serial antenna; only the
    candidate whose history reports the canonical subtype is accepted."""
    antenna = _device_history(50, subtype="antenna", serial="DUP-SN")
    receiver = _device_history(
        100, parent_id=10, subtype="gnss_receiver", serial="DUP-SN"
    )
    station = _station_history(10, "RHOF", connections=[_conn(100, "2025-03-15")])
    client = _client_returning(
        {50: antenna, 100: receiver, 10: station},
        hits=[
            {
                "code": "serial_number",
                "value_varchar": "DUP-SN",
                "distance": 0,
                "id_lvl_three": 50,  # antenna — wrong subtype
            },
            {
                "code": "serial_number",
                "value_varchar": "DUP-SN",
                "distance": 0,
                "id_lvl_three": 100,  # receiver — match
            },
        ],
    )

    report = audit_device(client, serial="DUP-SN", subtype="receiver")

    assert report.id_entity == 100


def test_audit_device_serial_without_subtype_raises():
    client = MagicMock()
    with pytest.raises(ValueError, match="--subtype"):
        audit_device(client, serial="SN-100")


def test_audit_device_no_target_raises():
    client = MagicMock()
    with pytest.raises(ValueError, match="id_entity or serial"):
        audit_device(client)


def test_audit_device_missing_id_raises_lookup_error():
    client = _client_returning({})
    with pytest.raises(LookupError, match="id_entity=999"):
        audit_device(client, id_entity=999)


def test_audit_device_missing_serial_raises_lookup_error():
    client = MagicMock()
    client.basic_search.return_value = []
    with pytest.raises(LookupError, match="No gnss_receiver with serial"):
        audit_device(client, serial="SN-X", subtype="gnss_receiver")


# ---------------------------------------------------------------------------
# audit_station — happy path
# ---------------------------------------------------------------------------


def test_audit_station_one_per_subtype_is_I2_ok():
    rcv = _device_history(101, "gnss_receiver", "SN-R")
    ant = _device_history(102, "antenna", "SN-A")
    mon = _device_history(103, "monument", "SN-M")
    station = _station_history(
        50,
        "RHOF",
        connections=[
            _conn(101, "2025-01-01"),
            _conn(102, "2025-01-01"),
            _conn(103, "2020-05-01"),
        ],
    )
    client = _client_returning({50: station, 101: rcv, 102: ant, 103: mon})

    report = audit_station(client, id_entity=50)

    assert isinstance(report, StationAuditReport)
    assert report.invariant_I2_ok is True
    assert report.invariant_violations == []
    assert set(report.open_children_by_subtype) == {
        "gnss_receiver",
        "antenna",
        "monument",
    }
    # Full GPS station set → no completeness warnings.
    assert report.completeness_warnings == []


def test_audit_station_two_open_receivers_is_I2_violation():
    rcv1 = _device_history(101, "gnss_receiver", "SN-R1")
    rcv2 = _device_history(201, "gnss_receiver", "SN-R2")
    station = _station_history(
        50,
        "RHOF",
        connections=[_conn(101, "2025-01-01"), _conn(201, "2025-03-15")],
    )
    client = _client_returning({50: station, 101: rcv1, 201: rcv2})

    report = audit_station(client, id_entity=50)

    assert report.invariant_I2_ok is False
    assert any("I2 duplicate gnss_receiver" in v for v in report.invariant_violations)
    assert any("2 open children" in v for v in report.invariant_violations)


def test_audit_station_closed_connections_ignored():
    rcv = _device_history(101, "gnss_receiver")
    station = _station_history(
        50,
        "RHOF",
        connections=[
            _conn(999, "2024-01-01", time_to="2025-01-01"),  # closed; ignored
            _conn(101, "2025-01-01"),
        ],
    )
    client = _client_returning({50: station, 101: rcv})

    report = audit_station(client, id_entity=50)

    assert report.invariant_I2_ok is True
    assert list(report.open_children_by_subtype) == ["gnss_receiver"]


def test_audit_station_partial_set_emits_completeness_warnings():
    """Receiver-only station — tech mid-deploy, antenna not yet installed."""
    rcv = _device_history(101, "gnss_receiver")
    station = _station_history(50, "RHOF", connections=[_conn(101, "2025-01-01")])
    client = _client_returning({50: station, 101: rcv})

    report = audit_station(client, id_entity=50)

    # I2 holds — partial sets are legitimate.
    assert report.invariant_I2_ok is True
    missing = {"antenna", "monument"}
    assert set(GPS_STATION_EXPECTED_SUBTYPES) >= missing
    warned_subtypes = " ".join(report.completeness_warnings)
    for m in missing:
        assert m in warned_subtypes


def test_audit_station_no_open_children():
    station = _station_history(50, "Empty Lab", connections=[])
    client = _client_returning({50: station})

    report = audit_station(client, id_entity=50)

    assert report.invariant_I2_ok is True
    assert report.open_children_by_subtype == {}
    # All expected subtypes flagged.
    assert len(report.completeness_warnings) == len(GPS_STATION_EXPECTED_SUBTYPES)


# ---------------------------------------------------------------------------
# audit_station — resolution
# ---------------------------------------------------------------------------


def test_audit_station_name_lookup_uses_basic_search():
    station = _station_history(50, "RHOF")
    client = _client_returning(
        {50: station},
        hits=[
            {"code": "marker", "value_varchar": "irrelevant", "id_entity": 999},
            {"code": "name", "value_varchar": "RHOF", "id_entity": 50},
        ],
    )

    report = audit_station(client, name="RHOF")

    client.basic_search.assert_called_once_with("RHOF")
    assert report.id_entity == 50
    assert report.name == "RHOF"


def test_audit_station_name_lookup_requires_exact_match():
    client = _client_returning(
        {},
        hits=[{"code": "name", "value_varchar": "RHOFFFF", "id_entity": 50}],
    )
    with pytest.raises(
        LookupError, match="No station entity with exact marker or name"
    ):
        audit_station(client, name="RHOF")


def test_audit_station_no_target_raises():
    client = MagicMock()
    with pytest.raises(ValueError, match="id_entity or name"):
        audit_station(client)


def test_audit_station_missing_id_raises_lookup_error():
    client = _client_returning({})
    with pytest.raises(LookupError, match="id_entity=999"):
        audit_station(client, id_entity=999)


# ---------------------------------------------------------------------------
# JoinRecord
# ---------------------------------------------------------------------------


def test_join_record_is_open():
    open_j = JoinRecord(1, 2, "2025-01-01", None)
    closed_j = JoinRecord(1, 2, "2025-01-01", "2025-03-01")
    assert open_j.is_open
    assert not closed_j.is_open


# ---------------------------------------------------------------------------
# list_orphan_devices
# ---------------------------------------------------------------------------


def _model_hit(model: str, id_entity: int) -> dict:
    """Build a basic_search hit shaped like a TOS model attribute hit."""
    return {
        "code": "model",
        "value_varchar": model,
        "id_lvl_three": id_entity,
    }


def test_list_orphan_devices_uses_default_models_for_subtype():
    """Pass no --model; the function should fall back to DEFAULT_ORPHAN_SCAN_MODELS."""
    client = MagicMock()
    client.basic_search.return_value = []
    client.get_entity_history.return_value = None

    scan = list_orphan_devices(client, subtype="receiver")

    assert scan.subtype == "gnss_receiver"
    expected_models = list(DEFAULT_ORPHAN_SCAN_MODELS["gnss_receiver"])
    assert scan.models_searched == expected_models
    assert client.basic_search.call_count == len(expected_models)


def test_list_orphan_devices_rejects_subtype_without_defaults():
    client = MagicMock()
    with pytest.raises(ValueError, match="No default model list"):
        list_orphan_devices(client, subtype="radome")


def test_list_orphan_devices_explicit_models_override_defaults():
    client = MagicMock()
    client.basic_search.return_value = []

    scan = list_orphan_devices(client, subtype="receiver", models=["FOO"])

    assert scan.models_searched == ["FOO"]
    client.basic_search.assert_called_once_with("FOO")


def test_list_orphan_devices_returns_only_I1_violations():
    """Three receivers found; one is clean, one is a closed orphan, one has
    no current parent. Scan should return the two violations only."""
    healthy = _device_history(101, parent_id=50, serial="HEALTHY")
    closed_orphan = _device_history(102, parent_id=51, serial="ORPHAN")
    no_parent = _device_history(103, parent_id=None, serial="NOPARENT")
    healthy_station = _station_history(
        50, "RHOF", connections=[_conn(101, "2025-01-01")]
    )
    closed_station = _station_history(
        51,
        "VMEY",
        connections=[_conn(102, "2024-01-01", time_to="2025-01-01")],
    )

    history_by_id = {
        101: healthy,
        102: closed_orphan,
        103: no_parent,
        50: healthy_station,
        51: closed_station,
    }
    client = MagicMock()
    client.get_entity_history.side_effect = lambda i: history_by_id.get(int(i))

    # basic_search returns all three candidates for the first model, none for
    # the rest — order is preserved via dedup. Use "POLARX" as the trigger
    # term since that's in DEFAULT_ORPHAN_SCAN_MODELS.
    def _search(term):
        if term == "POLARX":
            return [
                _model_hit("SEPT POLARX5", 101),
                _model_hit("SEPT POLARX5", 102),
                _model_hit("SEPT POLARX5", 103),
            ]
        return []

    client.basic_search.side_effect = _search

    scan = list_orphan_devices(client, subtype="receiver")

    assert isinstance(scan, OrphanScanResult)
    assert scan.subtype == "gnss_receiver"
    assert scan.total_audited == 3
    assert scan.violation_count == 2
    ids = {r.id_entity for r in scan.orphan_reports}
    assert ids == {102, 103}


def test_list_orphan_devices_filters_non_model_hits():
    """basic_search may return hits for the same value under different codes
    (e.g. 'serial_number'); only code='model' hits count as candidates."""
    receiver = _device_history(101, parent_id=50)
    station = _station_history(50, "RHOF", connections=[_conn(101, "2025-01-01")])
    history_by_id = {101: receiver, 50: station}
    client = MagicMock()
    client.get_entity_history.side_effect = lambda i: history_by_id.get(int(i))
    client.basic_search.return_value = [
        {"code": "serial_number", "value_varchar": "POLARX5", "id_lvl_three": 999},
        _model_hit("POLARX5", 101),
    ]

    scan = list_orphan_devices(client, subtype="receiver", models=["POLARX5"])

    assert scan.total_audited == 1
    assert scan.orphan_reports == []


def test_list_orphan_devices_filters_wrong_subtype():
    """A model hit pointing to a non-matching subtype (e.g. antenna with the
    same model string) is discarded after the history check."""
    antenna = _device_history(50, subtype="antenna", serial="A-1")
    client = MagicMock()
    history_by_id = {50: antenna}
    client.get_entity_history.side_effect = lambda i: history_by_id.get(int(i))
    client.basic_search.return_value = [_model_hit("POLARX5", 50)]

    scan = list_orphan_devices(client, subtype="receiver", models=["POLARX5"])

    assert scan.total_audited == 0
    assert scan.orphan_reports == []


def test_audit_device_reports_parent_subtype():
    """The parent's code_entity_subtype propagates onto the device report so
    callers can tell 'deployed at a real station' apart from 'in warehouse'
    without an extra round-trip."""
    device = _device_history(100, parent_id=50)
    station = _station_history(
        50, "RHOF", connections=[_conn(100, "2025-03-15")], subtype="geophysical"
    )
    client = _client_returning({100: device, 50: station})

    report = audit_device(client, id_entity=100)

    assert report.current_parent_subtype == "geophysical"


def test_audit_device_parent_subtype_is_lager_for_warehouse():
    device = _device_history(100, parent_id=4)
    warehouse = _station_history(
        4,
        "B9 - Kjallari - Jörð",
        connections=[_conn(100, "2025-03-15")],
        subtype="lager",
    )
    client = _client_returning({100: device, 4: warehouse})

    report = audit_device(client, id_entity=100)

    assert report.current_parent_subtype == "lager"
    assert report.invariant_I1_ok is True


# ---------------------------------------------------------------------------
# Real station vs. warehouse — I2 application
# ---------------------------------------------------------------------------


def test_audit_station_warehouse_skips_I2_with_many_receivers():
    """B9-like inventory: many open receivers is the expected steady state.
    I2 must not fire on a Lager-style entity."""
    receivers = {
        i: _device_history(i, "gnss_receiver", f"SN-{i}") for i in (201, 202, 203)
    }
    warehouse = _station_history(
        4,
        "B9 - Kjallari - Jörð",
        connections=[_conn(i, "2024-01-01") for i in (201, 202, 203)],
        subtype="lager",
    )
    history_by_id = {4: warehouse, **receivers}
    client = _client_returning(history_by_id)

    report = audit_station(client, id_entity=4)

    assert report.subtype == "lager"
    assert report.is_real_station is False
    # Three open receivers — but I2 doesn't apply to warehouses.
    assert report.invariant_I2_ok is True
    assert report.invariant_violations == []
    # No completeness warnings either; warehouses have no expected set.
    assert report.completeness_warnings == []
    assert len(report.open_children_by_subtype["gnss_receiver"]) == 3


def test_audit_station_real_station_still_enforces_I2():
    """The same shape on a geophysical station IS an I2 violation."""
    rcv1 = _device_history(101, "gnss_receiver", "SN-R1")
    rcv2 = _device_history(102, "gnss_receiver", "SN-R2")
    station = _station_history(
        50,
        "RHOF",
        connections=[_conn(101, "2025-01-01"), _conn(102, "2025-03-15")],
        subtype="geophysical",
    )
    client = _client_returning({50: station, 101: rcv1, 102: rcv2})

    report = audit_station(client, id_entity=50)

    assert report.is_real_station is True
    assert report.subtype == "geophysical"
    assert report.invariant_I2_ok is False


def test_audit_station_unknown_subtype_treated_as_inventory():
    """Defensive: any subtype not in REAL_STATION_SUBTYPES skips I2.
    This protects against new TOS subtypes (e.g. 'area' admin grouping)."""
    device = _device_history(101, parent_id=99)
    grouping = _station_history(
        99,
        "Reykjanes peninsula sites",
        connections=[_conn(101, "2025-01-01"), _conn(102, "2025-02-01")],
        subtype="area",
    )
    client = _client_returning({99: grouping, 101: device, 102: device})

    report = audit_station(client, id_entity=99)

    assert report.is_real_station is False
    assert report.subtype == "area"
    assert report.invariant_I2_ok is True
    assert report.completeness_warnings == []


# ---------------------------------------------------------------------------
# Marker-vs-name lookup
# ---------------------------------------------------------------------------


def test_audit_station_marker_lookup_resolves_RHOF():
    """The common case: operator types 'RHOF' which is a marker, not a name.
    `Raufarhöfn` is the display name. Marker should match first."""
    station = _station_history(4390, "Raufarhöfn")
    client = _client_returning(
        {4390: station},
        hits=[
            {"code": "marker", "value_varchar": "RHOF", "id_entity": 4390},
            {"code": "name", "value_varchar": "Raufarhöfn", "id_entity": 4390},
        ],
    )

    report = audit_station(client, name="RHOF")

    client.basic_search.assert_called_once_with("RHOF")
    assert report.id_entity == 4390


def test_audit_station_marker_preferred_over_name_when_both_match():
    """If a basic_search hit list contains both a marker and a name with the
    same value, prefer the marker. This is the common case for station ids."""
    rhof = _station_history(4390, "Raufarhöfn")
    other = _station_history(9999, "Other station with name RHOF")
    client = _client_returning(
        {4390: rhof, 9999: other},
        hits=[
            {"code": "name", "value_varchar": "RHOF", "id_entity": 9999},
            {"code": "marker", "value_varchar": "RHOF", "id_entity": 4390},
        ],
    )

    report = audit_station(client, name="RHOF")

    # Marker resolution wins → 4390 (Raufarhöfn), not 9999.
    assert report.id_entity == 4390


def test_audit_station_marker_match_is_case_insensitive():
    """TOS stores markers lowercase ('rhof') but operators type 'RHOF'.
    Both must resolve."""
    station = _station_history(4390, "Raufarhöfn")
    client = _client_returning(
        {4390: station},
        hits=[{"code": "marker", "value_varchar": "rhof", "id_entity": 4390}],
    )

    report_upper = audit_station(client, name="RHOF")
    assert report_upper.id_entity == 4390


def test_audit_station_name_collision_prefers_geophysical():
    """Real TOS data: 'Raufarhöfn' matches a weather station, a GPS station,
    and two parent entities. The GPS audit picks the geophysical match."""
    gps_station = _station_history(4390, "Raufarhöfn", subtype="geophysical")
    weather_station = _station_history(999, "Raufarhöfn", subtype="meteorological")
    client = _client_returning(
        {4390: gps_station, 999: weather_station},
        hits=[
            {
                "code": "name",
                "value_varchar": "Raufarhöfn",
                "id_entity": 999,
                "subtype_lvl_two": "Veðurstöð",
            },
            {
                "code": "name",
                "value_varchar": "Raufarhöfn",
                "id_entity": 4390,
                "subtype_lvl_two": "Jarðeðlisstöð",
            },
        ],
    )

    report = audit_station(client, name="Raufarhöfn")

    assert report.id_entity == 4390  # The geophysical one.


def test_audit_station_name_collision_with_multiple_geophysical_raises():
    """Defensive: if two geophysical stations share a name (shouldn't happen
    but TOS imposes no uniqueness), surface the ambiguity rather than
    silently picking one."""
    client = MagicMock()
    client.get_entity_history.return_value = None
    client.basic_search.return_value = [
        {
            "code": "name",
            "value_varchar": "TwinName",
            "id_entity": 1,
            "subtype_lvl_two": "Jarðeðlisstöð",
        },
        {
            "code": "name",
            "value_varchar": "TwinName",
            "id_entity": 2,
            "subtype_lvl_two": "Jarðeðlisstöð",
        },
    ]
    with pytest.raises(LookupError, match="Multiple geophysical stations"):
        audit_station(client, name="TwinName")


def test_audit_station_falls_back_to_name_when_no_marker_match():
    """A full display name like 'Raufarhöfn' isn't a marker — must hit the
    name fallback. Confirms the marker-first ordering doesn't break the
    long-name workflow."""
    station = _station_history(4390, "Raufarhöfn")
    client = _client_returning(
        {4390: station},
        hits=[{"code": "name", "value_varchar": "Raufarhöfn", "id_entity": 4390}],
    )

    report = audit_station(client, name="Raufarhöfn")

    assert report.id_entity == 4390


def test_list_orphan_devices_dedups_across_models():
    """A device that appears in multiple model searches is audited once."""
    receiver = _device_history(101, parent_id=50)
    station = _station_history(50, "RHOF", connections=[_conn(101, "2025-01-01")])
    history_by_id = {101: receiver, 50: station}
    client = MagicMock()
    client.get_entity_history.side_effect = lambda i: history_by_id.get(int(i))
    client.basic_search.return_value = [_model_hit("POLARX5", 101)]

    scan = list_orphan_devices(client, subtype="receiver", models=["M1", "M2"])

    assert scan.total_audited == 1
