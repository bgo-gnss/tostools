"""Tests for the identical-adjacent-render-session guard.

The guard exists because a TOS attribute period misaligned against a join
partitions one physical configuration into two site-log eras that render
identically — a *phantom era*. HRIC 2026-09-15 is the worked example: antenna
device 4680's five closed periods ended ``2022-11-22T00:00:00`` while its join
ended ``2022-11-22T16:00:00``, so §4 gained a 16-hour duplicate of the preceding
section that M3G asked us to remove.

§3 has always smoothed this silently (``coalesce_render_sessions`` +
``absorb_short_boundary_sessions``); §4 had no repair at all, which is how
HRIC's duplicate escaped. The guard therefore *reports* rather than repairs, and
refuses to render when a duplicate survives to the list that actually renders.

These tests pin the predicate, the contiguity requirement, and the
strict/warn/opt-out behaviour. They are written to be mutation-sensitive: see
the module-level notes in each test for which single-line change must make it
fail.
"""

from __future__ import annotations

import logging

import pytest

from tostools.devices import IdenticalRenderPair, find_identical_render_sessions
from tostools.exceptions import IdenticalRenderSessionsError
from tostools.legacy.gps_metadata_functions import (
    _antenna_render_signature,
    _check_identical_render_sessions,
    _identical_sessions_allowed,
    _receiver_render_signature,
)


def _slice(
    *,
    id_entity: int = 4680,
    sub: str = "antenna",
    date_from: str,
    date_to,
    serial: str = "0000",
    model: str = "TRM57971.00",
    arp: str = "BAM",
    height=0.0,
    north=0.0,
    east=0.0,
    azimuth=None,
):
    """One flat device-session row, shaped like the render loop consumes."""
    return {
        "device": {
            "id_entity": id_entity,
            "code_entity_subtype": sub,
            "model": model,
            "serial_number": serial,
            "antenna_reference_point": arp,
            "monument_height": None,
            "antenna_height": height,
            "antenna_offset_north": north,
            "antenna_offset_east": east,
            "azimuth": azimuth,
            "date_from": date_from,
            "date_to": date_to,
        }
    }


# ---------------------------------------------------------------------------
# the predicate
# ---------------------------------------------------------------------------


def test_detects_contiguous_identical_pair_HRIC_shape():
    """The exact HRIC shape: join ends 16:00, the period end lands on midnight.

    Mutation: drop the ``contiguous`` term from the condition and this still
    passes, so it is paired with the gap test below, which fails instead.
    """
    sl = [
        _slice(date_from="2014-08-23T00:00:00", date_to="2022-11-22T00:00:00"),
        _slice(date_from="2022-11-22T00:00:00", date_to="2022-11-22T16:00:00"),
    ]
    found = find_identical_render_sessions(sl, _antenna_render_signature, section="4")
    assert len(found) == 1
    pair = found[0]
    assert isinstance(pair, IdenticalRenderPair)
    assert pair.section == "4"
    assert pair.boundary == "2022-11-22T00:00:00"


def test_does_not_report_identical_slices_separated_by_a_real_gap():
    """Two identical blocks with a gap are two real installations, not one phantom.

    Mutation: remove the ``contiguous and`` guard from the condition and the
    phantom is reported for a station that is correct — this test goes red.
    """
    sl = [
        _slice(date_from="2014-08-23T00:00:00", date_to="2022-11-22T16:00:00"),
        _slice(date_from="2023-09-20T00:00:00", date_to=None),
    ]
    assert find_identical_render_sessions(sl, _antenna_render_signature) == []


def test_does_not_report_a_real_equipment_change():
    """A serial (or geometry) change between adjacent slices is a real era.

    Mutation: make the signature ignore ``serial_number`` and this goes red.
    """
    sl = [
        _slice(date_from="2022-11-22T16:00:00", date_to="2023-09-20T00:00:00",
               serial="0000"),
        _slice(date_from="2023-09-20T00:00:00", date_to=None, serial="5000119408"),
    ]
    assert find_identical_render_sessions(sl, _antenna_render_signature) == []


def test_open_ended_slice_is_never_a_phantom_source():
    """A lone open-ended row cannot be the prefix of anything."""
    sl = [_slice(date_from="2023-09-20T00:00:00", date_to=None)]
    assert find_identical_render_sessions(sl, _antenna_render_signature) == []


def test_two_none_dates_are_not_treated_as_contiguous():
    """``None == None`` must NOT count as a join boundary — it is missing data.

    This is the test that actually exercises the ``is not None`` half of the
    contiguity guard. Without it the equality alone is True for two rows whose
    dates are both unset, and a phantom is fabricated out of two rows that have
    no boundary between them at all.

    Mutation: drop ``pdev.get("date_to") is not None and`` from the condition
    and this goes red — while the single-slice test above stays green, which is
    exactly the trap that made this test necessary.
    """
    sl = [
        _slice(date_from="2022-11-22T00:00:00", date_to=None),
        _slice(date_from=None, date_to=None),
    ]
    assert find_identical_render_sessions(sl, _antenna_render_signature) == []


def test_single_slice_and_empty_input():
    assert find_identical_render_sessions([], _antenna_render_signature) == []
    assert (
        find_identical_render_sessions(
            [_slice(date_from="2022-11-22T00:00:00", date_to=None)],
            _antenna_render_signature,
        )
        == []
    )


def test_reports_every_pair_in_a_run():
    """A three-way run yields two pairs, not one — the count is the diagnostic.

    Mutation: return after the first hit (``break`` instead of ``append``) and
    this goes red.
    """
    sl = [
        _slice(date_from="1999-09-10T00:00:00", date_to="1999-09-13T00:00:00"),
        _slice(date_from="1999-09-13T00:00:00", date_to="1999-09-14T00:00:00"),
        _slice(date_from="1999-09-14T00:00:00", date_to="1999-09-15T00:00:00"),
    ]
    assert len(find_identical_render_sessions(sl, _antenna_render_signature)) == 2


def test_input_is_not_mutated():
    sl = [
        _slice(date_from="2022-11-22T00:00:00", date_to="2022-11-22T16:00:00"),
        _slice(date_from="2022-11-22T16:00:00", date_to="2023-09-20T00:00:00"),
    ]
    before = [dict(s["device"]) for s in sl]
    find_identical_render_sessions(sl, _antenna_render_signature)
    assert [dict(s["device"]) for s in sl] == before


def test_receiver_signature_resolves_the_satellite_system():
    """An unset-GPS and a set-GPS sub-window are the SAME system (the NYLA case).

    This is what makes §3's coalescer work, and it is why the detector must use
    the resolved value rather than the raw toggle. Mutation: compare raw
    ``GPS`` toggles instead of ``satellite_system_from_toggles`` and this goes
    red — the two rows stop matching.
    """
    a = _slice(sub="gnss_receiver", date_from="2006-07-27T00:00:00",
               date_to="2006-07-28T00:00:00")["device"]
    b = _slice(sub="gnss_receiver", date_from="2006-07-28T00:00:00",
               date_to=None)["device"]
    b["GPS"] = "true"
    a["firmware_version"] = b["firmware_version"] = "1.0"
    assert _receiver_render_signature(a) == _receiver_render_signature(b)


def test_receiver_signature_separates_a_real_constellation_change():
    """GPS -> GPS+GLO is a real change and must NOT be flagged.

    Mutation: drop the satellite-system term from the signature and this goes red.
    """
    a = _slice(sub="gnss_receiver", date_from="2023-09-20T00:00:00",
               date_to="2024-04-18T00:00:00")["device"]
    b = _slice(sub="gnss_receiver", date_from="2024-04-18T00:00:00",
               date_to=None)["device"]
    b["GPS"] = "true"
    b["GLO"] = "true"
    a["GPS"] = "true"
    assert _receiver_render_signature(a) != _receiver_render_signature(b)


# ---------------------------------------------------------------------------
# the guard: strict / warn / opt-out
# ---------------------------------------------------------------------------


def _pair():
    sl = [
        _slice(date_from="2014-08-23T00:00:00", date_to="2022-11-22T00:00:00"),
        _slice(date_from="2022-11-22T00:00:00", date_to="2022-11-22T16:00:00"),
    ]
    return find_identical_render_sessions(sl, _antenna_render_signature, section="4")


def test_strict_raises_and_names_the_device(caplog):
    """Strict must REFUSE, not warn — the site log itself would be wrong.

    Mutation: make strict log-and-return instead of raise and this goes red.
    """
    with pytest.raises(IdenticalRenderSessionsError) as exc:
        _check_identical_render_sessions(
            _pair(),
            station_identifier="HRIC",
            section="4",
            logger=logging.getLogger("test"),
            strict=True,
        )
    msg = str(exc.value)
    assert "HRIC" in msg and "phantom era" in msg
    # The diagnostic must name the device and the offending boundary, or the
    # operator cannot act on it.
    assert "id_entity=4680" in msg
    assert "2022-11-22T00:00:00" in msg
    assert "PATCH" in msg.upper() or "patch_attribute_value" in msg


def test_non_strict_warns_and_does_not_raise(caplog):
    """The pre-repair §3 list is recorded, never fatal.

    Mutation: make non-strict raise and this goes red.
    """
    with caplog.at_level(logging.WARNING):
        _check_identical_render_sessions(
            _pair(),
            station_identifier="HRIC",
            section="3",
            logger=logging.getLogger("test"),
            strict=False,
        )
    assert any("phantom era" in r.message for r in caplog.records)


def test_no_pairs_is_a_no_op():
    _check_identical_render_sessions(
        [],
        station_identifier="HRIC",
        section="4",
        logger=logging.getLogger("test"),
        strict=True,
    )


def test_opt_out_env_lets_a_strict_render_through(monkeypatch):
    """The documented escape hatch: investigate, then proceed deliberately.

    Mutation: ignore the env var and this goes red.
    """
    monkeypatch.setenv("TOSTOOLS_ALLOW_IDENTICAL_SESSIONS", "1")
    assert _identical_sessions_allowed() is True
    _check_identical_render_sessions(
        _pair(),
        station_identifier="HRIC",
        section="4",
        logger=logging.getLogger("test"),
        strict=True,
    )


@pytest.mark.parametrize("value", ["0", "false", "no", "", "off"])
def test_opt_out_env_is_not_truthy_for_falsey_values(monkeypatch, value):
    """Only an explicit truthy value opens the gate.

    Mutation: test membership in a set that omits the falsy check, or treat any
    non-empty string as truthy, and one of these goes red.
    """
    monkeypatch.setenv("TOSTOOLS_ALLOW_IDENTICAL_SESSIONS", value)
    assert _identical_sessions_allowed() is False
    with pytest.raises(IdenticalRenderSessionsError):
        _check_identical_render_sessions(
            _pair(),
            station_identifier="HRIC",
            section="4",
            logger=logging.getLogger("test"),
            strict=True,
        )
