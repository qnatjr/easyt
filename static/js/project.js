/* Project wizard: context -> titles -> script -> scenes -> voice-over.
   The server document is the only source of truth. Nothing is cached locally. */

(function () {
  "use strict";

  const APP = document.getElementById("app");
  if (!APP) return;

  const PID = APP.dataset.projectId;
  const D = window.APP_DEFAULTS || {};
  const DEFAULT_SCENE_SECONDS = Number(D.scene_seconds) || 3;
  const MIN_SCENE_SECONDS = Number(D.min_scene_seconds) || 2;
  const MAX_SCENE_SECONDS = Number(D.max_scene_seconds) || 10;
  const DEFAULT_WPM = Number(D.default_wpm) || 150;
  // Mirrors DEFAULT_MAX_WORDS_FACTOR on the server so the read-out matches.
  const MAX_WORDS_FACTOR = 1.1;
  // Mirrors MAX_CHUNK_CHARS on the server. Only used to say roughly how many
  // synthesis requests a script will become before one is started.
  const CHUNK_CHARS = 1500;
  // A short cadence turns a long script into hundreds of cards, so the card
  // view grows a page at a time instead of laying all of them out at once.
  const CARD_PAGE = 60;
  const POLL_MS = 2500;

  const url = (suffix) => `/api/projects/${encodeURIComponent(PID)}${suffix || ""}`;

  const state = {
    doc: null,
    step: 1,
    voices: [],
    vo: null, // live voice-over status while polling; null means "read the doc"
    pollTimer: null,
    polling: false,
    contextDirty: false,
    scriptFormDirty: false,
    scriptDirty: false,
    titlePending: false,
    sceneView: "cards",
    sceneFilter: "",
    sceneLimit: CARD_PAGE,
    bibleOpen: false,
  };

  /* --- Icons ------------------------------------------------------------- */

  const S = (body, w) =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${w || 2}" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;

  const ICON = {
    copy: S(
      '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'
    ),
    alert: S(
      '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>'
    ),
    info: S(
      '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>'
    ),
    zap: S('<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>'),
    file: S(
      '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>'
    ),
    image: S(
      '<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>'
    ),
    mic: S(
      '<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/>'
    ),
    refresh: S(
      '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>'
    ),
    download: S(
      '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>'
    ),
  };

  /* --- Small helpers ----------------------------------------------------- */

  const num = (n) => Number(n || 0).toLocaleString("en-US");

  function wordCount(text) {
    const t = (text || "").trim();
    return t ? t.split(/\s+/).length : 0;
  }

  // Tenths, because an image can be up for well under a second at a short
  // cadence and whole seconds would show two cuts at the same timecode.
  function timecode(seconds) {
    const total = Math.max(0, Number(seconds) || 0);
    const whole = Math.floor(total);
    let tenths = Math.round((total - whole) * 10);
    let secs = whole;
    if (tenths === 10) {
      tenths = 0;
      secs += 1;
    }
    return `${String(Math.floor(secs / 60)).padStart(2, "0")}:${String(secs % 60).padStart(
      2,
      "0"
    )}.${tenths}`;
  }

  function ctx() {
    return (state.doc && state.doc.context) || {};
  }

  function scenes() {
    return (state.doc && state.doc.scenes) || [];
  }

  function sceneSeconds() {
    return Number(ctx().scene_seconds) || DEFAULT_SCENE_SECONDS;
  }

  // Mirrors words_per_scene on the server: floored, never rounded, because a
  // rounded-up budget would let the narration overrun the seconds its image is
  // on screen and the error would compound across hundreds of images.
  function wordsPerScene(wpm, seconds) {
    const w = Number(wpm) || DEFAULT_WPM;
    const s = Number(seconds) || DEFAULT_SCENE_SECONDS;
    return Math.max(2, Math.floor((w / 60) * s));
  }

  function expectedImages(words, wpm, seconds) {
    const total = Number(words) || 0;
    return total ? Math.ceil(total / wordsPerScene(wpm, seconds)) : 0;
  }

  // The server splits the script on sentence boundaries into requests of at
  // most CHUNK_CHARS characters. This is close enough to warn with before a
  // run; the status endpoint reports the real count once the job starts.
  function expectedParts() {
    const script = ((state.doc || {}).script || "").trim();
    return script ? Math.max(1, Math.ceil(script.length / CHUNK_CHARS)) : 0;
  }

  function budget(minutes, wpm, maxWords) {
    const m = Number(minutes) || 0;
    const w = Number(wpm) || DEFAULT_WPM;
    const target = Math.max(20, Math.round(m * w));
    const max = Math.max(20, Math.round(Number(maxWords) || target * MAX_WORDS_FACTOR));
    return { target, max };
  }

  async function guard(button, label, fn) {
    if (button) busy(button, true, label);
    try {
      return await fn();
    } catch (err) {
      toast(err && err.message ? err.message : String(err), "error", "Request failed");
      return null;
    } finally {
      if (button) busy(button, false);
    }
  }

  // Reloading is defensive on purpose: several handlers await this after a
  // successful mutation, and a failed reload should surface as a toast against
  // the last known-good document rather than an unhandled rejection.
  async function refresh() {
    try {
      state.doc = await API.get(url());
    } catch (err) {
      toast(
        err && err.message ? err.message : String(err),
        "error",
        "Could not reload the project"
      );
    }
  }

  function setLabel(selector, text) {
    const node = $(`${selector} [data-label]`);
    if (node) node.textContent = text;
  }

  function emptyState(host, opts) {
    host.innerHTML = "";
    const wrap = el("div", "empty");
    const icon = el("div", "empty-icon");
    icon.innerHTML = opts.icon;
    wrap.appendChild(icon);
    wrap.appendChild(el("h3", "", opts.title));
    wrap.appendChild(el("p", "", opts.body));
    if (opts.actionLabel) {
      const action = opts.href
        ? el("a", "btn btn-primary", opts.actionLabel)
        : el("button", "btn btn-primary", opts.actionLabel);
      if (opts.href) action.href = opts.href;
      else {
        action.type = "button";
        action.onclick = opts.onAction;
      }
      wrap.appendChild(action);
    }
    host.appendChild(wrap);
    host.classList.remove("hidden");
  }

  function callout(kind, text, iconSvg) {
    const node = el("div", `callout ${kind}`);
    const icon = el("span", "");
    icon.innerHTML = iconSvg || (kind === "info" ? ICON.info : ICON.alert);
    icon.style.cssText = "width:15px;height:15px;flex:none;margin-top:1px";
    node.appendChild(icon);
    const body = el("div", "", text);
    node.appendChild(body);
    return node;
  }

  function copyButton(value, label, text, extraClass) {
    const button = el("button", `btn btn-sm ${text ? "" : "btn-icon"} ${extraClass || ""}`.trim());
    button.type = "button";
    button.title = `Copy ${label.toLowerCase()}`;
    button.innerHTML = ICON.copy;
    if (text) {
      const span = el("span", "", text);
      button.appendChild(span);
    }
    button.onclick = (event) => {
      event.stopPropagation();
      copyText(value || "", `${label} copied`);
    };
    return button;
  }

  function elapsedBlock(selector, on) {
    const node = $(selector);
    if (!node) return;
    const span = $("[data-elapsed]", node);
    if (node._stopElapsed) {
      node._stopElapsed();
      node._stopElapsed = null;
    }
    node.classList.toggle("hidden", !on);
    if (!on) {
      if (span) span.textContent = "";
      return;
    }
    const started = Date.now();
    const tick = () => {
      if (span) span.textContent = `${Math.round((Date.now() - started) / 1000)}s elapsed.`;
    };
    tick();
    const id = setInterval(tick, 1000);
    node._stopElapsed = () => clearInterval(id);
  }

  /* --- Stepper ----------------------------------------------------------- */

  const REACHABLE = {
    1: () => true,
    2: () => true,
    3: () => Boolean(state.doc && state.doc.selected_title),
    4: () => Boolean(state.doc && state.doc.script),
    // The narration is cut from the script, not from the scenes, so it can be
    // rendered before the breakdown exists.
    5: () => Boolean(state.doc && state.doc.script),
  };

  const BLOCKED = {
    3: "Pick a title before writing the script.",
    4: "Write a script before cutting it into scenes.",
    5: "Write a script before narrating it.",
  };

  function stepHasData(index) {
    const doc = state.doc || {};
    if (index === 1) return true;
    if (index === 2) return (doc.titles || []).length > 0;
    if (index === 3) return Boolean(doc.script);
    if (index === 4) return scenes().length > 0;
    if (index === 5) return Boolean((doc.voiceover || {}).total);
    return false;
  }

  function stepMeta(index) {
    const doc = state.doc || {};
    const c = ctx();
    if (index === 1) {
      const language = (D.languages || {})[c.language] || c.language || "";
      return `${c.minutes || 0} min · ${language}`;
    }
    if (index === 2) {
      const count = (doc.titles || []).length;
      if (!count) return "";
      return `${count} option${count === 1 ? "" : "s"}${doc.selected_title ? " · picked" : ""}`;
    }
    if (index === 3) {
      const words = (doc.script_meta || {}).word_count || wordCount(doc.script);
      return words ? `${num(words)} words` : "";
    }
    if (index === 4) {
      const all = scenes();
      if (!all.length) return "";
      const failed = all.filter((s) => s.status !== "ok").length;
      return `${num(all.length)} images${failed ? ` · ${failed} failed` : ""}`;
    }
    if (index === 5) {
      const vo = voState();
      if (!vo.total) return "";
      return `${num(vo.done || 0)}/${num(vo.total)} parts`;
    }
    return "";
  }

  function renderStepper() {
    $$("#stepper .step").forEach((node) => {
      const index = Number(node.dataset.step);
      const reachable = REACHABLE[index]();
      node.setAttribute("aria-disabled", reachable ? "false" : "true");
      node.classList.toggle("active", index === state.step);
      node.classList.toggle("done", stepHasData(index) && index !== state.step);
      const meta = $(`[data-meta="${index}"]`, node);
      if (meta) meta.textContent = reachable ? stepMeta(index) : BLOCKED[index] || "";
    });
  }

  function showStep(index) {
    if (!REACHABLE[index]()) {
      toast(BLOCKED[index] || "That step is not available yet.", "info");
      return;
    }
    state.step = index;
    $$("[data-panel]").forEach((panel) => {
      panel.classList.toggle("hidden", Number(panel.dataset.panel) !== index);
    });
    renderStepper();
    try {
      history.replaceState(null, "", `#step-${index}`);
    } catch (_) {
      /* hash is a convenience, not a requirement */
    }
  }

  /* --- Step 1: context --------------------------------------------------- */

  const CTX_FIELDS = ["#ctxTopic", "#ctxCategory", "#ctxLanguage", "#ctxReference", "#ctxMinutes", "#ctxWpm", "#ctxSceneSeconds", "#ctxMaxWords"];

  function fillContextForm() {
    const c = ctx();
    $("#ctxTopic").value = c.topic || "";
    $("#ctxCategory").value = c.category || D.default_category || "";
    $("#ctxLanguage").value = c.language || D.default_language || "";
    $("#ctxReference").value = c.reference || "";
    $("#ctxMinutes").value = c.minutes != null ? c.minutes : 5;
    $("#ctxWpm").value = c.wpm || DEFAULT_WPM;
    $("#ctxSceneSeconds").value = c.scene_seconds || DEFAULT_SCENE_SECONDS;
    $("#ctxMaxWords").value = c.max_words || "";
    state.contextDirty = false;
    $("#ctxDirty").textContent = "";
  }

  function readContextForm() {
    return {
      topic: $("#ctxTopic").value.trim(),
      category: $("#ctxCategory").value,
      language: $("#ctxLanguage").value,
      reference: $("#ctxReference").value.trim(),
      minutes: Number($("#ctxMinutes").value),
      wpm: Number($("#ctxWpm").value),
      scene_seconds: Number($("#ctxSceneSeconds").value),
      max_words: $("#ctxMaxWords").value === "" ? null : Number($("#ctxMaxWords").value),
    };
  }

  function renderDerived() {
    const form = readContextForm();
    const { target, max } = budget(form.minutes, form.wpm, form.max_words);
    const seconds = Number(form.scene_seconds) || DEFAULT_SCENE_SECONDS;
    const images = expectedImages(target, form.wpm, seconds);

    $("#derivedTarget").textContent = num(target);
    $("#derivedTargetSub").textContent = `${form.minutes || 0} min × ${form.wpm || DEFAULT_WPM} wpm`;

    $("#derivedMax").textContent = num(max);
    const over = target ? Math.round((max / target - 1) * 100) : 0;
    const maxSub = $("#derivedMaxSub");
    maxSub.textContent = over >= 0 ? `${over}% above the target` : `${Math.abs(over)}% below the target`;
    maxSub.style.color = over < 0 ? "var(--warn)" : "";

    $("#derivedImages").textContent = num(images);
    $("#derivedImagesSub").textContent = `${seconds}s on screen, ${num(wordsPerScene(form.wpm, seconds))} words each`;

    $("#derivedRuntime").textContent = formatDuration((form.minutes || 0) * 60);

    $("#derivedLine").textContent =
      `~${num(target)} words · ~${num(images)} images · ~${form.minutes || 0} min`;
  }

  function renderContext() {
    if (!state.contextDirty) fillContextForm();
    renderDerived();
  }

  function wireContext() {
    const onEdit = () => {
      state.contextDirty = true;
      $("#ctxDirty").textContent = "Unsaved changes";
      renderDerived();
    };
    CTX_FIELDS.forEach((selector) => {
      const node = $(selector);
      node.addEventListener("input", onEdit);
      node.addEventListener("change", onEdit);
    });

    $("#ctxMaxReset").onclick = () => {
      const form = readContextForm();
      const { target } = budget(form.minutes, form.wpm, null);
      $("#ctxMaxWords").value = Math.max(20, Math.round(target * MAX_WORDS_FACTOR));
      state.contextDirty = true;
      $("#ctxDirty").textContent = "Unsaved changes";
      renderDerived();
    };

    $("#ctxRevert").onclick = () => {
      fillContextForm();
      renderDerived();
    };

    $("#ctxSave").onclick = async (event) => {
      const form = readContextForm();
      if (!form.topic) {
        toast("The topic cannot be empty.", "error");
        return;
      }
      const minMinutes = Number(D.min_minutes) || 0.5;
      const maxMinutes = Number(D.max_minutes) || 60;
      if (!(form.minutes >= minMinutes && form.minutes <= maxMinutes)) {
        toast(`Minutes must be between ${minMinutes} and ${maxMinutes}.`, "error");
        return;
      }
      if (!(form.wpm >= 80 && form.wpm <= 220)) {
        toast("Words per minute must be between 80 and 220.", "error");
        return;
      }
      if (!(form.scene_seconds >= MIN_SCENE_SECONDS && form.scene_seconds <= MAX_SCENE_SECONDS)) {
        toast(
          `Seconds per image must be between ${MIN_SCENE_SECONDS} and ${MAX_SCENE_SECONDS}.`,
          "error"
        );
        return;
      }
      if (form.max_words != null && !(form.max_words >= 20 && form.max_words <= 20000)) {
        toast("Max words must be between 20 and 20,000, or left blank.", "error");
        return;
      }
      const payload = {
        topic: form.topic,
        category: form.category,
        language: form.language,
        reference: form.reference,
        minutes: form.minutes,
        wpm: form.wpm,
        scene_seconds: form.scene_seconds,
      };
      if (form.max_words != null) payload.max_words = form.max_words;

      const doc = await guard(event.currentTarget, "Saving", () => API.patch(url(), payload));
      if (!doc) return;
      state.doc = doc;
      state.contextDirty = false;
      state.scriptFormDirty = false;
      renderAll();
      toast("Context saved", "success");
    };
  }

  /* --- Step 2: titles ---------------------------------------------------- */

  function titleCard(item, rank) {
    const doc = state.doc || {};
    const card = el("div", "title-card");
    if (doc.selected_title && doc.selected_title === item.title) card.classList.add("selected");

    card.appendChild(el("div", "rank", String(rank)));

    const body = el("div", "grow");
    body.appendChild(el("div", "text", item.title || ""));

    const meta = el("div", "meta row wrap gap-6");
    const chars = el("span", "", `${num(item.char_count || (item.title || "").length)} characters`);
    if ((item.char_count || 0) > 70) chars.style.color = "var(--warn)";
    meta.appendChild(chars);
    (item.hooks || []).forEach((hook) => meta.appendChild(el("span", "chip", hook)));
    body.appendChild(meta);

    if (item.reason) {
      const reason = el("div", "small muted", item.reason);
      reason.style.marginTop = "6px";
      body.appendChild(reason);
    }
    card.appendChild(body);

    const score = el("div", "score");
    const value = el("div", "score-num", String(Math.round(item.ctr_score || 0)));
    value.style.color = scoreColor(item.ctr_score || 0);
    score.appendChild(value);
    const label = el("div", "small muted", "CTR");
    score.appendChild(label);
    const bar = el("div", "score-bar");
    bar.style.marginTop = "6px";
    const fill = el("div", "score-fill");
    fill.style.width = `${Math.max(0, Math.min(100, item.ctr_score || 0))}%`;
    fill.style.background = scoreColor(item.ctr_score || 0);
    bar.appendChild(fill);
    score.appendChild(bar);
    card.appendChild(score);

    card.onclick = () => selectTitle(item.title);
    return card;
  }

  async function selectTitle(title) {
    if (state.titlePending || !title) return;
    const list = $("#titlesList");
    state.titlePending = true;
    list.style.opacity = "0.55";
    list.style.pointerEvents = "none";
    try {
      state.doc = await API.patch(url(), { selected_title: title });
      renderAll();
      toast("Title selected", "success");
    } catch (err) {
      toast(err.message, "error", "Could not save the title");
    } finally {
      state.titlePending = false;
      list.style.opacity = "";
      list.style.pointerEvents = "";
    }
  }

  function renderTitles() {
    const doc = state.doc || {};
    const titles = doc.titles || [];
    const list = $("#titlesList");
    const emptyHost = $("#titlesEmpty");

    setLabel("#titlesGenerate", titles.length ? "Regenerate titles" : "Generate titles");
    $("#titlesDisclaimer").classList.toggle("hidden", titles.length === 0);

    $("#selectedTitleCard").classList.toggle("hidden", !doc.selected_title);
    $("#selectedTitleText").textContent = doc.selected_title || "";
    $("#customTitle").placeholder = doc.selected_title
      ? "Replace it with a custom title"
      : "Use a custom title instead";

    list.innerHTML = "";
    if (!titles.length) {
      list.classList.add("hidden");
      emptyState(emptyHost, {
        icon: ICON.zap,
        title: "No titles yet",
        body:
          "MIMO writes a handful of options from the context, scores how each one reads, and explains why. You pick the one you can actually deliver on.",
        actionLabel: "Generate titles",
        onAction: () => $("#titlesGenerate").click(),
      });
      return;
    }

    list.classList.remove("hidden");
    emptyHost.innerHTML = "";
    emptyHost.classList.add("hidden");
    titles.forEach((item, index) => list.appendChild(titleCard(item, index + 1)));
  }

  function wireTitles() {
    $("#titlesGenerate").onclick = async (event) => {
      // Captured before the first await: currentTarget is nulled once dispatch
      // ends, so reading it after the confirm dialog would leave the button
      // enabled and let a second click fire another generation.
      const button = event.currentTarget;
      if ((state.doc.titles || []).length) {
        const ok = await confirmDialog({
          title: "Regenerate titles",
          message:
            "This replaces the current list with a fresh one. A title you already selected stays selected.",
          confirmLabel: "Regenerate",
        });
        if (!ok) return;
      }
      const result = await guard(button, "Writing", () => API.post(url("/titles")));
      if (!result) return;
      await refresh();
      renderAll();
      toast(`${(result.titles || []).length} titles written`, "success");
    };

    $("#customTitleSave").onclick = async (event) => {
      const value = $("#customTitle").value.trim();
      if (!value) {
        toast("Type a title first.", "error");
        return;
      }
      const doc = await guard(event.currentTarget, "Saving", () =>
        API.patch(url(), { selected_title: value })
      );
      if (!doc) return;
      state.doc = doc;
      $("#customTitle").value = "";
      renderAll();
      toast("Custom title saved", "success");
    };
  }

  /* --- Step 3: script ---------------------------------------------------- */

  function fillScriptForm() {
    const c = ctx();
    $("#scMinutes").value = c.minutes != null ? c.minutes : 5;
    $("#scWpm").value = c.wpm || DEFAULT_WPM;
    $("#scMaxWords").value = c.max_words || "";
  }

  function updateScriptLive() {
    const text = $("#scriptText").value;
    const words = wordCount(text);
    const wpm = Number(ctx().wpm) || DEFAULT_WPM;
    const minutes = words / wpm;
    const dirty = state.scriptDirty ? " · unsaved edits" : "";
    $("#scriptLive").textContent = `${num(words)} words · about ${formatDuration(minutes * 60)} at ${wpm} wpm${dirty}`;
  }

  function renderScript() {
    const doc = state.doc || {};
    const meta = doc.script_meta || {};
    const c = ctx();

    $("#scriptForTitle").textContent = doc.selected_title
      ? `Writing for: ${doc.selected_title}`
      : "Pick a title first.";

    if (!state.scriptFormDirty) fillScriptForm();

    $("#scriptRegenWarn").textContent = scenes().length
      ? `Generating replaces the current script and clears the ${num(scenes().length)} scenes and the voice-over state cut from it.`
      : "Generating replaces the current script. Scenes are cut from whatever is saved here.";

    const hasScript = Boolean(doc.script);
    $("#scriptCard").classList.toggle("hidden", !hasScript);
    $("#scriptStats").classList.toggle("hidden", !hasScript);
    setLabel("#scriptGenerate", hasScript ? "Regenerate script" : "Generate script");

    const emptyHost = $("#scriptEmpty");
    if (!hasScript) {
      emptyState(emptyHost, {
        icon: ICON.file,
        title: "No script yet",
        body:
          "The script is written in one pass against the word budget above, then checked against the hard maximum. Everything downstream is cut from it.",
        actionLabel: "Generate script",
        onAction: () => $("#scriptGenerate").click(),
      });
      $("#scriptNotices").innerHTML = "";
      return;
    }
    emptyHost.innerHTML = "";
    emptyHost.classList.add("hidden");

    if (!state.scriptDirty) $("#scriptText").value = doc.script || "";
    updateScriptLive();

    const words = meta.word_count || wordCount(doc.script);
    const target = meta.target_words || budget(c.minutes, c.wpm, null).target;
    const maxWords = meta.max_words || c.max_words || budget(c.minutes, c.wpm, c.max_words).max;

    $("#statWords").textContent = num(words);
    $("#statWordsSub").textContent = meta.edited ? "Edited by hand" : "As written";

    $("#statTarget").textContent = num(target);
    $("#statTargetSub").textContent = `${num(maxWords)} words allowed`;

    const estimated = meta.estimated_minutes != null ? meta.estimated_minutes : words / (c.wpm || DEFAULT_WPM);
    $("#statMinutes").textContent = formatDuration(estimated * 60);
    $("#statMinutesSub").textContent = `At ${c.wpm || DEFAULT_WPM} wpm`;

    const over = Boolean(meta.over_budget) || words > maxWords;
    const budgetValue = $("#statBudget");
    budgetValue.textContent = over ? "Over" : "OK";
    budgetValue.style.color = over ? "var(--warn)" : "var(--ok)";
    $("#statBudgetSub").textContent = over
      ? `${num(words - maxWords)} words over the maximum`
      : `${num(Math.max(0, maxWords - words))} words of headroom`;

    const notices = $("#scriptNotices");
    notices.innerHTML = "";
    if (over) {
      notices.appendChild(
        callout(
          "warn",
          `The script came in at ${num(words)} words against a maximum of ${num(maxWords)}. Nothing was truncated — cutting it is your call, and every word over the line becomes another image to generate.`
        )
      );
    }
    if (meta.tightened) {
      notices.appendChild(
        callout(
          "info",
          "A shortening pass ran on this script: the first draft overran the budget, so the model was asked to tighten it before returning."
        )
      );
    }
  }

  function wireScript() {
    ["#scMinutes", "#scWpm", "#scMaxWords"].forEach((selector) => {
      $(selector).addEventListener("input", () => {
        state.scriptFormDirty = true;
      });
    });

    $("#scriptText").addEventListener("input", () => {
      state.scriptDirty = true;
      updateScriptLive();
    });

    $("#scriptGenerate").onclick = async (event) => {
      // Captured before the confirm dialog: currentTarget is null after it.
      const button = event.currentTarget;
      const doc = state.doc || {};
      if (!doc.selected_title) {
        toast("Pick a title first.", "error");
        return;
      }
      if (doc.script) {
        const ok = await confirmDialog({
          title: "Rewrite the script",
          message: scenes().length
            ? `This replaces the script and deletes the ${scenes().length} scenes and voice-over state derived from it.`
            : "This replaces the current script, including any edits you made by hand.",
          confirmLabel: "Rewrite",
          danger: true,
        });
        if (!ok) return;
      }

      const payload = { notes: $("#scNotes").value.trim() };
      const minutes = Number($("#scMinutes").value);
      const wpm = Number($("#scWpm").value);
      const maxWords = $("#scMaxWords").value === "" ? null : Number($("#scMaxWords").value);
      if (minutes) payload.minutes = minutes;
      if (wpm && !(wpm >= 80 && wpm <= 220)) {
        toast("Words per minute must be between 80 and 220.", "error");
        return;
      }
      if (wpm) payload.wpm = wpm;
      if (maxWords != null && !(maxWords >= 20 && maxWords <= 20000)) {
        toast("Max words must be between 20 and 20,000, or left blank.", "error");
        return;
      }
      if (maxWords) payload.max_words = maxWords;

      elapsedBlock("#scriptBusy", true);
      const result = await guard(button, "Writing", () =>
        API.post(url("/script"), payload)
      );
      elapsedBlock("#scriptBusy", false);
      if (!result) return;

      state.scriptDirty = false;
      state.scriptFormDirty = false;
      state.vo = null;
      await refresh();
      renderAll();
      toast(`${num(result.word_count)} words written`, "success", "Script ready");
    };

    $("#scriptSave").onclick = async (event) => {
      const text = $("#scriptText").value;
      if (!text.trim()) {
        toast("The script cannot be empty.", "error");
        return;
      }
      const doc = await guard(event.currentTarget, "Saving", () => API.patch(url(), { script: text }));
      if (!doc) return;
      state.doc = doc;
      state.scriptDirty = false;
      renderAll();
      toast("Script saved", "success");
    };

    $("#scriptCopy").onclick = () => copyText($("#scriptText").value, "Script copied");
  }

  /* --- Step 4: scenes ---------------------------------------------------- */

  function sceneStatusBadge(scene) {
    const ok = scene.status === "ok";
    const badge = el("span", `badge ${ok ? "ok" : "danger"}`, ok ? "ok" : "failed");
    return badge;
  }

  function sceneCard(scene) {
    const card = el("div", "scene-card");
    const seconds = scene.duration_sec || sceneSeconds();

    const head = el("div", "scene-head");
    // Shot, focus and the timing badge make this row long enough to wrap on a
    // narrow window; the head is not a .row so it needs the flag inline.
    head.style.flexWrap = "wrap";
    head.appendChild(el("span", "scene-idx", `#${scene.index}`));
    head.appendChild(
      el(
        "span",
        "small muted mono",
        `${timecode(scene.start_sec)} – ${timecode((scene.start_sec || 0) + seconds)}`
      )
    );
    if (scene.shot) head.appendChild(el("span", "badge", scene.shot));
    if (scene.focus) head.appendChild(el("span", "chip", scene.focus));

    // An image is held for exactly its own narration, so the interesting case
    // is a beat that drifts well off the cadence and makes the cut rhythm
    // lurch. `actual_sec` is measured from the rendered voice-over; before that
    // runs there is only the estimate.
    const target = scene.target_sec || sceneSeconds();
    if (seconds > target * 1.4) {
      const slow = el("span", "badge warn", `${seconds}s on screen`);
      slow.title =
        (scene.actual_sec != null
          ? "Measured from the rendered voice-over. "
          : "Estimated at the project pace. ") +
        `This image lingers well past the ${target}s cadence.`;
      head.appendChild(slow);
    }

    head.appendChild(el("span", "grow"));
    head.appendChild(sceneStatusBadge(scene));
    card.appendChild(head);

    const body = el("div", "scene-body");
    body.appendChild(el("div", "narration-text", scene.narration || ""));

    // A three-second beat is only about seven words. The sentence it was cut
    // from is what makes the image prompt make sense.
    const context = (scene.context || "").trim();
    if (context && context !== (scene.narration || "").trim()) {
      body.appendChild(el("div", "small muted", `Cut from the line: ${context}`));
    }

    const block = el("div", "prompt-block");
    const label = el("div", "prompt-label");
    label.appendChild(el("span", "", "Image prompt"));
    label.appendChild(el("span", "grow"));
    const button = copyButton(scene.image_prompt, "Image prompt");
    button.classList.add("btn-ghost");
    button.style.cssText = "height:22px;width:22px";
    label.appendChild(button);
    block.appendChild(label);
    block.appendChild(
      el("div", "prompt-text", scene.image_prompt || "— nothing generated for this scene —")
    );
    body.appendChild(block);

    card.appendChild(body);
    return card;
  }

  function sceneTable(list) {
    const wrap = el("div", "table-wrap");
    const table = el("table");
    const thead = el("thead");
    const headRow = el("tr");
    ["Scene", "Time", "Shot", "Narration", "Image prompt", ""].forEach((label) => {
      headRow.appendChild(el("th", "", label));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = el("tbody");
    list.forEach((scene) => {
      const row = el("tr");

      const index = el("td", "mono", `#${scene.index}`);
      row.appendChild(index);

      row.appendChild(
        el(
          "td",
          "mono muted",
          `${timecode(scene.start_sec)}–${timecode((scene.start_sec || 0) + (scene.duration_sec || sceneSeconds()))}`
        )
      );

      const shot = el("td", "small dim", scene.shot || "—");
      shot.style.maxWidth = "180px";
      row.appendChild(shot);

      const narration = el("td", "truncate", scene.narration || "");
      narration.style.maxWidth = "320px";
      narration.title = scene.narration || "";
      row.appendChild(narration);

      const image = el("td", "truncate", scene.image_prompt || "—");
      image.style.maxWidth = "380px";
      image.title = scene.image_prompt || "";
      row.appendChild(image);

      const actions = el("td");
      const copy = copyButton(scene.image_prompt, "Image prompt");
      copy.classList.add("btn-ghost");
      actions.appendChild(copy);
      row.appendChild(actions);

      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function renderSceneList() {
    const host = $("#scenesList");
    host.innerHTML = "";
    const all = scenes();
    const term = state.sceneFilter.trim().toLowerCase();
    const list = term
      ? all.filter((scene) => (scene.narration || "").toLowerCase().includes(term))
      : all;

    const count = $("#sceneFilterCount");
    count.textContent = term
      ? `${num(list.length)} of ${num(all.length)} images match`
      : `${num(all.length)} images`;

    if (!list.length) {
      host.appendChild(
        callout("info", term ? `Nothing matches “${term}”.` : "No scenes to show.", ICON.info)
      );
      return;
    }

    // The table is cheap enough to draw whole; the cards are not.
    if (state.sceneView === "table") {
      host.appendChild(sceneTable(list));
      return;
    }

    const shown = Math.min(state.sceneLimit, list.length);
    const grid = el("div", "stack tight");
    list.slice(0, shown).forEach((scene) => grid.appendChild(sceneCard(scene)));
    host.appendChild(grid);

    if (shown < list.length) {
      const more = el(
        "button",
        "btn btn-block",
        `Show more — ${num(shown)} of ${num(list.length)} shown`
      );
      more.type = "button";
      more.onclick = () => {
        state.sceneLimit += CARD_PAGE;
        renderSceneList();
      };
      host.appendChild(more);
    }
  }

  function renderScenes() {
    const doc = state.doc || {};
    const c = ctx();
    const all = scenes();
    const seconds = sceneSeconds();
    // The count is worth saying out loud before anything is committed to, so it
    // comes from the script itself and falls back to the word budget.
    const words = (doc.script_meta || {}).word_count || wordCount(doc.script);
    const plannedWords = words || budget(c.minutes, c.wpm, null).target;
    const perImage = wordsPerScene(c.wpm, seconds);
    const expected = expectedImages(plannedWords, c.wpm, seconds);

    $("#scenesExplainer").textContent =
      `One scene is one still image, held on screen for ${seconds} second${seconds === 1 ? "" : "s"}. ` +
      `At about ${num(perImage)} spoken words per image, this ${num(plannedWords)}-word script becomes ` +
      `roughly ${num(expected)} images — that is how many you will generate yourself, so change the ` +
      `cadence in step 1 first if it is more than you want.`;

    $("#sceneExpected").textContent = num(all.length || expected);
    $("#sceneExpectedSub").textContent = all.length
      ? "One image prompt each, already cut"
      : "Estimated from the script";
    $("#sceneSeconds").textContent = String(seconds);
    const covered = all.length
      ? all.reduce((sum, s) => sum + (s.duration_sec || seconds), 0)
      : expected * seconds;
    $("#sceneRuntime").textContent = formatDuration(covered);
    $("#sceneRuntimeSub").textContent = all.length ? "From the generated scenes" : "Estimated from the target length";

    setLabel("#scenesGenerate", all.length ? "Regenerate scenes" : "Generate scenes");

    const hasThumb = Boolean(doc.thumbnail_prompt);
    $("#thumbCard").classList.toggle("hidden", !hasThumb);
    $("#thumbText").textContent = doc.thumbnail_prompt || "";

    const hasNegative = Boolean(doc.negative_prompt);
    $("#negCard").classList.toggle("hidden", !hasNegative);
    $("#negText").textContent = doc.negative_prompt || "";

    const hasBible = Boolean(doc.style_bible);
    $("#bibleCard").classList.toggle("hidden", !hasBible);
    $("#bibleText").textContent = doc.style_bible || "";
    $("#bibleBody").classList.toggle("hidden", !state.bibleOpen);
    $("#bibleToggle").textContent = state.bibleOpen ? "Hide" : "Show";

    const failedHost = $("#scenesFailed");
    failedHost.innerHTML = "";
    const failed = all.filter((scene) => scene.status !== "ok").map((scene) => scene.index);
    if (failed.length) {
      const box = callout(
        "warn",
        `${failed.length} scene${failed.length === 1 ? "" : "s"} came back without a usable prompt: ${failed.map((i) => `#${i}`).join(", ")}. The narration is intact — only the image prompt is missing.`
      );
      const retry = el("button", "btn btn-sm", "Retry failed scenes");
      retry.type = "button";
      retry.style.marginTop = "8px";
      retry.onclick = async (event) => {
        const result = await guard(event.currentTarget, "Retrying", () =>
          API.post(url("/scenes/retry"))
        );
        if (!result) return;
        await refresh();
        renderAll();
        const left = (result.failed || []).length;
        if (left) toast(`${left} scenes still failed`, "error", "Retry incomplete");
        else toast("Every scene has an image prompt now", "success");
      };
      box.lastChild.appendChild(el("div"));
      box.lastChild.appendChild(retry);
      failedHost.appendChild(box);
    }

    $("#scenesToolbar").classList.toggle("hidden", all.length === 0);
    $("#viewCards").classList.toggle("btn-primary", state.sceneView === "cards");
    $("#viewTable").classList.toggle("btn-primary", state.sceneView === "table");

    const emptyHost = $("#scenesEmpty");
    if (!all.length) {
      $("#scenesList").innerHTML = "";
      emptyState(emptyHost, {
        icon: ICON.image,
        title: "No scene breakdown yet",
        body: `The script gets cut into ${seconds}-second beats — about ${num(expected)} of them at this length — and each beat gets one image prompt.`,
        actionLabel: "Generate scenes",
        onAction: () => $("#scenesGenerate").click(),
      });
      return;
    }
    emptyHost.innerHTML = "";
    emptyHost.classList.add("hidden");
    renderSceneList();
  }

  function wireScenes() {
    $("#scenesGenerate").onclick = async (event) => {
      // Captured before the confirm dialog: currentTarget is null after it.
      const button = event.currentTarget;
      if (!state.doc.script) {
        toast("Write a script first.", "error");
        return;
      }
      if (scenes().length) {
        const ok = await confirmDialog({
          title: "Rebuild the scene breakdown",
          message:
            "Every image prompt is rewritten from the current script. The voice-over is cut from the script rather than from the scenes, so the track itself is left alone.",
          confirmLabel: "Rebuild",
          danger: true,
        });
        if (!ok) return;
      }
      elapsedBlock("#scenesBusy", true);
      const result = await guard(button, "Cutting", () => API.post(url("/scenes")));
      elapsedBlock("#scenesBusy", false);
      if (!result) return;
      await refresh();
      state.sceneLimit = CARD_PAGE;
      renderAll();
      const failed = (result.failed || []).length;
      toast(
        `${num((result.scenes || []).length)} images${failed ? `, ${failed} without a prompt` : ""}`,
        failed ? "info" : "success",
        "Breakdown ready"
      );
    };

    $("#sceneSearch").addEventListener("input", (event) => {
      state.sceneFilter = event.target.value;
      state.sceneLimit = CARD_PAGE;
      renderSceneList();
    });

    $("#viewCards").onclick = () => {
      state.sceneView = "cards";
      state.sceneLimit = CARD_PAGE;
      renderScenes();
    };
    $("#viewTable").onclick = () => {
      state.sceneView = "table";
      renderScenes();
    };

    $("#thumbCopy").onclick = () =>
      copyText(state.doc.thumbnail_prompt || "", "Thumbnail prompt copied");
    $("#negCopy").onclick = () =>
      copyText(state.doc.negative_prompt || "", "Negative prompt copied");
    $("#bibleCopy").onclick = () => copyText(state.doc.style_bible || "", "Style bible copied");
    $("#bibleToggle").onclick = () => {
      state.bibleOpen = !state.bibleOpen;
      $("#bibleBody").classList.toggle("hidden", !state.bibleOpen);
      $("#bibleToggle").textContent = state.bibleOpen ? "Hide" : "Show";
    };
  }

  /* --- Step 5: voice-over ------------------------------------------------ */

  function voState() {
    if (state.vo) return state.vo;
    const doc = state.doc || {};
    const vo = doc.voiceover || { status: "idle" };
    return Object.assign({}, vo, { running: vo.status === "running" });
  }

  function currentVoice() {
    const id = $("#voiceSelect") ? $("#voiceSelect").value : "";
    return state.voices.find((voice) => voice.id === id) || null;
  }

  function renderVoicePicker() {
    const select = $("#voiceSelect");
    const detail = $("#voiceDetail");
    const warn = $("#voiceWarn");
    const emptyHost = $("#voiceEmpty");

    if (!state.voices.length) {
      $("#voiceControls").classList.add("hidden");
      $("#voGenerate").classList.add("hidden");
      emptyState(emptyHost, {
        icon: ICON.mic,
        title: "No cloned voices yet",
        body:
          "Narration uses a cloned voice from the Voice Bank. Upload a clean sample there, tune the delivery, then come back and render.",
        actionLabel: "Open the Voice Bank",
        href: "/voices",
      });
      return;
    }

    emptyHost.innerHTML = "";
    emptyHost.classList.add("hidden");
    $("#voiceControls").classList.remove("hidden");
    $("#voGenerate").classList.remove("hidden");

    const vo = voState();
    const preferred = select.value || vo.voice_id || state.voices[0].id;
    select.innerHTML = "";
    state.voices.forEach((voice) => {
      const option = el("option", "", voice.name || voice.id);
      option.value = voice.id;
      select.appendChild(option);
    });
    select.value = state.voices.some((v) => v.id === preferred) ? preferred : state.voices[0].id;

    const voice = currentVoice();
    detail.innerHTML = "";
    warn.innerHTML = "";
    if (!voice) return;

    const card = el("div", "card");
    const body = el("div", "card-body stack tight");

    const head = el("div", "row wrap");
    head.appendChild(el("div", "", voice.name || voice.id));
    if (voice.category) head.appendChild(el("span", "badge accent", voice.category));
    head.appendChild(el("span", "badge", formatBytes(voice.size_bytes)));
    head.appendChild(el("span", "badge", `speed ${voice.speed}`));
    body.appendChild(head);

    if (voice.description) body.appendChild(el("div", "small dim", voice.description));

    const block = el("div", "prompt-block");
    block.appendChild(el("div", "prompt-label", "Style prompt sent with every request"));
    block.appendChild(
      el("div", "prompt-text", voice.style_prompt || "No style prompt — the sample alone carries the delivery.")
    );
    body.appendChild(block);

    card.appendChild(body);
    detail.appendChild(card);

    if (voice.sample_warning) {
      const parts = expectedParts() || 1;
      warn.appendChild(
        callout(
          "warn",
          `This sample is ${formatBytes(voice.size_bytes)}. It is re-uploaded with every synthesis request, and this script is about ${num(parts)} request${parts === 1 ? "" : "s"} — roughly ${formatBytes((voice.size_bytes || 0) * parts)} of upload and a proportional bill. A shorter, cleaner sample usually clones just as well.`
        )
      );
    }
  }

  function renderVoStatus() {
    const host = $("#voStatus");
    host.innerHTML = "";
    const vo = voState();
    const running = vo.status === "running" || vo.running;

    const generate = $("#voGenerate");
    if (generate) generate.disabled = Boolean(running);

    if (!vo.total && !running) return;

    const card = el("div", "card");
    const head = el("div", "card-head");
    const grow = el("div", "grow");
    grow.appendChild(el("h2", "", running ? "Rendering narration" : "Last run"));
    grow.appendChild(
      el(
        "div",
        "small muted",
        `${vo.voice_name ? `Voice: ${vo.voice_name}. ` : ""}` +
          `${vo.started_at ? `Started ${relativeTime(vo.started_at)}.` : ""}` +
          `${!running && vo.elapsed_sec ? ` Took ${vo.elapsed_sec}s.` : ""}`
      )
    );
    head.appendChild(grow);
    if (running) {
      const spin = el("span", "spinner");
      head.appendChild(spin);
    } else {
      const kind =
        vo.status === "done"
          ? "ok"
          : vo.status === "partial"
          ? "warn"
          : vo.status === "error" || vo.status === "interrupted"
          ? "danger"
          : "";
      head.appendChild(el("span", `badge ${kind}`.trim(), vo.status || "idle"));
    }
    card.appendChild(head);

    const body = el("div", "card-body stack tight");

    const total = vo.total || expectedParts() || 0;
    const done = vo.done || 0;
    const failed = (vo.failed || []).length;

    const bar = el("div", "progress");
    const fill = el("div", "progress-bar");
    fill.style.width = `${total ? Math.round((done / total) * 100) : 0}%`;
    if (!running && !failed) fill.classList.add("ok");
    if (failed) fill.classList.add("warn");
    bar.appendChild(fill);
    body.appendChild(bar);

    const line = el("div", "row wrap");
    line.appendChild(el("span", "tabnum", `${num(done)} / ${num(total)} parts`));
    if (failed) line.appendChild(el("span", "badge danger", `${num(failed)} failed`));
    if (running) {
      line.appendChild(
        el("span", "small muted", "The page polls the server every 2.5 seconds.")
      );
    }
    body.appendChild(line);

    body.appendChild(
      el(
        "div",
        "small muted",
        "The script is split on sentence boundaries and each part is one request, so a ten-minute script is only a handful of them — not one per image."
      )
    );

    card.appendChild(body);
    host.appendChild(card);
  }

  function renderVoResults() {
    const host = $("#voResults");
    host.innerHTML = "";
    const vo = voState();
    if (vo.status === "running" || vo.running) return;

    if (vo.status === "error" || vo.status === "partial" || vo.status === "interrupted") {
      const errors = vo.errors || {};
      const keys = Object.keys(errors);
      const lines = vo.error
        ? vo.error
        : keys.length
        ? keys.map((index) => `part ${index}: ${errors[index]}`).join("  ·  ")
        : "Some parts of the script did not render.";
      const box = callout("danger", lines);

      if (vo.status === "partial") {
        box.lastChild.appendChild(el("div"));
        box.lastChild.appendChild(
          el(
            "div",
            "small",
            "A part that never rendered is a silent gap in the middle of a sentence, not a clean cut. The track was still written because most of it is usable, but it is not finished until every part is there."
          )
        );
      }

      // The retry endpoint rejects a request with nothing to retry, so the
      // button only appears when there really are missing parts. A job that
      // died before recording any (status "error") is restarted with the
      // generate button above instead.
      if ((vo.failed || []).length) {
        const retry = el("button", "btn btn-sm btn-danger", "Retry the missing parts");
        retry.type = "button";
        retry.style.marginTop = "8px";
        retry.onclick = async (event) => {
          const result = await guard(event.currentTarget, "Queueing", () =>
            API.post(url("/voiceover/retry"))
          );
          if (!result) return;
          state.vo = Object.assign({}, voState(), { status: "running", running: true });
          renderVoStatus();
          renderVoResults();
          startPolling();
        };
        box.lastChild.appendChild(el("div"));
        box.lastChild.appendChild(retry);
      } else {
        box.lastChild.appendChild(el("div"));
        box.lastChild.appendChild(
          el(
            "div",
            "small",
            "The job stopped before it recorded any per-part failures. Start it again above."
          )
        );
      }

      host.appendChild(box);
    }

    // The pace the model actually spoke at. The wpm setting decides how many
    // words go into one image's slot, so if the two disagree the narration will
    // not fit the image it belongs to — and the error compounds across hundreds.
    if (vo.measured_wpm) {
      const planned = Number(ctx().wpm) || DEFAULT_WPM;
      const measured = Number(vo.measured_wpm);
      const ratio = measured ? planned / measured : 1;
      const seconds = sceneSeconds();

      if (Math.abs(measured - planned) / planned > 0.1) {
        const box = callout(
          "warn",
          `This voice spoke at ${measured} words per minute, but the project is planned at ` +
            `${planned}. Every ${seconds}-second image therefore holds about ` +
            `${ratio.toFixed(2)}× the narration it has room for. Set words-per-minute to ` +
            `${measured} and regenerate the scenes so the images and the narration stay in step.`
        );
        const apply = el("button", "btn btn-sm", `Set words-per-minute to ${measured}`);
        apply.type = "button";
        apply.style.marginTop = "8px";
        apply.onclick = async (event) => {
          const result = await guard(event.currentTarget, "Saving", () =>
            API.patch(url(), { wpm: measured })
          );
          if (!result) return;
          state.doc = result;
          toast(
            `Words-per-minute is now ${measured}. Regenerate the scenes to apply it.`,
            "success"
          );
          renderAll();
        };
        box.lastChild.appendChild(el("div"));
        box.lastChild.appendChild(apply);
        host.appendChild(box);
      } else {
        host.appendChild(
          callout(
            "info",
            `This voice spoke at ${measured} words per minute, close to the ${planned} the ` +
              `project planned for. The scene timings hold.`
          )
        );
      }
    }

    // One track is the only audio there is: no per-scene files, no per-scene players.
    if (!vo.full_file) return;

    const card = el("div", "card");
    const head = el("div", "card-head");
    const grow = el("div", "grow");
    grow.appendChild(el("h2", "", "Full track"));
    grow.appendChild(
      el("div", "small muted", `${formatDuration(vo.full_sec)} · the whole script in one file`)
    );
    head.appendChild(grow);
    const link = el("a", "btn btn-sm", "Download .wav");
    link.href = url(`/audio/${encodeURIComponent(vo.full_file)}`);
    link.setAttribute("download", "");
    head.appendChild(link);
    card.appendChild(head);

    const body = el("div", "card-body");
    const audio = el("audio");
    audio.controls = true;
    audio.preload = "none";
    audio.src = url(`/audio/${encodeURIComponent(vo.full_file)}`);
    body.appendChild(audio);
    card.appendChild(body);
    host.appendChild(card);
  }

  function renderVoiceover() {
    renderVoicePicker();
    renderVoStatus();
    renderVoResults();
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function startPolling() {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(pollOnce, POLL_MS);
    pollOnce();
  }

  async function pollOnce() {
    if (state.polling) return;
    state.polling = true;
    try {
      const status = await API.get(url("/voiceover/status"));
      state.vo = status;
      renderVoStatus();
      renderVoResults();
      renderStepper();

      if (status.status !== "running" && !status.running) {
        stopPolling();
        await refresh();
        state.vo = null;
        renderAll();
        if (status.status === "done") toast("Voice-over finished", "success");
        else if (status.status === "partial") {
          toast(`${(status.failed || []).length} parts failed`, "error", "Voice-over incomplete");
        } else if (status.status === "error" || status.status === "interrupted") {
          toast(status.error || "The job stopped early", "error", "Voice-over failed");
        }
      }
    } catch (err) {
      stopPolling();
      toast(err.message, "error", "Lost the voice-over status");
    } finally {
      state.polling = false;
    }
  }

  function wireVoiceover() {
    $("#voiceSelect").addEventListener("change", renderVoicePicker);

    $("#voGenerate").onclick = async (event) => {
      // Captured before the confirm dialog: currentTarget is null after it.
      const button = event.currentTarget;
      const voice = currentVoice();
      if (!voice) {
        toast("Pick a voice first.", "error");
        return;
      }
      const parts = expectedParts();
      const ok = await confirmDialog({
        title: "Render the voice-over",
        message: `The whole script is narrated with “${voice.name}” as one track, split on sentence boundaries into about ${num(parts)} request${parts === 1 ? "" : "s"}. The ${formatBytes(voice.size_bytes)} sample is re-uploaded on every one of them.`,
        confirmLabel: "Render",
      });
      if (!ok) return;

      const result = await guard(button, "Queueing", () =>
        API.post(url("/voiceover"), { voice_id: voice.id })
      );
      if (!result) return;

      state.vo = {
        status: "running",
        running: true,
        // The 202 does not carry a count; this is our own estimate and the
        // first poll replaces it with the real number of chunks.
        total: parts,
        done: 0,
        failed: [],
        errors: {},
        voice_id: voice.id,
        voice_name: voice.name,
        started_at: new Date().toISOString(),
      };
      renderVoStatus();
      renderVoResults();
      startPolling();
      toast("Voice-over queued", "info");
    };
  }

  /* --- Render / boot ----------------------------------------------------- */

  function renderAll() {
    const doc = state.doc || {};
    const updated = $("#docUpdated");
    if (updated) updated.textContent = `updated ${relativeTime(doc.updated_at)}`;

    renderContext();
    renderTitles();
    renderScript();
    renderScenes();
    renderVoiceover();

    if (!REACHABLE[state.step]()) {
      let fallback = state.step;
      while (fallback > 1 && !REACHABLE[fallback]()) fallback -= 1;
      state.step = fallback;
      $$("[data-panel]").forEach((panel) => {
        panel.classList.toggle("hidden", Number(panel.dataset.panel) !== state.step);
      });
    }
    renderStepper();
  }

  function initialStep() {
    const match = /^#step-([1-5])$/.exec(location.hash || "");
    if (match) {
      const wanted = Number(match[1]);
      if (REACHABLE[wanted]()) return wanted;
    }
    let step = Math.min(5, Math.max(1, Number((state.doc || {}).step) || 1));
    while (step > 1 && !REACHABLE[step]()) step -= 1;
    return step;
  }

  function wireStepper() {
    const stepper = $("#stepper");
    const activate = (node) => {
      if (!node) return;
      if (node.getAttribute("aria-disabled") === "true") {
        toast(BLOCKED[Number(node.dataset.step)] || "That step is not available yet.", "info");
        return;
      }
      showStep(Number(node.dataset.step));
    };
    stepper.addEventListener("click", (event) => activate(event.target.closest(".step")));
    stepper.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        activate(event.target.closest(".step"));
      }
    });
    document.addEventListener("click", (event) => {
      const trigger = event.target.closest("[data-goto]");
      if (trigger) showStep(Number(trigger.dataset.goto));
    });
  }

  async function boot() {
    try {
      state.doc = await API.get(url());
    } catch (err) {
      $("#appLoading").innerHTML = "";
      emptyState($("#appLoading"), {
        icon: ICON.alert,
        title: "This project would not load",
        body: err.message || "The server did not return the project document.",
        actionLabel: "Back to projects",
        href: "/",
      });
      $("#appLoading").classList.remove("busy");
      return;
    }

    try {
      const payload = await API.get("/api/voices");
      state.voices = payload.voices || [];
    } catch (_) {
      state.voices = [];
      toast("Could not load the voice bank", "error");
    }

    wireStepper();
    wireContext();
    wireTitles();
    wireScript();
    wireScenes();
    wireVoiceover();

    state.step = initialStep();
    renderAll();
    showStep(state.step);

    $("#appLoading").classList.add("hidden");
    $("#appBody").classList.remove("hidden");

    const vo = (state.doc.voiceover || {}).status;
    if (vo === "running") startPolling();

    window.addEventListener("beforeunload", stopPolling);
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
