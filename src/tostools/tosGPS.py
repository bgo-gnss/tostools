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


def generate_igs_sitelog_filename(
    station_marker: str,
    country_code: str = "ISL",
    monument_number: str = "00",
    include_date: bool = False,
    base_dir: str = ".",
    custom_date: str = None,
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

    Returns:
        Tuple of (full_path, filename_only)
    """
    import os
    from datetime import datetime

    station_id = f"{station_marker.upper()}{monument_number}{country_code.upper()}"
    station_dir = os.path.join(base_dir, station_id)

    if include_date:
        if custom_date:
            date_str = custom_date  # Use provided date (YYYYMMDD format)
        else:
            date_str = datetime.now().strftime("%Y%m%d")
        filename = f"{station_id.lower()}_{date_str}.log"
    else:
        filename = f"{station_id}.log"

    full_path = os.path.join(station_dir, filename)
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
        log_level_explicitly_set = any(arg in sys.argv for arg in ['--log-level', '--debug-all'])
        if console_level == logging.INFO and not args.debug_all and not log_level_explicitly_set:
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
  tosGPS sync-meta --type gamit-stations RHOF
  
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
        help="Include creation date in filename (e.g., rhof00isl_20250825.log)",
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
  tosGPS syncMeta --type gamit-stations RHOF
  
  # Update with confirmation prompt
  tosGPS syncMeta --type gamit-stations RHOF --update
  
  # Batch update without detailed comparison
  tosGPS syncMeta --type gamit-stations RHOF REYK HOFN --update --no-compare
  
  # Multi-type operations
  tosGPS syncMeta --type gamit-stations,igs-logs RHOF
  
  # Discovery and status
  tosGPS syncMeta --list-types          # Show available metadata types
  tosGPS syncMeta --list-servers        # Show configured servers
  tosGPS syncMeta --status              # Show sync status of all types
  
  # Advanced options
  tosGPS syncMeta --type gamit-stations --force-server okada RHOF    # Force specific server
  tosGPS syncMeta --type gamit-stations RHOF --force-download  # Bypass cache
  tosGPS syncMeta --type all --all-stations                   # Check all TOS stations
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
        help="Comma-separated list of metadata types (e.g., gamit-stations,igs-logs) or 'all'",
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
        station_info = gpsqc.gps_metadata(sta, url, loglevel=log_level.value)

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
    tos_client = TOSClient(base_url=url)  # Use default, respect centralized logging

    all_comparisons = []

    for station in stations:
        print(f"\n=== Processing station {station} ===", file=sys.stderr)

        # Get station metadata using legacy system (more reliable for validation)
        try:
            station_data = gpsqc.gps_metadata(station, url, loglevel=log_level.value)
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
            current_session = device_sessions[-1]

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
            # Get complete station metadata with proper device sessions (like legacy system)
            complete_station_data = tos_client.get_complete_station_metadata(station)
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
            elif args.auto_filename:
                # Generate IGS-compliant filename with new features
                full_path, filename = generate_igs_sitelog_filename(
                    station,
                    include_date=args.date_in_name,
                    base_dir=args.dir,
                    custom_date=args.custom_date,
                )

                # Create directory if it doesn't exist
                os.makedirs(os.path.dirname(full_path), exist_ok=True)

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
                print(f"Using IGS filename: {filename}", file=sys.stderr)
                if previous_log:
                    print(f"Previous log: {previous_log}", file=sys.stderr)
                    print("Report type: UPDATE", file=sys.stderr)
                    if modified_sections:
                        print(
                            f"Modified sections: {modified_sections}", file=sys.stderr
                        )
                else:
                    print("Report type: NEW", file=sys.stderr)

            if output_file:
                # Write to file
                try:
                    with open(output_file, "w", encoding="utf-8") as f:
                        f.write(output_content)
                    # Send success message to stderr to keep stdout clean
                    print(f"✓ Site log saved to {output_file}", file=sys.stderr)

                    # Also output to stdout when using auto-filename (dual output)
                    if args.auto_filename:
                        print(output_content)  # Also send to stdout for piping

                except Exception as e:
                    print(f"Error writing site log: {e}", file=sys.stderr)
                    # Still output to stdout if file write failed
                    print(output_content)
            else:
                # Output to stdout only (pipe-friendly)
                print(output_content)  # Clean output to stdout
                # Optional: Send completion notice to stderr (only for multiple stations)
                if len(stations) > 1:
                    print(f"✓ Site log for {station} completed", file=sys.stderr)

        except Exception as e:
            print(f"Error generating site log for {station}: {e}", file=sys.stderr)


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
        print(f"ERROR: Station info file not found at: {station_info_path}", file=sys.stderr)
        print(f"Expected directory structure: <project_root>/data/station_config/", file=sys.stderr)
        print(f"To fetch the file, run: tosGPS sync-meta --type station-info", file=sys.stderr)
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
        col_end = min(end, len(line))

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
        ("Dome", 180, 185),  # "NONE   " vs "SPKE   "
        ("Antenna SN", 186, -1),  # "antenna-eldc-2020012" vs "0000" (to end of line)
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
                    "gamit-stations": {
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
    # This would implement the caching logic similar to the existing fetch functionality
    # For now, use the existing reference data parsing as a fallback
    if metadata_type == "gamit-stations":
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

    if metadata_type == "gamit-stations":
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


if __name__ == "__main__":
    main()
