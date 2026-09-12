#!/usr/bin/env node
/**
 * Frontend smoke tests — run with `npm run test:dom` (needs jsdom).
 *
 * Every UI bug so far has been a wiring failure that no amount of Python
 * testing catches: a referenced element missing from the HTML (null innerHTML
 * throws and kills every section after it), a duplicated brace from an edit,
 * Chart.js refusing to reuse a canvas, a template literal that blanked a table.
 * So this renders the real pages against the real committed snapshot and
 * asserts on what a visitor would actually see.
 *
 * It deliberately runs with Chart.js absent (the CDN-failure path), which is
 * how the site is meant to degrade — stat cards and tables must still render.
 *
 * Exits non-zero on any failed assertion so CI blocks the deploy.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM, VirtualConsole } from "jsdom";

const SITE = path.dirname(fileURLToPath(import.meta.url));
const failures = [];
let checks = 0;

function check(what, cond, detail) {
  checks++;
  if (!cond) failures.push(detail ? `${what} — ${detail}` : what);
}

/** Render a page with local files served, API calls failing (snapshot path). */
async function load(page, { api = {}, offline = true } = {}) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(e.detail?.message || e.message));
  const dom = new JSDOM(fs.readFileSync(path.join(SITE, page), "utf8"), {
    url: "https://example.test/",
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole: vc,
  });
  const { window } = dom;
  window.Element.prototype.scrollIntoView = function () {};  // jsdom lacks it
  window.fetch = async (u) => {
    const rel = String(u).replace(/^https?:\/\/[^/]+\//, "").replace(/^\//, "");
    if (api[rel] !== undefined) {
      return { ok: true, status: 200, json: async () => api[rel], text: async () => "" };
    }
    if (rel.startsWith("api/")) {
      if (offline) throw new Error("offline by test design");
      return { ok: false, status: 404, json: async () => ({}), text: async () => "" };
    }
    const p = path.join(SITE, rel);
    if (!fs.existsSync(p)) {
      return { ok: false, status: 404, json: async () => ({}), text: async () => "" };
    }
    const body = fs.readFileSync(p, "utf8");
    return { ok: true, status: 200, json: async () => JSON.parse(body), text: async () => body };
  };
  const scripts = [...window.document.querySelectorAll("script[src]")]
    .map((s) => s.getAttribute("src"))
    .filter((src) => src && !/^https?:/.test(src));  // skip the Chart.js CDN
  for (const src of scripts) {
    const p = path.join(SITE, src);
    if (!fs.existsSync(p)) continue;
    try {
      window.eval(fs.readFileSync(p, "utf8"));
    } catch (e) {
      errors.push(`eval ${src}: ${e.message}`);
    }
  }
  window.document.dispatchEvent(new window.Event("DOMContentLoaded", { bubbles: true }));
  await settle(window, 900);
  return { window, doc: window.document, errors };
}

function settle(window, ms) {
  return new Promise((r) => window.setTimeout(r, ms));
}

const text = (el) => (el?.textContent || "").replace(/\s+/g, " ").trim();

async function dashboard() {
  const { doc, errors } = await load("index.html");
  check("dashboard renders without JS errors", errors.length === 0, errors.join("; "));

  const cards = [...doc.querySelectorAll("#stat-cards .stat")]
    .map((c) => text(c.querySelector(".v")));
  check("stat cards render", cards.length >= 6, `got ${cards.length}`);
  check("queue stat has a number", /^\d+$/.test(cards[0] || ""), `got "${cards[0]}"`);

  check("TL;DR rendered", text(doc.getElementById("dashboard-tldr")).length > 20);
  check("big-picture cards rendered", doc.querySelectorAll("#bigpicture-cards .bp-card").length === 4);
  check("funnel legend rendered", text(doc.getElementById("funnel-legend")).includes("Shipped"));
  check("drain verdict rendered", text(doc.getElementById("drain-body")).length > 40);

  // the population rule must be stated, not implied
  const cut = text(doc.getElementById("cutoff-note"));
  check("cutoff note rendered", cut.length > 40, `got "${cut}"`);
  check("cutoff note names the date and zone", /2026-08-31/.test(cut) && /UTC/.test(cut), cut);

  // every stage segment must sum to the ship total the legend states
  const legend = text(doc.getElementById("funnel-legend"));
  const seg = (label) => {
    const m = legend.match(new RegExp(label + ":\\s*([\\d,]+)"));
    return m ? Number(m[1].replace(/,/g, "")) : null;
  };
  const stages = ["Shipped", "Needs changes", "Rejected", "In queue",
                  "Second pass", "Fraud check"].map(seg).filter((n) => n !== null);
  const stated = Number((legend.match(/\(([\d,]+) total\)/) || [])[1]?.replace(/,/g, ""));
  check("funnel legend states a total", !Number.isNaN(stated), legend);
  check("funnel segments sum to the stated total",
    stages.reduce((a, b) => a + b, 0) === stated,
    `${stages.join("+")}=${stages.reduce((a, b) => a + b, 0)} vs ${stated}`);

  // the type toggle must actually change the numbers it claims to
  const queueNow = () => text(doc.querySelector("#stat-cards .stat .v"));
  const byType = {};
  for (const t of ["all", "software", "hardware"]) {
    const btn = [...doc.querySelectorAll("#type-toggle button")].find((b) => b.dataset.t === t);
    check(`toggle button ${t} exists`, !!btn);
    if (!btn) continue;
    btn.dispatchEvent(new doc.defaultView.MouseEvent("click", { bubbles: true }));
    await settle(doc.defaultView, 300);
    byType[t] = queueNow();
    const legendQueue = (text(doc.getElementById("funnel-legend")).match(/In queue: (\d+)/) || [])[1];
    check(`${t}: funnel legend matches the stat card`, legendQueue === byType[t],
      `legend ${legendQueue} vs card ${byType[t]}`);
  }
  check("software + hardware queue = all",
    Number(byType.software) + Number(byType.hardware) === Number(byType.all),
    `${byType.software} + ${byType.hardware} != ${byType.all}`);

  // snapshot fallback for a project we do have data for
  doc.getElementById("search-input").value = "906";
  doc.getElementById("search-form").dispatchEvent(
    new doc.defaultView.Event("submit", { bubbles: true, cancelable: true }));
  await settle(doc.defaultView, 1200);
  const banner = text(doc.getElementById("result-banner"));
  check("lookup falls back to the snapshot card", banner.length > 30, `got "${banner}"`);
  check("snapshot fallback is labelled", /snapshot/i.test(banner), banner);
  return { doc, errors };
}

async function outOfWindowLookup() {
  const late = { id: 999999, name: "Late Project", level: 2, fruit: "x", type: "software",
    public_total_hours: 5, reward_estimate_multiplier: 1, project_streak_days: 1,
    activeShip: { id: 42, status: "under_review", created_at: "2026-09-06T10:00:00Z" } };
  const { doc, errors } = await load("index.html", {
    api: { "api/projects/999999": late,
           "api/projects/999999/ships": [{ id: 42, status: "under_review",
             created_at: "2026-09-06T10:00:00Z", hackatime_hours: 5, override_hours: 0,
             fruitRewarded: 0, is_fraud_rejection: false }] },
  });
  check("late-project lookup renders without errors", errors.length === 0, errors.join("; "));
  doc.getElementById("search-input").value = "999999";
  doc.getElementById("search-form").dispatchEvent(
    new doc.defaultView.Event("submit", { bubbles: true, cancelable: true }));
  await settle(doc.defaultView, 1200);
  const banner = text(doc.getElementById("result-banner"));
  check("out-of-window lookup says so", /after the .*cutoff/.test(banner), banner);
}

async function queuePage() {
  const { doc, errors } = await load("queue.html");
  check("queue page renders without errors", errors.length === 0, errors.join("; "));
  const rows = doc.querySelectorAll("#queue-table tbody tr");
  check("queue table has rows", rows.length > 100, `got ${rows.length}`);
  check("queue table is not the error placeholder",
    !text(doc.getElementById("queue-table")).includes("Snapshot unavailable"));
}

const only = process.argv.includes("--quick") ? [dashboard] : [dashboard, outOfWindowLookup, queuePage];
for (const fn of only) {
  try {
    await fn();
  } catch (e) {
    failures.push(`${fn.name} threw: ${e.message}`);
  }
}

if (failures.length) {
  console.error(`\n${failures.length} of ${checks} frontend checks FAILED:`);
  for (const f of failures) console.error("  ✗ " + f);
  process.exit(1);
}
console.log(`\n${checks} frontend checks passed`);
