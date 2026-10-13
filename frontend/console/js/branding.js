/* White-label branding — fetches /config/ui (public, pre-login) and
 * applies product name, accent colour and optional logo to the console.
 * Silent no-op on any failure: the default theme always works. */

export async function applyBranding() {
  let cfg;
  try {
    const r = await fetch("/api/v1/config/ui");
    if (!r.ok) return;
    cfg = await r.json();
  } catch {
    return;
  }
  if (cfg.product_name) {
    document.title = `Console — ${cfg.product_name}`;
    const el = document.querySelector(".brand-name");
    if (el) el.textContent = cfg.product_name.toLowerCase();
  }
  if (cfg.accent_color) {
    document.documentElement.style.setProperty("--accent", cfg.accent_color);
  }
  if (cfg.logo_url) {
    const mark = document.querySelector(".brand-mark");
    if (mark) {
      mark.style.background = "none";
      mark.innerHTML =
        `<img src="${encodeURI(cfg.logo_url)}" alt="" ` +
        'style="width:100%;height:100%;object-fit:contain" />';
    }
  }
}
