/* Operator console — submit briefs, stream orchestration events over SSE,
 * resolve HITL reviews, render the final report. Auth-free in local dev
 * (the API scopes everything to the `default` workspace). */

const API = "/api/v1";
const $ = (sel) => document.querySelector(sel);

let currentJob = null;
let eventSource = null;
let hitlOpen = false;

/* ── minimal safe markdown renderer ─────────────────────────────────── */

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
}

function md(src) {
  const esc = escapeHtml(src);
  const lines = esc.split(/\r?\n/);
  const out = [];
  let inList = false, inUl = false, para = [];

  const inline = (t) => t
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[\^(\d+)\^\]/g, "<sup>$1</sup>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    // bare URLs (claim — https://…) become real links; quoted/href'd URLs
    // are skipped because they are never preceded by space/>/—
    .replace(/(^|[\s>—])(https?:\/\/[^\s<)&"']+)/g,
             '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');

  const flushPara = () => {
    if (para.length) { out.push(`<p>${para.map(inline).join(" ")}</p>`); para = []; }
  };
  const flushList = () => {
    if (inList) { out.push(inUl ? "</ul>" : "</ol>"); inList = false; }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)/))) {
      flushPara(); flushList();
      const lvl = Math.min(m[1].length + 1, 4);
      out.push(`<h${lvl}>${inline(m[2])}</h${lvl}>`);
    } else if (/^(---+|\*\*\*+)$/.test(line.trim())) {
      flushPara(); flushList(); out.push("<hr>");
    } else if ((m = line.match(/^[-*]\s+(.*)/))) {
      flushPara();
      if (!inList || !inUl) { flushList(); out.push("<ul>"); inList = true; inUl = true; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^\[\^(\d+)\^\]:\s*(.*)/))) {
      flushPara(); flushList();
      out.push(`<p class="footnote"><sup>${m[1]}</sup> ${inline(m[2])}</p>`);
    } else if ((m = line.match(/^\d+\.\s+(.*)/))) {
      flushPara();
      if (!inList || inUl) { flushList(); out.push("<ol>"); inList = true; inUl = false; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if (line.startsWith("> ")) {
      flushPara(); flushList();
      out.push(`<blockquote>${inline(line.slice(2))}</blockquote>`);
    } else if (!line.trim()) {
      flushPara(); flushList();
    } else {
      para.push(line.trim());
    }
  }
  flushPara(); flushList();
  return out.join("\n");
}

/* ── toast ──────────────────────────────────────────────────────────── */

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4000);
}

/* ── dashboard ──────────────────────────────────────────────────────── */

async function refreshDashboard() {
  try {
    const s = await (await fetch(`${API}/dashboard/summary`)).json();
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

async function refreshJobs() {
  try {
    const jobs = await (await fetch(`${API}/orchestration/reports`)).json();
    const ul = $("#jobs-list");
    ul.innerHTML = "";
    $("#jobs-empty").classList.toggle("hidden", jobs.length > 0);
    for (const j of jobs) {
      const li = document.createElement("li");
      li.className = "jobs-item" + (j.job_id === currentJob ? " active" : "");
      li.innerHTML =
        `<span class="jobs-topic">${escapeHtml(j.topic)}</span>` +
        `<span class="jobs-meta">` +
        `<span class="status sm ${escapeHtml(j.status)}">${escapeHtml(j.status)}</span>` +
        `<span class="jobs-cost">$${(j.cost_usd || 0).toFixed(3)}</span>` +
        `</span>`;
      li.title = `${j.report_type} · ${j.job_id}`;
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

/** snake_case identifiers → human labels (vendor_assessment → Vendor Assessment). */
function prettyName(s) {
  return String(s || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function setStatus(status) {
  const el = $("#job-status");
  el.textContent = status;
  el.className = `status ${status}`;
  // A paused job keeps a persistent "Review required" affordance so the
  // modal can be dismissed and reopened without losing the exit path.
  $("#review-btn").classList.toggle(
    "hidden", status !== "awaiting_review" || hitlOpen
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
      openHitl(ev.reason);
      refreshJobs();
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

async function attachJob(jobId) {
  closeStream();
  currentJob = jobId;
  hitlOpen = false;
  $("#empty").classList.add("hidden");
  $("#job-view").classList.remove("hidden");
  $("#feed").innerHTML = "";
  $("#report-card").classList.add("hidden");
  $("#job-id").textContent = jobId;
  $("#pdf-link").classList.add("hidden");
  $("#hitl-modal").classList.add("hidden");
  setStatus("queued");

  const job = await (await fetch(`${API}/orchestration/reports/${jobId}`)).json();
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
  if (job.result?.report) renderReport(job.result);
  if (job.status === "awaiting_review") openHitl(job.result?.escalation_reason);

  if (!["done", "failed", "blocked"].includes(job.status)) {
    eventSource = new EventSource(`${API}/orchestration/reports/${jobId}/stream`);
    eventSource.onmessage = (e) => handleEvent(JSON.parse(e.data));
    eventSource.onerror = () => { /* EventSource auto-reconnects */ };
  }
}

function closeStream() {
  if (eventSource) { eventSource.close(); eventSource = null; }
}

async function finalize() {
  if (!currentJob) return;
  const job = await (await fetch(`${API}/orchestration/reports/${currentJob}`)).json();
  setStatus(job.status);
  $("#job-cost").textContent = `$${(job.cost_usd || 0).toFixed(4)}`;
  if (job.status === "awaiting_review") {
    openHitl(job.result?.escalation_reason);
    return;
  }
  closeHitl();
  if (job.result?.report) renderReport(job.result);
  refreshDashboard();
  refreshJobs();
}

async function renderReport(result) {
  const card = $("#report-card");
  card.classList.remove("hidden");
  // The audit seal is appended to the markdown for the PDF; the badge
  // already surfaces it in the UI — strip the tail block for display.
  const reportMd = String(result.report).replace(
    /\n?-{3,}\s*\n_Audit root:[\s\S]*$/m, ""
  );
  $("#report").innerHTML = md(reportMd);
  const pdfUrl = `${API}/orchestration/reports/${currentJob}/report.pdf`;
  $("#pdf-link").href = pdfUrl;
  $("#pdf-link").classList.remove("hidden");
  $("#report-pdf").href = pdfUrl;
  $("#report-pdf").classList.remove("hidden");
  const meta = document.createElement("p");
  meta.style.cssText = "color:var(--muted);font-size:12px;margin-top:16px";
  meta.textContent =
    `${result.debate_rounds} debate round(s) · ${result.duration_seconds}s`;
  $("#report").appendChild(meta);
  // Tamper-evident audit trail: re-verified server-side on demand.
  try {
    const a = await (await fetch(
      `${API}/orchestration/reports/${currentJob}/audit`)).json();
    const badge = $("#audit-badge");
    badge.textContent = a.valid
      ? `audit ✓ ${String(a.audit_root).slice(0, 12)}…`
      : "audit ✗ INVALID";
    badge.style.color = a.valid ? "var(--ok)" : "var(--danger)";
    badge.classList.remove("hidden");
  } catch { /* audit is best-effort */ }
}

/* ── HITL modal ─────────────────────────────────────────────────────── */

const ESCALATION_REASONS = {
  budget:
    "The LLM budget cap was reached — approve the current draft, " +
    "or add budget to keep the team working.",
  debate_rounds:
    "The Writer↔Reviewer debate used all its rounds without an approved " +
    "draft — approve the latest draft, request edits, or reject it.",
};

let lastEscalationReason = null;

function openHitl(reason) {
  lastEscalationReason = reason || lastEscalationReason;
  $("#hitl-reason").textContent =
    ESCALATION_REASONS[lastEscalationReason] || ESCALATION_REASONS.debate_rounds;
  // "fund" only makes sense when the escalation was a spent budget.
  const isBudget = lastEscalationReason === "budget";
  $("#hitl-fund-field").classList.toggle("hidden", !isBudget);
  $('#hitl-modal [data-action="fund"]').classList.toggle("hidden", !isBudget);
  // A second escalation reopens the modal — buttons stay disabled from the
  // previous in-flight decision unless reset here.
  hitlButtons.forEach((b) => { b.disabled = false; });
  if (hitlOpen) return;
  hitlOpen = true;
  $("#review-btn").classList.add("hidden");
  $("#hitl-modal").classList.remove("hidden");
}

function closeHitl() {
  hitlOpen = false;
  $("#hitl-modal").classList.add("hidden");
  if ($("#job-status").textContent === "awaiting_review") {
    $("#review-btn").classList.remove("hidden");
  }
}

$("#hitl-dismiss").addEventListener("click", closeHitl);
$("#review-btn").addEventListener("click", () => openHitl(lastEscalationReason));
$("#hitl-modal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeHitl();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && hitlOpen) closeHitl();
});

const hitlButtons = document.querySelectorAll("#hitl-modal [data-action]");
hitlButtons.forEach((btn) => {
  btn.addEventListener("click", async () => {
    const action = btn.dataset.action;
    const hitlError = $("#hitl-error");
    const showError = (msg) => {
      hitlError.textContent = msg;
      hitlError.classList.remove("hidden");
    };
    hitlError.classList.add("hidden");
    if (!currentJob) {
      showError("No job selected — open a paused job first.");
      return;
    }
    const feedback = $("#hitl-feedback").value.trim();
    if (action === "edit" && !feedback) {
      showError("Feedback is required to request edits.");
      return;
    }
    const fund = parseFloat($("#hitl-fund").value);
    if (action === "fund" && (Number.isNaN(fund) || fund <= 0)) {
      showError("Enter an amount to fund.");
      return;
    }
    hitlButtons.forEach((b) => { b.disabled = true; });
    try {
      const res = await fetch(`${API}/orchestration/reports/${currentJob}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action, feedback,
          additional_budget_usd: Number.isNaN(fund) ? 0 : fund,
        }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => null);
        throw new Error(detail?.detail || `HTTP ${res.status}`);
      }
      closeHitl();
      toast(`Decision sent: ${action}`);
      setStatus("running");
      if (!eventSource) {
        eventSource = new EventSource(`${API}/orchestration/reports/${currentJob}/stream`);
        eventSource.onmessage = (e) => handleEvent(JSON.parse(e.data));
      }
    } catch (ex) {
      showError(`Review failed: ${ex.message}. Is the server running?`);
      hitlButtons.forEach((b) => { b.disabled = false; });
    }
  });
});

/* ── form ───────────────────────────────────────────────────────────── */

$("#job-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const btn = $("#submit-btn");
  btn.disabled = true;
  try {
    const res = await fetch(`${API}/orchestration/reports`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        topic: f.topic.value.trim(),
        audience: f.audience.value.trim() || "general business",
        tone: f.tone.value,
        report_type: f.report_type.value,
        tenant_context: f.tenant_context.value.trim(),
        requirements: f.requirements.value.split("\n").map(s => s.trim()).filter(Boolean),
        ...(isNaN(parseFloat(f.budget_usd.value))
          ? {} : { budget_usd: parseFloat(f.budget_usd.value) }),
      }),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const job = await res.json();
    history.replaceState(null, "", `?job=${encodeURIComponent(job.job_id)}`);
    attachJob(job.job_id);
    refreshJobs();
  } catch (ex) {
    toast(`Submission failed: ${ex.message}`, true);
  } finally {
    btn.disabled = false;
  }
});

/* ── boot ───────────────────────────────────────────────────────────── */

fetch("/health/ready")
  .then((r) => $("#health-dot").classList.add(r.ok ? "ok" : "bad"))
  .catch(() => $("#health-dot").classList.add("bad"));

refreshDashboard();
refreshJobs();

const initialJob = new URLSearchParams(location.search).get("job");
if (initialJob) attachJob(initialJob);
