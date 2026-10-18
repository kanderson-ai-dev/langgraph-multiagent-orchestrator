/* Jobs view — brief submission, recent-jobs list, live SSE transcript
 * and the per-workspace dashboard strip. */

import { api, apiPost } from "../api.js";
import { openStream } from "../sse.js";
import { $, escapeHtml, prettyName, shortDate, toast } from "../util.js";
import { state, emitJobChange } from "../state.js";
import { renderReport } from "./report.js";
import { openHitl, closeHitl, resetHitlState } from "./review.js";

let stream = null;

/* ── dashboard strip ────────────────────────────────────────────────── */

export async function refreshDashboard() {
  try {
    const s = await api("/dashboard/summary");
    $("#ws-badge").textContent = `ws: ${s.workspace_id}`;
    $("#st-cost").textContent = `$${s.total_cost_usd.toFixed(4)}`;
    $("#st-calls").textContent = s.llm_calls;
    $("#st-lat").textContent = s.avg_latency_ms ? `${Math.round(s.avg_latency_ms)}ms` : "—";
    $("#st-tok").textContent = `${s.total_prompt_tokens}/${s.total_completion_tokens}`;
    const rc = $("#role-costs");
    rc.innerHTML = "";
    for (const [role, c] of Object.entries(s.cost_by_role || {})) {
      const row = document.createElement("div");
      row.innerHTML =
        `<span>${escapeHtml(prettyName(role))}</span><span>$${c.toFixed(4)}</span>`;
      rc.appendChild(row);
    }
  } catch { /* dashboard is best-effort */ }
}

/* ── recent jobs list ───────────────────────────────────────────────── */

export async function refreshJobs() {
  try {
    const jobs = await api("/orchestration/reports");
    const ul = $("#jobs-list");
    ul.innerHTML = "";
    $("#jobs-empty").classList.toggle("hidden", jobs.length > 0);
    for (const j of jobs) {
      const li = document.createElement("li");
      li.className = "jobs-item" + (j.job_id === state.currentJob ? " active" : "");
      li.innerHTML =
        `<span class="jobs-topic">${escapeHtml(j.topic)}</span>` +
        `<span class="jobs-meta">` +
        `<span class="status sm ${escapeHtml(j.status)}">${escapeHtml(prettyName(j.status))}</span>` +
        `<span class="jobs-when">${escapeHtml(shortDate(j.created_at))}</span>` +
        `<span class="jobs-cost">$${(j.cost_usd || 0).toFixed(3)}</span>` +
        `</span>`;
      li.title = `${prettyName(j.report_type)} · ${j.job_id}`;
      li.addEventListener("click", () => {
        history.replaceState(null, "", `?job=${encodeURIComponent(j.job_id)}`);
        attachJob(j.job_id);
        refreshJobs();
      });
      ul.appendChild(li);
    }
  } catch { /* list is best-effort */ }
}

/* ── job lifecycle ──────────────────────────────────────────────────── */

export function setStatus(status) {
  const el = $("#job-status");
  el.textContent = prettyName(status);
  el.className = `status ${status}`;
  // A paused job keeps a persistent "Review required" affordance so the
  // modal can be dismissed and reopened without losing the exit path.
  $("#review-btn").classList.toggle(
    "hidden", status !== "awaiting_review" || state.hitlOpen
  );
}

function feedEntry(node, html, cls = "") {
  const feed = $("#feed");
  const li = document.createElement("li");
  li.innerHTML = `<span class="feed-node ${escapeHtml(node)}">${escapeHtml(prettyName(node))}</span>` +
                 (html ? `<p class="feed-msg ${cls}">${html}</p>` : "");
  feed.appendChild(li);
  feed.parentElement.scrollTop = feed.parentElement.scrollHeight;
}

function handleEvent(ev) {
  if (ev.node && ev.node !== "runner") {
    if (ev.interrupt) {
      feedEntry(ev.node, "Paused — awaiting human decision.", "escalation");
      refreshJobs();
      // The interrupt event carries the server's guided set — open the modal
      // immediately; the persisted-result poll only runs as a fallback.
      if (ev.allowed_actions) openHitl(ev.reason, ev.allowed_actions);
      else fetchAndOpenHitl();
      return;
    }
    if (ev.messages && ev.messages.length) {
      for (const msg of ev.messages) {
        const tag = ["verdict", "escalation"].includes(msg.kind)
          ? `<span class="kind">${escapeHtml(msg.kind)}</span>` : "";
        feedEntry(
          ev.node,
          `${tag}${escapeHtml(msg.content)}`,
          msg.kind === "verdict" ? "verdict" : msg.kind === "escalation" ? "escalation" : ""
        );
      }
    } else {
      feedEntry(ev.node, "");
    }
  }
  if (ev.status) setStatus(ev.status);
  if (ev.error) { feedEntry("runner", escapeHtml(ev.error), "escalation"); toast("Job failed", true); }
  if (ev.terminal) {
    closeStream();
    finalize();
  }
}

async function fetchAndOpenHitl() {
  // The interrupt SSE event can arrive before the runner persists the
  // escalation fields — poll briefly so the modal always opens on the
  // pause's real reason + guided actions, never on stale retained ones.
  for (let i = 0; i < 8; i++) {
    try {
      const job = await api(`/orchestration/reports/${state.currentJob}`);
      if (job.status === "awaiting_review" && job.result?.escalation_reason) {
        openHitl(job.result.escalation_reason, job.result.allowed_actions);
        return;
      }
    } catch { /* retry */ }
    await new Promise((r) => setTimeout(r, 400));
  }
  openHitl();
}

export async function attachJob(jobId) {
  closeStream();
  emitJobChange(jobId);
  resetHitlState();
  $("#empty").classList.add("hidden");
  $("#job-view").classList.remove("hidden");
  $("#feed").innerHTML = "";
  $("#report-card").classList.add("hidden");
  $("#job-id").textContent = jobId;
  $("#pdf-link").classList.add("hidden");
  // Neutral placeholder until the fetch lands — claiming "queued" briefly is a
  // lie for jobs that are already running or done.
  const st = $("#job-status");
  st.textContent = "…";
  st.className = "status";
  $("#review-btn").classList.add("hidden");

  const job = await api(`/orchestration/reports/${jobId}`);
  $("#job-topic").textContent = job.brief?.topic || "";
  const meta = [
    prettyName(job.brief?.report_type), prettyName(job.brief?.tone),
    job.brief?.budget_usd != null ? `budget $${job.brief.budget_usd}` : null,
    job.brief?.tenant_context || null,
  ].filter(Boolean).join(" · ");
  $("#job-meta").textContent = meta;
  const teamEl = $("#job-team");
  teamEl.innerHTML = "";
  for (const member of job.team || []) {
    const chip = document.createElement("span");
    const specialist = !["researcher", "writer", "reviewer"].includes(member);
    chip.className = `chip agent-${member}` + (specialist ? " specialist" : "");
    chip.textContent = prettyName(member);
    teamEl.appendChild(chip);
  }
  $("#job-cost").textContent = `$${(job.cost_usd || 0).toFixed(4)}`;
  setStatus(job.status);
  if (job.result?.report) renderReport(job.result, jobId);
  if (job.status === "awaiting_review") {
    openHitl(job.result?.escalation_reason, job.result?.allowed_actions);
  }

  // Reattach history: a finished job carries its transcript in the
  // persisted result — replay it so the feed shows the full debate even
  // though the SSE stream is no longer open (e.g. reopened from the list).
  for (const msg of job.result?.transcript || []) {
    const tag = ["verdict", "escalation"].includes(msg.kind)
      ? `<span class="kind">${escapeHtml(msg.kind)}</span>` : "";
    feedEntry(
      msg.sender || "system",
      `${tag}${escapeHtml(msg.content || "")}`,
      msg.kind === "verdict" ? "verdict" : msg.kind === "escalation" ? "escalation" : ""
    );
  }

  if (!["done", "failed", "blocked", "expired"].includes(job.status)) {
    stream = openStream(jobId, handleEvent);
  }
}

function closeStream() {
  if (stream) { stream.close(); stream = null; }
}

export function onResumed(jobId) {
  setStatus("running");
  if (!stream) stream = openStream(jobId, handleEvent);
}

async function finalize() {
  if (!state.currentJob) return;
  const job = await api(`/orchestration/reports/${state.currentJob}`);
  setStatus(job.status);
  $("#job-cost").textContent = `$${(job.cost_usd || 0).toFixed(4)}`;
  if (job.status === "awaiting_review") {
    openHitl(job.result?.escalation_reason, job.result?.allowed_actions);
    return;
  }
  closeHitl();
  if (job.result?.report) renderReport(job.result, job.job_id);
  refreshDashboard();
  refreshJobs();
}

/* ── form ───────────────────────────────────────────────────────────── */

export function initJobsView() {
  $("#job-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const btn = $("#submit-btn");
    btn.disabled = true;
    try {
      const job = await apiPost("/orchestration/reports", {
        topic: f.topic.value.trim(),
        audience: f.audience.value.trim() || "general business",
        tone: f.tone.value,
        report_type: f.report_type.value,
        tenant_context: f.tenant_context.value.trim(),
        requirements: f.requirements.value.split("\n").map(s => s.trim()).filter(Boolean),
        ...(isNaN(parseFloat(f.budget_usd.value))
          ? {} : { budget_usd: parseFloat(f.budget_usd.value) }),
      });
      history.replaceState(null, "", `?job=${encodeURIComponent(job.job_id)}`);
      attachJob(job.job_id);
      refreshJobs();
    } catch (ex) {
      toast(`Submission failed: ${ex.message}`, true);
    } finally {
      btn.disabled = false;
    }
  });
}
