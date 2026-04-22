#!/usr/bin/env python3
"""
Export all GNSS stations with coordinates and current equipment information.

This script queries the TOS API for all stations and extracts:
- Station identifier (marker)
- Station name
- Coordinates (lat, lon)
- Current receiver type
- Current antenna type

Output formats: CSV, JSON, or Excel
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tostools.legacy import gps_metadata_qc as gpsqc
from tostools.legacy.gps_metadata_functions import getStationList


def get_current_equipment(station_marker, url_rest_tos, loglevel=logging.WARNING):
    """
    Get current equipment for a station.

    Returns dict with current receiver, antenna, and radome info.
    """
    try:
        station, devices_history = gpsqc.get_station_metadata(
            station_marker, url_rest_tos, loglevel=loglevel
        )
        device_sessions = gpsqc.get_device_sessions(
            devices_history, url_rest_tos, loglevel=loglevel
        )

        # Find current equipment (time_to is None)
        current_receiver = None
        current_antenna = None
        current_radome = None

        for session in device_sessions:
            if session.get("time_to") is None:  # Current equipment
                device_type = session["device"]["code_entity_subtype"]

                if device_type == "gnss_receiver":
                    current_receiver = {
                        "model": session["device"].get("model", ""),
                        "serial_number": session["device"].get("serial_number", ""),
                        "firmware": session["device"].get("firmware_version", ""),
                    }
                elif device_type == "antenna":
                    current_antenna = {
                        "model": session["device"].get("model", ""),
                        "serial_number": session["device"].get("serial_number", ""),
                        "height": session["device"].get("antenna_height", 0.0),
                    }
                elif device_type == "radome":
                    current_radome = {
                        "model": session["device"].get("model", "NONE"),
                    }

        return {
            "receiver": current_receiver,
            "antenna": current_antenna,
            "radome": current_radome,
        }
    except Exception as e:
        logging.warning(f"Failed to get equipment for {station_marker}: {e}")
        return {
            "receiver": None,
            "antenna": None,
            "radome": None,
        }


def main():
    parser = argparse.ArgumentParser(
        description="Export GNSS station coordinates and current equipment"
    )
    parser.add_argument(
        "-o", "--output",
        required=True,
        help="Output file path (extension determines format: .csv, .json, .xlsx)"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="WARNING",
        help="Logging level"
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include stations with no current equipment"
    )
    parser.add_argument(
        "--url",
        default="https://vi-api.vedur.is/tos/v1",
        help="TOS API base URL (default: https://vi-api.vedur.is/tos/v1)"
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="[%(levelname)s] %(message)s",
        stream=sys.stderr
    )

    print("Fetching station list from TOS API...", file=sys.stderr)
    station_list = getStationList()

    print(f"Found {len(station_list)} stations. Querying equipment information...", file=sys.stderr)

    results = []
    url_rest_tos = args.url

    for i, station in enumerate(station_list, 1):
        marker = station["marker"]
        print(f"[{i}/{len(station_list)}] Processing {marker}...", file=sys.stderr)

        equipment = get_current_equipment(marker, url_rest_tos, loglevel=logging.ERROR)

        # Skip stations with no current equipment if not including inactive
        if not args.include_inactive:
            if equipment["receiver"] is None and equipment["antenna"] is None:
                print(f"  Skipping {marker} (no current equipment)", file=sys.stderr)
                continue

        result = {
            "marker": marker,
            "name": station.get("name", ""),
            "latitude": station.get("lat"),
            "longitude": station.get("lon"),
            "receiver_type": equipment["receiver"]["model"] if equipment["receiver"] else "",
            "antenna_type": equipment["antenna"]["model"] if equipment["antenna"] else "",
        }

        results.append(result)

    # Create DataFrame
    df = pd.DataFrame(results)

    # Determine output format from extension
    output_path = Path(args.output)
    output_format = output_path.suffix.lower()

    print(f"\nWriting {len(results)} stations to {output_path}...", file=sys.stderr)

    if output_format == ".csv":
        df.to_csv(output_path, index=False)
    elif output_format == ".json":
        df.to_json(output_path, orient="records", indent=2)
    elif output_format in [".xlsx", ".xls"]:
        df.to_excel(output_path, index=False, engine="openpyxl")
    else:
        print(f"Error: Unsupported output format '{output_format}'", file=sys.stderr)
        print("Supported formats: .csv, .json, .xlsx", file=sys.stderr)
        sys.exit(1)

    print(f"Done! Exported {len(results)} stations.", file=sys.stderr)


if __name__ == "__main__":
    main()
