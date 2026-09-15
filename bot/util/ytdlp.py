"""Shared yt-dlp wiring.

Exists so the cookie jar is configured in ONE place: yt-dlp is constructed at
four separate call sites (`probe_link`, `_deep_probe`, `download_track`,
`download_social_video`) whose other options legitimately differ, and a cookie
file that only reaches three of them would fail in a way that looks random.
All four now go through `extract_info()` here, which is what keeps that true.

Cookies are opt-in AND deferred. With no `COOKIES_FILE` set -- or with the file
missing -- `cookie_opts()` returns `{}`; with one set, the jar is still spent
only on the requests that actually demand a login. See `extract_info`.
"""

import logging
from typing import Any, cast

import yt_dlp
from yt_dlp.utils import DownloadError

from bot.config import settings

log = logging.getLogger(__name__)

# yt-dlp error fragments meaning "this needs a logged-in session", as opposed to
# "this post is gone / has no video in it". Matched case-insensitively against
# the DownloadError text.
#
# Since `extract_info` retries behind the jar, these markers now do double duty:
# they pick the requests worth spending the session on, not just the ones worth
# alerting about. Still deliberately narrow -- a marker broad enough to also
# match ordinary private/removed posts would both flood the admin alert and burn
# an authenticated retry on every deleted link someone pastes.
_LOGIN_WALL_MARKERS = (
    "log in for access",
    "empty media response",
    "login required",
    "requires authentication",
    "use --cookies",
)


def cookie_opts() -> dict[str, Any]:
    """yt-dlp options carrying the cookie jar, or `{}` when none is configured.

    Checked per call rather than cached at import: dropping a freshly exported
    cookies.txt onto the host then takes effect on the next link, with no
    container restart.

    Note that yt-dlp writes the jar back on close, so the file must be mounted
    read-write -- that write-back is what keeps the session alive as the site
    rotates its cookies, instead of it expiring on the exporter's schedule.
    """
    path = settings.cookies_file
    if path is None:
        return {}
    if not path.exists():
        log.warning("COOKIES_FILE is set to %s, but no such file; continuing without cookies", path)
        return {}
    opts: dict[str, Any] = {"cookiefile": str(path)}
    if settings.cookies_user_agent:
        # The jar and the User-Agent are one identity: a session cookie exported
        # from the operator's browser but replayed under yt-dlp's built-in UA
        # (Windows Chrome, whatever the exporter actually ran) is a mismatch the
        # site can see. Unset leaves yt-dlp's default, i.e. the old behaviour.
        opts["http_headers"] = {"User-Agent": settings.cookies_user_agent}
    return opts


def extract_info(
    url: str,
    opts: dict[str, Any],
    permanent: type[Exception],
    *,
    download: bool = False,
) -> dict[str, Any] | None:
    """Runs yt-dlp's `extract_info`, reaching for the cookie jar only if the site
    demands a login. The single entry point for all four call sites.

    Anonymous first, authenticated on retry. Every call used to carry the jar, so
    the burner account behind it signed for traffic that never needed a session
    -- public reels, TikToks, both probes -- and Instagram flagged the account
    for automated activity (2026-09-15). Most links need no login at all, so the
    jar is now spent only where it buys something.

    Two costs, both accepted. One extra request per walled post: the anonymous
    attempt dies at extraction, before any bytes, and only then do we re-ask with
    cookies. And the jar is refreshed less often -- yt-dlp writes it back on
    close, so walled posts alone now keep the session alive where before every
    request did. A jar that goes stale anyway still surfaces loudly, through
    `_alert_if_cookies_stale`.

    Raises the caller's own `permanent` class or `TransientDownloadError`, per
    `wrap_download_error`. A login wall that survives the retry is permanent: the
    jar had its chance.
    """
    jar = cookie_opts()
    try:
        return _extract(url, opts, download)
    except DownloadError as e:
        if not jar or not is_login_wall(e):
            raise wrap_download_error(e, permanent) from e
        log.info("login wall on %s, retrying with the cookie jar", url)

    try:
        return _extract(url, {**opts, **jar}, download)
    except DownloadError as e:
        raise wrap_download_error(e, permanent) from e


def _extract(url: str, opts: Any, download: bool) -> dict[str, Any] | None:
    # `opts: Any`, not `dict[str, Any]`: yt-dlp types its constructor against a
    # `_Params` TypedDict that our option dicts do not satisfy structurally. The
    # call sites carried the same annotation for the same reason.
    with yt_dlp.YoutubeDL(opts) as ydl:
        return cast(dict[str, Any] | None, ydl.extract_info(url, download=download))


def is_login_wall(error: object) -> bool:
    """True if `error` reads like a site demanding a logged-in session."""
    text = str(error).lower()
    return any(marker in text for marker in _LOGIN_WALL_MARKERS)


# yt-dlp error fragments meaning "the site did not answer us properly *this
# time*", as opposed to "this post is private/removed/geo-blocked". Matched
# case-insensitively against the DownloadError text.
#
# Deliberately excludes "unexpected response from webpage request": that one is
# reproducible (see c64d1bf -- 5/5 fetches from the bookworm image), so retrying
# it just buys the user two minutes of silence before the same failure.
#
# 403 is the judgement call. A permanently blocked link also 403s, and pays the
# full retry budget before its error surfaces; a WAF that 403s one request out
# of several is common enough on TikTok/Instagram to be worth that cost.
_TRANSIENT_MARKERS = (
    "http error 403",
    "http error 429",
    "http error 5",  # 500/502/503/504 -- the site's own edge failing
    "too many requests",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection refused",
    "remote end closed connection",
    "temporary failure in name resolution",
    "name or service not known",
)


class TransientDownloadError(Exception):
    """A yt-dlp failure worth retrying later.

    Deliberately NOT a subclass of `AudioDownloadError` / `SocialDownloadError`:
    dramatiq matches an actor's `throws=` tuple by `isinstance`, so inheriting
    from either would restore the very no-retry behaviour this class exists to
    avoid.
    """


def is_transient(error: object) -> bool:
    """True if `error` reads like a hiccup rather than a verdict.

    A login wall is never transient however it is phrased -- retrying it wastes
    the budget and delays the admin alert that a human has to act on.
    """
    if is_login_wall(error):
        return False
    text = str(error).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def wrap_download_error(error: Exception, permanent: type[Exception]) -> Exception:
    """Classifies a yt-dlp `DownloadError` into the exception to raise: the
    caller's own permanent, user-facing class, or `TransientDownloadError`."""
    if is_transient(error):
        return TransientDownloadError(str(error))
    return permanent(str(error))
