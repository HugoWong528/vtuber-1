#!/usr/bin/env python3
"""
VTuber Original Short Generator
================================
Pipeline:
  1. AI (Pollinations text API) picks a topic, writes a spoken script,
     and generates full SEO metadata + music prompt.
  2. The original Miku Live2D model (already in the repository) is rendered
     in headless Chromium via Puppeteer and captured as an MP4 clip.
  3. Pollinations audio API synthesises the script via ElevenLabs TTS.
  4. Pollinations audio API generates a short ambient music loop.
  5. FFmpeg composes the final YouTube Short:
       • Original Miku Live2D animation (looped to TTS duration)
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
CAPTURE_FPS     = 10   # frames per second for Puppeteer capture (lower = faster)
CAPTURE_PORT    = 8787 # local HTTP server port for serving the repo
VIDEO_WIDTH     = 1080
VIDEO_HEIGHT    = 1920
VIDEO_FPS       = 30   # output video FPS
AUDIO_BUFFER_SECONDS = 0.5

# YouTube
YOUTUBE_CATEGORY_ID = "22"   # People & Blogs
YOUTUBE_PRIVACY     = "public"
REQUIRED_YOUTUBE_CREDENTIALS = (
    "YOUTUBE_REFRESH_TOKEN",
    "YOUTUBE_CLIENT_ID",
    "YOUTUBE_CLIENT_SECRET",
)

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
# Step 2: Render the original Live2D Miku model via Puppeteer
# ---------------------------------------------------------------------------


def capture_live2d_video(duration_secs: float, video_path: Path) -> None:
    """
    Start a local HTTP server (Python's built-in http.server) rooted at the
    repository, then invoke the Node.js Puppeteer script to capture the
    Live2D Miku model and encode it into an MP4 file.
    """
    print("[2/5] Rendering original Live2D Miku model via Puppeteer …")

    capture_script = REPO_ROOT / "scripts" / "capture_live2d.js"
    if not capture_script.exists():
        print(f"[ERROR] Capture script not found: {capture_script}", file=sys.stderr)
        sys.exit(1)

    # Start local HTTP server so Puppeteer can load model files over http://
    server = subprocess.Popen(
        [
            sys.executable, "-m", "http.server", str(CAPTURE_PORT),
            "--directory", str(REPO_ROOT),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"    HTTP server started on port {CAPTURE_PORT}")
    time.sleep(2)  # give server time to bind

    try:
        run([
            "node",
            str(capture_script),
            str(CAPTURE_PORT),
            str(video_path),
            str(duration_secs),
            str(CAPTURE_FPS),
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
    print("[3/5] Generating TTS audio …")
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
    print("[4/5] Generating background music …")
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
    words = script.split()
    chunk_size = 6
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
    FFmpeg pipeline:
      • Input 0 : Live2D Miku video (stream-looped to TTS duration)
      • Input 1 : TTS speech audio
      • Input 2 : BGM audio (optional, stream-looped)
    Filters:
      • Translucent bottom bar for subtitle readability
      • Styled burned-in subtitles (white, bold, 38pt, outline) synced to speech
      • Audio: TTS full volume + BGM at 20%, mixed
    """
    print("[5/5] Composing final video with FFmpeg …")

    speech_duration = get_audio_duration(audio_path)
    total_duration  = speech_duration + AUDIO_BUFFER_SECONDS

    srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")

    # Video filter: subtitle bar + burned-in captions on the Live2D video
    video_filter = (
        f"[0:v]"
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={VIDEO_FPS},"
        f"drawbox=y=ih-310:color=0x000000AA:width=iw:height=310:t=fill,"
        f"subtitles={srt_escaped}:force_style='"
        f"FontName=Liberation Sans,FontSize=38,Bold=1,"
        f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=3,"
        f"Shadow=1,Alignment=2,MarginV=55'"
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

    missing = [v for v in REQUIRED_YOUTUBE_CREDENTIALS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"YouTube credential(s) not set: {', '.join(missing)} — "
            "upload skipped. The video is saved in videos/ for manual upload."
        )

    youtube = get_youtube_service()
    title = _prepare_youtube_title(content["title"])

    all_tags = list(dict.fromkeys(
        content.get("tags", [])
        + ["VTuber", "Shorts", "Miku", "HatsuneMiku", "Anime", "Live2D", "OriginalVTuber"]
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
# Repository backup & log
# ---------------------------------------------------------------------------


def save_video_to_repo(video_path: Path, timestamp: str, content: dict) -> Path:
    videos_dir = REPO_ROOT / "videos"
    videos_dir.mkdir(exist_ok=True)

    dest = videos_dir / f"{timestamp}.mp4"
    shutil.copy2(video_path, dest)
    print(f"[Backup] Video saved to repository: videos/{timestamp}.mp4")

    meta_dest = videos_dir / f"{timestamp}.json"
    metadata = {
        "timestamp": timestamp,
        "title": _prepare_youtube_title(content.get("title", "")),
        "description": content.get("description", ""),
        "tags": content.get("tags", []),
        "script": content.get("script", ""),
        "youtube_category_id": YOUTUBE_CATEGORY_ID,
        "privacy": YOUTUBE_PRIVACY,
        "vtuber": "original-live2d",
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
        meta_rel  = video_rel.with_suffix(".json")
        video_cell = f"[{video_rel}]({video_rel})"
        meta_cell  = f"[{meta_rel}]({meta_rel})"
    else:
        video_cell = "N/A (generation failed before save)"
        meta_cell  = "N/A"

    date_display = timestamp.replace("_", " ").replace("-", ":", 2)

    entry = (
        f"\n## {date_display} UTC [original-vtuber]\n\n"
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

    print("[Log] Entry written to logs/upload_log.md")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    repo_video_path: Optional[Path] = None

    with tempfile.TemporaryDirectory(prefix="vtuber_original_") as tmpdir:
        tmp         = Path(tmpdir)
        live2d_path = tmp / "miku_live2d.mp4"
        audio_path  = tmp / "speech.mp3"
        music_path  = tmp / "music.mp3"
        srt_path    = tmp / "subtitles.srt"
        video_path  = tmp / "short.mp4"

        # 1. AI: topic, script, SEO metadata
        content = ai_generate_content()

        # 2. Render original Live2D Miku model
        # We capture slightly longer than the typical script (~70 s) so the
        # video can always be looped to fit any TTS duration.
        capture_live2d_video(duration_secs=70, video_path=live2d_path)

        # 3. TTS
        generate_tts(content["script"], audio_path)

        # 4. Background music (optional)
        speech_dur = get_audio_duration(audio_path)
        music_ok   = generate_music(content["music_prompt"], int(speech_dur), music_path)

        # 5. Video composition
        build_subtitle_file(content["script"], speech_dur, srt_path)
        compose_video(
            live2d_video_path=live2d_path,
            audio_path=audio_path,
            music_path=music_path if music_ok else None,
            srt_path=srt_path,
            output_path=video_path,
        )

        # 5b. Back up to repository before attempting upload
        repo_video_path = save_video_to_repo(video_path, timestamp, content)

        # 6. Upload to YouTube
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

    write_log_entry(timestamp, content, repo_video_path, youtube_url, upload_error)

    if upload_error:
        sys.exit(1)

    print("[✓] Done!")


if __name__ == "__main__":
    main()
