#!/usr/bin/env python3
"""
CLOUD JOB — Send (runs 3x/day in GitHub Actions).

Moves up to --limit contacts from the QUEUE list into the OUTREACH list.
Adding them to OUTREACH is what fires your Brevo automation = the email.

Dedupe is automatic: anyone already in OUTREACH is skipped, so this can run
twice, fail, or overlap and never double-emails. 3 runs x 100 = 300/day cap.

Env: BREVO_API_KEY, BREVO_QUEUE_LIST_ID, BREVO_LIST_ID (outreach/trigger)
Run: python send.py --limit 100 [--dry-run]
"""
import os
import sys
import argparse
import common_brevo as bv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=int(os.getenv("SEND_LIMIT", "100")))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    queue_id = os.getenv("BREVO_QUEUE_LIST_ID", "").strip()
    outreach_id = os.getenv("BREVO_LIST_ID", "").strip()
    if not queue_id.isdigit() or not outreach_id.isdigit():
        sys.exit("ERROR: BREVO_QUEUE_LIST_ID and BREVO_LIST_ID must be numeric (set them).")

    queue = bv.list_emails(queue_id)
    sent = bv.list_emails(outreach_id)
    to_send = sorted(queue - sent)[:args.limit]

    print(f"Queue={len(queue)}  AlreadySent={len(sent)}  "
          f"Eligible={len(queue - sent)}  SendingNow={len(to_send)} (limit {args.limit})")

    if not to_send:
        print("Nothing to send. Queue drained or empty — run discovery to refill.")
        return
    if args.dry_run:
        for e in to_send[:10]:
            print(f"  [dry] would send -> {e}")
        print(f"  ...{len(to_send)} total. Nothing sent.")
        return

    added = bv.add_to_list(outreach_id, to_send)
    print(f"Added {added} contacts to Outreach list {outreach_id}. Automation will email them.")


if __name__ == "__main__":
    main()
