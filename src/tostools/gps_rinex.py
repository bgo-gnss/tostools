#!/usr/bin/python3
#
# Project: gps_metadata
# Authors: Benedikt Gunnar Ófeigsson
#          parts edited TOSTools authored by Tryggvi Hjörvar
# Date: april 2022
# major update: aug 2024
#
#

import gzip
import logging
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path, PurePath

import fortranformat as ff
import numpy as np
from gtimes import timefunc as tf
from gtimes.timefunc import datefRinex

# Import legacy modules (transitioning)
from . import gps_metadata_functions as gpsf
from . import gps_metadata_qc as gpsqc
from .rinex.reader import get_rinex_labels
from .rinex.reader import read_rinex_file as modular_read_rinex_file
from .rinex.reader import read_rinex_header as modular_read_rinex_header

# Import new modular RINEX components


def rinex_labels():
    """Legacy wrapper for modular RINEX label function."""
    return get_rinex_labels()


def extract_from_rheader(rheader, loglevel=logging.WARNING):
    """
    Extracts lines containing the keywords in "searchlist" from a Rinex header string and returns as dictonary with keyword as keys
    and content of the lines in as items.
    input:
        rheader: header section of a Rinex file as a single string
    output:
        returns dictionary containing the keywords in searchlist as key's and variables in the relevant lines as items
        and rinex file name and path in key "rinex file"

    """
    module_logger = gpsf.get_logger(name=__name__)

    module_logger.debug(
        "Rinex file: {1} in directory {0}".format(*rheader["rinex file"])
    )
    module_logger.debug("Rinex header:\n{}".format(rheader["header"]))

    fname_date = datefRinex([rheader["rinex file"][1]])[0]
    module_logger.debug("{}: {}".format(rheader["rinex file"][1][0:4], fname_date))

    searchlist, fortran_format_list = rinex_labels()
    module_logger.debug("Strings to search for: {}".format(searchlist))

    rinex_header_dict = {"rinex file": rheader["rinex file"]}
    # for string, fformat in zip(searchlist, fortran_format):

    for string, fortran_format in zip(searchlist, fortran_format_list):
        pattern = r"(^.*(?:{}).*$)".format(string)
        module_logger.debug("Pattern to match: {}".format(pattern))

        module_logger.debug("Length of pattern string: {}".format(len(string)))

        mstring = re.compile(pattern, re.M)
        result = mstring.search(rheader["header"])

        if result:
            matched_line = result.group()
            module_logger.info("Matched line: {}".format(matched_line))

            matched_list = []

            format_reader = ff.FortranRecordReader(fortran_format)
            module_logger.debug("format string: {}".format(format_reader.format))
            matched_list = format_reader.read(matched_line)

            matched_list[:] = [
                string.strip() if type(string) is str else string
                for string in matched_list
            ]

            if matched_list[-1] == "TIME OF FIRST OBS":
                # matched_list[:-3] = list(map(int, matched_list[:-3]))
                time_first_obs = datetime(
                    *matched_list[:-4], round(float(matched_list[-4]))
                )
                matched_list[:-1] = [time_first_obs, matched_list[-3], matched_list[-2]]
                module_logger.debug(
                    "{}: {}".format(matched_list[-1], matched_list[:-1])
                )

            # module_logger.arning("Rinex line: {}".format(match_list_test))
            module_logger.info("Rinex line: {}".format(matched_list))

            rinex_header_dict[matched_list[-1]] = matched_list[:-1]

    module_logger.debug("rinex_header_dict: {}".format(rinex_header_dict))

    return rinex_header_dict


def compare_tos_to_rinex(rinex_dict, session, loglevel=logging.WARNING):
    """
    Reads in dictionary containing variables from the following
    line of a rinex header:
    'MARKER NAME', 'MARKER NUMBER', "OBSERVER / AGENCY", "REC # / TYPE / VERS",
    "ANT # / TYPE", "APPROX POSITION XYZ", "ANTENNA: DELTA H/E/N", "INTERVAL",
    "TIME OF FIRST OBS"
    and compares these varables to the relevant variables in TOS database

    input:
        rinex_dict: dictionary containing variables from a rinex header
        session: dictionary containing variables from TOS database
        loglevel: loglevel

    output:
        returns a dictionary containing those variables from TOS database that
        don't match the rinex header
    """

    # logging
    module_logger = gpsf.get_logger(name=__name__)
    module_logger.setLevel(loglevel)

    module_logger.debug("session.keys: {}".format(session.keys()))
    module_logger.debug("session dictionary: {}".format(session))

    remove_labels = ["TIME OF FIRST OBS"]
    searchlist, _ = rinex_labels()
    [searchlist.remove(label) for label in remove_labels if label in searchlist]
    rinex_header_labels = iter(
        [item for item in rinex_dict.keys() if item not in remove_labels]
    )
    module_logger.debug("rinex_dict: {}".format(rinex_dict))

    searchlist.append("rinex file")
    rinex_correction_dict = {}  # to collect inconsistansies

    for label in rinex_header_labels:
        module_logger.info('Checking "{}"'.format(label))
        searchlist.remove(label)

        if label == "rinex file":
            # This should always match
            # Any mismach here will return a string with the rinex  file name This reprecents reprecents
            # some serious issues which might be due to code bug or serious issue with file structure
            rinex_file_fullpath = Path(*rinex_dict[label])

            module_logger.info("Rinex path: {}".format(rinex_file_fullpath))
            if rinex_file_fullpath.is_file():
                module_logger.info("Rinex file: {} exists".format(rinex_file_fullpath))
                rinex_correction_dict[label] = rinex_dict[label]
            else:
                module_logger.error(
                    "Rinex file {} does not appear to exist. This should not happen".format(
                        rinex_file_fullpath
                    )
                )
                rinex_correction_dict[label] = [
                    rinex_file_fullpath.as_posix(),
                    None,
                ]

                return rinex_correction_dict

            rinex_file = rinex_dict[label][1]
            module_logger.info("Rinex file: {}".format(rinex_file))
            tos_marker = session["marker"].upper()
            TOS_session_period = [
                session["device_history"]["time_from"],
                session["device_history"]["time_to"],
            ]
            module_logger.info("session period: {} - {}".format(*TOS_session_period))

            marker = rinex_file[:4]
            date_from_rinex_fname = datefRinex([rinex_file])[0]

            # date_from_rinex_file =
            try:
                time_of_first_obs = rinex_dict["TIME OF FIRST OBS"][0]
            except KeyError as e:
                module_logger.error('key "{}" not in dictionary "rinex_dict"'.format(e))
                rinex_correction_dict["TIME OF FIRST OBS"] = [None]

                return rinex_correction_dict

            module_logger.debug('{0} "{1}"'.format(label, rinex_file))
            if (
                marker == tos_marker
                and date_from_rinex_fname.date() == time_of_first_obs.date()
            ):
                module_logger.debug(
                    '{0} "{1}" has matching name prefix with database marker "{2}" and the doy-year in {1} matches the date of first observation {3}'.format(
                        label, rinex_file, tos_marker, time_of_first_obs
                    )
                )

                if TOS_session_period[1] is not None:
                    if (
                        TOS_session_period[0]
                        <= date_from_rinex_fname
                        <= TOS_session_period[1]
                    ):
                        module_logger.debug(
                            'Time of file "{0}" falls within period "{2} <= {1} < {3}'.format(
                                rinex_file,
                                date_from_rinex_fname,
                                *TOS_session_period,
                            )
                        )
                    else:
                        module_logger.error(
                            'Time of file "{0}": {1}. DOES NOT fall within period "{2} - {3}". This should not happen'.format(
                                rinex_file,
                                date_from_rinex_fname,
                                *TOS_session_period,
                            )
                        )

                        rinex_correction_dict["session period"] = TOS_session_period

                        return rinex_correction_dict

                else:
                    TOS_session_period[1] = tf.currDatetime(days=-1)
                    if (
                        TOS_session_period[0]
                        <= date_from_rinex_fname
                        <= TOS_session_period[1]
                    ):
                        module_logger.debug(
                            'Time of file "{0}" falls within period "{2} <= {1} < {3}'.format(
                                rinex_file,
                                date_from_rinex_fname,
                                *TOS_session_period,
                            )
                        )
                    else:
                        module_logger.error(
                            'Time of file "{0}": {1}. DOES NOT fall within period "{2} - {3}". This should not happen'.format(
                                rinex_file,
                                date_from_rinex_fname,
                                *TOS_session_period,
                            )
                        )

                        rinex_correction_dict["session period"] = TOS_session_period

                        return rinex_correction_dict

            else:
                if marker != tos_marker:
                    module_logger.error(
                        'Mismach with {0} "{1}" and matching name prefix in database marker "{2}" '.format(
                            label, rinex_file, tos_marker
                        )
                    )
                    rinex_correction_dict["TOS marker"] = [tos_marker]

                if date_from_rinex_fname.date() != time_of_first_obs.date():
                    module_logger.error(
                        "Mismach with the doy-year in {0} and the date of first observation {1}".format(
                            rinex_file, time_of_first_obs
                        )
                    )
                    rinex_correction_dict["TIME OF FIRST OBS"] = [time_of_first_obs]

                module_logger.debug(
                    "Returning dictionary {}".format(rinex_correction_dict)
                )
                return rinex_correction_dict

        elif label == "MARKER NAME":
            rinex_marker = rinex_dict[label][0]
            module_logger.info('"Marker name" in Rinex file: {}'.format(rinex_marker))
            tos_marker = session["marker"].upper()
            if rinex_marker == tos_marker:
                module_logger.debug(
                    'Label "{0}" is "{1}" in file "{2}", matches database marker "{3}"'.format(
                        label, rinex_marker, rinex_dict["rinex file"], tos_marker
                    )
                )
            else:
                module_logger.info(
                    'Label "{0}" is "{1}" in file "{2}", DOES NOT match database value "{3}"'.format(
                        label, rinex_marker, rinex_dict["rinex file"], tos_marker
                    )
                )
                rinex_correction_dict[label] = [tos_marker]

        elif label == "MARKER NUMBER":
            rinex_number = rinex_dict[label][0]
            module_logger.info('"Marker number" in Rinex file: {}'.format(rinex_number))
            if "iers_domes_number" in session.keys():
                TOS_number = session["iers_domes_number"]
            else:
                TOS_number = session["marker"].upper()

            if rinex_number == TOS_number:
                module_logger.debug(
                    'Label "{0}" is "{1}" in file "{2}", matches database marker "{3}"'.format(
                        label, rinex_number, rinex_dict["rinex file"], TOS_number
                    )
                )
            else:
                module_logger.info(
                    'Label "{0}" is "{1}" in file "{2}", DOES NOT match database value "{3}"'.format(
                        label, rinex_number, rinex_dict["rinex file"], TOS_number
                    )
                )
                rinex_correction_dict[label] = [TOS_number, ""]

        elif label == "OBSERVER / AGENCY":
            rinex_observer_agency = rinex_dict[label]
            module_logger.info(
                '"OBSERVER / AGENCY" in Rinex file:\t{}\t{}'.format(
                    *rinex_observer_agency
                )
            )
            contact_correction_list = [None, None]

            TOS_operator = session["contact"]["operator"]["name"]
            module_logger.info('"operator "agency":\t{}'.format(TOS_operator))

            # HACK: This part needs to be moved to tos
            if TOS_operator == "Veðurstofa Íslands":
                TOS_observer_agency = ["BGO/HMF", "Vedurstofa Islands"]
            if TOS_operator == "Landmælingar Íslands":
                TOS_observer_agency = ["LMI", "Landmaelingar Islands"]

            if rinex_observer_agency[0] != TOS_observer_agency[0]:
                module_logger.info(
                    'Label OBSERVER in "{0}" is "{1}" in file "{2}", DOES NOT match database value "{3}"'.format(
                        label,
                        rinex_observer_agency[0],
                        rinex_dict["rinex file"],
                        TOS_observer_agency[0],
                    )
                )
                contact_correction_list[0] = TOS_observer_agency[0]
                rinex_correction_dict[label] = contact_correction_list

            if rinex_observer_agency[1] != TOS_observer_agency[1]:
                module_logger.info(
                    'Label OBSERVER in "{0}" is "{1}" in file "{2}", DOES NOT match database value "{3}"'.format(
                        label,
                        rinex_observer_agency[1],
                        rinex_dict["rinex file"],
                        TOS_observer_agency[1],
                    )
                )
                contact_correction_list[1] = TOS_observer_agency[1]
                rinex_correction_dict[label] = contact_correction_list

        elif label == "REC # / TYPE / VERS":
            rinex_receiver = rinex_dict[label]
            receiver_correction_list = [None, None, None]
            module_logger.info(
                '"REC # / TYPE / VERS" in Rinex file: {} / {} / {} '.format(
                    *rinex_receiver
                )
            )
            TOS_receiver_attributes = session["device_history"]["gnss_receiver"]
            module_logger.debug("{}".format(TOS_receiver_attributes))
            TOS_receiver_serial = TOS_receiver_attributes["serial_number"]
            TOS_receiver_model = TOS_receiver_attributes["model"]
            TOS_receiver_sversion = TOS_receiver_attributes["software_version"]

            if rinex_receiver[0] != TOS_receiver_serial:
                module_logger.info(
                    'Label REC # in "{0}" is "{1}" in file "{2}", DOES NOT match database value "{3}"'.format(
                        label,
                        rinex_receiver[0],
                        rinex_dict["rinex file"],
                        TOS_receiver_serial,
                    )
                )
                receiver_correction_list[0] = TOS_receiver_serial
                rinex_correction_dict[label] = receiver_correction_list
            else:
                module_logger.info(
                    'Label REC # in "{0}" is "{1}" in file "{2}", and matches database value "{3}"'.format(
                        label,
                        rinex_receiver[0],
                        rinex_dict["rinex file"],
                        TOS_receiver_serial,
                    )
                )

            if rinex_receiver[1] != TOS_receiver_model:
                module_logger.info(
                    'Label TYPE  in "{0}" is "{1}" in file "{2}", DOES NOT match database value "{3}"'.format(
                        label,
                        rinex_receiver[1],
                        rinex_dict["rinex file"],
                        TOS_receiver_model,
                    )
                )
                receiver_correction_list[1] = TOS_receiver_model
                rinex_correction_dict[label] = receiver_correction_list
            else:
                module_logger.info(
                    'Label TYPE in "{0}" is "{1}" in file "{2}", and matches database value "{3}"'.format(
                        label,
                        rinex_receiver[1],
                        rinex_dict["rinex file"],
                        TOS_receiver_model,
                    )
                )

            if rinex_receiver[2] != TOS_receiver_sversion:
                module_logger.info(
                    'Label VERS  in "{0}" is "{1}" in file "{2}", DOES NOT match database value "{3}"'.format(
                        label,
                        rinex_receiver[2],
                        rinex_dict["rinex file"],
                        TOS_receiver_sversion,
                    )
                )
                receiver_correction_list[2] = TOS_receiver_sversion
                rinex_correction_dict[label] = receiver_correction_list
            else:
                module_logger.info(
                    'Label VERS in "{0}" is "{1}" in file "{2}", and matches database value "{3}"'.format(
                        label,
                        rinex_receiver[2],
                        rinex_dict["rinex file"],
                        TOS_receiver_sversion,
                    )
                )

        elif label == "ANT # / TYPE":
            rinex_antenna = rinex_dict[label]
            antenna_correction_list = [
                None,
                None,
                "",
            ]  # extra empty string for plank space in rinex file
            module_logger.info(
                '"ANT # / TYPE" in Rinex file: {} / {} '.format(*rinex_antenna)
            )
            TOS_antenna_attributes = session["device_history"]["antenna"]
            module_logger.debug("{}".format(TOS_antenna_attributes))
            TOS_antenna_serial = TOS_antenna_attributes["serial_number"]
            TOS_antenna_model = TOS_antenna_attributes["model"]

            if "radome" in session["device_history"]:
                TOS_radome_model = session["device_history"]["radome"]["model"]
                module_logger.info("radome: {}".format(TOS_radome_model))
                TOS_antenna_model = "{0:<16.16}{1:>4.4}".format(
                    TOS_antenna_model, TOS_radome_model
                )
                module_logger.info(
                    'Antenna type with radome "{}"'.format(TOS_antenna_model)
                )

            if rinex_antenna[0] != TOS_antenna_serial:
                module_logger.info(
                    'Label ANT # in "{0}" is "{1}" in file "{2}", DOES NOT match database value "{3}"'.format(
                        label,
                        rinex_antenna[0],
                        rinex_dict["rinex file"],
                        TOS_antenna_serial,
                    )
                )
                antenna_correction_list[0] = TOS_antenna_serial
                rinex_correction_dict[label] = antenna_correction_list
            else:
                module_logger.debug(
                    'Label ANT # in "{0}" is "{1}" in file "{2}", and matches database value "{3}"'.format(
                        label,
                        rinex_antenna[0],
                        rinex_dict["rinex file"],
                        TOS_antenna_serial,
                    )
                )

            if rinex_antenna[1] != TOS_antenna_model:
                module_logger.info(
                    'Label TYPE  in "{0}" is "{1}" in file "{2}", DOES NOT match database value "{3}"'.format(
                        label,
                        rinex_antenna[1],
                        rinex_dict["rinex file"],
                        TOS_antenna_model,
                    )
                )
                antenna_correction_list[1] = TOS_antenna_model
                rinex_correction_dict[label] = antenna_correction_list
            else:
                module_logger.info(
                    'Label TYPE in "{0}" is "{1}" in file "{2}", and matches database value "{3}"'.format(
                        label,
                        rinex_antenna[1],
                        rinex_dict["rinex file"],
                        TOS_antenna_model,
                    )
                )

        elif label == "ANTENNA: DELTA H/E/N":
            rinex_antenna_offset_HEN = rinex_dict[label]
            antenna_offset_correction_list = [
                None,
                None,
                None,
                "",
            ]  # extra empty string for blank space in rinex file
            module_logger.info(
                '"ANTENNA: DELTA H/E/N" in Rinex file:\t{}\t{}\t{}'.format(
                    *rinex_antenna_offset_HEN
                )
            )

            TOS_antenna_attributes = session["device_history"]["antenna"]
            module_logger.debug("{}".format(TOS_antenna_attributes))
            TOS_antenna_height = TOS_antenna_attributes["antenna_height"]
            module_logger.debug("Antenna height: {}".format(TOS_antenna_height))

            TOS_monument_attributes = session["device_history"]["monument"]
            module_logger.info("{}".format(TOS_monument_attributes))
            TOS_monument_height = TOS_monument_attributes["monument_height"]
            module_logger.debug("Monument height: {}".format(TOS_monument_height))

            TOS_antenna_offset_HEN = [
                TOS_antenna_height + TOS_monument_height,
                0.0,
                0.0,
            ]
            module_logger.debug(
                "Antenna height + Monument height: {}".format(TOS_antenna_offset_HEN[0])
            )

            if abs(rinex_antenna_offset_HEN[0] - TOS_antenna_offset_HEN[0]) > 0.0001:
                module_logger.info(
                    'Label H  in "{0}" is "{1}" in file "{2}", DOES NOT match database value "{3}"'.format(
                        label,
                        rinex_antenna_offset_HEN[0],
                        rinex_dict["rinex file"],
                        TOS_antenna_offset_HEN[0],
                    )
                )
                antenna_offset_correction_list[0] = TOS_antenna_offset_HEN[0]
                rinex_correction_dict[label] = antenna_offset_correction_list
            else:
                module_logger.debug(
                    'Label H  in "{0}" is "{1}" in file "{2}", matches database value "{3}"'.format(
                        label,
                        rinex_antenna_offset_HEN[0],
                        rinex_dict["rinex file"],
                        TOS_antenna_offset_HEN[0],
                    )
                )

            if abs(rinex_antenna_offset_HEN[1] - TOS_antenna_offset_HEN[1]) > 0.0001:
                module_logger.info(
                    'Label E  in "{0}" is "{1}" in file "{2}", DOES NOT match database value "{3}"'.format(
                        label,
                        rinex_antenna_offset_HEN[1],
                        rinex_dict["rinex file"],
                        TOS_antenna_offset_HEN[1],
                    )
                )
                antenna_offset_correction_list[1] = TOS_antenna_offset_HEN[1]
                rinex_correction_dict[label] = antenna_offset_correction_list
            else:
                module_logger.debug(
                    'Label E in "{0}" is "{1}" in file "{2}", matches database value "{3}"'.format(
                        label,
                        rinex_antenna_offset_HEN[1],
                        rinex_dict["rinex file"],
                        TOS_antenna_offset_HEN[1],
                    )
                )

            if abs(rinex_antenna_offset_HEN[2] - TOS_antenna_offset_HEN[2]) > 0.0001:
                module_logger.info(
                    'Label N  in "{0}" is "{1}" in file "{2}", DOES NOT match database value "{3}"'.format(
                        label,
                        rinex_antenna_offset_HEN[2],
                        rinex_dict["rinex file"],
                        TOS_antenna_offset_HEN[2],
                    )
                )
                antenna_offset_correction_list[2] = TOS_antenna_offset_HEN[2]
                rinex_correction_dict[label] = antenna_offset_correction_list
            else:
                module_logger.debug(
                    'Label N in "{0}" is "{1}" in file "{2}", matches database value "{3}"'.format(
                        label,
                        rinex_antenna_offset_HEN[2],
                        rinex_dict["rinex file"],
                        TOS_antenna_offset_HEN[2],
                    )
                )

        elif label == "APPROX POSITION XYZ":
            rinex_xyz_coord = rinex_dict[label]
            module_logger.info("rinex_xyz_coord: {}".format(rinex_xyz_coord))
            module_logger.info(
                '"XYZ Position" in Rinex file:\t{}\t{}\t{}'.format(*rinex_xyz_coord)
            )

            TOS_coord_latlonheig = [
                session["lat"],
                session["lon"],
                session["altitude"],
            ]
            module_logger.info(
                '"lat, lon, height coordinates" in TOS database:\t{}\t{}\t{}'.format(
                    *TOS_coord_latlonheig
                )
            )
            TOS_coord_ECEF = list(gpsqc.wgs84toitrf08.transform(*TOS_coord_latlonheig))
            module_logger.info(
                "XYZ coordinates in TOS database:\t{0:.4f}\t{1:.4f}\t{2:.4f}".format(
                    *TOS_coord_ECEF
                )
            )

            Rinex_TOS_coord_difference = np.array(TOS_coord_ECEF) - np.array(
                rinex_xyz_coord[:-1]
            )
            module_logger.info(
                "difference in ECEF coordinates between Rinex file and TOS database in meters:\t{0:>.4f}\t{1:>.4f}\t{2:>.4f}".format(
                    *Rinex_TOS_coord_difference
                )
            )
            distance = np.sqrt(
                Rinex_TOS_coord_difference.dot(Rinex_TOS_coord_difference)
            )
            module_logger.info(
                "Distance between coordinates:\t{0:>.4f} m".format(distance)
            )

            tolerance = 60.0
            if distance > tolerance:
                module_logger.error(
                    "Distance between TOS database and Rinex files coordinates is more then {0:.4f} m < {1:.4f} m".format(
                        tolerance, distance
                    )
                )
                rinex_correction_dict[label] = [*TOS_coord_ECEF, ""]
            else:
                module_logger.info(
                    "Distance between TOS database and Rinex files coordinates is less then {0:.4f} m > {1:.4f} m".format(
                        tolerance, distance
                    )
                )

    else:
        module_logger.info(
            "OUT OF LABELS following labels where not handled {}".format(searchlist)
        )

        if "MARKER NUMBER" in searchlist:
            if "iers_domes_number" in session.keys():
                TOS_number = session["iers_domes_number"]
            else:
                TOS_number = session["marker"].upper()

            module_logger.info(
                '"MARKER NUMBER" is not in Rinex file adding {}'.format(TOS_number)
            )
            rinex_correction_dict["MARKER NUMBER"] = [TOS_number, ""]

    module_logger.debug("rinex_correction_dict: {}".format(rinex_correction_dict))

    return rinex_correction_dict


def fix_rinex_header(
    rinex_correction_dict, rinex_dict, rheader, loglevel=logging.WARNING
):
    """ """
    # logging settings
    module_logger = gpsf.get_logger(name=__name__)
    module_logger.setLevel(loglevel)

    module_logger.info(
        'keys in "rinex_correction_dict" {}'.format(rinex_correction_dict.keys())
    )
    module_logger.info(
        'keys in "rinex_correction_dict" {}'.format(rinex_correction_dict)
    )
    module_logger.info('keys in "rinex_dict" {}'.format(rinex_dict.keys()))
    module_logger.debug('keys in "rheader" {}'.format(rheader.keys()))

    if (
        rinex_correction_dict["rinex file"] != rinex_dict["rinex file"]
        or rheader["rinex file"] != rinex_dict["rinex file"]
    ):
        module_logger.debug("input dictionaries are not derived from the same files")
        module_logger.debug(
            'input rinex file is {} for "rinex_correction_dict"'.format(
                Path(*rinex_correction_dict["rinex file"])
            )
        )
        module_logger.debug(
            'input rinex file is {} for "rinex_dict"'.format(
                Path(*rinex_dict["rinex file"])
            )
        )
        module_logger.info(
            'input rinex file is {} for "rheader"'.format(Path(*rheader["rinex file"]))
        )
    else:
        module_logger.debug(
            "input dictionaries are derived from the same file {}".format(
                Path(*rinex_dict["rinex file"])
            )
        )

    rinex_header_line, fortran_format = rinex_labels()
    label_gen = (
        label for label in rinex_correction_dict.keys() if label not in ["rinex file"]
    )
    for label in label_gen:
        rinex_fix_line = fix_rinex_line(
            label, rinex_correction_dict, rinex_dict, loglevel=logging.WARNING
        )

        module_logger.info("Line to replace: {}".format(rinex_fix_line))
        pattern = r"(^.*(?:{}).*$)".format(label)
        module_logger.debug("Pattern to match: {}".format(pattern))
        module_logger.debug("Length of pattern string: {}".format(len(label)))

        mstring = re.compile(pattern, re.M)
        result = mstring.search(rheader["header"])
        if result:
            rheader["header"] = re.sub(mstring, rinex_fix_line, rheader["header"])
        else:
            label_list, _ = rinex_labels()
            prev_label = label_list[label_list.index(label) - 1]
            pattern = r"({}.*$)".format(prev_label)
            mstring = re.compile(pattern, re.M)
            rheader["header"] = re.sub(
                mstring, r"\1\n" + rinex_fix_line, rheader["header"]
            )

    return rheader


def fix_rinex_line(label, rinex_correction_dict, rinex_dict, loglevel=logging.WARNING):
    """ """

    # logging settings
    module_logger = gpsf.get_logger(name=__name__)
    module_logger.setLevel(loglevel)

    rinex_header_line, fortran_format = rinex_labels()

    module_logger.debug(
        'Correct variables "{}" {}'.format(label, rinex_correction_dict[label])
    )
    line_structure = fortran_format[rinex_header_line.index(label)]
    fwriter = ff.FortranRecordWriter(line_structure)
    module_logger.debug("Format string: {}".format(fwriter.format))

    space_width = [int(item) for item in re.findall(r"[0-9]+", fwriter.format)]
    module_logger.debug("with list: {}".format(space_width))

    rinex_correction_dict[label].append(label)
    if label in rinex_dict.keys():
        module_logger.debug('to be corrected "{}" {}'.format(label, rinex_dict[label]))

    for item, width in zip(rinex_correction_dict[label], space_width):
        index = rinex_correction_dict[label].index(item)

        if not item:
            if label in rinex_dict.keys():
                fill_item = rinex_dict[label][index]
                rinex_correction_dict[label][index] = fill_item
                if not isinstance(fill_item, float) and not isinstance(fill_item, int):
                    right_spaces = " " * (width - len(rinex_dict[label][index]))
                    rinex_correction_dict[label][index] = (
                        rinex_dict[label][index] + right_spaces
                    )

        else:
            if not isinstance(item, float) and not isinstance(item, int):
                right_spaces = " " * (width - len(item))
                rinex_correction_dict[label][index] += right_spaces

    module_logger.info(
        'Correct variables "{}" {}'.format(label, rinex_correction_dict[label])
    )

    return fwriter.write(rinex_correction_dict[label])


def change_rinex_files(
    rheader_correction_list, local_file_path, dir_structure="", loglevel=logging.WARNING
):
    """
    loop through list of rinex files to change the headers
    by applying change_rfile_header

    This function will create new files locally and does not overite the
    current rinex files
    """

    module_logger = gpsf.get_logger(name=__name__)
    module_logger.setLevel(loglevel)

    for rheader_correction_dict in rheader_correction_list:
        module_logger.debug(
            "New fixed rinex header\n%s\n%s\n%s\n%s",
            gpsf.json_print(rheader_correction_dict["rinex file"]),
            "-" * 50,
            rheader_correction_dict["header"],
            "-" * 50 + "\n",
        )

        # HACK:  need to handle file path parts in a config file
        local_path = PurePath(local_file_path)
        path_last_part = PurePath(rheader_correction_dict["rinex file"][0]).parts
        path_last_part = PurePath(*path_last_part[-5:])
        local_path = local_path / path_last_part

        try:
            os.makedirs(str(local_path), exist_ok=True)
            module_logger.warning(
                "Saving a rinex file %s to: %s",
                rheader_correction_dict["rinex file"][1],
                local_path,
            )
            change_rfile_header(
                rheader_correction_dict,
                dir_structure=dir_structure,
                savedir=Path(local_path),
            )
        except PermissionError as e:
            module_logger.error(e)
            sys.exit(1)


def change_rfile_header(
    rheader, dir_structure="", savedir=Path.cwd(), loglevel=logging.WARNING
):
    """
    change the contend of a rinex file header to rheader["header"]
    and save the rinex file to the savedir direcry savedir
    """
    # logging settings
    module_logger = gpsf.get_logger(name=__name__)
    module_logger.setLevel(loglevel)

    rfile = Path(*rheader["rinex file"])

    try:
        rfile_content = read_rinex_file(rfile, loglevel=loglevel)
    except RuntimeError as e:
        rfile_content = None
        module_logger.error(e)
        traceback.print_exc()

    # if rfile_content:
    #     # rheader = re.search(r"^.+(?:\n.+)+END OF HEADER", rfile_content).group()
    #     pattern = re.compile(r"(?m)^.*?(?=END OF HEADER)", flags=re.DOTALL | re.MULTILINE)
    #     match = pattern.search(rfile_content)
    #
    # if match is None:
    #     module_logger.warning(
    #         "Search for END OF HEADER did not return any result from the header of %s",
    #             rfile
    #         )
    #     return sys.exit(1)
    #
    # rheader = match.group()
    # rfile_new_content = re.sub(rheader, rfile_content)

    # NOTE: working replacement pattern need to improve
    rfile_new_content = re.sub(
        r"^.+(?:\n.+)+END OF HEADER", rheader["header"], rfile_content
    )

    if isinstance(savedir, str):
        savedir = Path(savedir)

    # if dir_structure:
    #     module_logger.warning("dir_structure %s", dir_structure)
    # else:
    #     module_logger.warning("dir_structure is empty %s, %s", dir_structure, rfile.name)

    rfile = Path(savedir, rfile.name)
    module_logger.warning("savedir %s", savedir)
    module_logger.warning("rfile %s", rfile)

    outfile = rfile.with_suffix(".gz")
    module_logger.warning("writing to file %s", outfile)

    with gzip.open(outfile, "wb") as f:
        f.write(bytes(rfile_new_content, "utf-8"))


def check_station_rinex_headers(
    station_identifier: str,
    save_file: bool = True,
    start=None,
    end=None,
    loglevel=logging.WARNING,
):
    """
    Go through a list of station rinex files and report on inconsistancies
    """

    module_logger = gpsf.get_logger(name=__name__)
    module_logger.setLevel(loglevel)

    station = gpsqc.gps_metadata(
        station_identifier, gpsqc.URL_REST_TOS, loglevel=loglevel
    )

    if not station:
        module_logger.warning(
            "dictionary for station %s is empty returning", station_identifier
        )
        return [], []

    module_logger.debug(
        "station_history: \n%s",
        # gpsf.json_print(station["device_history"]),
        gpsf.json_print(station["device_history"]),
    )

    if not Path(gpsqc.REMOTE_FILE_PATH).exists():
        module_logger.error(
            "\n    "
            + "=" * 30
            + "    Path: %s does NOT exist "
            + "    or is not a directory, Exiting ..."
            + "\n    "
            + "=" * 30
            + "",
            gpsqc.REMOTE_FILE_PATH,
        )
        sys.exit(1)
    else:
        if not any(Path(gpsqc.REMOTE_FILE_PATH).iterdir()):
            module_logger.error(
                "\n    "
                + "=" * 30
                + "    Path: %s, No files found:\n"
                + "    You might have forgot to mount a network file system\n"
                + "    Exiting ..."
                + "\n    "
                + "=" * 30,
                gpsqc.REMOTE_FILE_PATH,
            )
            sys.exit(1)

    session_list = gpsf.file_list(
        station, pdir=gpsqc.REMOTE_FILE_PATH, start=start, end=end, loglevel=loglevel
    )
    for session in session_list:
        module_logger.debug("session: \n%s", gpsf.json_print(session))

    rheader = []
    tos_session_metadata = {}
    session_nr = tmp_nr = ""
    rinex_correction_list = []
    rheader_correction_list = []
    if session_list:
        for session in session_list:
            module_logger.debug("session: \n%s", gpsf.json_print(session))
            session_nr = session["session_number"]
            if session_nr != tmp_nr:
                module_logger.info("------ session_number: %s -------", session_nr)
                tos_session_metadata = gpsf.getSession(station, session_nr)
                module_logger.debug(
                    "tos_session_metadata: \n%s", gpsf.json_print(tos_session_metadata)
                )

                tmp_nr = session_nr

            for file in session["filelist"]:
                rheader = read_rinex_header(file, loglevel=loglevel)
                if rheader["header"] != "":
                    module_logger.debug(
                        "rheader: \n%s\n%s",
                        gpsf.json_print(rheader["rinex file"]),
                        rheader["header"],
                    )
                    rinex_dict = extract_from_rheader(rheader, loglevel=loglevel)
                    module_logger.debug(
                        "%s\n%s",
                        rinex_dict["rinex file"][1],
                        gpsf.json_print(rinex_dict),
                    )
                    rinex_correction_dict = compare_tos_to_rinex(
                        rinex_dict,
                        tos_session_metadata,
                        loglevel=loglevel,
                    )
                    rheader_correction_dict = fix_rinex_header(
                        rinex_correction_dict, rinex_dict, rheader, loglevel=loglevel
                    )
                    module_logger.debug(
                        "New fixed rinex header\n%s\n%s\n%s\n%s",
                        gpsf.json_print(rheader_correction_dict["rinex file"]),
                        "-" * 50,
                        rheader_correction_dict["header"],
                        "-" * 50 + "\n",
                    )
                    # module_logger.warning(
                    #     "rinex_correction_dict: %s\n%s",
                    #     gpsf.json_print(rinex_correction_dict['rinex file']),
                    #     rinex_correction_dict['header'],
                    # )

                    if save_file is True:
                        local_path = PurePath(LOCAL_FILE_PATH)
                        path_last_part = PurePath(
                            rheader_correction_dict["rinex file"][0]
                        ).parts
                        path_last_part = PurePath(*path_last_part[-5:])
                        local_path = local_path / path_last_part

                        try:
                            os.makedirs(str(local_path), exist_ok=True)
                            module_logger.warning(
                                "Saving a rinex file %s to: %s",
                                rheader_correction_dict["rinex file"][1],
                                local_path,
                            )
                            change_rfile_header(
                                rheader_correction_dict, savedir=Path(local_path)
                            )
                        except PermissionError as e:
                            module_logger.error(e)
                            sys.exit(1)

                    rheader_correction_list.append(rheader_correction_dict)
                    rinex_correction_list.append(rinex_correction_dict)

                else:
                    module_logger.warning(
                        "No header found for \n%s\n%s",
                        gpsf.json_print(rheader["rinex file"]),
                        rheader["header"],
                    )

    return rinex_correction_list, rheader_correction_list


def read_rinex_file(rfile, loglevel=logging.WARNING):
    """Legacy wrapper for modular RINEX file reader."""
    content_bytes = modular_read_rinex_file(rfile, loglevel)
    if content_bytes:
        return content_bytes.decode("utf-8")
    return None


def read_rinex_header(rfile, loglevel=logging.WARNING):
    """Legacy wrapper for modular RINEX header reader."""
    return modular_read_rinex_header(rfile, loglevel)


def main(level=logging.INFO):
    """
    No main function
    """

    module_logger = gpsf.get_logger()
    module_logger.setLevel(level)

    module_logger.info("Functions to work with rinex data")


if __name__ == "__main__":
    main(level=logging.DEBUG)
