import logging
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import ffmpeg

from bot.config import settings
from bot.util.ytdlp import extract_info

from .exc import SocialDownloadError

log = logging.getLogger(__name__)

_PHOTO_FETCH_TIMEOUT = 30


def _probe_dimensions(file_path: Path) -> tuple[int, int]:
    try:
        probe = ffmpeg.probe(str(file_path), select_streams="v:0", show_entries="stream=width,height")
        stream = probe["streams"][0]
        return stream["width"], stream["height"]
    except Exception as e:
        log.warning("ffprobe failed for %s: %s", file_path, e)
        return 0, 0


def _probe_duration(file_path: Path) -> int:
    try:
        probe = ffmpeg.probe(str(file_path))
        return int(float(probe["format"].get("duration", 0)))
    except Exception as e:
        log.warning("ffprobe duration failed for %s: %s", file_path, e)
        return 0


@dataclass
class MediaFile:
    file_path: Path
    kind: Literal["video", "photo"]
    width: int
    height: int
    duration: int  # seconds; always 0 for a photo


@dataclass
class DownloadResult:
    files: list[MediaFile]
    video_id: str  # the post's own id, not a carousel item's
    title: str
    extractor: str  # yt-dlp extractor key, e.g. "TikTok", "Instagram", "Twitter"
    missing: list[int]  # carousel positions that never produced a file
    total: int  # items the post claims to have, including the missing ones


# The only query parameter that changes what we deliver. Everything else
# Instagram appends is per-share noise.
_MEANINGFUL_QUERY = frozenset({"img_index"})


def normalize_social_url(url: str) -> str:
    """Strips per-share junk so the same post hashes to the same cache key.

    Instagram's own share button appends `stkn=<random>` -- a fresh token every
    time, e.g. `?img_index=9&stkn=MW91eTg3d2hyNHdicQ==`. `cache_key` hashes the
    whole link, so two people sharing one post produced two different keys: the
    cache never hit and every share re-downloaded the post from scratch. Verified
    that the token means nothing to the extractor -- with it and without it,
    position 9 resolves to the same item.

    Scoped to Instagram deliberately: other extractors do carry meaning in the
    query string, and stripping it blindly would break them.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host != "instagram.com" and not host.endswith(".instagram.com"):
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query) if k in _MEANINGFUL_QUERY]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def carousel_index(url: str) -> int | None:
    """The 1-based carousel position named by `?img_index=N`, or None.

    yt-dlp ignores the parameter itself -- measured on 2026.07.04 and on the
    pinned 2026.08.19, both of which return the full 13-entry playlist for
    `?img_index=3` and for `?img_index=13` alike -- so translating it into
    `playlist_items` has to happen here.
    """
    raw = parse_qs(urlparse(url).query).get("img_index", [None])[0]
    if raw is None:
        return None
    try:
        position = int(raw)
    except ValueError:
        log.warning("ignoring non-numeric img_index=%r in %s", raw, url)
        return None
    return position if position >= 1 else None


def _photo_headers() -> dict[str, str]:
    if settings.cookies_user_agent:
        return {"User-Agent": settings.cookies_user_agent}
    return {}


def _download_photo(entry: dict[str, Any], output_dir: Path) -> Path | None:
    """Fetches a carousel still, which yt-dlp cannot download as media.

    A photo entry resolves to zero formats -- the extractor raises "No video
    formats found!" and the item is dropped -- so the picture only ever reaches
    us as the entry's thumbnail list. Instagram publishes no width, height or
    preference on any of those, so ordering is the only handle on "the full-size
    one"; yt-dlp sorts thumbnails worst-to-best, making the last the original.
    Real dimensions come from the bytes afterwards, which also guards against
    the extractor reordering the list.
    """
    thumbnails = [t for t in (entry.get("thumbnails") or []) if t.get("url")]
    if not thumbnails:
        return None
    url = thumbnails[-1]["url"]
    # The URL is usually named .heic while the CDN serves JPEG; trust the bytes.
    destination = output_dir / f"{entry.get('id') or 'photo'}.jpg"
    request = urllib.request.Request(url, headers=_photo_headers())  # noqa: S310 -- https CDN URL from yt-dlp
    try:
        with urllib.request.urlopen(request, timeout=_PHOTO_FETCH_TIMEOUT) as response:  # noqa: S310
            with destination.open("wb") as fh:
                shutil.copyfileobj(response, fh)
    except Exception as e:
        log.warning("failed to fetch carousel photo %s: %r", url, e)
        return None
    return destination


def _downloaded_path(entry: dict[str, Any], output_dir: Path) -> Path | None:
    for download in entry.get("requested_downloads") or []:
        filepath = download.get("filepath")
        if filepath:
            return Path(filepath)
    entry_id = entry.get("id")
    if entry_id:
        candidate = output_dir / f"{entry_id}.mp4"
        if candidate.exists():
            return candidate
    return None


def _media_file(
    entry: dict[str, Any],
    file_path: Path | None,
    output_dir: Path,
    *,
    is_video: bool,
) -> MediaFile | None:
    """Turns one enumerated entry into a sendable file, or None if it failed.

    `is_video` comes from the metadata pass, never from whether a file happens to
    be on disk. Inferring it the other way round substitutes a video's own poster
    frame for the video whenever its download fails -- measured: forcing one item
    of a 13-item post to fail produced 11 videos + 2 photos and reported nothing
    missing, instead of 12 items and a gap at position 5.
    """
    if is_video:
        if file_path is None or not file_path.exists():
            return None
        width = entry.get("width") or 0
        height = entry.get("height") or 0
        if not width or not height:
            width, height = _probe_dimensions(file_path)
        duration = int(entry.get("duration") or 0) or _probe_duration(file_path)
        return MediaFile(file_path=file_path, kind="video", width=width, height=height, duration=duration)

    photo_path = _download_photo(entry, output_dir)
    if photo_path is None:
        log.warning("carousel entry %s yielded neither video nor photo", entry.get("id"))
        return None
    width, height = _probe_dimensions(photo_path)
    return MediaFile(file_path=photo_path, kind="photo", width=width, height=height, duration=0)


def _base_opts(max_res: int) -> dict[str, Any]:
    return {
        "format": (
            f"worstvideo[ext=mp4][height>={max_res}]+bestaudio[ext=m4a]/"
            f"worst[ext=mp4][height>={max_res}]/best[ext=mp4]/best"
        ),
        "merge_output_format": "mp4",
        "quiet": True,
        # `noplaylist` is deliberately absent. A carousel URL *is* the playlist, so
        # the flag never narrowed anything: with it set, all 13 entries of a 13-item
        # post still came back, 12 were downloaded and 11 thrown away.
        # `playlist_items` is the option that actually selects.
        "ignore_no_formats_error": True,
    }


def _entries_of(info: dict[str, Any]) -> list[dict[str, Any] | None]:
    if info.get("_type") == "playlist":
        return list(info.get("entries") or [])
    return [info]


def _download_positions(url: str, positions: list[int], output_dir: Path, max_res: int) -> dict[str, Path]:
    """Downloads the given 1-based playlist positions, returning entry id -> file.

    `ignoreerrors="only_download"` is what makes a carousel survive one bad item:
    a position whose media 403s is skipped instead of aborting the post. Scoped to
    downloads on purpose -- an *extraction* error still raises, so a login wall
    reaches `extract_info`'s cookie retry rather than being silently swallowed.

    Nothing is inferred from what comes back: files are matched to entries by id,
    so a missing position stays missing instead of being filled by its neighbour.
    """
    opts = _base_opts(max_res)
    opts["outtmpl"] = str(output_dir / "%(id)s.%(ext)s")
    opts["playlist_items"] = ",".join(str(pos) for pos in positions)
    opts["ignoreerrors"] = "only_download"
    # Re-encode to H.264/AAC with iOS-compatible settings:
    # - yuv420p: iOS requires 8-bit 4:2:0 chroma
    # - faststart: moves moov atom to front so iOS can start playback immediately
    # - profile main: avoids B-frame issues on some decoders
    # - scale: this merger step is already a mandatory re-encode, so capping height here is free
    opts["postprocessor_args"] = {
        "merger": [
            "-vcodec", "libx264",
            "-profile:v", "main",
            "-pix_fmt", "yuv420p",
            "-vf", f"scale=-2:'min({max_res},ih)'",
            "-acodec", "aac",
            "-movflags", "+faststart",
        ],
    }
    done = extract_info(url, opts, SocialDownloadError, download=True)
    if done is None:
        return {}

    found: dict[str, Path] = {}
    for entry in _entries_of(done):
        if entry is None:
            continue
        path = _downloaded_path(entry, output_dir)
        if path is not None and path.exists():
            found[entry["id"]] = path
    return found


def download_social_video(url: str, output_dir: Path, max_res: int = settings.max_video_resolution) -> DownloadResult:
    """
    Synchronous yt-dlp download. Call via asyncio.to_thread in the handler.

    Returns every item of the post, in carousel order, unless the URL carries an
    `img_index` -- then just that one.

    Two passes, deliberately. A carousel still resolves to zero formats, and
    `ignore_no_formats_error` only survives the metadata stage: during an actual
    download the extractor's "No video formats found!" aborts the whole post,
    taking the twelve healthy videos beside it with it. So the first pass
    enumerates and classifies without downloading, and the second asks only for
    the positions that really are video. The alternative -- `ignoreerrors` over a
    single download pass -- would also swallow genuine failures, which is exactly
    where a wrong item silently substitutes for a missing one.

    Raises SocialDownloadError for unrecoverable failures (private/removed/geo-blocked),
    TransientDownloadError for the ones worth another attempt (429/5xx/timeouts).
    """
    index = carousel_index(url)
    probe_opts = _base_opts(max_res)
    if index is not None:
        probe_opts["playlist_items"] = str(index)

    info = extract_info(url, probe_opts, SocialDownloadError, download=False)
    if info is None:
        raise SocialDownloadError(f"Could not extract media from {url}")

    # Positions are 1-based and match the carousel: a 12-video + 1-photo post
    # enumerates as 13 entries with the still in its real place, so `img_index`
    # maps straight onto `playlist_items` with no off-by-one.
    first = index or 1
    entries = {first + offset: entry for offset, entry in enumerate(_entries_of(info)) if entry is not None}
    # The one place "is this a video?" is decided, so the download path and the
    # missing-item accounting can never disagree about it.
    is_video = {pos: bool(entry.get("formats")) for pos, entry in entries.items()}
    video_positions = [pos for pos in sorted(entries) if is_video[pos]]

    downloaded: dict[str, Path] = {}
    if video_positions:
        downloaded = _download_positions(url, video_positions, output_dir, max_res)
        retry = [pos for pos in video_positions if (entries[pos].get("id") or "") not in downloaded]
        if retry:
            # One narrow second attempt, for the failed positions only. A 403 on a
            # single item is common enough on Instagram to be worth re-asking for,
            # and asking for just those costs a fraction of redoing the post --
            # which is what raising here would make dramatiq do.
            log.warning("retrying %d failed position(s) of %s: %s", len(retry), url, retry)
            downloaded.update(_download_positions(url, retry, output_dir, max_res))

    files: list[MediaFile] = []
    missing: list[int] = []
    for pos in sorted(entries):
        entry = entries[pos]
        media = _media_file(entry, downloaded.get(entry.get("id") or ""), output_dir, is_video=is_video[pos])
        if media is None:
            log.warning("carousel position %d of %s yielded no media", pos, url)
            missing.append(pos)
            continue
        files.append(media)

    # Partial is a result; empty is a failure. Raising only when nothing at all
    # came through is what keeps one bad item from costing the other twelve.
    if not files:
        raise SocialDownloadError(f"No downloadable media found in {url}")

    dr = DownloadResult(
        files=files,
        video_id=info["id"],
        title=info.get("title") or "",
        extractor=info.get("extractor_key") or "unknown",
        missing=missing,
        total=len(entries),
    )
    log.info(
        "downloaded %d/%d item(s) (%d photo) for %s",
        len(files),
        len(entries),
        sum(1 for f in files if f.kind == "photo"),
        url,
    )
    return dr
