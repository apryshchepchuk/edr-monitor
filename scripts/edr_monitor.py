from __future__ import annotations

import html
import json
import os
import re
import shutil
import smtplib
import sys
import time
import zipfile
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from lxml import etree


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
DATA = ROOT / "data"
DOCS_DATA = ROOT / "docs" / "data"
TMP = ROOT / "tmp"

WATCHLIST = CONFIG / "watchlist.json"
SOURCE_STATE = DATA / "source_state.json"
CURRENT = DATA / "current.json"
PREVIOUS = DATA / "previous.json"
CHANGES = DATA / "changes.json"
HISTORY = DATA / "history.json"
DASHBOARD = DOCS_DATA / "dashboard.json"

DEFAULT_UO_URL = (
    "https://data.gov.ua/dataset/03cc1239-3988-4451-aa0d-aadb77448714/"
    "resource/d40cc921-39bb-44fd-be06-dc02589f45c6/download/uo.zip"
)

COMPARE_FIELDS = [
    ("stan", "status_changed", "critical"),
    ("termination_started_info", "termination_started", "critical"),
    ("terminated_info", "terminated", "critical"),
    ("bankruptcy_readjustment_info", "bankruptcy_started", "critical"),
    ("signers", "signers_changed", "medium"),
    ("founders", "founders_changed", "medium"),
    ("beneficiaries", "beneficiaries_changed", "medium"),
    ("short_name", "short_name_changed", "low"),
    ("opf", "opf_changed", "low"),
]


def ensure_dirs() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    DOCS_DATA.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)


def tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))


def now_iso() -> str:
    return datetime.now(tz()).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(tz()).date().isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"Invalid JSON: {path}", file=sys.stderr)
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def normalize_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_code(value: Any) -> str:
    return re.sub(r"\D", "", str(value or "")).strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def direct_child(elem: etree._Element, name: str) -> etree._Element | None:
    for child in elem:
        if local_name(child.tag) == name:
            return child
    return None


def child_text(elem: etree._Element, name: str) -> str:
    child = direct_child(elem, name)
    if child is None:
        return ""
    return normalize_ws(" ".join(child.itertext()))


def compact_list(items: list[str], limit: int = 30) -> list[str]:
    result = []
    seen = set()
    for item in items:
        clean = normalize_ws(item)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
        if len(result) >= limit:
            break
    return result


def container_items(elem: etree._Element, container_name: str) -> list[str]:
    container = direct_child(elem, container_name)
    if container is None:
        return []

    structured = []
    for child in container:
        text = normalize_ws(" ".join(child.itertext()))
        if text:
            structured.append(text)

    if structured:
        return compact_list(structured)

    text = normalize_ws(" ".join(container.itertext()))
    return compact_list([text] if text else [])


def parse_content_length(headers: dict[str, str]) -> str:
    cr = headers.get("Content-Range") or headers.get("content-range")
    if cr and "/" in cr:
        return cr.rsplit("/", 1)[-1].strip()
    return headers.get("Content-Length") or headers.get("content-length") or ""


def probe_source(url: str) -> dict[str, str]:
    headers = {"User-Agent": "edrpou-monitor/1.0"}

    try:
        r = requests.head(url, allow_redirects=True, timeout=45, headers=headers)
        if r.status_code < 400:
            meta = {
                "url": url,
                "final_url": r.url,
                "etag": r.headers.get("ETag", ""),
                "last_modified": r.headers.get("Last-Modified", ""),
                "content_length": parse_content_length(r.headers),
                "checked_at": now_iso(),
                "method": "HEAD",
            }
            if meta["etag"] or meta["last_modified"] or meta["content_length"]:
                return meta
    except requests.RequestException as exc:
        print(f"HEAD failed: {exc}", file=sys.stderr)

    r = requests.get(
        url,
        allow_redirects=True,
        timeout=45,
        headers={**headers, "Range": "bytes=0-0"},
        stream=True,
    )
    r.raise_for_status()
    for _ in r.iter_content(chunk_size=1):
        break

    return {
        "url": url,
        "final_url": r.url,
        "etag": r.headers.get("ETag", ""),
        "last_modified": r.headers.get("Last-Modified", ""),
        "content_length": parse_content_length(r.headers),
        "checked_at": now_iso(),
        "method": "RANGE_GET",
    }


def comparable(meta: dict[str, Any]) -> dict[str, str]:
    return {
        "final_url": str(meta.get("final_url") or ""),
        "etag": str(meta.get("etag") or ""),
        "last_modified": str(meta.get("last_modified") or ""),
        "content_length": str(meta.get("content_length") or ""),
    }


def source_changed(old: dict[str, Any], new: dict[str, Any]) -> bool:
    if not old:
        return True

    new_cmp = comparable(new)
    if not any(new_cmp.values()):
        return True

    return comparable(old) != new_cmp


def get_expected_size(url: str) -> int:
    headers = {"User-Agent": "edrpou-monitor/1.0", "Accept-Encoding": "identity"}

    try:
        r = requests.head(url, allow_redirects=True, timeout=45, headers=headers)
        if r.status_code < 400:
            size = parse_content_length(r.headers)
            return int(size) if str(size).isdigit() else 0
    except requests.RequestException as exc:
        print(f"Could not get expected size by HEAD: {exc}", file=sys.stderr)

    return 0


def download_zip(url: str, dest: Path) -> None:
    print(f"Downloading: {url}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    expected_size = get_expected_size(url)
    if expected_size:
        print(f"Expected size: {expected_size:,} bytes")

    if dest.exists() and expected_size and dest.stat().st_size == expected_size:
        print(f"ZIP already downloaded: {dest}")
        return

    max_attempts = 8
    base_headers = {
        "User-Agent": "edrpou-monitor/1.0",
        "Accept-Encoding": "identity",
    }

    for attempt in range(1, max_attempts + 1):
        existing_size = part.stat().st_size if part.exists() else 0

        if expected_size and existing_size > expected_size:
            print("Partial file is larger than expected. Removing it.")
            part.unlink(missing_ok=True)
            existing_size = 0

        headers = dict(base_headers)

        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"
            print(
                f"Download attempt {attempt}/{max_attempts}: "
                f"resuming from {existing_size:,} bytes"
            )
        else:
            print(f"Download attempt {attempt}/{max_attempts}: starting from zero")

        try:
            with requests.get(
                url,
                stream=True,
                timeout=(30, 300),
                allow_redirects=True,
                headers=headers,
            ) as r:
                if existing_size > 0 and r.status_code == 200:
                    # Server ignored Range. Start over.
                    print("Server ignored Range header. Restarting full download.")
                    part.unlink(missing_ok=True)
                    existing_size = 0
                    mode = "wb"
                elif r.status_code == 206:
                    mode = "ab"
                elif r.status_code == 416 and expected_size and existing_size == expected_size:
                    print("Partial file already complete.")
                    part.replace(dest)
                    return
                else:
                    r.raise_for_status()
                    mode = "wb"

                downloaded_this_attempt = 0

                with part.open(mode) as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            downloaded_this_attempt += len(chunk)

                current_size = part.stat().st_size
                print(
                    f"Downloaded this attempt: {downloaded_this_attempt:,} bytes; "
                    f"partial size: {current_size:,} bytes"
                )

                if expected_size:
                    if current_size == expected_size:
                        part.replace(dest)
                        print(f"Downloaded to {dest} ({dest.stat().st_size:,} bytes)")
                        return

                    print(
                        f"Download incomplete: {current_size:,}/{expected_size:,} bytes. "
                        "Will retry."
                    )
                else:
                    # If size is unknown, accept a non-empty completed response.
                    if current_size > 0:
                        part.replace(dest)
                        print(f"Downloaded to {dest} ({dest.stat().st_size:,} bytes)")
                        return

        except (
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
            requests.exceptions.Timeout,
            requests.exceptions.RequestException,
        ) as exc:
            print(f"Download attempt {attempt} failed: {exc}", file=sys.stderr)

        sleep_seconds = min(60, 5 * attempt)
        print(f"Waiting {sleep_seconds} seconds before retry...")
        time.sleep(sleep_seconds)

    current_size = part.stat().st_size if part.exists() else 0
    raise RuntimeError(
        f"Could not download ZIP after {max_attempts} attempts. "
        f"Partial size: {current_size:,}; expected: {expected_size:,}"
    )


def load_watchlist() -> dict[str, dict[str, Any]]:
    raw = load_json(WATCHLIST, [])
    if not isinstance(raw, list):
        raise RuntimeError("config/watchlist.json must be a JSON array")

    result = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("enabled") is False:
            continue
        code = normalize_code(item.get("edrpou"))
        if code:
            result[code] = item

    return result


def first_xml_name(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not names:
            raise RuntimeError("No XML file in UO.zip")
        return names[0]


def clear_element(elem: etree._Element) -> None:
    parent = elem.getparent()
    elem.clear()
    if parent is not None:
        while elem.getprevious() is not None:
            del parent[0]


def extract_company(subject: etree._Element, watch_item: dict[str, Any]) -> dict[str, Any]:
    return {
        "record": child_text(subject, "RECORD"),
        "edrpou": normalize_code(child_text(subject, "EDRPOU")),
        "watch_name": watch_item.get("name", ""),
        "watch_record": normalize_ws(watch_item.get("record", "")),
        "tags": watch_item.get("tags", []),
        "notes": watch_item.get("notes", ""),
        "watch_severity": watch_item.get("severity", "normal"),
        "found": True,

        "name": child_text(subject, "NAME"),
        "short_name": child_text(subject, "SHORT_NAME"),
        "opf": child_text(subject, "OPF"),
        "stan": child_text(subject, "STAN"),

        "registration_info": child_text(subject, "REGISTRATION"),
        "termination_started_info": child_text(subject, "TERMINATION_STARTED_INFO"),
        "terminated_info": child_text(subject, "TERMINATED_INFO"),
        "termination_cancel_info": child_text(subject, "TERMINATION_CANCEL_INFO"),
        "bankruptcy_readjustment_info": child_text(subject, "BANKRUPTCY_READJUSTMENT_INFO"),

        "signers": container_items(subject, "SIGNERS"),
        "founders": container_items(subject, "FOUNDERS"),
        "beneficiaries": container_items(subject, "BENEFICIARIES"),
    }


def extract_dates_from_text(value: Any) -> list[datetime]:
    text = str(value or "")
    dates: list[datetime] = []

    for match in re.finditer(r"\b(\d{2})\.(\d{2})\.(\d{4})\b", text):
        day, month, year = match.groups()
        try:
            dates.append(datetime(int(year), int(month), int(day)))
        except ValueError:
            pass

    return dates


def latest_company_date(company: dict[str, Any]) -> datetime:
    fields = [
        "registration_info",
        "termination_started_info",
        "terminated_info",
        "termination_cancel_info",
        "bankruptcy_readjustment_info",
    ]

    dates: list[datetime] = []

    for field in fields:
        dates.extend(extract_dates_from_text(company.get(field, "")))

    return max(dates) if dates else datetime.min


def status_group(company: dict[str, Any]) -> str:
    stan = str(company.get("stan") or "").lower()

    if company.get("found") is False:
        return "not_found"

    if "в стані припинення" in stan:
        return "termination"

    if "банкрут" in stan:
        return "bankruptcy"

    if "зареєстровано" in stan:
        return "registered"

    if "припинено" in stan:
        return "terminated"

    return "other"


def state_rank(company: dict[str, Any]) -> int:
    """
    Важливо:
    - "припинено" має бути нижче будь-якого неприпиненого стану;
    - "зареєстровано" має перемагати історичні припинені записи;
    - "в стані припинення" і банкрутство — найризиковіші актуальні стани.
    """
    group = status_group(company)

    ranks = {
        "termination": 600,
        "bankruptcy": 550,
        "registered": 500,
        "other": 300,
        "terminated": 100,
        "not_found": 0,
    }

    return ranks.get(group, 0)


def soft_name(value: Any) -> str:
    value = normalize_ws(value).lower()
    return re.sub(r"[^а-яіїєґa-z0-9]+", "", value)


def name_match_rank(company: dict[str, Any], watch_item: dict[str, Any]) -> int:
    watch_name = normalize_ws(watch_item.get("name", "")).lower()
    official_name = normalize_ws(company.get("name", "")).lower()
    short_name = normalize_ws(company.get("short_name", "")).lower()

    if not watch_name:
        return 0

    if watch_name == official_name or watch_name == short_name:
        return 100

    if watch_name in official_name or watch_name in short_name:
        return 80

    sw = soft_name(watch_name)
    so = soft_name(official_name)
    ss = soft_name(short_name)

    if sw and (sw in so or sw in ss or so in sw or ss in sw):
        return 60

    return 0


def company_score(company: dict[str, Any], watch_item: dict[str, Any]) -> tuple[int, int, datetime]:
    """
    Порядок:
    1. стан;
    2. схожість назви з watchlist;
    3. дата реєстраційної/статусної інформації.
    """
    return (
        state_rank(company),
        name_match_rank(company, watch_item),
        latest_company_date(company),
    )


def selection_reason_for(
    selected: dict[str, Any],
    candidates: list[dict[str, Any]],
    watch_item: dict[str, Any],
) -> str:
    watch_record = normalize_ws(watch_item.get("record", ""))
    if watch_record and normalize_ws(selected.get("record", "")) == watch_record:
        return "selected_by_watchlist_record"

    if len(candidates) == 1:
        return "single_record"

    groups = {status_group(item) for item in candidates}
    selected_group = status_group(selected)

    if "terminated" in groups and selected_group != "terminated":
        return "selected_non_terminated_record"

    if groups == {"terminated"}:
        return "all_candidates_terminated_selected_best_by_score"

    if selected_group in {"termination", "bankruptcy"}:
        return "selected_risk_status_record"

    return "selected_by_state_name_date_score"


def candidate_summary(item: dict[str, Any], watch_item: dict[str, Any]) -> dict[str, Any]:
    score = company_score(item, watch_item)
    score_date = score[2].isoformat() if score[2] != datetime.min else ""

    return {
        "record": item.get("record", ""),
        "edrpou": item.get("edrpou", ""),
        "name": item.get("name", ""),
        "short_name": item.get("short_name", ""),
        "opf": item.get("opf", ""),
        "stan": item.get("stan", ""),
        "status_group": status_group(item),
        "registration_info": item.get("registration_info", ""),
        "termination_started_info": item.get("termination_started_info", ""),
        "terminated_info": item.get("terminated_info", ""),
        "termination_cancel_info": item.get("termination_cancel_info", ""),
        "bankruptcy_readjustment_info": item.get("bankruptcy_readjustment_info", ""),
        "score": {
            "state_rank": score[0],
            "name_match_rank": score[1],
            "latest_date": score_date,
        },
    }


def select_best_company_record(
    code: str,
    candidates: list[dict[str, Any]],
    watch_item: dict[str, Any],
) -> dict[str, Any]:
    watch_record = normalize_ws(watch_item.get("record", ""))

    selected: dict[str, Any] | None = None

    if watch_record:
        exact_matches = [
            item
            for item in candidates
            if normalize_ws(item.get("record", "")) == watch_record
        ]
        if exact_matches:
            selected = exact_matches[0]

    if selected is None:
        selected = max(candidates, key=lambda item: company_score(item, watch_item))

    selected["candidate_count"] = len(candidates)
    selected["selected_from_duplicate_records"] = len(candidates) > 1
    selected["selection_reason"] = selection_reason_for(selected, candidates, watch_item)

    if len(candidates) > 1:
        selected["duplicate_records_summary"] = [
            candidate_summary(item, watch_item)
            for item in candidates
        ]
    else:
        selected["duplicate_records_summary"] = []

    return selected


def parse_uo_zip(zip_path: Path, watch: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    xml_name = first_xml_name(zip_path)
    candidates_by_code: dict[str, list[dict[str, Any]]] = {}

    print(f"Parsing {xml_name}; watchlist: {len(watch)} codes")

    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(xml_name) as xml_file:
            context = etree.iterparse(
                xml_file,
                events=("end",),
                tag="SUBJECT",
                recover=True,
                huge_tree=True,
            )

            processed = 0

            for _event, elem in context:
                processed += 1

                if processed % 100000 == 0:
                    print(
                        f"Processed: {processed:,}; "
                        f"matched codes: {len(candidates_by_code)}/{len(watch)}"
                    )

                code = normalize_code(child_text(elem, "EDRPOU"))

                if code in watch:
                    candidate = extract_company(elem, watch[code])
                    candidates_by_code.setdefault(code, []).append(candidate)

                    print(
                        f"Candidate {code}: "
                        f"{candidate.get('name') or watch[code].get('name')} "
                        f"[{candidate.get('stan')}] "
                        f"RECORD={candidate.get('record')}"
                    )

                clear_element(elem)

    found: dict[str, dict[str, Any]] = {}

    for code, candidates in sorted(candidates_by_code.items()):
        selected = select_best_company_record(code, candidates, watch[code])
        found[code] = selected

        if len(candidates) > 1:
            print(f"Duplicate EDRPOU records detected: {code}")
            for item in candidates:
                print(
                    "  - "
                    f"RECORD={item.get('record')} | "
                    f"{item.get('name')} | "
                    f"STAN={item.get('stan')} | "
                    f"group={status_group(item)} | "
                    f"score={company_score(item, watch[code])}"
                )
            print(
                f"  SELECTED: RECORD={selected.get('record')} | "
                f"{selected.get('name')} "
                f"[{selected.get('stan')}] | "
                f"reason={selected.get('selection_reason')}"
            )

    missing = set(watch.keys()) - set(found.keys())

    for code in sorted(missing):
        item = watch[code]
        found[code] = {
            "record": "",
            "edrpou": code,
            "watch_name": item.get("name", ""),
            "watch_record": normalize_ws(item.get("record", "")),
            "tags": item.get("tags", []),
            "notes": item.get("notes", ""),
            "watch_severity": item.get("severity", "normal"),
            "found": False,
            "name": "",
            "short_name": "",
            "opf": "",
            "stan": "не знайдено у джерелі",
            "registration_info": "",
            "termination_started_info": "",
            "terminated_info": "",
            "termination_cancel_info": "",
            "bankruptcy_readjustment_info": "",
            "signers": [],
            "founders": [],
            "beneficiaries": [],
            "candidate_count": 0,
            "selected_from_duplicate_records": False,
            "selection_reason": "not_found",
            "duplicate_records_summary": [],
        }

    return found


def norm_compare(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(normalize_ws(x) for x in value if normalize_ws(x))
    return normalize_ws(value)


def status_change_type(status: str) -> tuple[str, str]:
    s = status.lower()
    if "банкрут" in s:
        return "bankruptcy_started", "critical"
    if "в стані припинення" in s:
        return "termination_started", "critical"
    if "припинено" in s:
        return "terminated", "critical"
    return "status_changed", "medium"


def make_change(
    code: str,
    company: dict[str, Any],
    change_type: str,
    severity: str,
    old_value: Any,
    new_value: Any,
    details: str = "",
) -> dict[str, Any]:
    return {
        "severity": severity,
        "edrpou": code,
        "record": company.get("record", ""),
        "name": company.get("name") or company.get("watch_name") or "",
        "watch_name": company.get("watch_name") or "",
        "tags": company.get("tags", []),
        "change_type": change_type,
        "old_value": old_value if isinstance(old_value, str) else json.dumps(old_value, ensure_ascii=False),
        "new_value": new_value if isinstance(new_value, str) else json.dumps(new_value, ensure_ascii=False),
        "details": details,
        "detected_at": now_iso(),
    }


def dedupe(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for ch in changes:
        key = (ch["edrpou"], ch["change_type"], ch["old_value"], ch["new_value"])
        if key in seen:
            continue
        seen.add(key)
        result.append(ch)

    order = {"critical": 0, "medium": 1, "low": 2}
    return sorted(result, key=lambda x: (order.get(x["severity"], 9), x["name"]))


def compare_snapshots(previous: dict[str, Any], current: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    old_list = previous.get("companies", []) if isinstance(previous, dict) else []
    old_by_code = {normalize_code(x.get("edrpou")): x for x in old_list if isinstance(x, dict)}

    if not old_by_code:
        print("No previous snapshot. Baseline only, no alerts.")
        return []

    changes = []

    for code, cur in sorted(current.items()):
        old = old_by_code.get(code)

        if old is None:
            changes.append(make_change(code, cur, "added_to_monitoring", "low", "", cur.get("stan", "")))
            continue

        if old.get("found", True) and not cur.get("found", True):
            changes.append(make_change(
                code, cur, "not_found_in_source", "medium",
                old.get("stan", ""), "не знайдено у джерелі"
            ))
            continue

        for field, default_type, default_severity in COMPARE_FIELDS:
            old_value = norm_compare(old.get(field, [] if field in {"signers", "founders", "beneficiaries"} else ""))
            new_value = norm_compare(cur.get(field, [] if field in {"signers", "founders", "beneficiaries"} else ""))

            if old_value == new_value:
                continue

            change_type = default_type
            severity = default_severity
            details = ""

            if field == "stan":
                change_type, severity = status_change_type(str(new_value))
            elif field == "termination_started_info" and not old_value and new_value:
                change_type, severity, details = "termination_started", "critical", str(new_value)
            elif field == "terminated_info" and not old_value and new_value:
                change_type, severity, details = "terminated", "critical", str(new_value)
            elif field == "bankruptcy_readjustment_info" and not old_value and new_value:
                change_type, severity, details = "bankruptcy_started", "critical", str(new_value)

            changes.append(make_change(code, cur, change_type, severity, old_value, new_value, details))

    return dedupe(changes)


def summarize(changes: list[dict[str, Any]]) -> dict[str, int]:
    result = {"critical": 0, "medium": 0, "low": 0, "total": len(changes)}
    for ch in changes:
        if ch["severity"] in result:
            result[ch["severity"]] += 1
    return result


def update_history(summary: dict[str, int], source: dict[str, Any]) -> list[dict[str, Any]]:
    history = load_json(HISTORY, [])
    if not isinstance(history, list):
        history = []

    history.append({
        "date": today(),
        "generated_at": now_iso(),
        "critical": summary["critical"],
        "medium": summary["medium"],
        "low": summary["low"],
        "total": summary["total"],
        "source_last_modified": source.get("last_modified", ""),
        "source_etag": source.get("etag", ""),
    })

    history = history[-250:]
    save_json(HISTORY, history)
    return history


def build_email_html(doc: dict[str, Any], dashboard_url: str) -> str:
    summary = doc.get("summary", {})
    changes = doc.get("changes", [])

    def esc(v: Any) -> str:
        return html.escape(str(v or ""))

    def color(sev: str) -> str:
        return {"critical": "#b42318", "medium": "#b54708", "low": "#175cd3"}.get(sev, "#175cd3")

    def label(sev: str) -> str:
        return {"critical": "Критично", "medium": "Середньо", "low": "Низько"}.get(sev, sev or "—")

    cards = ""
    for ch in changes[:60]:
        c = color(ch.get("severity"))
        cards += f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #e5e7eb;border-left:6px solid {c};border-radius:14px;margin:0 0 12px;">
          <tr><td style="padding:16px;font-family:Arial,Helvetica,sans-serif;">
            <div style="font-size:16px;font-weight:800;color:#172033;">{esc(ch.get("name") or ch.get("watch_name"))}</div>
            <div style="font-size:12px;color:#667085;margin-top:3px;">ЄДРПОУ: {esc(ch.get("edrpou"))} · RECORD: {esc(ch.get("record"))} · {esc(ch.get("change_type"))}</div>
            <div style="margin-top:8px;">
              <span style="display:inline-block;background:#f5f7fb;color:{c};font-size:12px;font-weight:700;padding:4px 9px;border-radius:999px;">{esc(label(ch.get("severity")))}</span>
            </div>
            <div style="font-size:13px;line-height:19px;color:#344054;margin-top:10px;">
              <b>Було:</b> {esc(ch.get("old_value") or "—")}<br>
              <b>Стало:</b> {esc(ch.get("new_value") or "—")}
            </div>
            {f'<div style="font-size:13px;line-height:19px;color:#344054;margin-top:10px;">{esc(ch.get("details"))}</div>' if ch.get("details") else ''}
          </td></tr>
        </table>
        """

    if not cards:
        cards = """
        <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;margin:16px 0;">
          <tr><td style="padding:16px;font-family:Arial,Helvetica,sans-serif;color:#344054;">Змін за поточний прогін не виявлено.</td></tr>
        </table>
        """

    button = ""
    if dashboard_url:
        button = f"""
        <table cellpadding="0" cellspacing="0" style="margin-top:18px;">
          <tr><td bgcolor="#175cd3" style="border-radius:12px;">
            <a href="{esc(dashboard_url)}" style="display:inline-block;padding:12px 18px;color:#ffffff;text-decoration:none;font-family:Arial,Helvetica,sans-serif;font-weight:700;">Переглянути dashboard</a>
          </td></tr>
        </table>
        """

    return f"""
<!doctype html>
<html>
<body style="margin:0;background:#f5f7fb;padding:24px 0;">
  <center>
    <table width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:680px;">
      <tr><td style="padding:0 16px;">
        <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #e5e7eb;border-radius:18px;">
          <tr><td style="padding:24px;font-family:Arial,Helvetica,sans-serif;">
            <div style="font-size:26px;font-weight:800;color:#172033;">Моніторинг ЄДРПОУ</div>
            <div style="font-size:14px;color:#667085;margin-top:6px;">Звіт про зміни · {esc(doc.get("generated_at"))}</div>
          </td></tr>
        </table>

        <table width="100%" cellpadding="0" cellspacing="0" style="margin:14px 0;">
          <tr>
            <td style="padding:6px;"><div style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:14px;font-family:Arial;"><b style="font-size:24px;color:#b42318;">{summary.get("critical", 0)}</b><br><span style="font-size:12px;color:#667085;">Критичні</span></div></td>
            <td style="padding:6px;"><div style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:14px;font-family:Arial;"><b style="font-size:24px;color:#b54708;">{summary.get("medium", 0)}</b><br><span style="font-size:12px;color:#667085;">Середні</span></div></td>
            <td style="padding:6px;"><div style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:14px;font-family:Arial;"><b style="font-size:24px;color:#175cd3;">{summary.get("low", 0)}</b><br><span style="font-size:12px;color:#667085;">Низькі</span></div></td>
            <td style="padding:6px;"><div style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:14px;font-family:Arial;"><b style="font-size:24px;color:#172033;">{summary.get("total", 0)}</b><br><span style="font-size:12px;color:#667085;">Усього</span></div></td>
          </tr>
        </table>

        {cards}
        {button}

        <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;color:#667085;margin-top:22px;">
          Джерело даних: відкриті дані ЄДР з порталу data.gov.ua. Лист є інформаційним повідомленням і не замінює офіційний витяг з ЄДР.
        </div>
      </td></tr>
    </table>
  </center>
</body>
</html>
"""


def build_plain(doc: dict[str, Any], dashboard_url: str) -> str:
    lines = ["Моніторинг ЄДРПОУ", f"Звіт: {doc.get('generated_at', '')}", ""]
    s = doc.get("summary", {})
    lines += [
        f"Критичні: {s.get('critical', 0)}",
        f"Середні: {s.get('medium', 0)}",
        f"Низькі: {s.get('low', 0)}",
        f"Усього: {s.get('total', 0)}",
        "",
    ]

    for ch in doc.get("changes", [])[:60]:
        lines.append(f"- [{ch.get('severity')}] {ch.get('edrpou')} {ch.get('name') or ch.get('watch_name')}")
        lines.append(f"  RECORD: {ch.get('record') or '—'}")
        lines.append(f"  {ch.get('change_type')}: {ch.get('old_value') or '—'} → {ch.get('new_value') or '—'}")
        if ch.get("details"):
            lines.append(f"  {ch.get('details')}")
        lines.append("")

    if dashboard_url:
        lines.append(f"Dashboard: {dashboard_url}")

    return "\n".join(lines)


def split_addresses(raw: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,;]", raw or "") if x.strip()]


def format_from(raw: str) -> str:
    name, addr = parseaddr(raw)
    if name and addr:
        return formataddr((str(Header(name, "utf-8")), addr))
    return raw


def send_email(doc: dict[str, Any]) -> None:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    email_from = os.getenv("EMAIL_FROM", smtp_user).strip()
    email_to = split_addresses(os.getenv("EMAIL_TO", ""))
    dashboard_url = os.getenv("DASHBOARD_URL", "").strip()

    if not smtp_host or not smtp_user or not smtp_pass or not email_from or not email_to:
        print("Email is not configured. Skipping email.")
        return

    s = doc.get("summary", {})
    subject = f"Моніторинг ЄДРПОУ: критичні {s.get('critical', 0)}, середні {s.get('medium', 0)}, усього {s.get('total', 0)}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = format_from(email_from)
    msg["To"] = ", ".join(email_to)

    msg.attach(MIMEText(build_plain(doc, dashboard_url), "plain", "utf-8"))
    msg.attach(MIMEText(build_email_html(doc, dashboard_url), "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(email_from, email_to, msg.as_string())

    print(f"Email sent to {', '.join(email_to)}")


def build_current(companies: dict[str, dict[str, Any]], source: dict[str, Any]) -> dict[str, Any]:
    items = [companies[k] for k in sorted(companies)]
    return {
        "generated_at": now_iso(),
        "source": source,
        "stats": {
            "watchlist_enabled": len(items),
            "found": sum(1 for x in items if x.get("found")),
            "not_found": sum(1 for x in items if not x.get("found")),
            "selected_from_duplicate_records": sum(
                1 for x in items if x.get("selected_from_duplicate_records")
            ),
        },
        "companies": items,
    }


def main() -> None:
    ensure_dirs()

    source_url = os.getenv("EDR_UO_URL", "").strip() or DEFAULT_UO_URL
    old_source = load_json(SOURCE_STATE, {})
    new_source = probe_source(source_url)

    changed = source_changed(old_source, new_source)
    email_force = env_bool("EMAIL_FORCE", False)
    send_no_changes = env_bool("EMAIL_SEND_NO_CHANGES", False)

    print("Source changed:", changed)
    print(json.dumps(comparable(new_source), ensure_ascii=False, indent=2))

    if not changed and not email_force:
        print("Source not changed. Skipping download and parsing.")
        merged = dict(old_source)
        merged["checked_at"] = new_source.get("checked_at", now_iso())
        merged["last_probe"] = new_source
        save_json(SOURCE_STATE, merged)
        return

    watch = load_watchlist()
    if not watch:
        raise RuntimeError("No enabled codes in config/watchlist.json")

    zip_path = TMP / "uo.zip"
    if changed or not zip_path.exists():
        download_zip(source_url, zip_path)

    companies = parse_uo_zip(zip_path, watch)

    previous_doc = load_json(CURRENT, {})
    current_doc = build_current(companies, new_source)
    changes = compare_snapshots(previous_doc, companies)
    summary = summarize(changes)

    changes_doc = {
        "generated_at": now_iso(),
        "source": new_source,
        "summary": summary,
        "changes": changes,
    }

    history = update_history(summary, new_source)

    if CURRENT.exists():
        shutil.copyfile(CURRENT, PREVIOUS)

    save_json(CURRENT, current_doc)
    save_json(CHANGES, changes_doc)

    dashboard_doc = {
        "generated_at": current_doc["generated_at"],
        "source": new_source,
        "stats": current_doc["stats"],
        "summary": summary,
        "companies": current_doc["companies"],
        "changes": changes,
        "history": history,
    }
    save_json(DASHBOARD, dashboard_doc)
    save_json(SOURCE_STATE, new_source)

    if changes or email_force or send_no_changes:
        send_email(changes_doc)
    else:
        print("No changes. Email not sent.")

    print("Done.")


if __name__ == "__main__":
    main()
