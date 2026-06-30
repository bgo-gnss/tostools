"""
IGS Site Log generation and management.

This module provides functions for generating IGS-standard site logs from TOS metadata.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

from ..utils.logging import get_logger


def generate_igs_site_log(
    station_data: Dict[str, Any],
    device_sessions: List[Dict[str, Any]],
    loglevel: int = logging.WARNING,
) -> str:
    """
    Generate IGS-standard site log from station and device data.

    Args:
        station_data: Station metadata from TOS
        device_sessions: Device session history
        loglevel: Logging level

    Returns:
        IGS-formatted site log as string
    """
    logger = get_logger(__name__, loglevel)

    marker = station_data.get("marker", "").upper()
    site_name = station_data.get("name", "")
    iers_domes = station_data.get("iers_domes_number", "")

    # Parse dates (currently not used in output, but available for future use)
    station_data.get("date_start", "")

    # Site identification section
    site_id_section = _generate_site_identification(
        marker, site_name, iers_domes, station_data, device_sessions
    )

    # Site location section
    location_section = _generate_site_location(station_data)

    # GNSS receiver section
    receiver_section = _generate_receiver_section(device_sessions)

    # GNSS antenna section
    antenna_section = _generate_antenna_section(device_sessions)

    # Combine all sections
    site_log_content = f"""     {marker}ISL00 Site Information Form (site log)
     International GNSS Service
     See Instructions at:
       ftp://igs.ign.fr/pub/igscb/igscb_mail/general/sitelog_instr.txt


0.   Form

     Prepared by (full name)  : GNSS Operator
     Date Prepared            : {datetime.now().strftime('%Y-%m-%d')}
     Report Type              : UPDATE
     Previous Site Log       :
     Modified/Added Sections  : (n.n,n.n,...)


{site_id_section}

{location_section}

{receiver_section}

{antenna_section}

More Information           : (multiple lines)
"""

    logger.info(f"Generated IGS site log for {marker}")
    return site_log_content


def _generate_site_identification(
    marker: str,
    site_name: str,
    iers_domes: str,
    station_data: Dict[str, Any],
    device_sessions: List[Dict[str, Any]],
) -> str:
    """Generate site identification section (Section 1)."""

    # Get monument information from current session
    monument_height = "(m)"
    monument_description = "STEEL MAST"
    foundation = "STEEL RODS"

    current_monument = next(
        (
            session
            for session in device_sessions
            if "monument" in session and session.get("time_to") is None
        ),
        None,
    )

    if current_monument:
        device = current_monument.get("monument", {})
        height_val = device.get("monument_height") or device.get("antenna_height", 0.0)
        if height_val:
            monument_height = f"{float(height_val)} m"

        monument_description = device.get("description", "STEEL MAST")
        foundation = device.get("foundation", "STEEL RODS")

    return f"""1.   Site Identification of the GNSS Monument

     Site Name                : {site_name}
     Four Character ID        : {marker}
     Monument Inscription     :
     IERS DOMES Number        : {iers_domes}
     CDP Number               :
     Monument Description     : {monument_description}
       Height of the Monument : {monument_height}
       Monument Foundation    : {foundation}
       Foundation Depth       : (m)
     Marker Description       : {station_data.get('marker_description', '')}
     Date Installed           : {station_data.get('date_start', '')}
     Geologic Characteristic  : {station_data.get('geological_characteristic', '').upper()}
       Bedrock Type           : {station_data.get('bedrock_type', '').upper()}
       Bedrock Condition      : {station_data.get('bedrock_condition', '').upper()}
       Fracture Spacing       : {station_data.get('fracture_spacing', '')}
       Fault zones nearby     : {station_data.get('is_near_fault_zones', '').upper()}
         Distance/activity    :
     Additional Information   : (multiple lines)"""


def _generate_site_location(station_data: Dict[str, Any]) -> str:
    """Generate site location section (Section 2)."""

    lat = station_data.get("lat", 0.0)
    lon = station_data.get("lon", 0.0)
    altitude = station_data.get("altitude", 0.0)

    # Convert to approximate ECEF coordinates
    # This is a simplified conversion - in production should use precise transformations
    import math

    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)

    # WGS84 parameters
    a = 6378137.0  # Semi-major axis
    f = 1 / 298.257223563  # Flattening
    e2 = 2 * f - f * f  # First eccentricity squared

    # Radius of curvature in prime vertical
    N = a / math.sqrt(1 - e2 * math.sin(lat_rad) ** 2)

    # ECEF coordinates
    x = (N + altitude) * math.cos(lat_rad) * math.cos(lon_rad)
    y = (N + altitude) * math.cos(lat_rad) * math.sin(lon_rad)
    z = (N * (1 - e2) + altitude) * math.sin(lat_rad)

    return f"""2.   Site Location Information

     City or Town             :
     State or Province        :
     Country                  : Iceland
     Tectonic Plate           :
     Approximate Position (ITRF)
       X coordinate (m)       : {x:13.4f}
       Y coordinate (m)       : {y:13.4f}
       Z coordinate (m)       : {z:13.4f}
       Latitude (N is +)      : {lat:+012.8f}
       Longitude (E is +)     : {lon:+013.8f}
       Elevation (m,ellips.)  : {altitude:7.1f}
     Additional Information   : (multiple lines)"""


def _generate_receiver_section(device_sessions: List[Dict[str, Any]]) -> str:
    """Generate GNSS receiver section (Section 3)."""

    receiver_sections = []
    section_num = 1

    # Get all receiver sessions sorted by date (direct access to gnss_receiver key)
    receiver_sessions = [
        session for session in device_sessions if "gnss_receiver" in session
    ]

    receiver_sessions.sort(key=lambda x: x.get("time_from") or datetime.min)

    for session in receiver_sessions:
        receiver = session.get("gnss_receiver", {})

        receiver_type = receiver.get("model", "")
        serial_num = receiver.get("serial_number", "")
        firmware_ver = receiver.get("firmware_version", "") or receiver.get(
            "software_version", ""
        )

        date_installed = (
            session.get("time_from", "").strftime("%Y-%m-%dT%H:%MZ")
            if session.get("time_from")
            else ""
        )
        date_removed = (
            session.get("time_to", "").strftime("%Y-%m-%dT%H:%MZ")
            if session.get("time_to")
            else "(CCYY-MM-DDThh:mmZ)"
        )

        receiver_section = f"""3.{section_num}  Receiver Type            : {receiver_type}
     Satellite System         : GPS
     Serial Number            : {serial_num}
     Firmware Version         : {firmware_ver}
     Elevation Cutoff Setting : (deg)
     Date Installed           : {date_installed}
     Date Removed             : {date_removed}
     Temperature Stabiliz.    : (none or tolerance in degrees C)
     Additional Information   : (multiple lines)"""

        receiver_sections.append(receiver_section)
        section_num += 1

    return "\n\n".join(receiver_sections)


def _monument_height_for_period(
    device_sessions: List[Dict[str, Any]],
    time_from: Any,
    time_to: Any,
) -> float:
    """Monument height (mark -> monument top) in effect during an antenna period.

    Monument and antenna live in *separate* device-history sessions with their own
    date ranges, so the monument height for an antenna session must be looked up by
    period overlap. Among the monument sessions overlapping ``[time_from, time_to]``
    the one active at the antenna's install instant is preferred (else the
    latest-starting overlap). Returns 0.0 when no monument session applies — the
    same default the RINEX-header path uses.
    """
    monuments = [
        s
        for s in device_sessions
        if "monument" in s
        and (s.get("monument") or {}).get("monument_height") is not None
    ]
    overlapping = []
    for s in monuments:
        m_from = s.get("time_from")
        m_to = s.get("time_to")
        # No overlap if the antenna period ends before the monument starts ...
        if time_to is not None and m_from is not None and time_to <= m_from:
            continue
        # ... or the monument ends before the antenna period starts.
        if m_to is not None and time_from is not None and m_to <= time_from:
            continue
        overlapping.append(s)
    if not overlapping:
        return 0.0

    def _starts(s: Dict[str, Any]) -> datetime:
        return s.get("time_from") or datetime.min

    # Prefer the monument active at the antenna install instant; else latest start.
    active_at_start = [
        s
        for s in overlapping
        if (
            s.get("time_from") is None
            or time_from is None
            or s.get("time_from") <= time_from
        )
        and (
            s.get("time_to") is None
            or time_from is None
            or s.get("time_to") > time_from
        )
    ]
    chosen = max(active_at_start or overlapping, key=_starts)
    return float((chosen.get("monument") or {}).get("monument_height") or 0.0)


def _generate_antenna_section(device_sessions: List[Dict[str, Any]]) -> str:
    """Generate GNSS antenna section (Section 4)."""

    antenna_sections = []
    section_num = 1

    # Get all antenna sessions sorted by date (direct access to antenna key)
    antenna_sessions = [session for session in device_sessions if "antenna" in session]

    antenna_sessions.sort(key=lambda x: x.get("time_from") or datetime.min)

    for session in antenna_sessions:
        antenna = session.get("antenna", {})

        antenna_type = antenna.get("model", "")
        serial_num = antenna.get("serial_number", "")
        # IGS "Marker->ARP Up Ecc." is the FULL mark -> ARP height and must equal the
        # RINEX header's "ANTENNA: DELTA H". TOS stores the antenna eccentricity
        # (monument-top -> ARP) separately from the monument height (mark ->
        # monument-top), so the published value is the composite of the two — the
        # same sum the RINEX-header path uses (gps_rinex: antenna + monument).
        antenna_ecc = antenna.get("antenna_height", 0.0) or 0.0
        monument_height = _monument_height_for_period(
            device_sessions, session.get("time_from"), session.get("time_to")
        )
        antenna_height = antenna_ecc + monument_height

        # Get radome info if available from same session
        radome_type = "NONE"
        if "radome" in session:
            radome_info = session.get("radome", {})
            radome_type = radome_info.get("model", "NONE")

        date_installed = (
            session.get("time_from", "").strftime("%Y-%m-%dT%H:%MZ")
            if session.get("time_from")
            else ""
        )
        date_removed = (
            session.get("time_to", "").strftime("%Y-%m-%dT%H:%MZ")
            if session.get("time_to")
            else "(CCYY-MM-DDThh:mmZ)"
        )

        antenna_section = f"""4.{section_num}  Antenna Type             : {antenna_type:<16} {radome_type:>4}
     Serial Number            : {serial_num}
     Antenna Reference Point  : BPA (Bottom of Preamplifier)
     Marker->ARP Up Ecc. (m)  : {antenna_height:6.4f}
     Marker->ARP North Ecc(m) : 0.0000
     Marker->ARP East Ecc(m)  : 0.0000
     Alignment from True N    : (deg; + is clockwise/east)
     Antenna Radome Type      : {radome_type}
     Radome Serial Number     :
     Antenna Cable Type       : (vendor & type number)
     Antenna Cable Length     : (m)
     Date Installed           : {date_installed}
     Date Removed             : {date_removed}
     Additional Information   : (multiple lines)"""

        antenna_sections.append(antenna_section)
        section_num += 1

    return "\n\n".join(antenna_sections)


def validate_site_log_completeness(
    station_data: Dict[str, Any],
    device_sessions: List[Dict[str, Any]],
    loglevel: int = logging.WARNING,
) -> Dict[str, List[str]]:
    """
    Validate completeness of site log data.

    Args:
        station_data: Station metadata
        device_sessions: Device session history
        loglevel: Logging level

    Returns:
        Dictionary of missing/incomplete data by section
    """
    logger = get_logger(__name__, loglevel)

    issues = {
        "site_identification": [],
        "location": [],
        "receivers": [],
        "antennas": [],
        "general": [],
    }

    # Check required station fields
    required_station_fields = ["marker", "name", "lat", "lon", "altitude"]
    for field in required_station_fields:
        if not station_data.get(field):
            issues["site_identification"].append(f"Missing {field}")

    # Check for receiver data
    receivers = [s for s in device_sessions if "gnss_receiver" in s]
    if not receivers:
        issues["receivers"].append("No receiver information found")

    # Check for antenna data
    antennas = [s for s in device_sessions if "antenna" in s]
    if not antennas:
        issues["antennas"].append("No antenna information found")

    # Check for missing device details
    for receiver in receivers:
        device = receiver.get("gnss_receiver", {})
        if not device.get("model"):
            issues["receivers"].append("Missing receiver model")
        if not device.get("serial_number"):
            issues["receivers"].append("Missing receiver serial number")

    for antenna in antennas:
        device = antenna.get("antenna", {})
        if not device.get("model"):
            issues["antennas"].append("Missing antenna model")
        if not device.get("serial_number"):
            issues["antennas"].append("Missing antenna serial number")

    total_issues = sum(len(issue_list) for issue_list in issues.values())
    logger.info(f"Site log validation found {total_issues} completeness issues")

    return issues


def export_site_log_to_file(
    site_log_content: str,
    output_path: str,
    marker: str,
    loglevel: int = logging.WARNING,
) -> bool:
    """
    Export site log content to file.

    Args:
        site_log_content: Generated site log content
        output_path: Output file path
        marker: Station marker for filename
        loglevel: Logging level

    Returns:
        True if successful, False otherwise
    """
    logger = get_logger(__name__, loglevel)

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(site_log_content)

        logger.info(f"Site log exported to {output_path}")
        return True

    except Exception as e:
        logger.error(f"Failed to export site log: {e}")
        return False
