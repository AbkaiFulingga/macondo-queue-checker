#!/usr/bin/env python3
"""Bounded completeness audit of the committed corpus against the live API.

Answers two questions with evidence:
  A. is the project-ID space still covered?  (probe above the old frontier)
  B. are we missing ship records or whole projects?
     - re-fetch /ships for multi-ship projects and diff the ship-ID sets
     - sweep IDs spread across the space; any live project with ships that is
       NOT in our corpus is a project we failed to discover

Not a scraper: read-only, bounded, polite (shares the normal 1.1s spacing).
"""

import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/Users/guxunfeng/Desktop/mcondo/scraper")
import scrape  # noqa: E402

CORPUS = "/Users/guxunfeng/Desktop/mcondo/site/data/ships.ndjson"
SNAPSHOT = "/Users/guxunfeng/Desktop/mcondo/site/data/snapshot.json"
OLD_FRONTIER = 17451          # highest ID probed by the 2026-09-12 full sweep
SWEEP_N = 260                 # IDs sampled across the whole space
ABOVE = 140                   # IDs probed above the old frontier
# measured by the 2026-09-12 full sweep over IDs 1..OLD_FRONTIER
SWEEP_LIVE, SWEEP_DELETED = 12210, 5241


def record_coverage(report):
    """Merge this audit's tail measurement with the last full sweep and store a
    single dated coverage block on the snapshot, so the dashboard's project
    counts are measured values with a date rather than hardcoded constants."""
    tail = report["A_live_ids_probed_above"]
    n_tail = ABOVE
    ids_probed = OLD_FRONTIER + n_tail
    live = SWEEP_LIVE + len(tail)
    deleted = SWEEP_DELETED + (n_tail - len(tail))
    cov = {
        "ids_probed": ids_probed,
        "live_projects_total": live,
        "deleted_or_unreachable": deleted,
        "max_live_pid": report["A_max_live_pid_seen"],
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "method": f"full sweep 1..{OLD_FRONTIER} (2026-09-12) + audit probe "
                  f"{OLD_FRONTIER + 1}..{ids_probed}",
        "audit": {
            "ids_sampled": report["B2_ids_sampled"],
            "undiscovered_projects": len(report["B2_undiscovered_projects"]),
            "multi_ship_discrepancies": len(report["B1_discrepancies"]),
            "new_projects_with_ships": [p for p, _ in report["A_new_projects_with_ships_above_frontier"]],
        },
    }
    with open(SNAPSHOT) as f:
        snap = json.load(f)
    snap.setdefault("meta", {})["coverage"] = cov
    with open(SNAPSHOT, "w") as f:
        json.dump(snap, f, separators=(",", ":"))
    print("\nrecorded meta.coverage:", json.dumps(cov))


def main():
    ships = [json.loads(l) for l in open(CORPUS) if l.strip()]
    our_ship_ids = {s["id"] for s in ships}
    our_pids = {s["pid"] for s in ships}
    by_pid = {}
    for s in ships:
        by_pid.setdefault(s["pid"], []).append(s)

    f = scrape.Fetcher(quiet=False)
    report = {}

    # ---- A. frontier: any live project above what we swept?
    lo = OLD_FRONTIER - 10
    hi = OLD_FRONTIER + ABOVE
    print(f"\n[A] probing {lo}..{hi} for projects above the old frontier", file=sys.stderr)
    live_above, with_ships_above = [], []
    for pid in range(lo, hi + 1):
        code, data = f.get_json(f"/projects/{pid}")
        if code == 200 and isinstance(data, dict):
            live_above.append(pid)
            if pid > OLD_FRONTIER:
                c2, sh = f.get_json(f"/projects/{pid}/ships")
                n = len(sh) if isinstance(sh, list) else 0
                if n:
                    with_ships_above.append((pid, n))
    report["A_live_ids_probed_above"] = [p for p in live_above if p > OLD_FRONTIER]
    report["A_new_projects_with_ships_above_frontier"] = with_ships_above
    report["A_max_live_pid_seen"] = max(live_above) if live_above else None

    # ---- B1. ship-history completeness: diff /ships against the corpus
    multi = sorted([p for p, v in by_pid.items() if len(v) > 1],
                   key=lambda p: -len(by_pid[p]))[:60]
    print(f"\n[B1] re-fetching /ships for {len(multi)} multi-ship projects", file=sys.stderr)
    missing_ships, checked = [], 0
    for pid in multi:
        code, sh = f.get_json(f"/projects/{pid}/ships")
        if code != 200 or not isinstance(sh, list):
            continue
        checked += 1
        api_ids = {r.get("id") for r in sh}
        ours = {s["id"] for s in by_pid[pid]}
        gone = ours - api_ids          # we hold ships the API no longer lists
        new = api_ids - ours           # the API lists ships we never captured
        if new or gone:
            missing_ships.append({"pid": pid, "api": len(api_ids), "ours": len(ours),
                                  "never_captured": sorted(new)[:5],
                                  "no_longer_listed": sorted(gone)[:5]})
    report["B1_multi_ship_projects_checked"] = checked
    report["B1_discrepancies"] = missing_ships

    # ---- B2. whole-project coverage: sweep the ID space
    step = max(1, OLD_FRONTIER // SWEEP_N)
    sample = list(range(1, OLD_FRONTIER + 1, step))
    print(f"\n[B2] sweeping {len(sample)} IDs (every {step}th) for undiscovered projects",
          file=sys.stderr)
    live_seen, with_ships, missed, empty_ships = 0, 0, [], 0
    for pid in sample:
        code, data = f.get_json(f"/projects/{pid}")
        if code != 200 or not isinstance(data, dict):
            continue
        live_seen += 1
        code2, sh = f.get_json(f"/projects/{pid}/ships")
        n = len(sh) if isinstance(sh, list) else 0
        if n == 0:
            empty_ships += 1
            continue
        with_ships += 1
        if pid not in our_pids:
            missed.append({"pid": pid, "ships": n, "name": data.get("name")})
    report["B2_ids_sampled"] = len(sample)
    report["B2_live_projects_seen"] = live_seen
    report["B2_live_with_ships"] = with_ships
    report["B2_live_without_ships"] = empty_ships
    report["B2_undiscovered_projects"] = missed
    report["requests"] = {"ok": f.n_ok, "err": f.n_err}

    print("\n===== AUDIT RESULT =====")
    print(json.dumps(report, indent=1))
    if "--record" in sys.argv:
        record_coverage(report)


if __name__ == "__main__":
    main()
