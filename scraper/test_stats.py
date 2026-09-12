"""Tests for scraper/stats.py.

Two layers:
- Synthetic fixtures with hand-computed answers (exact math, edge cases).
- Seed-corpus invariants (values cross-checked against the 2026-09-11 manual
  analysis: queue ~100 in the 15800-16480 panel, front ~2026-08-03,
  fast-lane ~16%, backlog median ~25d, overall approval ~90% of decided).

Run: python3 test_stats.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stats  # noqa: E402

SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_ships.ndjson")

# ---------------------------------------------------------------- fixtures


def ship(sid, created, reviewed=None, status="shipped", hours=10.0, pid=1,
         level="2", type_="software", fraud=False, ai=False):
    """created/reviewed as day-offsets from a fixed epoch, 2026-01-01 UTC."""
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def d(off):
        return (epoch + timedelta(days=off)).strftime("%Y-%m-%dT%H:%M:%SZ")

    s = {"id": sid, "pid": pid, "status": status, "created_at": d(created),
         "hours": hours, "level": level, "type": type_, "is_fraud_rejection": fraud}
    if reviewed is not None:
        s["reviewed_at"] = d(reviewed)
    return s


# ---------------------------------------------------------------- helpers


def approx(a, b, tol=0.02):
    assert abs(a - b) <= tol, f"{a} !~ {b} (tol {tol})"


def run(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
        return True
    except AssertionError as e:
        print(f"  FAIL  {name}: {e}")
        return False


# ---------------------------------------------------------------- tests


def test_latency_basic():
    # decided ships with known latencies in days — none under 24h
    ships = [
        ship(1, 0, 2.0),
        ship(2, 0, 5.0),
        ship(3, 0, 10.0),
        ship(4, 0, 25.0),
        ship(5, 0, 40.0),
    ]
    lat = stats.latency_stats(ships)
    approx(lat["median_days"], 10.0, 0.01)
    assert lat["n_decided"] == 5
    assert lat["fast_lane_pct"] == 0.0  # nothing under 24h
    assert lat["backlog_median_days"] == 10.0  # all slow-lane


def test_two_lane_split():
    # 1 of 5 decided fast (<24h), 4 slow with latencies 3,4,5,6d
    ships = [
        ship(1, 0, 0.1),          # 2.4h -> fast lane
        ship(2, 0, 3.0),
        ship(3, 0, 4.0),
        ship(4, 0, 5.0),
        ship(5, 0, 6.0),
    ]
    lat = stats.latency_stats(ships)
    approx(lat["fast_lane_pct"], 0.2, 0.001)
    approx(lat["fast_lane_median_h"], 2.4, 0.01)
    approx(lat["backlog_median_days"], 4.5, 0.01)
    # overall median of [0.1,3,4,5,6] = 4d
    approx(lat["median_days"], 4.0, 0.01)


def test_percentiles_simple():
    # linear interpolation convention: p50 of 1..10 -> 5.5; p90 -> idx 8.1
    vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    approx(stats.percentile(vals, 0.5), 5.5, 0.001)
    approx(stats.percentile(vals, 0.9), 9.1, 0.001)
    approx(stats.percentile(vals, 0.0), 1, 0.001)
    approx(stats.percentile(vals, 1.0), 10, 0.001)


def test_queue_stats_and_front():
    # queue: two waiting ships, 10 and 40 days old
    now = datetime(2026, 2, 10, tzinfo=timezone.utc)  # day 40
    q_ships = [
        ship(10, 0, status="under_review"),
        ship(11, 30, status="under_review"),
    ]
    q = stats.queue_stats(q_ships, now=now)
    assert q["count"] == 2
    approx(q["oldest_days"], 40.0, 0.01)
    assert q["front_date"] == "2026-01-01"
    # rank by age (1 = oldest)
    assert stats.rank_by_age(q_ships, 10, now=now) == 1
    assert stats.rank_by_age(q_ships, 11, now=now) == 2
    assert stats.rank_by_age(q_ships, 999, now=now) is None


def test_front_velocity():
    # decisions: front advanced from submitted-day 0 to submitted-day 8 over
    # wall days 20..30 (10 wall days). velocity = 0.8 submission-days/wall-day
    decided = [
        ship(1, 0, 20),  # submitted day0, decided wall-day 20
        ship(2, 8, 30),  # submitted day8, decided wall-day 30
    ]
    now = datetime(2026, 1, 31, tzinfo=timezone.utc).timestamp()  # wall day 30
    v = stats.front_velocity(decided, window_days=45, now=now)
    approx(v["subm_days_per_wall_day"], 8 / 10, 0.01)


def test_eta_range():
    # Constructed so velocity is exactly 1.0 subm-day/wall-day:
    # decisions: ship1 submitted day0 decided wall-day20; ship2 submitted day30
    # decided wall-day50 (30 subm-days over 30 wall-days). Front (oldest
    # waiting) is day30 — my ship — so distance-to-front = 0 -> central ~0.
    # Instead test the in-queue case: front at day 0 (waiting ship), me at
    # day 30, velocity 1.0 -> ~30 wall days to reach me.
    decided = [ship(1, 0, 20), ship(2, 30, 50)]
    waiting = [ship(3, 0, status="under_review"),
               ship(4, 15, status="under_review"),
               ship(5, 30, status="under_review")]
    mine = ship(5, 30, status="under_review")
    now = datetime(2026, 2, 20, tzinfo=timezone.utc)  # wall day 50
    eta = stats.eta_range(mine, waiting, decided, now=now)
    v = stats.front_velocity(decided, window_days=30, now=now.timestamp())
    approx(v["subm_days_per_wall_day"], 1.0, 0.001)
    approx(eta["central_days"], 30.0, 2.0)
    assert eta["range_low_d"] <= eta["central_days"] <= eta["range_high_d"]
    assert eta["rank_by_age"] == 3  # oldest of the three waiting ships


def test_eta_decided_ship_short_circuits():
    mine = ship(1, 0, 5, status="shipped")
    out = stats.eta_for_ship(mine, [], [], now=datetime(2026, 1, 10, tzinfo=timezone.utc))
    assert out["stage"] == "shipped"
    assert out.get("already_decided") is True


def test_stage_classification():
    assert stats.stage_of({"status": "under_review"}) == "under_review"
    assert stats.stage_of({"status": "pending_fraud_review"}) == "fraud_review"
    assert stats.stage_of({"status": "pending_second_pass"}) == "second_pass"
    assert stats.stage_of({"status": "shipped"}) == "shipped"
    assert stats.stage_of({"status": "shipped_missing_airtable"}) == "shipped"
    assert stats.stage_of({"status": "rejected"}) == "rejected"
    assert stats.stage_of({"status": "needs_changes"}) == "needs_changes"
    assert stats.stage_of({"status": "???unknown??"}) == "unknown"


def test_pipeline_counts():
    ships = [
        ship(1, 0, status="under_review"),
        ship(2, 0, status="under_review"),
        ship(3, 0, status="pending_second_pass"),
        ship(4, 0, status="pending_fraud_review"),
        ship(5, 0, 5, status="shipped"),
    ]
    p = stats.pipeline_counts(ships)
    assert p["under_review"] == 2
    assert p["second_pass"] == 1
    assert p["fraud_review"] == 1
    assert p["shipped"] == 1


def test_outcomes():
    decided = [
        ship(1, 0, 5, status="shipped", level="4", hours=300),
        ship(2, 0, 6, status="shipped", level="4", hours=250),
        ship(3, 0, 7, status="shipped", level="4", hours=200),
        ship(4, 0, 8, status="shipped", level="4", hours=150),
        ship(5, 0, 9, status="shipped", level="4", hours=100),
        ship(6, 0, 9, status="shipped", level="4", hours=50),
        ship(7, 0, 9, status="shipped", level="4", hours=20),
        ship(8, 0, 9, status="shipped", level="4", hours=10),
        ship(9, 0, 9, status="shipped", level="4", hours=5),
        ship(10, 0, 9, status="shipped", level="4", hours=2),
        ship(11, 0, 3, status="rejected"),
        ship(12, 0, 4, status="needs_changes"),
        ship(13, 0, 4, status="rejected", fraud=True),
    ]
    o = stats.outcome_stats(decided)
    approx(o["approval"], 10 / 13, 0.001)
    approx(o["rejected"], 2 / 13, 0.001)
    approx(o["needs_changes"], 1 / 13, 0.001)
    approx(o["fraud_rejection"], 1 / 13, 0.001)
    # by_level: level 4 all decided -> 10/10 approved
    approx(o["by_level"]["4"]["approval"], 1.0, 0.001)
    # hours bucket 100+ (100,150,200,250,300 = 5 ships, all shipped)
    approx(o["by_hours"]["100h+"]["approval"], 1.0, 0.001)
    assert o["by_hours"]["100h+"]["n"] == 5


def test_gold_estimate():
    # 100h level 4 (60/hr) x multiplier 2.0 = 12000
    approx(stats.gold_estimate(100, "4", 2.0), 12000, 0.001)
    # 50h level 1 (40/hr) no multiplier = 2000
    approx(stats.gold_estimate(50, "1", 1.0), 2000, 0.001)
    # unknown level falls back to level-1 rate, but never None
    assert stats.gold_estimate(10, "9", 1.0) is not None


def test_depth_history_reconstruction():
    # A: submitted day0, decided day10. B: submitted day5, still waiting (now=day20)
    ships = [
        ship(1, 0, 10),
        ship(2, 5, status="under_review"),
    ]
    now = datetime(2026, 1, 21, tzinfo=timezone.utc)  # wall day 20
    series = stats.depth_history(ships, now=now, start="2026-01-01", end="2026-01-21")
    pts = {p["d"]: p["n"] for p in pts_of(series)}
    assert pts["2026-01-01"] == 1          # A waiting, B not yet submitted
    assert pts["2026-01-06"] == 2          # both waiting
    assert pts["2026-01-11"] == 1          # A decided on day10 -> depth drops
    assert pts["2026-01-21"] == 1          # B still waiting
    assert len(pts_of(series)) == 21


def pts_of(series):
    return series


def test_decisions_daily_series():
    decided = [
        ship(1, 0, 10),
        ship(2, 1, 10),
        ship(3, 2, 12),
    ]
    daily = stats.decisions_daily(decided)
    by_day = {p["d"]: p["n"] for p in daily}
    assert by_day["2026-01-11"] == 2
    assert by_day["2026-01-13"] == 1
    assert sum(by_day.values()) == 3


def test_arrivals_weekly():
    waiting = [ship(1, 0, status="under_review"), ship(2, 1, status="under_review")]
    weekly = stats.arrivals_weekly(waiting + [ship(3, 9), ship(4, 10, 12)])
    by_w = {p["w"]: p["n"] for p in weekly}
    assert by_w["2026-01-05"] == 2  # ships 3,4 in week Jan 5 (days 4-10 ISO week 2)
    assert sum(by_w.values()) == 4


def test_survival_curve():
    # 4 ships: decided at 5,10,15,20 days; 1 still waiting at day 25
    decided = [ship(1, 0, 5), ship(2, 0, 10), ship(3, 0, 15), ship(4, 0, 20)]
    waiting = [ship(5, 25, status="under_review")]
    now = datetime(2026, 1, 26, tzinfo=timezone.utc)
    sv = stats.survival_curve(decided + waiting, now=now)
    by_d = {p["d"]: p["decided_pct"] for p in sv}
    # at 20d all 4 decided ships are decided; waiting one censors at 25d
    approx(by_d[20], 4 / 5, 0.001)
    assert sv[-1]["d"] >= 20


def test_cohort_by_submit_week():
    decided = [
        ship(1, 0, 14),   # submitted Thu Jan 1 -> week starting Mon Dec 29
        ship(2, 1, 20),
        ship(3, 2, 5),
    ]
    waiting = [ship(4, 1, status="under_review")]
    now = datetime(2026, 1, 30, tzinfo=timezone.utc)
    cohorts = stats.cohort_table(decided + waiting, now=now)
    # all four fall in the week starting 2025-12-29 (Monday before Jan 1)
    w = [c for c in cohorts if c["week"] == "2025-12-29"][0]
    assert w["n"] == 4
    approx(w["dec14d"], 2 / 4, 0.001)   # ships 1 and 3 within 14d; ship2 19-20d, ship4 waiting


def test_empty_inputs():
    # no crash on empty corpora; graceful Nones / empties
    n = datetime(2026, 1, 5, tzinfo=timezone.utc)
    assert stats.latency_stats([])["n_decided"] == 0
    assert stats.queue_stats([], now=n)["count"] == 0
    assert stats.outcome_stats([])["n_decided"] == 0
    assert stats.depth_history([], now=n, start="2026-01-01", end="2026-01-05") == []
    assert stats.eta_range({}, [], [], now=n) is None
    assert stats.survival_curve([], now=n) == []
    assert stats.pipeline_counts([])["under_review"] == 0


# ------------------------------------------------- type-bundle / enrichment


def test_type_bundles_carry_exact_pipeline_counts():
    """The dashboard toggle reads these, so per-type stage counts must be the
    type's own -- not the global totals."""
    ships = [
        ship(1, 0, status="under_review", pid=1, type_="software"),
        ship(2, 0, 3, status="shipped", pid=2, type_="software"),
        ship(3, 0, 4, status="rejected", pid=3, type_="software"),
        ship(4, 0, status="under_review", pid=4, type_="hardware"),
        ship(5, 0, 2, status="needs_changes", pid=5, type_="hardware"),
    ]
    snap = stats.build_snapshot(ships, now=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert set(snap["by_type"]) == {"all", "software", "hardware"}

    sw = snap["by_type"]["software"]
    assert sw["queue_count"] == 1, sw["queue_count"]
    assert sw["decided_count"] == 2, sw["decided_count"]
    assert sw["pipeline"]["under_review"] == 1
    assert sw["pipeline"]["shipped"] == 1
    assert sw["pipeline"]["rejected"] == 1

    hw = snap["by_type"]["hardware"]
    assert hw["queue_count"] == 1 and hw["pipeline"]["needs_changes"] == 1
    assert hw["pipeline"]["shipped"] == 0

    # the 'all' bundle must agree with the top-level snapshot numbers
    assert snap["by_type"]["all"]["pipeline"] == snap["pipeline"]
    assert snap["by_type"]["all"]["queue_count"] == snap["queue"]["count"]
    assert snap["by_type"]["all"]["type_coverage"] == {"typed": 5, "total": 5}


class _FakeShipsAPI:
    """Minimal stand-in for scrape.Fetcher covering what enrichment uses."""
    workers = 1
    quiet = True

    def __init__(self):
        self.calls = []

    def get_json(self, path):
        self.calls.append(path)
        pid = int(path.rsplit("/", 1)[1])
        return 200, {"name": f"P{pid}", "level": 3, "type": "hardware",
                     "owner": {"username": "u"}, "public_total_hours": 12.0,
                     "reward_estimate_multiplier": 1.5}


def test_enrich_applies_cache_without_fetching():
    """A fully cached corpus must still be healed -- applying the cache and
    fetching it are separate steps, and the fill used to sit behind an early
    return that only ran when a fetch was needed."""
    import scrape

    cache = {"7": {"name": "P7", "level": "3", "type": "hardware",
                   "owner": "u", "hours": 12.0, "mult": 1.5}}
    s = ship(1, 0, 3, status="shipped", pid=7, type_=None)
    s["name"] = None
    fetch = _FakeShipsAPI()
    out = scrape.enrich_with_project_meta(fetch, [s], cache=cache, cache_path=None)

    assert fetch.calls == [], fetch.calls
    assert out[0]["type"] == "hardware"
    assert out[0]["name"] == "P7"
    # a decided ship keeps its review-time hours, so the cached live values
    # are not written over them
    approx(out[0]["hours"], 10.0, 0.001)


def test_enrich_gives_decided_ships_their_type_once():
    """Regression: decided ships used to stay untyped because only *waiting*
    ships were ever enriched, which skewed every per-type comparison."""
    import scrape

    s = ship(1, 0, 3, status="shipped", pid=7, type_=None)
    s["name"] = None
    fetch, cache = _FakeShipsAPI(), {}
    out = scrape.enrich_with_project_meta(fetch, [s], cache=cache, cache_path=None)

    assert fetch.calls == ["/projects/7"], fetch.calls
    assert out[0]["type"] == "hardware"
    assert out[0]["name"] == "P7"
    # hours/multiplier are "live" fields: a decided ship keeps the hours it was
    # reviewed at rather than being overwritten with today's project total
    approx(out[0]["hours"], 10.0, 0.001)

    # a decided ship's metadata is stable, so the second pass makes no request
    fetch2 = _FakeShipsAPI()
    out2 = scrape.enrich_with_project_meta(fetch2, out, cache=cache, cache_path=None)
    assert fetch2.calls == [], fetch2.calls
    assert out2[0]["type"] == "hardware"


def test_meta_cache_survives_a_save_load_round_trip():
    """Regression: JSON object keys are strings, so an int-keyed in-process
    cache missed every lookup after a reload and re-fetched the whole corpus
    on every run (and blew up sorting mixed int/str keys on save)."""
    import json
    import tempfile
    import scrape

    s = ship(1, 0, 3, status="shipped", pid=7, type_=None)
    fetch, cache = _FakeShipsAPI(), {}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "project_meta.json")
        scrape.enrich_with_project_meta(fetch, [s], cache=cache, cache_path=path)
        assert fetch.calls == ["/projects/7"]

        reloaded = scrape.load_meta_cache(path)
        assert set(reloaded) == {"7"}, reloaded        # keys are strings
        assert json.load(open(path))["7"]["type"] == "hardware"

        # a fresh process must not re-fetch anything it already knows
        fetch2 = _FakeShipsAPI()
        out = scrape.enrich_with_project_meta(fetch2, [s], cache=reloaded,
                                              cache_path=path)
        assert fetch2.calls == [], fetch2.calls
        assert out[0]["type"] == "hardware"


def test_enrich_refreshes_live_fields_for_waiting_ships():
    import scrape

    s = ship(1, 0, status="under_review", pid=9, type_="software", hours=10.0)
    fetch = _FakeShipsAPI()
    out = scrape.enrich_with_project_meta(fetch, [s], cache={}, cache_path=None)
    # waiting ships are re-read every run: hours accrue and the streak
    # multiplier changes while you sit in the queue
    approx(out[0]["hours"], 12.0, 0.001)
    approx(out[0]["mult"], 1.5, 0.001)
    # ...but the project's own type is authoritative, not the stale one
    assert out[0]["type"] == "hardware"


# ------------------------------------------------------- seed-corpus tests


def load_seed():
    if not os.path.exists(SEED):
        return None
    out = []
    with open(SEED) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def test_seed_invariants():
    ships = load_seed()
    if not ships:
        print("  SKIP  seed corpus not present")
        return
    # 1. status sanity: only known statuses
    known = {"under_review", "shipped", "rejected", "needs_changes",
             "pending_second_pass", "pending_fraud_review", "shipped_missing_airtable"}
    bad = {s["status"] for s in ships} - known
    assert not bad, f"unknown statuses in corpus: {bad}"
    # 2. decided ships always have reviewed_at; waiting never do
    for s in ships:
        decided = s["status"] not in ("under_review", "pending_second_pass", "pending_fraud_review")
        if decided:
            assert s.get("reviewed_at"), f"decided ship {s['id']} missing reviewed_at"
        else:
            assert not s.get("reviewed_at"), f"waiting ship {s['id']} has reviewed_at"
    # 3. latency stats computable and sane
    decided = [s for s in ships if s.get("reviewed_at")]
    lat = stats.latency_stats(decided)
    assert 10 <= lat["median_days"] <= 30, lat
    assert 0.05 <= lat["fast_lane_pct"] <= 0.35, lat
    # 4. queue stats sane
    now = datetime.now(timezone.utc)
    waiting = [s for s in ships if not s.get("reviewed_at")]
    q = stats.queue_stats(waiting, now=now)
    assert 80 <= q["count"] <= 120, q
    # 5. approval fraction of decided — NOTE: the seed corpus is a biased
    # sample (over-samples recent-ID projects, which skew needs_changes-heavy;
    # enriched metadata is also missing on most rows). The full 2026-09-11
    # analysis put true approval at ~0.90; this subset reads lower. Full
    # snapshot runs (scrape.py enriches every ship) land back near the truth.
    o = stats.outcome_stats(decided)
    assert 0.7 <= o["approval"] <= 1.0, o


def main():
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for name, fn in tests:
        if run(name, fn):
            passed += 1
    print(f"\n{passed}/{len(tests)} passed")
    sys.exit(0 if passed == len(tests) else 1)


if __name__ == "__main__":
    main()
