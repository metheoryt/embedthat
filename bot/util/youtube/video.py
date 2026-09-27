import logging
import math
import subprocess
from pathlib import Path
from typing import cast

import ffmpeg
from pytubefix import Stream

from bot.config import settings
from bot.util.ladder import fits, rung_candidates, scale_filter

from .enum import TargetLang
from .exc import YouTubeError, translates_youtube_errors
from .schema import YouTubeVideoData
from .translate import maybe_translate_audio

log = logging.getLogger(__name__)


def get_resolution(stream: Stream) -> tuple[int, int]:
    try:
        probe = ffmpeg.probe(stream.url, v='error', select_streams='v:0', show_entries='stream=width,height')
        width = probe['streams'][0]['width']
        height = probe['streams'][0]['height']
        log.info("probed resolution %dx%d for %s", width, height, stream)
        return width, height
    except (ffmpeg.Error, KeyError, IndexError) as e:
        log.warning("ffmpeg probe failed, falling back to stream metadata: %s", e)

    if stream.width and stream.height:
        return stream.width, stream.height

    # last resort: parse the resolution string (e.g. "720p" → height=720)
    if stream.resolution:
        height = int(stream.resolution.replace('p', ''))
        return height * 16 // 9, height

    return 720, 480


@translates_youtube_errors
def get_audio_stream(video: YouTubeVideoData, output_path: Path) -> str | Path:
    audio_streams = video.yt.streams.filter(file_extension='mp4', only_audio=True).order_by('abr').desc()
    log.info('adaptive audio streams: %s', audio_streams)
    audio_stream = audio_streams.first()
    if not audio_stream:
        # we don't want adaptive without a sound
        raise YouTubeError('no adaptive audio stream found')

    log.info('downloading audio stream')
    audio_stream_path = audio_stream.download(output_path=str(output_path), filename=f'{video.yt.video_id}.audio.mp4')
    if not audio_stream_path:
        # download() returns None only on an interrupt_checker abort, which we never pass;
        # make the impossible case a domain error instead of a TypeError in the caller
        raise YouTubeError('audio stream download returned no path')

    if settings.enable_audio_translation and video.target_lang != TargetLang.ORIGINAL:
        log.info('trying to translate audio stream to %s', video.target_lang)
        translated_audio_path = maybe_translate_audio(video, str(output_path), audio_stream_path)
        if translated_audio_path:
            log.info('choosing %s over %s', translated_audio_path, audio_stream_path)
            return translated_audio_path

    return audio_stream_path


def _download_video_stream(video: YouTubeVideoData, stream: Stream, output_path: Path) -> Path:
    filename = Path(f'{video.yt.video_id}.{stream.resolution}.{stream.codecs[0]}.video.mp4')
    path = output_path / filename
    if not path.exists():
        log.info('downloading %s video stream', filename)
        downloaded = stream.download(output_path=str(output_path), filename=str(filename))
        if not downloaded:
            # as for the audio stream: None only on an interrupt_checker abort
            raise YouTubeError('video stream download returned no path')
        path = Path(downloaded)
    log.info('%s size %dMb', path, path.stat().st_size // 1024 // 1024)
    return path


def _merge(
    video: YouTubeVideoData,
    stream: Stream,
    dims: tuple[int, int],
    rung: int,
    audio_stream_path: str | Path,
    output_path: Path,
) -> Path:
    # The rung is in the name: one source stream can serve two rungs (copied at
    # its own, scaled down below it), and those must not collide.
    merged = output_path / f'{video.yt.video_id}.{rung}.{stream.resolution}.{stream.codecs[0]}.{video.target_lang}.mp4'
    if merged.exists():
        return merged
    video_stream_path = _download_video_stream(video, stream, output_path)
    vf = scale_filter(*dims, rung)
    video_codec_args = ['-c:v', 'copy'] if vf is None else ['-vf', vf, '-c:v', 'libx264']
    log.info('merging %s and %s at %dp (%s)', video_stream_path, audio_stream_path, rung, vf or 'copy')
    command = [
        'ffmpeg',
        '-y',  # Overwrite an output file if exists
        '-i', str(video_stream_path),
        '-i', str(audio_stream_path),
        '-map', '0:v:0',  # Take video from the first input
        '-map', '1:a:0',  # Take audio from the second input
        *video_codec_args,
        '-c:a', 'aac',    # Ensure audio is in the proper format
        '-movflags', '+faststart',
        str(merged)
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("%s merged size: %.3fMb", merged, merged.stat().st_size / 1024 / 1024)
    return merged


def pick_stream(video: YouTubeVideoData, output_path: Path, min_res: int) -> tuple[Stream, int, Path]:
    """The highest rung that fits its own cap in one file; else the lowest rung, split.

    Rungs are the OUTER loop. With parts outermost (the old shape) a long video
    was split at the top rung instead of dropping a rung.
    """
    # Once, whatever rung wins: the audio (and its optional translation) is the
    # same for every rung.
    audio_stream_path = get_audio_stream(video, output_path)
    audio_size = Path(audio_stream_path).stat().st_size

    video_streams = (
        video.yt.streams
        .filter(file_extension='mp4', subtype='mp4', only_video=True)
        .order_by('resolution')
        .desc()
    )
    # filter only supported streams
    supported: list[Stream] = [
        s for s in video_streams if
        s.resolution
        and int(s.resolution.replace('p', '')) >= min_res
        and 'avc1' in s.codecs[0]
    ]

    # clear streams from those who lie about their resolution
    real_res_to_stream: dict[tuple[int, int], Stream] = {}
    for stream in supported:
        width, height = get_resolution(stream)
        key = (width, height)
        if key in real_res_to_stream:
            log.info('duplicate res %dx%d stream: %s', width, height, stream)
        real_res_to_stream[key] = stream
    stream_dims = {stream: dims for dims, stream in real_res_to_stream.items()}

    candidates = rung_candidates(stream_dims)
    log.info('rung candidates: %s', candidates)
    if not candidates:
        raise YouTubeError(f'no supported video stream for {video.yt.video_id}')

    lowest_rung, lowest_stream = candidates[-1]
    for rung, stream in candidates:
        estimate = audio_size + cast(int, stream.filesize)  # exact content length
        if not fits(rung, estimate):
            log.info('%dp: %dMb estimated, over its cap -- skipped without download', rung, estimate // 1024 // 1024)
            continue
        merged = _merge(video, stream, stream_dims[stream], rung, audio_stream_path, output_path)
        if fits(rung, merged.stat().st_size):
            log.info('selected %dp (%dMb): %s', rung, merged.stat().st_size // 1024 // 1024, stream)
            return stream, 1, merged
        log.info('%dp merged over its cap, trying the next rung', rung)
        if (rung, stream) != (lowest_rung, lowest_stream):
            merged.unlink()  # the lowest rung's file is kept: the split below reuses it

    # Nothing fits in one file: the lowest rung, split against the upload limit.
    merged = _merge(video, lowest_stream, stream_dims[lowest_stream], lowest_rung, audio_stream_path, output_path)
    n_parts = math.ceil(merged.stat().st_size / (settings.max_upload_size_bytes * 0.98))
    if n_parts > 10:  # 10 max (what an album can fit)
        raise YouTubeError(f'no suitable video stream found for {video.yt.length}s video length')
    log.info('selected %dp split into %d parts (%dMb merged): %s',
             lowest_rung, n_parts, merged.stat().st_size // 1024 // 1024, lowest_stream)
    return lowest_stream, n_parts, merged


def split_video(duration_seconds: int, input_path: Path, output_dir: Path, n_parts: int) -> list[Path]:
    if not duration_seconds:
        raise ValueError(f"Cannot split {input_path.name}: duration is 0")
    segment_time = math.ceil(duration_seconds / n_parts)
    if not segment_time:
        raise ValueError(f"Cannot split {input_path.name}: segment time is 0")

    log.info('video of %d duration will be split by %d parts of %d seconds', duration_seconds, n_parts, segment_time)
    output_pattern = output_dir / (input_path.stem + "_part_%03d.mp4")

    subprocess.run(
        [
            "ffmpeg",
            "-i", str(input_path),
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(segment_time),
            "-reset_timestamps", "1",
            str(output_pattern)
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    parts = sorted(output_dir.glob(input_path.stem + "_part_*.mp4"))
    if len(parts) != n_parts:
        raise ValueError(f"Splitting {input_path.name} into {n_parts} parts: got {len(parts)} parts")
    return parts


@translates_youtube_errors
def check_download_adaptive(
    video: YouTubeVideoData,
    output_path: str,
    min_res: int = 360,
) -> tuple[Stream, list[Path]]:
    output_path = Path(output_path)
    # pick one that fits best
    video_stream, n_parts, video_path = pick_stream(video, output_path, min_res)

    video_paths = []
    while True:
        # split the merged file into more and more parts, until every part is < 50Mb
        if video_paths:
            # clean up old split parts
            for file in video_paths:
                file: Path
                file.unlink()

        if n_parts == 1:
            video_paths = [video_path]
        else:
            video_paths = split_video(
                duration_seconds=video.yt.length,
                input_path=video_path,
                output_dir=output_path,
                n_parts=n_parts
            )

        for file in video_paths:
            log.info('%s size: %dMb', file.name, file.stat().st_size // 1024 // 1024)

        # the second size check is after split
        too_big_files = [file for file in video_paths if file.stat().st_size > settings.max_upload_size_bytes]
        if too_big_files:
            if n_parts == 10:
                raise YouTubeError("The video is too big and already split for 10 parts.")
            n_parts += 1
            log.info("video part size is too big, splitting for %d parts", n_parts)
            continue
        else:
            break

    return video_stream, video_paths
