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
    attribute_at,
    attribute_at_value,
    child_joins,
    close_join,
    correct_attribute,
    end_attribute,
    fill_join_gap,
    open_join,
    open_joins,
    parent_joins,
    set_attribute,
    set_open_attribute,
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
