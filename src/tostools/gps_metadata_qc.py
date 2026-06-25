#!/usr/bin/python3
#
# Project: gps_metadata
# Authors: Benedikt Gunnar Ófeigsson
#          parts edited TOSTools authored by Tryggvi Hjörvar
# Date: april 2022
#
#

import json
import logging
import sys
from datetime import datetime

import requests
from pyproj import CRS, Transformer

# Import legacy functions (transitioning)
from . import gps_metadata_functions as gpsf

# Import new modular components
from .api._http import canonical_tos_url
from .api.tos_client import TOSClient
from .io.file_utils import read_gzip_file as new_read_gzip_file
from .io.file_utils import read_text_file as new_read_text_file
from .io.file_utils import read_zzipped_file as new_read_zzipped_file
from .utils.logging import get_logger

# TODO: Move formatstring from file_list to a config file
# FREQD="15s_24hr"
# RAWDIR="rinex"
# FFORM="#Rin2"
# DZEND="D.Z",
#
# formatString = (
#     pdir
#     + "/%Y/#b/"
#     + stat
#     + "/"
#     + FREQD
#     + "/"
#     + RAWDIR
#     + "/"
#     + stat
#     + FFORM
#     + DZEND
# )
# HACK: This should be handled in a config with a config file
URL_REST_TOS = "https://vi-api.vedur.is/tos/internal"
REMOTE_FILE_PATH = "/mnt_data/rawgpsdata"
LOCAL_FILE_PATH = "/tmp/gpsdata"
REQUEST_TIMEOUT = 10

# Initialize modular TOS client
tos_client = TOSClient(base_url=URL_REST_TOS, timeout=REQUEST_TIMEOUT)

# defining coordinate systems
itrf2008 = CRS("EPSG:5332")
wgs84 = CRS("EPSG:4326")
# setting up the transformations.
itrf08towgs84 = Transformer.from_crs(itrf2008, wgs84)
wgs84toitrf08 = Transformer.from_crs(wgs84, itrf2008)


def search_station(
    station_identifier,
    code="marker",
    url_rest=URL_REST_TOS,
    domains=None,
    loglevel=logging.WARNING,
):
    """
    comment
    """

    # logging settings
    module_logger = get_logger(__name__, loglevel)

    if domains is None:
        domains = [
            "meteorological",
            "geophysical",
            "hydrological",
            "remote_sensing",
            "remote_sensing_platform",
            "general",
        ]
        module_logger.info("No domains specified, searcing " + str(domains))
    else:
        domains = list(domains.split(","))

    if "remote_sensing" in domains and "remote_sensing_platform" not in domains:
        domains.append("remote_sensing_platform")

    station_identifiers = [station_identifier]
    # Always include search for lowercase except for VM
    if not station_identifier.islower() and not (
        station_identifier[0].lower() == "v" and station_identifier[1].isdigit()
    ):
        station_identifiers += [station_identifier.lower()]
        module_logger.info(
            f"Including lowercase search for {station_identifier.lower()}"
        )

    # Remove padding 0 in search for VM
    if station_identifier[0:2] == "V0":
        station_identifiers += ["V" + station_identifier[2:]]
        module_logger.info(
            "Including unpadded search for " + "V" + station_identifier[2:]
        )

    stations = []
    for station_identifier in station_identifiers:
        for domain in domains:
            # Construct POST query
            body = {"code": code, "value": station_identifier}

            if domain == "remote_sensing_platform":
                entity_type = "platform"
            else:
                entity_type = "station"

            # Query TOS api
            try:
                url = canonical_tos_url(
                    url_rest, "/entity/search/" + entity_type + "/" + domain + "/"
                )
                module_logger.info("sending the post request: %s", url)
                response = requests.post(
                    url,
                    data=json.dumps(body),
                    headers={"Content-Type": "application/json"},
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.ConnectionError as error:
                module_logger.error(
                    "Failed to establish connection to %s with error:\n%s",
                    url_rest,
                    error,
                )
                sys.exit(1)

            response.raise_for_status()
            if response.content:
                # data={}
                for station in response.json():
                    # Get current location for remote_sensing_platform location
                    if (
                        station["id_entity_parent"]
                        and station["code_entity_subtype"] == "remote_sensing_platform"
                    ):
                        location = getEntity(station["id_entity_parent"])
                        if location:
                            station["location"] = []
                            # station['location']=location
                            station["location"].append(
                                next(
                                    (
                                        item
                                        for item in location["attributes"]
                                        if (
                                            item["code"] == "name"
                                            and item["date_to"] is None
                                        )
                                    ),
                                    {"value": None},
                                )
                            )
                            station["location"].append(
                                next(
                                    (
                                        item
                                        for item in location["attributes"]
                                        if (
                                            item["code"] == "lat"
                                            and item["date_to"] is None
                                        )
                                    ),
                                    {"value": None},
                                )
                            )
                            station["location"].append(
                                next(
                                    (
                                        item
                                        for item in location["attributes"]
                                        if (
                                            item["code"] == "lon"
                                            and item["date_to"] is None
                                        )
                                    ),
                                    {"value": None},
                                )
                            )

                    stations.append(station)
                    # stations.append(data)

    return stations


def device_attribute_history(device, session_start, session_end, loglevel=logging.INFO):
    """
    sort out history within device
    """

    module_logger = get_logger(__name__, loglevel)
    tmp_connections = []
    connections = []

    module_logger.info(
        "Session: %s-%s:: device type: %s, id_entity: %s",
        session_start,
        session_end,
        device["code_entity_subtype"],
        device["id_entity"],
    )

    module_logger.debug("\n%s", gpsf.json_print(device))
    module_logger.debug(
        "device['attributes']:\n%s\n", gpsf.json_print(device["attributes"])
    )

    key_list = [
        "serial_number",
        "model",
        "date_start",
        "GPS",
        "GLO",
        "firmware_version",
        "software_version",
        "antenna_height",
        "monument_height",
        "antenna_offset_north",
        "antenna_offset_east",
        "antenna_reference_point",
        "date_from",  # add keys above this point
        "date_to",
    ]
    collection = dict.fromkeys(key_list[:-2])
    collection["id_entity"] = device["id_entity"]
    collection["date_from"] = session_start
    collection["date_to"] = session_end
    collection["code_entity_subtype"] = device["code_entity_subtype"]
    connection = dict.fromkeys(key_list)

    dates_from = [attribute["date_from"] for attribute in device["attributes"]]
    dates_to = [attribute["date_to"] for attribute in device["attributes"]]
    sub_sessions = set(zip(dates_from, dates_to))
    module_logger.info("sub_sessions: %s" % sub_sessions)

    module_logger.debug("dates_from: %s" % dates_from)
    for sub_session in sub_sessions:
        module_logger.info("sub_session: %s", sub_session)

        # NOTE: only want session within session_start and session_end
        if sub_session[0] >= session_end if session_end else False:
            continue
        if sub_session[1] < session_start if sub_session[1] else False:
            continue

        connection["id_entity"] = device["id_entity"]
        connection["date_from"] = session_start
        connection["date_to"] = session_end
        connection["code_entity_subtype"] = device["code_entity_subtype"]

        for item in device["attributes"]:
            if (item["date_from"], item["date_to"]) != sub_session:
                continue
            if item["date_from"] >= session_end if session_end else False:
                continue
            if item["date_to"] <= session_start if item["date_to"] else False:
                continue

            # NOTE: ignoring items that have 0 or negative duration
            if item["date_to"] is not None:
                if item["date_from"] >= item["date_to"]:
                    module_logger.debug(
                        "Session start is the same as, or after session end: {}, end: {}. Skipping ...".format(
                            item["date_from"], item["date_to"]
                        )
                    )
                    continue

            if item["code"] in key_list:
                connection[item["code"]] = item["value"]
                collection[item["code"]] = item["value"]

                module_logger.debug(
                    "%s-%s:: item['code']: %s, item['value']: %s",
                    item["date_from"],
                    item["date_to"],
                    item["code"],
                    item["value"],
                )
                module_logger.debug("connection: \n%s", gpsf.json_print(connection))

                if sub_session[0] >= session_start:
                    connection["date_from"] = item["date_from"]
                    collection["date_from"] = item["date_from"]
                    module_logger.info(
                        "sub_session[0] >= session_start: %s >= %s: setting connection['date_from']=%s",
                        sub_session[0],
                        session_start,
                        item["date_from"],
                    )

                # connection["date_to"] = session_end checking if it needs changing
                if session_end is None:
                    if item["date_to"] is not None:
                        connection["date_to"] = item["date_to"]
                        collection["date_to"] = item["date_to"]
                else:
                    if item["date_to"] is not None and item["date_to"] < session_end:
                        module_logger.info(
                            "item['date_to'] < session_end: %s < %s: setting connection['date_to']=%s",
                            item["date_to"],
                            session_end,
                            item["date_to"],
                        )
                        connection["date_to"] = item["date_to"]
                        collection["date_to"] = item["date_to"]

            else:  # NOTE: reduntant skip later
                module_logger.debug(
                    "item['code']: %s is not in key_list:\n %s",
                    item["code"],
                    gpsf.json_print(key_list),
                )

        module_logger.debug("connection:\n%s", gpsf.json_print(connection))
        module_logger.debug("collection:\n%s", gpsf.json_print(collection))
        tmp_connections.append(connection.copy())

    module_logger.debug("tmp_connections:\n%s", gpsf.json_print(tmp_connections))

    # All attributes fell outside the session period — nothing to return.
    if not tmp_connections:
        module_logger.warning(
            "No attribute data within session %s-%s for entity %s",
            session_start,
            session_end,
            device.get("id_entity"),
        )
        return connections

    dates_from = [attribute["date_from"] for attribute in tmp_connections]
    dates_to = [attribute["date_to"] for attribute in tmp_connections]
    module_logger.debug("dates_from: %s" % dates_from)
    module_logger.debug("dates_to: %s" % dates_to)

    collection.update({key: None for key in key_list[:]})

    module_logger.debug("Number of sessions: %s", len(tmp_connections))
    module_logger.debug("tmp_connections: %s", gpsf.json_print(tmp_connections))
    sub_sessions = set(zip(dates_from, dates_to))
    module_logger.info("sub_sessions: %s", sub_sessions)

    full_session = (session_start, session_end)
    matching = [
        c for c in tmp_connections if (c["date_from"], c["date_to"]) == full_session
    ]
    if not matching:
        # Attribute periods don't span session exactly (e.g. firmware update
        # mid-session moves date_from away from session_start).  Use the
        # most-recent open connection as the base.
        matching = [c for c in tmp_connections if c.get("date_to") is None]
    if matching:
        full_session_dict = tmp_connections.pop(tmp_connections.index(matching[-1]))
        for key, value in full_session_dict.items():
            if value:
                collection[key] = value
    sub_sessions.discard(full_session)
    module_logger.debug("tmp_connections: %s", gpsf.json_print(tmp_connections))

    if sub_sessions:
        for sub_session in sorted(sub_sessions):
            session_collection = (
                connection
                for connection in tmp_connections
                if (connection["date_from"], connection["date_to"]) == sub_session
            )
            collection.update(zip(key_list[-2:], sub_session))
            for connection in session_collection:
                for key in key_list[2:-2]:
                    if connection[key]:
                        collection[key] = connection[key]
                        module_logger.info("%s: %s", key, connection[key])
            connections.append(collection.copy())
    else:
        connections.append(collection.copy())

    # if connection["code_entity_subtype"] == "gnss_receiver":
    #     module_logger.warning("connections: \n%s", gpsf.json_print(connections))

    module_logger.debug("connections: \n%s", gpsf.json_print(connections))

    return connections


def additional_contact_fields(contact_name, loglevel=logging.WARNING):
    get_logger(__name__, loglevel)

    contact_add = {}

    if contact_name == "Veðurstofa Íslands":
        contact_add["abbreviation"] = "IMO"
        contact_add["name_en"] = "Icelandic Meteorological Office"
        contact_add["email"] = "gnss-epos@vedur.is"
        contact_add["primary_contact"] = "GNSS Operator"
        contact_add["department"] = "Infrastructure Division"
        contact_add["address_en"] = "Bústaðarvegur 7-9, 105 Reykjavík, Iceland"
        contact_add["main_url"] = "https://vedur.is"
        contact_add["main_url_en"] = "https://en.vedur.is"
    # elif contact_name == "Landmælingar Íslands":
    #     contact_add["abbreviation"] = ""
    #     contact_add["name_en"] = ""
    #     contact_add["email"] = ""
    #     contact_add["primary_contact"] = ""
    #     contact_add["department"] = ""
    #     contact_add["address_en"] = ""
    #     contact_add["main_url"] = ""
    #     contact_add["main_url_en"] = ""
    else:
        contact_add["abbreviation"] = ""
        contact_add["name_en"] = ""
        contact_add["email"] = ""
        contact_add["primary_contact"] = ""
        contact_add["department"] = ""
        contact_add["address_en"] = ""
        contact_add["main_url"] = ""
        contact_add["main_url_en"] = ""

    return contact_add


def get_contacts(id_entity_parent, url_rest, loglevel=logging.WARNING):
    """
    get station contacts
    """

    module_logger = get_logger(__name__, loglevel)

    contact = {}
    owner_addition = {}

    owner_response = requests.get(
        canonical_tos_url(url_rest, "/entity_contacts/" + str(id_entity_parent) + "/"),
        timeout=REQUEST_TIMEOUT,
    )
    owners = owner_response.json()
    module_logger.debug("Owners %s", gpsf.json_print(owners))
    for owner in owners:
        # if owner["name"] == "Veðurstofa Íslands":
        owner_addition = additional_contact_fields(owner["name"])
        module_logger.debug("Owner_addtion %s", gpsf.json_print(owner_addition))

        contact[owner["role"]] = {
            "id_entity": owner["id_contact"],
            "role": owner["role"],
            "role_is": owner["role_is"],
            "name": owner["name"],
            "address": owner["address"],
            "comment": owner["comment"],
            "phone_primary": owner["phone_primary"],
            "ssid": owner["ssid"],
            "abbreviation": owner_addition["abbreviation"],
            "name_en": owner_addition["name_en"],
            "email": owner_addition["email"],
            "primary_contact": owner_addition["primary_contact"],
            "department": owner_addition["department"],
            "address_en": owner_addition["address_en"],
            "main_url": owner_addition["main_url"],
            "main_url_en": owner_addition["main_url_en"],
        }
        module_logger.info("%s: %s", owner["role_is"], owner["name"])

    if not owners:
        # HACK: Hardcoded IMO fallback when no owners found in TOS API
        # TODO: implement proper IMO contact API integration instead of hardcoded values
        # REVIEW: Contact management system architecture needs review - see CLAUDE.md
        # response = requests.get(
        #     url_rest + "/contact/" + str(imo_id) + "/",
        #     timeout=request_timeout,
        # )
        # owner = response.json()

        module_logger.warning("No owners found at: %s. Setting default", url_rest)
        # Get complete IMO contact information for fallback
        imo_addition = additional_contact_fields("Veðurstofa Íslands")

        contact["owner"] = {
            "role": "owner",
            "role_is": "Eigandi stöðvar",
            "name": "Veðurstofa Íslands",
            "address": "Bústaðarvegur 7-9, 105 Reykjavík, Ísland",
            "comment": "",
            "phone_primary": "5226000",
            "abbreviation": imo_addition["abbreviation"],
            "name_en": imo_addition["name_en"],
            "email": imo_addition["email"],
            "primary_contact": imo_addition["primary_contact"],
            "department": imo_addition["department"],
            "address_en": imo_addition["address_en"],
            "main_url": imo_addition["main_url"],
            "main_url_en": imo_addition["main_url_en"],
        }
        contact["operator"] = {
            "role": "operator",
            "role_is": "Rekstraraðili stöðvar",
            "name": "Veðurstofa Íslands",
            "address": "Bústaðarvegur 7-9, 105 Reykjavík, Ísland",
            "comment": "",
            "phone_primary": "5226000",
            "abbreviation": imo_addition["abbreviation"],
            "name_en": imo_addition["name_en"],
            "email": imo_addition["email"],
            "primary_contact": imo_addition["primary_contact"],
            "department": imo_addition["department"],
            "address_en": imo_addition["address_en"],
            "main_url": imo_addition["main_url"],
            "main_url_en": imo_addition["main_url_en"],
        }
        module_logger.info(
            "Setting default contact and role: %s", contact["operator"]["name"]
        )

    if contact["owner"]["name"] == "Landmælingar Íslands":
        contact["operator"] = {
            "role": "operator",
            "role_is": "Rekstraraðili stöðvar",
            "name": "Landmælingar Íslands",
        }
        module_logger.info(
            "Setting role of contact: %s: %s",
            contact["operator"]["role_is"],
            contact["operator"]["name"],
        )

    if "contact" not in contact.keys():
        contact["contact"] = contact["owner"]
        module_logger.info(
            "Setting role of contact: {}: {}".format(
                contact["contact"]["role_is"], contact["contact"]["name"]
            )
        )
    if "operator" not in contact.keys():
        contact["operator"] = contact["contact"]
        module_logger.info(
            "Setting role of contact: {}: {}".format(
                contact["operator"]["role_is"], contact["operator"]["name"]
            )
        )
    module_logger.info("contact: %s", gpsf.json_print(contact))

    return contact


def gps_metadata(station_identifier, url_rest, loglevel=logging.WARNING):
    """**Deprecated.** Use :func:`gps_metadata_via_devices` instead.

    Legacy synthesis chain — kept available for the
    ``tosGPS --use-legacy-synthesis`` opt-out and for byte-equality
    oracle tests on clean stations. The two known bugs are:

    1. ``device_attribute_history`` slicer drops sub-windows when
       attribute periods misalign (pair-based dedup).
    2. ``get_device_history`` pivot zips sorted starts and ends
       position-wise, producing inverted sessions on overlap-heavy
       stations.

    See ``docs/architecture/synthesis-legacy-divergence.md`` for the
    full write-up. Slated for removal once the new chain has run in
    production long enough to retire the ops fallback.

    Args:
        station_identifier: station 4 letter marker
        url_rest: REST service endpoint to access TOS
    """

    # logging settings
    module_logger = get_logger(__name__, loglevel)

    station, devices_history = get_station_metadata(
        station_identifier, url_rest, loglevel=loglevel
    )
    if not station:
        module_logger.warning(
            "dictionary for station %s is empty returning", station_identifier
        )
        return {}

    module_logger.debug("station: \n%s", gpsf.json_print(station))
    module_logger.debug("device_history: \n%s", gpsf.json_print(devices_history))

    device_sessions = get_device_sessions(devices_history, url_rest, loglevel=loglevel)

    # Sort by time_from
    device_sessions.sort(key=lambda d: d["device"]["date_from"])
    module_logger.info(
        "device_sessions: %s",
        json.dumps(
            [
                f"{item['device']['date_from']}-{item['device']['date_to']}: {item['device']['code_entity_subtype']}"
                for item in sorted(
                    device_sessions, key=lambda x: x["device"]["date_from"]
                )
            ],
            indent=2,
        ),
    )

    station["device_history"] = get_device_history(device_sessions, loglevel=loglevel)
    module_logger.debug("station: %s", gpsf.json_print(station))

    return station


def gps_metadata_via_devices(station_identifier, url_rest, loglevel=logging.WARNING):
    """Drop-in replacement for :func:`gps_metadata` using the new synthesis chain.

    Bridges the legacy ``station`` dict shape (consumed by
    ``tosGPS PrintTOS`` / ``tosGPS sitelog``) onto the
    :func:`tostools.devices.station_sessions` composer. The
    top-level station metadata (``marker``, ``name``, ``lat``,
    ``lon``, ``altitude``, ``contact``, ...) is still pulled from
    :func:`get_station_metadata` — only ``device_history`` is
    replaced.

    Behaviour against the legacy chain:

    * For stations with clean attribute periods + non-overlapping
      device installations (RHOF, AKUR, VMEY, SKRO): byte-equal
      output. Locked by
      ``test_rhof_gps_metadata_via_devices_matches_legacy_snapshot``.
    * For stations exhibiting either of the two legacy bugs or the
      "receiver-swap gap" enrichment pattern: divergent output.
      See ``docs/architecture/synthesis-legacy-divergence.md``.

    Gated behind the ``tosGPS --use-new-synthesis`` flag — once the
    sitelog golden files have been re-captured + domain-expert
    reviewed for the divergent stations, this becomes the default
    (phase 5).
    """
    from . import devices
    from .api.tos_client import TOSClient

    module_logger = get_logger(__name__, loglevel)
    station, devices_history = get_station_metadata(
        station_identifier, url_rest, loglevel=loglevel
    )
    if not station:
        module_logger.warning(
            "dictionary for station %s is empty returning", station_identifier
        )
        return {}

    client = TOSClient(base_url=url_rest)
    station_id = int(devices_history["id_entity"])
    station["device_history"] = devices.station_sessions(client, station_id)
    return station


def get_station_metadata(station_identifier, url_rest, loglevel=logging.WARNING):
    """"""

    module_logger = get_logger(__name__, loglevel)

    domain = "geophysical"
    try:
        station = search_station(
            station_identifier,
            url_rest=url_rest,
            domains=domain,
            loglevel=logging.WARNING,
        )[0]
        module_logger.setLevel(loglevel)
    except IndexError as error:
        module_logger.error(
            "{} station {} not found in TOS database. \
            Error {}".format(
                domain, station_identifier, error
            )
        )
        return [], []

    module_logger.debug(
        "TOS station %s dictionary:\n=================\n%s\n================",
        station_identifier,
        gpsf.json_print(station),
    )

    id_entity = station["id_entity"]
    module_logger.info("station {} id_entity: {}".format(station_identifier, id_entity))
    station = {}  # clear dictionary for later use
    module_logger.info(
        'Sending request "{}"'.format(
            url_rest + "/history/entity/" + str(id_entity) + "/"
        )
    )

    response = requests.get(
        canonical_tos_url(url_rest, "/history/entity/" + str(id_entity) + "/"),
        timeout=REQUEST_TIMEOUT,
    )
    devices_history = response.json()
    module_logger.debug(
        "TOS station %s /history/entity/%s:\n=================\n%s\n================\n",
        station_identifier,
        id_entity,
        gpsf.json_print(devices_history),
    )
    module_logger.debug(
        "TOS station dictionary keys: {}".format(devices_history.keys())
    )
    module_logger.debug(
        "Station attributes: {}".format(
            [attribute["code"] for attribute in devices_history["attributes"]]
        )
    )

    station["contact"] = get_contacts(id_entity, url_rest, loglevel=loglevel)
    for attribute in devices_history["attributes"]:
        module_logger.debug(attribute["code"])
        if attribute["code"] in [
            "marker",
            "name",
            "iers_domes_number",
            "in_network_epos",
            "geological_characteristic",
            "bedrock_condition",
            "bedrock_type",
            "is_near_fault_zones",
            "date_start",
        ]:
            station[attribute["code"]] = attribute["value"]
        elif attribute["code"] in ["lon", "lat", "altitude"]:
            station[attribute["code"]] = float(attribute["value"])

    return station, devices_history


def get_device_history(device_sessions, loglevel=logging.WARNING):
    """"""

    # logging settings
    module_logger = get_logger(__name__, loglevel)

    sessions_start = iter(
        sorted({session["device"]["date_from"] for session in device_sessions})
    )
    sessions_end = iter(
        sorted(
            {
                session["device"]["date_to"]
                for session in device_sessions
                if (session["device"]["date_to"] is not None)
            }
        )
    )

    station_history = []
    for start in sessions_start:
        try:
            end = next(sessions_end)
        except StopIteration:
            end = None
        module_logger.info("====== session start-end: {}-{} ======".format(start, end))

        station_session = {}
        if start:
            station_session["time_from"] = datetime.strptime(start, "%Y-%m-%dT%H:%M:%S")
        else:
            station_session["time_from"] = None

        if end:
            station_session["time_to"] = datetime.strptime(end, "%Y-%m-%dT%H:%M:%S")
        else:
            station_session["time_to"] = None

        for session in device_sessions:
            module_logger.debug(
                "Session: \n%s",
                gpsf.json_print(session),
            )

            device = session["device"]
            module_logger.debug("device: \n%s", gpsf.json_print(device))
            module_logger.info(
                "---------- %s: %s - %s ---------",
                device["code_entity_subtype"],
                session["device"]["date_from"],
                session["device"]["date_to"],
            )

            if end:
                if session["device"]["date_from"] <= start:
                    if (
                        session["device"]["date_to"] is not None
                        and session["device"]["date_to"] >= end
                    ):
                        station_session[device["code_entity_subtype"]] = (
                            device_structure(device.copy())
                        )
                        module_logger.info(device_structure(device.copy()))
                    elif session["device"]["date_to"] is None:
                        station_session[device["code_entity_subtype"]] = (
                            device_structure(device.copy())
                        )
                        module_logger.info(device_structure(device.copy()))
            else:
                if session["device"]["date_to"] is None:
                    station_session[device["code_entity_subtype"]] = device_structure(
                        device.copy()
                    )
                    module_logger.info(device_structure(device.copy()))

        module_logger.debug("%s", gpsf.json_print(station_session))
        station_history.append(station_session)
        module_logger.info("=================================\n")

    return station_history


def get_device_sessions(devices_history, url_rest, loglevel=logging.WARNING):
    """"""

    module_logger = get_logger(__name__, loglevel)

    device_sessions = []
    devices_used = ["gnss_receiver", "antenna", "radome", "monument"]
    for connection in devices_history["children_connections"]:
        # NOTE: ignoring sessions that have 0 duration
        if connection["time_from"] == connection["time_to"]:
            module_logger.debug(
                "Session start is the same as session end: {}, end: {}".format(
                    connection["time_from"], connection["time_to"]
                )
            )
            continue

        # NOTE: sending a request for device history
        id_entity_child = connection["id_entity_child"]
        request_url = canonical_tos_url(
            url_rest, f"/history/entity/{str(id_entity_child)}/"
        )
        try:
            devices_response = requests.get(request_url, timeout=REQUEST_TIMEOUT)
            device = devices_response.json()
            module_logger.debug("device: %s", gpsf.json_print(device))
            # module_logger.warning("device {}".format(device))
        except:
            module_logger.error("failed to establish connection to {}".format(url_rest))
            sys.exit(1)

        if device["code_entity_subtype"] in devices_used:
            module_logger.debug(
                "\n================= \
                \nitem in devices_history[\"children_connections\"]: \
                \n%s\nSending request: %s \
                \nreturned device as json \
                \n device['code_entity_subtype']: %s\
                \n-----------------\n",
                gpsf.json_print(connection),
                request_url,
                device["code_entity_subtype"],
            )
            module_logger.debug(
                "\njson reponse from %s in device:\n%s\n",
                request_url,
                gpsf.json_print(device),
            )

            attribute_history = device_attribute_history(
                device,
                connection["time_from"],
                connection["time_to"],
                loglevel=logging.CRITICAL,
            )

            module_logger.debug(
                "attribute_history:\n%s", gpsf.json_print(attribute_history)
            )

            for attribute in attribute_history:
                connection["device"] = attribute
                device_sessions.append(connection.copy())

        else:
            module_logger.debug(
                "\n================= \
                \nitem in devices_history[\"children_connections\"]: \
                \n%s\nSending request: %s \
                \nreturned device as json \
                \nNOT in 'device_used': %s \
                \n device['code_entity_subtype']: %s\
                \n=================\n",
                gpsf.json_print(connection),
                request_url,
                devices_used,
                device["code_entity_subtype"],
            )
            module_logger.debug(
                "\njson reponse from %s in device:\n%s\n",
                request_url,
                gpsf.json_print(device),
            )

    return device_sessions


def device_structure(device, loglevel=logging.WARNING):
    """"""

    module_logger = get_logger(__name__, loglevel)

    module_logger.debug("device_session: %s", device["code_entity_subtype"])

    if device["code_entity_subtype"] == "gnss_receiver":
        return {
            "model": device["model"],
            "serial_number": device["serial_number"],
            "firmware_version": device["firmware_version"],
            "software_version": device["software_version"],
        }

    if device["code_entity_subtype"] == "antenna":
        module_logger.debug("device: %s", gpsf.json_print(device))

        antenna_height = device["antenna_height"]
        if antenna_height is None:
            antenna_height = 0.0
        else:
            antenna_height = float(antenna_height)

        antenna_offset_north = device["antenna_offset_north"]
        if antenna_offset_north is None:
            antenna_offset_north = 0.0
        else:
            antenna_offset_north = float(antenna_offset_north)

        antenna_offset_east = device["antenna_offset_east"]
        if antenna_offset_east is None:
            antenna_offset_east = 0.0
        else:
            antenna_offset_east = float(antenna_offset_east)

        return {
            "model": device["model"],
            "serial_number": device["serial_number"],
            "antenna_height": antenna_height,
            "antenna_offset_east": antenna_offset_east,
            "antenna_offset_north": antenna_offset_north,
            "antenna_reference_point": device["antenna_reference_point"],
        }

    if device["code_entity_subtype"] == "radome":
        return {
            "model": device["model"],
            "serial_number": device["serial_number"],
        }

    if device["code_entity_subtype"] == "monument":
        if device["monument_height"]:
            monument_height = device["monument_height"]
        else:
            monument_height = device["antenna_height"]

        if monument_height is None:
            monument_height = 0.0
        else:
            monument_height = float(monument_height)

        antenna_offset_north = device["antenna_offset_north"]
        if antenna_offset_north is None:
            antenna_offset_north = 0.0
        else:
            antenna_offset_north = float(antenna_offset_north)

        antenna_offset_east = device["antenna_offset_east"]
        if antenna_offset_east is None:
            antenna_offset_east = 0.0
        else:
            antenna_offset_east = float(antenna_offset_east)

        return {
            "serial_number": device["serial_number"],
            "monument_height": monument_height,
            "monument_offset_north": antenna_offset_north,
            "monument_offset_east": antenna_offset_east,
        }

    else:
        return {}


def read_gzip_file(rfile, loglevel=logging.WARNING):
    """Legacy wrapper for the new modular file reader."""
    # Use the new modular file reader
    content_bytes = new_read_gzip_file(rfile, loglevel)
    if content_bytes:
        return content_bytes.decode("utf-8")
    return None


def read_zzipped_file(rfile, loglevel=logging.WARNING):
    """
    Legacy wrapper for the new modular Z file reader.
    reads a RINEX file from path rfile and returns the unzipped content.

    input:
        rfile: a filename of a rinex file
        loglevel: loglevel to use within the module
    output:
        unzipped file contend
    """
    # Use the new modular file reader
    content_bytes = new_read_zzipped_file(rfile, loglevel)
    if content_bytes:
        return content_bytes.decode("utf-8")
    return None


def read_text_file(rfile, loglevel=logging.WARNING):
    """
    Legacy wrapper for the new modular text file reader.
    read file and return the content
    """
    # Use the new modular file reader
    return new_read_text_file(rfile, loglevel)


def main(level=logging.WARNING):
    """
    quering metadata from tos and comparing to relevant rinex files
    """
    # logging settings
    logger = get_logger(__name__, level)

    logger.info("quering metadata from tos and comparing to relevant rinex files")


if __name__ == "__main__":
    main(level=logging.DEBUG)
