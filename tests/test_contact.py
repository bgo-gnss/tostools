"""Tests for `tos contact` — id_contact-namespace drill-down.

Contacts live in their own id namespace (id_contact / `/contact/{id}/`),
distinct from the entity namespace (id_entity / `/entity/{id}/`). This
file pins the dispatch surface for the new verb pair:

  * ``tos contact show --id N``     → /contact/{N}/
  * ``tos contact list --station S``→ entity_contacts/{station_id}/

Mocks both client methods so tests stay offline.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from tostools.api.tos_client import TOSClient
from tostools.tos import main as tos_main


def _veduri_contact():
    """One representative contact payload from `/contact/{id}/`."""
    return {
        "id": 1256,
        "name": "Veðurstofa Íslands",
        "organization": "Veðurstofa Íslands",
        "job_title": "",
        "phone_primary": "5226000",
        "phone_secondary": "",
        "phone_tertiary": "",
        "email": "",
        "address": "Bústaðarvegur 7-9, 105 Reykjavík, Ísland",
        "comment": "",
        "ssid": "6309080350",
        "start_date": "1845-01-01T00:00:00",
        "end_date": None,
    }


# ---------------------------------------------------------------------------
# tos contact show
# ---------------------------------------------------------------------------


def test_contact_show_renders_record(capsys):
    """`tos contact show --id N` fetches via TOSClient.get_contact and
    renders the field table."""
    payload = _veduri_contact()
    with patch.object(TOSClient, "get_contact", return_value=payload) as gc:
        rc = tos_main(["contact", "show", "--id", "1256"])

    assert rc == 0
    out = capsys.readouterr().out
    # Header reflects id + name.
    assert "Contact id=" in out
    assert "1256" in out
    assert "Veðurstofa" in out
    # Contact attributes table appears.
    assert "Contact attributes" in out
    # Schema-accurate field labels (no role / role_is — those live on
    # the relationship row, not the contact entity).
    assert "organization" in out
    assert "phone_primary" in out
    assert "address" in out
    # Fetched with the right id.
    gc.assert_called_once_with(1256)


def test_contact_show_missing_returns_1(capsys):
    """Unknown id_contact → exit 1 with a stderr message."""
    with patch.object(TOSClient, "get_contact", return_value=None):
        rc = tos_main(["contact", "show", "--id", "999999"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "No contact found for id_contact=999999" in err


def test_contact_show_requires_some_selector(capsys):
    """`tos contact show` with no --id / --name / --email is a usage
    error (exit 2). Argparse can't enforce "any one of these" so we
    do it explicitly in the handler — clearer message than argparse's
    generic --required failure."""
    rc = tos_main(["contact", "show"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "one of --id, --name, --email is required" in err


def test_contact_show_by_name_unique_match_renders_detail(capsys):
    """A --name filter that resolves to exactly one contact renders
    the full detail view — same path as --id."""
    rows = [
        {
            "id": 1259,
            "name": "Vegagerðin",
            "phone_primary": None,
            "email": "",
            "address": "",
            "start_date": "1992-01-01T00:00:00",
            "end_date": None,
        },
        {"id": 1256, "name": "Veðurstofa Íslands"},
    ]
    with patch.object(TOSClient, "list_all_contacts", return_value=rows):
        rc = tos_main(["contact", "show", "--name", "Vega"])
    assert rc == 0
    out = capsys.readouterr().out
    # Detail-view markers: header + Contact attributes table.
    assert "Contact id=" in out
    assert "1259" in out
    assert "Vegagerðin" in out
    assert "Contact attributes" in out


def test_contact_show_by_name_multi_match_renders_compact_list(capsys):
    """A --name filter that resolves to ≥2 contacts emits the compact
    table + a 'pick one with --id N' hint. Exit 0 because we surfaced
    a useful answer (just ambiguous)."""
    rows = [
        {"id": 1256, "name": "Veðurstofa Íslands"},
        {"id": 1257, "name": "Veðurstofa Íslands - Ofanflóð"},
        {"id": 1982, "name": "Starfsmenn Veðurstofunnar"},
    ]
    with patch.object(TOSClient, "list_all_contacts", return_value=rows):
        rc = tos_main(["contact", "show", "--name", "Veður"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "All TOS contacts — 3 record(s)" in out
    assert "matches — narrow the filter or pick one with" in out
    assert "tos contact show --id 1256" in out


def test_contact_show_by_name_zero_match_returns_1(capsys):
    """No match → exit 1 with the filter args echoed in the message
    so the operator sees what was searched."""
    with patch.object(TOSClient, "list_all_contacts", return_value=[]):
        rc = tos_main(["contact", "show", "--name", "no-such-org"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No contact matches" in err
    assert "no-such-org" in err


def test_contact_show_id_wins_over_filters(capsys):
    """When both --id and --name/--email are given, --id takes
    precedence (exact-match short-circuit). Filters are ignored."""
    payload = _veduri_contact()
    with (
        patch.object(TOSClient, "get_contact", return_value=payload) as gc,
        patch.object(TOSClient, "list_all_contacts") as la,
    ):
        rc = tos_main(["contact", "show", "--id", "1256", "--name", "Vega"])
    assert rc == 0
    gc.assert_called_once_with(1256)
    # Filter path skipped — saves a round-trip.
    la.assert_not_called()


def test_contact_show_json_emits_raw_payload(capsys):
    """`--json` bypasses the pretty renderer and emits the raw dict."""
    payload = _veduri_contact()
    with patch.object(TOSClient, "get_contact", return_value=payload):
        rc = tos_main(["contact", "show", "--id", "1256", "--json"])

    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == payload


# ---------------------------------------------------------------------------
# tos contact list
# ---------------------------------------------------------------------------


def test_contact_list_renders_station_contacts(capsys):
    """`tos contact list --station <STN>` resolves the station then
    delegates to TOSClient.get_contacts — same data the embedded
    Contacts section in `tos station show` uses."""
    contacts = [
        {
            "id_contact": 1256,
            "role": "owner",
            "role_is": "Eigandi stöðvar",
            "name": "Veðurstofa Íslands",
            "organization": "Veðurstofa Íslands",
            "phone_primary": "5226000",
            "address": "Bústaðarvegur 7-9, 105 Reykjavík, Ísland",
            "per_time_from": "2007-09-02T00:00:00",
            "per_time_to": None,
        }
    ]
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(TOSClient, "get_contacts", return_value=contacts) as gc,
    ):
        rc = tos_main(["contact", "list", "--station", "HEDI"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Contacts — 1 record(s)" in out
    assert "1256" in out
    assert "Eigandi" in out
    gc.assert_called_once_with(4257)


def test_contact_list_unresolvable_station_returns_1(capsys):
    """Unknown station marker → exit 1, distinct from
    `tos contact show` lookup miss but symmetric in exit code."""
    with patch("tostools.tos._resolve_parent_id", return_value=None):
        rc = tos_main(["contact", "list", "--station", "ZZZZ"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "No station found for marker 'ZZZZ'" in err


def test_contact_list_all_renders_full_table(capsys):
    """`tos contact list` with no --station fetches every contact in
    TOS via TOSClient.list_all_contacts and renders the compact
    id / name / phone / email table. Distinct shape from the
    --station view because there is no per-station role attached."""
    contacts = [
        {
            "id": 1256,
            "name": "Veðurstofa Íslands",
            "organization": "Veðurstofa Íslands",
            "phone_primary": "5226000",
            "email": "",
            "start_date": "1845-01-01T00:00:00",
            "end_date": None,
        },
        {
            "id": 1257,
            "name": "Veðurstofa Íslands - Ofanflóð",
            "phone_primary": None,
            "start_date": "1990-01-01T00:00:00",
            "end_date": None,
        },
    ]
    with patch.object(TOSClient, "list_all_contacts", return_value=contacts) as la:
        rc = tos_main(["contact", "list"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "All TOS contacts — 2 record(s)" in out
    assert "1256" in out
    assert "1257" in out
    la.assert_called_once_with()


def test_contact_list_all_empty_message(capsys):
    """Endpoint returns empty list (or 404 → silently empty per the
    client wrapper) → placeholder message."""
    with patch.object(TOSClient, "list_all_contacts", return_value=[]):
        rc = tos_main(["contact", "list"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "All TOS contacts — 0 record(s)" in out
    assert "(no contacts returned)" in out


def test_contact_list_filter_name_substring():
    """Plain-text --name pattern matches as a case-insensitive
    substring. `veður` finds `Veðurstofa Íslands`."""
    rows = [
        {"id": 1, "name": "Veðurstofa Íslands", "email": ""},
        {"id": 2, "name": "Landsvirkjun", "email": ""},
        {"id": 3, "organization": "Vegagerðin", "email": ""},
    ]
    payload = json.loads(
        _stdout_via_json(["contact", "list", "--name", "veður", "--json"], rows)
    )
    assert [r["id"] for r in payload["contacts"]] == [1]


def test_contact_list_filter_name_glob_star():
    """Glob wildcards translate to regex: `*stof*` matches anything
    containing `stof`."""
    rows = [
        {"id": 1, "name": "Veðurstofa Íslands", "email": ""},
        {"id": 2, "name": "Vegagerðin", "email": ""},
    ]
    payload = json.loads(
        _stdout_via_json(["contact", "list", "--name", "*stof*", "--json"], rows)
    )
    assert [r["id"] for r in payload["contacts"]] == [1]


def test_contact_list_filter_name_regex_alternation():
    """Full regex syntax: `vega|landsvirkjun` matches either."""
    rows = [
        {"id": 1, "name": "Veðurstofa Íslands", "email": ""},
        {"id": 2, "name": "Vegagerðin", "email": ""},
        {"id": 3, "name": "Landsvirkjun", "email": ""},
    ]
    payload = json.loads(
        _stdout_via_json(
            ["contact", "list", "--name", "vega|landsvirkjun", "--json"], rows
        )
    )
    assert sorted(r["id"] for r in payload["contacts"]) == [2, 3]


def test_contact_list_filter_email_regex():
    """`--email "@vedur\\.is$"` matches rows where the email ends in
    @vedur.is. Rows without an email are excluded (no value to
    match against)."""
    rows = [
        {"id": 1, "name": "A", "email": "alice@vedur.is"},
        {"id": 2, "name": "B", "email": "bob@example.com"},
        {"id": 3, "name": "C", "email": ""},
    ]
    payload = json.loads(
        _stdout_via_json(["contact", "list", "--email", "@vedur\\.is$", "--json"], rows)
    )
    assert [r["id"] for r in payload["contacts"]] == [1]


def test_contact_list_filters_and_combined():
    """When both --name and --email are given, they're AND'd."""
    rows = [
        {"id": 1, "name": "Veður A", "email": "a@vedur.is"},
        {"id": 2, "name": "Veður B", "email": "b@example.com"},
        {"id": 3, "name": "Other", "email": "c@vedur.is"},
    ]
    payload = json.loads(
        _stdout_via_json(
            [
                "contact",
                "list",
                "--name",
                "veður",
                "--email",
                "@vedur\\.is$",
                "--json",
            ],
            rows,
        )
    )
    assert [r["id"] for r in payload["contacts"]] == [1]


def test_contact_list_filter_invalid_regex_falls_back_to_substring():
    """Malformed regex (unbalanced `(`) falls back to literal
    substring search. Keeps the CLI forgiving — a stray paren in a
    name shouldn't crash the list."""
    rows = [
        {"id": 1, "name": "Owner (legacy)", "email": ""},
        {"id": 2, "name": "Owner", "email": ""},
    ]
    payload = json.loads(
        _stdout_via_json(["contact", "list", "--name", "(legacy", "--json"], rows)
    )
    # Substring "(legacy" only appears in row 1.
    assert [r["id"] for r in payload["contacts"]] == [1]


def _stdout_via_json(argv, rows):
    """Helper: run the CLI with mocked rows + --json, return captured stdout."""
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with patch.object(TOSClient, "list_all_contacts", return_value=rows):
        with redirect_stdout(buf):
            tos_main(argv)
    return buf.getvalue()


def test_contact_list_all_json_emits_contacts_key(capsys):
    """JSON shape for the no-filter mode is `{contacts: [...]}` (no
    station / id_entity fields because there's no station context)."""
    contacts = [{"id": 1256, "name": "A"}]
    with patch.object(TOSClient, "list_all_contacts", return_value=contacts):
        rc = tos_main(["contact", "list", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"contacts": contacts}
    # `station` / `id_entity` only appear in the --station mode payload.
    assert "station" not in payload
    assert "id_entity" not in payload


def test_contact_list_json_includes_station_id_and_rows(capsys):
    """JSON payload exposes id_entity (for downstream cross-ref) and
    the raw contacts list."""
    contacts = [{"id_contact": 1, "name": "A"}]
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(TOSClient, "get_contacts", return_value=contacts),
    ):
        rc = tos_main(["contact", "list", "--station", "HEDI", "--json"])

    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["id_entity"] == 4257
    assert parsed["contacts"] == contacts


# ---------------------------------------------------------------------------
# Drill hint integration
# ---------------------------------------------------------------------------


def test_station_show_drill_hint_includes_contact_id_when_present(capsys):
    """When `tos station show` has contacts, the drill-deeper block
    includes a `tos contact show --id N` line referencing the first
    contact's id_contact. Lets operators copy-paste straight into the
    correct namespace."""
    from tests.test_station_show import (
        _fake_get_entity_history_factory,
        _show_args,
    )
    from tostools.tos import _station_show_main

    contacts = [{"id_contact": 1256, "role": "owner", "name": "VI"}]
    with (
        patch("tostools.tos._resolve_parent_id", return_value=4257),
        patch.object(
            TOSClient,
            "get_entity_history",
            side_effect=_fake_get_entity_history_factory(4257),
        ),
        patch.object(TOSClient, "get_contacts", return_value=contacts),
    ):
        rc = _station_show_main(_show_args())

    assert rc == 0
    out = capsys.readouterr().out
    assert "tos contact show --id 1256" in out


# ---------------------------------------------------------------------------
# Write verbs — patch-relationship / assign / remove (dry-run default)
# ---------------------------------------------------------------------------


def test_contact_patch_relationship_dry_run_default(capsys):
    """`tos contact patch-relationship <id> --time-from DATE` constructs
    the writer dry-run by default and calls patch_contact_relationship."""
    from tostools.api.tos_writer import DryRunResult, TOSWriter

    with patch.object(
        TOSWriter,
        "patch_contact_relationship",
        autospec=True,
        return_value=DryRunResult("PUT", "/x", {}),
    ) as pcr:
        rc = tos_main(
            ["contact", "patch-relationship", "5018", "--time-from", "2006-06-29"]
        )

    assert rc == 0
    pcr.assert_called_once()
    # Writer instance is dry_run=True (no --no-dry-run).
    assert pcr.call_args.args[0].dry_run is True
    assert pcr.call_args.args[1] == 5018
    assert pcr.call_args.kwargs == {"time_from": "2006-06-29"}
    out = capsys.readouterr().out
    assert "Patched relationship 5018" in out
    assert "(dry-run)" in out


def test_contact_patch_relationship_no_dry_run_commits(capsys):
    from tostools.api.tos_writer import TOSWriter

    with (
        patch.object(
            TOSWriter,
            "patch_contact_relationship",
            autospec=True,
            return_value={"ok": 1},
        ) as pcr,
        patch.object(TOSWriter, "_ensure_authenticated", autospec=True),
    ):
        rc = tos_main(
            [
                "contact",
                "patch-relationship",
                "5018",
                "--time-from",
                "2006-06-29",
                "--no-dry-run",
            ]
        )

    assert rc == 0
    assert pcr.call_args.args[0].dry_run is False
    out = capsys.readouterr().out
    assert "(dry-run)" not in out


def test_contact_patch_relationship_requires_a_field(capsys):
    rc = tos_main(["contact", "patch-relationship", "5018"])
    assert rc == 2
    assert "at least one of" in capsys.readouterr().err


def test_contact_patch_relationship_json(capsys):
    from tostools.api.tos_writer import DryRunResult, TOSWriter

    with patch.object(
        TOSWriter,
        "patch_contact_relationship",
        autospec=True,
        return_value=DryRunResult("PUT", "/x", {}),
    ):
        rc = tos_main(
            ["contact", "patch-relationship", "5018", "--role", "operator", "--json"]
        )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verb"] == "patch-relationship"
    assert payload["id_rel"] == 5018
    assert payload["changes"] == {"role": "operator"}
    assert payload["dry_run"] is True


def test_contact_assign_resolves_station_and_creates(capsys):
    from tostools.api.tos_writer import DryRunResult, TOSWriter

    with (
        patch("tostools.tos._resolve_parent_id", return_value=4316),
        patch.object(
            TOSWriter,
            "create_contact_relationship",
            autospec=True,
            return_value=DryRunResult("POST", "/contact_joins", {}),
        ) as ccr,
    ):
        rc = tos_main(
            [
                "contact",
                "assign",
                "--station",
                "HEDI",
                "--contact",
                "1256",
                "--role",
                "operator",
                "--from",
                "2020-01-01",
            ]
        )

    assert rc == 0
    ccr.assert_called_once()
    # (self, id_contact, id_entity, role, time_from)
    assert ccr.call_args.args[1:] == (1256, 4316, "operator", "2020-01-01")
    out = capsys.readouterr().out
    assert "Assigned contact 1256" in out
    assert "(dry-run)" in out


def test_contact_assign_unresolvable_station_returns_1(capsys):
    with patch("tostools.tos._resolve_parent_id", return_value=None):
        rc = tos_main(
            [
                "contact",
                "assign",
                "--station",
                "XXXX",
                "--contact",
                "1256",
                "--role",
                "owner",
                "--from",
                "2020-01-01",
            ]
        )
    assert rc == 1
    assert "No station found for marker 'XXXX'" in capsys.readouterr().err


def test_contact_remove_dry_run_default(capsys):
    from tostools.api.tos_writer import DryRunResult, TOSWriter

    with patch.object(
        TOSWriter,
        "delete_contact_relationship",
        autospec=True,
        return_value=DryRunResult("DELETE", "/x", None),
    ) as dcr:
        rc = tos_main(["contact", "remove", "5018"])

    assert rc == 0
    dcr.assert_called_once()
    assert dcr.call_args.args[0].dry_run is True
    assert dcr.call_args.args[1] == 5018
    out = capsys.readouterr().out
    assert "Removed relationship 5018" in out
    assert "(dry-run)" in out


# ---------------------------------------------------------------------------
# Contact-entity write verbs — create / patch-entity
# ---------------------------------------------------------------------------


def test_contact_create_dry_run_default(capsys):
    from tostools.api.tos_writer import DryRunResult, TOSWriter

    with patch.object(
        TOSWriter,
        "create_contact",
        autospec=True,
        return_value=DryRunResult("POST", "/contacts", {}),
    ) as cc:
        rc = tos_main(["contact", "create", "--name", "Test Org", "--phone", "555"])
    assert rc == 0
    cc.assert_called_once()
    # writer instance dry_run=True
    assert cc.call_args.args[0].dry_run is True
    assert cc.call_args.kwargs["name"] == "Test Org"
    assert cc.call_args.kwargs["phone_primary"] == "555"
    out = capsys.readouterr().out
    assert "Created contact" in out
    assert "(dry-run)" in out


def test_contact_create_commits_and_shows_next_step(capsys):
    from tostools.api.tos_writer import TOSWriter

    with (
        patch.object(
            TOSWriter, "create_contact", autospec=True, return_value={"id": 9001}
        ),
        patch.object(TOSWriter, "_ensure_authenticated", autospec=True),
    ):
        rc = tos_main(["contact", "create", "--name", "Test Org", "--no-dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "id_contact=9001" in out
    assert "tos contact assign" in out  # next-step hint
    assert "9001" in out


def test_contact_create_json(capsys):
    import json

    from tostools.api.tos_writer import DryRunResult, TOSWriter

    with patch.object(
        TOSWriter,
        "create_contact",
        autospec=True,
        return_value=DryRunResult("POST", "/contacts", {}),
    ):
        rc = tos_main(
            ["contact", "create", "--name", "X", "--email", "x@y.is", "--json"]
        )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verb"] == "create"
    assert payload["name"] == "X"
    assert payload["fields"]["email"] == "x@y.is"
    assert payload["dry_run"] is True


def test_contact_patch_entity_dry_run(capsys):
    from tostools.api.tos_writer import DryRunResult, TOSWriter

    with patch.object(
        TOSWriter,
        "patch_contact",
        autospec=True,
        return_value=DryRunResult("PUT", "/contact/1256/", {}),
    ) as pc:
        rc = tos_main(["contact", "patch-entity", "1256", "--phone", "9998888"])
    assert rc == 0
    pc.assert_called_once()
    assert pc.call_args.args[1] == 1256
    assert pc.call_args.kwargs == {"phone_primary": "9998888"}
    out = capsys.readouterr().out
    assert "FLEET-GLOBAL" in out
    assert "(dry-run)" in out


def test_contact_patch_entity_requires_a_field(capsys):
    rc = tos_main(["contact", "patch-entity", "1256"])
    assert rc == 2
    assert "at least one field" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --commit — gps-tos-corrections audit trail for contact writes
# ---------------------------------------------------------------------------


def test_contact_remove_commit_records_audit(capsys):
    """`remove <id> --no-dry-run --commit` records a deletion audit (with the
    pre-delete row) in the corrections repo."""
    from tostools.api.tos_writer import TOSWriter

    pre = {"id": 4987, "id_contact": 2483, "id_entity": 4390, "role": "operator"}
    with (
        patch.object(TOSWriter, "_ensure_authenticated", autospec=True),
        patch.object(
            TOSWriter, "get_contact_relationship", autospec=True, return_value=pre
        ),
        patch.object(
            TOSWriter, "delete_contact_relationship", autospec=True, return_value={}
        ),
        patch("tostools.tos._audit_log_contact_change") as audit,
    ):
        rc = tos_main(["contact", "remove", "4987", "--no-dry-run", "--commit"])

    assert rc == 0
    audit.assert_called_once()
    kw = audit.call_args.kwargs
    assert kw["action"] == "contact_relationship_delete"
    assert kw["subdir"] == "deletions"
    assert kw["record"]["id_rel"] == 4987
    assert kw["record"]["deleted"] == pre  # the pre-delete row is captured


def test_contact_commit_without_no_dry_run_warns_and_skips_audit(capsys):
    """`--commit` alone (dry-run) warns and records nothing."""
    from tostools.api.tos_writer import DryRunResult, TOSWriter

    with (
        patch.object(
            TOSWriter,
            "delete_contact_relationship",
            autospec=True,
            return_value=DryRunResult("DELETE", "/x", None),
        ),
        patch("tostools.tos._audit_log_contact_change") as audit,
    ):
        rc = tos_main(["contact", "remove", "4987", "--commit"])

    assert rc == 0
    audit.assert_not_called()
    assert "no effect without --no-dry-run" in capsys.readouterr().err


def test_contact_create_commit_records_audit(capsys):
    from tostools.api.tos_writer import TOSWriter

    with (
        patch.object(TOSWriter, "_ensure_authenticated", autospec=True),
        patch.object(
            TOSWriter, "create_contact", autospec=True, return_value={"id": 9999}
        ),
        patch("tostools.tos._audit_log_contact_change") as audit,
    ):
        rc = tos_main(
            [
                "contact",
                "create",
                "--name",
                "GNSS Operator",
                "--email",
                "gnss-epos@vedur.is",
                "--no-dry-run",
                "--commit",
            ]
        )

    assert rc == 0
    audit.assert_called_once()
    kw = audit.call_args.kwargs
    assert kw["action"] == "contact_create"
    assert kw["subdir"] == "additions"
    assert kw["record"]["id_contact"] == 9999
    assert kw["record"]["name"] == "GNSS Operator"


def test_contact_no_commit_flag_records_nothing(capsys):
    """A live write WITHOUT --commit makes no corrections-repo entry."""
    from tostools.api.tos_writer import TOSWriter

    with (
        patch.object(TOSWriter, "_ensure_authenticated", autospec=True),
        patch.object(
            TOSWriter, "delete_contact_relationship", autospec=True, return_value={}
        ),
        patch("tostools.tos._audit_log_contact_change") as audit,
    ):
        rc = tos_main(["contact", "remove", "4987", "--no-dry-run"])

    assert rc == 0
    audit.assert_not_called()


def test_audit_log_contact_change_appends_and_commits(tmp_path):
    """The helper appends a JSONL record to the right ledger and git-commits it."""
    import json as _json
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)

    with patch("tostools.archive.tos_corrections_dir", return_value=tmp_path):
        from tostools.tos import _audit_log_contact_change

        res = _audit_log_contact_change(
            action="contact_relationship_delete",
            subdir="deletions",
            filename="contact_relationship_deletions.jsonl",
            record={"id_rel": 4987, "deleted": {"id_contact": 2483}},
            commit_msg="deletions: remove relationship 4987",
        )

    assert res["logged"] is True and res["committed"] is True
    ledger = tmp_path / "deletions" / "contact_relationship_deletions.jsonl"
    assert ledger.exists()
    rec = _json.loads(ledger.read_text().strip())
    assert rec["action"] == "contact_relationship_delete"
    assert rec["id_rel"] == 4987 and "ts" in rec
    log = subprocess.run(
        ["git", "-C", str(tmp_path), "log", "--oneline"],
        capture_output=True,
        text=True,
    ).stdout
    assert "remove relationship 4987" in log
