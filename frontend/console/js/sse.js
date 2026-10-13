/* SSE wrapper — one EventSource per job, explicit close, auto-reconnect
 * left to the browser. Returns a handle so the caller owns lifecycle. */

export function openStream(jobId, onEvent) {
  const src = new EventSource(`/api/v1/orchestration/reports/${jobId}/stream`);
  src.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data));
    } catch {
      /* malformed event — ignore */
    }
  };
  src.onerror = () => { /* EventSource auto-reconnects */ };
  return { source: src, close: () => src.close() };
}
