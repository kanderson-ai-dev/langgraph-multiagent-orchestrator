/* Console entry point — ES-module bootstrap (no bundler).
 * Wires the tab bar, initialises each view module and boots the jobs
 * view. Auth-free in local dev (the API scopes everything to the
 * `default` workspace). */

import { $ } from "./js/util.js";
import { state } from "./js/state.js";
import {
  attachJob, initJobsView, onResumed, refreshDashboard, refreshJobs,
} from "./js/views/jobs.js";
import { initReview } from "./js/views/review.js";
import { initPromptsView, loadRegistry } from "./js/views/prompts.js";
import { initPerformanceView, loadPerformance } from "./js/views/performance.js";
import { initAuditView, loadJobOptions } from "./js/views/audit.js";
import { initReportView } from "./js/views/report.js";
import { applyBranding } from "./js/branding.js";

/* ── tab bar ────────────────────────────────────────────────────────── */

const TABS = {
  jobs: { onShow: refreshJobs },
  prompts: { onShow: loadRegistry },
  performance: { onShow: loadPerformance },
  audit: { onShow: loadJobOptions },
};

function selectTab(name) {
  state.activeTab = name;
  document.querySelectorAll("[role=tab]").forEach((t) => {
    const active = t.dataset.tab === name;
    t.setAttribute("aria-selected", String(active));
    t.classList.toggle("active", active);
  });
  document.querySelectorAll("[role=tabpanel]").forEach((p) => {
    p.classList.toggle("hidden", p.id !== `tab-${name}`);
  });
  TABS[name]?.onShow?.();
}

document.querySelectorAll("[role=tab]").forEach((t) => {
  t.addEventListener("click", () => selectTab(t.dataset.tab));
});

/* ── boot ───────────────────────────────────────────────────────────── */

initJobsView();
initReview({ onResumed });
initPromptsView();
initPerformanceView();
initAuditView();
initReportView();
applyBranding();

fetch("/health/ready")
  .then((r) => $("#health-dot").classList.add(r.ok ? "ok" : "bad"))
  .catch(() => $("#health-dot").classList.add("bad"));

refreshDashboard();
refreshJobs();

const initialJob = new URLSearchParams(location.search).get("job");
if (initialJob) attachJob(initialJob);
