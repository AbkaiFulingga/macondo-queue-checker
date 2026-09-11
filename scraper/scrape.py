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
UA = "macondo-queue-checker/0.1 (+https://github.com/; nightly snapshot; contact via GitHub)"
SPACING = 1.1          # seconds between requests (~54/min < 60/min limit)
LAST_PEEK_FRONTIER = 30000  # project IDs above this are assumed nonexistent


class Fetcher:
    def __init__(self, spacing=SPACING, dry=False, quiet=False):
        self.spacing = spacing
        self.dry = dry
        self.quiet = quiet
        self.last = 0.0
        self.n_ok = 0
        self.n_err = 0

    def log(self, msg):
        if not self.quiet:
            print(msg, file=sys.stderr, flush=True)

    def _throttle(self):
        wait = self.spacing - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)

    def get_json(self, path, retries=3):
        """GET {BASE}{path} -> (status, parsed_json_or_None). Throttled."""
        if self.dry:
            return 204, None
        self._throttle()
        self.last = time.monotonic()
        url = BASE + path
        for attempt in range(retries):
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
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
    for u in usernames:
        code, data = fetch.get_json(f"/users/{urllib.parse.quote(u)}")
        if code != 200 or not isinstance(data, dict):
            continue
        ups = data.get("projects") or []
        m = project_meta_from_user(ups, u)
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
    for pid in range(lo, hi + 1):
        code, data = fetch.get_json(f"/projects/{pid}/ships")
        if code != 200 or not data:
            continue
        pids.add(pid)
        for s in data:
            s["pid"] = pid
            ships.append(s)
    return pids, ships


# ---------------------------------------------------------------- pipeline


def fetch_all_ships(fetch, pids, meta):
    """Fetch /projects/{pid}/ships for every discovered project; enrich."""
    all_ships = []
    for i, pid in enumerate(sorted(pids)):
        if i and i % 200 == 0:
            fetch.log(f"  ships {i}/{len(pids)}...")
        code, data = fetch.get_json(f"/projects/{pid}/ships")
        if code != 200 or not data:
            continue
        m = meta.get(pid) or {}
        for s in data:
            s["pid"] = pid
            s.setdefault("name", m.get("name"))
            s.setdefault("owner", m.get("owner"))
            s.setdefault("level", m.get("level"))
            s.setdefault("type", m.get("type"))
            s.setdefault("hours", s.get("hackatime_hours"))
            all_ships.append(s)
    return all_ships


def dedupe_ships(all_ships):
    seen = {}
    for s in all_ships:
        seen[s["id"]] = s  # later wins; scan-order is deterministic
    return list(seen.values())


# ------------------------------------------------------------------- main


def run(args):
    fetch = Fetcher(dry=args.dry_run, quiet=args.quiet)
    t0 = time.time()

    # ---- discovery
    if args.ids:
        lo_s, hi_s = args.ids.split(":")
        lo, hi = int(lo_s), int(hi_s)
        fetch.log(f"scanning explicit ID range {lo}..{hi}")
        pids, ships = scan_id_range(fetch, lo, hi)
        all_ships = ships
        meta = {}
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
        # ensure ships for range-scanned projects that had empty ship lists
        # are represented (they were already fetched in the scan itself)

    all_ships = dedupe_ships(all_ships)
    fetch.log(f"unique ships: {len(all_ships)}")

    # ---- snapshot
    snap = stats.build_snapshot(all_ships)
    snap["meta"] = {
        "n_projects": len(pids),
        "n_ships": len(all_ships),
        "fetch_stats": {"ok": fetch.n_ok, "err": fetch.n_err},
        "elapsed_s": round(time.time() - t0, 1),
    }

    out_path = args.out or os.path.join(HERE, "..", "site", "data", "snapshot.json")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(snap, f, separators=(",", ":"))
    # full corpus as a sidecar for diffing/debugging
    corpus_path = os.path.join(os.path.dirname(os.path.abspath(out_path)), "ships.ndjson")
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
