#!/usr/bin/env python3
"""
Step 3 — Push contacts into Brevo.

Upserts each contact and adds them to BREVO_LIST_ID. Because your Brevo
automation is triggered by "contact added to this list", the push IS the send.

Sources:
  existing  -> the 119 emails already in Soft-Reset-Lead-List.xlsx ("Start Here (Outreach)")
  new       -> leads_ready.csv (from step 2)
  both      -> existing then new

Safety: batched (--limit, default 100 ≈ Brevo free-plan headroom), --dry-run,
and a ledger.csv so the same person is never pushed twice.

Run:  python 3_push_brevo.py --source existing --dry-run
      python 3_push_brevo.py --source existing --limit 100
      python 3_push_brevo.py --source new --limit 100
"""
import os
import sys
import csv
import time
import argparse
import requests
from pathlib import Path
from dotenv import load_dotenv

HERE = Path(__file__).parent
ROOT = HERE.parent
load_dotenv(HERE / ".env")

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
BREVO_LIST_ID = os.getenv("BREVO_LIST_ID", "").strip()
XLSX = ROOT / "leads curated" / "Soft-Reset-Lead-List.xlsx"
LEADS = HERE / "leads_ready.csv"
LEDGER = HERE / "ledger.csv"
API = "https://api.brevo.com/v3/contacts"


def load_ledger() -> set:
    done = set()
    if LEDGER.exists():
        with open(LEDGER, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("email"):
                    done.add(r["email"].strip().lower())
    return done


def append_ledger(rows):
    new = not LEDGER.exists()
    with open(LEDGER, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["email", "handle", "source", "status"])
        if new:
            w.writeheader()
        w.writerows(rows)


def from_existing() -> list:
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
                "first_name": (str(name).split()[0] if name else (handle or "there")),
                "handle": (handle or "").strip(),
                "followers": followers or "",
                "platform": "IG",
                "source": "existing-xlsx",
            })
    return out


def from_new() -> list:
    if not LEADS.exists():
        return []
    with open(LEADS, newline="", encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r.get("email")]


def upsert(rec: dict) -> str:
    payload = {
        "email": rec["email"],
        "attributes": {
            "FIRSTNAME": rec.get("first_name", ""),
            "HANDLE": rec.get("handle", ""),
            "FOLLOWERS": str(rec.get("followers", "")),
            "PLATFORM": rec.get("platform", "IG"),
        },
        "listIds": [int(BREVO_LIST_ID)],
        "updateEnabled": True,
    }
    r = requests.post(API, headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
                      json=payload, timeout=30)
    if r.status_code in (201, 204):
        return "created" if r.status_code == 201 else "updated"
    return f"error {r.status_code}: {r.text[:140]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["existing", "new", "both"], default="existing")
    ap.add_argument("--limit", type=int, default=100, help="max contacts this run (throttles sends)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run:
        if not BREVO_API_KEY or BREVO_API_KEY.startswith("xkeysib-xxxx"):
            sys.exit("ERROR: set BREVO_API_KEY in lead-engine/.env")
        if not BREVO_LIST_ID.isdigit():
            sys.exit("ERROR: set BREVO_LIST_ID (numeric) in lead-engine/.env")

    pool = []
    if args.source in ("existing", "both"):
        pool += from_existing()
    if args.source in ("new", "both"):
        pool += from_new()

    done = load_ledger()
    queue, seen = [], set()
    for rec in pool:
        key = rec["email"].lower()
        if key in done or key in seen:
            continue
        seen.add(key)
        queue.append(rec)

    print(f"Source={args.source}: {len(pool)} candidates, {len(queue)} new after dedupe, "
          f"pushing up to {args.limit}.")
    queue = queue[:args.limit]

    if args.dry_run:
        for rec in queue[:10]:
            print(f"  [dry] {rec['email']:<40} {rec['handle']:<22} {rec['first_name']}")
        print(f"  ...{len(queue)} total (showing first 10). No contacts sent.")
        return

    results = []
    for i, rec in enumerate(queue, 1):
        status = upsert(rec)
        results.append({"email": rec["email"], "handle": rec.get("handle", ""),
                        "source": rec.get("source", ""), "status": status})
        print(f"  {i}/{len(queue)} {rec['email']:<40} -> {status}")
        time.sleep(0.4)

    append_ledger(results)
    ok = sum(1 for r in results if r["status"] in ("created", "updated"))
    print(f"\nDone. {ok}/{len(results)} pushed to list {BREVO_LIST_ID} (automation will send). "
          f"Logged to {LEDGER}.")


if __name__ == "__main__":
    main()
