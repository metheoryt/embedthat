# Embed That! Bot

A Telegram bot that converts social media links into native Telegram videos or audio, delivered straight into the chat.

Live at: https://t.me/embedthat_bot

## Supported Platforms

| Platform                                                | Behavior                                                                                              |
|---------------------------------------------------------|-------------------------------------------------------------------------------------------------------|
| YouTube                                                 | Downloads and uploads video natively (up to 50 MB, split into parts if needed); a button extracts audio |
| Instagram, TikTok, Twitter / X, Facebook, Reddit, …     | Downloaded with yt-dlp, re-encoded to iOS-compatible H.264/AAC and re-uploaded as a native video        |
| SoundCloud, Bandcamp, Mixcloud, Audiomack, Yandex Music | Audio-only: a single track, or a playlist paginated 10 tracks per page                                  |

Any `https://` link that is not YouTube falls into the same catch-all download path, so the
real list is "whatever yt-dlp supports". Spotify / Apple Music / Deezer are DRM-protected
and out of scope.

## Requirements

- Python 3.12
- [uv](https://github.com/astral-sh/uv)
- FFmpeg
- Node.js + `vot-cli` (`npm install -g vot-cli`) — for YouTube audio translation
- Redis

## Setup

1. Clone the repo and install dependencies:

   ```bash
   uv sync
   ```

2. Copy `.env.dist` to `.env` and fill in the required values:

   ```bash
   cp .env.dist .env
   ```

| Variable          | Required | Description                                                                         |
|-------------------|----------|-------------------------------------------------------------------------------------|
| `BOT_TOKEN`       | Yes      | Telegram bot token from @BotFather                                                  |
| `DUMP_CHAT_ID`    | Yes      | Chat ID where videos are temporarily sent to obtain Telegram `file_id`s for caching |
| `REDIS_URL`       | No       | Redis connection string (default: `redis://redis`)                                  |
| `LOGLEVEL`        | No       | Log level (default: `INFO`)                                                         |
| `TZ`              | No       | Timezone for log timestamps (default: `Asia/Almaty`)                                |
| `ADMIN_CHAT_ID`   | No       | Admin chat for error notifications                                                  |
| `COOKIES_FILE`    | No       | Path to a writable Netscape `cookies.txt` for yt-dlp; unlocks login-walled posts    |

## Running

**Locally** (requires Redis running separately):

```bash
uv run main.py
```

**With Docker Compose** (includes Redis):

```bash
docker compose up -d
```

**Build Docker image** (dev only — prod images are built by CI, see Deployment):

```bash
docker build -t embedthat:dev .
```

## Deployment

Production runs on `latitude`. **Pushing to `main` is the deploy**:
`.github/workflows/docker-publish.yml` builds and pushes `metheoryt/embedthat:latest` (plus
the `pyproject.toml` version), and Tugtainer on the host pulls the new digest within 15
minutes and recreates the containers. Nothing builds on the host; its compose is
`vps/homeserver/embedthat/compose.prod.yml`, tracked in the `vps` repo.

See CLAUDE.md's Deployment section for how to force a deploy instead of waiting for the
poll, and for the one failure mode that is silent — a container Tugtainer has disabled in
its own database never updates, whatever compose says.

## How It Works

1. User sends a link.
2. `bot/handlers.py` serves Redis cache hits inline and never downloads anything. On a miss it
   registers a waiter for the cache key and, only if it is the first waiter, enqueues a
   Dramatiq job — the separate `worker` process does all the work.
3. YouTube links go through their own pipeline:
    - Best quality stream within Telegram's 50 MB limit is selected
    - Video and audio are downloaded separately and merged with FFmpeg
    - If the result exceeds 50 MB, it is split into up to 10 parts
    - Optionally, audio is translated: language is detected via Whisper, translated via `vot-cli`, and mixed with the
      original (quieted) — off unless `ENABLE_AUDIO_TRANSLATION` is set
4. Every other link is downloaded with yt-dlp and re-encoded to iOS-compatible H.264/AAC. There
   is no domain rewriting or proxy embedding anywhere in the codebase.
5. A link whose formats carry no video track is classified as audio and routed to the audio
   pipeline instead. Classification is generic, never a domain allowlist.
6. Finished files are sent to `DUMP_CHAT_ID` to mint stable Telegram `file_id`s, which are cached
   in Redis and fanned out to every chat waiting on that key — repeat requests are served
   instantly without re-downloading.
