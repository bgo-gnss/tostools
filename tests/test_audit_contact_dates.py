"""Tests for :mod:`tostools.audit_contact_dates`.

Pins:
  * the migration-artifact rule (non-midnight per_time_from)
  * SUPPRESS filtering (1-tuple key: id_relationship)
  * triage emitter shape (patch-contact-relationship ... time_from start)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tostools.api.tos_client import TOSClient
from tostools.audit_contact_dates import (
    StationContactDatesReport,
    _is_migration_artifact,
    audit_station_contact_dates,
    format_triage_file,
    load_contact_dates_suppressions,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _station(station_id=4390, name="Raufarhöfn"):
    return {
        "id_entity": station_id,
        "code_entity_subtype": "geophysical",
        "attributes": [
            {
                "code": "marker",
                "value": "RHOF",
                "date_to": None,
                "date_from": "2002-01-01",
            },
            {"code": "name", "value": name, "date_to": None, "date_from": "2002-01-01"},
        ],
    }


def _rel(
    id_rel, per_time_from, *, id_contact=1256, name="Veðurstofa Íslands", role="owner"
):
    """A joined contact-relationship row (entity_contacts/{id}/ shape)."""
    return {
        "id_contact_entity_relationship": id_rel,
        "id_contact": id_contact,
        "name": name,
        "organization": name,
        "role": role,
        "per_time_from": per_time_from,
        "per_time_to": None,
    }


def _resolver():
    """Patch the marker→entity resolution to return our station fixture."""
    return patch.object(
        TOSClient,
        "basic_search",
        return_value=[
            {
                "code": "marker",
                "distance": 0,
                "value_varchar": "rhof",
                "type_lvl_two": "stöð",
                "id_entity": 4390,
            }
        ],
    )


# ---------------------------------------------------------------------------
# _is_migration_artifact — the core rule
# ---------------------------------------------------------------------------


def test_is_migration_artifact_non_midnight_true():
    assert _is_migration_artifact("2025-02-04T15:32:38") is True
    assert _is_migration_artifact("2024-11-07T13:19:27") is True


def test_is_migration_artifact_midnight_false():
    assert _is_migration_artifact("2006-06-29T00:00:00") is False


def test_is_migration_artifact_handles_empty_and_dateonly():
    assert _is_migration_artifact(None) is False
    assert _is_migration_artifact("") is False
    assert _is_migration_artifact("2006-06-29") is False  # no T / time component


# ---------------------------------------------------------------------------
# audit_station_contact_dates — rule + suppression
# ---------------------------------------------------------------------------


def test_audit_flags_non_midnight_relationships():
    contacts = [
        _rel(4961, "2024-08-14T09:30:16", role="operator"),
        _rel(
            4987,
            "2024-11-07T13:19:27",
            id_contact=2483,
            name="Benedikt G. Ófeigsson",
            role="operator",
        ),
        _rel(5000, "2002-01-01T00:00:00"),  # midnight — genuine, not flagged
    ]
    with (
        _resolver(),
        patch.object(TOSClient, "get_entity_history", return_value=_station()),
        patch.object(TOSClient, "get_contacts", return_value=contacts),
    ):
        report = audit_station_contact_dates(TOSClient(), name="RHOF")

    assert report.audited_relationships == 3
    assert [v.id_relationship for v in report.violations] == [4961, 4987]
    v = report.violations[0]
    assert v.per_time_from == "2024-08-14T09:30:16"
    assert v.role == "operator"
    assert v.contact_label == "Veðurstofa Íslands"


def test_audit_clean_when_all_midnight():
    contacts = [_rel(5000, "2006-06-29T00:00:00")]
    with (
        _resolver(),
        patch.object(TOSClient, "get_entity_history", return_value=_station()),
        patch.object(TOSClient, "get_contacts", return_value=contacts),
    ):
        report = audit_station_contact_dates(TOSClient(), name="RHOF")
    assert report.audited_relationships == 1
    assert report.violations == []
    assert report.has_violations is False


def test_audit_skips_relationship_without_id():
    """A row missing id_contact_entity_relationship can't be patched → skipped."""
    contacts = [{"per_time_from": "2025-02-04T15:32:38", "id_contact": 1}]  # no id_rel
    with (
        _resolver(),
        patch.object(TOSClient, "get_entity_history", return_value=_station()),
        patch.object(TOSClient, "get_contacts", return_value=contacts),
    ):
        report = audit_station_contact_dates(TOSClient(), name="RHOF")
    assert report.violations == []


def test_audit_suppression_silences_relationship(tmp_path: Path):
    supp = tmp_path / "contact_dates.txt"
    supp.write_text("SUPPRESS 4961\n")
    contacts = [
        _rel(4961, "2024-08-14T09:30:16"),
        _rel(4987, "2024-11-07T13:19:27"),
    ]
    with (
        _resolver(),
        patch.object(TOSClient, "get_entity_history", return_value=_station()),
        patch.object(TOSClient, "get_contacts", return_value=contacts),
    ):
        report = audit_station_contact_dates(
            TOSClient(), name="RHOF", suppressions_path=supp
        )
    assert [v.id_relationship for v in report.violations] == [4987]
    assert len(report.suppressed) == 1
    assert report.suppressed[0].violation.id_relationship == 4961
    assert report.suppressed[0].line_no == 1


# ---------------------------------------------------------------------------
# load_contact_dates_suppressions
# ---------------------------------------------------------------------------


def test_load_suppressions_parses_and_collects_errors(tmp_path: Path):
    p = tmp_path / "s.txt"
    p.write_text(
        "# header\n"
        "SUPPRESS 4961\n"
        "SUPPRESS 4987  # genuine 2024 assignment\n"
        "NOTSUPPRESS 1\n"
        "SUPPRESS notanint\n"
        "SUPPRESS\n"
    )
    out, errs, path = load_contact_dates_suppressions(p)
    assert out == {4961: 2, 4987: 3}
    assert len(errs) == 3


def test_load_suppressions_missing_file_silent(tmp_path: Path):
    out, errs, _ = load_contact_dates_suppressions(tmp_path / "nope.txt")
    assert out == {}
    assert errs == []


# ---------------------------------------------------------------------------
# format_triage_file
# ---------------------------------------------------------------------------


def test_format_triage_emits_patch_relationship_actions():
    report = StationContactDatesReport(
        station_id=4390,
        station_name="Raufarhöfn",
        audited_relationships=3,
    )
    from tostools.audit_contact_dates import ContactDateViolation

    report.violations = [
        ContactDateViolation(
            id_relationship=4961,
            id_contact=1256,
            contact_label="Veðurstofa Íslands",
            role="operator",
            per_time_from="2024-08-14T09:30:16",
        ),
    ]
    out = format_triage_file(report, generated_at="2026-05-31T12:00:00+00:00")

    assert "Raufarhöfn" in out
    assert "id_entity=4390" in out
    # Commented ACTION targeting the STATION id, backdating via `start`.
    assert "#ACTION 4390 patch-contact-relationship 4961 time_from start" in out
    assert "#   SUPPRESS 4961" in out
    # Context comment carries the suspect timestamp + contact label.
    assert "2024-08-14T09:30:16" in out
    assert "Veðurstofa Íslands" in out


def test_format_triage_no_violations_placeholder():
    report = StationContactDatesReport(
        station_id=4390, station_name="RHOF", audited_relationships=1
    )
    out = format_triage_file(report, generated_at="2026-05-31T12:00:00+00:00")
    assert "no violations" in out
    assert "#ACTION" not in out


# ---------------------------------------------------------------------------
# CLI verb — _audit_main dispatch
# ---------------------------------------------------------------------------


def test_cli_contact_dates_exit_codes(capsys):
    from tostools.tos import main as tos_main

    contacts = [_rel(4961, "2024-08-14T09:30:16")]
    with (
        _resolver(),
        patch.object(TOSClient, "get_entity_history", return_value=_station()),
        patch.object(TOSClient, "get_contacts", return_value=contacts),
    ):
        rc = tos_main(["audit", "contact-dates", "RHOF"])
    assert rc == 1  # has violations
    out = capsys.readouterr().out
    assert "VIOLATIONS" in out
    assert "4961" in out


def test_cli_contact_dates_clean_exit_0(capsys):
    from tostools.tos import main as tos_main

    contacts = [_rel(5000, "2002-01-01T00:00:00")]
    with (
        _resolver(),
        patch.object(TOSClient, "get_entity_history", return_value=_station()),
        patch.object(TOSClient, "get_contacts", return_value=contacts),
    ):
        rc = tos_main(["audit", "contact-dates", "RHOF"])
    assert rc == 0


def test_cli_contact_dates_json(capsys):
    import json

    from tostools.tos import main as tos_main

    contacts = [_rel(4961, "2024-08-14T09:30:16")]
    with (
        _resolver(),
        patch.object(TOSClient, "get_entity_history", return_value=_station()),
        patch.object(TOSClient, "get_contacts", return_value=contacts),
    ):
        rc = tos_main(["audit", "contact-dates", "RHOF", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "contact-dates"
    assert payload["station_id"] == 4390
    assert payload["violations"][0]["id_relationship"] == 4961
    assert payload["violations"][0]["per_time_from"] == "2024-08-14T09:30:16"


def test_cli_contact_dates_triage_file(tmp_path: Path, capsys):
    from tostools.tos import main as tos_main

    out_path = tmp_path / "rhof_cts.txt"
    contacts = [_rel(4961, "2024-08-14T09:30:16")]
    with (
        _resolver(),
        patch.object(TOSClient, "get_entity_history", return_value=_station()),
        patch.object(TOSClient, "get_contacts", return_value=contacts),
    ):
        rc = tos_main(["audit", "contact-dates", "RHOF", "--triage", str(out_path)])
    assert rc == 1
    content = out_path.read_text()
    assert "#ACTION 4390 patch-contact-relationship 4961 time_from start" in content
