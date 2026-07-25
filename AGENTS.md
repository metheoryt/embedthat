# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

**Embed That! Bot** is a Telegram bot that converts social media links (YouTube, Instagram, TikTok, Twitter/X) into playable embeds or native Telegram videos. Live at https://t.me/embedthat_bot.

## Commands

```bash
# Install dependencies
uv sync

# Run the bot locally (requires .env with BOT_TOKEN and DUMP_CHAT_ID)
uv run main.py

# Run with Docker Compose (includes Redis)
docker compose up -d

# Build the image locally (dev only — never tag it `metheoryt/embedthat:*`, see Deployment)
docker build -t embedthat:dev .
```

There is no test suite or linter configured.

## Deployment

Production runs on the homeserver (`g513ie`) through the **config-driven poll-and-build**
pipeline owned by the `vps` repo — no registry, no CI publish. **Pushing to `main` is the
deploy**; it lands within ~3 minutes plus build time.

- The `repos-deploy` scheduled task runs `vps/homeserver/deploy-repos.ps1` every 3 minutes
  over every entry in `vps/homeserver/repos.psd1`.
- Per repo it fast-forwards a gitignored clone at `vps/homeserver/embedthat/src/` and, only
  when the source SHA or the rendered compose config changed, archives the container logs,
  rebuilds, and recreates the stack.
- The prod compose lives in `vps` (`vps/homeserver/embedthat/compose.prod.yml`), not here —
  this repo carries only the dev `compose.yml`.

**Never tag an image `metheoryt/embedthat:*`.** The prod image tag is local-only
(`embedthat:local`) on purpose: a registry tag would let Tugtainer pull-update the container
out from under the local build and silently undo a deploy. For the same reason
`.github/workflows/docker-publish.yml` is retired — `workflow_dispatch` only.

Full runbook: `vps/homeserver/DEPLOYING-A-REPO.md`.

## Environment Setup

Copy `.env.dist` to `.env` and populate:
- `BOT_TOKEN` — Telegram bot token (required)
- `DUMP_CHAT_ID` — Telegram chat ID for temporary video storage (required); the bot sends videos here first to obtain Telegram `file_id`s for caching
- `REDIS_URL` — Redis connection string (default: `redis://redis`)
- `ADMIN_CHAT_ID` — optional

## Architecture

### Request Flow

1. User sends a social media link to the bot
2. `bot/handlers.py` routes the message by detected `LinkOrigin`
3. **Instagram/TikTok**: domain is rewritten to a proxy embedding service and sent back as a link
4. **Twitter/X**: domain is replaced with fxtwitter.com / fixupx.com
5. **YouTube**: full download-and-upload pipeline (see below)
6. Signals in `bot/events/signals.py` trigger cross-cutting handlers (logging in `log.py`, usage counters in `stats.py`)

### YouTube Pipeline (`bot/util/youtube/`)

- `video.py` — main orchestration: selects best adaptive stream within Telegram's 50 MB limit, downloads video and audio separately, merges with FFmpeg, splits into ≤50 MB parts if needed (up to 10 parts)
- `translate.py` — detects source language via Whisper (tiny model), translates audio using the `vot-cli` Node.js tool, mixes original (quieted) + translated audio with pydub
- `schema.py` — `YouTubeVideoData` Pydantic model for cached video metadata
- Redis caches processed `file_id`s to avoid re-downloading; Redis distributed locks prevent concurrent processing of the same video

### Key Patterns

- **Async throughout**: aiogram + asyncio; all I/O is non-blocking
- **Event signals** (`aiosignal`): `on_link_received`, `on_link_sent`, `on_yt_video_sent`, `on_yt_video_fail` — used for logging and stats without coupling handlers
- **Dump chat pattern**: videos are sent to `DUMP_CHAT_ID` to obtain a stable Telegram `file_id`, then forwarded to the user; cached `file_id`s allow instant resend on repeat requests
- **Config** via `pydantic-settings` in `bot/config.py`; `settings` singleton imported throughout

### System Dependencies (inside Docker)

- Python 3.12, FFmpeg, Node.js, `vot-cli` (global npm package for YouTube audio translation)
- Redis (separate container in `compose.yml`)

## Versioning

`version` in `pyproject.toml` follows semver (`major.minor.patch`), bumped by hand in the same commit/PR as the change it reflects:

- **major** — breaking changes (env var renames, incompatible cache/schema changes, dropped platform support)
- **minor** — new functionality (new link origin, new bot command/feature) that stays backward compatible
- **patch** — bug fixes, dependency bumps, refactors with no behavior change
