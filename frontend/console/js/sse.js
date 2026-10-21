/* SSE reader — fetch + ReadableStream instead of EventSource so the
 * Authorization header rides along (EventSource cannot set headers).
 * Parses `data:`/`id:` frames exactly like EventSource; re-attaches send
 * `Last-Event-ID` so the server replays only what was missed and frames
 * whose id is not newer than the last delivered are dropped (dedup).
 * Returns a handle so the caller owns lifecycle. */

import { authHeaders } from "./api.js";

export function openStream(jobId, onEvent) {
  const ctrl = new AbortController();
  // Bounded re-attach on an unexpected end (server restart, the stream's
  // lifetime-cap `timeout` frame, a proxy drop). The counter resets whenever
  // a real event flows — a healthy stream — so only consecutive silent
  // failures count against the cap. Terminal job events close the stream
  // from the caller side, so a reconnect only replays history and ends.
  let attempts = 0;
  // Last `id:` seen across all attaches — re-attach sends it as
  // Last-Event-ID and the server replays only events with a higher seq.
  let lastId = null;
  const run = async () => {
    try {
      const headers = { ...authHeaders() };
      if (lastId !== null) headers["Last-Event-ID"] = String(lastId);
      const res = await fetch(
        `/api/v1/orchestration/reports/${jobId}/stream`,
        { headers, signal: ctrl.signal },
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
          const lines = frame.split("\n");
          const idLine = lines.find((l) => l.startsWith("id:"));
          const frameId = idLine ? idLine.slice(3).trim() : null;
          const data = lines
            .filter((l) => l.startsWith("data:"))
            .map((l) => l.slice(5).trimStart())
            .join("\n");
          if (!data) continue; // `: ping` heartbeats carry no data:
          if (frameId !== null) {
            const n = Number(frameId);
            // Only an integer id can dedup — a non-integer still delivers.
            if (Number.isInteger(n)) {
              if (lastId !== null && n <= lastId) continue; // replay dup
              lastId = n;
            }
          }
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
