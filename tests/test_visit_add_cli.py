"""Tests for `tos visit add` CLI date-token resolution.

Regression pin for the `--start now` bug: the CLI used to pass the
raw token straight to TOS, which 422'd ("input is too short"). The
add path now routes `--start` / `--end` through `_resolve_date_token`
(the same helper the apply ACTION verbs use), so `now` → today (UTC)
and literal dates pass through unchanged.
"""

from __future__ import annotations

import datetime as _dt
from unittest.mock import MagicMock, patch

from tostools.tos import _visit_main


def _today_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def _run_add(*extra):
    """Invoke `tos visit add` on an --entity target with a mock writer.

    Returns the MagicMock standing in for the writer so callers can
    assert on the add_maintenance_visit kwargs. --entity avoids any
    station-resolution HTTP; the mock writer avoids auth / network.
    """
    argv = [
        "add",
        "--entity",
        "4390",
        "--type",
        "remote",
        "--work",
        "x",
        *extra,
    ]
    writer = MagicMock()
    writer.add_maintenance_visit.return_value = {"id_maintenance": "<dry-run>"}
    with (
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
        patch("tostools.api.tos_client.TOSClient"),
    ):
        rc = _visit_main(argv)
    return rc, writer


def test_visit_add_resolves_now_token():
    rc, writer = _run_add("--start", "now")
    assert rc == 0
    kwargs = writer.add_maintenance_visit.call_args.kwargs
    assert kwargs["start_time"] == _today_utc()
    # --end defaults to None (writer promotes to start) — not the token.
    assert kwargs["end_time"] is None


def test_visit_add_resolves_now_for_both_start_and_end():
    rc, writer = _run_add("--start", "now", "--end", "now")
    assert rc == 0
    kwargs = writer.add_maintenance_visit.call_args.kwargs
    assert kwargs["start_time"] == _today_utc()
    assert kwargs["end_time"] == _today_utc()


def test_visit_add_passes_literal_date_through():
    rc, writer = _run_add("--start", "2026-05-30")
    assert rc == 0
    kwargs = writer.add_maintenance_visit.call_args.kwargs
    assert kwargs["start_time"] == "2026-05-30"
