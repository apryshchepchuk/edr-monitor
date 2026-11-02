"""One-shot EDR -> LIGA360 company-code matcher.

This replaces the old EDR change/notification workflow, intentionally keeping only
source downloading, streaming UO XML parsing, and careful identity matching.

Inputs:
    config/edrpou_codes.txt       One code per line, ALWAYS read as text
    config/liga_companies.csv     LigaId,LigaName,SourceUrl
Outputs:
    data/edrpou_names.csv         Registry names for all input codes
    data/companycodes_all.csv     One line per LIGA card (auto or needs review)
    data/companycodes_review.csv  Proposed candidates for MANUAL verification
    data/match_summary.json       Machine-readable run diagnostics

A fuzzy match NEVER becomes an automatic LigaId <-> EDRPOU mapping.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin

import requests
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
OUTPUT = ROOT / "data"

PRIMARY_URL = (
    "https://data.gov.ua/dataset/03cc1239-3988-4451-aa0d-aadb77448714/"
    "resource/d40cc921-39bb-44fd-be06-dc02589f45c6/download/uo.zip"
)
NAIS_PAGE = (
    "https://nais.gov.ua/m/"
    "ediniy-derjavniy-reestr-yuridichnih-osib-fizichnih-osib-pidpriemtsiv-ta-gromadskih-formuvan"
)
DATASET = "https://data.gov.ua/dataset/03cc1239-3988-4451-aa0d-aadb77448714"
HEADERS = {
    "User-Agent": "Mozilla/5.0 EDR-LIGA-Matcher/1.0",
    "Accept": "application/zip,application/octet-stream,text/html,*/*",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.7",
}


def write_csv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: Ukrainian letters display properly in Excel; import EDRPOU as Text!
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: safe_csv(row.get(field, "")) for field in headers})


def safe_csv(value: object) -> str:
    text = str(value if value is not None else "")
    # Never allow untrusted registry names to be interpreted as spreadsheet formulas.
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def load_codes(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Missing code list: {path}")
    codes, seen = [], set()
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        code = raw.strip()
        if not code or code.startswith("#"):
            continue
        if not re.fullmatch(r"\d{1,12}", code):
            raise ValueError(f"Invalid code at line {line_no}: {code!r}")
        if code not in seen:
            codes.append(code)
            seen.add(code)
    if not codes:
        raise ValueError("Code list is empty")
    return codes


def load_liga(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing LIGA company list: {path}")
    result, seen = [], set()
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        expected = {"LigaId", "LigaName", "SourceUrl"}
        if not expected.issubset(reader.fieldnames or []):
            raise ValueError(f"LIGA CSV must have columns: {sorted(expected)}")
        for row in reader:
            liga_id = (row.get("LigaId") or "").strip().lower()
            name = (row.get("LigaName") or "").strip()
            if not liga_id or not name:
                continue
            if liga_id in seen:
                raise ValueError(f"Duplicate LigaId in input: {liga_id}")
            seen.add(liga_id)
            result.append({"LigaId": liga_id, "LigaName": name,
                           "SourceUrl": (row.get("SourceUrl") or "").strip()})
    if not result:
        raise ValueError("LIGA company CSV is empty")
    return result


def canonical_name(value: str) -> str:
    s = unicodedata.normalize("NFKC", str(value or "")).upper()
    s = re.sub(r"[’ʼ‘`´]", "'", s)
    s = s.replace("Ё", "Е")
    # Keep every letter/digit. Do not strip company-specific words or legal forms.
    return re.sub(r"[^A-ZА-ЯІЇЄҐ0-9]+", "", s)


def display_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def child_text(elem: etree._Element, field: str) -> str:
    for child in elem:
        if child.tag.rsplit("}", 1)[-1] == field:
            return display_name(" ".join(child.itertext()))
    return ""


def select_record(records: list[dict[str, str]]) -> dict[str, str]:
    # All records with the same code are retained in diagnostics.
    # Prefer the registered/live entry, never auto-approve multiple RECORD rows.
    def score(rec: dict[str, str]):
        state = rec["State"].lower()
        rank = 3 if "зареєстровано" in state else (
            2 if "в стані припинення" in state else (
                1 if "припинено" not in state else 0))
        return rank, bool(rec["OfficialName"]), rec["Record"]
    return max(records, key=score)


def first_uo_xml(zf: zipfile.ZipFile) -> str:
    xmls = [name for name in zf.namelist() if name.lower().endswith(".xml")]
    exact = [name for name in xmls if Path(name).name.lower() == "uo.xml"]
    if len(exact) == 1:
        return exact[0]
    candidates = [name for name in xmls
                  if "uo" in Path(name).name.lower()
                  and not any(tag in Path(name).name.lower() for tag in ("fop", "fsu", "schema"))]
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError(f"Cannot uniquely identify legal-entity UO.xml in ZIP. XML files: {xmls[:15]}")


@contextmanager
def zip_xml_reader(zip_path: Path, name: str):
    # The original monitor supported alternate compression codecs through 7z.
    try:
        with zipfile.ZipFile(zip_path) as archive:
            try:
                with archive.open(name) as stream:
                    yield stream
                    return
            except NotImplementedError:
                pass
    except NotImplementedError:
        pass
    process = subprocess.Popen(["7z", "x", "-so", str(zip_path), name],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        if process.stdout is None:
            raise RuntimeError("7z did not provide decompressed XML")
        yield process.stdout
    finally:
        if process.stdout:
            process.stdout.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        if process.wait() != 0:
            raise RuntimeError(f"7z extraction failed: {stderr[-1000:]}")


def parse_zip(zip_path: Path, codes: list[str]) -> tuple[dict[str, dict[str, str]], int]:
    target = set(codes)
    by_code: dict[str, list[dict[str, str]]] = defaultdict(list)
    with zipfile.ZipFile(zip_path) as archive:
        xml = first_uo_xml(archive)
    print(f"Reading ZIP {zip_path.name} | XML {xml} | target codes: {len(codes)}", flush=True)
    processed = 0
    with zip_xml_reader(zip_path, xml) as stream:
        # Namespace-agnostic (but only SUBJECT end-events are parsed).
        parser = etree.iterparse(stream, events=("end",), tag=("SUBJECT", "{*}SUBJECT"),
                                 huge_tree=True, recover=False)
        for _, elem in parser:
            processed += 1
            code = child_text(elem, "EDRPOU").strip()
            if code in target:
                by_code[code].append({
                    "EDRPOU": code,
                    "OfficialName": child_text(elem, "NAME"),
                    "ShortName": child_text(elem, "SHORT_NAME"),
                    "Record": child_text(elem, "RECORD"),
                    "State": child_text(elem, "STAN"),
                })
            parent = elem.getparent()
            elem.clear()
            if parent is not None:
                while elem.getprevious() is not None:
                    del parent[0]
            if processed % 250_000 == 0:
                print(f"Records: {processed:,} | found codes: {len(by_code)}/{len(codes)}", flush=True)
    if processed == 0:
        raise RuntimeError("XML contained no SUBJECT records; input schema may have changed")
    result = {}
    for code in codes:
        choices = by_code.get(code, [])
        if choices:
            chosen = select_record(choices)
            chosen = {**chosen, "RecordsForCode": str(len(choices)),
                      "Found": "YES", "SelectionNote": "MULTIPLE_RECORDS" if len(choices) > 1 else "SINGLE_RECORD"}
            # Ensure no silently missing name masquerades as found.
            if not chosen["OfficialName"] and not chosen["ShortName"]:
                chosen["SelectionNote"] = "NO_NAME"
        else:
            chosen = {"EDRPOU": code, "OfficialName": "", "ShortName": "", "Record": "",
                      "State": "", "Found": "NO", "RecordsForCode": "0", "SelectionNote": "NOT_FOUND"}
        result[code] = chosen
    return result, processed


def candidate_rank(liga: str, official: str, short: str) -> float:
    left = canonical_name(liga)
    return max((SequenceMatcher(None, left, canonical_name(value)).ratio()
                for value in (official, short) if canonical_name(value)), default=0.0)


def match_companies(liga: list[dict[str, str]], records: dict[str, dict[str, str]]):
    by_name: dict[str, set[str]] = defaultdict(set)
    eligible = {}
    for code, info in records.items():
        if info["Found"] != "YES" or info["SelectionNote"] != "SINGLE_RECORD":
            continue
        eligible[code] = info
        for name in (info["OfficialName"], info["ShortName"]):
            normalized = canonical_name(name)
            if normalized:
                by_name[normalized].add(code)

    proposals: dict[str, set[str]] = {}
    for row in liga:
        proposals[row["LigaId"]] = set(by_name.get(canonical_name(row["LigaName"]), set()))

    code_counts: dict[str, int] = defaultdict(int)
    for codes in proposals.values():
        for code in codes:
            code_counts[code] += 1

    all_rows, review_rows = [], []
    for company in liga:
        lid, name = company["LigaId"], company["LigaName"]
        exact = proposals[lid]
        matched = len(exact) == 1 and code_counts[next(iter(exact))] == 1
        selected_code = next(iter(exact)) if matched else ""
        if matched:
            info = eligible[selected_code]
            row = {
                **company, "EDRPOU": selected_code, "OfficialName": info["OfficialName"],
                "MatchStatus": "AUTO_EXACT", "MatchMethod": "EXACT_NAME_OR_SHORT_NAME",
                "MatchConfidence": "EXACT_UNIQUE", "Candidates": selected_code,
            }
        else:
            options = []
            for code, info in records.items():
                if info["Found"] != "YES":
                    continue
                rank = candidate_rank(name, info["OfficialName"], info["ShortName"])
                if rank >= 0.55:
                    options.append((rank, code, info["OfficialName"]))
            options.sort(reverse=True)
            candidates = sorted(exact) if exact else [x[1] for x in options[:3]]
            if len(exact) > 1:
                status = "REVIEW_AMBIGUOUS_NAME"
            elif len(exact) == 1:
                status = "REVIEW_SHARED_CODE"
            else:
                status = "REVIEW_NO_EXACT_MATCH"
            row = {
                **company, "EDRPOU": "", "OfficialName": "",
                "MatchStatus": status, "MatchMethod": "MANUAL_REVIEW_REQUIRED",
                "MatchConfidence": "NOT_VERIFIED", "Candidates": " | ".join(candidates),
            }
            review_rows.append({
                "LigaId": lid, "LigaName": name, "Reason": status, "SourceUrl": company["SourceUrl"],
                "Candidate1": format_candidate(options, exact, records, 0),
                "Candidate2": format_candidate(options, exact, records, 1),
                "Candidate3": format_candidate(options, exact, records, 2),
                "ManualEDRPOU": "", "Decision": "",
            })
        all_rows.append(row)
    return all_rows, review_rows


def format_candidate(options, exact, records, index: int) -> str:
    if exact:
        available = [(1.0, code, records[code]["OfficialName"]) for code in sorted(exact)]
    else:
        available = options[:3]
    if index >= len(available):
        return ""
    score, code, name = available[index]
    return f"{code} | {name} | similarity {score:.0%}"


def discover_nais_zip() -> str:
    print("Primary source inaccessible; attempting NAIS fallback", flush=True)
    try:
        response = requests.get(NAIS_PAGE, headers=HEADERS, timeout=(30, 90))
        response.raise_for_status()
        page = response.text
    except requests.RequestException:
        cmd = ["curl", "--http1.1", "--ipv4", "-L", "--fail", "--silent", "--show-error",
               "--max-time", "180", "--connect-timeout", "25", NAIS_PAGE]
        run = subprocess.run(cmd, text=True, capture_output=True, timeout=200, check=False)
        if run.returncode:
            raise RuntimeError(f"NAIS page unavailable: {run.stderr[-500:]}")
        page = run.stdout
    links = re.findall(r'href=["\']([^"\']+?\.zip(?:\?[^"\']*)?)["\']', page, flags=re.I)
    links += re.findall(r'(?:https://nais\.gov\.ua)?/files/general/[^"\'\s<>]+?\.zip', page, flags=re.I)
    unique_links = list({urljoin(NAIS_PAGE, link.replace("&amp;", "&")) for link in links})

    def score(url: str) -> tuple[int, str]:
        text = url.lower()
        points = 0
        if "ufopfsu" in text: points += 100  # original monitor's mixed UO/FOP/FSU archive
        if "/files/general/" in text: points += 30
        if "uo.zip" in text: points += 90
        if "schema" in text or "xsd" in text: points -= 1000
        if "fop.zip" in text or "fsu.zip" in text: points -= 1000
        return points, text

    unique_links.sort(key=score, reverse=True)
    for link in unique_links[:5]:
        print("NAIS fallback candidate:", link, flush=True)
    valid = [link for link in unique_links if score(link)[0] > 0]
    if not valid:
        raise RuntimeError("No legal-entity ZIP link found on NAIS page")
    return valid[0]


def download_zip(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        print(f"Downloading source (attempt {attempt}/2): {url}", flush=True)
        try:
            with requests.get(url, headers=HEADERS, stream=True, allow_redirects=True,
                              timeout=(45, 240)) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/html" in content_type:
                    raise RuntimeError("Server returned HTML, not a ZIP archive")
                with dest.open("wb") as file:
                    for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                        if chunk:
                            file.write(chunk)
            if not zipfile.is_zipfile(dest):
                raise RuntimeError("Downloaded file is not a ZIP archive")
            print(f"ZIP downloaded: {dest.stat().st_size / 1048576:.1f} MiB", flush=True)
            return
        except (requests.RequestException, RuntimeError, zipfile.BadZipFile) as exc:
            dest.unlink(missing_ok=True)
            if attempt == 2:
                raise RuntimeError(f"Could not download ZIP from {url}: {exc}") from exc
            print(f"Download error: {exc}", file=sys.stderr, flush=True)


def run(args: argparse.Namespace) -> None:
    codes = load_codes(Path(args.codes))
    liga = load_liga(Path(args.liga))
    invalid_lengths = [code for code in codes if len(code) != 8]
    print(f"Input: {len(codes)} unique codes, {len(liga)} LIGA cards; non-8-digit: {invalid_lengths}")
    with tempfile.TemporaryDirectory(prefix="edr_liga_") as tmp:
        if args.zip:
            archive = Path(args.zip).resolve()
            if not archive.is_file():
                raise FileNotFoundError(f"Local ZIP not found: {archive}")
            source = f"local:{archive.name}"
        else:
            archive = Path(tmp) / "uo.zip"
            primary = os.getenv("EDR_UO_URL", "").strip() or PRIMARY_URL
            try:
                download_zip(primary, archive)
                source = primary
                # If primary archive is invalid for UO, fallback will happen below.
                with zipfile.ZipFile(archive) as zf:
                    first_uo_xml(zf)
            except Exception as primary_error:
                print(f"Primary ZIP failed: {primary_error}", file=sys.stderr, flush=True)
                fallback = discover_nais_zip()
                download_zip(fallback, archive)
                source = fallback
        registry, processed = parse_zip(archive, codes)
    all_rows, review = match_companies(liga, registry)
    edr_rows = list(registry.values())
    for item in edr_rows:
        item["DataSourceURL"] = DATASET
    out = Path(args.output)
    write_csv(out / "edrpou_names.csv",
              ["EDRPOU", "OfficialName", "ShortName", "Record", "State", "Found",
               "RecordsForCode", "SelectionNote", "DataSourceURL"], edr_rows)
    write_csv(out / "companycodes_all.csv",
              ["LigaId", "LigaName", "EDRPOU", "OfficialName", "MatchStatus",
               "MatchMethod", "MatchConfidence", "Candidates", "SourceUrl"], all_rows)
    write_csv(out / "companycodes_review.csv",
              ["LigaId", "LigaName", "Reason", "SourceUrl", "Candidate1", "Candidate2",
               "Candidate3", "ManualEDRPOU", "Decision"], review)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_archive": source,
        "source_dataset": DATASET,
        "xml_subject_records_processed": processed,
        "input_edrpou_codes": len(codes),
        "nonstandard_length_codes": invalid_lengths,
        "registry_codes_found": sum(1 for x in edr_rows if x["Found"] == "YES"),
        "registry_codes_missing": sum(1 for x in edr_rows if x["Found"] == "NO"),
        "registry_codes_with_multiple_records": sum(1 for x in edr_rows if x["SelectionNote"] == "MULTIPLE_RECORDS"),
        "liga_cards": len(liga),
        "auto_exact_unique_matches": sum(1 for x in all_rows if x["MatchStatus"] == "AUTO_EXACT"),
        "needs_manual_review": len(review),
        "rule": "Only one-to-one exact normalized official/short names are automatically mapped; fuzzy suggestions never auto-mapped",
    }
    (out / "match_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\nRESULT SUMMARY:\n" + json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"Outputs saved under: {out.resolve()}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Link EDRPOU codes to LIGA360 UUIDs via official company names")
    parser.add_argument("--codes", default=str(CONFIG / "edrpou_codes.txt"))
    parser.add_argument("--liga", default=str(CONFIG / "liga_companies.csv"))
    parser.add_argument("--output", default=str(OUTPUT))
    parser.add_argument("--zip", help="Optional existing official UO ZIP for local/offline run")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
