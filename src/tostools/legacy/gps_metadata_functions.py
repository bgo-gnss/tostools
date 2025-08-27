#!/usr/bin/python3.1
#
# Project: gps_metadata_functions
# Authors: Benedikt Ǵunnar Ófeigsson
#          parts are edited TOSTools authored by Tryggvi Hjörvar
# Date: april 2022
#
#

import json
import logging
import sys
from datetime import datetime as dt
from datetime import timedelta
from operator import itemgetter
from pathlib import Path, PurePath

import pandas as pd
from gtimes import timefunc as tf
from gtimes.timefunc import datefRinex
from tabulate import tabulate

from . import gps_metadata_qc as gpsqc


def get_data_file_path(filename):
    """
    Get absolute path to data files, independent of working directory.

    This function locates data files relative to the package directory,
    making tosGPS work from any directory.
    """
    # Get the directory containing this module
    package_dir = Path(__file__).parent.parent.parent.parent
    data_path = package_dir / "data" / "station_config" / filename

    if not data_path.exists():
        # Fallback: try legacy path for backwards compatibility
        legacy_path = package_dir / "tmp" / "organized" / "station_data" / filename
        if legacy_path.exists():
            return str(legacy_path)
        # Fallback: try relative to current working directory
        fallback_path = Path("data") / "station_config" / filename
        if fallback_path.exists():
            return str(fallback_path)
        # If none exist, return the expected path for error reporting
        return str(data_path)

    return str(data_path)


def print_station_history(station, raw_format=False, loglevel=logging.WARNING):
    """
    print station history
    """

    # logging settings
    module_logger = get_logger(name=__name__)
    module_logger.setLevel(loglevel)

    station_headers = [key for key in station.keys() if key != "device_history"]
    station_attributes = tuple(
        value
        for key, value in station.items()
        if key not in ["contact", "device_history"]
    )
    module_logger.info("Station: {}".format(station))
    print(tabulate([station_attributes], headers=station_headers))
    contact_info = [
        (station["contact"][item]["role_is"], station["contact"][item]["name"])
        for item in station["contact"].keys()
    ]
    print(tabulate(contact_info, headers=["Hlutverk", "Nafn"]))
    print("-" * 100)
    device_list = ["gnss_receiver", "antenna", "monument", "radome"]
    print(
        " " * 42
        + f"| {device_list[0]}"
        + " " * 39
        + f"| {device_list[1]}"
        + " " * 38
        + f"| {device_list[2]}"
        + " " * 18
        + f"| {device_list[3]}"
    )

    headers_list = []
    devices_list = []
    device_types_list = []
    attributes_string_list = []

    for item in station["device_history"]:
        devices = [key for key in item.keys() if key not in ["time_from", "time_to"]]

        header_list = ["time_from", "time_to"]
        if item["time_from"] is None:
            time_from = "None"
        else:
            time_from = item["time_from"].strftime("%Y-%m-%d %H:%M:%S")

        if item["time_to"] is None:
            time_to = "None"
        else:
            time_to = item["time_to"].strftime("%Y-%m-%d %H:%M:%S")

        attributes_list = [time_from, time_to]

        print_attributes_string = "{:<19}  {:<19}  "
        print_header_string = "{:<19}  {:<19}  "

        for device in device_list:
            if device in item.keys():
                device_headers = list(key for key in item[device].keys())
                device_attributes = [value for _, value in item[device].items()]
                # make the labels nicer
                if device == "monument":
                    module_logger.debug("device_headers: %s", device_headers)
                    dev_index = device_headers.index("serial_number")
                    device_headers.remove("serial_number")
                    del device_attributes[dev_index]

                    # dev_index = device_headers.index("model")
                    # device_headers.remove("model")
                    # del device_attributes[dev_index]

                if raw_format is False:
                    if "antenna_height" in device_headers:
                        device_headers[device_headers.index("antenna_height")] = (
                            "Height"
                        )
                    if "antenna_reference_point" in device_headers:
                        device_headers[
                            device_headers.index("antenna_reference_point")
                        ] = "Ref."

                    if "monument_height" in device_headers:
                        device_headers[device_headers.index("monument_height")] = (
                            "Height"
                        )
                    if "monument_offset_north" in device_headers:
                        device_headers[
                            device_headers.index("monument_offset_north")
                        ] = "North"
                    if "monument_offset_east" in device_headers:
                        device_headers[device_headers.index("monument_offset_east")] = (
                            "East"
                        )

                    if "serial_number" in device_headers:
                        device_headers[device_headers.index("serial_number")] = "SN"
                    if "model" in device_headers:
                        device_headers[device_headers.index("model")] = "Model"
                    if "time_from" in device_headers:
                        device_headers[device_headers.index("time_from")] = "Start time"
                    if "time_to" in device_headers:
                        device_headers[device_headers.index("time_to")] = "End time"

                try:
                    for i, n in enumerate(device_attributes):
                        if n is None:
                            device_attributes[i] = "None"
                except:
                    pass

                if device == "gnss_receiver":
                    hstring = (
                        "| " + "{:14.14} " * (len(device_headers) - 1) + " {:5.5} "
                    )
                    string = "| " + "{:14.14} " * (len(device_headers) - 1) + " {:5.5} "
                elif device == "antenna":
                    hstring = "| " + "{:14.14} {:15.15} {:>7.4} {:>7.4} {:>7.4} {:5.5} "
                    string = (
                        "| " + "{:14.14} {:15.15} {:>7.4f} {:>7.4f} {:>7.4f} {:5.5} "
                    )
                elif device == "monument":
                    hstring = "| " + "{:25.25} {:7.7} {:7.7} {:7.7}   "
                    string = "| " + "{:25.25} {:>7.4f} {:>7.4f} {:>7.4f}   "
                else:
                    string = "| " + "{} " * (len(device_headers)) + "  "

                print_header_string += hstring
                header_list += device_headers

                print_attributes_string += string
                attributes_list += device_attributes

        device_types_list.append(devices)
        attributes_string_list.append(print_attributes_string)
        headers_list.append(header_list)
        devices_list.append(attributes_list)

    # print(print_string)
    # print( print_header_string.format(*header_list) )
    # print( print_attributes_string.format(*attributes_list) )
    if raw_format:
        print("+" * 200)
        for devices, headers, values in zip(
            device_types_list, headers_list, devices_list
        ):
            print(tabulate([devices], tablefmt="plain"))
            # print(tabulate([headers]))
            print(tabulate([values], tablefmt="fancy"))
        print("+" * 200)
    else:
        # print(print_header_string)
        # print(headers_list)
        # print(print_header_string.format(*headers_list[0]))
        print("-" * 200)
        # print(attributes_string_list)
        for string, value in zip(attributes_string_list, devices_list):
            # print(string)
            print(string.format(*value))


def getSession(station, session_nr, loglevel=logging.WARNING):
    """ """

    # logging
    module_logger = get_logger(name=__name__)

    session = {key: value for key, value in station.items() if key != "device_history"}
    module_logger.info("Station information: {}".format(session))
    session["device_history"] = station["device_history"][session_nr]
    module_logger.info("session dictionary: {}".format(session))

    return session


def print_station_info(station, loglevel=logging.WARNING):
    """
    print station metadata information
    """

    # logging
    module_logger = get_logger(name=__name__)
    module_logger.setLevel(loglevel)

    header = "*SITE  Station Name      Session Start      Session Stop       Ant Ht   HtCod  Ant N    Ant E    Receiver Type         Vers                  SwVer  Receiver SN           Antenna Type     Dome   Antenna SN"
    # print(header)

    stationInfo_list = []
    for item in station["device_history"]:
        module_logger.debug("item: %s", json_print(item))
        try:
            time_from = item["time_from"].strftime("%Y %j %H %M %S")
        except:
            module_logger.warning(
                "time_from has wrong type should be datetime, format is {0}: exiting program ...".format(
                    type(item["time_from"])
                )
            )
            quit()

        try:
            time_to = item["time_to"].strftime("%Y %j %H %M %S")
        except:
            time_to = "9999 999 00 00 00"

        # receiver type

        if "antenna" in item.keys():
            if item["antenna"]["model"] is None:
                antenna_type = "---------------"
            else:
                antenna_type = item["antenna"]["model"]

            # receiver sn
            if item["antenna"]["serial_number"] is None:
                antenna_SN = "---------------"
            else:
                antenna_SN = item["antenna"]["serial_number"]

            # Antenna height and offsets
            antenna_height = (
                item["antenna"]["antenna_height"] + item["monument"]["monument_height"]
            )

            antenna_N = (
                item["antenna"]["antenna_offset_north"]
                + item["monument"]["monument_offset_north"]
            )
            antenna_E = (
                item["antenna"]["antenna_offset_east"]
                + item["monument"]["monument_offset_east"]
            )

            if item["antenna"]["antenna_reference_point"] is None:
                antenna_reference_point = "-----"
            else:
                antenna_reference_point = item["antenna"]["antenna_reference_point"]

        else:
            antenna_height = 0.0000
            antenna_reference_point = "DHARP"
            antenna_N = 0.0000
            antenna_E = 0.0000
            antenna_type = "---------------"
            antenna_SN = "---------------"

        # receiver type
        if "gnss_receiver" in item.keys():
            if item["gnss_receiver"]["model"] is None:
                receiver_type = "--------------------"
            else:
                receiver_type = item["gnss_receiver"]["model"]

            # receiver SN
            if item["gnss_receiver"]["serial_number"] is None:
                receiver_SN = "--------------------"
            else:
                receiver_SN = item["gnss_receiver"]["serial_number"]

            # receiver firmware
            if item["gnss_receiver"]["firmware_version"] is None:
                firmware_version = "--------------------"
            else:
                firmware_version = item["gnss_receiver"]["firmware_version"]

            # receiver software
            if item["gnss_receiver"]["software_version"] is None:
                software_version = "-----"
            else:
                software_version = item["gnss_receiver"]["software_version"]
            # -------------------------------------------------------
        else:
            receiver_type = "--------------------"
            firmware_version = "--------------------"
            software_version = "-----"
            receiver_SN = "--------------------"

        # radome
        if "radome" in item.keys():
            dome = item["radome"]["model"]
        else:
            dome = "NONE"

        # header='*SITE  Station Name      Session Start      Session Stop       Ant Ht   HtCod  Ant N    Ant E    Receiver Type         Vers                  SwVer  Receiver SN           Antenna Type     Dome   Antenna SN'
        sessionLine = " {0:4.4}  {1:17.17} {2:17.17}  {3:17.17}  {4: 1.4f}  {5:5.5}  {6: 1.4f}  {7: 1.4f}  {8:20.20}  {9:20.20}  {10:>5.5}  {11:20.20}  {12:15.15}  {13:5.5}  {14:20.20}".format(
            station["marker"].upper(),
            station["name"][:18],
            time_from,
            time_to,
            antenna_height,
            antenna_reference_point,
            antenna_N,
            antenna_E,
            receiver_type[:21],
            firmware_version[:21],
            software_version[:6],
            receiver_SN[:21],
            antenna_type[:16],
            dome[:6],
            antenna_SN,
        )
        stationInfo_list.append(sessionLine)

    return stationInfo_list

    return session


def sessionsList(station, date_format="%Y-%m-%d %H:%M:%S"):
    """ """

    devices_list = []

    for item in station["device_history"]:
        if date_format:
            if item["time_from"] is None:
                time_from = "None"
            else:
                time_from = item["time_from"].strftime(date_format)

            if item["time_to"] is None:
                time_to = "None"
            else:
                time_to = item["time_to"].strftime(date_format)
        else:
            time_from = item["time_from"]
            time_to = item["time_to"]

        devices_list.append([time_from, time_to])

    return devices_list


def getStationList(subsets={}):
    """ """

    station_list = []
    keyorder = [
        "marker",
        "name",
        "date_from",
        "lon",
        "lat",
        "altitude",
        "operational_class",
        "date_to",
    ]
    stations = gpsqc.search_station(
        "GPS stöð", code="subtype", domains="geophysical", loglevel=logging.WARNING
    )
    for station in stations:
        sta_dict = {}
        for attribute in station["attributes"]:
            if attribute["code"] in ["marker", "operational_class", "name"]:
                sta_dict[attribute["code"]] = attribute["value"]
                if attribute["code"] == "marker":
                    try:
                        sta_dict["date_from"] = dt.strptime(
                            attribute["date_from"], "%Y-%m-%dT%H:%M:%S"
                        )
                    except:
                        sta_dict["date_from"] = None
                    try:
                        sta_dict["date_to"] = dt.strptime(
                            attribute["date_to"], "%Y-%m-%dT%H:%M:%S"
                        )
                    except:
                        sta_dict["date_to"] = None

            elif attribute["code"] in ["lat", "lon", "altitude"]:
                sta_dict[attribute["code"]] = float(attribute["value"])
        station_list.append({k: sta_dict[k] for k in keyorder if k in sta_dict})

    if subsets:
        LMI_station_list = [
            "akur",
            "gusk",
            "heid",
            "hofn",
            "isaf",
            "myva",
            "reyk",
            "alhv",
            "bjtv",
        ]
        HI_station_list = ["krac", "gonh", "ste2", "syrf", "thrc"]
        uknown_station_list = ["s001", "7058"]
        remove_list = LMI_station_list + HI_station_list + uknown_station_list

        tmp_list = []
        for item in station_list:
            if item["marker"] not in remove_list:
                tmp_list.append(item)

        station_list[:] = tmp_list

    return station_list


def print_station_list(station_list, sortby="marker"):
    """ """

    station_list[:] = sorted(station_list, key=itemgetter(sortby))
    keylist = [
        "marker",
        "name",
        "date_from",
        "lon",
        "lat",
        "altitude",
        "operational_class",
        "date_to",
    ]
    value_list = [list(item.values()) for item in station_list]

    # print(tabulate(value_list, headers=keylist))

    return station_list


def count_GPS_stations(station_list):
    """ """

    station_list[:] = sorted(station_list, key=itemgetter("date_from"))

    station_count = []
    station_counter = 0
    yearly_addition = total_in_year = 0
    last_item = station_list[0]["date_from"]
    for item in station_list:
        if item["date_from"].year > last_item.year:
            yearly_addition = station_counter - total_in_year
            # print("Total number of stations {}:\t{} stations added in {}".format(station_counter,yearly_addition, last_item.year))
            total_in_year = station_counter
            station_count.append([last_item.year, station_counter, yearly_addition])

        station_counter += 1
        last_item = item["date_from"]
    else:
        yearly_addition = station_counter - total_in_year
        station_count.append([item["date_from"].year, station_counter, yearly_addition])
        # print("Total number of stations {}:\t{} of stations added in {}".format(station_counter,yearly_addition, item['date_from'].year))

    keylist = ["Year", "Total #", "New #"]
    print(tabulate(station_count, headers=keylist))


def get_radome(device_iter, date_from, date_to, loglevel=logging.WARNING):
    """
    return monument_height for given interval
    """

    module_logger = get_logger(name=__name__)
    module_logger.setLevel(loglevel)
    # NOTE: default radome is NONE
    antenna_radome = "NONE"
    antenna_radome_serial = ""

    print("\n", file=sys.stderr)
    for item in device_iter:
        module_logger.debug("item: \n%s", json_print(item))
        device = item["device"]
        session_start = device["date_from"]
        session_end = device["date_to"]
        module_logger.warning("-" * 50)
        module_logger.warning("date input: %s - %s", date_from, date_to)
        module_logger.warning("current session: %s - %s", session_start, session_end)

        if date_to:
            if date_to > session_start:
                if session_end and date_from > session_end:
                    pass
                else:
                    antenna_radome = device["model"]
                    module_logger.warning("model: %s", antenna_radome)
        else:
            if session_end and session_end <= date_from:
                pass
            else:
                if date_from >= session_start:
                    antenna_radome = device["model"]
                    module_logger.warning("model: %s", antenna_radome)

    module_logger.warning("%s", "+" * 50)

    return antenna_radome, antenna_radome_serial


def get_monument_height(device_iter, date_from, date_to, loglevel=logging.WARNING):
    """
    return monument_heigt for given interval
    """

    module_logger = get_logger(name=__name__)
    module_logger.setLevel(loglevel)
    # NOTE: monument_height defaults to 0.0
    monument_height = 0.0

    print("", file=sys.stderr)
    for item in device_iter:
        module_logger.debug("monument_item: \n%s", json_print(item))
        device = item["device"]
        session_start = device["date_from"]
        session_end = device["date_to"]
        module_logger.debug("date_to: %s ", date_to)
        module_logger.warning("-" * 50)
        module_logger.warning("date input: %s - %s", date_from, date_to)
        module_logger.warning("current session: %s - %s", session_start, session_end)

        if date_to:
            if date_to > session_start:
                if session_end and date_from > session_end:
                    pass
                else:
                    monument_height = float(device["monument_height"])
                    module_logger.warning(
                        "monument_height: %s", device["monument_height"]
                    )
        else:
            if session_end and session_end < date_from:
                pass
            else:
                if date_from >= session_start:
                    monument_height = float(device["monument_height"])
                    module_logger.warning(
                        "monument_height: %s", device["monument_height"]
                    )

    module_logger.warning("%s", "+" * 50)

    return monument_height


def site_log(
    station_identifier,
    loglevel=logging.WARNING,
    report_type="UPDATE",
    previous_log="",
    modified_sections="1",
):
    """"""

    module_logger = get_logger(name=__name__)
    module_logger.setLevel(loglevel)

    module_logger.info(station_identifier)

    # station = gps_metadata(station_identifier, url_rest_tos, loglevel=logging.CRITICAL)
    station, devices_history = gpsqc.get_station_metadata(
        station_identifier, gpsqc.URL_REST_TOS, loglevel=loglevel
    )
    # [NOTE: testing device history]
    module_logger.info("devices_history: %s", json_print(devices_history))
    device_sessions = gpsqc.get_device_sessions(
        devices_history, gpsqc.URL_REST_TOS, loglevel=loglevel
    )

    # devices_used = ["gnss_receiver", "antenna", "radome", "monument"]
    module_logger.debug("deveces_sessions: %s", json_print(device_sessions))
    module_logger.debug("station: %s", json_print(station))

    # sessions_start = iter(sorted(session["device"]["date_from"] for session in device_sessions if session["device"]["code_entity_subtype"] == "gnss_receiver"))
    # sessions = list(
    #     session
    #     for session in device_sessions
    #     if session["device"]["code_entity_subtype"] == "gnss_receiver"
    # )
    # sessions.sort(key=lambda x: x["device"]["date_from"])
    # for session in sessions:
    #     module_logger.debug(
    #         "session: %s\n%s",
    #         session["device"]["code_entity_subtype"],
    #         json_print(session),
    #     )

    # NOTE: 1.   Site Identification of the GNSS Monument
    site_name = station.get("name", "")
    marker = station.get("marker", "").upper()
    iers_domes = station.get("iers_domes_number", "")
    cdp_num = station.get("cdp_num", "(A4)")
    monument_height = "(m)"
    monument_inscription = ""
    monument_description = ""
    foundation = ""
    foundation_depth = "(m)"

    monument_iter = (
        session
        for session in device_sessions
        if session["device"]["code_entity_subtype"] == "monument"
    )
    for item in monument_iter:
        if item["time_to"] is None:
            device = item.get("device", {})

            # monument_height_fl = float(device.get("monument_height", 0.0))
            monument_height_fl = device["monument_height"]
            if monument_height_fl is None:
                monument_height_fl = device["antenna_height"]

            if monument_height_fl is None:
                monument_height_fl = 0.0
            else:
                monument_height_fl = float(monument_height_fl)

            monument_offset_north = device["antenna_offset_north"]
            if monument_offset_north is None:
                monument_offset_north_fl = 0.0
            else:
                monument_offset_north_fl = float(monument_offset_north)

            monument_offset_east = device["antenna_offset_east"]
            if monument_offset_east is None:
                monument_offset_east_fl = 0.0
            else:
                monument_offset_east_fl = float(monument_offset_east)

            monument_height = f"{monument_height_fl} m"
            monument_inscription = device.get("inscription", "")
            monument_description = device.get("description", "STEEL MAST")
            foundation = device.get("foundation", "STEEL RODS")
            foundation_depth = device.get("foundation_depth", "(m)")
            if not foundation_depth == "(m)":
                foundation_depth = foundation_depth + " m"

    marker_description = station.get(
        "marker_description", "(CHISELLED CROSS/DIVOT/BRASS NAIL/etc)"
    )
    station_start_date = station.get("date_start", "")
    try:
        station_start_date = dt.strptime(station_start_date, "%Y-%m-%d %H:%M").strftime(
            "%Y-%m-%dT%H:%MZ"
        )
    except ValueError:
        station_start_date = dt.strptime(
            station_start_date[:19], "%Y-%m-%dT%H:%M:%S"
        ).strftime("%Y-%m-%dT%H:%MZ")
    geological_characteristic = station.get("geological_characteristic", "").upper()
    bedrock_type = station.get("bedrock_type", "").upper()
    bedrock_condition = station.get("bedrock_condition", "").upper()
    fracture_spacing = station.get(
        "fracture_spacing", "(0 cm/1-10 cm/11-50 cm/51-200 cm/over 200 cm)"
    )
    fault_zone = station.get("is_near_fault_zones", "NO").upper()
    # Translate Icelandic responses to English for IGS compliance
    if fault_zone == "NEI":
        fault_zone = "NO"
    elif fault_zone in ["JÁ", "JA"]:
        fault_zone = "YES"
    # Keep YES/NO unchanged

    # NOTE: 2.   Site Location Information
    llh = (station["lat"], station["lon"], station["altitude"])
    itrf = gpsqc.wgs84toitrf08.transform(*llh)
    coord_keys = ["X", "Y", "Z", "lat", "lon", "alt"]
    coordinates = dict(zip(coord_keys, (*itrf, *llh)))

    city = station.get("city", station["name"])
    state = station.get("state", "N/A")
    # Country name/code translation table for IGS compliance
    country_translation = {
        # Icelandic names
        "Ísland": "ISL",
        "Island": "ISL",
        # English names
        "Iceland": "ISL",
        # Nordic countries (common in region)
        "Norge": "NOR",
        "Norway": "NOR",
        "Danmark": "DNK",
        "Denmark": "DNK",
        "Sverige": "SWE",
        "Sweden": "SWE",
        "Suomi": "FIN",
        "Finland": "FIN",
        # Add more as needed
    }

    raw_country = station.get("country", "Iceland")
    # If it's already a 3-letter ISO code, use it; otherwise translate
    if len(raw_country) == 3 and raw_country.isupper():
        country = raw_country  # Already ISO 3166-1 alpha-3 code
    else:
        country = country_translation.get(raw_country, "ISL")  # Default to ISL
    tectonic_plate = station.get("tectonic_plate", "")
    if tectonic_plate == "":
        plate_name = {
            "EURA": "EURASIAN",
            "NOAM": "NORTH AMERICAN",
        }
        plate_short = grep_line_aslist(get_data_file_path("station-plate"), marker)[1]
        tectonic_plate = plate_name[plate_short] if plate_short != "" else "UNKNOWN"

    def decimal_to_dms(decimal_deg):
        """Convert decimal degrees to DDMMSS.SS format"""
        if not decimal_deg:
            return ""

        is_negative = decimal_deg < 0
        abs_deg = abs(decimal_deg)

        degrees = int(abs_deg)
        minutes = int((abs_deg - degrees) * 60)
        seconds = ((abs_deg - degrees) * 60 - minutes) * 60

        # Format as DDMMSS.SS or DDDMMSS.SS for longitude
        if abs_deg >= 100:  # longitude
            dms_str = f"{degrees:03d}{minutes:02d}{seconds:05.2f}"
        else:  # latitude
            dms_str = f"{degrees:02d}{minutes:02d}{seconds:05.2f}"

        return f"{'-' if is_negative else '+'}{dms_str}"

    x_coordinate = coordinates.get("X", "")
    y_coordinate = coordinates.get("Y", "")
    z_coordinate = coordinates.get("Z", "")
    latitude = decimal_to_dms(coordinates.get("lat", ""))
    longitude = decimal_to_dms(coordinates.get("lon", ""))
    elevation = coordinates.get("alt", "")

    # NOTE: 3.   GNSS Receiver Information
    receiver_list = list(
        session
        for session in device_sessions
        if session["device"]["code_entity_subtype"] == "gnss_receiver"
    )
    receiver_list.sort(key=lambda x: x["device"]["date_from"])
    receiver_info = "\n3.   GNSS Receiver Information\n\n"
    for session_nr, session in enumerate(receiver_list):
        device = session["device"]
        device_type = device.get("model", "")
        satellite_system = device.get("satellite_system", "GPS")
        serial_number = device.get("serial_number", "000000")
        firmware_version = device.get("firmware_version", "")
        elevation_cuttoff = device.get("elevation_cuttoff", "0 deg")
        date_installed = device["date_from"]
        if date_installed is None:
            date_installed = "CCYY-MM-DDThh:mmZ"
        else:
            date_installed = dt.strptime(date_installed, "%Y-%m-%dT%H:%M:%S").strftime(
                "%Y-%m-%dT%H:%MZ"
            )
        date_removed = device["date_to"]
        if date_removed is None:
            date_removed = "CCYY-MM-DDThh:mmZ"
        else:
            date_removed = dt.strptime(date_removed, "%Y-%m-%dT%H:%M:%S").strftime(
                "%Y-%m-%dT%H:%MZ"
            )
        temperature_stab = device.get("temperature_stab", "")
        add_information = device.get("add_information", "")

        receiver_info += (
            f"3.{session_nr + 1}  Receiver Type            : {device_type}\n"
            f"     Satellite System         : {satellite_system}\n"
            f"     Serial Number            : {serial_number}\n"
            f"     Firmware Version         : {firmware_version}\n"
            f"     Elevation Cutoff Setting : {elevation_cuttoff}\n"
            f"     Date Installed           : {date_installed}\n"
            f"     Date Removed             : {date_removed}\n"
            f"     Temperature Stabiliz.    : {temperature_stab}\n"
            f"     Additional Information   : {add_information}\n\n"
        )
    # print(receiver_info)

    # NOTE: 4.   GNSS Antenna Information
    antenna_list = list(
        session
        for session in device_sessions
        if session["device"]["code_entity_subtype"] == "antenna"
    )
    antenna_list.sort(key=lambda x: x["device"]["date_from"])
    module_logger.debug("antenna_list: \n%s", json_print(antenna_list))
    antenna_info = "\n4.   GNSS Antenna Information\n"
    for session_nr, session in enumerate(antenna_list):
        # antenna_height = 0.0
        device = session["device"]
        module_logger.debug("device: \n%s", json_print(device))

        device_type = device.get("model", "")
        serial_number = device.get("serial_number", "000000")
        arp = device.get("antenna_reference_point", "BPA")
        if arp == "DHARP":
            arp = grep_line_aslist(get_data_file_path("antenna_arp.list"), device_type)[
                1
            ]

        if device["monument_height"]:
            antenna_height = device["monument_height"]
        else:
            antenna_height = device["antenna_height"]

        if antenna_height is None:
            antenna_height = 0.0
        else:
            antenna_height = float(antenna_height)

        module_logger.debug("antenna_height: %s", antenna_height)
        monument_iter = (  # go through monent an pick the right monument_height
            session
            for session in device_sessions
            if session["device"]["code_entity_subtype"] == "monument"
        )
        monument_height_fl = get_monument_height(
            monument_iter, device["date_from"], device["date_to"]
        )
        module_logger.warning("monument_height_fl: %s", monument_height_fl)

        antenna_height = "{0:.4f}".format(antenna_height + monument_height_fl)

        antenna_offset_north = device["antenna_offset_north"]
        if antenna_offset_north is None:
            antenna_offset_north_fl = 0.0
        else:
            antenna_offset_north_fl = float(antenna_offset_north)
        antenna_offset_north = "{0:.4f}".format(
            antenna_offset_north_fl + monument_offset_north_fl
        )

        antenna_offset_east = device["antenna_offset_east"]
        if antenna_offset_east is None:
            antenna_offset_east_fl = 0.0
        else:
            antenna_offset_east_fl = float(antenna_offset_east)
        antenna_offset_east = "{0:.4f}".format(
            antenna_offset_east_fl + monument_offset_east_fl
        )

        alignment = device.get("antenna_alignment", "0 deg")
        # NOTE: radome is moved to the end of for loop as it needs end dates

        cable_type = device.get("antenna_cable_type", "")
        cable_length = device.get("antenna_cable_length", "")

        date_installed = device["date_from"]
        if date_installed is None:
            date_installed = "CCYY-MM-DDThh:mmZ"
        else:
            date_installed = dt.strptime(date_installed, "%Y-%m-%dT%H:%M:%S").strftime(
                "%Y-%m-%dT%H:%MZ"
            )
        date_removed = device["date_to"]
        if date_removed is None:
            date_removed = "CCYY-MM-DDThh:mmZ"
        else:
            date_removed = dt.strptime(date_removed, "%Y-%m-%dT%H:%M:%S").strftime(
                "%Y-%m-%dT%H:%MZ"
            )

        add_information = device.get("add_information", "")

        # NOTE: checking RADOME
        radome_iter = (
            session
            for session in device_sessions
            if session["device"]["code_entity_subtype"] == "radome"
        )

        antenna_radome, antenna_radome_serial = get_radome(
            radome_iter,
            device["date_from"],
            device["date_to"],
            loglevel=logging.WARNING,
        )
        module_logger.warning("antenna_radome: %s", antenna_radome)
        module_logger.warning("antenna_radome_serial: %s", antenna_radome_serial)

        antenna_info += (
            f"\n4.{session_nr + 1}  Antenna Type             : {device_type}    {antenna_radome}\n"
            # ASH701073.1     SNOW
            f"     Serial Number            : {serial_number}\n"
            f"     Antenna Reference Point  : {arp[-3:]}\n"
            f"     Marker->ARP Up Ecc. (m)  :   {antenna_height}\n"
            f"     Marker->ARP North Ecc(m) :   {antenna_offset_north}\n"
            f"     Marker->ARP East Ecc(m)  :   {antenna_offset_east}\n"
            f"     Alignment from True N    : {alignment}\n"
            f"     Antenna Radome Type      : {antenna_radome}\n"
            f"     Radome Serial Number     : {antenna_radome_serial}\n"
            f"     Antenna Cable Type       : {cable_type}\n"
            f"     Antenna Cable Length     : {cable_length}\n"
            f"     Date Installed           : {date_installed}\n"
            f"     Date Removed             : {date_removed}\n"
            f"     Additional Information   : {add_information}\n"
        )
    # print(antenna_info)

    other_info = (
        "\n5.   Surveyed Local Ties\n\n"
        "5.x  Tied Marker Name         : \n"
        "     Tied Marker Usage        : (SLR/VLBI/LOCAL CONTROL/FOOTPRINT/etc)\n"
        "     Tied Marker CDP Number   : (A4)\n"
        "     Tied Marker DOMES Number : (A9)\n"
        "     Differential Components from GNSS Marker to the tied monument (ITRS)\n"
        "       dx (m)                 : (m)\n"
        "       dy (m)                 : (m)\n"
        "       dz (m)                 : (m)\n"
        "     Accuracy (mm)            : (mm)\n"
        "     Survey method            : (GPS CAMPAIGN/TRILATERATION/TRIANGULATION/etc)\n"
        "     Date Measured            : (CCYY-MM-DDThh:mmZ)\n"
        "     Additional Information   : (multiple lines)\n\n\n"
        "6.   Frequency Standard\n\n"
        "6.1  Standard Type            : (INTERNAL or EXTERNAL H-MASER/CESIUM/etc)\n"
        "       Input Frequency        : (if external)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Notes                  : (multiple lines)\n\n"
        "6.x  Standard Type            : (INTERNAL or EXTERNAL H-MASER/CESIUM/etc)\n"
        "       Input Frequency        : (if external)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Notes                  : (multiple lines)\n\n\n"
        "7.   Collocation Information\n\n"
        "7.1  Instrumentation Type     : (GPS/GLONASS/DORIS/PRARE/SLR/VLBI/TIME/etc)\n"
        "       Status                 : (PERMANENT/MOBILE)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Notes                  : (multiple lines)\n\n"
        "7.x  Instrumentation Type     : (GPS/GLONASS/DORIS/PRARE/SLR/VLBI/TIME/etc)\n"
        "       Status                 : (PERMANENT/MOBILE)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Notes                  : (multiple lines)\n\n\n"
        "8.   Meteorological Instrumentation\n\n"
        "8.1.1 Humidity Sensor Model   : \n"
        "       Manufacturer           : \n"
        "       Serial Number          : \n"
        "       Data Sampling Interval : (sec)\n"
        "       Accuracy (% rel h)     : (% rel h)\n"
        "       Aspiration             : (UNASPIRATED/NATURAL/FAN/etc)\n"
        "       Height Diff to Ant     : (m)\n"
        "       Calibration date       : (CCYY-MM-DD)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Notes                  : (multiple lines)\n\n"
        "8.1.x Humidity Sensor Model   : \n"
        "       Manufacturer           : \n"
        "       Serial Number          : \n"
        "       Data Sampling Interval : (sec)\n"
        "       Accuracy (% rel h)     : (% rel h)\n"
        "       Aspiration             : (UNASPIRATED/NATURAL/FAN/etc)\n"
        "       Height Diff to Ant     : (m)\n"
        "       Calibration date       : (CCYY-MM-DD)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Notes                  : (multiple lines)\n\n"
        "8.2.1 Pressure Sensor Model   : \n"
        "       Manufacturer           : \n"
        "       Serial Number          : \n"
        "       Data Sampling Interval : (sec)\n"
        "       Accuracy               : (hPa)\n"
        "       Height Diff to Ant     : (m)\n"
        "       Calibration date       : (CCYY-MM-DD)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Notes                  : (multiple lines)\n\n"
        "8.2.x Pressure Sensor Model   : \n"
        "       Manufacturer           : \n"
        "       Serial Number          : \n"
        "       Data Sampling Interval : (sec)\n"
        "       Accuracy               : (hPa)\n"
        "       Height Diff to Ant     : (m)\n"
        "       Calibration date       : (CCYY-MM-DD)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Notes                  : (multiple lines)\n\n"
        "8.3.1 Temp. Sensor Model      : \n"
        "       Manufacturer           : \n"
        "       Serial Number          : \n"
        "       Data Sampling Interval : (sec)\n"
        "       Accuracy               : (deg C)\n"
        "       Aspiration             : (UNASPIRATED/NATURAL/FAN/etc)\n"
        "       Height Diff to Ant     : (m)\n"
        "       Calibration date       : (CCYY-MM-DD)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Notes                  : (multiple lines)\n\n"
        "8.3.x Temp. Sensor Model      : \n"
        "       Manufacturer           : \n"
        "       Serial Number          : \n"
        "       Data Sampling Interval : (sec)\n"
        "       Accuracy               : (deg C)\n"
        "       Aspiration             : (UNASPIRATED/NATURAL/FAN/etc)\n"
        "       Height Diff to Ant     : (m)\n"
        "       Calibration date       : (CCYY-MM-DD)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Notes                  : (multiple lines)\n\n"
        "8.4.1 Water Vapor Radiometer  : \n"
        "       Manufacturer           : \n"
        "       Serial Number          : \n"
        "       Distance to Antenna    : (m)\n"
        "       Height Diff to Ant     : (m)\n"
        "       Calibration date       : (CCYY-MM-DD)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Notes                  : (multiple lines)\n\n"
        "8.4.x Water Vapor Radiometer  : \n"
        "       Manufacturer           : \n"
        "       Serial Number          : \n"
        "       Distance to Antenna    : (m)\n"
        "       Height Diff to Ant     : (m)\n"
        "       Calibration date       : (CCYY-MM-DD)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Notes                  : (multiple lines)\n\n"
        "8.5.1 Other Instrumentation   : (multiple lines)\n\n"
        "8.5.x Other Instrumentation   : (multiple lines)\n\n\n"
        "9.  Local Ongoing Conditions Possibly Affecting Computed Position\n\n"
        "9.1.1 Radio Interferences     : (TV/CELL PHONE ANTENNA/RADAR/etc)\n"
        "       Observed Degradations  : (SN RATIO/DATA GAPS/etc)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Additional Information : (multiple lines)\n\n"
        "9.1.x Radio Interferences     : (TV/CELL PHONE ANTENNA/RADAR/etc)\n"
        "       Observed Degradations  : (SN RATIO/DATA GAPS/etc)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Additional Information : (multiple lines)\n\n"
        "9.2.1 Multipath Sources       : (METAL ROOF/DOME/VLBI ANTENNA/etc)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Additional Information : (multiple lines)\n\n"
        "9.2.x Multipath Sources       : (METAL ROOF/DOME/VLBI ANTENNA/etc)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Additional Information : (multiple lines)\n\n"
        "9.3.1 Signal Obstructions     : (TREES/BUILDINGS/etc)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Additional Information : (multiple lines)\n\n"
        "9.3.x Signal Obstructions     : (TREES/BUILDINGS/etc)\n"
        "       Effective Dates        : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "       Additional Information : (multiple lines)\n\n"
        "10.  Local Episodic Effects Possibly Affecting Data Quality\n\n"
        "10.1 Date                     : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "     Event                    : (TREE CLEARING/CONSTRUCTION/etc)\n\n"
        "10.x Date                     : (CCYY-MM-DD/CCYY-MM-DD)\n"
        "     Event                    : (TREE CLEARING/CONSTRUCTION/etc)\n"
    )

    # NOTE: 11.  On-Site, Point of Contact Agency Information
    contact = station["contact"]["contact"]
    module_logger.debug("contact: \n%s", json_print(contact))

    agency = contact.get("name_en", "")
    address = contact.get("address_en", "")
    abbreviation = contact.get("abbreviation", "")
    phone_primary = contact.get("phone_primary", "")
    email = contact.get("email", "")
    primary_contact = contact.get("primary_contact", "")
    department = contact.get("department", "")

    contact_info = (
        f"\n\n11.   On-Site, Point of Contact Agency Information\n\n"
        f"     Agency                   : {agency}\n"
        f"                              : {department}\n"
        f"     Preferred Abbreviation   : {abbreviation}\n"
        f"     Mailing Address          : {address}\n"
        f"     Primary Contact            \n"
        f"       Contact Name           : {primary_contact}\n"
        f"       Telephone (primary)    : +354 {phone_primary}\n"
        f"       Telephone (secondary)  : \n"
        f"       Fax                    : \n"
        f"       E-mail                 : {email}\n"
        f"     Secondary Contact          \n"
        f"       Contact Name           : \n"
        f"       Telephone (primary)    : \n"
        f"       Telephone (secondary)  : \n"
        f"       Fax                    : \n"
        f"       E-mail                 : \n"
        f"     Additional Information   : (multiple lines)"
    )

    # NOTE: 12. Responsible Agency (if different from 11.)
    if (
        station["contact"]["contact"]["id_entity"]
        == station["contact"]["operator"]["id_entity"]
    ):
        contact = {}

        agency = contact.get("name_en", "(multiple lines)")
        address = contact.get("address_en", "(multiple lines)")
        abbreviation = contact.get("abbreviation", "(A10)")
        phone_primary = contact.get("phone_primary", "")
        if phone_primary != "":
            phone_primary = +354 + phone_primary
        email = contact.get("email", "")
        primary_contact = contact.get("primary_contact", "")
        department = contact.get("department", "")
    else:
        contact = station["contact"]["operator"]
        module_logger.debug("contact: \n%s", json_print(contact))

        agency = contact.get("name_en", "")
        address = contact.get("address_en", "")
        abbreviation = contact.get("abbreviation", "")
        phone_primary = contact.get("phone_primary", "")
        email = contact.get("email", "")
        primary_contact = contact.get("primary_contact", "")
        department = contact.get("department", "")

    operator_info = (
        f"\n\n\n12.  Responsible Agency (if different from 11.)\n\n"
        f"     Agency                   : {agency}\n"
        f"     Preferred Abbreviation   : {abbreviation}\n"
        f"     Mailing Address          : {address}\n"
        f"     Primary Contact            \n"
        f"       Contact Name           : {primary_contact}\n"
        f"       Telephone (primary)    : {phone_primary}\n"
        f"       Telephone (secondary)  : \n"
        f"       Fax                    : \n"
        f"       E-mail                 : {email}\n"
        f"     Secondary Contact          \n"
        f"       Contact Name           : \n"
        f"       Telephone (primary)    : \n"
        f"       Telephone (secondary)  : \n"
        f"       Fax                    : \n"
        f"       E-mail                 : \n"
        f"     Additional Information   : (multiple lines)"
    )

    # NOTE: 13.  More Information
    operator = station["contact"]["operator"]
    primary_data_center = operator.get("abbreviation", "")
    primary_contact = operator.get("primary_contact", "")
    email = operator.get("email", "")

    if (
        station["contact"]["operator"]["id_entity"]
        != station["contact"]["owner"]["id_entity"]
    ):
        secondary_data_center = station["contact"]["owner"].get("abbreviation", "")
    else:
        secondary_data_center = ""
    main_url = operator["main_url_en"]
    map_url = ""

    more_info = (
        f"\n\n\n13.  More Information\n\n"
        f"     Primary Data Center      : {primary_data_center}\n"
        f"     Secondary Data Center    : {secondary_data_center}\n"
        f"     URL for More Information : {main_url}\n"
        f"     Hardcopy on File\n"
        f"       Site Map               : {map_url}\n"
        f"       Site Diagram           : (Y or URL)\n"
        f"       Horizon Mask           : (Y or URL)\n"
        f"       Monument Description   : (Y or URL)\n"
        f"       Site Pictures          : (Y or URL)\n"
        f"     Additional Information   : (multiple lines)\n"
        f"     Antenna Graphics with Dimensions"
    )

    module_logger.debug("monument_height: %s", monument_height)

    # Set default contact info for header
    primary_contact = "GNSS Operator"
    email = "gnss-epos@vedur.is"

    ascii_site_log = (
        f"     {marker}00ISL Site Information Form (site log v2.0)\n"
        f"     International GNSS Service\n"
        f"     See Instructions at:\n"
        f"       https://files.igs.org/pub/station/general/sitelog_instr_v2.0.txt\n\n\n"
        f"0.   Form\n\n"
        f"     Prepared by (full name)  : {primary_contact} ({email})\n"
        f"     Date Prepared            : {dt.now().strftime('%Y-%m-%d')}\n"
        f"     Report Type              : {report_type}\n"
        f"     If Update:\n"
        f"      Previous Site Log       : {previous_log}\n"
        f"      Modified/Added Sections : {modified_sections}\n\n\n"
        f"1.   Site Identification of the GNSS Monument\n\n"
        f"     Site Name                : {site_name}\n"
        f"     Nine Character ID        : {marker}00ISL\n"
        f"     Monument Inscription     : {monument_inscription}\n"
        f"     IERS DOMES Number        : {iers_domes}\n"
        f"     CDP Number               : {cdp_num}\n"
        f"     Monument Description     : {monument_description}\n"
        f"       Height of the Monument : {monument_height}\n"
        f"       Monument Foundation    : {foundation}\n"
        f"       Foundation Depth       : {foundation_depth}\n"
        f"     Marker Description       : {marker_description}\n"
        f"     Date Installed           : {station_start_date}\n"
        f"     Geologic Characteristic  : {geological_characteristic}\n"
        f"       Bedrock Type           : {bedrock_type}\n"
        f"       Bedrock Condition      : {bedrock_condition}\n"
        f"       Fracture Spacing       : {fracture_spacing}\n"
        f"       Fault zones nearby     : {fault_zone}\n"
        f"         Distance/activity    : \n"
        f"     Additional Information   : \n\n\n"
        f"2.   Site Location Information\n\n"
        f"     City or Town             : {city}\n"
        f"     State or Province        : {state}\n"
        f"     Country or Region        : {country}\n"
        f"     Tectonic Plate           : {tectonic_plate}\n"
        f"     Approximate Position (ITRF)\n"
        f"       X coordinate (m)       : {x_coordinate:.3f}\n"
        f"       Y coordinate (m)       : {y_coordinate:.3f}\n"
        f"       Z coordinate (m)       : {z_coordinate:.3f}\n"
        f"       Latitude (N is +)      : {latitude}\n"
        f"       Longitude (E is +)     : {longitude}\n"
        f"       Elevation (m,ellips.)  : {elevation:.1f}\n"
        f"     Additional Information   : \n\n"
        f"{receiver_info}"
        f"{antenna_info}"
        f"{other_info}"
        f"{contact_info}"
        f"{operator_info}"
        f"{more_info}"
    )

    # print(ascii_site_log)
    return ascii_site_log


def domes_info_form(station_identifier, loglevel=logging.WARNING):
    """
    print domes info form
    """

    module_logger = get_logger(name=__name__)
    module_logger.setLevel(loglevel)

    module_logger.info(station_identifier)

    # station = gps_metadata(station_identifier, url_rest_tos, loglevel=logging.CRITICAL)
    station, devices_history = gpsqc.get_station_metadata(
        station_identifier, gpsqc.URL_REST_TOS, loglevel=loglevel
    )
    device_sessions = gpsqc.get_device_sessions(
        devices_history, gpsqc.URL_REST_TOS, loglevel=loglevel
    )

    # devices_used = ["gnss_receiver", "antenna", "radome", "monument"]
    module_logger.info("station: %s", json_print(station))

    # DOMES INFORMATION FORM (DIF)

    # 1. Request from (full name) : Mr. Thorarinn Sigurdsson
    #     Agency                   : National Land Survey of Iceland
    #     E-mail                   : thorarinn.sigurdsson@lmi.is or lmi@lmi.is
    #     Date                     : 28.10.2021
    #
    # 2. Site Name                 : Fiflholt
    # 3. Country                   : Iceland
    # 4. Point Description         : The station is at the east site of Iceland on the
    #                              : North America tectonic plate. The antenna is mounted
    #                              : on a stainless steel quadripod, that is bolted
    #                              : and cemented into stable bedrock. The top of
    #                              : the quadripod is the ARP.
    #
    #  5. DOMES Number             :
    #  6. Local Number             : FIHO
    #  7. 4-Char Code              :
    #  8. Approximate Position
    #     Latitude (deg min)       : 064° 41.661'
    #     Longitude (deg min)      : 337° 51.121'
    #     Elevation (m)            : 125.2 m
    #  9. Instrument               : Rec.: Trimble NetR5, serial nr. 4806K53396
    #                  : Ant.: Navxperience 3G+C, serial nr. NA02473
    #
    #
    # 10. Date of Installation     : 18.06.2021
    # 11. Operation Contact Name   : Mr. Thorarinn Sigurdsson
    #     Agency                   : National Land Survey of Iceland
    #     E-mail                   : thorarinn.sigurdsson@lmi.is
    # 12. Site Contact Name        : Same as the operation contact person
    #     Agency                   :
    #     E-mail                   :


def file_list(
    station,
    pdir,
    start=None,
    end=None,
    freqd="15s_24hr",
    rawdir="rinex",
    fform="#Rin2",
    DZend="D.Z",
    loglevel=logging.WARNING,
):
    """
    Returns a list of potential station RINEX files from a given station dictionary as returned by gps_metadata()
    grouped according to station sessions.
    input:
        station:
    """

    # logging settings
    module_logger = get_logger(name=__name__)
    module_logger.setLevel(loglevel)

    filesList = []
    stat = station["marker"].upper()
    formatString = (
        pdir
        + "/%Y/#b/"
        + stat
        + "/"
        + freqd
        + "/"
        + rawdir
        + "/"
        + stat
        + fform
        + DZend
    )

    module_logger.info("Initial period: {}\t{}\n".format(start, end) + "*" * 50)

    for item in station["device_history"]:
        module_logger.info(
            "Session period: {}\t{}".format(item["time_from"], item["time_to"])
        )

        flist = []
        session_flag = True
        if item["time_to"] is None:
            time_to = tf.currDatetime(days=-1)
        else:
            time_to = item["time_to"]

        if item["time_from"] is not None:
            time_from = item["time_from"]

        if start is not None:
            if time_to < start:
                session_flag = False

            if time_from <= start:
                time_from = start

        if end is not None:
            if end < time_from:
                session_flag = False

            if end < time_to:
                time_to = end

        module_logger.info("Current period: {}\t{}".format(time_from, time_to))
        session_nr = station["device_history"].index(item)
        module_logger.info("Index number: {}".format(session_nr))

        if session_flag:
            flist = tf.datepathlist(
                formatString, "1D", time_from, time_to, closed="left"
            )
            # Add one day to compensate for edge effect of open 'right' boundaries used in datepathlist
            # But not if last day is to day i.e end is
            endfile = PurePath(flist[-1]).name
            endfile_date = datefRinex([endfile])[0]
            if (
                time_to - endfile_date == timedelta(1)
                and time_to != item["time_to"]
                and end is not None
            ):
                module_logger.debug("{}".format(time_to - endfile_date))
                flist.append(
                    tf.datepathlist(formatString, "1D", end, end, closed="left")[0]
                )

            filesList.append(
                {
                    "marker": stat,
                    "session_number": session_nr,
                    "time_from": item["time_from"],
                    "time_to": item["time_to"],
                    "filelist": flist,
                }
            )

    if module_logger.getEffectiveLevel() <= 10 and filesList:
        for flist in filesList:
            module_logger.debug(
                "Station: {}, Session number: {}".format(
                    flist["marker"], flist["session_number"]
                )
            )
            module_logger.debug("{}\t{}".format(flist["time_from"], flist["time_to"]))

            if flist["filelist"]:
                module_logger.debug(flist["filelist"][0])
                module_logger.debug(flist["filelist"][-1])
            else:
                module_logger.debug(flist["filelist"])
    else:
        module_logger.debug(
            "filesList empty, logging level: {}\tfilesList: {}".format(
                module_logger.getEffectiveLevel(), filesList
            )
        )

    return filesList


# NOTE: extra functions
def get_logger(name=__name__):
    """
    logger to use within the modules
    """

    # Create log handler
    logHandler = logging.StreamHandler()
    # logHandler.setLevel(level)

    # Set handler format
    logFormat = logging.Formatter("[%(levelname)s] %(funcName)s: %(message)s")
    logHandler.setFormatter(logFormat)

    # Create logger
    logger = logging.getLogger(name)
    # logger.setLevel(level)

    if logger.hasHandlers():
        logger.handlers.clear()
    # Add handler to logger
    logger.addHandler(logHandler)

    # Stop propagating the log messages to root logger
    logger.propagate = False

    return logger


def grep_line_aslist(listf, text):
    """
    grep a line from list
    """
    with open(listf, "r") as f:
        for line in f:
            if text in line:
                return line.split()
        else:
            return [text, ""]


def json_print(json_struct):
    """
    print json nicely
    """
    return json.dumps(json_struct, cls=CustomeJSONEncoder, indent=2)


class CustomeJSONEncoder(json.JSONEncoder):
    """
    encoder for dealing with posixpath in json.dumps
    """

    def default(self, obj):
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, dt):
            return obj.isoformat()
        # Let the base class default method raise the TypeError
        return super().default(obj)


def main():
    """ """

    station_list = getStationList()

    sorted_station_list = print_station_list(station_list, sortby="marker")
    ISGPS = pd.DataFrame(sorted_station_list)
    ISGPS.set_index("marker", inplace=True)
    # isgps["date_from"] = pd.to_datetime(isgps["date_from"], errors="coerce")
    # isgps = isgps[isgps["date_from"] < dt(2018, 1, 1)]
    print(ISGPS[["name", "date_from", "lon", "lat"]])
    ISGPS[["name", "date_from", "lon", "lat"]].to_csv("stations.list", sep="\t")

    # count_GPS_stations(station_list)

    antenna = "ASH701945C_M"
    antennaf = "antenna_arp.list"
    # marker = "TREE"
    # platefile = "./station-plate"
    # print(grep_line_aslist(platefile, marker))


if __name__ == "__main__":
    main()
