"""correct_rinex_from_tos must not hammer TOS on batch/historical corrections.

The historical path fetches the date-independent gps_metadata(station) payload;
a caller-owned cache collapses a 365-day sweep to 1 TOS call per station. These
tests prove the call count directly (the answer to "are we taking TOS down?").
"""

from datetime import datetime
from unittest.mock import patch

from tostools.rinex import corrector


def _fake_meta():
    return {
        "id_entity": 1,
        "device_history": [
            {
                "time_from": "2012-08-28T00:00:00",
                "time_to": None,
                "gnss_receiver": {
                    "model": "TRIMBLE NETR9",
                    "serial_number": "5041K12345",
                    "firmware_version": "5.37",
                },
                "antenna": {
                    "model": "TRM57971.00",
                    "serial_number": "1440911234",
                    "antenna_height": 0.0,
                },
                "radome": {"model": "NONE"},
                "monument": {"monument_height": 1.0},
            }
        ],
    }


def _spy(returns):
    calls = {"n": 0}

    def fn(station, url, loglevel=0):  # gps_metadata(station_id, URL, loglevel=)
        calls["n"] += 1
        return returns

    return calls, fn


def test_cache_makes_one_fetch_across_many_dates():
    calls, fn = _spy(_fake_meta())
    cache: dict = {}
    with patch.object(corrector, "gps_metadata", fn):
        for day in range(1, 32):  # a month of daily corrections, same station
            corrector._get_corrections_from_tos(
                "RHOF", datetime(2013, 1, day), 20, tos_metadata_cache=cache
            )
    assert calls["n"] == 1, "cached station payload must be fetched exactly once"
    assert "RHOF" in cache


def test_without_cache_fetches_every_time():
    calls, fn = _spy(_fake_meta())
    with patch.object(corrector, "gps_metadata", fn):
        for day in range(1, 4):
            corrector._get_corrections_from_tos("RHOF", datetime(2013, 1, day), 20)
    assert calls["n"] == 3, "no cache → one fetch per call (legacy behaviour)"


def test_soft_miss_is_not_cached():
    """An empty/None result must NOT be frozen for the run — otherwise a single
    transient miss on file 1 would starve all later dates (and, in re-rinex, drop
    every file via the 0-corrections guard)."""
    calls, fn = _spy({})  # soft miss
    cache: dict = {}
    with patch.object(corrector, "gps_metadata", fn):
        for day in range(1, 4):
            corrector._get_corrections_from_tos(
                "RHOF", datetime(2013, 1, day), 20, tos_metadata_cache=cache
            )
    assert calls["n"] == 3, "empty result must re-fetch, never be cached"
    assert "RHOF" not in cache


def test_cache_key_is_station_case_insensitive():
    calls, fn = _spy(_fake_meta())
    cache: dict = {}
    with patch.object(corrector, "gps_metadata", fn):
        corrector._get_corrections_from_tos(
            "rhof", datetime(2013, 1, 1), 20, tos_metadata_cache=cache
        )
        corrector._get_corrections_from_tos(
            "RHOF", datetime(2013, 1, 2), 20, tos_metadata_cache=cache
        )
    assert calls["n"] == 1, "cache keyed on upper() → 'rhof' and 'RHOF' share it"
