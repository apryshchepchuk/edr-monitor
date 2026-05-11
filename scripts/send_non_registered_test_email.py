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

def truncate(value: Any, limit: int = 360) -> str:
    text = normalize_ws(value)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def first_date_from_text(value: Any) -> str:
    text = str(value or "")
    match = re.search(r"\b\d{2}\.\d{2}\.\d{4}\b", text)
    return match.group(0) if match else ""


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = normalize_ws(value)
        if text:
            return text
    return ""


def relevant_status_block(company: dict[str, Any], group: str) -> str:
    if group == "termination":
        return first_non_empty(
            company.get("termination_started_info"),
            company.get("stan"),
        )

    if group == "terminated":
        return first_non_empty(
            company.get("terminated_info"),
            company.get("stan"),
        )

    if group == "bankruptcy":
        return first_non_empty(
            company.get("bankruptcy_readjustment_info"),
            company.get("stan"),
        )

    return first_non_empty(
        company.get("termination_started_info"),
        company.get("terminated_info"),
        company.get("bankruptcy_readjustment_info"),
        company.get("registration_info"),
        company.get("stan"),
    )


def relevant_date(company: dict[str, Any], group: str) -> str:
    block = relevant_status_block(company, group)
    date = first_date_from_text(block)
    if date:
        return date

    return first_non_empty(
        first_date_from_text(company.get("registration_info")),
        first_date_from_text(company.get("termination_started_info")),
        first_date_from_text(company.get("terminated_info")),
        first_date_from_text(company.get("bankruptcy_readjustment_info")),
    )


def short_signers(company: dict[str, Any], limit: int = 2) -> tuple[list[str], int]:
    signers = company.get("signers") or []
    if not isinstance(signers, list):
        return [], 0

    clean = [normalize_ws(x) for x in signers if normalize_ws(x)]
    return clean[:limit], max(0, len(clean) - limit)


def risk_title(group: str) -> str:
    return {
        "termination": "В стані припинення",
        "terminated": "Припинено",
        "bankruptcy": "Банкрутство / санація",
        "not_found": "Не знайдено у джерелі",
        "other": "Інший стан",
    }.get(group, "Стан відмінний від «зареєстровано»")


def section_title(severity: str) -> str:
    return {
        "critical": "Критичні стани",
        "medium": "Потребують перевірки",
        "low": "Інші стани",
    }.get(severity, "Інші стани")

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
        status_block = relevant_status_block(company, group)
        selected_signers, hidden_signers_count = short_signers(company)

        technical_notes: list[str] = []

        if company.get("selected_from_duplicate_records"):
            technical_notes.append(
                f"Запис обрано з {company.get('candidate_count')} кандидатів. "
                f"Причина вибору: {company.get('selection_reason') or '—'}"
            )

        if company.get("record"):
            technical_notes.append(f"RECORD: {company.get('record')}")

        items.append(
            {
                "severity": severity,
                "state_group": group,
                "state_label": state_label(group),
                "risk_title": risk_title(group),
                "edrpou": normalize_code(company.get("edrpou")),
                "record": company.get("record", ""),
                "name": company.get("name") or company.get("watch_name") or "Без назви",
                "watch_name": company.get("watch_name", ""),
                "opf": company.get("opf", ""),
                "stan": company.get("stan") or "—",
                "status_date": relevant_date(company, group) or "—",
                "action_summary": truncate(status_block, 520) or "—",
                "signers_short": selected_signers,
                "hidden_signers_count": hidden_signers_count,
                "technical_notes": technical_notes,
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
    source_text = source.get("last_modified") or source.get("etag") or "поточний snapshot"

    summary_line = (
        f"Виявлено {summary.get('total', 0)} контрагент(ів) "
        "зі станом, відмінним від «зареєстровано»."
    )

    def kpi_cell(label: str, value: int, color: str, bg: str = "#ffffff") -> str:
        return f"""
        <td style="padding:6px;">
          <table width="100%" cellpadding="0" cellspacing="0" style="background:{bg};border:1px solid #e5e7eb;border-radius:14px;">
            <tr>
              <td style="padding:14px;font-family:Arial,Helvetica,sans-serif;">
                <div style="font-size:26px;line-height:30px;font-weight:800;color:{color};">{value}</div>
                <div style="font-size:12px;line-height:17px;color:#667085;">{esc(label)}</div>
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

    def info_row(label: str, value: Any) -> str:
        return f"""
        <tr>
          <td width="150" style="padding:6px 8px 6px 0;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:18px;color:#667085;font-weight:700;vertical-align:top;">
            {esc(label)}
          </td>
          <td style="padding:6px 0;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:18px;color:#344054;vertical-align:top;">
            {esc(value or "—")}
          </td>
        </tr>
        """

    def render_signers(item: dict[str, Any]) -> str:
        signers = item.get("signers_short") or []
        hidden = int(item.get("hidden_signers_count") or 0)

        if not signers:
            return "—"

        text = "; ".join(signers)
        if hidden > 0:
            text += f"; + ще {hidden} у dashboard"

        return text

    def render_card(item: dict[str, Any]) -> str:
        color = color_for_severity(item["severity"])
        light_bg = {
            "critical": "#fef3f2",
            "medium": "#fffaeb",
            "low": "#eff8ff",
        }.get(item["severity"], "#f8fafc")

        technical = ""
        if item.get("technical_notes"):
            technical = f"""
            <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:17px;color:#667085;margin-top:10px;border-top:1px solid #e5e7eb;padding-top:10px;">
              {esc(" · ".join(item["technical_notes"]))}
            </div>
            """

        return f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #e5e7eb;border-left:6px solid {color};border-radius:14px;margin:0 0 12px;">
          <tr>
            <td style="padding:16px 18px;font-family:Arial,Helvetica,sans-serif;">

              <div style="margin-bottom:10px;">
                <span style="display:inline-block;background:{light_bg};color:{color};font-size:11px;line-height:15px;font-weight:800;letter-spacing:.03em;text-transform:uppercase;padding:5px 9px;border-radius:999px;">
                  {esc(label_for_severity(item["severity"]))} · {esc(item["risk_title"])}
                </span>
              </div>

              <div style="font-size:17px;line-height:23px;font-weight:800;color:#172033;">
                {esc(item["name"])}
              </div>

              <div style="font-size:12px;line-height:18px;color:#667085;margin-top:3px;">
                ЄДРПОУ: {esc(item["edrpou"])} · {esc(item["opf"] or "—")}
              </div>

              <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:12px;border-top:1px solid #e5e7eb;">
                {info_row("Поточний стан", item.get("stan"))}
                {info_row("Дата/орієнтир", item.get("status_date"))}
                {info_row("Суть запису", item.get("action_summary"))}
                {info_row("Керівник/підписанти", render_signers(item))}
              </table>

              {technical}

            </td>
          </tr>
        </table>
        """

    grouped = {
        "critical": [x for x in items if x.get("severity") == "critical"],
        "medium": [x for x in items if x.get("severity") == "medium"],
        "low": [x for x in items if x.get("severity") == "low"],
    }

    sections = ""

    for severity in ["critical", "medium", "low"]:
        group_items = grouped[severity]
        if not group_items:
            continue

        sections += f"""
        <div style="font-family:Arial,Helvetica,sans-serif;font-size:18px;line-height:24px;font-weight:800;color:#172033;margin:22px 0 12px;">
          {esc(section_title(severity))}
        </div>
        """

        for item in group_items:
            sections += render_card(item)

    if not sections:
        sections = """
        <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;margin:16px 0;">
          <tr>
            <td style="padding:16px;font-family:Arial,Helvetica,sans-serif;color:#344054;font-size:14px;line-height:20px;">
              У поточному snapshot немає контрагентів зі станом, відмінним від «зареєстровано».
            </td>
          </tr>
        </table>
        """

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
                  Звіт про ризикові стани
                </div>

                <div style="font-size:14px;line-height:20px;color:#344054;margin-top:8px;">
                  {esc(summary_line)}
                </div>

                <div style="font-size:12px;line-height:18px;color:#98a2b3;margin-top:8px;">
                  Тестова розсилка · {esc(generated_at)}<br>
                  Джерело: {esc(source_text)}
                </div>

                {dashboard_button()}
              </td>
            </tr>
          </table>

          <table width="100%" cellpadding="0" cellspacing="0" style="margin:14px 0;">
            <tr>
              {kpi_cell("Критичні стани", summary.get("critical", 0), "#b42318", "#fffafa")}
              {kpi_cell("Потребують перевірки", summary.get("medium", 0), "#b54708", "#fffdf5")}
              {kpi_cell("Інші", summary.get("low", 0), "#175cd3", "#fbfdff")}
              {kpi_cell("Усього", summary.get("total", 0), "#172033", "#ffffff")}
            </tr>
          </table>

          {sections}

          {dashboard_button()}

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
        f"Звіт про ризикові стани · {generated_at}",
        "",
        f"Виявлено {summary.get('total', 0)} контрагент(ів) зі станом, відмінним від «зареєстровано».",
        "",
        f"Критичні: {summary.get('critical', 0)}",
        f"Потребують перевірки: {summary.get('medium', 0)}",
        f"Інші: {summary.get('low', 0)}",
        f"Усього: {summary.get('total', 0)}",
        "",
    ]

    if not items:
        lines.append("У поточному snapshot немає контрагентів зі станом, відмінним від «зареєстровано».")
    else:
        for item in items:
            lines.append(f"- [{item['severity']}] {item['edrpou']} {item['name']}")
            lines.append(f"  Стан: {item.get('stan') or '—'}")
            lines.append(f"  Дата/орієнтир: {item.get('status_date') or '—'}")
            lines.append(f"  Суть запису: {item.get('action_summary') or '—'}")
            if item.get("record"):
                lines.append(f"  RECORD: {item.get('record')}")
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
    "Моніторинг ЄДРПОУ: ризикові стани; "
    f"критичні {summary.get('critical', 0)}, "
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
