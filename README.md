# YouTube Creation Dashboard

An idea goes in one end and a complete faceless-video package comes out the other: title options
with click-through scores, a script written to a length budget, a shot list of image-generator
prompts cut every few seconds, and a cloned-voice narration track.

No login. No database. Everything is written to disk as JSON, text, CSV and WAV.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then put your MIMO key in it
.venv/bin/uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

`.env` needs one line:

```
MIMO_API_KEY=your-key-here
```

Everything — titles, script, prompts, voice cloning — runs through MIMO
(`api.xiaomimimo.com`). There is no second provider and no other key to obtain.

## The pipeline

0. **Niche (optional).** A reference assembled once in the Niche Bank and reused across projects.
   See below.
1. **Context.** Topic, category, output language, optional reference channel, target minutes,
   word ceiling, words-per-minute, and seconds per image. `minutes × wpm` sets the word target;
   the ceiling defaults to 10% above it.
2. **Titles.** Eight options, each with a 0–100 score, the hook devices it leans on, its character
   count, and a one-line rationale. The score is the model's judgment of how the title reads — it
   is not measured click-through data.
3. **Script.** Written to the budget, then reported back with its real word count and estimated
   duration. If it overruns, one tightening pass runs; if it still overruns, you are told rather
   than silently truncated. Editable in place.
4. **Scenes.** The script is cut into beats a few seconds long, and each beat gets one
   image-generator prompt. A thumbnail prompt and one shared negative prompt come with them.
5. **Voice-over.** Pick a voice from the bank. The whole script is narrated as a single track.

## Niche Bank: matching a reference

A niche is a reference you want your videos to resemble. It holds a YouTube link, the reference
video, screenshots, and a transcript, in any combination. Analysing it produces one small profile:

```
hook_patterns  tone  pacing  vocabulary  structure  title_style
visual_style  shot_language  avoid  image_prompt_seed
```

That profile — and nothing else — is copied into a project when you pick the niche, and it feeds
the titles, the script and the style bible. **The transcript is never sent with it.** A ten-minute
transcript is around 1,500 words, and the script prompt already carries its own word budget; the
raw text beside those instructions would drown them and push the script off its target length.

Everything is read by `mimo-v2.6-flash`, which takes images and mp4 alike and reads a video's
frames and its audio track in the same call. There is no separate transcription step and nothing
converts video to audio. The same model also writes the titles, script and scene prompts; only
voice cloning uses a different one (`mimo-v2.5-tts-voiceclone`).

### The 45-second rule

The API caps video input by duration, not by size: a 50-second clip is accepted, a 55-second one is
refused, while a 3.75 MB 30-second clip passes and a 0.37 MB two-minute clip does not. The refusal
arrives as `Multimodal data is corrupted or cannot be processed`, which is not true and would send
you hunting the wrong problem.

So a reference video is never sent whole. Up to four 45-second clips are cut from it and analysed
separately, then reconciled into one profile. The count is `duration // 45`, capped at four: at a
fixed four, a two-minute video would be sampled at 0s, 30s, 60s and 90s, and four 45-second clips
from those offsets would overlap almost entirely — four times the cost for nearly the same footage.
A video shorter than one clip is used whole.

A clip that fails twice leaves the run `partial` with the missing time range named, and the rest of
the profile is kept.

### What it costs

Around 40,000 tokens for a ten-minute reference, paid once. Every project that then uses the niche
pays only for the profile, roughly 300 tokens. Video cost scales with resolution rather than length
— about 220 tokens per second at 360p, which is why downloads are capped at that height.

### Screenshots

Paste them straight into the niche: click the screenshot box and press paste, and every image on
the clipboard is taken, not just the first. Drag-and-drop and a file picker work too. The API
accepts bmp, gif, png, jpeg and webp.

### Downloading from YouTube

`yt-dlp` fetches the video. Two things are worth knowing before relying on it:

- **It is against YouTube's terms of service.**
- **It breaks.** YouTube blocks and throttles, and yt-dlp warns that extraction without a
  JavaScript runtime is deprecated — it works today via a fallback client, but deno will eventually
  be required. Keep the package updated.

`yt-dlp` and `imageio-ffmpeg` are both optional at runtime. Without them the YouTube and video
features report a 503 naming the missing package, and the rest of the bank — screenshots and a
pasted transcript — still works. ffmpeg only ever cuts durations here; it converts nothing.

## Scenes: one image every few seconds

A scene is one still image held on screen. The cadence is per project — 2 to 10 seconds, default
3 — because cutting often is what stops a faceless montage feeling static. A ten-minute video at
three seconds is about two hundred images.

The cadence is a **target, not a slot**. Each image is held for exactly as long as its own line
takes to say, and the start times accumulate. That is forced by the audio being a single
continuous track: if every image sat in a fixed three-second slot while the lines under them ran
2.2, 3.4 and 1.7 seconds, the pictures would fall further behind with every cut. Beats that would
come out as one-word stubs are folded into their neighbour, so the rhythm stays close to the
target without buying a whole image for the word "and".

That is far more than one model call can produce without drifting or being cut off, so the work is
staged:

1. **The script is split in code, not by the model.** That guarantees every word is covered, the
   scene count is exact, the order is right, and no beat's narration outruns the seconds its image
   is on screen. Sentence boundaries are preferred, then clause boundaries; a sentence longer than
   one beat is broken rather than allowed to overflow, because a fraction of a second repeated
   across two hundred images is a minute of drift.

   At three seconds a beat is only seven words — often half a clause, which is useless on its own
   as the basis for an image. So **every beat also carries the whole sentence it came from**. The
   beat decides *when* the image appears; the sentence tells the model what the moment is *about*.

2. **One call produces a style bible** — art direction, palette, lighting, camera language, and
   fixed descriptions for recurring subjects. Without it, two hundred independently generated
   stills look like two hundred different videos.

3. **A shot type is assigned in code**, cycling through wide, medium, close-up, detail, new angle,
   point-of-view, high angle, low angle. Models left to themselves write the same framing over and
   over; the cycle is what gives a long montage its rhythm. The prompt generator is told to honour
   the assigned shot.

4. **Prompts are generated in batches of sixteen**, four batches in flight, each carrying the style
   bible and the tail of the previous batch so consecutive images read as cuts within one scene
   rather than unrelated pictures.

5. **The thumbnail gets its own call**, and one **negative prompt** is derived for the whole video
   from the style bible's "avoid" list plus the usual junk (text, watermarks, extra fingers). It is
   global rather than per-scene: consistency matters more than variety, and it saves a field on
   every one of a few hundred rows.

A batch that fails twice leaves its scenes marked `failed` with their narration intact, and the UI
offers a retry for just those.

## Outputs

Everything lands in `data/projects/<project-id>/`:

| File | What it is |
|---|---|
| `project.json` | Every piece of state. The source of truth. |
| `script.txt` | The bare script, ready to paste anywhere. |
| `prompts.csv` | One row per image, thumbnail as the final row. |
| `audio/voiceover_full.wav` | The narration, as one track. |
| `audio/chunks/` | The per-request pieces, kept only so a retry can skip what it already rendered. |

`prompts.csv` columns:

```
scene,start,end,duration_sec,target_sec,shot,focus,narration,image_prompt,negative_prompt
```

`duration_sec` is how long that image is on screen: exactly as long as its own line takes to say.
`target_sec` is the cadence you asked for, kept alongside so you can see where the rhythm lurches.
`start` and `end` are `HH:MM:SS.s` and they butt up against each other — the narration is one
continuous track, so the times have to add up to it or the pictures drift further behind with
every cut. Once the voice-over has run, the whole list is re-laid against the pace the voice
actually read at.

`negative_prompt` is repeated on every row so a batch paste carries it, and `shot` and `focus` are
there so you can see the montage's rhythm at a glance in a spreadsheet.

There is also a `bundle.zip` export that packages the project directory. It leaves out
`audio/chunks/` — the deliverable is the full track.

## Voice bank

Cloned voices live under `data/voices/`, with the samples in `data/voices/samples/`. Each voice
stores a name, an mp3 or wav sample, five delivery settings, a category, a description, and an
optional style preset from `style_presets.json`.

MIMO's cloning model exposes **no numeric knobs** — the sliders are translated into a
natural-language style prompt, which is the only thing the model actually receives. The exact
prompt is shown in the UI so nothing is hidden.

MIMO also stores no voice ID: the base64 sample is re-sent on every synthesis request. Keep samples
under about 2 MB — the UI warns above that. Requests are capped at three in flight with exponential
backoff on retry.

## The voice-over is one track

The script is split on sentence boundaries into chunks small enough for a single request, each
chunk is synthesized, and the audio is concatenated into `voiceover_full.wav`. A ten-minute script
is about six requests.

Chunking on sentences rather than on images is what makes it sound right: the model sees whole
sentences, so the prosody carries across them. Cutting the audio at every image would have meant
two hundred requests and two hundred fresh starts.

If a chunk fails after its retries the track is still written, the status goes to `partial`, and
the missing piece is named — a hole in the middle of a sentence is worth saying out loud rather
than shipping quietly. Retrying re-renders only the pieces that are missing.

## Calibrate words-per-minute after your first voice-over

`wpm` decides how many words go into each beat, which is what makes the cut rhythm land near the
cadence you asked for. The default of 150 is an English figure and does not hold everywhere:
measured on this build, a cloned voice reading Indonesian came in between **107 and 122 wpm**
depending on the passage.

So every voice-over job reports `measured_wpm` once it finishes, and the UI offers it as a
one-click replacement for the setting. Put it in and regenerate the scenes and the beats land on
the cadence you asked for.

You do not have to, though. The shot list is re-laid against the measured pace as soon as the
voice-over finishes, so the timings in the CSV always match the audio. Getting `wpm` right changes
how *evenly* the images are spaced, not whether they are in sync.

The pace depends on the language, the voice, and the speed slider together, so measure once per
voice rather than trusting a default.

## Layout

```
app/
  main.py config.py models.py mimo.py audio.py media.py storage.py export.py
  services/  titles.py script.py scenes.py voiceover.py niche.py
  routers/   pages.py projects.py voices.py niches.py
templates/   base.html dashboard.html project.html voices.html niches.html
static/      css/app.css  js/{api,dashboard,project,voices,niches}.js
tests/
data/        created on first run
```

`media.py` is the only module that shells out or reaches YouTube, which is what lets the whole test
suite run offline by replacing that one import.

`routers/` never calls MIMO directly, and `storage.py` is the only module that writes project JSON.
`services/` does not know about requests or responses, but it does raise FastAPI's `HTTPException`
to report upstream failures — a deliberate shortcut so a MIMO error carries its status code all the
way out without a translation layer in every route.

The CSS is hand-written rather than Tailwind. The constraint that mattered was "no build step", and
a single stylesheet meets it while also working offline and keeping the templates readable.

## Tests

```bash
.venv/bin/python -m pytest
```

MIMO is mocked at the service boundary, so the suite runs offline and costs nothing. It covers WAV
handling, the segmenter's coverage guarantees, CSV quoting, storage round-trips and path-traversal
rejection, and the full API surface including partial-failure paths.

## Known limits

- **Sentence splitting assumes spaces.** The segmenter counts whitespace-separated words and splits
  on punctuation followed by whitespace. That covers Indonesian and English, the two output
  languages. A script in Chinese or Japanese would land in one enormous segment.
- **Audio is assembled in memory.** Building the full track and the zip bundle both hold the
  project's WAVs in RAM. A 60-minute project is a few hundred megabytes — fine on a laptop, but it
  is not streamed.
- **A very long script is capped.** `MAX_SCENES` (500) bounds a runaway breakdown. When it bites,
  the response reports `dropped_scenes` rather than quietly handing over a partial package.
- **A niche profile is a snapshot.** Re-analysing a niche does not change a project that already
  uses it. That is deliberate — a finished script should not have its reference rewritten under it —
  but it does mean an improved profile has to be re-attached to a project by hand.
- **Reference videos are kept.** The download stays on disk per niche and nothing prunes it. It is
  removable from the UI.

## Configuration

Tunables live in `app/config.py`: scene length, batch size, concurrency caps, retry counts, sample
size limits, and the words-per-minute default. Set `YTC_DATA_DIR` to put the data directory
somewhere other than `./data`.

## API

Interactive docs at http://127.0.0.1:8000/docs while the server is running.
