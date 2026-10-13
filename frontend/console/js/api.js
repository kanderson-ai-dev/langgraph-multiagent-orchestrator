/* Thin API client — one place for the /api/v1 prefix, JSON handling and
 * FastAPI error-detail extraction. Auth-free in local dev (the API
 * scopes everything to the `default` workspace). */

const API = "/api/v1";

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

export async function api(path, opts = {}) {
  const res = await fetch(`${API}${path}`, opts);
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail =
      typeof body?.detail === "string"
        ? body.detail
        : body?.detail?.message || `HTTP ${res.status}`;
    const err = new ApiError(res.status, detail);
    err.payload = body?.detail && typeof body.detail === "object" ? body.detail : null;
    throw err;
  }
  return body;
}

export const apiPost = (path, json) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(json),
  });

export const apiDelete = (path) => api(path, { method: "DELETE" });
