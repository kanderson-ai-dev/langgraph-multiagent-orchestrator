/* Tiny shared state — the console is single-page; modules coordinate
 * through this object instead of globals. */

export const state = {
  currentJob: null,
  hitlOpen: false,
  activeTab: "jobs",
};

/* Simple pub/sub for cross-view notifications (e.g. job selected). */
const listeners = new Set();
export function onJobChange(fn) { listeners.add(fn); }
export function emitJobChange(jobId) {
  state.currentJob = jobId;
  for (const fn of listeners) fn(jobId);
}
