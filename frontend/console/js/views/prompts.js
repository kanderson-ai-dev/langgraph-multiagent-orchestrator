/* Prompt Registry tab — versions with status, diff viewer, candidate
 * editor prefilled from the promoted template, A/B runs, gated promote
 * (with override reason) and rollback. All mutations are admin-gated
 * server-side; the UI surfaces the real API errors. */

import { api, apiPost } from "../api.js";
import { $, escapeHtml, toast } from "../util.js";

let registry = {}; // name → versions[]
let current = { name: null, version: null };

export function initPromptsView() {
  $("#prompts-refresh").addEventListener("click", loadRegistry);
  $("#prompt-names").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-name]");
    if (btn) selectPrompt(btn.dataset.name);
  });
  $("#diff-run").addEventListener("click", runDiff);
  $("#candidate-submit").addEventListener("click", submitCandidate);
  $("#ab-run").addEventListener("click", runAb);
  $("#promote-run").addEventListener("click", () => runPromote(false));
  $("#promote-override-run").addEventListener("click", () => runPromote(true));
  $("#rollback-run").addEventListener("click", runRollback);
}

export async function loadRegistry() {
  try {
    registry = await api("/prompts");
    const box = $("#prompt-names");
    box.innerHTML = "";
    for (const name of Object.keys(registry).sort()) {
      const versions = registry[name];
      const promoted = versions.find((v) => v.status === "promoted");
      const el = document.createElement("button");
      el.type = "button";
      el.className = "prompt-name" + (name === current.name ? " active" : "");
      el.dataset.name = name;
      el.innerHTML =
        `<span>${escapeHtml(name)}</span>` +
        `<span class="prompt-name-meta">${versions.length}v` +
        (promoted ? ` · v${promoted.version}` : "") + `</span>`;
      box.appendChild(el);
    }
    if (!current.name && Object.keys(registry).length) {
      selectPrompt(Object.keys(registry).sort()[0]);
    } else if (current.name) {
      // Keep the selected prompt's version table in sync after mutations
      // (candidate registered, promote, rollback).
      renderVersions();
      const promoted = registry[current.name]?.find(
        (v) => v.status === "promoted");
      $("#promote-status").textContent =
        promoted ? `promoted: v${promoted.version}` : "no promoted";
    }
  } catch (ex) {
    toast(`Registry load failed: ${ex.message}`, true);
  }
}

function selectPrompt(name) {
  current.name = name;
  document.querySelectorAll(".prompt-name").forEach((b) =>
    b.classList.toggle("active", b.dataset.name === name));
  renderVersions();
  const promoted = registry[name]?.find((v) => v.status === "promoted");
  if (promoted) {
    $("#candidate-template").value = promoted.template || "";
    $("#promote-status").textContent = `promoted: v${promoted.version}`;
  }
  $("#diff-result").innerHTML = "";
  $("#ab-result").innerHTML = "";
  $("#promote-result").innerHTML = "";
}

function renderVersions() {
  const tbody = $("#versions-body");
  tbody.innerHTML = "";
  for (const v of registry[current.name] || []) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="font-mono">v${v.version}</td>` +
      `<td><span class="status sm pv-${v.status}">${escapeHtml(v.status)}</span></td>` +
      `<td>${escapeHtml(v.changelog || "—")}</td>` +
      `<td class="font-mono" title="${escapeHtml(v.sha256)}">${escapeHtml(String(v.sha256).slice(0, 10))}…</td>`;
    tr.addEventListener("click", () => {
      current.version = v.version;
      $("#candidate-template").value = v.template || "";
      tbody.querySelectorAll("tr").forEach((r) => r.classList.remove("active"));
      tr.classList.add("active");
    });
    tbody.appendChild(tr);
  }
}

async function runDiff() {
  const from = parseInt($("#diff-from").value, 10);
  const to = parseInt($("#diff-to").value, 10);
  const out = $("#diff-result");
  if (!current.name || !from || !to) {
    out.innerHTML = '<p class="modal-error">Pick a prompt and two versions.</p>';
    return;
  }
  try {
    const d = await api(
      `/prompts/${encodeURIComponent(current.name)}/diff?from=${from}&to=${to}`);
    out.innerHTML = `<pre class="diff-view">${escapeHtml(d.diff)}</pre>`;
  } catch (ex) {
    out.innerHTML = `<p class="modal-error">${escapeHtml(ex.message)}</p>`;
  }
}

async function submitCandidate() {
  const template = $("#candidate-template").value.trim();
  const changelog = $("#candidate-changelog").value.trim();
  if (!current.name || !template) {
    toast("Select a prompt and write a template", true);
    return;
  }
  try {
    const pv = await apiPost(
      `/prompts/${encodeURIComponent(current.name)}/candidates`,
      { template, changelog });
    toast(`Candidate v${pv.version} registered`);
    loadRegistry();
  } catch (ex) {
    toast(`Candidate rejected: ${ex.message}`, true);
  }
}

async function runAb() {
  const version = parseInt($("#ab-version").value, 10) || undefined;
  const out = $("#ab-result");
  out.innerHTML = "<p>Running offline A/B…</p>";
  try {
    const res = await apiPost(
      `/prompts/${encodeURIComponent(current.name)}/ab-test`,
      { version });
    if (res.status === "running") {
      out.innerHTML = `<p>Live run started — run_id <code>${escapeHtml(res.run_id)}</code></p>`;
      return;
    }
    out.innerHTML = `<pre class="diff-view">${escapeHtml(JSON.stringify(res, null, 2))}</pre>`;
  } catch (ex) {
    out.innerHTML = `<p class="modal-error">${escapeHtml(ex.message)}</p>`;
  }
}

async function runPromote(override) {
  const version = parseInt($("#promote-version").value, 10) || undefined;
  const out = $("#promote-result");
  const body = { version };
  if (override) {
    const reason = $("#override-reason").value.trim();
    if (reason.length < 20) {
      out.innerHTML =
        '<p class="modal-error">Override needs a reason ≥ 20 chars.</p>';
      return;
    }
    body.override_reason = reason;
  }
  try {
    const pv = await apiPost(
      `/prompts/${encodeURIComponent(current.name)}/promote`, body);
    out.innerHTML = `<p class="ok-text">Promoted to v${pv.version}</p>`;
    $("#override-box").classList.add("hidden");
    loadRegistry();
  } catch (ex) {
    if (ex.status === 409 && ex.payload?.failures) {
      out.innerHTML =
        `<p class="modal-error">Blocked by metric gate:</p>` +
        `<pre class="diff-view">${escapeHtml(JSON.stringify(ex.payload.failures, null, 2))}</pre>`;
      $("#override-box").classList.remove("hidden");
    } else {
      out.innerHTML = `<p class="modal-error">${escapeHtml(ex.message)}</p>`;
    }
  }
}

async function runRollback() {
  const to = parseInt($("#rollback-version").value, 10);
  const reason = $("#rollback-reason").value.trim();
  const out = $("#promote-result");
  if (!to || reason.length < 5) {
    out.innerHTML =
      '<p class="modal-error">Rollback needs a version and a reason ≥ 5 chars.</p>';
    return;
  }
  try {
    const pv = await apiPost(
      `/prompts/${encodeURIComponent(current.name)}/rollback`,
      { to_version: to, reason });
    out.innerHTML = `<p class="ok-text">Rolled back to v${pv.version}</p>`;
    loadRegistry();
  } catch (ex) {
    out.innerHTML = `<p class="modal-error">${escapeHtml(ex.message)}</p>`;
  }
}
