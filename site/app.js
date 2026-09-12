/** Main app: search handling, result card, dashboard. Static site, no build. */
(function () {
  // The CORS proxy (see worker/DEPLOY.md). Leave as-is to use snapshot fallback.
  const PROXY = "https://macondo-queue-proxy.macondo-queue-906.workers.dev";
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
        await renderSnapshotFull(meta, "Live lookup unavailable (no proxy) — snapshot data from last night.");
        return;
      }
      $("search-msg").textContent = "Live lookup needs the CORS proxy (see How it works). Project may also not exist or already be decided.";
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
    const STEP_SUBS = {
      "under_review": SNAP && SNAP.latency && SNAP.latency.median_days ? "~" + Math.round(SNAP.latency.median_days) + "d median" : "in the queue",
      "fraud_review": "only if flagged",
      "second_pass": "final check after approval",
      "shipped": "rewards released",
    };
    $("stepper").innerHTML = STEP_DEFS.map((s, i) => {
      let cls = "";
      if (i < myIdx || (myStage === "shipped" && i <= 3)) cls = "done";
      else if (i === myIdx) cls = "active";
      return `<div class="step ${cls}">${s.label}
        <span class="dwell">${esc(STEP_SUBS[s.key] || "")}</span></div>`;
    }).join("");
    $("stepper-note").textContent =
      myStage === "second_pass"
        ? "Your project has been accepted by a reviewer and is waiting on the final second-pass check. Rewards release after it — nothing for you to do."
        : myStage === "fraud_review"
          ? "This ship was escalated to the fraud squad — it happens to a small fraction of ships and doesn't imply anything is wrong. Public data can't predict fraud-review timing."
          : "Reviewers claim ships from the queue individually — this shows where you are in the pipeline, not a strict line order. The fraud-check stage is only shown if it applies to you.";
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
    const methodText = {
      "front velocity (14d)": "from how fast reviewers have been clearing the backlog in the last 2 weeks",
      "gross decision rate (14d)": "from the raw decisions-per-day pace (backlog may share the work)",
      "backlog-lane median": "from the historical median wait (recent reviewer pace unclear)",
    }[est.method] || "";
    el.innerHTML = `
      <p class="eta-range">${est.range_low_d}–${est.range_high_d} days</p>
      <p class="eta-sub">median estimate ~${fmtDays(est.central_days)}${est.eta_date ? " · around " + est.eta_date : ""}</p>
      <div class="interp"><span class="lead">What this number means</span>
        This is <strong>not a promise or a date</strong> — it's a statistical window computed ${methodText}.
        ${est.rank ? `About <strong>${est.rank - 1}</strong> ships submitted before yours are still waiting; a pure oldest-first queue would put your turn near ${est.eta_date || fmtDays(est.central_days)}.` : ""}
        Reviewers don't work oldest-first though — they <strong>claim</strong> ships, so the real date can land anywhere in (or outside) this window.
      </div>
      ${est.rank ? `<p class="muted small">You are roughly #${est.rank} of ${est.queue_count} waiting ships by submission age <i class="tip" tabindex="0" data-tip="Sorted by when ships were submitted. It shows who's 'ahead' in a fair-world queue — but reviewers pick ships individually, so this is context, not a line number."></i> · waiting ${est.days_waiting} days so far${est.backlog_median_days ? " · ships that miss the fast lane historically wait a median of " + Math.round(est.backlog_median_days) + "d" : ""}.</p>` : ""}
      <div class="fast-lane">⚡ ${(fast != null ? fast : 16)}% of ships get claimed within 24h (median 3 hours) — so it could be any day now. This is real and it's the best argument for not stressing about the queue math.</div>`;
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
    const mkbar = (label, pct, color) => {
      const p = Math.round((pct || 0) * 100);
      return `<div class="bar"><span class="muted small" style="min-width:118px">${label}</span>
        <div class="track"><div class="fill" style="width:${p}%;${color ? "background:" + color : ""}"></div></div>
        <span class="pct">${p}%</span></div>`;
    };
    el.innerHTML = `<p class="readhow">Ships submitted the <b>same week as yours</b> — ${row.n} of them. What happened to each of them:</p>
      <div class="bars">
        ${mkbar("decided in 7 days", row.dec7d)}
        ${mkbar("in 14 days", row.dec14d)}
        ${mkbar("in 30 days", row.dec30d)}
        ${mkbar("decided at all", row.decided_total, "var(--ok)")}
      </div>
      <p class="muted small">These are the ships most similar to you in timing — they faced the same reviewers and the same backlog. If most of your week is decided but you're still waiting, you're in the slow tail; if few are decided, the whole week is still queued and there's nothing wrong with your ship.</p>`;
    $("cohort-card").classList.remove("hidden");
  }

  function renderSimilar(active, proj, ships, est) {
    const sims = window.eta.similarShips(
      { hours: proj.public_total_hours, level: proj.level }, SNAP || {}, 5);
    const el = $("similar-body");
    if (!sims.length) { el.innerHTML = '<p class="muted">No comparable decided ships yet.</p>'; return; }
    el.innerHTML = "<p class='readhow'>Recently decided ships with <b>similar hours and level</b> to yours — your closest real-world precedents:</p>" +
      "<table><thead><tr><th>Project</th><th>Lvl</th><th>Hours</th><th>Outcome</th><th>Waited</th></tr></thead><tbody>" +
      sims.map(s => {
        const cls = s.status === "shipped" ? "ok" : s.status === "rejected" ? "bad" : "wait";
        return `<tr><td><a href="https://macondo.hackclub.com/projects/${s.pid}">${esc(s.name || "#" + s.pid)}</a></td>
          <td>L${esc(s.level)}</td><td>${fmtH(s.hours)}</td>
          <td><span class="pill ${cls}">${esc(s.status)}</span></td><td>${fmtDays(s.waited_d)}</td></tr>`;
      }).join("") + "</tbody></table>" +
      "<p class='muted small'>Numbers beat abstractions: if similar ships waited 2–5 weeks, your expectation should anchor there — not on the median of every ship ever.</p>";
    $("similar-card").classList.remove("hidden");
  }

  function renderGold(proj, est) {
    if (est && est.decided) { $("gold-card").classList.add("hidden"); return; }
    const h = proj.public_total_hours;
    const rate = window.eta.GOLD_RATES[String(proj.level)] || 40;
    const mult = proj.reward_estimate_multiplier;
    if (h == null) { $("gold-card").classList.add("hidden"); return; }
    const base = window.eta.goldEstimate(h, proj.level, 1.0);
    let headline, sub;
    if (mult) {
      const g = window.eta.goldEstimate(h, proj.level, mult);
      headline = `${g.toLocaleString()} gold`;
      sub = `if approved: ${fmtH(h)} × ${rate}/hr (level ${esc(proj.level)}) × ${Math.round(mult * 100) / 100}× (streak multiplier)`;
    } else {
      headline = `${base.toLocaleString()}+ gold`;
      sub = `base rate: ${fmtH(h)}h × ${rate}/hr (level ${esc(proj.level)}) — before your streak bonus`;
    }
    $("gold-body").innerHTML = `<p class="eta-range">${headline}</p>
      <p class="eta-sub">${sub}</p>
      <p class="muted small">Where this comes from: Macondo pays a base rate per logged hour by project level (L1 40 · L2 45 · L3 50 · L4 60 gold/hour). Your streak bonus (shown on your Macondo project page — live lookups include it) multiplies that base. Your hours are the live Hackatime count, so this grows as you keep working. The final amount is whatever the reviewer confirms — estimates are not commitments.</p>`;
    $("gold-card").classList.remove("hidden");
  }

  async function renderSnapshotFull(meta, note) {
    // Full result card from snapshot data alone (no proxy / no live fetch).
    // meta: the ship's entry from SNAP.queue.ships.
    $("result").classList.remove("hidden");
    const banner = $("result-banner");
    banner.className = "card";
    banner.innerHTML = `<h2><span class="pill wait">In review queue</span> ${esc(meta.name || "Project " + meta.pid)} <span class="pill wait">snapshot</span></h2>
      <p class="eta-sub">${esc(note)}</p>
      <p class="muted small">Submitted ${esc((meta.created_at || "").slice(0, 10))} ·
      <a href="https://macondo.hackclub.com/projects/${meta.pid}">view live status on Macondo ↗</a></p>`;

    // stepper
    const active = { id: meta.id, status: "under_review", created_at: meta.created_at };
    renderStepper(active);

    // ETA from snapshot's own ranked queue
    const est = window.eta.estimate(
      { id: meta.id, status: "under_review", created_at: meta.created_at, hours: meta.hours, level: meta.level },
      SNAP);
    $("eta-card").classList.remove("hidden");
    renderEta(est);

    // your-file panel from snapshot fields
    $("file-card").classList.remove("hidden");
    const submitted = new Date(meta.created_at);
    const waitingDays = Math.floor((Date.now() - submitted.getTime()) / 86400000);
    const kv = [
      ["Project", `<a href="https://macondo.hackclub.com/projects/${meta.pid}">${esc(meta.name || "#" + meta.pid)} ↗</a>`],
      ["Owner", esc(meta.owner || "—")],
      ["Level", meta.level ? `L${esc(meta.level)} · ${esc(meta.type || "")}` : "—"],
      ["Submitted", (meta.created_at || "").slice(0, 10)],
      ["Waiting", waitingDays + " days"],
      ["Hours logged", fmtH(meta.hours) + " (as of snapshot)"],
    ];
    $("file-body").innerHTML = "<dl class='kv'>" + kv.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("") + "</dl>" +
      `<p class="muted small">Deploy the CORS proxy (see How it works) for live status and hours on every check.</p>`;

    // cohort + similar + gold all work from snapshot
    renderCohort(active, est);
    const fakeProj = { public_total_hours: meta.hours, level: meta.level,
                       reward_estimate_multiplier: meta.mult, project_streak_days: null,
                       name: meta.name, id: meta.pid };
    renderSimilar(active, fakeProj, [], est);
    renderGold(fakeProj, est);
    $("result").scrollIntoView({ behavior: "smooth", block: "start" });
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

  let ACTIVE_TYPE = "all";

  function bundleFor(t) {
    if (SNAP && SNAP.by_type && SNAP.by_type[t]) return SNAP.by_type[t];
    // fallback for older snapshots without by_type
    return {
      queue_count: SNAP.queue.count,
      series: SNAP.series, latency: SNAP.latency,
      outcomes: SNAP.outcomes, drain: SNAP.drain,
      type_coverage: null,
    };
  }

  function renderDashboard() {
    const s = SNAP;
    if (!s) return;
    const b = bundleFor(ACTIVE_TYPE);
    const lat = b.latency || {};
    const q = s.queue || {};
    const drain = b.drain || {};
    const typeLabel = ACTIVE_TYPE === "all" ? "" : (ACTIVE_TYPE === "software" ? "software" : "hardware");
    const stats = [
      { v: b.queue_count != null ? b.queue_count : (q.count || 0), k: "ships in queue",
        sub: typeLabel ? typeLabel + " ships waiting right now" : "submitted, waiting on a reviewer right now", tip: "Every ship that has been submitted for review but hasn't received a decision. This is the whole line you're in — reconstructed from public data, so it may miss a few ships on private/hidden projects." },
      { v: q.oldest_days != null ? Math.round(q.oldest_days) + "d" : "—", k: "longest wait",
        sub: "the oldest ship still waiting (all types)", tip: "How long the most patient ship has been in line. The higher this is, the more behind the review team is — a healthy queue keeps this under 2–3 weeks." },
      { v: q.front_date || "—", k: "review front",
        sub: "oldest waiting submission (all types)", tip: "The submission date of the oldest waiting ship. Decisions being made today are typically for ships submitted around this date — it's where the review 'front' has reached in the backlog." },
      { v: drain.decisions_per_day_14d != null ? drain.decisions_per_day_14d : "—", k: "decisions/day",
        sub: typeLabel ? typeLabel + " pace, last 14 days" : "reviewer pace over the last 14 days", tip: "Average ships decided per day over the trailing two weeks. Reviewers are volunteers — this varies a lot week to week (bursts of 5–10/day happen, so do quiet weeks)." },
      { v: lat.median_days != null ? Math.round(lat.median_days) + "d" : "—", k: "median wait",
        sub: typeLabel ? "median for decided " + typeLabel + " ships" : "half of decided ships waited this long", tip: "The middle of the distribution: half of all decided ships waited less than this, half waited more. Based on every decided ship in our data, not just recent ones." },
      { v: b.outcomes && b.outcomes.approval != null ? Math.round(b.outcomes.approval * 100) + "%" : (s.outcomes.approval != null ? Math.round(s.outcomes.approval * 100) + "%" : "—"), k: "approval rate",
        sub: "of ships that got a decision", tip: "Among ships that received any decision, the share approved. The rest were rejected or asked for changes. Your odds depend on your hours, level, and documentation — see the approval breakdown below." },
    ];
    $("stat-cards").innerHTML = stats.map(x =>
      `<div class="stat"><div class="v">${x.v}</div><div class="k">${x.k}<i class="tip" tabindex="0" data-tip="${esc(x.tip)}"></i></div><div class="sub">${x.sub}</div></div>`).join("");

    // plain-language TL;DR of the whole dashboard
    const trend = drain.daily_net != null ? (drain.daily_net < -0.05
      ? `the queue is <strong>shrinking</strong> by about ${Math.abs(drain.daily_net)} ship/day`
      : drain.daily_net > 0.05
        ? `the queue is <strong>growing</strong> by about ${drain.daily_net} ship/day`
        : "the queue size is <strong>holding steady</strong>") : "queue trend unclear";
    let clearsTxt;
    if (drain.gate_closed && drain.clears_by) {
      clearsTxt = ` With the submission gate closed and nothing new arriving, <strong>all reviews should finish around ${esc(drain.clears_by)}</strong> at this pace.`;
    } else {
      clearsTxt = drain.clears_by
        ? ` At this pace the current backlog would clear around <strong>${esc(drain.clears_by)}</strong>.`
        : "";
    }
    $("dashboard-tldr").innerHTML =
      `Right now: <strong>${b.queue_count != null ? b.queue_count : "?"}</strong>${typeLabel ? " " + typeLabel : ""} ships waiting · reviewers decide <strong>${drain.decisions_per_day_14d ?? "?"}</strong>/day · ${trend}.${clearsTxt}`;

    if (window.renderCharts) window.renderCharts({ series: b.series, drain: b.drain });

    // ---- big picture: every project on Macondo, accounted for
    const pipe = s.pipeline || {};
    const totalShips = s.meta.n_ships || null;
    const totalLive = (s.meta && s.meta.n_live_projects_total) || null;
    const withShips = s.meta.n_projects || null;
    const decidedShips = (pipe.shipped || 0) + (pipe.rejected || 0) + (pipe.needs_changes || 0);
    const neverSubmitted = (totalLive != null && withShips != null) ? totalLive - withShips : null;
    const distinctWaiting = new Set((q.ships || []).map(x => x.pid)).size || null;
    const bp = [
      { v: totalLive != null ? totalLive.toLocaleString() : "12,210", k: "projects on Macondo", tip: "Every live (non-deleted) project, from our complete scan of all 17,451 project IDs." },
      { v: neverSubmitted != null ? neverSubmitted.toLocaleString() : "≈8,350", k: "never submitted for review", tip: "Projects that exist but have never shipped anything. Most projects never get submitted — this is normal and they cost the queue nothing." },
      { v: withShips != null ? withShips.toLocaleString() : "3,860", k: "submitted at least once", tip: "Projects with at least one ship in their history." },
      { v: distinctWaiting || q.count, k: "projects in queue right now", tip: "Distinct projects with a ship currently awaiting review. Highlighted — this is the live line.", hot: true },
    ];
    $("bigpicture-cards").innerHTML = bp.map(x =>
      `<div class="bp-card${x.hot ? " hot" : ""}"><div class="v">${x.v}</div><div class="k">${x.k}<i class="tip" tabindex="0" data-tip="${esc(x.tip)}"></i></div></div>`).join("");

    // funnel bar: all 4,064 ships by current state
    if (totalShips) {
      const seg = (n, cls) => n ? `<div class="${cls}" style="flex:${n}" title="${n}"></div>` : "";
      $("funnel-bar").innerHTML =
        seg(pipe.shipped || 0, "seg-ok") + seg(pipe.needs_changes || 0, "seg-warn") +
        seg(pipe.rejected || 0, "seg-bad") + seg(pipe.under_review || 0, "seg-wait");
      const pct = (n) => totalShips ? Math.round((n / totalShips) * 100) : 0;
      $("funnel-legend").innerHTML = `
        <span><i class="dot seg-ok"></i>Shipped: <b>${pipe.shipped || 0}</b> (${pct(pipe.shipped)}%)</span>
        <span><i class="dot seg-warn"></i>Needs changes: <b>${pipe.needs_changes || 0}</b> (${pct(pipe.needs_changes)}%)</span>
        <span><i class="dot seg-bad"></i>Rejected: <b>${pipe.rejected || 0}</b> (${pct(pipe.rejected)}%)</span>
        <span><i class="dot seg-wait"></i>In queue: <b>${pipe.under_review || 0}</b> (${pct(pipe.under_review)}%)</span>
        <span class="muted">— every ship ever submitted (${totalShips.toLocaleString()} total)</span>`;
      $("bigpicture-interp").innerHTML =
        `<span class="lead">Reading this</span>Of <strong>${totalShips.toLocaleString()}</strong> ships ever submitted across ${withShips ? withShips.toLocaleString() + " projects" : "all projects"}: <strong>${decidedShips.toLocaleString()}</strong> have been reviewed (that's ${(pipe.shipped || 0).toLocaleString()} approvals, ${(pipe.needs_changes || 0).toLocaleString()} change-requests, ${(pipe.rejected || 0).toLocaleString()} rejections), and <strong>${(pipe.under_review || 0).toLocaleString()}</strong> are waiting right now. Roughly 2 in 3 decided ships get approved at first pass — and change-requests aren't rejections, you revise and rejoin the queue.`;
    }

    // drain verdict — the honest answer to "when will ALL reviews finish?"
    const drainEl = $("drain-body");
    if (drainEl) {
      const dps = drain.decisions_per_day_14d, aps = drain.arrivals_per_day_14d;
      const net = drain.daily_net;
      let verdict, color;
      if (drain.gate_closed) {
        verdict = `The submission gate has closed — <strong>no new ships are arriving</strong>, so the queue is final at <strong>${drain.gate_closed ? (b.queue_count != null ? b.queue_count : "?") : "?"} ships</strong>. ` +
          (drain.clears_by
            ? `Reviewers are deciding ~${dps ?? "?"}/day, which means <strong>all reviews should finish around ${esc(drain.clears_by)}</strong> (roughly ${Math.round((b.queue_count || 0) / Math.max(dps || 1, 0.1))} days at this pace).`
            : `At the current pace of ~${dps ?? "?"}/day there's no finish date yet — the pace is too slow to project.`);
        verdict += ` Reviewer pace has historically swung 4× (sustained 5–6/day pushes in August vs quiet weeks), so treat the date as a midpoint, not a promise.`;
        if (typeLabel && b.type_coverage && b.type_coverage.total) {
          const covPct = Math.round(b.type_coverage.typed / b.type_coverage.total * 100);
          verdict += ` <span class="muted">Type breakdowns use the ${covPct}% of decided ships with known type — the real pace for ${typeLabel} may differ.</span>`;
        }
        color = "var(--ok)";
      } else if (net == null) { verdict = "Not enough recent data to project."; color = "var(--muted)"; }
      else if (net <= 0) {
        verdict = `The queue is <strong>shrinking</strong> by ~${Math.abs(net).toFixed(1)} ships/day.` +
          (drain.clears_by ? ` At this pace the backlog <strong>fully clears around ${esc(drain.clears_by)}</strong>.` : " It keeps shrinking but doesn't hit zero within the projection window.");
        color = "var(--ok)";
      } else {
        verdict = `The queue is <strong>growing</strong> by ~${net.toFixed(1)} ships/day (${aps?.toFixed?.(1) ?? "?"} arriving vs ${dps?.toFixed?.(1) ?? "?"} decided each day). <strong>At this pace the backlog never fully drains</strong> — new ships arrive faster than reviewers decide them. In practice this means long waits until reviewer capacity picks up (it swings a lot: bursts of 5–10/day happen).`;
        color = "var(--danger)";
      }
      drainEl.innerHTML = `<div class="interp" style="border-color:${color}"><span class="lead">The honest answer</span>${verdict}</div>`;
    }

    // queue-depth interpretation
    const dep = (b.series || {}).queue_depth || [];
    if (dep.length > 1) {
      const first = dep[0].n, last = dep[dep.length - 1].n;
      const peak = Math.max(...dep.map(p => p.n));
      const d0 = dep[0].d, d1 = dep[dep.length - 1].d;
      $("depth-interp").innerHTML = `<span class="lead">How to read this</span>
        Since ${esc(d0)}, the queue went from <strong>${first}</strong> to <strong>${last}</strong> waiting ships
        (peak: <strong>${peak}</strong>). ${last < first ? "The backlog is smaller than it was — reviewers have been catching up." : "The backlog has grown — more ships have arrived than reviewers have decided."}`;
    }

    // approval rates breakdown (follows the type toggle)
    const o = b.outcomes || {};
    const cell = (label, obj) => obj && obj.n
      ? `<li>${label}: <strong>${Math.round(obj.approval * 100)}%</strong> <span class="muted small">(n=${obj.n})</span></li>` : "";
    $("approval-body").innerHTML = `<ul class="clean">
      <li>Overall: <strong>${Math.round((o.approval || 0) * 100)}%</strong> <span class="muted small">(n=${o.n_decided})</span></li>
      ${cell("Level 1", (o.by_level || {})["1"])}${cell("Level 2", (o.by_level || {})["2"])}
      ${cell("Level 3", (o.by_level || {})["3"])}${cell("Level 4", (o.by_level || {})["4"])}
      ${(o.by_hours ? ["<10h", "10-50h", "50-100h", "100h+"].map(b => cell(b, o.by_hours[b])).join("") : "")}
    </ul>
    <p class="muted small">Needs-changes rate: ${Math.round((o.needs_changes || 0) * 100)}% · Fraud-related rejections: ${Math.round((o.fraud_rejection || 0) * 100)}%</p>`;

    const approveInterp = $("approval-interp");
    if (approveInterp) {
      const nc = o.needs_changes || 0;
      approveInterp.innerHTML =
        `About <strong>${Math.round((o.approval || 0) * 100)}%</strong> of decided ships get approved.
         ${nc > 0.12 ? `But <strong>${Math.round(nc * 100)}%</strong> are asked for changes first — that's not a rejection: you revise, resubmit, and rejoin the queue (your wait clock starts over).` : "Few ships are asked for changes."}
         Rejections are rare (${Math.round((o.rejected || 0) * 100)}%), and most affect zero-hour or minimal-effort ships.`;
    }

    // composition — also answers "why only 1,384 when Macondo has 12k+ projects?"
    const ships = q.ships || [];
    const byLevel = {};
    ships.forEach(x => byLevel[x.level] = (byLevel[x.level] || 0) + 1);
    const hoursList = ships.map(x => x.hours || 0).filter(h => h != null);
    hoursList.sort((a, b) => a - b);
    const medH = hoursList.length ? Math.round(hoursList[Math.floor(hoursList.length / 2)] * 10) / 10 : null;
    const bigShips = hoursList.filter(h => h >= 100).length;
    $("composition-body").innerHTML = `<ul class="clean">` +
      Object.keys(byLevel).sort().map(l => `<li>Level ${esc(l)}: ${byLevel[l]} waiting</li>`).join("") +
      `</ul><p class="muted small">${ships.length} ships · median ${medH}h logged · ${bigShips} with 100h+</p>
      <div class="interp"><span class="lead">Why "only" ${ships.length}?</span> Macondo has ~${totalLive ? totalLive.toLocaleString() : "12,000"} projects — but the queue counts <strong>ships awaiting review</strong>, not projects. Most projects have never been submitted for review; of those that have, almost all are already decided (${(s.pipeline && s.pipeline.shipped) || "?"} shipped so far). What's left is the ${ships.length}-ship line you're in.</div>
      ${medH != null && medH < 15 ? '<div class="interp"><span class="lead">Reading</span>Most waiting ships have few hours — reviewers can clear many of them in a single active day. Big-hour ships (like 100h+) are rarer and may get more careful, slower looks.</div>' : ""}`;

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
        <p>Fastest decision: <strong>${fmtDays(rec.fastest.waited_d)}</strong> — ${esc(rec.fastest.name || "#" + rec.fastest.pid)}</p>
        <p class="muted small">Range is the story: the same queue contains 1-hour approvals and 3-month waits. Claim-based review means your ship can land anywhere in that range.</p>`
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
    // type toggle: re-render type-sensitive stats + charts
    const tt = $("type-toggle");
    if (tt) {
      tt.querySelectorAll("button").forEach(btn => {
        btn.addEventListener("click", () => {
          if (btn.dataset.t === ACTIVE_TYPE) return;
          ACTIVE_TYPE = btn.dataset.t;
          tt.querySelectorAll("button").forEach(b => b.classList.toggle("active", b === btn));
          if (SNAP) renderDashboard();
        });
      });
    }
  });
})();
