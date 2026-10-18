"""Offline audit-apply regressions: inspect real writer bodies, never send HTTP."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tostools.api.tos_writer import TOSWriter
from tostools.tos import ParsedAction, _dispatch_action


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("unexpected HTTP request")

    monkeypatch.setattr("requests.sessions.Session.request", refuse)


def action(verb, *args):
    return ParsedAction(1, 19619, verb, list(args), "mock ACTION")


def row(start="2025-06-04T12:22:38", end=None, ident=1, value="5.5.0"):
    return dict(id_attribute_value=ident, date_from=start, date_to=end, value=value)


def writer(dry_run, rows=()):
    w: Any = TOSWriter(dry_run=dry_run, username="mock", password="mock")
    w.get_attribute_values = MagicMock(return_value=list(rows))
    w._get_entity_type = MagicMock(return_value=1)
    w._resolve_id_attribute = MagicMock(return_value=42)
    # Capture the final wire body after the unchanged low-level date normalizer.
    w._request = MagicMock(return_value={"id_attribute_value": 100})
    return w


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "start,end,expected_start,expected_end",
    [
        (
            "2021-05-25",
            "2026-03-11T13:20:00",
            "2021-05-25T00:00:00",
            "2026-03-11T13:20:00",
        ),
        ("2026-04-29T10:23:00", "open", "2026-04-29T10:23:00", None),
        (
            "2025-06-04T12:22:38",
            "2025-06-05T10:17:57",
            "2025-06-04T12:22:38",
            "2025-06-05T10:17:57",
        ),
        (
            "2025-06-04T12:22:38",
            "2025-06-04T12:22:39",
            "2025-06-04T12:22:38",
            "2025-06-04T12:22:39",
        ),
        ("2025-06-04", "2025-06-05", "2025-06-04T00:00:00", "2025-06-05T00:00:00"),
        ("2025-06-04 12:22:38", "open", "2025-06-04T12:22:38", None),
    ],
)
def test_add_exact_wire_dates(dry_run, start, end, expected_start, expected_end):
    w = writer(dry_run)
    result = _dispatch_action(
        w, action("add-attribute-period", "firmware_version", "5.5.0", start, end)
    )
    assert result.status == "ok", result.detail
    w._request.assert_called_once_with(
        "POST",
        "/admin_attribute_value_rows",
        data={
            "id_entity": 19619,
            "id_attribute": 42,
            "value_varchar": "5.5.0",
            "date_from": expected_start,
            "date_to": expected_end,
        },
    )


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "verb,field,new",
    [
        ("patch-attribute-date", "date_from", "2026-04-29T10:23:00"),
        ("patch-attribute-date-to", "date_to", "2026-03-11T13:20:00"),
        ("patch-attribute-date", "date_from", "2025-06-03"),
        ("patch-attribute-date-to", "date_to", "2025-06-05"),
        ("patch-attribute-date-to", "date_to", "2025-06-04T12:22:39"),
    ],
)
def test_patch_exact_wire_dates_and_timestamp_selection(dry_run, verb, field, new):
    w = writer(dry_run, [row(), row(start="2025-06-04 13:00:00", ident=2)])
    result = _dispatch_action(
        w, action(verb, "firmware_version", "2025-06-04T12:22:38", new)
    )
    assert result.status == "ok", result.detail
    w._request.assert_called_once_with(
        "PATCH",
        "/attribute_value/1",
        data={field: new if "T" in new else new + "T00:00:00"},
    )


@pytest.mark.parametrize("verb", ["patch-attribute-date", "patch-attribute-date-to"])
def test_bare_date_ambiguity_still_refused(verb):
    w = writer(True, [row(), row(start="2025-06-04T13:00:00", ident=2)])
    result = _dispatch_action(
        w, action(verb, "firmware_version", "2025-06-04", "2025-06-05")
    )
    assert result.status == "failed"
    assert "ambiguously" in result.detail
    w._request.assert_not_called()


@pytest.mark.parametrize("verb", ["patch-attribute-date", "patch-attribute-date-to"])
def test_bare_date_unique_match_retains_compatibility(verb):
    w = writer(True, [row()])
    result = _dispatch_action(
        w, action(verb, "firmware_version", "2025-06-04", "2025-06-05")
    )
    assert result.status == "ok", result.detail
    assert w._request.call_args.kwargs["data"] == {
        (
            "date_from" if verb == "patch-attribute-date" else "date_to"
        ): "2025-06-05T00:00:00"
    }


BAD_DATES = [
    "2025-02-29",
    "2025-13-01",
    "2025-06-04T24:00:00",
    "2025-06-04T12:22:60",
    "2025-06-04T12:22",
    "2025-06-04T12:22:38.000",
    "2025-06-04T12:22:38Z",
    "2025-06-04T12:22:38+00:00",
    "2025-06-04T12:22:38-03:00",
    "2025-06-04garbage",
    "20250604",
    "2025-6-04",
]


@pytest.mark.parametrize("bad", BAD_DATES)
@pytest.mark.parametrize(
    "slot", ["add_from", "add_to", "patch_from", "patch_to", "anchor_from", "anchor_to"]
)
def test_invalid_calendar_precision_and_zones_refused(bad, slot):
    w = writer(True, [row()])
    if slot.startswith("add"):
        args = ["firmware_version", "5.5.0", "2025-06-04", "2025-06-05"]
        args[2 if slot == "add_from" else 3] = bad
        a = action("add-attribute-period", *args)
    else:
        verb = (
            "patch-attribute-date-to" if slot.endswith("to") else "patch-attribute-date"
        )
        a = action(
            verb,
            "firmware_version",
            bad if slot.startswith("anchor") else "2025-06-04",
            bad if slot.startswith("patch") else "2025-06-05",
        )
    result = _dispatch_action(w, a)
    assert result.status == "failed", result.detail
    w._request.assert_not_called()


@pytest.mark.parametrize(
    "start,end",
    [
        ("2025-06-04", "2025-06-04"),
        ("2025-06-04T12:22:38", "2025-06-04T12:22:38"),
        ("2025-06-04T12:22:39", "2025-06-04T12:22:38"),
        ("2025-06-05", "2025-06-04"),
    ],
)
def test_add_zero_and_inverted_intervals_refused(start, end):
    w = writer(True)
    result = _dispatch_action(
        w, action("add-attribute-period", "firmware_version", "5.5.0", start, end)
    )
    assert result.status == "failed"
    w._request.assert_not_called()


@pytest.mark.parametrize(
    "verb,new",
    [
        ("patch-attribute-date-to", "2025-06-04T12:22:38"),
        ("patch-attribute-date-to", "2025-06-04T11:00:00"),
        ("patch-attribute-date", "2025-06-04T13:00:00"),
        ("patch-attribute-date", "2025-06-04T14:00:00"),
    ],
)
def test_patch_order_uses_actual_row_instant_not_bare_anchor(verb, new):
    w = writer(True, [row(end="2025-06-04T13:00:00")])
    result = _dispatch_action(w, action(verb, "firmware_version", "2025-06-04", new))
    assert result.status == "failed"
    w._request.assert_not_called()


@pytest.mark.parametrize(
    "old_from,old_to,new_from,new_to,noop",
    [
        ("2025-06-04T12:22:38", None, "2025-06-04T13:00:00", "open", False),
        (
            "2025-06-04T00:00:00",
            "2025-06-05T10:17:57",
            "2025-06-04",
            "2025-06-05T11:00:00",
            False,
        ),
        (
            "2025-06-04 12:22:38",
            "2025-06-05 10:17:57",
            "2025-06-04T12:22:38",
            "2025-06-05T10:17:57",
            True,
        ),
        ("2025-06-04T00:00:00", None, "2025-06-04", "open", True),
        ("2025-06-04T12:22:38", None, "2025-06-04", "open", False),
    ],
)
def test_add_idempotency_is_instant_exact(old_from, old_to, new_from, new_to, noop):
    w = writer(True, [row(old_from, old_to)])
    result = _dispatch_action(
        w, action("add-attribute-period", "firmware_version", "5.5.0", new_from, new_to)
    )
    assert result.status == "ok", result.detail
    assert w._request.call_count == (0 if noop else 1)
    if not noop:
        body = w._request.call_args.kwargs["data"]
        assert body["date_from"] == (
            new_from if "T" in new_from else new_from + "T00:00:00"
        )
        assert body["date_to"] == (None if new_to == "open" else new_to)


@pytest.mark.parametrize(
    "verb,new,noop",
    [
        ("patch-attribute-date", "2025-06-04T12:22:38", True),
        ("patch-attribute-date", "2025-06-04T12:22:39", False),
        ("patch-attribute-date-to", "2025-06-05T10:17:57", True),
        ("patch-attribute-date-to", "2025-06-05T10:17:58", False),
    ],
)
def test_patch_idempotency_is_instant_exact(verb, new, noop):
    w = writer(True, [row(end="2025-06-05 10:17:57")])
    result = _dispatch_action(w, action(verb, "firmware_version", "2025-06-04", new))
    assert result.status == "ok", result.detail
    assert w._request.call_count == (0 if noop else 1)
    if not noop:
        assert list(w._request.call_args.kwargs["data"].values()) == [new]


@pytest.mark.parametrize("verb", ["patch-attribute-date", "patch-attribute-date-to"])
@pytest.mark.parametrize("anchor", ["2025-06-04T12:22:37", "2025-06-04T12:22:38"])
def test_timestamp_anchor_missing_or_still_ambiguous_refuses(verb, anchor):
    w = writer(True, [row(), row(ident=2)])
    result = _dispatch_action(w, action(verb, "firmware_version", anchor, "2025-06-05"))
    assert result.status == "failed"
    w._request.assert_not_called()


@pytest.mark.parametrize("dry_run", [True, False])
def test_move_start_inside_same_day_closed_interval(dry_run):
    w = writer(dry_run, [row(end="2025-06-04T13:00:00")])
    result = _dispatch_action(
        w,
        action(
            "patch-attribute-date",
            "firmware_version",
            "2025-06-04T12:22:38",
            "2025-06-04T12:30:00",
        ),
    )
    assert result.status == "ok", result.detail
    w._request.assert_called_once_with(
        "PATCH", "/attribute_value/1", data={"date_from": "2025-06-04T12:30:00"}
    )


@pytest.mark.parametrize(
    "verb", ["add-attribute-period", "patch-attribute-date", "patch-attribute-date-to"]
)
@pytest.mark.parametrize("field", ["date_from", "date_to"])
def test_invalid_stored_boundaries_refuse_without_silent_strip(verb, field):
    existing = row(end="2025-06-05T10:17:57")
    existing[field] = f"{existing[field]}Z"
    w = writer(True, [existing])
    args = (
        ("firmware_version", "5.5.0", "2025-06-04", "open")
        if verb == "add-attribute-period"
        else ("firmware_version", "2025-06-04", "2025-06-05")
    )
    result = _dispatch_action(w, action(verb, *args))
    assert result.status == "failed", result.detail
    w._request.assert_not_called()


@pytest.mark.parametrize(
    "mode", ["success", "guard_violation", "guard_error", "committed_error"]
)
def test_apply_json_with_real_console_logging_after_eight_mock_writes(tmp_path, mode):
    # Subprocess isolates/restores all global logging state, including handlers
    # closed/replaced by dictConfig. Only fake clients/writers are constructed.
    script = r"""
import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from tostools.tos import _apply_main
from tostools.utils.logging import LoggingConfig, configure_logging
configure_logging(LoggingConfig(console_level=logging.WARNING))
path, mode = sys.argv[1:]
logger = logging.getLogger("tostools.mock_writer")
w = MagicMock()
w.get_attribute_values.return_value = []
committed = []
def add(*args):
    committed.append(args)
    logger.warning("mock write committed")
    if mode == "committed_error" and len(committed) == 8:
        raise RuntimeError("mock response decoding failed after commit")
    return {"id_attribute_value": len(committed)}
w.add_attribute_value.side_effect = add
def guard(*args):
    logger.warning("mock post-write guard diagnostic")
    if mode == "guard_error":
        raise RuntimeError("mock guard read failed")
    return ["mock duplicate receiver"] if mode == "guard_violation" else []
args = SimpleNamespace(action_file=path, port=443, server="mock.invalid", apply=True, json=True, commit=False)
with patch("tostools.api.tos_client.TOSClient"), patch("tostools.api.tos_writer.TOSWriter", return_value=w), patch("tostools.tos._fetch_action_meta", return_value={}), patch("tostools.tos._verify_no_duplicate_open_singular", side_effect=guard):
    rc = _apply_main(args)
assert len(committed) == 8
assert committed[-1][3:] == ("2026-04-29", None)
assert rc == (0 if mode == "success" else 1)
"""
    actions = tmp_path / "actions.txt"
    actions.write_text(
        "\n".join(
            f"ACTION {19619 + i} add-attribute-period GPS true 2026-04-29 open"
            for i in range(8)
        )
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    proc = subprocess.run(
        [sys.executable, "-c", script, str(actions), mode],
        env=env,
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["total_actions"] == 8
    assert len(payload["results"]) == 8
    assert payload["results"][-1]["status"] == (
        "failed" if mode == "committed_error" else "ok"
    )
    assert "mock write committed" in proc.stderr
    assert "mock post-write guard diagnostic" in proc.stderr
    if mode.startswith("guard_"):
        assert payload["post_apply_violations"]
        assert "mock" in " ".join(payload["post_apply_violations"])
