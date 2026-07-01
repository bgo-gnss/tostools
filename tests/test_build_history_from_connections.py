"""Tests for ``TOSClient._build_history_from_connections`` — the boundary sweep.

The old builder grouped devices by their EXACT join ``(time_from, time_to)``
pair, so devices joined on different dates could never share a session. The
load-bearing fixture is ODDF's real staggered history (monument 2021, antenna
2023, receiver 2025 — all open): the current session must carry ALL three, or
``cfg reconcile`` reads "TOS: no value" for the antenna and site logs drop the
monument.

Pure unit tests — synthetic ``device_sessions`` rows, no network (the method
only consumes its argument).
"""

from __future__ import annotations

from datetime import datetime

from tostools.api.tos_client import TOSClient


def _client() -> TOSClient:
    return TOSClient(base_url="https://unused.invalid/tos/internal")


def _rx(serial: str, fw: str = "5.7.0", date_to=None) -> dict:
    return {
        "code_entity_subtype": "gnss_receiver",
        "model": "SEPT POLARX5",
        "serial_number": serial,
        "firmware_version": fw,
        "software_version": None,
        "date_to": date_to,
    }


def _ant(serial: str, height: float = 0.661, date_to=None) -> dict:
    return {
        "code_entity_subtype": "antenna",
        "model": "SEPPOLANT_X_MF",
        "serial_number": serial,
        "antenna_height": height,
        "antenna_offset_north": None,
        "antenna_offset_east": None,
        "antenna_reference_point": None,
        "date_to": date_to,
    }


def _mon(serial: str, date_to=None) -> dict:
    return {
        "code_entity_subtype": "monument",
        "serial_number": serial,
        "monument_height": 0.0,
        "antenna_height": None,
        "antenna_offset_north": None,
        "antenna_offset_east": None,
        "date_to": date_to,
    }


def _join(tf: str, tt, device: dict) -> dict:
    return {"time_from": tf, "time_to": tt, "device": device}


def test_oddf_staggered_open_joins_are_cumulative():
    # The live ODDF shape: three open joins with different install dates.
    sessions = _client()._build_history_from_connections(
        [
            _join("2021-02-28T12:00:00", None, _mon("monument-ODDF-20210228")),
            _join("2023-07-06T12:00:00", None, _ant("antenna-ODDF-20230706")),
            _join("2025-04-02T00:00:00", None, _rx("3012366")),
        ]
    )
    assert len(sessions) == 3
    assert set(sessions[0]) - {"time_from", "time_to"} == {"monument"}
    assert set(sessions[1]) - {"time_from", "time_to"} == {"monument", "antenna"}
    # The current session carries ALL THREE devices — the bug this fixes.
    cur = sessions[-1]
    assert cur["time_to"] is None
    assert set(cur) - {"time_from", "time_to"} == {
        "monument",
        "antenna",
        "gnss_receiver",
    }
    assert cur["antenna"]["model"] == "SEPPOLANT_X_MF"
    assert cur["antenna"]["antenna_height"] == 0.661
    assert cur["gnss_receiver"]["serial_number"] == "3012366"
    assert cur["monument"]["monument_height"] == 0.0
    # Boundaries are honest: middle session spans antenna-install → receiver-install.
    assert sessions[1]["time_from"] == datetime(2023, 7, 6, 12, 0, 0)
    assert sessions[1]["time_to"] == datetime(2025, 4, 2, 0, 0, 0)


def test_paired_joins_unchanged():
    # Classic paired history (all devices share the same join dates): the sweep
    # must reproduce the old grouping exactly.
    sessions = _client()._build_history_from_connections(
        [
            _join("2015-01-01T00:00:00", "2020-01-01T00:00:00", _rx("A1", "5.1.1")),
            _join("2015-01-01T00:00:00", "2020-01-01T00:00:00", _ant("ANT1")),
            _join("2020-01-01T00:00:00", None, _rx("B2", "5.3.0")),
            _join("2020-01-01T00:00:00", None, _ant("ANT2")),
        ]
    )
    assert len(sessions) == 2
    assert sessions[0]["gnss_receiver"]["serial_number"] == "A1"
    assert sessions[0]["antenna"]["serial_number"] == "ANT1"
    assert sessions[0]["time_to"] == datetime(2020, 1, 1)
    assert sessions[1]["gnss_receiver"]["serial_number"] == "B2"
    assert sessions[1]["antenna"]["serial_number"] == "ANT2"
    assert sessions[1]["time_to"] is None


def test_receiver_swap_under_continuing_antenna():
    # Antenna spans two receiver eras → two sessions, antenna in both.
    sessions = _client()._build_history_from_connections(
        [
            _join("2015-01-01T00:00:00", None, _ant("ANT1")),
            _join("2015-01-01T00:00:00", "2020-06-01T00:00:00", _rx("A1")),
            _join("2020-06-01T00:00:00", None, _rx("B2")),
        ]
    )
    assert len(sessions) == 2
    assert sessions[0]["gnss_receiver"]["serial_number"] == "A1"
    assert sessions[1]["gnss_receiver"]["serial_number"] == "B2"
    assert sessions[0]["antenna"]["serial_number"] == "ANT1"
    assert sessions[1]["antenna"]["serial_number"] == "ANT1"


def test_open_attribute_epoch_preferred_within_join():
    # One join, two attribute epochs (fw bump): the open epoch wins the slot.
    sessions = _client()._build_history_from_connections(
        [
            _join(
                "2020-01-01T00:00:00",
                None,
                _rx("A1", fw="5.1.1", date_to="2022-01-01T00:00:00"),
            ),
            _join("2020-01-01T00:00:00", None, _rx("A1", fw="5.3.0", date_to=None)),
        ]
    )
    assert len(sessions) == 1
    assert sessions[0]["gnss_receiver"]["firmware_version"] == "5.3.0"


def test_same_subtype_overlap_later_join_wins():
    # Overlapping receiver joins (data defect): the later-starting join wins
    # the slot for the overlap window — one receiver per session, never two.
    sessions = _client()._build_history_from_connections(
        [
            _join("2020-01-01T00:00:00", None, _rx("OLD")),
            _join("2022-01-01T00:00:00", None, _rx("NEW")),
        ]
    )
    assert [s["gnss_receiver"]["serial_number"] for s in sessions] == ["OLD", "NEW"]
    assert sessions[-1]["time_to"] is None


def test_empty_input():
    assert _client()._build_history_from_connections([]) == []
