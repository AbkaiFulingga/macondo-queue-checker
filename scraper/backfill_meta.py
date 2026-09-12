#!/usr/bin/env python3
"""One-off project-metadata backfill.

`enrich_with_project_meta` originally fetched a project only when it had a
ship *currently waiting*, so any ship decided before we first saw it waiting
never picked up its project's `type`.  Decided ships therefore split into a
thin typed set (95% approvals) and a fat untyped set (35% -- almost exactly the
needs-changes/rejected back-catalogue), which made the site's per-type numbers
incomparable with the overall ones.

This walks the committed corpus, fetches every project that isn't in
`project_meta.json` yet (plus the waiting ones, which refresh hours/multiplier),
writes the healed corpus back, and rebuilds the snapshot.  Afterwards the normal
nightly run only fetches projects it has genuinely never seen.

    python3 scraper/backfill_meta.py            # uses site/data/
    python3 scraper/backfill_meta.py --dry-run  # report the gap, fetch nothing
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scrape  # noqa: E402
import stats  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    data_dir = os.path.abspath(os.path.join(HERE, "..", "site", "data"))
    ap.add_argument("--data-dir", default=data_dir)
    ap.add_argument("--dry-run", action="store_true",
                    help="report how many projects are missing metadata, fetch nothing")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--gate-closed", action="store_true", default=True)
    ap.add_argument("--gate-open", dest="gate_closed", action="store_false")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    corpus = os.path.join(args.data_dir, "ships.ndjson")
    cache_path = os.path.join(args.data_dir, "project_meta.json")
    snap_path = os.path.join(args.data_dir, "snapshot.json")

    ships = scrape.load_corpus(corpus)
    cache = scrape.load_meta_cache(cache_path)
    # Seed the cache from metadata already sitting on the corpus, so the first
    # pass only pays for projects we have genuinely never fetched.
    seeded = 0
    for s in ships:
        pid = s.get("pid")
        if pid and pid not in cache and s.get("type"):
            cache[pid] = {k: s.get(k) for k in scrape.META_FILL_KEYS + scrape.META_LIVE_KEYS}
            seeded += 1
    pids = {s.get("pid") for s in ships}
    missing = sorted(p for p in pids if p not in cache)
    print(f"corpus: {len(ships)} ships / {len(pids)} projects"
          f" | cached: {len(cache)} (seeded {seeded}) | missing meta: {len(missing)}",
          file=sys.stderr)

    if args.dry_run:
        return 0

    t0 = time.time()
    fetch = scrape.Fetcher(quiet=args.quiet, workers=args.workers)
    # refresh_waiting=False: this pass exists only to heal the decided-ship
    # type gap; the waiting ships' hours/multiplier are the nightly run's job.
    ships = scrape.enrich_with_project_meta(fetch, ships, cache=cache,
                                           cache_path=cache_path,
                                           refresh_waiting=False)
    typed = sum(1 for s in ships if s.get("type"))
    print(f"enriched in {time.time() - t0:.0f}s ({fetch.n_ok} ok / {fetch.n_err} err)"
          f" | cache now {len(cache)} | ships with type: {typed}/{len(ships)}",
          file=sys.stderr)

    with open(corpus, "w") as f:
        for s in ships:
            f.write(json.dumps(s, separators=(",", ":")) + "\n")

    snap = stats.build_snapshot(ships, gate_closed=args.gate_closed)
    meta = {"n_projects": len(pids), "n_ships": len(ships),
            "fetch_stats": {"ok": fetch.n_ok, "err": fetch.n_err},
            "elapsed_s": round(time.time() - t0, 1),
            "meta_backfill": True}
    try:
        with open(snap_path) as f:
            prev = json.load(f).get("meta", {})
        for k in ("n_ids_probed", "n_deleted_or_unreachable", "n_live_projects_total"):
            if prev.get(k) is not None:
                meta[k] = prev[k]
    except (ValueError, OSError):
        pass
    snap["meta"] = meta
    with open(snap_path, "w") as f:
        json.dump(snap, f, separators=(",", ":"))

    for t in ("all", "software", "hardware"):
        b = snap["by_type"].get(t, {})
        o = b.get("outcomes", {})
        print(f"  {t:9s} queue={b.get('queue_count')} decided={o.get('n_decided')}"
              f" approval={o.get('approval', 0):.2%}"
              f" typed={b.get('type_coverage', {}).get('typed')}"
              f"/{b.get('type_coverage', {}).get('total')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
