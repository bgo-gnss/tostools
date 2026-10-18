"""RINEX corrections select TOS sessions as [from, to), not closed intervals.

Offline, minimal session-shaped fixtures from AFST.after-18.json (2026-10-17)
periods and the AFST staging pilot's receiver/antenna identities. No live TOS
lookup or operational files are needed. June 3 is the operator-confirmed
installation calendar date, not evidence of an exact physical midnight swap.

Daily headers represent firmware at the FIRST observation. June 4/5, 2025
also contain later firmware transitions; the recorded boundaries are first-
observed evidence, not necessarily exact upgrade instants.
"""

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from tostools.rinex import corrector

_ANTENNA_CHANGE = datetime(2021, 6, 3)
_FW_55 = datetime(2025, 6, 4, 12, 22, 38)
_FW_56 = datetime(2025, 6, 5, 10, 17, 57)
_FORMS = ("iso", "datetime", "iso-z", "datetime-utc")


def _date_form(value, form):
    if value is None:
        return None
    if form == "iso":
        return value.isoformat()
    if form == "iso-z":
        return value.isoformat() + "Z"
    if form == "datetime-utc":
        return value.replace(tzinfo=timezone.utc)
    return value


def _metadata(form="iso", new_first=False):
    def session(start, end, firmware, antenna="SEPCHOKE_B3E6", serial="3070589"):
        return {
            "time_from": _date_form(start, form),
            "time_to": _date_form(end, form),
            "gnss_receiver": {
                "serial_number": serial,
                "model": "SEPT POLARX5",
                "firmware_version": firmware,
            },
            "antenna": {
                "serial_number": "antenna-afst-placeholder",
                "model": antenna,
                "antenna_height": "0.0000",
            },
            "radome": {"model": "NONE" if antenna == "TRM115000.10" else "SPKE"},
            "monument": {"monument_height": "0.0000"},
        }

    history = [
        session(datetime(2021, 5, 25), _ANTENNA_CHANGE, "5.4.0", "TRM115000.10"),
        session(_ANTENNA_CHANGE, _FW_55, "5.4.0"),
        session(_FW_55, _FW_56, "5.5.0"),
        session(_FW_56, datetime(2026, 3, 11, 13, 20), "5.6.0"),
        session(datetime(2026, 4, 29, 10, 23), None, "5.7.0", serial="4103679"),
    ]
    return {"device_history": list(reversed(history)) if new_first else history}


@pytest.fixture
def mock_tos(monkeypatch):
    fetch = Mock()
    monkeypatch.setattr(corrector, "gps_metadata", fetch)
    return fetch


def _assert_fields(corrections, firmware, antenna="SEPCHOKE_B3E6", serial="3070589"):
    assert corrections["REC # / TYPE / VERS"] == [serial, "SEPT POLARX5", firmware]
    radome = "NONE" if antenna == "TRM115000.10" else "SPKE"
    assert corrections["ANT # / TYPE"] == ["0000", f"{antenna:<15} {radome:<4}"]
    assert corrections["ANTENNA: DELTA H/E/N"] == [0.0, 0.0, 0.0]


@pytest.mark.parametrize(
    "boundary,old_fw,new_fw,old_antenna",
    [
        (_ANTENNA_CHANGE, "5.4.0", "5.4.0", "TRM115000.10"),
        (_FW_55, "5.4.0", "5.5.0", "SEPCHOKE_B3E6"),
        (_FW_56, "5.5.0", "5.6.0", "SEPCHOKE_B3E6"),
    ],
    ids=["antenna-june3", "firmware-5.5", "firmware-5.6"],
)
@pytest.mark.parametrize("form", _FORMS)
@pytest.mark.parametrize("new_first", [False, True], ids=["old-first", "new-first"])
@pytest.mark.parametrize("cached", [False, True], ids=["uncached", "cached"])
def test_transition_before_at_after(
    mock_tos, boundary, old_fw, new_fw, old_antenna, form, new_first, cached
):
    mock_tos.return_value = _metadata(form, new_first)
    cache = {} if cached else None
    for delta in (-1, 0, 1):
        observation = boundary + timedelta(microseconds=delta)
        if form in ("iso-z", "datetime-utc"):
            observation = observation.replace(tzinfo=timezone.utc)
        corrections = corrector._get_corrections_from_tos(
            "AFST", observation, logging.CRITICAL, tos_metadata_cache=cache
        )
        _assert_fields(
            corrections,
            old_fw if delta < 0 else new_fw,
            old_antenna if delta < 0 else "SEPCHOKE_B3E6",
        )
    assert mock_tos.call_count == (1 if cached else 3)


@pytest.mark.parametrize("form", _FORMS)
@pytest.mark.parametrize("end", [None, ""], ids=["null-end", "empty-end"])
@pytest.mark.parametrize("new_first", [False, True], ids=["old-first", "new-first"])
def test_open_ended_session(mock_tos, form, end, new_first):
    payload = _metadata(form, new_first)
    next(s for s in payload["device_history"] if s["time_to"] is None)["time_to"] = end
    mock_tos.return_value = payload
    corrections = corrector._get_corrections_from_tos(
        "AFST", datetime(2030, 1, 1), logging.CRITICAL
    )
    _assert_fields(corrections, "5.7.0", serial="4103679")


def test_prepopulated_cache_resolves_boundary_without_fetch(mock_tos):
    corrections = corrector._get_corrections_from_tos(
        "afst", _ANTENNA_CHANGE, logging.CRITICAL, {"AFST": _metadata()}
    )
    _assert_fields(corrections, "5.4.0")
    mock_tos.assert_not_called()


@pytest.mark.parametrize(
    "observation", [datetime(2020, 1, 1), datetime(2026, 3, 20)], ids=["before", "gap"]
)
@pytest.mark.parametrize("new_first", [False, True], ids=["old-first", "new-first"])
def test_no_covering_session_retains_last_list_entry_fallback(
    mock_tos, observation, new_first
):
    """Existing fallback is order-dependent, NOT a chronological latest lookup."""
    mock_tos.return_value = _metadata(new_first=new_first)
    corrections = corrector._get_corrections_from_tos(
        "AFST", observation, logging.CRITICAL
    )
    if new_first:
        _assert_fields(corrections, "5.4.0", "TRM115000.10")
    else:
        _assert_fields(corrections, "5.7.0", serial="4103679")


@pytest.mark.parametrize(
    "first_observation,firmware,later_boundary,later_firmware",
    [
        (datetime(2025, 6, 4, 11, 35, 45), "5.4.0", _FW_55, "5.5.0"),
        (datetime(2025, 6, 5), "5.5.0", _FW_56, "5.6.0"),
    ],
)
def test_daily_header_uses_first_observation_not_later_firmware(
    mock_tos, tmp_path, first_observation, firmware, later_boundary, later_firmware
):
    mock_tos.return_value = _metadata()
    cache = {}
    daily = corrector.resolve_corrections(
        tmp_path / "unused.rnx", "AFST", first_observation, tos_metadata_cache=cache
    )
    _assert_fields(daily, firmware)
    later = corrector.resolve_corrections(
        tmp_path / "unused.rnx", "AFST", later_boundary, tos_metadata_cache=cache
    )
    _assert_fields(later, later_firmware)
    # Cached metadata must not mutate prior corrections.
    _assert_fields(daily, firmware)
    mock_tos.assert_called_once()
