"""`tosGPS` constrains every station search to ``GPS stöð``.

The bug these lock down: the GPS audits narrow which **attributes** they
grade (``audit_missing_attributes`` skips every code whose
``gps_relevance != 'yes'``), so pointing them at a non-GPS station leaves
nothing to fail and they pass **vacuously**. ``tosGPS station verify VLFS``
reported ``✓ clean — 0 finding(s)`` for a 1963 precipitation gauge with zero
devices. A false PASS from the oracle is the actual danger.

The gate is a **filter**, not a refusal. A station of another discipline is
not something ``tosGPS`` rejects; it is something ``tosGPS`` cannot see, and
each verb's existing "not found" path answers for it. So these tests assert
each verb's own not-found exit code rather than a bespoke one.

Three live cases, each a different outcome, measured 2026-10-01:

* **VLFS** (id 96, *Vífilsstaðir*, ``meteorological`` / ``Úrkomustöð``) —
  must not resolve. The reported bug.
* **BRST** — must not resolve. BRST is *Brest, France*
  (``is_in_iceland = false``), an external IGS site with no TOS entity; the
  only entity carrying marker ``brst`` is id 646, *Berustaðir í Ásum*, an
  Icelandic weather station. Nobody had noticed.
* **SOHO** — must RESOLVE, to 4416. Marker ``soho`` carries **two**
  geophysical entities: 5356 (``DOAS``, volcanic gas) and 4416
  (``GPS stöð``, receiver 3075357). SOHO is the discriminating case: an
  implementation that checked AFTER resolving would reject a perfectly valid
  GPS station here, while filtering candidates picks 4416. If the SOHO tests
  pass while the VLFS tests also pass, the gate filters.

And the invariant the whole design protects: on a single-candidate station
``tosGPS`` must be **byte-identical** to ``tos``, because
``gps-tos-corrections`` records 270 ``tos audit apply`` runs as operational
procedure and ``tos station`` appears ~150 times across the docs.

The unit tests need no network. The dispatch-level tests are cassette-backed
(``pytest-recording``); re-record with ``--record-mode=once``.
"""

import sys
from unittest.mock import patch

import pytest

from tostools import tos as tos_mod
from tostools import tosGPS as tosgps_mod
from tostools.search_selectors import gps_profile
from tostools.station_kind import (
    AmbiguousStation,
    all_attribute_values,
    describe_entity,
    open_attribute,
    select_station,
)

# --------------------------------------------------------------------------
# Fixtures shaped like the live entities, so the unit tests assert against
# the real data rather than a convenient simplification.
# --------------------------------------------------------------------------


def _entity(eid, etype, subtype, name):
    attrs = [{"code": "name", "value": name, "date_to": None}]
    if subtype is not None:
        attrs.append({"code": "subtype", "value": subtype, "date_to": None})
    return {"id_entity": eid, "code_entity_subtype": etype, "attributes": attrs}


VLFS_MET = _entity(96, "meteorological", "Úrkomustöð", "Vífilsstaðir")
BRST_MET = _entity(646, "meteorological", "Veðurfarsstöð", "Berustaðir í Ásum")
SOHO_DOAS = _entity(5356, "geophysical", "DOAS", "Sólheimaheiði")
SOHO_GPS = _entity(4416, "geophysical", "GPS stöð", "Sólheimaheiði")
VFLS_GPS = _entity(21832, "geophysical", "GPS stöð", "Vatnsfell Suður")
SIL_SEISMIC = _entity(1234, "geophysical", "SIL stöð", "Einhver SIL")
NO_SUBTYPE = _entity(9999, "geophysical", None, "Brand New Station")


@pytest.fixture(scope="module")
def predicate():
    return gps_profile().admits_station


# --------------------------------------------------------------------------
# The predicate — two levels, and why neither alone suffices
# --------------------------------------------------------------------------


def test_predicate_is_read_off_the_attribute_catalog():
    """Not hardcoded: both levels come from the catalog's `subtype` entry.

    ``applies_to`` gives the entity-type scope, ``default_value`` the
    subtype label — the same catalog ``gps_profile()`` already reads its
    station and device attribute sets from.
    """
    profile = gps_profile()
    assert profile.entity_scopes == ("geophysical",)
    assert profile.subtype == "GPS stöð"


def test_entity_type_alone_is_insufficient(predicate):
    """SIL and DOAS are `geophysical` too — the subtype attribute separates
    them. This is why the gate cannot be `code_entity_subtype` alone."""
    assert SIL_SEISMIC["code_entity_subtype"] == "geophysical"
    assert SOHO_DOAS["code_entity_subtype"] == "geophysical"
    assert predicate(SIL_SEISMIC) is False
    assert predicate(SOHO_DOAS) is False


def test_other_disciplines_are_excluded(predicate):
    assert predicate(VLFS_MET) is False
    assert predicate(BRST_MET) is False


def test_real_gps_stations_are_admitted(predicate):
    assert predicate(SOHO_GPS) is True
    assert predicate(VFLS_GPS) is True


def test_a_station_that_never_carried_a_subtype_is_admitted(predicate):
    """Lenient on purpose, and load-bearing.

    ``subtype`` is itself audited by ``missing-attributes``. Requiring it
    here would make the gate pre-empt the audit that exists to report it,
    and would lock a real GPS station out of its own verify run for exactly
    the defect that run should surface.
    """
    assert open_attribute(NO_SUBTYPE, "subtype") is None
    assert all_attribute_values(NO_SUBTYPE, "subtype") == []
    assert predicate(NO_SUBTYPE) is True


def test_a_non_geophysical_entity_with_no_subtype_is_still_excluded(predicate):
    """The one case where the entity-type level does the work by itself.

    Found by ``scripts/dev/mutate_station_gate.py``: deleting the entity-type
    check left every other test green, because ``Úrkomustöð != GPS stöð``
    catches VLFS and BRST on the subtype level alone. The type level only
    bites where the subtype attribute is absent — and that leniency is
    deliberate, so without the type check a meteorological or hydrological
    entity would sail straight through.
    """
    bare_met = {
        "id_entity": 11,
        "code_entity_subtype": "meteorological",
        "attributes": [{"code": "name", "value": "Einhver stöð", "date_to": None}],
    }
    assert open_attribute(bare_met, "subtype") is None, "fixture must have no subtype"
    assert predicate(bare_met) is False


def test_a_decommissioned_doas_station_is_not_admitted(predicate):
    """Absent means NEVER SET, not "not set right now".

    A geophysical entity whose only ``subtype`` period is a closed ``DOAS``
    is a decommissioned gas station. Reading a closed period as absent would
    hand it to the GPS audits via the leniency above.
    """
    retired_doas = {
        "id_entity": 12,
        "code_entity_subtype": "geophysical",
        "attributes": [{"code": "subtype", "value": "DOAS", "date_to": "2020-01-01"}],
    }
    assert open_attribute(retired_doas, "subtype") is None
    assert all_attribute_values(retired_doas, "subtype") == ["DOAS"]
    assert predicate(retired_doas) is False


def test_a_gps_station_whose_subtype_period_lapsed_is_still_admitted(predicate):
    """The other half of the same rule — a data gap on OUR station must not
    lock it out of its own audit."""
    lapsed_gps = {
        "id_entity": 13,
        "code_entity_subtype": "geophysical",
        "attributes": [
            {"code": "subtype", "value": "GPS stöð", "date_to": "2020-01-01"}
        ],
    }
    assert open_attribute(lapsed_gps, "subtype") is None
    assert predicate(lapsed_gps) is True


# --------------------------------------------------------------------------
# select_station — a filter; "none admitted" is NOT FOUND, not an error
# --------------------------------------------------------------------------


def test_without_a_predicate_the_first_candidate_wins():
    """The ungated path must stay exactly as it was — this is what keeps
    `tos` byte-identical, and it is also the bug, preserved deliberately."""
    chosen = select_station("soho", [SOHO_DOAS, SOHO_GPS], None)
    assert chosen["id_entity"] == 5356


def test_the_gate_picks_the_gps_candidate_not_the_first(predicate):
    """SOHO is the discriminating case for filter-vs-post-check."""
    chosen = select_station("soho", [SOHO_DOAS, SOHO_GPS], predicate)
    assert chosen["id_entity"] == 4416


def test_no_admitted_candidate_is_simply_not_found(predicate):
    """No exception, no bespoke refusal — the answer an absent marker gives.

    This is what lets the gate ride each caller's existing not-found path
    instead of a second one that every caller would have to remember to
    handle (``_visit_main`` did not, and turned an earlier refusal-object
    design into a stack trace).
    """
    assert select_station("vlfs", [VLFS_MET], predicate) is None
    assert select_station("brst", [], predicate) is None


def test_two_admitted_candidates_raise_rather_than_guess(predicate):
    """The one case the filter cannot answer by itself.

    Unreachable across the whole live fleet as measured 2026-10-01 — which
    is exactly why it is loud. The day a second ``GPS stöð`` appears on one
    marker, silently taking the first would be the SOHO bug again, one level
    down.
    """
    twin = dict(SOHO_GPS, id_entity=5555)
    with pytest.raises(AmbiguousStation) as exc:
        select_station("soho", [SOHO_GPS, twin], predicate)
    assert "ambiguous" in str(exc.value)
    assert issubclass(AmbiguousStation, LookupError)


def test_describe_entity_gives_the_three_facts_that_justify_exclusion():
    out = describe_entity(VLFS_MET)
    assert "meteorological" in out and "Vífilsstaðir" in out and "id_entity=96" in out


# --------------------------------------------------------------------------
# basic_search-only candidates (the HELC class) need a history fetch each
# --------------------------------------------------------------------------


class _FakeClient:
    """A client whose LIVE station index misses the marker entirely.

    HELC (id 16095) stores marker ``helc`` but is absent from
    ``/basic_search/``; the inverse also happens, and it is the branch where
    a candidate arrives WITHOUT ``attributes``, so the predicate cannot
    judge it in place and the resolver must fetch its history. Nothing else
    reaches that branch, so it would otherwise be untested.
    """

    def __init__(self, histories, basic_hits):
        self.histories = histories
        self.basic_hits = basic_hits
        self.history_calls = []

    def search_stations(self, *_a, **_k):
        return []

    def basic_search(self, *_a, **_k):
        return self.basic_hits

    def get_entity_history(self, eid):
        self.history_calls.append(int(eid))
        return self.histories.get(int(eid))


def _basic_hit(eid, marker):
    return {
        "code": "marker",
        "value_varchar": marker,
        "distance": 0,
        "id_entity": eid,
        "type_lvl_two": "stöð",
    }


def test_basic_search_only_candidates_are_fetched_and_filtered(predicate):
    client = _FakeClient(
        histories={5356: SOHO_DOAS, 4416: SOHO_GPS},
        basic_hits=[_basic_hit(5356, "soho"), _basic_hit(4416, "soho")],
    )
    got = tos_mod._resolve_parent_id(client, station_marker="soho", predicate=predicate)
    assert got == 4416
    assert set(client.history_calls) == {5356, 4416}, "both must be judged"


def test_basic_search_only_candidates_ungated_keep_first_hit_and_fetch_nothing():
    """Ungated, the extra history fetches must NOT happen — the request
    sequence is part of what "byte-identical" means here."""
    client = _FakeClient(
        histories={5356: SOHO_DOAS, 4416: SOHO_GPS},
        basic_hits=[_basic_hit(5356, "soho"), _basic_hit(4416, "soho")],
    )
    assert tos_mod._resolve_parent_id(client, station_marker="soho") == 5356
    assert client.history_calls == []


def test_unreadable_candidate_histories_yield_not_found(predicate):
    """All histories None → None, not a crash and not a false positive."""
    client = _FakeClient(
        histories={5356: None, 4416: None},
        basic_hits=[_basic_hit(5356, "soho"), _basic_hit(4416, "soho")],
    )
    assert (
        tos_mod._resolve_parent_id(client, station_marker="soho", predicate=predicate)
        is None
    )


# --------------------------------------------------------------------------
# _gated_station_id — the fan-out boundary, asserted directly
# --------------------------------------------------------------------------
#
# These are offline on purpose. The dispatch-level `station verify VLFS` test
# cannot carry this weight: break the miss branch and the verb proceeds,
# issuing requests the cassette does not hold, so VCR refuses BEFORE any
# assertion runs. The suite goes red either way, but only for the wrong
# reason — `mutate_station_gate.py` reports that as CASSETTE-ONLY rather than
# DETECTED, and these tests are what turn it into a real detection.


def test_gated_station_id_reports_a_miss_for_a_filtered_station(predicate):
    """A filtered-out marker must come back as "do not proceed".

    If this returned ``True`` the verb would run on with ``id_entity=None``
    and every audit would re-resolve the marker UNGATED — straight back to
    entity 96.
    """
    client = _FakeClient(histories={96: VLFS_MET}, basic_hits=[_basic_hit(96, "vlfs")])
    assert tos_mod._gated_station_id(client, "vlfs", predicate) == (False, None)


def test_gated_station_id_returns_the_admitted_candidate(predicate):
    client = _FakeClient(
        histories={5356: SOHO_DOAS, 4416: SOHO_GPS},
        basic_hits=[_basic_hit(5356, "soho"), _basic_hit(4416, "soho")],
    )
    assert tos_mod._gated_station_id(client, "soho", predicate) == (True, 4416)


def test_gated_station_id_is_free_and_silent_without_a_predicate():
    """Ungated it must cost NOTHING — no request, and the verb resolves the
    marker itself exactly as it always has."""
    client = _FakeClient(histories={}, basic_hits=[])
    assert tos_mod._gated_station_id(client, "vlfs", None) == (True, None)
    assert client.history_calls == []


# --------------------------------------------------------------------------
# Dispatch level — the wiring, not the helper
# --------------------------------------------------------------------------

#: Every `tosGPS` verb that resolves a station marker, with the exit code
#: that verb uses for a lookup miss. Parametrised so a resolver wired in one
#: verb and missed in a sibling fails HERE — the "fix one, leave the sibling
#: open" pattern this codebase has been bitten by repeatedly. The exit code
#: is pinned, not merely asserted non-zero: 1 is a lookup miss, 2 is "the
#: audit could not run", and a gate that silently moved a verb from one to
#: the other would change what callers see.
EXCLUDED_VERBS = [
    pytest.param(["station", "show", "VLFS"], 1, id="station-show"),
    pytest.param(["station", "show", "VLFS", "--device"], 1, id="station-show-device"),
    pytest.param(["station", "receivers", "VLFS"], 1, id="station-receivers"),
    pytest.param(["device", "list", "--station", "VLFS"], 1, id="device-list"),
    pytest.param(["contact", "list", "--station", "VLFS"], 1, id="contact-list"),
    pytest.param(["visit", "list", "--station", "VLFS"], 1, id="visit-list"),
    pytest.param(["station", "verify", "VLFS"], 2, id="station-verify"),
    pytest.param(["station", "triage", "VLFS", "--stdout"], 2, id="station-triage"),
    pytest.param(["audit", "station", "VLFS"], 2, id="audit-station"),
    pytest.param(
        ["audit", "missing-attributes", "VLFS"], 2, id="audit-missing-attributes"
    ),
    # WRITES. Dry-run by default (`--no-dry-run` is the opt-in), so these are
    # safe to run, and they matter most: writing the right value to the wrong
    # station is the worst outcome available here, not the most tolerable one.
    # `tosGPS station describe SOHO --text … --no-dry-run` would have PATCHed
    # `description` onto the DOAS gas station 5356.
    pytest.param(
        ["station", "describe", "VLFS", "--text", "hallo"], 2, id="station-describe"
    ),
    pytest.param(
        ["station", "set", "VLFS", "description", "hallo"], 2, id="station-set"
    ),
]


def _run_tosgps(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["tosGPS", *argv])
    return tosgps_mod._dispatch()


@pytest.mark.vcr
@pytest.mark.parametrize("argv,expected_rc", EXCLUDED_VERBS)
def test_tosgps_cannot_see_a_meteorological_station(
    monkeypatch, capsys, argv, expected_rc
):
    rc = _run_tosgps(monkeypatch, argv)
    captured = capsys.readouterr()
    assert rc == expected_rc, (
        f"{argv} returned {rc}; a zero exit here is the vacuous PASS this "
        f"whole change exists to remove"
    )
    # Vífilsstaðir's own data must not be rendered as if it were a GPS station.
    assert "Úrkomustöð" not in captured.out
    assert "✓ clean" not in captured.out


@pytest.mark.vcr
def test_tosgps_cannot_see_a_marker_whose_only_entity_is_a_weather_station(
    monkeypatch, capsys
):
    """BRST is Brest, France — an IGS site with no TOS record at all."""
    rc = _run_tosgps(monkeypatch, ["station", "show", "BRST"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Berustaðir" not in out


@pytest.mark.vcr
def test_tosgps_resolves_the_gps_entity_when_a_marker_has_two(monkeypatch, capsys):
    """SOHO must reach 4416 (GPS), never 5356 (DOAS gas station).

    The gas station's own devices — scanners, spectrometer — must not appear.
    """
    rc = _run_tosgps(monkeypatch, ["device", "list", "--station", "SOHO"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "4416" in out
    assert "5356" not in out
    assert "doas_scanner" not in out


@pytest.mark.vcr
def test_every_audit_in_a_fan_out_run_grades_the_same_entity(monkeypatch, capsys):
    """The fan-out shape, which is the half a one-hop test cannot prove.

    `tos station triage SOHO` genuinely mixed entities: `verify-from-rinex`
    resolved via `_resolve_station_id` (basic_search → 4416) while every
    other audit resolved via `_resolve_station_entity` (live search → 5356),
    so ONE triage file described TWO stations. Resolving once at the boundary
    is what fixes that, and this pins it: the header's id must be the GPS
    station, and no DOAS device may appear in any section.
    """
    _run_tosgps(monkeypatch, ["station", "verify", "SOHO"])
    out = capsys.readouterr().out
    assert "id_entity=4416" in out
    assert "5356" not in out
    for doas_device in ("19530", "19531", "19532", "15562", "15565", "15569"):
        assert doas_device not in out, f"DOAS device {doas_device} leaked into SOHO"


@pytest.mark.vcr
def test_plain_tos_still_resolves_the_meteorological_station(capsys):
    """`tos` is the whole-TOS tool and must keep answering for every
    discipline — that is the entire reason `tosGPS` may narrow."""
    rc = tos_mod._dispatch(["station", "show", "VLFS"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Vífilsstaðir" in out


@pytest.mark.vcr
def test_tosgps_is_byte_identical_to_tos_on_a_single_candidate_station(
    monkeypatch, capsys
):
    """The invariant the `_PROFILED_ALIASES` comment block exists to protect."""
    plain_rc = tos_mod._dispatch(["station", "show", "VFLS"])
    plain = capsys.readouterr()

    gps_rc = _run_tosgps(monkeypatch, ["station", "show", "VFLS"])
    gated = capsys.readouterr()

    assert gps_rc == plain_rc
    assert gated.out == plain.out
    assert gated.err == plain.err


@pytest.mark.vcr
def test_an_explicit_id_bypasses_the_gate(monkeypatch, capsys):
    """`--id` is the operator naming an entity outright, so it is not
    filtered. Gating it would leave no way to reach a known entity."""
    rc = _run_tosgps(monkeypatch, ["audit", "station", "--id", "96"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "96" in out


# --------------------------------------------------------------------------
# The metadata path — a PREFERENCE, not a filter
# --------------------------------------------------------------------------


def test_station_metadata_prefers_the_gps_candidate():
    """`TOSClient.get_station_metadata` fed PrintTOS, the IGS site log and
    syncMeta from `stations[0]`.

    `domains="geophysical"` pins only the FIRST of the two levels, so within
    `geophysical` marker `soho` offered both 5356 (DOAS) and 4416 (GPS stöð)
    in an order nobody controls — and the gas station won. That reaches M3G
    and EPOS, which is why it is worth fixing even though a DOAS station has
    no receiver and the failure is eventually loud.
    """
    from tostools.api.tos_client import TOSClient

    client = TOSClient()
    with (
        patch.object(TOSClient, "search_stations", return_value=[SOHO_DOAS, SOHO_GPS]),
        patch.object(TOSClient, "_make_request", return_value={"stub": True}),
    ):
        station, _history = client.get_station_metadata("soho")

    assert station["id_entity"] == 4416


def test_station_metadata_is_a_preference_so_nothing_becomes_unreachable():
    """Unlike the station FILTER, this path must still answer for a marker
    with no GPS candidate at all — its callers never asked to be narrowed,
    and returning None would be a new failure mode rather than a fix."""
    from tostools.api.tos_client import TOSClient

    client = TOSClient()
    with (
        patch.object(TOSClient, "search_stations", return_value=[SOHO_DOAS]),
        patch.object(TOSClient, "_make_request", return_value={"stub": True}),
    ):
        station, _history = client.get_station_metadata("soho")

    assert station["id_entity"] == 5356


def test_prefer_domain_leaves_a_single_candidate_alone():
    """198 of the fleet's 202 resolvable markers have one candidate; for them
    this change must be a no-op."""
    from tostools.station_kind import prefer_domain

    assert prefer_domain([VFLS_GPS], ("geophysical",), "GPS stöð")["id_entity"] == 21832
    assert prefer_domain([], ("geophysical",), "GPS stöð") is None
