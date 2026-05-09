import os
import requests

VBOX_BASE_URL = os.getenv("VBOX_BASE_URL")

methods = [
    "GetXmltvPrograms",
    "GetXmltvProgramGuide",
    "GetXmltvEvents",
    "GetXmltv",
    "GetEPG",
    "GetEpgData",
    "GetProgramGuide",
    "GetGuide",
]

for method in methods:
    url = (
        f"{VBOX_BASE_URL}/cgi-bin/HttpControl/HttpControlApp"
        f"?OPTION=1&Method={method}"
    )

    print("\n==============================")
    print(method)
    print(url)

    try:
        r = requests.get(url, verify=False, timeout=20)

        print("STATUS:", r.status_code)
        print(r.text[:1200])

    except Exception as e:
        print("ERROR:", e)
