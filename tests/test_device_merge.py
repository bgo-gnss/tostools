"""Tests for ``tos device merge`` (``_device_merge_main``).

Uses a small in-memory TOS simulator (``FakeTOS``) so the multi-step,
non-atomic orchestration is exercised end to end: survivor-side writes →
re-read/verify → delete-loser-last. Covers the guards, the husk vs rover
paths, the straddle-close cutover, overlap refusal, and abort-without-touching-
the-loser on a survivor-side failure.
"""

from __future__ import annotations

from argparse import Namespace
from unittest.mock import patch

from tostools.tos import _device_merge_main, _intervals_overlap, _merge_date


def _dev(subtype="gnss_receiver", serial="5048K71916", *, serial_from, model_from):
    return {
        "code_entity_subtype": subtype,
        "attributes": [
            {
                "code": "serial_number",
                "value": serial,
                "date_to": None,
                "date_from": f"{serial_from}T00:00:00",
                "id_attribute_value": 1,
            },
            {
                "code": "model",
                "value": "TRIMBLE NETR9",
                "date_to": None,
                "date_from": f"{model_from}T00:00:00",
                "id_attribute_value": 2,
            },
        ],
    }


def _join(jid, parent, tf, tt):
    return {
        "id": jid,
        "id_entity_parent": parent,
        "time_from": f"{tf}T00:00:00",
        "time_to": (f"{tt}T00:00:00" if tt else None),
    }


class FakeTOS:
    """Minimal in-memory stand-in for TOSClient + TOSWriter."""

    def __init__(self, entities, joins):
        self.entities = dict(entities)
        self.joins = {k: [dict(j) for j in v] for k, v in joins.items()}
        self._next = 90000
        self.fail_create = False

    # --- client reads ---
    def get_entity_history(self, eid):
        return self.entities.get(eid)

    def get_parent_history(self, eid):
        return [dict(j) for j in self.joins.get(eid, [])]

    # --- writer ---
    def create_entity_connection(self, parent, child, tf, tt=None):
        if self.fail_create:
            raise RuntimeError("simulated create failure")
        self._next += 1
        self.joins.setdefault(child, []).append(
            {
                "id": self._next,
                "id_entity_parent": parent,
                "time_from": f"{tf}T00:00:00",
                "time_to": (f"{tt}T00:00:00" if tt else None),
            }
        )

    def patch_entity_connection(self, jid, **kw):
        for joins in self.joins.values():
            for j in joins:
                if j["id"] == jid:
                    if "time_to" in kw:
                        j["time_to"] = f"{kw['time_to']}T00:00:00"

    def patch_attribute_value(self, av, **kw):
        pass

    def delete_entity_connection(self, jid):
        for joins in self.joins.values():
            joins[:] = [j for j in joins if j["id"] != jid]

    def delete_attribute_value(self, av):
        pass

    def delete_entity(self, eid):
        self.entities.pop(eid, None)
        self.joins.pop(eid, None)


def _args(from_id, into_id, at=None, **kw):
    base = dict(
        from_id=from_id,
        into_id=into_id,
        at=at,
        apply=False,
        force=False,
        commit=False,
        note=None,
        server="x",
        port=443,
        json=False,
    )
    base.update(kw)
    return Namespace(**base)


def _run(fake, args):
    with (
        patch("tostools.api.tos_client.TOSClient", return_value=fake),
        patch("tostools.api.tos_writer.TOSWriter", return_value=fake),
    ):
        return _device_merge_main(args)


def _pilot():
    """5048K71916: L=16358 (BRTT leg + B9 intake), S=4910 (HOTJ leg)."""
    entities = {
        4910: _dev(serial_from="2012-06-27", model_from="2011-08-25"),
        16358: _dev(serial_from="2019-07-30", model_from="2019-07-30"),
    }
    joins = {
        4910: [_join(6041, 4330, "2012-06-27", "2019-08-09")],
        16358: [
            _join(14617, 4, "2019-07-30", "2019-08-07"),  # B9 intake → dropped
            _join(14623, 16357, "2019-08-07", "2024-10-16"),  # BRTT → grafted
        ],
    }
    return FakeTOS(entities, joins)


# --- unit helpers ----------------------------------------------------------


def test_merge_date_prefix():
    assert _merge_date("2019-08-09T00:00:00") == "2019-08-09"
    assert _merge_date(None) is None


def test_intervals_overlap():
    assert _intervals_overlap("2012-01-01", "2019-08-09", "2019-08-07", "2024-01-01")
    # touching boundary (half-open) does not overlap
    assert not _intervals_overlap(
        "2012-01-01", "2019-08-09", "2019-08-09", "2024-01-01"
    )
    # open-ended
    assert _intervals_overlap("2012-01-01", None, "2020-01-01", None)


# --- guards ----------------------------------------------------------------


def test_merge_refuses_same_id():
    assert _run(_pilot(), _args(4910, 4910)) == 2


def test_merge_refuses_not_found():
    assert _run(_pilot(), _args(99999, 4910)) == 2


def test_merge_refuses_serial_mismatch():
    fake = _pilot()
    fake.entities[16358]["attributes"][0]["value"] = "DIFFERENT"
    assert _run(fake, _args(16358, 4910, at="2019-08-09")) == 1


def test_merge_refuses_subtype_mismatch():
    fake = _pilot()
    fake.entities[16358]["code_entity_subtype"] = "antenna"
    assert _run(fake, _args(16358, 4910, at="2019-08-09")) == 1


def test_merge_requires_at_when_loser_has_station_join():
    assert _run(_pilot(), _args(16358, 4910, at=None)) == 2  # no --at


# --- the pilot: rover merge ------------------------------------------------


def test_merge_dry_run_writes_nothing():
    fake = _pilot()
    assert _run(fake, _args(16358, 4910, at="2019-08-09")) == 0
    # loser still present, survivor still has only its HOTJ leg
    assert 16358 in fake.entities
    assert len(fake.joins[4910]) == 1


def test_merge_apply_grafts_and_deletes_loser():
    fake = _pilot()
    assert _run(fake, _args(16358, 4910, at="2019-08-09", apply=True)) == 0
    # loser gone
    assert 16358 not in fake.entities
    # survivor now holds HOTJ + grafted BRTT (clamped to start at the cutover)
    parents = sorted(j["id_entity_parent"] for j in fake.joins[4910])
    assert parents == [4330, 16357]
    brtt = next(j for j in fake.joins[4910] if j["id_entity_parent"] == 16357)
    assert _merge_date(brtt["time_from"]) == "2019-08-09"  # clamped
    assert _merge_date(brtt["time_to"]) == "2024-10-16"


def test_merge_straddle_closes_survivor_join():
    # --at 2019-08-07 falls inside the survivor's HOTJ leg → it must be closed there.
    fake = _pilot()
    assert _run(fake, _args(16358, 4910, at="2019-08-07", apply=True)) == 0
    hotj = next(j for j in fake.joins[4910] if j["id_entity_parent"] == 4330)
    assert _merge_date(hotj["time_to"]) == "2019-08-07"  # closed at the cutover
    brtt = next(j for j in fake.joins[4910] if j["id_entity_parent"] == 16357)
    assert _merge_date(brtt["time_from"]) == "2019-08-07"


# --- husk loser (no station joins) → delete-only ---------------------------


def test_merge_husk_loser_deletes_only_no_at_needed():
    entities = {
        4811: _dev(
            serial="0220353761", serial_from="2008-01-01", model_from="2008-01-01"
        ),
        19233: _dev(
            serial="0220353761", serial_from="2019-01-01", model_from="2019-01-01"
        ),
    }
    joins = {
        4811: [_join(1, 5000, "2008-01-01", None)],  # deployed, open
        19233: [_join(2, 4, "2019-01-01", None)],  # B9 husk only
    }
    fake = FakeTOS(entities, joins)
    # no --at needed: the husk has no station legs to graft
    assert _run(fake, _args(19233, 4811, apply=True)) == 0
    assert 19233 not in fake.entities
    # survivor untouched (still its one open join)
    assert len(fake.joins[4811]) == 1


# --- overlap refusal -------------------------------------------------------


def test_merge_refuses_residual_overlap():
    # Survivor and loser legs genuinely overlap, and --at is OUTSIDE the overlap
    # (before both), so the cutover cannot separate them → refuse, untouched.
    entities = {
        100: _dev(serial_from="2010-01-01", model_from="2010-01-01"),
        200: _dev(serial_from="2010-01-01", model_from="2010-01-01"),
    }
    joins = {
        100: [_join(1, 4330, "2010-01-01", "2020-01-01")],  # S leg
        200: [_join(2, 16357, "2012-01-01", "2018-01-01")],  # within S's span
    }
    fake = FakeTOS(entities, joins)
    # --at 2009 is before both → S not straddled, graft clamps to 2012 → overlaps S
    assert _run(fake, _args(200, 100, at="2009-01-01", apply=True)) == 1
    assert 200 in fake.entities  # refused → loser untouched


# --- abort safety: survivor-side failure leaves the loser untouched --------


def test_merge_aborts_without_deleting_loser_on_survivor_failure():
    fake = _pilot()
    fake.fail_create = True  # the graft create will raise
    rc = _run(fake, _args(16358, 4910, at="2019-08-09", apply=True))
    assert rc == 1
    assert 16358 in fake.entities  # loser NOT deleted — no data lost
