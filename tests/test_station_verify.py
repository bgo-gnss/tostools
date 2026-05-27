"""Tests for `tos station verify <STN>` — pass/fail oracle after apply.

Pins the exit-code policy (0 clean / 1 findings / 2 failure) and the
output formats (text + JSON). Verify is a thin wrapper around
:func:`generate_station_triage`, so we patch that to control the report
fed to `_station_verify_main`.
"""

from __future__ import annotations

import json
from argparse import Namespace
from unittest.mock import patch

from tostools.audit_attribute_dates import StationAttributeDateReport
from tostools.audit_missing_attributes import StationMissingAttributesReport
from tostools.station_triage import StationTriageReport
from tostools.tos import _station_verify_main


def _verify_args(**overrides):
    """Namespace mirroring the `tos station verify` argparser defaults."""
    defaults = {
        "station": "HEDI",
        "suppressions": None,
        "no_suppressions": False,
        "catalog": None,
        "json": False,
        "verbose": False,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


def _empty_report(station="HEDI", station_id=4257):
    """Triage report where both audits ran and reported zero violations."""
    return StationTriageReport(
        station=station,
        station_id=station_id,
        generated_at="2026-05-27T00:00:00Z",
        missing=StationMissingAttributesReport(
            station_id=station_id, station_name=station
        ),
        dates=StationAttributeDateReport(station_id=station_id, station_name=station),
        notes=[],
    )


# ---------------------------------------------------------------------------
# Exit-code policy
# ---------------------------------------------------------------------------


def test_verify_exit_0_when_both_audits_clean(capsys):
    """Both audits ran, neither has violations → exit 0."""
    report = _empty_report()
    with patch("tostools.station_triage.generate_station_triage", return_value=report):
        rc = _station_verify_main(_verify_args())

    assert rc == 0
    out = capsys.readouterr().out
    assert "VERIFY HEDI" in out
    assert "clean" in out
    assert "✓" in out


def test_verify_exit_1_when_missing_has_violations(capsys):
    """One audit reports violations → exit 1 (findings)."""
    report = _empty_report()
    report.missing.violations = ["v1", "v2"]  # type: ignore[list-item]
    with patch("tostools.station_triage.generate_station_triage", return_value=report):
        with patch("tostools.tos._print_missing_attributes_report"):
            rc = _station_verify_main(_verify_args())

    assert rc == 1
    out = capsys.readouterr().out
    assert "findings" in out
    assert "✗" in out
    assert "2 finding(s)" in out


def test_verify_exit_1_when_dates_has_violations(capsys):
    """Other audit reports violations → exit 1, symmetric to the
    missing-attributes case."""
    report = _empty_report()
    report.dates.violations = ["v1"]  # type: ignore[list-item]
    with patch("tostools.station_triage.generate_station_triage", return_value=report):
        with patch("tostools.tos._print_attribute_date_report"):
            rc = _station_verify_main(_verify_args())

    assert rc == 1
    out = capsys.readouterr().out
    assert "findings" in out
    assert "1 finding(s)" in out


def test_verify_exit_2_on_audit_failure(capsys):
    """An audit raised (notes non-empty) → exit 2 (failure). Distinct
    from exit 1 so cron / CI can tell "station needs work" from "my
    oracle is broken"."""
    report = _empty_report()
    report.missing = None
    report.notes = ["missing-attributes audit FAILED: timeout"]
    with patch("tostools.station_triage.generate_station_triage", return_value=report):
        rc = _station_verify_main(_verify_args())

    assert rc == 2
    out = capsys.readouterr().out
    assert "failed" in out
    assert "‽" in out
    assert "Audit failures" in out
    assert "timeout" in out


# ---------------------------------------------------------------------------
# Flag passthrough
# ---------------------------------------------------------------------------


def test_verify_passes_no_suppressions_to_generator():
    """`--no-suppressions` flips `use_suppressions=False` on the
    underlying generator call."""
    report = _empty_report()
    with patch(
        "tostools.station_triage.generate_station_triage", return_value=report
    ) as gen:
        _station_verify_main(_verify_args(no_suppressions=True))

    assert gen.call_args.kwargs["use_suppressions"] is False


def test_verify_passes_paths_to_generator(tmp_path):
    """--suppressions and --catalog paths are forwarded verbatim."""
    cat = tmp_path / "catalog.yaml"
    sup = tmp_path / "suppressions.txt"
    report = _empty_report()
    with patch(
        "tostools.station_triage.generate_station_triage", return_value=report
    ) as gen:
        _station_verify_main(_verify_args(catalog=cat, suppressions=sup))

    assert gen.call_args.kwargs["catalog_path"] == cat
    assert gen.call_args.kwargs["suppressions_path"] == sup


# ---------------------------------------------------------------------------
# JSON output schema
# ---------------------------------------------------------------------------


def test_verify_json_schema_clean(capsys):
    """JSON mode emits a stable shape: station, station_id, status,
    exit_code, audits.{missing_attributes,attribute_dates}, notes."""
    report = _empty_report()
    with patch("tostools.station_triage.generate_station_triage", return_value=report):
        rc = _station_verify_main(_verify_args(json=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["station"] == "HEDI"
    assert payload["station_id"] == 4257
    assert payload["status"] == "clean"
    assert payload["exit_code"] == 0
    assert payload["audits"]["missing_attributes"] is not None
    assert payload["audits"]["attribute_dates"] is not None
    assert payload["notes"] == []


def test_verify_json_schema_failed(capsys):
    """When one audit failed, its slot is None and `notes` carries the
    error string. Pinned so downstream JSON consumers can branch on
    `status == "failed"` cleanly."""
    report = _empty_report()
    report.missing = None
    report.notes = ["missing-attributes audit FAILED: boom"]
    with patch("tostools.station_triage.generate_station_triage", return_value=report):
        rc = _station_verify_main(_verify_args(json=True))

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["exit_code"] == 2
    assert payload["audits"]["missing_attributes"] is None
    assert payload["audits"]["attribute_dates"] is not None
    assert payload["notes"] == ["missing-attributes audit FAILED: boom"]
