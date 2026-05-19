"""
RINEX validation and quality control against TOS metadata.

This module provides functions for validating RINEX files against TOS database
information and performing quality control checks.
"""

import logging
import math
from datetime import datetime
from typing import Any, Dict, List

from .. import gps_metadata_qc as gpsqc
from ..utils.logging import get_logger


def compare_rinex_to_tos(
    rinex_info: Dict[str, str],
    tos_session: Dict[str, Any],
    loglevel: int = logging.WARNING,
    coord_tolerance: float = 10.0,
) -> Dict[str, Any]:
    """
    Compare RINEX header information with TOS database session.

    Args:
        rinex_info: Extracted RINEX header information
        tos_session: TOS session data with device information
        loglevel: Logging level
        coord_tolerance: Maximum distance in meters between the RINEX
            APPROX POSITION XYZ and the TOS coordinates (transformed to
            ECEF) before the coordinate check is flagged as a discrepancy

    Returns:
        Dictionary containing comparison results and corrections
    """
    logger = get_logger(__name__, loglevel)

    comparison_result = {
        "matches": {},
        "discrepancies": {},
        "corrections": {},
        "missing_tos": [],
        "missing_rinex": [],
    }

    # Compare marker name
    if "MARKER NAME" in rinex_info:
        rinex_marker = rinex_info["MARKER NAME"].strip().upper()
        tos_marker = tos_session.get("marker", "").upper()

        if rinex_marker and tos_marker:
            if rinex_marker == tos_marker:
                comparison_result["matches"]["marker"] = rinex_marker
            else:
                comparison_result["discrepancies"]["marker"] = {
                    "rinex": rinex_marker,
                    "tos": tos_marker,
                }
                comparison_result["corrections"]["MARKER NAME"] = tos_marker

    # Compare receiver information
    if "REC # / TYPE / VERS" in rinex_info:
        rinex_receiver = rinex_info["REC # / TYPE / VERS"].strip()
        receiver_info = tos_session.get("gnss_receiver", {})

        if receiver_info:
            tos_receiver = f"{receiver_info.get('serial_number', '')} {receiver_info.get('model', '')} {receiver_info.get('firmware_version', '')}"

            if rinex_receiver:
                comparison_result["discrepancies"]["receiver"] = {
                    "rinex": rinex_receiver,
                    "tos": tos_receiver.strip(),
                }
                comparison_result["corrections"][
                    "REC # / TYPE / VERS"
                ] = tos_receiver.strip()
            else:
                comparison_result["missing_rinex"].append("REC # / TYPE / VERS")
        else:
            comparison_result["missing_tos"].append("receiver information")

    # Compare antenna information
    if "ANT # / TYPE" in rinex_info:
        rinex_antenna = rinex_info["ANT # / TYPE"].strip()
        antenna_info = tos_session.get("antenna", {})

        if antenna_info:
            tos_antenna = f"{antenna_info.get('serial_number', '')} {antenna_info.get('model', '')}"

            if rinex_antenna:
                comparison_result["discrepancies"]["antenna"] = {
                    "rinex": rinex_antenna,
                    "tos": tos_antenna.strip(),
                }
                comparison_result["corrections"]["ANT # / TYPE"] = tos_antenna.strip()
            else:
                comparison_result["missing_rinex"].append("ANT # / TYPE")
        else:
            comparison_result["missing_tos"].append("antenna information")

    # Compare antenna height
    if "ANTENNA: DELTA H/E/N" in rinex_info:
        rinex_height = rinex_info["ANTENNA: DELTA H/E/N"].strip()
        antenna_info = tos_session.get("antenna", {})

        if antenna_info and "antenna_height" in antenna_info:
            tos_height = antenna_info["antenna_height"]

            if rinex_height:
                try:
                    # Parse RINEX height (first value in H/E/N)
                    rinex_h = float(rinex_height.split()[0])
                    tos_h = float(tos_height)

                    if abs(rinex_h - tos_h) > 0.001:  # 1mm tolerance
                        comparison_result["discrepancies"]["antenna_height"] = {
                            "rinex": rinex_h,
                            "tos": tos_h,
                        }
                        # Format as H/E/N with E=0, N=0
                        comparison_result["corrections"][
                            "ANTENNA: DELTA H/E/N"
                        ] = f"{tos_h:8.4f} 0.0000 0.0000"
                    else:
                        comparison_result["matches"]["antenna_height"] = rinex_h
                except (ValueError, IndexError) as e:
                    logger.warning(f"Error parsing antenna height: {e}")

    # Compare approximate position (XYZ) against TOS coordinates
    rinex_xyz_str = rinex_info.get("APPROX POSITION XYZ", "").strip()
    tos_lat = tos_session.get("lat")
    tos_lon = tos_session.get("lon")
    tos_alt = tos_session.get("altitude")

    have_tos_coords = (
        tos_lat is not None and tos_lon is not None and tos_alt is not None
    )
    if rinex_xyz_str and have_tos_coords:
        try:
            rinex_xyz = [float(v) for v in rinex_xyz_str.split()[:3]]
            tos_xyz = list(
                gpsqc.wgs84toitrf08.transform(
                    float(tos_lat), float(tos_lon), float(tos_alt)
                )
            )
            diff = [r - t for r, t in zip(rinex_xyz, tos_xyz)]
            distance = math.sqrt(sum(d * d for d in diff))

            comparison_result["coord_check"] = {
                "rinex_xyz": rinex_xyz,
                "tos_xyz": tos_xyz,
                "diff_xyz": diff,
                "distance_m": distance,
                "tolerance_m": coord_tolerance,
                "exceeds_tolerance": distance > coord_tolerance,
            }

            if distance > coord_tolerance:
                comparison_result["discrepancies"]["coordinates"] = {
                    "rinex": rinex_xyz,
                    "tos": tos_xyz,
                    "distance_m": distance,
                    "tolerance_m": coord_tolerance,
                }
            else:
                comparison_result["matches"]["coordinates"] = distance
        except (ValueError, TypeError) as e:
            logger.warning(f"Error comparing coordinates: {e}")

    # Compare observer/agency
    observer_info = tos_session.get("contact", {})
    if observer_info:
        agency_name = observer_info.get("owner", {}).get("abbreviation", "")
        if agency_name:
            if "OBSERVER / AGENCY" in rinex_info:
                rinex_agency = rinex_info["OBSERVER / AGENCY"].strip()
                if agency_name not in rinex_agency:
                    comparison_result["corrections"][
                        "OBSERVER / AGENCY"
                    ] = f"GNSS Operator {agency_name}"

    logger.info(
        f"Comparison found {len(comparison_result['discrepancies'])} discrepancies"
    )
    return comparison_result


def validate_rinex_time_range(
    rinex_info: Dict[str, str],
    tos_session: Dict[str, Any],
    loglevel: int = logging.WARNING,
) -> Dict[str, Any]:
    """
    Validate RINEX observation time range against TOS session period.

    Args:
        rinex_info: RINEX header information
        tos_session: TOS session data
        loglevel: Logging level

    Returns:
        Dictionary with time range validation results
    """
    logger = get_logger(__name__, loglevel)

    validation_result = {
        "valid": True,
        "issues": [],
        "session_start": None,
        "session_end": None,
        "rinex_start": None,
    }

    # Get TOS session time range
    session_start = tos_session.get("time_from")
    session_end = tos_session.get("time_to")

    validation_result["session_start"] = session_start
    validation_result["session_end"] = session_end

    # Parse RINEX start time
    if "TIME OF FIRST OBS" in rinex_info:
        time_str = rinex_info["TIME OF FIRST OBS"].strip()
        if time_str:
            try:
                # Parse RINEX time format (year, month, day, hour, min, sec)
                parts = time_str.split()
                if len(parts) >= 6:
                    year = int(parts[0])
                    month = int(parts[1])
                    day = int(parts[2])
                    hour = int(parts[3])
                    minute = int(parts[4])
                    second = float(parts[5])

                    rinex_start = datetime(year, month, day, hour, minute, int(second))
                    validation_result["rinex_start"] = rinex_start

                    # Check if RINEX time falls within session
                    if session_start and rinex_start < session_start:
                        validation_result["valid"] = False
                        validation_result["issues"].append(
                            f"RINEX start ({rinex_start}) before session start ({session_start})"
                        )

                    if session_end and rinex_start > session_end:
                        validation_result["valid"] = False
                        validation_result["issues"].append(
                            f"RINEX start ({rinex_start}) after session end ({session_end})"
                        )

            except (ValueError, IndexError) as e:
                validation_result["valid"] = False
                validation_result["issues"].append(f"Invalid RINEX time format: {e}")
                logger.warning(f"Error parsing RINEX time: {e}")

    return validation_result


def check_station_configuration(
    station_sessions: List[Dict[str, Any]], loglevel: int = logging.WARNING
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Check station configuration consistency across all sessions.

    Args:
        station_sessions: List of all station sessions
        loglevel: Logging level

    Returns:
        Dictionary of configuration issues by type
    """
    logger = get_logger(__name__, loglevel)

    issues = {
        "receiver_changes": [],
        "antenna_changes": [],
        "position_changes": [],
        "incomplete_sessions": [],
    }

    prev_session = None

    for session in station_sessions:
        # Check for incomplete sessions
        required_components = ["gnss_receiver", "antenna"]
        missing_components = [
            comp for comp in required_components if comp not in session
        ]

        if missing_components:
            issues["incomplete_sessions"].append(
                {
                    "session_start": session.get("time_from"),
                    "missing": missing_components,
                }
            )

        if prev_session:
            # Check for receiver changes
            prev_rx = prev_session.get("gnss_receiver", {})
            curr_rx = session.get("gnss_receiver", {})

            if prev_rx.get("model") != curr_rx.get("model") or prev_rx.get(
                "serial_number"
            ) != curr_rx.get("serial_number"):
                issues["receiver_changes"].append(
                    {
                        "change_time": session.get("time_from"),
                        "from": f"{prev_rx.get('model', 'Unknown')} #{prev_rx.get('serial_number', 'Unknown')}",
                        "to": f"{curr_rx.get('model', 'Unknown')} #{curr_rx.get('serial_number', 'Unknown')}",
                    }
                )

            # Check for antenna changes
            prev_ant = prev_session.get("antenna", {})
            curr_ant = session.get("antenna", {})

            if prev_ant.get("model") != curr_ant.get("model") or prev_ant.get(
                "serial_number"
            ) != curr_ant.get("serial_number"):
                issues["antenna_changes"].append(
                    {
                        "change_time": session.get("time_from"),
                        "from": f"{prev_ant.get('model', 'Unknown')} #{prev_ant.get('serial_number', 'Unknown')}",
                        "to": f"{curr_ant.get('model', 'Unknown')} #{curr_ant.get('serial_number', 'Unknown')}",
                    }
                )

        prev_session = session

    total_changes = sum(len(issue_list) for issue_list in issues.values())
    logger.info(f"Found {total_changes} configuration changes/issues")

    return issues


def generate_qc_report(
    station_data: Dict[str, Any],
    rinex_comparisons: List[Dict[str, Any]],
    loglevel: int = logging.WARNING,
) -> str:
    """
    Generate a comprehensive QC report.

    Args:
        station_data: Station information
        rinex_comparisons: List of RINEX comparison results
        loglevel: Logging level

    Returns:
        Formatted QC report string
    """
    logger = get_logger(__name__, loglevel)

    report_lines = []
    report_lines.append("=" * 60)
    report_lines.append(
        f"GPS METADATA QC REPORT - {station_data.get('marker', 'Unknown').upper()}"
    )
    report_lines.append("=" * 60)

    # Station summary
    report_lines.append(f"Station: {station_data.get('name', 'Unknown')}")
    report_lines.append(f"Marker: {station_data.get('marker', 'Unknown')}")
    report_lines.append(f"DOMES: {station_data.get('iers_domes_number', 'Unknown')}")
    report_lines.append(
        f"Location: {station_data.get('lat', 0):.5f}°N, {station_data.get('lon', 0):.5f}°E"
    )
    report_lines.append("")

    # Session summary
    sessions = station_data.get("device_history", [])
    report_lines.append(f"Total Sessions: {len(sessions)}")
    if sessions:
        start_date = min(s.get("time_from") for s in sessions if s.get("time_from"))
        end_date = max(
            s.get("time_to", datetime.now()) for s in sessions if s.get("time_to")
        )
        report_lines.append(
            f"Period: {start_date.strftime('%Y-%m-%d') if start_date else 'Unknown'} to {end_date.strftime('%Y-%m-%d') if end_date else 'Present'}"
        )
    report_lines.append("")

    # RINEX validation summary
    if rinex_comparisons:
        total_discrepancies = sum(
            len(comp.get("discrepancies", {})) for comp in rinex_comparisons
        )
        report_lines.append(f"RINEX Files Checked: {len(rinex_comparisons)}")
        report_lines.append(f"Total Discrepancies: {total_discrepancies}")

        if total_discrepancies > 0:
            report_lines.append("\nDISCREPANCIES FOUND:")
            for i, comp in enumerate(rinex_comparisons):
                if comp.get("discrepancies"):
                    report_lines.append(f"File {i+1}:")
                    for field, diff in comp["discrepancies"].items():
                        report_lines.append(
                            f"  {field}: RINEX='{diff.get('rinex', '')}' vs TOS='{diff.get('tos', '')}'"
                        )

    report_lines.append("\n" + "=" * 60)

    logger.info(f"Generated QC report with {len(report_lines)} lines")
    return "\n".join(report_lines)
