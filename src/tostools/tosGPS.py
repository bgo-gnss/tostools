#!python

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml
from argparse_logging import add_log_level_argument

from . import gps_metadata_qc as gpsqc

# Import new modular components
from .api.tos_client import TOSClient
from .legacy import gps_metadata_functions as gpsf

# Use the comprehensive legacy site log generator
# from .core.site_log import generate_igs_site_log
from .rinex.editor import update_rinex_files
from .rinex.reader import extract_header_info, read_rinex_header
from .rinex.validator import compare_rinex_to_tos
from .utils.data_quality import data_quality_manager

# Import new logging system
from .utils.logging import (
    LoggingConfig,
    configure_logging,
    get_logger,
    setup_console_logging,
)

# Reference data configuration
REFERENCE_DATA_CONFIG = {
    "station-info": {
        "remote_path": "/D/DATABASE/GAMIT/station.info.sopac.apr05",
        "remote_host": "gpsops@okada",
        "local_filename": "station.info.sopac.apr05",
        "description": "SOPAC station info file for GAMIT processing",
    }
}


def _select_synthesizer(args):
    """Return the gps_metadata function selected by ``--use-legacy-synthesis``.

    Both candidates have the same calling convention — drop-in
    swappable. Defaults to the new ``devices.station_sessions``
    composer chain (phase 5 default). Pass ``--use-legacy-synthesis``
    to opt back into the legacy ``gps_metadata_qc.gps_metadata``
    chain — kept available for ops compatibility during the
    transition and for side-by-side debugging, but slated for
    removal once production has run on the new chain long enough.
    """
    if getattr(args, "use_legacy_synthesis", False):
        return gpsqc.gps_metadata
    return gpsqc.gps_metadata_via_devices


def generate_igs_sitelog_filename(
    station_marker: str,
    country_code: str = "ISL",
    monument_number: str = "00",
    include_date: bool = False,
    base_dir: str = ".",
    custom_date: str = None,
    create_station_subdir: bool = True,
) -> tuple[str, str]:
    """
    Generate IGS-compliant site log filename and directory path.

    Format without date: {STATION}{MONUMENT}{COUNTRY}.log
    Format with date: {station}{monument}{country}_{YYYYMMDD}.log
    Example: RHOF00ISL.log or rhof00isl_20250825.log

    Args:
        station_marker: 4-character station code (e.g., "RHOF")
        country_code: 3-character country code (default: "ISL" for Iceland)
        monument_number: 2-digit monument number (default: "00" for main monument)
        include_date: Whether to include current date in filename
        base_dir: Base directory for site log storage
        create_station_subdir: Whether to create station-specific subdirectory (default: True)

    Returns:
        Tuple of (full_path, filename_only)
    """
    import os
    from datetime import datetime

    station_id = f"{station_marker.upper()}{monument_number}{country_code.upper()}"

    if create_station_subdir:
        output_dir = os.path.join(base_dir, station_id)
    else:
        output_dir = base_dir

    if include_date:
        if custom_date:
            date_str = custom_date  # Use provided date (YYYYMMDD format)
        else:
            date_str = datetime.now().strftime("%Y%m%d")
        filename = f"{station_id.lower()}_{date_str}.log"
    else:
        filename = f"{station_id}.log"

    full_path = os.path.join(output_dir, filename)
    return full_path, filename


def find_previous_sitelog(station_dir: str, station_id: str) -> str:
    """
    Find the most recent site log file in the station directory.

    Args:
        station_dir: Directory to search for previous logs
        station_id: Station identifier (e.g., RHOF00ISL)

    Returns:
        Filename of most recent log, or empty string if none found
    """
    import glob
    import os

    if not os.path.exists(station_dir):
        return ""

    # Pattern: rhof00isl_20240827.log (lowercase station + date)
    pattern = os.path.join(station_dir, f"{station_id.lower()}_????????.log")
    log_files = glob.glob(pattern)

    if not log_files:
        return ""

    # Sort by date (filename contains date) and return most recent
    log_files.sort()
    most_recent = os.path.basename(log_files[-1])
    return most_recent


def detect_modified_sections(current_content: str, previous_log_path: str) -> str:
    """
    Compare current site log content with previous log to detect modified sections.

    Args:
        current_content: Current site log content
        previous_log_path: Path to previous site log file

    Returns:
        Comma-separated list of modified sections (e.g., "1,3.2,4.2")
    """
    import os
    import re

    if not os.path.exists(previous_log_path):
        return ""  # No previous log to compare with

    try:
        with open(previous_log_path, "r", encoding="utf-8") as f:
            previous_content = f.read()
    except Exception:
        return ""  # Error reading previous file

    # Find all section headers in both files
    section_pattern = r"^(\d+(?:\.\d+)*)\s+.*$"

    current_sections = {}
    previous_sections = {}

    # Extract sections from current content
    for line in current_content.split("\n"):
        match = re.match(section_pattern, line.strip())
        if match:
            section_num = match.group(1)
            # Find section content (until next section or end)
            start_idx = current_content.find(line)
            # Simple approach: get next 500 chars as section content
            section_content = current_content[start_idx : start_idx + 500]
            current_sections[section_num] = section_content

    # Extract sections from previous content
    for line in previous_content.split("\n"):
        match = re.match(section_pattern, line.strip())
        if match:
            section_num = match.group(1)
            start_idx = previous_content.find(line)
            section_content = previous_content[start_idx : start_idx + 500]
            previous_sections[section_num] = section_content

    # Compare sections
    modified = []
    for section_num, content in current_sections.items():
        prev_content = previous_sections.get(section_num, "")
        if content != prev_content:
            modified.append(section_num)

    return (
        ",".join(modified) if modified else "1"
    )  # Default to "1" if no specific changes detected


def has_meaningful_changes(current_content: str, previous_log_path: str) -> bool:
    """
    Check if there are meaningful changes between current and previous site log.

    This function performs a more thorough comparison than detect_modified_sections(),
    specifically checking for substantive content changes while ignoring:
    - Date prepared changes
    - Minor formatting differences
    - Whitespace variations

    Args:
        current_content: Current site log content
        previous_log_path: Path to previous site log file

    Returns:
        True if meaningful changes detected, False if files are essentially identical
    """
    import os
    import re

    if not os.path.exists(previous_log_path):
        return True  # No previous log, so this is a new file

    try:
        with open(previous_log_path, "r", encoding="utf-8") as f:
            previous_content = f.read()
    except Exception:
        return True  # Error reading previous file, assume changes

    # Normalize both files for comparison by removing/standardizing elements that
    # commonly change but don't represent meaningful site log updates
    def normalize_content(content: str) -> str:
        """Normalize content for comparison by removing date prepared and minor formatting."""
        # Remove "Date Prepared" line which changes every run
        content = re.sub(
            r"^\s*Date Prepared\s*:\s*.*$",
            "Date Prepared            : [NORMALIZED]",
            content,
            flags=re.MULTILINE,
        )

        # Normalize whitespace (but preserve structure)
        content = re.sub(r"\s+", " ", content)
        content = re.sub(r"\s*\n\s*", "\n", content)

        return content.strip()

    current_normalized = normalize_content(current_content)
    previous_normalized = normalize_content(previous_content)

    # Compare normalized content
    return current_normalized != previous_normalized


def _configure_logging(args):
    """Configure the logging system based on command line arguments."""
    # Determine console log level
    console_level = (
        args.log_level.value if hasattr(args.log_level, "value") else args.log_level
    )

    # For manual QC workflow: default to minimal console logging
    # unless explicitly requested by user
    if hasattr(args, "subcommand") and args.subcommand in [
        "PrintTOS",
        "rinex",
        "sitelog",
    ]:
        # Manual QC commands: Keep console clean by default
        # Only force ERROR level if no explicit log level was provided
        log_level_explicitly_set = any(
            arg in sys.argv for arg in ["--log-level", "--debug-all"]
        )
        if (
            console_level == logging.INFO
            and not args.debug_all
            and not log_level_explicitly_set
        ):
            console_level = (
                logging.ERROR
            )  # Only show errors for clean output (warnings/errors can be enabled explicitly)

    # Smart console level: debug-all enables DEBUG for files but keeps console cleaner
    if args.debug_all and args.log_dir:
        # When file logging is available, keep console at INFO level for readability
        # but enable DEBUG for files
        file_level = logging.DEBUG
        if console_level == logging.ERROR:  # From manual QC logic above
            console_level = (
                logging.INFO
            )  # Show some progress info when debug-all is requested
    elif args.debug_all:
        # No file logging, so show DEBUG on console
        console_level = logging.DEBUG
        file_level = logging.DEBUG
    else:
        file_level = logging.DEBUG if not args.production_logging else logging.INFO

    if args.log_dir:
        # File logging enabled
        if args.production_logging:
            configure_logging(
                LoggingConfig(
                    console_level=console_level,
                    file_level=logging.INFO,
                    log_dir=args.log_dir,
                    console_format=args.log_format,
                    file_format="json",
                    structured_file=True,
                    separate_levels=True,
                ),
                force_reconfigure=True,
            )
        else:
            # Development logging - keep console clean but files comprehensive
            configure_logging(
                LoggingConfig(
                    console_level=console_level,  # Respect user's level choice
                    file_level=file_level,  # Use DEBUG for files when --debug-all
                    log_dir=args.log_dir,
                    console_format=args.log_format,
                    file_format="human",
                    structured_file=True,
                    separate_levels=True,
                ),
                force_reconfigure=True,
            )
    else:
        # Console only logging
        setup_console_logging(console_level)

        # Force update all existing loggers to respect the new level
        # This is needed because legacy loggers don't propagate and have their own handlers
        root_logger = logging.getLogger()
        root_logger.setLevel(console_level)
        for logger_name in logging.Logger.manager.loggerDict:
            logger = logging.getLogger(logger_name)
            # Force level update for all loggers, including non-propagating ones
            logger.setLevel(console_level)
            # Also update their handlers
            for handler in logger.handlers:
                handler.setLevel(console_level)
        # Update root logger handlers too
        for handler in root_logger.handlers:
            handler.setLevel(console_level)


def main():
    """
    quering metadata from tos and comparing to relevant rinex files
    """

    # print(module_logger.getEffectiveLevel())

    parser = argparse.ArgumentParser(
        description="GPS metadata quality control and RINEX processing toolkit",
        epilog="""
QUICK START:
  # View station metadata
  tosGPS PrintTOS RHOF --format table

  # Validate RINEX files
  tosGPS rinex RHOF data/RHOF*.rnx

  # Generate site log
  tosGPS sitelog RHOF --output RHOF.log

  # Sync metadata from reference servers
  tosGPS sync-meta --type gamit-station-info RHOF

LOGGING CONTROL:
  --log-level ERROR    # Clean output (recommended for scripting)
  --log-dir logs/      # Enable comprehensive file logging
  --debug-all          # Detailed debug info (to files when --log-dir used)

OUTPUT STREAMS:
  stdout: Program data (tables, validation results, site logs)
  stderr: Status messages, progress, errors (use 2>/dev/null to hide)

For detailed examples, use: tosGPS COMMAND --help

Contact: Benni (bgo@vedur.is) or Hildur (hildur@vedur.is)
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    add_log_level_argument(parser)

    # Logging options
    logging_options = parser.add_argument_group(title="Logging options")
    logging_options.add_argument(
        "--log-dir", type=str, help="Directory for log files (enables file logging)"
    )
    logging_options.add_argument(
        "--log-format",
        choices=["human", "json"],
        default="human",
        help="Log format (human-readable or structured JSON)",
    )
    logging_options.add_argument(
        "--production-logging",
        action="store_true",
        help="Use production logging configuration (less verbose)",
    )
    logging_options.add_argument(
        "--debug-all", action="store_true", help="Enable debug logging for all modules"
    )

    # Data quality reporting options
    quality_options = parser.add_argument_group(title="Data Quality Reporting")
    quality_options.add_argument(
        "--report-issues",
        type=str,
        help="Save TOS data quality issues to JSON file (e.g., data_issues.json)",
    )
    quality_options.add_argument(
        "--quality-report",
        type=str,
        help="Save human-readable data quality report to file (e.g., tos_quality_report.txt)",
    )

    # server options
    server_options = parser.add_argument_group(title="Server options")
    server_options.add_argument(
        "--protocol", type=str, default="https", help="Transfer protocol"
    )
    server_options.add_argument(
        "-s", "--server", type=str, default="vi-api.vedur.is", help="Host:"
    )
    server_options.add_argument("-p", "--port", type=int, default=443, help="Port:")
    server_options.add_argument(
        "-r", "--rest", type=str, default="/tos/v1", help="Top levels REST path"
    )
    server_options.add_argument(
        "-t", "--timeout", type=int, default=4, help="Connection timeout:"
    )

    synthesis_options = parser.add_argument_group(title="Synthesis options")
    synthesis_options.add_argument(
        "--use-legacy-synthesis",
        action="store_true",
        help=(
            "Fall back to the legacy gps_metadata_qc.gps_metadata "
            "synthesis chain for device_history. The default since "
            "phase 5 is the new devices.station_sessions composer "
            "chain, which fixes the two legacy bugs (pair-based slicer "
            "drop, position-wise pivot inversion) and emits well-paired "
            "equipment slots. The legacy path is kept for ops "
            "compatibility during the transition and side-by-side "
            "debugging; slated for removal once the new chain has run "
            "in production. See "
            "docs/architecture/synthesis-legacy-divergence.md."
        ),
    )

    # making subcommands
    subparsers = parser.add_subparsers(
        title="Subcommands",
        description="valid subcommands",
        dest="subcommand",
        required=True,
    )

    # For TOS print options
    print_options = subparsers.add_parser(
        "PrintTOS",
        help="Display GPS station metadata from TOS in various formats",
        epilog="""
Examples:
  # Rich visual output (default - best for manual QC)
  tosGPS PrintTOS RHOF --format rich

  # Clean table output (perfect for analysis)
  tosGPS --log-level ERROR PrintTOS RHOF --format table > station_data.csv

  # Show only device history
  tosGPS PrintTOS RHOF --show-history

  # Show only static data and contacts
  tosGPS PrintTOS RHOF --show-static --show-contacts

  # Detailed contact information
  tosGPS PrintTOS RHOF --contact

  # Multiple stations with status info
  tosGPS PrintTOS REYK HOFN RHOF --format table

  # GAMIT processing format
  tosGPS PrintTOS RHOF --format gamit > gamit_stations.dat

  # JSON output for scripting
  tosGPS PrintTOS RHOF --format json | jq .

  # Period filtering (show only sessions within date range)
  tosGPS PrintTOS RHOF --date-from 2010-01-01 --date-to 2020-12-31

  # Show equipment history from specific date onwards
  tosGPS PrintTOS RHOF --date-from 2012-08-28 --show-history

  # Silent operation (errors only)
  tosGPS --log-level ERROR PrintTOS RHOF 2>/dev/null
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    print_options.add_argument("stations", nargs="+", help="List of stations")
    print_options.add_argument(
        "-f",
        "--format",
        choices=["table", "rich", "json", "gamit"],
        default="rich",
        help="Output format: rich (enhanced tables), table (simple), json, gamit (processing)",
    )
    print_options.add_argument(
        "--raw", action="store_true", help="Include detailed raw metadata"
    )

    # Display control options
    display_group = print_options.add_argument_group("Display options")
    display_group.add_argument(
        "--show-static", action="store_true", help="Show only static station data"
    )
    display_group.add_argument(
        "--show-history", action="store_true", help="Show only device history"
    )
    display_group.add_argument(
        "--show-contacts", action="store_true", help="Show only contact summary"
    )
    display_group.add_argument(
        "--contact",
        action="store_true",
        help="Show detailed contact information in English and Icelandic",
    )

    # Period filtering options
    filter_group = print_options.add_argument_group("Period filtering")
    filter_group.add_argument(
        "--date-from",
        type=str,
        help="Filter sessions from this date (YYYY-MM-DD format, e.g., 2010-01-01)",
    )
    filter_group.add_argument(
        "--date-to",
        type=str,
        help="Filter sessions to this date (YYYY-MM-DD format, e.g., 2020-12-31)",
    )

    # Data quality reporting options for PrintTOS
    printtos_quality_options = print_options.add_argument_group(
        "Data Quality Reporting"
    )
    printtos_quality_options.add_argument(
        "--report-issues",
        type=str,
        help="Save TOS data quality issues to JSON file (e.g., data_issues.json)",
    )
    printtos_quality_options.add_argument(
        "--quality-report",
        type=str,
        help="Save human-readable data quality report to file (e.g., tos_quality_report.txt)",
    )

    # RINEX validation subcommand
    rinex_parser = subparsers.add_parser(
        "rinex",
        help="Validate RINEX files against TOS metadata and apply corrections",
        epilog="""
Examples:
  # Basic validation (results to stdout)
  tosGPS --log-level ERROR rinex RHOF data/RHOF*.rnx

  # Validate with detailed progress
  tosGPS rinex RHOF data/RHOF0790.02D

  # Apply corrections with backup
  tosGPS rinex RHOF data/RHOF0790.02D --fix --backup

  # Generate QC report
  tosGPS rinex RHOF data/*.rnx --report qc_report.txt

  # Silent validation for scripting
  tosGPS --log-level ERROR rinex RHOF file.rnx 2>/dev/null
  echo $?  # Check exit code: 0=success, 1=discrepancies

  # Batch processing
  for file in data/*.rnx; do
      tosGPS --log-level ERROR rinex RHOF "$file" || echo "Issue in $file"
  done
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    rinex_parser.add_argument(
        "stations", nargs="+", help="GPS stations to validate against"
    )
    rinex_parser.add_argument("rinex_files", nargs="+", help="RINEX files to validate")
    rinex_parser.add_argument(
        "--fix", action="store_true", help="Apply corrections to RINEX headers"
    )
    rinex_parser.add_argument(
        "--backup", action="store_true", help="Create backup files before fixing"
    )
    rinex_parser.add_argument(
        "--report", type=str, help="Generate detailed QC report to file"
    )

    # Data quality reporting options for rinex
    rinex_quality_options = rinex_parser.add_argument_group("Data Quality Reporting")
    rinex_quality_options.add_argument(
        "--report-issues",
        type=str,
        help="Save TOS data quality issues to JSON file (e.g., data_issues.json)",
    )
    rinex_quality_options.add_argument(
        "--quality-report",
        type=str,
        help="Save human-readable data quality report to file (e.g., tos_quality_report.txt)",
    )

    # Site log generation subcommand
    sitelog_parser = subparsers.add_parser(
        "sitelog",
        help="Generate IGS site log",
        epilog="""
Examples:
  # Standard IGS site log (default)
  tosGPS sitelog RHOF
  tosGPS sitelog RHOF | grep "Antenna"

  # Validate data completeness
  tosGPS sitelog RHOF --validate

  # JSON format for data processing
  tosGPS sitelog RHOF --format json | jq .

  # Save to specific file
  tosGPS sitelog RHOF --output RHOF_site.log
  tosGPS sitelog RHOF -o logs/stations/RHOF.txt

  # IGS-compliant filename (auto-generated: RHOF00ISL)
  tosGPS sitelog RHOF --auto-filename

  # Dated filenames with change detection (skips if no changes)
  tosGPS sitelog RHOF --auto-filename --date-in-name
  tosGPS sitelog RHOF --auto-filename --date-in-name --force-update  # Force creation

  # Validation with file output
  tosGPS sitelog RHOF --validate --output RHOF.log
  tosGPS sitelog RHOF --validate --auto-filename

  # Process multiple stations
  for station in REYK HOFN RHOF; do
      tosGPS sitelog $station --validate | process_sitelog.py $station
  done

  # Quality control workflow
  tosGPS sitelog RHOF --validate --format json > rhof_data.json
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sitelog_parser.add_argument("stations", nargs="+", help="List of stations")
    sitelog_parser.add_argument(
        "--output", "-o", type=str, help="Output file (default: stdout for piping)"
    )
    sitelog_parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate site log completeness and report issues",
    )
    sitelog_parser.add_argument(
        "--format",
        choices=["igs", "json"],
        default="igs",
        help="Output format: igs (standard site log) or json (structured data)",
    )
    sitelog_parser.add_argument(
        "--auto-filename",
        action="store_true",
        help="Generate IGS-compliant filename automatically (e.g., RHOF00ISL)",
    )
    sitelog_parser.add_argument(
        "--dir",
        default=".",
        help="Base directory for site log storage (default: current directory). Creates subdirectories per station.",
    )
    sitelog_parser.add_argument(
        "--date-in-name",
        action="store_true",
        help="Include creation date in filename and save to file (e.g., rhof00isl_20250825.log). Automatically skips file creation if no meaningful changes detected since the last site log. Implies --auto-filename behavior.",
    )
    sitelog_parser.add_argument(
        "--force-update",
        action="store_true",
        help="Force file creation even when no changes detected (overrides automatic change detection when using --date-in-name).",
    )
    sitelog_parser.add_argument(
        "--modified-sections",
        help="Manually specify modified sections (e.g., '1,3.2,4.2'). If not provided, auto-detected by comparing with previous log.",
    )
    sitelog_parser.add_argument(
        "--custom-date",
        help="Use custom date for filename (YYYYMMDD format, e.g., '20010719'). For testing historical equipment sessions.",
    )

    # Period filtering for sitelog
    sitelog_filter_group = sitelog_parser.add_argument_group("Period filtering")
    sitelog_filter_group.add_argument(
        "--date-from",
        type=str,
        help="Filter sessions from this date (YYYY-MM-DD format, e.g., 2010-01-01)",
    )
    sitelog_filter_group.add_argument(
        "--date-to",
        type=str,
        help="Filter sessions to this date (YYYY-MM-DD format, e.g., 2020-12-31)",
    )

    # Data quality reporting options for sitelog
    sitelog_quality_options = sitelog_parser.add_argument_group(
        "Data Quality Reporting"
    )
    sitelog_quality_options.add_argument(
        "--report-issues",
        type=str,
        help="Save TOS data quality issues to JSON file (e.g., data_issues.json)",
    )
    sitelog_quality_options.add_argument(
        "--quality-report",
        type=str,
        help="Save human-readable data quality report to file (e.g., tos_quality_report.txt)",
    )

    # Unified metadata synchronization subcommand
    sync_parser = subparsers.add_parser(
        "syncMeta",
        help="Synchronize metadata from reference servers (download, validate, compare)",
        epilog="""
Examples:
  # Check differences (default behavior)
  tosGPS syncMeta --type gamit-station-info RHOF

  # Update with confirmation prompt
  tosGPS syncMeta --type gamit-station-info RHOF --update

  # Batch update without detailed comparison
  tosGPS syncMeta --type gamit-station-info RHOF REYK HOFN --update --no-compare

  # Multi-type operations
  tosGPS syncMeta --type gamit-station-info,igs-logs RHOF

  # Discovery and status
  tosGPS syncMeta --list-types          # Show available metadata types
  tosGPS syncMeta --list-servers        # Show configured servers
  tosGPS syncMeta --status              # Show sync status of all types

  # Advanced options
  tosGPS syncMeta --type gamit-station-info --force-server okada RHOF    # Force specific server
  tosGPS syncMeta --type gamit-station-info RHOF --force-download  # Bypass cache
  tosGPS syncMeta --type all --all-stations                   # Check all TOS stations

  # Safe Update System (enhanced reliability)
  tosGPS syncMeta --type gamit-station-info RHOF --update --dry-run      # Test mode
  tosGPS syncMeta --type gamit-station-info RHOF --update                 # With prompts (default)
  tosGPS syncMeta --type gamit-station-info RHOF --update --non-interactive  # Skip prompts
  tosGPS syncMeta --type gamit-station-info --list-backups              # Show backups
  tosGPS syncMeta --type gamit-station-info --rollback 20250904_143022   # Restore backup
  tosGPS syncMeta --type gamit-station-info --verify-only               # Check integrity
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Station arguments (optional for discovery commands)
    sync_parser.add_argument(
        "stations",
        nargs="*",
        help="List of stations to sync (optional for --list-types, --status commands)",
    )

    # Core operation mode
    sync_parser.add_argument(
        "--update",
        action="store_true",
        help="Actually update local data (default: check differences without updating)",
    )

    # Metadata type selection
    type_group = sync_parser.add_argument_group("Metadata type selection")
    type_group.add_argument(
        "--type",
        type=str,
        help="Comma-separated list of metadata types (e.g., gamit-station-info,igs-logs) or 'all'",
    )
    type_group.add_argument(
        "--list-types",
        action="store_true",
        help="List available metadata types from configuration",
    )

    # Server options
    server_group = sync_parser.add_argument_group("Server options")
    server_group.add_argument(
        "--force-server",
        type=str,
        help="Force specific server (e.g., okada,sopac) - overrides configured priority",
    )
    server_group.add_argument(
        "--list-servers", action="store_true", help="List configured servers"
    )

    # Comparison and output options
    compare_group = sync_parser.add_argument_group("Comparison options")
    compare_compare_group = compare_group.add_mutually_exclusive_group()
    compare_compare_group.add_argument(
        "--compare",
        action="store_true",
        help="Show detailed comparison (default for single station)",
    )
    compare_compare_group.add_argument(
        "--no-compare",
        action="store_true",
        help="Skip detailed comparison (default for batch operations)",
    )

    # Station selection options
    station_group = sync_parser.add_argument_group("Station selection")
    station_group.add_argument(
        "--all-stations",
        action="store_true",
        help="Process all stations available in TOS",
    )

    # Advanced options
    advanced_group = sync_parser.add_argument_group("Advanced options")
    advanced_group.add_argument(
        "--force-download",
        action="store_true",
        help="Force re-download even if cache is valid",
    )
    advanced_group.add_argument(
        "--backup", action="store_true", help="Create backup before updating local data"
    )
    advanced_group.add_argument(
        "--status",
        action="store_true",
        help="Show sync status of all configured metadata types",
    )
    advanced_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Test mode - perform all operations except upload (safe update system)",
    )
    advanced_group.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip confirmation prompts before upload (safe update system)",
    )
    advanced_group.add_argument(
        "--rollback",
        type=str,
        metavar="BACKUP_ID",
        help="Rollback to specified backup ID (format: YYYYMMDD_HHMMSS)",
    )
    advanced_group.add_argument(
        "--list-backups",
        action="store_true",
        help="List available backups for rollback",
    )
    advanced_group.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify file integrity without making changes",
    )
    advanced_group.add_argument(
        "--production-mode",
        action="store_true",
        help="Production mode - minimize console output, use structured logging only",
    )

    args = parser.parse_args()
    stations = getattr(args, "stations", [])

    # Configure logging system
    _configure_logging(args)

    # Get main logger
    logger = get_logger(__name__)

    # Constructing the URL:
    url = "{}://{}:{}{}".format(args.protocol, args.server, args.port, args.rest)
    log_level = args.log_level

    # Suppress initial log message for certain commands for clean output
    if args.subcommand not in ["compare-reference", "syncMeta"]:
        logger.info(
            "tosGPS started",
            extra={
                "subcommand": args.subcommand,
                "stations": stations,
                "server_url": url,
                "log_level": (
                    log_level.name if hasattr(log_level, "name") else str(log_level)
                ),
            },
        )

    # Handle different subcommands
    if args.subcommand == "rinex":
        _handle_rinex_subcommand(args, stations, url, log_level)
    elif args.subcommand == "sitelog":
        _handle_sitelog_subcommand(args, stations, url, log_level)
    elif args.subcommand == "syncMeta":
        # For dry-run comparison mode, suppress logging like compare-reference did
        if not args.update and (
            args.compare or _determine_comparison_mode_simple(args, len(stations))
        ):
            logging.getLogger().setLevel(logging.CRITICAL)
            for logger_name in logging.Logger.manager.loggerDict:
                logging.getLogger(logger_name).setLevel(logging.CRITICAL)
        _handle_sync_meta_subcommand(args, stations, url, log_level, sync_parser)
    elif args.subcommand == "PrintTOS":
        _handle_print_subcommand(args, stations, url, log_level)
    else:
        # Default behavior - print station information
        _handle_print_subcommand(args, stations, url, log_level)

    # Generate data quality reports if requested
    if hasattr(args, "report_issues") and args.report_issues:
        data_quality_manager.save_issues_to_file(args.report_issues)
        print(f"✓ Data quality issues saved to {args.report_issues}", file=sys.stderr)

    if hasattr(args, "quality_report") and args.quality_report:
        report_content = data_quality_manager.generate_summary_report()
        with open(args.quality_report, "w") as f:
            f.write(report_content)
        print(f"✓ Data quality report saved to {args.quality_report}", file=sys.stderr)


def _filter_sessions_by_date(station_info, date_from=None, date_to=None):
    """
    Filter device sessions based on date range.

    Args:
        station_info: Station information dictionary containing device_history
        date_from: Start date string in YYYY-MM-DD format (optional)
        date_to: End date string in YYYY-MM-DD format (optional)

    Returns:
        Modified station_info with filtered device_history
    """
    if not date_from and not date_to:
        return station_info  # No filtering needed

    if not station_info or "device_history" not in station_info:
        return station_info  # No device history to filter

    import logging
    from datetime import datetime

    logger = logging.getLogger(__name__)

    # Parse filter dates
    filter_start = None
    filter_end = None

    try:
        if date_from:
            filter_start = datetime.strptime(date_from, "%Y-%m-%d")
        if date_to:
            filter_end = datetime.strptime(date_to, "%Y-%m-%d")
    except ValueError as e:
        logger.error(f"Invalid date format: {e}. Use YYYY-MM-DD format.")
        return station_info  # Return unfiltered data on error

    # Filter sessions
    original_sessions = station_info["device_history"]
    filtered_sessions = []

    for session in original_sessions:
        session_start = None
        session_end = None

        # Parse session dates
        try:
            if session.get("time_from"):
                session_start = datetime.strptime(
                    str(session["time_from"])[:10], "%Y-%m-%d"
                )

            if (
                session.get("time_to")
                and session["time_to"] != "Present"
                and session["time_to"]
            ):
                session_end = datetime.strptime(
                    str(session["time_to"])[:10], "%Y-%m-%d"
                )
            else:
                # Session is ongoing (Present) - use current date for filtering
                session_end = datetime.now()

        except (ValueError, TypeError) as e:
            logger.warning(f"Could not parse session dates: {e}")
            continue  # Skip sessions with invalid dates

        # Apply filtering logic
        include_session = True

        if filter_start and session_end and session_end < filter_start:
            include_session = False  # Session ends before filter start

        if filter_end and session_start and session_start > filter_end:
            include_session = False  # Session starts after filter end

        if include_session:
            filtered_sessions.append(session)

    # Update station info with filtered sessions
    station_info_filtered = station_info.copy()
    station_info_filtered["device_history"] = filtered_sessions

    # Log filtering results
    logger.info(
        f"Session filtering: {len(original_sessions)} → {len(filtered_sessions)} sessions "
        f"(from: {date_from}, to: {date_to})"
    )

    return station_info_filtered


def _handle_print_subcommand(args, stations, url, log_level):
    """Handle PrintTOS subcommand and default behavior."""
    stationInfo_list = []

    # Defining default behaviour
    pformat, raw = (
        (args.format, args.raw) if args.subcommand == "PrintTOS" else ("rich", False)
    )

    # Process show options - if any --show-* flag is used, show only those sections
    # If no --show-* flags are used, show everything (default behavior)
    show_static_flag = getattr(args, "show_static", False)
    show_history_flag = getattr(args, "show_history", False)
    show_contacts_flag = getattr(args, "show_contacts", False)
    detailed_contacts = getattr(args, "contact", False)

    # If any --show-* flag is specified, only show those sections
    any_show_flag = show_static_flag or show_history_flag or show_contacts_flag

    if any_show_flag:
        # Selective display mode - show only requested sections
        show_options = {
            "show_static": show_static_flag,
            "show_history": show_history_flag,
            "show_contacts": show_contacts_flag,
        }
    else:
        # Default mode - show everything unless --contact is used (which shows only contacts)
        if detailed_contacts:
            show_options = {
                "show_static": False,
                "show_history": False,
                "show_contacts": False,  # Will be handled by detailed_contacts
            }
        else:
            show_options = {
                "show_static": True,
                "show_history": True,
                "show_contacts": True,
            }

    for sta in stations:
        station_info = _select_synthesizer(args)(sta, url, loglevel=log_level.value)

        if not station_info:
            continue

        # Apply period filtering if specified
        station_info = _filter_sessions_by_date(
            station_info,
            getattr(args, "date_from", None),
            getattr(args, "date_to", None),
        )

        if pformat == "table":
            gpsf.print_station_history(
                station_info, raw_format=raw, loglevel=log_level.value
            )
        elif pformat == "rich":
            # Use new rich formatter with full flag support
            from .io.rich_formatters import print_stations_rich

            print_stations_rich(
                [station_info],
                show_static=show_options["show_static"],
                show_contacts=show_options["show_contacts"],
                show_history=show_options["show_history"],
                detailed_contacts=detailed_contacts,
            )
        elif pformat == "json":
            # Use JSON formatter
            from .io.formatters import json_print

            print(json_print(station_info))
        elif pformat == "gamit":
            stationInfo_list += gpsf.print_station_info(
                station_info, loglevel=log_level.value
            )

    # Handle gamit format output (accumulated at the end)
    if pformat == "gamit":
        # Print GAMIT header
        header = "*SITE  Station Name      Session Start      Session Stop       Ant Ht   HtCod  Ant N    Ant E    Receiver Type         Vers                  SwVer  Receiver SN           Antenna Type     Dome   Antenna SN"
        print(header)
        stationInfo_list.sort()
        for infoline in stationInfo_list:
            print(infoline)


def _handle_rinex_subcommand(args, stations, url, log_level):
    """Handle RINEX validation and correction subcommand."""

    print(f"RINEX QC for stations: {', '.join(stations)}", file=sys.stderr)

    # Initialize TOS client
    TOSClient(base_url=url)  # Use default, respect centralized logging

    all_comparisons = []

    for station in stations:
        print(f"\n=== Processing station {station} ===", file=sys.stderr)

        # Get station metadata using legacy system (more reliable for validation)
        try:
            station_data = _select_synthesizer(args)(
                station, url, loglevel=log_level.value
            )
            if not station_data:
                print(
                    f"Error: Could not retrieve metadata for station {station}",
                    file=sys.stderr,
                )
                continue

            # Extract device sessions for validation (use most recent)
            device_sessions = station_data.get("device_history", [])
            if not device_sessions:
                print(
                    f"Warning: No device history found for station {station}",
                    file=sys.stderr,
                )
                continue

            # Use the most recent session for validation
            device_sessions[-1]

        except Exception as e:
            print(f"Error retrieving station data: {e}", file=sys.stderr)
            continue

        # Validate each RINEX file
        for rinex_file in args.rinex_files:
            rinex_path = Path(rinex_file)
            if not rinex_path.exists():
                print(f"Warning: RINEX file {rinex_file} not found", file=sys.stderr)
                continue

            print(f"\nValidating RINEX file: {rinex_file}", file=sys.stderr)

            # Read RINEX header
            header_data = read_rinex_header(rinex_path, log_level.value)
            if not header_data:
                print(f"Error reading RINEX header from {rinex_file}", file=sys.stderr)
                continue

            # Extract header information
            rinex_info = extract_header_info(header_data, log_level.value)

            # Compare with TOS metadata
            comparison = compare_rinex_to_tos(rinex_info, station_data, log_level.value)
            all_comparisons.append(
                {"station": station, "file": rinex_file, "comparison": comparison}
            )

            # Report discrepancies
            if comparison.get("discrepancies"):
                print(
                    f"Found {len(comparison['discrepancies'])} discrepancies:",
                    file=sys.stderr,
                )
                for field, diff in comparison["discrepancies"].items():
                    print(
                        f"  {field}: RINEX='{diff.get('rinex', '')}' vs TOS='{diff.get('tos', '')}'",
                        file=sys.stderr,
                    )
            else:
                print("✓ No discrepancies found")

            # Apply fixes if requested
            if args.fix and comparison.get("corrections"):
                print(
                    f"Applying {len(comparison['corrections'])} corrections...",
                    file=sys.stderr,
                )
                success = update_rinex_files(
                    [rinex_path],
                    [comparison["corrections"]],
                    backup=args.backup,
                    loglevel=log_level.value,
                )
                if success.get(str(rinex_path)):
                    print("✓ Corrections applied successfully", file=sys.stderr)
                else:
                    print("✗ Failed to apply corrections", file=sys.stderr)

    # Generate report if requested
    if args.report and all_comparisons:
        report_content = "GPS RINEX QC REPORT\n" + "=" * 50 + "\n\n"
        for item in all_comparisons:
            report_content += f"Station: {item['station']}\n"
            report_content += f"File: {item['file']}\n"
            comp = item["comparison"]
            report_content += f"Discrepancies: {len(comp.get('discrepancies', {}))}\n"
            report_content += f"Corrections: {len(comp.get('corrections', {}))}\n\n"

        try:
            with open(args.report, "w") as f:
                f.write(report_content)
            print(f"\n✓ QC report saved to {args.report}", file=sys.stderr)
        except Exception as e:
            print(f"Error writing report: {e}", file=sys.stderr)


def _handle_sitelog_subcommand(args, stations, url, log_level):
    """Handle site log generation subcommand with validation and format options."""
    # Initialize TOS client
    tos_client = TOSClient(base_url=url)  # Use default, respect centralized logging

    for station in stations:
        # Send status messages to stderr (but only when saving to file or multiple stations)
        if args.output or len(stations) > 1:
            print(f"Generating site log for station {station}", file=sys.stderr)

        try:
            # Get complete station metadata with proper device sessions.
            # The default path (phase 5) routes through the
            # devices.station_sessions composer chain for consistency
            # with PrintTOS and rinex. Pass --use-legacy-synthesis to
            # fall back to TOSClient.get_complete_station_metadata,
            # which carries its own custom history-from-connections
            # logic (a third synthesis path that predates the
            # composer chain).
            if getattr(args, "use_legacy_synthesis", False):
                complete_station_data = tos_client.get_complete_station_metadata(
                    station
                )
            else:
                complete_station_data = gpsqc.gps_metadata_via_devices(
                    station, url, loglevel=log_level.value
                )
            if not complete_station_data:
                print(
                    f"Error: Could not retrieve metadata for station {station}",
                    file=sys.stderr,
                )
                continue

            # Extract device sessions from complete metadata
            device_sessions = complete_station_data.get("device_history", [])

            # Apply period filtering if specified
            complete_station_data = _filter_sessions_by_date(
                complete_station_data,
                getattr(args, "date_from", None),
                getattr(args, "date_to", None),
            )
            device_sessions = complete_station_data.get("device_history", [])

            # Validation if requested (basic validation for now)
            if args.validate:
                # Simple validation - check if we got station data
                if complete_station_data and device_sessions:
                    required_fields = ["marker", "name", "lat", "lon", "altitude"]
                    missing = [
                        f for f in required_fields if not complete_station_data.get(f)
                    ]

                    if missing:
                        print(
                            f"⚠️  Station {station}: Missing required fields: {', '.join(missing)}",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"✅ Station {station}: Basic required data present ({len(device_sessions)} device sessions)",
                            file=sys.stderr,
                        )
                else:
                    print(
                        f"❌ Station {station}: No station data or device sessions found",
                        file=sys.stderr,
                    )

            # Generate output based on format
            if args.format == "json":
                # JSON format - structured site log data
                from .io.formatters import json_print

                site_log_data = {
                    "station": station,
                    "metadata": complete_station_data,
                    "device_sessions": device_sessions,
                    "generated_date": datetime.now().isoformat(),
                    "format": "site_log_json_v1",
                }
                output_content = json_print(site_log_data)
            else:
                # IGS standard format (default) - use comprehensive legacy function
                # Prepare dynamic values for auto-filename mode
                if args.auto_filename:
                    station_id = f"{station.upper()}00ISL"
                    station_dir = os.path.join(args.dir, station_id)
                    previous_log = find_previous_sitelog(station_dir, station_id)

                    report_type = "NEW" if not previous_log else "UPDATE"
                    modified_sections = (
                        args.modified_sections if args.modified_sections else "1"
                    )

                    output_content = gpsf.site_log(
                        station,
                        loglevel=log_level.value,
                        report_type=report_type,
                        previous_log=previous_log,
                        modified_sections=modified_sections,
                    )
                else:
                    output_content = gpsf.site_log(station, loglevel=log_level.value)

            # Output handling - file, auto-filename, or stdout
            output_file = None

            if args.output:
                # Use specified output file
                output_file = args.output
            elif args.auto_filename or args.date_in_name:
                # --date-in-name implies auto-filename behavior
                if args.date_in_name:
                    # Special directory structure for --date-in-name: base_dir/sitelog/STATION/file
                    # Strip 'sitelog' from end of --dir if present to avoid duplication
                    base_path = args.dir.rstrip("/")
                    if base_path.endswith("sitelog"):
                        sitelog_base = base_path
                    else:
                        sitelog_base = os.path.join(base_path, "sitelog")
                    full_path, filename = generate_igs_sitelog_filename(
                        station,
                        include_date=True,
                        base_dir=sitelog_base,
                        custom_date=args.custom_date,
                        create_station_subdir=True,  # Create STATION subdir under sitelog/
                    )
                else:
                    # Regular --auto-filename behavior
                    full_path, filename = generate_igs_sitelog_filename(
                        station,
                        include_date=False,
                        base_dir=args.dir,
                        custom_date=args.custom_date,
                        create_station_subdir=False,
                    )

                # Ensure output directory exists
                output_dir = (
                    os.path.dirname(full_path) if os.path.dirname(full_path) else "."
                )
                os.makedirs(output_dir, exist_ok=True)

                # Determine previous log and report type
                station_id = f"{station.upper()}00ISL"
                station_dir = os.path.dirname(full_path)
                previous_log = find_previous_sitelog(station_dir, station_id)

                # Auto-detect modified sections if not manually specified
                modified_sections = args.modified_sections
                if not modified_sections and previous_log:
                    previous_log_path = os.path.join(station_dir, previous_log)
                    modified_sections = detect_modified_sections(
                        output_content, previous_log_path
                    )

                output_file = full_path
                # Quieter output for --date-in-name (automated workflows)
                if args.date_in_name:
                    print(f"Using IGS filename: {filename}", file=sys.stderr)
                elif args.auto_filename:
                    # Verbose output for manual workflows
                    print(f"Using IGS filename: {filename}", file=sys.stderr)
                    if previous_log:
                        print(f"Previous log: {previous_log}", file=sys.stderr)
                        print("Report type: UPDATE", file=sys.stderr)
                        if modified_sections:
                            print(
                                f"Modified sections: {modified_sections}",
                                file=sys.stderr,
                            )
                    else:
                        print("Report type: NEW", file=sys.stderr)

            if output_file:
                # Change detection for --date-in-name: skip file creation if no changes
                skip_unchanged = False
                if (
                    args.date_in_name
                    and previous_log
                    and not getattr(args, "force_update", False)
                ):
                    previous_log_path = os.path.join(station_dir, previous_log)
                    if not has_meaningful_changes(output_content, previous_log_path):
                        skip_unchanged = True
                        print(
                            f"⏭️  No changes detected for {station} since {previous_log}, skipping file creation",
                            file=sys.stderr,
                        )
                        print(
                            "    Use --force-update to create file anyway",
                            file=sys.stderr,
                        )

                if not skip_unchanged:
                    # Write to file
                    try:
                        with open(output_file, "w", encoding="utf-8") as f:
                            f.write(output_content)
                        # Send success message to stderr to keep stdout clean
                        print(f"✓ Site log saved to {output_file}", file=sys.stderr)
                    except Exception as e:
                        print(f"Error writing site log: {e}", file=sys.stderr)
                        # Still output to stdout if file write failed
                        print(output_content)
            else:
                # No file output specified - output to stdout for piping
                print(output_content)  # Clean output to stdout
                # Optional: Send completion notice to stderr (only for multiple stations)
                if len(stations) > 1:
                    print(f"✓ Site log for {station} completed", file=sys.stderr)

        except Exception as e:
            print(f"Error generating site log for {station}: {e}", file=sys.stderr)
            if log_level.value <= logging.DEBUG:
                import traceback as _tb

                _tb.print_exc(file=sys.stderr)


def _get_station_config_dir():
    """Get the station configuration data directory relative to the project root."""
    # Get the directory where this file is located (src/tostools/)
    current_dir = Path(__file__).parent
    # Go up to project root (../../ from src/tostools/)
    project_root = current_dir.parent.parent
    return project_root / "data" / "station_config"


def _fetch_station_info(station_config_dir, logger):
    """Fetch station info file from okada server."""
    config = REFERENCE_DATA_CONFIG["station-info"]
    local_path = station_config_dir / config["local_filename"]

    # Show existing file info if it exists
    if local_path.exists():
        mod_time = datetime.fromtimestamp(local_path.stat().st_mtime)
        print(f"Existing file: {mod_time.strftime('%Y-%m-%d %H:%M')}", file=sys.stderr)

    # Ensure local directory exists
    local_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Fetching {config['description']}...", file=sys.stderr)
    print(f"Remote: {config['remote_host']}:{config['remote_path']}", file=sys.stderr)
    print(f"Local: {local_path}", file=sys.stderr)

    try:
        # Use scp to fetch the file
        cmd = [
            "scp",
            f"{config['remote_host']}:{config['remote_path']}",
            str(local_path),
        ]

        logger.info(f"Executing: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,  # 1 minute timeout
        )

        if result.returncode == 0:
            # Verify file was downloaded
            if local_path.exists():
                size_mb = local_path.stat().st_size / (1024 * 1024)
                print(
                    f"✓ Successfully downloaded station info file ({size_mb:.1f} MB)",
                    file=sys.stderr,
                )
                logger.info(
                    f"Station info file downloaded: {local_path} ({size_mb:.1f} MB)"
                )
            else:
                print(
                    "✗ Download appeared successful but file not found", file=sys.stderr
                )
                logger.error("SCP completed but file not found locally")
        else:
            print(f"✗ Download failed: {result.stderr.strip()}", file=sys.stderr)
            logger.error(
                f"SCP failed with return code {result.returncode}: {result.stderr}"
            )

    except subprocess.TimeoutExpired:
        print("✗ Download timeout (60s)", file=sys.stderr)
        logger.error("SCP timeout after 60 seconds")
    except Exception as e:
        print(f"✗ Download error: {e}", file=sys.stderr)
        logger.error(f"SCP execution failed: {e}")


def _handle_fetch_reference_subcommand(args, log_level):
    """
    Handle reference data fetching subcommand.

    Downloads reference data files from remote servers to the local data/station_config directory.
    Supports status checking and automatic re-downloading.

    Args:
        args: Parsed command line arguments
        log_level: Logging level for output control
    """
    logger = get_logger(__name__)
    station_config_dir = _get_station_config_dir()

    if args.data_type == "station-info":
        _fetch_station_info(station_config_dir, logger)


def _fetch_station_info(station_config_dir, logger):
    """Fetch station info file from okada server."""
    config = REFERENCE_DATA_CONFIG["station-info"]
    local_path = station_config_dir / config["local_filename"]

    # Show existing file info if it exists
    if local_path.exists():
        mod_time = datetime.fromtimestamp(local_path.stat().st_mtime)
        print(f"Existing file: {mod_time.strftime('%Y-%m-%d %H:%M')}", file=sys.stderr)

    # Ensure local directory exists
    local_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Fetching {config['description']}...", file=sys.stderr)
    print(f"Remote: {config['remote_host']}:{config['remote_path']}", file=sys.stderr)
    print(f"Local: {local_path}", file=sys.stderr)

    try:
        # Use scp to fetch the file
        cmd = [
            "scp",
            f"{config['remote_host']}:{config['remote_path']}",
            str(local_path),
        ]

        logger.info(f"Executing: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,  # 1 minute timeout
        )

        if result.returncode == 0:
            # Verify file was downloaded
            if local_path.exists():
                size_mb = local_path.stat().st_size / (1024 * 1024)
                print(
                    f"✓ Successfully downloaded station info file ({size_mb:.1f} MB)",
                    file=sys.stderr,
                )
                logger.info(
                    f"Station info file downloaded: {local_path} ({size_mb:.1f} MB)"
                )
            else:
                print(
                    "✗ Download appeared successful but file not found", file=sys.stderr
                )
                logger.error("SCP completed but file not found locally")
        else:
            print(f"✗ Download failed: {result.stderr.strip()}", file=sys.stderr)
            logger.error(
                f"SCP failed with return code {result.returncode}: {result.stderr}"
            )

    except subprocess.TimeoutExpired:
        print("✗ Download timeout (60s)", file=sys.stderr)
        logger.error("SCP timeout after 60 seconds")
    except Exception as e:
        print(f"✗ Download error: {e}", file=sys.stderr)
        logger.error(f"SCP execution failed: {e}")


def _parse_station_info_file():
    """Parse the SOPAC station.info file and return station data dictionary."""
    station_config_dir = _get_station_config_dir()
    station_info_path = station_config_dir / "station.info.sopac.apr05"

    if not station_info_path.exists():
        print(
            f"ERROR: Station info file not found at: {station_info_path}",
            file=sys.stderr,
        )
        print(
            "Expected directory structure: <project_root>/data/station_config/",
            file=sys.stderr,
        )
        print(
            "To fetch the file, run: tosGPS syncMeta --type gamit-station-info RHOF",
            file=sys.stderr,
        )
        return None

    stations_data = {}

    try:
        with open(station_info_path, "r", encoding="utf-8", errors="ignore") as f:
            for line_num, line in enumerate(f, 1):
                line = line.rstrip("\n\r")

                # Skip comments, headers, and empty lines
                if not line or line.startswith(("*", "#")) or len(line) < 50:
                    continue

                # Extract station code from fixed positions (columns 1-4)
                if len(line) >= 4:
                    station_code = line[1:5].strip()

                    if station_code and not station_code.isspace():
                        if station_code not in stations_data:
                            stations_data[station_code] = []

                        stations_data[station_code].append(line)

    except Exception:
        return None

    return stations_data


def _print_line_with_highlights(line, other_line, column_info, is_tos=True):
    """Print a line with differences highlighted using ANSI colors."""
    # ANSI color codes
    BLUE = "\033[94m"  # Bright blue text for differences
    RESET = "\033[0m"  # Reset to normal

    result = ""
    last_pos = 0

    for col_name, start, end in column_info:
        col_start = start - 1  # Convert to 0-based indexing
        min(end, len(line))

        # Get the column values (stripped for comparison)
        # Handle -1 as "to end of line"
        if end == -1:
            my_val = line[col_start:].strip() if len(line) > col_start else ""
            other_val = (
                other_line[col_start:].strip() if len(other_line) > col_start else ""
            )
        else:
            my_val = (
                line[col_start:end].strip()
                if len(line) >= end
                else line[col_start:].strip() if len(line) > col_start else ""
            )
            other_val = (
                other_line[col_start:end].strip()
                if len(other_line) >= end
                else (
                    other_line[col_start:].strip()
                    if len(other_line) > col_start
                    else ""
                )
            )

        # Add content before this column
        if last_pos < col_start:
            result += line[last_pos:col_start]

        # Add this column's content (highlight if different)
        if col_start < len(line):
            # Handle -1 as "to end of line"
            if end == -1:
                column_content = line[col_start:]
                col_actual_end = len(line)
            else:
                column_content = line[col_start : min(end, len(line))]
                col_actual_end = min(end, len(line))

            if my_val != other_val and my_val:  # Highlight if different and not empty
                result += BLUE + column_content + RESET
            else:
                result += column_content

            last_pos = col_actual_end
        else:
            last_pos = col_start if end == -1 else min(end, len(line))

        if last_pos >= len(line):
            break

    # Add any remaining content after the last column
    if last_pos < len(line):
        result += line[last_pos:]

    print(result)


def _analyze_line_differences(tos_lines, ref_lines):
    """Analyze differences between TOS and reference lines with column-level detail."""
    # Define the column positions and names for station.info format
    # Fixed based on actual data analysis
    column_info = [
        ("Station", 1, 6),  # " ELDC  "
        ("Station Name", 7, 24),  # "Eldvorp           "
        ("Session Start", 25, 43),  # "2020 029 00 00 00"
        ("Session Stop", 44, 62),  # "2021 050 00 00 00"
        ("Height", 65, 73),  # "0.0000  "
        ("HtCod", 74, 80),  # "DHARP   "
        ("Ant N", 81, 88),  # "0.0000   "
        ("Ant E", 89, 96),  # "0.0000  "
        ("Receiver Type", 97, 116),  # "SEPT POLARX5          "
        ("Receiver FW", 117, 136),  # "5.3.0                  "
        ("SwVer", 137, 143),  # "5.30  "
        ("Receiver SN", 144, 161),  # "3012366               "
        ("Antenna Type", 162, 179),  # "SEPCHOKE_B3E6    "
        ("Dome", 187, 191),  # "SPKE" (4-character radome code) - 1-based: 186+1=187
        (
            "Antenna SN",
            194,
            -1,
        ),  # "antenna-eldc-2020012" vs "0000" (to end of line) - 1-based: 193+1=194
    ]

    # Find best matches between lines (by station code and rough timing)
    for tos_line in tos_lines:
        best_match = None
        best_score = 0

        # Look for similar lines in reference data
        for ref_line in ref_lines:
            if len(tos_line) >= 25 and len(ref_line) >= 25:
                # Match by station code and station name
                tos_station = tos_line[1:5].strip()
                tos_name = tos_line[7:25].strip()
                ref_station = ref_line[1:5].strip()
                ref_name = ref_line[7:25].strip()

                if tos_station == ref_station and tos_name == ref_name:
                    # Calculate similarity score based on session timing
                    tos_start = tos_line[26:45].strip() if len(tos_line) > 45 else ""
                    ref_start = ref_line[26:45].strip() if len(ref_line) > 45 else ""

                    if tos_start == ref_start:
                        score = 100  # Perfect timing match
                    elif tos_start[:9] == ref_start[:9]:  # Same start year/day
                        score = 80
                    else:
                        score = 50  # Same station/name, different timing

                    if score > best_score:
                        best_score = score
                        best_match = ref_line

        if best_match and best_score >= 50:
            # Compare columns
            differences = []
            for col_name, start, end in column_info:
                # Handle -1 as "to end of line"
                if end == -1:
                    tos_val = (
                        tos_line[start - 1 :].strip() if len(tos_line) >= start else ""
                    )
                    ref_val = (
                        best_match[start - 1 :].strip()
                        if len(best_match) >= start
                        else ""
                    )
                else:
                    tos_val = (
                        tos_line[start - 1 : end].strip()
                        if len(tos_line) >= end
                        else ""
                    )
                    ref_val = (
                        best_match[start - 1 : end].strip()
                        if len(best_match) >= end
                        else ""
                    )

                if tos_val != ref_val:
                    differences.append(f"{col_name}: '{tos_val}' vs '{ref_val}'")

            if differences:
                session = tos_line[26:45].strip()
                print(f"\n📅 Session: {session}")
                print("─" * 80)

                # Show the lines with colored text highlighting
                print("TOS: ", end="")
                _print_line_with_highlights(
                    tos_line, best_match, column_info, is_tos=True
                )
                print("STA: ", end="")
                _print_line_with_highlights(
                    best_match, tos_line, column_info, is_tos=False
                )
        else:
            # No good match found - this is a completely new/missing line
            session = tos_line[26:45].strip() if len(tos_line) > 45 else "unknown"
            print(f"\n➕ New in TOS: {session}")

    # Check for lines only in reference
    for ref_line in ref_lines:
        found_match = False
        for tos_line in tos_lines:
            if len(tos_line) >= 25 and len(ref_line) >= 25:
                tos_station = tos_line[1:5].strip()
                tos_name = tos_line[7:25].strip()
                ref_station = ref_line[1:5].strip()
                ref_name = ref_line[7:25].strip()

                if (
                    tos_station == ref_station
                    and tos_name == ref_name
                    and tos_line[26:45].strip() == ref_line[26:45].strip()
                ):
                    found_match = True
                    break

        if not found_match:
            session = ref_line[26:45].strip() if len(ref_line) > 45 else "unknown"
            print(f"\n➖ Missing from TOS: {session}")


def _handle_compare_reference_subcommand(args, stations, url, log_level):
    """Handle station comparison with reference station.info file."""
    # Load reference data
    reference_data = _parse_station_info_file()
    if not reference_data:
        print("Error: Could not load reference station.info file", file=sys.stderr)
        print("Run: tosGPS fetch-reference station-info", file=sys.stderr)
        return

    for station in stations:
        # Get TOS data in GAMIT format (always suppress logging for clean output)
        try:
            station_info = gpsqc.gps_metadata(station, url, loglevel=logging.CRITICAL)
            if not station_info:
                print(f"Error: No TOS data found for {station}", file=sys.stderr)
                continue

            tos_lines = gpsf.print_station_info(
                station_info, loglevel=logging.CRITICAL, skip_validation=True
            )

        except Exception as e:
            print(f"Error getting TOS data for {station}: {e}", file=sys.stderr)
            continue

        # Get reference data
        ref_lines = reference_data.get(station, [])
        if not ref_lines:
            print(f"Error: No reference data found for {station}", file=sys.stderr)
            continue

        # Compare the data with detailed column analysis
        tos_stripped = [line.strip() for line in tos_lines]
        ref_stripped = [line.strip() for line in ref_lines]

        # Check for exact matches first
        if set(tos_stripped) == set(ref_stripped):
            print(f"✓ {station}: No differences found")
            continue

        print(f"DIFFERENCES FOUND for {station}:")

        # Analyze differences line by line
        _analyze_line_differences(tos_stripped, ref_stripped)

        if len(stations) > 1:
            print()  # Add blank line between stations


def _handle_sync_meta_subcommand(args, stations, url, log_level, parser):
    """Handle unified metadata synchronization subcommand."""
    import sys

    # Handle discovery commands first (don't need stations)
    if args.list_types:
        _list_metadata_types()
        return

    if args.list_servers:
        _list_metadata_servers()
        return

    if args.status:
        _show_sync_status()
        return

    # Handle new safe update system commands
    if args.list_backups:
        _list_available_backups()
        return

    if args.rollback:
        _handle_rollback_command(args.rollback, args.type)
        return

    if args.verify_only:
        _handle_verify_only_command(args.type, stations)
        return

    # Validate required arguments for sync operations
    if not args.type and not args.status:
        parser.error(
            "the following arguments are required: --type (use --list-types to see available types)"
        )

    if not stations and not args.all_stations:
        parser.error("specify stations or use --all-stations")

    # Parse metadata types
    if args.type == "all":
        metadata_types = _get_all_metadata_types()
    else:
        metadata_types = [t.strip() for t in args.type.split(",")]

    # Validate metadata types
    available_types = _get_available_metadata_types()
    invalid_types = [t for t in metadata_types if t not in available_types]
    if invalid_types:
        print(
            f"Error: Unknown metadata types: {', '.join(invalid_types)}",
            file=sys.stderr,
        )
        print("Use --list-types to see available types", file=sys.stderr)
        return

    # Get station list
    if args.all_stations:
        stations = _discover_all_tos_stations(url, log_level)
        if not stations:
            print("Error: Could not discover TOS stations", file=sys.stderr)
            return

    # Determine comparison mode (smart defaults)
    show_comparison = _determine_comparison_mode(
        args, len(stations), len(metadata_types)
    )

    # Process each metadata type
    overall_success = True
    results = {}

    for metadata_type in metadata_types:
        print(f"\n=== Processing {metadata_type} ===", file=sys.stderr)

        # Use new safe update workflow if update mode is enabled and safe features are requested
        if args.update and (
            args.dry_run
            or not args.non_interactive
            or getattr(args, "use_safe_update", True)
        ):
            if not getattr(args, "production_mode", False):
                print(
                    f"🛡️  Using safe update workflow for {metadata_type}",
                    file=sys.stderr,
                )

            workflow_result = _safe_update_workflow(
                stations=stations,
                metadata_type=metadata_type,
                url=url,
                update_mode=args.update,
                dry_run=args.dry_run,
                interactive=not args.non_interactive,
                backup_required=args.backup or True,  # Default to backup for safety
                production_mode=getattr(args, "production_mode", False),
            )

            # Convert workflow result to expected format
            type_results = {
                "success": workflow_result["success"],
                "stations_processed": len(workflow_result["stations_processed"]),
                "stations_updated": len(workflow_result["stations_updated"]),
                "errors": workflow_result["errors"],
                "safe_update_workflow": True,
                "workflow_details": workflow_result,
            }
        else:
            # Use legacy workflow
            type_results = _process_metadata_type(
                metadata_type=metadata_type,
                stations=stations,
                url=url,
                log_level=log_level,
                update_mode=args.update,
                show_comparison=show_comparison,
                force_download=args.force_download,
                backup=args.backup,
                forced_server=args.force_server,
            )

        results[metadata_type] = type_results

        # Never abort - continue with other types even if this one fails
        if not type_results.get("success", False):
            overall_success = False

    # Generate summary report
    _generate_sync_summary(results, metadata_types, stations)

    # Exit with appropriate code for operational monitoring
    if not overall_success:
        import sys

        sys.exit(1)  # Partial failure


def _list_metadata_types():
    """List available metadata types from configuration."""
    try:
        config = _load_sync_config()
        types = config.get("metadata_sources", {}).get("types", {})

        if not types:
            print("No metadata types configured.", file=sys.stderr)
            print(
                "Create ~/.config/tostools/sync-config.yaml to configure metadata sources.",
                file=sys.stderr,
            )
            return

        print("Available metadata types:")
        for type_name, type_config in types.items():
            description = type_config.get("description", "No description")
            servers = type_config.get("primary_server", "unknown")
            fallbacks = type_config.get("fallback_servers", [])
            if fallbacks:
                servers += f", {', '.join(fallbacks)}"

            print(f"  {type_name:<20} {description} ({servers})")

    except Exception as e:
        print(f"Error loading metadata types: {e}", file=sys.stderr)


def _list_metadata_servers():
    """List configured metadata servers."""
    try:
        config = _load_sync_config()
        servers = config.get("metadata_sources", {}).get("servers", {})

        if not servers:
            print("No servers configured.", file=sys.stderr)
            return

        print("Configured servers:")
        for server_name, server_config in servers.items():
            host = server_config.get("host", "unknown")
            auth = server_config.get("auth_method", "unknown")
            print(f"  {server_name:<15} {auth:<8} ({host})")

    except Exception as e:
        print(f"Error loading server configuration: {e}", file=sys.stderr)


def _show_sync_status():
    """Show sync status of all configured metadata types."""
    try:
        cache_dir = _get_metadata_cache_dir()
        config = _load_sync_config()
        types = config.get("metadata_sources", {}).get("types", {})

        print("Metadata sync status:")

        for type_name in types:
            type_cache_dir = cache_dir / type_name
            metadata_file = type_cache_dir / "metadata.json"

            if metadata_file.exists():
                try:
                    with open(metadata_file, "r") as f:
                        metadata = json.load(f)

                    downloaded_at = datetime.fromisoformat(
                        metadata["downloaded_at"].replace("Z", "+00:00")
                    )
                    age = datetime.now() - downloaded_at.replace(tzinfo=None)

                    print(f"  {type_name:<20} Last sync: {age.days} days ago")
                except Exception:
                    print(f"  {type_name:<20} Cache corrupted")
            else:
                print(f"  {type_name:<20} Never synced")

    except Exception as e:
        print(f"Error checking sync status: {e}", file=sys.stderr)


def _load_sync_config():
    """Load sync configuration from YAML file."""
    config_path = Path.home() / ".config" / "tostools" / "sync-config.yaml"

    if not config_path.exists():
        # Return default configuration
        return {
            "metadata_sources": {
                "servers": {
                    "okada": {
                        "host": "gpsops@okada",
                        "auth_method": "ssh_key",
                        "base_path": "/D/DATABASE",
                    }
                },
                "types": {
                    "gamit-station-info": {
                        "description": "GAMIT station information files",
                        "primary_server": "okada",
                        "files": {"okada": "/GAMIT/station.info.sopac.apr05"},
                        "checksum_validation": True,
                        "update_frequency": "weekly",
                    }
                },
            }
        }

    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _get_metadata_cache_dir():
    """Get the metadata cache directory."""
    return Path.home() / ".local" / "share" / "tostools" / "cache"


def _get_available_metadata_types():
    """Get list of available metadata types."""
    config = _load_sync_config()
    return list(config.get("metadata_sources", {}).get("types", {}).keys())


def _get_all_metadata_types():
    """Get all available metadata types."""
    return _get_available_metadata_types()


def _determine_comparison_mode_simple(args, num_stations):
    """Simple version for early logging decision."""
    if args.compare:
        return True
    if args.no_compare:
        return False
    # Default: show comparison for single station
    return num_stations == 1


def _determine_comparison_mode(args, num_stations, num_types):
    """Determine whether to show detailed comparison based on arguments and context."""
    # Explicit flags override defaults
    if args.compare:
        return True
    if args.no_compare:
        return False

    # Smart defaults based on operation context
    if args.update:
        # Update mode: show comparison for single station operations
        return num_stations == 1 and num_types == 1
    else:
        # Dry-run mode: show comparison for single station operations
        return num_stations == 1 and num_types == 1


def _discover_all_tos_stations(url, log_level):
    """Discover all available stations in TOS."""
    # This is a placeholder - would need to implement TOS station discovery
    # For now, return a common set of Icelandic stations
    return ["RHOF", "REYK", "HOFN", "AKUR", "VOGS"]


def _process_metadata_type(
    metadata_type,
    stations,
    url,
    log_level,
    update_mode,
    show_comparison,
    force_download,
    backup,
    forced_server,
):
    """Process a single metadata type for all specified stations."""
    logger = get_logger(__name__)

    results = {
        "success": True,
        "stations_processed": 0,
        "stations_updated": 0,
        "stations_failed": 0,
        "errors": [],
    }

    try:
        # Load configuration for this metadata type
        config = _load_sync_config()
        type_config = config["metadata_sources"]["types"][metadata_type]

        # Download reference data if needed
        reference_data = _download_and_cache_reference_data(
            metadata_type, type_config, force_download, forced_server
        )

        if not reference_data:
            results["success"] = False
            results["errors"].append(
                f"Could not download reference data for {metadata_type}"
            )
            return results

        # Process each station
        for station in stations:
            try:
                station_result = _process_single_station(
                    station=station,
                    metadata_type=metadata_type,
                    reference_data=reference_data,
                    url=url,
                    log_level=log_level,
                    update_mode=update_mode,
                    show_comparison=show_comparison,
                    backup=backup,
                )

                results["stations_processed"] += 1

                if station_result.get("updated", False):
                    results["stations_updated"] += 1

            except Exception as e:
                results["stations_failed"] += 1
                results["errors"].append(f"{station}: {str(e)}")
                logger.error(f"Failed to process station {station}: {e}")
                # Continue with other stations (never abort)

    except Exception as e:
        results["success"] = False
        results["errors"].append(
            f"Failed to process metadata type {metadata_type}: {str(e)}"
        )
        logger.error(f"Failed to process metadata type {metadata_type}: {e}")

    return results


def _download_and_cache_reference_data(
    metadata_type, type_config, force_download, forced_server
):
    """Download and cache reference data with validation."""
    if metadata_type == "gamit-station-info":
        # Check if station info file exists, if not download it
        station_config_dir = _get_station_config_dir()
        station_info_path = station_config_dir / "station.info.sopac.apr05"

        if not station_info_path.exists() or force_download:
            logger = get_logger(__name__)
            print(
                f"Station info file not found, downloading from {REFERENCE_DATA_CONFIG['station-info']['remote_host']}...",
                file=sys.stderr,
            )
            _fetch_station_info(station_config_dir, logger)

        return _parse_station_info_file()

    return None


def _process_single_station(
    station,
    metadata_type,
    reference_data,
    url,
    log_level,
    update_mode,
    show_comparison,
    backup,
):
    """Process a single station for a specific metadata type."""
    result = {"updated": False}

    if metadata_type == "gamit-station-info":
        try:
            # Get TOS data using the exact same approach as PrintTOS
            station_info = gpsqc.gps_metadata(station, url, loglevel=logging.CRITICAL)
            if not station_info:
                print(f"Error: No TOS data found for {station}", file=sys.stderr)
                return result

            # Generate GAMIT format using the same function as PrintTOS --format gamit
            tos_lines = gpsf.print_station_info(station_info, loglevel=logging.CRITICAL)

            # Get reference data
            ref_lines = reference_data.get(station, [])
            if not ref_lines:
                print(f"Error: No reference data found for {station}", file=sys.stderr)
                return result

            # Enhanced visual comparison
            differences_found = not _lines_are_identical(tos_lines, ref_lines)

            if not differences_found:
                print(f"✓ {station}: No differences found")
            else:
                if show_comparison:
                    print(f"\n=== {station} - Session-by-Session Comparison ===")
                    _display_enhanced_comparison(tos_lines, ref_lines, station)
                    print()
                else:
                    print(
                        f"⚠️  {station}: Differences detected (use --compare for details)"
                    )

            if update_mode and differences_found:
                print(
                    f"✓ {station}: Would update local data (update logic not implemented)",
                    file=sys.stderr,
                )
                result["updated"] = True

        except Exception as e:
            print(f"Error processing {station}: {e}", file=sys.stderr)

    return result


def _lines_are_identical(tos_lines, ref_lines):
    """Quick check if TOS and reference lines are identical."""
    tos_set = set(line.strip() for line in tos_lines)
    ref_set = set(line.strip() for line in ref_lines)
    return tos_set == ref_set


def _parse_gamit_session(line):
    """Parse a GAMIT line into structured session data."""
    if len(line) < 50:
        return None

    # Define GAMIT column positions based on the format
    session = {
        "raw_line": line,
        "station": line[1:5].strip(),
        "station_name": line[7:24].strip(),
        "start_date": line[25:43].strip(),
        "end_date": line[44:62].strip(),
        "antenna_height": line[63:71].strip(),
        "height_code": line[72:78].strip(),
        "ant_north": line[79:87].strip(),
        "ant_east": line[88:95].strip(),
        "receiver_type": line[96:118].strip(),
        "receiver_fw": line[118:138].strip(),
        "sw_version": line[139:145].strip(),
        "receiver_sn": line[146:163].strip(),
        "antenna_type": line[164:180].strip(),
        "dome": line[181:186].strip(),
        "antenna_sn": line[187:].strip(),
    }

    return session


def _match_sessions(tos_sessions, ref_sessions):
    """Match TOS and reference sessions by station and timing."""
    matched_pairs = []
    tos_only = []
    ref_only = list(ref_sessions)  # Start with all ref sessions

    for tos_session in tos_sessions:
        best_match = None
        best_score = 0
        best_index = -1

        for i, ref_session in enumerate(ref_only):
            if (
                tos_session["station"] == ref_session["station"]
                and tos_session["start_date"] == ref_session["start_date"]
                and tos_session["end_date"] == ref_session["end_date"]
            ):
                # Perfect match on station + timing
                best_match = ref_session
                best_score = 100
                best_index = i
                break
            elif (
                tos_session["station"] == ref_session["station"]
                and tos_session["start_date"] == ref_session["start_date"]
            ):
                # Match on station + start date (different end dates)
                if best_score < 80:
                    best_match = ref_session
                    best_score = 80
                    best_index = i

        if best_match:
            matched_pairs.append((tos_session, best_match))
            ref_only.pop(best_index)
        else:
            tos_only.append(tos_session)

    return matched_pairs, tos_only, ref_only


def _display_enhanced_comparison(tos_lines, ref_lines, station):
    """Display enhanced visual comparison between TOS and reference data."""
    # ANSI color codes
    COLORS = {
        "RED": "\033[91m",
        "GREEN": "\033[92m",
        "YELLOW": "\033[93m",
        "BLUE": "\033[94m",
        "CYAN": "\033[96m",
        "GRAY": "\033[90m",
        "RESET": "\033[0m",
    }

    # Parse sessions
    tos_sessions = []
    for line in tos_lines:
        session = _parse_gamit_session(line)
        if session:
            tos_sessions.append(session)

    ref_sessions = []
    for line in ref_lines:
        session = _parse_gamit_session(line)
        if session:
            ref_sessions.append(session)

    # Match sessions
    matched, tos_only, ref_only = _match_sessions(tos_sessions, ref_sessions)

    # Display matched sessions with detailed comparison
    for tos_session, ref_session in matched:
        _display_session_comparison(tos_session, ref_session, COLORS)

    # Display TOS-only sessions
    if tos_only:
        print("\nExtra sessions in TOS (not in reference):")
        for session in tos_only:
            _display_session_line(session, "", "TOS", "")

    # Display reference-only sessions
    if ref_only:
        print("\nMissing sessions from TOS (exist in reference):")
        for session in ref_only:
            _display_session_line(session, "", "REF", "")


def _display_session_comparison(tos_session, ref_session, colors):
    """Display side-by-side comparison of a matched session."""
    print(
        f"\n{colors['CYAN']}Session {tos_session['start_date'][:8]} to {tos_session['end_date'][:8]}:{colors['RESET']}"
    )

    # Show both lines with differences highlighted in both
    ref_line_colored = _highlight_differences(
        ref_session, tos_session, colors, is_reference=True
    )
    tos_line_colored = _highlight_differences(
        tos_session, ref_session, colors, is_reference=False
    )

    print(f"REF: {ref_line_colored}")
    print(f"TOS: {tos_line_colored}")

    # Show summary of differences
    if tos_session["raw_line"].strip() != ref_session["raw_line"].strip():
        differences = _find_column_differences(tos_session, ref_session)
        if differences:
            print(f"     Differences in: {', '.join(differences)}")


def _display_session_line(session, color, prefix, reset):
    """Display a session line with color coding."""
    print(f"{color}{prefix}: {session['raw_line']}{reset}")


def _highlight_differences(session, other_session, colors, is_reference=False):
    """Create a colored version of the line highlighting differences."""
    if session["raw_line"].strip() == other_session["raw_line"].strip():
        return session["raw_line"]  # No differences

    # Start with the raw line
    line = session["raw_line"]

    # Define column positions for highlighting (sorted by start position)
    column_ranges = [
        ("station_name", 7, 24, colors["BLUE"]),  # Station name differences
        ("start_date", 25, 43, colors["RED"]),  # Session start date
        ("end_date", 44, 62, colors["RED"]),  # Session end date
        ("antenna_height", 63, 71, colors["RED"]),  # Antenna height (critical)
        ("receiver_fw", 118, 138, colors["CYAN"]),  # Receiver firmware
        ("sw_version", 139, 146, colors["CYAN"]),  # Software version
        ("receiver_sn", 146, 163, colors["YELLOW"]),  # Receiver serial number
        (
            "antenna_sn",
            187,
            len(line),
            colors["YELLOW"],
        ),  # Antenna serial number to end
    ]

    colored_line = ""
    last_end = 0

    for field, start, end, color in column_ranges:
        # Add the gray part before this column (spaces, etc.)
        if start > last_end:
            colored_line += f"{colors['GRAY']}{line[last_end:start]}{colors['RESET']}"

        # Extract this column (make sure we don't go beyond line length)
        actual_end = min(end, len(line))
        column_value = line[start:actual_end]

        # Check if this column differs - if so, color both REF and TOS versions
        if session[field] != other_session[field]:
            colored_line += f"{color}{column_value}{colors['RESET']}"
        else:
            # Non-differing parts should be gray
            colored_line += f"{colors['GRAY']}{column_value}{colors['RESET']}"

        last_end = actual_end

    # Add any remaining part of the line in gray
    if last_end < len(line):
        colored_line += f"{colors['GRAY']}{line[last_end:]}{colors['RESET']}"

    return colored_line


def _find_column_differences(tos_session, ref_session):
    """Find which columns differ between two sessions."""
    differences = []

    # Check each column for differences
    columns_to_check = [
        ("station_name", "Station Name"),
        ("antenna_height", "Antenna Height"),
        ("receiver_type", "Receiver Type"),
        ("receiver_fw", "Receiver Firmware"),
        ("sw_version", "Software Version"),
        ("receiver_sn", "Receiver S/N"),
        ("antenna_type", "Antenna Type"),
        ("dome", "Dome"),
        ("antenna_sn", "Antenna S/N"),
    ]

    for field, display_name in columns_to_check:
        if tos_session[field] != ref_session[field]:
            differences.append(display_name)

    # Check session periods with wiggle room (only check year, day, and hour)
    if _periods_differ_significantly(tos_session, ref_session):
        differences.append("Session Period")

    return differences


def _periods_differ_significantly(tos_session, ref_session):
    """Check if session periods differ significantly (with wiggle room)."""
    try:
        # Parse start dates: "YYYY DDD HH MM SS" format
        tos_start = tos_session["start_date"].strip()
        ref_start = ref_session["start_date"].strip()
        tos_end = tos_session["end_date"].strip()
        ref_end = ref_session["end_date"].strip()

        # Extract year, day, hour for comparison (ignore minutes/seconds)
        def extract_ymdh(date_str):
            if len(date_str) >= 11:  # "2008 073 00"
                parts = date_str.split()
                if len(parts) >= 3:
                    return parts[0], parts[1], parts[2]  # year, day, hour
            return None, None, None

        tos_start_ymdh = extract_ymdh(tos_start)
        ref_start_ymdh = extract_ymdh(ref_start)
        tos_end_ymdh = extract_ymdh(tos_end)
        ref_end_ymdh = extract_ymdh(ref_end)

        # Check if start dates differ significantly (year, day, hour)
        if tos_start_ymdh != ref_start_ymdh:
            return True

        # Check if end dates differ significantly (year, day, hour)
        if tos_end_ymdh != ref_end_ymdh:
            return True

    except Exception:
        # If parsing fails, be conservative and flag as different
        return (
            tos_session["start_date"] != ref_session["start_date"]
            or tos_session["end_date"] != ref_session["end_date"]
        )

    return False


def _generate_sync_summary(results, metadata_types, stations):
    """Generate a summary report of the sync operation."""
    total_processed = sum(r.get("stations_processed", 0) for r in results.values())
    total_updated = sum(r.get("stations_updated", 0) for r in results.values())
    total_failed = sum(r.get("stations_failed", 0) for r in results.values())

    print("\n=== Sync Summary ===", file=sys.stderr)
    print(f"Metadata types: {len(metadata_types)}", file=sys.stderr)
    print(f"Stations processed: {total_processed}", file=sys.stderr)
    print(f"Stations updated: {total_updated}", file=sys.stderr)

    if total_failed > 0:
        print(f"Stations failed: {total_failed}", file=sys.stderr)

        # Report errors
        for metadata_type, result in results.items():
            errors = result.get("errors", [])
            if errors:
                print(f"\n{metadata_type} errors:", file=sys.stderr)
                for error in errors:
                    print(f"  - {error}", file=sys.stderr)


# ============================================================================
# SAFE UPDATE SYSTEM - File Management and Versioning
# ============================================================================


def _get_backup_dir(base_dir):
    """Get or create backup directory."""
    backup_dir = base_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def _get_work_dir(base_dir):
    """Get or create working directory for file edits."""
    work_dir = base_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def _create_versioned_backup(original_file, backup_dir=None, metadata=None):
    """
    Create a timestamped backup with metadata tracking.

    Args:
        original_file: Path to original file to backup
        backup_dir: Directory for backups (default: original_file.parent/backups)
        metadata: Additional metadata to store with backup

    Returns:
        dict: Backup information with path, timestamp, checksum, etc.
    """
    import hashlib
    from datetime import datetime
    from pathlib import Path

    original_file = Path(original_file)

    if not original_file.exists():
        raise FileNotFoundError(f"Original file not found: {original_file}")

    # Default backup directory
    if backup_dir is None:
        backup_dir = _get_backup_dir(original_file.parent)
    else:
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)

    # Generate timestamp for backup
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"{original_file.name}.backup.{timestamp}"
    backup_path = backup_dir / backup_filename

    # Calculate checksum of original
    with open(original_file, "rb") as f:
        checksum = hashlib.sha256(f.read()).hexdigest()

    # Copy original to backup location
    import shutil

    shutil.copy2(original_file, backup_path)

    # Create backup metadata
    backup_info = {
        "backup_id": timestamp,
        "original_file": str(original_file),
        "backup_path": str(backup_path),
        "created_at": datetime.now().isoformat(),
        "original_size": original_file.stat().st_size,
        "original_checksum": checksum,
        "metadata": metadata or {},
    }

    # Update backup registry
    _update_backup_registry(backup_dir, backup_info)

    return backup_info


def _update_backup_registry(backup_dir, backup_info):
    """Update the backup registry with new backup information."""
    import json

    registry_path = backup_dir / "backup_registry.json"

    # Load existing registry or create new one
    if registry_path.exists():
        with open(registry_path, "r") as f:
            registry = json.load(f)
    else:
        registry = {"backups": []}

    # Add new backup info
    registry["backups"].append(backup_info)

    # Sort by creation time (newest first)
    registry["backups"].sort(key=lambda x: x["created_at"], reverse=True)

    # Implement retention policy (keep last 10 backups per file)
    original_file = backup_info["original_file"]
    file_backups = [
        b for b in registry["backups"] if b["original_file"] == original_file
    ]

    if len(file_backups) > 10:
        # Remove oldest backups beyond retention limit
        to_remove = file_backups[10:]
        for old_backup in to_remove:
            try:
                Path(old_backup["backup_path"]).unlink(missing_ok=True)
                registry["backups"].remove(old_backup)
            except Exception:
                pass  # Ignore cleanup failures

    # Save updated registry
    with open(registry_path, "w") as f:
        json.dump(registry, f, indent=2)


def _list_backups(backup_dir, original_file=None):
    """List available backups for a file or all backups."""
    import json

    backup_dir = Path(backup_dir)
    registry_path = backup_dir / "backup_registry.json"

    if not registry_path.exists():
        return []

    with open(registry_path, "r") as f:
        registry = json.load(f)

    backups = registry.get("backups", [])

    if original_file:
        backups = [b for b in backups if b["original_file"] == str(original_file)]

    return backups


def _restore_from_backup(backup_id, target_path=None):
    """
    Restore a file from backup.

    Args:
        backup_id: Timestamp ID of backup to restore
        target_path: Where to restore (default: original location)

    Returns:
        bool: Success status
    """
    import shutil

    # Find backup in registry
    station_config_dir = _get_station_config_dir()
    backup_dir = _get_backup_dir(station_config_dir)
    backups = _list_backups(backup_dir)

    backup_info = None
    for backup in backups:
        if backup["backup_id"] == backup_id:
            backup_info = backup
            break

    if not backup_info:
        raise ValueError(f"Backup with ID {backup_id} not found")

    backup_path = Path(backup_info["backup_path"])
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup file not found: {backup_path}")

    # Determine restore target
    if target_path is None:
        target_path = Path(backup_info["original_file"])
    else:
        target_path = Path(target_path)

    # Create backup of current file before restoring
    if target_path.exists():
        _create_versioned_backup(
            target_path,
            backup_dir,
            metadata={"restore_operation": True, "restored_from": backup_id},
        )

    # Restore from backup
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, target_path)

    return True


# ============================================================================
# SAFE UPDATE SYSTEM - Download and Verification
# ============================================================================


def _download_fresh_reference(
    metadata_type, server_config, temp_dir, force_download=False
):
    """
    Download a fresh copy of reference data with integrity checks.

    Args:
        metadata_type: Type of metadata to download (e.g., 'gamit-station-info')
        server_config: Server configuration dictionary
        temp_dir: Temporary directory for downloads
        force_download: Force download even if file exists and seems current

    Returns:
        dict: Download result with path, metadata, integrity info
    """
    import hashlib
    import subprocess
    from datetime import datetime
    from pathlib import Path

    logger = get_logger(__name__)
    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    if metadata_type == "gamit-station-info":
        config = REFERENCE_DATA_CONFIG["station-info"]

        # Generate unique filename for fresh download
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_filename = f"station.info.sopac.apr05.fresh.{timestamp}"
        temp_path = temp_dir / temp_filename

        print(
            f"Downloading fresh copy from {config['remote_host']}...", file=sys.stderr
        )
        print(f"Remote: {config['remote_path']}", file=sys.stderr)
        print(f"Temp: {temp_path}", file=sys.stderr)

        try:
            # Use scp to fetch the file to temporary location
            cmd = [
                "scp",
                f"{config['remote_host']}:{config['remote_path']}",
                str(temp_path),
            ]

            logger.info(f"Executing fresh download: {' '.join(cmd)}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,  # 2 minute timeout for fresh downloads
            )

            if result.returncode != 0:
                raise Exception(f"SCP failed: {result.stderr}")

            if not temp_path.exists():
                raise Exception("Download completed but file not found")

            # Verify file integrity
            file_size = temp_path.stat().st_size
            if file_size == 0:
                raise Exception("Downloaded file is empty")

            # Basic format validation for GAMIT files
            if not _validate_gamit_file_format(temp_path):
                raise Exception("Downloaded file failed format validation")

            # Calculate checksum
            with open(temp_path, "rb") as f:
                checksum = hashlib.sha256(f.read()).hexdigest()

            # Compare with existing local file if it exists
            local_path = _get_station_config_dir() / config["local_filename"]
            changed = True
            local_checksum = None

            if local_path.exists():
                with open(local_path, "rb") as f:
                    local_checksum = hashlib.sha256(f.read()).hexdigest()
                changed = checksum != local_checksum

            download_result = {
                "success": True,
                "temp_path": str(temp_path),
                "file_size": file_size,
                "checksum": checksum,
                "local_checksum": local_checksum,
                "changed": changed,
                "download_time": datetime.now().isoformat(),
                "metadata": {
                    "metadata_type": metadata_type,
                    "source_host": config["remote_host"],
                    "source_path": config["remote_path"],
                },
            }

            size_mb = file_size / (1024 * 1024)
            if changed:
                print(
                    f"✓ Fresh copy downloaded ({size_mb:.1f} MB) - Changes detected",
                    file=sys.stderr,
                )
            else:
                print(
                    f"✓ Fresh copy downloaded ({size_mb:.1f} MB) - No changes from local",
                    file=sys.stderr,
                )

            logger.info(f"Fresh download successful: {temp_path} ({size_mb:.1f} MB)")
            return download_result

        except subprocess.TimeoutExpired:
            error = "Download timeout (120s)"
            print(f"✗ {error}", file=sys.stderr)
            logger.error(error)
            return {"success": False, "error": error}
        except Exception as e:
            error = f"Download error: {e}"
            print(f"✗ {error}", file=sys.stderr)
            logger.error(error)
            return {"success": False, "error": str(e)}

    return {"success": False, "error": f"Unsupported metadata type: {metadata_type}"}


def _validate_gamit_file_format(file_path):
    """
    Basic validation of GAMIT station.info file format.

    Args:
        file_path: Path to file to validate

    Returns:
        bool: True if file appears to be valid GAMIT format
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        if len(lines) < 5:
            return False

        # Check for GAMIT header indicators
        header_found = False
        station_lines = 0

        for line in lines:
            line = line.rstrip("\n\r")

            # Look for GAMIT header
            if line.startswith("*SITE") and "Station Name" in line:
                header_found = True
                continue

            # Skip comments and empty lines
            if line.startswith("#") or line.startswith("*") or not line.strip():
                continue

            # Count potential station lines
            if len(line) > 100:  # GAMIT lines are long
                station_lines += 1

        # Valid if we found header and reasonable number of station lines
        return header_found and station_lines > 0

    except Exception:
        return False


def _compare_with_local(fresh_file_path, local_file_path):
    """
    Compare fresh download with local file to detect changes.

    Args:
        fresh_file_path: Path to freshly downloaded file
        local_file_path: Path to current local file

    Returns:
        dict: Comparison results with differences summary
    """
    import hashlib
    from pathlib import Path

    fresh_path = Path(fresh_file_path)
    local_path = Path(local_file_path)

    if not fresh_path.exists():
        return {"error": "Fresh file not found"}

    if not local_path.exists():
        return {"changed": True, "reason": "Local file does not exist"}

    # Compare checksums first (quick check)
    def get_checksum(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    fresh_checksum = get_checksum(fresh_path)
    local_checksum = get_checksum(local_path)

    if fresh_checksum == local_checksum:
        return {
            "changed": False,
            "fresh_checksum": fresh_checksum,
            "local_checksum": local_checksum,
        }

    # Files differ - analyze differences
    try:
        with open(fresh_path, "r", encoding="utf-8", errors="ignore") as f:
            fresh_lines = f.readlines()
        with open(local_path, "r", encoding="utf-8", errors="ignore") as f:
            local_lines = f.readlines()

        differences = {
            "changed": True,
            "fresh_checksum": fresh_checksum,
            "local_checksum": local_checksum,
            "fresh_lines": len(fresh_lines),
            "local_lines": len(local_lines),
            "line_differences": [],
        }

        # Find specific line differences (sample up to 10)
        max_lines = max(len(fresh_lines), len(local_lines))
        diff_count = 0

        for i in range(min(max_lines, 1000)):  # Limit comparison to first 1000 lines
            fresh_line = fresh_lines[i] if i < len(fresh_lines) else ""
            local_line = local_lines[i] if i < len(local_lines) else ""

            if fresh_line != local_line and diff_count < 10:
                differences["line_differences"].append(
                    {
                        "line_num": i + 1,
                        "fresh": fresh_line.rstrip(),
                        "local": local_line.rstrip(),
                    }
                )
                diff_count += 1

        return differences

    except Exception as e:
        return {"changed": True, "error": f"Comparison failed: {e}"}


# ============================================================================
# SAFE UPDATE SYSTEM - Working Copy Management
# ============================================================================


def _create_working_copy(reference_file, work_dir, metadata=None):
    """
    Create an editable working copy with tracking.

    Args:
        reference_file: Path to reference file to copy
        work_dir: Working directory for edits
        metadata: Additional metadata to track

    Returns:
        dict: Working copy information with original checksum for verification
    """
    import hashlib
    import shutil
    from datetime import datetime
    from pathlib import Path

    reference_file = Path(reference_file)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    if not reference_file.exists():
        raise FileNotFoundError(f"Reference file not found: {reference_file}")

    # Calculate original checksum
    with open(reference_file, "rb") as f:
        original_checksum = hashlib.sha256(f.read()).hexdigest()

    # Create working copy filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_filename = f"{reference_file.name}.work.{timestamp}"
    work_path = work_dir / work_filename

    # Copy to working location
    shutil.copy2(reference_file, work_path)

    # Verify copy integrity
    with open(work_path, "rb") as f:
        work_checksum = hashlib.sha256(f.read()).hexdigest()

    if original_checksum != work_checksum:
        raise Exception("Working copy creation failed - checksum mismatch")

    work_info = {
        "work_path": str(work_path),
        "original_file": str(reference_file),
        "original_checksum": original_checksum,
        "work_checksum": work_checksum,
        "created_at": datetime.now().isoformat(),
        "metadata": metadata or {},
    }

    return work_info


def _apply_station_updates(work_file, station_updates, original_metadata):
    """
    Apply station-specific updates to working copy while preserving file structure.

    Args:
        work_file: Path to working copy file
        station_updates: Dict of station updates {station_id: [new_lines]}
        original_metadata: Original file metadata for verification

    Returns:
        dict: Update results with change summary
    """
    from pathlib import Path

    work_file = Path(work_file)

    if not work_file.exists():
        raise FileNotFoundError(f"Working file not found: {work_file}")

    # Read current working file
    with open(work_file, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    # Track changes
    changes_made = []
    lines_modified = 0
    lines_added = 0
    lines_removed = 0

    # Parse existing stations for reference
    existing_stations = {}
    for i, line in enumerate(lines):
        if line.startswith(" ") and len(line) > 100:  # GAMIT station line
            station_id = line[1:5].strip()
            if station_id:
                if station_id not in existing_stations:
                    existing_stations[station_id] = []
                existing_stations[station_id].append(i)

    # Apply updates for each station
    modified_lines = lines[:]  # Start with copy of all lines

    for station_id, new_station_lines in station_updates.items():
        if station_id in existing_stations:
            # Replace existing station lines
            existing_indices = existing_stations[station_id]

            # Remove old lines (in reverse order to maintain indices)
            for idx in sorted(existing_indices, reverse=True):
                del modified_lines[idx]
                lines_removed += 1

            # Find insertion point (after header/comments, before next station)
            insert_point = len(modified_lines)
            for i, line in enumerate(modified_lines):
                if line.startswith(" ") and len(line) > 100:
                    line_station = line[1:5].strip()
                    if line_station > station_id:  # Maintain alphabetical order
                        insert_point = i
                        break

            # Insert new lines
            for j, new_line in enumerate(new_station_lines):
                if not new_line.endswith("\n"):
                    new_line += "\n"
                modified_lines.insert(insert_point + j, new_line)
                lines_added += 1

            changes_made.append(
                f"Updated {len(new_station_lines)} lines for station {station_id}"
            )

        else:
            # Add new station lines
            # Find appropriate insertion point to maintain order
            insert_point = len(modified_lines)
            for i, line in enumerate(modified_lines):
                if line.startswith(" ") and len(line) > 100:
                    line_station = line[1:5].strip()
                    if line_station > station_id:
                        insert_point = i
                        break

            # Insert new station lines
            for j, new_line in enumerate(new_station_lines):
                if not new_line.endswith("\n"):
                    new_line += "\n"
                modified_lines.insert(insert_point + j, new_line)
                lines_added += 1

            changes_made.append(
                f"Added {len(new_station_lines)} lines for new station {station_id}"
            )

    # Write modified content back to working file
    with open(work_file, "w", encoding="utf-8") as f:
        f.writelines(modified_lines)

    # Verify file is still valid GAMIT format
    if not _validate_gamit_file_format(work_file):
        raise Exception("Working file became invalid after updates")

    update_result = {
        "success": True,
        "changes_made": changes_made,
        "stations_updated": list(station_updates.keys()),
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "lines_modified": lines_modified,
        "total_lines": len(modified_lines),
    }

    return update_result


def _generate_station_lines_from_tos(station_info):
    """
    Generate GAMIT-format lines from TOS station data.

    Args:
        station_info: Station data from TOS API

    Returns:
        list: GAMIT-formatted lines for the station
    """
    # Use existing function from gps_metadata_functions
    try:
        lines = gpsf.print_station_info(station_info, loglevel=logging.CRITICAL)
        return lines
    except Exception as e:
        raise Exception(f"Failed to generate GAMIT lines from TOS data: {e}")


def _cleanup_working_files(work_dir, max_age_hours=24):
    """
    Clean up old working files to prevent disk space issues.

    Args:
        work_dir: Working directory to clean
        max_age_hours: Maximum age of files to keep (default 24 hours)
    """
    import time
    from pathlib import Path

    work_dir = Path(work_dir)
    if not work_dir.exists():
        return

    current_time = time.time()
    max_age_seconds = max_age_hours * 3600

    for work_file in work_dir.glob("*.work.*"):
        try:
            file_age = current_time - work_file.stat().st_mtime
            if file_age > max_age_seconds:
                work_file.unlink()
        except Exception:
            pass  # Ignore cleanup failures


# ============================================================================
# SAFE UPDATE SYSTEM - Change Verification and Integrity
# ============================================================================


def _verify_intended_changes(
    original_file, modified_file, intended_stations, metadata=None
):
    """
    Verify that only intended changes were made to the file.

    Args:
        original_file: Path to original file
        modified_file: Path to modified file
        intended_stations: List of station IDs that should have changes
        metadata: Additional verification metadata

    Returns:
        dict: Verification results with detailed change analysis
    """
    import hashlib
    from pathlib import Path

    original_file = Path(original_file)
    modified_file = Path(modified_file)

    if not original_file.exists():
        return {"success": False, "error": "Original file not found"}
    if not modified_file.exists():
        return {"success": False, "error": "Modified file not found"}

    # Read both files
    try:
        with open(original_file, "r", encoding="utf-8", errors="ignore") as f:
            original_lines = f.readlines()
        with open(modified_file, "r", encoding="utf-8", errors="ignore") as f:
            modified_lines = f.readlines()
    except Exception as e:
        return {"success": False, "error": f"Failed to read files: {e}"}

    # Analyze changes
    verification_result = {
        "success": True,
        "file_integrity_ok": True,
        "intended_changes_only": True,
        "change_summary": {
            "lines_added": 0,
            "lines_removed": 0,
            "lines_modified": 0,
            "stations_affected": [],
            "unintended_changes": [],
        },
        "checksums": {
            "original": hashlib.sha256(original_file.read_bytes()).hexdigest(),
            "modified": hashlib.sha256(modified_file.read_bytes()).hexdigest(),
        },
    }

    # Parse stations from both files
    original_stations = _parse_stations_from_lines(original_lines)
    modified_stations = _parse_stations_from_lines(modified_lines)

    # Check file format integrity
    if not _validate_gamit_file_format(modified_file):
        verification_result["success"] = False
        verification_result["file_integrity_ok"] = False
        verification_result["error"] = "Modified file failed GAMIT format validation"
        return verification_result

    # Analyze station-level changes
    all_stations = set(original_stations.keys()) | set(modified_stations.keys())
    intended_stations_set = set(intended_stations)

    for station_id in all_stations:
        original_station_lines = original_stations.get(station_id, [])
        modified_station_lines = modified_stations.get(station_id, [])

        if original_station_lines != modified_station_lines:
            # This station has changes
            verification_result["change_summary"]["stations_affected"].append(
                station_id
            )

            if station_id not in intended_stations_set:
                # Unintended change detected
                verification_result["intended_changes_only"] = False
                verification_result["change_summary"]["unintended_changes"].append(
                    {
                        "station_id": station_id,
                        "change_type": "unexpected_modification",
                        "original_lines": len(original_station_lines),
                        "modified_lines": len(modified_station_lines),
                    }
                )

            # Count line changes
            if not original_station_lines:
                verification_result["change_summary"]["lines_added"] += len(
                    modified_station_lines
                )
            elif not modified_station_lines:
                verification_result["change_summary"]["lines_removed"] += len(
                    original_station_lines
                )
            else:
                verification_result["change_summary"]["lines_modified"] += max(
                    len(original_station_lines), len(modified_station_lines)
                )

    # Check for changes in non-station parts (headers, comments)
    original_non_station = _extract_non_station_lines(original_lines)
    modified_non_station = _extract_non_station_lines(modified_lines)

    if original_non_station != modified_non_station:
        verification_result["intended_changes_only"] = False
        verification_result["change_summary"]["unintended_changes"].append(
            {
                "change_type": "header_or_comment_modified",
                "details": "Non-station lines were unexpectedly modified",
            }
        )

    # Overall success determination
    if (
        not verification_result["file_integrity_ok"]
        or not verification_result["intended_changes_only"]
    ):
        verification_result["success"] = False

    return verification_result


def _parse_stations_from_lines(lines):
    """Parse station data from GAMIT file lines."""
    stations = {}

    for line in lines:
        if line.startswith(" ") and len(line) > 100:  # GAMIT station line
            station_id = line[1:5].strip()
            if station_id:
                if station_id not in stations:
                    stations[station_id] = []
                stations[station_id].append(line)

    return stations


def _extract_non_station_lines(lines):
    """Extract non-station lines (headers, comments) from GAMIT file."""
    non_station_lines = []

    for line in lines:
        if not (line.startswith(" ") and len(line) > 100):  # Not a station line
            non_station_lines.append(line)

    return non_station_lines


def _validate_file_integrity_comprehensive(file_path, reference_checksums=None):
    """
    Comprehensive file integrity validation.

    Args:
        file_path: Path to file to validate
        reference_checksums: Optional reference checksums for comparison

    Returns:
        dict: Comprehensive integrity check results
    """
    import hashlib
    from pathlib import Path

    file_path = Path(file_path)

    if not file_path.exists():
        return {"success": False, "error": "File not found"}

    integrity_result = {
        "success": True,
        "file_exists": True,
        "file_readable": True,
        "format_valid": False,
        "size_bytes": 0,
        "line_count": 0,
        "station_count": 0,
        "checksum": None,
        "validation_errors": [],
    }

    try:
        # Basic file properties
        integrity_result["size_bytes"] = file_path.stat().st_size

        if integrity_result["size_bytes"] == 0:
            integrity_result["success"] = False
            integrity_result["validation_errors"].append("File is empty")
            return integrity_result

        # Read and analyze file
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        integrity_result["line_count"] = len(lines)

        # Calculate checksum
        with open(file_path, "rb") as f:
            integrity_result["checksum"] = hashlib.sha256(f.read()).hexdigest()

        # Format validation
        integrity_result["format_valid"] = _validate_gamit_file_format(file_path)
        if not integrity_result["format_valid"]:
            integrity_result["validation_errors"].append("Invalid GAMIT file format")

        # Count stations
        stations = _parse_stations_from_lines(lines)
        integrity_result["station_count"] = len(stations)

        # Additional validations
        if integrity_result["station_count"] == 0:
            integrity_result["validation_errors"].append("No station data found")

        # Check for common formatting issues in station lines
        station_line_count = 0
        for i, line in enumerate(lines[:200]):  # Check first 200 lines
            if line.startswith(" ") and len(line) > 100:  # Station line
                station_line_count += 1
                line_content = line.rstrip()
                line_length = len(line_content)

                # GAMIT lines should be reasonably long with proper format
                if line_length < 180:
                    integrity_result["validation_errors"].append(
                        f"Line {i+1}: Station line too short ({line_length} chars) - possible truncation"
                    )
                elif line_length > 220:
                    integrity_result["validation_errors"].append(
                        f"Line {i+1}: Station line too long ({line_length} chars) - possible format issue"
                    )

                # Check basic GAMIT structure (station ID should be in positions 1-5)
                if len(line_content) > 5:
                    station_id = line_content[1:5].strip()
                    if not station_id or len(station_id) < 3:
                        integrity_result["validation_errors"].append(
                            f"Line {i+1}: Invalid or missing station ID"
                        )

                # Stop after checking reasonable number of station lines
                if (
                    station_line_count >= 50
                    or len(integrity_result["validation_errors"]) >= 10
                ):
                    break

        # Compare with reference if provided
        if reference_checksums:
            if integrity_result["checksum"] not in reference_checksums:
                integrity_result["validation_errors"].append(
                    "Checksum does not match reference"
                )

        # Set success status
        if integrity_result["validation_errors"]:
            integrity_result["success"] = False

    except Exception as e:
        integrity_result["success"] = False
        integrity_result["file_readable"] = False
        integrity_result["validation_errors"].append(f"Failed to validate file: {e}")

    return integrity_result


def _generate_change_report(verification_result, work_info, update_result):
    """
    Generate a comprehensive change report for user review.

    Args:
        verification_result: Results from change verification
        work_info: Working copy information
        update_result: Results from applying updates

    Returns:
        str: Formatted change report
    """
    from datetime import datetime

    report = []
    report.append("=" * 60)
    report.append("SYNCMETA UPDATE VERIFICATION REPORT")
    report.append("=" * 60)
    report.append(f"Generated: {datetime.now().isoformat()}")
    report.append(f"Working file: {work_info['work_path']}")
    report.append(f"Original checksum: {work_info['original_checksum'][:16]}...")
    report.append("")

    # Verification status
    status = "✓ PASSED" if verification_result["success"] else "✗ FAILED"
    report.append(f"Verification Status: {status}")

    if not verification_result["success"]:
        report.append("ERRORS:")
        for error in verification_result.get("validation_errors", []):
            report.append(f"  - {error}")
        if verification_result.get("error"):
            report.append(f"  - {verification_result['error']}")
        report.append("")

    # Change summary
    summary = verification_result["change_summary"]
    report.append("CHANGE SUMMARY:")
    report.append(f"  Stations affected: {len(summary['stations_affected'])}")
    report.append(f"  Lines added: {summary['lines_added']}")
    report.append(f"  Lines removed: {summary['lines_removed']}")
    report.append(f"  Lines modified: {summary['lines_modified']}")

    if summary["stations_affected"]:
        report.append(f"  Affected stations: {', '.join(summary['stations_affected'])}")

    if summary["unintended_changes"]:
        report.append("")
        report.append("⚠️  UNINTENDED CHANGES DETECTED:")
        for change in summary["unintended_changes"]:
            report.append(f"  - {change.get('change_type', 'unknown')}: {change}")

    # Update details
    if update_result.get("changes_made"):
        report.append("")
        report.append("CHANGES MADE:")
        for change in update_result["changes_made"]:
            report.append(f"  - {change}")

    report.append("")
    report.append("=" * 60)

    return "\n".join(report)


# ============================================================================
# SAFE UPDATE SYSTEM - Upload and Rollback
# ============================================================================


def _safe_upload_with_rollback(
    local_file,
    remote_config,
    backup_info,
    metadata_type="gamit-station-info",
    dry_run=False,
):
    """
    Upload file with rollback capability.

    Args:
        local_file: Path to local file to upload
        remote_config: Remote server configuration
        backup_info: Information about the backup created before changes
        metadata_type: Type of metadata being uploaded
        dry_run: If True, simulate upload without actually doing it

    Returns:
        dict: Upload result with rollback information
    """
    import subprocess
    from datetime import datetime
    from pathlib import Path

    logger = get_logger(__name__)
    local_file = Path(local_file)

    if not local_file.exists():
        return {"success": False, "error": "Local file not found"}

    if metadata_type == "gamit-station-info":
        config = REFERENCE_DATA_CONFIG["station-info"]
        remote_path = config["remote_path"]
        remote_host = config["remote_host"]

        upload_result = {
            "success": False,
            "dry_run": dry_run,
            "local_file": str(local_file),
            "remote_host": remote_host,
            "remote_path": remote_path,
            "upload_time": datetime.now().isoformat(),
            "rollback_available": bool(backup_info),
        }

        if dry_run:
            # Simulate upload process
            print(f"🧪 DRY RUN: Would upload {local_file}", file=sys.stderr)
            print(f"🧪 DRY RUN: Target: {remote_host}:{remote_path}", file=sys.stderr)

            # Simulate validation steps
            file_size = local_file.stat().st_size
            print(
                f"🧪 DRY RUN: File size: {file_size / 1024 / 1024:.1f} MB",
                file=sys.stderr,
            )
            print(
                f"🧪 DRY RUN: Backup available for rollback: {backup_info is not None}",
                file=sys.stderr,
            )
            print("🧪 DRY RUN: Upload would succeed (simulated)", file=sys.stderr)

            upload_result["success"] = True
            upload_result["simulated"] = True
            return upload_result

        try:
            # Create remote backup first
            remote_backup_path = (
                f"{remote_path}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )

            print(
                f"Creating remote backup at {remote_host}:{remote_backup_path}...",
                file=sys.stderr,
            )
            backup_cmd = [
                "ssh",
                remote_host.split("@")[0] + "@" + remote_host.split("@")[1],
                f"cp '{remote_path}' '{remote_backup_path}'",
            ]

            result = subprocess.run(
                backup_cmd, capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                logger.warning(f"Remote backup creation failed: {result.stderr}")
                print("⚠️  Warning: Could not create remote backup", file=sys.stderr)
            else:
                print("✓ Remote backup created", file=sys.stderr)
                upload_result["remote_backup"] = remote_backup_path

            # Upload to temporary location first
            temp_remote_path = (
                f"{remote_path}.upload.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )

            print(
                f"Uploading to temporary location: {temp_remote_path}...",
                file=sys.stderr,
            )
            upload_cmd = ["scp", str(local_file), f"{remote_host}:{temp_remote_path}"]

            logger.info(f"Executing upload: {' '.join(upload_cmd)}")
            result = subprocess.run(
                upload_cmd, capture_output=True, text=True, timeout=120
            )

            if result.returncode != 0:
                raise Exception(f"Upload failed: {result.stderr}")

            # Verify upload integrity
            print("Verifying upload integrity...", file=sys.stderr)
            verify_cmd = [
                "ssh",
                remote_host.split("@")[0] + "@" + remote_host.split("@")[1],
                f"test -f '{temp_remote_path}' && wc -l '{temp_remote_path}'",
            ]

            result = subprocess.run(
                verify_cmd, capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                raise Exception("Upload verification failed")

            # Atomic move to final location
            print("Moving to final location...", file=sys.stderr)
            move_cmd = [
                "ssh",
                remote_host.split("@")[0] + "@" + remote_host.split("@")[1],
                f"mv '{temp_remote_path}' '{remote_path}'",
            ]

            result = subprocess.run(
                move_cmd, capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                raise Exception(f"Final move failed: {result.stderr}")

            file_size = local_file.stat().st_size
            print(
                f"✓ Upload successful ({file_size / 1024 / 1024:.1f} MB)",
                file=sys.stderr,
            )

            upload_result["success"] = True
            upload_result["temp_path_used"] = temp_remote_path
            logger.info(
                f"Upload successful: {local_file} -> {remote_host}:{remote_path}"
            )

        except subprocess.TimeoutExpired:
            error = "Upload timeout"
            upload_result["error"] = error
            logger.error(error)
            print(f"✗ {error}", file=sys.stderr)
        except Exception as e:
            error = f"Upload failed: {e}"
            upload_result["error"] = error
            logger.error(error)
            print(f"✗ {error}", file=sys.stderr)

        return upload_result

    return {"success": False, "error": "Unsupported metadata type for upload"}


def _rollback_remote_changes(upload_info, metadata_type="gamit-station-info"):
    """
    Rollback remote changes using backup.

    Args:
        upload_info: Information from previous upload
        metadata_type: Type of metadata that was uploaded

    Returns:
        dict: Rollback result
    """
    import subprocess
    from datetime import datetime

    logger = get_logger(__name__)

    if metadata_type == "gamit-station-info":
        config = REFERENCE_DATA_CONFIG["station-info"]
        remote_path = config["remote_path"]
        remote_host = config["remote_host"]

        rollback_result = {
            "success": False,
            "rollback_time": datetime.now().isoformat(),
            "remote_host": remote_host,
            "remote_path": remote_path,
        }

        if "remote_backup" not in upload_info:
            rollback_result["error"] = "No remote backup available for rollback"
            return rollback_result

        remote_backup_path = upload_info["remote_backup"]

        try:
            print(f"Rolling back from {remote_backup_path}...", file=sys.stderr)
            rollback_cmd = [
                "ssh",
                remote_host.split("@")[0] + "@" + remote_host.split("@")[1],
                f"cp '{remote_backup_path}' '{remote_path}'",
            ]

            result = subprocess.run(
                rollback_cmd, capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                raise Exception(f"Rollback failed: {result.stderr}")

            print("✓ Rollback successful", file=sys.stderr)
            rollback_result["success"] = True
            logger.info(f"Rollback successful: {remote_backup_path} -> {remote_path}")

        except Exception as e:
            error = f"Rollback failed: {e}"
            rollback_result["error"] = error
            logger.error(error)
            print(f"✗ {error}", file=sys.stderr)

        return rollback_result

    return {"success": False, "error": f"Rollback not supported for {metadata_type}"}


# ============================================================================
# SAFE UPDATE SYSTEM - Main Workflow Integration
# ============================================================================


def _safe_update_workflow(
    stations,
    metadata_type,
    url,
    update_mode=True,
    dry_run=False,
    interactive=True,
    backup_required=True,
    production_mode=False,
):
    """
    Main safe update workflow orchestrator.

    Args:
        stations: List of station IDs to update
        metadata_type: Type of metadata (e.g., 'gamit-station-info')
        url: TOS API URL
        update_mode: Whether to perform updates (vs just comparison)
        dry_run: Test mode - no actual uploads
        interactive: Prompt for confirmations (default: True)
        backup_required: Require backup before changes

    Returns:
        dict: Complete workflow results
    """
    from pathlib import Path

    logger = get_logger(__name__)
    workflow_result = {
        "success": False,
        "dry_run": dry_run,
        "stations_requested": stations,
        "stations_processed": [],
        "stations_updated": [],
        "errors": [],
        "workflow_steps": [],
    }

    try:
        # Step 1: Download fresh reference data
        # Use structured logging for production + user feedback
        def log_step(step_num, description, level="INFO", **extra_data):
            """Log workflow step with both structured logging and user feedback."""
            # Structured logging for production
            logger.log(
                getattr(logging, level),
                f"Step {step_num}: {description}",
                extra={
                    "workflow": "safe_update",
                    "step": step_num,
                    "metadata_type": metadata_type,
                    "dry_run": dry_run,
                    **extra_data,
                },
            )

            # User feedback (conditional based on mode)
            if not production_mode and (
                not dry_run or interactive or logger.isEnabledFor(logging.INFO)
            ):
                step_emoji = {
                    1: "📥",
                    2: "🗄️",
                    3: "🛠️",
                    4: "🔍",
                    5: "✏️",
                    6: "🔍",
                    7: "📤",
                    8: "🧹",
                }
                emoji = step_emoji.get(step_num, "🔧")
                print(f"{emoji} Step {step_num}: {description}...", file=sys.stderr)

        # Step 1: Download fresh reference data
        log_step(1, "Downloading fresh reference data")

        station_config_dir = _get_station_config_dir()
        temp_dir = station_config_dir / "cache"

        fresh_download = _download_fresh_reference(metadata_type, {}, temp_dir)
        workflow_result["workflow_steps"].append(
            {
                "step": "fresh_download",
                "success": fresh_download["success"],
                "details": fresh_download,
            }
        )

        if not fresh_download["success"]:
            error_msg = f"Fresh download failed: {fresh_download.get('error')}"
            logger.error(error_msg, extra={"step": 1, "workflow": "safe_update"})
            workflow_result["errors"].append(error_msg)
            return workflow_result
        else:
            logger.info(
                "Fresh download completed successfully",
                extra={
                    "step": 1,
                    "workflow": "safe_update",
                    "file_size_mb": fresh_download.get("file_size", 0) / 1024 / 1024,
                    "changed": fresh_download.get("changed", False),
                },
            )

        # Step 2: Create backup if required
        backup_info = None
        if backup_required:
            log_step(2, "Creating backup")

            local_reference = station_config_dir / "station.info.sopac.apr05"
            if local_reference.exists():
                try:
                    backup_info = _create_versioned_backup(
                        local_reference,
                        metadata={"update_workflow": True, "stations": stations},
                    )
                    logger.info(
                        "Backup created successfully",
                        extra={
                            "step": 2,
                            "workflow": "safe_update",
                            "backup_id": backup_info["backup_id"],
                        },
                    )
                    workflow_result["workflow_steps"].append(
                        {
                            "step": "backup_creation",
                            "success": True,
                            "backup_id": backup_info["backup_id"],
                        }
                    )
                except Exception as e:
                    workflow_result["errors"].append(f"Backup creation failed: {e}")
                    if backup_required:
                        return workflow_result

        # Step 3: Create working copy
        log_step(3, "Creating working copy")

        work_dir = _get_work_dir(station_config_dir)
        work_info = _create_working_copy(
            fresh_download["temp_path"],
            work_dir,
            metadata={"stations_to_update": stations},
        )

        workflow_result["workflow_steps"].append(
            {
                "step": "working_copy",
                "success": True,
                "work_path": work_info["work_path"],
            }
        )

        # Step 4: Collect station updates
        print("🔍 Step 4: Collecting station data from TOS...", file=sys.stderr)

        station_updates = {}
        for station in stations:
            try:
                station_info = gpsqc.gps_metadata(
                    station, url, loglevel=logging.CRITICAL
                )
                if station_info:
                    tos_lines = _generate_station_lines_from_tos(station_info)
                    station_updates[station] = tos_lines
                    workflow_result["stations_processed"].append(station)
                    print(f"  ✓ {station}: {len(tos_lines)} lines", file=sys.stderr)
                else:
                    print(f"  ✗ {station}: No TOS data", file=sys.stderr)
                    workflow_result["errors"].append(f"No TOS data for {station}")
            except Exception as e:
                print(f"  ✗ {station}: {e}", file=sys.stderr)
                workflow_result["errors"].append(f"Error processing {station}: {e}")

        if not station_updates:
            workflow_result["errors"].append("No valid station updates collected")
            return workflow_result

        # Step 5: Apply updates to working copy
        print("✏️  Step 5: Applying updates to working copy...", file=sys.stderr)

        try:
            update_result = _apply_station_updates(
                work_info["work_path"], station_updates, work_info
            )
            workflow_result["workflow_steps"].append(
                {
                    "step": "apply_updates",
                    "success": update_result["success"],
                    "details": update_result,
                }
            )
        except Exception as e:
            workflow_result["errors"].append(f"Failed to apply updates: {e}")
            return workflow_result

        # Step 6: Verify changes
        print("🔍 Step 6: Verifying changes...", file=sys.stderr)

        try:
            verification = _verify_intended_changes(
                fresh_download["temp_path"],
                work_info["work_path"],
                list(station_updates.keys()),
            )

            workflow_result["workflow_steps"].append(
                {
                    "step": "verification",
                    "success": verification["success"],
                    "details": verification,
                }
            )

            # Generate and display change report
            change_report = _generate_change_report(
                verification, work_info, update_result
            )

            if dry_run or interactive:
                print("\n" + change_report, file=sys.stderr)

            if not verification["success"]:
                workflow_result["errors"].append("Change verification failed")
                return workflow_result

        except Exception as e:
            workflow_result["errors"].append(f"Change verification failed: {e}")
            return workflow_result

        # Step 7: Upload (or simulate)
        if update_mode:
            print("📤 Step 7: Uploading changes...", file=sys.stderr)

            if interactive and not dry_run:
                response = input("Proceed with upload? [y/N]: ")
                if response.lower() != "y":
                    print("Upload cancelled by user", file=sys.stderr)
                    workflow_result["success"] = True
                    workflow_result["cancelled"] = True
                    return workflow_result

            try:
                upload_result = _safe_upload_with_rollback(
                    work_info["work_path"],
                    {},
                    backup_info,
                    metadata_type=metadata_type,
                    dry_run=dry_run,
                )

                workflow_result["workflow_steps"].append(
                    {
                        "step": "upload",
                        "success": upload_result["success"],
                        "details": upload_result,
                    }
                )

                if upload_result["success"]:
                    workflow_result["stations_updated"] = list(station_updates.keys())
                    if not dry_run:
                        print(
                            "🎉 Update workflow completed successfully!",
                            file=sys.stderr,
                        )
                    else:
                        print("🧪 Dry run completed successfully!", file=sys.stderr)
                else:
                    workflow_result["errors"].append(
                        f"Upload failed: {upload_result.get('error')}"
                    )
                    return workflow_result

            except Exception as e:
                workflow_result["errors"].append(f"Upload step failed: {e}")
                return workflow_result

        # Step 8: Cleanup
        print("🧹 Step 8: Cleaning up temporary files...", file=sys.stderr)
        _cleanup_working_files(
            work_dir, max_age_hours=1
        )  # Clean up immediately for this workflow

        workflow_result["success"] = True

    except Exception as e:
        workflow_result["errors"].append(f"Workflow failed: {e}")
        logger.error(f"Safe update workflow failed: {e}")

    finally:
        # Always clean up temp files
        try:
            Path(fresh_download["temp_path"]).unlink(missing_ok=True)
        except:
            pass

    return workflow_result


# ============================================================================
# SAFE UPDATE SYSTEM - CLI Helper Functions
# ============================================================================


def _list_available_backups():
    """List available backups for rollback."""
    station_config_dir = _get_station_config_dir()
    backup_dir = _get_backup_dir(station_config_dir)

    backups = _list_backups(backup_dir)

    if not backups:
        print("No backups available.", file=sys.stderr)
        return

    print("\nAvailable backups:", file=sys.stderr)
    print("-" * 80, file=sys.stderr)
    print(
        f"{'Backup ID':<20} {'Created':<20} {'Size (MB)':<10} {'Original File'}",
        file=sys.stderr,
    )
    print("-" * 80, file=sys.stderr)

    for backup in backups[:10]:  # Show last 10 backups
        from pathlib import Path

        Path(backup["backup_path"])
        size_mb = (
            backup["original_size"] / (1024 * 1024) if backup["original_size"] else 0
        )
        created = backup["created_at"][:19]  # Truncate timestamp
        original = Path(backup["original_file"]).name

        print(
            f"{backup['backup_id']:<20} {created:<20} {size_mb:<10.1f} {original}",
            file=sys.stderr,
        )


def _handle_rollback_command(backup_id, metadata_type):
    """Handle rollback to specified backup."""
    if not metadata_type:
        print("Error: --type is required for rollback operations", file=sys.stderr)
        return

    try:
        print(f"Rolling back to backup: {backup_id}", file=sys.stderr)

        success = _restore_from_backup(backup_id)

        if success:
            print(f"✓ Rollback successful: {backup_id}", file=sys.stderr)
            print(
                "Note: This restored the local file only. Remote file is unchanged.",
                file=sys.stderr,
            )
        else:
            print(f"✗ Rollback failed: {backup_id}", file=sys.stderr)

    except Exception as e:
        print(f"✗ Rollback error: {e}", file=sys.stderr)


def _handle_verify_only_command(metadata_type, stations):
    """Handle verify-only mode - check file integrity without changes."""
    if not metadata_type:
        print("Error: --type is required for verification", file=sys.stderr)
        return

    if "gamit-station-info" in metadata_type:
        station_config_dir = _get_station_config_dir()
        local_file = station_config_dir / "station.info.sopac.apr05"

        print("=" * 80, file=sys.stderr)
        print("GAMIT STATION INFO FILE INTEGRITY VERIFICATION", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        print(f"File being verified: {local_file}", file=sys.stderr)
        print(f"Metadata type: {metadata_type}", file=sys.stderr)

        if not local_file.exists():
            print(f"\n❌ ERROR: File does not exist at {local_file}", file=sys.stderr)
            print("\nTo download the file, run:", file=sys.stderr)
            print("  tosGPS syncMeta --type gamit-station-info RHOF", file=sys.stderr)
            return

        # Show file modification time
        import time

        mod_time = time.ctime(local_file.stat().st_mtime)
        print(f"Last modified: {mod_time}", file=sys.stderr)
        print("", file=sys.stderr)

        print("🔍 Running integrity checks...", file=sys.stderr)

        integrity_result = _validate_file_integrity_comprehensive(local_file)

        print("\nFILE INTEGRITY REPORT", file=sys.stderr)
        print("-" * 40, file=sys.stderr)
        print(f"📁 File path: {local_file}", file=sys.stderr)
        print(
            f"📊 File exists: {'✓ YES' if integrity_result['file_exists'] else '❌ NO'}",
            file=sys.stderr,
        )
        print(
            f"📖 File readable: {'✓ YES' if integrity_result['file_readable'] else '❌ NO'}",
            file=sys.stderr,
        )
        print(
            f"📋 GAMIT format valid: {'✓ YES' if integrity_result['format_valid'] else '❌ NO'}",
            file=sys.stderr,
        )
        print(
            f"📏 File size: {integrity_result['size_bytes'] / 1024 / 1024:.1f} MB ({integrity_result['size_bytes']:,} bytes)",
            file=sys.stderr,
        )
        print(f"📄 Total lines: {integrity_result['line_count']:,}", file=sys.stderr)
        print(
            f"📡 Station entries: {integrity_result['station_count']:,}",
            file=sys.stderr,
        )

        if integrity_result["checksum"]:
            print(
                f"🔐 SHA256 checksum: {integrity_result['checksum'][:16]}...{integrity_result['checksum'][-16:]}",
                file=sys.stderr,
            )
        else:
            print("🔐 SHA256 checksum: Not available", file=sys.stderr)

        if integrity_result["validation_errors"]:
            print(
                f"\n⚠️  VALIDATION ISSUES FOUND ({len(integrity_result['validation_errors'])}):",
                file=sys.stderr,
            )
            for i, error in enumerate(
                integrity_result["validation_errors"][:10], 1
            ):  # Show max 10 errors
                print(f"  {i}. {error}", file=sys.stderr)
            if len(integrity_result["validation_errors"]) > 10:
                print(
                    f"  ... and {len(integrity_result['validation_errors']) - 10} more issues",
                    file=sys.stderr,
                )

        # Overall status with clear indication
        status = "✅ PASSED" if integrity_result["success"] else "❌ FAILED"
        print(f"\n{'='*20} VERIFICATION RESULT {'='*20}", file=sys.stderr)
        print(f"Status: {status}", file=sys.stderr)

        if integrity_result["success"]:
            print(
                "The file appears to be valid and properly formatted.", file=sys.stderr
            )
        else:
            print("The file has integrity issues that need attention.", file=sys.stderr)

        print("=" * 80, file=sys.stderr)


if __name__ == "__main__":
    main()
