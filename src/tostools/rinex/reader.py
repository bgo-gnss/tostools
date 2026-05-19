"""
RINEX file reading and parsing utilities.

This module provides functions for reading and parsing RINEX files,
including support for various compression formats.
"""

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from ..io.file_utils import read_gzip_file, read_text_file, read_zzipped_file
from ..utils.logging import get_logger

# Lowercase month names — match the on-disk archive layout
# ``<base>/<YYYY>/<mon>/<STA>/...`` used by the IMO GPS pipeline.
MONTHS = [
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
]


def _parse_daily_rinex_date(name: str, station: str) -> Optional[datetime]:
    """
    Parse the observation date from a daily Hatanaka RINEX filename.

    Expected pattern: ``STA<DDD>0.<YY>[DO][.Z|.gz]`` where DDD is day of year
    and YY is two-digit year (pivot 80: <80 → 2000+YY, otherwise 1900+YY).

    Args:
        name: Bare filename (no directory)
        station: Four-character station code

    Returns:
        ``datetime`` at 00:00 on the parsed date, or ``None`` if the name
        does not match the daily pattern
    """
    m = re.match(
        rf"^{re.escape(station)}(\d{{3}})\d\.(\d{{2}})[DO](?:\.Z|\.gz)?$",
        name,
        re.IGNORECASE,
    )
    if not m:
        return None
    doy = int(m.group(1))
    yy = int(m.group(2))
    year = 2000 + yy if yy < 80 else 1900 + yy
    return datetime(year, 1, 1) + timedelta(days=doy - 1)


def _scan_dir_dates(
    directory: Path,
    parser,
    station: str,
) -> List[Tuple[datetime, Path]]:
    """Return ``(date, path)`` pairs for every file in ``directory`` whose
    name is recognised by ``parser``. Returns an empty list if the directory
    does not exist.
    """
    if not directory.is_dir():
        return []
    dates: List[Tuple[datetime, Path]] = []
    for f in directory.iterdir():
        dt = parser(f.name, station)
        if dt is not None:
            dates.append((dt, f))
    return dates


def find_most_recent_rinex(
    station: str,
    base_dir: Union[str, Path] = "/mnt_data/rawgpsdata",
    rate_dir: str = "15s_24hr",
) -> Optional[Path]:
    """
    Return the most recent daily RINEX file for a station.

    Archive layout:
    ``<base_dir>/<YYYY>/<mon>/<STATION>/<rate_dir>/rinex/<STA><DDD>0.<YY>[DO][.Z|.gz]``

    Walks years and months descending and, within the first populated month
    found, returns the file with the highest ``(year, DOY)``.

    Args:
        station: Four-character station code (case-insensitive)
        base_dir: Archive root directory
        rate_dir: Sampling-rate subdirectory (default ``"15s_24hr"``)

    Returns:
        Path to the most recent daily RINEX file, or ``None`` if no
        matching file is found under ``base_dir``.
    """
    base = Path(base_dir)
    if not base.is_dir():
        return None

    sta = station.upper()
    years = sorted(
        (p for p in base.iterdir() if p.is_dir() and p.name.isdigit()),
        reverse=True,
    )
    for year_dir in years:
        months_present = [m for m in MONTHS if (year_dir / m).is_dir()]
        for mon in reversed(months_present):
            rinex_dir = year_dir / mon / sta / rate_dir / "rinex"
            dates = _scan_dir_dates(rinex_dir, _parse_daily_rinex_date, sta)
            if dates:
                dates.sort()
                return dates[-1][1]
    return None


def get_rinex_labels() -> Tuple[List[str], List[str]]:
    """
    Get standard RINEX header labels and their corresponding format strings.

    Returns:
        Tuple of (search_list, format_list) for RINEX header parsing
    """
    search_list = [
        "MARKER NAME",
        "MARKER NUMBER",
        "OBSERVER / AGENCY",
        "REC # / TYPE / VERS",
        "ANT # / TYPE",
        "APPROX POSITION XYZ",
        "ANTENNA: DELTA H/E/N",
        "INTERVAL",
        "TIME OF FIRST OBS",
    ]

    fortran_format_list = [
        "(A60,20X)",
        "(A20,40X)",
        "(A20,A20,20X)",
        "(A20,A20,A20)",
        "(A20,A20,20X)",
        "(3F14.4,18X)",
        "(3F14.4,18X)",
        "(F10.3,50X)",
        "(I6,I6,F13.7,A3,9X,A3,9X)",
    ]

    return search_list, fortran_format_list


def read_rinex_file(
    file_path: Union[str, Path], loglevel: int = logging.WARNING
) -> Optional[bytes]:
    """
    Read a RINEX file with automatic format detection.

    Args:
        file_path: Path to RINEX file
        loglevel: Logging level

    Returns:
        File content as bytes, or None if error
    """
    logger = get_logger(__name__, loglevel)
    path = Path(file_path)

    # Handle compression formats (check for .Z and .gz at the end)
    if str(path).endswith(".Z"):
        return read_zzipped_file(path, loglevel)
    elif str(path).endswith(".gz"):
        return read_gzip_file(path, loglevel)
    elif path.suffix in [".rnx", ".obs", ".nav", ""]:
        content = read_text_file(path, loglevel)
        return content.encode() if content else None
    # Check for RINEX 2 short-name format (e.g., .24o, .24d, .24n, .24D, .26oT)
    # Format: .YYx or .YYxT where YY=year, x=file type, T=temp suffix
    elif len(path.suffix) >= 4 and path.suffix[1:3].isdigit():
        # Valid RINEX 2 file types: o=obs, d=hatanaka, n=nav, m=met, g=glonass, etc.
        file_type = path.suffix[3].upper()
        is_temp = len(path.suffix) == 5 and path.suffix[4].upper() == "T"
        if file_type in "ODNMGLP" or is_temp:
            content = read_text_file(path, loglevel)
            return content.encode() if content else None
        # Fall through to unknown format handler
    else:
        pass  # Fall through to unknown format handler

    # Unknown format - warn but try anyway
    logger.warning(f"Unknown file format: {path.suffix}")
    content = read_text_file(path, loglevel)
    return content.encode() if content else None


def read_rinex_header(
    file_path: Union[str, Path], loglevel: int = logging.WARNING
) -> Optional[Dict[str, Union[str, Path]]]:
    """
    Read and extract the header section from a RINEX file.

    Args:
        file_path: Path to RINEX file
        loglevel: Logging level

    Returns:
        Dictionary containing file info and header string, or None if error
    """
    logger = get_logger(__name__, loglevel)
    path = Path(file_path)

    # Read file content
    file_content = read_rinex_file(path, loglevel)
    if not file_content:
        return None

    try:
        # Decode content
        content_str = file_content.decode("utf-8", errors="ignore")

        # Find header section (ends with "END OF HEADER")
        header_end = content_str.find("END OF HEADER")
        if header_end == -1:
            logger.warning(f"No 'END OF HEADER' found in {path}")
            return None

        # Extract header (include the END OF HEADER line)
        header_section = content_str[: header_end + len("END OF HEADER")]

        return {"rinex file": [str(path.parent), path.name], "header": header_section}

    except Exception as e:
        logger.error(f"Error reading RINEX header from {path}: {e}")
        return None


def extract_header_info(
    header_data: Dict[str, Union[str, List[str]]], loglevel: int = logging.WARNING
) -> Dict[str, str]:
    """
    Extract specific information from RINEX header.

    Args:
        header_data: Header data from read_rinex_header
        loglevel: Logging level

    Returns:
        Dictionary with extracted header information
    """
    logger = get_logger(__name__, loglevel)

    if not header_data or "header" not in header_data:
        return {}

    header_str = header_data["header"]
    search_labels, _ = get_rinex_labels()

    extracted_info = {}

    # Add file information
    rinex_file = header_data.get("rinex file", ["", ""])
    extracted_info["file_path"] = rinex_file[0] if len(rinex_file) > 0 else ""
    extracted_info["file_name"] = rinex_file[1] if len(rinex_file) > 1 else ""

    # Extract each label from header
    for label in search_labels:
        # Find line containing the label
        for line in header_str.split("\n"):
            if label in line:
                # Extract the content before the label
                content = line[: line.find(label)].strip()
                extracted_info[label] = content
                logger.debug(f"Extracted {label}: {content}")
                break
        else:
            logger.debug(f"Label '{label}' not found in header")
            extracted_info[label] = ""

    return extracted_info


def parse_rinex_observation_types(header_str: str) -> List[str]:
    """
    Parse observation types from RINEX header.

    Args:
        header_str: RINEX header string

    Returns:
        List of observation types
    """
    obs_types = []

    # Look for observation types (version dependent)
    for line in header_str.split("\n"):
        if "# / TYPES OF OBSERV" in line:
            # RINEX 2.x format
            parts = line.split()
            if len(parts) > 1:
                num_obs = int(parts[0])
                obs_types = parts[1 : num_obs + 1]
            break
        elif "SYS / # / OBS TYPES" in line:
            # RINEX 3.x format
            parts = line.split()
            if len(parts) > 2:
                obs_types.extend(parts[2:-1])  # Skip system and count, and label

    return obs_types


def get_rinex_version(header_str: str) -> str:
    """
    Extract RINEX version from header.

    Args:
        header_str: RINEX header string

    Returns:
        RINEX version string
    """
    for line in header_str.split("\n"):
        if "RINEX VERSION / TYPE" in line:
            return line[:9].strip()
    return "Unknown"


def validate_rinex_header(
    header_info: Dict[str, str], loglevel: int = logging.WARNING
) -> Dict[str, List[str]]:
    """
    Validate RINEX header completeness and consistency.

    Args:
        header_info: Extracted header information
        loglevel: Logging level

    Returns:
        Dictionary of validation issues by category
    """
    logger = get_logger(__name__, loglevel)

    issues = {
        "missing_required": [],
        "empty_fields": [],
        "format_issues": [],
        "warnings": [],
    }

    # Required fields for GPS applications
    required_fields = [
        "MARKER NAME",
        "OBSERVER / AGENCY",
        "REC # / TYPE / VERS",
        "ANT # / TYPE",
        "APPROX POSITION XYZ",
    ]

    for field in required_fields:
        if field not in header_info:
            issues["missing_required"].append(f"Missing field: {field}")
        elif not header_info[field].strip():
            issues["empty_fields"].append(f"Empty field: {field}")

    # Check position format
    if "APPROX POSITION XYZ" in header_info:
        pos_str = header_info["APPROX POSITION XYZ"].strip()
        if pos_str:
            try:
                coords = [float(x) for x in pos_str.split()]
                if len(coords) != 3:
                    issues["format_issues"].append(
                        "APPROX POSITION XYZ should contain 3 coordinates"
                    )
                elif all(abs(c) < 1 for c in coords):
                    issues["warnings"].append(
                        "Position coordinates seem too small (possible error)"
                    )
            except ValueError:
                issues["format_issues"].append(
                    "APPROX POSITION XYZ contains non-numeric values"
                )

    total_issues = sum(len(issue_list) for issue_list in issues.values())
    logger.info(f"Header validation found {total_issues} issues")

    return issues
