/* Audit tab — pick a job, verify its hash-chained audit trail and
 * inspect the sealed evidence manifest. The badge re-computes nothing
 * locally: it displays the server's re-verification of the stored chain. */

import { api } from "../api.js";
import { $, escapeHtml } from "../util.js";
import { onJobChange, state } from "../state.js";

export function initAuditView() {
  $("#audit-refresh").addEventListener("click", loadJobOptions);
  $("#audit-verify").addEventListener("click", verify);
  onJobChange((jobId) => {
    if (jobId) $("#audit-job-select").value = jobId;
  });
}

export async function loadJobOptions() {
  try {
    const jobs = await api("/orchestration/reports?limit=50");
    const sel = $("#audit-job-select");
    const keep = state.currentJob;
    sel.innerHTML = '<option value="">— select a job —</option>';
    for (const j of jobs) {
      const opt = document.createElement("option");
      opt.value = j.job_id;
      opt.textContent = `${j.topic} · ${j.status} · ${j.job_id.slice(0, 8)}`;
      sel.appendChild(opt);
    }
    if (keep) sel.value = keep;
  } catch { /* list is best-effort */ }
}

async function verify() {
  const jobId = $("#audit-job-select").value;
  const badge = $("#audit-verify-badge");
  const root = $("#audit-body");
  if (!jobId) { root.innerHTML = '<p class="modal-error">Pick a job first.</p>'; return; }
  root.innerHTML = "<p>Verifying…</p>";
  try {
    const a = await api(`/orchestration/reports/${jobId}/audit`);
    const sig = a.signature_valid === true ? " · signed ✓"
      : a.signature_valid === false ? " · signature ✗" : "";
    badge.textContent = (a.valid ? "chain ✓ valid" : "chain ✗ INVALID") + sig;
    badge.className = "chip chip-mono " + (a.valid && a.signature_valid !== false ? "chip-ok" : "chip-bad");
    root.innerHTML = `
      <div class="perf-grid">
        <div class="stat"><span class="stat-label">Entries</span><span class="stat-value">${(a.entries || []).length}</span></div>
        <div class="stat"><span class="stat-label">Audit root</span><span class="stat-value font-mono">${escapeHtml(String(a.audit_root || "").slice(0, 16))}…</span></div>
        <div class="stat"><span class="stat-label">Evidence root</span><span class="stat-value font-mono">${escapeHtml(String(a.evidence_root || "—").slice(0, 16))}</span></div>
      </div>
      ${renderManifest(a.evidence_manifest)}`;
  } catch (ex) {
    badge.textContent = "unverified";
    badge.className = "chip chip-mono";
    root.innerHTML = `<p class="modal-error">${escapeHtml(ex.message)}</p>`;
  }
}

function renderManifest(manifest) {
  const items = manifest?.items;
  if (!Array.isArray(items) || !items.length) {
    return '<p class="perf-sub">No sealed evidence manifest for this job.</p>';
  }
  const rows = items.map((s) =>
    `<tr><td class="font-mono" title="${escapeHtml(s.sha256)}">#${escapeHtml(String(s.sha256).slice(0, 12))}</td>` +
    `<td>${escapeHtml(s.title || "")}</td>` +
    `<td class="font-mono">${escapeHtml(s.content_type || "")}</td>` +
    `<td>${s.url ? `<a href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer">source</a>` : "—"}</td></tr>`
  ).join("");
  return `<h3 class="card-title">Evidence manifest (${items.length} sealed sources)</h3>` +
    `<table class="perf-table"><thead><tr><th>Hash</th><th>Title</th><th>Type</th><th>Link</th></tr></thead><tbody>${rows}</tbody></table>`;
}
