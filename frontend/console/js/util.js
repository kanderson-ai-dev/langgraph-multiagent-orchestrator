/* Shared utilities — HTML escaping, the minimal safe markdown renderer,
 * toasts and label formatting. No DOM state lives here. */

export const $ = (sel) => document.querySelector(sel);

export function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
}

/** snake_case identifiers → human labels (vendor_assessment → Vendor Assessment). */
export function prettyName(s) {
  return String(s || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function md(src) {
  const esc = escapeHtml(src);
  const lines = esc.split(/\r?\n/);
  const out = [];
  let inList = false, inUl = false, para = [];

  const inline = (t) => t
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[\^(\d+)\^\]/g, "<sup>$1</sup>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    // bare URLs (claim — https://…) become real links; quoted/href'd URLs
    // are skipped because they are never preceded by space/>/—
    .replace(/(^|[\s>—])(https?:\/\/[^\s<)&"']+)/g,
             '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');

  const flushPara = () => {
    if (para.length) { out.push(`<p>${para.map(inline).join(" ")}</p>`); para = []; }
  };
  const flushList = () => {
    if (inList) { out.push(inUl ? "</ul>" : "</ol>"); inList = false; }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)/))) {
      flushPara(); flushList();
      const lvl = Math.min(m[1].length + 1, 4);
      out.push(`<h${lvl}>${inline(m[2])}</h${lvl}>`);
    } else if (/^(---+|\*\*\*+)$/.test(line.trim())) {
      flushPara(); flushList(); out.push("<hr>");
    } else if ((m = line.match(/^[-*]\s+(.*)/))) {
      flushPara();
      if (!inList || !inUl) { flushList(); out.push("<ul>"); inList = true; inUl = true; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^\[\^(\d+)\^\]:\s*(.*)/))) {
      flushPara(); flushList();
      out.push(`<p class="footnote"><sup>${m[1]}</sup> ${inline(m[2])}</p>`);
    } else if ((m = line.match(/^\d+\.\s+(.*)/))) {
      flushPara();
      if (!inList || inUl) { flushList(); out.push("<ol>"); inList = true; inUl = false; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if (line.startsWith("> ")) {
      flushPara(); flushList();
      out.push(`<blockquote>${inline(line.slice(2))}</blockquote>`);
    } else if (!line.trim()) {
      flushPara(); flushList();
    } else {
      para.push(line.trim());
    }
  }
  flushPara(); flushList();
  return out.join("\n");
}

/* ── toast ──────────────────────────────────────────────────────────── */

let toastTimer = null;
export function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4000);
}
