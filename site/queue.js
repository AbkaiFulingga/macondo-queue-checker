/** Full-queue table: sortable/filterable view over snapshot.queue.ships. */
(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function ageDays(iso) {
    if (!iso) return null;
    const d = (Date.now() - new Date(iso).getTime()) / 86400000;
    return d >= 0 ? d : null;
  }
  const fmtH = (h) => h == null ? "—" : (Math.round(h * 10) / 10) + "h";

  function render(snap) {
    let ships = (snap.queue && snap.queue.ships) || [];
    const level = $("q-level").value;
    const filt = $("q-filter").value.trim().toLowerCase();
    if (level) ships = ships.filter(s => String(s.level) === level);
    if (filt) ships = ships.filter(s =>
      ((s.name || "").toLowerCase().includes(filt)) ||
      ((s.owner || "").toLowerCase().includes(filt)));

    const sort = $("q-sort").value;
    if (sort === "newest") ships = ships.slice().sort((a, b) => b.rank_by_age - a.rank_by_age);
    else if (sort === "hours") ships = ships.slice().sort((a, b) => (b.hours || 0) - (a.hours || 0));
    else ships = ships.slice().sort((a, b) => a.rank_by_age - b.rank_by_age);

    $("queue-table").innerHTML = `<p class="muted small">${ships.length} ships ·
      data as of ${esc((snap.generated_at || "").replace("T", " ").replace("Z", " UTC"))}</p>
      <table><thead><tr><th>#</th><th>Project</th><th>Owner</th><th>Lvl</th><th>Type</th><th>Hours</th><th>Submitted</th><th>Waiting</th></tr></thead>
      <tbody>` +
      ships.map(s => {
        const age = ageDays(s.created_at);
        return `<tr>
          <td>${s.rank_by_age}</td>
          <td><a href="./?pid=${s.pid}" onclick="sessionStorage.setItem('pid','${s.pid}');location.href='./';return false">${esc(s.name || "#" + s.pid)}</a></td>
          <td>${esc(s.owner || "—")}</td>
          <td>L${esc(s.level)}</td>
          <td>${esc(s.type || "—")}</td>
          <td>${fmtH(s.hours)}</td>
          <td>${esc((s.created_at || "").slice(0, 10))}</td>
          <td>${age != null ? Math.round(age) + "d" : "—"}</td>
        </tr>`;
      }).join("") + "</tbody></table>";
  }

  async function init() {
    try {
      const r = await fetch("data/snapshot.json");
      const snap = await r.json();
      render(snap);
      ["q-filter", "q-level", "q-sort"].forEach(id =>
        $(id).addEventListener("input", () => render(snap)));
    } catch (e) {
      $("queue-table").innerHTML = '<p class="muted">Snapshot unavailable.</p>';
    }
  }
  document.addEventListener("DOMContentLoaded", init);
})();
