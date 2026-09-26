# Local Bot API Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the bot and worker through our own `telegram-bot-api` server in local mode so uploads go up to 2000 MB, rehearsed on the debug bot before production is logged out of the cloud.

**Architecture:** One factory (`bot/util/tg.py::make_bot`) becomes the only place a `Bot` is built; it points the session at `settings.bot_api_url` when set. The upload limit is derived from that same setting, so there is one switch, not two. A new `telegram-bot-api` compose service (no host port) holds the token; the worker mounts its volume read-only at the identical path so local-mode `download_file` works. Cached `file_id`s that the new server rejects degrade into a re-download on every cache path.

**Tech Stack:** Python 3.12, aiogram 3.30, dramatiq + redis, pydantic-settings, Docker Compose, `aiogram/telegram-bot-api` image pinned by digest.

**Spec:** `docs/superpowers/specs/2026-09-26-local-bot-api-server-design.md` (amended alongside this plan -- read both).

## Global Constraints

- There is **no test suite**. The gate per task is: no NEW pyright/ruff findings versus the Task 0 baseline (compare finding sets, not totals), plus the import smoke test through all four doors: `main.py`, `bot.worker.actors`, `bot.events`, `bot.events.handlers.stats`.
- **Never read secret VALUES into context.** Key names only: `cut -d= -f1 <file>`. Move secrets with pipes that never print them. Never `cat`, `grep` without `-q`/redirection, or `docker compose config` a `.env`.
- Never edit in the main checkout `/home/me/my/embedthat`. This worktree only. The `vps` repo gets its own workspace (Task 9).
- Never `git stash` (the stack is shared across worktrees). Never `docker compose down -v` on the prod project (`name: embedthat` holds the redis volume).
- Reach the prod host as `ssh latitude.gg.ez` (FQDN, never bare `latitude`).
- `BOT_API_URL` empty or unset = cloud server, 50 MB limit, behaviour identical to today. Set = local server, 2000 MB limit.
- The server gets **no host port**. It is reachable only as `http://telegram-bot-api:8081` on the compose network.
- Image: `aiogram/telegram-bot-api`, pinned by digest (`name:tag@sha256:...`), vetted in Task 5. Tugtainer auto-update stays OFF for it.
- Artifacts (code comments, commits, docs) in English. Commit AND push after each task. Commit prefixes follow the repo: `config:`, `tg:`, `social:`, `audio:`, `compose:`, `spec:`, `release: X.Y.Z`.
- Deploy only by `v*` tag; watch the `docker-publish` run BY ID, never `gh run list --limit 1`.

## Review Focus

1. **`BOT_API_URL=` present but empty in `.env`** -- must mean cloud + 50 MB, not "local server at ''". Pinned by the truthiness check in Task 1's verification.
2. **Rollback that unsets only `BOT_API_URL`** -- the upload limit must fall back to 50 MB with it, or every large video fails against the cloud. Pinned by deriving the limit from the URL (Task 1) and checked in Task 1's verification.
3. **A large upload taking longer than 60 s end-to-end** (the local server answers only after pushing the file on to Telegram) -- must not trip the `TelegramNetworkError` retry loop and post the same 2 GB file three times to the dump chat. Pinned by the worker-only upload timeout (Task 2) and by the >50 MB upload in the rehearsal (Task 7).
4. **Tapping a cached audio page / 🎵 button whose ids died in the move** -- must re-download, not raise a CRITICAL on every tap, and must not have the dead ids refilled from `au:` keys. Pinned by Task 4 and the pre-logout capture in Task 7.
5. **Admin uploads a cookie jar after the switch** -- the worker must read the file off the server's volume; a missing or mis-pathed mount breaks it silently exactly when the jar has expired. Pinned by the cookie upload in Task 7.

---

### Task 0: Record the lint baseline

**Files:** none in the repo. Baseline lives in `$BASE=/tmp/embedthat-local-bot-api-baseline/`.

- [ ] **Step 1: Confirm the tree is clean and at the plan commit**

Run: `git status --short && git log --oneline -1`
Expected: no output from status; HEAD is the commit that added this plan.

- [ ] **Step 2: Capture pyright and ruff finding sets (line numbers stripped, so moved code does not look new)**

```bash
BASE=/tmp/embedthat-local-bot-api-baseline; mkdir -p $BASE
uv sync --frozen
uv run --frozen pyright --outputjson > $BASE/pyright.json || true
uv run --frozen ruff check . --output-format json > $BASE/ruff.json || true
jq -r '.generalDiagnostics[] | "\(.file)|\(.rule // "-")|\(.message)"' $BASE/pyright.json | sed "s|$PWD/||" | sort > $BASE/pyright.set
jq -r '.[] | "\(.filename)|\(.code)|\(.message)"' $BASE/ruff.json | sed "s|$PWD/||" | sort > $BASE/ruff.set
wc -l $BASE/*.set
```

Expected: two non-empty `.set` files (the repo carries known debt).

- [ ] **Step 3: Save the gate as a script every later task runs**

Write `$BASE/gate.sh` (outside the repo):

```bash
#!/usr/bin/env bash
# Usage: bash /tmp/embedthat-local-bot-api-baseline/gate.sh   (from the worktree root)
set -u
BASE=/tmp/embedthat-local-bot-api-baseline
uv run --frozen pyright --outputjson > $BASE/pyright.now.json || true
uv run --frozen ruff check . --output-format json > $BASE/ruff.now.json || true
jq -r '.generalDiagnostics[] | "\(.file)|\(.rule // "-")|\(.message)"' $BASE/pyright.now.json | sed "s|$PWD/||" | sort > $BASE/pyright.now.set
jq -r '.[] | "\(.filename)|\(.code)|\(.message)"' $BASE/ruff.now.json | sed "s|$PWD/||" | sort > $BASE/ruff.now.set
echo "== NEW pyright =="; comm -13 $BASE/pyright.set $BASE/pyright.now.set
echo "== NEW ruff =="; comm -13 $BASE/ruff.set $BASE/ruff.now.set
echo "== import smoke =="
BOT_TOKEN=1:x DUMP_CHAT_ID=0 uv run --frozen python -c "import main, bot.worker.actors, bot.events, bot.events.handlers.stats; print('imports ok')"
```

Run: `bash /tmp/embedthat-local-bot-api-baseline/gate.sh`
Expected: both NEW sections empty, `imports ok`. (If the import smoke fails on a clean tree, e.g. because redis is unreachable at import, record the exact error here and treat that same error as the baseline.)

Nothing to commit.

---

### Task 1: One switch -- `bot_api_url` and the derived upload limit

**Files:**
- Modify: `bot/config.py` (class `Settings`)
- Modify: `.env.dist`

**Interfaces:**
- Produces: `settings.bot_api_url: str | None`; `settings.max_upload_size_bytes: int` (property). Every later task uses these two names.

- [ ] **Step 1: Add the setting and the property**

In `bot/config.py`, below `cookies_user_agent`, add:

```python
    # Our own telegram-bot-api server in local mode, e.g. http://telegram-bot-api:8081.
    # Unset or empty = Telegram's cloud server, exactly the pre-2026-09-26 behaviour.
    bot_api_url: str | None = Field(default=None, validation_alias=AliasChoices("bot_api_url"))
```

and below the `timezone` property:

```python
    @property
    def max_upload_size_bytes(self) -> int:
        # Derived from bot_api_url, never set on its own: a rollback that unsets
        # only the URL must also drop the limit, or every large video is sent to
        # the cloud and fails. Decimal MB on the local side on purpose -- it stays
        # under the 2000 MB cap whichever unit Telegram means.
        if self.bot_api_url:
            return 2000 * 1000 * 1000
        return 50 * 1024 * 1024
```

- [ ] **Step 2: Document the keys in `.env.dist`**

Append:

```
# optional: our own telegram-bot-api server (local mode) -- lifts the upload cap
# from 50 MB to 2000 MB. Unset or empty = Telegram's cloud server.
BOT_API_URL
# credentials the telegram-bot-api service itself reads (my.telegram.org -> API)
TELEGRAM_API_ID
TELEGRAM_API_HASH
```

- [ ] **Step 3: Verify the switch, including the empty-string and rollback cases**

```bash
for v in "" "http://telegram-bot-api:8081"; do
  BOT_TOKEN=1:x DUMP_CHAT_ID=0 BOT_API_URL="$v" uv run --frozen python -c \
    "from bot.config import settings as s; print(repr(s.bot_api_url), s.max_upload_size_bytes)"
done
BOT_TOKEN=1:x DUMP_CHAT_ID=0 uv run --frozen python -c \
  "from bot.config import settings as s; print(repr(s.bot_api_url), s.max_upload_size_bytes)"
```

Expected, in order: `'' 52428800`, `'http://telegram-bot-api:8081' 2000000000`, `None 52428800`.

- [ ] **Step 4: Gate**

Run: `bash /tmp/embedthat-local-bot-api-baseline/gate.sh` -- expected: no NEW findings, `imports ok`.

- [ ] **Step 5: Commit and push**

```bash
git add bot/config.py .env.dist
git commit -m "config: BOT_API_URL, with the upload limit derived from it"
git push
```

---

### Task 2: `make_bot` -- the only place a `Bot` is built

**Files:**
- Create: `bot/util/tg.py`
- Modify: `bot/worker/actors.py` -- the five `Bot(token=settings.bot_token)` lines in `process_youtube_link`, `process_youtube_audio`, `process_social_link`, `process_audio_page`, `install_cookies`
- Modify: `bot/util/telegram_log_handler.py::TelegramAlertHandler._send`
- Modify: `main.py::main`

**Interfaces:**
- Consumes: `settings.bot_api_url`, `settings.bot_token` (Task 1).
- Produces: `make_bot(token: str | None = None, *, uploads: bool = False, **kwargs: Any) -> Bot`; `UPLOAD_TIMEOUT: int` in `bot/util/tg.py`.

Why `uploads` exists: in local mode the server answers a `sendVideo` only after it has pushed the file on to Telegram, which for a 2000 MB file takes far longer than aiogram's 60 s default. A timeout raises `TelegramNetworkError`, and every upload helper retries that three times -- three copies of the same 2 GB file in the dump chat. The longer timeout is worker-only because aiogram polling adds `session.timeout` to every `getUpdates` wait (`Dispatcher._listen_updates`), so a 30-minute session timeout in `main.py` would hide a hung poll for 30 minutes. It is also local-only, so the cloud path keeps exactly today's behaviour.

- [ ] **Step 1: Create the factory**

`bot/util/tg.py`:

```python
from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from bot.config import settings

# How long an upload may take end to end against our own server, which replies
# only after it has pushed the file on to Telegram. Worker-only: polling adds the
# session timeout to every getUpdates wait, so main.py must keep the default.
UPLOAD_TIMEOUT = 30 * 60


def make_bot(token: str | None = None, *, uploads: bool = False, **kwargs: Any) -> Bot:
    """The only place a `Bot` is built. The API server is per-session, so a `Bot`
    constructed anywhere else silently keeps talking to the cloud -- with a token
    that is logged out there once we switch."""
    session = None
    if settings.bot_api_url:
        api = TelegramAPIServer.from_base(settings.bot_api_url, is_local=True)
        if uploads:
            session = AiohttpSession(api=api, timeout=UPLOAD_TIMEOUT)
        else:
            session = AiohttpSession(api=api)
    return Bot(token or settings.bot_token, session=session, **kwargs)
```

- [ ] **Step 2: Replace the five actor sites**

In `bot/worker/actors.py`, add `from bot.util.tg import make_bot` to the `bot.util` import group (keep isort order: after `bot.util.social.schema`, before `bot.util.youtube.enum`), and change each of the five lines

```python
    bot = Bot(token=settings.bot_token)
```

to

```python
    bot = make_bot(uploads=True)
```

`Bot` stays imported -- it is still used in type annotations.

- [ ] **Step 3: Replace the alert handler site**

In `bot/util/telegram_log_handler.py::_send`, change `bot = Bot(token=self._token)` to `bot = make_bot(self._token)`. Replace the import `from aiogram import Bot` with `from bot.util.tg import make_bot` (in the first-party group next to `from bot.config import settings`), since `Bot` is no longer referenced there.

- [ ] **Step 4: Replace `main.py`**

```python
    the_bot = make_bot(default=DefaultBotProperties(parse_mode="HTML"))
```

Replace `from aiogram import Bot` with `from bot.util.tg import make_bot` in the first-party group.

- [ ] **Step 5: Prove no other construction site is left**

Run: `git grep -nE '\bBot\(' -- '*.py'`
Expected: exactly one hit, in `bot/util/tg.py`.

- [ ] **Step 6: Prove the factory wires the session correctly in both modes**

```bash
BOT_TOKEN=1:x DUMP_CHAT_ID=0 BOT_API_URL=http://telegram-bot-api:8081 uv run --frozen python -c "
from bot.util.tg import make_bot
b = make_bot(uploads=True); print(b.session.api.is_local, b.session.api.base, b.session.timeout)
b = make_bot(); print(b.session.api.is_local, b.session.timeout)"
BOT_TOKEN=1:x DUMP_CHAT_ID=0 uv run --frozen python -c "
from bot.util.tg import make_bot
b = make_bot(uploads=True); print(b.session.api.is_local, b.session.api.base, b.session.timeout)"
```

Expected:
```
True http://telegram-bot-api:8081/bot{token}/{method} 1800
True 60.0
False https://api.telegram.org/bot{token}/{method} 60.0
```

- [ ] **Step 7: Gate** -- `bash /tmp/embedthat-local-bot-api-baseline/gate.sh`, no NEW findings, `imports ok`.

- [ ] **Step 8: Commit and push**

```bash
git add bot/util/tg.py bot/worker/actors.py bot/util/telegram_log_handler.py main.py
git commit -m "tg: build every Bot through make_bot, the local-server switch"
git push
```

---

### Task 3: The upload limit replaces `MAX_FILE_SIZE_BYTES` everywhere

**Files:**
- Modify: `bot/util/youtube/video.py` (constant at line 16; uses at ~121 and ~248)
- Modify: `bot/worker/pipeline.py` (import at ~21; uses in `_split_oversized` ~110, ~117 and `handle_social_video` ~232)
- Modify: `bot/util/audio/download.py` (import at line 7; check and message at ~142-144)

**Interfaces:**
- Consumes: `settings.max_upload_size_bytes` (Task 1).
- Produces: `MAX_FILE_SIZE_BYTES` no longer exists.

- [ ] **Step 1: Remove the constant and use the setting in `video.py`**

Delete `MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50MB`. Add `from bot.config import settings` if the module does not already import it (check the import block). Replace both uses:

```python
            max_size = settings.max_upload_size_bytes * 0.98
```
```python
        too_big_files = [file for file in video_paths if file.stat().st_size > settings.max_upload_size_bytes]
```

- [ ] **Step 2: `pipeline.py`**

Drop `MAX_FILE_SIZE_BYTES,` from the `from bot.util.youtube.video import (...)` block (pipeline already imports `settings` -- it uses `settings.dump_chat_id`). Replace the three uses with `settings.max_upload_size_bytes`:

```python
    n_parts = math.ceil(file_size / settings.max_upload_size_bytes)
```
```python
    while any(p.stat().st_size > settings.max_upload_size_bytes for p in file_paths):
```
```python
            if media.kind == "photo" or media.file_path.stat().st_size <= settings.max_upload_size_bytes:
```

- [ ] **Step 3: `audio/download.py` -- the third consumer, with a hardcoded "50MB" in its message**

Replace `from bot.util.youtube.video import MAX_FILE_SIZE_BYTES` with `from bot.config import settings` (unless already imported). Replace the check:

```python
    limit = settings.max_upload_size_bytes
    if file_path.stat().st_size > limit:
        file_path.unlink(missing_ok=True)
        raise AudioDownloadError(
            f"{track.title or track.webpage_url} is too large to send (over {limit // 1_000_000} MB)"
        )
```

- [ ] **Step 4: Prove nothing references the old constant**

Run: `git grep -n MAX_FILE_SIZE_BYTES`
Expected: no output.

- [ ] **Step 5: Gate** -- no NEW findings, `imports ok`.

- [ ] **Step 6: Commit and push**

```bash
git add bot/util/youtube/video.py bot/worker/pipeline.py bot/util/audio/download.py
git commit -m "config: the upload limit follows the API server, in all three places"
git push
```

---

### Task 4: Cached `file_id`s the new server rejects degrade into a re-download

**Files:**
- Modify: `bot/util/audio/pager.py` (`redeliver_page`; new `StaleFileIdsError`, `invalidate_page`)
- Modify: `bot/handlers.py` (`get_audio_page`, `_process_social_url`, the `aud:` callback handler around lines ~185-215; new helper `_queue_audio_page`)
- Modify: `bot/worker/actors.py::_notify_audio_page_waiters_success`

**Interfaces:**
- Produces (in `bot/util/audio/pager.py`): `class StaleFileIdsError(Exception)`; `async def invalidate_page(redis_client: redis.Redis, audio: AudioRequestData, page: int) -> None`; `redeliver_page` keeps its signature and now raises `StaleFileIdsError` (after invalidating) instead of `TelegramBadRequest` when the send fails.
- Produces (in `bot/handlers.py`): `async def _queue_audio_page(chat_id: int, chat_type: str, root_message_id: int, hash16: str, page: int) -> None`.

Already self-healing, leave alone: the YouTube video path and the social video path (both catch `TelegramBadRequest` on `reply_to`, delete the key and fall through to a download). The gaps are the audio pager (three callers) and the 🎵 `aud:` button. The pager heal must also drop the per-track `au:<extractor>:<id>` keys, because `actors._resolve_cached_tracks` refills track ids from them -- clearing only `da:` would bring the dead ids straight back.

- [ ] **Step 1: Pager -- invalidate and raise a typed error**

In `bot/util/audio/pager.py`, add below `_messages_key`:

```python
class StaleFileIdsError(Exception):
    """Telegram rejected a cached page. By the time this is raised the page's file
    ids are already cleared from `da:` and from the per-track `au:` keys, so a
    retry downloads instead of hitting the same dead ids."""


async def invalidate_page(redis_client: redis.Redis, audio: AudioRequestData, page: int) -> None:
    for track in audio.page(page):
        if track.file_id:
            await redis_client.delete(track.cache_key)
            track.file_id = None
    await redis_client.set(audio.cache_key, audio.model_dump_json())
```

and wrap the send in `redeliver_page`:

```python
    try:
        new_ids = await audio.send_to_chat(bot, chat_id, reply_to_message_id=root_message_id, page=page)
    except TelegramBadRequest as e:
        log.info("cached page %d of %s rejected (%s), clearing its file ids", page, audio.cache_key, e)
        await invalidate_page(redis_client, audio, page)
        raise StaleFileIdsError(audio.cache_key) from e
    await redis_client.set(key, json.dumps(new_ids))
```

- [ ] **Step 2: Handlers -- extract the enqueue helper from `get_audio_page`**

In `bot/handlers.py`, add near `_process_social_url`:

```python
async def _queue_audio_page(chat_id: int, chat_type: str, root_message_id: int, hash16: str, page: int) -> None:
    page_key = f"da:{hash16}:page:{page}"
    waiter = Waiter(chat_id=chat_id, chat_type=chat_type, reply_to_message_id=root_message_id)
    is_first = await register_waiter(redis_client, page_key, waiter, _SOCIAL_WAITERS_TTL)
    if is_first:
        try:
            process_audio_page.send(chat_id, hash16, page)
        except Exception:
            await clear_waiters(redis_client, page_key)
            raise
```

(`Waiter.chat_type` is declared `str` in `bot/worker/waiters.py`; aiogram's `chat.type` is a `str` subclass, so passing it straight through matches what the existing code does.)

Rewrite the tail of `get_audio_page` from `if all(t.file_id for t in page_tracks):` to the end:

```python
    if all(t.file_id for t in page_tracks):
        try:
            await redeliver_page(
                redis_client, callback.message.bot, callback.message.chat.id, root_message_id, audio, page,
            )
            return
        except StaleFileIdsError:
            pass  # ids already cleared -- fall through to a fresh download

    log.info("cache miss for %s page %d, registering waiter", cache_key, page)
    await _queue_audio_page(callback.message.chat.id, callback.message.chat.type, root_message_id, hash16, page)
```

- [ ] **Step 3: Handlers -- the audio cache hit in `_process_social_url`**

```python
    if audio_raw := await redis_client.get(audio.cache_key):
        cached_audio = AudioRequestData.model_validate_json(audio_raw)
        log.info("cache hit (audio) for %s", audio.cache_key)
        try:
            await redeliver_page(redis_client, message.bot, message.chat.id, message.message_id, cached_audio, page=1)
        except StaleFileIdsError:
            await _queue_audio_page(message.chat.id, message.chat.type, message.message_id, cached_audio.hash16, 1)
        return
```

Import: `from .util.audio.pager import StaleFileIdsError, redeliver_page`.

- [ ] **Step 4: Handlers -- the 🎵 `aud:` button**

In the callback handler that reads `video.audio_file_id`, wrap the send so a dead id re-extracts instead of raising (today a `TelegramBadRequest` there reaches `error_handler` and fires a CRITICAL alert on every tap):

```python
    video = YouTubeVideoData.model_validate_json(video_raw)
    if video.audio_file_id:
        if await asyncio.to_thread(video.ensure_metadata):
            # promote a pre-metadata entry so the next tap skips YouTube entirely
            await redis_client.set(cache_key, video.model_dump_json())
        try:
            await callback.message.answer_audio(
                video.audio_file_id,
                performer=video.author,
                title=video.title,
                duration=video.length,
            )
            return
        except TelegramBadRequest:
            log.info("cached audio file id for %s rejected, re-extracting", cache_key)
            video.audio_file_id = None
            await redis_client.set(cache_key, video.model_dump_json())
```

The existing waiter registration below it then runs unchanged, and `process_youtube_audio` sees `audio_file_id` empty and extracts.

- [ ] **Step 5: Worker -- the post-download delivery**

In `bot/worker/actors.py`, import `StaleFileIdsError` alongside `redeliver_page`, and rewrite `_notify_audio_page_waiters_success`:

```python
async def _notify_audio_page_waiters_success(
    redis_client: redis.Redis, bot: Bot, waiters: list[Waiter], audio: AudioRequestData, page: int,
) -> None:
    for i, waiter in enumerate(waiters):
        try:
            await redeliver_page(redis_client, bot, waiter.chat_id, waiter.reply_to_message_id, audio, page)
        except StaleFileIdsError:
            # A dead id came back from an `au:` key. redeliver_page has already
            # cleared it, so a resend downloads; the rest of the waiters would
            # otherwise get a misleading "no tracks could be downloaded".
            await _notify_waiters_failure(bot, waiters[i:], "❌ Couldn't deliver this page, please send the link again.")
            return
```

- [ ] **Step 6: Gate** -- no NEW findings, `imports ok`. The functional check is the rehearsal (Task 7, "cached ids from before the move").

- [ ] **Step 7: Commit and push**

```bash
git add bot/util/audio/pager.py bot/handlers.py bot/worker/actors.py
git commit -m "audio: a rejected cached file id re-downloads instead of raising"
git push
```

---

### Task 5: Vet and pin the server image

**Files:**
- Modify: `docs/superpowers/specs/2026-09-26-local-bot-api-server-design.md` ("Open questions" -> resolved)

**Interfaces:**
- Produces: `IMAGE=aiogram/telegram-bot-api:latest@sha256:<index digest>` -- the exact string Tasks 6 and 9 paste.

- [ ] **Step 1: Read the image's source**

Fetch `https://github.com/aiogram/telegram-bot-api` and read its `Dockerfile` and entrypoint script. Confirm, and note each in the spec:
- it builds from `tdlib/telegram-bot-api` source (which ref/commit), not from a downloaded binary;
- the entrypoint maps `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and `TELEGRAM_LOCAL` to the binary's `--api-id`, `--api-hash`, `--local`;
- the working directory is `/var/lib/telegram-bot-api` and the listen port is 8081;
- nothing phones home or adds flags beyond those (e.g. no `--http-stat-port` exposed publicly).

If any of these fails, stop and report to the user -- the fallback is building `tdlib/telegram-bot-api` ourselves, which is his call.

- [ ] **Step 2: Resolve the digest**

Run: `docker buildx imagetools inspect aiogram/telegram-bot-api:latest | head -5`
Take the top-level `Digest:` (the multi-arch index). Verify the linux/amd64 manifest is listed.

- [ ] **Step 3: Record in the spec**

Replace the first bullet of "Open questions" with a resolved note: image, digest, date, what was checked in Step 1. Update the YAML block under "Architecture" to the pinned `image:` line.

- [ ] **Step 4: Commit and push**

```bash
git add docs/superpowers/specs/2026-09-26-local-bot-api-server-design.md
git commit -m "spec: pin the telegram-bot-api image by digest after reading its build"
git push
```

---

### Task 6: The server in the dev compose

**Files:**
- Modify: `compose.yml`

**Interfaces:**
- Consumes: `IMAGE` from Task 5.
- Produces: service `telegram-bot-api`, volume `bot_api_data` mounted rw on the server and ro on `worker` at `/var/lib/telegram-bot-api`.

The dev compose has no `../compose.base.yml` (that exists only on latitude), so it keeps the dev file's own style: `restart: unless-stopped`, no `extends`.

- [ ] **Step 1: Add the service, the worker mount and the volume**

```yaml
    telegram-bot-api:
        # Our own Bot API server in local mode (2000 MB uploads). No host port on
        # purpose: it has no auth of its own, anything that reaches it can act as
        # the bot. bot/worker reach it as http://telegram-bot-api:8081.
        image: aiogram/telegram-bot-api:latest@sha256:<DIGEST FROM TASK 5>
        env_file:
            - .env
        environment:
            TELEGRAM_LOCAL: 1
        volumes:
            - bot_api_data:/var/lib/telegram-bot-api
        restart: unless-stopped
```

Under `worker:` `volumes:` add:

```yaml
            # Local mode hands out file paths on the server's disk, and
            # download_file reads them directly -- so the worker (the only
            # downloader: install_cookies) must see them at the same path.
            - bot_api_data:/var/lib/telegram-bot-api:ro
```

Under top-level `volumes:` add `bot_api_data:`.

- [ ] **Step 2: Validate the file without rendering env values**

Run: `docker compose config --quiet && docker compose config --services`
Expected: exit 0; services `bot`, `worker`, `redis`, `telegram-bot-api`. (`--quiet` prints nothing; never run plain `docker compose config` -- it prints the interpolated `.env`.)

If `.env` is not in the worktree yet, `config` fails on the missing `env_file`: `touch .env` first for this check only, then `rm .env` (Task 7 copies the real one).

- [ ] **Step 3: Commit and push**

```bash
git add compose.yml
git commit -m "compose: telegram-bot-api service for local mode (dev)"
git push
```

---

### Task 7: Rehearsal on the debug bot

Functional gate for Tasks 1-6. Needs the user for the Telegram side (sending links to `@assinstantbot`). Record every answer in the spec's "Rehearsal results" section (Step 12).

**Files:**
- Create (not committed, gitignored or outside the repo): `.env` (copy), `/tmp/embedthat-local-bot-api-baseline/cloud.yml`, `/tmp/embedthat-local-bot-api-baseline/local.yml`
- Modify: spec (results)

- [ ] **Step 1: Make sure nothing else polls the debug bot**

Run: `docker ps --format '{{.Names}} {{.Image}}' | grep -i embedthat`
If a dev stack from the main checkout is up (`embedthat-bot-1` with `embedthat:dev` etc.), two pollers would fight over updates: ask the user before stopping it. Prod containers (`metheoryt/embedthat`) are on latitude, not here.

- [ ] **Step 2: Bring the `.env` in without reading it**

```bash
cp /home/me/my/embedthat/.env .env
cut -d= -f1 .env | grep -vE '^\s*(#|$)'
```

Expected key names include `BOT_TOKEN`, `DUMP_CHAT_ID`, `ADMIN_CHAT_ID`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`. If `ADMIN_CHAT_ID` is missing, the alert and cookie checks (Steps 9-10) cannot run -- tell the user. `git status --short` must not list `.env` (it is gitignored).

- [ ] **Step 3: Override files (outside the repo)**

`/tmp/embedthat-local-bot-api-baseline/cloud.yml`:

```yaml
services:
    bot:
        environment: { REDIS_URL: "redis://redis", BOT_API_URL: "" }
    worker:
        environment: { REDIS_URL: "redis://redis", BOT_API_URL: "" }
```

`/tmp/embedthat-local-bot-api-baseline/local.yml`:

```yaml
services:
    bot:
        environment: { REDIS_URL: "redis://redis", BOT_API_URL: "http://telegram-bot-api:8081" }
    worker:
        environment: { REDIS_URL: "redis://redis", BOT_API_URL: "http://telegram-bot-api:8081" }
```

(`REDIS_URL` override: the dev `.env` is written for a host-run `main.py` and points at `localhost`. If host port 6379 is already taken, add `redis: { ports: !reset [] }` to both files.)

- [ ] **Step 4: Phase A -- cloud. Seed the cache with cloud-issued ids BEFORE any logout**

```bash
O=/tmp/embedthat-local-bot-api-baseline
docker compose -f compose.yml -f $O/cloud.yml up -d --build redis bot worker
docker compose logs --tail 20 bot worker
```

Ask the user to send `@assinstantbot`: (a) one short Instagram/TikTok video link, (b) one YouTube link and tap 🎵, (c) one SoundCloud track or short playlist link. Then confirm the cache holds them:

```bash
for p in 'dl2:*' 'yt:*' 'da:*' 'au:*'; do echo "$p $(docker compose exec -T redis redis-cli --scan --pattern "$p" | wc -l)"; done
```

Expected: at least one key under each of `dl2:`, `yt:`, `da:`, `au:`. Write down the three links.

- [ ] **Step 5: Log the debug bot out of the cloud (this is production step 3, rehearsed)**

```bash
docker compose -f compose.yml -f $O/cloud.yml stop bot worker
docker compose -f compose.yml -f $O/cloud.yml run --rm --no-deps -T bot python -c "
import asyncio
from bot.util.tg import make_bot
async def go():
    b = make_bot()
    try:
        print('logOut:', await b.log_out())
    finally:
        await b.session.close()
asyncio.run(go())"
```

Expected: `logOut: True`. Note the time: the bot cannot log back into the cloud for 10 minutes.

- [ ] **Step 6: Phase B -- local server**

```bash
docker compose -f compose.yml -f $O/local.yml up -d telegram-bot-api
docker compose logs --tail 30 telegram-bot-api
docker compose exec -T telegram-bot-api du -sb /var/lib/telegram-bot-api
docker compose -f compose.yml -f $O/local.yml up -d bot worker
docker compose logs --tail 30 bot worker
```

Expected: server log shows it listening, no API-id error; bot log shows polling started with no `Unauthorized`. Record the baseline `du` number.

- [ ] **Step 7: Cached ids from before the move**

Ask the user to resend the three links from Step 4 and tap 🎵 again on the YouTube one. For each, record: delivered straight from the cache (ids survived) or re-downloaded (ids dead, heal worked). Evidence from logs:

```bash
docker compose logs --since 5m bot worker | grep -E "cache hit|rejected|invalid|cache miss|clearing"
```

Expected: every link ends delivered, with either no rejection or a `rejected ... clearing` / `cached file ids invalid` line followed by a fresh download. **No CRITICAL in the admin chat.**

- [ ] **Step 8: A video over 50 MB arrives as one file**

Ask the user for a link that used to be split (e.g. the 939-second VK video from 2026-09-26). Then:

```bash
docker compose logs --since 10m worker | grep -Ei "split|parts|sent |retrying|NetworkError"
docker compose exec -T telegram-bot-api du -sb /var/lib/telegram-bot-api
```

Expected: one file delivered, no `split`, no `retrying in 2 seconds`. Record the file's size, the wall time of the upload and the `du` delta -- that delta is the per-video disk cost the prune is sized from.

- [ ] **Step 9: The alert path works through the local server**

```bash
docker compose -f compose.yml -f $O/local.yml exec -T worker python -c "
import logging, time
from bot.util.telegram_log_handler import install_admin_alert_handler
install_admin_alert_handler()
logging.getLogger('rehearsal').critical('local bot api rehearsal: alert path ok')
time.sleep(5)"
```

Expected: the message appears in the debug bot's admin chat.

- [ ] **Step 10: Cookie-jar upload (the only local-mode download, across containers)**

Ask the user to upload any `.txt` file (a harmless dummy is fine) as a document to the debug bot from the admin chat. Expected reply: either the install report or `❌ Not installed — <jar validation reason>`. Either proves the worker read the file off the server's volume. A reply mentioning a missing file / `FileNotFoundError` / `Install failed` means the ro mount or path is wrong -- stop and fix Task 6 before going on. Then record where the uploaded file sits on the volume:

```bash
docker compose exec -T telegram-bot-api find /var/lib/telegram-bot-api -maxdepth 3 -type d
docker compose exec -T telegram-bot-api find /var/lib/telegram-bot-api -name '*.txt' -newermt '-10 minutes'
```

Record the directory layout (it is what Task 8's prune allowlists) and whether the jar copy lingers.

- [ ] **Step 11: Rollback rehearsal**

```bash
docker compose -f compose.yml -f $O/local.yml run --rm --no-deps -T bot python -c "
import asyncio
from bot.util.tg import make_bot
async def go():
    b = make_bot()
    try:
        print('local logOut:', await b.log_out())
    finally:
        await b.session.close()
asyncio.run(go())"
docker compose -f compose.yml -f $O/cloud.yml up -d bot worker
docker compose logs --tail 20 bot
```

Run only after the 10-minute window from Step 5 has passed. Expected: bot polls the cloud again without errors; one short link delivers. Then stop the dev stack: `docker compose down` (no `-v` needed here either -- the volumes are cheap to keep for a re-run).

- [ ] **Step 12: Record results and decide the flush**

Add a "Rehearsal results (2026-09-XX)" section to the spec: ids survived yes/no per path; large-video size, time and disk delta; the volume layout; whether the jar copy lingers; rollback worked. Decision rule for production: if any cached ids died, run the flush in Task 10 Step 4; if all survived, skip it.

```bash
git add docs/superpowers/specs/2026-09-26-local-bot-api-server-design.md
git commit -m "spec: rehearsal results on the debug bot"
git push
```

Surface to the user in one line: the uploaded cookie jar stays on the server volume until the prune removes it (Task 8), i.e. up to 24 h -- the extra on-disk copy the `install_cookies` docstring tried to avoid. Ask whether 24 h is acceptable or he wants the worker to delete it right after reading (needs a read-write mount).

---

### Task 8: Prune the server's media files

**Files:**
- Modify (vps repo, done in Task 9's workspace): `homeserver/embedthat/compose.prod.yml`

The server keeps every uploaded file and does not clean up (tdlib/telegram-bot-api #402, #303). The same directory also holds each bot's tdlib state (`td.binlog`, `db.sqlite*`) -- deleting those logs the bot out. So the prune deletes only files older than 24 h inside the media subdirectories Task 7 Step 10 recorded, never the state files.

**Interfaces:**
- Consumes: the directory layout from Task 7 Step 10.

- [ ] **Step 1: Write the service (in the Task 9 edit)**

```yaml
  telegram-bot-api-prune:
    extends:
      file: ../compose.base.yml
      service: base
    image: alpine:3.20
    # Deletes uploaded/downloaded media older than a day. Allowlisted subdirs
    # only: the same volume holds each bot's td.binlog/db.sqlite, and deleting
    # those logs the bot out. A file_id stays valid after its local copy goes.
    command: >
      sh -c 'while :; do
        find /var/lib/telegram-bot-api -type f -mmin +1440
          \( -path "*/videos/*" -o -path "*/documents/*" -o -path "*/photos/*"
             -o -path "*/music/*" -o -path "*/temp/*" -o -path "*/thumbnails/*" \)
          -print -delete;
        sleep 3600; done'
    volumes:
      - bot_api_data:/var/lib/telegram-bot-api
```

Replace the `-path` list with exactly the media directories Task 7 recorded (add any missing, drop any absent). If Task 7 showed the state files living inside one of these directories, stop and report instead.

- [ ] **Step 2: Verify after deploy (Task 9 Step 6)**

`ssh latitude.gg.ez 'cd ~/my/vps/homeserver/embedthat && docker compose logs --tail 20 telegram-bot-api-prune'` -- no errors; after a day, printed paths are media files only.

(Committed as part of Task 9.)

---

### Task 9: Production -- ship the code, add the idle server

Order matters: the app release first (a no-op while `BOT_API_URL` is unset), then the `vps` compose, then Task 10's switch.

**Files:**
- Modify: `pyproject.toml` (`version`), `uv.lock` if it records the project version
- Modify (vps repo): `homeserver/embedthat/compose.prod.yml`
- Modify (latitude, not in git): `~/my/vps/homeserver/embedthat/.env`

- [ ] **Step 1: Release the app**

Bump `version` in `pyproject.toml` (0.4.22 -> 0.4.23), run `uv lock`, commit `release: 0.4.23`, push the branch. Merge back by fast-forward through the base checkout (it must be on `main` and clean -- check with `git -C /home/me/my/embedthat status -sb`; if not, stop and ask):

```bash
git -C /home/me/my/embedthat merge --ff-only work/local-bot-api
git -C /home/me/my/embedthat push origin main
git -C /home/me/my/embedthat tag v0.4.23 && git -C /home/me/my/embedthat push origin v0.4.23
RUN=$(gh run list --workflow docker-publish.yml --limit 1 --json databaseId -q '.[0].databaseId'); gh run watch "$RUN" --exit-status
```

Confirm the run's `headSha` matches the tag. Within 15 min Tugtainer recreates bot and worker; confirm:
`ssh latitude.gg.ez 'docker exec embedthat-worker-1 grep ^version /app/pyproject.toml'` -> `version = "0.4.23"`. Send one short link to the prod bot: still works on the cloud.

- [ ] **Step 2: A `vps` workspace**

Create a workspace for the vps repo with the `us:worktrees` flow (`wt.sh create`), never by editing `/home/me/my/vps` directly.

- [ ] **Step 3: Edit `homeserver/embedthat/compose.prod.yml`**

Add under `services:` (after `worker`):

```yaml
  telegram-bot-api:
    extends:
      file: ../compose.base.yml
      service: base
    # Local-mode Bot API server: lifts uploads to 2000 MB. No host port on
    # purpose -- it has no auth of its own; bot/worker reach it by service name.
    # Pinned by digest and NOT auto-updated in Tugtainer: it holds the prod token.
    image: aiogram/telegram-bot-api:latest@sha256:<DIGEST FROM TASK 5>
    env_file: [ .env ]          # TELEGRAM_API_ID, TELEGRAM_API_HASH
    environment:
      TELEGRAM_LOCAL: 1
    volumes:
      - bot_api_data:/var/lib/telegram-bot-api
```

plus the `telegram-bot-api-prune` service from Task 8. Under `worker:` `volumes:` add, with a comment:

```yaml
      # Local mode: download_file reads the server's paths directly, so the
      # worker (install_cookies) must see them at the same path. Read-only.
      - bot_api_data:/var/lib/telegram-bot-api:ro
```

Under top-level `volumes:` add `bot_api_data:`. Validate locally (key names only): `docker compose -f homeserver/embedthat/compose.prod.yml config --services` from the workspace needs the `.env` -- skip locally and validate on latitude in Step 5 instead.

- [ ] **Step 4: Commit, merge, push the vps change**

Commit in the vps workspace (`embedthat: local telegram-bot-api server, idle until BOT_API_URL`), fast-forward it into `/home/me/my/vps` main the same way, push, then `ssh latitude.gg.ez 'git -C ~/my/vps pull --ff-only'`.

- [ ] **Step 5: Move the two credentials to latitude without reading them**

```bash
ssh latitude.gg.ez 'cut -d= -f1 ~/my/vps/homeserver/embedthat/.env'
```

If `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` are absent:

```bash
grep -E '^TELEGRAM_API_(ID|HASH)=' /home/me/my/embedthat/.env \
  | ssh latitude.gg.ez 'cat >> ~/my/vps/homeserver/embedthat/.env'
ssh latitude.gg.ez 'cut -d= -f1 ~/my/vps/homeserver/embedthat/.env'
```

Expected: both names now listed once. `BOT_API_URL` must NOT be there yet.

- [ ] **Step 6: Start only the new services**

```bash
ssh latitude.gg.ez 'cd ~/my/vps/homeserver/embedthat && docker compose config --quiet && docker compose up -d telegram-bot-api telegram-bot-api-prune && docker compose ps && docker compose logs --tail 30 telegram-bot-api'
```

Expected: server running and idle (no bot has used it). Bot and worker untouched, still on the cloud. In the Tugtainer UI (`http://latitude.gg.ez:9412` -> Containers) confirm `embedthat-telegram-bot-api-1` has check/update OFF.

The worker needs the new ro mount before the switch: `docker compose up -d worker` recreates it with the mount while `BOT_API_URL` is still unset (a no-op otherwise). Confirm with a short link.

---

### Task 10: Production switch

Do it at a quiet hour; tell the user before Step 2 (it is the step that cannot be undone for 10 minutes).

- [ ] **Step 1: Save the logs Tugtainer will not archive**

`ssh latitude.gg.ez 'cd ~/my/vps/homeserver/embedthat && docker compose logs --since 24h bot worker > ~/embedthat-pre-local-api-$(date +%F).log'`

- [ ] **Step 2: Stop, log out of the cloud**

```bash
ssh latitude.gg.ez 'cd ~/my/vps/homeserver/embedthat && docker compose stop bot worker && docker compose run --rm --no-deps -T bot python -c "
import asyncio
from bot.util.tg import make_bot
async def go():
    b = make_bot()
    try:
        print(\"logOut:\", await b.log_out())
    finally:
        await b.session.close()
asyncio.run(go())"'
```

Expected: `logOut: True`. Note the time.

- [ ] **Step 3: Set the URL**

`ssh latitude.gg.ez "echo 'BOT_API_URL=http://telegram-bot-api:8081' >> ~/my/vps/homeserver/embedthat/.env && cut -d= -f1 ~/my/vps/homeserver/embedthat/.env"`

- [ ] **Step 4: Flush cached ids -- ONLY if the rehearsal showed they die**

With bot and worker stopped (so no live `:lock` or waiter key is in use), delete every file-id-bearing entry:

```bash
ssh latitude.gg.ez 'cd ~/my/vps/homeserver/embedthat && for p in "dl2:*" "yt:*" "da:*" "au:*"; do
  n=$(docker compose exec -T redis redis-cli --scan --pattern "$p" | wc -l); echo "$p $n"
  docker compose exec -T redis redis-cli --scan --pattern "$p" | xargs -r -n 500 docker compose exec -T redis redis-cli DEL >/dev/null
done'
```

Skip entirely if Task 7 showed the ids survive -- Task 4's heal covers stragglers either way.

- [ ] **Step 5: Start on the local server**

```bash
ssh latitude.gg.ez 'cd ~/my/vps/homeserver/embedthat && docker compose up -d bot worker && sleep 10 && docker compose logs --tail 30 bot worker telegram-bot-api'
```

Expected: polling started, no `Unauthorized`.

- [ ] **Step 6: Verify (spec runbook step 5)**

`/stats` in the admin chat; one short link; one long link that used to be split -> arrives as one file; one link cached from before the move -> delivered (from cache or re-downloaded, no CRITICAL). Check `docker compose logs --since 15m worker | grep -Ei "retrying|NetworkError|CRITICAL"` is empty.

**Rollback** (any step fails), in this order: (1) `docker compose stop bot worker`; (2) with `BOT_API_URL` still in `.env`, run the Step 2 `run --rm` snippet -- it now logs out of the *local* server; (3) `sed -i '/^BOT_API_URL=/d' .env`; (4) `docker compose up -d bot worker`. Inside the 10 minutes after Step 2 the cloud refuses the login -- wait it out; nothing else is destructive and the redis volume is untouched.

- [ ] **Step 7: Record the outcome**

Add a short "Production switch (date)" note to the spec (what was verified, whether the flush ran), and a bullet under "Repo & deploy conventions" in `.claude/memory/project.md`: the stack now has a `telegram-bot-api` service holding the prod token, pinned by digest and excluded from Tugtainer; `BOT_API_URL` is the single switch; rollback needs a `logOut` against the local server and a 10-minute wait. Commit and push both (`spec:` / `memory:`).
