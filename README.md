# Embed That! Bot

A Telegram bot that converts social media links into playable embeds or native videos directly in chat.

Live at: https://t.me/embedthat_bot

## Supported Platforms

| Platform    | Behavior                                                                       |
|-------------|--------------------------------------------------------------------------------|
| YouTube     | Downloads and uploads video natively (up to 50 MB, split into parts if needed) |
| Instagram   | Rewrites link to ddinstagram.com proxy                                         |
| TikTok      | Rewrites link to vxtiktok.com proxy                                            |
| Twitter / X | Rewrites link to fxtwitter.com / fixupx.com                                    |

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

1. User sends a social media link.
2. `bot/handlers.py` detects the `LinkOrigin` and routes accordingly.
3. Instagram/TikTok/Twitter links are rewritten to embed-friendly proxy domains and sent back.
4. YouTube links go through a full pipeline:
    - Best quality stream within Telegram's 50 MB limit is selected
    - Video and audio are downloaded separately and merged with FFmpeg
    - If the result exceeds 50 MB, it is split into up to 10 parts
    - Optionally, audio is translated: language is detected via Whisper, translated via `vot-cli`, and mixed with the
      original (quieted)
5. Processed YouTube `file_id`s are cached in Redis — repeat requests are served instantly without re-downloading.
