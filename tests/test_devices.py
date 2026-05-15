"""Unit tests for ``tostools.devices`` primitives.

Covers the read helpers (attribute_at, attribute_at_value,
child_joins, parent_joins, open_joins) and the write wrappers
(open_join, close_join, fill_join_gap, set_attribute, end_attribute,
correct_attribute, transition_attribute, set_open_attribute).

Read tests operate on hand-built history dicts — no network. Write
tests assert that the wrapper forwards to the right TOSWriter
method with the right kwargs.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

from tostools.devices import (
    LEGACY_GPS_ATTRIBUTE_CODES,
    _device_structure,
    attribute_at,
    attribute_at_value,
    child_joins,
    close_join,
    correct_attribute,
    decommission_device,
    device_sessions,
    end_attribute,
    fill_join_gap,
    install_device,
    move_device,
    open_join,
    open_joins,
    parent_joins,
    replace_device,
    set_attribute,
    set_open_attribute,
    slice_attributes_by_window,
    station_at,
    station_sessions,
    transition_attribute,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _attr(
    code: str,
    value: Optional[str],
    date_from: str,
    date_to: Any = None,
    id_attribute_value: int = 0,
) -> Dict[str, Any]:
    return {
        "id_attribute_value": id_attribute_value,
        "code": code,
        "value": value,
        "date_from": date_from,
        "date_to": date_to,
    }


def _join(
    id_conn: int,
    parent: int,
    child: int,
    time_from: str,
    time_to: Any = None,
) -> Dict[str, Any]:
    return {
        "id_entity_connection": id_conn,
        "id_entity_parent": parent,
        "id_entity_child": child,
        "time_from": time_from,
        "time_to": time_to,
    }


# ---------------------------------------------------------------------------
# attribute_at / attribute_at_value
# ---------------------------------------------------------------------------


def test_attribute_at_returns_period_covering_date():
    history = {
        "attributes": [
            _attr("firmware_version", "5.20", "2017-01-01", "2020-01-01"),
            _attr("firmware_version", "5.42", "2020-01-01", None),
        ]
    }
    period = attribute_at(history, "firmware_version", "2018-06-15")
    assert period is not None
    assert period["value"] == "5.20"


def test_attribute_at_returns_open_period_when_in_range():
    history = {
        "attributes": [
            _attr("status", "virkt", "1992-05-28", None),
        ]
    }
    period = attribute_at(history, "status", "2026-05-13")
    assert period is not None
    assert period["value"] == "virkt"
    assert period["date_to"] is None


def test_attribute_at_returns_none_when_before_any_period():
    history = {
        "attributes": [
            _attr("status", "virkt", "1992-05-28", None),
        ]
    }
    assert attribute_at(history, "status", "1990-01-01") is None


def test_attribute_at_returns_none_after_closed_period():
    history = {
        "attributes": [
            _attr("status", "virkt", "1992-05-28", "2026-05-13"),
            _attr("status", "óvirkt", "2026-05-13", None),
        ]
    }
    # 2026-05-12 is before óvirkt opens (excluded by date_from <= when)
    period = attribute_at(history, "status", "2026-05-12")
    assert period is not None
    assert period["value"] == "virkt"


def test_attribute_at_boundary_inclusive_on_date_from():
    history = {
        "attributes": [
            _attr("status", "virkt", "1992-05-28", "2026-05-13"),
            _attr("status", "óvirkt", "2026-05-13", None),
        ]
    }
    # On exactly 2026-05-13: virkt period closed at 2026-05-13 (excl),
    # óvirkt opens at 2026-05-13 (incl). Result: óvirkt.
    period = attribute_at(history, "status", "2026-05-13")
    assert period is not None
    assert period["value"] == "óvirkt"


def test_attribute_at_boundary_against_real_tos_datetime_format():
    """TOS stores dates as ``YYYY-MM-DDT00:00:00`` (the writer promotes
    bare dates on write — see ``TOSWriter._tos_date``). Operators query
    with bare ``YYYY-MM-DD``. Without normalisation, lexical comparison
    ``"2026-05-13" < "2026-05-13T00:00:00"`` flips the boundary case:
    the closed period leaks through and the new open period is rejected.
    """
    history = {
        "attributes": [
            _attr("status", "virkt", "1992-05-28T00:00:00", "2026-05-13T00:00:00"),
            _attr("status", "óvirkt", "2026-05-13T00:00:00", None),
        ]
    }
    period = attribute_at(history, "status", "2026-05-13")
    assert period is not None
    assert (
        period["value"] == "óvirkt"
    ), f"Expected óvirkt at 2026-05-13 boundary, got {period['value']!r}"


def test_attribute_at_filters_by_code():
    history = {
        "attributes": [
            _attr("firmware_version", "5.42", "2020-01-01", None),
            _attr("model", "NETR9", "2010-01-01", None),
        ]
    }
    period = attribute_at(history, "model", "2026-05-13")
    assert period is not None
    assert period["value"] == "NETR9"


def test_attribute_at_returns_none_when_code_not_present():
    history = {"attributes": [_attr("foo", "bar", "2020-01-01", None)]}
    assert attribute_at(history, "nonexistent", "2026-05-13") is None


def test_attribute_at_handles_empty_attributes():
    assert attribute_at({}, "status", "2026-05-13") is None
    assert attribute_at({"attributes": []}, "status", "2026-05-13") is None
    assert attribute_at({"attributes": None}, "status", "2026-05-13") is None


def test_attribute_at_raises_on_empty_when():
    history = {"attributes": [_attr("status", "virkt", "1992-05-28", None)]}
    with pytest.raises(ValueError, match="non-empty"):
        attribute_at(history, "status", "")


def test_attribute_at_value_returns_string():
    history = {"attributes": [_attr("firmware_version", "5.42", "2020-01-01", None)]}
    assert attribute_at_value(history, "firmware_version", "2026-05-13") == "5.42"


def test_attribute_at_value_returns_none_when_no_period():
    history = {"attributes": []}
    assert attribute_at_value(history, "status", "2026-05-13") is None


def test_attribute_at_value_returns_none_when_value_is_none():
    history = {"attributes": [_attr("model", None, "2020-01-01", None)]}
    assert attribute_at_value(history, "model", "2026-05-13") is None


# ---------------------------------------------------------------------------
# child_joins / parent_joins / open_joins
# ---------------------------------------------------------------------------


def test_child_joins_returns_sorted_list():
    history = {
        "children_connections": [
            _join(1, 100, 200, "2020-01-01", "2021-01-01"),
            _join(2, 100, 201, "2010-01-01", "2019-12-31"),
            _join(3, 100, 202, "2021-01-01", None),
        ]
    }
    out = child_joins(history)
    assert [j["id_entity_connection"] for j in out] == [2, 1, 3]


def test_child_joins_returns_empty_when_no_key():
    assert child_joins({}) == []
    assert child_joins({"children_connections": None}) == []


def test_parent_joins_returns_sorted_list():
    history = {
        "parent_connections": [
            _join(2, 101, 999, "2020-01-01", "2021-01-01"),
            _join(1, 100, 999, "2010-01-01", "2019-12-31"),
        ]
    }
    out = parent_joins(history)
    assert [j["id_entity_connection"] for j in out] == [1, 2]


def test_parent_joins_returns_empty_when_no_key():
    assert parent_joins({}) == []


def test_open_joins_parent_role():
    history = {
        "children_connections": [
            _join(1, 100, 200, "2020-01-01", "2021-01-01"),
            _join(2, 100, 201, "2021-01-01", None),
            _join(3, 100, 202, "2022-01-01", None),
        ]
    }
    out = open_joins(history, role="parent")
    assert [j["id_entity_connection"] for j in out] == [2, 3]


def test_open_joins_child_role():
    history = {
        "parent_connections": [
            _join(1, 100, 999, "2010-01-01", "2019-12-31"),
            _join(2, 101, 999, "2019-12-31", None),
        ]
    }
    out = open_joins(history, role="child")
    assert [j["id_entity_connection"] for j in out] == [2]


def test_open_joins_returns_empty_when_all_closed():
    history = {
        "children_connections": [
            _join(1, 100, 200, "2020-01-01", "2021-01-01"),
        ]
    }
    assert open_joins(history, role="parent") == []


def test_open_joins_raises_on_invalid_role():
    with pytest.raises(ValueError, match="role"):
        open_joins({}, role="sibling")


# ---------------------------------------------------------------------------
# Write — joins layer
# ---------------------------------------------------------------------------


def test_open_join_forwards_to_create_entity_connection():
    writer = MagicMock()
    open_join(writer, parent_id=100, child_id=200, date_from="2026-05-13")
    writer.create_entity_connection.assert_called_once_with(
        id_parent=100,
        id_child=200,
        time_from="2026-05-13",
        time_to=None,
    )


def test_open_join_coerces_ids_to_int():
    writer = MagicMock()
    open_join(writer, parent_id="100", child_id="200", date_from="2026-05-13")
    kwargs = writer.create_entity_connection.call_args.kwargs
    assert kwargs["id_parent"] == 100
    assert kwargs["id_child"] == 200


def test_close_join_forwards_to_patch_entity_connection():
    writer = MagicMock()
    close_join(writer, id_connection=26586, date_to="2026-05-13")
    writer.patch_entity_connection.assert_called_once_with(26586, time_to="2026-05-13")


def test_close_join_coerces_id_to_int():
    writer = MagicMock()
    close_join(writer, id_connection="26586", date_to="2026-05-13")
    assert writer.patch_entity_connection.call_args.args[0] == 26586


def test_fill_join_gap_creates_closed_join():
    writer = MagicMock()
    fill_join_gap(
        writer,
        parent_id=4243,
        child_id=4926,
        date_from="2017-04-12",
        date_to="2025-02-08",
    )
    writer.create_entity_connection.assert_called_once_with(
        id_parent=4243,
        id_child=4926,
        time_from="2017-04-12",
        time_to="2025-02-08",
    )


def test_fill_join_gap_requires_date_to():
    writer = MagicMock()
    with pytest.raises(ValueError, match="date_to"):
        fill_join_gap(
            writer,
            parent_id=4243,
            child_id=4926,
            date_from="2017-04-12",
            date_to="",
        )


# ---------------------------------------------------------------------------
# Write — attributes layer
# ---------------------------------------------------------------------------


def test_set_attribute_forwards_to_add_attribute_value():
    writer = MagicMock()
    set_attribute(
        writer,
        device_id=12345,
        code="firmware_version",
        value="5.42",
        date_from="2026-04-01",
    )
    writer.add_attribute_value.assert_called_once_with(
        id_entity=12345,
        code="firmware_version",
        value="5.42",
        date_from="2026-04-01",
        date_to=None,
    )


def test_set_attribute_passes_date_to_when_given():
    writer = MagicMock()
    set_attribute(
        writer,
        device_id=12345,
        code="firmware_version",
        value="5.20",
        date_from="2017-01-01",
        date_to="2020-01-01",
    )
    kwargs = writer.add_attribute_value.call_args.kwargs
    assert kwargs["date_to"] == "2020-01-01"


def test_end_attribute_forwards_to_patch_attribute_value():
    writer = MagicMock()
    end_attribute(writer, id_attribute_value=99, date_to="2026-05-13")
    writer.patch_attribute_value.assert_called_once_with(99, date_to="2026-05-13")


def test_correct_attribute_passes_only_provided_fields():
    writer = MagicMock()
    correct_attribute(
        writer,
        id_attribute_value=42,
        value="corrected",
    )
    writer.patch_attribute_value.assert_called_once_with(
        42, value="corrected", date_from=None, date_to=None
    )


def test_correct_attribute_can_shift_window():
    writer = MagicMock()
    correct_attribute(
        writer,
        id_attribute_value=42,
        date_from="2020-01-02",
        date_to="2021-01-02",
    )
    kwargs = writer.patch_attribute_value.call_args.kwargs
    assert kwargs["date_from"] == "2020-01-02"
    assert kwargs["date_to"] == "2021-01-02"


def test_transition_attribute_forwards_to_transition_attribute_value():
    writer = MagicMock()
    transition_attribute(
        writer,
        device_id=19712,
        code="status",
        new_value="óvirkt",
        date="2026-05-13",
    )
    writer.transition_attribute_value.assert_called_once_with(
        id_entity=19712,
        code="status",
        new_value="óvirkt",
        transition_date="2026-05-13",
    )


def test_set_open_attribute_forwards_to_upsert():
    writer = MagicMock()
    set_open_attribute(
        writer,
        device_id=12345,
        code="firmware_version",
        value="5.42",
        date_from="2026-04-01",
    )
    writer.upsert_attribute_value.assert_called_once_with(
        id_entity=12345,
        code="firmware_version",
        value="5.42",
        date_from="2026-04-01",
    )


# ---------------------------------------------------------------------------
# slice_attributes_by_window
#
# These tests use ``gps_metadata_qc.device_attribute_history`` as the
# oracle. The new primitive must produce byte-identical output when
# called with ``codes=LEGACY_GPS_ATTRIBUTE_CODES`` (the legacy
# hardcoded key list). This locks the contract before any phase 3
# consumer retrofit goes near production.
# ---------------------------------------------------------------------------


def _gnss_device(*attrs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id_entity": 12345,
        "code_entity_subtype": "gnss_receiver",
        "attributes": list(attrs),
    }


# A device with identity + firmware varying mid-session.
SAMPLE_DEVICE = _gnss_device(
    _attr("serial_number", "G1234", "2010-01-01T00:00:00", None, 1),
    _attr("model", "NETR9", "2010-01-01T00:00:00", None, 2),
    _attr("firmware_version", "5.20", "2017-01-01T00:00:00", "2020-01-01T00:00:00", 3),
    _attr("firmware_version", "5.42", "2020-01-01T00:00:00", None, 4),
)


def _legacy_oracle(
    device: Dict[str, Any],
    window_start: str,
    window_end: Optional[str],
) -> Any:
    """Call the legacy kernel as the contract oracle.

    Imported inside the helper so the module-level import cost of
    ``gps_metadata_qc`` doesn't slow non-slice tests.
    """
    import logging as _logging

    from tostools.gps_metadata_qc import device_attribute_history

    return device_attribute_history(
        device, window_start, window_end, loglevel=_logging.CRITICAL
    )


def test_slice_matches_legacy_open_window_firmware_change():
    """Open window crossing one firmware boundary → 2 atomic sub-windows."""
    actual = slice_attributes_by_window(
        SAMPLE_DEVICE,
        "2017-01-01T00:00:00",
        None,
        codes=LEGACY_GPS_ATTRIBUTE_CODES,
    )
    expected = _legacy_oracle(SAMPLE_DEVICE, "2017-01-01T00:00:00", None)
    assert actual == expected


def test_slice_matches_legacy_closed_window_firmware_change():
    """Closed window crossing the firmware boundary → both periods carry."""
    actual = slice_attributes_by_window(
        SAMPLE_DEVICE,
        "2017-01-01T00:00:00",
        "2022-01-01T00:00:00",
        codes=LEGACY_GPS_ATTRIBUTE_CODES,
    )
    expected = _legacy_oracle(
        SAMPLE_DEVICE, "2017-01-01T00:00:00", "2022-01-01T00:00:00"
    )
    assert actual == expected


def test_slice_matches_legacy_pre_firmware_window():
    """Window before any firmware → 1 row, firmware=None, serial+model present."""
    actual = slice_attributes_by_window(
        SAMPLE_DEVICE,
        "2010-01-01T00:00:00",
        "2015-01-01T00:00:00",
        codes=LEGACY_GPS_ATTRIBUTE_CODES,
    )
    expected = _legacy_oracle(
        SAMPLE_DEVICE, "2010-01-01T00:00:00", "2015-01-01T00:00:00"
    )
    assert actual == expected


def test_slice_matches_legacy_window_inside_single_period():
    """Window entirely inside one firmware period → 1 row."""
    actual = slice_attributes_by_window(
        SAMPLE_DEVICE,
        "2018-06-01T00:00:00",
        "2019-06-01T00:00:00",
        codes=LEGACY_GPS_ATTRIBUTE_CODES,
    )
    expected = _legacy_oracle(
        SAMPLE_DEVICE, "2018-06-01T00:00:00", "2019-06-01T00:00:00"
    )
    assert actual == expected


def test_slice_default_codes_uses_present_attributes_only():
    """codes=None uses only the codes actually in history['attributes'].

    For SAMPLE_DEVICE, that's {serial_number, model, firmware_version} —
    rows have those keys plus the universal id_entity, code_entity_subtype,
    date_from, date_to.
    """
    rows = slice_attributes_by_window(SAMPLE_DEVICE, "2017-01-01T00:00:00", None)
    assert len(rows) == 2
    for row in rows:
        assert set(row.keys()) == {
            "id_entity",
            "code_entity_subtype",
            "date_from",
            "date_to",
            "serial_number",
            "model",
            "firmware_version",
        }


def test_slice_coarse_mode_returns_single_row():
    """fine=False: one row covering the window, latest values as of window_end."""
    rows = slice_attributes_by_window(
        SAMPLE_DEVICE,
        "2017-01-01T00:00:00",
        None,
        codes=LEGACY_GPS_ATTRIBUTE_CODES,
        fine=False,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["date_from"] == "2017-01-01T00:00:00"
    assert row["date_to"] is None
    # Latest open firmware
    assert row["firmware_version"] == "5.42"
    assert row["serial_number"] == "G1234"
    assert row["model"] == "NETR9"


def test_slice_coarse_mode_closed_window_uses_end_for_lookup():
    rows = slice_attributes_by_window(
        SAMPLE_DEVICE,
        "2017-01-01T00:00:00",
        "2019-06-01T00:00:00",  # mid-period
        codes=LEGACY_GPS_ATTRIBUTE_CODES,
        fine=False,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["date_to"] == "2019-06-01T00:00:00"
    # firmware 5.20 is the value at 2019-06-01
    assert row["firmware_version"] == "5.20"


def test_slice_raises_on_empty_window_start():
    with pytest.raises(ValueError, match="window_start"):
        slice_attributes_by_window(SAMPLE_DEVICE, "", None)


ANTENNA_DEVICE = _gnss_device(
    _attr("serial_number", "A99", "2010-01-01T00:00:00", None, 10),
    _attr("model", "TRM59800.00", "2010-01-01T00:00:00", None, 11),
    _attr("antenna_height", "1.5", "2010-01-01T00:00:00", "2015-06-15T00:00:00", 12),
    _attr("antenna_height", "1.55", "2015-06-15T00:00:00", None, 13),
    _attr("antenna_offset_north", "0.0", "2010-01-01T00:00:00", None, 14),
    _attr("antenna_offset_east", "0.0", "2010-01-01T00:00:00", None, 15),
    _attr("antenna_reference_point", "DHARP", "2010-01-01T00:00:00", None, 16),
)
# code_entity_subtype is "gnss_receiver" via _gnss_device — override for clarity
ANTENNA_DEVICE["code_entity_subtype"] = "antenna"


@pytest.mark.parametrize(
    "label,window_start,window_end",
    [
        ("antenna_open", "2014-01-01T00:00:00", None),
        ("antenna_closed", "2014-01-01T00:00:00", "2020-01-01T00:00:00"),
        ("antenna_exact_boundary", "2015-06-15T00:00:00", None),
    ],
)
def test_slice_matches_legacy_antenna_height_change(label, window_start, window_end):
    """Antenna with mid-session height change — multiple attribute boundaries."""
    actual = slice_attributes_by_window(
        ANTENNA_DEVICE,
        window_start,
        window_end,
        codes=LEGACY_GPS_ATTRIBUTE_CODES,
    )
    expected = _legacy_oracle(ANTENNA_DEVICE, window_start, window_end)
    assert actual == expected, f"Mismatch for {label}"


def test_slice_empty_attributes_returns_window_only_row():
    """Device with no attributes: one row spanning the window, all codes None."""
    empty = _gnss_device()
    rows = slice_attributes_by_window(
        empty,
        "2017-01-01T00:00:00",
        "2022-01-01T00:00:00",
        codes=LEGACY_GPS_ATTRIBUTE_CODES,
    )
    # boundaries = {ws, we}; one closed sub-window
    assert len(rows) == 1
    assert rows[0]["date_from"] == "2017-01-01T00:00:00"
    assert rows[0]["date_to"] == "2022-01-01T00:00:00"
    for code in LEGACY_GPS_ATTRIBUTE_CODES:
        assert rows[0][code] is None


# ---------------------------------------------------------------------------
# _device_structure — per-subtype field extraction (§3e helper)
# ---------------------------------------------------------------------------


def test_device_structure_gnss_receiver_returns_expected_fields():
    row = {
        "code_entity_subtype": "gnss_receiver",
        "model": "SEPT POLARX5",
        "serial_number": "1234",
        "firmware_version": "5.4.1",
        "software_version": None,
    }
    assert _device_structure(row) == {
        "model": "SEPT POLARX5",
        "serial_number": "1234",
        "firmware_version": "5.4.1",
        "software_version": None,
    }


def test_device_structure_antenna_coerces_missing_floats_to_zero():
    """Legacy contract: None height/offset → 0.0, never None."""
    row = {
        "code_entity_subtype": "antenna",
        "model": "TRM57971.00",
        "serial_number": "A1",
        "antenna_height": None,
        "antenna_offset_north": None,
        "antenna_offset_east": "0.5",
        "antenna_reference_point": "DHARP",
    }
    out = _device_structure(row)
    assert out["antenna_height"] == 0.0
    assert out["antenna_offset_north"] == 0.0
    assert out["antenna_offset_east"] == 0.5
    assert out["antenna_reference_point"] == "DHARP"


def test_device_structure_radome_only_carries_model_and_serial():
    row = {
        "code_entity_subtype": "radome",
        "model": "SCIS",
        "serial_number": None,
    }
    assert _device_structure(row) == {"model": "SCIS", "serial_number": None}


def test_device_structure_monument_falls_back_to_antenna_height():
    """Legacy quirk: monument_height defaults to antenna_height when missing."""
    row = {
        "code_entity_subtype": "monument",
        "serial_number": "mon-1",
        "monument_height": None,
        "antenna_height": "1.5",
        "antenna_offset_north": None,
        "antenna_offset_east": None,
    }
    out = _device_structure(row)
    assert out["monument_height"] == 1.5
    assert out["monument_offset_north"] == 0.0
    assert out["monument_offset_east"] == 0.0


def test_device_structure_unknown_subtype_returns_empty():
    assert _device_structure({"code_entity_subtype": "digitizer"}) == {}


# ---------------------------------------------------------------------------
# device_sessions — children walk + sub-session slicing (§3e)
# ---------------------------------------------------------------------------


def _station_history_with_children(*connections: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id_entity": 100,
        "code_entity_subtype": "station",
        "attributes": [],
        "children_connections": list(connections),
    }


def test_device_sessions_skips_zero_duration_joins():
    """time_from == time_to → skip (matches legacy behaviour)."""
    client = MagicMock()
    station_history = _station_history_with_children(
        {
            "id_entity_connection": 1,
            "id_entity_child": 200,
            "time_from": "2020-01-01T00:00:00",
            "time_to": "2020-01-01T00:00:00",
        }
    )

    result = device_sessions(client, station_history)

    assert result == []
    client.get_entity_history.assert_not_called()


def test_device_sessions_skips_non_tracked_subtypes():
    """A digitizer child is silently dropped when subtypes is the GPS default."""
    client = MagicMock()
    client.get_entity_history.return_value = {
        "id_entity": 200,
        "code_entity_subtype": "digitizer",
        "attributes": [],
    }
    station_history = _station_history_with_children(
        {
            "id_entity_connection": 1,
            "id_entity_child": 200,
            "time_from": "2020-01-01T00:00:00",
            "time_to": "2021-01-01T00:00:00",
        }
    )

    result = device_sessions(client, station_history)

    assert result == []


def test_device_sessions_returns_sorted_per_join_subsessions():
    """One gnss_receiver join over a single firmware → one slice row attached."""
    client = MagicMock()
    client.get_entity_history.return_value = _gnss_device(
        _attr("serial_number", "G1", "2020-01-01T00:00:00", None, 1),
        _attr("model", "NETR9", "2020-01-01T00:00:00", None, 2),
        _attr(
            "firmware_version", "5.20", "2020-01-01T00:00:00", "2021-01-01T00:00:00", 3
        ),
        _attr("firmware_version", "5.42", "2021-01-01T00:00:00", None, 4),
    )
    station_history = _station_history_with_children(
        {
            "id_entity_connection": 99,
            "id_entity_child": 12345,
            "time_from": "2020-01-01T00:00:00",
            "time_to": "2022-01-01T00:00:00",
        }
    )

    rows = device_sessions(client, station_history)

    # 2 sub-windows on the firmware boundary
    assert len(rows) == 2
    assert rows[0]["id_entity_connection"] == 99
    assert rows[0]["device"]["date_from"] == "2020-01-01T00:00:00"
    assert rows[0]["device"]["firmware_version"] == "5.20"
    assert rows[1]["device"]["date_from"] == "2021-01-01T00:00:00"
    assert rows[1]["device"]["firmware_version"] == "5.42"


# ---------------------------------------------------------------------------
# station_sessions / station_at
# ---------------------------------------------------------------------------


def test_station_sessions_returns_empty_when_station_missing():
    client = MagicMock()
    client.get_entity_history.return_value = None
    assert station_sessions(client, 999) == []


def test_station_at_returns_session_covering_date():
    """Pivots produce datetime objects; station_at matches by lexical ISO."""
    client = MagicMock()
    client.get_entity_history.side_effect = [
        _station_history_with_children(
            {
                "id_entity_connection": 1,
                "id_entity_child": 200,
                "time_from": "2020-01-01T00:00:00",
                "time_to": "2021-01-01T00:00:00",
            },
            {
                "id_entity_connection": 2,
                "id_entity_child": 200,
                "time_from": "2021-01-01T00:00:00",
                "time_to": None,
            },
        ),
        # Same device fetched twice (one per join)
        _gnss_device(
            _attr("serial_number", "G1", "2020-01-01T00:00:00", None, 1),
            _attr("model", "NETR9", "2020-01-01T00:00:00", None, 2),
        ),
        _gnss_device(
            _attr("serial_number", "G1", "2020-01-01T00:00:00", None, 1),
            _attr("model", "NETR9", "2020-01-01T00:00:00", None, 2),
        ),
    ]

    result = station_at(client, 100, "2020-06-15")
    assert result is not None
    assert result["time_from"].year == 2020
    assert result["time_to"].year == 2021
    assert result["gnss_receiver"]["serial_number"] == "G1"


def test_station_at_returns_none_before_first_session():
    client = MagicMock()
    client.get_entity_history.side_effect = [
        _station_history_with_children(
            {
                "id_entity_connection": 1,
                "id_entity_child": 200,
                "time_from": "2020-01-01T00:00:00",
                "time_to": None,
            }
        ),
        _gnss_device(
            _attr("serial_number", "G1", "2020-01-01T00:00:00", None, 1),
        ),
    ]
    assert station_at(client, 100, "2019-01-01") is None


# ---------------------------------------------------------------------------
# move_device (§3f) — pure joins layer
# ---------------------------------------------------------------------------


def test_move_device_patches_old_join_and_posts_new():
    writer = MagicMock()
    writer.patch_entity_connection.return_value = {"id": 1, "time_to": "2026-05-13"}
    writer.create_entity_connection.return_value = {"id": 2, "time_from": "2026-05-13"}

    result = move_device(
        writer,
        id_connection=1,
        child_id=200,
        to_parent_id=400,
        date="2026-05-13",
    )

    writer.patch_entity_connection.assert_called_once_with(1, time_to="2026-05-13")
    writer.create_entity_connection.assert_called_once_with(
        id_parent=400, id_child=200, time_from="2026-05-13", time_to=None
    )
    assert result == {
        "closed": {"id": 1, "time_to": "2026-05-13"},
        "opened": {"id": 2, "time_from": "2026-05-13"},
    }


# ---------------------------------------------------------------------------
# decommission_device (§3h)
# ---------------------------------------------------------------------------


def _device_with_open_parent_join(*, id_entity: int = 200, join_id: int = 99):
    return {
        "id_entity": id_entity,
        "code_entity_subtype": "gnss_receiver",
        "attributes": [
            _attr("status", "virkt", "2020-01-01T00:00:00", None, 5),
        ],
        "parent_connections": [
            {
                "id_entity_connection": join_id,
                "id_entity_parent": 100,
                "id_entity_child": id_entity,
                "time_from": "2020-01-01T00:00:00",
                "time_to": None,
            }
        ],
    }


def test_decommission_device_closes_open_join_and_transitions_status():
    client = MagicMock()
    client.get_entity_history.return_value = _device_with_open_parent_join()

    writer = MagicMock()
    writer.patch_entity_connection.return_value = {"id": 99, "time_to": "2026-05-13"}
    writer.transition_attribute_value.return_value = {
        "closed": {"id": 5},
        "opened": {"id": 6, "value": "óvirkt"},
    }

    result = decommission_device(writer, client, device_id=200, date="2026-05-13")

    writer.patch_entity_connection.assert_called_once_with(99, time_to="2026-05-13")
    writer.transition_attribute_value.assert_called_once_with(
        id_entity=200, code="status", new_value="óvirkt", transition_date="2026-05-13"
    )
    assert len(result["closed_joins"]) == 1
    assert result["status_transition"]["opened"]["value"] == "óvirkt"


def test_decommission_device_skips_join_close_when_none_open():
    client = MagicMock()
    history = _device_with_open_parent_join()
    history["parent_connections"][0]["time_to"] = "2024-01-01T00:00:00"  # closed
    client.get_entity_history.return_value = history

    writer = MagicMock()
    writer.transition_attribute_value.return_value = {"closed": None, "opened": {}}

    result = decommission_device(writer, client, device_id=200, date="2026-05-13")

    writer.patch_entity_connection.assert_not_called()
    assert result["closed_joins"] == []
    writer.transition_attribute_value.assert_called_once()


def test_decommission_device_closes_every_open_parent_join():
    """A device with multiple open parents (misconfigured state) → close all."""
    client = MagicMock()
    history = _device_with_open_parent_join()
    history["parent_connections"].append(
        {
            "id_entity_connection": 100,
            "id_entity_parent": 101,
            "id_entity_child": 200,
            "time_from": "2021-01-01T00:00:00",
            "time_to": None,
        }
    )
    client.get_entity_history.return_value = history

    writer = MagicMock()
    writer.patch_entity_connection.return_value = {}
    writer.transition_attribute_value.return_value = {"closed": None, "opened": {}}

    result = decommission_device(writer, client, device_id=200, date="2026-05-13")

    assert writer.patch_entity_connection.call_count == 2
    assert len(result["closed_joins"]) == 2


# ---------------------------------------------------------------------------
# install_device (§3h)
# ---------------------------------------------------------------------------


def _fresh_device(id_entity: int = 200) -> Dict[str, Any]:
    """Device with no status periods yet — case (a) for install_device."""
    return {
        "id_entity": id_entity,
        "code_entity_subtype": "gnss_receiver",
        "attributes": [],
        "parent_connections": [],
    }


def test_install_device_fresh_opens_join_and_sets_initial_status():
    client = MagicMock()
    client.get_entity_history.return_value = _fresh_device()

    writer = MagicMock()
    writer.create_entity_connection.return_value = {"id": 50}
    writer.add_attribute_value.return_value = {"id": 7, "value": "virkt"}

    result = install_device(
        writer, client, parent_id=100, device_id=200, date="2026-05-13"
    )

    writer.create_entity_connection.assert_called_once_with(
        id_parent=100, id_child=200, time_from="2026-05-13", time_to=None
    )
    writer.add_attribute_value.assert_called_once_with(
        id_entity=200,
        code="status",
        value="virkt",
        date_from="2026-05-13",
        date_to=None,
    )
    writer.transition_attribute_value.assert_not_called()
    assert result["status"] == {"id": 7, "value": "virkt"}
    assert result["attributes"] == {}


def test_install_device_already_virkt_is_status_noop():
    """Device currently active → join is still opened, but status untouched."""
    client = MagicMock()
    client.get_entity_history.return_value = _device_with_open_parent_join()

    writer = MagicMock()
    writer.create_entity_connection.return_value = {"id": 50}

    result = install_device(
        writer, client, parent_id=101, device_id=200, date="2026-05-13"
    )

    writer.create_entity_connection.assert_called_once()
    writer.add_attribute_value.assert_not_called()
    writer.transition_attribute_value.assert_not_called()
    assert result["status"] == "noop"


def test_install_device_ovirkt_reactivates_via_transition():
    """Decommissioned device (open status='óvirkt') → transition path."""
    client = MagicMock()
    history = _fresh_device()
    history["attributes"] = [
        _attr("status", "óvirkt", "2024-01-01T00:00:00", None, 8),
    ]
    client.get_entity_history.return_value = history

    writer = MagicMock()
    writer.create_entity_connection.return_value = {"id": 51}
    writer.transition_attribute_value.return_value = {
        "closed": {"id": 8},
        "opened": {"id": 9, "value": "virkt"},
    }

    result = install_device(
        writer, client, parent_id=100, device_id=200, date="2026-05-13"
    )

    writer.transition_attribute_value.assert_called_once_with(
        id_entity=200, code="status", new_value="virkt", transition_date="2026-05-13"
    )
    writer.add_attribute_value.assert_not_called()
    assert result["status"]["opened"]["value"] == "virkt"


def test_install_device_initial_attributes_mix_of_noop_set_transition():
    """Three initial attrs covering all reconcile branches."""
    client = MagicMock()
    history = _fresh_device()
    history["attributes"] = [
        _attr("status", "virkt", "2020-01-01T00:00:00", None, 5),
        # model already matches target — noop
        _attr("model", "SEPT POLARX5", "2020-01-01T00:00:00", None, 10),
        # firmware open with different value → transition
        _attr("firmware_version", "5.4.0", "2020-01-01T00:00:00", None, 11),
        # serial_number has no period → set
    ]
    client.get_entity_history.return_value = history

    writer = MagicMock()
    writer.create_entity_connection.return_value = {"id": 50}
    writer.add_attribute_value.return_value = {"id": 99}
    writer.transition_attribute_value.return_value = {
        "closed": {"id": 11},
        "opened": {"id": 12, "value": "5.4.1"},
    }

    result = install_device(
        writer,
        client,
        parent_id=100,
        device_id=200,
        date="2026-05-13",
        initial_attributes={
            "model": "SEPT POLARX5",
            "firmware_version": "5.4.1",
            "serial_number": "abc123",
        },
    )

    # status is already virkt → no-op
    assert result["status"] == "noop"
    # model matches → no-op
    assert result["attributes"]["model"] == "noop"
    # firmware open with different value → transition called
    writer.transition_attribute_value.assert_called_once_with(
        id_entity=200,
        code="firmware_version",
        new_value="5.4.1",
        transition_date="2026-05-13",
    )
    # serial_number had no period → add_attribute_value POST
    writer.add_attribute_value.assert_any_call(
        id_entity=200,
        code="serial_number",
        value="abc123",
        date_from="2026-05-13",
        date_to=None,
    )


# ---------------------------------------------------------------------------
# replace_device (§3h)
# ---------------------------------------------------------------------------


def test_replace_device_decommissions_then_installs():
    """Chains decommission(out) + install(in) at the same parent."""
    client = MagicMock()
    # First lookup: out device (open join, virkt status)
    out_history = _device_with_open_parent_join(id_entity=201, join_id=10)
    # Second lookup: in device (fresh)
    in_history = _fresh_device(id_entity=202)
    client.get_entity_history.side_effect = [out_history, in_history]

    writer = MagicMock()
    writer.patch_entity_connection.return_value = {"id": 10}
    writer.transition_attribute_value.return_value = {
        "closed": {"id": 5},
        "opened": {"id": 6, "value": "óvirkt"},
    }
    writer.create_entity_connection.return_value = {"id": 50}
    writer.add_attribute_value.return_value = {"id": 7, "value": "virkt"}

    result = replace_device(
        writer,
        client,
        parent_id=100,
        out_device_id=201,
        in_device_id=202,
        date="2026-05-13",
    )

    # Decommission side
    writer.patch_entity_connection.assert_called_once_with(10, time_to="2026-05-13")
    transition_calls = writer.transition_attribute_value.call_args_list
    assert any(call.kwargs.get("new_value") == "óvirkt" for call in transition_calls)

    # Install side — new join at parent_id=100
    writer.create_entity_connection.assert_called_once_with(
        id_parent=100, id_child=202, time_from="2026-05-13", time_to=None
    )

    assert "decommissioned" in result and "installed" in result
    assert result["decommissioned"]["status_transition"]["opened"]["value"] == "óvirkt"
    assert result["installed"]["status"]["value"] == "virkt"
