/* Shared helpers: fetch wrapper, toasts, DOM utilities, theme. */

const API = {
  async request(url, options = {}) {
    const res = await fetch(url, options);
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body && body.detail) {
          detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
        }
      } catch (_) {
        /* non-JSON error body: keep the status line */
      }
      throw new Error(detail);
    }
    if (res.status === 204) return null;
    const type = res.headers.get("content-type") || "";
    if (type.includes("application/json")) return res.json();
    return res;
  },

  get(url) {
    return API.request(url);
  },

  post(url, body) {
    return API.request(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
  },

  patch(url, body) {
    return API.request(url, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
  },

  del(url) {
    return API.request(url, { method: "DELETE" });
  },

  form(url, formData, method = "POST") {
    return API.request(url, { method, body: formData });
  },

  async blob(url, options = {}) {
    const res = await API.request(url, options);
    return res instanceof Response ? res.blob() : res;
  },
};

/* --- Toasts -------------------------------------------------------------- */

const ICONS = {
  success:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
  error:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
  info:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
};

const COLORS = { success: "var(--ok)", error: "var(--danger)", info: "var(--info)" };

function toast(message, kind = "info", title = "") {
  const host = document.getElementById("toasts");
  if (!host) return;
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `
    <span style="color:${COLORS[kind] || COLORS.info};width:16px;height:16px;flex:none;margin-top:1px">${ICONS[kind] || ICONS.info}</span>
    <div class="toast-body">
      ${title ? `<div class="toast-title"></div>` : ""}
      <div class="toast-msg"></div>
    </div>`;
  if (title) el.querySelector(".toast-title").textContent = title;
  el.querySelector(".toast-msg").textContent = message;
  host.appendChild(el);
  const life = kind === "error" ? 8000 : 4000;
  setTimeout(() => {
    el.style.transition = "opacity .2s, transform .2s";
    el.style.opacity = "0";
    el.style.transform = "translateY(8px)";
    setTimeout(() => el.remove(), 220);
  }, life);
}

/* --- DOM ----------------------------------------------------------------- */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function busy(button, on, label = null) {
  if (!button) return;
  if (on) {
    button.dataset.label = button.innerHTML;
    button.disabled = true;
    button.innerHTML = `<span class="spinner"></span>${label ? ` ${escapeHtml(label)}` : ""}`;
  } else {
    button.disabled = false;
    if (button.dataset.label) button.innerHTML = button.dataset.label;
  }
}

async function copyText(value, label = "Copied") {
  try {
    await navigator.clipboard.writeText(value);
    toast(label, "success");
  } catch (_) {
    toast("Clipboard blocked by the browser", "error");
  }
}

function relativeTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diff = Math.round((Date.now() - then) / 1000);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function formatDuration(seconds) {
  const total = Math.round(seconds || 0);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function scoreColor(score) {
  if (score >= 80) return "var(--ok)";
  if (score >= 60) return "var(--warn)";
  return "var(--text-mute)";
}

/* --- Confirm modal ------------------------------------------------------- */

function confirmDialog({ title, message, confirmLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    const backdrop = el("div", "modal-backdrop");
    backdrop.innerHTML = `
      <div class="modal" style="max-width:440px">
        <div class="modal-head"><h2></h2></div>
        <div class="modal-body"><p class="dim" style="font-size:13.5px"></p></div>
        <div class="modal-foot">
          <button class="btn" data-act="cancel">Cancel</button>
          <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-act="ok"></button>
        </div>
      </div>`;
    backdrop.querySelector("h2").textContent = title;
    backdrop.querySelector("p").textContent = message;
    backdrop.querySelector('[data-act="ok"]').textContent = confirmLabel;

    const close = (value) => {
      backdrop.remove();
      document.removeEventListener("keydown", onKey);
      resolve(value);
    };
    const onKey = (e) => {
      if (e.key === "Escape") close(false);
    };

    backdrop.querySelector('[data-act="cancel"]').onclick = () => close(false);
    backdrop.querySelector('[data-act="ok"]').onclick = () => close(true);
    backdrop.onclick = (e) => {
      if (e.target === backdrop) close(false);
    };
    document.addEventListener("keydown", onKey);
    document.body.appendChild(backdrop);
  });
}

/* --- Theme --------------------------------------------------------------- */

(function initTheme() {
  const stored = localStorage.getItem("theme") || "dark";
  document.documentElement.dataset.theme = stored;

  const SUN =
    '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>';
  const MOON = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>';

  function paint() {
    const dark = document.documentElement.dataset.theme !== "light";
    const icon = document.getElementById("themeIcon");
    const label = document.getElementById("themeLabel");
    if (icon) icon.innerHTML = dark ? MOON : SUN;
    if (label) label.textContent = dark ? "Dark" : "Light";
  }

  document.addEventListener("DOMContentLoaded", () => {
    paint();
    const button = document.getElementById("themeToggle");
    if (!button) return;
    button.onclick = () => {
      const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      document.documentElement.dataset.theme = next;
      localStorage.setItem("theme", next);
      paint();
    };
  });
})();

/* --- Sidebar counts ------------------------------------------------------ */

document.addEventListener("DOMContentLoaded", async () => {
  const paint = (id, value) => {
    const node = document.getElementById(id);
    if (node && value > 0) {
      node.textContent = value;
      node.classList.remove("hidden");
    }
  };
  try {
    const [projects, voices] = await Promise.all([
      API.get("/api/projects"),
      API.get("/api/voices"),
    ]);
    paint("navProjectCount", (projects.projects || []).length);
    paint("navVoiceCount", (voices.voices || []).length);
  } catch (_) {
    /* counts are decorative */
  }
});
