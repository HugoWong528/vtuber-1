# VTuber Original Short (Repo Save) v4

## What's new in v4

### 1. YouTube upload
- After saving the video to the repository, the script **uploads it to YouTube** via the YouTube Data API v3.
- Upload is **graceful**: if YouTube credentials are not configured the upload step is skipped and the video is still saved to the repository (identical to v3 behaviour).
- The upload log (`logs/upload_log.md`) and per-video metadata JSON now include the YouTube video URL.

### 2. Everything from v3
All v3 features are inherited:
- Unique topics every run (past-topic deduplication).
- Girl voice (`shimmer`).
- Single AI call for content + motion cues.
- Parallel TTS + Live2D capture.
- `ultrafast` / CRF 26 FFmpeg encoding.

## Files

| File | Description |
|---|---|
| `scripts/generate_original_short_repo_v4.py` | Main generation script (v4) |
| `scripts/youtube_upload.py` | YouTube Data API upload helper (OAuth 2.0) |
| `.github/workflows/vtuber-original-repo-v4.yml` | GitHub Actions workflow |
| `scripts/capture_live2d_v2.js` | Puppeteer capture script (reused from v2) |
| `scripts/live2d_capture_v2.html` | Live2D capture HTML (reused from v2) |
| `index.html` | Web controller (unchanged) |

## How to run

### Automatic (scheduled)
The workflow runs daily at **16:00 UTC**. It generates a unique video, commits it to `videos/`, and uploads it to YouTube.

### Manual
1. Go to **Actions** → **VTuber Original Short (Repo Save) v4**
2. Click **Run workflow**
3. Optionally provide a custom topic hint
4. The video and metadata will be committed to the repository and uploaded to YouTube

---

## Required secrets

| Secret | Required | Description |
|---|---|---|
| `POLLINATIONS_API_KEY` | ✅ Yes | API key from https://enter.pollinations.ai |
| `YOUTUBE_CLIENT_ID` | Optional* | Google OAuth 2.0 Client ID |
| `YOUTUBE_CLIENT_SECRET` | Optional* | Google OAuth 2.0 Client Secret |
| `YOUTUBE_REFRESH_TOKEN` | Optional* | OAuth 2.0 Refresh Token with `youtube.upload` scope |

> \* YouTube secrets are optional. If any are missing, the YouTube upload step is skipped and the video is saved to the repository only (v3 behaviour).

---

## YouTube API Setup Guide

Follow these steps to obtain the three YouTube secrets (`YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`).

### Step 1 — Create a Google Cloud project

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Click **Select a project** → **New Project**.
3. Give it a name (e.g. `vtuber-uploader`) and click **Create**.

### Step 2 — Enable the YouTube Data API v3

1. In the Cloud Console, go to **APIs & Services** → **Library**.
2. Search for **YouTube Data API v3**.
3. Click it, then click **Enable**.

### Step 3 — Configure the OAuth consent screen

1. Go to **APIs & Services** → **OAuth consent screen**.
2. Choose **External** (unless you have a Google Workspace org) and click **Create**.
3. Fill in:
   - **App name**: e.g. `VTuber Uploader`
   - **User support email**: your email
   - **Developer contact email**: your email
4. Click **Save and Continue**.
5. On the **Scopes** page, click **Add or Remove Scopes** and add:
   ```
   https://www.googleapis.com/auth/youtube.upload
   ```
6. Click **Save and Continue** through the remaining pages.
7. On the **Test users** page, add the Google account that owns the YouTube channel.

> **Note:** While the app is in "Testing" status, only test users can authorise. You do NOT need to publish/verify the app if you are the only user.

### Step 4 — Create OAuth 2.0 credentials

1. Go to **APIs & Services** → **Credentials**.
2. Click **+ Create Credentials** → **OAuth client ID**.
3. Application type: **Desktop app** (or **Web application**).
4. Name: e.g. `VTuber CLI`.
5. Click **Create**.
6. Copy the **Client ID** and **Client Secret** — you'll need them below.

### Step 5 — Obtain a refresh token

The easiest way is to run a one-time local script. On your machine (not in CI):

```bash
pip install google-auth-oauthlib
```

Create a temporary file `get_refresh_token.py`:

```python
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# Option A: if you downloaded client_secrets.json from the Cloud Console
flow = InstalledAppFlow.from_client_secrets_file("client_secrets.json", SCOPES)

# Option B: if you only have the client ID and secret
# from google_auth_oauthlib.flow import InstalledAppFlow
# flow = InstalledAppFlow.from_client_config(
#     {
#         "installed": {
#             "client_id": "YOUR_CLIENT_ID",
#             "client_secret": "YOUR_CLIENT_SECRET",
#             "auth_uri": "https://accounts.google.com/o/oauth2/auth",
#             "token_uri": "https://oauth2.googleapis.com/token",
#         }
#     },
#     SCOPES,
# )

credentials = flow.run_local_server(port=0)
print("Access token :", credentials.token)
print("Refresh token:", credentials.refresh_token)   # <— save this!
```

Run it:
```bash
python get_refresh_token.py
```

A browser window will open. Sign in with the Google account that owns your YouTube channel and grant permission. The script prints the **refresh token** — copy it.

### Step 6 — Add secrets to GitHub

1. Go to your repository → **Settings** → **Secrets and variables** → **Actions**.
2. Click **New repository secret** for each:

| Name | Value |
|---|---|
| `YOUTUBE_CLIENT_ID` | The Client ID from Step 4 |
| `YOUTUBE_CLIENT_SECRET` | The Client Secret from Step 4 |
| `YOUTUBE_REFRESH_TOKEN` | The refresh token from Step 5 |

### Step 7 — Test

1. Go to **Actions** → **VTuber Original Short (Repo Save) v4**.
2. Click **Run workflow**.
3. Check the logs — you should see:
   ```
   [YouTube] Uploading video to YouTube …
   [YouTube] ✓ Uploaded — https://youtu.be/<VIDEO_ID>
   ```

If credentials are missing you'll see:
```
[YouTube] Credentials not configured — skipping upload.
```
The video is still saved to the repository.

---

## Troubleshooting

| Issue | Solution |
|---|---|
| `[YouTube] Credentials not configured` | Make sure all three secrets (`YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`) are set in the repository. |
| `invalid_grant` error | The refresh token may have expired. Re-run `get_refresh_token.py` to obtain a new one. This can happen if: (a) you revoked access, (b) the token was unused for 6 months, or (c) the OAuth app is in "Testing" mode and 7 days passed. |
| `quotaExceeded` | YouTube Data API has a daily quota (default 10,000 units). A single `videos.insert` costs 1,600 units. If you run more than ~6 uploads/day you may hit the limit. Request a quota increase in the Cloud Console. |
| `Access Not Configured` | Make sure the **YouTube Data API v3** is enabled in the Cloud Console (Step 2). |
| Video uploads as "private" | Check the `status.privacyStatus` in `youtube_upload.py`. The default is `public`. If your OAuth app is unverified and the user hasn't granted broad access, YouTube may override the privacy. |
