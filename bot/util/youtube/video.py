import logging
import math
import subprocess
from pathlib import Path

import ffmpeg
from pytubefix import Stream

from bot.config import settings

from .enum import TargetLang
from .exc import YouTubeError, translates_youtube_errors
from .schema import YouTubeVideoData
from .translate import maybe_translate_audio

MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50MB


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


def pick_stream(
    video: YouTubeVideoData, output_path: Path, min_res: int, max_res: int = settings.max_video_resolution
) -> tuple[Stream, int, Path]:
    # select a stream that can be split into as few parts as possible
    # We always want the highest audio quality
    audio_stream_path = get_audio_stream(video, output_path)
    audio_size = Path(audio_stream_path).stat().st_size

    video_streams = (
        video.yt.streams
        .filter(file_extension='mp4', subtype='mp4', only_video=True)
        .order_by('resolution')
        .desc()
    )
    # filter only supported streams
    video_streams = [
        s for s in video_streams if
        s.resolution
        and int(s.resolution.replace('p', '')) >= min_res
        and 'avc1' in s.codecs[0]
    ]

    # clear streams from those who lie about their resolution
    real_res_to_stream = {}
    for stream in video_streams:
        width, height = get_resolution(stream)
        key = (width, height)
        if key in real_res_to_stream:
            log.info('duplicate res %dx%d stream: %s', width, height, stream)
        real_res_to_stream[key] = stream

    stream_heights = {stream: height for (_, height), stream in real_res_to_stream.items()}

    # tier 1: streams at or above the cap, closest fit first (smallest first — minimizes re-encode work)
    tier1 = sorted(
        (s for s in stream_heights if stream_heights[s] >= max_res),
        key=lambda s: stream_heights[s],
    )
    # tier 2 (fallback, only used if tier 1 is empty): source tops out below the cap, take the best available
    tier2 = sorted(
        (s for s in stream_heights if stream_heights[s] < max_res),
        key=lambda s: stream_heights[s],
        reverse=True,
    )
    video_streams = tier1 or tier2
    log.info('supported adaptive video streams: %s', video_streams)

    for n_parts in range(1, 11):  # 10 max (what an album can fit)
        for stream in video_streams:
            stream: Stream
            total_size = audio_size + stream.filesize

            max_size = MAX_FILE_SIZE_BYTES * 0.98

            log.info(
                "%dMb total size (%dMb/part) for %d parts for %s",
                total_size // 1024 // 1024,
                total_size // 1024 // 1024 // n_parts,
                n_parts,
                stream
            )
            if total_size > max_size * n_parts:
                continue

            # download video stream to assess its real size
            video_stream_filename = Path(f'{video.yt.video_id}.{stream.resolution}.{stream.codecs[0]}.video.mp4')
            video_stream_path = output_path / video_stream_filename
            if not video_stream_path.exists():
                log.info('downloading %s video stream', video_stream_filename)
                video_stream_path = stream.download(output_path=str(output_path), filename=str(video_stream_filename))
                video_stream_path = Path(video_stream_path)

            log.info('%s size %dMb', video_stream_path, video_stream_path.stat().st_size // 1024 // 1024)
            # merge A+V and recheck the total size
            merged_stream_filename = Path(
                f'{video.yt.video_id}.{stream.resolution}.{stream.codecs[0]}.{video.target_lang}.mp4'
            )
            merged_stream_path = output_path / merged_stream_filename
            if not merged_stream_path.exists():
                log.info('merging %s and %s', video_stream_path, audio_stream_path)
                real_height = stream_heights[stream]
                if real_height <= max_res:
                    video_codec_args = ['-c:v', 'copy']  # Copy video codec without re-encoding
                else:
                    video_codec_args = ['-vf', f'scale=-2:{max_res}', '-c:v', 'libx264']
                command = [
                    'ffmpeg',
                    '-y',  # Overwrite an output file if exists
                    '-i', str(video_stream_path),
                    '-i', audio_stream_path,
                    '-map', '0:v:0',  # Take video from the first input
                    '-map', '1:a:0',  # Take audio from the second input
                    *video_codec_args,
                    '-c:a', 'aac',    # Ensure audio is in the proper format
                    # '-b:a', '192k',  # Optional: control audio quality
                    '-movflags', '+faststart',
                    str(merged_stream_path)
                ]
                subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            merged_size = merged_stream_path.stat().st_size
            log.info("%s merged size: %.3fMb", merged_stream_path, merged_size / 1024 / 1024)

            if merged_size > max_size * n_parts:
                # if the video is too big, don't select it or
                log.info("%s is too big for %d parts, continue", merged_stream_filename, n_parts)
                continue

            log.info('selected stream (%d parts, %dMb merged size): %s', n_parts, merged_size // 1024 // 1024, stream)
            return stream, n_parts, merged_stream_path

    raise YouTubeError(f'no suitable video stream found for {video.yt.length}s video length')


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
    max_res: int = settings.max_video_resolution,
) -> tuple[Stream, list[Path]]:
    output_path = Path(output_path)
    # pick one that fits best
    video_stream, n_parts, video_path = pick_stream(video, output_path, min_res, max_res)

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
        too_big_files = [file for file in video_paths if file.stat().st_size > MAX_FILE_SIZE_BYTES]
        if too_big_files:
            if n_parts == 10:
                raise YouTubeError("The video is too big and already split for 10 parts.")
            n_parts += 1
            log.info("video part size is too big, splitting for %d parts", n_parts)
            continue
        else:
            break

    return video_stream, video_paths
