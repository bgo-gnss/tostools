"""Unit tests for :mod:`tostools.search` and the ``tos search`` CLI.

All TOS interaction is mocked — no network. The fake client mirrors the
two response shapes the search engine consumes:

- ``list_stations()`` → bulk ``/entity/search/station/geophysical/``
  objects (``attributes`` with ``code`` / ``value`` / ``date_from`` /
  ``date_to``);
- ``get_entity_history()`` → ``/history/entity/<id>/`` payloads
  (``children_connections`` for stations, open attributes for devices).
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional
from unittest.mock import patch

import pytest

from tostools.api.client_cache import SnapshotMiss
from tostools.search import (
    ANY_DEVICE,
    DeviceSpec,
    Predicate,
    UnknownStationCode,
    UnsupportedSelector,
    as_day,
    attribute_inventory,
    attribute_periods,
    coord_frame,
    coord_frame_names,
    device_in_namespace,
    device_values,
    devices_satisfy,
    filter_by_predicates,
    has_glob,
    history_values,
    known_station_codes,
    norm_value,
    open_value,
    parse_device_spec,
    parse_expression,
    parse_selector,
    predicate_matches,
    predicate_matches_devices,
    require_station_selector,
    search_stations,
    split_expression_list,
    text_matches,
    validate_station_codes,
    value_at,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _attr(code: str, value: str, *, date_to: Optional[str] = None) -> dict:
    return {
        "code": code,
        "value": value,
        "date_from": "2019-01-01T00:00:00",
        "date_to": date_to,
    }


def _station(
    sid: int,
    marker: str,
    name: str,
    *,
    in_epos: Optional[str] = None,
    domes: Optional[str] = None,
    extra: Optional[List[dict]] = None,
) -> dict:
    attrs: List[dict] = [
        _attr("marker", marker),
        _attr("name", name),
        _attr("subtype", "GPS stöð"),
    ]
    if in_epos is not None:
        attrs.append(_attr("in_network_epos", in_epos))
    if domes is not None:
        attrs.append(_attr("iers_domes_number", domes))
    attrs.extend(extra or [])
    return {
        "id_entity": sid,
        "code_entity_subtype": "geophysical",
        "attributes": attrs,
    }


def _device(
    did: int,
    subtype: str,
    model: Optional[str],
    serial: str = "123",
    *,
    extra: Optional[List[dict]] = None,
) -> dict:
    attrs: List[dict] = [
        _attr("serial_number", serial),
        _attr("status", "virkt"),
        _attr("date_start", "2020-01-01T00:00:00"),
    ]
    if model is not None:
        attrs.append(_attr("model", model))
    attrs.extend(extra or [])
    return {
        "id_entity": did,
        "code_entity_subtype": subtype,
        "attributes": attrs,
    }


class FakeClient:
    """Fake TOSClient — bulk listing + per-entity history."""

    def __init__(
        self,
        stations: List[dict],
        devices_by_station: Optional[Dict[int, List[dict]]] = None,
        contacts_by_station: Optional[Dict[int, List[dict]]] = None,
        visits_by_station: Optional[Dict[int, List[dict]]] = None,
    ):
        self._stations = stations
        self._devices = devices_by_station or {}
        self._contacts = contacts_by_station or {}
        self._visits = visits_by_station or {}

    def list_stations(self, domain: str = "geophysical") -> List[dict]:
        return self._stations

    def get_contacts(self, entity_id: int) -> List[dict]:
        return self._contacts.get(int(entity_id), [])

    def list_maintenance_visits(self, id_entity: int) -> List[dict]:
        return self._visits.get(int(id_entity), [])

    def get_entity_history(self, id_entity: int) -> Optional[dict]:
        # Station → children_connections (open joins only needed by engine)
        if id_entity in self._devices:
            conns = [
                {
                    "id_entity_child": d["id_entity"],
                    "id_entity_connection": 9000 + d["id_entity"],
                    "time_from": "2020-01-01T00:00:00",
                    "time_to": None,
                }
                for d in self._devices[id_entity]
            ]
            return {
                "id_entity": id_entity,
                "children_connections": conns,
                "attributes": [],
            }
        # Device entities: match by id across all stations
        for devices in self._devices.values():
            for d in devices:
                if d["id_entity"] == id_entity:
                    return d
        return None


# ---------------------------------------------------------------------------
# Value normalization
# ---------------------------------------------------------------------------


class TestNormValue:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("true", "true"),
            ("TRUE", "true"),
            ("Já", "true"),  # Icelandic yes
            ("já", "true"),
            ("yes", "true"),
            ("1", "true"),
            ("false", "false"),
            ("Nei", "false"),
            ("no", "false"),
            ("0", "false"),
            ("  Igneous  ", "igneous"),
            ("GPS stöð", "gps stöð"),
            (63.42, "63.42"),
        ],
    )
    def test_normalization(self, raw, expected):
        assert norm_value(raw) == expected


# ---------------------------------------------------------------------------
# Expression parsing
# ---------------------------------------------------------------------------


class TestParseExpression:
    def test_equality(self):
        pred = parse_expression("in_network_epos = true")
        assert pred == Predicate("in_network_epos", "=", "true")

    def test_inequality(self):
        pred = parse_expression("in_network_epos != true")
        assert pred.op == "!="

    def test_substring(self):
        pred = parse_expression("marker ~ ve")
        assert pred == Predicate("marker", "~", "ve")

    def test_substring_negated(self):
        pred = parse_expression("marker !~ ve")
        assert pred.op == "!~"

    def test_no_whitespace(self):
        pred = parse_expression("bedrock_type=igneous")
        assert pred == Predicate("bedrock_type", "=", "igneous")

    def test_value_with_spaces(self):
        pred = parse_expression("subtype = GPS stöð")
        assert pred.value == "GPS stöð"

    def test_missing_operator_raises(self):
        with pytest.raises(ValueError, match="cannot parse"):
            parse_expression("in_network_epos true")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_expression("")

    def test_code_normalized_lower(self):
        pred = parse_expression("In_Network_EPOS = true")
        assert pred.code == "in_network_epos"


# ---------------------------------------------------------------------------
# Text matching — substring / glob / regex
# ---------------------------------------------------------------------------


class TestTextMatches:
    """A ``~`` term picks its own semantics; nothing is reinterpreted."""

    def test_plain_substring_unchanged(self):
        assert text_matches("Reykjavík", "kjav")
        assert not text_matches("Reykjavík", "zzz")

    def test_plain_is_case_insensitive(self):
        assert text_matches("HVEL", "hve")
        assert text_matches("hvel", "HVE")

    def test_boolean_folding_survives(self):
        # norm_value folds já/nei onto true/false on BOTH sides — the
        # property a raw regex .search() would have silently dropped.
        assert text_matches("já", "true")
        assert text_matches("true", "já")

    def test_dot_stays_literal(self):
        # The regression this design exists to prevent: '.' must not
        # become "any character" just because it looks like regex.
        assert text_matches("a.b", "a.b")
        assert not text_matches("axb", "a.b")

    def test_pipe_stays_literal(self):
        assert not text_matches("vík", "vík|dal")
        assert text_matches("vík|dal", "vík|dal")

    def test_glob_is_anchored(self):
        # 'HVE*' is starts-with, NOT contains — otherwise the glob would
        # be indistinguishable from the plain substring path.
        assert text_matches("HVEL", "HVE*")
        assert not text_matches("XHVEL", "HVE*")

    def test_glob_substring_needs_stars(self):
        assert text_matches("XHVELY", "*hvel*")

    def test_glob_question_mark_is_one_char(self):
        assert text_matches("HVEL", "HVE?")
        assert not text_matches("HVELX", "HVE?")

    def test_regex_requires_prefix(self):
        assert text_matches("vík", "re:vík|dal")
        assert text_matches("dal", "re:vík|dal")
        assert not text_matches("fjord", "re:vík|dal")

    def test_regex_is_unanchored(self):
        assert text_matches("Reykjavík", "re:kjav")

    def test_regex_empty_raises(self):
        with pytest.raises(ValueError, match="empty regex"):
            text_matches("anything", "re:")

    def test_regex_invalid_raises(self):
        with pytest.raises(ValueError, match="bad regex"):
            text_matches("anything", "re:[unclosed")

    def test_none_never_matches(self):
        assert not text_matches(None, "anything")

    def test_has_glob(self):
        assert has_glob("HVE*")
        assert has_glob("HVE?")
        assert has_glob("[abc]")
        assert not has_glob("plain")


class TestTextOpsThroughPredicates:
    """The ~ / !~ operators route through :func:`text_matches`."""

    def test_glob_filters_stations(self):
        stations = [
            _station(1, "HVEL", "Hvolsvöllur"),
            _station(2, "XHVE", "Annar"),
        ]
        kept = filter_by_predicates(stations, [Predicate("marker", "~", "hve*")])
        assert [open_value(s, "marker") for s in kept] == ["HVEL"]

    def test_regex_filters_stations(self):
        stations = [
            _station(1, "HVEL", "Hvolsvöllur"),
            _station(2, "REYK", "Reykjavík"),
            _station(3, "AKUR", "Akureyri"),
        ]
        kept = filter_by_predicates(
            stations, [Predicate("name", "~", "re:hvol|reykja")]
        )
        assert sorted(open_value(s, "marker") for s in kept) == ["HVEL", "REYK"]

    def test_negated_glob(self):
        stations = [_station(1, "HVEL", "A"), _station(2, "AKUR", "B")]
        kept = filter_by_predicates(stations, [Predicate("marker", "!~", "hve*")])
        assert [open_value(s, "marker") for s in kept] == ["AKUR"]

    def test_absent_attribute_matches_only_negation(self):
        st = _station(1, "HVEL", "A")  # no iers_domes_number
        assert not predicate_matches(st, Predicate("iers_domes_number", "~", "1*"))
        assert predicate_matches(st, Predicate("iers_domes_number", "!~", "1*"))


# ---------------------------------------------------------------------------
# Selectors — station (bare) vs device (dotted)
# ---------------------------------------------------------------------------


class TestParseSelector:
    def test_bare_code_is_station(self):
        assert parse_selector("iers_domes_number") == (None, "iers_domes_number")

    def test_bare_code_lowercased(self):
        assert parse_selector("IERS_Domes_Number") == (None, "iers_domes_number")

    def test_device_namespace_canonicalized(self):
        assert parse_selector("receiver.firmware_version") == (
            "gnss_receiver",
            "firmware_version",
        )

    def test_alias_and_canonical_agree(self):
        assert parse_selector("receiver.model") == parse_selector("gnss_receiver.model")

    def test_star_namespace(self):
        assert parse_selector("*.model") == (ANY_DEVICE, "model")

    def test_namespace_case_insensitive(self):
        assert parse_selector("Receiver.Firmware_Version") == (
            "gnss_receiver",
            "firmware_version",
        )

    def test_unknown_namespace_raises_and_lists_valid(self):
        with pytest.raises(ValueError, match="unknown device namespace"):
            parse_selector("widget.model")

    def test_missing_attribute_after_dot_raises(self):
        with pytest.raises(ValueError, match="no attribute after the dot"):
            parse_selector("receiver.")


class TestDeviceSelectorRefused:
    """Slice 1 parses the namespace but must refuse to resolve it."""

    def test_require_station_selector_passes_for_station(self):
        require_station_selector(None, "marker")  # no raise

    def test_require_station_selector_raises_for_device(self):
        with pytest.raises(UnsupportedSelector, match="bulk station listing"):
            require_station_selector("gnss_receiver", "receiver.firmware_version")

    def test_unsupported_selector_is_a_value_error(self):
        # The CLI turns ValueError into exit 2; the subclass must inherit
        # that path rather than escaping as an unhandled exception.
        assert issubclass(UnsupportedSelector, ValueError)

    def test_expression_with_device_selector_parses(self):
        pred = parse_expression("receiver.firmware_version = 5.7.0")
        assert pred.namespace == "gnss_receiver"
        assert pred.code == "firmware_version"
        assert pred.value == "5.7.0"

    def test_predicate_matches_refuses_device_namespace(self):
        # Backstop: even if a Predicate is built directly, evaluating it
        # against the bulk listing (which has no devices) must not
        # silently return False.
        st = _station(1, "HVEL", "A")
        with pytest.raises(UnsupportedSelector):
            predicate_matches(
                st,
                Predicate("firmware_version", "=", "5.7.0", namespace="gnss_receiver"),
            )

    def test_station_predicate_still_evaluates(self):
        st = _station(1, "HVEL", "A", in_epos="true")
        assert predicate_matches(st, Predicate("in_network_epos", "=", "true"))


class TestPredicateSelectorRendering:
    def test_describe_bare_code(self):
        assert Predicate("marker", "~", "ve").describe() == "marker ~ ve"

    def test_describe_dotted_selector(self):
        pred = Predicate("firmware_version", "=", "5.7.0", namespace="gnss_receiver")
        assert pred.describe() == "gnss_receiver.firmware_version = 5.7.0"

    def test_selector_property(self):
        assert Predicate("marker", "=", "x").selector == "marker"
        assert (
            Predicate("model", "=", "x", namespace="antenna").selector
            == "antenna.model"
        )


# ---------------------------------------------------------------------------
# Device selectors — resolution against joined devices
# ---------------------------------------------------------------------------


def _receiver(did, model="POLARX5", fw=None, sw=None, serial="123"):
    extra = []
    if fw is not None:
        extra.append(_attr("firmware_version", fw))
    if sw is not None:
        extra.append(_attr("software_version", sw))
    return _device(did, "gnss_receiver", model, serial, extra=extra)


def _walked(*devices) -> List[dict]:
    """Devices in the shape :func:`station_open_devices` returns them."""
    return [
        {
            "id_entity": d["id_entity"],
            "serial": open_value(d, "serial_number") or "?",
            "model": open_value(d, "model"),
            "subtype": d["code_entity_subtype"],
            "status": open_value(d, "status") or "—",
            "since": "2020-01-01T00:00:00",
            "attributes": d["attributes"],
        }
        for d in devices
    ]


class TestDeviceNamespace:
    def test_exact_subtype(self):
        d = _walked(_receiver(1))[0]
        assert device_in_namespace(d, "gnss_receiver")
        assert not device_in_namespace(d, "antenna")

    def test_star_matches_everything(self):
        d = _walked(_receiver(1))[0]
        assert device_in_namespace(d, ANY_DEVICE)


class TestDeviceValues:
    def test_collects_one_entry_per_matching_device(self):
        devices = _walked(
            _receiver(1, fw="5.7.0"),
            _receiver(2, fw="5.6.0"),
            _device(3, "antenna", "TRM59800.00"),
        )
        assert device_values(devices, "gnss_receiver", "firmware_version") == [
            "5.7.0",
            "5.6.0",
        ]

    def test_none_for_device_without_the_attribute(self):
        devices = _walked(_receiver(1, fw="5.7.0"), _receiver(2))
        assert device_values(devices, "gnss_receiver", "firmware_version") == [
            "5.7.0",
            None,
        ]

    def test_empty_when_no_device_of_subtype(self):
        devices = _walked(_device(1, "antenna", "TRM59800.00"))
        assert device_values(devices, "gnss_receiver", "firmware_version") == []


class TestPredicateMatchesDevices:
    def _two_receivers(self):
        return _walked(_receiver(1, fw="5.7.0"), _receiver(2, fw="5.6.0"))

    def test_equality_existential(self):
        devices = self._two_receivers()
        assert predicate_matches_devices(
            devices,
            Predicate("firmware_version", "=", "5.6.0", namespace="gnss_receiver"),
        )

    def test_equality_no_match(self):
        devices = self._two_receivers()
        assert not predicate_matches_devices(
            devices,
            Predicate("firmware_version", "=", "9.9.9", namespace="gnss_receiver"),
        )

    def test_inequality_is_existential_not_universal(self):
        # Documented semantics: 'has a receiver that is NOT 5.7.0'. With a
        # 5.7.0 and a 5.6.0 joined, that holds.
        devices = self._two_receivers()
        assert predicate_matches_devices(
            devices,
            Predicate("firmware_version", "!=", "5.7.0", namespace="gnss_receiver"),
        )

    def test_glob_on_device_attribute(self):
        devices = self._two_receivers()
        assert predicate_matches_devices(
            devices,
            Predicate("firmware_version", "~", "5.7*", namespace="gnss_receiver"),
        )
        assert not predicate_matches_devices(
            devices,
            Predicate("firmware_version", "~", "4.*", namespace="gnss_receiver"),
        )

    def test_null_tests_presence(self):
        without = _walked(_receiver(1))
        assert predicate_matches_devices(
            without,
            Predicate("firmware_version", "=", "null", namespace="gnss_receiver"),
        )
        assert not predicate_matches_devices(
            without,
            Predicate("firmware_version", "!=", "null", namespace="gnss_receiver"),
        )

    def test_absent_device_only_satisfies_negation(self):
        none_joined = _walked(_device(1, "antenna", "TRM59800.00"))
        eq = Predicate("firmware_version", "=", "5.7.0", namespace="gnss_receiver")
        ne = Predicate("firmware_version", "!=", "5.7.0", namespace="gnss_receiver")
        assert not predicate_matches_devices(none_joined, eq)
        assert predicate_matches_devices(none_joined, ne)

    def test_star_namespace_spans_subtypes(self):
        devices = _walked(
            _receiver(1, model="POLARX5"), _device(2, "antenna", "TRM59800.00")
        )
        assert predicate_matches_devices(
            devices, Predicate("model", "~", "trm*", namespace=ANY_DEVICE)
        )

    def test_in_list(self):
        devices = self._two_receivers()
        assert predicate_matches_devices(
            devices,
            Predicate(
                "firmware_version",
                "in",
                values=("5.6.0", "9.9.9"),
                namespace="gnss_receiver",
            ),
        )


# ---------------------------------------------------------------------------
# Time — --at (point in time) and --history (span)
# ---------------------------------------------------------------------------


def _period(code, value, date_from, date_to=None):
    return {
        "code": code,
        "value": value,
        "date_from": f"{date_from}T00:00:00",
        "date_to": f"{date_to}T00:00:00" if date_to else None,
    }


def _chained_receiver(did=1, code="firmware_version"):
    """A receiver whose chain is 5.1.2 -> 5.3.0 -> 5.6.0 (open)."""
    return {
        "id_entity": did,
        "code_entity_subtype": "gnss_receiver",
        "attributes": [
            _period("model", "SEPT POLARX5", "2017-07-12"),
            _period(code, "5.1.2", "2017-07-12", "2019-10-16"),
            _period(code, "5.3.0", "2019-10-16", "2021-04-15"),
            _period(code, "5.6.0", "2021-04-15"),
        ],
    }


class TestAsDay:
    def test_truncates_timestamp(self):
        assert as_day("2019-10-15T00:00:00") == "2019-10-15"

    def test_none_stays_none(self):
        assert as_day(None) is None


class TestAttributePeriods:
    def test_sorted_oldest_first(self):
        rx = _chained_receiver()
        vals = [p["value"] for p in attribute_periods(rx, "firmware_version")]
        assert vals == ["5.1.2", "5.3.0", "5.6.0"]

    def test_open_period_has_none_date_to(self):
        rx = _chained_receiver()
        assert attribute_periods(rx, "firmware_version")[-1]["date_to"] is None


class TestValueAt:
    @pytest.mark.parametrize(
        "day,expected",
        [
            ("2018-01-01", "5.1.2"),
            ("2019-10-15", "5.1.2"),  # day before the boundary
            ("2019-10-16", "5.3.0"),  # boundary belongs to the NEW period
            ("2020-06-01", "5.3.0"),
            ("2021-04-15", "5.6.0"),
            ("2030-01-01", "5.6.0"),  # open period runs forever
        ],
    )
    def test_period_covering_the_day(self, day, expected):
        assert value_at(_chained_receiver(), "firmware_version", day) == expected

    def test_before_the_chain_is_none(self):
        assert value_at(_chained_receiver(), "firmware_version", "2010-01-01") is None

    def test_none_means_open_period(self):
        rx = _chained_receiver()
        assert value_at(rx, "firmware_version") == "5.6.0"
        assert value_at(rx, "firmware_version") == open_value(rx, "firmware_version")


class TestHistoryValues:
    def test_every_value_ever_held(self):
        assert history_values(_chained_receiver(), "firmware_version") == [
            "5.1.2",
            "5.3.0",
            "5.6.0",
        ]


class TestHistoryFiltering:
    """The regression that motivated the whole feature.

    A chain whose OPEN period agrees can still have diverged history. A
    predicate must find it with history=True and miss it without — if
    --history quietly evaluated the open period, this passes silently and
    the bug returns.
    """

    def _station(self):
        return _station(1, "HVEL", "A", extra=[])

    def test_superseded_value_found_only_with_history(self):
        devices = _walked_raw(_chained_receiver())
        pred = Predicate("firmware_version", "=", "5.3.0", namespace="gnss_receiver")
        assert not predicate_matches_devices(devices, pred)
        assert predicate_matches_devices(devices, pred, history=True)

    def test_current_value_found_either_way(self):
        devices = _walked_raw(_chained_receiver())
        pred = Predicate("firmware_version", "=", "5.6.0", namespace="gnss_receiver")
        assert predicate_matches_devices(devices, pred)
        assert predicate_matches_devices(devices, pred, history=True)

    def test_at_selects_the_era(self):
        devices = _walked_raw(_chained_receiver())
        pred = Predicate("firmware_version", "=", "5.3.0", namespace="gnss_receiver")
        assert predicate_matches_devices(devices, pred, at="2020-06-01")
        assert not predicate_matches_devices(devices, pred, at="2018-01-01")

    def test_station_attribute_history(self):
        st = {
            "id_entity": 1,
            "code_entity_subtype": "geophysical",
            "attributes": [
                _period("marker", "HVEL", "2000-01-01"),
                _period("status", "old", "2000-01-01", "2020-01-01"),
                _period("status", "new", "2020-01-01"),
            ],
        }
        pred = Predicate("status", "=", "old")
        assert not predicate_matches(st, pred)
        assert predicate_matches(st, pred, history=True)
        assert predicate_matches(st, pred, at="2010-01-01")


def _walked_raw(*devices) -> List[dict]:
    """Raw device entities in walked shape, keeping full attributes."""
    return [
        {
            "id_entity": d["id_entity"],
            "serial": open_value(d, "serial_number") or "?",
            "model": open_value(d, "model"),
            "subtype": d["code_entity_subtype"],
            "status": open_value(d, "status") or "—",
            "since": "2017-07-12T00:00:00",
            "attributes": d["attributes"],
        }
        for d in devices
    ]


# ---------------------------------------------------------------------------
# open_value + predicate matching
# ---------------------------------------------------------------------------


class TestOpenValue:
    def test_returns_open_period(self):
        st = _station(1, "a", "A", extra=[_attr("note", "current")])
        assert open_value(st, "note") == "current"

    def test_skips_closed_period(self):
        st = _station(
            1, "a", "A", extra=[_attr("note", "old", date_to="2020-01-01T00:00:00")]
        )
        assert open_value(st, "note") is None

    def test_prefers_open_over_closed(self):
        st = _station(
            1,
            "a",
            "A",
            extra=[
                _attr("note", "old", date_to="2020-01-01T00:00:00"),
                _attr("note", "new"),
            ],
        )
        assert open_value(st, "note") == "new"

    def test_absent_code(self):
        st = _station(1, "a", "A")
        assert open_value(st, "bedrock_type") is None


class TestPredicateMatches:
    def test_epos_true_matches(self):
        st = _station(1, "a", "A", in_epos="true")
        assert predicate_matches(st, Predicate("in_network_epos", "=", "true"))

    def test_epos_ja_matches_true(self):
        st = _station(1, "a", "A", in_epos="Já")
        assert predicate_matches(st, Predicate("in_network_epos", "=", "true"))

    def test_epos_absent_counts_as_not_true(self):
        st = _station(1, "a", "A")  # no in_network_epos at all
        assert predicate_matches(st, Predicate("in_network_epos", "!=", "true"))
        assert not predicate_matches(st, Predicate("in_network_epos", "=", "true"))

    def test_null_presence(self):
        st = _station(1, "a", "A", domes="10217M001")
        assert predicate_matches(st, Predicate("iers_domes_number", "!=", "null"))
        assert not predicate_matches(st, Predicate("iers_domes_number", "=", "null"))

    def test_null_absence(self):
        st = _station(1, "a", "A")
        assert predicate_matches(st, Predicate("iers_domes_number", "=", "null"))

    def test_substring(self):
        st = _station(1, "VMEY", "Vestmannaeyjar")
        assert predicate_matches(st, Predicate("marker", "~", "vm"))
        assert predicate_matches(st, Predicate("name", "~", "manna"))
        assert not predicate_matches(st, Predicate("marker", "~", "xx"))

    def test_substring_case_insensitive(self):
        st = _station(1, "VMEY", "Vestmannaeyjar")
        assert predicate_matches(st, Predicate("name", "~", "VESTMANNA"))

    def test_substring_negated_on_absent(self):
        st = _station(1, "a", "A")
        assert predicate_matches(st, Predicate("name", "!~", "ve"))

    def test_closed_period_ignored(self):
        # EPOS flag closed in the past → station is NOT currently in EPOS
        st = _station(1, "a", "A")
        st["attributes"].append(
            _attr("in_network_epos", "true", date_to="2023-10-23T00:00:00")
        )
        assert not predicate_matches(st, Predicate("in_network_epos", "=", "true"))

    def test_filter_by_predicates_ands(self):
        fleet = [
            _station(1, "aa", "AA", in_epos="true", domes="10217M001"),
            _station(2, "bb", "BB", in_epos="true"),
            _station(3, "cc", "CC", in_epos="false"),
        ]
        preds = [
            Predicate("in_network_epos", "=", "true"),
            Predicate("iers_domes_number", "!=", "null"),
        ]
        out = filter_by_predicates(fleet, preds)
        assert [open_value(s, "marker") for s in out] == ["aa"]


# ---------------------------------------------------------------------------
# Device specs
# ---------------------------------------------------------------------------


class TestParseDeviceSpec:
    def test_type_and_model(self):
        spec = parse_device_spec("receiver:polarx5")
        assert spec == DeviceSpec(
            subtype="gnss_receiver", model="polarx5", negate=False
        )

    def test_bare_model(self):
        spec = parse_device_spec("teltonika")
        assert spec.subtype is None
        assert spec.model == "teltonika"

    def test_any_model(self):
        spec = parse_device_spec("router:any")
        assert spec == DeviceSpec(subtype="router", model=None, negate=False)

    def test_wildcard_star(self):
        spec = parse_device_spec("sim:*")
        assert spec.subtype == "sim_card"
        assert spec.model is None

    def test_case_insensitive_type(self):
        spec = parse_device_spec("ANTENNA:SEPCHOKE")
        assert spec.subtype == "antenna"
        assert spec.model == "sepchoke"

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="unknown device type"):
            parse_device_spec("fluxcapacitor:delorean")

    def test_negate_flag(self):
        spec = parse_device_spec("receiver:any", negate=True)
        assert spec.negate is True


class TestDevicesSatisfy:
    DEVICES = [
        {"subtype": "gnss_receiver", "model": "SEPT POLARX5", "serial": "1"},
        {"subtype": "router", "model": "Teltonika RUT240", "serial": "2"},
        {"subtype": "sim_card", "model": None, "serial": "3"},
    ]

    def test_has_receiver_model(self):
        spec = parse_device_spec("receiver:polarx5")
        assert devices_satisfy(self.DEVICES, [spec], [])

    def test_lacks_other_model(self):
        spec = parse_device_spec("receiver:netr9")
        assert not devices_satisfy(self.DEVICES, [spec], [])

    def test_no_model_on_sim(self):
        # sim_card entities carry no model attribute — a model predicate
        # must not match them, but a type-only spec must.
        assert not devices_satisfy(
            self.DEVICES, [parse_device_spec("sim:whatever")], []
        )
        assert devices_satisfy(self.DEVICES, [parse_device_spec("sim:any")], [])

    def test_must_not(self):
        assert not devices_satisfy(
            self.DEVICES, [], [parse_device_spec("router:any", negate=True)]
        )

    def test_no_receiver(self):
        assert not devices_satisfy(
            self.DEVICES, [], [parse_device_spec("receiver:any", negate=True)]
        )
        others = self.DEVICES[1:]
        assert devices_satisfy(
            others, [], [parse_device_spec("receiver:any", negate=True)]
        )

    def test_and_combination(self):
        must = [parse_device_spec("router:teltonika"), parse_device_spec("sim:any")]
        assert devices_satisfy(self.DEVICES, must, [])


def _attr_meta(code: str, value: str, **meta) -> dict:
    """Attribute row carrying the TOS metadata fields the bulk listing has."""
    row = _attr(code, value)
    row.update(
        {
            "attribute_datatype_code": "text",
            "name_is": {"continuity": "Samfella"}.get(code, code.title()),
            "description_en": None,
            "python_constraint": ".*",
        }
    )
    row.update(meta)
    return row


# ---------------------------------------------------------------------------
# Attribute discovery (—-attribute-list / --allowed-values)
# ---------------------------------------------------------------------------


class TestAttributeInventory:
    def _fleet(self):
        return [
            {
                "id_entity": 1,
                "attributes": [
                    _attr_meta("continuity", "continuous"),
                    _attr_meta("marker", "aaa"),
                ],
            },
            {
                "id_entity": 2,
                "attributes": [
                    _attr_meta("continuity", "continuous"),
                    _attr_meta("continuity", "samfelld", date_to="2020-01-01T00:00:00"),
                    _attr_meta("marker", "bbb"),
                ],
            },
            {"id_entity": 3, "attributes": [_attr_meta("marker", "ccc")]},
        ]

    def test_counts_distinct_stations_per_code(self):
        inv = attribute_inventory(self._fleet())
        by_code = {i.code: i for i in inv}
        assert by_code["marker"].station_count == 3
        assert by_code["continuity"].station_count == 2

    def test_values_count_stations_not_periods(self):
        # Station 2 carries continuous (open) AND a closed samfelld — both
        # observed; 'continuous' still counts 2 distinct stations, not 3 rows.
        inv = attribute_inventory(self._fleet())
        cont = next(i for i in inv if i.code == "continuity")
        assert dict(cont.values)["continuous"] == 2
        assert dict(cont.values)["samfelld"] == 1

    def test_sorted_by_station_count_then_code(self):
        inv = attribute_inventory(self._fleet())
        assert [i.code for i in inv] == ["marker", "continuity"]

    def test_single_code_filter(self):
        inv = attribute_inventory(self._fleet(), code="continuity")
        assert len(inv) == 1 and inv[0].code == "continuity"
        assert attribute_inventory(self._fleet(), code="nope") == []

    def test_metadata_carried_through(self):
        inv = attribute_inventory(self._fleet(), code="continuity")
        assert inv[0].name_is == "Samfella"
        assert inv[0].datatype == "text"
        assert inv[0].constraint == ".*"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class TestSearchStations:
    def _fleet(self):
        return [
            _station(100, "veym", "Vestmannaeyjar", in_epos="true", domes="10217M001"),
            _station(101, "rhof", "Raufarhöfn", in_epos="true"),
            _station(102, "krac", "Krýsuvík", in_epos="false"),
            _station(103, "hhhh", "Hveravellir"),  # no epos attr
        ]

    def _devices(self):
        return {
            100: [
                _device(1, "gnss_receiver", "SEPT POLARX5"),
                _device(2, "router", "Teltonika RUT240"),
            ],
            101: [_device(3, "gnss_receiver", "TRIMBLE NETR9")],
            102: [_device(4, "antenna", "TRM57971.00")],
            103: [],
        }

    def test_attribute_only_no_walk(self):
        client = FakeClient(self._fleet())
        result = search_stations(client, [Predicate("in_network_epos", "=", "true")])
        assert result.device_filters_active is False
        assert [open_value(s, "marker") for s in result.stations] == ["rhof", "veym"]

    def test_device_filter_positive(self):
        client = FakeClient(self._fleet(), self._devices())
        result = search_stations(
            client,
            [Predicate("in_network_epos", "=", "true")],
            device_must=[parse_device_spec("receiver:polarx5")],
        )
        assert [open_value(s, "marker") for s in result.stations] == ["veym"]

    def test_device_filter_negated(self):
        client = FakeClient(self._fleet(), self._devices())
        result = search_stations(
            client,
            [],
            device_must_not=[parse_device_spec("receiver:any", negate=True)],
        )
        # krac (antenna only) + hhhh (nothing) — walk covers all stations
        # because no attribute predicate narrowed the set first.
        markers = [open_value(s, "marker") for s in result.stations]
        assert markers == ["hhhh", "krac"]

    def test_walk_only_survivors(self):
        client = FakeClient(self._fleet(), self._devices())
        result = search_stations(
            client,
            [Predicate("in_network_epos", "=", "true")],
            device_must_not=[parse_device_spec("router:any", negate=True)],
        )
        # Only rhof + veym were walked (epos=true); veym has a router.
        assert [open_value(s, "marker") for s in result.stations] == ["rhof"]
        assert set(result.devices_by_id) == {100, 101}

    def test_no_criteria_returns_all_sorted(self):
        client = FakeClient(self._fleet())
        result = search_stations(client, [])
        markers = [open_value(s, "marker") for s in result.stations]
        assert markers == sorted(markers)
        assert len(markers) == 4

    def test_attribute_codes_dedupe_predicate_and_show(self):
        client = FakeClient(self._fleet())
        result = search_stations(
            client,
            [Predicate("in_network_epos", "=", "true")],
            show_codes=["iers_domes_number", "in_network_epos"],
        )
        assert result.attribute_codes == ["in_network_epos", "iers_domes_number"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(argv: List[str], fleet, devices=None, capsys=None):
    from tostools.tos import _search_main

    fake = FakeClient(fleet, devices)
    with patch("tostools.api.tos_client.TOSClient", return_value=fake):
        rc = _search_main(argv)
    out = ""
    if capsys:
        captured = capsys.readouterr()
        out = captured.out + captured.err
    return rc, out


class TestSearchCli:
    def _fleet(self):
        return [
            _station(100, "VMEY", "Vestmannaeyjar", in_epos="true", domes="10217M001"),
            _station(101, "RHOF", "Raufarhöfn", in_epos="true"),
            _station(102, "KRAC", "Krýsuvík", in_epos="false"),
        ]

    def test_dispatch_through_main(self):
        from tostools.tos import main

        with patch("tostools.tos._search_main", return_value=0) as spy:
            rc = main(["search", "--epos"])
        assert rc == 0
        spy.assert_called_once_with(["--epos"])

    def test_epos_sugar_table(self, capsys):
        rc, out = _run_cli(["--epos"], self._fleet(), capsys=capsys)
        assert rc == 0
        assert "2 station(s) match" in out
        assert "VMEY" in out and "RHOF" in out
        assert "KRAC" not in out
        # predicate code becomes a column
        assert "IN_NETWORK_EPOS" in out

    def test_no_epos_markers_only(self, capsys):
        rc, out = _run_cli(
            ["--no-epos", "--markers-only"], self._fleet(), capsys=capsys
        )
        assert rc == 0
        assert out.split() == ["KRAC"]

    def test_glob_expression(self, capsys):
        rc, out = _run_cli(["marker ~ r*"], self._fleet(), capsys=capsys)
        assert rc == 0
        assert "RHOF" in out
        assert "KRAC" not in out  # anchored: 'r*' is starts-with

    def test_regex_expression(self, capsys):
        rc, out = _run_cli(
            ["marker ~ re:^(vmey|krac)$", "--markers-only"],
            self._fleet(),
            capsys=capsys,
        )
        assert rc == 0
        assert sorted(out.split()) == ["KRAC", "VMEY"]

    def _fleet_with_receivers(self):
        """VMEY on 5.7.0, RHOF on 5.6.0, KRAC with two receivers (MOHA-like)."""
        fleet = self._fleet()
        devices = {
            100: [_receiver(500, fw="5.7.0", sw="5.70", serial="3018426")],
            101: [_receiver(501, fw="5.6.0", sw="5.60", serial="3018451")],
            # The MOHA shape: a stale TRIMBLE join never closed alongside
            # the live Septentrio.
            102: [
                _device(502, "gnss_receiver", "TRIMBLE NETRS", "4921172738"),
                _receiver(503, fw="5.4.0", sw="5.40", serial="3070999"),
            ],
        }
        return fleet, devices

    def test_device_selector_filters(self, capsys):
        fleet, devices = self._fleet_with_receivers()
        rc, out = _run_cli(
            ["receiver.firmware_version = 5.7.0", "--markers-only"],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        assert out.split() == ["VMEY"]

    def test_device_selector_glob_filters(self, capsys):
        fleet, devices = self._fleet_with_receivers()
        rc, out = _run_cli(
            ["receiver.firmware_version ~ 5.7*", "--markers-only"],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        assert out.split() == ["VMEY"]

    def test_device_selector_projects_column(self, capsys):
        fleet, devices = self._fleet_with_receivers()
        rc, out = _run_cli(
            ["--show", "receiver.firmware_version"], fleet, devices, capsys=capsys
        )
        assert rc == 0
        assert "GNSS_RECEIVER.FIRMWARE_VERSION" in out
        assert "5.7.0" in out and "5.6.0" in out

    def test_show_accepts_comma_separated_selectors(self, capsys):
        # Asserted via --json: the table truncates headers to terminal
        # width, which would make this assertion about Rich, not parsing.
        fleet, devices = self._fleet_with_receivers()
        rc, out = _run_cli(
            [
                "marker = VMEY",
                "--show",
                "receiver.firmware_version,receiver.software_version",
                "--json",
            ],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        attrs = json.loads(out)["stations"][0]["device_attributes"]
        assert attrs["gnss_receiver.firmware_version"] == ["5.7.0"]
        assert attrs["gnss_receiver.software_version"] == ["5.70"]

    def test_two_receivers_both_render(self, capsys):
        """The MOHA case — a station with two open receiver joins.

        Collapsing it to one value is the exact defect this feature exists
        to prevent (the sweep script keyed by marker and silently dropped
        one), so both serials must appear in the cell.
        """
        fleet, devices = self._fleet_with_receivers()
        rc, out = _run_cli(
            ["marker = KRAC", "--show", "receiver.serial_number"],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        assert "4921172738" in out
        assert "3070999" in out

    def test_device_selector_json_shape(self, capsys):
        fleet, devices = self._fleet_with_receivers()
        rc, out = _run_cli(
            ["marker = VMEY", "--show", "receiver.firmware_version", "--json"],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        st = payload["stations"][0]
        assert st["device_attributes"]["gnss_receiver.firmware_version"] == ["5.7.0"]

    def test_json_does_not_leak_raw_attribute_lists(self, capsys):
        """The walk carries device 'attributes' internally; JSON must not."""
        fleet, devices = self._fleet_with_receivers()
        rc, out = _run_cli(
            ["--receiver", "polarx5", "--json"], fleet, devices, capsys=capsys
        )
        assert rc == 0
        payload = json.loads(out)
        emitted = [d for st in payload["stations"] for d in st["devices"]]
        assert emitted, "expected device summaries in the payload"
        for dev in emitted:
            assert "attributes" not in dev
            assert set(dev) == {
                "id_entity",
                "serial",
                "model",
                "subtype",
                "status",
                "since",
            }

    def _fleet_with_chain(self):
        """One station whose receiver chain is 5.1.2 -> 5.3.0 -> 5.6.0.

        The station's own attributes are dated from well BEFORE the receiver
        chain, as a real station is — the marker predates the hardware. A
        fixture with the marker starting mid-chain would make
        ``marker = HVOL`` fail on the early segments and wrongly look like a
        discriminating mark.
        """
        station = {
            "id_entity": 200,
            "code_entity_subtype": "geophysical",
            "attributes": [
                _period("marker", "HVOL", "1999-10-19"),
                _period("name", "Láguhvolar", "1999-10-19"),
                _period("subtype", "GPS stöð", "1999-10-19"),
            ],
        }
        return [station], {200: [_chained_receiver(600)]}

    def test_history_finds_a_superseded_value(self, capsys):
        """The done criterion in miniature: agreeing tip, diverged history."""
        fleet, devices = self._fleet_with_chain()
        # --no-cache on both: this test runs the CLI twice, and a warm-cache
        # note on the second run would land in the merged capture.
        rc, out = _run_cli(
            ["receiver.firmware_version = 5.3.0", "--markers-only", "--no-cache"],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        assert out.split() == [], "5.3.0 is not current — must not match today"

        rc, out = _run_cli(
            [
                "receiver.firmware_version = 5.3.0",
                "--history",
                "--markers-only",
                "--no-cache",
            ],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        assert out.split() == ["HVOL"], "--history must find the superseded value"

    def test_at_selects_the_era(self, capsys):
        fleet, devices = self._fleet_with_chain()
        rc, out = _run_cli(
            [
                "marker = HVOL",
                "--at",
                "2020-06-01",
                "--show",
                "receiver.firmware_version",
                "--json",
            ],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        st = payload["stations"][0]
        assert st["device_attributes"]["gnss_receiver.firmware_version"] == ["5.3.0"]
        assert payload["criteria"]["at"] == "2020-06-01"

    def test_history_table_has_one_row_per_period(self, capsys):
        fleet, devices = self._fleet_with_chain()
        rc, out = _run_cli(
            ["marker = HVOL", "--history", "--show", "receiver.firmware_version"],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        assert "FROM" in out and "TO" in out
        for value in ("5.1.2", "5.3.0", "5.6.0"):
            assert value in out, f"{value} missing from the timeline"
        assert "2019-10-16" in out and "open" in out

    def test_history_json_carries_raw_periods(self, capsys):
        fleet, devices = self._fleet_with_chain()
        rc, out = _run_cli(
            [
                "marker = HVOL",
                "--history",
                "--show",
                "receiver.firmware_version",
                "--json",
            ],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        st = json.loads(out)["stations"][0]
        periods = st["device_periods"]["gnss_receiver.firmware_version"][0]
        assert [p["value"] for p in periods] == ["5.1.2", "5.3.0", "5.6.0"]
        assert periods[0]["date_from"] == "2017-07-12"
        assert periods[-1]["date_to"] is None

    def test_non_history_json_has_no_period_keys(self, capsys):
        fleet, devices = self._fleet_with_chain()
        rc, out = _run_cli(
            ["marker = HVOL", "--show", "receiver.firmware_version", "--json"],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        st = json.loads(out)["stations"][0]
        assert "device_periods" not in st
        assert "attribute_periods" not in st

    def test_marker_predicate_does_not_duplicate_the_column(self, capsys):
        """'marker = X' used to render MARKER twice — fixed and raw."""
        fleet, devices = self._fleet_with_chain()
        rc, out = _run_cli(
            ["marker = HVOL", "--history", "--show", "receiver.firmware_version"],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        header = next(ln for ln in out.splitlines() if "MARKER" in ln)
        assert header.count("MARKER") == 1

    def test_name_predicate_does_not_duplicate_the_column(self, capsys):
        rc, out = _run_cli(["name ~ Vest"], self._fleet(), capsys=capsys)
        assert rc == 0
        header = next(ln for ln in out.splitlines() if "MARKER" in ln)
        assert header.count("NAME") == 1

    def test_history_marks_the_matching_period(self, capsys):
        fleet, devices = self._fleet_with_chain()
        rc, out = _run_cli(
            [
                "receiver.firmware_version = 5.3.0",
                "--history",
                "--show",
                "receiver.firmware_version",
            ],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        assert "●" in out
        marked = [ln for ln in out.splitlines() if ln.rstrip().endswith("●")]
        assert len(marked) == 1, "exactly the 5.3.0 period should be marked"
        assert "5.3.0" in marked[0]

    def test_mark_column_suppressed_when_it_would_not_discriminate(self, capsys):
        """A column of solid dots is noise — 'marker = X' holds everywhere."""
        fleet, devices = self._fleet_with_chain()
        rc, out = _run_cli(
            ["marker = HVOL", "--history", "--show", "receiver.firmware_version"],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        assert "●" not in out

    def test_at_is_stated_in_the_criteria_line(self, capsys):
        """'0 station(s) match' means something different as-of a date."""
        fleet, devices = self._fleet_with_chain()
        rc, out = _run_cli(
            ["--at", "2018-01-01", "receiver.firmware_version = 5.7.0"],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        assert "0 station(s) match" in out
        assert "[as of 2018-01-01]" in out

    def test_history_is_stated_in_the_criteria_line(self, capsys):
        fleet, devices = self._fleet_with_chain()
        rc, out = _run_cli(["--history"], fleet, devices, capsys=capsys)
        assert rc == 0
        assert "[any period, ever]" in out

    def test_at_and_history_together_exits_2(self, capsys):
        rc, out = _run_cli(
            ["--at", "2020-01-01", "--history"], self._fleet(), capsys=capsys
        )
        assert rc == 2
        assert "mutually exclusive" in out

    def test_bad_at_date_exits_2(self, capsys):
        rc, out = _run_cli(["--at", "2026-13-45"], self._fleet(), capsys=capsys)
        assert rc == 2
        assert "not a valid YYYY-MM-DD" in out

    def test_cache_note_printed_on_a_warm_run(self, capsys):
        """Staleness is reported, never inferred — this output drives writes."""
        fleet, devices = self._fleet_with_receivers()
        _run_cli(["--receiver", "polarx5", "--markers-only"], fleet, devices)
        capsys.readouterr()
        rc, out = _run_cli(
            ["--receiver", "polarx5", "--markers-only"], fleet, devices, capsys=capsys
        )
        assert rc == 0
        assert "cache:" in out and "--refresh" in out

    def test_no_cache_prints_no_note(self, capsys):
        fleet, devices = self._fleet_with_receivers()
        _run_cli(["--receiver", "polarx5", "--markers-only"], fleet, devices)
        capsys.readouterr()
        rc, out = _run_cli(
            ["--receiver", "polarx5", "--markers-only", "--no-cache"],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        assert "cache:" not in out

    def test_snapshot_roundtrip(self, tmp_path, capsys):
        fleet, devices = self._fleet_with_receivers()
        snap = tmp_path / "fleet.json"
        rc, out = _run_cli(
            [
                "--receiver",
                "polarx5",
                "--show",
                "receiver.firmware_version",
                "--snapshot",
                str(snap),
                "--markers-only",
            ],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 0
        assert snap.exists()
        assert "snapshot written" in out

        # Replay against a client that would raise if touched.
        class Exploding:
            def list_stations(self, domain="geophysical"):
                raise AssertionError("replay must not reach the network")

            def get_entity_history(self, id_entity):
                raise AssertionError("replay must not reach the network")

        from tostools.tos import _search_main

        with patch("tostools.api.tos_client.TOSClient", return_value=Exploding()):
            rc = _search_main(
                [
                    "--receiver",
                    "polarx5",
                    "--show",
                    "receiver.firmware_version",
                    "--from-snapshot",
                    str(snap),
                    "--markers-only",
                ]
            )
        replayed = capsys.readouterr()
        assert rc == 0
        assert "NO live TOS read" in replayed.err
        assert "VMEY" in replayed.out

    def test_snapshot_and_from_snapshot_together_exits_2(self, tmp_path, capsys):
        fleet, devices = self._fleet_with_receivers()
        snap = tmp_path / "fleet.json"
        _run_cli(["--receiver", "polarx5", "--snapshot", str(snap)], fleet, devices)
        capsys.readouterr()
        rc, out = _run_cli(
            [
                "--receiver",
                "polarx5",
                "--from-snapshot",
                str(snap),
                "--snapshot",
                str(tmp_path / "other.json"),
            ],
            fleet,
            devices,
            capsys=capsys,
        )
        assert rc == 2
        assert "no-op" in out

    def test_missing_snapshot_file_exits_2(self, tmp_path, capsys):
        rc, out = _run_cli(
            ["--from-snapshot", str(tmp_path / "nope.json")],
            self._fleet(),
            capsys=capsys,
        )
        assert rc == 2

    def test_unknown_namespace_in_show_exits_2(self, capsys):
        rc, out = _run_cli(
            ["--epos", "--show", "widget.model"], self._fleet(), capsys=capsys
        )
        assert rc == 2
        assert "unknown device namespace" in out

    def test_bare_show_code_still_works(self, capsys):
        rc, out = _run_cli(
            ["--epos", "--show", "iers_domes_number"], self._fleet(), capsys=capsys
        )
        assert rc == 0
        assert "IERS_DOMES_NUMBER" in out
        assert "10217M001" in out

    def test_expression_json(self, capsys):
        rc, out = _run_cli(
            ["in_network_epos = true", "--json"], self._fleet(), capsys=capsys
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["count"] == 2
        # No implicit subtype pin — plain `tos search` is unconstrained.
        assert payload["criteria"]["attributes"] == ["in_network_epos = true"]
        markers = {s["marker"] for s in payload["stations"]}
        assert markers == {"VMEY", "RHOF"}
        # show codes/predicate codes surfaced per station
        assert payload["stations"][0]["attributes"]["in_network_epos"] == "true"

    def test_device_filter_cli(self, capsys):
        devices = {
            100: [_device(1, "gnss_receiver", "SEPT POLARX5")],
            101: [_device(2, "gnss_receiver", "TRIMBLE NETR9")],
            102: [_device(3, "antenna", "TRM57971.00")],
        }
        rc, out = _run_cli(
            ["--epos", "--receiver", "polarx5", "--markers-only"],
            self._fleet(),
            devices=devices,
            capsys=capsys,
        )
        assert rc == 0
        assert out.split() == ["VMEY"]

    def test_bad_expression_usage_error(self, capsys):
        rc, _ = _run_cli(["nonsense expression here"], self._fleet(), capsys=capsys)
        assert rc == 2

    def test_bad_device_type_usage_error(self, capsys):
        rc, _ = _run_cli(
            ["--device", "warpcore:dilithium"], self._fleet(), capsys=capsys
        )
        assert rc == 2

    def test_limit(self, capsys):
        rc, out = _run_cli(
            ["--epos", "--markers-only", "--limit", "1"], self._fleet(), capsys=capsys
        )
        assert rc == 0
        assert out.split() == ["RHOF"]  # sorted by marker: rhof < veym

    def test_show_extra_column(self, capsys):
        rc, out = _run_cli(
            ["--epos", "--show", "iers_domes_number"], self._fleet(), capsys=capsys
        )
        assert rc == 0
        assert "IERS_DOMES_NUMBER" in out
        assert "10217M001" in out

    def test_zero_results_is_success(self, capsys):
        rc, out = _run_cli(["marker = zzzz"], self._fleet(), capsys=capsys)
        assert rc == 0
        assert "0 station(s) match" in out

    def test_no_criteria_lists_all(self, capsys):
        rc, out = _run_cli(["--markers-only"], self._fleet(), capsys=capsys)
        assert rc == 0
        assert out.split() == ["KRAC", "RHOF", "VMEY"]


class TestDiscoveryCli:
    def _fleet(self):
        return [
            {
                "id_entity": 1,
                "attributes": [
                    _attr_meta("marker", "aaa"),
                    _attr_meta("continuity", "continuous"),
                ],
            },
            {
                "id_entity": 2,
                "attributes": [
                    _attr_meta("marker", "bbb"),
                    _attr_meta("continuity", "campaign"),
                ],
            },
        ]

    def test_attribute_list(self, capsys):
        rc, out = _run_cli(["--attribute-list"], self._fleet(), capsys=capsys)
        assert rc == 0
        assert "marker" in out and "continuity" in out
        assert "2 attribute code(s) observed" in out
        # station counts rendered
        assert "Samfella" in out

    def test_allowed_values(self, capsys):
        rc, out = _run_cli(
            ["--attribute", "continuity", "--allowed-values"],
            self._fleet(),
            capsys=capsys,
        )
        assert rc == 0
        assert "Samfella" in out
        assert "continuous" in out and "campaign" in out
        assert "constraint" in out

    def test_allowed_values_without_attribute_is_usage_error(self, capsys):
        rc, _ = _run_cli(["--allowed-values"], self._fleet(), capsys=capsys)
        assert rc == 2

    def test_unknown_attribute_is_error(self, capsys):
        rc, out = _run_cli(
            ["--attribute", "warpfield", "--allowed-values"],
            self._fleet(),
            capsys=capsys,
        )
        assert rc == 1
        assert "never observed" in out  # stderr message


class TestActiveGpsFlag:
    def _fleet(self):
        def st(sid, marker, **extra_codes):
            attrs = [
                _attr("marker", marker),
                _attr("name", marker),
                _attr("subtype", "GPS stöð"),
            ]
            for code, val in extra_codes.items():
                attrs.append(_attr(code, val))
            return {"id_entity": sid, "attributes": attrs}

        return [
            st(1, "good", continuity="continuous", geological_characteristic="bedrock"),
            st(2, "ended", continuity="continuous", date_end="2017-01-01 00:00"),
            st(3, "ice", continuity="continuous", geological_characteristic="ice"),
            st(
                4,
                "campaign",
                continuity="campaign",
                geological_characteristic="bedrock",
            ),
            st(5, "nofill", geological_characteristic="bedrock"),  # no continuity
            st(6, "sil", continuity="continuous"),  # SIL: overwrite subtype below
        ]

    def test_flag_keeps_only_operational_gps(self, capsys):
        fleet = self._fleet()
        fleet[5]["attributes"][2]["value"] = "SIL stöð"  # station 6 is SIL
        rc, out = _run_cli(["--active-gps", "--markers-only"], fleet, capsys=capsys)
        assert rc == 0
        assert out.split() == ["GOOD"]

    def test_flag_predicates_visible_in_criteria(self, capsys):
        rc, out = _run_cli(["--active-gps", "--json"], self._fleet(), capsys=capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["criteria"]["attributes"] == [
            "subtype = GPS stöð",
            "date_end = null",
            "geological_characteristic != ice",
            "continuity = continuous",
        ]

    def test_flag_composes_with_expression(self, capsys):
        fleet = self._fleet()
        fleet[0]["attributes"].append(_attr("in_network_epos", "true"))
        rc, out = _run_cli(
            ["--active-gps", "--epos", "--markers-only"], fleet, capsys=capsys
        )
        assert rc == 0
        assert out.split() == ["GOOD"]


class TestSugarFlags:
    """The individual --discontinued / --continuous / --no-ice sugars."""

    def _fleet(self):
        def st(sid, marker, **extra_codes):
            attrs = [
                _attr("marker", marker),
                _attr("name", marker),
                _attr("subtype", "GPS stöð"),
            ]
            for code, val in extra_codes.items():
                attrs.append(_attr(code, val))
            return {"id_entity": sid, "attributes": attrs}

        return [
            st(1, "live", continuity="continuous", geological_characteristic="bedrock"),
            st(2, "ended", continuity="continuous", date_end="2017-01-01 00:00"),
            st(3, "ice", continuity="continuous", geological_characteristic="ice"),
            st(4, "camp", continuity="campaign", geological_characteristic="bedrock"),
            st(5, "nofill"),  # no continuity, no geology, no date_end
        ]

    def test_discontinued(self, capsys):
        rc, out = _run_cli(
            ["--discontinued", "--markers-only"], self._fleet(), capsys=capsys
        )
        assert rc == 0
        assert out.split() == ["ENDED"]

    def test_continuous_is_strict(self, capsys):
        # only recorded-continuous passes: nofill (unrecorded) and camp drop
        rc, out = _run_cli(
            ["--continuous", "--markers-only"], self._fleet(), capsys=capsys
        )
        assert rc == 0
        assert out.split() == ["ENDED", "ICE", "LIVE"]

    def test_campaign(self, capsys):
        rc, out = _run_cli(
            ["--campaign", "--markers-only"], self._fleet(), capsys=capsys
        )
        assert rc == 0
        assert out.split() == ["CAMP"]

    def test_no_ice_is_lenient(self, capsys):
        # ice drops; unrecorded geology (nofill) stays
        rc, out = _run_cli(["--no-ice", "--markers-only"], self._fleet(), capsys=capsys)
        assert rc == 0
        assert out.split() == ["CAMP", "ENDED", "LIVE", "NOFILL"]

    def test_compose(self, capsys):
        rc, out = _run_cli(
            ["--continuous", "--no-ice", "--markers-only"], self._fleet(), capsys=capsys
        )
        assert rc == 0
        assert out.split() == ["ENDED", "LIVE"]

    def test_predicates_in_criteria(self, capsys):
        rc, out = _run_cli(
            ["--discontinued", "--continuous", "--no-ice", "--json"],
            self._fleet(),
            capsys=capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["criteria"]["attributes"] == [
            "date_end != null",
            "continuity = continuous",
            "geological_characteristic != ice",
        ]


class TestListAndAllPredicates:
    """'code = [a, b]' list syntax and 'code = all' keyword."""

    @staticmethod
    def _st(subtype):
        return {
            "id_entity": 1,
            "attributes": [_attr("marker", "a"), _attr("subtype", subtype)],
        }

    def test_parse_list_in(self):
        pred = parse_expression("subtype = [GPS stöð, SIL stöð]")
        assert pred.op == "in"
        assert pred.values == ("GPS stöð", "SIL stöð")

    def test_parse_list_not_in(self):
        pred = parse_expression("subtype != [GPS stöð, SIL stöð]")
        assert pred.op == "not in"
        assert pred.values == ("GPS stöð", "SIL stöð")

    def test_parse_empty_list_raises(self):
        with pytest.raises(ValueError, match="empty list"):
            parse_expression("subtype = []")

    def test_list_on_substring_op_raises(self):
        with pytest.raises(ValueError, match="only supports"):
            parse_expression("subtype ~ [GPS stöð]")

    def test_in_match(self):
        assert predicate_matches(
            self._st("SIL stöð"), parse_expression("subtype = [GPS stöð, SIL stöð]")
        )

    def test_in_no_match(self):
        assert not predicate_matches(
            self._st("DOAS"), parse_expression("subtype = [GPS stöð, SIL stöð]")
        )

    def test_not_in_match(self):
        assert predicate_matches(
            self._st("DOAS"), parse_expression("subtype != [GPS stöð, SIL stöð]")
        )

    def test_not_in_absent_passes(self):
        assert predicate_matches(
            {"id_entity": 1, "attributes": [_attr("marker", "a")]},
            parse_expression("subtype != [GPS stöð]"),
        )

    def test_all_matches_everything(self):
        assert predicate_matches(self._st("DOAS"), parse_expression("subtype = all"))

    def test_not_all_matches_nothing(self):
        assert not predicate_matches(
            self._st("GPS stöð"), parse_expression("subtype != all")
        )

    def test_describe_list(self):
        assert (
            parse_expression("subtype = [GPS stöð, SIL stöð]").describe()
            == "subtype in [GPS stöð, SIL stöð]"
        )


class TestSubtypeDefault:
    """`tos search` applies NO subtype scope; `tosGPS search` pins one.

    Until 2026-08-24 this command inserted a silent `subtype = GPS stöð`,
    left over from before `tosGPS search` existed. That made the
    entity-layer/GPS-layer split untrue — the docs called this the general
    fleet query while it answered about 217 of 443 geophysical entities.
    """

    def _fleet(self):
        return [
            {
                "id_entity": 1,
                "attributes": [_attr("marker", "gps1"), _attr("subtype", "GPS stöð")],
            },
            {
                "id_entity": 2,
                "attributes": [_attr("marker", "sil1"), _attr("subtype", "SIL stöð")],
            },
            {
                "id_entity": 3,
                "attributes": [_attr("marker", "gas1"), _attr("subtype", "DOAS")],
            },
            {
                "id_entity": 4,
                "attributes": [_attr("marker", "gps2"), _attr("subtype", "GPS stöð")],
            },
        ]

    def test_default_spans_every_subtype(self, capsys):
        rc, out = _run_cli(["--markers-only"], self._fleet(), capsys=capsys)
        assert rc == 0
        assert out.split() == ["GAS1", "GPS1", "GPS2", "SIL1"]

    def test_no_subtype_predicate_is_injected(self, capsys):
        """Not merely 'the fleet is unfiltered' — nothing is ADDED."""
        rc, out = _run_cli(["--json"], self._fleet(), capsys=capsys)
        assert rc == 0
        assert json.loads(out)["criteria"]["attributes"] == []

    def test_a_profile_still_pins(self, capsys):
        """The pin moved to `tosGPS`; it did not disappear."""
        from tostools.search_selectors import Profile
        from tostools.tos import _search_main

        profile = Profile(
            name="GPS",
            subtype="GPS stöð",
            station={"marker": False, "subtype": False},
        )
        fake = FakeClient(self._fleet())
        with patch("tostools.api.tos_client.TOSClient", return_value=fake):
            rc = _search_main(["--markers-only"], profile=profile)
        out = capsys.readouterr().out
        assert rc == 0
        assert out.split() == ["GPS1", "GPS2"]

    def test_explicit_type(self, capsys):
        rc, out = _run_cli(
            ["subtype = SIL stöð", "--markers-only"], self._fleet(), capsys=capsys
        )
        assert rc == 0
        assert out.split() == ["SIL1"]

    def test_list_type(self, capsys):
        rc, out = _run_cli(
            ["subtype = [GPS stöð, SIL stöð]", "--markers-only"],
            self._fleet(),
            capsys=capsys,
        )
        assert rc == 0
        assert out.split() == ["GPS1", "GPS2", "SIL1"]

    def test_all(self, capsys):
        rc, out = _run_cli(
            ["subtype = all", "--markers-only"], self._fleet(), capsys=capsys
        )
        assert rc == 0
        assert out.split() == ["GAS1", "GPS1", "GPS2", "SIL1"]

    def test_all_is_now_a_no_op_not_a_scope_lift(self, capsys):
        """`subtype = all` kept working rather than becoming an error.

        It is evaluated as always-true by `values_satisfy`, so it still
        parses and still matches everything — it just no longer lifts
        anything. Existing scripts and habits keep working.
        """
        rc, out = _run_cli(
            ["subtype = all", "--markers-only"], self._fleet(), capsys=capsys
        )
        assert rc == 0
        assert out.split() == ["GAS1", "GPS1", "GPS2", "SIL1"]


class TestStationCodeValidation:
    """An unknown station code must FAIL, never answer.

    Before this gate, ``'typo = null'`` matched the whole fleet (every
    station "lacks" a code nothing carries) and ``'typo != null'``
    returned a clean ``0 station(s) match``. Both read as real answers,
    and this output drives live TOS writes.
    """

    def _fleet(self):
        return [
            _station(100, "VMEY", "Vestmannaeyjar", in_epos="true", domes="10217M001"),
            _station(101, "RHOF", "Raufarhöfn", in_epos="true"),
        ]

    # ---- the engine -----------------------------------------------------

    def test_observed_code_is_known(self):
        assert "in_network_epos" in known_station_codes(self._fleet())

    def test_catalog_vouches_for_an_unpopulated_code(self):
        """'code = null' — which stations LACK this — must stay askable.

        No station in the fleet carries ``close_date``, so observation
        alone cannot vouch for it; the catalog must.
        """
        known = known_station_codes(self._fleet(), catalog_codes=["close_date"])
        assert "close_date" in known

    def test_unknown_code_raises_before_filtering(self):
        with pytest.raises(UnknownStationCode) as exc:
            validate_station_codes(
                self._fleet(),
                [Predicate("zzz_not_a_code", "=", "null")],
                catalog_codes=[],
            )
        assert [c for c, _ in exc.value.unknown] == ["zzz_not_a_code"]

    def test_every_offender_is_named_at_once(self):
        with pytest.raises(UnknownStationCode) as exc:
            validate_station_codes(
                self._fleet(),
                [Predicate("foo", "=", "1"), Predicate("bar", "=", "2")],
                catalog_codes=[],
            )
        assert [c for c, _ in exc.value.unknown] == ["foo", "bar"]

    def test_near_miss_gets_a_suggestion(self):
        with pytest.raises(UnknownStationCode) as exc:
            validate_station_codes(
                self._fleet(), [Predicate("marke", "=", "x")], catalog_codes=[]
            )
        assert "marker" in exc.value.unknown[0][1]

    def test_device_selector_is_not_station_scope(self):
        """Dotted selectors have their own vocabulary — not checked here."""
        validate_station_codes(
            self._fleet(),
            [Predicate("firmware_version", "=", "5.7.0", namespace="gnss_receiver")],
            catalog_codes=[],
        )

    def test_show_selector_is_validated(self):
        with pytest.raises(UnknownStationCode):
            validate_station_codes(
                self._fleet(), [], ["zzz_not_a_code"], catalog_codes=[]
            )

    def test_empty_fleet_does_not_guess(self):
        """No fleet means no observations — refusing would be a false positive."""
        validate_station_codes([], [Predicate("anything", "=", "1")], catalog_codes=[])

    def test_missing_catalog_degrades_to_observed(self):
        with patch(
            "tostools.search_selectors.catalog_station_codes",
            side_effect=FileNotFoundError("no catalog"),
        ):
            known = known_station_codes(self._fleet())
        assert "marker" in known and "close_date" not in known

    # ---- the CLI --------------------------------------------------------

    def test_cli_refuses_with_exit_2(self, capsys):
        rc, out = _run_cli(["zzz_not_a_code = null"], self._fleet(), capsys=capsys)
        assert rc == 2
        assert "is not a geophysical station attribute" in out
        assert "--attribute-list" in out

    def test_cli_points_at_the_domain_the_code_lives_in(self, capsys):
        """An attribute that exists only on another domain is reported with
        its home domain, not left as a dead-end 'did you mean'.

        ``vhm_reference_number`` is now catalog-known and would validate
        cleanly (returning a silent 0-match), so we use a fleet-only
        hydrological code and mock the catalog to NOT know about it.
        """
        fleet = self._fleet()
        hydro = [
            dict(
                st, attributes=st["attributes"] + [_attr("vhm_reference_number", "123")]
            )
            for st in fleet
        ]

        from tostools.tos import _search_main

        class DomainFleetClient(FakeClient):
            def list_stations(self, domain: str = "geophysical"):
                return hydro if domain == "hydrological" else self._stations

        with patch(
            "tostools.api.tos_client.TOSClient", return_value=DomainFleetClient(fleet)
        ):
            with patch(
                "tostools.search_selectors.catalog_station_codes", return_value={}
            ):
                rc = _search_main(["vhm_reference_number != null"])
        out = capsys.readouterr().err
        assert rc == 2
        assert "vhm_reference_number" in out
        assert "hydrological" in out
        assert "--domain hydrological" in out

    def test_cli_typo_no_longer_matches_the_whole_fleet(self, capsys):
        """The regression this exists to prevent."""
        rc, out = _run_cli(
            ["zzz_not_a_code = null", "--markers-only"], self._fleet(), capsys=capsys
        )
        assert rc == 2
        assert "VMEY" not in out and "RHOF" not in out

    def test_cli_accepts_a_real_code(self, capsys):
        rc, out = _run_cli(
            ["in_network_epos = true", "--markers-only"], self._fleet(), capsys=capsys
        )
        assert rc == 0
        assert out.split() == ["RHOF", "VMEY"]

    def test_cli_sugar_expansion_is_not_refused(self, capsys):
        """--active-gps expands to 'date_end = null', which the hand-written
        catalog calls 'close_date'. The union must keep the flag working."""
        fleet = self._fleet()
        for st in fleet:
            st["attributes"].append(_attr("date_end", None))
        rc, _ = _run_cli(["--active-gps", "--markers-only"], fleet, capsys=capsys)
        assert rc == 0

    def test_snapshot_replay_validates_against_the_whole_fleet(self, tmp_path):
        """A narrow query still snapshots the FULL bulk listing.

        Filtering is client-side, so ``list_stations`` is recorded whole
        regardless of how few stations the query kept. Validation on replay
        therefore sees the same vocabulary as a live run. If snapshots ever
        start recording a filtered listing, replaying an unrelated predicate
        would refuse a real code — this pins the property that prevents it.
        """
        from tostools.api.client_cache import CachingClient, SnapshotClient

        fleet = self._fleet()
        cached = CachingClient(
            FakeClient(fleet), ttl=0, enabled=False, server="fake:443"
        )
        cached.list_stations(domain="geophysical")
        snap = tmp_path / "narrow.json"
        cached.write_snapshot(snap, criteria=["marker = vmey"], domain="geophysical")

        replayed = SnapshotClient.load(snap).list_stations(domain="geophysical")
        assert len(replayed) == len(fleet)
        assert known_station_codes(replayed, catalog_codes=[]) == known_station_codes(
            fleet, catalog_codes=[]
        )


class TestSnapshotMissPropagation:
    """A replay gap must abort the walk, never read as 'no devices/visits'.

    The walk loops isolate per-station *transport* failures (empty list + a
    warning) so one flaky station cannot sink a fleet search. A SnapshotMiss
    is different: it is deterministic — the snapshot genuinely lacks the
    data this query needs — so swallowing it would turn a wrong answer
    ("no contacts") into a real-looking result. It must propagate.
    """

    def _snapshot_without_walks(self, tmp_path):
        from tostools.api.client_cache import CachingClient, SnapshotClient

        fleet = [
            _station(100, "VMEY", "Vestmannaeyjar", in_epos="true"),
            _station(101, "RHOF", "Raufarhöfn", in_epos="true"),
        ]
        cached = CachingClient(FakeClient(fleet), ttl=0, enabled=False, server="f:1")
        cached.list_stations(domain="geophysical")
        path = tmp_path / "snap.json"
        cached.write_snapshot(path, criteria=["marker = vmey"])
        return SnapshotClient.load(path)

    def test_visit_walk_propagates_snapshot_miss(self, tmp_path):
        from tostools.search import walk_visits

        replay = self._snapshot_without_walks(tmp_path)
        with pytest.raises(SnapshotMiss, match="no visits"):
            walk_visits(replay, [100, 101])

    def test_contact_walk_propagates_snapshot_miss(self, tmp_path):
        from tostools.search import walk_contacts

        replay = self._snapshot_without_walks(tmp_path)
        with pytest.raises(SnapshotMiss, match="no contacts"):
            walk_contacts(replay, [100, 101])

    def test_device_walk_propagates_snapshot_miss(self, tmp_path):
        from tostools.search import walk_devices

        replay = self._snapshot_without_walks(tmp_path)
        with pytest.raises(SnapshotMiss, match="no history"):
            walk_devices(replay, [100, 101])


class TestProjectionColumns:
    """--show contact.* / --show visit.* project the walked values.

    They were filters-only before; the columns must render the matching
    contact field / visit field, deduped, without re-walking when absent.
    """

    def test_contact_and_visit_columns_extracted_from_show(self):
        from tostools.search import SearchResult

        r = SearchResult(
            show_codes=["contact.owner", "visit.type", "iers_domes_number"],
        )
        assert r.contact_columns == ["owner"]
        assert r.visit_columns == ["type"]

    def test_contact_values_role_scoped_and_deduped(self):
        from tostools.search import SearchResult

        r = SearchResult(
            contacts_by_id={
                100: [
                    {"role_is": "Eigandi stöðvar", "organization": "NATT"},
                    {"role_is": "Rekstraraðili", "organization": "NATT"},
                ]
            }
        )
        st = {"id_entity": 100}
        # role-scoped: owner only reads the owner contact's org
        assert r.contact_values(st, "owner") == ["NATT"]
        # any-contact field spans both roles, deduped to one NATT
        assert r.contact_values(st, "organization") == ["NATT"]
        assert r.contact_column_value(st, "owner") == "NATT"
        assert r.contact_column_value({"id_entity": 999}, "owner") == "—"

    def test_visit_values_flatten_and_dedupe(self):
        from tostools.search import SearchResult

        r = SearchResult(
            visits_by_id={
                100: [
                    {"work": "TOS reviewed", "maintenance_type": "remote"},
                    {"work": "firmware", "maintenance_type": "remote"},
                ]
            }
        )
        st = {"id_entity": 100}
        assert r.visit_values(st, "type") == ["remote"]
        assert r.visit_values(st, "work") == ["TOS reviewed", "firmware"]
        assert r.visit_column_value(st, "type") == "remote"
        assert r.visit_column_value({"id_entity": 999}, "type") == "—"

    def test_cli_projects_contact_and_visit_columns(self, capsys):

        fleet = self._fleet()
        fake = FakeClient(
            fleet,
            contacts_by_station={
                100: [
                    {
                        "role_is": "Eigandi stöðvar",
                        "organization": "Veðurstofa Íslands",
                    }
                ]
            },
            visits_by_station={
                100: [{"work": "TOS reviewed", "maintenance_type": "remote"}]
            },
        )
        with patch("tostools.api.tos_client.TOSClient", return_value=fake):
            from tostools.tos import _search_main

            rc = _search_main(
                ["marker = VMEY", "--show", "contact.owner", "--show", "visit.type"]
            )
        out = capsys.readouterr().out
        assert rc == 0
        assert "CONTACT.OWNER" in out
        assert "VISIT.TYPE" in out
        assert "Veðurstofa Íslands" in out
        assert "remote" in out

    def _fleet(self):
        return [
            _station(100, "VMEY", "Vestmannaeyjar", in_epos="true"),
            _station(101, "RHOF", "Raufarhöfn", in_epos="true"),
        ]


# ---------------------------------------------------------------------------
# Coordinate columns (--show coord.*, geofunc)
# ---------------------------------------------------------------------------

# REYK surveyed values (WGS 84) and their known transforms — see
# geofunc/tests/test_coords.py.
_REYK = {"lat": "64.138788", "lon": "-21.955490", "altitude": "93.028"}


def _station_with_coords(sid: int, marker: str, *, coords: Optional[dict] = None):
    extra = []
    for code in ("lat", "lon", "altitude"):
        if coords and code in coords:
            extra.append(_attr(code, coords[code]))
    return _station(sid, marker, marker.title(), extra=extra)


class TestCoordColumns:
    def _fleet(self):
        return [
            _station_with_coords(100, "REYK", coords=_REYK),
            _station(101, "GAPS", "No coords here"),  # lat/lon/altitude absent
        ]

    @staticmethod
    def _station_by_marker(result, marker):
        return next(s for s in result.stations if open_value(s, "marker") == marker)

    def test_engine_coord_columns_and_values(self):
        client = FakeClient(self._fleet())
        result = search_stations(client, [], coord_frames=["itrf2008", "isn2016"])
        assert result.coord_columns == [
            ("itrf2008", "x"),
            ("itrf2008", "y"),
            ("itrf2008", "z"),
            ("isn2016", "east"),
            ("isn2016", "north"),
            ("isn2016", "h"),
        ]
        reyk = self._station_by_marker(result, "REYK")
        assert result.coord_value(reyk, "itrf2008", "x") == pytest.approx(
            2587383.87, abs=0.05
        )
        assert result.coord_value(reyk, "itrf2008", "y") == pytest.approx(
            -1043033.57, abs=0.05
        )
        assert result.coord_value(reyk, "isn2016", "east") == pytest.approx(
            2556148.998, abs=0.01
        )
        assert result.coord_value(reyk, "isn2016", "north") == pytest.approx(
            207354.677, abs=0.01
        )

    def test_missing_coords_render_none(self):
        client = FakeClient(self._fleet())
        result = search_stations(client, [], coord_frames=["itrf2008"])
        gaps = self._station_by_marker(result, "GAPS")
        assert result.coord_value(gaps, "itrf2008", "x") is None

    def test_unknown_frame_raises(self):
        with pytest.raises(KeyError):
            coord_frame("bogus")

    def test_coord_frame_names(self):
        names = coord_frame_names()
        assert "itrf2008" in names and "isn2016" in names and "wgs84" in names

    def test_cli_table(self, capsys):
        rc, out = _run_cli(
            ["marker = REYK", "--show", "coords.itrf2008,coords.isn2016"],
            self._fleet(),
            capsys=capsys,
        )
        assert rc == 0
        # Rich truncates wide headers/values to the test terminal width, so
        # assert on the frame-name prefix + a short value that survives.
        assert "ITRF2008" in out
        assert "ISN2016" in out
        assert "2556148.998" in out

    def test_cli_singular_alias(self, capsys):
        # 'coord.' (singular) is accepted as an alias for 'coords.'.
        rc, out = _run_cli(
            ["marker = REYK", "--show", "coord.itrf2008"],
            self._fleet(),
            capsys=capsys,
        )
        assert rc == 0
        assert "ITRF2008" in out

    def test_cli_json(self, capsys):
        rc, out = _run_cli(
            ["marker = REYK", "--show", "coords.itrf2008", "--json"],
            self._fleet(),
            capsys=capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        coords = payload["stations"][0]["coordinates"]
        assert set(coords["itrf2008"]) == {"x", "y", "z"}
        assert coords["itrf2008"]["x"] == pytest.approx(2587383.87, abs=0.05)

    def test_cli_unknown_frame_rc2(self, capsys):
        rc, out = _run_cli(
            ["marker = REYK", "--show", "coords.bogus"],
            self._fleet(),
            capsys=capsys,
        )
        assert rc == 2
        assert "unknown coordinate frame" in out

    def test_cli_coord_in_expression_rc2(self, capsys):
        # coords.* is projection-only — it must not parse as a filter predicate.
        rc, out = _run_cli(
            ["coords.itrf2008 = 1"],
            self._fleet(),
            capsys=capsys,
        )
        assert rc == 2
        assert "unknown device namespace" in out

    def _epos_fleet_with_coords(self):
        return [
            _station(
                100,
                "REYK",
                "Reykjavík",
                in_epos="true",
                extra=[
                    _attr("lat", _REYK["lat"]),
                    _attr("lon", _REYK["lon"]),
                    _attr("altitude", _REYK["altitude"]),
                ],
            )
        ]

    def test_show_restricts_to_listed_columns(self, capsys):
        fleet = self._epos_fleet_with_coords()
        # --show restricts: predicate column suppressed, --show columns shown.
        rc, out = _run_cli(
            ["--epos", "--show", "coords.itrf2008"], fleet, capsys=capsys
        )
        assert rc == 0
        assert "IN_NETWORK_EPOS" not in out
        assert "ITRF2008" in out

    def test_no_show_still_projects_predicates(self, capsys):
        fleet = self._epos_fleet_with_coords()
        rc, out = _run_cli(["--epos"], fleet, capsys=capsys)
        assert rc == 0
        assert "IN_NETWORK_EPOS" in out

    def test_only_without_show_suppresses_predicates(self, capsys):
        fleet = self._epos_fleet_with_coords()
        rc, out = _run_cli(["--epos", "--only"], fleet, capsys=capsys)
        assert rc == 0
        assert "IN_NETWORK_EPOS" not in out

    def test_only_json_drops_predicate_attributes(self, capsys):
        fleet = self._epos_fleet_with_coords()
        rc, out = _run_cli(
            ["--epos", "--show", "coords.itrf2008", "--only", "--json"],
            fleet,
            capsys=capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        attrs = payload["stations"][0]["attributes"]
        assert "in_network_epos" not in attrs

    def test_ordered_columns_respect_show_order_no_auto_name(self):
        client = FakeClient(self._fleet())
        result = search_stations(
            client,
            [],
            show_codes=["marker"],
            coord_frames=["itrf2008"],
            show_order=[("coord", "itrf2008"), ("marker",)],
        )
        descs = result.ordered_columns
        assert [d[0] for d in descs] == ["coord", "coord", "coord", "marker"]
        headers = [result.column_header(d) for d in descs]
        assert headers == ["ITRF2008.X", "ITRF2008.Y", "ITRF2008.Z", "MARKER"]
        assert "NAME" not in headers

    def test_default_columns_without_show(self):
        client = FakeClient(self._fleet())
        result = search_stations(client, [Predicate("marker", "=", "REYK")])
        headers = [result.column_header(d) for d in result.ordered_columns]
        assert headers[0] == "MARKER" and headers[1] == "NAME"


class TestSplitExpressionList:
    def test_plain(self):
        assert split_expression_list("a = 1") == ["a = 1"]

    def test_comma_separated(self):
        assert split_expression_list(
            'visit.all !~ "EPOS disseminated", tectonic_plate ~ "Eurasia"'
        ) == ['visit.all !~ "EPOS disseminated"', 'tectonic_plate ~ "Eurasia"']

    def test_comma_in_quotes_stays(self):
        assert split_expression_list('name ~ "a, b"') == ['name ~ "a, b"']

    def test_list_value_stays(self):
        assert split_expression_list("subtype = [GPS stöð, SIL stöð]") == [
            "subtype = [GPS stöð, SIL stöð]"
        ]

    def test_empty_entries_dropped(self):
        assert split_expression_list("a = 1, , b = 2") == ["a = 1", "b = 2"]

    def test_unbalanced_quote_raises(self):
        import pytest

        with pytest.raises(ValueError):
            split_expression_list(
                'receiver.firmware != 5.7.0", receiver.model ~ "SEPT POLARX5"'
            )


class TestCommaSeparatedExpressionCli:
    def _fleet(self):
        return [
            _station(
                100,
                "VMEY",
                "Vestmannaeyjar",
                in_epos="true",
                extra=[_attr("tectonic_plate", "Eurasia")],
            ),
            _station(
                101,
                "RHOF",
                "Raufarhöfn",
                in_epos="true",
                extra=[_attr("tectonic_plate", "North America")],
            ),
        ]

    def test_two_filters_one_argument(self, capsys):
        rc, out = _run_cli(
            [
                'in_network_epos = true, tectonic_plate ~ "Eurasia"',
                "--show",
                "marker,name,tectonic_plate",
            ],
            self._fleet(),
            capsys=capsys,
        )
        assert rc == 0
        assert "VMEY" in out
        assert "RHOF" not in out


# ---------------------------------------------------------------------------
# Library call: search_from_argv (gpsplot map --tos-search)
# ---------------------------------------------------------------------------


class TestSearchFromArgv:
    def _fleet(self):
        sil = _station(3, "SILA", "Sílastöð", in_epos="true")
        sil["attributes"] = [
            a if a["code"] != "subtype" else _attr("subtype", "SIL stöð")
            for a in sil["attributes"]
        ]
        return [
            _station(1, "AKUR", "Akureyri", in_epos="true"),
            _station(2, "HVER", "Hveravellir", in_epos="false"),
            sil,
        ]

    def _search(self, argv, **kw):
        from tostools.tos import search_from_argv

        with patch(
            "tostools.api.tos_client.TOSClient", return_value=FakeClient(self._fleet())
        ):
            result = search_from_argv(["--no-cache", *argv], **kw)
        return sorted(open_value(s, "marker") for s in result.stations)

    def test_sugar_flags_and_expressions_as_on_the_command_line(self):
        assert self._search(["--epos"]) == ["AKUR", "SILA"]
        assert self._search(["--epos", "name ~ Akur*"]) == ["AKUR"]

    def test_default_subtype_unless_the_search_names_one(self):
        gps = {"default_subtype": "GPS stöð"}
        assert self._search(["--epos"], **gps) == ["AKUR"]
        assert self._search(["subtype = SIL stöð"], **gps) == ["SILA"]
        assert self._search(["subtype = all"], **gps) == ["AKUR", "HVER", "SILA"]

    def test_usage_errors_raise_instead_of_exiting(self, capsys):
        from tostools.tos import SearchUsage

        for argv, match in [
            (["--nope"], "unrecognized"),
            (["--json"], "--json"),
            (["--markers-only"], "--markers-only"),
            (["nmae = x"], "did you mean name"),
            (["--at", "2020-13-01"], "YYYY-MM-DD"),
        ]:
            with pytest.raises(SearchUsage, match=match):
                self._search(argv)
        assert capsys.readouterr().out == ""
