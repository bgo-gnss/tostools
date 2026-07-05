"""Site log v2.0 form + §11/§12/§13 agency sections.

Pins the IGS v2.0 fixes (9-char title in the RIGHT order, "Nine Character ID",
current instructions URL, §0 email) and the agency-section rendering the EPOS
dissemination layer drives via the ``agencies`` parameter. Also pins the
None-guard: TOS delivers present-but-None device fields (HAMR's antenna model),
which used to crash the fixed-width format specs.
"""

from __future__ import annotations

from datetime import datetime

from tostools.core.site_log import generate_igs_site_log

_STATION = {
    "marker": "rhof",
    "name": "Raufarhöfn",
    "iers_domes_number": "10216M001",
    "date_start": "2001-07-19T00:00:00",
    "lat": 66.461122,
    "lon": -15.946708,
    "altitude": 78.8,
    "geological_characteristic": "bedrock",
    "bedrock_type": "igneous",
    "bedrock_condition": "weathered",
    "is_near_fault_zones": "nei",
}

_SESSIONS = [
    {
        "time_from": datetime(2001, 7, 19),
        "time_to": None,
        "gnss_receiver": {
            "model": "TRIMBLE NETR9",
            "serial_number": "123",
            "firmware_version": "4.60",
        },
    },
    {
        "time_from": datetime(2001, 7, 19),
        "time_to": None,
        "antenna": {
            "model": "TRM57971.00",
            "serial_number": "a1",
            "antenna_height": -0.007,
        },
    },
]

_AGENCIES = {
    "poc": {
        "name_lines": ["Icelandic Meteorological Office", "Infrastructure Division"],
        "abbrev": "IMO",
        "address": ["Bústaðarvegur 7-9", "105 Reykjavík", "Iceland"],
        "contact_name": "GNSS Operator",
        "phone": "5226000",
        "email": "gnss-epos@vedur.is",
    },
    "responsible": None,
    "data_center": {
        "primary": "IMO",
        "secondary": "NATT",
        "url": "https://en.vedur.is",
    },
}


def test_v2_title_nine_char_order_and_url():
    log = generate_igs_site_log(_STATION, _SESSIONS)
    first = log.splitlines()[0]
    # 9-char = MARKER + monument + country (RHOF00ISL — NOT the old RHOFISL00).
    assert "RHOF00ISL Site Information Form (site log v2.0)" in first
    assert "RHOFISL00" not in log
    assert "https://files.igs.org/pub/station/general/sitelog_instr_v2.0.txt" in log
    assert "ftp://igs.ign.fr" not in log
    # §1 uses the v2.0 label, §0 carries the team email.
    assert "Nine Character ID        : RHOF00ISL" in log
    assert "Four Character ID" not in log
    assert "GNSS Operator (gnss-epos@vedur.is)" in log


def test_monument_and_country_knobs_change_nine_char():
    log = generate_igs_site_log(
        _STATION, _SESSIONS, country_code="NOR", monument_number="5"
    )
    assert "RHOF05NOR Site Information Form" in log.splitlines()[0]
    assert "Nine Character ID        : RHOF05NOR" in log


def test_agency_sections_rendered():
    log = generate_igs_site_log(_STATION, _SESSIONS, agencies=_AGENCIES)
    assert "11.  On-Site, Point of Contact Agency Information" in log
    assert "Agency                   : Icelandic Meteorological Office" in log
    assert ": Infrastructure Division" in log  # multi-line continuation
    assert "Preferred Abbreviation   : IMO" in log
    assert "E-mail                 : gnss-epos@vedur.is" in log
    # §12 present but empty (responsible == POC ⇒ form placeholders only).
    assert "12.  Responsible Agency (if different from 11.)" in log
    # §13 data centers + URL.
    assert "Primary Data Center      : IMO" in log
    assert "Secondary Data Center    : NATT" in log
    assert "URL for More Information : https://en.vedur.is" in log


def test_responsible_agency_rendered_when_different():
    agencies = dict(_AGENCIES)
    agencies["responsible"] = {
        "name_lines": ["Natural Science Institute of Iceland", "Land Survey"],
        "abbrev": "NSII",
        "address": ["Smiðjuvellir 28", "300 Akranes", "Iceland"],
        "contact_name": "Geodetic department",
        "phone": "",
        "email": "gnss@natt.is",
    }
    log = generate_igs_site_log(_STATION, _SESSIONS, agencies=agencies)
    sec12 = log.split("12.  Responsible Agency")[1].split("13.  More Information")[0]
    assert "Natural Science Institute of Iceland" in sec12
    assert "NSII" in sec12
    assert "gnss@natt.is" in sec12


def test_sections_5_to_10_skeletons_present():
    log = generate_igs_site_log(_STATION, _SESSIONS)
    assert "5.   Surveyed Local Ties" in log
    assert "6.1  Standard Type            : INTERNAL" in log
    assert "Effective Dates        : 2001-07-19/CCYY-MM-DD" in log
    assert "7.   Collocation Information" in log
    assert "8.   Meteorological Instrumentation" in log
    assert "9.   Local Ongoing Conditions" in log
    assert "10.  Local Episodic Effects" in log


def test_none_device_fields_do_not_crash():
    """Present-but-None model/serial (HAMR) crashed `{antenna_type:<16}`."""
    sessions = [
        {
            "time_from": datetime(2001, 1, 1),
            "time_to": None,
            "antenna": {"model": None, "serial_number": None, "antenna_height": None},
            "radome": {"model": None},
        },
        {
            "time_from": datetime(2001, 1, 1),
            "time_to": None,
            "gnss_receiver": {
                "model": None,
                "serial_number": None,
                "firmware_version": None,
            },
        },
    ]
    station = dict(_STATION)
    station["iers_domes_number"] = None  # DOMES-less station (HAMR/SKOG)
    station["altitude"] = None
    log = generate_igs_site_log(station, sessions)
    assert "HAMR" not in log  # sanity: still RHOF fixture
    assert "Nine Character ID        : RHOF00ISL" in log


def test_no_trailing_whitespace():
    log = generate_igs_site_log(_STATION, _SESSIONS, agencies=_AGENCIES)
    assert not any(ln != ln.rstrip() for ln in log.splitlines())


def test_previous_site_log_rendered_in_form():
    log = generate_igs_site_log(
        _STATION, _SESSIONS, previous_site_log="rhof00isl_20240827.log"
    )
    assert "Previous Site Log        : rhof00isl_20240827.log" in log
