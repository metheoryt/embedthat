import itertools
import logging
from pathlib import Path
from typing import Any, cast

from bot.config import settings
from bot.util.ytdlp import extract_info

from .exc import AudioDownloadError
from .schema import AudioTrackData

log = logging.getLogger(__name__)


def _is_audio_only(info: dict[str, Any]) -> bool:
    entries = info.get("entries")
    if entries is not None:
        # A deep probe of an Instagram post comes back as a playlist wrapper, not
        # a media dict, and the wrapper has no formats of its own -- judged as-is
        # it reads as "no video track" and the whole carousel gets classified as
        # audio. Judge the first entry that actually has formats instead; a post
        # made only of stills has none, and is not an audio track either.
        first = next((e for e in entries if e and e.get("formats")), None)
        if first is None:
            return False
        info = first
    formats = info.get("formats") or [info]
    return not any(f.get("vcodec") not in (None, "none") for f in formats)


def _deep_probe(url: str) -> dict[str, Any]:
    opts: Any = {
        "quiet": True, "skip_download": True, "noplaylist": True,
        # `noplaylist` does not narrow an Instagram carousel -- the post URL *is*
        # the playlist -- so this probe still meets the still that has no formats.
        "ignore_no_formats_error": True,
    }
    info = extract_info(url, opts, AudioDownloadError)
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
        # An Instagram carousel containing a still has one entry with no formats.
        # Without this the whole probe dies on it -- and since this classification
        # runs before anything is downloaded, it took the social-video path down
        # with it: every carousel with a photo in it failed as "Couldn't process
        # this link", never reaching the downloader at all. Tolerated only inside
        # a playlist; a lone item with no formats is still an error below.
        "ignore_no_formats_error": True,
    }
    info = extract_info(url, opts, AudioDownloadError)

    if info is None:
        raise AudioDownloadError(f"Could not extract media from {url}")
    info = cast(dict[str, Any], info)

    if info.get("_type") == "playlist" or "entries" in info:
        # `ignore_no_formats_error` turns an unextractable entry into a None
        # rather than an exception, so the list can now have holes in it.
        entries = [e for e in itertools.islice(info["entries"], settings.max_playlist_tracks) if e]
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

    # Only a playlist earns the tolerance above: `_is_audio_only` reads a missing
    # format list as "no video track", so a lone item with none would be
    # misclassified as audio and fail later with a stranger message.
    if not info.get("formats"):
        raise AudioDownloadError(f"No media found at {url}")

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
    }
    info = extract_info(track.webpage_url, ydl_opts, AudioDownloadError, download=True)

    if info is None:
        raise AudioDownloadError(f"Could not download {track.title or track.webpage_url}")
    info = cast(dict[str, Any], info)

    track.title = track.title or info.get("title") or ""
    track.uploader = track.uploader or info.get("uploader") or ""
    track.duration = track.duration or (int(info["duration"]) if info.get("duration") else None)

    file_path = Path(info["requested_downloads"][0]["filepath"])
    limit = settings.max_upload_size_bytes
    if file_path.stat().st_size > limit:
        file_path.unlink(missing_ok=True)
        raise AudioDownloadError(
            f"{track.title or track.webpage_url} is too large to send (over {limit // 1_000_000} MB)"
        )

    log.info("downloaded track %s -> %s", track.webpage_url, file_path)
    return file_path
