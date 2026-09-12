/** Dashboard + queue charts (Chart.js via CDN). Degrades silently if absent. */
(function () {
  const css = getComputedStyle(document.documentElement);
  const C = {
    text: css.getPropertyValue("--muted").trim(),
    accent: css.getPropertyValue("--accent").trim(),
    ok: css.getPropertyValue("--ok").trim(),
    warn: css.getPropertyValue("--warn").trim(),
    danger: css.getPropertyValue("--danger").trim(),
    border: css.getPropertyValue("--border").trim(),
  };
  const base = {
    responsive: true,
    plugins: { legend: { display: false }, tooltip: { intersect: false } },
    scales: {
      x: { ticks: { color: C.text, maxTicksLimit: 8, font: { size: 10 } }, grid: { color: C.border } },
      y: { ticks: { color: C.text, font: { size: 10 } }, grid: { color: C.border }, beginAtZero: true },
    },
  };

  function make(id, cfg) {
    const el = document.getElementById(id);
    if (!el || typeof Chart === "undefined") return;
    try {
      // re-rendering on type-toggle: destroy the old chart or Chart.js throws
      const existing = Chart.getChart(el);
      if (existing) existing.destroy();
      new Chart(el, cfg);
    } catch (e) {
      (window.__chartErrs = window.__chartErrs || []).push(id + ": " + e.message);
    }
  }

  window.renderCharts = function (snap) {
    const s = snap.series || {};
    const drain = snap.drain || {};

    // drain projection (line): projected queue depth week by week
    const dp = drain.path || [];
    if (dp.length) {
      make("chart-drain", {
        type: "line",
        data: {
          labels: dp.map(p => "+" + p.d + "d"),
          datasets: [{
            data: dp.map(p => p.depth),
            borderColor: dp[dp.length - 1].depth <= dp[0].depth ? C.ok : C.danger,
            borderWidth: 2, pointRadius: 0, tension: 0.15,
            fill: { target: "origin", backgroundColor: "rgba(247,118,142,.08)" },
          }],
        },
        options: base,
      });
    }

    // queue depth (line)
    make("chart-depth", {
      type: "line",
      data: {
        labels: (s.queue_depth || []).map(p => p.d),
        datasets: [{
          data: (s.queue_depth || []).map(p => p.n),
          borderColor: C.accent, borderWidth: 2, pointRadius: 0,
          fill: { target: "origin", backgroundColor: "rgba(122,162,247,.12)" },
          tension: 0.15,
        }],
      },
      options: base,
    });

    // decisions/day (bar)
    const dec = s.decisions_daily || [];
    make("chart-decisions", {
      type: "bar",
      data: {
        labels: dec.map(p => p.d),
        datasets: [{ data: dec.map(p => p.n), backgroundColor: C.ok, maxBarThickness: 8 }],
      },
      options: base,
    });

    // latency histogram (bar)
    const lat = s.latency_hist || [];
    make("chart-latency", {
      type: "bar",
      data: {
        labels: lat.map(p => p.label),
        datasets: [{ data: lat.map(p => p.n), backgroundColor: C.warn, maxBarThickness: 40 }],
      },
      options: base,
    });

    // survival curve (line)
    const sv = s.survival || [];
    make("chart-survival", {
      type: "line",
      data: {
        labels: sv.map(p => p.d + "d"),
        datasets: [{
          data: sv.map(p => Math.round(p.decided_pct * 100)),
          borderColor: C.ok, borderWidth: 2, pointRadius: 0, tension: 0.2,
          fill: { target: "origin", backgroundColor: "rgba(158,206,106,.10)" },
        }],
      },
      options: {
        ...base,
        scales: {
          ...base.scales,
          y: { ...base.scales.y, max: 100, ticks: { ...base.scales.y.ticks, callback: v => v + "%" } },
        },
        plugins: { ...base.plugins, tooltip: { callbacks: { label: ctx => ctx.parsed.y + "% decided" } } },
      },
    });

    // arrivals/week (bar)
    const arr = s.arrivals_weekly || [];
    make("chart-arrivals", {
      type: "bar",
      data: {
        labels: arr.map(p => p.w),
        datasets: [{ data: arr.map(p => p.n), backgroundColor: C.accent, maxBarThickness: 14 }],
      },
      options: base,
    });
  };
})();
