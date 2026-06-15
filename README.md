# Soft Reset — Lead Engine (cloud)

Replaces Modash. Finds parenting micro/medium influencers on Instagram, resolves
their emails, and emails them your outreach — **fully in the cloud, no Mac
needed**. Runs on GitHub Actions on a schedule. **Brevo is the source of truth**
for who's been emailed, so the jobs are stateless and can never double-send.

```
                  ┌──────────────── GitHub Actions (cloud cron) ────────────────┐
                  │                                                             │
 discover.py  ──► Apify (IG hashtags → profiles) ──► upsert NEW into ► QUEUE list (Brevo)
 (Mon/Wed/Fri)                                                              │
                                                                           │ 100/day
 send.py      ──► move up to 100 (Queue minus Sent) ──────────────► OUTREACH list (Brevo)
 (3×/day)                                                                   │
                                                          automation fires ─┘──► EMAIL sent
```

- **QUEUE list** = everyone waiting (your 119 + all discovered). No automation.
- **OUTREACH list** = your existing automation's trigger list (`BREVO_LIST_ID`, currently `5`).
  Adding a contact here **sends the email**.
- **Dedupe = Brevo list membership.** In OUTREACH = already emailed = skipped forever.
- **300/day cap** = 3 send runs × 100, enforced by the schedule.

## Files
| File | Where it runs | Purpose |
|---|---|---|
| `discover.py` | cloud (GH Actions) | Apify discovery → emailable leads to QUEUE; no-email leads to DM worklist |
| `send.py` | cloud (GH Actions) | move 100/day QUEUE → OUTREACH (= send) |
| `stats.py` | local/anywhere | Brevo stats dashboard via API (no premium needed) — `python stats.py` |
| `dm_worklist.csv` | auto-updated by cloud | in-band creators with **no email** → DM by hand (see `DM_TEMPLATES.md`) |
| `DM_TEMPLATES.md` | reference | tier-based Instagram DM scripts + safe-sending rules |
| `common_brevo.py` | both | Brevo API helpers |
| `seed_existing.py` | **local, once** | push your 119 existing emails into QUEUE |
| `.github/workflows/*.yml` | cloud | the cron schedules |
| `1_/2_/3_*.py` | local (legacy) | the old manual Mac pipeline, still works if you want it |

### Two channels (the funnel reality)
Instagram no longer exposes business emails to scrapers, so only ~2–3% of
discovered accounts have a usable (bio) email. On the **free Apify tier (~$5/mo
≈ 2,700 scrapes)** that's roughly **55–65 emailed leads/month** — a hard ceiling,
not a bug. Everyone else is captured for the **DM channel**:
- **Email** → `discover.py` queues in-band accounts that have an email; Brevo sends.
- **DM** → in-band accounts with *no* email are appended to `dm_worklist.csv`
  (auto-committed by the cloud run). You DM these by hand, 20–30/day, using
  `DM_TEMPLATES.md`. Safe, free, and higher-converting for micro creators.

---

## One-time setup

### 1. Brevo (2 minutes)
1. **Contacts → Lists →** create a second list named **"Outreach Queue"**. Note its
   numeric ID.
2. Confirm your **existing automation** triggers on *"contact added to a list"* =
   your **OUTREACH** list (ID `5`). That stays as-is.
3. **Contacts → Settings → Contact attributes:** make sure `FIRSTNAME`, `HANDLE`,
   `FOLLOWERS`, `PLATFORM` exist (text).
4. Confirm your sending domain is **authenticated** (SPF/DKIM green). Done already.

### 2. Seed your existing 119 (local, run once on your Mac)
```bash
cd lead-engine
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# put the Queue list ID from step 1 into .env as BREVO_QUEUE_LIST_ID
python seed_existing.py --dry-run     # preview
python seed_existing.py               # push the 119 into the QUEUE list
```
This is the **only** time your Mac is involved.

### 3. Put it on GitHub (so the cloud runs it forever)
```bash
cd lead-engine
git init && git add . && git commit -m "Soft Reset lead engine"
# create a PRIVATE repo on github.com, then:
git remote add origin git@github.com:<you>/soft-reset-lead-engine.git
git branch -M main && git push -u origin main
```
> `.env` is git-ignored — your keys never leave your machine. The repo holds
> **only code** (no emails, no secrets, no ledger).

In the GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**, add:
- `APIFY_TOKEN`
- `BREVO_API_KEY`
- `BREVO_LIST_ID` = `5`
- `BREVO_QUEUE_LIST_ID` = your Queue list ID

Then **Actions** tab → enable workflows. Done — it now runs itself.

---

## Schedules (UTC; edit in the workflow files)
- **send-outreach** — `0 13,18,23 * * *` → ~9am / 2pm / 7pm ET, 100 each = 300/day.
- **discover-leads** — `0 11 * * 1,3,5` → Mon/Wed/Fri, refills the Queue (only job
  that spends Apify credit).

## Run on demand / monitor / pause
- **Run now:** Actions tab → pick a workflow → **Run workflow**.
- **Watch:** Actions tab shows each run's log (counts, errors).
- **Pause everything:** Actions tab → each workflow → **⋯ → Disable workflow**.
- **First-day smoke test:** Run `send-outreach` manually once; check 100 contacts
  landed in OUTREACH (Brevo) and the automation shows sends; check one inbox.

---

## Outreach email template (paste into the Brevo automation email)

```
Subject: a free copy of our screen-time guide for you, {{ contact.FIRSTNAME }}

Hi {{ contact.FIRSTNAME }}​,
I came across @{{ contact.HANDLE }}​ and loved your parenting content.

I wrote a short guide — The 7-Day Child Screen Reset — and I'd love to send you a free copy, no strings attached. 

If it's useful and you ever feel like sharing it, we can talk about an affiliate setup after that — but the copy is yours either way.

→ Download the guide here

If it ever resonates with your community and you feel like sharing, I'd be happy to set up a personal affiliate link for you.

But that's a conversation for another day, totally optional.

Either way — the guide is yours.
Hope it helps.

Warm,
Soft Reset Books

—

You're getting this note because you create parenting content. If it's not for
you, no hard feelings — <a href="{{ unsubscribe }}">unsubscribe here</a> and I
won't email you again.

Soft Reset Books · softresetbooks.com
```

> The unsubscribe line is an HTML link (`<a href="{{ unsubscribe }}">`). Brevo
> swaps `{{ unsubscribe }}` for each contact's real opt-out URL at send time, and
> auto-suppresses anyone who clicks it. This footer is legally required — keep it.

Merge tags available: `{{ contact.FIRSTNAME }}`, `{{ contact.HANDLE }}`,
`{{ contact.FOLLOWERS }}`, `{{ contact.PLATFORM }}`.

## Notes / guardrails
- **Volume:** 3×100 = 300/day, Brevo free-plan ceiling. Watch **bounce/complaint**
  in Brevo; disable the send workflow if bounce > 5%.
- **Cost:** only `discover.py` spends Apify credit (~$5/mo free ≈ 1–2k lookups).
  Lower `POSTS_PER_HASHTAG` or run discovery less often to spend less.
- **TikTok:** intentionally excluded — most TikTok profiles have no public email,
  so they're DM-only. Instagram-first by design.
- **Legal:** keep the unsubscribe link; Brevo auto-suppresses opt-outs.
