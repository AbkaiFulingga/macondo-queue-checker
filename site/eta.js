/**
 * Client-side ETA math for the Macondo queue checker.
 * Mirrors scraper/stats.py's eta logic over the committed snapshot +
 * the live project payload. Pure functions; no DOM.
 *
 * Node test: node eta.test.js   (browser: window.eta)
 */

(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.eta = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const DAY = 86400.0;
  const GOLD_RATES = { 1: 40, 2: 45, 3: 50, 4: 60 };
  const STAGES = ["under_review", "fraud_review", "second_pass", "shipped"];
  const STAGE_LABELS = {
    under_review: "In review queue",
    fraud_review: "Fraud squad check",
    second_pass: "Second pass (final check)",
    shipped: "Shipped",
    rejected: "Rejected",
    needs_changes: "Needs changes",
  };

  function parseTs(ts) {
    if (!ts) return null;
    if (typeof ts === "number") return ts;
    let s = String(ts);
    if (s.includes(".")) s = s.split(".")[0] + (s.endsWith("Z") ? "Z" : "");
    if (s.endsWith("Z")) s = s.slice(0, -1) + "+00:00";
    const d = new Date(s);
    return isNaN(d.getTime()) ? null : d.getTime() / 1000;
  }

  function stageOf(status) {
    if (status === "under_review") return "under_review";
    if (status === "pending_fraud_review") return "fraud_review";
    if (status === "pending_second_pass") return "second_pass";
    if (status === "shipped" || status === "shipped_missing_airtable") return "shipped";
    if (status === "rejected") return "rejected";
    if (status === "needs_changes") return "needs_changes";
    return "unknown";
  }

  function isWaiting(status) {
    return ["under_review", "pending_fraud_review", "pending_second_pass"].includes(status);
  }

  /** Compute the ETA descriptor for a live activeShip against a snapshot.
   *  liveShip: {status, created_at} from /api/projects/{id}.activeShip
   *  snapshot: committed snapshot.json
   */
  function estimate(liveShip, snapshot) {
    const stage = stageOf(liveShip && liveShip.status);
    if (!liveShip || !liveShip.created_at) return null;
    const myC = parseTs(liveShip.created_at);
    const now = Date.now() / 1000;
    const lat = snapshot.latency || {};
    const q = snapshot.queue || {};

    if (stage === "shipped" || stage === "rejected" || stage === "needs_changes") {
      return { stage, decided: true, label: STAGE_LABELS[stage] };
    }
    if (stage === "second_pass") {
      return { stage, decided: false, label: STAGE_LABELS[stage],
               note: "Accepted! Awaiting the final second-pass check. Rewards are released after it; nothing to do." };
    }
    if (stage === "fraud_review") {
      return { stage, decided: false, label: STAGE_LABELS[stage],
               note: "This ship is in fraud review; timing is not predictable from public data." };
    }
    if (stage !== "under_review") return { stage: "unknown", decided: false, label: status };

    // queue position from snapshot's ranked waiting list
    const ships = q.ships || [];
    const rank = (ships.findIndex(s => s.id === liveShip.id) + 1) || null;
    const frontC = ships.length ? parseTs(ships[0].created_at) : null;
    const behind = frontC ? Math.max(0, (myC - frontC) / DAY) : null;

    const drain = snapshot.drain || {};
    const dps = drain.decisions_per_day_14d || null;
    const aps = drain.arrivals_per_day_14d || null;
    const net = dps && aps ? dps - aps : null;

    let central = null, method = null;
    if (behind != null && net && net > 0.05) {
      // front clears (dps - aps) queue-days per wall-day; days_behind_front
      // is measured in submission-days — at steady state they match.
      central = behind / net;
      method = "front velocity (14d)";
    } else if (behind != null && dps && dps > 0.2) {
      central = behind / dps;
      method = "gross decision rate (14d)";
    } else if (lat.backlog_median_days) {
      central = lat.backlog_median_days;
      method = "backlog-lane median";
    }

    const out = {
      stage, decided: false, label: STAGE_LABELS[stage],
      rank, queue_count: ships.length,
      days_waiting: Math.floor((now - myC) / DAY),
      fast_lane_pct: lat.fast_lane_pct,
      backlog_median_days: lat.backlog_median_days,
      method,
    };
    if (central != null) {
      central = Math.max(0.5, central);
      const spread = Math.max(3, central * 0.35);
      out.central_days = Math.round(central * 10) / 10;
      out.range_low_d = Math.round(Math.max(0.5, central - spread));
      out.range_high_d = Math.round(central + spread);
      const etaMs = now + central * DAY * 1000;
      const fmt = new Date(etaMs).toISOString().slice(0, 10);
      out.eta_date = fmt;
    }
    return out;
  }

  function goldEstimate(hours, level, multiplier) {
    if (hours == null) return null;
    const rate = GOLD_RATES[String(level)] || GOLD_RATES[1];
    return Math.round(hours * rate * (multiplier || 1.0));
  }

  /** Cohort row for the ship's submission week from the snapshot table. */
  function myCohort(liveShip, snapshot) {
    const c = parseTs(liveShip.created_at);
    if (!c) return null;
    const d = new Date(c * 1000);
    const monday = new Date(d);
    monday.setUTCDate(monday.getUTCDate() - monday.getUTCDay());
    const key = monday.toISOString().slice(0, 10);
    const rows = snapshot.latency_by_week || [];
    // exact week, else the nearest earlier week with data
    let best = null;
    for (const r of rows) if (r.week <= key && r.n) best = r;
    return best;
  }

  /** Similar decided ships from the snapshot's ranked list (level+hours close). */
  function similarShips(liveShip, snapshot, limit) {
    limit = limit || 5;
    const myH = liveShip.hours || 0;
    const myL = String(liveShip.level || "");
    const pool = (snapshot.recent_decisions || []).slice();
    pool.sort((a, b) => {
      const da = Math.abs((a.hours || 0) - myH) + (String(a.level) === myL ? 0 : 0.5 * myH);
      const db = Math.abs((b.hours || 0) - myH) + (String(b.level) === myL ? 0 : 0.5 * myH);
      return da - db;
    });
    return pool.slice(0, limit);
  }

  return {
    parseTs, stageOf, isWaiting, estimate, goldEstimate, myCohort, similarShips,
    STAGES, STAGE_LABELS, GOLD_RATES,
  };
});
