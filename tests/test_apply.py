"""Tests for the `tos audit apply` action-file workflow.

Covers the parser (`_parse_action_file`), the dispatcher
(`_dispatch_action`), and the strict-then-permissive flow of
`_apply_main` (refuse on parse error; continue on individual failure).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from tostools.tos import (
    ParsedAction,
    ParseError,
    _dispatch_action,
    _fetch_action_meta,
    _parse_action_file,
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
        _make_action(
            4257, "add-attribute", "date_start", "2010-06-15", "2010-06-15"
        ),
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
        _make_action(
            4390, "add-attribute", "subtype", "GPS stöð", "2001-07-19"
        ),
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
        _make_action(
            4257, "add-attribute", "date_start", "2010-06-15", "2010-06-15"
        ),
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
        _make_action(
            4390, "add-attribute", "subtype", "GPS stöð", "2001-07-19"
        ),
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
        _make_action(
            4390, "add-attribute", "subtype", "GPS stöð", "2001-07-19"
        ),
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
        _make_action(
            4257, "add-attribute", "note", "<FILL_VALUE>", "2010-01-01"
        ),
    )

    assert result.status == "failed"
    assert "placeholder" in result.detail
    writer.get_attribute_values.assert_not_called()
    writer.add_attribute_value.assert_not_called()


def test_dispatch_add_attribute_fill_date_placeholder_refuses():
    writer = MagicMock()

    result = _dispatch_action(
        writer,
        _make_action(
            4257, "add-attribute", "date_start", "2010-01-01", "<FILL_DATE>"
        ),
    )

    assert result.status == "failed"
    assert "date placeholder" in result.detail
    writer.add_attribute_value.assert_not_called()


def test_dispatch_add_attribute_invalid_date_format_refuses():
    writer = MagicMock()

    result = _dispatch_action(
        writer,
        _make_action(
            4257, "add-attribute", "date_start", "2010-01-01", "not-a-date"
        ),
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
        _make_action(
            4257, "add-attribute", "date_start", "2010-06-15", "2010-06-15"
        ),
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
        _make_action(
            4257, "add-attribute", "date_start", "2010-06-15", "2010-06-15"
        ),
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
            50001, "subtype", "Old", "2001-01-01 00:00:00",
            date_to="2005-12-31 00:00:00",
        ),
        _attr_value(
            50002, "subtype", "Older", "1995-01-01 00:00:00",
            date_to="2000-12-31 00:00:00",
        ),
    ]
    writer.add_attribute_value.return_value = {"id_attribute_value": 99003}

    result = _dispatch_action(
        writer,
        _make_action(
            4390, "add-attribute", "subtype", "GPS stöð", "2006-01-01"
        ),
    )

    assert result.status == "ok"
    writer.add_attribute_value.assert_called_once_with(
        4390, "subtype", "GPS stöð", "2006-01-01"
    )
