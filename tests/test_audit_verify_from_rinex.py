"""Tests for `tostools.audit_verify_from_rinex` — the data-collection
layer behind `tos audit verify-from-rinex` and the rinex section of
`tos station triage` / `tos station verify --with-archive`.

The unit-level helpers (`infer_expected_family`,
`classify_tos_join_against_archive`) are pinned by `test_archive.py`
already. This file covers the *report* shape and the triage-file
formatter — what station_triage.py consumes downstream.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

from tostools.api.tos_client import TOSClient
from tostools.archive import BrandTransition, DataGap, RinexOnlySpan
from tostools.audit_verify_from_rinex import (
    StationRinexReport,
    TOSReceiverVerdict,
    audit_station_verify_from_rinex,
    format_triage_file,
)

# ---------------------------------------------------------------------------
# StationRinexReport.has_findings
# ---------------------------------------------------------------------------


def test_has_findings_brand_transitions_alone():
    """A brand transition is always a finding — real hardware change
    deserves operator review."""
    report = StationRinexReport(
        station="X",
        station_id=1,
        archive_root=Path("/tmp/archive"),
        timeline_count=10,
        first_day="2020-01-01",
        last_day="2020-01-10",
        brand_transitions=[
            BrandTransition(
                date_before=date(2020, 1, 5),
                date_after=date(2020, 1, 6),
                family_before="septentrio",
                family_after="trimble_netr9",
                file_before=Path("/dummy/before.sbf"),
                file_after=Path("/dummy/after.T02"),
            )
        ],
    )
    assert report.has_findings is True


def test_has_findings_data_gaps_alone():
    """A multi-day gap also always counts — dormant period needs a
    human read."""
    report = StationRinexReport(
        station="X",
        station_id=1,
        archive_root=Path("/tmp/archive"),
        timeline_count=10,
        first_day="2020-01-01",
        last_day="2020-01-10",
        data_gaps=[
            DataGap(
                last_day_with_data=date(2020, 1, 5),
                next_day_with_data=date(2020, 3, 1),
                duration_days=55,
            )
        ],
    )
    assert report.has_findings is True


def test_has_findings_actionable_verdict():
    """Receivers contribute only when they carry a suggested_action —
    clean / informational statuses don't escalate."""
    report = StationRinexReport(
        station="X",
        station_id=1,
        archive_root=Path("/tmp/archive"),
        timeline_count=10,
        first_day="2020-01-01",
        last_day="2020-01-10",
        receivers=[
            TOSReceiverVerdict(
                id_entity=1,
                serial="A",
                model="TRIMBLE NETR9",
                time_from="2020-01-01",
                time_to=None,
                id_connection=10,
                expected_family="trimble_netr9",
                status="join_too_wide",
                detail="bulk-load placeholder",
                suggested_action="ACTION 1 patch-join-date 10 time_from 2020-02-01",
            )
        ],
        suggested_actions=["ACTION 1 patch-join-date 10 time_from 2020-02-01"],
    )
    assert report.has_findings is True


def test_has_findings_clean_receivers_only():
    """Receivers with `ok` / `unmapped_model` / `rinex_only` verdicts
    don't trigger the oracle — they're informational, not actionable."""
    report = StationRinexReport(
        station="X",
        station_id=1,
        archive_root=Path("/tmp/archive"),
        timeline_count=10,
        first_day="2020-01-01",
        last_day="2020-01-10",
        receivers=[
            TOSReceiverVerdict(
                id_entity=1,
                serial="A",
                model="TRIMBLE NETR9",
                time_from="2020-01-01",
                time_to=None,
                id_connection=10,
                expected_family="trimble_netr9",
                status="ok",
                detail="archive trimble_netr9 throughout",
                suggested_action=None,
            )
        ],
    )
    assert report.has_findings is False


def test_has_findings_empty_report():
    """Empty timeline + no findings → clean."""
    report = StationRinexReport(
        station="X",
        station_id=None,
        archive_root=Path("/tmp/archive"),
        timeline_count=0,
        first_day=None,
        last_day=None,
    )
    assert report.has_findings is False


# ---------------------------------------------------------------------------
# audit_station_verify_from_rinex — empty timeline path
# ---------------------------------------------------------------------------


def test_audit_returns_empty_report_when_no_archive_days():
    """Station with no archived data: short-circuit before doing the
    expensive TOS resolution + child enumeration. Caller checks
    `timeline_count == 0`."""
    client = TOSClient()

    with (
        patch(
            "tostools.archive.cold_archive_prepath",
            return_value=Path("/tmp/archive"),
        ),
        patch("tostools.archive.walk_station_timeline", return_value=iter([])),
    ):
        report = audit_station_verify_from_rinex(client, "ZZZZ")

    assert report.timeline_count == 0
    assert report.first_day is None
    assert report.last_day is None
    assert report.receivers == []
    assert report.has_findings is False


# ---------------------------------------------------------------------------
# format_triage_file
# ---------------------------------------------------------------------------


def test_format_triage_file_empty_report_says_clean():
    """When the report has zero findings, the formatter emits a
    'No actionable …' line so operators see the section ran."""
    report = StationRinexReport(
        station="HEDI",
        station_id=4257,
        archive_root=Path("/mnt/archive"),
        timeline_count=3000,
        first_day="2006-06-29",
        last_day="2026-05-28",
    )
    text = format_triage_file(report, generated_at="2026-05-28T00:00:00Z")

    assert "Section: archive verification — HEDI" in text
    assert "Archive root: /mnt/archive" in text
    assert "Timeline: 3000 archived day(s)" in text
    # No actionable lines means the placeholder fires.
    assert "No actionable TOS-vs-archive discrepancies found" in text


def test_format_triage_file_renders_brand_transitions_as_comments():
    """Brand transitions are informational — operators decide whether
    to insert new TOS joins. Rendered as `#` lines, never as ACTION."""
    report = StationRinexReport(
        station="HEDI",
        station_id=4257,
        archive_root=Path("/mnt/archive"),
        timeline_count=3000,
        first_day="2006-06-29",
        last_day="2026-05-28",
        brand_transitions=[
            BrandTransition(
                date_before=date(2014, 10, 16),
                date_after=date(2014, 10, 17),
                family_before="septentrio",
                family_after="trimble_netr9",
                file_before=Path("/dummy/before.sbf"),
                file_after=Path("/dummy/after.T02"),
            )
        ],
    )
    text = format_triage_file(report, generated_at="2026-05-28T00:00:00Z")
    assert "Brand transitions (1 — real hardware changes" in text
    assert "2014-10-16 (septentrio) → 2014-10-17 (trimble_netr9)" in text
    # No bare-ACTION lines should appear for transitions — they're
    # informational only.
    assert "ACTION " not in text


def test_format_triage_file_renders_data_gaps_as_comments():
    """Multi-day gaps surface as informational `#` lines too."""
    report = StationRinexReport(
        station="HEDI",
        station_id=4257,
        archive_root=Path("/mnt/archive"),
        timeline_count=3000,
        first_day="2006-06-29",
        last_day="2026-05-28",
        data_gaps=[
            DataGap(
                last_day_with_data=date(2014, 7, 8),
                next_day_with_data=date(2016, 7, 2),
                duration_days=725,
            )
        ],
    )
    text = format_triage_file(report, generated_at="2026-05-28T00:00:00Z")
    assert "Data gaps (1 ≥30d" in text
    assert "2014-07-08 → 2016-07-02  (725d)" in text


def test_format_triage_file_emits_commented_actions():
    """``suggested_actions`` flow through as commented `#ACTION` lines
    — operator opts in by removing the leading `#`."""
    report = StationRinexReport(
        station="SAVI",
        station_id=4440,
        archive_root=Path("/mnt/archive"),
        timeline_count=3000,
        first_day="2006-06-29",
        last_day="2026-05-28",
        suggested_actions=[
            "ACTION 4830 patch-join-date 5942 time_from 2007-08-08  # was 2014-10-17",
        ],
    )
    text = format_triage_file(report, generated_at="2026-05-28T00:00:00Z")
    assert "Suggested ACTION lines (1 — uncomment to apply)" in text
    # Leading `#` ensures dry-run is the default — must be present.
    assert "#ACTION 4830 patch-join-date 5942 time_from 2007-08-08" in text


def test_format_triage_file_rinex_only_spans():
    """RINEX-only spans (raw missing) get their own informational
    section so the 'we're losing raw' signal is visible alongside
    actual ACTIONs."""
    report = StationRinexReport(
        station="HEDI",
        station_id=4257,
        archive_root=Path("/mnt/archive"),
        timeline_count=3000,
        first_day="2006-06-29",
        last_day="2026-05-28",
        rinex_only_spans=[
            RinexOnlySpan(start=date(2018, 1, 1), end=date(2018, 3, 1), days=60)
        ],
    )
    text = format_triage_file(report, generated_at="2026-05-28T00:00:00Z")
    assert "RINEX-only spans (1 — raw missing)" in text
    assert "2018-01-01 → 2018-03-01  (60d)" in text
