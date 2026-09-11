# Macondo tracker — project 906 / ship 7520

**Live site: https://abkaifulingga.github.io/macondo-queue-checker/**
**Repo: https://github.com/AbkaiFulingga/macondo-queue-checker**

This workspace holds two things:

1. **`macondo_check.sh`** — personal CLI tracker for your own project.
2. **Macondo Queue Checker** — a public ETA website (scraper + static site + proxy), described below.

---

## Macondo Queue Checker (the website)

A public tool: paste any project URL/ID (or username) → live status, pipeline
stepper, queue position, estimated review window (range + median + fast-lane
odds), cohort survival, approval statistics, gold estimate, similar-ships
comparison — plus a global dashboard (queue depth history, throughput,
latency histogram, survival curve, arrivals, approval rates) and a sortable
full-queue table. All data from Macondo's public API; nightly snapshot via
GitHub Actions; per-visitor live status via an optional Cloudflare Worker
CORS proxy.

### Layout
```
scraper/
  stats.py        all math (lanes, front velocity, KM survival, depth
                  history, outcomes, gold, snapshot build) — TDD, 18 tests
  test_stats.py   python3 test_stats.py
  scrape.py       nightly scraper → site/data/snapshot.json
                  (--offline builds from seed corpus; --ids A:B smoke test)
  seed_ships.ndjson  252-ship warm-start corpus (2026-09-11)
site/
  index.html      dashboard + search + result card (eta.js, app.js, charts.js)
  queue.html      full sortable queue table
  how.html       methodology & honesty page
worker/
  proxy.js        Cloudflare Worker CORS proxy (see worker/DEPLOY.md)
.github/workflows/scrape.yml   nightly cron: tests → scrape → commit → Pages
```

### Local preview
```bash
python3 scraper/test_stats.py     # stats tests
node site/eta.test.js            # client ETA tests
python3 scraper/scrape.py --offline   # build snapshot from seed
python3 -m http.server 8765 -d site   # then open http://127.0.0.1:8765
```

### Deploy to production
1. Push to GitHub; enable Pages (deploy-from-branch or the workflow's Pages artifacts).
2. Nightly Actions run keeps `site/data/snapshot.json` fresh.
3. Optional (for live status): deploy `worker/proxy.js` per `worker/DEPLOY.md`,
   then set `PROXY` in `site/app.js`. Without it, the site uses snapshot-only
   status with a clear label — everything else works identically.

### Test status (2026-09-11)
- stats: 18/18 · eta.js: 9/9 · browser E2E: dashboard, search 906
  (snapshot fallback), queue page + filter, how page all verified.

---

## Personal CLI tracker

`macondo_check.sh` watches the review status of your Macondo project.

## Usage

```bash
./macondo_check.sh daily    # 2 requests, instant: status + hours + decision check
./macondo_check.sh queue    # ~1500 requests, ~10 min: full queue approximation
./macondo_check.sh panel A B   # scan project IDs A..B only (debug)
```

## What each mode gives you

- **daily** — logs one line to `~/.macondo_tracker/daily.log`. The moment
  `status` leaves `under_review` (or `reviewed_at` gets set), it prints the
  decision: fruit rewarded + reviewer message. Run it often.
- **queue** — approximates the review queue from public data:
  - `queue_n` — under-review ships in the scan window (undercounts by
    ~10–15%: ships on old projects are outside the window)
  - `front_date` — submission date of the oldest waiting ship = where
    reviewers currently are in the backlog
  - `mine.rank_by_age` — 7520's position by submission age (lower = sooner,
    if processed oldest-first; but reviewers claim, so it's approximate)
  - `decisions_last_7d` — reviewer throughput this week
  - Appends `date,queue_n,front_date,rank,decisions_7d` to `history.csv` so
    trends are visible over time.

## Scheduling (crontab -e)

```
0 9 * * *  ~/Desktop/mcondo/macondo_check.sh daily
0 10 * * 6 ~/Desktop/mcondo/macondo_check.sh queue
```

## How to read the trend (history.csv)

- `front_date` advancing ≈ queue draining. If it stalls for a week, throughput
  is down.
- `decisions_7d` × weeks remaining ≈ time to drain. At 7/week with a 100-ship
  queue: ~14 weeks. At 30/week: ~3 weeks.
- Your expected review date ≈ when `front_date` passes `2026-08-31`
  (7520's submission date). Watch it close in on that date.

## Notes

- Rate-limit safe: 0.3s between requests (limit is 20/5s, 60/min).
- 404s during scans are normal (deleted projects).
- No API key needed — everything it reads is public.
- If a fast-claim reviewer picks 7520 out of order, daily mode will catch it
  immediately even if queue mode suggests otherwise.
