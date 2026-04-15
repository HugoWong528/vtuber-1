#!/usr/bin/env python3
"""
YouTube Upload Helper
=====================
Provides OAuth 2.0 authentication and video upload for the YouTube Data API v3.

Authentication modes (tried in order):
  1. **token.pickle** – If a ``token.pickle`` file exists (in the provided
     path, the working directory, or the repository root) it is loaded and
     refreshed automatically.  When refreshing an expired token also requires
     ``client_secrets.json``, the same search order is used.
  2. **Service-account style / token refresh** – If the environment variables
     ``YOUTUBE_CLIENT_ID``, ``YOUTUBE_CLIENT_SECRET``, and
     ``YOUTUBE_REFRESH_TOKEN`` are set, a credential is built directly from
     the refresh token (no browser required — ideal for CI).
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


def _resolve_file(filename: str, extra_dirs: list[Path] | None = None) -> Path | None:
    """Return the first existing path for *filename* in several locations."""
    candidates: list[Path] = []
    if extra_dirs:
        candidates.extend(d / filename for d in extra_dirs)
    candidates.append(Path(filename))  # CWD
    # Also try the repository root (parent of this script's directory).
    repo_root = Path(__file__).resolve().parent.parent
    candidates.append(repo_root / filename)
    for p in candidates:
        if p.exists():
            return p
    return None


def get_authenticated_service(
    token_pickle_path: str | Path | None = None,
    client_secrets_path: str | Path | None = None,
):
    """Return an authorised YouTube Data API v3 service object.

    Tries ``token.pickle`` first (file-based auth), then environment-variable
    credentials (for CI), then interactive OAuth with ``client_secrets.json``.

    Parameters
    ----------
    token_pickle_path:
        Explicit path to ``token.pickle``.  When *None* the file is searched
        in the current working directory and the repository root.
    client_secrets_path:
        Explicit path to ``client_secrets.json``.  Same search logic.
    """
    extra_dirs: list[Path] = []
    if token_pickle_path is not None:
        extra_dirs.append(Path(token_pickle_path).parent)
    if client_secrets_path is not None:
        extra_dirs.append(Path(client_secrets_path).parent)

    # 1. Try token.pickle (file-based auth — works with repo-committed files)
    credentials = None
    resolved_token = (
        Path(token_pickle_path) if token_pickle_path else _resolve_file("token.pickle", extra_dirs)
    )
    if resolved_token and resolved_token.exists():
        logger.info("Loading credentials from %s", resolved_token)
        with resolved_token.open("rb") as fh:
            credentials = pickle.load(fh)  # noqa: S301
    else:
        # Explicit path didn't exist — clear so we don't try to write back later
        resolved_token = None

    if credentials and credentials.valid:
        return build("youtube", "v3", credentials=credentials)

    if credentials and credentials.expired and credentials.refresh_token:
        logger.info("Refreshing expired token …")
        credentials.refresh(Request())
        # Persist the refreshed token back to the same file.
        if resolved_token:
            with resolved_token.open("wb") as fh:
                pickle.dump(credentials, fh)
        return build("youtube", "v3", credentials=credentials)

    # 2. Try environment-variable credentials (CI-friendly)
    credentials = _credentials_from_env()
    if credentials is not None:
        return build("youtube", "v3", credentials=credentials)

    # 3. Interactive OAuth with client_secrets.json (fallback)
    resolved_secrets = (
        Path(client_secrets_path) if client_secrets_path else None
    )
    if resolved_secrets and not resolved_secrets.exists():
        resolved_secrets = None
    if resolved_secrets is None:
        resolved_secrets = _resolve_file("client_secrets.json", extra_dirs)
    if resolved_secrets is None:
        raise FileNotFoundError(
            "No YouTube credentials found: token.pickle, env vars, or client_secrets.json"
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(resolved_secrets), SCOPES)
    credentials = flow.run_local_server(port=0)
    # Save for next run
    save_path = resolved_token or Path("token.pickle")
    with save_path.open("wb") as fh:
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
    token_pickle_path: str | Path | None = None,
    client_secrets_path: str | Path | None = None,
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
    token_pickle_path:
        Explicit path to ``token.pickle`` (optional).
    client_secrets_path:
        Explicit path to ``client_secrets.json`` (optional).

    Returns
    -------
    str
        The YouTube video ID.
    """
    youtube = get_authenticated_service(
        token_pickle_path=token_pickle_path,
        client_secrets_path=client_secrets_path,
    )

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
