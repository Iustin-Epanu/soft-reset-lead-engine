#!/usr/bin/env python3
"""
Shared Brevo helpers. Brevo is the single source of truth for who has been
emailed (= membership of the Outreach list), so the cloud jobs are stateless.
Env: BREVO_API_KEY
"""
import os
import time
import requests
from pathlib import Path

# Load a local .env if present (for local runs / seeding). In GitHub Actions the
# env comes from secrets, and no .env file exists — that's fine.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

BASE = "https://api.brevo.com/v3"


def _headers():
    key = os.getenv("BREVO_API_KEY", "").strip()
    if not key or key.startswith("xkeysib-xxxx"):
        raise SystemExit("ERROR: BREVO_API_KEY is not set.")
    return {"api-key": key, "accept": "application/json", "content-type": "application/json"}


def upsert_contact(email, attributes=None, list_ids=None):
    """Create-or-update a contact. Returns True on success."""
    payload = {"email": email, "updateEnabled": True}
    if attributes:
        payload["attributes"] = attributes
    if list_ids:
        payload["listIds"] = [int(x) for x in list_ids]
    r = requests.post(f"{BASE}/contacts", headers=_headers(), json=payload, timeout=30)
    if r.status_code in (201, 204):
        return True
    print(f"  ! upsert {email}: HTTP {r.status_code} {r.text[:160]}")
    return False


def list_emails(list_id):
    """Return the set of lowercased emails currently in a Brevo list."""
    emails, offset, limit = set(), 0, 500
    while True:
        r = requests.get(f"{BASE}/contacts/lists/{int(list_id)}/contacts", headers=_headers(),
                         params={"limit": limit, "offset": offset}, timeout=30)
        r.raise_for_status()
        batch = r.json().get("contacts", [])
        for c in batch:
            if c.get("email"):
                emails.add(c["email"].strip().lower())
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(0.2)
    return emails


def add_to_list(list_id, emails):
    """Add already-existing contacts to a list (fires that list's automation).
    Endpoint is .../contacts/add. Batches of 150 per the Brevo API limit.
    Returns count actually added (per Brevo's success array)."""
    added = 0
    emails = list(emails)
    for i in range(0, len(emails), 150):
        chunk = emails[i:i + 150]
        r = requests.post(f"{BASE}/contacts/lists/{int(list_id)}/contacts/add", headers=_headers(),
                          json={"emails": chunk}, timeout=30)
        if r.status_code in (201, 204):
            try:
                succ = r.json().get("contacts", {}).get("success", [])
                added += len(succ) if succ else len(chunk)
            except Exception:
                added += len(chunk)
        else:
            print(f"  ! add_to_list batch {i}: HTTP {r.status_code} {r.text[:160]}")
        time.sleep(0.3)
    return added
