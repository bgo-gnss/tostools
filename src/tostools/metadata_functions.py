#!/usr/bin/python3.1
#
# Project: gps_metadata_functions
# Authors: Benedikt Ǵunnar Ófeigsson
#          parts edited TOSTools authored byg Tryggvi Hjörvar
# Date: april 2022
#
#

import logging


def get_logger(name=__name__, level=logging.WARNING):
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
    logger.setLevel(level)

    if logger.hasHandlers():
        logger.handlers.clear()
    # Add handler to logger
    logger.addHandler(logHandler)

    # Stop propagating the log messages to root logger
    logger.propagate = False

    return logger


def printStationHistory(station, raw_format=False, loglevel=logging.WARNING):
    """ """

    from tabulate import tabulate

    # logging settings
    module_logger = get_logger(name=__name__, level=loglevel)

    station_headers = [key for key in station.keys() if key != "device_history"]
    station_attributes = tuple(
        value
        for key, value in station.items()
        if key not in ["contact", "device_history"]
    )
    module_logger.warning("Station: {}".format(station))
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
        + "| {0}                                       | {1}                                     | {2}                         | {3}".format(
            *device_list
        )
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
                device_headers = [key for key in item[device].keys()]
                device_attributes = [value for key, value in item[device].items()]

                try:
                    device_attributes[device_attributes.index(None)] = "None"
                except:
                    pass

                if device == "gnss_receiver":
                    string = "| " + "{:14.14} " * (len(device_headers) - 1) + " {:5.5} "
                elif device == "antenna":
                    string = "| " + "{:14.14} {:15.15} {:6.6} {:5.5} "
                elif device == "monument":
                    string = "| " + "{:6.6} {:14.14} {:6.6}     "
                else:
                    string = "| " + "{} " * (len(device_headers)) + "  "

                print_header_string += string
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
        for devices, headers, values in zip(
            device_types_list, headers_list, devices_list
        ):
            print("+" * 240)
            print(tabulate([devices], tablefmt="plain"))
            print(tabulate([headers]))
            print(tabulate([values], tablefmt="plain"))
        print("+" * 240)
    else:
        print(print_header_string.format(*headers_list[0]))
        print("-" * 240)
        for string, value in zip(attributes_string_list, devices_list):
            # print(value)
            print(string.format(*value))


def printStationInfo(station):
    """ """

    # print(header)

    stationInfo_list = []
    for item in station["device_history"]:
        try:
            time_from = item["time_from"].strftime("%Y %j %H %M %S")
        except:
            print(
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
        if item["antenna"]["model"] is None:
            antenna_type = "---------------"
        else:
            antenna_type = item["antenna"]["model"]

        # receiver SN
        if item["antenna"]["serial_number"] is None:
            antenna_SN = "---------------"
        else:
            antenna_SN = item["antenna"]["serial_number"]

        # Antenna height and offsets
        antenna_height = (
            item["antenna"]["antenna_height"] + item["monument"]["monument_height"]
        )
        antenna_N = 0.0
        antenna_E = 0.0

        # receiver type
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

        # radome
        try:
            dome = item["radome"]["model"]
        except:
            dome = "NONE"

        # header='*SITE  Station Name      Session Start      Session Stop       Ant Ht   HtCod  Ant N    Ant E    Receiver Type         Vers                  SwVer  Receiver SN           Antenna Type     Dome   Antenna SN'
        sessionLine = " {0:4.4}  {1:17.17} {2:17.17}  {3:17.17}  {4: 1.4f}  {5:5.5}  {6: 1.4f}  {7: 1.4f}  {8:20.20}  {9:20.20}  {10:>5.5}  {11:20.20}  {12:15.15}  {13:5.5}  {14:20.20}".format(
            station["marker"].upper(),
            station["name"][:18],
            time_from,
            time_to,
            antenna_height,
            item["antenna"]["antenna_reference_point"],
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


def getSession(station, session_nr, loglevel=logging.WARNING):
    """ """

    # logging
    module_logger = get_logger(name=__name__, level=loglevel)

    session = {key: value for key, value in station.items() if key != "device_history"}
    module_logger.info("Station information: {}".format(session))
    session["device_history"] = station["device_history"][session_nr]
    module_logger.info("session dictionary: {}".format(session))

    return session
