import logging
import subprocess
from pathlib import Path

from faster_whisper import WhisperModel
from pydub import AudioSegment
from pytubefix import YouTube

from .enum import SourceLang
from .schema import YouTubeVideoData

log = logging.getLogger(__name__)

_whisper_model: WhisperModel | None = None


def _get_whisper_model() -> WhisperModel:
    global _whisper_model
    if _whisper_model is None:
        _whisper_model = WhisperModel("tiny", device="cpu")
    return _whisper_model


def maybe_translate_audio(
    video: YouTubeVideoData,
    output_dir: str,
    source_audio_path: str,
) -> Path | None:
    video.source_lang = detect_source_lang(source_audio_path)
    if not video.source_lang or video.source_lang == SourceLang.MISSING:
        return None

    if video.source_lang.value == video.target_lang.value:
        log.info("original lang is the same as target, skipping translation")
        return None

    translated_audio_path = translate_audio(
        video.yt, output_dir, str(video.target_lang)
    )
    if not translated_audio_path:
        return None

    # the video successfully translated to a target language
    video.translated_lang = video.target_lang
    result_audio_path = (
        Path(output_dir)
        / f"{video.yt.video_id}.translated.{video.target_lang}.mixed.mp3"
    )

    # mix original and translated audios
    mix_audio(source_audio_path, str(translated_audio_path), str(result_audio_path))
    log.info("original and translated files are mixed into %s", result_audio_path)

    return result_audio_path


def detect_source_lang(audio_path: str) -> SourceLang | None:
    model = _get_whisper_model()

    # Transcribe just to get language info (no need to iterate over segments)
    _, info = model.transcribe(audio_path, beam_size=5)

    code, prob = info.language, info.language_probability

    if prob < 0.1:
        # Very low confidence, likely music
        log.info("%s lang confidence is %d, assuming music", code, prob)
        return SourceLang.MISSING

    if code not in SourceLang:
        log.info("%s detected but is not supported", code)
        return None

    lang = SourceLang(code)
    log.info("%s detected with %.3f confidence", lang, prob)
    return lang


def translate_audio(yt: YouTube, output_dir: str, target_lang: str) -> Path | None:
    output_file = Path(f"{yt.video_id}.translated.{target_lang}.mp3")
    command = [
        "vot-cli",
        f"--output={output_dir}",
        f"--output-file={output_file}",
        f"--reslang={target_lang}",
        yt.watch_url,
    ]
    try:
        # set a hard limit for a command (in case it hangs for some reason)
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    except subprocess.CalledProcessError:
        log.info("failed to translate %s to %s", yt.video_id, target_lang)
        return None
    except subprocess.TimeoutExpired:
        log.info("failed to translate %s to %s (timeout)", yt.video_id, target_lang)
        return None

    output_path = Path(output_dir) / output_file
    if not output_path.exists():
        log.info(
            "failed to translate %s to %s, no audio file found after calling the subprocess",
            yt.video_id,
            target_lang,
        )
        return None

    log.info("translated audio downloaded to %s", output_file)
    return output_path


def mix_audio(
    original_audio_path: str,
    translated_audio_path: str,
    output_path: str,
    original_volume_db: int = -10,
) -> None:
    """
    Mixes two audio files: the original and the translated, with the original being quieter.

    :param original_audio_path: Path to the original audio file.
    :param translated_audio_path: Path to the translated audio file.
    :param output_path: Path to save the output mixed audio.
    :param original_volume_db: Volume reduction for the original audio (in dB). Default is -10 dB.
    """
    log.info("mixing %s and %s", original_audio_path, translated_audio_path)

    original_audio = AudioSegment.from_file(original_audio_path)
    translated_audio = AudioSegment.from_file(translated_audio_path)

    original_audio = original_audio + original_volume_db

    # Pad the shorter track with silence so both are equal length
    if len(original_audio) > len(translated_audio):
        translated_audio = translated_audio + AudioSegment.silent(
            duration=len(original_audio) - len(translated_audio)
        )
    elif len(translated_audio) > len(original_audio):
        original_audio = original_audio + AudioSegment.silent(
            duration=len(translated_audio) - len(original_audio)
        )

    mixed_audio = original_audio.overlay(translated_audio)
    mixed_audio.export(output_path, format="mp3")
