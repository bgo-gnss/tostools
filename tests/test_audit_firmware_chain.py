"""Tests for ``tostools.audit_firmware_chain`` — firmware-history reconstruction.

The pure core (build_firmware_chain) takes an injected timeline + TosReceiver, so
every tier is tested offline. The load-bearing test is the serial-truncation
fixture: a current unit whose two adjacent segments differ ONLY in serial
spelling must produce one continuous chain dated from the EARLIER segment, and be
emitted COMMENTED (never auto-appliable) — the hazard that would otherwise corrupt
TOS firmware history via delete-all-rebuild.
"""

from __future__ import annotations

import argparse
from datetime import date

import tostools.audit_firmware_chain as fc
import tostools.tos as tos_cli
from tostools.receiver_timeline import ReceiverHeader, ReceiverSegment


def _seg(start: str, end: str, rtype: str, serial: str, fw: str) -> ReceiverSegment:
    return ReceiverSegment(
        date.fromisoformat(start),
        date.fromisoformat(end),
        ReceiverHeader(serial, rtype, fw),
    )


def _fwrow(id_av: int, value: str, date_from: str, date_to=None) -> dict:
    return {
        "id_attribute_value": id_av,
        "value": value,
        "date_from": date_from,
        "date_to": date_to,
    }


def _tos(serial: str, fw_rows, id_entity: int = 999) -> fc.TosReceiver:
    return fc.TosReceiver(id_entity=id_entity, serial=serial, fw_rows=fw_rows)


# --- clean tier --------------------------------------------------------------


def _olke_timeline():
    return [
        _seg("2017-07-08", "2019-10-14", "SEPT POLARX5", "3016143", "5.1.1"),
        _seg("2019-10-15", "2026-05-17", "SEPT POLARX5", "3016143", "5.3.0"),
        _seg("2026-05-18", "2026-06-29", "SEPT POLARX5", "3016143", "5.6.0"),
    ]


def test_clean_chain_is_actionable():
    tos = _tos("3016143", [_fwrow(100, "5.1.1", "2017-07-08")])
    r = fc.build_firmware_chain("OLKE", tos, timeline=_olke_timeline())
    assert r.tier == "clean"
    assert not r.commented and r.is_actionable
    assert [p.value for p in r.periods] == ["5.1.1", "5.3.0", "5.6.0"]
    # contiguous dates: each period closes where the next opens; last is open.
    assert r.periods[0].date_to == "2019-10-15"
    assert r.periods[-1].date_to is None
    # one delete (the stale row) + three add-attribute-period lines.
    assert sum(1 for a in r.action_lines if "delete-attribute-value" in a) == 1
    assert sum(1 for a in r.action_lines if "add-attribute-period" in a) == 3
    assert r.action_lines[-1].endswith("firmware_version 5.6.0 2026-05-18 open")


# --- serial truncation (the load-bearing safety case) ------------------------


def test_serial_truncation_is_one_chain_from_earlier_date_and_commented():
    # Same physical NetR9, serial written 20147817 then 7817 — differ ONLY in
    # spelling. Must be ONE period from the earlier day, flagged + commented.
    timeline = [
        _seg("2013-02-28", "2016-01-01", "TRIMBLE NETR9", "20147817", "5.01"),
        _seg("2016-01-02", "2020-01-01", "TRIMBLE NETR9", "7817", "5.01"),
    ]
    tos = _tos("7817", [_fwrow(200, "5.01", "2014-10-17")])
    r = fc.build_firmware_chain("TEST", tos, timeline=timeline)
    assert r.truncation_detected is True
    assert len(r.periods) == 1
    assert r.periods[0].date_from == "2013-02-28"  # dated from the EARLIER segment
    assert r.commented is True and not r.is_actionable


def test_truncation_multiperiod_chain_still_commented():
    # Truncation drift with a real firmware bump across the boundary → 2 periods,
    # still commented (the merge is a guess).
    timeline = [
        _seg("2013-02-28", "2016-01-01", "TRIMBLE NETR9", "20147817", "4.85"),
        _seg("2016-01-02", "2020-01-01", "TRIMBLE NETR9", "7817", "5.01"),
    ]
    tos = _tos("7817", [_fwrow(201, "5.01", "2014-10-17")])
    r = fc.build_firmware_chain("TEST", tos, timeline=timeline)
    assert r.truncation_detected and r.commented and not r.is_actionable
    assert len(r.periods) == 2
    assert r.periods[0].date_from == "2013-02-28"


# --- single / netrs / anomaly ------------------------------------------------


def test_single_ok_when_tos_already_matches():
    timeline = [_seg("2020-01-01", "2026-01-01", "SEPT POLARX5", "999", "5.6.0")]
    tos = _tos("999", [_fwrow(300, "5.6.0", "2020-01-01")])
    r = fc.build_firmware_chain("X", tos, timeline=timeline)
    assert r.tier == "single-ok"
    assert r.action_lines == [] and not r.is_actionable


def test_single_write_when_tos_differs():
    timeline = [_seg("2020-01-01", "2026-01-01", "SEPT POLARX5", "999", "5.6.0")]
    tos = _tos("999", [_fwrow(300, "5.3.0", "2020-01-01")])
    r = fc.build_firmware_chain("X", tos, timeline=timeline)
    assert r.tier == "single" and r.is_actionable
    assert any(
        "add-attribute-period firmware_version 5.6.0" in a for a in r.action_lines
    )


def test_netrs_uses_probe_value():
    timeline = [
        _seg("2010-01-01", "2015-01-01", "TRIMBLE NETRS", "4700", "1.15"),
        _seg("2015-01-02", "2020-01-01", "TRIMBLE NETRS", "4700", "1.3-2"),
    ]
    tos = _tos("4700", [_fwrow(400, "1.15", "2010-01-01")])
    r = fc.build_firmware_chain("BLEI", tos, timeline=timeline, probe_value="1.3-2")
    assert r.tier == "netrs" and r.is_actionable
    # current-period-only, probe value, dated from the unit's first archive day.
    add = [a for a in r.action_lines if "add-attribute-period" in a]
    assert len(add) == 1
    assert add[0].endswith("firmware_version 1.3-2 2010-01-01 open")


def test_netrs_without_probe_is_commented():
    # The netrs fallback value is the 1.x header the tier calls unreliable — so a
    # netrs chain with no --probe-value must NOT be auto-appliable.
    timeline = [
        _seg("2010-01-01", "2015-01-01", "TRIMBLE NETRS", "4700", "1.15"),
        _seg("2015-01-02", "2020-01-01", "TRIMBLE NETRS", "4700", "1.3-2"),
    ]
    tos = _tos("4700", [_fwrow(400, "1.15", "2010-01-01")])
    r = fc.build_firmware_chain("BLEI", tos, timeline=timeline)  # no probe_value
    assert r.tier == "netrs"
    assert r.commented is True and not r.is_actionable


def test_unreadable_anchor_serial_forces_commented():
    # Current (last) segment has an unreadable serial (******→None). Every earlier
    # same-type segment joins as a wildcard → unit identity unconfirmed → commented.
    timeline = [
        _seg("2017-01-01", "2020-01-01", "SEPT POLARX5", "3016143", "5.1.1"),
        _seg("2020-01-02", "2026-01-01", "SEPT POLARX5", "******", "5.3.0"),
    ]
    tos = _tos("3016143", [_fwrow(800, "5.1.1", "2017-01-01")])
    r = fc.build_firmware_chain("X", tos, timeline=timeline)
    assert r.tier == "clean"  # monotonic, but...
    assert r.commented is True and not r.is_actionable  # ...unit identity unconfirmed


def test_non_monotonic_is_anomaly_commented():
    timeline = [
        _seg("2018-01-01", "2020-01-01", "SEPT POLARX5", "5", "5.3.0"),
        _seg("2020-01-02", "2022-01-01", "SEPT POLARX5", "5", "5.1.1"),
    ]
    tos = _tos("5", [_fwrow(500, "5.1.1", "2018-01-01")])
    r = fc.build_firmware_chain("X", tos, timeline=timeline)
    assert r.tier == "anomaly" and r.commented and not r.is_actionable


def test_tos_multiperiod_forces_commented():
    tos = _tos(
        "3016143",
        [
            _fwrow(600, "5.1.1", "2017-07-08", "2019-10-15"),
            _fwrow(601, "5.3.0", "2019-10-15"),
        ],
    )
    r = fc.build_firmware_chain("OLKE", tos, timeline=_olke_timeline())
    assert r.tos_multiperiod and r.commented and not r.is_actionable


def test_serial_mismatch_skips():
    timeline = [_seg("2020-01-01", "2026-01-01", "SEPT POLARX5", "AAAA111", "5.6.0")]
    tos = _tos("BBBB222", [_fwrow(700, "5.6.0", "2020-01-01")])
    r = fc.build_firmware_chain("X", tos, timeline=timeline)
    assert r.tier == "skip"
    assert "serial mismatch" in r.reason


def test_render_triage_comments_destructive_lines():
    timeline = [
        _seg("2018-01-01", "2020-01-01", "SEPT POLARX5", "5", "5.3.0"),
        _seg("2020-01-02", "2022-01-01", "SEPT POLARX5", "5", "5.1.1"),
    ]
    tos = _tos("5", [_fwrow(500, "5.1.1", "2018-01-01")])
    body = fc.render_triage(fc.build_firmware_chain("X", tos, timeline=timeline))
    action_lines = [ln for ln in body if "ACTION" in ln]
    assert action_lines and all(ln.startswith("# ") for ln in action_lines)


# --- classify unit ------------------------------------------------------------


def _p(value, date_from, date_to=None, rtype="SEPT POLARX5"):
    return fc.FirmwarePeriod(value, rtype, "1", date_from, date_to)


def test_classify_tiers():
    assert fc.classify([_p("5.6.0", "2020-01-01")]) == "single"
    assert (
        fc.classify(
            [
                _p("5.1.1", "2017-01-01", "2019-01-01"),
                _p("5.3.0", "2019-01-01"),
            ]
        )
        == "clean"
    )
    assert (
        fc.classify(
            [
                _p("1.1", "2010-01-01", "2012-01-01", "TRIMBLE NETRS"),
                _p("1.2", "2012-01-01", rtype="TRIMBLE NETRS"),
            ]
        )
        == "netrs"
    )
    # short middle period (<50 days) → anomaly
    assert (
        fc.classify(
            [
                _p("5.1.1", "2020-01-01", "2020-01-10"),
                _p("5.3.0", "2020-01-10"),
            ]
        )
        == "anomaly"
    )


# --- CLI exit codes -----------------------------------------------------------


def _args(**kw):
    base = dict(
        station="OLKE",
        probe_value=None,
        rate="15s_24hr",
        archive_root=None,
        emit_triage=False,
        triage_dir=None,
        json=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _wire_cli(monkeypatch, tos_receiver, timeline):
    import tostools.archive as archive_mod
    import tostools.receiver_timeline as rt_mod

    monkeypatch.setattr(tos_cli, "_resolve_open_receiver", lambda c, s: tos_receiver)
    monkeypatch.setattr(rt_mod, "build_receiver_timeline", lambda *a, **k: timeline)
    from pathlib import Path

    monkeypatch.setattr(
        archive_mod, "cold_archive_prepath", lambda override=None: Path("/x")
    )


def test_cli_exit_2_when_no_tos_receiver(monkeypatch, capsys):
    monkeypatch.setattr(tos_cli, "_resolve_open_receiver", lambda c, s: None)
    assert tos_cli._audit_firmware_chain_main(_args(), client=None) == 2


def test_cli_exit_0_actionable(monkeypatch, capsys):
    tos = _tos("3016143", [_fwrow(100, "5.1.1", "2017-07-08")])
    _wire_cli(monkeypatch, tos, _olke_timeline())
    assert tos_cli._audit_firmware_chain_main(_args(), client=None) == 0
    assert "clean" in capsys.readouterr().out


def test_cli_exit_1_when_commented(monkeypatch, capsys):
    timeline = [
        _seg("2018-01-01", "2020-01-01", "SEPT POLARX5", "5", "5.3.0"),
        _seg("2020-01-02", "2022-01-01", "SEPT POLARX5", "5", "5.1.1"),
    ]
    tos = _tos("5", [_fwrow(500, "5.1.1", "2018-01-01")])
    _wire_cli(monkeypatch, tos, timeline)
    assert tos_cli._audit_firmware_chain_main(_args(), client=None) == 1
