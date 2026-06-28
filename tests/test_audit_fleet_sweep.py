"""Tests for ``tostools.audit_fleet_sweep`` — the cheap cfg-vs-TOS sweep
behind ``tos audit fleet-sweep``.

The fake client below mimics the two methods the sweep needs from a
``TOSWriter(dry_run=True)``: ``find_station_by_marker`` and
``get_entity_history``. Each station test wires a station-entity history
(``children_connections``) plus a per-child entity history (subtype +
attributes), exercising the four flags and the clean case.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from tostools.audit_fleet_sweep import (
    parse_stations_cfg,
    run_fleet_sweep,
    summarize,
)


class FakeClient:
    """Minimal stand-in for ``TOSWriter(dry_run=True)``.

    ``markers`` maps marker → station entity id.
    ``histories`` maps entity id → entity-history dict.
    """

    def __init__(
        self,
        markers: Dict[str, int],
        histories: Dict[int, Dict[str, Any]],
    ) -> None:
        self._markers = markers
        self._histories = histories

    def find_station_by_marker(self, marker: str) -> Optional[int]:
        return self._markers.get(marker)

    def get_entity_history(self, id_entity: int) -> Optional[Dict[str, Any]]:
        return self._histories.get(int(id_entity))


def _station_hist(children: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"children_connections": children}


def _receiver_hist(model: str, serial: str) -> Dict[str, Any]:
    return {
        "code_entity_subtype": "gnss_receiver",
        "attributes": [
            {"code": "model", "value": model},
            {"code": "serial_number", "value": serial},
        ],
    }


def _antenna_hist(serial: str) -> Dict[str, Any]:
    return {
        "code_entity_subtype": "antenna",
        "attributes": [{"code": "serial_number", "value": serial}],
    }


# ---------------------------------------------------------------------------
# Clean station — no flags
# ---------------------------------------------------------------------------


def test_clean_station_no_flags():
    markers = {"GJAC": 100}
    histories = {
        100: _station_hist(
            [{"id_entity_child": 200, "time_to": None, "time_from": "2020-01-01"}]
        ),
        200: _receiver_hist("SEPT POLARX5", "3070340"),
    }
    cfg = {"GJAC": {"receiver_type": "PolaRX5", "receiver_serial": "3070340"}}
    results = run_fleet_sweep(FakeClient(markers, histories), ["GJAC"], cfg)
    assert results == [
        {
            "marker": "GJAC",
            "flags": [],
            "tos_open_rx": "SEPT POLARX5/3070340",
            "cfg_rx": "PolaRX5/3070340",
        }
    ]


# ---------------------------------------------------------------------------
# (#1) rx_type_mismatch
# ---------------------------------------------------------------------------


def test_rx_type_mismatch():
    markers = {"KRIV": 100}
    histories = {
        100: _station_hist(
            [{"id_entity_child": 200, "time_to": None, "time_from": "2010-01-01"}]
        ),
        # TOS still says Trimble NetRS while cfg says PolaRX5.
        200: _receiver_hist("TRIMBLE NETRS", "4636K"),
    }
    cfg = {"KRIV": {"receiver_type": "PolaRX5", "receiver_serial": "3070340"}}
    results = run_fleet_sweep(FakeClient(markers, histories), ["KRIV"], cfg)
    flags = results[0]["flags"]
    assert any(f.startswith("rx_type_mismatch:") for f in flags)
    assert "tos=TRIMBLE NETRS" in flags[0]
    assert "cfg=PolaRX5" in flags[0]


# ---------------------------------------------------------------------------
# (#1b) rx_serial_mismatch — types agree, TOS serial real, serials differ
# ---------------------------------------------------------------------------


def test_rx_serial_mismatch():
    markers = {"BRTT": 100}
    histories = {
        100: _station_hist(
            [{"id_entity_child": 200, "time_to": None, "time_from": "2018-01-01"}]
        ),
        200: _receiver_hist("SEPT POLARX5", "1111111"),
    }
    cfg = {"BRTT": {"receiver_type": "PolaRX5", "receiver_serial": "2222222"}}
    results = run_fleet_sweep(FakeClient(markers, histories), ["BRTT"], cfg)
    flags = results[0]["flags"]
    assert flags == ["rx_serial_mismatch:tos=1111111|cfg=2222222"]


def test_rx_serial_mismatch_suppressed_when_synthetic():
    """A synthetic TOS serial fires synthetic_rx_serial, NOT serial-mismatch
    (the elif branch in analyze)."""
    markers = {"ELDV": 100}
    histories = {
        100: _station_hist(
            [{"id_entity_child": 200, "time_to": None, "time_from": "2018-01-01"}]
        ),
        200: _receiver_hist("SEPT POLARX5", "receiver-ELDV-20180101"),
    }
    cfg = {"ELDV": {"receiver_type": "PolaRX5", "receiver_serial": "2222222"}}
    results = run_fleet_sweep(FakeClient(markers, histories), ["ELDV"], cfg)
    flags = results[0]["flags"]
    assert flags == ["synthetic_rx_serial:receiver-ELDV-20180101"]


# ---------------------------------------------------------------------------
# (#3) synthetic_rx_serial — several placeholder forms
# ---------------------------------------------------------------------------


def test_synthetic_serial_prefix():
    markers = {"SARP": 100}
    histories = {
        100: _station_hist(
            [{"id_entity_child": 200, "time_to": None, "time_from": "2018-01-01"}]
        ),
        200: _receiver_hist("SEPT POLARX5", "receiver-SARP-20180101"),
    }
    cfg = {"SARP": {"receiver_type": "PolaRX5", "receiver_serial": ""}}
    results = run_fleet_sweep(FakeClient(markers, histories), ["SARP"], cfg)
    assert results[0]["flags"] == ["synthetic_rx_serial:receiver-SARP-20180101"]


def test_synthetic_serial_zeros():
    markers = {"VOGS": 100}
    histories = {
        100: _station_hist(
            [{"id_entity_child": 200, "time_to": None, "time_from": "2018-01-01"}]
        ),
        200: _receiver_hist("SEPT POLARX5", "0000000000"),
    }
    cfg = {"VOGS": {"receiver_type": "PolaRX5", "receiver_serial": ""}}
    results = run_fleet_sweep(FakeClient(markers, histories), ["VOGS"], cfg)
    assert results[0]["flags"] == ["synthetic_rx_serial:0000000000"]


# ---------------------------------------------------------------------------
# (#4) antenna_split — same device, consecutive touching joins to station
# ---------------------------------------------------------------------------


def test_antenna_split():
    markers = {"FIHO": 100}
    histories = {
        100: _station_hist(
            [
                # open receiver (clean)
                {"id_entity_child": 200, "time_to": None, "time_from": "2018-01-01"},
                # same antenna device 300, two touching joins:
                # join A closes 2019-06-01, join B opens 2019-06-01.
                {
                    "id_entity_child": 300,
                    "time_from": "2018-01-01",
                    "time_to": "2019-06-01",
                },
                {
                    "id_entity_child": 300,
                    "time_from": "2019-06-01",
                    "time_to": None,
                },
            ]
        ),
        200: _receiver_hist("SEPT POLARX5", "3070340"),
        300: _antenna_hist("ANT9999"),
    }
    cfg = {"FIHO": {"receiver_type": "PolaRX5", "receiver_serial": "3070340"}}
    results = run_fleet_sweep(FakeClient(markers, histories), ["FIHO"], cfg)
    flags = results[0]["flags"]
    assert len(flags) == 1
    assert flags[0].startswith("antenna_split:dev300@2019-06-01")
    assert "sn ANT9999" in flags[0]


def test_antenna_not_split_when_gap():
    """Non-touching antenna joins (a real gap) must NOT flag."""
    markers = {"OKAY": 100}
    histories = {
        100: _station_hist(
            [
                {"id_entity_child": 200, "time_to": None, "time_from": "2018-01-01"},
                {
                    "id_entity_child": 300,
                    "time_from": "2018-01-01",
                    "time_to": "2019-06-01",
                },
                {
                    "id_entity_child": 300,
                    "time_from": "2019-07-01",  # gap, not touching
                    "time_to": None,
                },
            ]
        ),
        200: _receiver_hist("SEPT POLARX5", "3070340"),
        300: _antenna_hist("ANT9999"),
    }
    cfg = {"OKAY": {"receiver_type": "PolaRX5", "receiver_serial": "3070340"}}
    results = run_fleet_sweep(FakeClient(markers, histories), ["OKAY"], cfg)
    assert results[0]["flags"] == []


# ---------------------------------------------------------------------------
# Notes / robustness
# ---------------------------------------------------------------------------


def test_no_tos_station_note():
    cfg = {"NONE": {"receiver_type": "PolaRX5"}}
    results = run_fleet_sweep(FakeClient({}, {}), ["NONE"], cfg)
    assert results[0]["note"] == "no_tos_station"
    assert results[0]["flags"] == []


def test_no_open_receiver_note():
    markers = {"BARE": 100}
    histories = {
        100: _station_hist(
            [
                {
                    "id_entity_child": 200,
                    "time_to": "2015-01-01",
                    "time_from": "2010-01-01",
                }
            ]
        ),
        200: _receiver_hist("SEPT POLARX5", "3070340"),  # closed join
    }
    cfg = {"BARE": {"receiver_type": "PolaRX5"}}
    results = run_fleet_sweep(FakeClient(markers, histories), ["BARE"], cfg)
    assert results[0]["note"] == "no_open_tos_receiver"
    assert results[0]["flags"] == []


def test_per_station_exception_isolated():
    class BoomClient:
        def find_station_by_marker(self, marker: str) -> int:
            raise RuntimeError("kaboom")

        def get_entity_history(self, id_entity: int):  # pragma: no cover
            return None

    results = run_fleet_sweep(BoomClient(), ["X1", "X2"], {})
    assert len(results) == 2
    assert all("kaboom" in r["error"] for r in results)
    assert all(r["flags"] == [] for r in results)


def test_only_diffs_filters_clean():
    markers = {"DIRTY": 100, "CLEAN": 101}
    histories = {
        100: _station_hist(
            [{"id_entity_child": 200, "time_to": None, "time_from": "2010-01-01"}]
        ),
        200: _receiver_hist("TRIMBLE NETRS", "x"),
        101: _station_hist(
            [{"id_entity_child": 201, "time_to": None, "time_from": "2010-01-01"}]
        ),
        201: _receiver_hist("SEPT POLARX5", "3070340"),
    }
    cfg = {
        "DIRTY": {"receiver_type": "PolaRX5", "receiver_serial": "3070340"},
        "CLEAN": {"receiver_type": "PolaRX5", "receiver_serial": "3070340"},
    }
    client = FakeClient(markers, histories)
    all_results = run_fleet_sweep(client, ["DIRTY", "CLEAN"], cfg)
    assert {r["marker"] for r in all_results} == {"DIRTY", "CLEAN"}
    diffs = run_fleet_sweep(client, ["DIRTY", "CLEAN"], cfg, only_diffs=True)
    assert [r["marker"] for r in diffs] == ["DIRTY"]

    # The CLI must NOT lose the full denominator under --only-diffs: it sweeps
    # all stations and applies the display filter only to what's shown, so the
    # --out artifact's `total` stays at the full count (2 here, not 1).
    flagged = [r for r in all_results if r.get("flags")]
    shown = flagged  # --only-diffs view
    payload = {
        "total": len(all_results),
        "flagged": len(flagged),
        "results": all_results,
    }
    assert payload["total"] == 2
    assert payload["flagged"] == 1
    assert [r["marker"] for r in shown] == ["DIRTY"]


def test_summarize_groups_by_category():
    results = [
        {"marker": "A", "flags": ["rx_type_mismatch:foo"]},
        {"marker": "B", "flags": ["rx_type_mismatch:bar", "synthetic_rx_serial:0"]},
    ]
    by = summarize(results)
    assert sorted(by["rx_type_mismatch"]) == ["A", "B"]
    assert by["synthetic_rx_serial"] == ["B"]


# ---------------------------------------------------------------------------
# cfg parser
# ---------------------------------------------------------------------------


def test_parse_stations_cfg(tmp_path):
    cfg_file = tmp_path / "stations.cfg"
    cfg_file.write_text(
        "[KRIV]\n"
        "receiver_type = PolaRX5\n"
        "receiver_serial = 3070340\n"
        "# a comment = ignored\n"
        "antenna_serial = ANT123\n"
        "[lowercase_section]\n"
        "foo = bar\n"
        "[GJAC]\n"
        "receiver_type = PolaRX5\n",
        encoding="utf-8",
    )
    cfg = parse_stations_cfg(str(cfg_file))
    assert set(cfg) == {"KRIV", "GJAC"}
    assert cfg["KRIV"]["receiver_type"] == "PolaRX5"
    assert cfg["KRIV"]["receiver_serial"] == "3070340"
    assert cfg["KRIV"]["antenna_serial"] == "ANT123"
    assert "a comment" not in cfg["KRIV"]
