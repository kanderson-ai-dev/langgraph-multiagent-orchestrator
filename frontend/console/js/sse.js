/* SSE reader — fetch + ReadableStream instead of EventSource so the
 * Authorization header rides along (EventSource cannot set headers).
 * Parses `data:` frames exactly like EventSource; no auto-reconnect —
 * the jobs view re-opens the stream when needed. Returns a handle so the
 * caller owns lifecycle. */

import { authHeaders } from "./api.js";

export function openStream(jobId, onEvent) {
  const ctrl = new AbortController();
  (async () => {
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
          if (!data) continue;
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
  })();
  return { close: () => ctrl.abort() };
}
