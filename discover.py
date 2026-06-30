#!/usr/bin/env python3
"""
CLOUD JOB — Discover (runs daily in GitHub Actions).

Apify PUBLIC scrapers (which run on Apify's own residential proxy pool — the free
"residential proxy"): parenting hashtags -> recent post authors -> profile lookup
(followers, bio, public email). Keeps micro/medium accounts with a usable email
and upserts the NEW ones into the Brevo QUEUE list (skipping anyone already in
Queue or Outreach). No-email in-band accounts go to dm_worklist.csv. The send job
drains the queue.

Requires a FREE-plan Apify token — a "CUSTOM" plan that disables public actors
will 403. No local files, no Playwright — fully cloud.

Env: APIFY_TOKEN (FREE plan), BREVO_API_KEY, BREVO_QUEUE_LIST_ID, BREVO_LIST_ID,
     HASHTAGS, POSTS_PER_HASHTAG, FOLLOWER_MIN, FOLLOWER_MAX,
     HASHTAGS_PER_RUN (rotating window; 0=all),
     APIFY_MONTHLY_RESULT_CAP (free-tier spend guard; default 1700)
Run: python discover.py

Cost control: results consumed are tallied per billing cycle in apify_usage.json
and the run hard-stops at APIFY_MONTHLY_RESULT_CAP, so the Free account never maxes.
"""
import os
import re
import csv
import sys
import json
import time
from datetime import datetime, timezone
import requests
import common_brevo as bv

WORKLIST_CSV = os.path.join(os.path.dirname(__file__), "dm_worklist.csv")
WORKLIST_COLS = ["handle", "followers", "full_name", "category", "bio_hook", "profile_url", "discovered", "status"]

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()
HASHTAGS = [h.strip().lstrip("#") for h in os.getenv("HASHTAGS", "momcontentcreator,momblogger,parentingcoach").split(",") if h.strip()]
POSTS_PER_HASHTAG = int(os.getenv("POSTS_PER_HASHTAG", "50"))
# Only scrape a rotating window of this many hashtags per run (0 / >=len = all).
# Keeps each run small + predictable; the window advances daily so every tag in
# HASHTAGS still gets covered over time. Pair with the monthly cap below.
HASHTAGS_PER_RUN = int(os.getenv("HASHTAGS_PER_RUN", "0"))
FOLLOWER_MIN = int(os.getenv("FOLLOWER_MIN", "5000"))
FOLLOWER_MAX = int(os.getenv("FOLLOWER_MAX", "250000"))

# --- Apify free-tier spend guard -------------------------------------------
# The Apify FREE plan gives ~$5/month of credit, and Apify's PUBLIC IG scrapers
# (used below) run on Apify's own residential proxy pool — that's the "free
# residential proxy": you don't manage it, it's bundled into the ~$2.70/1k result
# price. We tally results consumed per billing cycle in a committed JSON file
# (apify_usage.json) and HARD-STOP at the cap so the account never maxes out
# (which would disable public actors until next cycle). One Free account, forever.
USAGE_FILE = os.path.join(os.path.dirname(__file__), "apify_usage.json")
MONTHLY_RESULT_CAP = int(os.getenv("APIFY_MONTHLY_RESULT_CAP", "1700"))

# Public Apify IG scrapers — these run on Apify's residential infra (no sessionids
# or proxies for us to manage). Require a FREE-plan token; a "CUSTOM" plan that
# disables public actors will 403 here.
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


def _cycle_key():
    """Current Apify billing-cycle bucket (calendar month, UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _load_usage():
    """Load this cycle's result counter; auto-resets when the month rolls over."""
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    if data.get("cycle") != _cycle_key():
        data = {"cycle": _cycle_key(), "results": 0}
    return data


_USAGE = _load_usage()


def results_remaining():
    """Apify results still allowed this billing cycle (>= 0)."""
    return max(0, MONTHLY_RESULT_CAP - int(_USAGE.get("results", 0)))


def _record_results(n):
    """Add n consumed results to the cycle counter and persist immediately."""
    _USAGE["results"] = int(_USAGE.get("results", 0)) + max(0, int(n))
    try:
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(_USAGE, f)
    except OSError as e:
        print(f"  ! could not persist usage counter: {e}")


def todays_hashtags():
    """A rotating window of HASHTAGS_PER_RUN tags. The window advances by day so
    that, over successive runs, every tag in HASHTAGS gets covered — without any
    single run paying to scrape the whole set."""
    n = len(HASHTAGS)
    if HASHTAGS_PER_RUN <= 0 or HASHTAGS_PER_RUN >= n:
        return HASHTAGS
    doy = datetime.now(timezone.utc).timetuple().tm_yday
    start = (doy * HASHTAGS_PER_RUN) % n
    return [HASHTAGS[(start + i) % n] for i in range(HASHTAGS_PER_RUN)]


def run_actor(actor, payload, cost=True):
    """Call a public Apify actor synchronously. When cost=True the returned items
    are billed, so we charge them against the monthly cap counter."""
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
    r = requests.post(url, params={"token": APIFY_TOKEN}, json=payload, timeout=600)
    if r.status_code >= 400:
        print(f"  ! {actor} HTTP {r.status_code}: {r.text[:200]}")
        r.raise_for_status()
    items = r.json()
    if cost:
        _record_results(len(items))
    return items


def collect_usernames():
    users = set()
    for tag in todays_hashtags():
        budget = results_remaining()
        if budget <= 0:
            print("  ! monthly Apify cap reached — stopping hashtag scrape.")
            break
        limit = min(POSTS_PER_HASHTAG, budget)
        print(f"[hashtag] #{tag} (limit {limit}, budget left {budget})")
        try:
            items = run_actor(HASHTAG_ACTOR, {"hashtags": [tag], "resultsLimit": limit})
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
        budget = results_remaining()
        if budget <= 0:
            print("  ! monthly Apify cap reached — stopping profile lookups.")
            break
        # Never request more profiles than the remaining budget can pay for.
        batch = usernames[i:i + min(CHUNK, budget)]
        print(f"[profiles] {i + 1}-{i + len(batch)} of {len(usernames)} (budget left {budget})")
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

    used = int(_USAGE.get("results", 0))
    print(f"Apify budget [{_USAGE['cycle']}]: {used}/{MONTHLY_RESULT_CAP} results used, "
          f"{results_remaining()} left this cycle.")
    if results_remaining() <= 0:
        print("Monthly Apify cap already reached — skipping discovery until next cycle.")
        return

    usernames = collect_usernames()
    if not usernames:
        sys.exit("No handles discovered (check hashtags / Apify credit / that the token is a FREE-plan account).")
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
