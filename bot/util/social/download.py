import logging
import math
import shutil
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import ffmpeg
import yt_dlp

from bot.config import settings
from bot.util.ladder import fits, lowest_rung, rungs, scale_expression
from bot.util.ytdlp import extract_info

from .exc import SocialDownloadError

log = logging.getLogger(__name__)

_PHOTO_FETCH_TIMEOUT = 30

# Fallback only (no ready file): the merger always re-encodes, so the source size
# says nothing about the output. A per-rung bitrate ceiling does: duration x
# (ceiling + audio) bounds the file before a byte is downloaded.
_ENCODE_MAXRATE_KBPS = {1080: 6000, 720: 3000, 480: 1500}
_ENCODE_AUDIO_KBPS = 128
# Encoding time, not size, is what binds a merged download: measured 2026-09-27
# on latitude, x264 veryfast on real footage runs ~32 fps at 1080p and ~72 at
# 720p, under an actor limit of 45 min. The lowest rung has no ceiling.
_ENCODE_MAX_DURATION = {1080: 10 * 60, 720: 30 * 60}
# A single file is sent as downloaded, never re-encoded -- and HEVC does not
# play in every Telegram client (TikTok serves bytevc1 at 720p, 2026-09-27).
_NO_HEVC = "[vcodec!~='^(h265|hevc|hev1|hvc1|bytevc1)']"
# The options the fallback selector is compiled with: a merged pick's `ext`
# comes from the compiling instance's `merge_output_format`.
_SELECTOR_OPTS: dict[str, Any] = {"quiet": True, "merge_output_format": "mp4"}


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


def _format_spec(rung: int) -> str:
    """Strict: a rung the source does not reach selects nothing, so the caller
    moves down a rung. Only the lowest rung keeps the old loose tail -- at a
    higher one it would catch every source below the rung and send it
    un-re-encoded. Short side >= rung means both sides >= rung; every video
    format with a codec probed on Instagram, VK and TikTok carried a width
    (2026-09-27), so no `>=?`."""
    fit = f"[height>={rung}][width>={rung}]"
    spec = f"worstvideo[ext=mp4]{fit}+bestaudio[ext=m4a]/worst[ext=mp4]{fit}{_NO_HEVC}"
    if rung == lowest_rung():
        spec += "/best[ext=mp4]/best"
    return spec


def _download_spec(rung: int) -> str:
    """The fallback the download pass asks for: the chosen rung, then every rung
    below it, ending in the lowest rung's loose tail. The download is a second
    extraction (with the cookie jar, if the site walled the anonymous one) and may
    see other formats than the probe; the worst case is then a lower rung, never
    "Requested format is not available". Safe for size and time: a lower source
    is smaller than the chosen rung's bound, and the scale expression only
    shrinks."""
    return "/".join(_format_spec(r) for r in rungs() if r <= rung)


def _base_opts(fmt: str | Callable[[dict[str, Any]], Iterator[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "format": fmt,
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


def _probe_url(fmt: dict[str, Any]) -> dict[str, Any]:
    """ffprobe of a format's URL -- the header only, ~0.6 s. {} when it fails."""
    url = fmt.get("url")
    if not url:
        return {}
    http_headers = cast(dict[str, str], fmt.get("http_headers") or {})
    headers = "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())
    try:
        # kwargs become ffprobe flags; -timeout is ffprobe's HTTP timeout, in microseconds
        return cast(dict[str, Any], ffmpeg.probe(  # pyright: ignore[reportUnknownMemberType]
            url,
            v="error",
            show_entries="stream=codec_type,codec_name,width,height,pix_fmt:format=duration,size",
            headers=headers,
            timeout=30_000_000,
        ))
    except Exception as e:
        log.warning("ffprobe of format %s failed: %s", fmt.get("format_id"), e)
        return {}


def _url_duration(fmt: dict[str, Any]) -> int:
    """Instagram gives no duration in the probe, for carousel items and single
    reels alike (2026-09-27). 0 when unknown."""
    try:
        return int(float(_probe_url(fmt)["format"]["duration"]))
    except (KeyError, TypeError, ValueError):
        return 0


def _ready_info(fmt: dict[str, Any]) -> tuple[int, int | None] | None:
    """(short side, size) if `fmt` is a ready file -- mp4, h264, 4:2:0, with sound,
    playable everywhere as downloaded -- else None. yt-dlp's own metadata is used
    when complete (TikTok); otherwise the URL is probed (Instagram's `1/2/3`)."""
    if fmt.get("ext") != "mp4" or "none" in (fmt.get("vcodec"), fmt.get("acodec")):
        return None
    vcodec, width, height = fmt.get("vcodec"), fmt.get("width"), fmt.get("height")
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    pix_fmt = None
    if not (vcodec and width and height and fmt.get("acodec")):
        probe = _probe_url(fmt)
        streams = cast(list[dict[str, Any]], probe.get("streams") or [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None or not any(s.get("codec_type") == "audio" for s in streams):
            return None
        vcodec, width, height = video.get("codec_name"), video.get("width"), video.get("height")
        pix_fmt = video.get("pix_fmt")
        container = cast(dict[str, Any], probe.get("format") or {})
        size = size or int(container.get("size") or 0) or None
    if not str(vcodec).startswith(("avc1", "h264")) or pix_fmt not in (None, "yuv420p"):
        return None
    if not (width and height):
        return None
    return min(width, height), size


def _rung_of(short: int) -> int:
    """The rung a ready file of this short side is judged at."""
    return next((r for r in rungs() if short >= r), lowest_rung())


def _ready_file(formats: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The ready file to send, by the order in the spec. Walked best-first (yt-dlp
    lists formats worst first), so among equal short sides the best-ranked one
    wins -- a bitrate bump over the old `worst` for TikTok -- and the walk stops at
    the first ready file that fits, which usually means one ffprobe."""
    top, lowest = rungs()[0], lowest_rung()
    reaching: list[tuple[int, dict[str, Any]]] = []
    below: list[tuple[int, dict[str, Any]]] = []
    for fmt in reversed(formats):
        info = _ready_info(fmt)
        if info is None:
            continue
        short, size = info
        if short > top:
            continue  # would need a downscale, i.e. a re-encode
        if short < lowest:
            below.append((short, fmt))
            continue
        if fits(_rung_of(short), size):
            return fmt
        reaching.append((short, fmt))
    if reaching:
        # None fits its cap: the lightest of them as is, split after download if
        # it is over the upload limit -- still no re-encode.
        return min(reaching, key=lambda item: item[0])[1]
    if below:
        return max(below, key=lambda item: item[0])[1]
    return None


def _selector_ydl() -> yt_dlp.YoutubeDL:
    """Compiles format selectors only -- no network."""
    return yt_dlp.YoutubeDL(cast(Any, _SELECTOR_OPTS))


def _select_format(ydl: yt_dlp.YoutubeDL, entry: dict[str, Any], rung: int) -> dict[str, Any] | None:
    """What yt-dlp would download for `entry` at `rung`, from the probe's info --
    no second extraction. The ctx mirrors YoutubeDL.process_video_result."""
    formats = cast(list[dict[str, Any]], entry.get("formats") or [])
    ctx: dict[str, Any] = {
        "formats": formats,
        "has_merged_format": any("none" not in (f.get("acodec"), f.get("vcodec")) for f in formats),
        "incomplete_formats": all(f.get("vcodec") == "none" for f in formats)
        or all(f.get("acodec") == "none" for f in formats),
    }
    return next(iter(ydl.build_format_selector(_format_spec(rung))(ctx)), None)


def _entry_rung(ydl: yt_dlp.YoutubeDL, entry: dict[str, Any]) -> int:
    """The highest rung this entry reaches and fits in one file at its own cap,
    for an entry with no ready file."""
    duration = int(entry.get("duration") or 0)
    for rung in rungs():
        fmt = _select_format(ydl, entry, rung)
        if fmt is None:
            continue  # the source does not reach this rung
        if fmt.get("requested_formats"):  # merged, so re-encoded
            if not duration:
                duration = _url_duration(fmt["requested_formats"][0])
            ceiling = _ENCODE_MAX_DURATION.get(rung, math.inf)
            # Unknown length: only a rung without a ceiling -- encoding time is
            # the one limit that kills the job instead of splitting it.
            if duration > ceiling or (not duration and ceiling != math.inf):
                continue
            kbps = _ENCODE_MAXRATE_KBPS[rung] + _ENCODE_AUDIO_KBPS
            estimate = int(duration * kbps * 1000 / 8) if duration else None
        else:  # one file, sent as downloaded
            estimate = fmt.get("filesize") or fmt.get("filesize_approx")
        if fits(rung, estimate):
            return rung
    return lowest_rung()


def _selector(rung: int) -> Callable[[dict[str, Any]], Iterator[dict[str, Any]]]:
    """The download pass's format selector (yt-dlp accepts a callable `format`):
    per entry, a ready file if there is one, else the chained merged ladder from
    `rung` down. Deciding per entry inside the download pass itself is what keeps
    the probe and the download from disagreeing about ready files."""
    fallback = _selector_ydl().build_format_selector(_download_spec(rung))

    def select(ctx: dict[str, Any]) -> Iterator[dict[str, Any]]:
        ready = _ready_file(ctx.get("formats") or [])
        if ready is not None:
            yield ready
            return
        yield from fallback(ctx)

    return select


def _download_positions(url: str, positions: list[int], output_dir: Path, rung: int) -> dict[str, Path]:
    """Downloads the given 1-based playlist positions, returning entry id -> file.

    `ignoreerrors="only_download"` is what makes a carousel survive one bad item:
    a position whose media 403s is skipped instead of aborting the post. Scoped to
    downloads on purpose -- an *extraction* error still raises, so a login wall
    reaches `extract_info`'s cookie retry rather than being silently swallowed.

    Nothing is inferred from what comes back: files are matched to entries by id,
    so a missing position stays missing instead of being filled by its neighbour.
    """
    opts = _base_opts(_selector(rung))
    opts["outtmpl"] = str(output_dir / "%(id)s.%(ext)s")
    opts["playlist_items"] = ",".join(str(pos) for pos in positions)
    opts["ignoreerrors"] = "only_download"
    # Only a merged download (no ready file) reaches the merger. Re-encode to
    # H.264/AAC with iOS-compatible settings:
    # - yuv420p: iOS requires 8-bit 4:2:0 chroma
    # - faststart: moves moov atom to front so iOS can start playback immediately
    # - profile main: avoids B-frame issues on some decoders
    # - scale: this merger step is already a mandatory re-encode, so the short-side
    #   cap is free
    # - veryfast + maxrate: ~2x the speed of the default preset and a size known
    #   before download (a smaller file than `medium` under the same ceiling,
    #   measured 2026-09-27); audio pinned so it enters the estimate as a constant
    maxrate = _ENCODE_MAXRATE_KBPS[rung]
    opts["postprocessor_args"] = {
        "merger": [
            "-vcodec", "libx264",
            "-preset", "veryfast",
            "-profile:v", "main",
            "-pix_fmt", "yuv420p",
            "-maxrate", f"{maxrate}k",
            "-bufsize", f"{2 * maxrate}k",
            "-vf", scale_expression(rung),
            "-acodec", "aac",
            "-b:a", f"{_ENCODE_AUDIO_KBPS}k",
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


def download_social_video(url: str, output_dir: Path) -> DownloadResult:
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
    # A plain string spec for the probe: its own pick is never used (the decision
    # below reads the formats), and a callable here would ffprobe every entry twice.
    probe_opts = _base_opts(_download_spec(lowest_rung()))
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
        # One yt-dlp call downloads every position with one set of merger options,
        # so the post gets one merger rung: the lowest any entry WITHOUT a ready
        # file needs. Ready files ignore it -- nothing re-encodes them.
        with _selector_ydl() as ydl:
            merged_rungs = [
                _entry_rung(ydl, entries[pos])
                for pos in video_positions
                if _ready_file(entries[pos].get("formats") or []) is None
            ]
        rung = min(merged_rungs, default=rungs()[0])
        log.info("%s: %d ready, %d merged at %dp",
                 url, len(video_positions) - len(merged_rungs), len(merged_rungs), rung)
        downloaded = _download_positions(url, video_positions, output_dir, rung)
        retry = [pos for pos in video_positions if (entries[pos].get("id") or "") not in downloaded]
        if retry:
            # One narrow second attempt, for the failed positions only. A 403 on a
            # single item is common enough on Instagram to be worth re-asking for,
            # and asking for just those costs a fraction of redoing the post --
            # which is what raising here would make dramatiq do.
            log.warning("retrying %d failed position(s) of %s: %s", len(retry), url, retry)
            downloaded.update(_download_positions(url, retry, output_dir, rung))

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
