import asyncio
import logging
import math
import tempfile
from pathlib import Path

from aiogram import Bot, types
from aiogram.exceptions import TelegramNetworkError

from bot.config import settings
from bot.events.signals import on_social_video_fail, on_yt_video_fail
from bot.util.audio.download import download_track
from bot.util.audio.exc import AudioDownloadError
from bot.util.audio.schema import AudioTrackData
from bot.util.social.download import MediaFile, download_social_video
from bot.util.social.exc import SocialDownloadError
from bot.util.social.schema import MediaItem, SocialVideoData
from bot.util.youtube.exc import YouTubeError
from bot.util.youtube.schema import YouTubeVideoData
from bot.util.youtube.video import (
    check_download_adaptive,
    get_resolution,
    split_video,
)

log = logging.getLogger(__name__)


async def _upload_parts_to_dump_chat(bot: Bot, file_paths: list[Path], width: int, height: int) -> list[str]:
    file_ids = []
    for file_path in file_paths:
        for i in range(3):
            try:
                media_message = await bot.send_video(
                    settings.dump_chat_id,
                    types.FSInputFile(file_path),
                    width=width,
                    height=height,
                )
                break
            except TelegramNetworkError:
                if i == 2:
                    raise
                log.warning('failed to send a video file, retrying in 2 seconds')
                await asyncio.sleep(2)
        log.info("sent %s", file_path)
        file_ids.append(media_message.video.file_id)
    return file_ids


async def _send_one_to_dump_chat(bot: Bot, media: MediaFile) -> types.Message:
    for i in range(3):
        try:
            if media.kind == "photo":
                return await bot.send_photo(
                    settings.dump_chat_id,
                    types.FSInputFile(media.file_path),
                )
            return await bot.send_video(
                settings.dump_chat_id,
                types.FSInputFile(media.file_path),
                width=media.width,
                height=media.height,
            )
        except TelegramNetworkError:
            if i == 2:
                raise
            log.warning("failed to send a media file, retrying in 2 seconds")
            await asyncio.sleep(2)
    raise RuntimeError("unreachable: the retry loop above always returns or re-raises")


async def _upload_media_to_dump_chat(bot: Bot, files: list[MediaFile]) -> list[MediaItem]:
    """Parks every item in the dump chat and returns the file ids, in order.

    Kept separate from `_upload_parts_to_dump_chat`, which YouTube still uses:
    that one sends N slices of one video under a single width/height, this one
    sends N independent items that each carry their own kind and dimensions.
    """
    items: list[MediaItem] = []
    for media in files:
        media_message = await _send_one_to_dump_chat(bot, media)
        log.info("sent %s", media.file_path)

        if media.kind == "photo":
            sizes = media_message.photo
            if not sizes:
                raise SocialDownloadError(f"Telegram returned no photo for {media.file_path}")
            file_id = sizes[-1].file_id
        else:
            video = media_message.video
            if video is None:
                raise SocialDownloadError(f"Telegram returned no video for {media.file_path}")
            file_id = video.file_id
        items.append(MediaItem(file_id=file_id, kind=media.kind, width=media.width, height=media.height))
    return items


def _split_oversized(media: MediaFile, output_dir: Path) -> list[MediaFile]:
    """Cuts one too-large video into <= 10 sendable parts.

    Each item gets its own output directory: `split_video` names parts after the
    input, and a carousel runs this more than once into the same temp tree.
    """
    part_dir = output_dir / f"parts-{media.file_path.stem}"
    part_dir.mkdir(parents=True, exist_ok=True)

    file_size = media.file_path.stat().st_size
    n_parts = math.ceil(file_size / settings.max_upload_size_bytes)
    file_paths = split_video(
        duration_seconds=media.duration,
        input_path=media.file_path,
        output_dir=part_dir,
        n_parts=n_parts,
    )
    while any(p.stat().st_size > settings.max_upload_size_bytes for p in file_paths):
        n_parts += 1
        if n_parts > 10:
            raise SocialDownloadError("Video too large, cannot split into <= 10 parts")
        file_paths = split_video(
            duration_seconds=media.duration,
            input_path=media.file_path,
            output_dir=part_dir,
            n_parts=n_parts,
        )
    return [
        MediaFile(file_path=p, kind="video", width=media.width, height=media.height, duration=0) for p in file_paths
    ]


async def handle_youtube_video(bot: Bot, video: YouTubeVideoData) -> YouTubeVideoData:
    """Fires `on_yt_video_fail` for every failure, from exactly one place.

    The fail signal used to sit next to the retry loop's `if exc:`, which meant
    two whole classes of failure were never counted: `YouTubeError` re-raised as
    unrecoverable (private, removed, geo-blocked), and anything thrown *after*
    the download -- the dump-chat upload, where a Telegram-side timeout on
    2026-09-13 cost a full redundant re-download and left `/stats` reading 0 ✗.
    Wrapping is what makes "one failure, one count" true regardless of path.

    Still one count per job ATTEMPT, not per link: dramatiq retries call this
    again. That is the pre-existing meaning of the counter (45 links × 3
    attempts read as 135 in September) and is deliberately left alone here.
    """
    try:
        return await _handle_youtube_video(bot, video)
    except Exception:
        await on_yt_video_fail.send(video.link)
        raise


async def _handle_youtube_video(bot: Bot, video: YouTubeVideoData) -> YouTubeVideoData:
    with tempfile.TemporaryDirectory() as tmp:
        exc = None
        for i in range(3):
            try:
                stream, file_paths = await asyncio.to_thread(
                    check_download_adaptive,
                    video=video,
                    output_path=tmp,
                )
                exc = None
                break
            except YouTubeError:
                # raise YouTubeError directly (it is an unrecoverable error)
                raise
            except Exception as ex:
                exc = ex
                log.error("failed to download %s on try #%d: %r", video.yt.video_id, i + 1, exc)
                await asyncio.sleep(2)

        if exc:
            log.error("finally failed to download youtube link %s: %r", video.link, exc)
            raise exc

        width, height = get_resolution(stream)
        video.width = width
        video.height = height
        # free here (yt just fetched); spares every later redelivery a live lookup
        video.capture_metadata()

        log.info('sending %d part(s) to dump chat to obtain file ids', len(file_paths))
        video.file_ids = await _upload_parts_to_dump_chat(bot, file_paths, width, height)
        return video


async def handle_social_video(bot: Bot, video: SocialVideoData) -> SocialVideoData:
    """Fires `on_social_video_fail` for every failure, from exactly one place.

    Same gap as `handle_youtube_video`, and wider here: `SocialDownloadError` is
    re-raised as unrecoverable on the line below, so every private account,
    removed post, carousel-without-video and login wall went uncounted. That is
    why `fail:social` had fired on three days in the whole 90-day window while
    messages were steadily dead-lettering.
    """
    try:
        return await _handle_social_video(bot, video)
    except Exception:
        await on_social_video_fail.send(video.link)
        raise


async def _handle_social_video(bot: Bot, video: SocialVideoData) -> SocialVideoData:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        exc = None

        for i in range(3):
            try:
                result = await asyncio.to_thread(download_social_video, video.link, tmp_path)
                exc = None
                break
            except SocialDownloadError:
                raise  # unrecoverable — private account, removed video, geo-block
            except Exception as ex:
                exc = ex
                log.error("failed to download social %s on try #%d: %r", video.link, i + 1, exc)
                await asyncio.sleep(2)

        if exc:
            log.error("finally failed to download social link %s: %r", video.link, exc)
            raise exc

        video.video_id = result.video_id
        video.title = result.title
        video.missing = result.missing
        video.origin = result.extractor.lower()

        sendable: list[MediaFile] = []
        for media in result.files:
            if media.kind == "photo" or media.file_path.stat().st_size <= settings.max_upload_size_bytes:
                sendable.append(media)
                continue
            sendable.extend(_split_oversized(media, tmp_path))

        # Kept for the cached-metadata path; the send path now reads each item's
        # own dimensions, which a carousel does not share.
        video.width = sendable[0].width
        video.height = sendable[0].height

        log.info("sending %d item(s) to dump chat for %s", len(sendable), video.link)
        video.items = await _upload_media_to_dump_chat(bot, sendable)
        return video


async def handle_audio_page(bot: Bot, tracks: list[AudioTrackData]) -> int:
    """
    Downloads and dump-chat-uploads every track in `tracks` missing a file_id,
    mutating each in place. Returns how many tracks failed and were skipped --
    one bad track (geo-blocked/removed) shouldn't take down the whole page.
    Up to 3 tracks are downloaded/uploaded concurrently.

    Unlike the two video handlers, this fires NO fail signal, so skipped tracks
    stay invisible to `/stats`. Deliberate: a page that delivered 18 of 20 tracks
    is not a failed request, and the ✓/✗ counters are per request. Counting
    tracks would need its own signal and counter key, not a reuse of these.
    """
    semaphore = asyncio.Semaphore(3)

    async def process_one(track: AudioTrackData, tmp_path: Path) -> bool:
        async with semaphore:
            file_path = None
            exc = None
            for i in range(3):
                try:
                    file_path = await asyncio.to_thread(download_track, track, tmp_path)
                    exc = None
                    break
                except AudioDownloadError as ex:
                    exc = ex
                    break  # unrecoverable for this track -- don't retry
                except Exception as ex:
                    exc = ex
                    log.error("failed to download track %s on try #%d: %r", track.webpage_url, i + 1, exc)
                    await asyncio.sleep(2)

            if exc or file_path is None:
                log.error("giving up on track %s: %r", track.webpage_url, exc)
                return False

            media_message = None
            for i in range(3):
                try:
                    media_message = await bot.send_audio(
                        settings.dump_chat_id,
                        types.FSInputFile(file_path),
                        performer=track.uploader,
                        title=track.title,
                        duration=track.duration,
                    )
                    break
                except TelegramNetworkError:
                    if i == 2:
                        raise
                    log.warning('failed to send an audio track, retrying in 2 seconds')
                    await asyncio.sleep(2)

            track.file_id = media_message.audio.file_id
            log.info("uploaded track %s -> %s", track.webpage_url, track.file_id)
            return True

    results: list[bool] = []

    async def process_one_and_collect(track: AudioTrackData, tmp_path: Path) -> None:
        results.append(await process_one(track, tmp_path))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pending = [t for t in tracks if not t.file_id]
        try:
            async with asyncio.TaskGroup() as tg:
                for t in pending:
                    tg.create_task(process_one_and_collect(t, tmp_path))
        except* Exception as eg:
            # TaskGroup wraps propagated exceptions in an ExceptionGroup (PEP 654).
            # Unwrap to the first real exception so callers can still catch e.g.
            # TelegramNetworkError directly, same as the rest of this module does.
            for exc in eg.exceptions[1:]:
                log.error("additional error during page processing: %r", exc)
            raise eg.exceptions[0] from None

    return sum(1 for ok in results if not ok)
