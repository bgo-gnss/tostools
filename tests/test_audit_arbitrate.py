"""Tests for ``tostools.audit_arbitrate`` — duplicate-serial arbitration classifier.

Pure classifier, tested offline. The load-bearing cases mirror the asymmetry the
verb must honor: a rove drives a destructive merge, so anything short of positive
archive confirmation at every leg must NOT classify as rove.
"""

from __future__ import annotations

import tostools.audit_arbitrate as arb


def _leg(eid, serial, station, periods, seen=None):
    return arb.ArbitrationLeg(
        entity_id=eid,
        tos_serial=serial,
        station=station,
        archive_periods=list(periods),
        archive_serial_seen=seen,
    )


def test_rove_non_overlapping_confirmed():
    # The live ODDF case: 3012366 at ELDC (2020) then GRVC (2021), no overlap.
    legs = [
        _leg(16813, "3012366", "ELDC", [("2020-01-29", "2020-11-23")]),
        _leg(18973, "3012366 ", "GRVC", [("2021-11-23", "2021-12-13")]),
    ]
    v = arb.classify_arbitration("3012366", legs)
    assert v.verdict == arb.ROVE
    assert v.survivor_id == 16813  # earliest leg
    assert v.loser_ids == [18973]
    # steps: correct the trailing-space serial, merge, patch grafted dates
    joined = "\n".join(v.steps)
    assert "correct id 18973 serial" in joined
    assert "tos device merge --from 18973 --into 16813 --at 2021-11-23" in joined
    assert "patch the grafted GRVC join" in joined


def test_collision_overlapping_confirmed():
    legs = [
        _leg(100, "SN1", "AAAA", [("2020-01-01", "2021-06-01")]),
        _leg(200, "SN1", "BBBB", [("2020-06-01", "2021-01-01")]),  # overlaps
    ]
    v = arb.classify_arbitration("SN1", legs)
    assert v.verdict == arb.COLLISION
    assert v.survivor_id is None  # no merge recommended


def test_unconfirmed_leg_is_inconclusive_not_rove():
    # One leg has no archive confirmation → must NOT become a rove/merge.
    legs = [
        _leg(100, "SN1", "AAAA", [("2020-01-01", "2020-06-01")]),
        _leg(200, "SN1", "BBBB", [], seen="SN2"),  # archive shows a different serial
    ]
    v = arb.classify_arbitration("SN1", legs)
    assert v.verdict == arb.INCONCLUSIVE
    assert "SN2" in v.reason  # surfaces the likely typo
    assert v.survivor_id is None


def test_placeholder_serial_is_junk():
    legs = [
        _leg(1, "receiver-x", "AAAA", [("2020-01-01", None)]),
        _leg(2, "receiver-x", "BBBB", [("2021-01-01", None)]),
    ]
    v = arb.classify_arbitration("receiver-x", legs)
    assert v.verdict == arb.JUNK


def test_open_period_treated_as_ongoing_overlap():
    # An open (None end) leg overlaps a later leg → collision, not rove.
    legs = [
        _leg(1, "SN1", "AAAA", [("2020-01-01", None)]),
        _leg(2, "SN1", "BBBB", [("2022-01-01", "2022-06-01")]),
    ]
    v = arb.classify_arbitration("SN1", legs)
    assert v.verdict == arb.COLLISION


def test_survivor_is_earliest_leg_regardless_of_id():
    # Earliest archive start wins the survivor role, even with a higher id.
    legs = [
        _leg(200, "SN1 ", "AAAA", [("2020-01-01", "2020-03-01")]),
        _leg(100, "SN1", "BBBB", [("2021-01-01", "2021-03-01")]),
    ]
    v = arb.classify_arbitration("SN1", legs)
    assert v.verdict == arb.ROVE
    assert v.survivor_id == 200  # earliest start (2020) wins over lower id 100


def test_format_report_smoke():
    legs = [
        _leg(16813, "3012366", "ELDC", [("2020-01-29", "2020-11-23")]),
        _leg(18973, "3012366 ", "GRVC", [("2021-11-23", "2021-12-13")]),
    ]
    txt = arb.format_report(arb.classify_arbitration("3012366", legs))
    assert "ROVE" in txt
    assert "survivor" in txt
    assert "recommended" in txt
