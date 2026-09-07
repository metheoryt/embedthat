# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

# Lint / type-check (no test suite exists)
uv run ruff check .
uv run pyright
```

There is no test suite. Ruff (`E,F,I,UP,B,ANN`) and pyright are configured in
`pyproject.toml` and installed in the dev group; both carry pre-existing debt, so
the working gate is "no *new* findings versus baseline", not zero.

## Deployment

Production runs on **`latitude`** (`latitude5520`, Linux). The stack lives at
`~/my/vps/homeserver/embedthat/` there, built locally from a gitignored clone at `src/` —
no registry, no CI publish.

**Reach it as `latitude.gg.ez`, not bare `latitude`.** `/etc/resolv.conf` on the WSL boxes
carries `search lan gg.ez` in that order, so the bare name resolves through the router's
`.lan` zone (`latitude.lan` = `192.168.8.154`) and dies with *no route to host* from anywhere
off that LAN — the tailnet name is never tried. The MagicDNS FQDN skips the search list.
Same trap for `air`; `g15`/`hub`/`desktop-wsl` are unaffected only because the router has no
`.lan` record for them.

**Pushing to `main` is NOT the deploy on this host.** The config-driven poll-and-build
pipeline in the `vps` repo (`deploy-repos.ps1` + `repos.psd1`, driven by the `repos-deploy`
scheduled task) is **PowerShell, written for the Windows homeserver `g513ie`** — and it does
not run on `latitude`: no `pwsh`, no timer, no cron. Verified 2026-09-07, when the
`.embedthat-last-deployed` marker still read Jul 26 against an image built Aug 16. Deploying
is a manual errand:

```console
ssh latitude.gg.ez
git -C ~/my/vps/homeserver/embedthat/src pull --ff-only
cd ~/my/vps/homeserver/embedthat && docker compose -f compose.prod.yml up -d --build
```

Nothing archives the container logs first, so `docker compose logs` history is discarded by
that recreate — pull anything you still need out of it beforehand.

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
- `REDIS_URL` — Redis connection string (default: `redis://redis`); also the dramatiq broker URL
- `ADMIN_CHAT_ID` — optional; where CRITICAL log records are forwarded
- `LOGLEVEL` — default `INFO`
- `TZ` — timezone used for log timestamps and stats day boundaries
- `ENABLE_AUDIO_TRANSLATION` — default **false**; YouTube audio translation is off unless set
- `MAX_VIDEO_RESOLUTION` — default `480`
- `MAX_PLAYLIST_TRACKS` — default `200`; an abuse guard on playlist *listing*, not on consumption (page downloads are lazy)
- `COOKIES_FILE` — optional; path to a Netscape-format `cookies.txt` passed to every yt-dlp call (see `bot/util/ytdlp.py`), unlocking posts that demand a logged-in session. Unset, or pointing at a missing file, means no cookies and the pre-cookie behaviour. **Mount it read-write** — yt-dlp writes the refreshed jar back on close, which is what keeps the session from expiring on the exporter's schedule. When a login wall is hit *while* this is set, the worker raises one CRITICAL per 24h to `ADMIN_CHAT_ID` on the assumption the jar went stale.

## Architecture

### Two processes

The bot is **not** a single process. `main.py` runs aiogram polling; a separate
`worker` service runs `dramatiq bot.worker.actors --processes 1 --threads 4`.
Redis is simultaneously the cache, the dramatiq broker, and the lock store.

- `bot/handlers.py` serves cache hits inline and never downloads anything.
- On a cache miss it registers a **waiter** (`bot/worker/waiters.py`) and, only if
  it is the first waiter for that key, enqueues a dramatiq actor
  (`bot/worker/actors.py`).
- The worker downloads/merges/uploads (`bot/worker/pipeline.py`), then pops the
  whole waiter list and fans the result out to every waiting chat.

### Request Flow

1. User sends a link.
2. **YouTube** (`youtube.com/watch|shorts`, `youtu.be`) matches its own handler and
   goes through the YouTube pipeline below.
3. **Everything else** — Instagram, TikTok, Twitter/X, Facebook, Reddit, … — falls
   into the `embed_social` catch-all, which matches any `https?://` URL that is not
   YouTube and downloads it with yt-dlp, re-encoding to iOS-compatible H.264/AAC and
   re-uploading as a native Telegram video. There is no domain rewriting or proxy
   embedding anywhere in the codebase.
4. A link whose formats carry no video track (`vcodec == 'none'`) is classified as
   **audio** and routed to the audio pipeline instead. Classification is generic —
   never a domain allowlist — and costs one extra yt-dlp probe per uncached link.
5. Signals in `bot/events/signals.py` trigger cross-cutting handlers (logging in
   `log.py`, usage counters in `stats.py`).

### YouTube Pipeline (`bot/util/youtube/`)

- `video.py` — main orchestration: selects best adaptive stream within Telegram's 50 MB limit, downloads video and audio separately, merges with FFmpeg, splits into ≤50 MB parts if needed (up to 10 parts)
- `translate.py` — detects source language via Whisper (tiny model), translates audio using the `vot-cli` Node.js tool, mixes original (quieted) + translated audio with pydub
- `schema.py` — `YouTubeVideoData` Pydantic model for cached video metadata
- Redis caches processed `file_id`s to avoid re-downloading; `HeartbeatLock`
  (`bot/util/redis_lock.py`) prevents concurrent processing of the same video
- An `aud:` inline button on the delivered video triggers audio-only extraction
  (`process_youtube_audio`), cached on the same `yt:<id>` entry

### Audio Pipeline (`bot/util/audio/`)

- Handles audio-only links — SoundCloud, Bandcamp, Mixcloud, Audiomack, Yandex
  Music. Spotify / Apple Music / Deezer are DRM-protected and out of scope.
- Playlists are paginated at 10 tracks per page and delivered with an `apg:`
  inline pager; a page turn deletes the old page and sends a new one, because
  Telegram cannot edit a media group in place.
- Two-tier cache: `da:<hash16>` holds the ordered track index (non-authoritative
  for `file_id`s), `au:<extractor>:<id>` is the durable per-track dedup entry that
  is never clobbered — the split self-heals last-write-wins races between pages.
- A track over the 50 MB cap is **rejected**, not ffmpeg-split like video.

### Key Patterns

- **Async throughout**: aiogram + asyncio; all I/O is non-blocking
- **Event signals** (`aiosignal`): `on_link_received`, `on_link_sent`,
  `on_yt_video_sent`, `on_yt_video_fail`, `on_social_video_sent`,
  `on_social_video_fail` — used for logging and stats without coupling handlers
- **Per-process startup**: `freeze_signals()` and `install_admin_alert_handler()`
  must run in *both* entrypoints (`main.py` and `bot/worker/__init__.py`) — the two
  processes share no startup path. Each actor invocation runs its own
  `asyncio.run()`, so actor-side code must open a short-lived Redis client instead
  of reusing the module-level `redis_client` singleton (which binds to the first
  event loop)
- **Waiter fan-out**: cache keys are global rather than per-chat, so requesters
  `RPUSH` a `Waiter` onto the key's list and a returned length of `1` is the
  race-free "am I first" test — no lock needed to decide who enqueues the job
- **Error classification**: every routine, user-facing exception an actor can raise
  is listed in that actor's `throws=` tuple, which skips both the retries and
  `on_retry_exhausted` — so a bad link reaches the user as a reason instead of
  paging the admin. Telegram admin alerts fire on `log.critical` only, deliberately:
  WARNING/ERROR mark routine conditions (ack races, ffprobe fallbacks, per-attempt
  retries)
- **Multi-part delivery**: `sendMediaGroup` accepts no `reply_markup`, so split
  videos and multi-track audio pages must carry their buttons/footer in a separate
  follow-up message; only single-item sends can collapse to one message
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
