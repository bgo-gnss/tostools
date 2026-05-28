"""Tests for ``tostools.fleet_ops`` — the orchestrator behind
``tos fleet triage`` / ``tos fleet status``.

Scope:

* enumerator: subtype filter, include / exclude / limit semantics,
  empty-result guard
* iterator: side-effect callback receives a mutable result, per-station
  failure does not abort the run
* triage runner: clean stations are skipped by default, included on
  opt-in, file naming matches default_triage_path
* verify runner: exit-code semantics mirror station verify
* renderer + JSON: header surfaces totals, body suppresses clean by
  default
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from tostools.audit_attribute_dates import StationAttributeDateReport
from tostools.audit_missing_attributes import StationMissingAttributesReport
from tostools.fleet_ops import (
    FleetRunSummary,
    FleetStationResult,
    enumerate_fleet_stations,
    fleet_summary_to_dict,
    format_fleet_summary,
    run_fleet_triage,
    run_fleet_verify,
)
from tostools.history import ParentEntity
from tostools.station_triage import StationTriageReport

FROZEN_TS = "2026-05-28T00:00:00Z"


class _FakeClient:
    """Minimal TOSClient stand-in for fleet_ops tests.

    `enumerate_fleet_stations` calls ``client.get_entity_history`` to
    verify each resolved id is a geophysical entity. The real client
    hits the network — we substitute a dict lookup keyed by
    id_entity."""

    def __init__(self, by_id):
        self._by_id = by_id

    def get_entity_history(self, eid):
        p = self._by_id.get(eid)
        if p is None:
            return None
        return {"code_entity_subtype": p.code_subtype}


@contextmanager
def _mock_enumeration(parents):
    """Patch the cfg-read + marker-resolver primitives.

    Tests build a synthetic list of :class:`ParentEntity` rows where
    ``name`` holds the marker (e.g. ``"HEDI"``). This helper wires
    them through ``read_station_markers`` + ``resolve_marker_to_entity_id``
    so the production enumerator runs to completion against the fake
    fleet. Yields the :class:`_FakeClient` so the test can pass it in
    as the ``client`` arg.
    """
    markers = [p.name or "" for p in parents]
    by_marker = {(p.name or "").upper(): p for p in parents}
    by_id = {p.id_entity: p for p in parents}

    def _resolve(_client, marker):
        hit = by_marker.get(marker.upper())
        return hit.id_entity if hit else None

    fake = _FakeClient(by_id)
    with (
        patch(
            "tostools.fleet_ops.default_station_cfg_path",
            return_value="/dev/null",
        ),
        patch("tostools.fleet_ops.read_station_markers", return_value=markers),
        patch(
            "tostools.fleet_ops.resolve_marker_to_entity_id",
            side_effect=_resolve,
        ),
    ):
        yield fake


@contextmanager
def _patched_generators(gen_side_effect):
    """Patch both generate_station_triage + format_station_triage.

    The format renderer expects audit-violation dataclasses; our test
    fixtures use ``["v"] * N`` stub strings to keep
    ``len(violations)`` correct. Patching the renderer too lets us
    test fleet-level wiring without coupling to violation shape (the
    audit modules have their own format-tests)."""
    with (
        patch(
            "tostools.fleet_ops.generate_station_triage",
            side_effect=gen_side_effect,
        ),
        patch(
            "tostools.fleet_ops.format_station_triage",
            side_effect=lambda r: f"<rendered {r.station}>\n",
        ),
    ):
        yield


def _station(id_entity: int, name: str, *, code_subtype: str = "geophysical"):
    return ParentEntity(
        id_entity=id_entity,
        name=name,
        code_subtype=code_subtype,
        role="station" if code_subtype != "area" else "warehouse",
    )


def _clean_report(station: str, station_id: int) -> StationTriageReport:
    return StationTriageReport(
        station=station,
        station_id=station_id,
        generated_at=FROZEN_TS,
        missing=None,
        dates=None,
        rinex=None,
    )


def _findings_report(
    station: str, station_id: int, *, missing: int = 0, dates: int = 0
) -> StationTriageReport:
    """Build a fake report with violations whose only purpose is to
    inflate ``len(violations)``. The format renderer is patched out in
    tests that exercise on-disk side effects (the audit modules have
    their own format tests)."""
    missing_rpt = StationMissingAttributesReport(
        station_id=station_id, station_name=station
    )
    missing_rpt.violations = ["v"] * missing  # type: ignore[list-item]
    dates_rpt = StationAttributeDateReport(station_id=station_id, station_name=station)
    dates_rpt.violations = ["v"] * dates  # type: ignore[list-item]
    return StationTriageReport(
        station=station,
        station_id=station_id,
        generated_at=FROZEN_TS,
        missing=missing_rpt if missing else None,
        dates=dates_rpt if dates else None,
        rinex=None,
    )


# ---------------------------------------------------------------------------
# enumerate_fleet_stations
# ---------------------------------------------------------------------------


def test_enumerate_filters_to_geophysical_subtype():
    """Only ``geophysical`` parents are kept; warehouses + graveyard
    drop out. Prevents fleet runs from accidentally auditing storage
    locations as if they were GNSS sites."""
    parents = [
        _station(1, "RHOF"),
        _station(4, "B9", code_subtype="area"),  # warehouse
        _station(2, "HEDI"),
        _station(14, "discarded", code_subtype="discarded"),
        _station(3, "SAVI"),
    ]
    with _mock_enumeration(parents) as fake_client:
        out = enumerate_fleet_stations(fake_client)  # type: ignore[arg-type]

    assert [p.name for p in out] == ["RHOF", "HEDI", "SAVI"]


def test_enumerate_include_filter_keeps_only_listed_markers():
    parents = [_station(1, "RHOF"), _station(2, "HEDI"), _station(3, "SAVI")]
    with _mock_enumeration(parents) as fake_client:
        out = enumerate_fleet_stations(
            fake_client,  # type: ignore[arg-type]
            include=["hedi", "savi"],  # case-insensitive
        )
    assert [p.name for p in out] == ["HEDI", "SAVI"]


def test_enumerate_exclude_filter_drops_listed_markers():
    parents = [_station(1, "RHOF"), _station(2, "HEDI"), _station(3, "SAVI")]
    with _mock_enumeration(parents) as fake_client:
        out = enumerate_fleet_stations(
            fake_client,  # type: ignore[arg-type]
            exclude=["HEDI"],
        )
    assert [p.name for p in out] == ["RHOF", "SAVI"]


def test_enumerate_limit_caps_post_filter():
    """Limit applies after include/exclude — first N of the filtered
    set, not the raw enumeration."""
    parents = [
        _station(1, "AAA"),
        _station(2, "BBB"),
        _station(3, "CCC"),
        _station(4, "DDD"),
    ]
    with _mock_enumeration(parents) as fake_client:
        out = enumerate_fleet_stations(
            fake_client, exclude=["AAA"], limit=2  # type: ignore[arg-type]
        )
    assert [p.name for p in out] == ["BBB", "CCC"]


def test_enumerate_raises_when_zero_stations_resolve():
    """The infrastructure-only fallback silently leaves zero
    geophysical stations — that's a footgun in fleet ops, so we
    surface it as a RuntimeError with operator guidance."""
    parents = [_station(4, "B9", code_subtype="area")]
    with _mock_enumeration(parents) as fake_client:
        with pytest.raises(RuntimeError, match="zero stations"):
            enumerate_fleet_stations(fake_client)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# run_fleet_triage
# ---------------------------------------------------------------------------


def test_triage_skips_clean_stations_by_default(tmp_path):
    """Clean stations don't produce a file — operators don't want 100+
    empty triage files daily."""
    stations = [_station(1, "RHOF"), _station(2, "HEDI")]
    reports = {
        "RHOF": _clean_report("RHOF", 1),
        "HEDI": _findings_report("HEDI", 2, missing=3),
    }

    with _patched_generators(lambda s, **k: reports[s]):
        summary = run_fleet_triage(
            object(),  # type: ignore[arg-type]
            stations=stations,
            out_dir=tmp_path,
            generated_at=FROZEN_TS,
        )

    rhof_result = next(r for r in summary.results if r.station == "RHOF")
    hedi_result = next(r for r in summary.results if r.station == "HEDI")

    assert rhof_result.status == "clean"
    assert rhof_result.triage_path is None
    assert not (tmp_path / "rhof").exists()

    assert hedi_result.status == "findings"
    assert hedi_result.triage_path is not None
    assert hedi_result.triage_path.exists()
    assert hedi_result.triage_path.parent.name == "hedi"


def test_triage_writes_clean_stations_when_include_clean(tmp_path):
    """--include-clean opts in to the full fleet inventory."""
    stations = [_station(1, "RHOF")]
    with _patched_generators(lambda s, **k: _clean_report("RHOF", 1)):
        summary = run_fleet_triage(
            object(),  # type: ignore[arg-type]
            stations=stations,
            out_dir=tmp_path,
            include_clean=True,
            generated_at=FROZEN_TS,
        )

    result = summary.results[0]
    assert result.status == "clean"
    assert result.triage_path is not None
    assert result.triage_path.exists()


def test_triage_filename_includes_lowercased_station_and_date(tmp_path):
    """Output path is data/triage/<lower>/<lower>_audit_<YYYYMMDD>.txt
    — same convention as the single-station verb. Drives operator
    muscle memory."""
    stations = [_station(1, "HEDI")]
    with _patched_generators(lambda s, **k: _findings_report("HEDI", 1, missing=1)):
        summary = run_fleet_triage(
            object(),  # type: ignore[arg-type]
            stations=stations,
            out_dir=tmp_path,
            generated_at=FROZEN_TS,
        )

    path = summary.results[0].triage_path
    assert path is not None
    assert path.parent.name == "hedi"
    assert path.name.startswith("hedi_audit_")
    assert path.name.endswith(".txt")
    assert len(path.stem.split("_")[-1]) == 8  # YYYYMMDD


def test_triage_continues_after_per_station_failure(tmp_path):
    """One station raising does NOT abort the run — the rest of the
    fleet still gets audited. Failed station shows up as ``failed`` in
    the summary."""
    stations = [_station(1, "RHOF"), _station(2, "HEDI"), _station(3, "SAVI")]

    def _gen(s, **k):
        if s == "HEDI":
            raise RuntimeError("simulated TOS 500")
        return _findings_report(s, k.get("station_id", 1), missing=1)

    with _patched_generators(_gen):
        summary = run_fleet_triage(
            object(),  # type: ignore[arg-type]
            stations=stations,
            out_dir=tmp_path,
            generated_at=FROZEN_TS,
        )

    statuses = {r.station: r.status for r in summary.results}
    assert statuses == {"RHOF": "findings", "HEDI": "failed", "SAVI": "findings"}
    hedi = next(r for r in summary.results if r.station == "HEDI")
    assert hedi.error is not None
    assert "simulated TOS 500" in hedi.error


def test_triage_promotes_to_failed_when_write_raises(tmp_path, monkeypatch):
    """A failing side-effect (write_text IOError, permission denied,
    etc.) must promote the row to ``failed`` without aborting the rest
    of the fleet. Pins the inner side-effect try/except in
    `_iterate_fleet`."""
    stations = [_station(1, "HEDI"), _station(2, "SAVI")]

    def _boom(self, *a, **k):
        raise PermissionError("read-only fs")

    monkeypatch.setattr("pathlib.Path.write_text", _boom)

    with _patched_generators(lambda s, **k: _findings_report(s, 1, missing=1)):
        summary = run_fleet_triage(
            object(),  # type: ignore[arg-type]
            stations=stations,
            out_dir=tmp_path,
            generated_at=FROZEN_TS,
        )

    statuses = {r.station: r.status for r in summary.results}
    assert statuses == {"HEDI": "failed", "SAVI": "failed"}
    hedi = next(r for r in summary.results if r.station == "HEDI")
    assert hedi.error is not None
    assert "read-only fs" in hedi.error
    # Underlying counts preserved despite the write failure — operator
    # can still see what would have been written.
    assert hedi.missing_count == 1


def test_triage_summary_carries_run_kind_and_out_dir(tmp_path):
    stations = [_station(1, "HEDI")]
    with _patched_generators(lambda s, **k: _findings_report("HEDI", 1, missing=1)):
        summary = run_fleet_triage(
            object(),  # type: ignore[arg-type]
            stations=stations,
            out_dir=tmp_path,
            generated_at=FROZEN_TS,
        )
    assert summary.run_kind == "triage"
    assert summary.out_dir == tmp_path
    assert summary.generated_at == FROZEN_TS


# ---------------------------------------------------------------------------
# run_fleet_verify
# ---------------------------------------------------------------------------


def test_verify_exit_code_zero_when_all_clean():
    stations = [_station(1, "RHOF"), _station(2, "HEDI")]
    with patch(
        "tostools.fleet_ops.generate_station_triage",
        side_effect=lambda s, **k: _clean_report(s, 1),
    ):
        summary = run_fleet_verify(
            object(), stations=stations, generated_at=FROZEN_TS  # type: ignore[arg-type]
        )
    assert summary.exit_code() == 0
    assert summary.clean == 2


def test_verify_exit_code_one_when_any_findings():
    stations = [_station(1, "RHOF"), _station(2, "HEDI")]
    reports = {
        "RHOF": _clean_report("RHOF", 1),
        "HEDI": _findings_report("HEDI", 2, dates=2),
    }
    with patch(
        "tostools.fleet_ops.generate_station_triage",
        side_effect=lambda s, **k: reports[s],
    ):
        summary = run_fleet_verify(
            object(), stations=stations, generated_at=FROZEN_TS  # type: ignore[arg-type]
        )
    assert summary.exit_code() == 1
    assert summary.findings == 1


def test_verify_exit_code_two_when_any_failed():
    """A single broken audit promotes the exit code to 2 even if the
    other 172 stations are clean — operator wants to know the oracle
    itself is broken before looking at individual stations."""
    stations = [_station(1, "RHOF"), _station(2, "HEDI")]

    def _gen(s, **k):
        if s == "HEDI":
            raise RuntimeError("audit broken")
        return _clean_report(s, 1)

    with patch("tostools.fleet_ops.generate_station_triage", side_effect=_gen):
        summary = run_fleet_verify(
            object(), stations=stations, generated_at=FROZEN_TS  # type: ignore[arg-type]
        )
    assert summary.exit_code() == 2
    assert summary.failed == 1


def test_verify_writes_nothing_to_disk(tmp_path, monkeypatch):
    """verify must not touch the filesystem — it's the read-only
    oracle half of the apply→verify loop."""
    monkeypatch.chdir(tmp_path)
    stations = [_station(1, "RHOF")]
    with patch(
        "tostools.fleet_ops.generate_station_triage",
        return_value=_findings_report("RHOF", 1, missing=2),
    ):
        run_fleet_verify(
            object(), stations=stations, generated_at=FROZEN_TS  # type: ignore[arg-type]
        )
    # If verify accidentally wrote anything, it'd show up in tmp_path.
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Rendering + JSON
# ---------------------------------------------------------------------------


def _build_summary_for_render() -> FleetRunSummary:
    return FleetRunSummary(
        run_kind="status",
        generated_at=FROZEN_TS,
        results=[
            FleetStationResult(
                station="RHOF",
                station_id=1,
                status="clean",
                findings_count=0,
                missing_count=0,
                dates_count=0,
                rinex_count=0,
            ),
            FleetStationResult(
                station="HEDI",
                station_id=2,
                status="findings",
                findings_count=5,
                missing_count=3,
                dates_count=2,
                rinex_count=0,
            ),
            FleetStationResult(
                station="SAVI",
                station_id=3,
                status="failed",
                findings_count=0,
                missing_count=0,
                dates_count=0,
                rinex_count=0,
                error="TOS lookup 500",
            ),
        ],
    )


def test_format_summary_header_lists_status_counts():
    out = format_fleet_summary(_build_summary_for_render())
    assert "FLEET STATUS" in out
    assert "clean:    1" in out
    assert "findings: 1" in out
    assert "failed:   1" in out
    assert "total findings across fleet: 5" in out


def test_format_summary_suppresses_clean_by_default():
    out = format_fleet_summary(_build_summary_for_render())
    assert "RHOF" not in out  # clean row suppressed
    assert "HEDI" in out
    assert "SAVI" in out


def test_format_summary_show_clean_opt_in_includes_all_rows():
    out = format_fleet_summary(_build_summary_for_render(), show_clean=True)
    assert "RHOF" in out
    assert "HEDI" in out
    assert "SAVI" in out


def test_format_summary_sorts_by_findings_desc():
    summary = _build_summary_for_render()
    # Add a second findings row with fewer findings to verify ordering.
    summary.results.append(
        FleetStationResult(
            station="VOGS",
            station_id=4,
            status="findings",
            findings_count=1,
            missing_count=1,
            dates_count=0,
            rinex_count=0,
        )
    )
    out = format_fleet_summary(summary)
    # HEDI (5 findings) should appear before VOGS (1 finding) in the
    # rendered table. Use string find for ordering.
    assert out.find("HEDI") < out.find("VOGS")


def test_summary_to_dict_includes_exit_code_and_totals():
    payload = fleet_summary_to_dict(_build_summary_for_render())
    assert payload["run_kind"] == "status"
    assert payload["totals"]["total"] == 3
    assert payload["totals"]["clean"] == 1
    assert payload["totals"]["findings"] == 1
    assert payload["totals"]["failed"] == 1
    assert payload["exit_code"] == 2
    assert len(payload["results"]) == 3


def test_summary_to_dict_triage_path_serialized_to_string(tmp_path):
    result = FleetStationResult(
        station="HEDI",
        station_id=2,
        status="findings",
        findings_count=1,
        missing_count=1,
        dates_count=0,
        rinex_count=0,
        triage_path=tmp_path / "data" / "triage" / "hedi" / "hedi_audit_20260528.txt",
    )
    summary = FleetRunSummary(
        run_kind="triage",
        generated_at=FROZEN_TS,
        results=[result],
        out_dir=tmp_path / "data" / "triage",
    )
    payload = fleet_summary_to_dict(summary)
    assert isinstance(payload["out_dir"], str)
    assert isinstance(payload["results"][0]["triage_path"], str)
    assert payload["results"][0]["triage_path"].endswith("hedi_audit_20260528.txt")


# ---------------------------------------------------------------------------
# Progress callback
# ---------------------------------------------------------------------------


def test_progress_callback_fires_once_per_station():
    stations = [_station(1, "RHOF"), _station(2, "HEDI"), _station(3, "SAVI")]
    seen: list[tuple[int, int, str]] = []

    def _cb(idx, total, result):
        seen.append((idx, total, result.station))

    with patch(
        "tostools.fleet_ops.generate_station_triage",
        side_effect=lambda s, **k: _clean_report(s, 1),
    ):
        run_fleet_verify(
            object(),  # type: ignore[arg-type]
            stations=stations,
            generated_at=FROZEN_TS,
            progress=_cb,
        )

    assert seen == [(1, 3, "RHOF"), (2, 3, "HEDI"), (3, 3, "SAVI")]
