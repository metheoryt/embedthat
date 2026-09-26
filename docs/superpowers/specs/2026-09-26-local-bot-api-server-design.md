# Local Bot API server

**Date:** 2026-09-26
**Status:** design, approved in chat (approach B)

## Goal

Run our own `telegram-bot-api` server next to the bot on latitude, so the bot
uploads files up to 2000 MB instead of 50 MB.

Today a video that does not fit into 50 MB is cut into up to 10 parts by
`bot/worker/pipeline.py::_split_oversized`. A 939-second VK video measured on
2026-09-26 arrived as three parts at 852x480. With a local server nothing needs
to be split and nothing needs to be re-encoded to fit.

## Non-goals

- **Video normalisation (the 720p ceiling and the size-targeted re-encode) is
  out of scope.** It was designed in the same conversation and deliberately
  deferred: once the limit is 2000 MB the size argument disappears, and whether
  a quality ceiling is still wanted is a separate decision to make against the
  live result. The decisions that were settled are recorded below so they do not
  have to be re-argued if it is picked up.

  <details><summary>Deferred: the settled parameters</summary>

  - A new `_normalize_video(media, output_dir) -> MediaFile` in
    `bot/worker/pipeline.py`, called from `_handle_social_video` before the size
    check; `_split_oversized` stays as the fallback. The YouTube path is not
    touched.
  - **Measure by the short side, not the height.** `scale=-2:480` pins the
    height, which turns a portrait 1080x1920 into 270x480. Landscape caps the
    height, portrait caps the width, and the 720 ceiling is read the same way.
  - Two triggers: short side > 720 (always, even for a small file), or the file
    exceeds the upload limit (then also drop to 480 if the short side is above
    it).
  - x264, chosen over x265 on purpose: at 480p HEVC saves only 20-30%, encodes
    3-5x slower, and is not reliably playable in every Telegram client. The repo
    already made this call once -- `bot/util/youtube/video.py::pick_stream`
    filters YouTube streams to `avc1`.
  - Audio: AAC 96 kbps. Opus was asked for and does not work here -- mobile
    Telegram clients do not decode Opus in mp4, and the container where it is
    standard (WebM) would force VP9 and 5-10x the CPU.
  - Bitrate aims at a good 480p and no higher; below a floor of roughly
    450 kbps the re-encode is skipped and the video is split instead, because
    the agreed rule is not to sacrifice quality badly just to avoid a split.
  - The arithmetic that makes this narrow: a 50 MB budget minus 192 kbps of
    audio leaves one-file territory at about 6 minutes. Raising the upload limit
    is what actually moves the split boundary -- which is why this spec exists
    and that one is deferred.
  - If encoding ever proves expensive on latitude, `/dev/dri` is not passed into
    the worker today; VAAPI on the iGPU is the ready next step.

  </details>
- The splitting path stays in the code as a fallback. It is not removed.
- No change to the YouTube stream-selection logic.

## Approach

Rehearse the whole thing on the debug bot before the production bot is logged
out of the cloud.

The repo's local `.env` holds `@assinstantbot`, not the production token (see
`.claude/memory/project.md`, "Verification"), so the dev stack can point at a
local server with zero production risk. The two genuinely unknown things --
whether cloud-issued `file_id`s survive the move, and how the server behaves on
disk -- get answered there first.

The alternative, switching production directly, was rejected: `logOut` on the
cloud server cannot be undone for 10 minutes, and the unknowns would be measured
on real users.

## Architecture

A new `telegram-bot-api` service in the compose stack, reachable only on the
compose network:

```yaml
telegram-bot-api:
    extends:
        file: ../compose.base.yml
        service: base
    image: aiogram/telegram-bot-api:latest
    env_file: [ .env ]          # TELEGRAM_API_ID, TELEGRAM_API_HASH
    environment:
        TELEGRAM_LOCAL: 1
    volumes:
        - bot_api_data:/var/lib/telegram-bot-api
```

Every service in the production stack extends `../compose.base.yml`, so this one
does too rather than inventing its own restart policy and logging. The project
is pinned as `name: embedthat` and there are no explicit networks, so `bot` and
`worker` reach it as `http://telegram-bot-api:8081` on the default project
network.

- **No host port.** The server exposes a token-addressed HTTP API with no
  authentication of its own; anything that can reach it can act as the bot.
  Only `bot` and `worker` need it, and they reach it by service name.
- `TELEGRAM_LOCAL=1` is what raises the upload limit to 2000 MB and makes
  `getFile` return absolute local paths.
- The working directory must be a named volume: in local mode the file paths the
  server hands out have to stay valid across restarts.

**Two repositories change.** The production compose lives on latitude at
`/home/me/my/vps/homeserver/embedthat/compose.prod.yml`, i.e. in the `vps`
repo; this repo only carries the dev `compose.yml`. Each gets its own commit.

## Code changes

`Bot(...)` is constructed in **seven** places: five actors in
`bot/worker/actors.py` (`process_youtube_link`, `process_youtube_audio`,
`process_social_link`, `process_audio_page`, `install_cookies`), plus
`bot/util/telegram_log_handler.py::TelegramAlertHandler._send` and
`main.py::main`. The API server address is set per `Bot` instance, so a missed
site silently keeps talking to the cloud -- with a token that is logged out
there, which fails rather than degrades, but fails at an arbitrary later moment.

1. **One factory.** `bot/util/tg.py::make_bot(**kwargs) -> Bot` becomes the only
   place a `Bot` is built. It reads the new setting and, when it is set, passes
   `session=AiohttpSession(api=TelegramAPIServer.from_base(settings.bot_api_url, is_local=True))`.
   `main.py` keeps passing its `DefaultBotProperties`; the factory takes
   `**kwargs` so that stays a caller's concern.
   `TelegramAlertHandler` takes a token, not a `Bot`, so it calls the factory
   too -- its alerts are the one path that must keep working during migration.
2. **New setting.** `bot_api_url: str | None = None` in `bot/config.py::Settings`
   (`AliasChoices("bot_api_url")`). Empty means the cloud server, so local
   development and anyone else's checkout are unchanged by default.

   The production `.env` today holds `BOT_TOKEN`, `LOGLEVEL`, `REDIS_URL`, `TZ`,
   `ADMIN_CHAT_ID`, `DUMP_CHAT_ID` and `COOKIES_FILE`. This adds
   `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and `BOT_API_URL`; the same file is
   read by `bot`, `worker` and the new service, so the two credentials reach the
   server without being repeated anywhere.
3. **Size limit becomes a setting.** `MAX_FILE_SIZE_BYTES` in
   `bot/util/youtube/video.py` is a module constant used by both the YouTube and
   the social path. It becomes `settings.max_upload_size_bytes`, defaulting to
   the current 50 MB, set to 2000 MB where the local server is configured. The
   splitting code is untouched; it simply stops triggering.
4. **Stale `file_id` handling in the audio path.**
   `bot/util/audio/pager.py::redeliver_page` catches `TelegramBadRequest` only
   around `delete_message`, not around `audio.send_to_chat`. The social path
   already self-heals (`bot/handlers.py::_process_social_url` clears the cache
   entry and re-downloads on `TelegramBadRequest`); the audio path must do the
   same, or every cached audio page raises after the migration.

## Downloads in local mode

In local mode `getFile` returns an absolute path on the **server's** filesystem
instead of a URL, and aiogram follows suit: `Bot.download_file` branches on
`session.api.is_local` and reads the path off the local disk
(`aiogram/client/bot.py`, via `wrap_local_file.to_local`). Nothing is fetched
over HTTP.

`bot/worker/actors.py::_install_cookies_async` is the only code that downloads
anything -- it pulls an uploaded cookie jar with `get_file` + `download_file`.
It runs as a dramatiq actor, so it lives in the **worker** container, which
therefore has to see the server's files at the same path the server reports:

```yaml
worker:
    volumes:
        - ./cookies:/cookies
        - bot_api_data:/var/lib/telegram-bot-api:ro
```

Read-only, and only on `worker` -- the bot container never downloads. Mounting
at the identical path avoids aiogram's path-translation wrapper entirely. Get
this wrong and cookie installation breaks silently at the moment it is needed
most: the jar is uploaded when the old one has already expired.

## `file_id` and the cache

Assume every `file_id` cached before the move stops working, and verify rather
than trust it. The Bot API documentation does not state either way;
[tdlib/telegram-bot-api#359](https://github.com/tdlib/telegram-bot-api/issues/359)
reports cloud-issued ids failing against a local server.

With change 4 above, both cached paths degrade into a re-download instead of an
error, so no flush is strictly required. Flushing the `dl2:` keys is still the
cheaper option if the rehearsal shows ids are dead -- it turns a slow first
delivery per link into a single scripted `SCAN`+`DEL`. Decide after measuring.

## Disk

The server writes every uploaded and downloaded file under its working
directory and does not reliably clean up after itself
([#402](https://github.com/tdlib/telegram-bot-api/issues/402),
[#303](https://github.com/tdlib/telegram-bot-api/issues/303)). latitude has
240 GB free, so this is a maintenance item, not a blocker, but it is unbounded
growth and must be watched: after the rehearsal, measure what one video costs on
disk and add a cleanup (a periodic prune of the volume) sized from that number.

## Migration runbook

1. Rehearsal, dev stack, debug bot: bring the server up, point the dev bot at
   it, send a >50 MB video, and try a cloud-issued `file_id`. Record both
   answers.
2. Production: add the service and the two env vars, deploy, but leave
   `BOT_API_URL` unset -- the stack is unchanged and the server is idle.
3. Call `logOut` against `https://api.telegram.org` with the production token.
4. Set `BOT_API_URL`, recreate `bot` and `worker`.
5. Verify: `/stats`, one short link, one long link that used to be split, and one
   link that is in the cache from before the move.

**Rollback:** unset `BOT_API_URL` and recreate, then call `logOut` against the
local server. The bot cannot log back into the cloud for 10 minutes after step
3, so a rollback inside that window is a wait, not a failure. Nothing else is
destructive; the redis volume is untouched throughout.

## Verification

There is no test suite (`.claude/memory/project.md`). The checks are:

- **Import smoke test, all four doors** -- `main.py`, `bot.worker.actors`,
  `bot.events`, `bot.events.handlers.stats` -- because the factory touches the
  module that every door imports.
- **pyright and ruff against a recorded baseline**, comparing finding sets, not
  totals.
- **Rehearsal on the debug bot** as the real functional gate: one upload above
  50 MB, one cached `file_id` from before the switch, one `TelegramAlertHandler`
  alert (it is the path that must survive a broken migration).
- **Production**: a link that used to be split arrives as a single file.

## Open questions

- Which image: `aiogram/telegram-bot-api` (prebuilt, tracks upstream) versus
  building `tdlib/telegram-bot-api` ourselves. The prebuilt one is assumed here;
  it is a third-party build of a first-party source, which is worth a look before
  it holds the production token.
- Nothing outstanding on `getFile`; see "Downloads in local mode" above.
