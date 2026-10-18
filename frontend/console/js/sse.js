/* SSE reader — fetch + ReadableStream instead of EventSource so the
 * Authorization header rides along (EventSource cannot set headers).
 * Parses `data:` frames exactly like EventSource; no auto-reconnect —
 * the jobs view re-opens the stream when needed. Returns a handle so the
 * caller owns lifecycle. */

import { authHeaders } from "./api.js";

export function openStream(jobId, onEvent) {
  const ctrl = new AbortController();
  // Bounded re-attach on an unexpected end (server restart, the stream's
  // lifetime-cap `timeout` frame, a proxy drop). The counter resets whenever
  // a real event flows — a healthy stream — so only consecutive silent
  // failures count against the cap. Terminal job events close the stream
  // from the caller side, so a reconnect only replays history and ends.
  let attempts = 0;
  const run = async () => {
    try {
      const res = await fetch(
        `/api/v1/orchestration/reports/${jobId}/stream`,
        { headers: authHeaders(), signal: ctrl.signal },
      );
      if (!res.ok || !res.body) return;
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const frames = buf.split("\n\n");
        buf = frames.pop(); // trailing partial frame stays buffered
        for (const frame of frames) {
          const data = frame
            .split("\n")
            .filter((l) => l.startsWith("data:"))
            .map((l) => l.slice(5).trimStart())
            .join("\n");
          if (!data) continue; // `: ping` heartbeats carry no data:
          attempts = 0; // a real event just flowed — the stream is healthy
          try {
            onEvent(JSON.parse(data));
          } catch {
            /* malformed event — ignore */
          }
        }
      }
    } catch {
      /* aborted or network error — stream ends */
    }
  };
  (async () => {
    // Re-attach while the end was unexpected (not a caller abort) and the
    // failure streak is under the cap — covers repeated lifetime-cap
    // timeouts on long-parked jobs without looping forever on a dead server.
    while (!ctrl.signal.aborted) {
      await run();
      if (ctrl.signal.aborted || attempts >= 5) return;
      attempts += 1;
      await new Promise((r) => setTimeout(r, 500));
    }
  })();
  return { close: () => ctrl.abort() };
}
