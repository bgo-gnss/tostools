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

        self.logger.info(
            f"Found {len(device_sessions)} device sessions for {station_identifier}"
        )

        # Step 3: Build station history from connection periods (bypasses legacy
        # pairing algorithm which breaks when start/end counts coincide).
        built_history = self._build_history_from_connections(device_sessions)

        # Step 4: Create final station structure
        final_station = self._create_legacy_station_structure(
            station_data, device_history, built_history
        )

        return final_station

    def _build_history_from_connections(
        self, device_sessions: List[Dict]
    ) -> List[Dict]:
        """Build device history by grouping on connection (time_from, time_to).

        The legacy ``get_device_history()`` pairs sorted start/end dates across
        all device types, which breaks for long-lived stations where many dates
        coincide (e.g. an antenna connection ending on the same date a new
        receiver connection starts).  This method uses the connection's own
        ``time_from``/``time_to`` fields — set by TOS when the device was
        physically attached — so the open/closed status is always correct.
        """
        from datetime import datetime

        from ..legacy.gps_metadata_qc import device_structure

        # Map (time_from, time_to) → {subtype: device_dict}
        periods: Dict[Any, Dict[str, Any]] = {}

        for session in device_sessions:
            time_from = session.get("time_from")
            time_to = session.get("time_to")
            device = session["device"]
            subtype = device.get("code_entity_subtype")
            if not subtype:
                continue
            key = (time_from, time_to)
            if key not in periods:
                periods[key] = {}
            # Prefer the entry whose attribute period is still open (date_to=None).
            existing = periods[key].get(subtype)
            if existing is None or device.get("date_to") is None:
                try:
                    periods[key][subtype] = device_structure(device.copy())
                except (KeyError, TypeError):
                    pass

        result = []
        for (time_from, time_to), devices in sorted(
            periods.items(), key=lambda x: x[0][0] or ""
        ):
            if not devices:
                continue
            session_dict: Dict[str, Any] = {
                "time_from": (
                    datetime.strptime(time_from, "%Y-%m-%dT%H:%M:%S")
                    if time_from
                    else None
                ),
                "time_to": (
                    datetime.strptime(time_to, "%Y-%m-%dT%H:%M:%S") if time_to else None
                ),
            }
            session_dict.update(devices)
            result.append(session_dict)

        return result

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

    def get_contact(self, id_contact: int) -> Optional[Dict[str, Any]]:
        """Fetch a single contact record by its ``id_contact``.

        Contacts live in their own id namespace, distinct from
        ``id_entity``. Wraps ``GET /contact/{id_contact}/``. Returns
        the contact dict or ``None`` on lookup failure (404, malformed
        response). The endpoint shape mirrors what
        :meth:`get_contacts` returns one element of — same fields:
        ``id_contact``, ``role`` / ``role_is``, ``name`` /
        ``organization``, ``phone_primary``, ``address``, etc.
        """
        result = self._make_request(f"contact/{id_contact}/")
        if not result or not isinstance(result, dict):
            return None
        return result

    def list_all_contacts(self) -> List[Dict[str, Any]]:
        """Fetch every contact record in TOS.

        Wraps ``GET /contacts/`` (plural — distinct from singular
        ``/contact/{id}/`` for one record). Returns the full contact
        list with the same row shape as :meth:`get_contact`. Empty
        list on lookup failure or unexpected payload, so callers can
        treat "no contacts" and "endpoint error" alike for inspection
        flows.
        """
        result = self._make_request("contacts/")
        if not isinstance(result, list):
            return []
        return result

    def get_entity_history(self, id_entity: int) -> Optional[Dict[str, Any]]:
        """Return the full history dict for an entity (read-only, no auth).

        Calls ``/history/entity/<id>/`` and returns the response. For a station
        entity the dict carries ``attributes`` (all periods) and
        ``children_connections`` (one entry per child join). For a device
        entity it carries ``attributes`` plus ``id_entity_parent`` (the most
        recent parent); device-side ``parents_connections`` is not exposed by
        TOS.
        """
        return self._make_request(f"/history/entity/{id_entity}/")

    def get_parent_history(self, id_child: int) -> List[Dict[str, Any]]:
        """Return every parent connection of an entity, open + closed.

        Wraps ``GET /entity/parent_history/{id_child}``. Sorted by
        ``time_from`` ascending so a "where has this device been?"
        timeline reads top-to-bottom. Read-only, no auth.

        Returns an empty list when the endpoint yields no joins or an
        unexpected payload shape — read-only inspection callers can
        treat "no parent ever" and "endpoint failure" alike.
        """
        history = self._make_request(f"/entity/parent_history/{id_child}")
        if not isinstance(history, list):
            return []
        return sorted(history, key=lambda j: j.get("time_from") or "")

    def list_maintenance_visits(self, id_entity: int) -> List[Dict[str, Any]]:
        """List vitjun (maintenance) records for an entity. Public read.

        Wraps ``GET /maintenances/id_entity/{id_entity}``. Mirrors the
        writer-side method of the same name but uses the unauthenticated
        client path, so read verbs (``tos visit list``) don't force
        operators to set up TOS credentials. Returns an empty list when
        the endpoint yields no records or an unexpected payload shape.

        Row shape: ``id``, ``maintenance_type``, ``start_time``,
        ``end_time``, ``participants``, ``participants_names``,
        ``reason``, ``work``, ``remaining``, ``completed``,
        ``creation_time``.
        """
        result = self._make_request(f"/maintenances/id_entity/{id_entity}")
        return result if isinstance(result, list) else []

    def get_maintenance_visit(self, id_maintenance: int) -> Optional[Dict[str, Any]]:
        """Return full detail for one vitjun. Public read.

        Wraps ``GET /maintenance/id_maintenance/{id}``. Adds the
        ``maintenance_attribute_values`` array (each row carries its own
        ``id_maintenance_attribute_value`` — needed by the writer for
        updates, useful here for the detail view).
        """
        result = self._make_request(f"/maintenance/id_maintenance/{id_maintenance}")
        return result if isinstance(result, dict) else None

    def basic_search(self, search_term: str) -> List[Dict[str, Any]]:
        """
        Substring search across TOS attribute values.

        POSTs to /basic_search/ and returns the raw hit list. Each hit
        describes an attribute (e.g. code='owner', value_varchar='...')
        attached to an entity, plus the entity it belongs to. Used by the
        owners module to verify that recognized owner labels are still in
        use in TOS.

        Args:
            search_term: substring to match (case- and diacritic-sensitive)

        Returns:
            List of attribute hits; empty list on error or no match.
        """
        result = self._make_request(
            "/basic_search/", method="POST", data={"search_term": search_term}
        )
        return result if isinstance(result, list) else []

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
            # Build a schema-correct dict that device_structure() can consume.
            # Sort attributes by date_from so later values overwrite earlier ones.
            result: Dict[str, Any] = {
                "id_entity": device.get("id_entity"),
                "code_entity_subtype": device.get("code_entity_subtype"),
                "date_from": session_start,
                "date_to": session_end,
            }
            for attr in sorted(
                device.get("attributes", []),
                key=lambda a: a.get("date_from") or "",
            ):
                code = attr.get("code")
                if code:
                    result[code] = attr.get("value")
            return [result]


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
