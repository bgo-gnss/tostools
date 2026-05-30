"""Tests for :mod:`tostools.audit_visit_coverage` (Phase D).

Pins:
  * `audit_station_visit_coverage` rule semantics (covered vs uncovered
    events, cleanup-artifact skip, --since cutoff, SUPPRESS filtering)
  * `format_triage_file` shape — operator-editable, commented ACTIONs
  * Dispatcher integration: `_dispatch_action` for `add-visit` is the
    natural target — tested in test_apply.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tostools.api.tos_client import TOSClient
from tostools.audit_visit_coverage import (
    StationVisitCoverageReport,
    audit_station_visit_coverage,
    format_triage_file,
    load_coverage_suppressions,
)

# ---------------------------------------------------------------------------
# Fixtures — minimal station shape with a couple of devices
# ---------------------------------------------------------------------------


def _station_history(*joins):
    """Build a station entity payload with the given join events.

    Each join is (id_entity_child, time_from, time_to).
    """
    return {
        "id_entity": 4316,
        "code_entity_subtype": "geophysical",
        "attributes": [
            {
                "code": "marker",
                "value": "HEDI",
                "date_to": None,
                "date_from": "2006-06-29",
            },
            {
                "code": "name",
                "value": "Héðinshöfði",
                "date_to": None,
                "date_from": "2006-06-29",
            },
        ],
        "children_connections": [
            {
                "id_entity_child": child,
                "time_from": tf,
                "time_to": tt,
                "id_entity_connection": 9000 + i,
            }
            for i, (child, tf, tt) in enumerate(joins)
        ],
    }


def _device_history(id_entity, subtype="gnss_receiver", serial="?", model="?"):
    return {
        "id_entity": id_entity,
        "code_entity_subtype": subtype,
        "attributes": [
            {
                "code": "serial_number",
                "value": serial,
                "date_to": None,
                "date_from": "2024-01-01",
            },
            {
                "code": "model",
                "value": model,
                "date_to": None,
                "date_from": "2024-01-01",
            },
        ],
    }


def _visit(start_time):
    return {"id": 1, "start_time": f"{start_time}T10:00:00", "completed": True}


# ---------------------------------------------------------------------------
# audit_station_visit_coverage — rule semantics
# ---------------------------------------------------------------------------


def test_audit_covered_event_no_violation():
    """A join with a vitjun within ±7 days is covered → no violation."""
    station = _station_history((20567, "2024-09-18T00:00:00", None))
    visits = [_visit("2024-09-20")]  # 2 days after install — within window

    def fake_get(eid):
        if eid == 4316:
            return station
        return _device_history(eid, subtype="webcamera", serial="X")

    with (
        patch.object(
            TOSClient,
            "basic_search",
            return_value=[
                {
                    "code": "marker",
                    "distance": 0,
                    "value_varchar": "hedi",
                    "type_lvl_two": "stöð",
                    "id_entity": 4316,
                }
            ],
        ),
        patch.object(TOSClient, "get_entity_history", side_effect=fake_get),
        patch.object(TOSClient, "list_maintenance_visits", return_value=visits),
    ):
        report = audit_station_visit_coverage(
            TOSClient(), name="HEDI", since="2020-01-01"
        )

    assert isinstance(report, StationVisitCoverageReport)
    assert report.audited_events == 1
    assert report.violations == []


def test_audit_uncovered_event_becomes_violation():
    """No vitjun within ±7 days → violation surfaced."""
    station = _station_history((20567, "2024-09-18T00:00:00", None))
    visits = [_visit("2024-08-01")]  # 48 days earlier — outside ±7d window

    def fake_get(eid):
        if eid == 4316:
            return station
        return _device_history(
            eid, subtype="webcamera", serial="X", model="Reolink RLC-510A"
        )

    with (
        patch.object(
            TOSClient,
            "basic_search",
            return_value=[
                {
                    "code": "marker",
                    "distance": 0,
                    "value_varchar": "hedi",
                    "type_lvl_two": "stöð",
                    "id_entity": 4316,
                }
            ],
        ),
        patch.object(TOSClient, "get_entity_history", side_effect=fake_get),
        patch.object(TOSClient, "list_maintenance_visits", return_value=visits),
    ):
        report = audit_station_visit_coverage(
            TOSClient(), name="HEDI", since="2020-01-01"
        )

    assert len(report.violations) == 1
    v = report.violations[0]
    assert v.device_id == 20567
    assert v.event_date == "2024-09-18"
    assert v.device_subtype == "webcamera"
    assert "Reolink" in (v.device_label or "")


def test_audit_skips_cleanup_artifact_2014_10_17():
    """The fleet-wide bulk-load date is never audited — those aren't
    real install events."""
    station = _station_history(
        (4830, "2014-10-17T00:00:00", None),  # artifact — skip
        (20567, "2024-09-18T00:00:00", None),  # real — audit
    )

    def fake_get(eid):
        if eid == 4316:
            return station
        return _device_history(eid)

    with (
        patch.object(
            TOSClient,
            "basic_search",
            return_value=[
                {
                    "code": "marker",
                    "distance": 0,
                    "value_varchar": "hedi",
                    "type_lvl_two": "stöð",
                    "id_entity": 4316,
                }
            ],
        ),
        patch.object(TOSClient, "get_entity_history", side_effect=fake_get),
        patch.object(TOSClient, "list_maintenance_visits", return_value=[]),
    ):
        report = audit_station_visit_coverage(
            TOSClient(), name="HEDI", since="2010-01-01"
        )

    # Only the 2024 event got audited; the 2014-10-17 one was skipped.
    assert report.audited_events == 1
    assert len(report.violations) == 1
    assert report.violations[0].event_date == "2024-09-18"


def test_audit_skips_events_before_since():
    """Events older than --since are silently dropped."""
    station = _station_history(
        (4676, "2012-06-27T00:00:00", None),
        (20567, "2024-09-18T00:00:00", None),
    )

    def fake_get(eid):
        if eid == 4316:
            return station
        return _device_history(eid)

    with (
        patch.object(
            TOSClient,
            "basic_search",
            return_value=[
                {
                    "code": "marker",
                    "distance": 0,
                    "value_varchar": "hedi",
                    "type_lvl_two": "stöð",
                    "id_entity": 4316,
                }
            ],
        ),
        patch.object(TOSClient, "get_entity_history", side_effect=fake_get),
        patch.object(TOSClient, "list_maintenance_visits", return_value=[]),
    ):
        report = audit_station_visit_coverage(
            TOSClient(), name="HEDI", since="2020-01-01"
        )

    # 2012 event filtered out by --since 2020.
    assert report.audited_events == 1
    assert all(v.event_date >= "2020-01-01" for v in report.violations)


def test_audit_coverage_window_tunable():
    """A 48-day gap is uncovered at ±7d but covered at ±60d."""
    station = _station_history((20567, "2024-09-18T00:00:00", None))
    visits = [_visit("2024-08-01")]  # 48 days before

    def fake_get(eid):
        if eid == 4316:
            return station
        return _device_history(eid)

    with (
        patch.object(
            TOSClient,
            "basic_search",
            return_value=[
                {
                    "code": "marker",
                    "distance": 0,
                    "value_varchar": "hedi",
                    "type_lvl_two": "stöð",
                    "id_entity": 4316,
                }
            ],
        ),
        patch.object(TOSClient, "get_entity_history", side_effect=fake_get),
        patch.object(TOSClient, "list_maintenance_visits", return_value=visits),
    ):
        narrow = audit_station_visit_coverage(
            TOSClient(),
            name="HEDI",
            since="2020-01-01",
            coverage_window_days=7,
        )
        wide = audit_station_visit_coverage(
            TOSClient(),
            name="HEDI",
            since="2020-01-01",
            coverage_window_days=60,
        )

    assert len(narrow.violations) == 1
    assert wide.violations == []


def test_audit_suppression_silences_specific_event(tmp_path: Path):
    """A `SUPPRESS <device_id> <event_date>` line moves a violation
    from violations → suppressed."""
    station = _station_history((20567, "2024-09-18T00:00:00", None))

    def fake_get(eid):
        if eid == 4316:
            return station
        return _device_history(eid)

    supp = tmp_path / "visit_coverage.txt"
    supp.write_text("SUPPRESS 20567 2024-09-18\n")

    with (
        patch.object(
            TOSClient,
            "basic_search",
            return_value=[
                {
                    "code": "marker",
                    "distance": 0,
                    "value_varchar": "hedi",
                    "type_lvl_two": "stöð",
                    "id_entity": 4316,
                }
            ],
        ),
        patch.object(TOSClient, "get_entity_history", side_effect=fake_get),
        patch.object(TOSClient, "list_maintenance_visits", return_value=[]),
    ):
        report = audit_station_visit_coverage(
            TOSClient(),
            name="HEDI",
            since="2020-01-01",
            suppressions_path=supp,
        )

    assert report.violations == []
    assert len(report.suppressed) == 1
    s = report.suppressed[0]
    assert s.violation.device_id == 20567
    assert s.violation.event_date == "2024-09-18"
    assert s.line_no == 1


# ---------------------------------------------------------------------------
# load_coverage_suppressions — parse semantics
# ---------------------------------------------------------------------------


def test_load_coverage_suppressions_parses_well_formed_lines(tmp_path: Path):
    p = tmp_path / "suppressions.txt"
    p.write_text(
        "# header\n"
        "SUPPRESS 20567 2024-09-18\n"
        "\n"
        "SUPPRESS 4676 2012-06-27  # historical pre-vitjun-era\n"
    )
    out, errs, path = load_coverage_suppressions(p)
    assert errs == []
    assert out == {(20567, "2024-09-18"): 2, (4676, "2012-06-27"): 4}
    assert path == p


def test_load_coverage_suppressions_missing_file_is_silent(tmp_path: Path):
    """The SUPPRESS file is opt-in — file-not-found yields empty, no error."""
    out, errs, _ = load_coverage_suppressions(tmp_path / "nonexistent.txt")
    assert out == {}
    assert errs == []


def test_load_coverage_suppressions_collects_malformed_lines(tmp_path: Path):
    p = tmp_path / "suppressions.txt"
    p.write_text(
        "NOTSUPPRESS 1 2024-01-01\n"
        "SUPPRESS only-one-arg\n"
        "SUPPRESS notanint 2024-01-01\n"
        "SUPPRESS 99 2024-01-01\n"  # good
    )
    out, errs, _ = load_coverage_suppressions(p)
    assert len(errs) == 3
    assert (99, "2024-01-01") in out


# ---------------------------------------------------------------------------
# format_triage_file — emitter shape
# ---------------------------------------------------------------------------


def test_format_triage_file_emits_commented_add_visit_lines():
    """Each violation becomes a commented `add-visit` ACTION line with
    `change` as the default reason and `<FILL_WORK>` as the placeholder."""
    report = StationVisitCoverageReport(
        station_id=4316,
        station_name="Héðinshöfði",
        since="2020-01-01",
        coverage_window_days=7,
        audited_events=2,
    )
    from tostools.audit_visit_coverage import VisitCoverageViolation

    report.violations = [
        VisitCoverageViolation(
            device_id=20567,
            device_subtype="webcamera",
            device_label="X Reolink",
            event_date="2024-09-18",
            coverage_window_days=7,
        ),
        VisitCoverageViolation(
            device_id=4676,
            device_subtype="antenna",
            device_label="antenna-HEDI-20120627 TRM55971.00",
            event_date="2012-06-27",
            coverage_window_days=7,
        ),
    ]

    out = format_triage_file(report, generated_at="2026-05-30T12:00:00+00:00")

    # Header carries station + window + counts.
    assert "Héðinshöfði" in out
    assert "id_entity=4316" in out
    assert "Window:     ±7 days" in out
    assert "Events:     2" in out
    assert "Violations: 2" in out

    # One ACTION line per violation — commented, with FILL_WORK.
    assert "#ACTION 20567 add-visit change 2024-09-18 '<FILL_WORK>'" in out
    assert "#ACTION 4676 add-visit change 2012-06-27 '<FILL_WORK>'" in out
    # SUPPRESS hint accompanies each violation.
    assert "#   SUPPRESS 20567 2024-09-18" in out
    assert "#   SUPPRESS 4676 2012-06-27" in out
    # Devices grouped by id_entity for readability.
    assert "# --- webcamera id_entity=20567" in out
    assert "# --- antenna id_entity=4676" in out


def test_format_triage_file_no_violations_renders_placeholder():
    report = StationVisitCoverageReport(
        station_id=4316,
        station_name="Héðinshöfði",
        since="2020-01-01",
        coverage_window_days=7,
        audited_events=0,
    )
    out = format_triage_file(report, generated_at="2026-05-30T12:00:00+00:00")
    assert "no violations" in out
    assert "#ACTION" not in out


def test_format_triage_file_audit_command_in_header():
    report = StationVisitCoverageReport(
        station_id=4316,
        station_name="HEDI",
        since="2020-01-01",
        coverage_window_days=7,
        audited_events=0,
    )
    out = format_triage_file(
        report,
        audit_command="tos audit visit-coverage HEDI --since 2020-01-01",
        generated_at="2026-05-30T12:00:00+00:00",
    )
    assert "Audit cmd:  tos audit visit-coverage HEDI --since 2020-01-01" in out


# ---------------------------------------------------------------------------
# Orchestrator integration — generate_station_triage with --with-coverage
# ---------------------------------------------------------------------------


def test_generate_station_triage_coverage_opt_in():
    """--with-coverage=True runs the audit; False leaves coverage=None."""
    from tostools.station_triage import generate_station_triage

    station = _station_history((20567, "2024-09-18T00:00:00", None))

    def fake_get(eid):
        if eid == 4316:
            return station
        return _device_history(eid)

    with (
        patch.object(
            TOSClient,
            "basic_search",
            return_value=[
                {
                    "code": "marker",
                    "distance": 0,
                    "value_varchar": "hedi",
                    "type_lvl_two": "stöð",
                    "id_entity": 4316,
                }
            ],
        ),
        patch.object(TOSClient, "get_entity_history", side_effect=fake_get),
        patch.object(TOSClient, "list_maintenance_visits", return_value=[]),
        # Catalog audits need to not blow up on the partial fixture —
        # patch them to return innocuous empty reports.
        patch(
            "tostools.station_triage.audit_station_missing_attributes",
            return_value=type("R", (), {"violations": [], "station_id": 4316})(),
        ),
        patch(
            "tostools.station_triage.audit_station_attribute_dates",
            return_value=type("R", (), {"violations": [], "station_id": 4316})(),
        ),
    ):
        # Without --with-coverage: coverage is None.
        report_no = generate_station_triage("HEDI", generated_at="2026-05-30T12:00:00Z")
        assert report_no.coverage is None

        # With --with-coverage: coverage report populated.
        report_yes = generate_station_triage(
            "HEDI",
            generated_at="2026-05-30T12:00:00Z",
            with_coverage=True,
            coverage_since="2020-01-01",
        )
        assert report_yes.coverage is not None
        assert len(report_yes.coverage.violations) == 1


def test_total_findings_includes_coverage_count():
    """StationTriageReport.total_findings rolls coverage in."""
    from tostools.station_triage import StationTriageReport

    cov_report = StationVisitCoverageReport(
        station_id=4316,
        station_name="HEDI",
        since="2020-01-01",
        coverage_window_days=7,
    )
    # Two violations.
    from tostools.audit_visit_coverage import VisitCoverageViolation

    cov_report.violations = [
        VisitCoverageViolation(
            device_id=1,
            device_subtype="x",
            device_label=None,
            event_date="2024-01-01",
            coverage_window_days=7,
        ),
        VisitCoverageViolation(
            device_id=2,
            device_subtype="x",
            device_label=None,
            event_date="2024-02-01",
            coverage_window_days=7,
        ),
    ]
    report = StationTriageReport(
        station="HEDI",
        station_id=4316,
        generated_at="2026-05-30T12:00:00Z",
        coverage=cov_report,
    )
    assert report.total_findings == 2
