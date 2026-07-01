"""Tests for ``tostools.audit_rinex_timeline`` — the rinex-timeline orchestrator.

The per-field timelines come from receiver_timeline / antenna_timeline (tested
separately); here we monkeypatch those builders to fake segments and check the
orchestration: field dispatch, receiver-unit coalescing (NOT a firmware
re-display), row shape, unit-install date, and the CLI exit-code contract.
"""

from __future__ import annotations

import argparse
from datetime import date

import pytest

import tostools.audit_rinex_timeline as rtl
import tostools.tos as tos_cli
from tostools.antenna_timeline import AntennaHeader, AntennaSegment
from tostools.receiver_timeline import ReceiverHeader, ReceiverSegment


def _rseg(start, end, rtype, serial, fw):
    return ReceiverSegment(start, end, ReceiverHeader(serial, rtype, fw))


def test_firmware_field_keeps_fw_segments(monkeypatch):
    timeline = [
        _rseg(date(2017, 7, 8), date(2019, 1, 1), "SEPT POLARX5", "3016143", "5.1.1"),
        _rseg(date(2019, 1, 2), date(2026, 5, 1), "SEPT POLARX5", "3016143", "5.3.0"),
    ]
    monkeypatch.setattr(
        rtl.rec_mod, "build_receiver_timeline", lambda *a, **k: timeline
    )

    rep = rtl.run_rinex_timeline("OLKE", "firmware", root="/x")
    assert [r["firmware"] for r in rep.rows] == ["5.1.1", "5.3.0"]
    assert rep.unit_install_date is None  # firmware view has no unit date


def test_receiver_field_coalesces_firmware_bumps(monkeypatch):
    # Same physical PolaRX5 across two firmware bumps → ONE receiver segment.
    timeline = [
        _rseg(date(2017, 7, 8), date(2019, 1, 1), "SEPT POLARX5", "3016143", "5.1.1"),
        _rseg(date(2019, 1, 2), date(2026, 5, 1), "SEPT POLARX5", "3016143", "5.3.0"),
    ]
    monkeypatch.setattr(
        rtl.rec_mod, "build_receiver_timeline", lambda *a, **k: timeline
    )

    rep = rtl.run_rinex_timeline("OLKE", "receiver", root="/x")
    assert len(rep.rows) == 1
    assert rep.rows[0]["rec_serial"] == "3016143"
    assert "firmware" not in rep.rows[0]  # unit view drops firmware
    assert rep.unit_install_date == "2017-07-08"


def test_antenna_field_rows_and_install_date(monkeypatch):
    timeline = [
        AntennaSegment(
            date(2015, 1, 1),
            date(2020, 1, 1),
            AntennaHeader("A1", "TRM59800.00", "NONE", 0.0083, 0.0, 0.0),
        ),
    ]
    monkeypatch.setattr(rtl.ant_mod, "build_antenna_timeline", lambda *a, **k: timeline)

    rep = rtl.run_rinex_timeline("RHOF", "antenna", root="/x")
    assert rep.rows[0]["antenna_type"] == "TRM59800.00"
    assert rep.rows[0]["radome"] == "NONE"
    assert rep.rows[0]["delta_h"] == 0.0083
    assert rep.unit_install_date == "2015-01-01"


def test_unknown_field_raises():
    with pytest.raises(ValueError):
        rtl.run_rinex_timeline("X", "bogus", root="/x")


def test_to_json_and_format_smoke(monkeypatch):
    monkeypatch.setattr(rtl.rec_mod, "build_receiver_timeline", lambda *a, **k: [])
    rep = rtl.run_rinex_timeline("EMPTY", "firmware", root="/x")
    assert rep.rows == []
    assert rep.to_json_dict()["n_segments"] == 0
    assert "no archived RINEX headers" in rtl.format_report(rep)


# --- CLI handler exit codes --------------------------------------------------


def _args(**kw):
    base = dict(
        station="OLKE",
        field="firmware",
        archive_root=None,
        rate="15s_24hr",
        json=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_exit_0_when_segments(monkeypatch, capsys):
    rep = rtl.RinexTimelineReport("OLKE", "firmware", "15s_24hr", "/x")
    rep.rows = [
        {
            "date_from": "2020-01-01",
            "last_seen": "2020-02-01",
            "firmware": "5.3.0",
            "rec_type": "SEPT POLARX5",
            "rec_serial": "1",
        }
    ]
    monkeypatch.setattr(rtl, "run_rinex_timeline", lambda *a, **k: rep)
    assert tos_cli._audit_rinex_timeline_main(_args()) == 0
    assert "firmware timeline" in capsys.readouterr().out


def test_cli_exit_1_when_empty(monkeypatch):
    rep = rtl.RinexTimelineReport("OLKE", "firmware", "15s_24hr", "/x")
    monkeypatch.setattr(rtl, "run_rinex_timeline", lambda *a, **k: rep)
    assert tos_cli._audit_rinex_timeline_main(_args()) == 1


def test_cli_exit_2_on_unresolved_archive(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("cold_archive_prepath unresolved")

    monkeypatch.setattr(rtl, "run_rinex_timeline", _boom)
    assert tos_cli._audit_rinex_timeline_main(_args()) == 2


def test_cli_json_output(monkeypatch, capsys):
    import json as _json

    rep = rtl.RinexTimelineReport("OLKE", "receiver", "15s_24hr", "/x")
    rep.rows = [
        {
            "date_from": "2017-07-08",
            "last_seen": "2026-05-01",
            "rec_type": "SEPT POLARX5",
            "rec_serial": "3016143",
        }
    ]
    rep.unit_install_date = "2017-07-08"
    monkeypatch.setattr(rtl, "run_rinex_timeline", lambda *a, **k: rep)

    rc = tos_cli._audit_rinex_timeline_main(_args(field="receiver", json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["field"] == "receiver"
    assert payload["current_unit_install_date"] == "2017-07-08"
    assert payload["segments"][0]["rec_serial"] == "3016143"
