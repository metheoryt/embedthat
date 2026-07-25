<!-- KB refreshed against b5bbefb on 2026-07-25 -->

# Project memory — embedthat

<!--
Repo-local durable memory, auto-loaded at session start by the
project-memory-check.sh hook (merged with global + per-host memory). One bullet
per fact under a topical heading. Stable architecture/vision belongs in
CLAUDE.md (mirrored into AGENTS.md) instead. Git-tracked — no secrets here.
-->

## Verification (there is no test suite)

- The gate is **"no *new* findings versus a recorded baseline"**, never "zero" —
  the repo carries substantial pre-existing pyright/ruff debt. Capture counts on
  the working copy, `git stash` (or check `main` out in a throwaway worktree),
  re-run for the baseline, and diff the error *sets*, not just the totals.
- Two large pyright error classes are known false positives and must not be
  "fixed": `reportCallIssue` "Expected 0 positional arguments" on every
  `@dramatiq.actor` call site, and the `reportPossiblyUnbound` block in
  `bot/worker/pipeline.py` where pyright can't see that the post-retry-loop
  `if exc: raise` guard always fires first.
- `faster_whisper`, `ffmpeg`, `pydub` and `pytubefix` publish no stub package on
  PyPI, so their `reportMissingTypeStubs` / `reportUnknown*` noise can't be fixed
  by `uv add` — only boundary annotations at the call sites help.
- Never put `reportAny` / `reportExplicitAny` in `[tool.pyright]` — they are
  basedpyright-only keys and standard pyright prints "Config contains
  unrecognized setting" on every run.
- **Dramatiq actor behavior can't be tested with `StubBroker`**: actors bind to a
  broker at decoration time, so importing `bot.worker.actors` routes everything to
  the real `RedisBroker`. Exercise retry / `on_retry_exhausted` behavior with
  `docker compose exec -T worker python -c ...` inside the running worker.
- Probe pattern against the real image:
  `docker run --rm --env-file <prod .env> -e PYTHONPATH=/app -v <clone>/bot:/app/bot:ro embedthat:local /app/.venv/bin/python /tmp/probe.py`.
  Mount only `bot/` — mounting the repo root over `/app` hides the image's own
  `/app/.venv` — and set `PYTHONPATH=/app`, since a script under `/tmp` doesn't
  get `/app` on `sys.path`.
- An import smoke test must cover all four entry doors — `main.py`,
  `bot.worker.actors`, `bot.events`, `bot.events.handlers.stats` — because
  `stats.py` reaches both schema modules and the worker enters the package
  through a different door than `main.py`.
- The imports in `bot/events/__init__.py`, `bot/events/handlers/__init__.py` and
  `bot/util/social/__init__.py` are **deliberate side effects / re-exports**
  (`bot/events/__init__.py` pulls `.handlers` purely to register signal
  handlers). Silence F401 by declaring `__all__`, never by deleting them.

## Dependencies & versioning

- Re-run `uv lock` and commit `uv.lock` in the same change as any hand-edited
  `version` bump in `pyproject.toml` — the lockfile carries the project's own
  version entry and has silently drifted at least once. It stays harmless only
  because the Dockerfile uses `uv sync --frozen`, not `--locked`.
- Instagram failing with "Instagram sent an empty media response … use
  `--cookies-from-browser`" means a **stale pinned yt-dlp**, not an auth problem:
  the extractor's JSON-metadata path stops returning media for anonymous requests
  and upstream rewrites it. The fix is bumping yt-dlp; because the floor is a
  `>=` plus a committed lock, every recurrence needs another manual bump.
- Instagram no longer reports width/height/duration in yt-dlp metadata, so
  `bot/util/social/download.py` falls back to ffprobe — every Instagram download
  pays two extra ffprobe calls.
- `dramatiq-dashboard` is unusable here and was deliberately dropped: its latest
  release hard-pins `dramatiq[redis]<2.0` and `redis<5.0`, conflicting with both
  dramatiq 2.x and this repo's `redis[hiredis]>=5.2.1`. Queue depth / failure
  counts surface through the `/stats` admin command instead.
- Passing any `middleware=[...]` list to `RedisBroker()` **replaces** dramatiq's
  entire default middleware stack (AgeLimit, TimeLimit, ShutdownNotifications,
  Callbacks, Pipelines, Retries) rather than merging — so `bot/worker/broker.py`
  passes no `middleware=` at all and customization goes through per-actor
  `@dramatiq.actor(...)` options, which the default middleware already reads.
- The worker must stay `--processes 1 --threads 4`, not dramatiq's default of 20
  processes: `bot/util/youtube/translate.py`'s `_whisper_model` is a per-process
  global, so every extra process loads a duplicate Whisper model.
- Size every timeout and retry default against worst-case job duration —
  multi-stream size probing + ffmpeg merge/split up to 10 parts + per-part upload
  retries routinely exceeds 10 minutes, which is exactly dramatiq's `TimeLimit`
  default (it force-kills the actor thread). Its `Retries` default of 20 is
  equally dangerous, since a retry re-runs the whole download/upload pipeline.

## Cache & Redis

- **The video cache has no TTL** — entries never expire, so a cache-schema change
  can never be "waited out"; production still holds orphaned keys from the
  pre-`yt:<id>` cache-key scheme that current code never reads. New fields must
  default to `None`, parse old payloads, and backfill lazily on touch.
- Host-side `uv run` scripts need a one-off inline `REDIS_URL=redis://localhost:6379`
  (compose publishes 6379). Never persist it in `.env` — `redis://redis` is the
  compose service name and the containers break without it.
- `AudioRequestData.page()` returns **aliased slices** of `self.tracks`, not
  copies: the download flow mutates `AudioTrackData` in place and relies on that
  aliasing for `file_id`s to survive `model_dump_json()`. Making `page()` copy
  would silently break caching.
- Waiters popped from Redis must be deduped by `chat_id` before delivery in every
  actor, and the inline button is stripped on tap — added after a user received
  two audio files for one request.

## Behavior & UX decisions

- No "⏳ Processing…" ack message: results simply arrive when ready. The
  "🎵 Get audio" button rides on the video message itself via `reply_markup`,
  never as a second message.
- The audio pager sends the new page **before** deleting the old messages, never
  the reverse (a failed delete only leaves a stale message; a failed send after
  a delete leaves the user with nothing). Pages anchor to the user's original
  link message, not the tapped one, so two people pasting the same link in one
  group chat can't delete each other's pages. The
  `edit_reply_markup(reply_markup=None)` double-tap guard at the top of
  `get_audio_page` must stay unconditional.
- The 480p cap picks the **smallest** stream at or above 480p and downscales it
  (`-vf scale=-2:480 -c:v libx264`), not the largest stream under it — otherwise
  a 360p/720p-only video is needlessly delivered at 360p.
- The translation kill switch must gate two points: `target_lang` also feeds the
  Redis cache key, so `message.from_user.language_code` resolution has to be
  skipped in the handler too, or EN and RU users populate two cache entries with
  byte-identical output.
- Audio-vs-video is classified generically in the worker (no format with
  `vcodec != 'none'`), never by a domain allowlist — nothing can know before
  yt-dlp probes. Accepted cost: a second yt-dlp round-trip on every uncached
  non-YouTube link, plus a new pre-download failure surface.
- The audio delivery branch returns before emitting `on_social_video_sent`, so
  the signal-driven logging/stats consumers undercount audio traffic — a known
  consequence of the design, not an oversight.
- Spotify / Apple Music / Deezer are DRM-protected and yield metadata only;
  SoundCloud, Bandcamp, Mixcloud, Audiomack and Yandex Music are really
  downloadable. Sourcing audio from rippers or bulk catalogs was raised and ruled
  out as DRM circumvention — settled, not an open question.
- Repointing `DUMP_CHAT_ID` has three traps: group/channel ids are negative
  (`-100…`) while private-chat ids are positive; channel posts arrive as
  `channel_post` updates and never match a `router.message` handler (hence the
  `channel_post` `/start` variant, which exists purely to reveal a chat id); and
  a bot may only message a user who started the conversation. Verify with a real
  `sendMessage`, because setting the env var only proves configuration.
- aiogram evaluates `callback_query` filters in **registration order**, so the
  exact-match `F.data == "apg:noop"` handler must stay registered before the
  broad `F.data.startswith("apg:")` one — otherwise a page-indicator tap reaches
  `get_audio_page`, whose `split(":")` expects 3 parts and raises `ValueError`.
- `ydl.extract_info()` returns `None` (rather than raising `DownloadError`) for
  URLs it can't resolve — e.g. private `https://t.me/c/...` links — so every call
  site must None-guard and raise a domain error before `cast(dict, ...)`.
- Per-page track downloads run under `asyncio.Semaphore(3)` inside an
  `asyncio.TaskGroup`, never plain `asyncio.gather` (which doesn't cancel
  siblings on failure, leaving orphaned tasks writing into a deleted
  `TemporaryDirectory`). The `except* Exception as eg: … raise eg.exceptions[0]`
  unwrap is load-bearing: dramatiq classifies expected errors from `throws=` by
  `isinstance`, so a bare `ExceptionGroup` would reclassify them as crashes.
- `bot/util/aiohttp.py` is dead — zero importers, and its module-level
  `ClientSession()` would raise on import outside a running loop. Cleanup
  candidate.
- `bot/util/stats.py` formats dates with the glibc `%-d` extension, so
  `build_stats_report()` raises on a Windows-hosted Python run even though it
  works in the Linux container — verify helpers like `_queue_stats()` directly
  there.
- The dev `compose.yml`'s named `bot_venv` volume is shared by `bot` and `worker`
  and caches the venv, so after adding a dependency a freshly built image is
  masked by the stale volume: `docker compose rm -f` both services and drop the
  volume before the new deps appear.

## Repo & deploy conventions

- **`AGENTS.md` is a hand-synced twin of `CLAUDE.md`** — a real file, not a
  symlink, byte-identical except the header line (it is the Codex-facing
  counterpart). Mirror every `CLAUDE.md` edit into it in the same commit.
- Feature work commits both artifacts to git before any code: a design spec in
  `docs/superpowers/specs/YYYY-MM-DD-*.md`, then a task-by-task implementation
  plan in `docs/superpowers/plans/YYYY-MM-DD-*.md`.
- Work lands on `main` by **fast-forward only**, through the base clone
  (`git -C /home/me/my/embedthat merge --ff-only <branch>`); when `main` is
  checked out elsewhere, push the branch straight at the remote after verifying
  `main..<branch>` is empty, then `git pull --ff-only` the other checkout.
- Gortex indexes the **base checkout**, not Orca worktrees, so graph queries
  answer `main` and a worktree branch's changes are invisible to the graph until
  merge-back — a "resolution improved" claim made from a worktree is a pyright
  claim, not a graph claim.
- Paths like `C:\Users\methe\GitHub\embedthat-bot` in older plans/docs are the
  historical Windows checkout; the repo was renamed `embedthat-bot` → `embedthat`.
- The authoritative "deploy succeeded" signal is the marker file
  `homeserver/.<name>-last-deployed` (`sourceSHA:configHash`), written only after
  a successful build+recreate. Polling the deploy log for the new SHA is a false
  positive — the `deploying <sha>` line is written *before* the build runs.
- The deploy engine archives `docker compose logs` to
  `homeserver/logs/archive/<name>-<timestamp>.log` (7-day retention) immediately
  before each recreate, so pre-deploy history is recoverable there — but only for
  the engine's own recreate: a hand-run `docker compose up -d --build` or a
  `down` still discards logs (reboots and crash-restarts lose nothing).
