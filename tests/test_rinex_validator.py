"""Unit tests for :mod:`tostools.rinex.validator`.

Focused on the Layer-5 ``coord_tolerance`` flow added with PR #8 — the
RINEX ``APPROX POSITION XYZ`` vs TOS lat/lon/altitude ECEF distance
check. Mirrors Hildur's test plan from PR #8 (in-tolerance vs
exceeds-tolerance vs missing TOS coords). No network — the WGS84↔ITRF08
transformer is real (pyproj) but only fed deterministic inputs.
"""

from __future__ import annotations

from typing import Any, Dict

from tostools import gps_metadata_qc as gpsqc
from tostools.rinex.validator import compare_rinex_to_tos

# ---------------------------------------------------------------------------
# Helpers — build the minimal inputs compare_rinex_to_tos needs.
# ---------------------------------------------------------------------------


def _tos_session_with_coords(lat: float, lon: float, alt: float) -> Dict[str, Any]:
    """Minimal TOS-session dict carrying lat/lon/altitude.

    All other comparison branches (antenna height, observer/agency, etc.)
    are inactive because the dict has no devices.
    """
    return {
        "lat": lat,
        "lon": lon,
        "altitude": alt,
        "devices": {},
        "contact": {},
    }


def _rinex_info_with_xyz(x: float, y: float, z: float) -> Dict[str, str]:
    return {
        "MARKER NAME": "TEST",
        "APPROX POSITION XYZ": f"{x:.4f} {y:.4f} {z:.4f}",
    }


def _expected_xyz(lat: float, lon: float, alt: float) -> tuple:
    """Re-run the same transform compare_rinex_to_tos uses, so we can
    construct synthetic 'matching' inputs without hard-coding ECEF values
    that drift with pyproj versions."""
    return tuple(gpsqc.wgs84toitrf08.transform(lat, lon, alt))


# ---------------------------------------------------------------------------
# Hildur's test plan, mirrored
# ---------------------------------------------------------------------------


def test_coord_check_in_tolerance_records_match():
    """RINEX XYZ that exactly matches the TOS-derived ECEF coordinate
    should land in ``matches['coordinates']`` and ``coord_check.distance_m``
    should be ~0."""
    lat, lon, alt = 64.13, -21.93, 50.0
    x, y, z = _expected_xyz(lat, lon, alt)

    result = compare_rinex_to_tos(
        _rinex_info_with_xyz(x, y, z),
        _tos_session_with_coords(lat, lon, alt),
    )

    assert "coord_check" in result
    cc = result["coord_check"]
    assert cc["exceeds_tolerance"] is False
    assert cc["distance_m"] < 1e-3
    assert "coordinates" in result["matches"]
    assert "coordinates" not in result["discrepancies"]


def test_coord_check_exceeds_tolerance_flags_discrepancy():
    """A 20m offset on Z with the default 10m tolerance should trip the
    discrepancy branch — populates ``discrepancies['coordinates']`` and
    records ``exceeds_tolerance=True``."""
    lat, lon, alt = 64.13, -21.93, 50.0
    x, y, z = _expected_xyz(lat, lon, alt)

    result = compare_rinex_to_tos(
        _rinex_info_with_xyz(x, y, z + 20.0),  # 20m offset in Z
        _tos_session_with_coords(lat, lon, alt),
    )

    cc = result["coord_check"]
    assert cc["exceeds_tolerance"] is True
    assert cc["distance_m"] > 10.0
    assert "coordinates" in result["discrepancies"]


def test_coord_check_custom_tolerance_passes_a_known_offset():
    """A 5m offset should pass with tolerance=10m but fail with
    tolerance=1m. Same input, different threshold — proves the kwarg
    actually reaches the check."""
    lat, lon, alt = 64.13, -21.93, 50.0
    x, y, z = _expected_xyz(lat, lon, alt)
    rinex_info = _rinex_info_with_xyz(x, y, z + 5.0)
    tos_session = _tos_session_with_coords(lat, lon, alt)

    lenient = compare_rinex_to_tos(rinex_info, tos_session, coord_tolerance=10.0)
    assert lenient["coord_check"]["exceeds_tolerance"] is False

    strict = compare_rinex_to_tos(rinex_info, tos_session, coord_tolerance=1.0)
    assert strict["coord_check"]["exceeds_tolerance"] is True


def test_coord_check_missing_tos_coords_skips_silently():
    """When TOS has no lat/lon/alt (a station with incomplete metadata),
    the check should not crash — just leave ``coord_check`` out of the
    result. Other comparison branches still run."""
    rinex_info = _rinex_info_with_xyz(2587000.0, -1043000.0, 5755000.0)
    tos_session: Dict[str, Any] = {"devices": {}, "contact": {}}

    result = compare_rinex_to_tos(rinex_info, tos_session)

    assert "coord_check" not in result
    # And the function still returned its normal structure.
    assert "discrepancies" in result
    assert "matches" in result


def test_coord_check_missing_rinex_xyz_skips_silently():
    """When the RINEX header has no APPROX POSITION XYZ line, the check
    is skipped without affecting the rest of the comparison."""
    rinex_info: Dict[str, str] = {"MARKER NAME": "TEST"}
    tos_session = _tos_session_with_coords(64.13, -21.93, 50.0)

    result = compare_rinex_to_tos(rinex_info, tos_session)

    assert "coord_check" not in result


def test_coord_check_payload_shape():
    """The ``coord_check`` dict should carry the operator-visible fields
    used by the tosGPS CLI print line (diff_xyz, distance_m, tolerance_m,
    exceeds_tolerance)."""
    lat, lon, alt = 64.13, -21.93, 50.0
    x, y, z = _expected_xyz(lat, lon, alt)

    result = compare_rinex_to_tos(
        _rinex_info_with_xyz(x, y, z),
        _tos_session_with_coords(lat, lon, alt),
        coord_tolerance=10.0,
    )

    cc = result["coord_check"]
    assert set(cc.keys()) >= {
        "rinex_xyz",
        "tos_xyz",
        "diff_xyz",
        "distance_m",
        "tolerance_m",
        "exceeds_tolerance",
    }
    assert cc["tolerance_m"] == 10.0
    assert len(cc["diff_xyz"]) == 3


# --- antenna height = antenna eccentricity + monument height (composite) -------


def _ah_result(rinex_delta_h, antenna_ecc, monument_height):
    from tostools.rinex.validator import compare_rinex_to_tos

    rinex_info = {"ANTENNA: DELTA H/E/N": f"{rinex_delta_h:.4f} 0.0000 0.0000"}
    session = {"antenna": {"antenna_height": antenna_ecc}}
    if monument_height is not None:
        session["monument"] = {"monument_height": monument_height}
    return compare_rinex_to_tos(rinex_info, session)


def test_antenna_height_composite_matches_rinex_delta_h():
    # RHOF: ecc -0.007 + monument 1.014 == DELTA H 1.0070 -> match (no discrepancy).
    r = _ah_result(1.0070, -0.007, 1.014)
    assert "antenna_height" not in r["discrepancies"]
    assert r["matches"]["antenna_height"] == [1.0070, 0.0, 0.0]


def test_antenna_east_north_offset_flagged():
    # H matches but the header carries a bogus east offset TOS says is 0 → flagged.
    rinex_info = {"ANTENNA: DELTA H/E/N": "1.0140 0.0030 0.0000"}
    session = {
        "antenna": {"antenna_height": 0.0, "antenna_offset_east": 0.0},
        "monument": {"monument_height": 1.014},
    }
    r = compare_rinex_to_tos(rinex_info, session)
    assert "antenna_height" in r["discrepancies"]
    assert r["corrections"]["ANTENNA: DELTA H/E/N"] == [1.014, 0.0, 0.0]


def test_antenna_real_east_offset_from_tos_matches():
    # A genuine TOS east eccentricity is honoured (not assumed 0).
    rinex_info = {"ANTENNA: DELTA H/E/N": "1.0140 0.1000 0.0000"}
    session = {
        "antenna": {"antenna_height": 0.0, "antenna_offset_east": 0.1},
        "monument": {"monument_height": 1.014},
    }
    r = compare_rinex_to_tos(rinex_info, session)
    assert "antenna_height" not in r["discrepancies"]


def test_coordinate_correction_emitted_when_exceeds_tolerance():
    # A gross coordinate error now emits an APPROX POSITION XYZ correction so
    # fix-headers can rewrite it (previously detected but silently unfixable).
    lat, lon, alt = 64.13, -21.93, 50.0
    x, y, z = _expected_xyz(lat, lon, alt)
    r = compare_rinex_to_tos(
        _rinex_info_with_xyz(x, y, z + 20.0),
        _tos_session_with_coords(lat, lon, alt),
    )
    assert "coordinates" in r["discrepancies"]
    assert r["corrections"]["APPROX POSITION XYZ"] == list(_expected_xyz(lat, lon, alt))


def test_antenna_height_no_monument_uses_eccentricity_alone():
    # FIHO: no monument record -> ecc carries the full height (0.192) == DELTA H.
    r = _ah_result(0.1920, 0.192, None)
    assert "antenna_height" not in r["discrepancies"]


def test_antenna_height_real_mismatch_still_flagged():
    # Genuine mismatch (composite 1.014 vs DELTA H 1.500) is still a discrepancy.
    r = _ah_result(1.5000, 0.0, 1.014)
    assert "antenna_height" in r["discrepancies"]


# ---------------------------------------------------------------------------
# MARKER NUMBER ← DOMES (EPOS 4.1.7). Checked only when TOS carries a DOMES.
# ---------------------------------------------------------------------------


def _domes_result(rinex_marker_number, tos_domes):
    """compare_rinex_to_tos over a minimal session carrying a DOMES.

    ``rinex_marker_number`` is the header MARKER NUMBER value (or None to omit
    the header record entirely); ``tos_domes`` is the station's TOS DOMES.
    """
    rinex_info = {"MARKER NAME": "RHOF"}
    if rinex_marker_number is not None:
        rinex_info["MARKER NUMBER"] = rinex_marker_number
    session = {"marker": "RHOF", "domes": tos_domes, "devices": {}, "contact": {}}
    return compare_rinex_to_tos(rinex_info, session)


def test_domes_match_when_header_equals_tos():
    r = _domes_result("10216M001", "10216M001")
    assert r["matches"].get("domes") == "10216M001"
    assert "domes" not in r["discrepancies"]
    assert "MARKER NUMBER" not in r["corrections"]


def test_domes_discrepancy_when_header_blank():
    # 2000-2011-era RHOF: blank MARKER NUMBER line vs a real TOS DOMES.
    r = _domes_result("", "10216M001")
    assert r["discrepancies"]["domes"] == {"rinex": "", "tos": "10216M001"}
    assert r["corrections"]["MARKER NUMBER"] == "10216M001"


def test_domes_discrepancy_when_header_has_4char_id():
    # 2012-2022-era RHOF: MARKER NUMBER carries the 4-char ID, not the DOMES.
    r = _domes_result("RHOF", "10216M001")
    assert r["discrepancies"]["domes"] == {"rinex": "RHOF", "tos": "10216M001"}
    assert r["corrections"]["MARKER NUMBER"] == "10216M001"


def test_domes_discrepancy_when_header_record_absent():
    # No MARKER NUMBER record at all still flags (corrector can only replace an
    # existing line, but the validator must surface the gap regardless).
    r = _domes_result(None, "10216M001")
    assert r["discrepancies"]["domes"] == {"rinex": "", "tos": "10216M001"}


def test_falls_back_to_marker_when_no_domes_and_header_matches():
    # Station without a DOMES: MARKER NUMBER should be the 4-char marker. Header
    # already carries it → a match, no correction.
    r = _domes_result("RHOF", "")
    assert r["matches"].get("domes") == "RHOF"
    assert "domes" not in r["discrepancies"]


def test_falls_back_to_marker_when_no_domes_and_header_wrong():
    # No DOMES + blank/other MARKER NUMBER → correct it to the 4-char marker.
    r = _domes_result("", "")
    assert r["discrepancies"]["domes"] == {"rinex": "", "tos": "RHOF"}
    assert r["corrections"]["MARKER NUMBER"] == "RHOF"


# ---------------------------------------------------------------------------
# Receiver / antenna: REAL per-component equality (not the old unconditional
# flag). Header stores fixed-width columns; TOS single-spaced fields — a raw
# compare never matched. Normalized identity (ReceiverHeader/AntennaHeader key)
# means only a genuine change is flagged. Flag-only fields for --fix-headers.
# ---------------------------------------------------------------------------

# RINEX REC # / TYPE / VERS data portion: A20 serial + A20 type + A20 vers.
_REC_RHOF = "5038K70713          TRIMBLE NETR9       NP 4.60 / SP 4.60"
# RINEX ANT # / TYPE: A20 serial + A20 (type [+ radome]).
_ANT_RHOF = "1441045161          TRM57971.00"


def _rec_session(serial, model, firmware):
    return {
        "marker": "RHOF",
        "gnss_receiver": {
            "serial_number": serial,
            "model": model,
            "firmware_version": firmware,
        },
    }


def test_receiver_formatting_only_difference_is_not_flagged():
    # Fixed-width header cols vs single-spaced TOS, SAME values, and firmware
    # written two ways ("NP 4.60 / SP 4.60" vs "4.60") → normalized equal → match.
    # This is the exact case the old unconditional flag got wrong.
    r = compare_rinex_to_tos(
        {"REC # / TYPE / VERS": _REC_RHOF},
        _rec_session("5038K70713", "TRIMBLE NETR9", "4.60"),
    )
    assert "receiver" not in r["discrepancies"]
    assert "receiver" in r["matches"]


def test_receiver_real_model_change_is_flagged():
    r = compare_rinex_to_tos(
        {"REC # / TYPE / VERS": _REC_RHOF},
        _rec_session("5038K70713", "SEPT POLARX5", "5.4.0"),
    )
    assert "receiver" in r["discrepancies"]
    assert r["corrections"]["REC # / TYPE / VERS"][1] == "SEPT POLARX5"


def test_receiver_placeholder_serial_not_falsely_flagged():
    # RINEX serial all zeros (unknown) + TOS synthetic serial → both normalize to
    # None on the serial component, so a serial-only difference is not a change.
    rec = "00000000            TRIMBLE NETR9       NP 4.60 / SP 4.60"
    r = compare_rinex_to_tos(
        {"REC # / TYPE / VERS": rec},
        _rec_session("receiver-rhof-20150101", "TRIMBLE NETR9", "4.60"),
    )
    assert "receiver" not in r["discrepancies"]


def _ant_session(serial, model, radome=None):
    s = {"marker": "RHOF", "antenna": {"serial_number": serial, "model": model}}
    if radome is not None:
        s["radome"] = {"model": radome}
    return s


def test_antenna_formatting_only_difference_is_not_flagged():
    # Header has no radome token (→ NONE); TOS radome "NONE" → normalized equal.
    r = compare_rinex_to_tos(
        {"ANT # / TYPE": _ANT_RHOF},
        _ant_session("1441045161", "TRM57971.00", radome="NONE"),
    )
    assert "antenna" not in r["discrepancies"]
    assert "antenna" in r["matches"]


def test_antenna_real_type_change_is_flagged():
    r = compare_rinex_to_tos(
        {"ANT # / TYPE": _ANT_RHOF},
        _ant_session("1441045161", "LEIAR25.R4", radome="LEIT"),
    )
    assert "antenna" in r["discrepancies"]
    assert r["corrections"]["ANT # / TYPE"][1] == "LEIAR25.R4"


def test_receiver_missing_tos_records_missing_not_discrepancy():
    r = compare_rinex_to_tos({"REC # / TYPE / VERS": _REC_RHOF}, {"marker": "RHOF"})
    assert "receiver" not in r["discrepancies"]
    assert "receiver information" in r["missing_tos"]


# ---------------------------------------------------------------------------
# OBSERVER / AGENCY — resolved from TOS owner org (agencies.yaml) by the
# receivers session provider and placed on the session. Personal-initials
# headers are the discrepancy this corrects (EPOS 4.1.7).
# ---------------------------------------------------------------------------


def _oa_result(rinex_oa, observer, agency):
    info = {"MARKER NAME": "RHOF"}
    if rinex_oa is not None:
        info["OBSERVER / AGENCY"] = rinex_oa
    session = {"marker": "RHOF"}
    if observer is not None:
        session["observer"] = observer
    if agency is not None:
        session["agency"] = agency
    return compare_rinex_to_tos(info, session)


def test_observer_agency_match():
    r = _oa_result(
        "GNSSatIMO           Vedurstofa Islands", "GNSSatIMO", "Vedurstofa Islands"
    )
    assert "observer_agency" not in r["discrepancies"]
    assert "observer_agency" in r["matches"]


def test_observer_agency_personal_initials_flagged():
    # "SFS/BGO/SJ / ETH/IMO" → generic GNSSatIMO / Vedurstofa Islands.
    r = _oa_result("SFS/BGO/SJ          ETH/IMO", "GNSSatIMO", "Vedurstofa Islands")
    assert r["discrepancies"]["observer_agency"]["tos"] == (
        "GNSSatIMO / Vedurstofa Islands"
    )
    assert r["corrections"]["OBSERVER / AGENCY"] == [
        "GNSSatIMO",
        "Vedurstofa Islands",
    ]


def test_observer_agency_skipped_when_session_lacks_it():
    # No agencies.yaml deployed → provider puts nothing on the session → no check.
    r = _oa_result("SFS/BGO/SJ          ETH/IMO", None, None)
    assert "observer_agency" not in r["discrepancies"]
    assert "OBSERVER / AGENCY" not in r["corrections"]


# ---------------------------------------------------------------------------
# File-integrity consistency (flag-only): filename↔marker, TIME OF FIRST OBS↔date.
# Ported from the legacy compare_tos_to_rinex "rinex file" block.
# ---------------------------------------------------------------------------

from datetime import datetime as _dt  # noqa: E402


def test_filename_marker_mismatch_flagged():
    r = compare_rinex_to_tos(
        {"MARKER NAME": "RHOF", "file_name": "AKUR0910.15D.Z"}, {"marker": "RHOF"}
    )
    assert r["discrepancies"]["filename_marker"] == {"rinex": "AKUR", "tos": "RHOF"}


def test_filename_marker_match_not_flagged():
    r = compare_rinex_to_tos(
        {"MARKER NAME": "RHOF", "file_name": "RHOF0910.15D.Z"}, {"marker": "RHOF"}
    )
    assert "filename_marker" not in r["discrepancies"]


def test_time_of_first_obs_mismatch_flagged():
    r = compare_rinex_to_tos(
        {"TIME OF FIRST OBS": "2015     4     2     0     0    0.0000000     GPS"},
        {"marker": "RHOF"},
        observation_date=_dt(2015, 4, 1),
    )
    tofo = r["discrepancies"]["time_of_first_obs"]
    assert tofo == {"rinex": "2015-04-02", "expected": "2015-04-01"}


def test_time_of_first_obs_match_not_flagged():
    r = compare_rinex_to_tos(
        {"TIME OF FIRST OBS": "2015     4     1     0     0    0.0000000     GPS"},
        {"marker": "RHOF"},
        observation_date=_dt(2015, 4, 1),
    )
    assert "time_of_first_obs" not in r["discrepancies"]


def test_time_of_first_obs_skipped_without_observation_date():
    r = compare_rinex_to_tos(
        {"TIME OF FIRST OBS": "2015     4     2     0     0    0.0000000     GPS"},
        {"marker": "RHOF"},
    )
    assert "time_of_first_obs" not in r["discrepancies"]


# ---------------------------------------------------------------------------
# INTERVAL — session-mixing guard (flag-only): header sampling rate must match
# the nominal rate of the session tier the file belongs to.
# ---------------------------------------------------------------------------


def test_interval_mismatch_flagged():
    r = compare_rinex_to_tos(
        {"INTERVAL": "30.000"}, {"marker": "RHOF"}, expected_interval=15.0
    )
    assert r["discrepancies"]["interval"] == {"rinex": 30.0, "expected": 15.0}


def test_interval_match_not_flagged():
    r = compare_rinex_to_tos(
        {"INTERVAL": "15.000"}, {"marker": "RHOF"}, expected_interval=15.0
    )
    assert "interval" not in r["discrepancies"]


def test_interval_skipped_when_header_empty():
    r = compare_rinex_to_tos(
        {"INTERVAL": ""}, {"marker": "RHOF"}, expected_interval=15.0
    )
    assert "interval" not in r["discrepancies"]


def test_interval_skipped_without_expected():
    r = compare_rinex_to_tos({"INTERVAL": "30.000"}, {"marker": "RHOF"})
    assert "interval" not in r["discrepancies"]
