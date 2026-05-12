"""
Core station data models and business logic.

This module provides data structures and functions for GPS station management,
session handling, and metadata processing.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..api.tos_client import TOSClient
from ..utils.logging import get_logger


class Station:
    """
    GPS Station data model.

    Represents a GPS station with its metadata, location, contacts, and device history.
    """

    def __init__(self, data: Dict[str, Any]):
        """
        Initialize station from TOS data.

        Args:
            data: Station data dictionary from TOS API
        """
        self.data = data
        self._device_history: Optional[List[Dict[str, Any]]] = None

    @property
    def marker(self) -> str:
        """Station marker (4-letter identifier)."""
        return self.data.get("marker", "")

    @property
    def name(self) -> str:
        """Station name."""
        return self.data.get("name", "")

    @property
    def lat(self) -> float:
        """Station latitude in decimal degrees."""
        return float(self.data.get("lat", 0.0))

    @property
    def lon(self) -> float:
        """Station longitude in decimal degrees."""
        return float(self.data.get("lon", 0.0))

    @property
    def altitude(self) -> float:
        """Station altitude in meters."""
        return float(self.data.get("altitude", 0.0))

    @property
    def domes_number(self) -> str:
        """IERS DOMES number."""
        return self.data.get("iers_domes_number", "")

    @property
    def start_date(self) -> Optional[str]:
        """Station start date."""
        return self.data.get("date_start")

    @property
    def device_history(self) -> List[Dict[str, Any]]:
        """Station device history."""
        return self.data.get("device_history", [])

    @property
    def contacts(self) -> Dict[str, Any]:
        """Station contact information."""
        return self.data.get("contact", {})

    def get_session(self, session_nr: int) -> Optional[Dict[str, Any]]:
        """
        Get a specific session by number.

        Args:
            session_nr: Session number (0-indexed)

        Returns:
            Session dictionary or None if not found
        """
        if 0 <= session_nr < len(self.device_history):
            session = {
                key: value
                for key, value in self.data.items()
                if key != "device_history"
            }
            session["device_history"] = self.device_history[session_nr]
            return session
        return None

    def get_sessions_list(
        self, date_format: str = "%Y-%m-%d %H:%M:%S"
    ) -> List[Dict[str, Any]]:
        """
        Get formatted list of all sessions.

        Args:
            date_format: Date format string

        Returns:
            List of session dictionaries with formatted dates
        """
        sessions = []

        for item in self.device_history:
            session = {}

            if date_format:
                time_from = item.get("time_from")
                time_to = item.get("time_to")

                session["time_from"] = (
                    time_from.strftime(date_format) if time_from else "None"
                )
                session["time_to"] = (
                    time_to.strftime(date_format) if time_to else "None"
                )
            else:
                session["time_from"] = item.get("time_from")
                session["time_to"] = item.get("time_to")

            # Add device information
            for device_type in ["gnss_receiver", "antenna", "radome", "monument"]:
                if device_type in item:
                    session[device_type] = item[device_type]

            sessions.append(session)

        return sessions

    def get_radome(
        self, date_from: datetime, date_to: datetime, loglevel: int = logging.WARNING
    ) -> tuple[str, str]:
        """
        Get radome information for a given time interval.

        Args:
            date_from: Start date
            date_to: End date
            loglevel: Logging level

        Returns:
            Tuple of (radome_model, radome_serial)
        """
        logger = get_logger(__name__, loglevel)

        # Default radome is NONE
        radome_model = "NONE"
        radome_serial = ""

        for session in self.device_history:
            session_start = session.get("time_from")
            session_end = session.get("time_to")

            if not session_start:
                continue

            # Check if dates overlap with session
            if session_end is None or (
                date_from <= session_end and date_to >= session_start
            ):
                radome_info = session.get("radome", {})
                if radome_info:
                    radome_model = radome_info.get("model", "NONE")
                    radome_serial = radome_info.get("serial_number", "")
                    logger.debug(
                        f"Found radome: {radome_model}, serial: {radome_serial}"
                    )
                    break

        return radome_model, radome_serial

    def get_monument_height(
        self, date_from: datetime, date_to: datetime, loglevel: int = logging.WARNING
    ) -> float:
        """
        Get monument height for a given time interval.

        Args:
            date_from: Start date
            date_to: End date
            loglevel: Logging level

        Returns:
            Monument height in meters
        """
        logger = get_logger(__name__, loglevel)

        monument_height = 0.0

        for session in self.device_history:
            session_start = session.get("time_from")
            session_end = session.get("time_to")

            if not session_start:
                continue

            # Check if dates overlap with session
            if session_end is None or (
                date_from <= session_end and date_to >= session_start
            ):
                monument_info = session.get("monument", {})
                if monument_info:
                    monument_height = float(monument_info.get("monument_height", 0.0))
                    logger.debug(f"Found monument height: {monument_height}")
                    break

        return monument_height


def get_station_list(
    subsets: Optional[Dict[str, Any]] = None,
    tos_client: Optional[TOSClient] = None,
    loglevel: int = logging.WARNING,
) -> List[Dict[str, Any]]:
    """
    Get list of GPS stations from TOS.

    Args:
        subsets: Filter criteria
        tos_client: TOS client instance
        loglevel: Logging level

    Returns:
        List of station dictionaries
    """
    if tos_client is None:
        tos_client = TOSClient(loglevel=loglevel)

    # For now, return empty list - this would need to be implemented
    # based on specific search criteria
    logger = get_logger(__name__, loglevel)
    logger.warning("get_station_list not fully implemented yet")
    return []


def generate_file_list(
    station: Station,
    base_dir: Union[str, Path],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    freq: str = "15s_24hr",
    raw_dir: str = "rinex",
    file_format: str = "#Rin2",
    extension: str = "D.Z",
    loglevel: int = logging.WARNING,
) -> List[Path]:
    """
    Generate list of potential RINEX files for a station.

    Args:
        station: Station instance
        base_dir: Base directory path
        start: Start date (optional)
        end: End date (optional)
        freq: Frequency directory
        raw_dir: Raw data directory
        file_format: File format
        extension: File extension
        loglevel: Logging level

    Returns:
        List of potential file paths
    """
    logger = get_logger(__name__, loglevel)

    files_list = []
    marker = station.marker.upper()

    # This is a simplified version - the full implementation would
    # generate file paths based on the station's device history sessions
    logger.info(f"Generating file list for station {marker}")

    base_path = Path(base_dir)

    for session in station.device_history:
        session_start = session.get("time_from")
        session.get("time_to")

        if session_start:
            # Generate file path pattern based on session dates
            year_path = base_path / str(session_start.year)
            station_path = year_path / marker.lower() / freq / raw_dir

            # This is a placeholder - real implementation would generate
            # specific file names based on date ranges and formats
            potential_file = (
                station_path
                / f"{marker.lower()}{session_start.strftime('%j0.%y')}{extension}"
            )
            files_list.append(potential_file)

    return files_list
