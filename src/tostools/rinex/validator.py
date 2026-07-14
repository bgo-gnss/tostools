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
from .domes import domes_or_skip


def _parse_time_of_first_obs(value: Any) -> Any:
    """Parse a RINEX ``TIME OF FIRST OBS`` value → ``datetime``, or None.

    Format is free-width ``YYYY MM DD HH MM SS.sssssss SYS`` — the first three
    integers are the date.
    """
    try:
        parts = str(value).split()
        return datetime(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError, TypeError):
        return None


def compare_rinex_to_tos(
    rinex_info: Dict[str, str],
    tos_session: Dict[str, Any],
    loglevel: int = logging.WARNING,
    coord_tolerance: float = 10.0,
    observation_date: Any = None,
    expected_interval: Any = None,
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

    # Compare marker number. Policy: MARKER NUMBER carries the IERS DOMES number
    # and nothing else. When the station has no DOMES the line must be absent —
    # a leftover 4-char id (MARKER NAME already carries that) is a discrepancy to
    # STRIP, not something to re-inject. ``tos_session["domes"]`` is a station-
    # level field the session providers add (TOSSesionCache / make_session_provider).
    tos_domes = domes_or_skip(tos_session.get("domes"))
    rinex_number = str(rinex_info.get("MARKER NUMBER") or "").strip()
    if tos_domes:
        if rinex_number.upper() == tos_domes:
            comparison_result["matches"]["domes"] = tos_domes
        else:
            comparison_result["discrepancies"]["domes"] = {
                "rinex": rinex_number.upper(),
                "tos": tos_domes,
            }
            comparison_result["corrections"]["MARKER NUMBER"] = tos_domes
    elif rinex_number:
        # No real DOMES but the header still carries a MARKER NUMBER (legacy id):
        # flag it so --fix-headers removes the line. The corrector recomputes the
        # strip (it never re-injects an id); the empty "tos" value is display-only.
        comparison_result["discrepancies"]["domes"] = {
            "rinex": rinex_number.upper(),
            "tos": "",
        }
        # NB: "" here is a display-only strip flag — header_fix keys off the label
        # and lets the corrector recompute the real STRIP_LINE sentinel. Never wire
        # this "" straight into a writer (it would be a no-op, leaving the stale id).
        comparison_result["corrections"]["MARKER NUMBER"] = ""

    # Compare receiver by NORMALIZED IDENTITY, not raw string. The header stores
    # fixed-width A20,A20,A20 (serial/type/firmware) columns; TOS stores single-
    # spaced fields — a raw compare never matches (that was the old unconditional-
    # flag bug that forced fix-headers to whitelist receiver/antenna out).
    # ReceiverHeader.key applies IGS-name / placeholder-serial / vendor-firmware
    # normalization to both sides, so only a REAL change is flagged. Receiver is a
    # FLAG-only field for --fix-headers (a historical header records the actual
    # hardware at acquisition; TOS device_history is a reconstruction) — reported,
    # never auto-rewritten.
    if "REC # / TYPE / VERS" in rinex_info:
        receiver_info = tos_session.get("gnss_receiver", {})
        if receiver_info:
            from ..receiver_timeline import ReceiverHeader

            v = rinex_info["REC # / TYPE / VERS"]
            rinex_rec = ReceiverHeader(
                serial=(v[0:20].strip() or None),
                rtype=(v[20:40].strip() or None),
                firmware=(v[40:60].strip() or None),
            )
            tos_rec = ReceiverHeader(
                serial=(str(receiver_info.get("serial_number") or "").strip() or None),
                rtype=(str(receiver_info.get("model") or "").strip() or None),
                firmware=(
                    str(receiver_info.get("firmware_version") or "").strip() or None
                ),
            )
            if rinex_rec.is_known and tos_rec.is_known and rinex_rec.key != tos_rec.key:
                comparison_result["discrepancies"]["receiver"] = {
                    "rinex": str(rinex_rec),
                    "tos": str(tos_rec),
                }
                comparison_result["corrections"]["REC # / TYPE / VERS"] = [
                    tos_rec.serial or "",
                    tos_rec.rtype or "",
                    tos_rec.firmware or "",
                ]
            else:
                comparison_result["matches"]["receiver"] = str(tos_rec)
        else:
            comparison_result["missing_tos"].append("receiver information")

    # Compare antenna UNIT identity (serial / type / radome) by normalized key.
    # Height is a separate field (ANTENNA: DELTA H/E/N below), so unit_key ignores
    # it. Also FLAG-only for --fix-headers, same rationale as the receiver.
    if "ANT # / TYPE" in rinex_info:
        antenna_info = tos_session.get("antenna", {})
        if antenna_info:
            from ..antenna_timeline import AntennaHeader

            v = rinex_info["ANT # / TYPE"]
            toks = v[20:40].split()
            rinex_ant = AntennaHeader(
                serial=(v[0:20].strip() or None),
                atype=(toks[0] if toks else None),
                radome=(toks[1] if len(toks) > 1 else None),
                delta_h=None,
                delta_e=None,
                delta_n=None,
            )
            radome_info = tos_session.get("radome") or {}
            tos_ant = AntennaHeader(
                serial=(str(antenna_info.get("serial_number") or "").strip() or None),
                atype=(str(antenna_info.get("model") or "").strip() or None),
                radome=(str(radome_info.get("model") or "").strip() or None),
                delta_h=None,
                delta_e=None,
                delta_n=None,
            )
            if (
                rinex_ant.is_known
                and tos_ant.is_known
                and rinex_ant.unit_key != tos_ant.unit_key
            ):
                comparison_result["discrepancies"]["antenna"] = {
                    "rinex": str(rinex_ant),
                    "tos": str(tos_ant),
                }
                comparison_result["corrections"]["ANT # / TYPE"] = [
                    tos_ant.serial or "",
                    tos_ant.atype or "",
                    tos_ant.radome or "NONE",
                ]
            else:
                comparison_result["matches"]["antenna"] = str(tos_ant)
        else:
            comparison_result["missing_tos"].append("antenna information")

    # Compare antenna DELTA H/E/N — all three components (the legacy checker did;
    # the height-only check missed a bogus east/north offset).
    if "ANTENNA: DELTA H/E/N" in rinex_info:
        rinex_height = rinex_info["ANTENNA: DELTA H/E/N"].strip()
        antenna_info = tos_session.get("antenna", {})

        if antenna_info and "antenna_height" in antenna_info:
            if rinex_height:
                try:
                    parts = rinex_height.split()
                    rinex_h = float(parts[0])
                    rinex_e = float(parts[1]) if len(parts) > 1 else 0.0
                    rinex_n = float(parts[2]) if len(parts) > 2 else 0.0
                    # H: RINEX "DELTA H" is the full mark->ARP height, so the TOS
                    # expectation is the COMPOSITE of the antenna eccentricity
                    # (monument-top->ARP) and the monument height (mark->monument-
                    # top) — comparing the eccentricity alone falsely flags every
                    # station with a non-zero monument. monument_height is canonical;
                    # antenna_height on the monument is only a legacy fallback.
                    monument_info = tos_session.get("monument") or {}
                    mon_h = monument_info.get("monument_height")
                    if mon_h is None:
                        mon_h = monument_info.get("antenna_height")  # legacy fallback
                    tos_h = float(antenna_info["antenna_height"]) + float(mon_h or 0.0)
                    # E/N: the TOS antenna eccentricity (0.0 for a centered antenna,
                    # but stored per-station so a real offset is honoured, not
                    # assumed 0 — a non-zero header E/N that TOS says is 0 is an error).
                    tos_e = float(antenna_info.get("antenna_offset_east") or 0.0)
                    tos_n = float(antenna_info.get("antenna_offset_north") or 0.0)

                    if (
                        abs(rinex_h - tos_h) > 0.001
                        or abs(rinex_e - tos_e) > 0.001
                        or abs(rinex_n - tos_n) > 0.001
                    ):  # 1mm tolerance on any component
                        comparison_result["discrepancies"]["antenna_height"] = {
                            "rinex": [rinex_h, rinex_e, rinex_n],
                            "tos": [tos_h, tos_e, tos_n],
                        }
                        comparison_result["corrections"]["ANTENNA: DELTA H/E/N"] = [
                            tos_h,
                            tos_e,
                            tos_n,
                        ]
                    else:
                        comparison_result["matches"]["antenna_height"] = [
                            rinex_h,
                            rinex_e,
                            rinex_n,
                        ]
                except (ValueError, IndexError) as e:
                    logger.warning(f"Error parsing antenna DELTA H/E/N: {e}")

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
                # APPROX POSITION XYZ is an a-priori position; correct it to the
                # TOS surveyed ECEF. The tolerance means only gross errors fire —
                # normal cm/dm plate motion never trips it.
                comparison_result["corrections"]["APPROX POSITION XYZ"] = tos_xyz
            else:
                comparison_result["matches"]["coordinates"] = distance
        except (ValueError, TypeError) as e:
            logger.warning(f"Error comparing coordinates: {e}")

    # Compare observer / agency. The RINEX OBSERVER (A20) + AGENCY (A40) strings
    # are resolved from the station's TOS owner organization by the receivers
    # session provider (agencies.yaml → generic team name + agency, never personal
    # initials) and placed on the session as ``observer`` / ``agency``. Only
    # checked when the session carries them (agencies.yaml deployed) — a host
    # without it simply skips the field. Personal-initials headers (e.g.
    # "SFS/BGO/SJ / ETH/IMO") are the discrepancy this corrects (EPOS 4.1.7).
    tos_observer = str(tos_session.get("observer") or "").strip()
    tos_agency = str(tos_session.get("agency") or "").strip()
    if (tos_observer or tos_agency) and "OBSERVER / AGENCY" in rinex_info:
        v = rinex_info["OBSERVER / AGENCY"]
        rinex_observer = v[0:20].strip()
        rinex_agency = v[20:60].strip()
        if rinex_observer == tos_observer and rinex_agency == tos_agency:
            comparison_result["matches"][
                "observer_agency"
            ] = f"{tos_observer} / {tos_agency}"
        else:
            comparison_result["discrepancies"]["observer_agency"] = {
                "rinex": f"{rinex_observer} / {rinex_agency}",
                "tos": f"{tos_observer} / {tos_agency}",
            }
            comparison_result["corrections"]["OBSERVER / AGENCY"] = [
                tos_observer,
                tos_agency,
            ]

    # File-integrity consistency (flag-only — a misnamed or misdated file is a
    # data problem, not a header field to rewrite; ported from the legacy
    # compare_tos_to_rinex "rinex file" block):
    #   * the filename's 4-char prefix must be the station marker;
    #   * the header TIME OF FIRST OBS date must be the file's observation date.
    fname = str(rinex_info.get("file_name") or "").strip()
    tos_marker_id = str(tos_session.get("marker") or "").strip().upper()
    if fname and tos_marker_id and fname[:4].upper() != tos_marker_id:
        comparison_result["discrepancies"]["filename_marker"] = {
            "rinex": fname[:4].upper(),
            "tos": tos_marker_id,
        }
    if observation_date is not None and "TIME OF FIRST OBS" in rinex_info:
        tofo = _parse_time_of_first_obs(rinex_info["TIME OF FIRST OBS"])
        if tofo is not None and tofo.date() != observation_date.date():
            comparison_result["discrepancies"]["time_of_first_obs"] = {
                "rinex": tofo.date().isoformat(),
                "expected": observation_date.date().isoformat(),
            }
    # INTERVAL — session-mixing guard: the header sampling rate must match the
    # nominal rate of the session the file belongs to (caller supplies it — e.g.
    # 15.0s for 15s_24hr, 1.0s for 1Hz_1hr). A mismatch means a file sorted into
    # the wrong tier. Only when the header carries an INTERVAL (many older files
    # omit it) and the caller passes an expected rate; flag-only (a misplaced file
    # is a data problem, not a header field to rewrite).
    if expected_interval is not None:
        rinex_interval_raw = str(rinex_info.get("INTERVAL") or "").strip()
        if rinex_interval_raw:
            try:
                rinex_interval = float(rinex_interval_raw.split()[0])
                if abs(rinex_interval - float(expected_interval)) > 0.001:
                    comparison_result["discrepancies"]["interval"] = {
                        "rinex": rinex_interval,
                        "expected": float(expected_interval),
                    }
            except (ValueError, IndexError):
                pass

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
