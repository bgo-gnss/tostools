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
    CurrentReceiverVerdict,
    StationRinexReport,
    TOSReceiverVerdict,
    audit_station_verify_from_rinex,
    classify_current_receiver,
    format_triage_file,
)
from tostools.receiver_timeline import ReceiverHeader

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


# ---------------------------------------------------------------------------
# classify_current_receiver — receiver-LEVEL current-install vs TOS open join
# (todo #40). Pure function, no archive/TOS.
# ---------------------------------------------------------------------------


def test_classify_current_receiver_type_mismatch_emits_dated_command():
    """The OLKE case: TOS open join is a long-retired TRIMBLE 4700 while the
    archive header shows the PolaRX5 deployed since 2017 → dated
    replace-receiver suggestion."""
    rh = ReceiverHeader(serial="3016143", rtype="POLARX5", firmware="5.5.0")
    v = classify_current_receiver(
        "OLKE", rh, "2017-07-08", "TRIMBLE 4700", None, "1.3.0", "2000-10-17"
    )
    assert v.status == "type_mismatch"
    assert v.is_actionable is True
    assert v.suggested_command is not None
    # Dated to the RINEX install proxy, targets the receivers verb, right token.
    assert "receivers cfg replace-receiver" in v.suggested_command
    assert "--station OLKE" in v.suggested_command
    assert "--new-type polarx5" in v.suggested_command
    assert "--new-serial 3016143" in v.suggested_command
    assert "--date 2017-07-08" in v.suggested_command
    # NOT a tos-apply ACTION line.
    assert "ACTION" not in v.suggested_command


def test_classify_current_receiver_normalization_parity_is_clean():
    """The #1 false-positive trap: TOS stores `SEPT POLARX5` / fw `5.5.0`,
    the header says `POLARX5` / `5.50`. Same normalized key ⇒ `ok`, no
    suggestion. With most of the fleet drifting, this parity is the line
    between useful triage and noise."""
    rh = ReceiverHeader(serial="4101525", rtype="POLARX5", firmware="5.50")
    v = classify_current_receiver(
        "ARHO", rh, "2024-01-01", "SEPT POLARX5", "4101525", "5.5.0", "2024-01-01"
    )
    assert v.status == "ok"
    assert v.is_actionable is False
    assert v.suggested_command is None


def test_classify_current_receiver_serial_mismatch_emits_command():
    """Same model, different (both-known) serial → a new physical unit was
    swapped in; replace-receiver with the archive serial + date."""
    rh = ReceiverHeader(serial="4101525", rtype="POLARX5", firmware="5.5.0")
    v = classify_current_receiver(
        "XXXX", rh, "2024-01-01", "SEPT POLARX5", "9999999", "5.5.0", "2020-01-01"
    )
    assert v.status == "serial_mismatch"
    assert v.is_actionable is True
    assert "--new-serial 4101525" in v.suggested_command


def test_classify_current_receiver_firmware_only_drift_is_informational():
    """Type + serial agree, only firmware differs → firmware_drift, NOT a
    replace-receiver (recording firmware changes is todo #39)."""
    rh = ReceiverHeader(serial="4101525", rtype="POLARX5", firmware="5.5.0")
    v = classify_current_receiver(
        "YYYY", rh, "2024-01-01", "SEPT POLARX5", "4101525", "5.4.0", "2024-01-01"
    )
    assert v.status == "firmware_drift"
    assert v.is_actionable is False
    assert v.suggested_command is None


def test_classify_current_receiver_unknown_serial_does_not_false_positive():
    """TOS holds a synthetic/placeholder serial → unknown after normalization;
    a known archive serial must NOT be flagged as a serial_mismatch (can't
    assert a difference against unknown)."""
    rh = ReceiverHeader(serial="4101525", rtype="POLARX5", firmware="5.5.0")
    v = classify_current_receiver(
        "ZZZZ",
        rh,
        "2024-01-01",
        "SEPT POLARX5",
        "receiver-ZZZZ-20240101",
        "5.5.0",
        "2024-01-01",
    )
    assert v.status == "ok"
    assert v.is_actionable is False


def test_classify_current_receiver_no_open_join():
    """No open gnss_receiver join in TOS → informational, no command (adding
    a receiver is a different verb than replace)."""
    rh = ReceiverHeader(serial="4101525", rtype="POLARX5", firmware="5.5.0")
    v = classify_current_receiver("WWWW", rh, "2024-01-01", None, None, None, None)
    assert v.status == "no_open_join"
    assert v.is_actionable is False


def test_classify_current_receiver_no_rinex_identity():
    """Archive header with no usable identity → no_rinex_receiver, never a
    command (we have nothing to suggest)."""
    rh = ReceiverHeader(serial=None, rtype=None, firmware=None)
    v = classify_current_receiver(
        "VVVV", rh, "2024-01-01", "SEPT POLARX5", "4101525", "5.5.0", "2024-01-01"
    )
    assert v.status == "no_rinex_receiver"
    assert v.is_actionable is False


def test_classify_current_receiver_unmapped_type_uses_placeholder():
    """A type with no replace-receiver token (e.g. mosaic-X5) still emits a
    suggestion, with a `<TYPE?>` placeholder for the operator to fill."""
    rh = ReceiverHeader(serial="3001234", rtype="mosaic-X5", firmware="4.14.0")
    v = classify_current_receiver(
        "BLAL", rh, "2023-05-01", "SEPT POLARX2", None, None, "2010-01-01"
    )
    assert v.status == "type_mismatch"
    assert "--new-type <TYPE?>" in v.suggested_command


# ---------------------------------------------------------------------------
# StationRinexReport — current_receiver wiring into has_findings/finding_count
# ---------------------------------------------------------------------------


def _actionable_cr():
    return CurrentReceiverVerdict(
        status="type_mismatch",
        detail="TOS TRIMBLE 4700 but archive POLARX5 since 2017-07-08",
        rinex_install_date="2017-07-08",
        suggested_command=(
            "receivers cfg replace-receiver --station OLKE --new-type polarx5 "
            "--new-serial 3016143 --date 2017-07-08"
        ),
    )


def test_has_findings_current_receiver_alone():
    """An actionable receiver-swap verdict escalates the oracle on its own."""
    report = StationRinexReport(
        station="OLKE",
        station_id=1,
        archive_root=Path("/tmp/archive"),
        timeline_count=10,
        first_day="2000-01-01",
        last_day="2026-06-20",
        current_receiver=_actionable_cr(),
    )
    assert report.has_findings is True
    assert report.finding_count == 1


def test_finding_count_firmware_drift_does_not_count():
    """firmware_drift is informational — must not inflate finding_count."""
    cr = CurrentReceiverVerdict(status="firmware_drift", detail="fw differs")
    report = StationRinexReport(
        station="X",
        station_id=1,
        archive_root=Path("/tmp/archive"),
        timeline_count=10,
        first_day="2020-01-01",
        last_day="2020-01-10",
        current_receiver=cr,
    )
    assert report.has_findings is False
    assert report.finding_count == 0


def test_format_triage_file_renders_swap_as_manual_command_not_action():
    """The swap suggestion renders as a run-manually command, distinct from
    the `tos audit apply` ACTION grammar (advisor trap #2)."""
    report = StationRinexReport(
        station="OLKE",
        station_id=1,
        archive_root=Path("/mnt/archive"),
        timeline_count=3000,
        first_day="2000-01-01",
        last_day="2026-06-20",
        current_receiver=_actionable_cr(),
    )
    text = format_triage_file(report, generated_at="2026-06-20T00:00:00Z")
    assert "Current receiver (type_mismatch" in text
    assert "receivers cfg replace-receiver --station OLKE" in text
    assert "run MANUALLY" in text
    # Must NOT claim "no discrepancies" when a swap is suggested.
    assert "No actionable TOS-vs-archive discrepancies" not in text
