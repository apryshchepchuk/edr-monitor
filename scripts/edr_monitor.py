from __future__ import annotations

import html
import json
import os
import re
import shutil
import smtplib
import subprocess
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
from urllib.parse import urljoin
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

NAIS_EDR_PAGE_URL = (
    "https://nais.gov.ua/m/"
    "ediniy-derjavniy-reestr-yuridichnih-osib-fizichnih-osib-pidpriemtsiv-ta-gromadskih-formuvan"
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


def request_headers(referer: str = "", extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/zip,application/octet-stream,*/*",
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
    }

    if referer:
        headers["Referer"] = referer

    if extra:
        headers.update(extra)

    return headers

def is_html_response(response: requests.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    return "text/html" in content_type or "text/plain" in content_type


def print_response_debug(response: requests.Response) -> None:
    print(f"Download response status: {response.status_code}", flush=True)
    print(f"Content-Type: {response.headers.get('content-type', '')}", flush=True)
    print(f"Content-Length: {response.headers.get('content-length', '')}", flush=True)

    if response.status_code != 200 or is_html_response(response):
        try:
            preview = response.text[:700]
        except Exception:
            preview = ""

        if preview:
            print(f"Error response preview: {preview}", flush=True)


def fetch_nais_page_html(page_url: str = NAIS_EDR_PAGE_URL) -> tuple[str, str]:
    """
    ASVP-style:
    1 коротка спроба requests.get з Referer;
    якщо GitHub runner зависає/timeout — fallback на curl.

    Для curl примусово використовуємо HTTP/1.1 та IPv4,
    бо на GitHub runner НАІС може падати з HTTP/2 INTERNAL_ERROR.
    """
    last_exc: Exception | None = None

    # Не чекаємо 3 x 90 секунд. Одна спроба requests, далі curl.
    for attempt in range(1, 2):
        try:
            print(
                f"Fetching NAIS page with requests, attempt {attempt}/1: {page_url}",
                flush=True,
            )

            response = requests.get(
                page_url,
                timeout=90,
                headers=request_headers("https://nais.gov.ua/"),
                allow_redirects=True,
            )

            print(f"NAIS page status: {response.status_code}", flush=True)
            response.raise_for_status()

            if response.text.strip():
                return response.text, response.url

        except requests.RequestException as exc:
            last_exc = exc
            print(
                f"NAIS page requests attempt {attempt} failed: {exc}",
                file=sys.stderr,
                flush=True,
            )

    print(f"Trying NAIS page via curl after requests failure: {last_exc}", flush=True)

    curl_cmd = [
        "curl",
        "--http1.1",
        "--ipv4",
        "-L",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        "240",
        "--connect-timeout",
        "30",
        "--compressed",
        "-A",
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H",
        "Accept-Language: uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        "-e",
        "https://nais.gov.ua/",
        page_url,
    ]

    result = subprocess.run(
        curl_cmd,
        text=True,
        capture_output=True,
        timeout=270,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Could not fetch NAIS page via requests or curl. "
            f"requests error: {last_exc}; curl stderr: {result.stderr[-1000:]}"
        )

    html_text = result.stdout or ""

    if not html_text.strip():
        raise RuntimeError("curl returned empty NAIS page HTML")

    return html_text, page_url


def probe_source(url: str) -> dict[str, Any]:
    try:
        r = requests.head(
            url,
            allow_redirects=True,
            timeout=45,
            headers=request_headers(),
        )

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

        raise requests.HTTPError(
            f"HEAD returned HTTP {r.status_code} for {url}",
            response=r,
        )

    except requests.RequestException as exc:
        print(f"HEAD probe failed for {url}: {exc}", file=sys.stderr)

    r = requests.get(
        url,
        allow_redirects=True,
        timeout=45,
        headers=request_headers(extra={"Range": "bytes=0-0"}),
        stream=True,
    )

    if r.status_code >= 400:
        raise requests.HTTPError(
            f"Range probe returned HTTP {r.status_code} for {url}",
            response=r,
        )

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


def strip_html(value: str) -> str:
    return normalize_ws(re.sub(r"<[^>]+>", " ", value or ""))

def discover_nais_uo_zip_url(page_url: str = NAIS_EDR_PAGE_URL) -> tuple[str, str]:
    print(f"Discovering fallback ZIP from NAIS page: {page_url}", flush=True)

    html_text, final_page_url = fetch_nais_page_html(page_url)

    candidates: list[tuple[int, str, str]] = []

    # Варіант 1: anchor-aware пошук, якщо HTML нормально містить <a href="...zip">label</a>.
    for match in re.finditer(
        r"<a\b[^>]*href=[\"']([^\"']+?\.zip(?:\?[^\"']*)?)[\"'][^>]*>(.*?)</a>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        href = match.group(1)
        label = strip_html(match.group(2))
        full_url = urljoin(final_page_url, href)

        haystack = f"{label} {full_url}".lower()

        score = 0

        if "ufopfsu" in haystack:
            score += 100

        if "16-" in haystack or "16_" in haystack:
            score += 30

        if re.search(r"\d{2}\.\d{2}\.\d{4}", label):
            score += 20

        if "/files/general/202" in full_url:
            score += 10

        if "xsd" in haystack or "_xsd" in haystack or "schema" in haystack:
            score -= 500

        candidates.append((score, full_url, label))

    # Варіант 2: ASVP-style raw regex по всьому HTML.
    # Це потрібно, якщо label/anchor парситься погано.
    raw_links = re.findall(
        r'(?:https://nais\.gov\.ua)?/files/general/[^"\']+\.zip',
        html_text,
        flags=re.IGNORECASE,
    )

    normalized_raw_links = sorted({
        urljoin("https://nais.gov.ua", link)
        for link in raw_links
    })

    if normalized_raw_links:
        print("NAIS raw ZIP links found:", flush=True)
        for link in normalized_raw_links:
            print(f"  {link}", flush=True)

    known_candidate_urls = {url for _score, url, _label in candidates}

    for link in normalized_raw_links:
        if link in known_candidate_urls:
            continue

        haystack = link.lower()

        score = 10

        if "/files/general/202" in haystack:
            score += 20

        # Якщо label недоступний, орієнтуємось на найновіший URL.
        # XSD часто має старішу дату, але не завжди містить xsd в URL.
        candidates.append((score, link, ""))

    if not candidates:
        debug_path = TMP / "nais_page_debug.html"
        debug_path.write_text(html_text[:300_000], encoding="utf-8", errors="ignore")
        raise RuntimeError(f"No ZIP links found on NAIS page. Debug saved to {debug_path}")

    # Ключове: якщо є кандидати з label і там видно xsd — вони матимуть великий мінус.
    # Якщо label не видно — беремо найновіший /files/general/202... URL за рядковим сортуванням.
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)

    print("NAIS ZIP candidates ranked:", flush=True)
    for score, url, label in candidates:
        print(f"  score={score}; label={label or '—'}; url={url}", flush=True)

    best_score, best_url, best_label = candidates[0]

    if best_score < 0:
        raise RuntimeError(
            f"Only schema/XSD-like ZIP links found on NAIS page. "
            f"Best candidate: {best_url} ({best_label})"
        )

    print(f"Selected NAIS fallback ZIP: {best_label or best_url} -> {best_url}", flush=True)

    return best_url, best_label


def cached_nais_url(old_source: dict[str, Any]) -> str:
    if not isinstance(old_source, dict):
        return ""

    source_kind = str(old_source.get("source_kind") or "")

    if not source_kind.startswith("nais_"):
        return ""

    for key in ("url", "final_url"):
        value = str(old_source.get(key) or "").strip()
        if value.lower().endswith(".zip") or ".zip?" in value.lower():
            return value

    return ""


def resolve_source(primary_url: str, old_source: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """
    Порядок:
    1. data.gov.ua primary URL;
    2. NAIS page discovery ASVP-style;
    3. cached NAIS URL з попереднього source_state.json.
    """
    try:
        print(f"Trying primary source: {primary_url}", flush=True)
        meta = probe_source(primary_url)
        meta["source_kind"] = "data_gov_primary"
        return primary_url, meta

    except requests.RequestException as exc:
        print(f"Primary source unavailable: {exc}", file=sys.stderr, flush=True)

    discovery_error: Exception | None = None

    try:
        fallback_url, fallback_label = discover_nais_uo_zip_url()

        meta = {
            "url": fallback_url,
            "final_url": fallback_url,
            "etag": "",
            "last_modified": fallback_label,
            "content_length": "",
            "checked_at": now_iso(),
            "method": "NAIS_PAGE_REGEX",
            "source_kind": "nais_page_fallback",
            "source_page": NAIS_EDR_PAGE_URL,
            "source_label": fallback_label,
        }

        print(f"Resolved NAIS fallback source: {fallback_url}", flush=True)
        return fallback_url, meta

    except Exception as exc:
        discovery_error = exc
        print(f"NAIS page discovery failed: {exc}", file=sys.stderr, flush=True)

    cached_url = cached_nais_url(old_source)

    if cached_url:
        print(f"Using cached NAIS fallback URL from source_state.json: {cached_url}", flush=True)

        meta = {
            "url": cached_url,
            "final_url": cached_url,
            "etag": "",
            "last_modified": old_source.get("last_modified", ""),
            "content_length": "",
            "checked_at": now_iso(),
            "method": "NAIS_CACHED_URL",
            "source_kind": "nais_cached_fallback",
            "source_page": NAIS_EDR_PAGE_URL,
            "source_label": old_source.get("source_label", "cached NAIS ZIP"),
            "cached_fallback_used": True,
        }

        return cached_url, meta

    raise RuntimeError(
        "Unable to resolve EDR source: primary data.gov.ua unavailable, "
        f"NAIS page discovery failed, and cached fallback is unavailable. "
        f"Discovery error: {discovery_error}"
    )


def get_expected_size(url: str) -> int:
    headers = request_headers(extra={"Accept-Encoding": "identity"})

    try:
        r = requests.head(url, allow_redirects=True, timeout=45, headers=headers)
        if r.status_code < 400:
            size = parse_content_length(r.headers)
            return int(size) if str(size).isdigit() else 0
    except requests.RequestException as exc:
        print(f"Could not get expected size by HEAD: {exc}", file=sys.stderr)

    return 0


def download_zip(url: str, dest: Path) -> None:
    print(f"Downloading ZIP: {url}", flush=True)

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    part.unlink(missing_ok=True)

    source_name = "nais.gov.ua" if "nais.gov.ua" in url.lower() else "data.gov.ua"
    referer = NAIS_EDR_PAGE_URL if source_name == "nais.gov.ua" else ""

    max_attempts = 3 if source_name == "nais.gov.ua" else 2
    sleep_base_seconds = 45
    headers = request_headers(referer)

    for attempt in range(1, max_attempts + 1):
        print(
            f"Downloading EDR ZIP from {source_name}, "
            f"attempt {attempt}/{max_attempts}: {part}",
            flush=True,
        )

        try:
            with requests.get(
                url,
                stream=True,
                timeout=900,
                headers=headers,
                allow_redirects=True,
            ) as response:
                print_response_debug(response)
                response.raise_for_status()

                if is_html_response(response):
                    raise RuntimeError(f"{source_name} returned HTML/text instead of ZIP")

                total = 0

                with part.open("wb") as f:
                    for chunk in response.iter_content(chunk_size=1024 * 1024 * 8):
                        if not chunk:
                            continue

                        f.write(chunk)
                        total += len(chunk)

                        if total % (1024 * 1024 * 500) < (1024 * 1024 * 8):
                            print(
                                f"Downloaded from {source_name}: "
                                f"{total / 1024 / 1024:.1f} MB",
                                flush=True,
                            )

            size_mb = part.stat().st_size / 1024 / 1024
            print(f"Download complete from {source_name}: {size_mb:.1f} MB", flush=True)

            if size_mb < 10:
                raise RuntimeError(f"Downloaded file is unexpectedly small: {size_mb:.1f} MB")

            part.replace(dest)
            print(f"Downloaded to {dest} ({dest.stat().st_size:,} bytes)", flush=True)
            return

        except Exception as exc:
            part.unlink(missing_ok=True)

            print(
                f"Download attempt {attempt}/{max_attempts} "
                f"from {source_name} failed: {exc}",
                file=sys.stderr,
                flush=True,
            )

            if attempt >= max_attempts:
                raise

            sleep_seconds = sleep_base_seconds * attempt
            print(f"Sleeping {sleep_seconds} seconds before retry...", flush=True)
            time.sleep(sleep_seconds)

    raise RuntimeError(f"Download failed from {source_name}")


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
            raise RuntimeError("No XML file found inside ZIP")

        def base(name: str) -> str:
            return Path(name).name.lower()

        for name in names:
            if base(name) == "uo.xml":
                print(f"Selected XML inside ZIP: {name}")
                return name

        uo_candidates = [
            name
            for name in names
            if "uo" in base(name)
            and "fop" not in base(name)
            and "fsu" not in base(name)
            and "schema" not in base(name)
            and "xsd" not in base(name)
        ]

        if uo_candidates:
            selected = sorted(uo_candidates, key=len)[0]
            print(f"Selected XML inside ZIP: {selected}")
            return selected

        selected = names[0]
        print(f"WARNING: UO.xml not found explicitly. Selected first XML: {selected}")
        return selected


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
        "opf": company.get("opf", ""),
        "stan": company.get("stan", ""),
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
            changes.append(
                make_change(
                    code,
                    cur,
                    "not_found_in_source",
                    "medium",
                    old.get("stan", ""),
                    "не знайдено у джерелі",
                )
            )
            continue

        for field, default_type, default_severity in COMPARE_FIELDS:
            old_value = norm_compare(
                old.get(field, [] if field in {"signers", "founders", "beneficiaries"} else "")
            )
            new_value = norm_compare(
                cur.get(field, [] if field in {"signers", "founders", "beneficiaries"} else "")
            )

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

    history.append(
        {
            "date": today(),
            "generated_at": now_iso(),
            "critical": summary["critical"],
            "medium": summary["medium"],
            "low": summary["low"],
            "total": summary["total"],
            "source_last_modified": source.get("last_modified", ""),
            "source_etag": source.get("etag", ""),
            "source_kind": source.get("source_kind", ""),
            "source_label": source.get("source_label", ""),
        }
    )

    history = history[-250:]
    save_json(HISTORY, history)

    return history


def build_email_html(doc: dict[str, Any], dashboard_url: str) -> str:
    summary = doc.get("summary", {})
    changes = doc.get("changes", [])
    source = doc.get("source", {})
    generated_at = doc.get("generated_at", "")

    def esc(v: Any) -> str:
        return html.escape(str(v or ""))

    def truncate(value: Any, limit: int = 520) -> str:
        text = normalize_ws(value)

        if len(text) <= limit:
            return text

        return text[:limit].rstrip() + "…"

    def color(sev: str) -> str:
        return {
            "critical": "#b42318",
            "medium": "#b54708",
            "low": "#175cd3",
        }.get(sev, "#175cd3")

    def light_bg(sev: str) -> str:
        return {
            "critical": "#fef3f2",
            "medium": "#fffaeb",
            "low": "#eff8ff",
        }.get(sev, "#f8fafc")

    def label(sev: str) -> str:
        return {
            "critical": "Критично",
            "medium": "Середньо",
            "low": "Низько",
        }.get(sev, sev or "—")

    def change_label(change_type: str) -> str:
        return {
            "termination_started": "Початок припинення",
            "terminated": "Припинено",
            "bankruptcy_started": "Банкрутство / санація",
            "status_critical_changed": "Критична зміна стану",
            "status_changed": "Зміна стану",
            "signers_changed": "Зміна керівника / підписантів",
            "founders_changed": "Зміна засновників",
            "beneficiaries_changed": "Зміна КБВ",
            "short_name_changed": "Зміна короткої назви",
            "opf_changed": "Зміна ОПФ",
            "added_to_monitoring": "Додано до моніторингу",
            "not_found_in_source": "Не знайдено в джерелі",
            "found_in_source": "Знову знайдено в джерелі",
        }.get(change_type, change_type or "Зміна")

    def section_title(severity: str) -> str:
        return {
            "critical": "Критичні зміни",
            "medium": "Потребують перевірки",
            "low": "Інші зміни",
        }.get(severity, "Інші зміни")

    def kpi_cell(label_text: str, value: int, text_color: str, bg: str = "#ffffff") -> str:
        return f"""
        <td style="padding:6px;">
          <table width="100%" cellpadding="0" cellspacing="0" style="background:{bg};border:1px solid #e5e7eb;border-radius:14px;">
            <tr>
              <td style="padding:14px;font-family:Arial,Helvetica,sans-serif;">
                <div style="font-size:26px;line-height:30px;font-weight:800;color:{text_color};">{value}</div>
                <div style="font-size:12px;line-height:17px;color:#667085;">{esc(label_text)}</div>
              </td>
            </tr>
          </table>
        </td>
        """

    def dashboard_button() -> str:
        if not dashboard_url:
            return ""

        return f"""
        <table cellpadding="0" cellspacing="0" style="margin:18px 0 4px;">
          <tr>
            <td bgcolor="#172033" style="border-radius:12px;">
              <a href="{esc(dashboard_url)}" style="display:inline-block;padding:13px 18px;color:#ffffff;text-decoration:none;font-family:Arial,Helvetica,sans-serif;font-weight:700;font-size:14px;">
                Переглянути повні дані в dashboard
              </a>
            </td>
          </tr>
        </table>
        """

    def info_row(label_text: str, value: Any) -> str:
        return f"""
        <tr>
          <td width="145" style="padding:6px 8px 6px 0;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:18px;color:#667085;font-weight:700;vertical-align:top;">
            {esc(label_text)}
          </td>
          <td style="padding:6px 0;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:18px;color:#344054;vertical-align:top;">
            {esc(value or "—")}
          </td>
        </tr>
        """

    def render_card(ch: dict[str, Any]) -> str:
        sev = ch.get("severity", "low")
        c = color(sev)
        bg = light_bg(sev)
        details = truncate(ch.get("details"), 700)

        details_block = ""
        if details:
            details_block = info_row("Деталі", details)

        technical = []

        if ch.get("record"):
            technical.append(f"RECORD: {ch.get('record')}")

        if ch.get("detected_at"):
            technical.append(f"Виявлено: {ch.get('detected_at')}")

        technical_block = ""
        if technical:
            technical_block = f"""
            <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:17px;color:#667085;margin-top:10px;border-top:1px solid #e5e7eb;padding-top:10px;">
              {esc(" · ".join(technical))}
            </div>
            """

        return f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #e5e7eb;border-left:6px solid {c};border-radius:14px;margin:0 0 12px;">
          <tr>
            <td style="padding:16px 18px;font-family:Arial,Helvetica,sans-serif;">

              <div style="margin-bottom:10px;">
                <span style="display:inline-block;background:{bg};color:{c};font-size:11px;line-height:15px;font-weight:800;letter-spacing:.03em;text-transform:uppercase;padding:5px 9px;border-radius:999px;">
                  {esc(label(sev))} · {esc(change_label(ch.get("change_type", "")))}
                </span>
              </div>

              <div style="font-size:17px;line-height:23px;font-weight:800;color:#172033;">
                {esc(ch.get("name") or ch.get("watch_name") or "Без назви")}
              </div>

              <div style="font-size:12px;line-height:18px;color:#667085;margin-top:3px;">
                ЄДРПОУ: {esc(ch.get("edrpou"))} · {esc(ch.get("opf") or "—")}
              </div>

              <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:12px;border-top:1px solid #e5e7eb;">
                {info_row("Було", ch.get("old_value") or "—")}
                {info_row("Стало", ch.get("new_value") or "—")}
                {info_row("Поточний стан", ch.get("stan") or ch.get("new_value") or "—")}
                {details_block}
              </table>

              {technical_block}

            </td>
          </tr>
        </table>
        """

    grouped = {
        "critical": [x for x in changes if x.get("severity") == "critical"],
        "medium": [x for x in changes if x.get("severity") == "medium"],
        "low": [x for x in changes if x.get("severity") == "low"],
    }

    sections = ""

    for severity in ["critical", "medium", "low"]:
        items = grouped[severity]

        if not items:
            continue

        sections += f"""
        <div style="font-family:Arial,Helvetica,sans-serif;font-size:18px;line-height:24px;font-weight:800;color:#172033;margin:22px 0 12px;">
          {esc(section_title(severity))}
        </div>
        """

        for ch in items[:30]:
            sections += render_card(ch)

        if len(items) > 30:
            sections += f"""
            <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:19px;color:#667085;margin:4px 0 16px;">
              Показано 30 з {len(items)}. Повний перелік — у dashboard.
            </div>
            """

    if not sections:
        sections = """
        <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;margin:16px 0;">
          <tr>
            <td style="padding:16px;font-family:Arial,Helvetica,sans-serif;color:#344054;font-size:14px;line-height:20px;">
              За поточний прогін змін не виявлено.
            </td>
          </tr>
        </table>
        """

    source_text = (
        source.get("source_label")
        or source.get("last_modified")
        or source.get("etag")
        or "оновлення джерела перевірено"
    )

    total = int(summary.get("total", 0) or 0)
    summary_line = (
        f"Виявлено {total} змін(у) за поточний прогін."
        if total
        else "За поточний прогін змін не виявлено."
    )

    return f"""
<!doctype html>
<html>
<body style="margin:0;background:#f5f7fb;padding:24px 0;">
  <center>
    <table width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:680px;">
      <tr>
        <td style="padding:0 16px;">

          <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #e5e7eb;border-radius:18px;">
            <tr>
              <td style="padding:24px;font-family:Arial,Helvetica,sans-serif;">
                <div style="font-size:12px;line-height:16px;letter-spacing:.14em;text-transform:uppercase;font-weight:800;color:#667085;">
                  Моніторинг ЄДРПОУ
                </div>

                <div style="font-size:26px;line-height:32px;font-weight:800;color:#172033;margin-top:8px;">
                  Звіт про зміни
                </div>

                <div style="font-size:14px;line-height:20px;color:#344054;margin-top:8px;">
                  {esc(summary_line)}
                </div>

                <div style="font-size:12px;line-height:18px;color:#98a2b3;margin-top:8px;">
                  {esc(generated_at)}<br>
                  Джерело: {esc(source_text)}
                </div>

                {dashboard_button()}
              </td>
            </tr>
          </table>

          <table width="100%" cellpadding="0" cellspacing="0" style="margin:14px 0;">
            <tr>
              {kpi_cell("Критичні зміни", summary.get("critical", 0), "#b42318", "#fffafa")}
              {kpi_cell("Потребують перевірки", summary.get("medium", 0), "#b54708", "#fffdf5")}
              {kpi_cell("Інші", summary.get("low", 0), "#175cd3", "#fbfdff")}
              {kpi_cell("Усього", summary.get("total", 0), "#172033", "#ffffff")}
            </tr>
          </table>

          {sections}

          {dashboard_button()}

          <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;color:#667085;margin-top:22px;">
            Це автоматичний звіт про зміни у поточному snapshot ЄДР.
            Повні дані та контекст записів доступні у dashboard.
          </div>

        </td>
      </tr>
    </table>
  </center>
</body>
</html>
"""


def build_plain(doc: dict[str, Any], dashboard_url: str) -> str:
    lines = [
        "Моніторинг ЄДРПОУ",
        f"Звіт про зміни · {doc.get('generated_at', '')}",
        "",
    ]

    s = doc.get("summary", {})

    lines += [
        f"Критичні: {s.get('critical', 0)}",
        f"Потребують перевірки: {s.get('medium', 0)}",
        f"Інші: {s.get('low', 0)}",
        f"Усього: {s.get('total', 0)}",
        "",
    ]

    changes = doc.get("changes", [])

    if not changes:
        lines.append("За поточний прогін змін не виявлено.")
    else:
        for ch in changes[:60]:
            lines.append(f"- [{ch.get('severity')}] {ch.get('edrpou')} {ch.get('name') or ch.get('watch_name')}")
            lines.append(f"  Тип: {ch.get('change_type')}")

            if ch.get("record"):
                lines.append(f"  RECORD: {ch.get('record')}")

            lines.append(f"  Було: {ch.get('old_value') or '—'}")
            lines.append(f"  Стало: {ch.get('new_value') or '—'}")

            if ch.get("details"):
                lines.append(f"  Деталі: {normalize_ws(ch.get('details'))[:700]}")

            lines.append("")

    if dashboard_url:
        lines.append(f"Dashboard: {dashboard_url}")

    return "\n".join(lines)


def split_addresses(raw: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,;]", raw or "") if x.strip()]


def format_from(raw: str, default_name: str = "Моніторинг ЄДРПОУ") -> str:
    name, addr = parseaddr(raw)

    if not addr:
        addr = raw.strip()

    display_name = name or default_name

    if display_name and addr:
        return formataddr((str(Header(display_name, "utf-8")), addr))

    return addr or raw


def send_email(doc: dict[str, Any]) -> None:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    email_from = os.getenv("EMAIL_FROM", smtp_user).strip()
    envelope_from = parseaddr(email_from)[1] or smtp_user
    email_to = split_addresses(os.getenv("EMAIL_TO", ""))
    dashboard_url = os.getenv("DASHBOARD_URL", "").strip()

    if not smtp_host or not smtp_user or not smtp_pass or not email_from or not email_to:
        print("Email is not configured. Skipping email.")
        return

    s = doc.get("summary", {})

    subject = (
        "Моніторинг ЄДРПОУ: зміни; "
        f"критичні {s.get('critical', 0)}, "
        f"усього {s.get('total', 0)}"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = format_from(email_from)
    msg["To"] = ", ".join(email_to)

    plain_body = build_plain(doc, dashboard_url)
    html_body = build_email_html(doc, dashboard_url)

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(envelope_from, email_to, msg.as_string())

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

    configured_source_url = os.getenv("EDR_UO_URL", "").strip() or DEFAULT_UO_URL
    old_source = load_json(SOURCE_STATE, {})
    source_url, new_source = resolve_source(configured_source_url, old_source)

    changed = source_changed(old_source, new_source)
    email_force = env_bool("EMAIL_FORCE", False)
    send_no_changes = env_bool("EMAIL_SEND_NO_CHANGES", False)

    print("Source changed:", changed)
    print(json.dumps(comparable(new_source), ensure_ascii=False, indent=2))
    print(f"Resolved source URL: {source_url}")
    print(f"Source kind: {new_source.get('source_kind', 'unknown')}")

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
