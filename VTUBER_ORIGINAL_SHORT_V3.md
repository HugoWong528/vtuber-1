# VTuber Original Short (Repo Save) v3

## What's new in v3

### 1. Unique topics every run
- Reads **all past video metadata** from `videos/*.json` at the start of each run.
- Injects the list of past titles/topics into the AI system prompt with an explicit instruction to **never repeat** a past topic.
- A **random seed** (date + random words) is injected every run to further encourage variety, even when no custom topic is provided.

### 2. Girl voice
- TTS voice changed from `nova` (warm female) to **`shimmer`** (soft, bright female / girl voice).
- Works with both the OpenAI and ElevenLabs TTS backends.

### 3. Faster generation
| Optimization | v2 | v3 | Savings |
|---|---|---|---|
| AI calls | 2 (content + motion cues) | **1** combined call | ~10-30s |
| Live2D + TTS | Sequential | **Parallel** (threaded) | ~30s |
| Capture FPS | 30 | **24** | 20% fewer frames |
| FFmpeg preset | veryfast / CRF 23 | **ultrafast / CRF 26** | Faster encode |
| Model fallbacks | 10 models | **5** fastest models | Less retry time |

### 4. Web version bug fixes (`index.html`)
- **Pinned PIXI.js to v6.5.10** instead of floating `@6` tag — prevents future breaking changes.
- **Added `onerror` handlers** on all CDN `<script>` tags — shows a clear error message if a dependency fails to load.
- **Added PIXI/live2d guards** in `init()` — prevents cryptic errors if CDN scripts are blocked (ad-blockers, network issues).
- **Fixed double-scaling bug** in `positionModel()` — removed redundant `× 1.3` multiplier that was stacking with the default slider value of 1.3 (resulting in 1.69× instead of 1.3×).
- **Fixed optional catch binding** (`catch { ... }` → `catch (_e) { ... }`) for broader browser compatibility.

## Files

| File | Description |
|---|---|
| `scripts/generate_original_short_repo_v3.py` | Main generation script (v3) |
| `.github/workflows/vtuber-original-repo-v3.yml` | GitHub Actions workflow |
| `scripts/capture_live2d_v2.js` | Puppeteer capture script (reused from v2) |
| `scripts/live2d_capture_v2.html` | Live2D capture HTML (reused from v2) |
| `index.html` | Web controller (bug fixes applied) |

## How to run

### Automatic (scheduled)
The workflow runs daily at **16:00 UTC**. It generates a unique video and commits it to `videos/`.

### Manual
1. Go to **Actions** → **VTuber Original Short (Repo Save) v3**
2. Click **Run workflow**
3. Optionally provide a custom topic hint
4. The video and metadata will be committed to the repository

### Required secret
- `POLLINATIONS_API_KEY` — obtain from https://enter.pollinations.ai
