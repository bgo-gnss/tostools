"""Tests for the `tos audit apply` action-file workflow.

Covers the parser (`_parse_action_file`), the dispatcher
(`_dispatch_action`), and the strict-then-permissive flow of
`_apply_main` (refuse on parse error; continue on individual failure).
"""

from __future__ import annotations

import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from tostools.tos import (
    ParsedAction,
    ParseError,
    _dispatch_action,
    _fetch_action_meta,
    _git_commit_triage_file,
    _parse_action_file,
    _resolve_date_token,
)

# ---------------------------------------------------------------------------
# _parse_action_file — happy paths
# ---------------------------------------------------------------------------


def test_parse_action_file_empty_input():
    actions, errors = _parse_action_file("")
    assert actions == []
    assert errors == []


def test_parse_action_file_comments_and_blanks_ignored():
    text = (
        "# header comment\n" "\n" "  # indented comment\n" "ACTION 16321 defer\n" "\n"
    )
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert len(actions) == 1
    assert actions[0].id_entity == 16321
    assert actions[0].verb == "defer"


def test_parse_action_file_inline_comment_trimmed():
    text = "ACTION 16321 change-subtype digitizer  # CMG-ELP → stafsetjari\n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert len(actions) == 1
    assert actions[0].verb == "change-subtype"
    assert actions[0].args == ["digitizer"]


def test_parse_action_file_multiple_actions_preserve_order():
    text = (
        "ACTION 16321 change-subtype digitizer\n"
        "ACTION 16576 change-subtype gps_clock\n"
        "ACTION 4926 defer\n"
        "ACTION 19712 defer\n"
    )
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert [a.id_entity for a in actions] == [16321, 16576, 4926, 19712]


def test_parse_action_file_tolerates_extra_whitespace():
    text = "  ACTION   16321   change-subtype   digitizer  \n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert actions[0].args == ["digitizer"]


# ---------------------------------------------------------------------------
# _parse_action_file — error paths
# ---------------------------------------------------------------------------


def test_parse_action_file_missing_action_keyword():
    text = "16321 change-subtype digitizer\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert len(errors) == 1
    assert "expected line to start with 'ACTION'" in errors[0].message


def test_parse_action_file_rejects_non_int_id():
    text = "ACTION abc defer\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert len(errors) == 1
    assert "id_entity must be int" in errors[0].message


def test_parse_action_file_rejects_unknown_verb():
    text = "ACTION 16321 frobnicate 1 4\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert len(errors) == 1
    assert "unknown verb 'frobnicate'" in errors[0].message


def test_parse_action_file_change_subtype_requires_one_arg():
    text = "ACTION 16321 change-subtype\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "change-subtype requires exactly one argument" in errors[0].message


def test_parse_action_file_change_subtype_rejects_extra_args():
    text = "ACTION 16321 change-subtype digitizer extra\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "change-subtype requires exactly one argument" in errors[0].message


def test_parse_action_file_defer_takes_no_args():
    text = "ACTION 16321 defer something\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "defer takes no arguments" in errors[0].message


def test_parse_action_file_short_line():
    text = "ACTION 16321\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "needs at least" in errors[0].message


def test_parse_action_file_collects_multiple_errors():
    """A single malformed line doesn't short-circuit parsing — operators
    want to see every problem in one pass, not whack-a-mole."""
    text = (
        "ACTION 16321 defer\n"
        "ACTION abc defer\n"
        "ACTION 16576 fill-gap 1 4\n"
        "ACTION 19712 defer\n"
    )
    actions, errors = _parse_action_file(text)
    # Even one parse error makes the runner refuse to apply; we still
    # want to see all errors enumerated.
    assert len(errors) == 2
    assert errors[0].line_no == 2
    assert errors[1].line_no == 3
    # Valid actions are still returned (caller decides what to do).
    assert [a.id_entity for a in actions] == [16321, 19712]


# ---------------------------------------------------------------------------
# _dispatch_action
# ---------------------------------------------------------------------------


def _make_action(id_entity: int, verb: str, *args: str) -> ParsedAction:
    return ParsedAction(
        line_no=1,
        id_entity=id_entity,
        verb=verb,
        args=list(args),
        raw=f"ACTION {id_entity} {verb} {' '.join(args)}".strip(),
    )


def test_dispatch_defer_returns_deferred_no_writer_call():
    writer = MagicMock()
    result = _dispatch_action(writer, _make_action(16321, "defer"))
    assert result.status == "deferred"
    writer.update_entity_subtype.assert_not_called()


def test_dispatch_change_subtype_calls_update_entity_subtype_with_int_id():
    """The dispatcher translates the string code into the integer FK
    expected by /admin_entity_row/<id>."""
    writer = MagicMock()
    writer.update_entity_subtype.return_value = {
        "id": 16321,
        "id_entity_subtype": 25,
    }
    result = _dispatch_action(
        writer,
        _make_action(16321, "change-subtype", "digitizer"),
        subtype_id_by_code={"digitizer": 25, "gps_clock": 29},
    )
    assert result.status == "ok"
    writer.update_entity_subtype.assert_called_once_with(16321, 25)
    assert "digitizer" in result.detail
    assert "id_entity_subtype=25" in result.detail


def test_dispatch_change_subtype_unknown_code_fails_without_calling_writer():
    """An unmapped code (typo, stale vault note) must not silently write."""
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(16321, "change-subtype", "stafsetjari"),  # not a valid code
        subtype_id_by_code={"digitizer": 25, "gps_clock": 29},
    )
    assert result.status == "failed"
    assert "unknown subtype code" in result.detail
    writer.update_entity_subtype.assert_not_called()


def test_dispatch_change_subtype_captures_writer_exception():
    """An update_entity_subtype failure becomes a 'failed' result, not a
    raised exception — so the runner keeps going on the remaining actions."""
    writer = MagicMock()
    writer.update_entity_subtype.side_effect = RuntimeError("simulated 400")
    result = _dispatch_action(
        writer,
        _make_action(16321, "change-subtype", "digitizer"),
        subtype_id_by_code={"digitizer": 25},
    )
    assert result.status == "failed"
    assert "simulated 400" in result.detail


def test_dispatch_change_subtype_without_mapping_fails():
    """Missing the subtype_id_by_code map entirely → fail with clear error,
    no write attempt. Catches an apply-time programming error rather than
    silently sending a malformed payload."""
    writer = MagicMock()
    result = _dispatch_action(
        writer, _make_action(16321, "change-subtype", "digitizer")
    )
    assert result.status == "failed"
    assert "unknown subtype code" in result.detail


# ---------------------------------------------------------------------------
# parse_error dataclass
# ---------------------------------------------------------------------------


def test_parse_error_carries_raw_for_user_message():
    text = "ACTION abc defer\n"
    _, errors = _parse_action_file(text)
    assert isinstance(errors[0], ParseError)
    assert errors[0].raw == "ACTION abc defer"
    assert errors[0].line_no == 1


# ---------------------------------------------------------------------------
# _fetch_action_meta
# ---------------------------------------------------------------------------


def _device_history(
    id_entity: int,
    *,
    subtype: str = "gnss_receiver",
    serial: str | None = None,
    model: str | None = None,
):
    """Build a /history/entity/<id>/ response shape for a device."""
    attrs = []
    if serial is not None:
        attrs.append({"code": "serial_number", "value": serial, "date_to": None})
    if model is not None:
        attrs.append({"code": "model", "value": model, "date_to": None})
    return {
        "id_entity": id_entity,
        "code_entity_subtype": subtype,
        "attributes": attrs,
        "children_connections": [],
    }


def test_fetch_action_meta_returns_serial_model_subtype():
    client = MagicMock()
    client.get_entity_history.side_effect = lambda i: _device_history(
        int(i), subtype="gnss_receiver", serial="G2584", model="CMG-ELP"
    )
    meta = _fetch_action_meta(client, [16321])
    assert meta == {
        16321: {
            "subtype": "gnss_receiver",
            "serial": "G2584",
            "model": "CMG-ELP",
        }
    }


def test_fetch_action_meta_handles_missing_entity():
    """A None response from TOS yields None fields, not a raised exception."""
    client = MagicMock()
    client.get_entity_history.return_value = None
    meta = _fetch_action_meta(client, [99999])
    assert meta == {99999: {"subtype": None, "serial": None, "model": None}}


def test_fetch_action_meta_handles_fetch_exception():
    """A raised exception during fetch yields None fields — the apply
    runner still wants to attempt the write, the writer decides."""
    client = MagicMock()
    client.get_entity_history.side_effect = RuntimeError("network down")
    meta = _fetch_action_meta(client, [16321])
    assert meta == {16321: {"subtype": None, "serial": None, "model": None}}


def test_fetch_action_meta_caches_by_id():
    """Duplicate ids in the input cost one HTTP each (not one per repeat)."""
    client = MagicMock()
    client.get_entity_history.side_effect = lambda i: _device_history(
        int(i), serial=f"SN-{i}"
    )
    _fetch_action_meta(client, [16321, 16576, 16321, 16321, 16576])
    # Two unique ids → two fetches, despite five inputs.
    assert client.get_entity_history.call_count == 2


# ---------------------------------------------------------------------------
# decommission verb
# ---------------------------------------------------------------------------


def test_parse_decommission_requires_date():
    text = "ACTION 19712 decommission\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "decommission requires exactly one argument" in errors[0].message


def test_parse_decommission_with_iso_date():
    text = "ACTION 19712 decommission 2025-12-31\n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert actions[0].verb == "decommission"
    assert actions[0].args == ["2025-12-31"]


def _open_join(*, parent: int, connection: int = 999):
    """Build a Join-shaped object for tests (only the fields the dispatcher uses)."""
    from tostools.history import Join

    return Join(
        id_entity_connection=connection,
        id_entity_parent=parent,
        id_entity_child=19712,
        time_from="2000-07-08T00:00:00",
        time_to=None,
    )


def test_dispatch_decommission_closes_join_and_transitions_status():
    """Happy path: open join exists, status virkt → óvirkt."""
    writer = MagicMock()
    writer.transition_attribute_value.return_value = {
        "closed": {"id_attribute_value": 1, "date_to": "2025-12-31"},
        "opened": {"id_attribute_value": 2, "value": "óvirkt"},
    }
    join = _open_join(parent=4, connection=27500)
    result = _dispatch_action(
        writer,
        _make_action(19712, "decommission", "2025-12-31"),
        open_joins_by_device={19712: join},
    )
    assert result.status == "ok"
    writer.patch_entity_connection.assert_called_once_with(27500, time_to="2025-12-31")
    writer.transition_attribute_value.assert_called_once_with(
        19712, code="status", new_value="óvirkt", transition_date="2025-12-31"
    )
    assert "PATCH /join/27500" in result.detail
    assert "óvirkt" in result.detail


def test_dispatch_decommission_skips_close_when_no_open_join():
    """A device already without an open join still gets a status transition."""
    writer = MagicMock()
    writer.transition_attribute_value.return_value = {"closed": None, "opened": {}}
    result = _dispatch_action(
        writer,
        _make_action(19712, "decommission", "2025-12-31"),
        open_joins_by_device={19712: None},
    )
    assert result.status == "ok"
    writer.patch_entity_connection.assert_not_called()
    writer.transition_attribute_value.assert_called_once()
    assert "no open join" in result.detail


def test_dispatch_decommission_failure_on_join_close_doesnt_send_status():
    """If the join close raises, we stop — better to leave an inconsistent
    join-side state than to also mutate the status independently."""
    writer = MagicMock()
    writer.patch_entity_connection.side_effect = RuntimeError("simulated 400")
    result = _dispatch_action(
        writer,
        _make_action(19712, "decommission", "2025-12-31"),
        open_joins_by_device={19712: _open_join(parent=4, connection=27500)},
    )
    assert result.status == "failed"
    assert "simulated 400" in result.detail
    writer.transition_attribute_value.assert_not_called()


def test_dispatch_decommission_status_failure_after_join_close_is_reported():
    """If join closes OK but status fails, surface BOTH outcomes so the
    operator can clean up the half-applied state in TOS."""
    writer = MagicMock()
    writer.transition_attribute_value.side_effect = RuntimeError("status 400")
    result = _dispatch_action(
        writer,
        _make_action(19712, "decommission", "2025-12-31"),
        open_joins_by_device={19712: _open_join(parent=4, connection=27500)},
    )
    assert result.status == "failed"
    assert "PATCH /join/27500" in result.detail  # join did happen
    assert "status 400" in result.detail


def test_dispatch_decommission_no_prior_status_period_still_succeeds():
    """The transition helper handles 'no open status period' by just
    POSTing the óvirkt period — dispatcher should treat that as ok."""
    writer = MagicMock()
    writer.transition_attribute_value.return_value = {
        "closed": None,
        "opened": {"id_attribute_value": 5, "value": "óvirkt"},
    }
    result = _dispatch_action(
        writer,
        _make_action(4926, "decommission", "2025-12-31"),
        open_joins_by_device={4926: _open_join(parent=4, connection=27200)},
    )
    assert result.status == "ok"
    assert "no prior status" in result.detail


# ---------------------------------------------------------------------------
# transition_attribute_value (TOSWriter helper) — quick contract tests
# ---------------------------------------------------------------------------


def test_transition_attribute_value_closes_then_opens():
    """Two HTTP calls: PATCH the open period's date_to, then POST new."""
    from tostools.api.tos_writer import TOSWriter

    w = TOSWriter(dry_run=True, username="u", password="p")
    w._token = "tok"  # type: ignore[attr-defined]
    w._token_exp = 9_999_999_999.0  # type: ignore[attr-defined]

    existing = [
        {
            "id_attribute_value": 100,
            "code": "status",
            "value": "virkt",
            "date_from": "1992-05-28T00:00:00",
            "date_to": None,
        }
    ]
    with (
        patch.object(w, "get_attribute_values", return_value=existing),
        patch.object(w, "patch_attribute_value") as mock_patch,
        patch.object(w, "add_attribute_value") as mock_add,
    ):
        mock_patch.return_value = {"id_attribute_value": 100, "date_to": "2025-12-31"}
        mock_add.return_value = {"id_attribute_value": 200, "value": "óvirkt"}
        result = w.transition_attribute_value(
            19712, code="status", new_value="óvirkt", transition_date="2025-12-31"
        )

    mock_patch.assert_called_once_with(100, date_to="2025-12-31")
    mock_add.assert_called_once_with(
        19712, code="status", value="óvirkt", date_from="2025-12-31"
    )
    assert result["closed"] is not None
    assert result["opened"] is not None


def test_transition_attribute_value_no_prior_just_opens():
    """When no open period exists, just POST — no PATCH attempt."""
    from tostools.api.tos_writer import TOSWriter

    w = TOSWriter(dry_run=True, username="u", password="p")
    w._token = "tok"  # type: ignore[attr-defined]
    w._token_exp = 9_999_999_999.0  # type: ignore[attr-defined]

    with (
        patch.object(w, "get_attribute_values", return_value=[]),
        patch.object(w, "patch_attribute_value") as mock_patch,
        patch.object(w, "add_attribute_value") as mock_add,
    ):
        mock_add.return_value = {"id_attribute_value": 200, "value": "óvirkt"}
        result = w.transition_attribute_value(
            4926, code="status", new_value="óvirkt", transition_date="2025-12-31"
        )

    mock_patch.assert_not_called()
    mock_add.assert_called_once()
    assert result["closed"] is None
    assert result["opened"] is not None


# ---------------------------------------------------------------------------
# move verb — parser
# ---------------------------------------------------------------------------


def test_parse_move_requires_two_args():
    text = "ACTION 19712 move 4523\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "move requires exactly two arguments" in errors[0].message


def test_parse_move_rejects_extra_args():
    text = "ACTION 19712 move 4523 2025-12-31 extra\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "move requires exactly two arguments" in errors[0].message


def test_parse_move_with_two_args():
    text = "ACTION 19712 move 4523 2025-12-31\n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert actions[0].verb == "move"
    assert actions[0].args == ["4523", "2025-12-31"]


# ---------------------------------------------------------------------------
# move verb — dispatcher
# ---------------------------------------------------------------------------


def test_dispatch_move_closes_old_join_and_opens_new():
    """Happy path: open join exists, close + open both succeed."""
    writer = MagicMock()
    writer.patch_entity_connection.return_value = {
        "id_entity_connection": 27500,
        "time_to": "2025-12-31",
    }
    writer.create_entity_connection.return_value = {
        "id_entity_connection": 27999,
        "id_entity_parent": 4523,
        "time_from": "2025-12-31",
    }
    join = _open_join(parent=4, connection=27500)
    result = _dispatch_action(
        writer,
        _make_action(19712, "move", "4523", "2025-12-31"),
        open_joins_by_device={19712: join},
    )
    assert result.status == "ok"
    writer.patch_entity_connection.assert_called_once_with(27500, time_to="2025-12-31")
    writer.create_entity_connection.assert_called_once_with(
        id_parent=4523,
        id_child=19712,
        time_from="2025-12-31",
        time_to=None,
    )
    assert "PATCH /join/27500" in result.detail
    assert "parent=4523" in result.detail


def test_dispatch_move_fails_loudly_without_open_join():
    """Unlike decommission, missing open join is a hard failure — there's
    nothing to close, so the move is ill-defined."""
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(19712, "move", "4523", "2025-12-31"),
        open_joins_by_device={19712: None},
    )
    assert result.status == "failed"
    assert "no open parent join" in result.detail
    writer.patch_entity_connection.assert_not_called()
    writer.create_entity_connection.assert_not_called()


def test_dispatch_move_close_failure_doesnt_open_new():
    """If the close fails, don't POST the new join — we'd duplicate."""
    writer = MagicMock()
    writer.patch_entity_connection.side_effect = RuntimeError("simulated 400")
    result = _dispatch_action(
        writer,
        _make_action(19712, "move", "4523", "2025-12-31"),
        open_joins_by_device={19712: _open_join(parent=4, connection=27500)},
    )
    assert result.status == "failed"
    assert "simulated 400" in result.detail
    writer.create_entity_connection.assert_not_called()


def test_dispatch_move_open_failure_after_close_surfaces_both():
    """If close succeeds but open fails, the device is parent-less in TOS;
    the detail must mention both outcomes so operator can clean up."""
    writer = MagicMock()
    writer.create_entity_connection.side_effect = RuntimeError("open 400")
    result = _dispatch_action(
        writer,
        _make_action(19712, "move", "4523", "2025-12-31"),
        open_joins_by_device={19712: _open_join(parent=4, connection=27500)},
    )
    assert result.status == "failed"
    assert "PATCH /join/27500" in result.detail
    assert "open 400" in result.detail
    assert "parent-less" in result.detail


def test_dispatch_move_rejects_non_int_parent():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(19712, "move", "nope", "2025-12-31"),
        open_joins_by_device={19712: _open_join(parent=4, connection=27500)},
    )
    assert result.status == "failed"
    assert "integer to_parent_id" in result.detail
    writer.patch_entity_connection.assert_not_called()


# ---------------------------------------------------------------------------
# fill-gap verb — parser
# ---------------------------------------------------------------------------


def test_parse_fill_gap_requires_three_args():
    text = "ACTION 19712 fill-gap 4523 2010-01-01\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "fill-gap requires exactly three arguments" in errors[0].message


def test_parse_fill_gap_with_three_args():
    text = "ACTION 19712 fill-gap 4523 2010-01-01 2012-06-30\n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert actions[0].verb == "fill-gap"
    assert actions[0].args == ["4523", "2010-01-01", "2012-06-30"]


# ---------------------------------------------------------------------------
# fill-gap verb — dispatcher
# ---------------------------------------------------------------------------


def test_dispatch_fill_gap_calls_create_entity_connection():
    writer = MagicMock()
    writer.create_entity_connection.return_value = {"id_entity_connection": 27800}
    result = _dispatch_action(
        writer,
        _make_action(19712, "fill-gap", "4523", "2010-01-01", "2012-06-30"),
    )
    assert result.status == "ok"
    writer.create_entity_connection.assert_called_once_with(
        id_parent=4523,
        id_child=19712,
        time_from="2010-01-01",
        time_to="2012-06-30",
    )
    assert "child=19712" in result.detail
    assert "2010-01-01 → 2012-06-30" in result.detail


def test_dispatch_fill_gap_captures_writer_exception():
    writer = MagicMock()
    writer.create_entity_connection.side_effect = RuntimeError("simulated 400")
    result = _dispatch_action(
        writer,
        _make_action(19712, "fill-gap", "4523", "2010-01-01", "2012-06-30"),
    )
    assert result.status == "failed"
    assert "simulated 400" in result.detail


def test_dispatch_fill_gap_rejects_non_int_parent():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(19712, "fill-gap", "nope", "2010-01-01", "2012-06-30"),
    )
    assert result.status == "failed"
    assert "integer parent_id" in result.detail
    writer.create_entity_connection.assert_not_called()


# ---------------------------------------------------------------------------
# _parse_action_file — patch-attribute-date verb (Layer 4)
# ---------------------------------------------------------------------------


def test_parse_action_file_patch_attribute_date_three_args():
    text = "ACTION 4773 patch-attribute-date serial_number 2014-10-17 2002-01-01\n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert len(actions) == 1
    assert actions[0].verb == "patch-attribute-date"
    assert actions[0].args == ["serial_number", "2014-10-17", "2002-01-01"]


def test_parse_action_file_patch_attribute_date_rejects_too_few_args():
    text = "ACTION 4773 patch-attribute-date serial_number 2014-10-17\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert len(errors) == 1
    assert "patch-attribute-date requires exactly three arguments" in (
        errors[0].message
    )


def test_parse_action_file_patch_attribute_date_rejects_extra_args():
    text = (
        "ACTION 4773 patch-attribute-date serial_number "
        "2014-10-17 2002-01-01 EXTRA\n"
    )
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "patch-attribute-date requires exactly three arguments" in (
        errors[0].message
    )


# ---------------------------------------------------------------------------
# _dispatch_action — patch-attribute-date
# ---------------------------------------------------------------------------


def _attr_value(id_attribute_value, code: str, value, date_from: str, date_to=None):
    """One row as returned by writer.get_attribute_values (TOS shape)."""
    return {
        "id_attribute_value": id_attribute_value,
        "code": code,
        "value": value,
        "date_from": date_from,
        "date_to": date_to,
    }


def test_dispatch_patch_attribute_date_happy_path():
    """The dispatcher must (a) call get_attribute_values to find the
    period, (b) match by date-only prefix, (c) PATCH the right id with
    the right new date."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        _attr_value(
            55001, "serial_number", "13831", "2014-10-17 00:00:00"
        ),  # the violation period
        _attr_value(
            55002, "model", "ASHTECH UZ-12", "2002-01-01 00:00:00"
        ),  # unrelated code
    ]
    writer.patch_attribute_value.return_value = {"ok": True}

    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-date",
            "serial_number",
            "2014-10-17",
            "2002-01-01",
        ),
    )

    assert result.status == "ok"
    writer.get_attribute_values.assert_called_once_with(4773, "serial_number")
    writer.patch_attribute_value.assert_called_once_with(55001, date_from="2002-01-01")
    assert "PATCH /attribute_value/55001" in result.detail
    assert "2014-10-17 → 2002-01-01" in result.detail


def test_dispatch_patch_attribute_date_normalises_tos_datetime():
    """TOS stores `2014-10-17 00:00:00` but the violation carries
    `2014-10-17`. Without date-only normalisation the dispatcher would
    silently no-op against live TOS — this test is the regression guard."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        _attr_value(55001, "serial_number", "13831", "2014-10-17 00:00:00"),
    ]
    writer.patch_attribute_value.return_value = {"ok": True}

    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-date",
            "serial_number",
            "2014-10-17",  # date-only — must match the full datetime above
            "2002-01-01",
        ),
    )
    assert result.status == "ok"
    writer.patch_attribute_value.assert_called_once_with(55001, date_from="2002-01-01")


def test_dispatch_patch_attribute_date_zero_matches_fails():
    """No period with the given date_from → failed; never falls through
    to PATCH some other arbitrary period."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        _attr_value(55001, "serial_number", "13831", "2010-01-01 00:00:00"),
    ]
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-date",
            "serial_number",
            "2014-10-17",  # no period matches
            "2002-01-01",
        ),
    )
    assert result.status == "failed"
    assert "no period found" in result.detail
    writer.patch_attribute_value.assert_not_called()


def test_dispatch_patch_attribute_date_ambiguous_match_fails():
    """Two periods with the same date_only date_from → refuse to PATCH.
    Silent corruption (picking 'first') is the failure mode we're
    explicitly guarding against per the design discussion."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        _attr_value(55001, "serial_number", "OLD", "2014-10-17 00:00:00"),
        _attr_value(
            55002, "serial_number", "NEW", "2014-10-17 12:00:00"
        ),  # different time, same date
    ]
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-date",
            "serial_number",
            "2014-10-17",
            "2002-01-01",
        ),
    )
    assert result.status == "failed"
    assert "2 periods match" in result.detail
    assert "ambiguously" in result.detail
    writer.patch_attribute_value.assert_not_called()


def test_dispatch_patch_attribute_date_missing_id_attribute_value_fails():
    """A matching period without ``id_attribute_value`` (partial TOS
    payload) is unrecoverable in this run — the dispatcher must fail
    rather than guess or POST."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        {
            "code": "serial_number",
            "value": "13831",
            "date_from": "2014-10-17 00:00:00",
            "date_to": None,
            # No id_attribute_value
        },
    ]
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-date",
            "serial_number",
            "2014-10-17",
            "2002-01-01",
        ),
    )
    assert result.status == "failed"
    assert "id_attribute_value" in result.detail
    writer.patch_attribute_value.assert_not_called()


def test_dispatch_patch_attribute_date_rejects_bad_new_date_format():
    """``new_date_from`` must look like YYYY-MM-DD. Catch typos at
    dispatch time rather than letting an obviously-malformed value
    travel to TOS."""
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-date",
            "serial_number",
            "2014-10-17",
            "not-a-date",
        ),
    )
    assert result.status == "failed"
    assert "YYYY-MM-DD" in result.detail
    writer.get_attribute_values.assert_not_called()
    writer.patch_attribute_value.assert_not_called()


def test_dispatch_patch_attribute_date_captures_writer_exception():
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        _attr_value(55001, "serial_number", "13831", "2014-10-17 00:00:00"),
    ]
    writer.patch_attribute_value.side_effect = RuntimeError("simulated 500")
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-date",
            "serial_number",
            "2014-10-17",
            "2002-01-01",
        ),
    )
    assert result.status == "failed"
    assert "patch_attribute_value raised" in result.detail
    assert "simulated 500" in result.detail


def test_dispatch_patch_attribute_date_captures_read_exception():
    writer = MagicMock()
    writer.get_attribute_values.side_effect = RuntimeError("simulated 503")
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-date",
            "serial_number",
            "2014-10-17",
            "2002-01-01",
        ),
    )
    assert result.status == "failed"
    assert "get_attribute_values raised" in result.detail
    writer.patch_attribute_value.assert_not_called()


# ---------------------------------------------------------------------------
# _parse_action_file — add-attribute
# ---------------------------------------------------------------------------


def test_parse_action_file_add_attribute_three_args():
    text = "ACTION 4257 add-attribute in_network_epos true 2021-11-01\n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert len(actions) == 1
    assert actions[0].verb == "add-attribute"
    assert actions[0].id_entity == 4257
    assert actions[0].args == ["in_network_epos", "true", "2021-11-01"]


def test_parse_action_file_add_attribute_quoted_value():
    """Values with spaces must be quoted — shlex.split unquotes them
    back to a single token. The `'GPS stöð'` case is the regression
    target: the catalog's `subtype` default carries a space."""
    text = "ACTION 4390 add-attribute subtype 'GPS stöð' 2001-07-19\n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert actions[0].args == ["subtype", "GPS stöð", "2001-07-19"]


def test_parse_action_file_add_attribute_double_quoted_value():
    """Double quotes also work — shlex accepts either form."""
    text = 'ACTION 4390 add-attribute subtype "GPS stöð" 2001-07-19\n'
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert actions[0].args == ["subtype", "GPS stöð", "2001-07-19"]


def test_parse_action_file_add_attribute_rejects_too_few_args():
    text = "ACTION 4257 add-attribute date_start 2010-01-01\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "add-attribute requires exactly three arguments" in errors[0].message


def test_parse_action_file_add_attribute_rejects_extra_args():
    text = "ACTION 4257 add-attribute date_start 2010-01-01 2011-01-01 EXTRA\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "add-attribute requires exactly three arguments" in errors[0].message


def test_parse_action_file_rejects_unbalanced_quoting():
    """shlex raises on unbalanced quotes; the error is captured, not
    propagated, so the rest of the file still parses."""
    text = (
        "ACTION 100 add-attribute marker 'unclosed quote 2020-01-01\n"
        "ACTION 200 defer\n"
    )
    actions, errors = _parse_action_file(text)
    assert len(actions) == 1
    assert actions[0].id_entity == 200
    assert "malformed quoting" in errors[0].message


def test_parse_action_file_backward_compat_with_bare_tokens():
    """shlex.split on lines with no quoting behaves identically to
    str.split — verify existing verbs aren't disturbed."""
    text = (
        "ACTION 16321 change-subtype digitizer\n"
        "ACTION 16321 patch-attribute-date serial_number "
        "2014-10-17 2002-01-01\n"
        "ACTION 16321 defer\n"
    )
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert [a.verb for a in actions] == [
        "change-subtype",
        "patch-attribute-date",
        "defer",
    ]


# ---------------------------------------------------------------------------
# _dispatch_action — add-attribute
# ---------------------------------------------------------------------------


def test_dispatch_add_attribute_happy_path():
    """No existing open period → POST a new attribute value."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = []  # entity has no values yet
    writer.add_attribute_value.return_value = {"id_attribute_value": 99001}

    result = _dispatch_action(
        writer,
        _make_action(4257, "add-attribute", "date_start", "2010-06-15", "2010-06-15"),
    )

    assert result.status == "ok"
    writer.get_attribute_values.assert_called_once_with(4257, "date_start")
    writer.add_attribute_value.assert_called_once_with(
        4257, "date_start", "2010-06-15", "2010-06-15"
    )
    assert "POST /attribute_values id_entity=4257" in result.detail


def test_dispatch_add_attribute_quoted_value_passes_through():
    """A value parsed from `'GPS stöð'` reaches the writer unchanged."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = []
    writer.add_attribute_value.return_value = {"id_attribute_value": 99002}

    result = _dispatch_action(
        writer,
        _make_action(4390, "add-attribute", "subtype", "GPS stöð", "2001-07-19"),
    )

    assert result.status == "ok"
    writer.add_attribute_value.assert_called_once_with(
        4390, "subtype", "GPS stöð", "2001-07-19"
    )


def test_dispatch_add_attribute_same_value_is_no_op():
    """Open period with the same value → idempotent skip; never POSTs.
    Guards against accidental duplicate periods when the apply runs
    twice in a row."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        _attr_value(50001, "date_start", "2010-06-15", "2010-06-15 00:00:00")
    ]

    result = _dispatch_action(
        writer,
        _make_action(4257, "add-attribute", "date_start", "2010-06-15", "2010-06-15"),
    )

    assert result.status == "ok"
    assert "already present" in result.detail
    writer.add_attribute_value.assert_not_called()


def test_dispatch_add_attribute_different_value_refuses():
    """Open period exists with a different value → refuse rather than
    silently overwrite. This is the safety contract the dispatcher
    enforces; the operator should use a transition verb for
    history-preserving updates."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        _attr_value(50001, "subtype", "Annað", "2001-01-01 00:00:00")
    ]

    result = _dispatch_action(
        writer,
        _make_action(4390, "add-attribute", "subtype", "GPS stöð", "2001-07-19"),
    )

    assert result.status == "failed"
    assert "refuse to overwrite" in result.detail
    assert "Annað" in result.detail
    writer.add_attribute_value.assert_not_called()


def test_dispatch_add_attribute_multiple_open_periods_refuses():
    """Two open periods for the same code → data is already corrupt.
    add-attribute refuses to compound the problem."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        _attr_value(50001, "subtype", "X", "2001-01-01 00:00:00"),
        _attr_value(50002, "subtype", "Y", "2005-01-01 00:00:00"),
    ]

    result = _dispatch_action(
        writer,
        _make_action(4390, "add-attribute", "subtype", "GPS stöð", "2001-07-19"),
    )

    assert result.status == "failed"
    assert "2 open periods" in result.detail
    writer.add_attribute_value.assert_not_called()


def test_dispatch_add_attribute_fill_value_placeholder_refuses():
    """A literal <FILL_VALUE> in the action line means the operator
    forgot to fill it in — refuse before any network call."""
    writer = MagicMock()

    result = _dispatch_action(
        writer,
        _make_action(4257, "add-attribute", "note", "<FILL_VALUE>", "2010-01-01"),
    )

    assert result.status == "failed"
    assert "placeholder" in result.detail
    writer.get_attribute_values.assert_not_called()
    writer.add_attribute_value.assert_not_called()


def test_dispatch_add_attribute_fill_date_placeholder_refuses():
    writer = MagicMock()

    result = _dispatch_action(
        writer,
        _make_action(4257, "add-attribute", "date_start", "2010-01-01", "<FILL_DATE>"),
    )

    assert result.status == "failed"
    assert "date placeholder" in result.detail
    writer.add_attribute_value.assert_not_called()


def test_dispatch_add_attribute_invalid_date_format_refuses():
    writer = MagicMock()

    result = _dispatch_action(
        writer,
        _make_action(4257, "add-attribute", "date_start", "2010-01-01", "not-a-date"),
    )

    assert result.status == "failed"
    assert "YYYY-MM-DD" in result.detail
    writer.add_attribute_value.assert_not_called()


def test_dispatch_add_attribute_get_raises_returns_failed():
    """Network/auth errors from get_attribute_values surface as failed
    actions — never raise out of the dispatcher."""
    writer = MagicMock()
    writer.get_attribute_values.side_effect = RuntimeError("simulated 503")

    result = _dispatch_action(
        writer,
        _make_action(4257, "add-attribute", "date_start", "2010-06-15", "2010-06-15"),
    )

    assert result.status == "failed"
    assert "get_attribute_values raised" in result.detail
    writer.add_attribute_value.assert_not_called()


def test_dispatch_add_attribute_writer_raises_returns_failed():
    """Network/auth errors from add_attribute_value surface as failed
    actions — never raise out of the dispatcher."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = []
    writer.add_attribute_value.side_effect = RuntimeError("simulated 500")

    result = _dispatch_action(
        writer,
        _make_action(4257, "add-attribute", "date_start", "2010-06-15", "2010-06-15"),
    )

    assert result.status == "failed"
    assert "add_attribute_value raised" in result.detail


def test_dispatch_add_attribute_skips_closed_periods_for_conflict_check():
    """Closed periods (date_to set) don't count as open conflicts — only
    open periods (date_to is None) gate the new POST."""
    writer = MagicMock()
    # All existing periods are closed → still allowed to add a new open one.
    writer.get_attribute_values.return_value = [
        _attr_value(
            50001,
            "subtype",
            "Old",
            "2001-01-01 00:00:00",
            date_to="2005-12-31 00:00:00",
        ),
        _attr_value(
            50002,
            "subtype",
            "Older",
            "1995-01-01 00:00:00",
            date_to="2000-12-31 00:00:00",
        ),
    ]
    writer.add_attribute_value.return_value = {"id_attribute_value": 99003}

    result = _dispatch_action(
        writer,
        _make_action(4390, "add-attribute", "subtype", "GPS stöð", "2006-01-01"),
    )

    assert result.status == "ok"
    writer.add_attribute_value.assert_called_once_with(
        4390, "subtype", "GPS stöð", "2006-01-01"
    )


# ---------------------------------------------------------------------------
# _parse_action_file — patch-attribute-value verb
# ---------------------------------------------------------------------------


def test_parse_action_file_patch_attribute_value_three_args():
    text = "ACTION 4773 patch-attribute-value serial_number 2006-06-29 3163\n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert len(actions) == 1
    assert actions[0].verb == "patch-attribute-value"
    assert actions[0].args == ["serial_number", "2006-06-29", "3163"]


def test_parse_action_file_patch_attribute_value_quoted_value():
    """Values with spaces must be quoted — same shlex behaviour as add-attribute."""
    text = "ACTION 4258 patch-attribute-value name 2006-06-29 'Hedinshofdi, IS'\n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert actions[0].args == ["name", "2006-06-29", "Hedinshofdi, IS"]


def test_parse_action_file_patch_attribute_value_rejects_too_few_args():
    text = "ACTION 4773 patch-attribute-value serial_number 2006-06-29\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "patch-attribute-value requires exactly three arguments" in (
        errors[0].message
    )


def test_parse_action_file_patch_attribute_value_rejects_extra_args():
    text = "ACTION 4773 patch-attribute-value serial_number 2006-06-29 3163 EXTRA\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "patch-attribute-value requires exactly three arguments" in (
        errors[0].message
    )


# ---------------------------------------------------------------------------
# _dispatch_action — patch-attribute-value
# ---------------------------------------------------------------------------


def test_dispatch_patch_attribute_value_happy_path():
    """The dispatcher must (a) look up the period via get_attribute_values,
    (b) match by date-only prefix, (c) PATCH the right id with the new value."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        _attr_value(60001, "serial_number", "UNKNOWN", "2006-06-29 00:00:00"),
        _attr_value(60002, "model", "ASHTECH UZ-12", "2002-01-01 00:00:00"),
    ]
    writer.patch_attribute_value.return_value = {"ok": True}

    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-value",
            "serial_number",
            "2006-06-29",
            "3163",
        ),
    )

    assert result.status == "ok"
    writer.get_attribute_values.assert_called_once_with(4773, "serial_number")
    writer.patch_attribute_value.assert_called_once_with(60001, value="3163")
    assert "PATCH /attribute_value/60001" in result.detail
    assert "'UNKNOWN' → '3163'" in result.detail


def test_dispatch_patch_attribute_value_normalises_tos_datetime():
    """TOS stores `2006-06-29 00:00:00` but the triage carries `2006-06-29`.
    The date-prefix match must succeed — regression guard mirroring the
    patch-attribute-date test of the same name."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        _attr_value(60001, "serial_number", "UNKNOWN", "2006-06-29 00:00:00"),
    ]
    writer.patch_attribute_value.return_value = {"ok": True}

    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-value",
            "serial_number",
            "2006-06-29",
            "3163",
        ),
    )
    assert result.status == "ok"
    writer.patch_attribute_value.assert_called_once_with(60001, value="3163")


def test_dispatch_patch_attribute_value_idempotent_no_op():
    """If the matched period already holds the requested value, return ok
    and skip the PATCH — same shape as add-attribute's no-op path."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        _attr_value(60001, "serial_number", "3163", "2006-06-29 00:00:00"),
    ]
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-value",
            "serial_number",
            "2006-06-29",
            "3163",
        ),
    )
    assert result.status == "ok"
    assert "already present" in result.detail
    writer.patch_attribute_value.assert_not_called()


def test_dispatch_patch_attribute_value_zero_matches_fails():
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        _attr_value(60001, "serial_number", "UNKNOWN", "2010-01-01 00:00:00"),
    ]
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-value",
            "serial_number",
            "2006-06-29",
            "3163",
        ),
    )
    assert result.status == "failed"
    assert "no period found" in result.detail
    writer.patch_attribute_value.assert_not_called()


def test_dispatch_patch_attribute_value_ambiguous_match_fails():
    """Two periods with the same date_only date_from → refuse to PATCH,
    same silent-corruption guard as patch-attribute-date."""
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        _attr_value(60001, "serial_number", "OLD", "2006-06-29 00:00:00"),
        _attr_value(60002, "serial_number", "OTHER", "2006-06-29 12:00:00"),
    ]
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-value",
            "serial_number",
            "2006-06-29",
            "3163",
        ),
    )
    assert result.status == "failed"
    assert "2 periods match" in result.detail
    assert "ambiguously" in result.detail
    writer.patch_attribute_value.assert_not_called()


def test_dispatch_patch_attribute_value_missing_id_attribute_value_fails():
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        {
            "code": "serial_number",
            "value": "UNKNOWN",
            "date_from": "2006-06-29 00:00:00",
            "date_to": None,
            # No id_attribute_value
        },
    ]
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-value",
            "serial_number",
            "2006-06-29",
            "3163",
        ),
    )
    assert result.status == "failed"
    assert "id_attribute_value" in result.detail
    writer.patch_attribute_value.assert_not_called()


def test_dispatch_patch_attribute_value_rejects_bad_date_format():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-value",
            "serial_number",
            "not-a-date",
            "3163",
        ),
    )
    assert result.status == "failed"
    assert "YYYY-MM-DD" in result.detail
    writer.get_attribute_values.assert_not_called()


def test_dispatch_patch_attribute_value_fill_value_placeholder_refuses():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-value",
            "serial_number",
            "2006-06-29",
            "<FILL_VALUE>",
        ),
    )
    assert result.status == "failed"
    assert "<FILL_VALUE>" in result.detail
    writer.get_attribute_values.assert_not_called()


def test_dispatch_patch_attribute_value_fill_date_placeholder_refuses():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-value",
            "serial_number",
            "<FILL_DATE>",
            "3163",
        ),
    )
    assert result.status == "failed"
    assert "<FILL_DATE>" in result.detail
    writer.get_attribute_values.assert_not_called()


def test_dispatch_patch_attribute_value_captures_writer_exception():
    writer = MagicMock()
    writer.get_attribute_values.return_value = [
        _attr_value(60001, "serial_number", "UNKNOWN", "2006-06-29 00:00:00"),
    ]
    writer.patch_attribute_value.side_effect = RuntimeError("simulated 500")
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-value",
            "serial_number",
            "2006-06-29",
            "3163",
        ),
    )
    assert result.status == "failed"
    assert "patch_attribute_value raised" in result.detail
    assert "simulated 500" in result.detail


def test_dispatch_patch_attribute_value_captures_read_exception():
    writer = MagicMock()
    writer.get_attribute_values.side_effect = RuntimeError("simulated 503")
    result = _dispatch_action(
        writer,
        _make_action(
            4773,
            "patch-attribute-value",
            "serial_number",
            "2006-06-29",
            "3163",
        ),
    )
    assert result.status == "failed"
    assert "get_attribute_values raised" in result.detail
    writer.patch_attribute_value.assert_not_called()


# ---------------------------------------------------------------------------
# _parse_action_file — patch-join-date verb
# ---------------------------------------------------------------------------


def test_parse_action_file_patch_join_date_three_args():
    text = "ACTION 17234 patch-join-date 28104 time_from 2012-06-27\n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert len(actions) == 1
    assert actions[0].verb == "patch-join-date"
    assert actions[0].args == ["28104", "time_from", "2012-06-27"]


def test_parse_action_file_patch_join_date_rejects_too_few_args():
    text = "ACTION 17234 patch-join-date 28104 time_from\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "patch-join-date requires exactly three arguments" in errors[0].message


def test_parse_action_file_patch_join_date_rejects_extra_args():
    text = "ACTION 17234 patch-join-date 28104 time_from 2012-06-27 EXTRA\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "patch-join-date requires exactly three arguments" in errors[0].message


# ---------------------------------------------------------------------------
# _dispatch_action — patch-join-date
# ---------------------------------------------------------------------------


def test_dispatch_patch_join_date_extend_time_from():
    writer = MagicMock()
    writer.patch_entity_connection.return_value = {"ok": True}

    result = _dispatch_action(
        writer,
        _make_action(17234, "patch-join-date", "28104", "time_from", "2012-06-27"),
    )

    assert result.status == "ok"
    writer.patch_entity_connection.assert_called_once_with(
        28104, time_from="2012-06-27"
    )
    assert "PATCH /join/28104" in result.detail
    assert "time_from=2012-06-27" in result.detail


def test_dispatch_patch_join_date_close_time_to():
    writer = MagicMock()
    writer.patch_entity_connection.return_value = {"ok": True}

    result = _dispatch_action(
        writer,
        _make_action(17234, "patch-join-date", "28104", "time_to", "2014-10-17"),
    )

    assert result.status == "ok"
    writer.patch_entity_connection.assert_called_once_with(28104, time_to="2014-10-17")


def test_dispatch_patch_join_date_rejects_non_int_connection():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(17234, "patch-join-date", "nope", "time_from", "2012-06-27"),
    )
    assert result.status == "failed"
    assert "integer id_connection" in result.detail
    writer.patch_entity_connection.assert_not_called()


def test_dispatch_patch_join_date_rejects_field_outside_whitelist():
    """The writer would happily PATCH id_entity_parent — but that's the
    semantics of `move`. Block it here so this verb can't backdoor a reparent."""
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(17234, "patch-join-date", "28104", "id_entity_parent", "9999"),
    )
    assert result.status == "failed"
    assert "field must be one of" in result.detail
    assert "move verb to reparent" in result.detail
    writer.patch_entity_connection.assert_not_called()


def test_dispatch_patch_join_date_rejects_bad_date_format():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(17234, "patch-join-date", "28104", "time_from", "not-a-date"),
    )
    assert result.status == "failed"
    assert "YYYY-MM-DD" in result.detail
    writer.patch_entity_connection.assert_not_called()


def test_dispatch_patch_join_date_rejects_placeholder():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(17234, "patch-join-date", "28104", "time_from", "<FILL_DATE>"),
    )
    assert result.status == "failed"
    assert "<FILL_DATE>" in result.detail
    writer.patch_entity_connection.assert_not_called()


def test_dispatch_patch_join_date_captures_writer_exception():
    writer = MagicMock()
    writer.patch_entity_connection.side_effect = RuntimeError("simulated 500")
    result = _dispatch_action(
        writer,
        _make_action(17234, "patch-join-date", "28104", "time_from", "2012-06-27"),
    )
    assert result.status == "failed"
    assert "patch_entity_connection raised" in result.detail
    assert "simulated 500" in result.detail


# ---------------------------------------------------------------------------
# HEDI fixture — the todo #22 scenario end-to-end through the dispatcher
# ---------------------------------------------------------------------------


def test_dispatch_hedi_scenario_all_three_correction_verbs():
    """Cover HEDI todo #22 (2006-180 PolaRX2 wrong serial, missing antenna
    serial, missing 2012-179 → 2014-290 NETR9 join window) by dispatching
    one of each new verb against fixture data shaped like the live TOS
    response. VPN-unreachable substitute for the live dry-run."""
    writer = MagicMock()

    # (1) patch-attribute-value — fix PolaRX2 serial UNKNOWN → 3163
    writer.get_attribute_values.return_value = [
        _attr_value(70001, "serial_number", "UNKNOWN", "2006-06-29 00:00:00"),
    ]
    writer.patch_attribute_value.return_value = {"ok": True}
    result = _dispatch_action(
        writer,
        _make_action(
            16234,  # PolaRX2 device id (fixture)
            "patch-attribute-value",
            "serial_number",
            "2006-06-29",
            "3163",
        ),
    )
    assert result.status == "ok"
    writer.patch_attribute_value.assert_called_once_with(70001, value="3163")
    writer.reset_mock()

    # (2) add-attribute — fill missing antenna serial 5924 (existing verb,
    # no-open-period path)
    writer.get_attribute_values.return_value = []
    writer.add_attribute_value.return_value = {"id_attribute_value": 70010}
    result = _dispatch_action(
        writer,
        _make_action(
            16235,  # antenna device id (fixture)
            "add-attribute",
            "serial_number",
            "5924",
            "2006-06-29",
        ),
    )
    assert result.status == "ok"
    writer.add_attribute_value.assert_called_once_with(
        16235, "serial_number", "5924", "2006-06-29"
    )
    writer.reset_mock()

    # (3) patch-join-date — extend NETR9 join time_from back to 2012-179
    writer.patch_entity_connection.return_value = {"ok": True}
    result = _dispatch_action(
        writer,
        _make_action(
            17234,  # NETR9 device id (fixture)
            "patch-join-date",
            "28104",  # the join id currently opening at 2014-290
            "time_from",
            "2012-06-27",  # day 179 of 2012
        ),
    )
    assert result.status == "ok"
    writer.patch_entity_connection.assert_called_once_with(
        28104, time_from="2012-06-27"
    )


# ---------------------------------------------------------------------------
# Known limitation — chaining two `move`s on the same device in one apply
# run uses a stale open-joins cache. Pins observed behavior so the eventual
# dispatcher fix can flip the assertion. Tracking memory:
# project_dispatch_move_stale_cache.md
# ---------------------------------------------------------------------------


def test_dispatch_two_moves_same_device_uses_stale_cache():
    """`_build_open_joins_lookup` is called once at `_apply_main` startup,
    not per action. After the first move closes a device's open join and
    opens a new one, the cached lookup still points at the now-closed
    join. A second move on the same device therefore re-closes (PATCHes)
    the already-closed join with a new time_to instead of operating on
    the new SAVI join.

    This test does not assert correct behavior — it documents the bug.
    When `_dispatch_move` is fixed to refresh the cache after a
    successful open, flip the assertion: the second close should target
    the SAVI join, not the warehouse join.
    """
    writer = MagicMock()
    writer.create_entity_connection.return_value = {"id_entity_connection": 99999}
    writer.patch_entity_connection.return_value = {"ok": True}

    warehouse_join = _open_join(parent=4, connection=50001)
    joins_cache = {99001: warehouse_join}

    # First move: warehouse → SAVI (parent=4440).
    r1 = _dispatch_action(
        writer,
        _make_action(99001, "move", "4440", "2007-08-08"),
        open_joins_by_device=joins_cache,
    )
    # Second move on the same device: SAVI → warehouse (parent=4) at session end.
    r2 = _dispatch_action(
        writer,
        _make_action(99001, "move", "4", "2007-09-07"),
        open_joins_by_device=joins_cache,
    )

    assert r1.status == "ok"
    assert r2.status == "ok"

    # Both moves close `id_connection=50001` — that's the bug. The second
    # close should have targeted the newly-opened SAVI join. Until the
    # dispatcher refreshes joins_cache after a successful move, this is
    # the observed (incorrect) behavior.
    close_calls = [
        c for c in writer.patch_entity_connection.call_args_list if c.args == (50001,)
    ]
    assert len(close_calls) == 2  # XXX: should be 1 once the bug is fixed


# ---------------------------------------------------------------------------
# create-join verb — parser
# ---------------------------------------------------------------------------


def test_parse_create_join_open_form_two_args():
    """Open-join form: ACTION <id> create-join <parent_id> <date_from>.
    5 tokens total."""
    text = "ACTION 21510 create-join 4 2016-07-02\n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert len(actions) == 1
    assert actions[0].verb == "create-join"
    assert actions[0].args == ["4", "2016-07-02"]


def test_parse_create_join_closed_form_three_args():
    """Closed-historical form: ACTION <id> create-join <parent_id>
    <date_from> <date_to>. Functionally equivalent to fill-gap."""
    text = "ACTION 21510 create-join 4440 2007-09-07 2016-07-02\n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert actions[0].args == ["4440", "2007-09-07", "2016-07-02"]


def test_parse_create_join_rejects_too_few_args():
    text = "ACTION 21510 create-join 4\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "create-join requires 2 or 3 arguments" in errors[0].message


def test_parse_create_join_rejects_too_many_args():
    text = "ACTION 21510 create-join 4 2016-07-02 2017-01-01 EXTRA\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "create-join requires 2 or 3 arguments" in errors[0].message


# ---------------------------------------------------------------------------
# create-join verb — dispatcher
# ---------------------------------------------------------------------------


def test_dispatch_create_join_open_calls_create_entity_connection_with_none_time_to():
    """Open-join form: time_to=None passed to the writer."""
    writer = MagicMock()
    writer.create_entity_connection.return_value = {"id_entity_connection": 99999}
    result = _dispatch_action(
        writer,
        _make_action(21510, "create-join", "4", "2016-07-02"),
    )
    assert result.status == "ok"
    writer.create_entity_connection.assert_called_once_with(
        id_parent=4, id_child=21510, time_from="2016-07-02", time_to=None
    )
    assert "open" in result.detail
    assert "parent=4" in result.detail
    assert "child=21510" in result.detail


def test_dispatch_create_join_closed_passes_time_to():
    """Closed-historical form: time_to=<date> passed to the writer."""
    writer = MagicMock()
    writer.create_entity_connection.return_value = {"id_entity_connection": 99999}
    result = _dispatch_action(
        writer,
        _make_action(21510, "create-join", "4440", "2007-09-07", "2016-07-02"),
    )
    assert result.status == "ok"
    writer.create_entity_connection.assert_called_once_with(
        id_parent=4440,
        id_child=21510,
        time_from="2007-09-07",
        time_to="2016-07-02",
    )
    assert "2007-09-07 → 2016-07-02" in result.detail


def test_dispatch_create_join_rejects_non_int_parent():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(21510, "create-join", "nope", "2016-07-02"),
    )
    assert result.status == "failed"
    assert "integer parent_id" in result.detail
    writer.create_entity_connection.assert_not_called()


def test_dispatch_create_join_captures_writer_exception():
    """A writer-level failure becomes a `status='failed'` result, not a
    raised exception — so the apply runner continues with later actions."""
    writer = MagicMock()
    writer.create_entity_connection.side_effect = RuntimeError("simulated 400")
    result = _dispatch_action(
        writer,
        _make_action(21510, "create-join", "4", "2016-07-02"),
    )
    assert result.status == "failed"
    assert "create_entity_connection raised" in result.detail
    assert "simulated 400" in result.detail


# ---------------------------------------------------------------------------
# delete-join — parsing + dispatch
# ---------------------------------------------------------------------------
#
# Destructive admin verb. Erases a join row entirely (DELETE
# /admin_entity_connection_row/<id>). Intended for SOPAC-convention
# split-monument workarounds and zero-duration orphan cleanup — NOT
# the default close-out workflow (use `decommission` / `move` for that).


def test_parse_action_file_delete_join_one_arg():
    text = "ACTION 5244 delete-join 6429\n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert len(actions) == 1
    assert actions[0].verb == "delete-join"
    assert actions[0].args == ["6429"]


def test_parse_action_file_delete_join_rejects_no_args():
    text = "ACTION 5244 delete-join\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "delete-join requires exactly one argument" in errors[0].message


def test_parse_action_file_delete_join_rejects_extra_args():
    text = "ACTION 5244 delete-join 6429 EXTRA\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "delete-join requires exactly one argument" in errors[0].message


def test_dispatch_delete_join_calls_writer():
    """Successful path: writer.delete_entity_connection is called with the
    parsed id_connection and result.status == 'ok'."""
    writer = MagicMock()
    writer.delete_entity_connection.return_value = {"deleted": True}

    result = _dispatch_action(
        writer,
        _make_action(5244, "delete-join", "6429"),
    )

    assert result.status == "ok"
    writer.delete_entity_connection.assert_called_once_with(6429)
    assert "DELETE /join/6429" in result.detail
    assert "device=5244" in result.detail


def test_dispatch_delete_join_rejects_non_int_connection():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(5244, "delete-join", "nope"),
    )
    assert result.status == "failed"
    assert "integer id_connection" in result.detail
    writer.delete_entity_connection.assert_not_called()


def test_dispatch_delete_join_captures_writer_exception():
    """A writer-level failure (e.g. 403 from non-admin token, or 404 if
    the row was already deleted) becomes status='failed' rather than
    propagating — so the apply runner moves on to the next action."""
    writer = MagicMock()
    writer.delete_entity_connection.side_effect = RuntimeError(
        "simulated 403 Forbidden"
    )
    result = _dispatch_action(
        writer,
        _make_action(5244, "delete-join", "6429"),
    )
    assert result.status == "failed"
    assert "delete_entity_connection raised" in result.detail
    assert "simulated 403" in result.detail


# ---------------------------------------------------------------------------
# delete-attribute-value — parsing + dispatch
# ---------------------------------------------------------------------------
#
# Sibling of delete-join. Destructive admin verb that removes an
# attribute_value row entirely. Intended use cases:
#   * wrong-scope id_attribute FKs (the _resolve_id_attribute bug fixed
#     2026-05-25 sent some monument attributes to station-scoped
#     schema rows; cleaning requires DELETE + re-write)
#   * duplicate values from idempotency mistakes
#   * orphan rows from historical bugs


def test_parse_action_file_delete_attribute_value_one_arg():
    text = "ACTION 5245 delete-attribute-value 152926\n"
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert len(actions) == 1
    assert actions[0].verb == "delete-attribute-value"
    assert actions[0].args == ["152926"]


def test_parse_action_file_delete_attribute_value_rejects_no_args():
    text = "ACTION 5245 delete-attribute-value\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "delete-attribute-value requires exactly one argument" in errors[0].message


def test_parse_action_file_delete_attribute_value_rejects_extra_args():
    text = "ACTION 5245 delete-attribute-value 152926 EXTRA\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert "delete-attribute-value requires exactly one argument" in errors[0].message


def test_dispatch_delete_attribute_value_calls_writer():
    """Successful path: writer.delete_attribute_value is called with the
    parsed id_attribute_value and result.status == 'ok'."""
    writer = MagicMock()
    writer.delete_attribute_value.return_value = {"deleted": True}

    result = _dispatch_action(
        writer,
        _make_action(5245, "delete-attribute-value", "152926"),
    )

    assert result.status == "ok"
    writer.delete_attribute_value.assert_called_once_with(152926)
    assert "DELETE /attribute_value/152926" in result.detail
    assert "device=5245" in result.detail


def test_dispatch_delete_attribute_value_rejects_non_int_id():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(5245, "delete-attribute-value", "nope"),
    )
    assert result.status == "failed"
    assert "integer id_attribute_value" in result.detail
    writer.delete_attribute_value.assert_not_called()


def test_dispatch_delete_attribute_value_captures_writer_exception():
    """A writer-level failure (e.g. 403 non-admin, 404 already-deleted)
    becomes status='failed' rather than propagating — apply runner
    moves on to the next action."""
    writer = MagicMock()
    writer.delete_attribute_value.side_effect = RuntimeError("simulated 403 Forbidden")
    result = _dispatch_action(
        writer,
        _make_action(5245, "delete-attribute-value", "152926"),
    )
    assert result.status == "failed"
    assert "delete_attribute_value raised" in result.detail
    assert "simulated 403" in result.detail


# ---------------------------------------------------------------------------
# _resolve_date_token — `now` and `start` symbolic dates
# ---------------------------------------------------------------------------
#
# `now` = today UTC. `start` = entity's earliest_known anchor (earliest
# non-2014-10-17 open attribute date_from, fallback to open parent join
# time_from). Anything else passes through unchanged. See memory
# project_layer6_followup_date_shortcuts.


def test_resolve_date_token_passes_through_non_tokens():
    """Bare YYYY-MM-DD dates and unknown strings are returned as-is."""
    writer = MagicMock()
    assert _resolve_date_token("2007-09-07", 5245, writer) == ("2007-09-07", None)
    assert _resolve_date_token("anything-else", 5245, writer) == (
        "anything-else",
        None,
    )
    writer._get_earliest_known.assert_not_called()


def test_resolve_date_token_now_returns_today_utc():
    """`now` resolves to today's YYYY-MM-DD in UTC."""
    import re

    writer = MagicMock()
    resolved, err = _resolve_date_token("now", 5245, writer)
    assert err is None
    assert resolved is not None
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolved)
    writer._get_earliest_known.assert_not_called()


def test_resolve_date_token_start_calls_writer_get_earliest_known():
    """`start` delegates to writer._get_earliest_known(id_entity)."""
    writer = MagicMock()
    writer._get_earliest_known.return_value = "2006-06-29"
    resolved, err = _resolve_date_token("start", 5106, writer)
    assert resolved == "2006-06-29"
    assert err is None
    writer._get_earliest_known.assert_called_once_with(5106)


def test_resolve_date_token_start_errors_when_writer_returns_none():
    """If the writer can't compute earliest_known, surface a clear
    error so the apply dispatcher refuses rather than POSTing None."""
    writer = MagicMock()
    writer._get_earliest_known.return_value = None
    resolved, err = _resolve_date_token("start", 99999, writer)
    assert resolved is None
    assert err is not None
    assert "start" in err
    assert "id_entity=99999" in err


def test_resolve_date_token_start_errors_when_writer_raises():
    """Writer-side exceptions become a failed status — never propagate."""
    writer = MagicMock()
    writer._get_earliest_known.side_effect = RuntimeError("simulated 500")
    resolved, err = _resolve_date_token("start", 5245, writer)
    assert resolved is None
    assert err is not None
    assert "start" in err
    assert "simulated 500" in err


# ---------------------------------------------------------------------------
# Token resolution wired into dispatchers — end-to-end smoke tests
# ---------------------------------------------------------------------------


def test_dispatch_add_attribute_resolves_start_to_earliest_known():
    """add-attribute should resolve `start` against
    writer._get_earliest_known before posting."""
    writer = MagicMock()
    writer._get_earliest_known.return_value = "2006-06-29"
    writer.get_attribute_values.return_value = []  # no existing periods
    writer.add_attribute_value.return_value = {"id_attribute_value": 9999}

    result = _dispatch_action(
        writer,
        _make_action(4316, "add-attribute", "visit_class", "B", "start"),
    )

    assert result.status == "ok", result.detail
    writer._get_earliest_known.assert_called_once_with(4316)
    # Dispatcher uses positional args:
    # writer.add_attribute_value(id_entity, code, value, date_from)
    writer.add_attribute_value.assert_called_once_with(
        4316, "visit_class", "B", "2006-06-29"
    )


def test_dispatch_add_attribute_now_resolves_to_today():
    """add-attribute with `now` resolves to today YYYY-MM-DD."""
    import re

    writer = MagicMock()
    writer.get_attribute_values.return_value = []
    writer.add_attribute_value.return_value = {"id_attribute_value": 9999}

    result = _dispatch_action(
        writer,
        _make_action(4316, "add-attribute", "in_network_epos", "nei", "now"),
    )
    assert result.status == "ok", result.detail
    # 4th positional arg is date_from.
    call_args = writer.add_attribute_value.call_args.args
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", call_args[3])


def test_dispatch_patch_join_date_resolves_start():
    """patch-join-date also accepts `start` for new_date."""
    writer = MagicMock()
    writer._get_earliest_known.return_value = "2006-06-29"
    writer.patch_entity_connection.return_value = {"ok": True}

    result = _dispatch_action(
        writer,
        _make_action(5107, "patch-join-date", "6264", "time_from", "start"),
    )

    assert result.status == "ok", result.detail
    writer._get_earliest_known.assert_called_once_with(5107)
    writer.patch_entity_connection.assert_called_once_with(6264, time_from="2006-06-29")


def test_dispatch_add_attribute_fails_when_start_unresolvable():
    """If `start` can't be resolved (entity has no anchor), the
    dispatcher returns failed without calling the POST."""
    writer = MagicMock()
    writer._get_earliest_known.return_value = None

    result = _dispatch_action(
        writer,
        _make_action(99999, "add-attribute", "visit_class", "B", "start"),
    )

    assert result.status == "failed"
    assert "start" in result.detail
    writer.add_attribute_value.assert_not_called()


# ---------------------------------------------------------------------------
# _dispatch_add_visit — Phase C lifecycle-tracker ACTION verb
# ---------------------------------------------------------------------------


def test_parse_action_file_keeps_csv_reasons_as_one_token():
    """shlex.split must NOT split on commas — `repairs,change` is one
    token, the dispatcher splits internally. Pinning this so a future
    parser refactor can't silently break add-visit's reason CSV."""
    text = 'ACTION 4316 add-visit repairs,change 2026-05-30 "sent for repair"\n'
    actions, errors = _parse_action_file(text)
    assert errors == []
    assert len(actions) == 1
    a = actions[0]
    assert a.verb == "add-visit"
    assert a.args == ["repairs,change", "2026-05-30", "sent for repair"]


def test_dispatch_add_visit_happy_path_closed_default():
    """Default 3-arg form: closed (completed=True), single reason."""
    writer = MagicMock()
    writer.add_maintenance_visit.return_value = {"id_maintenance": 5500}
    result = _dispatch_action(
        writer,
        _make_action(4316, "add-visit", "repairs", "2026-05-30", "sent for repair"),
    )
    assert result.status == "ok"
    writer.add_maintenance_visit.assert_called_once()
    kwargs = writer.add_maintenance_visit.call_args.kwargs
    assert writer.add_maintenance_visit.call_args.args == (4316,)
    assert kwargs["reasons"] == ["repairs"]
    assert kwargs["start_time"] == "2026-05-30"
    assert kwargs["end_time"] is None  # writer defaults to start
    assert kwargs["work"] == "sent for repair"
    assert kwargs["completed"] is True
    assert kwargs["maintenance_type"] == "on_site"
    assert kwargs["participants"] == ""
    assert "id_maintenance=5500" in result.detail
    assert "completed" in result.detail


def test_dispatch_add_visit_open_positional_marks_open():
    """4th positional `open` → completed=False (long-running repair start)."""
    writer = MagicMock()
    writer.add_maintenance_visit.return_value = {"id_maintenance": 5501}
    result = _dispatch_action(
        writer,
        _make_action(
            4316, "add-visit", "repairs", "2026-05-30", "sent for repair", "open"
        ),
    )
    assert result.status == "ok"
    assert writer.add_maintenance_visit.call_args.kwargs["completed"] is False
    assert " open" in result.detail


def test_dispatch_add_visit_csv_reasons_split_and_validated():
    """Comma-separated reasons split into a list; each validated."""
    writer = MagicMock()
    writer.add_maintenance_visit.return_value = {"id_maintenance": 5502}
    result = _dispatch_action(
        writer,
        _make_action(
            4316, "add-visit", "repairs,change", "2026-06-15", "back from vendor"
        ),
    )
    assert result.status == "ok"
    assert writer.add_maintenance_visit.call_args.kwargs["reasons"] == [
        "repairs",
        "change",
    ]


def test_dispatch_add_visit_unknown_reason_fails_without_writer_call():
    """Unknown reason → 'failed' (runner continues with remaining
    actions). Cheaper than a writer ValueError + stack trace."""
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(4316, "add-visit", "fixme", "2026-05-30", "bad reason"),
    )
    assert result.status == "failed"
    assert "unknown reason code" in result.detail
    assert "fixme" in result.detail
    writer.add_maintenance_visit.assert_not_called()


def test_dispatch_add_visit_rejects_fill_placeholders():
    """<FILL_*> placeholders in reasons/date/work are operator
    forgot-to-fill errors — refuse rather than POST literal templates."""
    writer = MagicMock()
    for pos, args in enumerate(
        [
            ["<FILL_REASON>", "2026-05-30", "work"],
            ["repairs", "<FILL_DATE>", "work"],
            ["repairs", "2026-05-30", "<FILL_WORK>"],
        ]
    ):
        result = _dispatch_action(writer, _make_action(4316, "add-visit", *args))
        assert result.status == "failed", f"variant {pos}"
        assert "placeholder" in result.detail, f"variant {pos}"
        assert "not replaced" in result.detail, f"variant {pos}"
    writer.add_maintenance_visit.assert_not_called()


def test_dispatch_add_visit_now_token_resolved_via_resolver():
    """`now` resolves to today's UTC date — same path as add-attribute."""
    writer = MagicMock()
    writer.add_maintenance_visit.return_value = {"id_maintenance": 5503}
    result = _dispatch_action(
        writer,
        _make_action(4316, "add-visit", "inspection", "now", "post-check"),
    )
    assert result.status == "ok"
    resolved_date = writer.add_maintenance_visit.call_args.kwargs["start_time"]
    # Today's UTC date in YYYY-MM-DD.
    import datetime as _dt

    expected = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    assert resolved_date == expected


def test_dispatch_add_visit_start_token_uses_earliest_known():
    """`start` resolves via writer._get_earliest_known — same as
    other date-token-aware verbs."""
    writer = MagicMock()
    writer._get_earliest_known.return_value = "2006-06-29"
    writer.add_maintenance_visit.return_value = {"id_maintenance": 5504}
    result = _dispatch_action(
        writer,
        _make_action(4316, "add-visit", "change", "start", "founded"),
    )
    assert result.status == "ok"
    assert writer.add_maintenance_visit.call_args.kwargs["start_time"] == "2006-06-29"


def test_dispatch_add_visit_bad_status_positional_rejected():
    """4th positional must be 'open' or 'closed' — anything else fails
    before the writer call."""
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(4316, "add-visit", "repairs", "2026-05-30", "work", "maybe"),
    )
    assert result.status == "failed"
    assert "open" in result.detail and "closed" in result.detail
    writer.add_maintenance_visit.assert_not_called()


def test_dispatch_add_visit_wrong_arg_count_rejected():
    """Too few / too many positional args → failed with clear message."""
    writer = MagicMock()
    # Too few — just 2.
    result_few = _dispatch_action(
        writer,
        _make_action(4316, "add-visit", "repairs", "2026-05-30"),
    )
    assert result_few.status == "failed"
    assert "expected 3 or 4 positional args" in result_few.detail
    # Too many — 5.
    result_many = _dispatch_action(
        writer,
        _make_action(4316, "add-visit", "repairs", "2026-05-30", "w", "open", "extra"),
    )
    assert result_many.status == "failed"
    assert "expected 3 or 4 positional args" in result_many.detail
    writer.add_maintenance_visit.assert_not_called()


def test_dispatch_add_visit_writer_exception_captured_as_failed():
    """A writer crash (e.g. network failure) becomes a 'failed' result,
    not a raised exception — runner continues."""
    writer = MagicMock()
    writer.add_maintenance_visit.side_effect = RuntimeError("simulated 500")
    result = _dispatch_action(
        writer,
        _make_action(4316, "add-visit", "repairs", "2026-05-30", "work"),
    )
    assert result.status == "failed"
    assert "simulated 500" in result.detail


def test_dispatch_add_visit_bad_date_format_rejected_after_token_resolution():
    """Garbage date that doesn't resolve to a token AND isn't
    YYYY-MM-DD → failed."""
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(4316, "add-visit", "repairs", "yesterday", "work"),
    )
    assert result.status == "failed"
    assert "YYYY-MM-DD" in result.detail
    writer.add_maintenance_visit.assert_not_called()


def test_dispatch_add_visit_empty_csv_reasons_rejected():
    """An empty/whitespace-only reasons_csv would otherwise sneak past
    the per-code validation — pin the explicit refusal."""
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(4316, "add-visit", ",", "2026-05-30", "work"),
    )
    assert result.status == "failed"
    assert "at least one code" in result.detail
    writer.add_maintenance_visit.assert_not_called()


# ---------------------------------------------------------------------------
# _dispatch contact-relationship verbs (patch / assign / delete)
# ---------------------------------------------------------------------------


def test_dispatch_patch_contact_relationship_happy_path():
    writer = MagicMock()
    writer.patch_contact_relationship.return_value = {"ok": True}
    result = _dispatch_action(
        writer,
        _make_action(
            4316, "patch-contact-relationship", "5018", "time_from", "2006-06-29"
        ),
    )
    assert result.status == "ok"
    writer.patch_contact_relationship.assert_called_once_with(
        5018, time_from="2006-06-29"
    )
    assert "PUT /admin_contact_entity_relationship_row/5018" in result.detail


def test_dispatch_patch_contact_relationship_start_token_resolves_against_station():
    """`start` resolves against the id_entity slot (the station), NOT the
    relationship id — that's why the station goes in the id slot."""
    writer = MagicMock()
    writer._get_earliest_known.return_value = "2006-06-29"
    writer.patch_contact_relationship.return_value = {"ok": True}
    result = _dispatch_action(
        writer,
        _make_action(4316, "patch-contact-relationship", "5018", "time_from", "start"),
    )
    assert result.status == "ok"
    writer._get_earliest_known.assert_called_once_with(4316)
    writer.patch_contact_relationship.assert_called_once_with(
        5018, time_from="2006-06-29"
    )


def test_dispatch_patch_contact_relationship_role_field_no_date_check():
    writer = MagicMock()
    writer.patch_contact_relationship.return_value = {"ok": True}
    result = _dispatch_action(
        writer,
        _make_action(4316, "patch-contact-relationship", "5018", "role", "operator"),
    )
    assert result.status == "ok"
    writer.patch_contact_relationship.assert_called_once_with(5018, role="operator")


def test_dispatch_patch_contact_relationship_bad_field_rejected():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(4316, "patch-contact-relationship", "5018", "bogus", "x"),
    )
    assert result.status == "failed"
    assert "field must be one of" in result.detail
    writer.patch_contact_relationship.assert_not_called()


def test_dispatch_patch_contact_relationship_placeholder_rejected():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(
            4316, "patch-contact-relationship", "5018", "time_from", "<FILL_DATE>"
        ),
    )
    assert result.status == "failed"
    assert "placeholder" in result.detail
    writer.patch_contact_relationship.assert_not_called()


def test_dispatch_patch_contact_relationship_non_int_id_rejected():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(
            4316, "patch-contact-relationship", "abc", "time_from", "2006-06-29"
        ),
    )
    assert result.status == "failed"
    assert "integer id_relationship" in result.detail
    writer.patch_contact_relationship.assert_not_called()


def test_dispatch_patch_contact_relationship_writer_exception_captured():
    writer = MagicMock()
    writer._get_earliest_known.return_value = "2006-06-29"
    writer.patch_contact_relationship.side_effect = RuntimeError("simulated 500")
    result = _dispatch_action(
        writer,
        _make_action(
            4316, "patch-contact-relationship", "5018", "time_from", "2006-06-29"
        ),
    )
    assert result.status == "failed"
    assert "simulated 500" in result.detail


def test_dispatch_assign_contact_happy_path():
    writer = MagicMock()
    writer.create_contact_relationship.return_value = {"id": 6000}
    result = _dispatch_action(
        writer,
        _make_action(4316, "assign-contact", "1256", "operator", "2020-01-01"),
    )
    assert result.status == "ok"
    writer.create_contact_relationship.assert_called_once_with(
        1256, 4316, "operator", "2020-01-01"
    )
    assert "POST /contact_joins" in result.detail


def test_dispatch_assign_contact_non_int_contact_rejected():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(4316, "assign-contact", "notanint", "operator", "2020-01-01"),
    )
    assert result.status == "failed"
    assert "integer id_contact" in result.detail
    writer.create_contact_relationship.assert_not_called()


def test_dispatch_assign_contact_bad_date_rejected():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(4316, "assign-contact", "1256", "operator", "not-a-date"),
    )
    assert result.status == "failed"
    assert "YYYY-MM-DD" in result.detail
    writer.create_contact_relationship.assert_not_called()


def test_dispatch_delete_contact_relationship_happy_path():
    writer = MagicMock()
    writer.delete_contact_relationship.return_value = None
    result = _dispatch_action(
        writer,
        _make_action(4316, "delete-contact-relationship", "5018"),
    )
    assert result.status == "ok"
    writer.delete_contact_relationship.assert_called_once_with(5018)
    assert "DELETE /admin_contact_entity_relationship_row/5018" in result.detail


def test_dispatch_delete_contact_relationship_non_int_rejected():
    writer = MagicMock()
    result = _dispatch_action(
        writer,
        _make_action(4316, "delete-contact-relationship", "xyz"),
    )
    assert result.status == "failed"
    assert "integer id_relationship" in result.detail
    writer.delete_contact_relationship.assert_not_called()


# ---------------------------------------------------------------------------
# Post-apply guard: no duplicate open singular device on a station
# ---------------------------------------------------------------------------
from tostools.tos import _verify_no_duplicate_open_singular  # noqa: E402


def _dev_hist(did, subtype="gnss_receiver"):
    return {"id_entity": did, "code_entity_subtype": subtype, "attributes": []}


def _station_hist(
    pid, *, subtype="geophysical", name="OLKE", open_children=(), closed_children=()
):
    conns = [{"id_entity_child": c, "time_to": None} for c in open_children]
    conns += [
        {"id_entity_child": c, "time_to": "2000-10-17T00:00:00"}
        for c in closed_children
    ]
    return {
        "id_entity": pid,
        "code_entity_subtype": subtype,
        "attributes": [{"code": "name", "value_varchar": name, "time_to": None}],
        "children_connections": conns,
    }


def _guard_client(station, child_subtypes, opening_child_parent):
    histories = {station["id_entity"]: station}
    for cid, st in child_subtypes.items():
        histories[cid] = _dev_hist(cid, st)
    c = MagicMock()
    c.get_entity_history.side_effect = lambda i: histories.get(int(i))

    def _ph(cid):
        pid = opening_child_parent.get(int(cid))
        return (
            []
            if pid is None
            else [
                {
                    "id": 9000 + int(cid),
                    "id_entity_parent": pid,
                    "id_entity_child": int(cid),
                    "time_from": "2017-06-26",
                    "time_to": None,
                }
            ]
        )

    c.get_parent_history.side_effect = _ph
    return c


def test_guard_clean_single_open_receiver():
    """One open gnss_receiver (+ a closed phantom stub) → no violation."""
    station = _station_hist(4370, open_children=[21580], closed_children=[4979])
    c = _guard_client(
        station, {21580: "gnss_receiver", 4979: "gnss_receiver"}, {21580: 4370}
    )
    assert (
        _verify_no_duplicate_open_singular(
            c, [_make_action(21580, "create-join", "4370", "2017-06-26")]
        )
        == []
    )


def test_guard_flags_two_open_receivers():
    """Two open gnss_receivers (the failed-delete case) → violation."""
    station = _station_hist(4370, open_children=[21580, 4979])
    c = _guard_client(
        station, {21580: "gnss_receiver", 4979: "gnss_receiver"}, {21580: 4370}
    )
    v = _verify_no_duplicate_open_singular(
        c, [_make_action(21580, "create-join", "4370", "2017-06-26")]
    )
    assert len(v) == 1
    assert "2 open gnss_receiver" in v[0]
    assert "4979" in v[0] and "21580" in v[0]


def test_guard_ignores_warehouse_parent():
    """Warehouses hold multiple receivers by design → no violation."""
    wh = _station_hist(
        4, subtype="area", name="B9 - Kjallari - Jörð", open_children=[100, 200]
    )
    c = _guard_client(wh, {100: "gnss_receiver", 200: "gnss_receiver"}, {100: 4})
    assert _verify_no_duplicate_open_singular(c, [_make_action(100, "move", "4")]) == []


def test_guard_ignores_non_singular_subtype():
    """Two open solar_panels on a station is legitimate → no violation."""
    station = _station_hist(4370, open_children=[100, 200])
    c = _guard_client(station, {100: "solar_panel", 200: "solar_panel"}, {100: 4370})
    assert (
        _verify_no_duplicate_open_singular(
            c, [_make_action(100, "create-join", "4370", "2020-01-01")]
        )
        == []
    )


def test_guard_closed_stub_not_counted():
    """A zero-duration / closed join doesn't count toward the open total."""
    station = _station_hist(4370, open_children=[21580], closed_children=[4979, 4798])
    c = _guard_client(
        station,
        {21580: "gnss_receiver", 4979: "gnss_receiver", 4798: "gnss_receiver"},
        {21580: 4370},
    )
    assert (
        _verify_no_duplicate_open_singular(
            c, [_make_action(21580, "create-join", "4370", "2017-06-26")]
        )
        == []
    )


# ---------------------------------------------------------------------------
# Dry-run pre-flight: would the action SET leave a duplicate open singular?
# ---------------------------------------------------------------------------
from tostools.tos import _preflight_open_singular_conflicts  # noqa: E402


def _preflight_client(pid, open_children, child_subtypes, *, subtype="geophysical"):
    """open_children: list of (child_id, conn_id) currently open at the station."""
    conns = [
        {"id_entity_child": c, "id_entity_connection": cn, "time_to": None}
        for c, cn in open_children
    ]
    station = {
        "id_entity": pid,
        "code_entity_subtype": subtype,
        "attributes": [{"code": "name", "value_varchar": "OLKE", "time_to": None}],
        "children_connections": conns,
    }
    histories = {pid: station}
    for cid, st in child_subtypes.items():
        histories[cid] = {"id_entity": cid, "code_entity_subtype": st, "attributes": []}
    c = MagicMock()
    c.get_entity_history.side_effect = lambda i: histories.get(int(i))
    return c


def test_preflight_flags_open_without_close():
    """create-join a 2nd receiver without closing the 1st → warning."""
    c = _preflight_client(
        4370, [(4979, 6120)], {4979: "gnss_receiver", 21580: "gnss_receiver"}
    )
    w = _preflight_open_singular_conflicts(
        c, [_make_action(21580, "create-join", "4370", "2017-06-26")]
    )
    assert len(w) == 1
    assert "would leave 2 open gnss_receiver" in w[0]


def test_preflight_clean_swap_delete_join_no_warning():
    """delete-join old + create-join new = a clean swap → no warning."""
    c = _preflight_client(
        4370, [(4979, 6120)], {4979: "gnss_receiver", 21580: "gnss_receiver"}
    )
    actions = [
        _make_action(4979, "delete-join", "6120"),
        _make_action(21580, "create-join", "4370", "2017-06-26"),
    ]
    assert _preflight_open_singular_conflicts(c, actions) == []


def test_preflight_clean_swap_decommission_no_warning():
    """decommission old + create-join new → no warning."""
    c = _preflight_client(
        4370, [(4979, 6120)], {4979: "gnss_receiver", 21580: "gnss_receiver"}
    )
    actions = [
        _make_action(4979, "decommission", "2000-10-17"),
        _make_action(21580, "create-join", "4370", "2017-06-26"),
    ]
    assert _preflight_open_singular_conflicts(c, actions) == []


def test_preflight_clean_swap_patch_join_date_no_warning():
    """patch-join-date <conn> time_to <date> closes the old → no warning."""
    c = _preflight_client(
        4370, [(4979, 6120)], {4979: "gnss_receiver", 21580: "gnss_receiver"}
    )
    actions = [
        _make_action(4979, "patch-join-date", "6120", "time_to", "2013-02-28"),
        _make_action(21580, "create-join", "4370", "2017-06-26"),
    ]
    assert _preflight_open_singular_conflicts(c, actions) == []


def test_preflight_warehouse_destination_ignored():
    """Opening a receiver join at a warehouse is fine (holds many)."""
    c = _preflight_client(
        4, [(100, 7000)], {100: "gnss_receiver", 200: "gnss_receiver"}, subtype="area"
    )
    assert (
        _preflight_open_singular_conflicts(
            c, [_make_action(200, "create-join", "4", "2020-01-01")]
        )
        == []
    )


# ---------------------------------------------------------------------------
# _git_commit_triage_file — the --commit flag's git helper
# ---------------------------------------------------------------------------


def _git(repo, *argv):
    subprocess.run(
        ["git", "-C", str(repo), *argv], check=True, capture_output=True, text=True
    )


def _init_repo(tmp_path):
    repo = tmp_path / "corrections"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _head_count(repo):
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
    )
    return int(r.stdout.strip()) if r.returncode == 0 else 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_commit_triage_commits_new_file(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    station = repo / "gran"
    station.mkdir()
    f = station / "gran_fix.txt"
    f.write_text("ACTION 4909 create-join 4306 2015-05-01 2025-01-14\n")

    _git_commit_triage_file(f, message=None, n_ok=2)

    err = capsys.readouterr().err
    assert "committed gran/gran_fix.txt" in err
    assert _head_count(repo) == 1
    # The auto message uses the top-level dir + filename + action count.
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--pretty=%s"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert log == "gran: apply gran_fix.txt (2 action(s))"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_commit_triage_custom_message(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    f = repo / "x.txt"
    f.write_text("ACTION 1 defer\n")

    _git_commit_triage_file(f, message="custom subject line", n_ok=1)

    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--pretty=%s"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert log == "custom subject line"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_commit_triage_noop_when_already_committed(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    f = repo / "y.txt"
    f.write_text("ACTION 1 defer\n")
    _git_commit_triage_file(f, message=None, n_ok=1)
    capsys.readouterr()
    assert _head_count(repo) == 1

    # Second call with no change to the file must not create a new commit.
    _git_commit_triage_file(f, message=None, n_ok=1)
    err = capsys.readouterr().err
    assert "already committed" in err
    assert _head_count(repo) == 1


def test_commit_triage_outside_repo_warns_no_crash(tmp_path, capsys):
    # A bare directory (no `git init`) must not raise — just warn.
    f = tmp_path / "loose.txt"
    f.write_text("ACTION 1 defer\n")
    _git_commit_triage_file(f, message=None, n_ok=1)
    err = capsys.readouterr().err
    assert "not inside a git repository" in err
