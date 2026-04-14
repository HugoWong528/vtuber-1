#!/usr/bin/env python3
"""
VTuber Original Short Generator — Repository Save Only
=======================================================
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
  6. Video is committed directly to the repository (no YouTube upload).

Speed optimisations vs. generate_original_short.py:
  • Puppeteer captures at 30 fps for 30 s instead of 60 fps for 70 s
    → reduces screenshot round-trips by ~4.3×.
  • FFmpeg capture preset changed to ``veryfast`` (passed as CLI arg to
    capture_live2d.js).
  • FFmpeg composition preset changed to ``veryfast`` with CRF 23.
  • Output video is 30 fps instead of 60 fps.

Required GitHub Secret:
  POLLINATIONS_API_KEY   – from https://enter.pollinations.ai
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

# Live2D capture settings — lower fps/duration for faster CI runs
CAPTURE_FPS           = 30    # frames per second (30 is sufficient for smooth output)
CAPTURE_DURATION_SECS = 30    # seconds to capture (looped to TTS length, so 30 s is enough)
CAPTURE_PRESET        = "veryfast"  # FFmpeg preset passed to capture_live2d.js
CAPTURE_PORT          = 8787  # local HTTP server port

VIDEO_WIDTH  = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS    = 30   # output video frame rate

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
# Step 2: Render the original Live2D Miku model via Puppeteer
# ---------------------------------------------------------------------------


def capture_live2d_video(duration_secs: float, video_path: Path) -> None:
    """
    Start a local HTTP server (Python's built-in http.server) rooted at the
    repository, then invoke the Node.js Puppeteer script to capture the
    Live2D Miku model and encode it into an MP4 file.

    Passes CAPTURE_PRESET to capture_live2d.js so encoding uses a fast
    FFmpeg preset during the capture stage as well.
    """
    print("[2/4] Rendering original Live2D Miku model via Puppeteer …")

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
            CAPTURE_PRESET,
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
# Video composition with FFmpeg
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
    FFmpeg pipeline:
      • Input 0 : Live2D Miku video (stream-looped to TTS duration)
      • Input 1 : TTS speech audio
      • Input 2 : BGM audio (optional, stream-looped)
    Filters:
      • Translucent bottom bar for subtitle readability
      • Styled burned-in subtitles (white, bold, 38pt, outline) synced to speech
      • Audio: TTS full volume + BGM at 20%, mixed
    Uses veryfast preset and CRF 23 for significantly faster encoding.
    """
    print("[Compose] Composing final video with FFmpeg …")

    speech_duration = get_audio_duration(audio_path)
    total_duration  = speech_duration + AUDIO_BUFFER_SECONDS

    srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")

    # Video filter: subtitle bar + burned-in captions on the Live2D video
    video_filter = (
        f"[0:v]"
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={VIDEO_FPS},"
        f"drawbox=y=ih-80:color=0x000000AA:width=iw:height=80:t=fill,"
        f"subtitles={srt_escaped}:force_style='"
        f"FontName=Liberation Sans,FontSize=38,Bold=1,"
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
# Intermediate-result cache (persists between workflow runs via git commit)
# ---------------------------------------------------------------------------

CACHE_DIR = REPO_ROOT / "cache"
_CACHE_META    = CACHE_DIR / "meta_repo.json"
_CACHE_CONTENT = CACHE_DIR / "content_repo.json"
_CACHE_LIVE2D  = CACHE_DIR / "miku_live2d_repo.mp4"
_CACHE_AUDIO   = CACHE_DIR / "speech_repo.mp3"
_CACHE_MUSIC   = CACHE_DIR / "music_repo.mp3"


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
        "workflow": "repo-save-only",
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
        f"\n## {date_display} UTC [original-vtuber-repo]\n\n"
        f"| Field | Value |\n"
        f"|---|---|\n"
        f"| **Title** | {content.get('title', 'N/A')} |\n"
        f"| **Video** | {video_cell} |\n"
        f"| **Metadata** | {meta_cell} |\n"
        f"| **Status** | ✅ Saved to repository |\n"
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

    with tempfile.TemporaryDirectory(prefix="vtuber_original_repo_") as tmpdir:
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

        # 2. Render original Live2D Miku model
        # Capture CAPTURE_DURATION_SECS of animation; it is looped to TTS length
        # during composition so a 30-second clip is sufficient for any short.
        if cache_meta.get("live2d") and _CACHE_LIVE2D.exists():
            print("[2/4] Re-using cached Live2D video …")
            shutil.copy2(_CACHE_LIVE2D, live2d_path)
        else:
            capture_live2d_video(duration_secs=CAPTURE_DURATION_SECS, video_path=live2d_path)
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

        # Video composition
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

    # Write log entry after temp dir is cleaned up (video is safely in repo)
    write_log_entry(timestamp, content, repo_video_path)

    # Clear cache only after a fully successful run
    cache_clear()
    print("[✓] Done!")


if __name__ == "__main__":
    main()
