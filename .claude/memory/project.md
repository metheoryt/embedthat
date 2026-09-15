<!-- KB refreshed against 710e8a5 on 2026-09-12 -->

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
- **Exercising an actor's retry path locally** (verified 2026-09-10): the dev
  stack is safe to run beside prod — the local `.env` holds the debug bot
  `@assinstantbot`, not the prod token — but its `REDIS_URL` points at
  `localhost` (it is written for a host-run `main.py`), so an in-container
  worker needs an override file: `docker compose -f compose.yml -f <tmp>.yml up
  -d worker` with `REDIS_URL: redis://redis`. Source is bind-mounted, so code
  changes need no rebuild.
- A deterministic yt-dlp failure is easier to *serve* than to find: run a
  `BaseHTTPRequestHandler` answering 403 on `127.0.0.1:8099` inside the worker
  (`docker compose exec -d worker python -c ...`) and feed the actor that URL —
  the generic extractor turns it into a real `HTTP Error 403`. Register a
  `Waiter` with `chat_id=settings.dump_chat_id` first, so the failure message
  lands in the dump chat instead of a user's.

- **`pydub-stubs` exists and is now a dev dependency** — the stub situation is
  asymmetric, not uniform: `pydub-stubs` is on PyPI and installed, while both
  `types-<name>` and `<name>-stubs` return 404 for `pytubefix`, `ffmpeg-python`
  and `faster-whisper` (checked against PyPI directly, 2026-09-10). So the
  remaining `reportUnknown*` noise in `bot/util/youtube/video.py` and
  `translate.py` is a **ceiling, not debt** — no `uv add` moves it, and the only
  lever left is boundary annotations at the call sites. The Dockerfile installs
  with `--no-dev`, so a stub package never reaches prod.
  <!-- conflicts-with: "`faster_whisper`, `ffmpeg`, `pydub` and `pytubefix` publish no stub package on PyPI" -->
  <!-- src: embedthat cec9e3a | 2026-09-12 -->
- **Gortex reports live code as dead here — do not act on it.** The graph cannot
  see aiogram `@router.message` / `@router.callback_query` handlers, the
  `@dramatiq.actor` entries in `bot/worker/actors.py`, the aiosignal handlers
  wired by `freeze_signals()`, or `emit` in `telegram_log_handler.py`. Every one
  of those is reached by a registry or a decorator at import time, so a
  dead-symbol or unused-import finding on them is an artifact of the resolver,
  not a cleanup candidate.
  <!-- src: embedthat 710e8a5 | 2026-09-12 -->
- **"Streams resolved" is not proof that bytes arrive.** pytubefix's `WEB` and
  `WEB_SAFARI` clients resolve stream metadata perfectly and the CDN then serves
  a **31-byte body** for every range request unless a PO token rides along. Any
  claim that a YouTube client works has to come from a real byte-range fetch
  (`urllib` with `Range: bytes=0-262143`, assert HTTP 206 and the length), never
  from `yt.streams` succeeding.
  <!-- src: embedthat 710e8a5 | 2026-09-12 -->

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

- **No InnerTube client is pinned in `bot/util/youtube/schema.py`, deliberately.**
  Which client YouTube still serves changes without notice and pytubefix's
  default is the one upstream keeps current: 10.10.1 defaulted to `ANDROID_VR`,
  which began answering every request with `BotDetection` on 2026-09-01 and took
  the whole YouTube path down until 11.1.0 (default `VISION_OS`) restored it. A
  pin would have frozen the repo on the broken default, and the obvious pin
  (`"WEB"`) is the worst of them — see the 31-byte-body note above. 10.11.0
  predates the breakage, so 11.x was the only forward version.
  <!-- src: embedthat 710e8a5 | 2026-09-12 -->
- **The image has Node but yt-dlp will not use it.** `/usr/bin/node` is present
  (it is there for `vot-cli`), yet yt-dlp enables only `deno` by default, so it
  warns "No supported JavaScript runtime could be found … some formats may be
  missing" on every run. Social downloads work regardless, so this is a known
  non-urgent gap rather than a fault to chase — closing it means adding deno to
  the Dockerfile or passing `js_runtimes`.
  <!-- src: embedthat 710e8a5 | 2026-09-12 -->

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
  non-YouTube link, plus a new pre-download failure surface. The probe facts this
  rests on — flat entries carry no `formats`/`vcodec`, `extract_info()` returns
  `None` instead of raising, per-extractor entry shapes — are in
  [`docs/yt-dlp-probe-behavior.md`](../../docs/yt-dlp-probe-behavior.md).
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

- **Attacker-controlled fields are logged with `%r`, never `%s`.** A Telegram
  display name, chat title, username or message body is whatever the sender
  typed — one real sample carried a `<prompt>…</prompt>` injection aimed at
  whoever reads the logs, and a newline in any of them forges whole log lines.
  `%r` quotes and escapes, so a hostile value can only ever be one line's
  argument. `bot/events/handlers/log.py` interpolates every field but `origin`
  that way; keep it that way when adding fields.
  <!-- src: embedthat 710e8a5 | 2026-09-12 -->
- `bot/util/aiohttp.py` was **deleted** — the cleanup happened; there is no such
  module to remove any more.
  <!-- conflicts-with: "`bot/util/aiohttp.py` is dead — zero importers, and its module-level `ClientSession()` would raise on import outside a running loop. Cleanup candidate." -->
  <!-- src: embedthat 1c8ce78 | 2026-09-12 -->

## Repo & deploy conventions

- **`AGENTS.md` is a hand-synced twin of `CLAUDE.md`** — a real file, not a
  symlink, byte-identical except the header line (it is the Codex-facing
  counterpart). Mirror every `CLAUDE.md` edit into it in the same commit — resync
  it mechanically rather than by hand-editing twice:
  `{ head -3 AGENTS.md; tail -n +4 CLAUDE.md; } > .agents.new && mv .agents.new AGENTS.md`,
  then verify with `diff <(tail -n +4 AGENTS.md) <(tail -n +4 CLAUDE.md)`.
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
- **The poll-and-build engine no longer deploys this repo (2026-09-08).** Its
  marker file `homeserver/.<name>-last-deployed` and its pre-recreate log archive
  under `homeserver/logs/archive/` are written only for repos still in
  `vps/homeserver/repos.psd1` — embedthat was removed from it. Prod is now
  registry + Tugtainer (see CLAUDE.md → Deployment), and nothing archives logs
  before a Tugtainer recreate: pull what you need out of `docker compose logs`
  *before* triggering a deploy, or it is gone.

- **`AGENTS.md` is now the ONLY guidance file, and `CLAUDE.md` is a 135-byte
  pointer at it.** The twin arrangement was collapsed because the two files were
  12.8 KB of byte-identical text and every session paid for both. **Do not run
  the old resync recipe** — `tail -n +4` of a one-line pointer is empty, so it
  would truncate `AGENTS.md` to three lines and delete the repo's entire
  guidance. Edit `AGENTS.md` directly and leave `CLAUDE.md` alone; nothing needs
  mirroring in either direction. Any bullet or doc here that says "see
  `CLAUDE.md` → <section>" means `AGENTS.md` → that section.
  <!-- conflicts-with: "`{ head -3 AGENTS.md; tail -n +4 CLAUDE.md; } > .agents.new && mv .agents.new AGENTS.md`, then verify with `diff <(tail -n +4 AGENTS.md) <(tail -n +4 CLAUDE.md)`" -->
  <!-- src: embedthat 710e8a5 | 2026-09-12 -->
- **`gh run list --limit 1` does not mean "the build".** This repo also runs a
  Dependency Graph workflow, which finishes in seconds, so a bare
  `gh run list`/`gh run watch` on the newest row reports success while
  `docker-publish` is still building — that mistake produced a `compose pull` of
  the *previous* image and a "deployed" claim that was false. Watch by run ID,
  or filter on the workflow name.
  <!-- src: embedthat 710e8a5 | 2026-09-12 -->
- **`embedthat-redis-1` is left disabled in Tugtainer on purpose.** Only the bot
  and worker rows are enabled for auto-update; the datastore is not, because its
  volume is the one the prod compose warns must never be `down -v`'d. An
  apparently "missing" Tugtainer row for redis is the intended state.
  <!-- src: embedthat 710e8a5 | 2026-09-12 -->

## Base image (measured 2026-09-10)

- **A site can block by TLS stack, so "works on my box" proves nothing about the
  container.** Every TikTok link failed in prod while the same yt-dlp, same
  version, same public IP (`37.99.43.34`) succeeded on g15. Deterministic 5/5
  both ways. The container's request got a 537-byte "Site Maintenance" edge page;
  the working one got the 1462-byte WAF challenge page, which the extractor
  retries with a challenge cookie.
- **Reproduce the split in two commands** before touching code:
  `docker compose -f compose.prod.yml exec worker .venv/bin/python -c` a
  `ydl.urlopen(Request(<video page>)).read()` and compare its length against the
  same call on a stock `python:3.12-slim-trixie` container on the same host.
- Ruled out on evidence, so do not re-try them: newer yt-dlp (2026.08.19 fails
  too), `curl_cffi` impersonation (the extractor forces its own `chrome` target,
  which resolves to the macOS build → same 537; `chrome:windows` fetches the page
  fine but `extract_info` never uses it), cookies, yt-dlp cache, DNS, egress IP.
- `curl_cffi` >= 0.16 is rejected by yt-dlp 2026.07.04 (`Only ... 0.5.10 and
  0.10.x through 0.15.x are supported`) — pin `<0.16` if impersonation is ever
  actually needed.
- The trixie bump carries ffmpeg 5.1 → 7.1; the YouTube merge and `split_video`
  paths were verified at 480p on the built image, not assumed.

## Cookie jar (COOKIES_FILE)

- **It never reaches the YouTube pipeline.** `cookie_opts()` is called only from
  `bot/util/social/download.py` and `bot/util/audio/download.py`; YouTube goes
  through pytubefix, which is passed no cookies at all. A YouTube login wall is
  not fixable by exporting a jar.
- Live at `~/my/vps/homeserver/embedthat/cookies/cookies.txt` on latitude
  (burner Instagram account, placed 2026-09-08). yt-dlp rewrites it in place on
  close **preserving uid 1000**. The write-back legitimately drops session-only
  cookies whose expiry is `0` (`rur`); a shorter file after the first download is
  not corruption.
- **Installing a new jar is a Telegram upload now, not an `scp`.** Send the
  exported `.txt` to the bot from `ADMIN_CHAT_ID`; `install_cookies` in
  `bot/worker/actors.py` merges it, backs the old one up as `cookies.txt.bak-*`,
  clears `cookies:stale-alerted`, replies with a per-site cookie count and
  deletes the upload from the chat. The merge, the `sessionid` gate and the
  ownership handling live in `bot/util/cookies.py`. The manual procedure below
  is still the reference for what that code does — and the fallback when the bot
  itself is down.
- **The jar is MULTI-SITE — never `scp` a single-site export over it.** It also
  carries tiktok, vk, x.com, reddit, soundcloud and threads cookies, and a
  browser's per-site export would silently drop them all. Merge instead: strip
  the target domain's lines from the live jar, append the new export's, then `cp`
  onto the existing file so uid 1000 and mode 600 survive. Back the old one up
  first (`cookies.txt.bak-<date>`).
- **`grep -c sessionid` is THE check on any Instagram export — the jar placed on
  2026-09-08 never had one** (found 2026-09-15, after Instagram warned about
  automated activity on the burner). `sessionid` is HttpOnly, so any exporter
  reading `document.cookie` cannot see it and writes a jar that looks complete —
  `csrftoken` and `ds_user_id` are there, and `mid`/`datr`/`ig_did` still tie
  every request to the account. Use an extension that exports HttpOnly cookies
  (Get cookies.txt LOCALLY). Nobody noticed for a week because public reels need
  no session at all, so the success rate looked fine.
- **Cookies are spent only behind a login wall** since `e51e2fb`: `extract_info()`
  in `bot/util/ytdlp.py` runs anonymously and retries with the jar when
  `is_login_wall()` matches. Consequence — the jar is refreshed only by walled
  posts now, not by every request.
- **Discriminate bad-jar from stale-extractor before re-exporting.** yt-dlp in
  the image lags (2026.07.04 as of this writing) and the Instagram extractor
  churns fast. Control: `https://www.instagram.com/reel/Dc8fZX9idsQ/` needs no
  login — if that fails too, it is the extractor, not the cookies. Walled
  reference post: `https://www.instagram.com/reel/DcMT3ZEtSuN/`.
- Verify with a real download inside `embedthat-worker-1`, never a metadata
  probe — extraction succeeding with cookies present is not proof of bytes.

## Instagram carousels & yt-dlp (measured 2026-09-15, post `DdLlGOuGdlE`, 12 videos + 1 photo)

- **`?img_index=N` is ignored by yt-dlp** — 2026.07.04 and the pinned 2026.08.19
  both return the full 13-entry playlist for `img_index=3` and `img_index=13`
  alike. `carousel_index()` in `bot/util/social/download.py` parses it and
  translates to `playlist_items`; nothing upstream does it for you.
- **`noplaylist: True` never narrowed a carousel either.** A post URL *is* the
  playlist, so the flag has no video-plus-playlist case to disambiguate: all 13
  entries were extracted and downloaded, and the old `glob("*.mp4")[0]` kept an
  arbitrary one. Removed.
- **Entry positions map 1:1 onto the carousel, photo included** — 13 entries with
  the still at position 13. No off-by-one, so `img_index` → `playlist_items` is
  a straight translation. Re-check this if a post ever shows fewer entries than
  items.
- **`ignore_no_formats_error` survives the metadata stage but NOT a download.**
  With `download=False` a photo entry comes back with 0 formats and a warning;
  with `download=True` the extractor's `No video formats found!` aborts the whole
  post and takes the healthy videos with it. Hence the two-pass structure —
  enumerate, then download only the positions that really are video.
  `ignoreerrors` over one pass would work too, and would also swallow genuine
  failures, which is why it was not chosen.
- **A carousel still is not downloadable as media.** It resolves to 0 formats and
  exists only as `thumbnails` — 14 of them, all with `width`/`height`/
  `preference` `None`, so ordering is the only handle; yt-dlp sorts worst-to-best
  and the last one is the original (1440x1800 JPEG, 87 KB, served from a URL
  named `.heic`). Fetched over plain HTTP and probed for real dimensions.
- **Telegram accepts a mixed photo+video media group**, verified by sending
  10 + 3 into the dump chat. The cap is 10 per group, so a 13-item post is two
  messages.
- **A carousel delivers partially.** `ignoreerrors="only_download"` on the
  download pass skips an item whose media 403s instead of aborting the post;
  the failed positions are re-asked for once, on their own, and whatever is
  still missing is named in the caption. Scoped to `only_download` on purpose —
  an *extraction* error still raises, so a login wall keeps reaching
  `extract_info`'s cookie retry. Empty is still a failure: `SocialDownloadError`
  if nothing at all came through.
- **Never infer "video vs photo" from whether a file landed on disk.** The kind
  comes from the metadata pass (`entry["formats"]` non-empty) and nothing else.
  Deciding it by file existence means a video whose download failed falls through
  to the photo branch and is delivered as its own poster frame: forcing one item
  of the 13-item post to fail produced 11 videos + 2 photos and reported nothing
  missing, instead of 12 items and a gap at position 5. Caught only because the
  probe asserted the counts.
