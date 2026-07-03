"""
RINEX header correction using TOS metadata or station config.

This module provides high-level functions for correcting RINEX headers:
- For recent dates: Uses station.cfg (fast, no network dependency)
- For historical dates: Queries TOS database

Architecture:
    Daily operations use station.cfg to avoid constantly querying TOS.
    Historical reprocessing queries TOS when config_valid_from is missing
    or observation date is before the config validity period.
"""

import gzip
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..gps_metadata_qc import URL_REST_TOS, gps_metadata
from ..utils.logging import get_logger
from .reader import get_rinex_labels, read_rinex_header


def _is_placeholder_serial(serial: str) -> bool:
    """Check if a serial number is a TOS placeholder/assigned value.

    TOS assigns placeholder serial numbers when the actual serial is unknown.
    These typically follow patterns like:
    - "antenna-{station}-{date}" (e.g., "antenna-thob-20200128")
    - "receiver-{station}-{date}"
    - "Unknown"

    Args:
        serial: Serial number string to check

    Returns:
        True if this appears to be a placeholder, False if it's a real serial
    """
    if not serial:
        return True

    serial_lower = serial.lower().strip()

    # Check for common placeholder patterns
    if serial_lower == "unknown":
        return True
    if serial_lower.startswith("antenna-"):
        return True
    if serial_lower.startswith("receiver-"):
        return True
    if serial_lower.startswith("radome-"):
        return True

    return False


def correct_rinex_from_tos(
    rinex_file: Path,
    station_id: str,
    observation_date: Optional[datetime] = None,
    output_file: Optional[Path] = None,
    station_config: Optional[Dict[str, Any]] = None,
    loglevel: int = logging.INFO,
    only_fields: Optional[set] = None,
) -> Optional[Path]:
    """
    Correct RINEX header using TOS metadata or station config.

    Logic:
    1. If station_config provided with valid config_valid_from:
       - If observation_date >= config_valid_from: use station_config
       - Otherwise: query TOS
    2. If station_config not provided or no config_valid_from: query TOS
    3. Apply corrections using fortranformat for proper RINEX formatting
    4. Write corrected file

    Args:
        rinex_file: Path to RINEX file to correct
        station_id: 4-character station identifier
        observation_date: Date of observation (auto-detected from file if None)
        output_file: Output path (default: overwrite in place)
        station_config: Station configuration dict from gps_parser.
                       Should contain 'rinex' section with config_valid_from.
        loglevel: Logging level
        only_fields: Optional set of RINEX header labels (e.g. ``{"ANTENNA: DELTA H/E/N"}``)
                     to restrict the rewrite to. When None (default) all known fields
                     are corrected. When set, only corrections whose label is in the
                     set are applied — used by ``receivers rinex --fix-headers`` to
                     touch only the fields that actually differ from TOS.

    Returns:
        Path to corrected file, or None if correction failed

    Example:
        >>> from tostools.rinex.corrector import correct_rinex_from_tos
        >>> # With station config (daily operations - no TOS query)
        >>> correct_rinex_from_tos(
        ...     Path("THOB0160.26o"),
        ...     "THOB",
        ...     station_config=station_cfg,
        ... )
        >>> # Without config (historical - queries TOS)
        >>> correct_rinex_from_tos(Path("THOB0010.20o"), "THOB")
    """
    logger = get_logger(__name__, loglevel)
    rinex_file = Path(rinex_file)

    if not rinex_file.exists():
        logger.error(f"RINEX file not found: {rinex_file}")
        return None

    # Auto-detect observation date from filename if not provided
    if observation_date is None:
        observation_date = _extract_date_from_rinex(rinex_file)
        if observation_date is None:
            logger.warning(f"Could not extract date from {rinex_file.name}")

    # Determine whether to use config or TOS
    use_config = False
    if station_config:
        rinex_cfg = station_config.get("rinex", {})
        valid_from_str = rinex_cfg.get("config_valid_from", "")
        if valid_from_str:
            try:
                config_valid_from = datetime.strptime(valid_from_str, "%Y-%m-%d")
                if (
                    observation_date
                    and observation_date.date() >= config_valid_from.date()
                ):
                    use_config = True
                    logger.debug(
                        f"Using station.cfg for {station_id} "
                        f"(date {observation_date.date()} >= {config_valid_from.date()})"
                    )
            except ValueError:
                logger.warning(f"Invalid config_valid_from format: {valid_from_str}")

    # Get metadata from appropriate source
    if use_config and station_config:
        corrections = _get_corrections_from_config(station_id, station_config, logger)
    else:
        logger.debug(f"Querying TOS for {station_id} metadata")
        corrections = _get_corrections_from_tos(station_id, observation_date, loglevel)

    if not corrections:
        logger.warning(f"No corrections available for {station_id}")
        return rinex_file  # Return original file unchanged

    # Field-selective mode: keep only the requested labels (used by
    # `receivers rinex --fix-headers` to touch only discrepant fields).
    if only_fields is not None:
        corrections = {
            label: values
            for label, values in corrections.items()
            if label in only_fields
        }
        if not corrections:
            logger.debug(
                "No corrections after only_fields filter for %s — leaving file unchanged",
                station_id,
            )
            return rinex_file

    # Apply corrections
    corrected_file = _apply_corrections(rinex_file, corrections, output_file, logger)

    return corrected_file


def _get_corrections_from_config(
    station_id: str,
    station_config: Dict[str, Any],
    logger: logging.Logger,
) -> Dict[str, Any]:
    """Extract RINEX corrections from station.cfg configuration."""
    corrections = {}

    rinex_cfg = station_config.get("rinex", {})
    antenna_cfg = station_config.get("antenna", {})

    # MARKER NAME
    marker_name = rinex_cfg.get("marker_name", station_id.upper())
    if marker_name:
        corrections["MARKER NAME"] = [marker_name]

    # MARKER NUMBER
    marker_number = rinex_cfg.get("marker_number", "")
    if marker_number:
        corrections["MARKER NUMBER"] = [marker_number]

    # OBSERVER / AGENCY
    observer = rinex_cfg.get("observer", "")
    agency = rinex_cfg.get("agency", "")
    if observer or agency:
        corrections["OBSERVER / AGENCY"] = [observer or "", agency or ""]

    # ANT # / TYPE (serial + type with radome)
    ant_serial = antenna_cfg.get("serial", "")
    ant_type = antenna_cfg.get("type", "")
    ant_radome = antenna_cfg.get("radome", "NONE")
    if ant_serial:
        # Format: serial (20) + type with radome (20)
        ant_type_full = f"{ant_type:<15} {ant_radome:<4}" if ant_type else ""
        corrections["ANT # / TYPE"] = [ant_serial, ant_type_full]

    # ANTENNA: DELTA H/E/N
    ant_height = float(antenna_cfg.get("height", 0) or 0)
    corrections["ANTENNA: DELTA H/E/N"] = [ant_height, 0.0, 0.0]

    logger.debug(f"Corrections from config: {list(corrections.keys())}")
    return corrections


def _get_corrections_from_tos(
    station_id: str,
    observation_date: Optional[datetime],
    loglevel: int,
) -> Dict[str, Any]:
    """Get RINEX corrections from TOS database.

    TOS returns station data with device_history containing sessions:
    - time_from, time_to: Session time period
    - gnss_receiver: {model, serial_number, firmware_version}
    - antenna: {model, serial_number, antenna_height}
    - radome: {model}
    - monument: {monument_height}
    """
    logger = get_logger(__name__, loglevel)

    try:
        # Get station metadata from TOS
        station_data = gps_metadata(station_id, URL_REST_TOS, loglevel=loglevel)

        if not station_data:
            logger.warning(f"No TOS data found for {station_id}")
            return {}

        # Get device history (list of sessions)
        device_history = station_data.get("device_history", [])
        if not device_history:
            logger.warning(f"No device history found in TOS for {station_id}")
            return {}

        # Find session covering observation date
        session = None
        for s in device_history:
            time_from = s.get("time_from")
            time_to = s.get("time_to")

            # Handle datetime objects or strings
            if isinstance(time_from, str):
                try:
                    time_from = datetime.fromisoformat(time_from.replace("Z", "+00:00"))
                except ValueError:
                    time_from = None
            if isinstance(time_to, str) and time_to:
                try:
                    time_to = datetime.fromisoformat(time_to.replace("Z", "+00:00"))
                except ValueError:
                    time_to = None

            # Check if observation date falls within session
            if observation_date:
                obs_date = (
                    observation_date.replace(tzinfo=None)
                    if observation_date.tzinfo
                    else observation_date
                )
                if time_from:
                    tf = (
                        time_from.replace(tzinfo=None)
                        if hasattr(time_from, "tzinfo") and time_from.tzinfo
                        else time_from
                    )
                    if obs_date < tf:
                        continue
                if time_to:
                    tt = (
                        time_to.replace(tzinfo=None)
                        if hasattr(time_to, "tzinfo") and time_to.tzinfo
                        else time_to
                    )
                    if obs_date > tt:
                        continue
            session = s
            break

        if session is None and device_history:
            # Use most recent session as fallback
            session = device_history[-1]
            logger.debug(f"Using most recent TOS session for {station_id}")

        if session is None:
            return {}

        logger.info(
            f"Found TOS session for {station_id}: {session.get('time_from')} - {session.get('time_to')}"
        )

        # Extract corrections from TOS session
        # TOS uses keys like 'gnss_receiver', 'antenna', 'radome', 'monument'
        corrections = {}

        # MARKER NAME
        corrections["MARKER NAME"] = [station_id.upper()]

        # REC # / TYPE / VERS - from gnss_receiver
        receiver = session.get("gnss_receiver", {})
        if receiver:
            corrections["REC # / TYPE / VERS"] = [
                receiver.get("serial_number", ""),
                receiver.get("model", ""),
                receiver.get("firmware_version", ""),
            ]

        # ANT # / TYPE
        antenna = session.get("antenna", {})
        radome = session.get("radome", {})
        if antenna:
            ant_model = antenna.get("model", "")
            radome_model = radome.get("model", "NONE") if radome else "NONE"
            ant_type_full = f"{ant_model:<15} {radome_model:<4}" if ant_model else ""
            # Use "0000" if serial is a TOS placeholder
            ant_serial = antenna.get("serial_number", "")
            if _is_placeholder_serial(ant_serial):
                ant_serial = "0000"
            corrections["ANT # / TYPE"] = [ant_serial, ant_type_full]

        # ANTENNA: DELTA H/E/N
        ant_height = float(antenna.get("antenna_height", 0) or 0) if antenna else 0.0
        monument = session.get("monument", {})
        mon_height = float(monument.get("monument_height", 0) or 0) if monument else 0.0
        corrections["ANTENNA: DELTA H/E/N"] = [ant_height + mon_height, 0.0, 0.0]

        logger.debug(f"Corrections from TOS: {list(corrections.keys())}")
        return corrections

    except Exception as e:
        logger.error(f"TOS query failed for {station_id}: {e}")
        import traceback

        logger.debug(traceback.format_exc())
        return {}


def _format_rinex_data(label: str, values: list) -> Optional[str]:
    """Format RINEX header data fields with correct column widths.

    RINEX uses fixed-width format with left-justified strings.
    This function returns the 60-character data portion of a header line.

    Args:
        label: RINEX header label (e.g., "MARKER NAME")
        values: List of values for this field

    Returns:
        60-character formatted string, or None if formatting fails
    """
    if label == "MARKER NAME":
        # A60: Single 60-char field, left-justified
        v = str(values[0]) if values else ""
        return v.upper().ljust(60)[:60]

    elif label == "MARKER NUMBER":
        # A20 + 40X: 20-char field + 40 spaces
        v = str(values[0]) if values else ""
        return v.ljust(20)[:20] + " " * 40

    elif label == "OBSERVER / AGENCY":
        # A20 + A20 + 20X: observer(20) + agency(20) + 20 spaces
        obs = str(values[0]).ljust(20)[:20] if len(values) > 0 else " " * 20
        agency = str(values[1]).ljust(40)[:40] if len(values) > 1 else " " * 40
        return f"{obs}{agency}"

    elif label == "REC # / TYPE / VERS":
        # A20 + A20 + A20: serial(20) + type(20) + version(20)
        serial = str(values[0]).ljust(20)[:20] if len(values) > 0 else " " * 20
        rtype = str(values[1]).ljust(20)[:20] if len(values) > 1 else " " * 20
        vers = str(values[2]).ljust(20)[:20] if len(values) > 2 else " " * 20
        return f"{serial}{rtype}{vers}"

    elif label == "ANT # / TYPE":
        # A20 + A20 + 20X: serial(20) + type(20) + 20 spaces
        serial = str(values[0]).ljust(20)[:20] if len(values) > 0 else " " * 20
        atype = str(values[1]).ljust(20)[:20] if len(values) > 1 else " " * 20
        return f"{serial}{atype}" + " " * 20

    elif label == "ANTENNA: DELTA H/E/N":
        # 3F14.4 + 18X: three 14-char floats + 18 spaces
        h = float(values[0]) if len(values) > 0 else 0.0
        e = float(values[1]) if len(values) > 1 else 0.0
        n = float(values[2]) if len(values) > 2 else 0.0
        return f"{h:14.4f}{e:14.4f}{n:14.4f}" + " " * 18

    elif label == "APPROX POSITION XYZ":
        # 3F14.4 + 18X: three 14-char floats + 18 spaces
        x = float(values[0]) if len(values) > 0 else 0.0
        y = float(values[1]) if len(values) > 1 else 0.0
        z = float(values[2]) if len(values) > 2 else 0.0
        return f"{x:14.4f}{y:14.4f}{z:14.4f}" + " " * 18

    else:
        # Unknown field - try to format as string
        v = str(values[0]) if values else ""
        return v.ljust(60)[:60]


def _apply_corrections(
    rinex_file: Path,
    corrections: Dict[str, Any],
    output_file: Optional[Path],
    logger: logging.Logger,
) -> Path:
    """Apply corrections to RINEX file header."""
    # Read the RINEX file
    rheader = read_rinex_header(rinex_file)
    if not rheader or "header" not in rheader:
        logger.error(f"Failed to read RINEX header from {rinex_file}")
        return rinex_file

    header_content: str = str(rheader["header"])
    rinex_labels, _ = get_rinex_labels()

    # Apply each correction
    for label, values in corrections.items():
        if label not in rinex_labels:
            logger.debug(f"Unknown label {label}, skipping")
            continue

        # Create the corrected line using proper RINEX formatting
        try:
            # Format data part based on field type (columns 1-60)
            data_part = _format_rinex_data(label, values)
            if data_part is None:
                logger.debug(f"Could not format {label}, skipping")
                continue

            # RINEX format: 60 chars data + 20 chars label
            new_line = f"{data_part}{label}"

            # Replace in header
            pattern = rf"(^.*{re.escape(label)}.*$)"
            mstring = re.compile(pattern, re.M)
            if mstring.search(header_content):
                header_content = re.sub(mstring, new_line, header_content)
                logger.debug(f"Applied correction to {label}")
            else:
                logger.debug(f"Label {label} not found in header")
        except Exception as e:
            logger.warning(f"Failed to apply correction for {label}: {e}")

    # Determine output path
    if output_file is None:
        output_file = rinex_file

    # Write corrected file
    _write_rinex_file(rinex_file, header_content, output_file, logger)

    return output_file


def _write_rinex_file(
    original_file: Path,
    new_header: str,
    output_file: Path,
    logger: logging.Logger,
) -> None:
    """Write RINEX file with corrected header.

    The CRINEX data section (after END OF HEADER) is binary for Hatanaka-
    compressed files; decoding it as text corrupts it. So we work with BYTES
    for reading and the header/data split, decode only the header portion,
    replace it, and re-encode — keeping the data section bit-identical."""
    import subprocess
    import tempfile

    # Determine original format
    is_gzipped = original_file.suffix == ".gz"
    is_z_compressed = original_file.suffix == ".Z"

    # Read the FULL content as BYTES (never decode the data section for
    # CRINEX/Hatanaka files where it may be binary).
    if is_gzipped:
        with gzip.open(original_file, "rb") as f:
            content_bytes = f.read()
    elif is_z_compressed:
        proc = subprocess.run(
            ["zcat", str(original_file)], capture_output=True, check=True
        )
        content_bytes = proc.stdout
    else:
        content_bytes = original_file.read_bytes()

    # Find END OF HEADER as bytes, split header bytes from data bytes.
    end_marker_b = b"END OF HEADER"
    idx = content_bytes.find(end_marker_b)
    if idx >= 0:
        # header_bytes = everything up to and including END OF HEADER + trailing \n
        eoh_end = idx + len(end_marker_b)
        nl = content_bytes.find(b"\n", eoh_end)
        header_end = nl + 1 if nl >= 0 else eoh_end
        header_bytes = content_bytes[:header_end]
        data_bytes = content_bytes[header_end:]
        # Decode ONLY the header for the regex-based edits (errors='ignore'
        # to survive any stray non-UTF-8 bytes in the header).
        header_text = header_bytes.decode("utf-8", errors="ignore")
        # Replace the header in the text domain and re-encode.
        new_content_bytes = new_header.encode("utf-8") + data_bytes
    else:
        logger.warning("END OF HEADER not found (bytes), replacing entire content")
        new_content_bytes = new_header.encode("utf-8")

    # Write output — re-compress in the SAME format as the original so the
    # corrected file is byte-compatible with the archive convention.
    temp_file = output_file.with_suffix(".tmp")
    try:
        if output_file.suffix == ".gz":
            with gzip.open(temp_file, "wb") as f:
                f.write(new_content_bytes)
        elif output_file.suffix == ".Z":
            # Unix-compress (LZW). Write plain bytes to a temp file, run
            # compress.  ``compress foo.plain`` produces ``foo.plain.Z`` —
            # NOT a simple suffix-replacement of .plain with .Z — so read the
            # actual compress output filename rather than assuming.
            import subprocess

            plain = temp_file.with_name(temp_file.name + ".plain")
            plain.write_bytes(new_content_bytes)
            subprocess.run(
                ["compress", "-f", str(plain)],
                check=True,
                capture_output=True,
            )
            compressed = plain.with_name(plain.name + ".Z")
            temp_file.write_bytes(compressed.read_bytes())
            compressed.unlink(missing_ok=True)
            plain.unlink(missing_ok=True)
        else:
            temp_file.write_bytes(new_content_bytes)

        # Atomic replace
        shutil.move(temp_file, output_file)
        logger.info(f"Wrote corrected RINEX to {output_file}")

    except Exception as e:
        logger.error(f"Failed to write {output_file}: {e}")
        if temp_file.exists():
            temp_file.unlink()
        raise


def _extract_date_from_rinex(rinex_file: Path) -> Optional[datetime]:
    """Extract observation date from RINEX filename."""
    from gtimes.timefunc import datefRinex

    try:
        # Remove compression extensions
        name = rinex_file.name
        for ext in [".gz", ".Z"]:
            if name.endswith(ext):
                name = name[: -len(ext)]

        dates = datefRinex([name])
        if dates and dates[0]:
            return dates[0]
    except Exception:
        pass

    return None
