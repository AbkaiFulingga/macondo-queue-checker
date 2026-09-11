# Macondo Queue Checker — public ETA website

## What we're building
A public tool where any Macondo member pastes their project URL/ID (or username) and sees live status, full pipeline context, data-driven review estimates, gold projections, and a complete reconstructed queue view — with rich interactive graphs.

**Decisions locked in:** GitHub Pages + Actions · public tool · nightly snapshot refresh · Cloudflare Worker CORS proxy for live status (Macondo API has no CORS headers) · Chart.js 4 via CDN for graphs.

---

## THE REVIEW PIPELINE — verified model (from decompiled reviewer pages)

Stage machine a ship moves through, with public visibility of each:

```
[submit] → UNDER_REVIEW (amber, "In queue" — unassigned or assigned; assignment NOT public)
         → reviewer decides →
   ├─ needs_changes  → owner revises → resubmits (new ship, back to under_review)
   ├─ rejected       → done (is_fraud_rejection flag visible if fraud-related)
   └─ approved → PENDING_SECOND_PASS ("Your project has been accepted! … waiting on a
                 final second-pass check before the reward is released. Project details
                 and journals stay read-only until it's done.")
                 → supaadmin double-check in ARI (separate Hack Club system) confirms
                 → SHIPPED / shipped_missing_airtable → rewards, notifications, Unified sync

  Side-door: any point → PENDING_FRAUD_REVIEW ("Fraudsquad Review", red badge in reviewer
  queue) — escalated to an external fraud API via cron poll; "Routed to fraud without an
  upstream submission (shadow-ban path or hardware ship)" exists; verdicts return async.
  Reviewer roles: supaadmin/admin/software_reviewer/hardware_reviewer/fulfiller/hq/fraudster.
  Past reviews are reversible (reverse-decision endpoint, audit-logged).
```

**Public visibility, verified by probing:**
- `under_review`, `needs_changes`, `rejected`, `shipped`, `shipped_missing_airtable`, AND `pending_second_pass` all appear in the PUBLIC `/api/projects/{id}/ships` payload (the public project page branches on `pending_second_pass` for the "accepted, awaiting final check" banner) — we simply have none in our 252-ship corpus because no ship is at that stage right now
- `fruitRewarded`, `decision_user_message`, `is_fraud_rejection` are public per-ship
- `grantAmountCents` is OWNER-scoped (public page renders it only on your own timeline; anonymous /ships omits it) — the site's gold estimate remains a computed estimate, not a scrape
- NOT public anywhere: `assigned_user_id` (claim state), fraud trust scores, reviewer handles on in-flight ships
- Statistically observable despite being hidden: assignment itself is a *rate* — the 16%-claimed-within-24h fast lane vs. backlog lane is measurable from outcome timings

---

## USER-FACING OUTPUT — the complete display inventory

⏱ = live at page load; everything else snapshot (≤24h, timestamped).

### View 1: Project result card (main event)
1. **Pipeline stage diagram**: a horizontal stepper — Submitted → In Review → (Fraud check, shown if applicable) → Second Pass → Shipped — with the visitor's ship lit at its current stage ⏱. Each stage annotated with its median dwell time from historical data. This is the visual anchor of the page.
2. **Status banner ⏱**: stage-appropriate copy — under_review: "In the review queue"; pending_second_pass: "Accepted! Awaiting final second-pass check — rewards held, read-only, nothing to do"; decided: reviewer message + fruit rewarded + is_fraud_rejection note if set.
3. **Your file (reviewer's-eye view) ⏱**: name, thumbnail, type, level+fruit, submitted, days waiting, live hours, streak+multiplier, AI flag, repo/demo, README status, journal count + last entry.
4. **Queue position**: "#38 of ~107 by age" + claim-based caveat.
5. **Estimated review window**: range + median + basis + fast-lane note ("16% claimed within 24h").
6. **📊 Cohort survival chart** + wait percentile + approval odds (level×hours×type cell).
7. **Gold estimate**: hours × level rate (40/45/50/60 g/hr) × multiplier, line-by-line math.
8. **Similar ships panel**: recent decided ships with similar level+hours and their outcomes.
9. **All your projects ⏱** (from `/api/users/{you}`): each with its pipeline stage.
10. Links + provenance labels throughout.

### View 2: The full queue
Sortable/filterable table of every waiting ship (name, owner, level, fruit, type, hours, submitted, age, rank), reconstructed from public user/project enumeration. Rows link to result cards. Labeled: "reconstructed from public data — reviewer order is claim-based."

### View 3: Global dashboard — graph centerpiece (Chart.js, all data public)
- **📊 Pipeline funnel**: ships currently at each stage (107 in review · N in fraud · N in second pass) — the aggregate view of the stage diagram
- **📊 Queue depth over time** (line, full history — retroactively reconstructable: depth(date) = ships spanning that date)
- **📊 Decisions per day** (bar) — throughput rhythm, bursts/stalls
- **📊 Review front progression** (line) — submission-date the front has reached over time
- **📊 Arrivals per week** (bar) — queue growth
- **📊 Latency histogram** — the bimodal 3h-fast-lane / 25d-backlog shape is the story
- **📊 Survival curve** (Kaplan-Meier, all ships) — % decided vs days-since-submission
- **📊 Drain projection** (annotated line) + projected clear date
- **📊 Approval rates** (bar): by level, type, hours bucket, AI flag · **📊 needs_changes rate** (the resubmit loop is a real outcome) · **📊 fraud-escalation rate** (aggregate only, no individuals)
- **📊 Queue composition** (donut by level) + hours distribution
- **Recent decisions feed** (last 20, public) + **records** (longest waits, fastest approvals)
- Stat cards: depth now, oldest wait, front date, 7d/30d throughput, overall approval %

### View 4: How-it-works & accuracy
The pipeline model above (with the second-pass and fraud-squad explanations quoted from the actual UI copy), the claim-based reality, what we can never know (assignment, trust scores, reviewer availability), "distributions, not promises," etiquette note, future-work (a public claimed flag would collapse error bars).

### Not shown
No reviewer identities on in-flight ships, no single-date promises, no PII beyond Macondo's public pages, no individual fraud flags (aggregate rates only), no speculating about shadow-bans.

---

## Architecture

```
Nightly GitHub Action (cron ~03:00 UTC) — python3 scraper/
  1. Enumerate: /api/explore/people → /api/users/{u} (projects+meta)
     → /api/explore/projects → ID-range gap scan
  2. Per project: /api/projects/{id}/ships (1.1s spacing, 429→Retry-After backoff)
  3. Snapshot: all stage counts, queue ships+meta, every graph series
     (incl. reconstructed depth history), latency cohorts, front velocity,
     approval/needs-changes/fraud crosstabs, drain projection
  4. Commit data/snapshot.json; full NDJSON as workflow artifact

GitHub Pages (static, Chart.js via CDN, no build step)
  ├─ Dashboard + search (URL/ID/username)
  ├─ Result card: live fetch via proxy ⏱ + client-side ETA vs snapshot
  └─ Queue table + how-it-works

Cloudflare Worker (stateless, free tier)
  └─ GET-only: /api/projects/{id}, /ships, /api/users/{u} → CORS:*; 60s cache
```

## snapshot.json (drives every number and chart)
```json
{
  "generated_at": "...",
  "pipeline": { "under_review": 107, "pending_fraud_review": 0,
                "pending_second_pass": 0,
                "stage_dwell_days": {"under_review": 20, "second_pass": 3} },
  "queue": { "count":107, "oldest_days":46, "front_date":"2026-08-03",
             "ships":[{id,pid,name,owner,level,type,hours,created_at,rank_by_age}] },
  "series": { "queue_depth":[...], "decisions_daily":[...], "arrivals_weekly":[...],
              "front_progression":[...], "latency_hist":[...], "survival":[...],
              "drain_projection":{"daily_net":-0.8,"clears":"2026-10-28","path":[...]} },
  "latency": { "median_days":20, "p90_days":55, "fast_lane_pct":16,
               "fast_lane_median_h":3, "backlog_median_days":25,
               "by_submit_week":[{"week":"2026-08-24","dec14d":0.42}] },
  "outcomes": { "approval":0.90, "needs_changes":0.21, "rejected":0.08,
                "fraud_escalation":0.01, "by_level":{...}, "by_hours_bucket":{...},
                "by_type":{...}, "by_ai_flag":{...} },
  "gold": { "rates_by_level":{"1":40,"2":45,"3":50,"4":60} },
  "recent_decisions": [...], "records": {...}
}
```

## ETA algorithm (client-side, ~100 lines)
Live activeShip → decided/second-pass/fraud → show stage card (no ETA needed past approval). Else age-rank → days-till-front at current velocity → blend cohort survival → range (p25–p75) + median + fast-lane note. Gold = hours × level rate × multiplier. Approval odds from outcome cells (min 10 samples, else broader bucket, labeled).

## Repo layout (~/Desktop/mcondo)
```
macondo_check.sh, README.md          (existing — keep)
scraper/scrape.py                    (enumeration + scan + snapshot incl. all series)
scraper/stats.py                     (all math incl. depth-history reconstruction — TDD)
scraper/seed_ships.ndjson            (252-ship corpus — warm start + fixture)
scraper/seed_users.json
site/index.html, app.js, charts.js, style.css, how.html, queue.html
worker/proxy.js
.github/workflows/scrape.yml
```

## Implementation steps
1. **stats.py** (TDD: percentiles, two-lane split, front velocity, ETA range, outcome crosstabs, gold math, depth-history reconstruction, histogram binning, survival curve; edge cases: empty queue, tiny cells, unknown-stage ships; validate vs today's knowns: queue ~100, front ~Aug 3, fast-lane ~16%, approval ~90%)
2. **scrape.py** (rate-limited fetcher, 3 discovery methods, snapshot writer, --dry-run/--max-ids; smoke test on 20 IDs)
3. **worker/proxy.js** (CORS proxy; site degrades to snapshot-only)
4. **site/** (4 views + stepper + charts; local preview via `python3 -m http.server`)
5. **Actions workflow** (nightly scrape → commit snapshot → Pages)
6. **E2E with 906**: live stage = under_review, ETA range, queue table contains 7520, all charts render, dashboard matches tracker-script values

## Constraints & etiquette
~54 req/min nightly (~1.5k/day); honor Retry-After; no API keys; proxy GET-only/no auth passthrough; every estimate timestamped+caveated; charts show sample counts; CDN failure degrades to stat cards + tables.

## One-time manual step (yours)
Cloudflare account (free) + one `npx wrangler deploy` of worker/proxy.js — instructions included; site degrades to snapshot-only if skipped.

## Testing
stats.py unit tests vs seed corpus with today's validated answers · scraper --max-ids 20 smoke test · browser test of all 4 views, fallback path, stage stepper for each status value, every chart with real snapshot data, 906 numbers matching the tracker script.