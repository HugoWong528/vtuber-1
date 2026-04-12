# 🎤 VTuber Auto-Short Generator

An automated AI pipeline that creates and publishes YouTube Shorts featuring a VTuber character — fully hands-free via GitHub Actions, powered by the [Pollinations API](https://gen.pollinations.ai).

---

## ✨ What it does

Every day at 12:00 UTC (or on-demand), the pipeline:

1. **AI Script + SEO** — A large language model autonomously picks a trending topic, writes a ~130-word spoken script, and generates a full YouTube metadata package (title, description, tags, hashtags).
2. **AI Background Image** — The Pollinations image API generates a vivid portrait (1080 × 1920) anime VTuber scene tailored to the topic.
3. **Expressive TTS** — The Pollinations audio API synthesises the script using ElevenLabs voices.
4. **Ambient BGM** — The Pollinations audio API generates a short background music loop via ACE-Step.
5. **Video Composition** — FFmpeg renders a high-quality YouTube Short with Ken Burns zoom, styled subtitles, and mixed audio.
6. **YouTube Upload** — The finished video is published via the YouTube Data API v3 with fully optimised metadata.

---

## 🏗 Architecture

```
GitHub Actions (daily cron / manual dispatch)
    │
    ▼
scripts/generate_short.py
    │
    ├─ [1] Pollinations Chat API ──► Script + SEO metadata
    │       Fallback chain: openai-large → openai → deepseek → kimi → glm
    │                       → claude-fast → mistral → nova → grok → minimax
    │
    ├─ [2] Pollinations Image API ──► Portrait background (1080 × 1920)
    │       Fallback chain: flux → zimage → klein → wan-image
    │
    ├─ [3] Pollinations Audio API ──► TTS speech (ElevenLabs voice)
    │       Fallback chain: elevenlabs → openai → GET /audio/{text}
    │
    ├─ [4] Pollinations Audio API ──► Ambient BGM (ACE-Step, best-effort)
    │
    ├─ [5] FFmpeg
    │       ├─ Ken Burns slow-zoom on AI background
    │       ├─ Translucent subtitle bar
    │       ├─ Styled burned-in subtitles (white, bold, 38pt, outline)
    │       └─ Audio mix: TTS (100%) + BGM (20%, ducked)
    │
    └─ [6] YouTube Data API v3 ──► Public Short uploaded
```

---

## 📋 Algorithm Deep-Dive

### 1 · AI Content Generation

The script sends a structured system prompt to the Pollinations Chat API (OpenAI-compatible endpoint). The AI returns a single JSON object containing:

| Field | Description |
|---|---|
| `title` | YouTube title ≤ 100 chars, ending with `#Shorts` |
| `description` | Multi-paragraph description with emoji, CTA block, ≥ 15 hashtags |
| `tags` | 20–30 keyword tags for YouTube metadata |
| `script` | ~130-word spoken script (≤ 55 s at natural pace) |
| `bg_prompt` | Detailed image prompt for the background scene |
| `music_prompt` | Short prompt for ambient background music |

If the primary model fails (timeout, rate limit, API error), the next model in the fallback chain is tried automatically with a 1-second delay between attempts.

### 2 · Background Image

The Pollinations image API is called with the `bg_prompt` at `1080 × 1920` (portrait / 9:16).  
The `enhance=true` flag lets Pollinations automatically improve the prompt for better image quality.  
Model fallback: `flux → zimage → klein → wan-image`.  
If all image models fail, the repo's `texture_00.png` is used as a static background.

### 3 · Text-to-Speech

The Pollinations `/v1/audio/speech` endpoint (OpenAI-compatible) is called with the `elevenlabs` model and `nova` voice for an expressive, natural-sounding delivery.  
Fallback: `openai` TTS model, then the simple `GET /audio/{text}` endpoint.

### 4 · Background Music

ACE-Step generates ambient background music from the `music_prompt`.  
Duration is clamped between 5 and 30 seconds (ACE-Step limit). The music is looped to cover the full video in FFmpeg.  
Music generation is **best-effort** — if it fails for any reason, the video is still produced with TTS-only audio.

### 5 · Video Composition (FFmpeg)

```
Background image (AI-generated, 1080 × 1920)
    │
    ▼  scale to 1.2× size (1296 × 2304) → Ken Burns zoompan → 1080 × 1920 @ 30 fps
       Slow zoom: 1.0 → 1.15 over the full video duration
    │
    ▼  drawbox: translucent black bar, bottom 310 px
    │
    ▼  subtitles: Liberation Sans, 38pt, Bold, White, Outline=3, Shadow
    │
    ▼  Audio mix:
       TTS speech (weight 1.0, full volume)
     + BGM loop  (weight 0.2, ducked)
    │
    ▼  H.264 (CRF 20, preset medium) + AAC 192 kbps
       1080 × 1920, yuv420p, faststart
    │
    ▼  short.mp4  →  YouTube upload
```

**Why Ken Burns?**  
A static image as background looks amateur. The slow zoom adds perceived motion and production value without requiring a video model.

### 6 · SEO Strategy

YouTube rewards well-optimised metadata. The AI generates:

- **Title** — Hook phrase + primary keyword + `#Shorts` (≤ 100 chars)
- **Description** — Emoji-formatted, multi-paragraph:
  - Hook sentence (drives click-through)
  - Body (2–3 sentences with keywords)
  - Subscribe CTA block
  - 15+ hashtags trailing block
- **Tags** — 20–30 deduplicated tags covering topic + VTuber genre keywords, trimmed to YouTube's 500-char limit
- **Subtitles** — Burned-in captions improve watch time, accessibility, and indexability
- **Category** — People & Blogs (ID 22)

### Model Fallback System

All Pollinations API calls use an ordered fallback chain. **Only non-paid models** are used.

| Category | Primary | Fallback order |
|---|---|---|
| Text / Script | `openai-large` | `openai` → `deepseek` → `kimi` → `glm` → `claude-fast` → `mistral` → `nova` → `grok` → `minimax` |
| Image | `flux` | `zimage` → `klein` → `wan-image` |
| TTS | `elevenlabs` | `openai` → GET fallback |
| Music | `acestep` | skipped on failure |

---

## 🔧 Setup

### 1 · Get a Pollinations API Key

Visit [https://enter.pollinations.ai](https://enter.pollinations.ai), sign in with GitHub, and create a free API key.

### 2 · Get YouTube API Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a new project.
2. Enable the **YouTube Data API v3**.
3. Go to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID** (Application type: Desktop app). Download the JSON.
4. Run the OAuth flow **once** locally to obtain a refresh token:

```python
from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json",
    scopes=["https://www.googleapis.com/auth/youtube.upload"]
)
creds = flow.run_local_server(port=0)
print("Refresh token:", creds.refresh_token)
```

5. Store the refresh token, client ID, and client secret as GitHub Secrets.

### 3 · Add GitHub Secrets

**Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Description |
|---|---|
| `POLLINATIONS_API_KEY` | Pollinations API key (from step 1) |
| `YOUTUBE_CLIENT_ID` | Google OAuth2 client ID |
| `YOUTUBE_CLIENT_SECRET` | Google OAuth2 client secret |
| `YOUTUBE_REFRESH_TOKEN` | Long-lived refresh token (`youtube.upload` scope) |

### 4 · That's it!

The workflow fires automatically at **12:00 UTC every day**.  
To trigger manually: **Actions → VTuber Auto-Short → Run workflow**  
Optionally fill in `custom_topic` to override the AI's autonomous topic choice.

---

## 📁 Repository Structure

```
vtuber-1/
├── .github/
│   └── workflows/
│       ├── deploy.yml           # GitHub Pages deployment (Live2D viewer)
│       └── vtuber-short.yml     # VTuber Short auto-generation pipeline
├── scripts/
│   ├── generate_short.py        # Main AI → video → YouTube pipeline
│   └── requirements.txt         # Python dependencies
├── texture_00.png               # Live2D Miku texture (background fallback)
├── miku_sample_t04.model3.json  # Live2D model configuration
├── index.html                   # GitHub Pages Live2D viewer
└── README.md                    # This file
```

---

## 🎛 Customisation

| Setting | File | Variable / Location |
|---|---|---|
| Posting time | `vtuber-short.yml` | `cron: "0 12 * * *"` |
| Video privacy | `generate_short.py` | `YOUTUBE_PRIVACY` |
| TTS voice | `generate_short.py` | `TTS_VOICE` |
| Primary text model | `generate_short.py` | `TEXT_MODEL_FALLBACK[0]` |
| Primary image model | `generate_short.py` | `IMAGE_MODEL_FALLBACK[0]` |
| Zoom intensity | `generate_short.py` | `0.15` in `compose_video()` |
| BGM volume | `generate_short.py` | `weights='1.0 0.2'` in `amix` |

---

## 📦 Dependencies

| Package | Purpose |
|---|---|
| `openai` | Pollinations OpenAI-compatible SDK client (chat + TTS) |
| `requests` | Pollinations image and music HTTP calls |
| `google-api-python-client` | YouTube Data API v3 |
| `google-auth-httplib2` | HTTP transport for Google Auth |
| `google-auth-oauthlib` | OAuth2 flow helpers |
| `ffmpeg` | Video/audio composition (pre-installed on `ubuntu-latest`) |
| `fonts-liberation` | Liberation Sans for subtitles (installed in workflow) |

---

## 📜 License

Live2D Miku sample assets are subject to the [Live2D Free Material License](https://www.live2d.com/download/sample-data/).  
All generated video content is produced autonomously by AI.
