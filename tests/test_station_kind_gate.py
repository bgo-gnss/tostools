"""`tosGPS` must refuse non-GPS stations, and `tos` must still find them.

The bug these lock down: the GPS audits narrow which **attributes** they
grade (``audit_missing_attributes`` skips every code whose
``gps_relevance != 'yes'``), so pointing them at a non-GPS station leaves
nothing to fail and they pass **vacuously**. ``tosGPS station verify VLFS``
reported ``✓ clean — 0 finding(s)`` for a 1963 precipitation gauge with zero
devices. A false PASS from the oracle is the actual danger.

Three live cases, each a different outcome, measured 2026-10-01:

* **VLFS** (id 96, *Vífilsstaðir*, ``meteorological`` / ``Úrkomustöð``) —
  must REFUSE. The reported bug.
* **BRST** — must REFUSE. BRST is *Brest, France*
  (``is_in_iceland = false``), an external IGS site with no TOS entity; its
  only candidate is id 646, *Berustaðir í Ásum*, an Icelandic weather
  station. Nobody had noticed.
* **SOHO** — must RESOLVE, to 4416. Marker ``soho`` carries **two**
  geophysical entities: 5356 (``DOAS``, volcanic gas) and 4416
  (``GPS stöð``, receiver 3075357). SOHO is the discriminating case: a
  resolve-then-refuse implementation refuses a perfectly valid GPS station
  here, while filtering candidates picks 4416. If this test passes while
  the VLFS tests also pass, the gate filters rather than post-checks.

And the invariant the whole design protects: on a single-candidate station
``tosGPS`` must be **byte-identical** to ``tos``, because
``gps-tos-corrections`` records 270 ``tos audit apply`` runs as operational
procedure and ``tos station`` appears ~150 times across the docs.

The unit tests below need no network. The dispatch-level tests are
cassette-backed (``pytest-recording``); re-record with
``pytest tests/test_station_kind_gate.py --record-mode=once``.
"""

import sys

import pytest

from tostools import tos as tos_mod
from tostools import tosGPS as tosgps_mod
from tostools.search_selectors import gps_profile
from tostools.station_kind import (
    AmbiguousStation,
    WrongStationKind,
    describe_entity,
    open_attribute,
    select_station,
)

# --------------------------------------------------------------------------
# Fixtures shaped like the live entities, so the unit tests below assert
# against the real data rather than a convenient simplification.
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
# The predicate — both levels, and why neither alone suffices
# --------------------------------------------------------------------------


def test_predicate_is_read_off_the_attribute_catalog():
    """Not hardcoded: both halves come from the catalog's `subtype` entry.

    ``applies_to`` gives the entity-type scope, ``default_value`` the
    subtype label — the same catalog ``gps_profile()`` already reads its
    station and device attribute sets from.
    """
    profile = gps_profile()
    assert profile.entity_scopes == ("geophysical",)
    assert profile.subtype == "GPS stöð"


def test_entity_type_alone_is_insufficient(predicate):
    """SIL and DOAS are `geophysical` too — the subtype attribute separates them."""
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


def test_an_absent_subtype_attribute_is_admitted(predicate):
    """Lenient on purpose, and load-bearing.

    ``subtype`` is itself audited by ``missing-attributes``. Requiring it
    here would make the gate pre-empt the audit that exists to report it,
    and would lock a real GPS station out of its own verify run for exactly
    the defect that run should surface.
    """
    assert open_attribute(NO_SUBTYPE, "subtype") is None
    assert predicate(NO_SUBTYPE) is True


def test_a_non_geophysical_entity_with_no_subtype_is_still_refused(predicate):
    """The one case where the entity-type level does the work by itself.

    Found by ``scripts/dev/mutate_station_gate.py``: deleting the entity-type
    check left every other test green, because ``Úrkomustöð != GPS stöð``
    catches VLFS and BRST on the subtype level alone. The type level only
    bites where the subtype attribute is ABSENT — and the absent-subtype
    leniency is deliberate, so without this check it would be a hole a
    meteorological or hydrological entity sails straight through.
    """
    bare_met = {
        "id_entity": 11,
        "code_entity_subtype": "meteorological",
        "attributes": [{"code": "name", "value": "Einhver stöð", "date_to": None}],
    }
    assert open_attribute(bare_met, "subtype") is None, "fixture must have no subtype"
    assert predicate(bare_met) is False


def test_a_closed_subtype_period_reads_as_absent(predicate):
    """Only the OPEN period counts; a closed one is history, not identity."""
    closed = {
        "id_entity": 7,
        "code_entity_subtype": "geophysical",
        "attributes": [{"code": "subtype", "value": "DOAS", "date_to": "2020-01-01"}],
    }
    assert open_attribute(closed, "subtype") is None
    assert predicate(closed) is True


# --------------------------------------------------------------------------
# select_station — filter candidates, never resolve-then-refuse
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


def test_refusal_names_what_was_actually_found(predicate):
    """A refusal an operator cannot check is one they will work around."""
    with pytest.raises(WrongStationKind) as exc:
        select_station("vlfs", [VLFS_MET], predicate)
    msg = str(exc.value)
    assert "VLFS" in msg
    assert "meteorological" in msg
    assert "Vífilsstaðir" in msg
    assert "id_entity=96" in msg
    assert "tos station show VLFS" in msg


def test_refusal_distinguishes_absent_from_wrong_kind(predicate):
    with pytest.raises(WrongStationKind) as exc:
        select_station("brst", [], predicate)
    assert "no entity carries marker" in str(exc.value)


def test_a_refusal_is_a_lookup_error(predicate):
    """Subclassing LookupError is what lets a refusal ride the lookup-miss
    path every caller already handles, instead of growing a second one."""
    assert issubclass(WrongStationKind, LookupError)
    assert issubclass(AmbiguousStation, LookupError)


def test_two_admitted_candidates_refuse_rather_than_guess(predicate):
    """Unreachable fleet-wide today — which is exactly why it raises.

    The day a second ``GPS stöð`` appears on one marker, silently taking the
    first would be the SOHO bug again, one level down.
    """
    twin = dict(SOHO_GPS, id_entity=5555)
    with pytest.raises(AmbiguousStation) as exc:
        select_station("soho", [SOHO_GPS, twin], predicate)
    assert "ambiguous" in str(exc.value)
    assert "--id" in str(exc.value)


def test_describe_entity_gives_the_three_facts_that_justify_a_refusal():
    out = describe_entity(VLFS_MET)
    assert "meteorological" in out and "Vífilsstaðir" in out and "id_entity=96" in out


# --------------------------------------------------------------------------
# Dispatch level — the wiring, not the helper
# --------------------------------------------------------------------------

#: Every `tosGPS` read verb that resolves a station marker. Parametrised so a
#: resolver wired in one verb and missed in a sibling fails here — the
#: "fix one, leave the sibling open" pattern this codebase has been bitten by
#: repeatedly.
REFUSING_VERBS = [
    pytest.param(["station", "show", "VLFS"], id="station-show"),
    pytest.param(["station", "verify", "VLFS"], id="station-verify"),
    pytest.param(["device", "list", "--station", "VLFS"], id="device-list"),
    pytest.param(["audit", "station", "VLFS"], id="audit-station"),
    pytest.param(
        ["audit", "missing-attributes", "VLFS"], id="audit-missing-attributes"
    ),
]


def _run_tosgps(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["tosGPS", *argv])
    return tosgps_mod._dispatch()


@pytest.mark.vcr
@pytest.mark.parametrize("argv", REFUSING_VERBS)
def test_tosgps_refuses_a_meteorological_station(monkeypatch, capsys, argv):
    rc = _run_tosgps(monkeypatch, argv)
    err = capsys.readouterr().err
    assert rc != 0, f"{argv} returned {rc} — a vacuous PASS is the bug"
    assert "is not a GPS station" in err
    assert "Vífilsstaðir" in err


@pytest.mark.vcr
def test_tosgps_refuses_a_marker_whose_only_entity_is_a_weather_station(
    monkeypatch, capsys
):
    """BRST is Brest, France — an IGS site with no TOS record at all."""
    rc = _run_tosgps(monkeypatch, ["station", "show", "BRST"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "Berustaðir í Ásum" in err


@pytest.mark.vcr
def test_tosgps_resolves_the_gps_entity_when_a_marker_has_two(monkeypatch, capsys):
    """SOHO: must reach 4416 (GPS), never 5356 (DOAS gas station).

    The DOAS station's own devices (scanners, spectrometer) must not appear.
    """
    rc = _run_tosgps(monkeypatch, ["device", "list", "--station", "SOHO"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "4416" in out
    assert "5356" not in out
    assert "doas_scanner" not in out


@pytest.mark.vcr
def test_plain_tos_still_resolves_the_meteorological_station(capsys):
    """`tos` is the whole-TOS tool and must keep answering for every
    discipline — the refusal message points operators here."""
    rc = tos_mod._dispatch(["station", "show", "VLFS"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Vífilsstaðir" in out


@pytest.mark.vcr
def test_tosgps_is_byte_identical_to_tos_on_a_single_candidate_station(
    monkeypatch, capsys
):
    """The invariant the `_PLAIN_ALIASES` comment block exists to protect."""
    plain_rc = tos_mod._dispatch(["station", "show", "VFLS"])
    plain = capsys.readouterr()

    gps_rc = _run_tosgps(monkeypatch, ["station", "show", "VFLS"])
    gated = capsys.readouterr()

    assert gps_rc == plain_rc
    assert gated.out == plain.out
    assert gated.err == plain.err


@pytest.mark.vcr
def test_an_explicit_id_bypasses_the_gate(monkeypatch, capsys):
    """`--id` is the operator naming an entity outright, and is the escape
    hatch the refusal message advertises. Gating it would make the message
    a lie."""
    rc = _run_tosgps(monkeypatch, ["audit", "station", "--id", "96"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "96" in out
