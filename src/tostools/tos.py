#!/usr/bin/python3
#
# Project: TOSTools
# Authors: Tryggvi Hjörvar
# Date: Feb 2020
#
# Module to simplify query:ing TOS
#
# Usage:
#  Command-line: tos.py <identifier> -s -o <format> -t <tablefmt>
#  Help:  tos.py -h
#
# Examples:
#   tos.py ada --schema_version 0.9 --sc3ml --compareto ..\..\gempa\sil_VI-orfeus-editedSH.xml
#       ada asb fag gil god skr
#
#   TODO:
#   Fjarlægð
#   Næstu
#   Myndir í PDF
#   Devices
#   History
#


# import os
import argparse
import copy
import json

# import stat
import logging
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List

import requests
from tabulate import tabulate

from .xmltools import compareSC3

url_rest_tos = "https://vi-api.vedur.is:11223/tos/v1"

# Set logging
# NOTE: Commented out to avoid interfering with main logging configuration
# logging.basicConfig(
#     level=logging.INFO, format="%(levelname)s %(message)s"
# )  # Formatting


def searchStation(station_identifier, url_rest, domains=None):
    if not isinstance(url_rest, str) or not url_rest.startswith(
        ("http://", "https://")
    ):
        raise ValueError(
            f"searchStation: url_rest must be a TOS base URL like "
            f"{url_rest_tos!r}; got {url_rest!r}. Argument order is "
            "(station_identifier, url_rest, domains)."
        )

    if domains is None:
        domains = [
            "meteorological",
            "geophysical",
            "hydrological",
            "remote_sensing",
            "remote_sensing_platform",
            "general",
        ]
        logging.info("No domains specified, searcing " + str(domains))
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
        logging.info(f"Including lowercase search for {station_identifier.lower()}")

    # Remove padding 0 in search for VM
    if station_identifier[0:2] == "V0":
        station_identifiers += ["V" + station_identifier[2:]]
        logging.info("Including unpadded search for " + "V" + station_identifier[2:])

    stations = []
    for station_identifier in station_identifiers:
        for domain in domains:
            # Construct POST query
            body = {"code": "marker", "value": station_identifier}

            if domain == "remote_sensing_platform":
                entity_type = "platform"
            else:
                entity_type = "station"

            # Query TOS api
            response = requests.post(
                url_rest + "/entity/search/" + entity_type + "/" + domain + "/",
                data=json.dumps(body),
            )
            response.raise_for_status()
            if response.content:
                # data={}
                for station in response.json():
                    # data['domain'] = domain
                    #
                    ##Find current attributes
                    # data['station_identifier'] = next((item for item in station['attributes'] if (item['code'] == 'marker' and item['date_to'] is None)), None)['value']
                    # data['subtype'] = next((item for item in station['attributes'] if (item['code'] == 'subtype' and item['date_to'] is None)), None)['value']
                    #
                    # if domain=='hydrological':
                    #    value = next((item for item in station['attributes'] if (item['code'] == 'lat_isn93' and item['date_to'] is None)), {'value': None})['value']
                    #    if value:
                    #        data['lat_isn93'] = float(value)
                    #    value = next((item for item in station['attributes'] if (item['code'] == 'lon_isn93' and item['date_to'] is None)), {'value': None})['value']
                    #    if value:
                    #        data['lon_isn93'] = float(value)
                    # else:
                    #    value = next((item for item in station['attributes'] if (item['code'] == 'lat' and item['date_to'] is None)), {'value': None})['value']
                    #    if value:
                    #        data['lat'] = float(value)
                    #    value = next((item for item in station['attributes'] if (item['code'] == 'lon' and item['date_to'] is None)), {'value': None})['value']
                    #    if value:
                    #        data['lon'] = float(value)

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
                            # station['lat'] = next((item for item in location['attributes'] if (item['code'] == 'lat' and item['date_to'] is None)), {'value': None})
                            # station['lon'] = next((item for item in location['attributes'] if (item['code'] == 'lon' and item['date_to'] is None)), {'value': None})
                            # value = next((item for item in location['attributes'] if (item['code'] == 'lat' and item['date_to'] is None)), {'value': None})['value']
                            # if value:
                            #    station['lat'] = float(value)
                            # value = next((item for item in location['attributes'] if (item['code'] == 'lon' and item['date_to'] is None)), {'value': None})['value']
                            # if value:
                            #    station['lon'] = float(value)

                    stations.append(station)
                    # stations.append(data)

    return stations


def getDevicesByParentEntityId(id_entity, subtypes=None):
    devices = []

    # Query TOS api
    response = requests.get(
        url_rest_tos + "/entity/get_children/parent/" + str(id_entity) + "/"
    )
    response.raise_for_status()
    if response.content:
        for device in response.json():
            if subtypes:
                if device["code_entity_subtype"] in subtypes:
                    devices.append(device)
            else:
                devices.append(device)

    return devices


# def getDeviceHistoryByEntityId(id_entity, id_entity_parent):
#    history = []
#
#    #Query TOS api
#    response = requests.get(url_rest_tos+'/entity/parent_history/'+str(id_entity)+'/')
#    response.raise_for_status()
#    if response.content:
#        for connection in response.json():
#            if connection['id_entity_parent'] == id_entity_parent:
#                history.append(connection)
#
#    return history


def getDeviceSessions(id_entity):
    sessions = []
    devices_history = []
    # Query TOS api
    response = requests.get(url_rest_tos + "/history/entity/" + str(id_entity) + "/")
    response.raise_for_status()
    if response.content:
        devices_history = response.json()

    device_sessions = []
    # Get devices and filter selected ['digitizer','seismometer', 'seismic_sensor']
    if devices_history["children_connections"] is None:
        logging.critical("No device sessions found")
    else:
        for connection in devices_history["children_connections"]:
            response = requests.get(
                url_rest_tos + "/entity/" + str(connection["id_entity_child"]) + "/"
            )
            response.raise_for_status()
            if response.content:
                device = response.json()
                if device["code_entity_subtype"] in [
                    "digitizer",
                    "seismometer",
                    "seismic_sensor",
                ]:
                    # serial_number = next((item for item in device['attributes'] if (item['code'] == 'serial_number' and item['date_to'] is None)), None)['value']
                    # model = next((item for item in device['attributes'] if (item['code'] == 'model' and item['date_to'] is None)), None)['value']
                    # sensor_sensitivity = next((item for item in device['attributes'] if (item['code'] == 'sensor_sensitivity' and item['date_to'] is None)), None)['value']
                    # connection['device'] = device
                    connection["device"] = {
                        "code_entity_subtype": device["code_entity_subtype"],
                        "serial_number": next(
                            (
                                item
                                for item in device["attributes"]
                                if (
                                    item["code"] == "serial_number"
                                    and item["date_to"] is None
                                )
                            ),
                            {"value": None},
                        )["value"],
                        "model": next(
                            (
                                item
                                for item in device["attributes"]
                                if (item["code"] == "model" and item["date_to"] is None)
                            ),
                            {"value": None},
                        )["value"],
                        "sensor_sensitivity": next(
                            (
                                item
                                for item in device["attributes"]
                                if (
                                    item["code"] == "sensor_sensitivity"
                                    and item["date_to"] is None
                                )
                            ),
                            {"value": None},
                        )["value"],
                    }
                    device_sessions.append(connection)

        # Sort by time_from
        device_sessions.sort(key=lambda d: d["time_from"])

        # Create sessions
        device_slots = {
            "digitizer": None,
            "seismic_sensor": None,
            "seismometer": None,
        }

        for device_session in device_sessions:
            code_entity_subtype = device_session["device"]["code_entity_subtype"]
            device_slots[code_entity_subtype] = device_session
            if code_entity_subtype == "seismometer":
                time_from = device_slots["seismometer"]["time_from"]
                time_to = device_slots["seismometer"]["time_to"]
                sessions.append(
                    {
                        "time_from": time_from,
                        "time_to": time_to,
                        "seismometer": device_slots["seismometer"],
                    }
                )
            else:
                if device_slots["digitizer"] and device_slots["seismic_sensor"]:
                    time_from = datetime.strftime(
                        max(
                            datetime.strptime(
                                device_slots["digitizer"]["time_from"],
                                "%Y-%m-%dT%H:%M:%S",
                            ),
                            datetime.strptime(
                                device_slots["seismic_sensor"]["time_from"],
                                "%Y-%m-%dT%H:%M:%S",
                            ),
                        ),
                        "%Y-%m-%dT%H:%M:%S",
                    )
                    time_to = None
                    if device_slots["digitizer"]["time_to"]:
                        time_to = device_slots["digitizer"]["time_to"]
                    if device_slots["seismic_sensor"]["time_to"]:
                        if time_to is None:
                            time_to = device_slots["seismic_sensor"]["time_to"]
                        else:
                            time_to = datetime.strftime(
                                min(
                                    datetime.strptime(
                                        device_slots["digitizer"]["time_to"],
                                        "%Y-%m-%dT%H:%M:%S",
                                    ),
                                    datetime.strptime(
                                        device_slots["seismic_sensor"]["time_to"],
                                        "%Y-%m-%dT%H:%M:%S",
                                    ),
                                ),
                                "%Y-%m-%dT%H:%M:%S",
                            )
                    sessions.append(
                        {
                            "time_from": time_from,
                            "time_to": time_to,
                            "digitizer": device_slots["digitizer"],
                            "seismic_sensor": device_slots["seismic_sensor"],
                        }
                    )

    return sessions


def getEntity(id_entity):
    response = requests.get(url_rest_tos + "/entity/" + str(id_entity) + "/")
    response.raise_for_status()

    if response.content:
        data = response.json()
        return data
    else:
        return None


def searchDevice(serial_number=None, galvos=None):
    devices = []
    if serial_number:
        search_term = serial_number
        search_code = "serial_number"
    elif galvos:
        search_term = galvos
        search_code = "galvos"
    else:
        logging.critical("No serial_number or Galvos number")
        sys.exit(1)

    # Construct POST query
    body = {"search_term": str(search_term)}
    # Query TOS api
    response = requests.post(url_rest_tos + "/basic_search/", data=json.dumps(body))
    response.raise_for_status()
    if response.content:
        # Make unique
        unique = {}
        for search in response.json():
            if search["distance"] == 0 and search["code"] == search_code:
                unique[search["value_varchar"]] = search

        for value_varchar, search in unique.items():
            if search["distance"] == 0 and search["code"] == search_code:
                id_entity_device = search["id_lvl_three"]
                # Query TOS api for device
                response_device = requests.get(
                    url_rest_tos + "/entity/" + str(id_entity_device) + "/"
                )
                response_device.raise_for_status()
                if response_device.content:
                    device = response_device.json()
                    # Add attributes
                    # for attribute in device['attributes']:
                    #    #if attribute['date_to'] is None:
                    #    device['attributes'].append(attribute)

                # Get current station/platform
                # NOTE: due to a bug in TOS we must use the result from the basic_search endpoint
                # https://git.vedur.is/AOT/tos/issues/277
                if "id_lvl_two" in search:
                    device["id_entity_parent"] = search["id_lvl_two"]
                    parent = getEntity(device["id_entity_parent"])  # station/platform
                    if parent:
                        device["station"] = []
                        device["station"].append(
                            next(
                                (
                                    item
                                    for item in parent["attributes"]
                                    if (
                                        item["code"] == "name"
                                        and item["date_to"] is None
                                    )
                                ),
                                {"value": None},
                            )
                        )
                        device["station"].append(
                            next(
                                (
                                    item
                                    for item in parent["attributes"]
                                    if (
                                        item["code"] == "marker"
                                        and item["date_to"] is None
                                    )
                                ),
                                {"value": None},
                            )
                        )
                        # data['station_name'] = next((item for item in parent['attributes'] if (item['code'] == 'name' and item['date_to'] is None)), None)['value']
                        # data['station_identifier'] = next((item for item in parent['attributes'] if (item['code'] == 'marker' and item['date_to'] is None)), None)['value']

                        lat = next(
                            (
                                item
                                for item in parent["attributes"]
                                if (item["code"] == "lat" and item["date_to"] is None)
                            ),
                            None,
                        )
                        if lat:
                            device["station"].append(lat)
                        lon = next(
                            (
                                item
                                for item in parent["attributes"]
                                if (item["code"] == "lon" and item["date_to"] is None)
                            ),
                            None,
                        )
                        if lon:
                            device["station"].append(lon)

                        # device['station'].append( next((item for item in parent['attributes'] if (item['code'] == 'lat' and item['date_to'] is None)), {'value': None}) )
                        # device['station'].append( next((item for item in parent['attributes'] if (item['code'] == 'lon' and item['date_to'] is None)), {'value': None}) )

                        # NOTE: due to the code_entity_type missing from the API we must use entity_type_name_en:
                        # https://git.vedur.is/AOT/tos/-/issues/291
                        # if parent['entity_type_name_en']=='station':

                        # value = next((item for item in parent['attributes'] if (item['code'] == 'lat' and item['date_to'] is None)), {'value': None})['value']
                        # if value:
                        #    data['lat'] = float(value)
                        # value = next((item for item in parent['attributes'] if (item['code'] == 'lon' and item['date_to'] is None)), {'value': None})['value']
                        # if value:
                        #    data['lon'] = float(value)

                        # Get current location for remote_sensing_platform
                        if parent["code_entity_subtype"] == "remote_sensing_platform":
                            # NOTE: due to a bug in TOS we must use the result from the basic_search endpoint
                            # https://git.vedur.is/AOT/tos/issues/277
                            location = getEntity(search["id_lvl_one"])
                            if location:
                                device["location"] = []
                                device["location"].append(
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
                                device["location"].append(
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
                                device["location"].append(
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
                                # data['location_name'] = next((item for item in location['attributes'] if (item['code'] == 'name' and item['date_to'] is None)), None)['value']

                            # value = next((item for item in location['attributes'] if (item['code'] == 'lat' and item['date_to'] is None)), {'value': None})['value']
                            # if value:
                            #    data['lat'] = float(value)
                            # value = next((item for item in location['attributes'] if (item['code'] == 'lon' and item['date_to'] is None)), {'value': None})['value']
                            # if value:
                            #    data['lon'] = float(value)

                devices.append(device)

    return devices


def lookupMasterDataloggerXML(device):
    # <datalogger name="VI_Guralp_G24e_1000_500_100_MK3" publicID="Datalogger#20150925141352.76545.33138">
    #    <description>3,2uV/bit</description>
    #    <digitizerModel>CMG_DM24</digitizerModel>
    #    <digitizerManufacturer>Guralp</digitizerManufacturer>
    #    <recorderModel>MK3</recorderModel>
    #    <recorderManufacturer>Guralp</recorderManufacturer>
    #    <gain>1</gain>
    #    <maxClockDrift>1</maxClockDrift>
    #    <decimation sampleRateDenominator="1" sampleRateNumerator="100">
    #        <digitalFilterChain>ResponseFIR#20151008175711.330075.76957 ResponseFIR#20151008175711.773266.76958 ResponseFIR#20151008175712.429604.76959 ResponseFIR#20151008175712.814358.76960 ResponseFIR#20151008175713.368669.76961 ResponseFIR#20151008175713.761908.76962 ResponseFIR#20151008175714.255511.76963 ResponseFIR#20151008175714.73044.76964 ResponseFIR#20151008175715.086604.76965</digitalFilterChain>
    #    </decimation>
    # </datalogger>

    # print('model',device['model'])
    # print('sensor_sensitivity',sensor_sensitivity)

    regex = re.compile("({http.*})Inventory")
    match = regex.search(master_inventoryXML.tag)
    if match:
        ns = match.group(1)
    else:
        raise ValueError("Invalid SC3ML file")
        sys.exit(1)

    dataloggers = master_inventoryXML.findall(ns + "datalogger")
    # datalogger = master_inventory.find(ns+"datalogger[@name='VI_Guralp_G24e_1000_500_100_MK3']")

    # regex = re.compile('^({http.*(\d.\d{1,2})})')
    is_found = False
    for dataloggerXML in dataloggers:
        if device["model"] == "DM24-S3":
            sensor_sensitivity = device["sensor_sensitivity"].replace(".", ",")
            description = dataloggerXML.find(ns + "description")
            if description is not None and description.text.startswith(
                sensor_sensitivity
            ):
                dataloggerXML.attrib["name"]
                publicID = dataloggerXML.attrib["publicID"]
                is_found = True
                break
        elif device["model"] == "Minimus":
            sensor_sensitivity = device["sensor_sensitivity"].replace(".", ",")
            description = dataloggerXML.find(ns + "description")
            if description is not None and description.text.startswith(
                sensor_sensitivity
            ):
                dataloggerXML.attrib["name"]
                publicID = dataloggerXML.attrib["publicID"]
                is_found = True
                break
        elif device["model"] == "CMG-3TD 120s - 50Hz":
            # Model fixes
            model = "CMG3TD"
            modelXML = dataloggerXML.find(ns + "model")

            if modelXML is not None and modelXML.text == model:
                dataloggerXML.attrib["name"]
                dataloggerXML.attrib["response"]
                publicID = dataloggerXML.attrib["publicID"]
                is_found = True
                break

    if is_found:
        # Copy element
        dataloggerXML = copy.deepcopy(dataloggerXML)
        # Strip namespace from elements
        dataloggerXML.tag = dataloggerXML.tag[len(ns) :]
        for child in dataloggerXML:
            child.tag = child.tag[len(ns) :]
            if child.tag == "decimation":
                for grandchild in child:
                    grandchild.tag = grandchild.tag[len(ns) :]

        return {"publicID": publicID, "xml": dataloggerXML}
    else:
        logging.critical(
            f"Datalogger with sensor_sensitivity {sensor_sensitivity} not found in master_inventory.xml"
        )
        sys.exit(1)


def lookupMasterSensorXML(device):
    # print('model',model)
    # <sensor publicID="NRL/Guralp/CMG3ESP.60.2000" name="Guralp/CMG3ESP.60.2000" response="NRL/Guralp/CMG3ESP.60.2000/1">
    #    <description>GURESPA in SIL-system</description>
    #    <model>CMG-3ESP</model>
    #    <manufacturer>Guralp</manufacturer>
    #    <unit>M/S</unit>
    # </sensor>

    model = device["model"]
    # Model fixes
    if model.startswith("CMG-3ESPC"):
        model = "CMG-3ESP"

    # <model>LE-3D/5s</model>
    # <model>LE-3D5s</model>

    regex = re.compile("({http.*})Inventory")
    match = regex.search(master_inventoryXML.tag)
    if match:
        ns = match.group(1)
    else:
        raise ValueError("Invalid SC3ML file")
        sys.exit(1)

    sensorsXML = master_inventoryXML.findall(ns + "sensor")
    is_found = False
    for sensorXML in sensorsXML:
        modelXML = sensorXML.find(ns + "model")

        if modelXML is not None and modelXML.text == model:
            sensorXML.attrib["name"]
            sensorXML.attrib["response"]
            publicID = sensorXML.attrib["publicID"]
            is_found = True
            break

    if is_found:
        # Copy element
        sensorXML = copy.deepcopy(sensorXML)
        # Strip namespace from elements
        sensorXML.tag = sensorXML.tag[len(ns) :]
        for child in sensorXML:
            child.tag = child.tag[len(ns) :]
            if child.tag == "decimation":
                for grandchild in child:
                    grandchild.tag = grandchild.tag[len(ns) :]

        return {"publicID": publicID, "xml": sensorXML}
    else:
        logging.critical(f"Sensor model {model} not found in master_inventory.xml")
        sys.exit(1)


def display(data, format="pretty", tablefmt="simple"):
    if format == "pretty":
        for item in data:
            output = []
            for attribute in item["attributes"]:
                # Find current attributes
                if attribute["date_to"] is None:
                    output.append(attribute)

            # Apply sort_order
            output.sort(key=lambda d: d["sort_order"])

            # Station and location specific parameters
            if "station" in item:
                output.append(
                    {"name_is": "--Stöð--", "name_en": "--Station--", "value": ""}
                )
                for attribute in item["station"]:
                    # Find current attributes
                    if attribute["date_to"] is None:
                        output.append(attribute)

            if "location" in item:
                output.append(
                    {
                        "name_is": "--Staðsetning--",
                        "name_en": "--Location--",
                        "value": "",
                    }
                )
                for attribute in item["location"]:
                    # Find current attributes
                    if attribute["date_to"] is None:
                        output.append(attribute)

            print(
                tabulate(
                    [{"name_is": d["name_is"], "value": d["value"]} for d in output],
                    tablefmt=tablefmt,
                )
            )

            if "devices" in item:
                print("Tæki")
                output = []
                for device in item["devices"]:
                    model = ""
                    for attribute in device["attributes"]:
                        if attribute["code"] == "model":
                            model = attribute["value"]
                    output.append(
                        {
                            "code_entity_subtype": device["code_entity_subtype"],
                            "entity_subtype_name_is": device["entity_subtype_name_is"],
                            "model": model,
                        }
                    )
                    # output.append({'code_entity_subtype':device['code_entity_subtype'], 'entity_subtype_name_is':device['entity_subtype_name_is']})
                    # output.append({'serial_number': serial_number['value'], 'model': model['value'], 'name': name['value'], 'marker': marker['value']})

                    # print(device['code_entity_subtype'])
                    # print(device['entity_subtype_name_is'])
                    # for attribute in device['attributes']:
                    #    #if
                    #    print(attribute)
                print(
                    tabulate(
                        [
                            {
                                "entity_subtype_name_is": d["entity_subtype_name_is"],
                                "code_entity_subtype": d["code_entity_subtype"],
                                "model": d["model"],
                            }
                            for d in output
                        ],
                        tablefmt=tablefmt,
                    )
                )

    elif format == "table":
        for item in data:
            output = []
            # NOTE: due to the code_entity_type missing from the API we must use entity_type_name_en:
            # https://git.vedur.is/AOT/tos/-/issues/291
            headers = {}
            row = {}
            if item["entity_type_name_en"] in ["station", "platform"]:

                name = next(
                    (
                        item
                        for item in item["attributes"]
                        if (item["code"] == "name" and item["date_to"] is None)
                    ),
                    {"value": None},
                )
                if name["value"]:
                    headers["name"] = name["name_is"]
                    row["name"] = name["value"]

                marker = next(
                    (
                        item
                        for item in item["attributes"]
                        if (item["code"] == "marker" and item["date_to"] is None)
                    ),
                    {"value": None},
                )
                if marker["value"]:
                    headers["marker"] = marker["name_is"]
                    row["marker"] = marker["value"]

                wmo = next(
                    (
                        item
                        for item in item["attributes"]
                        if (item["code"] == "wmo" and item["date_to"] is None)
                    ),
                    {"value": None},
                )
                if wmo["value"]:
                    headers["wmo"] = wmo["name_is"]
                    row["wmo"] = wmo["value"]

                imo = next(
                    (
                        item
                        for item in item["attributes"]
                        if (item["code"] == "imo" and item["date_to"] is None)
                    ),
                    {"value": None},
                )
                if imo["value"]:
                    headers["imo"] = imo["name_is"]
                    row["imo"] = imo["value"]

                subtype = next(
                    (
                        item
                        for item in item["attributes"]
                        if (item["code"] == "subtype" and item["date_to"] is None)
                    ),
                    {"value": None},
                )
                if subtype["value"]:
                    headers["subtype"] = subtype["name_is"]
                    row["subtype"] = subtype["value"]

                if item["code_entity_subtype"] == "hydrological":
                    lat = next(
                        (
                            item
                            for item in item["attributes"]
                            if (item["code"] == "lat_isn93" and item["date_to"] is None)
                        ),
                        {"value": None},
                    )
                    if lat["value"]:
                        headers["lat"] = lat["name_is"]
                        row["lat"] = lat["value"]
                    lon = next(
                        (
                            item
                            for item in item["attributes"]
                            if (item["code"] == "lon_isn93" and item["date_to"] is None)
                        ),
                        {"value": None},
                    )
                    if lon["value"]:
                        headers["lon"] = lon["name_is"]
                        row["lon"] = lon["value"]
                else:
                    lat = next(
                        (
                            item
                            for item in item["attributes"]
                            if (item["code"] == "lat" and item["date_to"] is None)
                        ),
                        {"value": None},
                    )
                    if lat["value"]:
                        headers["lat"] = lat["name_is"]
                        row["lat"] = lat["value"]
                    lon = next(
                        (
                            item
                            for item in item["attributes"]
                            if (item["code"] == "lon" and item["date_to"] is None)
                        ),
                        {"value": None},
                    )
                    if lon["value"]:
                        headers["lon"] = lon["name_is"]
                        row["lon"] = lon["value"]

                altitude = next(
                    (
                        item
                        for item in item["attributes"]
                        if (item["code"] == "altitude" and item["date_to"] is None)
                    ),
                    {"value": None},
                )
                if altitude["value"]:
                    headers["altitude"] = altitude["name_is"]
                    row["altitude"] = altitude["value"]

                if "location" in item:
                    location = next(
                        (
                            item
                            for item in item["location"]
                            if (item["code"] == "name" and item["date_to"] is None)
                        ),
                        {"value": None},
                    )
                    if location["value"]:
                        headers["location"] = location["name_is"]
                        row["location"] = location["value"]

                output.append(row)

            elif item["entity_type_name_en"] == "device":
                serial_number = next(
                    (
                        item
                        for item in item["attributes"]
                        if (item["code"] == "serial_number" and item["date_to"] is None)
                    ),
                    {"value": None},
                )
                if serial_number["value"]:
                    headers["serial_number"] = serial_number["name_is"]

                model = next(
                    (
                        item
                        for item in item["attributes"]
                        if (item["code"] == "model" and item["date_to"] is None)
                    ),
                    {"value": None},
                )
                if model["value"]:
                    headers["model"] = model["name_is"]

                name = next(
                    (
                        item
                        for item in item["station"]
                        if (item["code"] == "name" and item["date_to"] is None)
                    ),
                    {"value": None},
                )
                if name["value"]:
                    headers["name"] = name["name_is"]

                marker = next(
                    (
                        item
                        for item in item["station"]
                        if (item["code"] == "marker" and item["date_to"] is None)
                    ),
                    {"value": None},
                )
                if marker["value"]:
                    headers["marker"] = marker["name_is"]

                if "location" in item:
                    location = next(
                        (
                            item
                            for item in item["location"]
                            if (item["code"] == "name" and item["date_to"] is None)
                        ),
                        {"value": None},
                    )
                    if location["value"]:
                        headers["location"] = location["name_is"]

                    output.append(
                        {
                            "serial_number": serial_number["value"],
                            "model": model["value"],
                            "name": name["value"],
                            "marker": marker["value"],
                            "location": location["value"],
                        }
                    )

                else:
                    output.append(
                        {
                            "serial_number": serial_number["value"],
                            "model": model["value"],
                            "name": name["value"],
                            "marker": marker["value"],
                        }
                    )

            print(tabulate(output, headers, tablefmt=tablefmt))

    elif format == "json":
        print(json.dumps(data, ensure_ascii=False))
    # elif format=='table':  #Full lines for CSV
    #    print(data)


def indent(elem, level=0, more_sibs=False):
    i = "\n"
    if level:
        i += (level - 1) * "  "
    num_kids = len(elem)
    if num_kids:
        if not elem.text or not elem.text.strip():
            elem.text = i + "  "
            if level:
                elem.text += "  "
        count = 0
        for kid in elem:
            indent(kid, level + 1, count < num_kids - 1)
            count += 1
        if not elem.tail or not elem.tail.strip():
            elem.tail = i
            if more_sibs:
                elem.tail += "  "
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = i
            if more_sibs:
                elem.tail += "  "


def parseSeiscompInventoryXML(inventory_file):
    # Parse inventory file
    seiscomp = ET.parse(inventory_file).getroot()
    schema_version = seiscomp.attrib["version"]
    ns = "{http://geofon.gfz-potsdam.de/ns/seiscomp3-schema/" + schema_version + "}"
    if seiscomp.tag != ns + "seiscomp":
        logging.critical("Invalid inventory file, root element seiscomp not found")
        sys.exit(1)
    inventoryXML = seiscomp.find(ns + "Inventory")
    # sensors = inventory.findall(ns+'sensor')
    # dataloggers = inventory.findall(ns+'datalogger')
    # responsePAZ = inventory.findall(ns+'responsePAZ')
    # responseFIR = inventory.findall(ns+'responseFIR')

    return inventoryXML


def generatePublicID(schema_version, classname, resource, time, id):
    map_classname = {
        "0.11": {
            "network": "NET",
            "station": "STA",
            "sensorLocation": "LOC",
            "stream": "Stream",
            "datalogger": "Datalogger",
        },
        "0.0": {
            "network": "Network",
            "station": "Station",
            "sensorLocation": "SensorLocation",
            "stream": "Stream",
            "datalogger": "Datalogger",
        },
    }
    tokens = schema_version.split(".")
    if int(tokens[1]) >= 11:
        # Pattern: @classname@/@time/%Y%m%d%H%M%S.%f@.@id@
        if resource:
            return (
                map_classname["0.11"][classname]
                + "/VI/"
                + resource
                + "/"
                + time
                + "000000.000000."
                + str(id)
            )
        else:
            return (
                map_classname["0.11"][classname]
                + "/VI/"
                + time
                + "000000.000000."
                + str(id)
            )
    else:
        return map_classname["0.0"][classname] + "#" + time + "000000.000000." + str(id)


def generateSC3ML(station_list=None, schema_version=None):
    if schema_version is None:
        schema_version = "0.11"
    # from xml.etree.ElementTree import Element, SubElement, Comment
    # from ElementTree_pretty import prettify
    # from io import BytesIO
    # xml.etree.ElementTree.SubElement(parent, tag, attrib={}, **extra)¶

    dataloggers = {}
    sensors = {}

    # <seiscomp version="0.10" xmlns="http://geofon.gfz-potsdam.de/ns/seiscomp3-schema/0.10">
    # seiscomp = ET.SubElement(root, 'seiscomp', {'version': schema_version, 'xmlns': 'http://geofon.gfz-potsdam.de/ns/seiscomp3-schema/'+schema_version})
    seiscompXML = ET.Element(
        "seiscomp",
        {
            "version": schema_version,
            "xmlns": "http://geofon.gfz-potsdam.de/ns/seiscomp3-schema/"
            + schema_version,
        },
    )

    # <Inventory>
    inventoryXML = ET.SubElement(seiscompXML, "Inventory")

    # <network code="VI" publicID="Network#20131010133839.04134.12281">
    #    <start>1990-10-10T00:00:00.0000Z</start>
    #    <description>SIL  Icelandic national seismic network</description>
    #    <institutions>IMO</institutions>
    #    <region>Iceland</region>
    #    <type>BB, SP</type>
    #    <restricted>false</restricted>
    #    <shared>false</shared>
    publicID = generatePublicID(schema_version, "network", "", "19901010", "0")
    # network = ET.SubElement(inventory, 'network', {'code': 'VI', 'publicID': publicID})
    networkXML = ET.Element("network", {"code": "VI", "publicID": publicID})
    ET.SubElement(networkXML, "start").text = "1990-10-10T00:00:00.0000Z"
    ET.SubElement(networkXML, "description").text = (
        "SIL  Icelandic national seismic network"
    )
    ET.SubElement(networkXML, "institutions").text = "IMO"
    ET.SubElement(networkXML, "region").text = "Iceland"
    ET.SubElement(networkXML, "type").text = "BB, SP"
    ET.SubElement(networkXML, "restricted").text = "false"
    ET.SubElement(networkXML, "shared").text = "false"

    # Get stations
    if len(station_list) > 0:
        for station_identifier in station_list:
            station = searchStation(station_identifier, url_rest_tos, "geophysical")
            if len(station) == 0:
                logging.warning(
                    f"Station with station_identifier {station_identifier} not found"
                )
            elif len(station) > 1:
                logging.critical(
                    f"Multiple stations with station_identifier {station_identifier} found"
                )
                sys.exit(1)
            else:
                subtype = next(
                    (
                        item
                        for item in station[0]["attributes"]
                        if (item["code"] == "subtype" and item["date_to"] is None)
                    ),
                    None,
                )["value"]
                if subtype == "SIL stöð":
                    # <station publicID="STA/VI/ada/20201119135844.865164.24" code="ada">
                    # <station publicID="Station#20131011220040.346443.30884" code="ada">
                    #    <start>1998-09-19T00:00:00.0000Z</start>
                    #    <latitude>65.01879</latitude>
                    #    <longitude>-15.57452</longitude>
                    #    <elevation>443</elevation>
                    #    <place>Aðalból</place>
                    #    <restricted>false</restricted>
                    #    <shared>false</shared>
                    id_entity = station[0]["id_entity"]
                    station_identifier = next(
                        (
                            item
                            for item in station[0]["attributes"]
                            if (item["code"] == "marker" and item["date_to"] is None)
                        ),
                        None,
                    )["value"]
                    name = next(
                        (
                            item
                            for item in station[0]["attributes"]
                            if (item["code"] == "name" and item["date_to"] is None)
                        ),
                        None,
                    )["value"]
                    date_start = next(
                        (
                            item
                            for item in station[0]["attributes"]
                            if (
                                item["code"] == "date_start" and item["date_to"] is None
                            )
                        ),
                        None,
                    )["value"]
                    lat = next(
                        (
                            item
                            for item in station[0]["attributes"]
                            if (item["code"] == "lat" and item["date_to"] is None)
                        ),
                        None,
                    )["value"]
                    lon = next(
                        (
                            item
                            for item in station[0]["attributes"]
                            if (item["code"] == "lon" and item["date_to"] is None)
                        ),
                        None,
                    )["value"]
                    altitude = next(
                        (
                            item
                            for item in station[0]["attributes"]
                            if (item["code"] == "altitude" and item["date_to"] is None)
                        ),
                        None,
                    )["value"]

                    publicID = generatePublicID(
                        schema_version,
                        "station",
                        station_identifier,
                        date_start.replace("-", ""),
                        "0",
                    )
                    stationXML = ET.SubElement(
                        networkXML,
                        "station",
                        {"code": station_identifier, "publicID": publicID},
                    )
                    ET.SubElement(stationXML, "start").text = (
                        date_start + "T00:00:00.0000Z"
                    )
                    ET.SubElement(stationXML, "latitude").text = lat
                    ET.SubElement(stationXML, "longitude").text = lon
                    ET.SubElement(stationXML, "elevation").text = altitude
                    ET.SubElement(stationXML, "place").text = name
                    ET.SubElement(stationXML, "restricted").text = "false"
                    ET.SubElement(stationXML, "shared").text = "false"

                    # Device sessions
                    sessions = getDeviceSessions(id_entity)

                    if sessions:
                        for session in sessions:
                            if "seismometer" in session:
                                # sensor = lookupMasterDataloggerXML(session['digitizer']['sensor_sensitivity'])  #publicID, xml
                                print("TODO seismometer")
                                sys.exit()
                                # datalogger = lookupMasterDataloggerXML(session['seismometer']['device'])  #publicID, xml
                                # sensor = lookupMasterSensorXML(session['seismometer']['device'])  #publicID, xml
                            else:
                                if "digitizer" in session:
                                    # print(session['digitizer']['device'])
                                    datalogger = lookupMasterDataloggerXML(
                                        session["digitizer"]["device"]
                                    )  # publicID, xml
                                if "seismic_sensor" in session:
                                    # print(session['seismic_sensor']['device'])
                                    sensor = lookupMasterSensorXML(
                                        session["seismic_sensor"]["device"]
                                    )  # publicID, xml

                            # Insert into XML list
                            if datalogger["publicID"] not in dataloggers:
                                dataloggers[datalogger["publicID"]] = datalogger
                            if sensor["publicID"] not in sensors:
                                sensors[sensor["publicID"]] = sensor

                        # <sensorLocation publicID="LOC/VI/ada//20201119135914.058238.323" code="">
                        # <sensorLocation publicID="SensorLocation#20131011220040.346534.30885" code="">
                        #    <start>1998-09-19T00:00:00.0000Z</start>
                        #    <latitude>65.01879</latitude>
                        #    <longitude>-15.57452</longitude>
                        #    <elevation>443</elevation>
                        publicID = generatePublicID(
                            schema_version,
                            "sensorLocation",
                            station_identifier,
                            date_start.replace("-", ""),
                            "0",
                        )
                        sensorLocationXML = ET.SubElement(
                            stationXML,
                            "sensorLocation",
                            {"code": "", "publicID": publicID},
                        )
                        # ET.SubElement(sensorLocationXML, 'start').text = date_start+'T00:00:00.0000Z'
                        ET.SubElement(sensorLocationXML, "start").text = session[
                            "time_from"
                        ]
                        ET.SubElement(sensorLocationXML, "latitude").text = lat
                        ET.SubElement(sensorLocationXML, "longitude").text = lon
                        ET.SubElement(sensorLocationXML, "elevation").text = altitude

                        # <stream code="HHE" datalogger="Datalogger#20150925141352.76545.33138" sensor="NRL/Guralp/CMG3ESP.60.2000">
                        #    <start>2015-01-01T00:00:00.0000Z</start>
                        #    <dataloggerSerialNumber>E086</dataloggerSerialNumber>
                        #    <dataloggerChannel>0</dataloggerChannel>
                        #    <sensorSerialNumber>T3Y33</sensorSerialNumber>
                        #    <sensorChannel>0</sensorChannel>
                        #    <sampleRateNumerator>100</sampleRateNumerator>
                        #    <sampleRateDenominator>1</sampleRateDenominator>
                        #    <depth>0</depth>
                        #    <azimuth>90</azimuth>
                        #    <dip>0</dip>
                        #    <gain>625000000</gain>
                        #    <gainFrequency>1</gainFrequency>
                        #    <gainUnit>M/S</gainUnit>
                        #    <format>Steim2</format>
                        #    <restricted>false</restricted>
                        #    <shared>true</shared>
                        # </stream>
                        streamHHEXML = ET.SubElement(
                            sensorLocationXML,
                            "stream",
                            {
                                "code": "HHE",
                                "datalogger": datalogger["publicID"],
                                "sensor": "asdf",
                            },
                        )
                        # ET.SubElement(streamHHEXML, 'start').text = date_start+'T00:00:00.0000Z'
                        ET.SubElement(streamHHEXML, "start").text = session["time_from"]
                        ET.SubElement(streamHHEXML, "dataloggerSerialNumber").text = (
                            session["digitizer"]["device"]["serial_number"]
                        )
                        ET.SubElement(streamHHEXML, "dataloggerChannel").text = (
                            "0"  # Always 0
                        )
                        ET.SubElement(streamHHEXML, "sensorSerialNumber").text = (
                            session["seismic_sensor"]["device"]["serial_number"]
                        )
                        ET.SubElement(streamHHEXML, "sensorChannel").text = (
                            "0"  # Always 0
                        )
                        ET.SubElement(streamHHEXML, "sampleRateNumerator").text = "xxx"
                        ET.SubElement(streamHHEXML, "sampleRateDenominator").text = (
                            "xxx"
                        )
                        ET.SubElement(streamHHEXML, "depth").text = "0"
                        ET.SubElement(streamHHEXML, "azimuth").text = (
                            "90"  # HHE: 90, HHN: 0, HHZ: 0
                        )
                        ET.SubElement(streamHHEXML, "dip").text = (
                            "0"  # HHE:  0, HHN: 0, HHZ: -90
                        )
                        ET.SubElement(streamHHEXML, "gain").text = "xxx"
                        ET.SubElement(streamHHEXML, "gainFrequency").text = "xxx"
                        ET.SubElement(streamHHEXML, "gainUnit").text = "xxx"
                        ET.SubElement(streamHHEXML, "format").text = "xxx"
                        ET.SubElement(streamHHEXML, "restricted").text = "xxx"
                        ET.SubElement(streamHHEXML, "shared").text = "xxx"

                        # <stream code="HHN" datalogger="Datalogger#20150925141352.76545.33138" sensor="NRL/Guralp/CMG3ESP.60.2000">
                        #    <start>2015-01-01T00:00:00.0000Z</start>
                        #    <dataloggerSerialNumber>E086</dataloggerSerialNumber>
                        #    <dataloggerChannel>0</dataloggerChannel>
                        #    <sensorSerialNumber>T3Y33</sensorSerialNumber>
                        #    <sensorChannel>0</sensorChannel>
                        #    <sampleRateNumerator>100</sampleRateNumerator>
                        #    <sampleRateDenominator>1</sampleRateDenominator>
                        #    <depth>0</depth>
                        #    <azimuth>0</azimuth>
                        #    <dip>0</dip>
                        #    <gain>625000000</gain>
                        #    <gainFrequency>1</gainFrequency>
                        #    <gainUnit>M/S</gainUnit>
                        #    <format>Steim2</format>
                        #    <restricted>false</restricted>
                        #    <shared>true</shared>
                        # </stream>
                        streamHHNXML = ET.SubElement(
                            sensorLocationXML,
                            "stream",
                            {
                                "code": "HHN",
                                "datalogger": datalogger["publicID"],
                                "sensor": "asdf",
                            },
                        )
                        # ET.SubElement(streamHHNXML, 'start').text = date_start+'T00:00:00.0000Z'
                        # ET.SubElement(streamHHNXML, 'dataloggerSerialNumber').text = datalogger['serial_number']
                        ET.SubElement(streamHHNXML, "start").text = session["time_from"]
                        ET.SubElement(streamHHNXML, "dataloggerSerialNumber").text = (
                            session["digitizer"]["device"]["serial_number"]
                        )
                        ET.SubElement(streamHHNXML, "dataloggerChannel").text = (
                            "0"  # Always 0
                        )
                        ET.SubElement(streamHHNXML, "sensorSerialNumber").text = (
                            session["seismic_sensor"]["device"]["serial_number"]
                        )
                        ET.SubElement(streamHHNXML, "sensorChannel").text = (
                            "0"  # Always 0
                        )
                        ET.SubElement(streamHHNXML, "sampleRateNumerator").text = "xxx"
                        ET.SubElement(streamHHNXML, "sampleRateDenominator").text = (
                            "xxx"
                        )
                        ET.SubElement(streamHHNXML, "depth").text = "0"
                        ET.SubElement(streamHHNXML, "azimuth").text = (
                            "0"  # HHE: 90, HHN: 0, HHZ: 0
                        )
                        ET.SubElement(streamHHNXML, "dip").text = (
                            "0"  # HHE:  0, HHN: 0, HHZ: -90
                        )
                        ET.SubElement(streamHHNXML, "gain").text = "xxx"
                        ET.SubElement(streamHHNXML, "gainFrequency").text = "xxx"
                        ET.SubElement(streamHHNXML, "gainUnit").text = "xxx"
                        ET.SubElement(streamHHNXML, "format").text = "xxx"
                        ET.SubElement(streamHHNXML, "restricted").text = "xxx"
                        ET.SubElement(streamHHNXML, "shared").text = "xxx"

                        # <stream code="HHZ" datalogger="Datalogger#20150925141352.76545.33138" sensor="NRL/Guralp/CMG3ESP.60.2000">
                        #    <start>2015-01-01T00:00:00.0000Z</start>
                        #    <dataloggerSerialNumber>E086</dataloggerSerialNumber>
                        #    <dataloggerChannel>0</dataloggerChannel>
                        #    <sensorSerialNumber>T3Y33</sensorSerialNumber>
                        #    <sensorChannel>0</sensorChannel>
                        #    <sampleRateNumerator>100</sampleRateNumerator>
                        #    <sampleRateDenominator>1</sampleRateDenominator>
                        #    <depth>0</depth>
                        #    <azimuth>0</azimuth>
                        #    <dip>-90</dip>
                        #    <gain>625000000</gain>
                        #    <gainFrequency>1</gainFrequency>
                        #    <gainUnit>M/S</gainUnit>
                        #    <format>Steim2</format>
                        #    <restricted>false</restricted>
                        #    <shared>true</shared>
                        # </stream>
                        streamHHZXML = ET.SubElement(
                            sensorLocationXML,
                            "stream",
                            {
                                "code": "HHZ",
                                "datalogger": datalogger["publicID"],
                                "sensor": "asdf",
                            },
                        )
                        # ET.SubElement(streamHHZXML, 'start').text = date_start+'T00:00:00.0000Z'
                        # ET.SubElement(streamHHZXML, 'dataloggerSerialNumber').text = datalogger['serial_number']
                        ET.SubElement(streamHHZXML, "start").text = session["time_from"]
                        ET.SubElement(streamHHZXML, "dataloggerSerialNumber").text = (
                            session["digitizer"]["device"]["serial_number"]
                        )
                        ET.SubElement(streamHHZXML, "dataloggerChannel").text = (
                            "0"  # Always 0
                        )
                        ET.SubElement(streamHHZXML, "sensorSerialNumber").text = (
                            session["seismic_sensor"]["device"]["serial_number"]
                        )
                        ET.SubElement(streamHHZXML, "sensorChannel").text = (
                            "0"  # Always 0
                        )
                        ET.SubElement(streamHHZXML, "sampleRateNumerator").text = "xxx"
                        ET.SubElement(streamHHZXML, "sampleRateDenominator").text = (
                            "xxx"
                        )
                        ET.SubElement(streamHHZXML, "depth").text = "0"
                        ET.SubElement(streamHHZXML, "azimuth").text = (
                            "0"  # HHE: 90, HHN: 0, HHZ: 0
                        )
                        ET.SubElement(streamHHZXML, "dip").text = (
                            "-90"  # HHE:  0, HHN: 0, HHZ: -90
                        )
                        ET.SubElement(streamHHZXML, "gain").text = "xxx"
                        ET.SubElement(streamHHZXML, "gainFrequency").text = "xxx"
                        ET.SubElement(streamHHZXML, "gainUnit").text = "xxx"
                        ET.SubElement(streamHHZXML, "format").text = "xxx"
                        ET.SubElement(streamHHZXML, "restricted").text = "xxx"
                        ET.SubElement(streamHHZXML, "shared").text = "xxx"

    else:
        print("TODO: all sil stations")
        sys.exit()

    # inventory.append(sensors)
    # inventory.append(dataloggers)
    # inventory.append(responsePAZs)
    # inventory.append(responseFIRs)

    # Compile the XML Inventory
    # Dataloggers
    for publicID, datalogger in dataloggers.items():
        # print(datalogger)
        inventoryXML.append(datalogger["xml"])

    for publicID, sensor in sensors.items():
        # print(datalogger)
        inventoryXML.append(sensor["xml"])

    inventoryXML.append(networkXML)

    # Prettify
    tree = ET.ElementTree(seiscompXML)
    for elem in tree.iter():
        indent(elem)
    # ET.dump(tree)

    # f = BytesIO()
    # tree.write(f, encoding='utf-8', xml_declaration=True)
    # print(f.getvalue())  # your XML file, encoded as UTF-8

    # xml = ET.tostring(seiscomp, encoding='UTF-8', method='xml', xml_declaration=True).decode()
    xml = ET.tostring(seiscompXML, encoding="UTF-8", method="xml").decode()

    # ET.dump(top)
    # for elem in tree.iter():

    # IRIS Nomial Response Library (http://ds.iris.edu/NRL/)
    # nrl = NRL()

    # ada
    # <stream code="HHE" datalogger="Datalogger#20150925141352.76545.33138" sensor="NRL/Guralp/CMG3ESP.60.2000">
    #    <start>2015-01-01T00:00:00.0000Z</start>
    #    <dataloggerSerialNumber>E086</dataloggerSerialNumber>
    #    <dataloggerChannel>0</dataloggerChannel>
    #    <sensorSerialNumber>T3Y33</sensorSerialNumber>
    #    <sensorChannel>0</sensorChannel>
    #    <sampleRateNumerator>100</sampleRateNumerator>
    #    <sampleRateDenominator>1</sampleRateDenominator>
    #    <depth>0</depth>
    #    <azimuth>90</azimuth>
    #    <dip>0</dip>
    #    <gain>625000000</gain>
    #    <gainFrequency>1</gainFrequency>
    #    <gainUnit>M/S</gainUnit>
    #    <format>Steim2</format>
    #    <restricted>false</restricted>
    #    <shared>true</shared>
    # </stream>

    # <dataloggerChannel> alltaf 0
    # <sensorChannel> alltaf 0
    # <azimuth>90 for HHE, 0 for HHN, HHZ
    # <dip>-90 for HHZ, 0 for HHE, HHN

    #    print(nrl.sensors['Guralp']['CMG-3ESP']['60 s - 50 Hz']['2000'])
    #    #('CMG-3ESP, 60 s, 2000 V/m/s', 'http://ds.iris.edu/NRL/sensors/guralp/RESP.XX.NS029..BHZ.CMG3ESP.60.2000')
    #
    #    #Select the the model of your DM-24 (3 items): 'Mk1', 'Mk2', 'Mk3'
    #    #Select whether your DM24 Mk3 preamp has fixed (1) or variable (1,2,4,8,16,32,64) gain (2 items): 'Fixed', 'Variable'
    #    #Select a tap table lookup range (10 items): '1-10', '11-20', '21-30', '31-40', '41-50', '51-60', '61-70','71-80', '81-90', '91-95'
    #    #Select this channel's tap table lookup number (10 items): '1', '10', '2', '3', '4', '5', '6', '7', '8', '9'
    #    #Select this channel's sample rate in samples per second (6 items): '1000', '125', '25', '250', '5', '500'
    #    print(nrl.dataloggers['Guralp']['CMG-DM24']['Mk3']['Fixed']['1-10']['1']['1000'])    #DM24-S3, preamp=Fixed, tap-table lookup rante=1-10, tap-table=1, sample-rate=
    #    #('DM-24 Mk3 Fixed Gain, gain 1, 1000 sps, tap id 1, (1000 500 250 125 25 and 5 Hz)', 'http://ds.iris.edu/NRL/dataloggers/guralp/CMG_DM24/mk3/fixed/RESP.XX.G0143..HHZ.CMG_DM24_MK3_FIX.1..1000')
    #
    #    #response = nrl.get_response(sensor_keys=['Streckeisen', 'STS-1', '360 seconds'],datalogger_keys=['REF TEK', 'RT 130 & 130-SMA', '1', '200'])
    #    response = nrl.get_response(sensor_keys=['Guralp', 'CMG-3ESP', '60 s - 50 Hz', '2000'],
    #                                datalogger_keys=['Guralp','CMG-DM24','Mk3','Fixed','1-10','1','1000'] )
    #    print(response)
    #
    #    #asb
    #    #<stream code="HHN" datalogger="Datalogger#20140806133046.974009.123501" sensor="NRL/Streckeisen/STS3.120.1500">
    #    #print(nrl.sensors['Streckeisen']['STS-3'])

    #    #Get stations
    #    if station_list==None:
    #        logging.info("No station_identifier:s specified, Generate Gempa XML for all SIL stations")
    #    else:
    #        logging.info("Generate Gempa XML for stations: "+str(station_list))
    #
    #    #Query TOS api for SIL stations
    #    body = {
    #            'code': 'subtype',
    #            'value': 'SIL stöð'
    #        }
    #    response = requests.post(url_rest_tos+'/entity/search/station/geophysical/', data=json.dumps(body))
    #    response.raise_for_status()
    #
    #    stations=[]
    #    for station in response.json():
    #        data={}
    #        data['station_identifier'] = next((item for item in station['attributes'] if (item['code'] == 'marker' and item['date_to'] is None)), None)['value']
    #
    #        value = next((item for item in station['attributes'] if (item['code'] == 'lat' and item['date_to'] is None)), {'value': None})['value']
    #        if value:
    #            data['lat'] = float(value)
    #        value = next((item for item in station['attributes'] if (item['code'] == 'lon' and item['date_to'] is None)), {'value': None})['value']
    #        if value:
    #            data['lon'] = float(value)
    #
    #        if (station_list and data['station_identifier'] in station_list) or station_list is None:
    #            #if data['station_identifier'] in station_list:
    #            stations.append(data)
    #
    #    #print(stations)

    # Valid formats: CSS, KML, SACPZ, SHAPEFILE, STATIONTXT, STATIONXML
    # inv.write("station.response.xml", format="stationxml", validate=True)

    # print(xml)
    return xml
    # return tree


def generateFDSNXML(station_list=None):
    # IRIS Nomial Response Library (http://ds.iris.edu/NRL/)
    nrl = NRL()

    # ada
    # <stream code="HHE" datalogger="Datalogger#20150925141352.76545.33138" sensor="NRL/Guralp/CMG3ESP.60.2000">
    #    <start>2015-01-01T00:00:00.0000Z</start>
    #    <dataloggerSerialNumber>E086</dataloggerSerialNumber>
    #    <dataloggerChannel>0</dataloggerChannel>
    #    <sensorSerialNumber>T3Y33</sensorSerialNumber>
    #    <sensorChannel>0</sensorChannel>
    #    <sampleRateNumerator>100</sampleRateNumerator>
    #    <sampleRateDenominator>1</sampleRateDenominator>
    #    <depth>0</depth>
    #    <azimuth>90</azimuth>
    #    <dip>0</dip>
    #    <gain>625000000</gain>
    #    <gainFrequency>1</gainFrequency>
    #    <gainUnit>M/S</gainUnit>
    #    <format>Steim2</format>
    #    <restricted>false</restricted>
    #    <shared>true</shared>
    # </stream>

    # <dataloggerChannel> alltaf 0
    # <sensorChannel> alltaf 0
    # <azimuth>90 for HHE, 0 for HHN, HHZ
    # <dip>-90 for HHZ, 0 for HHE, HHN

    #    print(nrl.sensors['Guralp']['CMG-3ESP']['60 s - 50 Hz']['2000'])
    #    #('CMG-3ESP, 60 s, 2000 V/m/s', 'http://ds.iris.edu/NRL/sensors/guralp/RESP.XX.NS029..BHZ.CMG3ESP.60.2000')
    #
    #    #Select the the model of your DM-24 (3 items): 'Mk1', 'Mk2', 'Mk3'
    #    #Select whether your DM24 Mk3 preamp has fixed (1) or variable (1,2,4,8,16,32,64) gain (2 items): 'Fixed', 'Variable'
    #    #Select a tap table lookup range (10 items): '1-10', '11-20', '21-30', '31-40', '41-50', '51-60', '61-70','71-80', '81-90', '91-95'
    #    #Select this channel's tap table lookup number (10 items): '1', '10', '2', '3', '4', '5', '6', '7', '8', '9'
    #    #Select this channel's sample rate in samples per second (6 items): '1000', '125', '25', '250', '5', '500'
    #    print(nrl.dataloggers['Guralp']['CMG-DM24']['Mk3']['Fixed']['1-10']['1']['1000'])    #DM24-S3, preamp=Fixed, tap-table lookup rante=1-10, tap-table=1, sample-rate=
    #    #('DM-24 Mk3 Fixed Gain, gain 1, 1000 sps, tap id 1, (1000 500 250 125 25 and 5 Hz)', 'http://ds.iris.edu/NRL/dataloggers/guralp/CMG_DM24/mk3/fixed/RESP.XX.G0143..HHZ.CMG_DM24_MK3_FIX.1..1000')
    #
    #    #response = nrl.get_response(sensor_keys=['Streckeisen', 'STS-1', '360 seconds'],datalogger_keys=['REF TEK', 'RT 130 & 130-SMA', '1', '200'])
    #    response = nrl.get_response(sensor_keys=['Guralp', 'CMG-3ESP', '60 s - 50 Hz', '2000'],
    #                                datalogger_keys=['Guralp','CMG-DM24','Mk3','Fixed','1-10','1','1000'] )
    #    print(response)
    #
    #    #asb
    #    #<stream code="HHN" datalogger="Datalogger#20140806133046.974009.123501" sensor="NRL/Streckeisen/STS3.120.1500">
    #    #print(nrl.sensors['Streckeisen']['STS-3'])

    #    #Get stations
    #    if station_list==None:
    #        logging.info("No station_identifier:s specified, Generate Gempa XML for all SIL stations")
    #    else:
    #        logging.info("Generate Gempa XML for stations: "+str(station_list))
    #
    #    #Query TOS api for SIL stations
    #    body = {
    #            'code': 'subtype',
    #            'value': 'SIL stöð'
    #        }
    #    response = requests.post(url_rest_tos+'/entity/search/station/geophysical/', data=json.dumps(body))
    #    response.raise_for_status()
    #
    #    stations=[]
    #    for station in response.json():
    #        data={}
    #        data['station_identifier'] = next((item for item in station['attributes'] if (item['code'] == 'marker' and item['date_to'] is None)), None)['value']
    #
    #        value = next((item for item in station['attributes'] if (item['code'] == 'lat' and item['date_to'] is None)), {'value': None})['value']
    #        if value:
    #            data['lat'] = float(value)
    #        value = next((item for item in station['attributes'] if (item['code'] == 'lon' and item['date_to'] is None)), {'value': None})['value']
    #        if value:
    #            data['lon'] = float(value)
    #
    #        if (station_list and data['station_identifier'] in station_list) or station_list is None:
    #            #if data['station_identifier'] in station_list:
    #            stations.append(data)
    #
    #    #print(stations)

    inv = Inventory(networks=[], source="TosTOOLS")

    net = Network(
        code="VI",
        stations=[],
        description="SIL  Icelandic national seismic network",
        start_date=obspy.UTCDateTime(1990, 10, 10),
    )

    sta = Station(
        code="ada",
        latitude=1.0,
        longitude=2.0,
        elevation=345.0,
        creation_date=obspy.UTCDateTime(2016, 1, 2),
        site=Site(name="Aðalból"),
    )

    cha = Channel(
        code="HHZ",
        location_code="",
        latitude=1.0,
        longitude=2.0,
        elevation=345.0,
        depth=10.0,
        azimuth=0.0,
        dip=-90.0,
        sample_rate=200,
    )

    response = nrl.get_response(
        sensor_keys=["Streckeisen", "STS-1", "360 seconds"],
        datalogger_keys=["REF TEK", "RT 130 & 130-SMA", "1", "200"],
    )

    # Now tie it all together
    cha.response = response
    sta.channels.append(cha)
    net.stations.append(sta)
    inv.networks.append(net)

    # Valid formats: CSS, KML, SACPZ, SHAPEFILE, STATIONTXT, STATIONXML
    inv.write("station.fdsn.xml", format="stationxml", validate=True)
    # inv.write("station", format="css")
    # inv.write("station.txt", format="stationtxt")


KNOWN_SUBCOMMANDS = {"owners", "device", "audit"}


def _owners_main(argv):
    """Handle `tos owners ...` subcommands."""
    from .api.tos_client import TOSClient
    from .owners import KNOWN_OWNERS, OwnersCache

    p = argparse.ArgumentParser(
        prog="tos owners",
        description="Manage the recognized TOS device-owner allow-list.",
    )
    sub = p.add_subparsers(dest="action", required=True)

    p_list = sub.add_parser("list", help="List recognized owner labels.")
    p_list.add_argument(
        "--refresh",
        action="store_true",
        help="Probe TOS to verify each owner is still in use; rewrites the cache.",
    )
    p_list.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_list.add_argument(
        "--cache-path",
        help="Override the cache file path (default: ~/.config/tostools/owners.yaml).",
    )
    p_list.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_list.add_argument("--port", type=int, default=443)

    args = p.parse_args(argv)

    if args.action != "list":
        p.error(f"unknown action: {args.action}")
        return 2

    cache_path = args.cache_path
    cache = OwnersCache(cache_path) if cache_path else OwnersCache()

    if args.refresh:
        scheme = "https" if args.port == 443 else "http"
        base_url = f"{scheme}://{args.server}:{args.port}/tos/v1"
        client = TOSClient(base_url=base_url)
        result = cache.refresh(client)
        owners = result.in_use
        missing = result.missing
    else:
        owners = cache.load()
        missing = []

    if args.json:
        import json as _json

        payload = {
            "owners": owners,
            "missing": missing,
            "cache_path": str(cache.cache_path),
            "seed": list(KNOWN_OWNERS),
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for o in owners:
            print(o)
        if missing:
            print(
                "\nMissing from TOS (not found via basic_search):",
                file=sys.stderr,
            )
            for o in missing:
                print(f"  - {o}", file=sys.stderr)
    return 0


def _device_main(argv):
    """Handle ``tos device ...`` subcommands.

    Step 3 of the device-warehouse interface — adds a brand-new device entity
    (gnss_receiver, antenna, radome, monument) to TOS with strict input
    validation, owner allow-list checking, IGS model normalisation, and a
    duplicate-serial guard (bypassable with ``--force``). Defaults to dry-run.
    """
    from . import device as device_helpers
    from .api.tos_writer import TOSWriter
    from .owners import OwnersCache

    p = argparse.ArgumentParser(
        prog="tos device",
        description="Manage TOS device entities (warehouse intake).",
    )
    sub = p.add_subparsers(dest="action", required=True)

    p_add = sub.add_parser("add", help="Add a new device entity to TOS.")
    p_add.add_argument(
        "--subtype",
        required=True,
        choices=device_helpers.VALID_SUBTYPES,
        help="Device subtype.",
    )
    p_add.add_argument("--serial", required=True, help="Device serial number.")
    p_add.add_argument(
        "--model",
        required=True,
        help="Equipment model. Normalised to IGS rcvr_ant.tab format.",
    )
    p_add.add_argument(
        "--owner",
        required=True,
        help="Owner label; must match an entry in the OwnersCache.",
    )
    p_add.add_argument("--location", required=True, help="Physical location.")
    p_add.add_argument(
        "--date-start",
        required=True,
        help="Start date for all attribute values (YYYY-MM-DD or "
        "YYYY-MM-DDTHH:MM:SS).",
    )
    p_add.add_argument("--firmware", help="Optional firmware_version attribute.")
    p_add.add_argument("--comment", help="Optional free-form comment attribute.")
    p_add.add_argument(
        "--galvos", help="Optional galvos (inventory/registration) number."
    )
    p_add.add_argument(
        "--force",
        action="store_true",
        help="Bypass the duplicate-serial guard from create_device.",
    )
    p_add.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes. Without this flag, payloads are logged only.",
    )
    p_add.add_argument(
        "--owners-cache",
        help="Override the owners cache path "
        "(default: ~/.config/tostools/owners.yaml).",
    )
    p_add.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_add.add_argument("--port", type=int, default=443)
    p_add.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary instead of plain text.",
    )

    args = p.parse_args(argv)
    if args.action != "add":
        p.error(f"unknown action: {args.action}")
        return 2

    # ---- Input validation ------------------------------------------------
    try:
        date_start = device_helpers.normalize_date_start(args.date_start)
    except ValueError as e:
        print(f"Invalid --date-start: {e}", file=sys.stderr)
        return 2

    cache = OwnersCache(args.owners_cache) if args.owners_cache else OwnersCache()
    known_owners = cache.load()
    if args.owner not in known_owners:
        print(
            f"Unknown owner: {args.owner!r}. "
            f"Run 'tos owners list' to see allowed values, or "
            f"'tos owners list --refresh' if you recently added one in TOS.",
            file=sys.stderr,
        )
        return 2

    try:
        igs_model = device_helpers.validate_model(args.subtype, args.model)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    required = device_helpers.build_required_attributes(
        serial=args.serial,
        model=igs_model,
        owner=args.owner,
        location=args.location,
        date_start=date_start,
    )
    optional = device_helpers.iter_optional_attributes(
        firmware=args.firmware,
        comment=args.comment,
        galvos=args.galvos,
    )

    # ---- Writer setup ----------------------------------------------------
    scheme = "https" if args.port == 443 else "http"
    base_url = f"{scheme}://{args.server}:{args.port}/tos/v1"
    dry_run = not args.no_dry_run
    writer = TOSWriter(base_url=base_url, dry_run=dry_run)

    # ---- Create entity ---------------------------------------------------
    try:
        response = writer.create_device(args.subtype, required, force=args.force)
    except ValueError as e:
        msg = str(e)
        if "already exists" in msg and not args.force:
            print(f"{msg}\nPass --force to add the duplicate anyway.", file=sys.stderr)
        else:
            print(msg, file=sys.stderr)
        return 1

    # In dry-run, response is a DryRunResult (no id_entity). In live mode,
    # response is the TOS API dict containing id_entity for the new entity.
    id_entity = None
    if isinstance(response, dict):
        id_entity = response.get("id_entity")

    # ---- Optional attributes --------------------------------------------
    upsert_responses = []
    for code, value in optional:
        if dry_run or id_entity is None:
            print(
                f"DRY RUN: would upsert {code}={value!r} "
                f"from {date_start} on id_entity="
                f"{id_entity if id_entity is not None else '<new entity>'}"
            )
            upsert_responses.append({"code": code, "value": value, "dry_run": True})
        else:
            r = writer.upsert_attribute_value(
                id_entity, code=code, value=value, date_from=date_start
            )
            upsert_responses.append({"code": code, "value": value, "response": r})

    # ---- Summary ---------------------------------------------------------
    if args.json:
        import json as _json

        payload = {
            "subtype": args.subtype,
            "serial": args.serial,
            "model": igs_model,
            "owner": args.owner,
            "location": args.location,
            "date_start": date_start,
            "id_entity": id_entity,
            "dry_run": dry_run,
            "required_attributes": required,
            "optional_attributes": [{"code": c, "value": v} for c, v in optional],
            "upsert_results": upsert_responses,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        suffix = " (dry-run)" if dry_run else ""
        id_str = id_entity if id_entity is not None else "<would be assigned>"
        print(
            f"Created {args.subtype} serial={args.serial} "
            f"id_entity={id_str}{suffix}"
        )
    return 0


def _audit_main(argv):
    """Handle ``tos audit <kind>`` subcommands.

    Step 1 of the device-warehouse implementation order — read-only invariant
    checks for devices (I1) and stations (I2) per the design doc at
    ``2.Areas/VI_GPS_Library/1778592216-device-warehouse-design.md``.

    Exit codes: 0 = clean, 1 = invariant violation detected, 2 = usage error
    or entity not found. Completeness warnings on a station are advisory and
    do **not** affect the exit code.
    """
    import json as _json
    from pathlib import Path

    from . import audit as audit_mod
    from .api.tos_client import TOSClient

    p = argparse.ArgumentParser(
        prog="tos audit",
        description=(
            "Verify TOS device-warehouse invariants. Read-only; no "
            "credentials required."
        ),
        epilog=(
            "WHAT TOS TRACKS\n"
            "  Every GPS device (receiver, antenna, radome, monument) is its\n"
            "  own entity in TOS. The location of a device — 'this receiver\n"
            "  is plugged into station X' — is NOT stored as an attribute. It\n"
            "  is a parent-child *join* record with a date range:\n"
            "    time_from = day the device was attached\n"
            "    time_to   = day it was removed (NULL for the current state)\n"
            "  When a device moves, the old join should close (time_to is\n"
            "  set) and a new one open. B9-Jörð (id_entity=4) is the virtual\n"
            "  'warehouse' station: a device that is not deployed in the\n"
            "  field should be joined to B9.\n"
            "\n"
            "INVARIANTS THIS COMMAND CHECKS (TOS does not enforce them)\n"
            "  I1  Every device has exactly one open join at any moment.\n"
            "      Violations:\n"
            "        I1 no-parent : id_entity_parent on the device is null.\n"
            "        I1 orphan    : the device's last join was closed but no\n"
            "                       replacement was opened — the device is\n"
            "                       'in limbo'.\n"
            "        I1 multi-open: more than one open join to the same\n"
            "                       parent (internally inconsistent).\n"
            "  I2  Every station has at most one open join per device\n"
            "      subtype (no two active receivers, etc., at one station).\n"
            "  Completeness (advisory, never blocks):\n"
            "      A full GPS station has one open receiver + antenna +\n"
            "      monument. Partial sets are legal but flagged.\n"
            "\n"
            "WHY THIS MATTERS\n"
            "  Wrong joins propagate into RINEX metadata, dashboards, GAMIT,\n"
            "  IGS site logs. Audit catches inconsistencies before they leak\n"
            "  downstream.\n"
            "\n"
            "Examples:\n"
            "  tos audit device --serial 3235768 --subtype receiver\n"
            "  tos audit device --id 21489\n"
            "  tos audit device --id 21489 --verbose\n"
            "  tos audit station RHOF\n"
            "  tos audit station --id 4               # B9 - Kjallari - Jörð\n"
            "  tos audit orphans --subtype receiver\n"
            "  tos audit orphans --subtype receiver --verbose\n"
            "  tos audit orphans --subtype receiver --model POLARX5 --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="kind", required=True)

    p_dev = sub.add_parser(
        "device",
        help="Audit one device (invariant I1).",
        description=(
            "Verify that one device has exactly one open join to its current "
            "parent. Exits 0 on I1 OK, 1 on I1 violation, 2 on lookup or "
            "usage error."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit device --serial 3235768 --subtype receiver\n"
            "  tos audit device --id 21489\n"
            "  tos audit device --id 21489 --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dev_target = p_dev.add_mutually_exclusive_group(required=True)
    dev_target.add_argument(
        "--serial", help="Device serial number; requires --subtype."
    )
    dev_target.add_argument(
        "--id", dest="id_entity", type=int, help="Device id_entity."
    )
    p_dev.add_argument(
        "--subtype",
        choices=sorted(set(audit_mod.SUBTYPE_ALIASES)),
        help="Device subtype (short or canonical). Required with --serial.",
    )
    p_dev.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_dev.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_dev.add_argument("--port", type=int, default=443)
    p_dev.add_argument(
        "--verbose",
        action="store_true",
        help="On violations, add a plain-English block explaining what it "
        "means, the expected state, and how to fix it.",
    )

    p_st = sub.add_parser(
        "station",
        help="Audit one station (real station: I2; warehouse: inventory).",
        description=(
            "Subtype-aware. For a real physical station (code_entity_subtype = "
            "'geophysical' — Jarðeðlisstöð such as RHOF), verify I2 (at most "
            "one open join per device subtype) and emit non-blocking "
            "completeness warnings when expected subtypes are missing. "
            "For a warehouse-style entity (Lager, such as B9 - Kjallari - "
            "Jörð, id_entity=4), I2 does not apply — render an inventory "
            "listing instead. Exits 0 on I2 OK (or warehouse), 1 on I2 "
            "violation, 2 on lookup or usage error.\n\n"
            "The positional argument matches either the station's marker "
            "(short id, like 'RHOF') or its display name ('Raufarhöfn'). "
            "Markers are tried first."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit station RHOF                 # marker lookup\n"
            "  tos audit station Raufarhöfn           # display-name lookup\n"
            "  tos audit station --id 4               # B9 - Kjallari - Jörð (warehouse)\n"
            "  tos audit station RHOF --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    st_target = p_st.add_mutually_exclusive_group(required=True)
    st_target.add_argument(
        "name", nargs="?", help="Station marker (e.g. RHOF) or display name."
    )
    st_target.add_argument(
        "--id", dest="id_entity", type=int, help="Station id_entity."
    )
    p_st.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_st.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_st.add_argument("--port", type=int, default=443)
    p_st.add_argument(
        "--verbose",
        action="store_true",
        help="On violations, add a plain-English block explaining what it "
        "means, the expected state, and how to fix it.",
    )

    p_orph = sub.add_parser(
        "orphans",
        help="List I1-orphan devices across the fleet (scan-by-model).",
        description=(
            "Enumerate devices of a given subtype via basic_search on a list "
            "of model strings, audit each, and report those with I1 "
            "violations (closed-without-replacement orphans, multi-open "
            "joins, or no current parent). For gnss_receivers the default "
            "list covers the enumerable fleet (~322 devices across modern + "
            "legacy models) as discovered via a TOS probe on 2026-05-12.\n\n"
            "Known limitation: TOS basic_search mis-indexes hyphen-and-digit "
            "patterns, so ASHTECH Z-XII3 receivers cannot be enumerated by "
            "any model search. Use `tos audit device --id <n>` for those, "
            "or wait for `cfg fix` (todo #5) to enumerate by join graph "
            "instead.\n\n"
            "Exits 0 when no violations are found, 1 when at least one "
            "violation is reported, 2 on usage error."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit orphans --subtype receiver\n"
            "  tos audit orphans --subtype receiver --model POLARX5 --model NetR9\n"
            "  tos audit orphans --subtype receiver --json\n"
            "\n"
            "Default models per subtype (used when --model is not given):\n"
            + "\n".join(
                f"  {sub}: {', '.join(models)}"
                for sub, models in audit_mod.DEFAULT_ORPHAN_SCAN_MODELS.items()
            )
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_orph.add_argument(
        "--subtype",
        required=True,
        choices=sorted(set(audit_mod.SUBTYPE_ALIASES)),
        help="Device subtype to scan (short or canonical).",
    )
    p_orph.add_argument(
        "--model",
        action="append",
        dest="models",
        help=(
            "Model string passed to basic_search (repeatable). When omitted, "
            "uses the subtype's DEFAULT_ORPHAN_SCAN_MODELS list."
        ),
    )
    p_orph.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_orph.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_orph.add_argument("--port", type=int, default=443)
    p_orph.add_argument(
        "--verbose",
        action="store_true",
        help="Add a plain-English preamble explaining what an I1 orphan is "
        "and how to fix one.",
    )

    p_fleet = sub.add_parser(
        "fleet-gaps",
        help="Report devices whose join history has unrecorded coverage gaps.",
        description=(
            "Walk every known parent's children_connections, build the global "
            "join index, and surface devices whose timeline contains gaps "
            "longer than --min-days. A gap is the time between the close of "
            "one join and the open of the next; when such a stretch exists, "
            "the device was somewhere TOS does not record (typically: sat at "
            "B9 after pickup, or was sent for repair, but the move was never "
            "entered).\n\n"
            "This is a *report*, not an invariant gate — exit 0 always, "
            "even when gaps are reported. Use --json to feed the output into "
            "downstream tooling, or follow up on individual rows with "
            "`tos audit device --id <n>`.\n\n"
            "Empirical baseline (2026-05-12 fleet probe): with --min-days 30, "
            "the IMO fleet surfaces ~50 devices and ~115 gaps; with "
            "--min-days 365, ~40 devices in the high-confidence tail. Below "
            "~7 days the result set is dominated by date-rounding artifacts."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit fleet-gaps                                # ≥30d gaps + orphans\n"
            "  tos audit fleet-gaps --min-days 365                 # high-confidence tail\n"
            "  tos audit fleet-gaps --subtype receiver --json      # GNSS receivers, JSON\n"
            "  tos audit fleet-gaps --top 10 --no-orphans          # top 10 longest gaps\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_fleet.add_argument(
        "--min-days",
        type=float,
        default=30.0,
        help="Minimum gap duration in days (default: 30). Below ~7 the "
        "report fills with date-rounding noise; ≥365 isolates the "
        "high-confidence tail.",
    )
    p_fleet.add_argument(
        "--subtype",
        choices=sorted(set(audit_mod.SUBTYPE_ALIASES)),
        help="Filter to one device subtype. Requires per-device enrichment "
        "(implicit unless --no-enrich is set).",
    )
    p_fleet.add_argument(
        "--top",
        type=int,
        default=None,
        help="Show only the N rows with the longest gaps (rows are already "
        "sorted by max gap descending).",
    )
    p_fleet.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip per-device subtype/serial/model lookup. Faster, but the "
        "output only carries id_entity. Incompatible with --subtype.",
    )
    p_fleet.add_argument(
        "--no-orphans",
        action="store_true",
        help="Suppress the truly-orphan section (devices with closed joins "
        "but none open). Default reports both gaps and orphans.",
    )
    p_fleet.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_fleet.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_fleet.add_argument("--port", type=int, default=443)
    p_fleet.add_argument(
        "--verbose",
        action="store_true",
        help="Print a per-gap detail block for every row, not just the "
        "single longest gap. Independent of --json.",
    )
    p_fleet.add_argument(
        "--no-progress",
        action="store_true",
        help="Suppress the parent-walk progress line on stderr.",
    )
    p_fleet.add_argument(
        "--with-timelines",
        action="store_true",
        help="Embed each device's complete join history under its row. "
        "Reuses the same index walk (no extra cost). Use this when "
        "drilling down from a fleet-gap row needs the surrounding "
        "context — equivalent to running `timeline` for every "
        "surfaced device in a single invocation.",
    )

    p_tl = sub.add_parser(
        "timeline",
        help="Print one or more devices' complete join history (drill-down).",
        description=(
            "Walk the global join index once, then dump every join — open "
            "or closed — for each requested device id, in chronological "
            "order. Gaps between adjacent joins are annotated inline. This "
            "is the drill-down companion to `fleet-gaps`: use that to find "
            "interesting devices, then pass their id_entity values here to "
            "see what TOS actually has on file.\n\n"
            "Pass multiple ids in a single invocation so the ~110s index "
            "build is amortised — `timeline 16321 4926 16576 19712` is one "
            "build, four lookups. Default `--min-gap-days=0` surfaces every "
            "gap (timeline view normally wants the full picture, unlike "
            "fleet-gaps which filters noise)."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit timeline 19969                             # one device, full history\n"
            "  tos audit timeline 16321 4926 16576 19712            # the fleet-gaps top 4\n"
            "  tos audit timeline 16581 --json                      # structured output\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_tl.add_argument(
        "ids",
        nargs="+",
        type=int,
        help="One or more device id_entity values to report on.",
    )
    p_tl.add_argument(
        "--min-gap-days",
        type=float,
        default=0.0,
        help="Threshold for gap annotations. Default 0 (every gap shown). "
        "Raise to suppress short date-rounding artifacts.",
    )
    p_tl.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip per-device subtype/serial/model lookup. The join history "
        "is still complete; only the header line loses metadata.",
    )
    p_tl.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_tl.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_tl.add_argument("--port", type=int, default=443)
    p_tl.add_argument(
        "--no-progress",
        action="store_true",
        help="Suppress the parent-walk progress lines on stderr.",
    )

    p_apply = sub.add_parser(
        "apply",
        help="Apply ACTION lines from an operator-edited triage file.",
        description=(
            "Read an action file and apply each ACTION line as a TOS write. "
            "Default is dry-run: every action is logged but no HTTP write "
            "goes out. Pass --apply to commit.\n\n"
            "File format — one ACTION per line, '#' for comments:\n"
            "  ACTION <id_entity> <verb> [args...]\n\n"
            "Verbs:\n"
            "  change-subtype <code>     PUT /admin_entity_row/<id>/ "
            "with id_entity_subtype=<resolved-int>\n"
            "  decommission <date>       Close the device's open join + "
            "transition status to óvirkt on <date>\n"
            "  move <to_parent_id> <date>\n"
            "                            Close the device's open join + "
            "open a new join at <to_parent_id> on <date>\n"
            "  fill-gap <parent_id> <date_from> <date_to>\n"
            "                            POST a closed join for a known "
            "historical window (cfg-fix backfill)\n"
            "  patch-attribute-date <code> <old_date_from> <new_date_from>\n"
            "                            PATCH /attribute_value/<id> "
            "date_from — consumes triage files from\n"
            "                            `tos audit attribute-dates --triage`\n"
            "  defer                      no-op placeholder (review next run)\n\n"
            "Validation: each ACTION line is parsed before any HTTP call. "
            "If any line is malformed, nothing is sent. Otherwise actions "
            "run in file order; a single failed write logs the error and "
            "continues to the next."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit apply triage.txt              # dry-run (default)\n"
            "  tos audit apply triage.txt --apply      # commit writes\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_apply.add_argument(
        "action_file",
        help="Path to the action file (text, one ACTION per line).",
    )
    p_apply.add_argument(
        "--apply",
        action="store_true",
        help="Commit writes. Without this flag, payloads are logged only "
        "(safe default).",
    )
    p_apply.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_apply.add_argument("--port", type=int, default=443)
    p_apply.add_argument(
        "--json", action="store_true", help="Emit a structured JSON summary."
    )

    p_show = sub.add_parser(
        "show",
        help="Display a device's full record — attributes + (optional) join chronology.",
        description=(
            "Print the complete TOS record for one device: header, every "
            "attribute period (status / firmware / model / ...) grouped by "
            "code, and (when --no-joins is not set) the full chronological "
            "join history. Useful for verifying what TOS knows about a "
            "device after a write, or as input to deciding fill-gap / "
            "decommission actions.\n\n"
            "Accepts the device by id (preferred when known) or by "
            "(serial, subtype). The subtype is the canonical TOS code "
            "(``digitizer``, ``gps_clock``, ``gnss_receiver``, ...) — see "
            "vault note ``1778677922-tos-entity-subtype-codes`` for the "
            "full list. Unlike ``audit device --id N``, this verb is "
            "subtype-agnostic and works on the broader fleet."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit show --id 19712                # full record + joins (slow)\n"
            "  tos audit show --id 19712 --no-joins     # attributes only (fast)\n"
            "  tos audit show --serial G2584 --subtype digitizer --no-joins\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    show_target = p_show.add_mutually_exclusive_group(required=True)
    show_target.add_argument(
        "--id", dest="id_entity", type=int, help="Device id_entity."
    )
    show_target.add_argument(
        "--serial", help="Device serial number; requires --subtype."
    )
    p_show.add_argument(
        "--subtype",
        help="Canonical TOS subtype code (digitizer, gps_clock, ...). "
        "Required with --serial.",
    )
    p_show.add_argument(
        "--no-joins",
        action="store_true",
        help="Skip the join chronology (the ~110s index build). Attribute "
        "periods are still printed in full.",
    )
    p_show.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_show.add_argument("--port", type=int, default=443)

    p_attr = sub.add_parser(
        "attribute-dates",
        help="Flag TOS attribute periods misdated by data-entry stamp (rule 3).",
        description=(
            "Detect attribute periods whose `date_from` is later than the "
            "device's earliest known signal. TOS auto-stamps a period's "
            "date_from with the date the value was entered, not the date it "
            "became applicable — so retroactive data entry produces phantom "
            "transition dates that propagate into PrintTOS / sitelog / "
            "GAMIT. The discriminator is the station-side join time_from: "
            "when every attribute on a device is stamped at the entry date "
            "but the station's join carries a much earlier time_from, that "
            "contradiction surfaces the bug.\n\n"
            "Rule 3 fires when ``period.date_from > min(earliest attribute "
            "date_from, earliest station-side join time_from)``. By default "
            "only inherent codes (per data/attribute_codes.yaml) are "
            "checked — firmware bumps and other mutable transitions are "
            "skipped. Pass --include-mutable to widen.\n\n"
            "Exits 0 when no violations found, 1 when at least one is, "
            "2 on lookup / usage error. The (id_entity, code, date_from) "
            "triple in each violation is the natural suppression key for "
            "Layer 3 (data/audit_suppressions/attribute_dates.txt) — not "
            "implemented in this layer."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit attribute-dates ARHO\n"
            "  tos audit attribute-dates ARHO --verbose\n"
            "  tos audit attribute-dates RHOF --include-mutable --json\n"
            "  tos audit attribute-dates --id 1234 --subtypes antenna monument\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_attr.add_argument(
        "name", nargs="?", help="Station marker (e.g. ARHO) or display name."
    )
    p_attr.add_argument("--id", dest="id_entity", type=int, help="Station id_entity.")
    p_attr.add_argument(
        "--subtypes",
        nargs="+",
        help=(
            "Device subtypes to audit (short or canonical). Default: "
            "gnss_receiver, antenna, radome, monument."
        ),
    )
    p_attr.add_argument(
        "--include-mutable",
        action="store_true",
        help="Also check mutable codes (firmware bumps, status transitions, "
        "etc.). Default is inherent-only.",
    )
    p_attr.add_argument(
        "--include",
        action="append",
        default=[],
        dest="include",
        metavar="CODE[,CODE...]",
        help="Audit these codes regardless of their catalog classification "
        "(mutable, TODO, applies_to mismatch, gps_relevance=no all "
        "bypassed). Surgical alternative to --include-mutable. Repeatable, "
        "and each value may be a comma-separated list. Unknown codes "
        "raise an error with did-you-mean suggestions.",
    )
    p_attr.add_argument(
        "--exclude",
        action="append",
        default=[],
        dest="exclude",
        metavar="CODE[,CODE...]",
        help="Drop these codes entirely — not flagged, not even tracked as "
        "suppressed. Station-wide silencer (coarser than the per-violation "
        "SUPPRESS file). On conflict with --include, --exclude wins.",
    )
    p_attr.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Override the catalog YAML path. Defaults to repo "
        "data/attribute_codes.yaml or $TOSTOOLS_ATTRIBUTE_CODES_PATH.",
    )
    p_attr.add_argument(
        "--suppressions",
        type=Path,
        default=None,
        help="Override the suppression file path. Defaults to "
        "data/audit_suppressions/attribute_dates.txt. File-not-found is "
        "silent (the file is opt-in).",
    )
    p_attr.add_argument(
        "--no-suppressions",
        action="store_true",
        help="Bypass the suppression file entirely; every rule-3 hit is "
        "reported. Useful to verify what a stale SUPPRESS line is hiding.",
    )
    p_attr.add_argument(
        "--triage",
        dest="triage_path",
        type=Path,
        default=None,
        help="Emit a draft ACTION file at this path. One commented "
        "`ACTION ... patch-attribute-date ...` line per violation, with "
        "earliest_known as the suggested new date_from. Feeds into "
        "`tos audit apply <file>` (dry-run by default).",
    )
    p_attr.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_attr.add_argument(
        "--verbose",
        action="store_true",
        help="Show extra context: anchor source per violation and any "
        "unknown attribute codes seen in TOS but missing from the catalog.",
    )
    p_attr.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_attr.add_argument("--port", type=int, default=443)

    args = p.parse_args(argv)

    scheme = "https" if args.port == 443 else "http"
    base_url = f"{scheme}://{args.server}:{args.port}/tos/v1"
    client = TOSClient(base_url=base_url)

    if args.kind == "device":
        if args.serial and not args.subtype:
            print("--subtype is required when using --serial", file=sys.stderr)
            return 2
        try:
            report = audit_mod.audit_device(
                client,
                serial=args.serial,
                id_entity=args.id_entity,
                subtype=args.subtype,
            )
        except (LookupError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2
        if args.json:
            print(
                _json.dumps(
                    _device_report_to_dict(report), ensure_ascii=False, indent=2
                )
            )
        else:
            _print_device_report(report, verbose=args.verbose)
        return 0 if report.invariant_I1_ok else 1

    if args.kind == "orphans":
        try:
            scan = audit_mod.list_orphan_devices(
                client, subtype=args.subtype, models=args.models
            )
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        if args.json:
            print(_json.dumps(_orphan_scan_to_dict(scan), ensure_ascii=False, indent=2))
        else:
            _print_orphan_scan(scan, verbose=args.verbose)
        return 1 if scan.orphan_reports else 0

    if args.kind == "station":
        try:
            report = audit_mod.audit_station(
                client, name=args.name, id_entity=args.id_entity
            )
        except (LookupError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2
        if args.json:
            print(
                _json.dumps(
                    _station_report_to_dict(report), ensure_ascii=False, indent=2
                )
            )
        else:
            _print_station_report(report, verbose=args.verbose)
        return 0 if report.invariant_I2_ok else 1

    if args.kind == "fleet-gaps":
        from . import history as history_mod

        if args.subtype and args.no_enrich:
            print(
                "--subtype requires per-device enrichment; drop --no-enrich.",
                file=sys.stderr,
            )
            return 2
        canonical_subtype = (
            audit_mod.canonical_subtype(args.subtype) if args.subtype else None
        )
        if args.no_progress or args.json or not sys.stderr.isatty():
            walk_progress = None
            enumerate_progress = None
        else:
            sys.stderr.write(
                "Resolving station markers (one basic_search per marker, "
                "~100s for the IMO fleet)...\n"
            )
            sys.stderr.flush()
            enumerate_progress = _stderr_progress("markers")
            walk_progress = _stderr_progress("parents")
        try:
            report = history_mod.scan_fleet_gaps(
                client,
                min_days=args.min_days,
                include_orphans=not args.no_orphans,
                enrich=not args.no_enrich,
                subtype=canonical_subtype,
                progress=walk_progress,
                enumerate_progress=enumerate_progress,
                with_timelines=args.with_timelines,
            )
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        if args.json:
            print(
                _json.dumps(
                    _fleet_gap_report_to_dict(report, top=args.top),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            _print_fleet_gap_report(report, top=args.top, verbose=args.verbose)
        return 0

    if args.kind == "apply":
        return _apply_main(args)

    if args.kind == "show":
        if args.serial and not args.subtype:
            print("--subtype is required when using --serial", file=sys.stderr)
            return 2
        try:
            display_device_record(
                client,
                serial=args.serial,
                id_entity=args.id_entity,
                subtype=args.subtype,
                with_joins=not args.no_joins,
            )
        except (LookupError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2
        return 0

    if args.kind == "timeline":
        from . import history as history_mod

        if args.no_progress or args.json or not sys.stderr.isatty():
            walk_progress = None
            enumerate_progress = None
        else:
            sys.stderr.write(
                "Resolving station markers (one basic_search per marker, "
                "~100s for the IMO fleet)...\n"
            )
            sys.stderr.flush()
            enumerate_progress = _stderr_progress("markers")
            walk_progress = _stderr_progress("parents")
        report = history_mod.get_device_timelines(
            client,
            args.ids,
            min_gap_days=args.min_gap_days,
            enrich=not args.no_enrich,
            progress=walk_progress,
            enumerate_progress=enumerate_progress,
        )
        if args.json:
            print(
                _json.dumps(
                    _timelines_report_to_dict(report),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            _print_timelines_report(report)
        return 0

    if args.kind == "attribute-dates":
        from . import audit_attribute_dates as add_mod

        # Flatten the repeatable + comma-separated --include / --exclude
        # forms into plain lists. argparse handles `--include a --include b`
        # via action='append'; we add comma-splitting on top so
        # `--include a,b` is equivalent. Why post-process here instead of
        # in argparse: type=callable would split a single value but lose
        # the per-flag composition with append.
        def _flatten_codes(values):
            out = []
            for v in values or []:
                out.extend(c.strip() for c in v.split(",") if c.strip())
            return out

        include_codes = _flatten_codes(args.include)
        exclude_codes = _flatten_codes(args.exclude)

        try:
            report = add_mod.audit_station_attribute_dates(
                client,
                name=args.name,
                id_entity=args.id_entity,
                subtypes=args.subtypes,
                include_mutable=args.include_mutable,
                include_codes=include_codes or None,
                exclude_codes=exclude_codes or None,
                catalog_path=args.catalog,
                suppressions_path=args.suppressions,
                use_suppressions=not args.no_suppressions,
            )
        except (LookupError, ValueError, FileNotFoundError) as e:
            print(str(e), file=sys.stderr)
            return 2
        if report.included_codes_unmatched:
            print(
                "note: --include matched 0 attributes on this station for: "
                f"{', '.join(report.included_codes_unmatched)} "
                "(typo? wrong station? wrong subtype filter?)",
                file=sys.stderr,
            )
        if report.suppressions_errors:
            print(
                f"warning: {len(report.suppressions_errors)} malformed line(s) "
                f"in {report.suppressions_path}:",
                file=sys.stderr,
            )
            for err in report.suppressions_errors:
                print(
                    f"  line {err.line_no}: {err.message}",
                    file=sys.stderr,
                )
        if args.triage_path:
            audit_cmd = "tos audit " + " ".join(argv)
            content = add_mod.format_triage_file(report, audit_command=audit_cmd)
            args.triage_path.write_text(content, encoding="utf-8")
            print(
                f"wrote triage file: {args.triage_path} "
                f"({len(report.violations)} violation(s))",
                file=sys.stderr,
            )
        if args.json:
            print(
                _json.dumps(
                    _attribute_date_report_to_dict(report),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            _print_attribute_date_report(report, verbose=args.verbose)
        return 1 if report.has_violations else 0

    p.error(f"unknown kind: {args.kind}")
    return 2


# ---------------------------------------------------------------------------
# Action-file parser + runner (operator-edited triage workflow)
# ---------------------------------------------------------------------------


@dataclass
class ParsedAction:
    """One action parsed from a triage file."""

    line_no: int
    id_entity: int
    verb: str
    args: List[str]
    raw: str


@dataclass
class ParseError:
    """One error encountered while parsing an action file."""

    line_no: int
    message: str
    raw: str


_SUPPORTED_VERBS = (
    "change-subtype",
    "decommission",
    "defer",
    "fill-gap",
    "move",
    "patch-attribute-date",
)


def _parse_action_file(text: str) -> tuple[List[ParsedAction], List[ParseError]]:
    """Parse a triage action file into (actions, errors).

    Format: one ACTION per line. Comments (``#`` to end-of-line) and blank
    lines are ignored. Each non-blank line must match::

        ACTION <id_entity> <verb> [args...]

    Returns both lists so the runner can report every malformed line at
    once instead of bailing on the first error.
    """
    actions: List[ParsedAction] = []
    errors: List[ParseError] = []
    for i, line in enumerate(text.splitlines(), 1):
        raw = line
        # Strip comments and surrounding whitespace.
        if "#" in line:
            line = line.split("#", 1)[0]
        line = line.strip()
        if not line:
            continue
        tokens = line.split()
        if tokens[0] != "ACTION":
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "expected line to start with 'ACTION' " f"(got {tokens[0]!r})"
                    ),
                    raw=raw,
                )
            )
            continue
        if len(tokens) < 3:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "ACTION line needs at least: ACTION <id> <verb> "
                        f"(got {len(tokens)} tokens)"
                    ),
                    raw=raw,
                )
            )
            continue
        try:
            id_entity = int(tokens[1])
        except ValueError:
            errors.append(
                ParseError(
                    line_no=i,
                    message=f"id_entity must be int, got {tokens[1]!r}",
                    raw=raw,
                )
            )
            continue
        verb = tokens[2]
        if verb not in _SUPPORTED_VERBS:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        f"unknown verb {verb!r}; supported: "
                        f"{', '.join(_SUPPORTED_VERBS)}"
                    ),
                    raw=raw,
                )
            )
            continue
        if verb == "change-subtype" and len(tokens) != 4:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "change-subtype requires exactly one argument: "
                        "the new subtype code"
                    ),
                    raw=raw,
                )
            )
            continue
        if verb == "defer" and len(tokens) != 3:
            errors.append(
                ParseError(
                    line_no=i,
                    message="defer takes no arguments",
                    raw=raw,
                )
            )
            continue
        if verb == "decommission" and len(tokens) != 4:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "decommission requires exactly one argument: the "
                        "retirement date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"
                    ),
                    raw=raw,
                )
            )
            continue
        if verb == "move" and len(tokens) != 5:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "move requires exactly two arguments: " "<to_parent_id> <date>"
                    ),
                    raw=raw,
                )
            )
            continue
        if verb == "fill-gap" and len(tokens) != 6:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "fill-gap requires exactly three arguments: "
                        "<parent_id> <date_from> <date_to>"
                    ),
                    raw=raw,
                )
            )
            continue
        if verb == "patch-attribute-date" and len(tokens) != 6:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "patch-attribute-date requires exactly three "
                        "arguments: <code> <old_date_from> <new_date_from>"
                    ),
                    raw=raw,
                )
            )
            continue
        actions.append(
            ParsedAction(
                line_no=i,
                id_entity=id_entity,
                verb=verb,
                args=tokens[3:],
                raw=raw,
            )
        )
    return actions, errors


@dataclass
class ActionResult:
    """Outcome of one action execution."""

    action: ParsedAction
    status: str  # "ok" | "deferred" | "failed"
    detail: str


def _dispatch_decommission(
    writer, action: ParsedAction, *, open_joins_by_device: "Dict[int, Any]"
) -> ActionResult:
    """Retire a device — close its open join (if any) + transition status.

    Two writes per device:

    1. **Close the open join** with ``time_to=<retirement_date>`` via
       :func:`devices.close_join`. Skipped (with a noted "no open join
       — skip close" in the detail) when the device has no open join —
       that's a legitimate state for a device already lifted out of
       TOS.
    2. **Transition the ``status`` attribute** from its open value (most
       commonly ``virkt``) to ``óvirkt`` via
       :func:`devices.transition_attribute`. History-preserving.

    A partial failure (e.g. join closed OK, status PATCH 400s) still
    returns ``failed`` so the operator notices, but the closed-join
    side will already have happened on the server. The detail string
    enumerates both write outcomes so the operator can see exactly
    what state TOS is in.

    The :func:`devices.decommission_device` composite does the same
    workflow plus an internal history fetch and "close all open
    joins" loop; the apply path uses the pre-computed
    ``open_joins_by_device`` cache from
    :func:`_build_open_joins_lookup` to avoid a redundant GET, and
    fleet survey shows zero devices with multiple open parent joins
    so the "close all" semantics is moot in practice. Both paths now
    invoke the same :func:`devices.close_join` /
    :func:`devices.transition_attribute` sub-primitives.
    """
    from . import devices

    retirement_date = action.args[0]
    open_join = open_joins_by_device.get(action.id_entity)

    join_detail = "no open join — skip close"
    if open_join is not None:
        try:
            devices.close_join(
                writer,
                id_connection=open_join.id_entity_connection,
                date_to=retirement_date,
            )
            join_detail = (
                f"PATCH /join/{open_join.id_entity_connection} "
                f"time_to={retirement_date} (parent={open_join.id_entity_parent})"
            )
        except Exception as exc:  # noqa: BLE001
            return ActionResult(
                action=action,
                status="failed",
                detail=f"patch_entity_connection raised: {exc}",
            )

    try:
        status_resp = devices.transition_attribute(
            writer,
            device_id=action.id_entity,
            code="status",
            new_value="óvirkt",
            date=retirement_date,
        )
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"join: {join_detail}; status transition raised: {exc}",
        )

    closed_part = (
        "closed prior status period"
        if status_resp.get("closed") is not None
        else "no prior status — opened first óvirkt period"
    )
    return ActionResult(
        action=action,
        status="ok",
        detail=f"join: {join_detail}; status: {closed_part} + óvirkt from {retirement_date}",
    )


def _dispatch_move(
    writer, action: ParsedAction, *, open_joins_by_device: "Dict[int, Any]"
) -> ActionResult:
    """Relocate a device — close its open join and open a new one on the same date.

    Unlike :func:`_dispatch_decommission`, a missing open join is a
    hard failure here: there's nothing to close, so the move is
    ill-defined. Don't silently POST the new join on its own — the
    operator's input file is wrong.

    The dispatcher invokes ``close_join`` + ``open_join`` directly
    (rather than the bundled :func:`devices.move_device` composite)
    so a second-step failure surfaces the first step's success in
    the detail string. That lets the operator see exactly what state
    TOS is in if the new join POST fails: the old parent is closed,
    the device is parent-less, and the new join needs manual
    creation.
    """
    from . import devices

    to_parent_token, date = action.args[0], action.args[1]
    try:
        to_parent_id = int(to_parent_token)
    except ValueError:
        return ActionResult(
            action=action,
            status="failed",
            detail=(f"move requires integer to_parent_id, got {to_parent_token!r}"),
        )

    open_join = open_joins_by_device.get(action.id_entity)
    if open_join is None:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"cannot move device {action.id_entity}: no open parent "
                "join to close"
            ),
        )

    try:
        devices.close_join(
            writer, id_connection=open_join.id_entity_connection, date_to=date
        )
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"close_join raised: {exc}",
        )

    close_detail = (
        f"PATCH /join/{open_join.id_entity_connection} "
        f"time_to={date} (was parent={open_join.id_entity_parent})"
    )

    try:
        devices.open_join(
            writer,
            parent_id=to_parent_id,
            child_id=action.id_entity,
            date_from=date,
        )
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"close: {close_detail}; open_join raised: {exc} — "
                "device is parent-less, manual cleanup needed"
            ),
        )

    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"close: {close_detail}; "
            f"open: POST /join parent={to_parent_id} date_from={date}"
        ),
    )


def _dispatch_fill_gap(writer, action: ParsedAction) -> ActionResult:
    """Backfill a closed historical join for a known window.

    Pure single-write verb — no prerequisite reads. The action shape
    is ``ACTION <child_id> fill-gap <parent_id> <date_from>
    <date_to>``. Surfaces the writer's response or exception verbatim
    in the detail string.
    """
    from . import devices

    parent_token, date_from, date_to = action.args[0], action.args[1], action.args[2]
    try:
        parent_id = int(parent_token)
    except ValueError:
        return ActionResult(
            action=action,
            status="failed",
            detail=f"fill-gap requires integer parent_id, got {parent_token!r}",
        )

    try:
        devices.fill_join_gap(
            writer,
            parent_id=parent_id,
            child_id=action.id_entity,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"fill_join_gap raised: {exc}",
        )

    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"POST /join parent={parent_id} child={action.id_entity} "
            f"{date_from} → {date_to}"
        ),
    )


def _dispatch_patch_attribute_date(writer, action: ParsedAction) -> ActionResult:
    """Re-date an existing TOS attribute period in-place.

    Action shape: ``ACTION <id_entity> patch-attribute-date <code>
    <old_date_from> <new_date_from>``. Looks up the attribute period
    via fresh writer.get_attribute_values (so we never operate on a
    stale id_attribute_value), then PATCHes ``date_from``.

    Match rule
    ----------
    A period matches when its ``date_from`` *date-only* prefix
    (``YYYY-MM-DD``) equals ``old_date_from``. The same normalisation
    applied at audit / suppression time — without it, ``"2014-10-17"
    != "2014-10-17 00:00:00"`` lexically and the dispatcher would
    silently no-op against live TOS.

    Failure modes
    -------------
    * **Zero matches** — the audit's old_date_from doesn't appear on
      the device. Returns ``failed``; the operator should re-audit
      and regenerate the triage file.
    * **Multiple matches** — two or more periods for the same code
      share the same date-only ``date_from``. Refuse to PATCH rather
      than pick arbitrarily (silent corruption is the failure mode
      we're guarding against). Operator must disambiguate manually.
    * **Period has no ``id_attribute_value``** — partial TOS payload.
      Returns ``failed``; rerun against a fresh history.
    """
    code = action.args[0]
    old_date_raw = action.args[1]
    new_date_raw = action.args[2]

    # Normalise both date arguments to YYYY-MM-DD up front. Matches the
    # audit-time _date_only() contract and keeps the comparison robust
    # to operator-pasted datetimes.
    old_date = old_date_raw[:10]
    new_date = new_date_raw[:10]
    if len(new_date) != 10 or new_date[4] != "-" or new_date[7] != "-":
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-attribute-date: new_date_from must be YYYY-MM-DD "
                f"(got {new_date_raw!r})"
            ),
        )

    try:
        attrs = writer.get_attribute_values(action.id_entity, code)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"get_attribute_values raised: {exc}",
        )

    matches: List[Dict[str, Any]] = []
    for a in attrs:
        df = a.get("date_from")
        if not df:
            continue
        if str(df)[:10] == old_date:
            matches.append(a)

    if not matches:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-attribute-date: no period found for "
                f"id_entity={action.id_entity} code={code!r} "
                f"date_from={old_date} (re-audit and regenerate triage)"
            ),
        )
    if len(matches) > 1:
        ids = ", ".join(str(a.get("id_attribute_value")) for a in matches)
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-attribute-date: {len(matches)} periods match "
                f"id_entity={action.id_entity} code={code!r} "
                f"date_from={old_date} (id_attribute_value: {ids}); "
                "refusing to PATCH ambiguously — disambiguate manually"
            ),
        )

    target = matches[0]
    id_av_raw = target.get("id_attribute_value")
    if id_av_raw is None:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                "patch-attribute-date: matching period has no "
                "id_attribute_value (partial payload); rerun later"
            ),
        )

    try:
        id_av = int(id_av_raw)
    except (TypeError, ValueError):
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-attribute-date: id_attribute_value={id_av_raw!r} "
                "is not an integer (unexpected TOS payload shape)"
            ),
        )

    try:
        response = writer.patch_attribute_value(id_av, date_from=new_date)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"patch_attribute_value raised: {exc}",
        )

    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"PATCH /attribute_value/{id_av} "
            f"date_from {old_date} → {new_date} "
            f"(code={code!r}) — {response!r}"
        ),
    )


def _dispatch_action(
    writer,
    action: ParsedAction,
    *,
    subtype_id_by_code: "Dict[str, int] | None" = None,
    open_joins_by_device: "Dict[int, Any] | None" = None,
) -> ActionResult:
    """Apply one parsed action through ``writer``. Never raises.

    The writer's ``dry_run`` flag controls whether anything is sent over
    the wire — this function only knows about TOS-level semantics.

    ``subtype_id_by_code`` maps canonical subtype strings (``digitizer``,
    ``gps_clock``, ...) to the integer FK TOS uses on the admin write
    path. Built once per apply run by :func:`_apply_main` from
    ``GET /entity_subtypes/``. Required for the ``change-subtype`` verb;
    unused otherwise.

    ``open_joins_by_device`` maps device id_entity → its current open
    :class:`tostools.history.Join` (or None if no open join). Built once
    per apply run from the join index. Required for ``decommission``;
    unused otherwise.
    """
    if action.verb == "defer":
        return ActionResult(action=action, status="deferred", detail="defer (no-op)")
    if action.verb == "decommission":
        return _dispatch_decommission(
            writer, action, open_joins_by_device=open_joins_by_device or {}
        )
    if action.verb == "move":
        return _dispatch_move(
            writer, action, open_joins_by_device=open_joins_by_device or {}
        )
    if action.verb == "fill-gap":
        return _dispatch_fill_gap(writer, action)
    if action.verb == "patch-attribute-date":
        return _dispatch_patch_attribute_date(writer, action)
    if action.verb == "change-subtype":
        code = action.args[0]
        mapping = subtype_id_by_code or {}
        sid = mapping.get(code)
        if sid is None:
            return ActionResult(
                action=action,
                status="failed",
                detail=(
                    f"unknown subtype code {code!r} — not in TOS's "
                    "/entity_subtypes/ list. Check spelling against the "
                    "vault reference note."
                ),
            )
        try:
            response = writer.update_entity_subtype(action.id_entity, sid)
        except Exception as exc:  # noqa: BLE001
            return ActionResult(
                action=action,
                status="failed",
                detail=f"update_entity_subtype raised: {exc}",
            )
        return ActionResult(
            action=action,
            status="ok",
            detail=(
                f"PUT /admin_entity_row/{action.id_entity} "
                f"id_entity_subtype={sid} ({code!r}) — {response!r}"
            ),
        )
    # Unreachable — parser rejects unknown verbs.
    return ActionResult(
        action=action, status="failed", detail=f"unimplemented verb {action.verb!r}"
    )


def _build_open_joins_lookup(client, *, target_ids):
    """Build ``{device_id: open_join_or_None}`` via the global join index.

    The ~110s cost is the marker-resolution + parent walk inside
    :func:`tostools.history.build_join_index`. For the apply workflow
    this is the price of a decommission action — we need to know *which
    join* to close (it's keyed by ``id_entity_connection``, which only
    the parent's ``children_connections`` carries).

    Only includes devices in ``target_ids``; the rest of the index
    contents are discarded. The returned dict has one entry per target
    id; the value is the device's currently-open :class:`Join` or
    ``None`` if the device has no open join (already orphan).
    """
    from .history import build_join_index

    progress_to_stderr = sys.stderr.isatty()
    if progress_to_stderr:
        sys.stderr.write(
            "Building join index to locate open joins for decommission "
            "(~110s on the IMO fleet)...\n"
        )
        sys.stderr.flush()
    index = build_join_index(client)
    out: Dict[int, Any] = {}
    for did in target_ids:
        timeline = index.timeline(int(did))
        open_joins = timeline.open_joins
        out[int(did)] = open_joins[0] if open_joins else None
    return out


def _fetch_subtype_id_by_code(client) -> "Dict[str, int]":
    """Return a ``{code_entity_subtype: id_entity_subtype}`` mapping from TOS.

    Calls ``GET /entity_subtypes/`` once and folds the result. Needed by
    the ``change-subtype`` action verb: the operator types the canonical
    string code (e.g. ``digitizer``) but TOS's admin write path keys on
    the integer FK (``id``). See vault note
    `1778677922-tos-entity-subtype-codes` for the human-readable
    Icelandic ↔ code reference. Returns an empty dict if the endpoint
    is unreachable; callers should report the failure rather than
    silently writing wrong ids.
    """
    try:
        rows = client._make_request("/entity_subtypes/")
    except Exception:  # noqa: BLE001
        rows = None
    out: Dict[str, int] = {}
    for r in rows or []:
        code = r.get("code")
        sid = r.get("id")
        if code and isinstance(sid, int):
            out[code] = sid
    return out


def _fetch_action_meta(client, ids):
    """Return ``{id: {"subtype", "serial", "model"}}`` for each unique id.

    One :meth:`TOSClient.get_entity_history` call per unique id; cached
    in a local dict so repeats are free. A missing/unreadable entity
    yields a dict of ``None`` fields rather than raising — the apply
    runner uses ``?`` placeholders in that case and lets the writer
    decide whether the id is actually valid.

    Reuses the open-period attribute reader from :mod:`tostools.history`
    so we don't duplicate the ``date_to is None`` filtering logic.
    """
    from .history import _open_attribute_value

    meta_by_id: Dict[int, Dict[str, Any]] = {}
    for raw in ids:
        did = int(raw)
        if did in meta_by_id:
            continue
        entry: Dict[str, Any] = {"subtype": None, "serial": None, "model": None}
        try:
            history = client.get_entity_history(did)
        except Exception:  # noqa: BLE001
            history = None
        if history:
            entry["subtype"] = history.get("code_entity_subtype") or None
            attrs = history.get("attributes") or []
            entry["serial"] = _open_attribute_value(attrs, "serial_number")
            entry["model"] = _open_attribute_value(attrs, "model")
        meta_by_id[did] = entry
    return meta_by_id


def _fmt_action_verb(action: "ParsedAction") -> str:
    """Render a verb + its args as a single human-readable token string."""
    return (action.verb + " " + " ".join(action.args)).rstrip()


def _print_apply_preflight_table(actions, meta, *, mode_tag: str) -> None:
    """Print a pre-flight table summarising every action before HTTP runs.

    Operator scans the serial column against the TOS web UI in one pass
    before committing — the dry-run becomes self-sufficient as a
    pre-commit checklist.
    """
    rows = []
    for a in actions:
        m = meta.get(a.id_entity, {})
        rows.append(
            (
                str(a.id_entity),
                m.get("serial") or "?",
                m.get("model") or "?",
                m.get("subtype") or "?",
                _fmt_action_verb(a),
            )
        )
    headers = ("id", "serial", "model", "current_subtype", "→ action")
    widths = [
        max(len(h), max((len(r[i]) for r in rows), default=0))
        for i, h in enumerate(headers)
    ]
    print(f"Pre-flight ({len(actions)} action(s), {mode_tag}):")
    print()
    line = "  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    for row in rows:
        print("  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))
    print()


def _apply_main(args) -> int:
    """Handle ``tos audit apply <file>``.

    Strict-then-permissive: parse the whole file first, refuse to send
    any HTTP if a single line is malformed; once all lines parse, run
    them in file order, continuing past individual failures so an
    operator with N independent fixes doesn't see one bad line abort
    the rest.
    """
    import json as _json
    from pathlib import Path

    from .api.tos_client import TOSClient
    from .api.tos_writer import TOSWriter

    path = Path(args.action_file)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read action file: {exc}", file=sys.stderr)
        return 2

    actions, errors = _parse_action_file(text)
    if errors:
        print(
            f"Refusing to apply — {len(errors)} parse error(s) in {path}:",
            file=sys.stderr,
        )
        for err in errors:
            print(
                f"  line {err.line_no}: {err.message}\n    | {err.raw}",
                file=sys.stderr,
            )
        return 2

    scheme = "https" if args.port == 443 else "http"
    base_url = f"{scheme}://{args.server}:{args.port}/tos/v1"
    dry_run = not args.apply

    # Pre-flight metadata lookup. Read-only client (no auth needed) — separate
    # from the writer so authentication only happens if we actually need to
    # write. One GET per unique id; failed lookups yield None fields and the
    # row renders with `?` placeholders.
    client = TOSClient(base_url=base_url)
    meta = _fetch_action_meta(client, (a.id_entity for a in actions))

    # Build the subtype code → id resolver lazily — only when at least one
    # action actually needs it. Avoids the GET when every line is `defer`.
    needs_subtypes = any(a.verb == "change-subtype" for a in actions)
    subtype_id_by_code = _fetch_subtype_id_by_code(client) if needs_subtypes else {}

    # Build the open-join lookup lazily — only when decommission or move
    # appears. Both verbs need to close the device's currently-open parent
    # join. The index build is the ~110s cost; once built, the per-device
    # open join is an O(1) dict access. Shared across all such actions in
    # this run.
    _NEEDS_JOIN_INDEX = {"decommission", "move"}
    needs_join_index = any(a.verb in _NEEDS_JOIN_INDEX for a in actions)
    open_joins_by_device: Dict[int, Any] = {}
    if needs_join_index:
        open_joins_by_device = _build_open_joins_lookup(
            client,
            target_ids={a.id_entity for a in actions if a.verb in _NEEDS_JOIN_INDEX},
        )

    writer = TOSWriter(base_url=base_url, dry_run=dry_run)

    results: List[ActionResult] = []
    for action in actions:
        results.append(
            _dispatch_action(
                writer,
                action,
                subtype_id_by_code=subtype_id_by_code,
                open_joins_by_device=open_joins_by_device,
            )
        )

    if args.json:
        payload = {
            "file": str(path),
            "dry_run": dry_run,
            "total_actions": len(actions),
            "results": [
                {
                    "line_no": r.action.line_no,
                    "id_entity": r.action.id_entity,
                    "serial": meta.get(r.action.id_entity, {}).get("serial"),
                    "model": meta.get(r.action.id_entity, {}).get("model"),
                    "current_subtype": meta.get(r.action.id_entity, {}).get("subtype"),
                    "verb": r.action.verb,
                    "args": r.action.args,
                    "status": r.status,
                    "detail": r.detail,
                }
                for r in results
            ],
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        mode_tag = "DRY-RUN" if dry_run else "APPLY"
        print(f"Action file: {path}")
        _print_apply_preflight_table(actions, meta, mode_tag=mode_tag)

        for r in results:
            marker = {"ok": "✓", "deferred": "·", "failed": "✗"}.get(r.status, "?")
            m = meta.get(r.action.id_entity, {})
            sn = m.get("serial") or "?"
            model = m.get("model") or "?"
            cur = m.get("subtype") or "?"
            print(
                f"  {marker} line {r.action.line_no}: "
                f"id={r.action.id_entity} SN {sn} ({model}, {cur}) → "
                f"{_fmt_action_verb(r.action)}"
            )
            print(f"      → {r.detail}")
        counts = {"ok": 0, "deferred": 0, "failed": 0}
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1
        print(
            f"\nSummary: {counts['ok']} ok, {counts['deferred']} deferred, "
            f"{counts['failed']} failed"
        )
        if dry_run:
            print("(no writes were sent — re-run with --apply to commit)")

    return 0 if all(r.status != "failed" for r in results) else 1


def _stderr_progress(unit: str):
    """Return a (current, total) callback that overwrites a single stderr line.

    Used by ``tos audit fleet-gaps`` so the operator sees the parent walk
    is alive (the index build is the slow step, ~10s on the live IMO
    fleet). Skipped when stderr isn't a TTY to avoid polluting log files.
    """
    if not sys.stderr.isatty():
        return None

    def cb(current: int, total: int) -> None:
        end = "\n" if current == total else ""
        sys.stderr.write(f"\r  {unit}: {current}/{total}{end}")
        sys.stderr.flush()

    return cb


def _device_report_to_dict(report):
    """Convert a :class:`DeviceAuditReport` to a JSON-serialisable dict."""
    return {
        "kind": "device",
        "id_entity": report.id_entity,
        "subtype": report.subtype,
        "serial": report.serial,
        "current_parent_id": report.current_parent_id,
        "current_parent_name": report.current_parent_name,
        "current_parent_subtype": report.current_parent_subtype,
        "open_joins": [_join_to_dict(j) for j in report.open_joins],
        "invariant_I1_ok": report.invariant_I1_ok,
        "invariant_violations": list(report.invariant_violations),
    }


def _attribute_date_report_to_dict(report):
    """Convert a :class:`StationAttributeDateReport` to a JSON-serialisable dict."""

    def _violation_dict(v):
        return {
            "id_entity": v.id_entity,
            "subtype": v.subtype,
            "serial": v.serial,
            "code": v.code,
            "date_from": v.date_from,
            "value": v.value,
            "earliest_known": v.earliest_known,
            "anchor_source": v.anchor_source,
        }

    return {
        "kind": "attribute-dates",
        "station_id": report.station_id,
        "station_name": report.station_name,
        "audited_devices": report.audited_devices,
        "devices_skipped": report.devices_skipped,
        "unknown_codes": list(report.unknown_codes),
        "violations": [_violation_dict(v) for v in report.violations],
        "suppressed": [
            {
                **_violation_dict(s.violation),
                "suppressions_path": str(s.suppressions_path),
                "line_no": s.line_no,
            }
            for s in report.suppressed
        ],
        "suppressions_path": (
            str(report.suppressions_path) if report.suppressions_path else None
        ),
        "suppressions_disabled": report.suppressions_disabled,
        "suppressions_errors": [
            {"line_no": e.line_no, "message": e.message, "raw": e.raw}
            for e in report.suppressions_errors
        ],
        "included_codes": list(report.included_codes),
        "excluded_codes": list(report.excluded_codes),
        "included_codes_unmatched": list(report.included_codes_unmatched),
    }


def _print_attribute_date_report(report, *, verbose: bool = False):
    """Render an attribute-dates audit report as plain text on stdout.

    Groups violations under each device for a compact, human-readable layout.
    ``verbose=True`` shows:

    * the anchor source on each line (which signal pinned earliest_known)
    * a copy-pasteable ``SUPPRESS`` hint per violation
    * unknown attribute codes that the catalog doesn't cover yet
    * the suppressed entries that the file silenced, with file:lineno
      references — the only audit trail of silenced violations
    """
    status = "CLEAN" if not report.has_violations else "VIOLATIONS"
    marker = "✓" if not report.has_violations else "✗"
    name = report.station_name or "?"
    print(f"{marker} Station {name!r} (id_entity={report.station_id}) — " f"{status}")
    print(
        f"  audited devices: {report.audited_devices}  "
        f"(skipped {report.devices_skipped} outside requested subtypes)"
    )
    if report.suppressed_count:
        print(
            f"  suppressed: {report.suppressed_count} entry(ies) via "
            f"{report.suppressions_path}"
        )
    elif report.suppressions_disabled:
        print("  suppressions: disabled (--no-suppressions)")
    if report.included_codes:
        print(f"  --include codes: {', '.join(report.included_codes)}")
    if report.excluded_codes:
        print(f"  --exclude codes: {', '.join(report.excluded_codes)}")

    if report.violations:
        # Group by (device_id, subtype, serial) to render a compact block per device.
        by_device: Dict[int, List] = {}
        device_meta: Dict[int, tuple] = {}
        for v in report.violations:
            by_device.setdefault(v.id_entity, []).append(v)
            device_meta[v.id_entity] = (v.subtype, v.serial)
        print()
        print(f"  flagged ({len(report.violations)} period(s)):")
        for did in sorted(by_device):
            subtype, serial = device_meta[did]
            serial_label = f" SN {serial!r}" if serial else ""
            print(f"    {subtype} id_entity={did}{serial_label}")
            for v in by_device[did]:
                value_part = f" value={v.value!r}" if v.value is not None else ""
                if verbose:
                    print(
                        f"      · {v.code:24s} date_from={v.date_from}  "
                        f"(earliest_known={v.earliest_known}, "
                        f"anchor={v.anchor_source}){value_part}"
                    )
                    # Copy-pasteable SUPPRESS line — closes the loop between
                    # detection and committing a suppression.
                    print(
                        f"        suppress: SUPPRESS {v.id_entity} "
                        f"{v.code} {v.date_from}"
                    )
                else:
                    print(
                        f"      · {v.code:24s} date_from={v.date_from}  "
                        f"earliest_known={v.earliest_known}{value_part}"
                    )

    if verbose and report.suppressed:
        print()
        print(f"  suppressed ({len(report.suppressed)} silenced entry(ies)):")
        by_device_s: Dict[int, List] = {}
        device_meta_s: Dict[int, tuple] = {}
        for s in report.suppressed:
            v = s.violation
            by_device_s.setdefault(v.id_entity, []).append(s)
            device_meta_s[v.id_entity] = (v.subtype, v.serial)
        for did in sorted(by_device_s):
            subtype, serial = device_meta_s[did]
            serial_label = f" SN {serial!r}" if serial else ""
            print(f"    {subtype} id_entity={did}{serial_label}")
            for s in by_device_s[did]:
                v = s.violation
                value_part = f" value={v.value!r}" if v.value is not None else ""
                print(
                    f"      · {v.code:24s} date_from={v.date_from}  "
                    f"(suppressed at {s.suppressions_path}:{s.line_no})"
                    f"{value_part}"
                )

    if verbose and report.unknown_codes:
        print()
        print(
            f"  unknown attribute codes (seen in TOS, missing from catalog): "
            f"{len(report.unknown_codes)}"
        )
        for code in report.unknown_codes:
            print(f"    - {code}")
    elif report.unknown_codes and not verbose:
        print()
        print(
            f"  ({len(report.unknown_codes)} unknown attribute code(s); "
            f"re-run with --verbose to list them)"
        )


def _station_report_to_dict(report):
    """Convert a :class:`StationAuditReport` to a JSON-serialisable dict."""
    return {
        "kind": "station",
        "id_entity": report.id_entity,
        "name": report.name,
        "subtype": report.subtype,
        "is_real_station": report.is_real_station,
        "open_children_by_subtype": {
            subtype: [_join_to_dict(j) for j in joins]
            for subtype, joins in report.open_children_by_subtype.items()
        },
        "invariant_I2_ok": report.invariant_I2_ok,
        "invariant_violations": list(report.invariant_violations),
        "completeness_warnings": list(report.completeness_warnings),
    }


def _join_to_dict(join):
    if join is None:
        return None
    return {
        "id_entity_parent": join.id_entity_parent,
        "id_entity_child": join.id_entity_child,
        "parent_name": join.parent_name,
        "child_subtype": join.child_subtype,
        "time_from": join.time_from,
        "time_to": join.time_to,
    }


def _fleet_gap_report_to_dict(report, *, top=None):
    """Convert a :class:`history.FleetGapReport` to a JSON-serialisable dict.

    Honors ``top`` by trimming the device list to the N rows with the
    longest gaps (rows are pre-sorted by max-gap descending).

    When the report was built with ``with_timelines=True``, each device
    dict carries a ``timeline`` field with its complete join history;
    otherwise that field is ``None``.
    """
    devices = report.devices if top is None else report.devices[: max(top, 0)]
    return {
        "kind": "fleet-gaps",
        "min_days": report.min_days,
        "build": {
            "parents_walked": report.parents_walked,
            "parents_failed": report.parents_failed,
            "total_joins": report.total_joins,
            "total_devices": report.total_devices,
        },
        "summary": {
            "devices_with_gaps": report.devices_with_gaps,
            "gap_count": report.gap_count,
            "truly_orphan": report.orphan_count,
            "rows_returned": len(devices),
        },
        "parent_names": {
            str(pid): name for pid, name in sorted(report.parent_names.items())
        },
        "devices": [
            {
                "id_entity": d.id_entity,
                "subtype": d.subtype,
                "serial": d.serial,
                "model": d.model,
                "is_truly_orphan": d.is_truly_orphan,
                "last_parent_id": d.last_parent_id,
                "last_parent_name": d.last_parent_name,
                "max_gap_days": d.max_gap_days,
                "gaps": [
                    {
                        "after_parent": g.after.id_entity_parent,
                        "before_parent": g.before.id_entity_parent,
                        "time_from": g.time_from,
                        "time_to": g.time_to,
                        "duration_days": g.duration_days,
                    }
                    for g in d.gaps
                ],
                "timeline": _embedded_timeline_to_dict(d.timeline, report.parent_names),
            }
            for d in devices
        ],
    }


def _embedded_timeline_to_dict(timeline, parent_names):
    """Render an embedded DeviceTimelineReport as a dict, or None.

    Used by the fleet-gaps `--with-timelines` JSON path. Excludes the
    redundant id_entity/subtype/serial/model fields (they're already on
    the surrounding device row) — the embedded view is just the joins
    + every gap, which is what the headline row doesn't carry.
    """
    if timeline is None:
        return None
    return {
        "is_currently_attached": timeline.is_currently_attached,
        "joins": [
            {
                "id_entity_connection": j.id_entity_connection,
                "id_entity_parent": j.id_entity_parent,
                "parent_name": parent_names.get(j.id_entity_parent),
                "time_from": j.time_from,
                "time_to": j.time_to,
                "is_open": j.is_open,
            }
            for j in timeline.joins
        ],
        "all_gaps": [
            {
                "after_parent": g.after.id_entity_parent,
                "before_parent": g.before.id_entity_parent,
                "time_from": g.time_from,
                "time_to": g.time_to,
                "duration_days": g.duration_days,
            }
            for g in timeline.gaps
        ],
    }


def _print_fleet_gap_report(report, *, top=None, verbose: bool = False):
    """Render a fleet-gap report as plain text on stdout.

    Layout: header counts, gap-bearing rows (one line per device, with
    the longest gap inline), then a separate truly-orphan section.
    ``verbose=True`` lists every gap on every gap-bearing device, not
    just the longest. ``top`` trims the gap-bearing rows; orphans are
    always shown in full when present.
    """
    print(
        f"Fleet gap report — min duration {report.min_days:g} days "
        f"(walked {report.parents_walked} parents, "
        f"{report.parents_failed} failed; "
        f"{report.total_joins} joins / {report.total_devices} devices indexed)"
    )

    gap_rows = [d for d in report.devices if d.gaps]
    orphan_rows = [d for d in report.devices if d.is_truly_orphan and not d.gaps]
    print(
        f"  {report.devices_with_gaps} device(s) with gaps ≥{report.min_days:g}d, "
        f"{report.gap_count} gap(s) total"
    )
    if report.orphan_count:
        print(
            f"  {report.orphan_count} device(s) truly orphan (joins exist, none open)"
        )
    print()

    shown_gap_rows = gap_rows if top is None else gap_rows[: max(top, 0)]
    if shown_gap_rows:
        suffix = (
            f" (top {len(shown_gap_rows)} of {len(gap_rows)})"
            if top is not None and len(shown_gap_rows) < len(gap_rows)
            else ""
        )
        print(f"Gaps{suffix}:")
        for d in shown_gap_rows:
            _print_fleet_gap_row(d, verbose=verbose, parent_names=report.parent_names)
    elif gap_rows:
        # `top=0` edge case
        print(f"Gaps: (suppressed by --top {top})")

    if orphan_rows:
        print()
        print("Truly orphan:")
        for d in orphan_rows:
            _print_fleet_orphan_row(d)

    if not gap_rows and not orphan_rows:
        print("No gaps or orphans matched the current filters.")


def _fmt_device_label(d) -> str:
    """Format the leading id/serial/model/subtype block for one device row."""
    serial = d.serial or "?"
    model = d.model or "?"
    subtype = d.subtype or "?"
    return f"id={d.id_entity:<6d} SN {serial:<14s} {model:<18s} {subtype}"


def _fmt_gap(g) -> str:
    """Format a single Gap as ``Nd  from → to  (parent → parent)``."""
    return (
        f"{g.duration_days:>6.0f}d  "
        f"{g.time_from} → {g.time_to}  "
        f"(parent {g.after.id_entity_parent} → {g.before.id_entity_parent})"
    )


def _print_fleet_gap_row(d, *, verbose: bool, parent_names=None) -> None:
    label = _fmt_device_label(d)
    gaps = sorted(d.gaps, key=lambda g: -g.duration_days)
    show_block = verbose or len(gaps) == 1 or d.timeline is not None
    if show_block:
        print(f"  {label}")
        for g in gaps:
            print(f"      {_fmt_gap(g)}")
    else:
        # Headline: one line per device, longest gap inline; mention overflow.
        print(f"  {label}  {_fmt_gap(gaps[0])}  (+{len(gaps) - 1} more)")
    if d.timeline is not None:
        _print_embedded_timeline(d.timeline, parent_names or {})


def _print_embedded_timeline(timeline, parent_names):
    """Render the embedded join history under a fleet-gap row.

    ``parent_names`` is the report-level dict from
    :func:`scan_fleet_gaps`; entries not in it fall back to ``?``.
    """
    joins = timeline.joins
    if not joins:
        print("      (no joins indexed — device unreachable from any walked parent)")
        return
    print(f"      Full history — {len(joins)} join(s):")
    gap_by_after_id: Dict[int, Any] = {id(g.after): g for g in timeline.gaps}
    for i, j in enumerate(joins, 1):
        kind = "open  " if j.is_open else "closed"
        pname = parent_names.get(j.id_entity_parent) or "?"
        end = j.time_to if j.time_to is not None else "—"
        print(
            f"        {i:2d}. [{kind}] {j.time_from} → {end}   "
            f"parent={j.id_entity_parent} ({pname})"
        )
        g = gap_by_after_id.get(id(j))
        if g is not None:
            print(f"              ⚠ gap of {g.duration_days:.0f}d before next join")


def _print_fleet_orphan_row(d) -> None:
    label = _fmt_device_label(d)
    if d.last_parent_id is not None:
        parent = d.last_parent_name or "?"
        tail = f"last at {parent!r} (id_entity={d.last_parent_id})"
    else:
        tail = "no closed-join history available"
    print(f"  {label}  {tail}")


def _timelines_report_to_dict(report):
    """Convert a :class:`history.TimelinesReport` to a JSON-serialisable dict."""
    return {
        "kind": "timeline",
        "build": {
            "parents_walked": report.parents_walked,
            "parents_failed": report.parents_failed,
            "total_joins": report.total_joins,
            "total_devices": report.total_devices,
        },
        "parent_names": {
            str(pid): name for pid, name in sorted(report.parent_names.items())
        },
        "timelines": [
            {
                "id_entity": t.id_entity,
                "subtype": t.subtype,
                "serial": t.serial,
                "model": t.model,
                "is_currently_attached": t.is_currently_attached,
                "is_truly_orphan": t.is_truly_orphan,
                "joins": [
                    {
                        "id_entity_connection": j.id_entity_connection,
                        "id_entity_parent": j.id_entity_parent,
                        "parent_name": report.parent_names.get(j.id_entity_parent),
                        "time_from": j.time_from,
                        "time_to": j.time_to,
                        "is_open": j.is_open,
                    }
                    for j in t.joins
                ],
                "gaps": [
                    {
                        "after_parent": g.after.id_entity_parent,
                        "before_parent": g.before.id_entity_parent,
                        "time_from": g.time_from,
                        "time_to": g.time_to,
                        "duration_days": g.duration_days,
                    }
                    for g in t.gaps
                ],
            }
            for t in report.timelines
        ],
    }


def _print_timelines_report(report):
    """Render a TimelinesReport as plain text on stdout.

    One block per device: header (id / subtype / serial / model / state),
    then a numbered list of every join in chronological order, with the
    gap before each non-first join annotated inline.
    """
    print(
        f"Timeline lookup — walked {report.parents_walked} parents, "
        f"{report.parents_failed} failed; "
        f"{report.total_joins} joins / {report.total_devices} devices indexed"
    )
    for t in report.timelines:
        print()
        _print_one_timeline(t, report.parent_names)


def _print_one_timeline(t, parent_names):
    subtype = t.subtype or "?"
    serial = t.serial or "?"
    model = t.model or "?"
    state_bits = []
    if t.is_currently_attached:
        state_bits.append("currently attached")
    if t.is_truly_orphan:
        state_bits.append("truly orphan (no open join)")
    if not state_bits:
        state_bits.append("no joins indexed" if not t.joins else "state unknown")
    state = "; ".join(state_bits)
    print(
        f"Device id={t.id_entity}  SN {serial!r}  model {model!r}  "
        f"{subtype}  — {state}"
    )

    if not t.joins:
        print("  (no joins in the walked parent set — device unreachable)")
        return

    print(f"  History — {len(t.joins)} join(s):")
    gap_by_after_id: Dict[int, Any] = {id(g.after): g for g in t.gaps}
    for i, j in enumerate(t.joins, 1):
        kind = "open  " if j.is_open else "closed"
        pname = parent_names.get(j.id_entity_parent) or "?"
        end = j.time_to if j.time_to is not None else "—"
        print(
            f"    {i:2d}. [{kind}] {j.time_from} → {end}   "
            f"parent={j.id_entity_parent} ({pname})"
        )
        g = gap_by_after_id.get(id(j))
        if g is not None:
            print(
                f"          ⚠ gap of {g.duration_days:.0f}d before next join "
                f"({g.time_from} → {g.time_to})"
            )


def _orphan_scan_to_dict(scan):
    """Convert an :class:`OrphanScanResult` to a JSON-serialisable dict."""
    return {
        "kind": "orphans",
        "subtype": scan.subtype,
        "models_searched": list(scan.models_searched),
        "total_audited": scan.total_audited,
        "violation_count": scan.violation_count,
        "orphan_reports": [_device_report_to_dict(r) for r in scan.orphan_reports],
    }


def _print_orphan_scan(scan, *, verbose: bool = False):
    """Render an orphan-scan summary as plain text on stdout.

    Default: one row per orphan. ``verbose=True`` prepends a paragraph
    explaining what an I1 orphan is and how to fix one (shared preamble
    covers every row, since they're all the same violation type).
    """
    from . import audit as audit_mod

    print(
        f"Scanned {scan.total_audited} {scan.subtype} devices "
        f"(models: {', '.join(scan.models_searched)}) — "
        f"{scan.violation_count} I1 violation(s)."
    )
    if not scan.orphan_reports:
        print("  (no orphans found)")
        return
    if verbose:
        print()
        print(audit_mod.orphan_scan_preamble())
        print()
        print("Orphans:")
    for r in scan.orphan_reports:
        serial = r.serial or "?"
        if r.current_parent_id is None:
            tail = "no current parent in TOS"
        else:
            tail = (
                f"last at {r.current_parent_name!r} "
                f"(id_entity={r.current_parent_id})"
            )
        print(f"  ✗ id_entity={r.id_entity} SN {serial}  {tail}")
    if not verbose:
        print()
        print("(run with --verbose for what this means and how to fix)")


def _print_device_report(report, *, verbose: bool = False):
    """Render a device audit report as plain text on stdout.

    Default: header + structural lines + short tagged violation strings.
    ``verbose=True`` appends a three-block explainer (What this means /
    Expected state / To fix).
    """
    from . import audit as audit_mod

    serial = report.serial or "?"
    if report.current_parent_id is None:
        parent = "<none>"
    else:
        subtype_tag = (
            f", {report.current_parent_subtype}"
            if report.current_parent_subtype
            else ""
        )
        parent = (
            f"id_entity={report.current_parent_id} "
            f"({report.current_parent_name!r}{subtype_tag})"
        )
    status = "I1 OK" if report.invariant_I1_ok else "I1 VIOLATION"
    marker = "✓" if report.invariant_I1_ok else "✗"
    print(
        f"{marker} Device {report.subtype} SN {serial} "
        f"(id_entity={report.id_entity}) — {status}"
    )
    print(f"  current parent: {parent}")
    if not report.open_joins:
        print("  open joins: <none>")
    elif len(report.open_joins) == 1:
        j = report.open_joins[0]
        end = j.time_to or "present"
        print(f"  open join: {j.time_from} → {end}")
    else:
        print(f"  open joins: {len(report.open_joins)} (I1 violation)")
        for j in report.open_joins:
            end = j.time_to or "present"
            print(f"    - {j.time_from} → {end}")
    for v in report.invariant_violations:
        print(f"  · {v}")
    if report.invariant_violations:
        if verbose:
            print()
            print(audit_mod.explain_device_violations(report))
        else:
            print()
            print("  (run with --verbose for what this means and how to fix)")


def _print_station_report(report, *, verbose: bool = False):
    """Render a station audit report as plain text on stdout.

    Default: header + open-children list + short violation/warning strings.
    ``verbose=True`` appends a three-block explainer for I2 violations.
    """
    from . import audit as audit_mod

    subtype_tag = f", {report.subtype}" if report.subtype else ""
    if not report.is_real_station:
        # Warehouse-style entity (e.g. B9 - Kjallari - Jörð). I2 doesn't
        # apply — render an inventory listing instead. No violation marker.
        print(
            f"📦 Inventory at {report.name!r} "
            f"(id_entity={report.id_entity}{subtype_tag})"
        )
        if not report.open_children_by_subtype:
            print("  (no open children)")
        else:
            counts = {k: len(v) for k, v in report.open_children_by_subtype.items()}
            total = sum(counts.values())
            print(f"  {total} open device(s):")
            for st in sorted(counts):
                print(f"    {st:14s} {counts[st]}")
            if verbose:
                print()
                print("  detail:")
                for st in sorted(report.open_children_by_subtype):
                    for j in report.open_children_by_subtype[st]:
                        print(
                            f"    {st:14s} id_entity={j.id_entity_child} "
                            f"from {j.time_from}"
                        )
        return

    status = "I2 OK" if report.invariant_I2_ok else "I2 VIOLATION"
    marker = "✓" if report.invariant_I2_ok else "✗"
    print(
        f"{marker} Station {report.name!r} "
        f"(id_entity={report.id_entity}{subtype_tag}) — {status}"
    )
    if not report.open_children_by_subtype:
        print("  (no open children)")
    else:
        print("  open children:")
        for subtype in sorted(report.open_children_by_subtype):
            joins = report.open_children_by_subtype[subtype]
            for j in joins:
                print(
                    f"    {subtype:14s} id_entity={j.id_entity_child} "
                    f"from {j.time_from}"
                )
    for v in report.invariant_violations:
        print(f"  · {v}")
    for w in report.completeness_warnings:
        print(f"  ⚠ {w}")
    if report.invariant_violations:
        if verbose:
            print()
            print(audit_mod.explain_station_violations(report))
        else:
            print()
            print("  (run with --verbose for what this means and how to fix)")


def _legacy_main(argv):
    """Pre-subcommand flat-arg behavior — kept for backward compatibility."""
    parser = argparse.ArgumentParser(
        add_help=True, description="TOS tools - Version 0.3"
    )
    parser.add_argument(
        "identifiers",
        nargs="*",
        help="Identifiers to use, station_identifier or serial_number",
    )
    parser.add_argument(
        "-D",
        "--domain",
        help="Only search specific domain (meteorological, geophysical, hydrological, remote_sensing, general)",
    )
    parser.add_argument(
        "-s",
        "--serial_number",
        action="store_true",
        help="Search for device by serial_number",
    )
    parser.add_argument("-d", "--devices", action="store_true", help="Include devices)")
    parser.add_argument(
        "-G", "--galvos", action="store_true", help="Search for device by GALVOS number"
    )
    parser.add_argument(
        "-x",
        "--exclude",
        action="store_true",
        help="Exclude the specified station_identifier:s instead of including",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="pretty",
        help="Output format; pretty (default), table, json",
    )
    parser.add_argument(
        "-t",
        "--tablefmt",
        default="simple",
        help="Tableformat for output; simple (default), plain, github, grid...See https://pypi.org/project/tabulate/ for full list",
    )
    parser.add_argument(
        "--fdsnxml",
        action="store_true",
        help="Generate FDSN XML file for SIL stations. Defaults to all SIL stations if no station_identifier is provided.",
    )
    parser.add_argument(
        "--sc3ml",
        action="store_true",
        help="Generate SC3ML XML (Gempa Seiscomp3) file for SIL stations. Defaults to all SIL stations if no station_identifier is provided.",
    )
    parser.add_argument(
        "--compareto",
        help="Compare generated SC3ML XML file structure to the specified file",
    )
    parser.add_argument(
        "--schema_version",
        help="XML schema version. Supported versions: 0.9, 0.10, 0.11",
    )
    # parser.add_argument('-p', '--pdf', action="store_true", help='Export PDF')
    # parser.add_argument('-l', '--language', help='Language for output. Default IS')

    args = parser.parse_args(argv)

    # Check args
    if not argv:
        parser.print_help(sys.stderr)
        return 2

    # Detect flag-only invocation (no action selected, no identifiers).
    action_selected = (
        args.serial_number
        or args.galvos
        or args.sc3ml
        or args.fdsnxml
        or bool(args.identifiers)
    )
    if not action_selected:
        parser.print_help(sys.stderr)
        return 2

    if args.serial_number:
        if args.exclude:
            logging.warning("Ignoring option --exclude with serial_number search")
        devices = []
        for serial_number in args.identifiers:
            device = searchDevice(serial_number)
            if len(device) == 0:
                logging.warning(f"Device with serial_number {serial_number} not found")
            else:
                devices += [device]

        for device in devices:
            display(device, args.output, args.tablefmt)

    elif args.galvos:
        if args.exclude:
            logging.warning("Ignoring option --exclude with galvos search")
        devices = []
        for galvos in args.identifiers:
            device = searchDevice(galvos=galvos)
            if len(device) == 0:
                logging.warning(f"Device with Galvos number {galvos} not found")
            else:
                devices += [device]

        for device in devices:
            display(device, args.output, args.tablefmt)

    elif args.sc3ml:
        # from obspy.clients.nrl import NRL
        parseSeiscompInventoryXML("master_inventory.xml")
        # print(inventory)

        if args.schema_version:
            if args.schema_version not in ["0.9", "0.10", "0.11"]:
                logging.critical(
                    f"Unsupported XML schema version {args.schema_version}"
                )
                return 2

        if args.identifiers:
            sc3ml = generateSC3ML(args.identifiers, args.schema_version)
        else:
            sc3ml = generateSC3ML(schema_version=args.schema_version)

        # Write to file
        with open("tos.sc3ml.xml", "w", encoding="utf-8") as f:
            f.write(sc3ml)

        if args.compareto is not None:
            # from obspy import read_inventory
            # inventory = read_inventory(args.compareto, format='SC3ML')
            # compareXMLtoInventory(xml,inventory)
            with open(args.compareto, encoding="utf-8") as comparefile:
                compareSC3(sc3ml, comparefile)

    elif args.fdsnxml:

        # master_inventory = parseSeiscompInventoryXML('master_inventory.xml')
        # print(inventory)

        if args.identifiers:
            generateFDSNXML(args.identifiers)
        else:
            generateFDSNXML()

    elif args.identifiers:
        if args.exclude:
            logging.warning("Ignoring option --exclude with station_identifier search")
        stations = []

        for station_identifier in args.identifiers:
            station = searchStation(station_identifier, url_rest_tos, args.domain)
            id_entity = station[0]["id_entity"]
            if args.devices:
                devices = getDevicesByParentEntityId(id_entity)
                station[0]["devices"] = devices

            if len(station) == 0:
                logging.warning(
                    f"Station with station_identifier {station_identifier} not found"
                )
            else:
                stations += [station]

        for station in stations:
            display(station, args.output, args.tablefmt)

    return 0


def _print_top_level_help() -> None:
    """Umbrella help — lists every subcommand AND keeps the legacy form visible.

    The dispatch in :func:`main` accepts both subcommand-style (``tos
    audit timeline ...``) and the original Tryggvi flat-arg form (``tos
    RHOF -s``). Argparse's own help only knows about whichever form it
    happens to enter, so neither view alone reflects the full surface.
    Print our own.
    """
    print(
        "usage: tos <command> [args...]\n"
        "       tos <legacy flat-arg form>   # backward-compat, see `tos legacy --help`\n"
        "\n"
        "GPS / GNSS station-metadata tool. Two CLI generations live side\n"
        "by side here:\n"
        "\n"
        "Subcommand-style verbs (current development surface):\n"
        "  owners     Manage the recognised TOS device-owner allow-list.\n"
        "             Examples: `tos owners list`, `tos owners list --refresh`.\n"
        "\n"
        "  device     Manage device entities (warehouse intake).\n"
        "             Examples: `tos device add --subtype gnss_receiver --serial ...`.\n"
        "\n"
        "  audit      Read-only invariants + history reconstruction.\n"
        "             Subverbs:\n"
        "               audit device --id N | --serial SN --subtype TYPE\n"
        "                            I1 single-device audit (current parent +\n"
        "                            open joins).\n"
        "               audit station NAME | --id N\n"
        "                            I2 single-station audit; inventory view\n"
        "                            for warehouses.\n"
        "               audit orphans --subtype TYPE\n"
        "                            Fleet I1-orphan scan (model-search\n"
        "                            enumeration).\n"
        "               audit fleet-gaps [--min-days N] [--subtype TYPE]\n"
        "                            Fleet-wide gap-detection report from\n"
        "                            the global join index.\n"
        "               audit timeline ID [ID ...]\n"
        "                            Per-device complete join history.\n"
        "               audit show --id N | --serial SN --subtype TYPE\n"
        "                            Full device record (attributes +\n"
        "                            optional join chronology).\n"
        "               audit apply <file> [--apply]\n"
        "                            Operator-edited action file —\n"
        "                            change-subtype / decommission / defer.\n"
        "                            Dry-run by default.\n"
        "\n"
        "Legacy flat-arg form (Tryggvi original, station/device lookup):\n"
        "  tos RHOF                       # query one station\n"
        "  tos -s 3018484                 # search by serial number\n"
        "  tos --fdsnxml --sc3ml ...      # XML export\n"
        "  Run `tos legacy --help` for the full legacy option list.\n"
        "\n"
        "Per-subcommand help: `tos <subcommand> --help`.\n"
    )


def main(argv=None):
    """Entry point — dispatches to `tos <subcommand> ...` or the legacy CLI.

    Two coexisting CLI generations. The legacy flat-arg form
    (``tos RHOF -s``) remains the default fall-through for backward
    compatibility; the modern subcommand-style form (``tos audit ...``)
    is routed via :data:`KNOWN_SUBCOMMANDS` before falling through.

    ``tos --help`` / ``tos -h`` prints a custom umbrella help listing
    both surfaces — argparse's per-parser help only sees its own
    arguments, which is why the bare ``tos --help`` historically only
    showed the legacy flags.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        _print_top_level_help()
        return 0
    if argv and argv[0] == "legacy":
        # Explicit opt-in to the legacy parser's own argparse help.
        return _legacy_main(argv[1:])
    if argv and argv[0] in KNOWN_SUBCOMMANDS:
        subcmd = argv[0]
        rest = argv[1:]
        if subcmd == "owners":
            return _owners_main(rest)
        if subcmd == "device":
            return _device_main(rest)
        if subcmd == "audit":
            return _audit_main(rest)
    return _legacy_main(argv)


# ---------------------------------------------------------------------------
# Device-record display (REPL/scripting helper)
# ---------------------------------------------------------------------------


def display_device_record(
    client=None,
    *,
    serial: "str | None" = None,
    id_entity: "int | None" = None,
    subtype: "str | None" = None,
    with_joins: bool = True,
    file=None,
) -> None:
    """Print everything TOS knows about one device.

    Accepts either ``id_entity`` (preferred) **or** ``(serial, subtype)``
    — same contract as :func:`tostools.audit.audit_device`. Useful from
    the REPL after a write to verify TOS state matches intent (status
    transitions, decommissions, subtype changes — all visible in one
    block).

    Output sections:

    1. **Header** — id, subtype, currently-open serial / model.
    2. **Attribute periods** — every attribute code grouped together,
       periods sorted chronologically. Shows closed AND open periods so
       the historical record is visible (status transitions land here).
    3. **Join history** — when ``with_joins=True`` (default), uses the
       global join index to dump every join the device has ever had,
       chronologically. **Triggers a ~110s index build** on first call;
       pass ``with_joins=False`` to skip when you only want attributes.

    Reuses :func:`tostools.audit._resolve_device_entity` for the lookup
    path and :func:`tostools.history.build_join_index` for the
    chronology. The on-screen rendering mirrors
    :func:`_print_one_timeline` so the visual shape is consistent
    across the ``tos audit timeline`` and ``device`` CLI verbs.

    Args:
        client: An unauthenticated :class:`TOSClient`. If omitted, one
            is constructed against the default ``vi-api.vedur.is`` host
            — useful for one-line REPL invocations.
        serial: Device serial number; requires ``subtype``.
        id_entity: Device primary key (preferred when known).
        subtype: Required with ``serial`` to disambiguate (a serial can
            legitimately collide across device types).
        with_joins: Whether to build the join index and print the join
            chronology. Off-by-default mode (False) is the fast path:
            no parent walk, just attribute periods.
        file: Output stream; defaults to ``sys.stdout``. Pass an
            ``io.StringIO`` to capture for tests.
    """
    import sys as _sys

    from .api.tos_client import TOSClient
    from .devices import (
        attribute_periods as _attribute_periods,
    )
    from .devices import (
        find_device as _find_device,
    )
    from .devices import (
        open_attribute as _open_attribute,
    )

    if file is None:
        file = _sys.stdout
    if client is None:
        client = TOSClient()

    history = _find_device(client, serial=serial, id_entity=id_entity, subtype=subtype)
    did = int(history["id_entity"])
    dev_subtype = history.get("code_entity_subtype") or "?"

    open_serial = _open_attribute(history, "serial_number") or "?"
    open_model = _open_attribute(history, "model") or "?"
    open_status = _open_attribute(history, "status") or "<no status attribute>"
    parent_id = history.get("id_entity_parent")

    print(
        f"Device id={did}  subtype={dev_subtype}  SN {open_serial!r}  "
        f"model {open_model!r}",
        file=file,
    )
    print(
        f"  current status: {open_status}  "
        f"id_entity_parent (stale field): {parent_id}",
        file=file,
    )

    # ---- Attribute periods ------------------------------------------------
    by_code = _attribute_periods(history)
    total_periods = sum(len(v) for v in by_code.values())
    print(file=file)
    print(f"Attribute periods ({total_periods}):", file=file)
    for code in sorted(by_code):
        print(f"  {code}:", file=file)
        for p in by_code[code]:
            df = p.get("date_from") or "?"
            dt = p.get("date_to") or "open"
            v = p.get("value")
            marker = "·" if p.get("date_to") is None else " "
            print(
                f"    {marker} {df} → {dt:24s} value={v!r}",
                file=file,
            )

    # ---- Join history -----------------------------------------------------
    if not with_joins:
        print(file=file)
        print(
            "(join history skipped — pass with_joins=True for the full "
            "chronology, costs ~110s for the index build)",
            file=file,
        )
        return

    from .history import build_join_index, enumerate_known_parents

    print(file=file)
    print(
        "Building join index for chronology (~110s on the IMO fleet)...",
        file=_sys.stderr,
    )
    parents = enumerate_known_parents(client)
    parent_names: Dict[int, "str | None"] = {p.id_entity: p.name for p in parents}
    index = build_join_index(client, parents=parents)
    timeline = index.timeline(did)

    state_bits = []
    if timeline.is_currently_attached:
        state_bits.append("currently attached")
    if timeline.is_truly_orphan:
        state_bits.append("truly orphan (no open join)")
    if not timeline.joins:
        state_bits.append("no joins indexed")
    state = "; ".join(state_bits) if state_bits else "state unknown"

    print(f"Join history — {state}:", file=file)
    if not timeline.joins:
        print("  (device unreachable from any walked parent)", file=file)
        return
    gaps = timeline.gaps(min_days=0.0)
    gap_by_after_id = {id(g.after): g for g in gaps}
    for i, j in enumerate(timeline.joins, 1):
        kind = "open  " if j.is_open else "closed"
        pname = parent_names.get(j.id_entity_parent) or "?"
        end = j.time_to if j.time_to is not None else "—"
        print(
            f"  {i:2d}. [{kind}] {j.time_from} → {end:24s} "
            f"parent={j.id_entity_parent} ({pname})",
            file=file,
        )
        g = gap_by_after_id.get(id(j))
        if g is not None:
            print(
                f"        ⚠ gap of {g.duration_days:.0f}d before next join "
                f"({g.time_from} → {g.time_to})",
                file=file,
            )


if __name__ == "__main__":
    sys.exit(main())
