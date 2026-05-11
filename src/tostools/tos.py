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
from datetime import datetime

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

    if domains == None:
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
                name = dataloggerXML.attrib["name"]
                publicID = dataloggerXML.attrib["publicID"]
                is_found = True
                break
        elif device["model"] == "Minimus":
            sensor_sensitivity = device["sensor_sensitivity"].replace(".", ",")
            description = dataloggerXML.find(ns + "description")
            if description is not None and description.text.startswith(
                sensor_sensitivity
            ):
                name = dataloggerXML.attrib["name"]
                publicID = dataloggerXML.attrib["publicID"]
                is_found = True
                break
        elif device["model"] == "CMG-3TD 120s - 50Hz":
            # Model fixes
            model = "CMG3TD"
            modelXML = dataloggerXML.find(ns + "model")

            if modelXML is not None and modelXML.text == model:
                name = dataloggerXML.attrib["name"]
                response = dataloggerXML.attrib["response"]
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
            name = sensorXML.attrib["name"]
            response = sensorXML.attrib["response"]
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
            station = searchStation(station_identifier, "geophysical", url_rest_tos)
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


KNOWN_SUBCOMMANDS = {"owners", "device"}


def _owners_main(argv):
    """Handle `tos owners ...` subcommands."""
    from .owners import KNOWN_OWNERS, OwnersCache
    from .api.tos_client import TOSClient

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

    cache = (
        OwnersCache(args.owners_cache) if args.owners_cache else OwnersCache()
    )
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
        response = writer.create_device(
            args.subtype, required, force=args.force
        )
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
            "optional_attributes": [
                {"code": c, "value": v} for c, v in optional
            ],
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
        master_inventoryXML = parseSeiscompInventoryXML("master_inventory.xml")
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
        import obspy
        from obspy.clients.nrl import NRL
        from obspy.core.inventory import Channel, Inventory, Network, Site, Station

        # master_inventory = parseSeiscompInventoryXML('master_inventory.xml')
        # print(inventory)

        if args.identifiers:
            xml = generateFDSNXML(args.identifiers)
        else:
            xml = generateFDSNXML()

    elif args.identifiers:
        if args.exclude:
            logging.warning("Ignoring option --exclude with station_identifier search")
        stations = []

        for station_identifier in args.identifiers:
            station = searchStation(station_identifier, args.domain, url_rest_tos)
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


def main(argv=None):
    """Entry point — dispatches to `tos <subcommand> ...` or the legacy CLI."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in KNOWN_SUBCOMMANDS:
        subcmd = argv[0]
        rest = argv[1:]
        if subcmd == "owners":
            return _owners_main(rest)
        if subcmd == "device":
            return _device_main(rest)
    return _legacy_main(argv)


if __name__ == "__main__":
    sys.exit(main())
