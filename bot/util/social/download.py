import logging
from dataclasses import dataclass
from pathlib import Path

import ffmpeg

from bot.config import settings
from bot.util.ytdlp import extract_info

from .exc import SocialDownloadError

log = logging.getLogger(__name__)


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
class DownloadResult:
    file_path: Path
    video_id: str
    width: int
    height: int
    title: str
    duration: int  # seconds
    extractor: str  # yt-dlp extractor key, e.g. "TikTok", "Instagram", "Twitter"


def download_social_video(url: str, output_dir: Path, max_res: int = settings.max_video_resolution) -> DownloadResult:
    """
    Synchronous yt-dlp download. Call via asyncio.to_thread in the handler.

    Raises SocialDownloadError for unrecoverable failures (private/removed/geo-blocked),
    TransientDownloadError for the ones worth another attempt (429/5xx/timeouts).
    """
    ydl_opts = {
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "format": (
            f"worstvideo[ext=mp4][height>={max_res}]+bestaudio[ext=m4a]/"
            f"worst[ext=mp4][height>={max_res}]/best[ext=mp4]/best"
        ),
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
        # Re-encode to H.264/AAC with iOS-compatible settings:
        # - yuv420p: iOS requires 8-bit 4:2:0 chroma
        # - faststart: moves moov atom to front so iOS can start playback immediately
        # - profile main: avoids B-frame issues on some decoders
        # - scale: this merger step is already a mandatory re-encode, so capping height here is free
        "postprocessor_args": {
            "merger": [
                "-vcodec", "libx264",
                "-profile:v", "main",
                "-pix_fmt", "yuv420p",
                "-vf", f"scale=-2:'min({max_res},ih)'",
                "-acodec", "aac",
                "-movflags", "+faststart",
            ],
        },
    }
    info = extract_info(url, ydl_opts, SocialDownloadError, download=True)
    if info is None:
        raise SocialDownloadError(f"Could not extract media from {url}")

    video_id = info["id"]
    file_path = output_dir / f"{video_id}.mp4"
    if not file_path.exists():
        # Carousel items (e.g. Instagram img_index) get a per-item ID different
        # from the parent post ID, so the expected filename won't match.
        mp4_files = list(output_dir.glob("*.mp4"))
        if not mp4_files:
            raise SocialDownloadError(f"Downloaded file not found: {file_path}")
        file_path = mp4_files[0]

    width = info.get("width") or 0
    height = info.get("height") or 0
    if not width or not height:
        width, height = _probe_dimensions(file_path)

    dr = DownloadResult(
        file_path=file_path,
        video_id=video_id,
        width=width,
        height=height,
        title=info.get("title") or "",
        duration=int(info.get("duration") or 0) or _probe_duration(file_path),
        extractor=info.get("extractor_key") or "unknown",
    )
    log.info("downloaded %s", dr)
    return dr
