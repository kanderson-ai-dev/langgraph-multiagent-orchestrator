/* HITL review modal — the human decision surface.

The server publishes ``allowed_actions`` per pause (e.g. a budget
escalation with no draft offers "fund", not "approve"); the modal
honours that guided set instead of offering dead ends. */

import { apiPost } from "../api.js";
import { openStream } from "../sse.js";
import { $, escapeHtml, toast } from "../util.js";
import { state } from "../state.js";

const ESCALATION_REASONS = {
  budget:
    "The LLM budget cap was reached — add budget to keep the team " +
    "working, or reject the run.",
  budget_exceeded:
    "The LLM budget cap was reached — add budget to keep the team " +
    "working, or reject the run.",
  debate_rounds:
    "The Writer↔Reviewer debate used all its rounds without an approved " +
    "draft — approve the latest draft, request edits, or reject it.",
};

const ACTION_LABELS = {
  approve: "Approve report",
  fund: "Fund & continue",
  fund_and_continue: "Fund & continue",
  edit: "Request edits",
  reject: "Reject",
};

let lastEscalationReason = null;
let lastAllowedActions = null;
let resumeStream = null;

/** Wire the modal; ``onResumed`` reattaches the job SSE after a decision. */
export function initReview({ onResumed }) {
  $("#hitl-dismiss").addEventListener("click", closeHitl);
  $("#review-btn").addEventListener("click", () => openHitl());
  $("#hitl-modal").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) closeHitl();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.hitlOpen) closeHitl();
  });

  document.querySelectorAll("#hitl-modal [data-action]").forEach((btn) => {
    btn.addEventListener("click", () => submitDecision(btn, onResumed));
  });
}

export function openHitl(reason, allowedActions) {
  lastEscalationReason = reason || lastEscalationReason;
  lastAllowedActions = allowedActions || lastAllowedActions;
  $("#hitl-reason").textContent =
    ESCALATION_REASONS[lastEscalationReason] || ESCALATION_REASONS.debate_rounds;

  // Guided set: only actions the server advertises are clickable.
  const guided = lastAllowedActions;
  document.querySelectorAll("#hitl-modal [data-action]").forEach((btn) => {
    const action = btn.dataset.action;
    const allowed =
      !guided || guided.includes(action) ||
      (action === "fund" && guided.includes("fund_and_continue"));
    btn.classList.toggle("hidden", !allowed);
  });
  const isBudget =
    lastEscalationReason === "budget" ||
    lastEscalationReason === "budget_exceeded";
  $("#hitl-fund-field").classList.toggle("hidden", !isBudget);

  setHitlButtonsDisabled(false);
  if (state.hitlOpen) return;
  state.hitlOpen = true;
  $("#review-btn").classList.add("hidden");
  $("#hitl-modal").classList.remove("hidden");
}

export function closeHitl() {
  state.hitlOpen = false;
  $("#hitl-modal").classList.add("hidden");
  if ($("#job-status").classList.contains("awaiting_review")) {
    $("#review-btn").classList.remove("hidden");
  }
}

/** Drop the retained pause state when switching jobs — a previous job's
 *  reason/guided set must never leak into another job's modal. */
export function resetHitlState() {
  lastEscalationReason = null;
  lastAllowedActions = null;
  state.hitlOpen = false;
  $("#hitl-modal").classList.add("hidden");
}

function setHitlButtonsDisabled(disabled) {
  document.querySelectorAll("#hitl-modal [data-action]").forEach((b) => {
    b.disabled = disabled;
  });
}

async function submitDecision(btn, onResumed) {
  const action = btn.dataset.action;
  const hitlError = $("#hitl-error");
  const showError = (msg) => {
    hitlError.textContent = msg;
    hitlError.classList.remove("hidden");
  };
  hitlError.classList.add("hidden");
  if (!state.currentJob) {
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
  setHitlButtonsDisabled(true);
  try {
    await apiPost(`/orchestration/reports/${state.currentJob}/review`, {
      action: action === "fund" ? "fund" : action,
      feedback,
      additional_budget_usd: Number.isNaN(fund) ? 0 : fund,
    });
    closeHitl();
    toast(`Decision sent: ${action}`);
    onResumed(state.currentJob);
  } catch (ex) {
    const guided =
      ex.payload?.allowed_actions || ex.detail?.allowed_actions;
    if (guided) {
      showError(`${ex.message} — allowed: ${escapeHtml(guided.join(", "))}`);
    } else {
      showError(`Review failed: ${ex.message}. Is the server running?`);
    }
    setHitlButtonsDisabled(false);
  }
}
