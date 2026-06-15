#!/usr/bin/env python3
"""
Brevo stats dashboard — pulls everything via the API so you don't need the
premium in-app analytics. Reads BREVO_API_KEY (and optionally the list IDs)
from .env or the environment.

Usage:
    python stats.py                # last 30 days
    python stats.py --days 7       # last 7 days
    python stats.py --events 20    # also show 20 most-recent email events
"""
import os
import sys
import argparse
import requests
import common_brevo as bv

BASE = "https://api.brevo.com/v3"


def get(path, **params):
    r = requests.get(f"{BASE}{path}", headers=bv._headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def pct(part, whole):
    return f"{(100.0 * part / whole):.1f}%" if whole else "—"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="lookback window for email stats")
    ap.add_argument("--events", type=int, default=0, help="show N most-recent email events")
    args = ap.parse_args()

    print("=" * 60)
    print(f"  SOFT RESET — BREVO STATS  (last {args.days} days)")
    print("=" * 60)

    # ---- Account ----------------------------------------------------------
    try:
        acct = get("/account")
        plan = acct.get("plan", [{}])
        credits = next((p.get("credits") for p in plan if p.get("type") in ("sendLimit", "free")), None)
        print(f"\nAccount: {acct.get('email','?')}")
        if credits is not None:
            print(f"Email credits remaining: {credits}")
    except Exception as e:
        print(f"\n(account info unavailable: {e})")

    # ---- Lists ------------------------------------------------------------
    print("\n" + "-" * 60)
    print("  LISTS  (real contact counts, not Brevo's 'subscribers')")
    print("-" * 60)
    queue_id = os.getenv("BREVO_QUEUE_LIST_ID", "6").strip()
    outreach_id = os.getenv("BREVO_LIST_ID", "5").strip()
    try:
        queue = bv.list_emails(queue_id)
        sent = bv.list_emails(outreach_id)
        eligible = queue - sent
        print(f"Queue   (list {queue_id}): {len(queue):>4} contacts")
        print(f"Outreach(list {outreach_id}): {len(sent):>4} contacts  (= emailed)")
        print(f"Eligible to send next:   {len(eligible):>4}")
    except Exception as e:
        print(f"(list counts unavailable: {e})")

    # ---- Email performance ------------------------------------------------
    print("\n" + "-" * 60)
    print("  EMAIL PERFORMANCE")
    print("-" * 60)
    rep = get("/smtp/statistics/aggregatedReport", days=args.days)
    requests_ = rep.get("requests", 0)
    delivered = rep.get("delivered", 0)
    hard = rep.get("hardBounces", 0)
    soft = rep.get("softBounces", 0)
    opens = rep.get("opens", 0)
    uopens = rep.get("uniqueOpens", 0)
    clicks = rep.get("clicks", 0)
    uclicks = rep.get("uniqueClicks", 0)
    unsub = rep.get("unsubscribed", 0)
    spam = rep.get("spamReports", 0)
    blocked = rep.get("blocked", 0)
    invalid = rep.get("invalid", 0)

    rows = [
        ("Sent (requests)", requests_, ""),
        ("Delivered", delivered, pct(delivered, requests_)),
        ("Unique opens", uopens, pct(uopens, delivered)),
        ("Total opens", opens, ""),
        ("Unique clicks", uclicks, pct(uclicks, delivered)),
        ("Total clicks", clicks, ""),
        ("Unsubscribed", unsub, pct(unsub, delivered)),
        ("Spam reports", spam, pct(spam, delivered)),
        ("Hard bounces", hard, pct(hard, requests_)),
        ("Soft bounces", soft, pct(soft, requests_)),
        ("Blocked", blocked, ""),
        ("Invalid", invalid, ""),
    ]
    for label, val, rate in rows:
        print(f"  {label:<18} {val:>6}   {rate}")

    # ---- Health flags -----------------------------------------------------
    print("\n" + "-" * 60)
    print("  HEALTH")
    print("-" * 60)
    bounce_rate = (hard + soft) / requests_ * 100 if requests_ else 0
    spam_rate = spam / delivered * 100 if delivered else 0
    open_rate = uopens / delivered * 100 if delivered else 0
    flag = lambda ok: "OK " if ok else "!! "
    print(f"  {flag(bounce_rate <= 5)}Bounce rate {bounce_rate:.1f}%   (target <=5%)")
    print(f"  {flag(spam_rate <= 0.1)}Spam rate   {spam_rate:.2f}%   (target <=0.1%)")
    print(f"  {flag(open_rate >= 15)}Open rate   {open_rate:.1f}%   (cold-email good >=15%)")
    if bounce_rate > 5:
        print("\n  -> High bounces. Add MX/syntax pre-filter in discover.py to protect reputation.")

    # ---- Recent events ----------------------------------------------------
    if args.events:
        print("\n" + "-" * 60)
        print(f"  RECENT EVENTS (last {args.events})")
        print("-" * 60)
        ev = get("/smtp/statistics/events", days=args.days, limit=args.events).get("events", [])
        for e in ev:
            print(f"  {e.get('date','')[:19]}  {e.get('event',''):<14} {e.get('email','')}")

    print()


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        sys.exit(f"Brevo API error: {e.response.status_code} {e.response.text[:200]}")
