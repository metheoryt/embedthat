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
  live result.
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
    image: aiogram/telegram-bot-api:latest
    environment:
        TELEGRAM_API_ID: ${TELEGRAM_API_ID}
        TELEGRAM_API_HASH: ${TELEGRAM_API_HASH}
        TELEGRAM_LOCAL: 1
    volumes:
        - bot_api_data:/var/lib/telegram-bot-api
    restart: unless-stopped
```

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
- Whether `getFile` returning absolute local paths breaks anything we do. We
  only ever upload and re-send by `file_id`; `install_cookies` is the one actor
  that downloads (`COOKIES_FILE`), and it must be checked against local mode.
