/* Niche Bank: assemble a reference, analyse it once, reuse the profile. */

const state = {
  niches: [],
  current: null,
  poll: null,
};

const PROFILE_LABELS = {
  hook_patterns: "How it hooks",
  tone: "Narration tone",
  pacing: "Pacing",
  vocabulary: "Words it returns to",
  structure: "How it is ordered",
  title_style: "How it packages a title",
  visual_style: "Visual style",
  shot_language: "Framing habits",
  avoid: "What it never does",
  image_prompt_seed: "Art direction",
};

const STATUS_TEXT = {
  idle: "Not analysed yet.",
  running: "Analysing…",
  done: "Analysed.",
  partial: "Analysed, with gaps.",
  failed: "The last analysis failed.",
  interrupted: "The server restarted mid-analysis.",
};

/* --- Listing ------------------------------------------------------------- */

async function loadNiches() {
  try {
    const data = await API.get("/api/niches");
    state.niches = data.niches || [];
  } catch (e) {
    toast(e.message, "error", "Could not load niches");
    state.niches = [];
  }
  renderList();
}

function renderList() {
  const grid = $("#nicheGrid");
  const empty = $("#nichesEmpty");
  $("#nichesLoading").classList.add("hidden");
  grid.innerHTML = "";

  const count = $("#navNicheCount");
  if (count) {
    count.textContent = state.niches.length;
    count.classList.toggle("hidden", state.niches.length === 0);
  }

  if (!state.niches.length) {
    grid.classList.add("hidden");
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  grid.classList.remove("hidden");

  state.niches.forEach((niche) => {
    const card = el("div", "card niche-card");
    const bits = [];
    if (niche.has_video) bits.push("video");
    if (niche.shot_count) bits.push(`${niche.shot_count} shot${niche.shot_count > 1 ? "s" : ""}`);
    if (niche.has_transcript) bits.push("transcript");

    card.innerHTML = `
      <div class="row">
        <h3 class="grow truncate"></h3>
        <span class="pill" data-status="${escapeHtml(niche.status)}"></span>
      </div>
      <p class="dim small niche-bits"></p>
      <p class="dim small niche-when"></p>`;
    card.querySelector("h3").textContent = niche.name || "Untitled";
    card.querySelector(".pill").textContent = niche.status;
    card.querySelector(".niche-bits").textContent = bits.join(" · ") || "nothing added yet";
    card.querySelector(".niche-when").textContent = relativeTime(niche.updated_at);
    card.addEventListener("click", () => openDetail(niche.id));
    grid.appendChild(card);
  });
}

/* --- Detail -------------------------------------------------------------- */

async function openDetail(nicheId) {
  try {
    state.current = await API.get(`/api/niches/${nicheId}`);
  } catch (e) {
    toast(e.message, "error", "Could not open that niche");
    return;
  }
  fillDetail();
  $("#detailModal").classList.remove("hidden");
  startPolling();
}

function fillDetail() {
  const doc = state.current;
  if (!doc) return;
  const source = doc.source || {};

  $("#dTitle").textContent = doc.name || "Niche";
  $("#dName").value = doc.name || "";
  $("#dUrl").value = source.youtube_url || "";
  $("#dTranscript").value = doc.transcript || "";
  $("#dNotes").value = doc.notes || "";

  const haveVideo = Boolean(source.video_file);
  $("#dVideoHave").classList.toggle("hidden", !haveVideo);
  $("#dVideoDrop").classList.toggle("hidden", haveVideo);
  if (haveVideo) {
    const parts = [source.title || "Reference video"];
    if (source.channel) parts.push(source.channel);
    if (source.duration_sec) parts.push(formatDuration(source.duration_sec));
    $("#dVideoInfo").textContent = parts.join(" · ");
  }

  renderShots();
  renderStatus();
  renderProfile();
}

function renderShots() {
  const doc = state.current;
  const strip = $("#dShots");
  const shots = doc.shots || [];
  strip.innerHTML = "";
  $("#dShotCount").textContent = `${shots.length} of 12`;

  shots.forEach((shot) => {
    const item = el("div", "shot");
    const img = el("img");
    img.src = `/api/niches/${doc.id}/shots/${shot.filename}`;
    img.alt = "";
    const remove = el("button", "btn btn-ghost btn-icon btn-sm shot-remove");
    remove.type = "button";
    remove.textContent = "×";
    remove.title = "Remove";
    remove.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        state.current = await API.del(`/api/niches/${doc.id}/shots/${shot.filename}`);
        renderShots();
      } catch (e) {
        toast(e.message, "error", "Could not remove that screenshot");
      }
    });
    item.appendChild(img);
    item.appendChild(remove);
    strip.appendChild(item);
  });
}

function renderStatus() {
  const analysis = (state.current && state.current.analysis) || { status: "idle" };
  const text = STATUS_TEXT[analysis.status] || analysis.status;
  const extras = [];
  if (analysis.error) extras.push(analysis.error);
  if (analysis.failed_ranges && analysis.failed_ranges.length) {
    extras.push(`No reading for: ${analysis.failed_ranges.join(", ")}`);
  }
  $("#dStatusText").textContent = extras.length ? `${text} ${extras.join(" ")}` : text;
  $("#dStatus").className = `callout ${
    analysis.status === "failed" ? "warn" : analysis.status === "done" ? "info" : ""
  }`;
}

function renderProfile() {
  const host = $("#dProfile");
  const profile = (state.current && state.current.profile) || {};
  host.innerHTML = "";

  const rows = Object.entries(PROFILE_LABELS)
    .map(([key, label]) => {
      const raw = profile[key];
      const value = Array.isArray(raw) ? raw.join("; ") : raw || "";
      return value ? [label, value] : null;
    })
    .filter(Boolean);

  if (!rows.length) {
    host.innerHTML =
      '<p class="dim small">The profile appears here once an analysis finishes. This is the only part that travels to a project.</p>';
    return;
  }

  const card = el("div", "card stack");
  card.appendChild(el("h3", "", "Profile"));
  rows.forEach(([label, value]) => {
    const row = el("div", "profile-row");
    row.appendChild(el("div", "label", label));
    row.appendChild(el("div", "dim small", value));
    card.appendChild(row);
  });
  host.appendChild(card);
}

/* --- Saving and analysing ------------------------------------------------ */

async function saveDetail() {
  const doc = state.current;
  if (!doc) return;
  const button = $("#dSave");
  busy(button, true);
  try {
    state.current = await API.patch(`/api/niches/${doc.id}`, {
      name: $("#dName").value.trim() || doc.name,
      notes: $("#dNotes").value,
      transcript: $("#dTranscript").value,
      youtube_url: $("#dUrl").value.trim(),
    });
    fillDetail();
    await loadNiches();
    toast("Saved", "success");
  } catch (e) {
    toast(e.message, "error", "Could not save");
  } finally {
    busy(button, false);
  }
}

async function analyse() {
  const doc = state.current;
  if (!doc) return;
  const button = $("#dAnalyse");
  busy(button, true, "Queuing");
  try {
    // Save first: analysing what is on screen rather than what was last
    // persisted is what anyone pressing this button expects.
    await saveDetailQuietly();
    await API.post(`/api/niches/${doc.id}/analyse`);
    toast("Analysis started", "info");
    startPolling();
  } catch (e) {
    toast(e.message, "error", "Could not start the analysis");
  } finally {
    busy(button, false);
  }
}

async function saveDetailQuietly() {
  const doc = state.current;
  state.current = await API.patch(`/api/niches/${doc.id}`, {
    name: $("#dName").value.trim() || doc.name,
    notes: $("#dNotes").value,
    transcript: $("#dTranscript").value,
    youtube_url: $("#dUrl").value.trim(),
  });
}

function startPolling() {
  stopPolling();
  state.poll = setInterval(async () => {
    const doc = state.current;
    if (!doc) return stopPolling();
    let status;
    try {
      status = await API.get(`/api/niches/${doc.id}/status`);
    } catch (_) {
      return stopPolling();
    }
    const wasRunning = (doc.analysis || {}).status === "running";
    if (status.running || status.status === "running") {
      state.current = { ...doc, analysis: status };
      renderStatus();
      return;
    }
    stopPolling();
    if (wasRunning || status.status !== (doc.analysis || {}).status) {
      state.current = await API.get(`/api/niches/${doc.id}`);
      fillDetail();
      await loadNiches();
    }
  }, 3000);
}

function stopPolling() {
  if (state.poll) clearInterval(state.poll);
  state.poll = null;
}

/* --- Uploads ------------------------------------------------------------- */

async function uploadShots(files) {
  const doc = state.current;
  if (!doc || !files.length) return;
  const form = new FormData();
  Array.from(files).forEach((file) => form.append("files", file, file.name || "pasted.png"));
  try {
    state.current = await API.form(`/api/niches/${doc.id}/shots`, form);
    renderShots();
    await loadNiches();
    toast(`${files.length} screenshot${files.length > 1 ? "s" : ""} added`, "success");
  } catch (e) {
    toast(e.message, "error", "Could not add those screenshots");
  }
}

async function uploadVideo(file) {
  const doc = state.current;
  if (!doc || !file) return;
  const form = new FormData();
  form.append("file", file, file.name);
  toast("Uploading video…", "info");
  try {
    state.current = await API.form(`/api/niches/${doc.id}/video`, form);
    fillDetail();
    await loadNiches();
    toast("Video uploaded", "success");
  } catch (e) {
    toast(e.message, "error", "Could not upload that video");
  }
}

/* Pasting is the point of this zone: a screenshot on the clipboard arrives as
   an image item on the paste event, with no file on disk anywhere. Several can
   arrive in one paste, so every image item is taken, not just the first. */
function imagesFromClipboard(event) {
  const items = Array.from((event.clipboardData || {}).items || []);
  return items
    .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
    .map((item) => item.getAsFile())
    .filter(Boolean);
}

/* --- Wiring -------------------------------------------------------------- */

function openNew() {
  $("#nName").value = "";
  $("#newModal").classList.remove("hidden");
  $("#nName").focus();
}

async function createNiche() {
  const name = $("#nName").value.trim();
  if (!name) {
    toast("Give the niche a name", "error");
    return;
  }
  const button = $("#newSave");
  busy(button, true);
  try {
    const doc = await API.post("/api/niches", { name });
    $("#newModal").classList.add("hidden");
    await loadNiches();
    openDetail(doc.id);
  } catch (e) {
    toast(e.message, "error", "Could not create that niche");
  } finally {
    busy(button, false);
  }
}

function closeDetail() {
  stopPolling();
  state.current = null;
  $("#detailModal").classList.add("hidden");
}

document.addEventListener("DOMContentLoaded", () => {
  loadNiches();

  $("#addNiche").addEventListener("click", openNew);
  $("#emptyAddNiche").addEventListener("click", openNew);
  $("#newClose").addEventListener("click", () => $("#newModal").classList.add("hidden"));
  $("#newCancel").addEventListener("click", () => $("#newModal").classList.add("hidden"));
  $("#newSave").addEventListener("click", createNiche);
  $("#nName").addEventListener("keydown", (e) => {
    if (e.key === "Enter") createNiche();
  });

  $("#dClose").addEventListener("click", closeDetail);
  $("#dSave").addEventListener("click", saveDetail);
  $("#dAnalyse").addEventListener("click", analyse);

  $("#dDelete").addEventListener("click", async () => {
    const doc = state.current;
    if (!doc) return;
    const ok = await confirmDialog({
      title: "Delete this niche?",
      message: `"${doc.name}" and everything in it — screenshots, video, transcript and profile — will be removed. Projects already using it keep their own copy of the profile.`,
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      await API.del(`/api/niches/${doc.id}`);
      closeDetail();
      await loadNiches();
      toast("Niche deleted", "success");
    } catch (e) {
      toast(e.message, "error", "Could not delete that niche");
    }
  });

  const shotDrop = $("#dShotDrop");
  const shotInput = $("#dShotFiles");
  shotDrop.addEventListener("click", () => shotInput.click());
  shotInput.addEventListener("change", () => {
    uploadShots(shotInput.files);
    shotInput.value = "";
  });
  shotDrop.addEventListener("dragover", (e) => {
    e.preventDefault();
    shotDrop.classList.add("over");
  });
  shotDrop.addEventListener("dragleave", () => shotDrop.classList.remove("over"));
  shotDrop.addEventListener("drop", (e) => {
    e.preventDefault();
    shotDrop.classList.remove("over");
    uploadShots(e.dataTransfer.files);
  });

  // Paste anywhere while the detail is open, as long as the caret is not in a
  // text field — pasting a transcript must not be intercepted as an image.
  document.addEventListener("paste", (e) => {
    if (!state.current || $("#detailModal").classList.contains("hidden")) return;
    const tag = (document.activeElement || {}).tagName;
    if (tag === "TEXTAREA" || tag === "INPUT") return;
    const images = imagesFromClipboard(e);
    if (!images.length) return;
    e.preventDefault();
    uploadShots(images);
  });

  const videoDrop = $("#dVideoDrop");
  const videoInput = $("#dVideoFile");
  videoDrop.addEventListener("click", () => videoInput.click());
  videoInput.addEventListener("change", () => {
    uploadVideo(videoInput.files[0]);
    videoInput.value = "";
  });
  videoDrop.addEventListener("dragover", (e) => {
    e.preventDefault();
    videoDrop.classList.add("over");
  });
  videoDrop.addEventListener("dragleave", () => videoDrop.classList.remove("over"));
  videoDrop.addEventListener("drop", (e) => {
    e.preventDefault();
    videoDrop.classList.remove("over");
    uploadVideo(e.dataTransfer.files[0]);
  });

  $("#dVideoRemove").addEventListener("click", async () => {
    const doc = state.current;
    if (!doc) return;
    try {
      state.current = await API.del(`/api/niches/${doc.id}/video`);
      fillDetail();
      await loadNiches();
    } catch (e) {
      toast(e.message, "error", "Could not remove that video");
    }
  });
});
