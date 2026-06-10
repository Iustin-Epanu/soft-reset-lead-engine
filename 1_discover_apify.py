#!/usr/bin/env python3
"""
Step 1 — Discovery (replaces Modash).

Uses Apify actors to:
  1. Pull recent post authors under parenting hashtags (Instagram Hashtag Scraper).
  2. Look up each unique profile (Instagram Profile Scraper) for followers / bio / email.
  3. Keep only micro/medium accounts in the follower band.

Output: discovered_raw.csv  (handle, name, followers, biography, public_email)

Run:  python 1_discover_apify.py
Env:  APIFY_TOKEN, HASHTAGS, POSTS_PER_HASHTAG, FOLLOWER_MIN, FOLLOWER_MAX  (see .env.example)
"""
import os
import sys
import csv
import json
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()
HASHTAGS = [h.strip().lstrip("#") for h in os.getenv("HASHTAGS", "gentleparenting,momlife").split(",") if h.strip()]
POSTS_PER_HASHTAG = int(os.getenv("POSTS_PER_HASHTAG", "80"))
FOLLOWER_MIN = int(os.getenv("FOLLOWER_MIN", "10000"))
FOLLOWER_MAX = int(os.getenv("FOLLOWER_MAX", "120000"))

HASHTAG_ACTOR = "apify~instagram-hashtag-scraper"
PROFILE_ACTOR = "apify~instagram-profile-scraper"
OUT = HERE / "discovered_raw.csv"

EMAIL_KEYS = ("public_email", "businessEmail", "business_email", "email")


def run_actor(actor: str, payload: dict) -> list:
    """Run an Apify actor synchronously and return its dataset items."""
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
    r = requests.post(url, params={"token": APIFY_TOKEN}, json=payload, timeout=600)
    if r.status_code >= 400:
        print(f"  ! {actor} HTTP {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
    return r.json()


def collect_usernames() -> set:
    users = set()
    for tag in HASHTAGS:
        print(f"[hashtag] #{tag} (limit {POSTS_PER_HASHTAG}) ...")
        try:
            items = run_actor(HASHTAG_ACTOR, {"hashtags": [tag], "resultsLimit": POSTS_PER_HASHTAG})
        except Exception as e:
            print(f"  ! skipped #{tag}: {e}")
            continue
        new = 0
        for it in items:
            u = it.get("ownerUsername") or it.get("username")
            if u:
                if u not in users:
                    new += 1
                users.add(u)
        print(f"  -> {len(items)} posts, +{new} new handles (total {len(users)})")
        time.sleep(1)
    return users


def fetch_profiles(usernames: list) -> list:
    """Look up profiles in chunks; return normalized dicts."""
    rows = []
    CHUNK = 25
    for i in range(0, len(usernames), CHUNK):
        batch = usernames[i:i + CHUNK]
        print(f"[profiles] {i + 1}-{i + len(batch)} of {len(usernames)} ...")
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
            rows.append({
                "handle": "@" + (p.get("username") or "").lstrip("@"),
                "name": p.get("fullName") or "",
                "followers": int(followers) if str(followers).isdigit() else followers,
                "biography": (p.get("biography") or "").replace("\n", " ").strip(),
                "public_email": email,
            })
        time.sleep(1)
    return rows


def in_band(followers) -> bool:
    try:
        n = int(followers)
    except (ValueError, TypeError):
        return False
    return FOLLOWER_MIN <= n <= FOLLOWER_MAX


def main():
    if not APIFY_TOKEN or APIFY_TOKEN.startswith("apify_api_xxxx"):
        sys.exit("ERROR: set APIFY_TOKEN in lead-engine/.env (copy from .env.example).")

    usernames = sorted(collect_usernames())
    if not usernames:
        sys.exit("No handles discovered. Check hashtags / Apify credit.")
    print(f"\nDiscovered {len(usernames)} unique handles. Fetching profiles...\n")

    profiles = fetch_profiles(usernames)
    kept = [p for p in profiles if in_band(p["followers"])]
    print(f"\n{len(profiles)} profiles fetched, {len(kept)} in band [{FOLLOWER_MIN}-{FOLLOWER_MAX}].")

    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["handle", "name", "followers", "biography", "public_email"])
        w.writeheader()
        w.writerows(kept)
    with_email = sum(1 for p in kept if p["public_email"])
    print(f"Wrote {OUT}  ({len(kept)} rows, {with_email} already have a public email).")


if __name__ == "__main__":
    main()
