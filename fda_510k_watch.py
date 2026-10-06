#!/usr/bin/env python3
"""Daily watcher for new FDA 510(k) clearances from a list of companies.

Flow per run:
  1. Query openFDA's 510(k) endpoint for each company in companies.json.
  2. Diff against state/seen.json to find K numbers not processed before.
  3. Download the 510(k) summary PDF (if one exists) into summaries/<company>/.
  4. Retry any PDFs that were missing on earlier runs.
  5. Notify (Slack webhook or email) about new clearances, then save state.

Usage:
  python fda_510k_watch.py              # normal daily run
  python fda_510k_watch.py --backfill   # first run: record everything, no alerts
  python fda_510k_watch.py --dry-run    # query + diff only, write nothing
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
import time
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
COMPANIES_FILE = ROOT / "companies.json"
STATE_FILE = ROOT / "state" / "seen.json"
SUMMARIES_DIR = ROOT / "summaries"

OPENFDA_URL = "https://api.fda.gov/device/510k.json"
PDF_URL_TEMPLATE = "https://www.accessdata.fda.gov/cdrh_docs/pdf{yy}/{k_number}.pdf"
# De Novo decision summaries live under a different path than 510(k) summaries.
DEN_PDF_URL_TEMPLATE = "https://www.accessdata.fda.gov/cdrh_docs/reviews/{k_number}.pdf"
FDA_DETAIL_URL = "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID={k_number}"
DEN_DETAIL_URL = "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/denovo.cfm?ID={k_number}"

# accessdata.fda.gov sits behind Akamai, which rejects non-browser user agents.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
TIMEOUT = 30

log = logging.getLogger("fda_510k_watch")


# ----------------------------------------------------------------------------
# Config and state
# ----------------------------------------------------------------------------

def load_companies() -> list[dict]:
    with COMPANIES_FILE.open() as f:
        companies = json.load(f)
    for c in companies:
        if "name" not in c or "applicant_query" not in c:
            raise ValueError(f"companies.json entry missing name/applicant_query: {c}")
    return companies


def load_state() -> dict:
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f)
    return {"seen": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(STATE_FILE)


# ----------------------------------------------------------------------------
# openFDA
# ----------------------------------------------------------------------------

def query_openfda(applicant_query: str, session: requests.Session) -> list[dict]:
    """Return every 510(k) record whose applicant matches the query."""
    params = {
        "search": f'applicant:"{applicant_query}"',
        "sort": "decision_date:desc",
        "limit": 100,
        "skip": 0,
    }
    api_key = os.environ.get("OPENFDA_API_KEY")
    if api_key:
        params["api_key"] = api_key

    results: list[dict] = []
    while True:
        resp = session.get(OPENFDA_URL, params=params, timeout=TIMEOUT)
        if resp.status_code == 404:
            # openFDA returns 404 when a search matches nothing.
            break
        resp.raise_for_status()
        body = resp.json()
        page = body.get("results", [])
        results.extend(page)
        total = body.get("meta", {}).get("results", {}).get("total", 0)
        params["skip"] += len(page)
        if not page or params["skip"] >= total:
            break
    return results


def record_metadata(rec: dict, company: str) -> dict:
    k = rec["k_number"]
    openfda = rec.get("openfda", {}) or {}
    return {
        "k_number": k,
        "company": company,
        "applicant": rec.get("applicant"),
        "device_name": rec.get("device_name"),
        "product_code": rec.get("product_code"),
        "decision_date": rec.get("decision_date"),
        "date_received": rec.get("date_received"),
        "decision_description": rec.get("decision_description"),
        "clearance_type": rec.get("clearance_type"),
        "statement_or_summary": rec.get("statement_or_summary"),
        "regulation_number": openfda.get("regulation_number"),
        "device_class": openfda.get("device_class"),
        "generic_device_name": openfda.get("device_name"),
        "fda_detail_url": detail_url(k),
        "summary_pdf_url": pdf_url(k),
        "expects_pdf": expects_pdf(rec),
        "raw": rec,
    }


# ----------------------------------------------------------------------------
# Summary PDFs
# ----------------------------------------------------------------------------

def is_de_novo(k_number: str) -> bool:
    return k_number.upper().startswith("DEN")


def pdf_url(k_number: str) -> str:
    if is_de_novo(k_number):
        return DEN_PDF_URL_TEMPLATE.format(k_number=k_number)
    # K261855 -> pdf26/K261855.pdf ; the folder is the 2-digit year in the K number.
    return PDF_URL_TEMPLATE.format(yy=k_number[1:3], k_number=k_number)


def detail_url(k_number: str) -> str:
    tmpl = DEN_DETAIL_URL if is_de_novo(k_number) else FDA_DETAIL_URL
    return tmpl.format(k_number=k_number)


def expects_pdf(rec: dict) -> bool:
    """True when the FDA should have posted a summary document for this record."""
    if is_de_novo(rec["k_number"]):
        return True  # De Novo grants always get a decision summary
    return rec.get("statement_or_summary") == "Summary"


def pdf_path(company: str, k_number: str) -> Path:
    return SUMMARIES_DIR / company / f"{k_number}.pdf"


def download_pdf(company: str, k_number: str, session: requests.Session) -> bool:
    """Download the summary PDF. Returns True on success, False if not yet posted."""
    dest = pdf_path(company, k_number)
    if dest.exists() and dest.stat().st_size > 0:
        return True

    url = pdf_url(k_number)
    resp = session.get(url, headers={"User-Agent": BROWSER_UA}, timeout=TIMEOUT, allow_redirects=True)
    ctype = resp.headers.get("content-type", "")
    if resp.status_code == 404:
        log.info("%s: summary PDF not posted yet (404)", k_number)
        return False
    if resp.status_code != 200 or "pdf" not in ctype.lower():
        # Akamai blocks and FDA "apology" pages return HTML, not PDF.
        log.warning("%s: unexpected response %s %s from %s", k_number, resp.status_code, ctype, url)
        return False
    if not resp.content.startswith(b"%PDF"):
        log.warning("%s: response did not look like a PDF", k_number)
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    log.info("%s: saved %s (%d bytes)", k_number, dest.relative_to(ROOT), len(resp.content))
    return True


def write_metadata(meta: dict) -> None:
    dest = SUMMARIES_DIR / meta["company"] / f"{meta['k_number']}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")


# ----------------------------------------------------------------------------
# Notifications
# ----------------------------------------------------------------------------

def format_message(new_items: list[dict], recovered: list[dict]) -> str:
    lines: list[str] = []
    if new_items:
        lines.append(f"*New FDA 510(k) clearances ({len(new_items)})*")
        for m in new_items:
            pdf_note = "summary PDF saved in repo" if m["_pdf_ok"] else (
                "summary PDF not posted yet, will retry" if m["expects_pdf"]
                else f"filed as {m['statement_or_summary'] or 'unknown'}, no summary PDF"
            )
            lines.append(
                f"• *{m['applicant']}* — {m['device_name']} ({m['k_number']}, "
                f"product code {m['product_code']}, decided {m['decision_date']})\n"
                f"  {m['fda_detail_url']}\n"
                f"  {m['summary_pdf_url']}\n"
                f"  _{pdf_note}_"
            )
    if recovered:
        lines.append(f"*Summary PDFs now available ({len(recovered)})*")
        for m in recovered:
            lines.append(f"• {m['applicant']} — {m['device_name']} ({m['k_number']}): {m['summary_pdf_url']}")
    return "\n".join(lines)


def notify(text: str) -> None:
    sent = False
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if webhook:
        resp = requests.post(webhook, json={"text": text}, timeout=TIMEOUT)
        resp.raise_for_status()
        log.info("Slack notification sent")
        sent = True

    smtp_host = os.environ.get("SMTP_HOST")
    email_to = os.environ.get("EMAIL_TO")
    if smtp_host and email_to:
        msg = EmailMessage()
        msg["Subject"] = "FDA 510(k) watch: new clearances"
        msg["From"] = os.environ.get("EMAIL_FROM", os.environ.get("SMTP_USER", "fda-watch@localhost"))
        msg["To"] = email_to
        msg.set_content(text.replace("*", "").replace("_", ""))
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(smtp_host, port, timeout=TIMEOUT) as s:
            s.starttls()
            user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
        log.info("Email notification sent to %s", email_to)
        sent = True

    if not sent:
        log.info("No notification channel configured; printing to stdout")
    print(text)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def run(backfill: bool, dry_run: bool, no_notify: bool) -> int:
    companies = load_companies()
    state = load_state()
    seen: dict = state.setdefault("seen", {})
    session = requests.Session()

    new_items: list[dict] = []
    recovered: list[dict] = []
    today = date.today().isoformat()

    for company in companies:
        name, q = company["name"], company["applicant_query"]
        try:
            records = query_openfda(q, session)
        except requests.RequestException as e:
            log.error("%s: openFDA query failed: %s", name, e)
            continue
        log.info("%s: %d records in openFDA", name, len(records))

        for rec in records:
            k = rec["k_number"]
            meta = record_metadata(rec, name)
            if k in seen:
                continue
            if dry_run:
                log.info("%s: NEW %s %s (%s)", name, k, meta["device_name"], meta["decision_date"])
                continue

            write_metadata(meta)
            pdf_ok = False
            if meta["expects_pdf"]:
                pdf_ok = download_pdf(name, k, session)
                time.sleep(1)  # be polite to accessdata.fda.gov
            seen[k] = {
                "company": name,
                "applicant": meta["applicant"],
                "device_name": meta["device_name"],
                "decision_date": meta["decision_date"],
                "statement_or_summary": meta["statement_or_summary"],
                "expects_pdf": meta["expects_pdf"],
                "first_seen": today,
                "pdf_downloaded": pdf_ok,
            }
            meta["_pdf_ok"] = pdf_ok
            new_items.append(meta)

    # Retry PDFs that were not available on earlier runs.
    if not dry_run:
        for k, entry in seen.items():
            if entry.get("pdf_downloaded") or not entry.get("expects_pdf"):
                continue
            if any(m["k_number"] == k for m in new_items):
                continue
            if download_pdf(entry["company"], k, session):
                entry["pdf_downloaded"] = True
                recovered.append({
                    "k_number": k,
                    "applicant": entry.get("applicant"),
                    "device_name": entry.get("device_name"),
                    "summary_pdf_url": pdf_url(k),
                })
                time.sleep(1)

    if dry_run:
        log.info("Dry run complete; nothing written")
        return 0

    save_state(state)
    log.info("State saved: %d K numbers tracked", len(seen))

    if backfill:
        log.info("Backfill: recorded %d clearances without alerting", len(new_items))
        return 0

    if new_items or recovered:
        text = format_message(new_items, recovered)
        if no_notify:
            print(text)
        else:
            notify(text)
    else:
        log.info("No new clearances")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backfill", action="store_true", help="record all current clearances without sending alerts")
    parser.add_argument("--dry-run", action="store_true", help="query and diff only; write nothing")
    parser.add_argument("--no-notify", action="store_true", help="print alerts to stdout instead of sending")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    return run(backfill=args.backfill, dry_run=args.dry_run, no_notify=args.no_notify)


if __name__ == "__main__":
    sys.exit(main())
