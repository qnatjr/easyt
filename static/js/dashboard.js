/* Dashboard: project list, workspace stats and the "new project" modal. */

(function () {
  "use strict";

  const D = window.APP_DEFAULTS || {};
  // How long one still image stays on screen. Per project, so this is only the
  // starting value for a new one — the listing never reports it back.
  const SCENE_SECONDS = Number(D.scene_seconds) || 3;
  const MIN_SCENE_SECONDS = Number(D.min_scene_seconds) || 2;
  const MAX_SCENE_SECONDS = Number(D.max_scene_seconds) || 10;
  const LANGUAGES = D.languages || {};
  const STEP_NAMES = ["Brief", "Titles", "Script", "Scenes", "Voiceover"];

  const VOICEOVER_BADGE = {
    idle: { cls: "", label: "Voiceover idle" },
    running: { cls: "info", label: "Voiceover running" },
    done: { cls: "ok", label: "Voiceover done" },
    partial: { cls: "warn", label: "Voiceover partial" },
    error: { cls: "danger", label: "Voiceover failed" },
    // The server rewrites a job stranded by a restart to this. Without an entry
    // it would fall back to "idle" and look like a project that never ran one.
    interrupted: { cls: "danger", label: "Voiceover interrupted" },
  };

  const SVG = {
    film:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20" rx="2.18"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="2" y1="7" x2="7" y2="7"/><line x1="2" y1="17" x2="7" y2="17"/><line x1="17" y1="17" x2="22" y2="17"/><line x1="17" y1="7" x2="22" y2="7"/></svg>',
    search:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
    plus:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
    trash:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
    info:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
  };

  let PROJECTS = [];
  let query = "";

  /* --- Helpers ----------------------------------------------------------- */

  const num = (value, fallback) => {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  };

  const titleCase = (value) =>
    String(value || "").replace(/\b[a-z]/g, (c) => c.toUpperCase());

  // escapeHtml() is text-node safe but leaves quotes intact, so attribute
  // values get one extra pass.
  const attr = (value) => escapeHtml(value).replace(/"/g, "&quot;");

  const languageLabel = (code) => LANGUAGES[code] || code || "";

  const imageCountFor = (minutes, seconds) => Math.ceil((minutes * 60) / seconds);

  const grouped = (value) => Math.round(value).toLocaleString("en-US");

  function stepDots(step) {
    const current = Math.min(Math.max(num(step, 1), 1), 5);
    let out = "";
    for (let i = 1; i <= 5; i += 1) {
      const color =
        i < current ? "var(--ok)" : i === current ? "var(--accent)" : "var(--surface-3)";
      out += `<span class="dot" style="color:${color}"></span>`;
    }
    const label = `Step ${current} of 5 · ${STEP_NAMES[current - 1]}`;
    return `<span class="row gap-6" style="justify-content:flex-end" title="${attr(
      label
    )}" aria-label="${attr(label)}">${out}</span>`;
  }

  /* --- Stats ------------------------------------------------------------- */

  function paintStats() {
    const total = PROJECTS.length;
    const withScript = PROJECTS.filter((p) => p.has_script).length;
    const withScenes = PROJECTS.filter((p) => num(p.scene_count, 0) > 0).length;
    const images = PROJECTS.reduce((sum, p) => sum + num(p.scene_count, 0), 0);
    const voiced = PROJECTS.filter((p) => p.voiceover_status === "done").length;
    const newest = PROJECTS.reduce(
      (best, p) => (p.updated_at > best ? p.updated_at : best),
      ""
    );

    const set = (id, value) => {
      const node = document.getElementById(id);
      if (node) node.textContent = value;
    };

    set("statProjects", String(total));
    set(
      "statProjectsSub",
      total ? `last touched ${relativeTime(newest) || "recently"}` : "nothing here yet"
    );

    set("statScripts", String(withScript));
    set(
      "statScriptsSub",
      total ? `${Math.round((withScript / total) * 100)}% of projects` : "no scripts yet"
    );

    set("statWithScenes", String(withScenes));
    set("statWithScenesSub", `${voiced} fully voiced`);

    // Every project sets its own seconds-per-image and the listing does not
    // report it, so an average is the only honest thing to say here.
    set("statScenes", grouped(images));
    set(
      "statScenesSub",
      images
        ? `≈ ${grouped(images / withScenes)} per project on average`
        : "no image prompts yet"
    );

    const navCount = document.getElementById("navProjectCount");
    if (navCount) {
      navCount.textContent = String(total);
      navCount.classList.toggle("hidden", total === 0);
    }
  }

  /* --- List -------------------------------------------------------------- */

  function visibleProjects() {
    const q = query.trim().toLowerCase();
    if (!q) return PROJECTS;
    return PROJECTS.filter(
      (p) =>
        String(p.topic || "").toLowerCase().includes(q) ||
        String(p.selected_title || "").toLowerCase().includes(q)
    );
  }

  function rowHtml(p) {
    const id = String(p.id || "");
    const minutes = num(p.minutes, 0);
    const sceneCount = num(p.scene_count, 0);
    const vo = VOICEOVER_BADGE[p.voiceover_status] || VOICEOVER_BADGE.idle;

    const badges = [
      p.category ? `<span class="badge">${escapeHtml(titleCase(p.category))}</span>` : "",
      p.language ? `<span class="badge">${escapeHtml(languageLabel(p.language))}</span>` : "",
      `<span class="badge">${escapeHtml(String(minutes))} min</span>`,
      p.has_script ? '<span class="badge">Script</span>' : "",
      sceneCount > 0
        ? `<span class="badge">${sceneCount} image${sceneCount === 1 ? "" : "s"}</span>`
        : "",
      `<span class="badge ${vo.cls}">${escapeHtml(vo.label)}</span>`,
    ]
      .filter(Boolean)
      .join("");

    const subtitle = p.selected_title
      ? `<div class="small muted truncate" style="margin-top:2px">${escapeHtml(
          p.selected_title
        )}</div>`
      : `<div class="small muted truncate" style="margin-top:2px">No title picked yet</div>`;

    return `
      <div class="list-item" data-id="${attr(id)}" role="link" tabindex="0" style="cursor:pointer">
        <div class="grow">
          <div class="truncate" style="font-weight:600">${escapeHtml(p.topic || "Untitled project")}</div>
          ${subtitle}
          <div class="row wrap gap-6" style="margin-top:8px">${badges}</div>
        </div>
        <div style="flex:none;text-align:right">
          ${stepDots(p.step)}
          <div class="small muted" style="margin-top:6px">${escapeHtml(
            relativeTime(p.updated_at)
          )}</div>
        </div>
        <a class="btn btn-sm" href="/projects/${encodeURIComponent(id)}" data-open>Open</a>
        <button class="btn btn-sm btn-icon btn-danger" type="button" data-delete="${attr(
          id
        )}" title="Delete project" aria-label="Delete project">${SVG.trash}</button>
      </div>`;
  }

  function emptyState(kind) {
    if (kind === "search") {
      return `
        <div class="empty">
          <div class="empty-icon">${SVG.search}</div>
          <h3>No project matches that</h3>
          <p>Nothing here has that word in its topic or selected title. Try a shorter search, or start something new.</p>
          <button class="btn btn-primary" type="button" id="clearSearchBtn">Clear search</button>
        </div>`;
    }
    return `
      <div class="empty">
        <div class="empty-icon">${SVG.film}</div>
        <h3>No projects yet</h3>
        <p>A project starts with one line about the video you want. The model writes the titles, the script and one image prompt for every few seconds of narration.</p>
        <button class="btn btn-primary" type="button" id="emptyNewBtn">${SVG.plus} New project</button>
      </div>`;
  }

  function render() {
    const host = document.getElementById("projectList");
    if (!host) return;
    const rows = visibleProjects();

    const counter = document.getElementById("listCount");
    if (counter) {
      counter.textContent = PROJECTS.length
        ? rows.length === PROJECTS.length
          ? `${PROJECTS.length} project${PROJECTS.length === 1 ? "" : "s"}`
          : `${rows.length} of ${PROJECTS.length} shown`
        : "Nothing here yet";
    }

    if (!rows.length) {
      host.innerHTML = emptyState(PROJECTS.length ? "search" : "none");
      const clear = document.getElementById("clearSearchBtn");
      if (clear) {
        clear.onclick = () => {
          const input = document.getElementById("searchInput");
          if (input) input.value = "";
          query = "";
          render();
        };
      }
      const create = document.getElementById("emptyNewBtn");
      if (create) create.onclick = () => openNewProject();
      return;
    }

    host.innerHTML = rows.map(rowHtml).join("");
  }

  /* --- Delete ------------------------------------------------------------ */

  async function deleteProject(id, button) {
    const project = PROJECTS.find((p) => String(p.id) === String(id));
    const label = project && project.topic ? project.topic : "This project";
    const ok = await confirmDialog({
      title: "Delete this project?",
      message: `"${label}" and everything generated for it — titles, script, image prompts and rendered audio — will be deleted from disk. This cannot be undone.`,
      confirmLabel: "Delete project",
      danger: true,
    });
    if (!ok) return;

    busy(button, true);
    try {
      await API.del(`/api/projects/${encodeURIComponent(id)}`);
      PROJECTS = PROJECTS.filter((p) => String(p.id) !== String(id));
      paintStats();
      render();
      toast("Project deleted", "success");
    } catch (err) {
      busy(button, false);
      toast(err.message || "Could not delete the project", "error", "Delete failed");
    }
  }

  /* --- New project modal ------------------------------------------------- */

  function openNewProject() {
    if (document.querySelector("[data-new-project]")) return;

    const categories = Array.isArray(D.categories) ? D.categories : [];
    const minMinutes = num(D.min_minutes, 0.5);
    const maxMinutes = num(D.max_minutes, 60);
    const defaultWpm = num(D.default_wpm, 150);
    const defaultMinutes = Math.min(Math.max(5, minMinutes), maxMinutes);
    const defaultSceneSeconds = Math.min(
      Math.max(SCENE_SECONDS, MIN_SCENE_SECONDS),
      MAX_SCENE_SECONDS
    );

    const categoryOptions = categories
      .map(
        (c) =>
          `<option value="${attr(c)}"${c === D.default_category ? " selected" : ""}>${escapeHtml(
            titleCase(c)
          )}</option>`
      )
      .join("");

    const languageOptions = Object.keys(LANGUAGES)
      .map(
        (code) =>
          `<option value="${attr(code)}"${code === D.default_language ? " selected" : ""}>${escapeHtml(
            LANGUAGES[code]
          )}</option>`
      )
      .join("");

    const backdrop = el("div", "modal-backdrop");
    backdrop.setAttribute("data-new-project", "");
    backdrop.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="npTitle">
        <div class="modal-head">
          <h2 id="npTitle" class="grow">New project</h2>
        </div>
        <div class="modal-body">
          <div class="stack">
            <div class="field">
              <label class="label" for="npTopic">What is this video about?</label>
              <textarea class="textarea" id="npTopic" placeholder="e.g. Why the Roman concrete in seawater harbours is still standing after 2000 years — told as a slow, cinematic mystery."></textarea>
              <div class="hint">Write it in any language you like. The model reads it and answers in the output language you choose below.</div>
            </div>

            <div class="grid cols-2">
              <div class="field">
                <label class="label" for="npCategory">Category</label>
                <select class="select" id="npCategory">${categoryOptions}</select>
              </div>
              <div class="field">
                <label class="label" for="npLanguage">Output language</label>
                <select class="select" id="npLanguage">${languageOptions}</select>
                <div class="hint">Titles and narration follow this. Image prompts stay in English either way — the generators expect it.</div>
              </div>
            </div>

            <div class="field">
              <label class="label" for="npNiche">Niche <span class="muted">optional</span></label>
              <select class="select" id="npNiche"><option value="">None</option></select>
              <div class="hint">An analysed reference from the Niche Bank. Its profile — tone, pacing, hooks, art direction — is copied into this project and used for the titles, the script and the image prompts.</div>
            </div>

            <div class="field">
              <label class="label" for="npReference">Reference <span class="muted">optional</span></label>
              <input class="input" type="text" id="npReference" placeholder="Reference channel or video to echo">
              <div class="hint">A channel or video whose tone and pacing you want the script to sit close to. A niche above says the same thing in far more detail.</div>
            </div>

            <div class="grid cols-4">
              <div class="field">
                <label class="label" for="npMinutes">Minutes</label>
                <input class="input tabnum" type="number" id="npMinutes" step="0.5" min="${minMinutes}" max="${maxMinutes}" value="${defaultMinutes}">
              </div>
              <div class="field">
                <label class="label" for="npWpm">Words per minute</label>
                <input class="input tabnum" type="number" id="npWpm" step="5" min="80" max="220" value="${defaultWpm}">
              </div>
              <div class="field">
                <label class="label" for="npSceneSeconds">Seconds per image</label>
                <input class="input tabnum" type="number" id="npSceneSeconds" step="1" min="${MIN_SCENE_SECONDS}" max="${MAX_SCENE_SECONDS}" value="${defaultSceneSeconds}">
              </div>
              <div class="field">
                <label class="label" for="npMaxWords">Word cap <span class="muted">optional</span></label>
                <input class="input tabnum" type="number" id="npMaxWords" step="10" min="20" max="20000" placeholder="—">
              </div>
            </div>
            <div class="hint">Words per minute drives the word target; seconds per image decides how many stills the runtime needs. Leave the cap empty to let it sit 10% above the target.</div>

            <div class="callout">
              ${SVG.info}
              <div>
                <div><strong id="npDerived">—</strong></div>
                <div class="small" style="margin-top:3px" id="npCadence"></div>
              </div>
            </div>
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn" type="button" data-act="cancel">Cancel</button>
          <button class="btn btn-primary" type="button" data-act="create">Create project</button>
        </div>
      </div>`;

    const topicEl = $("#npTopic", backdrop);
    const categoryEl = $("#npCategory", backdrop);
    const languageEl = $("#npLanguage", backdrop);
    const referenceEl = $("#npReference", backdrop);
    const nicheEl = $("#npNiche", backdrop);

    // Only analysed niches are offered: one with an empty profile would attach
    // nothing and quietly look like it had.
    API.get("/api/niches")
      .then(({ niches }) => {
        (niches || [])
          .filter((niche) => niche.has_profile)
          .forEach((niche) => {
            const option = el("option", "", niche.name || niche.id);
            option.value = niche.id;
            nicheEl.appendChild(option);
          });
      })
      .catch(() => {
        /* The niche bank is optional; a project can be created without it. */
      });
    const minutesEl = $("#npMinutes", backdrop);
    const wpmEl = $("#npWpm", backdrop);
    const sceneSecondsEl = $("#npSceneSeconds", backdrop);
    const maxWordsEl = $("#npMaxWords", backdrop);
    const derivedEl = $("#npDerived", backdrop);
    const cadenceEl = $("#npCadence", backdrop);
    const createBtn = $('[data-act="create"]', backdrop);
    const cancelBtn = $('[data-act="cancel"]', backdrop);

    let creating = false;

    function paintDerived() {
      const minutes = num(minutesEl.value, 0);
      const wpm = num(wpmEl.value, defaultWpm);
      // A half-typed or emptied field reads as 0; fall back so the read-out
      // never divides by it. submit() is what actually enforces the bounds.
      const typedSeconds = Math.round(num(sceneSecondsEl.value, 0));
      const seconds = typedSeconds > 0 ? typedSeconds : defaultSceneSeconds;

      cadenceEl.textContent = `One scene is one still image held for ${seconds} second${
        seconds === 1 ? "" : "s"
      }. Nothing is animated — this tool writes image prompts, it never generates video.`;

      if (minutes <= 0 || wpm <= 0) {
        derivedEl.textContent = "Set a runtime to see the budget";
        maxWordsEl.placeholder = "—";
        return;
      }
      const words = Math.round(minutes * wpm);
      const images = imageCountFor(minutes, seconds);
      const shownMinutes = Number.isInteger(minutes) ? minutes : minutes.toFixed(1);
      derivedEl.textContent = `~${grouped(words)} words · ~${grouped(
        images
      )} images · ~${shownMinutes} min`;
      maxWordsEl.placeholder = String(Math.round(words * 1.1));
    }

    function close() {
      if (creating) return;
      backdrop.remove();
      document.removeEventListener("keydown", onKey);
    }

    function onKey(e) {
      if (e.key === "Escape") close();
    }

    async function submit() {
      const topic = topicEl.value.trim();
      if (!topic) {
        toast(
          "Tell the model what the video is about before creating the project.",
          "error",
          "Topic required"
        );
        topicEl.focus();
        return;
      }

      const minutes = num(minutesEl.value, NaN);
      if (!Number.isFinite(minutes) || minutes < minMinutes || minutes > maxMinutes) {
        toast(
          `Runtime must be between ${minMinutes} and ${maxMinutes} minutes.`,
          "error",
          "Check the runtime"
        );
        minutesEl.focus();
        return;
      }

      const wpm = num(wpmEl.value, NaN);
      if (!Number.isFinite(wpm) || wpm < 80 || wpm > 220) {
        toast("Words per minute must be between 80 and 220.", "error", "Check the pacing");
        wpmEl.focus();
        return;
      }

      const sceneSeconds = Math.round(num(sceneSecondsEl.value, NaN));
      if (
        !Number.isFinite(sceneSeconds) ||
        sceneSeconds < MIN_SCENE_SECONDS ||
        sceneSeconds > MAX_SCENE_SECONDS
      ) {
        toast(
          `Seconds per image must be between ${MIN_SCENE_SECONDS} and ${MAX_SCENE_SECONDS}.`,
          "error",
          "Check the cadence"
        );
        sceneSecondsEl.focus();
        return;
      }

      const body = {
        topic,
        category: categoryEl.value,
        language: languageEl.value,
        reference: referenceEl.value.trim(),
        niche_id: nicheEl ? nicheEl.value : "",
        minutes,
        wpm,
        scene_seconds: sceneSeconds,
      };

      const maxWords = parseInt(maxWordsEl.value, 10);
      if (maxWordsEl.value.trim() && !Number.isFinite(maxWords)) {
        toast("Max words must be a number, or left blank.", "error");
        return;
      }
      if (Number.isFinite(maxWords)) {
        // Mirror the server's bounds so a bad value is caught in the form
        // rather than coming back as a raw validation error.
        if (maxWords < 20 || maxWords > 20000) {
          toast("Max words must be between 20 and 20,000.", "error");
          return;
        }
        body.max_words = maxWords;
      }

      creating = true;
      busy(createBtn, true, "Creating…");
      cancelBtn.disabled = true;
      try {
        const doc = await API.post("/api/projects", body);
        window.location = `/projects/${encodeURIComponent(doc.id)}`;
      } catch (err) {
        creating = false;
        busy(createBtn, false);
        cancelBtn.disabled = false;
        toast(err.message || "Could not create the project", "error", "Create failed");
      }
    }

    minutesEl.addEventListener("input", paintDerived);
    wpmEl.addEventListener("input", paintDerived);
    sceneSecondsEl.addEventListener("input", paintDerived);
    createBtn.onclick = submit;
    cancelBtn.onclick = close;
    backdrop.onclick = (e) => {
      if (e.target === backdrop) close();
    };
    backdrop.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.tagName !== "TEXTAREA" && !creating) {
        e.preventDefault();
        submit();
      }
    });
    document.addEventListener("keydown", onKey);

    document.body.appendChild(backdrop);
    paintDerived();
    topicEl.focus();
  }

  /* --- Boot -------------------------------------------------------------- */

  async function load() {
    const host = document.getElementById("projectList");
    try {
      const data = await API.get("/api/projects");
      PROJECTS = Array.isArray(data.projects) ? data.projects : [];
      paintStats();
      render();
    } catch (err) {
      if (host) {
        host.innerHTML = `
          <div class="empty">
            <div class="empty-icon">${SVG.film}</div>
            <h3>Could not load your projects</h3>
            <p class="dim"></p>
            <button class="btn btn-primary" type="button" id="retryLoadBtn">Try again</button>
          </div>`;
        const message = $(".empty p", host);
        if (message) message.textContent = err.message || "The API did not respond.";
        const retry = document.getElementById("retryLoadBtn");
        if (retry) retry.onclick = () => load();
      }
      toast(err.message || "Could not load projects", "error", "Load failed");
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    const newBtn = document.getElementById("newProjectBtn");
    if (newBtn) newBtn.onclick = () => openNewProject();

    const search = document.getElementById("searchInput");
    if (search) {
      search.addEventListener("input", () => {
        query = search.value;
        render();
      });
    }

    const host = document.getElementById("projectList");
    if (host) {
      host.addEventListener("click", (e) => {
        const del = e.target.closest("[data-delete]");
        if (del) {
          e.preventDefault();
          e.stopPropagation();
          deleteProject(del.getAttribute("data-delete"), del);
          return;
        }
        if (e.target.closest("a")) return;
        const row = e.target.closest(".list-item");
        if (row && row.dataset.id) {
          window.location = `/projects/${encodeURIComponent(row.dataset.id)}`;
        }
      });

      host.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const row = e.target.closest(".list-item");
        if (!row || !row.dataset.id || e.target.closest("[data-delete], a")) return;
        e.preventDefault();
        window.location = `/projects/${encodeURIComponent(row.dataset.id)}`;
      });
    }

    const params = new URLSearchParams(window.location.search);
    if (params.get("new") === "1") {
      params.delete("new");
      const rest = params.toString();
      window.history.replaceState({}, "", window.location.pathname + (rest ? `?${rest}` : ""));
      openNewProject();
    }

    load();
  });
})();
