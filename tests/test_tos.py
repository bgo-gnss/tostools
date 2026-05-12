#!/usr/bin/python3.1
#
# Project: gps_metadata
# Authors: Benedikt Ǵunnar Ófeigsson
#          parts edited TOSTools authored byg Tryggvi Hjörvar
# Date: april 2022
#
#
def main():
    import requests

    url_rest_tos = "https://vi-api.vedur.is:11223/tos/v1"
    response = requests.get(url_rest_tos + "/entity/get_children/parent/4235/")

    print(response.json())


if __name__ == "__main__":
    main()
