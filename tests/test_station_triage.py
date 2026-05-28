"""Tests for ``tostools.station_triage`` — the orchestrator behind
``tos station triage <STN>``.

Scope:

* aggregator: each sub-report is correctly attached / detected as None
* renderer: section emission is conditional on findings
* renderer: header reflects audit summary + station id
* path helper: default_triage_path follows the documented convention

We mock the underlying audit modules rather than hitting TOS — the
individual audits already have their own tests for content-correctness.
station_triage is tested only at the wiring layer.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tostools.audit_attribute_dates import StationAttributeDateReport
from tostools.audit_missing_attributes import StationMissingAttributesReport
from tostools.station_triage import (
    STATUS_EXIT_CODE,
    STATUS_MARK,
    StationTriageReport,
    _substitute_fill_date_with_start,
    classify_station_triage,
    default_triage_path,
    format_station_triage,
    generate_station_triage,
    now_iso_utc,
)

FROZEN_TS = "2026-05-26T00:00:00Z"


# ---------------------------------------------------------------------------
# default_triage_path
# ---------------------------------------------------------------------------


def test_default_triage_path_format(tmp_path):
    """data/triage/<lower>/<lower>_audit_<YYYYMMDD>.txt under base_dir."""
    p = default_triage_path("HEDI", base_dir=tmp_path)
    assert p.parent.name == "hedi"
    assert p.parent.parent.name == "triage"
    assert p.parent.parent.parent == tmp_path / "data"
    assert p.name.startswith("hedi_audit_")
    assert p.name.endswith(".txt")
    # 8-digit YYYYMMDD slug in the middle.
    assert len(p.stem.split("_")[-1]) == 8
    assert p.stem.split("_")[-1].isdigit()


def test_default_triage_path_lowercases_station():
    """Marker is lowercased for both the directory and filename — keeps
    the on-disk layout case-stable regardless of how the operator types
    the station name on the CLI."""
    p = default_triage_path("RHOF", base_dir=Path("/tmp"))
    assert "rhof" in str(p)
    assert "RHOF" not in str(p)


# ---------------------------------------------------------------------------
# generate_station_triage
# ---------------------------------------------------------------------------


def test_generate_aggregates_both_audits_when_both_succeed(tmp_path):
    """Happy path — both audits return a Report; both are attached to
    the aggregated triage."""
    missing = StationMissingAttributesReport(station_id=4440, station_name="SAVI")
    dates = StationAttributeDateReport(station_id=4440, station_name="SAVI")

    with (
        patch(
            "tostools.station_triage.audit_station_missing_attributes",
            return_value=missing,
        ),
        patch(
            "tostools.station_triage.audit_station_attribute_dates",
            return_value=dates,
        ),
    ):
        report = generate_station_triage(
            "SAVI", client=object(), generated_at=FROZEN_TS
        )

    assert report.station == "SAVI"
    assert report.station_id == 4440
    assert report.missing is missing
    assert report.dates is dates
    assert report.notes == []


def test_generate_records_failure_and_keeps_other_audit(tmp_path):
    """If one audit raises, the failure is captured in ``notes`` but
    the other audit still runs — partial reporting beats total blackout
    when one audit has a bug or a transient TOS error."""
    dates = StationAttributeDateReport(station_id=9999, station_name="HEDI")

    with (
        patch(
            "tostools.station_triage.audit_station_missing_attributes",
            side_effect=RuntimeError("simulated lookup 500"),
        ),
        patch(
            "tostools.station_triage.audit_station_attribute_dates",
            return_value=dates,
        ),
    ):
        report = generate_station_triage(
            "HEDI", client=object(), generated_at=FROZEN_TS
        )

    assert report.missing is None
    assert report.dates is dates
    assert any("missing-attributes audit FAILED" in n for n in report.notes)
    assert any("simulated lookup 500" in n for n in report.notes)
    # station_id resolved via the surviving sub-report.
    assert report.station_id == 9999


def test_generate_returns_station_id_from_first_successful_audit():
    """When both audits succeed, station_id is taken from the
    missing-attributes report (first in resolution order)."""
    missing = StationMissingAttributesReport(station_id=4440, station_name="SAVI")
    dates = StationAttributeDateReport(station_id=4440, station_name="SAVI")
    with (
        patch(
            "tostools.station_triage.audit_station_missing_attributes",
            return_value=missing,
        ),
        patch(
            "tostools.station_triage.audit_station_attribute_dates",
            return_value=dates,
        ),
    ):
        report = generate_station_triage(
            "SAVI", client=object(), generated_at=FROZEN_TS
        )

    assert report.station_id == 4440


def test_total_findings_sums_violations_across_sub_reports():
    """The header's "<N> total finding(s)" derives from
    ``total_findings``. Verify it walks each sub-report's violations
    list and sums correctly."""

    # Use realistic violation objects — the audit modules' dataclasses
    # require specific fields, so easiest is to construct empty reports
    # and patch ``.violations`` directly.
    missing = StationMissingAttributesReport(station_id=1, station_name="X")
    dates = StationAttributeDateReport(station_id=1, station_name="X")
    missing.violations = ["v"] * 7  # type: ignore[list-item]
    dates.violations = ["v"] * 3  # type: ignore[list-item]
    rpt = StationTriageReport(
        station="X",
        station_id=1,
        generated_at=FROZEN_TS,
        missing=missing,
        dates=dates,
    )
    assert rpt.total_findings == 10


def test_total_findings_zero_when_no_sub_reports():
    rpt = StationTriageReport(
        station="X",
        station_id=None,
        generated_at=FROZEN_TS,
        missing=None,
        dates=None,
    )
    assert rpt.total_findings == 0


# ---------------------------------------------------------------------------
# format_station_triage
# ---------------------------------------------------------------------------


def test_format_includes_header_with_station_and_summary():
    """Header carries enough metadata for an operator to identify the
    file without opening it: station name, id, finding count, audit
    summary, run + verify hints."""
    rpt = StationTriageReport(
        station="HEDI",
        station_id=4257,
        generated_at=FROZEN_TS,
        missing=None,
        dates=None,
    )
    out = format_station_triage(rpt)
    assert "HEDI station triage" in out
    assert FROZEN_TS in out
    assert "id_entity=4257" in out
    assert "0 total finding(s)" in out
    assert "missing-attributes:" in out  # audit summary line
    assert "attribute-dates:" in out
    assert "tos audit apply" in out  # run hint
    assert "# Verify (run after --apply lands)" in out  # footer


def test_format_omits_dates_section_when_no_violations():
    """Empty sub-reports → section is dropped from the output. Keeps
    the file from filling with empty headers when a station is partially
    clean."""
    missing = StationMissingAttributesReport(station_id=1, station_name="X")
    dates = StationAttributeDateReport(station_id=1, station_name="X")
    # Both empty.

    rpt = StationTriageReport(
        station="X",
        station_id=1,
        generated_at=FROZEN_TS,
        missing=missing,
        dates=dates,
    )
    out = format_station_triage(rpt)
    assert "suspicious attribute dates" not in out
    assert "missing required attributes" not in out


def test_format_includes_failure_notes_section():
    """Audit-runtime failures captured in ``notes`` get their own
    "Notes" section so operators see why parts of the file are
    incomplete."""
    rpt = StationTriageReport(
        station="X",
        station_id=1,
        generated_at=FROZEN_TS,
        missing=None,
        dates=None,
        notes=["missing-attributes audit FAILED: connection refused"],
    )
    out = format_station_triage(rpt)
    assert "Notes (audit-runtime warnings)" in out
    assert "connection refused" in out


def test_format_renders_dates_section_via_existing_formatter():
    """The dates section delegates to
    ``audit_attribute_dates.format_triage_file`` rather than
    re-implementing. Patch the delegated formatter and verify it's
    invoked + the returned text appears in the output verbatim.

    Why test this: keeps the contract clear — station_triage is a
    composer, not a renderer. Format-stability of the dates section
    lives in test_audit_attribute_dates.py."""
    dates = StationAttributeDateReport(station_id=1, station_name="X")
    dates.violations = ["v"]  # type: ignore[list-item]

    rpt = StationTriageReport(
        station="X",
        station_id=1,
        generated_at=FROZEN_TS,
        missing=None,
        dates=dates,
    )
    with patch(
        "tostools.station_triage.format_dates_triage",
        return_value="--- DATES BODY ---",
    ):
        out = format_station_triage(rpt)

    assert "--- DATES BODY ---" in out
    # And the section header that wraps it.
    assert "Section: suspicious attribute dates" in out


# ---------------------------------------------------------------------------
# _substitute_fill_date_with_start
# ---------------------------------------------------------------------------


def test_substitute_replaces_fill_date_placeholder():
    """`<FILL_DATE>` becomes `start` so the dispatcher's date token
    resolver can substitute the entity's earliest_known at apply-time."""
    body = "#ACTION 4316 add-attribute visit_class B <FILL_DATE>"
    out = _substitute_fill_date_with_start(body)
    assert out == "#ACTION 4316 add-attribute visit_class B start"


def test_substitute_leaves_fill_value_alone():
    """`<FILL_VALUE>` placeholders need operator input — substitution
    must NOT touch them."""
    body = (
        "#ACTION 4316 add-attribute manufacturer <FILL_VALUE> <FILL_DATE>\n"
        "#ACTION 4316 add-attribute description <FILL_VALUE> <FILL_DATE>"
    )
    out = _substitute_fill_date_with_start(body)
    # FILL_DATE swapped, FILL_VALUE preserved.
    assert "<FILL_VALUE>" in out
    assert "<FILL_DATE>" not in out
    assert out.count("start") == 2


def test_substitute_leaves_concrete_dates_alone():
    """Lines with concrete dates already shouldn't be rewritten."""
    body = "#ACTION 4676 add-attribute antenna_height 0.0 2012-06-27"
    out = _substitute_fill_date_with_start(body)
    assert out == body


def test_substitute_swaps_all_occurrences():
    """Multiple <FILL_DATE> in one body all get swapped (idempotent on
    a body that has none)."""
    body = "<FILL_DATE> and <FILL_DATE> and again <FILL_DATE>"
    assert _substitute_fill_date_with_start(body) == "start and start and again start"
    # Empty / no placeholders → no-op.
    assert _substitute_fill_date_with_start("nothing") == "nothing"


# ---------------------------------------------------------------------------
# generate_station_triage — audit kwargs forwarding
# ---------------------------------------------------------------------------


def test_generate_forwards_use_suppressions_to_both_audits():
    """`use_suppressions=False` is passed through to both underlying
    audits. Lets `tos station verify --no-suppressions` actually bypass
    the SUPPRESS files at the audit level."""
    missing = StationMissingAttributesReport(station_id=1, station_name="X")
    dates = StationAttributeDateReport(station_id=1, station_name="X")
    with (
        patch(
            "tostools.station_triage.audit_station_missing_attributes",
            return_value=missing,
        ) as m_missing,
        patch(
            "tostools.station_triage.audit_station_attribute_dates",
            return_value=dates,
        ) as m_dates,
    ):
        generate_station_triage(
            "X", client=object(), generated_at=FROZEN_TS, use_suppressions=False
        )

    assert m_missing.call_args.kwargs["use_suppressions"] is False
    assert m_dates.call_args.kwargs["use_suppressions"] is False


def test_generate_skips_rinex_audit_by_default():
    """`with_archive=False` (the default) keeps the rinex slot None
    AND must NOT call the rinex audit — saves the archive-mount probe
    on offline workflows."""
    missing = StationMissingAttributesReport(station_id=1, station_name="X")
    dates = StationAttributeDateReport(station_id=1, station_name="X")
    with (
        patch(
            "tostools.station_triage.audit_station_missing_attributes",
            return_value=missing,
        ),
        patch(
            "tostools.station_triage.audit_station_attribute_dates",
            return_value=dates,
        ),
        patch("tostools.station_triage.audit_station_verify_from_rinex") as m_rinex,
    ):
        report = generate_station_triage("X", client=object(), generated_at=FROZEN_TS)

    assert report.rinex is None
    m_rinex.assert_not_called()


def test_generate_runs_rinex_audit_when_with_archive_set(tmp_path):
    """`with_archive=True` calls the rinex audit and attaches the
    report under `.rinex`. archive_root / min_gap_days are forwarded."""
    from tostools.audit_verify_from_rinex import StationRinexReport

    missing = StationMissingAttributesReport(station_id=1, station_name="X")
    dates = StationAttributeDateReport(station_id=1, station_name="X")
    rinex = StationRinexReport(
        station="X",
        station_id=1,
        archive_root=tmp_path,
        timeline_count=0,
        first_day=None,
        last_day=None,
    )
    with (
        patch(
            "tostools.station_triage.audit_station_missing_attributes",
            return_value=missing,
        ),
        patch(
            "tostools.station_triage.audit_station_attribute_dates",
            return_value=dates,
        ),
        patch(
            "tostools.station_triage.audit_station_verify_from_rinex",
            return_value=rinex,
        ) as m_rinex,
    ):
        report = generate_station_triage(
            "X",
            client=object(),
            generated_at=FROZEN_TS,
            with_archive=True,
            archive_root=tmp_path,
            min_gap_days=45.0,
        )

    assert report.rinex is rinex
    # Audit kwargs forwarded.
    assert m_rinex.call_args.kwargs["archive_root"] == tmp_path
    assert m_rinex.call_args.kwargs["min_gap_days"] == 45.0


def test_generate_records_rinex_audit_failure_in_notes():
    """Rinex audit raising → captured in `notes`, doesn't block the
    other two audits. Same convention as the missing / dates handlers."""
    missing = StationMissingAttributesReport(station_id=1, station_name="X")
    dates = StationAttributeDateReport(station_id=1, station_name="X")
    with (
        patch(
            "tostools.station_triage.audit_station_missing_attributes",
            return_value=missing,
        ),
        patch(
            "tostools.station_triage.audit_station_attribute_dates",
            return_value=dates,
        ),
        patch(
            "tostools.station_triage.audit_station_verify_from_rinex",
            side_effect=FileNotFoundError("archive root not found"),
        ),
    ):
        report = generate_station_triage(
            "X",
            client=object(),
            generated_at=FROZEN_TS,
            with_archive=True,
        )

    assert report.rinex is None
    assert any("verify-from-rinex audit FAILED" in n for n in report.notes)
    assert any("archive root not found" in n for n in report.notes)


def test_generate_forwards_catalog_and_suppression_paths(tmp_path):
    """`catalog_path` and `suppressions_path` are forwarded to both
    audits. Each audit reads from its own concrete file; passing one
    path is benign for the audit that doesn't load that name."""
    cat = tmp_path / "catalog.yaml"
    sup = tmp_path / "suppressions.txt"
    missing = StationMissingAttributesReport(station_id=1, station_name="X")
    dates = StationAttributeDateReport(station_id=1, station_name="X")
    with (
        patch(
            "tostools.station_triage.audit_station_missing_attributes",
            return_value=missing,
        ) as m_missing,
        patch(
            "tostools.station_triage.audit_station_attribute_dates",
            return_value=dates,
        ) as m_dates,
    ):
        generate_station_triage(
            "X",
            client=object(),
            generated_at=FROZEN_TS,
            catalog_path=cat,
            suppressions_path=sup,
        )

    assert m_missing.call_args.kwargs["catalog_path"] == cat
    assert m_missing.call_args.kwargs["suppressions_path"] == sup
    assert m_dates.call_args.kwargs["catalog_path"] == cat
    assert m_dates.call_args.kwargs["suppressions_path"] == sup


# ---------------------------------------------------------------------------
# classify_station_triage + STATUS_MARK / STATUS_EXIT_CODE
# ---------------------------------------------------------------------------


def test_classify_returns_clean_when_no_findings_no_notes():
    rpt = StationTriageReport(
        station="X",
        station_id=1,
        generated_at=FROZEN_TS,
        missing=None,
        dates=None,
    )
    assert classify_station_triage(rpt) == "clean"


def test_classify_returns_findings_when_any_violations():
    missing = StationMissingAttributesReport(station_id=1, station_name="X")
    missing.violations = ["v"]  # type: ignore[list-item]
    rpt = StationTriageReport(
        station="X",
        station_id=1,
        generated_at=FROZEN_TS,
        missing=missing,
        dates=None,
    )
    assert classify_station_triage(rpt) == "findings"


def test_classify_returns_failed_when_notes_present_even_with_findings():
    """`failed` wins over `findings` so an audit that raised is never
    silently downgraded to 'station needs work'."""
    missing = StationMissingAttributesReport(station_id=1, station_name="X")
    missing.violations = ["v"]  # type: ignore[list-item]
    rpt = StationTriageReport(
        station="X",
        station_id=1,
        generated_at=FROZEN_TS,
        missing=missing,
        dates=None,
        notes=["attribute-dates audit FAILED: lookup 500"],
    )
    assert classify_station_triage(rpt) == "failed"


def test_status_mark_and_exit_code_keys_align():
    """The two maps share the same keys — a status missing from either
    would break the verify oracle / fleet renderer."""
    assert (
        set(STATUS_MARK)
        == set(STATUS_EXIT_CODE)
        == {
            "clean",
            "findings",
            "failed",
        }
    )


def test_status_exit_code_orders_failed_above_findings():
    """Numeric ordering is load-bearing — fleet exit_code() takes the
    max, so failed must outrank findings must outrank clean."""
    assert STATUS_EXIT_CODE["clean"] < STATUS_EXIT_CODE["findings"]
    assert STATUS_EXIT_CODE["findings"] < STATUS_EXIT_CODE["failed"]


def test_now_iso_utc_renders_with_z_suffix_no_microseconds():
    """Triage file headers use this — must be byte-deterministic in
    shape so format-comparison tests don't flake."""
    s = now_iso_utc()
    assert s.endswith("Z")
    assert "+" not in s  # no +00:00 tail
    assert "." not in s  # no microsecond fraction
    # Format: YYYY-MM-DDTHH:MM:SSZ — 20 chars
    assert len(s) == 20
