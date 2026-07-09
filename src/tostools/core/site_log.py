"""
IGS Site Log generation and management.

This module provides functions for generating IGS-standard site logs from TOS metadata.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..utils.logging import get_logger


def generate_igs_site_log(
    station_data: Dict[str, Any],
    device_sessions: List[Dict[str, Any]],
    loglevel: int = logging.WARNING,
    *,
    country_code: str = "ISL",
    monument_number: str = "00",
    agencies: Optional[Dict[str, Any]] = None,
    prepared_by: str = "GNSS Operator",
    prepared_email: str = "gnss-epos@vedur.is",
    previous_site_log: str = "",
) -> str:
    """
    Generate an IGS v2.0 site log from station and device data.

    Args:
        station_data: Station metadata from TOS
        device_sessions: Device session history
        loglevel: Logging level
        country_code: ISO country for the 9-char station ID (``RHOF00ISL``)
        monument_number: 2-digit monument/receiver number of the 9-char ID
        agencies: Optional agency rendering data for §11/§12/§13 (the caller
            resolves TOS contact roles through ``agencies.yaml`` — tostools takes
            plain dicts so it doesn't depend on the resolver)::

                {"poc":         {agency dict},          # §11 On-Site POC
                 "responsible": {agency dict} | None,   # §12 — None ⇒ same as §11
                 "data_center": {"primary": "IMO", "secondary": "", "url": "..."}}

            An agency dict has ``name_lines`` (list), ``abbrev``, ``address``
            (list), ``contact_name``, ``phone``, ``email``. When ``agencies`` is
            None the sections render as empty v2.0 form placeholders.
        prepared_by / prepared_email: §0 "Prepared by" identity.
        previous_site_log: §0 "Previous Site Log" — the prior dated filename in
            the M3G series (``rhof00isl_20240827.log``); the caller discovers it
            from the site-log archive.

    Returns:
        IGS-formatted site log as string
    """
    logger = get_logger(__name__, loglevel)

    marker = (station_data.get("marker") or "").upper()
    site_name = station_data.get("name") or ""
    iers_domes = station_data.get("iers_domes_number") or ""

    # 9-char station ID (EPOS/IGS v2.0): MARKER + monument/receiver number + country.
    mon = str(monument_number)[:2].rjust(2, "0")
    nine_char = f"{marker}{mon}{country_code.upper()}"

    # Site identification section
    site_id_section = _generate_site_identification(
        marker, site_name, iers_domes, station_data, device_sessions, nine_char
    )

    # Site location section
    location_section = _generate_site_location(station_data)

    # GNSS receiver section
    receiver_section = _generate_receiver_section(device_sessions)

    # GNSS antenna section
    antenna_section = _generate_antenna_section(device_sessions)

    # Static / low-churn sections (§5-§10) + agency sections (§11-§13).
    static_sections = _generate_static_sections(station_data)
    agencies = agencies or {}
    poc_section = _generate_agency_section(
        "11.", "On-Site, Point of Contact Agency Information", agencies.get("poc")
    )
    responsible_section = _generate_agency_section(
        "12.", "Responsible Agency (if different from 11.)", agencies.get("responsible")
    )
    more_info_section = _generate_more_information(agencies.get("data_center"))

    # Combine all sections
    site_log_content = f"""     {nine_char} Site Information Form (site log v2.0)
     International GNSS Service
     See Instructions at:
       https://files.igs.org/pub/station/general/sitelog_instr_v2.0.txt


0.   Form

     Prepared by (full name)  : {prepared_by} ({prepared_email})
     Date Prepared            : {datetime.now().strftime('%Y-%m-%d')}
     Report Type              : UPDATE
     Previous Site Log        : {previous_site_log}
     Modified/Added Sections  : (n.n,n.n,...)


{site_id_section}

{location_section}

{receiver_section}

{antenna_section}

{static_sections}

{poc_section}

{responsible_section}

{more_info_section}
"""

    logger.info(f"Generated IGS site log for {marker}")
    # Normalize: empty-value fields otherwise end "…: " with a trailing space.
    return "\n".join(ln.rstrip() for ln in site_log_content.splitlines()) + "\n"


def _generate_site_identification(
    marker: str,
    site_name: str,
    iers_domes: str,
    station_data: Dict[str, Any],
    device_sessions: List[Dict[str, Any]],
    nine_char: str = "",
) -> str:
    """Generate site identification section (Section 1)."""

    # Get monument information from current session. Height defaults to the
    # catalog default for the monument_height attribute (attribute_codes.yaml:
    # default_value "0.0") rather than the empty "(m)" placeholder — a missing
    # monument record means a zero mark->monument offset, not "unknown". The
    # canonical code is monument_height; antenna_height is only a *legacy*
    # fallback (old records misfiled the height on the monument under the
    # antenna code — flagged by the missing-attributes audit).
    monument_height = "0.0 m"
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
        height_val = device.get("monument_height")
        if height_val is None:
            height_val = device.get("antenna_height")  # legacy fallback only
        monument_height = f"{float(height_val or 0.0)} m"

        monument_description = device.get("description", "STEEL MAST")
        foundation = device.get("foundation", "STEEL RODS")

    return f"""1.   Site Identification of the GNSS Monument

     Site Name                : {site_name}
     Nine Character ID        : {nine_char or marker}
     Monument Inscription     :
     IERS DOMES Number        : {iers_domes}
     CDP Number               :
     Monument Description     : {monument_description}
       Height of the Monument : {monument_height}
       Monument Foundation    : {foundation}
       Foundation Depth       : (m)
     Marker Description       : {station_data.get('marker_description') or ''}
     Date Installed           : {station_data.get('date_start') or ''}
     Geologic Characteristic  : {(station_data.get('geological_characteristic') or '').upper()}
       Bedrock Type           : {(station_data.get('bedrock_type') or '').upper()}
       Bedrock Condition      : {(station_data.get('bedrock_condition') or '').upper()}
       Fracture Spacing       : {station_data.get('fracture_spacing') or ''}
       Fault zones nearby     : {(station_data.get('is_near_fault_zones') or '').upper()}
         Distance/activity    :
     Additional Information   : (multiple lines)"""


def _generate_site_location(station_data: Dict[str, Any]) -> str:
    """Generate site location section (Section 2)."""

    # `or 0.0`: present-but-None coordinates must not crash the ECEF math or the
    # signed fixed-width format specs below.
    lat = station_data.get("lat") or 0.0
    lon = station_data.get("lon") or 0.0
    altitude = station_data.get("altitude") or 0.0

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

    # §3.3 Satellite System — built from the receiver's TOS constellation toggles
    # (GPS/GLO/GAL/BDS/QZSS/SBAS/IRN, those set 'true'), the axis the
    # `tos audit constellations` cross-check populates from the recorded data.
    # Falls back to "GPS" when no toggle is set (the fleet baseline) so an
    # un-populated receiver is no worse than before.
    def _satellite_system(receiver: Dict[str, Any]) -> str:
        codes = [
            c
            for c in ("GPS", "GLO", "GAL", "BDS", "QZSS", "SBAS", "IRN")
            if str(receiver.get(c) or "").strip().lower() == "true"
        ]
        return "+".join(codes) if codes else "GPS"

    # Get all receiver sessions sorted by date (direct access to gnss_receiver key)
    receiver_sessions = [
        session for session in device_sessions if "gnss_receiver" in session
    ]

    receiver_sessions.sort(key=lambda x: x.get("time_from") or datetime.min)

    for session in receiver_sessions:
        receiver = session.get("gnss_receiver", {})

        # `or ""` (not .get default): TOS delivers present-but-None fields, which
        # would crash the fixed-width format specs below (the HAMR/SKOG case).
        receiver_type = receiver.get("model") or ""
        serial_num = receiver.get("serial_number") or ""
        firmware_ver = (
            receiver.get("firmware_version") or receiver.get("software_version") or ""
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
     Satellite System         : {_satellite_system(receiver)}
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

        # `or ""`: a present-but-None model/serial (e.g. HAMR's antenna session)
        # crashed the `{antenna_type:<16}` format spec with NoneType.__format__.
        antenna_type = antenna.get("model") or ""
        serial_num = antenna.get("serial_number") or ""
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
            radome_info = session.get("radome") or {}
            radome_type = radome_info.get("model") or "NONE"

        # §4 Alignment from True N (azimuth / Áttarhorn). Absent → 0.0 (fleet
        # default: north-aligned); the rare non-zero survey is set in TOS and
        # surfaced as a `tos audit missing-attributes` recommended reminder.
        _az_raw = antenna.get("azimuth")
        try:
            alignment = f"{float(_az_raw):.1f}" if _az_raw not in (None, "") else "0.0"
        except (TypeError, ValueError):
            alignment = "0.0"

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
     Alignment from True N    : {alignment}
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


def _generate_static_sections(station_data: Dict[str, Any]) -> str:
    """Sections 5-10 — v2.0 form skeletons for data TOS does not carry.

    §6 Frequency Standard is real data (all our receivers run on their INTERNAL
    oscillator, effective from station start); the rest are emitted as empty v2.0
    form headers so the log is section-complete for M3G/EPOS parsers.
    """
    date_start = str(station_data.get("date_start") or "")[:10] or "(CCYY-MM-DD)"
    return f"""5.   Surveyed Local Ties

5.x  Tied Marker Name         :


6.   Frequency Standard

6.1  Standard Type            : INTERNAL
       Input Frequency        :
       Effective Dates        : {date_start}/CCYY-MM-DD
       Notes                  : (multiple lines)


7.   Collocation Information

7.x  Instrumentation Type     : (GPS/GLONASS/DORIS/PRARE/SLR/VLBI/TIME/etc)


8.   Meteorological Instrumentation

8.1.x Humidity Sensor Model   :


9.   Local Ongoing Conditions Possibly Affecting Computed Position

9.1.x Radio Interferences     : (TV/CELL PHONE ANTENNA/RADAR/etc)


10.  Local Episodic Effects Possibly Affecting Data Quality

10.x Date                     : (CCYY-MM-DD/CCYY-MM-DD)
     Event                    : (TREE CLEARING/CONSTRUCTION/etc)"""


def _multiline_value(label: str, lines: List[str]) -> str:
    """Render ``label : v1`` with continuation lines (`` : v2`` …) per IGS format."""
    lines = [ln for ln in (lines or []) if ln] or [""]
    out = [f"     {label:<25}: {lines[0]}"]
    for extra in lines[1:]:
        out.append(f"     {'':<25}: {extra}")
    return "\n".join(out)


def _generate_agency_section(
    number: str, title: str, agency: Optional[Dict[str, Any]]
) -> str:
    """§11 / §12 agency-information block.

    ``agency`` is a plain dict (``name_lines``, ``abbrev``, ``address``,
    ``contact_name``, ``phone``, ``email``) — resolved by the caller from TOS
    contact roles + agencies.yaml. None ⇒ the empty v2.0 form placeholders (used
    for §12 when the responsible agency IS the §11 contact, per the form's
    "if different from 11.").
    """
    ag = agency or {}
    name_lines = ag.get("name_lines") or []
    address = ag.get("address") or []
    return f"""{number:<5}{title}

{_multiline_value("Agency", list(name_lines))}
     Preferred Abbreviation   : {ag.get("abbrev") or ""}
{_multiline_value("Mailing Address", list(address))}
     Primary Contact
       Contact Name           : {ag.get("contact_name") or ""}
       Telephone (primary)    : {ag.get("phone") or ""}
       Telephone (secondary)  :
       Fax                    :
       E-mail                 : {ag.get("email") or ""}
     Secondary Contact
       Contact Name           :
       Telephone (primary)    :
       Telephone (secondary)  :
       Fax                    :
       E-mail                 :
     Additional Information   : (multiple lines)"""


def _generate_more_information(data_center: Optional[Dict[str, Any]]) -> str:
    """§13 More Information — data centers + URL (from the agency resolution)."""
    dc = data_center or {}
    return f"""13.  More Information

     Primary Data Center      : {dc.get("primary") or ""}
     Secondary Data Center    : {dc.get("secondary") or ""}
     URL for More Information : {dc.get("url") or ""}
     Hardcopy on File
       Site Map               : (Y or URL)
       Site Diagram           : (Y or URL)
       Horizon Mask           : (Y or URL)
       Monument Description   : (Y or URL)
       Site Pictures          : (Y or URL)
     Additional Information   : (multiple lines)
     Antenna Graphics with Dimensions"""


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
