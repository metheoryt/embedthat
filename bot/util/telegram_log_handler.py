import asyncio
import html
import logging
import queue
import threading
import traceback

from aiogram.types import LinkPreviewOptions

from bot.config import settings
from bot.util.tg import make_bot

_MAX_MESSAGE_LEN = 4000  # keep under Telegram's 4096-char message cap with margin

# loggers whose records we never forward: the send path itself goes through
# aiogram/aiohttp, so forwarding those risks a send-triggers-send feedback loop
_EXCLUDED_LOGGERS = ("aiogram", "aiohttp", "asyncio")


class TelegramAlertHandler(logging.Handler):
    """Forwards log records at/above its level to a Telegram chat, one message
    per record -- never split across multiple messages.

    emit() only formats the record and queues it -- the actual send happens on a
    dedicated background thread with its own event loop, so callers (the bot's
    asyncio event loop, or a dramatiq worker thread) never block on network I/O.

    The header+message is plain text (so e.g. a link in the message
    auto-linkifies); a traceback -- from exc_info, or passed explicitly via
    `extra={"pre_text": "..."}` when there's no live exception object (e.g. a
    traceback string recovered from a dramatiq message) -- is appended as a
    monospace block only if it still fits in one message, and dropped entirely
    otherwise rather than shipping a second message for the overflow.
    """

    def __init__(self, token: str, chat_id: int, level: int = logging.CRITICAL) -> None:
        super().__init__(level)
        self._token = token
        self._chat_id = chat_id
        self._queue: queue.Queue[str] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="telegram-alert-handler", daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        if any(record.name == n or record.name.startswith(n + ".") for n in _EXCLUDED_LOGGERS):
            return
        try:
            header = f"🚨 <b>{html.escape(record.levelname)} — {html.escape(record.name)}</b>"
            text = f"{header}\n{html.escape(record.getMessage())}"

            tb_parts = []
            if record.exc_info:
                tb_parts.append("".join(traceback.format_exception(*record.exc_info)))
            if pre_text := getattr(record, "pre_text", None):
                tb_parts.append(pre_text)
            if tb_parts:
                tb_block = f"\n<pre>{html.escape(chr(10).join(tb_parts))}</pre>"
                if len(text) + len(tb_block) <= _MAX_MESSAGE_LEN:
                    text += tb_block
        except Exception:
            return
        self._queue.put_nowait(text[:_MAX_MESSAGE_LEN])

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            try:
                asyncio.run(self._send(text))
            except Exception:
                pass

    async def _send(self, text: str) -> None:
        bot = make_bot(self._token)
        try:
            await bot.send_message(
                self._chat_id,
                text,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        finally:
            await bot.session.close()


def install_admin_alert_handler() -> None:
    """Attach a CRITICAL-only Telegram handler to the root logger.

    CRITICAL (not WARNING/ERROR) because this codebase already uses those
    levels for routine, expected conditions (ack-message races, per-attempt
    retry logging, dramatiq's own per-attempt failure log) -- a lower
    threshold would flood the admin chat. CRITICAL is reserved for calls that
    mean "a human should look at this", e.g. bot.handlers.error_handler and
    bot.worker.error_reporting.report_actor_failure.
    """
    if not settings.admin_chat_id:
        return
    handler = TelegramAlertHandler(settings.bot_token, settings.admin_chat_id, level=logging.CRITICAL)
    logging.getLogger().addHandler(handler)
