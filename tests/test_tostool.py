#!/usr/bin/python3
#
# Project: gps_metadata
# Authors: Benedikt Gunnar Ófeigsson
#          parts edited TOSTools authored by Tryggvi Hjörvar
# Date: april 2022
#
#

import logging
from datetime import datetime

import pytest
import requests

import tostools.gps_metadata_functions as gpsf
import tostools.gps_metadata_qc as gpsqc

# The functions below take a `station_identifier` parameter and were written
# as interactive/CLI-style smoke checks, not as automated unit tests. pytest
# nonetheless collects them because of the `test_` prefix and fails for
# missing fixtures. Skip them in CI; they can still be invoked manually by
# calling them with an explicit station identifier.
_NOT_A_UNIT_TEST = pytest.mark.skip(
    reason="manual smoke test — requires a real TOS station identifier"
)


@_NOT_A_UNIT_TEST
def test_device_attribute_history(station_identifier: str, loglevel=logging.WARNING):
    """
    A funciton to test devie_attribute_history of a station
    """

    module_logger = gpsf.get_logger(name=__name__)
    module_logger.setLevel(loglevel)

    domain = "geophysical"
    station = gpsqc.search_station(
        station_identifier,
        url_rest=gpsqc.URL_REST_TOS,
        domains=domain,
        loglevel=logging.WARNING,
    )[0]

    module_logger.debug(
        "TOS station %s dictionary:\n=================\n%s\n================",
        station_identifier,
        gpsf.json_print(station),
    )

    id_entity = station["id_entity"]
    module_logger.warning(
        "station {} id_entity: {}".format(station_identifier, id_entity)
    )
    station = {}  # clear dictionary for later use
    module_logger.warning(
        'Sending request "{}"'.format(
            gpsqc.URL_REST_TOS + "/history/entity/" + str(id_entity) + "/"
        )
    )
    response = requests.get(
        gpsqc.URL_REST_TOS + "/history/entity/" + str(id_entity) + "/",
        timeout=gpsqc.REQUEST_TIMEOUT,
    )
    devices_history = response.json()
    module_logger.debug(
        "TOS station %s /history/entity/%s:\n=================\n%s\n================\n",
        station_identifier,
        id_entity,
        gpsf.json_print(devices_history),
    )
    module_logger.info("TOS station dictionary keys: {}".format(devices_history.keys()))
    module_logger.info(
        "Station attributes: {}".format(
            [attribute["code"] for attribute in devices_history["attributes"]]
        )
    )

    station["contact"] = gpsqc.get_contacts(id_entity, gpsqc.URL_REST_TOS)
    for attribute in devices_history["attributes"]:
        if attribute["code"] in [
            "marker",
            "name",
            "iers_domes_number",
            "in_network_epos",
        ]:
            station[attribute["code"]] = attribute["value"]
        elif attribute["code"] in ["lon", "lat", "altitude"]:
            station[attribute["code"]] = float(attribute["value"])
    module_logger.debug(gpsf.json_print(station))

    sessions = []
    device_sessions = []
    devices_used = ["gnss_receiver", "antenna", "radome", "monument"]
    for connection in devices_history["children_connections"][:]:
        # NOTE: ignoring sessions that have 0 duration
        if connection["time_to"]:
            if connection["time_from"] >= connection["time_to"]:
                module_logger.info(
                    "Session start is the same as session end: {}, end: {}".format(
                        connection["time_from"], connection["time_to"]
                    )
                )
                continue

        # NOTE: sending a request for device history
        id_entity_child = connection["id_entity_child"]
        request_url = f"{gpsqc.URL_REST_TOS}/history/entity/{str(id_entity_child)}/"
        module_logger.debug("device history request: %s:", request_url)
        try:
            devices_response = requests.get(request_url, timeout=gpsqc.REQUEST_TIMEOUT)
            device = devices_response.json()
        except:
            module_logger.error(
                "failed to establish connection to {}".format(gpsqc.URL_REST_TOS)
            )
            sys.exit(1)

        module_logger.debug("device:\n%s", gpsf.json_print(device))

        # if device["code_entity_subtype"] in devices_used:
        # devices_used = ["gnss_receiver", "antenna", "radome", "monument"]
        if device["code_entity_subtype"] in devices_used:
            module_logger.warning(
                "\n================= \
                \nitem in devices_history[\"children_connections\"]: \
                \n%s\nSending request: %s \
                \nreturned json for device device['code_entity_subtype']: %s\
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

            attribute_history = gpsqc.device_attribute_history(
                device, connection["time_from"], connection["time_to"]
            )
            module_logger.info(
                "attribute_history\n%s", gpsf.json_print(attribute_history)
            )
            module_logger.debug(
                "device['attributes']:\n%s\n" % gpsf.json_print(device["attributes"])
            )
        # device, session_start, session_end


@_NOT_A_UNIT_TEST
def test_gps_metadata(station_identifier: list, loglevel=logging.WARNING):
    """
    Testing gps metada funciton
    """
    module_logger = gpsf.get_logger(name=__name__)
    module_logger.setLevel(loglevel)

    rheader = []
    for sta in station_identifier:  # , "AUST", "VMEY"]:
        station = gpsqc.gps_metadata(sta, gpsqc.URL_REST_TOS, loglevel=loglevel)
        module_logger.info("station: %s", gpsf.json_print(station))
        module_logger.debug(
            "station_history: %s",
            gpsf.json_print(station["device_history"]),
        )
        for item in station["device_history"]:
            module_logger.debug("%s - %s", item["time_from"], item["time_to"])

        # gpsf.print_station_history(station, raw_format=False, loglevel=logging.WARNING)

        station_info_list = []
        station_info_list += gpsf.print_station_info(station)
        for infoline in station_info_list:
            print(infoline)

        start = datetime(2002, 3, 29)
        end = datetime(2022, 4, 23)
        session_nr = 0
        # if station:
        #     session_list = fileList(
        #         station, start=None, end=None, loglevel=loglevel
        #     )
        #     # for session in session_list:
        #     #    print(session)
        #
        #     if session_list:
        #         session = session_list[-1]
        #         module_logger.warning(session['session_number'])
        #         module_logger.warning("%s", gpsf.sessionsList(station))
        #         rheader = read_rinex_header(session['filelist'][-1],loglevel=loglevel)
        #         module_logger.debug("rheader: \n%s\n%s", gpsf.json_print(rheader["rinex file"]), rheader['header'])
        #         # rheader = read_rinex_header("./RHOF0870.02D.Z",loglevel=logging.INFO)
        #         # rheader = read_gzip_file("./RHOF0870.02D.gz", loglevel=logging.WARNING)
        #         # rheader = read_rinex_header(session["filelist"][-1], loglevel=logging.WARNING)
        #
        #         if rheader['header']:
        #             rinex_dict = extract_from_rheader(rheader, loglevel=loglevel)
        #             module_logger.debug("%s\n%s", rinex_dict["rinex file"][1], gpsf.json_print(rinex_dict))
        #
        #             rinex_correction_dict = compare_TOS_to_rinex(
        #                 rinex_dict,
        #                 gpsf.getSession(station, session["session_number"]),
        #                 loglevel=logging.INFO,
        #             )
        #             module_logger.warning("\n%s", gpsf.json_print(rinex_correction_dict))
        #
        #             rheader = fix_rinex_header(rinex_correction_dict, rinex_dict, rheader, loglevel=logging.WARNING)
        #             module_logger.critical("%s\n%s", rheader["rinex file"][1], rheader["header"])
        #             change_file_header(rheader,savedir=Path.cwd())

        # rfile_content = read_zzipped_file("./RHOF0870.02D.Z")
        # pattern = r"^.+(?:\n.+)+END OF HEADER"
        # mstring = re.compile(pattern)
        # result = mstring.search(rfile_content)
        # rfile_new = re.sub(mstring,rheader['header'],rfile_content)
        # print(rfile_new)
        # with open("tmp.txt", 'w') as f:
        #    f.write(rfile_new)
        # if rfile_new:

        #   #matched_line = result.group()
        #   print("{}".format(rfile_new))

        # sessions = gpsf.sessionsList(station)
        # print_attributes_string = "{:<19}  {:<19}  "
        # for session in sessions:
        #     print( print_attributes_string.format(*session) )

    # for line in stationInfo_list:
    #    print(line)


def test_change_rfile_header(level=logging.WARNING):
    """
    Test the function change_rfile_header
    """

    module_logger = gpsf.get_logger(name=__name__)
    module_logger.setLevel(level)

    rheader_correction_ = {
        "rinex file": [
            "/mnt_data/rawgpsdata/2023/jun/RHOF/15s_24hr/rinex",
            "RHOF1610.23D.Z",
        ],
        "OBSERVER / AGENCY": [
            "BGO/HMF             ",
            "Vedurstofa Islands                      ",
            "OBSERVER / AGENCY   ",
        ],
    }

    rheader_correction_dict = {
        "rinex file": [
            "/mnt_data/rawgpsdata/2023/jun/RHOF/15s_24hr/rinex",
            "RHOF1610.23D.Z",
        ],
        "header": "1.0                 COMPACT RINEX FORMAT                    CRINEX VERS   / TYPE\n"
        + "RNX2CRX ver.4.0.7                       11-Jun-23 00:42     CRINEX PROG / DATE\n"
        + "     2.11           OBSERVATION DATA    M (MIXED)      RINEX VERSION / TYPE\n"
        + "teqc  2019Feb25     BGO/HMF             20230611 00:42:14UTCPGM / RUN BY / DATE\n"
        + "Linux 2.6.32-573.12.1.x86_64|x86_64|gcc|Linux 64|=+         COMMENT\n"
        + "   0.000      (antenna height)           COMMENT\n"
        + " +66.46112266 (latitude)                                    COMMENT\n"
        + " -15.94670515 (longitude)                                   COMMENT\n"
        + "0078.586      (elevation)                           COMMENT\n"
        + "BIT 2 OF LLI FLAGS DATA COLLECTED UNDER A/S CONDITION       COMMENT\n"
        + "RHOF (COGO code)                                            COMMENT\n"
        + "RHOF                                                        MARKER NAME\n"
        + "10216M001                                                   MARKER NUMBER\n"
        + "BGO/HMF             Vedurstofa Islands                      OBSERVER / AGENCY   \n"
        + "5038K70713          TRIMBLE NETR9       4.60       REC # / TYPE / VERS\n"
        + "1441045161          TRM57971.00                             ANT # / TYPE\n"
        + "  2456172.0745  -701824.1329  5824747.9643                  APPROX POSITION XYZ\n"
        + "        1.0070        0.0000     0.0000                  ANTENNA: DELTA H/E/N\n"
        + "     1     1                                                WAVELENGTH FACT L1/2\n"
        + "     8    L1    L2    C1    P1    C2    P2    S1    S2      # / TYPES OF OBSERV\n"
        + "    18 LEAP SECONDS\n"
        + " SNR is mapped to RINEX snr flag value [0-9]                COMMENT\n"
        + "  L1 & L2: min(max(int(snr_dBHz/6), 0), 9)                  COMMENT\n"
        + "  2023     6 10     0     0    0.0000000     GPS         TIME OF FIRST OBS\n"
        + "                                                            ",
    }

    module_logger.warning("\n%s", rheader_correction_dict["header"])

    # gpsr.change_rfile_header(
    #     rheader_correction_dict,
    #     dir_structure=dir_structure,
    #     savedir=Path(local_path),
    # )


def main(level=logging.WARNING):
    """
    quering metadata from tos and comparing to relevant rinex files
    """
    # logging settings
    logger = gpsf.get_logger(name=__name__)
    logger.setLevel(level)

    # sta_list = ["AKUR", "ISAF", "ALHV", "BJTV", "BLON", "FIHO", "GJFV", "GUSK", "HEID", "LAVI", "SKHA", "RHOL", "VOFJ", "MYVA"]  # , "HAUD", "HRIC", "HVEL"]
    sta_list = ["LISF"]
    # test_device_attri(bute_history("RHOF", loglevel=logging.WARNING)
    # test_gps_metadata(sta_list, loglevel=logging.WARNING)
    gpsf.site_log("RHOF", loglevel=logging.WARNING)
    # gpsf.domes_info_form("RHOF", loglevel=logging.INFO)

    start = datetime(2023, 6, 9)
    end = datetime(2023, 6, 10)
    # _, rheader_correction_list = gpsr.check_station_rinex_headers(
    #     "RHOF", save_file=False, start=start, end=end, loglevel=logging.WARNING
    # )
    # print(gpsf.json_print(rinex_correction_list))
    # gpsr.change_rinex_files(
    #     rheader_correction_list,
    #     dir_structure="test",
    #     local_file_path=Path.cwd(),
    #     loglevel=logging.WARNING,
    # )
    #
    # test_change_rfile_header()


if __name__ == "__main__":
    main(level=logging.DEBUG)
