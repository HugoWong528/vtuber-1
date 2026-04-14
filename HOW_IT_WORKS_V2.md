# How "VTuber Original Short (Repo Save) v2" Works

A complete walkthrough of the automated pipeline that generates YouTube Shorts featuring a Live2D Hatsune Miku VTuber — with **AI-controlled body motions** and **lip-sync animation** — and saves the output directly into this repository.

---

## Overview

The **v2** pipeline runs entirely inside GitHub Actions. On every scheduled run (daily at 15:00 UTC) or manual trigger, it:

1. Asks an AI to write a spoken script and YouTube SEO metadata.
2. Asks a **second** AI call to create a motion-cue schedule so Miku moves expressively while speaking.
3. Renders a 30-second Live2D Miku animation video via a headless browser (Puppeteer).
4. Generates text-to-speech (TTS) audio of the script.
5. Generates ambient background music (best-effort).
6. Composites everything into a final 1080 × 1920 YouTube Short with burned-in subtitles.
7. Commits the finished `.mp4` and metadata `.json` into the `videos/` folder of this repository.

No YouTube upload happens — the video is **saved to the repo only**.

---

## Pipeline Architecture

```
GitHub Actions  (cron: 15:00 UTC daily  /  manual dispatch)
    │
    ▼
scripts/generate_original_short_repo_v2.py
    │
    ├─ [Step 1]  Pollinations Chat API  ──►  Script + SEO metadata (JSON)
    │     Fallback chain: openai-large → openai → deepseek → kimi → glm
    │                     → claude-fast → mistral → nova → grok → minimax
    │
    ├─ [Step 1b] Pollinations Chat API  ──►  Motion-cue schedule (JSON array)
    │     Same model fallback chain
    │     Keyword-based fallback if all AI models fail
    │
    ├─ [Step 2]  Puppeteer + Live2D     ──►  30 s animated Miku video (1080×1920)
    │     capture_live2d_v2.js + live2d_capture_v2.html
    │     Headless Chrome with SwiftShader WebGL
    │
    ├─ [Step 3]  Pollinations Audio API ──►  TTS speech (MP3)
    │     Fallback: elevenlabs → openai → GET /audio/{text}
    │
    ├─ [Step 4]  Pollinations Audio API ──►  Background music (ACE-Step, best-effort)
    │
    ├─ [Step 5]  FFmpeg                 ──►  Final composed video
    │     Live2D video + TTS + BGM + subtitles → H.264 MP4
    │
    └─ [Step 6]  git commit + push      ──►  videos/{timestamp}.mp4 + .json
```

---

## Step-by-Step Breakdown

### Step 1 — AI Content Generation

**File:** `scripts/generate_original_short_repo_v2.py` → `ai_generate_content()`

The pipeline sends a system prompt to the [Pollinations Chat API](https://gen.pollinations.ai) (OpenAI-compatible endpoint). The AI receives instructions to act as "Miku" and write a YouTube Short script.

**What the AI returns** (a single JSON object):

| Field | Purpose |
|---|---|
| `title` | YouTube title ≤ 80 chars, ending with `#Shorts` |
| `description` | Multi-paragraph YouTube description with emojis, CTA, 15+ hashtags |
| `tags` | 20–30 keyword tags for YouTube metadata |
| `script` | ~130-word spoken script (≤ 55 seconds when spoken) |
| `music_prompt` | Short prompt for ambient background music |

**Model fallback:** If one model fails (timeout, rate limit, bad JSON), the next model in a 10-model chain is tried automatically with a 1-second delay. The chain is:

```
openai-large → openai → deepseek → kimi → glm → claude-fast → mistral → nova → grok → minimax
```

---

### Step 1b — AI Motion-Cue Generation (v2 exclusive)

**File:** `scripts/generate_original_short_repo_v2.py` → `generate_motion_cues_ai()`

This is the **key v2 enhancement**. A second AI call reads the spoken script and produces a **motion-cue schedule** — a JSON array that maps emotional beats in the script to Live2D body animations.

**Available motion groups in the Miku model:**

| Group | Motion Files | Description |
|---|---|---|
| `Idle` | miku_01, miku_04, miku_07 | Looping idle breathing |
| `Tap` | miku_02, miku_03 | Gentle tapping / soft action |
| `Flick` | miku_05, miku_08 | Quick flick / surprise |
| `FlickUp` | miku_06 | Excited upward jump |
| `Wave` | miku_09 | Greeting wave |

**Motion cue format:**

```json
[
  {"frameIndex": 0,   "group": "Wave",    "motionIndex": 0},
  {"frameIndex": 90,  "group": "FlickUp", "motionIndex": 0},
  {"frameIndex": 180, "group": "Tap",     "motionIndex": 1},
  ...
]
```

The AI is instructed to:
- Schedule 10–18 cues spread across the video (900 frames at 30 fps = 30 seconds).
- Start at frame 0 (Wave if Miku greets, otherwise Idle).
- End with an Idle reset within the last 90 frames.
- Never place two cues of the same group within 45 frames (~1.5 s) of each other.

**Validation:** After the AI responds, `_validate_motion_cues()` cleans the output:
- Drops unrecognised groups.
- Clamps `frameIndex` to `[0, totalFrames)`.
- Enforces `motionIndex` ranges per group.
- Enforces 45-frame minimum cooldown per group.
- Requires at least 3 valid cues; otherwise falls back.

**Keyword fallback:** If all AI models fail, `_generate_motion_cues_keyword()` scans the script for emotional words and assigns motions:
- Greeting words ("hello", "hi", "welcome") → `Wave`
- Excited words ("wow", "amazing", "awesome") → `FlickUp`
- Sad words ("sad", "sorry", "terrible") → `Tap`
- Surprise words ("wait", "what", "really") → `Flick`
- Idle resets are injected every 8 seconds of no activity.

---

### Step 2 — Live2D Miku Rendering

**Files:**
- `scripts/capture_live2d_v2.js` — Node.js Puppeteer capture orchestrator
- `scripts/live2d_capture_v2.html` — HTML page that loads and animates the Live2D model

**How it works:**

1. A Python `http.server` is started on port 8787, serving the repo root.
2. Puppeteer launches a headless Chrome (with SwiftShader for WebGL rendering).
3. The browser navigates to `live2d_capture_v2.html`, which:
   - Loads the Live2D Cubism 4 Core SDK, PIXI.js v6, and pixi-live2d-display.
   - Creates a 1080 × 1920 canvas with a purple-to-teal gradient background.
   - Loads the Miku model (`miku_sample_t04.model3.json`) at 1.35× scale, centered and shifted down to fill the frame.
   - Disables the automatic PIXI ticker — animation is advanced manually frame-by-frame for deterministic rendering.
   - Runs a 90-frame physics warm-up so the model settles into a natural pose.
4. Puppeteer enables **lip-sync mode** — a sine-wave oscillation on `ParamMouthOpenY` that makes Miku's mouth open and close naturally while "speaking."
5. For each of the 900 frames (30 fps × 30 s):
   - If a motion cue is scheduled for this frame, `window.triggerMotion(group, index)` is called, making the model play the specified animation.
   - `window.advanceFrame(33ms)` advances the animation clock by one frame period.
   - A PNG screenshot is taken and piped to FFmpeg.
6. FFmpeg encodes the PNG stream into an H.264 MP4 (CRF 18, `veryfast` preset).

**Result:** A 30-second silent video of Miku animating with expressive body motions and lip sync.

---

### Step 3 — Text-to-Speech (TTS)

**File:** `scripts/generate_original_short_repo_v2.py` → `generate_tts()`

The spoken script is sent to the Pollinations `/v1/audio/speech` endpoint (OpenAI-compatible), requesting the `nova` voice (bright and energetic).

**Fallback chain:**
1. `elevenlabs` model → expressive, natural delivery
2. `openai` model → solid fallback
3. `GET /audio/{text}` endpoint → last-resort simple TTS

**Output:** An MP3 file of Miku's spoken script.

---

### Step 4 — Background Music (Best-Effort)

**File:** `scripts/generate_original_short_repo_v2.py` → `generate_music()`

The `music_prompt` from step 1 is sent to the Pollinations audio API with the `acestep` model (ACE-Step music generator).

- Duration is clamped between 5 and 30 seconds (ACE-Step limit).
- If generation fails for any reason, the video is produced with TTS-only audio — **music is optional**.

---

### Step 5 — Video Composition (FFmpeg)

**File:** `scripts/generate_original_short_repo_v2.py` → `compose_video()`

FFmpeg combines all assets into the final YouTube Short:

```
Live2D video (1080×1920, 30 fps, 30 s, silent)
    │
    ▼  Scale to fit 1080×1920, pad if needed
    │
    ▼  drawbox: translucent black bar (80 px tall) at the bottom
    │
    ▼  Burned-in subtitles:
    │     Font: Liberation Sans, 14pt, Bold
    │     White text, black outline (2px), shadow
    │     Positioned at bottom with 18px margin
    │     Words grouped in chunks of 3, timed to speech duration
    │
    ▼  Audio mix:
    │     TTS speech at full volume (weight 1.0)
    │   + BGM loop at 20% volume (weight 0.2)  [if available]
    │
    ▼  Encoding: H.264, CRF 23, veryfast preset, AAC 192kbps
    │  Container: MP4, yuv420p, faststart
    │
    ▼  Duration: matches TTS speech length + 0.5s buffer
```

**v2 differences from v1:**
- FontSize reduced from 38 → **14** (smaller, less intrusive subtitles).
- Encoding preset: `veryfast` (faster CI runs).
- CRF: 23 (slightly smaller files).

---

### Step 6 — Save to Repository

**File:** `scripts/generate_original_short_repo_v2.py` → `save_video_to_repo()`

The finished video and a JSON metadata file are copied into the `videos/` directory:

```
videos/
├── 2026-04-14_16-09-44.mp4    ← The final YouTube Short
└── 2026-04-14_16-09-44.json   ← Title, description, tags, script, workflow name
```

A log entry is also appended to `logs/upload_log.md`.

The GitHub Actions workflow then commits and pushes these files:

```yaml
git add videos/ logs/ cache/
git commit -m "chore: auto-save original-vtuber-v2 video to repository [skip ci]"
git push
```

The `[skip ci]` tag prevents the commit from re-triggering the workflow.

---

## Caching / Resume System

The pipeline caches intermediate results in the `cache/` directory so that if a run fails partway through, the next run can resume without repeating expensive steps:

| Cache File | What It Stores |
|---|---|
| `meta_repo_v2.json` | Which stages have completed |
| `content_repo_v2.json` | AI-generated script + SEO metadata |
| `miku_live2d_repo_v2.mp4` | Rendered Live2D video |
| `speech_repo_v2.mp3` | TTS audio |
| `music_repo_v2.mp3` | Background music |

On a fully successful run, the cache is cleared.

---

## GitHub Actions Workflow

**File:** `.github/workflows/vtuber-original-repo-v2.yml`

### Trigger

- **Scheduled:** Daily at 15:00 UTC (1 hour after the v1 pipeline).
- **Manual:** Via the Actions tab, with an optional `custom_topic` input.

### Environment Setup

1. **System packages:** FFmpeg, Liberation/Noto fonts, Xvfb, Chromium dependencies.
2. **Node.js 20:** Puppeteer, PIXI.js v6, pixi-live2d-display 0.4.0.
3. **Live2D SDK:** Cubism Core JS downloaded from `cubism.live2d.com`.
4. **Python 3.12:** openai, requests (from `scripts/requirements.txt`).

### Secret Required

| Secret | Purpose |
|---|---|
| `POLLINATIONS_API_KEY` | Authenticates all Pollinations API calls (text, TTS, music) |

---

## What Changed from v1 to v2

| Feature | v1 (`generate_original_short_repo.py`) | v2 (`generate_original_short_repo_v2.py`) |
|---|---|---|
| **VTuber motion** | Static idle animation only | AI-directed motion cues tied to script emotion |
| **Lip sync** | None | Sine-wave mouth oscillation throughout speech |
| **Motion cue source** | N/A | AI scheduling + keyword-based fallback |
| **Capture script** | `capture_live2d.js` | `capture_live2d_v2.js` (accepts motion cue JSON) |
| **HTML page** | `live2d_capture.html` | `live2d_capture_v2.html` (exposes `triggerMotion`, `setSpeaking`) |
| **Subtitle font size** | 38pt | 14pt (smaller, less intrusive) |
| **Model scale** | Default | 1.35× fill, shifted down for better visibility |

---

## File Map

```
.github/workflows/
└── vtuber-original-repo-v2.yml          ← GitHub Actions workflow definition

scripts/
├── generate_original_short_repo_v2.py   ← Main Python pipeline (AI + TTS + music + FFmpeg)
├── capture_live2d_v2.js                 ← Puppeteer frame-by-frame capture with motion cues
├── live2d_capture_v2.html               ← HTML page: Live2D model + lip-sync + motion API
└── requirements.txt                     ← Python dependencies (openai, requests)

miku_sample_t04.model3.json              ← Live2D model config
miku_sample_t04.moc3                     ← Live2D model binary
miku_sample_t04.cdi3.json                ← Live2D display info
miku_sample_t04.physics3.json            ← Live2D physics simulation config
miku_01–09.motion3.json                  ← Motion data (Idle, Tap, Flick, FlickUp, Wave)
texture_00.png                           ← Miku texture atlas

videos/                                  ← Output: timestamped .mp4 and .json files
logs/upload_log.md                       ← Append-only log of all runs
cache/                                   ← Intermediate results for crash recovery
```

---

## Quick Summary

> **VTuber Original Short (Repo Save) v2** is a fully automated GitHub Actions pipeline that uses AI to write a script, directs a Live2D Miku model to move expressively and lip-sync to the words, generates TTS voiceover and background music, composites everything into a polished 1080×1920 YouTube Short with subtitles, and commits the result to the repository — all without human intervention.
