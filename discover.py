#!/usr/bin/env python3
"""
CLOUD JOB — Discover (runs hourly in GitHub Actions).

Apify: parenting hashtags -> recent post authors -> profile lookup (followers,
bio, public email). Keeps micro/medium accounts that have a usable email and
upserts the NEW ones into the Brevo QUEUE list (skipping anyone already in
Queue or Outreach). The send job drains the queue 100/day.

No local files, no Playwright — fully cloud. Public email + bio regex only.

Env: APIFY_TOKEN, BREVO_API_KEY, BREVO_QUEUE_LIST_ID, BREVO_LIST_ID,
     HASHTAGS, POSTS_PER_HASHTAG, FOLLOWER_MIN, FOLLOWER_MAX
Run: python discover.py
"""
import os
import re
import csv
import sys
import time
from datetime import datetime, timezone
import requests
import common_brevo as bv

WORKLIST_CSV = os.path.join(os.path.dirname(__file__), "dm_worklist.csv")
WORKLIST_COLS = ["handle", "followers", "full_name", "category", "bio_hook", "profile_url", "discovered", "status"]

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()
HASHTAGS = [h.strip().lstrip("#") for h in os.getenv("HASHTAGS", "momcontentcreator,momblogger,parentingcoach").split(",") if h.strip()]
POSTS_PER_HASHTAG = int(os.getenv("POSTS_PER_HASHTAG", "50"))
FOLLOWER_MIN = int(os.getenv("FOLLOWER_MIN", "5000"))
FOLLOWER_MAX = int(os.getenv("FOLLOWER_MAX", "250000"))

HASHTAG_ACTOR = "apify~instagram-hashtag-scraper"
PROFILE_ACTOR = "apify~instagram-profile-scraper"
EMAIL_KEYS = ("public_email", "businessEmail", "business_email", "email")
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# Strict full-string syntax check (anchored), stricter than the search regex above.
EMAIL_STRICT_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

try:
    import dns.resolver  # dnspython
    _DNS_OK = True
except Exception:
    _DNS_OK = False
    print("  ! dnspython not installed — MX check disabled, syntax-only filtering.")

_MX_CACHE = {}  # domain -> bool (has a usable mail server)


def domain_has_mx(domain):
    """True if the domain can receive mail (MX, or A-record fallback).
    Results cached per domain. Transient DNS errors are treated leniently
    (kept) so a flaky resolver never nukes a whole batch of good leads."""
    if not _DNS_OK:
        return True
    if domain in _MX_CACHE:
        return _MX_CACHE[domain]
    ok = True
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        ok = len(answers) > 0
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        # No MX. Some domains accept mail on their A record (implicit MX).
        try:
            dns.resolver.resolve(domain, "A", lifetime=5)
            ok = True
        except Exception:
            ok = False
    except Exception:
        ok = True  # timeout / NoNameservers / network — be lenient, keep it
    _MX_CACHE[domain] = ok
    return ok


def valid_email(email):
    """Syntax check + deliverable-domain (MX) check. Returns True if sendable."""
    email = (email or "").strip()
    if not EMAIL_STRICT_RE.match(email):
        return False
    domain = email.rsplit("@", 1)[-1].lower()
    return domain_has_mx(domain)


def run_actor(actor, payload):
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
    r = requests.post(url, params={"token": APIFY_TOKEN}, json=payload, timeout=600)
    if r.status_code >= 400:
        print(f"  ! {actor} HTTP {r.status_code}: {r.text[:200]}")
        r.raise_for_status()
    return r.json()


def collect_usernames():
    users = set()
    for tag in HASHTAGS:
        print(f"[hashtag] #{tag} (limit {POSTS_PER_HASHTAG})")
        try:
            items = run_actor(HASHTAG_ACTOR, {"hashtags": [tag], "resultsLimit": POSTS_PER_HASHTAG})
        except Exception as e:
            print(f"  ! skipped #{tag}: {e}")
            continue
        for it in items:
            u = it.get("ownerUsername") or it.get("username")
            if u:
                users.add(u)
        print(f"  total handles so far: {len(users)}")
        time.sleep(1)
    return sorted(users)


def fetch_profiles(usernames):
    rows, CHUNK = [], 50
    for i in range(0, len(usernames), CHUNK):
        batch = usernames[i:i + CHUNK]
        print(f"[profiles] {i + 1}-{i + len(batch)} of {len(usernames)}")
        try:
            items = run_actor(PROFILE_ACTOR, {"usernames": batch})
        except Exception as e:
            print(f"  ! chunk failed: {e}")
            continue
        for p in items:
            followers = p.get("followersCount") or p.get("followers") or 0
            email = ""
            for k in EMAIL_KEYS:
                if p.get(k):
                    email = str(p[k]).strip()
                    break
            if not email:
                m = EMAIL_RE.search(p.get("biography") or "")
                email = m.group(0) if m else ""
            rows.append({
                "username": (p.get("username") or "").lstrip("@"),
                "name": p.get("fullName") or "",
                "followers": followers,
                "email": email,
                "bio": (p.get("biography") or "").strip(),
                "category": str(p.get("businessCategoryName") or "").replace("None", "").strip(","),
            })
        time.sleep(1)
    return rows


def in_band(f):
    try:
        return FOLLOWER_MIN <= int(f) <= FOLLOWER_MAX
    except (ValueError, TypeError):
        return False


def _bio_hook(bio):
    """One-line, comma-safe snippet of the bio for personalizing a manual DM."""
    one = " ".join((bio or "").split())            # collapse newlines/whitespace
    return one[:140]


def append_worklist(dm_candidates):
    """Append in-band, NO-email accounts to dm_worklist.csv for manual DM outreach.
    Dedupes by handle against the existing file so re-runs never duplicate.
    Returns the number of NEW rows written."""
    existing = set()
    if os.path.exists(WORKLIST_CSV):
        with open(WORKLIST_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("handle"):
                    existing.add(row["handle"].lstrip("@").lower())

    today = datetime.now(timezone.utc).date().isoformat()
    new_rows = []
    seen = set()
    for p in dm_candidates:
        h = p["username"].lower()
        if not h or h in existing or h in seen:
            continue
        seen.add(h)
        new_rows.append({
            "handle": "@" + p["username"],
            "followers": p["followers"],
            "full_name": p["name"],
            "category": p.get("category", ""),
            "bio_hook": _bio_hook(p.get("bio", "")),
            "profile_url": f"https://www.instagram.com/{p['username']}",
            "discovered": today,
            "status": "",          # you fill: sent / replied / passed
        })

    if not new_rows:
        return 0
    write_header = not os.path.exists(WORKLIST_CSV)
    with open(WORKLIST_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=WORKLIST_COLS)
        if write_header:
            w.writeheader()
        w.writerows(new_rows)
    return len(new_rows)


def main():
    if not APIFY_TOKEN or APIFY_TOKEN.startswith("apify_api_xxxx"):
        sys.exit("ERROR: APIFY_TOKEN not set.")
    queue_id = os.getenv("BREVO_QUEUE_LIST_ID", "").strip()
    outreach_id = os.getenv("BREVO_LIST_ID", "").strip()
    if not queue_id.isdigit():
        sys.exit("ERROR: BREVO_QUEUE_LIST_ID must be numeric.")

    usernames = collect_usernames()
    if not usernames:
        sys.exit("No handles discovered (check hashtags / Apify credit).")
    profiles = fetch_profiles(usernames)

    in_band_with_email = [p for p in profiles if in_band(p["followers"]) and p["email"]]
    # MX/syntax pre-filter: drop undeliverable addresses BEFORE they hit the
    # Queue, so the send job never burns reputation on bounces.
    candidates, rejected = [], 0
    for p in in_band_with_email:
        if valid_email(p["email"]):
            candidates.append(p)
        else:
            rejected += 1
            print(f"  - dropped (bad syntax/MX): {p['email']}")
    print(f"\n{len(profiles)} profiles, {len(in_band_with_email)} in band with an email, "
          f"{rejected} dropped by MX/syntax filter, {len(candidates)} deliverable.")

    # In-band accounts with NO email -> manual-DM worklist (rescues the ~97% the
    # email pipeline can't use; you DM these by hand, safely, 20-30/day).
    dm_candidates = [p for p in profiles if in_band(p["followers"]) and not p["email"]]
    dm_added = append_worklist(dm_candidates)
    print(f"DM worklist: {len(dm_candidates)} in-band no-email accounts found, "
          f"{dm_added} new appended to dm_worklist.csv.")

    # Dedupe against Brevo (source of truth): skip anyone already queued or sent.
    known = bv.list_emails(queue_id)
    if outreach_id.isdigit():
        known |= bv.list_emails(outreach_id)
    print(f"Already known in Brevo (queue+sent): {len(known)}")

    added = 0
    seen = set()
    for p in candidates:
        em = p["email"].lower()
        if em in known or em in seen:
            continue
        seen.add(em)
        attrs = {
            "FIRSTNAME": (p["name"].split()[0] if p["name"] else p["username"]),
            "HANDLE": "@" + p["username"],
            "FOLLOWERS": str(p["followers"]),
            "PLATFORM": "IG",
        }
        if bv.upsert_contact(p["email"], attrs, [queue_id]):
            added += 1
            time.sleep(0.3)

    print(f"\nQueued {added} new leads into Brevo list {queue_id}. "
          f"Send job will email them 100/day.")


if __name__ == "__main__":
    main()
