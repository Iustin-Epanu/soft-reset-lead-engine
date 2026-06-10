#!/usr/bin/env python3
"""
ONE-TIME LOCAL SEED — run once on your Mac.

Reads the 119 emails already in Soft-Reset-Lead-List.xlsx and upserts them into
the Brevo QUEUE list. After this, the cloud send job drains them 100/day and you
never touch your Mac for outreach again.

Env (from lead-engine/.env): BREVO_API_KEY, BREVO_QUEUE_LIST_ID
Run: python seed_existing.py [--dry-run]
"""
import os
import sys
import argparse
from pathlib import Path
import common_brevo as bv

XLSX = Path(__file__).parent.parent / "leads curated" / "Soft-Reset-Lead-List.xlsx"


def rows_from_xlsx():
    import openpyxl
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb["Start Here (Outreach)"]
    out = []
    for row in ws.iter_rows(min_row=5, values_only=True):
        name = row[2] if len(row) > 2 else ""
        handle = row[3] if len(row) > 3 else ""
        followers = row[5] if len(row) > 5 else ""
        email = row[14] if len(row) > 14 else ""
        if email and isinstance(email, str) and "@" in email:
            out.append({
                "email": email.strip(),
                "FIRSTNAME": (str(name).split()[0] if name else (handle or "there")),
                "HANDLE": (handle or "").strip(),
                "FOLLOWERS": str(followers or ""),
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    queue_id = os.getenv("BREVO_QUEUE_LIST_ID", "").strip()
    if not queue_id.isdigit():
        sys.exit("ERROR: set BREVO_QUEUE_LIST_ID in lead-engine/.env (create the Queue list in Brevo first).")

    rows = rows_from_xlsx()
    print(f"Found {len(rows)} emails in the xlsx.")
    if args.dry_run:
        for r in rows[:10]:
            print(f"  [dry] {r['email']:<38} {r['HANDLE']:<22} {r['FIRSTNAME']}")
        print(f"  ...{len(rows)} total. Nothing sent.")
        return

    known = bv.list_emails(queue_id)
    added = 0
    for r in rows:
        if r["email"].lower() in known:
            continue
        attrs = {"FIRSTNAME": r["FIRSTNAME"], "HANDLE": r["HANDLE"],
                 "FOLLOWERS": r["FOLLOWERS"], "PLATFORM": "IG"}
        if bv.upsert_contact(r["email"], attrs, [queue_id]):
            added += 1
    print(f"Seeded {added} contacts into Brevo Queue list {queue_id}.")


if __name__ == "__main__":
    main()
