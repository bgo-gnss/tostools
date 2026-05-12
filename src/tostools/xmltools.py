#!/usr/bin/python3
#
# Project: TOSTools
# Authors: Tryggvi Hjörvar
# Date: Nov 2020
#
# Module for XML handling
#
# Usage:
#  Command-line: xmltools.py
#  Help:  xmltools.py -h
#
# Examples:
#  xmltools.py station_sc.xml station_sc-edit.xml
#
#

import argparse
import difflib
import logging
import re
import sys
import xml.etree.ElementTree as ET

import colorama
from colorama import Back as bg
from colorama import Fore as fg
from tabulate import tabulate

# def compareXMLtoInventory(xml, inventory):
#    station_identifier='ada'
#    print(xml.select(network='VI', station=station_identifier))


def format_diff(seqm):
    """Unify operations between two compared strings seqm is a difflib.SequenceMatcher instance whose a & b are strings
    https://stackoverflow.com/questions/774316/python-difflib-highlighting-differences-inline
    """

    output = []
    for opcode, a0, a1, b0, b1 in seqm.get_opcodes():
        if opcode == "equal":
            output.append(seqm.a[a0:a1])
        elif opcode == "insert":
            # output.append("<ins>" + seqm.b[b0:b1] + "</ins>")
            output.append(fg.RED + seqm.b[b0:b1] + fg.RESET)
        elif opcode == "delete":
            output.append(fg.RED + seqm.a[a0:a1] + fg.RESET)
            # output.append("<del>" + seqm.a[a0:a1] + "</del>")
        elif opcode == "replace":
            # raise NotImplementedError ("what to do with 'replace' opcode?")
            # output.append("<repl>" + seqm.a[a0:a1] + "</repl>")
            output.append(fg.RED + seqm.b[b0:b1] + fg.RESET)
        else:
            raise RuntimeError("unexpected opcode")
    return "".join(output)


# >>> sm= difflib.SequenceMatcher(None, "lorem ipsum dolor sit amet", "lorem foo ipsum dolor amet")
# >>> show_diff(sm)


def validateElement(element, child):
    is_valid = True

    # Element
    regex = re.compile("^{http.*}(.*)")
    match = regex.search(element.tag)
    element_code = match.group(1)
    axis = element.attrib["code"]

    if element_code == "stream":
        match = regex.search(child.tag)
        if match:
            code = match.group(1)

            if code == "azimuth":
                if axis == "HHE" and child.text != "90":
                    is_valid = False
                elif axis in ["HHN", "HHZ"] and child.text != "0":
                    is_valid = False
            if code == "dip":
                if axis in ["HHE", "HHN"] and child.text != "0":
                    is_valid = False
                elif axis == "HHZ" and child.text != "-90":
                    is_valid = False
    return is_valid


def compareChildren(element, ns, c_element, c_ns, ignore):
    output = []
    is_identical = True
    regex = re.compile("^{http.*}(.*)")

    # First level children
    for child in element:
        # print(child.tag,child.attrib,child.text)
        if child.tag != ns + ignore:
            is_found = False
            # Parse element name
            match = regex.search(child.tag)
            if match:
                code = match.group(1)
                # Find element in compare element
                # print('c_element',c_element.attrib['code'])
                for c_child in c_element:
                    c_match = re.search(code, c_child.tag)
                    if c_match:
                        is_found = True

                        # Validate selected children
                        is_valid = validateElement(element, child)
                        cell = f"<{code}>" + child.text + f"</{code}>"

                        c_is_valid = validateElement(c_element, c_child)
                        c_cell = f"<{code}>" + c_child.text + f"</{code}>"

                        if not (is_valid and c_is_valid):
                            if not is_valid and c_is_valid:
                                output.append(
                                    {
                                        "generated": bg.RED + cell + bg.RESET,
                                        "comparefile": c_cell,
                                    }
                                )
                            elif is_valid and not c_is_valid:
                                output.append(
                                    {
                                        "generated": cell,
                                        "comparefile": bg.RED + c_cell + bg.RESET,
                                    }
                                )
                            else:
                                output.append(
                                    {
                                        "generated": bg.RED + cell,
                                        "comparefile": c_cell + bg.RESET,
                                    }
                                )

                        else:
                            # Compare
                            if child.text == c_child.text:
                                cell = f"<{code}>" + child.text + f"</{code}>"
                                c_cell = f"<{code}>" + c_child.text + f"</{code}>"
                                output.append(
                                    {
                                        "generated": fg.GREEN + cell,
                                        "comparefile": c_cell + fg.RESET,
                                    }
                                )
                            else:
                                is_identical = False
                                seqm = difflib.SequenceMatcher(
                                    None, child.text, c_child.text
                                )
                                cell = f"<{code}>" + child.text + f"</{code}>"
                                c_cell = f"<{code}>" + format_diff(seqm) + f"</{code}>"
                                output.append(
                                    {"generated": cell, "comparefile": c_cell}
                                )
                            break
                if not is_found:
                    is_identical = False
                    cell = f"<{code}>" + child.text + f"</{code}>"
                    c_cell = ""
                    output.append(
                        {"generated": fg.RED + cell, "comparefile": c_cell + fg.RESET}
                    )

    # Check for extra children in right compare (missing in left)
    for c_child in c_element:
        if c_child.tag != c_ns + ignore:
            is_found = False
            # Parse element name
            match = regex.search(c_child.tag)
            if match:
                code = match.group(1)
                # Find element in
                for child in element:
                    match = re.search(code, child.tag)
                    if match:
                        is_found = True
                        break
                if not is_found:
                    is_identical = False
                    cell = ""
                    c_cell = f"<{code}>" + c_child.text + f"</{code}>"
                    output.append(
                        {"generated": fg.RED + cell, "comparefile": c_cell + fg.RESET}
                    )

    return {"output": output, "is_identical": is_identical}


def compareAttrib(element, c_element):
    output = []
    is_identical = True
    # Parse element name
    regex = re.compile("^{http.*}(.*)")
    match = regex.search(element.tag)
    if match:
        code = match.group(1)
    else:
        raise ValueError("Could not parse element")

    if element.attrib == c_element.attrib:
        cell = (
            "<"
            + code
            + " "
            + " ".join([x[0] + '="' + x[1] + '"' for x in element.attrib.items()])
            + ">"
        )
        c_cell = (
            "<"
            + code
            + " "
            + " ".join([x[0] + '="' + x[1] + '"' for x in c_element.attrib.items()])
            + ">"
        )
        output.append({"generated": fg.GREEN + cell, "comparefile": c_cell + fg.RESET})
    else:
        attributes = {}
        attributes_missing = {}
        c_attributes = {}
        c_cell = ""
        for attribute, value in element.attrib.items():
            # TODO: extra attributes
            # Ignore publicID
            # if attribute == 'publicID':
            #    if attribute in c_element.attrib:
            #        c_attributes[attribute] = c_element.attrib[attribute]

            # Check if attribute exists in compare element
            if attribute in c_element.attrib:
                seqm = difflib.SequenceMatcher(
                    None, str(value), str(c_element.attrib[attribute])
                )
                attributes[attribute] = value
                c_attributes[attribute] = format_diff(seqm)
            else:
                attributes_missing[attribute] = value

        # cell   = '<'+code+' '+' '.join([x[0]+'="'+x[1]+'"' for x in attributes.items()]) + '>'
        cell = (
            "<"
            + code
            + " "
            + " ".join([x[0] + '="' + x[1] + '"' for x in attributes.items()])
            + " "
            + fg.RED
            + " ".join([x[0] + '="' + x[1] + '"' for x in attributes_missing.items()])
            + fg.RESET
            + ">"
        )
        c_cell = (
            "<"
            + code
            + " "
            + " ".join([x[0] + '="' + x[1] + '"' for x in c_attributes.items()])
            + ">"
        )
        if element == c_element:
            output.append(
                {
                    "generated": fg.GREEN + cell.replace(" ", "\r\n  "),
                    "comparefile": c_cell.replace(" ", "\r\n  ") + fg.RESET,
                }
            )
        else:
            is_identical = False
            output.append(
                {
                    "generated": cell.replace(" ", "\r\n  "),
                    "comparefile": c_cell.replace(" ", "\r\n  "),
                }
            )

    return {"output": output, "is_identical": is_identical}


def compareStreams(sensorLocation, ns, c_sensorLocation, c_ns):
    output = []
    for code in ["HHE", "HHN", "HHZ"]:
        is_identical = True

        stream = sensorLocation.find(ns + "stream[@code='" + code + "']")
        c_stream = c_sensorLocation.find(c_ns + "stream[@code='" + code + "']")

        # Validate if stream is missing
        if stream is None or c_stream is None:
            if stream is None:
                cell = bg.RED + '<stream code="' + code + '">' + bg.RESET
            else:
                cell = (
                    '<stream code="'
                    + code
                    + '" '
                    + " ".join(
                        [x[0] + '="' + x[1] + '"' for x in stream.attrib.items()]
                    )
                    + ">"
                )
            if c_stream is None:
                c_cell = bg.RED + '<stream code="' + code + '">' + bg.RESET
            else:
                c_cell = (
                    '<stream code="'
                    + code
                    + '" '
                    + " ".join(
                        [x[0] + '="' + x[1] + '"' for x in c_stream.attrib.items()]
                    )
                    + ">"
                )
            output.append({"generated": cell, "comparefile": c_cell})
        else:
            # Compare attributes and children
            output += compareAttrib(stream, c_stream)["output"]
            output += compareChildren(stream, ns, c_stream, c_ns, ignore="")["output"]

    return {"output": output, "is_identical": is_identical}


def compareSC3(sc3ml, comparefile, tablefmt="simple"):

    colorama.init()

    # tree = ET.ElementTree(seiscomp)
    # print(tree.getroot().tag)

    # seiscomp = ET.parse(sc3ml).getroot()
    seiscomp = ET.fromstring(sc3ml)

    regex = re.compile(r"({http.*(\d.\d{1,2})})seiscomp")
    match = regex.search(seiscomp.tag)
    if match:
        ns = match.group(1)
        match.group(2)
    else:
        raise ValueError("Invalid SC3ML file")
        sys.exit(1)

    inventory = seiscomp.find(ns + "Inventory")
    network = inventory.find(ns + "network")

    # Parse comparefile
    c_seiscomp = ET.parse(comparefile).getroot()
    # match = re.search(seiscomp.tag, r'{http.*(\d.\d{1,2})}seiscomp')
    regex = re.compile(r"({http.*(\d.\d{1,2})})seiscomp")
    match = regex.search(c_seiscomp.tag)
    if match:
        c_ns = match.group(1)
        match.group(2)
    else:
        raise ValueError("Invalid SC3ML comparefile")
        sys.exit(1)

    c_inventory = c_seiscomp.find(c_ns + "Inventory")
    c_network = c_inventory.find(c_ns + "network")
    #    c_sensors = c_inventory.findall(ns+'sensor')
    #    c_dataloggers = c_inventory.findall(ns+'datalogger')
    #    c_responsePAZ = c_inventory.findall(ns+'responsePAZ')
    #    c_responseFIR = c_inventory.findall(ns+'responseFIR')
    #
    #    #for child in dataloggers:
    #    #    print(child.tag, child.attrib)

    # Compare
    output = []
    # output.append({'generated': fg.RED+'hello1'+fg.RESET, 'comparefile': 'hello2'})

    # XML header
    # print('seiscomp',seiscomp.tag)
    # print('seiscomp',seiscomp.attrib)
    if seiscomp.tag == c_seiscomp.tag:
        c_attributes = {}
        c_element = ""
        for attribute, value in seiscomp.attrib.items():
            # Check if attribute exists in compare seiscomp element
            if attribute in c_seiscomp.attrib:
                seqm = difflib.SequenceMatcher(
                    None, str(value), str(c_seiscomp.attrib[attribute])
                )
                c_attributes[attribute] = format_diff(seqm)
        element = (
            "<seiscomp xmlns="
            + ns
            + " ".join([x[0] + '="' + x[1] + '"' for x in seiscomp.attrib.items()])
            + ">"
        )
        c_element = (
            "<seiscomp xmlns="
            + c_ns
            + " ".join([x[0] + '="' + x[1] + '"' for x in c_attributes.items()])
            + ">"
        )
        # output.append({'generated': fg.GREEN+seiscomp.tag, 'comparefile': c_seiscomp.tag+fg.RESET})
        output.append(
            {"generated": fg.GREEN + element, "comparefile": c_element + fg.RESET}
        )
    else:
        seqm = difflib.SequenceMatcher(None, seiscomp.tag, c_seiscomp.tag)
        # print('seqm',format_diff(seqm))
        # output.append({'generated': seiscomp.tag, 'comparefile': fg.RED+c_seiscomp.tag+fg.RESET})
        c_attributes = {}
        c_element = ""
        for attribute, value in seiscomp.attrib.items():
            # Check if attribute exists in compare seiscomp element
            if attribute in c_seiscomp.attrib:
                seqm = difflib.SequenceMatcher(
                    None, str(value), str(c_seiscomp.attrib[attribute])
                )
                c_attributes[attribute] = format_diff(seqm)
        element = (
            '<seiscomp xmlns="'
            + ns[1:-1]
            + '" '
            + " ".join([x[0] + '="' + x[1] + '"' for x in seiscomp.attrib.items()])
            + ">"
        )
        c_element = (
            '<seiscomp xmlns="'
            + c_ns[1:-1]
            + '" '
            + " ".join([x[0] + '="' + x[1] + '"' for x in c_attributes.items()])
            + ">"
        )

        # output.append({'generated': seiscomp.tag, 'comparefile': format_diff(seqm) })
        output.append(
            {
                "generated": element.replace(" ", "\r\n  "),
                "comparefile": c_element.replace(" ", "\r\n  "),
            }
        )

    # Network
    # Compare attributes and children
    output += compareAttrib(network, c_network)["output"]
    output += compareChildren(network, ns, c_network, c_ns, ignore="station")["output"]

    # Stations
    stations = network.findall(ns + "station")
    c_network.findall(c_ns + "station")
    for station in stations:
        station_identifier = station.attrib["code"]
        # print(station.attrib['code'])
        # Find station in compare
        c_station = c_network.find(c_ns + "station[@code='" + station_identifier + "']")
        if c_station is None:
            # Station missing from compare
            cell = (
                "<station "
                + " ".join([x[0] + '="' + x[1] + '"' for x in station.attrib.items()])
                + ">"
            )
            c_cell = ""
            output.append(
                {"generated": fg.RED + cell, "comparefile": c_cell + fg.RESET}
            )
        else:
            # Check attributes and children
            output += compareAttrib(station, c_station)["output"]
            output += compareChildren(
                station, ns, c_station, c_ns, ignore="sensorLocation"
            )["output"]

            # SensorLocation
            sensorLocations = station.findall(ns + "sensorLocation")
            c_sensorLocations = c_station.findall(c_ns + "sensorLocation")

            for sensorLocation in sensorLocations:
                # Find station in compare
                # sensorLocation is treated as identical if its children (excluding streams) are identical
                # The sensorLocation publicID is ignored in this comparison
                is_found = False
                for c_sensorLocation in c_sensorLocations:
                    result = compareChildren(
                        sensorLocation, ns, c_sensorLocation, c_ns, ignore="stream"
                    )
                    if result["is_identical"]:
                        is_found = True
                        output += compareAttrib(sensorLocation, c_sensorLocation)[
                            "output"
                        ]
                        output += result["output"]

                        # Streams
                        # <stream code="HHE" datalogger="Datalogger#20150925141352.76545.33138" sensor="NRL/Guralp/CMG3ESP.60.2000">
                        output = compareStreams(
                            sensorLocation, ns, c_sensorLocation, c_ns
                        )["output"]

                        # Add datalogger from master
                        # Confirm datalogger exists
                        # Add sensor from master
                        # Confirm sensor exists

                if not is_found:
                    # sensorLocaiton missing from compare (or not matching on children)
                    cell = (
                        "<sensorLocation "
                        + " ".join(
                            [
                                x[0] + '="' + x[1] + '"'
                                for x in sensorLocation.attrib.items()
                            ]
                        )
                        + ">"
                    )
                    c_cell = ""
                    output.append(
                        {"generated": fg.RED + cell, "comparefile": c_cell + fg.RESET}
                    )
                    # Add children
                    output += compareChildren(
                        sensorLocation, ns, [], c_ns, ignore="stream"
                    )["output"]

            # Check for extra sensorLocations in compare (or not matching)
            if len(sensorLocations) != len(c_sensorLocations) or not is_found:
                for c_sensorLocation in c_sensorLocations:
                    is_found = False
                    for sensorLocation in sensorLocations:
                        result = compareChildren(
                            sensorLocation, ns, c_sensorLocation, c_ns, ignore="stream"
                        )
                        if result["is_identical"]:
                            is_found = True

                    if not is_found:
                        # c_sensorLocaiton missing from left (or not matching on children)
                        cell = ""
                        c_cell = (
                            "<sensorLocation "
                            + " ".join(
                                [
                                    x[0] + '="' + x[1] + '"'
                                    for x in c_sensorLocation.attrib.items()
                                ]
                            )
                            + ">"
                        )
                        output.append(
                            {
                                "generated": fg.RED + cell,
                                "comparefile": c_cell + fg.RESET,
                            }
                        )
                        # Add children
                        output += compareChildren(
                            [], ns, c_sensorLocation, c_ns, ignore="stream"
                        )["output"]

    # Display
    headers = {"generated": "stations.sc3.xml", "comparefile": "comparefile"}
    print(
        tabulate(
            [
                {"generated": d["generated"], "comparefile": d["comparefile"]}
                for d in output
            ],
            headers,
            tablefmt=tablefmt,
        )
    )


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        add_help=True, description="TOS tools - Version 0.3"
    )
    parser.add_argument("file1", help="SC3ML (left) file to compare")
    parser.add_argument("file2", help="SC3ML (right) file to compare")
    # parser.add_argument('-t', '--tablefmt', default='simple', help='Tableformat for output; simple (default), plain, github, grid...See https://pypi.org/project/tabulate/ for full list')
    # parser.add_argument(      '--compareto', help='Compare SC3ML XML (left) file structure to the specified (right) file')
    # parser.add_argument(      '--schema_version', help='XML schema version. Supported versions: 0.9, 0.10, 0.11')
    # parser.add_argument('-v', '--validate', help='SC3ML (right) file to compare')

    args = parser.parse_args()

    # Check args
    if not len(sys.argv) == 3:
        logging.error("Incorrect arguments passed, see --help")
        sys.exit(2)

    # from obspy.clients.nrl import NRL

    # if args.schema_version:
    #    if args.schema_version not in ['0.9','0.10','0.11']:
    #        logging.critical(f'Unsupported XML schema version {args.schema_version}')
    #        sys.exit()

    # if args.compareto is not None:
    #    #from obspy import read_inventory
    #    #inventory = read_inventory(args.compareto, format='SC3ML')
    #    #compareXMLtoInventory(xml,inventory)

    # sc3ml = ET.parse(args.file1).getroot()
    sc3ml = ET.parse(args.file1).getroot()
    # sc3ml = ET.tostring(sc3ml, encoding='UTF-8', method='xml', xml_declaration=True).decode()
    sc3ml = ET.tostring(sc3ml, encoding="UTF-8", method="xml").decode()
    # xml = ET.tostring(seiscomp, encoding='UTF-8', method='xml', xml_declaration=True).decode()

    with open(args.file2, encoding="utf-8") as comparefile:
        compareSC3(sc3ml, comparefile)
