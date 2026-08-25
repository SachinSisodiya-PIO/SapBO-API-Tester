import os
import sys
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET

import requests


BO_SERVER = os.environ.get("BO_SERVER", "c-bi-v03")
BO_PORT = int(os.environ.get("BO_PORT", "8080"))
AUTH_TYPE = os.environ.get("BO_AUTH", "secEnterprise")

BASE_URL = f"http://{BO_SERVER}:{BO_PORT}/biprws/raylight/v1"
LOGON_URL = f"http://{BO_SERVER}:{BO_PORT}/biprws/logon/long"
DOCUMENTS_URL = f"{BASE_URL}/documents"
SEARCH_URL = f"{BASE_URL}/searches"


def local_name(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def get_required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is not set.")
    return value


def get_target_date() -> datetime.date:
    yesterday = datetime.now().date() - timedelta(days=1)
    date_input = input(
        f"\nEnter target check date (YYYY-MM-DD), or press Enter for yesterday [{yesterday}]: "
    ).strip()

    if not date_input:
        return yesterday

    try:
        return datetime.strptime(date_input, "%Y-%m-%d").date()
    except ValueError:
        print("\nInvalid date format. Please enter the date in YYYY-MM-DD format.")
        sys.exit(1)


def get_report_name() -> str:
    report_name = input("\nEnter report name to search [Montana Dairy Item Audit]: ").strip()
    return report_name or "Montana Dairy Item Audit"


def build_login_body(username: str, password: str) -> str:
    attrs = ET.Element("attrs", {"xmlns": "http://www.sap.com/rws/bip"})
    ET.SubElement(attrs, "attr", {"name": "userName"}).text = username
    ET.SubElement(attrs, "attr", {"name": "password"}).text = password
    ET.SubElement(attrs, "attr", {"name": "auth"}).text = AUTH_TYPE
    return ET.tostring(attrs, encoding="unicode")


def extract_logon_token(xml_text: str) -> str:
    root = ET.fromstring(xml_text)

    for elem in root.iter():
        if local_name(elem.tag) == "attr" and elem.attrib.get("name") == "logonToken":
            if elem.text:
                return elem.text

    raise RuntimeError(f"Unable to extract SAP BO logon token.\nResponse:\n{xml_text}")


def test_get(test_name: str, url: str, headers: dict) -> requests.Response | None:
    print("\n" + "=" * 110)
    print(f"TEST: {test_name}")
    print(f"URL : {url}")
    print("-" * 110)

    try:
        response = requests.get(url, headers=headers, timeout=30)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        return None

    print(f"HTTP Status: {response.status_code}")
    print(f"Content-Type: {response.headers.get('Content-Type', '')}")

    if response.text:
        print("\nResponse:")
        print(response.text[:10000])
        if len(response.text) > 10000:
            print("\n... response truncated ...")
    else:
        print("\nEmpty response.")

    return response


def normalize_to_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def main() -> None:
    target_check_date = get_target_date()
    report_name = get_report_name()

    print(f"\nTarget Check Date: {target_check_date}")
    print(f"\nReport to search: {report_name}")

    username = get_required_env("BO_USERNAME")
    password = get_required_env("BO_PASSWORD")

    print("\nConnecting to SAP BusinessObjects...")

    login_body = build_login_body(username, password)
    response = requests.post(
        LOGON_URL,
        headers={"Content-Type": "application/xml", "Accept": "application/xml"},
        data=login_body,
        timeout=30,
    )
    response.raise_for_status()

    token = extract_logon_token(response.text)
    print("Successfully logged in to SAP BusinessObjects.")

    headers = {"X-SAP-LogonToken": token, "Accept": "application/json"}

    print("\n" + "=" * 110)
    print("STEP 1 - SEARCHING CMS FOR REPORT")
    print("=" * 110)

    search_body = {"search": {"document": {}, "spreadsheet": {}}}

    try:
        search_response = requests.post(
            SEARCH_URL,
            headers={**headers, "Content-Type": "application/json"},
            json=search_body,
            timeout=60,
        )
    except Exception as exc:
        print(f"\nSearch failed: {exc}")
        sys.exit(1)

    print(f"Search HTTP Status: {search_response.status_code}")
    print("\nSearch response:")
    print(search_response.text[:10000])

    if search_response.status_code != 200:
        print("\nCMS search did not return HTTP 200.")
        sys.exit(1)

    try:
        search_data = search_response.json()
    except Exception:
        print("\nSearch response is not JSON.")
        print(search_response.text)
        sys.exit(1)

    search_results = search_data.get("search", {})
    documents_found = normalize_to_list(search_results.get("document", []))
    spreadsheets_found = normalize_to_list(search_results.get("spreadsheet", []))

    all_results = []
    for item in documents_found:
        enriched = dict(item)
        enriched["_search_type"] = "document"
        all_results.append(enriched)

    for item in spreadsheets_found:
        enriched = dict(item)
        enriched["_search_type"] = "spreadsheet"
        all_results.append(enriched)

    exact_matches = [
        item
        for item in all_results
        if str(item.get("name", "")).lower() == report_name.lower()
    ]

    matching_results = exact_matches or [
        item
        for item in all_results
        if report_name.lower() in str(item.get("name", "")).lower()
    ]

    print("\n" + "=" * 110)
    print(f"SEARCH RESULTS FOR: {report_name}")
    print("=" * 110)

    if not matching_results:
        print("\nNo matching report was found.")
        print("\nAvailable search results returned by CMS:")
        for item in all_results[:100]:
            print(
                f"  {item.get('name', 'Unknown')} | ID={item.get('id', '')} | Type={item.get('_search_type', '')}"
            )
        sys.exit(1)

    print(f"\nFound {len(matching_results)} matching object(s).\n")
    for index, item in enumerate(matching_results, start=1):
        print(f"{index}. Name={item.get('name', '')}")
        print(f"   ID={item.get('id', '')}")
        print(f"   CUID={item.get('cuid', '')}")
        print(f"   Folder ID={item.get('folderId', '')}")
        print(f"   Type={item.get('_search_type', '')}\n")

    selected = matching_results[0]
    document_id = selected.get("id")
    if not document_id:
        print("\nCould not obtain Document ID.")
        sys.exit(1)

    print("=" * 110)
    print("SELECTED DOCUMENT")
    print(f"Name        : {selected.get('name', '')}")
    print(f"Document ID : {document_id}")
    print(f"CUID        : {selected.get('cuid', '')}")
    print(f"Folder ID   : {selected.get('folderId', '')}")
    print("=" * 110)

    document_url = f"{DOCUMENTS_URL}/{document_id}"
    test_get("DOCUMENT DETAILS", document_url, headers)

    schedules_url = f"{DOCUMENTS_URL}/{document_id}/schedules"
    schedules_response = test_get("DOCUMENT SCHEDULES", schedules_url, headers)

    schedules = []
    if schedules_response is not None and schedules_response.status_code == 200:
        try:
            schedules_data = schedules_response.json()
            schedules = normalize_to_list(
                schedules_data.get("schedules", {}).get("schedule", [])
            )
        except Exception as exc:
            print(f"\nUnable to parse schedules: {exc}")

    print("\n" + "=" * 110)
    print(f"SCHEDULES FOUND: {len(schedules)}")
    print("=" * 110)

    for index, schedule in enumerate(schedules, start=1):
        schedule_id = schedule.get("id")
        schedule_name = schedule.get("name", "Unknown")

        print("\n" + "#" * 110)
        print(f"SCHEDULE {index}/{len(schedules)}")
        print(f"Schedule Name: {schedule_name}")
        print(f"Schedule ID  : {schedule_id}")
        print("#" * 110)

        if not schedule_id:
            print("No schedule ID.")
            continue

        schedule_url = f"{DOCUMENTS_URL}/{document_id}/schedules/{schedule_id}"
        test_get("SCHEDULE DETAILS", schedule_url, headers)

    print("\n" + "=" * 110)
    print("EXECUTION / HISTORY DISCOVERY")
    print(f"Target Date: {target_check_date}")
    print("=" * 110)

    execution_urls = [
        ("DOCUMENT INSTANCES", f"{document_url}/instances"),
        ("DOCUMENT HISTORY", f"{document_url}/history"),
        ("DOCUMENT EXECUTIONS", f"{document_url}/executions"),
        ("DOCUMENT SCHEDULE HISTORY", f"{document_url}/scheduleHistory"),
        ("DOCUMENT SCHEDULE INSTANCES", f"{document_url}/scheduleInstances"),
        ("DOCUMENT SUBSCRIPTIONS", f"{document_url}/subscriptions"),
        ("DOCUMENT SUBSCRIPTION", f"{document_url}/subscription"),
    ]

    for test_name, url in execution_urls:
        test_get(test_name, url, headers)

    date_string = target_check_date.strftime("%Y-%m-%d")
    date_filtered_urls = [
        ("INSTANCES WITH START DATE", f"{document_url}/instances?startDate={date_string}"),
        ("INSTANCES WITH DATE", f"{document_url}/instances?date={date_string}"),
        ("EXECUTIONS WITH START DATE", f"{document_url}/executions?startDate={date_string}"),
        ("HISTORY WITH DATE", f"{document_url}/history?date={date_string}"),
    ]

    for test_name, url in date_filtered_urls:
        test_get(test_name, url, headers)

    print("\n" + "=" * 110)
    print("TESTING COMPLETED")
    print("=" * 110)
    print(f"Target Date : {target_check_date}")
    print(f"Report      : {report_name}")
    print(f"Document ID : {document_id}")
    print(f"Schedules   : {len(schedules)}")
    print("=" * 110)


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as exc:
        print(f"\nHTTP error: {exc}")
        if exc.response is not None:
            print(exc.response.text[:10000])
        raise SystemExit(1)
    except Exception as exc:
        print(f"\nFatal error: {exc}")
        raise SystemExit(1)
