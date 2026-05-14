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
    text = "ACTION 16321 fill-gap 1 4\n"
    actions, errors = _parse_action_file(text)
    assert actions == []
    assert len(errors) == 1
    assert "unknown verb 'fill-gap'" in errors[0].message


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
