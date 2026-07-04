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
    assert r["matches"]["antenna_height"] == 1.0070


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
