#!/usr/bin/env python3
"""
YouTube Upload Helper
=====================
Provides OAuth 2.0 authentication and video upload for the YouTube Data API v3.

Authentication modes (tried in order):
  1. **Service-account style / token refresh** – If the environment variables
     ``YOUTUBE_CLIENT_ID``, ``YOUTUBE_CLIENT_SECRET``, and
     ``YOUTUBE_REFRESH_TOKEN`` are set, a credential is built directly from
     the refresh token (no browser required — ideal for CI).
  2. **token.pickle** – If a ``token.pickle`` file exists in the working
     directory it is loaded and refreshed automatically.
  3. **Interactive OAuth** – Falls back to ``InstalledAppFlow`` with
     ``client_secrets.json`` (requires a browser; not suitable for CI).
"""

import logging
import os
import pickle
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

# Scopes required for uploading videos
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

_TOKEN_URI = "https://oauth2.googleapis.com/token"

# YouTube metadata limits
_YT_TITLE_MAX = 100
_YT_DESCRIPTION_MAX = 5000


# ---------------------------------------------------------------------------
# Text sanitisation helpers
# ---------------------------------------------------------------------------

def _sanitize_text(text: str) -> str:
    """Strip characters that the YouTube Data API rejects in metadata fields."""
    # Remove ASCII control characters (below 0x20), keeping newlines and tabs.
    text = "".join(ch for ch in text if ord(ch) >= 32 or ch in "\n\t")
    # YouTube rejects '<' and '>' anywhere in snippet metadata.
    text = text.replace("<", "").replace(">", "")
    return text


def _sanitize_title(text: str) -> str:
    """Truncate and strip characters disallowed in YouTube video titles."""
    text = _sanitize_text(text)
    if len(text) > _YT_TITLE_MAX:
        text = text[: _YT_TITLE_MAX - 3] + "..."
    return text


def _sanitize_description(text: str) -> str:
    """Truncate and strip characters disallowed in YouTube video descriptions."""
    text = _sanitize_text(text)
    if len(text) > _YT_DESCRIPTION_MAX:
        text = text[: _YT_DESCRIPTION_MAX - 3] + "..."
    return text


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _credentials_from_env() -> Credentials | None:
    """Build credentials from environment variables (CI-friendly)."""
    client_id = os.environ.get("YOUTUBE_CLIENT_ID", "")
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
    refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")
    if not (client_id and client_secret and refresh_token):
        return None
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def get_authenticated_service():
    """Return an authorised YouTube Data API v3 service object.

    Tries environment-variable credentials first (for CI), then
    ``token.pickle``, then interactive OAuth.
    """
    credentials = _credentials_from_env()

    if credentials is None:
        token_path = Path("token.pickle")
        if token_path.exists():
            with token_path.open("rb") as fh:
                credentials = pickle.load(fh)  # noqa: S301

        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    "client_secrets.json", SCOPES
                )
                credentials = flow.run_local_server(port=0)
            with token_path.open("wb") as fh:
                pickle.dump(credentials, fh)

    return build("youtube", "v3", credentials=credentials)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def upload_to_youtube(
    file_path: str | Path,
    title: str,
    description: str,
    tags: list[str] | None = None,
    category_id: str = "27",
) -> str:
    """Upload a video to YouTube and return the video ID.

    Parameters
    ----------
    file_path:
        Path to the video file.
    title:
        Video title (will be sanitised and truncated).
    description:
        Video description (will be sanitised and truncated).
    tags:
        Optional list of tags. Defaults to ``["AI", "VTuber", "Automation"]``.
    category_id:
        YouTube category ID.  ``"27"`` = Education.

    Returns
    -------
    str
        The YouTube video ID.
    """
    youtube = get_authenticated_service()

    body = {
        "snippet": {
            "title": _sanitize_title(title),
            "description": _sanitize_description(description),
            "tags": tags if tags else ["AI", "VTuber", "Automation"],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(str(file_path), chunksize=-1, resumable=True)

    logger.info("Uploading file: %s", file_path)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = request.execute()
    video_id = response.get("id")
    logger.info("Upload successful. Video ID: %s", video_id)
    logger.info("Video URL: https://www.youtube.com/watch?v=%s", video_id)
    logger.info("Privacy status: public — video is live immediately.")
    return video_id
