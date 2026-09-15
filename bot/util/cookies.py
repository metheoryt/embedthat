"""Installing a browser-exported cookie jar over the live one.

The live jar is MULTI-SITE -- instagram, tiktok, vk, x.com, reddit, soundcloud,
threads -- while a browser exports one site at a time. Dropping an export on top
of the file therefore silently deletes every other site's session, which is why
this module merges by site instead of overwriting, and why the merge is here
rather than inline in the actor: it is the part worth getting right.
"""

import logging
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bot.config import settings

log = logging.getLogger(__name__)

# A jar is ~4 KB. The cap is there so a mis-sent document cannot be read into
# memory and parsed line by line, not because a legitimate jar ever approaches it.
MAX_JAR_BYTES = 1024 * 1024

_HEADER = "# Netscape HTTP Cookie File"
_HTTPONLY_PREFIX = "#HttpOnly_"


class CookieJarError(Exception):
    """The upload is not a usable jar, or the live one cannot be written."""


@dataclass(frozen=True)
class Cookie:
    domain: str
    name: str
    line: str

    @property
    def site(self) -> str:
        """The `example.com` a browser export is scoped to.

        Two labels is enough for every site this bot touches. A multi-label
        public suffix (`co.uk`) would group one label too much -- and the cost of
        being wrong that way is that an export replaces slightly more of the live
        jar than it strictly had to, never that it replaces less and leaves two
        conflicting sessions for one site behind.
        """
        labels = self.domain.lstrip(".").lower().split(".")
        return ".".join(labels[-2:])


def _parse(text: str) -> list[Cookie]:
    cookies: list[Cookie] = []
    for line in text.splitlines():
        stripped = line.strip()
        # `#HttpOnly_.instagram.com` is a cookie line wearing a comment's hat --
        # and it is the line `sessionid` arrives on, so skipping every `#` here
        # would throw away the one cookie that matters.
        if not stripped or (stripped.startswith("#") and not stripped.startswith(_HTTPONLY_PREFIX)):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        cookies.append(
            Cookie(domain=fields[0].removeprefix(_HTTPONLY_PREFIX), name=fields[5], line=line)
        )
    return cookies


def parse_export(text: str) -> list[Cookie]:
    """Parse an upload, refusing anything that is not a Netscape jar."""
    cookies = _parse(text)
    if not cookies:
        raise CookieJarError(
            "that file has no cookie lines in it -- expected a Netscape-format "
            "cookies.txt (7 tab-separated fields per line)"
        )
    return cookies


def check_export(cookies: list[Cookie]) -> None:
    """Refuse an Instagram export that carries no `sessionid`.

    `sessionid` is HttpOnly, so an exporter that reads `document.cookie` cannot
    see it and writes a jar that looks complete -- `csrftoken` and `ds_user_id`
    are both there. That jar authenticates nothing while still tying every
    request to the account, and the bot's success rate does not drop, because
    public reels need no session. It went unnoticed for a week in September 2026.
    A warning would repeat that; refusing is the point of the check.
    """
    instagram = [c for c in cookies if c.site == "instagram.com"]
    if instagram and not any(c.name == "sessionid" for c in instagram):
        raise CookieJarError(
            "this Instagram export has no `sessionid` -- it is HttpOnly, so the "
            "exporter could not see it and the jar would authenticate nothing. "
            "Re-export with an extension that includes HttpOnly cookies "
            "(Get cookies.txt LOCALLY)."
        )


def merge(current: str, incoming: list[Cookie]) -> str:
    """Replace the sites the export covers; keep every other site untouched."""
    replaced = {c.site for c in incoming}
    kept = [c for c in _parse(current) if c.site not in replaced]
    return "\n".join([_HEADER, *(c.line for c in [*kept, *incoming])]) + "\n"


def _inherit_owner(source: Path, target: Path) -> None:
    """Give `target` the uid/gid of `source`.

    The container runs as root and the mount is the host user's, so anything
    root creates in `/cookies` lands root-owned and he can no longer edit or
    back it up from the host without sudo. Writing the jar itself avoids this by
    truncating the existing inode (yt-dlp's own write-back does the same); a new
    backup, or a first-ever jar, has no inode to inherit from and needs this.
    """
    try:
        st = source.stat()
        os.chown(target, st.st_uid, st.st_gid)
    except (OSError, AttributeError):  # unprivileged, or not POSIX
        log.warning("could not set ownership on %s", target, exc_info=True)
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)


def install(text: str) -> str:
    """Merge an export into the live jar. Returns a report for the admin chat.

    Raises `CookieJarError` -- and leaves the live jar alone -- for anything
    that is not a usable export.
    """
    path = settings.cookies_file
    if path is None:
        raise CookieJarError("COOKIES_FILE is not set, so there is no jar to install into")
    if not path.parent.is_dir():
        raise CookieJarError(f"{path.parent} is not mounted in this container")
    # Re-checked here and not only at the upload: Telegram may omit `file_size`.
    if len(text.encode("utf-8")) > MAX_JAR_BYTES:
        raise CookieJarError(f"that file is larger than {MAX_JAR_BYTES // 1024} KB")

    incoming = parse_export(text)
    check_export(incoming)

    current = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    merged = merge(current, incoming)

    backup: Path | None = None
    if current:
        backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        backup.write_text(current, encoding="utf-8")
        _inherit_owner(path, backup)

    existed = path.exists()
    path.write_text(merged, encoding="utf-8")
    if not existed:
        _inherit_owner(path.parent, path)

    log.info("installed cookie jar: %d lines, backup %s", len(merged.splitlines()), backup)
    return _report(incoming, merged, backup)


def _report(incoming: list[Cookie], merged_text: str, backup: Path | None) -> str:
    """Names and counts only -- never a cookie value, this goes to a chat."""
    replaced = sorted({c.site for c in incoming})
    by_site: dict[str, list[str]] = {}
    for cookie in _parse(merged_text):
        by_site.setdefault(cookie.site, []).append(cookie.name)

    lines = [f"🍪 Installed. Replaced: {', '.join(replaced)}", "", "Jar now holds:"]
    for site in sorted(by_site):
        names = by_site[site]
        mark = " ✅ sessionid" if site == "instagram.com" and "sessionid" in names else ""
        lines.append(f"• {site} — {len(names)} cookie{'s' if len(names) != 1 else ''}{mark}")
    if backup:
        lines += ["", f"Previous jar: {backup.name}"]
    return "\n".join(lines)
