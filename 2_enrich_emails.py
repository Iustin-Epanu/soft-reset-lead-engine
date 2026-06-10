#!/usr/bin/env python3
"""
Step 2 — Enrich emails + dedupe.

Input:  discovered_raw.csv  (from step 1)
For each row, resolve an email:
    1. public_email from the profile, else
    2. regex match on the biography, else
    3. (optional, --scrape-missing) live Playwright bio scrape, reusing
       get_ig_email() from ../leads curated/scrape_emails.py

Drops anyone already in the master xlsx or already pushed (ledger.csv),
so nobody is emailed twice.

Output: leads_ready.csv  (email, first_name, handle, followers, platform, source)

Run:  python 2_enrich_emails.py                 # fast: public_email + bio regex
      python 2_enrich_emails.py --scrape-missing # also Playwright-scrape the gaps
"""
import os
import sys
import csv
import re
import importlib.util
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
RAW = HERE / "discovered_raw.csv"
OUT = HERE / "leads_ready.csv"
LEDGER = HERE / "ledger.csv"
XLSX = ROOT / "leads curated" / "Soft-Reset-Lead-List.xlsx"
SCRAPER_PY = ROOT / "leads curated" / "scrape_emails.py"

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def load_existing_handles() -> set:
    """Handles already in the master list (any sheet) + the ledger — skip these."""
    handles = set()
    try:
        import openpyxl
        wb = openpyxl.load_workbook(XLSX, data_only=True, read_only=True)
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for v in row:
                    if isinstance(v, str) and v.strip().startswith("@"):
                        handles.add(v.strip().lower())
    except Exception as e:
        print(f"  (warn) could not read xlsx handles: {e}")
    if LEDGER.exists():
        with open(LEDGER, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("handle"):
                    handles.add(r["handle"].strip().lower())
    return handles


def first_name_from(name: str, handle: str) -> str:
    if name and name.strip():
        return name.strip().split()[0]
    return handle.lstrip("@")


def load_ig_email_fn():
    """Import get_ig_email from the existing scraper (path has a space)."""
    spec = importlib.util.spec_from_file_location("scrape_emails", SCRAPER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.get_ig_email


def main():
    scrape_missing = "--scrape-missing" in sys.argv
    if not RAW.exists():
        sys.exit(f"ERROR: {RAW} not found. Run 1_discover_apify.py first.")

    skip = load_existing_handles()
    print(f"Known handles to skip (xlsx + ledger): {len(skip)}")

    with open(RAW, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    ready, gaps = [], []
    for r in rows:
        handle = (r.get("handle") or "").strip()
        if not handle or handle.lower() in skip:
            continue
        email = (r.get("public_email") or "").strip()
        if not email:
            m = EMAIL_RE.search(r.get("biography") or "")
            email = m.group(0) if m else ""
        rec = {
            "email": email,
            "first_name": first_name_from(r.get("name", ""), handle),
            "handle": handle,
            "followers": r.get("followers", ""),
            "platform": "IG",
            "source": "apify-discovery",
        }
        (ready if email else gaps).append(rec)

    if scrape_missing and gaps:
        print(f"Playwright-scraping {len(gaps)} profiles with no public email...")
        try:
            from playwright.sync_api import sync_playwright
            get_ig_email = load_ig_email_fn()
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                for rec in gaps:
                    em = get_ig_email(page, rec["handle"])
                    if em:
                        rec["email"] = em
                        ready.append(rec)
                browser.close()
        except Exception as e:
            print(f"  (warn) Playwright scrape skipped: {e}")

    # dedupe by email
    seen, deduped = set(), []
    for rec in ready:
        key = rec["email"].lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(rec)

    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["email", "first_name", "handle", "followers", "platform", "source"])
        w.writeheader()
        w.writerows(deduped)

    print(f"\nWrote {OUT}: {len(deduped)} emailable leads "
          f"({len(gaps) - (len(ready) - len([r for r in ready if r in deduped]))} still without email).")
    print(f"Tip: re-run with --scrape-missing to recover more emails via Playwright.")


if __name__ == "__main__":
    main()
