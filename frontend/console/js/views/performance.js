/* Performance & Cost tab — renders /dashboard/performance: rates,
 * per-role cost, latency quantiles, daily trend, budget usage,
 * prompt-version mix and the human-vs-agent comparison (explicit
 * empty state when the API returns none). */

import { api } from "../api.js";
import { $, escapeHtml, prettyName } from "../util.js";

export function initPerformanceView() {
  $("#perf-refresh").addEventListener("click", loadPerformance);
}

export async function loadPerformance() {
  const root = $("#perf-body");
  root.innerHTML = "<p>Loading…</p>";
  try {
    const p = await api("/dashboard/performance");
    root.innerHTML = "";
    if (!(p.totals?.jobs > 0)) {
      const empty = document.createElement("p");
      empty.className = "empty-note";
      empty.textContent =
        "No completed jobs yet — submit a brief from the Jobs tab and this " +
        "panel fills with measured cost, latency and outcomes.";
      root.appendChild(empty);
    }
    renderRates(p, root);
    renderCost(p, root);
    renderLatency(p, root);
    renderDaily(p, root);
    renderBudget(p, root);
    renderMix(p, root);
    renderHuman(p, root);
    const stamp = document.createElement("p");
    stamp.className = "perf-stamp";
    stamp.textContent = `price table ${p.price_table_version}`;
    root.appendChild(stamp);
  } catch (ex) {
    root.innerHTML = `<p class="modal-error">${escapeHtml(ex.message)}</p>`;
  }
}

function section(root, title) {
  const el = document.createElement("section");
  el.className = "perf-section card";
  el.innerHTML = `<h3 class="card-title">${escapeHtml(title)}</h3>`;
  root.appendChild(el);
  return el;
}

const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);
const usd = (v, digits = 4) => (v == null ? "—" : `$${Number(v).toFixed(digits)}`);

function renderRates(p, root) {
  const el = section(root, "Outcomes");
  const t = p.totals?.by_status || {};
  el.innerHTML += `
    <div class="perf-grid">
      <div class="stat"><span class="stat-label">Success rate</span><span class="stat-value">${pct(p.success_rate)}</span></div>
      <div class="stat"><span class="stat-label">Convergence</span><span class="stat-value">${pct(p.convergence_rate)}</span></div>
      <div class="stat"><span class="stat-label">Escalation</span><span class="stat-value">${pct(p.escalation_rate)}</span></div>
      <div class="stat"><span class="stat-label">Jobs</span><span class="stat-value">${p.totals?.jobs ?? 0}</span></div>
    </div>
    <div class="perf-statuses">${Object.entries(t).map(([s, n]) =>
      `<span class="status sm ${escapeHtml(s)}">${escapeHtml(s)}: ${n}</span>`).join("")}
    </div>`;
  const reasons = Object.entries(p.escalation_reasons || {});
  if (reasons.length) {
    el.innerHTML += `<p class="perf-sub">escalations: ${reasons.map(([r, n]) =>
      `${escapeHtml(r)} ×${n}`).join(", ")}</p>`;
  }
}

function renderCost(p, root) {
  const el = section(root, "Cost by role");
  const roles = Object.entries(p.cost?.by_role || {});
  el.innerHTML += `<p class="perf-sub">total ${usd(p.cost?.total_usd)} · avg/job ${usd(p.cost?.avg_per_job_usd, 6)}</p>`;
  if (!roles.length) { el.innerHTML += '<p class="perf-sub">No LLM usage yet.</p>'; return; }
  const rows = roles.map(([role, r]) =>
    `<tr><td>${escapeHtml(prettyName(role))}</td><td class="font-mono">${usd(r.cost_usd)}</td>` +
    `<td class="font-mono">${r.calls}</td><td class="font-mono">${Math.round(r.avg_latency_ms)}ms</td>` +
    `<td class="font-mono">${r.prompt_tokens}/${r.completion_tokens}</td></tr>`).join("");
  el.innerHTML += `<table class="perf-table"><thead><tr><th>Role</th><th>Cost</th><th>Calls</th><th>Avg ms</th><th>Tok in/out</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderLatency(p, root) {
  const el = section(root, "Latency (rolling)");
  const job = p.latency_seconds?.job || {};
  el.innerHTML += `<p class="perf-sub">job p50 ${job.p50 ?? "—"}s · p95 ${job.p95 ?? "—"}s</p>`;
  const nodes = Object.entries(p.latency_seconds?.nodes || {});
  if (!nodes.length) { el.innerHTML += '<p class="perf-sub">No node samples yet.</p>'; return; }
  const rows = nodes.map(([n, q]) =>
    `<tr><td>${escapeHtml(prettyName(n))}</td><td class="font-mono">${q.p50 ?? "—"}s</td><td class="font-mono">${q.p95 ?? "—"}s</td><td class="font-mono">${q.count ?? "—"}</td></tr>`).join("");
  el.innerHTML += `<table class="perf-table"><thead><tr><th>Node</th><th>p50</th><th>p95</th><th>n</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderDaily(p, root) {
  const el = section(root, "Daily trend (14d)");
  const days = p.daily || [];
  if (!days.length) { el.innerHTML += '<p class="perf-sub">No jobs in the window.</p>'; return; }
  const rows = days.map((d) =>
    `<tr><td class="font-mono">${escapeHtml(d.date)}</td><td class="font-mono">${d.jobs}</td><td class="font-mono">${usd(d.cost_usd)}</td></tr>`).join("");
  el.innerHTML += `<table class="perf-table"><thead><tr><th>Day</th><th>Jobs</th><th>Cost</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderBudget(p, root) {
  const el = section(root, "Budget usage");
  const b = p.budget || {};
  el.innerHTML += `<p class="perf-sub">${b.jobs_with_budget ?? 0} jobs with caps · ` +
    `spent ${usd(b.spent_usd)} of ${usd(b.budget_usd)}` +
    (b.pct_used != null ? ` (${b.pct_used}%)` : "") + `</p>`;
}

function renderMix(p, root) {
  const el = section(root, "Prompt version mix");
  const mix = p.prompt_version_mix || [];
  if (!mix.length) { el.innerHTML += '<p class="perf-sub">No prompt-tagged calls yet.</p>'; return; }
  const rows = mix.map((m) =>
    `<tr><td>${escapeHtml(m.prompt_name)}</td><td class="font-mono">v${m.prompt_version}</td><td class="font-mono">${m.calls}</td></tr>`).join("");
  el.innerHTML += `<table class="perf-table"><thead><tr><th>Prompt</th><th>Version</th><th>Calls</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderHuman(p, root) {
  const el = section(root, "Human vs agent");
  const h = p.human_vs_agent;
  if (!h) {
    el.innerHTML += '<p class="perf-sub">No completed reports yet — ' +
      "the comparison appears once the first job finishes.</p>";
    return;
  }
  el.innerHTML += `
    <div class="perf-grid">
      <div class="stat"><span class="stat-label">Reports</span><span class="stat-value">${h.reports_completed}</span></div>
      <div class="stat"><span class="stat-label">Human / report</span><span class="stat-value">${usd(h.cost_per_report.human_usd, 2)}</span></div>
      <div class="stat"><span class="stat-label">Agent / report</span><span class="stat-value">${usd(h.cost_per_report.agent_usd, 4)}</span></div>
      <div class="stat"><span class="stat-label">Saved</span><span class="stat-value">${usd(h.savings_usd, 2)}</span></div>
    </div>
    <p class="perf-sub">assumes ${h.hours_per_report}h/report at ${usd(h.hourly_usd, 2)}/h</p>`;
}
