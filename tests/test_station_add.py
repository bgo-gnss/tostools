"""Tests for ``tos station add`` — geophysical station shell + site-join.

Two layers:

1. :mod:`tostools.station` helpers — catalog-driven required set + attribute
   shaping (must-provide vs catalog-defaulted, coordinate validation).
2. The CLI (``tostools.tos._station_add_main``) against a fake writer —
   duplicate-marker guard, find-or-create site (existing / auto-mint /
   --location-id / --create-location conflict), the station create + join,
   dry-run safety, and the orphaned-station-on-join-failure path.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from tostools.station import (
    STATION_SUBTYPE,
    build_required_station_attributes,
    station_required_codes,
)

# ---------------------------------------------------------------------------
# Layer 1 — station.py helpers
# ---------------------------------------------------------------------------


def test_station_subtype_is_geophysical() -> None:
    assert STATION_SUBTYPE == "geophysical"


def test_required_codes_split_must_provide_vs_defaulted() -> None:
    codes = station_required_codes()
    must_provide = {c for c, d in codes.items() if d is None}
    defaulted = {c for c, d in codes.items() if d is not None}
    # Must-provide (no catalog default).
    assert must_provide == {
        "marker",
        "name",
        "lat",
        "lon",
        "altitude",
        "date_start",
        "continuity",
    }
    # Catalog-defaulted (operator can override).
    assert {"subtype", "operational_class", "in_network_epos"} <= defaulted
    assert codes["subtype"] == "GPS stöð"


def _full_provided() -> Dict[str, Optional[str]]:
    return {
        "marker": "TEST",
        "name": "Teststaður",
        "lat": "65.1",
        "lon": "-19.2",
        "altitude": "120",
        "continuity": "continuous",
    }


def test_build_uses_catalog_defaults_for_unprovided() -> None:
    attrs = build_required_station_attributes(
        provided=_full_provided(), date_start="2026-06-08T00:00:00"
    )
    by_code = {a["code"]: a["value"] for a in attrs}
    assert by_code["subtype"] == "GPS stöð"
    assert by_code["operational_class"] == "B"
    assert by_code["in_network_epos"] == "nei"
    assert by_code["marker"] == "test"  # lowercased to the fleet convention
    # date_start attribute carries the date value.
    assert by_code["date_start"] == "2026-06-08T00:00:00"
    # Every row open, anchored at date_start.
    for a in attrs:
        assert a["date_from"] == "2026-06-08T00:00:00"
        assert a["date_to"] is None


def test_build_override_beats_default() -> None:
    prov = _full_provided()
    prov["operational_class"] = "A"
    prov["subtype"] = "SIL stöð"
    attrs = build_required_station_attributes(
        provided=prov, date_start="2026-06-08T00:00:00"
    )
    by_code = {a["code"]: a["value"] for a in attrs}
    assert by_code["operational_class"] == "A"
    assert by_code["subtype"] == "SIL stöð"


def test_build_missing_no_default_reports_all() -> None:
    prov = _full_provided()
    del prov["continuity"]  # no catalog default
    del prov["altitude"]  # no catalog default
    with pytest.raises(ValueError, match="continuity") as exc:
        build_required_station_attributes(
            provided=prov, date_start="2026-06-08T00:00:00"
        )
    assert "altitude" in str(exc.value)


def test_build_validates_coordinates() -> None:
    prov = _full_provided()
    prov["lat"] = "999"
    with pytest.raises(ValueError, match="between -90 and 90"):
        build_required_station_attributes(
            provided=prov, date_start="2026-06-08T00:00:00"
        )


def test_build_lowercases_marker() -> None:
    # TOS stores markers lowercase (e.g. "hedi"); a new station must match the
    # fleet convention so find_station_by_marker resolves it.
    prov = _full_provided()
    prov["marker"] = "HEDI"
    attrs = build_required_station_attributes(
        provided=prov, date_start="2026-06-08T00:00:00"
    )
    by_code = {a["code"]: a["value"] for a in attrs}
    assert by_code["marker"] == "hedi"


def test_built_station_passes_missing_attributes_audit() -> None:
    """The headline claim: a fully-built station shell has zero station-scope
    missing-attribute violations (justifies keying on gps_required_for)."""
    from unittest.mock import MagicMock

    import tostools.audit_missing_attributes as ama

    attrs = build_required_station_attributes(
        provided=_full_provided(), date_start="2026-06-08T00:00:00"
    )
    fake_history = {
        "id_entity": 1,
        "code_entity_subtype": "geophysical",
        "attributes": attrs,
        "children_connections": [],  # no devices yet → no device-scope findings
    }
    client = MagicMock()
    client.get_entity_history.return_value = {}
    with patch.object(ama, "_resolve_station_entity", return_value=fake_history):
        report = ama.audit_station_missing_attributes(
            client, name="Teststaður", use_suppressions=False
        )
    assert report.violations == [], [v.code for v in report.violations]


# ---------------------------------------------------------------------------
# Layer 2 — CLI (_station_add_main)
# ---------------------------------------------------------------------------


class _FakeWriter:
    """Scriptable fake: marker/site lookups + records create / join calls."""

    last_instance: "Optional[_FakeWriter]" = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.dry_run = kwargs.get("dry_run", True)
        self.marker_id: Optional[int] = None  # find_station_by_marker result
        self.land_id: Optional[int] = None  # find_land_location_by_name result
        self.location_history: Dict[int, Dict[str, Any]] = {}
        self.create_calls: List[tuple] = []
        self.join_calls: List[Dict[str, Any]] = []
        # create_entity returns these in order (land first if minted, then
        # station) — default both unknown (dry-run shape).
        self.create_responses: List[Any] = []
        self.join_raises = False
        self.create_raises_on: Optional[str] = None  # subtype that raises
        _FakeWriter.last_instance = self

    def find_station_by_marker(self, marker: str) -> Optional[int]:
        return self.marker_id

    def find_land_location_by_name(self, name: str) -> Optional[int]:
        return self.land_id

    def get_entity_history(self, entity_id: int) -> Dict[str, Any]:
        return self.location_history.get(int(entity_id), {"children_connections": []})

    def create_entity(self, subtype: str, attributes: List[Dict[str, Any]]) -> Any:
        if subtype == self.create_raises_on:
            raise RuntimeError(f"{subtype} create boom")
        self.create_calls.append((subtype, attributes))
        if self.create_responses:
            return self.create_responses.pop(0)
        return {"id_entity": None}

    def create_entity_connection(self, *, id_parent, id_child, time_from) -> Any:
        if self.join_raises:
            raise RuntimeError("join boom")
        rec = {"id_parent": id_parent, "id_child": id_child, "time_from": time_from}
        self.join_calls.append(rec)
        return {"id_connection": 9000, **rec}


def _run_cli(argv: List[str], configure=None) -> int:
    from tostools import location as location_mod
    from tostools import power as power_mod
    from tostools.tos import _station_main

    _FakeWriter.last_instance = None

    class _Configured(_FakeWriter):
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            if configure:
                configure(self)

    with (
        patch("tostools.api.tos_writer.TOSWriter", _Configured),
        patch.object(
            location_mod, "summarize_location_children", lambda w, lid, **k: []
        ),
        patch.object(power_mod, "summarize_site_power", lambda w, lid, **k: []),
    ):
        return _station_main(["add", *argv])


def _base_args() -> List[str]:
    return [
        "--marker",
        "ZZZZ",
        "--name",
        "Teststaður",
        "--lat",
        "65.1",
        "--lon",
        "-19.2",
        "--altitude",
        "120",
        "--date-start",
        "2026-06-08",
        "--continuity",
        "continuous",
    ]


def test_cli_duplicate_marker_refused(capsys) -> None:
    rc = _run_cli(_base_args(), configure=lambda w: setattr(w, "marker_id", 4316))
    assert rc == 1
    assert "already exists" in capsys.readouterr().err
    assert _FakeWriter.last_instance.create_calls == []


def test_cli_force_overrides_duplicate_marker() -> None:
    def cfg(w: _FakeWriter) -> None:
        w.marker_id = 4316
        w.land_id = 4360  # reuse existing site

    rc = _run_cli(_base_args() + ["--force"], configure=cfg)
    assert rc == 0
    # Station create happened despite the existing marker.
    assert any(s == "geophysical" for s, _ in _FakeWriter.last_instance.create_calls)


def test_cli_attaches_to_existing_site_no_site_create(capsys) -> None:
    def cfg(w: _FakeWriter) -> None:
        w.land_id = 4360  # site found by name

    rc = _run_cli(_base_args(), configure=cfg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "already exists" in out
    w = _FakeWriter.last_instance
    # Only the geophysical create — no land create when site is reused.
    subtypes = [s for s, _ in w.create_calls]
    assert subtypes == ["geophysical"]


def test_cli_auto_mints_site_then_station(capsys) -> None:
    # land_id stays None → no site found → mint land, then station.
    rc = _run_cli(_base_args())
    out = capsys.readouterr().out
    assert rc == 0
    w = _FakeWriter.last_instance
    subtypes = [s for s, _ in w.create_calls]
    assert subtypes == ["land", "geophysical"]
    assert "Created station" in out


def test_cli_create_location_conflict_when_name_exists(capsys) -> None:
    def cfg(w: _FakeWriter) -> None:
        w.land_id = 4360

    rc = _run_cli(_base_args() + ["--create-location"], configure=cfg)
    assert rc == 2
    assert "already exists" in capsys.readouterr().err
    # No writes attempted on the conflict.
    assert _FakeWriter.last_instance.create_calls == []


def test_cli_location_id_must_be_land(capsys) -> None:
    def cfg(w: _FakeWriter) -> None:
        w.location_history = {7777: {"code_entity_subtype": "geophysical"}}

    rc = _run_cli(_base_args() + ["--location-id", "7777"], configure=cfg)
    assert rc == 2
    assert "not a `land` site" in capsys.readouterr().err
    assert _FakeWriter.last_instance.create_calls == []


def test_cli_location_id_land_attaches() -> None:
    def cfg(w: _FakeWriter) -> None:
        w.location_history = {
            4360: {"code_entity_subtype": "land", "children_connections": []}
        }

    rc = _run_cli(_base_args() + ["--location-id", "4360"], configure=cfg)
    assert rc == 0
    w = _FakeWriter.last_instance
    assert [s for s, _ in w.create_calls] == ["geophysical"]


def test_cli_live_creates_and_joins() -> None:
    def cfg(w: _FakeWriter) -> None:
        w.land_id = 4360
        w.create_responses = [{"id_entity": 8888}]  # the station

    rc = _run_cli(_base_args() + ["--no-dry-run"], configure=cfg)
    assert rc == 0
    w = _FakeWriter.last_instance
    assert w.join_calls == [
        {"id_parent": 4360, "id_child": 8888, "time_from": "2026-06-08T00:00:00"}
    ]


def test_cli_join_failure_reports_orphaned_station(capsys) -> None:
    def cfg(w: _FakeWriter) -> None:
        w.land_id = 4360
        w.create_responses = [{"id_entity": 8888}]
        w.join_raises = True

    rc = _run_cli(_base_args() + ["--no-dry-run"], configure=cfg)
    assert rc == 1
    err = capsys.readouterr().err
    assert "8888" in err and "create-join" in err


def test_cli_station_create_failure_after_mint_flags_orphan_site(capsys) -> None:
    def cfg(w: _FakeWriter) -> None:
        # No existing site → mint a fresh land site, then the station create
        # raises. The just-minted site is irreversible — must be surfaced.
        w.land_id = None
        w.create_responses = [{"id_entity": 4400}]  # the minted land site
        w.create_raises_on = "geophysical"

    rc = _run_cli(_base_args() + ["--no-dry-run"], configure=cfg)
    assert rc == 1
    err = capsys.readouterr().err
    assert "4400" in err
    assert "--location-id 4400" in err


def test_cli_missing_required_no_default_exits_2(capsys) -> None:
    # Drop --continuity (no catalog default). argparse marks it required, so
    # the parser exits 2 before our code runs.
    argv = [
        "--marker",
        "ZZZZ",
        "--name",
        "X",
        "--lat",
        "65",
        "--lon",
        "-19",
        "--altitude",
        "1",
        "--date-start",
        "2026-06-08",
    ]
    with pytest.raises(SystemExit) as exc:
        _run_cli(argv)
    assert exc.value.code == 2


def test_cli_invalid_date_exits_2(capsys) -> None:
    argv = _base_args()
    argv[argv.index("--date-start") + 1] = "not-a-date"
    rc = _run_cli(argv)
    assert rc == 2
    assert "Invalid --date-start" in capsys.readouterr().err


def test_cli_triage_substitutes_station_id(tmp_path) -> None:
    triage = tmp_path / "onboard.txt"
    triage.write_text("ACTION <STN_ID> create-join 4360 2026-06-08\n", encoding="utf-8")

    def cfg(w: _FakeWriter) -> None:
        w.land_id = 4360
        w.create_responses = [{"id_entity": 8888}]

    rc = _run_cli(
        _base_args()
        + ["--no-dry-run", "--triage", str(triage), "--placeholder", "STN_ID"],
        configure=cfg,
    )
    assert rc == 0
    assert triage.read_text(encoding="utf-8") == (
        "ACTION 8888 create-join 4360 2026-06-08\n"
    )


def test_cli_json_output_shape(capsys) -> None:
    import json

    def cfg(w: _FakeWriter) -> None:
        w.land_id = 4360

    rc = _run_cli(_base_args() + ["--json"], configure=cfg)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["marker"] == "ZZZZ"
    assert payload["site_id"] == 4360
    assert payload["site_reused"] is True
    assert payload["dry_run"] is True
    codes = [a["code"] for a in payload["attributes"]]
    assert "marker" in codes and "operational_class" in codes
