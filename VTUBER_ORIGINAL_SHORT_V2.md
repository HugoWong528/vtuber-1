# 🎬 How "VTuber Original Short (Repo Save) v2" Works

## Overview

**VTuber Original Short (Repo Save) v2** is a fully automated GitHub Actions pipeline that generates YouTube-style Shorts featuring a **Live2D Hatsune Miku** model — complete with AI-written scripts, text-to-speech, background music, motion-synced animation, and burned-in subtitles. The finished video is saved directly to the repository (no YouTube upload).

This document explains every stage of the pipeline, from trigger to final output.

---

## 🔄 Trigger

The pipeline is defined in `.github/workflows/vtuber-original-repo-v2.yml` and fires in two ways:

| Trigger | Details |
|---|---|
| **Scheduled** | Every day at **15:00 UTC** (`cron: "0 15 * * *"`) |
| **Manual** | Via the Actions tab → *Run workflow*, with an optional `custom_topic` input |

---

## 🏗 End-to-End Architecture

```
GitHub Actions (cron / manual dispatch)
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│  scripts/generate_original_short_repo_v2.py              │
│                                                          │
│  [Step 1] AI Content Generation                          │
│     Pollinations Chat API → script + SEO metadata        │
│     Fallback: openai-large → openai → deepseek → …      │
│                                                          │
│  [Step 1b] AI Motion-Cue Scheduling                      │
│     Same Pollinations Chat API → JSON motion schedule    │
│     Fallback: keyword-based emotion matching             │
│                                                          │
│  [Step 2] Live2D Miku Capture (Puppeteer)                │
│     Node.js (capture_live2d_v2.js) + Headless Chrome     │
│     Renders 30 fps × 30 s = 900 frames → miku_live2d.mp4│
│                                                          │
│  [Step 3] Text-to-Speech                                 │
│     Pollinations Audio API → speech.mp3                  │
│     Fallback: elevenlabs → openai → GET /audio/{text}    │
│                                                          │
│  [Step 4] Background Music (best-effort)                 │
│     Pollinations Audio API (ACE-Step) → music.mp3        │
│                                                          │
│  [Compose] FFmpeg Video Composition                      │
│     Live2D video + TTS + BGM + subtitles → short.mp4     │
│                                                          │
│  [Save] Copy to videos/ + metadata JSON + log entry      │
└──────────────────────────────────────────────────────────┘
    │
    ▼
git commit & push → videos/{timestamp}.mp4 + .json
                    logs/upload_log.md
```

---

## 📋 Pipeline Steps in Detail

### Step 1 — AI Content Generation

The script calls the **Pollinations Chat API** (OpenAI-compatible) with a system prompt that tells the AI it is "Miku", a cheerful VTuber. The AI returns a single JSON object:

| Field | Purpose |
|---|---|
| `title` | YouTube title (≤ 80 chars, ending with `#Shorts`) |
| `description` | Multi-paragraph description with emojis, CTA, 15+ hashtags |
| `tags` | 20–30 keyword tags |
| `script` | ~130-word spoken script (≤ 55 seconds at natural pace) |
| `music_prompt` | Short prompt for ambient background music |

**Model fallback chain** (tried in order; each gets 1 s cooldown on failure):

```
openai-large → openai → deepseek → kimi → glm →
claude-fast → mistral → nova → grok → minimax
```

If the user provided a `custom_topic` via the manual trigger, it is injected into the prompt. Otherwise the AI autonomously picks a trending topic.

---

### Step 1b — AI Motion-Cue Scheduling (new in v2)

This is the **key difference from v1**. A second AI call reads the full spoken script and returns a **motion-cue schedule** — a JSON array that maps emotional beats to Live2D motion groups and the exact video frame at which they should fire.

**Available motion groups** (from the Miku Live2D model):

| Group | Description | Use when… |
|---|---|---|
| `Wave` (index 0) | Greeting wave | Miku says hello / welcome |
| `FlickUp` (index 0) | Excited upward flick | Joy, hype, amazing moments |
| `Tap` (indices 0–1) | Gentle tap | Calm explanation, soft/sad moments |
| `Flick` (indices 0–1) | Flick / surprise | Shock, unexpected, dramatic beats |
| `Idle` (indices 0–2) | Idle breathing reset | Between active cues to avoid freezing |

**Example AI output:**

```json
[
  {"frameIndex": 0,   "group": "Wave",    "motionIndex": 0},
  {"frameIndex": 90,  "group": "FlickUp", "motionIndex": 0},
  {"frameIndex": 210, "group": "Tap",     "motionIndex": 1},
  {"frameIndex": 450, "group": "Flick",   "motionIndex": 0},
  {"frameIndex": 820, "group": "Idle",    "motionIndex": 0}
]
```

**Validation rules** (enforced in `_validate_motion_cues()`):
- Only recognised motion groups are kept.
- `frameIndex` is clamped to `[0, total_frames)`.
- `motionIndex` is clamped to the group's allowed range.
- Two cues of the **same group** within 45 frames (~1.5 s) are dropped.
- At least 3 valid cues are required; fewer triggers a fallback.

**Keyword-based fallback:** If all AI models fail, the script scans words in the spoken script for emotion keywords (e.g., "wow" → `FlickUp`, "wait" → `Flick`, "sorry" → `Tap`, "hello" → `Wave`) and maps them to frame positions proportionally.

---

### Step 2 — Live2D Miku Capture via Puppeteer

This step renders 900 PNG frames (30 fps × 30 s) of the animated Live2D Miku model and pipes them into FFmpeg to produce a raw video clip.

**Architecture (three components):**

#### 2a. Local HTTP Server
A Python `http.server` is started on port `8787`, serving the repository root so Puppeteer can load the HTML page and Live2D model files.

#### 2b. `live2d_capture_v2.html` — The Render Page
Loaded inside headless Chrome. It:
1. Initialises a **PIXI.js** canvas (1080 × 1920).
2. Draws a gradient background (dark purple → teal).
3. Loads the **Live2D Cubism 4** Miku model (`.model3.json`, `.moc3`, textures).
4. Scales the model to **1.35×** fill and shifts it downward 120 px so the character fills the frame.
5. Stops the auto-ticker and exposes a manual frame-advance API:
   - `window.advanceFrame(ms)` — advances animation by `ms` milliseconds.
   - `window.setSpeaking(bool)` — enables/disables lip-sync (mouth oscillates via a sine-wave on `ParamMouthOpenY`).
   - `window.triggerMotion(group, index)` — fires a named Live2D motion.
   - `window.setMotionSchedule(schedule)` — receives the full cue list.

#### 2c. `capture_live2d_v2.js` — The Puppeteer Controller
A Node.js script that:
1. Launches headless Chrome with **SwiftShader** (software WebGL).
2. Navigates to `live2d_capture_v2.html` and waits for `window.modelReady`.
3. Pushes the motion schedule into the page via `window.setMotionSchedule()`.
4. Enables speaking mode (`window.setSpeaking(true)`) for the entire recording.
5. Loops 900 times (one per frame):
   - Checks the **cueMap** (a `Map<frameIndex, cue[]>`) — if the current frame has a cue, calls `window.triggerMotion()`.
   - Calls `window.advanceFrame(33)` (33 ms = 1 frame at 30 fps).
   - Takes a PNG screenshot and writes it to FFmpeg's stdin pipe.
6. Closes FFmpeg stdin → FFmpeg encodes all frames into `miku_live2d.mp4` (H.264, CRF 18).

**Lip-sync mechanism:**  
While `_speaking` is true, every `advanceFrame()` call oscillates the `ParamMouthOpenY` Live2D parameter using:
```
openAmt = max(0, sin(phase × π)) × 0.9
```
where `phase` increments by `ms × 0.003` per call (~2 Hz oscillation), simulating natural mouth movement.

---

### Step 3 — Text-to-Speech (TTS)

The spoken script is converted to audio using the **Pollinations Audio API** (`/v1/audio/speech`):

| Priority | Model | Voice |
|---|---|---|
| 1st | `elevenlabs` | `nova` |
| 2nd | `openai` | `nova` |
| 3rd (last resort) | `GET /audio/{text}` endpoint | `nova` |

The resulting `speech.mp3` determines the final video duration (`speech_duration + 0.5 s` buffer).

---

### Step 4 — Background Music (Best-Effort)

The Pollinations Audio API is called with the `music_prompt` and `model=acestep` (ACE-Step music generator). Duration is clamped to 5–30 seconds.

**If music generation fails**, the pipeline continues — the final video simply uses TTS-only audio. This is a "best-effort" step.

---

### Video Composition (FFmpeg)

All assets are combined into the final YouTube Short:

```
Live2D video (miku_live2d.mp4, 30 fps, 1080×1920)
    │
    ▼  scale + pad to 1080×1920 → fps=30
    │
    ▼  drawbox: translucent black bar, bottom 80 px
    │
    ▼  subtitles: Liberation Sans, 14pt, Bold, White, Outline=2, Shadow
       (FontSize reduced from 38 in v1 to 14 in v2)
    │
    ▼  Audio mix:
       TTS speech (weight 1.0, full volume)
     + BGM loop  (weight 0.2, ducked)  ← if music available
    │
    ▼  H.264 (CRF 23, preset veryfast) + AAC 192 kbps
       1080×1920, yuv420p, faststart
    │
    ▼  short.mp4
```

**Subtitle generation:** The script splits the spoken text into 3-word chunks, evenly distributes them across the speech duration, and writes an `.srt` file that FFmpeg burns into the video.

---

### Save to Repository

The final video and metadata are saved permanently:

| Output | Location |
|---|---|
| Video | `videos/{YYYY-MM-DD_HH-MM-SS}.mp4` |
| Metadata JSON | `videos/{YYYY-MM-DD_HH-MM-SS}.json` |
| Log entry | `logs/upload_log.md` (appended) |

The metadata JSON contains:
```json
{
  "timestamp": "2026-04-14_16-09-44",
  "title": "...",
  "description": "...",
  "tags": ["..."],
  "script": "...",
  "vtuber": "original-live2d",
  "workflow": "repo-save-only-v2"
}
```

The GitHub Actions workflow then runs `git add`, `git commit`, and `git push` with message `chore: auto-save original-vtuber-v2 video to repository [skip ci]`.

---

## 🔁 Cache / Resume System

The pipeline uses a `cache/` directory to save intermediate results. If a run fails partway through, the next run resumes from where it left off:

| Cache File | Stage |
|---|---|
| `cache/meta_repo_v2.json` | Tracks which stages completed |
| `cache/content_repo_v2.json` | AI-generated content (step 1) |
| `cache/miku_live2d_repo_v2.mp4` | Rendered Live2D clip (step 2) |
| `cache/speech_repo_v2.mp3` | TTS audio (step 3) |
| `cache/music_repo_v2.mp3` | Background music (step 4) |

On a **fully successful** run, all cache files are cleared.

---

## 🆚 What's New in v2 vs v1

| Feature | v1 (`generate_original_short_repo.py`) | v2 (`generate_original_short_repo_v2.py`) |
|---|---|---|
| **Motion cues** | None — Miku plays idle animation only | AI-controlled motion schedule (Wave, FlickUp, Tap, Flick, Idle) synced to script emotions |
| **Lip sync** | None | Continuous mouth oscillation via `ParamMouthOpenY` sine wave |
| **Subtitle font size** | 38 pt | 14 pt (smaller, less intrusive) |
| **Model scale** | Standard | 1.35× fill, shifted down 120 px — character fills more of the frame |
| **Capture HTML** | `live2d_capture.html` | `live2d_capture_v2.html` (gradient bg, motion API) |
| **Capture script** | `capture_live2d.js` | `capture_live2d_v2.js` (motion cue map, lip-sync control) |

---

## 📁 Key Files

```
.github/workflows/vtuber-original-repo-v2.yml   ← GitHub Actions workflow definition
scripts/generate_original_short_repo_v2.py       ← Main Python pipeline (orchestrator)
scripts/capture_live2d_v2.js                     ← Node.js Puppeteer capture controller
scripts/live2d_capture_v2.html                   ← Browser page that renders the Live2D model
scripts/requirements.txt                         ← Python dependencies (openai, requests, google-*)
miku_sample_t04.model3.json                      ← Live2D model config
miku_sample_t04.moc3                             ← Live2D model binary
miku_*.motion3.json                              ← 9 motion files (Idle, Tap, Flick, FlickUp, Wave)
texture_00.png                                   ← Miku texture atlas
videos/                                          ← Output directory (committed videos + metadata)
logs/upload_log.md                               ← Append-only run log
cache/                                           ← Intermediate result cache for resume
```

---

## ⚙️ CI Environment Setup

The workflow installs the following before running the pipeline:

| Category | Packages |
|---|---|
| **System** | FFmpeg, Liberation fonts, Noto fonts, Xvfb, Chromium deps (libgbm, libnss3, etc.) |
| **Node.js 20** | `puppeteer`, `pixi.js@6`, `pixi-live2d-display@0.4.0` |
| **Vendor** | `live2dcubismcore.min.js` (downloaded from Live2D CDN) |
| **Python 3.12** | `openai`, `requests`, `google-api-python-client`, `google-auth-*` |

---

## 🔑 Required Secret

| Secret | Description |
|---|---|
| `POLLINATIONS_API_KEY` | API key from [enter.pollinations.ai](https://enter.pollinations.ai) — powers all AI calls (text, image, TTS, music) |

No YouTube credentials are needed for the repo-save variant (unlike the upload variant in `vtuber-short.yml`).
