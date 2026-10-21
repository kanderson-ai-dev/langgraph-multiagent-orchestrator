/* Auth — only needed when the API has auth enabled (JWT_SECRET_KEY +
 * ADMIN_PASSWORD_HASH configured). Sign in with admin credentials to get
 * a short-lived JWT, or paste an `orx_…` workspace API key directly.
 * The token lives in sessionStorage (cleared when the tab closes — no
 * persistent-storage opt-in); api.js and sse.js attach it as a Bearer
 * header. Auth-disabled local dev never sees this modal — nothing
 * returns 401 there. */

import { $, toast } from "./util.js";
import { getToken, setToken } from "./api.js";

export function openAuthModal() {
  $("#auth-modal").classList.remove("hidden");
}

function closeAuthModal() {
  $("#auth-modal").classList.add("hidden");
  $("#auth-error").classList.add("hidden");
}

export function initAuth() {
  const btn = $("#auth-btn");
  const refresh = () => {
    btn.textContent = getToken() ? "Sign out" : "Sign in";
  };
  refresh();

  btn.addEventListener("click", async () => {
    if (getToken()) {
      // Revoke the JWT server-side before dropping it locally.
      await fetch("/api/v1/auth/logout", {
        method: "POST",
        headers: { Authorization: `Bearer ${getToken()}` },
      }).catch(() => {});
      setToken("");
      toast("Signed out");
      location.reload();
      return;
    }
    openAuthModal();
  });
  $("#auth-dismiss").addEventListener("click", closeAuthModal);

  $("#auth-login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#auth-error");
    err.classList.add("hidden");

    const key = $("#auth-apikey").value.trim();
    const user = $("#auth-user").value.trim();
    const pass = $("#auth-pass").value;
    if (key) {
      setToken(key);
      location.reload();
      return;
    }
    if (!user || !pass) {
      err.textContent = "Enter username + password or paste an API key.";
      err.classList.remove("hidden");
      return;
    }
    try {
      const res = await fetch("/api/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: user,
          password: pass,
          workspace_id: $("#auth-ws").value.trim() || "default",
        }),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) throw new Error(body?.detail || `HTTP ${res.status}`);
      setToken(body.access_token);
      location.reload();
    } catch (ex) {
      err.textContent = `Sign-in failed: ${ex.message}`;
      err.classList.remove("hidden");
    }
  });
}
