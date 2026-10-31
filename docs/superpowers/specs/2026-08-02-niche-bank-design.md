# Niche Bank — Design Spec

Date: 2026-08-02
Status: Approved

## Problem

A project's only reference today is `context.reference`: a single free-text string the creator
types, threaded into two prompts (`titles.py` and `script.py`) as one line of "echo this channel".
It carries almost nothing. A creator who wants the script and the image prompts to actually match
a reference video has no way to say what that video sounds like or looks like beyond a sentence.

## Goal

A third workspace, the Niche Bank, where a reference is assembled once — YouTube link, transcript,
screenshots, video — analysed into a compact profile, and then reused across many projects so the
script and the image prompts land close to the reference.

## Non-goals

- Frame extraction into screenshots. Visual sampling comes from video clips; screenshots stay a
  manual paste.
- Fetching YouTube captions or subtitle tracks.
- Re-analysing a niche automatically when it changes after a project already used it.
- Any notion of accounts, or a database. Unchanged from the original design.

## Reversal of an earlier non-goal

The 2026-07-26 spec listed "External APIs beyond MIMO" as a non-goal. This feature reverses that:
the Niche Bank downloads from YouTube via `yt-dlp`. The reversal is deliberate and was requested
directly. Its cost is recorded under Risks below.

## Measured capability of the MIMO API

Everything in this section was established by probing the live API, not from documentation. It is
recorded because the whole design rests on it.

| Model | Text | Image | Audio | Video |
|---|---|---|---|---|
| `mimo-v2.5-pro` (the app's current `TEXT_MODEL`) | yes | **no** — `404 No endpoints found that support image input` | — | — |
| `mimo-v2.5` | yes | yes | yes | yes |
| `mimo-v2.5-asr` | — | — | wav/mp3, audio-only content parts | no |

Detail that shaped the design:

- **`mimo-v2.5` reads video via a `video_url` content part**, and reads both the frames and the
  audio track. A 6-second clip of red → green → blue with speech over it came back with the colours
  in order and the speech transcribed. Usage reports the two as separate buckets: `video_tokens=480`,
  `audio_tokens=37`.
- **Duration is capped between 50 and 55 seconds.** 50s is accepted; 55s and 59s are rejected. The
  rejection message is `Multimodal data is corrupted or cannot be processed`, which is misleading —
  the file is fine.
- **There is no meaningful payload cap.** A 30s clip at 3.75 MB base64 was accepted while a 120s
  clip at 0.37 MB was rejected. The limit is duration alone.
- **Video token cost scales with resolution, not duration**: roughly 80 tokens/s at 240p,
  220 tokens/s at 360p, 276 tokens/s at 720p.
- **Images are limited to bmp/gif/png/jpeg/webp.** The allow-list is quoted from the API's own
  error message.
- `mimo-v2.5-asr` exists but is not used: `mimo-v2.5` already transcribes the audio inside an mp4,
  so no separate transcription step and no mp3 conversion are needed.

End-to-end proof: a real 19-second YouTube download went to `mimo-v2.5` in one call and returned a
verbatim transcript (disfluencies included), an accurate description of the clothing and setting,
per-shot notes, and a reusable art-direction line — for 2,410 total tokens.

## Decisions

Settled with the user before this document was written.

| Question | Decision |
|---|---|
| What the screenshots are for | Frames and thumbnails of the reference. The model must see them, so they go through `mimo-v2.5`. |
| Video upload | Transcribed and visually analysed by `mimo-v2.5` directly. No ASR model, no mp3 conversion. |
| The YouTube link | A download source, not a note. `yt-dlp` fetches the video. |
| A long reference video | Sampled: four 45-second clips at 0%, 25%, 50%, 75%, merged into one profile. |
| How a niche reaches a project | Chosen at project creation; its profile replaces the `reference` string in the prompts. |

## The core idea: the profile is injected, never the transcript

A ten-minute transcript is around 1,500 words. The script prompt already carries its own word
budget (`script.py:resolve_budget`), and dropping a raw transcript beside those instructions would
drown them and push the script off its target length. Raw video is worse.

So analysis compresses every input into one small profile, and only that profile reaches a project's
prompts:

```json
{
  "hook_patterns": [],
  "tone": "",
  "pacing": "",
  "vocabulary": [],
  "structure": [],
  "title_style": "",
  "visual_style": "",
  "shot_language": [],
  "avoid": [],
  "image_prompt_seed": ""
}
```

The full transcript is stored and readable in the UI, but never sent to the project pipeline.

Each consumer receives only the fields it can use:

| Module | Existing injection point | Profile fields |
|---|---|---|
| `services/titles.py` | `build_prompt`, the `ref_line` | `title_style`, `hook_patterns` |
| `services/script.py` | `build_prompt`, the `ref_line` | `tone`, `pacing`, `vocabulary`, `structure`, `hook_patterns` |
| `services/scenes.py` | `build_bible_prompt` | `visual_style`, `shot_language`, `image_prompt_seed`, `avoid` |

All three already read `ctx["reference"]`, so the injection points exist. A project with no niche
keeps using the free-text `reference` exactly as it does today; nothing about the current behaviour
changes.

## Storage

A niche owns files, so it gets a directory like a project rather than a row in a flat JSON map like
a voice.

```
data/niches/<niche-id>/
  niche.json          # source of truth
  shots/              # pasted screenshots
  video/
    source.mp4        # the download, kept
    clips/            # the <=45s excerpts actually sent to the model
```

`niche.json`:

```json
{
  "id": "cold-open-history-a1b2c3",
  "name": "Cold-open history",
  "created_at": "", "updated_at": "",
  "source": {
    "youtube_url": "", "title": "", "channel": "", "duration_sec": 0,
    "video_file": "", "downloaded_at": ""
  },
  "transcript": "",
  "shots": [{"filename": "", "note": ""}],
  "notes": "",
  "analysis": {"status": "idle"},
  "profile": {}
}
```

`analysis.status` is one of `idle`, `running`, `done`, `partial`, `failed`, `interrupted` — the same
vocabulary the voice-over job already uses.

## Analysis pipeline

A background job, mirroring the voice-over job in `routers/projects.py:248-297` including its
restart correction at line 269: a status of `running` with no live job is rewritten to `interrupted`
on read, so a server restart cannot leave the browser polling forever.

1. If there is a `youtube_url` and no local video, download it with `yt-dlp`, preferring a
   progressive mp4 at 360p or below. That is what format 18 provides, it needs no stream merging,
   and it keeps the token cost at the measured 220/s rather than 276/s.
2. Cut clips with ffmpeg: up to `NICHE_CLIP_COUNT` (4) clips of `NICHE_CLIP_SECONDS` (45) spread
   evenly across the duration — at 0%, 25%, 50% and 75% when four are taken. Clips are never
   allowed to exceed 45s, so the gateway's 50-55s wall is never reached.

   The count is `min(NICHE_CLIP_COUNT, duration // NICHE_CLIP_SECONDS)`, floored at one. Without
   that floor division a two-minute video would be sampled at 0s, 30s, 60s and 90s, and four
   45-second clips taken from those offsets would overlap heavily — the creator would pay four
   times to analyse largely the same footage. So a 60-second video yields one clip, a three-minute
   video yields four contiguous ones, and a ten-minute video yields four spread across it. A video
   shorter than a single clip is used whole.
3. Each clip is one `mimo-v2.5` call returning a partial profile and its slice of the transcript.
4. All screenshots go in one further call, as several image parts in a single message.
5. A final merge call reconciles the partials — plus any transcript the creator pasted by hand —
   into the single profile above.

Concurrency is capped at 4 in flight, matching `SCENE_BATCH_CONCURRENCY`.

Cost, from the measured figures: a ten-minute reference is 4 clips x 45s at 360p, roughly 40,000
tokens, paid once per niche. Every project that later uses it pays only for the compact profile,
around 300 tokens.

## Modules

| File | Change |
|---|---|
| `app/config.py` | `VISION_MODEL = "mimo-v2.5"`, `NICHES_DIR`, `NICHE_CLIP_SECONDS = 45`, `NICHE_CLIP_COUNT = 4`, `ALLOWED_IMAGE_TYPES`, size caps |
| `app/mimo.py` | Extract the JSON-repair loop into its own function; add `chat_json_parts(system, parts, model=...)` taking text/image/video parts. The existing text-only path keeps its current behaviour |
| `app/media.py` | New. `yt-dlp` and ffmpeg wrappers, isolated so the whole of it can be mocked in tests |
| `app/services/niche.py` | New. Clip planning, per-clip analysis, profile merge |
| `app/storage.py` | A fourth store section, following the three already there |
| `app/routers/niches.py` | New. CRUD, uploads, analyse, status |
| `app/models.py` | `NicheCreate`, `NichePatch`; `niche_id` on `ProjectCreate` and `ProjectPatch` |
| `templates/niches.html`, `static/js/niches.js`, `templates/base.html` | The workspace and its nav entry |

`app/mimo.py` grows from 148 to roughly 220 lines; `app/storage.py` from 241 to roughly 330. Both
stay within their current responsibility. Putting the niche store in `storage.py` keeps the
README's claim that one module owns the file writes.

## Dependencies

Two, both through `requirements.txt`, neither needing Homebrew:

- `yt-dlp` — the downloader.
- `imageio-ffmpeg` — ships a static ffmpeg binary. Verified working on the target machine.

Both are optional at runtime. If either import fails, the YouTube and video features return a clear
503 and the rest of the bank — screenshots and a pasted transcript — still works. A missing
dependency must not stop the server from starting.

ffmpeg's only job is cutting duration. It does no format conversion, because `mimo-v2.5` reads mp4
audio directly.

## Error handling

| Case | Behaviour |
|---|---|
| A clip would exceed the model's limit | Refused by our own 45s cap, so the gateway's misleading "corrupted" message is never surfaced to the creator |
| One clip fails | Status `partial`, the missing time range named, the other clips kept — the same shape as a partial voice-over |
| `yt-dlp` blocked or broken | 502 carrying the real message; the niche keeps whatever it already had |
| Unsupported image type | 400 naming `bmp/gif/png/jpeg/webp` |
| `MIMO_API_KEY` unset | The existing 503 from `mimo.get_client` |
| Path traversal on a niche id or a filename | Rejected by the same positive-pattern check used for project ids, and by `_safe_file` for filenames |

## Testing

Following the existing suite: MIMO is mocked at the service boundary, and `app/media.py` is mocked
at its own boundary, so the tests stay offline and cost nothing.

- Clip planning arithmetic: a 19s video yields one whole clip; a 60s video yields one, not four
  overlapping ones; a three-minute video yields four contiguous clips; a ten-minute video yields
  four spread across it. No planned clip ever exceeds 45s.
- Profile merge across partials, including one failed partial.
- Storage round-trip, and path-traversal rejection for both niche ids and screenshot filenames.
- Prompt injection: `titles`, `script` and the style bible carry the profile fields when a
  `niche_id` is set, and are byte-for-byte unchanged when it is not.
- The API surface, including the partial-failure path.

## Risks

- **`yt-dlp` is the fragile part.** The verified download succeeded without a JavaScript runtime,
  but yt-dlp warns that this path is deprecated and that deno will eventually be required. YouTube
  also blocks and throttles, so the package needs frequent updating. No design choice removes this.
- **Downloading from YouTube is against YouTube's terms of service.** Recorded here because it was
  raised and accepted, not to reopen it.
- **Stored video is large.** A downloaded reference is kept on disk per niche. It is deletable from
  the UI, but nothing prunes it automatically.
