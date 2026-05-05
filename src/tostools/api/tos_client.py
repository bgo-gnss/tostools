"""
TOS (Technical Operations System) API client.

This module provides functions to interact with the TOS REST API for GPS stations
metadata retrieval and management.
"""

import logging
from typing import Any, Dict, List, Optional

import requests

from ..utils.logging import get_logger

# TOS API Configuration
DEFAULT_TOS_URL = "https://vi-api.vedur.is/tos/v1"
DEFAULT_TIMEOUT = 10


class TOSClient:
    """Client for interacting with TOS API."""

    def __init__(
        self,
        base_url: str = DEFAULT_TOS_URL,
        timeout: int = DEFAULT_TIMEOUT,
        loglevel: int = logging.WARNING,
    ):
        """
        Initialize TOS client.

        Args:
            base_url: Base URL for TOS API
            timeout: Request timeout in seconds
            loglevel: Logging level
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.logger = get_logger(__name__, loglevel)

    def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """
        Make HTTP request to TOS API.

        Args:
            endpoint: API endpoint (without base URL)
            method: HTTP method
            data: Request body data
            params: URL parameters

        Returns:
            Response data as dictionary, or None if error
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        self.logger.info(f'Sending request "{url}"')

        try:
            if method.upper() == "POST":
                # Use the same format as legacy system
                import json

                response = requests.post(
                    url,
                    data=json.dumps(data),
                    headers={"Content-Type": "application/json"},
                    params=params,
                    timeout=self.timeout,
                )
            else:
                response = requests.get(url, params=params, timeout=self.timeout)

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Request failed: {e}")
            return None
        except ValueError as e:
            self.logger.error(f"Invalid JSON response: {e}")
            return None

    def search_stations(
        self,
        station_identifier: str,
        code: str = "marker",
        domains: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search for stations in TOS database using legacy-compatible approach.

        Args:
            station_identifier: Station identifier to search for
            code: Search code type (marker, name, etc.)
            domains: Domain filter (e.g., "geophysical")

        Returns:
            List of matching stations
        """
        # Use the same approach as legacy system
        if domains is None:
            domain_list = ["geophysical", "meteorological", "hydrological"]
        else:
            domain_list = [domains] if isinstance(domains, str) else domains

        # Prepare station identifiers (like legacy system)
        station_identifiers = [station_identifier]

        # Always try lowercase first (TOS API prefers lowercase)
        if not station_identifier.islower():
            station_identifiers = [station_identifier.lower()] + station_identifiers

        stations = []
        for station_id in station_identifiers:
            for domain in domain_list:
                # POST body exactly like legacy system
                body = {"code": code, "value": station_id}

                # Entity type based on domain
                entity_type = (
                    "platform" if domain == "remote_sensing_platform" else "station"
                )

                # Endpoint like legacy system
                endpoint = f"/entity/search/{entity_type}/{domain}/"

                # Make POST request
                result = self._make_request(endpoint, method="POST", data=body)

                if result:
                    # Handle both list and dict responses
                    if isinstance(result, list):
                        found_stations = result
                    elif isinstance(result, dict) and "objects" in result:
                        found_stations = result["objects"]
                    else:
                        found_stations = []

                    stations.extend(found_stations)
                    self.logger.info(
                        f"Found {len(found_stations)} stations for '{station_id}' in {domain}"
                    )

        # Remove duplicates based on id_entity
        unique_stations = []
        seen_ids = set()
        for station in stations:
            station_id = station.get("id_entity")
            if station_id and station_id not in seen_ids:
                unique_stations.append(station)
                seen_ids.add(station_id)

        self.logger.info(f"Total unique stations found: {len(unique_stations)}")
        return unique_stations

    def get_station_metadata(
        self, station_identifier: str, domains: str = "geophysical"
    ) -> tuple[Optional[Dict], Optional[Dict]]:
        """
        Get complete station metadata including device history.

        Args:
            station_identifier: Station identifier
            domains: Domain filter

        Returns:
            Tuple of (station_data, device_history) or (None, None) if not found
        """
        stations = self.search_stations(station_identifier, domains=domains)

        if not stations:
            return None, None

        station = stations[0]
        station_id = station["id_entity"]

        self.logger.info(f"station {station_identifier} id_entity: {station_id}")

        # Get device history
        device_history = self._make_request(f"history/entity/{station_id}/")

        if device_history:
            return station, device_history
        else:
            return station, None

    def get_complete_station_metadata(
        self, station_identifier: str, domains: str = "geophysical"
    ) -> Optional[Dict]:
        """
        Get complete station metadata processed exactly like legacy gps_metadata() function.

        This replicates the complete legacy workflow:
        1. Search for station
        2. Get device history connections
        3. Get detailed device information for each connection
        4. Process into structured device sessions
        5. Organize into time-based device history

        Args:
            station_identifier: Station identifier
            domains: Domain filter

        Returns:
            Complete station dictionary with device_history, or None if not found
        """
        # Step 1: Get basic station info and device history
        station_data, device_history = self.get_station_metadata(
            station_identifier, domains
        )

        if not station_data or not device_history:
            self.logger.warning(f"No station data found for {station_identifier}")
            return None

        # Step 2: Process device sessions (makes additional API calls)
        device_sessions = self.get_device_sessions(device_history)

        # Step 3: Sort by date (legacy behavior)
        device_sessions.sort(key=lambda d: d["device"]["date_from"])

        self.logger.info(
            f"Found {len(device_sessions)} device sessions for {station_identifier}"
        )

        # Step 4: Process into legacy format device history
        processed_history = self._process_device_history(device_sessions)

        # Step 5: Create final station structure (matching legacy format)
        final_station = self._create_legacy_station_structure(
            station_data, device_history, processed_history
        )

        return final_station

    def _process_device_history(self, device_sessions: List[Dict]) -> List[Dict]:
        """
        Process device sessions into legacy-compatible device history format.
        Replicates get_device_history() logic.
        """
        from ..legacy.gps_metadata_qc import get_device_history

        # Use legacy function to maintain exact compatibility
        return get_device_history(device_sessions)

    def _create_legacy_station_structure(
        self, station_data: Dict, device_history: Dict, processed_history: List[Dict]
    ) -> Dict:
        """Create station structure matching legacy format."""
        # Extract key station attributes from device_history (legacy pattern)
        station = {}

        # Basic station info with proper type conversion
        if "attributes" in device_history:
            for attr in device_history["attributes"]:
                code = attr["code"]
                value = attr["value"]
                if attr["date_to"] is None:  # Current value
                    if code in ["marker", "name"]:
                        station[code] = value
                    elif code in ["lat", "lon", "altitude"]:
                        # Convert coordinates to float
                        try:
                            station[code] = float(value) if value else 0.0
                        except (ValueError, TypeError):
                            station[code] = 0.0
                    elif code in [
                        "geological_characteristic",
                        "bedrock_type",
                        "bedrock_condition",
                    ]:
                        station[code] = value
                    elif code == "is_near_fault_zones":
                        station[code] = value
                    elif code == "iers_domes_number":
                        station[code] = value
                    elif code == "date_start":
                        station[code] = value
                    elif code == "in_network_epos":
                        station[code] = value

        # Add processed device history
        station["device_history"] = processed_history

        # Expose station entity ID so callers can resolve TOS write targets
        if "id_entity" in station_data:
            station["id_entity"] = station_data["id_entity"]

        # Add contact information (always include, even if empty)
        station["contact"] = {}
        if "id_entity" in station_data:
            contacts = self.get_contacts(station_data["id_entity"])
            if contacts:
                station["contact"] = self._process_contacts(contacts)

        return station

    def _process_contacts(self, contacts: List[Dict]) -> Dict:
        """Process contact information into legacy format."""
        contact_info = {}

        for contact in contacts:
            role = contact.get("role_is", "").lower()
            if "eigandi" in role:  # Owner
                contact_info["owner"] = contact
            elif "rekstraraðili" in role:  # Operator
                contact_info["operator"] = contact
            else:
                contact_info["contact"] = contact

        return contact_info

    def get_contacts(self, entity_id: int) -> List[Dict[str, Any]]:
        """
        Get contact information for an entity.

        Args:
            entity_id: Entity ID

        Returns:
            List of contacts
        """
        result = self._make_request(f"entity_contacts/{entity_id}/")
        return result if result else []

    def get_device_sessions(
        self, device_history: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Process device history into organized sessions.

        This replicates the legacy TOS API interaction pattern:
        1. For each connection in children_connections
        2. Make additional API call to get device details
        3. Process device attribute history
        4. Structure into sessions

        Args:
            device_history: Raw device history from TOS

        Returns:
            List of device sessions with organized data
        """
        device_sessions = []
        devices_used = ["gnss_receiver", "antenna", "radome", "monument"]

        for connection in device_history.get("children_connections", []):
            # Skip zero-duration sessions
            if connection["time_from"] == connection["time_to"]:
                self.logger.debug(
                    f"Session start is the same as session end: {connection['time_from']}, end: {connection['time_to']}"
                )
                continue

            # Make additional API call for device details (legacy pattern)
            id_entity_child = connection["id_entity_child"]
            device = self._make_request(f"history/entity/{id_entity_child}/")

            if not device:
                self.logger.error(
                    f"Failed to get device details for entity {id_entity_child}"
                )
                continue

            # Only process devices we care about
            if device["code_entity_subtype"] not in devices_used:
                self.logger.debug(
                    f"Device type {device['code_entity_subtype']} not in devices_used: {devices_used}"
                )
                continue

            self.logger.debug(
                f"Processing device {device['code_entity_subtype']} for connection {connection['time_from']} - {connection['time_to']}"
            )

            # Get device attribute history (simplified version of legacy logic)
            attribute_history = self._get_device_attribute_history(
                device, connection["time_from"], connection["time_to"]
            )

            # Create sessions for each attribute period
            for attribute in attribute_history:
                session = connection.copy()
                session["device"] = attribute
                device_sessions.append(session)

        return device_sessions

    def _get_device_attribute_history(
        self, device: Dict[str, Any], session_start: str, session_end: str
    ) -> List[Dict[str, Any]]:
        """
        Extract device attribute history for a specific time period.

        This should use the legacy device_attribute_history function for exact compatibility.
        For now, return the device as-is with time stamps added.
        """
        from ..legacy.gps_metadata_qc import device_attribute_history

        # Use legacy function to process the device properly
        try:
            return device_attribute_history(
                device, session_start, session_end, logging.CRITICAL
            )
        except Exception as e:
            self.logger.warning(f"Legacy device_attribute_history failed: {e}")
            # Fallback: return device as-is with date stamps
            device_copy = device.copy()
            device_copy["date_from"] = session_start
            device_copy["date_to"] = session_end
            return [device_copy]


# Convenience functions for backward compatibility
def search_station(
    station_identifier: str,
    code: str = "marker",
    url_rest: str = DEFAULT_TOS_URL,
    domains: Optional[str] = None,
    loglevel: int = logging.WARNING,
) -> List[Dict[str, Any]]:
    """
    Search for stations using TOS API.

    Args:
        station_identifier: Station identifier
        code: Search code type
        url_rest: TOS API base URL
        domains: Domain filter
        loglevel: Logging level

    Returns:
        List of matching stations
    """
    client = TOSClient(url_rest, loglevel=loglevel)
    return client.search_stations(station_identifier, code, domains)


def get_station_metadata(
    station_identifier: str,
    url_rest: str = DEFAULT_TOS_URL,
    loglevel: int = logging.WARNING,
) -> tuple[Optional[Dict], Optional[Dict]]:
    """
    Get station metadata from TOS API.

    Args:
        station_identifier: Station identifier
        url_rest: TOS API base URL
        loglevel: Logging level

    Returns:
        Tuple of (station_data, device_history)
    """
    client = TOSClient(url_rest, loglevel=loglevel)
    return client.get_station_metadata(station_identifier)


def get_contacts(
    entity_id: int, url_rest: str = DEFAULT_TOS_URL, loglevel: int = logging.WARNING
) -> List[Dict[str, Any]]:
    """
    Get contacts for an entity.

    Args:
        entity_id: Entity ID
        url_rest: TOS API base URL
        loglevel: Logging level

    Returns:
        List of contacts
    """
    client = TOSClient(url_rest, loglevel=loglevel)
    return client.get_contacts(entity_id)
