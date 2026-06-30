"""IGS site log antenna Up-Ecc = monument + antenna eccentricity (composite).

The site log's "Marker->ARP Up Ecc." must equal the RINEX header's
"ANTENNA: DELTA H". TOS stores the antenna eccentricity (monument-top -> ARP)
separately from the monument height (mark -> monument-top), and they live in
*different* device-history sessions, so the published value is the composite —
the same sum the RINEX-header path (gps_rinex) uses. These tests pin that and
the period-overlap monument lookup.
"""

from __future__ import annotations

from datetime import datetime

from tostools.core.site_log import (
    _generate_antenna_section,
    _monument_height_for_period,
)


def _up_ecc_values(section: str) -> list[float]:
    return [
        float(line.split(":")[1].strip())
        for line in section.splitlines()
        if "Marker->ARP Up Ecc" in line
    ]


def test_up_ecc_is_antenna_plus_monument_composite():
    # RHOF-shaped: one open monument (1.014) spanning two antenna periods.
    sessions = [
        {
            "time_from": datetime(2001, 7, 19),
            "time_to": None,
            "monument": {"monument_height": 1.014},
        },
        {
            "time_from": datetime(2001, 7, 19),
            "time_to": datetime(2012, 8, 28),
            "antenna": {
                "model": "ASH701945C_M",
                "serial_number": "x",
                "antenna_height": 0.0,
            },
        },
        {
            "time_from": datetime(2012, 8, 28),
            "time_to": None,
            "antenna": {
                "model": "TRM57971.00",
                "serial_number": "y",
                "antenna_height": -0.007,
            },
        },
    ]
    up = _up_ecc_values(_generate_antenna_section(sessions))
    # old antenna: 0.0 + 1.014; current antenna: -0.007 + 1.014 == RINEX DELTA H.
    assert up == [1.0140, 1.0070]


def test_no_monument_session_falls_back_to_eccentricity_only():
    sessions = [
        {
            "time_from": datetime(2012, 8, 28),
            "time_to": None,
            "antenna": {
                "model": "TRM57971.00",
                "serial_number": "y",
                "antenna_height": 1.5,
            },
        }
    ]
    assert _up_ecc_values(_generate_antenna_section(sessions)) == [1.5000]


def test_monument_height_for_period_picks_overlapping_monument():
    # Two monuments; the second replaces the first in 2015.
    sessions = [
        {
            "time_from": datetime(2000, 1, 1),
            "time_to": datetime(2015, 1, 1),
            "monument": {"monument_height": 1.0},
        },
        {
            "time_from": datetime(2015, 1, 1),
            "time_to": None,
            "monument": {"monument_height": 2.0},
        },
    ]
    # Antenna installed under the first monument.
    assert (
        _monument_height_for_period(
            sessions, datetime(2010, 1, 1), datetime(2014, 1, 1)
        )
        == 1.0
    )
    # Antenna installed under the second monument.
    assert _monument_height_for_period(sessions, datetime(2016, 1, 1), None) == 2.0


def test_monument_height_for_period_defaults_zero_when_absent():
    assert _monument_height_for_period([], datetime(2020, 1, 1), None) == 0.0
    # monument session present but with no height -> treated as absent.
    sessions = [
        {
            "time_from": datetime(2000, 1, 1),
            "time_to": None,
            "monument": {"monument_height": None},
        }
    ]
    assert _monument_height_for_period(sessions, datetime(2020, 1, 1), None) == 0.0


# --- Height of the Monument: catalog default 0.0, monument_height canonical ----

from tostools.core.site_log import _generate_site_identification  # noqa: E402


def _monument_height_line(section: str) -> str:
    for line in section.splitlines():
        if "Height of the Monument" in line:
            return line.split(":")[1].strip()
    return ""


def test_monument_height_uses_monument_height_code():
    sessions = [
        {
            "time_from": datetime(2001, 7, 19),
            "time_to": None,
            "monument": {"monument_height": 1.014},
        }
    ]
    sec = _generate_site_identification("RHOF", "Raufarhofn", "10216M001", {}, sessions)
    assert _monument_height_line(sec) == "1.014 m"


def test_monument_height_defaults_to_zero_when_no_monument_record():
    # No monument session (e.g. FIHO) -> catalog default 0.0, not empty "(m)".
    sessions = [
        {
            "time_from": datetime(2012, 8, 28),
            "time_to": None,
            "antenna": {
                "model": "NAX3G+C",
                "serial_number": "x",
                "antenna_height": 0.192,
            },
        }
    ]
    sec = _generate_site_identification("FIHO", "Fimmvorduhals", "", {}, sessions)
    assert _monument_height_line(sec) == "0.0 m"


def test_monument_height_legacy_antenna_height_fallback():
    # Old record misfiled the height under antenna_height on the monument entity.
    sessions = [
        {
            "time_from": datetime(1998, 9, 13),
            "time_to": None,
            "monument": {"antenna_height": 0.9237},
        }
    ]
    sec = _generate_site_identification("REYK", "Reykjavik", "", {}, sessions)
    assert _monument_height_line(sec) == "0.9237 m"
