"""Tests for ``tos device delete`` (``_device_delete_main``).

Covers the guard rails (not-found, still-joined, non-device subtype),
the dry-run plan (no writes), and the apply path including the
re-read-after-delete verdict (deleted vs silent-no-op).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from tostools.tos import _append_device_deletion_record, _device_delete_main

_DEVHIST = {
    "code_entity_subtype": "gnss_receiver",
    "attributes": [
        {
            "code": "serial_number",
            "value": "5046K71747",
            "date_to": None,
            "id_attribute_value": 153697,
        },
        {
            "code": "model",
            "value": "TRIMBLE NETR9",
            "date_to": None,
            "id_attribute_value": 153698,
        },
    ],
    "children_connections": None,
}


def _args(**kw):
    base = dict(
        id_entity=21602,
        apply=False,
        force=False,
        commit=False,
        note=None,
        server="x",
        port=443,
        json=False,
    )
    base.update(kw)
    return Namespace(**base)


def _patches(client, writer=None):
    """Patch the in-function imports of TOSClient / TOSWriter."""
    writer = writer or MagicMock()
    return (
        patch("tostools.api.tos_client.TOSClient", return_value=client),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    )


def _run(client, writer=None, **argkw):
    cpatch, wpatch = _patches(client, writer)
    with cpatch, wpatch:
        return _device_delete_main(_args(**argkw))


def test_delete_not_found_returns_2():
    client = MagicMock()
    client.get_entity_history.return_value = None
    assert _run(client) == 2


def test_delete_refuses_when_joined():
    client = MagicMock()
    client.get_entity_history.return_value = _DEVHIST
    client.get_parent_history.return_value = [{"id": 99, "id_entity_parent": 4306}]
    writer = MagicMock()
    assert _run(client, writer) == 1
    writer.delete_entity.assert_not_called()
    writer.delete_attribute_value.assert_not_called()


def test_delete_refuses_with_children():
    client = MagicMock()
    hist = {**_DEVHIST, "children_connections": [{"id": 5}]}
    client.get_entity_history.return_value = hist
    client.get_parent_history.return_value = []
    writer = MagicMock()
    assert _run(client, writer) == 1
    writer.delete_entity.assert_not_called()


def test_delete_refuses_non_device_subtype_without_force():
    client = MagicMock()
    client.get_entity_history.return_value = {
        **_DEVHIST,
        "code_entity_subtype": "station",
    }
    client.get_parent_history.return_value = []
    writer = MagicMock()
    assert _run(client, writer) == 1
    writer.delete_entity.assert_not_called()


def test_delete_force_allows_non_device_subtype():
    client = MagicMock()
    # first read = the entity; re-read = gone
    client.get_entity_history.side_effect = [
        {**_DEVHIST, "code_entity_subtype": "station"},
        None,
    ]
    client.get_parent_history.return_value = []
    writer = MagicMock()
    assert _run(client, writer, apply=True, force=True) == 0
    writer.delete_entity.assert_called_once_with(21602)


def test_delete_dry_run_touches_nothing():
    client = MagicMock()
    client.get_entity_history.return_value = _DEVHIST
    client.get_parent_history.return_value = []
    writer = MagicMock()
    assert _run(client, writer) == 0  # apply=False (default)
    writer.delete_entity.assert_not_called()
    writer.delete_attribute_value.assert_not_called()
    # exactly one read (the initial fetch) — no re-read in dry-run
    assert client.get_entity_history.call_count == 1


def test_delete_apply_deletes_attrs_then_entity_and_confirms_gone():
    client = MagicMock()
    client.get_entity_history.side_effect = [_DEVHIST, None]  # fetch, then re-read=gone
    client.get_parent_history.return_value = []
    writer = MagicMock()
    assert _run(client, writer, apply=True) == 0
    # both attribute_value rows deleted, then the entity
    assert writer.delete_attribute_value.call_count == 2
    writer.delete_attribute_value.assert_any_call(153697)
    writer.delete_attribute_value.assert_any_call(153698)
    writer.delete_entity.assert_called_once_with(21602)
    # re-read happened
    assert client.get_entity_history.call_count == 2


def test_delete_apply_silent_noop_returns_1():
    client = MagicMock()
    # DELETE no-ops: the entity is still there on re-read
    client.get_entity_history.side_effect = [_DEVHIST, _DEVHIST]
    client.get_parent_history.return_value = []
    writer = MagicMock()
    assert _run(client, writer, apply=True) == 1
    writer.delete_entity.assert_called_once_with(21602)


# ---------------------------------------------------------------------------
# --commit audit logging to the gps-tos-corrections repo
# ---------------------------------------------------------------------------


def _init_repo(tmp_path):
    repo = tmp_path / "corrections"
    repo.mkdir()
    for argv in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True)
    return repo


def _head_count(repo):
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
    )
    return int(r.stdout.strip()) if r.returncode == 0 else 0


def test_append_device_deletion_record_creates_and_appends(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    p = _append_device_deletion_record(repo, {"id_entity": 21602, "serial": "X"})
    p2 = _append_device_deletion_record(repo, {"id_entity": 99, "serial": "Y"})
    assert p == p2 == repo / "deletions" / "device_deletions.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["id_entity"] == 21602
    assert json.loads(lines[1])["serial"] == "Y"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_delete_apply_commit_logs_and_commits(tmp_path):
    repo = _init_repo(tmp_path)
    client = MagicMock()
    client.get_entity_history.side_effect = [_DEVHIST, None]  # fetch, re-read=gone
    client.get_parent_history.return_value = []
    writer = MagicMock()
    cpatch, wpatch = _patches(client, writer)
    with (
        cpatch,
        wpatch,
        patch("tostools.archive.tos_corrections_dir", return_value=repo),
    ):
        rc = _device_delete_main(
            _args(apply=True, commit=True, note="dup husk of 5046K71747")
        )
    assert rc == 0
    log = repo / "deletions" / "device_deletions.jsonl"
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert rec["id_entity"] == 21602
    assert rec["serial"] == "5046K71747"
    assert rec["note"] == "dup husk of 5046K71747"
    assert rec["action"] == "device_delete"
    assert _head_count(repo) == 1  # the log was committed


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_delete_apply_commit_skips_log_on_noop(tmp_path):
    repo = _init_repo(tmp_path)
    client = MagicMock()
    client.get_entity_history.side_effect = [_DEVHIST, _DEVHIST]  # re-read=still there
    client.get_parent_history.return_value = []
    writer = MagicMock()
    cpatch, wpatch = _patches(client, writer)
    with (
        cpatch,
        wpatch,
        patch("tostools.archive.tos_corrections_dir", return_value=repo),
    ):
        rc = _device_delete_main(_args(apply=True, commit=True))
    assert rc == 1
    # no record written, no commit, when the deletion wasn't confirmed
    assert not (repo / "deletions").exists()
    assert _head_count(repo) == 0
