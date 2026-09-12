"""Statistics engine for the Macondo queue checker.

Pure functions over ship records (dicts with the public /api/projects/{id}/ships
shape, optionally enriched with project metadata). No network access — the
scraper feeds it data, the tests feed it fixtures.

Ship record fields used:
  id, pid, status, created_at, reviewed_at, hours, level, type,
  is_fraud_rejection, name, owner (last three optional, from enrichment)
"""

import math
from datetime import datetime, timedelta, timezone

DAY = 86400.0
GOLD_RATES = {"1": 40, "2": 45, "3": 50, "4": 60}
WAITING_STATUSES = {"under_review", "pending_second_pass", "pending_fraud_review"}
DECIDED_STATUSES = {"shipped", "shipped_missing_airtable", "rejected", "needs_changes"}


# ---------------------------------------------------------------- parsing


def parse_ts(ts):
    """Parse a Macondo timestamp (ISO with optional fractional seconds) to UTC epoch seconds."""
    if not ts:
        return None
    if isinstance(ts, (int, float)):
        return ts
    s = str(ts)
    # strip fractional seconds
    if "." in s:
        head, _, tail = s.partition(".")
        s = head + ("Z" if tail.endswith("Z") else "")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # normalize space separator to T
    if " " in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # naive means UTC here, never local
    return dt.timestamp()


def stage_of(ship):
    """Map a ship status to the pipeline stage."""
    s = ship.get("status")
    if s in ("under_review",):
        return "under_review"
    if s == "pending_fraud_review":
        return "fraud_review"
    if s == "pending_second_pass":
        return "second_pass"
    if s in ("shipped", "shipped_missing_airtable"):
        return "shipped"
    if s == "rejected":
        return "rejected"
    if s == "needs_changes":
        return "needs_changes"
    return "unknown"


def is_waiting(ship):
    return ship.get("status") in WAITING_STATUSES


def is_decided(ship):
    return ship.get("status") in DECIDED_STATUSES


# ---------------------------------------------------------------- generic


def percentile(sorted_vals, p):
    """Percentile of an already-sorted list; empty -> None."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = p * (len(sorted_vals) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_vals[int(idx)]
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _median(vals):
    if not vals:
        return None
    s = sorted(vals)
    return percentile(s, 0.5)


# -------------------------------------------------------------- latency


def latency_days(ship):
    c = parse_ts(ship.get("created_at"))
    r = parse_ts(ship.get("reviewed_at"))
    if c is None or r is None or r < c:
        return None
    return (r - c) / DAY


def latency_stats(decided_ships):
    """Overall + two-lane latency statistics.

    fast lane: decided within 24h of submission.
    backlog lane: everything else.
    """
    lats = [latency_days(s) for s in decided_ships if is_decided(s)]
    lats = [l for l in lats if l is not None]
    fast = [l for l in lats if l < 1.0]
    slow = [l for l in lats if l >= 1.0]
    n = len(lats)
    return {
        "n_decided": n,
        "median_days": _median(lats),
        "p90_days": percentile(sorted(lats), 0.9) if lats else None,
        "fast_lane_pct": (len(fast) / n) if n else None,
        "fast_lane_median_h": (_median(fast) * 24) if fast else None,
        "backlog_median_days": _median(slow),
        "backlog_p90_days": percentile(sorted(slow), 0.9) if slow else None,
    }


# --------------------------------------------------------------- queue


def age_days(ship, now):
    c = parse_ts(ship.get("created_at"))
    if c is None:
        return None
    return (now - c) / DAY


def queue_stats(waiting_ships, now=None):
    """Depth, oldest wait, front date (= earliest waiting submission)."""
    now = now.timestamp() if isinstance(now, datetime) else (now or datetime.now(timezone.utc).timestamp())
    ages = [age_days(s, now) for s in waiting_ships if is_waiting(s)]
    ages = [a for a in ages if a is not None and a >= 0]
    count = len(ages)
    oldest = max(ages) if ages else None
    front = None
    if count:
        earliest = min(parse_ts(s["created_at"]) for s in waiting_ships
                       if is_waiting(s) and s.get("created_at"))
        front = datetime.fromtimestamp(earliest, tz=timezone.utc).strftime("%Y-%m-%d")
    return {"count": count, "oldest_days": oldest, "front_date": front}


def rank_by_age(waiting_ships, ship_id, now=None):
    """1 = oldest waiting ship. None if ship_id not in the waiting set."""
    now = now.timestamp() if isinstance(now, datetime) else (now or datetime.now(timezone.utc).timestamp())
    entries = []
    for s in waiting_ships:
        if not is_waiting(s):
            continue
        c = parse_ts(s.get("created_at"))
        if c is None:
            continue
        entries.append((c, s.get("id")))
    if not entries:
        return None
    entries.sort()  # oldest first
    for i, (_, sid) in enumerate(entries):
        if sid == ship_id:
            return i + 1
    return None


# --------------------------------------------------------- front velocity


def front_velocity(decided_ships, window_days=30, now=None):
    """How fast the review front advances, in submission-days per wall-day.

    Compares the submission date of decisions near the start vs. end of the
    trailing window: (front_now - front_then) / (wall days elapsed).
    Requires >= 2 decisions spanning >= 0.5 wall days inside the window.
    """
    now = now.timestamp() if isinstance(now, datetime) else (now or datetime.now(timezone.utc).timestamp())
    recent = []
    for s in decided_ships:
        r = parse_ts(s.get("reviewed_at"))
        c = parse_ts(s.get("created_at"))
        if r and c is not None and r <= now and (now - r) / DAY <= window_days:
            recent.append((r, c))
    if len(recent) < 2:
        return {"subm_days_per_wall_day": None, "n_recent_decisions": len(recent)}
    recent.sort()
    first_r, first_c = recent[0]
    last_r, last_c = recent[-1]
    wall_days = (last_r - first_r) / DAY
    if wall_days < 0.5:
        return {"subm_days_per_wall_day": None, "n_recent_decisions": len(recent)}
    subm_span_days = (last_c - first_c) / DAY
    return {
        "subm_days_per_wall_day": max(0.0, subm_span_days / wall_days),
        "n_recent_decisions": len(recent),
    }


# ------------------------------------------------------------------- eta


def eta_range(mine, waiting_ships, decided_ships, now=None):
    """ETA for a waiting ship, as a day-range + central estimate.

    Method: front velocity (submission-days cleared per wall-day) applied to
    the distance between the current front and my submission date. Blended
    with the backlog-lane median as a sanity cap.
    """
    now = now.timestamp() if isinstance(now, datetime) else (now or datetime.now(timezone.utc).timestamp())
    my_c = parse_ts(mine.get("created_at"))
    if my_c is None:
        return None
    stage = stage_of(mine)
    if stage in ("shipped", "rejected", "needs_changes"):
        return {"stage": stage, "central_days": 0, "already_decided": True}
    if stage in ("second_pass",):
        return {"stage": stage, "central_days": 2, "note": "awaiting final check"}
    if stage == "fraud_review":
        return {"stage": stage, "central_days": None, "note": "fraud review; no historical basis"}

    # distance from front to my submission date
    waiting = [s for s in waiting_ships if is_waiting(s) and parse_ts(s.get("created_at"))]
    if not waiting:
        return None
    front_c = min(parse_ts(s["created_at"]) for s in waiting)
    subm_days_behind_front = max(0.0, (my_c - front_c) / DAY)
    rank = rank_by_age(waiting_ships, mine.get("id"), now=now)

    v = front_velocity(decided_ships, window_days=30, now=now)
    velocity = v["subm_days_per_wall_day"]

    lat = latency_stats(decided_ships)
    backlog_median = lat["backlog_median_days"] if lat["backlog_median_days"] else None

    if velocity and velocity > 0:
        central = subm_days_behind_front / velocity
    elif backlog_median:
        central = backlog_median
    else:
        return {"stage": stage, "central_days": None, "reason": "insufficient data"}

    # sanity caps: never less than 0.5d, never more than 2x p90
    central = max(0.5, central)
    if lat["p90_days"]:
        central = min(central, lat["p90_days"] * 2)

    spread = max(3.0, central * 0.35)
    return {
        "stage": stage,
        "central_days": round(central, 1),
        "range_low_d": round(max(0.5, central - spread), 1),
        "range_high_d": round(central + spread, 1),
        "rank_by_age": rank,
        "queue_count": len(waiting),
        "front_velocity": velocity,
        "fast_lane_pct": lat["fast_lane_pct"],
        "backlog_median_days": backlog_median,
        "basis": {
            "days_behind_front": round(subm_days_behind_front, 1),
            "recent_decisions": v["n_recent_decisions"],
        },
    }


def eta_for_ship(mine, waiting_ships, decided_ships, now=None):
    """Wrapper used by tests/UI: full descriptor including stage."""
    now = now or datetime.now(timezone.utc)
    base = eta_range(mine, waiting_ships, decided_ships, now=now)
    if base is None:
        return {"stage": stage_of(mine), "eta_days": None}
    base["eta_days"] = base.get("central_days")
    return base


# --------------------------------------------------------------- pipeline


def pipeline_counts(all_ships):
    counts = {k: 0 for k in ("under_review", "fraud_review", "second_pass",
                             "shipped", "rejected", "needs_changes", "unknown")}
    for s in all_ships:
        counts[stage_of(s)] += 1
    return counts


# --------------------------------------------------------------- outcomes


def _hours_bucket(h):
    if h is None:
        return "unknown"
    if h < 10:
        return "<10h"
    if h < 50:
        return "10-50h"
    if h < 100:
        return "50-100h"
    return "100h+"


def outcome_stats(decided_ships):
    """Approval / rejection / needs-changes / fraud rates, overall and crosstabbed."""
    ships = [s for s in decided_ships if is_decided(s)]
    n = len(ships)
    if not n:
        return {"n_decided": 0}

    def rate(pred):
        return sum(1 for s in ships if pred(s)) / n

    out = {
        "n_decided": n,
        "approval": rate(lambda s: s.get("status") in ("shipped", "shipped_missing_airtable")),
        "rejected": rate(lambda s: s.get("status") == "rejected"),
        "needs_changes": rate(lambda s: s.get("status") == "needs_changes"),
        "fraud_rejection": rate(lambda s: bool(s.get("is_fraud_rejection"))),
    }
    # by level
    by_level = {}
    for lvl in ("1", "2", "3", "4"):
        cell = [s for s in ships if str(s.get("level")) == lvl]
        if len(cell) >= 5:
            a = sum(1 for s in cell if s.get("status") in ("shipped", "shipped_missing_airtable"))
            by_level[lvl] = {"n": len(cell), "approval": a / len(cell)}
    out["by_level"] = by_level
    # by hours bucket
    by_hours = {}
    for bucket in ("<10h", "10-50h", "50-100h", "100h+"):
        cell = [s for s in ships if _hours_bucket(s.get("hours")) == bucket]
        if len(cell) >= 5:
            a = sum(1 for s in cell if s.get("status") in ("shipped", "shipped_missing_airtable"))
            by_hours[bucket] = {"n": len(cell), "approval": a / len(cell)}
    out["by_hours"] = by_hours
    # by type
    by_type = {}
    for t in ("software", "hardware"):
        cell = [s for s in ships if s.get("type") == t]
        if len(cell) >= 5:
            a = sum(1 for s in cell if s.get("status") in ("shipped", "shipped_missing_airtable"))
            by_type[t] = {"n": len(cell), "approval": a / len(cell)}
    out["by_type"] = by_type
    return out


# ------------------------------------------------------------------ gold


def gold_estimate(hours, level, multiplier=1.0):
    if hours is None:
        return None
    rate = GOLD_RATES.get(str(level), GOLD_RATES["1"])
    return round(hours * rate * multiplier)


# --------------------------------------------------------------- series


def depth_history(all_ships, now=None, start=None, end=None):
    """Reconstruct queue depth for each day in [start, end].

    depth(day) = ships whose created_at <= day < reviewed_at (or still waiting).
    Daily resolution; includes every day even when depth is flat.
    """
    now = now.timestamp() if isinstance(now, datetime) else (now or datetime.now(timezone.utc).timestamp())
    spans = []
    for s in all_ships:
        c = parse_ts(s.get("created_at"))
        if c is None:
            continue
        r = parse_ts(s.get("reviewed_at"))
        if r is None or not is_decided(s):
            r = now + DAY  # still open — counts through 'end'
        spans.append((c, r))
    if not spans:
        return []
    lo = parse_ts(start) if start else min(c for c, _ in spans)
    hi = parse_ts(end) if end else now
    if hi <= lo:
        return []
    out = []
    day = lo
    while day <= hi:
        n = sum(1 for c, r in spans if c <= day < r)
        out.append({"d": datetime.fromtimestamp(day, tz=timezone.utc).strftime("%Y-%m-%d"), "n": n})
        day += DAY
    return out


def decisions_daily(decided_ships, days=None, now=None):
    """Count of decisions per wall-day (from reviewed_at)."""
    now = now.timestamp() if isinstance(now, datetime) else (now or datetime.now(timezone.utc).timestamp())
    counts = {}
    for s in decided_ships:
        r = parse_ts(s.get("reviewed_at"))
        if r is None:
            continue
        d = datetime.fromtimestamp(r, tz=timezone.utc).strftime("%Y-%m-%d")
        counts[d] = counts.get(d, 0) + 1
    items = sorted(counts.items())
    if days:
        cutoff = datetime.fromtimestamp(now - days * DAY, tz=timezone.utc).strftime("%Y-%m-%d")
        items = [(d, n) for d, n in items if d >= cutoff]
    return [{"d": d, "n": n} for d, n in items]


def arrivals_weekly(all_ships, now=None):
    """Submissions per ISO week."""
    now = now.timestamp() if isinstance(now, datetime) else (now or datetime.now(timezone.utc).timestamp())
    counts = {}
    for s in all_ships:
        c = parse_ts(s.get("created_at"))
        if c is None or c > now:
            continue
        d = datetime.fromtimestamp(c, tz=timezone.utc)
        iso = d.isocalendar()
        week_start = d - timedelta(days=d.weekday())
        key = week_start.strftime("%Y-%m-%d")
        counts[key] = counts.get(key, 0) + 1
    return [{"w": w, "n": n} for w, n in sorted(counts.items())]


def front_progression(decided_ships, now=None):
    """For each decision day, the latest submission date decided that day.

    Reading the series over time shows the front sweeping forward.
    """
    now = now.timestamp() if isinstance(now, datetime) else (now or datetime.now(timezone.utc).timestamp())
    per_day = {}
    for s in decided_ships:
        r = parse_ts(s.get("reviewed_at"))
        c = parse_ts(s.get("created_at"))
        if r is None or c is None or r > now:
            continue
        d = datetime.fromtimestamp(r, tz=timezone.utc).strftime("%Y-%m-%d")
        cur = per_day.get(d)
        if cur is None or c > cur:
            per_day[d] = c
    return [{"d": d, "front": datetime.fromtimestamp(c, tz=timezone.utc).strftime("%Y-%m-%d")}
            for d, c in sorted(per_day.items())]


def latency_histogram(decided_ships, bins=None):
    """Histogram of days-to-decision with bimodal-friendly bins."""
    bins = bins or [("0-1d", 0, 1), ("1-3d", 1, 3), ("3-7d", 3, 7), ("7-14d", 7, 14),
                    ("14-30d", 14, 30), ("30-60d", 30, 60), ("60d+", 60, 1e9)]
    lats = [latency_days(s) for s in decided_ships if is_decided(s)]
    lats = [l for l in lats if l is not None]
    return [{"label": label, "n": sum(1 for l in lats if lo <= l < hi)}
            for label, lo, hi in bins]


def survival_curve(all_ships, now=None, max_days=90, step_days=5):
    """Kaplan-Meier-ish: fraction of ships still undecided vs days-since-submission.

    Waiting ships censor at their current age; decided ships event at their
    latency. decided_pct(d) = fraction of at-risk ships decided by day d.
    """
    now = now.timestamp() if isinstance(now, datetime) else (now or datetime.now(timezone.utc).timestamp())
    events = []  # (day_of_decision, kind) kind: 'decided' or 'censored'
    for s in all_ships:
        c = parse_ts(s.get("created_at"))
        if c is None:
            continue
        if is_decided(s):
            r = parse_ts(s.get("reviewed_at"))
            if r is None:
                continue
            events.append(((r - c) / DAY, True))
        else:
            events.append(((now - c) / DAY, False))
    if not events:
        return []
    total = len(events)
    out = []
    for d in range(0, max_days + 1, step_days):
        decided = sum(1 for t, ev in events if ev and t <= d)
        out.append({"d": d, "decided_pct": round(decided / total, 4),
                    "still_waiting": sum(1 for t, ev in events if not ev and t > d)})
    return out


def cohort_table(all_ships, now=None):
    """Per submission-week: n, % decided within 7/14/30 days, % decided total."""
    now = now.timestamp() if isinstance(now, datetime) else (now or datetime.now(timezone.utc).timestamp())
    weeks = {}
    for s in all_ships:
        c = parse_ts(s.get("created_at"))
        if c is None:
            continue
        d = datetime.fromtimestamp(c, tz=timezone.utc)
        week_start = d - timedelta(days=d.weekday())
        key = week_start.strftime("%Y-%m-%d")
        r = parse_ts(s.get("reviewed_at"))
        lat = ((r - c) / DAY) if (r and is_decided(s)) else None
        weeks.setdefault(key, []).append(lat)
    out = []
    for w in sorted(weeks):
        lats = weeks[w]
        n = len(lats)
        decided = [l for l in lats if l is not None]
        out.append({
            "week": w,
            "n": n,
            "dec7d": round(sum(1 for l in decided if l <= 7) / n, 4) if n else None,
            "dec14d": round(sum(1 for l in decided if l <= 14) / n, 4) if n else None,
            "dec30d": round(sum(1 for l in decided if l <= 30) / n, 4) if n else None,
            "decided_total": round(len(decided) / n, 4) if n else None,
        })
    return out


def drain_projection(waiting_count, decided_ships, all_ships, now=None, horizon_days=120):
    """Project queue clearance from net flow (decisions - arrivals, recent)."""
    now = now.timestamp() if isinstance(now, datetime) else (now or datetime.now(timezone.utc).timestamp())
    dec14 = decisions_daily(decided_ships, days=14, now=now)
    decisions_per_day = (sum(p["n"] for p in dec14) / 14.0) if dec14 else 0.0
    arr14 = [s for s in all_ships
             if (c := parse_ts(s.get("created_at"))) is not None
             and (now - c) / DAY <= 14]
    arrivals_per_day = len(arr14) / 14.0
    net = arrivals_per_day - decisions_per_day  # queue growth per day
    path = []
    depth = waiting_count
    d = 0
    while d <= horizon_days and depth > 0:
        depth = max(0, depth - (decisions_per_day - arrivals_per_day))
        path.append({"d": d, "depth": round(depth, 1)})
        d += 7
    clears = None
    if decisions_per_day > arrivals_per_day and waiting_count > 0:
        clears_in = waiting_count / (decisions_per_day - arrivals_per_day)
        if clears_in <= horizon_days * 2:
            clears = (datetime.fromtimestamp(now + clears_in * DAY, tz=timezone.utc)
                      .strftime("%Y-%m-%d"))
    return {
        "decisions_per_day_14d": round(decisions_per_day, 2),
        "arrivals_per_day_14d": round(arrivals_per_day, 2),
        "daily_net": round(net, 2),
        "clears_by": clears,
        "path": path,
    }


# ------------------------------------------------------------ records


def records(decided_ships):
    """Longest waits and fastest decisions, for the dashboard 'records' cards."""
    entries = []
    for s in decided_ships:
        if not is_decided(s):
            continue
        l = latency_days(s)
        if l is None:
            continue
        entries.append({"pid": s.get("pid"), "id": s.get("id"),
                        "name": s.get("name"), "status": s.get("status"),
                        "waited_d": round(l, 1)})
    if not entries:
        return {}
    entries.sort(key=lambda e: e["waited_d"])
    return {
        "longest_wait": entries[-1],
        "fastest": entries[0],
        "slowest_5": entries[-5:][::-1],
    }


# ---------------------------------------------------------- similar ships


def similar_ships(mine, decided_ships, limit=5):
    """Recent decided ships with closest level+hours, for the comparison panel."""
    my_h = mine.get("hours") or 0
    my_l = str(mine.get("level") or "")

    def score(s):
        h = s.get("hours") or 0
        dh = abs(h - my_h) / max(my_h, 1.0)
        dl = 0.0 if str(s.get("level")) == my_l else 0.5
        return dh + dl

    cands = [s for s in decided_ships if is_decided(s) and latency_days(s) is not None]
    cands.sort(key=lambda s: (score(s), -(parse_ts(s.get("reviewed_at")) or 0)))
    out = []
    for s in cands[:limit]:
        out.append({
            "pid": s.get("pid"), "name": s.get("name"), "level": s.get("level"),
            "hours": s.get("hours"), "status": s.get("status"),
            "waited_d": round(latency_days(s), 1),
        })
    return out


# ------------------------------------------------------------ snapshot


def build_snapshot(all_ships, now=None):
    """Assemble the full snapshot consumed by the site."""
    now_dt = now or datetime.now(timezone.utc)
    now = now_dt.timestamp()
    decided = [s for s in all_ships if is_decided(s)]
    waiting = [s for s in all_ships if is_waiting(s)]
    lat = latency_stats(decided)
    q = queue_stats(waiting, now=now)
    # rank waiting ships by age once
    ranked = sorted(waiting, key=lambda s: parse_ts(s.get("created_at")) or 0)
    queue_ships = []
    for i, s in enumerate(ranked):
        queue_ships.append({
            "id": s.get("id"), "pid": s.get("pid"), "name": s.get("name"),
            "owner": s.get("owner"), "level": s.get("level"),
            "type": s.get("type"), "hours": s.get("hours"),
            "mult": s.get("mult"),
            "created_at": s.get("created_at"), "rank_by_age": i + 1,
        })
    snap = {
        "generated_at": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pipeline": pipeline_counts(all_ships),
        "queue": {
            "count": q["count"], "oldest_days": q["oldest_days"],
            "front_date": q["front_date"],
            "ships": queue_ships,
        },
        "series": {
            "queue_depth": depth_history(all_ships, now=now),
            "decisions_daily": decisions_daily(decided, now=now),
            "arrivals_weekly": arrivals_weekly(all_ships, now=now),
            "front_progression": front_progression(decided, now=now),
            "latency_hist": latency_histogram(decided),
            "survival": survival_curve(all_ships, now=now),
        },
        "latency": lat,
        "latency_by_week": cohort_table(all_ships, now=now),
        "outcomes": outcome_stats(decided),
        "gold": {"rates_by_level": GOLD_RATES},
        "recent_decisions": [
            {"pid": s.get("pid"), "name": s.get("name"), "status": s.get("status"),
             "waited_d": round(latency_days(s), 1) if latency_days(s) is not None else None,
             "decided": (parse_ts(s.get("reviewed_at")) or 0) and
                        datetime.fromtimestamp(parse_ts(s["reviewed_at"]), tz=timezone.utc).strftime("%Y-%m-%d")}
            for s in sorted(decided, key=lambda s: parse_ts(s.get("reviewed_at")) or 0)[-20:][::-1]
        ],
        "records": records(decided),
        "drain": drain_projection(q["count"], decided, all_ships, now=now),
    }
    return snap
