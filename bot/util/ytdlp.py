"""Shared yt-dlp wiring.

Exists so the cookie jar is configured in ONE place: yt-dlp is constructed at
four separate call sites (`probe_link`, `_deep_probe`, `download_track`,
`download_social_video`) whose other options legitimately differ, and a cookie
file that only reaches three of them would fail in a way that looks random.

Cookies are opt-in. With no `COOKIES_FILE` set -- or with the file missing --
`cookie_opts()` returns `{}` and every call site behaves exactly as it did
before this module existed.
"""

import logging
from typing import Any

from bot.config import settings

log = logging.getLogger(__name__)

# yt-dlp error fragments meaning "this needs a logged-in session", as opposed to
# "this post is gone / has no video in it". Matched case-insensitively against
# the DownloadError text.
#
# Deliberately narrow: the only consumer is the stale-cookie admin alert, whose
# entire value is that it is rare. A marker broad enough to also match ordinary
# private/removed posts would turn the alert into noise and get it muted.
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
    return {"cookiefile": str(path)}


def is_login_wall(error: object) -> bool:
    """True if `error` reads like a site demanding a logged-in session."""
    text = str(error).lower()
    return any(marker in text for marker in _LOGIN_WALL_MARKERS)
