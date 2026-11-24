"""Tests for `tos visit remove`: read first, dry-run by default, audit on commit."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from tostools.tos import _visit_main

# the shape GET /maintenance/id_maintenance/{id} returns: work and the
# owning entity are only on the attribute-value rows
VISIT = {
    "id_maintenance": 5147,
    "start_time": "2026-11-20T00:00:00",
    "maintenance_type": "remote",
    "maintenance_attribute_values": [
        {"id_entity": 4390, "code": "comment", "value": ""},
        {"id_entity": 4390, "code": "work", "value": "created by mistake"},
    ],
}


def _run(*argv, visit=VISIT):
    client = MagicMock()
    client.get_maintenance_visit.return_value = visit
    writer = MagicMock()
    writer.delete_maintenance.return_value = None
    with (
        patch("tostools.api.tos_client.TOSClient", return_value=client),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer) as cls,
        patch("tostools.tos._audit_log_contact_change") as audit,
    ):
        rc = _visit_main(["remove", *argv])
    return rc, cls, writer, audit


def test_remove_is_dry_run_by_default(capsys):
    rc, cls, writer, audit = _run("5147")
    assert rc == 0
    # no writer at all: a dry-run must not need a TOS login
    cls.assert_not_called()
    writer.delete_maintenance.assert_not_called()
    audit.assert_not_called()
    out = capsys.readouterr().out
    assert "Would remove vitjun id=5147 entity=4390" in out
    assert "created by mistake" in out


def test_no_dry_run_deletes_live():
    rc, cls, writer, audit = _run("5147", "--no-dry-run")
    assert rc == 0
    assert cls.call_args.kwargs["dry_run"] is False
    writer.delete_maintenance.assert_called_once_with(5147)
    audit.assert_not_called()


def test_unknown_id_exits_1_without_any_write(capsys):
    rc, cls, writer, audit = _run("9999", "--no-dry-run", "--commit", visit=None)
    assert rc == 1
    cls.assert_not_called()
    writer.delete_maintenance.assert_not_called()
    audit.assert_not_called()
    assert "nothing removed" in capsys.readouterr().err


def test_commit_records_the_deleted_row():
    rc, _, _, audit = _run("5147", "--no-dry-run", "--commit")
    assert rc == 0
    kw = audit.call_args.kwargs
    assert kw["subdir"] == "deletions"
    assert kw["filename"] == "visit_deletions.jsonl"
    assert kw["record"] == {"id_maintenance": 5147, "deleted": VISIT}


def test_commit_without_no_dry_run_records_nothing(capsys):
    rc, _, _, audit = _run("5147", "--commit")
    assert rc == 0
    audit.assert_not_called()
    assert "--commit has no effect" in capsys.readouterr().err


def test_writer_error_exits_1(capsys):
    client = MagicMock()
    client.get_maintenance_visit.return_value = VISIT
    writer = MagicMock()
    writer.delete_maintenance.side_effect = ValueError("403 Forbidden")
    with (
        patch("tostools.api.tos_client.TOSClient", return_value=client),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
        patch("tostools.tos._audit_log_contact_change") as audit,
    ):
        rc = _visit_main(["remove", "5147", "--no-dry-run", "--commit"])
    assert rc == 1
    audit.assert_not_called()
    assert "403 Forbidden" in capsys.readouterr().err


def test_json_output(capsys):
    rc, *_ = _run("5147", "--json")
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["verb"] == "remove"
    assert doc["dry_run"] is True
    assert doc["visit"] == VISIT


def test_network_timeout_is_one_line_and_records_nothing(capsys):
    import requests

    client = MagicMock()
    client.get_maintenance_visit.return_value = VISIT
    writer = MagicMock()
    writer.delete_maintenance.side_effect = requests.ReadTimeout("Read timed out")
    with (
        patch("tostools.api.tos_client.TOSClient", return_value=client),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
        patch("tostools.tos._audit_log_contact_change") as audit,
    ):
        rc = _visit_main(["remove", "5147", "--no-dry-run", "--commit"])
    assert rc == 1
    audit.assert_not_called()
    err = capsys.readouterr().err
    assert "Read timed out" in err and "tos visit show 5147" in err
