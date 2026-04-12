#!/usr/bin/env python3
"""
VTuber Auto-Short Generator — Pollinations Edition
====================================================
Pipeline:
  1. AI (Pollinations text API, non-paid models with auto-fallback) picks a topic,
     writes a spoken script, and generates full SEO metadata + image/music prompts.
  2. Pollinations image API generates a vivid portrait (1080×1920) background scene.
  3. Pollinations audio API synthesises the script via ElevenLabs TTS.
  4. Pollinations audio API generates a short ambient music loop via ACE-Step.
  5. FFmpeg composes a high-quality YouTube Short:
       • Ken Burns slow-zoom on AI-generated background
       • Styled burned-in subtitles synced to speech
       • TTS audio mixed with ducked background music (20%)
  6. YouTube Data API v3 uploads the video with optimised SEO metadata.

Required GitHub Secrets:
  POLLINATIONS_API_KEY   – from https://enter.pollinations.ai
  YOUTUBE_CLIENT_ID      – Google Cloud OAuth2 client ID
  YOUTUBE_CLIENT_SECRET  – Google Cloud OAuth2 client secret
  YOUTUBE_REFRESH_TOKEN  – Long-lived refresh token (youtube.upload scope)
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
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
AVATAR_FALLBACK = REPO_ROOT / "texture_00.png"

POLLINATIONS_BASE = "https://gen.pollinations.ai"
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

# Non-paid image models — tried in order until one succeeds
IMAGE_MODEL_FALLBACK = ["flux", "zimage", "klein", "wan-image"]

# TTS model fallback
TTS_MODEL_FALLBACK = ["elevenlabs", "openai"]
TTS_VOICE = "nova"  # bright, energetic voice

# Video
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS = 30
AUDIO_BUFFER_SECONDS = 0.5

# YouTube
YOUTUBE_CATEGORY_ID = "22"   # People & Blogs
YOUTUBE_PRIVACY = "public"   # "public" | "private" | "unlisted"
REQUIRED_YOUTUBE_CREDENTIALS = ("YOUTUBE_REFRESH_TOKEN", "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET")

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
    """Return an OpenAI SDK client pointed at the Pollinations v1 base."""
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
    You are a cheerful VTuber named Miku (Hatsune Miku style). You create short,
    engaging YouTube Shorts (under 55 seconds when spoken — roughly 130 words or fewer).

    Your task: {topic_instruction}. Write the full spoken script and all metadata.

    Respond ONLY with a valid JSON object in this exact format (no markdown, no fences):
    {{
      "title": "Catchy title max 80 chars ending with #Shorts",
      "description": "Multi-paragraph YouTube description with emojis, subscribe CTA, and a trailing hashtag block of at least 15 hashtags. Format:\\n\\n[Hook sentence]\\n\\n[2-3 body sentences]\\n\\n━━━━━━━━━━━━━━━━━━━━━━━━\\n✨ LIKE & SUBSCRIBE for daily VTuber content!\\n�� Turn on notifications!\\n💬 Comment below!\\n━━━━━━━━━━━━━━━━━━━━━━━━\\n\\n#Shorts #VTuber #Anime #Miku [add 12+ more relevant hashtags]",
      "tags": ["tag1", "tag2", "add 20 to 30 relevant tags here"],
      "script": "Full spoken script approximately 130 words. Lively and positive.",
      "bg_prompt": "Detailed Pollinations image prompt for a vivid anime 9:16 portrait scene matching the video topic. Include an anime VTuber character with blue twin-tails, art style (cinematic anime, vibrant colors), lighting, mood, and environment. High quality, detailed.",
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
    """Strip optional markdown fences and parse JSON."""
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def ai_generate_content() -> dict:
    print("[1/5] Asking AI to generate content + SEO metadata …")
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

            required_keys = ("title", "description", "tags", "script", "bg_prompt", "music_prompt")
            missing = [k for k in required_keys if k not in data]
            if missing:
                raise ValueError(f"Missing JSON keys: {missing}")

            # Truncate script to TTS-safe length
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
# Step 2: Background image via Pollinations image API
# ---------------------------------------------------------------------------


def generate_background_image(prompt: str, output_path: Path) -> None:
    print("[2/5] Generating AI background image …")
    encoded = urllib.parse.quote(prompt, safe="")
    last_error: Optional[Exception] = None

    for model in IMAGE_MODEL_FALLBACK:
        try:
            print(f"    Trying image model: {model}")
            resp = requests.get(
                f"{POLLINATIONS_BASE}/image/{encoded}",
                params={
                    "model": model,
                    "width": VIDEO_WIDTH,
                    "height": VIDEO_HEIGHT,
                    "seed": -1,
                    "enhance": "true",
                    "safe": "true",
                },
                headers=_auth_header(),
                timeout=90,
            )
            resp.raise_for_status()
            if len(resp.content) < 1024:
                raise ValueError("Response too small — likely an error page")
            output_path.write_bytes(resp.content)
            print(f"    ✓ Image saved ({len(resp.content) // 1024} KB) via {model}")
            return

        except Exception as exc:
            print(f"    ✗ Image model {model} failed: {exc}")
            last_error = exc
            time.sleep(2)

    # Final fallback: use the repo avatar texture
    if AVATAR_FALLBACK.exists():
        import shutil
        shutil.copy(AVATAR_FALLBACK, output_path)
        print(f"    ⚠ All image models failed ({last_error}); using repo avatar as background")
    else:
        print(f"[ERROR] Background image generation failed: {last_error}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Step 3: TTS via Pollinations audio API
# ---------------------------------------------------------------------------


def generate_tts(script: str, audio_path: Path) -> None:
    print("[3/5] Generating TTS audio …")
    last_error: Optional[Exception] = None

    # Try OpenAI-compatible /v1/audio/speech endpoint first
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

    # Last-resort: GET /audio/{text}?voice=nova
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
    """Generate background music. Returns True on success, False on failure."""
    print("[4/5] Generating background music …")
    clamped = min(max(int(duration_secs) + 5, 5), 30)  # clamp to 5-30 s
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
# Step 5: Video composition with FFmpeg
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
    """Create an SRT file: ~6 words per cue, evenly timed."""
    words = script.split()
    chunk_size = 6
    chunks = [" ".join(words[i : i + chunk_size]) for i in range(0, len(words), chunk_size)]
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
    bg_image_path: Path,
    audio_path: Path,
    music_path: Optional[Path],
    srt_path: Path,
    output_path: Path,
) -> None:
    """
    FFmpeg pipeline:
      • Input 0 : background image (looped)
      • Input 1 : TTS speech audio
      • Input 2 : BGM audio (optional, stream-looped)
    Filters:
      • Ken Burns slow zoom-in (1.0 → 1.15) over full video duration
      • Translucent bottom bar for subtitle readability
      • Styled burned-in subtitles (white, bold, 38pt, outline)
      • Audio: TTS full volume + BGM at 20%, mixed
    """
    print("[5/5] Composing video with FFmpeg …")

    speech_duration = get_audio_duration(audio_path)
    total_duration = speech_duration + AUDIO_BUFFER_SECONDS
    total_frames = max(int(total_duration * VIDEO_FPS) + 1, 1)

    # Zoom increment to reach +15% over the video
    zoom_inc = 0.15 / total_frames
    # Scale background 20% larger than output to have panning room during zoom
    bg_w = int(VIDEO_WIDTH * 1.2)
    bg_h = int(VIDEO_HEIGHT * 1.2)

    srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")

    # ── video filter ─────────────────────────────────────────────────────────
    # [bg]: Ken Burns — scale large, crop, slow zoom centered
    bg_vf = (
        f"[0:v]"
        f"scale={bg_w}:{bg_h}:force_original_aspect_ratio=increase,"
        f"crop={bg_w}:{bg_h},"
        f"zoompan="
        f"z='min(zoom+{zoom_inc:.8f},1.15)':"
        f"x='iw/2-(iw/zoom/2)':"
        f"y='ih/2-(ih/zoom/2)':"
        f"d={total_frames}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={VIDEO_FPS}"
        f"[bg];"
    )
    # [outv]: subtitle bar + burned-in captions
    sub_vf = (
        f"[bg]"
        f"drawbox=y=ih-310:color=0x000000AA:width=iw:height=310:t=fill,"
        f"subtitles={srt_escaped}:force_style='"
        f"FontName=Liberation Sans,FontSize=38,Bold=1,"
        f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=3,"
        f"Shadow=1,Alignment=2,MarginV=55'"
        f"[outv]"
    )
    video_filter = bg_vf + sub_vf

    # ── inputs + audio filter ────────────────────────────────────────────────
    has_music = music_path is not None and music_path.exists()

    if has_music:
        input_args = [
            "ffmpeg", "-y",
            "-loop", "1", "-framerate", str(VIDEO_FPS), "-i", str(bg_image_path),
            "-i", str(audio_path),
            "-stream_loop", "-1", "-i", str(music_path),
        ]
        # Trim BGM to video length; mix speech (100%) + BGM (20%)
        audio_filter = (
            f"[2:a]atrim=duration={total_duration},asetpts=PTS-STARTPTS[bgm];"
            f"[1:a][bgm]amix=inputs=2:weights='1.0 0.2':normalize=0[outa]"
        )
        filter_complex = video_filter + ";" + audio_filter
        audio_map = ["-map", "[outa]"]
    else:
        input_args = [
            "ffmpeg", "-y",
            "-loop", "1", "-framerate", str(VIDEO_FPS), "-i", str(bg_image_path),
            "-i", str(audio_path),
        ]
        filter_complex = video_filter
        audio_map = ["-map", "1:a"]

    cmd = input_args + [
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        *audio_map,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-t", str(total_duration),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]
    run(cmd)
    print(f"    ✓ Video saved: {output_path}")


# ---------------------------------------------------------------------------
# YouTube upload
# ---------------------------------------------------------------------------


def _prepare_youtube_title(title: str) -> str:
    """Ensure the title has a #Shorts suffix and fits YouTube's 100-char limit."""
    if "#Shorts" not in title:
        title = f"{title} #Shorts"
    return title[:100]


def get_youtube_service():
    credentials = Credentials(
        token=None,
        refresh_token=env("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=env("YOUTUBE_CLIENT_ID"),
        client_secret=env("YOUTUBE_CLIENT_SECRET"),
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    credentials.refresh(Request())
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def upload_to_youtube(video_path: Path, content: dict) -> str:
    print("[Upload] Uploading to YouTube …")

    # Validate credentials up-front — raise a plain exception instead of sys.exit
    # so the caller can catch it and still write the log / keep the saved video.
    missing = [v for v in REQUIRED_YOUTUBE_CREDENTIALS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"YouTube credential(s) not set: {', '.join(missing)} — "
            "upload skipped. The video is saved in videos/ for manual upload."
        )

    youtube = get_youtube_service()

    title = _prepare_youtube_title(content["title"])

    # Deduplicate tags; enforce YouTube's 500-char total limit
    all_tags = list(dict.fromkeys(
        content.get("tags", [])
        + ["VTuber", "Shorts", "Miku", "HatsuneMiku", "Anime", "AIGenerated"]
    ))
    tags_trimmed: list[str] = []
    char_count = 0
    for tag in all_tags:
        if char_count + len(tag) + 2 <= 500:
            tags_trimmed.append(tag)
            char_count += len(tag) + 2

    body = {
        "snippet": {
            "title": title,
            "description": content["description"][:5000],
            "tags": tags_trimmed,
            "categoryId": YOUTUBE_CATEGORY_ID,
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": YOUTUBE_PRIVACY,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=10 * 1024 * 1024,
    )
    request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"    Upload: {int(status.progress() * 100)}%")

    video_id = response.get("id", "unknown")
    print(f"    ✓ Uploaded: https://www.youtube.com/shorts/{video_id}")
    return video_id


# ---------------------------------------------------------------------------
# Intermediate-result cache (persists between workflow runs via git commit)
# ---------------------------------------------------------------------------

CACHE_DIR = REPO_ROOT / "cache"
_CACHE_META = CACHE_DIR / "meta.json"
_CACHE_CONTENT = CACHE_DIR / "content.json"
_CACHE_BG = CACHE_DIR / "background.jpg"
_CACHE_AUDIO = CACHE_DIR / "speech.mp3"
_CACHE_MUSIC = CACHE_DIR / "music.mp3"


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
    """Return (meta, content). Both are empty dicts if cache is absent or corrupt."""
    meta = _read_meta()
    content: dict = {}
    if meta.get("content") and _CACHE_CONTENT.exists():
        try:
            content = json.loads(_CACHE_CONTENT.read_text())
        except (json.JSONDecodeError, OSError):
            meta.pop("content", None)
    return meta, content


def cache_clear() -> None:
    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
        print("[Cache] Cleared after successful run.")


# ---------------------------------------------------------------------------
# Repository backup & log
# ---------------------------------------------------------------------------


def save_video_to_repo(video_path: Path, timestamp: str, content: dict) -> Path:
    """Copy the generated video and its YouTube metadata into the repo's videos/ directory."""
    videos_dir = REPO_ROOT / "videos"
    videos_dir.mkdir(exist_ok=True)

    # Save the complete video file
    dest = videos_dir / f"{timestamp}.mp4"
    shutil.copy2(video_path, dest)
    print(f"[Backup] Video saved to repository: videos/{timestamp}.mp4")

    # Save a companion metadata JSON so the video can be manually uploaded to YouTube
    meta_dest = videos_dir / f"{timestamp}.json"
    metadata = {
        "timestamp": timestamp,
        "title": _prepare_youtube_title(content.get("title", "")),
        "description": content.get("description", ""),
        "tags": content.get("tags", []),
        "script": content.get("script", ""),
        "youtube_category_id": YOUTUBE_CATEGORY_ID,
        "privacy": YOUTUBE_PRIVACY,
    }
    meta_dest.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Backup] Metadata saved to repository: videos/{timestamp}.json")

    return dest


def write_log_entry(
    timestamp: str,
    content: dict,
    repo_video_path: Optional[Path],
    youtube_url: Optional[str],
    upload_error: Optional[str],
) -> None:
    """Append a run record to logs/upload_log.md."""
    logs_dir = REPO_ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / "upload_log.md"

    if youtube_url:
        status = f"✅ Uploaded — [{youtube_url}]({youtube_url})"
    else:
        status = f"❌ Upload failed — `{upload_error}`"

    script_preview = content.get("script", "").replace("\n", " ").strip()
    if len(script_preview) > 200:
        script_preview = script_preview[:197] + "…"

    if repo_video_path is not None:
        video_rel = repo_video_path.relative_to(REPO_ROOT)
        meta_rel = video_rel.with_suffix(".json")
        video_cell = f"[{video_rel}]({video_rel})"
        meta_cell = f"[{meta_rel}]({meta_rel})"
    else:
        video_cell = "N/A (generation failed before save)"
        meta_cell = "N/A"

    date_display = timestamp.replace("_", " ").replace("-", ":", 2)

    entry = (
        f"\n## {date_display} UTC\n\n"
        f"| Field | Value |\n"
        f"|---|---|\n"
        f"| **Title** | {content.get('title', 'N/A')} |\n"
        f"| **Video** | {video_cell} |\n"
        f"| **Metadata (for manual upload)** | {meta_cell} |\n"
        f"| **YouTube** | {status} |\n"
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

    print(f"[Log] Entry written to logs/upload_log.md")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    repo_video_path: Optional[Path] = None

    cache_meta, cached_content = cache_load()
    if cache_meta:
        print("[Cache] Resuming from a previous partial run …")

    with tempfile.TemporaryDirectory(prefix="vtuber_short_") as tmpdir:
        tmp = Path(tmpdir)
        bg_path = tmp / "background.jpg"
        audio_path = tmp / "speech.mp3"
        music_path = tmp / "music.mp3"
        srt_path = tmp / "subtitles.srt"
        video_path = tmp / "short.mp4"

        # 1. AI: topic, script, SEO metadata
        if cached_content:
            print(f"[1/5] Re-using cached AI content (title: {cached_content.get('title', '?')}) …")
            content = cached_content
        else:
            content = ai_generate_content()
            cache_save_content(content)

        # 2. AI-generated background image
        if cache_meta.get("image") and _CACHE_BG.exists():
            print("[2/5] Re-using cached background image …")
            shutil.copy2(_CACHE_BG, bg_path)
        else:
            generate_background_image(content["bg_prompt"], bg_path)
            cache_save_file(bg_path, _CACHE_BG, "image")

        # 3. TTS
        if cache_meta.get("audio") and _CACHE_AUDIO.exists():
            print("[3/5] Re-using cached TTS audio …")
            shutil.copy2(_CACHE_AUDIO, audio_path)
        else:
            generate_tts(content["script"], audio_path)
            cache_save_file(audio_path, _CACHE_AUDIO, "audio")

        # 4. Background music (optional — skip gracefully if fails)
        speech_dur = get_audio_duration(audio_path)
        if cache_meta.get("music") and _CACHE_MUSIC.exists():
            print("[4/5] Re-using cached background music …")
            shutil.copy2(_CACHE_MUSIC, music_path)
            music_ok = True
        else:
            music_ok = generate_music(content["music_prompt"], int(speech_dur), music_path)
            if music_ok:
                cache_save_file(music_path, _CACHE_MUSIC, "music")

        # 5. Video composition
        build_subtitle_file(content["script"], speech_dur, srt_path)
        compose_video(
            bg_image_path=bg_path,
            audio_path=audio_path,
            music_path=music_path if music_ok else None,
            srt_path=srt_path,
            output_path=video_path,
        )

        # 5b. Back up the video and metadata to the repository before attempting upload
        repo_video_path = save_video_to_repo(video_path, timestamp, content)

        # 6. Upload to YouTube (errors are caught so the log is always written)
        youtube_url: Optional[str] = None
        upload_error: Optional[str] = None
        try:
            video_id = upload_to_youtube(video_path, content)
            if video_id and video_id != "unknown":
                youtube_url = f"https://www.youtube.com/shorts/{video_id}"
            else:
                upload_error = f"Upload completed but no video ID returned (got: {video_id!r})"
        except Exception as exc:
            upload_error = str(exc)
            print(f"[ERROR] YouTube upload failed: {exc}", file=sys.stderr)

    # Write log entry after temp dir is cleaned up (video is safely in repo)
    write_log_entry(timestamp, content, repo_video_path, youtube_url, upload_error)

    if upload_error:
        sys.exit(1)

    # Clear cache only after a fully successful run
    cache_clear()
    print("[✓] Done!")


if __name__ == "__main__":
    main()
