/** Main app: search handling, result card, dashboard. Static site, no build. */
(function () {
  // The CORS proxy (see worker/DEPLOY.md). Leave as-is to use snapshot fallback.
  const PROXY = ""; // e.g. "https://macondo-queue-proxy.yourname.workers.dev"
  let SNAP = null;

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtDays = (d) => d == null ? "—" : (d >= 10 ? Math.round(d) : Math.round(d * 10) / 10) + "d";
  const fmtH = (h) => h == null ? "—" : (Math.round(h * 10) / 10) + "h";

  // ------------------------------------------------------------ snapshot

  async function loadSnapshot() {
    try {
      const r = await fetch("data/snapshot.json");
      SNAP = await r.json();
      renderDashboard();
      $("snapshot-age").textContent = "as of " + (SNAP.generated_at || "?").replace("T", " ").replace("Z", " UTC");
    } catch (e) {
      $("snapshot-age").textContent = "unavailable";
      $("search-msg").textContent = "Could not load queue snapshot — dashboard offline, live lookup may still work.";
    }
  }

  // ------------------------------------------------------------ search

  function parseQuery(q) {
    q = (q || "").trim();
    if (!q) return null;
    let m = q.match(/projects\/(\d+)/) || q.match(/project\/(\d+)/);
    if (m) return { type: "pid", value: m[1] };
    m = q.match(/^#?(\d{1,8})$/);
    if (m) return { type: "pid", value: m[1] };
    m = q.match(/users\/([A-Za-z0-9_.:-]+)/);
    if (m) return { type: "user", value: m[1] };
    if (/^[A-Za-z0-9_.:-]{1,64}$/.test(q) && /\D/.test(q)) return { type: "user", value: q };
    if (/^\d{1,8}$/.test(q)) return { type: "pid", value: q };
    return { type: "user", value: q };
  }

  async function apiGet(path) {
    // live via proxy if configured; else null (snapshot fallback)
    if (PROXY) {
      try {
        const r = await fetch(PROXY + path);
        if (r.ok) return await r.json();
      } catch (e) { /* fall through */ }
    }
    return null;
  }

  async function handleSearch(q) {
    const parsed = parseQuery(q);
    if (!parsed) { $("search-msg").textContent = "Enter a project URL, ID, or username."; return; }
    $("search-msg").textContent = "Looking up…";

    if (parsed.type === "user") {
      await showUser(parsed.value);
      return;
    }
    const pid = parsed.value;
    const proj = await apiGet(`/api/projects/${pid}`);
    if (!proj && SNAP) {
      const meta = (SNAP.queue.ships || []).find(s => String(s.pid) === String(pid));
      if (meta) {
        renderSnapshotOnly(meta, "Live lookup unavailable (no proxy) — showing last night's snapshot.");
        return;
      }
      // decided project not in queue: try snapshot corpus is not available; ask user
      $("search-msg").textContent = "Live lookup needs the CORS proxy (see How it works). Project may also not exist.";
      return;
    }
    if (!proj) {
      $("search-msg").textContent = "Couldn't fetch this project. Is the ID right?";
      return;
    }
    $("search-msg").textContent = "";
    await renderProject(proj, pid);
  }

  async function showUser(username) {
    const user = await apiGet(`/api/users/${encodeURIComponent(username)}`);
    if (!user || !user.projects || !user.projects.length) {
      $("search-msg").textContent = "No public projects found for that username (or proxy not deployed).";
      return;
    }
    $("search-msg").textContent = "";
    const card = $("all-projects-card");
    card.classList.remove("hidden");
    $("all-projects-title").textContent = `Projects of @${esc(user.username)} — click to check one`;
    $("all-projects-body").innerHTML = "<table><thead><tr><th>Project</th><th>Level</th><th>Type</th><th>Status</th></tr></thead><tbody>" +
      user.projects.map(p => {
        const st = p.has_shipped ? '<span class="pill ok">has shipped before</span>'
          : '<span class="pill wait">no ship yet</span>';
        return `<tr><td><a href="#" data-pid="${p.id}" class="proj-link">${esc(p.name)}</a></td>
                <td>L${esc(p.level)}</td><td>${esc(p.type)}</td><td>${st}</td></tr>`;
      }).join("") + "</tbody></table>";
    $("all-projects-body").querySelectorAll(".proj-link").forEach(a =>
      a.addEventListener("click", (ev) => {
        ev.preventDefault();
        $("search-input").value = a.dataset.pid;
        handleSearch(a.dataset.pid);
        window.scrollTo({ top: 0, behavior: "smooth" });
      }));
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // -------------------------------------------------------- project card

  async function renderProject(proj, pid) {
    const live = proj.activeShip;
    const ships = await apiGet(`/api/projects/${pid}/ships`) || [];
    const active = live || null;
    const hasShip = !!(active && active.status);

    $("result").classList.remove("hidden");
    const banner = $("result-banner");
    if (!hasShip) {
      banner.className = "card";
      banner.innerHTML = `<h2>${esc(proj.name)}</h2>
        <p>This project has no ship in review — it hasn't been submitted for review yet.</p>
        <p class="muted">See Macondo's <a href="https://macondo.hackclub.com/docs/what-is-shipping">shipping docs</a> for how to ship it.</p>`;
      hideResultSections();
      return;
    }

    const est = window.eta.estimate(
      { id: active.id, status: active.status, created_at: active.created_at, hours: proj.public_total_hours, level: proj.level },
      SNAP || {});
    renderBanner(proj, active, est, ships);
    renderStepper(active, est);
    renderEta(est);
    renderFile(proj, active, ships);
    renderCohort(active, est);
    renderSimilar(active, proj, ships, est);
    renderGold(proj, est);
    $("result").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function hideResultSections() {
    ["stepper-card", "eta-card", "file-card", "cohort-card", "similar-card", "gold-card"]
      .forEach(id => $(id).classList.add("hidden"));
  }

  function renderBanner(proj, active, est, ships) {
    const b = $("result-banner");
    const liveNote = PROXY ? '<span class="pill info">live</span>' : '<span class="pill wait">snapshot</span>';
    const ship = ships.find(s => s.id === active.id);
    let statusPill, headline;
    const st = active.status;
    if (st === "under_review") { statusPill = '<span class="pill wait">In review queue</span>'; headline = "In the review queue"; b.className = "card"; }
    else if (st === "pending_second_pass") { statusPill = '<span class="pill info">Second pass</span>'; headline = "Accepted — awaiting final check!"; b.className = "card decided-ship"; }
    else if (st === "pending_fraud_review") { statusPill = '<span class="pill bad">Fraud review</span>'; headline = "In fraud-squad review"; b.className = "card"; }
    else if (st === "shipped" || st === "shipped_missing_airtable") { statusPill = '<span class="pill ok">Shipped</span>'; headline = "Shipped 🎉"; b.className = "card decided-ship"; }
    else if (st === "rejected") { statusPill = '<span class="pill bad">Rejected</span>'; headline = "Rejected"; b.className = "card decided-ship"; }
    else if (st === "needs_changes") { statusPill = '<span class="pill wait">Needs changes</span>'; headline = "Needs changes"; b.className = "card decided-ship"; }
    else { statusPill = '<span class="pill wait">' + esc(st) + "</span>"; headline = esc(st); }

    let decisionHtml = "";
    if (ship && ship.decision_user_message) {
      decisionHtml = `<div class="decision-box"><strong>Reviewer's message:</strong><br>${esc(ship.decision_user_message)}</div>`;
    }
    if (ship && ship.fruitRewarded > 0) {
      decisionHtml += `<p class="muted small">Fruit rewarded: ${ship.fruitRewarded}</p>`;
    }
    if (ship && ship.is_fraud_rejection) {
      decisionHtml += `<p class="muted small">This rejection was fraud-related (per public ship record).</p>`;
    }

    b.innerHTML = `<h2>${statusPill} ${esc(proj.name)} ${liveNote}</h2>
      <p class="eta-sub">${headline} · <a href="https://macondo.hackclub.com/projects/${proj.id}">view on Macondo ↗</a></p>
      ${decisionHtml}`;
  }

  const STEP_DEFS = [
    { key: "under_review", label: "In review" },
    { key: "fraud_review", label: "Fraud check" },
    { key: "second_pass", label: "Second pass" },
    { key: "shipped", label: "Shipped" },
  ];

  function renderStepper(active, est) {
    const sc = $("stepper-card");
    sc.classList.remove("hidden");
    const stageKeys = ["under_review", "fraud_review", "second_pass", "shipped"];
    const myStage = window.eta.stageOf(active.status);
    const decidedStates = { shipped: true, rejected: true, needs_changes: true };
    const myIdx = stageKeys.indexOf(myStage === "rejected" || myStage === "needs_changes" ? "second_pass" : myStage);
    const dwell = SNAP ? SNAP.pipeline || {} : {};
    $("stepper").innerHTML = STEP_DEFS.map((s, i) => {
      let cls = "";
      if (i < myIdx || (myStage === "shipped" && i <= 3)) cls = "done";
      else if (i === myIdx) cls = "active";
      else if (decidedStates[myStage] && i === 3) cls = "";
      return `<div class="step ${cls}">${s.label}
        <span class="dwell">${i === 0 && dwell.under_review ? "~" + Math.round((SNAP.latency.median_days || 0)) + "d median" : ""}
        ${i === 2 ? "final check" : ""}${i === 3 ? "rewards released" : ""}</span></div>`;
    }).join("");
    $("stepper-note").textContent =
      myStage === "second_pass"
        ? "Your project has been accepted by a reviewer and is waiting on the final second-pass check. Rewards release after it — nothing for you to do."
        : myStage === "fraud_review"
          ? "This ship was escalated to the fraud squad. Public data can't predict fraud-review timing."
          : "Reviewers claim ships from the queue individually — this shows where you are, not a strict line order.";
  }

  function renderEta(est) {
    const el = $("eta-body");
    if (!est) { el.innerHTML = '<p class="muted">No estimate available.</p>'; return; }
    if (est.decided) {
      el.innerHTML = `<p>This ship already has a decision (${esc(est.label)}). No estimate needed.</p>`;
      return;
    }
    if (est.note) {
      el.innerHTML = `<p class="eta-range">${esc(est.label)}</p><p class="eta-sub">${esc(est.note)}</p>`;
      return;
    }
    const fast = est.fast_lane_pct != null ? Math.round(est.fast_lane_pct * 100) : null;
    el.innerHTML = `
      <p class="eta-range">${est.range_low_d}–${est.range_high_d} days</p>
      <p class="eta-sub">median estimate ~${fmtDays(est.central_days)}${est.eta_date ? " · around " + est.eta_date : ""}</p>
      ${est.rank ? `<p class="muted small">Roughly #${est.rank} of ${est.queue_count} waiting ships by age (claim-based — not a strict line).</p>` : ""}
      ${est.method ? `<p class="muted small">Method: ${esc(est.method)}${est.days_waiting != null ? " · waiting " + est.days_waiting + " days so far" : ""}</p>` : ""}
      <div class="fast-lane">⚡ ${(fast != null ? fast : 16)}% of ships get claimed within 24h — so it could be any day now.</div>`;
  }

  function renderFile(proj, active, ships) {
    const kv = [
      ["Project", `<a href="https://macondo.hackclub.com/projects/${proj.id}">${esc(proj.name)} ↗</a>`],
      ["Type / level", `${esc(proj.type)} · Level ${esc(proj.level)} (${esc(proj.fruit)})`],
      ["Submitted", active.created_at ? new Date(active.created_at).toLocaleDateString() : "—"],
      ["Waiting", est_days(active.created_at) + " days"],
      ["Hours logged", fmtH(proj.public_total_hours) + " (live — updates during review)"],
      ["Streak", proj.project_streak_days + " days"],
      ["AI usage", proj.next_ship_used_ai ? "declared" : "not declared"],
      ["Repo / demo", [proj.repository_url ? `<a href="${esc(proj.repository_url)}">repo</a>` : null,
                       proj.demo_url ? `<a href="${esc(proj.demo_url)}">demo</a>` : null].filter(Boolean).join(" · ") || "—"],
      ["Ships total", ships.length],
    ];
    $("file-body").innerHTML = "<dl class='kv'>" + kv.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("") + "</dl>";
    $("file-card").classList.remove("hidden");
  }

  function est_days(fromIso) {
    if (!fromIso) return "—";
    const d = (Date.now() - new Date(fromIso).getTime()) / 86400000;
    return d >= 10 ? Math.round(d) : Math.round(d * 10) / 10;
  }

  function renderCohort(active, est) {
    const row = window.eta.myCohort({ created_at: active.created_at }, SNAP || {});
    const el = $("cohort-body");
    if (!row) { el.innerHTML = '<p class="muted">No cohort data for this week yet.</p>'; return; }
    const bar = (pct) => {
      const p = Math.round((pct || 0) * 100);
      return `<div class="bar"><div class="bar-fill" style="width:${p}%"></div><span>${p}%</span></div>`;
    };
    el.innerHTML = `<p class="muted small">Week of ${esc(row.week)} — ${row.n} ships submitted:</p>
      <ul class="clean">
        <li>Decided within 7 days: ${bar(row.dec7d)}</li>
        <li>within 14 days: ${bar(row.dec14d)}</li>
        <li>within 30 days: ${bar(row.dec30d)}</li>
      </ul>`;
    $("cohort-card").classList.remove("hidden");
  }

  function renderSimilar(active, proj, ships, est) {
    const sims = window.eta.similarShips(
      { hours: proj.public_total_hours, level: proj.level }, SNAP || {}, 5);
    const el = $("similar-body");
    if (!sims.length) { el.innerHTML = '<p class="muted">No comparable decided ships yet.</p>'; return; }
    el.innerHTML = "<table><thead><tr><th>Project</th><th>Lvl</th><th>Hours</th><th>Outcome</th><th>Waited</th></tr></thead><tbody>" +
      sims.map(s => {
        const cls = s.status === "shipped" ? "ok" : s.status === "rejected" ? "bad" : "wait";
        return `<tr><td><a href="https://macondo.hackclub.com/projects/${s.pid}">${esc(s.name || "#" + s.pid)}</a></td>
          <td>L${esc(s.level)}</td><td>${fmtH(s.hours)}</td>
          <td><span class="pill ${cls}">${esc(s.status)}</span></td><td>${fmtDays(s.waited_d)}</td></tr>`;
      }).join("") + "</tbody></table>";
    $("similar-card").classList.remove("hidden");
  }

  function renderGold(proj, est) {
    if (est && est.decided) { $("gold-card").classList.add("hidden"); return; }
    const h = proj.public_total_hours;
    const rate = window.eta.GOLD_RATES[String(proj.level)] || 40;
    const mult = proj.reward_estimate_multiplier || 1;
    const g = window.eta.goldEstimate(h, proj.level, mult);
    if (g == null) { $("gold-card").classList.add("hidden"); return; }
    $("gold-body").innerHTML = `<p class="eta-range">${g.toLocaleString()} gold</p>
      <p class="eta-sub">if approved: ${fmtH(h)} × ${rate}/hr (level ${esc(proj.level)}) × ${Math.round(mult * 100) / 100}× (streak multiplier)</p>
      <p class="muted small">Estimate from public reward rates; the final amount is set at review. Base rates: L1 40 · L2 45 · L3 50 · L4 60 gold/hour.</p>`;
    $("gold-card").classList.remove("hidden");
  }

  function renderSnapshotOnly(meta, note) {
    $("result").classList.remove("hidden");
    $("result-banner").className = "card";
    $("result-banner").innerHTML = `<h2><span class="pill wait">In review queue</span> ${esc(meta.name || "Project " + meta.pid)}</h2>
      <p class="eta-sub">${esc(note)}</p>
      <p class="muted small">Queue rank by age: #${meta.rank_by_age} of ${(SNAP.queue.ships || []).length} ·
      submitted ${esc((meta.created_at || "").slice(0, 10))} · <a href="https://macondo.hackclub.com/projects/${meta.pid}">view on Macondo ↗</a></p>`;
    hideResultSections();
  }

  // ---------------------------------------------------------- dashboard

  function renderDashboard() {
    const s = SNAP;
    if (!s) return;
    const lat = s.latency || {};
    const q = s.queue || {};
    const drain = s.drain || {};
    const stats = [
      { v: q.count || 0, k: "ships in queue" },
      { v: q.oldest_days != null ? Math.round(q.oldest_days) + "d" : "—", k: "longest wait" },
      { v: q.front_date || "—", k: "front (oldest waiting)" },
      { v: drain.decisions_per_day_14d != null ? drain.decisions_per_day_14d : "—", k: "decisions/day (14d)" },
      { v: lat.median_days != null ? Math.round(lat.median_days) + "d" : "—", k: "median wait" },
      { v: Math.round((s.outcomes.approval || 0) * 100) + "%", k: "approval rate" },
    ];
    $("stat-cards").innerHTML = stats.map(x => `<div class="stat"><div class="v">${x.v}</div><div class="k">${x.k}</div></div>`).join("");

    if (window.renderCharts) window.renderCharts(s);

    // approval rates breakdown
    const o = s.outcomes || {};
    const cell = (label, obj) => obj && obj.n
      ? `<li>${label}: <strong>${Math.round(obj.approval * 100)}%</strong> <span class="muted small">(n=${obj.n})</span></li>` : "";
    $("approval-body").innerHTML = `<ul class="clean">
      <li>Overall: <strong>${Math.round((o.approval || 0) * 100)}%</strong> <span class="muted small">(n=${o.n_decided})</span></li>
      ${cell("Level 1", (o.by_level || {})["1"])}${cell("Level 2", (o.by_level || {})["2"])}
      ${cell("Level 3", (o.by_level || {})["3"])}${cell("Level 4", (o.by_level || {})["4"])}
      ${(o.by_hours ? ["<10h", "10-50h", "50-100h", "100h+"].map(b => cell(b, o.by_hours[b])).join("") : "")}
    </ul>
    <p class="muted small">Needs-changes rate: ${Math.round((o.needs_changes || 0) * 100)}% · Fraud-related rejections: ${Math.round((o.fraud_rejection || 0) * 100)}%</p>`;

    // composition
    const ships = q.ships || [];
    const byLevel = {};
    ships.forEach(x => byLevel[x.level] = (byLevel[x.level] || 0) + 1);
    $("composition-body").innerHTML = `<ul class="clean">` +
      Object.keys(byLevel).sort().map(l => `<li>Level ${esc(l)}: ${byLevel[l]} waiting</li>`).join("") +
      `</ul><p class="muted small">${ships.length} ships · median hours ${fmtH(median(ships.map(x => x.hours || 0)))}</p>`;

    // recent decisions
    $("recent-body").innerHTML = "<table><thead><tr><th>Project</th><th>Status</th><th>Waited</th><th>Decided</th></tr></thead><tbody>" +
      (s.recent_decisions || []).slice(0, 20).map(d => {
        const cls = d.status === "shipped" ? "ok" : d.status === "rejected" ? "bad" : "wait";
        return `<tr><td><a href="https://macondo.hackclub.com/projects/${d.pid}">${esc(d.name || "#" + d.pid)}</a></td>
          <td><span class="pill ${cls}">${esc(d.status)}</span></td><td>${fmtDays(d.waited_d)}</td><td>${esc(d.decided)}</td></tr>`;
      }).join("") + "</tbody></table>";

    // records
    const rec = s.records || {};
    $("records-body").innerHTML = rec.longest_wait
      ? `<p>Longest wait decided: <strong>${fmtDays(rec.longest_wait.waited_d)}</strong> — ${esc(rec.longest_wait.name || "#" + rec.longest_wait.pid)} (${esc(rec.longest_wait.status)})</p>
        <p>Fastest decision: <strong>${fmtDays(rec.fastest.waited_d)}</strong> — ${esc(rec.fastest.name || "#" + rec.fastest.pid)}</p>`
      : "<p class='muted'>—</p>";
  }

  function median(vals) {
    if (!vals.length) return null;
    const s = vals.slice().sort((a, b) => a - b);
    return s[Math.floor(s.length / 2)];
  }

  // -------------------------------------------------------------- init

  document.addEventListener("DOMContentLoaded", () => {
    loadSnapshot();
    $("search-form").addEventListener("submit", (e) => {
      e.preventDefault();
      handleSearch($("search-input").value);
    });
  });
})();
