/* Thin API client — one place for the /api/v1 prefix, JSON handling and
 * FastAPI error-detail extraction. Auth-free in local dev; when the API
 * has auth enabled the console sends a Bearer credential (JWT from
 * /auth/login or an `orx_…` workspace API key) stored by js/auth.js. */

const API = "/api/v1";
const TOKEN_KEY = "orx_token";

export const getToken = () => localStorage.getItem(TOKEN_KEY) || "";

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export function authHeaders() {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

export async function api(path, opts = {}) {
  const res = await fetch(`${API}${path}`, {
    ...opts,
    headers: { ...authHeaders(), ...(opts.headers || {}) },
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail =
      typeof body?.detail === "string"
        ? body.detail
        : body?.detail?.message || `HTTP ${res.status}`;
    const err = new ApiError(res.status, detail);
    err.payload = body?.detail && typeof body.detail === "object" ? body.detail : null;
    if (res.status === 401) {
      window.dispatchEvent(new CustomEvent("orx:unauthorized"));
    }
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
