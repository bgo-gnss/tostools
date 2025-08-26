#!python
import logging


def main():
    """
    quering metadata from tos and comparing to relevant rinex files
    """

    import argparse
    from argparse_logging import add_log_level_argument

    import gps_metadata_qc as gpsqc
    import gps_metadata_functions as gpsf

    # logging settings
    # logger = get_logger(name=__name__, level=logging.DEBUG)

    url_rest_tos = "vi-api.vedur.is/tos/v1"
    stationInfo_list = []

    # print(module_logger.getEffectiveLevel())

    parser = argparse.ArgumentParser(
        description="QC tool to manage GPS medata through TOS",
        epilog="For any issues regarding this program or the GPS"
        + "system contact, Benni,  email: bgo@vedur.is,"
        + "or Hildur email: hildur@vedur.is",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("Stations", nargs="+", help="List of stations")
    # parser.add_argument('--url', type=str ,nargs='?', default = url_rest_tos, const = url_rest_tos,
    #         help='URL to a TOS REST service')
    # parser.add_argument('-r', type=str ,nargs='?', default = url_rest_tos, const = url_rest_tos,
    #         help='URL to a TOS REST service')
    add_log_level_argument(parser)

    # server options
    server_options = parser.add_argument_group(title="Server options")
    server_options.add_argument(
        "--protocol", type=str, default="https", help="Transfer protocol"
    )
    server_options.add_argument(
        "-s", "--server", type=str, default="vi-api.vedur.is", help="Host:"
    )
    server_options.add_argument("-p", "--port", type=int, default=443, help="Port:")
    server_options.add_argument(
        "-r", "--rest", type=str, default="/tos/v1", help="Top levels REST path"
    )
    server_options.add_argument(
        "-t", "--timeout", type=int, default=4, help="Connection timeout:"
    )

    # making subcommands
    subparsers = parser.add_subparsers(
        title="Subcommands", description="valid subcommands", dest="subcommand"
    )

    # For TOS print options
    print_options = subparsers.add_parser(
        "PrintTOS", help="Choose beteween printing options of TOS metadata"
    )
    print_options.add_argument(
        "-f",
        "--format",
        choices=["table", "gamit"],
        default="table",
        help="Print raw format as",
    )
    print_options.add_argument("--raw", action="store_true", help="Print raw format")

    args, sub_commands = parser.parse_known_args()

    # args = parser.parse_args()
    stations = args.Stations
    # Defining default behaviour
    pformat, raw = (
        (args.format, args.raw) if args.subcommand == "PrintTOS" else ("table", False)
    )

    # Constructing the URL:
    url = "{}://{}:{}{}".format(args.protocol, args.server, args.port, args.rest)
    log_level = args.log_level

    for sta in stations:
        station_info = gpsqc.gps_metadata(sta, url, loglevel=log_level.value)
        if pformat == "table":
            gpsf.print_station_history(
                station_info, raw_format=raw, loglevel=log_level.value
            )
        elif pformat == "gamit":
            stationInfo_list += gpsf.print_station_history(station_info)

    stationInfo_list.sort()
    for infoline in stationInfo_list:
        print(infoline)


if __name__ == "__main__":
    main()
