#!/usr/bin/env python3
"""Macondo queue snapshot scraper.

Enumerates projects via three public discovery paths, fetches every project's
ship history, enriches ships with project metadata, and writes the snapshot
JSON consumed by the static site (via stats.build_snapshot).

Discovery triangulation:
  1. /api/explore/people (paginated) -> every user's username
  2. /api/users/{username} -> that user's project IDs + metadata
  3. /api/explore/projects (cursor-paginated shipped gallery)
  4. ID-range scan around the known frontier for gaps (deleted-project 404s)

Etiquette: 1.1s spacing (~54 req/min, under the 60/min limit), exponential
backoff honoring Retry-After on 429/5xx, single User-Agent with contact.

Usage:
  python3 scrape.py                    # full run
  python3 scrape.py --max-users 5      # smoke test: first 5 users only
  python3 scrape.py --ids 15800:15830  # scan an explicit project-ID range
  python3 scrape.py --offline          # build snapshot from seed corpus only
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stats  # noqa: E402

BASE = "https://macondo.hackclub.com/api"
UA = "macondo-queue-checker/0.1 (+https://github.com/AbkaiFulingga/macondo-queue-checker; nightly snapshot; contact via GitHub)"
SPACING = 1.1          # seconds between requests, single worker (~54/min < 60/min limit)
LAST_PEEK_FRONTIER = 30000  # project IDs above this are assumed nonexistent


class Fetcher:
    def __init__(self, spacing=None, dry=False, quiet=False, workers=1):
        self.workers = max(1, workers)
        # shared throttle keeps TOTAL request rate under the API limit:
        # spacing shrinks as workers are added so workers * rate <= ~1.8 req/s
        self.spacing = spacing if spacing is not None else min(SPACING, 1.1 / self.workers)
        self.dry = dry
        self.quiet = quiet
        self.last = 0.0
        self.n_ok = 0
        self.n_err = 0
        self.lock = None  # created lazily under threading

    def log(self, msg):
        if not self.quiet:
            print(msg, file=sys.stderr, flush=True)

    def _throttle(self):
        wait = self.spacing - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)

    def _get_once(self, path, retries):
        req = urllib.request.Request(BASE + path,
                                     headers={"User-Agent": UA, "Accept": "application/json"})
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read()
                    self.n_ok += 1
                    try:
                        return r.status, json.loads(body)
                    except json.JSONDecodeError:
                        return r.status, None
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    retry_after = e.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else 2 ** (attempt + 1)
                    self.log(f"  {e.code} on {path}; backoff {delay}s")
                    time.sleep(delay)
                    continue
                self.n_err += 1
                return e.code, None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                self.n_err += 1
                self.log(f"  network error on {path}: {e}")
                return 0, None
        return 0, None

    def get_json(self, path, retries=3):
        """GET {BASE}{path} -> (status, parsed_json_or_None). Throttled; thread-safe
        when a threading lock is attached (see map_workers)."""
        if self.dry:
            return 204, None
        if getattr(self, "lock", None):
            with self.lock:
                self._throttle()
                self.last = time.monotonic()
        else:
            self._throttle()
            self.last = time.monotonic()
        return self._get_once(path, retries)


def map_workers(fetch, fn, items, desc=""):
    """Run fn(item) over items with fetch.workers threads, sharing one throttle.
    Results returned in input order; errors are fn's responsibility (return None)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    fetch.lock = threading.Lock()
    out = [None] * len(items)
    with ThreadPoolExecutor(max_workers=fetch.workers) as ex:
        futures = {ex.submit(fn, it): i for i, it in enumerate(items)}
        for fut, i in futures.items():
            out[i] = fut.result()
    if desc and not fetch.quiet:
        done = sum(1 for r in out if r is not None)
        print(f"  {desc}: {done}/{len(items)} ok", file=sys.stderr, flush=True)
    return out


# ------------------------------------------------------------------ helpers


def cursor_encode(cur):
    return urllib.parse.quote(str(cur), safe="")


def project_meta_from_user(payload_projects, owner_username):
    """User-endpoint project entries -> {pid: metadata} map."""
    out = {}
    for p in payload_projects or []:
        out[p["id"]] = {
            "name": p.get("name"),
            "level": str(p.get("level")) if p.get("level") else None,
            "type": p.get("type"),
            "fruit": p.get("fruit"),
            "owner": owner_username,
            "streak": p.get("project_streak_days"),
        }
    return out


# ------------------------------------------------------------- discovery


def discover_users(fetch, max_users=None):
    """Paginate /explore/people -> list of usernames."""
    usernames = []
    cursor = None
    while True:
        path = "/explore/people?limit=60" + (f"&cursor={cursor_encode(cursor)}" if cursor else "")
        code, data = fetch.get_json(path)
        if code != 200 or not isinstance(data, dict):
            break
        items = data.get("items") or []
        usernames.extend(i["username"] for i in items if i.get("username"))
        if max_users and len(usernames) >= max_users:
            usernames = usernames[:max_users]
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return usernames


def projects_from_users(fetch, usernames):
    """/users/{u} for each -> (project_ids set, meta map, user->pids)."""
    pids = set()
    meta = {}
    by_user = {}

    def one(u):
        code, data = fetch.get_json(f"/users/{urllib.parse.quote(u)}")
        if code != 200 or not isinstance(data, dict):
            return None
        ups = data.get("projects") or []
        m = project_meta_from_user(ups, u)
        return u, m

    for res in map_workers(fetch, one, usernames, desc="user lookups"):
        if not res:
            continue
        u, m = res
        meta.update(m)
        ids = set(m)
        pids |= ids
        by_user[u] = sorted(ids)
    return pids, meta, by_user


def projects_from_gallery(fetch, max_pages=40):
    """Cursor-paginate /explore/projects (shipped gallery) -> (pids, meta)."""
    pids = set()
    meta = {}
    cursor = None
    for _ in range(max_pages):
        path = "/explore/projects?limit=60" + (f"&cursor={cursor_encode(cursor)}" if cursor else "")
        code, data = fetch.get_json(path)
        if code != 200 or not isinstance(data, dict):
            break
        items = data.get("items") or []
        for it in items:
            pids.add(it["id"])
            owner = (it.get("owner") or {}).get("username")
            meta[it["id"]] = {
                "name": it.get("name"),
                "level": str(it.get("level")) if it.get("level") else None,
                "type": it.get("type"),
                "fruit": it.get("fruit"),
                "owner": owner,
                "streak": it.get("project_streak_days"),
            }
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return pids, meta


def find_frontier(fetch, known_max):
    """Climb in 500-ID rungs until 3 consecutive dead rungs."""
    hi = known_max
    cand = known_max + 500
    dead = 0
    while cand < LAST_PEEK_FRONTIER and dead < 3:
        code, _ = fetch.get_json(f"/projects/{cand}/ships")
        if code == 200:
            hi = cand
            dead = 0
        else:
            dead += 1
        cand += 500
    return hi


def scan_id_range(fetch, lo, hi):
    """Probe every project ID in [lo, hi]; return (pids_with_ships, ships)."""
    pids = set()
    ships = []

    def one(pid):
        code, data = fetch.get_json(f"/projects/{pid}/ships")
        if code != 200 or not data:
            return None
        return [(s | {"pid": pid}) for s in data]

    results = map_workers(fetch, one, range(lo, hi + 1), desc=f"range {lo}..{hi}")
    for chunk in results:
        if chunk:
            pids.add(chunk[0]["pid"])
            ships.extend(chunk)
    return pids, ships


# ---------------------------------------------------------------- pipeline


def fetch_all_ships(fetch, pids, meta):
    """Fetch /projects/{pid}/ships for every discovered project; enrich."""
    def one(pid):
        code, data = fetch.get_json(f"/projects/{pid}/ships")
        if code != 200 or not data:
            return []
        m = meta.get(pid) or {}
        out = []
        for s in data:
            s["pid"] = pid
            # explicit assignment: fresh /ships payload has none of these keys
            s["name"] = m.get("name")
            s["owner"] = m.get("owner")
            s["level"] = m.get("level")
            s["type"] = m.get("type")
            s["hours"] = s.get("hackatime_hours")
            out.append(s)
        return out

    results = map_workers(fetch, one, sorted(pids), desc="ships")
    all_ships = [s for chunk in results if chunk for s in chunk]
    return all_ships


def dedupe_ships(all_ships):
    seen = {}
    for s in all_ships:
        seen[s["id"]] = s  # later wins; scan-order is deterministic
    return list(seen.values())


# ------------------------------------------------------------------- main


def enrich_with_project_meta(fetch, ships):
    """Fill name/level/type/owner on ship records via /projects/{pid}.
    Only pids missing metadata are fetched (~1 req each, still polite)."""
    need = {s.get("pid") for s in ships
            if s.get("status") in ("under_review", "pending_second_pass",
                                   "pending_fraud_review")
            and not s.get("name")}
    if not need:
        return ships

    def one(pid):
        code, data = fetch.get_json(f"/projects/{pid}")
        if code != 200 or not isinstance(data, dict):
            return None
        return pid, {
            "name": data.get("name"),
            "level": str(data.get("level")) if data.get("level") else None,
            "type": data.get("type"),
            "owner": (data.get("owner") or {}).get("username") if isinstance(data.get("owner"), dict) else None,
            "hours": data.get("public_total_hours"),
        }

    fetched = {}
    for res in map_workers(fetch, one, sorted(need), desc="meta enrichment"):
        if res:
            fetched[res[0]] = res[1]
    for s in ships:
        m = fetched.get(s.get("pid"))
        if m:
            # fill only when missing OR null (setdefault skips null-valued keys)
            for key in ("name", "level", "type", "owner"):
                if not s.get(key):
                    s[key] = m[key]
    return ships


def load_corpus(path):
    ships = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                if line.strip():
                    ships.append(json.loads(line))
    return ships


def run(args):
    fetch = Fetcher(dry=args.dry_run, quiet=args.quiet, workers=args.workers)
    t0 = time.time()
    out_path = args.out or os.path.join(HERE, "..", "site", "data", "snapshot.json")
    out_path = os.path.abspath(out_path)
    corpus_path = os.path.join(os.path.dirname(out_path), "ships.ndjson")
    baseline = load_corpus(corpus_path)
    baseline_by_pid = {}
    for s in baseline:
        baseline_by_pid.setdefault(s.get("pid"), []).append(s)

    # ---- discovery
    if args.ids:
        lo_s, hi_s = args.ids.split(":")
        lo, hi = int(lo_s), int(hi_s)
        fetch.log(f"scanning explicit ID range {lo}..{hi}")
        pids, all_ships = scan_id_range(fetch, lo, hi)
        meta = {}
    elif args.nightly and baseline:
        # Fast nightly path (a few hundred requests): refresh only what can
        # have changed — waiting ships, the recent-ID window up to the live
        # frontier, and the shipped gallery (catches resubmits + fresh
        # decisions). Decided ships below the window are frozen history;
        # reuse them from the baseline corpus.
        fetch.log(f"nightly delta over baseline ({len(baseline)} ships)")
        waiting_pids = {s.get("pid") for s in baseline
                        if s.get("status") in ("under_review", "pending_second_pass",
                                               "pending_fraud_review")}
        gpids, gmeta = projects_from_gallery(fetch)
        meta = gmeta
        # recent-ID window: from just below the newest baseline pid up to the
        # LIVE frontier (climb until dead rungs; typically a few hundred IDs)
        newest = max((s.get("pid") or 0) for s in baseline)
        lo = max(1, newest - 100)
        frontier = find_frontier(fetch, max(lo, newest))
        fetch.log(f"recent window {lo}..{frontier}")
        spids, sships = (scan_id_range(fetch, lo, frontier) if frontier >= lo else (set(), []))
        refresh = waiting_pids | set(spids)
        fetch.log(f"refreshing {len(refresh)} projects (waiting={len(waiting_pids)}, recent-window hits={len(spids)})")
        fresh = fetch_all_ships(fetch, sorted(refresh), meta)
        # merge: fresh wins for refreshed pids; frozen baseline for everything else
        fresh_by_pid = {}
        for s in fresh:
            fresh_by_pid.setdefault(s.get("pid"), []).append(s)
        all_ships = []
        for pid, ships in baseline_by_pid.items():
            all_ships.extend(fresh_by_pid.pop(pid, ships))
        for ships in fresh_by_pid.values():
            all_ships.extend(ships)
        # new gallery projects not in baseline
        new_gallery = [p for p in gpids if p not in baseline_by_pid and p not in fresh_by_pid]
        if new_gallery:
            fetch.log(f"+ {len(new_gallery)} new gallery projects")
            all_ships += fetch_all_ships(fetch, new_gallery, meta)
        pids = {s.get("pid") for s in all_ships}
    else:
        usernames = discover_users(fetch, args.max_users)
        fetch.log(f"discovered {len(usernames)} users")
        upids, umeta, _ = projects_from_users(fetch, usernames)
        fetch.log(f"user path: {len(upids)} projects")
        gpids, gmeta = projects_from_gallery(fetch)
        fetch.log(f"gallery path: {len(gpids)} shipped projects")
        meta = {**gmeta, **umeta}  # user meta wins (fresher for unshipped)

        # gap scan around the frontier for projects both paths miss
        known_max = max(list(upids) + list(gpids) + [0])
        frontier = find_frontier(fetch, known_max)
        fetch.log(f"frontier: {frontier}")
        lo = max(1, frontier - 1500)
        spids, sships = scan_id_range(fetch, lo, frontier)
        fetch.log(f"range scan {lo}..{frontier}: {len(spids)} projects, {len(sships)} ships")

        pids = upids | gpids | spids
        all_ships = sships
        fetch.log(f"union: {len(pids)} projects")
        # fetch ships for user/gallery projects not covered by the range scan
        todo = [p for p in (upids | gpids) if not (lo <= p <= frontier)]
        if todo:
            fetch.log(f"fetching ships for {len(todo)} out-of-range projects")
            all_ships += fetch_all_ships(fetch, todo, meta)
        # seed-baseline ships from any earlier run supplement whatever the
        # user/gallery paths didn't re-see this time (deleted users etc.)
        if baseline:
            seen_pids = {s.get("pid") for s in all_ships}
            for pid, ships in baseline_by_pid.items():
                if pid not in seen_pids:
                    all_ships.extend(ships)

    all_ships = dedupe_ships(all_ships)
    fetch.log(f"unique ships: {len(all_ships)}")
    # waiting ships without metadata get one /projects/{pid} each so the
    # queue table shows names/levels/owners
    all_ships = enrich_with_project_meta(fetch, all_ships)

    # ---- snapshot
    snap = stats.build_snapshot(all_ships)
    snap["meta"] = {
        "n_projects": len(pids),
        "n_ships": len(all_ships),
        "fetch_stats": {"ok": fetch.n_ok, "err": fetch.n_err},
        "elapsed_s": round(time.time() - t0, 1),
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(snap, f, separators=(",", ":"))
    # full corpus as a sidecar for diffing/debugging AND the next nightly delta
    with open(corpus_path, "w") as f:
        for s in all_ships:
            f.write(json.dumps(s, separators=(",", ":")) + "\n")

    fetch.log(f"snapshot written: {out_path}")
    fetch.log(f"queue={snap['queue']['count']} oldest={snap['queue']['oldest_days']} "
              f"front={snap['queue']['front_date']} median={snap['latency']['median_days']}d "
              f"fast_lane={snap['latency']['fast_lane_pct']}")
    return snap


def run_offline(out_path):
    """Build snapshot from the seed corpus (no network)."""
    seed = os.path.join(HERE, "seed_ships.ndjson")
    ships = []
    with open(seed) as f:
        for line in f:
            if line.strip():
                ships.append(json.loads(line))
    snap = stats.build_snapshot(ships)
    snap["meta"] = {"n_projects": len({s.get("pid") for s in ships}),
                    "n_ships": len(ships), "offline_seed": True}
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(snap, f, separators=(",", ":"))
    print(f"offline snapshot: {out_path} (queue={snap['queue']['count']})", file=sys.stderr)
    return snap


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-users", type=int, default=None, help="smoke test: limit user enumeration")
    ap.add_argument("--ids", type=str, default=None, help="explicit ID range LO:HI instead of discovery")
    ap.add_argument("--out", type=str, default=None, help="output snapshot path")
    ap.add_argument("--offline", action="store_true", help="build from seed corpus, no network")
    ap.add_argument("--dry-run", action="store_true", help="enumerate but do not fetch")
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent fetch workers sharing one throttle (keeps total rate under limits)")
    ap.add_argument("--nightly", action="store_true",
                    help="fast delta mode: refresh waiting ships + recent window only (needs ships.ndjson corpus)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.offline:
        run_offline(args.out or os.path.join(HERE, "..", "site", "data", "snapshot.json"))
        return

    snap = run(args)
    # summary for humans
    print(json.dumps({
        "queue_count": snap["queue"]["count"],
        "front_date": snap["queue"]["front_date"],
        "oldest_days": snap["queue"]["oldest_days"],
        "median_days": snap["latency"]["median_days"],
        "fast_lane_pct": snap["latency"]["fast_lane_pct"],
        "decisions_14d": snap["drain"]["decisions_per_day_14d"],
    }, indent=2))


if __name__ == "__main__":
    main()
