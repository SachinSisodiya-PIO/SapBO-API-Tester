import os
import sys
from datetime import date, datetime, timedelta
import xml.etree.ElementTree as ET
from typing import Any

import requests


BO_SERVER = os.environ.get("BO_SERVER", "c-bi-v03")
BO_PORT = int(os.environ.get("BO_PORT", "8080"))
AUTH_TYPE = os.environ.get("BO_AUTH", "secEnterprise")

BASE_URL = f"http://{BO_SERVER}:{BO_PORT}/biprws/raylight/v1"
LOGON_URL = f"http://{BO_SERVER}:{BO_PORT}/biprws/logon/long"
DOCUMENTS_URL = f"{BASE_URL}/documents"
SEARCH_URL = f"{BASE_URL}/searches"

DEFAULT_FAILED_STATUS_FILTERS = ["failed", "error", "exception", "aborted"]
SUCCESS_STATUS_HINTS = ["success", "succeeded", "complete", "completed"]


def local_name(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def get_required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is not set.")
    return value


def normalize_to_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


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

    raise RuntimeError(f"Unable to extract SAP BO logon token.\\nResponse:\\n{xml_text}")


def get_target_date() -> date:
    yesterday = datetime.now().date() - timedelta(days=1)
    date_input = input(
        f"\nEnter target check date (YYYY-MM-DD), or press Enter for yesterday [{yesterday}]: "
    ).strip()

    if not date_input:
        return yesterday

    try:
        return datetime.strptime(date_input, "%Y-%m-%d").date()
    except ValueError:
        print("\nInvalid date format. Please enter YYYY-MM-DD.")
        sys.exit(1)


def get_report_name() -> str:
    report_name = input("\nEnter report name to search [Montana Dairy Item Audit]: ").strip()
    return report_name or "Montana Dairy Item Audit"


def get_status_filters() -> list[str]:
    prompt = ",".join(DEFAULT_FAILED_STATUS_FILTERS)
    value = input(
        "\nEnter status filter keywords (comma-separated), or press Enter for failed-only "
        f"[{prompt}]: "
    ).strip()

    if not value:
        return DEFAULT_FAILED_STATUS_FILTERS

    keywords = [item.strip().lower() for item in value.split(",") if item.strip()]
    return keywords or DEFAULT_FAILED_STATUS_FILTERS


def first_string(record: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = record.get(key)
        if value is None:
            continue
        as_text = str(value).strip()
        if as_text:
            return as_text
    return ""


def parse_date_text(value: str) -> date | None:
    text = value.strip()
    if not text:
        return None

    # Many SAP BO payloads use ISO strings. Extracting YYYY-MM-DD first handles
    # both `2026-01-20T06:01:00Z` and `2026-01-20 06:01:00` safely.
    if len(text) >= 10:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            pass

    # Fallback patterns for non-ISO payloads observed in some BO deployments.
    for fmt in ("%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    return None


def execution_matches_date(execution: dict[str, Any], target_date: date) -> bool:
    # Different API versions expose start timestamps under different fields.
    # We scan common keys and compare only the calendar date.
    date_fields = [
        "startTime",
        "startDate",
        "beginTime",
        "runDate",
        "scheduledTime",
        "creationTime",
    ]

    parsed_any_date = False
    for field in date_fields:
        value = execution.get(field)
        if value is None:
            continue

        parsed = parse_date_text(str(value))
        if parsed is not None:
            parsed_any_date = True
        if parsed == target_date:
            return True

    # If the payload has no parseable date fields, trust server-side filtering
    # from the endpoint query parameters instead of dropping the record.
    return not parsed_any_date


def extract_schedule_records(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    schedules_node = payload.get("schedules")
    if isinstance(schedules_node, dict):
        return normalize_to_list(schedules_node.get("schedule", []))

    return normalize_to_list(payload.get("schedule", []))


def extract_executions(payload: Any) -> list[dict[str, Any]]:
    # SAP BO responses vary by endpoint and version. This parser checks several
    # common container shapes and gracefully falls back to scanning dict values.
    if isinstance(payload, list):
        return normalize_to_list(payload)

    if not isinstance(payload, dict):
        return []

    candidate_paths = [
        ("scheduleInstances", "scheduleInstance"),
        ("instances", "instance"),
        ("executions", "execution"),
        ("history", "entry"),
        ("history", "instance"),
        ("entries", None),
        ("instances", None),
        ("executions", None),
        ("scheduleInstance", None),
        ("instance", None),
    ]

    for parent_key, child_key in candidate_paths:
        parent = payload.get(parent_key)
        if child_key is None:
            parsed = normalize_to_list(parent)
        elif isinstance(parent, dict):
            parsed = normalize_to_list(parent.get(child_key))
        else:
            parsed = []

        if parsed:
            return parsed

    # As a final fallback, collect dict-like items from values.
    extracted: list[dict[str, Any]] = []
    for value in payload.values():
        extracted.extend(normalize_to_list(value))

    return extracted


def filter_executions_by_status(
    executions: list[dict[str, Any]], status_filters: list[str]
) -> list[dict[str, Any]]:
    filters = [item.lower() for item in status_filters if item]
    if not filters:
        return executions

    matched: list[dict[str, Any]] = []
    for execution in executions:
        status = first_string(execution, ["status", "state", "runStatus", "statusText"]).lower()
        if any(filter_value in status for filter_value in filters):
            matched.append(execution)

    return matched


def identify_failed_executions(executions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return filter_executions_by_status(executions, DEFAULT_FAILED_STATUS_FILTERS)


def extract_error_details(execution: dict[str, Any]) -> list[str]:
    # BO error metadata can be flat strings or nested objects. We collect both.
    messages: list[str] = []
    direct_keys = [
        "error",
        "errorMessage",
        "errorText",
        "failureReason",
        "statusDescription",
        "message",
    ]

    for key in direct_keys:
        value = execution.get(key)
        if isinstance(value, str) and value.strip():
            messages.append(value.strip())

    nested_error = execution.get("errors") or execution.get("errorInfo") or execution.get("details")
    if isinstance(nested_error, dict):
        for nested_value in nested_error.values():
            if isinstance(nested_value, str) and nested_value.strip():
                messages.append(nested_value.strip())
    elif isinstance(nested_error, list):
        for item in nested_error:
            if isinstance(item, str) and item.strip():
                messages.append(item.strip())
            elif isinstance(item, dict):
                for nested_value in item.values():
                    if isinstance(nested_value, str) and nested_value.strip():
                        messages.append(nested_value.strip())

    # Keep order but remove duplicates.
    deduped: list[str] = []
    for message in messages:
        if message not in deduped:
            deduped.append(message)

    return deduped


def count_successful_executions(executions: list[dict[str, Any]]) -> int:
    success_count = 0
    for execution in executions:
        status = first_string(execution, ["status", "state", "runStatus", "statusText"]).lower()
        if any(hint in status for hint in SUCCESS_STATUS_HINTS) and not any(
            failed_hint in status for failed_hint in DEFAULT_FAILED_STATUS_FILTERS
        ):
            success_count += 1
    return success_count


def api_get_json(url: str, headers: dict[str, str], timeout: int = 30) -> tuple[int, Any]:
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        print(f"\nRequest failed for {url}: {exc}")
        return 0, None

    if response.status_code != 200:
        print(f"\nGET {url} returned HTTP {response.status_code}")
        if response.text:
            print(response.text[:1000])
        return response.status_code, None

    try:
        return response.status_code, response.json()
    except ValueError:
        print(f"\nGET {url} did not return valid JSON.")
        return response.status_code, None


def fetch_schedule_executions_for_date(
    document_id: str,
    schedule_id: str,
    target_date: date,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    date_string = target_date.strftime("%Y-%m-%d")

    # Query multiple likely endpoints; deployments differ in where schedule
    # execution history is exposed.
    candidate_urls = [
        f"{DOCUMENTS_URL}/{document_id}/schedules/{schedule_id}/instances?date={date_string}",
        f"{DOCUMENTS_URL}/{document_id}/schedules/{schedule_id}/instances?startDate={date_string}",
        f"{DOCUMENTS_URL}/{document_id}/schedules/{schedule_id}/history?date={date_string}",
        f"{DOCUMENTS_URL}/{document_id}/instances?scheduleId={schedule_id}&date={date_string}",
        f"{DOCUMENTS_URL}/{document_id}/executions?scheduleId={schedule_id}&date={date_string}",
    ]

    seen_keys: set[str] = set()
    merged_results: list[dict[str, Any]] = []

    for url in candidate_urls:
        status_code, payload = api_get_json(url, headers)
        if status_code != 200 or payload is None:
            continue

        for execution in extract_executions(payload):
            execution_id = first_string(execution, ["id", "instanceId", "executionId"])
            start_time = first_string(execution, ["startTime", "startDate", "beginTime", "runDate"])
            unique_key = f"{execution_id}|{start_time}"
            if unique_key in seen_keys:
                continue
            seen_keys.add(unique_key)
            merged_results.append(execution)

    # Some endpoints ignore query params and return full history. Enforce the
    # date filter client-side so the report strictly reflects the requested date.
    return [item for item in merged_results if execution_matches_date(item, target_date)]


def choose_schedules(schedules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(schedules) <= 1:
        return schedules

    print("\nMultiple schedules found:")
    for index, schedule in enumerate(schedules, start=1):
        print(
            f"{index}. {schedule.get('name', 'Unknown')} "
            f"(ID={schedule.get('id', 'N/A')})"
        )

    selection = input(
        "\nSelect schedule number(s) (comma-separated) or 'all' [all]: "
    ).strip()

    if not selection or selection.lower() == "all":
        return schedules

    chosen: list[dict[str, Any]] = []
    max_index = len(schedules)
    for token in selection.split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit():
            continue

        position = int(token)
        if 1 <= position <= max_index:
            chosen.append(schedules[position - 1])

    return chosen or schedules


def print_failed_execution_report(
    schedule_name: str,
    schedule_id: str,
    executions: list[dict[str, Any]],
    filtered_failures: list[dict[str, Any]],
) -> None:
    print("\n" + "#" * 110)
    print(f"Schedule Name : {schedule_name}")
    print(f"Schedule ID   : {schedule_id}")
    print(f"Total Executions on Date : {len(executions)}")
    print(f"Failed Executions Found  : {len(filtered_failures)}")
    print(f"Successful Executions    : {count_successful_executions(executions)}")
    print("#" * 110)

    if not filtered_failures:
        print("No failed executions found for this schedule on the selected date.")
        return

    for index, execution in enumerate(filtered_failures, start=1):
        execution_id = first_string(execution, ["id", "instanceId", "executionId"]) or "N/A"
        status = first_string(execution, ["status", "state", "runStatus", "statusText"]) or "N/A"
        start_time = first_string(execution, ["startTime", "startDate", "beginTime", "runDate"]) or "N/A"
        error_messages = extract_error_details(execution)

        print(f"\n{index}. Execution ID: {execution_id}")
        print(f"   Status    : {status}")
        print(f"   Start Time: {start_time}")
        if error_messages:
            print("   Error(s)  :")
            for message in error_messages:
                print(f"     - {message}")
        else:
            print("   Error(s)  : N/A")


def search_report(report_name: str, headers: dict[str, str]) -> dict[str, Any]:
    search_body = {"search": {"document": {}, "spreadsheet": {}}}

    try:
        response = requests.post(
            SEARCH_URL,
            headers={**headers, "Content-Type": "application/json"},
            json=search_body,
            timeout=60,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"CMS search request failed: {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"CMS search failed with HTTP {response.status_code}: {response.text[:1000]}"
        )

    try:
        search_data = response.json()
    except ValueError as exc:
        raise RuntimeError("CMS search did not return JSON.") from exc

    search_results = search_data.get("search", {})
    documents_found = normalize_to_list(search_results.get("document", []))
    spreadsheets_found = normalize_to_list(search_results.get("spreadsheet", []))

    all_results: list[dict[str, Any]] = []
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
        item for item in all_results if report_name.lower() in str(item.get("name", "")).lower()
    ]

    if not matching_results:
        available = [str(item.get("name", "Unknown")) for item in all_results[:20]]
        raise RuntimeError(
            f"No report matched '{report_name}'. Available sample results: {available}"
        )

    return matching_results[0]


def main() -> None:
    target_check_date = get_target_date()
    report_name = get_report_name()
    status_filters = get_status_filters()

    print(f"\nTarget Check Date : {target_check_date}")
    print(f"Report to search  : {report_name}")
    print(f"Status filter(s)  : {', '.join(status_filters)}")

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
    headers = {"X-SAP-LogonToken": token, "Accept": "application/json"}
    print("Successfully logged in to SAP BusinessObjects.")

    selected_report = search_report(report_name, headers)
    document_id = str(selected_report.get("id", "")).strip()
    if not document_id:
        raise RuntimeError("Matched report did not include a document ID.")

    print("\n" + "=" * 110)
    print("SELECTED REPORT")
    print(f"Name        : {selected_report.get('name', '')}")
    print(f"Document ID : {document_id}")
    print(f"CUID        : {selected_report.get('cuid', '')}")
    print("=" * 110)

    schedules_url = f"{DOCUMENTS_URL}/{document_id}/schedules"
    status_code, schedules_payload = api_get_json(schedules_url, headers)
    if status_code != 200 or schedules_payload is None:
        raise RuntimeError("Unable to fetch schedules for the selected report.")

    schedules = extract_schedule_records(schedules_payload)
    if not schedules:
        print("\nNo schedules were found for this report.")
        return

    selected_schedules = choose_schedules(schedules)
    print(f"\nAnalyzing {len(selected_schedules)} schedule(s)...")

    for schedule in selected_schedules:
        schedule_id = str(schedule.get("id", "")).strip()
        schedule_name = str(schedule.get("name", "Unknown"))

        if not schedule_id:
            print(f"\nSkipping schedule '{schedule_name}' because ID is missing.")
            continue

        executions = fetch_schedule_executions_for_date(
            document_id=document_id,
            schedule_id=schedule_id,
            target_date=target_check_date,
            headers=headers,
        )

        # First derive generic failures, then apply the user-provided status
        # filters so users can narrow (or broaden) status matching criteria.
        failed_executions = identify_failed_executions(executions)
        filtered_failures = filter_executions_by_status(failed_executions, status_filters)

        print_failed_execution_report(
            schedule_name=schedule_name,
            schedule_id=schedule_id,
            executions=executions,
            filtered_failures=filtered_failures,
        )

    print("\n" + "=" * 110)
    print("FAILED EXECUTION ANALYSIS COMPLETED")
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
