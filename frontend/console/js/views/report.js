/* Final report view — markdown render, prompt-version chips, audit seal
 * badge and the sealed evidence manifest (per-source content hashes the
 * citation footnotes reference as `#<sha12>`). */

import { api, apiPost } from "../api.js";
import { $, escapeHtml, md, toast } from "../util.js";
import { state } from "../state.js";

export async function renderReport(result) {
  const card = $("#report-card");
  card.classList.remove("hidden");
  // The audit seal is appended to the markdown for the PDF; the badge
  // already surfaces it in the UI — strip the tail block for display.
  const reportMd = String(result.report).replace(
    /\n?-{3,}\s*\n_Audit root:[\s\S]*$/m, ""
  );
  $("#report").innerHTML = md(reportMd);
  const base = `/api/v1/orchestration/reports/${state.currentJob}`;
  $("#pdf-link").href = `${base}/report.pdf`;
  $("#pdf-link").classList.remove("hidden");
  for (const ext of ["pdf", "docx", "md"]) {
    const link = $(`#report-${ext}`);
    link.href = `${base}/report.${ext}`;
    link.classList.remove("hidden");
  }
  $("#fb-status").textContent = "";
  $("#fb-comment").value = "";
  document.querySelectorAll(".fb-btn").forEach((b) => {
    b.classList.remove("active");
    b.disabled = false;
  });

  renderPromptVersions(result.prompt_versions);
  const meta = document.createElement("p");
  meta.style.cssText = "color:var(--muted);font-size:12px;margin-top:16px";
  meta.textContent =
    `${result.debate_rounds} debate round(s) · ${result.duration_seconds}s`;
  $("#report").appendChild(meta);
  renderEvidence(result.evidence_manifest);

  // Tamper-evident audit trail: re-verified server-side on demand.
  try {
    const a = await api(`/orchestration/reports/${state.currentJob}/audit`);
    const badge = $("#audit-badge");
    badge.textContent = a.valid
      ? `audit ✓ ${String(a.audit_root).slice(0, 12)}…`
      : "audit ✗ INVALID";
    badge.style.color = a.valid ? "var(--ok)" : "var(--danger)";
    badge.classList.remove("hidden");
  } catch { /* audit is best-effort */ }
}

/** Wire the thumbs widget — one rating per click, comment optional. */
export function initReportView() {
  document.querySelectorAll(".fb-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!state.currentJob) return;
      btn.disabled = true;
      try {
        await apiPost("/feedback", {
          job_id: state.currentJob,
          rating: btn.dataset.rating,
          comment: $("#fb-comment").value.trim(),
        });
        btn.classList.add("active");
        $("#fb-status").textContent = "thanks — feedback queued";
      } catch (ex) {
        btn.disabled = false;
        $("#fb-status").textContent = `feedback failed: ${ex.message}`;
        toast("Feedback failed", true);
      }
    });
  });
}

function renderPromptVersions(versions) {
  const box = $("#prompt-versions");
  box.innerHTML = "";
  const entries = Object.entries(versions || {});
  box.classList.toggle("hidden", entries.length === 0);
  for (const [name, ver] of entries) {
    const chip = document.createElement("span");
    chip.className = "chip chip-mono chip-prompt";
    chip.textContent = `${name}@v${ver}`;
    chip.title = `prompt ${name} version ${ver}`;
    box.appendChild(chip);
  }
}

function renderEvidence(manifest) {
  const box = $("#evidence-strip");
  const items = manifest?.items;
  box.innerHTML = "";
  box.classList.toggle("hidden", !Array.isArray(items) || !items.length);
  if (!Array.isArray(items) || !items.length) return;
  for (const s of items) {
    const el = document.createElement("a");
    el.className = "chip chip-mono chip-evidence";
    el.textContent = `#${String(s.sha256 || "").slice(0, 12)}`;
    el.title = `${s.title || s.content_type} — ${s.url || "evidence"}`;
    if (s.url) {
      el.href = s.url;
      el.target = "_blank";
      el.rel = "noopener noreferrer";
    }
    box.appendChild(el);
  }
}
