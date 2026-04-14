#!/usr/bin/env python3
"""
VTuber Original Short Generator — Repository Save Only  (v2)
=============================================================
Pipeline identical to generate_original_short_repo.py with two enhancements:

  1. Subtitle FontSize reduced from 38 to 14.
  2. VTuber motion follows speech content:
       • Lip-sync mouth animation is active throughout (handled in the
         capture HTML via a sine-wave oscillation of ParamMouthOpenY).
       • A motion-cue schedule is derived from the AI script by detecting
         greeting words (→ Wave), excited words (→ FlickUp), action/tap words
         (→ Tap), surprise words (→ Flick), and periodic idle resets (→ Idle).
       • The schedule is passed as JSON to capture_live2d_v2.js, which
         triggers window.triggerMotion() at the exact frame each cue fires.

Available motion groups in the Miku model:
  Idle     → miku_01, miku_04, miku_07  (looping idle breathing)
  Tap      → miku_02, miku_03           (tapping / gentle action)
  Flick    → miku_05, miku_08           (flick / surprise)
  FlickUp  → miku_06                    (jump / excited upward flick)
  Wave     → miku_09                    (greeting wave)

Speed optimisations (inherited from v1 repo-save script):
  • Puppeteer captures at 30 fps for 30 s.
  • FFmpeg composition preset: veryfast, CRF 23.
  • Output video: 30 fps.

Required GitHub Secret:
  POLLINATIONS_API_KEY  – from https://enter.pollinations.ai
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

POLLINATIONS_BASE    = "https://gen.pollinations.ai"
POLLINATIONS_V1_BASE = "https://gen.pollinations.ai/v1"

# Non-paid text models — tried in order until one succeeds
TEXT_MODEL_FALLBACK = [
    "openai-large",  # GPT-5.4 — most capable
    "openai",        # GPT-5.4 Nano — balanced
    "deepseek",      # DeepSeek V3.2
    "kimi",          # Moonshot Kimi K2 Thinking
    "glm",           # Z.ai GLM-5 744B MoE
    "claude-fast",   # Anthropic Claude Haiku 4.5
    "mistral",       # Mistral Small 3.2
    "nova",          # Amazon Nova 2 Lite
    "grok",          # xAI Grok 4.1
    "minimax",       # MiniMax M2.5
]

# TTS model fallback
TTS_MODEL_FALLBACK = ["elevenlabs", "openai"]
TTS_VOICE = "nova"  # bright, energetic voice

# Live2D capture settings
CAPTURE_FPS           = 30
CAPTURE_DURATION_SECS = 30
CAPTURE_PRESET        = "veryfast"
CAPTURE_PORT          = 8787

VIDEO_WIDTH  = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS    = 30

AUDIO_BUFFER_SECONDS = 0.5

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "")
    if required and not value:
        print(f"[ERROR] Environment variable '{name}' is not set.", file=sys.stderr)
        sys.exit(1)
    return value


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    print(f"[CMD] {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def pollinations_client() -> OpenAI:
    return OpenAI(
        base_url=POLLINATIONS_V1_BASE,
        api_key=env("POLLINATIONS_API_KEY"),
    )


def _auth_header() -> dict:
    return {"Authorization": f"Bearer {env('POLLINATIONS_API_KEY')}"}


# ---------------------------------------------------------------------------
# Step 1: AI content generation with model fallback
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = textwrap.dedent("""
    You are a cheerful VTuber named Miku (Hatsune Miku). You create short,
    engaging YouTube Shorts (under 55 seconds when spoken — roughly 130 words or fewer).

    Your task: {topic_instruction}. Write the full spoken script and all metadata.

    Respond ONLY with a valid JSON object in this exact format (no markdown, no fences):
    {{
      "title": "Catchy title max 80 chars ending with #Shorts",
      "description": "Multi-paragraph YouTube description with emojis, subscribe CTA, and a trailing hashtag block of at least 15 hashtags. Format:\\n\\n[Hook sentence]\\n\\n[2-3 body sentences]\\n\\n━━━━━━━━━━━━━━━━━━━━━━━━\\n✨ LIKE & SUBSCRIBE for daily VTuber content!\\n🔔 Turn on notifications!\\n💬 Comment below!\\n━━━━━━━━━━━━━━━━━━━━━━━━\\n\\n#Shorts #VTuber #Anime #Miku [add 12+ more relevant hashtags]",
      "tags": ["tag1", "tag2", "add 20 to 30 relevant tags here"],
      "script": "Full spoken script approximately 130 words. Lively and positive.",
      "music_prompt": "Short prompt for upbeat ambient background music that fits the topic mood."
    }}
""").strip()


def build_system_prompt() -> str:
    custom_topic = os.environ.get("CUSTOM_TOPIC", "").strip()
    topic_instruction = (
        f'create a short video about: "{custom_topic}"'
        if custom_topic
        else "autonomously decide a fun, trending topic for today's short video"
    )
    return SYSTEM_PROMPT_TEMPLATE.format(topic_instruction=topic_instruction)


def _parse_json_response(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def ai_generate_content() -> dict:
    print("[1/4] Asking AI to generate content + SEO metadata …")
    client = pollinations_client()
    system_prompt = build_system_prompt()
    last_error: Optional[Exception] = None

    for model in TEXT_MODEL_FALLBACK:
        try:
            print(f"    Trying model: {model}")
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system_prompt}],
                temperature=0.9,
                max_tokens=900,
            )
            raw = response.choices[0].message.content.strip()
            data = _parse_json_response(raw)

            required_keys = ("title", "description", "tags", "script", "music_prompt")
            missing = [k for k in required_keys if k not in data]
            if missing:
                raise ValueError(f"Missing JSON keys: {missing}")

            if len(data["script"]) > 2000:
                data["script"] = data["script"][:2000]

            print(f"    ✓ Model {model} succeeded")
            print(f"    Title : {data['title']}")
            return data

        except Exception as exc:
            print(f"    ✗ Model {model} failed: {exc}")
            last_error = exc
            time.sleep(1)

    print(f"[ERROR] All text models failed. Last error: {last_error}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Motion-cue generation — analyse script text to schedule VTuber reactions
# ---------------------------------------------------------------------------

# Words that map to specific motion groups
_GREETING_WORDS  = {"hello", "hi", "hey", "greetings", "howdy", "welcome", "konnichiwa", "ohayo"}
_EXCITED_WORDS   = {
    "wow", "amazing", "incredible", "awesome", "excited", "exciting",
    "great", "fantastic", "wonderful", "omg", "yes", "yay", "hooray",
    "love", "best", "perfect", "epic", "fire", "insane",
}
_SAD_WORDS       = {
    "sad", "sorry", "unfortunate", "sadly", "unfortunately",
    "miss", "missed", "crying", "terrible", "bad", "awful",
}
_SURPRISE_WORDS  = {
    "wait", "what", "really", "seriously", "unbelievable", "surprise",
    "suddenly", "unexpected", "actually", "crazy", "shocking",
}

# Minimum number of frames to keep between motion triggers (avoids hammering
# the motion manager with rapid consecutive calls on the same group).
_MIN_FRAMES_BETWEEN_CUES = 45  # ~1.5 s at 30 fps


def generate_motion_cues(script: str, duration: float, fps: int) -> list[dict]:
    """
    Analyse *script* and return a list of motion-cue dicts:
      {"frameIndex": int, "group": str, "motionIndex": int}

    Strategy
    --------
    • Estimate per-word timing by dividing *duration* evenly across all words
      (rough but sufficient given that motion triggers are blended by the
      Live2D motion manager anyway).
    • First 5 words: detect greeting → schedule a Wave.
    • All words: detect excited, sad, or surprise vocabulary → schedule the
      matching motion group.
    • Every ~8 s of silence (no non-idle cue in the window): insert an Idle
      reset so the animation does not stagnate on a single pose.
    • Deduplicate: skip a cue if the same group fired within _MIN_FRAMES_BETWEEN_CUES.
    """
    words = script.split()
    total_words = len(words)
    if total_words == 0 or duration <= 0:
        return []

    word_duration = duration / total_words  # seconds per word
    cues: list[dict] = []
    last_frame_by_group: dict[str, int] = {}

    def _clean(w: str) -> str:
        return re.sub(r"[^a-z]", "", w.lower())

    def _add_cue(frame_idx: int, group: str, motion_idx: int = 0) -> bool:
        """Return True if the cue was added (not suppressed by cooldown)."""
        fi = max(0, frame_idx)
        if fi - last_frame_by_group.get(group, -9999) < _MIN_FRAMES_BETWEEN_CUES:
            return False
        cues.append({"frameIndex": fi, "group": group, "motionIndex": motion_idx})
        last_frame_by_group[group] = fi
        return True

    def _alternate_idx() -> int:
        """Return 0 or 1 alternating with each call, for motion variety."""
        return len(cues) % 2

    # ── Scan through every word ──────────────────────────────────────────────
    last_non_idle_frame = -9999
    idle_interval_frames = int(fps * 8)  # insert Idle reset every ~8 s

    for word_idx, word in enumerate(words):
        clean = _clean(word)
        frame_idx = int(word_idx * word_duration * fps)

        if word_idx < 5 and clean in _GREETING_WORDS:
            # Greeting in the first 5 words → Wave (index 0)
            if _add_cue(frame_idx, "Wave", 0):
                last_non_idle_frame = frame_idx

        elif clean in _EXCITED_WORDS:
            if _add_cue(frame_idx, "FlickUp", 0):
                last_non_idle_frame = frame_idx

        elif clean in _SAD_WORDS:
            # Alternate between two Tap motions for variety
            if _add_cue(frame_idx, "Tap", _alternate_idx()):
                last_non_idle_frame = frame_idx

        elif clean in _SURPRISE_WORDS:
            if _add_cue(frame_idx, "Flick", _alternate_idx()):
                last_non_idle_frame = frame_idx

        # Periodic Idle reset to prevent the model freezing on one pose
        if frame_idx - last_non_idle_frame >= idle_interval_frames:
            idle_idx = len([c for c in cues if c["group"] == "Idle"]) % 3
            if _add_cue(frame_idx, "Idle", idle_idx):
                pass  # Idle resets don't count as "non-idle"

    print(f"[MotionCues] Generated {len(cues)} motion cue(s):")
    for c in cues:
        print(f"    frame {c['frameIndex']:4d}  {c['group']} [{c['motionIndex']}]")

    return cues


# ---------------------------------------------------------------------------
# Step 2: Render the original Live2D Miku model via Puppeteer (v2 script)
# ---------------------------------------------------------------------------


def capture_live2d_video(
    duration_secs: float,
    video_path: Path,
    motion_cues: list[dict],
) -> None:
    """
    Start a local HTTP server, then invoke capture_live2d_v2.js with the
    motion-cue schedule JSON so the model reacts to the spoken content.
    """
    print("[2/4] Rendering Live2D Miku model (v2) via Puppeteer …")

    capture_script = REPO_ROOT / "scripts" / "capture_live2d_v2.js"
    if not capture_script.exists():
        print(f"[ERROR] Capture script not found: {capture_script}", file=sys.stderr)
        sys.exit(1)

    server = subprocess.Popen(
        [
            sys.executable, "-m", "http.server", str(CAPTURE_PORT),
            "--directory", str(REPO_ROOT),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"    HTTP server started on port {CAPTURE_PORT}")
    time.sleep(2)

    motion_cues_json = json.dumps(motion_cues)

    try:
        run([
            "node",
            str(capture_script),
            str(CAPTURE_PORT),
            str(video_path),
            str(duration_secs),
            str(CAPTURE_FPS),
            CAPTURE_PRESET,
            motion_cues_json,
        ])
    finally:
        server.terminate()
        server.wait()
        print("    HTTP server stopped")

    if not video_path.exists() or video_path.stat().st_size < 1024:
        print("[ERROR] Live2D capture produced no valid video.", file=sys.stderr)
        sys.exit(1)

    print(f"    ✓ Live2D video captured ({video_path.stat().st_size // 1024} KB)")


# ---------------------------------------------------------------------------
# Step 3: TTS via Pollinations audio API
# ---------------------------------------------------------------------------


def generate_tts(script: str, audio_path: Path) -> None:
    print("[3/4] Generating TTS audio …")
    last_error: Optional[Exception] = None

    for tts_model in TTS_MODEL_FALLBACK:
        try:
            print(f"    Trying TTS model: {tts_model}")
            client = pollinations_client()
            with client.audio.speech.with_streaming_response.create(
                model=tts_model,
                voice=TTS_VOICE,
                input=script,
                response_format="mp3",
            ) as response:
                response.stream_to_file(str(audio_path))
            print(f"    ✓ TTS saved ({audio_path.stat().st_size // 1024} KB) via {tts_model}")
            return

        except Exception as exc:
            print(f"    ✗ TTS model {tts_model} failed: {exc}")
            last_error = exc
            time.sleep(1)

    # Last-resort GET fallback
    try:
        print("    Trying GET /audio/{text} fallback …")
        encoded = urllib.parse.quote(script[:500], safe="")
        resp = requests.get(
            f"{POLLINATIONS_BASE}/audio/{encoded}",
            params={"voice": TTS_VOICE},
            headers=_auth_header(),
            timeout=60,
        )
        resp.raise_for_status()
        audio_path.write_bytes(resp.content)
        print(f"    ✓ TTS saved via GET fallback ({audio_path.stat().st_size // 1024} KB)")
        return

    except Exception as exc:
        print(f"    ✗ GET TTS fallback failed: {exc}")

    print(f"[ERROR] All TTS methods failed. Last error: {last_error}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Step 4: Background music via Pollinations (ACE-Step)
# ---------------------------------------------------------------------------


def generate_music(prompt: str, duration_secs: int, music_path: Path) -> bool:
    print("[4/4] Generating background music …")
    clamped = min(max(int(duration_secs) + 5, 5), 30)
    try:
        encoded = urllib.parse.quote(prompt, safe="")
        resp = requests.get(
            f"{POLLINATIONS_BASE}/audio/{encoded}",
            params={"model": "acestep", "duration": clamped},
            headers=_auth_header(),
            timeout=120,
        )
        resp.raise_for_status()
        if len(resp.content) < 1024:
            raise ValueError("Response too small")
        music_path.write_bytes(resp.content)
        print(f"    ✓ Music saved ({music_path.stat().st_size // 1024} KB, {clamped}s)")
        return True
    except Exception as exc:
        print(f"    ⚠ Music generation failed: {exc} — video will use TTS-only audio")
        return False


# ---------------------------------------------------------------------------
# Video composition with FFmpeg  (FontSize = 14)
# ---------------------------------------------------------------------------


def get_audio_duration(audio_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def build_subtitle_file(script: str, duration: float, srt_path: Path) -> None:
    words = script.split()
    chunk_size = 3
    chunks = [" ".join(words[i: i + chunk_size]) for i in range(0, len(words), chunk_size)]
    n = len(chunks)
    segment = duration / n if n else duration

    def fmt(s: float) -> str:
        h, r = divmod(s, 3600)
        m, r = divmod(r, 60)
        sec = int(r)
        ms = int((r % 1) * 1000)
        return f"{int(h):02d}:{int(m):02d}:{sec:02d},{ms:03d}"

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, chunk in enumerate(chunks):
            f.write(f"{i + 1}\n{fmt(i * segment)} --> {fmt((i + 1) * segment)}\n{chunk}\n\n")


def compose_video(
    live2d_video_path: Path,
    audio_path: Path,
    music_path: Optional[Path],
    srt_path: Path,
    output_path: Path,
) -> None:
    """
    FFmpeg pipeline (v2):
      • FontSize changed from 38 to 14 in the burned-in subtitle style.
      • All other settings identical to v1 (veryfast, CRF 23, 30 fps).
    """
    print("[Compose] Composing final video with FFmpeg (v2) …")

    speech_duration = get_audio_duration(audio_path)
    total_duration  = speech_duration + AUDIO_BUFFER_SECONDS

    srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")

    # FontSize=14 (reduced from 38 in generate_original_short_repo.py v1)
    video_filter = (
        f"[0:v]"
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={VIDEO_FPS},"
        f"drawbox=y=ih-80:color=0x000000AA:width=iw:height=80:t=fill,"
        f"subtitles={srt_escaped}:force_style='"
        f"FontName=Liberation Sans,FontSize=14,Bold=1,"
        f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,"
        f"Shadow=1,Alignment=2,MarginV=18'"
        f"[outv]"
    )

    has_music = music_path is not None and music_path.exists()

    if has_music:
        input_args = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", str(live2d_video_path),
            "-i", str(audio_path),
            "-stream_loop", "-1", "-i", str(music_path),
        ]
        audio_filter = (
            f"[2:a]atrim=duration={total_duration},asetpts=PTS-STARTPTS[bgm];"
            f"[1:a][bgm]amix=inputs=2:weights='1.0 0.2':normalize=0[outa]"
        )
        filter_complex = video_filter + ";" + audio_filter
        audio_map = ["-map", "[outa]"]
    else:
        input_args = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", str(live2d_video_path),
            "-i", str(audio_path),
        ]
        filter_complex = video_filter
        audio_map = ["-map", "1:a"]

    cmd = input_args + [
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        *audio_map,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-t", str(total_duration),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]
    run(cmd)
    print(f"    ✓ Video saved: {output_path}")


# ---------------------------------------------------------------------------
# Intermediate-result cache
# ---------------------------------------------------------------------------

CACHE_DIR      = REPO_ROOT / "cache"
_CACHE_META    = CACHE_DIR / "meta_repo_v2.json"
_CACHE_CONTENT = CACHE_DIR / "content_repo_v2.json"
_CACHE_LIVE2D  = CACHE_DIR / "miku_live2d_repo_v2.mp4"
_CACHE_AUDIO   = CACHE_DIR / "speech_repo_v2.mp3"
_CACHE_MUSIC   = CACHE_DIR / "music_repo_v2.mp3"


def _read_meta() -> dict:
    try:
        return json.loads(_CACHE_META.read_text()) if _CACHE_META.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_meta(meta: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    _CACHE_META.write_text(json.dumps(meta, indent=2))


def cache_save_content(content: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    _CACHE_CONTENT.write_text(json.dumps(content, ensure_ascii=False, indent=2))
    meta = _read_meta()
    meta["content"] = True
    _write_meta(meta)


def cache_save_file(src: Path, dest: Path, stage: str) -> None:
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        shutil.copy2(src, dest)
        meta = _read_meta()
        meta[stage] = True
        _write_meta(meta)
    except OSError as exc:
        print(f"[Cache] Warning: could not cache {stage}: {exc}")


def cache_load() -> tuple[dict, dict]:
    meta = _read_meta()
    content: dict = {}
    if meta.get("content") and _CACHE_CONTENT.exists():
        try:
            content = json.loads(_CACHE_CONTENT.read_text())
        except (json.JSONDecodeError, OSError):
            meta.pop("content", None)
    return meta, content


def cache_clear() -> None:
    for path in [_CACHE_META, _CACHE_CONTENT, _CACHE_LIVE2D, _CACHE_AUDIO, _CACHE_MUSIC]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    print("[Cache] Cleared after successful run.")


# ---------------------------------------------------------------------------
# Repository save & log
# ---------------------------------------------------------------------------


def save_video_to_repo(video_path: Path, timestamp: str, content: dict) -> Path:
    videos_dir = REPO_ROOT / "videos"
    videos_dir.mkdir(exist_ok=True)

    dest = videos_dir / f"{timestamp}.mp4"
    shutil.copy2(video_path, dest)
    print(f"[Save] Video saved to repository: videos/{timestamp}.mp4")

    meta_dest = videos_dir / f"{timestamp}.json"
    metadata = {
        "timestamp": timestamp,
        "title": content.get("title", ""),
        "description": content.get("description", ""),
        "tags": content.get("tags", []),
        "script": content.get("script", ""),
        "vtuber": "original-live2d",
        "workflow": "repo-save-only-v2",
    }
    meta_dest.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Save] Metadata saved to repository: videos/{timestamp}.json")

    return dest


def write_log_entry(
    timestamp: str,
    content: dict,
    repo_video_path: Optional[Path],
) -> None:
    logs_dir = REPO_ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / "upload_log.md"

    script_preview = content.get("script", "").replace("\n", " ").strip()
    if len(script_preview) > 200:
        script_preview = script_preview[:197] + "…"

    if repo_video_path is not None:
        video_rel = repo_video_path.relative_to(REPO_ROOT)
        meta_rel  = video_rel.with_suffix(".json")
        video_cell = f"[{video_rel}]({video_rel})"
        meta_cell  = f"[{meta_rel}]({meta_rel})"
    else:
        video_cell = "N/A (generation failed before save)"
        meta_cell  = "N/A"

    date_display = timestamp.replace("_", " ").replace("-", ":", 2)

    entry = (
        f"\n## {date_display} UTC [original-vtuber-repo-v2]\n\n"
        f"| Field | Value |\n"
        f"|---|---|\n"
        f"| **Title** | {content.get('title', 'N/A')} |\n"
        f"| **Video** | {video_cell} |\n"
        f"| **Metadata** | {meta_cell} |\n"
        f"| **Status** | ✅ Saved to repository (v2) |\n"
        f"| **Script preview** | {script_preview} |\n\n"
        f"---\n"
    )

    if not log_file.exists():
        log_file.write_text(
            "# VTuber Short Upload Log\n\n"
            "Each row is one automated run. Newest entries are at the bottom.\n",
            encoding="utf-8",
        )

    with log_file.open("a", encoding="utf-8") as f:
        f.write(entry)

    print("[Log] Entry written to logs/upload_log.md")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    repo_video_path: Optional[Path] = None

    cache_meta, cached_content = cache_load()
    if cache_meta:
        print("[Cache] Resuming from a previous partial run …")

    with tempfile.TemporaryDirectory(prefix="vtuber_original_repo_v2_") as tmpdir:
        tmp         = Path(tmpdir)
        live2d_path = tmp / "miku_live2d.mp4"
        audio_path  = tmp / "speech.mp3"
        music_path  = tmp / "music.mp3"
        srt_path    = tmp / "subtitles.srt"
        video_path  = tmp / "short.mp4"

        # 1. AI: topic, script, SEO metadata
        if cached_content:
            print(f"[1/4] Re-using cached AI content (title: {cached_content.get('title', '?')}) …")
            content = cached_content
        else:
            content = ai_generate_content()
            cache_save_content(content)

        # 2. Render Live2D Miku model with motion cues
        # Motion cues are derived here so they are available even when
        # re-using a cached Live2D clip (the cues only affect capture).
        motion_cues = generate_motion_cues(
            content["script"],
            duration=CAPTURE_DURATION_SECS,
            fps=CAPTURE_FPS,
        )

        if cache_meta.get("live2d") and _CACHE_LIVE2D.exists():
            print("[2/4] Re-using cached Live2D video …")
            shutil.copy2(_CACHE_LIVE2D, live2d_path)
        else:
            capture_live2d_video(
                duration_secs=CAPTURE_DURATION_SECS,
                video_path=live2d_path,
                motion_cues=motion_cues,
            )
            cache_save_file(live2d_path, _CACHE_LIVE2D, "live2d")

        # 3. TTS
        if cache_meta.get("audio") and _CACHE_AUDIO.exists():
            print("[3/4] Re-using cached TTS audio …")
            shutil.copy2(_CACHE_AUDIO, audio_path)
        else:
            generate_tts(content["script"], audio_path)
            cache_save_file(audio_path, _CACHE_AUDIO, "audio")

        # 4. Background music (optional) + video composition
        speech_dur = get_audio_duration(audio_path)
        if cache_meta.get("music") and _CACHE_MUSIC.exists():
            print("[4/4] Re-using cached background music …")
            shutil.copy2(_CACHE_MUSIC, music_path)
            music_ok = True
        else:
            music_ok = generate_music(content["music_prompt"], int(speech_dur), music_path)
            if music_ok:
                cache_save_file(music_path, _CACHE_MUSIC, "music")

        # Video composition (FontSize=14)
        build_subtitle_file(content["script"], speech_dur, srt_path)
        compose_video(
            live2d_video_path=live2d_path,
            audio_path=audio_path,
            music_path=music_path if music_ok else None,
            srt_path=srt_path,
            output_path=video_path,
        )

        # Save to repository
        repo_video_path = save_video_to_repo(video_path, timestamp, content)

    # Write log entry after temp dir is cleaned up
    write_log_entry(timestamp, content, repo_video_path)

    # Clear cache only after a fully successful run
    cache_clear()
    print("[✓] Done!")


if __name__ == "__main__":
    main()
