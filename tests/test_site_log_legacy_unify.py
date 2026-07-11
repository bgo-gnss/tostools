"""Legacy site_log() as the single renderer — injected agencies + None-guards.

Step 1 of the sitelog unification: the proven legacy generator gains injectable
§11/§12/§13 agencies (receivers resolve_sitelog_agencies shape), M3G dated-series
§0 parameters, nine-char parts, and None-guards — all offline via the injectable
``station`` / ``device_sessions`` metadata.
"""

from tostools.legacy.gps_metadata_functions import _fmt_igs_date, site_log

STATION = {
    "name": "Prófstöð",
    "marker": "TEST",
    "iers_domes_number": "12345M001",
    "lat": 66.46112200,
    "lon": -15.94670800,
    "altitude": 78.8,
    "date_start": "2001-07-19 00:00",
    "geological_characteristic": "bedrock",
    "bedrock_type": "igneous",
    "is_near_fault_zones": "NEI",
    "country": "Ísland",
    "tectonic_plate": "EURASIAN",
}

SESSIONS = [
    {
        "time_to": None,
        "device": {
            "code_entity_subtype": "gnss_receiver",
            "model": "TRIMBLE NETR9",
            "serial_number": "123",
            "firmware_version": "5.6.0",
            "date_from": "2012-08-28T00:00:00",
            "date_to": None,
        },
    },
    {
        "time_to": None,
        "device": {
            "code_entity_subtype": "antenna",
            "model": "TRM57971.00",
            "serial_number": "456",
            "antenna_reference_point": "BAM",
            "antenna_height": 1.007,
            "monument_height": None,
            "antenna_offset_north": None,
            "antenna_offset_east": None,
            "date_from": "2012-08-28T00:00:00",
            "date_to": None,
        },
    },
]

AGENCIES = {
    "poc": {
        "name_lines": ["Icelandic Meteorological Office", "Infrastructure Division"],
        "abbrev": "IMO",
        "address": ["Bústaðarvegur 7-9", "105 Reykjavík", "Iceland"],
        "contact_name": "GNSS Operator",
        "phone": "5226000",
        "email": "gnss-epos@vedur.is",
    },
    "responsible": {
        "name_lines": ["Natural Science Institute of Iceland"],
        "abbrev": "NSII",
        "address": ["Smiðjuvellir 28", "300 Akranes"],
        "contact_name": "Geodetic department",
        "phone": "",
        "email": "gnss@natt.is",
    },
    "data_center": {
        "primary": "IMO",
        "secondary": "NATT",
        "url": "https://en.vedur.is",
    },
}


def _render(**kw):
    kw.setdefault("station", dict(STATION))
    kw.setdefault("device_sessions", [dict(s) for s in SESSIONS])
    kw.setdefault("agencies", AGENCIES)
    return site_log("TEST", **kw)


class TestInjectedAgencies:
    def test_poc_section_renders_injected_agency(self):
        log = _render()
        assert "11.  On-Site, Point of Contact Agency Information" in log
        assert "Agency                   : Icelandic Meteorological Office" in log
        assert "                              : Infrastructure Division" in log
        assert "Preferred Abbreviation   : IMO" in log
        assert "E-mail                 : gnss-epos@vedur.is" in log

    def test_responsible_section_renders_owner(self):
        log = _render()
        assert "12.  Responsible Agency (if different from 11.)" in log
        assert "Natural Science Institute of Iceland" in log
        assert "Preferred Abbreviation   : NSII" in log

    def test_responsible_none_renders_empty_template(self):
        ag = dict(AGENCIES, responsible=None)
        log = _render(agencies=ag)
        sec12 = log.split("12.  Responsible Agency")[1].split("13.  More")[0]
        assert "Agency                   : (multiple lines)" in sec12
        assert "Preferred Abbreviation   : (A10)" in sec12

    def test_data_center_section(self):
        log = _render()
        assert "Primary Data Center      : IMO" in log
        assert "Secondary Data Center    : NATT" in log
        assert "URL for More Information : https://en.vedur.is" in log


class TestFormParameters:
    def test_previous_log_and_prepared_by(self):
        log = _render(
            previous_log="test00isl_20240827.log",
            prepared_by="Prófari",
            prepared_email="prof@vedur.is",
        )
        assert "Previous Site Log       : test00isl_20240827.log" in log
        assert "Prepared by (full name)  : Prófari (prof@vedur.is)" in log

    def test_nine_char_parts(self):
        log = _render(monument_number="05", country_code="NOR")
        assert "TEST05NOR Site Information Form (site log v2.0)" in log
        assert "Nine Character ID        : TEST05NOR" in log


class TestNoneGuards:
    def test_missing_coordinates_render_blank(self):
        st = dict(STATION, lat=None, lon=None, altitude=None)
        log = _render(station=st)
        assert "X coordinate (m)       : \n" in log
        assert "Elevation (m,ellips.)  : \n" in log

    def test_none_domes_and_fault_zone(self):
        st = dict(STATION, iers_domes_number=None, is_near_fault_zones=None)
        log = _render(station=st)
        assert "IERS DOMES Number        : \n" in log
        assert "Fault zones nearby     : NO" in log

    def test_no_monument_session_no_nameerror(self):
        # Antenna offsets must not NameError when no monument session exists.
        log = _render()
        assert "Marker->ARP Up Ecc. (m)  :   1.0070" in log

    def test_none_device_model(self):
        sessions = [dict(s) for s in SESSIONS]
        sessions[1] = {
            "time_to": None,
            "device": dict(sessions[1]["device"], model=None),
        }
        log = _render(device_sessions=sessions)
        assert "4.1  Antenna Type" in log


class TestCoordinateFormat:
    def test_longitude_three_digit_degrees(self):
        log = _render()
        assert "Longitude (E is +)     : -0155648.15" in log
        assert "Latitude (N is +)      : +662740.04" in log


def _rcv(model, serial, fw, date_from, date_to, **toggles):
    """A gnss_receiver device-session for the §3 render loop."""
    dev = {
        "id_entity": 999,
        "code_entity_subtype": "gnss_receiver",
        "model": model,
        "serial_number": serial,
        "firmware_version": fw,
        "date_from": date_from,
        "date_to": date_to,
    }
    dev.update(toggles)
    return {"time_to": date_to, "device": dev}


class TestSection3PhantomSliverCoalesce:
    """site_log-level wiring: the §3 coalesce runs on the render path.

    Mirrors NYLA — a receiver whose GPS constellation toggle was entered a
    day after install produces a one-day sub-window that renders identically
    to the following block. The coalesce (devices.coalesce_render_sessions,
    wired into site_log's §3 loop) must merge it while preserving the real
    firmware and constellation splits. NYLA is not in the byte-equality
    oracle set, so this is the regression guard for the wiring itself.
    """

    SESSIONS = [
        # One-day sliver: GPS toggle unset → "GPS" fallback.
        _rcv(
            "TRIMBLE NETRS", "N1", "1.1-5", "2006-07-27T00:00:00", "2006-07-28T00:00:00"
        ),
        # Same config, GPS toggle now set → merges with the sliver.
        _rcv(
            "TRIMBLE NETRS",
            "N1",
            "1.1-5",
            "2006-07-28T00:00:00",
            "2019-04-06T00:00:00",
            GPS="true",
        ),
        # Real firmware change → stays a separate block.
        _rcv(
            "TRIMBLE NETRS",
            "N1",
            "1.3-2",
            "2019-04-06T00:00:00",
            "2022-07-22T00:00:00",
            GPS="true",
        ),
        # Real constellation change → stays a separate block.
        _rcv(
            "SEPT POLARX5",
            "P2",
            "5.5.0",
            "2022-07-22T00:00:00",
            "2022-12-16T00:00:00",
            GPS="true",
            GLO="true",
        ),
        _rcv(
            "SEPT POLARX5",
            "P2",
            "5.5.0",
            "2022-12-16T00:00:00",
            None,
            GPS="true",
            GLO="true",
            GAL="true",
        ),
    ]

    def _section3(self):
        log = _render(device_sessions=[dict(s) for s in self.SESSIONS])
        return log.split("3.   GNSS Receiver Information")[1].split(
            "4.   GNSS Antenna Information"
        )[0]

    def test_sliver_merged_into_first_block(self):
        sec3 = self._section3()
        # Five input sub-windows collapse to four real blocks (+ the 3.x
        # template): the sliver merges into 3.1, so 3.4 is the last block.
        assert "3.4  Receiver Type" in sec3
        assert "3.5  Receiver Type" not in sec3
        # 3.1 spans the merged window: install 2006-07-27, removed at the
        # firmware change 2019-04-06 — the 2006-07-28 boundary is absorbed.
        block1 = sec3.split("3.1  Receiver Type")[1].split("3.2  Receiver Type")[0]
        assert "Firmware Version         : 1.1-5" in block1
        assert "Date Installed           : 2006-07-27T00:00Z" in block1
        assert "Date Removed             : 2019-04-06T00:00Z" in block1
        # The phantom one-day boundary must not appear as any §3 install date.
        assert "Date Installed           : 2006-07-28T00:00Z" not in sec3

    def test_firmware_and_constellation_splits_preserved(self):
        sec3 = self._section3()
        assert "Firmware Version         : 1.3-2" in sec3  # 3.2 firmware split
        assert "Satellite System         : GPS+GLO\n" in sec3  # 3.3
        assert "Satellite System         : GPS+GLO+GAL\n" in sec3  # 3.4


class TestFmtIgsDate:
    def test_formats(self):
        assert _fmt_igs_date("2012-08-28T00:00:00") == "2012-08-28T00:00Z"
        assert _fmt_igs_date("2012-08-28 00:00") == "2012-08-28T00:00Z"
        assert _fmt_igs_date("2012-08-28") == "2012-08-28T00:00Z"
        assert _fmt_igs_date(None) == "CCYY-MM-DDThh:mmZ"
        assert _fmt_igs_date("garbage", placeholder="(x)") == "(x)"
