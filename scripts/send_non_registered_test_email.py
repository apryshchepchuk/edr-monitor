from __future__ import annotations

import html
import json
import os
import re
import smtplib
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]

CURRENT = ROOT / "data" / "current.json"
DASHBOARD = ROOT / "docs" / "data" / "dashboard.json"


def tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))


def now_iso() -> str:
    return datetime.now(tz()).isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def normalize_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_code(value: Any) -> str:
    return re.sub(r"\D", "", str(value or "")).strip()


def split_addresses(raw: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,;]", raw or "") if x.strip()]


def format_from(raw: str) -> str:
    name, addr = parseaddr(raw)
    if name and addr:
        return formataddr((str(Header(name, "utf-8")), addr))
    return raw


def state_group(company: dict[str, Any]) -> str:
    stan = str(company.get("stan") or "").lower()

    if company.get("found") is False or "не знайдено" in stan:
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


def state_label(group: str) -> str:
    return {
        "registered": "Зареєстровано",
        "termination": "В стані припинення",
        "terminated": "Припинено",
        "bankruptcy": "Банкрутство",
        "not_found": "Не знайдено",
        "other": "Інший стан",
    }.get(group, group or "—")


def severity_for_state(group: str) -> str:
    if group in {"termination", "terminated", "bankruptcy"}:
        return "critical"

    if group in {"not_found", "other"}:
        return "medium"

    return "low"


def color_for_severity(severity: str) -> str:
    return {
        "critical": "#b42318",
        "medium": "#b54708",
        "low": "#175cd3",
    }.get(severity, "#175cd3")


def label_for_severity(severity: str) -> str:
    return {
        "critical": "Критично",
        "medium": "Середньо",
        "low": "Низько",
    }.get(severity, severity or "—")


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def load_companies_doc() -> dict[str, Any]:
    current_doc = load_json(CURRENT, {})

    if isinstance(current_doc, dict) and current_doc.get("companies"):
        return current_doc

    dashboard_doc = load_json(DASHBOARD, {})

    if isinstance(dashboard_doc, dict) and dashboard_doc.get("companies"):
        return dashboard_doc

    raise RuntimeError(
        "Не знайдено даних для тестової розсилки. "
        "Спочатку запустіть основний EDRPOU Monitor, щоб сформувати data/current.json."
    )


def build_test_items(companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    for company in companies:
        if not isinstance(company, dict):
            continue

        group = state_group(company)

        if group == "registered":
            continue

        severity = severity_for_state(group)

        details: list[str] = [
            f"Група стану: {state_label(group)}",
        ]

        if company.get("record"):
            details.append(f"RECORD: {company.get('record')}")

        if company.get("selected_from_duplicate_records"):
            details.append(
                f"Запис обрано з {company.get('candidate_count')} кандидатів. "
                f"Причина вибору: {company.get('selection_reason') or '—'}"
            )

        if company.get("registration_info"):
            details.append(f"Реєстрація: {company.get('registration_info')}")

        if company.get("termination_started_info"):
            details.append(f"Початок припинення: {company.get('termination_started_info')}")

        if company.get("terminated_info"):
            details.append(f"Припинення: {company.get('terminated_info')}")

        if company.get("termination_cancel_info"):
            details.append(f"Скасування припинення: {company.get('termination_cancel_info')}")

        if company.get("bankruptcy_readjustment_info"):
            details.append(f"Банкрутство / санація: {company.get('bankruptcy_readjustment_info')}")

        signers = company.get("signers") or []
        if signers:
            details.append(f"Керівник/підписанти: {'; '.join(signers[:5])}")

        items.append(
            {
                "severity": severity,
                "state_group": group,
                "state_label": state_label(group),
                "edrpou": normalize_code(company.get("edrpou")),
                "record": company.get("record", ""),
                "name": company.get("name") or company.get("watch_name") or "Без назви",
                "watch_name": company.get("watch_name", ""),
                "opf": company.get("opf", ""),
                "stan": company.get("stan") or "—",
                "details": "\n".join(details),
            }
        )

    order = {"critical": 0, "medium": 1, "low": 2}
    return sorted(items, key=lambda x: (order.get(x["severity"], 9), x["name"]))


def summarize(items: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "critical": 0,
        "medium": 0,
        "low": 0,
        "total": len(items),
    }

    for item in items:
        severity = item.get("severity")
        if severity in summary:
            summary[severity] += 1

    return summary


def build_email_html(
    items: list[dict[str, Any]],
    summary: dict[str, int],
    generated_at: str,
    source: dict[str, Any],
    dashboard_url: str,
) -> str:
    cards = ""

    for item in items:
        color = color_for_severity(item["severity"])

        cards += f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #e5e7eb;border-left:6px solid {color};border-radius:14px;margin:0 0 12px;">
          <tr>
            <td style="padding:16px;font-family:Arial,Helvetica,sans-serif;">
              <div style="font-size:16px;font-weight:800;color:#172033;">
                {esc(item["name"])}
              </div>

              <div style="font-size:12px;color:#667085;margin-top:3px;">
                ЄДРПОУ: {esc(item["edrpou"])} · RECORD: {esc(item["record"] or "—")} · {esc(item["opf"])}
              </div>

              <div style="margin-top:8px;">
                <span style="display:inline-block;background:#f5f7fb;color:{color};font-size:12px;font-weight:700;padding:4px 9px;border-radius:999px;">
                  {esc(label_for_severity(item["severity"]))}
                </span>
                <span style="display:inline-block;background:#f2f4f7;color:#344054;font-size:12px;font-weight:700;padding:4px 9px;border-radius:999px;">
                  {esc(item["state_label"])}
                </span>
              </div>

              <div style="font-size:13px;line-height:19px;color:#344054;margin-top:10px;">
                <b>Поточний стан:</b> {esc(item["stan"])}
              </div>

              <div style="font-size:13px;line-height:19px;color:#344054;margin-top:10px;white-space:pre-wrap;">
                {esc(item["details"])}
              </div>
            </td>
          </tr>
        </table>
        """

    if not cards:
        cards = """
        <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;margin:16px 0;">
          <tr>
            <td style="padding:16px;font-family:Arial,Helvetica,sans-serif;color:#344054;">
              У поточному snapshot немає контрагентів зі станом, відмінним від «зареєстровано».
            </td>
          </tr>
        </table>
        """

    button = ""
    if dashboard_url:
        button = f"""
        <table cellpadding="0" cellspacing="0" style="margin-top:18px;">
          <tr>
            <td bgcolor="#175cd3" style="border-radius:12px;">
              <a href="{esc(dashboard_url)}" style="display:inline-block;padding:12px 18px;color:#ffffff;text-decoration:none;font-family:Arial,Helvetica,sans-serif;font-weight:700;">
                Переглянути dashboard
              </a>
            </td>
          </tr>
        </table>
        """

    source_text = source.get("last_modified") or source.get("etag") or "поточний snapshot"

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
                <div style="font-size:26px;font-weight:800;color:#172033;">
                  Моніторинг ЄДРПОУ
                </div>
                <div style="font-size:14px;color:#667085;margin-top:6px;">
                  Тестова розсилка: стани відмінні від «зареєстровано» · {esc(generated_at)}
                </div>
                <div style="font-size:12px;color:#98a2b3;margin-top:6px;">
                  Джерело: {esc(source_text)}
                </div>
              </td>
            </tr>
          </table>

          <table width="100%" cellpadding="0" cellspacing="0" style="margin:14px 0;">
            <tr>
              <td style="padding:6px;">
                <div style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:14px;font-family:Arial;">
                  <b style="font-size:24px;color:#b42318;">{summary.get("critical", 0)}</b><br>
                  <span style="font-size:12px;color:#667085;">Критичні</span>
                </div>
              </td>
              <td style="padding:6px;">
                <div style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:14px;font-family:Arial;">
                  <b style="font-size:24px;color:#b54708;">{summary.get("medium", 0)}</b><br>
                  <span style="font-size:12px;color:#667085;">Середні</span>
                </div>
              </td>
              <td style="padding:6px;">
                <div style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:14px;font-family:Arial;">
                  <b style="font-size:24px;color:#175cd3;">{summary.get("low", 0)}</b><br>
                  <span style="font-size:12px;color:#667085;">Низькі</span>
                </div>
              </td>
              <td style="padding:6px;">
                <div style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:14px;font-family:Arial;">
                  <b style="font-size:24px;color:#172033;">{summary.get("total", 0)}</b><br>
                  <span style="font-size:12px;color:#667085;">Усього</span>
                </div>
              </td>
            </tr>
          </table>

          {cards}

          {button}

          <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;color:#667085;margin-top:22px;">
            Це тестова розсилка по поточному snapshot. Вона не є автоматичним alert про нові зміни.
            Джерело даних: відкриті дані ЄДР з порталу data.gov.ua.
          </div>
        </td>
      </tr>
    </table>
  </center>
</body>
</html>
"""


def build_plain(
    items: list[dict[str, Any]],
    summary: dict[str, int],
    generated_at: str,
    dashboard_url: str,
) -> str:
    lines = [
        "Моніторинг ЄДРПОУ",
        f"Тестова розсилка: стани відмінні від «зареєстровано» · {generated_at}",
        "",
        f"Критичні: {summary.get('critical', 0)}",
        f"Середні: {summary.get('medium', 0)}",
        f"Низькі: {summary.get('low', 0)}",
        f"Усього: {summary.get('total', 0)}",
        "",
    ]

    if not items:
        lines.append("У поточному snapshot немає контрагентів зі станом, відмінним від «зареєстровано».")
    else:
        for item in items:
            lines.append(f"- [{item['severity']}] {item['edrpou']} {item['name']}")
            lines.append(f"  RECORD: {item.get('record') or '—'}")
            lines.append(f"  Стан: {item.get('stan') or '—'}")
            if item.get("details"):
                lines.append(f"  {item['details']}")
            lines.append("")

    if dashboard_url:
        lines.append(f"Dashboard: {dashboard_url}")

    return "\n".join(lines)


def send_email(
    items: list[dict[str, Any]],
    summary: dict[str, int],
    generated_at: str,
    source: dict[str, Any],
) -> None:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    email_from = os.getenv("EMAIL_FROM", smtp_user).strip()
    email_to = split_addresses(os.getenv("EMAIL_TO", ""))
    dashboard_url = os.getenv("DASHBOARD_URL", "").strip()

    if not smtp_host or not smtp_user or not smtp_pass or not email_from or not email_to:
        raise RuntimeError(
            "Email is not configured. Required: SMTP_HOST, SMTP_PORT, SMTP_USER, "
            "SMTP_PASS, EMAIL_FROM, EMAIL_TO."
        )

    subject = (
        "Моніторинг ЄДРПОУ: тест станів; "
        f"критичні {summary.get('critical', 0)}, "
        f"середні {summary.get('medium', 0)}, "
        f"усього {summary.get('total', 0)}"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = format_from(email_from)
    msg["To"] = ", ".join(email_to)

    msg.attach(MIMEText(build_plain(items, summary, generated_at, dashboard_url), "plain", "utf-8"))
    msg.attach(MIMEText(build_email_html(items, summary, generated_at, source, dashboard_url), "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(email_from, email_to, msg.as_string())

    print(f"Email sent to {', '.join(email_to)}")


def main() -> None:
    doc = load_companies_doc()
    companies = doc.get("companies", [])

    if not isinstance(companies, list):
        raise RuntimeError("Invalid snapshot: companies must be a list.")

    items = build_test_items(companies)
    summary = summarize(items)
    generated_at = now_iso()
    source = doc.get("source", {}) if isinstance(doc.get("source"), dict) else {}

    print(f"Companies in snapshot: {len(companies)}")
    print(f"Non-registered companies selected: {len(items)}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    send_email(items, summary, generated_at, source)


if __name__ == "__main__":
    main()
