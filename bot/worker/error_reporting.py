import logging
from typing import Any, TypedDict

import dramatiq

from bot.worker.broker import (
    broker,  # noqa: F401 -- registers the Redis broker before the actor is declared
)

log = logging.getLogger(__name__)

_TRACEBACK_LIMIT = 3000  # keep well under Telegram's 4096-char message cap

# the link (or, for process_audio_page, the link's cache hash) is always the second
# positional arg for these actors: process_youtube_link(chat_id, link, target_lang),
# process_social_link(chat_id, url), process_audio_page(chat_id, hash16, page)
_LINK_ARG_ACTORS = ("process_youtube_link", "process_social_link", "process_audio_page")

# Exception class names that mean "transient infra hiccup", not "a human should
# fix something": Telegram/aiohttp/redis network + timeout failures. When a
# permanent failure bottoms out in one of these there is nothing actionable to
# report, so we log it at ERROR (below the admin alert handler's CRITICAL
# threshold) instead of forwarding it to the admin chat. Matched as substrings
# so dotted paths (asyncio.TimeoutError, redis.exceptions.ConnectionError, ...)
# and aiogram's Telegram* wrappers are all covered.
_TRANSIENT_EXC_NAMES = (
    "TelegramNetworkError",
    "TelegramRetryAfter",
    "TimeoutError",
    "ServerDisconnectedError",
    "ClientConnectorError",
    "ClientOSError",
    "ClientPayloadError",
    "ClientConnectionError",
    "ConnectionResetError",
    "ConnectionError",
)


class RetryInfo(TypedDict):
    retries: int
    max_retries: int | None


def _extract_link(message_data: dict[str, Any]) -> str | None:
    args: list[Any] = message_data.get("args") or []
    if message_data.get("actor_name") in _LINK_ARG_ACTORS and len(args) >= 2:
        return args[1]
    return None


def _is_transient_failure(traceback_text: str) -> bool:
    """True if the exception that actually propagated out of the actor -- the
    last non-empty line of the formatted traceback -- is a known transient
    network/timeout error rather than a genuine bug."""
    for line in reversed(traceback_text.splitlines()):
        line = line.strip()
        if not line:
            continue
        return any(name in line for name in _TRANSIENT_EXC_NAMES)
    return False


@dramatiq.actor(max_retries=0)
def report_actor_failure(message_data: dict[str, Any], retry_info: RetryInfo) -> None:
    """Registered as `on_retry_exhausted` on worker actors; fires once, when
    dramatiq's Retries middleware gives up on a message for good. Actors whose
    `throws` option matches the exception are excluded upstream by Retries
    itself, so this only sees genuine bugs/timeouts, not routine user-facing
    failures (those are already reported to the user via `_notify_waiters_failure`).

    Reported via log.critical rather than sending to Telegram directly -- the
    root logger's TelegramAlertHandler (see bot.util.telegram_log_handler)
    picks up CRITICAL records and forwards them to the admin chat.
    """
    options: dict[str, Any] = message_data.get("options") or {}
    traceback_text: str = options.get("traceback", "")

    if _is_transient_failure(traceback_text):
        # Not forwarded to the admin chat (ERROR < CRITICAL): a network/timeout
        # blip the retries couldn't outlast, nothing to act on.
        log.error(
            "%s gave up after %s retries on a transient network error (not alerted)\nlink: %s",
            message_data.get("actor_name"),
            retry_info.get("retries"),
            _extract_link(message_data) or "n/a",
        )
        return

    log.critical(
        "%s failed permanently after %s retries\nlink: %s\nargs=%s kwargs=%s",
        message_data.get("actor_name"),
        retry_info.get("retries"),
        _extract_link(message_data) or "n/a",
        message_data.get("args"),
        message_data.get("kwargs"),
        extra={"pre_text": traceback_text[-_TRACEBACK_LIMIT:]},
    )
