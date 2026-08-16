import itertools
import logging
from pathlib import Path
from typing import Any, cast

import yt_dlp
from yt_dlp.utils import DownloadError

from bot.config import settings
from bot.util.youtube.video import MAX_FILE_SIZE_BYTES
from bot.util.ytdlp import cookie_opts

from .exc import AudioDownloadError
from .schema import AudioTrackData

log = logging.getLogger(__name__)


def _is_audio_only(info: dict) -> bool:
    formats = info.get("formats") or [info]
    return not any(f.get("vcodec") not in (None, "none") for f in formats)


def _deep_probe(url: str) -> dict:
    opts: Any = {"quiet": True, "skip_download": True, "noplaylist": True, **cookie_opts()}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except DownloadError as e:
            raise AudioDownloadError(str(e)) from e
    if info is None:
        raise AudioDownloadError(f"Could not extract media from {url}")
    return cast(dict[str, Any], info)


def probe_link(url: str) -> tuple[bool, list[AudioTrackData]]:
    """
    Classifies `url` as audio-only or not, and builds its (capped) track index.

    Synchronous/blocking -- call via asyncio.to_thread.
    """
    opts: Any = {
        "quiet": True, "skip_download": True, "extract_flat": "in_playlist", "noplaylist": False,
        **cookie_opts(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except DownloadError as e:
            raise AudioDownloadError(str(e)) from e

    if info is None:
        raise AudioDownloadError(f"Could not extract media from {url}")
    info = cast(dict[str, Any], info)

    if info.get("_type") == "playlist" or "entries" in info:
        entries = list(itertools.islice(info["entries"], settings.max_playlist_tracks))
        if not entries:
            raise AudioDownloadError("Playlist has no tracks")

        first_url = entries[0].get("url") or entries[0].get("webpage_url")
        if not first_url:
            raise AudioDownloadError("First playlist entry has no URL")
        if not _is_audio_only(_deep_probe(first_url)):
            return False, []

        tracks = []
        for e in entries:
            webpage_url = e.get("url") or e.get("webpage_url")
            if not webpage_url or "id" not in e:
                log.warning("skipping malformed playlist entry (missing id/url): %r", e)
                continue
            tracks.append(
                AudioTrackData(
                    extractor=e.get("ie_key") or info.get("extractor_key") or "unknown",
                    id=str(e["id"]),
                    webpage_url=webpage_url,
                    title=e.get("title"),
                    uploader=e.get("uploader"),
                    duration=int(e["duration"]) if e.get("duration") else None,
                )
            )
        if not tracks:
            raise AudioDownloadError("Playlist has no usable tracks")

        log.info("classified %s as audio playlist, %d tracks", url, len(tracks))
        return True, tracks

    if not _is_audio_only(info):
        return False, []

    track = AudioTrackData(
        extractor=info.get("extractor_key") or "unknown",
        id=str(info["id"]),
        webpage_url=info.get("webpage_url") or url,
        title=info.get("title"),
        uploader=info.get("uploader"),
        duration=int(info["duration"]) if info.get("duration") else None,
    )
    log.info("classified %s as a single audio track", url)
    return True, [track]


def download_track(track: AudioTrackData, output_dir: Path) -> Path:
    """Synchronous/blocking -- call via asyncio.to_thread."""
    ydl_opts: Any = {
        "outtmpl": str(output_dir / f"{track.extractor}_{track.id}.%(ext)s"),
        "format": "bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        **cookie_opts(),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(track.webpage_url, download=True)
        except DownloadError as e:
            raise AudioDownloadError(str(e)) from e

    if info is None:
        raise AudioDownloadError(f"Could not download {track.title or track.webpage_url}")
    info = cast(dict[str, Any], info)

    track.title = track.title or info.get("title") or ""
    track.uploader = track.uploader or info.get("uploader") or ""
    track.duration = track.duration or (int(info["duration"]) if info.get("duration") else None)

    file_path = Path(info["requested_downloads"][0]["filepath"])
    if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
        file_path.unlink(missing_ok=True)
        raise AudioDownloadError(f"{track.title or track.webpage_url} is too large to send (over 50MB)")

    log.info("downloaded track %s -> %s", track.webpage_url, file_path)
    return file_path
