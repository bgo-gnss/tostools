"""Tests for ``tos location add`` — the ``land`` site find-or-create verb.

Three layers:

1. :mod:`tostools.location` helpers — coordinate validation, attribute
   shaping, child summary.
2. :meth:`TOSWriter.find_land_location_by_name` — the idempotent reuse lookup.
3. The CLI (``tostools.tos._location_main`` / ``_location_add_main``) — wired
   against a fake writer, exercising the reuse / create / --force / validation
   / triage-handoff paths.

The driving domain fact (operator-confirmed): a new GPS station is usually
colocated at a site that already exists because a SIL seismic station is
there — so "location already exists" is the COMMON, idempotent path, not an
error.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from tostools.api.tos_writer import TOSWriter
from tostools.location import (
    LOCATION_OPTIONAL_ATTR_CODES,
    build_location_attributes,
    summarize_location_children,
    validate_altitude,
    validate_latitude,
    validate_longitude,
)

# ---------------------------------------------------------------------------
# Layer 1 — location.py helpers
# ---------------------------------------------------------------------------


def test_validate_latitude_accepts_in_range() -> None:
    assert validate_latitude("66.0807") == "66.0807"
    assert validate_latitude("-90") == "-90"
    assert validate_latitude("90") == "90"


def test_validate_latitude_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="between -90 and 90"):
        validate_latitude("90.1")


def test_validate_longitude_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="between -180 and 180"):
        validate_longitude("181")


def test_validate_coordinate_rejects_non_numeric() -> None:
    with pytest.raises(ValueError, match="lat must be numeric"):
        validate_latitude("north")
    with pytest.raises(ValueError, match="altitude must be numeric"):
        validate_altitude("high")


def test_validate_altitude_accepts_negative() -> None:
    # Below sea level is legitimate.
    assert validate_altitude("-12.5") == "-12.5"


def test_build_location_attributes_required_shape() -> None:
    attrs = build_location_attributes(
        name="Héðinshöfði",
        lat="66.0807",
        lon="-17.3095",
        altitude="143",
        date_start="2026-06-07T00:00:00",
    )
    codes = [a["code"] for a in attrs]
    assert codes == ["name", "lat", "lon", "altitude"]
    for a in attrs:
        assert a["date_from"] == "2026-06-07T00:00:00"
        assert a["date_to"] is None


def test_build_location_attributes_includes_optionals_in_order() -> None:
    attrs = build_location_attributes(
        name="X",
        lat="65",
        lon="-19",
        altitude="10",
        date_start="2026-01-01T00:00:00",
        notes="hi",
        identifier="TST",
    )
    codes = [a["code"] for a in attrs]
    # Optionals appended in LOCATION_OPTIONAL_ATTR_CODES order, not kwarg order.
    assert codes == ["name", "lat", "lon", "altitude", "identifier", "notes"]
    assert all(
        c in (("name", "lat", "lon", "altitude") + LOCATION_OPTIONAL_ATTR_CODES)
        for c in codes
    )


def test_build_location_attributes_omits_blank_optionals() -> None:
    attrs = build_location_attributes(
        name="X",
        lat="65",
        lon="-19",
        altitude="10",
        date_start="2026-01-01T00:00:00",
        identifier="",
        notes=None,
    )
    assert [a["code"] for a in attrs] == ["name", "lat", "lon", "altitude"]


def test_build_location_attributes_empty_name_raises() -> None:
    with pytest.raises(ValueError, match="name must be a non-empty"):
        build_location_attributes(
            name="  ",
            lat="65",
            lon="-19",
            altitude="10",
            date_start="2026-01-01T00:00:00",
        )


def test_build_location_attributes_invalid_coord_raises() -> None:
    with pytest.raises(ValueError, match="between -90 and 90"):
        build_location_attributes(
            name="X",
            lat="999",
            lon="-19",
            altitude="10",
            date_start="2026-01-01T00:00:00",
        )


class _FakeHistoryWriter:
    """Minimal writer exposing ``get_entity_history`` for summary tests."""

    def __init__(self, histories: Dict[int, Dict[str, Any]]) -> None:
        self._histories = histories

    def get_entity_history(self, entity_id: int) -> Optional[Dict[str, Any]]:
        return self._histories.get(int(entity_id))


def _attr(code: str, value: str) -> Dict[str, Any]:
    return {"code": code, "value": value, "date_from": "2000", "date_to": None}


def test_summarize_location_children_resolves_subtype_and_name() -> None:
    histories = {
        4315: {
            "children_connections": [
                {"id_entity_child": 4316, "time_from": "2006", "time_to": None},
                {"id_entity_child": 5442, "time_from": "2000", "time_to": None},
            ]
        },
        4316: {
            "code_entity_subtype": "geophysical",
            "attributes": [_attr("subtype", "GPS stöð"), _attr("name", "Héð")],
        },
        5442: {
            "code_entity_subtype": "geophysical",
            "attributes": [_attr("subtype", "SIL stöð"), _attr("name", "Héð")],
        },
    }
    children = summarize_location_children(_FakeHistoryWriter(histories), 4315)
    # Sorted most-recent time_from first → GPS (2006) before SIL (2000).
    assert [c["subtype"] for c in children] == ["GPS stöð", "SIL stöð"]
    assert children[0]["id_entity"] == 4316
    assert children[1]["code_entity_subtype"] == "geophysical"
    assert all(c["open"] for c in children)


def test_summarize_location_children_open_only_filters_closed() -> None:
    histories = {
        1: {
            "children_connections": [
                {"id_entity_child": 2, "time_from": "2000", "time_to": "2005"},
                {"id_entity_child": 3, "time_from": "2006", "time_to": None},
            ]
        },
        2: {"code_entity_subtype": "geophysical", "attributes": []},
        3: {"code_entity_subtype": "geophysical", "attributes": []},
    }
    open_only = summarize_location_children(_FakeHistoryWriter(histories), 1)
    assert [c["id_entity"] for c in open_only] == [3]
    both = summarize_location_children(
        _FakeHistoryWriter(histories), 1, open_only=False
    )
    assert {c["id_entity"] for c in both} == {2, 3}


def test_summarize_location_children_empty_history() -> None:
    assert summarize_location_children(_FakeHistoryWriter({}), 999) == []


# ---------------------------------------------------------------------------
# Layer 2 — TOSWriter.find_land_location_by_name
# ---------------------------------------------------------------------------


def _writer() -> TOSWriter:
    return TOSWriter(dry_run=True, username="u", password="p")


def _name_hit(entity_id: int, name: str, id_lvl_two: Optional[int]) -> dict:
    return {
        "code": "name",
        "value_varchar": name,
        "distance": 0,
        "id_entity": entity_id,
        "id_lvl_two": id_lvl_two,
    }


def test_find_land_location_returns_lvl_one_confirmed_land() -> None:
    w = _writer()
    search = [
        _name_hit(4316, "Héðinshöfði", id_lvl_two=4316),  # station
        _name_hit(4315, "Héðinshöfði", id_lvl_two=None),  # the land site
    ]
    land_history = {"code_entity_subtype": "land"}
    with patch.object(w, "_request") as req:
        # 1: basic_search, 2: confirm history for the lvl-one candidate (4315)
        req.side_effect = [search, land_history]
        assert w.find_land_location_by_name("Héðinshöfði") == 4315
    # The first confirm GET should target the lvl-one hit, not the station.
    assert req.call_args_list[1].args[1] == "/history/entity/4315/"


def test_find_land_location_returns_none_when_only_station_matches() -> None:
    w = _writer()
    search = [_name_hit(4316, "Héðinshöfði", id_lvl_two=4316)]
    station_history = {"code_entity_subtype": "geophysical"}
    with patch.object(w, "_request") as req:
        req.side_effect = [search, station_history]
        assert w.find_land_location_by_name("Héðinshöfði") is None


def test_find_land_location_none_on_empty_name() -> None:
    w = _writer()
    with patch.object(w, "_request") as req:
        assert w.find_land_location_by_name("") is None
        req.assert_not_called()


def test_find_land_location_none_on_no_hits() -> None:
    w = _writer()
    with patch.object(w, "_request", return_value=[]):
        assert w.find_land_location_by_name("Nowhere") is None


def test_find_land_location_skips_distance_and_value_mismatch() -> None:
    w = _writer()
    search = [
        {
            "code": "name",
            "value_varchar": "Other",
            "distance": 0,
            "id_lvl_two": None,
            "id_entity": 1,
        },
        {
            "code": "name",
            "value_varchar": "Place",
            "distance": 3,
            "id_lvl_two": None,
            "id_entity": 2,
        },
    ]
    with patch.object(w, "_request", return_value=search) as req:
        assert w.find_land_location_by_name("Place") is None
        # Only the basic_search call — no candidate cleared the filters.
        assert req.call_count == 1


# ---------------------------------------------------------------------------
# Layer 3 — CLI (_location_main / _location_add_main)
# ---------------------------------------------------------------------------


class _FakeWriter:
    """Records create_entity calls; scripts find/summary results."""

    last_instance: "Optional[_FakeWriter]" = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.dry_run = kwargs.get("dry_run", True)
        self.existing_id: Optional[int] = None
        self.children: List[Dict[str, Any]] = []
        self.create_calls: List[tuple] = []
        self.create_response: Any = {"id_entity": None}
        _FakeWriter.last_instance = self

    def find_land_location_by_name(self, name: str) -> Optional[int]:
        return self.existing_id

    def get_entity_history(self, entity_id: int) -> Dict[str, Any]:
        return {"children_connections": []}

    def create_entity(self, subtype: str, attributes: List[Dict[str, Any]]) -> Any:
        self.create_calls.append((subtype, attributes))
        return self.create_response


def _run_cli(argv: List[str], configure=None) -> int:
    """Run ``tos location <argv>`` with TOSWriter replaced by _FakeWriter.

    *configure* is an optional callback invoked with the fake instance right
    after construction, so a test can set existing_id / children / response.
    Patched via a subclass so per-instance config lands before the CLI uses it.
    """
    from tostools import location as location_mod
    from tostools.tos import _location_main

    # Reset so a test can assert "writer never constructed" (validation that
    # fails before TOSWriter() leaves this None).
    _FakeWriter.last_instance = None
    created: Dict[str, Any] = {}

    class _Configured(_FakeWriter):
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            created["w"] = self
            if configure:
                configure(self)

    with (
        patch("tostools.api.tos_writer.TOSWriter", _Configured),
        patch.object(
            location_mod,
            "summarize_location_children",
            lambda w, lid, **kw: getattr(w, "children", []),
        ),
    ):
        return _location_main(argv)


def _base_args() -> List[str]:
    return [
        "add",
        "--name",
        "Teststaður",
        "--lat",
        "65.1",
        "--lon",
        "-19.2",
        "--altitude",
        "120",
        "--date-start",
        "2026-06-07",
    ]


def test_cli_reuse_existing_skips_create(capsys) -> None:
    def cfg(w: _FakeWriter) -> None:
        w.existing_id = 4315
        w.children = [
            {
                "id_entity": 5442,
                "subtype": "SIL stöð",
                "code_entity_subtype": "geophysical",
                "name": "Héð",
                "time_from": "2000",
                "open": True,
            },
        ]

    rc = _run_cli(_base_args(), configure=cfg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "already exists" in out
    assert "SIL stöð" in out
    assert "tos station add --location-id 4315" in out
    # No create on the reuse path.
    assert _FakeWriter.last_instance is not None
    assert _FakeWriter.last_instance.create_calls == []


def test_cli_create_path_warns_exact_match_and_irreversible(capsys) -> None:
    # existing_id stays None → create path emits the spelling / no-delete
    # caution to stderr so the operator looks before --no-dry-run.
    rc = _run_cli(_base_args())
    err = capsys.readouterr().err
    assert rc == 0
    assert "exact" in err.lower()
    assert "irreversible" in err.lower()


def test_cli_create_new_dry_run(capsys) -> None:
    rc = _run_cli(_base_args())  # existing_id stays None → create
    out = capsys.readouterr().out
    assert rc == 0
    assert "would be assigned" in out
    assert "(dry-run)" in out
    w = _FakeWriter.last_instance
    assert w is not None and len(w.create_calls) == 1
    subtype, attrs = w.create_calls[0]
    assert subtype == "land"
    assert [a["code"] for a in attrs][:4] == ["name", "lat", "lon", "altitude"]


def test_cli_create_live_reports_id_and_triage(tmp_path, capsys) -> None:
    triage = tmp_path / "onboard.txt"
    triage.write_text("parent = <SITE_ID>\n", encoding="utf-8")

    def cfg(w: _FakeWriter) -> None:
        w.create_response = {"id_entity": 7777}

    argv = _base_args() + [
        "--no-dry-run",
        "--triage",
        str(triage),
        "--placeholder",
        "SITE_ID",
    ]
    rc = _run_cli(argv, configure=cfg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "id_entity=7777" in out
    assert triage.read_text(encoding="utf-8") == "parent = 7777\n"


def test_cli_reused_id_substituted_into_triage_even_in_dry_run(tmp_path) -> None:
    triage = tmp_path / "onboard.txt"
    triage.write_text("parent = <SITE_ID>\n", encoding="utf-8")

    def cfg(w: _FakeWriter) -> None:
        w.existing_id = 4315

    argv = _base_args() + ["--triage", str(triage), "--placeholder", "SITE_ID"]
    rc = _run_cli(argv, configure=cfg)
    assert rc == 0
    # Reused site has a real id even in dry-run → substitution proceeds.
    assert triage.read_text(encoding="utf-8") == "parent = 4315\n"


def test_cli_force_creates_duplicate_with_warning(capsys) -> None:
    def cfg(w: _FakeWriter) -> None:
        w.existing_id = 4315

    rc = _run_cli(_base_args() + ["--force"], configure=cfg)
    err = capsys.readouterr().err
    assert rc == 0
    assert "duplicate" in err.lower()
    w = _FakeWriter.last_instance
    assert w is not None and len(w.create_calls) == 1


def test_cli_invalid_coord_exits_2_without_writer(capsys) -> None:
    argv = [
        "add",
        "--name",
        "X",
        "--lat",
        "999",
        "--lon",
        "-19",
        "--altitude",
        "10",
        "--date-start",
        "2026-06-07",
    ]
    rc = _run_cli(argv)
    assert rc == 2
    assert "between -90 and 90" in capsys.readouterr().err
    # Validation fails before the writer is even constructed.
    assert _FakeWriter.last_instance is None


def test_cli_invalid_date_exits_2(capsys) -> None:
    argv = [
        "add",
        "--name",
        "X",
        "--lat",
        "65",
        "--lon",
        "-19",
        "--altitude",
        "10",
        "--date-start",
        "not-a-date",
    ]
    rc = _run_cli(argv)
    assert rc == 2
    assert "Invalid --date-start" in capsys.readouterr().err


def test_cli_triage_without_placeholder_exits_2(capsys) -> None:
    argv = _base_args() + ["--triage", "/tmp/x.txt"]
    rc = _run_cli(argv)
    assert rc == 2
    assert "must be used together" in capsys.readouterr().err


def test_cli_json_output_shape(capsys) -> None:
    import json

    rc = _run_cli(_base_args() + ["--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["name"] == "Teststaður"
    assert payload["reused_existing"] is False
    assert payload["dry_run"] is True
    assert [a["code"] for a in payload["attributes"]][:4] == [
        "name",
        "lat",
        "lon",
        "altitude",
    ]
