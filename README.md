# vtuber-1

Live2D Miku VTuber project with an automated YouTube Shorts pipeline.

---

## Auto-Short Workflow

The workflow **VTuber Auto-Short** (`.github/workflows/vtuber-short.yml`) runs every day at 12:00 UTC and can also be triggered manually from the **Actions** tab.

### What it does

1. **AI picks a topic** — GPT-4o autonomously decides what today's short will be about and writes the full spoken script (≤ 60 s when read aloud).
2. **Text-to-Speech** — OpenAI TTS converts the script to an MP3 using the "nova" voice.
3. **Video composition** — FFmpeg composites a portrait (1080 × 1920) YouTube Short:
   - `texture_00.png` (Miku avatar) as the looping background
   - Burned-in subtitles synced to the speech
4. **YouTube upload** — The video is uploaded as a public YouTube Short via the YouTube Data API v3.

### Required GitHub Secrets

Add these in **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Description |
|---|---|
| `OPENAI_API_KEY` | Your OpenAI API key (used for GPT-4o + TTS) |
| `YOUTUBE_CLIENT_ID` | OAuth2 client ID from Google Cloud Console |
| `YOUTUBE_CLIENT_SECRET` | OAuth2 client secret |
| `YOUTUBE_REFRESH_TOKEN` | Long-lived refresh token with `youtube.upload` scope |

### How to get a YouTube refresh token

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a project.
2. Enable the **YouTube Data API v3**.
3. Create **OAuth 2.0 credentials** (Desktop app type).
4. Run the OAuth flow locally once to obtain a refresh token (use the `google-auth-oauthlib` flow or the [OAuth Playground](https://developers.google.com/oauthplayground/)).
5. Paste the refresh token, client ID, and client secret as GitHub secrets.

### Manual trigger with a topic hint

In the Actions tab, select **VTuber Auto-Short → Run workflow** and optionally fill in the **custom_topic** field. The AI will use it as a hint when writing the script.