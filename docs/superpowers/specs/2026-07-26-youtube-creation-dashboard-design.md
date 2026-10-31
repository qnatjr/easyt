# YouTube Creation Dashboard — Design Spec

Date: 2026-07-26
Status: Approved, revised 2026-07-26

## Revision — image-only, faster cutting, one narration track

After the first build the scope changed on three points. The rest of the document is written to
match the code as it now stands; these are called out because they reverse earlier decisions.

1. **No video prompts.** Google Flow / Veo is out of the product entirely. Every scene now carries
   one `image_prompt` and nothing else. Rationale from the request: the workflow is stills, not
   generated video.
2. **Scenes are much shorter and configurable.** A scene is one still image held for
   `context.scene_seconds` — 2 to 10 seconds, default 3, instead of a fixed 8. A ten-minute video
   goes from 75 scenes to roughly 200. To keep prompts good at that density, two things were added:
   every beat carries the whole sentence it came from as context, and a shot type is assigned in
   code from a repeating cycle so the montage varies.
3. **The voice-over is one full track.** No per-scene audio. The script is chunked on sentence
   boundaries and concatenated, which is about six requests for a ten-minute script instead of two
   hundred.

## Problem

The project is a single 1557-line `server.py` holding a FastAPI app plus five HTML pages
embedded as Python string literals. It offers four disconnected tools (preset TTS, TTS from
file, voice cloning, a "studio" clone page with sliders) and one endpoint, `/api/faceless`,
that already produces a title/script/scene package in a single LLM call.

Nothing ties these together. A creator who wants to go from an idea to a finished
faceless-video package has to drive four separate pages by hand and stitch the results
themselves.

## Goal

One dashboard that carries an idea through the whole pipeline: context → title →
script → a shot list of image prompts → cloned voice-over. No login. No database.
Everything on disk as JSON, text, CSV, and WAV.

## Non-goals

- Authentication, multi-user, or any notion of accounts.
- A database. JSON files on disk are the store.
- Image generation and video assembly. The tool produces prompts and audio; the image generator
  and the editor do the rest.
- External APIs beyond MIMO. Title CTR scoring is done by MIMO, not the YouTube Data API.

## Decisions

These were settled with the user before the design was written.

| Question | Decision |
|---|---|
| Git | Delete `.git` entirely. No versioning, no remote. |
| Scene prompts | One `image_prompt` per scene, plus an assigned `shot` type and a `focus`. One `negative_prompt` for the whole video. |
| CTR data | MIMO only. No YouTube Data API, no new external dependency. |
| Frontend | FastAPI + Jinja2 + vanilla JS. No Node, no build step. Tailwind via CDN was agreed, but the build dropped it for a hand-written stylesheet: the requirement was "no build step", and one CSS file meets that while also working offline and keeping the templates readable. |

Scene length deserves one more note. A scene is capped at the words that fit in eight seconds, and
the cap is hard. Sentence boundaries are preferred and clause boundaries come next, but a sentence
longer than one scene is broken rather than allowed to overflow — an overrun repeated across
seventy-five scenes is a minute of drift against the clips.
| Output language | Chosen per project (Indonesian or English) for titles and script. Prompts are always English, because image and video models are markedly more accurate in English. |
| Scene duration | One still image held for 2–10 seconds, chosen per project, default 3. One CSV row is one image to generate. |
| Voice-over files | Both a full track and one file per scene. |
| Voice bank | Name, sample, the Studio sliders, and a style preset. The old `/studio` page folds into it. |

## Architecture

```
app/
  main.py            FastAPI app: mounts static, includes routers, creates data dirs
  config.py          env vars, paths, model names, tunable constants
  models.py          pydantic request/response schemas
  mimo.py            MIMO client wrapper: chat_json(), synth_clone()
  audio.py           WAV header parse/build, text chunking, PCM concat
  storage.py         project and voice JSON stores
  export.py          script.txt and prompts.csv writers
  services/
    titles.py        title ideas with CTR scores
    script.py        script generation under a minute and word budget
    scenes.py        deterministic 8s segmentation, style bible, batched prompt generation
    voiceover.py     chunked clone TTS for the whole script, background job state
  routers/
    pages.py         HTML routes
    projects.py      /api/projects/*
    voices.py        /api/voices/*, /api/style-presets
templates/
  base.html          shell: sidebar, topbar, toast host
  dashboard.html     project list, new-project form
  project.html       the five-step wizard
  voices.html        voice bank CRUD
static/
  css/app.css        design tokens and components beyond Tailwind utilities
  js/api.js          fetch wrapper, error toasts
  js/dashboard.js    js/project.js  js/voices.js
data/
  projects/<id>/project.json script.txt prompts.csv audio/*.wav
  voices/voices.json  voices/samples/<vid>.<ext>
style_presets.json   narration style presets (was styles.json)
```

Each module has one job and a narrow interface. `services/` never touches HTTP; `routers/`
never talks to MIMO directly; `storage.py` is the only module that writes project JSON.

## Pipeline

### Step 1 — Context

The user supplies a topic, a category, an output language, an optional reference channel, a
target length in minutes, a maximum word count, and a words-per-minute rate (default 150).

`minutes × wpm` gives the target word count. The maximum word count is a hard ceiling,
defaulting to 110% of the target. Both are editable.

`POST /api/projects` creates `data/projects/<id>/project.json` where `<id>` is a slug of the
topic plus a short uuid.

### Step 2 — Titles

`POST /api/projects/{id}/titles` asks MIMO for eight titles. Each carries a CTR score from 0
to 100, the hook devices it uses, its character count, and a one-line rationale. The user
picks one or edits it.

Scores are model judgment, not measured click-through. The UI says so.

### Step 3 — Script

`POST /api/projects/{id}/script` generates the script from the chosen title and the context.
The response reports the real word count and the estimated duration so the user can see
whether it landed inside the budget. If the script exceeds the maximum word count, the
service asks MIMO once to tighten it; if it still overruns, the UI shows the overrun rather
than silently truncating. Regeneration and manual editing are both available.

### Step 4 — Scenes

A scene is one still image. `scene_seconds` is the **target** cadence, 2 to 10, default three; at
three, a ten-minute video is about two hundred images.

Each image is held for exactly as long as its own line takes to say, and the start times
accumulate. This is forced by the narration being a single continuous track: fixed slots would
drift against it, a little more with every cut. Beats that would come out as one-word stubs are
folded into a neighbour, allowing up to 1.5× the target, because a stub is worse than a slightly
long beat — it buys a whole image for a single word and makes the rhythm lurch.

Asking one LLM call to produce two hundred prompts guarantees drift and truncation, so the work is
split:

1. **Deterministic segmentation.** Code, not the model, splits the script into beats of at most
   `wpm / 60 × scene_seconds` words (seven at 150 wpm and three seconds), preferring sentence
   boundaries and then clause boundaries. This guarantees full coverage of the script, an exact
   scene count, correct order, and that no beat's narration outruns the seconds its image is on
   screen. The cap is hard: a sentence longer than one beat is broken rather than allowed to
   overflow, because a fraction of a second repeated across two hundred images compounds into a
   minute of drift.

   Seven words is often half a clause, which is useless on its own as the basis for an image, so
   **every beat also carries the whole sentence it came from**. The beat decides when the image
   appears; the sentence tells the model what the moment is about.
2. **Style bible.** One MIMO call produces a global visual guide: art direction, palette,
   lighting, camera language, and fixed descriptions for recurring subjects. Without it, two
   hundred independently generated stills look like two hundred different videos.
3. **Assigned shot rhythm.** Each scene is given a shot type in code, cycling through wide,
   medium, close-up, detail, new angle, point-of-view, high angle and low angle. Models left to
   themselves reach for the same framing repeatedly; the cycle is what gives a long montage its
   rhythm, and the prompt generator is told to honour the assigned shot.
4. **Batched prompts.** Beats go to MIMO in batches of sixteen, each batch receiving the style
   bible and the tail of the previous batch, so consecutive images read as cuts within one scene
   rather than unrelated pictures. Batches run concurrently, capped at four in flight.
5. **Thumbnail and negative prompt.** The thumbnail gets its own call. One negative prompt is
   derived for the whole video from the style bible's "avoid" list plus the usual junk — global
   rather than per-scene, because consistency matters more than variety here.

A batch that fails is retried once; if it fails again its scenes are marked `failed` and the
UI offers a retry for just those. Partial results are never discarded.

### Step 5 — Voice-over

The user picks a voice from the bank. The whole script is narrated as one track: it is split on
sentence boundaries into chunks small enough for a single request, each chunk is synthesized,
and the PCM is concatenated into `voiceover_full.wav`. A ten-minute script is about six
requests.

Chunking on sentences rather than on images is what makes it sound right — the model sees whole
sentences, so the prosody carries. Cutting at every image would have meant two hundred requests
and two hundred fresh starts.

A chunk that fails after its retries leaves a silent gap mid-sentence. The track is still
written, because most of it is usable, but the status goes to `partial` and the missing piece is
named rather than hidden. Retrying re-renders only what is missing; the rendered pieces stay in
`audio/chunks/` so a retry does not redo the lot.

MIMO stores no voice ID — the base64 sample is re-sent on every request. Mitigations:
concurrency capped at three, exponential backoff on retry, a UI warning when a sample exceeds
2 MB, and the job running in the background with `GET /api/projects/{id}/voiceover/status`
reporting progress.

## Exports

`script.txt` — the plain script.

`prompts.csv`:

```
scene,start,end,duration_sec,target_sec,shot,focus,narration,image_prompt,negative_prompt
1,00:00:00.0,00:00:02.2,2.2,3,wide establishing shot,the aqueduct,"...","...","..."
2,00:00:02.2,00:00:05.6,3.4,3,medium shot,the mortar,"...","...","..."
thumbnail,,,,,,,,"...","..."
```

`duration_sec` is how long the image is on screen — exactly as long as its own line takes to say.
`target_sec` is the cadence the creator asked for, kept for comparison. Times are `HH:MM:SS.s` and
they butt up against each other, because the narration is one continuous track and anything else
would drift. Once the voice-over runs, the whole list is re-laid against the measured pace.
`negative_prompt` repeats on every row so a batch paste carries it; `shot` and `focus` are there so
the montage's rhythm is legible in a spreadsheet.

The thumbnail is the final row, with `scene` set to the literal `thumbnail`. Written with
`csv.writer` so quoting and embedded newlines are handled correctly.

`project.json` holds every piece of state: context, titles with scores, script, scenes,
voice-over job status. It is the single source of truth for a project.

## Voice bank

A sidebar destination with full CRUD. Each voice stores a name, an mp3 or wav sample, the
Studio sliders (speed, stability, similarity, style, speaker boost), a category, a free-text
description, and an optional style preset. A preview button synthesizes one test sentence.

The sliders are not numeric knobs on the MIMO API — MIMO exposes none. `build_style_prompt`
translates them into a natural-language style prompt, which is the only lever MIMO responds
to. That translation carries over from the existing code unchanged.

## Removals

Files: `main.py`, `debug_stream.py`, `audio_file.wav`, `debug_concat.wav`, `__pycache__/`,
`server.py`, `.git/`.

Endpoints and pages: `/api/speak`, `/api/speak/file`, `/api/speak/studio`, `/api/styles`, and
the pages `/`, `/file`, `/clone`, `/studio`, `/faceless`.

Carried over into the new modules: `parse_wav_header`, `build_wav`, `split_text`,
`synthesize_clone`, `build_style_prompt`, `extract_audio_bytes`, `_short_upstream_error`, and
the contents of `styles.json`.

## Error handling

MIMO failures surface as HTTP 502 with the upstream message truncated and any embedded
base64 sample stripped — the existing `_short_upstream_error` behaviour.

Invalid JSON from the model is handled in two stages: strip a wrapping code fence and retry
the parse, then ask the model once to repair its own output. A second failure is a 502.

Long-running work (scenes, voice-over) writes status into `project.json`, so a browser
refresh or a server restart never loses progress.

## Testing

pytest, with MIMO mocked at the `app.mimo` boundary.

- `split_text` and the word-budget segmenter: coverage, ordering, boundary handling,
  single oversized sentences.
- `build_wav` and PCM concat: header correctness, byte lengths.
- CSV export: column order, thumbnail row, quoting of embedded commas and newlines.
- Storage: project and voice round-trips, id slugging.
- Routes: FastAPI `TestClient` over the full API surface with MIMO mocked.
