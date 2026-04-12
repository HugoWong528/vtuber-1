#!/usr/bin/env python3
"""
VTuber Auto-Short Generator
----------------------------
Pipeline:
  1. Ask the AI to autonomously decide a topic and write a short script.
  2. Convert the script to speech with OpenAI TTS.
  3. Compose a YouTube Short video (portrait 9:16) with FFmpeg:
       - Animated avatar loop (texture_00.png → video loop)
       - TTS audio
       - Burned-in subtitles
  4. Upload the video to YouTube via the Data API v3.

Required environment variables (set as GitHub Secrets):
  OPENAI_API_KEY          – OpenAI API key (used for both chat and TTS)
  YOUTUBE_CLIENT_ID       – OAuth2 client ID for YouTube Data API
  YOUTUBE_CLIENT_SECRET   – OAuth2 client secret
  YOUTUBE_REFRESH_TOKEN   – Long-lived OAuth2 refresh token
  YOUTUBE_CHANNEL_ID      – (optional) target channel id for tagging
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from openai import OpenAI
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
AVATAR_IMAGE = REPO_ROOT / "texture_00.png"

# YouTube Short dimensions
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS = 30

AUDIO_BUFFER_SECONDS = 0.5  # small buffer so audio doesn't clip at end
MAX_SCRIPT_CHARS = 4096

# YouTube upload settings
YOUTUBE_CATEGORY_ID = "22"   # People & Blogs
YOUTUBE_PRIVACY = "public"   # "public" | "private" | "unlisted"
SHORT_HASHTAG = "#Shorts"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "")
    if required and not value:
        print(f"[ERROR] Environment variable '{name}' is not set.", file=sys.stderr)
        sys.exit(1)
    return value


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a shell command, streaming output, and raise on failure."""
    print(f"[CMD] {' '.join(cmd)}")
    result = subprocess.run(cmd, check=True, **kwargs)
    return result


# ---------------------------------------------------------------------------
# Step 1: AI decides topic + writes script
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = textwrap.dedent("""
    You are a cheerful VTuber named Miku. You create short, engaging YouTube Shorts
    (under 60 seconds when spoken at a natural pace — roughly 150 words or fewer).

    Your task: {topic_instruction}. Write the full spoken script.
    The script should be lively, positive, and end with a call-to-action asking viewers
    to like and subscribe.

    Respond ONLY with a JSON object in this exact format (no markdown fences):
    {{
      "title": "catchy video title (max 80 chars, include #Shorts)",
      "description": "YouTube description (2–3 sentences + #Shorts #VTuber)",
      "tags": ["tag1", "tag2", "tag3"],
      "script": "The full spoken script here."
    }}
""").strip()


def build_system_prompt() -> str:
    custom_topic = os.environ.get("CUSTOM_TOPIC", "").strip()
    if custom_topic:
        topic_instruction = f'create a short video about: "{custom_topic}"'
    else:
        topic_instruction = "autonomously decide a fun topic for today's short video"
    return SYSTEM_PROMPT_TEMPLATE.format(topic_instruction=topic_instruction)


def ai_generate_content(client: OpenAI) -> dict:
    print("[1/4] Asking AI to pick a topic and write a script …")
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": build_system_prompt()}],
        temperature=1.0,
        max_tokens=600,
    )
    raw = response.choices[0].message.content.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Attempt to extract JSON block if the model wrapped it anyway
        import re
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            print(f"[ERROR] Could not parse AI response:\n{raw}", file=sys.stderr)
            sys.exit(1)
        data = json.loads(match.group())

    for key in ("title", "description", "tags", "script"):
        if key not in data:
            print(f"[ERROR] AI response missing field: {key}", file=sys.stderr)
            sys.exit(1)

    # Guard against overly long scripts
    if len(data["script"]) > MAX_SCRIPT_CHARS:
        data["script"] = data["script"][:MAX_SCRIPT_CHARS]

    print(f"    Title  : {data['title']}")
    print(f"    Script : {data['script'][:80]} …")
    return data


# ---------------------------------------------------------------------------
# Step 2: Text-to-Speech
# ---------------------------------------------------------------------------

def generate_tts(client: OpenAI, script: str, audio_path: Path) -> None:
    print("[2/4] Generating TTS audio …")
    with client.audio.speech.with_streaming_response.create(
        model="tts-1",
        voice="nova",          # bright, energetic voice — swap to any OpenAI TTS voice
        input=script,
        response_format="mp3",
    ) as response:
        response.stream_to_file(str(audio_path))
    print(f"    Saved  : {audio_path}")


# ---------------------------------------------------------------------------
# Step 3: Compose video with FFmpeg
# ---------------------------------------------------------------------------

def get_audio_duration(audio_path: Path) -> float:
    """Return audio duration in seconds using ffprobe."""
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
    """Create a simple SRT file: split script into ~6-word chunks timed evenly."""
    words = script.split()
    chunk_size = 6
    chunks = [" ".join(words[i : i + chunk_size]) for i in range(0, len(words), chunk_size)]
    n = len(chunks)
    segment = duration / n if n else duration

    def srt_time(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, chunk in enumerate(chunks):
            start = i * segment
            end = (i + 1) * segment
            f.write(f"{i+1}\n")
            f.write(f"{srt_time(start)} --> {srt_time(end)}\n")
            f.write(f"{chunk}\n\n")


def compose_video(
    audio_path: Path,
    srt_path: Path,
    output_path: Path,
) -> None:
    """
    FFmpeg pipeline:
      - Loop the avatar PNG for the audio duration (portrait 9:16, 1080×1920)
      - Add a semi-transparent dark gradient at the bottom for subtitle readability
      - Burn in subtitles
      - Mux with TTS audio
    """
    print("[3/4] Composing video with FFmpeg …")

    duration = get_audio_duration(audio_path)

    # Escape srt path for FFmpeg filtergraph
    srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")

    # Build complex filter:
    #   [0:v] scale+pad avatar to 1080×1920 → [bg]
    #   drawbox for subtitle background gradient
    #   subtitles filter for burned-in captions
    vf_filter = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"drawbox=y=ih-320:color=black@0.45:width=iw:height=320:t=fill,"
        f"subtitles={srt_escaped}:force_style='"
        f"FontName=Arial,FontSize=28,PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00000000,Outline=2,Alignment=2,"
        f"MarginV=60'"
    )

    run([
        "ffmpeg", "-y",
        "-loop", "1",
        "-framerate", str(VIDEO_FPS),
        "-i", str(AVATAR_IMAGE),
        "-i", str(audio_path),
        "-vf", vf_filter,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-t", str(duration + AUDIO_BUFFER_SECONDS),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ])
    print(f"    Saved  : {output_path}")


# ---------------------------------------------------------------------------
# Step 4: Upload to YouTube
# ---------------------------------------------------------------------------

def get_youtube_service():
    """Build an authenticated YouTube service using OAuth2 refresh token."""
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
    print("[4/4] Uploading to YouTube …")
    youtube = get_youtube_service()

    title = content["title"]
    if SHORT_HASHTAG not in title:
        title = f"{title} {SHORT_HASHTAG}"

    body = {
        "snippet": {
            "title": title[:100],
            "description": content["description"],
            "tags": content.get("tags", []) + ["VTuber", "Shorts", "Miku"],
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
        chunksize=10 * 1024 * 1024,  # 10 MB chunks
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
            pct = int(status.progress() * 100)
            print(f"    Upload progress: {pct}%")

    video_id = response.get("id", "unknown")
    print(f"    Uploaded! https://www.youtube.com/shorts/{video_id}")
    return video_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    openai_client = OpenAI(api_key=env("OPENAI_API_KEY"))

    with tempfile.TemporaryDirectory(prefix="vtuber_short_") as tmpdir:
        tmp = Path(tmpdir)
        audio_path = tmp / "speech.mp3"
        srt_path = tmp / "subtitles.srt"
        video_path = tmp / "short.mp4"

        # 1. AI decides content
        content = ai_generate_content(openai_client)

        # 2. TTS
        generate_tts(openai_client, content["script"], audio_path)

        # 3. Compose video
        audio_duration = get_audio_duration(audio_path)
        build_subtitle_file(content["script"], audio_duration, srt_path)
        compose_video(audio_path, srt_path, video_path)

        # 4. Upload
        upload_to_youtube(video_path, content)

    print("[✓] Done!")


if __name__ == "__main__":
    main()
