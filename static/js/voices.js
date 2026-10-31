/* Voice Bank — cloned voices, their samples and the sliders that become a style prompt. */

/* The server caps raw uploads at 7 MB because MIMO caps the base64 string at 10 MB. */
const MAX_SAMPLE_BYTES = 7 * 1024 * 1024;
const WARN_BYTES = (window.APP_DEFAULTS && window.APP_DEFAULTS.sample_warn_bytes) || 2 * 1024 * 1024;
/* Mirrors MAX_CHUNK_CHARS on the server. Narration is split on sentence
   boundaries into requests of about this size — scene duration has no bearing
   on it, so a long script is a handful of uploads, not one per image. */
const CHUNK_CHARS = 1500;

const DEFAULT_PREVIEW_TEXT =
  "This is a preview of the cloned voice. " +
  "Notice the pacing, the tone, and how each sentence lands.";

const DEFAULTS = { speed: 0.97, stability: 45, similarity: 75, style: 9, speaker_boost: true };

const state = {
  voices: [],
  presets: [],
  editingId: null, // null = create
  file: null, // File chosen in the add/edit modal
  previewVoice: null,
  previewUrl: null,
};

/* --- Icons ---------------------------------------------------------------- */

const SVG = (paths) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;

const ICON_PLAY = SVG('<polygon points="5 3 19 12 5 21 5 3"/>');
const ICON_EDIT = SVG(
  '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>'
);
const ICON_TRASH = SVG(
  '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
);
const ICON_WARN = SVG(
  '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>'
);

/* --- Style prompt (mirrors app/services/voiceover.build_style_prompt) ------ */

function sliderSentences({ stability, style, similarity, speaker_boost, speed }) {
  const parts = [];

  if (stability < 34) parts.push("Expressive, dynamic delivery with varied intonation.");
  else if (stability <= 66) parts.push("Balanced, natural intonation.");
  else parts.push("Steady, consistent, even tone.");

  if (style < 20) parts.push("Minimal stylization, natural read.");
  else if (style <= 60) parts.push("Moderately stylized delivery.");
  else parts.push("Strongly stylized, expressive delivery.");

  if (similarity > 66) parts.push("Stay faithful to the reference speaker's timbre.");
  if (speaker_boost) parts.push("Clear, well-projected, present voice.");

  if (speed < 0.95) parts.push("Speak at a slightly slower pace.");
  else if (speed > 1.05) parts.push("Speak at a slightly faster pace.");
  else parts.push("Speak at a natural pace.");

  return parts;
}

function presetName(id) {
  const preset = state.presets.find((p) => p.id === id);
  return preset ? preset.name : "";
}

/* --- Cards ---------------------------------------------------------------- */

function voiceCard(voice) {
  const card = el("div", "card");
  const preset = presetName(voice.preset_id);
  const kind = (voice.mime || "").includes("wav") ? "WAV" : "MP3";

  const chips = [
    `Speed ${Number(voice.speed).toFixed(2)}`,
    `Stability ${Math.round(voice.stability)}`,
    `Similarity ${Math.round(voice.similarity)}`,
    `Style ${Math.round(voice.style)}`,
    `Boost ${voice.speaker_boost ? "on" : "off"}`,
    `${kind} · ${formatBytes(voice.size_bytes)}`,
  ];

  const warnBlock = voice.sample_warning
    ? `<div class="callout warn">${ICON_WARN}<div>
         This sample is ${escapeHtml(formatBytes(voice.size_bytes))}, and it is re-uploaded on every
         synthesis request. A script is split into requests of about
         ${escapeHtml(String(CHUNK_CHARS))} characters, so a 10-minute narration is roughly six of
         them. Under ${escapeHtml(formatBytes(WARN_BYTES))} is recommended.
       </div></div>`
    : "";

  card.innerHTML = `
    <div class="card-head">
      <div class="grow">
        <h3 class="truncate">${escapeHtml(voice.name)}</h3>
        <div class="small muted">Added ${escapeHtml(relativeTime(voice.created_at) || "recently")}</div>
      </div>
      ${voice.category ? `<span class="badge">${escapeHtml(voice.category)}</span>` : ""}
      ${preset ? `<span class="badge accent">${escapeHtml(preset)}</span>` : ""}
    </div>
    <div class="card-body stack tight">
      ${
        voice.description
          ? `<p class="small dim">${escapeHtml(voice.description)}</p>`
          : `<p class="small muted">No description — the prompt leans entirely on the preset and the sliders.</p>`
      }
      <div class="row wrap gap-6">
        ${chips.map((c) => `<span class="chip">${escapeHtml(c)}</span>`).join("")}
      </div>
      <audio controls preload="none" src="/api/voices/${encodeURIComponent(voice.id)}/sample"></audio>
      ${warnBlock}
    </div>
    <div class="card-foot">
      <button class="btn btn-sm" data-act="preview" type="button">${ICON_PLAY} Preview</button>
      <button class="btn btn-sm" data-act="edit" type="button">${ICON_EDIT} Edit</button>
      <span class="grow"></span>
      <button class="btn btn-sm btn-icon btn-danger" data-act="delete" type="button"
              title="Delete voice" aria-label="Delete voice">${ICON_TRASH}</button>
    </div>`;

  $('[data-act="preview"]', card).onclick = () => openPreview(voice);
  $('[data-act="edit"]', card).onclick = () => openVoiceModal(voice);
  $('[data-act="delete"]', card).onclick = (e) => removeVoice(voice, e.currentTarget);
  return card;
}

function renderVoices() {
  const grid = $("#voiceGrid");
  const empty = $("#voicesEmpty");
  grid.textContent = "";

  const has = state.voices.length > 0;
  grid.classList.toggle("hidden", !has);
  empty.classList.toggle("hidden", has);
  if (has) state.voices.forEach((voice) => grid.appendChild(voiceCard(voice)));

  const count = $("#navVoiceCount");
  if (count) {
    count.textContent = state.voices.length;
    count.classList.toggle("hidden", !has);
  }
}

async function loadVoices() {
  try {
    const data = await API.get("/api/voices");
    state.voices = data.voices || [];
    renderVoices();
  } catch (err) {
    toast(err.message, "error", "Could not load voices");
    $("#voicesEmpty").classList.remove("hidden");
  } finally {
    $("#voicesLoading").classList.add("hidden");
  }
}

async function loadPresets() {
  try {
    const data = await API.get("/api/style-presets");
    state.presets = data.presets || [];
  } catch (err) {
    state.presets = [];
    toast(err.message, "error", "Could not load style presets");
  }
  const select = $("#vPreset");
  state.presets.forEach((preset) => {
    const option = el("option", "", preset.name);
    option.value = preset.id;
    select.appendChild(option);
  });
}

/* --- Add / edit modal ----------------------------------------------------- */

function readForm() {
  return {
    name: $("#vName").value.trim(),
    category: $("#vCategory").value.trim(),
    description: $("#vDescription").value.trim(),
    preset_id: $("#vPreset").value,
    speed: Number($("#vSpeed").value),
    stability: Number($("#vStability").value),
    similarity: Number($("#vSimilarity").value),
    style: Number($("#vStyle").value),
    speaker_boost: $("#vBoost").checked,
  };
}

function paintSliders() {
  const form = readForm();
  $("#vSpeedVal").textContent = form.speed.toFixed(2);
  $("#vStabilityVal").textContent = String(form.stability);
  $("#vSimilarityVal").textContent = String(form.similarity);
  $("#vStyleVal").textContent = String(form.style);

  const preset = state.presets.find((p) => p.id === form.preset_id);
  $("#vPresetPrompt").textContent = preset ? preset.prompt : "";

  const prefix = [];
  if (preset) prefix.push(`the "${preset.name}" preset prompt`);
  if (form.category) prefix.push("your category");
  if (form.description) prefix.push("your description");

  const note = $("#vPrefixNote");
  if (prefix.length) {
    const list =
      prefix.length === 1
        ? prefix[0]
        : `${prefix.slice(0, -1).join(", ")} and ${prefix[prefix.length - 1]}`;
    note.textContent = `Prepended verbatim, in order: ${list}. Then:`;
    note.classList.remove("hidden");
  } else {
    note.classList.add("hidden");
  }

  $("#vStylePrompt").textContent = sliderSentences(form).join(" ");
}

function setSampleLabel(file) {
  const warn = $("#vSampleWarn");
  if (!file) {
    $("#vDropTitle").textContent = "Drop an MP3 or WAV here, or click to browse";
    $("#vDropHint").textContent = `Up to ${formatBytes(MAX_SAMPLE_BYTES)}. Under ${formatBytes(WARN_BYTES)} keeps a long project cheap.`;
    warn.classList.add("hidden");
    return;
  }
  $("#vDropTitle").textContent = file.name;
  $("#vDropHint").textContent = `${formatBytes(file.size)} — click to choose a different file`;
  if (file.size > WARN_BYTES) {
    $("#vSampleWarnText").textContent =
      `${formatBytes(file.size)} is above the ${formatBytes(WARN_BYTES)} guideline. The sample is ` +
      `re-uploaded on every synthesis request, and a long script becomes several requests, so ` +
      `this file goes up once for each of them.`;
    warn.classList.remove("hidden");
  } else {
    warn.classList.add("hidden");
  }
}

function acceptFile(file) {
  if (!file) return;
  const name = (file.name || "").toLowerCase();
  const type = (file.type || "").toLowerCase();
  const ok =
    name.endsWith(".mp3") ||
    name.endsWith(".wav") ||
    ["audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/wave"].includes(type);

  if (!ok) {
    toast(`${file.name} is not an MP3 or WAV file.`, "error", "Unsupported sample");
    return;
  }
  if (file.size > MAX_SAMPLE_BYTES) {
    toast(
      `${formatBytes(file.size)} exceeds the ${formatBytes(MAX_SAMPLE_BYTES)} upload limit.`,
      "error",
      "Sample too large"
    );
    return;
  }
  state.file = file;
  setSampleLabel(file);
}

function openVoiceModal(voice) {
  state.editingId = voice ? voice.id : null;
  state.file = null;
  $("#vFile").value = "";

  const values = voice || DEFAULTS;
  $("#voiceModalTitle").textContent = voice ? "Edit voice" : "Add voice";
  $("#vSave").textContent = voice ? "Save changes" : "Add voice";
  $("#vSampleReq").textContent = voice ? "optional — replaces the current sample" : "required";

  $("#vName").value = voice ? voice.name : "";
  $("#vCategory").value = voice ? voice.category || "" : "";
  $("#vDescription").value = voice ? voice.description || "" : "";
  $("#vPreset").value = voice ? voice.preset_id || "" : "";
  $("#vSpeed").value = values.speed;
  $("#vStability").value = values.stability;
  $("#vSimilarity").value = values.similarity;
  $("#vStyle").value = values.style;
  $("#vBoost").checked = values.speaker_boost !== false;

  setSampleLabel(null);
  paintSliders();
  $("#voiceModal").classList.remove("hidden");
  $("#vName").focus();
}

function closeVoiceModal() {
  $("#voiceModal").classList.add("hidden");
  state.file = null;
  state.editingId = null;
}

async function saveVoice(button) {
  const form = readForm();
  if (!form.name) {
    toast("Give the voice a name first.", "error");
    $("#vName").focus();
    return;
  }
  if (!state.editingId && !state.file) {
    toast("A voice sample is required to clone a new voice.", "error");
    return;
  }

  busy(button, true, state.editingId ? "Saving" : "Uploading");
  try {
    if (state.editingId) {
      await API.patch(`/api/voices/${encodeURIComponent(state.editingId)}`, form);
      if (state.file) {
        const fd = new FormData();
        fd.append("file", state.file);
        await API.form(`/api/voices/${encodeURIComponent(state.editingId)}/sample`, fd);
      }
      toast(`${form.name} updated.`, "success");
    } else {
      const fd = new FormData();
      fd.append("file", state.file);
      fd.append("name", form.name);
      fd.append("speed", String(form.speed));
      fd.append("stability", String(form.stability));
      fd.append("similarity", String(form.similarity));
      fd.append("style", String(form.style));
      fd.append("speaker_boost", form.speaker_boost ? "true" : "false");
      fd.append("category", form.category);
      fd.append("description", form.description);
      fd.append("preset_id", form.preset_id);
      await API.form("/api/voices", fd);
      toast(`${form.name} is ready to use.`, "success", "Voice added");
    }
    closeVoiceModal();
    await loadVoices();
  } catch (err) {
    toast(err.message, "error", state.editingId ? "Save failed" : "Upload failed");
  } finally {
    busy(button, false);
  }
}

/* --- Delete --------------------------------------------------------------- */

async function removeVoice(voice, button) {
  const ok = await confirmDialog({
    title: `Delete ${voice.name}?`,
    message:
      "The sample file is removed from disk and cannot be recovered. Voiceovers already generated " +
      "with this voice stay where they are, but you will not be able to synthesize with it again.",
    confirmLabel: "Delete voice",
    danger: true,
  });
  if (!ok) return;

  busy(button, true);
  try {
    await API.del(`/api/voices/${encodeURIComponent(voice.id)}`);
    toast(`${voice.name} deleted.`, "success");
    await loadVoices();
  } catch (err) {
    toast(err.message, "error", "Delete failed");
    busy(button, false);
  }
}

/* --- Preview -------------------------------------------------------------- */

function releasePreviewUrl() {
  if (state.previewUrl) {
    URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = null;
  }
}

function openPreview(voice) {
  state.previewVoice = voice;
  $("#pvTitle").textContent = `Preview ${voice.name}`;
  $("#pvText").value = DEFAULT_PREVIEW_TEXT;
  releasePreviewUrl();
  $("#pvResult").textContent = "";
  $("#previewModal").classList.remove("hidden");
}

function closePreview() {
  $("#previewModal").classList.add("hidden");
  const audio = $("#pvResult audio");
  if (audio) audio.pause();
  $("#pvResult").textContent = "";
  releasePreviewUrl();
  state.previewVoice = null;
}

async function synthesizePreview(button) {
  const voice = state.previewVoice;
  if (!voice) return;
  const text = $("#pvText").value.trim();
  if (!text) {
    toast("Type a line to synthesize.", "error");
    return;
  }

  busy(button, true, "Calling the cloning model…");
  try {
    const blob = await API.blob(`/api/voices/${encodeURIComponent(voice.id)}/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    releasePreviewUrl();
    state.previewUrl = URL.createObjectURL(blob);

    const result = $("#pvResult");
    result.textContent = "";
    const audio = el("audio");
    audio.controls = true;
    audio.autoplay = true;
    audio.src = state.previewUrl;
    result.appendChild(audio);
  } catch (err) {
    toast(err.message, "error", "Preview failed");
  } finally {
    busy(button, false);
  }
}

/* --- Wiring --------------------------------------------------------------- */

function wireDropzone() {
  const drop = $("#vDrop");
  const input = $("#vFile");

  drop.onclick = () => input.click();
  input.onchange = () => acceptFile(input.files && input.files[0]);

  ["dragenter", "dragover"].forEach((name) =>
    drop.addEventListener(name, (e) => {
      e.preventDefault();
      drop.classList.add("over");
    })
  );
  ["dragleave", "dragend"].forEach((name) =>
    drop.addEventListener(name, () => drop.classList.remove("over"))
  );
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    acceptFile(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]);
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  $("#addVoice").onclick = () => openVoiceModal(null);
  $("#emptyAddVoice").onclick = () => openVoiceModal(null);
  $("#voiceModalClose").onclick = closeVoiceModal;
  $("#vCancel").onclick = closeVoiceModal;
  $("#vSave").onclick = (e) => saveVoice(e.currentTarget);
  $("#voiceModal").onclick = (e) => {
    if (e.target === $("#voiceModal")) closeVoiceModal();
  };

  ["#vSpeed", "#vStability", "#vSimilarity", "#vStyle"].forEach((sel) => {
    $(sel).addEventListener("input", paintSliders);
  });
  $("#vBoost").addEventListener("change", paintSliders);
  $("#vPreset").addEventListener("change", paintSliders);
  $("#vCategory").addEventListener("input", paintSliders);
  $("#vDescription").addEventListener("input", paintSliders);

  $("#pvClose").onclick = closePreview;
  $("#pvCancel").onclick = closePreview;
  $("#pvGo").onclick = (e) => synthesizePreview(e.currentTarget);
  $("#previewModal").onclick = (e) => {
    if (e.target === $("#previewModal")) closePreview();
  };

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#previewModal").classList.contains("hidden")) closePreview();
    else if (!$("#voiceModal").classList.contains("hidden")) closeVoiceModal();
  });

  wireDropzone();
  await loadPresets();
  await loadVoices();
});
