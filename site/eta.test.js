/** Node tests for site/eta.js — run: node eta.test.js */
const fs = require("fs");
const path = require("path");
const eta = require("./eta.js");

const snap = JSON.parse(fs.readFileSync(path.join(__dirname, "data", "snapshot.json")));

let pass = 0, fail = 0;
function t(name, fn) {
  try { fn(); console.log("  PASS", name); pass++; }
  catch (e) { console.log("  FAIL", name, e.message); fail++; }
}
function approx(a, b, tol) { tol = tol == null ? 0.02 : tol; if (Math.abs(a - b) > tol) throw `${a} !~ ${b}`; }
function assert(c, m) { if (!c) throw m || "assertion failed"; }

// fixture: a live under_review ship mirroring 7520
const live7520 = { id: 7520, status: "under_review", created_at: "2026-08-31T23:04:29.396Z" };

t("stage classification", () => {
  assert(eta.stageOf("under_review") === "under_review");
  assert(eta.stageOf("pending_second_pass") === "second_pass");
  assert(eta.stageOf("pending_fraud_review") === "fraud_review");
  assert(eta.stageOf("shipped") === "shipped");
  assert(eta.stageOf("shipped_missing_airtable") === "shipped");
});

t("estimate returns rank + range for waiting ship", () => {
  const est = eta.estimate(live7520, snap);
  assert(est, "estimate null");
  assert(est.stage === "under_review");
  assert(typeof est.rank === "number" && est.rank >= 1, "bad rank: " + est.rank);
  assert(est.queue_count > 50, "queue_count too small");
  assert(est.days_waiting >= 9, "days_waiting: " + est.days_waiting);
  if (est.central_days != null) {
    assert(est.range_low_d <= est.central_days, "range low above central");
    assert(est.range_high_d >= est.central_days, "range high below central");
  }
});

t("decided ship short-circuits", () => {
  const est = eta.estimate({ id: 1, status: "shipped", created_at: "2026-08-01T00:00:00Z" }, snap);
  assert(est.decided === true);
  assert(est.stage === "shipped");
});

t("second-pass ship: no ETA, correct note", () => {
  const est = eta.estimate({ id: 2, status: "pending_second_pass", created_at: "2026-08-20T00:00:00Z" }, snap);
  assert(est.stage === "second_pass");
  assert(est.note && est.note.includes("second-pass"));
  assert(!est.central_days, "second pass should not carry central_days");
});

t("fraud ship: no ETA, note present", () => {
  const est = eta.estimate({ id: 3, status: "pending_fraud_review", created_at: "2026-08-20T00:00:00Z" }, snap);
  assert(est.stage === "fraud_review");
  assert(!est.central_days);
});

t("gold estimate", () => {
  approx(eta.goldEstimate(100, "4", 2.0), 12000, 0.001);
  approx(eta.goldEstimate(50, "1", 1.0), 2000, 0.001);
  assert(eta.goldEstimate(10, "9", 1.0) > 0, "unknown level must not be null");
});

t("cohort lookup finds a week row", () => {
  const row = eta.myCohort(live7520, snap);
  assert(row && row.week && row.n, "no cohort row for Aug 31 week: " + JSON.stringify(row));
});

t("similar ships returns list", () => {
  const sims = eta.similarShips({ hours: 397, level: "4" }, snap, 5);
  assert(Array.isArray(sims) && sims.length > 0, "no similar ships");
});

t("graceful on empty snapshot", () => {
  const est = eta.estimate(live7520, {});
  assert(est === null || est.stage === "under_review" || est.stage === "unknown");
});

console.log(`\n${pass}/${pass + fail} passed`);
process.exit(fail ? 1 : 0);
